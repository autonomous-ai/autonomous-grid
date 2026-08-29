from __future__ import annotations

import hashlib

import pytest

from shared.allocator.intelligence import (
    RequestFeatures,
    WorkloadIntelligence,
    anonymous_tenant_cohort,
    classify_request,
)
from shared.allocator.models import (
    DemandForecast,
    ModelProfile,
    ModelResidency,
    NodeSnapshot,
    ResidencyState,
)
from shared.allocator.planner import PlacementPlanner


def profile(
    model: str,
    memory: int,
    *scores: tuple[str, float],
    artifact_sha256: str = "",
) -> ModelProfile:
    return ModelProfile(
        model_id=model,
        memory_mb=memory,
        min_replicas=0,
        max_replicas=2,
        load_seconds=0,
        warm_seconds=0,
        min_residency_seconds=0,
        workload_scores=scores,
        artifact_sha256=artifact_sha256,
    )


def test_classification_retains_only_bounded_features():
    body = {
        "model": "auto",
        "messages": [
            {"role": "user", "content": "Debug this Python API and add unit tests"},
            {
                "role": "user",
                "content": [
                    {"type": "input_image", "image_url": "data:image/png;base64,secret"}
                ],
            },
        ],
        "max_completion_tokens": 900,
    }

    features = classify_request("chat/completions", body)

    assert features.requested_model == "auto"
    assert features.workload == "coding"
    assert features.modalities == ("image", "text")
    assert features.input_units > 0
    assert features.requested_output_units == 900
    assert "secret" not in repr(features)


def test_request_features_accept_only_fixed_anonymous_tenant_cohorts():
    assert RequestFeatures(
        "chat/completions", "auto", tenant_class="cohort-15"
    ).tenant_class == ("cohort-15")
    for unsafe in ("alice@example.com", "cohort-16", "customer-secret"):
        with pytest.raises(ValueError, match="anonymous bounded cohort"):
            RequestFeatures("chat/completions", "auto", tenant_class=unsafe)


def test_media_endpoint_is_a_hard_workload_signal():
    features = classify_request(
        "media/video/i2v",
        {"model": "video", "prompt": "make an ad", "image": "opaque"},
    )
    assert features.workload == "video"
    assert "video" in features.modalities


def test_aggregate_unbound_demand_creates_one_non_destructive_portfolio_projection():
    intelligence = WorkloadIntelligence(portfolio_min_samples=3)
    features = RequestFeatures(
        endpoint="chat/completions",
        requested_model="auto",
        workload="coding",
    )
    for second in range(3):
        intelligence.observe(
            features,
            portfolio_unbound=True,
            service_seconds=4,
            latency_ms=9_000,
            queue_depth=3,
            error=second == 0,
            timestamp=1_000 + second,
        )

    projected = intelligence.portfolio_forecasts(
        (
            profile("general", 1_000, ("coding", 0.5)),
            profile("coder", 2_000, ("coding", 0.9)),
        ),
        (),
        now=1_002,
    )

    assert len(projected) == 1
    assert projected[0].model_id == "coder"
    assert projected[0].observed_requests_per_minute == 0
    assert (
        projected[0].correlated_requests_per_minute == projected[0].requests_per_minute
    )
    assert projected[0].correlation_sources == ("workload:coding",)
    assert projected[0].queue_depth == 0
    assert projected[0].p95_latency_ms == 0
    assert projected[0].error_rate == 0


def test_direct_and_portfolio_pressure_merge_without_erasing_direct_lineage():
    intelligence = WorkloadIntelligence(portfolio_min_samples=1)
    intelligence.observe(
        RequestFeatures("chat/completions", "auto", "coding"),
        portfolio_unbound=True,
        service_seconds=3,
        timestamp=1_000,
    )
    direct = DemandForecast(
        model_id="coder",
        requests_per_minute=2,
        observed_requests_per_minute=2,
        offered_concurrency=1,
        confidence=1,
        sample_count=2,
        updated_at=1_000,
    )

    result = intelligence.portfolio_forecasts(
        (profile("coder", 2_000, ("coding", 1.0)),),
        (direct,),
        now=1_000,
    )[0]

    assert result.observed_requests_per_minute == 2
    assert result.correlated_requests_per_minute > 0
    assert result.requests_per_minute > 2
    assert result.sample_count == 3


def test_joint_selector_can_force_different_fleet_feasible_candidates_per_workload():
    intelligence = WorkloadIntelligence(portfolio_min_samples=1)
    candidates = (
        profile("general", 1_000, ("coding", 0.8), ("research", 0.8)),
        profile("coder", 1_000, ("coding", 1.0)),
        profile("researcher", 1_000, ("research", 1.0)),
    )
    for workload in ("coding", "research"):
        intelligence.observe(
            RequestFeatures("chat/completions", "auto", workload),
            portfolio_unbound=True,
            timestamp=100,
        )

    forecasts = intelligence.portfolio_forecasts(
        candidates,
        (),
        now=100,
        chosen_models={"coding": "general", "research": "researcher"},
    )
    projections = intelligence.projections(
        candidates,
        now=100,
        chosen_models={"coding": "general", "research": "researcher"},
    )

    assert {item.model_id for item in forecasts} == {"general", "researcher"}
    assert {item["workload"]: item["chosen_model"] for item in projections} == {
        "coding": "general",
        "research": "researcher",
    }
    assert all("joint fleet optimization" in item["reason"] for item in projections)


def test_model_outcomes_are_bounded_and_persisted_without_content():
    intelligence = WorkloadIntelligence()
    features = RequestFeatures("chat/completions", "coder", "coding")
    intelligence.observe(
        features,
        served_model="coder",
        service_seconds=2,
        output_units=50,
        quality=0.8,
        timestamp=1_000,
    )
    restored = WorkloadIntelligence.from_dict(intelligence.to_dict())

    assert restored.outcomes[0].model_id == "coder"
    assert restored.outcomes[0].requests == 1
    assert restored.outcomes[0].quality == 0.8
    assert "prompt" not in str(restored.to_dict()).lower()


def test_model_evaluation_updates_quality_without_creating_demand():
    intelligence = WorkloadIntelligence()

    outcome = intelligence.observe_model_evaluation(
        "coder",
        "coding",
        quality=0.75,
        latency_ms=125,
        output_units=32,
        timestamp=10,
    )

    assert outcome.model_id == "coder"
    assert outcome.quality == 0.75
    assert outcome.quality_samples == 1
    assert outcome.latency_ms == 125
    assert intelligence.demand.to_dict()["models"] == {}
    assert intelligence.unbound_demand.to_dict()["models"] == {}


def test_measured_quality_and_latency_choose_between_equal_portfolio_candidates():
    intelligence = WorkloadIntelligence(portfolio_min_samples=1)
    candidates = (
        ModelProfile(
            model_id="fast-correct",
            memory_mb=1_000,
            min_replicas=0,
            max_replicas=1,
            latency_slo_ms=1_000,
            workload_scores=(("coding", 1.0),),
        ),
        ModelProfile(
            model_id="slow-wrong",
            memory_mb=1_000,
            min_replicas=0,
            max_replicas=1,
            latency_slo_ms=1_000,
            workload_scores=(("coding", 1.0),),
        ),
    )
    for index in range(20):
        intelligence.observe_model_evaluation(
            "fast-correct", "coding", quality=1.0, latency_ms=100, timestamp=index
        )
        intelligence.observe_model_evaluation(
            "slow-wrong", "coding", quality=0.0, latency_ms=2_000, timestamp=index
        )
    intelligence.observe(
        RequestFeatures("chat/completions", "auto", "coding"),
        portfolio_unbound=True,
        timestamp=100,
    )

    projection = intelligence.portfolio_forecasts(candidates, (), now=100)

    assert projection[0].model_id == "fast-correct"


def test_uncertainty_bonus_explores_cold_peer_but_yields_to_strong_fresh_evidence():
    intelligence = WorkloadIntelligence(portfolio_min_samples=1)
    incumbent = profile("z-incumbent", 8_000, ("coding", 1.0))
    cold = profile("a-cold", 8_000, ("coding", 1.0))

    intelligence.observe(
        RequestFeatures("chat/completions", "z-incumbent", "coding"),
        served_model="z-incumbent",
        timestamp=1,
    )
    intelligence.observe(
        RequestFeatures("chat/completions", "auto", "coding"),
        portfolio_unbound=True,
        timestamp=100,
    )
    assert intelligence.portfolio_forecasts((incumbent, cold), (), now=100)[
        0
    ].model_id == ("a-cold")

    for timestamp in range(2, 21):
        intelligence.observe(
            RequestFeatures("chat/completions", "z-incumbent", "coding"),
            served_model="z-incumbent",
            timestamp=timestamp,
        )
    assert intelligence.portfolio_forecasts((incumbent, cold), (), now=100)[
        0
    ].model_id == ("z-incumbent")


def test_stale_outcomes_decay_and_candidate_status_explains_effective_evidence():
    intelligence = WorkloadIntelligence(portfolio_min_samples=1)
    stale = profile("stale-perfect", 8_000, ("coding", 1.0))
    recent = profile("recent-good", 8_000, ("coding", 1.0))
    now = 8 * 7 * 24 * 60 * 60.0
    for _ in range(20):
        intelligence.observe_model_evaluation(
            "stale-perfect", "coding", quality=1.0, latency_ms=100, timestamp=0
        )
    for _ in range(4):
        intelligence.observe_model_evaluation(
            "recent-good", "coding", quality=0.7, latency_ms=100, timestamp=now - 1
        )
    intelligence.observe(
        RequestFeatures("chat/completions", "auto", "coding"),
        portfolio_unbound=True,
        timestamp=now,
    )

    projection = intelligence.projections((stale, recent), now=now)[0]
    candidates = {row["model_id"]: row for row in projection["candidates"]}
    assert projection["chosen_model"] == "recent-good"
    assert candidates["stale-perfect"]["evidence"]["freshness"] == pytest.approx(
        1 / 256
    )
    assert candidates["stale-perfect"]["evidence"]["effective_requests"] == (
        pytest.approx(20 / 256)
    )
    assert (
        candidates["recent-good"]["evidence"]["confidence"]
        > candidates["stale-perfect"]["evidence"]["confidence"]
    )


def test_fresh_service_request_does_not_revive_stale_quality_evidence():
    intelligence = WorkloadIntelligence(portfolio_min_samples=1)
    candidate = profile("coder", 8_000, ("coding", 1.0))
    half_life = 7 * 24 * 60 * 60
    now = 8 * half_life
    for _ in range(8):
        intelligence.observe_model_evaluation(
            "coder", "coding", quality=1.0, timestamp=0
        )

    intelligence.observe(
        RequestFeatures("chat/completions", "coder", "coding"),
        served_model="coder",
        timestamp=now,
    )

    evidence = intelligence.projections((candidate,), now=now)
    assert evidence == ()
    outcome_evidence = intelligence._outcome_evidence("coder", "coding", now=now)
    assert outcome_evidence["freshness"] == 1.0
    assert outcome_evidence["quality_freshness"] == pytest.approx(1 / 256)
    assert outcome_evidence["quality_confidence"] == pytest.approx(1 / 256)


def test_eight_fresh_evaluations_give_full_independent_quality_confidence():
    intelligence = WorkloadIntelligence()
    for _ in range(8):
        intelligence.observe_model_evaluation(
            "coder", "coding", quality=0.8, timestamp=100
        )

    evidence = intelligence._outcome_evidence("coder", "coding", now=100)

    assert evidence["confidence"] == pytest.approx(8 / 20)
    assert evidence["quality_confidence"] == 1.0


def test_model_quality_evidence_is_bound_to_artifact_revision():
    intelligence = WorkloadIntelligence()
    revision_a = "a" * 64
    revision_b = "b" * 64
    for _ in range(8):
        intelligence.observe_model_evaluation(
            "coder",
            "coding",
            artifact_sha256=revision_a,
            quality=1.0,
            timestamp=100,
        )

    evidence_a = intelligence._outcome_evidence(
        "coder", "coding", artifact_sha256=revision_a, now=100
    )
    evidence_b = intelligence._outcome_evidence(
        "coder", "coding", artifact_sha256=revision_b, now=100
    )

    assert evidence_a["quality_confidence"] == 1.0
    assert evidence_b["quality_confidence"] == 0.0
    assert evidence_b["exploration_bonus"] > 0


def test_legacy_shared_timestamp_discards_ambiguous_quality_on_restore():
    intelligence = WorkloadIntelligence()
    intelligence.observe_model_evaluation("coder", "coding", quality=1.0, timestamp=100)
    payload = intelligence.to_dict()
    legacy = payload["outcomes"][0]
    legacy["updated_at"] = legacy.pop("service_updated_at")
    legacy.pop("quality_updated_at")
    legacy.pop("artifact_sha256")

    restored = WorkloadIntelligence.from_dict(payload)
    evidence = restored._outcome_evidence("coder", "coding", now=100)

    assert restored.outcomes[0].quality_samples == 0
    assert evidence["confidence"] > 0
    assert evidence["quality_confidence"] == 0.0


def test_uncertain_preemption_only_candidate_pays_more_than_exploration_bonus():
    intelligence = WorkloadIntelligence(portfolio_min_samples=1)
    feasible = profile("a-feasible", 8_000, ("coding", 1.0))
    preempting = profile("z-preempting", 8_000, ("coding", 1.0))
    hints = {
        "a-feasible": {"feasible": True, "feasible_now": True},
        "z-preempting": {
            "feasible": False,
            "feasible_now": False,
            "feasible_after_preemption": True,
        },
    }

    feasible_score = intelligence._portfolio_score_with_placement(
        feasible, "coding", hints, now=100
    )
    preempting_score = intelligence._portfolio_score_with_placement(
        preempting, "coding", hints, now=100
    )
    assert feasible_score > preempting_score


def test_portfolio_falls_back_from_preferred_but_infeasible_model():
    intelligence = WorkloadIntelligence(portfolio_min_samples=1)
    candidates = (
        profile("preferred-huge", 32_000, ("coding", 1.0)),
        profile("feasible-small", 8_000, ("coding", 0.7)),
    )
    intelligence.observe(
        RequestFeatures("chat/completions", "auto", "coding"),
        portfolio_unbound=True,
        timestamp=100,
    )
    placement_hints = {
        "preferred-huge": {
            "feasible": False,
            "reason": "no live node is currently eligible",
        },
        "feasible-small": {
            "feasible": True,
            "best_node_id": "cheap-node",
            "cost_per_hour": 0.20,
        },
    }

    forecasts = intelligence.portfolio_forecasts(
        candidates,
        (),
        now=100,
        placement_hints=placement_hints,
    )
    projection = intelligence.projections(
        candidates,
        now=100,
        placement_hints=placement_hints,
    )[0]

    assert forecasts[0].model_id == "feasible-small"
    assert projection["chosen_model"] == "feasible-small"
    assert projection["placement"]["best_node_id"] == "cheap-node"
    candidate_rows = {row["model_id"]: row for row in projection["candidates"]}
    assert candidate_rows["preferred-huge"]["feasible"] is False


def test_portfolio_cost_breaks_an_otherwise_equal_model_tie():
    intelligence = WorkloadIntelligence(portfolio_min_samples=1)
    candidates = (
        profile("a-cheap", 8_000, ("coding", 1.0)),
        profile("z-expensive", 8_000, ("coding", 1.0)),
    )
    intelligence.observe(
        RequestFeatures("chat/completions", "auto", "coding"),
        portfolio_unbound=True,
        timestamp=100,
    )

    forecasts = intelligence.portfolio_forecasts(
        candidates,
        (),
        now=100,
        placement_hints={
            "a-cheap": {"feasible": True, "cost_per_hour": 0.05},
            "z-expensive": {"feasible": True, "cost_per_hour": 5.00},
        },
    )

    assert forecasts[0].model_id == "a-cheap"


def test_resident_model_hysteresis_rejects_tiny_gain_but_allows_clear_improvement():
    intelligence = WorkloadIntelligence(portfolio_min_samples=1)
    incumbent = profile("incumbent", 8_000, ("coding", 0.8))
    tiny_challenger = profile("challenger", 8_000, ("coding", 0.805))
    strong_challenger = profile("challenger", 8_000, ("coding", 0.9))
    intelligence.observe(
        RequestFeatures("chat/completions", "auto", "coding"),
        portfolio_unbound=True,
        timestamp=100,
    )
    placement_hints = {
        "incumbent": {
            "feasible": True,
            "feasible_now": True,
            "startup_seconds": 0,
        },
        "challenger": {
            "feasible": True,
            "feasible_now": True,
            "startup_seconds": 60,
        },
    }

    stable = intelligence.projections(
        (incumbent, tiny_challenger),
        now=100,
        placement_hints=placement_hints,
    )[0]
    switched = intelligence.projections(
        (incumbent, strong_challenger),
        now=100,
        placement_hints=placement_hints,
    )[0]

    stable_rows = {row["model_id"]: row for row in stable["candidates"]}
    assert stable["chosen_model"] == "incumbent"
    assert stable_rows["incumbent"]["transition_penalty"] == 0.0
    assert stable_rows["challenger"]["transition_penalty"] > 0.01
    assert switched["chosen_model"] == "challenger"


def test_broad_cohort_slo_failure_graduates_portfolio_allocation_pressure():
    intelligence = WorkloadIntelligence(portfolio_min_samples=1)
    candidate = profile("coder", 8_000, ("coding", 1.0))
    for cohort in range(3):
        for _ in range(4):
            intelligence.observe(
                RequestFeatures(
                    "chat/completions",
                    "auto",
                    "coding",
                    tenant_class=f"cohort-{cohort:02d}",
                    tenant_attested=True,
                ),
                portfolio_unbound=True,
                service_seconds=2,
                latency_ms=2_000,
                error=True,
                timestamp=100,
            )

    forecast = intelligence.portfolio_forecasts((candidate,), (), now=100)[0]
    plan = PlacementPlanner().plan(
        (NodeSnapshot("node", 16_000, last_heartbeat=100),),
        (candidate,),
        (forecast,),
        now=100,
    )
    projection = intelligence.projections((candidate,), now=100)[0]

    assert forecast.active_cohorts == 3
    assert forecast.trusted_active_cohorts == 3
    assert forecast.cohort_slo_breach_rate == 1
    assert forecast.trusted_cohort_graduated is True
    assert plan.urgency_for("coder") == 2
    assert projection["cohort_evidence"]["graduated_allocation_pressure"] is True


def test_one_slow_outlier_per_cohort_does_not_manufacture_p95_slo_breach():
    intelligence = WorkloadIntelligence(portfolio_min_samples=1)
    candidate = ModelProfile(
        "coder",
        8_000,
        min_replicas=0,
        max_replicas=1,
        latency_slo_ms=1_000,
        workload_scores=(("coding", 1.0),),
    )
    for cohort in range(3):
        features = RequestFeatures(
            "chat/completions",
            "auto",
            "coding",
            tenant_class=f"cohort-{cohort:02d}",
            tenant_attested=True,
        )
        for _ in range(99):
            intelligence.observe(
                features,
                portfolio_unbound=True,
                latency_ms=100,
                timestamp=100,
            )
        intelligence.observe(
            features,
            portfolio_unbound=True,
            latency_ms=2_000,
            timestamp=100,
        )

    projection = intelligence.projections((candidate,), now=100)[0]
    cohort = projection["cohort_evidence"]

    assert cohort["trusted_active_cohorts"] == 3
    assert cohort["trusted_samples"] == 300
    assert cohort["trusted_slo_breach_rate"] == 0.0
    assert cohort["graduated_allocation_pressure"] is False


def test_one_noisy_cohort_remains_non_destructive_canary_pressure():
    intelligence = WorkloadIntelligence(portfolio_min_samples=1)
    candidate = profile("coder", 8_000, ("coding", 1.0))
    for _ in range(20):
        intelligence.observe(
            RequestFeatures(
                "chat/completions",
                "auto",
                "coding",
                tenant_class="cohort-00",
            ),
            portfolio_unbound=True,
            error=True,
            timestamp=100,
        )

    forecast = intelligence.portfolio_forecasts((candidate,), (), now=100)[0]
    plan = PlacementPlanner().plan(
        (NodeSnapshot("node", 16_000, last_heartbeat=100),),
        (candidate,),
        (forecast,),
        now=100,
    )

    assert forecast.active_cohorts == 1
    assert forecast.trusted_active_cohorts == 0
    assert forecast.trusted_cohort_graduated is False
    assert plan.urgency_for("coder") == 1


def test_minority_cohort_failure_does_not_promote_allocation_pressure():
    intelligence = WorkloadIntelligence(portfolio_min_samples=1)
    candidate = profile("coder", 8_000, ("coding", 1.0))
    for cohort in range(4):
        for _ in range(3):
            intelligence.observe(
                RequestFeatures(
                    "chat/completions",
                    "auto",
                    "coding",
                    tenant_class=f"cohort-{cohort:02d}",
                    tenant_attested=True,
                ),
                portfolio_unbound=True,
                error=cohort == 0,
                timestamp=100,
            )

    forecast = intelligence.portfolio_forecasts((candidate,), (), now=100)[0]
    plan = PlacementPlanner().plan(
        (NodeSnapshot("node", 16_000, last_heartbeat=100),),
        (candidate,),
        (forecast,),
        now=100,
    )

    assert forecast.active_cohorts == 4
    assert forecast.trusted_active_cohorts == 4
    assert forecast.cohort_slo_breach_rate == 0.25
    assert forecast.trusted_cohort_graduated is False
    assert plan.urgency_for("coder") == 1


def test_rotating_affinity_keys_cannot_create_unbounded_cohort_state():
    intelligence = WorkloadIntelligence(portfolio_min_samples=1)
    for index in range(1_000):
        digest = hashlib.sha256(f"rotating-user-{index}".encode()).digest()
        intelligence.observe(
            RequestFeatures(
                "chat/completions",
                "auto",
                "coding",
                tenant_class=anonymous_tenant_cohort(digest),
            ),
            portfolio_unbound=True,
            error=True,
            timestamp=100,
        )

    state = intelligence.to_dict()["cohort_demand"]

    assert len(state["models"]) == 16
    assert all(len(buckets) == 1 for buckets in state["models"].values())

    candidate = profile("coder", 8_000, ("coding", 1.0))
    forecast = intelligence.portfolio_forecasts((candidate,), (), now=100)[0]
    plan = PlacementPlanner().plan(
        (NodeSnapshot("node", 16_000, last_heartbeat=100),),
        (candidate,),
        (forecast,),
        now=100,
    )
    assert forecast.trusted_active_cohorts == 0
    assert forecast.trusted_cohort_graduated is False
    assert plan.urgency_for("coder") == 1


def test_skewed_trusted_cohort_samples_do_not_graduate_pressure():
    intelligence = WorkloadIntelligence(portfolio_min_samples=1)
    candidate = profile("coder", 8_000, ("coding", 1.0))
    for cohort, samples in enumerate((10, 1, 1)):
        for _ in range(samples):
            intelligence.observe(
                RequestFeatures(
                    "chat/completions",
                    "auto",
                    "coding",
                    tenant_class=f"cohort-{cohort:02d}",
                    tenant_attested=True,
                ),
                portfolio_unbound=True,
                error=True,
                timestamp=100,
            )

    forecast = intelligence.portfolio_forecasts((candidate,), (), now=100)[0]
    evidence = intelligence.projections((candidate,), now=100)[0]["cohort_evidence"]

    assert forecast.trusted_active_cohorts == 3
    assert forecast.trusted_cohort_graduated is False
    assert evidence["trusted_qualifying_cohorts"] == 1


def test_graduated_cohort_pressure_can_reclaim_a_speculative_only_slot():
    intelligence = WorkloadIntelligence(portfolio_min_samples=1)
    coder = profile("coder", 8_000, ("coding", 1.0))
    speculative = profile("old-speculative", 8_000, ("general", 1.0))
    for cohort in range(3):
        for _ in range(4):
            intelligence.observe(
                RequestFeatures(
                    "chat/completions",
                    "auto",
                    "coding",
                    tenant_class=f"cohort-{cohort:02d}",
                    tenant_attested=True,
                ),
                portfolio_unbound=True,
                error=True,
                timestamp=100,
            )
    promoted = intelligence.portfolio_forecasts((coder, speculative), (), now=100)[0]
    old_speculation = DemandForecast(
        "old-speculative",
        requests_per_minute=1,
        correlated_requests_per_minute=1,
        correlation_confidence=1,
        correlation_sources=("historical-association",),
        sample_count=3,
        updated_at=100,
    )
    machine = NodeSnapshot(
        "only-slot",
        16_000,
        max_models=1,
        residencies=(
            ModelResidency(
                "old-speculative",
                8_000,
                ResidencyState.READY,
                managed=True,
            ),
        ),
        last_heartbeat=100,
    )

    plan = PlacementPlanner().plan(
        (machine,),
        (coder, speculative),
        (promoted, old_speculation),
        now=100,
    )

    assert plan.urgency_for("coder") == 2
    assert plan.urgency_for("old-speculative") == 1
    assert [(item.model_id, item.for_model_id) for item in plan.preemptions] == [
        ("old-speculative", "coder")
    ]


def test_cohort_fairness_is_aggregated_and_persisted_without_identity():
    intelligence = WorkloadIntelligence(portfolio_min_samples=1)
    for cohort, error in (("cohort-00", False), ("cohort-01", True)):
        for _ in range(4):
            intelligence.observe(
                RequestFeatures(
                    "chat/completions",
                    "auto",
                    "coding",
                    tenant_class=cohort,
                ),
                portfolio_unbound=True,
                service_seconds=1,
                error=error,
                timestamp=100,
            )
    state = intelligence.to_dict()
    restored = WorkloadIntelligence.from_dict(state)

    summary = intelligence.cohort_summaries(now=100)[0]

    assert summary["active_cohorts"] == 2
    assert summary["slo_breach_rate"] == 0.5
    assert summary["fairness"] == 0.5
    assert restored.cohort_summaries(now=100) == intelligence.cohort_summaries(now=100)
    assert "alice" not in str(state)
    assert intelligence.cohort_summaries(now=401)[0]["active_cohorts"] == 0
