"""Windows blocking a program says so, instead of a traceback (`shared.app_control`).

Smart App Control and App Control for Business refuse to start an unsigned program they do not trust:
`CreateProcess` fails with ERROR 4551 ("An Application Control policy has blocked this file"), or 1260
for the older Software Restriction Policies. A raw OSError there reads as a grid bug; the person needs
to know it is Windows' policy, which file, and what to do (Hermes prints the same kind of guidance).
"""
from __future__ import annotations

import pytest

from shared import app_control, detach


def _winerror(code: int) -> OSError:
    error = OSError(13, "An Application Control policy has blocked this file")
    error.winerror = code
    return error


@pytest.mark.parametrize("code", [4551, 1260])
def test_an_application_control_block_is_recognised(code):
    assert app_control.blocked(_winerror(code)) is True


def test_other_spawn_errors_are_not_mistaken_for_a_block():
    assert app_control.blocked(_winerror(2)) is False
    assert app_control.blocked(FileNotFoundError(2, "nope")) is False


def test_the_message_names_the_file_the_policy_and_the_way_out():
    text = app_control.explain(r"C:\grid\engines\llama.cpp\llama-server.exe")
    assert r"C:\grid\engines\llama.cpp\llama-server.exe" in text
    assert "Smart App Control" in text
    assert "aka.ms/smartappcontrol" in text


def test_detach_turns_a_blocked_spawn_into_that_message(monkeypatch):
    monkeypatch.setattr(detach, "_IS_WINDOWS", True)

    def blocked(argv, **kwargs):
        raise _winerror(4551)

    monkeypatch.setattr(detach.subprocess, "Popen", blocked)
    with pytest.raises(SystemExit, match="Smart App Control"):
        detach.popen([r"C:\x\grid.exe", "__server", "g"])


def test_starting_llama_server_turns_a_block_into_that_message(monkeypatch):
    from shared.engine import launcher

    def blocked(cmd, **kwargs):
        raise _winerror(4551)

    monkeypatch.setattr(launcher.subprocess, "Popen", blocked)
    with pytest.raises(SystemExit, match="llama-server.exe"):
        launcher._spawn_llama_server([r"C:\grid\llama-server.exe", "-m", "x.gguf"], log_fh=None)
