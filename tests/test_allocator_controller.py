from __future__ import annotations

import json
import threading
import time
from dataclasses import replace

import pytest

from shared import jsonio
from shared.allocator.controller import AllocatorController
from shared.allocator.intelligence import RequestFeatures
from shared.allocator.models import (
    ActionKind,
    AllocatorMode,
    DemandForecast,
    ModelProfile,
    ModelResidency,
    MutationAction,
    NodeSnapshot,
    ResidencyState,
)
from shared.allocator.planner import PlannerPolicy
from shared.allocator.reconcile import MutationStatus, ReconcilePolicy


def profile(model_id: str = "qwen", **kwargs) -> ModelProfile:
    return ModelProfile(
        model_id,
        8_000,
        runtimes=("llama.cpp",),
        min_replicas=1,
        max_replicas=1,
        min_residency_seconds=0,
        **kwargs,
    )


def node(
    *,
    cached: bool = False,
    ready: bool = False,
    node_id: str = "n",
    heartbeat: float = 10,
    artifact_sha256: str = "",
) -> NodeSnapshot:
    residencies = (
        (
            ModelResidency(
                "qwen",
                8_000,
                ResidencyState.READY if ready else ResidencyState.CACHED,
                loaded_at=1 if ready else 0,
                artifact_sha256=artifact_sha256,
            ),
        )
        if ready or artifact_sha256
        else ()
    )
    return NodeSnapshot(
        node_id,
        16_000,
        runtimes=("llama.cpp",),
        backends=("metal",),
        cached_models=(("qwen",) if cached else ()),
        residencies=residencies,
        last_heartbeat=heartbeat,
    )


def test_controller_recommend_is_the_safe_default_and_queues_nothing():
    controller = AllocatorController()
    controller.put_profile(profile())
    result = controller.tick([node()], now=10)
    assert result.mode == AllocatorMode.RECOMMEND
    assert result.actions
    assert controller.commands_for("n", now=10) == ()


def test_controller_status_reports_last_successful_tick_duration(monkeypatch):
    monotonic_times = iter((100.0, 100.25))
    monkeypatch.setattr(
        "shared.allocator.controller.time.monotonic",
        lambda: next(monotonic_times),
    )
    controller = AllocatorController()
    controller.put_profile(profile())

    controller.tick([node()], now=10)

    assert controller.status([node()], now=10)["last_tick_duration_seconds"] == 0.25


def test_late_response_from_old_artifact_cannot_update_new_revision_evidence():
    controller = AllocatorController()
    revision_a = "a" * 64
    revision_b = "b" * 64
    controller.put_profile(profile(artifact_sha256=revision_a))
    controller.observe_lifecycle(
        RequestFeatures("chat/completions", "qwen", "coding"),
        served_model="qwen",
        served_artifact_sha256=revision_a,
        service_seconds=1,
        quality=1.0,
        timestamp=10,
    )

    controller.put_profile(profile(artifact_sha256=revision_b))
    controller.observe_lifecycle(
        RequestFeatures("chat/completions", "qwen", "coding"),
        served_model="qwen",
        served_artifact_sha256=revision_a,
        service_seconds=1,
        quality=1.0,
        timestamp=11,
    )

    assert {item.artifact_sha256 for item in controller.intelligence.outcomes} == {
        revision_a
    }
    evidence = controller.intelligence._outcome_evidence(
        "qwen", "coding", artifact_sha256=revision_b, now=11
    )
    assert evidence["quality_confidence"] == 0.0


def test_router_fallback_preserves_workload_demand_without_pinning_router_choice():
    controller = AllocatorController()
    controller.put_profile(profile(model_id="general"))
    controller.put_profile(
        ModelProfile(
            "coder",
            8_000,
            runtimes=("llama.cpp",),
            min_replicas=0,
            max_replicas=1,
            min_residency_seconds=0,
            workload_scores=(("coding", 1.0),),
        )
    )

    controller.observe_lifecycle(
        RequestFeatures("chat/completions", "auto", "coding"),
        served_model="general",
        service_seconds=2,
        timestamp=10,
    )

    assert controller.demand.to_dict()["models"] == {}
    assert set(controller.intelligence.unbound_demand.to_dict()["models"]) == {
        "coding"
    }
    assert controller.intelligence.outcomes[0].model_id == "general"


def test_explicit_named_request_does_not_become_portfolio_demand():
    controller = AllocatorController()
    controller.put_profile(profile(model_id="general"))

    controller.observe_lifecycle(
        RequestFeatures("chat/completions", "general", "coding"),
        served_model="general",
        service_seconds=2,
        timestamp=10,
    )

    assert set(controller.demand.to_dict()["models"]) == {"general"}
    assert controller.intelligence.unbound_demand.to_dict()["models"] == {}


def test_evaluation_rejects_explicit_artifact_mismatch_and_uses_current_revision():
    controller = AllocatorController()
    revision_a = "a" * 64
    revision_b = "b" * 64
    controller.put_profile(profile(artifact_sha256=revision_b))

    with pytest.raises(ValueError, match="does not match"):
        controller.observe_evaluation(
            "qwen",
            "coding",
            artifact_sha256=revision_a,
            quality=1.0,
        )
    outcome = controller.observe_evaluation("qwen", "coding", quality=1.0)

    assert outcome.artifact_sha256 == revision_b


def test_controller_portfolio_uses_a_fleet_feasible_fallback():
    controller = AllocatorController(mode=AllocatorMode.AUTOMATIC)
    for candidate in (
        ModelProfile(
            "preferred-huge",
            32_000,
            runtimes=("llama.cpp",),
            backends=("metal",),
            min_replicas=0,
            max_replicas=1,
            min_residency_seconds=0,
            workload_scores=(("coding", 1.0),),
        ),
        ModelProfile(
            "feasible-small",
            8_000,
            runtimes=("llama.cpp",),
            backends=("metal",),
            min_replicas=0,
            max_replicas=1,
            min_residency_seconds=0,
            workload_scores=(("coding", 0.7),),
        ),
    ):
        controller.put_profile(candidate)
    for timestamp in (98, 99, 100):
        controller.observe_lifecycle(
            RequestFeatures("chat/completions", "auto", "coding"),
            service_seconds=1,
            timestamp=timestamp,
        )
    machine = NodeSnapshot(
        "only-node",
        16_000,
        runtimes=("llama.cpp",),
        backends=("metal",),
        last_heartbeat=100,
    )

    controller.tick((machine,), now=100)
    status = controller.status((machine,), now=100)

    assert controller.last_plan is not None
    assert [
        (item.model_id, item.node_id) for item in controller.last_plan.assignments
    ] == [("feasible-small", "only-node")]
    assert status["portfolio_projections"][0]["chosen_model"] == "feasible-small"
    hints = {row["model_id"]: row for row in status["portfolio_placement_hints"]}
    assert hints["preferred-huge"]["feasible"] is False
    assert hints["feasible-small"]["best_node_id"] == "only-node"


def test_controller_portfolio_requires_meaningful_gain_to_replace_resident_model():
    controller = AllocatorController()
    for model_id, score in (("incumbent", 0.8), ("challenger", 0.805)):
        controller.put_profile(
            ModelProfile(
                model_id,
                8_000,
                runtimes=("llama.cpp",),
                backends=("metal",),
                min_replicas=0,
                max_replicas=1,
                min_residency_seconds=0,
                workload_scores=(("coding", score),),
            )
        )
    for timestamp in (98, 99, 100):
        controller.observe_lifecycle(
            RequestFeatures("chat/completions", "auto", "coding"),
            service_seconds=1,
            timestamp=timestamp,
        )
    machines = (
        NodeSnapshot(
            "resident-node",
            16_000,
            runtimes=("llama.cpp",),
            backends=("metal",),
            max_models=1,
            residencies=(
                ModelResidency(
                    "incumbent",
                    8_000,
                    ResidencyState.READY,
                    loaded_at=1,
                ),
            ),
            last_heartbeat=100,
        ),
        NodeSnapshot(
            "empty-node",
            16_000,
            runtimes=("llama.cpp",),
            backends=("metal",),
            max_models=1,
            last_heartbeat=100,
        ),
    )

    stable = controller.status(machines, now=100)["portfolio_projections"][0]
    controller.put_profile(
        ModelProfile(
            "challenger",
            8_000,
            runtimes=("llama.cpp",),
            backends=("metal",),
            min_replicas=0,
            max_replicas=1,
            min_residency_seconds=0,
            workload_scores=(("coding", 0.9),),
        )
    )
    switched = controller.status(machines, now=101)["portfolio_projections"][0]

    assert stable["chosen_model"] == "incumbent"
    assert switched["chosen_model"] == "challenger"


def test_joint_portfolio_chooses_one_shared_model_when_independent_specialists_do_not_fit():
    controller = AllocatorController()
    for candidate in (
        ModelProfile(
            "coding-specialist",
            8_000,
            runtimes=("llama.cpp",),
            backends=("metal",),
            min_replicas=0,
            max_replicas=1,
            min_residency_seconds=0,
            workload_scores=(("coding", 1.0),),
        ),
        ModelProfile(
            "research-specialist",
            8_000,
            runtimes=("llama.cpp",),
            backends=("metal",),
            min_replicas=0,
            max_replicas=1,
            min_residency_seconds=0,
            workload_scores=(("research", 1.0),),
        ),
        ModelProfile(
            "shared-generalist",
            8_000,
            runtimes=("llama.cpp",),
            backends=("metal",),
            min_replicas=0,
            max_replicas=1,
            min_residency_seconds=0,
            workload_scores=(("coding", 0.9), ("research", 0.9)),
        ),
    ):
        controller.put_profile(candidate)
    for workload in ("coding", "research"):
        for timestamp in (98, 99, 100):
            controller.observe_lifecycle(
                RequestFeatures("chat/completions", "auto", workload),
                service_seconds=1,
                timestamp=timestamp,
            )
    machine = NodeSnapshot(
        "one-slot",
        16_000,
        runtimes=("llama.cpp",),
        backends=("metal",),
        max_models=1,
        last_heartbeat=100,
    )

    controller.tick((machine,), now=100)
    status = controller.status((machine,), now=100)

    assert status["portfolio_selection"] == {
        "coding": "shared-generalist",
        "research": "shared-generalist",
    }
    assert controller.last_plan is not None
    assert {item.model_id for item in controller.last_plan.assignments} == {
        "shared-generalist"
    }
    assert status["portfolio_policy"]["joint"] is True


def test_joint_portfolio_keeps_fifth_ranked_shared_model_inside_bounded_search():
    controller = AllocatorController()
    for workload in ("coding", "research"):
        for index, score in enumerate((1.0, 0.99, 0.98, 0.97)):
            controller.put_profile(
                ModelProfile(
                    f"{workload}-specialist-{index}",
                    8_000,
                    runtimes=("llama.cpp",),
                    backends=("metal",),
                    min_replicas=0,
                    max_replicas=1,
                    min_residency_seconds=0,
                    workload_scores=((workload, score),),
                )
            )
    controller.put_profile(
        ModelProfile(
            "fifth-ranked-generalist",
            8_000,
            runtimes=("llama.cpp",),
            backends=("metal",),
            min_replicas=0,
            max_replicas=1,
            min_residency_seconds=0,
            workload_scores=(("coding", 0.8), ("research", 0.8)),
        )
    )
    for workload in ("coding", "research"):
        for timestamp in (98, 99, 100):
            controller.observe_lifecycle(
                RequestFeatures("chat/completions", "auto", workload),
                service_seconds=1,
                timestamp=timestamp,
            )
    machine = NodeSnapshot(
        "one-slot",
        16_000,
        runtimes=("llama.cpp",),
        backends=("metal",),
        max_models=1,
        last_heartbeat=100,
    )

    selection = controller.status((machine,), now=100)["portfolio_selection"]

    assert selection == {
        "coding": "fifth-ranked-generalist",
        "research": "fifth-ranked-generalist",
    }


def test_joint_portfolio_allows_only_one_uncertainty_driven_model_experiment():
    controller = AllocatorController()
    candidates = []
    for workload in ("coding", "research"):
        for suffix in ("incumbent", "cold"):
            candidate = ModelProfile(
                f"{workload}-{suffix}",
                8_000,
                runtimes=("llama.cpp",),
                backends=("metal",),
                min_replicas=0,
                max_replicas=1,
                min_residency_seconds=0,
                workload_scores=((workload, 1.0),),
            )
            candidates.append(candidate)
            controller.put_profile(candidate)
        controller.observe_evaluation(
            f"{workload}-incumbent",
            workload,
            quality=0.5,
            timestamp=99,
        )
        for timestamp in (98, 99, 100):
            controller.observe_lifecycle(
                RequestFeatures("chat/completions", "auto", workload),
                service_seconds=1,
                timestamp=timestamp,
            )
    machines = tuple(
        NodeSnapshot(
            f"node-{index}",
            16_000,
            runtimes=("llama.cpp",),
            backends=("metal",),
            max_models=1,
            last_heartbeat=100,
        )
        for index in range(2)
    )

    controller.tick(machines, now=100)
    selection = controller.status(machines, now=100)["portfolio_selection"]

    assert sum(model_id.endswith("-cold") for model_id in selection.values()) == 1
    policy = controller.status(machines, now=100)["portfolio_policy"]
    assert len(policy["exploration_models"]) == 1
    assert policy["max_exploration_models"] == 1


def test_joint_portfolio_search_has_a_hard_planner_evaluation_bound(monkeypatch):
    controller = AllocatorController()
    workloads = (
        "general",
        "coding",
        "research",
        "marketing",
        "sales",
        "design",
        "image",
        "video",
        "embedding",
    )
    for index in range(6):
        controller.put_profile(
            ModelProfile(
                f"candidate-{index}",
                1_000,
                runtimes=("llama.cpp",),
                backends=("metal",),
                min_replicas=0,
                max_replicas=1,
                min_residency_seconds=0,
                workload_scores=tuple((workload, 1.0) for workload in workloads),
            )
        )
    for workload in workloads:
        for timestamp in (98, 99, 100):
            controller.observe_lifecycle(
                RequestFeatures("chat/completions", "auto", workload),
                service_seconds=1,
                timestamp=timestamp,
            )
    machines = tuple(
        NodeSnapshot(
            f"node-{index}",
            16_000,
            runtimes=("llama.cpp",),
            backends=("metal",),
            max_models=1,
            last_heartbeat=100,
        )
        for index in range(6)
    )
    hints = controller.planner.portfolio_placement_hints(
        machines,
        controller.profiles,
        now=100,
    )
    original_plan = controller.planner.plan
    calls = 0

    def counted_plan(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_plan(*args, **kwargs)

    monkeypatch.setattr(controller.planner, "plan", counted_plan)

    _, selection = controller._forecast_bundle(
        100,
        placement_hints=hints,
        nodes=machines,
    )

    assert selection is not None
    assert calls <= 65  # one baseline plus at most 64 distinct portfolio evaluations


def test_joint_portfolio_planning_does_not_block_request_telemetry(monkeypatch):
    controller = AllocatorController()
    for model_id, workload in (("coder", "coding"), ("researcher", "research")):
        controller.put_profile(
            ModelProfile(
                model_id,
                1_000,
                runtimes=("llama.cpp",),
                backends=("metal",),
                min_replicas=0,
                max_replicas=1,
                min_residency_seconds=0,
                workload_scores=((workload, 1.0),),
            )
        )
        for timestamp in (98, 99, 100):
            controller.observe_lifecycle(
                RequestFeatures("chat/completions", "auto", workload),
                service_seconds=1,
                timestamp=timestamp,
            )
    machines = (
        NodeSnapshot(
            "node",
            16_000,
            runtimes=("llama.cpp",),
            backends=("metal",),
            max_models=1,
            last_heartbeat=100,
        ),
    )
    hints = controller.planner.portfolio_placement_hints(
        machines, controller.profiles, now=100
    )
    planner_entered = threading.Event()
    release_planner = threading.Event()
    planning_finished = threading.Event()
    telemetry_finished = threading.Event()
    failures: list[BaseException] = []
    original_plan = controller.planner.plan

    def blocking_plan(*args, **kwargs):
        planner_entered.set()
        if not release_planner.wait(2):
            raise TimeoutError("test did not release planner")
        return original_plan(*args, **kwargs)

    def plan_portfolio():
        try:
            controller._forecast_bundle(100, placement_hints=hints, nodes=machines)
        except BaseException as exc:  # pragma: no cover - asserted through failures
            failures.append(exc)
        finally:
            planning_finished.set()

    def record_request():
        controller.observe_lifecycle(
            RequestFeatures("chat/completions", "auto", "coding"),
            service_seconds=1,
            timestamp=101,
        )
        telemetry_finished.set()

    monkeypatch.setattr(controller.planner, "plan", blocking_plan)
    planning_thread = threading.Thread(target=plan_portfolio)
    planning_thread.start()
    assert planner_entered.wait(1)
    telemetry_thread = threading.Thread(target=record_request)
    telemetry_thread.start()
    try:
        assert telemetry_finished.wait(0.1)
    finally:
        release_planner.set()
        planning_thread.join(2)
        telemetry_thread.join(2)

    assert planning_finished.is_set()
    assert failures == []


def test_controller_versioning_preserves_plan_model_urgency():
    controller = AllocatorController()
    controller.put_profile(profile())

    controller.tick([node()], now=10)

    assert controller.last_plan is not None
    assert controller.last_plan.urgency_for("qwen") == 3
    assert controller.last_plan.to_dict()["model_urgencies"] == {"qwen": 3}


def test_tick_duration_clock_failure_cannot_fail_committed_reconciliation(
    monkeypatch,
):
    calls = iter((100.0, RuntimeError("clock unavailable")))

    def monotonic():
        value = next(calls)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr("shared.allocator.controller.time.monotonic", monotonic)
    controller = AllocatorController()
    controller.put_profile(profile())

    result = controller.tick([node()], now=10)

    assert result.actions
    assert controller.last_plan is not None
    assert controller.status([node()], now=10)["last_tick_duration_seconds"] == 0


def test_controller_automatic_queues_repeats_and_acknowledges_action():
    controller = AllocatorController(
        mode=AllocatorMode.AUTOMATIC,
        reconcile_policy=ReconcilePolicy(max_concurrent_mutations=1),
    )
    controller.put_profile(profile())
    controller.tick([node(cached=True)], now=10)
    command = controller.commands_for("n", now=10)[0]
    assert command.executable
    assert controller.commands_for("n", now=11) == (command,)
    running = controller.acknowledge(
        "n", command.action_id, MutationStatus.RUNNING, now=11
    )
    assert running.status == MutationStatus.RUNNING
    done = controller.acknowledge(
        "n", command.action_id, MutationStatus.SUCCEEDED, now=12
    )
    assert done.status == MutationStatus.SUCCEEDED
    assert controller.commands_for("n", now=12) == ()
    assert (
        controller.acknowledge("n", command.action_id, MutationStatus.SUCCEEDED, now=13)
        == done
    )
    assert (
        controller.acknowledge("n", command.action_id, MutationStatus.FAILED, now=13)
        == done
    )


def test_higher_priority_service_replaces_undelivered_pending_warm():
    controller = AllocatorController(
        mode=AllocatorMode.AUTOMATIC,
        reconcile_policy=ReconcilePolicy(max_concurrent_mutations=1),
    )
    controller.put_profile(profile("low", pinned_nodes=("a-low",), priority=1))

    def cached_node(node_id: str, model_id: str, heartbeat: float) -> NodeSnapshot:
        return NodeSnapshot(
            node_id,
            16_000,
            runtimes=("llama.cpp",),
            backends=("metal",),
            cached_models=(model_id,),
            last_heartbeat=heartbeat,
        )

    first = controller.tick([cached_node("a-low", "low", 10)], now=10)
    old_action = first.executable_actions[0]
    controller.put_profile(profile("high", pinned_nodes=("z-high",), priority=1_000))

    second = controller.tick(
        [
            cached_node("a-low", "low", 11),
            cached_node("z-high", "high", 11),
        ],
        now=11,
    )

    assert [(action.kind, action.model_id) for action in second.executable_actions] == [
        (ActionKind.WARM, "high")
    ]
    assert any(
        record.action_id == old_action.action_id
        and record.status == MutationStatus.CANCELLED
        and "higher-priority" in record.message
        for record in controller.history
    )


def test_command_delivery_preserves_reconciler_priority_for_same_tick(
    monkeypatch,
    tmp_path,
):
    state_path = tmp_path / "controller.json"
    controller = AllocatorController(
        mode=AllocatorMode.AUTOMATIC,
        state_path=state_path,
        reconcile_policy=ReconcilePolicy(
            max_concurrent_mutations=2,
            max_mutations_per_node=2,
        ),
    )
    controller.put_profile(profile("low", pinned_nodes=("shared",), priority=1))
    controller.put_profile(profile("high", pinned_nodes=("shared",), priority=1_000))
    machine = NodeSnapshot(
        "shared",
        32_000,
        runtimes=("llama.cpp",),
        backends=("metal",),
        cached_models=("high", "low"),
        last_heartbeat=10,
    )
    original_stable_id = MutationAction.stable_id

    def inverted_stable_id(kind, node_id, model_id, transition=""):
        if "controller-attempt-v1" in transition:
            return "a-low" if model_id == "low" else "z-high"
        return original_stable_id(kind, node_id, model_id, transition)

    monkeypatch.setattr(MutationAction, "stable_id", inverted_stable_id)

    result = controller.tick((machine,), now=10)
    delivered = controller.commands_for("shared", now=10)

    assert [action.model_id for action in result.executable_actions] == [
        "high",
        "low",
    ]
    assert [action.action_id for action in delivered] == ["z-high", "a-low"]
    restored = AllocatorController(state_path=state_path)
    assert [action.action_id for action in restored.commands_for("shared", now=10)] == [
        "z-high",
        "a-low",
    ]


def test_command_delivery_prioritizes_new_urgent_work_over_older_queue_entry():
    controller = AllocatorController(
        mode=AllocatorMode.AUTOMATIC,
        reconcile_policy=ReconcilePolicy(
            max_concurrent_mutations=2,
            max_mutations_per_node=2,
        ),
    )
    controller.put_profile(profile("low", pinned_nodes=("shared",), priority=1))
    machine = NodeSnapshot(
        "shared",
        32_000,
        runtimes=("llama.cpp",),
        backends=("metal",),
        cached_models=("high", "low"),
        last_heartbeat=10,
    )
    first = controller.tick((machine,), now=10)
    assert [action.model_id for action in first.executable_actions] == ["low"]

    controller.put_profile(profile("high", pinned_nodes=("shared",), priority=1_000))
    second = controller.tick(
        (replace(machine, last_heartbeat=11),),
        now=11,
    )
    assert [action.model_id for action in second.executable_actions] == ["high"]

    assert [
        action.model_id for action in controller.commands_for("shared", now=11)
    ] == ["high", "low"]


def test_higher_priority_service_does_not_cancel_delivered_pending_warm():
    controller = AllocatorController(
        mode=AllocatorMode.AUTOMATIC,
        reconcile_policy=ReconcilePolicy(max_concurrent_mutations=1),
    )
    controller.put_profile(profile("low", pinned_nodes=("a-low",), priority=1))

    def cached_node(node_id: str, model_id: str, heartbeat: float) -> NodeSnapshot:
        return NodeSnapshot(
            node_id,
            16_000,
            runtimes=("llama.cpp",),
            backends=("metal",),
            cached_models=(model_id,),
            last_heartbeat=heartbeat,
        )

    first = controller.tick([cached_node("a-low", "low", 10)], now=10)
    old_action = first.executable_actions[0]
    assert controller.commands_for("a-low", now=10) == (old_action,)
    controller.put_profile(profile("high", pinned_nodes=("z-high",), priority=1_000))

    second = controller.tick(
        [
            cached_node("a-low", "low", 11),
            cached_node("z-high", "high", 11),
        ],
        now=11,
    )

    assert second.executable_actions == ()
    assert controller.commands_for("a-low", now=11) == (old_action,)
    assert not any(
        record.action_id == old_action.action_id
        and record.status == MutationStatus.CANCELLED
        for record in controller.history
    )


def test_equal_service_class_does_not_churn_undelivered_pending_warm():
    controller = AllocatorController(
        mode=AllocatorMode.AUTOMATIC,
        reconcile_policy=ReconcilePolicy(max_concurrent_mutations=1),
    )
    controller.put_profile(profile("alpha", pinned_nodes=("a-alpha",), priority=100))

    def cached_node(node_id: str, model_id: str, heartbeat: float) -> NodeSnapshot:
        return NodeSnapshot(
            node_id,
            16_000,
            runtimes=("llama.cpp",),
            backends=("metal",),
            cached_models=(model_id,),
            last_heartbeat=heartbeat,
        )

    first = controller.tick([cached_node("a-alpha", "alpha", 10)], now=10)
    old_action = first.executable_actions[0]
    controller.put_profile(profile("beta", pinned_nodes=("z-beta",), priority=100))

    second = controller.tick(
        [
            cached_node("a-alpha", "alpha", 11),
            cached_node("z-beta", "beta", 11),
        ],
        now=11,
    )

    assert second.executable_actions == ()
    assert controller.commands_for("a-alpha", now=11) == (old_action,)
    assert not any(
        record.action_id == old_action.action_id
        and record.status == MutationStatus.CANCELLED
        for record in controller.history
    )


def test_direct_demand_replaces_undelivered_speculative_prewarm(monkeypatch):
    controller = AllocatorController(
        mode=AllocatorMode.AUTOMATIC,
        reconcile_policy=ReconcilePolicy(max_concurrent_mutations=1),
    )
    profiles = (
        ModelProfile(
            "speculative",
            8_000,
            runtimes=("llama.cpp",),
            required_tags=("speculative",),
            min_replicas=0,
            max_replicas=1,
        ),
        ModelProfile(
            "direct",
            8_000,
            runtimes=("llama.cpp",),
            required_tags=("direct",),
            min_replicas=0,
            max_replicas=1,
        ),
    )
    for item in profiles:
        controller.put_profile(item)
    machines = (
        NodeSnapshot(
            "a-speculative",
            16_000,
            runtimes=("llama.cpp",),
            backends=("metal",),
            tags=("speculative",),
            cached_models=("speculative",),
            last_heartbeat=10,
        ),
        NodeSnapshot(
            "z-direct",
            16_000,
            runtimes=("llama.cpp",),
            backends=("metal",),
            tags=("direct",),
            cached_models=("direct",),
            last_heartbeat=10,
        ),
    )
    speculative = DemandForecast(
        "speculative",
        requests_per_minute=60,
        correlated_requests_per_minute=60,
        correlation_confidence=1,
        correlation_sources=("source",),
        updated_at=10,
    )
    current_forecasts = [(speculative,)]
    monkeypatch.setattr(
        AllocatorController,
        "_forecasts",
        lambda _controller, _now, **_kwargs: current_forecasts[0],
    )

    first = controller.tick(machines, now=10)
    speculative_action = first.executable_actions[0]
    assert speculative_action.model_id == "speculative"

    current_forecasts[0] = (
        speculative,
        DemandForecast(
            "direct",
            requests_per_minute=60,
            observed_requests_per_minute=60,
            offered_concurrency=1,
            updated_at=11,
        ),
    )
    second = controller.tick(
        tuple(replace(machine, last_heartbeat=11) for machine in machines),
        now=11,
    )

    assert controller.last_plan is not None
    assert controller.last_plan.urgency_for("speculative") == 1
    assert controller.last_plan.urgency_for("direct") == 2
    assert [(action.kind, action.model_id) for action in second.executable_actions] == [
        (ActionKind.WARM, "direct")
    ]
    assert any(
        record.action_id == speculative_action.action_id
        and record.status == MutationStatus.CANCELLED
        for record in controller.history
    )


def test_direct_demand_is_delivered_before_older_speculative_prewarm(monkeypatch):
    controller = AllocatorController(
        mode=AllocatorMode.AUTOMATIC,
        reconcile_policy=ReconcilePolicy(
            max_concurrent_mutations=2,
            max_mutations_per_node=2,
        ),
    )
    profiles = (
        ModelProfile(
            "speculative",
            8_000,
            runtimes=("llama.cpp",),
            min_replicas=0,
            max_replicas=1,
        ),
        ModelProfile(
            "direct",
            8_000,
            runtimes=("llama.cpp",),
            min_replicas=0,
            max_replicas=1,
        ),
    )
    for item in profiles:
        controller.put_profile(item)
    machine = NodeSnapshot(
        "shared",
        32_000,
        runtimes=("llama.cpp",),
        backends=("metal",),
        cached_models=("direct", "speculative"),
        last_heartbeat=10,
    )
    speculative = DemandForecast(
        "speculative",
        requests_per_minute=60,
        correlated_requests_per_minute=60,
        correlation_confidence=1,
        correlation_sources=("source",),
        updated_at=10,
    )
    current_forecasts = [(speculative,)]
    monkeypatch.setattr(
        AllocatorController,
        "_forecasts",
        lambda _controller, _now, **_kwargs: current_forecasts[0],
    )
    first = controller.tick((machine,), now=10)
    assert [action.model_id for action in first.executable_actions] == ["speculative"]

    current_forecasts[0] = (
        speculative,
        DemandForecast(
            "direct",
            requests_per_minute=60,
            observed_requests_per_minute=60,
            offered_concurrency=1,
            updated_at=11,
        ),
    )
    second = controller.tick(
        (replace(machine, last_heartbeat=11),),
        now=11,
    )
    assert [action.model_id for action in second.executable_actions] == ["direct"]

    delivered = controller.commands_for("shared", now=11)

    assert [action.model_id for action in delivered] == ["direct", "speculative"]
    assert not any(
        record.model_id == "speculative" and record.status == MutationStatus.CANCELLED
        for record in controller.history
    )


def test_controller_starts_equal_services_one_replica_round_at_a_time():
    controller = AllocatorController(
        mode=AllocatorMode.AUTOMATIC,
        reconcile_policy=ReconcilePolicy(max_concurrent_mutations=2),
    )
    pairs = (
        ("a-alpha-0", "alpha"),
        ("b-alpha-1", "alpha"),
        ("y-beta-0", "beta"),
        ("z-beta-1", "beta"),
    )
    machines = tuple(
        NodeSnapshot(
            node_id,
            16_000,
            runtimes=("llama.cpp",),
            backends=("metal",),
            cached_models=(model_id,),
            last_heartbeat=10,
        )
        for node_id, model_id in pairs
    )
    for model_id, pinned_nodes in (
        ("alpha", ("a-alpha-0", "b-alpha-1")),
        ("beta", ("y-beta-0", "z-beta-1")),
    ):
        controller.put_profile(
            ModelProfile(
                model_id,
                8_000,
                runtimes=("llama.cpp",),
                pinned_nodes=pinned_nodes,
                min_replicas=2,
                max_replicas=2,
                min_residency_seconds=0,
            )
        )

    first = controller.tick(machines, now=10)
    assert [action.model_id for action in first.executable_actions] == [
        "alpha",
        "beta",
    ]
    started: dict[str, str] = {}
    for machine in machines:
        commands = controller.commands_for(machine.node_id, now=10)
        if not commands:
            continue
        command = commands[0]
        started[machine.node_id] = command.model_id
        controller.acknowledge(
            machine.node_id,
            command.action_id,
            MutationStatus.SUCCEEDED,
            now=10.5,
        )

    observed = tuple(
        replace(
            machine,
            residencies=(
                ModelResidency(
                    started[machine.node_id],
                    8_000,
                    ResidencyState.READY,
                    loaded_at=10.5,
                ),
            ),
            last_heartbeat=11,
        )
        if machine.node_id in started
        else replace(machine, last_heartbeat=11)
        for machine in machines
    )
    second = controller.tick(observed, now=11)

    assert [action.model_id for action in second.executable_actions] == [
        "alpha",
        "beta",
    ]
    assert {action.node_id for action in first.executable_actions}.isdisjoint(
        action.node_id for action in second.executable_actions
    )


def test_undelivered_reprioritization_indexes_large_command_queue_once():
    count = 64
    controller = AllocatorController(
        mode=AllocatorMode.AUTOMATIC,
        reconcile_policy=ReconcilePolicy(max_concurrent_mutations=count),
        max_history=10,
    )
    profiles = tuple(
        ModelProfile(
            f"{service}-{index}",
            8_000,
            runtimes=("llama.cpp",),
            pinned_nodes=(f"{service}-node-{index}",),
            priority=1 if service == "low" else 1_000,
            min_replicas=1,
            max_replicas=1,
        )
        for service in ("low", "high")
        for index in range(count)
    )
    machines = tuple(
        NodeSnapshot(
            f"{service}-node-{index}",
            16_000,
            runtimes=("llama.cpp",),
            backends=("metal",),
            cached_models=(f"{service}-{index}",),
            last_heartbeat=10,
        )
        for service in ("low", "high")
        for index in range(count)
    )
    plan = controller.planner.plan(machines, profiles, now=10)

    class CountingCommands(dict):
        values_calls = 0

        def values(self):
            self.values_calls += 1
            return super().values()

    commands = CountingCommands(
        {
            f"action-{index}": MutationAction(
                f"action-{index}",
                ActionKind.WARM,
                f"low-node-{index}",
                f"low-{index}",
                8_000,
                "queued low-priority warm",
                plan.generation,
                10,
                executable=True,
            )
            for index in range(count)
        }
    )
    controller._commands = commands

    controller._reprioritize_undelivered_constructive(
        plan,
        machines,
        profiles,
        now=11,
    )

    assert controller._commands == {}
    assert commands.values_calls <= 6
    assert len(controller.history) == 10


def test_reprioritization_cancels_leaf_before_dependency_root(monkeypatch):
    controller = AllocatorController(
        mode=AllocatorMode.AUTOMATIC,
        reconcile_policy=ReconcilePolicy(
            max_concurrent_mutations=2,
            max_mutations_per_node=2,
        ),
    )
    controller.put_profile(profile("low", pinned_nodes=("shared",), priority=1))
    machine = NodeSnapshot(
        "shared",
        32_000,
        runtimes=("llama.cpp",),
        backends=("metal",),
        cached_models=("high",),
        last_heartbeat=10,
    )
    original_stable_id = MutationAction.stable_id

    def root_first_stable_id(kind, node_id, model_id, transition=""):
        if "controller-attempt-v1" in transition and model_id == "low":
            return "a-load" if kind == ActionKind.LOAD else "z-warm"
        return original_stable_id(kind, node_id, model_id, transition)

    monkeypatch.setattr(MutationAction, "stable_id", root_first_stable_id)
    first = controller.tick((machine,), now=10)
    assert [action.kind for action in first.executable_actions] == [
        ActionKind.LOAD,
        ActionKind.WARM,
    ]

    controller.put_profile(profile("high", pinned_nodes=("shared",), priority=1_000))
    second = controller.tick(
        (replace(machine, last_heartbeat=11),),
        now=11,
    )

    assert [action.model_id for action in second.executable_actions] == ["high"]
    assert {
        (action.kind, action.model_id) for action in controller._commands.values()
    } == {
        (ActionKind.LOAD, "low"),
        (ActionKind.WARM, "high"),
    }
    cancelled = [
        record
        for record in controller.history
        if record.status == MutationStatus.CANCELLED
    ]
    assert [(record.kind, record.model_id) for record in cancelled] == [
        (ActionKind.WARM, "low")
    ]


def test_controller_learns_and_persists_bounded_warm_duration(tmp_path):
    state_path = tmp_path / "controller.json"
    controller = AllocatorController(
        mode=AllocatorMode.AUTOMATIC,
        state_path=state_path,
    )
    controller.put_profile(profile())
    controller.tick([node(cached=True)], now=10)
    command = controller.commands_for("n", now=10)[0]

    record = controller.acknowledge(
        "n",
        command.action_id,
        MutationStatus.SUCCEEDED,
        duration_seconds=20,
        now=11,
    )

    assert record.duration_seconds == 20
    learned = controller.status(now=11)["learned_warm_seconds"]
    assert learned == [
        {"node_id": "n", "model_id": "qwen", "seconds": 8.75, "samples": 1}
    ]
    restored = AllocatorController(state_path=state_path)
    assert restored.status(now=11)["learned_warm_seconds"] == learned
    assert restored.status(now=31 * 24 * 60 * 60)["learned_warm_seconds"] == []


def test_learned_warm_time_prioritizes_faster_equal_priority_start():
    controller = AllocatorController(
        mode=AllocatorMode.AUTOMATIC,
        reconcile_policy=ReconcilePolicy(max_concurrent_mutations=1),
    )
    slow = profile(
        "slow",
        pinned_nodes=("a-slow",),
        warm_seconds=1,
    )
    controller.put_profile(slow)

    def cached_node(node_id: str, model_id: str, heartbeat: float) -> NodeSnapshot:
        return NodeSnapshot(
            node_id,
            16_000,
            runtimes=("llama.cpp",),
            backends=("metal",),
            cached_models=(model_id,),
            residencies=(ModelResidency(model_id, 8_000, ResidencyState.CACHED),),
            last_heartbeat=heartbeat,
        )

    controller.tick([cached_node("a-slow", "slow", 10)], now=10)
    learned_command = controller.commands_for("a-slow", now=10)[0]
    assert learned_command.kind == ActionKind.WARM
    controller.acknowledge(
        "a-slow",
        learned_command.action_id,
        MutationStatus.SUCCEEDED,
        duration_seconds=40,
        now=11,
    )

    controller.put_profile(
        profile(
            "fast",
            pinned_nodes=("z-fast",),
            warm_seconds=5,
        )
    )
    result = controller.tick(
        [
            cached_node("a-slow", "slow", 12),
            cached_node("z-fast", "fast", 12),
        ],
        now=12,
    )

    assert [(action.kind, action.model_id) for action in result.executable_actions] == [
        (ActionKind.WARM, "fast")
    ]


def test_controller_forecasts_against_fastest_learned_cached_path(monkeypatch):
    controller = AllocatorController(
        mode=AllocatorMode.AUTOMATIC,
        reconcile_policy=ReconcilePolicy(max_concurrent_mutations=2),
    )
    initial_profile = ModelProfile(
        "qwen",
        8_000,
        runtimes=("llama.cpp",),
        min_replicas=2,
        max_replicas=2,
        target_utilization=1,
        load_seconds=60,
        warm_seconds=1,
        min_residency_seconds=0,
    )
    controller.put_profile(initial_profile)
    machines = tuple(
        NodeSnapshot(
            node_id,
            16_000,
            runtimes=("llama.cpp",),
            backends=("metal",),
            cached_models=("qwen",),
            last_heartbeat=10,
        )
        for node_id in ("a-fast", "z-slow")
    )
    controller.tick(machines, now=10)
    commands = {
        machine.node_id: controller.commands_for(machine.node_id, now=10)[0]
        for machine in machines
    }
    for node_id, duration in (("a-fast", 1), ("z-slow", 100)):
        controller.acknowledge(
            node_id,
            commands[node_id].action_id,
            MutationStatus.SUCCEEDED,
            duration_seconds=duration,
            now=11,
        )

    controller.put_profile(replace(initial_profile, min_replicas=0, max_replicas=10))
    monkeypatch.setattr(
        AllocatorController,
        "_forecasts",
        lambda _controller, _now, **_kwargs: (
            DemandForecast(
                "qwen",
                requests_per_minute=60,
                observed_requests_per_minute=60,
                offered_concurrency=1.5,
                trend_per_minute=60,
                confidence=1,
                updated_at=12,
            ),
        ),
    )

    controller.tick(
        tuple(replace(machine, last_heartbeat=12) for machine in machines),
        now=12,
    )

    assert controller.last_plan is not None
    assert controller.last_plan.target_for("qwen") == 2
    learned = {
        row["node_id"]: row["seconds"]
        for row in controller.status(now=12)["learned_warm_seconds"]
    }
    assert learned == {"a-fast": 1, "z-slow": 25.75}


def test_controller_does_not_reuse_warm_timing_across_artifact_revisions():
    controller = AllocatorController(mode=AllocatorMode.AUTOMATIC)
    controller.put_profile(profile(artifact_sha256="a" * 64))
    controller.tick([node(artifact_sha256="a" * 64)], now=10)
    command = controller.commands_for("n", now=10)[0]
    assert command.kind == ActionKind.WARM
    controller.acknowledge(
        "n",
        command.action_id,
        MutationStatus.SUCCEEDED,
        duration_seconds=20,
        now=11,
    )
    assert controller.status(now=11)["learned_warm_seconds"]

    controller.put_profile(profile(artifact_sha256="b" * 64))

    assert controller.status(now=12)["learned_warm_seconds"] == []


def test_controller_learned_warm_cost_selects_preemption_victim():
    controller = AllocatorController(
        mode=AllocatorMode.AUTOMATIC,
        planner_policy=PlannerPolicy(memory_headroom_fraction=0),
    )
    batch = ModelProfile(
        "batch",
        8_000,
        runtimes=("llama.cpp",),
        min_replicas=2,
        max_replicas=2,
        priority=10,
        min_residency_seconds=0,
    )
    controller.put_profile(batch)

    def batch_node(node_id: str, *, ready: bool) -> NodeSnapshot:
        return NodeSnapshot(
            node_id,
            8_000,
            runtimes=("llama.cpp",),
            backends=("metal",),
            cached_models=("batch",),
            residencies=(
                (
                    ModelResidency(
                        "batch",
                        8_000,
                        ResidencyState.READY,
                        loaded_at=1,
                    ),
                )
                if ready
                else ()
            ),
            last_heartbeat=10,
        )

    cold = (batch_node("a-expensive", ready=False), batch_node("z-cheap", ready=False))
    controller.tick(cold, now=10)
    commands = {
        command.node_id: command
        for node_id in ("a-expensive", "z-cheap")
        for command in controller.commands_for(node_id, now=10)
        if command.kind == ActionKind.WARM
    }
    assert set(commands) == {"a-expensive", "z-cheap"}
    controller.acknowledge(
        "a-expensive",
        commands["a-expensive"].action_id,
        MutationStatus.SUCCEEDED,
        duration_seconds=100,
        now=11,
    )
    controller.acknowledge(
        "z-cheap",
        commands["z-cheap"].action_id,
        MutationStatus.SUCCEEDED,
        duration_seconds=1,
        now=11,
    )

    controller.put_profile(
        ModelProfile(
            "critical",
            8_000,
            runtimes=("llama.cpp",),
            priority=1_000,
            min_residency_seconds=0,
        )
    )
    controller.tick(
        (batch_node("a-expensive", ready=True), batch_node("z-cheap", ready=True)),
        now=12,
    )

    assert [
        (item.node_id, item.model_id, item.for_model_id)
        for item in controller.last_plan.preemptions
    ] == [("z-cheap", "batch", "critical")]


@pytest.mark.parametrize("duration", ["nan", "inf", -1, 3_601, True, object()])
def test_controller_ignores_untrusted_action_durations(duration):
    controller = AllocatorController(mode=AllocatorMode.AUTOMATIC)
    controller.put_profile(profile())
    controller.tick([node(cached=True)], now=10)
    command = controller.commands_for("n", now=10)[0]

    record = controller.acknowledge(
        "n",
        command.action_id,
        MutationStatus.SUCCEEDED,
        duration_seconds=duration,
        now=11,
    )

    assert record.duration_seconds == 0
    assert controller.status(now=11)["learned_warm_seconds"] == []


def test_controller_rejects_action_ack_from_the_wrong_node():
    controller = AllocatorController(mode=AllocatorMode.AUTOMATIC)
    controller.put_profile(profile())
    controller.tick([node(cached=True)], now=10)
    action = controller.commands_for("n", now=10)[0]
    with pytest.raises(KeyError, match="unknown"):
        controller.acknowledge(
            "attacker", action.action_id, MutationStatus.SUCCEEDED, now=11
        )


def test_invalid_ack_timestamp_cannot_leak_failure_streak_or_backoff():
    controller = AllocatorController(mode=AllocatorMode.AUTOMATIC)
    controller.put_profile(profile())
    controller.tick([node(cached=True)], now=10)
    action = controller.commands_for("n", now=10)[0]

    with pytest.raises(ValueError, match="finite and non-negative"):
        controller.acknowledge(
            "n",
            action.action_id,
            MutationStatus.FAILED,
            now=float("nan"),
        )

    failure = controller.acknowledge(
        "n",
        action.action_id,
        MutationStatus.FAILED,
        now=11,
    )
    assert failure.failures == 1


def test_post_replace_directory_fsync_failure_keeps_memory_and_disk_consistent(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "allocator.json"
    controller = AllocatorController(state_path=path)
    real_fsync = jsonio.os.fsync
    calls = 0

    def fail_directory_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("directory fsync failed")
        real_fsync(fd)

    monkeypatch.setattr(jsonio.os, "fsync", fail_directory_fsync)
    with pytest.raises(jsonio.AtomicWriteCommittedError, match="committed"):
        controller.put_profile(profile())

    # os.replace already made the profile visible. The live transaction must remain applied too,
    # even though the caller is warned that crash durability could not be confirmed.
    assert [item.model_id for item in controller.profiles] == ["qwen"]
    assert [
        item.model_id for item in AllocatorController(state_path=path).profiles
    ] == ["qwen"]


def test_controller_disabling_automatic_mode_cancels_pending_commands():
    controller = AllocatorController(mode=AllocatorMode.AUTOMATIC)
    controller.put_profile(profile())
    controller.tick([node(cached=True)], now=10)
    action = controller.commands_for("n", now=10)[0]
    controller.set_mode(AllocatorMode.RECOMMEND)
    assert controller.commands_for("n", now=11) == ()
    record = next(
        item
        for item in reversed(controller.history)
        if item.action_id == action.action_id
    )
    assert record.status == MutationStatus.CANCELLED


def test_controller_removing_profile_cancels_work_and_demand():
    controller = AllocatorController(mode=AllocatorMode.AUTOMATIC)
    controller.put_profile(profile())
    controller.observe("qwen", service_seconds=2, timestamp=1)
    controller.tick([node(cached=True)], now=10)
    assert controller.remove_profile("qwen")
    assert not controller.remove_profile("qwen")
    assert len(controller.profiles) == 1
    assert controller.profiles[0].min_replicas == 0
    assert controller.profiles[0].max_replicas == 0
    assert controller.commands_for("n", now=11) == ()
    status = controller.status(now=11)
    assert status["forecasts"] == []
    assert status["retiring_models"] == ["qwen"]
    assert status["models"][0]["retiring"] is True


def test_controller_status_contains_forecast_plan_and_reconciliation():
    controller = AllocatorController()
    controller.put_profile(profile())
    controller.observe("qwen", service_seconds=2, latency_ms=500, timestamp=1)
    controller.tick([node()], now=10)
    status = controller.status([node()], now=10)
    assert status["mode"] == "recommend"
    assert status["forecasts"][0]["sample_count"] == 1
    assert status["plan"]["desired_replicas"] == {"qwen": 1}
    assert status["reconciliation"]["actions"]


def test_controller_prewarms_correlated_model_group_before_peer_request_arrives():
    controller = AllocatorController()
    for model_id in ("source", "target"):
        controller.put_profile(
            ModelProfile(
                model_id,
                4_000,
                runtimes=("llama.cpp",),
                min_replicas=0,
                max_replicas=2,
                min_residency_seconds=0,
                scale_down_cooldown_seconds=0,
            )
        )
    for timestamp in (1, 61, 121, 181):
        for _ in range(40):
            controller.observe("source", service_seconds=1, timestamp=timestamp)
        for _ in range(10):
            controller.observe("target", service_seconds=10, timestamp=timestamp)
    for timestamp in (241, 301, 361):
        for _ in range(40):
            controller.observe("source", service_seconds=1, timestamp=timestamp)
    nodes = tuple(
        NodeSnapshot(
            f"node-{index}",
            8_000,
            runtimes=("llama.cpp",),
            backends=("metal",),
            cached_models=("source", "target"),
            last_heartbeat=361,
        )
        for index in range(4)
    )

    controller.tick(nodes, now=361)
    status = controller.status(nodes, now=361)
    forecasts = {item["model_id"]: item for item in status["forecasts"]}

    assert forecasts["target"]["correlation_sources"] == ("source",)
    assert forecasts["target"]["offered_concurrency"] > 0
    assert status["plan"]["desired_replicas"]["target"] == 2
    assignments = status["plan"]["assignments"]
    assert sum(item["model_id"] == "source" for item in assignments) == 2
    assert sum(item["model_id"] == "target" for item in assignments) == 2
    assert len({item["node_id"] for item in assignments}) == 4
    assert len(status["reconciliation"]["actions"]) == 4
    assert {
        (item["kind"], item["model_id"]) for item in status["reconciliation"]["actions"]
    } == {("warm", "source"), ("warm", "target")}


def test_controller_ignores_unconfigured_and_retiring_demand_keys():
    controller = AllocatorController()
    assert (
        controller.observe("attacker-chosen-name", service_seconds=1, timestamp=1)
        is False
    )
    assert controller.demand.to_dict()["models"] == {}

    controller.put_profile(profile())
    assert controller.observe("qwen", service_seconds=1, timestamp=2) is True
    controller.remove_profile("qwen")
    assert controller.observe("qwen", service_seconds=1, timestamp=3) is False
    assert controller.demand.to_dict()["models"] == {}


def test_controller_persists_configuration_demand_history_and_commands(tmp_path):
    path = tmp_path / "allocator.json"
    controller = AllocatorController(
        mode=AllocatorMode.AUTOMATIC,
        state_path=path,
        reconcile_policy=ReconcilePolicy(max_concurrent_mutations=1),
    )
    controller.put_profile(profile())
    controller.observe("qwen", service_seconds=2, timestamp=1)
    controller.tick([node(cached=True)], now=10)
    action = controller.commands_for("n", now=10)[0]

    restored = AllocatorController(state_path=path)
    assert restored.mode == AllocatorMode.AUTOMATIC
    assert restored.profiles == (profile(),)
    assert restored.commands_for("n", now=10)[0].action_id == action.action_id
    assert restored.status(now=10)["forecasts"][0]["sample_count"] == 1


def test_tick_rolls_back_executable_commands_when_durable_write_fails(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "allocator.json"
    controller = AllocatorController(
        mode=AllocatorMode.AUTOMATIC,
        state_path=path,
    )
    controller.put_profile(profile())

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(
        "shared.allocator.controller.jsonio.atomic_write_json",
        fail_write,
    )
    with pytest.raises(OSError, match="disk full"):
        controller.tick([node(cached=True)], now=10)

    status = controller.status(now=10)
    assert status["plan_sequence"] == 0
    assert status["plan"] is None
    assert status["pending_commands"] == []
    assert status["history"] == []

    restored = AllocatorController(state_path=path)
    assert restored.commands_for("n", now=10) == ()


def test_controller_rejects_unknown_persisted_schema(tmp_path):
    path = tmp_path / "allocator.json"
    path.write_text('{"schema_version": 99}')
    with pytest.raises(ValueError, match="unsupported"):
        AllocatorController(state_path=path)


def test_controller_rejects_inconsistent_persisted_retirement_tombstone(tmp_path):
    path = tmp_path / "allocator.json"
    controller = AllocatorController(
        state_path=path,
        planner_policy=PlannerPolicy(node_ttl_seconds=0),
    )
    controller.put_profile(profile())
    state = json.loads(path.read_text())
    state["retiring_models"] = ["qwen"]
    path.write_text(json.dumps(state))
    with pytest.raises(ValueError, match="retirement tombstone"):
        AllocatorController(state_path=path)


def test_controller_is_thread_safe_for_request_observation():
    controller = AllocatorController()
    controller.put_profile(profile())

    def observe(start: int) -> None:
        for index in range(100):
            controller.observe(
                "qwen", service_seconds=0.1, timestamp=start + index / 1000
            )

    threads = [threading.Thread(target=observe, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert controller.status(now=10)["forecasts"][0]["sample_count"] == 400


def test_request_observation_does_not_wait_for_a_long_placement_cycle(monkeypatch):
    controller = AllocatorController()
    controller.put_profile(profile())
    planning = threading.Event()
    release = threading.Event()
    original_plan = controller.planner.plan

    def slow_plan(*args, **kwargs):
        planning.set()
        assert release.wait(1)
        return original_plan(*args, **kwargs)

    monkeypatch.setattr(controller.planner, "plan", slow_plan)
    tick = threading.Thread(target=lambda: controller.tick([node()], now=10))
    tick.start()
    assert planning.wait(1)

    started = time.monotonic()
    assert controller.observe("qwen", service_seconds=0.1, timestamp=11)
    assert time.monotonic() - started < 0.1

    release.set()
    tick.join(1)
    assert not tick.is_alive()
    assert controller.status(now=11)["forecasts"][0]["sample_count"] == 1


def test_failed_tick_rollback_preserves_demand_observed_during_planning(
    tmp_path,
    monkeypatch,
):
    controller = AllocatorController(state_path=tmp_path / "allocator.json")
    controller.put_profile(profile())
    planning = threading.Event()
    release = threading.Event()
    original_plan = controller.planner.plan
    failure: list[BaseException] = []

    def slow_plan(*args, **kwargs):
        planning.set()
        assert release.wait(1)
        return original_plan(*args, **kwargs)

    def fail_save() -> None:
        raise OSError("disk full")

    def tick() -> None:
        try:
            controller.tick([node()], now=10)
        except BaseException as exc:  # noqa: BLE001 - asserted below across the thread boundary
            failure.append(exc)

    monkeypatch.setattr(controller.planner, "plan", slow_plan)
    monkeypatch.setattr(controller, "_save", fail_save)
    worker = threading.Thread(target=tick)
    worker.start()
    assert planning.wait(1)
    assert controller.observe("qwen", service_seconds=0.1, timestamp=11)
    release.set()
    worker.join(1)

    assert len(failure) == 1
    assert isinstance(failure[0], OSError)
    assert controller.status(now=11)["forecasts"][0]["sample_count"] == 1


def test_failed_profile_retirement_keeps_concurrent_demand_observable(
    tmp_path,
    monkeypatch,
):
    controller = AllocatorController(state_path=tmp_path / "allocator.json")
    controller.put_profile(profile())
    saving = threading.Event()
    release = threading.Event()
    failure: list[BaseException] = []

    def fail_save() -> None:
        saving.set()
        assert release.wait(1)
        raise OSError("disk full")

    def retire() -> None:
        try:
            controller.remove_profile("qwen")
        except BaseException as exc:  # noqa: BLE001 - asserted across the thread boundary
            failure.append(exc)

    monkeypatch.setattr(controller, "_save", fail_save)
    worker = threading.Thread(target=retire)
    worker.start()
    assert saving.wait(1)

    assert controller.observe("qwen", service_seconds=0.1, timestamp=11)
    release.set()
    worker.join(1)

    assert len(failure) == 1
    assert isinstance(failure[0], OSError)
    status = controller.status(now=11)
    assert status["models"][0]["retiring"] is False
    assert status["forecasts"][0]["sample_count"] == 1


def test_planner_exception_rolls_back_all_controller_state_but_not_demand(monkeypatch):
    controller = AllocatorController(
        mode=AllocatorMode.AUTOMATIC,
        reconcile_policy=ReconcilePolicy(
            failure_backoff_base_seconds=10,
            failure_backoff_max_seconds=10,
        ),
    )
    controller.put_profile(profile())
    controller.tick([node(cached=True, heartbeat=100)], now=100)
    command = controller.commands_for("n", now=100)[0]
    controller.acknowledge("n", command.action_id, MutationStatus.FAILED, now=100)
    before = controller._checkpoint()

    def fail_plan(*args, **kwargs):
        controller.observe("qwen", service_seconds=0.1, timestamp=2)
        raise RuntimeError("planner failed")

    monkeypatch.setattr(controller.planner, "plan", fail_plan)
    with pytest.raises(RuntimeError, match="planner failed"):
        # Bounding the future backoff for this rolled-back clock happens before planner invocation.
        controller.tick([node(cached=True, heartbeat=1)], now=1)

    after = controller._checkpoint()
    assert after == before
    assert controller.status(now=2)["forecasts"][0]["sample_count"] == 1


def test_restored_retirement_tombstone_cannot_resurrect_stale_demand(tmp_path):
    path = tmp_path / "allocator.json"
    controller = AllocatorController(state_path=path)
    controller.put_profile(profile())
    controller.observe("qwen", service_seconds=1, timestamp=1)

    assert controller.remove_profile("qwen")
    restored = AllocatorController(state_path=path)
    restored.put_profile(profile())

    assert restored.status(now=2)["forecasts"][0]["sample_count"] == 0


def test_controller_tick_with_ready_replica_proposes_no_mutation():
    controller = AllocatorController(mode=AllocatorMode.AUTOMATIC)
    controller.put_profile(profile())
    result = controller.tick([node(ready=True)], now=10)
    assert result.actions == ()
    assert controller.commands_for("n", now=10) == ()


def test_delivered_warm_intent_prevents_duplicate_cold_start_before_heartbeat():
    digest = "a" * 64
    controller = AllocatorController(mode=AllocatorMode.AUTOMATIC)
    controller.put_profile(profile(artifact_sha256=digest))
    cheap = replace(
        node(node_id="cheap", artifact_sha256=digest),
        max_models=1,
    )
    expensive = replace(
        node(node_id="expensive"),
        max_models=1,
    )

    first = controller.tick((cheap, expensive), now=10)
    assert [(item.node_id, item.kind) for item in first.executable_actions] == [
        ("cheap", ActionKind.WARM)
    ]
    command = controller.commands_for("cheap", now=10)[0]

    # The next registry snapshot has not caught up with the delivered WARM. Meanwhile the other
    # node reports cached weights and would otherwise look cheaper to start.
    lagging_cheap = replace(cheap, residencies=(), last_heartbeat=11)
    newly_cached_expensive = replace(
        expensive,
        residencies=(
            ModelResidency(
                "qwen",
                8_000,
                ResidencyState.CACHED,
                artifact_sha256=digest,
            ),
        ),
        last_heartbeat=11,
    )
    controller.tick((lagging_cheap, newly_cached_expensive), now=11)

    assert controller.last_plan is not None
    assert [
        (item.node_id, item.model_id) for item in controller.last_plan.assignments
    ] == [("cheap", "qwen")]
    assert command.action_id in {
        item["action_id"]
        for item in controller.status((lagging_cheap, newly_cached_expensive), now=11)[
            "pending_commands"
        ]
    }


def test_controller_plan_generation_is_logical_stable_and_persisted(tmp_path):
    path = tmp_path / "allocator.json"
    controller = AllocatorController(state_path=path)
    controller.put_profile(profile())

    first = controller.tick([node(cached=True)], now=10).plan_generation
    unchanged = controller.tick([node(cached=True)], now=20).plan_generation
    assert unchanged == first
    epoch, sequence, digest = first.split(":")
    assert len(epoch) == 32
    assert sequence == "00000000000000000001"
    assert len(digest) == 12

    restored = AllocatorController(state_path=path)
    assert restored.tick([node(cached=True)], now=30).plan_generation == first
    expired = restored.tick([node(cached=True)], now=101).plan_generation
    assert expired.split(":")[:2] == [epoch, "00000000000000000002"]
    restored.put_profile(replace(profile(), priority=101))
    changed = restored.tick([node(cached=True)], now=102).plan_generation
    assert changed.split(":")[:2] == [epoch, "00000000000000000003"]
    assert restored.status(now=102)["plan_sequence"] == 3


def test_controller_refences_pending_commands_when_authority_changes(tmp_path):
    path = tmp_path / "allocator.json"
    controller = AllocatorController(
        mode=AllocatorMode.AUTOMATIC,
        state_path=path,
    )
    controller.put_profile(profile())
    controller.tick([node(cached=True)], now=10)
    original = controller.commands_for("n", now=10)[0]

    controller.update_authority(2, "successor", 200)
    refenced = controller.commands_for("n", now=10)[0]
    assert refenced.action_id == original.action_id
    assert refenced.controller_term == 2
    assert refenced.controller_id == "successor"
    assert refenced.controller_lease_expires_at == 200

    restored = AllocatorController(state_path=path)
    persisted = restored.commands_for("n", now=10)[0]
    assert persisted.controller_term == 2
    assert persisted.controller_id == "successor"


def test_controller_rejects_inconsistent_persisted_plan_generation(tmp_path):
    path = tmp_path / "allocator.json"
    controller = AllocatorController(state_path=path)
    controller.put_profile(profile())
    controller.tick([node(cached=True)], now=10)
    valid = json.loads(path.read_text())

    invalid_states = (
        {**valid, "last_plan_generation": "not-a-generation"},
        {**valid, "plan_sequence": valid["plan_sequence"] + 1},
        {**valid, "last_plan_input_digest": "0" * 64},
        {**valid, "last_plan_generation": ""},
    )
    for state in invalid_states:
        path.write_text(json.dumps(state))
        with pytest.raises(ValueError, match="plan generation"):
            AllocatorController(state_path=path)


def test_restored_commands_wait_for_membership_recovery_grace(tmp_path):
    path = tmp_path / "allocator.json"
    controller = AllocatorController(
        mode=AllocatorMode.AUTOMATIC,
        state_path=path,
        membership_recovery_grace_seconds=30,
    )
    controller.put_profile(profile())
    controller.tick([node(cached=True)], now=10)
    action = controller.commands_for("n", now=10)[0]

    restored = AllocatorController(state_path=path)
    # Downtime itself does not consume the grace: membership recovery starts on the first tick.
    restored.tick([], now=10_000)
    assert restored.commands_for("n", now=10_000) == (action,)
    restored.tick([], now=10_029.999)
    assert restored.commands_for("n", now=10_029.999) == (action,)
    restored.tick([], now=10_030)
    assert restored.commands_for("n", now=10_030) == ()
    assert restored.history[-1].status == MutationStatus.CANCELLED
    assert restored.history[-1].message == "target node is not live"


def test_restored_command_is_kept_when_target_membership_recovers(tmp_path):
    path = tmp_path / "allocator.json"
    controller = AllocatorController(
        mode=AllocatorMode.AUTOMATIC,
        state_path=path,
        membership_recovery_grace_seconds=30,
    )
    controller.put_profile(profile())
    controller.tick([node(cached=True)], now=10)
    action = controller.commands_for("n", now=10)[0]

    restored = AllocatorController(state_path=path)
    restored.tick([], now=20)
    restored.tick([node(cached=True)], now=21)
    assert restored.commands_for("n", now=21) == (action,)


def test_late_terminal_ack_after_cancellation_is_recorded_without_error():
    controller = AllocatorController(mode=AllocatorMode.AUTOMATIC)
    controller.put_profile(profile())
    controller.tick([node(cached=True)], now=10)
    action = controller.commands_for("n", now=10)[0]
    controller.tick([], now=20)
    assert controller.history[-1].status == MutationStatus.CANCELLED

    late = controller.acknowledge(
        "n",
        action.action_id,
        MutationStatus.SUCCEEDED,
        message="finished after cancellation",
        now=21,
    )
    assert late.status == MutationStatus.SUCCEEDED
    assert late.message == "finished after cancellation"
    assert (
        controller.acknowledge("n", action.action_id, MutationStatus.FAILED, now=22)
        == late
    )


def test_failure_backoff_accumulates_across_attempts_restart_and_resets(tmp_path):
    path = tmp_path / "allocator.json"
    policy = ReconcilePolicy(
        mutation_cooldown_seconds=0,
        failure_backoff_base_seconds=10,
        failure_backoff_max_seconds=100,
        success_observation_timeout_seconds=0,
    )
    controller = AllocatorController(
        mode=AllocatorMode.AUTOMATIC,
        state_path=path,
        reconcile_policy=policy,
    )
    controller.put_profile(profile())
    controller.tick([node(cached=True)], now=10)
    first = controller.commands_for("n", now=10)[0]
    assert (
        controller.acknowledge(
            "n", first.action_id, MutationStatus.FAILED, now=10
        ).failures
        == 1
    )

    restored = AllocatorController(state_path=path)
    assert restored.tick([node(cached=True)], now=19).actions == ()
    restored.tick([node(cached=True)], now=20)
    second = restored.commands_for("n", now=20)[0]
    assert second.action_id != first.action_id
    assert (
        restored.acknowledge(
            "n", second.action_id, MutationStatus.FAILED, now=20
        ).failures
        == 2
    )
    assert restored.tick([node(cached=True)], now=39).actions == ()

    restored.tick([node(cached=True)], now=40)
    third = restored.commands_for("n", now=40)[0]
    assert third.action_id not in {first.action_id, second.action_id}
    assert (
        restored.acknowledge(
            "n", third.action_id, MutationStatus.SUCCEEDED, now=40
        ).failures
        == 0
    )

    restored.tick([node(cached=True)], now=40)
    fourth = restored.commands_for("n", now=40)[0]
    assert (
        restored.acknowledge(
            "n", fourth.action_id, MutationStatus.FAILED, now=41
        ).failures
        == 1
    )


def test_failure_backoff_expires_after_wall_clock_rollback():
    policy = ReconcilePolicy(
        mutation_cooldown_seconds=0,
        failure_backoff_base_seconds=10,
        failure_backoff_max_seconds=10,
        success_observation_timeout_seconds=0,
    )
    controller = AllocatorController(
        mode=AllocatorMode.AUTOMATIC,
        reconcile_policy=policy,
        max_history=1,
    )
    controller.put_profile(profile())
    controller.tick([node(cached=True, heartbeat=100)], now=100)
    failed = controller.commands_for("n", now=100)[0]
    controller.acknowledge("n", failed.action_id, MutationStatus.FAILED, now=100)

    # A rollback bounds the persisted deadline once. Repeated ticks before that bounded deadline
    # must not turn the terminal history row into a sliding ``now + backoff`` block.
    assert controller.tick([node(cached=True, heartbeat=1)], now=1).actions == ()
    assert controller.tick([node(cached=True, heartbeat=10)], now=10).actions == ()
    retried = controller.tick([node(cached=True, heartbeat=11)], now=11)
    assert [action.kind for action in retried.executable_actions] == [ActionKind.WARM]


def test_failed_load_cancels_its_dependent_warm_and_allows_a_fresh_chain():
    controller = AllocatorController(
        mode=AllocatorMode.AUTOMATIC,
        reconcile_policy=ReconcilePolicy(
            max_concurrent_mutations=2,
            max_mutations_per_node=2,
            mutation_cooldown_seconds=0,
            failure_backoff_base_seconds=0,
            failure_backoff_max_seconds=0,
            success_observation_timeout_seconds=0,
        ),
    )
    controller.put_profile(profile())
    controller.tick([node(cached=False)], now=10)
    first = controller.commands_for("n", now=10)
    first_load = next(item for item in first if item.kind == ActionKind.LOAD)
    first_warm = next(item for item in first if item.kind == ActionKind.WARM)
    assert first_warm.dependencies == (first_load.action_id,)

    controller.acknowledge("n", first_load.action_id, MutationStatus.FAILED, now=11)
    assert controller.commands_for("n", now=11) == ()
    assert (
        next(
            item
            for item in reversed(controller.history)
            if item.action_id == first_warm.action_id
        ).status
        == MutationStatus.CANCELLED
    )

    controller.tick([node(cached=False)], now=12)
    replacement = controller.commands_for("n", now=12)
    replacement_load = next(
        item for item in replacement if item.kind == ActionKind.LOAD
    )
    replacement_warm = next(
        item for item in replacement if item.kind == ActionKind.WARM
    )
    assert replacement_load.action_id != first_load.action_id
    assert replacement_warm.action_id != first_warm.action_id
    assert replacement_warm.dependencies == (replacement_load.action_id,)


def test_retired_profile_persists_and_drains_a_node_that_returns_later(tmp_path):
    path = tmp_path / "allocator.json"
    controller = AllocatorController(
        mode=AllocatorMode.AUTOMATIC,
        state_path=path,
        reconcile_policy=ReconcilePolicy(
            mutation_cooldown_seconds=0,
            success_observation_timeout_seconds=0,
        ),
    )
    controller.put_profile(profile())
    assert controller.remove_profile("qwen")

    restored = AllocatorController(state_path=path)
    restored.tick([], now=100_000)
    assert restored.status(now=100_000)["retiring_models"] == ["qwen"]

    restored.tick([node(ready=True, heartbeat=100_001)], now=100_001)
    drain = restored.commands_for("n", now=100_001)[0]
    assert drain.kind == ActionKind.DRAIN
    restored.acknowledge("n", drain.action_id, MutationStatus.SUCCEEDED, now=100_002)

    draining = NodeSnapshot(
        "n",
        16_000,
        runtimes=("llama.cpp",),
        backends=("metal",),
        residencies=(
            ModelResidency(
                "qwen",
                8_000,
                ResidencyState.DRAINING,
                loaded_at=1,
                active_requests=0,
            ),
        ),
        last_heartbeat=100_003,
    )
    restored.tick([draining], now=100_003)
    unload = restored.commands_for("n", now=100_003)[0]
    assert unload.kind == ActionKind.UNLOAD
    assert restored.status(now=100_003)["retiring_models"] == ["qwen"]

    restored.put_profile(profile())
    assert restored.status(now=100_004)["retiring_models"] == []
    assert restored.profiles == (profile(),)


def test_unload_waits_only_for_requests_on_the_draining_model():
    draining_idle = ModelResidency(
        "a",
        8_000,
        ResidencyState.DRAINING,
        loaded_at=1,
        active_requests=0,
    )
    busy_other_model = ModelResidency(
        "b",
        8_000,
        ResidencyState.READY,
        loaded_at=1,
        active_requests=5,
    )
    machine = NodeSnapshot(
        "n",
        24_000,
        runtimes=("llama.cpp",),
        backends=("metal",),
        residencies=(draining_idle, busy_other_model),
        active_requests=5,
        last_heartbeat=10,
    )
    zero_profile = replace(profile("a"), min_replicas=0, max_replicas=0)
    controller = AllocatorController(mode=AllocatorMode.AUTOMATIC)
    controller.put_profile(zero_profile)
    result = controller.tick([machine], now=10)
    assert any(
        action.kind == ActionKind.UNLOAD and action.model_id == "a"
        for action in result.executable_actions
    )

    draining_busy = replace(draining_idle, active_requests=1)
    busy_machine = replace(
        machine,
        residencies=(draining_busy, replace(busy_other_model, active_requests=0)),
        active_requests=1,
    )
    blocked = AllocatorController(mode=AllocatorMode.AUTOMATIC)
    blocked.put_profile(zero_profile)
    blocked_result = blocked.tick([busy_machine], now=10)
    assert not any(
        action.kind == ActionKind.UNLOAD and action.model_id == "a"
        for action in blocked_result.actions
    )
    assert any(
        item.code == "requests_in_flight" and item.model_id == "a"
        for item in blocked_result.deferred
    )


def test_active_commands_are_exempt_from_terminal_history_cap():
    controller = AllocatorController(
        mode=AllocatorMode.AUTOMATIC,
        max_history=1,
        reconcile_policy=ReconcilePolicy(
            max_concurrent_mutations=2,
            max_mutations_per_node=1,
        ),
    )
    controller.put_profile(replace(profile(), min_replicas=2, max_replicas=2))
    machines = [node(cached=True, node_id="a"), node(cached=True, node_id="b")]
    controller.tick(machines, now=10)

    commands = controller.commands_for("a", now=10) + controller.commands_for(
        "b", now=10
    )
    assert len(commands) == 2
    assert {record.action_id for record in controller.history} == {
        command.action_id for command in commands
    }
    assert all(record.status == MutationStatus.PENDING for record in controller.history)

    controller.tick(machines, now=11)
    assert len(controller.commands_for("a", now=11)) == 1
    assert len(controller.commands_for("b", now=11)) == 1


def test_rebound_success_block_provenance_survives_history_compaction_and_restart(
    tmp_path,
):
    path = tmp_path / "allocator.json"
    controller = AllocatorController(
        mode=AllocatorMode.AUTOMATIC,
        state_path=path,
        max_history=1,
        reconcile_policy=ReconcilePolicy(
            mutation_cooldown_seconds=0,
            success_observation_timeout_seconds=120,
            max_concurrent_mutations=2,
        ),
    )
    controller.put_profile(profile("qwen"))
    controller.tick([node(cached=True)], now=10)
    qwen_warm = controller.commands_for("n", now=10)[0]
    controller.acknowledge("n", qwen_warm.action_id, MutationStatus.SUCCEEDED, now=11)

    controller.put_profile(profile("other"))
    qwen_ready = ModelResidency(
        "qwen", 8_000, ResidencyState.READY, loaded_at=1, managed=True
    )
    both_cached = NodeSnapshot(
        "n",
        32_000,
        runtimes=("llama.cpp",),
        backends=("metal",),
        cached_models=("qwen", "other"),
        residencies=(qwen_ready,),
        last_heartbeat=12,
    )
    controller.tick([both_cached], now=12)
    other_warm = controller.commands_for("n", now=12)[0]
    assert other_warm.model_id == "other"
    controller.acknowledge("n", other_warm.action_id, MutationStatus.SUCCEEDED, now=13)
    assert [item.model_id for item in controller.history] == ["other"]

    restored = AllocatorController(state_path=path)
    draining_qwen = replace(qwen_ready, state=ResidencyState.DRAINING)
    other_ready = ModelResidency(
        "other", 8_000, ResidencyState.READY, loaded_at=12, managed=True
    )
    rebound = restored.tick(
        [
            replace(
                both_cached,
                residencies=(draining_qwen, other_ready),
                last_heartbeat=14,
            )
        ],
        now=14,
    )

    assert [
        (action.kind, action.model_id) for action in rebound.executable_actions
    ] == [(ActionKind.WARM, "qwen")]


def test_profile_memory_change_cancels_and_replaces_pending_warm():
    controller = AllocatorController(
        mode=AllocatorMode.AUTOMATIC,
        reconcile_policy=ReconcilePolicy(
            mutation_cooldown_seconds=0,
            success_observation_timeout_seconds=0,
        ),
    )
    controller.put_profile(profile())
    controller.tick([node(cached=True)], now=10)
    old = controller.commands_for("n", now=10)[0]
    assert old.kind == ActionKind.WARM
    assert old.memory_mb == 8_000

    controller.put_profile(replace(profile(), memory_mb=12_000))
    controller.tick([node(cached=True, heartbeat=11)], now=11)
    replacement = controller.commands_for("n", now=11)[0]

    assert replacement.action_id != old.action_id
    assert replacement.memory_mb == 12_000
    cancelled = next(
        record for record in controller.history if record.action_id == old.action_id
    )
    assert cancelled.status == MutationStatus.CANCELLED


def allocator_node(
    node_id: str,
    *,
    residency: ModelResidency | None = None,
    cached: bool = True,
    domain: str = "",
    manually_managed: bool = False,
    heartbeat: float = 10,
) -> NodeSnapshot:
    return NodeSnapshot(
        node_id,
        16_000,
        runtimes=("llama.cpp",),
        backends=("metal",),
        failure_domain=domain,
        cached_models=(("qwen",) if cached else ()),
        residencies=((residency,) if residency is not None else ()),
        manually_managed=manually_managed,
        last_heartbeat=heartbeat,
    )


def ready_qwen(**kwargs) -> ModelResidency:
    return ModelResidency(
        "qwen",
        8_000,
        ResidencyState.READY,
        loaded_at=1,
        **kwargs,
    )


def automatic_controller(*, concurrent: int = 4) -> AllocatorController:
    return AllocatorController(
        mode=AllocatorMode.AUTOMATIC,
        reconcile_policy=ReconcilePolicy(
            max_concurrent_mutations=concurrent,
            mutation_cooldown_seconds=0,
            success_observation_timeout_seconds=0,
        ),
    )


def test_success_waits_for_a_post_ack_heartbeat_before_repeating_drain():
    controller = AllocatorController(
        mode=AllocatorMode.AUTOMATIC,
        reconcile_policy=ReconcilePolicy(
            mutation_cooldown_seconds=0,
            success_observation_timeout_seconds=120,
        ),
    )
    controller.put_profile(
        replace(
            profile(),
            min_replicas=0,
            max_replicas=1,
            min_residency_seconds=0,
            scale_down_cooldown_seconds=0,
        )
    )
    stale = allocator_node(
        "n",
        residency=ready_qwen(),
        heartbeat=10,
    )
    controller.tick((stale,), now=10)
    first = controller.commands_for("n", now=10)[0]
    assert first.kind == ActionKind.DRAIN
    controller.acknowledge(
        "n",
        first.action_id,
        MutationStatus.SUCCEEDED,
        now=11,
    )

    controller.tick((stale,), now=11.5)
    assert controller.commands_for("n", now=11.5) == ()

    controller.tick((replace(stale, last_heartbeat=12),), now=12)
    repeated = controller.commands_for("n", now=12)
    assert len(repeated) == 1
    assert repeated[0].kind == ActionKind.DRAIN


def test_pending_drain_is_cancelled_when_new_required_replacement_is_not_ready():
    controller = automatic_controller()
    baseline = replace(
        profile(),
        min_residency_seconds=0,
        scale_down_cooldown_seconds=0,
    )
    controller.put_profile(baseline)
    nodes = (
        allocator_node("a", residency=ready_qwen()),
        allocator_node("b", residency=ready_qwen()),
        allocator_node("c"),
    )
    controller.tick(nodes, now=10)
    stale_drain = controller.commands_for("b", now=10)[0]
    assert stale_drain.kind == ActionKind.DRAIN

    controller.put_profile(
        replace(
            baseline,
            min_replicas=2,
            max_replicas=2,
            pinned_nodes=("c",),
        )
    )
    result = controller.tick(
        tuple(replace(item, last_heartbeat=11) for item in nodes),
        now=11,
    )

    assert controller.commands_for("b", now=11) == ()
    replacement = controller.commands_for("c", now=11)
    assert len(replacement) == 1
    assert replacement[0].kind == ActionKind.WARM
    assert any(
        item.node_id == "b" and item.code == "replacement_not_ready"
        for item in result.deferred
    )
    cancelled = next(
        item for item in controller.history if item.action_id == stale_drain.action_id
    )
    assert cancelled.status == MutationStatus.CANCELLED
    assert [
        item["action_id"] for item in controller.status(now=11)["withdrawn_destructive"]
    ] == [stale_drain.action_id]


@pytest.mark.parametrize("ownership_change", ["pinned", "manual"])
def test_pending_drain_is_cancelled_when_ownership_changes(ownership_change: str):
    controller = automatic_controller()
    model = replace(profile(), scale_down_cooldown_seconds=0)
    controller.put_profile(model)
    nodes = (
        allocator_node("a", residency=ready_qwen()),
        allocator_node("b", residency=ready_qwen()),
    )
    controller.tick(nodes, now=10)
    stale_drain = controller.commands_for("b", now=10)[0]

    old = nodes[1]
    changed = (
        replace(old, residencies=(replace(old.residencies[0], pinned=True),))
        if ownership_change == "pinned"
        else replace(old, manually_managed=True)
    )
    result = controller.tick(
        (replace(nodes[0], last_heartbeat=11), replace(changed, last_heartbeat=11)),
        now=11,
    )

    assert controller.commands_for("b", now=11) == ()
    assert any(
        item.node_id == "b" and item.code == "not_allocator_owned"
        for item in result.deferred
    )
    assert (
        next(
            item
            for item in controller.history
            if item.action_id == stale_drain.action_id
        ).status
        == MutationStatus.CANCELLED
    )


def test_pending_unload_is_cancelled_when_requests_resume():
    controller = automatic_controller()
    controller.put_profile(
        replace(
            profile(),
            min_replicas=0,
            max_replicas=0,
            min_residency_seconds=0,
            scale_down_cooldown_seconds=0,
        )
    )
    draining = replace(ready_qwen(), state=ResidencyState.DRAINING)
    machine = allocator_node("n", residency=draining)
    controller.tick((machine,), now=10)
    stale_unload = controller.commands_for("n", now=10)[0]
    assert stale_unload.kind == ActionKind.UNLOAD

    result = controller.tick(
        (
            replace(
                machine,
                residencies=(replace(draining, active_requests=1),),
                active_requests=1,
                last_heartbeat=11,
            ),
        ),
        now=11,
    )

    assert controller.commands_for("n", now=11) == ()
    assert any(item.code == "requests_in_flight" for item in result.deferred)
    assert (
        next(
            item
            for item in controller.history
            if item.action_id == stale_unload.action_id
        ).status
        == MutationStatus.CANCELLED
    )


def test_pending_drain_is_cancelled_when_minimum_residency_increases():
    controller = automatic_controller()
    retiring = replace(
        profile(),
        min_replicas=0,
        max_replicas=0,
        min_residency_seconds=0,
        scale_down_cooldown_seconds=0,
    )
    controller.put_profile(retiring)
    machine = allocator_node("n", residency=ready_qwen())
    controller.tick((machine,), now=10)
    stale_drain = controller.commands_for("n", now=10)[0]

    controller.put_profile(replace(retiring, min_residency_seconds=100))
    result = controller.tick((replace(machine, last_heartbeat=11),), now=11)

    assert controller.commands_for("n", now=11) == ()
    assert any(item.code == "minimum_residency" for item in result.deferred)
    assert (
        next(
            item
            for item in controller.history
            if item.action_id == stale_drain.action_id
        ).status
        == MutationStatus.CANCELLED
    )


def test_pending_drains_are_revalidated_as_one_failure_domain_batch():
    controller = automatic_controller()
    baseline = replace(
        profile(),
        min_failure_domains=1,
        scale_down_cooldown_seconds=0,
    )
    controller.put_profile(baseline)
    nodes = (
        allocator_node("a1", residency=ready_qwen(), domain="rack-a"),
        allocator_node("a2", residency=ready_qwen(), domain="rack-a"),
        allocator_node("b", residency=ready_qwen(), domain="rack-b"),
        allocator_node("c", residency=ready_qwen(), domain="rack-c"),
    )
    controller.tick(nodes, now=10)
    assert (
        sum(len(controller.commands_for(item.node_id, now=10)) for item in nodes) == 3
    )

    controller.put_profile(
        replace(
            baseline,
            min_replicas=2,
            max_replicas=2,
            min_failure_domains=2,
            pinned_nodes=("a1", "a2"),
        )
    )
    result = controller.tick(
        tuple(replace(item, last_heartbeat=11) for item in nodes),
        now=11,
    )

    surviving = [
        action
        for node_id in ("b", "c")
        for action in controller.commands_for(node_id, now=11)
        if action.kind == ActionKind.DRAIN
    ]
    assert surviving == []
    assert any(
        item.code == "destructive_outcome_unresolved" for item in result.deferred
    )
    assert len(controller.status(now=11)["withdrawn_destructive"]) == 3


def test_valid_active_drain_is_not_double_subtracted_from_its_failure_domain():
    controller = automatic_controller()
    model = replace(
        profile(),
        min_replicas=2,
        max_replicas=2,
        min_failure_domains=2,
        scale_down_cooldown_seconds=0,
    )
    controller.put_profile(model)
    nodes = (
        allocator_node("a1", residency=ready_qwen(), domain="rack-a"),
        allocator_node("a2", residency=ready_qwen(), domain="rack-a"),
        allocator_node("b", residency=ready_qwen(), domain="rack-b"),
    )
    controller.tick(nodes, now=10)
    active = controller.commands_for("a2", now=10)[0]
    assert active.kind == ActionKind.DRAIN

    result = controller.tick(
        tuple(replace(item, last_heartbeat=11) for item in nodes),
        now=11,
    )

    assert controller.commands_for("a2", now=11) == (active,)
    assert not any(
        item.node_id == "a2" and item.code == "failure_domain_replacement_not_ready"
        for item in result.deferred
    )


def test_command_delivery_marker_rolls_back_when_persistence_fails(
    tmp_path, monkeypatch
):
    path = tmp_path / "allocator.json"
    controller = AllocatorController(
        mode=AllocatorMode.AUTOMATIC,
        state_path=path,
        reconcile_policy=ReconcilePolicy(
            mutation_cooldown_seconds=0,
            success_observation_timeout_seconds=0,
        ),
    )
    controller.put_profile(profile())
    controller.tick([node(cached=True)], now=10)
    action_id = controller.status(now=10)["pending_commands"][0]["action_id"]

    def fail_save() -> None:
        raise OSError("disk full")

    monkeypatch.setattr(controller, "_save", fail_save)
    with pytest.raises(OSError, match="disk full"):
        controller.commands_for("n", now=10)

    assert action_id not in controller._delivered_command_ids
    assert action_id not in json.loads(path.read_text())["delivered_command_ids"]


def test_post_replace_delivery_marker_failure_keeps_committed_marker(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "allocator.json"
    controller = AllocatorController(
        mode=AllocatorMode.AUTOMATIC,
        state_path=path,
        reconcile_policy=ReconcilePolicy(
            mutation_cooldown_seconds=0,
            success_observation_timeout_seconds=0,
        ),
    )
    controller.put_profile(profile())
    controller.tick([node(cached=True)], now=10)
    action_id = controller.status(now=10)["pending_commands"][0]["action_id"]
    real_fsync = jsonio.os.fsync
    calls = 0

    def fail_directory_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("directory fsync failed")
        real_fsync(fd)

    monkeypatch.setattr(jsonio.os, "fsync", fail_directory_fsync)
    with pytest.raises(jsonio.AtomicWriteCommittedError, match="committed"):
        controller.commands_for("n", now=10)

    assert action_id in controller._delivered_command_ids
    assert action_id in json.loads(path.read_text())["delivered_command_ids"]
    assert action_id in AllocatorController(state_path=path)._delivered_command_ids


def test_withdrawn_destructive_survives_restart_and_history_compaction(tmp_path):
    path = tmp_path / "allocator.json"
    policy = ReconcilePolicy(
        max_concurrent_mutations=4,
        mutation_cooldown_seconds=0,
        success_observation_timeout_seconds=0,
    )
    controller = AllocatorController(
        mode=AllocatorMode.AUTOMATIC,
        state_path=path,
        max_history=1,
        reconcile_policy=policy,
    )
    retiring = replace(
        profile(),
        min_replicas=0,
        max_replicas=0,
        min_residency_seconds=0,
        scale_down_cooldown_seconds=0,
    )
    qwen_node = allocator_node("qwen-node", residency=ready_qwen())
    controller.put_profile(retiring)
    controller.tick((qwen_node,), now=10)
    withdrawn = controller.commands_for("qwen-node", now=10)[0]

    controller.put_profile(replace(retiring, min_residency_seconds=100))
    controller.tick((replace(qwen_node, last_heartbeat=11),), now=11)
    assert [
        item["action_id"] for item in controller.status(now=11)["withdrawn_destructive"]
    ] == [withdrawn.action_id]

    # The unresolved qwen command must not prevent an independent model from draining.
    controller.put_profile(replace(retiring, model_id="other", memory_mb=1_000))
    other_residency = ModelResidency(
        "other",
        1_000,
        ResidencyState.READY,
        loaded_at=1,
    )
    other_node = allocator_node("other-node", residency=other_residency)
    result = controller.tick(
        (
            replace(qwen_node, last_heartbeat=12),
            replace(other_node, last_heartbeat=12),
        ),
        now=12,
    )
    assert any(item.code == "minimum_residency" for item in result.deferred)
    assert controller.status(now=12)["withdrawn_destructive"]
    other_drain = controller.commands_for("other-node", now=12)[0]
    assert other_drain.kind == ActionKind.DRAIN
    controller.acknowledge(
        "other-node",
        other_drain.action_id,
        MutationStatus.SUCCEEDED,
        now=12.5,
    )

    restored = AllocatorController(state_path=path, max_history=1)
    restored_status = restored.status(now=13)
    assert [item["action_id"] for item in restored_status["withdrawn_destructive"]] == [
        withdrawn.action_id
    ]
    assert any(
        item["action_id"] == withdrawn.action_id
        and item["status"] == MutationStatus.CANCELLED.value
        for item in restored_status["history"]
    )

    # An authenticated terminal receipt settles the withdrawn command and permits a new drain.
    restored.acknowledge(
        "qwen-node",
        withdrawn.action_id,
        MutationStatus.SUCCEEDED,
        now=13,
    )
    assert restored.status(now=13)["withdrawn_destructive"] == []
    restored.put_profile(retiring)
    restored.tick((replace(qwen_node, last_heartbeat=14),), now=14)
    replacement = restored.commands_for("qwen-node", now=14)
    assert len(replacement) == 1
    assert replacement[0].kind == ActionKind.DRAIN
    assert replacement[0].action_id != withdrawn.action_id


def test_withdrawn_drain_revalidates_when_destructive_outcome_is_safe_again():
    controller = automatic_controller()
    retiring = replace(
        profile(),
        min_replicas=0,
        max_replicas=0,
        min_residency_seconds=0,
        scale_down_cooldown_seconds=0,
    )
    machine = allocator_node("n", residency=ready_qwen())
    controller.put_profile(retiring)
    controller.tick((machine,), now=10)
    withdrawn = controller.commands_for("n", now=10)[0]

    controller.put_profile(replace(retiring, min_residency_seconds=100))
    controller.tick((replace(machine, last_heartbeat=11),), now=11)
    assert controller.status(now=11)["withdrawn_destructive"]

    controller.put_profile(retiring)
    # The pair is undesired again and every current destructive guard accepts the old outcome.
    # Revalidate it: otherwise READY can never reach DRAINING because this uncertainty fence blocks
    # the very replacement command that would establish that postcondition.
    controller.tick((replace(machine, last_heartbeat=12),), now=12)
    assert controller.status(now=12)["withdrawn_destructive"] == []
    replacement = controller.commands_for("n", now=12)
    assert len(replacement) == 1
    assert replacement[0].kind == ActionKind.DRAIN
    assert replacement[0].action_id != withdrawn.action_id

    draining = replace(ready_qwen(), state=ResidencyState.DRAINING)
    controller.acknowledge(
        "n",
        replacement[0].action_id,
        MutationStatus.SUCCEEDED,
        now=12.5,
    )
    controller.tick(
        (allocator_node("n", residency=draining, heartbeat=13),),
        now=13,
    )
    assert controller.status(now=13)["withdrawn_destructive"] == []
    unload = controller.commands_for("n", now=13)
    assert len(unload) == 1
    assert unload[0].kind == ActionKind.UNLOAD
    assert unload[0].action_id not in (withdrawn.action_id, replacement[0].action_id)


def test_membership_recovery_grace_rebases_after_wall_clock_rollback(tmp_path):
    path = tmp_path / "allocator.json"
    controller = AllocatorController(
        mode=AllocatorMode.AUTOMATIC,
        state_path=path,
        membership_recovery_grace_seconds=30,
        reconcile_policy=ReconcilePolicy(max_concurrent_mutations=1),
    )
    controller.put_profile(profile())
    controller.tick([node(cached=True)], now=10)
    old = controller.commands_for("n", now=10)[0]

    restored = AllocatorController(state_path=path)
    restored.tick([], now=10_000)
    recovered = node(cached=True, node_id="new", heartbeat=100)
    rolled_back = restored.tick((recovered,), now=100)
    assert restored.commands_for("n", now=100) == (old,)
    assert restored.commands_for("new", now=100) == ()
    assert any(item.code == "global_mutation_limit" for item in rolled_back.deferred)

    restored.tick((replace(recovered, last_heartbeat=129.999),), now=129.999)
    assert restored.commands_for("n", now=129.999) == (old,)
    restored.tick((replace(recovered, last_heartbeat=130),), now=130)
    assert restored.commands_for("n", now=130) == ()
    replacement = restored.commands_for("new", now=130)
    assert len(replacement) == 1
    assert replacement[0].kind == ActionKind.WARM
