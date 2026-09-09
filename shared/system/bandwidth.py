"""Estimate a machine's memory bandwidth (GB/s) — the real bottleneck for token generation.

Decode is memory-bound: tokens/sec tracks bandwidth ÷ bytes-read-per-token, and bandwidth spans an
order of magnitude *within* a single backend. A flat per-backend number would tell an M-series base
chip (~100 GB/s) and an M-Ultra (~800 GB/s) the same story, and they are nothing alike — so anything
sizing a model to this machine picks wrong on both. We read it off the chip / GPU name here, where
the hardware is known, so consumers of the inventory get a number instead of a guess.

Published figures, rounded; unknown parts return ``None`` so the consumer falls back to a coarse
per-backend default rather than a wrong specific number.
"""
from __future__ import annotations

import re

# Apple unified-memory bandwidth by (generation, tier). Tiers scale ~2× each step; generations drift.
_APPLE_GBPS = {
    (1, "base"): 67, (1, "pro"): 200, (1, "max"): 400, (1, "ultra"): 800,
    (2, "base"): 100, (2, "pro"): 200, (2, "max"): 400, (2, "ultra"): 800,
    (3, "base"): 100, (3, "pro"): 150, (3, "max"): 400, (3, "ultra"): 800,
    (4, "base"): 120, (4, "pro"): 273, (4, "max"): 546, (4, "ultra"): 1092,
    (5, "base"): 153, (5, "pro"): 307, (5, "max"): 614, (5, "ultra"): 1228,
}
_APPLE_TIER_DEFAULT = {"base": 100, "pro": 200, "max": 400, "ultra": 800}

# NVIDIA VRAM bandwidth by name fragment (longest/most-specific match wins). Datacenter + desktop.
_NVIDIA_GBPS = (
    ("h200", 4800), ("h100", 3350), ("a100", 2039), ("v100", 900),
    ("a6000", 768), ("a40", 696), ("a10", 600), ("l40", 864), ("l4", 300), ("t4", 320),
    ("5090", 1792), ("4090", 1008), ("4080", 717), ("4070 ti", 672), ("4070", 504),
    ("4060", 272), ("3090 ti", 1008), ("3090", 936), ("3080", 760), ("3070", 448),
    ("3060", 360), ("2080 ti", 616), ("2080", 448), ("2070", 448), ("2060", 336),
)

# Intel MacBook memory bandwidth by CPU model fragment. LPDDR/DDR is soldered on every MacBook so
# each model has exactly one config; these are published peak rates rounded down to the nearest GB/s,
# longest/most-specific match wins (same convention as _NVIDIA_GBPS).
_INTEL_MACBOOK_GBPS = (
    # 10th-gen Ice Lake / Comet Lake — LPDDR4X-3733 dual-channel (~60 GB/s peak)
    ("i7-1068ng7", 60), ("i5-1038ng7", 60), ("i7-1060ng7", 60), ("i5-1030ng7", 60),
    # 11th-gen Tiger Lake — LPDDR4X-4266 (~68 GB/s peak)
    ("i7-1185g7", 68), ("i5-1145g7", 68), ("i7-1165g7", 68),
    # 9th-gen Coffee Lake — DDR4-2666 dual-channel (MacBook Pro 16")
    ("i9-9980hk", 42), ("i9-9880h", 42), ("i7-9750h", 42), ("i9-9880hk", 42),
    # 8th-gen Coffee Lake — DDR4-2400/2666 (MacBook Pro 15" 2018)
    ("i9-8950hk", 38), ("i7-8850h", 38), ("i7-8750h", 38),
    # 7th-gen Kaby Lake — LPDDR3-2133 (MacBook Pro 13" 2017)
    ("i7-7567u", 34), ("i7-7660u", 34), ("i5-7360u", 34), ("i5-7267u", 34),
)

# GFLOPS per Apple GPU core, dense FP16/BF16 matmul (FP32 × 2). Per-generation because each
# generation widens SIMD lanes / adds matrix units; a flat per-core constant under-counts M3+ badly.
# Source: published FP32 TFLOPS ÷ core count, doubled for FP16 (Apple GPUs do matmul in FP16/BF16).
_APPLE_GFLOPS_PER_CORE = {"m1": 650.0, "m2": 714.0, "m3": 710.0, "m4": 852.0, "m5": 1050.0}
# CPU matmul with no dedicated tensor/AVX-512 path is well below a GPU's per-unit throughput but
# modern AVX2/FMA cores still move real work — calibrated so a small (~3B active) dense model's
# prefill still clears a normal interactive bar, while a much larger active model does not.
_CPU_GFLOPS_PER_CORE = 190.0

# NVIDIA dense FP16/BF16 Tensor-core TFLOPS by name fragment (no sparsity — sparsity needs a
# structured-pruned model, which nothing in this catalog assumes). Published spec sheets, rounded;
# reuses the exact fragment set as `_NVIDIA_GBPS` above.
_NVIDIA_TFLOPS = (
    ("h200", 990), ("h100", 990), ("a100", 310), ("v100", 125),
    ("a6000", 155), ("a40", 150), ("a10", 125), ("l40", 180), ("l4", 120), ("t4", 65),
    ("5090", 420), ("4090", 165), ("4080", 97), ("4070 ti", 80), ("4070", 60),
    ("4060", 30), ("3090 ti", 80), ("3090", 71), ("3080", 60), ("3070", 40),
    ("3060", 25), ("2080 ti", 28), ("2080", 20), ("2070", 15), ("2060", 13),
)


def _apple_compute_gflops(core_count: int | None, chip: str = "") -> float | None:
    """Apple GPU compute (GFLOPS) from its core count and generation — the same fields
    ``device_info`` already collects (``apple.gpu_core_count()``, ``apple.describe_chip()``).
    Per-generation because each gen widens SIMD lanes; a flat per-core constant under-counts M3+."""
    if not core_count or core_count <= 0:
        return None
    gen = ""
    if chip:
        m = re.search(r"\bm(\d+)\b", chip.lower())
        if m:
            gen = f"m{m.group(1)}"
    per_core = _APPLE_GFLOPS_PER_CORE.get(gen, 700.0)  # M5+ until measured
    return core_count * per_core


def _nvidia_compute_gflops(name: str) -> float | None:
    if not name:
        return None
    text = name.lower()
    best = None
    for fragment, tflops in _NVIDIA_TFLOPS:
        if fragment in text and (best is None or len(fragment) > best[0]):
            best = (len(fragment), tflops)
    return float(best[1]) * 1000.0 if best else None


def _cpu_compute_gflops(physical_cores: int | None) -> float | None:
    if not physical_cores or physical_cores <= 0:
        return None
    return physical_cores * _CPU_GFLOPS_PER_CORE


def _apple_bandwidth(chip: str) -> float | None:
    """Bandwidth for an Apple chip string like ``"Apple M3 Max"`` / ``"M1 Ultra"`` / ``"M2"``."""
    if not chip:
        return None
    text = chip.lower()
    m = re.search(r"\bm(\d+)\b", text)
    if not m:
        return None
    gen = int(m.group(1))
    tier = next((t for t in ("ultra", "max", "pro") if t in text), "base")
    return _APPLE_GBPS.get((gen, tier)) or _APPLE_TIER_DEFAULT[tier]


def _nvidia_bandwidth(name: str) -> float | None:
    """Bandwidth for an NVIDIA GPU name; the most specific fragment wins (``4070 ti`` before ``4070``)."""
    if not name:
        return None
    text = name.lower()
    best = None
    for fragment, gbps in _NVIDIA_GBPS:
        if fragment in text and (best is None or len(fragment) > best[0]):
            best = (len(fragment), gbps)
    return float(best[1]) if best else None


def _cpu_bandwidth(brand: str) -> float | None:
    """Bandwidth for an Intel MacBook CPU by model fragment. RAM is soldered on every MacBook so each
    model has exactly one config; non-Mac Intel machines (NUCs, hackintoshes, generic PCs) don't match
    any fragment and fall back to the caller's per-backend default."""
    if not brand:
        return None
    text = brand.lower()
    best = None
    for fragment, gbps in _INTEL_MACBOOK_GBPS:
        if fragment in text and (best is None or len(fragment) > best[0]):
            best = (len(fragment), gbps)
    return float(best[1]) if best else None


def estimate(device_class: str, chip: str, gpus: list[dict]) -> float | None:
    """Best-effort memory bandwidth (GB/s) for the machine, or ``None`` when the part isn't recognised.

    Apple reads from the chip name (unified memory); NVIDIA from the fastest recognised card's VRAM;
    Intel MacBooks from the CPU model fragment (LPDDR/DDR is soldered so each model has one config);
    anything else (AMD CPUs, generic PCs, unknown parts) falls back to the caller's per-backend default.
    """
    if device_class == "apple-silicon":
        return _apple_bandwidth(chip)
    if device_class == "nvidia":
        rates = [r for g in (gpus or []) if (r := _nvidia_bandwidth(g.get("name") or "")) is not None]
        return max(rates) if rates else None
    if device_class == "cpu":
        return _cpu_bandwidth(chip)
    return None


def estimate_compute_gflops(device_class: str, gpus: list[dict], physical_cores: int | None,
                            chip: str = "") -> float | None:
    """Best-effort compute throughput (GFLOPS) — the second bottleneck alongside `estimate()`'s
    memory bandwidth. Decode is memory-bound (see `estimate`); prefill (prompt processing) is
    compute-bound, so a consumer sizing prefill time needs this instead. ``chip`` (e.g. ``"M2 Pro"``)
    lets the Apple path pick a per-generation per-core figure; ignored on other backends."""
    if device_class == "apple-silicon":
        core_count = (gpus or [{}])[0].get("core_count") if gpus else None
        return _apple_compute_gflops(core_count, chip)
    if device_class == "nvidia":
        rates = [r for g in (gpus or []) if (r := _nvidia_compute_gflops(g.get("name") or "")) is not None]
        return max(rates) if rates else None
    if device_class == "cpu":
        return _cpu_compute_gflops(physical_cores)
    return None
