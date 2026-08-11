"""NVIDIA GPU discovery using nvidia-smi."""

from __future__ import annotations

import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from shared.system import apple, arch


@dataclass
class GpuInfo:
    index: int
    name: str
    driver_version: str
    compute_cap: str
    memory_total_mb: float
    memory_used_mb: float
    utilization_pct: float
    # Thermals and power, for the grid page's per-node gauges. Optional and defaulted because they
    # are the ONLY fields here a card may legitimately not report: an integrated GPU (Tegra, GB10)
    # has no per-card power sensor and answers `[N/A]`, while every discrete card reports both.
    # Defaults keep every existing positional construction (and its tests) valid.
    temp_c: float | None = None
    power_w: float | None = None
    power_limit_w: float | None = None

    @property
    def compute_cap_sm(self) -> str:
        return "sm_" + self.compute_cap.replace(".", "")


def nvidia_smi_available() -> bool:
    return shutil.which("nvidia-smi") is not None


# The two `--query-gpu` field lists, richest first. The extra three are ancient fields, but
# `nvidia-smi` answers an unknown/unsupported field by exiting NON-ZERO for the whole invocation
# rather than blanking that column — so on a platform that rejects one of them, querying them
# together with VRAM would cost us VRAM too. The legacy list is the exact query this module shipped
# with, so falling back to it restores the previous behaviour byte for byte.
_QUERY_FULL = "index,name,driver_version,compute_cap,memory.total,memory.used,utilization.gpu,temperature.gpu,power.draw,power.limit"
_QUERY_LEGACY = "index,name,driver_version,compute_cap,memory.total,memory.used,utilization.gpu"

# Which query this box accepts, learned once. `None` = not yet determined. Memoized because the
# probe runs on the heartbeat thread every 30s and a box that rejects the full query would
# otherwise pay two process spawns forever.
_QUERY_FIELDS: str | None = None


def _optional_num(raw: str) -> float | None:
    """A `--query-gpu` column that a card may legitimately not report. ``[N/A]``, ``N/A``, an empty
    column, or anything unparseable all mean "this card has no such sensor" → ``None``.

    Deliberately separate from the strict `float()` calls on the memory/utilisation columns: those
    are what the node is FOR, and a card that cannot report them is not usable capacity, so it is
    right that they drop the whole row. A missing thermal sensor is not that."""
    try:
        return float(raw)
    except ValueError:
        return None


def _query_gpus(fields: str, timeout: float) -> list[GpuInfo] | None:
    """Run one `nvidia-smi` query, or ``None`` if the invocation itself failed (which is what a
    rejected field looks like — see `_QUERY_FULL`). An empty list means it ran and found no cards."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
            timeout=timeout,
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except (subprocess.SubprocessError, OSError):
        return None
    expected = fields.count(",") + 1
    gpus: list[GpuInfo] = []
    for line in out.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != expected:
            continue
        try:
            info = GpuInfo(
                index=int(parts[0]),
                name=parts[1],
                driver_version=parts[2],
                compute_cap=parts[3],
                memory_total_mb=float(parts[4]),
                memory_used_mb=float(parts[5]),
                utilization_pct=float(parts[6]),
            )
        except ValueError:
            continue
        if len(parts) > 7:
            info.temp_c = _optional_num(parts[7])
            info.power_w = _optional_num(parts[8])
            info.power_limit_w = _optional_num(parts[9])
        gpus.append(info)
    return gpus


def enumerate_gpus(timeout: float = 5.0) -> list[GpuInfo]:
    global _QUERY_FIELDS
    if not nvidia_smi_available():
        return []
    if _QUERY_FIELDS is not None:
        return _query_gpus(_QUERY_FIELDS, timeout) or []
    gpus = _query_gpus(_QUERY_FULL, timeout)
    if gpus is not None:
        _QUERY_FIELDS = _QUERY_FULL
        return gpus
    gpus = _query_gpus(_QUERY_LEGACY, timeout)
    if gpus is not None:
        _QUERY_FIELDS = _QUERY_LEGACY
        return gpus
    # Both failed — nvidia-smi is present but not answering (driver wedged, container without
    # device access). Learn nothing: the next tick retries the rich query, since this is a
    # transient condition rather than a statement about which fields the platform supports.
    return []


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


# Substrings that identify a thermal zone / hwmon device as belonging to the GPU rather than to the
# CPU clusters, the board, or a regulator. Matched case-insensitively against the kernel's own `type`
# / `name` string. Deliberately narrow: a wrong match reports a CPU's temperature as the GPU's, which
# is worse than reporting nothing at all.
_GPU_SENSOR_MARKERS = ("gpu", "gpu-therm", "gpu_thermal")


def _read_sysfs_number(path) -> float | None:
    """One numeric sysfs file, or None if it is absent/unreadable/not a number."""
    try:
        return float(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _sysfs_gpu_temp_c() -> float | None:
    """GPU die temperature (°C) from the kernel's thermal zones — the fallback for an integrated GPU
    whose `nvidia-smi` answers `[N/A]` (Tegra, GB10).

    Zones are matched by their `type` string, never by index: zone numbering is board-specific and
    the zone that is `GPU-therm` on one machine is a CPU cluster on the next. Values are in
    millidegrees. Returns the hottest matching zone, or None when nothing matches — never a guess."""
    if platform.system() != "Linux":
        return None
    best: float | None = None
    try:
        zones = sorted(Path("/sys/class/thermal").glob("thermal_zone*"))
    except OSError:
        return None
    for zone in zones:
        try:
            kind = (zone / "type").read_text().strip().lower()
        except OSError:
            continue
        if not any(marker in kind for marker in _GPU_SENSOR_MARKERS):
            continue
        milli = _read_sysfs_number(zone / "temp")
        if milli is None:
            continue
        celsius = milli / 1000.0
        if best is None or celsius > best:
            best = celsius
    return best


def _sysfs_gpu_power() -> tuple[float | None, float | None]:
    """(draw, cap) in watts from a GPU hwmon device, both None when no GPU hwmon is recognised.

    Same discipline as the thermal path: the hwmon is identified by its `name`, and an unrecognised
    one yields nothing rather than a plausible-looking wattage borrowed from a CPU rail. Kernel
    units are microwatts."""
    if platform.system() != "Linux":
        return None, None
    try:
        hwmons = sorted(Path("/sys/class/hwmon").glob("hwmon*"))
    except OSError:
        return None, None
    for hwmon in hwmons:
        try:
            name = (hwmon / "name").read_text().strip().lower()
        except OSError:
            continue
        if not any(marker in name for marker in _GPU_SENSOR_MARKERS):
            continue
        draw = _read_sysfs_number(hwmon / "power1_input")
        cap = _read_sysfs_number(hwmon / "power1_cap")
        if cap is None:
            cap = _read_sysfs_number(hwmon / "power1_max")
        if draw is None and cap is None:
            continue
        return (
            draw / 1_000_000.0 if draw is not None else None,
            cap / 1_000_000.0 if cap is not None else None,
        )
    return None, None


def _thermals(gpus: list[GpuInfo]) -> dict[str, float]:
    """The temperature/power keys for the heartbeat load, from `nvidia-smi` where the cards report
    them and from sysfs where they do not.

    Temperature is the hottest card and power is the sum across cards, matching how `load_snapshot`
    already aggregates utilisation and VRAM: one node, one number. A key is emitted ONLY when
    something actually measured it — absent means "not reported", and the grid page renders nothing
    rather than a zero (a 0°C card and a 0W card both read as a real, alarming measurement)."""
    out: dict[str, float] = {}
    temps = [g.temp_c for g in gpus if g.temp_c is not None]
    powers = [g.power_w for g in gpus if g.power_w is not None]
    limits = [g.power_limit_w for g in gpus if g.power_limit_w is not None]

    temp = max(temps) if temps else _sysfs_gpu_temp_c()
    if temp is not None:
        out["gpu_temp_c"] = round(temp, 1)

    if powers:
        out["gpu_power_w"] = round(sum(powers), 2)
    if limits:
        out["gpu_power_limit_w"] = round(sum(limits), 1)
    if not powers or not limits:
        sysfs_draw, sysfs_cap = _sysfs_gpu_power()
        if not powers and sysfs_draw is not None:
            out["gpu_power_w"] = round(sysfs_draw, 2)
        if not limits and sysfs_cap is not None:
            out["gpu_power_limit_w"] = round(sysfs_cap, 1)
    return out


def load_snapshot(timeout: float = 3.0) -> dict[str, float]:
    """Lightweight GPU totals for the provider heartbeat load payload. Keys: ``gpu_count``,
    ``memory_total_mb`` (advertised VRAM — what the grid aggregates per provider), ``memory_used_mb``,
    ``gpu_util`` (max across cards), and — where the hardware reports them — ``gpu_temp_c``,
    ``gpu_power_w``, ``gpu_power_limit_w``.

    NVIDIA first (summed/maxed across all cards). Failing that, on macOS advertise the GPU-usable memory
    — Apple Silicon unified memory (``hw.memsize``) or an Intel Mac's discrete VRAM (``system_profiler``)
    — so Mac providers still surface VRAM. Returns ``{}`` on a box with no detectable GPU.

    **Every key is emitted only when it was measured.** This branch used to send a hardcoded
    ``memory_used_mb: 0.0`` / ``gpu_util: 0.0`` on macOS — invented, not observed. Harmless while the
    grid page read only the total, but the moment anything renders a gauge they make every Mac look
    like a dead node holding 128 GB it never uses. A Mac now reports what it can genuinely measure
    (`_mac_telemetry`, which reads the IORegistry and needs no privileges) and omits the rest."""
    gpus = enumerate_gpus(timeout=timeout)
    if gpus:
        return {
            "gpu_count": float(len(gpus)),
            "memory_total_mb": sum(g.memory_total_mb for g in gpus),
            "memory_used_mb": sum(g.memory_used_mb for g in gpus),
            "gpu_util": max(g.utilization_pct for g in gpus),
            **_thermals(gpus),
        }
    mac_mb = _macos_vram_mb(timeout=timeout)
    if mac_mb:
        return {"gpu_count": 1.0, "memory_total_mb": mac_mb, **_mac_telemetry(mac_mb, timeout)}
    return {}


def _mac_telemetry(total_mb: float, timeout: float) -> dict[str, float]:
    """A Mac's live GPU counters, in the same key names the NVIDIA branch uses.

    Utilisation, temperature and power come from the IORegistry (`apple.accelerator_stats`) and need
    no privileges — see that function, which corrects the assumption that ``powermetrics`` and root
    are the only way to read them on macOS.

    **Memory occupancy is read from a different place per architecture, so that `used` and `total`
    always describe the same pool.** Getting this wrong is the failure mode worth spelling out: the
    two are divided and drawn as one bar, so a mismatched pair renders a confident percentage of
    nothing.

    - **Apple Silicon** — the advertised total is ``hw.memsize``, because the GPU shares one unified
      pool with the CPU (the premise `_macos_vram_mb` already rests on). The matching occupancy is
      therefore system memory in use. The IORegistry's ``inUseVidMemoryBytes`` is NOT it: on a
      unified-memory part it tracks a driver allocation, not the pool the total names.
    - **Intel Mac** — the advertised total is a discrete or integrated card's own VRAM from
      ``system_profiler``, a pool entirely separate from system RAM. Here ``inUseVidMemoryBytes`` is
      exactly right and system RAM would be measuring the wrong memory.

    Clamped to ``total_mb``: the pair comes from two independent sources, and a bar past its own
    track is a worse answer than a pinned one.
    """
    if platform.system() != "Darwin":
        return {}
    stats = apple.accelerator_stats(timeout=timeout)
    out: dict[str, float] = {}
    for key in ("gpu_util", "gpu_temp_c", "gpu_power_w"):
        if key in stats:
            out[key] = round(stats[key], 1)

    if arch.native_machine() == "arm64":
        from shared.system import host

        used_mb = host.memory_used_mb()
    else:
        used_bytes = stats.get("vram_used_bytes")
        used_mb = used_bytes / (1024 * 1024) if used_bytes else None
    if used_mb is not None:
        out["memory_used_mb"] = round(min(used_mb, total_mb), 1)
    return out

