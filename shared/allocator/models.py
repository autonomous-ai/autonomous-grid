"""Typed state shared by Grid's local and global allocation loops.

These values cross process and, eventually, repository boundaries.  The wire representation is
therefore plain JSON-compatible dictionaries with explicit schema versions rather than pickles or
Pydantic implementation details.  Dataclasses stay frozen so a planner cannot accidentally mutate a
heartbeat snapshot while considering alternatives.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

SCHEMA_VERSION = 1
MAX_MEMORY_MB = 1_000_000_000
MAX_COUNTER = 1_000_000_000
MAX_ID_LENGTH = 1_024


class NodeState(StrEnum):
    ACCEPTING = "accepting"
    THROTTLED = "throttled"
    DRAINING = "draining"
    PAUSED = "paused"
    UNHEALTHY = "unhealthy"
    QUARANTINED = "quarantined"


class ResidencyState(StrEnum):
    CACHED = "cached"
    LOADING = "loading"
    WARMING = "warming"
    READY = "ready"
    DRAINING = "draining"
    FAILED = "failed"


class AllocatorMode(StrEnum):
    OBSERVE = "observe"
    RECOMMEND = "recommend"
    AUTOMATIC = "automatic"


class ActionKind(StrEnum):
    LOAD = "load"
    WARM = "warm"
    DRAIN = "drain"
    UNLOAD = "unload"


def _finite_nonnegative(value: float, name: str) -> None:
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and non-negative")


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in values if str(value)))


def _canonical_set(values: Iterable[str]) -> tuple[str, ...]:
    """Canonicalize a wire field whose order has no meaning."""

    return tuple(sorted({str(value) for value in values if str(value)}))


@dataclass(frozen=True, slots=True)
class ModelResidency:
    model_id: str
    memory_mb: int
    state: ResidencyState = ResidencyState.READY
    loaded_at: float = 0.0
    last_used_at: float = 0.0
    load_failures: int = 0
    pinned: bool = False
    managed: bool = True
    active_requests: int = 0

    def __post_init__(self) -> None:
        if not self.model_id or len(self.model_id) > MAX_ID_LENGTH:
            raise ValueError("model_id is required")
        if self.memory_mb <= 0 or self.memory_mb > MAX_MEMORY_MB:
            raise ValueError(f"memory_mb must be in [1, {MAX_MEMORY_MB}]")
        _finite_nonnegative(self.loaded_at, "loaded_at")
        _finite_nonnegative(self.last_used_at, "last_used_at")
        if not 0 <= self.load_failures <= MAX_COUNTER:
            raise ValueError("load_failures is outside the supported range")
        if not 0 <= self.active_requests <= MAX_COUNTER:
            raise ValueError("active_requests is outside the supported range")
        if not isinstance(self.state, ResidencyState):
            object.__setattr__(self, "state", ResidencyState(self.state))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ModelResidency:
        return cls(
            model_id=str(value["model_id"]),
            memory_mb=int(value["memory_mb"]),
            state=ResidencyState(value.get("state", ResidencyState.READY)),
            loaded_at=float(value.get("loaded_at") or 0.0),
            last_used_at=float(value.get("last_used_at") or 0.0),
            load_failures=int(value.get("load_failures") or 0),
            pinned=bool(value.get("pinned", False)),
            managed=bool(value.get("managed", True)),
            active_requests=int(value.get("active_requests") or 0),
        )


@dataclass(frozen=True, slots=True)
class ModelPerformance:
    """Proxy-measured serving performance attributable to one model on one host."""

    model_id: str
    tokens_per_second: float = 0.0
    latency_ms: float = 0.0
    sample_count: int = 0
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.model_id or len(self.model_id) > MAX_ID_LENGTH:
            raise ValueError("model_id is required")
        _finite_nonnegative(self.tokens_per_second, "tokens_per_second")
        _finite_nonnegative(self.latency_ms, "latency_ms")
        _finite_nonnegative(self.updated_at, "updated_at")
        if not 0 <= self.sample_count <= MAX_COUNTER:
            raise ValueError("sample_count is outside the supported range")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ModelPerformance:
        return cls(
            model_id=str(value["model_id"]),
            tokens_per_second=float(value.get("tokens_per_second") or 0.0),
            latency_ms=float(value.get("latency_ms") or 0.0),
            sample_count=int(value.get("sample_count") or 0),
            updated_at=float(value.get("updated_at") or 0.0),
        )


@dataclass(frozen=True, slots=True)
class NodeSnapshot:
    node_id: str
    capacity_mb: int
    reserved_mb: int = 0
    backends: tuple[str, ...] = ()
    runtimes: tuple[str, ...] = ()
    state: NodeState = NodeState.ACCEPTING
    failure_domain: str = ""
    allowed_data_tiers: tuple[str, ...] = ("public", "internal")
    allowed_models: tuple[str, ...] = ()
    denied_models: tuple[str, ...] = ()
    required_tags: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    max_models: int | None = None
    residencies: tuple[ModelResidency, ...] = ()
    cached_models: tuple[str, ...] = ()
    active_requests: int = 0
    max_concurrency: int = 1
    queue_depth: int = 0
    tokens_per_second: float = 0.0
    latency_ms: float = 0.0
    model_performance: tuple[ModelPerformance, ...] = ()
    memory_bandwidth_gbps: float = 0.0
    compute_gflops: float = 0.0
    gpu_count: int = 0
    gpu_memory_mb: tuple[int, ...] = ()
    cost_per_hour: float = 0.0
    host_priority: int = 0
    last_heartbeat: float = 0.0
    mutation_cooldown_until: float = 0.0
    actuator_capabilities: tuple[str, ...] = ("load", "warm", "drain", "unload")
    manually_managed: bool = False

    def __post_init__(self) -> None:
        if not self.node_id or len(self.node_id) > MAX_ID_LENGTH:
            raise ValueError("node_id is required")
        if (
            not 0 <= self.capacity_mb <= MAX_MEMORY_MB
            or not 0 <= self.reserved_mb <= MAX_MEMORY_MB
        ):
            raise ValueError(
                "capacity_mb and reserved_mb are outside the supported range"
            )
        if self.reserved_mb > self.capacity_mb:
            raise ValueError("reserved_mb cannot exceed capacity_mb")
        if self.max_models is not None and not 0 <= self.max_models <= MAX_COUNTER:
            raise ValueError("max_models must be non-negative or None")
        if any(
            not 0 <= value <= MAX_COUNTER
            for value in (self.active_requests, self.max_concurrency, self.queue_depth)
        ):
            raise ValueError(
                "request counts and concurrency are outside the supported range"
            )
        _finite_nonnegative(self.tokens_per_second, "tokens_per_second")
        _finite_nonnegative(self.latency_ms, "latency_ms")
        _finite_nonnegative(self.memory_bandwidth_gbps, "memory_bandwidth_gbps")
        _finite_nonnegative(self.compute_gflops, "compute_gflops")
        _finite_nonnegative(self.cost_per_hour, "cost_per_hour")
        _finite_nonnegative(self.last_heartbeat, "last_heartbeat")
        _finite_nonnegative(self.mutation_cooldown_until, "mutation_cooldown_until")
        if (
            isinstance(self.gpu_count, bool)
            or not isinstance(self.gpu_count, int)
            or not 0 <= self.gpu_count <= MAX_COUNTER
        ):
            raise ValueError(f"gpu_count must be in [0, {MAX_COUNTER}]")
        try:
            gpu_memory = tuple(
                sorted((int(value) for value in self.gpu_memory_mb), reverse=True)
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("gpu_memory_mb values must be positive integers") from exc
        if any(value <= 0 or value > MAX_MEMORY_MB for value in gpu_memory):
            raise ValueError(f"gpu_memory_mb values must be in [1, {MAX_MEMORY_MB}]")
        object.__setattr__(self, "gpu_memory_mb", gpu_memory)
        if len(gpu_memory) > self.gpu_count:
            object.__setattr__(self, "gpu_count", len(gpu_memory))
        if not isinstance(self.state, NodeState):
            object.__setattr__(self, "state", NodeState(self.state))
        for field_name in (
            "backends",
            "runtimes",
            "allowed_data_tiers",
            "allowed_models",
            "denied_models",
            "required_tags",
            "tags",
            "cached_models",
            "actuator_capabilities",
        ):
            object.__setattr__(
                self, field_name, _canonical_set(getattr(self, field_name))
            )
        object.__setattr__(
            self,
            "residencies",
            tuple(
                sorted(
                    (
                        item
                        if isinstance(item, ModelResidency)
                        else ModelResidency.from_dict(item)
                        for item in self.residencies
                    ),
                    key=lambda item: item.model_id,
                )
            ),
        )
        object.__setattr__(
            self,
            "model_performance",
            tuple(
                sorted(
                    (
                        item
                        if isinstance(item, ModelPerformance)
                        else ModelPerformance.from_dict(item)
                        for item in self.model_performance
                    ),
                    key=lambda item: item.model_id,
                )
            ),
        )
        model_ids = [item.model_id for item in self.residencies]
        if len(model_ids) != len(set(model_ids)):
            raise ValueError(f"node {self.node_id!r} has duplicate model residencies")
        performance_model_ids = [item.model_id for item in self.model_performance]
        if len(performance_model_ids) != len(set(performance_model_ids)):
            raise ValueError(f"node {self.node_id!r} has duplicate model performance")

    @property
    def usable_capacity_mb(self) -> int:
        return self.capacity_mb - self.reserved_mb

    @property
    def resident_memory_mb(self) -> int:
        return sum(
            residency.memory_mb
            for residency in self.residencies
            if residency.state not in (ResidencyState.CACHED, ResidencyState.FAILED)
        )

    @property
    def ready_models(self) -> frozenset[str]:
        return frozenset(
            residency.model_id
            for residency in self.residencies
            if residency.state == ResidencyState.READY
        )

    def residency(self, model_id: str) -> ModelResidency | None:
        return next(
            (item for item in self.residencies if item.model_id == model_id), None
        )

    def performance(self, model_id: str) -> ModelPerformance | None:
        return next(
            (item for item in self.model_performance if item.model_id == model_id),
            None,
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["schema_version"] = SCHEMA_VERSION
        data["state"] = self.state.value
        for residency in data["residencies"]:
            residency["state"] = str(residency["state"])
        return data

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> NodeSnapshot:
        if int(value.get("schema_version", SCHEMA_VERSION)) != SCHEMA_VERSION:
            raise ValueError("unsupported allocator node schema")
        fields = dict(value)
        fields.pop("schema_version", None)
        fields["residencies"] = tuple(
            ModelResidency.from_dict(item) for item in fields.get("residencies") or ()
        )
        fields["model_performance"] = tuple(
            ModelPerformance.from_dict(item)
            for item in fields.get("model_performance") or ()
        )
        return cls(**fields)


@dataclass(frozen=True, slots=True)
class ModelProfile:
    model_id: str
    memory_mb: int
    # Runtime-specific resident-memory estimates. ``memory_mb`` remains the portable fallback for
    # old profiles and hosts whose runtime has no override. A tuple keeps the frozen wire model
    # deterministic while accepting ordinary JSON arrays on input.
    runtime_memory_mb: tuple[tuple[str, int], ...] = ()
    runtimes: tuple[str, ...] = ()
    backends: tuple[str, ...] = ()
    data_tier: str = "internal"
    required_tags: tuple[str, ...] = ()
    forbidden_tags: tuple[str, ...] = ()
    pinned_nodes: tuple[str, ...] = ()
    min_replicas: int = 1
    max_replicas: int = 1
    target_utilization: float = 0.70
    # Conservative service slots supplied by a newly placed replica before engine-specific live
    # telemetry exists. A ready single-model engine may prove a higher value via max_concurrency.
    replica_concurrency: int = 1
    expected_service_seconds: float = 5.0
    latency_slo_ms: float = 5_000.0
    priority: int = 100
    load_seconds: float = 30.0
    warm_seconds: float = 5.0
    min_residency_seconds: float = 300.0
    scale_down_cooldown_seconds: float = 900.0
    min_failure_domains: int = 1
    min_gpu_count: int = 0
    min_gpu_memory_mb: int = 0

    def __post_init__(self) -> None:
        if not self.model_id or len(self.model_id) > MAX_ID_LENGTH:
            raise ValueError("model_id is required")
        if self.memory_mb <= 0 or self.memory_mb > MAX_MEMORY_MB:
            raise ValueError(f"memory_mb must be in [1, {MAX_MEMORY_MB}]")
        runtime_memory: dict[str, int] = {}
        for item in self.runtime_memory_mb:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise ValueError("runtime_memory_mb entries must be (runtime, memory_mb) pairs")
            try:
                runtime, memory_mb = str(item[0]), int(item[1])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "runtime_memory_mb entries must be (runtime, memory_mb) pairs"
                ) from exc
            if not runtime or len(runtime) > MAX_ID_LENGTH:
                raise ValueError("runtime_memory_mb runtime names are invalid")
            if runtime in runtime_memory:
                raise ValueError(f"duplicate runtime_memory_mb entry for {runtime!r}")
            if memory_mb <= 0 or memory_mb > MAX_MEMORY_MB:
                raise ValueError(
                    f"runtime_memory_mb values must be in [1, {MAX_MEMORY_MB}]"
                )
            runtime_memory[runtime] = memory_mb
        object.__setattr__(
            self,
            "runtime_memory_mb",
            tuple(sorted(runtime_memory.items())),
        )
        if (
            self.min_replicas < 0
            or self.max_replicas < self.min_replicas
            or self.max_replicas > MAX_COUNTER
        ):
            raise ValueError("replica bounds are invalid")
        if not 0 < self.target_utilization <= 1:
            raise ValueError("target_utilization must be in (0, 1]")
        if (
            isinstance(self.replica_concurrency, bool)
            or not isinstance(self.replica_concurrency, int)
            or not 1 <= self.replica_concurrency <= MAX_COUNTER
        ):
            raise ValueError(f"replica_concurrency must be in [1, {MAX_COUNTER}]")
        if self.priority < 0 or self.min_failure_domains < 1:
            raise ValueError("priority/domain values are invalid")
        if (
            isinstance(self.min_gpu_count, bool)
            or not isinstance(self.min_gpu_count, int)
            or not 0 <= self.min_gpu_count <= MAX_COUNTER
        ):
            raise ValueError(f"min_gpu_count must be in [0, {MAX_COUNTER}]")
        if (
            isinstance(self.min_gpu_memory_mb, bool)
            or not isinstance(self.min_gpu_memory_mb, int)
            or not 0 <= self.min_gpu_memory_mb <= MAX_MEMORY_MB
        ):
            raise ValueError(f"min_gpu_memory_mb must be in [0, {MAX_MEMORY_MB}]")
        for name in (
            "expected_service_seconds",
            "latency_slo_ms",
            "load_seconds",
            "warm_seconds",
            "min_residency_seconds",
            "scale_down_cooldown_seconds",
        ):
            _finite_nonnegative(float(getattr(self, name)), name)
        for field_name in (
            "runtimes",
            "backends",
            "required_tags",
            "forbidden_tags",
            "pinned_nodes",
        ):
            object.__setattr__(
                self, field_name, _canonical_set(getattr(self, field_name))
            )
        unknown_memory_runtimes = set(runtime_memory) - set(self.runtimes)
        if self.runtimes and unknown_memory_runtimes:
            names = ", ".join(sorted(unknown_memory_runtimes))
            raise ValueError(
                f"runtime_memory_mb has overrides outside compatible runtimes: {names}"
            )
        if len(self.pinned_nodes) > self.max_replicas:
            raise ValueError("pinned_nodes cannot exceed max_replicas")

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "schema_version": SCHEMA_VERSION}

    def memory_for(self, runtimes: Iterable[str]) -> int:
        """Return the conservative footprint for one host's advertised runtimes."""

        overrides = dict(self.runtime_memory_mb)
        matched = [overrides[runtime] for runtime in set(runtimes) if runtime in overrides]
        return max(matched, default=self.memory_mb)

    @property
    def maximum_memory_mb(self) -> int:
        """Largest configured footprint, used for deterministic model placement ordering."""

        return max([self.memory_mb, *(value for _, value in self.runtime_memory_mb)])

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ModelProfile:
        if int(value.get("schema_version", SCHEMA_VERSION)) != SCHEMA_VERSION:
            raise ValueError("unsupported allocator model schema")
        fields = dict(value)
        fields.pop("schema_version", None)
        return cls(**fields)


@dataclass(frozen=True, slots=True)
class DemandForecast:
    model_id: str
    requests_per_minute: float = 0.0
    offered_concurrency: float = 0.0
    queue_depth: int = 0
    p95_latency_ms: float = 0.0
    error_rate: float = 0.0
    trend_per_minute: float = 0.0
    confidence: float = 0.0
    sample_count: int = 0
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.model_id:
            raise ValueError("model_id is required")
        for name in (
            "requests_per_minute",
            "offered_concurrency",
            "p95_latency_ms",
            "error_rate",
            "confidence",
            "updated_at",
        ):
            _finite_nonnegative(float(getattr(self, name)), name)
        if self.queue_depth < 0 or self.sample_count < 0:
            raise ValueError("queue_depth and sample_count must be non-negative")
        if self.error_rate > 1 or self.confidence > 1:
            raise ValueError("error_rate and confidence cannot exceed 1")
        if not math.isfinite(self.trend_per_minute):
            raise ValueError("trend_per_minute must be finite")


@dataclass(frozen=True, slots=True)
class PlacementAssignment:
    model_id: str
    node_id: str
    memory_mb: int
    replica_index: int
    score: float
    existing: bool = False
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.model_id or not self.node_id:
            raise ValueError("model_id and node_id are required")
        if (
            self.memory_mb <= 0
            or self.replica_index < 0
            or not math.isfinite(self.score)
        ):
            raise ValueError("invalid assignment memory, index, or score")
        object.__setattr__(self, "reasons", _unique(self.reasons))


@dataclass(frozen=True, slots=True)
class UnsatisfiedConstraint:
    model_id: str
    code: str
    message: str
    missing_replicas: int = 0

    def __post_init__(self) -> None:
        if not self.code or not self.message or self.missing_replicas < 0:
            raise ValueError("invalid unsatisfied constraint")


@dataclass(frozen=True, slots=True)
class PlacementPlan:
    generation: str
    created_at: float
    assignments: tuple[PlacementAssignment, ...] = ()
    desired_replicas: tuple[tuple[str, int], ...] = ()
    unsatisfied: tuple[UnsatisfiedConstraint, ...] = ()
    objective_score: float = 0.0
    input_digest: str = ""

    def __post_init__(self) -> None:
        if not self.generation:
            raise ValueError("generation is required")
        _finite_nonnegative(self.created_at, "created_at")
        if not math.isfinite(self.objective_score):
            raise ValueError("objective_score must be finite")
        pairs = [(item.model_id, item.node_id) for item in self.assignments]
        if len(pairs) != len(set(pairs)):
            raise ValueError("a model cannot be assigned twice to the same node")

    @property
    def desired_pairs(self) -> frozenset[tuple[str, str]]:
        return frozenset((item.node_id, item.model_id) for item in self.assignments)

    def nodes_for(self, model_id: str) -> tuple[str, ...]:
        return tuple(
            item.node_id for item in self.assignments if item.model_id == model_id
        )

    def models_for(self, node_id: str) -> tuple[str, ...]:
        return tuple(
            item.model_id for item in self.assignments if item.node_id == node_id
        )

    def target_for(self, model_id: str) -> int:
        return dict(self.desired_replicas).get(model_id, 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "generation": self.generation,
            "created_at": self.created_at,
            "assignments": [asdict(item) for item in self.assignments],
            "desired_replicas": dict(self.desired_replicas),
            "unsatisfied": [asdict(item) for item in self.unsatisfied],
            "objective_score": self.objective_score,
            "input_digest": self.input_digest,
        }


@dataclass(frozen=True, slots=True)
class MutationAction:
    action_id: str
    kind: ActionKind
    node_id: str
    model_id: str
    memory_mb: int
    reason: str
    plan_generation: str
    created_at: float
    not_before: float = 0.0
    dependencies: tuple[str, ...] = ()
    executable: bool = False

    def __post_init__(self) -> None:
        if (
            not self.action_id
            or not self.node_id
            or not self.model_id
            or not self.reason
        ):
            raise ValueError("action identity, target, and reason are required")
        if self.memory_mb <= 0:
            raise ValueError("memory_mb must be positive")
        _finite_nonnegative(self.created_at, "created_at")
        _finite_nonnegative(self.not_before, "not_before")
        if not isinstance(self.kind, ActionKind):
            object.__setattr__(self, "kind", ActionKind(self.kind))
        object.__setattr__(self, "dependencies", _unique(self.dependencies))

    def to_dict(self) -> dict[str, Any]:
        """Return the versioned command envelope sent to a managed node."""

        data = asdict(self)
        data["schema_version"] = SCHEMA_VERSION
        data["kind"] = self.kind.value
        data["dependencies"] = list(self.dependencies)
        return data

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> MutationAction:
        """Decode an allocator command received from an untrusted peer."""

        if int(value.get("schema_version", SCHEMA_VERSION)) != SCHEMA_VERSION:
            raise ValueError("unsupported allocator action schema")
        fields = dict(value)
        fields.pop("schema_version", None)
        fields["kind"] = ActionKind(fields["kind"])
        fields["dependencies"] = tuple(fields.get("dependencies") or ())
        return cls(**fields)

    @staticmethod
    def stable_id(
        kind: ActionKind,
        node_id: str,
        model_id: str,
        transition: str = "",
    ) -> str:
        raw = (
            f"allocator-action-v1\0{kind.value}\0{node_id}\0{model_id}\0{transition}"
        ).encode()
        return hashlib.sha256(raw).hexdigest()[:24]


def stable_digest(value: Any) -> str:
    """A deterministic, short identity for plan inputs and generations."""
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def new_generation(input_digest: str, created_at: float | None = None) -> str:
    now = time.time() if created_at is None else created_at
    return f"{int(now * 1000):013d}-{input_digest[:12]}"
