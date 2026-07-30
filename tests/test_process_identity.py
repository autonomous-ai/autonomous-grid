"""``shared.process_identity`` — the probes that stop the CLI trusting a bare pid.

Why this file exists (`.scratch/grid-leave/` issue 08, PRD follow-up F6). ``os.kill(pid, 0)`` reports
a **zombie** as alive, and in a container whose PID 1 never reaps, every serve-child death mints one.
The grid then lies twice: ``grid join`` prints "Already serving …; nothing to append." over a dead
engine, and every teardown burns its full 25s stop grace before declaring a phantom survivor.

So the zombie regime is tested with a **real zombie**, not a mock: spawn a child, let it exit, and
never ``wait()`` it — this test process is its parent, so the corpse stays in the process table
deliberately. (``tests/test_remote_leave_real_child.py`` runs a reaper thread to *avoid* exactly
this; here it is the point.) Everything that parses foreign-platform output is tested separately as
a pure function over canned text, so the Linux and Windows branches are covered from macOS too.
"""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from shared import process_identity, run_records

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the real-process regimes assert POSIX zombie semantics, which Windows has no equivalent "
           "of; the Windows branches are covered by the pure-parser tests in this file",
)


def _raw_state(pid: int) -> str:
    """The kernel's own answer for ``pid``'s state, asked WITHOUT going through the code under test,
    so a fixture that failed to produce a zombie is distinguishable from a probe that failed to see
    one. Deliberately duplicates a line of ``process_identity`` rather than importing it."""
    if sys.platform == "linux":
        try:
            content = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return ""
        return content[content.rindex(")") + 2:].split()[0]
    out = subprocess.run(
        ["ps", "-p", str(pid), "-o", "state="], capture_output=True, text=True
    ).stdout.strip()
    return out[:1]


@pytest.fixture
def zombie():
    """A **real** zombie pid: a child that has exited and that nobody has reaped. Yields the pid; the
    fixture reaps it afterwards so the corpse never outlives the test.

    Waits on **pipe EOF**, not ``poll()``/``wait()`` — both of those reap, which is precisely the
    state this fixture must not reach. The child's stdout closes as it exits, so ``read()`` returns
    at the moment it becomes a corpse. (``os.waitid(..., WNOWAIT)`` would say this more directly but
    CPython does not expose it on macOS.)
    """
    proc = subprocess.Popen([sys.executable, "-c", ""], stdout=subprocess.PIPE)
    try:
        assert proc.stdout.read() == b""  # blocks until the child exits; does not reap it
        deadline = time.monotonic() + 5.0
        while _raw_state(proc.pid) != "Z" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert _raw_state(proc.pid) == "Z", (
            f"the fixture never produced a zombie (pid {proc.pid} state "
            f"{_raw_state(proc.pid)!r}); nothing below would be testing what it claims"
        )
        yield proc.pid
    finally:
        proc.stdout.close()
        proc.wait()


def test_a_real_zombie_reads_as_a_zombie_while_pid_alive_still_calls_it_alive(zombie):
    """The lie this whole issue exists to stop, pinned from both sides.

    ``pid_alive`` deliberately keeps its old answer — 55 tests in the suite fake it to fabricate
    liveness, and changing it would silently reach the orphan sweep's exclusion logic. The new probe
    is what tells a corpse from a running engine.
    """
    assert run_records.pid_alive(zombie) is True, (
        "os.kill(pid, 0) is supposed to still report a zombie as alive — that is the lie the new "
        "probe exists to correct, and this assertion is what proves the probe is load-bearing"
    )
    assert process_identity.is_zombie(zombie) is True


# `/proc/<pid>/stat` fields 3..22 for a normal process: state, ppid, pgrp, session, tty_nr, tpgid,
# flags, min/cmin/maj/cmajflt, utime, stime, cutime, cstime, priority, nice, num_threads,
# itrealvalue, starttime. Twenty tokens, so `starttime` (field 22) sits at index 19.
_PROC_TAIL = "1 1234 1234 0 -1 4194304 100 0 0 0 5 3 0 0 20 0 1 0 987654321"


@pytest.mark.parametrize(
    ("comm", "state"),
    [
        ("python3", "S"),
        # The comm is arbitrary bytes chosen by the process itself, and it is NOT escaped: a
        # `prctl(PR_SET_NAME)` of `weird ) name` produces exactly this. Splitting on the FIRST `)`,
        # or on whitespace, walks the field index off by one and reads `starttime` out of the middle
        # of the comm — so a hostile (or merely creative) process name would silently poison every
        # identity comparison on Linux.
        ("weird ) name", "S"),
        ("a )( b", "Z"),
        ("python3", "Z"),
    ],
)
def test_linux_proc_stat_is_parsed_after_the_last_paren(comm, state):
    """The Linux probe reads state + start time out of one ``/proc/<pid>/stat`` line, indexing from
    the **last** ``)`` — pure, so the Linux branch is covered from macOS too."""
    assert process_identity._parse_proc_stat(
        f"1234 ({comm}) {state} {_PROC_TAIL}\n"
    ) == (state, "987654321")


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("Z    Tue Jul 28 05:23:07 2026\n", ("Z", "Tue Jul 28 05:23:07 2026")),
        ("  S   Tue Jul 28 05:23:07 2026  ", ("S", "Tue Jul 28 05:23:07 2026")),
        # BSD decorates the state column — `S+` foreground, `Ss` session leader, `R<` raised priority.
        # Only the leading character is the state; treating the whole token as one would stop a
        # decorated zombie (`Z+`) from ever being recognised.
        ("S+ Tue Jul 28 05:23:07 2026", ("S", "Tue Jul 28 05:23:07 2026")),
        ("Ss Tue Jul 28 05:23:07 2026", ("S", "Tue Jul 28 05:23:07 2026")),
        ("", None),           # `ps` printed nothing — no such pid
        ("   \n", None),      # ...and whitespace is not a process either
        ("Z\n", None),        # a state with no start time is half an answer, which is no answer
    ],
)
def test_macos_ps_output_is_parsed_into_state_and_an_opaque_token(output, expected):
    """The macOS probe's parser, pure so it runs on every platform. The start time is taken verbatim
    and never parsed into a date — it is only ever compared for equality against a token stamped
    earlier, so its format is deliberately opaque."""
    assert process_identity._parse_ps_fields(output) == expected


def test_start_time_is_readable_and_stable_for_a_live_process():
    """The identity token's second half. It must be **stable** — re-reading it may not produce a new
    value, or every verification after the stamp would read as a recycled pid."""
    token = process_identity.process_start_time(os.getpid())
    assert token
    assert process_identity.process_start_time(os.getpid()) == token


def test_start_time_is_still_readable_for_a_zombie(zombie):
    """Load-bearing for the terminator: a corpse must be provable as *ours* before its process group
    is signalled. If the token vanished with the process, the zombie regime — the one this issue was
    filed about — could never be verified and would fall back to not acting."""
    assert process_identity.process_start_time(zombie)


def test_start_time_is_none_for_a_pid_that_does_not_exist():
    """Unreadable is ``None``, never a placeholder. A sentinel string would compare equal across
    unrelated processes and turn every unverifiable record into a confident (wrong) match."""
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()  # exited AND reaped — the pid names nothing
    assert process_identity.process_start_time(proc.pid) is None


def test_windows_has_no_zombies_and_an_unopenable_process_yields_no_token(monkeypatch):
    """The Windows decision logic, pinned off Windows (the ctypes call itself rides the `test-windows`
    CI job). Two rules: there is no POSIX-style zombie on Windows, so `is_zombie` can never be True and
    must not cost a syscall; and a process we could not open yields **no token** rather than a
    fabricated one — unverifiable, so the terminator declines to signal instead of guessing."""
    import ctypes

    class _FakeKernel32:
        def OpenProcess(self, *a):
            return 0  # NULL — access denied, or no such pid; either way we cannot read a start time

    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **k: _FakeKernel32(), raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    monkeypatch.setattr(process_identity, "_IS_WINDOWS", True)

    assert process_identity.is_zombie(4242) is False
    assert process_identity.process_start_time(4242) is None


def test_windows_start_time_is_the_creation_filetime(monkeypatch):
    """A readable Windows process yields its creation FILETIME as the token, and still never reads as
    a zombie."""
    monkeypatch.setattr(process_identity, "_IS_WINDOWS", True)
    monkeypatch.setattr(process_identity, "_win_creation_filetime", lambda pid: 133_000_000_000_000_000)

    assert process_identity.process_start_time(4242) == "133000000000000000"
    assert process_identity.is_zombie(4242) is False


# A session leader that spawns a grandchild and then exits — the exact shape of a `grid join` whose
# launcher shim (the Nuitka `--onefile` bootstrap, the uv trampoline) hands off to the real serve
# process and steps out of the way. `start_new_session=True` makes the leader's pid the group id.
_LEADER_SOURCE = (
    "import os, subprocess, sys, time; "
    "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
    "print(os.getpgrp(), p.pid, flush=True)"
)


@pytest.fixture
def orphaned_group():
    """``(pgid, grandchild_pid)`` for a process group whose **leader has exited and been reaped**,
    leaving one live member behind. Reaped by the fixture."""
    leader = subprocess.Popen(
        [sys.executable, "-c", _LEADER_SOURCE],
        start_new_session=True, stdout=subprocess.PIPE, text=True,
    )
    pgid, grandchild = (int(token) for token in leader.stdout.readline().split())
    leader.wait()  # the leader is now gone AND reaped: nothing of it is left in the process table
    try:
        yield pgid, grandchild
    finally:
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGKILL)


def test_a_process_group_outlives_its_reaped_leader(orphaned_group):
    """The property the `ps`-free reap rests on (`.scratch/grid-leave/` issue 08, residual (b)).

    When a `grid leave` wins the record lock before the child self-stamps, the record names the
    spawner's pid — a launcher shim — while the real serve process is a *member* of that shim's
    group. POSIX will not recycle a group id while the group has members, so the recorded pgid still
    names our own descendants even after the leader is gone. Without this, the only backstop is the
    argv sweep, which needs a readable process table.
    """
    pgid, grandchild = orphaned_group
    assert not run_records.pid_alive(pgid), "the leader must be gone for this to prove anything"
    assert process_identity.group_alive(pgid) is True
    assert run_records.pid_alive(grandchild) is True

    os.killpg(pgid, signal.SIGKILL)
    deadline = time.monotonic() + 5.0
    while process_identity.group_alive(pgid) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert process_identity.group_alive(pgid) is False


def test_group_alive_reports_our_own_group():
    """A sanity anchor for the probe's polarity: this test process is in a live group."""
    assert process_identity.group_alive(os.getpgrp()) is True


@pytest.mark.parametrize("pgid", [0, -1, -4242, 2**31, 2**63])
def test_group_alive_never_asks_the_kernel_about_a_pgid_that_is_not_one(monkeypatch, pgid):
    """Defence in depth for the worst outcome in this whole feature.

    ``killpg(0, sig)`` addresses **the caller's own process group**, so a run record carrying
    ``pgid: 0`` — which the same join write-race that leaves ``pid: 0`` produces — would make the
    group backstop SIGKILL `grid leave` itself *and the operator's shell job*. A negative value is
    not a group id either, and an out-of-range one makes ``os.killpg`` raise ``OverflowError``, an
    ``ArithmeticError`` that no caller's ``OSError`` handler catches — so a teardown would crash
    ahead of the relay deregister that is the mechanism of record.

    None of them can name a real group, so all of them answer "no group" **before** any syscall.
    ``run_records.recorded_pgid`` refuses the same values at the record boundary; this is the second
    lock on the same door.
    """
    def _explode(*_a, **_k):
        raise AssertionError(f"group_alive({pgid}) reached the kernel — killpg must never see this")

    monkeypatch.setattr(process_identity.os, "killpg", _explode)
    assert process_identity.group_alive(pgid) is False


def test_terminating_a_real_zombie_confirms_immediately_and_signals_nothing(zombie, monkeypatch):
    """Criterion 3, against a real corpse rather than a mock.

    Before this, ``terminate_pid`` believed ``pid_alive``: it SIGTERMed the zombie, waited out the
    whole 25s grace, SIGKILLed it, still read it as alive, and returned "not confirmed". A
    union-changing join or an ``--engine`` shrink then aborted with "Could not stop the engine(s) …",
    and on this branch the honest-teardown gate turned a bare `grid leave` into a **permanently**
    failing retry loop — the argv sweep can never match a zombie, whose command line is empty.

    A zombie is not a running process: there is nothing to signal, nothing to wait for, and nothing
    of ours left to reap.
    """
    signalled = []
    real_kill = os.kill

    def _spy(pid, sig):
        # Signal 0 delivers nothing — it is how `pid_alive` asks whether a pid exists — so it is let
        # through to the kernel rather than counted. Everything else is a real signal.
        if sig:
            signalled.append((pid, sig))
            return
        real_kill(pid, sig)

    monkeypatch.setattr(run_records.os, "kill", _spy)
    monkeypatch.setattr(run_records, "kill_group", lambda pid: signalled.append((pid, "group")))

    started = time.monotonic()
    assert run_records.terminate_pid(zombie) is True
    elapsed = time.monotonic() - started

    assert signalled == [], "a corpse was signalled — there is no process there to receive it"
    assert elapsed < 5.0, (
        f"took {elapsed:.1f}s: the teardown burned its stop grace on a process that had already "
        f"exited (the grace is {run_records._STOP_GRACE_SECONDS}s)"
    )


def test_identity_stamp_describes_this_process_and_round_trips_as_ours():
    """What a spawner and a serve child write into a run record, and the proof it verifies.

    All three fields go in **together**, in one atomic record write. That is what lets a verified
    `pid` vouch for the `pgid` beside it: the group backstop can then trust a group id it has no
    other way to authenticate.
    """
    stamp = run_records.identity_stamp(os.getpid())

    assert stamp["pid"] == os.getpid()
    assert stamp["pgid"] == os.getpgrp()
    assert stamp["pid_start_time"]
    assert run_records.record_verdict(stamp) is run_records.RecordVerdict.LIVE_OURS


def test_identity_stamp_leaves_an_unreadable_start_time_absent_rather_than_placeholding():
    """The rule that keeps every token-less record working, and the one that would break all of them
    at once if it were got wrong.

    A record whose start time could not be read must carry ``None`` — not ``""``, ``"unknown"`` or any
    other sentinel. A sentinel compares *equal* across unrelated processes, so it would turn "we
    cannot tell" into a confident match; and it would make every hand-built ``{"pid": 4242}`` record
    in this suite read as a **mismatch** against a real live process, which is the verdict that
    refuses to signal.
    """
    stamp = run_records.identity_stamp(_reaped_pid())

    assert stamp["pid_start_time"] is None
    assert "unknown" not in repr(stamp)


def _reaped_pid() -> int:
    """A pid that certainly names nothing: a child run to completion and reaped."""
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid
