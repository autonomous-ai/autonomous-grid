"""Device memory-budget resolution."""
from __future__ import annotations

from shared.system import device, gpu

GIB = 1024 ** 3


def test_apple_silicon_budget_reserves_for_os(monkeypatch):
    """Unified memory is also system RAM — the OS/app reserve must apply, or a
    36 GB Mac gets recommendations sized to the whole machine and swaps."""
    monkeypatch.setattr(gpu, "enumerate_gpus", lambda **k: [])
    monkeypatch.setattr(device, "_is_apple_silicon", lambda: True)
    monkeypatch.setattr(gpu, "load_snapshot", lambda **k: {"memory_total_mb": 36.0 * 1024})

    budget = device.resolve_budget()

    assert budget.source == "vram"
    assert budget.total_bytes < 33 * GIB          # meaningfully below the full 36 GB
    assert "usable of 36 GB" in budget.detected


def test_nvidia_budget_uses_available_vram(monkeypatch):
    fake = [type("G", (), {"name": "RTX 4090", "compute_cap_sm": "sm_89"})()]
    monkeypatch.setattr(gpu, "enumerate_gpus", lambda **k: fake)
    monkeypatch.setattr(gpu, "load_snapshot",
                        lambda **k: {"memory_total_mb": 24_576.0, "memory_used_mb": 2_048.0})

    budget = device.resolve_budget()

    assert budget.source == "vram"
    assert budget.total_bytes == int(22_528.0 * 1024 * 1024)   # total - used
    assert "RTX 4090" in budget.detected
