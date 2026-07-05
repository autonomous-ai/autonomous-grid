"""Host-level system probes (CPU / RAM / disk).

Mirrors the shape of `additional_services_manager.py`'s `/system/info` so
operators can compare metrics directly across the Desktop App and the CLI.
"""

from __future__ import annotations

import os
import platform
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
        machine=platform.machine(),
        disk_total_gb=round(disk.total / (1024 ** 3), 2),
        disk_free_gb=round(disk.free / (1024 ** 3), 2),
    )


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
    usage = os.statvfs(home_dir)

    class Disk:
        total = usage.f_frsize * usage.f_blocks
        free = usage.f_frsize * usage.f_bavail

    return Disk()
