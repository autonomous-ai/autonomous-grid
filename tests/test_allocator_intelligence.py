from __future__ import annotations

import pytest

from shared.allocator.demand import DemandTracker
from shared.allocator.intelligence import (
    RequestFeatures,
    WorkloadIntelligence,
    classify_request,
)
from shared.allocator.models import DemandForecast, ModelProfile


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


def observe_workflow(
    intelligence: WorkloadIntelligence,
    workload: str,
    timestamp: float,
    *,
    workflow_key: str = "campaign-session",
    service_seconds: float | None = None,
) -> None:
    intelligence.observe(
        RequestFeatures(f"{workload}/request", "auto", workload),
        portfolio_unbound=True,
        service_seconds=(
            service_seconds
            if service_seconds is not None
            else (45 if workload == "video" else 20)
        ),
        workflow_key=workflow_key,
        timestamp=timestamp,
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


def test_prepared_workload_forecasts_are_reused_without_reserializing_demand(monkeypatch):
    intelligence = WorkloadIntelligence(portfolio_min_samples=1)
    intelligence.observe(
        RequestFeatures("chat/completions", "auto", "coding"),
        portfolio_unbound=True,
        service_seconds=4,
        timestamp=100,
    )
    profiles = (profile("coder", 2_000, ("coding", 1.0)),)
    prepared = intelligence.portfolio_workload_forecast_map(now=100)
    expected_forecasts = intelligence.portfolio_forecasts(profiles, (), now=100)
    expected_projections = intelligence.projections(profiles, now=100)

    def fail_serialization(_self):
        raise AssertionError("prepared workload forecasts must avoid tracker serialization")

    monkeypatch.setattr(DemandTracker, "to_dict", fail_serialization)

    assert intelligence.portfolio_forecasts(
        profiles,
        (),
        now=100,
        workload_forecasts=prepared,
    ) == expected_forecasts
    assert intelligence.projections(
        profiles,
        now=100,
        workload_forecasts=prepared,
    ) == expected_projections


def test_long_sparse_job_gets_one_queue_promoted_canary_before_cheap_call():
    intelligence = WorkloadIntelligence(
        portfolio_min_samples=3,
        portfolio_min_offered_concurrency=1.5,
    )
    intelligence.observe(
        RequestFeatures("videos/generations", "auto", "video"),
        portfolio_unbound=True,
        service_seconds=60,
        queue_depth=1,
        timestamp=100,
    )
    intelligence.observe(
        RequestFeatures("chat/completions", "auto", "coding"),
        portfolio_unbound=True,
        service_seconds=1,
        queue_depth=1,
        timestamp=100,
    )

    projected = intelligence.portfolio_forecasts(
        (
            profile("video-model", 2_000, ("video", 1.0)),
            profile("code-model", 2_000, ("coding", 1.0)),
        ),
        (),
        now=100,
    )

    assert [forecast.model_id for forecast in projected] == ["video-model"]
    assert projected[0].sample_count == 1
    assert projected[0].offered_concurrency == pytest.approx(1.0)
    assert projected[0].canary_only is True


def test_weak_device_pressure_gets_one_canary_only_when_spare_placement_is_proven():
    intelligence = WorkloadIntelligence(
        portfolio_min_samples=3,
        portfolio_min_offered_concurrency=1.5,
    )
    intelligence.observe(
        RequestFeatures("videos/generations", "auto", "video"),
        portfolio_unbound=True,
        service_seconds=20,
        queue_depth=1,
        timestamp=100,
    )
    candidate = profile("video-model", 2_000, ("video", 1.0))

    assert intelligence.portfolio_forecasts((candidate,), (), now=100) == ()
    projected = intelligence.portfolio_forecasts(
        (candidate,),
        (),
        now=100,
        placement_hints={
            "video-model": {
                "feasible": True,
                "feasible_now": True,
                "spare_canary_allowed": True,
            },
        },
    )

    assert len(projected) == 1
    assert projected[0].offered_concurrency == pytest.approx(1 / 3)
    assert projected[0].canary_only is True


def test_real_model_demand_lifts_weak_workload_canary_cap():
    intelligence = WorkloadIntelligence(
        portfolio_min_samples=3,
        portfolio_min_offered_concurrency=1.5,
    )
    intelligence.observe(
        RequestFeatures("videos/generations", "auto", "video"),
        portfolio_unbound=True,
        service_seconds=20,
        timestamp=100,
    )
    direct = DemandForecast(
        "video-model",
        requests_per_minute=1,
        observed_requests_per_minute=1,
        offered_concurrency=0.1,
        sample_count=1,
        updated_at=100,
    )

    projected = intelligence.portfolio_forecasts(
        (profile("video-model", 2_000, ("video", 1.0)),),
        (direct,),
        now=100,
        placement_hints={
            "video-model": {
                "feasible": True,
                "feasible_now": True,
                "spare_canary_allowed": True,
            },
        },
    )

    assert projected[0].observed_requests_per_minute == 1
    assert projected[0].canary_only is False


def test_successful_canary_response_lifts_weak_workload_scale_out_cap():
    intelligence = WorkloadIntelligence(
        portfolio_min_samples=3,
        portfolio_min_offered_concurrency=1.5,
    )
    features = RequestFeatures("videos/generations", "auto", "video")
    intelligence.observe(
        features,
        portfolio_unbound=True,
        service_seconds=20,
        queue_depth=1,
        timestamp=100,
    )
    intelligence.observe(
        features,
        served_model="video-model",
        portfolio_unbound=True,
        service_seconds=0,
        quality=0.9,
        timestamp=101,
    )

    projected = intelligence.portfolio_forecasts(
        (profile("video-model", 2_000, ("video", 1.0)),),
        (),
        now=101,
        placement_hints={
            "video-model": {
                "feasible": True,
                "feasible_now": True,
                "spare_canary_allowed": True,
            },
        },
    )

    assert projected[0].sample_count == 2
    assert projected[0].offered_concurrency == pytest.approx(1 / 3)
    assert projected[0].canary_only is False


def test_portfolio_prewarms_a_workload_from_a_mature_directional_sequence():
    intelligence = WorkloadIntelligence(portfolio_min_samples=3)
    intelligence.unbound_demand = DemandTracker(
        bucket_seconds=10,
        window_seconds=1_000,
        ewma_alpha=1,
        confidence_samples=1,
    )
    for image_timestamp in (1, 21, 41, 61, 81):
        observe_workflow(intelligence, "image", image_timestamp)
        observe_workflow(intelligence, "video", image_timestamp + 10)
    observe_workflow(intelligence, "image", 101)
    candidates = (
        profile("image-model", 2_000, ("image", 1.0)),
        profile("video-model", 2_000, ("video", 1.0)),
    )

    independent = intelligence.unbound_demand.forecast("video", now=101)
    projected = {
        forecast.model_id: forecast
        for forecast in intelligence.portfolio_forecasts(candidates, (), now=101)
    }
    projection_rows = {
        row["workload"]: row
        for row in intelligence.projections(candidates, now=101)
    }

    assert independent.requests_per_minute == 0
    assert projected["video-model"].requests_per_minute > 0
    assert projected["video-model"].observed_requests_per_minute == 0
    assert projected["video-model"].prediction_lead_seconds == pytest.approx(10)
    assert projected["video-model"].correlation_sources == (
        "workload-predictor:image",
        "workload:video",
    )
    assert projection_rows["video"]["demand_correlation_sources"] == ("image",)
    assert projection_rows["video"]["demand_correlation_confidence"] == pytest.approx(
        6 / 7
    )
    assert projection_rows["video"]["prediction_lead_seconds"] == pytest.approx(10)
    serialized = intelligence.to_dict()
    assert "campaign-session" not in repr(serialized)
    assert {
        row["lead_seconds"] for row in serialized["workflow_sequences"]["transitions"]
    } == {10}
    restored = WorkloadIntelligence.from_dict(serialized)
    assert {
        forecast.model_id for forecast in restored.portfolio_forecasts(candidates, (), now=101)
    } == {"image-model", "video-model"}
    for row in serialized["workflow_sequences"]["transitions"]:
        row.pop("lead_seconds")
    legacy_restored = WorkloadIntelligence.from_dict(serialized)
    legacy_projection = {
        forecast.model_id: forecast
        for forecast in legacy_restored.portfolio_forecasts(candidates, (), now=101)
    }
    assert legacy_projection["video-model"].prediction_lead_seconds is None


def test_portfolio_does_not_predict_a_workload_from_two_directional_examples():
    intelligence = WorkloadIntelligence(portfolio_min_samples=3)
    intelligence.unbound_demand = DemandTracker(
        bucket_seconds=10,
        window_seconds=1_000,
        ewma_alpha=1,
        confidence_samples=1,
    )
    for image_timestamp in (1, 21):
        observe_workflow(intelligence, "image", image_timestamp)
        observe_workflow(intelligence, "video", image_timestamp + 10)
    observe_workflow(intelligence, "image", 41)

    projected = intelligence.portfolio_forecasts(
        (
            profile("image-model", 2_000, ("image", 1.0)),
            profile("video-model", 2_000, ("video", 1.0)),
        ),
        (),
        now=41,
    )

    assert [forecast.model_id for forecast in projected] == ["image-model"]


def test_portfolio_does_not_promote_four_perfect_but_immature_transitions():
    intelligence = WorkloadIntelligence(portfolio_min_samples=3)
    intelligence.unbound_demand = DemandTracker(
        bucket_seconds=10,
        window_seconds=1_000,
        ewma_alpha=1,
        confidence_samples=1,
    )
    for image_timestamp in (1, 21, 41, 61):
        observe_workflow(intelligence, "image", image_timestamp)
        observe_workflow(intelligence, "video", image_timestamp + 10)
    observe_workflow(intelligence, "image", 81)

    projected = intelligence.portfolio_forecasts(
        (
            profile("image-model", 2_000, ("image", 1.0)),
            profile("video-model", 2_000, ("video", 1.0)),
        ),
        (),
        now=81,
    )

    assert [forecast.model_id for forecast in projected] == ["image-model"]


def test_portfolio_shrinks_a_barely_passing_sequence_below_the_action_threshold():
    intelligence = WorkloadIntelligence(portfolio_min_samples=3)
    intelligence.unbound_demand = DemandTracker(
        bucket_seconds=10,
        window_seconds=1_000,
        ewma_alpha=1,
        confidence_samples=1,
    )
    for image_timestamp in (1, 21, 41, 61, 81):
        observe_workflow(intelligence, "image", image_timestamp)
        observe_workflow(intelligence, "video", image_timestamp + 10)
    # Two observed departures to another target leave image→video at 5/7 (> 0.70). A
    # small-sample posterior is less certain and must not move a model yet.
    observe_workflow(intelligence, "image", 101)
    observe_workflow(intelligence, "research", 111)
    observe_workflow(intelligence, "image", 121)
    observe_workflow(intelligence, "research", 131)
    observe_workflow(intelligence, "image", 141)

    projected = intelligence.portfolio_forecasts(
        (
            profile("image-model", 2_000, ("image", 1.0)),
            profile("video-model", 2_000, ("video", 1.0)),
        ),
        (),
        now=141,
    )

    assert [forecast.model_id for forecast in projected] == ["image-model"]


def test_portfolio_does_not_infer_workflow_from_unrelated_users_in_adjacent_buckets():
    intelligence = WorkloadIntelligence(portfolio_min_samples=3)
    intelligence.unbound_demand = DemandTracker(
        bucket_seconds=10,
        window_seconds=1_000,
        ewma_alpha=1,
        confidence_samples=1,
    )
    for image_timestamp in (1, 21, 41, 61, 81):
        observe_workflow(
            intelligence,
            "image",
            image_timestamp,
            workflow_key="image-team",
        )
        observe_workflow(
            intelligence,
            "video",
            image_timestamp + 10,
            workflow_key="video-team",
        )
    observe_workflow(intelligence, "image", 101, workflow_key="image-team")

    projected = intelligence.portfolio_forecasts(
        (
            profile("image-model", 2_000, ("image", 1.0)),
            profile("video-model", 2_000, ("video", 1.0)),
        ),
        (),
        now=101,
    )

    assert [forecast.model_id for forecast in projected] == ["image-model"]


def test_portfolio_does_not_treat_mature_coactivity_as_a_directional_transition():
    intelligence = WorkloadIntelligence(portfolio_min_samples=3)
    intelligence.unbound_demand = DemandTracker(
        bucket_seconds=10,
        window_seconds=1_000,
        ewma_alpha=1,
        confidence_samples=1,
    )
    for timestamp in (1, 21, 41, 61):
        observe_workflow(intelligence, "image", timestamp)
        observe_workflow(intelligence, "video", timestamp)
    observe_workflow(intelligence, "image", 81)

    projected = intelligence.portfolio_forecasts(
        (
            profile("image-model", 2_000, ("image", 1.0)),
            profile("video-model", 2_000, ("video", 1.0)),
        ),
        (),
        now=81,
    )

    assert [forecast.model_id for forecast in projected] == ["image-model"]


def test_workflow_cursor_ignores_out_of_order_and_simultaneous_completions():
    intelligence = WorkloadIntelligence()
    observe_workflow(intelligence, "image", 100)
    observe_workflow(intelligence, "video", 90)
    observe_workflow(intelligence, "research", 100)

    sequence_state = intelligence.to_dict()["workflow_sequences"]

    assert [row["workload"] for row in sequence_state["states"]] == ["image"]
    assert sequence_state["transitions"] == []


def test_invalid_workflow_key_does_not_partially_record_demand():
    intelligence = WorkloadIntelligence()

    with pytest.raises(ValueError, match="workflow_key"):
        observe_workflow(intelligence, "image", 100, workflow_key="bad\nkey")

    assert intelligence.unbound_demand.to_dict()["models"] == {}
    assert intelligence.to_dict()["workflow_sequences"] == {
        "states": [],
        "transitions": [],
    }


def test_portfolio_device_time_evidence_configuration_round_trips_and_validates():
    intelligence = WorkloadIntelligence(
        portfolio_min_samples=4,
        portfolio_min_offered_concurrency=0.75,
    )

    restored = WorkloadIntelligence.from_dict(intelligence.to_dict())

    assert restored.portfolio_min_samples == 4
    assert restored.portfolio_min_offered_concurrency == 0.75
    legacy = intelligence.to_dict()
    legacy.pop("portfolio_min_offered_concurrency")
    assert WorkloadIntelligence.from_dict(
        legacy
    ).portfolio_min_offered_concurrency == 1.5
    for invalid in (True, 0, -1, float("inf"), float("nan")):
        with pytest.raises(ValueError, match="offered_concurrency"):
            WorkloadIntelligence(portfolio_min_offered_concurrency=invalid)


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


def test_one_fresh_result_does_not_revive_an_entire_stale_history():
    intelligence = WorkloadIntelligence()
    half_life = 7 * 24 * 60 * 60
    now = 8 * half_life
    for _ in range(20):
        intelligence.observe(
            RequestFeatures("chat/completions", "coder", "coding"),
            served_model="coder",
            error=True,
            quality=0.0,
            timestamp=0,
        )

    intelligence.observe(
        RequestFeatures("chat/completions", "coder", "coding"),
        served_model="coder",
        error=False,
        timestamp=now,
    )

    outcome = intelligence.outcomes[0]
    evidence = intelligence._outcome_evidence("coder", "coding", now=now)
    # Lifetime counters remain available for diagnostics, while decision evidence contains one
    # current result plus 1/256th of the old mass. The old implementation treated all 21 results as
    # current merely because the newest timestamp was fresh.
    assert outcome.requests == 21
    assert outcome.errors == 20
    assert evidence["effective_requests"] == pytest.approx(1 + 20 / 256)
    assert evidence["effective_errors"] == pytest.approx(20 / 256)
    assert evidence["confidence"] == pytest.approx((1 + 20 / 256) / 20)
    assert evidence["effective_quality_samples"] == pytest.approx(20 / 256)


def test_late_completion_is_discounted_without_rewinding_evidence_time():
    intelligence = WorkloadIntelligence()
    half_life = 7 * 24 * 60 * 60
    now = 8 * half_life
    intelligence.observe(
        RequestFeatures("chat/completions", "coder", "coding"),
        served_model="coder",
        latency_ms=100,
        timestamp=now,
    )

    intelligence.observe(
        RequestFeatures("chat/completions", "coder", "coding"),
        served_model="coder",
        latency_ms=10_000,
        error=True,
        timestamp=0,
    )

    outcome = intelligence.outcomes[0]
    evidence = intelligence._outcome_evidence("coder", "coding", now=now)
    assert outcome.service_updated_at == now
    assert evidence["effective_requests"] == pytest.approx(1 + 1 / 256)
    assert evidence["effective_errors"] == pytest.approx(1 / 256)
    assert outcome.latency_ms < 150


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
            "portfolio_preemption_safe": True,
        },
    }

    feasible_score = intelligence._portfolio_score_with_placement(
        feasible, "coding", hints, now=100
    )
    preempting_score = intelligence._portfolio_score_with_placement(
        preempting, "coding", hints, now=100
    )
    assert feasible_score > preempting_score
    intelligence.observe(
        RequestFeatures("chat/completions", "auto", "coding"),
        portfolio_unbound=True,
        timestamp=100,
    )
    projection = intelligence.projections(
        (feasible, preempting),
        now=100,
        placement_hints=hints,
    )[0]
    candidates = {
        row["model_id"]: row for row in projection["candidates"]
    }
    assert candidates["z-preempting"]["selectable"] is True
    assert projection["chosen_model"] == "a-feasible"


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
            "best_node_id": "ready-node",
            "startup_seconds": 0.0,
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
    assert projection["placement"]["best_node_id"] == "ready-node"
    candidate_rows = {row["model_id"]: row for row in projection["candidates"]}
    assert candidate_rows["preferred-huge"]["feasible"] is False


def test_portfolio_startup_time_breaks_an_otherwise_equal_model_tie():
    intelligence = WorkloadIntelligence(portfolio_min_samples=1)
    candidates = (
        profile("a-fast", 8_000, ("coding", 1.0)),
        profile("z-slow", 8_000, ("coding", 1.0)),
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
            "a-fast": {"feasible": True, "startup_seconds": 1.0},
            "z-slow": {"feasible": True, "startup_seconds": 120.0},
        },
    )

    assert forecasts[0].model_id == "a-fast"


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
