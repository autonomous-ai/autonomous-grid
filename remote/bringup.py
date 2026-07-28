"""Bounded, observable bring-up for the detached serve child (ADR 0022).

Bring-up is spawn → first successful registration. Before this module it was silent and had no
deadline of its own: the first unconditional line the child printed came *after* it registered, and
one transient relay failure killed it. A `grid join` issued while the grid's master was mid-respawn
therefore produced a child that sat in a socket for 8+ minutes behind a 0-byte log and never
recovered (PRD follow-up F9).

Everything here is bring-up only. The steady-state poll and heartbeat loops already survive relay
outages and log their retries; they are untouched.
"""
from __future__ import annotations

import sys
import threading
import time
from typing import Any, Callable, Sequence

from . import relay, service_truth


# Capped exponential backoff between registration attempts. Capped rather than unbounded because a
# relay that has been down for an hour should still be rejoined within a minute of coming back, and
# the relay's own node TTL is 120s — waiting longer than that between attempts would mean the grid
# had already given up on us anyway. The last value repeats for as long as the child runs.
_BACKOFF_SCHEDULE: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0)

# The relay understood the request and refused it: a malformed body, a token addressing someone
# else's node, a payload it validated and rejected. None of that changes on a retry, so these stay
# fatal and the child dies naming the reason rather than hiding a misconfiguration behind "starting".
#
# Written as the TERMINAL set rather than a retryable one on purpose (ADR 0022). The regime this
# module exists for is a master mid-respawn, which answers 404 as readily as 503 — routes not yet
# mounted, or a proxy in front of an app that has not started — so an allowlist of retryable statuses
# would have missed the very incident it was written for. Everything not named here retries.
_TERMINAL_STATUSES = frozenset({400, 403, 422})

# An exhausted token gets its own, much longer schedule. It is the one retryable class that cannot
# fix itself, so retrying it like a 503 would be wrong — but killing the child is wrong too:
# `_ServeState.refresh` re-reads `credentials.toml` and adopts a token another PROCESS stored, which
# makes `grid login` on this box a real remedy for a child that is already running, with no re-join.
# It climbs quickly so an operator who acts is picked up in about a minute, then settles low, because
# every attempt that gets past the credential file's own re-read is a control-plane round trip.
_AUTH_BACKOFF_SCHEDULE: tuple[float, ...] = (60.0, 120.0, 300.0)

# `RelayUnauthorized` carries no message — before ADR 0022 this path died printing
# "Remote engine stopped: " with nothing after the colon. The reason is authored here instead, and
# it names the command, because it is the one failure on this path with an operator-side remedy.
_AUTH_REASON = (
    "the relay rejected this grid's access token and it could not be refreshed — "
    "run `grid login` on this machine to renew it"
)


class BringUpStopped(Exception):
    """``stop`` was set while bring-up was backing off — the child is shutting down, not registered."""

    def __init__(self, message: str = "stopped while waiting to register") -> None:
        super().__init__(message)


# One wall clock over the WHOLE capability fan-out, which is what the 8-minute field wedge actually
# was: no single probe exceeded its own bound, but the fan-out runs 4 metadata probes plus up to 4
# real-inference probes per model, sequentially, each with its own clock. 120s is deliberately
# generous — every individual probe is already capped at 15s, so a healthy engine (milliseconds per
# probe) never approaches this, and even a genuinely slow one gets two full models' worth. It bites
# only on an engine that is not answering, which is the regime it exists for. Same shape as
# `engine_health.PROBE_BUDGET`, one layer up.
PROBE_BUDGET = 120.0


class ProbeBudgetExceeded(Exception):
    """The capability fan-out ran out of wall clock.

    Carries what was probed before the budget ran out and which advertised models never got their
    turn, so the caller can decide — and the callers decide *differently*, which is the whole reason
    this is an exception rather than a silent degrade. See ADR 0022.
    """

    def __init__(self, probed: dict[str, Any], skipped: Sequence[str]) -> None:
        super().__init__(f"capability probing exceeded its {PROBE_BUDGET:.0f}s budget")
        self.probed = dict(probed)  # copied, like `skipped`: this is carried across a raise boundary
        self.skipped = list(skipped)


def probe_deadline() -> float:
    """A fresh deadline for one capability fan-out. Read from the module at call time, never bound as
    a default argument — a default would freeze the budget at import and make it untestable."""
    return time.monotonic() + PROBE_BUDGET


def log(msg: str) -> None:
    """The child's one narration voice: stderr, prefixed, flushed.

    The spawner redirects the child's stdout+stderr into ``<engine_id>.log`` and starts it with
    ``PYTHONUNBUFFERED=1``, so a line written here is on disk the moment it is written. That is what
    makes a 0-byte log positive evidence that nothing was *printed*, never that something was merely
    buffered — and why putting the first line before the first socket closes the whole failure mode.
    """
    print(f"[engine] {msg}", file=sys.stderr, flush=True)


def describe_intent(record: dict[str, Any], signaling_url: str) -> str:
    """What this child is about to do, rendered from the run record alone.

    Deliberately derived from the record rather than from probe results, so it can be said *before*
    the first socket — the point being that a child which never gets a network answer still explains
    itself. Names engines, models and the relay: the three facts an operator reading a stuck child's
    log needs before anything else.
    """
    specs = record.get("engines") or [
        {"endpoint_url": record.get("endpoint_url"), "models": record.get("models") or []}
    ]
    parts = []
    for spec in specs:
        models = ", ".join(str(m) for m in (spec.get("models") or [])) or "(no models named)"
        parts.append(f"{models} at {spec.get('endpoint_url') or 'the built-in engine'}")
    if record.get("media"):
        parts.append("media via ComfyUI")
    return f"serving {'; '.join(parts)}; registering with {signaling_url}"


def is_terminal(exc: relay.RelayError) -> bool:
    """Whether this failure will still be a failure after a retry.

    Two ways to be fatal. A *status* the relay answered with, meaning it understood the request and
    refused it; or a failure that never produced a request at all — a relay address that is not a
    usable URL is exactly as unfixable-by-waiting as a 403, and merely has no status to say so. A
    plain transport error (nothing answered, but the request was well-formed) is neither, and is
    retryable by construction — that is the mid-respawn case this module exists for.
    """
    return exc.terminal or exc.status in _TERMINAL_STATUSES


def _short(reason: str) -> str:
    """Trim a failure reason to a trace, never a transcript.

    The same bound, for the same reason, that ``service_truth.REGISTER_ERROR_MAX_CHARS`` puts on the
    copy reaching the run record — and it is needed independently here: ``relay._guard`` truncates
    response *bodies*, but the transport arm interpolates the raw exception, and one line per attempt
    for the life of a child that may never register goes into a log with no in-process rotation.
    """
    reason = reason.strip()
    if len(reason) <= service_truth.REGISTER_ERROR_MAX_CHARS:
        return reason
    return reason[: service_truth.REGISTER_ERROR_MAX_CHARS] + "… (truncated)"


def _wait_for(waits: Sequence[float], attempt: int) -> float:
    """The wait after ``attempt`` failures — the schedule, then its last value forever (the cap)."""
    return waits[min(attempt, len(waits)) - 1]


def register_with_backoff(
    register: Callable[[], None],
    *,
    stop: threading.Event,
    log: Callable[[str], None],
    note_error: Callable[[str | None], None],
    schedule: Sequence[float] | None = None,
) -> None:
    """Register with the relay, retrying a transient failure until it lands or ``stop`` is set.

    ``register`` is the caller's one-shot attempt (``serve.register_once``), passed in rather than
    imported so this module stays below the serve loop. ``log`` narrates each attempt and
    ``note_error`` persists the reason where the CLI can read it offline (ADR 0021's
    ``last_register_error``).

    ``schedule`` is resolved inside the body, never as a default argument: a default binds at import
    and would silently freeze the seam for every test that tried to replace it.
    """
    waits = list(_BACKOFF_SCHEDULE if schedule is None else schedule)
    attempt = 0
    relay_failures = 0
    auth_failures = 0
    while True:
        attempt += 1
        try:
            register()
            return
        except relay.RelayUnauthorized:
            # Counted separately from transport/status failures: the two schedules mean different
            # things, so a run of 503s must not push a later token failure straight to its floor.
            auth_failures += 1
            wait = _wait_for(_AUTH_BACKOFF_SCHEDULE, auth_failures)
            reason = _AUTH_REASON
        except relay.RelayError as exc:
            if is_terminal(exc):
                raise
            relay_failures += 1
            wait = _wait_for(waits, relay_failures)
            reason = str(exc)
        log(f"register attempt {attempt} failed ({_short(reason)}); retrying in {wait:.0f}s")
        # The RAW reason for the record — `note_register_error` applies its own bound. The attempt
        # number stays out of it on purpose: the record's writer skips a write when nothing changed,
        # so a stable string is what makes a day-long outage cost one write instead of one per beat.
        note_error(reason)
        if stop.wait(wait):
            raise BringUpStopped()
