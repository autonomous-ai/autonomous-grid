"""Install or upgrade llama.cpp into ~/.grid/bin."""

from __future__ import annotations

import hashlib
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
from shared.system import gpu


@dataclass(frozen=True)
class TarballPin:
    label: str
    url: str
    sha256: str
    supports_sm: tuple[str, ...]


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
        archive = tmp / Path(tarball.url).name
        print(f"Downloading {tarball.label} from {tarball.url} ...")
        _download(tarball.url, archive)
        got = _sha256(archive)
        if got != tarball.sha256:
            raise SystemExit(f"SHA-256 mismatch for {tarball.label}: expected {tarball.sha256}, got {got}")
        extracted = tmp / "extract"
        _extract(archive, extracted)
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


def install_macos_homebrew() -> Path:
    paths.ensure_all()
    brew = shutil.which("brew")
    if not brew:
        raise SystemExit(
            "Homebrew is required for the Apple Silicon prebuilt llama.cpp install. "
            "Install Homebrew from https://brew.sh/ or re-run with --from-source."
        )

    installed = subprocess.run(
        [brew, "list", "--formula", "llama.cpp"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0
    if installed:
        print("Upgrading Homebrew formula llama.cpp ...")
        subprocess.check_call([brew, "upgrade", "llama.cpp"])
    else:
        print("Installing Homebrew formula llama.cpp ...")
        subprocess.check_call([brew, "install", "llama.cpp"])

    source = _homebrew_llama_server_path(brew)
    if not source:
        raise SystemExit(
            "Homebrew completed, but llama-server was not found in the llama.cpp formula. "
            "Ensure Homebrew's bin directory is on PATH or set LLAMA_SERVER."
        )

    target = paths.llama_server_bin()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        if target.is_dir():
            raise SystemExit(f"Cannot install llama-server because {target} is a directory.")
        target.unlink()
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


def is_apple_silicon() -> bool:
    return platform.system() == "Darwin" and platform.machine() in ("arm64", "aarch64")


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


def _download(url: str, dest: Path) -> None:
    with httpx.stream("GET", url, timeout=httpx.Timeout(30, read=None), follow_redirects=True) as resp:
        if resp.status_code != 200:
            raise SystemExit(f"Download failed ({resp.status_code}): {url}")
        with dest.open("wb") as fh:
            for chunk in resp.iter_bytes(1 << 20):
                fh.write(chunk)


def _extract(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
    elif archive.suffixes[-2:] == [".tar", ".gz"] or archive.suffix == ".tgz":
        with tarfile.open(archive, "r:gz") as tf:
            tf.extractall(dest)
    elif archive.suffix == ".tar":
        with tarfile.open(archive, "r:") as tf:
            tf.extractall(dest)
    else:
        raise SystemExit(f"Unsupported archive type: {archive.name}")


def _locate_llama_server(root: Path) -> Path | None:
    for path in root.rglob("llama-server"):
        if path.is_file():
            return path
    return None


def _homebrew_llama_server_path(brew: str) -> Path | None:
    candidates: list[Path] = []
    formula_prefix = _brew_prefix(brew, "llama.cpp")
    if formula_prefix:
        candidates.append(formula_prefix / "bin" / "llama-server")
    brew_prefix = _brew_prefix(brew)
    if brew_prefix:
        candidates.append(brew_prefix / "bin" / "llama-server")

    on_path = shutil.which("llama-server")
    if on_path:
        path = Path(on_path)
        if path != paths.llama_server_bin():
            candidates.append(path)

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _brew_prefix(brew: str, formula: str | None = None) -> Path | None:
    args = [brew, "--prefix"]
    if formula:
        args.append(formula)
    try:
        output = subprocess.check_output(args, text=True).strip()
    except subprocess.CalledProcessError:
        return None
    return Path(output) if output else None


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

