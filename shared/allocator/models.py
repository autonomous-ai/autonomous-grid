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
MAX_SOURCE_LENGTH = 4_096
SHA256_HEX_LENGTH = 64


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


def canonical_sha256(value: Any, name: str = "artifact_sha256") -> str:
    """Validate and canonicalize an optional immutable artifact identity."""

    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a 64-character hexadecimal SHA-256")
    normalized = value.lower()
    if len(normalized) != SHA256_HEX_LENGTH or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{name} must be a 64-character hexadecimal SHA-256")
    return normalized


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
    artifact_sha256: str = ""

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
        object.__setattr__(
            self,
            "artifact_sha256",
            canonical_sha256(self.artifact_sha256),
        )

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
            artifact_sha256=value.get("artifact_sha256") or "",
        )


@dataclass(frozen=True, slots=True)
class ModelPerformance:
    """Proxy-measured serving performance attributable to one model on one host."""

    model_id: str
    tokens_per_second: float = 0.0
    latency_ms: float = 0.0
    sample_count: int = 0
    updated_at: float = 0.0
    throughput_sample_count: int = 0
    throughput_updated_at: float = 0.0
    artifact_sha256: str = ""

    def __post_init__(self) -> None:
        if not self.model_id or len(self.model_id) > MAX_ID_LENGTH:
            raise ValueError("model_id is required")
        _finite_nonnegative(self.tokens_per_second, "tokens_per_second")
        _finite_nonnegative(self.latency_ms, "latency_ms")
        _finite_nonnegative(self.updated_at, "updated_at")
        _finite_nonnegative(self.throughput_updated_at, "throughput_updated_at")
        if not 0 <= self.sample_count <= MAX_COUNTER:
            raise ValueError("sample_count is outside the supported range")
        if not 0 <= self.throughput_sample_count <= MAX_COUNTER:
            raise ValueError("throughput_sample_count is outside the supported range")
        if self.throughput_sample_count > 0 and not self.throughput_updated_at:
            object.__setattr__(self, "throughput_updated_at", self.updated_at)
        object.__setattr__(
            self,
            "artifact_sha256",
            canonical_sha256(self.artifact_sha256),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ModelPerformance:
        sample_count = int(value.get("sample_count") or 0)
        throughput_sample_count = (
            int(value.get("throughput_sample_count") or 0)
            if "throughput_sample_count" in value
            else sample_count
        )
        throughput_updated_at = (
            float(value.get("throughput_updated_at") or 0.0)
            if "throughput_updated_at" in value
            else float(value.get("updated_at") or 0.0)
        )
        return cls(
            model_id=str(value["model_id"]),
            tokens_per_second=float(value.get("tokens_per_second") or 0.0),
            latency_ms=float(value.get("latency_ms") or 0.0),
            sample_count=sample_count,
            throughput_sample_count=throughput_sample_count,
            updated_at=float(value.get("updated_at") or 0.0),
            throughput_updated_at=throughput_updated_at,
            artifact_sha256=value.get("artifact_sha256") or "",
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
    disk_capacity_mb: int | None = None
    disk_available_mb: int | None = None
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
        for name in ("disk_capacity_mb", "disk_available_mb"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= MAX_MEMORY_MB
            ):
                raise ValueError(f"{name} must be a non-negative integer or None")
        if (
            self.disk_capacity_mb is not None
            and self.disk_available_mb is not None
            and self.disk_available_mb > self.disk_capacity_mb
        ):
            raise ValueError("disk_available_mb cannot exceed disk_capacity_mb")
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
        # Older allocator snapshots carried optional accounting metadata. It never affects current
        # placement; ignore it so durable pre-removal state remains readable during upgrades.
        fields.pop("cost_per_hour", None)
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
    artifact_sha256: str = ""
    # An authenticated operator may provide an immutable, runtime-specific artifact source. The
    # first managed adapter accepts exact ``hf://owner/repo/path.gguf`` URIs. Other runtimes can
    # add adapters without teaching the placement controller their download protocol.
    artifact_source: str = ""
    # Upper bound for an autonomous transfer and the controller's disk-admission estimate.
    artifact_size_mb: int = 0
    max_colocated_models: int = 0
    colocation_excludes: tuple[str, ...] = ()
    # Allocator portfolio suitability by workload. These are planning priors, not router ranks.
    # A model with no entries participates only in direct named-model scaling.
    workload_scores: tuple[tuple[str, float], ...] = ()

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
        workload_scores: dict[str, float] = {}
        for item in self.workload_scores:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise ValueError("workload_scores entries must be (workload, score) pairs")
            workload, score = str(item[0]), float(item[1])
            if not workload or len(workload) > 64 or workload in workload_scores:
                raise ValueError("workload_scores contains an invalid or duplicate workload")
            if not math.isfinite(score) or not 0 < score <= 1:
                raise ValueError("workload_scores values must be in (0, 1]")
            workload_scores[workload] = score
        object.__setattr__(self, "workload_scores", tuple(sorted(workload_scores.items())))
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
        if (
            isinstance(self.artifact_size_mb, bool)
            or not isinstance(self.artifact_size_mb, int)
            or not 0 <= self.artifact_size_mb <= MAX_MEMORY_MB
        ):
            raise ValueError(f"artifact_size_mb must be in [0, {MAX_MEMORY_MB}]")
        if (
            isinstance(self.max_colocated_models, bool)
            or not isinstance(self.max_colocated_models, int)
            or not 0 <= self.max_colocated_models <= MAX_COUNTER
        ):
            raise ValueError(
                f"max_colocated_models must be in [0, {MAX_COUNTER}]"
            )
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
            "colocation_excludes",
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
        if self.model_id in self.colocation_excludes:
            raise ValueError("colocation_excludes cannot contain the profile model")
        if any(len(model_id) > MAX_ID_LENGTH for model_id in self.colocation_excludes):
            raise ValueError("colocation_excludes contains an invalid model ID")
        object.__setattr__(
            self,
            "artifact_sha256",
            canonical_sha256(self.artifact_sha256),
        )
        source = str(self.artifact_source or "").strip()
        if len(source) > MAX_SOURCE_LENGTH or any(
            character in source for character in "\r\n\0"
        ):
            raise ValueError("artifact_source is invalid")
        if source and "://" not in source:
            raise ValueError("artifact_source must be an absolute runtime-specific URI")
        if source and (not self.artifact_sha256 or not self.artifact_size_mb):
            raise ValueError(
                "artifact_source requires artifact_sha256 and artifact_size_mb"
            )
        object.__setattr__(self, "artifact_source", source)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "schema_version": SCHEMA_VERSION}

    def memory_for(self, runtimes: Iterable[str]) -> int:
        """Return the conservative footprint for one host's advertised runtimes."""

        overrides = dict(self.runtime_memory_mb)
        matched = [overrides[runtime] for runtime in set(runtimes) if runtime in overrides]
        return max(matched, default=self.memory_mb)

    def workload_score(self, workload: str) -> float:
        """Return this model's configured portfolio suitability for one workload."""

        return dict(self.workload_scores).get(workload, 0.0)

    def matches_artifact(self, residency: ModelResidency | None) -> bool:
        """Whether a residency proves the immutable artifact requested by this profile."""

        return residency is not None and (
            not self.artifact_sha256
            or residency.artifact_sha256 == self.artifact_sha256
        )

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
    correlated_requests_per_minute: float = 0.0
    correlation_confidence: float = 0.0
    correlation_sources: tuple[str, ...] = ()
    sample_count: int = 0
    updated_at: float = 0.0
    observed_requests_per_minute: float = 0.0
    canary_only: bool = False

    def __post_init__(self) -> None:
        if not self.model_id:
            raise ValueError("model_id is required")
        if not isinstance(self.canary_only, bool):
            raise ValueError("canary_only must be a boolean")
        for name in (
            "requests_per_minute",
            "offered_concurrency",
            "p95_latency_ms",
            "error_rate",
            "confidence",
            "correlated_requests_per_minute",
            "correlation_confidence",
            "observed_requests_per_minute",
            "updated_at",
        ):
            _finite_nonnegative(float(getattr(self, name)), name)
        if self.queue_depth < 0 or self.sample_count < 0:
            raise ValueError("queue_depth and sample_count must be non-negative")
        if (
            self.error_rate > 1
            or self.confidence > 1
            or self.correlation_confidence > 1
        ):
            raise ValueError("error_rate and confidence cannot exceed 1")
        if self.observed_requests_per_minute > self.requests_per_minute:
            raise ValueError(
                "observed_requests_per_minute cannot exceed requests_per_minute"
            )
        if self.correlated_requests_per_minute > self.requests_per_minute:
            raise ValueError(
                "correlated_requests_per_minute cannot exceed requests_per_minute"
            )
        if not math.isfinite(self.trend_per_minute):
            raise ValueError("trend_per_minute must be finite")
        object.__setattr__(
            self,
            "correlation_sources",
            _canonical_set(self.correlation_sources),
        )


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
class PlacementPreemption:
    """A staged removal that makes scarce capacity available to more important work."""

    node_id: str
    model_id: str
    for_model_id: str = ""

    def __post_init__(self) -> None:
        if (
            not self.node_id
            or not self.model_id
            or any(
                len(value) > MAX_ID_LENGTH
                for value in (self.node_id, self.model_id, self.for_model_id)
            )
        ):
            raise ValueError("preemption node and victim model are required")
        if self.for_model_id and self.model_id == self.for_model_id:
            raise ValueError("a model cannot preempt itself")


@dataclass(frozen=True, slots=True)
class ArtifactPrefetch:
    """A cache-only placement hint that consumes disk but no runtime capacity."""

    node_id: str
    model_id: str

    def __post_init__(self) -> None:
        if (
            not self.node_id
            or not self.model_id
            or len(self.node_id) > MAX_ID_LENGTH
            or len(self.model_id) > MAX_ID_LENGTH
        ):
            raise ValueError("artifact prefetch node and model are required")


@dataclass(frozen=True, slots=True)
class PlacementPlan:
    generation: str
    created_at: float
    assignments: tuple[PlacementAssignment, ...] = ()
    desired_replicas: tuple[tuple[str, int], ...] = ()
    unsatisfied: tuple[UnsatisfiedConstraint, ...] = ()
    objective_score: float = 0.0
    input_digest: str = ""
    preemptions: tuple[PlacementPreemption, ...] = ()
    artifact_prefetches: tuple[ArtifactPrefetch, ...] = ()
    model_urgencies: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        if not self.generation:
            raise ValueError("generation is required")
        _finite_nonnegative(self.created_at, "created_at")
        if not math.isfinite(self.objective_score):
            raise ValueError("objective_score must be finite")
        pairs = [(item.model_id, item.node_id) for item in self.assignments]
        if len(pairs) != len(set(pairs)):
            raise ValueError("a model cannot be assigned twice to the same node")
        preemption_pairs = [(item.node_id, item.model_id) for item in self.preemptions]
        if len(preemption_pairs) != len(set(preemption_pairs)):
            raise ValueError("a residency cannot be preempted more than once")
        prefetch_pairs = [
            (item.node_id, item.model_id) for item in self.artifact_prefetches
        ]
        if len(prefetch_pairs) != len(set(prefetch_pairs)):
            raise ValueError("an artifact cannot be prefetched twice to the same node")
        desired_pairs = {(node_id, model_id) for model_id, node_id in pairs}
        if desired_pairs.intersection(preemption_pairs):
            raise ValueError("a residency cannot be both desired and preempted")
        if desired_pairs.intersection(prefetch_pairs):
            raise ValueError("a desired residency does not need a cache-only prefetch")
        preemption_beneficiaries = {
            (item.node_id, item.for_model_id)
            for item in self.preemptions
            if item.for_model_id
        }
        if preemption_beneficiaries.intersection(prefetch_pairs):
            raise ValueError(
                "a preemption beneficiary does not need a predictive artifact prefetch"
            )
        urgency_models = [model_id for model_id, _ in self.model_urgencies]
        if len(urgency_models) != len(set(urgency_models)) or any(
            not model_id
            or not isinstance(model_id, str)
            or len(model_id) > MAX_ID_LENGTH
            or isinstance(urgency, bool)
            or not isinstance(urgency, int)
            or not 0 <= urgency <= 3
            for model_id, urgency in self.model_urgencies
        ):
            raise ValueError("model urgencies must be unique integer tiers in [0, 3]")
        object.__setattr__(self, "model_urgencies", tuple(sorted(self.model_urgencies)))

    @property
    def desired_pairs(self) -> frozenset[tuple[str, str]]:
        return frozenset((item.node_id, item.model_id) for item in self.assignments)

    @property
    def preempted_pairs(self) -> frozenset[tuple[str, str]]:
        return frozenset((item.node_id, item.model_id) for item in self.preemptions)

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

    def urgency_for(self, model_id: str) -> int:
        return dict(self.model_urgencies).get(model_id, 0)

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
            "preemptions": [asdict(item) for item in self.preemptions],
            "artifact_prefetches": [
                asdict(item) for item in self.artifact_prefetches
            ],
            "model_urgencies": dict(self.model_urgencies),
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
    artifact_sha256: str = ""
    artifact_source: str = ""
    artifact_size_mb: int = 0
    controller_term: int = 0
    controller_id: str = ""
    controller_lease_expires_at: float = 0.0

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
        if (
            isinstance(self.controller_term, bool)
            or not isinstance(self.controller_term, int)
            or not 0 <= self.controller_term <= MAX_COUNTER
        ):
            raise ValueError("controller_term must be an integer in the supported range")
        if self.controller_term and (
            not self.controller_id
            or not isinstance(self.controller_id, str)
            or len(self.controller_id) > MAX_ID_LENGTH
        ):
            raise ValueError("fenced allocator commands require a controller_id")
        if not self.controller_term and self.controller_id:
            raise ValueError("legacy allocator commands cannot declare a controller_id")
        _finite_nonnegative(
            self.controller_lease_expires_at,
            "controller_lease_expires_at",
        )
        if self.controller_lease_expires_at and not self.controller_term:
            raise ValueError("a controller lease requires a positive controller_term")
        if not isinstance(self.kind, ActionKind):
            object.__setattr__(self, "kind", ActionKind(self.kind))
        object.__setattr__(self, "dependencies", _unique(self.dependencies))
        object.__setattr__(
            self,
            "artifact_sha256",
            canonical_sha256(self.artifact_sha256),
        )
        source = str(self.artifact_source or "").strip()
        if len(source) > MAX_SOURCE_LENGTH or any(
            character in source for character in "\r\n\0"
        ):
            raise ValueError("artifact_source is invalid")
        if source and "://" not in source:
            raise ValueError("artifact_source must be an absolute runtime-specific URI")
        if (
            isinstance(self.artifact_size_mb, bool)
            or not isinstance(self.artifact_size_mb, int)
            or not 0 <= self.artifact_size_mb <= MAX_MEMORY_MB
        ):
            raise ValueError(f"artifact_size_mb must be in [0, {MAX_MEMORY_MB}]")
        if source and (not self.artifact_sha256 or not self.artifact_size_mb):
            raise ValueError(
                "artifact_source requires artifact_sha256 and artifact_size_mb"
            )
        object.__setattr__(self, "artifact_source", source)

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
