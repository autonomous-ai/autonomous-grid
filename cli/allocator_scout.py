"""Operator-facing autonomous model discovery and real-canary qualification."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from local import config, runtime
from shared import paths
from shared.allocator.models import ModelProfile, stable_digest
from shared.allocator.scout import (
    DEFAULT_ALLOWED_LICENSES,
    DEFAULT_TRUSTED_AUTHORS,
    HuggingFaceDiscovery,
    ScoutPolicy,
    ScoutProposal,
    benchmark_candidate,
    build_proposals,
    load_scout_state,
    proposals_from_state,
    save_scout_state,
)


def cmd_allocator_scout_run(args: argparse.Namespace) -> int:
    cfg = config.select_grid(getattr(args, "grid", None))
    policy = _policy(args)
    from .allocator import _request

    status = _request(cfg, "GET", "/allocator/status")
    discovery = HuggingFaceDiscovery(base_url=args.hub_url)
    try:
        candidates = discovery.discover(policy, search=args.search)
    finally:
        discovery.close()
    proposals = build_proposals(candidates, status)
    state_path = _state_path(cfg, getattr(args, "state_file", None))
    save_scout_state(state_path, proposals, policy=policy)
    payload = {
        "state_file": str(state_path),
        "discovered": len(candidates),
        "benchmark_ready": sum(item.state == "benchmark-ready" for item in proposals),
        "proposals": [item.to_dict() for item in proposals],
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(
            f"Allocator scout: {len(candidates)} immutable candidates · "
            f"{payload['benchmark_ready']} fit this fleet"
        )
        _print_proposals(proposals)
        print(f"  state  {state_path}")
    return 0


def cmd_allocator_scout_status(args: argparse.Namespace) -> int:
    cfg = config.select_grid(getattr(args, "grid", None))
    state_path = _state_path(cfg, getattr(args, "state_file", None))
    state = load_scout_state(state_path)
    proposals = proposals_from_state(state)
    if args.json:
        print(json.dumps({**state, "state_file": str(state_path)}, indent=2))
    else:
        print(
            f"Allocator scout: {len(proposals)} proposals · "
            f"{sum(item.state == 'qualified' for item in proposals)} qualified"
        )
        _print_proposals(proposals)
        print(f"  state  {state_path}")
    return 0


def cmd_allocator_scout_benchmark(args: argparse.Namespace) -> int:
    cfg = config.select_grid(getattr(args, "grid", None))
    state_path = _state_path(cfg, getattr(args, "state_file", None))
    state = load_scout_state(state_path)
    proposals = list(proposals_from_state(state))
    matches = [item for item in proposals if item.proposal_id == args.proposal]
    if not matches:
        raise SystemExit(f"Scout proposal not found: {args.proposal}")
    proposal = matches[0]
    if proposal.state not in ("benchmark-ready", "benchmark-failed", "qualified"):
        raise SystemExit(
            f"Scout proposal {proposal.proposal_id} is {proposal.state}; it cannot be benchmarked."
        )
    if not args.deploy_canary:
        raise SystemExit(
            "Real benchmarking requires --deploy-canary. It creates a min=0/max=1 immutable "
            "profile and never retires an incumbent."
        )

    _put_canary_profile(cfg, proposal, args)
    runner = _GridBenchmarkRunner(
        grid=args.inference_grid,
        startup_timeout=args.startup_timeout,
        request_timeout=args.request_timeout,
    )
    updated, samples = benchmark_candidate(
        proposal,
        runner,
        workloads=args.workloads,
    )
    _record_evaluations(cfg, updated, samples, args)
    proposals[proposals.index(proposal)] = updated
    sample_state = dict(state.get("benchmark_samples") or {})
    sample_state[updated.proposal_id] = samples
    policy = _policy_from_state(state)
    save_scout_state(
        state_path,
        proposals,
        policy=policy,
        benchmark_samples=sample_state,
    )
    payload = {
        "proposal": updated.to_dict(),
        "samples": [
            {
                "workload": sample.workload,
                "quality": sample.quality,
                "latency_ms": sample.latency_ms,
                "output_units": sample.output_units,
                "error": sample.error,
            }
            for sample in samples
        ],
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(
            f"Scout benchmark {updated.state}: {updated.candidate.model_id} · "
            f"quality {updated.benchmark_quality:.2f} · "
            f"median {updated.benchmark_latency_ms or 0:.0f} ms"
        )
        if updated.compare_models:
            print("  compare with  " + ", ".join(updated.compare_models))
    return 0 if updated.state == "qualified" else 1


def cmd_allocator_scout_watch(args: argparse.Namespace) -> int:
    """Run repeated discovery cycles; canaries remain explicit and operator-observable."""

    cycles = 0
    try:
        while args.max_cycles == 0 or cycles < args.max_cycles:
            cmd_allocator_scout_run(args)
            cycles += 1
            if args.max_cycles and cycles >= args.max_cycles:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("Allocator scout stopped.")
    return 0


def _policy(args: argparse.Namespace) -> ScoutPolicy:
    return ScoutPolicy(
        workloads=tuple(args.workloads or ("coding", "general", "research")),
        runtimes=tuple(args.runtimes or ("llama.cpp", "vllm")),
        trusted_authors=tuple(args.authors or DEFAULT_TRUSTED_AUTHORS),
        allowed_licenses=tuple(args.licenses or DEFAULT_ALLOWED_LICENSES),
        quantizations=tuple(args.quantizations or ("Q4_K_M", "Q5_K_M", "Q4_K_S", "Q5_K_S", "Q6_K")),
        max_results=args.limit,
        max_repositories=args.inspect,
        max_artifact_size_mb=args.max_artifact_size_mb,
        min_downloads=args.min_downloads,
    )


def _policy_from_state(state: dict[str, Any]) -> ScoutPolicy | None:
    raw = state.get("policy")
    if not isinstance(raw, dict) or not raw:
        return None
    try:
        return ScoutPolicy(**raw)
    except (TypeError, ValueError):
        return None


def _state_path(cfg: dict[str, Any], explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    identity = str(cfg.get("grid_id") or runtime.grid_url(cfg))
    scope = stable_digest({"grid": identity})[:20]
    return paths.grid_home() / "allocator-scout" / f"{scope}.json"


def _put_canary_profile(
    cfg: dict[str, Any], proposal: ScoutProposal, args: argparse.Namespace
) -> None:
    from .allocator import _control_token, _request

    candidate = proposal.candidate
    backends = ("cuda",) if candidate.runtime == "vllm" else ("cpu", "cuda", "metal")
    profile = ModelProfile(
        model_id=candidate.model_id,
        memory_mb=candidate.estimated_memory_mb,
        runtimes=(candidate.runtime,),
        backends=backends,
        min_replicas=0,
        max_replicas=1,
        target_utilization=0.70,
        replica_concurrency=1,
        expected_service_seconds=15.0,
        latency_slo_ms=float(args.request_timeout) * 1_000.0,
        priority=50,
        load_seconds=float(args.startup_timeout),
        warm_seconds=min(120.0, float(args.startup_timeout)),
        min_residency_seconds=300.0,
        scale_down_cooldown_seconds=300.0,
        min_failure_domains=1,
        min_gpu_count=1 if candidate.runtime == "vllm" else 0,
        artifact_sha256=candidate.artifact_sha256,
        artifact_source=candidate.artifact_source,
        artifact_size_mb=candidate.artifact_size_mb,
        max_colocated_models=1 if candidate.estimated_memory_mb >= 16_000 else 0,
        workload_scores=candidate.workload_scores,
    )
    _request(
        cfg,
        "PUT",
        f"/allocator/models/{quote(candidate.model_id, safe='')}",
        body=profile.to_dict(),
        token=_control_token(cfg, getattr(args, "token_file", None)),
        allow_insecure_http=getattr(args, "allow_insecure_http", False),
    )


def _record_evaluations(cfg, proposal, samples, args) -> None:
    from .allocator import _control_token, _request

    token = _control_token(cfg, getattr(args, "token_file", None))
    for sample in samples:
        _request(
            cfg,
            "POST",
            "/allocator/evaluations",
            body={
                "model_id": proposal.candidate.model_id,
                "workload": sample.workload,
                "artifact_sha256": proposal.candidate.artifact_sha256,
                "quality": sample.quality,
                "error": sample.error,
                "latency_ms": sample.latency_ms,
                "output_units": sample.output_units,
            },
            token=token,
            allow_insecure_http=getattr(args, "allow_insecure_http", False),
        )


class _GridBenchmarkRunner:
    def __init__(self, *, grid: str, startup_timeout: float, request_timeout: float) -> None:
        self.grid = grid
        self.startup_timeout = startup_timeout
        self.request_timeout = request_timeout

    def __call__(self, model: str, prompt: str) -> tuple[str, float]:
        deadline = time.monotonic() + self.startup_timeout
        alias = model.removesuffix(".gguf")
        last_error = ""
        while True:
            started = time.monotonic()
            command = runtime.cli_command() + [
                "--remote",
                "chat",
                "--grid",
                self.grid,
                "-m",
                alias,
                "--allow-self-provider",
                "--timeout",
                str(self.request_timeout),
                "--json",
                prompt,
            ]
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.request_timeout + 10.0,
                check=False,
            )
            elapsed_ms = (time.monotonic() - started) * 1_000.0
            if completed.returncode == 0:
                try:
                    payload = json.loads(completed.stdout)
                    text = str(payload["choices"][0]["message"]["content"])
                    timings = payload.get("timings") or {}
                    latency = float(timings.get("predicted_ms") or elapsed_ms)
                    return text, latency
                except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    last_error = f"malformed Grid response: {exc}"
            else:
                last_error = completed.stderr.strip() or completed.stdout.strip()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(last_error or "canary did not become routable")
            time.sleep(min(5.0, remaining))


def _print_proposals(proposals: list[ScoutProposal] | tuple[ScoutProposal, ...]) -> None:
    for item in proposals[:10]:
        ready_hosts = sum(fit.fits for fit in item.fits)
        quality = (
            f" · quality {item.benchmark_quality:.2f}"
            if item.benchmark_quality is not None
            else ""
        )
        print(
            f"  {item.state:<16} {item.candidate.model_id} · {item.candidate.runtime} · "
            f"{item.candidate.artifact_size_mb} MB · {ready_hosts} hosts{quality}"
        )
        print(f"    id {item.proposal_id} · {item.candidate.repo_id}@{item.candidate.revision[:12]}")
