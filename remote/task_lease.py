"""Keeping a claimed task's lease alive, and knowing when to stop (ADR 0032 issue 07, D-c).

One `LeaseRenewer` per claimed task, on its own daemon thread. Its entire contract is one sentence:
**it renews only while the work it is vouching for is really happening, and it never costs the
task.**

What a renewal is allowed to prove
----------------------------------

**The child process, not the network.** The renewer holds the agent's `Popen` — handed to it by
`on_spawn`, the same handle `_run_child` already keeps — and renews only while `poll() is None`. It
never reads a pid from a record and asks the operating system about it: a recycled pid answering
"alive" is the hazard [ADR 0020] and [ADR 0026] removed from the run-record seams, and this is not
the place to reintroduce it. Piggybacking on the heartbeat instead would be worse still — it shows
only that the provider is reachable, so a task whose agent died would hold its lease forever and
keep its project locked by the one-active-task rule, which is the exact failure this issue exists to
prevent.

**Silence is not death.** Nothing here reads the child's output, tracks idle time, or infers a hang
from quiet: a task legitimately produces nothing for ten minutes while a build or a test suite runs.
A genuinely stuck task is caught by its own `deadline_at` on the relay, and by `tasks.task_timeout()`
here — never by an idle timer.

**Before the child exists, it renews anyway**, and that is a deliberate exception rather than an
oversight. The pre-spawn phase is a real `git fetch` whose own ceiling is 300s
(`task_repo._GIT_NETWORK_TIMEOUT_SECONDS`), more than double the relay's 120s lease TTL — so a
renewer that waited for a child would guarantee a reclaim mid-checkout on every slow clone, and the
task would be handed to another provider that has to do the same slow clone again. What renewal
proves in that window is narrower but still true: the supervisor is inside `run_task` for this task.
`_run_and_report` creates this object and closes it in a `finally`, so the renewer's life IS that
call's, and it cannot outlive a supervisor that moved on.

When it stops
-------------

Once `poll()` returns non-None the renewer latches OFF permanently. That is the point: a supervisor
wedged *after* its child died must not be able to hold the lease.

A refusal from the relay latches it off too — but whether the AGENT is also stopped depends on which
refusal, and the split is load-bearing rather than fussy:

  * **403** can only come from the real endpoint, and means exactly one thing: another provider holds
    this task now. The fence has already made this child's work unlandable, so it is killed rather
    than left spending the operator's agent subscription on a result that will be refused.
  * **404 is ambiguous**, because this route is NEWER than the rest of the tasks plane. A relay that
    has not redeployed answers a bare framework 404 for an unmatched route, which is byte-identical
    to its own "this task already ended". Killing on that reading would destroy every task longer
    than one renewal interval on every provider that updated ahead of its relay — a guaranteed break
    on version skew, and the exact inversion of this repo's degrade rule (an absent feature must fail
    back to the OLD behaviour, never to a new failure). So a 404 stops the renewals and leaves the
    agent alone, falling back to the relay's own lease expiry and deadline.

Killing is never required for correctness — D-c's fence holds whatever the loser does. It is a
saving, so it is only spent on certainty.
"""
from __future__ import annotations

import sys
import threading
from typing import Any

from . import relay

# How often the lease is pushed out. LOCKSTEP with the relay's `task_lease_seconds` (120s, grid-src
# `config.py`): four beats of slack, because a tight TTL converts "busy" into "dead" and spends a
# real attempt to learn nothing (ADR 0032 D-c). Renewing FASTER than this is always safe; the value
# that must never be raised without raising the relay's TTL with it is this one.
RENEW_INTERVAL_SECONDS = 30.0

# The answers that mean no later renewal can land either. Deliberately not "any 4xx" — the same
# reasoning `task_events._VERDICT_STATUSES` records: a 422 is about one malformed request and a
# proxy's 408/429 says nothing at all, so latching on either would drop the lease of a task that is
# running perfectly well.
#
# 403 and 404 both stop the renewals, and they are split because only ONE of them may stop the
# AGENT. See `_renew_once`.
_LEASE_IS_SOMEONE_ELSES = 403
_NO_SUCH_TASK_OR_ROUTE = 404
_VERDICT_STATUSES = frozenset({_LEASE_IS_SOMEONE_ELSES, _NO_SUCH_TASK_OR_ROUTE})

# How long `close()` waits for the thread to notice. Bounded because the caller is on its way to
# reporting a terminal result and must not be parked by a renewal in flight; short because the
# thread is a daemon and cannot outlive the process either way.
_CLOSE_JOIN_SECONDS = 5.0


def _warn(message: str) -> None:
    print(f"\n[tasks] {message}", file=sys.stderr)


class LeaseRenewer:
    """Renews one task's lease while its agent lives. Never raises into the task loop."""

    def __init__(self, state: Any, task_id: str, *,
                 interval: float = RENEW_INTERVAL_SECONDS) -> None:
        self._state = state
        self._task_id = task_id
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # The agent's process handle — the ONLY thing this class treats as evidence. `None` means
        # "not spawned yet", which is not the same as "gone"; see the module docstring.
        self._proc: Any = None
        # Set when the relay told us the lease is not ours. Readable by the caller, which is how
        # `_run_and_report` can say what happened rather than reporting a bare push failure.
        self.lost = False

    def attach(self, proc: Any) -> None:
        """Take the agent's process handle. Called from `run_task`'s `on_spawn`."""
        self._proc = proc

    def start(self) -> None:
        """Begin renewing. Guarded end to end — a renewer that cannot start must not fail the task.

        A task that runs without renewal is a task that will be reclaimed and retried, which is a
        far better outcome than one that never runs at all. The reason is said out loud, because the
        visible symptom otherwise is a task that mysteriously restarts every two minutes.
        """
        try:
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name=f"task-lease-{self._task_id}")
            self._thread.start()
        except (Exception, SystemExit) as exc:
            self._thread = None
            _warn(f"could not start lease renewal for task {self._task_id} ({exc!r}); the task "
                  f"still runs, but its lease will lapse and another provider may retry it")

    def close(self) -> None:
        """Stop renewing and wait briefly for the thread to notice.

        Called BEFORE the terminal report, so a renewal cannot be in flight while the state changes
        underneath it — the write would simply be refused, but the log would then carry a 404 that
        looks like a fault rather than a race the caller created.
        """
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=_CLOSE_JOIN_SECONDS)

    def _loop(self) -> None:
        # Waits FIRST: the claim has just written a full-TTL lease, so an immediate renewal would be
        # a round trip that changes nothing.
        while not self._stop.wait(self._interval):
            try:
                if self._child_is_gone():
                    # Latched, permanently. The lease now lapses on the relay's clock and the task
                    # is reclaimed — which is the correct outcome for a supervisor that can no
                    # longer report, and harmless for one that can (its report lands first).
                    return
                if not self._renew_once():
                    return
            except (Exception, SystemExit) as exc:
                # `SystemExit` is this repo's clean-error idiom and is NOT an `Exception`; a guard
                # naming only `Exception` would let it kill this thread silently. Nothing here may
                # escape into the thread and vanish — a renewer that died quietly looks exactly
                # like one that is working, right up until the task is reclaimed mid-run.
                _warn(f"lease renewal for task {self._task_id} hit an unexpected error ({exc!r}); "
                      f"still renewing")

    def _child_is_gone(self) -> bool:
        """Whether the agent we are vouching for has exited.

        `poll()` on the HELD handle, and nothing else. No pid is read back from anywhere, and no
        signal is sent to probe: `proc.pid` is never consulted here at all.
        """
        proc = self._proc
        return proc is not None and proc.poll() is not None

    def _renew_once(self) -> bool:
        """One renewal. False means stop for good.

        The 401 path is the single-retry rule `claim_once` and `report_once` use: refresh once, then
        try again. Reaching a second 401 means no credential is available, and no later renewal can
        land either.
        """
        token = self._state.token()
        try:
            try:
                relay.renew_task_lease(self._state.signaling_url, token, self._task_id)
            except relay.RelayUnauthorized:
                if not self._state.refresh(stale_token=token):
                    raise
                relay.renew_task_lease(
                    self._state.signaling_url, self._state.token(), self._task_id)
        except relay.RelayUnauthorized:
            # No credential is available, so no later renewal can land. NOT a statement about
            # who holds the lease — we simply cannot ask any more — so the agent is left to
            # finish and the relay's deadline decides, exactly as for a 404.
            self._give_up(f"the relay rejected this provider's token while renewing the lease "
                          f"on task {self._task_id} and no refresh is available",
                          stop_the_agent=False)
            return False
        except (Exception, SystemExit) as exc:
            status = getattr(exc, "status", None)
            if status == _LEASE_IS_SOMEONE_ELSES:
                # Only the real endpoint can answer 403, and it means exactly one thing: another
                # provider holds this task now. This child's work can no longer be delivered — its
                # push and its report are both fenced — so stopping it is the honest saving.
                self.lost = True
                self._give_up(
                    f"another provider has taken over task {self._task_id} (403) — this attempt's "
                    f"work can no longer be delivered",
                    stop_the_agent=True)
                return False
            if status == _NO_SUCH_TASK_OR_ROUTE:
                # AMBIGUOUS, and the ambiguity is a fleet-wide hazard rather than a corner case.
                # `POST /tasks/{id}/lease` is newer than the rest of the tasks plane, so a relay that
                # has not redeployed answers a bare framework 404 for the unmatched route — which is
                # indistinguishable from its own "this task already ended". Killing the agent on that
                # reading would destroy every task longer than one renewal interval, on every
                # provider that updated ahead of its relay, until the relay caught up.
                #
                # So this degrades to the behaviour that existed before renewal did: stop renewing,
                # leave the agent alone, and let the relay's lease expiry and deadline decide. The
                # cost of being wrong in this direction is one wasted attempt; the cost of being
                # wrong in the other is the work itself. `lost` stays False — we do not know that we
                # lost anything.
                self._give_up(
                    f"the relay would not renew task {self._task_id}'s lease (404). Either the task "
                    f"has ended, or this relay is older than this provider and has no lease "
                    f"endpoint. The agent is left running and the task now relies on the relay's "
                    f"own deadline",
                    stop_the_agent=False)
                return False
            # A 5xx, a 422, a proxy's 408/429, or a bare transport failure: nobody has decided
            # anything, so the lease may well still be ours. Keep renewing — giving up here would
            # hand a perfectly healthy task to another provider over one bad round trip.
            _warn(f"could not renew task {self._task_id}'s lease (status={status}, {exc}); "
                  f"retrying at the next interval")
        return True

    def _give_up(self, why: str, *, stop_the_agent: bool) -> None:
        """Stop renewing — and, only when we are certain, stop the agent with it.

        Killing is never required for correctness: ADR 0032 D-c's fence holds whatever the loser
        does, which is the whole point of authorizing on the lease rather than on liveness. It is a
        saving, not a safety measure — it stops the operator's own agent subscription being spent on
        a result the relay will refuse. So it is only ever done on an answer that can mean nothing
        else (403), never on an ambiguous one (404), because the cost of killing a healthy agent is
        the work itself while the cost of leaving one running is some wasted quota.
        """
        _warn(f"{why}; lease renewal has stopped "
              + ("and the agent is being terminated" if stop_the_agent
                 else "and the agent has been left running"))
        proc = self._proc
        if proc is None or not stop_the_agent:
            return
        try:
            if proc.poll() is None:
                proc.kill()
        except (Exception, SystemExit) as exc:
            # A child that cannot be killed is bounded by `_run_child`'s own deadline anyway, and
            # this thread owes the task nothing further.
            _warn(f"could not terminate the agent for task {self._task_id} ({exc!r}); it will be "
                  f"stopped by the task's own timeout")
