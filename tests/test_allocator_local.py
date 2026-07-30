from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from shared.allocator.local import (
    AdmissionDecision,
    HostPolicy,
    LocalAllocatorState,
    LocalHostProtectionLoop,
    LocalOverride,
    UnknownSignalBehavior,
    evaluate_host,
)
from shared.allocator.models import NodeState
from shared.system import hostsignals
from shared.system.hostsignals import HostSignalCollector, HostSignals, ThermalState


def signals(timestamp: float = 100.0, **changes: object) -> HostSignals:
    values: dict[str, object] = {
        "timestamp": timestamp,
        "battery_percent": 80.0,
        "on_battery": False,
        "battery_charging": False,
        "idle_seconds": 600.0,
        "user_active": False,
        "thermal_state": ThermalState.NOMINAL,
        "temperature_celsius": 45.0,
        "gpu_utilization_percent": 10.0,
        "gpu_memory_percent": 20.0,
        "cpu_utilization_percent": 15.0,
        "load_average_1m": 0.5,
        "load_per_cpu": 0.1,
        "memory_percent": 30.0,
        "network_available": True,
    }
    values.update(changes)
    return HostSignals(**values)  # type: ignore[arg-type]


def short_policy(**changes: object) -> HostPolicy:
    values: dict[str, object] = {
        "activity_debounce_seconds": 10.0,
        "activity_recovery_seconds": 20.0,
        "thermal_debounce_seconds": 5.0,
        "thermal_recovery_seconds": 20.0,
        "drain_grace_seconds": 10.0,
        "recovery_cooldown_seconds": 15.0,
    }
    values.update(changes)
    return HostPolicy(**values)  # type: ignore[arg-type]


def advance(
    prior: AdmissionDecision,
    timestamp: float,
    policy: HostPolicy,
    **changes: object,
) -> AdmissionDecision:
    return evaluate_host(signals(timestamp, **changes), policy, prior.next_state)


def test_host_signals_are_immutable_and_json_round_trip() -> None:
    snapshot = signals(123.25, collector_errors=("thermal:Timeout",))

    with pytest.raises(FrozenInstanceError):
        snapshot.memory_percent = 99.0  # type: ignore[misc]

    encoded = json.loads(json.dumps(snapshot.to_dict()))
    assert HostSignals.from_dict(encoded) == snapshot
    assert encoded["thermal_state"] == "nominal"
    assert encoded["schema_version"] == 1


def test_host_signals_from_dict_degrades_malformed_values_to_unknown() -> None:
    snapshot = HostSignals.from_dict(
        {
            "timestamp": "not-a-time",
            "battery_percent": 101,
            "on_battery": "yes",
            "idle_seconds": -1,
            "thermal_state": "melting",
            "gpu_utilization": float("nan"),
            "memory_pressure": "full",
            "network_available": 1,
        }
    )

    assert snapshot.timestamp == 0.0
    assert snapshot.battery_percent is None
    assert snapshot.on_battery is None
    assert snapshot.thermal_state == ThermalState.UNKNOWN
    assert snapshot.memory_percent is None
    assert "malformed:timestamp" in snapshot.collector_errors
    assert "malformed:thermal_state" in snapshot.collector_errors


def test_host_signals_reject_unknown_schema() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        HostSignals.from_dict({"schema_version": 99, "timestamp": 1})


def test_host_policy_round_trip_and_invalid_boundaries() -> None:
    policy = short_policy(unknown_signal_behavior=UnknownSignalBehavior.THROTTLE)
    assert HostPolicy.from_dict(json.loads(json.dumps(policy.to_dict()))) == policy

    with pytest.raises(ValueError, match="recover < throttle < critical"):
        HostPolicy(thermal_recover_celsius=80, thermal_throttle_celsius=80)
    with pytest.raises(ValueError, match="below critical"):
        HostPolicy(memory_throttle_percent=95, memory_critical_percent=95)
    with pytest.raises(ValueError, match="greater than zero"):
        HostPolicy(unknown_concurrency_multiplier=0)
    with pytest.raises(ValueError, match="clock_rebase_threshold_seconds"):
        HostPolicy(clock_rebase_threshold_seconds=0)


def test_all_unknown_signals_never_fail_closed_by_default() -> None:
    decision = evaluate_host(HostSignals(timestamp=1.0))

    assert decision.state == NodeState.ACCEPTING
    assert decision.accept is True
    assert decision.concurrency_multiplier == 1.0
    assert "signal_unavailable:thermal" in decision.reasons
    assert "signal_unavailable:memory" in decision.reasons


def test_unknown_signal_throttle_still_accepts_work() -> None:
    policy = HostPolicy(
        unknown_signal_behavior=UnknownSignalBehavior.THROTTLE,
        unknown_concurrency_multiplier=0.65,
        unknown_priority_multiplier=0.4,
    )

    decision = evaluate_host(HostSignals(timestamp=1.0), policy)

    assert decision.state == NodeState.THROTTLED
    assert decision.accept is True
    assert decision.concurrency_multiplier == 0.65
    assert decision.priority_multiplier == 0.4


def test_malformed_direct_telemetry_is_unknown_not_critical() -> None:
    snapshot = signals(
        memory_percent=float("inf"),
        gpu_memory_percent=-5,
        cpu_utilization_percent="busy",
        network_available="no",
    )

    decision = evaluate_host(snapshot)

    assert decision.accept is True
    assert decision.state == NodeState.ACCEPTING
    assert "telemetry_malformed:memory_percent" in decision.reasons
    assert "telemetry_malformed:network_available" in decision.reasons


@pytest.mark.parametrize(
    ("memory", "expected"),
    [(84.999, NodeState.ACCEPTING), (85.0, NodeState.THROTTLED), (95.0, NodeState.UNHEALTHY)],
)
def test_system_memory_thresholds_are_inclusive(memory: float, expected: NodeState) -> None:
    decision = evaluate_host(signals(memory_percent=memory))
    assert decision.state == expected
    assert decision.accept is (expected != NodeState.UNHEALTHY)


@pytest.mark.parametrize(
    ("gpu_memory", "expected"),
    [(91.999, NodeState.ACCEPTING), (92.0, NodeState.THROTTLED), (98.0, NodeState.UNHEALTHY)],
)
def test_gpu_memory_thresholds_are_inclusive(gpu_memory: float, expected: NodeState) -> None:
    decision = evaluate_host(signals(gpu_memory_percent=gpu_memory))
    assert decision.state == expected


@pytest.mark.parametrize(
    "snapshot",
    [
        signals(thermal_state=ThermalState.CRITICAL, temperature_celsius=None),
        signals(thermal_state=ThermalState.UNKNOWN, temperature_celsius=95.0),
    ],
)
def test_confirmed_critical_heat_is_immediately_unhealthy(snapshot: HostSignals) -> None:
    policy = short_policy(thermal_debounce_seconds=300)
    decision = evaluate_host(snapshot, policy)

    assert decision.state == NodeState.UNHEALTHY
    assert decision.accept is False
    assert "thermal_critical" in decision.reasons


def test_thermal_debounce_hysteresis_and_recovery_do_not_thrash() -> None:
    policy = short_policy()
    hot = evaluate_host(signals(0, temperature_celsius=85), policy)
    assert hot.state == NodeState.THROTTLED
    assert "thermal_debouncing" in hot.reasons

    latched = advance(hot, 5, policy, temperature_celsius=85)
    assert latched.next_state.thermal_latched is True
    assert "thermal_hot" in latched.reasons

    # Inside the 72-80 C hysteresis band, the latch neither clears nor restarts.
    neutral = advance(latched, 100, policy, temperature_celsius=75)
    assert neutral.state == NodeState.THROTTLED
    assert neutral.next_state.thermal_latched is True

    cooling = advance(neutral, 101, policy, temperature_celsius=72)
    still_latched = advance(cooling, 120.999, policy, temperature_celsius=72)
    assert still_latched.next_state.thermal_latched is True

    unlatched = advance(still_latched, 121, policy, temperature_celsius=72)
    assert unlatched.next_state.thermal_latched is False
    assert unlatched.state == NodeState.THROTTLED  # generic recovery cooldown starts now

    recovered = advance(unlatched, 136, policy, temperature_celsius=72)
    assert recovered.state == NodeState.ACCEPTING


def test_user_activity_debounces_then_drains_then_pauses_at_boundaries() -> None:
    policy = short_policy()
    first = evaluate_host(signals(0, user_active=True, idle_seconds=0), policy)
    assert first.state == NodeState.THROTTLED

    before_debounce = advance(first, 9.999, policy, user_active=True, idle_seconds=0)
    assert before_debounce.state == NodeState.THROTTLED

    draining = advance(before_debounce, 10, policy, user_active=True, idle_seconds=0)
    assert draining.state == NodeState.DRAINING
    assert draining.accept is False
    assert draining.retry_after_seconds == 10

    before_grace = advance(draining, 19.999, policy, user_active=True, idle_seconds=0)
    assert before_grace.state == NodeState.DRAINING

    paused = advance(before_grace, 20, policy, user_active=True, idle_seconds=0)
    assert paused.state == NodeState.PAUSED


def test_brief_activity_flap_is_held_by_recovery_cooldown() -> None:
    policy = short_policy(activity_debounce_seconds=10, recovery_cooldown_seconds=15)
    active = evaluate_host(signals(10, user_active=True, idle_seconds=0), policy)
    cleared = advance(active, 11, policy, user_active=False, idle_seconds=600)
    assert cleared.state == NodeState.THROTTLED
    assert "recovery_cooldown" in cleared.reasons

    almost = advance(cleared, 25.999, policy, user_active=False, idle_seconds=600)
    assert almost.state == NodeState.THROTTLED
    recovered = advance(almost, 26, policy, user_active=False, idle_seconds=600)
    assert recovered.state == NodeState.ACCEPTING


def test_idle_boundary_counts_as_active() -> None:
    policy = short_policy(activity_debounce_seconds=0, user_active_idle_seconds=60)
    decision = evaluate_host(signals(user_active=None, idle_seconds=60), policy)
    assert decision.state == NodeState.DRAINING


def test_conflicting_activity_signals_choose_host_safe_value() -> None:
    policy = short_policy(activity_debounce_seconds=0)
    decision = evaluate_host(signals(user_active=False, idle_seconds=0), policy)
    assert decision.state == NodeState.DRAINING
    assert "telemetry_conflict:user_activity" in decision.reasons


@pytest.mark.parametrize(
    ("percent", "on_battery", "expected"),
    [
        (30.0, True, NodeState.THROTTLED),
        (12.0, True, NodeState.DRAINING),
        (1.0, False, NodeState.ACCEPTING),
    ],
)
def test_battery_protection_only_applies_while_on_battery(
    percent: float, on_battery: bool, expected: NodeState
) -> None:
    decision = evaluate_host(
        signals(battery_percent=percent, on_battery=on_battery), short_policy()
    )
    assert decision.state == expected


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"cpu_utilization_percent": 90.0}, "cpu_busy"),
        ({"load_per_cpu": 1.25}, "load_high"),
    ],
)
def test_cpu_and_load_boundaries_throttle(changes: dict[str, float], reason: str) -> None:
    decision = evaluate_host(signals(**changes))
    assert decision.state == NodeState.THROTTLED
    assert reason in decision.reasons


def test_confirmed_network_loss_is_unhealthy_but_unknown_network_is_not() -> None:
    lost = evaluate_host(signals(network_available=False))
    unknown = evaluate_host(signals(network_available=None))

    assert lost.state == NodeState.UNHEALTHY
    assert unknown.state == NodeState.ACCEPTING
    assert "signal_unavailable:network" in unknown.reasons


def test_critical_memory_recovery_obeys_exact_cooldown_boundary() -> None:
    policy = short_policy(recovery_cooldown_seconds=15)
    unhealthy = evaluate_host(signals(100, memory_percent=95), policy)
    starting = advance(unhealthy, 101, policy, memory_percent=30)
    assert starting.state == NodeState.UNHEALTHY

    almost = advance(starting, 115.999, policy, memory_percent=30)
    assert almost.state == NodeState.UNHEALTHY
    recovered = advance(almost, 116, policy, memory_percent=30)
    assert recovered.state == NodeState.ACCEPTING


def test_clock_regression_does_not_advance_drain_or_recovery_timers() -> None:
    policy = short_policy(activity_debounce_seconds=0, drain_grace_seconds=10)
    draining = evaluate_host(signals(100, user_active=True, idle_seconds=0), policy)
    assert draining.state == NodeState.DRAINING

    regressed = advance(draining, 90, policy, user_active=True, idle_seconds=0)
    assert regressed.evaluated_at == 100
    assert regressed.state == NodeState.DRAINING
    assert regressed.retry_after_seconds == 10
    assert "clock_regression" in regressed.reasons

    paused = advance(regressed, 110, policy, user_active=True, idle_seconds=0)
    assert paused.state == NodeState.PAUSED


def test_far_future_sample_cannot_freeze_activity_debounce_after_clock_correction() -> None:
    policy = short_policy(clock_rebase_threshold_seconds=30)
    future = evaluate_host(signals(10_000), policy)

    starting = advance(future, 100, policy, user_active=True, idle_seconds=0)
    almost = advance(starting, 109.999, policy, user_active=True, idle_seconds=0)
    draining = advance(almost, 110, policy, user_active=True, idle_seconds=0)
    paused = advance(draining, 120, policy, user_active=True, idle_seconds=0)

    assert "clock_epoch_rebased" in starting.reasons
    assert starting.evaluated_at == 100
    assert starting.next_state.activity_active_since == 100
    assert starting.state == NodeState.THROTTLED
    assert almost.state == NodeState.THROTTLED
    assert draining.state == NodeState.DRAINING
    assert paused.state == NodeState.PAUSED


def test_far_forward_clock_jump_cannot_instantly_satisfy_existing_debounce() -> None:
    policy = short_policy(clock_rebase_threshold_seconds=30)
    starting = evaluate_host(
        signals(100, user_active=True, idle_seconds=0),
        policy,
    )

    jumped = advance(starting, 10_000, policy, user_active=True, idle_seconds=0)
    draining = advance(jumped, 10_010, policy, user_active=True, idle_seconds=0)

    assert "clock_epoch_rebased" in jumped.reasons
    assert jumped.state == NodeState.THROTTLED
    assert jumped.next_state.activity_active_since == 10_000
    assert draining.state == NodeState.DRAINING


def test_clock_epoch_jump_cannot_expire_or_resurrect_a_timed_local_quarantine() -> None:
    policy = short_policy(
        clock_rebase_threshold_seconds=30,
        recovery_cooldown_seconds=0,
    )
    override = LocalOverride(
        NodeState.QUARANTINED,
        "incident",
        expires_at=200,
    )
    initial = evaluate_host(signals(100), policy, override=override)

    future = evaluate_host(
        signals(10_000),
        policy,
        initial.next_state,
        override=override,
    )
    assert future.state == NodeState.QUARANTINED
    assert future.retry_after_seconds == 100
    assert "clock_epoch_rebased" in future.reasons
    assert future.next_state.override_effective_expires_at == 10_100

    # The adjusted deadline is durable, and correcting the clock rebases it back without either
    # releasing the fence early or reactivating an already-expired absolute deadline.
    persisted = LocalAllocatorState.from_dict(future.next_state.to_dict())
    corrected = evaluate_host(
        signals(100),
        policy,
        persisted,
        override=override,
    )
    assert corrected.state == NodeState.QUARANTINED
    assert corrected.next_state.override_effective_expires_at == 200

    current = corrected
    for timestamp in (130, 160, 190):
        current = evaluate_host(
            signals(timestamp),
            policy,
            current.next_state,
            override=override,
        )
        assert current.state == NodeState.QUARANTINED
    expired = evaluate_host(
        signals(200),
        policy,
        current.next_state,
        override=override,
    )
    assert expired.state == NodeState.ACCEPTING
    assert "local_override_expired" in expired.reasons


def test_malformed_clock_is_clamped_to_prior_timestamp() -> None:
    prior = evaluate_host(signals(50))
    malformed = signals(float("nan"))
    decision = evaluate_host(malformed, previous_state=prior.next_state)

    assert decision.evaluated_at == 50
    assert "telemetry_malformed:timestamp" in decision.reasons


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        (LocalOverride.drain("maintenance"), NodeState.DRAINING),
        (LocalOverride.pause("lunch"), NodeState.PAUSED),
        (LocalOverride.quarantine("investigate"), NodeState.QUARANTINED),
    ],
)
def test_manual_overrides_apply_immediately(
    override: LocalOverride, expected: NodeState
) -> None:
    decision = evaluate_host(signals(), override=override)
    assert decision.state == expected
    assert decision.accept is False
    assert any(reason.startswith("local_override:") for reason in decision.reasons)


def test_local_override_outranks_more_restrictive_global_desired_state() -> None:
    decision = evaluate_host(
        signals(),
        override=LocalOverride.drain("local maintenance"),
        global_desired_state=NodeState.QUARANTINED,
    )

    assert decision.state == NodeState.DRAINING
    assert not any(reason.startswith("global_desired:") for reason in decision.reasons)


def test_confirmed_critical_safety_cannot_be_relaxed_by_manual_pause() -> None:
    decision = evaluate_host(
        signals(memory_percent=95),
        override=LocalOverride.pause("ignore scheduler"),
        global_desired_state=NodeState.ACCEPTING,
    )

    assert decision.state == NodeState.UNHEALTHY
    assert "memory_critical" in decision.reasons


def test_expired_override_is_ignored() -> None:
    decision = evaluate_host(
        signals(100),
        override=LocalOverride(NodeState.PAUSED, "expired", expires_at=100),
    )
    assert decision.state == NodeState.ACCEPTING
    assert "local_override_expired" in decision.reasons


def test_global_desired_state_applies_without_local_override() -> None:
    decision = evaluate_host(signals(), global_desired_state=NodeState.PAUSED)
    assert decision.state == NodeState.PAUSED
    assert "global_desired:paused" in decision.reasons


def test_invalid_global_state_does_not_fail_closed() -> None:
    decision = evaluate_host(signals(), global_desired_state="bogus")
    assert decision.state == NodeState.ACCEPTING
    assert "global_desired_state_invalid" in decision.reasons


def test_manual_indefinite_pause_has_no_retry_hint() -> None:
    decision = evaluate_host(signals(), override=LocalOverride.pause())
    assert decision.retry_after_seconds is None


def test_state_decision_and_override_json_round_trip() -> None:
    decision = evaluate_host(
        signals(),
        short_policy(),
        override=LocalOverride.drain("deploy"),
    )
    encoded = json.loads(json.dumps(decision.to_dict()))
    restored = AdmissionDecision.from_dict(encoded)

    assert restored == decision
    assert LocalAllocatorState.from_dict(decision.next_state.to_dict()) == decision.next_state
    override = LocalOverride(NodeState.PAUSED, "quiet", expires_at=200)
    assert LocalOverride.from_dict(override.to_dict()) == override


def test_serialization_rejects_unknown_state_schema() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        LocalAllocatorState.from_dict({"schema_version": 2})
    with pytest.raises(ValueError, match="unsupported"):
        AdmissionDecision.from_dict({"schema_version": 2})


def test_stateful_loop_matches_pure_evaluator_and_can_reset() -> None:
    policy = short_policy(activity_debounce_seconds=0)
    loop = LocalHostProtectionLoop(policy)
    first = loop.evaluate(signals(0, user_active=True, idle_seconds=0))
    second = loop.evaluate(signals(10, user_active=True, idle_seconds=0))

    assert first.state == NodeState.DRAINING
    assert second.state == NodeState.PAUSED
    assert loop.state == second.next_state
    loop.reset()
    assert loop.state == LocalAllocatorState()


def test_unavailable_signal_eventually_releases_a_prior_activity_latch() -> None:
    policy = short_policy(
        activity_debounce_seconds=0,
        activity_recovery_seconds=10,
        drain_grace_seconds=0,
        recovery_cooldown_seconds=5,
    )
    paused = evaluate_host(signals(0, user_active=True, idle_seconds=0), policy)
    assert paused.state == NodeState.PAUSED

    unknown = advance(paused, 1, policy, user_active=None, idle_seconds=None)
    assert unknown.state == NodeState.PAUSED
    latch_released = advance(unknown, 11, policy, user_active=None, idle_seconds=None)
    assert latch_released.next_state.activity_latched is False
    recovered = advance(latch_released, 16, policy, user_active=None, idle_seconds=None)
    assert recovered.state == NodeState.ACCEPTING


def test_collector_produces_coherent_snapshot_and_cpu_delta(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter(((100, 1_000), (120, 1_100)))
    monkeypatch.setattr(hostsignals, "_battery_snapshot", lambda _timeout: (50.0, True, False))
    monkeypatch.setattr(hostsignals, "_idle_seconds", lambda _timeout: 12.0)
    monkeypatch.setattr(
        hostsignals,
        "_thermal_snapshot",
        lambda _timeout: (ThermalState.FAIR, 70.0),
    )
    monkeypatch.setattr(
        hostsignals.gpu,
        "load_snapshot",
        lambda timeout: {
            "gpu_util": 40.0,
            "memory_total_mb": 1_000.0,
            "memory_used_mb": 250.0,
        },
    )
    monkeypatch.setattr(
        hostsignals.host,
        "gather",
        lambda: SimpleNamespace(memory_percent=55.0),
    )
    monkeypatch.setattr(hostsignals.os, "getloadavg", lambda: (2.0, 1.0, 1.0))
    monkeypatch.setattr(hostsignals.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(hostsignals, "_cpu_ticks", lambda: next(ticks))
    monkeypatch.setattr(hostsignals, "_network_available", lambda _timeout: True)
    monkeypatch.setattr(hostsignals.platform, "system", lambda: "Linux")
    collector = HostSignalCollector(clock=lambda: 123.0, user_active_window_seconds=60)

    first = collector.collect()
    second = collector.collect()

    assert first.cpu_utilization_percent is None
    assert second.cpu_utilization_percent == pytest.approx(80.0)
    assert second.timestamp == 123.0
    assert second.user_active is True
    assert second.gpu_memory_percent == 25.0
    assert second.load_per_cpu == 0.5
    assert second.network_available is True


def test_collector_is_best_effort_when_every_probe_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("sensor exploded")

    monkeypatch.setattr(hostsignals, "_battery_snapshot", broken)
    monkeypatch.setattr(hostsignals, "_idle_seconds", broken)
    monkeypatch.setattr(hostsignals, "_thermal_snapshot", broken)
    monkeypatch.setattr(hostsignals.gpu, "load_snapshot", broken)
    monkeypatch.setattr(hostsignals.host, "gather", broken)
    monkeypatch.setattr(hostsignals, "_cpu_ticks", broken)
    monkeypatch.setattr(hostsignals, "_network_available", broken)
    collector = HostSignalCollector(clock=broken)

    snapshot = collector.collect()

    assert snapshot.timestamp == 0.0
    assert snapshot.memory_percent is None
    assert snapshot.network_available is None
    assert {item.split(":", 1)[0] for item in snapshot.collector_errors} >= {
        "clock",
        "battery",
        "idle",
        "thermal",
        "gpu",
        "memory",
        "cpu",
        "network",
    }


def test_apple_placeholder_gpu_values_are_not_treated_as_measurements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hostsignals, "_battery_snapshot", lambda _timeout: (80, False, False))
    monkeypatch.setattr(hostsignals, "_idle_seconds", lambda _timeout: None)
    monkeypatch.setattr(
        hostsignals,
        "_thermal_snapshot",
        lambda _timeout: (ThermalState.UNKNOWN, None),
    )
    monkeypatch.setattr(
        hostsignals.gpu,
        "load_snapshot",
        lambda timeout: {
            "gpu_count": 1.0,
            "gpu_util": 0.0,
            "memory_total_mb": 64_000.0,
            "memory_used_mb": 0.0,
        },
    )
    monkeypatch.setattr(
        hostsignals.host,
        "gather",
        lambda: SimpleNamespace(memory_percent=10.0),
    )
    monkeypatch.setattr(hostsignals, "_cpu_ticks", lambda: None)
    monkeypatch.setattr(hostsignals, "_network_available", lambda _timeout: True)
    monkeypatch.setattr(hostsignals.platform, "system", lambda: "Darwin")
    collector = HostSignalCollector(clock=lambda: 1.0)

    snapshot = collector.collect()

    assert snapshot.gpu_utilization_percent is None
    assert snapshot.gpu_memory_percent is None
