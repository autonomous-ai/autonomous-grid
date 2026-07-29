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

from shared import paths

try:  # POSIX (macOS/Linux — the primary CLI target)
    import fcntl

    _HAVE_FCNTL = True
except ModuleNotFoundError:  # Windows (consumer/playground scope) — msvcrt fallback
    import msvcrt

    _HAVE_FCNTL = False


def _open_lock_fd(path: Path) -> int:
    """The fd the lock is taken on: a sibling ``<path>.lock``, so it never collides with the
    atomic-rename target. Parent directories are created as needed — through ``paths.ensure_dir``,
    because this can be the first thing to build a grid's run directory (a `grid leave` or a
    ``mutate_record`` reaching a grid no record has been written for yet), and that directory's write
    bits are what decide who may delete the records in it (`.scratch/grid-leave/` issue 19)."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    paths.ensure_dir(lock_path.parent)
    return os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)


def _release(fd: int) -> None:
    if _HAVE_FCNTL:
        fcntl.flock(fd, fcntl.LOCK_UN)
    else:
        os.lseek(fd, 0, os.SEEK_SET)
        with contextlib.suppress(OSError):
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


def _seed_windows_byte(fd: int) -> None:
    """``msvcrt.locking`` locks from the current file offset and needs a byte to lock, so seed a
    sentinel byte and rewind before locking a 1-byte region at offset 0."""
    if os.fstat(fd).st_size == 0:
        os.write(fd, b"\0")
    os.lseek(fd, 0, os.SEEK_SET)


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
    fd = _open_lock_fd(path)
    try:
        if _HAVE_FCNTL:
            fcntl.flock(fd, fcntl.LOCK_EX)  # blocks until no other holder
        else:
            _seed_windows_byte(fd)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)  # blocks (retries) until no other holder
        yield
    finally:
        _release(fd)
        os.close(fd)


@contextlib.contextmanager
def try_file_lock(path: Path) -> Iterator[bool]:
    """Non-blocking sibling of ``file_lock``: yields whether the lock was acquired.

    For a caller that must never *wait* for the record's critical section — an engine child unlinking
    its own run record from its exit path, where the contending holder is a `grid join`/`leave` that
    is already authoritative over that record. Blocking there would be worse than skipping: leave
    holds this lock across its whole teardown, so the dying child would stall until leave's stop grace
    expired and SIGKILLed it (`shared.run_records.discard_own_record`).

    Yields ``False`` without waiting when another holder has it — the body must then do nothing. The
    lock is released on exit only when it was actually taken.

    Only *contention* yields ``False``. A lock that is genuinely broken here (``ENOLCK``, a
    filesystem that can't ``flock``) propagates, so the caller reports a real fault instead of
    silently skipping its work under a "someone else has it" reading.
    """
    fd = _open_lock_fd(path)
    acquired = False
    try:
        try:
            if _HAVE_FCNTL:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                _seed_windows_byte(fd)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # raises OSError instead of retrying
            acquired = True
        except BlockingIOError:  # POSIX EAGAIN/EWOULDBLOCK — another holder, the expected case
            acquired = False
        except OSError:
            if _HAVE_FCNTL:
                raise  # not contention: a real lock failure the caller must hear about
            acquired = False  # Windows reports LK_NBLCK contention as a plain OSError (EACCES)
        yield acquired
    finally:
        if acquired:
            _release(fd)
        os.close(fd)
