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
    (1, "base"): 68, (1, "pro"): 200, (1, "max"): 400, (1, "ultra"): 800,
    (2, "base"): 100, (2, "pro"): 200, (2, "max"): 400, (2, "ultra"): 800,
    (3, "base"): 100, (3, "pro"): 150, (3, "max"): 400, (3, "ultra"): 800,
    (4, "base"): 120, (4, "pro"): 273, (4, "max"): 546, (4, "ultra"): 1092,
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


def estimate(device_class: str, chip: str, gpus: list[dict]) -> float | None:
    """Best-effort memory bandwidth (GB/s) for the machine, or ``None`` when the part isn't recognised.

    Apple reads from the chip name (unified memory); NVIDIA from the fastest recognised card's VRAM;
    a plain CPU is left to the caller's DDR fallback (channels/speed aren't exposed to detect here).
    """
    if device_class == "apple-silicon":
        return _apple_bandwidth(chip)
    if device_class == "nvidia":
        rates = [r for g in (gpus or []) if (r := _nvidia_bandwidth(g.get("name") or "")) is not None]
        return max(rates) if rates else None
    return None
