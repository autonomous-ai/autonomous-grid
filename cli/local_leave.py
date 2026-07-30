"""The teardown half of local-mode `grid leave` — the record kills, the argv sweep beside them, and
what the success line is allowed to claim (grid-leave issue 17, ADR 0025).

Split out of `cli/provider.py` so the leave contract reads in one place; `cmd_leave` there stays the
argparse/target-resolution shell, and `provider._stop_engine` stays where remote's teardown already
calls it.

Why local needs a sweep at all, given remote's exists for a different reason: remote survives a
missed child because `grid leave` sends an authoritative relay deregister (`PUT` role=`consumer`),
which a surviving child cannot undo. Local has no equivalent and cannot have one — the only CLI-side
verb is `DELETE /nodes/{id}`, and a surviving child's next heartbeat 404s and **re-registers**
(`cli/provider._heartbeat`), putting the models back within one 15s beat. So a local engine child
that leave fails to stop pins its models in `grid models` indefinitely, and the argv sweep is the
only net there is. That asymmetry is why local's caveats below cannot promise a TTL drop the way
remote's do.
"""
from __future__ import annotations

import sys
from typing import Any, NamedTuple

from shared import orphan_sweep, run_records


# The three things a local sweep can fail to establish. The wording is local's own — remote's says a
# stray child's models drop at the ~120s node TTL, which is true there only because its backstop
# already flipped the node to consumer. Here a stray child keeps heartbeating and its models never
# drop, so the caveat has to name the remedy instead of a deadline. The precedence between the three
# lives in `orphan_sweep.caveats`, shared with remote so the two can't drift.
_SWEEP_UNSCANNED_NOTE = (
    " Couldn't scan for stray engine processes (the process table was unreadable); a stray one would "
    "keep serving its models to this grid until it is stopped."
)
_SWEEP_PARTIAL_NOTE = (
    " Couldn't see all of the process table (most command lines were hidden from this account), so a "
    "stray engine process may remain; an elevated `grid leave` can see them."
)
_SWEEP_FOREIGN_NOTE = (
    " Couldn't stop an engine process for this grid owned by another user (named above); it keeps "
    "serving its models here until an elevated `grid leave` stops it, or its owner does."
)
_SWEEP_NOTES = orphan_sweep.SweepNotes(
    unscanned=_SWEEP_UNSCANNED_NOTE, partial=_SWEEP_PARTIAL_NOTE, foreign=_SWEEP_FOREIGN_NOTE
)


class _Reap(NamedTuple):
    """What one local teardown established. Named rather than a bare tuple for the reason
    ``run_records.Teardown`` and ``remote_provider._ReapResult`` are: ``survivors`` and ``unverified``
    are both ``list[int]`` and mean opposite things — "still running and we failed to stop it" versus
    "we could not tell" — so a transposition would be invisible to a type checker."""

    survivors: list[int]   # negative entries name a process GROUP (`run_records.describe_target`)
    unverified: list[int]  # record pids neither stopped as ours nor proven clean
    scanned: bool          # False ⇒ the argv sweep could not read the process table at all
    foreign: tuple[int, ...] = ()  # live children of THIS grid we were not permitted to stop
    partial: bool = False  # the sweep ran but was shown only part of the table (Windows)


def tear_down(
    grid_id: str, label: str, targets: dict[str, dict[str, Any]], *, engine_id: str | None = None
) -> int:
    """Stop the engine children ``targets`` names, sweep for any the records could not reach, and
    report. Returns the exit code; raises ``SystemExit`` when something was left running or nothing
    could be established at all.

    ``engine_id`` is the single engine a ``--engine`` leave named, and ``None`` for a whole-grid
    leave (bare, ``--all``, or a repair run with no records at all). It decides how much of the spawn
    argv the sweep pins, which is the difference between reaping a record-less orphan and killing a
    sibling engine that is still meant to be serving.
    """
    reap = _reap(grid_id, label, targets, engine_id=engine_id)
    if reap.survivors:  # a live child survived SIGKILL — fail loud, never "Left engine …"
        _survivor_exit(reap.survivors, label)
    if reap.unverified and not reap.scanned:  # both nets blind at once — this leave checked nothing
        _unchecked_exit(reap.unverified, label)
    _report(label, targets, reap)
    return 0


def _reap(
    grid_id: str, label: str, targets: dict[str, dict[str, Any]], *, engine_id: str | None
) -> _Reap:
    """Kill the recorded children, then argv-sweep for what the records could not reach."""
    from . import provider  # the shared teardown: stops the engine + reaps a media engine's ComfyUI

    pinned = (grid_id,) if engine_id is None else (grid_id, engine_id)
    record_pids = {
        pid for record in targets.values() if (pid := run_records.recorded_pid(record) or 0) > 0
    }
    # BEFORE the kills, not after: a recorded child's launcher shim carries the same argv it does, and
    # the only way to tell them apart is the parent link — which leaves the process table the moment
    # the kill below succeeds. Asked afterwards this returns nothing, and a healthy leave would then
    # sweep its own launcher and announce a reaped orphan. Guarded because it runs EARLIER than the
    # sweep, so an exception here would abort the leave before the record kills as well.
    try:
        shims = orphan_sweep.launcher_ancestors(
            run_records.LOCAL_ENGINE_MARKER, *pinned, pids=record_pids
        )
    except (Exception, SystemExit) as exc:
        print(f"Note: couldn't identify launcher processes ({exc}).", file=sys.stderr)
        shims = frozenset()

    survivors: list[int] = []
    unverified: list[int] = []
    for target_id, record in targets.items():
        outcome = provider._stop_engine(grid_id, target_id, record)
        if outcome.survivor:  # survived even SIGKILL — its record was kept; the caller fails loud
            # Negated for a process group, so the failure message can name it as one and print a
            # remedy that works: a bare pid there names the group's leader, which is already gone.
            survivors.append(-outcome.survivor if outcome.is_group else outcome.survivor)
        elif not outcome.verified:
            # Remote's rule verbatim (`remote_provider._full_leave_reap`), never-stamped `0`
            # included. A `pid: 0` record still leaves quietly, but because the SWEEP vouched for the
            # box, not because of a special case here — the conjunction in `tear_down` is what makes
            # that hold, and `_unchecked_exit` filters the 0 out of its message.
            unverified.append(run_records.recorded_pid(record) or 0)

    swept = _sweep(grid_id, pinned, exclude_pids=record_pids | shims)
    _narrate_sweep(label, swept)
    return _Reap(survivors + list(swept.survivors), unverified, swept.scanned,
                 foreign=swept.foreign, partial=swept.partial)


def _narrate_sweep(label: str, swept: orphan_sweep.SweepResult) -> None:
    """What the sweep did, per pid, as it happened. Separate from the success line because these are
    facts about individual processes, where the caveats are a claim about the whole box — and because
    a `foreign` match must be *named* on stderr whichever branch the report below takes."""
    if swept.reaped:
        print(
            f"Reaped {len(swept.reaped)} orphaned engine child(ren) on {label} "
            f"(pid(s) {', '.join(map(str, swept.reaped))})."
        )
    for pid in swept.foreign:
        print(
            f"Note: an engine process for {label} (pid {pid}) is owned by another user; left it alone.",
            file=sys.stderr,
        )


def _sweep(
    grid_id: str, pinned: tuple[str, ...], *, exclude_pids: frozenset[int] | set[int],
) -> orphan_sweep.SweepResult:
    """Terminate this grid's stray engine children — but never one that a run record vouches for.

    Local mode runs one child per engine and, unlike remote, `grid leave` holds **no file lock**
    (`cli/provider` takes none, and neither does the join it would have to serialize against). So a
    `grid join` landing mid-leave writes its `pid: 0` placeholder, spawns a child carrying this
    grid's marker, and a grid-wide sweep would SIGKILL a healthy engine somebody just started — for
    the whole leave, including up to 25 seconds of stop grace.

    What closes it is an ordering invariant already in the spawn path: `cli/provider._spawn_engine`
    writes the record **before** it calls `Popen`, so a child that exists always has a record. Read
    the process table first and the records second, and every child in that table is in that
    snapshot — so a match whose engine id still has a record is a live engine we must leave alone,
    and a match with no record at all is the orphan we came for. The read order is the guard;
    re-reading first would leave the hole wide open.

    Every record present at the re-read vouches, **including this leave's own targets** — because by
    then `run_records.stop_engine` has unlinked the record of every target it confirmed dead. A
    target's record that is *back* was therefore written after we removed it, which is precisely a
    `grid join --name <same id>` that raced us; subtracting the target ids instead would sweep that
    re-joined child and report it as a reaped orphan. A target we could *not* confirm dead keeps its
    record and is vouched for here too, which changes nothing: its pid is already in ``exclude_pids``
    and it is already being reported as a survivor.
    """
    scan = _scan(pinned, exclude_pids=exclude_pids)
    if scan is None:
        return orphan_sweep.SweepResult((), (), (), scanned=False)
    try:
        vouched = set(run_records.read_records(grid_id))
    except (Exception, SystemExit) as exc:
        # Deciding failed AFTER the table was read, so the matches are real findings and must not be
        # thrown away with them — the mistake `orphan_sweep.terminate` documents one level down, and
        # the one this whole issue exists to end. We cannot tell an orphan from a racing join here,
        # so nothing is signalled; but a live child of this grid that we did not stop is exactly what
        # `survivors` means, and reporting it that way names the pid and fails loud instead of
        # printing a clean success over it. `scanned` stays **True**: the process table was read, and
        # blaming it would send the operator to `ps` for a run-record problem.
        print(
            f"Note: couldn't read the run records for {grid_id} to tell this grid's own engine "
            f"children from strays ({exc}); left them alone.",
            file=sys.stderr,
        )
        return orphan_sweep.SweepResult(
            (), tuple(scan.matches), (), unreadable=scan.unreadable
        )
    return orphan_sweep.terminate_matches(
        scan, [pid for pid, engine_id in scan.matches.items() if engine_id not in vouched]
    )


def _scan(
    pinned: tuple[str, ...], *, exclude_pids: frozenset[int] | set[int]
) -> orphan_sweep.Scan | None:
    """Read the process table, or ``None`` when it could not be read *or* the read itself blew up.

    The sweep is a best-effort diagnostic and the record teardown that already ran is the mechanism
    of record, so anything unforeseen here degrades to an honest "couldn't check" rather than taking
    the whole leave down with it. (`SystemExit` is this repo's clean-error idiom and is not an
    `Exception`, hence both.) Narrow on purpose: this guard covers only the read, so a failure
    *after* it cannot borrow its "the table was unreadable" verdict.
    """
    try:
        return orphan_sweep.scan_engine_children(
            run_records.LOCAL_ENGINE_MARKER, *pinned, exclude_pids=exclude_pids
        )
    except (Exception, SystemExit) as exc:
        print(f"Note: the scan for orphaned engine children failed ({exc}).", file=sys.stderr)
        return None


def _survivor_exit(survivors: list[int], label: str) -> None:
    """Fail loud when a child outlived the teardown, naming what it actually is.

    Deliberately makes **no** claim about the run record: it is kept for a recorded survivor
    (`run_records.stop_engine` only unlinks on confirmed death) but a swept orphan never had one, and
    the two arrive in the same list. What is true of both is that a retry targets them again.
    """
    named = ", ".join(run_records.describe_target(value) for value in survivors)
    first = survivors[0]
    remedy = (
        f"taskkill /F /PID {first}" if sys.platform == "win32"
        else f"kill -9 -{-first}" if first < 0
        else f"kill -9 {first}"
    )
    raise SystemExit(
        f"grid leave: could not stop engine child(ren) on {label}: {named}. They keep serving their "
        f"models to this grid until they stop. A retried `grid leave` will target them again; "
        f"investigate before re-joining (e.g. `{remedy}`)."
    )


def _unchecked_exit(unverified: list[int], label: str) -> None:
    """Fail a leave that could neither confirm its recorded child stopped nor scan for a stray one.

    Two independent nets normally catch a stranded engine child: the run record (stop it by identity)
    and the argv sweep (find it in the process table). Each failing alone is fine and silent — that
    is what the other is for, and it is why a never-stamped `pid: 0` record still leaves quietly on
    any box whose process table can be read. Both failing at once is the reported bug: leave printed
    "Left engine …" having stopped nothing and confirmed nothing, so the operator had no signal while
    a live orphan kept its models listed (grid-leave issue 08's residual (b), issue 17 for local).

    The record is **not** kept here, unlike the survivor case: there is no live process it is a handle
    to, and an unverifiable record stays unverifiable forever, so keeping it would recreate exactly
    the never-converging retry loop. A retry runs the bare repair path instead, which succeeds as soon
    as processes are listable again.
    """
    pids = ", ".join(str(pid) for pid in unverified if pid) or "the recorded child"
    raise SystemExit(
        f"grid leave: could not confirm the engine child for record pid(s) {pids} on {label} was "
        "stopped, and could not read the process table to look for a stray one — so nothing about "
        "this box was verified. Retry `grid leave` once processes are listable "
        f"({'tasklist' if sys.platform == 'win32' else 'ps'} is the check)."
    )


def _report(label: str, targets: dict[str, dict[str, Any]], reap: _Reap) -> None:
    """Print the honest outcome of a leave with no surviving child, qualified by whatever the sweep
    could not establish. None of those is a failure — but none of them is a verified-clean box
    either, and "couldn't check" must not read as "checked, clean"."""
    if targets:
        for target_id in targets:
            print(f"Left engine {target_id} on {label}.")
    else:
        print(f"No engines were tracked for {label}; checked this box for stray engine children.")
    caveats = orphan_sweep.caveats(
        scanned=reap.scanned, partial=reap.partial, foreign=bool(reap.foreign), notes=_SWEEP_NOTES,
    )
    if caveats:
        # One trailing line rather than a suffix per engine: a local leave can print several "Left
        # engine …" lines, and a caveat repeated N times is one nobody finishes reading.
        print(caveats.strip())
