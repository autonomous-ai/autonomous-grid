"""`grid comfyui stop` on Windows (`shared.engine.comfyui.stop_running`).

``os.kill(pid, 15)`` on Windows is ``TerminateProcess(pid, 15)``: the launcher dies and the Python
interpreter under it keeps the port. The whole tree has to go, the way `run_records.kill_group` takes it.
"""
from __future__ import annotations

import pytest

from shared import run_records
from shared.engine import comfyui


@pytest.fixture
def pid_file(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    path = comfyui.comfyui_pid_file(8188)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("4242\n8188\n")
    return path


def test_comfyui_stop_on_windows_kills_the_tree(pid_file, monkeypatch):
    monkeypatch.setattr(comfyui, "_IS_WINDOWS", True)
    killed: list[int] = []
    monkeypatch.setattr(run_records, "kill_group", killed.append)

    def no_kill(pid, sig):
        raise AssertionError("TerminateProcess on the launcher alone leaves the interpreter running")

    monkeypatch.setattr(comfyui.os, "kill", no_kill)
    assert comfyui.stop_running(8188) == 0
    assert killed == [4242]
    assert not pid_file.exists()


def test_comfyui_stop_on_posix_still_sends_sigterm(pid_file, monkeypatch):
    monkeypatch.setattr(comfyui, "_IS_WINDOWS", False)
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(comfyui.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    assert comfyui.stop_running(8188) == 0
    assert sent == [(4242, 15)]
