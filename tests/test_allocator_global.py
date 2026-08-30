from __future__ import annotations

import math
from dataclasses import replace

import pytest

import shared.allocator.planner as planner_module
from shared.allocator.demand import DemandTracker
from shared.allocator.models import (
    ActionKind,
    AllocatorMode,
    ArtifactPrefetch,
    DemandForecast,
    ModelPerformance,
    ModelProfile,
    ModelResidency,
    MutationAction,
    NodeSnapshot,
    NodeState,
    PlacementAssignment,
    PlacementPlan,
    PlacementPreemption,
    ResidencyState,
    UnsatisfiedConstraint,
)
from shared.allocator.planner import (
    PlacementPlanner,
    PlannerPolicy,
    desired_replica_count,
)
from shared.allocator.reconcile import (
    MutationRecord,
    MutationStatus,
    ReconcilePolicy,
    Reconciler,
)


def node(
    node_id: str,
    capacity_mb: int = 32_000,
    *,
    state: NodeState = NodeState.ACCEPTING,
    domain: str = "",
    residencies: tuple[ModelResidency, ...] = (),
    cached: tuple[str, ...] = (),
    tiers: tuple[str, ...] = ("public", "internal"),
    tags: tuple[str, ...] = (),
    backend: str = "metal",
    runtime: str = "llama.cpp",
    now: float = 10,
    **kwargs,
) -> NodeSnapshot:
    return NodeSnapshot(
        node_id=node_id,
        capacity_mb=capacity_mb,
        backends=(backend,),
        runtimes=(runtime,),
        state=state,
        failure_domain=domain,
        residencies=residencies,
        cached_models=cached,
        allowed_data_tiers=tiers,
        tags=tags,
        last_heartbeat=now,
        **kwargs,
    )


def model(model_id: str = "qwen", memory_mb: int = 8_000, **kwargs) -> ModelProfile:
    return ModelProfile(
        model_id=model_id,
        memory_mb=memory_mb,
        runtimes=("llama.cpp",),
        backends=("metal", "cuda"),
        **kwargs,
    )


def test_portfolio_hints_filter_by_live_fleet_and_rank_the_preferred_host():
    planner = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0))
    nodes = (
        node("small", 4_000),
        node("secondary", 16_000, host_priority=2),
        node("preferred", 16_000, host_priority=1),
    )

    hints = planner.portfolio_placement_hints(
        nodes,
        (model("coder", 8_000, min_replicas=0),),
        now=10,
    )

    assert hints["coder"] == {
        "model_id": "coder",
        "feasible": True,
        "feasible_now": True,
        "hard_compatible": True,
        "feasible_after_preemption": False,
        "eligible_nodes": 2,
        "best_node_id": "preferred",
        "host_priority": 1,
        "startup_seconds": 35.0,
        "reason": "fleet-feasible",
    }


def test_portfolio_hints_explain_when_no_live_node_can_host_a_model():
    planner = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0))
    nodes = (
        node("too-small", 4_000),
        node("wrong-runtime", 16_000, runtime="comfyui", backend="mps"),
        node("no-slots", 16_000, max_models=0),
    )

    hint = planner.portfolio_placement_hints(
        nodes,
        (model("coder", 8_000, min_replicas=0),),
        now=10,
    )["coder"]

    assert hint["feasible"] is False
    assert hint["eligible_nodes"] == 0
    assert "model exceeds allocatable memory" in str(hint["reason"])
    assert "runtime is incompatible" in str(hint["reason"])
    assert "model slots are disabled" in str(hint["reason"])


def test_portfolio_hint_distinguishes_compatible_but_occupied_capacity():
    planner = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0))
    occupied = node(
        "occupied",
        16_000,
        max_models=1,
        residencies=(
            ModelResidency(
                "external-model",
                8_000,
                ResidencyState.READY,
                managed=False,
            ),
        ),
    )

    hint = planner.portfolio_placement_hints(
        (occupied,),
        (model("coder", 8_000, min_replicas=0),),
        now=10,
    )["coder"]

    assert hint["feasible"] is False
    assert hint["hard_compatible"] is True
    assert hint["feasible_after_preemption"] is False
    assert "model slots are full" in str(hint["reason"])


def test_portfolio_hint_accepts_candidate_after_safe_incumbent_relocation():
    baseline = model("baseline", 256, min_replicas=1, max_replicas=1)
    specialist = model("specialist", 714, min_replicas=0, max_replicas=1)
    machines = (
        node(
            "small",
            512,
            max_models=1,
            residencies=(
                ModelResidency("baseline", 256, ResidencyState.CACHED),
            ),
        ),
        node(
            "large",
            4_096,
            max_models=1,
            residencies=(
                ready("baseline", 256),
                ModelResidency("specialist", 714, ResidencyState.CACHED),
            ),
        ),
    )

    hint = PlacementPlanner(
        PlannerPolicy(memory_headroom_fraction=0)
    ).portfolio_placement_hints(machines, (baseline, specialist), now=10)[
        "specialist"
    ]

    assert hint["feasible_now"] is False
    assert hint["feasible_after_preemption"] is True
    assert hint["best_node_id"] == "large"
    assert hint["preemption_victims"] == ["baseline"]
    assert hint["relocation_targets"] == [
        {"model_id": "baseline", "node_id": "small"}
    ]
    assert hint["preemption_paths"] == [
        {
            "startup_seconds": hint["startup_seconds"],
            "host_priority": hint["host_priority"],
            "best_node_id": "large",
            "preemption_victims": ["baseline"],
            "relocation_targets": [
                {"model_id": "baseline", "node_id": "small"}
            ],
        }
    ]


def test_scale_down_cooldown_does_not_preserve_a_policy_ineligible_residency():
    machine = node(
        "worker",
        residencies=(
            ModelResidency(
                "coder",
                8_000,
                ResidencyState.READY,
                loaded_at=99,
                last_used_at=100,
            ),
        ),
        now=100,
    )
    candidate = model(
        "coder",
        8_000,
        min_replicas=0,
        max_replicas=1,
        min_residency_seconds=0,
        scale_down_cooldown_seconds=300,
        required_tags=("missing",),
    )

    assert desired_replica_count(candidate, None, nodes=(machine,), now=100) == 0


def test_hard_policy_change_evicts_owned_residency_despite_direct_demand():
    machine = node(
        "worker",
        residencies=(
            ModelResidency(
                "coder",
                8_000,
                ResidencyState.READY,
                loaded_at=99,
                last_used_at=100,
            ),
        ),
        now=100,
    )
    candidate = model(
        "coder",
        8_000,
        min_replicas=0,
        max_replicas=1,
        min_residency_seconds=0,
        scale_down_cooldown_seconds=300,
        required_tags=("missing",),
    )
    direct = DemandForecast(
        "coder",
        requests_per_minute=60,
        observed_requests_per_minute=60,
        offered_concurrency=1,
        updated_at=100,
    )

    plan = PlacementPlanner().plan((machine,), (candidate,), (direct,), now=100)
    result = Reconciler().reconcile(
        plan,
        (machine,),
        (candidate,),
        mode=AllocatorMode.AUTOMATIC,
        now=100,
    )

    assert plan.target_for("coder") == 1
    assert [(item.node_id, item.model_id) for item in plan.preemptions] == [
        ("worker", "coder")
    ]
    assert [item.kind for item in result.actions] == [ActionKind.DRAIN]


def test_impossible_direct_model_does_not_block_unrelated_speculative_warm():
    machine = node("worker", cached=("speculative",), now=10)
    direct = model(
        "direct",
        8_000,
        min_replicas=0,
        max_replicas=1,
        required_tags=("missing",),
    )
    speculative = model(
        "speculative",
        8_000,
        min_replicas=0,
        max_replicas=1,
    )
    forecasts = (
        DemandForecast(
            "direct",
            requests_per_minute=60,
            observed_requests_per_minute=60,
            offered_concurrency=1,
            updated_at=10,
        ),
        DemandForecast(
            "speculative",
            requests_per_minute=60,
            correlated_requests_per_minute=60,
            correlation_confidence=1,
            correlation_sources=("workload:coding",),
            updated_at=10,
        ),
    )

    plan = PlacementPlanner().plan(
        (machine,),
        (direct, speculative),
        forecasts,
        now=10,
    )
    result = Reconciler().reconcile(
        plan,
        (machine,),
        (direct, speculative),
        mode=AllocatorMode.AUTOMATIC,
        now=10,
    )

    assert plan.nodes_for("direct") == ()
    assert plan.nodes_for("speculative") == ("worker",)
    assert [item.kind for item in result.executable_actions] == [ActionKind.WARM]


def test_runtime_specific_memory_is_canonical_and_round_trips():
    profile = ModelProfile(
        model_id="qwen",
        memory_mb=8_000,
        runtime_memory_mb=(("vllm", 24_000), ("llama.cpp", 10_000)),
        runtimes=("vllm", "llama.cpp"),
    )

    assert profile.runtime_memory_mb == (("llama.cpp", 10_000), ("vllm", 24_000))
    assert profile.memory_for(("llama.cpp",)) == 10_000
    assert profile.memory_for(("vllm",)) == 24_000
    assert profile.memory_for(("unknown",)) == 8_000
    assert profile.memory_for(("llama.cpp", "vllm")) == 24_000
    assert profile.maximum_memory_mb == 24_000
    assert ModelProfile.from_dict(profile.to_dict()) == profile
    assert replace(profile, memory_mb=30_000).maximum_memory_mb == 30_000


def test_artifact_sha256_is_canonical_validated_and_round_trips():
    profile = model(artifact_sha256="A" * 64)
    residency = ModelResidency("qwen", 8_000, artifact_sha256="A" * 64)

    assert profile.artifact_sha256 == "a" * 64
    assert residency.artifact_sha256 == "a" * 64
    assert profile.matches_artifact(residency)
    assert ModelProfile.from_dict(profile.to_dict()) == profile
    assert (
        ModelResidency.from_dict(
            {
                "model_id": residency.model_id,
                "memory_mb": residency.memory_mb,
                "artifact_sha256": residency.artifact_sha256,
            }
        )
        == residency
    )

    performance = ModelPerformance("qwen", artifact_sha256="A" * 64)
    assert performance.artifact_sha256 == "a" * 64
    assert (
        ModelPerformance.from_dict({"model_id": "qwen", "artifact_sha256": "A" * 64})
        == performance
    )
    with pytest.raises(ValueError, match="artifact_sha256"):
        ModelPerformance("qwen", artifact_sha256="not-a-digest")


def test_autonomous_artifact_source_requires_immutable_identity_and_size_bound():
    with pytest.raises(ValueError, match="requires artifact_sha256"):
        model(artifact_source="hf://owner/repo/qwen.gguf")
    with pytest.raises(ValueError, match="requires artifact_sha256"):
        model(
            artifact_source="hf://owner/repo/qwen.gguf",
            artifact_size_mb=4_000,
        )

    profile = model(
        artifact_sha256="A" * 64,
        artifact_source="hf://owner/repo/qwen.gguf",
        artifact_size_mb=4_000,
    )
    restored = ModelProfile.from_dict(profile.to_dict())

    assert restored.artifact_sha256 == "a" * 64
    assert restored.artifact_source == "hf://owner/repo/qwen.gguf"
    assert restored.artifact_size_mb == 4_000


def test_node_disk_telemetry_validates_and_round_trips():
    snapshot = node(
        "disk-node",
        disk_capacity_mb=100_000,
        disk_available_mb=25_000,
    )

    assert NodeSnapshot.from_dict(snapshot.to_dict()) == snapshot
    with pytest.raises(ValueError, match="cannot exceed"):
        node(
            "invalid-disk",
            disk_capacity_mb=10,
            disk_available_mb=11,
        )


@pytest.mark.parametrize("digest", ["short", "g" * 64, 1, True])
def test_artifact_sha256_rejects_invalid_values(digest):
    with pytest.raises(ValueError, match="SHA-256"):
        model(artifact_sha256=digest)


def test_plan_rejects_a_residency_that_is_both_desired_and_preempted():
    plan = PlacementPlanner().plan((node("n"),), (model(),), now=10)

    with pytest.raises(ValueError, match="both desired and preempted"):
        replace(
            plan,
            preemptions=(PlacementPreemption("n", "qwen", "critical"),),
        )


def test_plan_rejects_predictive_prefetch_for_a_preemption_beneficiary():
    plan = PlacementPlanner().plan((node("n"),), (model(),), now=10)

    with pytest.raises(ValueError, match="preemption beneficiary"):
        replace(
            plan,
            assignments=(),
            preemptions=(PlacementPreemption("n", "qwen", "critical"),),
            artifact_prefetches=(ArtifactPrefetch("n", "critical"),),
        )


@pytest.mark.parametrize(
    "urgencies",
    [
        (("qwen", 4),),
        (("qwen", True),),
        (("qwen", 1), ("qwen", 2)),
    ],
)
def test_plan_rejects_invalid_model_urgencies(urgencies):
    plan = PlacementPlanner().plan((node("n"),), (model(),), now=10)

    with pytest.raises(ValueError, match="model urgencies"):
        replace(plan, model_urgencies=urgencies)


def test_plan_canonicalizes_model_urgencies():
    plan = PlacementPlanner().plan((node("n"),), (model(),), now=10)

    canonical = replace(plan, model_urgencies=(("z", 1), ("a", 3)))

    assert canonical.model_urgencies == (("a", 3), ("z", 1))
    assert canonical.to_dict()["model_urgencies"] == {"a": 3, "z": 1}


@pytest.mark.parametrize("value", [-1, True, 1.5])
def test_max_colocated_models_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="max_colocated_models"):
        model(max_colocated_models=value)


def test_pairwise_colocation_exclusions_are_reciprocal_and_selective():
    alpha = model("alpha", colocation_excludes=("beta",))
    beta = model("beta")
    compatible = model("compatible")
    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        (node("one", 16_000), node("two", 16_000)),
        (beta, compatible, alpha),
        now=10,
    )

    assert plan.unsatisfied == ()
    assert len(plan.assignments) == 3
    assert not any(
        {"alpha", "beta"}.issubset(plan.models_for(node_id))
        for node_id in ("one", "two")
    )
    assert any(
        "compatible" in plan.models_for(node_id) and len(plan.models_for(node_id)) == 2
        for node_id in ("one", "two")
    )
    assert ModelProfile.from_dict(alpha.to_dict()) == alpha


def test_pairwise_colocation_exclusions_reject_self_reference():
    with pytest.raises(ValueError, match="profile model"):
        model("alpha", colocation_excludes=("alpha",))


@pytest.mark.parametrize(
    ("exclusive_priority", "shared_priority"),
    [(200, 100), (100, 200)],
)
def test_exclusive_model_never_shares_host_regardless_of_placement_order(
    exclusive_priority,
    shared_priority,
):
    exclusive = model(
        "latency",
        priority=exclusive_priority,
        max_colocated_models=1,
    )
    shared = model("batch", priority=shared_priority)

    plan = PlacementPlanner().plan(
        [node("a"), node("b")],
        [exclusive, shared],
        now=10,
    )

    assert plan.unsatisfied == ()
    assert plan.nodes_for("latency") != plan.nodes_for("batch")


def test_exclusive_model_rejects_live_peer_but_ignores_cached_weights():
    exclusive = model("latency", max_colocated_models=1)
    live_peer = ModelResidency("batch", 8_000, ResidencyState.READY)
    cached_peer = replace(live_peer, state=ResidencyState.CACHED)

    blocked = PlacementPlanner().plan(
        [node("host", residencies=(live_peer,))],
        [exclusive],
        now=10,
    )
    allowed = PlacementPlanner().plan(
        [node("host", residencies=(cached_peer,))],
        [exclusive],
        now=10,
    )

    assert blocked.nodes_for("latency") == ()
    assert blocked.unsatisfied[0].code == "colocation_limit"
    assert allowed.nodes_for("latency") == ("host",)


def test_colocation_policy_never_actuates_external_vllm_inventory():
    profile = ModelProfile(
        model_id="qwen",
        memory_mb=8_000,
        runtimes=("vllm",),
        backends=("cuda",),
        max_colocated_models=1,
    )
    external = node(
        "external-vllm",
        runtime="vllm",
        backend="cuda",
        manually_managed=True,
        actuator_capabilities=(),
        residencies=(
            ready("qwen", managed=False),
            ready("other", managed=False),
        ),
    )

    plan = PlacementPlanner().plan([external], [profile], now=10)
    result = Reconciler().reconcile(plan, [external], [profile], now=10)

    assert plan.nodes_for("qwen") == ()
    assert result.actions == ()
    assert all(item.code == "not_allocator_owned" for item in result.deferred)


@pytest.mark.parametrize(
    ("exclusive_priority", "batch_priority", "victim", "beneficiary"),
    [
        (100, 100, "batch", "latency"),
        (100, 200, "latency", "batch"),
    ],
)
def test_managed_colocation_violation_stages_deterministic_convergence(
    exclusive_priority,
    batch_priority,
    victim,
    beneficiary,
):
    exclusive = model(
        "latency",
        priority=exclusive_priority,
        max_colocated_models=1,
        min_residency_seconds=0,
    )
    batch = model(
        "batch",
        priority=batch_priority,
        min_residency_seconds=0,
    )
    machine = node(
        "managed",
        residencies=(ready("latency"), ready("batch")),
    )
    planner = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0))

    staged = planner.plan((machine,), (exclusive, batch), now=10)

    assert staged.assignments == ()
    assert [
        (item.node_id, item.model_id, item.for_model_id) for item in staged.preemptions
    ] == [("managed", victim, beneficiary)]
    result = Reconciler().reconcile(
        staged,
        (machine,),
        (exclusive, batch),
        mode=AllocatorMode.AUTOMATIC,
        now=10,
    )
    assert [(item.kind, item.model_id) for item in result.executable_actions] == [
        (ActionKind.DRAIN, victim)
    ]

    converged_node = replace(
        machine,
        residencies=tuple(
            item for item in machine.residencies if item.model_id != victim
        ),
    )
    converged = planner.plan((converged_node,), (exclusive, batch), now=11)
    assert converged.nodes_for(beneficiary) == ("managed",)
    assert converged.preemptions == ()


def test_managed_pairwise_colocation_violation_converges_but_external_does_not():
    alpha = model(
        "alpha",
        colocation_excludes=("beta",),
        min_residency_seconds=0,
    )
    beta = model("beta", min_residency_seconds=0)
    planner = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0))
    managed = node("managed", residencies=(ready("alpha"), ready("beta")))

    staged = planner.plan((managed,), (alpha, beta), now=10)
    assert [(item.model_id, item.for_model_id) for item in staged.preemptions] == [
        ("beta", "alpha")
    ]
    result = Reconciler().reconcile(
        staged,
        (managed,),
        (alpha, beta),
        mode=AllocatorMode.AUTOMATIC,
        now=10,
    )
    assert [item.model_id for item in result.executable_actions] == ["beta"]

    external = replace(
        managed,
        node_id="external-vllm",
        residencies=(ready("alpha", managed=False), ready("beta", managed=False)),
        manually_managed=True,
        actuator_capabilities=(),
    )
    external_plan = planner.plan((external,), (alpha, beta), now=10)
    external_result = Reconciler().reconcile(
        external_plan,
        (external,),
        (alpha, beta),
        mode=AllocatorMode.AUTOMATIC,
        now=10,
    )
    assert external_plan.preemptions == ()
    assert external_result.actions == ()


@pytest.mark.parametrize(
    "runtime_memory_mb",
    [
        (("vllm", 0),),
        (("vllm", 8_000), ("vllm", 9_000)),
        (("", 8_000),),
        (("vllm",),),
        (1,),
        (("vllm", "many"),),
        (("vllm", 8_000),),
    ],
)
def test_runtime_specific_memory_rejects_invalid_entries(runtime_memory_mb):
    with pytest.raises(ValueError, match="runtime_memory_mb"):
        ModelProfile(
            model_id="qwen",
            memory_mb=8_000,
            runtime_memory_mb=runtime_memory_mb,
            runtimes=("llama.cpp",),
        )


def test_runtime_specific_memory_controls_placement_and_assignment_size():
    profile = ModelProfile(
        model_id="qwen",
        memory_mb=8_000,
        runtime_memory_mb=(("llama.cpp", 10_000), ("vllm", 24_000)),
        runtimes=("llama.cpp", "vllm"),
        min_replicas=1,
        max_replicas=1,
    )
    llama = node("llama", capacity_mb=16_000, runtime="llama.cpp")
    vllm = node("vllm", capacity_mb=16_000, runtime="vllm")

    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        (llama, vllm),
        (profile,),
        now=10,
    )

    assert plan.nodes_for("qwen") == ("llama",)
    assert plan.assignments[0].memory_mb == 10_000


def test_runtime_specific_assignment_memory_reaches_warm_action():
    profile = ModelProfile(
        model_id="qwen",
        memory_mb=8_000,
        runtime_memory_mb=(("vllm", 24_000),),
        runtimes=("vllm",),
    )
    machine = node("vllm", capacity_mb=32_000, runtime="vllm", cached=("qwen",))
    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        (machine,),
        (profile,),
        now=10,
    )

    result = Reconciler().reconcile(
        plan,
        (machine,),
        (profile,),
        mode=AllocatorMode.RECOMMEND,
        now=10,
    )

    assert len(result.actions) == 1
    assert result.actions[0].kind == ActionKind.WARM
    assert result.actions[0].memory_mb == 24_000


def test_gpu_topology_constraints_distinguish_device_count_and_per_device_vram():
    profile = ModelProfile(
        model_id="qwen",
        memory_mb=64_000,
        runtimes=("vllm",),
        backends=("cuda",),
        min_gpu_count=2,
        min_gpu_memory_mb=48_000,
    )
    two_blackwell = node(
        "two-blackwell",
        capacity_mb=192_000,
        runtime="vllm",
        backend="cuda",
        gpu_count=2,
        gpu_memory_mb=(96_000, 96_000),
    )
    eight_5090 = node(
        "eight-5090",
        capacity_mb=192_000,
        runtime="vllm",
        backend="cuda",
        gpu_count=8,
        gpu_memory_mb=(24_000,) * 8,
    )
    unknown_topology = node(
        "unknown",
        capacity_mb=192_000,
        runtime="vllm",
        backend="cuda",
    )

    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        (eight_5090, unknown_topology, two_blackwell),
        (profile,),
        now=10,
    )

    assert plan.nodes_for("qwen") == ("two-blackwell",)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"gpu_count": -1}, "gpu_count"),
        ({"gpu_count": True}, "gpu_count"),
        ({"gpu_memory_mb": (0,)}, "gpu_memory_mb"),
        ({"gpu_memory_mb": (float("inf"),)}, "gpu_memory_mb"),
    ],
)
def test_gpu_topology_rejects_malformed_node_values(updates, message):
    with pytest.raises(ValueError, match=message):
        node("bad", **updates)


@pytest.mark.parametrize(
    "updates",
    [
        {"min_gpu_count": -1},
        {"min_gpu_count": True},
        {"min_gpu_memory_mb": -1},
        {"min_gpu_memory_mb": True},
    ],
)
def test_gpu_topology_rejects_malformed_profile_values(updates):
    with pytest.raises(ValueError, match="min_gpu"):
        model(**updates)


def test_cold_unmeasured_placement_prefers_faster_hardware_without_repacking_cache():
    profile = replace(model(), backends=())
    intel = node(
        "intel",
        backend="cpu",
        memory_bandwidth_gbps=50,
        compute_gflops=1_000,
    )
    m2 = node(
        "m2",
        memory_bandwidth_gbps=400,
        compute_gflops=27_132,
    )

    cold = PlacementPlanner().plan((intel, m2), (profile,), now=10)
    assert cold.assignments[0].node_id == "m2"
    assert "hardware performance estimate" in cold.assignments[0].reasons

    cached_intel = replace(intel, cached_models=(profile.model_id,))
    stable = PlacementPlanner().plan((cached_intel, m2), (profile,), now=10)
    assert stable.assignments[0].node_id == "intel"
    assert "weights cached locally" in stable.assignments[0].reasons


def test_hot_model_amortizes_cold_start_onto_materially_faster_host():
    profile = replace(model(), backends=(), replica_concurrency=1)
    cached_slow = node(
        "cached-slow",
        backend="cpu",
        cached=(profile.model_id,),
        memory_bandwidth_gbps=50,
        compute_gflops=1_000,
    )
    cold_fast = node(
        "cold-fast",
        memory_bandwidth_gbps=400,
        compute_gflops=27_132,
    )
    planner = PlacementPlanner()

    light = planner.plan(
        (cached_slow, cold_fast),
        (profile,),
        (
            DemandForecast(
                profile.model_id,
                requests_per_minute=1,
                observed_requests_per_minute=1,
                offered_concurrency=0.5,
                sample_count=1,
                updated_at=10,
            ),
        ),
        now=10,
    )
    hot = planner.plan(
        (cached_slow, cold_fast),
        (profile,),
        (
            DemandForecast(
                profile.model_id,
                requests_per_minute=10,
                observed_requests_per_minute=10,
                offered_concurrency=6,
                sample_count=10,
                updated_at=10,
            ),
        ),
        now=10,
    )

    assert light.nodes_for(profile.model_id) == ("cached-slow",)
    assert hot.nodes_for(profile.model_id) == ("cold-fast",)
    assert "performance value amortized by demand" in hot.assignments[0].reasons


def test_failed_cached_target_yields_to_healthy_peer_but_remains_a_fallback():
    profile = model(min_residency_seconds=0)
    failed = node(
        "failed",
        cached=(profile.model_id,),
        residencies=(
            replace(
                ready(),
                state=ResidencyState.FAILED,
                load_failures=1,
            ),
        ),
    )
    healthy = node("healthy", cached=(profile.model_id,))
    planner = PlacementPlanner()

    preferred = planner.plan((failed, healthy), (profile,), now=10)
    assert preferred.nodes_for(profile.model_id) == ("healthy",)
    assert "prior model failures" not in preferred.assignments[0].reasons

    fallback = planner.plan((failed,), (profile,), now=10)
    assert fallback.nodes_for(profile.model_id) == ("failed",)
    assert "prior model failures" in fallback.assignments[0].reasons


def test_measured_engine_performance_supersedes_hardware_prior_for_cold_placement():
    profile = replace(model(), backends=())
    high_spec_slow = node(
        "high-spec-slow",
        tokens_per_second=10,
        latency_ms=1_000,
        memory_bandwidth_gbps=800,
        compute_gflops=100_000,
    )
    measured_fast = node(
        "measured-fast",
        tokens_per_second=100,
        latency_ms=100,
        memory_bandwidth_gbps=50,
        compute_gflops=1_000,
    )

    plan = PlacementPlanner().plan(
        (high_spec_slow, measured_fast),
        (profile,),
        now=10,
    )

    assert plan.assignments[0].node_id == "measured-fast"
    assert "measured throughput" in plan.assignments[0].reasons
    assert "hardware performance estimate" not in plan.assignments[0].reasons


def test_multi_model_engine_uses_only_per_model_performance_for_placement():
    profile = model()
    misattributed = node(
        "multi-model",
        residencies=(ready(), ready("other")),
        tokens_per_second=1_000,
        latency_ms=10,
    )
    measured_qwen = node(
        "measured-qwen",
        residencies=(ready(),),
        tokens_per_second=200,
        latency_ms=100,
    )
    planner = PlacementPlanner()

    safe = planner.plan((misattributed, measured_qwen), (profile,), now=10)
    assert safe.nodes_for(profile.model_id) == ("measured-qwen",)

    attributed = replace(
        misattributed,
        model_performance=(
            ModelPerformance(
                profile.model_id,
                tokens_per_second=300,
                latency_ms=50,
                sample_count=8,
                throughput_sample_count=8,
                updated_at=10,
            ),
        ),
    )
    specific = planner.plan((attributed, measured_qwen), (profile,), now=10)
    assert specific.nodes_for(profile.model_id) == ("multi-model",)
    assert "measured throughput" in specific.assignments[0].reasons

    stale = replace(
        attributed,
        model_performance=(replace(attributed.model_performance[0], updated_at=1),),
    )
    expired = PlacementPlanner(
        PlannerPolicy(performance_ttl_seconds=5),
    ).plan((stale, measured_qwen), (profile,), now=10)
    assert expired.nodes_for(profile.model_id) == ("measured-qwen",)

    fresh = replace(
        attributed,
        model_performance=(replace(attributed.model_performance[0], updated_at=9),),
    )
    refreshed = PlacementPlanner(
        PlannerPolicy(performance_ttl_seconds=5),
    ).plan((fresh, measured_qwen), (profile,), now=10)
    assert refreshed.nodes_for(profile.model_id) == ("multi-model",)


def test_placement_uses_performance_only_from_current_artifact_revision():
    digest = "a" * 64
    previous = "b" * 64
    profile = model(artifact_sha256=digest)
    stale_fast = node(
        "stale-fast",
        residencies=(ready(artifact_sha256=digest), ready("other")),
        model_performance=(
            ModelPerformance(
                profile.model_id,
                tokens_per_second=1_000,
                latency_ms=10,
                sample_count=8,
                throughput_sample_count=8,
                updated_at=100,
                artifact_sha256=previous,
            ),
        ),
        tokens_per_second=2_000,
        latency_ms=1,
        now=100,
    )
    current = node(
        "current",
        residencies=(ready(artifact_sha256=digest), ready("other")),
        model_performance=(
            ModelPerformance(
                profile.model_id,
                tokens_per_second=200,
                latency_ms=100,
                sample_count=8,
                throughput_sample_count=8,
                updated_at=100,
                artifact_sha256=digest,
            ),
        ),
        now=100,
    )

    plan = PlacementPlanner().plan((stale_fast, current), (profile,), now=100)

    assert plan.nodes_for(profile.model_id) == ("current",)
    assert "measured throughput" in plan.assignments[0].reasons


def test_per_model_performance_weights_sample_confidence_over_one_outlier():
    profile = model()
    noisy = node(
        "noisy",
        residencies=(ready(), ready("other")),
        model_performance=(
            ModelPerformance(
                profile.model_id,
                tokens_per_second=1_000,
                latency_ms=10,
                sample_count=1,
                throughput_sample_count=1,
                updated_at=100,
            ),
        ),
        now=100,
    )
    mature = node(
        "mature",
        residencies=(ready(), ready("other")),
        model_performance=(
            ModelPerformance(
                profile.model_id,
                tokens_per_second=200,
                latency_ms=100,
                sample_count=8,
                throughput_sample_count=8,
                updated_at=100,
            ),
        ),
        now=100,
    )

    plan = PlacementPlanner().plan((noisy, mature), (profile,), now=100)

    assert plan.nodes_for(profile.model_id) == ("mature",)

    cold_prior = replace(
        noisy,
        memory_bandwidth_gbps=400,
        compute_gflops=27_132,
    )
    blended = PlacementPlanner().plan((cold_prior,), (profile,), now=100)
    assert set(blended.assignments[0].reasons) >= {
        "measured throughput",
        "hardware performance estimate",
    }
    trusted = replace(
        cold_prior,
        model_performance=(
            replace(
                cold_prior.model_performance[0],
                sample_count=8,
                throughput_sample_count=8,
            ),
        ),
    )
    mature_plan = PlacementPlanner().plan((trusted,), (profile,), now=100)
    assert "hardware performance estimate" not in mature_plan.assignments[0].reasons


def test_per_model_performance_decays_smoothly_before_expiry():
    profile = model()

    def measured(node_id: str, updated_at: float) -> NodeSnapshot:
        return node(
            node_id,
            residencies=(ready(), ready("other")),
            model_performance=(
                ModelPerformance(
                    profile.model_id,
                    tokens_per_second=200,
                    latency_ms=100,
                    sample_count=8,
                    throughput_sample_count=8,
                    updated_at=updated_at,
                ),
            ),
            now=100,
        )

    fresh = measured("fresh", 99)
    aging = measured("aging", 25)
    plan = PlacementPlanner(PlannerPolicy(performance_ttl_seconds=100)).plan(
        (aging, fresh),
        (profile,),
        now=100,
    )

    assert plan.nodes_for(profile.model_id) == ("fresh",)

    expired = measured("expired", 1)
    boundary = PlacementPlanner(PlannerPolicy(performance_ttl_seconds=99)).plan(
        (expired,),
        (profile,),
        now=100,
    )
    assert "measured throughput" not in boundary.assignments[0].reasons


def test_fresh_latency_does_not_keep_stale_throughput_in_placement():
    profile = model()
    machine = node(
        "stale-throughput",
        residencies=(ready(), ready("other")),
        model_performance=(
            ModelPerformance(
                profile.model_id,
                tokens_per_second=1_000,
                latency_ms=100,
                sample_count=8,
                throughput_sample_count=8,
                updated_at=100,
                throughput_updated_at=1,
            ),
        ),
        memory_bandwidth_gbps=400,
        now=100,
    )

    assignment = (
        PlacementPlanner(
            PlannerPolicy(performance_ttl_seconds=50),
        )
        .plan((machine,), (profile,), now=100)
        .assignments[0]
    )

    assert "measured throughput" not in assignment.reasons
    assert "hardware performance estimate" in assignment.reasons


def ready(
    model_id: str = "qwen",
    memory_mb: int = 8_000,
    *,
    loaded_at: float = 1,
    last_used_at: float = 1,
    **kwargs,
) -> ModelResidency:
    return ModelResidency(
        model_id,
        memory_mb,
        ResidencyState.READY,
        loaded_at,
        last_used_at,
        **kwargs,
    )


def test_current_eight_node_framework_inventory_is_usable_but_never_actuated():
    """Mirror the live fleet: its external-discovery NVIDIA engines are known vLLM."""

    inventory = (
        ("firmware-engineer-daniel", "llama.cpp", "metal", ("Qwen3.6-35B-A3B",)),
        ("video-editor-tom", "llama.cpp", "metal", ("Gemma-4-31B-it",)),
        ("3d-artist-diego", "llama.cpp", "metal", ("Gemma-4-31B-it",)),
        ("ml-engineer-priya", "llama.cpp", "metal", ("GLM-4.7-Flash",)),
        (
            "mac-studio-turtle",
            "comfyui",
            "mps",
            ("comfyui:krea2", "comfyui:z_image"),
        ),
        ("scholes-60002-01", "vllm", "cuda", ("Qwen3.8-Flash-Next",)),
        ("scholes-60001", "vllm", "cuda", ("DeepSeek-V4-Flash",)),
        ("8x50902-67-qwen38-27b", "vllm", "cuda", ("Qwen3.8-27B",)),
    )
    machines = tuple(
        node(
            node_id,
            capacity_mb=196_608 if backend in ("metal", "mps") else 768_000,
            runtime=runtime,
            backend=backend,
            domain=node_id,
            residencies=tuple(
                ready(model_id, 32_000, managed=False) for model_id in models
            ),
            manually_managed=True,
            actuator_capabilities=(),
        )
        for node_id, runtime, backend, models in inventory
    )
    expected = {
        model_id: tuple(
            node_id
            for node_id, _runtime, _backend, models in inventory
            if model_id in models
        )
        for _node_id, _runtime, _backend, models in inventory
        for model_id in models
    }
    profiles = tuple(
        ModelProfile(
            model_id=model_id,
            memory_mb=32_000,
            runtimes=(
                next(
                    runtime
                    for _node_id, runtime, _backend, models in inventory
                    if model_id in models
                ),
            ),
            min_replicas=len(node_ids),
            max_replicas=len(node_ids),
            min_residency_seconds=0,
        )
        for model_id, node_ids in expected.items()
    )

    plan = PlacementPlanner().plan(machines, profiles, now=10)

    assert plan.unsatisfied == ()
    assert {
        profile.model_id: plan.nodes_for(profile.model_id) for profile in profiles
    } == {model_id: tuple(sorted(node_ids)) for model_id, node_ids in expected.items()}
    result = Reconciler().reconcile(
        plan,
        machines,
        profiles,
        mode=AllocatorMode.AUTOMATIC,
        now=10,
    )
    assert result.actions == ()


def test_new_text_model_can_only_land_on_explicitly_owned_llama_nodes():
    owned_llama = tuple(
        node(
            f"owned-llama-{index}",
            runtime="llama.cpp",
            backend="metal",
            domain=f"logical-{index}",
        )
        for index in range(4)
    )
    immutable_other_frameworks = (
        node(
            "comfyui-mps",
            runtime="comfyui",
            backend="mps",
            manually_managed=True,
            actuator_capabilities=(),
        ),
        *(
            node(
                f"external-{index}",
                runtime="vllm",
                backend="cuda",
                manually_managed=True,
                actuator_capabilities=(),
            )
            for index in range(3)
        ),
    )
    profile = model(
        "new-text-model",
        memory_mb=4_000,
        min_replicas=4,
        max_replicas=4,
        min_failure_domains=4,
    )

    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        (*owned_llama, *immutable_other_frameworks),
        (profile,),
        now=10,
    )

    assert plan.nodes_for(profile.model_id) == tuple(
        sorted(item.node_id for item in owned_llama)
    )
    assert plan.unsatisfied == ()


def test_allocator_models_validate_impossible_values():
    with pytest.raises(ValueError, match="memory_mb"):
        model(memory_mb=0)
    with pytest.raises(ValueError, match="reserved_mb"):
        node("n", 10, reserved_mb=11)
    with pytest.raises(ValueError, match="replica bounds"):
        model(min_replicas=2, max_replicas=1)
    with pytest.raises(ValueError, match="replica_concurrency"):
        model(replica_concurrency=0)
    with pytest.raises(ValueError, match="replica_concurrency"):
        model(replica_concurrency=True)
    with pytest.raises(ValueError, match="pinned_nodes"):
        model(pinned_nodes=("a", "b"), max_replicas=1)
    with pytest.raises(ValueError, match="finite"):
        DemandForecast("m", requests_per_minute=math.inf)
    with pytest.raises(ValueError, match="active_requests"):
        ready(active_requests=-1)
    with pytest.raises(ValueError, match="non-negative"):
        PlannerPolicy(performance_ttl_seconds=-1)
    with pytest.raises(ValueError, match="performance_full_confidence_samples"):
        PlannerPolicy(performance_full_confidence_samples=0)
    with pytest.raises(ValueError, match="performance_full_confidence_samples"):
        PlannerPolicy(performance_full_confidence_samples=True)
    with pytest.raises(ValueError, match="max_staged_preemptions"):
        PlannerPolicy(max_staged_preemptions=0)
    with pytest.raises(ValueError, match="max_staged_preemptions"):
        PlannerPolicy(max_staged_preemptions=True)
    with pytest.raises(ValueError, match="max_predictive_artifact_prefetches"):
        PlannerPolicy(max_predictive_artifact_prefetches=-1)
    with pytest.raises(ValueError, match="max_predictive_artifact_prefetches"):
        PlannerPolicy(max_predictive_artifact_prefetches=True)
    with pytest.raises(ValueError, match="non-negative"):
        PlannerPolicy(max_predictive_lookahead_seconds=-1)
    with pytest.raises(ValueError, match="predictive_growth_limit"):
        PlannerPolicy(predictive_growth_limit=0.9)
    with pytest.raises(ValueError, match="updated_at"):
        ModelPerformance("qwen", updated_at=-1)
    with pytest.raises(ValueError, match="throughput_sample_count"):
        ModelPerformance("qwen", throughput_sample_count=-1)


def test_node_snapshot_round_trip_preserves_residency_and_enum():
    original = node(
        "n",
        residencies=(ready(managed=False, pinned=True, active_requests=2),),
        cached=("other",),
        state=NodeState.THROTTLED,
        model_performance=(
            ModelPerformance(
                "qwen",
                tokens_per_second=12.5,
                latency_ms=250,
                sample_count=4,
                throughput_sample_count=4,
                updated_at=9,
                artifact_sha256="a" * 64,
            ),
        ),
        memory_bandwidth_gbps=400,
        compute_gflops=27_132,
    )
    restored = NodeSnapshot.from_dict(original.to_dict())
    assert restored == original
    assert restored.residency("qwen").managed is False
    assert restored.residency("qwen").active_requests == 2
    assert restored.performance("qwen").tokens_per_second == 12.5
    assert restored.performance("qwen").throughput_sample_count == 4
    assert restored.performance("qwen").throughput_updated_at == 9
    assert restored.performance("qwen").updated_at == 9
    assert restored.performance("qwen").artifact_sha256 == "a" * 64

    legacy_performance = original.to_dict()
    legacy_performance["model_performance"][0].pop("throughput_sample_count")
    legacy_performance["model_performance"][0].pop("throughput_updated_at")
    restored_performance = NodeSnapshot.from_dict(legacy_performance).performance(
        "qwen"
    )
    assert restored_performance.throughput_sample_count == 4
    assert restored_performance.throughput_updated_at == 9

    legacy = original.to_dict()
    legacy["residencies"][0].pop("active_requests")
    legacy.pop("memory_bandwidth_gbps")
    legacy.pop("compute_gflops")
    legacy.pop("model_performance")
    restored_legacy = NodeSnapshot.from_dict(legacy)
    assert restored_legacy.residency("qwen").active_requests == 0
    assert restored_legacy.memory_bandwidth_gbps == 0
    assert restored_legacy.compute_gflops == 0
    assert restored_legacy.model_performance == ()


def test_node_snapshot_rejects_duplicate_model_residencies():
    with pytest.raises(ValueError, match="duplicate"):
        node("n", residencies=(ready(), ready()))


def test_demand_tracker_has_zero_cold_start_forecast():
    forecast = DemandTracker().forecast("qwen", now=100)
    assert forecast.requests_per_minute == 0
    assert forecast.offered_concurrency == 0
    assert forecast.confidence == 0
    assert forecast.updated_at == 0


def test_expired_demand_does_not_refresh_its_own_recency_watermark():
    tracker = DemandTracker(window_seconds=10, bucket_seconds=5)
    tracker.observe("qwen", timestamp=1, service_seconds=1)

    forecast = tracker.forecast("qwen", now=100)

    assert forecast.sample_count == 0
    assert forecast.updated_at == 0
    assert (
        desired_replica_count(
            model(min_replicas=0, max_replicas=1, scale_down_cooldown_seconds=60),
            forecast,
            now=100,
        )
        == 0
    )


def test_demand_tracker_estimates_rate_concurrency_queue_and_p95():
    tracker = DemandTracker(bucket_seconds=60, window_seconds=600, ewma_alpha=1)
    for offset, latency in enumerate((100, 200, 300, 4_000)):
        tracker.observe(
            "qwen",
            requests=3,
            service_seconds=2,
            latency_ms=latency,
            queue_depth=2 if offset == 3 else 0,
            timestamp=60 + offset * 15,
        )
    forecast = tracker.forecast("qwen", now=119)
    assert forecast.requests_per_minute == 12
    assert forecast.offered_concurrency == pytest.approx(2.4)
    assert forecast.queue_depth == 2
    assert forecast.p95_latency_ms == pytest.approx(4_000, rel=0.05)


def test_demand_tracker_accepts_out_of_order_completion_and_clock_regression():
    tracker = DemandTracker(window_seconds=100, bucket_seconds=10)
    tracker.observe("m", timestamp=100, service_seconds=1)
    tracker.observe("m", timestamp=90, service_seconds=2)
    assert tracker.forecast("m", now=80).sample_count == 2
    tracker.observe("m", timestamp=250, service_seconds=1)
    # A small rollback retains the model-local watermark. A jump beyond the complete retention
    # window is instead treated as a bad future timestamp so it cannot pin demand indefinitely.
    assert tracker.forecast("m", now=1).sample_count == 0


def test_demand_tracker_bounds_history_and_round_trips():
    tracker = DemandTracker(
        window_seconds=1_000, bucket_seconds=10, max_samples_per_model=3
    )
    for timestamp in range(10):
        tracker.observe("m", timestamp=timestamp, service_seconds=1)
    # Requests in one time bucket are compacted without losing their aggregate demand.
    assert tracker.forecast("m", now=9).sample_count == 10
    restored = DemandTracker.from_dict(tracker.to_dict())
    assert restored.to_dict() == tracker.to_dict()


def test_demand_tracker_model_ids_are_sorted_and_do_not_expose_history():
    tracker = DemandTracker()
    tracker.observe("z", timestamp=1)
    tracker.observe("a", timestamp=1)

    keys = tracker.model_ids()
    tracker.clear("a")

    assert keys == ("a", "z")
    assert tracker.model_ids() == ("z",)


def test_clock_skewed_model_does_not_suppress_another_models_fresh_demand():
    tracker = DemandTracker(window_seconds=100, bucket_seconds=10)
    tracker.observe("healthy", requests=10, timestamp=100, service_seconds=1)
    tracker.observe("skewed", timestamp=10_000, service_seconds=1)
    tracker.observe("healthy", requests=100, timestamp=101, service_seconds=1)

    forecast = tracker.forecast("healthy", now=101)

    assert forecast.sample_count == 110
    assert forecast.requests_per_minute > 0
    assert forecast.updated_at == 101


def test_per_model_clock_watermarks_round_trip_and_read_legacy_state():
    tracker = DemandTracker(window_seconds=100, bucket_seconds=10)
    tracker.observe("healthy", timestamp=100, service_seconds=1)
    tracker.observe("skewed", timestamp=10_000, service_seconds=1)
    serialized = tracker.to_dict()

    restored = DemandTracker.from_dict(serialized)
    restored.observe("healthy", requests=100, timestamp=101, service_seconds=1)
    restored.observe("skewed", requests=100, timestamp=101, service_seconds=1)

    assert restored.forecast("healthy", now=101).sample_count == 101
    # The implausible future sample is discarded and corrected traffic is accepted immediately.
    assert restored.forecast("skewed", now=101).sample_count == 100
    assert DemandTracker.from_dict(serialized).to_dict() == serialized

    legacy = dict(serialized)
    legacy.pop("model_high_watermarks")
    restored_legacy = DemandTracker.from_dict(legacy)
    restored_legacy.observe("healthy", requests=100, timestamp=101, service_seconds=1)
    assert restored_legacy.forecast("healthy", now=101).sample_count == 101


def test_future_timestamp_cannot_pin_one_models_capacity_until_wall_clock_catches_up():
    tracker = DemandTracker(
        window_seconds=100,
        bucket_seconds=10,
        ewma_alpha=1,
        max_future_skew_seconds=30,
    )
    tracker.observe("m", requests=500, timestamp=10_000, service_seconds=1)

    expired = tracker.forecast("m", now=100)

    assert expired.sample_count == 0
    assert expired.updated_at == 0
    assert expired.requests_per_minute == 0

    tracker.observe("m", requests=3, timestamp=101, service_seconds=1)
    corrected = tracker.forecast("m", now=101)
    assert corrected.sample_count == 3
    assert corrected.updated_at == 101
    assert tracker.to_dict()["model_high_watermarks"] == {"m": 101}


def test_future_skew_configuration_round_trips_and_watermarks_are_validated():
    tracker = DemandTracker(max_future_skew_seconds=17)
    tracker.observe("m", timestamp=10)
    serialized = tracker.to_dict()

    assert DemandTracker.from_dict(serialized).max_future_skew_seconds == 17

    invalid_map = dict(serialized)
    invalid_map["model_high_watermarks"] = []
    with pytest.raises(ValueError, match="watermarks must be an object"):
        DemandTracker.from_dict(invalid_map)

    invalid_value = dict(serialized)
    invalid_value["model_high_watermarks"] = {"m": float("inf")}
    with pytest.raises(ValueError, match="watermark is invalid"):
        DemandTracker.from_dict(invalid_value)


def test_clearing_a_clock_skewed_model_removes_its_watermark():
    tracker = DemandTracker(window_seconds=100, bucket_seconds=10)
    tracker.observe("healthy", timestamp=100, service_seconds=1)
    tracker.observe("skewed", timestamp=10_000, service_seconds=1)

    tracker.clear("skewed")

    assert tracker.forecast("healthy", now=100).sample_count == 1
    assert tracker.to_dict()["high_watermark"] == 100
    assert tracker.to_dict()["model_high_watermarks"] == {"healthy": 100}


def test_demand_tracker_rejects_invalid_samples_and_schema():
    tracker = DemandTracker()
    with pytest.raises(ValueError, match="errors"):
        tracker.observe("m", requests=1, errors=2)
    with pytest.raises(ValueError, match="unsupported"):
        DemandTracker.from_dict({"schema_version": 99})
    with pytest.raises(ValueError, match="observed_requests_per_minute"):
        DemandForecast(
            "m",
            requests_per_minute=1,
            observed_requests_per_minute=2,
        )
    with pytest.raises(ValueError, match="correlated_requests_per_minute"):
        DemandForecast(
            "m",
            requests_per_minute=1,
            correlated_requests_per_minute=2,
        )


def test_demand_tracker_prewarms_quiet_models_from_mature_group_demand():
    tracker = DemandTracker(
        bucket_seconds=10,
        window_seconds=200,
        ewma_alpha=1,
        confidence_samples=20,
    )
    for timestamp in (1, 11, 21, 31):
        tracker.observe(
            "source",
            requests=20,
            service_seconds=1,
            timestamp=timestamp,
        )
        tracker.observe(
            "target",
            requests=10,
            service_seconds=10,
            latency_ms=20_000,
            errors=2,
            timestamp=timestamp,
        )
    tracker.observe("source", requests=20, service_seconds=1, timestamp=41)

    independent = tracker.forecast("target", now=41)
    forecasts = {item.model_id: item for item in tracker.forecasts(now=41)}
    correlated = forecasts["target"]

    assert independent.requests_per_minute == 0
    assert independent.offered_concurrency == 0
    assert correlated.requests_per_minute > 0
    assert correlated.offered_concurrency > 0
    assert correlated.correlated_requests_per_minute == correlated.requests_per_minute
    assert correlated.observed_requests_per_minute == 0
    assert forecasts["source"].observed_requests_per_minute > 0
    assert correlated.correlation_sources == ("source",)
    assert 0.7 * forecasts["source"].confidence < correlated.correlation_confidence < 1
    assert correlated.updated_at == forecasts["source"].updated_at
    assert correlated.trend_per_minute == 0
    assert correlated.queue_depth == 0
    assert correlated.p95_latency_ms == 0
    assert correlated.error_rate == 0


def test_demand_tracker_prewarms_mature_next_model_sequence():
    tracker = DemandTracker(
        bucket_seconds=10,
        window_seconds=200,
        ewma_alpha=1,
        confidence_samples=1,
    )
    for source_timestamp in (1, 21, 41, 61, 81):
        tracker.observe(
            "planner",
            requests=20,
            service_seconds=1,
            timestamp=source_timestamp,
        )
        tracker.observe(
            "implementer",
            requests=10,
            service_seconds=2,
            timestamp=source_timestamp + 10,
        )
    tracker.observe(
        "planner",
        requests=20,
        service_seconds=1,
        timestamp=101,
    )

    independent = tracker.forecast("implementer", now=101)
    forecasts = {item.model_id: item for item in tracker.forecasts(now=101)}
    predicted = forecasts["implementer"]

    assert independent.requests_per_minute == 0
    assert predicted.requests_per_minute == pytest.approx(
        forecasts["planner"].requests_per_minute / 2
    )
    assert predicted.offered_concurrency == pytest.approx(
        predicted.requests_per_minute / 30
    )
    assert predicted.correlation_confidence == 1
    assert predicted.correlation_sources == ("planner",)
    assert predicted.updated_at == 101


def test_sequential_demand_prediction_does_not_propagate_two_hops():
    tracker = DemandTracker(
        bucket_seconds=10,
        window_seconds=200,
        ewma_alpha=1,
        confidence_samples=1,
    )
    for first_timestamp in (1, 31, 61):
        tracker.observe("first", requests=20, timestamp=first_timestamp)
        tracker.observe("second", requests=10, timestamp=first_timestamp + 10)
        tracker.observe("third", requests=5, timestamp=first_timestamp + 20)
    tracker.observe("first", requests=20, timestamp=91)

    forecasts = {item.model_id: item for item in tracker.forecasts(now=91)}

    assert forecasts["second"].correlation_sources == ("first",)
    assert forecasts["second"].requests_per_minute > 0
    assert forecasts["third"].correlation_sources == ()
    assert forecasts["third"].requests_per_minute == 0


def test_demand_correlation_requires_support_and_rejects_popularity_overlap():
    sparse = DemandTracker(bucket_seconds=10, window_seconds=200, ewma_alpha=1)
    for timestamp in (1, 11):
        sparse.observe("source", requests=20, service_seconds=1, timestamp=timestamp)
        sparse.observe("target", requests=10, service_seconds=1, timestamp=timestamp)
    sparse.observe("source", requests=20, service_seconds=1, timestamp=21)
    sparse_target = {item.model_id: item for item in sparse.forecasts(now=21)}["target"]
    assert sparse_target.correlation_sources == ()

    popular = DemandTracker(bucket_seconds=10, window_seconds=200, ewma_alpha=1)
    for timestamp in range(1, 101, 10):
        popular.observe("target", requests=10, service_seconds=1, timestamp=timestamp)
    for timestamp in (71, 81, 91, 101):
        popular.observe("source", requests=20, service_seconds=1, timestamp=timestamp)
    popular_target = {item.model_id: item for item in popular.forecasts(now=101)}[
        "target"
    ]
    assert popular_target.correlation_sources == ()


def test_correlated_demand_uses_maximum_source_without_transitive_amplification():
    tracker = DemandTracker(
        bucket_seconds=10,
        window_seconds=200,
        ewma_alpha=1,
        confidence_samples=1,
        correlation_threshold=0.5,
    )
    for timestamp in (1, 11, 21):
        tracker.observe("a", requests=20, service_seconds=1, timestamp=timestamp)
        tracker.observe("target", requests=10, service_seconds=1, timestamp=timestamp)
        tracker.observe("b", requests=40, service_seconds=1, timestamp=timestamp)
    tracker.observe("a", requests=20, service_seconds=1, timestamp=31)
    tracker.observe("b", requests=40, service_seconds=1, timestamp=31)

    forecasts = {item.model_id: item for item in tracker.forecasts(now=31)}
    target = forecasts["target"]

    assert target.correlation_sources == ("a", "b")
    # Both sources learned the same target rate. Taking their maximum keeps the inferred target at
    # its bounded historical scale instead of summing two equivalent explanations.
    assert target.requests_per_minute <= 2 * 60


def test_demand_correlation_configuration_round_trips_and_validates():
    tracker = DemandTracker(
        correlation_min_buckets=4,
        correlation_threshold=0.8,
        correlation_max_growth=1.5,
        correlation_max_sources=7,
    )
    restored = DemandTracker.from_dict(tracker.to_dict())
    assert restored.correlation_min_buckets == 4
    assert restored.correlation_threshold == 0.8
    assert restored.correlation_max_growth == 1.5
    assert restored.correlation_max_sources == 7

    with pytest.raises(ValueError, match="correlation_min_buckets"):
        DemandTracker(correlation_min_buckets=0)
    with pytest.raises(ValueError, match="correlation_min_buckets"):
        DemandTracker(correlation_min_buckets=True)
    with pytest.raises(ValueError, match="correlation_threshold"):
        DemandTracker(correlation_threshold=0)
    with pytest.raises(ValueError, match="correlation_max_growth"):
        DemandTracker(correlation_max_growth=0.5)
    with pytest.raises(ValueError, match="correlation_max_sources"):
        DemandTracker(correlation_max_sources=0)
    with pytest.raises(ValueError, match="correlation_max_sources"):
        DemandTracker(correlation_max_sources=True)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("window_seconds", math.inf),
        ("window_seconds", math.nan),
        ("bucket_seconds", math.inf),
        ("bucket_seconds", math.nan),
        ("max_samples_per_model", True),
        ("max_samples_per_model", 1.5),
        ("confidence_samples", True),
        ("confidence_samples", 1.5),
    ),
)
def test_demand_tracker_rejects_nonfinite_windows_and_fractional_limits(
    field,
    value,
):
    with pytest.raises(ValueError):
        DemandTracker(**{field: value})


def test_observed_demand_is_placed_before_inferred_only_prewarm():
    inferred = model("a-inferred", min_replicas=0, max_replicas=1)
    observed = model("z-observed", min_replicas=0, max_replicas=1)
    forecasts = (
        DemandForecast(
            "a-inferred",
            requests_per_minute=60,
            offered_concurrency=1,
            correlated_requests_per_minute=60,
            correlation_confidence=1,
            correlation_sources=("z-observed",),
            updated_at=10,
        ),
        DemandForecast(
            "z-observed",
            requests_per_minute=60,
            observed_requests_per_minute=60,
            offered_concurrency=1,
            updated_at=10,
        ),
    )

    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        [node("only", capacity_mb=8_000)],
        [inferred, observed],
        forecasts,
        now=10,
    )

    assert plan.nodes_for("z-observed") == ("only",)
    assert plan.nodes_for("a-inferred") == ()


def test_observed_service_fills_free_fleet_before_inferred_canary():
    inferred = model("inferred", min_replicas=0, max_replicas=4)
    observed = model("observed", min_replicas=1, max_replicas=4)
    forecasts = (
        DemandForecast(
            "inferred",
            requests_per_minute=240,
            offered_concurrency=4,
            correlated_requests_per_minute=240,
            correlation_confidence=1,
            correlation_sources=("workload:coding",),
            updated_at=10,
        ),
        DemandForecast(
            "observed",
            requests_per_minute=240,
            observed_requests_per_minute=240,
            offered_concurrency=4,
            updated_at=10,
        ),
    )
    machines = tuple(
        node(
            f"n-{index}",
            capacity_mb=8_000,
            cached=("inferred", "observed"),
            max_models=1,
        )
        for index in range(4)
    )

    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        machines,
        (inferred, observed),
        forecasts,
        now=10,
    )

    assert plan.nodes_for("observed") == ("n-0", "n-1", "n-2", "n-3")
    assert plan.nodes_for("inferred") == ()


def test_required_baseline_is_placed_before_burst_capacity_at_equal_priority():
    observed = model("a-observed", min_replicas=0, max_replicas=1)
    baseline = model("z-baseline", min_replicas=1, max_replicas=1)
    forecast = DemandForecast(
        "a-observed",
        requests_per_minute=60,
        observed_requests_per_minute=60,
        offered_concurrency=1,
        updated_at=10,
    )

    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        [node("only", capacity_mb=8_000)],
        [observed, baseline],
        [forecast],
        now=10,
    )

    assert plan.nodes_for("z-baseline") == ("only",)
    assert plan.nodes_for("a-observed") == ()


def test_replica_count_uses_offered_concurrency_headroom_and_bounds():
    profile = model(min_replicas=1, max_replicas=5, target_utilization=0.5)
    forecast = DemandForecast("qwen", offered_concurrency=1)
    assert desired_replica_count(profile, forecast, now=100) == 3
    huge = DemandForecast("qwen", offered_concurrency=100)
    assert desired_replica_count(profile, huge, now=100) == 5


def test_replica_count_prewarms_across_model_startup_horizon():
    slow = model(
        min_replicas=0,
        max_replicas=10,
        target_utilization=1,
        load_seconds=60,
        warm_seconds=0,
    )
    instant = replace(slow, load_seconds=0)
    rising = DemandForecast(
        "qwen",
        requests_per_minute=60,
        offered_concurrency=1,
        trend_per_minute=60,
        confidence=1,
    )

    assert desired_replica_count(instant, rising, now=100) == 2
    assert desired_replica_count(slow, rising, now=100) == 3


def test_predictive_prewarm_uses_fastest_eligible_learned_startup_path():
    profile = model(
        min_replicas=0,
        max_replicas=10,
        target_utilization=1,
        load_seconds=60,
        warm_seconds=100,
    )
    machines = (
        node("a-fast", cached=("qwen",)),
        node("z-slow", cached=("qwen",)),
    )
    rising = DemandForecast(
        "qwen",
        requests_per_minute=60,
        offered_concurrency=1,
        trend_per_minute=60,
        confidence=1,
    )

    plan = PlacementPlanner().plan(
        machines,
        (profile,),
        (rising,),
        now=100,
        startup_seconds={
            ("a-fast", "qwen"): 1,
            ("z-slow", "qwen"): 100,
        },
    )

    assert plan.target_for("qwen") == 2
    assert plan.nodes_for("qwen") == ("a-fast", "z-slow")


def test_predictive_prewarm_does_not_recount_load_for_warming_replica():
    profile = model(
        min_replicas=0,
        max_replicas=10,
        target_utilization=1,
        load_seconds=100,
        warm_seconds=1,
    )
    warming = node(
        "warming",
        residencies=(
            ModelResidency(
                "qwen",
                8_000,
                ResidencyState.WARMING,
            ),
        ),
    )
    rising = DemandForecast(
        "qwen",
        requests_per_minute=60,
        offered_concurrency=1,
        trend_per_minute=60,
        confidence=1,
    )

    plan = PlacementPlanner().plan((warming,), (profile,), (rising,), now=100)

    assert plan.target_for("qwen") == 2


@pytest.mark.parametrize("startup_horizon", (-1, math.inf, math.nan, True))
def test_replica_count_rejects_invalid_startup_horizon(startup_horizon):
    with pytest.raises(ValueError, match="startup horizon"):
        desired_replica_count(
            model(),
            DemandForecast("qwen"),
            now=100,
            startup_horizon_seconds=startup_horizon,
        )


def test_predictive_prewarm_ignores_untrusted_or_falling_trends_and_bounds_growth():
    profile = model(
        min_replicas=0,
        max_replicas=10,
        target_utilization=0.5,
        load_seconds=10_000,
        warm_seconds=10_000,
    )
    baseline = DemandForecast(
        "qwen",
        requests_per_minute=60,
        offered_concurrency=1,
    )
    no_confidence = replace(
        baseline,
        trend_per_minute=1_000_000,
        confidence=0,
    )
    falling = replace(baseline, trend_per_minute=-1_000_000, confidence=1)
    adversarial = replace(baseline, trend_per_minute=1_000_000, confidence=1)
    bounded_policy = PlannerPolicy(
        max_predictive_lookahead_seconds=300,
        predictive_growth_limit=1.5,
    )

    assert desired_replica_count(profile, baseline, now=100) == 3
    assert desired_replica_count(profile, no_confidence, now=100) == 3
    assert desired_replica_count(profile, falling, now=100) == 3
    assert (
        desired_replica_count(
            profile,
            adversarial,
            now=100,
            policy=bounded_policy,
        )
        == 4
    )


def test_predictive_prewarm_does_not_resurrect_expired_demand():
    profile = model(
        min_replicas=0,
        max_replicas=10,
        load_seconds=300,
        scale_down_cooldown_seconds=10,
    )
    stale = DemandForecast(
        "qwen",
        requests_per_minute=60,
        offered_concurrency=1,
        trend_per_minute=1_000,
        confidence=1,
        updated_at=1,
    )

    assert desired_replica_count(profile, stale, now=100) == 0


def test_replica_count_credits_a_single_model_vllm_batcher():
    profile = replace(
        model(min_replicas=0, max_replicas=10, target_utilization=0.75),
        runtimes=("vllm",),
        backends=("cuda",),
    )
    batcher = node(
        "vllm-batcher",
        runtime="vllm",
        backend="cuda",
        max_concurrency=16,
        manually_managed=True,
        actuator_capabilities=(),
        residencies=(ready(managed=False),),
    )

    assert (
        desired_replica_count(
            profile,
            DemandForecast("qwen", offered_concurrency=6),
            nodes=(batcher,),
            now=10,
        )
        == 1
    )


def test_replica_count_does_not_depend_on_selecting_the_widest_ready_engine():
    profile = replace(
        model(min_replicas=0, max_replicas=10, target_utilization=0.75),
        runtimes=("vllm",),
        backends=("cuda",),
    )
    narrow = node(
        "narrow",
        runtime="vllm",
        backend="cuda",
        max_concurrency=1,
        manually_managed=True,
        actuator_capabilities=(),
        residencies=(ready(managed=False),),
    )
    wide = node(
        "wide",
        runtime="vllm",
        backend="cuda",
        max_concurrency=16,
        manually_managed=True,
        actuator_capabilities=(),
        residencies=(ready(managed=False),),
    )

    assert (
        desired_replica_count(
            profile,
            DemandForecast("qwen", offered_concurrency=6),
            nodes=(narrow, wide),
            now=10,
        )
        == 2
    )


def test_replica_count_does_not_multiply_shared_multi_model_batch_capacity():
    profile = replace(
        model(min_replicas=0, max_replicas=10, target_utilization=0.5),
        runtimes=("vllm",),
        backends=("cuda",),
    )
    shared_batcher = node(
        "shared-vllm",
        runtime="vllm",
        backend="cuda",
        max_concurrency=32,
        manually_managed=True,
        actuator_capabilities=(),
        residencies=(ready(managed=False), ready("other", managed=False)),
    )

    assert (
        desired_replica_count(
            profile,
            DemandForecast("qwen", offered_concurrency=1),
            nodes=(shared_batcher,),
            now=10,
        )
        == 3
    )


def test_observed_pressure_scales_beyond_a_wide_ready_batcher():
    profile = replace(
        model(min_replicas=0, max_replicas=4, latency_slo_ms=1_000),
        runtimes=("vllm",),
        backends=("cuda",),
    )
    batcher = node(
        "vllm-batcher",
        runtime="vllm",
        backend="cuda",
        max_concurrency=64,
        manually_managed=True,
        actuator_capabilities=(),
        residencies=(ready(managed=False),),
    )

    assert (
        desired_replica_count(
            profile,
            DemandForecast(
                "qwen",
                offered_concurrency=1,
                queue_depth=1,
                p95_latency_ms=2_000,
            ),
            nodes=(batcher,),
            now=10,
        )
        == 2
    )


def test_profile_replica_concurrency_sizes_not_yet_ready_capacity():
    profile = model(
        min_replicas=0,
        max_replicas=10,
        target_utilization=0.5,
        replica_concurrency=4,
    )

    assert (
        desired_replica_count(
            profile,
            DemandForecast("qwen", offered_concurrency=1),
            now=10,
        )
        == 1
    )


def test_recent_demand_watermark_preserves_one_replica_across_registry_restart():
    profile = model(
        min_replicas=0,
        max_replicas=1,
        scale_down_cooldown_seconds=60,
    )
    recent = DemandForecast("qwen", updated_at=90)
    future = DemandForecast("qwen", updated_at=1_000)

    assert desired_replica_count(profile, recent, now=100) == 1
    assert desired_replica_count(profile, recent, now=151) == 0
    assert desired_replica_count(profile, future, now=100) == 0


def test_replica_count_preserves_recent_residencies_from_single_pass_iterable():
    profile = model(
        min_replicas=0,
        max_replicas=3,
        target_utilization=1,
        scale_down_cooldown_seconds=60,
    )
    machines = tuple(
        node(
            f"n{index}",
            residencies=(
                ModelResidency(
                    "qwen",
                    8_000,
                    ResidencyState.READY,
                    loaded_at=90,
                ),
            ),
        )
        for index in range(3)
    )
    forecast = DemandForecast("qwen", offered_concurrency=0.5)

    assert (
        desired_replica_count(
            profile,
            forecast,
            nodes=(machine for machine in machines),
            now=100,
        )
        == desired_replica_count(
            profile,
            forecast,
            nodes=machines,
            now=100,
        )
        == 3
    )


def test_replica_count_falls_back_to_profile_service_time_for_rate_only_demand():
    profile = model(
        min_replicas=0,
        max_replicas=10,
        target_utilization=0.70,
        expected_service_seconds=5,
    )
    forecast = DemandForecast("qwen", requests_per_minute=60, offered_concurrency=0)
    assert desired_replica_count(profile, forecast, now=100) == 9


def test_replica_count_scales_for_queue_latency_and_errors():
    profile = model(min_replicas=0, max_replicas=10, latency_slo_ms=1_000)
    forecast = DemandForecast(
        "qwen",
        offered_concurrency=0.1,
        queue_depth=5,
        p95_latency_ms=2_000,
        error_rate=0.5,
    )
    assert desired_replica_count(profile, forecast, now=100) >= 6


def test_weak_portfolio_evidence_caps_spare_capacity_scale_out_to_one_canary():
    profile = model(
        min_replicas=0,
        max_replicas=10,
        target_utilization=0.5,
    )
    forecast = DemandForecast(
        "qwen",
        requests_per_minute=10,
        offered_concurrency=4,
        correlated_requests_per_minute=10,
        correlation_sources=("workload:video",),
        canary_only=True,
    )

    assert desired_replica_count(profile, forecast, now=100) == 1


def test_historical_error_rate_does_not_ratchet_one_replica_per_tick():
    profile = model(
        min_replicas=1,
        max_replicas=8,
        scale_down_cooldown_seconds=0,
    )
    forecast = DemandForecast(
        "qwen",
        offered_concurrency=0.1,
        error_rate=0.25,
        updated_at=100,
    )
    machines = tuple(
        node(
            f"n-{index}",
            residencies=(ready("qwen", 8_000, loaded_at=1, last_used_at=1),),
        )
        for index in range(4)
    )

    assert desired_replica_count(profile, forecast, nodes=machines, now=100) == 3


def test_historical_queue_pressure_adds_one_safety_replica_without_ratcheting():
    profile = model(
        min_replicas=1,
        max_replicas=8,
        scale_down_cooldown_seconds=0,
    )
    forecast = DemandForecast(
        "qwen",
        offered_concurrency=0.1,
        queue_depth=1,
        updated_at=100,
    )
    machines = tuple(
        node(
            f"n-{index}",
            residencies=(ready("qwen", 8_000, loaded_at=1, last_used_at=1),),
        )
        for index in range(4)
    )

    assert desired_replica_count(profile, forecast, nodes=machines, now=100) == 2


def test_replica_count_keeps_recent_ready_replicas_during_scale_down():
    profile = model(min_replicas=1, max_replicas=3, scale_down_cooldown_seconds=100)
    nodes = [
        node("a", residencies=(ready(last_used_at=90),)),
        node("b", residencies=(ready(last_used_at=80),)),
    ]
    assert (
        desired_replica_count(profile, DemandForecast("qwen"), nodes=nodes, now=100)
        == 2
    )
    assert (
        desired_replica_count(profile, DemandForecast("qwen"), nodes=nodes, now=500)
        == 1
    )


def test_replica_count_does_not_treat_a_future_node_timestamp_as_recent_forever():
    profile = model(min_replicas=0, max_replicas=2, scale_down_cooldown_seconds=100)
    skewed = node(
        "future-clock",
        residencies=(ready(loaded_at=1_000_000, last_used_at=1_000_000),),
    )
    assert (
        desired_replica_count(
            profile,
            DemandForecast("qwen"),
            nodes=[skewed],
            now=100,
        )
        == 0
    )


def test_planner_empty_inputs_are_valid_and_deterministic():
    planner = PlacementPlanner()
    first = planner.plan([], [], now=10)
    second = planner.plan([], [], now=10)
    assert first == second
    assert first.assignments == ()


def test_planner_rejects_duplicate_node_or_model_ids():
    planner = PlacementPlanner()
    with pytest.raises(ValueError, match="duplicate node"):
        planner.plan([node("n"), node("n")], [model()], now=10)
    with pytest.raises(ValueError, match="duplicate model"):
        planner.plan([node("n")], [model(), model()], now=10)
    with pytest.raises(ValueError, match="duplicate forecast model"):
        planner.plan(
            [node("n")],
            [model()],
            [DemandForecast("qwen"), DemandForecast("qwen", offered_concurrency=1)],
            now=10,
        )


def test_planner_places_minimum_replica_without_demand():
    plan = PlacementPlanner().plan([node("n")], [model()], now=10)
    assert plan.nodes_for("qwen") == ("n",)
    assert plan.target_for("qwen") == 1
    assert plan.unsatisfied == ()


def test_planner_never_overcommits_memory():
    machines = [node("n", capacity_mb=10_000)]
    models = [model("large", 8_000), model("other", 8_000)]
    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        machines, models, now=10
    )
    assert len(plan.assignments) == 1
    assert sum(item.memory_mb for item in plan.assignments) <= 10_000
    assert any(item.code == "insufficient_capacity" for item in plan.unsatisfied)


def test_planner_accounts_for_reserved_and_unmanaged_memory():
    external = ready("external", 8_000, managed=False)
    machine = node("n", 16_000, reserved_mb=2_000, residencies=(external,))
    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        [machine], [model(memory_mb=8_000)], now=10
    )
    assert plan.assignments == ()


def test_planner_reserves_obsolete_managed_memory_until_it_is_unloaded():
    obsolete = ready("old", 8_000, managed=True)
    machine = node("n", 8_000, residencies=(obsolete,))
    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        [machine],
        [
            model("old", 8_000, min_replicas=0, max_replicas=0),
            model("new", 8_000),
        ],
        now=10,
    )
    assert plan.nodes_for("new") == ()
    assert any(
        item.model_id == "new" and item.code == "insufficient_capacity"
        for item in plan.unsatisfied
    )


def test_reserve_growth_moves_low_priority_incumbent_to_safe_peer():
    policy = PlannerPolicy(memory_headroom_fraction=0.05)
    profiles = (
        model("code", 12_000, min_replicas=1, max_replicas=1, priority=400),
        model("assistant", 8_000, min_replicas=1, max_replicas=1, priority=300),
        model("embeddings", 4_000, min_replicas=1, max_replicas=1, priority=100),
    )
    pressured = node(
        "pressured",
        32_000,
        reserved_mb=9_000,
        residencies=(
            ready("code", 12_000),
            ready("assistant", 8_000),
            ready("embeddings", 4_000),
        ),
    )
    peer = node("peer", 6_000)

    plan = PlacementPlanner(policy).plan((pressured, peer), profiles, now=10)

    assert plan.models_for("pressured") == ("assistant", "code")
    assert plan.nodes_for("embeddings") == ("peer",)
    assert (
        sum(
            assignment.memory_mb
            for assignment in plan.assignments
            if assignment.node_id == "pressured"
        )
        <= 32_000 - 9_000
    )
    result = Reconciler().reconcile(plan, (pressured, peer), profiles, now=10)
    assert [action.kind for action in result.actions] == [
        ActionKind.LOAD,
        ActionKind.WARM,
    ]
    assert all(
        action.kind not in (ActionKind.DRAIN, ActionKind.UNLOAD)
        for action in result.actions
    )
    assert any(item.code == "replacement_not_ready" for item in result.deferred)


def test_reserve_growth_without_safe_peer_reports_shortfall_before_drain():
    policy = PlannerPolicy(memory_headroom_fraction=0.05)
    profiles = (
        model("code", 12_000, min_replicas=1, max_replicas=1, priority=400),
        model("assistant", 8_000, min_replicas=1, max_replicas=1, priority=300),
        model("embeddings", 4_000, min_replicas=1, max_replicas=1, priority=100),
    )
    pressured = node(
        "pressured",
        32_000,
        reserved_mb=9_000,
        residencies=(
            ready("code", 12_000),
            ready("assistant", 8_000),
            ready("embeddings", 4_000),
        ),
    )

    plan = PlacementPlanner(policy).plan((pressured,), profiles, now=10)

    assert plan.models_for("pressured") == ("assistant", "code")
    assert plan.nodes_for("embeddings") == ()
    assert any(
        item.model_id == "embeddings" and item.code == "insufficient_capacity"
        for item in plan.unsatisfied
    )
    result = Reconciler().reconcile(plan, (pressured,), profiles, now=10)
    assert all(
        action.kind not in (ActionKind.DRAIN, ActionKind.UNLOAD)
        for action in result.actions
    )
    assert any(item.code == "replacement_not_ready" for item in result.deferred)


def test_throttling_derating_moves_lower_priority_incumbent_to_peer():
    policy = PlannerPolicy(
        memory_headroom_fraction=0.05,
        throttled_capacity_fraction=0.5,
    )
    profiles = (
        model("code", 12_000, min_replicas=1, max_replicas=1, priority=400),
        model("assistant", 8_000, min_replicas=1, max_replicas=1, priority=300),
    )
    throttled = node(
        "throttled",
        32_000,
        state=NodeState.THROTTLED,
        residencies=(ready("code", 12_000), ready("assistant", 8_000)),
    )
    peer = node("peer", 10_000)

    plan = PlacementPlanner(policy).plan((throttled, peer), profiles, now=10)

    assert plan.nodes_for("code") == ("throttled",)
    assert plan.nodes_for("assistant") == ("peer",)
    assert (
        sum(
            assignment.memory_mb
            for assignment in plan.assignments
            if assignment.node_id == "throttled"
        )
        <= 16_000
    )
    result = Reconciler().reconcile(plan, (throttled, peer), profiles, now=10)
    assert [action.kind for action in result.actions] == [
        ActionKind.LOAD,
        ActionKind.WARM,
    ]
    assert any(item.code == "replacement_not_ready" for item in result.deferred)


def test_planner_prefers_existing_then_cached_then_cold():
    profile = model()
    existing = node("existing", residencies=(ready(),), now=1_000)
    cached = node("cached", cached=("qwen",), now=1_000)
    cold = node("cold", now=1_000)
    plan = PlacementPlanner().plan([cold, cached, existing], [profile], now=1_000)
    assert plan.nodes_for("qwen") == ("existing",)
    plan = PlacementPlanner().plan([cold, cached], [profile], now=1_000)
    assert plan.nodes_for("qwen") == ("cached",)


def test_planner_uses_learned_warm_time_to_choose_between_cached_hosts():
    slow = node("a-slow", cached=("qwen",))
    fast = node("z-fast", cached=("qwen",))
    profile = model(min_replicas=1, max_replicas=1, warm_seconds=10)
    planner = PlacementPlanner()

    baseline = planner.plan((slow, fast), (profile,), now=10)
    learned = planner.plan(
        (slow, fast),
        (profile,),
        now=10,
        startup_seconds={
            (slow.node_id, profile.model_id): 40,
            (fast.node_id, profile.model_id): 2,
        },
    )

    assert baseline.assignments[0].node_id == slow.node_id
    assert learned.assignments[0].node_id == fast.node_id
    assert "learned warm-start estimate" in learned.assignments[0].reasons


def test_planner_uses_learned_artifact_load_time_to_choose_between_cold_hosts():
    slow = node("a-slow")
    fast = node("z-fast")
    profile = model(min_replicas=1, max_replicas=1, load_seconds=10, warm_seconds=1)
    planner = PlacementPlanner()

    baseline = planner.plan((slow, fast), (profile,), now=10)
    learned = planner.plan(
        (slow, fast),
        (profile,),
        now=10,
        load_seconds={
            (slow.node_id, profile.model_id): 100,
            (fast.node_id, profile.model_id): 2,
        },
    )

    assert baseline.assignments[0].node_id == slow.node_id
    assert learned.assignments[0].node_id == fast.node_id
    assert "learned artifact-load estimate" in learned.assignments[0].reasons


def test_portfolio_hint_uses_fastest_learned_cold_path():
    profile = model("coder", min_replicas=0, load_seconds=10, warm_seconds=1)

    hint = PlacementPlanner().portfolio_placement_hints(
        (node("a-slow"), node("z-fast")),
        (profile,),
        now=10,
        load_seconds={
            ("a-slow", "coder"): 100,
            ("z-fast", "coder"): 2,
        },
    )["coder"]

    assert hint["best_node_id"] == "z-fast"
    assert hint["startup_seconds"] == 3


@pytest.mark.parametrize("duration", [float("nan"), float("inf"), -1, True])
def test_planner_rejects_invalid_startup_estimates(duration):
    with pytest.raises(ValueError, match="startup estimates"):
        PlacementPlanner().plan(
            (node("n", cached=("qwen",)),),
            (model(),),
            now=10,
            startup_seconds={("n", "qwen"): duration},
        )


@pytest.mark.parametrize("duration", [float("nan"), float("inf"), -1, True])
def test_planner_rejects_invalid_load_estimates(duration):
    with pytest.raises(ValueError, match="load estimates"):
        PlacementPlanner().plan(
            (node("n"),),
            (model(),),
            now=10,
            load_seconds={("n", "qwen"): duration},
        )


def test_planner_avoids_a_loaded_or_queued_host_when_equivalent_capacity_is_idle():
    busy = node("busy", active_requests=1, max_concurrency=1, queue_depth=2)
    idle = node("idle", active_requests=0, max_concurrency=1)
    plan = PlacementPlanner().plan([busy, idle], [model()], now=10)
    assert plan.nodes_for("qwen") == ("idle",)


def test_planner_spreads_replicas_across_failure_domains():
    machines = [
        node("a1", domain="rack-a", cached=("qwen",)),
        node("a2", domain="rack-a", cached=("qwen",)),
        node("b1", domain="rack-b"),
    ]
    profile = model(min_replicas=2, max_replicas=2, min_failure_domains=2)
    plan = PlacementPlanner().plan(machines, [profile], now=10)
    chosen = set(plan.nodes_for("qwen"))
    assert "b1" in chosen
    assert len(chosen & {"a1", "a2"}) == 1


def test_planner_reports_failure_domain_shortfall_without_lying():
    machines = [node("a", domain="rack"), node("b", domain="rack")]
    profile = model(min_replicas=2, max_replicas=2, min_failure_domains=2)
    plan = PlacementPlanner().plan(machines, [profile], now=10)
    assert len(plan.assignments) == 2
    assert any(item.code == "failure_domain_shortfall" for item in plan.unsatisfied)


def test_planner_enforces_privacy_runtime_backend_and_tags():
    profile = model(
        data_tier="confidential",
        required_tags=("finance",),
        forbidden_tags=("contractor",),
    )
    machines = [
        node("public"),
        node(
            "wrong-runtime",
            tiers=("confidential",),
            tags=("finance",),
            runtime="ollama",
        ),
        node("contractor", tiers=("confidential",), tags=("finance", "contractor")),
        node("approved", tiers=("confidential",), tags=("finance",)),
    ]
    plan = PlacementPlanner().plan(machines, [profile], now=10)
    assert plan.nodes_for("qwen") == ("approved",)


@pytest.mark.parametrize(
    "state",
    [NodeState.DRAINING, NodeState.PAUSED, NodeState.UNHEALTHY, NodeState.QUARANTINED],
)
def test_planner_never_places_on_host_protected_node(state):
    plan = PlacementPlanner().plan([node("n", state=state)], [model()], now=10)
    assert plan.assignments == ()
    assert plan.unsatisfied[0].code == "no_eligible_nodes"


def test_planner_excludes_stale_missing_and_implausibly_future_heartbeats():
    planner = PlacementPlanner(PlannerPolicy(node_ttl_seconds=10))
    stale = node("stale", now=1)
    missing = node("missing", now=0)
    future = node("future", now=1_000)
    healthy = node("healthy", now=95)
    plan = planner.plan([stale, missing, future, healthy], [model()], now=100)
    assert plan.nodes_for("qwen") == ("healthy",)


def test_planner_honours_pinned_placement_and_reports_bad_pin():
    profile = model(pinned_nodes=("b",), min_replicas=1, max_replicas=1)
    plan = PlacementPlanner().plan([node("a"), node("b")], [profile], now=10)
    assert plan.nodes_for("qwen") == ("b",)
    bad = PlacementPlanner().plan([node("a")], [profile], now=10)
    assert bad.assignments == ()
    assert bad.unsatisfied[0].code == "pinned_node_unavailable"


def test_hard_pins_are_a_floor_for_the_reported_replica_target():
    profile = model(
        pinned_nodes=("a", "b"),
        min_replicas=0,
        max_replicas=2,
    )
    plan = PlacementPlanner().plan([node("a"), node("b")], [profile], now=10)

    assert plan.target_for("qwen") == 2
    assert set(plan.nodes_for("qwen")) == {"a", "b"}
    assert plan.unsatisfied == ()


def test_hard_pins_are_reserved_before_other_models_optional_placements():
    machines = [
        node("only", max_models=1),
        node("other", max_models=1),
    ]
    optional = model("optional", memory_mb=4_000, priority=1_000)
    pinned = model(
        "pinned",
        memory_mb=4_000,
        priority=1,
        pinned_nodes=("only",),
    )

    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        machines,
        [optional, pinned],
        now=10,
    )

    assert plan.nodes_for("pinned") == ("only",)
    assert plan.nodes_for("optional") == ("other",)
    assert plan.unsatisfied == ()


def test_planner_does_not_add_models_to_manual_node_but_uses_existing_replica():
    manual = node("n", manually_managed=True)
    assert PlacementPlanner().plan([manual], [model()], now=10).assignments == ()
    existing = node("n", manually_managed=True, residencies=(ready(managed=False),))
    assert PlacementPlanner().plan([existing], [model()], now=10).nodes_for("qwen") == (
        "n",
    )


def test_planner_does_not_count_ineligible_external_replica_as_available_capacity():
    paused = node(
        "paused",
        state=NodeState.PAUSED,
        manually_managed=True,
        residencies=(ready(managed=False),),
    )
    wrong_tier = node(
        "public-only",
        tiers=("public",),
        manually_managed=True,
        residencies=(ready(managed=False),),
    )
    plan = PlacementPlanner().plan(
        [paused, wrong_tier], [model(data_tier="internal")], now=10
    )
    assert plan.assignments == ()
    assert any(item.code == "no_eligible_nodes" for item in plan.unsatisfied)


def test_external_ready_inventory_satisfies_but_cannot_inflate_replica_target():
    machines = [
        node(
            f"external-{index}",
            manually_managed=True,
            residencies=(ready(managed=False),),
        )
        for index in range(3)
    ]
    plan = PlacementPlanner().plan(
        machines,
        [model(min_replicas=1, max_replicas=1)],
        now=10,
    )
    assert len(plan.assignments) == 1
    assert plan.target_for("qwen") == 1


def test_external_inventory_cannot_authorize_draining_the_last_managed_baseline():
    external = node(
        "a-external",
        manually_managed=True,
        residencies=(ready(managed=False),),
    )
    managed = node("z-managed", residencies=(ready(managed=True),))
    profile = model(min_replicas=1, max_replicas=1, min_residency_seconds=0)
    plan = PlacementPlanner().plan([external, managed], [profile], now=10)
    assert plan.nodes_for("qwen") == ("a-external",)

    result = Reconciler().reconcile(
        plan,
        [external, managed],
        [profile],
        mode=AllocatorMode.AUTOMATIC,
        now=10,
    )

    assert result.actions == ()
    assert any(item.code == "trusted_replacement_not_ready" for item in result.deferred)


def test_existing_external_replica_can_satisfy_a_hard_pin_without_actuation():
    external = node(
        "external",
        manually_managed=True,
        residencies=(ready(managed=False),),
    )
    plan = PlacementPlanner().plan(
        [external],
        [model(pinned_nodes=("external",), min_replicas=1, max_replicas=1)],
        now=10,
    )
    assert plan.nodes_for("qwen") == ("external",)
    assert plan.unsatisfied == ()


def test_nonready_external_residency_does_not_satisfy_demand_it_cannot_re_admit():
    draining = ModelResidency(
        "qwen",
        8_000,
        ResidencyState.DRAINING,
        managed=False,
    )
    external = node("external", manually_managed=True, residencies=(draining,))
    plan = PlacementPlanner().plan([external], [model()], now=10)
    assert plan.assignments == ()
    assert plan.unsatisfied[0].code == "no_eligible_nodes"


def test_planner_respects_node_model_limit():
    machine = node("n", max_models=1)
    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        [machine], [model("a"), model("b")], now=10
    )
    assert len(plan.assignments) == 1


def test_same_priority_models_share_scarce_capacity_before_scaling_out():
    machines = [
        node(f"n{index}", capacity_mb=8_000, max_models=1) for index in range(3)
    ]
    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        machines,
        [
            model("alpha", min_replicas=3, max_replicas=3),
            model("beta", min_replicas=1, max_replicas=1),
        ],
        now=10,
    )

    assert len(plan.nodes_for("alpha")) == 2
    assert len(plan.nodes_for("beta")) == 1
    assert any(
        item.model_id == "alpha"
        and item.code == "insufficient_capacity"
        and item.missing_replicas == 1
        for item in plan.unsatisfied
    )


def test_hard_pins_count_toward_equal_priority_fair_share():
    machines = [
        node(f"n{index}", capacity_mb=8_000, max_models=1) for index in range(4)
    ]
    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        machines,
        [
            model(
                "pinned",
                min_replicas=4,
                max_replicas=4,
                pinned_nodes=("n0", "n1"),
            ),
            model("peer", min_replicas=4, max_replicas=4),
        ],
        now=10,
    )

    assert len(plan.nodes_for("pinned")) == 2
    assert len(plan.nodes_for("peer")) == 2


def test_higher_priority_model_can_use_all_scarce_capacity():
    machines = [
        node(f"n{index}", capacity_mb=8_000, max_models=1) for index in range(2)
    ]
    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        machines,
        [
            model("critical", min_replicas=2, max_replicas=2, priority=100),
            model("batch", min_replicas=1, max_replicas=1, priority=1),
        ],
        now=10,
    )

    assert len(plan.nodes_for("critical")) == 2
    assert plan.nodes_for("batch") == ()


def test_higher_priority_model_stages_managed_incumbent_preemption():
    policy = PlannerPolicy(memory_headroom_fraction=0)
    planner = PlacementPlanner(policy)
    critical = model(
        "critical",
        8_000,
        priority=1_000,
        min_residency_seconds=0,
    )
    batch = model(
        "batch",
        8_000,
        priority=10,
        min_residency_seconds=0,
    )
    ready_batch = ready("batch", 8_000, loaded_at=1)
    occupied = node("n", 8_000, residencies=(ready_batch,))

    drain_plan = planner.plan((occupied,), (batch, critical), now=10)

    assert drain_plan.assignments == ()
    assert [
        (item.node_id, item.model_id, item.for_model_id)
        for item in drain_plan.preemptions
    ] == [("n", "batch", "critical")]
    assert drain_plan.to_dict()["preemptions"] == [
        {"node_id": "n", "model_id": "batch", "for_model_id": "critical"}
    ]
    drain = Reconciler().reconcile(
        drain_plan,
        (occupied,),
        (batch, critical),
        mode=AllocatorMode.AUTOMATIC,
        now=10,
    )
    assert [item.kind for item in drain.executable_actions] == [ActionKind.DRAIN]
    assert "higher-priority model 'critical'" in drain.executable_actions[0].reason

    draining = replace(
        occupied,
        residencies=(replace(ready_batch, state=ResidencyState.DRAINING),),
    )
    unload_plan = planner.plan((draining,), (batch, critical), now=11)
    assert unload_plan.preempted_pairs == frozenset({("n", "batch")})
    unload = Reconciler().reconcile(
        unload_plan,
        (draining,),
        (batch, critical),
        mode=AllocatorMode.AUTOMATIC,
        now=11,
    )
    assert [item.kind for item in unload.executable_actions] == [ActionKind.UNLOAD]

    available = replace(occupied, residencies=())
    ready_plan = planner.plan((available,), (batch, critical), now=12)
    assert ready_plan.nodes_for("critical") == ("n",)
    assert ready_plan.preemptions == ()


def test_priority_preemption_budget_progressively_frees_a_multi_victim_host():
    planner = PlacementPlanner(
        PlannerPolicy(memory_headroom_fraction=0, max_staged_preemptions=1)
    )
    critical = model("critical", 16_000, priority=1_000)
    incumbents = (
        model("batch-a", 8_000, priority=10),
        model("batch-b", 8_000, priority=10),
    )
    machine = node(
        "n",
        16_000,
        residencies=(ready("batch-a", 8_000), ready("batch-b", 8_000)),
    )

    first = planner.plan((machine,), (*incumbents, critical), now=10)
    assert first.preempted_pairs == frozenset({("n", "batch-a")})
    assert first.nodes_for("critical") == ()

    one_released = replace(machine, residencies=(ready("batch-b", 8_000),))
    second = planner.plan((one_released,), (*incumbents, critical), now=11)
    assert second.preempted_pairs == frozenset({("n", "batch-b")})
    assert second.nodes_for("critical") == ()

    available = replace(machine, residencies=())
    converged = planner.plan((available,), (*incumbents, critical), now=12)
    assert converged.preemptions == ()
    assert converged.nodes_for("critical") == ("n",)


def test_priority_preemption_budget_bounds_large_fleet_wave_deterministically():
    machines = tuple(
        node(
            f"n{index:03d}",
            8_000,
            max_models=1,
            residencies=(ready("batch", 8_000),),
        )
        for index in range(256)
    )
    profiles = (
        model(
            "batch",
            8_000,
            min_replicas=256,
            max_replicas=256,
            priority=10,
        ),
        model(
            "critical",
            8_000,
            min_replicas=256,
            max_replicas=256,
            priority=1_000,
        ),
    )
    planner = PlacementPlanner(
        PlannerPolicy(memory_headroom_fraction=0, max_staged_preemptions=16)
    )

    forward = planner.plan(machines, profiles, now=10)
    reversed_input = planner.plan(tuple(reversed(machines)), profiles, now=10)
    fresh_domain_search = planner.plan(
        machines,
        (profiles[0], replace(profiles[1], min_failure_domains=2)),
        now=10,
    )

    expected = frozenset((f"n{index:03d}", "batch") for index in range(16))
    assert len(forward.preemptions) == 16
    assert forward.preempted_pairs == expected
    assert reversed_input.preemptions == forward.preemptions
    assert fresh_domain_search.preemptions == forward.preemptions


def test_isolated_ready_bulk_placement_matches_general_scoring_exactly():
    general_nodes = tuple(
        node(
            f"n{index:02d}",
            8_000,
            domain=f"rack-{index}",
            max_models=None,
            residencies=(ready("batch", 8_000),),
            active_requests=index % 4,
            max_concurrency=4,
            queue_depth=index % 3,
        )
        for index in range(32)
    )
    isolated_nodes = tuple(replace(item, max_models=1) for item in general_nodes)
    profile = model(
        "batch",
        8_000,
        min_replicas=20,
        max_replicas=20,
        min_failure_domains=4,
    )
    planner = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0))

    general = planner.plan(general_nodes, (profile,), now=10)
    optimized = planner.plan(isolated_nodes, (profile,), now=10)

    assert optimized.assignments == general.assignments


def test_single_contender_cold_bulk_placement_matches_general_scoring_exactly():
    general_nodes = tuple(
        node(
            f"n{index:02d}",
            8_000,
            domain=f"rack-{index}",
            max_models=None,
            cached=("cold",) if index % 2 else (),
            active_requests=index % 4,
            max_concurrency=4,
            queue_depth=index % 3,
        )
        for index in range(32)
    )
    isolated_nodes = tuple(replace(item, max_models=1) for item in general_nodes)
    profile = model(
        "cold",
        8_000,
        min_replicas=20,
        max_replicas=20,
        min_failure_domains=4,
    )
    planner = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0))

    general = planner.plan(general_nodes, (profile,), now=10)
    optimized = planner.plan(isolated_nodes, (profile,), now=10)

    assert optimized.assignments == general.assignments


def test_equal_priority_cached_candidate_order_matches_general_scoring_exactly():
    general_nodes = tuple(
        node(
            f"n{index:02d}",
            8_000,
            domain=f"rack-{index}",
            max_models=None,
            active_requests=index % 4,
            max_concurrency=4,
            queue_depth=index % 3,
        )
        for index in range(64)
    )
    isolated_nodes = tuple(replace(item, max_models=1) for item in general_nodes)
    profiles = tuple(
        model(
            f"model-{index}",
            8_000,
            min_replicas=64,
            max_replicas=64,
            min_failure_domains=4,
        )
        for index in range(4)
    )
    planner = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0))

    general = planner.plan(general_nodes, profiles, now=10)
    optimized = planner.plan(isolated_nodes, profiles, now=10)

    assert optimized.assignments == general.assignments
    assert {
        profile.model_id: len(optimized.nodes_for(profile.model_id))
        for profile in profiles
    } == {profile.model_id: 16 for profile in profiles}


def test_equal_priority_isolated_fleet_scores_candidates_linearly(monkeypatch):
    machines = tuple(node(f"n{index:03d}", 8_000, max_models=1) for index in range(256))
    profiles = tuple(
        model(
            f"model-{index}",
            8_000,
            min_replicas=256,
            max_replicas=256,
        )
        for index in range(8)
    )
    original = planner_module._candidate_score
    score_calls = 0

    def counted_score(*args, **kwargs):
        nonlocal score_calls
        score_calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(planner_module, "_candidate_score", counted_score)

    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        machines,
        profiles,
        now=10,
    )

    assert len(plan.assignments) == len(machines)
    assert score_calls <= len(machines) * (len(profiles) + 2)


def test_shared_host_fairness_rounds_reuse_exact_candidate_state(monkeypatch):
    machines = tuple(node(f"n{index:03d}", 16_000) for index in range(64))
    profiles = tuple(
        model(
            f"model-{index}",
            8_000,
            min_replicas=32,
            max_replicas=32,
        )
        for index in range(4)
    )
    calls = {
        "score": 0,
        "compatibility": 0,
        "fit": 0,
        "memory": 0,
        "residency": 0,
    }
    original_score = planner_module._candidate_score
    original_compatibility = planner_module._ineligible_reason
    original_fit = planner_module._fits
    original_memory = ModelProfile.memory_for
    original_residency = NodeSnapshot.residency

    def counted_score(*args, **kwargs):
        calls["score"] += 1
        return original_score(*args, **kwargs)

    def counted_compatibility(*args, **kwargs):
        calls["compatibility"] += 1
        return original_compatibility(*args, **kwargs)

    def counted_fit(*args, **kwargs):
        calls["fit"] += 1
        return original_fit(*args, **kwargs)

    def counted_memory(*args, **kwargs):
        calls["memory"] += 1
        return original_memory(*args, **kwargs)

    def counted_residency(*args, **kwargs):
        calls["residency"] += 1
        return original_residency(*args, **kwargs)

    monkeypatch.setattr(planner_module, "_candidate_score", counted_score)
    monkeypatch.setattr(
        planner_module,
        "_ineligible_reason",
        counted_compatibility,
    )
    monkeypatch.setattr(planner_module, "_fits", counted_fit)
    monkeypatch.setattr(ModelProfile, "memory_for", counted_memory)
    monkeypatch.setattr(NodeSnapshot, "residency", counted_residency)

    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        machines,
        profiles,
        now=10,
    )

    assert len(plan.assignments) == 128
    distinct_pairs = len(machines) * len(profiles)
    assert calls["score"] <= distinct_pairs * 3
    assert calls["compatibility"] <= distinct_pairs * 3
    assert calls["fit"] <= distinct_pairs * 3
    # Public pair lookups include desired-count forecasting, scoring, assignment materialization,
    # and plan validation in addition to candidate evaluation. They must remain linear in the
    # distinct node/model facts instead of repeating on every fairness round.
    assert calls["memory"] <= distinct_pairs * 6
    assert calls["residency"] <= distinct_pairs * 10


def test_counterfactual_plans_reuse_only_the_exact_fleet_topology(monkeypatch):
    machines = tuple(node(f"n{index}", 16_000) for index in range(8))
    profiles = tuple(
        model(f"model-{index}", 8_000, min_replicas=0, max_replicas=2)
        for index in range(4)
    )
    planner = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0))
    original = planner_module._ineligible_reason
    compatibility_calls = 0

    def counted_compatibility(*args, **kwargs):
        nonlocal compatibility_calls
        compatibility_calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(planner_module, "_ineligible_reason", counted_compatibility)
    forecasts = (DemandForecast("model-0", requests_per_minute=1, sample_count=1),)
    planner.plan(machines, profiles, forecasts, now=10)
    initial_calls = compatibility_calls

    planner.plan(machines, profiles, forecasts, now=10)
    repeated_calls = compatibility_calls - initial_calls
    assert repeated_calls < initial_calls

    changed_machines = (replace(machines[0], host_priority=1), *machines[1:])
    planner.plan(changed_machines, profiles, forecasts, now=10)
    after_heartbeat = compatibility_calls
    assert after_heartbeat - initial_calls - repeated_calls > repeated_calls

    planner.plan(changed_machines, profiles, forecasts, now=11)
    assert compatibility_calls - after_heartbeat > repeated_calls


def test_counterfactual_plans_reuse_startup_horizons_until_timing_changes(monkeypatch):
    machines = tuple(node(f"n{index}", 16_000) for index in range(4))
    profiles = tuple(
        model(f"model-{index}", 8_000, min_replicas=0, max_replicas=2)
        for index in range(3)
    )
    planner = PlacementPlanner()
    original = planner_module._next_replica_startup_seconds
    startup_calls = 0

    def counted_startup(*args, **kwargs):
        nonlocal startup_calls
        startup_calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(
        planner_module,
        "_next_replica_startup_seconds",
        counted_startup,
    )
    planner.plan(machines, profiles, now=10)
    assert startup_calls == len(profiles)

    planner.plan(machines, profiles, now=10)
    assert startup_calls == len(profiles)

    planner.plan(
        machines,
        profiles,
        now=10,
        startup_seconds={("n0", "model-0"): 1.0},
    )
    assert startup_calls == len(profiles) * 2


def test_independent_preemption_wave_scans_the_fleet_once(monkeypatch):
    machines = tuple(
        node(
            f"n{index:03d}",
            8_000,
            max_models=1,
            residencies=(ready("batch", 8_000),),
        )
        for index in range(256)
    )
    profiles = (
        model(
            "batch",
            8_000,
            min_replicas=0,
            max_replicas=256,
            priority=10,
            scale_down_cooldown_seconds=0,
        ),
        model(
            "critical",
            8_000,
            min_replicas=256,
            max_replicas=256,
            priority=1_000,
        ),
    )
    original = planner_module._priority_preemption_candidates
    fleet_scans = 0

    def counted_candidates(*args, **kwargs):
        nonlocal fleet_scans
        fleet_scans += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(
        planner_module,
        "_priority_preemption_candidates",
        counted_candidates,
    )

    plan = PlacementPlanner(
        PlannerPolicy(memory_headroom_fraction=0, max_staged_preemptions=16)
    ).plan(machines, profiles, now=10)

    assert len(plan.preemptions) == 16
    assert fleet_scans == 1


def test_priority_preemption_prefers_the_cheapest_learned_warm_back_cost():
    batch = model(
        "batch",
        8_000,
        min_replicas=2,
        max_replicas=2,
        priority=10,
        min_residency_seconds=0,
    )
    critical = model(
        "critical",
        8_000,
        priority=1_000,
        min_residency_seconds=0,
    )
    expensive = node(
        "a-expensive",
        8_000,
        residencies=(ready("batch", 8_000),),
    )
    cheap = node(
        "z-cheap",
        8_000,
        residencies=(ready("batch", 8_000),),
    )

    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        (expensive, cheap),
        (batch, critical),
        now=10,
        startup_seconds={
            ("a-expensive", "batch"): 100,
            ("z-cheap", "batch"): 1,
        },
    )

    assert [
        (item.node_id, item.model_id, item.for_model_id) for item in plan.preemptions
    ] == [("z-cheap", "batch", "critical")]


def test_priority_preemption_prefers_cached_beneficiary_after_equal_disruption():
    low_a = model(
        "low-a",
        8_000,
        min_replicas=0,
        max_replicas=0,
        priority=10,
        min_residency_seconds=0,
    )
    low_z = replace(low_a, model_id="low-z")
    critical = model(
        "critical",
        8_000,
        priority=1_000,
        load_seconds=60,
        warm_seconds=5,
        min_residency_seconds=0,
    )
    cold = node(
        "a-cold",
        8_000,
        residencies=(ready("low-a", 8_000),),
    )
    cached = node(
        "z-cached",
        8_000,
        residencies=(ready("low-z", 8_000),),
        cached=("critical",),
    )

    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        (cold, cached),
        (low_a, low_z, critical),
        now=10,
    )

    assert [
        (item.node_id, item.model_id, item.for_model_id) for item in plan.preemptions
    ] == [("z-cached", "low-z", "critical")]


def test_priority_preemption_prefers_idle_work_over_cheaper_busy_work():
    batch = model("batch", 8_000, priority=10, min_residency_seconds=0)
    critical = model("critical", 8_000, priority=1_000, min_residency_seconds=0)
    busy = node(
        "a-busy",
        8_000,
        residencies=(ready("batch", 8_000, active_requests=1),),
    )
    idle = node(
        "z-idle",
        8_000,
        residencies=(ready("batch", 8_000),),
    )

    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        (busy, idle),
        (batch, critical),
        now=10,
        startup_seconds={
            ("a-busy", "batch"): 1,
            ("z-idle", "batch"): 100,
        },
    )

    assert plan.preempted_pairs == frozenset({("z-idle", "batch")})


def test_priority_preemption_prefers_a_residency_already_draining():
    batch = model("batch", 8_000, priority=10, min_residency_seconds=0)
    critical = model("critical", 8_000, priority=1_000, min_residency_seconds=0)
    ready_node = node(
        "a-ready",
        8_000,
        residencies=(ready("batch", 8_000),),
    )
    draining_node = node(
        "z-draining",
        8_000,
        residencies=(replace(ready("batch", 8_000), state=ResidencyState.DRAINING),),
    )

    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        (ready_node, draining_node),
        (batch, critical),
        now=10,
        startup_seconds={
            ("a-ready", "batch"): 1,
            ("z-draining", "batch"): 100,
        },
    )

    assert plan.preempted_pairs == frozenset({("z-draining", "batch")})


def test_priority_preemption_reserves_required_failure_domains_before_cost():
    batch = model(
        "batch",
        8_000,
        min_replicas=3,
        max_replicas=3,
        priority=10,
        min_residency_seconds=0,
    )
    critical = model(
        "critical",
        8_000,
        min_replicas=2,
        max_replicas=2,
        min_failure_domains=2,
        priority=1_000,
        min_residency_seconds=0,
    )
    nodes = (
        node(
            "a-cheapest",
            8_000,
            domain="rack-a",
            residencies=(ready("batch", 8_000),),
        ),
        node(
            "a-second",
            8_000,
            domain="rack-a",
            residencies=(ready("batch", 8_000),),
        ),
        node(
            "b-expensive",
            8_000,
            domain="rack-b",
            residencies=(ready("batch", 8_000),),
        ),
    )
    planner = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0))
    plan = planner.plan(
        nodes,
        (batch, critical),
        now=10,
        startup_seconds={
            ("a-cheapest", "batch"): 1,
            ("a-second", "batch"): 2,
            ("b-expensive", "batch"): 100,
        },
    )

    assert plan.preempted_pairs == frozenset(
        {("a-cheapest", "batch"), ("b-expensive", "batch")}
    )
    released = tuple(
        replace(node_snapshot, residencies=())
        if (node_snapshot.node_id, "batch") in plan.preempted_pairs
        else node_snapshot
        for node_snapshot in nodes
    )
    converged = planner.plan(released, (batch, critical), now=11)
    critical_nodes = {
        node_snapshot.node_id: node_snapshot for node_snapshot in released
    }
    assert {
        critical_nodes[node_id].failure_domain
        for node_id in converged.nodes_for("critical")
    } == {"rack-a", "rack-b"}


def test_priority_preemption_targets_missing_hard_pin_before_cheaper_host():
    batch = model(
        "batch",
        8_000,
        min_replicas=2,
        max_replicas=2,
        priority=10,
        min_residency_seconds=0,
    )
    critical = model(
        "critical",
        8_000,
        pinned_nodes=("a-pinned",),
        priority=1_000,
        min_residency_seconds=0,
    )
    pinned = node(
        "a-pinned",
        8_000,
        residencies=(ready("batch", 8_000),),
    )
    cheap_but_invalid = node(
        "z-cheap",
        8_000,
        residencies=(ready("batch", 8_000),),
    )
    planner = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0))

    staged = planner.plan(
        (pinned, cheap_but_invalid),
        (batch, critical),
        now=10,
        startup_seconds={
            ("a-pinned", "batch"): 100,
            ("z-cheap", "batch"): 1,
        },
    )

    assert staged.preempted_pairs == frozenset({("a-pinned", "batch")})
    released = (replace(pinned, residencies=()), cheap_but_invalid)
    converged = planner.plan(released, (batch, critical), now=11)
    assert converged.nodes_for("critical") == ("a-pinned",)


def test_correlation_only_prediction_cannot_preempt_live_observed_service():
    batch = model(
        "batch",
        8_000,
        priority=10,
        min_residency_seconds=0,
    )
    critical = model(
        "critical",
        8_000,
        min_replicas=0,
        max_replicas=1,
        priority=1_000,
        min_residency_seconds=0,
    )
    machine = node("n", 8_000, residencies=(ready("batch", 8_000),))
    planner = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0))
    inferred = DemandForecast(
        "critical",
        requests_per_minute=60,
        offered_concurrency=1,
        correlated_requests_per_minute=60,
        correlation_confidence=1,
        correlation_sources=("batch",),
        updated_at=10,
    )

    speculative = planner.plan(
        (machine,),
        (batch, critical),
        (inferred,),
        now=10,
    )
    assert speculative.preemptions == ()
    assert speculative.nodes_for("batch") == ("n",)

    direct = DemandForecast(
        "critical",
        requests_per_minute=60,
        offered_concurrency=1,
        observed_requests_per_minute=60,
        updated_at=11,
    )
    observed = planner.plan(
        (replace(machine, last_heartbeat=11),),
        (batch, critical),
        (direct,),
        now=11,
    )
    assert observed.preempted_pairs == frozenset({("n", "batch")})


def test_direct_demand_reclaims_same_priority_speculative_canary():
    speculative = model(
        "speculative",
        8_000,
        min_replicas=0,
        max_replicas=1,
        priority=100,
        min_residency_seconds=0,
    )
    direct = model(
        "direct",
        8_000,
        min_replicas=0,
        max_replicas=1,
        priority=100,
        min_residency_seconds=0,
    )
    machine = node(
        "n",
        8_000,
        residencies=(ready("speculative", 8_000),),
        max_models=1,
    )
    forecasts = (
        DemandForecast(
            "speculative",
            requests_per_minute=60,
            offered_concurrency=1,
            correlated_requests_per_minute=60,
            correlation_confidence=1,
            correlation_sources=("workload:coding",),
            updated_at=10,
        ),
        DemandForecast(
            "direct",
            requests_per_minute=60,
            offered_concurrency=1,
            observed_requests_per_minute=60,
            updated_at=10,
        ),
    )

    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        (machine,),
        (speculative, direct),
        forecasts,
        now=10,
    )

    assert plan.preempted_pairs == frozenset({("n", "speculative")})
    assert plan.preemptions[0].for_model_id == "direct"


def test_new_speculative_model_rebalances_one_excess_speculative_replica():
    incumbent = model(
        "incumbent",
        8_000,
        min_replicas=0,
        max_replicas=2,
        priority=100,
        min_residency_seconds=0,
    )
    newcomer = replace(incumbent, model_id="newcomer", max_replicas=1)
    machines = (
        node("a", 8_000, residencies=(ready("incumbent", 8_000),), max_models=1),
        node("b", 8_000, residencies=(ready("incumbent", 8_000),), max_models=1),
    )
    forecasts = (
        DemandForecast(
            "incumbent",
            requests_per_minute=60,
            offered_concurrency=2,
            correlated_requests_per_minute=60,
            correlation_sources=("workload:general",),
            updated_at=10,
        ),
        DemandForecast(
            "newcomer",
            requests_per_minute=30,
            offered_concurrency=1,
            correlated_requests_per_minute=30,
            correlation_sources=("workload:video",),
            updated_at=10,
        ),
    )

    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        machines,
        (incumbent, newcomer),
        forecasts,
        now=10,
    )

    assert len(plan.preemptions) == 1
    assert plan.preemptions[0].model_id == "incumbent"
    assert plan.preemptions[0].for_model_id == "newcomer"


def test_speculative_model_cannot_take_another_models_only_canary():
    incumbent = model(
        "incumbent",
        8_000,
        min_replicas=0,
        max_replicas=1,
        priority=100,
        min_residency_seconds=0,
    )
    newcomer = replace(incumbent, model_id="newcomer")
    machine = node(
        "n",
        8_000,
        residencies=(ready("incumbent", 8_000),),
        max_models=1,
    )
    forecasts = tuple(
        DemandForecast(
            model_id,
            requests_per_minute=30,
            offered_concurrency=1,
            correlated_requests_per_minute=30,
            correlation_sources=(f"workload:{model_id}",),
            updated_at=10,
        )
        for model_id in ("incumbent", "newcomer")
    )

    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        (machine,),
        (incumbent, newcomer),
        forecasts,
        now=10,
    )

    assert plan.preemptions == ()
    assert plan.nodes_for("incumbent") == ("n",)


def test_speculative_model_may_replace_stale_speculation():
    stale = model(
        "stale",
        8_000,
        min_replicas=0,
        max_replicas=1,
        priority=100,
        min_residency_seconds=0,
    )
    newcomer = replace(stale, model_id="newcomer")
    machine = node("n", 8_000, residencies=(ready("stale", 8_000),), max_models=1)
    forecast = DemandForecast(
        "newcomer",
        requests_per_minute=30,
        offered_concurrency=1,
        correlated_requests_per_minute=30,
        correlation_sources=("workload:video",),
        updated_at=10,
    )

    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        (machine,),
        (stale, newcomer),
        (forecast,),
        now=10,
    )

    assert plan.preempted_pairs == frozenset({("n", "stale")})
    assert plan.preemptions[0].for_model_id == "newcomer"


def test_same_priority_baseline_does_not_preempt_live_direct_service():
    baseline = model(
        "baseline",
        8_000,
        min_replicas=1,
        max_replicas=1,
        priority=100,
        min_residency_seconds=0,
    )
    direct = model(
        "direct",
        8_000,
        min_replicas=0,
        max_replicas=1,
        priority=100,
        min_residency_seconds=0,
    )
    machine = node(
        "n",
        8_000,
        residencies=(ready("direct", 8_000),),
        max_models=1,
    )
    forecast = DemandForecast(
        "direct",
        requests_per_minute=60,
        observed_requests_per_minute=60,
        offered_concurrency=1,
        updated_at=10,
    )

    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        (machine,),
        (baseline, direct),
        (forecast,),
        now=10,
    )

    assert plan.preemptions == ()
    assert plan.nodes_for("direct") == ("n",)
    assert any(item.model_id == "baseline" for item in plan.unsatisfied)


def test_priority_preemption_preserves_ownership_and_minimum_residency():
    critical = model(
        "critical",
        8_000,
        priority=1_000,
        min_residency_seconds=0,
    )
    batch = model(
        "batch",
        8_000,
        priority=10,
        min_residency_seconds=100,
    )
    policy = PlannerPolicy(memory_headroom_fraction=0)
    planner = PlacementPlanner(policy)

    external = node(
        "external",
        8_000,
        residencies=(ready("batch", 8_000, managed=False),),
        manually_managed=True,
        actuator_capabilities=(),
    )
    assert planner.plan((external,), (batch, critical), now=10).preemptions == ()

    recent = node(
        "managed",
        8_000,
        residencies=(ready("batch", 8_000, loaded_at=9),),
    )
    plan = planner.plan((recent,), (batch, critical), now=10)
    assert plan.preempted_pairs == frozenset({("managed", "batch")})
    result = Reconciler().reconcile(
        plan,
        (recent,),
        (batch, critical),
        mode=AllocatorMode.AUTOMATIC,
        now=10,
    )
    assert result.actions == ()
    assert any(item.code == "minimum_residency" for item in result.deferred)


def test_planner_converges_when_model_limit_is_lowered_below_existing_inventory():
    machine = node(
        "n",
        max_models=1,
        residencies=(ready("important", 4_000), ready("background", 4_000)),
    )
    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        [machine],
        [
            model("important", 4_000, priority=100),
            model("background", 4_000, priority=1),
        ],
        now=10,
    )
    assert plan.models_for("n") == ("important",)
    assert any(item.model_id == "background" for item in plan.unsatisfied)
    assert plan.preempted_pairs == frozenset({("n", "background")})


def test_zero_model_limit_selects_no_existing_residency():
    machine = node("n", max_models=0, residencies=(ready(),))
    profile = model(min_residency_seconds=0)
    plan = PlacementPlanner().plan([machine], [profile], now=10)
    assert plan.assignments == ()
    assert plan.preempted_pairs == frozenset({("n", "qwen")})
    result = Reconciler().reconcile(
        plan,
        (machine,),
        (profile,),
        mode=AllocatorMode.AUTOMATIC,
        now=10,
    )
    assert [item.kind for item in result.executable_actions] == [ActionKind.DRAIN]
    assert result.executable_actions[0].reason == (
        "Enforce the host model-capacity policy"
    )


def test_throttled_capacity_fraction_limits_new_placement_memory():
    machine = node("n", capacity_mb=16_000, state=NodeState.THROTTLED)
    plan = PlacementPlanner(
        PlannerPolicy(memory_headroom_fraction=0, throttled_capacity_fraction=0.5)
    ).plan([machine], [model(memory_mb=12_000)], now=10)
    assert plan.assignments == ()


def test_failure_domain_minimum_uses_an_additional_domain_when_feasible():
    machines = [
        node("a1", domain="rack-a"),
        node("a2", domain="rack-a"),
        node("b1", domain="rack-b"),
    ]
    profile = model(min_replicas=2, max_replicas=2, min_failure_domains=2)
    plan = PlacementPlanner().plan(machines, [profile], now=10)
    assert set(plan.nodes_for("qwen")) == {"a1", "b1"}


def test_high_volume_demand_is_aggregated_without_truncation():
    tracker = DemandTracker(
        bucket_seconds=60,
        window_seconds=600,
        max_samples_per_model=4_096,
        ewma_alpha=1,
    )
    for index in range(60_000):
        tracker.observe(
            "qwen",
            service_seconds=0.1,
            timestamp=60 + index / 1_000,
        )
    forecast = tracker.forecast("qwen", now=119.999)
    assert forecast.sample_count == 60_000
    assert forecast.requests_per_minute == pytest.approx(60_000)
    assert forecast.offered_concurrency == pytest.approx(100)


def test_planner_prioritizes_important_model_under_contention():
    machine = node("n", 10_000)
    important = model("important", 8_000, priority=1_000)
    background = model("background", 8_000, priority=10)
    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        [machine], [background, important], now=10
    )
    assert plan.models_for("n") == ("important",)


def test_planner_relocates_a_flexible_model_to_preserve_the_only_large_host():
    planner = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0))
    baseline = model(
        "baseline",
        256,
        min_replicas=1,
        max_replicas=1,
        min_residency_seconds=100,
        scale_down_cooldown_seconds=0,
    )
    specialist = model(
        "specialist",
        714,
        min_replicas=0,
        max_replicas=1,
        min_residency_seconds=0,
        scale_down_cooldown_seconds=0,
    )
    forecasts = (
        DemandForecast(
            "specialist",
            requests_per_minute=10,
            offered_concurrency=0.5,
            observed_requests_per_minute=10,
            updated_at=10,
        ),
    )
    machines = (
        node(
            "small",
            512,
            max_models=1,
            residencies=(
                ModelResidency("baseline", 256, ResidencyState.CACHED),
            ),
        ),
        node(
            "large",
            4_096,
            max_models=1,
            residencies=(
                ready("baseline", 256, last_used_at=10),
                ModelResidency("specialist", 714, ResidencyState.CACHED),
            ),
        ),
    )

    relocating = planner.plan(
        machines,
        (baseline, specialist),
        forecasts=forecasts,
        now=10,
    )

    assert relocating.nodes_for("baseline") == ("small",)
    assert relocating.nodes_for("specialist") == ()
    assert relocating.preemptions == (
        PlacementPreemption("large", "baseline", "specialist"),
    )
    baseline_assignment = next(
        item for item in relocating.assignments if item.model_id == "baseline"
    )
    assert "preserves scarce host for specialist" in baseline_assignment.reasons

    warming_replacement = Reconciler().reconcile(
        relocating,
        machines,
        (baseline, specialist),
        mode=AllocatorMode.AUTOMATIC,
        now=10,
    )
    assert [
        (item.kind, item.node_id, item.model_id)
        for item in warming_replacement.executable_actions
    ] == [(ActionKind.WARM, "small", "baseline")]
    assert any(
        item.node_id == "large"
        and item.model_id == "baseline"
        and item.code == "replacement_not_ready"
        for item in warming_replacement.deferred
    )

    replacement_ready = replace(
        machines[0],
        residencies=(ready("baseline", 256, last_used_at=101),),
        last_heartbeat=101,
    )
    current_large = replace(machines[1], last_heartbeat=101)
    relocated = planner.plan(
        (replacement_ready, current_large),
        (baseline, specialist),
        forecasts=(replace(forecasts[0], updated_at=101),),
        now=101,
    )
    draining_incumbent = Reconciler().reconcile(
        relocated,
        (replacement_ready, current_large),
        (baseline, specialist),
        mode=AllocatorMode.AUTOMATIC,
        now=101,
    )
    assert [
        (item.kind, item.node_id, item.model_id)
        for item in draining_incumbent.executable_actions
    ] == [(ActionKind.DRAIN, "large", "baseline")]

    converged = planner.plan(
        (
            replacement_ready,
            replace(
                current_large,
                residencies=(
                    ModelResidency("baseline", 256, ResidencyState.CACHED),
                    ModelResidency("specialist", 714, ResidencyState.CACHED),
                ),
                last_heartbeat=102,
            ),
        ),
        (baseline, specialist),
        forecasts=(replace(forecasts[0], updated_at=102),),
        now=102,
    )

    assert {
        (item.model_id, item.node_id) for item in converged.assignments
    } == {("baseline", "small"), ("specialist", "large")}
    assert converged.unsatisfied == ()
    assert converged.preemptions == ()


def test_planner_does_not_relocate_without_demand_for_the_constrained_model():
    planner = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0))
    baseline = model(
        "baseline",
        256,
        min_replicas=1,
        max_replicas=1,
        min_residency_seconds=0,
        scale_down_cooldown_seconds=0,
    )
    specialist = model(
        "specialist",
        714,
        min_replicas=0,
        max_replicas=1,
    )
    machines = (
        node(
            "small",
            512,
            max_models=1,
            residencies=(
                ModelResidency("baseline", 256, ResidencyState.CACHED),
            ),
        ),
        node(
            "large",
            4_096,
            max_models=1,
            residencies=(ready("baseline", 256, last_used_at=10),),
        ),
    )

    plan = planner.plan(machines, (baseline, specialist), now=10)

    assert plan.nodes_for("baseline") == ("large",)
    assert plan.preemptions == ()


def test_planner_preserves_dormant_catalog_option_on_an_equivalent_empty_host():
    planner = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0))
    flexible = model(
        "flexible",
        256,
        min_replicas=1,
        max_replicas=1,
        min_residency_seconds=0,
        scale_down_cooldown_seconds=0,
    )
    dormant_specialist = model(
        "dormant-specialist",
        714,
        min_replicas=0,
        max_replicas=1,
    )
    machines = (
        node(
            "general-host",
            1_000,
            max_models=1,
            allowed_models=("flexible",),
        ),
        node(
            "specialized-host",
            1_000,
            max_models=1,
            allowed_models=("flexible", "dormant-specialist"),
            cached=("flexible",),
        ),
    )

    plan = planner.plan(machines, (flexible, dormant_specialist), now=10)

    assert plan.nodes_for("flexible") == ("general-host",)
    assignment = plan.assignments[0]
    assert "preserves scarce host for dormant-specialist" in assignment.reasons
    assert plan.target_for("dormant-specialist") == 0


def test_equal_scarcity_does_not_ping_pong_a_ready_flexible_model():
    planner = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0))
    flexible = model(
        "flexible",
        256,
        min_replicas=1,
        max_replicas=1,
        min_residency_seconds=0,
        scale_down_cooldown_seconds=0,
    )
    specialist_a = model("specialist-a", 700, min_replicas=1, max_replicas=1)
    specialist_b = model("specialist-b", 700, min_replicas=1, max_replicas=1)
    machines = (
        node(
            "a",
            1_000,
            max_models=1,
            allowed_models=("flexible", "specialist-a"),
            residencies=(ready("flexible", 256, last_used_at=10),),
        ),
        node(
            "b",
            1_000,
            max_models=1,
            allowed_models=("flexible", "specialist-b"),
            cached=("flexible",),
        ),
        node(
            "c",
            1_000,
            max_models=1,
            allowed_models=("flexible", "specialist-a"),
            residencies=(ready("specialist-a", 700, last_used_at=10),),
        ),
        node(
            "d",
            1_000,
            max_models=1,
            allowed_models=("flexible", "specialist-b"),
            residencies=(ready("specialist-b", 700, last_used_at=10),),
        ),
    )

    plan = planner.plan(machines, (flexible, specialist_a, specialist_b), now=10)

    assert plan.nodes_for("flexible") == ("a",)
    assert plan.nodes_for("specialist-a") == ("c",)
    assert plan.nodes_for("specialist-b") == ("d",)
    assert plan.preemptions == ()


def test_planner_repacks_when_a_materially_better_host_has_a_beneficiary():
    planner = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0))
    baseline = model(
        "baseline",
        256,
        min_replicas=1,
        max_replicas=1,
        min_residency_seconds=0,
        scale_down_cooldown_seconds=0,
    )
    specialist = model(
        "specialist",
        714,
        min_replicas=0,
        max_replicas=1,
        min_residency_seconds=0,
        scale_down_cooldown_seconds=0,
    )
    forecast = DemandForecast(
        "specialist",
        requests_per_minute=10,
        offered_concurrency=0.5,
        observed_requests_per_minute=10,
        updated_at=10,
    )
    machines = (
        node(
            "small",
            512,
            max_models=1,
            host_priority=0,
            residencies=(
                ModelResidency("baseline", 256, ResidencyState.CACHED),
            ),
        ),
        node(
            "medium",
            2_048,
            max_models=1,
            host_priority=1,
            residencies=(
                ready("baseline", 256, last_used_at=10),
                ModelResidency("specialist", 714, ResidencyState.CACHED),
            ),
        ),
        node(
            "large",
            4_096,
            max_models=1,
            host_priority=10,
            cached=("specialist",),
        ),
    )

    staged = planner.plan(
        machines,
        (baseline, specialist),
        forecasts=(forecast,),
        now=10,
    )

    assert staged.nodes_for("baseline") == ("small",)
    assert staged.nodes_for("specialist") == ()
    assert staged.preemptions == (
        PlacementPreemption("medium", "baseline", "specialist"),
    )


def test_planner_uses_immediate_capacity_when_repack_gain_is_only_marginal():
    planner = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0))
    baseline = model(
        "baseline",
        256,
        min_replicas=1,
        max_replicas=1,
        min_residency_seconds=100,
        scale_down_cooldown_seconds=0,
    )
    specialist = model(
        "specialist",
        714,
        min_replicas=0,
        max_replicas=1,
        scale_down_cooldown_seconds=0,
    )
    forecast = DemandForecast(
        "specialist",
        requests_per_minute=10,
        offered_concurrency=0.5,
        observed_requests_per_minute=10,
        updated_at=10,
    )
    machines = (
        node(
            "small",
            512,
            max_models=1,
            host_priority=0,
            residencies=(
                ModelResidency("baseline", 256, ResidencyState.CACHED),
            ),
        ),
        node(
            "medium",
            4_096,
            max_models=1,
            host_priority=0,
            residencies=(
                ready("baseline", 256, last_used_at=10),
                ModelResidency("specialist", 714, ResidencyState.CACHED),
            ),
        ),
        node(
            "large",
            4_096,
            max_models=1,
            host_priority=1,
            cached=("specialist",),
        ),
    )

    plan = planner.plan(
        machines,
        (baseline, specialist),
        forecasts=(forecast,),
        now=10,
    )

    assert plan.nodes_for("baseline") == ("medium",)
    assert plan.nodes_for("specialist") == ("large",)
    assert plan.preemptions == ()


def test_planner_repacking_finds_capacity_path_independent_of_ineligible_nodes():
    machines = [
        node(
            "n0",
            4,
            tags=("m0", "m1", "m2", "m4", "m5"),
            max_models=2,
        ),
        node(
            "n1",
            4,
            tags=("m0", "m3", "m4", "m5"),
            max_models=3,
        ),
        node("n2", 6, tags=("m1", "m2", "m3"), max_models=3),
    ]
    profiles = [
        model(
            model_id,
            memory_mb,
            required_tags=(model_id,),
            min_replicas=1,
            max_replicas=1,
            min_residency_seconds=0,
            scale_down_cooldown_seconds=0,
        )
        for model_id, memory_mb in (
            ("m0", 3),
            ("m1", 1),
            ("m2", 3),
            ("m3", 4),
            ("m4", 1),
            ("m5", 1),
        )
    ]
    planner = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0))
    baseline = planner.plan(machines, profiles, now=10)
    with_irrelevant = planner.plan(
        [*machines, node("paused", 100, state=NodeState.PAUSED)],
        profiles,
        now=10,
    )

    baseline_pairs = {(item.model_id, item.node_id) for item in baseline.assignments}
    assert baseline.unsatisfied == ()
    assert with_irrelevant.unsatisfied == ()
    assert {
        (item.model_id, item.node_id) for item in with_irrelevant.assignments
    } == baseline_pairs
    assert {
        ("m0", "n1"),
        ("m1", "n2"),
        ("m2", "n0"),
        ("m3", "n2"),
    }.issubset(baseline_pairs)
    assert {baseline.nodes_for("m4")[0], baseline.nodes_for("m5")[0]} == {"n0", "n1"}


def test_planner_repacking_can_evict_multiple_smaller_victims():
    capacities = (6, 5, 4, 5)
    model_limits = (2, 3, 3, 3)
    memories = (3, 3, 3, 2, 2, 1)
    eligibility_masks = (0b1010, 0b1000, 0b1110, 0b0101, 0b1100, 0b1011)
    machines = [
        node(
            f"n{node_index}",
            capacities[node_index],
            tags=tuple(
                f"m{model_index}"
                for model_index, mask in enumerate(eligibility_masks)
                if mask & (1 << node_index)
            ),
            max_models=model_limits[node_index],
        )
        for node_index in range(4)
    ]
    profiles = [
        model(
            f"m{index}",
            memories[index],
            required_tags=(f"m{index}",),
            min_replicas=1,
            max_replicas=1,
            min_residency_seconds=0,
            scale_down_cooldown_seconds=0,
        )
        for index in range(6)
    ]

    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        machines,
        profiles,
        now=10,
    )

    assert plan.unsatisfied == ()
    assert {(item.model_id, item.node_id) for item in plan.assignments} == {
        ("m0", "n1"),
        ("m1", "n3"),
        ("m2", "n2"),
        ("m3", "n0"),
        ("m4", "n3"),
        ("m5", "n1"),
    }


def test_planner_repacking_preserves_an_existing_failure_domain_floor():
    machines = [
        node("n1", 100, tags=("a", "c"), domain="d1", max_models=1),
        node("n2", 100, tags=("a", "c"), domain="d2", max_models=1),
        node("n3", 100, tags=("b", "c"), domain="d3", max_models=1, cached=("b",)),
        node("n4", 100, tags=("a", "b"), domain="d2", max_models=1),
    ]
    profiles = [
        model(
            "a",
            100,
            required_tags=("a",),
            min_replicas=2,
            max_replicas=2,
            min_failure_domains=2,
        ),
        model("b", 100, required_tags=("b",)),
        model("c", 100, required_tags=("c",)),
    ]

    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        machines,
        profiles,
        now=10,
    )
    node_by_id = {item.node_id: item for item in machines}
    a_domains = {
        node_by_id[item.node_id].failure_domain
        for item in plan.assignments
        if item.model_id == "a"
    }

    assert len(plan.assignments) == 4
    assert a_domains == {"d1", "d2"}
    assert plan.unsatisfied == ()


def test_planner_repacks_before_accepting_an_avoidable_same_domain_replica():
    machines = [
        node("n0", 7, domain="d0", max_models=2),
        node("n1", 6, domain="d0", max_models=2),
        node("n2", 6, domain="d1", max_models=1),
    ]
    profiles = [
        model(
            "m0",
            2,
            min_replicas=2,
            max_replicas=2,
            min_failure_domains=2,
        ),
        model(
            "m1",
            4,
            min_replicas=2,
            max_replicas=2,
            min_failure_domains=1,
        ),
    ]

    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        machines,
        profiles,
        now=10,
    )
    node_by_id = {item.node_id: item for item in machines}
    m0_domains = {
        node_by_id[item.node_id].failure_domain
        for item in plan.assignments
        if item.model_id == "m0"
    }

    assert plan.unsatisfied == ()
    assert m0_domains == {"d0", "d1"}
    assert "n2" in plan.nodes_for("m0")
    assert len(plan.nodes_for("m0")) == 2
    assert len(plan.nodes_for("m1")) == 2


def test_planner_output_does_not_depend_on_input_order():
    machines = [node("b", domain="b"), node("a", domain="a")]
    profiles = [model("small", 4_000), model("large", 8_000)]
    planner = PlacementPlanner()
    first = planner.plan(machines, profiles, now=10)
    second = planner.plan(reversed(machines), reversed(profiles), now=10)
    assert first == second


def test_counterfactual_plan_skips_identity_hash_without_changing_decisions():
    machines = [node("b", domain="b"), node("a", domain="a")]
    profiles = [model("small", 4_000), model("large", 8_000)]
    planner = PlacementPlanner()

    authoritative = planner.plan(machines, profiles, now=10)
    counterfactual = planner.plan(
        machines,
        profiles,
        now=10,
        compute_input_digest=False,
    )

    assert authoritative.input_digest
    assert counterfactual.input_digest == ""
    assert counterfactual.generation == "evaluation-0000000010000"
    assert counterfactual.assignments == authoritative.assignments
    assert counterfactual.desired_replicas == authoritative.desired_replicas
    assert counterfactual.unsatisfied == authoritative.unsatisfied
    assert counterfactual.preemptions == authoritative.preemptions
    with pytest.raises(ValueError, match="compute_input_digest must be boolean"):
        planner.plan(machines, profiles, compute_input_digest=1)  # type: ignore[arg-type]


def test_reconciler_proposes_load_then_dependent_warm():
    machine = node("n")
    profile = model()
    plan = PlacementPlanner().plan([machine], [profile], now=10)
    result = Reconciler().reconcile(plan, [machine], [profile], now=10)
    assert [item.kind for item in result.actions] == [ActionKind.LOAD, ActionKind.WARM]
    assert result.actions[1].dependencies == (result.actions[0].action_id,)
    assert not any(item.executable for item in result.actions)


def test_reconciler_carries_bounded_immutable_source_only_on_load():
    machine = node("n", disk_capacity_mb=10_000, disk_available_mb=8_000)
    profile = model(
        artifact_sha256="a" * 64,
        artifact_source="hf://owner/repo/qwen.gguf",
        artifact_size_mb=4_000,
    )
    plan = PlacementPlanner().plan([machine], [profile], now=10)

    result = Reconciler().reconcile(plan, [machine], [profile], now=10)

    load, warm = result.actions
    assert load.kind == ActionKind.LOAD
    assert load.artifact_source == "hf://owner/repo/qwen.gguf"
    assert load.artifact_size_mb == 4_000
    assert warm.kind == ActionKind.WARM
    assert warm.artifact_source == ""
    assert warm.artifact_size_mb == 0


def test_planner_rejects_cold_host_without_artifact_disk_and_uses_fitting_peer():
    too_full = node(
        "a-full",
        disk_capacity_mb=100_000,
        disk_available_mb=3_999,
    )
    fitting = node(
        "z-fitting",
        disk_capacity_mb=100_000,
        disk_available_mb=4_000,
    )
    profile = model(
        artifact_sha256="a" * 64,
        artifact_source="hf://owner/repo/qwen.gguf",
        artifact_size_mb=4_000,
    )

    plan = PlacementPlanner().plan([too_full, fitting], [profile], now=10)

    assert plan.nodes_for("qwen") == ("z-fitting",)


def test_cached_artifact_remains_eligible_when_free_disk_is_below_artifact_size():
    digest = "a" * 64
    cached = ModelResidency(
        "qwen",
        8_000,
        state=ResidencyState.CACHED,
        artifact_sha256=digest,
    )
    machine = node(
        "cached",
        residencies=(cached,),
        disk_capacity_mb=100_000,
        disk_available_mb=1,
    )
    profile = model(
        artifact_sha256=digest,
        artifact_source="hf://owner/repo/qwen.gguf",
        artifact_size_mb=4_000,
    )

    assert PlacementPlanner().plan([machine], [profile], now=10).nodes_for("qwen") == (
        "cached",
    )


def test_reconciler_skips_download_when_weights_are_cached():
    machine = node("n", cached=("qwen",))
    profile = model()
    plan = PlacementPlanner().plan([machine], [profile], now=10)
    result = Reconciler().reconcile(plan, [machine], [profile], now=10)
    assert [item.kind for item in result.actions] == [ActionKind.WARM]


def test_reconciler_observe_mode_returns_drift_without_actions():
    machine = node("n")
    profile = model()
    plan = PlacementPlanner().plan([machine], [profile], now=10)
    result = Reconciler().reconcile(
        plan, [machine], [profile], mode=AllocatorMode.OBSERVE, now=10
    )
    assert result.actions == ()
    assert {item.code for item in result.deferred} == {"observe_mode"}


def test_reconciler_automatic_mode_applies_global_and_per_node_governor():
    machines = [node("a"), node("b")]
    profile = model(min_replicas=2, max_replicas=2)
    plan = PlacementPlanner().plan(machines, [profile], now=10)
    reconciler = Reconciler(
        ReconcilePolicy(max_concurrent_mutations=2, max_mutations_per_node=1)
    )
    result = reconciler.reconcile(
        plan, machines, [profile], mode=AllocatorMode.AUTOMATIC, now=10
    )
    assert len(result.executable_actions) == 2
    assert {item.node_id for item in result.executable_actions} == {"a", "b"}
    assert any(item.code == "node_mutation_limit" for item in result.deferred)


def test_mutation_governor_starts_fastest_learned_cold_path_first():
    machines = (node("a-slow"), node("z-fast"))
    profiles = (
        model("slow", pinned_nodes=("a-slow",), load_seconds=10),
        model("fast", pinned_nodes=("z-fast",), load_seconds=10),
    )
    plan = PlacementPlanner().plan(machines, profiles, now=10)

    result = Reconciler(ReconcilePolicy(max_concurrent_mutations=1)).reconcile(
        plan,
        machines,
        profiles,
        mode=AllocatorMode.AUTOMATIC,
        now=10,
        load_seconds={
            ("a-slow", "slow"): 100,
            ("z-fast", "fast"): 2,
        },
    )

    assert [(item.kind, item.model_id) for item in result.executable_actions] == [
        (ActionKind.LOAD, "fast")
    ]


def test_mutation_governor_starts_higher_priority_model_before_node_id_order():
    machines = [
        node("a-batch"),
        node("z-critical", cached=("critical",)),
    ]
    profiles = [
        model("batch", priority=1, pinned_nodes=("a-batch",)),
        model("critical", priority=1_000, pinned_nodes=("z-critical",)),
    ]
    plan = PlacementPlanner().plan(machines, profiles, now=10)
    result = Reconciler(ReconcilePolicy(max_concurrent_mutations=1)).reconcile(
        plan,
        machines,
        profiles,
        mode=AllocatorMode.AUTOMATIC,
        now=10,
    )

    assert [(item.kind, item.model_id) for item in result.executable_actions] == [
        (ActionKind.WARM, "critical")
    ]


def test_mutation_governor_starts_direct_demand_before_speculative_prewarm():
    machines = [
        node("a-speculative", cached=("speculative",), tags=("speculative",)),
        node("z-direct", cached=("direct",), tags=("direct",)),
    ]
    profiles = [
        model(
            "speculative",
            min_replicas=0,
            max_replicas=1,
            required_tags=("speculative",),
        ),
        model(
            "direct",
            min_replicas=0,
            max_replicas=1,
            required_tags=("direct",),
        ),
    ]
    forecasts = [
        DemandForecast(
            "speculative",
            requests_per_minute=60,
            correlated_requests_per_minute=60,
            correlation_confidence=1,
            correlation_sources=("source",),
            updated_at=10,
        ),
        DemandForecast(
            "direct",
            requests_per_minute=60,
            observed_requests_per_minute=60,
            offered_concurrency=1,
            updated_at=10,
        ),
    ]
    plan = PlacementPlanner().plan(machines, profiles, forecasts, now=10)
    assert plan.urgency_for("speculative") == 1
    assert plan.urgency_for("direct") == 2

    result = Reconciler(ReconcilePolicy(max_concurrent_mutations=1)).reconcile(
        plan,
        machines,
        profiles,
        mode=AllocatorMode.AUTOMATIC,
        now=10,
    )

    assert [(item.kind, item.model_id) for item in result.executable_actions] == [
        (ActionKind.WARM, "direct")
    ]


def test_reconciler_suppresses_speculative_warm_during_service_shortfall():
    machines = [
        node("a-direct", cached=("direct",), max_models=1),
        node("z-speculative", cached=("speculative",), max_models=1),
    ]
    profiles = [
        model("direct", min_replicas=0, max_replicas=2),
        model("speculative", min_replicas=0, max_replicas=2),
    ]
    plan = PlacementPlan(
        generation="test",
        created_at=10,
        assignments=(
            PlacementAssignment("direct", "a-direct", 8_000, 0, 1),
            PlacementAssignment("speculative", "z-speculative", 8_000, 0, 1),
        ),
        desired_replicas=(("direct", 2), ("speculative", 2)),
        unsatisfied=(
            UnsatisfiedConstraint(
                "direct",
                "insufficient_capacity",
                "Placed 1 of 2 desired replicas",
                1,
            ),
        ),
        model_urgencies=(("direct", 2), ("speculative", 1)),
    )

    result = Reconciler().reconcile(
        plan,
        machines,
        profiles,
        mode=AllocatorMode.AUTOMATIC,
        now=10,
    )

    assert [(item.kind, item.model_id) for item in result.executable_actions] == [
        (ActionKind.WARM, "direct")
    ]
    assert any(
        item.model_id == "speculative" and item.code == "service_capacity_unsatisfied"
        for item in result.deferred
    )


def test_mutation_governor_prefers_fast_cached_start_within_service_class():
    machines = [
        node("a-cold"),
        node("z-cached", cached=("cached",)),
    ]
    profiles = [
        model(
            "cold",
            pinned_nodes=("a-cold",),
            load_seconds=60,
            warm_seconds=5,
        ),
        model(
            "cached",
            pinned_nodes=("z-cached",),
            load_seconds=60,
            warm_seconds=5,
        ),
    ]
    plan = PlacementPlanner().plan(machines, profiles, now=10)

    result = Reconciler(ReconcilePolicy(max_concurrent_mutations=1)).reconcile(
        plan,
        machines,
        profiles,
        mode=AllocatorMode.AUTOMATIC,
        now=10,
    )

    assert [(item.kind, item.model_id) for item in result.executable_actions] == [
        (ActionKind.WARM, "cached")
    ]
    assert any(
        item.kind == ActionKind.LOAD
        and item.model_id == "cold"
        and item.code == "global_mutation_limit"
        for item in result.deferred
    )


def test_mutation_governor_preserves_replica_round_fairness():
    machines = [
        node(node_id, cached=(model_id,))
        for node_id, model_id in (
            ("a-alpha-0", "alpha"),
            ("b-alpha-1", "alpha"),
            ("y-beta-0", "beta"),
            ("z-beta-1", "beta"),
        )
    ]
    profiles = [
        model(
            "alpha",
            min_replicas=2,
            max_replicas=2,
            pinned_nodes=("a-alpha-0", "b-alpha-1"),
        ),
        model(
            "beta",
            min_replicas=2,
            max_replicas=2,
            pinned_nodes=("y-beta-0", "z-beta-1"),
        ),
    ]
    plan = PlacementPlanner().plan(machines, profiles, now=10)

    result = Reconciler(ReconcilePolicy(max_concurrent_mutations=2)).reconcile(
        plan,
        machines,
        profiles,
        mode=AllocatorMode.AUTOMATIC,
        now=10,
    )

    assert [item.model_id for item in result.executable_actions] == [
        "alpha",
        "beta",
    ]


def test_mutation_governor_preserves_replica_round_fairness_during_preemption():
    incumbent_pairs = (
        ("a-alpha-0", "old-a"),
        ("b-alpha-1", "old-b"),
        ("y-beta-0", "old-y"),
        ("z-beta-1", "old-z"),
    )
    machines = [
        node(
            node_id,
            capacity_mb=8_000,
            max_models=1,
            residencies=(
                ModelResidency(
                    incumbent,
                    8_000,
                    ResidencyState.READY,
                    loaded_at=1,
                ),
            ),
        )
        for node_id, incumbent in incumbent_pairs
    ]
    profiles = [
        model(
            incumbent,
            min_replicas=0,
            max_replicas=0,
            priority=1,
            min_residency_seconds=0,
        )
        for _, incumbent in incumbent_pairs
    ]
    profiles.extend(
        (
            model(
                "alpha",
                min_replicas=2,
                max_replicas=2,
                priority=100,
                pinned_nodes=("a-alpha-0", "b-alpha-1"),
            ),
            model(
                "beta",
                min_replicas=2,
                max_replicas=2,
                priority=100,
                pinned_nodes=("y-beta-0", "z-beta-1"),
            ),
        )
    )
    plan = PlacementPlanner().plan(machines, profiles, now=10)

    result = Reconciler(ReconcilePolicy(max_concurrent_mutations=2)).reconcile(
        plan,
        machines,
        profiles,
        mode=AllocatorMode.AUTOMATIC,
        now=10,
    )

    beneficiary_by_pair = {
        (item.node_id, item.model_id): item.for_model_id for item in plan.preemptions
    }
    assert [
        beneficiary_by_pair[(item.node_id, item.model_id)]
        for item in result.executable_actions
    ] == ["alpha", "beta"]
    assert all(item.kind == ActionKind.DRAIN for item in result.executable_actions)


def test_mutation_governor_finishes_nearest_capacity_release_group_first():
    machines = [
        node(
            "a-two-victims",
            capacity_mb=8_000,
            max_models=2,
            residencies=(
                ModelResidency("old-a1", 4_000, ResidencyState.READY, loaded_at=1),
                ModelResidency("old-a2", 4_000, ResidencyState.READY, loaded_at=1),
            ),
        ),
        node(
            "z-one-victim",
            capacity_mb=8_000,
            max_models=2,
            residencies=(
                ModelResidency("old-z", 8_000, ResidencyState.READY, loaded_at=1),
            ),
        ),
    ]
    profiles = [
        model(
            model_id,
            memory_mb,
            min_replicas=0,
            max_replicas=0,
            priority=1,
            min_residency_seconds=0,
        )
        for model_id, memory_mb in (
            ("old-a1", 4_000),
            ("old-a2", 4_000),
            ("old-z", 8_000),
        )
    ]
    profiles.extend(
        (
            model(
                "alpha",
                8_000,
                pinned_nodes=("a-two-victims",),
                priority=100,
            ),
            model(
                "beta",
                8_000,
                pinned_nodes=("z-one-victim",),
                priority=100,
            ),
        )
    )
    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        machines,
        profiles,
        now=10,
    )

    result = Reconciler(ReconcilePolicy(max_concurrent_mutations=1)).reconcile(
        plan,
        machines,
        profiles,
        mode=AllocatorMode.AUTOMATIC,
        now=10,
    )

    selected = result.executable_actions[0]
    beneficiary_by_pair = {
        (item.node_id, item.model_id): item.for_model_id for item in plan.preemptions
    }
    assert beneficiary_by_pair[(selected.node_id, selected.model_id)] == "beta"


def test_reconciler_indexes_high_cardinality_inputs_once(
    monkeypatch,
):
    machines = [
        node(f"node-{index}", cached=(f"model-{index}",)) for index in range(64)
    ]
    profiles = [
        model(
            f"model-{index}",
            pinned_nodes=(f"node-{index}",),
        )
        for index in range(64)
    ]
    plan = PlacementPlanner().plan(machines, profiles, now=10)

    class CountingAssignments(tuple):
        def __new__(cls, values):
            instance = super().__new__(cls, values)
            instance.iterations = 0
            return instance

        def __iter__(self):
            self.iterations += 1
            return super().__iter__()

    assignments = CountingAssignments(plan.assignments)
    plan = replace(plan, assignments=assignments)
    assignments.iterations = 0

    def unexpected_lookup(_plan, _model_id):
        raise AssertionError("per-action urgency lookup rebuilt the plan index")

    residency_lookups = 0
    original_residency = NodeSnapshot.residency

    def counted_residency(snapshot, model_id):
        nonlocal residency_lookups
        residency_lookups += 1
        return original_residency(snapshot, model_id)

    matching_history_sizes = []
    original_blocked_until = Reconciler._history_blocked_until

    def counted_blocked_until(reconciler, matching, now):
        matching_history_sizes.append(len(matching))
        return original_blocked_until(reconciler, matching, now)

    monkeypatch.setattr(type(plan), "urgency_for", unexpected_lookup)
    monkeypatch.setattr(NodeSnapshot, "residency", counted_residency)
    monkeypatch.setattr(Reconciler, "_history_blocked_until", counted_blocked_until)
    history = tuple(
        MutationRecord(
            f"attempt-{index}",
            ActionKind.WARM,
            f"node-{index}",
            f"model-{index}",
            MutationStatus.CANCELLED,
            attempted_at=1,
            completed_at=1,
        )
        for index in range(64)
    )

    result = Reconciler(ReconcilePolicy(mutation_cooldown_seconds=0)).reconcile(
        plan, machines, profiles, history, now=10
    )

    assert len(result.actions) == 64
    assert assignments.iterations <= 5
    assert residency_lookups <= len(machines) * 2
    assert matching_history_sizes == [1] * len(machines)


def test_reconciler_indexes_plan_once_for_broad_retirement_wave():
    machines = tuple(
        node(
            f"node-{index}",
            residencies=(
                replace(
                    ready(f"old-{index}"),
                    state=ResidencyState.DRAINING,
                ),
            ),
        )
        for index in range(64)
    )
    profiles = tuple(
        model(
            f"old-{index}",
            min_replicas=0,
            max_replicas=0,
            min_residency_seconds=0,
            scale_down_cooldown_seconds=0,
        )
        for index in range(64)
    )
    plan = PlacementPlanner().plan(machines, profiles, now=10)
    assert plan.assignments == ()

    class CountingAssignments(tuple):
        def __new__(cls, values):
            instance = super().__new__(cls, values)
            instance.iterations = 0
            return instance

        def __iter__(self):
            self.iterations += 1
            return super().__iter__()

    assignments = CountingAssignments(plan.assignments)
    plan = replace(plan, assignments=assignments)
    assignments.iterations = 0

    result = Reconciler().reconcile(plan, machines, profiles, now=10)

    assert len(result.actions) == len(machines)
    assert {action.kind for action in result.actions} == {ActionKind.UNLOAD}
    assert assignments.iterations <= 5


def test_constructive_command_revalidation_skips_destructive_safety_index(monkeypatch):
    machine = node("n", cached=("qwen",))
    profile = model()
    plan = PlacementPlanner().plan((machine,), (profile,), now=10)
    warm = Reconciler().reconcile(plan, (machine,), (profile,), now=10).actions[0]

    def unexpected_safety_index(*_args, **_kwargs):
        raise AssertionError(
            "constructive-only validation built destructive safety state"
        )

    monkeypatch.setattr(
        "shared.allocator.reconcile._destructive_safety_state",
        unexpected_safety_index,
    )

    assert (
        Reconciler().destructive_command_deferrals(
            plan,
            (machine,),
            (profile,),
            (warm,),
            now=10,
        )
        == {}
    )


def test_dense_host_retirement_reuses_observed_residencies(monkeypatch):
    count = 64
    residencies = tuple(
        replace(ready(f"old-{index}"), state=ResidencyState.DRAINING)
        for index in range(count)
    )
    machine = node("dense", 8_000 * count, residencies=residencies)
    profiles = tuple(
        model(
            f"old-{index}",
            min_replicas=0,
            max_replicas=0,
            min_residency_seconds=0,
            scale_down_cooldown_seconds=0,
        )
        for index in range(count)
    )
    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        (machine,),
        profiles,
        now=10,
    )
    residency_lookups = 0
    original_residency = NodeSnapshot.residency

    def counted_residency(snapshot, model_id):
        nonlocal residency_lookups
        residency_lookups += 1
        return original_residency(snapshot, model_id)

    monkeypatch.setattr(NodeSnapshot, "residency", counted_residency)

    result = Reconciler().reconcile(plan, (machine,), profiles, now=10)

    assert len(result.actions) == count
    assert {action.kind for action in result.actions} == {ActionKind.UNLOAD}
    assert residency_lookups <= 1


def test_dense_preemption_wave_indexes_victim_residencies_once(monkeypatch):
    count = 64
    residencies = tuple(ready(f"batch-{index}", 1_000) for index in range(count))
    machine = node("dense", count * 1_000, residencies=residencies)
    profiles = (
        *(
            model(
                f"batch-{index}",
                1_000,
                min_replicas=0,
                max_replicas=0,
                priority=1,
                min_residency_seconds=0,
                scale_down_cooldown_seconds=0,
            )
            for index in range(count)
        ),
        model(
            "critical",
            count * 1_000,
            priority=1_000,
            min_residency_seconds=0,
        ),
    )
    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        (machine,),
        profiles,
        now=10,
    )
    assert len(plan.preemptions) == count
    residency_lookups = 0
    original_residency = NodeSnapshot.residency

    def counted_residency(snapshot, model_id):
        nonlocal residency_lookups
        residency_lookups += 1
        return original_residency(snapshot, model_id)

    monkeypatch.setattr(NodeSnapshot, "residency", counted_residency)

    result = Reconciler().reconcile(plan, (machine,), profiles, now=10)

    assert len(result.actions) == count
    assert {action.kind for action in result.actions} == {ActionKind.DRAIN}
    assert residency_lookups == 0


def test_mutation_governor_prioritizes_drain_for_preemption_beneficiary():
    machines = [
        node("a-routine", 8_000, residencies=(ready("obsolete", 8_000),)),
        node("z-preempt", 8_000, residencies=(ready("batch", 8_000),)),
    ]
    profiles = [
        model(
            "batch",
            8_000,
            min_replicas=0,
            max_replicas=0,
            priority=1,
            min_residency_seconds=0,
            scale_down_cooldown_seconds=0,
        ),
        model(
            "critical",
            8_000,
            priority=1_000,
            min_residency_seconds=0,
        ),
        model(
            "obsolete",
            8_000,
            min_replicas=0,
            max_replicas=0,
            priority=500,
            min_residency_seconds=0,
            scale_down_cooldown_seconds=0,
        ),
    ]
    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        machines,
        profiles,
        now=10,
    )
    assert plan.preempted_pairs == frozenset({("z-preempt", "batch")})

    result = Reconciler(ReconcilePolicy(max_concurrent_mutations=1)).reconcile(
        plan,
        machines,
        profiles,
        mode=AllocatorMode.AUTOMATIC,
        now=10,
    )

    assert [
        (item.kind, item.node_id, item.model_id) for item in result.executable_actions
    ] == [(ActionKind.DRAIN, "z-preempt", "batch")]


def test_preemption_prefetches_exact_beneficiary_before_disrupting_service():
    machine = node(
        "scarce",
        8_000,
        residencies=(ready("batch", 8_000),),
        disk_capacity_mb=32_000,
        disk_available_mb=16_000,
    )
    batch = model(
        "batch",
        8_000,
        min_replicas=0,
        max_replicas=0,
        priority=1,
        min_residency_seconds=0,
        scale_down_cooldown_seconds=0,
    )
    critical = model(
        "critical",
        8_000,
        priority=1_000,
        min_residency_seconds=0,
        artifact_sha256="a" * 64,
        artifact_source="hf://example/models/critical.gguf",
        artifact_size_mb=4_000,
    )
    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        (machine,),
        (batch, critical),
        now=10,
    )
    assert plan.preemptions == (
        PlacementPreemption("scarce", "batch", "critical"),
    )

    result = Reconciler(ReconcilePolicy(max_concurrent_mutations=1)).reconcile(
        plan,
        (machine,),
        (batch, critical),
        mode=AllocatorMode.AUTOMATIC,
        now=10,
    )

    assert [
        (item.kind, item.node_id, item.model_id, item.artifact_source)
        for item in result.executable_actions
    ] == [
        (
            ActionKind.LOAD,
            "scarce",
            "critical",
            "hf://example/models/critical.gguf",
        )
    ]
    assert any(
        item.kind == ActionKind.DRAIN
        and item.node_id == "scarce"
        and item.model_id == "batch"
        and item.code == "beneficiary_artifact_not_cached"
        for item in result.deferred
    )

    loading_machine = replace(
        machine,
        residencies=(
            ready("batch", 8_000),
            ModelResidency(
                "critical",
                8_000,
                ResidencyState.LOADING,
                artifact_sha256="a" * 64,
            ),
        ),
        last_heartbeat=10.5,
    )
    while_loading = Reconciler(
        ReconcilePolicy(max_concurrent_mutations=1)
    ).reconcile(
        plan,
        (loading_machine,),
        (batch, critical),
        mode=AllocatorMode.AUTOMATIC,
        now=10.5,
    )
    assert while_loading.executable_actions == ()
    assert any(
        item.kind == ActionKind.DRAIN
        and item.node_id == "scarce"
        and item.model_id == "batch"
        and item.code == "beneficiary_artifact_not_cached"
        for item in while_loading.deferred
    )

    cached_machine = replace(
        machine,
        residencies=(
            ready("batch", 8_000),
            ModelResidency(
                "critical",
                8_000,
                ResidencyState.CACHED,
                artifact_sha256="a" * 64,
            ),
        ),
        last_heartbeat=11,
    )
    after_prefetch = Reconciler(ReconcilePolicy(max_concurrent_mutations=1)).reconcile(
        plan,
        (cached_machine,),
        (batch, critical),
        mode=AllocatorMode.AUTOMATIC,
        now=11,
    )
    assert [
        (item.kind, item.node_id, item.model_id)
        for item in after_prefetch.executable_actions
    ] == [(ActionKind.DRAIN, "scarce", "batch")]


def test_correlation_only_demand_prefetches_exact_artifact_without_eviction():
    machine = node(
        "scarce",
        8_000,
        residencies=(ready("baseline", 8_000),),
        max_models=1,
        disk_capacity_mb=32_000,
        disk_available_mb=16_000,
    )
    baseline = model(
        "baseline",
        8_000,
        min_replicas=1,
        max_replicas=1,
        priority=100,
        min_residency_seconds=0,
    )
    predicted = model(
        "predicted",
        8_000,
        min_replicas=0,
        max_replicas=1,
        priority=100,
        artifact_sha256="b" * 64,
        artifact_source="hf://example/models/predicted.gguf",
        artifact_size_mb=4_000,
    )
    forecast = DemandForecast(
        "predicted",
        requests_per_minute=6,
        offered_concurrency=1,
        confidence=0.9,
        correlated_requests_per_minute=6,
        correlation_confidence=0.9,
        correlation_sources=("workload-predictor:image",),
        sample_count=10,
        updated_at=10,
    )
    planner = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0))
    plan = planner.plan(
        (machine,),
        (baseline, predicted),
        (forecast,),
        now=10,
    )

    assert plan.nodes_for("baseline") == ("scarce",)
    assert plan.nodes_for("predicted") == ()
    assert plan.preemptions == ()
    assert plan.artifact_prefetches == (ArtifactPrefetch("scarce", "predicted"),)
    assert plan.to_dict()["artifact_prefetches"] == [
        {"node_id": "scarce", "model_id": "predicted"}
    ]

    result = Reconciler(ReconcilePolicy(max_concurrent_mutations=1)).reconcile(
        plan,
        (machine,),
        (baseline, predicted),
        mode=AllocatorMode.AUTOMATIC,
        now=10,
    )
    assert [
        (item.kind, item.node_id, item.model_id, item.artifact_source)
        for item in result.executable_actions
    ] == [
        (
            ActionKind.LOAD,
            "scarce",
            "predicted",
            "hf://example/models/predicted.gguf",
        )
    ]
    assert all(
        item.kind not in (ActionKind.WARM, ActionKind.DRAIN, ActionKind.UNLOAD)
        for item in result.actions
    )

    cached_machine = replace(
        machine,
        residencies=(
            ready("baseline", 8_000),
            ModelResidency(
                "predicted",
                8_000,
                ResidencyState.CACHED,
                artifact_sha256="b" * 64,
            ),
        ),
    )
    cached_plan = planner.plan(
        (cached_machine,),
        (baseline, predicted),
        (forecast,),
        now=11,
    )
    assert cached_plan.artifact_prefetches == ()

    disabled = PlacementPlanner(
        PlannerPolicy(
            memory_headroom_fraction=0,
            max_predictive_artifact_prefetches=0,
        )
    ).plan(
        (machine,),
        (baseline, predicted),
        (forecast,),
        now=10,
    )
    assert disabled.artifact_prefetches == ()

    higher_pressure = model(
        "higher-pressure",
        8_000,
        min_replicas=0,
        max_replicas=1,
        priority=100,
        artifact_sha256="c" * 64,
        artifact_source="hf://example/models/higher-pressure.gguf",
        artifact_size_mb=7_000,
    )
    higher_forecast = replace(
        forecast,
        model_id="higher-pressure",
        requests_per_minute=12,
        offered_concurrency=2,
        correlated_requests_per_minute=12,
    )
    disk_bounded = PlacementPlanner(
        PlannerPolicy(
            memory_headroom_fraction=0,
            max_predictive_artifact_prefetches=2,
        )
    ).plan(
        (replace(machine, disk_available_mb=10_000),),
        (baseline, predicted, higher_pressure),
        (forecast, higher_forecast),
        now=10,
    )
    assert disk_bounded.artifact_prefetches == (
        ArtifactPrefetch("scarce", "higher-pressure"),
    )


def test_queued_preemption_drain_is_revalidated_against_beneficiary_cache():
    machine = node(
        "scarce",
        8_000,
        residencies=(ready("batch", 8_000),),
        disk_capacity_mb=32_000,
        disk_available_mb=16_000,
    )
    batch = model(
        "batch",
        8_000,
        min_replicas=0,
        max_replicas=0,
        priority=1,
        min_residency_seconds=0,
        scale_down_cooldown_seconds=0,
    )
    critical = model(
        "critical",
        8_000,
        priority=1_000,
        artifact_sha256="a" * 64,
        artifact_source="hf://example/models/critical.gguf",
        artifact_size_mb=4_000,
    )
    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        (machine,),
        (batch, critical),
        now=10,
    )
    queued_drain = MutationAction(
        "queued-drain",
        ActionKind.DRAIN,
        "scarce",
        "batch",
        8_000,
        "Release capacity for critical",
        plan.generation,
        9,
        executable=True,
    )
    reconciler = Reconciler()

    before_cache = reconciler.destructive_command_deferrals(
        plan,
        (machine,),
        (batch, critical),
        (queued_drain,),
        now=10,
    )
    assert before_cache["queued-drain"].code == "beneficiary_artifact_not_cached"

    cached_machine = replace(
        machine,
        residencies=(
            ready("batch", 8_000),
            ModelResidency(
                "critical",
                8_000,
                ResidencyState.CACHED,
                artifact_sha256="a" * 64,
            ),
        ),
    )
    assert (
        reconciler.destructive_command_deferrals(
            plan,
            (cached_machine,),
            (batch, critical),
            (queued_drain,),
            now=10,
        )
        == {}
    )


def test_preemption_governor_unloads_drained_capacity_before_starting_new_drain():
    machines = [
        node("a-ready", 8_000, residencies=(ready("batch", 8_000),)),
        node(
            "z-drained",
            8_000,
            residencies=(
                replace(
                    ready("batch", 8_000),
                    state=ResidencyState.DRAINING,
                ),
            ),
        ),
    ]
    profiles = [
        model(
            "batch",
            8_000,
            min_replicas=0,
            max_replicas=0,
            priority=1,
            min_residency_seconds=0,
            scale_down_cooldown_seconds=0,
        ),
        model(
            "critical",
            8_000,
            min_replicas=2,
            max_replicas=2,
            priority=1_000,
            min_residency_seconds=0,
        ),
    ]
    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        machines,
        profiles,
        now=10,
    )
    assert len(plan.preemptions) == 2

    result = Reconciler(ReconcilePolicy(max_concurrent_mutations=1)).reconcile(
        plan,
        machines,
        profiles,
        mode=AllocatorMode.AUTOMATIC,
        now=10,
    )

    assert [(action.kind, action.node_id) for action in result.executable_actions] == [
        (ActionKind.UNLOAD, "z-drained")
    ]


def test_preemption_governor_drains_idle_capacity_before_busy_capacity():
    machines = [
        node(
            "a-busy",
            8_000,
            residencies=(replace(ready("batch", 8_000), active_requests=5),),
        ),
        node("z-idle", 8_000, residencies=(ready("batch", 8_000),)),
    ]
    profiles = [
        model(
            "batch",
            8_000,
            min_replicas=0,
            max_replicas=0,
            priority=1,
            expected_service_seconds=10,
            min_residency_seconds=0,
            scale_down_cooldown_seconds=0,
        ),
        model(
            "critical",
            8_000,
            min_replicas=2,
            max_replicas=2,
            priority=1_000,
            min_residency_seconds=0,
        ),
    ]
    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        machines,
        profiles,
        now=10,
    )
    assert len(plan.preemptions) == 2

    result = Reconciler(ReconcilePolicy(max_concurrent_mutations=1)).reconcile(
        plan,
        machines,
        profiles,
        mode=AllocatorMode.AUTOMATIC,
        now=10,
    )

    assert [(action.kind, action.node_id) for action in result.executable_actions] == [
        (ActionKind.DRAIN, "z-idle")
    ]


@pytest.mark.parametrize(
    ("residency_state", "expected_action"),
    (
        (ResidencyState.READY, ActionKind.DRAIN),
        (ResidencyState.DRAINING, ActionKind.UNLOAD),
    ),
)
def test_capacity_unlocking_mutation_precedes_unrelated_low_priority_load(
    residency_state,
    expected_action,
):
    machines = [
        node("a-background", 8_000),
        node(
            "z-critical",
            8_000,
            residencies=(
                replace(
                    ready("batch", 8_000),
                    state=residency_state,
                ),
            ),
        ),
    ]
    profiles = [
        model(
            "background",
            8_000,
            pinned_nodes=("a-background",),
            priority=1,
        ),
        model(
            "batch",
            8_000,
            min_replicas=0,
            max_replicas=0,
            priority=1,
            min_residency_seconds=0,
            scale_down_cooldown_seconds=0,
        ),
        model(
            "critical",
            8_000,
            pinned_nodes=("z-critical",),
            priority=1_000,
            min_residency_seconds=0,
        ),
    ]
    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        machines,
        profiles,
        now=10,
    )
    assert plan.preempted_pairs == frozenset({("z-critical", "batch")})

    result = Reconciler(ReconcilePolicy(max_concurrent_mutations=1)).reconcile(
        plan,
        machines,
        profiles,
        mode=AllocatorMode.AUTOMATIC,
        now=10,
    )

    assert [
        (item.kind, item.node_id, item.model_id) for item in result.executable_actions
    ] == [(expected_action, "z-critical", "batch")]


def test_reconciler_waits_for_replacement_before_draining_sole_replica():
    profile = model(min_replicas=1, max_replicas=1, min_residency_seconds=0)
    old = node("old", residencies=(ready(),))
    target = node("target", cached=("qwen",))
    plan = PlacementPlanner().plan([target], [profile], now=100)
    result = Reconciler().reconcile(plan, [old, target], [profile], now=100)
    assert [item.kind for item in result.actions] == [ActionKind.WARM]
    assert any(item.code == "replacement_not_ready" for item in result.deferred)


def test_artifact_rollout_selects_only_matching_ready_replica():
    profile = model(
        min_replicas=1,
        max_replicas=1,
        artifact_sha256="b" * 64,
    )
    old = node(
        "old",
        residencies=(ready(artifact_sha256="a" * 64),),
    )
    current = node(
        "current",
        residencies=(ready(artifact_sha256="b" * 64),),
    )

    plan = PlacementPlanner().plan([old, current], [profile], now=100)

    assert plan.nodes_for("qwen") == ("current",)
    assert plan.assignments[0].existing is True


def test_artifact_rollout_warms_replacement_before_draining_old_version():
    profile = model(
        min_replicas=1,
        max_replicas=1,
        min_residency_seconds=0,
        artifact_sha256="b" * 64,
    )
    old = node(
        "old",
        residencies=(ready(artifact_sha256="a" * 64),),
    )
    target = node(
        "target",
        residencies=(
            ModelResidency(
                "qwen",
                8_000,
                ResidencyState.CACHED,
                artifact_sha256="b" * 64,
            ),
        ),
    )
    plan = PlacementPlanner().plan([old, target], [profile], now=100)

    waiting = Reconciler().reconcile(plan, [old, target], [profile], now=100)

    assert [(item.kind, item.node_id) for item in waiting.actions] == [
        (ActionKind.WARM, "target")
    ]
    assert waiting.actions[0].artifact_sha256 == "b" * 64
    assert any(
        item.node_id == "old" and item.code == "replacement_not_ready"
        for item in waiting.deferred
    )

    ready_target = replace(
        target,
        residencies=(ready(artifact_sha256="b" * 64),),
    )
    converging = Reconciler().reconcile(
        plan,
        [old, ready_target],
        [profile],
        now=101,
    )
    assert [(item.kind, item.node_id) for item in converging.actions] == [
        (ActionKind.DRAIN, "old")
    ]
    assert converging.actions[0].artifact_sha256 == "a" * 64


def test_new_artifact_is_not_delayed_by_old_artifact_success_history():
    profile = model(artifact_sha256="b" * 64)
    target = node(
        "target",
        residencies=(
            ModelResidency(
                "qwen",
                8_000,
                ResidencyState.CACHED,
                artifact_sha256="b" * 64,
            ),
        ),
    )
    plan = PlacementPlanner().plan([target], [profile], now=100)
    old_success = MutationRecord(
        "old-warm",
        ActionKind.WARM,
        "target",
        "qwen",
        MutationStatus.SUCCEEDED,
        99,
        completed_at=99,
        artifact_sha256="a" * 64,
    )

    result = Reconciler().reconcile(
        plan,
        [target],
        [profile],
        [old_success],
        now=100,
    )

    assert [(item.kind, item.artifact_sha256) for item in result.actions] == [
        (ActionKind.WARM, "b" * 64)
    ]


def test_failed_process_keeps_matching_verified_artifact_cached():
    profile = model(artifact_sha256="b" * 64)
    target = node(
        "target",
        residencies=(
            ModelResidency(
                "qwen",
                8_000,
                ResidencyState.FAILED,
                managed=True,
                artifact_sha256="b" * 64,
            ),
        ),
    )
    plan = PlacementPlanner().plan([target], [profile], now=100)
    prior_load = MutationRecord(
        "prior-load",
        ActionKind.LOAD,
        "target",
        "qwen",
        MutationStatus.SUCCEEDED,
        99,
        completed_at=99,
        artifact_sha256="b" * 64,
    )

    result = Reconciler().reconcile(
        plan,
        [target],
        [profile],
        [prior_load],
        now=100,
    )

    assert [item.kind for item in result.actions] == [ActionKind.WARM]


def test_reconciler_does_not_count_paused_ready_residency_as_replacement():
    profile = model(min_replicas=1, max_replicas=1, min_residency_seconds=0)
    old = node("old", residencies=(ready(),))
    target = node("target", residencies=(ready(),))
    plan = PlacementPlanner().plan([target], [profile], now=100)

    result = Reconciler().reconcile(
        plan,
        [old, replace(target, state=NodeState.PAUSED)],
        [profile],
        now=100,
    )

    assert not any(item.kind == ActionKind.DRAIN for item in result.actions)
    assert any(
        item.node_id == "old" and item.code == "replacement_not_ready"
        for item in result.deferred
    )


def test_reconciler_drains_old_replica_after_replacement_is_ready():
    profile = model(min_replicas=1, max_replicas=1, min_residency_seconds=0)
    old = node("old", residencies=(ready(),))
    target = node("target", residencies=(ready(),))
    plan = PlacementPlanner().plan([target], [profile], now=100)
    result = Reconciler().reconcile(plan, [old, target], [profile], now=100)
    assert [item.kind for item in result.actions] == [ActionKind.DRAIN]


def test_reconciler_readmits_live_draining_replica_despite_prior_warm_success_block():
    profile = model(min_replicas=1, max_replicas=1, min_residency_seconds=0)
    draining = ModelResidency(
        "qwen",
        8_000,
        ResidencyState.DRAINING,
        loaded_at=90,
        managed=True,
    )
    machine = node("n", residencies=(draining,), now=100)
    plan = PlacementPlanner().plan([machine], [profile], now=100)
    prior_success = MutationRecord(
        "warm-prior",
        ActionKind.WARM,
        "n",
        "qwen",
        MutationStatus.SUCCEEDED,
        90,
        completed_at=90,
    )

    result = Reconciler().reconcile(
        plan,
        [machine],
        [profile],
        [prior_success],
        mode=AllocatorMode.AUTOMATIC,
        now=100,
        blocked_until={(ActionKind.WARM, "n", "qwen"): 210},
    )

    assert [item.kind for item in result.executable_actions] == [ActionKind.WARM]


def test_reconciler_readmission_retains_failed_warm_backoff():
    profile = model(min_replicas=1, max_replicas=1, min_residency_seconds=0)
    draining = ModelResidency(
        "qwen",
        8_000,
        ResidencyState.DRAINING,
        loaded_at=90,
        managed=True,
    )
    machine = node("n", residencies=(draining,))
    plan = PlacementPlanner().plan([machine], [profile], now=100)
    failed = MutationRecord(
        "warm-failed",
        ActionKind.WARM,
        "n",
        "qwen",
        MutationStatus.FAILED,
        99,
        completed_at=99,
        failures=1,
    )

    result = Reconciler().reconcile(
        plan,
        [machine],
        [profile],
        [failed],
        mode=AllocatorMode.AUTOMATIC,
        now=100,
        blocked_until={(ActionKind.WARM, "n", "qwen"): 130},
    )

    assert result.actions == ()
    assert any(
        item.kind == ActionKind.WARM and item.code == "cooldown"
        for item in result.deferred
    )


def test_reconciler_failed_desired_residency_bypasses_only_old_success_observation():
    profile = model(min_replicas=1, max_replicas=1, min_residency_seconds=0)
    failed_residency = ModelResidency(
        "qwen",
        8_000,
        ResidencyState.FAILED,
        loaded_at=90,
        managed=True,
    )
    machine = node(
        "n",
        residencies=(failed_residency,),
        cached=("qwen",),
        now=100,
    )
    plan = PlacementPlanner().plan([machine], [profile], now=100)
    key = (ActionKind.WARM, "n", "qwen")

    retried = Reconciler().reconcile(
        plan,
        [machine],
        [profile],
        mode=AllocatorMode.AUTOMATIC,
        now=100,
        blocked_until={key: 210},
        blocked_causes={key: MutationStatus.SUCCEEDED},
    )
    assert [item.kind for item in retried.executable_actions] == [ActionKind.WARM]

    blocked = Reconciler().reconcile(
        plan,
        [machine],
        [profile],
        mode=AllocatorMode.AUTOMATIC,
        now=100,
        blocked_until={key: 130},
        blocked_causes={key: MutationStatus.FAILED},
    )
    assert blocked.actions == ()
    assert any(
        item.kind == ActionKind.WARM and item.code == "cooldown"
        for item in blocked.deferred
    )


def test_reconciler_cached_residency_can_rewarm_after_completed_prior_lifecycle():
    profile = model(min_replicas=1, max_replicas=1, min_residency_seconds=0)
    cached = ModelResidency(
        "qwen",
        8_000,
        ResidencyState.CACHED,
        loaded_at=90,
        managed=True,
    )
    machine = node("n", residencies=(cached,), cached=("qwen",), now=100)
    plan = PlacementPlanner().plan((machine,), (profile,), now=100)
    succeeded = MutationRecord(
        "old-warm-success",
        ActionKind.WARM,
        "n",
        "qwen",
        MutationStatus.SUCCEEDED,
        99,
        completed_at=99,
    )

    retried = Reconciler().reconcile(
        plan,
        (machine,),
        (profile,),
        (succeeded,),
        mode=AllocatorMode.AUTOMATIC,
        now=100,
    )
    assert [item.kind for item in retried.executable_actions] == [ActionKind.WARM]

    failed = replace(
        succeeded,
        action_id="old-warm-failure",
        status=MutationStatus.FAILED,
    )
    blocked = Reconciler().reconcile(
        plan,
        (machine,),
        (profile,),
        (failed,),
        mode=AllocatorMode.AUTOMATIC,
        now=100,
    )
    assert blocked.actions == ()
    assert any(item.code == "cooldown" for item in blocked.deferred)


@pytest.mark.parametrize(
    ("kind", "state"),
    [
        (ActionKind.DRAIN, ResidencyState.READY),
        (ActionKind.UNLOAD, ResidencyState.DRAINING),
    ],
)
def test_authoritative_state_allows_repeated_destructive_lifecycle(kind, state):
    profile = model(min_replicas=0, max_replicas=1, min_residency_seconds=0)
    residency = ModelResidency(
        "qwen",
        8_000,
        state,
        loaded_at=90,
        managed=True,
    )
    machine = node("n", residencies=(residency,), now=100)
    plan = PlacementPlanner().plan((), (profile,), now=100)
    old_success = MutationRecord(
        "old-success",
        kind,
        "n",
        "qwen",
        MutationStatus.SUCCEEDED,
        99,
        completed_at=99,
    )

    repeated = Reconciler().reconcile(
        plan,
        (machine,),
        (profile,),
        (old_success,),
        mode=AllocatorMode.AUTOMATIC,
        now=100,
    )
    assert [item.kind for item in repeated.executable_actions] == [kind]

    old_failure = replace(
        old_success,
        action_id="old-failure",
        status=MutationStatus.FAILED,
        failures=1,
    )
    blocked = Reconciler().reconcile(
        plan,
        (machine,),
        (profile,),
        (old_failure,),
        mode=AllocatorMode.AUTOMATIC,
        now=100,
    )
    assert blocked.actions == ()
    assert any(item.code == "cooldown" for item in blocked.deferred)


def test_reconciler_honours_minimum_residency():
    profile = model(min_replicas=0, max_replicas=1, min_residency_seconds=100)
    machine = node("n", residencies=(ready(loaded_at=50),))
    plan = PlacementPlanner().plan([], [profile], [DemandForecast("qwen")], now=100)
    result = Reconciler().reconcile(plan, [machine], [profile], now=100)
    item = next(item for item in result.deferred if item.code == "minimum_residency")
    assert item.retry_at == 150


def test_reconciler_treats_future_loaded_at_as_unknown_age_after_clock_rollback():
    profile = model(min_replicas=0, max_replicas=1, min_residency_seconds=100)
    machine = node("n", residencies=(ready(loaded_at=1_000),))
    plan = PlacementPlanner().plan([], [profile], now=10)
    result = Reconciler().reconcile(plan, [machine], [profile], now=10)
    assert result.actions == ()
    minimum = next(item for item in result.deferred if item.code == "minimum_residency")
    assert minimum.retry_at == 110


def test_reconciler_drain_waits_for_inflight_then_unloads():
    profile = model(min_replicas=0, max_replicas=1, min_residency_seconds=0)
    draining = ModelResidency(
        "qwen",
        8_000,
        ResidencyState.DRAINING,
        loaded_at=1,
        active_requests=1,
    )
    busy = node("n", residencies=(draining,), active_requests=1)
    plan = PlacementPlanner().plan([], [profile], now=100)
    result = Reconciler().reconcile(plan, [busy], [profile], now=100)
    assert result.actions == ()
    assert any(item.code == "requests_in_flight" for item in result.deferred)
    idle = node(
        "n",
        residencies=(replace(draining, active_requests=0),),
        active_requests=0,
    )
    result = Reconciler().reconcile(plan, [idle], [profile], now=100)
    assert [item.kind for item in result.actions] == [ActionKind.UNLOAD]


def test_reconciler_never_mutates_external_pinned_or_manual_residency():
    profile = model(min_replicas=0, max_replicas=1, min_residency_seconds=0)
    plan = PlacementPlanner().plan([], [profile], now=100)
    for machine in (
        node("external", residencies=(ready(managed=False),)),
        node("pinned", residencies=(ready(pinned=True),)),
        node("manual", residencies=(ready(),), manually_managed=True),
    ):
        result = Reconciler().reconcile(plan, [machine], [profile], now=100)
        assert result.actions == ()
        assert any(item.code == "not_allocator_owned" for item in result.deferred)


def test_reconciler_suppresses_equivalent_pending_action():
    machine = node("n")
    profile = model()
    plan = PlacementPlanner().plan([machine], [profile], now=10)
    first = Reconciler().reconcile(plan, [machine], [profile], now=10)
    load = first.actions[0]
    record = MutationRecord(
        load.action_id,
        load.kind,
        load.node_id,
        load.model_id,
        MutationStatus.RUNNING,
        10,
    )
    second = Reconciler().reconcile(plan, [machine], [profile], [record], now=11)
    assert ActionKind.LOAD not in {item.kind for item in second.actions}
    assert any(item.code == "already_in_progress" for item in second.deferred)


def test_reconciler_exponential_failure_backoff_then_retries():
    machine = node("n", cached=("qwen",))
    profile = model()
    plan = PlacementPlanner().plan([machine], [profile], now=10)
    action = Reconciler().reconcile(plan, [machine], [profile], now=10).actions[0]
    failed = MutationRecord(
        action.action_id,
        action.kind,
        "n",
        "qwen",
        MutationStatus.FAILED,
        10,
        completed_at=10,
        failures=3,
    )
    reconciler = Reconciler(ReconcilePolicy(failure_backoff_base_seconds=10))
    waiting = reconciler.reconcile(plan, [machine], [profile], [failed], now=49)
    assert waiting.actions == ()
    assert (
        next(item for item in waiting.deferred if item.code == "cooldown").retry_at
        == 50
    )
    retried = reconciler.reconcile(plan, [machine], [profile], [failed], now=50)
    assert [item.kind for item in retried.actions] == [ActionKind.WARM]


def test_failed_load_backoff_blocks_warm_until_artifact_is_cached():
    machine = node("n")
    profile = model()
    plan = PlacementPlanner().plan([machine], [profile], now=10)
    failed = MutationRecord(
        "failed-load",
        ActionKind.LOAD,
        "n",
        "qwen",
        MutationStatus.FAILED,
        10,
        completed_at=10,
        failures=1,
    )
    reconciler = Reconciler(ReconcilePolicy(failure_backoff_base_seconds=10))
    waiting = reconciler.reconcile(plan, [machine], [profile], [failed], now=19)
    assert waiting.actions == ()
    assert any(
        item.kind == ActionKind.LOAD and item.code == "cooldown"
        for item in waiting.deferred
    )
    assert any(
        item.kind == ActionKind.WARM and item.code == "artifact_not_cached"
        for item in waiting.deferred
    )

    retried = reconciler.reconcile(plan, [machine], [profile], [failed], now=20)
    assert [item.kind for item in retried.actions] == [ActionKind.LOAD, ActionKind.WARM]
    assert retried.actions[1].dependencies == (retried.actions[0].action_id,)


def test_failed_unload_uses_unload_history_for_backoff():
    profile = model(min_replicas=0, max_replicas=1, min_residency_seconds=0)
    machine = node(
        "n",
        residencies=(
            ModelResidency("qwen", 8_000, ResidencyState.DRAINING, loaded_at=1),
        ),
    )
    plan = PlacementPlanner().plan([], [profile], now=10)
    failed = MutationRecord(
        "failed-unload",
        ActionKind.UNLOAD,
        "n",
        "qwen",
        MutationStatus.FAILED,
        10,
        completed_at=10,
        failures=1,
    )
    reconciler = Reconciler(ReconcilePolicy(failure_backoff_base_seconds=10))
    waiting = reconciler.reconcile(plan, [machine], [profile], [failed], now=19)
    assert waiting.actions == ()
    deferred = next(item for item in waiting.deferred if item.model_id == "qwen")
    assert deferred.kind == ActionKind.UNLOAD
    assert deferred.code == "cooldown"
    assert deferred.retry_at == 20
    assert [
        item.kind
        for item in reconciler.reconcile(
            plan, [machine], [profile], [failed], now=20
        ).actions
    ] == [ActionKind.UNLOAD]


def test_retry_ids_and_latest_records_ignore_wall_clock_rollback():
    machine = node("n", cached=("qwen",), now=100)
    profile = model()
    plan = PlacementPlanner().plan([machine], [profile], now=100)
    reconciler = Reconciler(
        ReconcilePolicy(
            mutation_cooldown_seconds=0,
            failure_backoff_base_seconds=0,
            failure_backoff_max_seconds=0,
        )
    )
    first = reconciler.reconcile(plan, [machine], [profile], now=100).actions[0]
    first_failed = MutationRecord(
        first.action_id,
        first.kind,
        "n",
        "qwen",
        MutationStatus.FAILED,
        100,
        completed_at=100,
        failures=1,
    )
    second = reconciler.reconcile(
        plan, [machine], [profile], [first_failed], now=10
    ).actions[0]
    second_failed = MutationRecord(
        second.action_id,
        second.kind,
        "n",
        "qwen",
        MutationStatus.FAILED,
        10,
        completed_at=10,
        failures=2,
    )
    third = reconciler.reconcile(
        plan,
        [machine],
        [profile],
        [first_failed, second_failed],
        now=5,
    ).actions[0]
    assert len({first.action_id, second.action_id, third.action_id}) == 3


def test_history_backoff_is_bounded_after_clock_rollback_and_large_streak():
    machine = node("n", cached=("qwen",), now=100)
    profile = model()
    plan = PlacementPlanner().plan([machine], [profile], now=100)
    failed = MutationRecord(
        "failed-warm",
        ActionKind.WARM,
        "n",
        "qwen",
        MutationStatus.FAILED,
        100,
        completed_at=100,
        failures=10**9,
    )
    reconciler = Reconciler(
        ReconcilePolicy(
            failure_backoff_base_seconds=10,
            failure_backoff_max_seconds=3_600,
        )
    )
    waiting = reconciler.reconcile(plan, [machine], [profile], [failed], now=10)
    retry = next(item.retry_at for item in waiting.deferred if item.code == "cooldown")
    assert retry == 3_610


def test_reconciler_action_ids_are_stable_for_same_plan():
    machine = node("n")
    profile = model()
    plan = PlacementPlanner().plan([machine], [profile], now=10)
    one = Reconciler().reconcile(plan, [machine], [profile], now=11)
    two = Reconciler().reconcile(plan, [machine], [profile], now=12)
    assert [item.action_id for item in one.actions] == [
        item.action_id for item in two.actions
    ]
