"""ComfyUI install, start, stop.

Layout:
    ~/.grid/services/ComfyUI/                  ComfyUI source tree
    ~/.grid/services/ComfyUI/.venv/             dedicated venv (uv-managed)
    ~/.grid/services/ComfyUI/custom_nodes/ComfyUI-GGUF
    ~/.grid/public/temp_comfy_output/           runtime output

Why a dedicated venv: ComfyUI pulls torch + many transitive packages that
should not clobber Grid's own runtime env.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import socket
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Iterator, Optional

import httpx

from shared import paths
from shared.system import gpu as gpu_probe


logger = logging.getLogger(__name__)

COMFYUI_REPO = "https://github.com/comfyanonymous/ComfyUI"
COMFYUI_GGUF_REPO = "https://github.com/city96/ComfyUI-GGUF"
COMFYUI_PORT_DEFAULT = 8188

# Pinned working media stack from Interns-Desktop-App's
# additional_services_manager.py. ComfyUI source, custom node, gguf, and the
# Apple Silicon PyTorch stack stay aligned so bundled media workflows resolve
# the same nodes and dependency versions as the Desktop App.
COMFYUI_PINNED_COMMIT = "47ccecaee009cce148e8c2a5bdc2ecb302cc52ee"
COMFYUI_GGUF_PINNED_COMMIT = "6ea2651e7df66d7585f6ffee804b20e92fb38b8a"
GGUF_PINNED_VERSION = "gguf==0.18.0"
# PyTorch nightly (CPU/MPS index below). Bumped 2026-07-02: the prior dev20260423 pins aged out of
# the nightly index (it keeps only ~recent dates), and torchaudio==2.11.0 only exists as a nightly —
# dev20260504 is the earliest date with matching torch/torchvision/torchaudio cp311 arm64 wheels on
# the same 2.13.0 line as the original pin.
TORCH_PINNED = "torch==2.13.0.dev20260504"
TORCHVISION_PINNED = "torchvision==0.27.0.dev20260504"
TORCHAUDIO_PINNED = "torchaudio==2.11.0.dev20260504"
TORCH_NIGHTLY_INDEX = "https://download.pytorch.org/whl/nightly/cpu"
MACOS_MEDIA_PACKAGE_LOCK = "comfyui_macos_package_lock.txt"

# Pip-package pins originally from Interns-Desktop-App commit 5fbb26c ("Enforce
# pinned ComfyUI media stack"). These ship via ComfyUI's requirements.txt and
# have broken vendored workflow JSONs across releases in the past. Re-install
# them after the requirements.txt step so the pin sticks.
#
# NOTE: the desktop reverted 5fbb26c in commit 92e46e3 (no stated reason; the
# revert pulled back a 573-line change, of which this 2-package pin was only a
# slice). The CLI deliberately KEEPS the pin because unpinned frontend/template drift
# is exactly what breaks the workflow JSONs we depend on for cross-client
# node-ID parity. Re-evaluate (and check with the operator) before removing.
COMFYUI_REQUIREMENT_PINS = (
    "comfyui_frontend_package==1.42.14",
    "comfyui_workflow_templates==0.9.62",
)


def services_root() -> Path:
    return paths.home() / "services"


def comfyui_dir() -> Path:
    return services_root() / "ComfyUI"


def comfyui_venv() -> Path:
    return comfyui_dir() / ".venv"


def comfyui_python() -> Path:
    return comfyui_venv() / "bin" / "python"


def output_dir() -> Path:
    return paths.home() / "public" / "temp_comfy_output"


def comfyui_pid_file() -> Path:
    return paths.run_dir() / "comfyui.pid"


def _is_macos() -> bool:
    return platform.system() == "Darwin"


def _is_apple_silicon() -> bool:
    return _is_macos() and platform.machine() in ("arm64", "aarch64")


def _torch_index_for_compute_cap(compute_cap: str | None) -> str:
    """Pick the matching CUDA PyTorch wheel index.

    Blackwell (sm_120) needs CUDA 12.8+ wheels; Ada / Ampere are happy on
    cu124. Mirrors the install-engine CUDA matrix.
    """
    if compute_cap == "12.0":
        return "https://download.pytorch.org/whl/cu128"
    return "https://download.pytorch.org/whl/cu124"


def _run(cmd: list[str], **kwargs) -> None:
    logger.info("$ %s", " ".join(cmd))
    subprocess.check_call(cmd, **kwargs)


def _checkout_pin(repo: Path, sha: str) -> None:
    """Fetch + checkout an exact commit SHA. Works for fresh clones and
    existing checkouts; needed because `git clone --depth 1` doesn't bring
    history that may include the pinned commit."""
    _run(["git", "fetch", "--quiet", "origin", sha], cwd=str(repo))
    _run(["git", "checkout", "--quiet", sha], cwd=str(repo))


def _pick_compute_cap() -> str | None:
    gpus = gpu_probe.enumerate_gpus()
    if not gpus:
        return None
    return gpus[0].compute_cap


def _create_venv() -> None:
    venv = comfyui_venv()
    if venv.exists():
        return
    venv.parent.mkdir(parents=True, exist_ok=True)
    uv = shutil.which("uv")
    if uv:
        # `--seed` installs pip into the venv. Without it, `uv venv` ships no pip, and when uv
        # resolves a *system* Python that lacks `ensurepip` (e.g. a distro's python3.11, or a 3.11
        # rc build), grid's pip bootstrap has nothing to fall back to and the install dies with
        # "No module named ensurepip". `--seed` makes uv install pip directly, independent of the
        # base interpreter, so bring-up works on any host uv can find a 3.11 on.
        _run([uv, "venv", "--seed", "-p", "3.11", str(venv)])
        return
    # No uv: fall back to the stdlib venv, but only if a real python3.11 is on PATH. A bare
    # `python3.11 -m venv` otherwise dies with an opaque FileNotFoundError — surface actionable
    # guidance instead (uv is the recommended toolchain and auto-provisions a 3.11).
    python311 = shutil.which("python3.11")
    if not python311:
        raise SystemExit(
            "Creating the ComfyUI environment needs `uv` (recommended) or Python 3.11, but neither "
            "is on PATH.\n"
            "  Install uv:  curl -LsSf https://astral.sh/uv/install.sh | sh    (then re-run)\n"
            "  or install Python 3.11 so `python3.11` is on PATH."
        )
    _run([python311, "-m", "venv", str(venv)])


def _pip_install(
    packages: list[str],
    *,
    index_url: str | None = None,
    extra_index_url: str | None = None,
    pre: bool = False,
) -> None:
    python = str(comfyui_python())
    cmd = [python, "-m", "pip", "install", "--upgrade"]
    if pre:
        cmd.append("--pre")
    if index_url:
        cmd.extend(["--index-url", index_url])
    if extra_index_url:
        cmd.extend(["--extra-index-url", extra_index_url])
    cmd.extend(packages)
    _run(cmd)


def _pip_install_requirements(
    requirements: Path,
    *,
    index_url: str | None = None,
    extra_index_url: str | None = None,
    pre: bool = False,
) -> None:
    python = str(comfyui_python())
    cmd = [python, "-m", "pip", "install", "--upgrade"]
    if pre:
        cmd.append("--pre")
    if index_url:
        cmd.extend(["--index-url", index_url])
    if extra_index_url:
        cmd.extend(["--extra-index-url", extra_index_url])
    cmd.extend(["-r", str(requirements)])
    _run(cmd)


def _ensure_pip() -> None:
    """Make sure pip is available inside the dedicated venv (uv venvs ship with it
    by default; bare `python -m venv` ones don't on some distros)."""
    python = str(comfyui_python())
    try:
        subprocess.check_call([python, "-m", "pip", "--version"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        _run([python, "-m", "ensurepip", "--upgrade", "--default-pip"])


def _installed_torch_version() -> str | None:
    try:
        result = subprocess.run(
            [str(comfyui_python()), "-c", "import torch; print(torch.__version__)"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _install_torch_stack() -> None:
    if _is_macos():
        expected_torch_version = TORCH_PINNED.split("==", 1)[1]
        installed = _installed_torch_version()
        if installed == expected_torch_version:
            print(f"PyTorch already pinned at {expected_torch_version}.")
            return
        print(f"Installing pinned PyTorch ({expected_torch_version}) from {TORCH_NIGHTLY_INDEX}")
        _pip_install(
            [TORCH_PINNED, TORCHVISION_PINNED, TORCHAUDIO_PINNED],
            extra_index_url=TORCH_NIGHTLY_INDEX,
            pre=True,
        )
        return

    compute_cap = _pick_compute_cap()
    index = _torch_index_for_compute_cap(compute_cap)
    print(f"Installing torch from {index} (detected compute_cap={compute_cap or 'unknown'})")
    _pip_install(["torch", "torchvision", "torchaudio"], index_url=index)


@contextmanager
def _package_resource_path(filename: str) -> Iterator[Path]:
    resource = resources.files(__package__).joinpath(filename)
    with resources.as_file(resource) as path:
        yield path


def _install_macos_media_package_lock() -> None:
    if not _is_macos():
        return
    print(f"Installing Desktop-matched media package lock from {MACOS_MEDIA_PACKAGE_LOCK}")
    with _package_resource_path(MACOS_MEDIA_PACKAGE_LOCK) as requirements:
        _pip_install_requirements(
            requirements,
            extra_index_url=TORCH_NIGHTLY_INDEX,
            pre=True,
        )


def install() -> None:
    """Clone ComfyUI + ComfyUI-GGUF, create venv, install torch + requirements.

    Idempotent: re-running on an existing install just upgrades pip-installed deps.
    """
    paths.ensure_all()
    services_root().mkdir(parents=True, exist_ok=True)
    output_dir().mkdir(parents=True, exist_ok=True)

    if shutil.which("git") is None:
        raise SystemExit("git is required to install ComfyUI. Install it via your distro's package manager.")

    if not comfyui_dir().exists():
        # Full clone (no --depth 1) so we can check out the pinned commit even
        # if it's not the tip.
        _run(["git", "clone", COMFYUI_REPO, str(comfyui_dir())])
    else:
        logger.info("ComfyUI checkout already present at %s", comfyui_dir())

    _checkout_pin(comfyui_dir(), COMFYUI_PINNED_COMMIT)

    gguf_dir = comfyui_dir() / "custom_nodes" / "ComfyUI-GGUF"
    if not gguf_dir.exists():
        gguf_dir.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", COMFYUI_GGUF_REPO, str(gguf_dir)])

    _checkout_pin(gguf_dir, COMFYUI_GGUF_PINNED_COMMIT)

    _create_venv()
    _ensure_pip()

    _install_torch_stack()

    reqs = comfyui_dir() / "requirements.txt"
    if reqs.exists():
        _run([str(comfyui_python()), "-m", "pip", "install", "-r", str(reqs)])

    if _is_macos():
        _install_macos_media_package_lock()
        print(f"ComfyUI installed at {comfyui_dir()} (venv {comfyui_venv()})")
        return

    # requirements.txt pulls comfyui_frontend_package + comfyui_workflow_templates
    # at unpinned versions; reinstall the pinned set on top so the lock sticks.
    print(f"Pinning ComfyUI frontend/template packages: {list(COMFYUI_REQUIREMENT_PINS)}")
    _pip_install(list(COMFYUI_REQUIREMENT_PINS))

    _pip_install([GGUF_PINNED_VERSION])
    print(f"ComfyUI installed at {comfyui_dir()} (venv {comfyui_venv()})")


def _is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("localhost", port)) == 0


@dataclass
class ComfyProcess:
    proc: subprocess.Popen
    port: int
    log: Path


_active: Optional[ComfyProcess] = None


_LOWVRAM_THRESHOLD_GB = 32.0


def _vram_flags() -> list[str]:
    """Pick ComfyUI memory-mode flags based on the largest available GPU.

    On cards below 32 GB we partition the UNet across CPU RAM (`--lowvram`)
    and keep 1 GB free for a co-resident llama-server. Larger cards get the
    default normalvram path. Returns [] when no GPU is detected (CPU mode
    will fail upstream anyway; we don't want to silently force --lowvram).
    """
    gpus = gpu_probe.enumerate_gpus()
    if not gpus:
        return []
    max_gb = max(g.memory_total_mb for g in gpus) / 1024.0
    if max_gb < _LOWVRAM_THRESHOLD_GB:
        return ["--lowvram", "--reserve-vram", "1"]
    return []


def start(port: int = COMFYUI_PORT_DEFAULT) -> ComfyProcess:
    """Spawn ComfyUI and wait for /system_stats to respond."""
    global _active
    if not comfyui_python().exists():
        raise SystemExit(
            f"ComfyUI not installed at {comfyui_dir()}. Run `grid engine install comfyui` first."
        )
    if _is_port_in_use(port):
        raise SystemExit(f"Port {port} already in use; cannot start ComfyUI.")

    paths.ensure_all()
    output_dir().mkdir(parents=True, exist_ok=True)
    log = paths.logs_dir() / f"comfyui_{port}.log"
    log_fh = log.open("a", buffering=1)
    log_fh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} grid starting comfyui on :{port} ===\n")
    cmd = [
        str(comfyui_python()),
        "main.py",
        "--output-directory", str(output_dir()),
        "--listen", "0.0.0.0",
        "--port", str(port),
        "--disable-smart-memory",
        "--cache-none",
        *_vram_flags(),
    ]
    env = os.environ.copy()
    # expandable_segments reduces CUDA allocator fragmentation, which matters
    # when a long-lived process loads / unloads multi-GB weight blocks.
    if _is_apple_silicon():
        env.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    else:
        env.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    proc = subprocess.Popen(cmd, cwd=str(comfyui_dir()), stdout=log_fh, stderr=log_fh, env=env)
    cp = ComfyProcess(proc=proc, port=port, log=log)
    _active = cp
    _write_pid_file(proc.pid, port)
    return cp


def wait_for_ready(port: int = COMFYUI_PORT_DEFAULT, timeout: float = 180.0) -> None:
    """Block until ComfyUI answers /system_stats with 200."""
    url = f"http://localhost:{port}/api/system_stats"
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(url, timeout=5.0)
            if resp.status_code == 200:
                return
        except httpx.RequestError as exc:
            last_exc = exc
        time.sleep(2.0)
    raise SystemExit(
        f"ComfyUI did not become ready on port {port} within {timeout}s "
        f"(last error: {last_exc})"
    )


def stop(*, timeout: float = 60.0) -> None:
    global _active
    if _active is not None and _active.proc.poll() is None:
        _active.proc.terminate()
        try:
            _active.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _active.proc.kill()
        _active = None
    _remove_pid_file()


def is_running(port: int = COMFYUI_PORT_DEFAULT) -> bool:
    try:
        resp = httpx.get(f"http://localhost:{port}/api/system_stats", timeout=2.0)
        return resp.status_code == 200
    except httpx.RequestError:
        return False


def ensure_running(comfyui_url: str = "http://localhost:8188/api") -> None:
    """Start ComfyUI if it isn't already; wait until it answers.

    Called by media_handler when a media request arrives but ComfyUI is down
    (e.g. crashed since provider start).
    """
    # Strip the trailing /api to detect the port reliably.
    port = _port_from_url(comfyui_url)
    if is_running(port):
        return
    start(port)
    wait_for_ready(port)


def _port_from_url(url: str) -> int:
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return parsed.port or COMFYUI_PORT_DEFAULT
    except Exception:
        return COMFYUI_PORT_DEFAULT


def _write_pid_file(pid: int, port: int) -> None:
    paths.run_dir().mkdir(parents=True, exist_ok=True)
    comfyui_pid_file().write_text(f"{pid}\n{port}\n")


def _remove_pid_file() -> None:
    try:
        comfyui_pid_file().unlink(missing_ok=True)
    except OSError:
        pass


def stop_running() -> int:
    """Stop a previously-started ComfyUI process referenced by the PID file."""
    pid_path = comfyui_pid_file()
    if not pid_path.exists():
        print("No ComfyUI process tracked.")
        return 0
    lines = pid_path.read_text().splitlines()
    if not lines:
        pid_path.unlink(missing_ok=True)
        return 0
    try:
        pid = int(lines[0])
    except ValueError:
        pid_path.unlink(missing_ok=True)
        return 1
    try:
        os.kill(pid, 15)
    except ProcessLookupError:
        pass
    pid_path.unlink(missing_ok=True)
    print(f"Sent SIGTERM to ComfyUI pid={pid}.")
    return 0
