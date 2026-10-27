"""On Windows grid re-runs itself through the interpreter, never through uv's `grid.exe`.

`grid.exe` is uv's launcher: unsigned and rewritten per install, so Smart App Control blocks some
installs' copy outright ("An Application Control policy has blocked this file" — measured on Windows 11,
v0.3.60). Every detached child (`__server`, `__engine`, `__remote-engine`, seats, media, the update check)
was spawned through it, so a blocked launcher took them all down. The install's own `python.exe` is
signed by the PSF and runs: Hermes re-runs itself the same way (`sys.executable -m …`).
"""
from __future__ import annotations

import sys

from local import runtime


def test_windows_children_run_through_the_interpreter_even_when_a_launcher_exists(monkeypatch, tmp_path):
    launcher = tmp_path / "grid.exe"
    launcher.write_bytes(b"MZ")
    monkeypatch.setattr(runtime, "_IS_WINDOWS", True)
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "grid")])
    assert runtime.cli_command() == [sys.executable, "-m", "cli"]


def test_posix_children_keep_using_the_installed_launcher(monkeypatch, tmp_path):
    launcher = tmp_path / "grid"
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    monkeypatch.setattr(runtime, "_IS_WINDOWS", False)
    monkeypatch.setattr(sys, "argv", [str(launcher)])
    assert runtime.cli_command() == [str(launcher.resolve())]
