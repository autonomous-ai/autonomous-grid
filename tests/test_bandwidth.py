"""Memory-bandwidth estimator: the number that lets the ranker tell an M-base from an M-Ultra."""
from __future__ import annotations

from shared.system import bandwidth


def test_apple_scales_with_tier_and_generation():
    assert bandwidth.estimate("apple-silicon", "Apple M3 Max", []) == 400
    assert bandwidth.estimate("apple-silicon", "Apple M1", []) == 68
    assert bandwidth.estimate("apple-silicon", "M2 Ultra", []) == 800
    # An Ultra always outpaces a base of the same generation by a wide margin.
    assert bandwidth.estimate("apple-silicon", "M3 Ultra", []) > \
        4 * bandwidth.estimate("apple-silicon", "M3", [])


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


def test_plain_cpu_is_left_to_the_caller_fallback():
    assert bandwidth.estimate("cpu", "Intel(R) Core(TM) i9-9980HK", []) is None


def test_apple_compute_scales_with_core_count():
    assert bandwidth.estimate_compute_gflops("apple-silicon", [{"core_count": 10}], None) == 4000.0
    assert bandwidth.estimate_compute_gflops("apple-silicon", [{"core_count": 68}], None) == 27200.0
    assert bandwidth.estimate_compute_gflops("apple-silicon", [{"core_count": None}], None) is None
    assert bandwidth.estimate_compute_gflops("apple-silicon", [], None) is None


def test_nvidia_compute_reads_the_fastest_recognised_card():
    assert bandwidth.estimate_compute_gflops(
        "nvidia", [{"name": "NVIDIA H100 80GB HBM3"}], None) == 990_000.0
    assert bandwidth.estimate_compute_gflops(
        "nvidia", [{"name": "NVIDIA GeForce RTX 4090"}], None) == 165_000.0
    assert bandwidth.estimate_compute_gflops("nvidia", [{"name": "Some Unknown Card"}], None) is None


def test_cpu_compute_scales_with_physical_cores():
    assert bandwidth.estimate_compute_gflops("cpu", [], 8) == 1520.0
    assert bandwidth.estimate_compute_gflops("cpu", [], None) is None
