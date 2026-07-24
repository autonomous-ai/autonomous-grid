"""Apple Silicon hardware probes: chip/model names and GPU core count.

macOS does not expose the marketing chip name ("Apple M3 Max") or the machine
model ("MacBook Pro (Mac15,9)") through ``platform`` — it only reports "arm64".
``system_profiler`` carries both, and the Apple GPU core count appears only in
``SPDisplaysDataType`` (no other API reports it).

Every probe is best-effort and never raises — a missing fact is "" or ``None``
and the caller degrades gracefully.
"""

from __future__ import annotations

import json
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
