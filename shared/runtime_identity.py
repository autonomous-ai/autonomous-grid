"""Bounded provenance for the Grid process that executes a native Goal.

Package versions identify releases, but two unmerged PR revisions can deliberately carry the same
version.  Physical acceptance therefore needs the checked-out revision as well.  This is audit
metadata, not an authorization claim: the relay authenticates the node and snapshots the value on
attempt start; an offline verifier may then require the exact revision deployed for a test.

Wheel/container builds can set ``GRID_BUILD_REVISION``.  A source checkout falls back to its Git
HEAD and reports whether tracked Grid runtime files differ from that commit.  Failure to discover a
revision is represented by omission, never by a plausible-looking placeholder.
"""

from __future__ import annotations

import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from shared._version import __version__

_REVISION = re.compile(r"[0-9a-fA-F]{7,64}")
_BUILD_REVISION_ENV = "GRID_BUILD_REVISION"
_GIT_TIMEOUT_SECONDS = 3.0
_TRACKED_RUNTIME_PATHS = ("cli", "remote", "shared", "pyproject.toml")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_GIT_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


@lru_cache(maxsize=1)
def grid_runtime_identity() -> dict[str, Any]:
    """Return the immutable process-start Grid version/revision identity.

    Cached because a provider heartbeat repeats it every thirty seconds.  The cache also gives the
    field its intended meaning: which checkout launched this process, not whichever branch happens
    to be on disk after a long-lived worker was started.
    """
    identity: dict[str, Any] = {"version": __version__}
    configured = (os.getenv(_BUILD_REVISION_ENV) or "").strip()
    if configured:
        if _REVISION.fullmatch(configured):
            identity.update({"revision": configured.lower(), "dirty": False})
        return identity

    root = Path(__file__).resolve().parents[1]
    head = _git(root, "rev-parse", "--verify", "HEAD")
    revision = (head.stdout.strip() if head is not None and head.returncode == 0 else "")
    if not _REVISION.fullmatch(revision):
        return identity

    # Tracked changes only. Untracked customer/project files do not alter the installed Grid
    # runtime, while staged and unstaged changes to any shipped runtime path do.
    status = _git(
        root, "status", "--porcelain", "--untracked-files=no", "--", *_TRACKED_RUNTIME_PATHS)
    identity["revision"] = revision.lower()
    if status is not None and status.returncode == 0:
        identity["dirty"] = bool(status.stdout.strip())
    return identity

