"""How many tasks a provider can actually take (ADR 0032 issue 09).

Every Claude Code child a provider spawns draws on the same Claude subscription, so the ceiling on
task work is that subscription's rate limit — not memory, not CPU, and not the inference
`max_concurrency`. This module holds the provider's reading of its own pressure and answers one
question with it: **may this provider claim another task right now?**

The signal is free. Claude Code emits a `rate_limit_event` on its `stream-json` stdout while a task
runs, so the provider learns its own state from work it was doing anyway rather than from a number an
operator benchmarked — which would be wrong on the next machine and stale after any plan change.

**Fail-open, in one direction only.** Stopping is the expensive answer: a provider that withdraws is
capacity the grid has lost until someone notices, and nothing here can tell the difference between
"the vendor changed a string" and "we are genuinely spent". So a block is only ever taken from a
reading that says, in terms this build recognises, BOTH that the window is spent AND when it comes
back. Everything else — an unparseable payload, a status in neither of the two known sets, a reset
that is missing or absurd — keeps the provider serving, which is the contract `cli_seat.probe_quota`
already states for the inference seat ("unknown means keep serving").

That rule has one edge worth stating out loud, because the obvious implementation gets it backwards.
Recognising statuses with an ALLOWLIST — spent is "anything that is not `allowed`" — reads every
string nobody has seen yet as a refusal, and then fails in the direction nothing recovers from: the
provider parks for the whole window the vendor named, the same string arrives next window, and the
log confidently blames a subscription when the real fault is here. So the two polarities are BOTH
enumerated, a status in neither is `None` (the same answer as junk), and an unrecognised one is
named in the log once so the gap can be closed rather than lived with.
"""
from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

# The statuses this build recognises, in BOTH directions — and a status in neither set is a reading
# with no verdict in it, handled exactly like a payload that would not parse.
#
# The two sets are what stops the fail-open rule from being fail-open in name only. An allowlist
# (`serving = status in _SERVING_STATUSES`) reads every string nobody has seen yet as *spent*, which
# is a claim about strings that do not exist — and it fails in the direction nothing recovers from:
# the provider parks for the whole window the vendor named, the same string arrives next window, and
# the log says "out of headroom" about what is really a parsing gap. `allowed_overage` is not a
# hypothetical shape; the payload already carries `isUsingOverage`.
#
# `allowed_warning` serves deliberately: it says *allowed*, and reading a warning as a stop parks a
# provider for a whole five-hour or weekly window. The inference seat
# (`shared/agent/seats/claude.py`) reads `!= "allowed"` as spent, which is right there — refusing one
# request costs seconds — and wrong here.
_SERVING_STATUSES = frozenset({"allowed", "allowed_warning"})
_SPENT_STATUSES = frozenset({"rejected"})

# The one status with nothing in it for the person who submitted the task: the window is open and
# their task is running. `task_stream` drops the CLIENT event for it — the reading still reaches the
# gate above, which is the half that acts on one.
#
# Only this exact string, and deliberately NOT `_SERVING_STATUSES`. `allowed_warning` is precisely
# when the next task's wait needs explaining, and an unrecognised status is news rather than silence
# — the same asymmetry `_SERVING_STATUSES`' own comment argues for, applied to narration instead of
# to routing. Lives here rather than in `task_stream` so `"allowed"` has ONE owner; a second copy
# would let the gate and the narration disagree about which reading is boring.
QUIET_STATUS = "allowed"


def worth_reporting(status: Any) -> bool:
    """Whether a rate-limit reading says anything the requesting user could act on.

    Not the same question as `serving`. That one decides whether this provider claims more work;
    this one decides whether a line appears in somebody's `grid task follow`. A reading can be
    serving and still worth saying (`allowed_warning`), which is why neither answer is derived from
    the other.
    """
    return status != QUIET_STATUS

# The furthest ahead a `resetsAt` may point and still be taken as a window. The vendor's widest is
# weekly, so two weeks clears every real one with room to spare — and rejects by many orders of
# magnitude the stamp that is actually a units mistake (milliseconds where seconds were meant), which
# would otherwise retire this provider's task serving for fifty thousand years. Not a capacity
# constant: it bounds how far a wire value is believed, which every other module here already does to
# the free text it forwards.
_MAX_TRUSTED_PAUSE_SECONDS = 14 * 24 * 3600.0

# The heartbeat `load` key this provider publishes its own withdrawal under (ADR 0033 D-l, issue
# 19b). **Hand-duplicated** with grid-src's `task_capacity.PAUSED_LOAD_KEY` and kept in lockstep by
# editing both repos — there is no compile-time link between the copies.
#
# ADR 0032 published none of this on purpose, and the reason was sound for one member per project: a
# task is claimed from a durable queue at poll time, so a provider that does not ask is simply not
# given one. With six other people on the same subscription it stops being sound — one member's
# reading withdraws the provider from the whole team, and the explanation reaches only the client
# whose task happened to be running.
#
# It is PUBLISHED and nothing more. Nothing routes on it, nothing consults it before handing out
# work, and the claim path is untouched: a paused provider still simply does not ask. That is why
# this is telemetry beside `platform` and `codex_rate_limits` rather than a second gate.
#
# *Absent ⇒ nothing withheld*, in both directions — an old provider never sends it, an old relay
# stores it in `NodeRow.load` and ignores it, and a provider that is serving emits nothing. So the
# rollout is free either way, like `unhealthy_models` and the author pair.
PAUSED_LOAD_KEY = "tasks_paused_until"


def _warn(message: str) -> None:
    print(f"\n[tasks] {message}", file=sys.stderr)


@dataclass(frozen=True)
class Pressure:
    """One reading of the provider's own subscription state."""

    serving: bool
    #: Unix seconds, exactly as the vendor gave it. `None` when it named none we could use.
    resets_at: float | None
    limit_type: str | None


class TaskCapacity:
    """The provider's answer to "may I start another task?". Process-wide, and never raises."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Monotonic, not wall-clock: the vendor's `resetsAt` is converted ONCE, at the moment the
        # block is taken, so a clock step afterwards can neither extend nor shorten it.
        self._blocked_until: float | None = None
        # Status strings already reported as unrecognised, so each is named once rather than once per
        # record — the signal repeats with every turn of the agent's output.
        self._unrecognised: set[str] = set()

    def observe(self, info: Any) -> None:
        """Take one `rate_limit_info` payload into account."""
        pressure = read(info)
        if pressure is None:
            self._note_unrecognised(info)
            return
        if pressure.serving:
            # Direct evidence the window came back, so the provider does not sit out the rest of a
            # block it no longer needs. Only a reading this build UNDERSTOOD gets to do this —
            # `read` returning None above leaves an existing block exactly as it was.
            with self._lock:
                self._blocked_until = None
            return
        pause = _pause_for(pressure)
        if pause is None:
            # Spent, but with no window this build can believe. There is nothing to resume AT, and a
            # block with no end is exactly the "silently stop taking work" this feature may never
            # produce — so it keeps serving and lets the run itself report the refusal.
            return
        now = time.monotonic()
        with self._lock:
            # A block already in force is EXTENDED silently; only entering one is announced. The
            # signal rides the agent's output, so a spent subscription reports itself over and over,
            # and a line per record would bury the one an operator needs in its own repetition.
            fresh = self._blocked_until is None or self._blocked_until <= now
            self._blocked_until = now + pause
        if fresh:
            _warn(f"this provider's Claude subscription is out of headroom "
                  f"({pressure.limit_type or 'rate'} limit), so it is no longer claiming tasks. "
                  f"Tasks already running are unaffected, and claiming resumes in "
                  f"{pause / 60:.0f} minute(s), when the window resets. Inference serving is "
                  f"untouched.")

    def _note_unrecognised(self, info: Any) -> None:
        """Report a status this build does not know, once per distinct string.

        Serving through one is the safe answer AND a blind spot: if the new status did mean "spent",
        every task this provider claims from here fails. The string itself is the only thing an
        operator can act on, so it is named — and a payload with no readable status at all is NOT
        reported, because that is ordinary junk rather than a gap in what this build understands.
        """
        status = info.get("status") if isinstance(info, dict) else None
        if not isinstance(status, str) or not status:
            return
        with self._lock:
            if status in self._unrecognised:
                return
            self._unrecognised.add(status)
        _warn(f"this build does not recognise the rate-limit status {status!r}, so it is being "
              f"treated as no information and this provider keeps claiming tasks. If tasks start "
              f"failing against a spent subscription, that is why — the status needs adding to "
              f"remote/task_capacity.py.")

    def pause_seconds(self) -> float:
        """How long before this provider may claim again. `0.0` means claim now."""
        with self._lock:
            until = self._blocked_until
        if until is None:
            return 0.0
        return max(0.0, until - time.monotonic())

    def paused_until(self) -> float | None:
        """When this provider starts claiming again, as unix seconds — or `None` while it is
        claiming now (ADR 0033 D-l, issue 19b).

        The published half of `pause_seconds`, and the only thing about capacity that leaves this
        process. A TIMESTAMP rather than a boolean because "paused" alone tells a team to keep
        watching, while a reset time tells them whether to wait or to add a provider — which are the
        only two things they can do about it.

        Derived from the block, never from the vendor's `resetsAt`. That stamp was converted to a
        MONOTONIC deadline once, at the moment the block was taken, precisely so a later clock step
        could not move it — and republishing the raw value would undo that, as well as republish a
        window `_pause_for`'s two-week ceiling had already refused. Converting back at read time
        means a clock step moves the ANSWER with the clock, which is right: the reader's clock is
        the one the answer is for.
        """
        pause = self.pause_seconds()
        # `0.0` is the same fact as "never blocked": absent from the heartbeat, nothing withheld.
        # Recovery therefore needs no fresh reading — the block simply stops being published.
        if pause <= 0.0:
            return None
        return time.time() + pause


# One provider process runs against one Claude subscription, so there is one reading of it. Held
# here rather than threaded from `serve.py` because that is what makes N task workers share a gate
# without any of them owning it: a per-worker view would let each worker learn the same refusal the
# expensive way, by claiming a task and failing it.
_SHARED = TaskCapacity()


def shared() -> TaskCapacity:
    """This process's subscription gate — the one every task worker consults."""
    return _SHARED


def read(info: Any) -> Pressure | None:
    """One `rate_limit_info` payload as a reading, or `None` when it cannot be read."""
    if not isinstance(info, dict):
        return None
    status = info.get("status")
    if not isinstance(status, str) or status not in _SERVING_STATUSES | _SPENT_STATUSES:
        return None
    limit_type = info.get("rateLimitType")
    return Pressure(
        serving=status in _SERVING_STATUSES,
        resets_at=_resets_at(info.get("resetsAt")),
        limit_type=limit_type if isinstance(limit_type, str) else None,
    )


def _pause_for(pressure: Pressure) -> float | None:
    """How long a spent reading is worth waiting out, or `None` when it names no usable window.

    Converted from the vendor's wall clock HERE, once, so everything downstream is monotonic — and
    bounded on both ends, with the SAME answer at each end: no usable window.

    A reset already behind us is the end that is easy to get wrong. Clamping it to zero looks
    equivalent and is not: it would install a block that has already expired, and an expired block
    reads as "claim now" — so a garbled reading would LIFT a real block taken moments earlier. The
    fail-open rule is about never inventing a block, never about discarding one we were given.
    """
    if pressure.resets_at is None:
        return None
    pause = pressure.resets_at - time.time()
    if pause <= 0.0 or pause > _MAX_TRUSTED_PAUSE_SECONDS:
        return None
    return pause


def _resets_at(raw: Any) -> float | None:
    """The vendor's reset stamp as unix seconds, or `None` when it is not one we can use."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)
