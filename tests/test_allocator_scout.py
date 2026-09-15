from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest

from cli import parser
from shared.allocator.scout import (
    HuggingFaceDiscovery,
    ModelCandidate,
    ScoutPolicy,
    analyze_fleet_fit,
    benchmark_candidate,
    build_proposals,
    load_scout_state,
    proposals_from_state,
    save_scout_state,
)
from shared.allocator.runtime import _parse_hugging_face_artifact_source
from shared.models.download import hf_url


REVISION = "a" * 40
DIGEST = "b" * 64


def _detail(*, repo: str = "Qwen/Qwen-Coder-GGUF", license_name: str = "apache-2.0") -> dict:
    return {
        "id": repo,
        "sha": REVISION,
        "downloads": 10_000,
        "likes": 500,
        "lastModified": datetime.now(timezone.utc).isoformat(),
        "pipeline_tag": "text-generation",
        "tags": ["gguf", "transformers", "code", f"license:{license_name}"],
        "cardData": {"license": license_name},
        "siblings": [
            {"rfilename": "config.json", "size": 100},
            {
                "rfilename": "Qwen-Coder-Q4_K_M.gguf",
                "size": 4_000_000_000,
                "lfs": {"size": 4_000_000_000, "sha256": DIGEST},
            },
            {
                "rfilename": "model-00001-of-00002-Q4_K_M.gguf",
                "lfs": {"size": 2_000_000_000, "sha256": "c" * 64},
            },
            {
                "rfilename": "model.safetensors",
                "size": 5_000_000_000,
                "lfs": {"size": 5_000_000_000, "sha256": "d" * 64},
            },
        ],
    }


def _candidate(**changes) -> ModelCandidate:
    value = ModelCandidate(
        candidate_id="candidate-a",
        repo_id="Qwen/Qwen-Coder-GGUF",
        revision=REVISION,
        runtime="llama.cpp",
        model_id="Qwen-Coder-Q4_K_M.gguf",
        artifact_path="Qwen-Coder-Q4_K_M.gguf",
        artifact_source=f"hf://Qwen/Qwen-Coder-GGUF@{REVISION}/Qwen-Coder-Q4_K_M.gguf",
        artifact_sha256=DIGEST,
        artifact_size_mb=4_000,
        estimated_memory_mb=5_568,
        quantization="Q4_K_M",
        license="apache-2.0",
        downloads=10_000,
        likes=500,
        last_modified=datetime.now(timezone.utc).isoformat(),
        pipeline_tag="text-generation",
        workload_scores=(("coding", 1.0), ("general", 0.7)),
    )
    return replace(value, **changes)


def _node(**changes) -> dict:
    value = {
        "node_id": "gpu-a",
        "state": "accepting",
        "capacity_mb": 24_000,
        "reserved_mb": 4_000,
        "disk_available_mb": 80_000,
        "runtimes": ["llama.cpp", "vllm"],
        "memory_bandwidth_gbps": 1_000,
    }
    value.update(changes)
    return value


def test_policy_normalizes_and_bounds_external_input():
    policy = ScoutPolicy(
        workloads=("Coding", "coding"),
        runtimes=("VLLM",),
        trusted_authors=("Qwen",),
        allowed_licenses=("MIT",),
    )
    assert policy.workloads == ("coding",)
    assert policy.runtimes == ("vllm",)
    assert policy.trusted_authors == ("qwen",)
    with pytest.raises(ValueError, match="max_results"):
        ScoutPolicy(max_results=101)


def test_discovery_resolves_exact_gguf_and_vllm_snapshot_without_downloading_bytes():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/models":
            return httpx.Response(200, json=[{"id": "Qwen/Qwen-Coder-GGUF", "downloads": 10_000}])
        assert request.url.path == "/api/models/Qwen/Qwen-Coder-GGUF"
        return httpx.Response(200, json=_detail())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    discovery = HuggingFaceDiscovery(base_url="https://hub.test", client=client)
    candidates = discovery.discover(
        ScoutPolicy(trusted_authors=("qwen",), runtimes=("llama.cpp", "vllm"))
    )

    assert {item.runtime for item in candidates} == {"llama.cpp", "vllm"}
    gguf = next(item for item in candidates if item.runtime == "llama.cpp")
    assert gguf.artifact_sha256 == DIGEST
    assert gguf.artifact_source == (
        f"hf://Qwen/Qwen-Coder-GGUF@{REVISION}/Qwen-Coder-Q4_K_M.gguf"
    )
    vllm = next(item for item in candidates if item.runtime == "vllm")
    assert vllm.artifact_source == f"hf://Qwen/Qwen-Coder-GGUF@{REVISION}"
    assert len(vllm.artifact_sha256) == 64
    # One GGUF listing + one vLLM listing, but the duplicate repo is inspected only once.
    assert sum(request.url.path == "/api/models" for request in requests) == 2
    assert sum(request.url.path.endswith("Qwen/Qwen-Coder-GGUF") for request in requests) == 1


def test_discovery_rejects_untrusted_gated_mutable_and_unknown_license_repositories():
    details = _detail(license_name="other")
    details["sha"] = "main"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/models":
            return httpx.Response(
                200,
                json=[
                    {"id": "random/model", "downloads": 99_000},
                    {"id": "Qwen/gated", "gated": True, "downloads": 99_000},
                    {"id": "Qwen/mutable", "downloads": 99_000},
                ],
            )
        return httpx.Response(200, json=details)

    discovery = HuggingFaceDiscovery(
        base_url="https://hub.test",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert discovery.discover(ScoutPolicy(trusted_authors=("qwen",), runtimes=("llama.cpp",))) == ()


def test_discovery_keeps_good_candidates_when_one_repository_is_rate_limited():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/models":
            return httpx.Response(
                200,
                json=[
                    {"id": "Qwen/rate-limited", "downloads": 10_000},
                    {"id": "Qwen/Qwen-Coder-GGUF", "downloads": 10_000},
                ],
            )
        if request.url.path.endswith("/rate-limited"):
            return httpx.Response(429)
        return httpx.Response(200, json=_detail())

    discovery = HuggingFaceDiscovery(
        base_url="https://hub.test",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    candidates = discovery.discover(
        ScoutPolicy(trusted_authors=("qwen",), runtimes=("llama.cpp",))
    )

    assert candidates
    assert {candidate.repo_id for candidate in candidates} == {"Qwen/Qwen-Coder-GGUF"}
    assert discovery.issues == ["detail Qwen/rate-limited: HTTP 429"]


def test_fleet_fit_fails_closed_on_runtime_memory_disk_and_state():
    candidate = _candidate()
    fits = analyze_fleet_fit(
        candidate,
        [
            _node(),
            _node(node_id="no-runtime", runtimes=["ollama"]),
            _node(node_id="no-memory", capacity_mb=5_000, reserved_mb=1_000),
            _node(node_id="no-disk", disk_available_mb=1_000),
            _node(node_id="unknown-disk", disk_available_mb=None),
            _node(node_id="paused", state="paused"),
        ],
    )
    assert fits[0].node_id == "gpu-a" and fits[0].fits
    reasons = {item.node_id: item.reason for item in fits}
    assert "runtime" in reasons["no-runtime"]
    assert "memory" in reasons["no-memory"]
    assert "disk" in reasons["no-disk"]
    assert "unknown" in reasons["unknown-disk"]
    assert "not accepting" in reasons["paused"]


def test_proposals_compare_only_models_serving_the_same_workload():
    status = {
        "nodes": [_node()],
        "models": [
            {"model_id": "old-coder", "workload_scores": [["coding", 0.9]]},
            {"model_id": "image", "workload_scores": [["image", 1.0]]},
        ],
    }
    proposal = build_proposals([_candidate()], status)[0]
    assert proposal.state == "benchmark-ready"
    assert proposal.compare_models == ("old-coder",)
    assert proposal.score > 0


def test_benchmark_requires_real_outputs_to_meet_every_case_floor():
    proposal = build_proposals([_candidate()], {"nodes": [_node()], "models": []})[0]

    def passing(_model: str, prompt: str) -> tuple[str, float]:
        if "Python" in prompt:
            return "def clamp(value, low, high):\n    return max(low, min(high, value))", 12
        if "correlation" in prompt:
            return "Correlation is association. Causation means one factor causes another.", 18
        return "GRID", 4

    qualified, samples = benchmark_candidate(proposal, passing)
    assert qualified.state == "qualified"
    assert qualified.benchmark_quality == 1.0
    assert qualified.benchmark_samples == 3
    assert len(samples) == 3

    failed, _ = benchmark_candidate(proposal, lambda _model, _prompt: ("wrong", 1))
    assert failed.state == "benchmark-failed"
    assert failed.benchmark_quality == 0.0


def test_benchmark_transport_failure_is_evidence_not_an_exception():
    proposal = build_proposals([_candidate()], {"nodes": [_node()], "models": []})[0]

    def broken(_model: str, _prompt: str):
        raise RuntimeError("relay offline")

    failed, samples = benchmark_candidate(proposal, broken, workloads=("coding",))
    assert failed.state == "benchmark-failed"
    assert samples[0].error is True


def test_scout_state_round_trips_and_is_owner_only(tmp_path):
    proposal = build_proposals([_candidate()], {"nodes": [_node()], "models": []})[0]
    path = tmp_path / "scout.json"
    save_scout_state(path, [proposal], policy=ScoutPolicy())
    loaded = load_scout_state(path)
    restored = proposals_from_state(loaded)
    assert restored == (proposal,)
    if os.name == "posix":
        assert path.stat().st_mode & 0o077 == 0


def test_scout_state_rejects_unknown_schema(tmp_path):
    path = tmp_path / "scout.json"
    path.write_text(json.dumps({"schema_version": 99, "proposals": []}))
    with pytest.raises(ValueError, match="unsupported"):
        load_scout_state(path)


def test_commit_pinned_gguf_source_reaches_exact_download_revision():
    source = f"hf://Qwen/Qwen-Coder-GGUF@{REVISION}/nested/model.gguf"
    assert _parse_hugging_face_artifact_source(source) == (
        "Qwen/Qwen-Coder-GGUF",
        REVISION,
        "nested/model.gguf",
    )
    assert f"/resolve/{REVISION}/" in hf_url("Qwen/repo", "model.gguf", REVISION)
    with pytest.raises(RuntimeError, match="full 40-hex"):
        _parse_hugging_face_artifact_source(
            "hf://Qwen/Qwen-Coder-GGUF@main/model.gguf"
        )


def test_existing_unpinned_gguf_sources_remain_compatible():
    assert _parse_hugging_face_artifact_source("hf://owner/repo/model.gguf") == (
        "owner/repo",
        "main",
        "model.gguf",
    )


def test_parser_exposes_scout_run_watch_status_and_real_benchmark():
    cli = parser.build_parser()
    assert cli.parse_args(["allocator", "scout", "run"]).handler.__name__ == "cmd_allocator_scout_run"
    assert cli.parse_args(["allocator", "scout", "watch"]).handler.__name__ == "cmd_allocator_scout_watch"
    assert cli.parse_args(["allocator", "scout", "status"]).handler.__name__ == "cmd_allocator_scout_status"
    args = cli.parse_args(
        [
            "allocator",
            "scout",
            "benchmark",
            "proposal-1",
            "--inference-grid",
            "forge",
            "--deploy-canary",
        ]
    )
    assert args.handler.__name__ == "cmd_allocator_scout_benchmark"
    assert args.deploy_canary is True
