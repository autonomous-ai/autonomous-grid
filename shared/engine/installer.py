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
from shared.system import arch, gpu


@dataclass(frozen=True)
class TarballPin:
    label: str
    url: str
    sha256: str
    supports_sm: tuple[str, ...]


@dataclass(frozen=True)
class MacosBuild:
    """A pinned official llama.cpp release build for macOS. Pinning the release (rather than
    tracking `latest`) keeps the download reproducible and lets us check it against a known
    SHA-256 — the binaries are fetched over the network, so they are verified before they run."""

    label: str
    url: str
    sha256: str


LLAMA_RELEASE = "b9985"

MACOS_BUILDS: dict[str, MacosBuild] = {
    "arm64": MacosBuild(
        label="macos-arm64",
        url=f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_RELEASE}/llama-{LLAMA_RELEASE}-bin-macos-arm64.tar.gz",
        sha256="7ac3076397fd7e7cb0d757ec3dc0eb2d876d37aa3021906baa4d197b31758038",
    ),
    "x86_64": MacosBuild(
        label="macos-x64",
        url=f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_RELEASE}/llama-{LLAMA_RELEASE}-bin-macos-x64.tar.gz",
        sha256="e601fb2ae9b1976fbe1cf32aa22382d2a69acc74af8d70c0e2e81a8f08aaeb10",
    ),
}


TARBALLS: tuple[TarballPin, ...] = (
    TarballPin(
        label="cuda-12.4-ampere-ada",
        url="https://github.com/ggml-org/llama.cpp/releases/download/PLACEHOLDER/llama-cuda12.4.zip",
        sha256="PLACEHOLDER",
        supports_sm=("sm_86", "sm_89"),
    ),
    TarballPin(
        label="cuda-12.8-blackwell",
        url="https://github.com/ggml-org/llama.cpp/releases/download/PLACEHOLDER/llama-cuda12.8.zip",
        sha256="PLACEHOLDER",
        supports_sm=("sm_120",),
    ),
)


def pick_tarball(gpus: list[gpu.GpuInfo]) -> TarballPin | None:
    if not gpus:
        return None
    required = {item.compute_cap_sm for item in gpus}
    for tarball in TARBALLS:
        if required.issubset(set(tarball.supports_sm)):
            return tarball
    return None


def install_pinned(tarball: TarballPin) -> Path:
    if tarball.sha256 == "PLACEHOLDER" or "PLACEHOLDER" in tarball.url:
        raise SystemExit(
            f"Pinned tarball {tarball.label!r} has placeholder URL/sha. Fill in real values "
            "in engine/installer.py before running "
            "`grid engine install llama.cpp`, or pass --from-source."
        )
    paths.ensure_all()
    with tempfile.TemporaryDirectory(prefix="grid-engine-") as tmpdir:
        tmp = Path(tmpdir)
        extracted = fetch_and_extract(tarball.label, tarball.url, tarball.sha256, tmp)
        found = _locate_llama_server(extracted)
        if not found:
            raise SystemExit(f"Extracted archive did not contain llama-server: {tarball.label}")
        target = paths.llama_server_bin()
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            if target.is_dir():
                raise SystemExit(f"Cannot install llama-server because {target} is a directory.")
            target.unlink()
        shutil.copy2(found, target)
        target.chmod(0o755)
        return target


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
    `~/.grid/bin/llama-server` at it. The binary resolves its libraries via `@loader_path`, so they
    must sit beside it — copying the binary alone yields one that cannot start."""
    prefix = paths.llama_prefix_dir()
    if prefix.exists():
        shutil.rmtree(prefix)
    prefix.mkdir(parents=True, exist_ok=True)

    shutil.copy2(source / "llama-server", prefix / "llama-server")
    for lib in source.glob("*.dylib"):
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
    src = _ensure_llama_cpp_source()
    build = src / "build"
    build.mkdir(parents=True, exist_ok=True)
    sm_digits = target_sm.removeprefix("sm_")
    print(f"Configuring CUDA build for CMAKE_CUDA_ARCHITECTURES={sm_digits} ...")
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
    subprocess.check_call(["cmake", "--build", str(build), "--target", "llama-server", "-j"])
    candidates = list(build.rglob("llama-server"))
    if not candidates:
        raise SystemExit("Build completed but llama-server binary was not found.")
    target = paths.llama_server_bin()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        if target.is_dir():
            raise SystemExit(f"Cannot install llama-server because {target} is a directory.")
        target.unlink()
    shutil.copy2(candidates[0], target)
    target.chmod(0o755)
    return target


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
    subprocess.check_call(["cmake", "--build", str(build), "--target", "llama-server", "--config", "Release", "-j"])
    candidates = list(build.rglob("llama-server"))
    if not candidates:
        raise SystemExit("Build completed but llama-server binary was not found.")
    target = paths.llama_server_bin()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        if target.is_dir():
            raise SystemExit(f"Cannot install llama-server because {target} is a directory.")
        target.unlink()
    shutil.copy2(candidates[0], target)
    target.chmod(0o755)
    return target


def is_macos() -> bool:
    return platform.system() == "Darwin"


def require_toolchain() -> None:
    missing = [tool for tool in ("cmake", "g++", "nvcc", "git") if shutil.which(tool) is None]
    if not missing:
        return
    distro = _detect_distro()
    if distro == "debian":
        hint = "sudo apt update && sudo apt install -y build-essential cmake git nvidia-cuda-toolkit"
    elif distro == "rhel":
        hint = "sudo dnf install -y @development-tools cmake git cuda-toolkit"
    else:
        hint = "Install gcc/g++, cmake, git, and the CUDA toolkit via your distro's package manager."
    raise SystemExit(f"Missing required build tools: {', '.join(missing)}.\nInstall them with:\n  {hint}")


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




def _ensure_llama_cpp_source() -> Path:
    src = paths.home() / "src" / "llama.cpp"
    src.parent.mkdir(parents=True, exist_ok=True)
    if not src.exists():
        print(f"Cloning llama.cpp into {src} ...")
        subprocess.check_call(["git", "clone", "--depth", "1", "https://github.com/ggml-org/llama.cpp", str(src)])
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

