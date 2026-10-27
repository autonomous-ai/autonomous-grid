"""Stop and reload requests for a detached engine child, where POSIX would send a signal.

On POSIX `grid leave` SIGTERMs the engine child (it unregisters, stops what it launched and exits) and
a remote re-join SIGHUPs the serve child (it hot-reloads the merged record). Windows has neither: a
console-less child receives no SIGTERM — `run_records.terminate_pid` can only ``taskkill /F /T``, so the
engine never unregistered — and ``signal.SIGHUP`` does not exist, so the reload raised
``AttributeError``.

So on Windows the request is a file beside the run record (`run_records.control_path`), the sidecar
shape the heartbeat already uses. The child's `Watch` deletes the file — that deletion is the
acknowledgement `acknowledged` waits for — and calls the handler registered for it: the same
``KeyboardInterrupt`` its SIGTERM handler raises, the same ``reload_requested`` its SIGHUP handler sets.

Addressed by (grid, engine), never by pid: on Windows the recorded pid can be uv's ``grid.exe`` launcher
rather than the Python child that reads the request.

POSIX keeps its signals; ``Watch.start`` there returns None and nothing polls.
"""
from __future__ import annotations

import signal
import sys
import threading
import time
from collections.abc import Callable

from shared import run_records

_IS_WINDOWS = sys.platform == "win32"


def request(grid_id: str, engine_id: str, kind: str) -> None:
    """Leave a ``kind`` request for the child serving (grid_id, engine_id)."""
    path = run_records.control_path(grid_id, engine_id, kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600)


def acknowledged(grid_id: str, engine_id: str, kind: str, timeout: float) -> bool:
    """Whether the child picked the request up within ``timeout``. An unanswered request is withdrawn,
    so it cannot be mistaken later for one addressed to a replacement child."""
    path = run_records.control_path(grid_id, engine_id, kind)
    deadline = time.monotonic() + timeout
    while path.exists():
        if time.monotonic() >= deadline:
            path.unlink(missing_ok=True)
            return False
        time.sleep(0.05)
    return True


def interrupt_main() -> None:
    """Unwind the main thread with ``KeyboardInterrupt`` — what the POSIX SIGTERM handler raises.

    ``signal.raise_signal`` rather than ``_thread.interrupt_main``: raising the real signal runs the C
    handler, which on Windows also sets the event ``time.sleep`` waits on, so a main thread parked in a
    sleep wakes at once instead of when the sleep ends."""
    signal.raise_signal(signal.SIGINT)


def install_stop(grid_id: str, engine_id: str, interval: float = 0.2) -> Watch | None:
    """What a child calls beside ``signal.signal(SIGTERM, …)``: on Windows, take a stop request as the
    SIGTERM it cannot receive. Returns the watch (None on POSIX) so a later handler — the serve child's
    reload — can be registered on it."""
    watch = Watch.start(grid_id, engine_id, interval=interval)
    if watch is not None:
        watch.on("stop", lambda: interrupt_main())  # looked up per call, so a test can stand in for it
    return watch


class Watch:
    """The child's side: poll for requests and hand each to its handler."""

    def __init__(self, grid_id: str, engine_id: str, interval: float) -> None:
        self._grid_id, self._engine_id, self._interval = grid_id, engine_id, interval
        self._handlers: dict[str, Callable[[], None]] = {}
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._run, name="grid-control-watch", daemon=True)

    @classmethod
    def start(cls, grid_id: str, engine_id: str, interval: float = 0.2) -> Watch | None:
        """Start watching on Windows; None on POSIX, where the signals already do this.

        A ``stop`` left on disk is cleared first: it was addressed to a child that died before reading
        it, and acting on it would stop this one the moment it starts. A ``reload`` is kept — one written
        during bring-up is the SIGHUP `remote/serve.py` holds blocked until its handler exists."""
        if not _IS_WINDOWS:
            return None
        if threading.current_thread() is threading.main_thread():
            # A known disposition for what `interrupt_main` raises, whatever the parent left behind.
            signal.signal(signal.SIGINT, signal.default_int_handler)
        run_records.control_path(grid_id, engine_id, "stop").unlink(missing_ok=True)
        watch = cls(grid_id, engine_id, interval)
        watch._thread.start()
        return watch

    def on(self, kind: str, handler: Callable[[], None]) -> None:
        """Handle ``kind`` from now on. Until then its requests stay queued on disk."""
        self._handlers[kind] = handler

    def close(self) -> None:
        self._closed.set()
        self._thread.join(timeout=max(1.0, self._interval * 5))

    def _run(self) -> None:
        while not self._closed.wait(self._interval):
            for kind, handler in list(self._handlers.items()):
                path = run_records.control_path(self._grid_id, self._engine_id, kind)
                try:
                    path.unlink()
                except FileNotFoundError:
                    continue
                except OSError:
                    continue  # held open by the writer this instant: the next poll takes it
                handler()
