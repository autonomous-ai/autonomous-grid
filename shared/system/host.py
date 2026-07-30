"""Host-level system probes (CPU / RAM / disk).

Mirrors the shape of `additional_services_manager.py`'s `/system/info` so
operators can compare metrics directly across the Desktop App and the CLI.
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class HostInfo:
    home_directory: str
    memory_total_gb: float
    memory_available_gb: float | None
    memory_percent: float | None
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
        memory_available_gb=(
            round(mem_available / (1024 ** 3), 2) if mem_available is not None else None
        ),
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
    except (ImportError, AttributeError, OSError, RuntimeError, TypeError, ValueError):
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


def _memory_snapshot() -> tuple[int, int | None, float | None]:
    try:
        import psutil

        mem = psutil.virtual_memory()
        return int(mem.total), int(mem.available), float(mem.percent)
    except (ImportError, AttributeError, OSError, RuntimeError, TypeError, ValueError):
        mem = None
    system = platform.system()
    if system == "Linux":
        snapshot = _linux_memory_snapshot()
        if snapshot is not None:
            return snapshot
    elif system == "Darwin":
        snapshot = _macos_memory_snapshot()
        if snapshot is not None:
            return snapshot
    elif system == "Windows":
        snapshot = _windows_memory_snapshot()
        if snapshot is not None:
            return snapshot

    # A total-only sysconf result is useful for inventory/model sizing, but it says nothing about
    # current pressure. Report that uncertainty explicitly instead of the dangerous old 0%-used
    # value, which made an unobservable machine look perfectly idle to the local allocator.
    if hasattr(os, "sysconf"):
        try:
            page_size = int(os.sysconf("SC_PAGE_SIZE"))
            pages = int(os.sysconf("SC_PHYS_PAGES"))
            total = page_size * pages
            return total, None, None
        except (OSError, TypeError, ValueError):
            pass
    return 0, None, None


def _linux_memory_snapshot() -> tuple[int, int, float] | None:
    try:
        text = Path("/proc/meminfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    values: dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, raw = line.split(":", 1)
        match = re.search(r"\d+", raw)
        if match:
            # Linux meminfo is specified in KiB for these fields.
            values[name] = int(match.group(0)) * 1024
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable")
    if available is None:
        # Kernel 3.14 and older have no MemAvailable. This is the conventional conservative
        # approximation, with shared memory removed because it is counted in Cached.
        available = (
            values.get("MemFree", 0)
            + values.get("Buffers", 0)
            + values.get("Cached", 0)
            + values.get("SReclaimable", 0)
            - values.get("Shmem", 0)
        )
    if total <= 0:
        return None
    available = max(0, min(int(available), total))
    return total, available, _used_percent(total, available)


def _macos_memory_snapshot() -> tuple[int, int, float] | None:
    raw_total = _sysctl("hw.memsize")
    try:
        total = int(raw_total)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    try:
        raw_stats = subprocess.check_output(
            ["vm_stat"], timeout=3.0, stderr=subprocess.DEVNULL
        ).decode("utf-8", "replace")
    except (subprocess.SubprocessError, OSError):
        return total, None, None
    page_match = re.search(r"page size of\s+(\d+)\s+bytes", raw_stats, re.IGNORECASE)
    if not page_match:
        return total, None, None
    page_size = int(page_match.group(1))
    pages: dict[str, int] = {}
    for line in raw_stats.splitlines():
        match = re.match(r"Pages\s+([^:]+):\s*([\d.]+)", line.strip(), re.IGNORECASE)
        if match:
            pages[match.group(1).strip().lower()] = int(match.group(2).rstrip("."))
    available_pages = sum(
        pages.get(name, 0) for name in ("free", "inactive", "speculative")
    )
    available = max(0, min(available_pages * page_size, total))
    return total, available, _used_percent(total, available)


def _windows_memory_snapshot() -> tuple[int, int, float] | None:
    try:
        import ctypes
        from ctypes import wintypes

        class MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatusEx()
        status.dwLength = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        total = int(status.ullTotalPhys)
        available = int(status.ullAvailPhys)
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    if total <= 0:
        return None
    available = max(0, min(available, total))
    return total, available, _used_percent(total, available)


def _used_percent(total: int, available: int) -> float:
    return round((total - available) * 100.0 / total, 1)


# `vm_stat` prints the page size in its own header, and it must be READ rather than assumed: Apple
# Silicon pages are 16384 bytes where Intel's are 4096. Hardcoding 4096 would under-report every
# Apple Silicon node by a factor of four — and to a plausible-looking figure, which is worse than an
# obviously broken one because nobody goes looking.
_VM_STAT_PAGE = re.compile(r"page size of (\d+) bytes")

# The page classes macOS can hand back on demand. This is psutil's own definition of ``available``,
# matched deliberately: the same machine must report the same occupancy whether or not psutil
# happens to be installed, or the dashboard's memory bar would shift the day somebody pip-installs
# it and the change would look like real movement. Verified against `psutil.virtual_memory()` on a
# live Mac — the two agree to within the drift of reading them a moment apart.
_VM_STAT_AVAILABLE = ("Pages free", "Pages speculative", "Pages inactive")


def _vm_stat_used_bytes() -> int | None:
    """System memory in use, from ``vm_stat``, or None when it cannot be read. macOS only.

    Exists so an Apple Silicon node can draw its memory bar on a stock install. psutil is an
    OPTIONAL dependency here — ``pyproject.toml`` never declares it and nothing in the CLI's
    dependency tree pulls it in — so trusting psutil alone left this reading absent on any Mac that
    happened not to have it, which on unified-memory hardware is the VRAM figure itself.

    ``vm_stat`` ships with every macOS and needs no privileges. All three page counts must be
    present: a missing one would read as zero and silently inflate "used", and a format change is
    better answered with "unknown" than with a confident wrong number.
    """
    if platform.system() != "Darwin":
        return None
    try:
        out = subprocess.check_output(["vm_stat"], timeout=3.0, stderr=subprocess.DEVNULL)
        raw = out.decode("utf-8", "replace")
    except (subprocess.SubprocessError, OSError):
        return None
    header = _VM_STAT_PAGE.search(raw)
    if not header:
        return None
    page = int(header.group(1))

    pages: dict[str, int] = {}
    for line in raw.splitlines():
        name, sep, value = line.partition(":")
        if not sep:
            continue
        try:  # counts are printed with a trailing period ("Pages free:  177973.")
            pages[name.strip()] = int(value.strip().rstrip("."))
        except ValueError:
            continue
    if not all(key in pages for key in _VM_STAT_AVAILABLE):
        return None

    try:
        total = int(_sysctl("hw.memsize"))
    except ValueError:
        return None
    used = total - sum(pages[key] for key in _VM_STAT_AVAILABLE) * page
    # A total and a page census read a few milliseconds apart can disagree; anything outside the
    # pool is that disagreement, not a measurement.
    return used if 0 < used <= total else None


def memory_used_mb() -> float | None:
    """System memory in use, in MB — or None when this box cannot truly measure it.

    psutil when present, else ``vm_stat`` on macOS (`_vm_stat_used_bytes`), else nothing. What is
    never used is `_memory_snapshot`'s ``sysconf`` fallback: it reports available == total because
    it has no way to ask, and a caller subtracting those would publish a confident "0 MB in use"
    for a machine it never measured. None, not 0 — the distinction is the whole point.

    Used by the Apple Silicon VRAM path, where the GPU shares this pool — see `gpu.load_snapshot`.
    """
    used: int | None
    try:
        import psutil

        mem = psutil.virtual_memory()
        used = int(mem.total) - int(mem.available)
    except Exception:  # noqa: BLE001 — a probe, never a failure path
        used = _vm_stat_used_bytes()
    if used is None or used <= 0:
        return None
    return used / (1024 * 1024)


def disk_gb(path: str) -> tuple[float, float] | None:
    """``(total_gb, used_gb)`` for the volume containing ``path``, or None when it cannot be read.

    Goes through `_disk_snapshot`, so it inherits the psutil→``statvfs``→zeros ladder the rest of
    this module uses — psutil is an OPTIONAL dependency here and a probe that imported it directly
    would silently report nothing on every box that does without it.

    ``used`` is ``total - free``, which counts the filesystem's reserved blocks as used and so runs
    slightly above what ``df`` shows. The alternative (psutil's own ``used``) does not exist on the
    ``statvfs`` path, and one definition that works everywhere beats two that disagree by platform.

    None rather than zeros on failure: a caller reporting telemetry must be able to say "unknown",
    and a zeroed pair would render as an empty disk.
    """
    try:
        usage = _disk_snapshot(path)
        total, free = float(usage.total), float(usage.free)
    except (OSError, AttributeError, TypeError, ValueError):
        return None
    if total <= 0:
        return None
    gib = 1024 ** 3
    return round(total / gib, 1), round((total - free) / gib, 1)


def _disk_snapshot(home_dir: str):
    try:
        import psutil

        return psutil.disk_usage(home_dir)
    except (ImportError, AttributeError, OSError, RuntimeError, TypeError, ValueError):
        usage = None
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
