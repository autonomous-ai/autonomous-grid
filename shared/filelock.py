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
import os
from collections.abc import Iterator
from pathlib import Path

try:  # POSIX (macOS/Linux — the primary CLI target)
    import fcntl

    _HAVE_FCNTL = True
except ModuleNotFoundError:  # Windows (consumer/playground scope) — msvcrt fallback
    import msvcrt

    _HAVE_FCNTL = False


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock for the body of the ``with`` block.

    ``path`` is the file whose read-modify-write is being guarded; the lock is taken on a sibling
    ``<path>.lock`` so it never collides with the atomic-rename target. Parent directories are created
    as needed. Blocks until the lock is acquired, and always releases (and closes the fd) on exit.

    POSIX uses ``fcntl.flock``; on Windows we fall back to ``msvcrt.locking`` on a one-byte region.
    Both are advisory locks tied to the open fd and released on ``close``/process exit, so a crashed
    holder never strands the lock.
    """
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if _HAVE_FCNTL:
            fcntl.flock(fd, fcntl.LOCK_EX)  # blocks until no other holder
        else:
            # msvcrt.locking locks from the current file offset and needs a byte to lock,
            # so seed a sentinel byte and lock a 1-byte region at offset 0.
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)  # blocks (retries) until no other holder
        yield
    finally:
        if _HAVE_FCNTL:
            fcntl.flock(fd, fcntl.LOCK_UN)
        else:
            os.lseek(fd, 0, os.SEEK_SET)
            with contextlib.suppress(OSError):
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)
