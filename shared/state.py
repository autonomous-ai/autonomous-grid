"""Persisted CLI mode + per-mode active grid selection (the shared kernel).

State lives at ``~/.grid/state.json`` (``GRID_HOME`` overrides the base)::

    {"version": 1, "mode": "local", "active": {"local": <name|null>, "remote": <name|null>}}

A missing file means mode ``local`` with no active selection — so an existing local user
behaves exactly as before. This module is pure: it imports only ``shared.paths`` and
``shared.jsonio`` (never ``local``/``remote``), because mode is shared by both modes.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from shared import jsonio, paths


VALID_MODES = ("local", "remote")
DEFAULT_MODE = "local"
STATE_VERSION = 1
STATE_FILE = "state.json"


def state_path() -> Path:
    return paths.grid_home() / STATE_FILE


def read_state() -> dict[str, Any]:
    """Lenient read: missing/unreadable/malformed/non-dict ⇒ ``{}`` (treated as defaults).

    Mode is read on every command, so a corrupt state file must not brick the CLI; the
    next ``set_mode``/``set_active`` self-heals it.
    """
    path = state_path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def validate_mode(mode: str) -> str:
    if mode not in VALID_MODES:
        raise SystemExit(f"Unknown mode: {mode!r}. Choose one of: {', '.join(VALID_MODES)}.")
    return mode


def get_mode() -> str:
    mode = read_state().get("mode")
    return mode if mode in VALID_MODES else DEFAULT_MODE


def resolve_mode(override: str | None) -> str:
    """Effective mode for one invocation: ``--local``/``--remote`` override > persisted > default."""
    return validate_mode(override) if override else get_mode()


def get_active(mode: str) -> str | None:
    active = read_state().get("active")
    if not isinstance(active, dict):
        return None
    return active.get(mode) or None


def set_mode(mode: str) -> None:
    data = _normalized(read_state())
    data["mode"] = validate_mode(mode)
    jsonio.atomic_write_json(state_path(), data)


def set_active(mode: str, name: str | None) -> None:
    validate_mode(mode)
    data = _normalized(read_state())
    data["active"][mode] = name or None
    jsonio.atomic_write_json(state_path(), data)


def _normalized(data: dict[str, Any]) -> dict[str, Any]:
    """A well-formed state dict from possibly-empty/partial on-disk data."""
    active = data.get("active") if isinstance(data.get("active"), dict) else {}
    mode = data.get("mode")
    return {
        "version": STATE_VERSION,
        "mode": mode if mode in VALID_MODES else DEFAULT_MODE,
        "active": {m: (active.get(m) or None) for m in VALID_MODES},
    }
