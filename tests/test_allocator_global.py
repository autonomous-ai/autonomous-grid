from __future__ import annotations

import math
from dataclasses import replace

import pytest

from shared.allocator.demand import DemandTracker
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


def test_allocator_models_validate_impossible_values():
    with pytest.raises(ValueError, match="memory_mb"):
        model(memory_mb=0)
    with pytest.raises(ValueError, match="reserved_mb"):
        node("n", 10, reserved_mb=11)
    with pytest.raises(ValueError, match="replica bounds"):
        model(min_replicas=2, max_replicas=1)
    with pytest.raises(ValueError, match="pinned_nodes"):
        model(pinned_nodes=("a", "b"), max_replicas=1)
    with pytest.raises(ValueError, match="finite"):
        DemandForecast("m", requests_per_minute=math.inf)
    with pytest.raises(ValueError, match="active_requests"):
        ready(active_requests=-1)


def test_node_snapshot_round_trip_preserves_residency_and_enum():
    original = node(
        "n",
        residencies=(ready(managed=False, pinned=True, active_requests=2),),
        cached=("other",),
        state=NodeState.THROTTLED,
    )
    restored = NodeSnapshot.from_dict(original.to_dict())
    assert restored == original
    assert restored.residency("qwen").managed is False
    assert restored.residency("qwen").active_requests == 2

    legacy = original.to_dict()
    legacy["residencies"][0].pop("active_requests")
    assert NodeSnapshot.from_dict(legacy).residency("qwen").active_requests == 0


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
    assert desired_replica_count(
        model(min_replicas=0, max_replicas=1, scale_down_cooldown_seconds=60),
        forecast,
        now=100,
    ) == 0


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
    assert forecast.p95_latency_ms == 4_000


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
    tracker = DemandTracker(window_seconds=1_000, bucket_seconds=10, max_samples_per_model=3)
    for timestamp in range(10):
        tracker.observe("m", timestamp=timestamp, service_seconds=1)
    # Requests in one time bucket are compacted without losing their aggregate demand.
    assert tracker.forecast("m", now=9).sample_count == 10
    restored = DemandTracker.from_dict(tracker.to_dict())
    assert restored.to_dict() == tracker.to_dict()


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


def test_replica_count_uses_offered_concurrency_headroom_and_bounds():
    profile = model(min_replicas=1, max_replicas=5, target_utilization=0.5)
    forecast = DemandForecast("qwen", offered_concurrency=1)
    assert desired_replica_count(profile, forecast, now=100) == 3
    huge = DemandForecast("qwen", offered_concurrency=100)
    assert desired_replica_count(profile, huge, now=100) == 5


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


def test_replica_count_keeps_recent_ready_replicas_during_scale_down():
    profile = model(min_replicas=1, max_replicas=3, scale_down_cooldown_seconds=100)
    nodes = [
        node("a", residencies=(ready(last_used_at=90),)),
        node("b", residencies=(ready(last_used_at=80),)),
    ]
    assert desired_replica_count(profile, DemandForecast("qwen"), nodes=nodes, now=100) == 2
    assert desired_replica_count(profile, DemandForecast("qwen"), nodes=nodes, now=500) == 1


def test_replica_count_does_not_treat_a_future_node_timestamp_as_recent_forever():
    profile = model(min_replicas=0, max_replicas=2, scale_down_cooldown_seconds=100)
    skewed = node(
        "future-clock",
        residencies=(ready(loaded_at=1_000_000, last_used_at=1_000_000),),
    )
    assert desired_replica_count(
        profile,
        DemandForecast("qwen"),
        nodes=[skewed],
        now=100,
    ) == 0


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


def test_planner_prefers_existing_then_cached_then_cold():
    profile = model()
    existing = node("existing", residencies=(ready(),), now=1_000)
    cached = node("cached", cached=("qwen",), now=1_000)
    cold = node("cold", now=1_000)
    plan = PlacementPlanner().plan([cold, cached, existing], [profile], now=1_000)
    assert plan.nodes_for("qwen") == ("existing",)
    plan = PlacementPlanner().plan([cold, cached], [profile], now=1_000)
    assert plan.nodes_for("qwen") == ("cached",)


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
        node("wrong-runtime", tiers=("confidential",), tags=("finance",), runtime="ollama"),
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
    assert PlacementPlanner().plan([existing], [model()], now=10).nodes_for("qwen") == ("n",)


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
    plan = PlacementPlanner().plan([paused, wrong_tier], [model(data_tier="internal")], now=10)
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


def test_zero_model_limit_selects_no_existing_residency():
    machine = node("n", max_models=0, residencies=(ready(),))
    plan = PlacementPlanner().plan([machine], [model()], now=10)
    assert plan.assignments == ()


def test_throttled_capacity_fraction_limits_new_placement_memory():
    machine = node("n", capacity_mb=16_000, state=NodeState.THROTTLED)
    plan = PlacementPlanner(
        PlannerPolicy(memory_headroom_fraction=0, throttled_capacity_fraction=0.5)
    ).plan([machine], [model(memory_mb=12_000)], now=10)
    assert plan.assignments == ()


def test_failure_domain_minimum_wins_over_cost_when_feasible():
    machines = [
        node("a1", domain="rack-a"),
        node("a2", domain="rack-a"),
        node("b1", domain="rack-b", cost_per_hour=10_000),
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


def test_reconciler_proposes_load_then_dependent_warm():
    machine = node("n")
    profile = model()
    plan = PlacementPlanner().plan([machine], [profile], now=10)
    result = Reconciler().reconcile(plan, [machine], [profile], now=10)
    assert [item.kind for item in result.actions] == [ActionKind.LOAD, ActionKind.WARM]
    assert result.actions[1].dependencies == (result.actions[0].action_id,)
    assert not any(item.executable for item in result.actions)


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
    reconciler = Reconciler(ReconcilePolicy(max_concurrent_mutations=2, max_mutations_per_node=1))
    result = reconciler.reconcile(
        plan, machines, [profile], mode=AllocatorMode.AUTOMATIC, now=10
    )
    assert len(result.executable_actions) == 2
    assert {item.node_id for item in result.executable_actions} == {"a", "b"}
    assert any(item.code == "node_mutation_limit" for item in result.deferred)


def test_reconciler_waits_for_replacement_before_draining_sole_replica():
    profile = model(min_replicas=1, max_replicas=1, min_residency_seconds=0)
    old = node("old", residencies=(ready(),))
    target = node("target", cached=("qwen",))
    plan = PlacementPlanner().plan([target], [profile], now=100)
    result = Reconciler().reconcile(plan, [old, target], [profile], now=100)
    assert [item.kind for item in result.actions] == [ActionKind.WARM]
    assert any(item.code == "replacement_not_ready" for item in result.deferred)


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
    machine = node("n", residencies=(draining,))
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
    assert any(item.kind == ActionKind.WARM and item.code == "cooldown" for item in result.deferred)


def test_reconciler_failed_desired_residency_bypasses_only_old_success_observation():
    profile = model(min_replicas=1, max_replicas=1, min_residency_seconds=0)
    failed_residency = ModelResidency(
        "qwen",
        8_000,
        ResidencyState.FAILED,
        loaded_at=90,
        managed=True,
    )
    machine = node("n", residencies=(failed_residency,), cached=("qwen",))
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
    assert next(item for item in waiting.deferred if item.code == "cooldown").retry_at == 50
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
        for item in reconciler.reconcile(plan, [machine], [profile], [failed], now=20).actions
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
    assert [item.action_id for item in one.actions] == [item.action_id for item in two.actions]
