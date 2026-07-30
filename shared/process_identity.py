"""Process **identity** and **state** — the probes that stop the CLI trusting a bare pid.

``os.kill(pid, 0)`` answers "does this pid exist", which is not the question any run-record seam
actually asks. It says *alive* for a **zombie**, and in a container whose PID 1 never reaps
(`.scratch/grid-leave/` PRD, field confirmation #2) every serve-child death mints one — so `grid
join` printed "Already serving …; nothing to append." over a dead engine while `grid models` stayed
empty, and every teardown burned its full 25s stop grace before declaring a phantom survivor. It also
cannot tell *the process we spawned* from *an unrelated process that inherited its recycled pid*,
which is how a stale run record turns a `SIGHUP` hot-reload into a signal — SIGHUP's default
disposition is **terminate** — delivered to one of the operator's own processes.

This module answers the two questions that fix both (`.scratch/grid-leave/` issue 08):

* ``is_zombie(pid)`` — is this pid a corpse rather than a running process?
* ``process_start_time(pid)`` — the kernel's start time for this pid, an **opaque token**. Paired
  with the pid it forms the ``(pid, start_time)`` identity a run record stamps, so a recycled pid is
  detectable: same number, different start time.
* ``group_alive(pgid)`` — does this POSIX process group still have members? A group id cannot be
  recycled while the group lives, so this is a liveness probe that needs no ``ps``.

Deliberately **not** here: ``pid_alive``. It stays in ``shared.run_records`` unchanged, because it is
the seam 55 tests in the suite monkeypatch to fabricate liveness, and because widening it would
silently reach the orphan sweep's pid-exclusion logic. Everything here is *additive*: a pid this
module cannot read is reported as "not a zombie" and "no token", i.e. exactly the behaviour the CLI
had before — an unreadable process must never become a *new* failure.
"""
from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

_IS_WINDOWS = sys.platform == "win32"
_IS_LINUX = sys.platform == "linux"

# The largest value any OS hands out as a pid (or a process-GROUP id, which is the pid of the group's
# leader) fits in a C int. Anything outside ``1..PID_MAX`` cannot name one, and every way of finding
# that out at the syscall is worse than answering here:
#   * a value above it makes ``os.kill``/``os.killpg`` raise **OverflowError** — an ``ArithmeticError``,
#     not an ``OSError``, so no caller's handler catches it and a teardown crashes ahead of the relay
#     deregister that is the mechanism of record;
#   * a **negative** pid is not a process — ``os.kill(-N, sig)`` addresses process GROUP N;
#   * **zero** is worse still — ``os.kill(0, sig)`` and ``os.killpg(0, sig)`` address **the caller's
#     own process group**, so a record carrying ``pgid: 0`` (the same write race that leaves
#     ``pid: 0``) would make `grid leave` SIGKILL itself and the operator's shell job.
PID_MAX = 2**31 - 1


def is_addressable(pid: int) -> bool:
    """Whether ``pid`` could name a real process or process group, i.e. is safe to hand to a signal
    call at all. Checked before every syscall in this module and mirrored at the record boundary by
    ``run_records.recorded_pid``/``recorded_pgid``."""
    return isinstance(pid, int) and not isinstance(pid, bool) and 0 < pid <= PID_MAX

# `ps -p <pid>` is a fork, and the callers poll. One second is generous for a single-pid query and
# short enough that a wedged `ps` cannot add up across a 25s stop grace (the callers additionally
# rate-limit how often they ask — see `run_records._ZOMBIE_PROBE_INTERVAL_SECONDS`).
_PS_TIMEOUT_SECONDS = 1


# Index of `starttime` (`/proc/<pid>/stat` field 22) among the fields that FOLLOW the comm. The
# fields after the closing paren start at 3 (state), so field 22 sits at 22 - 3 = 19.
_PROC_STARTTIME_INDEX = 19


def _parse_proc_stat(content: str) -> tuple[str, str] | None:
    """``(state, start_time)`` out of one ``/proc/<pid>/stat`` line, or ``None`` when it is too short
    or malformed.

    Indexed from the **last** ``)``, never by splitting on whitespace. Field 2 is the ``comm``, which
    a process sets for itself via ``prctl(PR_SET_NAME)`` and which the kernel does **not** escape —
    it can contain spaces and parentheses. Any parse that assumes otherwise shifts every subsequent
    field, so ``starttime`` would be read out of the middle of a process's own chosen name and every
    identity comparison on Linux would silently compare garbage.
    """
    end = content.rfind(")")
    if end < 0:
        return None
    fields = content[end + 1:].split()
    if len(fields) <= _PROC_STARTTIME_INDEX:
        return None
    return fields[0], fields[_PROC_STARTTIME_INDEX]


def _linux_fields(pid: int) -> tuple[str, str] | None:
    """``(state, start_time)`` for ``pid``, read straight from ``/proc`` — **no fork**, which is what
    lets the callers afford to ask inside a wait loop. Readable for a zombie, which is the whole
    point. A vanished pid raises ``FileNotFoundError`` and reads as unknown."""
    try:
        return _parse_proc_stat(Path(f"/proc/{pid}/stat").read_text(errors="replace"))
    except OSError:
        return None


def _macos_fields(pid: int) -> tuple[str, str] | None:
    """``(state, start_time)`` for ``pid`` from one ``ps`` call, or ``None`` when it can't be read.

    Both facts come from a **single** fork because every caller that wants one usually wants the
    other. ``TZ=UTC LC_ALL=C`` pins the ``lstart`` rendering: the token is only ever compared as an
    opaque string, so an operator's timezone or locale changing between the stamp and the check must
    not make a process look like a different one.
    """
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "state=,lstart="],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_PS_TIMEOUT_SECONDS,
            env={**os.environ, "TZ": "UTC", "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None  # no `ps`, or it wedged — unreadable, which every caller treats as "unknown"
    if proc.returncode != 0:
        return None  # `ps` exits non-zero for a pid that does not exist
    return _parse_ps_fields(proc.stdout)


def _parse_ps_fields(output: str) -> tuple[str, str] | None:
    """``(state, start_time)`` out of ``ps -o state=,lstart=`` output — e.g.
    ``"Z    Tue Jul 28 05:23:07 2026"`` → ``("Z", "Tue Jul 28 05:23:07 2026")``.

    Pure, so the macOS branch is covered from any platform. The state column can carry BSD
    decorations (``S+``, ``Ss``, ``R<``); only the leading character is a state, and the rest of the
    line is the start time verbatim — never parsed into a date, only compared.
    """
    line = output.strip()
    if not line:
        return None
    state, _, start = line.partition(" ")
    start = start.strip()
    if not state or not start:
        return None
    return state[:1], start


# Windows has no POSIX-style zombie: a process that has exited leaves a *handle* behind, not a
# process-table entry, and `GetExitCodeProcess` reports it exited. So the state half of a Windows
# probe is only ever "not a zombie", and this placeholder keeps `ProcessFacts`'s shape uniform.
_WIN_LIVE_STATE = "R"


# The access right both Windows probes ask for. Grantable across users, which is *not* the same as
# granted — see the failure discipline in `run_records._win_pid_alive`.
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def win_kernel32():
    """``kernel32`` with the signatures this codebase calls through it **declared**.

    ctypes defaults an undeclared function to ``restype=c_int`` — 4 signed bytes — while
    ``OpenProcess`` returns a ``HANDLE``, which is pointer-sized on x64. Real Windows handle values do
    fit in the low 32 bits (the documented 32/64-bit interop guarantee) and failure is ``NULL`` rather
    than ``INVALID_HANDLE_VALUE``, so the truncated form happens to work — but "happens to work" is
    not something a reader should have to re-derive at a seam that decides which processes we may
    signal. Declaring it also stops the truncated value from being what gets passed on to
    ``GetProcessTimes``.

    One helper so the two probes cannot drift: ``run_records._win_pid_alive`` (liveness) and
    ``_win_creation_filetime`` (identity) ask the same library for overlapping calls, and the real
    ones execute only on the ``test-windows`` CI job. ``use_last_error=True`` is required by
    ``_win_pid_alive``, which reads ``ctypes.get_last_error()`` to tell "no such pid" from "not
    allowed to look".
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    for name, restype, argtypes in (
        ("OpenProcess", wintypes.HANDLE, (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)),
        ("CloseHandle", wintypes.BOOL, (wintypes.HANDLE,)),
        ("GetExitCodeProcess", wintypes.BOOL, (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))),
        ("GetProcessTimes", wintypes.BOOL, (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4),
    ):
        function = getattr(kernel32, name, None)
        if function is None:  # a faked kernel32 in a test stands in for only what that test needs
            continue
        with contextlib.suppress(TypeError, AttributeError):
            function.restype = restype
            function.argtypes = list(argtypes)
    return kernel32


def _win_creation_filetime(pid: int) -> int | None:
    """``pid``'s creation time as a 64-bit FILETIME, or ``None`` when it can't be read.

    Same access right and the same failure discipline as ``run_records._win_pid_alive``: a NULL
    ``OpenProcess`` means we could not *look* — another user's or a protected process included — and
    a start time we could not read must be reported as unverifiable, never as a value. Split out so
    the decision logic above it stays unit-testable off Windows; the call itself is exercised by the
    ``test-windows`` CI job, which is the only place it runs against a real kernel.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = win_kernel32()
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        creation, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exited),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return None
        return (creation.dwHighDateTime << 32) | creation.dwLowDateTime
    finally:
        kernel32.CloseHandle(handle)


def _win_fields(pid: int) -> tuple[str, str] | None:
    filetime = _win_creation_filetime(pid)
    return None if filetime is None else (_WIN_LIVE_STATE, str(filetime))


class ProcessFacts(NamedTuple):
    """What the kernel says about one pid, in a single read."""

    state: str        # the platform's state character; "Z" is a zombie (never on Windows)
    start_time: str   # the opaque identity token — compared for equality, never parsed

    @property
    def is_zombie(self) -> bool:
        return self.state == "Z"


def is_zombie(pid: int) -> bool:
    """Whether ``pid`` is a corpse — exited, but still occupying a process-table slot because nobody
    reaped it. ``False`` whenever that can't be established (Windows has no such state at all; an
    unreadable pid is not evidence of one), so this can only ever *correct* a false "alive", never
    invent a false "dead"."""
    facts = process_facts(pid)
    return bool(facts and facts.is_zombie)


def process_start_time(pid: int) -> str | None:
    """``pid``'s start time as an **opaque token**, or ``None`` when it can't be read.

    Never parsed into a date and never compared for ordering — only for equality against a token
    stamped earlier beside the same pid. That is what makes ``(pid, start_time)`` an identity: a
    recycled pid keeps the number and loses the token.

    ``None`` means *unverifiable*, and every caller must treat it as neither a match nor a mismatch.
    It is deliberately not a sentinel string: a placeholder would compare equal across unrelated
    processes, turning "we cannot tell" into a confident wrong answer at the one seam where being
    wrong means signalling somebody else's process.
    """
    facts = process_facts(pid)
    return facts.start_time if facts else None


_probe_failure_reported = False


def _report_probe_failure(exc: BaseException) -> None:
    """Say **once per process** that the identity probe itself is broken.

    The degradation is safe (see ``process_facts``) but it is otherwise completely invisible, and
    what it silently switches off is every identity check in the CLI: a typo or a platform quirk in
    one of the per-platform readers would send every join, leave and hot-reload back to trusting a
    bare pid, with no trace anywhere that it had stopped working. Once, not per call — this runs
    inside wait loops, and a message repeated five times a second would be its own failure.
    """
    global _probe_failure_reported
    if _probe_failure_reported:
        return
    _probe_failure_reported = True
    print(
        f"Note: could not read process state ({exc!r}); grid will fall back to pid-only checks "
        "for the rest of this command.",
        file=sys.stderr,
    )


def process_facts(pid: int) -> ProcessFacts | None:
    """Everything the kernel will tell us about ``pid`` in one read, or ``None`` when it can't be read
    — no such process, or one we are not allowed to inspect.

    The ONE platform-neutral seam, and deliberately a *single* call: every caller that wants the state
    wants the token too, and on macOS each read is a ``ps`` fork. Tests patch this rather than a
    per-platform implementation, so a record-seam test stays hermetic on any OS.
    """
    if not is_addressable(pid):
        return None
    try:
        if _IS_WINDOWS:
            fields = _win_fields(pid)
        else:
            fields = _linux_fields(pid) if _IS_LINUX else _macos_fields(pid)
    except Exception as exc:
        _report_probe_failure(exc)
        # Deliberately everything, at the one seam, and the fail direction is what makes it safe:
        # this probe is **advisory**. Its answer only ever upgrades a decision the CLI was already
        # making — it cannot authorise a signal that a bare pid would not have — so "unreadable"
        # degrades to precisely the behaviour this code had before the probe existed.
        #
        # What raising costs instead is concrete: `identity_stamp` runs in `grid join` immediately
        # AFTER the serve child is spawned, so an exception here leaves a **live child with no
        # record** — the untracked orphan this whole feature exists to prevent, manufactured by the
        # stamp meant to prevent it. On the teardown side it would land ahead of the relay
        # deregister that is the mechanism of record. The same reasoning already guards the argv
        # sweep (`cli.remote_provider._full_leave_reap`) and the child's self-stamp
        # (`remote.serve._stamp_own_pid`); this is the third instance of one rule, not a new licence.
        return None
    return ProcessFacts(*fields) if fields else None


def process_group_of(pid: int) -> int | None:
    """``pid``'s POSIX process group, or ``None`` when there isn't one to read (Windows, a pid that
    has already gone).

    Asked **at spawn time and stamped**, never re-derived when a teardown needs it: on macOS
    ``os.getpgid`` raises ``ProcessLookupError`` for a **zombie** (measured), which is precisely the
    regime the group backstop exists to handle. For a child spawned with ``start_new_session=True``
    this is the child's own pid — it is the session and group leader — so a launcher shim and the
    real serve process it execs share one group id that outlives the shim.
    """
    if _IS_WINDOWS or not hasattr(os, "getpgid") or not is_addressable(pid):
        return None
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def group_alive(pgid: int) -> bool:
    """Whether the POSIX process group ``pgid`` still has at least one member.

    The ``ps``-free liveness probe behind the leave teardown's group backstop. Signal ``0`` performs
    the permission and existence checks and delivers nothing, so this reads the group without
    touching it. ``EPERM`` means the group exists and is somebody else's, which is *alive* — the same
    direction ``pid_alive`` chose for a pid.

    Load-bearing property (see ``tests/test_process_identity``): a group id is not recycled while the
    group has members, so this keeps naming **our own** descendants even after the group leader —
    a launcher shim — has exited and been reaped. That is what lets a `grid leave` racing a just-spawned
    child reap it without a readable process table.

    ``False`` on Windows, which has no equivalent: ``taskkill /T`` already tears down the whole tree
    there, so there is nothing for a group backstop to add. A caller must therefore never treat
    ``False`` as proof on Windows — it is "no such mechanism", not "nothing is running".
    """
    if _IS_WINDOWS or not hasattr(os, "killpg") or not is_addressable(pgid):
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False  # ESRCH: the group has no members left
    except PermissionError:
        return True  # EPERM: it exists, we simply may not signal it
    except OSError:
        return False
