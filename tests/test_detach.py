"""`shared.detach.popen`: the POSIX spawn is unchanged, and Windows gets the flags that actually detach.

The Windows branch runs here on any OS by flipping the module's platform flag — the flags are spelled as
literals in `shared.detach` for exactly that reason.
"""
from __future__ import annotations

import pytest

from shared import detach


def _record(monkeypatch, *, fail_first: OSError | None = None) -> list[dict]:
    calls: list[dict] = []

    def fake_popen(argv, **kwargs):
        calls.append({"argv": argv, **kwargs})
        if fail_first is not None and len(calls) == 1:
            raise fail_first
        return object()

    monkeypatch.setattr(detach.subprocess, "Popen", fake_popen)
    return calls


def test_posix_passes_start_new_session_and_nothing_windows(monkeypatch):
    monkeypatch.setattr(detach, "_IS_WINDOWS", False)
    calls = _record(monkeypatch)
    detach.popen(["grid", "__server", "g"], stdout=None)
    assert calls == [{"argv": ["grid", "__server", "g"], "start_new_session": True, "stdout": None}]


def test_windows_detaches_from_the_console_the_group_and_the_job(monkeypatch):
    monkeypatch.setattr(detach, "_IS_WINDOWS", True)
    calls = _record(monkeypatch)
    detach.popen(["grid.exe", "__engine", "g", "e"], env={"A": "1"})
    assert len(calls) == 1
    flags = calls[0]["creationflags"]
    assert flags & 0x08000000  # CREATE_NO_WINDOW: closing the terminal does not reach it
    assert flags & 0x00000200  # CREATE_NEW_PROCESS_GROUP: the terminal's Ctrl-C does not reach it
    assert flags & 0x01000000  # CREATE_BREAKAWAY_FROM_JOB: an sshd session's job does not kill it
    assert "start_new_session" not in calls[0]
    assert calls[0]["env"] == {"A": "1"}


def test_windows_job_refusing_breakaway_still_starts_the_child_detached(monkeypatch):
    monkeypatch.setattr(detach, "_IS_WINDOWS", True)
    denied = OSError(13, "Access is denied")
    denied.winerror = 5
    calls = _record(monkeypatch, fail_first=denied)
    detach.popen(["grid.exe", "__server", "g"])
    assert len(calls) == 2
    retry = calls[1]["creationflags"]
    assert not retry & detach.CREATE_BREAKAWAY_FROM_JOB
    assert retry & detach.CREATE_NO_WINDOW and retry & detach.CREATE_NEW_PROCESS_GROUP


def test_windows_other_spawn_errors_are_not_swallowed(monkeypatch):
    monkeypatch.setattr(detach, "_IS_WINDOWS", True)
    missing = FileNotFoundError(2, "The system cannot find the file specified")
    missing.winerror = 2
    calls = _record(monkeypatch, fail_first=missing)
    with pytest.raises(FileNotFoundError):
        detach.popen(["nope.exe"])
    assert len(calls) == 1
