"""Deterministic heterogeneous-fleet scenario lab for allocator development.

This module deliberately simulates planning time rather than model processes. The persistent
``grid test demo`` fixture supplies the separate real-process lifecycle proof. Keeping the two
layers explicit lets developers explore dozens of logical nodes and users on one Mac without
claiming that modeled CUDA, MPS, disk, or VRAM telemetry is physically present.
"""

from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from typing import Any, Iterable

from shared.allocator.demand import DemandTracker
from shared.allocator.intelligence import WorkloadIntelligence, classify_request
from shared.allocator.models import (
    ModelProfile,
    ModelResidency,
    NodeSnapshot,
    NodeState,
    ResidencyState,
)
from shared.allocator.planner import PlacementPlanner, PlannerPolicy


@dataclass(frozen=True, slots=True)
class ScenarioConfig:
    machines: int = 8
    models: int = 8
    users: int = 50
    minutes: int = 30
    seed: int = 42

    def __post_init__(self) -> None:
        bounds = {
            "machines": (self.machines, 1, 64),
            "models": (self.models, 1, 32),
            "users": (self.users, 1, 10_000),
            "minutes": (self.minutes, 6, 1_440),
        }
        for name, (value, minimum, maximum) in bounds.items():
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise ValueError(f"{name} must be in [{minimum}, {maximum}]")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ValueError("seed must be an integer")


@dataclass(frozen=True, slots=True)
class Persona:
    user_id: str
    role: str
    workload: str
    requests_per_minute: float
    service_seconds: float


@dataclass(frozen=True, slots=True)
class LogicalMachine:
    snapshot: NodeSnapshot
    hardware: str
    disk_total_mb: int
    disk_available_mb: int


@dataclass(frozen=True, slots=True)
class CatalogModel:
    profile: ModelProfile
    job: str
    artifact_size_mb: int


@dataclass(frozen=True, slots=True)
class ScenarioReport:
    configuration: dict[str, Any]
    machines: tuple[dict[str, Any], ...]
    models: tuple[dict[str, Any], ...]
    users: tuple[dict[str, Any], ...]
    timeline: tuple[dict[str, Any], ...]
    metrics: dict[str, Any]
    safety: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_PERSONA_BLUEPRINTS = (
    ("software-engineer", "coding", 0.80, 8.0),
    ("researcher", "research", 0.55, 10.0),
    ("marketer", "marketing", 0.70, 5.0),
    ("sales", "sales", 0.75, 4.0),
    ("designer", "design", 0.45, 8.0),
    ("image-creator", "image", 0.20, 20.0),
    ("video-editor", "video", 0.08, 45.0),
    ("operations", "general", 0.65, 4.0),
    ("data-engineer", "embedding", 1.20, 1.0),
)

_REQUESTS: dict[str, tuple[str, str]] = {
    "coding": ("chat/completions", "Debug this Python API, refactor the function, and add tests."),
    "research": ("chat/completions", "Research the evidence, compare sources, and cite the study."),
    "marketing": ("chat/completions", "Write a marketing campaign headline for this audience."),
    "sales": ("chat/completions", "Draft sales outreach for this prospect and handle objections."),
    "design": ("chat/completions", "Design a UI wireframe with a clear layout and typography."),
    "image": ("images/generations", "Create a product illustration for a launch."),
    "video": ("videos/generations", "Create a short launch video from this storyboard."),
    "general": ("chat/completions", "Summarize the meeting and identify the next actions."),
    "embedding": ("embeddings", "Index these internal documents."),
}

_DIRECT_MODEL_BY_WORKLOAD = {"general": "general-assistant"}

_MODEL_BLUEPRINTS = (
    {
        "model_id": "general-assistant",
        "job": "general LLM",
        "memory_mb": 12_000,
        "artifact_mb": 8_000,
        "runtimes": ("llama.cpp", "vllm"),
        "backends": ("metal", "cuda"),
        "scores": (("general", 1.0), ("sales", 0.55), ("marketing", 0.55)),
        "concurrency": 4,
        "min_replicas": 1,
    },
    {
        "model_id": "code-specialist",
        "job": "coding LLM",
        "memory_mb": 24_000,
        "artifact_mb": 16_000,
        "runtimes": ("llama.cpp", "vllm"),
        "backends": ("metal", "cuda"),
        "scores": (("coding", 1.0), ("research", 0.45)),
        "concurrency": 3,
    },
    {
        "model_id": "research-specialist",
        "job": "research LLM",
        "memory_mb": 28_000,
        "artifact_mb": 20_000,
        "runtimes": ("llama.cpp", "vllm"),
        "backends": ("metal", "cuda"),
        "scores": (("research", 1.0), ("general", 0.55)),
        "concurrency": 2,
    },
    {
        "model_id": "creative-writer",
        "job": "marketing and sales LLM",
        "memory_mb": 16_000,
        "artifact_mb": 11_000,
        "runtimes": ("llama.cpp", "vllm"),
        "backends": ("metal", "cuda"),
        "scores": (("marketing", 1.0), ("sales", 0.85), ("design", 0.35)),
        "concurrency": 4,
    },
    {
        "model_id": "image-generator",
        "job": "image generation",
        "memory_mb": 20_000,
        "artifact_mb": 12_000,
        "runtimes": ("comfyui",),
        "backends": ("mps", "cuda"),
        "scores": (("image", 1.0), ("design", 0.70)),
        "concurrency": 1,
    },
    {
        "model_id": "video-generator",
        "job": "video generation",
        "memory_mb": 40_000,
        "artifact_mb": 30_000,
        "runtimes": ("comfyui",),
        "backends": ("mps", "cuda"),
        "scores": (("video", 1.0), ("design", 0.50)),
        "concurrency": 1,
    },
    {
        "model_id": "embedding-model",
        "job": "embedding",
        "memory_mb": 8_000,
        "artifact_mb": 4_000,
        "runtimes": ("llama.cpp", "vllm"),
        "backends": ("metal", "cuda"),
        "scores": (("embedding", 1.0),),
        "concurrency": 16,
    },
    {
        "model_id": "design-multimodal",
        "job": "multimodal design",
        "memory_mb": 32_000,
        "artifact_mb": 22_000,
        "runtimes": ("llama.cpp", "vllm"),
        "backends": ("metal", "cuda"),
        "scores": (("design", 1.0), ("image", 0.35)),
        "concurrency": 2,
    },
    {
        "model_id": "sales-specialist",
        "job": "sales LLM",
        "memory_mb": 14_000,
        "artifact_mb": 9_000,
        "runtimes": ("llama.cpp", "vllm"),
        "backends": ("metal", "cuda"),
        "scores": (("sales", 1.0), ("marketing", 0.60)),
        "concurrency": 5,
    },
)

_MACHINE_BLUEPRINTS = (
    {
        "hardware": "Mac Studio M2 Ultra 192 GB",
        "capacity": 196_608,
        "reserved": 24_576,
        "runtimes": ("llama.cpp",),
        "backends": ("metal",),
        "gpu_count": 1,
        "gpu_memory": (196_608,),
        "disk_total": 2_000_000,
        "disk_free": 1_100_000,
        "concurrency": 8,
        "cost": 0.22,
    },
    {
        "hardware": "Mac Studio M2 Max 64 GB",
        "capacity": 65_536,
        "reserved": 8_192,
        "runtimes": ("llama.cpp",),
        "backends": ("metal",),
        "gpu_count": 1,
        "gpu_memory": (65_536,),
        "disk_total": 1_000_000,
        "disk_free": 180_000,
        "concurrency": 4,
        "cost": 0.12,
    },
    {
        "hardware": "2x RTX Pro 6000 Blackwell",
        "capacity": 196_608,
        "reserved": 16_384,
        "runtimes": ("vllm", "comfyui"),
        "backends": ("cuda",),
        "gpu_count": 2,
        "gpu_memory": (98_304, 98_304),
        "disk_total": 4_000_000,
        "disk_free": 2_400_000,
        "concurrency": 32,
        "cost": 2.80,
    },
    {
        "hardware": "RTX 4090 workstation",
        "capacity": 24_576,
        "reserved": 4_096,
        "runtimes": ("vllm", "comfyui"),
        "backends": ("cuda",),
        "gpu_count": 1,
        "gpu_memory": (24_576,),
        "disk_total": 1_000_000,
        "disk_free": 48_000,
        "concurrency": 8,
        "cost": 0.75,
    },
    {
        "hardware": "Mac Studio media node 192 GB",
        "capacity": 196_608,
        "reserved": 32_768,
        "runtimes": ("comfyui",),
        "backends": ("mps",),
        "gpu_count": 1,
        "gpu_memory": (196_608,),
        "disk_total": 2_000_000,
        "disk_free": 620_000,
        "concurrency": 2,
        "cost": 0.24,
    },
    {
        "hardware": "Mac mini M2 Pro 32 GB",
        "capacity": 32_768,
        "reserved": 6_144,
        "runtimes": ("llama.cpp",),
        "backends": ("metal",),
        "gpu_count": 1,
        "gpu_memory": (32_768,),
        "disk_total": 500_000,
        "disk_free": 36_000,
        "concurrency": 2,
        "cost": 0.07,
    },
)


def run_scenario(config: ScenarioConfig) -> ScenarioReport:
    # Independent streams make capacity sweeps comparable: changing the machine/model inventory
    # must not silently generate different users or request arrivals for the same seed.
    topology_rng = random.Random(config.seed ^ 0x4E4F4445)
    persona_rng = random.Random(config.seed ^ 0x55534552)
    rng = random.Random(config.seed ^ 0x44454D41)
    catalog = _build_catalog(config.models, config.machines)
    machines = _build_machines(config.machines, catalog, topology_rng)
    personas = _build_personas(config.users, persona_rng)
    intelligence = WorkloadIntelligence(portfolio_min_samples=3)
    direct_demand = DemandTracker()
    planner_policy = PlannerPolicy(memory_headroom_fraction=0.05, node_ttl_seconds=180)
    planner = PlacementPlanner(planner_policy)
    profiles = tuple(item.profile for item in catalog)
    profile_by_id = {item.profile.model_id: item.profile for item in catalog}
    artifact_by_id = {item.profile.model_id: item.artifact_size_mb for item in catalog}
    nodes = tuple(item.snapshot for item in machines)

    requested_by_workload: Counter[str] = Counter()
    served_by_workload: defaultdict[str, float] = defaultdict(float)
    requested_by_user: Counter[str] = Counter()
    served_by_user: defaultdict[str, float] = defaultdict(float)
    total_requests = 0
    loads = 0
    unloads = 0
    migrations = 0
    cache_loads = 0
    cold_start_seconds = 0.0
    artifact_download_mb = 0
    unsatisfied_replica_minutes = 0
    shortfall_by_model: Counter[str] = Counter()
    peak_shortfall_by_model: Counter[str] = Counter()
    catalog_gap_requests = 0
    direct_named_requests = 0
    suitability_weight = 0.0
    memory_utilization: list[float] = []
    cost = 0.0
    safety_violations: list[str] = []
    timeline: list[dict[str, Any]] = []
    prior_phase = ""
    prior_states = {node.node_id: node.state for node in nodes}
    disk_available = {
        machine.snapshot.node_id: machine.disk_available_mb for machine in machines
    }

    base_time = 1_000_000.0
    nodes = _refresh_disk_admission(nodes, catalog, disk_available)
    bootstrap = planner.plan(nodes, profiles, (), now=base_time - 30.0)
    bootstrap_violations = _validate_plan(
        bootstrap.assignments,
        nodes,
        profile_by_id,
        artifact_by_id,
        disk_available,
        planner_policy,
    )
    safety_violations.extend(f"bootstrap: {item}" for item in bootstrap_violations)
    bootstrap_loads: list[str] = []
    for assignment in bootstrap.assignments:
        node = next(item for item in nodes if item.node_id == assignment.node_id)
        profile = profile_by_id[assignment.model_id]
        cached = assignment.model_id in node.cached_models
        loads += 1
        cache_loads += int(cached)
        cold_start_seconds += profile.warm_seconds + (0.0 if cached else profile.load_seconds)
        if not cached:
            disk_available[assignment.node_id] -= artifact_by_id[assignment.model_id]
            artifact_download_mb += artifact_by_id[assignment.model_id]
        bootstrap_loads.append(f"{assignment.model_id}@{assignment.node_id}")
    nodes = _materialize(bootstrap.assignments, nodes, base_time - 15.0)
    if bootstrap_loads:
        timeline.append(
            {
                "minute": -1,
                "phase": "bootstrap",
                "requests": 0,
                "workloads": {},
                "desired_replicas": dict(bootstrap.desired_replicas),
                "loads": sorted(bootstrap_loads),
                "unloads": [],
                "node_changes": [],
                "unsatisfied": [],
            }
        )
    prior_phase = "bootstrap"
    prior_states = {node.node_id: node.state for node in nodes}

    for minute in range(config.minutes):
        now = base_time + minute * 60.0
        phase = _phase(minute, config.minutes)
        nodes = _evolve_nodes(nodes, phase, now)
        nodes = _refresh_disk_admission(nodes, catalog, disk_available)
        request_counts: Counter[str] = Counter()
        request_counts_by_user: Counter[str] = Counter()
        service_totals: defaultdict[str, float] = defaultdict(float)
        current_ready = _ready_models(nodes)

        for persona_index, persona in enumerate(personas):
            expected = persona.requests_per_minute * _demand_multiplier(phase, persona.workload)
            count = math.floor(expected)
            if rng.random() < expected - count:
                count += 1
            if count <= 0:
                continue
            endpoint, prompt = _REQUESTS[persona.workload]
            explicit_model = (
                _DIRECT_MODEL_BY_WORKLOAD.get(persona.workload, "")
                if _DIRECT_MODEL_BY_WORKLOAD.get(persona.workload, "") in profile_by_id
                else ""
            )
            features = replace(
                classify_request(
                    endpoint,
                    {
                        "model": explicit_model or "auto",
                        "messages": [{"role": "user", "content": prompt}],
                        "prompt": prompt,
                        "max_completion_tokens": 256,
                    },
                ),
                tenant_class=f"cohort-{persona_index % 16:02d}",
            )
            served_model = (
                explicit_model
                if explicit_model and explicit_model in current_ready
                else (
                    ""
                    if explicit_model
                    else _best_ready_model(features.workload, current_ready, profile_by_id)
                )
            )
            capability = (
                profile_by_id[served_model].workload_score(features.workload)
                if served_model
                else 0.0
            )
            for _ in range(count):
                intelligence.observe(
                    features,
                    served_model=served_model,
                    portfolio_unbound=not explicit_model,
                    service_seconds=persona.service_seconds,
                    latency_ms=persona.service_seconds * 1_000.0,
                    queue_depth=0 if served_model else 1,
                    error=not served_model,
                    output_units=128 if features.workload not in {"image", "video"} else 1,
                    quality=(max(0.0, min(1.0, capability * 0.95)) if served_model else None),
                    timestamp=now + rng.random() * 30.0,
                )
                if explicit_model:
                    direct_demand.observe(
                        explicit_model,
                        service_seconds=persona.service_seconds,
                        latency_ms=persona.service_seconds * 1_000.0,
                        queue_depth=0 if served_model else 1,
                        errors=int(not served_model),
                        timestamp=now + rng.random() * 30.0,
                    )
            request_counts[features.workload] += count
            request_counts_by_user[persona.user_id] += count
            service_totals[features.workload] += count * persona.service_seconds
            direct_named_requests += count if explicit_model else 0

        direct_forecasts = direct_demand.forecasts(
            tuple(profile_by_id),
            now=now + 30.0,
        )
        forecasts = intelligence.portfolio_forecasts(
            profiles,
            direct_forecasts,
            now=now + 30.0,
        )
        plan = planner.plan(nodes, profiles, forecasts, now=now + 30.0)
        violations = _validate_plan(
            plan.assignments,
            nodes,
            profile_by_id,
            artifact_by_id,
            disk_available,
            planner_policy,
        )
        safety_violations.extend(f"minute {minute}: {item}" for item in violations)

        before_pairs = {
            (node.node_id, residency.model_id)
            for node in nodes
            for residency in node.residencies
            if residency.state == ResidencyState.READY
        }
        after_pairs = plan.desired_pairs
        added = after_pairs - before_pairs
        removed = before_pairs - after_pairs
        loads += len(added)
        unloads += len(removed)
        for node_id, model_id in added:
            node = next(item for item in nodes if item.node_id == node_id)
            profile = profile_by_id[model_id]
            cached = model_id in node.cached_models
            cache_loads += int(cached)
            cold_start_seconds += profile.warm_seconds + (0.0 if cached else profile.load_seconds)
            if not cached:
                disk_available[node_id] -= artifact_by_id[model_id]
                artifact_download_mb += artifact_by_id[model_id]
        before_nodes: defaultdict[str, set[str]] = defaultdict(set)
        after_nodes: defaultdict[str, set[str]] = defaultdict(set)
        for node_id, model_id in before_pairs:
            before_nodes[model_id].add(node_id)
        for node_id, model_id in after_pairs:
            after_nodes[model_id].add(node_id)
        migrations += sum(
            min(len(before_nodes[model] - after_nodes[model]), len(after_nodes[model] - before_nodes[model]))
            for model in set(before_nodes).union(after_nodes)
        )
        unsatisfied_replica_minutes += sum(item.missing_replicas for item in plan.unsatisfied)
        for item in plan.unsatisfied:
            shortfall_by_model[item.model_id] += item.missing_replicas
            peak_shortfall_by_model[item.model_id] = max(
                peak_shortfall_by_model[item.model_id],
                item.missing_replicas,
            )

        projection_by_workload = {
            row["workload"]: row
            for row in intelligence.projections(profiles, now=now + 30.0)
        }
        capacity_by_model = {
            model_id: len(plan.nodes_for(model_id))
            * profile_by_id[model_id].replica_concurrency
            * profile_by_id[model_id].target_utilization
            for model_id in profile_by_id
        }
        offered_by_model: defaultdict[str, float] = defaultdict(float)
        chosen_by_workload: dict[str, str] = {}
        for workload, count in request_counts.items():
            chosen = _DIRECT_MODEL_BY_WORKLOAD.get(workload, "")
            if chosen not in profile_by_id:
                chosen = ""
            if not chosen:
                chosen = str(
                    (projection_by_workload.get(workload) or {}).get("chosen_model") or ""
                )
            chosen_by_workload[workload] = chosen
            if not chosen:
                catalog_gap_requests += count
                continue
            offered_by_model[chosen] += service_totals[workload] / 60.0
            suitability_weight += count * profile_by_id[chosen].workload_score(workload)
        service_ratio_by_model = {
            model_id: min(1.0, capacity_by_model.get(model_id, 0.0) / offered)
            if offered > 0
            else 1.0
            for model_id, offered in offered_by_model.items()
        }
        for workload, count in request_counts.items():
            chosen = chosen_by_workload.get(workload, "")
            served_by_workload[workload] += count * service_ratio_by_model.get(chosen, 0.0)
            requested_by_workload[workload] += count
            total_requests += count
        for persona in personas:
            count = request_counts_by_user[persona.user_id]
            if not count:
                continue
            chosen = chosen_by_workload.get(persona.workload, "")
            requested_by_user[persona.user_id] += count
            served_by_user[persona.user_id] += count * service_ratio_by_model.get(chosen, 0.0)

        utilization = _memory_utilization(plan.assignments, nodes)
        memory_utilization.append(utilization)
        active_node_ids = {item.node_id for item in plan.assignments}
        cost += sum(node.cost_per_hour for node in nodes if node.node_id in active_node_ids) / 60.0

        state_changes = [
            f"{node.node_id}:{prior_states.get(node.node_id, node.state).value}->{node.state.value}"
            for node in nodes
            if prior_states.get(node.node_id) != node.state
        ]
        desired = dict(plan.desired_replicas)
        if phase != prior_phase or added or removed or plan.unsatisfied or state_changes:
            timeline.append(
                {
                    "minute": minute,
                    "phase": phase,
                    "requests": sum(request_counts.values()),
                    "workloads": dict(sorted(request_counts.items())),
                    "desired_replicas": desired,
                    "loads": [f"{model}@{node}" for node, model in sorted(added)],
                    "unloads": [f"{model}@{node}" for node, model in sorted(removed)],
                    "node_changes": state_changes,
                    "unsatisfied": [
                        {
                            "model": item.model_id,
                            "missing": item.missing_replicas,
                            "code": item.code,
                        }
                        for item in plan.unsatisfied
                    ],
                }
            )
        prior_phase = phase
        prior_states = {node.node_id: node.state for node in nodes}
        nodes = _materialize(plan.assignments, nodes, now + 45.0)

    total_served = sum(served_by_workload.values())
    service_rates = [
        served_by_workload[workload] / requested
        for workload, requested in requested_by_workload.items()
        if requested > 0
    ]
    workload_fairness = _jain(service_rates)
    user_service_rates = [
        served_by_user[user_id] / requested
        for user_id, requested in requested_by_user.items()
        if requested > 0
    ]
    user_fairness = _jain(user_service_rates)
    workload_slo_attainment = (
        sum(rate >= 0.90 for rate in service_rates) / len(service_rates)
        if service_rates
        else 1.0
    )
    user_slo_attainment = (
        sum(rate >= 0.90 for rate in user_service_rates) / len(user_service_rates)
        if user_service_rates
        else 1.0
    )
    churn = loads + unloads
    service_rate = total_served / total_requests if total_requests else 1.0
    suitability = suitability_weight / total_requests if total_requests else 1.0
    churn_efficiency = max(0.0, 1.0 - churn / max(1.0, config.minutes * config.machines))
    safety_rate = 1.0 if not safety_violations else 0.0
    overall = 100.0 * (
        0.35 * service_rate
        + 0.10 * user_fairness
        + 0.05 * workload_fairness
        + 0.10 * suitability
        + 0.10 * workload_slo_attainment
        + 0.10 * churn_efficiency
        + 0.20 * safety_rate
    )

    machine_rows = tuple(
        {
            "node_id": machine.snapshot.node_id,
            "hardware": machine.hardware,
            "memory_mb": machine.snapshot.capacity_mb,
            "reserved_mb": machine.snapshot.reserved_mb,
            "disk_total_mb": machine.disk_total_mb,
            "disk_available_mb": machine.disk_available_mb,
            "runtimes": machine.snapshot.runtimes,
            "backends": machine.snapshot.backends,
            "max_models": machine.snapshot.max_models,
            "cached_models": machine.snapshot.cached_models,
        }
        for machine in machines
    )
    model_rows = tuple(
        {
            "model_id": item.profile.model_id,
            "job": item.job,
            "memory_mb": item.profile.memory_mb,
            "artifact_size_mb": item.artifact_size_mb,
            "runtimes": item.profile.runtimes,
            "workload_scores": dict(item.profile.workload_scores),
            "replica_concurrency": item.profile.replica_concurrency,
        }
        for item in catalog
    )
    persona_counts = Counter((item.role, item.workload) for item in personas)
    role_requested: Counter[tuple[str, str]] = Counter()
    role_served: defaultdict[tuple[str, str], float] = defaultdict(float)
    for persona in personas:
        key = (persona.role, persona.workload)
        role_requested[key] += requested_by_user[persona.user_id]
        role_served[key] += served_by_user[persona.user_id]
    user_rows = tuple(
        {
            "role": role,
            "workload": workload,
            "users": count,
            "requests": role_requested[(role, workload)],
            "served_equivalent": round(role_served[(role, workload)], 2),
            "service_rate_pct": (
                round(
                    100.0
                    * role_served[(role, workload)]
                    / role_requested[(role, workload)],
                    2,
                )
                if role_requested[(role, workload)]
                else 100.0
            ),
        }
        for (role, workload), count in sorted(persona_counts.items())
    )
    per_workload = {
        workload: {
            "requests": requested,
            "served_equivalent": round(served_by_workload[workload], 2),
            "service_rate_pct": round(100.0 * served_by_workload[workload] / requested, 2),
        }
        for workload, requested in sorted(requested_by_workload.items())
    }
    return ScenarioReport(
        configuration={**asdict(config), "mode": "deterministic planning simulation"},
        machines=machine_rows,
        models=model_rows,
        users=user_rows,
        timeline=tuple(timeline),
        metrics={
            "overall_score": round(overall, 2),
            "total_requests": total_requests,
            "served_equivalent": round(total_served, 2),
            "service_rate_pct": round(100.0 * service_rate, 2),
            "fairness_pct": round(100.0 * user_fairness, 2),
            "user_fairness_pct": round(100.0 * user_fairness, 2),
            "workload_fairness_pct": round(100.0 * workload_fairness, 2),
            "user_slo_attainment_pct": round(100.0 * user_slo_attainment, 2),
            "workload_slo_attainment_pct": round(100.0 * workload_slo_attainment, 2),
            "minimum_workload_service_pct": round(100.0 * min(service_rates, default=1.0), 2),
            "portfolio_suitability_pct": round(100.0 * suitability, 2),
            "average_memory_utilization_pct": round(100.0 * sum(memory_utilization) / len(memory_utilization), 2),
            "peak_memory_utilization_pct": round(100.0 * max(memory_utilization, default=0.0), 2),
            "loads": loads,
            "unloads": unloads,
            "migrations": migrations,
            "cache_hit_rate_pct": round(100.0 * cache_loads / loads, 2) if loads else 100.0,
            "modeled_cold_start_seconds": round(cold_start_seconds, 2),
            "artifact_download_mb": artifact_download_mb,
            "minimum_remaining_disk_mb": min(disk_available.values(), default=0),
            "modeled_compute_cost": round(cost, 4),
            "unsatisfied_replica_minutes": unsatisfied_replica_minutes,
            "shortfall_by_model": dict(sorted(shortfall_by_model.items())),
            "peak_shortfall_by_model": dict(sorted(peak_shortfall_by_model.items())),
            "catalog_gap_requests": catalog_gap_requests,
            "direct_named_requests": direct_named_requests,
            "per_workload": per_workload,
        },
        safety={
            "passed": not safety_violations,
            "violations": tuple(safety_violations),
            "checks": (
                "memory and headroom",
                "runtime/backend compatibility",
                "node lifecycle eligibility",
                "one-model logical serving slot",
                "modeled artifact disk admission",
                "desired/preemption disjointness",
            ),
        },
    )


def _build_catalog(count: int, machines: int) -> tuple[CatalogModel, ...]:
    rows: list[CatalogModel] = []
    for index in range(count):
        source = _MODEL_BLUEPRINTS[index % len(_MODEL_BLUEPRINTS)]
        variant = index // len(_MODEL_BLUEPRINTS)
        model_id = str(source["model_id"])
        if variant:
            model_id = f"{model_id}-v{variant + 1}"
        memory_mb = int(source["memory_mb"]) + variant * 2_000
        profile = ModelProfile(
            model_id=model_id,
            memory_mb=memory_mb,
            runtimes=tuple(source["runtimes"]),
            backends=tuple(source["backends"]),
            min_replicas=int(source.get("min_replicas", 0)) if index == 0 else 0,
            max_replicas=machines,
            target_utilization=0.70,
            replica_concurrency=int(source["concurrency"]),
            expected_service_seconds=5.0,
            priority=100,
            load_seconds=20.0 + int(source["artifact_mb"]) / 2_000.0,
            warm_seconds=3.0 + memory_mb / 8_000.0,
            min_residency_seconds=0,
            scale_down_cooldown_seconds=120,
            min_failure_domains=1,
            max_colocated_models=1,
            workload_scores=tuple(source["scores"]),
        )
        rows.append(
            CatalogModel(
                profile=profile,
                job=str(source["job"]),
                artifact_size_mb=int(source["artifact_mb"]) + variant * 1_000,
            )
        )
    return tuple(rows)


def _build_machines(
    count: int,
    catalog: tuple[CatalogModel, ...],
    rng: random.Random,
) -> tuple[LogicalMachine, ...]:
    result: list[LogicalMachine] = []
    for index in range(count):
        source = _MACHINE_BLUEPRINTS[index % len(_MACHINE_BLUEPRINTS)]
        node_id = f"logical-{index + 1:02d}"
        compatible = [
            item
            for item in catalog
            if set(item.profile.runtimes).intersection(source["runtimes"])
            and set(item.profile.backends).intersection(source["backends"])
            and item.profile.memory_mb <= int(source["capacity"]) - int(source["reserved"])
            and item.artifact_size_mb <= int(source["disk_free"])
        ]
        rng.shuffle(compatible)
        cached: list[str] = []
        cache_budget = int(source["disk_free"]) // 2
        for item in compatible:
            if item.artifact_size_mb <= cache_budget and len(cached) < 3:
                cached.append(item.profile.model_id)
                cache_budget -= item.artifact_size_mb
        remaining_disk = int(source["disk_free"]) - sum(
            item.artifact_size_mb for item in compatible if item.profile.model_id in cached
        )
        allowed = tuple(
            item.profile.model_id
            for item in compatible
            if item.profile.model_id in cached or item.artifact_size_mb <= remaining_disk
        )
        snapshot = NodeSnapshot(
            node_id=node_id,
            capacity_mb=int(source["capacity"]),
            reserved_mb=int(source["reserved"]),
            runtimes=tuple(source["runtimes"]),
            backends=tuple(source["backends"]),
            state=NodeState.ACCEPTING,
            failure_domain=f"domain-{index % 3 + 1}",
            allowed_data_tiers=("internal", "public"),
            allowed_models=allowed,
            tags=("logical-scenario",),
            max_models=1,
            cached_models=tuple(cached),
            max_concurrency=int(source["concurrency"]),
            memory_bandwidth_gbps=100.0 + index * 20.0,
            compute_gflops=6_000.0 + index * 2_000.0,
            gpu_count=int(source["gpu_count"]),
            gpu_memory_mb=tuple(source["gpu_memory"]),
            cost_per_hour=float(source["cost"]),
            last_heartbeat=1_000_000.0,
        )
        result.append(
            LogicalMachine(
                snapshot=snapshot,
                hardware=str(source["hardware"]),
                disk_total_mb=int(source["disk_total"]),
                disk_available_mb=remaining_disk,
            )
        )
    return tuple(result)


def _build_personas(count: int, rng: random.Random) -> tuple[Persona, ...]:
    offset = rng.randrange(len(_PERSONA_BLUEPRINTS))
    result: list[Persona] = []
    for index in range(count):
        role, workload, rate, service = _PERSONA_BLUEPRINTS[(index + offset) % len(_PERSONA_BLUEPRINTS)]
        result.append(
            Persona(
                user_id=f"user-{index + 1:05d}",
                role=role,
                workload=workload,
                requests_per_minute=rate * rng.uniform(0.75, 1.25),
                service_seconds=service * rng.uniform(0.85, 1.15),
            )
        )
    return tuple(result)


def _phase(minute: int, minutes: int) -> str:
    fraction = minute / max(1, minutes)
    if fraction < 0.18:
        return "warmup"
    if fraction < 0.38:
        return "coding-surge"
    if fraction < 0.56:
        return "creative-campaign"
    if fraction < 0.70:
        return "node-outage"
    if fraction < 0.88:
        return "research-recovery"
    return "cooldown"


def _demand_multiplier(phase: str, workload: str) -> float:
    base = {
        "warmup": 0.35,
        "coding-surge": 0.75,
        "creative-campaign": 0.65,
        "node-outage": 0.80,
        "research-recovery": 0.70,
        "cooldown": 0.08,
    }[phase]
    boosts = {
        "coding-surge": {"coding": 4.0, "research": 1.5, "embedding": 1.4},
        "creative-campaign": {"marketing": 3.0, "sales": 2.5, "design": 3.0, "image": 3.5, "video": 4.0},
        "node-outage": {"general": 2.0, "sales": 1.5},
        "research-recovery": {"research": 4.0, "embedding": 2.5, "coding": 1.3},
    }
    return base * boosts.get(phase, {}).get(workload, 1.0)


def _evolve_nodes(
    nodes: tuple[NodeSnapshot, ...],
    phase: str,
    now: float,
) -> tuple[NodeSnapshot, ...]:
    result: list[NodeSnapshot] = []
    for index, node in enumerate(nodes):
        state = NodeState.ACCEPTING
        if phase == "creative-campaign" and index == 0:
            state = NodeState.THROTTLED
        if phase == "node-outage" and index == min(2, len(nodes) - 1):
            state = NodeState.PAUSED
        result.append(replace(node, state=state, last_heartbeat=now + 30.0))
    return tuple(result)


def _ready_models(nodes: Iterable[NodeSnapshot]) -> frozenset[str]:
    return frozenset(
        residency.model_id
        for node in nodes
        if node.state in (NodeState.ACCEPTING, NodeState.THROTTLED)
        for residency in node.residencies
        if residency.state == ResidencyState.READY
    )


def _best_ready_model(
    workload: str,
    ready_models: Iterable[str],
    profiles: dict[str, ModelProfile],
) -> str:
    candidates = [
        profiles[model_id]
        for model_id in ready_models
        if model_id in profiles and profiles[model_id].workload_score(workload) > 0
    ]
    if not candidates:
        return ""
    return max(candidates, key=lambda item: (item.workload_score(workload), item.model_id)).model_id


def _materialize(
    assignments: Iterable[Any],
    nodes: tuple[NodeSnapshot, ...],
    now: float,
) -> tuple[NodeSnapshot, ...]:
    by_node: defaultdict[str, list[ModelResidency]] = defaultdict(list)
    for assignment in assignments:
        by_node[assignment.node_id].append(
            ModelResidency(
                model_id=assignment.model_id,
                memory_mb=assignment.memory_mb,
                state=ResidencyState.READY,
                loaded_at=now,
                last_used_at=now,
            )
        )
    return tuple(
        replace(
            node,
            residencies=tuple(by_node[node.node_id]),
            cached_models=tuple(
                sorted({*node.cached_models, *(item.model_id for item in by_node[node.node_id])})
            ),
            last_heartbeat=now,
        )
        for node in nodes
    )


def _refresh_disk_admission(
    nodes: tuple[NodeSnapshot, ...],
    catalog: tuple[CatalogModel, ...],
    disk_available: dict[str, int],
) -> tuple[NodeSnapshot, ...]:
    return tuple(
        replace(
            node,
            allowed_models=tuple(
                item.profile.model_id
                for item in catalog
                if set(item.profile.runtimes).intersection(node.runtimes)
                and set(item.profile.backends).intersection(node.backends)
                and item.profile.memory_for(node.runtimes) <= node.usable_capacity_mb
                and (
                    item.profile.model_id in node.cached_models
                    or item.artifact_size_mb <= disk_available[node.node_id]
                )
            ),
        )
        for node in nodes
    )


def _memory_utilization(assignments: Iterable[Any], nodes: tuple[NodeSnapshot, ...]) -> float:
    node_by_id = {node.node_id: node for node in nodes}
    used = sum(item.memory_mb for item in assignments)
    available = sum(
        node.usable_capacity_mb
        for node in nodes
        if node.state in (NodeState.ACCEPTING, NodeState.THROTTLED)
    )
    if not available:
        return 0.0
    # Referencing the index also ensures every assignment names a current node before scoring.
    assert all(item.node_id in node_by_id for item in assignments)
    return min(1.0, used / available)


def _validate_plan(
    assignments: Iterable[Any],
    nodes: tuple[NodeSnapshot, ...],
    profiles: dict[str, ModelProfile],
    artifact_sizes: dict[str, int],
    disk_available: dict[str, int],
    policy: PlannerPolicy,
) -> tuple[str, ...]:
    violations: list[str] = []
    node_by_id = {node.node_id: node for node in nodes}
    assigned_by_node: defaultdict[str, list[Any]] = defaultdict(list)
    for assignment in assignments:
        assigned_by_node[assignment.node_id].append(assignment)
    for node_id, rows in assigned_by_node.items():
        node = node_by_id.get(node_id)
        if node is None:
            violations.append(f"assignment references missing node {node_id}")
            continue
        if node.state not in (NodeState.ACCEPTING, NodeState.THROTTLED):
            violations.append(f"assignment placed on {node.state.value} node {node_id}")
        if node.max_models is not None and len(rows) > node.max_models:
            violations.append(f"model-slot overcommit on {node_id}")
        used_memory = sum(item.memory_mb for item in rows)
        multiplier = policy.throttled_capacity_fraction if node.state == NodeState.THROTTLED else 1.0
        budget = math.floor(node.usable_capacity_mb * multiplier)
        if used_memory > budget:
            violations.append(f"memory overcommit on {node_id}: {used_memory}>{budget}")
        for assignment in rows:
            profile = profiles[assignment.model_id]
            if not set(profile.runtimes).intersection(node.runtimes):
                violations.append(f"runtime mismatch for {assignment.model_id} on {node_id}")
            if not set(profile.backends).intersection(node.backends):
                violations.append(f"backend mismatch for {assignment.model_id} on {node_id}")
            if node.allowed_models and assignment.model_id not in node.allowed_models:
                violations.append(f"disk/admission mismatch for {assignment.model_id} on {node_id}")
            if assignment.model_id not in node.cached_models and artifact_sizes[assignment.model_id] > disk_available[node_id]:
                violations.append(f"artifact disk overcommit for {assignment.model_id} on {node_id}")
    return tuple(violations)


def _jain(values: Iterable[float]) -> float:
    rows = tuple(values)
    if not rows:
        return 1.0
    denominator = len(rows) * sum(value * value for value in rows)
    return (sum(rows) ** 2 / denominator) if denominator else 1.0
