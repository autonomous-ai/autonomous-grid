"""Host-level system probes (CPU / RAM / disk).

Mirrors the shape of `additional_services_manager.py`'s `/system/info` so
operators can compare metrics directly across the Desktop App and the CLI.
"""

from __future__ import annotations

import os
import platform
import subprocess
from dataclasses import dataclass


@dataclass
class HostInfo:
    home_directory: str
    memory_total_gb: float
    memory_available_gb: float
    memory_percent: float
    os_name: str
    os_version: str
    cpu_count: int
    physical_cores: int
    machine: str
    disk_total_gb: float
    disk_free_gb: float


def platform_kind() -> str:
    """Coarse OS/arch class advertised in the heartbeat so the grid knows what a node runs:
    ``linux`` · ``macos-arm64`` (Apple Silicon) · ``macos-x86_64`` (Intel Mac) · ``windows`` · ``other``.
    Same classification drives the VRAM path in ``gpu.load_snapshot`` (Apple Silicon vs Intel Mac)."""
    system = platform.system()
    if system == "Linux":
        return "linux"
    if system == "Darwin":
        return "macos-arm64" if platform.machine() == "arm64" else "macos-x86_64"
    if system == "Windows":
        return "windows"
    return "other"


def gather(home: str = "~") -> HostInfo:
    home_dir = os.path.expanduser(home)
    mem_total, mem_available, mem_percent = _memory_snapshot()
    disk = _disk_snapshot(home_dir)
    return HostInfo(
        home_directory=home_dir,
        memory_total_gb=round(mem_total / (1024 ** 3), 2),
        memory_available_gb=round(mem_available / (1024 ** 3), 2),
        memory_percent=mem_percent,
        os_name=platform.system(),
        os_version=platform.release(),
        cpu_count=os.cpu_count() or 1,
        physical_cores=physical_cores(),
        machine=platform.machine(),
        disk_total_gb=round(disk.total / (1024 ** 3), 2),
        disk_free_gb=round(disk.free / (1024 ** 3), 2),
    )


def _sysctl(name: str) -> str:
    """Read one sysctl scalar (macOS/BSD). "" on any failure — best-effort."""
    try:
        out = subprocess.check_output(["sysctl", "-n", name], timeout=3.0, stderr=subprocess.DEVNULL)
        return out.decode("utf-8", "replace").strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def physical_cores() -> int:
    """Physical (not logical) CPU cores. `psutil.cpu_count(logical=False)` when
    psutil is present; else `sysctl hw.physicalcpu` on macOS; else the logical
    count. Never exceeds the logical thread count. Best-effort, always >= 1."""
    logical = os.cpu_count() or 1
    n: int | None = None
    try:
        import psutil

        n = psutil.cpu_count(logical=False)
    except Exception:
        n = None
    if not n and platform.system() == "Darwin":
        raw = _sysctl("hw.physicalcpu")
        if raw:
            try:
                n = int(raw)
            except ValueError:
                n = None
    if not n:
        n = logical
    return max(1, min(int(n), logical))


def cpu_brand() -> str:
    """Human CPU brand string (e.g. "Intel(R) Core(TM) i9-9980HK CPU @ 2.40GHz").
    macOS via sysctl, Linux via /proc/cpuinfo, else `platform.processor()`.
    Best-effort — never raises."""
    system = platform.system()
    if system == "Darwin":
        brand = _sysctl("machdep.cpu.brand_string")
        if brand:
            return brand
    if system == "Linux":
        try:
            with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.lower().startswith("model name") and ":" in line:
                        return line.split(":", 1)[1].strip()
        except OSError:
            pass
    return platform.processor() or platform.machine() or "unknown"


def _memory_snapshot() -> tuple[int, int, float]:
    try:
        import psutil

        mem = psutil.virtual_memory()
        return int(mem.total), int(mem.available), float(mem.percent)
    except Exception:
        pass
    if hasattr(os, "sysconf"):
        try:
            page_size = int(os.sysconf("SC_PAGE_SIZE"))
            pages = int(os.sysconf("SC_PHYS_PAGES"))
            total = page_size * pages
            return total, total, 0.0
        except (OSError, ValueError):
            pass
    return 0, 0, 0.0


def _disk_snapshot(home_dir: str):
    try:
        import psutil

        return psutil.disk_usage(home_dir)
    except Exception:
        pass
    # `os.statvfs` is Unix-only; guard it so a Windows box without psutil degrades to
    # zeros instead of raising AttributeError (which would crash `host.gather()`).
    if hasattr(os, "statvfs"):
        usage = os.statvfs(home_dir)

        class Disk:
            total = usage.f_frsize * usage.f_blocks
            free = usage.f_frsize * usage.f_bavail

        return Disk()

    class Disk:
        total = 0
        free = 0

    return Disk()
