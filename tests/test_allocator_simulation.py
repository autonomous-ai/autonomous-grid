from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import replace

from shared.allocator.local import HostPolicy, LocalHostProtectionLoop
from shared.allocator.models import (
    ActionKind,
    AllocatorMode,
    DemandForecast,
    ModelProfile,
    ModelResidency,
    NodeSnapshot,
    NodeState,
    ResidencyState,
)
from shared.allocator.planner import PlacementPlanner, PlannerPolicy
from shared.allocator.reconcile import (
    MutationRecord,
    MutationStatus,
    ReconcilePolicy,
    Reconciler,
)
from shared.system.hostsignals import HostSignals, ThermalState


def host(
    node_id: str,
    *,
    capacity_mb: int = 24_000,
    domain: str | None = None,
    state: NodeState = NodeState.ACCEPTING,
    residencies: tuple[ModelResidency, ...] = (),
    cached: tuple[str, ...] = (),
    now: float = 1_000,
    active_requests: int = 0,
) -> NodeSnapshot:
    return NodeSnapshot(
        node_id=node_id,
        capacity_mb=capacity_mb,
        reserved_mb=2_000,
        backends=("metal",),
        runtimes=("llama.cpp",),
        state=state,
        failure_domain=domain or node_id,
        allowed_data_tiers=("public", "internal", "confidential"),
        tags=("employee",),
        residencies=residencies,
        cached_models=cached,
        active_requests=active_requests,
        max_concurrency=4,
        last_heartbeat=now,
    )


def profile(
    model_id: str,
    memory_mb: int,
    *,
    min_replicas: int = 1,
    max_replicas: int = 4,
    priority: int = 100,
) -> ModelProfile:
    return ModelProfile(
        model_id=model_id,
        memory_mb=memory_mb,
        runtimes=("llama.cpp",),
        backends=("metal",),
        min_replicas=min_replicas,
        max_replicas=max_replicas,
        target_utilization=0.70,
        priority=priority,
        min_residency_seconds=0,
        scale_down_cooldown_seconds=180,
        min_failure_domains=min(2, max_replicas),
    )


def ready(
    model_id: str,
    memory_mb: int,
    *,
    now: float,
    state: ResidencyState = ResidencyState.READY,
    active_requests: int = 0,
) -> ModelResidency:
    return ModelResidency(
        model_id=model_id,
        memory_mb=memory_mb,
        state=state,
        loaded_at=now,
        last_used_at=now,
        active_requests=active_requests,
    )


def forecasts(
    at: float,
    *,
    assistant: float,
    code: float,
    embeddings: float,
) -> tuple[DemandForecast, ...]:
    return (
        DemandForecast(
            "assistant",
            offered_concurrency=assistant,
            requests_per_minute=assistant * 12,
            updated_at=at,
        ),
        DemandForecast(
            "code",
            offered_concurrency=code,
            requests_per_minute=code * 8,
            updated_at=at,
        ),
        DemandForecast(
            "embeddings",
            offered_concurrency=embeddings,
            requests_per_minute=embeddings * 60,
            updated_at=at,
        ),
    )


def materialize(
    plan,
    nodes: tuple[NodeSnapshot, ...],
    *,
    now: float,
) -> tuple[NodeSnapshot, ...]:
    by_node: dict[str, list[ModelResidency]] = defaultdict(list)
    for assignment in plan.assignments:
        by_node[assignment.node_id].append(
            ready(assignment.model_id, assignment.memory_mb, now=now)
        )
    return tuple(
        replace(
            node,
            residencies=tuple(sorted(by_node[node.node_id], key=lambda item: item.model_id)),
            cached_models=tuple(
                sorted({*node.cached_models, *(item.model_id for item in by_node[node.node_id])})
            ),
            last_heartbeat=now,
        )
        for node in nodes
    )


def assert_no_overcommit(plan, nodes: tuple[NodeSnapshot, ...], policy: PlannerPolicy) -> None:
    node_by_id = {node.node_id: node for node in nodes}
    allocated: dict[str, int] = defaultdict(int)
    for assignment in plan.assignments:
        allocated[assignment.node_id] += assignment.memory_mb
    for node_id, memory_mb in allocated.items():
        node = node_by_id[node_id]
        budget = math.floor(
            (node.capacity_mb - node.reserved_mb) * (1 - policy.memory_headroom_fraction)
        )
        assert memory_mb <= budget


def test_workday_ramp_and_spike_scale_out_without_overcommit_or_reshuffle():
    policy = PlannerPolicy(memory_headroom_fraction=0.05, node_ttl_seconds=90)
    planner = PlacementPlanner(policy)
    models = (
        profile("assistant", 8_000, max_replicas=4, priority=300),
        profile("code", 12_000, max_replicas=3, priority=400),
        profile("embeddings", 4_000, max_replicas=2, priority=100),
    )
    nodes = tuple(
        host(
            f"host-{index}",
            capacity_mb=32_000 if index < 2 else 24_000,
            cached=("assistant", "code", "embeddings"),
            now=1_000,
        )
        for index in range(5)
    )

    quiet = planner.plan(
        nodes,
        models,
        forecasts(1_000, assistant=0.10, code=0.10, embeddings=0.05),
        now=1_000,
    )
    assert dict(quiet.desired_replicas) == {
        "assistant": 1,
        "code": 1,
        "embeddings": 1,
    }
    assert_no_overcommit(quiet, nodes, policy)

    morning_nodes = materialize(quiet, nodes, now=1_060)
    ramp = planner.plan(
        morning_nodes,
        models,
        forecasts(1_060, assistant=0.80, code=0.50, embeddings=0.40),
        now=1_060,
    )
    assert quiet.desired_pairs <= ramp.desired_pairs
    assert dict(ramp.desired_replicas) == {
        "assistant": 2,
        "code": 1,
        "embeddings": 1,
    }
    assert_no_overcommit(ramp, morning_nodes, policy)

    ramp_nodes = materialize(ramp, morning_nodes, now=1_120)
    spike = planner.plan(
        ramp_nodes,
        models,
        forecasts(1_120, assistant=1.90, code=1.20, embeddings=0.90),
        now=1_120,
    )
    # The larger, higher-priority code model can force one low-priority embedding replica to move
    # while bins are repacked. The planner must not churn more than that bounded, useful migration.
    removed_during_spike = ramp.desired_pairs - spike.desired_pairs
    assert len(removed_during_spike) <= 1
    assert all(model_id == "embeddings" for _, model_id in removed_during_spike)
    assert dict(spike.desired_replicas) == {
        "assistant": 4,
        "code": 3,
        "embeddings": 2,
    }
    assert_no_overcommit(spike, ramp_nodes, policy)
    assert not spike.unsatisfied

    reordered = planner.plan(
        reversed(ramp_nodes),
        reversed(models),
        reversed(forecasts(1_120, assistant=1.90, code=1.20, embeddings=0.90)),
        now=1_120,
    )
    assert reordered == spike


def test_seeded_four_node_evolution_preserves_raw_and_incremental_memory_safety():
    """Continuously perturb logical hosts instead of testing only isolated snapshots."""

    rng = random.Random(0x4A110CA7E)
    policy = PlannerPolicy(memory_headroom_fraction=0.05, node_ttl_seconds=90)
    planner = PlacementPlanner(policy)
    models = (
        profile("assistant", 8_000, max_replicas=4, priority=300),
        profile("code", 12_000, max_replicas=3, priority=400),
        profile("embeddings", 4_000, max_replicas=3, priority=100),
    )
    nodes = tuple(
        host(
            f"logical-{index}",
            capacity_mb=32_000,
            cached=("assistant", "code", "embeddings"),
            now=1_000,
        )
        for index in range(4)
    )

    for cycle in range(500):
        now = 1_000 + cycle
        evolved = tuple(
            replace(
                node,
                reserved_mb=rng.choice((2_000, 6_000, 10_000, 14_000)),
                state=rng.choices(
                    (NodeState.ACCEPTING, NodeState.THROTTLED, NodeState.PAUSED),
                    weights=(8, 2, 1),
                )[0],
                last_heartbeat=now,
            )
            for node in nodes
        )
        demand = forecasts(
            now,
            assistant=rng.uniform(0, 2.5),
            code=rng.uniform(0, 2.0),
            embeddings=rng.uniform(0, 1.5),
        )

        plan = planner.plan(evolved, models, demand, now=now)
        reordered = planner.plan(
            tuple(reversed(evolved)),
            tuple(reversed(models)),
            tuple(reversed(demand)),
            now=now,
        )

        assert plan == reordered
        assert not plan.desired_pairs.intersection(plan.preempted_pairs)
        desired_memory: dict[str, int] = defaultdict(int)
        incremental_memory: dict[str, int] = defaultdict(int)
        node_by_id = {node.node_id: node for node in evolved}
        for assignment in plan.assignments:
            desired_memory[assignment.node_id] += assignment.memory_mb
            residency = node_by_id[assignment.node_id].residency(assignment.model_id)
            incremental_memory[assignment.node_id] += (
                assignment.memory_mb
                if residency is None or residency.state == ResidencyState.CACHED
                else max(0, assignment.memory_mb - residency.memory_mb)
            )
        for node in evolved:
            raw_budget = node.capacity_mb - node.reserved_mb
            admission_budget = raw_budget * (1 - policy.memory_headroom_fraction)
            if node.state == NodeState.THROTTLED:
                raw_budget = math.floor(
                    raw_budget * policy.throttled_capacity_fraction
                )
                admission_budget *= policy.throttled_capacity_fraction
            assert desired_memory[node.node_id] <= raw_budget
            live_memory = sum(
                residency.memory_mb
                for residency in node.residencies
                if residency.state != ResidencyState.CACHED
            )
            assert incremental_memory[node.node_id] <= max(
                0,
                math.floor(admission_budget) - live_memory,
            )

        # Advance actual state only after checking the transition plan. This repeatedly feeds
        # ready incumbents back through later reserve growth and host-state changes.
        nodes = materialize(plan, evolved, now=now)


def test_scale_down_hysteresis_holds_recent_replicas_then_releases_them():
    policy = PlannerPolicy(memory_headroom_fraction=0)
    planner = PlacementPlanner(policy)
    model = profile("assistant", 8_000, max_replicas=4)
    nodes = tuple(host(f"host-{index}", cached=("assistant",), now=100) for index in range(4))
    spike = planner.plan(
        nodes,
        (model,),
        (DemandForecast("assistant", offered_concurrency=2.0),),
        now=100,
    )
    assert spike.target_for("assistant") == 4

    warm_nodes = materialize(spike, nodes, now=110)
    just_after_spike = planner.plan(
        warm_nodes,
        (model,),
        (DemandForecast("assistant"),),
        now=150,
    )
    assert just_after_spike.target_for("assistant") == 4
    assert just_after_spike.desired_pairs == spike.desired_pairs

    heartbeat_nodes = tuple(replace(node, last_heartbeat=400) for node in warm_nodes)
    after_cooldown = planner.plan(
        heartbeat_nodes,
        (model,),
        (DemandForecast("assistant"),),
        now=400,
    )
    assert after_cooldown.target_for("assistant") == 1
    assert len(after_cooldown.desired_pairs) == 1


def test_employee_activity_protects_host_and_recovery_does_not_cause_repatriation():
    policy = HostPolicy(
        activity_debounce_seconds=0,
        activity_recovery_seconds=20,
        drain_grace_seconds=10,
        recovery_cooldown_seconds=15,
    )
    local_loop = LocalHostProtectionLoop(policy)

    def signals(timestamp: float, *, user_active: bool) -> HostSignals:
        return HostSignals(
            timestamp=timestamp,
            user_active=user_active,
            idle_seconds=0 if user_active else 300,
            thermal_state=ThermalState.NOMINAL,
            temperature_celsius=55,
            cpu_utilization_percent=10,
            load_per_cpu=0.1,
            memory_percent=20,
            on_battery=False,
            battery_percent=100,
            network_available=True,
        )

    assert local_loop.evaluate(signals(0, user_active=False)).state == NodeState.ACCEPTING
    assert local_loop.evaluate(signals(1, user_active=True)).state == NodeState.DRAINING
    paused = local_loop.evaluate(signals(12, user_active=True))
    assert paused.state == NodeState.PAUSED
    assert not paused.accept

    model = profile("assistant", 8_000, max_replicas=1)
    protected = host(
        "employee-laptop",
        state=paused.state,
        residencies=(ready("assistant", 8_000, now=0),),
        cached=("assistant",),
        now=12,
    )
    fallback = host("always-on-server", cached=("assistant",), now=12)
    failover = PlacementPlanner().plan((protected, fallback), (model,), now=12)
    assert failover.nodes_for("assistant") == ("always-on-server",)

    # The cause clears first, then the recovery cooldown must also elapse.
    assert local_loop.evaluate(signals(13, user_active=False)).state == NodeState.PAUSED
    recovered = local_loop.evaluate(signals(33, user_active=False))
    assert recovered.state == NodeState.ACCEPTING

    # Once failover is READY, a recovered laptop is only a cached candidate. The strong preference
    # for an existing READY replica prevents an unnecessary migration back to the laptop.
    recovered_laptop = replace(
        protected,
        state=recovered.state,
        residencies=(ready("assistant", 8_000, now=0, state=ResidencyState.CACHED),),
        last_heartbeat=33,
    )
    ready_fallback = replace(
        fallback,
        residencies=(ready("assistant", 8_000, now=20),),
        last_heartbeat=33,
    )
    recovered_plan = PlacementPlanner().plan(
        (recovered_laptop, ready_fallback),
        (model,),
        now=33,
    )
    assert recovered_plan.nodes_for("assistant") == ("always-on-server",)


def test_multi_model_spike_obeys_global_and_per_host_mutation_budgets():
    nodes = tuple(
        host(f"host-{index}", cached=("assistant", "code", "embeddings"), now=10)
        for index in range(4)
    )
    models = (
        profile("assistant", 8_000, max_replicas=3),
        profile("code", 12_000, max_replicas=2),
        profile("embeddings", 4_000, max_replicas=2),
    )
    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        nodes,
        models,
        forecasts(10, assistant=1.4, code=0.8, embeddings=0.8),
        now=10,
    )
    reconciler = Reconciler(
        ReconcilePolicy(max_concurrent_mutations=2, max_mutations_per_node=1)
    )
    first = reconciler.reconcile(
        plan,
        nodes,
        models,
        mode=AllocatorMode.AUTOMATIC,
        now=10,
    )
    assert len(first.executable_actions) == 2
    assert len({action.node_id for action in first.executable_actions}) == 2
    assert all(action.kind == ActionKind.WARM for action in first.executable_actions)
    assert {item.code for item in first.deferred} >= {
        "global_mutation_limit",
        "node_mutation_limit",
    }

    pending = tuple(
        MutationRecord(
            action.action_id,
            action.kind,
            action.node_id,
            action.model_id,
            MutationStatus.RUNNING,
            attempted_at=10,
        )
        for action in first.executable_actions
    )
    second = reconciler.reconcile(
        plan,
        nodes,
        models,
        pending,
        mode=AllocatorMode.AUTOMATIC,
        now=11,
    )
    assert second.executable_actions == ()
    assert all(
        item.code in {"already_in_progress", "global_mutation_limit", "node_mutation_limit"}
        for item in second.deferred
    )


def test_vllm_batch_capacity_absorbs_demand_until_observed_pressure_scales_managed_llama():
    model = ModelProfile(
        model_id="assistant",
        memory_mb=8_000,
        runtimes=("llama.cpp", "vllm"),
        backends=("cuda", "metal"),
        min_replicas=0,
        max_replicas=2,
        target_utilization=0.75,
        latency_slo_ms=1_000,
        min_residency_seconds=0,
        scale_down_cooldown_seconds=0,
        min_failure_domains=2,
    )
    external_vllm = NodeSnapshot(
        node_id="external-vllm",
        capacity_mb=64_000,
        backends=("cuda",),
        runtimes=("vllm",),
        failure_domain="gpu-rack",
        residencies=(
            replace(ready("assistant", 8_000, now=100), managed=False),
        ),
        max_concurrency=16,
        last_heartbeat=100,
        actuator_capabilities=(),
        manually_managed=True,
    )
    managed_llama = host(
        "managed-llama",
        cached=("assistant",),
        domain="mac-studio",
        now=100,
    )
    planner = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0))

    normal = planner.plan(
        (external_vllm, managed_llama),
        (model,),
        (DemandForecast("assistant", offered_concurrency=6, updated_at=100),),
        now=100,
    )
    assert normal.target_for("assistant") == 1
    assert normal.nodes_for("assistant") == ("external-vllm",)
    assert Reconciler().reconcile(
        normal,
        (external_vllm, managed_llama),
        (model,),
        mode=AllocatorMode.AUTOMATIC,
        now=100,
    ).actions == ()

    pressured = planner.plan(
        (external_vllm, managed_llama),
        (model,),
        (
            DemandForecast(
                "assistant",
                offered_concurrency=6,
                queue_depth=1,
                p95_latency_ms=1_100,
                updated_at=101,
            ),
        ),
        now=101,
    )
    assert pressured.target_for("assistant") == 2
    assert pressured.nodes_for("assistant") == ("external-vllm", "managed-llama")

    reconciliation = Reconciler().reconcile(
        pressured,
        (external_vllm, replace(managed_llama, last_heartbeat=101)),
        (model,),
        mode=AllocatorMode.AUTOMATIC,
        now=101,
    )
    assert [(action.kind, action.node_id) for action in reconciliation.actions] == [
        (ActionKind.WARM, "managed-llama")
    ]
    assert all(action.node_id != "external-vllm" for action in reconciliation.actions)


def test_equal_priority_bursts_share_nodes_then_switch_models_without_overcommit():
    nodes = tuple(host(f"host-{index}", now=100) for index in range(4))
    models = (
        replace(
            profile("alpha", 22_000, min_replicas=0, max_replicas=4),
            scale_down_cooldown_seconds=0,
        ),
        replace(
            profile("beta", 22_000, min_replicas=0, max_replicas=4),
            scale_down_cooldown_seconds=0,
        ),
    )
    planner = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0))
    burst = (
        DemandForecast("alpha", offered_concurrency=10, updated_at=100),
        DemandForecast("beta", offered_concurrency=10, updated_at=100),
    )

    shared = planner.plan(nodes, models, burst, now=100)

    assert shared.target_for("alpha") == 4
    assert shared.target_for("beta") == 4
    assert len(shared.nodes_for("alpha")) == 2
    assert len(shared.nodes_for("beta")) == 2
    assert all(
        action.kind in (ActionKind.LOAD, ActionKind.WARM)
        for action in Reconciler().reconcile(
            shared,
            nodes,
            models,
            mode=AllocatorMode.AUTOMATIC,
            now=100,
        ).actions
    )

    resident = materialize(shared, nodes, now=100)
    switched = planner.plan(
        resident,
        models,
        (
            DemandForecast("alpha", updated_at=101),
            DemandForecast("beta", offered_concurrency=10, updated_at=101),
        ),
        now=101,
    )
    switch_actions = Reconciler().reconcile(
        switched,
        resident,
        models,
        mode=AllocatorMode.AUTOMATIC,
        now=101,
    ).actions

    assert switched.nodes_for("alpha") == ()
    assert len(switched.nodes_for("beta")) == 2
    assert all(action.model_id == "alpha" for action in switch_actions)
    assert all(action.kind == ActionKind.DRAIN for action in switch_actions)

    after_drain = tuple(
        replace(
            node,
            residencies=tuple(
                item for item in node.residencies if item.model_id != "alpha"
            ),
            last_heartbeat=102,
        )
        for node in resident
    )
    expanded = planner.plan(
        after_drain,
        models,
        (
            DemandForecast("alpha", updated_at=102),
            DemandForecast("beta", offered_concurrency=10, updated_at=102),
        ),
        now=102,
    )

    assert len(expanded.nodes_for("beta")) == 4
    assert all(
        action.model_id == "beta"
        and action.kind in (ActionKind.LOAD, ActionKind.WARM)
        for action in Reconciler().reconcile(
            expanded,
            after_drain,
            models,
            mode=AllocatorMode.AUTOMATIC,
            now=102,
        ).actions
    )


def test_four_logical_nodes_preempt_batch_for_critical_demand_then_refill():
    nodes = tuple(
        host(
            f"logical-{index}",
            capacity_mb=10_000,
            residencies=(ready("batch", 8_000, now=1),),
            cached=("batch", "critical"),
            now=100,
        )
        for index in range(4)
    )
    batch = replace(
        profile("batch", 8_000, min_replicas=4, max_replicas=4, priority=10),
        scale_down_cooldown_seconds=0,
    )
    critical = replace(
        profile(
            "critical",
            8_000,
            min_replicas=0,
            max_replicas=4,
            priority=1_000,
        ),
        scale_down_cooldown_seconds=0,
    )
    models = (batch, critical)
    planner = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0))

    staged = planner.plan(
        nodes,
        models,
        (DemandForecast("critical", offered_concurrency=3, updated_at=100),),
        now=100,
    )

    assert staged.nodes_for("critical") == ()
    assert staged.nodes_for("batch") == ()
    assert staged.preempted_pairs == frozenset(
        (f"logical-{index}", "batch") for index in range(4)
    )
    drains = Reconciler().reconcile(
        staged,
        nodes,
        models,
        mode=AllocatorMode.AUTOMATIC,
        now=100,
    )
    assert len(drains.executable_actions) == 4
    assert all(action.kind == ActionKind.DRAIN for action in drains.executable_actions)

    released = tuple(replace(node, residencies=(), last_heartbeat=101) for node in nodes)
    refilled = planner.plan(
        released,
        models,
        (DemandForecast("critical", offered_concurrency=3, updated_at=101),),
        now=101,
    )
    assert len(refilled.nodes_for("critical")) == 4
    assert refilled.preemptions == ()
    assert_no_overcommit(refilled, released, PlannerPolicy(memory_headroom_fraction=0))


def test_failover_keeps_last_replica_until_replacement_is_ready_and_drained():
    model = profile("code", 12_000, max_replicas=1)
    old = host(
        "old-host",
        state=NodeState.DRAINING,
        residencies=(ready("code", 12_000, now=1),),
        now=100,
    )
    target = host("new-host", cached=("code",), now=100)
    plan = PlacementPlanner().plan((target,), (model,), now=100)
    reconciler = Reconciler()

    before_ready = reconciler.reconcile(plan, (old, target), (model,), now=100)
    assert [action.kind for action in before_ready.actions] == [ActionKind.WARM]
    assert any(item.code == "replacement_not_ready" for item in before_ready.deferred)
    assert all(action.kind not in (ActionKind.DRAIN, ActionKind.UNLOAD) for action in before_ready.actions)

    target_ready = replace(
        target,
        residencies=(ready("code", 12_000, now=101),),
    )
    after_ready = reconciler.reconcile(plan, (old, target_ready), (model,), now=102)
    assert [action.kind for action in after_ready.actions] == [ActionKind.DRAIN]

    old_inflight = replace(
        old,
        residencies=(
            ready(
                "code",
                12_000,
                now=1,
                state=ResidencyState.DRAINING,
                active_requests=2,
            ),
        ),
        active_requests=2,
    )
    waiting = reconciler.reconcile(plan, (old_inflight, target_ready), (model,), now=103)
    assert waiting.actions == ()
    assert any(item.code == "requests_in_flight" for item in waiting.deferred)

    old_idle = replace(
        old_inflight,
        residencies=(replace(old_inflight.residencies[0], active_requests=0),),
        active_requests=0,
    )
    unload = reconciler.reconcile(plan, (old_idle, target_ready), (model,), now=104)
    assert [action.kind for action in unload.actions] == [ActionKind.UNLOAD]
