"""Install or upgrade llama.cpp into ~/.grid/bin."""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from shared import paths
from shared.system import arch


@dataclass(frozen=True)
class LinuxBuild:
    """A pinned official llama.cpp release build for Linux.

    There is no CUDA entry here, and that is not an omission: upstream publishes CUDA binaries for
    **Windows only** (`llama-*-bin-win-cuda-12.4-x64.zip` and friends). Every Linux asset in a
    llama.cpp release is CPU, Vulkan, ROCm, SYCL or OpenVINO. CUDA on Linux has to be compiled, which
    is what `--from-source` does. Vulkan runs on NVIDIA cards perfectly well and needs no toolchain,
    so it is what an NVIDIA box gets when the operator has not asked to build.
    """

    label: str
    url: str
    sha256: str


@dataclass(frozen=True)
class MacosBuild:
    """A pinned official llama.cpp release build for macOS. Pinning the release (rather than
    tracking `latest`) keeps the download reproducible and lets us check it against a known
    SHA-256 — the binaries are fetched over the network, so they are verified before they run."""

    label: str
    url: str
    sha256: str


LLAMA_RELEASE = "b10369"
_RELEASE_BASE = f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_RELEASE}"

MACOS_BUILDS: dict[str, MacosBuild] = {
    "arm64": MacosBuild(
        label="macos-arm64",
        url=f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_RELEASE}/llama-{LLAMA_RELEASE}-bin-macos-arm64.tar.gz",
        sha256="de2ac2c0a7cc245bce2411393658ff19c9c00d9d1fe37c5dfe94668c0d7bc01f",
    ),
    "x86_64": MacosBuild(
        label="macos-x64",
        url=f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_RELEASE}/llama-{LLAMA_RELEASE}-bin-macos-x64.tar.gz",
        sha256="3cd137ae474fe4a55dcbcf319b94b36fe136571e6ca680d3eb8a175aa6ff1717",
    ),
}


def _linux_asset(kind: str, machine: str) -> str:
    suffix = "x64" if machine == "x86_64" else "arm64"
    infix = "vulkan-" if kind == "vulkan" else ""
    return f"llama-{LLAMA_RELEASE}-bin-ubuntu-{infix}{suffix}.tar.gz"


LINUX_BUILDS: dict[tuple[str, str], LinuxBuild] = {
    ("vulkan", "x86_64"): LinuxBuild(
        label="linux-vulkan-x64",
        url=f"{_RELEASE_BASE}/{_linux_asset('vulkan', 'x86_64')}",
        sha256="baa1deb5adda0baf72fc9d213d657b8388997d0e20b1a0e8bea48ff91b5cad00",
    ),
    ("vulkan", "aarch64"): LinuxBuild(
        label="linux-vulkan-arm64",
        url=f"{_RELEASE_BASE}/{_linux_asset('vulkan', 'aarch64')}",
        sha256="12f09eb4dc7df11940deda07329bf6b0bb643bf5aad323b8af778689528187f4",
    ),
    ("cpu", "x86_64"): LinuxBuild(
        label="linux-cpu-x64",
        url=f"{_RELEASE_BASE}/{_linux_asset('cpu', 'x86_64')}",
        sha256="675a266f6cc8a8c7b85dc431a2472e372d0ff3741b7f4eb153dc786dff3964d1",
    ),
    ("cpu", "aarch64"): LinuxBuild(
        label="linux-cpu-arm64",
        url=f"{_RELEASE_BASE}/{_linux_asset('cpu', 'aarch64')}",
        sha256="7a806180a5136358b76cc654eebf98efb6c7d6b0f6879a55e69697d944bd91f1",
    ),
}


def pick_linux_build(kind: str, machine: str) -> LinuxBuild:
    """The official Linux build for this backend and architecture."""
    build = LINUX_BUILDS.get((kind, machine))
    if not build:
        raise SystemExit(
            f"No prebuilt llama.cpp for Linux {machine!r} ({kind}). "
            "Re-run with --from-source to build it here."
        )
    return build


def install_linux_prebuilt(kind: str) -> Path:
    """Install llama.cpp on Linux from the project's official release tarball."""
    paths.ensure_all()
    build = pick_linux_build(kind, arch.normalized_machine())
    with tempfile.TemporaryDirectory(prefix="grid-engine-") as tmpdir:
        extracted = fetch_and_extract(build.label, build.url, build.sha256, Path(tmpdir))
        server = _locate_llama_server(extracted)
        if not server:
            raise SystemExit(f"Extracted archive did not contain llama-server: {build.label}")
        return _install_prefix(server.parent)


def pick_macos_build(machine: str) -> MacosBuild:
    """The official build for this Mac's architecture. `aarch64` is an alias some Pythons report
    for Apple Silicon."""
    key = "arm64" if machine in ("arm64", "aarch64") else machine
    build = MACOS_BUILDS.get(key)
    if not build:
        raise SystemExit(
            f"No prebuilt llama.cpp for macOS {machine!r}. Re-run with --from-source to build it."
        )
    return build


def install_macos_prebuilt() -> Path:
    """Install llama.cpp on macOS from the project's official release tarball.

    Deliberately does NOT use Homebrew: installing Homebrew needs an interactive `sudo`, which a
    GUI app cannot drive, so it dead-ended the app's hands-off setup. The tarball needs no package
    manager and no admin rights — it is unpacked under the user's own `~/.grid`."""
    paths.ensure_all()
    build = pick_macos_build(arch.native_machine())
    with tempfile.TemporaryDirectory(prefix="grid-engine-") as tmpdir:
        extracted = fetch_and_extract(build.label, build.url, build.sha256, Path(tmpdir))
        server = _locate_llama_server(extracted)
        if not server:
            raise SystemExit(f"Extracted archive did not contain llama-server: {build.label}")
        return _install_prefix(server.parent)


def _install_prefix(source: Path) -> Path:
    """Place `llama-server` and the shared libraries it loads into their own directory, then point
    `~/.grid/bin/llama-server` at it. The binary resolves its libraries relatively — `@loader_path`
    on macOS, `$ORIGIN` on Linux — so they must sit beside it; copying the binary alone yields one
    that cannot start."""
    prefix = paths.llama_prefix_dir()
    if prefix.exists():
        shutil.rmtree(prefix)
    prefix.mkdir(parents=True, exist_ok=True)

    shutil.copy2(source / "llama-server", prefix / "llama-server")
    # `.so*`, not `.so`: Linux releases ship versioned sonames (`libggml-base.so.0`) that the
    # binary loads by their full name, so a bare `*.so` glob installs an engine that cannot start.
    for lib in [*source.glob("*.dylib"), *source.glob("*.so*")]:
        target = prefix / lib.name
        # Keep the release's versioned aliases as links; following them would copy each library
        # several times over.
        if lib.is_symlink():
            target.symlink_to(os.readlink(lib))
            continue
        shutil.copy2(lib, target)

    server = prefix / "llama-server"
    server.chmod(0o755)
    return _link_bin(server)


def _link_bin(source: Path) -> Path:
    """Expose [source] as `~/.grid/bin/llama-server`, the one path the rest of Grid looks for."""
    target = paths.llama_server_bin()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        raise SystemExit(f"Cannot install llama-server because {target} is a directory.")
    target.symlink_to(source)
    return target


def install_from_source(target_sm: str) -> Path:
    paths.ensure_all()
    require_toolchain()
    sm_digits = target_sm.removeprefix("sm_")
    require_cuda_arch(sm_digits)
    src = _ensure_llama_cpp_source()
    build = src / "build"
    build.mkdir(parents=True, exist_ok=True)
    print(f"Configuring CUDA build for CMAKE_CUDA_ARCHITECTURES={sm_digits} ...")
    # No -DCMAKE_BUILD_TYPE: llama.cpp's own CMakeLists already forces Release when the caller
    # leaves it unset. And a plain `120` is correct — llama.cpp rewrites any `12X` to the
    # architecture-specific `12Xa` itself, because Blackwell's FP4 tensor-core instructions are
    # not forwards compatible.
    subprocess.check_call(
        [
            "cmake",
            "-S",
            str(src),
            "-B",
            str(build),
            "-DGGML_CUDA=ON",
            f"-DCMAKE_CUDA_ARCHITECTURES={sm_digits}",
        ]
    )
    _build_target(build)
    return _install_prefix(_built_server(build).parent)


def install_metal_from_source() -> Path:
    paths.ensure_all()
    require_metal_toolchain()
    src = _ensure_llama_cpp_source()
    build = src / "build-metal"
    build.mkdir(parents=True, exist_ok=True)
    print("Configuring Metal build for Apple Silicon ...")
    subprocess.check_call(
        [
            "cmake",
            "-S",
            str(src),
            "-B",
            str(build),
            "-DGGML_METAL=ON",
            "-DCMAKE_BUILD_TYPE=Release",
        ]
    )
    _build_target(build)
    return _install_prefix(_built_server(build).parent)


def _build_target(build: Path, target: str = "llama-server") -> None:
    """Compile one target, with the job count BOUNDED.

    A bare `-j` is passed straight through to make, which reads it as "unlimited jobs". A CUDA
    build is hundreds of nvcc invocations that each want a GB or more, so unlimited means the
    machine runs itself out of memory partway through a long build. Leave one core for the rest
    of the system.
    """
    jobs = max(1, (os.cpu_count() or 2) - 1)
    subprocess.check_call(
        ["cmake", "--build", str(build), "--target", target, "--config", "Release", "-j", str(jobs)]
    )


def _built_server(build: Path) -> Path:
    """The freshly built `llama-server`, preferring the canonical output directory.

    llama.cpp sets `CMAKE_RUNTIME_OUTPUT_DIRECTORY` to `<build>/bin`, so that is where the binary
    and the shared libraries it needs land together. A bare `rglob` can also match copies left
    elsewhere in the tree and returns them in filesystem order, so picking the first result was
    a coin flip between the real output and a stale one.
    """
    canonical = build / "bin" / "llama-server"
    if canonical.is_file():
        return canonical
    found = sorted(build.rglob("llama-server"))
    if not found:
        raise SystemExit("Build completed but llama-server binary was not found.")
    return found[0]


BUILD_TOOLS = ("cmake", "g++", "nvcc", "git")


def _nvcc_architectures() -> set[str]:
    """What this CUDA toolkit can compile for, as ``{"compute_50", ...}``; empty if unknowable."""
    try:
        out = subprocess.run(
            ["nvcc", "--list-gpu-arch"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    return {line.strip() for line in (out.stdout or "").splitlines() if line.strip().startswith("compute_")}


def cuda_build_readiness(sm_digits: str) -> tuple[bool, str]:
    """Can this machine compile the CUDA engine for [sm_digits], and if not, what is in the way.

    One prober, two callers, so the answer cannot differ between them: `--from-source` raises this
    as an error before doing any work, and the plain install prints it as guidance. The whole point
    is that nobody has to run `nvcc --list-gpu-arch` by hand to find out where they stand.

    Returns ``(ready, message)``. When not ready the message is a complete, actionable paragraph.
    """
    missing = [tool for tool in BUILD_TOOLS if shutil.which(tool) is None]
    if missing:
        return False, (
            f"CUDA needs {', '.join(missing)}, which this machine does not have.\n"
            f"    {_toolchain_hint()}"
        )
    listed = _nvcc_architectures()
    # An empty list means nvcc would not tell us — say nothing rather than block a build that may
    # be perfectly fine, and let the compiler have the final word.
    if listed and f"compute_{sm_digits}" not in listed:
        newest = max(listed, key=lambda a: int(a.removeprefix("compute_").rstrip("af") or 0))
        return False, (
            f"This CUDA toolkit cannot build for compute_{sm_digits} — the newest it supports is "
            f"{newest}. RTX 50-series needs CUDA 12.8 or newer; install a current toolkit from "
            "NVIDIA rather than your distro's default package."
        )
    return True, ""


def require_cuda_arch(sm_digits: str) -> None:
    """Refuse to start a long build this machine cannot finish."""
    ready, message = cuda_build_readiness(sm_digits)
    if not ready:
        raise SystemExit(message)


def is_macos() -> bool:
    return platform.system() == "Darwin"


def _toolchain_hint() -> str:
    """The install command for this distro's build tools."""
    distro = _detect_distro()
    if distro == "debian":
        return "sudo apt update && sudo apt install -y build-essential cmake git nvidia-cuda-toolkit"
    if distro == "rhel":
        return "sudo dnf install -y @development-tools cmake git cuda-toolkit"
    return "Install gcc/g++, cmake, git, and the CUDA toolkit via your distro's package manager."


def require_toolchain() -> None:
    missing = [tool for tool in BUILD_TOOLS if shutil.which(tool) is None]
    if not missing:
        return
    raise SystemExit(
        f"Missing required build tools: {', '.join(missing)}.\nInstall them with:\n  {_toolchain_hint()}"
    )


def require_metal_toolchain() -> None:
    missing = [tool for tool in ("cmake", "git") if shutil.which(tool) is None]
    clang_ok = False
    if shutil.which("xcrun"):
        result = subprocess.run(
            ["xcrun", "--find", "clang"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        clang_ok = result.returncode == 0
    if not clang_ok and shutil.which("clang"):
        clang_ok = True
    if not clang_ok:
        missing.append("Xcode Command Line Tools")
    if missing:
        raise SystemExit(
            f"Missing required build tools: {', '.join(missing)}.\n"
            "Install them with:\n"
            "  xcode-select --install\n"
            "  brew install cmake git"
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_and_extract(label: str, url: str, sha256: str, tmp: Path) -> Path:
    """Download a pinned archive into [tmp], check it against its SHA-256, and unpack it. The hash
    is the only thing standing between a network fetch and code we execute, so a mismatch aborts."""
    archive = tmp / Path(url).name
    print(f"Downloading {label} from {url} ...")
    _download(url, archive)
    got = _sha256(archive)
    if got != sha256:
        raise SystemExit(f"SHA-256 mismatch for {label}: expected {sha256}, got {got}")
    extracted = tmp / "extract"
    _extract(archive, extracted)
    return extracted


def _download(url: str, dest: Path) -> None:
    with httpx.stream("GET", url, timeout=httpx.Timeout(30, read=None), follow_redirects=True) as resp:
        if resp.status_code != 200:
            raise SystemExit(f"Download failed ({resp.status_code}): {url}")
        with dest.open("wb") as fh:
            for chunk in resp.iter_bytes(1 << 20):
                fh.write(chunk)


def _extract(archive: Path, dest: Path) -> None:
    """Unpack a downloaded archive. Tars are extracted with the `data` filter so a member cannot
    write outside [dest] (absolute paths, `..`, escaping symlinks) — this unpacks a file fetched
    over the network."""
    dest.mkdir(parents=True, exist_ok=True)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
    elif archive.suffixes[-2:] == [".tar", ".gz"] or archive.suffix == ".tgz":
        with tarfile.open(archive, "r:gz") as tf:
            tf.extractall(dest, filter="data")
    elif archive.suffix == ".tar":
        with tarfile.open(archive, "r:") as tf:
            tf.extractall(dest, filter="data")
    else:
        raise SystemExit(f"Unsupported archive type: {archive.name}")


def _locate_llama_server(root: Path) -> Path | None:
    for path in root.rglob("llama-server"):
        if path.is_file():
            return path
    return None




_LLAMA_REPO = "https://github.com/ggml-org/llama.cpp"


def _ensure_llama_cpp_source() -> Path:
    """A checkout of llama.cpp at exactly the release Grid pins everywhere else.

    Two bugs used to live here. It cloned the default branch, so `--from-source` built whatever
    master happened to be that day while the prebuilt path installed a pinned, checksummed
    release — one command, two versions. And an existing directory was returned untouched, so a
    second run silently rebuilt a checkout that could be months old.
    """
    src = paths.home() / "src" / "llama.cpp"
    src.parent.mkdir(parents=True, exist_ok=True)
    if not (src / ".git").is_dir():
        if src.exists():
            shutil.rmtree(src)
        print(f"Cloning llama.cpp {LLAMA_RELEASE} into {src} ...")
        subprocess.check_call(
            ["git", "clone", "--depth", "1", "--branch", LLAMA_RELEASE, _LLAMA_REPO, str(src)]
        )
        return src

    print(f"Updating {src} to {LLAMA_RELEASE} ...")
    try:
        subprocess.check_call(
            ["git", "-C", str(src), "fetch", "--depth", "1", "origin", "tag", LLAMA_RELEASE]
        )
        subprocess.check_call(["git", "-C", str(src), "checkout", "--force", LLAMA_RELEASE])
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            f"Could not move {src} to {LLAMA_RELEASE} ({exc}). Delete that directory and re-run "
            "to get a clean checkout."
        ) from exc
    return src


def _detect_distro() -> str:
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8")
    except OSError:
        return "other"
    ids: list[str] = []
    for line in text.splitlines():
        if line.startswith("ID=") or line.startswith("ID_LIKE="):
            ids.extend(line.split("=", 1)[1].strip().strip('"').split())
    for token in ids:
        if token in ("debian", "ubuntu"):
            return "debian"
        if token in ("rhel", "centos", "fedora", "rocky", "almalinux"):
            return "rhel"
    return "other"

