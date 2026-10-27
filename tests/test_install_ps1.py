"""Static invariants of install.ps1, the Windows installer.

Like install.sh it goes live the moment `main` does, so what must stay true of it is pinned here, on
every push. Its behaviour is exercised for real by the `install-windows` CI job, which runs it on
`windows-latest` against the wheel that job builds.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_PS1 = REPO_ROOT / "install.ps1"
INSTALL_SH = REPO_ROOT / "install.sh"


def _script() -> str:
    return INSTALL_PS1.read_text(encoding="utf-8")


def test_installer_is_ascii():
    """Windows PowerShell 5.1 reads a BOM-less script as the ANSI code page, so one "—" or "✓" in a
    string turns into mojibake — or an unbalanced quote — when the file is run locally."""
    INSTALL_PS1.read_bytes().decode("ascii")


def _code() -> str:
    """The script without its comments (`#` to end of line), as install.sh's test reads that script."""
    return "\n".join(line.split("#", 1)[0] for line in _script().splitlines())


def test_installer_never_calls_the_github_api():
    """Same reason as install.sh: api.github.com allows 60 unauthenticated requests per hour per IP."""
    assert "api.github.com" not in _code()


def test_latest_release_resolves_through_the_redirect_then_the_atom_feed():
    script = _script()
    assert "/releases/latest" in script
    assert "releases.atom" in script
    # Only a CLI tag (`v<digit>…`) counts — the repository also publishes `protocol-v…` releases.
    assert re.search(r"\^v\[0-9\]", script)


def test_installer_uses_a_signed_python_not_one_uv_downloads():
    """Smart App Control blocks the unsigned CPython builds uv downloads: `grid` must run on python.org's."""
    script = _script()
    assert "--python" in script
    assert "Python.Python.3.12" in script  # winget's id for python.org's signed build
    assert "WindowsApps" in script  # the Microsoft Store alias stub is not a Python


def test_installer_cannot_close_the_callers_window():
    """`irm … | iex` runs in the caller's own session: an `exit` there closes their PowerShell."""
    assert not re.search(r"(?im)^\s*exit\b", _script())


def test_installer_takes_the_same_knobs_as_install_sh():
    script = _script()
    for knob in ("GRID_VERSION", "GRID_WHEEL_URL", "GRID_PACKAGE", "GRID_REPO_OWNER", "GRID_REPO_NAME"):
        assert f"$env:{knob}" in script, knob
        assert knob in INSTALL_SH.read_text(encoding="utf-8"), knob


def test_installer_refuses_to_reinstall_under_a_running_grid():
    """A running grid holds its install's files open on Windows: `uv tool install --force` then fails
    half-way and leaves a grid that cannot even `grid stop` (measured on Windows 11). Look first."""
    code = _code()
    check, install = code.find("Win32_Process"), code.find("tool install --force")
    assert 0 <= check < install, "the running-grid check must come before the reinstall"
    assert "grid stop" in _script()


def test_installer_falls_back_to_a_cmd_launcher_when_grid_exe_is_blocked():
    """uv's grid.exe is unsigned and rewritten per install; Smart App Control blocks some installs' copy
    (measured on Windows 11, v0.3.60). The same install's python.exe is PSF-signed, so a .cmd launcher
    that runs it takes over — only when the .exe was actually blocked."""
    code = _code()
    assert "Application Control" in code, "the fallback keys on the block itself, not on every install"
    assert "-m cli %*" in _script()  # on the npm line, whose `#_undefined_#` `_code` would cut
    assert ".exe.blocked" in code, "the blocked .exe is moved aside, or `grid` still resolves to it"
    for name in ("grid", "agrid"):
        assert f"'{name}'" in code


def test_the_cmd_launcher_suppresses_the_terminate_batch_job_prompt():
    """npm cmd-shim's form: ending the batch context before the program runs means Ctrl+C never asks
    "Terminate batch job (Y/N)?" (measured on Windows 11 with Ctrl+Break, against a plain .cmd)."""
    assert "endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & " in _script()


def test_installer_ends_with_the_same_next_steps_as_install_sh():
    sh = INSTALL_SH.read_text(encoding="utf-8")
    for line in ("grid login       # sign in, then pick from the grids you can use",
                 "grid mode local  # then 'grid start' creates your own grid on this computer"):
        assert line in sh
        assert line in _script()
