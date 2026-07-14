"""Install the Hermes agent into ~/.grid, without a package manager.

Hermes ships as a Python package, so the obvious route is `brew install hermes-agent`. We do not
take it: installing Homebrew needs an interactive `sudo`, which a GUI app cannot drive, and that
dead-ended the app's hands-off setup. Instead we fetch a pinned `uv` — a single static binary — and
let it install Hermes together with its own private CPython. Nothing leaves ~/.grid, nothing needs
admin rights, and uninstalling is a directory removal.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from shared import paths
from shared.engine.installer import fetch_and_extract, is_macos
from shared.system import arch

# Hermes requires >=3.11,<3.14. Pin the interpreter so an install never silently drifts onto a
# version the package cannot run on.
HERMES_PACKAGE = "hermes-agent"
HERMES_PYTHON = "3.13"

UV_RELEASE = "0.11.28"


@dataclass(frozen=True)
class UvBuild:
    """A pinned `uv` release build. Verified against its published SHA-256 before it is run — it is
    a binary we download and then execute."""

    label: str
    url: str
    sha256: str


def _uv_build(release: str, target: str, sha256: str) -> UvBuild:
    return UvBuild(
        label=target,
        url=f"https://github.com/astral-sh/uv/releases/download/{release}/uv-{target}.tar.gz",
        sha256=sha256,
    )


UV_BUILDS: dict[str, UvBuild] = {
    "arm64": _uv_build(
        UV_RELEASE,
        "aarch64-apple-darwin",
        "33540eb7c883ab857eff79bd5ac2aa31fe27b595abecb4a9c003a2c998447232",
    ),
    "x86_64": _uv_build(
        UV_RELEASE,
        "x86_64-apple-darwin",
        "2ad79983127ffca7d77b77ce6a24278d7e4f7b817a1acf72fea5f8124b4aac5e",
    ),
}


def pick_uv_build(machine: str) -> UvBuild:
    """The `uv` build for this Mac's architecture.

    Getting this wrong is not cosmetic: an x86_64 `uv` under Rosetta installs an x86_64 CPython,
    and everything downstream then believes the machine is an Intel Mac.
    """
    key = "arm64" if machine in ("arm64", "aarch64") else machine
    build = UV_BUILDS.get(key)
    if not build:
        raise SystemExit(f"No uv build for macOS {machine!r}, so Hermes cannot be installed here.")
    return build


def hermes_bin() -> Path:
    return paths.bin_dir() / "hermes"


def uv_bin() -> Path:
    return paths.bin_dir() / "uv"


def is_installed() -> bool:
    return hermes_bin().is_file()


def ensure_uv() -> Path:
    """The pinned `uv`, downloading it on first use. Idempotent."""
    target = uv_bin()
    if target.is_file():
        return target

    build = pick_uv_build(arch.native_machine())
    paths.bin_dir().mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="grid-agent-") as tmpdir:
        extracted = fetch_and_extract(build.label, build.url, build.sha256, Path(tmpdir))
        found = next((path for path in extracted.rglob("uv") if path.is_file()), None)
        if not found:
            raise SystemExit(f"Extracted archive did not contain uv: {build.label}")
        shutil.copy2(found, target)
    target.chmod(0o755)
    return target


def install_hermes() -> Path:
    """Install (or upgrade) Hermes into ~/.grid/bin. Streams uv's own progress to the console, so a
    caller watching stdout can show what is happening during a slow first install."""
    if not is_macos():
        raise SystemExit("Hermes auto-install is macOS-only for now. Install it yourself, then re-run.")
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
    print(f"Installing {HERMES_PACKAGE} (this downloads a private Python; it can take a minute) ...")
    result = subprocess.run(
        [str(uv), "tool", "install", "--force", "--python", HERMES_PYTHON, HERMES_PACKAGE],
        env=env,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"uv could not install {HERMES_PACKAGE} (exit {result.returncode}).")

    target = hermes_bin()
    if not target.is_file():
        raise SystemExit(f"uv reported success but {target} is missing.")
    return target
