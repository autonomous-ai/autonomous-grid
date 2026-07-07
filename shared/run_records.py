"""Detached-engine run records — the on-disk handle that ties a `grid join` to its
detached subprocess, shared by both modes.

`grid join` writes one record per engine under
``~/.grid/run/engines/<grid_id>/<engine_id>.json`` (`shared.paths.engines_dir`) and spawns a
detached child; `grid leave` reads it back to SIGTERM that child and remove the file. The record
holds only **non-secret routing** — never a token (remote tokens live in ``credentials.toml``,
``0o600``).

Extracted from ``cli/provider.py`` so the remote serve loop (`remote/serve.py`) and the remote
join/leave handlers (`cli/remote_provider.py`) reuse the exact same record format and teardown
without an ``remote → cli`` back-dependency (DECISIONS D17). Writes go through ``shared.jsonio`` —
the same atomic, ``0o600`` writer ``local/config`` re-exports — so local behaviour is byte-identical.
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


def update_record(grid_id: str, engine_id: str, **fields: Any) -> None:
    """Merge fields into an existing engine record; no-op if the record is already gone."""
    record = read_records(grid_id).get(engine_id)
    if record is None:
        return
    record.update(fields)
    write_record(grid_id, engine_id, record)


def match_engine(
    specs: list[dict[str, Any]],
    selector: str,
    *,
    label: str,
    summary: str,
    hint: str = "pass the exact endpoint URL instead",
) -> list[dict[str, Any]]:
    """Engine spec(s) a `grid leave --engine <selector>` picks out of ``specs``, tried in order: exact
    ``endpoint_url`` → exact ``engine_label`` → a served model → an ``endpoint_url`` substring. Each match
    must resolve to exactly ONE engine or it raises ``SystemExit`` (naming ``summary`` and ``hint``);
    returns ``[]`` on no match so the caller raises its own not-found. Returned dicts are the SAME objects
    passed in — identity is preserved for an ``id()``-based drop filter. An exact engine-*id* match is the
    caller's job BEFORE this (remote keys engines by URL/label; local by record id). ``hint`` is the
    disambiguation instruction, per mode: remote points at the endpoint URL, local at the engine id."""
    if not selector:  # defensive: an empty selector is a substring of every URL — never "match all"
        return []

    def unique(matches: list[dict[str, Any]], how: str) -> list[dict[str, Any]]:
        if len(matches) > 1:
            raise SystemExit(
                f"{how} {selector!r} matches several engines on {label}; {hint}. Engines: {summary}."
            )
        return matches

    by_url = unique([s for s in specs if s.get("endpoint_url") == selector], "URL")
    if by_url:
        return by_url
    by_label = unique([s for s in specs if s.get("engine_label") == selector], "Label")
    if by_label:
        return by_label
    by_model = unique([s for s in specs if selector in (s.get("models") or [])], "Model")
    if by_model:
        return by_model
    return unique([s for s in specs if selector in (s.get("endpoint_url") or "")], "URL fragment")


def media_signature(record: dict[str, Any]) -> tuple[bool, tuple[str, ...], int, int]:
    """A comparable fingerprint of an identity's media config (on/off, bundles, ports). A SIGHUP
    hot-reload can't bring media up/down or swap bundles, so ``grid join``/``leave`` (CLI) and the serve
    loop's reload both compare this to choose hot-reload vs respawn — ONE definition so the two decisions
    can never desync (ADR 0010 C3)."""
    return (
        bool(record.get("media")),
        tuple(sorted(record.get("media_bundles") or [])),
        int(record.get("comfyui_port") or 8188),
        int(record.get("media_port") or 8190),
    )


def pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False  # ESRCH: no such process
    except PermissionError:
        return True  # EPERM: the process exists, it's just owned by another uid — reporting it dead would
        # let a join spawn a second engine under the same token node_id and clobber it.
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


def terminate_pid(pid: int) -> bool:
    """SIGTERM a detached engine child and wait for it to exit, escalating to SIGKILL of its process
    group after the grace window. Does **not** touch any run record — the caller decides whether to
    remove it (a `grid leave` teardown) or keep it (a respawn that rewrites the record in place, so the
    engine child unregisters + stops what it launched, but the merged record survives). A ``0``/dead pid
    is a no-op.

    Returns whether the process is confirmed gone. ``False`` means it survived even SIGKILL — the caller
    must NOT spawn a replacement, because two live children on one token-pinned relay node_id clobber
    each other (the exact bug this whole flow exists to prevent).
    """
    if not (pid and pid_alive(pid)):
        return True
    # SIGTERM the detached engine so it unregisters and stops anything it started.
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.time() + _STOP_GRACE_SECONDS
    while time.time() < deadline and pid_alive(pid):
        time.sleep(0.2)
    if pid_alive(pid):
        kill_group(pid)
    return not pid_alive(pid)


def stop_engine(grid_id: str, engine_id: str, record: dict[str, Any]) -> None:
    """SIGTERM the detached engine child so it unregisters + tears down, then drop its record.

    Escalates to SIGKILL of the process group if it does not exit within the grace window. The
    record is removed either way, so a leave never leaves a stale handle behind.
    """
    terminate_pid(int(record.get("pid") or 0))
    record_path(grid_id, engine_id).unlink(missing_ok=True)
