from __future__ import annotations

import math
import random

from shared.allocator.models import (
    AllocatorMode,
    DemandForecast,
    ModelProfile,
    ModelResidency,
    NodeSnapshot,
    NodeState,
    ResidencyState,
)
from shared.allocator.planner import (
    PlacementPlanner,
    PlannerPolicy,
    compatibility_reason,
)
from shared.allocator.reconcile import Reconciler


def test_seeded_heterogeneous_fleets_preserve_planner_safety_invariants():
    """Exercise many awkward-but-valid fleet shapes without a flaky random seed."""

    rng = random.Random(0xA110CA7E)
    policy = PlannerPolicy(memory_headroom_fraction=0.10, node_ttl_seconds=90)
    planner = PlacementPlanner(policy)
    for scenario in range(150):
        model_count = rng.randint(1, 5)
        profiles = tuple(
            ModelProfile(
                model_id=f"model-{index}",
                memory_mb=rng.choice((1_000, 2_000, 4_000, 8_000, 12_000)),
                runtimes=("llama.cpp",),
                backends=("metal", "cuda"),
                min_replicas=(minimum := rng.randint(0, 1)),
                max_replicas=rng.randint(max(1, minimum), 4),
                min_residency_seconds=0,
                scale_down_cooldown_seconds=0,
            )
            for index in range(model_count)
        )
        nodes: list[NodeSnapshot] = []
        for index in range(rng.randint(1, 7)):
            capacity = rng.choice((8_000, 12_000, 16_000, 24_000, 32_000, 48_000))
            reserved = rng.choice((0, 1_000, 2_000, 4_000))
            reserved = min(reserved, capacity)
            residencies: list[ModelResidency] = []
            for profile in profiles:
                if rng.random() >= 0.22:
                    continue
                residencies.append(
                    ModelResidency(
                        profile.model_id,
                        rng.choice(
                            (profile.memory_mb, max(1, profile.memory_mb - 500))
                        ),
                        rng.choice(
                            (
                                ResidencyState.READY,
                                ResidencyState.CACHED,
                                ResidencyState.DRAINING,
                                ResidencyState.FAILED,
                            )
                        ),
                        loaded_at=1,
                        last_used_at=1,
                        managed=rng.random() < 0.8,
                    )
                )
            nodes.append(
                NodeSnapshot(
                    node_id=f"scenario-{scenario}-node-{index}",
                    capacity_mb=capacity,
                    reserved_mb=reserved,
                    runtimes=("llama.cpp",),
                    backends=(rng.choice(("metal", "cuda", "cpu")),),
                    state=rng.choice(
                        (
                            NodeState.ACCEPTING,
                            NodeState.ACCEPTING,
                            NodeState.THROTTLED,
                            NodeState.PAUSED,
                        )
                    ),
                    failure_domain=f"rack-{index % 3}",
                    max_models=rng.choice((None, 1, 2, 4)),
                    residencies=tuple(residencies),
                    manually_managed=rng.random() < 0.1,
                    last_heartbeat=100,
                )
            )
        forecasts = tuple(
            DemandForecast(
                profile.model_id,
                offered_concurrency=rng.random() * 2,
                queue_depth=rng.randint(0, 3),
            )
            for profile in profiles
        )

        plan = planner.plan(nodes, profiles, forecasts, now=100)
        assert len(plan.desired_pairs) == len(plan.assignments)
        node_by_id = {node.node_id: node for node in nodes}
        profile_by_id = {profile.model_id: profile for profile in profiles}
        incremental_by_node = {node.node_id: 0 for node in nodes}
        added_slots_by_node = {node.node_id: 0 for node in nodes}
        for assignment in plan.assignments:
            node = node_by_id[assignment.node_id]
            profile = profile_by_id[assignment.model_id]
            residency = node.residency(assignment.model_id)
            for_new = residency is None or residency.state in {
                ResidencyState.CACHED,
                ResidencyState.FAILED,
            }
            assert (
                compatibility_reason(
                    node,
                    profile,
                    now=100,
                    policy=policy,
                    for_new=for_new,
                )
                is None
            )
            # An external ready residency is inventory, not a new allocation. Every other new or
            # resized placement must fit inside memory left after current live processes.
            if (
                residency is None
                or residency.state == ResidencyState.CACHED
                or (residency.state == ResidencyState.FAILED and not residency.managed)
            ):
                incremental_by_node[node.node_id] += assignment.memory_mb
                added_slots_by_node[node.node_id] += 1
            elif residency.managed:
                incremental_by_node[node.node_id] += max(
                    0,
                    assignment.memory_mb - residency.memory_mb,
                )

        for node in nodes:
            budget = math.floor(
                (node.capacity_mb - node.reserved_mb)
                * (1.0 - policy.memory_headroom_fraction)
            )
            live_memory = sum(
                residency.memory_mb
                for residency in node.residencies
                if residency.state != ResidencyState.CACHED
                and (residency.state != ResidencyState.FAILED or residency.managed)
            )
            assert incremental_by_node[node.node_id] <= max(0, budget - live_memory)
            if node.max_models is not None:
                live_slots = sum(
                    residency.state != ResidencyState.CACHED
                    and (residency.state != ResidencyState.FAILED or residency.managed)
                    for residency in node.residencies
                )
                if live_slots <= node.max_models:
                    assert (
                        live_slots + added_slots_by_node[node.node_id]
                        <= node.max_models
                    )


def test_seeded_live_framework_mix_never_crosses_runtime_or_ownership_boundaries():
    """Soak the actual 4 llama.cpp + 1 ComfyUI + 3 vLLM fleet shape."""

    rng = random.Random(0x8F1EE7)
    planner = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0.05))
    reconciler = Reconciler()
    profiles = (
        ModelProfile(
            "Qwen3.6-35B-A3B",
            32_000,
            runtimes=("llama.cpp",),
            backends=("metal",),
            min_replicas=0,
            max_replicas=4,
            min_residency_seconds=0,
            scale_down_cooldown_seconds=0,
        ),
        ModelProfile(
            "Gemma-4-31B-it",
            32_000,
            runtimes=("llama.cpp",),
            backends=("metal",),
            min_replicas=0,
            max_replicas=4,
            min_residency_seconds=0,
            scale_down_cooldown_seconds=0,
        ),
        *(
            ModelProfile(
                model_id,
                32_000,
                runtimes=(runtime,),
                min_replicas=1,
                max_replicas=1,
                min_residency_seconds=0,
                scale_down_cooldown_seconds=0,
            )
            for model_id, runtime in (
                ("comfyui:krea2", "comfyui"),
                ("comfyui:z_image", "comfyui"),
                ("Qwen3.8-Flash-Next", "vllm"),
                ("DeepSeek-V4-Flash", "vllm"),
                ("Qwen3.8-27B", "vllm"),
            )
        ),
    )
    profile_by_id = {item.model_id: item for item in profiles}
    fixed_inventory = (
        ("comfyui-1", "comfyui", "mps", ("comfyui:krea2", "comfyui:z_image")),
        ("vllm-1", "vllm", "cuda", ("Qwen3.8-Flash-Next",)),
        ("vllm-2", "vllm", "cuda", ("DeepSeek-V4-Flash",)),
        ("vllm-3", "vllm", "cuda", ("Qwen3.8-27B",)),
    )

    for _ in range(250):
        llama_nodes = tuple(
            NodeSnapshot(
                node_id=f"llama-{index}",
                capacity_mb=48_000,
                reserved_mb=rng.choice((0, 4_000, 8_000)),
                runtimes=("llama.cpp",),
                backends=("metal",),
                state=rng.choice(
                    (
                        NodeState.ACCEPTING,
                        NodeState.ACCEPTING,
                        NodeState.THROTTLED,
                        NodeState.PAUSED,
                    )
                ),
                failure_domain=f"mac-{index}",
                residencies=tuple(
                    ModelResidency(
                        model_id,
                        32_000,
                        rng.choice((ResidencyState.READY, ResidencyState.CACHED)),
                    )
                    for model_id in ("Qwen3.6-35B-A3B", "Gemma-4-31B-it")
                    if rng.random() < 0.25
                ),
                cached_models=tuple(
                    model_id
                    for model_id in ("Qwen3.6-35B-A3B", "Gemma-4-31B-it")
                    if rng.random() < 0.5
                ),
                last_heartbeat=100,
            )
            for index in range(4)
        )
        immutable_nodes = tuple(
            NodeSnapshot(
                node_id=node_id,
                capacity_mb=196_608,
                runtimes=(runtime,),
                backends=(backend,),
                state=rng.choice(
                    (NodeState.ACCEPTING, NodeState.ACCEPTING, NodeState.PAUSED)
                ),
                failure_domain=node_id,
                residencies=tuple(
                    ModelResidency(
                        model_id,
                        32_000,
                        ResidencyState.READY,
                        managed=False,
                    )
                    for model_id in models
                ),
                manually_managed=True,
                actuator_capabilities=(),
                last_heartbeat=100,
            )
            for node_id, runtime, backend, models in fixed_inventory
        )
        nodes = (*llama_nodes, *immutable_nodes)
        demand = tuple(
            DemandForecast(
                item.model_id,
                offered_concurrency=rng.random() * 2,
                queue_depth=rng.randint(0, 3),
            )
            for item in profiles
        )

        plan = planner.plan(nodes, profiles, demand, now=100)
        node_by_id = {item.node_id: item for item in nodes}
        for assignment in plan.assignments:
            target = node_by_id[assignment.node_id]
            selected_profile = profile_by_id[assignment.model_id]
            assert set(target.runtimes).intersection(selected_profile.runtimes)
            residency = target.residency(assignment.model_id)
            if target.manually_managed:
                assert residency is not None
                assert residency.state == ResidencyState.READY
                assert residency.managed is False

        result = reconciler.reconcile(
            plan,
            nodes,
            profiles,
            mode=AllocatorMode.AUTOMATIC,
            now=100,
        )
        assert all(action.node_id.startswith("llama-") for action in result.actions)
        assert all(
            profile_by_id[action.model_id].runtimes == ("llama.cpp",)
            for action in result.actions
        )


def test_seeded_vllm_batch_pressure_spills_only_to_owned_logical_llama_nodes():
    """Stress capacity-aware spillover on the live framework mix without physical hosts."""

    rng = random.Random(0xBA7C4A11)
    policy = PlannerPolicy(memory_headroom_fraction=0.05)
    planner = PlacementPlanner(policy)
    reconciler = Reconciler()

    for scenario in range(500):
        profile = ModelProfile(
            "Qwen3.8-27B",
            32_000,
            runtimes=("llama.cpp", "vllm"),
            backends=("cuda", "metal"),
            min_replicas=0,
            max_replicas=5,
            target_utilization=rng.choice((0.5, 0.7, 0.9)),
            replica_concurrency=rng.choice((1, 2, 4)),
            latency_slo_ms=1_000,
            min_residency_seconds=0,
            scale_down_cooldown_seconds=0,
            min_failure_domains=2,
        )
        batch_width = rng.choice((1, 4, 16, 64))
        external_vllm = NodeSnapshot(
            "8x50902-67-qwen38-27b",
            768_000,
            runtimes=("vllm",),
            backends=("cuda",),
            state=NodeState.ACCEPTING,
            failure_domain="nvidia-rack-3",
            residencies=(
                ModelResidency(
                    profile.model_id,
                    profile.memory_mb,
                    ResidencyState.READY,
                    managed=False,
                ),
            ),
            max_concurrency=batch_width,
            active_requests=rng.randint(0, batch_width),
            queue_depth=rng.randint(0, 3),
            tokens_per_second=rng.choice((0.0, 40.0, 120.0)),
            manually_managed=True,
            actuator_capabilities=(),
            last_heartbeat=100,
        )
        immutable_inventory = (
            NodeSnapshot(
                "mac-studio-turtle",
                196_608,
                runtimes=("comfyui",),
                backends=("mps",),
                state=NodeState.ACCEPTING,
                failure_domain="mac-studio-turtle",
                residencies=(
                    ModelResidency(
                        "comfyui:krea2",
                        32_000,
                        ResidencyState.READY,
                        managed=False,
                    ),
                    ModelResidency(
                        "comfyui:z_image",
                        32_000,
                        ResidencyState.READY,
                        managed=False,
                    ),
                ),
                manually_managed=True,
                actuator_capabilities=(),
                last_heartbeat=100,
            ),
            *(
                NodeSnapshot(
                    node_id,
                    768_000,
                    runtimes=("vllm",),
                    backends=("cuda",),
                    state=NodeState.ACCEPTING,
                    failure_domain=domain,
                    residencies=(
                        ModelResidency(
                            model_id,
                            32_000,
                            ResidencyState.READY,
                            managed=False,
                        ),
                    ),
                    max_concurrency=rng.choice((4, 16, 64)),
                    manually_managed=True,
                    actuator_capabilities=(),
                    last_heartbeat=100,
                )
                for node_id, domain, model_id in (
                    ("scholes-60002-01", "nvidia-rack-1", "Qwen3.8-Flash-Next"),
                    ("scholes-60001", "nvidia-rack-2", "DeepSeek-V4-Flash"),
                )
            ),
        )

        logical_llama: list[NodeSnapshot] = []
        for index, node_id in enumerate(
            (
                "firmware-engineer-daniel",
                "video-editor-tom",
                "3d-artist-diego",
                "ml-engineer-priya",
            )
        ):
            residency_state = rng.choice(
                (
                    None,
                    None,
                    ResidencyState.CACHED,
                    ResidencyState.FAILED,
                    ResidencyState.READY,
                )
            )
            residencies = ()
            cached_models = ()
            if residency_state is not None:
                residencies = (
                    ModelResidency(
                        profile.model_id,
                        profile.memory_mb,
                        residency_state,
                        load_failures=(
                            rng.randint(1, 5)
                            if residency_state == ResidencyState.FAILED
                            else 0
                        ),
                    ),
                )
                if residency_state == ResidencyState.CACHED:
                    cached_models = (profile.model_id,)
            logical_llama.append(
                NodeSnapshot(
                    node_id,
                    48_000,
                    reserved_mb=rng.choice((0, 4_000, 8_000)),
                    runtimes=("llama.cpp",),
                    backends=("metal",),
                    state=rng.choice(
                        (
                            NodeState.ACCEPTING,
                            NodeState.ACCEPTING,
                            NodeState.THROTTLED,
                            NodeState.PAUSED,
                        )
                    ),
                    failure_domain=f"logical-mac-{index}",
                    max_models=1,
                    residencies=residencies,
                    cached_models=cached_models,
                    active_requests=rng.randint(0, 2),
                    max_concurrency=rng.choice((1, 2, 4)),
                    queue_depth=rng.randint(0, 3),
                    tokens_per_second=rng.choice((0.0, 10.0, 40.0, 80.0)),
                    last_heartbeat=100,
                )
            )

        pressure_kind = rng.choice(("none", "queue", "latency", "error"))
        forecast = DemandForecast(
            profile.model_id,
            offered_concurrency=rng.choice((0.0, 0.5, 1.0, 2.0, 8.0, 32.0)),
            queue_depth=rng.randint(1, 8) if pressure_kind == "queue" else 0,
            p95_latency_ms=rng.choice((1_500.0, 3_000.0))
            if pressure_kind == "latency"
            else 0,
            error_rate=rng.choice((0.05, 0.25, 0.75))
            if pressure_kind == "error"
            else 0,
        )
        nodes = [external_vllm, *immutable_inventory, *logical_llama]
        rng.shuffle(nodes)

        plan = planner.plan(nodes, (profile,), (forecast,), now=100)
        permuted = planner.plan(reversed(nodes), (profile,), (forecast,), now=100)
        assert plan == permuted, (
            f"planner depended on node order in scenario {scenario}"
        )
        assert (
            profile.min_replicas
            <= plan.target_for(profile.model_id)
            <= profile.max_replicas
        )
        if pressure_kind != "none":
            assert plan.target_for(profile.model_id) >= 2

        node_by_id = {node.node_id: node for node in nodes}
        for assignment in plan.assignments:
            target = node_by_id[assignment.node_id]
            residency = target.residency(profile.model_id)
            for_new = residency is None or residency.state in {
                ResidencyState.CACHED,
                ResidencyState.FAILED,
                ResidencyState.DRAINING,
            }
            assert (
                compatibility_reason(
                    target,
                    profile,
                    now=100,
                    policy=policy,
                    for_new=for_new,
                )
                is None
            )
            if target.manually_managed:
                assert target.node_id == external_vllm.node_id
                assert residency is not None
                assert residency.state == ResidencyState.READY
                assert residency.managed is False

        result = reconciler.reconcile(
            plan,
            nodes,
            (profile,),
            mode=AllocatorMode.AUTOMATIC,
            now=100,
        )
        assert all(
            action.node_id in {node.node_id for node in logical_llama}
            for action in result.actions
        )
        assert all(
            action.kind.value in node_by_id[action.node_id].actuator_capabilities
            for action in result.executable_actions
        )


def test_managed_failed_process_stays_reserved_until_reconciler_unloads_it():
    planner = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0))
    retiring = ModelProfile(
        "old-model",
        8_000,
        min_replicas=0,
        max_replicas=0,
        min_residency_seconds=0,
        scale_down_cooldown_seconds=0,
    )
    replacement = ModelProfile(
        "new-model",
        8_000,
        min_replicas=1,
        max_replicas=1,
        min_residency_seconds=0,
        scale_down_cooldown_seconds=0,
    )
    host = NodeSnapshot(
        "host-a",
        8_000,
        runtimes=("llama.cpp",),
        backends=("cpu",),
        residencies=(
            ModelResidency(
                "old-model",
                8_000,
                ResidencyState.FAILED,
                managed=True,
            ),
        ),
        cached_models=("old-model", "new-model"),
        actuator_capabilities=("load", "warm", "drain", "unload"),
        last_heartbeat=100,
    )

    plan = planner.plan((host,), (retiring, replacement), now=100)

    assert plan.assignments == ()
    assert any(
        item.model_id == "new-model" and item.code == "insufficient_capacity"
        for item in plan.unsatisfied
    )


def test_repacking_cannot_release_the_slot_of_a_managed_failed_process():
    planner = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0))

    def profile(model_id: str, memory_mb: int) -> ModelProfile:
        return ModelProfile(
            model_id,
            memory_mb,
            required_tags=(model_id,),
            min_replicas=1,
            max_replicas=1,
            min_residency_seconds=0,
            scale_down_cooldown_seconds=0,
        )

    profiles = (
        profile("a-x", 4),
        profile("b-w", 3),
        profile("c-z", 2),
        profile("d-y", 1),
    )

    def host(
        node_id: str,
        capacity_mb: int,
        max_models: int,
        tags: tuple[str, ...],
        *,
        residencies: tuple[ModelResidency, ...] = (),
        cached_models: tuple[str, ...] = (),
    ) -> NodeSnapshot:
        return NodeSnapshot(
            node_id,
            capacity_mb,
            runtimes=("llama.cpp",),
            backends=("cpu",),
            tags=tags,
            max_models=max_models,
            residencies=residencies,
            cached_models=cached_models,
            actuator_capabilities=("load", "warm", "drain", "unload"),
            last_heartbeat=10,
        )

    nodes = (
        host(
            "n1",
            8,
            2,
            ("a-x", "b-w", "d-y"),
            residencies=(
                ModelResidency(
                    "a-x",
                    2,
                    ResidencyState.FAILED,
                    managed=True,
                ),
            ),
            cached_models=("a-x",),
        ),
        host("n2", 2, 1, ("c-z", "d-y")),
        host("n3", 4, 1, ("a-x",)),
        host("n4", 100, 1, ("b-w",)),
        host("n5", 100, 1, ("c-z",)),
    )

    plan = planner.plan(nodes, profiles, now=10)

    assert set(plan.models_for("n1")) == {"a-x", "d-y"}
    assert plan.unsatisfied == ()
