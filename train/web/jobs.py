"""Running a training job from the browser, without the browser holding it up.

A run is an ordinary `grid train …` subprocess — the same command an engineer types. Consequences
that matter: closing the tab cannot kill a training run, the log is a plain file anyone can tail,
and a crash is visible as an exit code rather than a spinner that never stops.

That last one is the whole reason this module has a reaper. A page that says "learning" for eleven
hours after the trainer died is worse than an error: she waits all night for nothing and never
trusts the tool again. So the outcome is recorded when the child exits, and `status()` reports one
of four honest states — running, done, failed, or stopped — with a sentence for each.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

# What a nonzero exit usually means, in her words. The trainers already print a plain reason; this
# is the fallback when the log gives us nothing to quote.
_EXIT_HINTS = {
    1: "It stopped with an error. The details are in the log below.",
    2: "Something was wrong with the setup — usually a missing file.",
    137: "The computer ran out of memory. A smaller starting model will fit.",
    139: "It crashed. Trying a smaller starting model usually clears this.",
}


def _state_file(workspace_path: Path, job: str = "run") -> Path:
    return workspace_path / f"{job}-job.json"


def _run_dir(workspace_path: Path) -> Path:
    return workspace_path / "run"


def start(workspace_path: Path, config_path: Path, *, verb: str = "run",
          extra: list[str] | None = None, expected_steps: int = 0, job: str = "run") -> dict:
    """Launch the run detached from this request. Returns the job record.

    `verb` is the training stage: "sft" imitates the answers a team already wrote and needs nothing
    but this machine; "run" is the feedback loop and needs an engine that can serve rollouts. The
    page decides which is possible before offering it (train/web/machines.py).
    """
    existing = status(workspace_path, job=job)
    if existing.get("running"):
        return existing              # one of each kind at a time: pressing twice is harmless
    _run_dir(workspace_path).mkdir(parents=True, exist_ok=True)
    log_path = workspace_path / f"{job}.log"
    # Truncate: one workspace, one current run — history lives in the run dir's log.jsonl.
    handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "cli", "train", verb, "--config", str(config_path),
         *(extra or [])],
        stdout=handle,
        stderr=subprocess.STDOUT,
        cwd=str(workspace_path),
        start_new_session=True,  # survives the server; Ctrl-C on the server won't kill training
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    record = {"pid": process.pid, "started": time.time(), "config": str(config_path),
              "verb": verb, "expected_steps": expected_steps}
    _write(workspace_path, record, job)
    _reap_later(workspace_path, process, handle, job)
    return status(workspace_path, job=job)


def _write(workspace_path: Path, record: dict, job: str = "run") -> None:
    _state_file(workspace_path, job).write_text(json.dumps(record, indent=2) + "\n",
                                                encoding="utf-8")


def _reap_later(workspace_path: Path, process: subprocess.Popen, handle, job: str = "run") -> None:
    """Wait on the child in the background and record how it ended.

    Without this the exit code is lost — the request that started the run is long gone — and the
    page can only guess. A daemon thread costs nothing and buys an honest answer.
    """
    def wait() -> None:
        code = process.wait()
        try:
            handle.close()
        except OSError:
            pass
        try:
            record = json.loads(_state_file(workspace_path, job).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if record.get("pid") != process.pid:
            return                       # a newer run owns the file now
        record.update({"exit": code, "ended": time.time()})
        try:
            _write(workspace_path, record, job)
        except OSError:
            pass

    threading.Thread(target=wait, name="grid-train-reaper", daemon=True).start()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


# Phases worth naming, because a first run spends its first minutes downloading and silence there
# reads as "stuck". Matched against the tail of the trainer's own output.
_PHASES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"(\d+(?:\.\d+)?)([MG])B?/(\d+(?:\.\d+)?)([MG])B", re.IGNORECASE),
     "Getting the starting model — {done}{du} of {total}{tu}. This happens once."),
    (re.compile(r"Loading|loading pretrained|resolving|Fetching", re.IGNORECASE),
     "Getting the starting model ready. This happens once."),
    (re.compile(r"nodes synced", re.IGNORECASE), "Learning, and keeping the other machines up to date."),
    (re.compile(r"Iter \d+|'loss'|reward", re.IGNORECASE), "Learning from your examples."),
)


def describe_phase(log_tail: str) -> str:
    for pattern, template in _PHASES:
        match = pattern.search(log_tail)
        if match:
            if "{done}" in template and match.lastindex == 4:
                return template.format(done=match.group(1), du=match.group(2),
                                       total=match.group(3), tu=match.group(4))
            return template.split("{")[0] if "{" in template else template
    return "Getting started."


def estimate(steps_done: int, expected: int, elapsed_seconds: float) -> str:
    """"about 35 minutes left" — or nothing, when we genuinely cannot say yet."""
    if steps_done < 3 or expected <= 0 or steps_done >= expected or elapsed_seconds < 20:
        return ""
    per_step = elapsed_seconds / steps_done
    remaining = per_step * (expected - steps_done)
    if remaining < 90:
        return "less than two minutes left"
    minutes = int(remaining // 60)
    if minutes < 60:
        return f"about {minutes} minutes left"
    hours = remaining / 3600
    return f"about {hours:.1f} hours left"


def status(workspace_path: Path, job: str = "run") -> dict:
    """What the page needs: an honest state, how far along, the curve, and any error."""
    state_path = _state_file(workspace_path, job)
    if not state_path.is_file():
        return {"exists": False, "running": False, "state": "none"}
    try:
        record = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"exists": False, "running": False, "state": "none"}

    running = "exit" not in record and _alive(int(record.get("pid", -1)))
    points = []
    log_jsonl = _run_dir(workspace_path) / "log.jsonl"
    if log_jsonl.is_file():
        for line in log_jsonl.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                points.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    log_path = workspace_path / f"{job}.log"
    tail = ""
    if log_path.is_file():
        tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-14:])
    adapter = _run_dir(workspace_path) / "adapter"
    has_adapter = adapter.is_dir() and any(adapter.iterdir())
    exit_code = record.get("exit")
    elapsed = (record.get("ended") or time.time()) - record["started"]
    steps_done = max((p.get("step", 0) for p in points), default=0)
    expected = int(record.get("expected_steps") or 0)

    if running:
        state, reason = "running", ""
    elif record.get("stopped_by_user"):
        # A deliberate Stop is not a crash. SIGTERM surfaces as exit -15, which is truthy, so
        # without this the page told her the run "stopped unexpectedly (code -15)".
        state = "stopped"
        reason = "You stopped it. Nothing was served, and your examples and checks are unchanged."
    elif exit_code not in (0, None):
        # Trust the exit code over the artifacts: a crash can leave a half-written adapter behind,
        # and calling that "finished" would send her to a gate that scores a broken model.
        state = "failed"
        reason = _reason_from(tail) or _EXIT_HINTS.get(
            exit_code, f"It stopped unexpectedly (code {exit_code}).")
    elif has_adapter:
        state, reason = "done", ""
    elif exit_code == 0:
        state = "failed"
        reason = (_reason_from(tail)
                  or "It finished without producing a model. The log below says why.")
    else:
        state = "stopped"
        reason = ("It stopped before finishing — the computer may have gone to sleep, or the "
                  "process was ended.")

    return {
        "exists": True,
        "state": state,
        "running": running,
        "reason": reason,
        "verb": record.get("verb", "run"),
        "pid": record.get("pid"),
        "started": record["started"],
        "minutes": round(elapsed / 60, 1),
        "steps_done": steps_done,
        "expected_steps": expected,
        "eta": estimate(steps_done, expected, elapsed) if running else "",
        "phase": describe_phase(tail) if running else "",
        "points": points,
        "log_tail": tail,
        "finished_ok": state == "done",
        "adapter": str(adapter) if has_adapter else "",
    }


def _reason_from(log_tail: str) -> str:
    """Quote the trainer's own plain-language complaint when it left one.

    Our CLI errors are already written for a person ("grid train: only 8 usable tickets…"), so
    showing that beats inventing a sentence from an exit code.
    """
    for line in reversed(log_tail.splitlines()):
        text = line.strip()
        if text.startswith("grid train:"):
            return text.removeprefix("grid train:").strip().capitalize()
    return ""


def stop(workspace_path: Path, job: str = "run") -> dict:
    state_path = _state_file(workspace_path, job)
    if state_path.is_file():
        try:
            record = json.loads(state_path.read_text(encoding="utf-8"))
            pid = int(record.get("pid", -1))
        except (OSError, json.JSONDecodeError, ValueError):
            return status(workspace_path, job=job)
        # Recorded before the signal, so the page can tell "you stopped it" from "it died".
        record["stopped_by_user"] = True
        try:
            _write(workspace_path, record, job)
        except OSError:
            pass
        if pid > 0 and _alive(pid):
            # The whole process group: the trainer may have spawned dataloader workers.
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
    return status(workspace_path, job=job)
