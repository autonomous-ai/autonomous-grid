"""Regression tests for install.sh.

install.sh is served from raw `main` by the grid.autonomous.ai worker, so it goes
live the moment `main` is pushed: no release carries it, no tag rolls it back, and
nothing stands between the push and the next user's `curl … | bash`. Its invariants
are therefore pinned here, where CI checks them on every push and PR.

Two classes of test live in this file:

1. Behaviour, driven through the real script. install_wheel exports
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


def test_installer_local_path_names_the_mode(tmp_path):
    """The installer's two on-ramps must both work on a machine that has just run it.

    A new install is in remote mode (ADR 0001 D-2, amended), where `grid start` is the hosted
    lifecycle verb and answers `You're not signed in.` — so an installer that offers a bare
    `grid start` as the "your own grid on this computer" option hands the reader a command that
    refuses. Driven through the real script rather than grepped, because the banner is what a user
    actually sees.
    """
    res = _run_installer(tmp_path, local_bin_on_path=True)
    assert res.returncode == 0, f"installer failed:\n{res.stdout}\n{res.stderr}"
    assert "grid login" in res.stdout, f"the hosted on-ramp must be named:\n{res.stdout}"
    assert "grid mode local" in res.stdout, (
        "the local on-ramp must name the mode, or `grid start` refuses on a fresh install; "
        f"output was:\n{res.stdout}"
    )


# --- Linux: a binary this machine cannot run falls back to the wheel ------------------------------
#
# Every release up to v0.3.56 built its Linux binaries on Ubuntu 24.04, so they need glibc 2.38 and die on Debian 12,
# Ubuntu 22.04 and RHEL 9 (`version 'GLIBC_2.38' not found`). The installer used to install that binary anyway and
# end on "installed but failed to run". It now tries the binary first and, when it cannot run here, installs the
# wheel with uv — the same grid, as macOS always gets. And because install.sh is served from `main`, that covers
# every release already published, not only the ones built after the build moved to an older glibc.

STUB_UNAME_LINUX = """#!/bin/bash
case "$1" in
  -m) echo x86_64 ;;
  *) echo Linux ;;
esac
"""

# Serves the release's binary (the script in $STUB_BINARY) and no SHA256SUMS; anything else is a 404.
STUB_CURL_LINUX = """#!/bin/bash
out=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
case "$url" in
  */grid-linux-x86_64) cp "$STUB_BINARY" "$out"; exit 0 ;;
esac
exit 22
"""

# Records that it ran, then installs a grid the way `uv tool install` does.
STUB_UV_RECORDING = """#!/bin/bash
echo "$*" >> "$HOME/uv-calls"
if [ "$1" = "tool" ] && [ "$2" = "install" ]; then
  mkdir -p "$HOME/.local/bin"
  printf '#!/bin/bash\\necho "grid 0.0.0-wheel"\\n' > "$HOME/.local/bin/grid"
  chmod +x "$HOME/.local/bin/grid"
fi
exit 0
"""

BINARY_THIS_MACHINE_CANNOT_RUN = """#!/bin/bash
echo "grid: /lib/x86_64-linux-gnu/libc.so.6: version \\`GLIBC_2.38' not found (required by grid)" >&2
exit 1
"""

BINARY_THAT_RUNS = """#!/bin/bash
echo "grid 9.9.9-binary"
"""


def _run_linux_installer(tmp_path: Path, binary: str) -> tuple[subprocess.CompletedProcess, Path]:
    tmp = tmp_path.resolve()
    home, stubbin = tmp / "home", tmp / "stubbin"
    home.mkdir(exist_ok=True)
    stubbin.mkdir(exist_ok=True)
    _write_exe(stubbin / "uv", STUB_UV_RECORDING)
    _write_exe(stubbin / "uname", STUB_UNAME_LINUX)
    _write_exe(stubbin / "curl", STUB_CURL_LINUX)
    _write_exe(tmp / "release-binary", binary)
    env = {
        "HOME": str(home),
        "PATH": f"{home / '.local' / 'bin'}:{stubbin}:/usr/bin:/bin",
        "STUB_BINARY": str(tmp / "release-binary"),
        # The wheel the fallback installs; pinned so no release tag is resolved (no network).
        "GRID_WHEEL_URL": "https://invalid.example/grid-0.0.0-py3-none-any.whl",
    }
    res = subprocess.run(["bash", str(INSTALL_SH)], env=env, capture_output=True, text=True, timeout=60)
    return res, home


def test_a_linux_binary_this_machine_cannot_run_falls_back_to_the_wheel(tmp_path):
    res, home = _run_linux_installer(tmp_path, BINARY_THIS_MACHINE_CANNOT_RUN)

    assert res.returncode == 0, f"installer failed:\n{res.stdout}\n{res.stderr}"
    assert "GLIBC_2.38" in res.stdout, f"say WHY the binary was not used:\n{res.stdout}"
    assert (home / "uv-calls").exists(), "the wheel must be installed with uv"
    assert "grid 0.0.0-wheel" in res.stdout, f"the grid that answers must be the wheel's:\n{res.stdout}"
    assert not (home / ".local" / "bin" / ".grid.new").exists(), "the binary that could not run is left behind"


def test_a_linux_binary_that_runs_is_installed_and_uv_is_never_called(tmp_path):
    res, home = _run_linux_installer(tmp_path, BINARY_THAT_RUNS)

    assert res.returncode == 0, f"installer failed:\n{res.stdout}\n{res.stderr}"
    assert "grid 9.9.9-binary" in res.stdout
    assert not (home / "uv-calls").exists(), "uv must not be touched when the binary runs"
    assert (home / ".local" / "bin" / "agrid").is_symlink()


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


# --- latest_release_tag(): only a CLI release is "the latest grid" -------------------------------
#
# Since grid-platform ticket 12 this repository also publishes `protocol-vX.Y.Z` releases (the
# grid-protocol wheel). They are never marked Latest, so the /releases/latest redirect skips them —
# but the Atom fallback took the NEWEST release of any kind, so the first protocol release cut after
# a CLI release would have been "installed" as grid (a wheel URL that 404s, or worse, the wrong
# package). Both sources must yield a `v<digit>…` tag or nothing.

STUB_CURL_RELEASES = """#!/bin/bash
for arg in "$@"; do
  case "$arg" in
    */releases/latest) printf '%s' "${STUB_REDIRECT:-}"; exit 0 ;;
    */releases.atom) cat "$STUB_ATOM"; exit 0 ;;
  esac
done
exit 22
"""


def _atom(*tags: str) -> str:
    entries = "".join(
        f'<entry><link rel="alternate" type="text/html" '
        f'href="https://github.com/autonomous-ai/autonomous-grid/releases/tag/{t}"/></entry>'
        for t in tags
    )
    return f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'


def _latest_release_tag(tmp_path: Path, redirect: str, atom: str) -> str:
    """Run install.sh's own latest_release_tag() — cut out of the real file — against a stub curl."""
    text = INSTALL_SH.read_text()
    start = text.index("latest_release_tag() {")
    function = text[start:text.index("\n}\n", start) + 3]
    stubbin = tmp_path / "stubbin"
    stubbin.mkdir(exist_ok=True)
    _write_exe(stubbin / "curl", STUB_CURL_RELEASES)
    feed = tmp_path / "releases.atom"
    feed.write_text(atom)
    res = subprocess.run(
        # install.sh's own shell options: a no-match grep inside a pipefail pipeline is the case that
        # most needs them (drop the function's `|| true` and every install with no CLI entry dies).
        ["bash", "-c", f'set -euo pipefail\nOWNER=autonomous-ai; REPO=autonomous-grid\n{function}\nlatest_release_tag'],
        env={"PATH": f"{stubbin}:/usr/bin:/bin", "STUB_REDIRECT": redirect, "STUB_ATOM": str(feed)},
        capture_output=True, text=True, timeout=30,
    )
    assert res.returncode == 0, res.stderr
    return res.stdout


RELEASES = "https://github.com/autonomous-ai/autonomous-grid/releases/tag/"


def test_latest_release_tag_follows_the_latest_redirect(tmp_path):
    """The feed disagrees on purpose, so only the redirect can produce this answer."""
    assert _latest_release_tag(tmp_path, RELEASES + "v0.3.56", _atom("v0.3.99")) == "v0.3.56"


def test_a_cli_pre_release_tag_is_still_a_cli_release(tmp_path):
    assert _latest_release_tag(tmp_path, RELEASES + "v0.4.0-rc1", _atom("v0.3.99")) == "v0.4.0-rc1"
    assert _latest_release_tag(tmp_path, "", _atom("protocol-v0.2.0", "v0.4.0-rc1")) == "v0.4.0-rc1"


def test_atom_fallback_skips_a_newer_protocol_release(tmp_path):
    """The redirect came back empty; the newest release in the feed is a protocol one."""
    feed = _atom("protocol-v0.2.0", "v0.3.56", "v0.3.55", "protocol-v0.1.0")
    assert _latest_release_tag(tmp_path, "", feed) == "v0.3.56"


def test_a_protocol_release_marked_latest_is_not_installed_as_grid(tmp_path):
    """Somebody ticks "Latest" on a protocol release: the redirect must not be believed."""
    feed = _atom("protocol-v0.2.0", "v0.3.56")
    assert _latest_release_tag(tmp_path, RELEASES + "protocol-v0.2.0", feed) == "v0.3.56"


def test_no_cli_release_at_all_resolves_to_nothing(tmp_path):
    """Empty, so the caller's own `die` names GRID_VERSION — never a protocol tag."""
    assert _latest_release_tag(tmp_path, "", _atom("protocol-v0.1.0")) == ""
