"""The Windows half of the `grid leave` argv sweep, exercised against the REAL process table.

Why a separate file (`.scratch/grid-leave/` issue 06). The sweep's Windows enumerator shells out to
PowerShell and asks WMI for every process's command line. Its whole surface — the `-EncodedCommand`
round-trip, the console/ANSI code-page decode, the per-row ASCII fold, PowerShell's habit of exiting
0 on a non-terminating error, and `taskkill /F /T` — is exactly the part a mocked `subprocess.run`
cannot vouch for. `tests/test_local_cli.py` covers the branch logic on any platform by flipping
`orphan_sweep.sys.platform`; this file is the only thing that proves the command itself works, so it
is what the `test-windows` CI job runs.

Scope, stated plainly: this proves the sweep at the ``sweep_orphans`` layer, not ``grid leave``
end-to-end. The end-to-end real-child test (`tests/test_remote_leave_real_child.py`) stays skipped on
Windows — its SIGTERM → 25s grace → `killpg` teardown has no Windows equivalent, which is a property
of the assertions, not of the sweep.
"""
from __future__ import annotations

import contextlib
import os
import subprocess
import sys

import pytest

from shared import orphan_sweep
from shared import run_records

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="exercises the real Windows process table (PowerShell/CIM + taskkill); the branch logic "
           "itself is covered on every platform by tests/test_local_cli.py",
)


def _network_id(tag: str) -> str:
    """A network id nothing else on this machine can be serving. The sweep reads the WHOLE host
    process table, so a shared id would let it match — and kill — a developer's or a CI neighbour's
    real `grid join` child (the trap measured while building the POSIX real-child test)."""
    return f"itest-{os.getpid()}-{tag}"


@contextlib.contextmanager
def _marked_child(network_id: str):
    """A harmless long-lived process whose command line carries the real spawn marker, so
    `Win32_Process.CommandLine` shows `… __remote-engine <network_id> remote` exactly as a detached
    serve child's would. It sleeps rather than serving anything: this file tests the sweep, not the
    engine.

    Teardown kills by pid. `start_new_session=True` is silently IGNORED on Windows (CPython passes it
    through as `unused_start_new_session`), so this is an ordinary child of the pytest process — a
    process-group kill would take the test runner with it.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)",
         run_records.REMOTE_ENGINE_MARKER, network_id, "remote"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield proc
    finally:
        with contextlib.suppress(OSError):
            proc.kill()
        proc.wait(timeout=30)  # reap our handle so the pid can't be misread as live


def test_process_identity_reads_a_real_windows_process():
    """The Windows `ctypes` half of the identity probe, against the real kernel (grid-leave issue 08).

    Everything else that covers `shared/process_identity`'s Windows branch runs on Linux/macOS against
    a hand-written `_FakeKernel32`, which by construction cannot catch a wrong `wintypes` field name,
    a bad bit-shift, a `restype` truncation, or a handle leak. This is the only place the real
    `OpenProcess`/`GetProcessTimes`/`CloseHandle` sequence executes — and the identity token it
    produces is what decides whether `grid leave` may signal a process, so "it compiles" is not enough.

    Uses the live marked child this file already spawns rather than the pytest process itself: a
    parent reading its own creation time exercises none of the open-a-foreign-process path.
    """
    from shared import process_identity

    with _marked_child(_network_id("identity")) as proc:
        token = process_identity.process_start_time(proc.pid)
        assert token, "no creation time for a process we are looking straight at"
        assert token == process_identity.process_start_time(proc.pid), "the token is not stable"
        # Windows has no POSIX-style zombie, so this must be False for a live process AND cost
        # nothing — `record_verdict` calls it on every record.
        assert process_identity.is_zombie(proc.pid) is False
        # A pid the kernel has no process for yields no token — never a placeholder, which is the
        # rule the whole upgrade path rests on.
        assert process_identity.process_start_time(process_identity.PID_MAX) is None
        # Windows has no process groups here, so the group backstop is inert by construction.
        assert process_identity.process_group_of(proc.pid) is None
        assert process_identity.group_alive(proc.pid) is False

    # Repeated across a killed-and-reaped pid: proves the handle is released each time rather than
    # leaking one per probe (this runs inside `terminate_pid`'s wait loop in production).
    for _ in range(50):
        process_identity.process_start_time(proc.pid)


def test_win_process_output_returns_many_parseable_rows():
    """The raw enumerator, before any matching. Two things are asserted that only a real run can show:
    the output parses as `<pid> <ppid> <command line>` rows, and there are MANY of them.

    The row count is the guard for the ASCII fold. `-replace '[^\\x20-\\x7E]'` applied to the joined
    output instead of per command line would eat CR/LF as well, collapsing the entire table into a
    single line — after which the matcher reads one row and silently finds at most one pid. A mocked
    `subprocess.run` supplies canned rows, so it can never catch that; a live Windows box always has
    dozens of processes with readable command lines.
    """
    output = orphan_sweep._win_process_output()
    assert output is not None, "the process table could not be read at all"

    rows = [line.split(None, 2) for line in output.splitlines() if line.strip()]
    parsed = [row for row in rows if len(row) == 3 and row[0].isdecimal() and row[1].isdecimal()]
    assert len(parsed) >= 2, f"expected many rows, got {len(parsed)} of {len(rows)}: {output[:400]!r}"


def test_win_process_output_keeps_a_long_command_line_on_one_row():
    """The assertion the row count cannot make. PowerShell's host writer wraps pipeline output at the
    host width (120 with no console, and `CREATE_NO_WINDOW` guarantees no console), which is why the
    script writes through `[Console]::Out` instead. If that ever regresses, a long row splits and the
    continuation line has no leading pid — and since the marker and network id are the LAST tokens on
    a serve child's command line, the sweep would go blind to exactly what it hunts while `scanned`
    stayed True. A row count survives that: dozens of short rows still parse.

    So: spawn a child whose command line is deliberately far past any plausible wrap width, and
    require it to come back whole on a single row.
    """
    network_id = _network_id("wide")
    # The padding goes in the `-c` payload, not after the marker: the matcher requires the marker to
    # be followed by EXACTLY two arguments, so a trailing argv would (correctly) stop this looking
    # like a serve child. A real child's row is long for the same reason this one is — a long
    # interpreter path in front of the tokens that matter.
    payload = "import time; time.sleep(120)  # " + "x" * 400
    proc = subprocess.Popen(
        [sys.executable, "-c", payload,
         run_records.REMOTE_ENGINE_MARKER, network_id, "remote"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        output = orphan_sweep._win_process_output()
        assert output is not None
        ours = [line for line in output.splitlines() if line.split(None, 1)[:1] == [str(proc.pid)]]
        assert len(ours) == 1, f"expected exactly one row for pid {proc.pid}, got {len(ours)}: {ours!r}"
        row = ours[0]
        assert len(row) > 400, f"row looks truncated at {len(row)} chars: {row!r}"
        # The tokens the matcher needs must survive intact, adjacent, and in order.
        tokens = row.split()
        assert tokens[tokens.index(run_records.REMOTE_ENGINE_MARKER) + 1] == network_id
    finally:
        with contextlib.suppress(OSError):
            proc.kill()
        proc.wait(timeout=30)


def test_sweep_orphans_reaps_a_live_marked_child():
    """Issue 06 AC-1, the stale-pid regime's core: a live serve child that no run record can reach is
    found by argv alone and terminated. This is the Windows twin of the POSIX case that stranded five
    real orphans for 7-23 days."""
    network_id = _network_id("reap")
    with _marked_child(network_id) as proc:
        result = orphan_sweep.sweep_orphans("__remote-engine", network_id)

        assert result.scanned is True
        assert result.reaped == (proc.pid,), f"unexpected sweep result: {result}"
        assert result.survivors == () and result.foreign == ()
        assert run_records.pid_alive(proc.pid) is False


def test_sweep_orphans_leaves_another_grids_child_alone():
    """The negative control, and the reason the matcher compares the network id as a whole token: a
    child of a DIFFERENT grid is in the same process table, matches the same marker, and must survive
    untouched. Without this, the test above would also pass for a sweep that kills everything."""
    network_id = _network_id("control")
    with _marked_child(network_id) as proc:
        result = orphan_sweep.sweep_orphans("__remote-engine", _network_id("someone-else"))

        assert result.scanned is True
        # Field by field, not a whole-tuple comparison against a default-constructed result: on a
        # real box `unreadable` is a live measurement of what WMI would not show us (see below), so
        # equality here would assert that number is zero — which it never is, on any Windows.
        assert result.reaped == () and result.survivors == () and result.foreign == ()
        assert run_records.pid_alive(proc.pid) is True


def test_the_enumerator_emits_a_sentinel_for_a_process_it_cannot_read():
    """Windows hands an unelevated caller a **null** command line for any process it cannot open, and
    the enumerator used to drop those rows before anything saw them — so the sweep was blind to
    another user's serve child while still reporting `scanned=True`, and `grid leave` printed an
    unqualified success over it (grid-leave issue 15/B).

    Every Windows has processes that are opaque to everyone (pid 0, pid 4, Registry, Memory
    Compression, Secure System, anything protected), so at least one sentinel must come back here. A
    zero would not mean "we can see everything" — it would mean the sentinel branch is dead and the
    count that qualifies the success line is permanently 0, which is the silent failure this whole
    issue is about. That is why it is asserted rather than merely measured."""
    output = orphan_sweep._win_process_output()
    assert output is not None, "the process table could not be read at all"

    sentinels = [
        line for line in output.splitlines()
        if line.split(None, 2)[2:] == [orphan_sweep._WIN_UNREADABLE_MARKER]
    ]
    assert sentinels, f"no unreadable process was reported at all: {output[:400]!r}"
    # ...and they are inert: a sentinel carries no marker, so it can never become a kill.
    assert orphan_sweep._match_orphan_pids("\n".join(sentinels), "__remote-engine", _network_id("x"), exclude_pids=set()) == []


def test_the_unreadable_row_count_stays_under_the_qualifier_floor():
    """The floor's empirical pin — the one number in this feature that only a real Windows box can
    supply, which is why issue 15/B could not be closed until this job existed.

    `_WIN_UNREADABLE_FLOOR` has to sit above what is unreadable to *everyone* and below what a caller
    who cannot see another user's processes would find. This runner is an administrator, so what it
    measures IS that lower baseline. If the assertion fails, the message carries the measured number
    and the fix is that one constant — not the predicate.

    What this proves is one-directional: the floor is not too LOW, so no Windows leave is footnoted
    for a table it could in fact see. That it is not too HIGH is `test_sweep_result_is_only_partial_
    above_the_floor`'s job, because no CI runner can produce a genuinely half-hidden table on demand.
    """
    output = orphan_sweep._win_process_output()
    assert output is not None, "the process table could not be read at all"

    unreadable = orphan_sweep._count_unreadable(output)
    assert unreadable >= 1, "the sentinel branch produced nothing — the count can never qualify anything"
    assert unreadable <= orphan_sweep._WIN_UNREADABLE_FLOOR, (
        f"an administrator could not read {unreadable} rows, at or above the "
        f"_WIN_UNREADABLE_FLOOR of {orphan_sweep._WIN_UNREADABLE_FLOOR} — every Windows `grid leave` "
        "would now print a partial-scan caveat. Raise the floor to clear this baseline."
    )
    assert orphan_sweep.sweep_orphans("__remote-engine", _network_id("floor")).partial is False
