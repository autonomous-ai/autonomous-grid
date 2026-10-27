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


def test_installer_ends_with_the_same_next_steps_as_install_sh():
    sh = INSTALL_SH.read_text(encoding="utf-8")
    for line in ("grid login       # sign in, then pick from the grids you can use",
                 "grid mode local  # then 'grid start' creates your own grid on this computer"):
        assert line in sh
        assert line in _script()
