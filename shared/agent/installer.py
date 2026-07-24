"""Install the Hermes agent into ~/.grid, without a package manager.

Hermes ships as a Python package, so the obvious route is `brew install hermes-agent`. We do not
take it: installing Homebrew needs an interactive `sudo`, which a GUI app cannot drive, and that
dead-ended the app's hands-off setup. Instead we fetch a pinned `uv` — a single static binary — and
let it install Hermes together with its own private CPython. Nothing leaves ~/.grid, nothing needs
admin rights, and uninstalling is a directory removal.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from shared import paths
from shared.engine.installer import fetch_and_extract
from shared.system import arch

# Hermes requires >=3.11,<3.14. Pin the interpreter so an install never silently drifts onto a
# version the package cannot run on.
HERMES_PACKAGE = "hermes-agent"
HERMES_PYTHON = "3.13"

# What we actually hand uv. The `acp` extra pulls `agent-client-protocol`, which Hermes's Agent
# Client Protocol adapter imports — and ACP is the only channel the Grid app talks to the agent
# through. Installed without it, `hermes` lands on the machine looking healthy while `hermes acp`
# dies on startup ("ACP dependencies not installed"), so every chat turn fails on what reads to
# the user as a successful install.
HERMES_REQUIREMENT = f"{HERMES_PACKAGE}[acp]"

# `hermes acp --check` verifies the adapter's imports and exits; it is a local import check, so a
# machine that needs longer than this is wedged, not slow.
ACP_CHECK_TIMEOUT_SECONDS = 30

UV_RELEASE = "0.11.28"


@dataclass(frozen=True)
class UvBuild:
    """A pinned `uv` release build. Verified against its published SHA-256 before it is run — it is
    a binary we download and then execute."""

    target: str
    url: str
    sha256: str


def _uv_build(target: str, sha256: str) -> UvBuild:
    # uv ships Windows as a .zip and every other platform as a .tar.gz.
    ext = "zip" if "windows" in target else "tar.gz"
    return UvBuild(
        target=target,
        url=f"https://github.com/astral-sh/uv/releases/download/{UV_RELEASE}/uv-{target}.{ext}",
        sha256=sha256,
    )


# Pinned per OS+arch. Getting the arch wrong is not cosmetic: an x86_64 `uv` installs an x86_64
# CPython, and everything downstream then believes the machine is x86_64.
UV_BUILDS: dict[str, UvBuild] = {
    "aarch64-apple-darwin": _uv_build(
        "aarch64-apple-darwin",
        "33540eb7c883ab857eff79bd5ac2aa31fe27b595abecb4a9c003a2c998447232",
    ),
    "x86_64-apple-darwin": _uv_build(
        "x86_64-apple-darwin",
        "2ad79983127ffca7d77b77ce6a24278d7e4f7b817a1acf72fea5f8124b4aac5e",
    ),
    "x86_64-pc-windows-msvc": _uv_build(
        "x86_64-pc-windows-msvc",
        "0a23463216d09c6a72ff80ef5dc5a795f07dc1575cb84d24596c2f124a441b7b",
    ),
    "aarch64-pc-windows-msvc": _uv_build(
        "aarch64-pc-windows-msvc",
        "3248109afad3ec59baad299d324ff53de17e2d9a3b3e21580ffd26744b11e036",
    ),
    "x86_64-unknown-linux-gnu": _uv_build(
        "x86_64-unknown-linux-gnu",
        "e490a6464492183c5d4534a5527fb4440f7f2bb2f228162ad7e4afe076dc0224",
    ),
    "aarch64-unknown-linux-gnu": _uv_build(
        "aarch64-unknown-linux-gnu",
        "03e9fe0a81b0718d0bc84625de3885df6cc3f89a8b6af6121d6b9f6113fb6533",
    ),
}


def _is_windows() -> bool:
    return platform.system() == "Windows"


def _exe(name: str) -> str:
    """The executable's on-disk name — `.exe`-suffixed on Windows, bare elsewhere."""
    return f"{name}.exe" if _is_windows() else name


def platform_target() -> str:
    """The `<arch>-<os>` triple uv (and Hermes' private CPython) build for this machine."""
    machine = arch.normalized_machine()
    system = platform.system()
    if system == "Darwin":
        os_part = "apple-darwin"
    elif system == "Windows":
        os_part = "pc-windows-msvc"
    elif system == "Linux":
        os_part = "unknown-linux-gnu"
    else:
        raise SystemExit(f"Hermes cannot be installed on {system!r}: no uv build for it.")
    return f"{machine}-{os_part}"


def pick_uv_build() -> UvBuild:
    """The `uv` build for this machine's OS and architecture."""
    target = platform_target()
    build = UV_BUILDS.get(target)
    if not build:
        raise SystemExit(f"No uv build for {target!r}, so Hermes cannot be installed here.")
    return build


def hermes_bin() -> Path:
    return paths.bin_dir() / _exe("hermes")


def uv_bin() -> Path:
    return paths.bin_dir() / _exe("uv")


def is_installed() -> bool:
    return hermes_bin().is_file()


def acp_ready() -> bool:
    """Whether the installed Hermes can actually serve ACP — the mode the Grid app drives it in.

    A binary on disk is not the same thing as a working agent: an install made before we asked for
    the `[acp]` extra leaves `hermes` runnable and `hermes acp` dead. Hermes answers that question
    itself with `acp --check`, so we ask it rather than guessing from the venv's contents.
    """
    hermes = hermes_bin()
    if not hermes.is_file():
        return False
    try:
        result = subprocess.run(
            [str(hermes), "acp", "--check"],
            capture_output=True,
            timeout=ACP_CHECK_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def ensure_uv() -> Path:
    """The pinned `uv`, downloading it on first use. Idempotent."""
    target = uv_bin()
    if target.is_file():
        return target

    build = pick_uv_build()
    paths.bin_dir().mkdir(parents=True, exist_ok=True)
    uv_name = _exe("uv")
    with tempfile.TemporaryDirectory(prefix="grid-agent-") as tmpdir:
        extracted = fetch_and_extract(build.target, build.url, build.sha256, Path(tmpdir))
        found = next((path for path in extracted.rglob(uv_name) if path.is_file()), None)
        if not found:
            raise SystemExit(f"Extracted archive did not contain {uv_name}: {build.target}")
        shutil.copy2(found, target)
    if not _is_windows():
        target.chmod(0o755)
    return target


def install_hermes() -> Path:
    """Install (or upgrade) Hermes into ~/.grid/bin. Streams uv's own progress to the console, so a
    caller watching stdout can show what is happening during a slow first install."""
    paths.ensure_all()
    uv = ensure_uv()

    # Keep uv's tool tree, and the CPython it downloads, inside ~/.grid rather than the user's
    # home — Grid installed them, so Grid's directory owns them.
    env = {
        **os.environ,
        "UV_TOOL_BIN_DIR": str(paths.bin_dir()),
        "UV_TOOL_DIR": str(paths.tools_dir()),
        "UV_PYTHON_INSTALL_DIR": str(paths.python_dir()),
    }
    print(
        f"Installing {HERMES_REQUIREMENT} (this downloads a private Python; it can take a minute) ..."
    )
    result = subprocess.run(
        [str(uv), "tool", "install", "--force", "--python", HERMES_PYTHON, HERMES_REQUIREMENT],
        env=env,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"uv could not install {HERMES_REQUIREMENT} (exit {result.returncode}).")

    target = hermes_bin()
    if not target.is_file():
        raise SystemExit(f"uv reported success but {target} is missing.")
    return target
