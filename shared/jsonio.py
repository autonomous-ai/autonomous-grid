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
import getpass
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


class AtomicWriteCommittedError(OSError):
    """The target was atomically replaced, but its directory durability barrier failed.

    Callers that keep an in-memory transaction must not roll that transaction back: the new file
    is already the visible target.  The exception still propagates because a crash may lose the
    directory entry when the filesystem could not confirm the final fsync.
    """

    def __init__(self, path: Path, cause: OSError) -> None:
        super().__init__(
            cause.errno,
            f"atomic write to {path} committed, but directory fsync failed: {cause}",
            os.fspath(path),
        )
        self.path = path


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

    The temp file is created exclusively with ``mkstemp``'s owner-only mode and ``fchmod``'d to the
    requested mode before any bytes land, so it never exists world-readable. The explicit
    ``fchmod`` also defeats umask — important for the credential store, where a restrictive umask
    must not drop the owner bits either.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # A predictable ``<target>.tmp`` can be pre-created as a symlink in a shared provisioning
    # directory, turning a secret write into disclosure or arbitrary truncation. mkstemp uses
    # O_EXCL and gives every attempt a same-directory name so the final replace stays atomic.
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:  # takes ownership of fd; closes it on exit
            if sys.platform == "win32":
                # Windows temp files inherit their directory DACL. Establish the owner-only ACL
                # while the file is still empty, before a bearer token or other secret is written.
                _restrict_windows_owner_only(tmp)
            elif hasattr(os, "fchmod"):
                os.fchmod(fh.fileno(), mode)  # POSIX: defeat umask before any bytes land
            else:
                # Windows has no fchmod/umask; os.chmod only toggles the read-only bit,
                # but per-user ACLs already keep %USERPROFILE%\.grid private. Best-effort.
                with contextlib.suppress(OSError):
                    os.chmod(tmp, mode)
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)  # no-op (EBADF) if fdopen already owns/closed it; closes a leak otherwise
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    try:
        os.replace(tmp, path)
        # Persist the renamed directory entry before a process side effect relies on this state.
        # Windows has no portable directory-fsync equivalent; the file _commit above still protects
        # its contents there.
        if os.name != "nt":
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError as exc:
                # os.replace above is the linearization point: the new target is already visible.
                # Preserve that fact so transactional callers do not restore stale in-memory state.
                raise AtomicWriteCommittedError(path, exc) from exc
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)  # don't leave an orphaned 0o600 temp behind on a failed rename
        raise


def atomic_write_json(path: Path, data: dict[str, Any], mode: int = 0o600) -> None:
    payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
    atomic_write_bytes(path, payload.encode("utf-8"), mode)


def restrict_owner_only(path: Path) -> None:
    """Enforce an owner-only ACL for a secret already written atomically.

    POSIX mode bits are enough on Unix. Windows' ``chmod`` only toggles read-only, so use the
    built-in ACL utility and fail closed if it cannot prove the restriction.
    """

    if sys.platform != "win32":
        os.chmod(path, 0o600)
        return
    _restrict_windows_owner_only(path)


def _restrict_windows_owner_only(path: Path) -> None:
    account = os.environ.get("USERDOMAIN", "").strip()
    username = os.environ.get("USERNAME", "").strip() or getpass.getuser()
    principal = f"{account}\\{username}" if account else username
    try:
        result = subprocess.run(
            [
                "icacls.exe",
                os.fspath(path),
                "/inheritance:r",
                "/grant:r",
                f"{principal}:(F)",
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError(f"could not restrict Windows ACL on {path}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise OSError(f"could not restrict Windows ACL on {path}: {detail or 'icacls failed'}")
