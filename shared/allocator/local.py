"""Deterministic local host-protection policy for Grid nodes.

This module is intentionally a policy boundary, not an actuator.  :func:`evaluate_host` consumes an
immutable telemetry snapshot and prior evaluator state, then returns an admission decision containing
the next state.  Provider code may publish or apply that decision, but this module never starts,
stops, or signals a workload itself.
"""

from __future__ import annotations

import hashlib
import math
import threading
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from typing import Any

from shared.allocator.models import NodeState
from shared.system.hostsignals import HostSignals, ThermalState

LOCAL_ALLOCATOR_SCHEMA_VERSION = 1


class UnknownSignalBehavior(StrEnum):
    """Policy for unavailable or malformed telemetry.

    Neither behavior rejects admission: absence of a sensor is never, by itself, proof that a host
    is unsafe.  ``THROTTLE`` is useful for unattended or heterogeneous fleets that prefer reduced
    concurrency until safety telemetry is established.
    """

    IGNORE = "ignore"
    THROTTLE = "throttle"


_STATE_RANK = {
    NodeState.ACCEPTING: 0,
    NodeState.THROTTLED: 1,
    NodeState.DRAINING: 2,
    NodeState.PAUSED: 3,
    NodeState.UNHEALTHY: 4,
    NodeState.QUARANTINED: 5,
}


def _finite(value: object, *, minimum: float | None = None) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        return None
    return number


def _unique(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _node_state(value: object, default: NodeState = NodeState.ACCEPTING) -> NodeState:
    try:
        return value if isinstance(value, NodeState) else NodeState(str(value).lower())
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class HostPolicy:
    """Configurable local safety thresholds with conservative desktop defaults."""

    # A host belongs to its local user first.  A short input burst throttles immediately; sustained
    # activity drains new work, then pauses after the grace period.
    pause_for_user_activity: bool = True
    user_active_idle_seconds: float = 60.0
    activity_debounce_seconds: float = 10.0
    activity_recovery_seconds: float = 60.0

    # Temperature state and direct temperature are independent: either can protect the host.
    thermal_throttle_celsius: float = 80.0
    thermal_critical_celsius: float = 95.0
    thermal_recover_celsius: float = 72.0
    thermal_debounce_seconds: float = 5.0
    thermal_recovery_seconds: float = 60.0

    memory_throttle_percent: float = 85.0
    memory_critical_percent: float = 95.0
    gpu_memory_throttle_percent: float = 92.0
    gpu_memory_critical_percent: float = 98.0
    cpu_throttle_percent: float = 90.0
    load_per_cpu_throttle: float = 1.25

    battery_throttle_percent: float = 30.0
    battery_pause_percent: float = 12.0
    require_network: bool = True

    drain_grace_seconds: float = 30.0
    recovery_cooldown_seconds: float = 60.0
    throttled_concurrency_multiplier: float = 0.5
    throttled_priority_multiplier: float = 0.5
    retry_after_seconds: float = 30.0
    # Wall time is persisted across daemon restarts, but a broken RTC/NTP sample must not freeze or
    # instantly satisfy every debounce timer. Larger discontinuities start a new clock epoch while
    # preserving already-observed elapsed durations.
    clock_rebase_threshold_seconds: float = 300.0

    unknown_signal_behavior: UnknownSignalBehavior = UnknownSignalBehavior.IGNORE
    unknown_concurrency_multiplier: float = 0.75
    unknown_priority_multiplier: float = 0.75

    def __post_init__(self) -> None:
        if not isinstance(self.unknown_signal_behavior, UnknownSignalBehavior):
            object.__setattr__(
                self,
                "unknown_signal_behavior",
                UnknownSignalBehavior(self.unknown_signal_behavior),
            )
        durations = (
            "user_active_idle_seconds",
            "activity_debounce_seconds",
            "activity_recovery_seconds",
            "thermal_debounce_seconds",
            "thermal_recovery_seconds",
            "drain_grace_seconds",
            "recovery_cooldown_seconds",
            "retry_after_seconds",
            "clock_rebase_threshold_seconds",
        )
        for name in durations:
            value = _finite(getattr(self, name), minimum=0.0)
            if value is None:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.clock_rebase_threshold_seconds == 0:
            raise ValueError("clock_rebase_threshold_seconds must be greater than zero")

        percentages = (
            "memory_throttle_percent",
            "memory_critical_percent",
            "gpu_memory_throttle_percent",
            "gpu_memory_critical_percent",
            "cpu_throttle_percent",
            "battery_throttle_percent",
            "battery_pause_percent",
        )
        for name in percentages:
            value = _finite(getattr(self, name), minimum=0.0)
            if value is None or value > 100.0:
                raise ValueError(f"{name} must be in [0, 100]")

        temperatures = (
            self.thermal_recover_celsius,
            self.thermal_throttle_celsius,
            self.thermal_critical_celsius,
        )
        if any(_finite(value) is None for value in temperatures):
            raise ValueError("thermal thresholds must be finite")
        if not temperatures[0] < temperatures[1] < temperatures[2]:
            raise ValueError("thermal thresholds must satisfy recover < throttle < critical")
        if self.memory_throttle_percent >= self.memory_critical_percent:
            raise ValueError("memory throttle threshold must be below critical")
        if self.gpu_memory_throttle_percent >= self.gpu_memory_critical_percent:
            raise ValueError("GPU memory throttle threshold must be below critical")
        if self.battery_pause_percent >= self.battery_throttle_percent:
            raise ValueError("battery pause threshold must be below throttle")
        if _finite(self.load_per_cpu_throttle, minimum=0.0) is None:
            raise ValueError("load_per_cpu_throttle must be finite and non-negative")
        for name in (
            "throttled_concurrency_multiplier",
            "throttled_priority_multiplier",
            "unknown_concurrency_multiplier",
            "unknown_priority_multiplier",
        ):
            value = _finite(getattr(self, name), minimum=0.0)
            if value is None or value > 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
        if self.throttled_concurrency_multiplier == 0:
            raise ValueError("throttled_concurrency_multiplier must be greater than zero")
        if self.unknown_concurrency_multiplier == 0:
            raise ValueError("unknown_concurrency_multiplier must be greater than zero")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["schema_version"] = LOCAL_ALLOCATOR_SCHEMA_VERSION
        data["unknown_signal_behavior"] = self.unknown_signal_behavior.value
        return data

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> HostPolicy:
        try:
            version = int(value.get("schema_version", LOCAL_ALLOCATOR_SCHEMA_VERSION))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("invalid host-policy schema version") from exc
        if version != LOCAL_ALLOCATOR_SCHEMA_VERSION:
            raise ValueError("unsupported host-policy schema")
        known = set(cls.__dataclass_fields__)
        fields = {name: raw for name, raw in value.items() if name in known}
        return cls(**fields)


DEFAULT_HOST_POLICY = HostPolicy()


@dataclass(frozen=True, slots=True)
class LocalOverride:
    """Explicit local operator control, which always outranks global desired state."""

    state: NodeState
    reason: str = "manual"
    expires_at: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, NodeState):
            object.__setattr__(self, "state", NodeState(self.state))
        if self.state not in (NodeState.DRAINING, NodeState.PAUSED, NodeState.QUARANTINED):
            raise ValueError("local override must be draining, paused, or quarantined")
        reason = str(self.reason).strip()
        object.__setattr__(self, "reason", reason or "manual")
        if self.expires_at is not None and _finite(self.expires_at, minimum=0.0) is None:
            raise ValueError("expires_at must be finite and non-negative")

    @classmethod
    def drain(cls, reason: str = "manual") -> LocalOverride:
        return cls(NodeState.DRAINING, reason)

    @classmethod
    def pause(cls, reason: str = "manual") -> LocalOverride:
        return cls(NodeState.PAUSED, reason)

    @classmethod
    def quarantine(cls, reason: str = "manual") -> LocalOverride:
        return cls(NodeState.QUARANTINED, reason)

    def active_at(self, timestamp: float) -> bool:
        return self.expires_at is None or timestamp < self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": LOCAL_ALLOCATOR_SCHEMA_VERSION,
            "state": self.state.value,
            "reason": self.reason,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> LocalOverride:
        if int(value.get("schema_version", LOCAL_ALLOCATOR_SCHEMA_VERSION)) != (
            LOCAL_ALLOCATOR_SCHEMA_VERSION
        ):
            raise ValueError("unsupported local-override schema")
        return cls(
            state=NodeState(value["state"]),
            reason=str(value.get("reason") or "manual"),
            expires_at=(
                None if value.get("expires_at") is None else float(value["expires_at"])
            ),
        )


@dataclass(frozen=True, slots=True)
class LocalAllocatorState:
    """Serializable memory required by debounce, drain, and recovery transitions."""

    lifecycle: NodeState = NodeState.ACCEPTING
    last_timestamp: float | None = None
    state_since: float | None = None
    activity_active_since: float | None = None
    activity_clear_since: float | None = None
    activity_latched: bool = False
    thermal_hot_since: float | None = None
    thermal_clear_since: float | None = None
    thermal_latched: bool = False
    drain_started_at: float | None = None
    drain_target: NodeState | None = None
    recovery_started_at: float | None = None
    # A timed local override is authored in wall-clock time, but a broken clock epoch must not
    # release or resurrect it. The token binds the adjusted deadline to the exact operator intent.
    override_token: str = ""
    override_effective_expires_at: float | None = None
    generation: int = 0
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.lifecycle, NodeState):
            object.__setattr__(self, "lifecycle", NodeState(self.lifecycle))
        if self.drain_target is not None and not isinstance(self.drain_target, NodeState):
            object.__setattr__(self, "drain_target", NodeState(self.drain_target))
        if self.drain_target not in (None, NodeState.PAUSED):
            raise ValueError("drain_target may only be paused or None")
        for name in (
            "last_timestamp",
            "state_since",
            "activity_active_since",
            "activity_clear_since",
            "thermal_hot_since",
            "thermal_clear_since",
            "drain_started_at",
            "recovery_started_at",
            "override_effective_expires_at",
        ):
            value = getattr(self, name)
            if value is not None and _finite(value, minimum=0.0) is None:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.generation < 0:
            raise ValueError("generation must be non-negative")
        if self.override_token and (
            len(self.override_token) != 64
            or any(character not in "0123456789abcdef" for character in self.override_token)
        ):
            raise ValueError("override_token must be an SHA-256 digest or empty")
        if self.override_effective_expires_at is not None and not self.override_token:
            raise ValueError("an effective override expiry requires an override token")
        object.__setattr__(self, "reasons", _unique(list(self.reasons)))

    @property
    def state(self) -> NodeState:
        """Compatibility alias for callers that use state rather than lifecycle."""

        return self.lifecycle

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["schema_version"] = LOCAL_ALLOCATOR_SCHEMA_VERSION
        data["lifecycle"] = self.lifecycle.value
        data["drain_target"] = self.drain_target.value if self.drain_target else None
        data["reasons"] = list(self.reasons)
        return data

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> LocalAllocatorState:
        try:
            version = int(value.get("schema_version", LOCAL_ALLOCATOR_SCHEMA_VERSION))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("invalid local-state schema version") from exc
        if version != LOCAL_ALLOCATOR_SCHEMA_VERSION:
            raise ValueError("unsupported local-state schema")
        known = set(cls.__dataclass_fields__)
        fields = {name: raw for name, raw in value.items() if name in known}
        fields["reasons"] = tuple(fields.get("reasons") or ())
        return cls(**fields)


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """Local lifecycle and request-admission result for one telemetry snapshot."""

    state: NodeState
    accept: bool
    concurrency_multiplier: float
    priority_multiplier: float
    reasons: tuple[str, ...]
    retry_after_seconds: float | None
    evaluated_at: float
    next_state: LocalAllocatorState

    def __post_init__(self) -> None:
        if not isinstance(self.state, NodeState):
            object.__setattr__(self, "state", NodeState(self.state))
        for name in ("concurrency_multiplier", "priority_multiplier"):
            value = _finite(getattr(self, name), minimum=0.0)
            if value is None or value > 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
        if (
            self.retry_after_seconds is not None
            and _finite(self.retry_after_seconds, minimum=0.0) is None
        ):
            raise ValueError("retry_after_seconds must be finite and non-negative")
        if _finite(self.evaluated_at, minimum=0.0) is None:
            raise ValueError("evaluated_at must be finite and non-negative")
        if self.state != self.next_state.lifecycle:
            raise ValueError("decision and next-state lifecycles must match")
        if self.accept and self.state not in (NodeState.ACCEPTING, NodeState.THROTTLED):
            raise ValueError("only accepting or throttled states can accept work")
        if not self.accept and self.concurrency_multiplier != 0:
            raise ValueError("a rejecting decision must have zero concurrency")
        object.__setattr__(self, "reasons", _unique(list(self.reasons)))

    @property
    def accepted(self) -> bool:
        return self.accept

    @property
    def lifecycle(self) -> NodeState:
        return self.state

    @property
    def concurrency_factor(self) -> float:
        return self.concurrency_multiplier

    @property
    def priority_factor(self) -> float:
        return self.priority_multiplier

    @property
    def retry_after(self) -> float | None:
        return self.retry_after_seconds

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": LOCAL_ALLOCATOR_SCHEMA_VERSION,
            "state": self.state.value,
            "accept": self.accept,
            "concurrency_multiplier": self.concurrency_multiplier,
            "priority_multiplier": self.priority_multiplier,
            "reasons": list(self.reasons),
            "retry_after_seconds": self.retry_after_seconds,
            "evaluated_at": self.evaluated_at,
            "next_state": self.next_state.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AdmissionDecision:
        try:
            version = int(value.get("schema_version", LOCAL_ALLOCATOR_SCHEMA_VERSION))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("invalid admission-decision schema version") from exc
        if version != LOCAL_ALLOCATOR_SCHEMA_VERSION:
            raise ValueError("unsupported admission-decision schema")
        state = NodeState(value["state"])
        next_state_raw = value.get("next_state")
        if isinstance(next_state_raw, Mapping):
            next_state = LocalAllocatorState.from_dict(next_state_raw)
        else:
            evaluated_at = float(value.get("evaluated_at") or 0.0)
            next_state = LocalAllocatorState(
                lifecycle=state,
                last_timestamp=evaluated_at,
                state_since=evaluated_at,
                reasons=tuple(value.get("reasons") or ()),
            )
        return cls(
            state=state,
            accept=bool(value["accept"]),
            concurrency_multiplier=float(value["concurrency_multiplier"]),
            priority_multiplier=float(value["priority_multiplier"]),
            reasons=tuple(value.get("reasons") or ()),
            retry_after_seconds=(
                None
                if value.get("retry_after_seconds") is None
                else float(value["retry_after_seconds"])
            ),
            evaluated_at=float(value.get("evaluated_at") or 0.0),
            next_state=next_state,
        )


@dataclass(frozen=True, slots=True)
class _Latch:
    active_since: float | None
    clear_since: float | None
    latched: bool


def _elapsed(now: float, since: float | None) -> float:
    return 0.0 if since is None else max(now - since, 0.0)


def _rebase_state_clock(
    state: LocalAllocatorState,
    *,
    timestamp: float,
) -> LocalAllocatorState:
    """Move persisted timer anchors into a new wall-clock epoch without granting elapsed time."""

    if state.last_timestamp is None:
        return state
    offset = timestamp - state.last_timestamp

    def shifted(value: float | None) -> float | None:
        return None if value is None else max(0.0, value + offset)

    return replace(
        state,
        last_timestamp=timestamp,
        state_since=shifted(state.state_since),
        activity_active_since=shifted(state.activity_active_since),
        activity_clear_since=shifted(state.activity_clear_since),
        thermal_hot_since=shifted(state.thermal_hot_since),
        thermal_clear_since=shifted(state.thermal_clear_since),
        drain_started_at=shifted(state.drain_started_at),
        recovery_started_at=shifted(state.recovery_started_at),
        override_effective_expires_at=shifted(
            state.override_effective_expires_at
        ),
    )


def _activity_latch(
    observation: bool | None,
    previous: LocalAllocatorState,
    now: float,
    policy: HostPolicy,
) -> _Latch:
    if observation is True:
        active_since = previous.activity_active_since
        if active_since is None or active_since > now:
            active_since = now
        latched = previous.activity_latched or (
            _elapsed(now, active_since) >= policy.activity_debounce_seconds
        )
        return _Latch(active_since, None, latched)

    # Unknown is allowed to recover a previously latched state after the normal recovery period.
    # Holding a pause forever merely because an input API disappeared would be failing closed on an
    # unavailable signal.  THROTTLE behavior still limits admission independently below.
    clear_since = previous.activity_clear_since
    if clear_since is None or clear_since > now:
        clear_since = now
    latched = previous.activity_latched and (
        _elapsed(now, clear_since) < policy.activity_recovery_seconds
    )
    return _Latch(None, clear_since, latched)


def _thermal_latch(
    observation: str,
    previous: LocalAllocatorState,
    now: float,
    policy: HostPolicy,
) -> _Latch:
    if observation == "hot":
        hot_since = previous.thermal_hot_since
        if hot_since is None or hot_since > now:
            hot_since = now
        latched = previous.thermal_latched or (
            _elapsed(now, hot_since) >= policy.thermal_debounce_seconds
        )
        return _Latch(hot_since, None, latched)
    if observation == "neutral":
        # The temperature is between recovery and throttle boundaries: hold the latch without
        # progressing either debounce timer.  This is the actual thermal hysteresis band.
        return _Latch(previous.thermal_hot_since, None, previous.thermal_latched)

    # Both a confirmed cool reading and a newly unavailable sensor eventually release the latch.
    clear_since = previous.thermal_clear_since
    if clear_since is None or clear_since > now:
        clear_since = now
    latched = previous.thermal_latched and (
        _elapsed(now, clear_since) < policy.thermal_recovery_seconds
    )
    return _Latch(None, clear_since, latched)


class _Telemetry:
    """Defensive view of a potentially hand-constructed/malformed snapshot."""

    def __init__(self, signals: HostSignals, policy: HostPolicy) -> None:
        self.signals = signals
        self.policy = policy
        self.reasons: list[str] = []
        self.unknown: list[str] = []

    def percentage(self, name: str, category: str) -> float | None:
        raw = getattr(self.signals, name, None)
        value = _finite(raw, minimum=0.0)
        if value is None or value > 100.0:
            if raw is not None:
                self.reasons.append(f"telemetry_malformed:{name}")
            self.unknown.append(category)
            return None
        return value

    def nonnegative(self, name: str, category: str) -> float | None:
        raw = getattr(self.signals, name, None)
        value = _finite(raw, minimum=0.0)
        if value is None:
            if raw is not None:
                self.reasons.append(f"telemetry_malformed:{name}")
            self.unknown.append(category)
        return value

    def boolean(self, name: str, category: str) -> bool | None:
        raw = getattr(self.signals, name, None)
        if isinstance(raw, bool):
            return raw
        if raw is not None:
            self.reasons.append(f"telemetry_malformed:{name}")
        self.unknown.append(category)
        return None

    def mark_unknown(self, category: str) -> None:
        self.unknown.append(category)

    def mark_known(self, category: str) -> None:
        self.unknown = [item for item in self.unknown if item != category]


def _thermal_observation(
    telemetry: _Telemetry,
) -> tuple[bool, bool, str]:
    signals = telemetry.signals
    try:
        thermal_state = (
            signals.thermal_state
            if isinstance(signals.thermal_state, ThermalState)
            else ThermalState(str(signals.thermal_state).lower())
        )
    except ValueError:
        thermal_state = ThermalState.UNKNOWN
        telemetry.reasons.append("telemetry_malformed:thermal_state")
    temperature = telemetry.nonnegative("temperature_celsius", "thermal")
    state_known = thermal_state != ThermalState.UNKNOWN
    if state_known or temperature is not None:
        telemetry.mark_known("thermal")
    if not state_known and temperature is None:
        telemetry.mark_unknown("thermal")

    critical = thermal_state == ThermalState.CRITICAL or (
        temperature is not None
        and temperature >= telemetry.policy.thermal_critical_celsius
    )
    hot = thermal_state in (ThermalState.SERIOUS, ThermalState.CRITICAL) or (
        temperature is not None
        and temperature >= telemetry.policy.thermal_throttle_celsius
    )
    if hot:
        observation = "hot"
    elif temperature is not None and temperature > telemetry.policy.thermal_recover_celsius:
        observation = "neutral"
    elif (
        (state_known and thermal_state in (ThermalState.NOMINAL, ThermalState.FAIR))
        or (
            temperature is not None
            and temperature <= telemetry.policy.thermal_recover_celsius
        )
    ):
        observation = "safe"
    elif not state_known and temperature is None:
        observation = "unknown"
    else:
        observation = "neutral"
    return critical, hot, observation


def _user_activity(telemetry: _Telemetry) -> bool | None:
    raw_active = getattr(telemetry.signals, "user_active", None)
    active: bool | None
    if isinstance(raw_active, bool):
        active = raw_active
    else:
        active = None
        if raw_active is not None:
            telemetry.reasons.append("telemetry_malformed:user_active")
    idle = telemetry.nonnegative("idle_seconds", "activity")
    idle_active = (
        idle <= telemetry.policy.user_active_idle_seconds if idle is not None else None
    )
    if active is False and idle_active is True:
        telemetry.reasons.append("telemetry_conflict:user_activity")
        telemetry.mark_known("activity")
        return True
    if active is not None:
        telemetry.mark_known("activity")
        return active
    if idle_active is not None:
        telemetry.mark_known("activity")
        return idle_active
    telemetry.mark_unknown("activity")
    return None


def _more_restrictive(left: NodeState, right: NodeState) -> NodeState:
    return left if _STATE_RANK[left] >= _STATE_RANK[right] else right


def _coerce_override(
    value: LocalOverride | NodeState | str | None,
) -> tuple[LocalOverride | None, str | None]:
    if value is None:
        return None, None
    if isinstance(value, LocalOverride):
        return value, None
    try:
        return LocalOverride(NodeState(value)), None
    except (TypeError, ValueError):
        return None, "local_override_invalid"


def _override_token(override: LocalOverride) -> str:
    """Return a stable identity for one exact timed operator override."""

    expiry = "" if override.expires_at is None else float(override.expires_at).hex()
    material = f"{override.state.value}\0{override.reason}\0{expiry}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def evaluate_host(
    signals: HostSignals,
    policy: HostPolicy | None = None,
    previous_state: LocalAllocatorState | AdmissionDecision | None = None,
    override: LocalOverride | NodeState | str | None = None,
    *,
    global_desired_state: NodeState | str = NodeState.ACCEPTING,
) -> AdmissionDecision:
    """Pure local state-machine evaluation.

    Time comes only from ``signals.timestamp``. Small regressions and malformed clocks are clamped
    to the last evaluated timestamp. A larger discontinuity rebases every persisted timer anchor
    into the new wall-clock epoch, so one bad RTC/NTP sample can neither freeze nor instantly
    satisfy debounce, drain, or recovery intervals. Pass ``decision.next_state`` into the next call
    (passing the prior decision itself is accepted as a convenience).
    """

    policy = policy or DEFAULT_HOST_POLICY
    if isinstance(previous_state, AdmissionDecision):
        previous = previous_state.next_state
    else:
        previous = previous_state or LocalAllocatorState()

    reasons: list[str] = []
    raw_now = _finite(getattr(signals, "timestamp", None), minimum=0.0)
    if raw_now is None:
        reasons.append("telemetry_malformed:timestamp")
        raw_now = previous.last_timestamp if previous.last_timestamp is not None else 0.0
    if previous.last_timestamp is not None:
        clock_delta = raw_now - previous.last_timestamp
        if abs(clock_delta) > policy.clock_rebase_threshold_seconds:
            reasons.append("clock_epoch_rebased")
            previous = _rebase_state_clock(previous, timestamp=raw_now)
            now = raw_now
        elif clock_delta < 0:
            reasons.append("clock_regression")
            now = previous.last_timestamp
        else:
            now = raw_now
    else:
        now = raw_now

    telemetry = _Telemetry(signals, policy)
    activity = _user_activity(telemetry)
    thermal_critical, thermal_hot, thermal_observation = _thermal_observation(telemetry)
    activity_latch = _activity_latch(activity, previous, now, policy)
    thermal_latch = _thermal_latch(thermal_observation, previous, now, policy)

    memory = telemetry.percentage("memory_percent", "memory")
    gpu_memory = telemetry.percentage("gpu_memory_percent", "gpu_memory")
    gpu_utilization = telemetry.percentage("gpu_utilization_percent", "gpu")
    cpu_utilization = telemetry.percentage("cpu_utilization_percent", "cpu")
    load_per_cpu = telemetry.nonnegative("load_per_cpu", "cpu")
    on_battery = telemetry.boolean("on_battery", "battery")
    battery = telemetry.percentage("battery_percent", "battery")
    network = telemetry.boolean("network_available", "network")

    if cpu_utilization is not None or load_per_cpu is not None:
        telemetry.mark_known("cpu")
    if on_battery is False or (on_battery is True and battery is not None):
        telemetry.mark_known("battery")

    # A missing GPU metric can simply mean this is a CPU node.  It is recorded for observability
    # but does not become an unknown-signal throttle unless at least one GPU signal was present.
    if gpu_memory is None and gpu_utilization is None:
        telemetry.unknown = [item for item in telemetry.unknown if item not in {"gpu", "gpu_memory"}]

    target = NodeState.ACCEPTING
    safety_target = NodeState.ACCEPTING
    needs_drain_grace = False

    if thermal_critical:
        safety_target = NodeState.UNHEALTHY
        reasons.append("thermal_critical")
    if memory is not None and memory >= policy.memory_critical_percent:
        safety_target = _more_restrictive(safety_target, NodeState.UNHEALTHY)
        reasons.append("memory_critical")
    if gpu_memory is not None and gpu_memory >= policy.gpu_memory_critical_percent:
        safety_target = _more_restrictive(safety_target, NodeState.UNHEALTHY)
        reasons.append("gpu_memory_critical")
    if policy.require_network and network is False:
        safety_target = _more_restrictive(safety_target, NodeState.UNHEALTHY)
        reasons.append("network_unavailable")

    if safety_target != NodeState.UNHEALTHY:
        if policy.pause_for_user_activity and activity_latch.latched:
            safety_target = _more_restrictive(safety_target, NodeState.PAUSED)
            needs_drain_grace = True
            reasons.append("user_active")
        if on_battery is True and battery is not None and battery <= policy.battery_pause_percent:
            safety_target = _more_restrictive(safety_target, NodeState.PAUSED)
            needs_drain_grace = True
            reasons.append("battery_critical")

    throttle_reasons: list[str] = []
    if activity is True and not activity_latch.latched:
        throttle_reasons.append("user_activity_debouncing")
    if thermal_hot and not thermal_latch.latched:
        throttle_reasons.append("thermal_debouncing")
    if thermal_latch.latched:
        throttle_reasons.append("thermal_hot")
    if memory is not None and memory >= policy.memory_throttle_percent:
        throttle_reasons.append("memory_pressure")
    if gpu_memory is not None and gpu_memory >= policy.gpu_memory_throttle_percent:
        throttle_reasons.append("gpu_memory_pressure")
    if cpu_utilization is not None and cpu_utilization >= policy.cpu_throttle_percent:
        throttle_reasons.append("cpu_busy")
    if load_per_cpu is not None and load_per_cpu >= policy.load_per_cpu_throttle:
        throttle_reasons.append("load_high")
    if on_battery is True and battery is not None and battery <= policy.battery_throttle_percent:
        throttle_reasons.append("battery_low")
    if throttle_reasons and _STATE_RANK[safety_target] < _STATE_RANK[NodeState.THROTTLED]:
        safety_target = NodeState.THROTTLED
    reasons.extend(throttle_reasons)

    unknown_categories = _unique(telemetry.unknown)
    reasons.extend(f"signal_unavailable:{name}" for name in unknown_categories)
    if (
        unknown_categories
        and policy.unknown_signal_behavior == UnknownSignalBehavior.THROTTLE
        and safety_target == NodeState.ACCEPTING
    ):
        safety_target = NodeState.THROTTLED
        reasons.append("unknown_signals_throttled")
    reasons.extend(telemetry.reasons)
    reasons.extend(f"collector_error:{item}" for item in signals.collector_errors[:16])

    local_override, override_error = _coerce_override(override)
    if override_error:
        reasons.append(override_error)
    override_token = ""
    override_effective_expires_at: float | None = None
    if local_override is not None and local_override.expires_at is not None:
        override_token = _override_token(local_override)
        override_effective_expires_at = (
            previous.override_effective_expires_at
            if previous.override_token == override_token
            and previous.override_effective_expires_at is not None
            else local_override.expires_at
        )
    override_active = local_override is not None and (
        override_effective_expires_at is None
        or now < override_effective_expires_at
    )
    override_drives_target = False
    global_drives_target = False
    if override_active and local_override is not None:
        # Local operator intent bypasses global state entirely.  Confirmed local safety may still
        # make the result more restrictive (e.g. a critical-temperature host cannot be manually
        # downgraded from unhealthy to draining).
        target = _more_restrictive(safety_target, local_override.state)
        override_drives_target = _STATE_RANK[local_override.state] >= _STATE_RANK[safety_target]
        reasons.append(f"local_override:{local_override.state.value}:{local_override.reason}")
        if local_override.state in (NodeState.PAUSED, NodeState.QUARANTINED):
            needs_drain_grace = False
    else:
        if local_override is not None:
            reasons.append("local_override_expired")
        try:
            global_state = (
                global_desired_state
                if isinstance(global_desired_state, NodeState)
                else NodeState(str(global_desired_state).lower())
            )
        except ValueError:
            global_state = NodeState.ACCEPTING
            reasons.append("global_desired_state_invalid")
        target = _more_restrictive(safety_target, global_state)
        global_drives_target = _STATE_RANK[global_state] >= _STATE_RANK[safety_target]
        if global_state != NodeState.ACCEPTING:
            reasons.append(f"global_desired:{global_state.value}")
        if global_drives_target and global_state != NodeState.ACCEPTING:
            needs_drain_grace = False

    # Manual changes, confirmed critical safety, and more-restrictive global commands apply now.
    # Automated activity/battery pause first rejects new admissions as draining, giving existing
    # work a deterministic grace interval before the lifecycle becomes paused.
    immediate = (
        (override_active and override_drives_target)
        or target in (NodeState.UNHEALTHY, NodeState.QUARANTINED)
        or (global_drives_target and target != NodeState.ACCEPTING)
    )
    drain_started_at: float | None = None
    drain_target: NodeState | None = None
    if target == NodeState.PAUSED and needs_drain_grace and not immediate:
        if previous.drain_target == NodeState.PAUSED and previous.drain_started_at is not None:
            drain_started_at = min(previous.drain_started_at, now)
        else:
            drain_started_at = now
        if _elapsed(now, drain_started_at) < policy.drain_grace_seconds:
            proposed = NodeState.DRAINING
            drain_target = NodeState.PAUSED
            reasons.append("drain_grace")
        else:
            proposed = NodeState.PAUSED
    else:
        proposed = target

    recovery_started_at: float | None = None
    if (
        not override_active
        and _STATE_RANK[proposed] < _STATE_RANK[previous.lifecycle]
    ):
        recovery_started_at = previous.recovery_started_at
        if recovery_started_at is None or recovery_started_at > now:
            recovery_started_at = now
        if _elapsed(now, recovery_started_at) < policy.recovery_cooldown_seconds:
            actual = previous.lifecycle
            reasons = [*previous.reasons, *reasons, "recovery_cooldown"]
            # A cleared drain cause must not keep its old target/start time; if it returns, it earns
            # a fresh grace period rather than using stale time to jump directly to paused.
            if previous.lifecycle != NodeState.DRAINING or proposed == NodeState.PAUSED:
                drain_started_at = previous.drain_started_at
                drain_target = previous.drain_target
        else:
            actual = proposed
            recovery_started_at = None
    else:
        actual = proposed

    reasons_tuple = _unique(reasons)
    state_since = (
        previous.state_since
        if previous.lifecycle == actual and previous.state_since is not None
        else now
    )
    next_state = LocalAllocatorState(
        lifecycle=actual,
        last_timestamp=now,
        state_since=state_since,
        activity_active_since=activity_latch.active_since,
        activity_clear_since=activity_latch.clear_since,
        activity_latched=activity_latch.latched,
        thermal_hot_since=thermal_latch.active_since,
        thermal_clear_since=thermal_latch.clear_since,
        thermal_latched=thermal_latch.latched,
        drain_started_at=drain_started_at,
        drain_target=drain_target,
        recovery_started_at=recovery_started_at,
        override_token=override_token,
        override_effective_expires_at=override_effective_expires_at,
        generation=previous.generation + 1,
        reasons=reasons_tuple,
    )

    if actual == NodeState.ACCEPTING:
        accept = True
        concurrency = 1.0
        priority = 1.0
    elif actual == NodeState.THROTTLED:
        accept = True
        if "unknown_signals_throttled" in reasons_tuple and not throttle_reasons:
            concurrency = policy.unknown_concurrency_multiplier
            priority = policy.unknown_priority_multiplier
        else:
            concurrency = policy.throttled_concurrency_multiplier
            priority = policy.throttled_priority_multiplier
    else:
        accept = False
        concurrency = 0.0
        priority = 0.0

    retry_after: float | None = None
    if not accept:
        if (
            override_active
            and local_override is not None
            and override_effective_expires_at is not None
        ):
            retry_after = max(override_effective_expires_at - now, 0.0)
        elif actual == NodeState.QUARANTINED or (
            override_active and local_override is not None and local_override.expires_at is None
        ):
            retry_after = None
        elif actual == NodeState.DRAINING and drain_started_at is not None:
            retry_after = max(
                policy.drain_grace_seconds - _elapsed(now, drain_started_at),
                0.0,
            )
        elif recovery_started_at is not None:
            retry_after = max(
                policy.recovery_cooldown_seconds - _elapsed(now, recovery_started_at),
                0.0,
            )
        else:
            retry_after = policy.retry_after_seconds

    return AdmissionDecision(
        state=actual,
        accept=accept,
        concurrency_multiplier=concurrency,
        priority_multiplier=priority,
        reasons=reasons_tuple,
        retry_after_seconds=retry_after,
        evaluated_at=now,
        next_state=next_state,
    )


class LocalHostProtectionLoop:
    """Small thread-safe holder around the pure evaluator for heartbeat loops."""

    def __init__(
        self,
        policy: HostPolicy | None = None,
        initial_state: LocalAllocatorState | None = None,
    ) -> None:
        self.policy = policy or DEFAULT_HOST_POLICY
        self._state = initial_state or LocalAllocatorState()
        self._lock = threading.Lock()

    @property
    def state(self) -> LocalAllocatorState:
        with self._lock:
            return self._state

    def evaluate(
        self,
        signals: HostSignals,
        override: LocalOverride | NodeState | str | None = None,
        *,
        global_desired_state: NodeState | str = NodeState.ACCEPTING,
    ) -> AdmissionDecision:
        with self._lock:
            decision = evaluate_host(
                signals,
                self.policy,
                self._state,
                override,
                global_desired_state=global_desired_state,
            )
            self._state = decision.next_state
            return decision

    def reset(self, state: LocalAllocatorState | None = None) -> None:
        with self._lock:
            self._state = state or LocalAllocatorState()


# Short aliases for integrations that treat the policy as a generic local evaluator.
evaluate = evaluate_host
LocalProtectionLoop = LocalHostProtectionLoop
ManualOverride = LocalOverride
HostProtectionState = LocalAllocatorState
