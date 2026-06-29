"""Detached-engine run records — the on-disk handle that ties a `grid join` to its
detached subprocess, shared by both modes.

`grid join` writes one record per engine under
``~/.grid/run/engines/<grid_id>/<engine_id>.json`` (`shared.paths.engines_dir`) and spawns a
detached child; `grid leave` reads it back to SIGTERM that child and remove the file. The record
holds only **non-secret routing** — never a token (cloud tokens live in ``credentials.toml``,
``0o600``).

Extracted from ``cli/provider.py`` so the cloud serve loop (`cloud/serve.py`) and the cloud
join/leave handlers (`cli/cloud_provider.py`) reuse the exact same record format and teardown
without a ``cloud → cli`` back-dependency (DECISIONS D17). Writes go through ``shared.jsonio`` —
the same atomic, ``0o600`` writer ``lan/config`` re-exports — so LAN behaviour is byte-identical.
"""
from __future__ import annotations

import os
import signal
import time
from pathlib import Path
from typing import Any

from shared import jsonio, paths


# How long ``stop_engine`` waits for a SIGTERM'd child to exit before SIGKILLing its group.
_STOP_GRACE_SECONDS = 8


def record_path(grid_id: str, engine_id: str) -> Path:
    return paths.engines_dir(grid_id) / f"{engine_id}.json"


def write_record(grid_id: str, engine_id: str, record: dict[str, Any]) -> None:
    jsonio.atomic_write_json(record_path(grid_id, engine_id), record)


def read_records(grid_id: str) -> dict[str, dict[str, Any]]:
    root = paths.engines_dir(grid_id)
    if not root.exists():
        return {}
    records: dict[str, dict[str, Any]] = {}
    for path in sorted(root.glob("*.json")):
        data = jsonio.load_json(path)
        if data.get("engine_id"):
            records[data["engine_id"]] = data
    return records


def read_record(grid_id: str, engine_id: str) -> dict[str, Any] | None:
    """One engine's record, or ``None`` — the detached child's lookup of its own routing."""
    return read_records(grid_id).get(engine_id)


def pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill_group(pid: int) -> None:
    try:
        if hasattr(os, "killpg"):
            os.killpg(pid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def stop_engine(grid_id: str, engine_id: str, record: dict[str, Any]) -> None:
    """SIGTERM the detached engine child so it unregisters + tears down, then drop its record.

    Escalates to SIGKILL of the process group if it does not exit within the grace window. The
    record is removed either way, so a leave never leaves a stale handle behind.
    """
    pid = int(record.get("pid") or 0)
    if pid and pid_alive(pid):
        # SIGTERM the detached engine so it unregisters and stops anything it started.
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.time() + _STOP_GRACE_SECONDS
        while time.time() < deadline and pid_alive(pid):
            time.sleep(0.2)
        if pid_alive(pid):
            kill_group(pid)
    record_path(grid_id, engine_id).unlink(missing_ok=True)
