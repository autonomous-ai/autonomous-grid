from __future__ import annotations

from local.logical_test_grid import _estimated_model_memory_mb, _profile, logical_resources


def test_logical_resources_partition_real_physical_capacity_without_multiplying_it():
    gib = 1024**3
    physical = {
        "usable_bytes": 48 * gib,
        "backend": "metal",
        "machine": {"platform": "macos", "arch": "arm64"},
        "memory": {"total_gb": 64, "available_gb": 36},
        "mem_bandwidth_gbps": 800,
        "compute_gflops": 54_000,
    }

    small = logical_resources(
        physical,
        machine_index=0,
        machine_count=4,
        capacity_bytes=8 * gib,
    )()
    large = logical_resources(
        physical,
        machine_index=1,
        machine_count=4,
        capacity_bytes=20 * gib,
    )()

    assert small["usable_bytes"] == 8 * gib
    assert large["usable_bytes"] == 20 * gib
    assert small["memory"]["total_gb"] == 8
    assert large["memory"]["total_gb"] == 20
    assert small["memory"]["available_gb"] == 6
    assert large["memory"]["available_gb"] == 15
    assert small["failure_domain"] == "logical-machine-1"
    assert large["failure_domain"] == "logical-machine-2"


def test_real_test_profile_is_owned_llama_lifecycle_with_bounded_replicas():
    profile = _profile(
        "tiny.gguf",
        3,
        "a" * 64,
        min_replicas=1,
        workload_scores=(("coding", 1.0),),
    )

    assert profile.runtimes == ("llama.cpp",)
    assert profile.min_replicas == 1
    assert profile.max_replicas == 3
    assert profile.artifact_sha256 == "a" * 64
    assert profile.workload_scores == (("coding", 1.0),)


def test_model_memory_estimate_uses_real_cached_weight_size(monkeypatch, tmp_path):
    model = tmp_path / "candidate.gguf"
    model.write_bytes(b"x" * 1024 * 1024)
    monkeypatch.setattr("local.logical_test_grid.paths.models_dir", lambda: tmp_path)

    assert _estimated_model_memory_mb(model.name) == 256
