"""POSIX argv sweep for ``grid leave`` (remote full-leave): reap detached serve children the
run-record pid can no longer reach.

The bug this closes: full leave kills by the run record's pid alone, so a stale/missing record
strands a live ``<cli> __remote-engine <network_id> …`` child that heartbeats as a provider forever.
This enumerates the process table and terminates every child whose argv carries the exact
``__remote-engine <network_id>`` marker+token — the safety net beside the record-pid kills, and the
only thing that reaps a record-less orphan (a bare ``grid leave``). POSIX only; Windows keeps the
record-pid path (follow-up F2). Matching is by exact argv tokens, so grid B's child (different
network id), unrelated commands (no marker), and the leave process itself (own pid, via
``exclude_pids``) are never touched; a match owned by another user surfaces as EPERM at terminate
time and is reported, not fatal.
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import NamedTuple

from shared import run_records


class SweepResult(NamedTuple):
    """The outcome of one ``sweep_orphans`` pass — immutable pid tuples plus whether the scan ran."""

    reaped: tuple[int, ...]     # our orphans, confirmed dead after the SIGTERM→SIGKILL escalation
    survivors: tuple[int, ...]  # ours, still alive past SIGKILL — leave must fail loud, naming them
    foreign: tuple[int, ...]    # matched but EPERM (another user's process) — reported, not fatal
    scanned: bool = True        # False ⇒ the process table couldn't be read (ps down). "Couldn't
    #                             check" ≠ "checked, clean", so the caller must qualify its success line
    #                             rather than claim a teardown it never verified (silent-failure review).


# ``ps`` wall-clock budget; a hung ``ps`` must not wedge ``grid leave`` (its record kills + backstop
# already ran, so the sweep degrades to empty rather than blocking).
_PS_TIMEOUT_SECONDS = 5


def _match_orphan_pids(
    ps_output: str, network_id: str, *, exclude_pids: set[int] | frozenset[int]
) -> list[int]:
    """Pids in ``ps_output`` (``pid=,command=`` rows) whose argv is a detached serve child for
    ``network_id``: the ``__remote-engine`` marker followed *immediately* by the exact ``network_id``
    token (spawn order is ``… __remote-engine <network_id> <engine_id>``). Skips any pid in
    ``exclude_pids`` (the leave process's own pid + the recorded pids already handled). Pure — the
    whole matcher is unit-tested over canned ``ps`` output.
    """
    matched: list[int] = []
    for line in ps_output.splitlines():
        parts = line.split(None, 1)  # split off exactly the pid; robust to a space-padded pid column
        if len(parts) != 2 or not parts[0].isdigit():
            continue  # header, blank line, or a pid-only / empty-command row
        pid = int(parts[0])
        if pid in exclude_pids:
            continue
        tokens = parts[1].split()
        try:
            marker = tokens.index(run_records.REMOTE_ENGINE_MARKER)
        except ValueError:
            continue  # not a remote-engine child
        # Exact whole-token, positional: the network id is the token right after the marker, so a
        # different grid's child and a coincidental substring never match, and a marker with no token
        # after it (``marker + 1`` out of range) is skipped.
        if marker + 1 < len(tokens) and tokens[marker + 1] == network_id:
            matched.append(pid)
    return matched


def _ps_output() -> str | None:
    """The process table as ``pid command`` rows, or ``None`` when ``ps`` is unavailable/slow.

    ``-A`` (POSIX "all processes") is deliberate over BSD ``-ax``: the detached serve child runs
    with ``start_new_session=True`` — a session leader with no controlling tty — and on Linux procps
    the dash form ``-a`` *excludes* exactly those, so the orphan we exist to reap would be invisible.
    ``-A`` lists every process on both macOS and Linux. ``-ww`` disables ``ps``'s column truncation,
    so a long interpreter path can never push the ``<network_id>`` token off the line and silently
    drop a match. A missing/slow ``ps`` is a note, not a crash: the record-pid kills and the relay
    backstop already ran, so leave degrades rather than tracebacks.
    """
    try:
        proc = subprocess.run(
            ["ps", "-A", "-ww", "-o", "pid=,command="],
            capture_output=True,
            text=True,
            errors="replace",  # a rogue process's non-UTF8 argv must not crash leave (marker/id are ASCII)
            timeout=_PS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(
            f"Note: couldn't list processes to reap orphaned serve children ({exc}).",
            file=sys.stderr,
        )
        return None
    if proc.returncode != 0:
        # ps ran but rejected the flags (a minimal/BusyBox ps) or errored — its (likely empty) stdout
        # must NOT read as a clean "no orphans" scan, or a failed check masquerades as a verified one.
        print(
            f"Note: `ps` exited {proc.returncode}; couldn't scan for orphaned serve children.",
            file=sys.stderr,
        )
        return None
    return proc.stdout


def sweep_orphans(
    network_id: str, *, exclude_pids: set[int] | frozenset[int] = frozenset()
) -> SweepResult:
    """Terminate every detached serve child of ``network_id`` in the process table, classifying each
    matched pid. POSIX only — Windows returns an empty result (the record-pid path stays; follow-up
    F2), and a missing/slow ``ps`` also returns empty (leave degrades, never crashes).

    ``exclude_pids`` (the caller's recorded pids) plus this process's own pid are never matched, so a
    wedged recorded child the record loop just failed to kill is not SIGKILL-escalated a second time,
    and the ``grid leave`` process never targets itself. Each match runs through the existing
    ``run_records.terminate_pid`` SIGTERM→grace→SIGKILL escalation: confirmed dead → ``reaped``,
    survives SIGKILL → ``survivors``; a match owned by another user raises ``PermissionError``
    (``terminate_pid`` does not catch EPERM) → ``foreign`` — reported, not fatal, not a survivor.
    """
    if sys.platform == "win32":
        # Windows keeps the record-pid path (follow-up F2); ``scanned=True`` because the argv sweep
        # isn't part of its contract, so there is no "couldn't check" gap to warn a Windows user about.
        return SweepResult((), (), ())
    output = _ps_output()
    if output is None:
        return SweepResult((), (), (), scanned=False)  # ps unavailable — couldn't check for orphans
    excluded = frozenset(exclude_pids) | {os.getpid()}
    reaped: list[int] = []
    survivors: list[int] = []
    foreign: list[int] = []
    for pid in _match_orphan_pids(output, network_id, exclude_pids=excluded):
        # PID-reuse TOCTOU: ``terminate_pid`` has an up-to-25s window between "alive" and its SIGKILL,
        # so on a busy box a matched pid could exit and be recycled before its turn — the same race
        # ``run_records.terminate_pid`` / ``_hot_reload_identity`` already carry, "not fully fixable
        # without pidfd". Accepted here too; a Linux ``pidfd_send_signal`` hardening is the future fix.
        try:
            if run_records.terminate_pid(pid):
                reaped.append(pid)
            else:
                survivors.append(pid)
        except PermissionError:
            foreign.append(pid)  # another user's process — report, never die
    return SweepResult(tuple(reaped), tuple(survivors), tuple(foreign))
