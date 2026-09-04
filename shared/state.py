"""Persisted CLI mode + per-mode active grid selection (the shared kernel).

State lives at ``~/.grid/state.json`` (``GRID_HOME`` overrides the base)::

    {"version": 1, "mode": "remote", "active": {"local": <name|null>, "remote": <name|null>}}

A missing file means no active selection and the *derived* default mode (``_default_mode``):
``remote`` for a new install, ``local`` for a machine that already holds local grids — so an
existing local user who never ran ``grid mode`` still behaves exactly as before (ADR 0001 D-2,
amended). This module is pure: it imports only ``shared.paths`` and ``shared.jsonio`` (never
``local``/``remote``), because mode is shared by both modes.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from shared import jsonio, paths


VALID_MODES = ("local", "remote")
DEFAULT_MODE = "remote"
LEGACY_DEFAULT_MODE = "local"
STATE_VERSION = 1
STATE_FILE = "state.json"

# Hand-duplicated from ``local.config.CONFIG_FILE``. It cannot be imported: ``local.config``
# imports *this* module, so the dependency runs one way only and this module stays pure. Kept in
# lockstep by editing both sides — renamed there with no edit here, ``_has_local_grids`` sees an
# empty directory and every existing local user is silently moved onto the remote default.
GRID_CONFIG_FILE = "config.json"


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


def _has_local_grids() -> bool:
    """Whether this machine already holds at least one local grid's config on disk.

    The evidence ``_default_mode`` reads. Unreadable ⇒ ``True``: a new install has no ``grids``
    directory at all and ``glob`` yields nothing *without* raising, so an ``OSError`` means the
    directory is there and only unreadable — and "cannot tell" must answer the way that leaves an
    existing local user where they were, never the way that takes their grids out of sight.
    """
    try:
        return any(paths.grids_dir().glob(f"*/{GRID_CONFIG_FILE}"))
    except OSError:
        return True


def _default_mode() -> str:
    """The mode for a machine that has never persisted a choice (ADR 0001 D-2, amended).

    ``remote`` is the default the product wants for a new install. A machine that already has
    local grids keeps ``local`` until its owner opts in with ``grid mode remote`` — ADR 0001's
    invariant that an existing local user with no state file behaves exactly as before.
    """
    return LEGACY_DEFAULT_MODE if _has_local_grids() else DEFAULT_MODE


def get_mode() -> str:
    mode = read_state().get("mode")
    return mode if mode in VALID_MODES else _default_mode()


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
        "mode": mode if mode in VALID_MODES else _default_mode(),
        "active": {m: (active.get(m) or None) for m in VALID_MODES},
    }
