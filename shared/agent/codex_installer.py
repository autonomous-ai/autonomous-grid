"""Install the Codex agent into ~/.grid/bin from OpenAI's official release binaries.

Codex ships as a prebuilt binary per OS/arch on GitHub — no npm, no package manager. We fetch the
pinned archive for this machine, verify it against its published SHA-256, and drop the binary into
~/.grid/bin: the same no-admin-rights, package-manager-free path the engine and Hermes installers
take. Nothing leaves ~/.grid, and uninstalling is a file removal.
"""

from __future__ import annotations

import platform
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from shared import paths
from shared.engine.installer import fetch_and_extract
from shared.system import arch

# Pin the release rather than tracking `latest`, so an install is reproducible and its SHA-256 can
# be checked — the binary is fetched over the network and then executed.
CODEX_RELEASE = "rust-v0.144.6"


@dataclass(frozen=True)
class CodexBuild:
    """A pinned Codex release build, verified against its published SHA-256 before it is run."""

    target: str
    url: str
    sha256: str


def _codex_build(target: str, asset: str, sha256: str) -> CodexBuild:
    return CodexBuild(
        target=target,
        url=f"https://github.com/openai/codex/releases/download/{CODEX_RELEASE}/{asset}",
        sha256=sha256,
    )


# Pinned per OS+arch. Windows ships as `.exe.zip`, the Unixes as `.tar.gz`; Linux takes the static
# musl build so it runs without a matching system glibc.
CODEX_BUILDS: dict[str, CodexBuild] = {
    "aarch64-apple-darwin": _codex_build(
        "aarch64-apple-darwin",
        "codex-aarch64-apple-darwin.tar.gz",
        "023590f828bc9507ac61132ee35e74d3c5d33fb5ba3e1ca4fc2e013a2f71a3d7",
    ),
    "x86_64-apple-darwin": _codex_build(
        "x86_64-apple-darwin",
        "codex-x86_64-apple-darwin.tar.gz",
        "763c81a56ba24a4f6c2fd256ed7ee1775caeccd22537d28887de8f6864ac5947",
    ),
    "x86_64-pc-windows-msvc": _codex_build(
        "x86_64-pc-windows-msvc",
        "codex-x86_64-pc-windows-msvc.exe.zip",
        "0048604040fe61fa6163238fb0fcbda79e6bc465a8eecafc8f5ae8e4b69f77fd",
    ),
    "aarch64-pc-windows-msvc": _codex_build(
        "aarch64-pc-windows-msvc",
        "codex-aarch64-pc-windows-msvc.exe.zip",
        "de13275b7e31731474e0c1bce68ceaa07ba85ceecf63a1a4a9d5f7f58275b2d2",
    ),
    "x86_64-unknown-linux-musl": _codex_build(
        "x86_64-unknown-linux-musl",
        "codex-x86_64-unknown-linux-musl.tar.gz",
        "6a9def51a0ad8cea6684d8eb3bf033c89f33e3bc5cfe492f1a1e0a718451a1c6",
    ),
    "aarch64-unknown-linux-musl": _codex_build(
        "aarch64-unknown-linux-musl",
        "codex-aarch64-unknown-linux-musl.tar.gz",
        "8eddae5e6c009dff9ba51ae1bfe3bdd9ff4c1ccc93a48cc6860db1cd9fdf11be",
    ),
}


def _is_windows() -> bool:
    return platform.system() == "Windows"


def platform_target() -> str:
    """The `<arch>-<os>` triple Codex ships a binary for on this machine."""
    machine = arch.normalized_machine()
    system = platform.system()
    if system == "Darwin":
        os_part = "apple-darwin"
    elif system == "Windows":
        os_part = "pc-windows-msvc"
    elif system == "Linux":
        os_part = "unknown-linux-musl"
    else:
        raise SystemExit(f"Codex cannot be installed on {system!r}: no build for it.")
    return f"{machine}-{os_part}"


def pick_codex_build() -> CodexBuild:
    """The Codex build for this machine's OS and architecture."""
    target = platform_target()
    build = CODEX_BUILDS.get(target)
    if not build:
        raise SystemExit(f"No Codex build for {target!r}, so Codex cannot be installed here.")
    return build


def codex_bin() -> Path:
    return paths.bin_dir() / ("codex.exe" if _is_windows() else "codex")


def is_installed() -> bool:
    return codex_bin().is_file()


def _locate_codex(root: Path) -> Path | None:
    """Find the Codex executable inside the extracted archive.

    The archive holds a single binary, but its name varies between releases — a bare ``codex``/
    ``codex.exe`` or the full ``codex-<target>`` asset stem — so we match by prefix and take the
    largest file: the binary dwarfs any bundled README or licence.
    """
    candidates = [
        path
        for path in root.rglob("*")
        if path.is_file() and path.name.lower().startswith("codex")
    ]
    if _is_windows():
        exes = [path for path in candidates if path.suffix.lower() == ".exe"]
        candidates = exes or candidates
    if not candidates:
        return None
    wanted = "codex.exe" if _is_windows() else "codex"
    for path in candidates:
        if path.name.lower() == wanted:
            return path
    return max(candidates, key=lambda path: path.stat().st_size)


def install_codex() -> Path:
    """Install (or upgrade) Codex into ~/.grid/bin from the pinned release archive."""
    paths.ensure_all()
    build = pick_codex_build()
    target = codex_bin()
    print(f"Installing codex ({build.target}) ...")
    with tempfile.TemporaryDirectory(prefix="grid-agent-") as tmpdir:
        extracted = fetch_and_extract(build.target, build.url, build.sha256, Path(tmpdir))
        found = _locate_codex(extracted)
        if not found:
            raise SystemExit(f"Extracted archive did not contain the codex binary: {build.target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            target.unlink()
        shutil.copy2(found, target)
    if not _is_windows():
        target.chmod(0o755)
    return target
