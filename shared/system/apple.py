"""Mac hardware probes: chip/model names, GPU core count, and live GPU counters.

macOS does not expose the marketing chip name ("Apple M3 Max") or the machine
model ("MacBook Pro (Mac15,9)") through ``platform`` — it only reports "arm64".
``system_profiler`` carries both, and the Apple GPU core count appears only in
``SPDisplaysDataType`` (no other API reports it).

Live GPU counters come from the IORegistry (`accelerator_stats`), which — unlike
the ``powermetrics`` this module's author first assumed — needs **no elevated
privileges**. See that function for what is and is not actually gated.

Every probe is best-effort and never raises — a missing fact is "" or ``None``
and the caller degrades gracefully.
"""

from __future__ import annotations

import json
import re
import subprocess


def _run(cmd: list[str], timeout: float = 10.0) -> str:
    try:
        out = subprocess.check_output(cmd, timeout=timeout, stderr=subprocess.DEVNULL)
        return out.decode("utf-8", errors="replace").strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def describe_chip() -> tuple[str, str]:
    """Best-effort ``(model, chip)`` on Apple Silicon — e.g.
    ``("MacBook Pro (Mac15,9)", "Apple M3 Max")``. Reads
    ``system_profiler SPHardwareDataType`` (``machine_name`` / ``chip_type``),
    falling back to ``sysctl machdep.cpu.brand_string`` for the chip. The full
    "Apple " prefix is kept — it's the human-readable brand name callers display.
    Returns ``("", "")`` off Apple or on failure."""
    model = ""
    chip = ""
    raw = _run(["system_profiler", "SPHardwareDataType", "-json"], timeout=10.0)
    if raw:
        try:
            items = json.loads(raw).get("SPHardwareDataType") or []
            if items:
                info = items[0]
                model = info.get("machine_name") or info.get("model_name") or ""
                chip = info.get("chip_type") or info.get("cpu_type") or ""
        except (json.JSONDecodeError, AttributeError, IndexError):
            pass
    if not chip:
        chip = _run(["sysctl", "-n", "machdep.cpu.brand_string"])  # "Apple M3 Max"
    return model, chip


def gpu_core_count() -> int | None:
    """Apple GPU total core count from ``system_profiler SPDisplaysDataType``
    ("Total Number of Cores: 40"), or ``None`` when the field is absent (e.g. a
    non-Apple GPU, or an older OS). Best-effort — never raises."""
    raw = _run(["system_profiler", "SPDisplaysDataType"], timeout=10.0)
    if not raw:
        return None
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("total number of cores") and ":" in stripped:
            value = stripped.split(":", 1)[1].strip()
            try:
                return int(value.split()[0])
            except (ValueError, IndexError):
                return None
    return None


# One `"PerformanceStatistics" = { ... }` block from `ioreg`, and the `"key"=<int>` pairs inside it.
# The dictionary is printed on a single line per accelerator, so a non-greedy match to the first
# closing brace takes exactly one device's block.
_PERF_BLOCK = re.compile(r'"PerformanceStatistics"\s*=\s*\{(.*?)\}')
_PERF_PAIR = re.compile(r'"([^"]+)"\s*=\s*(-?\d+)')

# What each counter is called in the IORegistry. Several spellings per reading because the key set is
# the GPU driver's, not macOS's: an AMD card names its load `Device Utilization %`, and some drivers
# only publish `GPU Activity(%)`. First key present wins, so a driver publishing neither simply
# reports nothing for that reading rather than a zero it never measured.
_STAT_KEYS: dict[str, tuple[str, ...]] = {
    "vram_used_bytes": ("inUseVidMemoryBytes",),
    "gpu_util": ("Device Utilization %", "GPU Activity(%)"),
    "gpu_temp_c": ("Temperature(C)", "GPU Core Temperature(C)"),
    "gpu_power_w": ("Total Power(W)",),
}


def accelerator_stats(timeout: float = 5.0) -> dict[str, float]:
    """Live GPU counters from the IORegistry, as whichever of ``vram_used_bytes``, ``gpu_util``,
    ``gpu_temp_c`` and ``gpu_power_w`` this machine actually publishes. ``{}`` off macOS or when
    nothing is readable.

    **No privileges required, and that is the correction this function exists to make.** The obvious
    macOS answer for GPU telemetry is ``powermetrics``, which does need root — so it is easy to
    conclude that a Mac provider simply cannot report load or temperature without it. That is wrong:
    ``IOAccelerator``'s ``PerformanceStatistics`` dictionary is world-readable, and on an Intel Mac
    with a discrete card it carries VRAM occupancy, utilisation, die temperature and power draw. It
    was measured on a real box before this was written, not assumed.

    **Values are maxed across accelerators, never summed.** ``ioreg`` lists the same physical GPU
    more than once (the accelerator and its subclasses each carry a copy of the statistics), so
    summing power would report a 22W card as drawing 44W and summing VRAM would double its
    occupancy. Max is duplicate-safe, and on a Mac pairing an integrated GPU with a discrete one it
    also picks the discrete card — the one doing the work.

    ``vramFreeBytes`` is deliberately ignored despite sitting right beside the occupancy figure: it
    counts free bytes within the currently committed pool, not against the card's advertised size
    (on the measured box, used + free came to ~950 MB of a 4 GB card). Pairing it with the
    ``system_profiler`` total would state a headroom nothing measured.
    """
    raw = _run(["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"], timeout=timeout)
    if not raw:
        return {}
    best: dict[str, float] = {}
    for block in _PERF_BLOCK.finditer(raw):
        stats = dict(_PERF_PAIR.findall(block.group(1)))
        for name, candidates in _STAT_KEYS.items():
            for key in candidates:
                if key not in stats:
                    continue
                try:
                    value = float(stats[key])
                except ValueError:
                    break
                # A zero here is ambiguous — an idle GPU and a device that publishes the key without
                # populating it look identical — so it never displaces a positive reading from
                # another accelerator, and only stands if nothing better is found.
                if name not in best or value > best[name]:
                    best[name] = value
                break
    return best
