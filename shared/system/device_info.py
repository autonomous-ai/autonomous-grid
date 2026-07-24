"""``collect_device_info()`` — the single detection entry point.

Assembles one flat inventory of this machine from the individual probes
(``arch`` / ``gpu`` / ``host`` / ``device.resolve_budget`` and the Apple
chip/model/GPU-core probes in ``apple``) — plain, serializable data, so anything
sizing a model to this box never needs a live hardware handle. Only
``device_class`` / ``backend`` / ``usable_bytes`` drive that decision; the rest
is for display.

``backend`` (from ``resolve_budget``) is the single source of truth: it decides
``device_class`` and which ``gpus[]`` are synthesised, so those three can never
disagree — the bug that a separate budget block and host block describing
different machines would reintroduce. Every probe is best-effort; collection
never raises.
"""

from __future__ import annotations

import os
import platform

from shared.system import apple, arch, bandwidth, device, gpu, host
from shared.system.device import Budget

# backend is authoritative; device_class and gpu backend both derive from it.
_BACKEND_TO_CLASS = {"metal": "apple-silicon", "cuda": "nvidia", "cpu": "cpu"}
_CLASS_TO_BACKEND = {cls: backend for backend, cls in _BACKEND_TO_CLASS.items()}

_OS_NAMES = {"Darwin": "macOS", "Linux": "Linux", "Windows": "Windows"}


def _os_name() -> str:
    return _OS_NAMES.get(platform.system(), platform.system() or "unknown")


def _os_version() -> str:
    if platform.system() == "Darwin":
        ver = platform.mac_ver()[0]
        if ver:
            return ver
    return platform.release() or ""


def _int_gb(value) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return 0


def _safe_budget() -> Budget:
    try:
        return device.resolve_budget()
    except Exception:
        return Budget(0, "ram", "detection unavailable", backend="cpu")


def _apple_gpu_entry(chip: str) -> dict:
    try:
        snap = gpu.load_snapshot()
    except Exception:
        snap = {}
    return {
        "index": 0,
        "name": (f"{chip} GPU" if chip else "Apple GPU"),
        "backend": "metal",
        "memory_total_mb": float(snap.get("memory_total_mb") or 0.0),
        "memory_used_mb": float(snap.get("memory_used_mb") or 0.0),
        "compute_cap": None,
        "driver_version": None,
        "utilization_pct": float(snap.get("gpu_util") or 0.0),
        "core_count": apple.gpu_core_count(),
    }


def _nvidia_gpu_entries() -> list[dict]:
    try:
        gpus = gpu.enumerate_gpus()
    except Exception:
        gpus = []
    entries: list[dict] = []
    for g in gpus:
        entries.append({
            "index": g.index,
            "name": g.name,
            "backend": "cuda",
            "memory_total_mb": g.memory_total_mb,
            "memory_used_mb": g.memory_used_mb,
            "compute_cap": g.compute_cap or None,
            "driver_version": g.driver_version or None,
            "utilization_pct": g.utilization_pct,
            "core_count": None,          # NVIDIA does not report an Apple-style core count
        })
    return entries


def collect_device_info() -> dict:
    """Return the full device-info inventory for this machine. Best-effort, never raises."""
    budget = _safe_budget()
    backend = budget.backend if budget.backend in _BACKEND_TO_CLASS else "cpu"
    device_class = _BACKEND_TO_CLASS[backend]

    try:
        hinfo = host.gather()
    except Exception:
        hinfo = None

    # chip + model: Apple exposes both; on NVIDIA/CPU model is null and brand is
    # the CPU brand string.
    if backend == "metal":
        model, chip = apple.describe_chip()
        model = model or None
        cpu_brand = chip or host.cpu_brand()
    else:
        chip = ""
        model = None
        cpu_brand = host.cpu_brand()

    if backend == "metal":
        gpus = [_apple_gpu_entry(chip)]
    elif backend == "cuda":
        gpus = _nvidia_gpu_entries()
    else:
        gpus = []

    logical = hinfo.cpu_count if hinfo else (os.cpu_count() or 1)

    return {
        # ── Decision inputs ──
        "device_class": device_class,
        "backend": backend,
        "usable_bytes": int(budget.total_bytes),
        # Memory bandwidth is the decode bottleneck; null when the chip/GPU isn't recognised, so the
        # ranker falls back to a coarse per-backend default rather than a confidently-wrong number.
        "mem_bandwidth_gbps": bandwidth.estimate(device_class, chip, gpus),
        "detected": budget.detected,
        # ── Full inventory ──
        "machine": {
            "model": model,
            "platform": host.platform_kind(),
            "os_name": _os_name(),
            "os_version": _os_version(),
            "arch": arch.normalized_machine(),
        },
        "cpu": {
            "brand": cpu_brand,
            "physical_cores": host.physical_cores(),
            "logical_threads": int(logical),
        },
        "memory": {
            "total_gb": _int_gb(hinfo.memory_total_gb) if hinfo else 0,
            "available_gb": _int_gb(hinfo.memory_available_gb) if hinfo else 0,
            "used_percent": _int_gb(hinfo.memory_percent) if hinfo else 0,
        },
        "disk": {
            "total_gb": _int_gb(hinfo.disk_total_gb) if hinfo else 0,
            "free_gb": _int_gb(hinfo.disk_free_gb) if hinfo else 0,
        },
        "gpus": gpus,
    }


def consistency_ok(info: dict) -> bool:
    """True when ``device_class`` ↔ ``backend`` ↔ ``gpus[].backend`` all agree
    (apple-silicon↔metal, nvidia↔cuda, cpu↔cpu; a cpu device carries no discrete GPUs)."""
    device_class = info.get("device_class")
    backend = info.get("backend")
    if _CLASS_TO_BACKEND.get(device_class) != backend:
        return False
    gpus = info.get("gpus") or []
    if backend == "cpu":
        return len(gpus) == 0
    return all(g.get("backend") == backend for g in gpus)
