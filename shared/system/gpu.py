"""NVIDIA GPU discovery using nvidia-smi."""

from __future__ import annotations

import platform
import shutil
import subprocess
from dataclasses import dataclass

from shared.system import arch


@dataclass
class GpuInfo:
    index: int
    name: str
    driver_version: str
    compute_cap: str
    memory_total_mb: float
    memory_used_mb: float
    utilization_pct: float

    @property
    def compute_cap_sm(self) -> str:
        return "sm_" + self.compute_cap.replace(".", "")


def nvidia_smi_available() -> bool:
    return shutil.which("nvidia-smi") is not None


def enumerate_gpus(timeout: float = 5.0) -> list[GpuInfo]:
    if not nvidia_smi_available():
        return []
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=index,name,driver_version,compute_cap,memory.total,memory.used,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            timeout=timeout,
        ).decode().strip()
    except (subprocess.SubprocessError, OSError):
        return []
    gpus: list[GpuInfo] = []
    for line in out.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 7:
            continue
        try:
            gpus.append(
                GpuInfo(
                    index=int(parts[0]),
                    name=parts[1],
                    driver_version=parts[2],
                    compute_cap=parts[3],
                    memory_total_mb=float(parts[4]),
                    memory_used_mb=float(parts[5]),
                    utilization_pct=float(parts[6]),
                )
            )
        except ValueError:
            continue
    return gpus


_MAC_VRAM_MB: float | None = None  # memoized — VRAM total is static per host, and system_profiler is slow


def _sysctl_memsize_mb() -> float:
    """Total unified memory (MB) via ``sysctl hw.memsize`` — on Apple Silicon the GPU shares this pool,
    so it IS the advertised VRAM."""
    try:
        out = subprocess.check_output(["sysctl", "-n", "hw.memsize"], timeout=2.0).decode().strip()
        return int(out) / (1024 * 1024)
    except (subprocess.SubprocessError, OSError, ValueError):
        return 0.0


def _parse_size_to_mb(text: str) -> float:
    """``"4 GB"`` / ``"1536 MB"`` → MB. A unitless number is assumed MB; unparseable → 0."""
    parts = text.split()
    if not parts:
        return 0.0
    try:
        num = float(parts[0])
    except ValueError:
        return 0.0
    unit = parts[1].upper() if len(parts) > 1 else "MB"
    if unit.startswith("TB"):
        return num * 1024 * 1024
    if unit.startswith("GB"):
        return num * 1024
    if unit.startswith("KB"):
        return num / 1024
    return num  # MB / unitless


def _macos_profiler_vram_mb(timeout: float = 5.0) -> float:
    """Largest VRAM (MB) from ``system_profiler SPDisplaysDataType`` — Intel Macs report
    ``VRAM (Total): N GB`` (discrete) or ``VRAM (Dynamic, Max): N MB`` (integrated). Picks the biggest GPU."""
    try:
        out = subprocess.check_output(["system_profiler", "SPDisplaysDataType"], timeout=timeout).decode()
    except (subprocess.SubprocessError, OSError):
        return 0.0
    best = 0.0
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("VRAM") and ":" in stripped:
            best = max(best, _parse_size_to_mb(stripped.split(":", 1)[1].strip()))
    return best


def _macos_vram_mb(timeout: float = 5.0) -> float:
    """VRAM (MB) for a Mac provider, memoized: Apple Silicon → unified memory (``hw.memsize``); Intel Mac
    → discrete/integrated VRAM from ``system_profiler``. 0 when not macOS."""
    global _MAC_VRAM_MB
    if _MAC_VRAM_MB is not None:
        return _MAC_VRAM_MB
    if platform.system() != "Darwin":
        _MAC_VRAM_MB = 0.0
    elif arch.native_machine() == "arm64":
        # `native_machine`, not `platform.machine()`: an x86_64 (Rosetta) Python on Apple
        # Silicon reports "x86_64" and would wrongly take the Intel-Mac path, reading a few
        # GB of integrated VRAM instead of the full unified-memory pool.
        _MAC_VRAM_MB = _sysctl_memsize_mb()
    else:
        _MAC_VRAM_MB = _macos_profiler_vram_mb(timeout=timeout)
    return _MAC_VRAM_MB


def load_snapshot(timeout: float = 3.0) -> dict[str, float]:
    """Lightweight GPU totals for the provider heartbeat load payload. Keys: ``gpu_count``,
    ``memory_total_mb`` (advertised VRAM — what the grid aggregates per provider), ``memory_used_mb``,
    ``gpu_util`` (max across cards).

    NVIDIA first (summed/maxed across all cards). Failing that, on macOS advertise the GPU-usable memory
    — Apple Silicon unified memory (``hw.memsize``) or an Intel Mac's discrete VRAM (``system_profiler``)
    — so Mac providers still surface VRAM. Returns ``{}`` on a box with no detectable GPU."""
    gpus = enumerate_gpus(timeout=timeout)
    if gpus:
        return {
            "gpu_count": float(len(gpus)),
            "memory_total_mb": sum(g.memory_total_mb for g in gpus),
            "memory_used_mb": sum(g.memory_used_mb for g in gpus),
            "gpu_util": max(g.utilization_pct for g in gpus),
        }
    mac_mb = _macos_vram_mb(timeout=timeout)
    if mac_mb:
        return {"gpu_count": 1.0, "memory_total_mb": mac_mb, "memory_used_mb": 0.0, "gpu_util": 0.0}
    return {}

