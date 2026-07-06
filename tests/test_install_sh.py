"""Regression tests for install.sh's post-install PATH hint (macOS wheel path).

install_macos_wheel exports ~/.local/bin into the script's own PATH, so the
post-install "Add <dir> to PATH" check must test the invoking shell's PATH
(ORIG_PATH), not the augmented one — otherwise the hint is dead code on macOS
and users get a success banner with `grid` unreachable in their shell.

The installer runs against a throwaway HOME with `uv`, `uname`, and `curl`
stubbed out, so the tests are deterministic and never touch the network.
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
