"""Learning from the work the grid is already doing.

The end state this exists for: a business runs its grid, and its models get better without anyone
opening a training tool. That requires two things this module provides — the work itself, and some
signal about whether the answer was any good.

Every served request is a candidate example. What it is *worth* depends on what happened next, and
there are exactly three signals that cost a human nothing:

  1. **What the person did with the answer.** Sent as-is, edited, or discarded. An edited answer is
     the most valuable row in the file: the human wrote the correct version for free.
  2. **What a stronger model said.** When the router sends a hard request to a frontier model, that
     answer is a training target for the local one. The mix shifts toward your own models because
     you used the grid.
  3. **A check that runs itself.** Tests passed, the schema validated, the row matched the ERP.

What this module refuses to do is treat a model's own unlabelled output as truth. Imitating your own
guesses is how a model drifts, so an unjudged row is stored but never trained on.

Storage is plain JSONL under `~/.grid/capture/`, one file per day, on the machine that served the
request — never uploaded, never aggregated anywhere. Off unless someone turns it on, redacting
obvious personal data on the way in, and pruned on a retention window.
"""
from __future__ import annotations

import dataclasses
import json
import queue
import random
import re
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from shared import paths

VERDICTS = ("accepted", "edited", "rejected")


def capture_dir() -> Path:
    return paths.grid_home() / "capture"


def policy_file() -> Path:
    return capture_dir() / "policy.json"


@dataclasses.dataclass(frozen=True)
class Policy:
    """What may be kept, and for how long. Absent file = everything off."""

    enabled: bool = False
    sample: float = 1.0          # fraction of requests recorded
    retain_days: int = 30
    redact: bool = True          # scrub emails/phones/cards on the way in
    teachers: tuple[str, ...] = ()   # model names whose answers count as targets (frontier nodes)

    def as_dict(self) -> dict:
        return {**dataclasses.asdict(self), "teachers": list(self.teachers)}


def load_policy() -> Policy:
    path = policy_file()
    if not path.is_file():
        return Policy()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Policy()
    return Policy(
        enabled=bool(raw.get("enabled", False)),
        sample=float(raw.get("sample", 1.0)),
        retain_days=int(raw.get("retain_days", 30)),
        redact=bool(raw.get("redact", True)),
        teachers=tuple(str(t) for t in raw.get("teachers", ())),
    )


def save_policy(policy: Policy) -> Path:
    path = policy_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(policy.as_dict(), indent=2) + "\n", encoding="utf-8")
    return path


# --- redaction ------------------------------------------------------------------------------
# Not a compliance tool and not claimed as one — a coarse net over the obvious identifiers, so a
# captured ticket doesn't sit on disk with a customer's card number in it. Order matters: cards
# before phones, or a 16-digit card partially matches as a phone number.
# Every quantifier is bounded. An unbounded `+` before the required "@" made this quadratic: on a
# long opaque token (a hash, a base64 blob) the engine retried the whole run at every offset —
# measured 5.4s for 40k characters, 4x per doubling, which an unauthenticated LAN request could
# aim at the serving proxy's event loop. RFC 5321 caps a local part at 64 and each label at 63, so
# the bounds cost nothing real.
_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"[\w.+-]{1,64}@[\w-]{1,63}\.[\w.]{2,24}"), "[email]"),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[card]"),
    (re.compile(r"(?<!\w)(?:\+?\d{1,3}[ .-]?)?(?:\(\d{2,4}\)[ .-]?)?\d{3,4}[ .-]\d{3,4}(?:[ .-]\d{3,4})?(?!\w)"), "[phone]"),
    (re.compile(r"\b(?:sk|pk|ghp|xox[bp])[-_][A-Za-z0-9_-]{12,64}\b"), "[secret]"),
)

# What one stored example may weigh. A 5 MB answer is not a training example, it is a file dump —
# and storing it means a gigabyte of JSONL that later gets read into memory whole.
MAX_TEXT = 8_000


def clip(text: str, limit: int = MAX_TEXT) -> str:
    """Bound the text before anything scans it. Order matters: redacting first and clipping after
    means the cap protects nothing, which is how a 100 KB feedback body became 49 seconds of work.
    """
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + " …[trimmed]"


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# --- writing --------------------------------------------------------------------------------

def _day_file(kind: str, when: date | None = None) -> Path:
    # Local calendar day on purpose: these files are read by the person whose machine served the
    # requests, and "yesterday" should mean their yesterday.
    day = (when or date.today()).isoformat()  # noqa: DTZ011
    return capture_dir() / f"{kind}-{day}.jsonl"


_WRITES: queue.Queue = queue.Queue(maxsize=256)
_WRITER: threading.Thread | None = None
_WRITER_LOCK = threading.Lock()


def _writer_loop() -> None:
    while True:
        path, row = _WRITES.get()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass                      # a full or read-only disk costs an example, nothing more
        finally:
            _WRITES.task_done()


def _append(path: Path, row: dict) -> None:
    """Hand the row to a single background writer.

    One thread, so appends stay ordered and never interleave a torn line; a bounded queue, so a
    stalled disk drops examples instead of growing memory; and no I/O on the caller's thread,
    because the caller is often a serving path holding a customer's request open.
    """
    global _WRITER
    with _WRITER_LOCK:
        if _WRITER is None or not _WRITER.is_alive():
            _WRITER = threading.Thread(target=_writer_loop, name="grid-capture-writer",
                                       daemon=True)
            _WRITER.start()
    try:
        _WRITES.put_nowait((path, row))
    except queue.Full:
        pass                          # busier than the disk can keep up with: drop this one


def flush(timeout: float = 5.0) -> None:
    """Wait for queued writes — for tests, and for anything that reads straight after writing."""
    deadline = time.time() + timeout
    while not _WRITES.empty() and time.time() < deadline:
        time.sleep(0.005)
    _WRITES.join()


def record(prompt: str, completion: str, *, model: str, policy: Policy | None = None,
           node: str = "", request_id: str | None = None) -> str | None:
    """Store one served request. Returns its id, or None if policy says don't.

    Best-effort by contract: the caller is a serving path, and nothing here may ever be the reason
    a customer's request fails. Callers swallow exceptions; this function also keeps its own work
    trivial (one append, no fsync, no locks).
    """
    policy = policy or load_policy()
    if not policy.enabled or not prompt.strip() or not completion.strip():
        return None
    # A real draw. The previous trick — `time.time_ns() % 1000` — is always 0 on macOS, whose
    # clock has microsecond resolution, so `--sample 0.1` silently kept everything.
    if policy.sample < 1.0 and random.random() >= policy.sample:
        return None
    request_id = request_id or uuid.uuid4().hex[:16]
    prompt, completion = clip(prompt), clip(completion)
    _append(_day_file("traffic"), {
        "id": request_id,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": model,
        "node": node,
        "prompt": redact(prompt) if policy.redact else prompt,
        "completion": redact(completion) if policy.redact else completion,
        # A teacher row is one a stronger model answered: its answer is a target, not a guess.
        "teacher": model in policy.teachers,
    })
    return request_id


def record_feedback(request_id: str, verdict: str, *, final_text: str = "",
                    policy: Policy | None = None) -> bool:
    """What the human did with the answer. This is the signal that makes automation honest."""
    if verdict not in VERDICTS:
        return False
    policy = policy or load_policy()
    final_text = clip(final_text)
    _append(_day_file("feedback"), {
        "id": request_id,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "verdict": verdict,
        "final_text": redact(final_text) if policy.redact else final_text,
    })
    return True


# --- reading --------------------------------------------------------------------------------

def _read_days(kind: str, days: int) -> list[dict]:
    # Reads see prior writes. Appends are queued on a writer thread, so anything that reads the
    # store — a summary, tonight's dataset — waits for what is already in flight rather than
    # quietly missing the last few minutes of work.
    flush()
    rows: list[dict] = []
    root = capture_dir()
    if not root.is_dir():
        return rows
    cutoff = date.today() - timedelta(days=days)  # noqa: DTZ011
    for path in sorted(root.glob(f"{kind}-*.jsonl")):
        stamp = path.stem.split("-", 1)[1]
        try:
            if date.fromisoformat(stamp) < cutoff:
                continue
        except ValueError:
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def prune(policy: Policy | None = None) -> int:
    """Delete day files past the retention window. Returns how many files went."""
    policy = policy or load_policy()
    root = capture_dir()
    if not root.is_dir():
        return 0
    cutoff = date.today() - timedelta(days=policy.retain_days)  # noqa: DTZ011
    removed = 0
    for path in list(root.glob("traffic-*.jsonl")) + list(root.glob("feedback-*.jsonl")):
        stamp = path.stem.split("-", 1)[1]
        try:
            if date.fromisoformat(stamp) < cutoff:
                path.unlink()
                removed += 1
        except (ValueError, OSError):
            continue
    return removed


@dataclasses.dataclass
class TrafficSummary:
    """What the capture store holds, in terms a person can act on."""

    requests: int = 0
    judged: int = 0
    accepted: int = 0
    edited: int = 0
    rejected: int = 0
    teacher_rows: int = 0
    trainable: int = 0
    models: dict = dataclasses.field(default_factory=dict)
    first_seen: str = ""
    last_seen: str = ""
    enabled: bool = False

    @property
    def headline(self) -> str:
        if not self.enabled:
            return "Not collecting anything yet."
        if not self.requests:
            return "Collecting — nothing has come through the grid yet."
        if not self.trainable:
            return (f"{self.requests} requests kept, but none can be learned from yet — "
                    "they need a verdict, or an answer from a stronger model.")
        return f"{self.trainable} examples ready to learn from, out of {self.requests} requests."

    @property
    def advice(self) -> str:
        if not self.enabled:
            return ("Turn it on and every answer the grid gives becomes a candidate example — "
                    "stored on this machine only.")
        if self.trainable >= 200:
            return "That is enough for a training run tonight."
        if self.trainable:
            return (f"A few hundred makes a real difference; you have {self.trainable}. "
                    "It grows on its own as your team works.")
        return ("Examples become usable when your app reports what the person did with the answer "
                "(sent it, edited it, or discarded it), or when a stronger model answers a hard "
                "request.")


def summarize(days: int = 30) -> TrafficSummary:
    policy = load_policy()
    traffic = _read_days("traffic", days)
    feedback = {row["id"]: row for row in _read_days("feedback", days) if row.get("id")}
    summary = TrafficSummary(requests=len(traffic), enabled=policy.enabled)
    for row in traffic:
        summary.models[row.get("model", "?")] = summary.models.get(row.get("model", "?"), 0) + 1
        if row.get("teacher"):
            summary.teacher_rows += 1
        verdict = (feedback.get(row.get("id"), {}) or {}).get("verdict")
        if verdict:
            summary.judged += 1
            setattr(summary, verdict, getattr(summary, verdict) + 1)
    stamps = sorted(row.get("ts", "") for row in traffic if row.get("ts"))
    if stamps:
        summary.first_seen, summary.last_seen = stamps[0], stamps[-1]
    summary.trainable = len(build_examples(days=days))
    return summary


# --- turning traffic into training data -----------------------------------------------------

@dataclasses.dataclass
class Example:
    """One row we are willing to learn from, and why it earned that."""

    prompt: str
    answer: str
    source: str      # "edited" | "accepted" | "teacher"
    weight: float    # 1.0 for a human-corrected answer; less for weaker signals


def build_examples(days: int = 30, *, include_accepted: bool = True,
                   models: tuple[str, ...] | list[str] | None = None) -> list[Example]:
    """Join traffic with feedback into rows worth imitating.

    `models` narrows the store to the traffic a particular model answered. The store is grid-wide
    — one file for every request the whole office made — so without this a business running a
    support model and a routing model would train each on the other's work, and the routing model
    would spend a night learning to write apologies.

    The ranking is the whole ethic of this module:

      * **edited** — the human rewrote the answer. Their version is ground truth; weight 1.0.
      * **teacher** — a stronger model answered. A good target, but not a person's judgement; 0.8.
      * **accepted** — sent as-is, so at least it was good enough; 0.6.
      * **rejected** — kept for the record, never imitated.
      * **unjudged** — the model's own guess with nobody's opinion attached. Never trained on:
        that is the path to a model that only agrees with itself.
    """
    traffic = _read_days("traffic", days)
    if models:
        wanted = {str(m) for m in models}
        traffic = [row for row in traffic if row.get("model") in wanted]
    feedback = {row["id"]: row for row in _read_days("feedback", days) if row.get("id")}
    examples: list[Example] = []
    for row in traffic:
        prompt = (row.get("prompt") or "").strip()
        completion = (row.get("completion") or "").strip()
        if not prompt:
            continue
        verdict_row = feedback.get(row.get("id")) or {}
        verdict = verdict_row.get("verdict")
        if verdict == "edited":
            corrected = (verdict_row.get("final_text") or "").strip()
            if corrected:
                examples.append(Example(prompt, corrected, "edited", 1.0))
            continue
        if verdict == "rejected":
            continue
        if row.get("teacher") and completion:
            examples.append(Example(prompt, completion, "teacher", 0.8))
            continue
        if verdict == "accepted" and include_accepted and completion:
            examples.append(Example(prompt, completion, "accepted", 0.6))
    return examples


def write_training_files(examples: list[Example], dest: Path, *, system_prompt: str = "") -> dict:
    """Write the same files the packs and the browser write, so every path downstream is identical.

    `prompts.jsonl` (the RL tasks), `refs.jsonl` (what the graders compare against — the human's
    version where there is one), and `sft.jsonl` (imitation examples).
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    dest.joinpath("prompts.jsonl").write_text(
        "".join(json.dumps({"prompt": e.prompt}) + "\n" for e in examples), encoding="utf-8"
    )
    dest.joinpath("refs.jsonl").write_text(
        "".join(json.dumps({"prompt": e.prompt, "reference": e.answer}) + "\n" for e in examples),
        encoding="utf-8",
    )
    dest.joinpath("sft.jsonl").write_text(
        "".join(
            json.dumps({"messages": (
                ([{"role": "system", "content": system_prompt}] if system_prompt else [])
                + [{"role": "user", "content": e.prompt},
                   {"role": "assistant", "content": e.answer}]
            )}) + "\n"
            for e in examples
        ),
        encoding="utf-8",
    )
    dest.joinpath("sources.json").write_text(
        json.dumps({
            "built_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "examples": len(examples),
            "by_source": {
                source: sum(1 for e in examples if e.source == source)
                for source in ("edited", "teacher", "accepted")
            },
        }, indent=2) + "\n",
        encoding="utf-8",
    )
    return {"examples": len(examples), "dir": str(dest)}
