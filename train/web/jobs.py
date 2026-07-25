"""Running a training job from the browser, without the browser holding it up.

A run is an ordinary `grid train run --config …` subprocess — the same command an engineer types.
Consequences that matter: closing the tab cannot kill a training run, the log is a plain file
anyone can tail, and a crash is visible as an exit code rather than a spinner that never stops.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def _state_file(workspace_path: Path) -> Path:
    return workspace_path / "job.json"


def _run_dir(workspace_path: Path) -> Path:
    return workspace_path / "run"


def start(workspace_path: Path, config_path: Path, *, verb: str = "run",
          extra: list[str] | None = None) -> dict:
    """Launch the run detached from this request. Returns the job record.

    `verb` is the training stage: "sft" imitates the answers a team already wrote and needs nothing
    but this machine; "run" is the feedback loop and needs an engine that can serve rollouts. The
    page decides which is possible before offering it (train/web/machines.py).
    """
    existing = status(workspace_path)
    if existing.get("running"):
        return existing
    _run_dir(workspace_path).mkdir(parents=True, exist_ok=True)
    log_path = workspace_path / "run.log"
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
              "verb": verb}
    _state_file(workspace_path).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return status(workspace_path)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def status(workspace_path: Path) -> dict:
    """What the page needs: is it running, how far along, the curve, and any error."""
    state_path = _state_file(workspace_path)
    if not state_path.is_file():
        return {"exists": False, "running": False}
    record = json.loads(state_path.read_text(encoding="utf-8"))
    running = _alive(int(record["pid"]))
    points = []
    log_jsonl = _run_dir(workspace_path) / "log.jsonl"
    if log_jsonl.is_file():
        for line in log_jsonl.read_text(encoding="utf-8").splitlines():
            try:
                points.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    tail = ""
    log_path = workspace_path / "run.log"
    if log_path.is_file():
        tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-14:])
    adapter = _run_dir(workspace_path) / "adapter"
    return {
        "exists": True,
        "running": running,
        "verb": record.get("verb", "run"),
        "pid": record["pid"],
        "started": record["started"],
        "minutes": round((time.time() - record["started"]) / 60, 1),
        "steps_done": max((p.get("step", 0) for p in points), default=0),
        "points": points,
        "log_tail": tail,
        "finished_ok": (not running) and adapter.is_dir(),
        "adapter": str(adapter) if adapter.is_dir() else "",
    }


def stop(workspace_path: Path) -> dict:
    state_path = _state_file(workspace_path)
    if state_path.is_file():
        pid = int(json.loads(state_path.read_text(encoding="utf-8"))["pid"])
        if _alive(pid):
            # The whole process group: the trainer may have spawned dataloader workers.
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
    return status(workspace_path)
