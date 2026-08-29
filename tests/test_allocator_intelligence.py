from __future__ import annotations

from shared.allocator.intelligence import (
    RequestFeatures,
    WorkloadIntelligence,
    classify_request,
)
from shared.allocator.models import DemandForecast, ModelProfile


def profile(model: str, memory: int, *scores: tuple[str, float]) -> ModelProfile:
    return ModelProfile(
        model_id=model,
        memory_mb=memory,
        min_replicas=0,
        max_replicas=2,
        load_seconds=0,
        warm_seconds=0,
        min_residency_seconds=0,
        workload_scores=scores,
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
    assert projected[0].correlated_requests_per_minute == projected[0].requests_per_minute
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
