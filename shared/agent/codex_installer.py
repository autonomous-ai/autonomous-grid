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
CODEX_RELEASE = "rust-v0.150.1"


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
        "f66f1c45f1eda49d6a8aef86faee24121b0c8913cd9023f23ee44262606fc7b6",
    ),
    "x86_64-apple-darwin": _codex_build(
        "x86_64-apple-darwin",
        "codex-x86_64-apple-darwin.tar.gz",
        "d00bdeb113c2cb42b43fbe4916b681ab1405772ac38fc8ac7fa9cc0934d1d0aa",
    ),
    "x86_64-pc-windows-msvc": _codex_build(
        "x86_64-pc-windows-msvc",
        "codex-x86_64-pc-windows-msvc.exe.zip",
        "6b4b13811c2e0a2dc7a79ad94686b7b665e69407c9dc25cdbc2dadfc31dd8e19",
    ),
    "aarch64-pc-windows-msvc": _codex_build(
        "aarch64-pc-windows-msvc",
        "codex-aarch64-pc-windows-msvc.exe.zip",
        "589e1c49d7b0fac369913c5f8195b49bd6fd458954ed47cd76c9b7e8f46eb056",
    ),
    "x86_64-unknown-linux-musl": _codex_build(
        "x86_64-unknown-linux-musl",
        "codex-x86_64-unknown-linux-musl.tar.gz",
        "ab308870bc7fc048c23dc49d03f6b8af9ce7fc99b9da882d6688be7a90155c7a",
    ),
    "aarch64-unknown-linux-musl": _codex_build(
        "aarch64-unknown-linux-musl",
        "codex-aarch64-unknown-linux-musl.tar.gz",
        "5bb1f75e1a1588845b4a31f2c98fb2b394be5c2a8d90a24a8ab0ebbae1169264",
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
