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
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import Any, Callable

from . import (relay, task_agent, task_capacity, task_codex, task_codex_proxy, task_evict,
               task_repo, task_stream)
from shared.runtime_identity import grid_runtime_identity

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
#
# Since ADR 0033 D-k (issue 18) the two line up more closely than they used to, and nothing here had
# to change for that. `task_deadline_seconds` is now the RUN budget and starts at the claim, so this
# 3600 is measured against the same span rather than against an hour a task may already have spent
# waiting in a queue — which is what used to make a long wait look, from here, like an agent that
# had barely started before the relay gave up on it.
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
# How many CONSECUTIVE 403s from the claim endpoint mean "this relay has decided" rather than
# "something transient". Every 403 that route can answer is a permanent configuration fact — the
# caller is not a registered provider, its token carries no node id, its scope is missing, or (ADR
# 0033 D-f, issue 24) this grid does not serve its account's email domain — so asking again cannot
# change any of them.
#
# Before this, a 403 fell into the generic branch and was treated exactly like a 500: warn, back off
# five seconds, ask again, for the life of the process. That is one stderr line every five seconds
# saying the same thing, which is the shape of a message nobody reads.
#
# Keyed on the STATUS, never on a parsed refusal `code`. The status already carries the whole
# verdict, so this stays clear of the rule that `remote/task_lease.CANCELLED_CODE` is the ONE parsed
# `detail` this provider reads — there, the status genuinely could not express the distinction; here
# it can, and a second reader of a `detail` body would be a second thing a reworded relay breaks.
#
# Its OWN counter, not the 404 one: a provider alternating between the two would otherwise retire on
# a pair of unrelated blips neither of which was persistent.
_REFUSED_CLAIM_403_BUDGET = 3
# What brings task serving back, appended to every retirement a serve child SURVIVES (dev-VM finding
# G-02). Retiring is a one-way door and nothing said so: `tasks_stop` is set in eight places and
# cleared in none, and `_ServeState` is built once per serve child — so repairing the cause does not
# re-arm anything. Every retirement that carries this is a condition somebody fixes: a served-domain
# allowlist, a role, a relay that gains its tasks plane, a credential.
#
# ⚠️ It names BOTH commands because the obvious shortcut is a no-op, measured on the dev VM: a bare
# `grid join` against a live child answers "Already serving on <grid>; nothing to append." and leaves
# the pid untouched. An operator whose grid has just been fixed is otherwise told everything is fine
# while the whole fleet stays retired — the same shape as the `queue_expired` message that sent a
# member to a `grid project status` which said nothing (G-01).
#
# Deliberately NOT on the two `_start_task_workers` failures or on `_serve_loop`'s teardown. Those
# print while the operator is watching `grid join`, or while the child is on its way out; this is for
# the retirements that happen hours later, unattended, into a log.
RESUME_HINT = ("This stays off until the serve child restarts — fixing the cause is not enough. "
               "Once it is: `grid leave <grid>` then `grid join <grid> …` (a bare re-join answers "
               "\"Already serving\" and changes nothing).")
# Attempts to land a terminal report before giving up. Worth retrying at all because this is the one
# loss that is salvageable cheaply: the child already ran, so only the last message went missing, and
# a report that lands here saves a whole second attempt on another provider. Bounded and small — a
# provider that cannot reach the relay at all is not going to be rescued by a fourth try, the lease
# reclaim recovers the task anyway, and the loop still owes its attention to the next one.
_REPORT_ATTEMPTS = 3
_REPORT_BACKOFF_SECONDS = 2.0
# LOCKSTEP with the relay's `_MAX_SESSION_RESET_REASON_CHARS` (grid-src `private_server/tasks.py`).
# Bounded on THIS side too, not only server-side, because the relay's answer to an over-long reason
# is a 422 that refuses the entire terminal report — so an auxiliary diagnostic could cost a task
# every one of its attempts. Same reasoning as `task_events.MAX_EVENT_BYTES`.
_MAX_SESSION_RESET_REASON_CHARS = 2_000
# LOCKSTEP with grid-src's private_server/task_claim.py. This is a credential-sized opaque
# generation, not unbounded task metadata.
_MAX_CLAIM_ID_BYTES = 200

# How many unresolved paths the failure SENTENCE names before it stops listing them. A merge across
# a large rename conflicts in hundreds of files, and this string travels into the task's `error`
# column and into a terminal report the relay bounds at `_MAX_RESULT_CHARS` — an unbounded list
# would be refused with a 422, which rejects the WHOLE report and leaves the task to be reclaimed
# and re-run into the identical refusal.
_MAX_UNRESOLVED_NAMED = 10


def _warn(message: str) -> None:
    print(f"\n[tasks] {message}", file=sys.stderr)


@dataclass(frozen=True)
class TaskOutcome:
    """What one task run produced. A record rather than a tuple — it grew a fourth field the moment
    a real agent ran, and a fifth when the result started coming back through git."""

    state: str
    output: str | None
    error: str | None
    #: The Claude Code conversation this run opened, so the project's next task resumes it (issue 06).
    session_id: str | None = None
    #: The commit the pushed task branch ended on. Set for EVERY terminal outcome, success and
    #: failure alike (ADR 0032 D-e) — only the relay decides whether `main` follows it.
    result_commit: str | None = None
    #: The exact tip published to this conversation's transcript side ref. Unlike
    #: ``result_commit``, this preserves the agent's resumable history rather than its files.
    transcript_result_commit: str | None = None
    #: Why this run started a FRESH conversation instead of continuing the project's (issue 07).
    #: Reported to the relay so it lands on the task row and in the durable log: the matching
    #: `task.session_reset` progress event travels through a publisher that latches off permanently
    #: on a 403/404, so it is the one disclosure that could not be relied on to arrive.
    session_reset_reason: str | None = None
    #: Native Codex Goal checkpoint. Present only on a successfully completed Goal slice.
    goal_status: str | None = None
    goal_turns_completed: int | None = None
    goal_tokens_used: int | None = None
    goal_time_used_seconds: int | None = None
    #: True only when this outcome describes the local/native harness rather than the Goal's
    #: verdict. A durable Goal must not become terminal because one machine's Codex app-server or
    #: Claude process crashed after spawn; the supervisor preserves what it can and lets the lease
    #: reaper hand the same turn to another capable node. Kept last so every historical positional
    #: construction of ``TaskOutcome`` retains its meaning.
    retryable: bool = False


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


def claim_once(state: Any, *,
               excluded_agent_kinds: tuple[str, ...] = ()) -> dict[str, Any] | None:
    """One claim long-poll; on 401 refresh the token and retry exactly once.

    Mirrors `serve.poll_once` — the same credential, the same single-retry rule, a different queue.

    ``excluded_agent_kinds`` is local capacity, not relay policy. In particular, Claude's own
    subscription can be spent while Codex remains able to run against Grid inference. Reapply the
    exclusion after credential refresh so a long refresh cannot accidentally re-advertise the
    harness this process already knows is unavailable.
    """
    excluded = frozenset(excluded_agent_kinds)

    def available_profiles() -> tuple[dict[str, Any], ...]:
        return tuple(profile for profile in _agent_profiles()
                     if profile.get("kind") not in excluded)

    profiles = available_profiles()
    # The relay intentionally rejects an empty profile list. Empty here means this node can execute
    # no configured harness, so a claim could only produce a permanent 422 and a noisy retry loop.
    # Fail closed without spending an attempt or learning anything about the queue.
    if not profiles:
        return None
    token = state.token()
    try:
        return relay.claim_task(
            state.signaling_url, token,
            agent_kinds=tuple(profile["kind"] for profile in profiles),
            agent_profiles=profiles)
    except relay.RelayUnauthorized:
        if state.refresh(stale_token=token):
            # Capabilities are scheduling authority. Re-read them rather than replaying a stale
            # advertisement after a potentially long credential refresh.
            profiles = available_profiles()
            if not profiles:
                return None
            return relay.claim_task(
                state.signaling_url, state.token(),
                agent_kinds=tuple(profile["kind"] for profile in profiles),
                agent_profiles=profiles)
        raise


def report_once(serve_state: Any, task_id: str, *, state: str, output: str | None,
                error: str | None, session_id: str | None = None,
                result_commit: str | None = None,
                transcript_result_commit: str | None = None,
                session_reset_reason: str | None = None,
                goal_status: str | None = None,
                goal_turns_completed: int | None = None,
                goal_tokens_used: int | None = None,
                goal_time_used_seconds: int | None = None,
                claim_id: str | None = None) -> None:
    """Report a terminal result; on 401 refresh the token and retry exactly once.

    The serve state is named `serve_state` here only because `state` is the wire's name for the
    task's terminal state, and the wire vocabulary wins on a function whose whole job is to send it.
    """
    token = serve_state.token()
    claim = ({"claim_id": claim_id} if claim_id else {})
    try:
        relay.report_task_result(
            serve_state.signaling_url, token, task_id,
            state=state, output=output, error=error, session_id=session_id,
            result_commit=result_commit, session_reset_reason=session_reset_reason,
            transcript_result_commit=transcript_result_commit,
            goal_status=goal_status, goal_turns_completed=goal_turns_completed,
            goal_tokens_used=goal_tokens_used,
            goal_time_used_seconds=goal_time_used_seconds, **claim)
    except relay.RelayUnauthorized:
        if not serve_state.refresh(stale_token=token):
            raise
        relay.report_task_result(
            serve_state.signaling_url, serve_state.token(), task_id,
            state=state, output=output, error=error, session_id=session_id,
            result_commit=result_commit, session_reset_reason=session_reset_reason,
            transcript_result_commit=transcript_result_commit,
            goal_status=goal_status, goal_turns_completed=goal_turns_completed,
            goal_tokens_used=goal_tokens_used,
            goal_time_used_seconds=goal_time_used_seconds, **claim)


def checkpoint_retry_once(serve_state: Any, task_id: str, *,
                          claim_id: str | None = None,
                          **checkpoint: Any) -> dict[str, Any]:
    """Hand off a native Goal checkpoint; refresh an expired provider token exactly once.

    Goal slices can outlive an access token. Losing coherent partial work merely because the token
    expired between the last lease beat and this request would turn an immediate handoff into a
    lease-expiry rollback. Claims, events, lease renewal, and terminal reports already use this
    one-refresh rule; checkpoint settlement must not be the lone stale-token gap.
    """
    token = serve_state.token()
    claim = ({"claim_id": claim_id} if claim_id else {})
    try:
        return relay.checkpoint_task_retry(
            serve_state.signaling_url, token, task_id, **claim, **checkpoint)
    except relay.RelayUnauthorized:
        if not serve_state.refresh(stale_token=token):
            raise
        return relay.checkpoint_task_retry(
            serve_state.signaling_url, serve_state.token(), task_id,
            **claim, **checkpoint)


def decline_claim_once(serve_state: Any, task_id: str, *, attempt: int,
                       claim_id: str) -> dict[str, Any]:
    """Return one unstarted stale Goal claim; refresh a rotated credential exactly once."""
    token = serve_state.token()
    try:
        return relay.decline_task_claim(
            serve_state.signaling_url, token, task_id,
            attempt=attempt, claim_id=claim_id)
    except relay.RelayUnauthorized:
        if not serve_state.refresh(stale_token=token):
            raise
        return relay.decline_task_claim(
            serve_state.signaling_url, serve_state.token(), task_id,
            attempt=attempt, claim_id=claim_id)


def _configured_agent_kinds() -> tuple[str, ...]:
    """Supported harnesses the operator enabled, independent of temporary binary availability."""
    configured = (os.getenv("GRID_TASK_AGENT_KINDS") or "claude,codex").replace(",", " ").split()
    allowed = {kind for kind in configured if kind in ("claude", "codex")}
    invalid = [kind for kind in configured if kind not in ("claude", "codex")]
    for kind in invalid:
        _warn(f"ignoring unsupported harness {kind!r} in GRID_TASK_AGENT_KINDS")
    # Empty or wholly invalid is fail closed: this provider claims no tasks instead of running a
    # harness its operator meant to disable.
    return tuple(kind for kind in ("claude", "codex") if kind in allowed)


def _agent_kinds() -> tuple[str, ...]:
    """Harnesses this process can really execute; never advertise either one optimistically."""
    configured = _configured_agent_kinds()
    kinds = ["claude"] if "claude" in configured and task_agent.claude_available() else []
    if "codex" in configured and task_codex.available():
        kinds.append("codex")
    return tuple(kinds)


_CAPABILITY = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_CODEX_ONLY_GOAL_CAPABILITIES = frozenset({
    "dynamic_tools", "subgoals", "image_generation",
})


def _declared_capabilities(env_name: str) -> set[str]:
    """Operator-declared harness features. Invalid names fail closed and are said locally."""
    answer: set[str] = set()
    for value in (os.getenv(env_name) or "").replace(",", " ").split():
        if _CAPABILITY.fullmatch(value):
            answer.add(value)
        else:
            _warn(f"ignoring invalid Goal capability {value!r} in {env_name}")
    return answer


def _agent_profiles() -> tuple[dict[str, Any], ...]:
    """Capabilities are harness-scoped; a binary alone proves no privileged integration."""
    kinds = _agent_kinds()
    profiles: list[dict[str, Any]] = []
    if "claude" in kinds:
        claude_capabilities = _declared_capabilities("GRID_CLAUDE_TASK_CAPABILITIES")
        # These names describe concrete Grid runner wiring, not an operator assertion. Claude has
        # native `/goal`, but this provider does not inject arbitrary Goal HTTP tools or the built-in
        # child-spawn action into Claude Code. Letting an env var advertise either would schedule a
        # Goal onto a harness that cannot perform its required actions and strand it mid-run.
        claude_capabilities.difference_update({
            "native_goal", *_CODEX_ONLY_GOAL_CAPABILITIES,
        })
        if task_agent.distributed_goal_available():
            claude_capabilities.add("native_goal")
        profile: dict[str, Any] = {
            "kind": "claude", "capabilities": sorted(claude_capabilities)}
        if "native_goal" in claude_capabilities:
            # Runtime provenance rides beside capability scheduling. Older relays ignore this
            # additive key; a Goal-aware relay snapshots the authenticated node's copy onto the
            # attempt boundary for audit/training attribution.
            profile["version"] = task_agent.distributed_goal_version()
        profiles.append(profile)
    if "codex" in kinds:
        profiles.append({
            "kind": "codex",
            "capabilities": sorted({"native_goal", "dynamic_tools", "subgoals"}
                                   | task_codex.goal_tool_origin_capabilities()
                                   | _declared_capabilities("GRID_CODEX_GOAL_CAPABILITIES")),
            "version": task_codex.distributed_goal_version(),
        })
    return tuple(profiles)


def goal_worker_metadata() -> dict[str, Any]:
    """Bounded Goal runtime metadata for registration and heartbeat snapshots.

    Agent versions come from the same live, revision-keyed probes that authorize claims. An empty
    ``agents`` object deliberately clears stale metadata after both native harnesses disappear.
    """
    agents = {
        str(profile["kind"]): {"version": str(profile["version"])}
        for profile in _agent_profiles()
        if profile.get("kind") in ("codex", "claude") and profile.get("version")
    }
    return {
        "goal_runtime": {
            "schema_version": 1,
            "grid": grid_runtime_identity(),
            "agents": agents,
        }
    }


def has_non_claude_claim_capacity() -> bool:
    """Whether this process can keep claiming while Claude's subscription is paused.

    Kept beside the actual profile builder so heartbeat telemetry cannot invent a second admission
    rule. Today Codex is the independent harness; future harnesses can extend this predicate when
    they have their own capacity signal.
    """
    return any(profile.get("kind") == "codex" for profile in _agent_profiles())


def _claim_supported_now(job: dict[str, Any]) -> bool:
    """Revalidate a delivered Goal against the live harness profile before attempt-start.

    A claim long-poll advertises once and can wait while another worker quarantines the exact Codex
    or Claude executable revision. The relay correctly selected against the request it received,
    but that answer can be obsolete by delivery. Ordinary tasks preserve their established
    behavior; only native Goals have a capability policy and the decline protocol.
    """
    goal = job.get("goal")
    if not isinstance(goal, dict):
        return True
    kind = job.get("agent_kind")
    if kind not in ("claude", "codex"):
        return False
    required = goal.get("required_capabilities", [])
    if (not isinstance(required, list)
            or any(not isinstance(value, str) or not _CAPABILITY.fullmatch(value)
                   for value in required)):
        return False
    needed = {"native_goal", *required}
    for profile in _agent_profiles():
        if profile.get("kind") != kind:
            continue
        capabilities = profile.get("capabilities")
        return isinstance(capabilities, list) and needed <= set(capabilities)
    return False


def _goal_claim_is_fenced(job: dict[str, Any]) -> bool:
    """Whether a delivered native Goal has the exact generation needed on every mutation plane."""
    if not isinstance(job.get("goal"), dict):
        return True
    claim_id = job.get("claim_id")
    return (isinstance(claim_id, str) and bool(claim_id)
            and len(claim_id.encode("utf-8")) <= _MAX_CLAIM_ID_BYTES)


def _decline_stale_goal_claim(state: Any, job: dict[str, Any]) -> None:
    """Best-effort safe release of a claim that must never reach its native harness."""
    task_id = job.get("task_id")
    attempt = job.get("attempt")
    claim_id = job.get("claim_id")
    if (not isinstance(task_id, str) or not task_id
            or not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1
            or not isinstance(claim_id, str) or not claim_id):
        _warn(
            "a delivered Goal claim no longer matches this node's live harness capabilities, but "
            "its relay payload lacks the claim identity needed for an immediate safe decline; no "
            "agent will start and the lease will expire for bounded recovery")
        return
    try:
        decline_claim_once(
            state, task_id, attempt=attempt, claim_id=claim_id)
        _warn(
            f"declined stale Goal claim {task_id} attempt {attempt} before attempt-start; the "
            "relay returned it to the distributed queue without spending retry budget")
    except (Exception, SystemExit) as exc:
        # Safety does not depend on this endpoint being present during a rolling upgrade: the
        # provider still refuses to start. An older relay reclaims the untouched lease through its
        # existing bounded reaper path, at the cost of that old relay counting the delivery.
        _warn(
            f"could not immediately decline stale Goal claim {task_id} attempt {attempt} "
            f"({exc!r}); no agent will start and lease-expiry recovery remains active")


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
    is published from this thread, and the reason is ORDERING rather than safety: the publisher is
    lock-guarded since issue 08 (the lease heartbeat publishes tree snapshots through it too), so a
    direct publish from here would no longer corrupt a batch — it would simply interleave stderr with
    stdout in whichever order two threads happened to reach the lock. The one queue is what keeps the
    task's log in the order the child actually wrote it.
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
               on_spawn: Callable[[subprocess.Popen], None] | None = None,
               stop_when: Callable[[], bool] | None = None) -> tuple[int, str]:
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
    on the child: `task_lease.LeaseRenewer` proves liveness with `poll() is None`, never by reading a
    pid back from a record and signalling it — the hazard ADRs 0020 and 0026 removed from the
    run-record seams and which is not reintroduced here.
    """
    # `stdin=DEVNULL`, never inherited (ADR 0033 D-n). `claude -p` reads a non-TTY stdin as MORE
    # PROMPT — measured while taking issue 23's other measurements, when a heredoc feeding the
    # harness turned up inside the agent's answer and was acted on. A provider started by anything
    # that leaves a pipe on fd 0 would otherwise mix its contents into a prompt written by somebody
    # else, silently, on every task.
    #
    # `errors="replace"`, not the default strict decode: one non-UTF-8 byte from the child would
    # otherwise raise `UnicodeDecodeError` inside the reader thread, where the broad guard swallows
    # it — the thread ends, EOF is queued, and the task reports `completed` having LOST its output
    # (measured: the whole of it, not just the tail). The same fix this repo already applies to
    # `orphan_sweep`'s process-list subprocess, for the same reason.
    proc = subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
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
    intentionally_stopped = False
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
                if (not intentionally_stopped and stop_when is not None and stop_when()
                        and proc.poll() is None):
                    # Claude Code documents Ctrl-C as the way to stop a non-interactive `/goal`
                    # while preserving an active Goal for `--resume`. We send it only AFTER its
                    # evaluator attachment was translated and durably published.
                    intentionally_stopped = True
                    proc.send_signal(signal.SIGINT)
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

    if proc.returncode != 0 and not intentionally_stopped:
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
             on_spawn: Callable[[subprocess.Popen], None] | None = None,
             remote: task_repo.GitRemote | None = None,
             inference: task_codex.GridInference | None = None,
             capacity: Any = None) -> TaskOutcome:
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
    lease renewal (`task_lease`). `remote` is the project's git repository on the relay, used only when
    the claim named an `input_commit` — a relay predating the git plane sends none, and a provider
    talking to one runs in an empty workspace exactly as it did then.

    `capacity` is the provider's subscription gate (issue 09). The signal it feeds on exists ONLY
    inside a running agent's output, so it is wired to the translator here — a run is how this
    provider learns its own pressure. Optional, like `publish`: a caller with no gate wired runs a
    task exactly as it did before the gate existed.
    """
    # Built first so every `return` below can go through `_failed`, which is what makes the scrub
    # and the session id impossible to forget on a path someone adds later.
    translator = task_stream.StreamTranslator(
        on_rate_limit=capacity.observe if capacity is not None else None)
    # Why this run started a fresh conversation, once that is known. Held here and threaded through
    # `failed` below rather than passed at each call site, so a failure path someone adds later
    # carries it without having to remember — the same property `_failed` gives the scrub.
    reset_reason: str | None = None

    def failed(error: str, *, retryable: bool = False) -> TaskOutcome:
        return replace(
            _failed(translator, error, session_reset_reason=reset_reason),
            retryable=retryable,
        )

    prompt = job.get("prompt")
    if not isinstance(prompt, str):
        # The job dict came off the wire; a missing or mistyped prompt is bad input, not a crash.
        return failed(f"task has no usable prompt (got {type(prompt).__name__})")

    agent_kind = str(job.get("agent_kind") or "claude")
    if agent_kind not in ("claude", "codex"):
        return failed(f"the relay requested unsupported agent kind {agent_kind!r}")
    goal = job.get("goal")
    is_claude_goal = agent_kind == "claude" and isinstance(goal, dict)
    if is_claude_goal:
        translator = task_stream.GoalStreamTranslator(
            on_rate_limit=capacity.observe if capacity is not None else None)

    # WHOSE workspace this task runs in (ADR 0033 D-g). Refused rather than defaulted, and this is
    # the one wire field on this path that gets that treatment: a missing key would put the agent in
    # a project-level directory, which changes `transcript_dir_name(cwd)`, which makes that member's
    # conversation permanently unresumable — while the task completes, the push lands, and every
    # other signal reads healthy. A fallback here would be a second path to a conversation, which is
    # exactly what D-g exists to remove.
    #
    # Terminal rather than silent, unlike a failed push: no provider can fix this and retrying finds
    # the same answer, so the user gets the reason now instead of `retries_exhausted` in three lease
    # TTLs with nothing to read. It is also what makes the relay's half fail loudly on a version
    # skew — hence: roll the relay out BEFORE the provider fleet.
    member_key = str(job.get("member_key") or "")
    if not member_key:
        return failed(
            "the relay's claim named no member_key, so this provider cannot tell whose workspace "
            "this task belongs to. It refuses to run rather than share one project-level workspace "
            "between members. Upgrade the relay (ADR 0033 issue 11).")

    # WHICH CONVERSATION this turn continues (ADR 0034 D-c). The second wire field on this path that
    # is refused rather than defaulted, and for the same reason one level down: a missing key would
    # put the agent in the MEMBER's directory, which changes `transcript_dir_name(cwd)`, which makes
    # that conversation permanently unresumable — while the turn completes, the push lands, and
    # every other signal reads healthy. Falling back to the member level would also be the very
    # directory two of a member's conversations must not share.
    #
    # A SEPARATE sentence from the one above, deliberately (D-c). Both mean "upgrade the relay", but
    # they arrive at different releases and name different keys: fused, an operator whose relay is
    # missing this one is sent to check ADR 0033 issue 11, a slice they already deployed.
    #
    # ⚠️ It must be a refusal WRITTEN HERE and not merely the `TypeError` that `workspace_for`'s
    # required third argument would raise below. That crash is caught by the guarded block, arrives
    # as "could not start the agent: workspace_for() missing 1 required positional argument", and
    # hands an operator a Python signature instead of an instruction — while looking, from every
    # state and every path on disk, exactly like this.
    conversation_id = str(job.get("conversation_id") or "")
    if not conversation_id:
        return failed(
            "the relay's claim named no conversation_id, so this provider cannot tell which "
            "conversation this turn continues. It refuses to run rather than share one "
            "member-level workspace between a member's conversations, which would make each of "
            "them unresumable. Upgrade the relay (ADR 0034 issue 38).")

    sink = publish if publish is not None else _no_publish
    timeout = task_timeout()
    try:
        workspace = task_agent.ensure_workspace(
            task_agent.workspace_for(
                str(job.get("project_id") or ""), member_key, conversation_id))
        # The writable cache tree beside it, created in the same guarded block so a provider whose
        # task root is unwritable fails here — as "could not create …" on a task that cost nothing —
        # rather than three minutes later as an `EROFS` inside somebody's `npm install`.
        task_agent.ensure_cache(workspace)
        # And this conversation is the most recently used one from now on, so the next sweep offers
        # somebody else's cold checkout before it offers this one.
        task_evict.touch(workspace)
        # Resolved HERE rather than with the argv below, which is now built after the checkout: a
        # provider with no Claude Code installed must fail before it fetches anything, not after.
        binary = (task_codex.resolve_binary() if agent_kind == "codex"
                  else task_agent.resolve_binary())
        if is_claude_goal:
            # Recheck after the claim as well as in the advertised profile. A binary can be
            # downgraded, replaced or runtime-quarantined while another worker's long poll is open;
            # stale scheduling authority must not start a native Goal it cannot resume.
            task_agent.require_distributed_goal(binary)
        # And the rest of what the argv and the child's environment need, for the same reason: they
        # are built AFTER the checkout and outside these guards, so a provider misconfiguration
        # would arrive as "task runner raised" having already fetched the repository. Here it is an
        # ordinary "could not start the agent: …" naming the variable to change, on a task that cost
        # nothing.
        if agent_kind == "claude":
            task_agent.preflight()
    except (Exception, SystemExit) as exc:
        # Nothing was spawned, so there is no session id and no output — only a reason, and it is
        # one an operator can act on ("Claude Code isn't installed", "/var/grid is not writable").
        return failed(f"could not start the agent: {exc}")

    # BOUND THE DISK (ADR 0034 D-c, issue 50), here and nowhere else. This is the last moment before
    # a working tree is fetched, and a provider that never claims again is spending nothing — so
    # there is no start-up sweep and no background thread to keep alive.
    #
    # ⚠️ **AFTER `ensure_workspace`, and that ordering is the bound.** The sweep counts what is on
    # disk, so run before this conversation's directory exists it under-counts by one and a cap of N
    # keeps N+1 workspaces — off by exactly the one that matters, silently, on every provider.
    # Nothing expensive has happened yet: the directories are empty until `materialize` below.
    #
    # Eviction takes the SAME reservation a worker takes rather than reading a second registry, so
    # "in use" stays one fact with one owner. `keep` covers this call, which in production already
    # holds that reservation (`_run_and_report`) but need not — a direct `run_task` does not.
    task_evict.sweep(
        task_agent.workspace_root(),
        keep=(str(job.get("project_id") or ""), member_key, conversation_id),
        reserve=lambda triple: _reserve_workspace(*triple),
        release=lambda triple: _release_workspace(*triple))

    # BEFORE the spawn, and fatal if it fails. An agent run against input that never arrived
    # produces a confidently wrong result with nothing anywhere indicating why — the precise
    # failure ADR 0032 D-b exists to prevent — so there is no "carry on without it" branch.
    input_commit = job.get("input_commit")
    if input_commit:
        if remote is None:
            # A CALLER bug, and it must not be mistaken for the old-relay degrade below. That
            # degrade is gated on `input_commit` being ABSENT; a truthy commit with no remote
            # would otherwise skip the checkout in silence and spawn the agent against whatever the
            # per-project workspace already held — stale from a prior task, or empty. Issues 05 and
            # 07 both assemble job dicts for this path, so the guard lives in the function rather
            # than in the convention today's only caller happens to follow.
            return failed(
                f"the task names input commit {input_commit} but no git remote was wired")
        try:
            claim = ({"claim_id": remote.claim_id} if remote.claim_id else {})
            task_repo.materialize(
                workspace, url=remote.url, token=remote.live_token(),
                branch=str(job.get("branch") or ""), input_commit=str(input_commit),
                # A MERGE TASK's second ref (ADR 0033 D-e, issue 15), fetched here rather than by
                # the agent — which has no grid credential and must not get one. Empty on every
                # ordinary task and on every claim from a relay that predates integration, which is
                # the pre-integration behaviour rather than a new failure.
                merge_ref=str(job.get("merge_ref") or ""),
                # THIS CONVERSATION's transcript (ADR 0034 D-j, issue 39). The REF is built here
                # from the conversation id rather than taken off the wire — the prefix is the
                # duplicated constant, so there is no name for a proxy to mangle — and the PIN is
                # what the relay decided this turn should resume.
                #
                # *Absent ⇒ nothing is fetched and the agent starts a fresh session*, which is a
                # conversation's first turn and also every claim from a relay predating this key.
                # A fetch that FAILS is an `InputFetchError` and is handled below, which is what
                # keeps "the relay has a transcript and I could not get it" from becoming a silent
                # fresh start.
                transcript_ref=task_repo.transcript_ref(conversation_id),
                transcript_commit=str(job.get("transcript_commit") or ""),
                # A first Goal turn has no pin, but its retry must still discard any Codex DB/WAL
                # files left by a dead attempt on this provider. Ordinary Claude tasks retain the
                # old no-pin compatibility behaviour in `materialize`.
                reset_agent_state=(agent_kind == "codex"), **claim)
        except task_repo.InputFetchError as exc:
            # BEFORE the blanket handler below, and that order is the whole change (ADR 0033 issue
            # 16a, criterion 4). The fetch is the one step whose failure is about this attempt
            # rather than about the task, and `failed` is TERMINAL — so treating it like the rest
            # meant an imported history the relay could not pack in time failed every task in that
            # project instantly, with nothing to retry it.
            #
            # The reason goes out TWICE, to two audiences, and neither is redundant — this is the
            # rule issue 05 established for the failed-push path, which this one now shares:
            #
            #   * to the durable event log, for the person who submitted the task. No terminal
            #     report will carry it, so this is their only copy;
            #   * to the provider's OWN stderr, unconditionally. `TaskEventPublisher` latches off
            #     permanently on a 403/404 and then drops everything in silence — and a lost lease
            #     is both a common cause of a failed fetch AND exactly what silences that channel,
            #     so the run where the reason matters most is the run most likely to lose it.
            #
            # `_supervise_one_task`'s own warning names the PHASE but not the detail; the timeout
            # figure and git's words are here.
            detail = task_stream.redact(str(exc))
            _publish_safely(sink, "task.stderr",
                            text=f"could not fetch the task's input: {detail}")
            _warn(f"task {job.get('task_id')}: could not fetch its input from the relay "
                  f"({detail}) — no terminal state will be reported, so the relay reclaims it "
                  f"and another provider retries.")
            raise
        except (Exception, SystemExit) as exc:
            return failed(f"could not prepare the task's workspace: {exc}")

    if agent_kind == "codex":
        if inference is None:
            return failed("the Codex Goal task has no Grid inference endpoint")
        try:
            result = task_codex.run_slice(
                job, workspace, inference=inference, executable=binary, timeout=timeout,
                publish=sink, on_spawn=on_spawn)
            return TaskOutcome(
                "completed", result.output, None,
                goal_status=result.status,
                goal_turns_completed=result.turns_completed,
                goal_tokens_used=result.tokens_used,
                goal_time_used_seconds=result.time_used_seconds,
            )
        except (task_codex.CodexGoalError, OSError) as exc:
            # Once the native process exists, an app-server exit, protocol fault or local timeout
            # is evidence about this attempt/node, not a terminal verdict about a durable Goal.
            # `_supervise_one_task` knows whether `on_spawn` fired and retries only that case.
            return failed(f"could not run Codex Goal slice: {exc}", retryable=True)

    # AFTER the checkout, for two reasons that both bite: the link's target has to survive
    # `reset --hard`/`clean`, and the transcript this task may resume only exists once the input
    # commit has been materialized. Fatal if it fails — an agent spawned without the link writes its
    # transcript outside the repository, so the conversation is silently lost from that task on.
    try:
        transcript_directory = task_agent.link_transcript(workspace, member_key)
    except (Exception, SystemExit) as exc:
        return failed(f"could not prepare the agent's transcript directory: {exc}")

    resume = task_agent.resumable_session(workspace, job.get("resume_session_id"), member_key)
    transcript_start_bytes = 0
    if resume.session_id:
        try:
            resumed_transcript = task_agent.session_transcript_path(
                transcript_directory, resume.session_id)
            info = resumed_transcript.stat(follow_symlinks=False)
            if stat.S_ISREG(info.st_mode):
                transcript_start_bytes = info.st_size
        except (OSError, ValueError):
            # ``resumable_session`` already made the start/no-start decision. Recovery below will
            # fail closed if the transcript changes between that check and the child exit.
            transcript_start_bytes = 0
    if resume.session_id:
        _publish_safely(sink, "task.session_resumed", session_id=resume.session_id)
    elif resume.reason:
        # The relay named a session and this workspace cannot use it. Starting fresh is correct —
        # `--resume` against a missing transcript fails the whole task — but it must never be
        # silent: from the user's side an agent that has forgotten the project looks like an agent
        # that ignored them. Published to the durable log AND to the provider's own stderr, because
        # the two have different readers.
        _warn(f"task {job.get('task_id')}: starting a fresh session ({resume.reason})")
        _publish_safely(sink, "task.session_reset",
                        requested_session_id=str(job.get("resume_session_id") or ""),
                        reason=resume.reason)
        # THE durable copy (issue 07). Neither of the two lines above can be relied on to reach the
        # person who submitted the task: `_warn` writes to the PROVIDER's log, and `_publish_safely`
        # goes through a publisher that latches off permanently on a 403/404 and then silently drops
        # everything. Carried out on the outcome instead, so it rides the terminal report onto the
        # task row — where `grid task get` shows it long after the run is over.
        #
        # Scrubbed like every other message that leaves this process: the reason is built from a
        # session id that arrived off the wire and from a filesystem error's own words.
        #
        # And BOUNDED, which every other free-text field that leaves here already is. The relay
        # refuses an over-long reason with a 422 — and a 422 rejects the WHOLE terminal report, state
        # and output and all. The report loop treats a 4xx as a verdict, so it would not retry; the
        # task would be left `running`, reclaimed, and retried from scratch by a provider that hits
        # the identical reason and the identical 422. A diagnostic field would have burned every
        # attempt on a task whose agent succeeded every time, and the durable log would blame
        # `lease_expired`. One truncation is worth more than that whole failure mode.
        reset_reason = task_stream.redact(resume.reason)[:_MAX_SESSION_RESET_REASON_CHARS]

    if is_claude_goal:
        assert isinstance(translator, task_stream.GoalStreamTranslator)
        if inference is None:
            return failed("the Claude Goal task has no Grid inference endpoint")
        objective = goal.get("objective")
        done_when = goal.get("done_when")
        model = goal.get("model")
        if any(not isinstance(value, str) or not value.strip()
               for value in (objective, done_when, model)):
            return failed("the Claude Goal metadata is missing objective, done_when, or model")
        native_objective = f"{objective.strip()}\n\nDone when: {done_when.strip()}"
        first_prompt = f"/goal {native_objective}"
        # A Claude harness may join a Goal after Codex (or after an earlier Claude transcript was
        # lost with a dead worker). There is no native Claude session to resume in that case, but
        # the turn prompt still carries Grid's relay-authored handoff: prior work, failed evals and
        # child results. Dropping it here gives the replacement the shared files while hiding the
        # reason this turn exists. The first turn's prompt is exactly the native objective, so do
        # not duplicate it; every continuation gets its handoff inside the newly-created /goal.
        if prompt.strip() != native_objective:
            first_prompt += f"\n\nGrid handoff for this distributed turn:\n{prompt.strip()}"
        argv = task_agent.agent_argv(
            binary, prompt if resume.session_id else first_prompt,
            workspace=workspace, resume=resume.session_id)
        child_env = task_agent.goal_child_env(
            author=task_repo.identity_or_default(
                job.get("author_name"), job.get("author_email")),
            workspace=workspace)
        claim = ({"claim_id": inference.claim_id} if inference.claim_id else {})
        proxy = task_codex_proxy.InferenceProxy(
            inference.relay_base_url, inference.current_token,
            refresh_token=inference.refresh_token,
            turn_id=str(job.get("task_id") or "") or None,
            conversation_id=str(job.get("conversation_id") or "") or None,
            upstream_model=model.strip(),
            **claim)
        # Claude rejects arbitrary company-local model ids before making an HTTP request. Keep its
        # selectors on one current native id; the credential proxy above rewrites every Messages
        # body to the exact immutable Grid model, including /goal's small/fast evaluator
        # attachment. Do not pin a dated id here: Claude Code rejects retired ids locally before
        # the request can reach that rewrite boundary.
        child_env.update({
            "ANTHROPIC_BASE_URL": proxy.anthropic_base_url,
            "ANTHROPIC_AUTH_TOKEN": proxy.child_token,
            # The SDK environment path does not expand CLI aliases such as ``sonnet``. All four
            # selectors deliberately use the same current accepted id because no Anthropic model
            # is actually called; the loopback proxy replaces it with the Goal's Grid model.
            "ANTHROPIC_MODEL": "claude-fable-5",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-fable-5",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-fable-5",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-fable-5",
        })
        child_env.pop("ANTHROPIC_API_KEY", None)
        started = time.monotonic()
        try:
            proxy.start()
            _returncode, _raw = _run_child(
                argv, timeout=timeout, publish=sink, cwd=str(workspace), env=child_env,
                translator=translator, on_spawn=on_spawn,
                # A terminal verdict makes Claude exit itself. An unmet verdict would immediately
                # start another native turn, so Ctrl-C at this exact attachment is Grid's slice.
                stop_when=lambda: (translator.goal_evaluated
                                   and not translator.goal_met
                                   and not translator.goal_impossible))
        except subprocess.TimeoutExpired:
            return failed(f"Claude Goal slice timed out after {timeout:.0f}s", retryable=True)
        except _ChildFailed as exc:
            proxy_detail = f" ({proxy.last_failure})" if proxy.last_failure else ""
            return failed(
                f"Claude Goal exited {exc.returncode}: {exc.stderr[-500:].strip()}"
                f"{proxy_detail}",
                retryable=True,
            )
        except (Exception, SystemExit) as exc:
            return failed(f"could not run Claude Goal slice: {exc}", retryable=True)
        finally:
            try:
                proxy.stop()
            except (Exception, SystemExit):
                pass
        if not translator.goal_evaluated and translator.session_id:
            try:
                transcript_path = task_agent.session_transcript_path(
                    transcript_directory, translator.session_id)
                for event, fields in translator.recover_goal_status(
                        transcript_path, after_bytes=transcript_start_bytes):
                    _publish_safely(sink, event, **fields)
            except (OSError, ValueError):
                # Preserve the established protocol-drift verdict below. The native transcript is
                # agent-writable and may vanish or become unsafe; it is never a reason to trust a
                # completion Grid did not actually read.
                pass
        if translator.goal_protocol_error:
            reason = translator.goal_protocol_error
            task_agent.remember_distributed_goal_failure(binary, reason)
            return failed(reason, retryable=True)
        if not translator.goal_evaluated:
            reason = "Claude Goal exited without a native evaluator checkpoint"
            # Exit zero with no goal_status attachment is deterministic protocol drift: model/API
            # failures take the nonzero/timeout paths above. Stop this exact revision advertising
            # native_goal so another harness/machine receives the retry instead of burning the cap.
            task_agent.remember_distributed_goal_failure(binary, reason)
            return failed(reason, retryable=True)

        def goal_counter(name: str) -> int:
            value = goal.get(name)
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

        slice_tokens = max(translator.observed_tokens, translator.goal_tokens or 0)
        status = ("failed" if translator.goal_impossible else
                  "complete" if translator.goal_met else "active")
        total_tokens = goal_counter("tokens_used") + slice_tokens
        token_budget = goal.get("token_budget")
        if (status == "active" and isinstance(token_budget, int)
                and not isinstance(token_budget, bool) and total_tokens >= token_budget):
            status = "budget_limited"
        output = translator.last_output or translator.result_text or translator.goal_reason
        return TaskOutcome(
            "completed", output[:_TASK_OUTPUT_MAX_CHARS] if output else None, None,
            session_id=translator.session_id, session_reset_reason=reset_reason,
            goal_status=status,
            goal_turns_completed=goal_counter("turns_completed") + 1,
            goal_tokens_used=total_tokens,
            goal_time_used_seconds=(goal_counter("time_used_seconds")
                                    + max(1, int(time.monotonic() - started))),
        )

    # The workspace goes in because the confinement policy is built around it: it is the one
    # directory the agent must be able to read and write while `$HOME` around it is denied.
    argv = task_agent.agent_argv(binary, prompt, workspace=workspace, resume=resume.session_id)

    try:
        _returncode, _raw = _run_child(
            argv, timeout=timeout, publish=sink, cwd=str(workspace),
            # WHO a commit the AGENT makes is authored by (ADR 0033 D-m). Only reachable since
            # issue 15 — a merge task's prompt is the first thing that ever asked an agent to
            # commit — and it must be the SAME identity `_push_result` gives the provider's own
            # commit, or one merge would name the member and the next a hostname.
            env=task_agent.child_env(
                author=task_repo.identity_or_default(
                    job.get("author_name"), job.get("author_email")),
                # Same workspace the confinement policy was built around, so the cache variables
                # name the one directory beside it that the policy made writable.
                workspace=workspace),
            translator=translator, on_spawn=on_spawn)
    except subprocess.TimeoutExpired:
        return failed(f"task timed out after {timeout:.0f}s")
    except _ChildFailed as exc:
        return failed(f"the agent exited {exc.returncode}: {exc.stderr[-500:].strip()}")
    except (Exception, SystemExit) as exc:
        return failed(f"could not run the agent: {exc}")

    if translator.is_error:
        # Exit 0 and the agent saying it did not finish (`error_max_turns`,
        # `error_during_execution`). Believed, because it only ever fails a run that would otherwise
        # have been reported as a success the agent itself disclaims.
        return failed(f"the agent reported {translator.subtype or 'an error'}")
    return TaskOutcome(
        "completed", _output(translator), None, session_id=translator.session_id,
        session_reset_reason=reset_reason)


def _publish_safely(sink: Callable[..., None], event_type: str, **fields: Any) -> None:
    """Publish a progress event that must never be able to fail the task.

    The same rule `_Reporter._emit` applies, and for the same reason: the publisher is documented
    never to raise, but `run_task`'s contract cannot rest on another module keeping a promise. These
    two calls sit BEFORE the spawn, where a raise would not merely lose an event — it would unwind
    past the agent, past the push, and report "task runner raised: ..." for a task that never ran.
    """
    try:
        sink(event_type, **fields)
    except (Exception, SystemExit) as exc:
        _warn(f"the event publisher raised on {event_type} ({exc!r}) — it is documented never to")


def _failed(translator: task_stream.StreamTranslator, error: str, *,
            session_reset_reason: str | None = None) -> TaskOutcome:
    """The one constructor for every failed outcome `run_task` returns.

    One place, so the two things that must be true of a failure are true of ALL of them — including
    a message some later issue adds, without anyone having to remember:

      * **It is scrubbed.** The message is built from text nobody here controls: the child's stderr,
        or an exception's own words. It travels to the relay, is stored on the task row, and is
        written verbatim into the durable `task.terminal` event the requesting user reads back.
      * **It still carries what the run produced** — the session id especially. A run that timed out
        or was killed still opened a conversation, and issue 06 resumes the PROJECT's session, so
        dropping it here would strand every later task on that project. The same applies to
        `session_reset_reason`: a run that started a fresh conversation and THEN failed is exactly
        the run whose owner most needs to know the project's history was not in it.
    """
    return TaskOutcome(
        "failed", _output(translator), task_stream.redact(error),
        session_id=translator.session_id, session_reset_reason=session_reset_reason)


def _output(translator: task_stream.StreamTranslator) -> str | None:
    """The agent's final message, bounded to what the relay stores."""
    text = translator.result_text
    return text[:_TASK_OUTPUT_MAX_CHARS] if isinstance(text, str) else None


def _no_publish(*_args: Any, **_kwargs: Any) -> None:
    """The default sink: a task with no channel attached still runs."""


def task_loop(state: Any, capacity: Any = None) -> None:
    """Claim, run, report — until the engine stops or the task plane retires.

    Retiring (`tasks_stop`) rather than stopping (`state.stop`) is the whole point: every exit path
    below leaves inference serving. `state.stop` is only ever read here, never written.

    `capacity` is this provider's reading of its own Claude subscription (issue 09), consulted before
    every claim. It defaults to the PROCESS-wide gate, which is what makes several Claude workers
    throttle on one reading of the one subscription they all spend. A mixed worker continues to
    advertise Codex-only capacity because Codex uses Grid inference rather than that Claude window.
    Withdrawing a harness is simply not advertising it at claim time: the queue waits for a provider
    that can run it.
    """
    capacity = capacity if capacity is not None else task_capacity.shared()
    if not _configured_agent_kinds():
        # An empty/invalid policy cannot repair itself when a binary changes. Do not send the relay
        # an invalid empty profile list or hot-spin locally; retire only task serving. A supported
        # configured harness whose binary is temporarily unavailable takes the recoverable suspend
        # path below instead, so installing/replacing it does not require an inference restart.
        _warn("task serving retired because GRID_TASK_AGENT_KINDS enables no supported harness; "
              "choose codex, claude, or both (inference is unaffected)")
        state.tasks_stop.set()
        return
    # Two counters, not one: a provider alternating between a missing plane and a refusal would
    # otherwise retire on a pair of unrelated blips neither of which was persistent.
    consecutive_404s = 0
    consecutive_403s = 0
    agent_claims_suspended = False
    while not (state.stop.is_set() or state.tasks_stop.is_set()):
        pause = _capacity_pause(capacity)
        excluded_agent_kinds: tuple[str, ...] = ()
        if pause > 0:
            # This signal belongs to Claude's subscription, not to the provider process and not to
            # Codex. A mixed worker keeps offering its independent Codex harness instead of turning
            # a vendor-specific refusal into fleet-wide Goal withdrawal.
            codex_configured = "codex" in _configured_agent_kinds()
            codex_available = codex_configured and any(
                profile.get("kind") == "codex" for profile in _agent_profiles())
            if not codex_available:
                # Waiting on `tasks_stop` rather than sleeping, so teardown does not have to sit out
                # the window. A Claude-only policy can wait for the vendor's exact reset. When Codex
                # is configured but temporarily absent/quarantined, recheck locally at the ordinary
                # claim backoff so installing or replacing it does not wait out a five-hour window.
                wait = min(pause, _CLAIM_BACKOFF_SECONDS) if codex_configured else pause
                state.tasks_stop.wait(wait)
                continue
            excluded_agent_kinds = ("claude",)
        try:
            job = (claim_once(state, excluded_agent_kinds=excluded_agent_kinds)
                   if excluded_agent_kinds else claim_once(state))
        except relay.RelayUnauthorized:
            _warn("relay rejected the token and refresh is unavailable — task serving retired "
                  f"(inference is unaffected). {RESUME_HINT}")
            state.tasks_stop.set()
            return
        except relay.RelayError as exc:
            if getattr(exc, "status", None) == 404:
                consecutive_404s += 1
                # Symmetric to the `consecutive_404s = 0` below, and it has to live INSIDE this
                # branch rather than beneath it: every path out of here `continue`s or returns, so
                # the resets below are unreachable from a 404. Without it the clearing was
                # one-directional and "consecutive" was a lie on one side — `403, 404, 403, 404,
                # 403` retired the provider announcing three consecutive 403s with no two adjacent.
                consecutive_403s = 0
                if consecutive_404s >= _MISSING_PLANE_404_BUDGET:
                    # Persistently absent, so retrying cannot make the endpoint appear. Retire
                    # rather than log the same 404 every few seconds for the life of the process.
                    _warn(f"this grid's relay has no tasks plane ({consecutive_404s} consecutive "
                          f"404s) — task serving retired. {RESUME_HINT}")
                    state.tasks_stop.set()
                    return
                _warn(f"claim 404 ({consecutive_404s}/{_MISSING_PLANE_404_BUDGET}); retrying — a "
                      "relay mid-respawn answers this too")
                state.tasks_stop.wait(_CLAIM_BACKOFF_SECONDS)
                continue
            consecutive_404s = 0
            if getattr(exc, "status", None) == 403:
                consecutive_403s += 1
                if consecutive_403s >= _REFUSED_CLAIM_403_BUDGET:
                    # A decision, not a fault. Retiring says it once, with the relay's own words —
                    # only the relay knows WHICH refusal, and a domain refusal and a role refusal
                    # want different things done about them.
                    _warn(f"this grid's relay refuses this provider's claims "
                          f"({consecutive_403s} consecutive 403s) — task serving retired "
                          f"(inference is unaffected): {exc}. {RESUME_HINT}")
                    state.tasks_stop.set()
                    return
                _warn(f"claim 403 ({consecutive_403s}/{_REFUSED_CLAIM_403_BUDGET}); retrying — an "
                      f"intermediary answers this too: {exc}")
                state.tasks_stop.wait(_CLAIM_BACKOFF_SECONDS)
                continue
            consecutive_403s = 0
            _warn(f"claim failed ({exc}); retrying")
            state.tasks_stop.wait(_CLAIM_BACKOFF_SECONDS)
            continue
        except (Exception, SystemExit) as exc:
            # Never let an unexpected claim fault escape into the thread and vanish.
            consecutive_404s = consecutive_403s = 0
            _warn(f"unexpected claim error ({exc!r}); retrying")
            state.tasks_stop.wait(_CLAIM_BACKOFF_SECONDS)
            continue

        consecutive_404s = consecutive_403s = 0
        if job is None:  # 204 — nothing queued, or the local profile disappeared before the call
            # Agent availability is deliberately re-read for every claim.  It can also disappear
            # DURING the life of this loop: Codex runtime protocol drift quarantines that executable,
            # an operator can replace/remove a binary, or its permissions can change.  In a
            # Codex-only process, claim_once then returns None without touching the relay.  Treating
            # that as an ordinary 204 would hot-spin forever because there is no network long-poll
            # left to pace the loop.  Suspend and recheck instead: a repaired/replaced executable has
            # a new stat revision, clears the quarantine naturally, and rejoins without taking down
            # inference or requiring a process restart.
            if not _agent_profiles():
                if not agent_claims_suspended:
                    _warn("task claims suspended because this node no longer has a runnable "
                          "configured agent harness; rechecking after backoff (inference is "
                          "unaffected)")
                agent_claims_suspended = True
                state.tasks_stop.wait(_CLAIM_BACKOFF_SECONDS)
            else:
                agent_claims_suspended = False
            continue

        agent_claims_suspended = False
        if not _goal_claim_is_fenced(job):
            # Grid Goal has no released node-only protocol to preserve. Starting here would run a
            # native agent whose lease, events, Git, inference, tools, checkpoint and result are all
            # guaranteed to be refused by the relay. Leave the unannounced delivery for bounded
            # lease recovery; it has no valid generation with which a safe decline can be fenced.
            _warn(
                "refusing a delivered Grid Goal with a missing or malformed claim generation; "
                "no checkout or native agent will start, and the untouched lease will expire for "
                "distributed recovery")
            continue
        if not _claim_supported_now(job):
            # The long-poll that returned this job may have been waiting with a capability snapshot
            # from before another worker quarantined the exact harness revision. Recheck BEFORE
            # `_publisher_for` records attempt-start and before checkout, process spawn, or any
            # business action. The relay's decline fence makes this a delivery race rather than a
            # consumed Goal attempt; on an older relay the untouched lease still expires safely.
            _decline_stale_goal_claim(state, job)
            continue
        _run_and_report(state, job, capacity)


def _capacity_pause(capacity: Any) -> float:
    """How long this provider should hold off claiming. `0.0` means claim now. Never raises.

    Guarded, and the guard's direction is the whole point: a throttle that cannot answer must leave
    the provider CLAIMING. The alternative — treating a fault as "no headroom" — is a provider that
    silently withdrew from the fleet over a bug in its own bookkeeping, with the symptom being tasks
    that queue forever and a log that says nothing. A non-number is treated the same way, because a
    gate returning `None` would otherwise raise on the comparison below.
    """
    try:
        pause = capacity.pause_seconds()
        return float(pause) if isinstance(pause, (int, float)) and not isinstance(pause, bool) else 0.0
    except (Exception, SystemExit) as exc:
        _warn(f"the task capacity gate raised ({exc!r}) — it is documented never to. This provider "
              f"keeps claiming tasks: an unreadable limit is not evidence of one.")
        return 0.0


def _run_and_report(state: Any, job: dict[str, Any], capacity: Any = None) -> None:
    """One claimed task, start to terminal report. Guarded so no single task can end the loop.

    The workspace reservation around it exists because a provider may now run several turns at once
    (`GRID_MAX_TASKS`). A workspace belongs to a **(project, member, conversation) triple** and
    persists between that conversation's turns, and preparing one runs `reset --hard` and `clean`
    across it — so two supervisors inside one workspace is not a confusing log, it is one agent's
    work being deleted underneath it mid-run.

    Keyed on the PAIR since ADR 0033 D-g and on the TRIPLE since ADR 0034 D-c, and each step was a
    correction rather than a tidy-up. It began keyed on the project alone, justified by the relay's
    `tasks_one_active_per_project` index; each time that index was re-keyed, this had to follow or it
    would refuse the newly-allowed thing — and refuse it with NO terminal report by design: the turn
    sits `running` for a lease TTL, is reclaimed, and can be refused again, reaching
    `retries_exhausted` on a provider that had capacity the whole time. First that was a project's
    second MEMBER; then a member's second CONVERSATION.

    Since ADR 0034 D-b (issue 40) the relay's index is `turns_one_running_per_conversation`, so a
    repeat of the same TRIPLE is exactly what it forbids — the claim skips a turn whose conversation
    is already running one, and the index is the backstop behind that. It is checked here anyway
    because the two failures are not comparable: refusing a turn the relay will hand to someone else
    costs a lease TTL, and being wrong once about the invariant costs the work.
    """
    task_id = str(job.get("task_id") or "")
    if not task_id:
        # The relay always sends one, so this is wire drift. It is already claimed server-side, and
        # with no id there is no way to report it — say so plainly rather than "dropping it".
        _warn(f"claimed a task with no id — it is now stuck `running` on the relay and its project "
              f"is locked until an operator clears it: {job!r}")
        return

    project_id = str(job.get("project_id") or "")
    member_key = str(job.get("member_key") or "")
    conversation_id = str(job.get("conversation_id") or "")
    if not _reserve_workspace(project_id, member_key, conversation_id):
        # DELIBERATELY no terminal report, the same policy as a result that could not be pushed:
        # terminal is the one state nothing retries, and nothing has been done. Left `running`, its
        # lease lapses and the relay hands it to a provider that can actually run it.
        _warn(f"refusing task {task_id}: this provider is already running a turn of conversation "
              f"{conversation_id} in project {project_id} for member {member_key}, and two agents "
              f"in one workspace would destroy each other's work. No terminal state is reported, "
              f"so the task's lease lapses and the relay reclaims it. The relay is supposed to "
              f"make this impossible — if it recurs, its one-running-turn-per-conversation index "
              f"is not holding. A DIFFERENT conversation, or a different member, is fine in this "
              f"project and is not refused here.")
        return
    try:
        _supervise_one_task(state, job, task_id, capacity)
    finally:
        # In a `finally`, and unconditional: a supervisor that raised on its way out must not take
        # the workspace with it, or one bad task locks that conversation out on this provider for
        # the life of the process.
        _release_workspace(project_id, member_key, conversation_id)


# The (project, member, conversation) triples this process is running a turn of, so two workers can
# never share a workspace. A set rather than a lock per triple: the collection is tiny, it is only
# ever touched at the two ends of a turn, and a dict of locks would need its own lock to grow safely
# anyway.
#
# The PAIR since ADR 0033 D-g and the TRIPLE since ADR 0034 D-c, because that is what a workspace is
# — this key must be the one `task_agent.workspace_for` builds a path from, or it guards a directory
# nobody uses. Keyed on the project alone, this refused the second MEMBER's task the moment
# concurrency was switched on; keyed on the pair, it refuses a member's second CONVERSATION the same
# way, which is the failure issue 40 would otherwise walk straight into.
_WORKSPACES_IN_USE: set[tuple[str, str, str]] = set()
_WORKSPACES_LOCK = threading.Lock()


def _reserve_workspace(project_id: str, member_key: str, conversation_id: str) -> bool:
    """Take this conversation's workspace for the caller. False means a worker already has it.

    Two conversations of one member — like two members of one project — reserve two different
    workspaces and never collide, which is the whole of the re-key and is what stops a provider
    refusing the second one into `retries_exhausted`.

    An empty project id, member key or conversation id reserves nothing and always succeeds:
    `run_task` refuses such a job with a readable message of its own, and there is no workspace to
    protect. Reserving `(project_id, member_key, "")` instead would make a SECOND keyless turn
    collide with the first and take the silent no-report path above, replacing a refusal the user
    can read with one they cannot.

    ⚠️ That "always succeeds" arm is only safe because the refusal exists. Add a segment here
    without one in `run_task` and an empty id reserves nothing while `workspace_for` is still handed
    it — two conversations then share one directory with no lock at all, which is worse than the
    collision this function exists to prevent.
    """
    if not project_id or not member_key or not conversation_id:
        return True
    with _WORKSPACES_LOCK:
        if (project_id, member_key, conversation_id) in _WORKSPACES_IN_USE:
            return False
        _WORKSPACES_IN_USE.add((project_id, member_key, conversation_id))
        return True


def _release_workspace(project_id: str, member_key: str, conversation_id: str) -> None:
    """Give the workspace back. `discard`, so releasing twice is not an error."""
    if not project_id or not member_key or not conversation_id:
        return
    with _WORKSPACES_LOCK:
        _WORKSPACES_IN_USE.discard((project_id, member_key, conversation_id))


def _supervise_one_task(state: Any, job: dict[str, Any], task_id: str, capacity: Any) -> None:
    """`_run_and_report`'s body, with this member's workspace on this project already reserved."""
    publisher = _publisher_for(state, task_id, job)
    if (isinstance(job.get("goal"), dict)
            and getattr(publisher, "_goal_attempt_recorded", True) is not True):
        # Training/release evidence must be able to prove which machine actually started a native
        # Goal attempt. Do not run unrecorded work or business actions. The live lease expires and
        # the relay requeues the untouched turn for another capable node.
        _warn(
            f"task {task_id}: the relay did not durably acknowledge the Goal attempt-start "
            "record; not starting the native harness")
        publisher.close()
        return
    remote = _git_remote(state, job)
    landed = True
    retry_handed_off = False
    retry_state: str | None = None
    # Which half of the git round trip gave up, for the operator's log. Two things can now leave a
    # task unreported, and they call for opposite reading: a push that did not land means this
    # provider ran the agent and lost the result, while an input that never arrived means it never
    # started. Naming only the first — as this did — sends an operator hunting for an agent run that
    # never happened.
    abandoned_because = "'s result could not be pushed"
    # Whether the agent was actually SPAWNED — a dict because the callback closes over it.
    # `on_spawn` has been plumbed through `run_task` since issue 03 and had no caller; this
    # is it. It is the only precise answer to "is there anything of the agent's to push",
    # and the checks that look like it (does the workspace exist, is it a git repo) are
    # true for the whole life of a project regardless of what this attempt managed.
    spawned = {"yes": False}
    # The lease this attempt holds, kept alive for exactly as long as this call is working on the
    # task (ADR 0032 D-c). Started BEFORE `run_task` because the pre-spawn checkout is real work
    # whose own ceiling (`task_repo._GIT_NETWORK_TIMEOUT_SECONDS`) far outruns the 120s lease TTL,
    # and closed in the `finally` below so a supervisor that moved on cannot still be vouching for a
    # child it no longer has.
    claim_id = str(job.get("claim_id") or "") or None
    renewer_options: dict[str, Any] = {"on_beat": _tree_beat(job, publisher)}
    if claim_id:
        renewer_options["claim_id"] = claim_id
    renewer = _lease_renewer(state, task_id, **renewer_options)
    try:
        run_kwargs: dict[str, Any] = {
            "remote": remote,
            "on_spawn": lambda proc: _spawned(spawned, renewer, proc),
            "capacity": capacity,
        }
        # Ordinary Claude tasks keep their established call contract. Native Goal turns of either
        # harness use a loopback credential boundary and route their model calls through Grid.
        if str(job.get("agent_kind") or "claude") == "codex" or isinstance(job.get("goal"), dict):
            inference_options: dict[str, Any] = {}
            if claim_id:
                inference_options["claim_id"] = claim_id
            run_kwargs["inference"] = task_codex.GridInference(
                state.signaling_url, state.token,
                lambda stale: state.refresh(stale_token=stale),
                **inference_options)
        outcome = run_task(job, publisher.publish, **run_kwargs)
        is_goal = isinstance(job.get("goal"), dict)
        retry_goal_failure = (is_goal and outcome.state == "failed"
                              and (not spawned["yes"] or outcome.retryable))
        if retry_goal_failure and not spawned["yes"]:
            # A native Goal is durable and has a bounded attempt budget. Failure before the child
            # process exists is evidence about THIS NODE (binary vanished after advertisement,
            # local permissions, socket setup), not about the Goal. Report nothing terminal: the
            # lease reaper hands the same turn to another capable machine and eventually ends it at
            # retries_exhausted if the fault is fleet-wide. Ordinary one-shot tasks retain their
            # established terminal-failure behavior.
            landed = False
            abandoned_because = "'s native Goal harness could not start"
        else:
            outcome, landed = _push_result(job, outcome, spawned["yes"], remote, publisher)
            if retry_goal_failure and landed:
                # The process did start, so `_push_result` publishes its worktree and native
                # history before we relinquish the lease. This is still NOT a terminal report:
                # the relay's bounded reaper moves the same logical turn to another machine. The
                # relay-authenticated event makes the reason and the handoff visible in the Goal's
                # trajectory without trusting the agent to declare its own verdict.
                reason = task_stream.redact(outcome.error or "native Goal harness failed")[
                    :_MAX_SESSION_RESET_REASON_CHARS]
                publisher.publish("task.retrying", reason=reason)
                # Flush the entire trajectory tail while this provider still holds the lease.
                # The retry endpoint below revokes event authority immediately; leaving the normal
                # close in `finally` as the first flush would make the handoff reliably discard the
                # last output/tool events—the exact training evidence this path exists to retain.
                publisher.flush()
                try:
                    retry_answer = checkpoint_retry_once(
                        state, task_id,
                        claim_id=claim_id,
                        reason=reason,
                        result_commit=outcome.result_commit,
                        transcript_result_commit=outcome.transcript_result_commit,
                        session_id=outcome.session_id,
                        session_reset_reason=outcome.session_reset_reason,
                    )
                    retry_handed_off = True
                    retry_state = str(retry_answer.get("state") or "queued")
                except (Exception, SystemExit) as exc:
                    # Additive rollout and ambiguous acknowledgements both land here. Do not turn
                    # a retry-path transport/version fault into a terminal Goal failure: leaving
                    # the row running preserves the established bounded lease-reaper fallback.
                    _warn(f"task {task_id}: could not explicitly hand off its native Goal "
                          f"checkpoint ({exc!r}); falling back to lease-expiry recovery")
                landed = False
                abandoned_because = "'s native Goal harness failed retryably"
    except task_repo.InputFetchError:
        # The input never arrived, so this attempt has produced no evidence about the task at all —
        # not even a failed one. Routed to the same silence a failed push takes: report nothing,
        # let the lease lapse, let the relay's reclaim try another provider (ADR 0033 issue 16a).
        #
        # `landed` is the existing name for "there is nothing in the repository worth reporting",
        # which is exactly true here and for a second reason: nothing was ever pushed. Reusing it
        # keeps ONE no-report path rather than two that have to be kept in step.
        landed = False
        abandoned_because = "'s input could not be fetched from the relay"
        outcome = TaskOutcome("failed", None, None)
    except (Exception, SystemExit) as exc:
        # `run_task` is written not to raise; if it ever does, the task still owes the relay a
        # terminal report — silence would hold the project's lock until the lease expires.
        #
        # SCRUBBED, like every message `_failed` builds, and for the identical reason: this text is
        # made from an exception nobody here controls, and it travels to the relay, onto the task
        # row, and into the durable `task.terminal` event the requesting user reads back. `_failed`
        # itself cannot be reused — it needs the translator that lives inside `run_task`, which is
        # exactly the call that just failed — so the guarantee is restated rather than inherited.
        outcome = TaskOutcome(
            "failed", None, task_stream.redact(f"task runner raised: {exc!r}"))
    finally:
        # Renewal stops FIRST, and before the terminal report below. A renewal still in flight while
        # the state changes underneath it is refused with a 404 that reads as a fault rather than as
        # the race this call just created — and the work it was vouching for is over either way.
        try:
            renewer.close()
        except (Exception, SystemExit) as exc:
            _warn(f"could not stop lease renewal for task {task_id} ({exc!r})")
        # Flush the tail BEFORE reporting terminal: the relay appends `task.terminal` as part of the
        # state change, and a batch arriving after that is refused (the task is no longer running),
        # so the last lines of output would be exactly the ones lost.
        try:
            publisher.close()
        except (Exception, SystemExit) as exc:
            _warn(f"could not flush the last progress events for task {task_id} ({exc!r})")

    if getattr(renewer, "lost", False):
        # The renewer learned, mid-run, that another provider had taken this task over — and it is
        # the ONLY thing that knows why everything after it failed. Without this line the operator
        # sees a bare "could not push the task's result" or a bare 403 on the report, with no
        # indication that the cause was decided minutes earlier and is not their problem to fix.
        _warn(f"task {task_id} was taken over by another provider while this one was still running "
              f"it, so this attempt's work was never going to land. The other provider's attempt is "
              f"the live one; `grid task get {task_id}` follows it.")

    if getattr(renewer, "cancelled", False):
        # A member stopped this task, and this is the ONE no-report path that is not waiting for
        # anything. The relay recorded the terminal state when the cancel landed, and a terminal row
        # is inert: no lease lapse, no reclaim, no second provider, no `retries_exhausted`. Measured
        # on a live cancel — the task ends `failed` / `cancelled` with a single `attempt 1`.
        #
        # Said here rather than left to the message below, which promises that whole cascade. An
        # operator reading it after a cancel waits for a retry the relay will never schedule, and
        # every cancel produced it. The push failure logged just above is expected for the same
        # reason the report is skipped: a cancelled task's lease is gone, so the git fence refuses
        # the result push.
        _warn(f"task {task_id} was cancelled by a project member, so nothing was reported and "
              f"nothing will be retried — the relay recorded the terminal state when the cancel "
              f"landed, and a cancelled task is never reclaimed. Any push failure logged above is "
              f"the same cancel seen from the git fence. `grid task get {task_id}` shows it.")
        return

    if not landed:
        if retry_handed_off:
            if retry_state == "failed":
                _warn(f"task {task_id}'s native Goal harness failed on its final allowed attempt; "
                      "the relay preserved the partial checkpoint for audit and ended the Goal "
                      "with retries_exhausted.")
            else:
                _warn(f"task {task_id}'s native Goal checkpoint was accepted by the relay and the "
                      "same turn is queued immediately for another capable provider; no terminal "
                      "state was reported by this worker.")
            return
        # DELIBERATELY no terminal report. Nothing this attempt produced is in the repository, so
        # reporting one would mark the task terminal with nothing to fetch — and terminal is
        # precisely the state nothing retries. Left `running`, its lease lapses and the relay's
        # reclaim hands it to another provider, which is the only path that can still produce the
        # result the user asked for. The reason has already gone to the user as a `task.stderr`
        # event, from whichever half failed.
        _warn(f"task {task_id} {abandoned_because}, so no terminal state was reported — "
              f"the task is left `running` so its lease can lapse. The relay reclaims it within "
              f"roughly the lease TTL and hands it to another provider, up to the task's retry cap; "
              f"after that it fails with `retries_exhausted` and its project unlocks. "
              f"`grid task get {task_id}` shows where it stands.")
        return

    for attempt in range(1, _REPORT_ATTEMPTS + 1):
        try:
            report_kwargs: dict[str, Any] = {
                "state": outcome.state, "output": outcome.output, "error": outcome.error,
                "session_id": outcome.session_id, "result_commit": outcome.result_commit,
                "session_reset_reason": outcome.session_reset_reason,
            }
            if claim_id:
                report_kwargs["claim_id"] = claim_id
            # Additive rolling upgrade: an older relay ignores this key, while a worker that
            # produced no transcript keeps the exact pre-feature report shape.
            if outcome.transcript_result_commit:
                report_kwargs["transcript_result_commit"] = outcome.transcript_result_commit
            # Preserve the established Claude report shape. Besides compatibility with older
            # relays, omitting absent Goal fields keeps `goal_status: null` from being mistaken for
            # an attempted checkpoint by an intermediary that validates keys rather than values.
            if isinstance(job.get("goal"), dict):
                report_kwargs.update({
                    "goal_status": outcome.goal_status,
                    "goal_turns_completed": outcome.goal_turns_completed,
                    "goal_tokens_used": outcome.goal_tokens_used,
                    "goal_time_used_seconds": outcome.goal_time_used_seconds,
                })
            report_once(state, task_id, **report_kwargs)
            return
        except (Exception, SystemExit) as exc:
            status = getattr(exc, "status", None)
            if status == 404:
                # NOT a loss. 404 means the task is no longer `running` on this provider, and it now
                # has TWO ordinary causes, which is why this says "or" rather than diagnosing:
                #
                #   * an EARLIER attempt of ours landed and only its acknowledgement was lost, or
                #   * the relay reclaimed the task — its lease had lapsed — and it is queued for, or
                #     already running on, another provider. A reclaimed row is `queued` with a NULL
                #     `provider_id`, which `_refuse_unleased` answers 404 to, exactly as it does for
                #     a task that ended.
                #
                # Both need the same thing from this loop (stop, and do not report again) and the
                # same thing from a human (look, do not intervene). Naming only the first would send
                # an operator hunting for a completed task that is in fact still being worked on.
                _warn(f"the relay would not accept a result for task {task_id} (404): either an "
                      f"earlier attempt of ours landed and only its acknowledgement was lost, or "
                      f"the task's lease lapsed and it has been reclaimed for another provider. "
                      f"Either way this attempt is finished and no action is needed — "
                      f"`grid task get {task_id}` says which.")
                return
            if _is_answer_not_blip(status) or attempt == _REPORT_ATTEMPTS:
                # Losing one result must not cost the loop. What happens next is a RETRY, not a
                # stranding: renewal stopped before this loop began, so the lease lapses and the
                # relay reclaims the task for another provider. What is lost is this attempt's work,
                # not the task. Naming the status separates incidents the relay already
                # distinguishes: 403 means another provider already holds the lease and may have run
                # this task too; 404 means it was already terminal (possibly our own earlier attempt,
                # which did land); a bare transport failure means nobody knows.
                _warn(f"could not report task {task_id} after {attempt} attempt(s) "
                      f"(status={status}, {exc}) — this attempt's result is lost. Its lease is no "
                      f"longer being renewed, so the relay reclaims the task and another provider "
                      f"retries it, up to the task's retry cap; `grid task get {task_id}` shows "
                      f"where it stands.")
                return
            _warn(f"report attempt {attempt}/{_REPORT_ATTEMPTS} for task {task_id} failed "
                  f"({exc}); retrying")
            state.tasks_stop.wait(_REPORT_BACKOFF_SECONDS)


def _lease_renewer(state: Any, task_id: str, *,
                   claim_id: str | None = None,
                   on_beat: Callable[[], None] | None = None) -> Any:
    """This attempt's lease renewer, already started. Never raises.

    Guarded for the same reason `_publisher_for` is: a task that runs without renewal is reclaimed
    and retried, which is recoverable, while a task that never runs because its renewer could not be
    constructed is simply lost. The import is local so a fault in that module cannot stop the task
    loop from starting at all.
    """
    try:
        from . import task_lease

        options: dict[str, Any] = {"on_beat": on_beat}
        if claim_id:
            options["claim_id"] = claim_id
        renewer = task_lease.LeaseRenewer(state, task_id, **options)
        renewer.start()
        return renewer
    except (Exception, SystemExit) as exc:
        _warn(f"could not set up lease renewal for task {task_id} ({exc!r}); the task still runs, "
              f"but its lease will lapse and another provider may retry it")
        return _NoRenewal()


def _tree_beat(job: dict[str, Any], publisher: Any) -> Callable[[], None] | None:
    """The workspace snapshot this task's heartbeat carries, or `None` if it cannot be set up.

    Built here rather than inside `run_task` because the beat has to exist before the checkout does:
    the renewer starts first, and `LeaseRenewer._beat` is what holds the snapshot back until there is
    an agent to snapshot for. Guarded end to end and returning `None` on any fault, so a task whose
    tree could not be wired runs exactly as it did before issue 08 — the degrade rule this repo
    applies to every optional signal.
    """
    try:
        from . import task_tree

        # The SAME triple `run_task` builds its workspace from (ADR 0033 D-g, ADR 0034 D-c), read
        # off the job again because this runs before `run_task` does. A path that is one level up
        # snapshots a directory the agent is not in, and nothing here would say so: the construction
        # raises, this returns `None`, and the task simply has no live view for its whole run.
        workspace = task_agent.workspace_for(
            str(job.get("project_id") or ""), str(job.get("member_key") or ""),
            str(job.get("conversation_id") or ""))
        return task_tree.WorkspaceTree(workspace, publisher).beat
    except (Exception, SystemExit) as exc:
        _warn(f"could not set up workspace tree snapshots for task {job.get('task_id')} ({exc!r}); "
              f"the task runs normally, but its live file view will be missing")
        return None


class _NoRenewal:
    """The renewer's shape, doing nothing. Lets every caller below stay unconditional."""

    lost = False

    def attach(self, _proc: Any) -> None:
        pass

    def close(self) -> None:
        pass


def _spawned(spawned: dict[str, bool], renewer: Any, proc: subprocess.Popen) -> None:
    """The one `on_spawn` callback, doing both jobs the child's handle is needed for.

    Guarded around the renewer half only: recording that the agent STARTED decides whether the
    result is pushed at all, so it must happen whatever the renewer does with the handle.
    """
    spawned["yes"] = True
    try:
        renewer.attach(proc)
    except (Exception, SystemExit) as exc:
        _warn(f"could not hand the agent's process to lease renewal ({exc!r}); the task runs on, "
              f"but its lease may lapse and another provider may retry it")


def _git_remote(state: Any, job: dict[str, Any]) -> task_repo.GitRemote | None:
    """The project's repository on the relay, or `None` when this claim named no git plane.

    There is no refresh-and-retry here, unlike `claim_once` and `report_once`. git does not answer
    with a status code — a stale credential arrives as prose on stderr, and pattern-matching that to
    decide whether to refresh would be a guess. It does not need to be one: a git failure routes to
    the push-failure path below, the task's lease lapses, and the next attempt claims with a token
    freshly fetched. **The recovery path already exists**, so a second one keyed on parsing git's
    English would add a way to be wrong without adding a way to succeed. The remote DOES read the
    live token before every distinct Git phase, however: lease/event/heartbeat traffic may already
    have refreshed an expired credential during a long agent run, and retaining the claim-time
    string would knowingly discard that successful refresh.
    """
    project_id = str(job.get("project_id") or "")
    if not job.get("input_commit") or not project_id:
        return None
    return task_repo.GitRemote(
        url=relay.git_remote_url(state.signaling_url, project_id), token=state.token(),
        claim_id=str(job.get("claim_id") or "") or None, token_provider=state.token)


def _push_result(job: dict[str, Any], outcome: TaskOutcome, spawned: bool,
                 remote: task_repo.GitRemote | None, publisher: Any) -> tuple[TaskOutcome, bool]:
    """Commit the workspace and push the task branch. Returns `(outcome, it_landed)`.

    Runs for a FAILED outcome too (ADR 0032 D-e): the branch is what lets a user see what the agent
    did before it broke and cherry-pick what was right. Only the relay decides whether `main`
    follows, and only on success.

    **Gated on the agent having been SPAWNED**, which is the precise reading of that rule — "what
    the agent did" presupposes an agent that ran. An earlier version asked whether the workspace was
    a git repo instead, and that is a proxy which fails in the direction that matters: `.git` is
    created by the FIRST step of `materialize`, and the workspace persists per project, so it exists
    for the rest of the project's life. A `materialize` that got as far as `symbolic-ref` and then
    failed at `clean` — leaving a previous task's un-cleanable leftovers in place — would satisfy
    that check, and `commit_and_push` would then succeed, because HEAD already legitimately names
    the task branch. The result: a `result_commit` full of another task's files, published as
    "what the agent did" for a task whose agent never started. That is precisely the confidently
    wrong answer D-b exists to prevent, reached from the other end.
    """
    if remote is None:
        # No git plane on this relay, so there is no branch to push and nothing to explain. Not a
        # failure — it is the pre-issue-04 degrade, and the relay records nothing either.
        return outcome, True
    if not spawned:
        # `run_task` already failed the task for a reason the user can read, and there is nothing of
        # the agent's to preserve. The relay reads the branch tip itself, so the task still settles
        # on its input commit — the truthful answer for an attempt that never ran.
        return outcome, True

    try:
        # The same TRIPLE `run_task` built its workspace from (ADR 0034 D-c), and reached only when
        # the agent was SPAWNED — so `workspace_for` has already accepted all three segments once on
        # this path, and a hostile one would have failed the task before any of this ran. Built one
        # level short, this commits from a worktree the agent never touched.
        member_key = str(job.get("member_key") or "")
        conversation_id = str(job.get("conversation_id") or "")
        workspace = task_agent.workspace_for(
            str(job.get("project_id") or ""), member_key, conversation_id)
        # THE CONVERSATION FIRST, and the order is a decision (ADR 0034 D-j, issue 39).
        #
        # Both pushes are failable and both are retried the same way, so the order only decides
        # which failure costs less. The transcript is the smaller push and the one whose loss is
        # unrecoverable — the result branch is reset to `input_commit` by the reaper and rebuilt by
        # the retry, while a conversation nobody published is simply gone. Publishing it first also
        # means a turn whose result push fails has still recorded what the agent did.
        #
        # A `PushError` here lands in the same handler as `commit_and_push`'s and produces the same
        # outcome: no terminal report, the lease lapses, the reaper reclaims. That is deliberately
        # NOT a terminal `failed` — D-j says a failed transcript push must fail the turn, and this
        # codebase's word for that is "do not report success and let it be retried", exactly as a
        # failed result push already behaves. A terminal failure would spend the whole turn on a
        # transient network fault.
        claim = ({"claim_id": remote.claim_id} if remote.claim_id else {})
        transcript_result_commit = task_repo.push_transcript(
            workspace, url=remote.url, token=remote.live_token(),
            ref=task_repo.transcript_ref(conversation_id), **claim)
        pushed = task_repo.commit_and_push(
            workspace, url=remote.url, token=remote.live_token(),
            branch=str(job.get("branch") or ""),
            message=f"task {job.get('task_id')} ({outcome.state})",
            # No `transcript=` any more (ADR 0034 D-j, issue 39). The conversation does not travel
            # in this commit; `_push_transcript` above has already put it on its own ref.
            # Who asked for this task (ADR 0033 D-m). Straight off the claim payload and NOT
            # coerced here: `identity_or_default` is the boundary, and it is the one place that
            # knows git's rules — an empty name is a refusal, a NUL never reaches git at all.
            # Neither key present is the pre-0033 `grid <grid@invalid>`, which is exactly what an
            # older relay produces, so this is free to roll out in either direction.
            author=task_repo.identity_or_default(
                job.get("author_name"), job.get("author_email")), **claim)
    except (Exception, SystemExit) as exc:
        # SCRUBBED like every other message that leaves this process: it is built from git's own
        # stderr, and it travels into the durable event log the requesting user reads back.
        reason = task_stream.redact(f"could not push the task's result: {exc}")
        # Logged LOCALLY AND UNCONDITIONALLY, before the event is even attempted, because the event
        # cannot be relied on to carry it. `TaskEventPublisher` is documented "never raises": it
        # buffers, and a batch the relay refuses is dropped inside `flush()` with a generic message.
        # So the case this reason matters most in — the push failed BECAUSE the lease was lost — is
        # exactly the case where the same lost lease silences the channel carrying the explanation.
        # Guarding the publish in a `try` looked like it covered this and could not: nothing raises.
        _warn(f"task {job.get('task_id')}: {reason}")
        publisher.publish("task.stderr", text=reason)
        return outcome, False

    if pushed.unchecked and job.get("merge_ref"):
        # The check could not RUN — a git blip, or `ls-files` past its budget on a large repository.
        # The task still completes: refusing to report a result because a diagnostic failed would
        # lose the agent's whole run over a transient.
        #
        # But it is DISCLOSED, to the person who submitted the task and not only to this host's
        # stderr. Otherwise "this merge was verified" and "nobody looked at this merge" are the same
        # observation everywhere a user can see, which is the shape of the failure this whole slice
        # exists to remove. The same rule `task.wip_not_advanced` follows, and an unknown event type
        # renders verbatim on the client, so it needs no client release to be readable.
        unchecked = task_stream.redact(
            f"this merge task's result could not be checked for unresolved conflicts "
            f"({pushed.unchecked}), so nothing has confirmed the conflict was actually resolved; "
            f"read the diff before promoting")
        _warn(f"task {job.get('task_id')}: {unchecked}")
        publisher.publish("task.merge_unchecked", reason=unchecked)

    if pushed.unresolved and outcome.state == "completed" and job.get("merge_ref"):
        # A MERGE TASK whose agent never resolved the conflict (ADR 0033 D-e, issue 15). It exited
        # 0 and said it was done, and what it left is structurally a perfectly good merge commit —
        # so the relay's ancestry check PASSES and the member's WIP branch fast-forwards onto a tree
        # full of `<<<<<<<`. The unmerged index is the only evidence, and it never leaves this host.
        #
        # **Gated on `merge_ref`, so this is only ever applied to a task the GRID asked to merge.**
        # A merge task's prompt is the relay's own and says "resolve the conflict", so an agent
        # reporting success having resolved nothing is contradicting its instructions. An ordinary
        # task's prompt is the USER's, and "start merging X into Y and leave it for me to look at"
        # is an ordinary thing to ask for — failing that would be the grid overruling the person
        # whose repository it is.
        #
        # The same direction as `translator.is_error`: this can fail a run the agent called a
        # success, and never pass one it did not. Reported AFTER the push, so the work is still on
        # the branch for the user to read and finish by hand — a failed attempt is pushed too
        # (ADR 0032 D-e).
        named = ", ".join(pushed.unresolved[:_MAX_UNRESOLVED_NAMED])
        if len(pushed.unresolved) > _MAX_UNRESOLVED_NAMED:
            named += f" and {len(pushed.unresolved) - _MAX_UNRESOLVED_NAMED} more file(s)"
        # Says INDEX, not markers, and that is the same correction `_unresolved_paths` already
        # carries in code: a **modify/delete** conflict leaves no `<<<<<<<` anywhere — measured on
        # git 2.54.0, git writes the surviving side verbatim and reports the conflict only through
        # the index. Naming markers sent the reader hunting for a string that is not there, in
        # precisely the conflict class this guard exists to catch, and the natural conclusion is
        # that the grid is wrong. It also pointed at the wrong tool: `git status` and
        # `git ls-files --unmerged` answer this, grep does not.
        reason = task_stream.redact(
            f"the agent reported success but left {named} unmerged in git's index; whatever the "
            f"files look like, git still has an unresolved conflict there, so this is not a "
            f"resolved merge")
        _warn(f"task {job.get('task_id')}: {reason}")
        # Everything else on the outcome is KEPT — its output and session id are how somebody works
        # out what the agent was doing when it stopped, and the session id is what the next attempt
        # resumes. Only the verdict changes.
        return replace(outcome, state="failed", error=reason,
                       result_commit=pushed.commit,
                       transcript_result_commit=transcript_result_commit), True

    return replace(outcome, result_commit=pushed.commit,
                   transcript_result_commit=transcript_result_commit), True


def _publisher_for(state: Any, task_id: str, job: dict[str, Any]) -> Any:
    """The task's event channel, opened with `task.attempt_started`.

    Constructed here rather than inside `run_task` so the marker is published even when the child
    never starts: a client watching a claimed task should see WHICH attempt picked it up and where,
    and a task that fails at spawn is exactly when that matters most.

    Never raises. If the channel cannot be opened at all the task still runs — a provider that
    refused to work because it could not narrate would be strictly worse than a silent one.
    """
    from .task_events import TaskEventPublisher

    claim_id = str(job.get("claim_id") or "") or None
    publisher = (TaskEventPublisher(state, task_id, claim_id=claim_id)
                 if claim_id else TaskEventPublisher(state, task_id))
    try:
        is_goal = isinstance(job.get("goal"), dict)
        accepted = False
        for _attempt in range(2 if is_goal else 1):
            accepted = publisher.publish(
                "task.attempt_started",
                # Ordinary task narration stays buffered. Goal attempt identity is a
                # release/training boundary and must be durably acknowledged before the native
                # harness can execute. The relay deduplicates this bounded ambiguity retry.
                _flush=is_goal,
                attempt=job.get("attempt"),
                provider_id=job.get("provider_id"),
                # The relay overwrites this from the claim row, so the durable trajectory does not
                # trust the worker. Sending it keeps provider-first rolling upgrades informative
                # when an older relay stores the event opaquely.
                agent_kind=job.get("agent_kind"),
            )
            if accepted:
                break
        if is_goal:
            publisher._goal_attempt_recorded = accepted is True
    except (Exception, SystemExit) as exc:
        is_goal = isinstance(job.get("goal"), dict)
        action = "the Goal will not start" if is_goal else "running it anyway"
        _warn(f"could not announce the start of task {task_id} ({exc!r}); {action}")
        if is_goal:
            publisher._goal_attempt_recorded = False
    return publisher


def _is_answer_not_blip(status: int | None) -> bool:
    """Whether the relay ANSWERED (so retrying cannot change the outcome) rather than went missing.

    4xx here are verdicts: 403 the lease is someone else's, 404 the task is already terminal, 422 we
    sent something malformed. Retrying any of them just delays the loop. 5xx and a bare transport
    failure (`status is None`) are the opposite — nobody decided anything yet.
    """
    return status is not None and 400 <= status < 500
