"""Generic atomic read/write, shared by both modes.

Extracted from ``local/config.py`` so the shared kernel (e.g. ``shared/state.py``)
can persist JSON without importing ``local/``. ``local/config.py`` re-imports these
names, so ``config.load_json`` / ``config.atomic_write_json`` keep resolving for
existing callers.

``atomic_write_bytes`` is the single hardened write primitive both the JSON state
file and the remote TOML credential store go through, so secret-bearing files are
never briefly world-readable (see its docstring).
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Cannot read {path}: {exc}") from None
    if not isinstance(data, dict):
        raise SystemExit(f"Invalid JSON file: {path}")
    return data


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Atomically write ``data`` to ``path`` with ``mode`` perms — no looser-perm window.

    The temp file is created with ``mode`` from the start (``os.open`` + ``O_CREAT``) and
    ``fchmod``'d before any bytes land, so it never exists world-readable. The explicit
    ``fchmod`` also defeats umask, which masks ``O_CREAT``'s mode argument — important for
    the credential store, where a restrictive umask must not drop the owner bits either.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as fh:  # takes ownership of fd; closes it on exit
            if hasattr(os, "fchmod"):
                os.fchmod(fh.fileno(), mode)  # POSIX: defeat umask before any bytes land
            else:
                # Windows has no fchmod/umask; os.chmod only toggles the read-only bit,
                # but per-user ACLs already keep %USERPROFILE%\.grid private. Best-effort.
                with contextlib.suppress(OSError):
                    os.chmod(tmp, mode)
            fh.write(data)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)  # no-op (EBADF) if fdopen already owns/closed it; closes a leak otherwise
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    try:
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)  # don't leave an orphaned 0o600 temp behind on a failed rename
        raise


def atomic_write_json(path: Path, data: dict[str, Any], mode: int = 0o600) -> None:
    payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
    atomic_write_bytes(path, payload.encode("utf-8"), mode)
