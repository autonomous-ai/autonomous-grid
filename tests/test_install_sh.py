"""Regression tests for install.sh.

install.sh is served from raw `main` by the grid.autonomous.ai worker, so it goes
live the moment `main` is pushed: no release carries it, no tag rolls it back, and
nothing stands between the push and the next user's `curl … | bash`. Its invariants
are therefore pinned here, where CI checks them on every push and PR.

Two classes of test live in this file:

1. Behaviour, driven through the real script. install_macos_wheel exports
   ~/.local/bin into the script's own PATH, so the post-install "Add <dir> to PATH"
   check must test the invoking shell's PATH (ORIG_PATH), not the augmented one —
   otherwise the hint is dead code on macOS and users get a success banner with
   `grid` unreachable in their shell. The installer runs against a throwaway HOME
   with `uv`, `uname`, and `curl` stubbed out, so it never touches the network.

2. Static invariants (valid bash; never calls api.github.com). These were previously
   enforced only by a grep inside the release-grid-cli skill — a guard that holds
   only while a human remembers to run it, on the one path nobody exercises until
   it is already release day.
"""

import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "install.sh"

# Mimics `uv tool install`: drops a fake grid executable into ~/.local/bin.
STUB_UV = """#!/bin/bash
if [ "$1" = "tool" ] && [ "$2" = "install" ]; then
  mkdir -p "$HOME/.local/bin"
  printf '#!/bin/bash\\necho "grid 0.0.0-test"\\n' > "$HOME/.local/bin/grid"
  chmod +x "$HOME/.local/bin/grid"
fi
exit 0
"""

# Forces the macOS/arm64 code path regardless of the host OS.
STUB_UNAME = """#!/bin/bash
case "$1" in
  -m) echo arm64 ;;
  *) echo Darwin ;;
esac
"""

# Only `command -v curl` is exercised in this flow (uv present, wheel URL pinned).
STUB_CURL = "#!/bin/bash\nexit 0\n"


def _write_exe(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run_installer(tmp_path: Path, local_bin_on_path: bool) -> subprocess.CompletedProcess:
    tmp = tmp_path.resolve()
    home = tmp / "home"
    stubbin = tmp / "stubbin"
    home.mkdir(exist_ok=True)
    stubbin.mkdir(exist_ok=True)
    _write_exe(stubbin / "uv", STUB_UV)
    _write_exe(stubbin / "uname", STUB_UNAME)
    _write_exe(stubbin / "curl", STUB_CURL)

    path = f"{stubbin}:/usr/bin:/bin"
    if local_bin_on_path:
        local_bin = home / ".local" / "bin"
        local_bin.mkdir(parents=True, exist_ok=True)
        path = f"{local_bin}:{path}"

    env = {
        "HOME": str(home),
        "PATH": path,
        # Pinning the wheel URL skips release-tag resolution (no network).
        "GRID_WHEEL_URL": "https://invalid.example/grid-0.0.0-py3-none-any.whl",
    }
    return subprocess.run(
        ["bash", str(INSTALL_SH)], env=env, capture_output=True, text=True, timeout=60
    )


def test_hint_fires_when_local_bin_missing_from_users_path(tmp_path):
    """Clean-Mac scenario: ~/.local/bin not on the user's PATH → hint must print."""
    res = _run_installer(tmp_path, local_bin_on_path=False)
    assert res.returncode == 0, f"installer failed:\n{res.stdout}\n{res.stderr}"
    assert ".local/bin to PATH" in res.stdout, (
        "installer must tell the user to add ~/.local/bin to PATH; "
        f"output was:\n{res.stdout}"
    )


def test_no_hint_when_local_bin_already_on_users_path(tmp_path):
    res = _run_installer(tmp_path, local_bin_on_path=True)
    assert res.returncode == 0, f"installer failed:\n{res.stdout}\n{res.stderr}"
    assert "to PATH" not in res.stdout, (
        f"no hint expected when ~/.local/bin is already on PATH; output was:\n{res.stdout}"
    )


def _code_lines(text: str) -> list[tuple[int, str]]:
    """(lineno, code) for each line, with comments stripped.

    Mirrors the `^[^#]*` guard this check replaces. Truncating at a `#` inside a
    quoted string could only ever *hide* a violation sitting after it, never invent
    one — and on these lines anything past a `#` is prose.
    """
    return [(n, line.split("#", 1)[0]) for n, line in enumerate(text.splitlines(), 1)]


def test_installer_is_valid_bash():
    """A syntax error here reaches users directly, with no release in between."""
    res = subprocess.run(["bash", "-n", str(INSTALL_SH)], capture_output=True, text=True)
    assert res.returncode == 0, f"install.sh is not valid bash:\n{res.stderr}"


def test_installer_never_calls_the_github_api():
    """api.github.com's 60 req/hr/IP cap 403s every install behind a shared NAT.

    install.sh must resolve releases through github.com itself — the /releases/latest
    redirect, falling back to the releases.atom feed — neither of which is rate
    limited. See the rationale block above latest_release_tag().
    """
    offenders = [
        (n, code.strip())
        for n, code in _code_lines(INSTALL_SH.read_text())
        if "api.github.com" in code
    ]
    assert not offenders, (
        "install.sh must never call api.github.com: shared-NAT IPs exhaust its "
        f"60 req/hr limit and every install behind them 403s. Offending lines: {offenders}"
    )
