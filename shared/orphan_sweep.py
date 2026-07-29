"""Argv sweep for ``grid leave`` (both modes): reap detached engine children the run-record pid can no
longer reach.

The bug this closes: leave kills by the run record's pid alone, so a stale/missing record strands a
live engine child that keeps heartbeating forever. This enumerates the process table and terminates
every child whose argv carries the exact marker+token of the grid being left — the safety net beside
the record-pid kills, and the only thing that reaps a record-less orphan (a bare ``grid leave``).

Both spawn argvs have the same shape, which is why one matcher serves both modes: a remote serve
child is ``<cli> __remote-engine <network_id> <engine_id>`` and a local engine child is
``<cli> __engine <grid_id> <engine_id>``. Callers pass the ``marker`` and however many leading
positionals they want pinned — remote pins the network id, a local whole-grid leave pins the grid id,
and a local ``--engine`` leave pins the grid id *and* the engine id so a sibling engine's child on the
same grid is never touched. Matching is by exact argv tokens *and their count* (the marker must be
followed by exactly the two positionals ``cli/_main.py`` parses), so another grid's child (different
id), unrelated commands (no marker), a bystander that merely mentions the marker (a
``pgrep``/``watch``/wrapper — one token after it), and the leave process itself (own pid, via
``exclude_pids``) are never touched.

It lives in ``shared/`` rather than under one mode because both modes' leave uses it, exactly as
``shared.run_records`` holds the record format and teardown they share.

There is deliberately **no owner check** (ADR 0024): unelevated POSIX already declines for free — a
match owned by another user raises EPERM at terminate time and lands in ``foreign``, reported and
never signalled — while an *elevated* sweep is authoritative on purpose and kills every match for this
grid regardless of owner. ``foreign`` is not benign, though: it means a live child of THIS grid we
could not stop, so the caller qualifies its success line rather than exiting non-zero.

Both platforms are covered (grid-leave follow-up F2 closed the Windows gap): POSIX reads ``ps``,
Windows asks WMI through PowerShell, and both normalise to the same ``pid ppid command`` rows so ONE
matcher serves both. Two Windows behaviours differ from POSIX and are deliberate. A process the caller
cannot open has a **null command line** from WMI, so it cannot be matched — but it is emitted as an
inert ``_WIN_UNREADABLE_MARKER`` row rather than dropped, so the size of that blind spot is *counted*
and reported (``unreadable`` / ``partial``); dropping them is what let a scan of a fraction of the
table report itself clean. And ``run_records.terminate_pid`` has no grace loop there, so a Windows
teardown is a single forced tree kill, and a visible-but-unkillable cross-user match becomes a
``survivor`` rather than ``foreign``. See ``remote/CONTEXT.md`` and ``local/CONTEXT.md`` ("What
``grid leave`` guarantees") for each mode's operator-facing contract.
"""
from __future__ import annotations

import base64
import os
import subprocess
import sys
import time
from collections.abc import Iterable
from typing import NamedTuple

from shared import run_records, win_paths


# How many unreadable rows (see ``_WIN_UNREADABLE_MARKER``) a scan may contain and still be treated
# as having seen the box. Windows-only in practice: ``ps`` prints every process's argv.
#
# It is a floor rather than zero because some processes are opaque to *everyone* — pid 0, pid 4,
# Registry, Memory Compression, Secure System, and anything running as a protected process (Defender)
# hand back a null command line to an administrator too. Qualifying on `> 0` would therefore footnote
# every Windows leave ever run, and a caveat that is always printed is one nobody reads.
#
# The number is pinned empirically, not guessed twice: the ``test-windows`` CI job runs on a
# `windows-latest` runner *as an administrator* — i.e. against exactly the "unreadable even to an
# admin" baseline this floor has to clear — and asserts the real count stays under it, failing with
# the measured value. What that proves is one-directional (the floor is not too LOW, so no false
# qualification); that it is not too HIGH is covered by the hermetic boundary test instead.
_WIN_UNREADABLE_FLOOR = 24


def _is_partial_scan(unreadable: int) -> bool:
    """Whether enough of the process table was hidden that a scan cannot vouch for the box.

    One predicate, so the two result types below can never disagree about where "I saw the box" stops
    and "I saw what I was permitted to" begins — and so the floor itself stays inside this module. No
    caller should have to know the number, and none does.
    """
    return unreadable > _WIN_UNREADABLE_FLOOR


class SweepResult(NamedTuple):
    """The outcome of one ``sweep_orphans`` pass — immutable pid tuples plus whether the scan ran."""

    reaped: tuple[int, ...]     # our orphans, confirmed dead after the SIGTERM→SIGKILL escalation
    survivors: tuple[int, ...]  # ours, still alive past SIGKILL — leave must fail loud, naming them
    foreign: tuple[int, ...]    # matched but EPERM (another user's process) — reported, not fatal
    scanned: bool = True        # False ⇒ the process table couldn't be read. "Couldn't check" ≠
    #                             "checked, clean", so the caller must qualify its success line
    #                             rather than claim a teardown it never verified (silent-failure review).
    unreadable: int = 0         # rows the enumerator was not allowed to read a command line for. A
    #                             scan can succeed and still be partial, which `scanned` alone cannot
    #                             say — see `partial` below. Always 0 on POSIX.

    @property
    def partial(self) -> bool:
        """Whether this scan saw enough of the box to vouch for it — see ``_is_partial_scan``.

        A *property* rather than a field so whole-tuple equality keeps working in the tests that use
        it, and so it can never be constructed inconsistently with ``unreadable``.
        """
        return _is_partial_scan(self.unreadable)


class SweepNotes(NamedTuple):
    """One mode's wording for the three things a sweep can fail to establish.

    The *strings* are per mode and cannot be shared: remote's say a stray child's models drop at the
    ~120s node TTL, which is true there only because the backstop already flipped the node to
    consumer — locally a stray child keeps heartbeating and its models never drop at all. The
    *precedence* between them is not per mode, which is why `caveats` below is.
    """

    unscanned: str  # the process table couldn't be read at all
    partial: str    # it was read, but most command lines were hidden from this account
    foreign: str    # a live child of this grid we were not permitted to stop


def caveats(*, scanned: bool, partial: bool, foreign: bool, notes: SweepNotes) -> str:
    """Everything a leave's success line must not leave unsaid, as one suffix.

    Lives here rather than at each caller because that is how the two remote report paths drifted
    before: one carried the unscanned caveat and the other re-derived it. Now a third caller (local)
    inherits the rule for free and can only differ in wording.

    "Couldn't read the table at all" **returns early** rather than accumulating: with no rows there
    was nothing to match and nothing to count, so the other two are empty by construction and would
    only add a second, quieter version of the same admission.
    """
    if not scanned:
        return notes.unscanned
    return (notes.partial if partial else "") + (notes.foreign if foreign else "")


# ``ps`` wall-clock budget; a hung ``ps`` must not wedge ``grid leave`` (its record kills + backstop
# already ran, so the sweep degrades to empty rather than blocking).
_PS_TIMEOUT_SECONDS = 5

# The Windows equivalent, deliberately larger. PowerShell + WMI is a different order of cost from
# ``ps``: a cold ``powershell.exe`` start is seconds on its own, a ``Win32_Process`` enumeration adds
# more on a loaded box, and EDR process-creation hooks tax both. Still well under the same leave's
# other budgets (it may already have spent ``run_records._STOP_GRACE_SECONDS`` = 25s on the record
# kill) — this means "WMI is genuinely wedged", not "the box is busy".
_WIN_PROCESS_TIMEOUT_SECONDS = 15

# How long to let a process that a tree kill was *supposed* to take actually disappear before judging
# it. Both platforms terminate asynchronously — Windows `TerminateProcess` returns before the process
# is gone, and on POSIX a just-signalled child is briefly a zombie, which `pid_alive` reports as alive
# — so probing the instant after the root dies would report a successful teardown as a survivor.
_SETTLE_POLLS = 20
_SETTLE_INTERVAL_SECONDS = 0.05

# Total wall-clock the sweep may spend terminating. Each `terminate_pid` can burn
# `run_records._STOP_GRACE_SECONDS` (25) on a process that ignores SIGTERM, and the number of matches
# is attacker-influenced: the network id is public in every serve child's command line, so any local
# user can spawn K processes carrying it that trap SIGTERM. Unbounded, that stalls `grid leave` for
# K x 25s — and the relay deregister runs *after* the sweep, so the node stays registered and keeps
# taking inference jobs for the whole window. Past the deadline the remaining matches are reported as
# survivors (they are alive and we did not stop them, which is the honest thing to say) and leave
# fails loud instead of hanging.
_SWEEP_DEADLINE_SECONDS = 60

# What the Windows enumerator writes in the command-line column for a process it was not allowed to
# read. WMI hands an unelevated caller a **null** command line for any process it cannot open, and
# those rows used to be dropped before they ever reached the matcher — so the sweep was structurally
# blind to another user's (or an elevated) serve child while ``scanned`` stayed True and ``grid
# leave`` printed an unqualified success (grid-leave issue 15/B).
#
# A sentinel row is inert to every consumer by construction: it holds no ``__remote-engine`` marker,
# so ``_match_orphan_pids`` can never match it and nothing is ever signalled because of one. It is a
# single token containing no ``_``, so it cannot even accidentally form the marker triple. What it
# adds is a row the sweep can COUNT — the difference between "checked, clean" and "checked what I was
# permitted to see".
_WIN_UNREADABLE_MARKER = "<unreadable>"

# The Windows process table as ``pid ppid <command line>`` rows — the shape ``ps -o pid=,ppid=,command=``
# prints, so ``_match_orphan_pids`` serves both platforms unchanged.
#
# Why CIM and nothing else: ``tasklist`` has no command-line column in any output format; ``wmic`` can
# do it but is deprecated and no longer installed by default (Windows 11 24H2 / Server 2025);
# ``Get-Process`` grew a ``CommandLine`` property only in PowerShell 7.1, and 5.1 is what ships in the
# box. A local CIM query needs no WinRM.
#
# Four details are load-bearing:
#   * ``$ErrorActionPreference='Stop'`` + try/catch. A *non-terminating* cmdlet error — the Winmgmt
#     service stopped, a corrupt WMI repository, access denied — otherwise leaves PowerShell exiting
#     **0 with empty stdout**, which under this module's contract reads as "checked, clean" and would
#     let leave claim a teardown it never verified. (``_win_process_output`` guards the same thing a
#     second way, because "exit 0 on failure" is PowerShell's default disposition, not an edge case.)
#   * ``[Console]::Out.WriteLine`` rather than emitting to the pipeline. Pipeline output goes through
#     PowerShell's host writer, which wraps at the host width (120 with no console — and
#     ``CREATE_NO_WINDOW`` guarantees no console). A wrapped row loses its *tail*, and the marker plus
#     network id are the last tokens on a serve child's command line, so the sweep would go blind to
#     exactly what it hunts while ``scanned`` stayed True. This is the Windows counterpart of the
#     POSIX ``-ww``. It is a static .NET call, which Constrained Language Mode forbids — but under CLM
#     it *throws*, so the script exits 1 and the caller reports "couldn't scan". Failing loudly under
#     a lockdown policy beats silently mis-scanning everywhere else.
#   * The ASCII fold is applied to ``$_.CommandLine`` ALONE. Applied to the joined output it would
#     replace CR/LF too (they are outside \x20-\x7E) and collapse the whole table into a single row.
#     Folding to ASCII at the producer is what makes the output decode identically under any
#     console/ANSI code page — PowerShell writes the OEM page while Python's text mode decodes the
#     ANSI one.
#   * The script is a CONSTANT: the network id is never interpolated into it (all matching happens in
#     Python), so this helper's own row can never carry the marker it is hunting for. The sentinel
#     literal below is written inline for the same reason, and `_count_unreadable`'s test asserts the
#     two spellings have not drifted.
#   * A null ``CommandLine`` becomes ``_WIN_UNREADABLE_MARKER``, never a dropped row. Filtering with
#     ``Where-Object { $_.CommandLine }`` hid every process the caller could not open from the matcher
#     *and* from the count, which is what let a scan of a fraction of the table report itself clean.
_WIN_PROCESS_LIST_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; "
    "try { "
    "Get-CimInstance -ClassName Win32_Process -Property ProcessId,ParentProcessId,CommandLine | "
    "ForEach-Object { "
    "$line = if ($_.CommandLine) { "
    "(($_.CommandLine -replace '[^\\x20-\\x7E]', '?') -replace '\\s+', ' ') } "
    "else { '<unreadable>' }; "
    "[Console]::Out.WriteLine('{0} {1} {2}' -f $_.ProcessId, $_.ParentProcessId, $line) }; "
    "exit 0 } catch { exit 1 }"
)


# How many positionals every engine-child dispatch takes after its marker. ONE number because both
# `cli/_main.py` entries parse exactly two — `__remote-engine <network_id> <engine_id>` and
# `__engine <grid_id> <engine_id>` — and the matcher's whole discriminator is this count.
_DISPATCH_ARITY = 2


def _match_engine_children(
    table_output: str, marker: str, *pinned: str, exclude_pids: set[int] | frozenset[int]
) -> dict[int, str]:
    """Engine children in ``table_output`` (``pid ppid command`` rows) as ``{pid: engine_id}``.

    A row matches when its argv contains ``marker`` as a whole token, followed *immediately* by each
    token in ``pinned``, and by exactly ``_DISPATCH_ARITY`` tokens in total — so a caller pins as much
    of the spawn argv as it means to claim: the grid alone (every engine child of that grid, which is
    what finds a record-less orphan whose engine id nobody knows) or the grid **and** an engine id
    (that child only). Skips any pid in ``exclude_pids`` (the leave process's own pid + the recorded
    pids already handled). Pure — the whole matcher is unit-tested over canned output from both
    platforms.

    The value is the **engine id**, always the last positional, which is what lets a caller decide per
    match rather than per scan — `cli/local_leave` spares a child whose engine id belongs to a record
    it is not leaving.

    Position is measured from the marker, never from the start of the row, so the leading ``ppid``
    column is simply another token it steps over — which is why one matcher can read both a Windows
    command line (quoted ``"C:\\Program Files\\…"`` paths and all) and a POSIX ``ps`` row.

    Pinning **nothing** is refused rather than treated as "match everything". The marker alone is not
    the identity of a grid — it is the identity of the *CLI* — so a caller that forgets its id would
    match every engine child on the box and hand them all to ``terminate_matches``, killing every
    other grid's engines. Guarded here rather than trusted to callers, for the reason
    ``terminate_group`` guards its own ``pgid``: this is the last place before something gets
    signalled, and both teardowns wrap it in a degrade-to-"couldn't check" handler, so the failure
    direction is a reported no-op rather than a crash.
    """
    if not pinned:
        raise ValueError(f"a {marker!r} sweep must pin at least the grid id; matching on the marker "
                         "alone would reach every grid's engine children")
    matched: dict[int, str] = {}
    for line in table_output.splitlines():
        parts = line.split(None, 1)  # split off exactly the pid; robust to a space-padded pid column
        # ``isdecimal`` rather than ``isdigit``: the latter is True for characters ``int()`` rejects
        # (``'²'``), and this runs inside a teardown that must never raise.
        if len(parts) != 2 or not parts[0].isdecimal():
            continue  # header, blank line, or a pid-only / empty-command row
        pid = int(parts[0])
        if pid in exclude_pids:
            continue
        tokens = parts[1].split()
        try:
            at = tokens.index(marker)
        except ValueError:
            continue  # not an engine child
        # Exact whole-token, positional, and the marker must be followed by EXACTLY the two arguments
        # `cli/_main.py` parses for it — `<grid id> <engine_id>`, nothing after. Requiring the count
        # is what separates an engine child from a bystander that merely mentions it: an operator
        # running `pgrep -f "__remote-engine <nid>"`, a `watch`, or a wrapper script has one token
        # after the marker, and without this the next `grid leave` force-kills it (measured). A
        # different grid's child and a coincidental substring never match either way.
        #
        # Lockstep, and it is a TWO-way one: this count is the dispatch signature of **both**
        # `__remote-engine` and `__engine` in `cli/_main.py`. Adding an argument or a flag to either
        # without updating it makes the sweep stop seeing that mode's engine children entirely —
        # silently, since "no orphans" is also what a clean box looks like.
        if len(tokens) != at + 1 + _DISPATCH_ARITY:
            continue
        if list(pinned) != tokens[at + 1 : at + 1 + len(pinned)]:
            continue
        matched[pid] = tokens[at + _DISPATCH_ARITY]
    return matched


def _match_orphan_pids(
    table_output: str, marker: str, *pinned: str, exclude_pids: set[int] | frozenset[int]
) -> list[int]:
    """The pids of ``_match_engine_children``, in process-table order — for every caller that acts on
    the whole match set rather than deciding per child."""
    return list(_match_engine_children(table_output, marker, *pinned, exclude_pids=exclude_pids))


def _count_unreadable(table_output: str) -> int:
    """How many rows the enumerator could not read a command line for — the size of this scan's blind
    spot. Always ``0`` on POSIX, where ``ps`` prints every process's argv.

    The command field must equal ``_WIN_UNREADABLE_MARKER`` **whole**. A substring test would let any
    process whose real command line merely mentions the token inflate the count, and the count is the
    only thing deciding whether ``grid leave`` qualifies its success line.
    """
    unreadable = 0
    for line in table_output.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdecimal() and parts[2] == _WIN_UNREADABLE_MARKER:
            unreadable += 1
    return unreadable


def _ppid_by_pid(output: str) -> dict[int, int]:
    """Parent pid per pid, read from the same ``pid ppid command`` rows the matcher reads. A row whose
    second field isn't a number (a two-column table, a header) yields no entry."""
    parents: dict[int, int] = {}
    for line in output.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2 or not parts[0].isdecimal() or not parts[1].isdecimal():
            continue
        parents[int(parts[0])] = int(parts[1])
    return parents


def _tree_roots(matched: list[int], ppid_by_pid: dict[int, int]) -> tuple[list[int], list[int]]:
    """Split argv matches into the pids to terminate and the ones that are descendants of those.

    A launcher shim carries the SAME argv as the interpreter it spawns — the uv/pip trampoline on
    Windows, the Nuitka ``--onefile`` bootstrap on the Linux build — so one record-less orphan shows
    up as two rows. Terminating the root can take the descendant with it, so only roots are targeted
    and the caller re-probes each descendant afterwards rather than assuming the tree kill reached it.

    Be precise about what that buys, because the two platforms differ. On Windows ``taskkill /T``
    really does take the tree, so one orphan is terminated once and reported once. On POSIX
    ``terminate_pid`` reaches ``killpg`` only after the target ignores SIGTERM for the whole grace, so
    a root that exits promptly signals nothing and each descendant is escalated on its own — meaning
    ``reaped`` there counts **processes actually terminated, not logical engines**, and a three-link
    chain can honestly report three. This split avoids escalating the same tree twice; it is not a
    de-duplicated engine count on POSIX and must not be described as one.

    An unknown parent is treated as a root, and so is every pid in an ancestry cycle (possible under
    pid reuse) — both fail toward *terminating*, which is the direction that cannot strand a live
    serve child. With no ppid column at all the map is empty and every match is a root, i.e. exactly
    the pre-shim-rule behaviour.
    """
    kin = set(matched)
    roots = [pid for pid in matched if ppid_by_pid.get(pid) not in kin]
    if matched and not roots:  # every match is inside a parent cycle — try them all rather than none
        return list(matched), []
    root_set = set(roots)
    return roots, [pid for pid in matched if pid not in root_set]


def _posix_ps_output() -> str | None:
    """The process table as ``pid ppid command`` rows, or ``None`` when ``ps`` is unavailable/slow.

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
            ["ps", "-A", "-ww", "-o", "pid=,ppid=,command="],
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
            f"Note: `ps` exited {proc.returncode}; couldn't scan for orphaned serve children."
            f"{_first_line(proc.stderr)}",
            file=sys.stderr,
        )
        return None
    return proc.stdout


def _first_line(stderr: str | None) -> str:
    """The tool's own first line of complaint, appended to our note. Without it an operator cannot
    tell a stopped Winmgmt service from a corrupt WMI repository from access denied — three problems
    with three different remedies, behind one identical message."""
    line = (stderr or "").strip().splitlines()
    return f" ({line[0][:200]})" if line else ""


def _win_powershell_path() -> str:
    """Windows PowerShell 5.1 by absolute path — it ships in every supported Windows, unlike ``pwsh``.

    Absolute because ``CreateProcess`` resolves a bare name through a search order that includes the
    **current directory**, and ``grid leave`` runs wherever the operator happens to be: a binary
    planted there would be executed as us. The kernel-backed directory it is built on lives in
    ``shared.win_paths`` — shared with ``run_records.kill_group``'s ``taskkill``, which had the same
    hazard, because ``shared/`` may not import ``remote/``.
    """
    return win_paths.system32_tool("WindowsPowerShell\\v1.0\\powershell.exe")


def _win_process_output() -> str | None:
    """The Windows process table as ``pid ppid command`` rows, or ``None`` when it couldn't be read.

    ``-EncodedCommand`` (base64 UTF-16LE) rather than ``-Command``: it removes the whole
    command-line-quoting question instead of managing it with an invariant a later edit could break,
    it is not subject to execution policy the way ``-File`` is, and — because the base64 alphabet has
    no ``_`` — it makes it *structurally* impossible for this helper's own row to contain the
    ``__remote-engine`` marker and be swept by the very scan it is performing.

    Three failure modes, three answers. A missing/slow PowerShell degrades exactly like a missing
    ``ps``. A non-zero exit (the script's ``catch``) is "couldn't check". And **output with no
    parseable process row is also "couldn't check"**, which the POSIX path does not need: PowerShell
    reports a non-terminating error on stderr and still exits 0 with nothing on stdout, so under the
    ``ps`` contract a stopped Winmgmt service would read as "checked, clean". The check is for a real
    row rather than merely non-blank text, so a stray banner or a BOM cannot satisfy it either.

    All three answer one question — *did the enumeration run at all*. They say nothing about how much
    of the table it was allowed to see, and no version of this guard could: since the sentinel change
    every process yields a row, so a caller who can open almost nothing still gets a full-looking
    table. That second question is answered by counting sentinels (``_count_unreadable`` /
    ``SweepResult.partial``), not here.
    """
    encoded = base64.b64encode(_WIN_PROCESS_LIST_SCRIPT.encode("utf-16-le")).decode("ascii")
    try:
        proc = subprocess.run(
            [
                _win_powershell_path(),
                "-NoProfile", "-NonInteractive",
                "-InputFormat", "None", "-OutputFormat", "Text",
                "-EncodedCommand", encoded,
            ],
            capture_output=True,
            text=True,
            errors="replace",  # the payload is ASCII-folded, but a decode must never raise (ValueError)
            timeout=_WIN_PROCESS_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
            # Windows-only flag, fetched defensively: referencing it directly would crash the import on
            # POSIX, and keeping it reachable is what lets a test flip ``sys.platform`` and exercise
            # this branch on the Linux CI. Without it, a grid invoked from a GUI/service host (no
            # console to inherit) pops a PowerShell window mid-teardown.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        # Named separately: TimeoutExpired stringifies its whole argv, which here is an 876-character
        # base64 blob — an unreadable wall in place of a one-line note.
        print(
            f"Note: listing processes timed out after {_WIN_PROCESS_TIMEOUT_SECONDS}s; "
            "couldn't scan for orphaned serve children.",
            file=sys.stderr,
        )
        return None
    except (OSError, subprocess.SubprocessError) as exc:
        print(
            f"Note: couldn't list processes to reap orphaned serve children ({exc}).",
            file=sys.stderr,
        )
        return None
    if proc.returncode != 0:
        print(
            f"Note: `powershell` exited {proc.returncode}; couldn't scan for orphaned serve children."
            f"{_first_line(proc.stderr)}",
            file=sys.stderr,
        )
        return None
    if not _ppid_by_pid(proc.stdout):
        print(
            "Note: `powershell` listed no processes; couldn't scan for orphaned serve children."
            f"{_first_line(proc.stderr)}",
            file=sys.stderr,
        )
        return None
    return proc.stdout


def _process_table_output() -> str | None:
    """The host's process table as ``pid ppid command`` rows, or ``None`` when it couldn't be read.

    The ONE platform-neutral seam. Tests patch this (never a per-platform implementation), so a
    full-leave test stays hermetic on any OS and can never shell out to the developer's real process
    table and SIGKILL a ``grid join`` they were actually running.
    """
    return _win_process_output() if sys.platform == "win32" else _posix_ps_output()


def launcher_ancestors(
    marker: str, *pinned: str, pids: set[int] | frozenset[int]
) -> frozenset[int]:
    """The launcher shims of ``pids`` — parents that are themselves engine children of the same grid.

    **Call this before terminating ``pids``, not after.** A shim carries the same argv as the
    interpreter it spawns, so leaving it out of the sweep's exclusions makes a perfectly healthy
    ``grid leave`` announce "Reaped 1 orphaned serve child(ren)" — and, if the shim is mid-cleanup and
    slow to die (a Nuitka ``--onefile`` bootstrap removing its extracted temp directory), burn the
    25-second stop grace and exit non-zero. The parentage can only be read while the child still has a
    row in the process table, which a confirmed kill has already removed; asking afterwards silently
    returns nothing, which is a guard that looks present and never fires.

    **Both** the recorded pid and its parent must carry the marker and these exact pinned tokens.
    Testing only the parent is not enough, and the case that breaks it is this feature's own premise
    rather than an edge case: a stale recorded pid, recycled onto some unrelated process, whose new
    parent happens to be a real orphan — that orphan carries the marker, so a parent-only rule would
    quietly exclude the very thing the sweep exists to reap. Requiring the child to be an engine child
    too separates them, because a stale pid's new occupant is not one.

    Best-effort: this returns nothing on failure and lets the sweep that follows report the problem
    (which it will state twice if the table is unreadable, once per read — noisy, but each note is
    true). Returns immediately when there is nothing to look up: a bare ``grid leave`` with no records
    is a first-class repair verb, and enumerating the whole table to answer a question about an empty
    set would double that path's cost — on Windows a second cold PowerShell start, seconds of it.
    """
    if not pids:
        return frozenset()
    output = _process_table_output()
    if output is None:
        return frozenset()
    ours = set(_match_orphan_pids(output, marker, *pinned, exclude_pids=frozenset()))
    parents = _ppid_by_pid(output)
    shims: set[int] = set()
    for pid in pids:
        if pid not in ours:
            continue  # a stale recorded pid whose occupant isn't an engine child has no shim of ours
        # Walk up while every step is still an engine child of this grid: a chain can be more than one
        # link (a `.cmd` shim into `grid.exe` into the interpreter), and stopping at the first parent
        # would spare the inner shim while sweeping the outer one — the same false "Reaped 1" on a
        # healthy leave, moved up a level. The ``not in shims`` guard is what terminates an ancestry
        # cycle, which pid reuse can produce; without it this loop spins forever, so the failure it
        # prevents is a hung `grid leave`, not a wrong answer (measured by deleting it).
        ancestor = parents.get(pid)
        while ancestor in ours and ancestor not in shims:
            shims.add(ancestor)
            ancestor = parents.get(ancestor)
    return frozenset(shims)


def _has_exited(pid: int) -> bool:
    """Whether ``pid`` has stopped running, allowing a bounded settle first — a process that was just
    signalled is briefly still there (Windows ``TerminateProcess`` is asynchronous). Probing the
    instant a tree kill returns would report a successful teardown as a survivor and hand the operator
    a kill command for a pid that is already dying — or, by then, belongs to something else.

    The cheap ``pid_alive`` drives the loop and the zombie discrimination is asked **once**, at the
    end: on macOS the richer probe is a ``ps`` fork, and this polls twenty times. Asking it at all is
    what stops a child that exited into a container with no reaper — where a corpse answers "alive"
    forever — from being reported as a survivor `grid leave` failed to stop (grid-leave issue 08).
    """
    for _ in range(_SETTLE_POLLS):
        if not run_records.pid_alive(pid):
            return True
        time.sleep(_SETTLE_INTERVAL_SECONDS)
    return run_records.stopped_running(pid)


class ScanResult(NamedTuple):
    """The outcome of one detect-only ``find_orphans`` pass. A NamedTuple for the same reason
    ``SweepResult`` is: ``scanned`` and ``partial`` are both booleans meaning nearly-opposite things
    ("I read the table" / "I read only part of it"), and a transposed positional unpack would be
    invisible to a type checker."""

    found: dict[str, tuple[int, ...]]  # grid id -> live serve-child pids; ABSENT when there are none
    scanned: bool = True               # False ⇒ the process table couldn't be read at all
    unreadable: int = 0                # rows we were not permitted to read a command line for

    @property
    def partial(self) -> bool:
        """Whether this scan saw enough of the box to vouch for it — see ``_is_partial_scan``.

        Load-bearing here in a way it is not for a sweep: an unreadable process yields a sentinel row
        carrying no marker, so a serve child hiding behind one can never appear in ``found``. Without
        this flag, "no hit" from a mostly-hidden table is indistinguishable from "nothing is serving".
        """
        return _is_partial_scan(self.unreadable)


def find_orphans(
    marker: str, network_ids: Iterable[str], *, exclude_pids: set[int] | frozenset[int] = frozenset()
) -> ScanResult:
    """Live serve children per network id, from ONE process-table read. Nothing is signalled.

    The detect-only half of the sweep, for a caller that must decide *which* grids to act on before
    it acts on any of them (``grid logout``, and the sync/login warnings). ``sweep_orphans`` cannot
    answer that: it terminates what it finds.

    One read for all ids rather than a call per grid, because the cost is per *read*, not per match:
    a cold ``powershell.exe`` + ``Win32_Process`` enumeration is seconds on Windows (see
    ``_WIN_PROCESS_TIMEOUT_SECONDS``), so a per-grid loop would tax a sign-out in proportion to how
    many grids the box has ever joined. The matcher itself is pure and cheap.

    A grid with no live child is **absent** from ``found`` rather than mapped to an empty tuple, so a
    caller can write ``if nid in found``. ``scanned`` is False when the table couldn't be read at all —
    the same "couldn't check ≠ checked, clean" contract as ``SweepResult.scanned``, and the reason this
    returns a flag instead of an empty dict. ``partial`` is the narrower version of the same warning
    and matters *more* here than it does to a sweep: a sweep at least terminates what it can see,
    whereas this answers "does this grid need tearing down at all", and a caller that reads a bare
    empty ``found`` off a mostly-hidden table walks past the one grid that did.
    """
    output = _process_table_output()
    if output is None:
        return ScanResult({}, scanned=False)  # couldn't read the table — couldn't check
    excluded = frozenset(exclude_pids) | {os.getpid()}
    found: dict[str, tuple[int, ...]] = {}
    for network_id in network_ids:
        matched = _match_orphan_pids(output, marker, network_id, exclude_pids=excluded)
        if matched:
            found[network_id] = tuple(matched)
    return ScanResult(found, unreadable=_count_unreadable(output))


class Scan(NamedTuple):
    """One process-table read, matched but not yet acted on — the halfway point ``sweep_orphans``
    passes through and a caller that must decide *per match* stops at.

    It exists so a caller can consult other state **between** the read and the kills, which is the
    only order in which that state is trustworthy: `cli/local_leave` re-reads the run records here, and
    because a local `grid join` writes the record *before* it spawns the child, every child in this
    table already has a record in a snapshot taken after this read. Re-reading first would leave
    exactly the hole the check exists to close — and local leave, unlike remote's, holds no file lock
    to close it any other way.
    """

    matches: dict[int, str]        # pid -> engine id, in process-table order
    ppid_by_pid: dict[int, int]    # for `_tree_roots`: which matches are descendants of other matches
    unreadable: int                # rows we were not permitted to read a command line for


def scan_engine_children(
    marker: str, *pinned: str, exclude_pids: set[int] | frozenset[int] = frozenset()
) -> Scan | None:
    """Read the process table once and match this grid's engine children, signalling nothing.
    ``None`` when the table couldn't be read at all — "couldn't check" is never "checked, clean"."""
    output = _process_table_output()
    if output is None:
        return None
    excluded = frozenset(exclude_pids) | {os.getpid()}
    return Scan(
        _match_engine_children(output, marker, *pinned, exclude_pids=excluded),
        _ppid_by_pid(output),
        _count_unreadable(output),
    )


def sweep_orphans(
    marker: str, *pinned: str, exclude_pids: set[int] | frozenset[int] = frozenset()
) -> SweepResult:
    """Terminate every detached engine child of this grid in the process table, classifying each
    matched pid. A process table that can't be read returns an empty result with ``scanned=False``
    (leave degrades and says so, never crashes).

    ``exclude_pids`` (the caller's recorded pids, plus their launcher shims via
    ``launcher_ancestors``) and this process's own pid are never matched, so a wedged recorded child
    the record loop just failed to kill is not escalated a second time, and the ``grid leave`` process
    never targets itself. Each match runs through the existing ``run_records.terminate_pid``
    escalation: confirmed dead → ``reaped``, survives → ``survivors``; a match owned by another user
    raises ``PermissionError`` (``terminate_pid`` does not catch EPERM) → ``foreign``, reported but
    never fatal. On Windows ``foreign`` stays empty by construction — see the module docstring.

    Terminates **every** match. A caller that must spare some of them stops at
    ``scan_engine_children`` and calls ``terminate_matches`` itself.
    """
    scan = scan_engine_children(marker, *pinned, exclude_pids=exclude_pids)
    if scan is None:
        return SweepResult((), (), (), scanned=False)  # couldn't read the table — couldn't check
    return terminate_matches(scan, scan.matches)


def terminate_matches(scan: Scan, pids: Iterable[int]) -> SweepResult:
    """Terminate ``pids`` (a subset of ``scan.matches``) through the record teardown's own
    escalation, classifying each. See ``sweep_orphans`` for what each classification means."""
    targets, descendants = _tree_roots(list(pids), scan.ppid_by_pid)
    unreadable = scan.unreadable
    reaped: list[int] = []
    survivors: list[int] = []
    foreign: list[int] = []
    deadline = time.monotonic() + _SWEEP_DEADLINE_SECONDS

    def terminate(pid: int) -> None:
        # PID-reuse TOCTOU: ``terminate_pid`` has an up-to-25s window between "alive" and its SIGKILL,
        # so on a busy box a matched pid could exit and be recycled before its turn — the same race
        # ``run_records.terminate_pid`` / ``_hot_reload_identity`` already carry, "not fully fixable
        # without pidfd". Accepted here too; a Linux ``pidfd_send_signal`` hardening is the future fix.
        #
        # Note for grid-leave issue 08 (the ``(pid, starttime)`` identity token): a pid reached here
        # came from the process table, NOT from a run record, so it deliberately carries no token —
        # the sweep is identity-proof by argv content instead. If the token ever becomes *required*
        # rather than optional, this call silently stops killing anything.
        try:
            (reaped if run_records.terminate_pid(pid) else survivors).append(pid)
        except PermissionError:
            foreign.append(pid)  # another user's process — report, never die
        except (Exception, SystemExit) as exc:
            # The kill itself could not be carried out — `taskkill` missing from a stripped Windows
            # image is the concrete case. Classify here rather than letting it escape: the caller's
            # guard would substitute an EMPTY result, throwing away every survivor this pass had
            # already positively identified and turning a loud, correct failure into "Left …" plus a
            # footnote blaming the process table. This pid is alive and we did not stop it, which is
            # exactly what `survivors` means.
            print(f"Note: couldn't stop serve child pid {pid} ({exc}).", file=sys.stderr)
            survivors.append(pid)

    for index, pid in enumerate(targets):
        if time.monotonic() >= deadline:
            # Out of budget: stop rather than keep the deregister waiting (see
            # `_SWEEP_DEADLINE_SECONDS`). Report only what is still ALIVE — a pid we never got to may
            # well have exited on its own, and naming a dead one would tell the operator to `kill -9`
            # a pid that by now belongs to something else.
            survivors.extend(
                pid for pid in targets[index:] + descendants if not run_records.stopped_running(pid)
            )
            return SweepResult(tuple(reaped), tuple(survivors), tuple(foreign), unreadable=unreadable)
        terminate(pid)
    # Terminating a root was *supposed* to take its descendants with it. "Supposed to" is not
    # evidence, and on POSIX it is usually not even true: ``terminate_pid`` reaches ``killpg`` only
    # after the target ignores SIGTERM for the full grace, so a root that exits promptly signals
    # nothing. Anything still standing gets its own escalation rather than a report — reporting alone
    # would turn a case the sweep used to finish in one pass into a loud failure needing a retry.
    for index, pid in enumerate(descendants):
        if time.monotonic() >= deadline:
            survivors.extend(p for p in descendants[index:] if not run_records.stopped_running(p))
            break
        if not _has_exited(pid):
            terminate(pid)
    return SweepResult(tuple(reaped), tuple(survivors), tuple(foreign), unreadable=unreadable)
