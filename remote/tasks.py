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

import os
import queue
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from . import relay, task_agent, task_stream

# Queue sentinels. Plain `object()`s rather than None or "" — a task's output legitimately contains
# blank lines, and both would be indistinguishable from one. `_EOF` is posted once per pipe.
_EOF = object()
_STDOUT = object()
_STDERR = object()

# `errors="replace"` decoding means a line can carry U+FFFD; nothing here depends on the line being
# valid UTF-8, only on it being a `str`.

# How long an agent child may run before the provider gives up on it.
#
# LOCKSTEP-ish with the relay's `task_deadline_seconds` (grid-src `config.py`), which defaults to the
# same 3600. It is the provider's own backstop, not the authority: the task's `deadline_at` is
# relay-side and is what the reaper enforces (issue 07). A provider whose budget is longer than the
# relay's deadline simply keeps working on a task that has already been given up on; one whose budget
# is shorter fails it early. Neither corrupts anything, which is why this is a tunable default rather
# than a value the claim payload has to carry.
DEFAULT_TASK_TIMEOUT_SECONDS = 3600.0
TASK_TIMEOUT_ENV = "GRID_TASK_TIMEOUT_SECONDS"
# Back-off after a transient claim failure, so an unreachable relay is retried without spinning.
_CLAIM_BACKOFF_SECONDS = 5.0
_TASK_OUTPUT_MAX_CHARS = 100_000
# How long to wait for a killed child to actually die before giving up on reaping it. SIGKILL is
# not instantaneous, and a bounded wait here is what stops a zombie without letting the loop hang
# on one that is stuck in uninterruptible sleep.
_KILL_REAP_SECONDS = 5.0
# How many stderr lines are published as progress. Bounded because stderr is not the task's output —
# it is the channel a chatty child, or one stuck in a retry loop, floods. The full text is still
# collected for the failure message; this only caps what reaches the durable event log.
_MAX_PUBLISHED_STDERR_LINES = 200
# How much of each of the child's streams is RETAINED in memory. A task runs for up to an hour and
# `stream-json` is verbose, so "keep every line" is unbounded growth on exactly the long runs this
# feature exists for. Nothing needs the whole of either: `run_task` reports the translator's
# `result_text` rather than raw stdout, and stderr is only ever read back 500 characters at a time
# for the failure message. The HEAD is kept, not the tail — the first error a child prints is
# almost always the one that explains the rest.
_MAX_COLLECTED_CHARS = 256 * 1024
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


@dataclass(frozen=True)
class TaskOutcome:
    """What one task run produced. A record rather than a tuple — it grew a fourth field the moment
    a real agent ran, and a fifth is coming with issue 05's result commit."""

    state: str
    output: str | None
    error: str | None
    #: The Claude Code conversation this run opened, so the project's next task resumes it (issue 06).
    session_id: str | None = None


def task_timeout() -> float:
    """The provider's own wall-clock backstop for one agent run.

    Misconfiguration falls back rather than failing: a task loop that refused to start because an
    operator typed `1h` would take task serving down for the life of the process, which is a far
    worse answer than running with the default and saying so.
    """
    raw = (os.getenv(TASK_TIMEOUT_ENV) or "").strip()
    if not raw:
        return DEFAULT_TASK_TIMEOUT_SECONDS
    try:
        seconds = float(raw)
    except ValueError:
        seconds = 0.0
    if seconds <= 0:
        _warn(f"{TASK_TIMEOUT_ENV}={raw!r} is not a positive number of seconds; "
              f"using {DEFAULT_TASK_TIMEOUT_SECONDS:.0f}s")
        return DEFAULT_TASK_TIMEOUT_SECONDS
    return seconds


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
                error: str | None, session_id: str | None = None) -> None:
    """Report a terminal result; on 401 refresh the token and retry exactly once.

    The serve state is named `serve_state` here only because `state` is the wire's name for the
    task's terminal state, and the wire vocabulary wins on a function whose whole job is to send it.
    """
    token = serve_state.token()
    try:
        relay.report_task_result(
            serve_state.signaling_url, token, task_id,
            state=state, output=output, error=error, session_id=session_id)
    except relay.RelayUnauthorized:
        if not serve_state.refresh(stale_token=token):
            raise
        relay.report_task_result(
            serve_state.signaling_url, serve_state.token(), task_id,
            state=state, output=output, error=error, session_id=session_id)


class _Collected:
    """One of the child's streams, retained up to `_MAX_COLLECTED_CHARS`.

    The running length is carried rather than recomputed: a chatty child produces hundreds of
    thousands of lines, and re-summing the buffer per line is quadratic — a fix for one resource
    problem that introduces another.

    The head is kept, not the tail. The first error a child prints is almost always the one that
    explains the rest, and the point it stopped is MARKED — a failure message that simply ends reads
    as the child having had no more to say.
    """

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._length = 0
        self._full = False

    def add(self, line: str) -> None:
        if self._full:
            return
        if self._length + len(line) > _MAX_COLLECTED_CHARS:
            self._full = True
            self._parts.append(task_stream.TRUNCATION_MARKER)
            return
        # Scrubbed on the way IN, so nothing downstream has to remember to. What is retained here
        # leaves the provider as `_ChildFailed.stderr` → the task's `error` → the relay's durable
        # `task.terminal` event, which the requesting user reads back. That is a different door from
        # the progress events `StreamTranslator` scrubs, and it was open.
        #
        # Placed after the budget check, so a child that floods this buffer pays for the regex only
        # until the buffer is full rather than once per line forever.
        line = task_stream.redact(line)
        self._parts.append(line)
        self._length += len(line)

    def text(self) -> str:
        return "".join(self._parts)


class _Tail:
    """The LAST `_MAX_COLLECTED_CHARS` of a stream — what stderr needs.

    Retention has to want the same end of the stream that the reader does. The failure message is
    built from `stderr[-500:]`, deliberately the tail, because the line saying *why* a child exited
    non-zero is the last one it wrote. Keeping the head instead throws that line away before the
    slice runs, and the slice then returns whatever sat on the cap boundary — plausible, irrelevant,
    and indistinguishable from the real reason. That is not a corner case: it is precisely the
    "child stuck in a retry loop" the cap exists for.

    stdout keeps its head (`_Collected`) for the mirror-image reason — it is a transcript, and a
    transcript explains itself at the beginning.
    """

    def __init__(self) -> None:
        self._parts: deque[str] = deque()
        self._length = 0
        self._dropped = False

    def add(self, line: str) -> None:
        line = task_stream.redact(line)
        if len(line) > _MAX_COLLECTED_CHARS:
            # One line longer than the whole budget. Keep its tail for the same reason as above,
            # rather than letting a single line push the buffer past the bound it exists to enforce.
            line = line[-_MAX_COLLECTED_CHARS:]
            self._dropped = True
        self._parts.append(line)
        self._length += len(line)
        while self._length > _MAX_COLLECTED_CHARS:
            self._length -= len(self._parts.popleft())
            self._dropped = True

    def text(self) -> str:
        # The marker goes at the FRONT: what was dropped is the beginning, and a marker on the end
        # would claim the opposite — that the child's last words are the ones missing.
        prefix = f"{task_stream.TRUNCATION_MARKER}\n" if self._dropped else ""
        return prefix + "".join(self._parts)


class _Reporter:
    """Turns the child's two output streams into published events. Called from the MAIN loop only.

    It exists to hold the two pieces of state that must not be per-line: the translator (a session
    id and a final answer accumulate across lines) and the "the publisher is broken" latch.
    """

    def __init__(self, publish: Callable[..., None], translator: Any) -> None:
        self._publish = publish
        self._translator = translator
        self._broken = False
        self._stderr_published = 0

    def stdout(self, line: str) -> None:
        """One line of the agent's stream, as whatever events it turned into."""
        if self._translator is None:
            # No translator: the line is the output. Kept so a caller can still run a plain child.
            self._emit("task.output", text=task_stream.bounded(line.rstrip("\n")))
            return
        # The translator is documented never to raise, and a fault there must not fail the task
        # either — a run that lost its narration is still a run that did the work.
        try:
            events = self._translator.feed(line)
        except (Exception, SystemExit) as exc:
            self._complain(f"the stream translator raised ({exc!r})")
            return
        for event_type, fields in events:
            self._emit(event_type, **fields)

    def stderr(self, line: str) -> None:
        """One line of the child's stderr, up to a cap.

        Bounded because stderr is not the task's output — it is the channel a child stuck in a retry
        loop floods. The full text is still collected for the failure message; only what reaches the
        durable event log is capped, and the cap is announced rather than silently applied.
        """
        if self._stderr_published > _MAX_PUBLISHED_STDERR_LINES:
            return
        self._stderr_published += 1
        if self._stderr_published > _MAX_PUBLISHED_STDERR_LINES:
            self._emit("task.stderr", text=f"… [further stderr suppressed after "
                                           f"{_MAX_PUBLISHED_STDERR_LINES} lines]")
            return
        self._emit("task.stderr", text=task_stream.redact(
            task_stream.bounded(line.rstrip("\n"))))

    def _emit(self, event_type: str, **fields: Any) -> None:
        # The publisher is documented as never raising, but `run_task`'s contract cannot rest on
        # another module keeping a promise — a progress event must never fail the task. Reaching the
        # guard means the publisher has a BUG, so say so: the visible symptom is a stream that simply
        # goes quiet, and silence is what this whole plane treats as "still working".
        try:
            self._publish(event_type, **fields)
        except (Exception, SystemExit) as exc:
            self._complain(f"the event publisher raised ({exc!r}) — it is documented never to")

    def _complain(self, what: str) -> None:
        """Say it once per run, not once per line — a broken publisher would otherwise emit one
        warning for every line of a task's output."""
        if self._broken:
            return
        self._broken = True
        _warn(f"{what}; progress events for this task are being dropped from here on. The task "
              f"itself is unaffected and still reports its result.")


def _drain(stream, sink: _Tail, queued: "queue.Queue | None" = None) -> None:
    """Read a child pipe to EOF into `sink`. Runs on its own thread; never raises out of it.

    Both pipes need a reader for the whole run, not just stdout: a child that fills the OTHER pipe's
    buffer blocks on write and looks exactly like a hang.

    Lines are also handed to `queued` when one is given, so the MAIN loop can publish them. Nothing
    is published from this thread: `TaskEventPublisher` buffers into a plain list with no lock, and a
    second thread appending to it while the stdout pump flushes it is a corrupted batch — silently,
    and only under load.
    """
    try:
        for line in stream:
            sink.add(line)
            if queued is not None:
                queued.put((_STDERR, line))
    except (Exception, SystemExit):
        pass  # a closed/killed pipe is the ordinary end of this thread, not an incident
    finally:
        # In a `finally`, and unconditional: the main loop counts EOFs to know both pipes are done,
        # so a thread that ends without posting one parks that loop until the task's whole deadline
        # expires. An hour, for a child that exited in milliseconds.
        if queued is not None:
            queued.put(_EOF)


def _run_child(argv: list[str], *, timeout: float, publish: Callable[..., None],
               cwd: str | None = None, env: dict[str, str] | None = None,
               translator: Any = None,
               on_spawn: Callable[[subprocess.Popen], None] | None = None) -> tuple[int, str]:
    """Run `argv`, publishing each stdout line as it arrives. Returns `(returncode, stdout)`.

    Raises `subprocess.TimeoutExpired` when the wall-clock budget is spent, so the caller's existing
    timeout branch is unchanged.

    Reading happens on daemon threads feeding a queue, and the main loop waits on the QUEUE with the
    remaining budget as its timeout — never on the pipe. `for line in proc.stdout` would block
    forever on a child that prints nothing, which silently deletes the deadline; and the deadline is
    absolute rather than reset per line, so an early line cannot buy unlimited wallclock.

    The child is killed on the way out. A daemon thread does not reap a process, so an abandoned
    child would keep the provider's CPU — and its Claude subscription.

    `on_spawn` receives the `Popen` the instant it exists. That handle is the supervisor's ONLY grip
    on the child: issue 07's lease renewal proves liveness with `poll() is None`, never by reading a
    pid back from a record and signalling it — the hazard ADRs 0020 and 0026 removed from the
    run-record seams and which is not reintroduced here.
    """
    # `errors="replace"`, not the default strict decode: one non-UTF-8 byte from the child would
    # otherwise raise `UnicodeDecodeError` inside the reader thread, where the broad guard swallows
    # it — the thread ends, EOF is queued, and the task reports `completed` having LOST its output
    # (measured: the whole of it, not just the tail). The same fix this repo already applies to
    # `orphan_sweep`'s process-list subprocess, for the same reason.
    proc = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, errors="replace", bufsize=1, cwd=cwd, env=env)
    if on_spawn is not None:
        on_spawn(proc)

    lines = _Collected()
    # Tail-retained, because the failure message reads it from the end — see `_Tail`.
    stderr_lines = _Tail()
    queued: queue.Queue = queue.Queue()

    def _pump_stdout() -> None:
        try:
            for line in proc.stdout:
                queued.put((_STDOUT, line))
        except (Exception, SystemExit):
            pass
        finally:
            queued.put(_EOF)

    threading.Thread(target=_pump_stdout, daemon=True, name="task-stdout").start()
    threading.Thread(
        target=_drain, args=(proc.stderr, stderr_lines, queued),
        daemon=True, name="task-stderr").start()

    deadline = time.monotonic() + timeout
    reporter = _Reporter(publish, translator)
    # BOTH pipes, not just stdout. Breaking on stdout's EOF alone would leave whatever the stderr
    # thread had already queued unpublished — and a child that dies with its explanation on stderr
    # is exactly the run whose last lines matter most. Both reach EOF when the child exits, and the
    # deadline still governs a child that closes one and holds the other open.
    open_pipes = 2
    try:
        while open_pipes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Whatever is still sitting in the queue at this instant goes unpublished, and that
                # is deliberate rather than overlooked. Draining it first has no bound — the child
                # is alive and still writing, which is exactly why the deadline fired — so a
                # "publish the rest" step would let a chatty wedged child extend the very budget it
                # is being killed for. The loop dequeues continuously, so the backlog here is a
                # burst's worth at most, the task fails either way, and the session id was captured
                # from the opening record long before this.
                raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)
            try:
                item = queued.get(timeout=remaining)
            except queue.Empty:
                raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout) from None
            if item is _EOF:
                open_pipes -= 1
                continue
            channel, line = item
            if channel is _STDOUT:
                lines.add(line)
                reporter.stdout(line)
            else:
                reporter.stderr(line)

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
        # `!= 0`, never `> 0`: a killed child carries a NEGATIVE code (`-9` for SIGKILL), and `> 0`
        # would report the one outcome this seam exists to catch — a task whose agent was killed —
        # as a success (ADR 0032 D-e).
        raise _ChildFailed(proc.returncode, stderr_lines.text())
    return proc.returncode, lines.text()


class _ChildFailed(Exception):
    """A non-zero exit, carrying the stderr the caller reports. Internal to this module."""

    def __init__(self, returncode: int, stderr: str) -> None:
        super().__init__(f"exited {returncode}")
        self.returncode = returncode
        self.stderr = stderr


def run_task(job: dict[str, Any],
             publish: Callable[..., None] | None = None,
             on_spawn: Callable[[subprocess.Popen], None] | None = None) -> TaskOutcome:
    """Run one task's agent and return how it went.

    The contract that matters is this one: **every** failure mode returns a `failed` outcome.
    Nothing raises out of here, because a raise would be indistinguishable from a bug in the loop
    itself — and the loop's job is to report a result, always.

    The terminal state comes from the CHILD'S EXIT, never from an in-agent hook: a hook does not run
    when the process is killed, and an agent executing an arbitrary user prompt is not a trustworthy
    executor of its own completion protocol (ADR 0032 D-e). A non-zero exit — including the negative
    code a killed child carries — is a failure. The agent's own `is_error` is *also* believed, but
    only in that direction: it can fail a run that exited 0, never pass one that did not.

    `publish` is optional: the child is the point and the stream is an observer, so a caller with no
    channel wired runs a task exactly as before. `on_spawn` hands the caller the child's `Popen` for
    issue 07's lease renewal.
    """
    # Built first so every `return` below can go through `_failed`, which is what makes the scrub
    # and the session id impossible to forget on a path someone adds later.
    translator = task_stream.StreamTranslator()

    prompt = job.get("prompt")
    if not isinstance(prompt, str):
        # The job dict came off the wire; a missing or mistyped prompt is bad input, not a crash.
        return _failed(translator, f"task has no usable prompt (got {type(prompt).__name__})")

    sink = publish if publish is not None else _no_publish
    timeout = task_timeout()
    try:
        workspace = task_agent.ensure_workspace(
            task_agent.workspace_for(str(job.get("project_id") or "")))
        argv = task_agent.agent_argv(task_agent.resolve_binary(), prompt)
    except (Exception, SystemExit) as exc:
        # Nothing was spawned, so there is no session id and no output — only a reason, and it is
        # one an operator can act on ("Claude Code isn't installed", "/var/grid is not writable").
        return _failed(translator, f"could not start the agent: {exc}")

    try:
        _returncode, _raw = _run_child(
            argv, timeout=timeout, publish=sink, cwd=str(workspace),
            env=task_agent.child_env(), translator=translator, on_spawn=on_spawn)
    except subprocess.TimeoutExpired:
        return _failed(translator, f"task timed out after {timeout:.0f}s")
    except _ChildFailed as exc:
        return _failed(translator, f"the agent exited {exc.returncode}: {exc.stderr[-500:].strip()}")
    except (Exception, SystemExit) as exc:
        return _failed(translator, f"could not run the agent: {exc}")

    if translator.is_error:
        # Exit 0 and the agent saying it did not finish (`error_max_turns`,
        # `error_during_execution`). Believed, because it only ever fails a run that would otherwise
        # have been reported as a success the agent itself disclaims.
        return _failed(translator, f"the agent reported {translator.subtype or 'an error'}")
    return TaskOutcome(
        "completed", _output(translator), None, session_id=translator.session_id)


def _failed(translator: task_stream.StreamTranslator, error: str) -> TaskOutcome:
    """The one constructor for every failed outcome `run_task` returns.

    One place, so the two things that must be true of a failure are true of ALL of them — including
    a message some later issue adds, without anyone having to remember:

      * **It is scrubbed.** The message is built from text nobody here controls: the child's stderr,
        or an exception's own words. It travels to the relay, is stored on the task row, and is
        written verbatim into the durable `task.terminal` event the requesting user reads back.
      * **It still carries what the run produced** — the session id especially. A run that timed out
        or was killed still opened a conversation, and issue 06 resumes the PROJECT's session, so
        dropping it here would strand every later task on that project.
    """
    return TaskOutcome(
        "failed", _output(translator), task_stream.redact(error),
        session_id=translator.session_id)


def _output(translator: task_stream.StreamTranslator) -> str | None:
    """The agent's final message, bounded to what the relay stores."""
    text = translator.result_text
    return text[:_TASK_OUTPUT_MAX_CHARS] if isinstance(text, str) else None


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
        outcome = run_task(job, publisher.publish)
    except (Exception, SystemExit) as exc:
        # `run_task` is written not to raise; if it ever does, the task still owes the relay a
        # terminal report — silence would hold the project's lock until the lease expires.
        outcome = TaskOutcome("failed", None, f"task runner raised: {exc!r}")
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
            report_once(state, task_id, state=outcome.state, output=outcome.output,
                        error=outcome.error, session_id=outcome.session_id)
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
