"""Memory-bandwidth estimator: the number that lets the ranker tell an M-base from an M-Ultra."""
from __future__ import annotations

from shared.system import bandwidth


def test_apple_scales_with_tier_and_generation():
    assert bandwidth.estimate("apple-silicon", "Apple M3 Max", []) == 400
    assert bandwidth.estimate("apple-silicon", "Apple M1", []) == 67
    assert bandwidth.estimate("apple-silicon", "M2 Ultra", []) == 800
    # An Ultra always outpaces a base of the same generation by a wide margin.
    assert bandwidth.estimate("apple-silicon", "M3 Ultra", []) > \
        4 * bandwidth.estimate("apple-silicon", "M3", [])
    # M5 family (Oct 2025): base 153, pro 307, max 614, ultra 1228 GB/s.
    assert bandwidth.estimate("apple-silicon", "Apple M5", []) == 153
    assert bandwidth.estimate("apple-silicon", "Apple M5 Pro", []) == 307
    assert bandwidth.estimate("apple-silicon", "Apple M5 Max", []) == 614
    assert bandwidth.estimate("apple-silicon", "Apple M5 Ultra", []) == 1228


def test_apple_unknown_generation_falls_back_to_tier_default():
    assert bandwidth.estimate("apple-silicon", "Apple M9 Max", []) == 400   # tier known, gen not
    assert bandwidth.estimate("apple-silicon", "not-a-chip", []) is None


def test_nvidia_reads_the_fastest_recognised_card_most_specific_first():
    assert bandwidth.estimate("nvidia", "", [{"name": "NVIDIA GeForce RTX 4090"}]) == 1008
    # "4070 Ti" must win over the "4070" substring.
    assert bandwidth.estimate("nvidia", "", [{"name": "NVIDIA GeForce RTX 4070 Ti"}]) == 672
    assert bandwidth.estimate("nvidia", "", [{"name": "NVIDIA A100-SXM4-80GB"}]) == 2039
    # Multi-GPU: the fastest card sets the pace.
    assert bandwidth.estimate("nvidia", "", [
        {"name": "NVIDIA RTX 3060"}, {"name": "NVIDIA RTX 4090"}]) == 1008
    assert bandwidth.estimate("nvidia", "", [{"name": "Some Unknown Card"}]) is None


def test_cpu_recognises_intel_macbook_models_by_brand_fragment():
    # 10th-gen Ice Lake — LPDDR4X-3733 dual-channel.
    assert bandwidth.estimate("cpu", "Intel(R) Core(TM) i7-1068NG7", []) == 60
    # 9th-gen Coffee Lake — DDR4-2666 (MacBook Pro 16").
    assert bandwidth.estimate("cpu", "Intel(R) Core(TM) i9-9980HK", []) == 42
    # 8th-gen Coffee Lake — DDR4-2400.
    assert bandwidth.estimate("cpu", "Intel(R) Core(TM) i7-8750H", []) == 38
    # 7th-gen Kaby Lake — LPDDR3-2133.
    assert bandwidth.estimate("cpu", "Intel(R) Core(TM) i5-7360U", []) == 34
    # Unknown / non-Mac Intel falls back to the caller's per-backend default.
    assert bandwidth.estimate("cpu", "Intel(R) Core(TM) i7-12700K", []) is None
    assert bandwidth.estimate("cpu", "", []) is None
    assert bandwidth.estimate("cpu", "Some AMD Ryzen 9 7950X", []) is None


def test_cpu_bandwidth_picks_most_specific_fragment():
    # Longest fragment wins when multiple could match.
    assert bandwidth.estimate(
        "cpu", "Intel(R) Core(TM) i7-1068NG7 vs i7 generic", []
    ) == 60


def test_apple_compute_scales_with_core_count():
    # Per-generation FP16 GFLOPS/core (FP32 × 2). M1=650, M2=714, M3=710, M4=852, M5=1050.
    assert bandwidth.estimate_compute_gflops("apple-silicon", [{"core_count": 8}], None, "Apple M1") == 5200.0
    assert bandwidth.estimate_compute_gflops("apple-silicon", [{"core_count": 10}], None, "M2 Pro") == 7140.0
    assert bandwidth.estimate_compute_gflops("apple-silicon", [{"core_count": 10}], None, "Apple M3") == 7100.0
    assert bandwidth.estimate_compute_gflops("apple-silicon", [{"core_count": 10}], None, "Apple M4") == 8520.0
    assert bandwidth.estimate_compute_gflops("apple-silicon", [{"core_count": 10}], None, "Apple M5") == 10500.0
    assert bandwidth.estimate_compute_gflops("apple-silicon", [{"core_count": 40}], None, "M4 Max") == 34080.0
    assert bandwidth.estimate_compute_gflops("apple-silicon", [{"core_count": None}], None, "M2") is None
    assert bandwidth.estimate_compute_gflops("apple-silicon", [], None, "M2") is None


def test_nvidia_compute_reads_the_fastest_recognised_card():
    assert bandwidth.estimate_compute_gflops(
        "nvidia", [{"name": "NVIDIA H100 80GB HBM3"}], None) == 990_000.0
    assert bandwidth.estimate_compute_gflops(
        "nvidia", [{"name": "NVIDIA GeForce RTX 4090"}], None) == 165_000.0
    assert bandwidth.estimate_compute_gflops("nvidia", [{"name": "Some Unknown Card"}], None) is None


def test_cpu_compute_scales_with_physical_cores():
    assert bandwidth.estimate_compute_gflops("cpu", [], 8) == 1520.0
    assert bandwidth.estimate_compute_gflops("cpu", [], None) is None
