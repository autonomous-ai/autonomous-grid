"""A tiny cross-process advisory file lock (POSIX ``fcntl.flock``).

Run records are written atomically (``shared.jsonio`` — tmp file + ``os.replace``), but a
*read-merge-write* of the singleton remote record (read the current union, add an engine, write it
back) spans two syscalls, so two concurrent ``grid join`` processes could lost-update the union
(``cli.remote_provider``, ADR 0010). ``file_lock`` serializes that critical section with an exclusive
lock on a sibling ``<name>.lock`` file.

POSIX-only, which matches the macOS/Linux CLI target (there is no Windows path). ``flock`` locks are
tied to the open file description and released on ``close``/process exit, so a crashed holder never
strands the lock. The lock file itself is created ``0o600`` and intentionally left on disk (an empty
sentinel — creating/removing it per acquire would itself race).
"""
from __future__ import annotations

import contextlib
import fcntl
import os
from collections.abc import Iterator
from pathlib import Path


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock for the body of the ``with`` block.

    ``path`` is the file whose read-modify-write is being guarded; the lock is taken on a sibling
    ``<path>.lock`` so it never collides with the atomic-rename target. Parent directories are created
    as needed. Blocks until the lock is acquired, and always releases (and closes the fd) on exit.
    """
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # blocks until no other holder
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
