"""Is the identity a run record names actually **serving**, or merely running?

`grid join`'s idempotent no-op gate used to answer that with `os.kill(pid, 0)`. Issue 08 closed the
half of the lie where the pid was a corpse; this module closes the other half, which no liveness
check can reach: a genuinely live serve child that never got onto the grid, or stopped being on it.
Field-confirmed 2026-07-27 — a join issued while the grid's master was mid-respawn produced a child
parked in bring-up for 8+ minutes with a 0-byte log, and the gate reported "Already serving …;
nothing to append." at exit 0 while `grid models` showed nothing from the box.

Two facts make "serving" checkable offline, both written by the serve child:

- ``registered_at`` on the run record — did it ever advertise itself to the relay;
- the **heartbeat sidecar**'s mtime (``run_records.heartbeat_path``) — is it still doing so.

The sidecar doubles as the **upgrade marker**, which is the whole reason this needs no version field:
a child that reports service truth creates it in its startup self-stamp, so *no sidecar ⇒ the running
child is not a build that reports ⇒ say nothing and behave exactly as before*. That fails open in the
other direction too — the self-stamp is best-effort, so a new build whose stamp failed also reads as
"not reporting" rather than as "broken". Same rule as ADR 0020's "a fact we could not read is
``None``, never a placeholder".

See `docs/adr/0021-service-truth-at-the-join-gate.md`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import time
from typing import Any, NamedTuple

from shared import run_records

from . import relay

# How stale the sidecar may be before the identity is reported as not heartbeating. **3×** the
# heartbeat interval, not the 2× the issue sketched, because a *successful* tick's own worst case is
# already ~65s: the touch lands the moment the heartbeat returns, but the rest of the tick runs after
# it — `_maybe_refresh_codex` (15s) + the ADR 0019 engine probe (10s) + the 30s wait + the next
# heartbeat's own 10s timeout. A 404 pushes it past 90s, since `heartbeat_once` re-registers inline
# (15s, plus a possible 401 refresh). 60s would therefore accuse a healthy engine routinely. 90s also
# stays under the relay's 120s node TTL, so the CLI still knows before the grid gives up.
HEARTBEAT_STALE_AFTER_SECONDS = 3 * relay.HEARTBEAT_INTERVAL

# Below this age a child that has not registered is reported as **starting**, and no respawn is
# suggested. `_await_remote_engine_start` waits 3s, but a real bring-up — a large GGUF load, a
# ComfyUI start — is minutes, and the child stamps its sidecar *before* bringing engines up. Telling
# an operator to `--respawn` 60s into a 4-minute model load would kill a healthy engine and restart
# the load, repeatably. The residual >90s staleness tail above is survivable for the same reason this
# window exists: the message never acts.
BRINGUP_QUIET_SECONDS = 300.0

# A trace, not a transcript — the `last_reload_error` bound (remote/serve.py), for the same reason:
# this lands in a run record that is read by every CLI command and must never grow a payload.
REGISTER_ERROR_MAX_CHARS = 300


# The record fields this module owns. They belong to the **process**, exactly like `run_records`'
# identity block — which is what decides where they travel: a hot-reload is the same process and
# carries them, a respawn is a new one and must not. They live here rather than beside
# `identity_stamp` because "did this engine reach the relay" is a remote concept: `shared/` has no
# relay, and `local/` never writes them.
SERVICE_TRUTH_FIELDS = ("registered_at", "last_register_error")


def carry_service_truth(new: dict[str, Any], old: dict[str, Any]) -> None:
    """Move an identity's service truth onto a rebuilt record for the SAME process (a hot-reload).

    ``_build_record`` constructs a fresh dict that has none of these, so without this a hot-reloading
    append would make the next re-join report "has not registered" about a process that has been
    serving all along. Absent stays absent — never written as ``None``, which the gate would then have
    to tell apart from "never registered"."""
    for field in SERVICE_TRUTH_FIELDS:
        if field in old:
            new[field] = old[field]


def clear_service_truth(record: dict[str, Any]) -> None:
    """Strip an identity's service truth from a record about to name a NEW process (a respawn).

    Same rule as the identity stamp's "never a stale token", one layer up: a fresh child has
    registered nothing, and an inherited ``registered_at`` would make the gate vouch for a process
    that has not yet reached the relay."""
    for field in SERVICE_TRUTH_FIELDS:
        record.pop(field, None)


class ServiceTruth(NamedTuple):
    """What the record and its sidecar say about an identity that is *running*."""

    reports: bool  # the child is a build that writes service truth (its sidecar exists)
    registered: bool
    beat_age: float | None  # seconds since the last successful heartbeat; None when not reporting
    uptime: float | None  # seconds since this process was spawned; None when unparseable
    last_error: str | None
    pid: int


class NotServing(NamedTuple):
    """A running identity that is not on the grid. ``state`` is the operator-facing clause the caller
    prints to stdout; ``detail`` is the stderr block (never empty — it always carries the log path)."""

    state: str
    detail: str


def touch_heartbeat(grid_id: str, engine_id: str) -> None:
    """Mark one **successful** heartbeat. The mtime is the entire payload — no lock, no record write,
    so this costs one `utime` per 30s instead of contending with `grid join`/`leave` at that cadence.

    Skipped when the record is already gone: a sidecar beside nothing is inert, but leaving one is
    litter that outlives the grid it belonged to, and a `grid leave` racing this call has just earned
    the right to a clean directory. Called only on success — freshening it after a *failed* beat would
    launder an unreachable engine into a healthy-looking one, which is the exact lie this module ends.
    """
    if not run_records.record_path(grid_id, engine_id).exists():
        return
    # `mode=0o600`, like every other file grid writes under `~/.grid` (`filelock`'s sentinel, the
    # 0o600 records, the credential stores). The default 0o666 would land 0o644 under a normal umask
    # and 0o666 under `umask 0`, where any local user could forge a beat and make a dead engine read
    # as healthy to the gate. `touch` bumps mtime without reopening when the file already exists, so
    # the per-beat cost is one `utime`; the mode applies at creation only, which is the only moment
    # it matters. (A umask can only make this stricter, never looser.)
    run_records.heartbeat_path(grid_id, engine_id).touch(mode=0o600)


def mark_registered(grid_id: str, engine_id: str) -> bool:
    """Record that this child reached the relay, and clear whatever failure it had been carrying.

    ``setdefault``, not assignment: the field answers "did this process ever get on the grid", and a
    heartbeat-404 re-register or a post-reload re-advertise is the same process getting back on. The
    sidecar is what says whether it is *still* there.

    Returns whether there is anything left to do, because this is the one fact here that fails
    **closed**: absent ``registered_at`` is read as a confident "has not registered", so a single
    lost write would make the gate accuse an engine that is serving perfectly. The caller retries on
    later beats while this is ``False``. A record that no longer exists counts as settled — there is
    nothing to write onto, and retrying would take the record lock every 30s forever."""
    stamped = False

    def _mark(record: dict[str, Any]) -> None:
        nonlocal stamped
        record.setdefault("registered_at", _utc_now())
        record.pop("last_register_error", None)
        stamped = True

    run_records.mutate_record(grid_id, engine_id, _mark)
    return stamped or not run_records.record_path(grid_id, engine_id).exists()


def note_register_error(grid_id: str, engine_id: str, message: str | None) -> None:
    """Persist (``str``) or clear (``None``) the reason this engine is not on the grid.

    Bounded to a trace, never a transcript, and never a secret — the same contract as
    ``last_reload_error``, for the same reason: this lands in a run record every CLI command reads.
    ``mutate_record`` skips the write when nothing changed, so a relay that has been down for a day
    costs one write, not one per beat."""

    def _note(record: dict[str, Any]) -> None:
        if message is None:
            record.pop("last_register_error", None)
        else:
            record["last_register_error"] = message[:REGISTER_ERROR_MAX_CHARS]

    run_records.mutate_record(grid_id, engine_id, _note)


def _utc_now() -> str:
    """The `started_at` format (``local.runtime.utc_now``), duplicated rather than imported: `remote`
    does not depend on `local`, and the two values are only ever compared to the wall clock."""
    return datetime.now(timezone.utc).isoformat()


def _parse_started_at(record: dict[str, Any]) -> float | None:
    """Seconds since ``started_at``, or ``None`` when it is absent or unparseable. Never raises and
    never guesses: an unknown age is reported as unknown, so the message cannot invent an uptime."""
    raw = record.get("started_at")
    if not isinstance(raw, str):
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamp.tzinfo is None:  # a naive stamp from an older build — read it as UTC, not as local time
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0.0, time.time() - stamp.timestamp())


def _humanize(seconds: float) -> str:
    """A duration an operator reads at a glance. Carries a day tier because both durations here are
    genuinely unbounded — an engine that stopped heartbeating is not noticed until someone re-joins,
    which can be weeks — and "up 57603h 19m" is a number nobody parses."""
    if seconds < 90:
        return f"{int(seconds)}s"
    minutes = int(seconds // 60)
    if minutes < 90:
        return f"{minutes}m"
    hours, mins = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {mins}m"
    return f"{hours // 24}d {hours % 24}h"


def service_truth(record: dict[str, Any], grid_id: str, engine_id: str) -> ServiceTruth:
    """Read the identity's service facts off disk. Pure observation — it never writes and never
    raises; an unreadable sidecar is simply "not reporting"."""
    try:
        beat_at: float | None = run_records.heartbeat_path(grid_id, engine_id).stat().st_mtime
    except OSError:  # absent, or a directory we cannot stat — either way it tells us nothing
        beat_at = None
    error = record.get("last_register_error")
    return ServiceTruth(
        reports=beat_at is not None,
        registered=bool(record.get("registered_at")),
        beat_age=None if beat_at is None else max(0.0, time.time() - beat_at),
        uptime=_parse_started_at(record),
        last_error=error if isinstance(error, str) and error else None,
        pid=run_records.recorded_pid(record) or 0,
    )


def not_serving(
    record: dict[str, Any], grid_id: str, engine_id: str, *, log_path: Path
) -> NotServing | None:
    """``None`` when this running identity is serving (or when it cannot be asked), else the honest
    report the caller prints instead of claiming success.

    The order matters: "never registered" is checked before staleness, because a child that never got
    on the grid has a stale sidecar too, and the first fact is the one that explains the second."""
    truth = service_truth(record, grid_id, engine_id)
    if not truth.reports:
        return None  # an older build's child — say nothing new about it (the upgrade rule)

    age = "uptime unknown" if truth.uptime is None else f"up {_humanize(truth.uptime)}"
    starting = truth.uptime is not None and truth.uptime < BRINGUP_QUIET_SECONDS
    # A record whose pid can't be read shouldn't be described as "pid=0" — that names init. It should
    # not reach here (such a record reads as dead, so the gate never runs), which is exactly why the
    # phrasing must not depend on it being unreachable.
    who = f"the engine is up (pid={truth.pid})" if truth.pid else "the engine is up"
    if not truth.registered:
        state = (
            f"{who} and is still starting — not registered with the relay yet ({age})"
            if starting
            else f"{who} but has not registered with the relay ({age})"
        )
        return NotServing(state, _detail(truth, log_path, starting=starting))
    if truth.beat_age is not None and truth.beat_age > HEARTBEAT_STALE_AFTER_SECONDS:
        state = f"{who} but its last heartbeat was {_humanize(truth.beat_age)} ago"
        return NotServing(state, _detail(truth, log_path, starting=False))
    return None


def _detail(truth: ServiceTruth, log_path: Path, *, starting: bool) -> str:
    """The stderr block: what went wrong, where to look, and — only once the bring-up window has
    passed — the way out. The suggestion is deliberately **conditional**, never an instruction: this
    verdict is inferred from two timestamps, and acting on a wrong one costs the operator a working
    engine and its in-flight requests."""
    lines = []
    if truth.last_error:
        # "Warning:" belongs to the error, not to the block — prefixing the block produced
        # "Warning: (see log=…)" whenever the record carried no reason, which reads like a bug.
        lines.append(f"Warning: last register error: {truth.last_error}")
    lines.append(f"(see log={log_path})")
    if not starting:
        lines.append(
            "If it stays this way, re-run this join with --respawn to stop this engine and start a "
            "fresh one."
        )
    return "\n".join(lines)
