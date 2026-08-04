"""The provider's task loop — the second, independent claim loop (ADR 0032).

A task is not an inference job, so it does not travel through `remote/serve.py`'s poll workers:
it is claimed from a durable queue rather than routed to this provider at enqueue time, it runs for
minutes rather than seconds, and it carries a lease rather than escrow. It therefore gets its own
loop, and the two must not be able to take each other down — a saturated task must not affect
inference dispatch, and an inference fault must not stop task serving.

That independence is why this loop is modelled on `serve._reload_loop` and NOT on `serve._poll_loop`:

  * `_poll_loop` runs under `_supervise`, which sets `state.stop` on any fault and tears the whole
    engine down. Correct there — a dead poll worker strands advertised inference capacity. Wrong
    here: a task fault would stop inference too.
  * So this loop self-guards, catching `(Exception, SystemExit)` — `SystemExit` included because it
    is this repo's clean-error idiom and is not an `Exception`, so a loop guarding only `Exception`
    dies silently in a daemon thread.

It observes `state.stop` (engine teardown retires it) but never sets it. `state.tasks_stop` retires
the task loop alone.
"""
from __future__ import annotations

import subprocess
import sys
from typing import Any

from . import relay

# How long a tracer-bullet child may run. `/bin/echo` returns instantly; this exists so a wedged
# child cannot park the loop forever. The real budget for an agent run is the task's own
# `deadline_at`, enforced relay-side by the reaper slice — not a constant here.
_TASK_TIMEOUT_SECONDS = 30.0
# Back-off after a transient claim failure, so an unreachable relay is retried without spinning.
_CLAIM_BACKOFF_SECONDS = 5.0
_TASK_OUTPUT_MAX_CHARS = 100_000
# How many CONSECUTIVE 404s from the claim endpoint mean "this relay has no tasks plane" rather than
# "something transient". One is not evidence: `serve.py`'s bring-up records the field incident where
# "a master mid-respawn answers 503 (or 404), and before this loop that single answer ended the
# child". A relay redeploy would otherwise retire task serving on every provider in the fleet
# simultaneously, for the life of each process. Counted consecutively, and reset by any non-404, so
# unrelated blips spread over days never accumulate into a retirement.
_MISSING_PLANE_404_BUDGET = 3
# Attempts to land a terminal report before giving up. Worth retrying at all because this is the one
# stranding that is salvageable: the child already ran, so only the last message was lost, and until
# lease expiry exists (issue 07) a lost report strands the task AND locks its project. Bounded and
# small — a provider that cannot reach the relay at all is not going to be rescued by a fourth try,
# and the loop still owes its attention to the next task.
_REPORT_ATTEMPTS = 3
_REPORT_BACKOFF_SECONDS = 2.0


def _warn(message: str) -> None:
    print(f"\n[tasks] {message}", file=sys.stderr)


def claim_once(state: Any) -> dict[str, Any] | None:
    """One claim long-poll; on 401 refresh the token and retry exactly once.

    Mirrors `serve.poll_once` — the same credential, the same single-retry rule, a different queue.
    """
    token = state.token()
    try:
        return relay.claim_task(state.signaling_url, token)
    except relay.RelayUnauthorized:
        if state.refresh(stale_token=token):
            return relay.claim_task(state.signaling_url, state.token())
        raise


def report_once(serve_state: Any, task_id: str, *, state: str, output: str | None,
                error: str | None) -> None:
    """Report a terminal result; on 401 refresh the token and retry exactly once.

    The serve state is named `serve_state` here only because `state` is the wire's name for the
    task's terminal state, and the wire vocabulary wins on a function whose whole job is to send it.
    """
    token = serve_state.token()
    try:
        relay.report_task_result(
            serve_state.signaling_url, token, task_id,
            state=state, output=output, error=error)
    except relay.RelayUnauthorized:
        if not serve_state.refresh(stale_token=token):
            raise
        relay.report_task_result(
            serve_state.signaling_url, serve_state.token(), task_id,
            state=state, output=output, error=error)


def run_task(job: dict[str, Any]) -> tuple[str, str | None, str | None]:
    """Run one task's child and return `(terminal_state, output, error)`.

    The tracer bullet runs `/bin/echo` — no git, no agent. What is being proven is the seam, so the
    contract that matters is this one: **every** failure mode returns a `failed` triple. Nothing
    raises out of here, because a raise would be indistinguishable from a bug in the loop itself.
    """
    prompt = job.get("prompt")
    if not isinstance(prompt, str):
        # The job dict came off the wire; a missing or mistyped prompt is bad input, not a crash.
        return ("failed", None, f"task has no usable prompt (got {type(prompt).__name__})")

    try:
        completed = subprocess.run(
            ["/bin/echo", prompt],
            capture_output=True, text=True, timeout=_TASK_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return ("failed", None, f"task timed out after {_TASK_TIMEOUT_SECONDS:.0f}s")
    except (Exception, SystemExit) as exc:
        return ("failed", None, f"could not run the task: {exc}")

    if completed.returncode != 0:
        return ("failed", None,
                f"task exited {completed.returncode}: {(completed.stderr or '')[:500]}")
    return ("completed", (completed.stdout or "")[:_TASK_OUTPUT_MAX_CHARS], None)


def task_loop(state: Any) -> None:
    """Claim, run, report — until the engine stops or the task plane retires.

    Retiring (`tasks_stop`) rather than stopping (`state.stop`) is the whole point: every exit path
    below leaves inference serving. `state.stop` is only ever read here, never written.
    """
    consecutive_404s = 0
    while not (state.stop.is_set() or state.tasks_stop.is_set()):
        try:
            job = claim_once(state)
        except relay.RelayUnauthorized:
            _warn("relay rejected the token and refresh is unavailable — task serving retired "
                  "(inference is unaffected)")
            state.tasks_stop.set()
            return
        except relay.RelayError as exc:
            if getattr(exc, "status", None) == 404:
                consecutive_404s += 1
                if consecutive_404s >= _MISSING_PLANE_404_BUDGET:
                    # Persistently absent, so retrying cannot make the endpoint appear. Retire
                    # rather than log the same 404 every few seconds for the life of the process.
                    _warn(f"this grid's relay has no tasks plane ({consecutive_404s} consecutive "
                          "404s) — task serving retired")
                    state.tasks_stop.set()
                    return
                _warn(f"claim 404 ({consecutive_404s}/{_MISSING_PLANE_404_BUDGET}); retrying — a "
                      "relay mid-respawn answers this too")
                state.tasks_stop.wait(_CLAIM_BACKOFF_SECONDS)
                continue
            consecutive_404s = 0
            _warn(f"claim failed ({exc}); retrying")
            state.tasks_stop.wait(_CLAIM_BACKOFF_SECONDS)
            continue
        except (Exception, SystemExit) as exc:
            # Never let an unexpected claim fault escape into the thread and vanish.
            consecutive_404s = 0
            _warn(f"unexpected claim error ({exc!r}); retrying")
            state.tasks_stop.wait(_CLAIM_BACKOFF_SECONDS)
            continue

        consecutive_404s = 0
        if job is None:  # 204 — nothing queued; claim again
            continue

        _run_and_report(state, job)


def _run_and_report(state: Any, job: dict[str, Any]) -> None:
    """One claimed task, start to terminal report. Guarded so no single task can end the loop."""
    task_id = str(job.get("task_id") or "")
    if not task_id:
        # The relay always sends one, so this is wire drift. It is already claimed server-side, and
        # with no id there is no way to report it — say so plainly rather than "dropping it".
        _warn(f"claimed a task with no id — it is now stuck `running` on the relay and its project "
              f"is locked until an operator clears it: {job!r}")
        return

    try:
        terminal_state, output, error = run_task(job)
    except (Exception, SystemExit) as exc:
        # `run_task` is written not to raise; if it ever does, the task still owes the relay a
        # terminal report — silence would hold the project's lock until the lease expires.
        terminal_state, output, error = ("failed", None, f"task runner raised: {exc!r}")

    for attempt in range(1, _REPORT_ATTEMPTS + 1):
        try:
            report_once(state, task_id, state=terminal_state, output=output, error=error)
            return
        except (Exception, SystemExit) as exc:
            status = getattr(exc, "status", None)
            if _is_answer_not_blip(status) or attempt == _REPORT_ATTEMPTS:
                # Losing one result must not cost the loop — but say what actually happens, which is
                # NOT "it will be requeued": nothing renews or expires a lease in this build, so the
                # task stays `running` forever, and `running` is inside the one-active-task index
                # predicate, so the PROJECT is locked with it. Naming the status separates incidents
                # the relay already distinguishes: 403 means another provider holds the lease and may
                # have run this task too; 404 means it was already terminal (possibly our own earlier
                # attempt, which did land); a bare transport failure means nobody knows.
                _warn(f"could not report task {task_id} after {attempt} attempt(s) "
                      f"(status={status}, {exc}) — the result is lost and the task is stuck "
                      f"`running`, locking its project, until an operator clears it "
                      f"(lease expiry and requeue are not implemented in this build)")
                return
            _warn(f"report attempt {attempt}/{_REPORT_ATTEMPTS} for task {task_id} failed "
                  f"({exc}); retrying")
            state.tasks_stop.wait(_REPORT_BACKOFF_SECONDS)


def _is_answer_not_blip(status: int | None) -> bool:
    """Whether the relay ANSWERED (so retrying cannot change the outcome) rather than went missing.

    4xx here are verdicts: 403 the lease is someone else's, 404 the task is already terminal, 422 we
    sent something malformed. Retrying any of them just delays the loop. 5xx and a bare transport
    failure (`status is None`) are the opposite — nobody decided anything yet.
    """
    return status is not None and 400 <= status < 500
