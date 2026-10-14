"""Memory-bandwidth estimator: the number that lets the ranker tell an M-base from an M-Ultra."""
from __future__ import annotations

from shared.system import bandwidth


def test_apple_scales_with_tier_and_generation():
    assert bandwidth.estimate("apple-silicon", "Apple M3 Max", []) == 400
    assert bandwidth.estimate("apple-silicon", "Apple M1", []) == 67
    assert bandwidth.estimate("apple-silicon", "M2 Ultra", []) == 800
    assert bandwidth.estimate("apple-silicon", "Apple M3 Ultra", []) == 819
    # An Ultra always outpaces a base of the same generation by a wide margin.
    assert bandwidth.estimate("apple-silicon", "M3 Ultra", []) > \
        4 * bandwidth.estimate("apple-silicon", "M3", [])
    # M5 family (Oct 2025): base 153, pro 307, max 614, ultra 1228 GB/s.
    assert bandwidth.estimate("apple-silicon", "Apple M5", []) == 153
    assert bandwidth.estimate("apple-silicon", "Apple M5 Pro", []) == 307
    assert bandwidth.estimate("apple-silicon", "Apple M5 Max", []) == 614
    assert bandwidth.estimate("apple-silicon", "Apple M5 Ultra", []) == 1228


def test_apple_binned_max_reads_its_gpu_core_count():
    # Same chip name, narrower bus: only the GPU core count tells the binned Max from the full one.
    for chip, cores, gbps in (("Apple M3 Max", 30, 300), ("Apple M3 Max", 40, 400),
                              ("Apple M4 Max", 32, 410), ("Apple M4 Max", 40, 546),
                              ("Apple M5 Max", 32, 460), ("Apple M5 Max", 40, 614),
                              ("Apple M1 Max", 24, 400), ("Apple M3 Max", None, 400)):
        assert bandwidth.estimate("apple-silicon", chip, [{"core_count": cores}]) == gbps, (chip, cores)


def test_apple_unknown_generation_falls_back_to_tier_default():
    assert bandwidth.estimate("apple-silicon", "Apple M9 Max", []) == 400   # tier known, gen not
    assert bandwidth.estimate("apple-silicon", "not-a-chip", []) is None


def test_nvidia_reads_the_fastest_recognised_card_most_specific_first():
    assert bandwidth.estimate("nvidia", "", [{"name": "NVIDIA GeForce RTX 4090"}]) == 1008
    # "4070 Ti SUPER" must win over the "4070 Ti" and "4070" substrings.
    assert bandwidth.estimate("nvidia", "", [{"name": "NVIDIA GeForce RTX 4070 Ti SUPER"}]) == 672
    assert bandwidth.estimate("nvidia", "", [{"name": "NVIDIA GeForce RTX 4070 Ti"}]) == 504
    assert bandwidth.estimate("nvidia", "", [{"name": "NVIDIA A100-SXM4-80GB"}]) == 2039
    # The RTX PRO 6000 name shares no fragment with the GeForce cards; Server Edition is slower.
    assert bandwidth.estimate("nvidia", "", [
        {"name": "NVIDIA RTX PRO 6000 Blackwell Workstation Edition"}]) == 1792
    assert bandwidth.estimate("nvidia", "", [
        {"name": "NVIDIA RTX PRO 6000 Blackwell Max-Q Workstation Edition"}]) == 1792
    assert bandwidth.estimate("nvidia", "", [
        {"name": "NVIDIA RTX PRO 6000 Blackwell Server Edition"}]) == 1597
    assert bandwidth.estimate("nvidia", "", [{"name": "NVIDIA RTX 6000 Ada Generation"}]) == 960
    # Multi-GPU: the fastest card sets the pace.
    assert bandwidth.estimate("nvidia", "", [
        {"name": "NVIDIA RTX 3060"}, {"name": "NVIDIA RTX 4090"}]) == 1008
    assert bandwidth.estimate("nvidia", "", [{"name": "Some Unknown Card"}]) is None


def test_nvidia_names_that_contain_a_shorter_fragment_get_their_own_number():
    # Each name, as nvidia-smi reports it, also contains a shorter fragment with another card's
    # number ("a40" in A4000, "5070" in the laptop part, "4080" in the SUPER).
    for name, gbps in (
        ("NVIDIA RTX A4000", 448), ("NVIDIA RTX A5000", 768), ("NVIDIA A100-PCIE-40GB", 1555),
        ("NVIDIA GeForce RTX 4080 SUPER", 736), ("NVIDIA GeForce RTX 4060 Ti", 288),
        ("NVIDIA GeForce RTX 3080 Ti", 912), ("NVIDIA GeForce RTX 3070 Ti", 608),
        ("NVIDIA GeForce RTX 3060 Ti", 448), ("NVIDIA GeForce RTX 5070 Ti", 896),
        ("NVIDIA GeForce RTX 5080 Laptop GPU", 896), ("NVIDIA GeForce RTX 5070 Ti Laptop GPU", 672),
        ("NVIDIA GeForce RTX 5070 Laptop GPU", 384), ("NVIDIA GeForce RTX 5060 Laptop GPU", 384),
        ("NVIDIA RTX PRO 5000 Blackwell", 1344), ("NVIDIA RTX 5000 Ada Generation", 576),
        ("NVIDIA RTX 4000 Ada Generation", 360), ("NVIDIA GB10", 273), ("NVIDIA B200", 7700),
    ):
        assert bandwidth.estimate("nvidia", "", [{"name": name}]) == gbps, name


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
    assert bandwidth.estimate_compute_gflops(
        "nvidia", [{"name": "NVIDIA RTX PRO 6000 Blackwell Workstation Edition"}], None) == 500_000.0
    assert bandwidth.estimate_compute_gflops("nvidia", [{"name": "Some Unknown Card"}], None) is None


def test_cpu_compute_scales_with_physical_cores():
    assert bandwidth.estimate_compute_gflops("cpu", [], 8) == 1520.0
    assert bandwidth.estimate_compute_gflops("cpu", [], None) is None
