"""What this box is still serving, for the flows that are about to delete the credentials that
address it (grid-leave issue 13, ADR 0023).

Three handlers clear or overwrite the remote grid list — ``grid logout`` deletes ``credentials.toml``
outright, ``grid sync`` and ``grid login`` overwrite ``[[networks]]`` authoritatively — and all three
used to do it without a glance at the detached serve children still polling the relay. Those children
hold their per-grid access token **in memory** from spawn (TTL ≈ 1 year), so they keep registering and
heartbeating as a provider long after the operator believes they signed out; and because the credential
store is simultaneously the only index of which grids this box knows *and* the gate on ``grid leave``,
deleting it removes the only handle able to stop them.

This module answers the one question all three need — *which grids is this box still serving?* — and,
for logout, performs the teardown while the token that makes it authoritative still exists.
"""
from __future__ import annotations

import sys
from collections.abc import Iterable
from typing import Any, NamedTuple

from shared import run_records


class LiveScan(NamedTuple):
    """Which grids have a live serve child on this box, and whether the process table could be read.

    ``scanned`` is separate from an empty ``by_grid`` for the reason it always is in this feature: a
    box with nothing running and a box whose process table we could not read look identical, and only
    one of them may be reported as clean.
    """

    by_grid: dict[str, tuple[int, ...]]  # grid id -> pids of its live serve child(ren)
    scanned: bool                        # False ⇒ the process table couldn't be read


def _is_remote_child(record: dict[str, Any]) -> bool:
    """Whether a run record belongs to a **remote** serve child rather than a local-mode engine.

    ``~/.grid/run/engines`` is shared by both modes: a local grid's engines sit in a directory beside a
    remote grid's, and nothing in the path distinguishes them. Signing out of a remote account must not
    stop an engine serving a *local* grid — that grid has no account behind it, sign-out says nothing
    about it, and killing it would take down a working endpoint the operator never mentioned.

    ``signaling_url`` is the discriminator because it is the *definition* of the difference rather than
    a proxy for it: a remote child polls a relay and records the one it polls, while a local engine is
    pushed to and records only its own endpoint. (The argv sweep needs no equivalent — it matches
    ``__remote-engine <network_id>``, so a local engine can never produce a hit.)

    The fail direction is safe by construction: a remote record that somehow lacks the field is skipped
    *here* and still found by the sweep, which matches argv rather than record content. A false negative
    costs one extra `grid leave`; a false positive would kill a local operator's engine.
    """
    return bool(str(record.get("signaling_url") or "").strip())


def _recorded_live_pids(records: dict[str, dict[str, Any]]) -> tuple[int, ...]:
    """The pids of a grid's records that still name a running **remote** serve child of ours.

    ``record_alive`` rather than ``pid_alive``: a zombie serves nothing and a recycled pid is somebody
    else's process, and treating either as live is what made `grid join` vouch for dead engines
    (ADR 0020). A stale record alone is therefore not evidence that this box is serving anything.
    """
    live: list[int] = []
    for record in records.values():
        if not _is_remote_child(record) or not run_records.record_alive(record):
            continue
        pid = run_records.recorded_pid(record) or 0  # None (unusable shape) and 0 (never stamped) alike
        if pid > 0:
            live.append(pid)
    return tuple(live)


def live_identities(network_ids: Iterable[str]) -> LiveScan:
    """The serve children this box is running for ``network_ids``, cheapest evidence first.

    Records answer for free, so the process table is read only for the grids they cannot vouch for —
    and not at all when every grid has a live record, or when there are no grids to ask about. That
    ordering is what keeps an ordinary `grid logout` on a box with nothing joined free of any scan.

    The fall-through matters as much as the shortcut: ``read_records`` returns ``{}`` both for a grid
    never joined and for a grid whose record was unlinked out from under a **live** child, and only
    the argv sweep can tell those apart.
    """
    from remote import orphan_sweep  # lazy: remote.* imports stay out of cli import time

    by_grid: dict[str, tuple[int, ...]] = {}
    unvouched: list[str] = []
    for network_id in network_ids:
        live = _recorded_live_pids(run_records.read_records(network_id))
        if live:
            by_grid[network_id] = live
        else:
            unvouched.append(network_id)
    if not unvouched:
        return LiveScan(by_grid, True)
    found, scanned = orphan_sweep.find_orphans(unvouched)
    return LiveScan({**by_grid, **found}, scanned)


def warn_stranded(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> None:
    """Name every serve child an authoritative grid-list overwrite just left without a token.

    ``grid sync`` and ``grid login`` both replace ``[[networks]]`` wholesale, so a grid that drops out
    reaches the same state a logout does for that grid: the child keeps polling on the token it loaded
    at spawn, and nothing addressed by the credential store can deregister it. Neither verb tears the
    child down — a control-plane answer is not an intent to stop serving, and a transient one returning
    fewer grids would destroy working capacity that nothing was wrong with. So they say it instead.

    Dropped grids are diffed on ``network_id``, never on the display name: a grid renamed on the website
    would otherwise read as one vanishing and another appearing, and warn about a child nothing
    stranded. Nothing here is a fatal path — a warning that raised would turn a refresh into a failure.
    """
    dropped = {
        str(net["network_id"]): net for net in previous if net.get("network_id")
    }
    for net in current:
        dropped.pop(str(net.get("network_id") or ""), None)
    if not dropped:
        return
    scan = live_identities(dropped)
    for network_id, pids in scan.by_grid.items():
        label = str(dropped[network_id].get("name") or network_id)
        named = ", ".join(str(pid) for pid in pids)
        print(
            f"Warning: this box is still serving {label} (pid {named}), which is no longer in your "
            f"grid list — so nothing here can deregister it. Run `grid leave {network_id}` to stop it "
            "(that grid drops its models after the node TTL, ~120s).",
            file=sys.stderr,
        )
    # This warning is the ONLY place a stranded child can surface: by the time it runs the overwrite has
    # already taken the token. So an unreadable process table cannot be allowed to look like "nothing
    # was stranded" — a record-less orphan is visible nowhere else. Named per grid here, unlike the
    # sign-out's aggregate note, because a drop list is short and each entry is separately actionable.
    for network_id, net in dropped.items():
        if scan.scanned or network_id in scan.by_grid:
            continue
        label = str(net.get("name") or network_id)
        print(
            f"Warning: {label} is no longer in your grid list, and the process table couldn't be read "
            f"to check whether this box is still serving it. Run `grid leave {network_id}` to make sure "
            "(it needs no sign-in).",
            file=sys.stderr,
        )


class SignoutOutcome(NamedTuple):
    """What one grid's teardown established, for a caller deciding whether it may clear credentials.

    ``bundled`` is the axis that decides what a failure *costs*. A grid we still hold a token for can
    be retried authoritatively, so keeping its credentials is worth blocking the sign-out for; a grid
    whose bundle an earlier sync/login overwrite already dropped has no token to preserve, and its
    remedy — ``grid leave <id>`` — needs none.
    """

    label: str
    network_id: str
    bundled: bool                # a credential bundle for this grid still exists
    ok: bool                     # nothing live remains that we were able to see
    sent: bool                   # the relay accepted the backstop deregister
    survivors: tuple[int, ...]   # live children we could not stop (negative ⇒ a process group)
    unchecked: bool              # neither the record nor the process table could verify anything


class UncheckedGrid(NamedTuple):
    """A grid the sign-out could establish nothing about, and what it managed to do anyway."""

    label: str
    network_id: str
    deregistered: bool  # the backstop landed, so the grid stops listing this box regardless


class SignoutResult(NamedTuple):
    """Everything a sign-out established — including what it could **not**.

    ``unscanned`` is not a detail: a grid whose record file was unlinked out from under a live child is
    visible only in the process table, so when that table cannot be read there is no evidence either
    way. Returning the outcomes alone would let the caller delete the credentials and report a clean
    exit over a child still heartbeating — the exact "couldn't check read as checked, clean" failure
    ADR 0019-0023 keep closing. So the grids nothing could be established about travel back by name.
    """

    outcomes: list[SignoutOutcome]
    unscanned: tuple[UncheckedGrid, ...]  # grids an orphan could not be ruled out for


def stop_serving(networks: list[dict[str, Any]], *, session: str) -> SignoutResult:
    """Tear down every serve identity this box is running, before the credentials that address them go.

    Scoped by run-record **directories** (``run_records.known_grid_ids``), not by the credential list:
    a grid stranded by an earlier ``grid sync``/``grid login`` overwrite has a live child and no bundle,
    and scoping by the bundle list would walk straight past it. A grid that was never joined has no
    directory, so a box with nothing to stop still pays nothing.

    Each grid is decided and torn down under the same record ``file_lock`` that ``grid leave`` holds, so
    a concurrent ``grid join`` either finished (its record is visible to the read below) or waits behind
    us. The one process-table read cannot be inside a per-grid lock — it answers for every grid at once
    — so a *record-less* child spawned between that read and this lock is missed here and caught by the
    next ``grid leave <id>``; a completed join always writes a record, which is why the window is narrow.
    """
    from shared.filelock import file_lock

    from . import remote_grid

    # Directory names reach a relay/control-plane request path via the teardown, so they are validated
    # exactly like an id out of the credential store — the run tree is a filesystem, and a name in it
    # is untrusted input the same way a control-plane reply is.
    grid_ids = [nid for nid in run_records.known_grid_ids() if remote_grid._valid_network_id(nid)]
    if not grid_ids:
        return SignoutResult([], ())
    bundles = {
        str(net["network_id"]): net for net in networks if net.get("network_id")
    }
    from . import remote_provider

    scan = live_identities(grid_ids)
    outcomes: list[SignoutOutcome] = []
    unscanned: list[UncheckedGrid] = []
    for network_id in grid_ids:
        rec = bundles.get(network_id) or {"network_id": network_id, "name": network_id}
        label = str(rec.get("name") or network_id)
        with file_lock(run_records.record_path(network_id, run_records.REMOTE_IDENTITY)):
            records = run_records.read_records(network_id)
            # The authoritative liveness read, inside the lock: a join that completed while we were
            # scanning is visible here even though the hint predates it.
            if not _recorded_live_pids(records) and network_id not in scan.by_grid:
                # No live record and no sweep hit. That is "nothing is serving" ONLY if the sweep
                # actually ran: a record-less orphan lives exclusively in the process table, so an
                # unreadable table makes this branch "no evidence" rather than "no child". Skipping it
                # silently is how a sign-out reports clean over a live provider — carry it back named.
                if not scan.scanned:
                    # ...and still deregister while the token exists. The sweep is a diagnostic; the
                    # backstop is the mechanism of record (which is why `grid leave` sends it
                    # unconditionally) and it needs no process table. Withholding it here would leave the
                    # grid advertising this box over a child we could not even look for. The flip is
                    # idempotent and resurrection-proof, so sending it for a grid that turns out not to be
                    # serving costs one request. An unbundled grid has no token, so nothing is sent.
                    sent = bool(bundles.get(network_id)) and remote_provider._full_leave_backstop(
                        rec, records, session, network_id, label
                    )
                    unscanned.append(UncheckedGrid(label, network_id, sent))
                continue
            outcomes.append(
                _teardown_one(rec, session, network_id, label, records, bundled=network_id in bundles)
            )
    return SignoutResult(outcomes, tuple(unscanned))


def _teardown_one(
    rec: dict[str, Any], session: str, network_id: str, label: str,
    records: dict[str, dict[str, Any]], *, bundled: bool,
) -> SignoutOutcome:
    """One grid's teardown, reduced to an outcome. Never raises: a sign-out must not be abandoned
    half-done because one grid misbehaved, and the *next* grid may be the one actually serving.

    An unexpected failure is reported as ``unchecked`` rather than swallowed — "we could not check"
    is the one thing this feature refuses to let read as "checked, clean" (ADR 0022/0023).
    """
    from . import remote_provider

    try:
        reaped, sent = remote_provider._full_leave_execute(rec, session, network_id, label, records)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — deliberately broad; see the docstring
        print(f"Couldn't stop what this box serves on {label} ({exc}).", file=sys.stderr)
        return SignoutOutcome(label, network_id, bundled, ok=False, sent=False,
                              survivors=(), unchecked=True)
    unchecked = bool(reaped.unverified) and not reaped.scanned
    return SignoutOutcome(
        label, network_id, bundled,
        ok=not reaped.survivors and not unchecked,
        sent=sent,
        survivors=tuple(reaped.survivors),
        unchecked=unchecked,
    )
