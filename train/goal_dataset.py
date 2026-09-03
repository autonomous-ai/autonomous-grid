"""Build trustworthy training data from completed Grid Goals.

The relay already owns the hard part: an immutable record tying each turn, tool call, model
request, Git handoff and evaluation to a leased worker.  This module is the deliberately boring
consumer of that record.  It verifies before transforming, redacts before writing, and keeps the
held-out split at the *Goal* boundary so turns from one trajectory can never leak into both sets.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from .capture import clip, redact

SCHEMA_VERSION = 1

_SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:access_?token|api_?key|authorization|cookie|credential|password|private_?key|"
    r"refresh_?token|secret|session)(?:$|_)", re.IGNORECASE)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_URL_CREDENTIAL = re.compile(r"(?i)(https?://)[^\s/@:]+:[^\s/@]+@")


@dataclasses.dataclass(frozen=True)
class DatasetResult:
    destination: Path
    accepted: int
    rejected: int
    duplicates: int
    train: int
    held_out: int


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def sanitize(value: object, *, key: str = "") -> object:
    """Recursively redact a JSON value without changing its container shape.

    Text-pattern redaction alone cannot protect a field literally named ``access_token``.  Key
    names therefore win, then bounded text is passed through the capture plane's established PII
    scrubber plus credentials that commonly appear in HTTP/tool evidence.
    """
    if _SENSITIVE_KEY.search(key):
        return "[secret]"
    if isinstance(value, dict):
        return {str(k): sanitize(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(item, key=key) for item in value]
    if isinstance(value, str):
        text = redact(clip(value, limit=64_000))
        text = _BEARER.sub("Bearer [secret]", text)
        text = _JWT.sub("[secret]", text)
        return _URL_CREDENTIAL.sub(r"\1[secret]@", text)
    return value


def _accepted_evals(evidence: dict) -> tuple[list[dict], list[str]]:
    goal = evidence.get("goal") if isinstance(evidence.get("goal"), dict) else {}
    specs = goal.get("evals") if isinstance(goal.get("evals"), list) else []
    turns = evidence.get("turns") if isinstance(evidence.get("turns"), list) else []
    if not specs:
        return [], ["no independent Grid eval"]
    if not turns or not isinstance(turns[-1], dict):
        return [], ["no final Goal turn"]
    final_id = turns[-1].get("id")
    runs = evidence.get("eval_runs") if isinstance(evidence.get("eval_runs"), list) else []
    accepted = [run for run in runs if isinstance(run, dict)
                and run.get("turn_id") == final_id and run.get("accepted") is True]
    failures = []
    for spec in specs:
        definition = spec.get("definition_id") if isinstance(spec, dict) else None
        matches = [run for run in accepted if run.get("definition_id") == definition
                   and run.get("state") == "passed" and run.get("passed") is True]
        if len(matches) != 1:
            failures.append(f"eval {definition or '?'} has {len(matches)} accepted passing runs")
    return accepted, failures


def trajectory_record(evidence: dict, *, grid: str) -> tuple[dict | None, list[str]]:
    """Convert one verified evidence document into the versioned training record shape."""
    goal = evidence.get("goal") if isinstance(evidence.get("goal"), dict) else {}
    turns = evidence.get("turns") if isinstance(evidence.get("turns"), list) else []
    accepted_evals, failures = _accepted_evals(evidence)
    if failures:
        return None, failures
    messages: list[dict] = [{
        "role": "system",
        "content": (f"Goal: {goal.get('objective') or ''}\n"
                    f"Done when: {goal.get('done_when') or ''}"),
    }]
    for turn in turns:
        if not isinstance(turn, dict):
            return None, ["malformed turn"]
        prompt, output = turn.get("prompt"), turn.get("output")
        if not isinstance(prompt, str) or not prompt.strip():
            return None, [f"turn {turn.get('id') or '?'} has no prompt"]
        if not isinstance(output, str) or not output.strip():
            return None, [f"turn {turn.get('id') or '?'} has no output"]
        messages.extend(({"role": "user", "content": prompt},
                         {"role": "assistant", "content": output}))

    scores = [float(run["score"]) for run in accepted_evals
              if isinstance(run.get("score"), (int, float))
              and not isinstance(run.get("score"), bool)
              and math.isfinite(float(run["score"]))]
    record = sanitize({
        "schema_version": SCHEMA_VERSION,
        "goal_id": goal.get("id"),
        "grid": grid,
        "objective": goal.get("objective"),
        "done_when": goal.get("done_when"),
        "model": goal.get("model"),
        "status": goal.get("status"),
        "score": (sum(scores) / len(scores)) if scores else 1.0,
        "messages": messages,
        "turns": turns,
        "tool_events": [item for item in evidence.get("attempt_events", [])
                        if isinstance(item, dict)
                        and isinstance(item.get("event"), dict)
                        and str(item["event"].get("type") or "").startswith("goal.")],
        "inference": evidence.get("inference", []),
        "evals": goal.get("evals", []),
        "eval_runs": accepted_evals,
        "relationships": evidence.get("relationships", {}),
    })
    assert isinstance(record, dict)
    # Relay ids say *where* an action ran, not what was learned. Exclude them so repeating the same
    # trajectory on a different machine is deduplicated instead of overweighting one answer.
    tool_semantics = [{key: item["event"].get(key)
                       for key in ("type", "tool", "arguments", "result", "success")
                       if key in item["event"]}
                      for item in record["tool_events"]]
    record["fingerprint"] = _hash({"messages": record["messages"],
                                    "tool_events": tool_semantics})
    return record, []


def _held_out(fingerprint: str, seed: str, fraction: float) -> bool:
    bucket = int(hashlib.sha256(f"{seed}:{fingerprint}".encode()).hexdigest()[:16], 16)
    return bucket / float(0xFFFFFFFFFFFFFFFF) < fraction


def split_records(records: list[dict], *, fraction: float, seed: str) -> tuple[list[dict], list[dict]]:
    """Return (train, held-out), deterministically and only at whole-trajectory boundaries."""
    ordered = sorted(records, key=lambda row: (row["fingerprint"], str(row.get("goal_id") or "")))
    held = [row for row in ordered if _held_out(row["fingerprint"], seed, fraction)]
    held_fingerprints = {row["fingerprint"] for row in held}
    train = [row for row in ordered if row["fingerprint"] not in held_fingerprints]
    # Small smoke-test corpora still need one honest held-out Goal and one training Goal.
    if len(ordered) >= 2 and fraction > 0 and not held:
        held, train = ordered[:1], ordered[1:]
    elif len(ordered) >= 2 and not train:
        train, held = ordered[:1], ordered[1:]
    return train, held


def _jsonl(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                            for row in rows), encoding="utf-8")


def _sft(row: dict) -> dict:
    return {"messages": row["messages"], "metadata": {
        "schema_version": row["schema_version"], "goal_id": row.get("goal_id"),
        "fingerprint": row["fingerprint"], "score": row["score"],
    }}


def _write_parquet(path: Path, rows: list[dict]) -> None:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError(
            "Parquet export needs pyarrow; install the training dependencies with "
            "`pip install 'grid[train]'`") from exc
    flattened = [{
        "schema_version": row["schema_version"], "split": row["split"],
        "goal_id": row.get("goal_id"), "grid": row.get("grid"),
        "model": row.get("model"), "score": row.get("score"),
        "fingerprint": row["fingerprint"],
        "messages_json": json.dumps(row["messages"], ensure_ascii=False),
        "trajectory_json": json.dumps(row, ensure_ascii=False, sort_keys=True),
    } for row in rows]
    pq.write_table(pa.Table.from_pylist(flattened), path)


def build_dataset(goals: Iterable[dict], fetch_evidence: Callable[[str], dict], destination: Path,
                  *, grid: str, holdout_fraction: float = 0.1, seed: str = "1729",
                  output_format: str = "jsonl", force: bool = False,
                  verify: Callable[[dict], list[str]] | None = None) -> DatasetResult:
    """Verify, filter, redact, dedupe, split and atomically publish one dataset directory."""
    from cli.goal import _verify_evidence

    if not 0 <= holdout_fraction < 1:
        raise ValueError("held-out fraction must be at least 0 and less than 1")
    if output_format not in ("jsonl", "parquet", "both"):
        raise ValueError("format must be jsonl, parquet or both")
    destination = Path(destination).expanduser()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())) and not force:
        raise FileExistsError(f"{destination} exists (pass --force to replace it)")
    verify = verify or (lambda record: _verify_evidence(record, require_inference=True))
    records, rejected, seen = [], [], set()
    duplicates = 0
    for summary in goals:
        goal_id = summary.get("id") if isinstance(summary, dict) else None
        if not isinstance(goal_id, str) or not goal_id:
            rejected.append({"goal_id": None, "reasons": ["malformed Goal summary"]})
            continue
        if summary.get("status") != "complete":
            rejected.append({"goal_id": goal_id,
                             "reasons": [f"status is {summary.get('status')!r}"]})
            continue
        try:
            evidence = fetch_evidence(goal_id)
        except Exception as exc:  # one unavailable Goal must not discard all earlier evidence
            rejected.append({"goal_id": goal_id,
                             "reasons": [f"evidence fetch failed: {type(exc).__name__}: {exc}"]})
            continue
        failures = verify(evidence)
        record, transform_failures = trajectory_record(evidence, grid=grid)
        failures.extend(transform_failures)
        if failures or record is None:
            rejected.append({"goal_id": goal_id, "reasons": sanitize(failures)})
            continue
        if record["fingerprint"] in seen:
            duplicates += 1
            rejected.append({"goal_id": goal_id, "reasons": ["duplicate trajectory"]})
            continue
        seen.add(record["fingerprint"])
        records.append(record)

    train, held = split_records(records, fraction=holdout_fraction, seed=seed)
    for row in train:
        row["split"] = "train"
    for row in held:
        row["split"] = "held_out"

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    staging.chmod(0o700)
    try:
        if output_format in ("jsonl", "both"):
            for name, rows in (("train", train), ("held_out", held)):
                _jsonl(staging / name / "trajectories.jsonl", rows)
                _jsonl(staging / name / "sft.jsonl", (_sft(row) for row in rows))
        if output_format in ("parquet", "both"):
            _write_parquet(staging / "trajectories.parquet", train + held)
        _jsonl(staging / "rejected.jsonl", rejected)
        for directory in (item for item in staging.rglob("*") if item.is_dir()):
            directory.chmod(0o700)
        files = sorted(path for path in staging.rglob("*") if path.is_file())
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "kind": "grid-goal-trajectories",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "grid": grid, "format": output_format, "seed": seed,
            "holdout_fraction": holdout_fraction,
            "counts": {"accepted": len(records), "rejected": len(rejected),
                       "duplicates": duplicates, "train": len(train), "held_out": len(held)},
            "files": {str(path.relative_to(staging)): hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in files},
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                                                encoding="utf-8")
        for path in (item for item in staging.rglob("*") if item.is_file()):
            path.chmod(0o600)
        if destination.is_dir():
            shutil.rmtree(destination)
        elif destination.exists():
            destination.unlink()
        os.replace(staging, destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return DatasetResult(destination, len(records), len(rejected), duplicates,
                         len(train), len(held))
