"""Held-out Grid Goal evaluation and soak runs.

A benchmark is an authored suite of measurable Goals against disposable projects/sandboxes.  The
candidate agent never sees the answer key beyond each Goal's immutable eval contract; the relay
runs those evals and this client independently verifies its evidence.  The metric is intentionally
one number: completed-and-verified Goals / submitted Goals.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .goal_dataset import sanitize

SCHEMA_VERSION = 1
TERMINAL = frozenset({"complete", "failed", "cancelled", "budget_limited", "usage_limited"})
_CASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


@dataclass(frozen=True)
class BenchmarkResult:
    run_dir: Path
    passed: int
    total: int
    pass_rate: float
    threshold: float
    complete: bool

    @property
    def met_threshold(self) -> bool:
        return self.complete and self.pass_rate >= self.threshold


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def load_suite(path: Path | str) -> dict:
    source = Path(path).expanduser()
    try:
        suite = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"could not read Goal suite {source}: {exc}") from exc
    if not isinstance(suite, dict) or suite.get("version") != 1:
        raise ValueError("Goal suite must be a JSON object with version 1")
    cases = suite.get("cases")
    if not isinstance(cases, list) or not 1 <= len(cases) <= 1000:
        raise ValueError("Goal suite cases must contain 1-1000 cases")
    seen = set()
    for index, case in enumerate(cases):
        prefix = f"cases[{index}]"
        if not isinstance(case, dict):
            raise ValueError(f"{prefix} must be an object")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not _CASE_ID.fullmatch(case_id):
            raise ValueError(f"{prefix}.id must be a 1-64 character stable identifier")
        if case_id in seen:
            raise ValueError(f"duplicate Goal suite case id {case_id!r}")
        seen.add(case_id)
        for field in ("objective", "done_when"):
            if not isinstance(case.get(field), str) or not case[field].strip():
                raise ValueError(f"{prefix}.{field} must be non-empty text")
        evals = case.get("evals")
        if not isinstance(evals, list) or not evals or any(not isinstance(x, dict) for x in evals):
            raise ValueError(f"{prefix}.evals must contain independent Grid eval definitions")
        if not case.get("project") and not suite.get("project"):
            raise ValueError(f"{prefix} needs project, or the suite needs a default project")
        agents = case.get("agents", suite.get("agents", ["codex"]))
        if (not isinstance(agents, list) or not agents
                or any(agent not in ("codex", "claude") for agent in agents)):
            raise ValueError(f"{prefix}.agents must contain codex and/or claude")
        tools = case.get("tools", suite.get("tools", []))
        if not isinstance(tools, list) or any(not isinstance(tool, dict) for tool in tools):
            raise ValueError(f"{prefix}.tools must be a list of Goal tool definitions")
    return suite


def suite_hash(suite: dict) -> str:
    return hashlib.sha256(_canonical(suite)).hexdigest()


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _new_state(suite: dict, *, grid: str, model: str, repeats: int,
               threshold: float) -> dict:
    digest = suite_hash(suite)
    benchmark_id = str(uuid.uuid4())
    cases = []
    for repetition in range(1, repeats + 1):
        for case in suite["cases"]:
            run_id = f"{case['id']}#{repetition}"
            cases.append({
                "run_id": run_id, "case_id": case["id"], "repetition": repetition,
                "idempotency_key": str(uuid.uuid5(uuid.NAMESPACE_URL,
                                                   f"grid-goal-benchmark:{benchmark_id}:{run_id}")),
                "goal_id": None, "status": "not_submitted", "passed": None,
                "failures": [],
            })
    return {"schema_version": SCHEMA_VERSION, "benchmark_id": benchmark_id,
            "suite_hash": digest, "grid": grid,
            "model": model, "repeats": repeats, "threshold": threshold,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "cases": cases}


def _load_or_create(run_dir: Path, suite: dict, *, grid: str, model: str,
                    repeats: int, threshold: float) -> dict:
    path = run_dir / "benchmark.json"
    if not path.is_file():
        return _new_state(suite, grid=grid, model=model, repeats=repeats, threshold=threshold)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"could not resume {path}: {exc}") from exc
    expected = {"suite_hash": suite_hash(suite), "grid": grid, "model": model,
                "repeats": repeats, "threshold": threshold}
    mismatched = [key for key, value in expected.items() if state.get(key) != value]
    if mismatched:
        raise ValueError("run directory belongs to a different benchmark: " + ", ".join(mismatched))
    return state


def _summarize(state: dict, run_dir: Path) -> BenchmarkResult:
    cases = state["cases"]
    passed = sum(case.get("passed") is True for case in cases)
    complete = all(case.get("status") in TERMINAL for case in cases)
    total = len(cases)
    rate = passed / total if total else 0.0
    state["result"] = {"passed": passed, "total": total, "pass_rate": rate,
                       "complete": complete, "met_threshold": complete and rate >= state["threshold"]}
    if complete and "finished_at" not in state:
        state["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    _write(run_dir / "benchmark.json", state)
    return BenchmarkResult(run_dir, passed, total, rate, state["threshold"], complete)


def run_benchmark(
    suite: dict, run_dir: Path | str, *, grid: str, model: str, repeats: int = 1,
    threshold: float = 1.0, timeout_seconds: float = 86_400, poll_seconds: float = 5,
    wait: bool = True, submit: Callable[[dict, str, str], dict],
    get_status: Callable[[str], dict], get_evidence: Callable[[str], dict],
    resolve_project: Callable[[str], str],
    verify: Callable[[dict], list[str]],
) -> BenchmarkResult:
    """Submit/resume a suite, then verify terminal evidence and calculate Goal pass rate."""
    if not 1 <= repeats <= 10_000:
        raise ValueError("repeats must be 1-10000")
    if not 0 <= threshold <= 1:
        raise ValueError("minimum pass rate must be between 0 and 1")
    if timeout_seconds <= 0 or poll_seconds < 0:
        raise ValueError("timeout must be positive and poll interval cannot be negative")
    run_dir = Path(run_dir).expanduser()
    run_dir.mkdir(parents=True, exist_ok=True)
    run_dir.chmod(0o700)
    state = _load_or_create(run_dir, suite, grid=grid, model=model,
                            repeats=repeats, threshold=threshold)
    # Persist the run identity and every idempotency key *before* the first network request. If the
    # create response is lost after the relay commits, rerunning this directory recovers the same
    # Goals instead of generating a fresh key and duplicating autonomous work.
    _write(run_dir / "benchmark.json", state)
    by_id = {case["id"]: case for case in suite["cases"]}
    projects: dict[str, str] = {}

    # Submit every independent case before polling. The relay's normal queue then fans the suite
    # across all compatible workers; the benchmark client is not a second scheduler.
    for item in state["cases"]:
        if item.get("goal_id"):
            continue
        case = by_id[item["case_id"]]
        project = str(case.get("project") or suite["project"])
        try:
            if project not in projects:
                projects[project] = resolve_project(project)
            created = submit(case, projects[project], item["idempotency_key"])
            goal_id = created.get("id") if isinstance(created, dict) else None
            if not isinstance(goal_id, str) or not goal_id:
                raise RuntimeError("relay returned no Goal id")
            item.update(goal_id=goal_id, status=str(created.get("status") or "active"))
        except Exception as exc:
            item.update(status="failed", passed=False,
                        failures=[f"submission failed: {type(exc).__name__}: {exc}"])
        _write(run_dir / "benchmark.json", state)
    if not wait:
        return _summarize(state, run_dir)

    deadline = time.monotonic() + timeout_seconds
    while True:
        pending = [item for item in state["cases"] if item.get("status") not in TERMINAL]
        if not pending:
            break
        if time.monotonic() >= deadline:
            for item in pending:
                item["failures"] = ["benchmark wait timed out; rerun with the same --run-dir to resume"]
            _write(run_dir / "benchmark.json", state)
            return _summarize(state, run_dir)
        changed = False
        for item in pending:
            try:
                current = get_status(item["goal_id"])
                status = current.get("status") if isinstance(current, dict) else None
                if isinstance(status, str) and status:
                    changed |= item.get("status") != status
                    item["status"] = status
            except Exception as exc:
                item["last_poll_error"] = sanitize(f"{type(exc).__name__}: {exc}")
        if changed:
            _write(run_dir / "benchmark.json", state)
        if any(item.get("status") not in TERMINAL for item in state["cases"]):
            time.sleep(poll_seconds)

    for item in state["cases"]:
        if item.get("passed") is not None:
            continue
        if item.get("status") != "complete":
            item.update(passed=False, failures=[f"Goal ended {item.get('status')}"])
            continue
        try:
            evidence = get_evidence(item["goal_id"])
            failures = verify(evidence)
            item.update(passed=not failures, failures=sanitize(failures))
            _write(run_dir / "evidence" / f"{item['case_id']}-{item['repetition']}.json",
                   sanitize(evidence))
        except Exception as exc:
            item.update(passed=False,
                        failures=[sanitize(f"evidence failed: {type(exc).__name__}: {exc}")])
        _write(run_dir / "benchmark.json", state)
    return _summarize(state, run_dir)
