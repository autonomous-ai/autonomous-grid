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

import queue
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from . import relay
from .task_events import MAX_EVENT_BYTES

# Sentinel: the child's stdout reached EOF. A plain object() rather than None or "" — a task's
# output legitimately contains blank lines, and both would be indistinguishable from one.
_STDOUT_EOF = object()

# How long a tracer-bullet child may run. `/bin/echo` returns instantly; this exists so a wedged
# child cannot park the loop forever. The real budget for an agent run is the task's own
# `deadline_at`, enforced relay-side by the reaper slice — not a constant here.
_TASK_TIMEOUT_SECONDS = 30.0
# Back-off after a transient claim failure, so an unreachable relay is retried without spinning.
_CLAIM_BACKOFF_SECONDS = 5.0
_TASK_OUTPUT_MAX_CHARS = 100_000
# How long to wait for a killed child to actually die before giving up on reaping it. SIGKILL is
# not instantaneous, and a bounded wait here is what stops a zombie without letting the loop hang
# on one that is stuck in uninterruptible sleep.
_KILL_REAP_SECONDS = 5.0
# Characters of one output line that fit inside `task_events.MAX_EVENT_BYTES` in the worst case
# (6 bytes/char once JSON `\uXXXX` escaping is counted), with room left for the event's other keys.
_MAX_LINE_CHARS = MAX_EVENT_BYTES // 6 - 200
_TRUNCATION_MARKER = "… [truncated]"
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


def _task_argv(prompt: str) -> list[str]:
    """The child this slice runs. `/bin/echo` — no git, no agent; issue 03 replaces this."""
    return ["/bin/echo", prompt]


def _drain(stream, sink: list[str]) -> None:
    """Read a child pipe to EOF into `sink`. Runs on its own thread; never raises out of it.

    Both pipes need a reader for the whole run, not just stdout: a child that fills the OTHER pipe's
    buffer blocks on write and looks exactly like a hang.
    """
    try:
        for line in stream:
            sink.append(line)
    except (Exception, SystemExit):
        pass  # a closed/killed pipe is the ordinary end of this thread, not an incident


def _bounded_line(line: str) -> str:
    """One line of child output, trimmed to fit inside the relay's per-event cap.

    A `task.output` event carries this string inside a JSON object, and the relay refuses any event
    whose encoded form exceeds `task_events.MAX_EVENT_BYTES`. That limit is SMALLER than the prompt
    the relay already accepts, so `/bin/echo` on a newline-free 100 KB prompt produces exactly one
    line that can never be published — and without this, every batch carrying it is refused for as
    long as the task runs.

    The budget is in CHARACTERS against a bytes limit, so it is divided by the worst case: a
    character can reach 6 bytes once JSON escaping (`\\uXXXX`) is counted, not merely 4 for UTF-8.
    Truncation is marked, because output that silently stops mid-line reads like a crash.
    """
    stripped = line.rstrip("\n")
    if len(stripped) <= _MAX_LINE_CHARS:
        return stripped
    return stripped[:_MAX_LINE_CHARS] + _TRUNCATION_MARKER


def _run_child(argv: list[str], *, timeout: float, publish: Callable[..., None]) -> tuple[int, str]:
    """Run `argv`, publishing each stdout line as it arrives. Returns `(returncode, stdout)`.

    Raises `subprocess.TimeoutExpired` when the wall-clock budget is spent, so the caller's existing
    timeout branch is unchanged.

    Reading happens on daemon threads feeding a queue, and the main loop waits on the QUEUE with the
    remaining budget as its timeout — never on the pipe. `for line in proc.stdout` would block
    forever on a child that prints nothing, which silently deletes the deadline; and the deadline is
    absolute rather than reset per line, so an early line cannot buy unlimited wallclock.

    The child is killed on the way out. A daemon thread does not reap a process, so an abandoned
    child would keep the provider's CPU — and, once issue 03 lands, its agent subscription.
    """
    # `errors="replace"`, not the default strict decode: one non-UTF-8 byte from the child would
    # otherwise raise `UnicodeDecodeError` inside the reader thread, where the broad guard swallows
    # it — the thread ends, EOF is queued, and the task reports `completed` having LOST its output
    # (measured: the whole of it, not just the tail). The same fix this repo already applies to
    # `orphan_sweep`'s process-list subprocess, for the same reason.
    proc = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, errors="replace", bufsize=1)

    lines: list[str] = []
    stderr_lines: list[str] = []
    queued: queue.Queue = queue.Queue()

    def _pump_stdout() -> None:
        try:
            for line in proc.stdout:
                queued.put(line)
        except (Exception, SystemExit):
            pass
        finally:
            queued.put(_STDOUT_EOF)

    threading.Thread(target=_pump_stdout, daemon=True, name="task-stdout").start()
    threading.Thread(
        target=_drain, args=(proc.stderr, stderr_lines), daemon=True, name="task-stderr").start()

    deadline = time.monotonic() + timeout
    publish_broken = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)
            try:
                item = queued.get(timeout=remaining)
            except queue.Empty:
                raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout) from None
            if item is _STDOUT_EOF:
                break
            lines.append(item)
            # The publisher is documented as never raising, but `run_task`'s contract cannot rest on
            # another module keeping a promise — a progress event must never fail the task. Reaching
            # the guard means the publisher has a BUG, so say so: the visible symptom is a stream
            # that simply goes quiet, and silence is what this whole plane treats as "still working".
            # Once per run, not per line — a broken publisher would otherwise emit one warning per
            # line of the task's output.
            try:
                publish("task.output", text=_bounded_line(item))
            except (Exception, SystemExit) as exc:
                if not publish_broken:
                    publish_broken = True
                    _warn(f"the event publisher raised ({exc!r}) — it is documented never to; "
                          f"progress events for this task are being dropped from here on. The task "
                          f"itself is unaffected and still reports its result.")

        proc.wait(timeout=max(0.0, deadline - time.monotonic()))
    finally:
        if proc.poll() is None:
            proc.kill()
            # Reap it, so the child cannot linger as a zombie once this thread moves on.
            try:
                proc.wait(timeout=_KILL_REAP_SECONDS)
            except (Exception, SystemExit):
                pass

    if proc.returncode != 0:
        raise _ChildFailed(proc.returncode, "".join(stderr_lines))
    return proc.returncode, "".join(lines)


class _ChildFailed(Exception):
    """A non-zero exit, carrying the stderr the caller reports. Internal to this module."""

    def __init__(self, returncode: int, stderr: str) -> None:
        super().__init__(f"exited {returncode}")
        self.returncode = returncode
        self.stderr = stderr


def run_task(job: dict[str, Any],
             publish: Callable[..., None] | None = None) -> tuple[str, str | None, str | None]:
    """Run one task's child and return `(terminal_state, output, error)`.

    The tracer bullet runs `/bin/echo` — no git, no agent. What is being proven is the seam, so the
    contract that matters is this one: **every** failure mode returns a `failed` triple. Nothing
    raises out of here, because a raise would be indistinguishable from a bug in the loop itself.

    `publish` is optional: the child is the point and the stream is an observer, so a caller with no
    channel wired runs a task exactly as before.
    """
    prompt = job.get("prompt")
    if not isinstance(prompt, str):
        # The job dict came off the wire; a missing or mistyped prompt is bad input, not a crash.
        return ("failed", None, f"task has no usable prompt (got {type(prompt).__name__})")

    sink = publish if publish is not None else _no_publish
    try:
        _returncode, output = _run_child(
            _task_argv(prompt), timeout=_TASK_TIMEOUT_SECONDS, publish=sink)
    except subprocess.TimeoutExpired:
        return ("failed", None, f"task timed out after {_TASK_TIMEOUT_SECONDS:.0f}s")
    except _ChildFailed as exc:
        return ("failed", None, f"task exited {exc.returncode}: {exc.stderr[:500]}")
    except (Exception, SystemExit) as exc:
        return ("failed", None, f"could not run the task: {exc}")

    return ("completed", output[:_TASK_OUTPUT_MAX_CHARS], None)


def _no_publish(*_args: Any, **_kwargs: Any) -> None:
    """The default sink: a task with no channel attached still runs."""


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

    publisher = _publisher_for(state, task_id, job)
    try:
        terminal_state, output, error = run_task(job, publisher.publish)
    except (Exception, SystemExit) as exc:
        # `run_task` is written not to raise; if it ever does, the task still owes the relay a
        # terminal report — silence would hold the project's lock until the lease expires.
        terminal_state, output, error = ("failed", None, f"task runner raised: {exc!r}")
    finally:
        # Flush the tail BEFORE reporting terminal: the relay appends `task.terminal` as part of the
        # state change, and a batch arriving after that is refused (the task is no longer running),
        # so the last lines of output would be exactly the ones lost.
        try:
            publisher.close()
        except (Exception, SystemExit) as exc:
            _warn(f"could not flush the last progress events for task {task_id} ({exc!r})")

    for attempt in range(1, _REPORT_ATTEMPTS + 1):
        try:
            report_once(state, task_id, state=terminal_state, output=output, error=error)
            return
        except (Exception, SystemExit) as exc:
            status = getattr(exc, "status", None)
            if status == 404:
                # NOT a loss, and the old message said it was. 404 means the task is no longer
                # `running` — and the most likely way to reach it after a failed attempt is that an
                # EARLIER attempt of ours landed and only its ack was lost. Telling an operator to
                # go clear a task that actually completed correctly is worse than saying nothing.
                _warn(f"task {task_id} was already terminal when reporting (404) — most likely an "
                      f"earlier attempt landed and only its acknowledgement was lost. Confirm with "
                      f"`grid task get {task_id}`; no action is needed if it reads terminal.")
                return
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


def _publisher_for(state: Any, task_id: str, job: dict[str, Any]) -> Any:
    """The task's event channel, opened with `task.attempt_started`.

    Constructed here rather than inside `run_task` so the marker is published even when the child
    never starts: a client watching a claimed task should see WHICH attempt picked it up and where,
    and a task that fails at spawn is exactly when that matters most.

    Never raises. If the channel cannot be opened at all the task still runs — a provider that
    refused to work because it could not narrate would be strictly worse than a silent one.
    """
    from .task_events import TaskEventPublisher

    publisher = TaskEventPublisher(state, task_id)
    try:
        publisher.publish(
            "task.attempt_started",
            attempt=job.get("attempt"),
            provider_id=job.get("provider_id"),
        )
    except (Exception, SystemExit) as exc:
        _warn(f"could not announce the start of task {task_id} ({exc!r}); running it anyway")
    return publisher


def _is_answer_not_blip(status: int | None) -> bool:
    """Whether the relay ANSWERED (so retrying cannot change the outcome) rather than went missing.

    4xx here are verdicts: 403 the lease is someone else's, 404 the task is already terminal, 422 we
    sent something malformed. Retrying any of them just delays the loop. 5xx and a bare transport
    failure (`status is None`) are the opposite — nobody decided anything yet.
    """
    return status is not None and 400 <= status < 500
