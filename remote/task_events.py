"""The provider's side of a task's event stream: buffer progress, ship it, never cost the result.

One `TaskEventPublisher` per claimed task. Its entire contract is one sentence: **publishing is
best-effort and must never raise into the caller.** An event is progress — losing one costs a line
of rendered output. The caller is the thread running the child and then reporting the terminal
result, and that report is the opposite: it is the durable record of work already done, and issue 01
already spends bounded retries salvaging it. Letting a lost *event* propagate would trade the thing
that matters for the thing that does not.

Two consequences follow, and they are why this is a class rather than a function:

  * **Coalescing.** Events arrive per line of child output. One POST per line is one TCP+TLS
    handshake per line, which `/bin/echo` never notices and issue 03's stream-json would drown in.
    The buffer flushes at `_FLUSH_AT_EVENTS` or after `_FLUSH_AFTER_SECONDS`, whichever comes first
    — the second so a slow task still shows progress rather than nothing until it ends.

  * **Stopping.** 403 (the lease moved on) and 404 (the task already ended) are verdicts: every
    later batch is refused too. A publisher that kept trying would spend the task's time and flood
    the log with the same line. It latches off and says so ONCE. A 5xx or a transport failure is the
    opposite — nobody decided anything, so the next batch is still attempted.

The wire itself stays in `remote/relay.py`, which is this repo's one stateless relay boundary; a
client per flush (not per event) is what the coalescing above buys.

**Two threads call this, so it is locked.** Until issue 08 there was exactly one caller — the thread
running the child — and being single-caller was a convention `tasks._drain` states out loud rather
than a property of this class. Issue 08's tree snapshot rides the lease heartbeat (ADR 0032 D-f), so
the renewal thread publishes too, and the convention had to become a lock. It is held across the wire
call and not only across the buffer swap, which is the part worth stating: the relay assigns `seq` on
arrival, so two batches released to overtake each other are two batches whose events are numbered out
of order. Out-of-order agent output is a real regression; a tree snapshot arriving a beat late is
not. The cost is that one caller can wait on the other for at most one `_TASK_EVENT_TIMEOUT` (15s),
and a blocking POST inside the child-supervision loop is already how this works.
"""
from __future__ import annotations

import sys
import threading
import time
from typing import Any

from . import relay

# Coalescing bounds. Small enough that a follower sees output as work happens, large enough that a
# chatty child is not one request per line.
_FLUSH_AT_EVENTS = 16
_FLUSH_AFTER_SECONDS = 0.2

# The only two answers that mean NO later batch can land either: the lease moved to another
# provider (403) and the task already ended (404). Deliberately not "any 4xx" — a 422 says this one
# batch was malformed, and a proxy's 408/429 says nothing at all, so latching on either silences a
# task that is still running under a valid lease.
_VERDICT_STATUSES = frozenset({403, 404})

# LOCKSTEP with the relay's `_MAX_EVENT_BYTES` (grid-src `private_server/tasks.py`): the largest
# single event it will store. It is SMALLER than the prompt the relay already accepts (100 KB), so
# a newline-free prompt echoed back as one line exceeds it — and then every batch carrying that
# line is refused, forever. Bounded here rather than only server-side, because a client that keeps
# resending an event the relay can never accept has silently stopped narrating.
MAX_EVENT_BYTES = 64 * 1024


def _warn(message: str) -> None:
    print(f"\n[tasks] {message}", file=sys.stderr)


class TaskEventPublisher:
    """Buffered, best-effort publisher for one task's log. Never raises."""

    def __init__(self, state: Any, task_id: str) -> None:
        self._state = state
        self._task_id = task_id
        self._buffer: list[dict[str, Any]] = []
        self._last_flush = time.monotonic()
        # Latched by a verdict (403/404/422, or a 401 we cannot refresh past). Once set, this
        # publisher is inert for the rest of the task — see the module docstring.
        self._stopped = False
        # Guards the buffer, the latch, AND the wire call. NOT re-entrant, so nothing under it may
        # call back in: `publish` reaches `flush` through `_locked_flush`, never through the public
        # `flush`, which takes the lock itself.
        self._lock = threading.Lock()

    def publish(self, event_type: str, *, blocking: bool = True, **fields: Any) -> bool:
        """Buffer one event, flushing if the buffer is full or has been sitting too long.

        Returns whether the event was ACCEPTED. "Never raises" hides two different outcomes — queued,
        and dropped because this publisher latched off after a verdict — and a caller that cannot
        tell them apart will record a lost event as delivered. Every caller may ignore the answer;
        the one that must not is `task_tree.WorkspaceTree`, which stops re-sending a snapshot once it
        believes it landed.

        `blocking=False` declines to wait for the lock instead of parking on it. The lock is held
        across a POST bounded only by `relay._TASK_EVENT_TIMEOUT`, and the lease heartbeat publishes
        through here (ADR 0032 D-f): waiting there costs the LEASE, and a task reclaimed mid-run is a
        far worse outcome than a progress event skipped for one beat.
        """
        if not self._lock.acquire(blocking=blocking):
            return False
        try:
            if self._stopped:
                return False
            self._buffer.append({"type": event_type, **fields})
            if (len(self._buffer) >= _FLUSH_AT_EVENTS
                    or time.monotonic() - self._last_flush >= _FLUSH_AFTER_SECONDS):
                self._locked_flush()
            return True
        finally:
            self._lock.release()

    def flush(self) -> None:
        """Send whatever is buffered. Swallows every failure — deliberately, see the docstring."""
        with self._lock:
            self._locked_flush()

    def _locked_flush(self) -> None:
        """`flush`'s body, for callers that already hold the lock. See `_lock` for why that matters."""
        if self._stopped or not self._buffer:
            return
        batch, self._buffer = self._buffer, []
        self._last_flush = time.monotonic()
        try:
            self._send(batch)
        except relay.RelayUnauthorized:
            # One refresh, then stop: the same single-retry rule `claim_once` and `report_once` use.
            # Reaching here twice means no credential is available, and no later batch can land.
            self._stop(f"relay rejected the token while publishing events for task "
                       f"{self._task_id} — progress events stop (the result is unaffected)")
        except (Exception, SystemExit) as exc:
            # `SystemExit` is this repo's clean-error idiom and is NOT an `Exception`; a guard that
            # named only `Exception` would let it through and kill the task loop's thread.
            status = getattr(exc, "status", None)
            if status in _VERDICT_STATUSES:
                self._stop(
                    f"the relay refused progress events for task {self._task_id} "
                    f"(status={status}, {exc}) — publishing stops for this task "
                    f"(403 = another provider holds the lease, 404 = the task already ended)")
            else:
                # Everything else is about THIS BATCH or about nobody having decided anything, so
                # the next batch is still worth sending. Latching on the whole 4xx range was wrong
                # in both directions: a 422 (one event over the relay's size cap) or a proxy's
                # 408/429 would kill every remaining progress event for a task that is still
                # running under a perfectly valid lease.
                _warn(f"dropped {len(batch)} progress event(s) for task {self._task_id} "
                      f"(status={status}, {exc}) — the stream will have a gap; the task and its "
                      f"result are unaffected")

    def close(self) -> None:
        """Flush the tail. The last lines of a task are the ones that say how it went."""
        self.flush()

    def _send(self, batch: list[dict[str, Any]]) -> None:
        token = self._state.token()
        try:
            relay.publish_task_events(self._state.signaling_url, token, self._task_id, batch)
        except relay.RelayUnauthorized:
            if not self._state.refresh(stale_token=token):
                raise
            relay.publish_task_events(
                self._state.signaling_url, self._state.token(), self._task_id, batch)

    def _stop(self, message: str) -> None:
        self._stopped = True
        self._buffer.clear()
        _warn(message)
