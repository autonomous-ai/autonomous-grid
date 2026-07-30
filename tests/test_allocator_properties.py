from __future__ import annotations

import math
import random

from shared.allocator.models import (
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
                        rng.choice((profile.memory_mb, max(1, profile.memory_mb - 500))),
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
            assert compatibility_reason(
                node,
                profile,
                now=100,
                policy=policy,
                for_new=for_new,
            ) is None
            # An external ready residency is inventory, not a new allocation. Every other new or
            # resized placement must fit inside memory left after current live processes.
            if (
                residency is None
                or residency.state == ResidencyState.CACHED
                or (
                    residency.state == ResidencyState.FAILED
                    and not residency.managed
                )
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
                and (
                    residency.state != ResidencyState.FAILED
                    or residency.managed
                )
            )
            assert incremental_by_node[node.node_id] <= max(0, budget - live_memory)
            if node.max_models is not None:
                live_slots = sum(
                    residency.state != ResidencyState.CACHED
                    and (
                        residency.state != ResidencyState.FAILED
                        or residency.managed
                    )
                    for residency in node.residencies
                )
                if live_slots <= node.max_models:
                    assert live_slots + added_slots_by_node[node.node_id] <= node.max_models


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
