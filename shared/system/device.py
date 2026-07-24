"""Resolve this machine's memory budget for fitting a local model.

NVIDIA VRAM when a card is usable, Apple Silicon unified memory, else system RAM —
using available memory rather than total.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass

from shared.system import arch, gpu, host

GIB = 1024 ** 3


@dataclass(frozen=True)
class Budget:
    total_bytes: int
    source: str          # "vram" | "ram"
    detected: str        # human-readable "Detected: …" line
    backend: str = "cpu"  # "cuda" | "metal" | "cpu" — decides i-quant vs k-quant preference

    @property
    def is_cuda(self) -> bool:
        # i-quants are fast only on CUDA; slow on Metal / CPU / partial offload.
        return self.backend == "cuda"


def _is_apple_silicon() -> bool:
    # native_machine() sees through Rosetta (x86_64 Python on Apple Silicon).
    return platform.system() == "Darwin" and arch.native_machine() == "arm64"


def resolve_budget() -> Budget:
    # NVIDIA: real VRAM budget (available = total - used).
    gpus = gpu.enumerate_gpus()
    if gpus:
        snap = gpu.load_snapshot()
        total_mb = float(snap.get("memory_total_mb") or 0.0)
        used_mb = float(snap.get("memory_used_mb") or 0.0)
        avail_mb = (total_mb - used_mb) if (total_mb - used_mb) > 0 else total_mb
        gb = avail_mb / 1024.0
        name = _nvidia_name()
        detected = f"{name}, {gb:.1f} GB VRAM" if name else f"{gb:.1f} GB VRAM"
        return Budget(int(avail_mb * 1024 * 1024), "vram", detected, backend="cuda")

    # Apple Silicon: the GPU shares unified memory, so hw.memsize is the pool — but
    # that pool is also system RAM, so the same OS/app reserve applies.
    if _is_apple_silicon():
        total_mb = float(gpu.load_snapshot().get("memory_total_mb") or 0.0)
        if total_mb > 0:
            gb_total = total_mb / 1024.0
            reserve = max(3.0, gb_total * 0.15)
            usable = max(gb_total - reserve, 0.0)
            return Budget(int(usable * GIB), "vram",
                          f"Apple Silicon, {usable:.1f} GB usable of {gb_total:.0f} GB unified memory",
                          backend="metal")

    # Intel Mac / AMD / no GPU: an integrated GPU's few GB of VRAM is not a useful
    # inference budget — llama.cpp runs on CPU here, so the budget is system RAM,
    # minus a reserve for the OS and other apps.
    info = host.gather()
    gb = info.memory_available_gb or info.memory_total_gb
    reserve = max(3.0, gb * 0.15)
    usable = max(gb - reserve, 0.0)
    return Budget(int(usable * GIB), "ram",
                  f"{usable:.1f} GB usable system RAM of {gb:.0f} GB (no usable GPU)")


def _nvidia_name() -> str | None:
    gpus = gpu.enumerate_gpus()
    if not gpus:
        return None
    if len(gpus) == 1:
        return f"NVIDIA {gpus[0].name} ({gpus[0].compute_cap_sm})"
    return f"{len(gpus)}× NVIDIA GPUs"
