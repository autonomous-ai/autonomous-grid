"""Deterministic, capacity-aware model placement for a heterogeneous Grid.

This is intentionally a transparent heuristic rather than a black-box solver.  It computes replica
need from observed concurrency, preserves warm state, spreads replicas across failure domains, and
uses best-fit placement to avoid memory fragmentation.  Every shortfall is returned as data; the
planner never overcommits a node to make a dashboard look green.
"""

from __future__ import annotations

import math
import time
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from itertools import combinations

from shared.allocator.models import (
    MAX_COUNTER,
    MAX_MEMORY_MB,
    ArtifactEviction,
    ArtifactPrefetch,
    DemandForecast,
    ModelProfile,
    ModelResidency,
    NodeSnapshot,
    NodeState,
    PlacementAssignment,
    PlacementPlan,
    PlacementPreemption,
    ResidencyState,
    UnsatisfiedConstraint,
    new_generation,
    stable_digest,
)


@dataclass(frozen=True, slots=True)
class PlannerPolicy:
    memory_headroom_fraction: float = 0.05
    demand_headroom_fraction: float = 0.20
    node_ttl_seconds: float = 90.0
    max_future_clock_skew_seconds: float = 30.0
    queue_items_per_replica: int = 2
    latency_pressure_limit: float = 2.0
    error_pressure_limit: float = 0.5
    model_failure_penalty: float = 50_000.0
    performance_ttl_seconds: float = 900.0
    performance_full_confidence_samples: int = 8
    max_predictive_lookahead_seconds: float = 300.0
    predictive_growth_limit: float = 2.0
    throttled_capacity_fraction: float = 0.5
    preserve_recent_residencies: bool = True
    max_staged_preemptions: int = 64
    max_predictive_artifact_prefetches: int = 1
    predictive_artifact_disk_reserve_mb: int = 10_240
    predictive_artifact_ttl_seconds: float = 21_600.0
    max_predictive_artifact_evictions: int = 1
    predictive_artifact_replacement_min_age_seconds: float = 900.0
    predictive_artifact_replacement_min_gain: float = 2.0
    predictive_artifact_replacement_max_victims: int = 3

    def __post_init__(self) -> None:
        if not 0 <= self.memory_headroom_fraction < 1:
            raise ValueError("memory_headroom_fraction must be in [0, 1)")
        if any(
            not math.isfinite(value) or value < 0
            for value in (
                self.demand_headroom_fraction,
                self.node_ttl_seconds,
                self.max_future_clock_skew_seconds,
                self.model_failure_penalty,
                self.performance_ttl_seconds,
                self.max_predictive_lookahead_seconds,
                self.predictive_artifact_ttl_seconds,
                self.predictive_artifact_replacement_min_age_seconds,
            )
        ):
            raise ValueError("planner weights and TTL must be finite and non-negative")
        if (
            not math.isfinite(self.predictive_growth_limit)
            or self.predictive_growth_limit < 1
        ):
            raise ValueError("predictive_growth_limit must be finite and at least 1")
        if self.queue_items_per_replica < 1:
            raise ValueError("queue_items_per_replica must be positive")
        if (
            isinstance(self.performance_full_confidence_samples, bool)
            or not isinstance(self.performance_full_confidence_samples, int)
            or not 1 <= self.performance_full_confidence_samples <= MAX_COUNTER
        ):
            raise ValueError(
                f"performance_full_confidence_samples must be in [1, {MAX_COUNTER}]"
            )
        if (
            isinstance(self.max_staged_preemptions, bool)
            or not isinstance(self.max_staged_preemptions, int)
            or not 1 <= self.max_staged_preemptions <= MAX_COUNTER
        ):
            raise ValueError(f"max_staged_preemptions must be in [1, {MAX_COUNTER}]")
        if (
            isinstance(self.max_predictive_artifact_prefetches, bool)
            or not isinstance(self.max_predictive_artifact_prefetches, int)
            or not 0 <= self.max_predictive_artifact_prefetches <= MAX_COUNTER
        ):
            raise ValueError(
                "max_predictive_artifact_prefetches must be in "
                f"[0, {MAX_COUNTER}]"
            )
        if (
            isinstance(self.predictive_artifact_disk_reserve_mb, bool)
            or not isinstance(self.predictive_artifact_disk_reserve_mb, int)
            or not 0 <= self.predictive_artifact_disk_reserve_mb <= MAX_MEMORY_MB
        ):
            raise ValueError(
                "predictive_artifact_disk_reserve_mb must be in "
                f"[0, {MAX_MEMORY_MB}]"
            )
        if (
            isinstance(self.max_predictive_artifact_evictions, bool)
            or not isinstance(self.max_predictive_artifact_evictions, int)
            or not 0 <= self.max_predictive_artifact_evictions <= MAX_COUNTER
        ):
            raise ValueError(
                "max_predictive_artifact_evictions must be in "
                f"[0, {MAX_COUNTER}]"
            )
        if (
            not math.isfinite(self.predictive_artifact_replacement_min_gain)
            or self.predictive_artifact_replacement_min_gain < 1
        ):
            raise ValueError(
                "predictive_artifact_replacement_min_gain must be finite and at least 1"
            )
        if (
            isinstance(self.predictive_artifact_replacement_max_victims, bool)
            or not isinstance(self.predictive_artifact_replacement_max_victims, int)
            or not 1 <= self.predictive_artifact_replacement_max_victims <= 8
        ):
            raise ValueError(
                "predictive_artifact_replacement_max_victims must be in [1, 8]"
            )
        if (
            not math.isfinite(self.latency_pressure_limit)
            or not math.isfinite(self.error_pressure_limit)
            or self.latency_pressure_limit < 1
            or self.error_pressure_limit < 0
        ):
            raise ValueError("pressure limits are invalid")
        if not 0 < self.throttled_capacity_fraction <= 1:
            raise ValueError("throttled_capacity_fraction must be in (0, 1]")


_MAX_REPACK_SEARCH_STATES = 10_000
_MAX_PREDICTIVE_REPLACEMENT_CANDIDATES = 16
_MAX_REPACK_DEPTH = 64
# A ready-residency preference is intentionally strong, but it must not strand a demanded model
# whose only capable host is occupied by a flexible model that can move elsewhere. This penalty is
# applied only while another desired model has strictly fewer future-compatible hosts.
_SCARCE_HOST_OPPORTUNITY_PENALTY = 200_000.0
# Repacking delays placement by at least one controller wave, so require a material improvement
# over an immediately usable host. Candidate scores already account for load/warm time, live
# traffic, hardware, and fit; this guard prevents marginal score noise from causing churn.
_PROACTIVE_REPACK_SCORE_MARGIN = 500.0
_MAX_PORTFOLIO_PREEMPTION_PATHS = 16
_PairSeconds = tuple[tuple[tuple[str, str], float], ...]


@dataclass(slots=True)
class _RepackSearchState:
    """Bound one deterministic augmenting search and prune placement cycles."""

    max_steps: int = _MAX_REPACK_SEARCH_STATES
    explored_steps: int = 0
    visited: set[
        tuple[
            tuple[tuple[str, int, int], ...],
            tuple[tuple[str, str, int], ...],
        ]
    ] = field(default_factory=set)

    def consume_step(self) -> bool:
        if self.explored_steps >= self.max_steps:
            return False
        self.explored_steps += 1
        return True


@dataclass(frozen=True, slots=True)
class _PreemptionCandidate:
    sort_key: tuple[int, int, int, int, int, float, float, int, int, str]
    node: NodeSnapshot
    victims: tuple[ModelResidency, ...]
    displaced_assignments: tuple[PlacementAssignment, ...]


@dataclass(slots=True)
class _PreemptionSearchCache:
    """One beneficiary's independent candidate sets, built lazily once per plan."""

    candidates: list[_PreemptionCandidate] | None = None


@dataclass(frozen=True, slots=True)
class _PendingReplica:
    model_id: str
    replica_index: int
    domain_floor: int


@dataclass(slots=True)
class _PlanTopologyContext:
    """Reusable hard fleet facts for counterfactual plans over one exact snapshot."""

    timestamp: float
    policy: PlannerPolicy
    nodes: tuple[NodeSnapshot, ...]
    models: tuple[ModelProfile, ...]
    compatibility: dict[tuple[str, str, bool], str | None] = field(default_factory=dict)
    runtime_requirements: dict[tuple[str, str], bool] = field(default_factory=dict)
    runtime_memory: dict[tuple[str, str], int] = field(default_factory=dict)
    artifact_disk: dict[tuple[str, str], int] = field(default_factory=dict)
    future_eligible_nodes: dict[str, frozenset[str]] | None = None
    eligible_host_counts: dict[str, int] | None = None
    startup_horizons: dict[
        tuple[_PairSeconds, _PairSeconds], dict[str, float]
    ] = field(default_factory=dict)

    def matches(
        self,
        nodes: list[NodeSnapshot],
        models: list[ModelProfile],
        timestamp: float,
        policy: PlannerPolicy,
    ) -> bool:
        # Retain the referenced objects in this context, so an id cannot be recycled while the
        # cache is live. Identity matching is deliberately stricter and cheaper than deep hashing
        # a complete fleet on every counterfactual plan.
        return bool(
            self.timestamp == timestamp
            and self.policy == policy
            and len(self.nodes) == len(nodes)
            and len(self.models) == len(models)
            and all(cached is current for cached, current in zip(self.nodes, nodes))
            and all(cached is current for cached, current in zip(self.models, models))
        )


class PlacementPlanner:
    def __init__(self, policy: PlannerPolicy | None = None) -> None:
        self.policy = policy or PlannerPolicy()
        self._plan_topology_context: _PlanTopologyContext | None = None

    def _topology_context(
        self,
        nodes: list[NodeSnapshot],
        models: list[ModelProfile],
        timestamp: float,
    ) -> _PlanTopologyContext:
        cached = self._plan_topology_context
        if cached is not None and cached.matches(nodes, models, timestamp, self.policy):
            return cached
        result = _PlanTopologyContext(
            timestamp=timestamp,
            policy=self.policy,
            nodes=tuple(nodes),
            models=tuple(models),
        )
        self._plan_topology_context = result
        return result

    def portfolio_placement_hints(
        self,
        nodes: Iterable[NodeSnapshot],
        models: Iterable[ModelProfile],
        *,
        now: float | None = None,
        startup_seconds: Mapping[tuple[str, str], float] | None = None,
        load_seconds: Mapping[tuple[str, str], float] | None = None,
    ) -> dict[str, dict[str, object]]:
        """Summarize which portfolio models can occupy a live node right now.

        Portfolio choice happens before ordinary placement, so it needs a bounded feasibility
        filter or an attractive but impossible model can suppress every usable fallback. This is a
        single fleet scan per model using the same hard compatibility, memory, model-slot, and
        colocation rules as placement. It does not promise that globally contended capacity will be
        awarded to the candidate; the normal fair planner remains authoritative for that decision.
        """

        timestamp = time.time() if now is None else float(now)
        if not math.isfinite(timestamp) or timestamp < 0:
            raise ValueError("now must be finite and non-negative")
        node_list = tuple(sorted(nodes, key=lambda item: item.node_id))
        model_list = tuple(sorted(models, key=lambda item: item.model_id))
        _require_unique((node.node_id for node in node_list), "node")
        _require_unique((model.model_id for model in model_list), "model")
        profile_by_id = {model.model_id: model for model in model_list}
        warm_by_pair = _validated_startup_seconds(startup_seconds)
        load_by_pair = _validated_load_seconds(load_seconds)
        hints: dict[str, dict[str, object]] = {}
        relocation_cache: dict[tuple[str, str, frozenset[str]], str] = {}

        def relocation_target(
            victim: ModelProfile,
            *,
            blocked_node_id: str,
            excluded_node_ids: frozenset[str] = frozenset(),
        ) -> str:
            """Find a host that can receive a required victim before its current copy drains."""

            cache_key = (victim.model_id, blocked_node_id, excluded_node_ids)
            cached = relocation_cache.get(cache_key)
            if cached is not None:
                return cached
            candidates: list[tuple[float, int, str]] = []
            for candidate_node in node_list:
                if (
                    candidate_node.node_id == blocked_node_id
                    or candidate_node.node_id in excluded_node_ids
                ):
                    continue
                residency = candidate_node.residency(victim.model_id)
                for_new = _requires_new_runtime(residency, victim)
                reason = _ineligible_reason(
                    candidate_node,
                    victim,
                    timestamp,
                    self.policy,
                    for_new=for_new,
                )
                allocatable = candidate_node.capacity_mb - candidate_node.reserved_mb
                if candidate_node.state == NodeState.THROTTLED:
                    allocatable *= self.policy.throttled_capacity_fraction
                allocatable = max(0, math.floor(allocatable))
                if reason is None and victim.memory_for(candidate_node.runtimes) > allocatable:
                    reason = "model exceeds allocatable memory"
                if reason is not None or candidate_node.max_models == 0:
                    continue
                if (
                    residency is not None
                    and residency.state == ResidencyState.READY
                    and not residency.managed
                ):
                    # Reconciliation requires an allocator-owned replacement before removing a
                    # required managed baseline. External inventory cannot prove that boundary.
                    continue
                if (
                    _portfolio_dynamic_fit_reason(
                        candidate_node,
                        victim,
                        profile_by_id,
                        self.policy,
                    )
                    is not None
                ):
                    continue
                candidates.append(
                    (
                        _portfolio_startup_seconds(
                            candidate_node,
                            residency,
                            victim,
                            warm_by_pair,
                            load_by_pair,
                        ),
                        candidate_node.host_priority,
                        candidate_node.node_id,
                    )
                )
            result = min(candidates)[2] if candidates else ""
            relocation_cache[cache_key] = result
            return result

        for model in model_list:
            rejected: Counter[str] = Counter()
            eligible: list[tuple[float, int, str]] = []
            preemptible: list[
                tuple[
                    float,
                    int,
                    str,
                    tuple[str, ...],
                    tuple[tuple[str, str], ...],
                ]
            ] = []
            hard_compatible_nodes = 0
            if model.max_replicas <= 0:
                rejected["model is disabled"] += 1
            else:
                for node in node_list:
                    residency = node.residency(model.model_id)
                    for_new = _requires_new_runtime(residency, model)
                    reason = _ineligible_reason(
                        node,
                        model,
                        timestamp,
                        self.policy,
                        for_new=for_new,
                    )
                    allocatable = node.capacity_mb - node.reserved_mb
                    if node.state == NodeState.THROTTLED:
                        allocatable *= self.policy.throttled_capacity_fraction
                    allocatable = max(0, math.floor(allocatable))
                    if reason is None and model.memory_for(node.runtimes) > allocatable:
                        reason = "model exceeds allocatable memory"
                    if (
                        reason is None
                        and node.max_models is not None
                        and node.max_models == 0
                    ):
                        reason = "model slots are disabled"
                    if reason is not None:
                        rejected[reason] += 1
                        continue
                    hard_compatible_nodes += 1

                    reason = _portfolio_dynamic_fit_reason(
                        node,
                        model,
                        profile_by_id,
                        self.policy,
                    )

                    startup_seconds = _portfolio_startup_seconds(
                        node,
                        residency,
                        model,
                        warm_by_pair,
                        load_by_pair,
                    )
                    if reason is None:
                        eligible.append(
                            (
                                float(startup_seconds),
                                node.host_priority,
                                node.node_id,
                            )
                        )
                        continue
                    rejected[reason] += 1

                    removable: list[tuple[ModelResidency, str]] = []
                    for item in node.residencies:
                        victim = profile_by_id.get(item.model_id)
                        if (
                            item.model_id == model.model_id
                            or _adds_model_slot(item)
                            or item.state
                            not in (
                                ResidencyState.READY,
                                ResidencyState.DRAINING,
                                ResidencyState.FAILED,
                            )
                            or not item.managed
                            or item.pinned
                            or node.manually_managed
                            or victim is None
                            or victim.pinned_nodes
                            or victim.priority > model.priority
                        ):
                            continue
                        destination = ""
                        if victim.min_replicas > 0:
                            destination = relocation_target(
                                victim,
                                blocked_node_id=node.node_id,
                            )
                            if not destination:
                                continue
                        removable.append((item, destination))
                    removable.sort(
                        key=lambda pair: (
                            profile_by_id[pair[0].model_id].priority,
                            pair[0].active_requests,
                            _preemption_state_cost(pair[0].state),
                            -pair[0].memory_mb,
                            pair[0].model_id,
                        )
                    )
                    selected: list[ModelResidency] = []
                    relocations: list[tuple[str, str]] = []
                    relocation_nodes: set[str] = set()
                    for victim_residency, destination in removable:
                        if destination and destination in relocation_nodes:
                            continue
                        selected.append(victim_residency)
                        if destination:
                            relocations.append((victim_residency.model_id, destination))
                            relocation_nodes.add(destination)
                        projected = replace(
                            node,
                            residencies=tuple(
                                item for item in node.residencies if item not in selected
                            ),
                        )
                        if (
                            _portfolio_dynamic_fit_reason(
                                projected,
                                model,
                                profile_by_id,
                                self.policy,
                            )
                            is None
                        ):
                            preemptible.append(
                                (
                                    float(startup_seconds),
                                    node.host_priority,
                                    node.node_id,
                                    tuple(item.model_id for item in selected),
                                    tuple(relocations),
                                )
                            )
                            break

            if eligible:
                startup_seconds, host_priority, node_id = min(eligible)
                hints[model.model_id] = {
                    "model_id": model.model_id,
                    "feasible": True,
                    "feasible_now": True,
                    "hard_compatible": True,
                    "feasible_after_preemption": False,
                    "eligible_nodes": len(eligible),
                    "best_node_id": node_id,
                    "host_priority": host_priority,
                    "startup_seconds": startup_seconds,
                    "reason": "fleet-feasible",
                }
            elif preemptible:
                ordered_paths = sorted(preemptible)
                startup_seconds, host_priority, node_id, victims, relocations = ordered_paths[0]
                hints[model.model_id] = {
                    "model_id": model.model_id,
                    # Preserve the old field as current-headroom feasibility. Callers must opt in
                    # to the separately explained preemption path.
                    "feasible": False,
                    "feasible_now": False,
                    "hard_compatible": True,
                    "feasible_after_preemption": True,
                    "eligible_nodes": 0,
                    "preemption_eligible_nodes": len(preemptible),
                    "best_node_id": node_id,
                    "host_priority": host_priority,
                    "startup_seconds": startup_seconds,
                    "preemption_victims": list(victims),
                    "relocation_targets": [
                        {"model_id": victim, "node_id": destination}
                        for victim, destination in relocations
                    ],
                    "preemption_paths": [
                        {
                            "startup_seconds": path_startup,
                            "host_priority": path_priority,
                            "best_node_id": path_node,
                            "preemption_victims": list(path_victims),
                            "relocation_targets": [
                                {"model_id": victim, "node_id": destination}
                                for victim, destination in path_relocations
                            ],
                        }
                        for (
                            path_startup,
                            path_priority,
                            path_node,
                            path_victims,
                            path_relocations,
                        ) in ordered_paths[:_MAX_PORTFOLIO_PREEMPTION_PATHS]
                    ],
                    "reason": "fleet-feasible after planner-authorized relocation/preemption",
                }
            else:
                reason = "no live node is currently eligible"
                if rejected:
                    reason += ": " + "; ".join(
                        f"{item} ({count})"
                        for item, count in sorted(
                            rejected.items(),
                            key=lambda pair: (-pair[1], pair[0]),
                        )[:3]
                    )
                hints[model.model_id] = {
                    "model_id": model.model_id,
                    "feasible": False,
                    "feasible_now": False,
                    "hard_compatible": bool(hard_compatible_nodes),
                    "feasible_after_preemption": False,
                    "eligible_nodes": 0,
                    "best_node_id": "",
                    "host_priority": 0,
                    "startup_seconds": 0.0,
                    "reason": reason,
                }
        return hints

    def plan(
        self,
        nodes: Iterable[NodeSnapshot],
        models: Iterable[ModelProfile],
        forecasts: Iterable[DemandForecast] = (),
        *,
        now: float | None = None,
        startup_seconds: Mapping[tuple[str, str], float] | None = None,
        load_seconds: Mapping[tuple[str, str], float] | None = None,
        compute_input_digest: bool = True,
    ) -> PlacementPlan:
        timestamp = time.time() if now is None else float(now)
        if not math.isfinite(timestamp) or timestamp < 0:
            raise ValueError("now must be finite and non-negative")
        if not isinstance(compute_input_digest, bool):
            raise ValueError("compute_input_digest must be boolean")
        node_list = sorted(nodes, key=lambda item: item.node_id)
        model_list = sorted(models, key=lambda item: item.model_id)
        _require_unique((node.node_id for node in node_list), "node")
        _require_unique((model.model_id for model in model_list), "model")
        forecast_list = sorted(forecasts, key=lambda item: item.model_id)
        _require_unique((item.model_id for item in forecast_list), "forecast model")
        topology_context = self._topology_context(node_list, model_list, timestamp)
        forecast_by_model = {item.model_id: item for item in forecast_list}
        startup_by_pair = _validated_startup_seconds(startup_seconds)
        load_by_pair = _validated_load_seconds(load_seconds)
        startup_horizon_key = (
            tuple(sorted(startup_by_pair.items())),
            tuple(sorted(load_by_pair.items())),
        )
        startup_horizons = topology_context.startup_horizons.setdefault(
            startup_horizon_key,
            {},
        )
        capacity = {}
        placement_budget: dict[str, int] = {}
        for node in node_list:
            allocatable = node.capacity_mb - node.reserved_mb
            usable = allocatable * (1.0 - self.policy.memory_headroom_fraction)
            if node.state == NodeState.THROTTLED:
                allocatable *= self.policy.throttled_capacity_fraction
                usable *= self.policy.throttled_capacity_fraction
            capacity[node.node_id] = max(0, math.floor(usable))
            placement_budget[node.node_id] = max(0, math.floor(allocatable))
        # ``capacity`` becomes the physical *incremental* memory ledger after live
        # residencies are reserved below. Keep a separate immutable raw budget for the
        # complete desired placement. Headroom is an admission margin, so an incumbent may
        # continue to occupy it and perform a zero-allocation state transition. It may not,
        # however, remain selected once non-model reserve or thermal derating has made the
        # combined resident footprint physically unsafe.
        desired_memory: dict[str, int] = {node.node_id: 0 for node in node_list}
        occupied_models: dict[str, int] = {node.node_id: 0 for node in node_list}
        desired_model_slots: dict[str, int] = {node.node_id: 0 for node in node_list}
        disk_remaining: dict[str, int | None] = {
            node.node_id: node.disk_available_mb for node in node_list
        }
        assignments: list[PlacementAssignment] = []
        assigned_pairs: set[tuple[str, str]] = set()
        assigned_domains: dict[str, set[str]] = {}
        unsatisfied: list[UnsatisfiedConstraint] = []
        preemptions: list[PlacementPreemption] = []

        # Reserve every process that is resident *now*, including allocator-owned models that the
        # new plan intends to remove.  Reconciliation warms replacements before draining obsolete
        # work, so pretending that memory is already free would produce an unsafe transient
        # overcommit. Selection happens below for both managed and external ready residencies, so
        # external inventory cannot silently inflate the desired plan above max_replicas.
        for node in node_list:
            for residency in node.residencies:
                if not _adds_model_slot(residency):
                    capacity[node.node_id] = max(
                        0, capacity[node.node_id] - residency.memory_mb
                    )
                    occupied_models[node.node_id] += 1

        def startup_horizon(model: ModelProfile) -> float:
            if model.model_id not in startup_horizons:
                startup_horizons[model.model_id] = _next_replica_startup_seconds(
                    model,
                    node_list,
                    startup_by_pair,
                    load_by_pair,
                    now=timestamp,
                    policy=self.policy,
                )
            return startup_horizons[model.model_id]

        desired_by_model = {
            model.model_id: desired_replica_count(
                model,
                forecast_by_model.get(model.model_id),
                nodes=node_list,
                now=timestamp,
                policy=self.policy,
                startup_horizon_seconds=startup_horizon(model),
            )
            for model in model_list
        }
        profile_by_id = {item.model_id: item for item in model_list}
        compatibility_cache = topology_context.compatibility
        runtime_requirement_cache = topology_context.runtime_requirements
        runtime_memory_cache = topology_context.runtime_memory
        artifact_disk_cache = topology_context.artifact_disk
        colocation_policy_active = any(
            profile.max_colocated_models or profile.colocation_excludes
            for profile in model_list
        )
        # Augmenting placement repair is deliberately bounded because its backtracking space is
        # exponential. Preserve the full search for the 2/4/8-node operating envelope while
        # scaling the per-replica budget down for very large node×model matrices. Exhaustion is
        # fail-safe: the planner reports an unsatisfied replica instead of emitting an unsafe plan
        # or blocking the controller for seconds.
        repack_search_steps = min(
            _MAX_REPACK_SEARCH_STATES,
            max(
                512,
                1_000_000 // max(1, len(node_list) * len(model_list)),
            ),
        )
        fit_cache: dict[tuple[str, str, int, int, int, int, int], bool] = {}

        def requires_new_runtime(node: NodeSnapshot, model: ModelProfile) -> bool:
            key = (node.node_id, model.model_id)
            if key not in runtime_requirement_cache:
                runtime_requirement_cache[key] = _requires_new_runtime(
                    node.residency(model.model_id),
                    model,
                )
            return runtime_requirement_cache[key]

        def runtime_memory(node: NodeSnapshot, model: ModelProfile) -> int:
            key = (node.node_id, model.model_id)
            if key not in runtime_memory_cache:
                runtime_memory_cache[key] = model.memory_for(node.runtimes)
            return runtime_memory_cache[key]

        def compatibility(
            node: NodeSnapshot,
            model: ModelProfile,
            *,
            for_new: bool,
        ) -> str | None:
            key = (node.node_id, model.model_id, for_new)
            if key not in compatibility_cache:
                compatibility_cache[key] = _ineligible_reason(
                    node,
                    model,
                    timestamp,
                    self.policy,
                    for_new=for_new,
                )
            return compatibility_cache[key]

        def artifact_disk_cost(node: NodeSnapshot, model: ModelProfile) -> int:
            key = (node.node_id, model.model_id)
            if key not in artifact_disk_cache:
                artifact_disk_cache[key] = _artifact_load_disk_mb(node, model)
            return artifact_disk_cache[key]

        def fits_node(node: NodeSnapshot, model: ModelProfile) -> bool:
            model_memory_mb = runtime_memory(node, model)
            if (
                desired_memory[node.node_id] + model_memory_mb
                > placement_budget[node.node_id]
            ):
                return False
            remaining_disk = disk_remaining[node.node_id]
            if (
                remaining_disk is not None
                and artifact_disk_cost(node, model) > remaining_disk
            ):
                return False
            if colocation_policy_active:
                return _fits(
                    node,
                    model,
                    capacity,
                    occupied_models,
                    desired_model_slots,
                    assignments,
                    profile_by_id,
                )
            key = (
                node.node_id,
                model.model_id,
                capacity[node.node_id],
                desired_memory[node.node_id],
                occupied_models[node.node_id],
                desired_model_slots[node.node_id],
                -1 if disk_remaining[node.node_id] is None else disk_remaining[node.node_id],
            )
            if key not in fit_cache:
                fit_cache[key] = _fits(
                    node,
                    model,
                    capacity,
                    occupied_models,
                    desired_model_slots,
                    assignments,
                    profile_by_id,
                )
            return fit_cache[key]

        # Higher-priority and larger models place first.  Larger-first is the standard bin-packing
        # guard against a collection of small replicas fragmenting every host before a large model.
        def eligible_host_count(model: ModelProfile) -> int:
            return sum(
                compatibility(
                    node,
                    model,
                    for_new=requires_new_runtime(node, model),
                )
                is None
                and fits_node(node, model)
                for node in node_list
            )

        pin_order = sorted(
            model_list,
            key=lambda item: (-item.priority, -item.maximum_memory_mb, item.model_id),
        )
        pinned_successes_by_model = {model.model_id: 0 for model in model_list}

        # Reserve every explicit pin across the fleet before any ordinary scored placement. Without
        # this global phase, an earlier model's optional replica could consume the last model slot or
        # memory on a node that a later model is hard-pinned to.
        for model in pin_order:
            domains = assigned_domains.setdefault(model.model_id, set())
            for node_id in model.pinned_nodes:
                if (model.model_id, node_id) in assigned_pairs:
                    pinned_successes_by_model[model.model_id] += 1
                    continue
                node = next(
                    (item for item in node_list if item.node_id == node_id), None
                )
                for_new = (
                    requires_new_runtime(node, model) if node is not None else True
                )
                reason = (
                    compatibility(node, model, for_new=for_new)
                    if node is not None
                    else "node does not exist"
                )
                if node is None or reason is not None or not fits_node(node, model):
                    unsatisfied.append(
                        UnsatisfiedConstraint(
                            model_id=model.model_id,
                            code="pinned_node_unavailable",
                            message=f"Pinned node {node_id!r} cannot host the model"
                            + (f": {reason}" if reason else ": insufficient capacity"),
                            missing_replicas=1,
                        )
                    )
                    continue
                assignment = _assignment(
                    model,
                    node,
                    index=pinned_successes_by_model[model.model_id],
                    score=2_000_000.0,
                    reasons=("pinned placement",),
                )
                _place(
                    assignment,
                    node,
                    assignments,
                    assigned_pairs,
                    domains,
                    capacity,
                    occupied_models,
                    desired_model_slots,
                    desired_memory,
                    disk_remaining,
                    artifact_disk_cost(node, model),
                )
                pinned_successes_by_model[model.model_id] += 1

        # Recompute scarcity after hard pins have consumed their declared capacity. Within one
        # administrator-priority class, constrained models go before flexible models.
        if topology_context.eligible_host_counts is None:
            topology_context.eligible_host_counts = {
                model.model_id: eligible_host_count(model) for model in model_list
            }
        eligible_host_counts = topology_context.eligible_host_counts
        order = sorted(
            model_list,
            key=lambda item: (
                -item.priority,
                -_placement_demand_urgency(
                    item,
                    forecast_by_model.get(item.model_id),
                ),
                eligible_host_counts[item.model_id],
                -item.maximum_memory_mb,
                item.model_id,
            ),
        )
        demand_urgency_by_model = {
            model.model_id: _placement_demand_urgency(
                model,
                forecast_by_model.get(model.model_id),
            )
            for model in model_list
        }
        node_by_id = {item.node_id: item for item in node_list}
        # Moving an assignment can change net resource use when runtimes have different model
        # footprints or a destination already has resident weights. Only the simple homogeneous
        # case supports aggregate no-capacity proofs; every heterogeneous case keeps the full
        # augmenting search.
        repacking_conserves_aggregate_resources = not any(
            node.residencies for node in node_list
        ) and all(
            len({profile.memory_for(node.runtimes) for node in node_list}) <= 1
            for profile in model_list
        )

        # Evaluate hard *future* fit without current occupancy. The ordinary capacity ledger must
        # reserve live processes for make-before-break safety, but placement choice also needs to
        # know that putting a tiny flexible model on the only large host can make a larger demanded
        # model impossible. The desired plan may move the flexible model first; reconciliation then
        # proves its replacement ready before draining the old residency.
        # Dormant catalog models still carry topology option value. This does not reserve or idle
        # capacity: zero-demand models are never placed. It only preserves uniquely capable hosts
        # for demand that may arrive during the model's startup horizon.
        if topology_context.future_eligible_nodes is None:
            topology_context.future_eligible_nodes = {
                candidate_model.model_id: frozenset(
                    candidate_node.node_id
                    for candidate_node in node_list
                    if compatibility(
                        candidate_node,
                        candidate_model,
                        for_new=requires_new_runtime(candidate_node, candidate_model),
                    )
                    is None
                    and runtime_memory(candidate_node, candidate_model)
                    <= placement_budget[candidate_node.node_id]
                    and candidate_node.max_models != 0
                )
                for candidate_model in model_list
            }
        future_eligible_nodes = topology_context.future_eligible_nodes
        scarce_host_claims: dict[tuple[str, str], tuple[str, ...]] = {}
        scarce_host_excess_claims: dict[tuple[str, str], int] = {}
        scarce_host_preservations: dict[tuple[str, str], tuple[str, ...]] = {}
        eligible_models_by_node: dict[str, list[str]] = {
            node.node_id: [] for node in node_list
        }
        for model_id, eligible_nodes in future_eligible_nodes.items():
            for node_id in eligible_nodes:
                eligible_models_by_node[node_id].append(model_id)
        for candidate_model in model_list:
            candidate_nodes = future_eligible_nodes.get(candidate_model.model_id, frozenset())
            if len(candidate_nodes) <= 1:
                continue
            for node_id in candidate_nodes:
                constrained = tuple(
                    sorted(
                        other_model_id
                        for other_model_id in eligible_models_by_node[node_id]
                        if other_model_id != candidate_model.model_id
                        and len(future_eligible_nodes[other_model_id])
                        < len(candidate_nodes)
                    )
                )
                if constrained:
                    scarce_host_claims[(candidate_model.model_id, node_id)] = constrained
            # For a proposed alternative host, the preserved models are exactly the narrower
            # peers that overlap at least one of this model's other hosts but cannot use the
            # alternative itself. The former host-pair expansion computed the same union once per
            # scarce host, turning a set relationship into O(models² × hosts²) repeated scans.
            narrower_overlapping = tuple(
                (other_model_id, other_nodes)
                for other_model_id, other_nodes in future_eligible_nodes.items()
                if other_model_id != candidate_model.model_id
                and len(other_nodes) < len(candidate_nodes)
                and not candidate_nodes.isdisjoint(other_nodes)
            )
            for alternative_node_id in candidate_nodes:
                preserved = tuple(
                    sorted(
                        other_model_id
                        for other_model_id, other_nodes in narrower_overlapping
                        if alternative_node_id not in other_nodes
                    )
                )
                if preserved:
                    scarce_host_preservations[
                        (candidate_model.model_id, alternative_node_id)
                    ] = preserved
        # Scarcity is relative. If every feasible host for a flexible model protects one equally
        # constrained peer, moving a healthy incumbent preserves nothing and merely changes which
        # peer names the penalty. Normalize by the least-scarce feasible alternative so only a real
        # reduction in opportunity cost can overcome ready-residency stickiness.
        for candidate_model_id, candidate_nodes in future_eligible_nodes.items():
            if not candidate_nodes:
                continue
            minimum_claims = min(
                len(scarce_host_claims.get((candidate_model_id, node_id), ()))
                for node_id in candidate_nodes
            )
            for node_id in candidate_nodes:
                excess = (
                    len(scarce_host_claims.get((candidate_model_id, node_id), ()))
                    - minimum_claims
                )
                if excess > 0:
                    scarce_host_excess_claims[(candidate_model_id, node_id)] = excess
        def snapshot_placement_state() -> tuple[
            list[PlacementAssignment],
            set[tuple[str, str]],
            dict[str, set[str]],
            dict[str, int],
            dict[str, int],
            dict[str, int],
            dict[str, int],
            dict[str, int | None],
        ]:
            return (
                list(assignments),
                set(assigned_pairs),
                {key: set(value) for key, value in assigned_domains.items()},
                dict(capacity),
                dict(occupied_models),
                dict(desired_model_slots),
                dict(desired_memory),
                dict(disk_remaining),
            )

        def restore_placement_state(
            state: tuple[
                list[PlacementAssignment],
                set[tuple[str, str]],
                dict[str, set[str]],
                dict[str, int],
                dict[str, int],
                dict[str, int],
                dict[str, int],
                dict[str, int | None],
            ],
        ) -> None:
            (
                saved_assignments,
                saved_pairs,
                saved_domains,
                saved_capacity,
                saved_occupied,
                saved_slots,
                saved_desired_memory,
                saved_disk_remaining,
            ) = state
            assignments[:] = saved_assignments
            assigned_pairs.clear()
            assigned_pairs.update(saved_pairs)
            assigned_domains.clear()
            assigned_domains.update(saved_domains)
            capacity.clear()
            capacity.update(saved_capacity)
            occupied_models.clear()
            occupied_models.update(saved_occupied)
            desired_model_slots.clear()
            desired_model_slots.update(saved_slots)
            desired_memory.clear()
            desired_memory.update(saved_desired_memory)
            disk_remaining.clear()
            disk_remaining.update(saved_disk_remaining)

        candidate_score_cache: dict[
            tuple[str, str, int, bool, bool],
            tuple[float, tuple[str, ...]],
        ] = {}

        def score_candidate(
            node: NodeSnapshot,
            model: ModelProfile,
            remaining_mb: int,
            domains: set[str],
            *,
            need_new_domain: bool,
        ) -> tuple[float, tuple[str, ...]]:
            domain_present = (node.failure_domain or node.node_id) in domains
            key = (
                node.node_id,
                model.model_id,
                remaining_mb,
                domain_present,
                need_new_domain,
            )
            cached = candidate_score_cache.get(key)
            if cached is None:
                cached = _candidate_score(
                    node,
                    model,
                    remaining_mb,
                    domains,
                    self.policy,
                    forecast=forecast_by_model.get(model.model_id),
                    now=timestamp,
                    need_new_domain=need_new_domain,
                    startup_seconds=startup_by_pair,
                    load_seconds=load_by_pair,
                )
                constrained = scarce_host_claims.get((model.model_id, node.node_id), ())
                excess_scarcity = scarce_host_excess_claims.get(
                    (model.model_id, node.node_id), 0
                )
                if constrained and excess_scarcity:
                    score, reasons = cached
                    cached = (
                        score
                        - _SCARCE_HOST_OPPORTUNITY_PENALTY * excess_scarcity,
                        (
                            *reasons,
                            "scarce host also required by " + ", ".join(constrained[:3]),
                        ),
                    )
                preserved = scarce_host_preservations.get(
                    (model.model_id, node.node_id), ()
                )
                if preserved:
                    score, reasons = cached
                    cached = (
                        score,
                        (
                            *reasons,
                            "preserves scarce host for " + ", ".join(preserved[:3]),
                        ),
                    )
                candidate_score_cache[key] = cached
            return cached

        def remove_regular_assignment(assignment: PlacementAssignment) -> None:
            assignment_node = node_by_id[assignment.node_id]
            residency = assignment_node.residency(assignment.model_id)
            assignments.remove(assignment)
            assigned_pairs.remove((assignment.model_id, assignment.node_id))
            capacity[assignment.node_id] += _incremental_memory_mb(
                residency,
                assignment.memory_mb,
            )
            desired_memory[assignment.node_id] -= assignment.memory_mb
            desired_model_slots[assignment.node_id] -= 1
            if _adds_model_slot(residency):
                occupied_models[assignment.node_id] -= 1
            remaining_disk = disk_remaining[assignment.node_id]
            if remaining_disk is not None:
                disk_remaining[assignment.node_id] = remaining_disk + artifact_disk_cost(
                    assignment_node,
                    profile_by_id[assignment.model_id],
                )
            assigned_domains[assignment.model_id] = {
                node_by_id[item.node_id].failure_domain or item.node_id
                for item in assignments
                if item.model_id == assignment.model_id
            }

        def proactive_repack_is_better(
            model: ModelProfile,
            immediate_score: float,
            domains: set[str],
        ) -> bool:
            """Prefer a materially better blocked host when every victim is already relocated."""

            assignment_counts = Counter(item.model_id for item in assignments)
            for candidate_node in node_list:
                if (model.model_id, candidate_node.node_id) in assigned_pairs:
                    continue
                if compatibility(
                    candidate_node,
                    model,
                    for_new=requires_new_runtime(candidate_node, model),
                ) is not None:
                    continue
                victims: list[ModelResidency] = []
                relocation_delay_seconds = 0.0
                for residency in candidate_node.residencies:
                    if _adds_model_slot(residency):
                        continue
                    victim = profile_by_id.get(residency.model_id)
                    destination = next(
                        (
                            item.node_id
                            for item in assignments
                            if item.model_id == residency.model_id
                            and item.node_id != candidate_node.node_id
                        ),
                        "",
                    )
                    if (
                        residency.model_id == model.model_id
                        or residency.state
                        not in (
                            ResidencyState.READY,
                            ResidencyState.DRAINING,
                        )
                        or not residency.managed
                        or residency.pinned
                        or candidate_node.manually_managed
                        or victim is None
                        or candidate_node.node_id in victim.pinned_nodes
                        or desired_by_model.get(residency.model_id, 0) <= 0
                        or assignment_counts[residency.model_id]
                        < desired_by_model[residency.model_id]
                        or not destination
                    ):
                        victims = []
                        break
                    victims.append(residency)
                    relocation_delay_seconds += startup_by_pair.get(
                        (destination, residency.model_id),
                        victim.warm_seconds,
                    )
                    relocation_delay_seconds += (
                        residency.active_requests * victim.expected_service_seconds
                    )
                if not victims:
                    continue
                projected = replace(
                    candidate_node,
                    residencies=tuple(
                        item for item in candidate_node.residencies if item not in victims
                    ),
                )
                if (
                    _portfolio_dynamic_fit_reason(
                        projected,
                        model,
                        profile_by_id,
                        self.policy,
                    )
                    is not None
                ):
                    continue
                projected_remaining = capacity[candidate_node.node_id] + sum(
                    item.memory_mb for item in victims
                )
                score, _ = score_candidate(
                    candidate_node,
                    model,
                    projected_remaining,
                    domains,
                    need_new_domain=(
                        len(domains)
                        < min(
                            model.min_failure_domains,
                            desired_by_model[model.model_id],
                        )
                    ),
                )
                score -= min(relocation_delay_seconds, 1_000_000_000_000.0) * 20.0
                if score > immediate_score + _PROACTIVE_REPACK_SCORE_MARGIN:
                    return True
            return False

        def place_pending_replicas(
            pending: tuple[_PendingReplica, ...],
            *,
            depth: int,
            search_state: _RepackSearchState,
        ) -> bool:
            if not pending:
                return True
            next_replica = pending[0]
            return try_place_with_repacking(
                profile_by_id[next_replica.model_id],
                next_replica.replica_index,
                depth=depth,
                required_domain_floor=next_replica.domain_floor,
                remaining=pending[1:],
                search_state=search_state,
            )

        def try_place_with_repacking(
            placement_model: ModelProfile,
            replica_index: int,
            *,
            depth: int = 0,
            required_domain_floor: int = 0,
            remaining: tuple[_PendingReplica, ...] = (),
            search_state: _RepackSearchState | None = None,
        ) -> bool:
            """Backtrack through bounded same-priority moves until every displaced replica fits."""

            if depth > _MAX_REPACK_DEPTH:
                return False
            if search_state is None:
                # Each missing replica gets the same scale-aware bounded search effort. A prior
                # failed search or unrelated ineligible inventory cannot change an otherwise
                # identical result.
                search_state = _RepackSearchState(max_steps=repack_search_steps)
            pending_signature = (
                (placement_model.model_id, replica_index, required_domain_floor),
                *(
                    (item.model_id, item.replica_index, item.domain_floor)
                    for item in remaining
                ),
            )
            signature = (
                pending_signature,
                tuple(
                    sorted(
                        (item.model_id, item.node_id, item.replica_index)
                        for item in assignments
                    )
                ),
            )
            if signature in search_state.visited or not search_state.consume_step():
                return False
            search_state.visited.add(signature)

            domains = assigned_domains.setdefault(placement_model.model_id, set())
            candidates: list[tuple[float, str, NodeSnapshot, tuple[str, ...]]] = []
            compatible_nodes: list[NodeSnapshot] = []
            target = desired_by_model[placement_model.model_id]
            for candidate_node in node_list:
                if (placement_model.model_id, candidate_node.node_id) in assigned_pairs:
                    continue
                for_new = requires_new_runtime(candidate_node, placement_model)
                if (
                    compatibility(
                        candidate_node,
                        placement_model,
                        for_new=for_new,
                    )
                    is not None
                ):
                    continue
                compatible_nodes.append(candidate_node)
                if not fits_node(candidate_node, placement_model):
                    continue
                candidate_domain = (
                    candidate_node.failure_domain or candidate_node.node_id
                )
                if len(domains | {candidate_domain}) < required_domain_floor:
                    continue
                score, reasons = score_candidate(
                    candidate_node,
                    placement_model,
                    capacity[candidate_node.node_id],
                    domains,
                    need_new_domain=(
                        len(domains) < min(placement_model.min_failure_domains, target)
                    ),
                )
                candidates.append(
                    (score, candidate_node.node_id, candidate_node, reasons)
                )

            for score, _, selected_node, reasons in sorted(
                candidates,
                key=lambda item: (-item[0], item[1]),
            ):
                if not search_state.consume_step():
                    return False
                state = snapshot_placement_state()
                _place(
                    _assignment(
                        placement_model,
                        selected_node,
                        index=replica_index,
                        score=score,
                        reasons=(*reasons, "augmenting placement repair"),
                    ),
                    selected_node,
                    assignments,
                    assigned_pairs,
                    assigned_domains.setdefault(placement_model.model_id, set()),
                    capacity,
                    occupied_models,
                    desired_model_slots,
                    desired_memory,
                    disk_remaining,
                    artifact_disk_cost(selected_node, placement_model),
                )
                if place_pending_replicas(
                    remaining,
                    depth=depth + 1,
                    search_state=search_state,
                ):
                    return True
                restore_placement_state(state)

            # Relocation conserves total memory and model slots. If the fleet does not have the
            # minimum net resource needed by this additional replica, no sequence of victim moves
            # can help. This admissible lower bound avoids spending the full backtracking budget on
            # a saturated fleet while leaving topology/fragmentation repairs untouched.
            if compatible_nodes and repacking_conserves_aggregate_resources:
                minimum_incremental_mb = min(
                    _incremental_memory_mb(
                        node.residency(placement_model.model_id),
                        placement_model.memory_for(node.runtimes),
                    )
                    for node in compatible_nodes
                )
                if sum(capacity.values()) < minimum_incremental_mb:
                    return False
                if all(
                    _adds_model_slot(node.residency(placement_model.model_id))
                    for node in compatible_nodes
                ) and all(node.max_models is not None for node in node_list):
                    available_model_slots = sum(
                        max(
                            0,
                            min(
                                int(node.max_models)
                                - desired_model_slots[node.node_id],
                                int(node.max_models) - occupied_models[node.node_id],
                            ),
                        )
                        for node in node_list
                    )
                    if available_model_slots < 1:
                        return False

            for target_node in sorted(compatible_nodes, key=lambda item: item.node_id):
                victims = sorted(
                    (
                        item
                        for item in assignments
                        if item.node_id == target_node.node_id
                        and item.model_id != placement_model.model_id
                        and profile_by_id[item.model_id].priority
                        == placement_model.priority
                        and item.node_id
                        not in profile_by_id[item.model_id].pinned_nodes
                        and _incremental_memory_mb(
                            target_node.residency(item.model_id),
                            item.memory_mb,
                        )
                        > 0
                    ),
                    key=lambda item: (item.model_id, item.replica_index),
                )
                # One large incoming replica may need several smaller placements evacuated from the
                # same host. Try the smallest victim sets first and backtrack across every later
                # relocation, while the shared step budget keeps controller latency bounded.
                for victim_count in range(1, len(victims) + 1):
                    for victim_group in combinations(victims, victim_count):
                        if not search_state.consume_step():
                            return False
                        state = snapshot_placement_state()
                        displaced: list[_PendingReplica] = []
                        for victim in victim_group:
                            victim_profile = profile_by_id[victim.model_id]
                            displaced.append(
                                _PendingReplica(
                                    victim.model_id,
                                    victim.replica_index,
                                    min(
                                        len(
                                            assigned_domains.get(victim.model_id, set())
                                        ),
                                        min(
                                            victim_profile.min_failure_domains,
                                            desired_by_model[victim.model_id],
                                        ),
                                    ),
                                )
                            )
                        for victim in victim_group:
                            remove_regular_assignment(victim)
                        if not fits_node(target_node, placement_model):
                            restore_placement_state(state)
                            continue
                        target_domain = (
                            target_node.failure_domain or target_node.node_id
                        )
                        placement_domains = assigned_domains.setdefault(
                            placement_model.model_id,
                            set(),
                        )
                        if (
                            len(placement_domains | {target_domain})
                            < required_domain_floor
                        ):
                            restore_placement_state(state)
                            continue
                        score, reasons = score_candidate(
                            target_node,
                            placement_model,
                            capacity[target_node.node_id],
                            placement_domains,
                            need_new_domain=(
                                len(placement_domains)
                                < min(placement_model.min_failure_domains, target)
                            ),
                        )
                        _place(
                            _assignment(
                                placement_model,
                                target_node,
                                index=replica_index,
                                score=score,
                                reasons=(*reasons, "augmenting placement repair"),
                            ),
                            target_node,
                            assignments,
                            assigned_pairs,
                            placement_domains,
                            capacity,
                            occupied_models,
                            desired_model_slots,
                            desired_memory,
                            disk_remaining,
                            artifact_disk_cost(target_node, placement_model),
                        )
                        pending = (*displaced, *remaining)
                        if place_pending_replicas(
                            pending,
                            depth=depth + 1,
                            search_state=search_state,
                        ) and all(
                            len(assigned_domains.get(item.model_id, set()))
                            >= item.domain_floor
                            for item in displaced
                        ):
                            return True
                        restore_placement_state(state)
            return False

        def placement_goal(model: ModelProfile) -> int:
            target = desired_by_model[model.model_id]
            regular_slots = max(0, target - len(model.pinned_nodes))
            return pinned_successes_by_model[model.model_id] + regular_slots

        isolated_empty_orders: dict[str, tuple[NodeSnapshot, ...]] = {}
        isolated_empty_cursors: dict[str, int] = {}
        isolated_empty_next_indices: dict[str, int] = {}

        def place_next_replica(model: ModelProfile) -> bool:
            target = desired_by_model[model.model_id]
            domains = assigned_domains.setdefault(model.model_id, set())
            cached_order = isolated_empty_orders.get(model.model_id)
            if cached_order is not None:
                cursor = isolated_empty_cursors.get(model.model_id, 0)
                while cursor < len(cached_order):
                    node = cached_order[cursor]
                    cursor += 1
                    isolated_empty_cursors[model.model_id] = cursor
                    if (model.model_id, node.node_id) in assigned_pairs:
                        continue
                    if compatibility(
                        node,
                        model,
                        for_new=requires_new_runtime(node, model),
                    ) is not None or not fits_node(node, model):
                        continue
                    score, reasons = score_candidate(
                        node,
                        model,
                        capacity[node.node_id],
                        domains,
                        need_new_domain=(
                            len(domains) < min(model.min_failure_domains, target)
                        ),
                    )
                    if proactive_repack_is_better(model, score, domains):
                        return False
                    index = isolated_empty_next_indices[model.model_id]
                    isolated_empty_next_indices[model.model_id] = index + 1
                    _place(
                        _assignment(
                            model,
                            node,
                            index=index,
                            score=score,
                            reasons=reasons,
                        ),
                        node,
                        assignments,
                        assigned_pairs,
                        domains,
                        capacity,
                        occupied_models,
                        desired_model_slots,
                        desired_memory,
                        disk_remaining,
                        artifact_disk_cost(node, model),
                    )
                    return True
            candidates: list[tuple[float, str, NodeSnapshot, tuple[str, ...]]] = []
            compatible_new_domain_exists = False
            for node in node_list:
                if (model.model_id, node.node_id) in assigned_pairs:
                    continue
                for_new = requires_new_runtime(node, model)
                reason = compatibility(node, model, for_new=for_new)
                if reason is not None:
                    continue
                if (node.failure_domain or node.node_id) not in domains:
                    compatible_new_domain_exists = True
                if not fits_node(node, model):
                    continue
                score, reasons = score_candidate(
                    node,
                    model,
                    capacity[node.node_id],
                    domains,
                    need_new_domain=(
                        len(domains) < min(model.min_failure_domains, target)
                    ),
                )
                candidates.append((score, node.node_id, node, reasons))
            needed_domains = min(model.min_failure_domains, target)
            needs_new_domain = len(domains) < needed_domains
            if needs_new_domain:
                new_domain_candidates = [
                    candidate
                    for candidate in candidates
                    if (candidate[2].failure_domain or candidate[2].node_id)
                    not in domains
                ]
                if new_domain_candidates:
                    candidates = new_domain_candidates
                elif compatible_new_domain_exists:
                    # A fitting same-domain host is only a capacity fallback. First give the
                    # bounded backtracker a chance to open one additional required domain by
                    # relocating equal-priority, non-pinned work. Requiring only the next
                    # incremental domain keeps a three-domain target achievable one replica at
                    # a time instead of demanding all three from the second placement.
                    index = sum(
                        1 for item in assignments if item.model_id == model.model_id
                    )
                    if try_place_with_repacking(
                        model,
                        index,
                        required_domain_floor=min(needed_domains, len(domains) + 1),
                    ):
                        return True
            if not candidates:
                index = sum(
                    1 for item in assignments if item.model_id == model.model_id
                )
                return try_place_with_repacking(model, index)
            # Highest score wins; node_id ascending is the deterministic final tie-break.
            score, _, node, reasons = min(
                candidates, key=lambda item: (-item[0], item[1])
            )
            if proactive_repack_is_better(model, score, domains):
                # Leave this replica missing for the bounded preemption stage below. It will emit
                # the relocation drain only after reconciliation proves the victim's replacement
                # READY, then place this model on the better host in the following control wave.
                return False
            index = sum(1 for item in assignments if item.model_id == model.model_id)
            assignment = _assignment(
                model, node, index=index, score=score, reasons=reasons
            )
            _place(
                assignment,
                node,
                assignments,
                assigned_pairs,
                domains,
                capacity,
                occupied_models,
                desired_model_slots,
                desired_memory,
                disk_remaining,
                artifact_disk_cost(node, model),
            )
            return True

        def place_isolated_replicas(
            model: ModelProfile,
            *,
            live_incumbents: bool,
        ) -> None:
            """Bulk-place replicas on independent one-model hosts with static scores.

            Ready and in-progress incumbents are non-fungible because their full host cannot accept
            another model. Including LOADING/WARMING here is also essential for placement stability:
            otherwise the empty-host fast path can skip an executing warm and start the same replica
            on a second node. Empty hosts are handled only when the caller proves there is one
            remaining contender in its priority class. A unique, unused domain per candidate keeps
            score ordering static; selected assignments still receive their exact current-domain
            score.
            """

            goal = placement_goal(model)
            placed = sum(item.model_id == model.model_id for item in assignments)
            missing = max(0, goal - placed)
            if not missing:
                return
            domains = assigned_domains.setdefault(model.model_id, set())
            candidates: list[tuple[float, str, NodeSnapshot]] = []
            candidate_domains: list[str] = []
            for node in node_list:
                if node.max_models != 1:
                    continue
                if (model.model_id, node.node_id) in assigned_pairs:
                    continue
                residency = node.residency(model.model_id)
                residency_age = (
                    timestamp - residency.loaded_at
                    if residency is not None and residency.loaded_at
                    else math.inf
                )
                hard_scarcity_blocker = any(
                    len(future_eligible_nodes.get(constrained_model_id, ())) == 1
                    for constrained_model_id in scarce_host_claims.get(
                        (model.model_id, node.node_id), ()
                    )
                )
                recently_loaded = bool(
                    residency is not None
                    and -self.policy.max_future_clock_skew_seconds <= residency_age
                    < model.min_residency_seconds
                )
                if (
                    live_incumbents
                    and scarce_host_excess_claims.get((model.model_id, node.node_id))
                    and (not recently_loaded or hard_scarcity_blocker)
                ):
                    # Repacking may reserve a relatively scarce host, but a fresh placement is
                    # sticky against soft score improvements. A demanded model's sole feasible host
                    # remains a hard override; reconciliation still enforces make-before-break.
                    continue
                if live_incumbents:
                    candidate_shape_matches = bool(
                        residency is not None
                        and residency.state
                        in (
                            ResidencyState.LOADING,
                            ResidencyState.WARMING,
                            ResidencyState.READY,
                        )
                        and model.matches_artifact(residency)
                        and sum(not _adds_model_slot(item) for item in node.residencies)
                        == 1
                    )
                    for_new = False
                else:
                    candidate_shape_matches = (
                        occupied_models[node.node_id] == 0
                        and desired_model_slots[node.node_id] == 0
                    )
                    for_new = requires_new_runtime(node, model)
                if not candidate_shape_matches:
                    continue
                if compatibility(
                    node, model, for_new=for_new
                ) is not None or not fits_node(node, model):
                    continue
                domain = node.failure_domain or node.node_id
                candidate_domains.append(domain)
                score, _ = score_candidate(
                    node,
                    model,
                    capacity[node.node_id],
                    domains,
                    need_new_domain=False,
                )
                candidates.append((score, node.node_id, node))
            if len(candidate_domains) != len(set(candidate_domains)) or set(
                candidate_domains
            ).intersection(domains):
                return
            if missing == 1 and candidates:
                immediate_score = max(item[0] for item in candidates)
                if proactive_repack_is_better(model, immediate_score, domains):
                    return
            for _, _, node in sorted(
                candidates,
                key=lambda item: (-item[0], item[1]),
            )[:missing]:
                need_new_domain = len(domains) < min(
                    model.min_failure_domains,
                    desired_by_model[model.model_id],
                )
                score, reasons = score_candidate(
                    node,
                    model,
                    capacity[node.node_id],
                    domains,
                    need_new_domain=need_new_domain,
                )
                _place(
                    _assignment(
                        model,
                        node,
                        index=placed,
                        score=score,
                        reasons=reasons,
                    ),
                    node,
                    assignments,
                    assigned_pairs,
                    domains,
                    capacity,
                    occupied_models,
                    desired_model_slots,
                    desired_memory,
                    disk_remaining,
                    artifact_disk_cost(node, model),
                )
                placed += 1

        def cache_isolated_empty_order(model: ModelProfile) -> None:
            """Cache a static candidate order without changing fair round progression."""

            domains = assigned_domains.setdefault(model.model_id, set())
            candidates: list[tuple[float, str, NodeSnapshot]] = []
            candidate_domains: list[str] = []
            for node in node_list:
                if (model.model_id, node.node_id) in assigned_pairs:
                    continue
                if compatibility(
                    node,
                    model,
                    for_new=requires_new_runtime(node, model),
                ) is not None or not fits_node(node, model):
                    continue
                if (
                    node.max_models != 1
                    or occupied_models[node.node_id] != 0
                    or desired_model_slots[node.node_id] != 0
                ):
                    # A feasible fungible or already-shared host can change relative score as peers
                    # place. Use the complete scorer instead of caching a partial candidate set.
                    return
                domain = node.failure_domain or node.node_id
                candidate_domains.append(domain)
                score, _ = score_candidate(
                    node,
                    model,
                    capacity[node.node_id],
                    domains,
                    need_new_domain=False,
                )
                candidates.append((score, node.node_id, node))
            if len(candidate_domains) != len(set(candidate_domains)) or set(
                candidate_domains
            ).intersection(domains):
                return
            isolated_empty_orders[model.model_id] = tuple(
                item[2]
                for item in sorted(candidates, key=lambda item: (-item[0], item[1]))
            )
            isolated_empty_cursors[model.model_id] = 0
            isolated_empty_next_indices[model.model_id] = sum(
                item.model_id == model.model_id for item in assignments
            )

        def preserve_healthy_incumbents() -> None:
            """Seed the plan with still-wanted live placements before opening new slots.

            The current desired placement is itself a feasibility witness. Rebuilding entirely
            from a greedy order can fragment a heterogeneous fleet even when every healthy
            incumbent remains wanted, producing an avoidable unload/reload wave and a plan with
            fewer replicas than the state it just observed. Pins are already reserved above; this
            phase retains the best remaining live replicas up to each model's target, while normal
            placement and bounded preemption still handle new demand and genuine conflicts.
            """

            incumbent_nodes_by_model: dict[str, list[NodeSnapshot]] = {}
            incumbent_residency_by_pair: dict[
                tuple[str, str], ModelResidency
            ] = {}
            for node in node_list:
                for residency in node.residencies:
                    if residency.state in (
                        ResidencyState.LOADING,
                        ResidencyState.WARMING,
                        ResidencyState.READY,
                    ):
                        incumbent_nodes_by_model.setdefault(
                            residency.model_id, []
                        ).append(node)
                        incumbent_residency_by_pair[
                            (node.node_id, residency.model_id)
                        ] = residency

            # When every target already has enough healthy live replicas, the observed placement
            # is a complete feasibility witness. In that case soft scarcity improvements must not
            # dismantle it and then discover that the greedy rebuild cannot put all pieces back.
            # A missing or newly demanded replica disables this shortcut, preserving the ordinary
            # relocation behavior that opens a scarce host for real new work.
            complete_live_witness = all(
                sum(
                    model.matches_artifact(
                        incumbent_residency_by_pair[(node.node_id, model.model_id)]
                    )
                    and compatibility(node, model, for_new=False) is None
                    for node in incumbent_nodes_by_model.get(model.model_id, ())
                )
                >= placement_goal(model)
                for model in order
            )

            for model in order:
                goal = placement_goal(model)
                domains = assigned_domains.setdefault(model.model_id, set())
                placed = sum(
                    item.model_id == model.model_id for item in assignments
                )
                while placed < goal:
                    candidates: list[
                        tuple[float, str, NodeSnapshot, tuple[str, ...]]
                    ] = []
                    for node in incumbent_nodes_by_model.get(model.model_id, ()):
                        if (model.model_id, node.node_id) in assigned_pairs:
                            continue
                        residency = incumbent_residency_by_pair[
                            (node.node_id, model.model_id)
                        ]
                        residency_age = (
                            timestamp - residency.loaded_at
                            if residency.loaded_at
                            else math.inf
                        )
                        recently_loaded = (
                            -self.policy.max_future_clock_skew_seconds
                            <= residency_age
                            < model.min_residency_seconds
                        )
                        hard_scarcity_blocker = any(
                            len(future_eligible_nodes.get(other_model_id, ())) == 1
                            for other_model_id in scarce_host_claims.get(
                                (model.model_id, node.node_id), ()
                            )
                        )
                        if (
                            (
                                not complete_live_witness
                                and scarce_host_excess_claims.get(
                                    (model.model_id, node.node_id)
                                )
                                and (not recently_loaded or hard_scarcity_blocker)
                            )
                            or not model.matches_artifact(residency)
                            or compatibility(node, model, for_new=False) is not None
                            or not fits_node(node, model)
                        ):
                            continue
                        score, reasons = score_candidate(
                            node,
                            model,
                            capacity[node.node_id],
                            domains,
                            need_new_domain=(
                                len(domains)
                                < min(model.min_failure_domains, goal)
                            ),
                        )
                        candidates.append((score, node.node_id, node, reasons))
                    if not candidates:
                        break
                    needed_domains = min(model.min_failure_domains, goal)
                    if len(domains) < needed_domains:
                        diverse = [
                            candidate
                            for candidate in candidates
                            if (
                                candidate[2].failure_domain
                                or candidate[2].node_id
                            )
                            not in domains
                        ]
                        if diverse:
                            candidates = diverse
                    score, _, selected, reasons = min(
                        candidates,
                        key=lambda item: (-item[0], item[1]),
                    )
                    _place(
                        _assignment(
                            model,
                            selected,
                            index=placed,
                            score=score,
                            reasons=(*reasons, "preserved healthy incumbent"),
                        ),
                        selected,
                        assignments,
                        assigned_pairs,
                        domains,
                        capacity,
                        occupied_models,
                        desired_model_slots,
                        desired_memory,
                        disk_remaining,
                        artifact_disk_cost(selected, model),
                    )
                    placed += 1

        preserve_healthy_incumbents()
        for model in order:
            place_isolated_replicas(model, live_incumbents=True)

        priorities = sorted({model.priority for model in order}, reverse=True)
        for priority in priorities:
            priority_models = [model for model in order if model.priority == priority]
            # Preserve max-min fairness among real service at one administrator priority, but do
            # not let correlation-only canaries take freshly freed slots while directly observed
            # models are still missing replicas. Once service demand is satisfied or infeasible,
            # opportunistic models may fairly share what remains.
            evidence_tiers = (
                [
                    model
                    for model in priority_models
                    if demand_urgency_by_model[model.model_id] >= 2
                ],
                [
                    model
                    for model in priority_models
                    if demand_urgency_by_model[model.model_id] < 2
                ],
            )
            for tier_models in evidence_tiers:
                remaining_contenders = [
                    model
                    for model in tier_models
                    if sum(item.model_id == model.model_id for item in assignments)
                    < placement_goal(model)
                ]
                if len(remaining_contenders) == 1:
                    place_isolated_replicas(
                        remaining_contenders[0],
                        live_incumbents=False,
                    )
                else:
                    for model in remaining_contenders:
                        cache_isolated_empty_order(model)
                blocked: set[str] = set()
                placed_by_model = {
                    model.model_id: sum(
                        item.model_id == model.model_id for item in assignments
                    )
                    for model in tier_models
                }
                while True:
                    unfinished = [
                        model
                        for model in tier_models
                        if model.model_id not in blocked
                        and placed_by_model[model.model_id] < placement_goal(model)
                    ]
                    if not unfinished:
                        break
                    minimum_placed = min(
                        placed_by_model[model.model_id] for model in unfinished
                    )
                    current_level = sorted(
                        (
                            model
                            for model in unfinished
                            if placed_by_model[model.model_id] == minimum_placed
                        ),
                        key=lambda model: (
                            -demand_urgency_by_model[model.model_id],
                            _scarcity_service_pressure(
                                model,
                                forecast_by_model.get(model.model_id),
                            ),
                            model.model_id,
                        ),
                    )
                    progress = False
                    for model in current_level:
                        if place_next_replica(model):
                            placed_by_model[model.model_id] += 1
                            progress = True
                    if not progress:
                        # An infeasible service tier must not strand capacity usable by the next
                        # evidence tier; bounded repacking already exhausted useful moves here.
                        blocked.update(model.model_id for model in current_level)

        # Lower-priority or lower-evidence managed residency can otherwise deadlock a saturated
        # fleet forever: its live slot prevents critical placement while desired assignment keeps
        # reconciliation from draining it. Stage deterministic victims. Administrator priority is
        # primary; within one class, baseline/direct evidence may reclaim speculative canaries.
        # A newly demanded speculative model may also replace stale speculation or one excess
        # replica, but it cannot take another model's only canary.
        # The beneficiary remains unsatisfied until a later heartbeat proves capacity is free.
        preempted_nodes: set[str] = set()
        assignments_by_node: dict[str, list[PlacementAssignment]] = {}
        for assignment in assignments:
            assignments_by_node.setdefault(assignment.node_id, []).append(assignment)
        staged_domains = {
            model.model_id: set(assigned_domains.get(model.model_id, set()))
            for model in order
        }
        for beneficiary in order:
            if len(preemptions) >= self.policy.max_staged_preemptions:
                break
            beneficiary_urgency = _placement_demand_urgency(
                beneficiary,
                forecast_by_model.get(beneficiary.model_id),
            )
            beneficiary_forecast = forecast_by_model.get(beneficiary.model_id)
            placed = sum(item.model_id == beneficiary.model_id for item in assignments)
            if beneficiary_urgency == 0 or (beneficiary_urgency == 1 and placed > 0):
                # Speculation may acquire one fair canary by replacing only stale speculation or
                # another speculative model's excess replica. Further speculative scale-out waits
                # for spare capacity; direct/baseline evidence retains broader preemption rights.
                continue
            missing = max(0, desired_by_model[beneficiary.model_id] - placed)
            pending_pins = [
                node_id
                for node_id in beneficiary.pinned_nodes
                if (beneficiary.model_id, node_id) not in assigned_pairs
            ]
            preemption_targets: list[str | None] = [
                *pending_pins,
                *(None for _ in range(max(0, missing - len(pending_pins)))),
            ]
            if beneficiary_urgency == 1:
                preemption_targets = preemption_targets[:1]
            authorized_victims: frozenset[str] | None = None
            if (
                beneficiary_forecast is not None
                and beneficiary_forecast.preemption_authorized
                and not pending_pins
            ):
                preemption_targets = [beneficiary_forecast.preemption_node_id]
                authorized_victims = frozenset(
                    beneficiary_forecast.preemption_victims
                )
            preemption_cache = (
                _PreemptionSearchCache()
                if not pending_pins and beneficiary.min_failure_domains <= 1
                else None
            )
            for required_node_id in preemption_targets:
                if len(preemptions) >= self.policy.max_staged_preemptions:
                    break
                staged = _stage_priority_preemption(
                    beneficiary,
                    node_list,
                    assignments,
                    assigned_pairs,
                    assigned_domains,
                    capacity,
                    occupied_models,
                    desired_model_slots,
                    disk_remaining,
                    profile_by_id,
                    demand_urgency_by_model,
                    desired_by_model,
                    timestamp,
                    self.policy,
                    startup_by_pair,
                    load_by_pair,
                    require_new_domain=(
                        len(staged_domains[beneficiary.model_id])
                        < min(
                            beneficiary.min_failure_domains,
                            desired_by_model[beneficiary.model_id],
                        )
                    ),
                    existing_domains=staged_domains[beneficiary.model_id],
                    required_node_id=required_node_id,
                    excluded_nodes=preempted_nodes,
                    max_victims=(self.policy.max_staged_preemptions - len(preemptions)),
                    assignments_by_node=assignments_by_node,
                    candidate_cache=preemption_cache,
                    required_victim_ids=authorized_victims,
                )
                if not staged:
                    break
                node_id, victims = staged
                preempted_nodes.add(node_id)
                selected_node = node_by_id[node_id]
                staged_domains[beneficiary.model_id].add(
                    selected_node.failure_domain or selected_node.node_id
                )
                preemptions.extend(
                    PlacementPreemption(
                        node_id=node_id,
                        model_id=victim.model_id,
                        for_model_id=beneficiary.model_id,
                    )
                    for victim in victims
                )

        # Host model ceilings can be lowered below live inventory. Preserve the winners selected by
        # ordinary priority placement and explicitly evict only the excess managed residencies;
        # otherwise replacement-readiness safety would retain a violating incumbent indefinitely.
        preempted_pairs = {(item.node_id, item.model_id) for item in preemptions}
        # A newly tightened privacy, allowlist, hardware, or runtime constraint is authoritative
        # even when old direct demand or residency cooldown still requests a replica. The old
        # residency cannot legally satisfy that target, so keeping it ready as a "replacement"
        # deadlocks convergence and can violate policy. Stage an owned, unpinned residency for
        # removal; the desired target remains visible as unsatisfied until a compliant host exists.
        for node in node_list:
            if len(preemptions) >= self.policy.max_staged_preemptions:
                break
            for residency in sorted(node.residencies, key=lambda item: item.model_id):
                if len(preemptions) >= self.policy.max_staged_preemptions:
                    break
                pair = (node.node_id, residency.model_id)
                profile = profile_by_id.get(residency.model_id)
                if (
                    pair in preempted_pairs
                    or profile is None
                    or residency.state
                    not in (
                        ResidencyState.READY,
                        ResidencyState.DRAINING,
                        ResidencyState.FAILED,
                    )
                    or not residency.managed
                    or residency.pinned
                    or node.manually_managed
                    or node.node_id in profile.pinned_nodes
                    or _hard_residency_policy_violation(node, profile) is None
                ):
                    continue
                preemptions.append(
                    PlacementPreemption(
                        node_id=node.node_id,
                        model_id=residency.model_id,
                    )
                )
                preempted_pairs.add(pair)

        for node in node_list:
            if len(preemptions) >= self.policy.max_staged_preemptions:
                break
            if node.max_models is None:
                continue
            live = [item for item in node.residencies if not _adds_model_slot(item)]
            already_staged = sum(
                (node.node_id, item.model_id) in preempted_pairs for item in live
            )
            excess = max(0, len(live) - node.max_models - already_staged)
            if not excess:
                continue
            selected_models = {
                item.model_id for item in assignments if item.node_id == node.node_id
            }
            beneficiary = max(
                (
                    profile_by_id[model_id]
                    for model_id in selected_models
                    if model_id in profile_by_id
                ),
                key=lambda item: (item.priority, item.model_id),
                default=None,
            )
            removable = sorted(
                (
                    item
                    for item in live
                    if item.model_id not in selected_models
                    and (node.node_id, item.model_id) not in preempted_pairs
                    and item.state
                    in (
                        ResidencyState.READY,
                        ResidencyState.DRAINING,
                        ResidencyState.FAILED,
                    )
                    and item.managed
                    and not item.pinned
                    and not node.manually_managed
                    and (profile := profile_by_id.get(item.model_id)) is not None
                    and node.node_id not in profile.pinned_nodes
                ),
                key=lambda item: (
                    profile_by_id[item.model_id].priority,
                    item.model_id,
                ),
            )
            remaining_budget = self.policy.max_staged_preemptions - len(preemptions)
            for victim in removable[: min(excess, remaining_budget)]:
                preemption = PlacementPreemption(
                    node_id=node.node_id,
                    model_id=victim.model_id,
                    for_model_id=(beneficiary.model_id if beneficiary else ""),
                )
                preemptions.append(preemption)
                preempted_pairs.add((node.node_id, victim.model_id))

        # A newly tightened reciprocal co-location policy has the same staged-convergence need as
        # a lowered host model ceiling. Keep the assignment winners, then evict only managed,
        # unpinned incumbents until every selected model's isolation contract is actually true.
        for node in node_list:
            if len(preemptions) >= self.policy.max_staged_preemptions:
                break
            selected = [item for item in assignments if item.node_id == node.node_id]
            protected_profiles = tuple(
                profile_by_id[item.model_id]
                for item in selected
                if item.model_id in profile_by_id
            )
            if not protected_profiles:
                # A pre-existing reciprocal violation can prevent *every* model from being
                # selected. Elect the same highest-priority/model-ID winner as ordinary placement
                # would use once the peers are gone, then stage only its removable blockers.
                configured_live = {
                    item.model_id
                    for item in node.residencies
                    if not _adds_model_slot(item)
                    and desired_by_model.get(item.model_id, 0) > 0
                    and item.model_id in profile_by_id
                }
                protected_profiles = tuple(
                    sorted(
                        (profile_by_id[model_id] for model_id in configured_live),
                        key=lambda item: (
                            -item.priority,
                            item.max_colocated_models or MAX_COUNTER,
                            -len(item.colocation_excludes),
                            -item.maximum_memory_mb,
                            item.model_id,
                        ),
                    )[:1]
                )
            if not protected_profiles:
                continue
            projected_residencies = [
                item
                for item in node.residencies
                if (node.node_id, item.model_id) not in preempted_pairs
            ]

            def violates_selected_colocation() -> bool:
                projected = replace(node, residencies=tuple(projected_residencies))
                return any(
                    not _colocation_allowed(
                        projected,
                        profile,
                        assignments,
                        profile_by_id,
                    )
                    for profile in protected_profiles
                )

            while violates_selected_colocation():
                if len(preemptions) >= self.policy.max_staged_preemptions:
                    break
                selected_models = {item.model_id for item in protected_profiles}
                removable = sorted(
                    (
                        item
                        for item in projected_residencies
                        if item.model_id not in selected_models
                        and item.state
                        in (
                            ResidencyState.READY,
                            ResidencyState.DRAINING,
                            ResidencyState.FAILED,
                        )
                        and item.managed
                        and not item.pinned
                        and not node.manually_managed
                        and (profile := profile_by_id.get(item.model_id)) is not None
                        and node.node_id not in profile.pinned_nodes
                    ),
                    key=lambda item: (
                        profile_by_id[item.model_id].priority,
                        item.model_id,
                    ),
                )
                if not removable:
                    break
                victim = removable[0]
                beneficiary = protected_profiles[0]
                preemption = PlacementPreemption(
                    node_id=node.node_id,
                    model_id=victim.model_id,
                    for_model_id=beneficiary.model_id,
                )
                preemptions.append(preemption)
                preempted_pairs.add((node.node_id, victim.model_id))
                projected_residencies.remove(victim)

        for model in order:
            target = desired_by_model[model.model_id]
            regular_slots = max(0, target - len(model.pinned_nodes))
            placed = sum(1 for item in assignments if item.model_id == model.model_id)
            regular_placed = sum(
                1
                for item in assignments
                if item.model_id == model.model_id
                and item.node_id not in model.pinned_nodes
            )
            regular_missing = max(0, regular_slots - regular_placed)
            if regular_missing:
                eligible = [
                    node
                    for node in node_list
                    if compatibility(node, model, for_new=True) is None
                ]
                colocation_eligible = [
                    node
                    for node in eligible
                    if _colocation_allowed(
                        node,
                        model,
                        assignments,
                        profile_by_id,
                    )
                ]
                code = (
                    "no_eligible_nodes"
                    if not eligible
                    else "colocation_limit"
                    if not colocation_eligible
                    else "insufficient_capacity"
                )
                message = (
                    f"Placed {placed} of {target} desired replicas"
                    if code != "colocation_limit"
                    else (
                        f"Placed {placed} of {target} desired replicas; all eligible hosts "
                        "would violate a model co-location limit"
                    )
                )
                unsatisfied.append(
                    UnsatisfiedConstraint(
                        model_id=model.model_id,
                        code=code,
                        message=message,
                        missing_replicas=regular_missing,
                    )
                )
        # Repacking can move a model after its own turn, so domain diagnostics must be computed from
        # final state rather than cached per-model loop state.
        unsatisfied = [
            item for item in unsatisfied if item.code != "failure_domain_shortfall"
        ]
        for model in model_list:
            placed = sum(1 for item in assignments if item.model_id == model.model_id)
            domains = assigned_domains.setdefault(model.model_id, set())
            needed_domains = min(
                model.min_failure_domains,
                desired_by_model[model.model_id],
            )
            if placed and len(domains) < needed_domains:
                unsatisfied.append(
                    UnsatisfiedConstraint(
                        model_id=model.model_id,
                        code="failure_domain_shortfall",
                        message=(
                            f"Replicas span {len(domains)} failure domains; "
                            f"{needed_domains} required"
                        ),
                    )
                )

        assignments.sort(
            key=lambda item: (item.model_id, item.replica_index, item.node_id)
        )
        unsatisfied.sort(key=lambda item: (item.model_id, item.code, item.message))
        # A correlation-only model cannot evict serving work, but a mature workflow prediction can
        # still move its immutable artifact transfer off the future critical path. Cache at most a
        # small policy-bounded set on hosts where the model can eventually run; this consumes only
        # authenticated disk and never changes the serving assignment or preemption authority.
        assignment_pairs = {
            (item.node_id, item.model_id) for item in assignments
        }
        preemption_beneficiary_pairs = {
            (item.node_id, item.for_model_id)
            for item in preemptions
            if item.for_model_id
        }
        placed_by_model = Counter(item.model_id for item in assignments)
        # Begin after every uncached desired assignment has reserved its artifact bytes. A
        # speculative transfer must not race a real load for the same free-disk observation.
        prefetch_disk_remaining = dict(disk_remaining)
        artifact_prefetches: list[ArtifactPrefetch] = []

        def predictive_artifact_present(
            node: NodeSnapshot,
            model: ModelProfile,
        ) -> bool:
            residency = node.residency(model.model_id)
            return bool(
                residency
                and model.matches_artifact(residency)
                and residency.state
                in (
                    ResidencyState.CACHED,
                    ResidencyState.LOADING,
                    ResidencyState.WARMING,
                    ResidencyState.READY,
                    ResidencyState.DRAINING,
                    ResidencyState.FAILED,
                )
            )

        def predictive_prefetch_candidates(
            model: ModelProfile,
            *,
            require_disk: bool = True,
        ) -> list[NodeSnapshot]:
            candidates: list[NodeSnapshot] = []
            candidate_nodes = (
                (
                    node_by_id[node_id]
                    for node_id in future_eligible_nodes.get(model.model_id, ())
                )
                if require_disk
                else iter(node_list)
            )
            for node in candidate_nodes:
                node_id = node.node_id
                pair = (node_id, model.model_id)
                remaining_disk = prefetch_disk_remaining[node_id]
                # The ordinary future-eligibility index deliberately includes current artifact
                # disk. Replacement discovery must instead prove every other hard constraint while
                # asking whether one exact victim would make disk sufficient.
                disk_agnostic_compatible = require_disk or (
                    _ineligible_reason(
                        replace(node, disk_available_mb=None),
                        model,
                        timestamp,
                        self.policy,
                        for_new=requires_new_runtime(node, model),
                    )
                    is None
                    and runtime_memory(node, model)
                    <= placement_budget[node_id]
                    and node.max_models != 0
                )
                if (
                    pair not in assignment_pairs
                    and pair not in preemption_beneficiary_pairs
                    and not predictive_artifact_present(node, model)
                    # Predictive provenance and bounded cleanup arrived with the EVICT actuator.
                    # Legacy nodes may still receive ordinary/preemption LOAD work, but must not
                    # accumulate new speculative artifacts they cannot identify and expire.
                    and "evict" in node.actuator_capabilities
                    and remaining_disk is not None
                    and disk_agnostic_compatible
                    and (
                        not require_disk
                        or model.artifact_size_mb
                        + self.policy.predictive_artifact_disk_reserve_mb
                        <= remaining_disk
                    )
                ):
                    candidates.append(node)
            return candidates

        def predictive_artifact_value(
            model: ModelProfile,
            node: NodeSnapshot,
        ) -> tuple[float, float]:
            """Return expected cold-path seconds saved per MB and in total."""

            forecast = forecast_by_model.get(model.model_id)
            if (
                forecast is None
                or _placement_demand_urgency(model, forecast) != 1
            ):
                return (0.0, 0.0)
            load_seconds = load_by_pair.get(
                (node.node_id, model.model_id),
                model.load_seconds,
            )
            # A transfer that is only half complete when predicted demand arrives still removes
            # half of the later cold-path wait. Unknown lead retains the conservative historical
            # estimate; a calibrated deadline discounts value to work that can finish in time.
            useful_transfer_seconds = (
                load_seconds
                if forecast.prediction_lead_seconds is None
                else min(load_seconds, forecast.prediction_lead_seconds)
            )
            expected_startup_value = (
                forecast.correlation_confidence
                * max(forecast.offered_concurrency, 0.01)
                * useful_transfer_seconds
            )
            return (
                expected_startup_value / max(1, model.artifact_size_mb),
                expected_startup_value,
            )

        def predictive_prefetch_rank(model: ModelProfile) -> tuple[float, float, float, str]:
            """Rank speculative transfers by expected critical-path value per stored byte."""

            forecast = forecast_by_model[model.model_id]
            candidates = predictive_prefetch_candidates(model)
            preferred = min(
                candidates,
                key=lambda node: (
                    load_by_pair.get(
                        (node.node_id, model.model_id),
                        model.load_seconds,
                    )
                    + startup_by_pair.get(
                        (node.node_id, model.model_id),
                        model.warm_seconds,
                    ),
                    node.host_priority,
                    node.node_id,
                ),
                default=None,
            )
            value_per_mb, expected_startup_value = (
                predictive_artifact_value(model, preferred)
                if preferred is not None
                else (0.0, 0.0)
            )
            return (
                -value_per_mb,
                -expected_startup_value,
                -forecast.correlation_confidence,
                model.model_id,
            )

        all_predictive_models = [
            model
            for model in model_list
            if compute_input_digest
            and model.artifact_source
            and desired_by_model[model.model_id] > placed_by_model[model.model_id]
            and _placement_demand_urgency(
                model,
                forecast_by_model.get(model.model_id),
            )
            == 1
        ]
        predictive_models = list(all_predictive_models)
        while (
            predictive_models
            and len(artifact_prefetches)
            < self.policy.max_predictive_artifact_prefetches
        ):
            # Re-rank after every admission because one transfer changes the per-node disk ledger.
            # This matters when policy permits several predictions: a model's best host before the
            # first choice may no longer be feasible afterward.
            model = min(predictive_models, key=predictive_prefetch_rank)
            predictive_models.remove(model)
            candidates = predictive_prefetch_candidates(model)
            if not candidates:
                continue
            selected = min(
                candidates,
                key=lambda node: (
                    load_by_pair.get(
                        (node.node_id, model.model_id),
                        model.load_seconds,
                    )
                    + startup_by_pair.get(
                        (node.node_id, model.model_id),
                        model.warm_seconds,
                    ),
                    node.host_priority,
                    node.node_id,
                ),
            )
            _value_density, expected_value = predictive_artifact_value(
                model,
                selected,
            )
            if expected_value <= 0:
                continue
            artifact_prefetches.append(
                ArtifactPrefetch(selected.node_id, model.model_id)
            )
            selected_disk = prefetch_disk_remaining[selected.node_id]
            if selected_disk is not None:
                prefetch_disk_remaining[selected.node_id] = (
                    selected_disk - model.artifact_size_mb
                )
        # Disk pressure may otherwise strand a newly learned high-value prediction behind an old
        # speculative artifact forever. Allow a bounded replacement only after the old artifact
        # has aged, and only when the incoming prediction has both at least as much absolute value
        # and a policy-sized value-per-byte gain. The eviction and replacement prefetch are
        # intentionally separate plan generations: bytes are not credited until a heartbeat proves
        # deletion completed.
        prefetched_model_ids = {
            item.model_id for item in artifact_prefetches
        }
        replacement_opportunities: list[
            tuple[
                float,
                float,
                int,
                int,
                float,
                str,
                tuple[str, ...],
                str,
            ]
        ] = []
        if (
            self.policy.max_predictive_artifact_prefetches
            and self.policy.max_predictive_artifact_evictions
        ):
            for incoming in all_predictive_models:
                if (
                    incoming.model_id in prefetched_model_ids
                    or predictive_prefetch_candidates(incoming)
                ):
                    continue
                for node in predictive_prefetch_candidates(
                    incoming,
                    require_disk=False,
                ):
                    remaining_disk = prefetch_disk_remaining[node.node_id]
                    if remaining_disk is None:
                        continue
                    required_disk = (
                        incoming.artifact_size_mb
                        + self.policy.predictive_artifact_disk_reserve_mb
                    )
                    disk_shortfall = required_disk - remaining_disk
                    if disk_shortfall <= 0:
                        continue
                    incoming_density, incoming_value = predictive_artifact_value(
                        incoming,
                        node,
                    )
                    if incoming_value <= 0:
                        continue
                    eligible_victims: list[
                        tuple[float, float, int, float, str]
                    ] = []
                    for residency in node.residencies:
                        victim = profile_by_id.get(residency.model_id)
                        pair = (node.node_id, residency.model_id)
                        age = (
                            timestamp - residency.loaded_at
                            if 0 < residency.loaded_at <= timestamp
                            else 0.0
                        )
                        if (
                            victim is None
                            or victim.model_id == incoming.model_id
                            or residency.state != ResidencyState.CACHED
                            or not residency.predictive_cache
                            or not residency.managed
                            or residency.pinned
                            or not victim.matches_artifact(residency)
                            or pair in assignment_pairs
                            or victim.model_id in prefetched_model_ids
                            or demand_urgency_by_model.get(victim.model_id, 0) > 1
                            or age
                            < self.policy.predictive_artifact_replacement_min_age_seconds
                        ):
                            continue
                        victim_density, victim_value = predictive_artifact_value(
                            victim,
                            node,
                        )
                        eligible_victims.append(
                            (
                                victim_density,
                                victim_value,
                                victim.artifact_size_mb,
                                age,
                                victim.model_id,
                            )
                        )

                    # Keep the combinatorial cover search bounded while retaining both kinds of
                    # useful candidate: the cheapest predictions to lose and the largest artifacts
                    # that can actually close a fragmented-disk shortfall. The union is stable and
                    # capped before enumerating groups of at most three victims by default.
                    half_limit = _MAX_PREDICTIVE_REPLACEMENT_CANDIDATES // 2
                    bounded_by_id: dict[
                        str,
                        tuple[float, float, int, float, str],
                    ] = {}
                    for candidate in sorted(
                        eligible_victims,
                        key=lambda item: (
                            item[0],
                            item[1],
                            -item[2],
                            -item[3],
                            item[4],
                        ),
                    )[:half_limit]:
                        bounded_by_id[candidate[4]] = candidate
                    for candidate in sorted(
                        eligible_victims,
                        key=lambda item: (
                            -item[2],
                            item[0],
                            item[1],
                            -item[3],
                            item[4],
                        ),
                    )[:half_limit]:
                        bounded_by_id[candidate[4]] = candidate
                    bounded_victims = tuple(
                        bounded_by_id[model_id]
                        for model_id in sorted(bounded_by_id)
                    )
                    max_group_size = min(
                        len(bounded_victims),
                        self.policy.predictive_artifact_replacement_max_victims,
                    )
                    best_node_replacement: tuple[
                        float,
                        float,
                        int,
                        int,
                        float,
                        str,
                        tuple[str, ...],
                        str,
                    ] | None = None
                    for group_size in range(1, max_group_size + 1):
                        for group in combinations(bounded_victims, group_size):
                            reclaimed_mb = sum(item[2] for item in group)
                            if reclaimed_mb < disk_shortfall:
                                continue
                            lost_value = sum(item[1] for item in group)
                            victim_density = lost_value / max(1, reclaimed_mb)
                            if (
                                incoming_value < lost_value
                                or incoming_density
                                < victim_density
                                * self.policy.predictive_artifact_replacement_min_gain
                            ):
                                continue
                            ordered_victims = tuple(
                                item[4]
                                for item in sorted(
                                    group,
                                    key=lambda item: (
                                        item[0],
                                        item[1],
                                        -item[2],
                                        -item[3],
                                        item[4],
                                    ),
                                )
                            )
                            opportunity = (
                                -(incoming_value - lost_value),
                                -(incoming_density - victim_density),
                                reclaimed_mb - disk_shortfall,
                                group_size,
                                -min(item[3] for item in group),
                                node.node_id,
                                ordered_victims,
                                incoming.model_id,
                            )
                            if (
                                best_node_replacement is None
                                or opportunity < best_node_replacement
                            ):
                                best_node_replacement = opportunity
                    if best_node_replacement is not None:
                        replacement_opportunities.append(best_node_replacement)

        artifact_evictions: list[ArtifactEviction] = []
        replacement_victims: set[tuple[str, str]] = set()
        replacement_victim_model_ids: set[str] = set()
        replacement_beneficiaries: set[str] = set()
        replacement_limit = self.policy.max_predictive_artifact_evictions
        for (
            _negative_net_value,
            _negative_density_gain,
            _excess_reclaimed_mb,
            _group_size,
            _negative_min_age,
            node_id,
            victim_model_ids,
            incoming_model_id,
        ) in sorted(replacement_opportunities):
            if (
                len(artifact_evictions) >= replacement_limit
                or incoming_model_id in replacement_beneficiaries
                or incoming_model_id in replacement_victim_model_ids
                or len(replacement_beneficiaries)
                >= self.policy.max_predictive_artifact_prefetches
                or any(
                    (node_id, victim_model_id) in replacement_victims
                    or victim_model_id in replacement_beneficiaries
                    for victim_model_id in victim_model_ids
                )
            ):
                continue
            for victim_model_id in victim_model_ids:
                if len(artifact_evictions) >= replacement_limit:
                    break
                pair = (node_id, victim_model_id)
                artifact_evictions.append(
                    ArtifactEviction(node_id, victim_model_id, incoming_model_id)
                )
                replacement_victims.add(pair)
                replacement_victim_model_ids.add(victim_model_id)
            replacement_beneficiaries.add(incoming_model_id)

        # A speculative artifact that was never warmed must not consume disk forever. Only exact
        # artifacts that a managed node explicitly labels as predictive are eligible; operator
        # caches, pinned entries, used models, live runtimes, and legacy nodes are never inferred
        # to be disposable. Expiration is plan state so ordinary generation fencing and mutation
        # budgets apply to deletion just as they do to every other allocator side effect.
        prefetch_pairs = {
            (item.node_id, item.model_id) for item in artifact_prefetches
        }
        stale_predictive_artifacts: list[
            tuple[float, int, str, str]
        ] = []
        for node in node_list:
            if node.manually_managed or "evict" not in node.actuator_capabilities:
                continue
            for residency in node.residencies:
                profile = profile_by_id.get(residency.model_id)
                pair = (node.node_id, residency.model_id)
                age = (
                    timestamp - residency.loaded_at
                    if 0 < residency.loaded_at <= timestamp
                    else 0.0
                )
                if (
                    profile is not None
                    and residency.state == ResidencyState.CACHED
                    and residency.predictive_cache
                    and residency.managed
                    and not residency.pinned
                    and profile.matches_artifact(residency)
                    and desired_by_model[profile.model_id] == 0
                    and pair not in assignment_pairs
                    and pair not in prefetch_pairs
                    and age >= self.policy.predictive_artifact_ttl_seconds
                ):
                    stale_predictive_artifacts.append(
                        (
                            residency.loaded_at,
                            -profile.artifact_size_mb,
                            node.node_id,
                            profile.model_id,
                        )
                    )
        for _loaded_at, _negative_size, node_id, model_id in sorted(
            stale_predictive_artifacts
        ):
            if len(artifact_evictions) >= self.policy.max_predictive_artifact_evictions:
                break
            if (node_id, model_id) in replacement_victims:
                continue
            artifact_evictions.append(ArtifactEviction(node_id, model_id))
        # Wall-clock time is not itself a desired-state input, but TTL and scale-down boundaries
        # derived from it are. Include the resulting targets and placements so the controller
        # advances its logical generation exactly when a time-sensitive decision changes. A late
        # command from the old state can then be fenced by the node runtime.
        input_digest = (
            _input_digest(
                node_list,
                model_list,
                forecast_by_model,
                desired_by_model=desired_by_model,
                assignments=assignments,
                preemptions=preemptions,
                artifact_prefetches=artifact_prefetches,
                artifact_evictions=artifact_evictions,
            )
            if compute_input_digest
            else ""
        )
        objective = sum(item.score for item in assignments) - 1_000_000 * sum(
            item.missing_replicas for item in unsatisfied
        )
        return PlacementPlan(
            generation=(
                new_generation(input_digest, timestamp)
                if compute_input_digest
                else f"evaluation-{int(timestamp * 1000):013d}"
            ),
            created_at=timestamp,
            assignments=tuple(assignments),
            desired_replicas=tuple(sorted(desired_by_model.items())),
            unsatisfied=tuple(unsatisfied),
            objective_score=objective,
            input_digest=input_digest,
            preemptions=tuple(preemptions),
            artifact_prefetches=tuple(artifact_prefetches),
            artifact_evictions=tuple(artifact_evictions),
            model_urgencies=tuple(
                sorted(
                    (
                        model.model_id,
                        _placement_demand_urgency(
                            model,
                            forecast_by_model.get(model.model_id),
                        ),
                    )
                    for model in model_list
                )
            ),
        )


def _portfolio_dynamic_fit_reason(
    node: NodeSnapshot,
    model: ModelProfile,
    profile_by_id: Mapping[str, ModelProfile],
    policy: PlannerPolicy,
) -> str | None:
    """Check occupancy-dependent fit after hard node compatibility already passed."""

    allocatable = node.capacity_mb - node.reserved_mb
    if node.state == NodeState.THROTTLED:
        allocatable *= policy.throttled_capacity_fraction
    allocatable = max(0, math.floor(allocatable))
    usable = max(
        0,
        math.floor(allocatable * (1.0 - policy.memory_headroom_fraction)),
    )
    residency = node.residency(model.model_id)
    live_memory = sum(
        item.memory_mb for item in node.residencies if not _adds_model_slot(item)
    )
    incremental = _incremental_memory_mb(
        residency,
        model.memory_for(node.runtimes),
    )
    if incremental > max(0, usable - live_memory):
        return "insufficient current memory headroom"
    occupied = sum(not _adds_model_slot(item) for item in node.residencies)
    if (
        node.max_models is not None
        and _adds_model_slot(residency)
        and occupied >= node.max_models
    ):
        return "model slots are full"
    if not _colocation_allowed(node, model, (), profile_by_id):
        return "colocation policy rejects the model"
    return None


def _portfolio_startup_seconds(
    node: NodeSnapshot,
    residency: ModelResidency | None,
    model: ModelProfile,
    startup_seconds: Mapping[tuple[str, str], float],
    load_seconds: Mapping[tuple[str, str], float],
) -> float:
    if (
        residency is not None
        and residency.state == ResidencyState.READY
        and model.matches_artifact(residency)
    ):
        return 0.0
    if (
        residency is not None
        and model.matches_artifact(residency)
        and residency.state
        in (
            ResidencyState.CACHED,
            ResidencyState.LOADING,
            ResidencyState.WARMING,
            ResidencyState.DRAINING,
        )
    ):
        return startup_seconds.get(
            (node.node_id, model.model_id),
            model.warm_seconds,
        )
    key = (node.node_id, model.model_id)
    return load_seconds.get(key, model.load_seconds) + startup_seconds.get(
        key,
        model.warm_seconds,
    )


def _placement_demand_urgency(
    model: ModelProfile,
    forecast: DemandForecast | None,
) -> int:
    """Keep speculative prewarms behind required and directly observed service."""

    if model.min_replicas or model.pinned_nodes:
        return 3
    if forecast is None:
        return 0
    if forecast.preemption_authorized:
        return 2
    observed_rate = forecast.observed_requests_per_minute
    # Forecasts supplied by older peers/tests predate the explicit observed field. No correlation
    # lineage means their ordinary request rate is direct evidence, preserving wire compatibility.
    if not observed_rate and not forecast.correlation_sources:
        observed_rate = forecast.requests_per_minute
    if (
        observed_rate > 0
        or forecast.queue_depth
        or forecast.p95_latency_ms
        or forecast.error_rate
        or (forecast.offered_concurrency > 0 and not forecast.correlation_sources)
    ):
        return 2
    if forecast.correlation_sources and forecast.correlated_requests_per_minute > 0:
        return 1
    return 0


def _scarcity_service_pressure(
    model: ModelProfile,
    forecast: DemandForecast | None,
) -> tuple[float, ...]:
    """Order equal-share contenders by attributable harm, never caller-chosen breadth.

    Fair rounds still give the next replica to the least-served models. This key only resolves a
    tie at that level, which matters when capacity runs out partway through the round. Only direct,
    attributable service measurements participate: queue, SLO latency, errors, concurrency, and
    observed request rate.
    """

    if forecast is None:
        return (0.0,) * 5
    observed_rate = forecast.observed_requests_per_minute
    if not observed_rate and not forecast.correlation_sources:
        observed_rate = forecast.requests_per_minute
    latency_ratio = (
        min(100.0, forecast.p95_latency_ms / model.latency_slo_ms)
        if model.latency_slo_ms > 0
        else 0.0
    )
    # ``sorted`` is ascending; negate every descending service-impact component.
    return (
        -float(forecast.queue_depth),
        -latency_ratio,
        -forecast.error_rate,
        -forecast.offered_concurrency,
        -observed_rate,
    )


def _next_replica_startup_seconds(
    model: ModelProfile,
    nodes: Iterable[NodeSnapshot],
    startup_seconds: Mapping[tuple[str, str], float],
    load_seconds: Mapping[tuple[str, str], float],
    *,
    now: float,
    policy: PlannerPolicy,
) -> float:
    """Estimate the fastest eligible path for the next not-yet-ready replica."""

    candidates: list[float] = []
    for node in nodes:
        residency = node.residency(model.model_id)
        if (
            residency is not None
            and residency.state == ResidencyState.READY
            and model.matches_artifact(residency)
        ):
            continue
        if (
            _ineligible_reason(
                node,
                model,
                now,
                policy,
                for_new=_requires_new_runtime(residency, model),
            )
            is not None
        ):
            continue
        warm_seconds = startup_seconds.get(
            (node.node_id, model.model_id),
            model.warm_seconds,
        )
        if (
            residency is not None
            and residency.managed
            and residency.state == ResidencyState.WARMING
            and model.matches_artifact(residency)
        ):
            candidates.append(warm_seconds)
            continue
        artifact_cached = (
            not model.artifact_sha256 and model.model_id in node.cached_models
        ) or bool(
            residency
            and residency.managed
            and residency.state
            in (
                ResidencyState.CACHED,
                ResidencyState.DRAINING,
                ResidencyState.FAILED,
            )
            and model.matches_artifact(residency)
        )
        candidates.append(
            warm_seconds
            if artifact_cached
            else load_seconds.get(
                (node.node_id, model.model_id),
                model.load_seconds,
            )
            + warm_seconds
        )
    return min(candidates, default=model.load_seconds + model.warm_seconds)


def desired_replica_count(
    model: ModelProfile,
    forecast: DemandForecast | None,
    *,
    nodes: Iterable[NodeSnapshot] = (),
    now: float | None = None,
    policy: PlannerPolicy | None = None,
    startup_horizon_seconds: float | None = None,
) -> int:
    policy = policy or PlannerPolicy()
    timestamp = time.time() if now is None else float(now)
    node_list = tuple(nodes)
    if startup_horizon_seconds is not None:
        if isinstance(startup_horizon_seconds, bool):
            raise ValueError("startup horizon must be finite and non-negative")
        startup_horizon_seconds = float(startup_horizon_seconds)
        if not math.isfinite(startup_horizon_seconds) or startup_horizon_seconds < 0:
            raise ValueError("startup horizon must be finite and non-negative")
    if forecast is None:
        target = model.min_replicas
    else:
        demand_age = (
            timestamp - forecast.updated_at
            if forecast.updated_at and forecast.updated_at <= timestamp
            else 0.0
        )
        demand_expired = bool(
            forecast.updated_at
            and (
                demand_age > 0
                if model.scale_down_cooldown_seconds == 0
                else demand_age >= model.scale_down_cooldown_seconds
            )
        )
        if demand_expired:
            # The forecast window may intentionally retain a long history for trend quality. It
            # must not silently become the scale-to-zero cooldown: once no request has arrived for
            # the model's configured cooldown, old EWMA residue and old latency/error samples no
            # longer create a replica. Recent READY state is handled independently below.
            forecast = DemandForecast(
                model_id=forecast.model_id, updated_at=forecast.updated_at
            )
        offered_concurrency = forecast.offered_concurrency
        # Normal Grid requests report their measured service time, but imported/early demand may
        # only contain a request rate.  The profile's service estimate is the conservative fallback
        # in that case; leaving this field unused would silently under-scale those workloads.
        if offered_concurrency == 0 and forecast.requests_per_minute > 0:
            offered_concurrency = (
                forecast.requests_per_minute / 60.0 * model.expected_service_seconds
            )
        offered_concurrency = _predictive_offered_concurrency(
            model,
            forecast,
            offered_concurrency,
            policy,
            startup_horizon_seconds=startup_horizon_seconds,
        )
        concurrency = offered_concurrency * (1.0 + policy.demand_headroom_fraction)
        required_capacity = _bounded_replica_ceil(
            concurrency / model.target_utilization,
            MAX_COUNTER,
        )
        baseline_required_capacity = required_capacity
        if forecast.queue_depth:
            required_capacity = max(
                required_capacity,
                _bounded_replica_ceil(
                    forecast.queue_depth / policy.queue_items_per_replica,
                    MAX_COUNTER,
                ),
            )
        if model.latency_slo_ms and forecast.p95_latency_ms > model.latency_slo_ms:
            pressure = min(
                policy.latency_pressure_limit,
                forecast.p95_latency_ms / model.latency_slo_ms,
            )
            required_capacity = max(
                required_capacity,
                _bounded_replica_ceil(
                    max(1, required_capacity) * pressure,
                    MAX_COUNTER,
                ),
            )
        if forecast.error_rate:
            pressure = 1.0 + min(policy.error_pressure_limit, forecast.error_rate)
            required_capacity = max(
                required_capacity,
                _bounded_replica_ceil(
                    max(1, required_capacity) * pressure,
                    MAX_COUNTER,
                ),
            )
        target, ready_replicas = _replicas_for_service_capacity(
            model,
            required_capacity,
            node_list,
            timestamp,
            policy,
        )
        baseline_target, _ = _replicas_for_service_capacity(
            model,
            baseline_required_capacity,
            node_list,
            timestamp,
            policy,
        )
        # A queue or degraded service level is direct evidence that computed service capacity needs
        # one safety replica, even if one engine advertises a wide theoretical batch. Anchor that
        # increment to the freshly computed target—not the current ready count—so a historical
        # pressure sample cannot ratchet the fleet by one replica on every planning tick.
        if ready_replicas and (baseline_target == 0 or target <= baseline_target) and (
            forecast.queue_depth
            or (model.latency_slo_ms and forecast.p95_latency_ms > model.latency_slo_ms)
            or forecast.error_rate
        ):
            target = min(model.max_replicas, target + 1)
        if forecast.canary_only:
            # Weak, placement-safe portfolio evidence earns one real experiment, not an unchecked
            # scale-out across every currently free accelerator. Direct demand or stronger
            # workload evidence clears this marker before the next plan can grow the model.
            target = min(target, 1)
        target = max(model.min_replicas, target)

    # Recently-used ready replicas are retained even after a traffic dip.  This is the global
    # hysteresis that makes placement migration-frugal; the reconciler independently gates mutation.
    if policy.preserve_recent_residencies:
        recent = 0
        for node in node_list:
            residency = node.residency(model.model_id)
            if (
                residency is None
                or residency.state != ResidencyState.READY
                or not model.matches_artifact(residency)
                or _ineligible_reason(
                    node,
                    model,
                    timestamp,
                    policy,
                    for_new=False,
                )
                is not None
            ):
                continue
            last_activity = max(residency.loaded_at, residency.last_used_at)
            age = timestamp - last_activity
            # A small positive skew is valid under the node heartbeat clock policy. Conservatively
            # clamp that age to zero so a just-loaded/used replica cannot be scaled down early.
            # Grossly future timestamps remain unusable rather than pinning a model indefinitely.
            if (
                last_activity
                and -policy.max_future_clock_skew_seconds <= age
                and max(0.0, age) < model.scale_down_cooldown_seconds
            ):
                recent += 1
        target = max(target, recent)
        if forecast is not None and forecast.updated_at:
            demand_age = timestamp - forecast.updated_at
            if 0 <= demand_age < model.scale_down_cooldown_seconds:
                target = max(target, 1)
    # Pins are a hard desired-state declaration, not merely preferred candidates.  Keeping the
    # reported target below the number of pins would produce a plan whose assignment count exceeds
    # its own target and would weaken replacement/failure-domain safety checks in reconciliation.
    target = max(target, len(model.pinned_nodes))
    return min(model.max_replicas, target)


def _predictive_offered_concurrency(
    model: ModelProfile,
    forecast: DemandForecast,
    offered_concurrency: float,
    policy: PlannerPolicy,
    *,
    startup_horizon_seconds: float | None = None,
) -> float:
    """Project a rising workload across the time needed to make a new replica ready.

    DemandTracker already includes a short one-bucket trend in its base forecast. This additional
    horizon is model-specific: a model that takes two minutes to load must start earlier than one
    that warms in two seconds. Confidence and a hard growth ratio bound noisy or imported trends.
    Negative trends never accelerate scale-down; residency cooldown remains its sole authority.
    """

    startup_horizon = min(
        policy.max_predictive_lookahead_seconds,
        (
            model.load_seconds + model.warm_seconds
            if startup_horizon_seconds is None
            else startup_horizon_seconds
        ),
    )
    base_rate = forecast.requests_per_minute
    if (
        offered_concurrency <= 0
        or base_rate <= 0
        or forecast.trend_per_minute <= 0
        or forecast.confidence <= 0
        or startup_horizon <= 0
    ):
        return offered_concurrency
    trend_growth = (
        forecast.trend_per_minute * (startup_horizon / 60.0) * forecast.confidence
    )
    projected_rate = min(
        base_rate * policy.predictive_growth_limit,
        base_rate + trend_growth,
    )
    return offered_concurrency * (projected_rate / base_rate)


def _replicas_for_service_capacity(
    model: ModelProfile,
    required_capacity: int,
    nodes: Iterable[NodeSnapshot],
    timestamp: float,
    policy: PlannerPolicy,
) -> tuple[int, int]:
    """Convert demand slots to replicas using only conservative, model-scoped evidence.

    A single-model ready engine may apply its advertised batch width. Multi-model engines retain a
    capacity of one because their node-wide limit is shared and cannot safely be credited to every
    model. Newly managed replicas use the explicit profile value. Missing physical candidates do
    not shrink desired state: virtual profile-capacity slots preserve the existing visible
    ``insufficient_capacity`` signal during outages.
    """

    if required_capacity <= 0:
        return 0, 0
    capacities: list[tuple[bool, int, str]] = []
    ready_count = 0
    for node in nodes:
        residency = node.residency(model.model_id)
        for_new = _requires_new_runtime(residency, model)
        if (
            _ineligible_reason(node, model, timestamp, policy, for_new=for_new)
            is not None
        ):
            continue
        is_ready = bool(
            residency
            and residency.state == ResidencyState.READY
            and model.matches_artifact(residency)
        )
        capacity = model.replica_concurrency
        if is_ready:
            ready_count += 1
            ready_models = sum(
                item.state == ResidencyState.READY for item in node.residencies
            )
            if ready_models == 1 and node.max_concurrency > 0:
                capacity = max(capacity, node.max_concurrency)
        capacities.append((is_ready, capacity, node.node_id))

    # Prefer already-ready capacity, matching the planner's much larger ready-residency bonus. Use
    # the narrowest ready engine first: target calculation must be safe for any ready replica the
    # later scorer can retain, rather than depending on it coincidentally choosing the widest one.
    capacities.sort(key=lambda item: (not item[0], item[1], item[2]))
    replicas = 0
    supplied = 0
    for _is_ready, capacity, _node_id in capacities:
        if replicas >= model.max_replicas or supplied >= required_capacity:
            break
        replicas += 1
        supplied = min(MAX_COUNTER, supplied + capacity)
    while replicas < model.max_replicas and supplied < required_capacity:
        replicas += 1
        supplied = min(MAX_COUNTER, supplied + model.replica_concurrency)
    return replicas, ready_count


def compatibility_reason(
    node: NodeSnapshot,
    model: ModelProfile,
    *,
    now: float | None = None,
    policy: PlannerPolicy | None = None,
    for_new: bool = True,
) -> str | None:
    return _ineligible_reason(
        node,
        model,
        time.time() if now is None else now,
        policy or PlannerPolicy(),
        for_new=for_new,
    )


def _bounded_replica_ceil(value: float, maximum: int) -> int:
    """Ceil demand without letting valid-but-extreme floats overflow Python integers."""

    if maximum <= 0 or value <= 0:
        return 0
    if not math.isfinite(value) or value >= maximum:
        return maximum
    return math.ceil(value)


def _requires_new_runtime(
    residency: ModelResidency | None,
    model: ModelProfile,
) -> bool:
    return (
        residency is None
        or not model.matches_artifact(residency)
        or residency.state
        in (
            ResidencyState.CACHED,
            ResidencyState.FAILED,
            ResidencyState.DRAINING,
        )
    )


def _ineligible_reason(
    node: NodeSnapshot | None,
    model: ModelProfile,
    now: float,
    policy: PlannerPolicy,
    *,
    for_new: bool,
) -> str | None:
    if node is None:
        return "node is missing"
    if node.state not in (NodeState.ACCEPTING, NodeState.THROTTLED):
        return f"node is {node.state.value}"
    if node.last_heartbeat <= 0:
        return "heartbeat is missing"
    if node.last_heartbeat > now + policy.max_future_clock_skew_seconds:
        return "heartbeat is in the future"
    if policy.node_ttl_seconds and now - node.last_heartbeat > policy.node_ttl_seconds:
        return "heartbeat is stale"
    residency = node.residency(model.model_id)
    if residency is not None and not model.matches_artifact(residency):
        if not residency.managed:
            return "external residency does not prove the requested artifact SHA-256"
        if residency.state in (
            ResidencyState.LOADING,
            ResidencyState.WARMING,
            ResidencyState.READY,
            ResidencyState.DRAINING,
        ):
            return "a different artifact version is live and must be replaced safely"
    if (
        residency is not None
        and not residency.managed
        and residency.state != ResidencyState.READY
    ):
        return f"external residency is {residency.state.value} and cannot be actuated"
    if model.data_tier not in node.allowed_data_tiers:
        return f"data tier {model.data_tier!r} is not allowed"
    if node.allowed_models and model.model_id not in node.allowed_models:
        return "model is not allowlisted"
    if model.model_id in node.denied_models:
        return "model is denied"
    if model.runtimes and not set(model.runtimes).intersection(node.runtimes):
        return "runtime is incompatible"
    if model.backends and not set(model.backends).intersection(node.backends):
        return "backend is incompatible"
    if node.gpu_count < model.min_gpu_count:
        return f"requires at least {model.min_gpu_count} GPUs"
    if model.min_gpu_memory_mb:
        required_devices = max(1, model.min_gpu_count)
        matching_devices = sum(
            memory_mb >= model.min_gpu_memory_mb for memory_mb in node.gpu_memory_mb
        )
        if matching_devices < required_devices:
            return (
                f"requires {required_devices} GPU(s) with at least "
                f"{model.min_gpu_memory_mb} MB each"
            )
    topology_reason = _gpu_topology_violation(node, model)
    if topology_reason:
        return topology_reason
    if not set(model.required_tags).issubset(node.tags):
        return "required node tags are missing"
    if set(model.forbidden_tags).intersection(node.tags):
        return "node has a forbidden tag"
    if for_new and (node.manually_managed or "warm" not in node.actuator_capabilities):
        return "node cannot be actuated"
    artifact_cached = _artifact_cached_residency(node, model, residency)
    if for_new and not artifact_cached and "load" not in node.actuator_capabilities:
        return "node cannot load uncached model weights"
    if (
        for_new
        and not artifact_cached
        and model.artifact_size_mb
        and node.disk_available_mb is not None
        and model.artifact_size_mb > node.disk_available_mb
    ):
        return (
            f"requires {model.artifact_size_mb} MB artifact disk, "
            f"only {node.disk_available_mb} MB available"
        )
    return None


def _artifact_cached_residency(
    node: NodeSnapshot,
    model: ModelProfile,
    residency: ModelResidency | None,
) -> bool:
    return (
        not model.artifact_sha256 and model.model_id in node.cached_models
    ) or bool(
        residency
        and residency.managed
        and residency.state
        in (
            ResidencyState.CACHED,
            ResidencyState.DRAINING,
            ResidencyState.FAILED,
        )
        and model.matches_artifact(residency)
    )


def _gpu_topology_violation(node: NodeSnapshot, model: ModelProfile) -> str | None:
    """Prove that one schedulable GPU subset satisfies advanced shard constraints.

    Legacy count/VRAM profiles remain compatible with nodes that predate topology reporting.
    Once a profile asks for link or NUMA guarantees, missing topology fails closed.
    """

    advanced = bool(
        model.min_gpu_interconnect_gbps or model.require_single_numa_node or not model.allow_mig
    )
    if not advanced:
        return None
    if not node.gpu_devices:
        return "GPU topology is unknown"
    required = max(
        model.min_gpu_count,
        2 if model.min_gpu_interconnect_gbps else 1,
        1 if model.min_gpu_memory_mb or model.require_single_numa_node else 0,
    )
    devices = tuple(
        item
        for item in node.gpu_devices
        if (not model.min_gpu_memory_mb or item.memory_mb >= model.min_gpu_memory_mb)
        and (model.allow_mig or not item.is_mig)
    )
    if len(devices) < required:
        if not model.allow_mig and any(item.is_mig for item in node.gpu_devices):
            return f"requires {required} non-MIG GPU(s)"
        return f"GPU topology has no eligible {required}-device shard set"
    links = {
        frozenset((item.device_a, item.device_b)): item for item in node.gpu_links
    }
    for selected in combinations(devices, required):
        if model.require_single_numa_node:
            numa_nodes = {item.numa_node for item in selected}
            if -1 in numa_nodes or len(numa_nodes) != 1:
                continue
        if model.min_gpu_interconnect_gbps and any(
            (link := links.get(frozenset((first.device_id, second.device_id)))) is None
            or link.bandwidth_gbps < model.min_gpu_interconnect_gbps
            for first, second in combinations(selected, 2)
        ):
            continue
        return None
    requirements: list[str] = []
    if model.min_gpu_interconnect_gbps:
        requirements.append(f"{model.min_gpu_interconnect_gbps:g} GB/s all-peer links")
    if model.require_single_numa_node:
        requirements.append("one known NUMA node")
    return "no GPU shard set satisfies " + " and ".join(requirements)


def _artifact_load_disk_mb(node: NodeSnapshot, model: ModelProfile) -> int:
    """Additional artifact bytes a new desired residency must reserve on this node."""

    # This fact is cached once per topology pair by the main planner. Read the immutable tuple
    # directly so adding disk accounting does not add another public residency lookup to every
    # fairness round; the ordinary compatibility/runtime caches own those lookups already.
    residency = next(
        (item for item in node.residencies if item.model_id == model.model_id),
        None,
    )
    if not _requires_new_runtime(residency, model):
        return 0
    return (
        0
        if _artifact_cached_residency(node, model, residency)
        else model.artifact_size_mb
    )


def _hard_residency_policy_violation(
    node: NodeSnapshot,
    model: ModelProfile,
) -> str | None:
    """Return a stable policy violation that an existing runtime cannot satisfy in place.

    Transient node health, heartbeat age, actuator availability, artifact replacement, capacity,
    and colocation are intentionally handled by their existing safety paths. This helper is only
    for administrator and hardware constraints that make continued service on this node invalid.
    """

    if model.data_tier not in node.allowed_data_tiers:
        return "data tier is not allowed"
    if node.allowed_models and model.model_id not in node.allowed_models:
        return "model is not allowlisted"
    if model.model_id in node.denied_models:
        return "model is denied"
    if model.runtimes and not set(model.runtimes).intersection(node.runtimes):
        return "runtime is incompatible"
    if model.backends and not set(model.backends).intersection(node.backends):
        return "backend is incompatible"
    if node.gpu_count < model.min_gpu_count:
        return "GPU count is insufficient"
    if model.min_gpu_memory_mb:
        required_devices = max(1, model.min_gpu_count)
        matching_devices = sum(
            memory_mb >= model.min_gpu_memory_mb for memory_mb in node.gpu_memory_mb
        )
        if matching_devices < required_devices:
            return "GPU memory is insufficient"
    if not set(model.required_tags).issubset(node.tags):
        return "required node tags are missing"
    if set(model.forbidden_tags).intersection(node.tags):
        return "node has a forbidden tag"
    return None


def _fits(
    node: NodeSnapshot,
    model: ModelProfile,
    capacity: dict[str, int],
    occupied_models: dict[str, int],
    desired_model_slots: dict[str, int],
    assignments: list[PlacementAssignment],
    profile_by_id: Mapping[str, ModelProfile],
) -> bool:
    residency = node.residency(model.model_id)
    incremental_mb = _incremental_memory_mb(residency, model.memory_for(node.runtimes))
    if capacity[node.node_id] < incremental_mb:
        return False
    adds_slot = _adds_model_slot(residency)
    if node.max_models is not None:
        if desired_model_slots[node.node_id] >= node.max_models:
            return False
        if adds_slot and occupied_models[node.node_id] >= node.max_models:
            return False
    return _colocation_allowed(node, model, assignments, profile_by_id)


def _evidence_preemption_allowed(
    victim_model_id: str,
    beneficiary_model_id: str,
    victim_priority: int,
    beneficiary_priority: int,
    urgency_by_model: Mapping[str, int],
    assignment_counts: Mapping[str, int],
) -> bool:
    """Allow bounded evidence rebalancing without letting speculation harm real service."""

    if victim_priority > beneficiary_priority:
        return False
    victim_urgency = urgency_by_model.get(victim_model_id, 0)
    beneficiary_urgency = urgency_by_model.get(beneficiary_model_id, 0)
    if beneficiary_urgency >= 2 and (
        victim_priority < beneficiary_priority or victim_urgency < 2
    ):
        return True
    if beneficiary_urgency != 1 or victim_urgency > 1:
        return False
    if victim_urgency == 0:
        return True
    # Both are speculative. Guarantee a newly demanded model one canary only by taking an excess
    # replica; a model's sole canary is never exchanged for another equally weak hypothesis.
    return (
        assignment_counts.get(beneficiary_model_id, 0) == 0
        and assignment_counts.get(victim_model_id, 0) > 1
    )


def _priority_preemption_candidates(
    beneficiary: ModelProfile,
    nodes: list[NodeSnapshot],
    assignments_by_node: Mapping[str, list[PlacementAssignment]],
    assigned_pairs: set[tuple[str, str]],
    capacity: Mapping[str, int],
    occupied_models: Mapping[str, int],
    desired_model_slots: Mapping[str, int],
    disk_remaining: Mapping[str, int | None],
    profile_by_id: Mapping[str, ModelProfile],
    demand_urgency_by_model: Mapping[str, int],
    desired_by_model: Mapping[str, int],
    now: float,
    policy: PlannerPolicy,
    startup_seconds: Mapping[tuple[str, str], float],
    load_seconds: Mapping[tuple[str, str], float],
    *,
    required_node_id: str | None,
    required_victim_ids: frozenset[str] | None,
    excluded_nodes: set[str],
) -> list[_PreemptionCandidate]:
    """Prove every currently independent node-local victim set in one fleet scan."""

    candidates: list[_PreemptionCandidate] = []
    assignment_counts = Counter(
        assignment.model_id
        for node_assignments in assignments_by_node.values()
        for assignment in node_assignments
    )
    assignments_on_node = {
        node_id: {assignment.model_id for assignment in node_assignments}
        for node_id, node_assignments in assignments_by_node.items()
    }
    for node in nodes:
        if required_node_id is not None and node.node_id != required_node_id:
            continue
        if node.node_id in excluded_nodes:
            continue
        if (beneficiary.model_id, node.node_id) in assigned_pairs:
            continue
        residency = node.residency(beneficiary.model_id)
        if (
            _ineligible_reason(
                node,
                beneficiary,
                now,
                policy,
                for_new=_requires_new_runtime(residency, beneficiary),
            )
            is not None
        ):
            continue
        victims = sorted(
            (
                item
                for item in node.residencies
                if item.model_id != beneficiary.model_id
                and not _adds_model_slot(item)
                and item.state
                in (
                    ResidencyState.READY,
                    ResidencyState.DRAINING,
                    ResidencyState.FAILED,
                )
                and item.managed
                and not item.pinned
                and not node.manually_managed
                and (victim_profile := profile_by_id.get(item.model_id)) is not None
                and (
                    (
                        desired_by_model.get(item.model_id, 0) > 0
                        and assignment_counts.get(item.model_id, 0)
                        >= desired_by_model[item.model_id]
                        and item.model_id
                        not in assignments_on_node.get(node.node_id, set())
                    )
                    or _evidence_preemption_allowed(
                        item.model_id,
                        beneficiary.model_id,
                        victim_profile.priority,
                        beneficiary.priority,
                        demand_urgency_by_model,
                        assignment_counts,
                    )
                )
                and node.node_id not in victim_profile.pinned_nodes
            ),
            key=lambda item: (
                profile_by_id[item.model_id].priority,
                demand_urgency_by_model.get(item.model_id, 0),
                item.active_requests,
                _preemption_state_cost(item.state),
                startup_seconds.get(
                    (node.node_id, item.model_id),
                    profile_by_id[item.model_id].warm_seconds,
                ),
                -item.memory_mb,
                item.model_id,
            ),
        )
        if not victims:
            continue

        simulated_assignments = list(assignments_by_node.get(node.node_id, ()))
        simulated_capacity = {node.node_id: capacity[node.node_id]}
        simulated_occupied = {node.node_id: occupied_models[node.node_id]}
        simulated_slots = {node.node_id: desired_model_slots[node.node_id]}
        # A prior bounded wave may already have freed part of this host. Do not let a fresh,
        # lower-priority cold assignment immediately consume that progress and deadlock the next
        # wave. Such an assignment is only planner intent, so it can be displaced without an
        # actuator command or service interruption.
        displaced_assignments = tuple(
            item
            for item in simulated_assignments
            if _adds_model_slot(node.residency(item.model_id))
            and (profile := profile_by_id.get(item.model_id)) is not None
            and (
                _evidence_preemption_allowed(
                    item.model_id,
                    beneficiary.model_id,
                    profile.priority,
                    beneficiary.priority,
                    demand_urgency_by_model,
                    assignment_counts,
                )
            )
        )
        projected_disk = disk_remaining[node.node_id]
        if projected_disk is not None:
            projected_disk += sum(
                _artifact_load_disk_mb(
                    node,
                    profile_by_id[item.model_id],
                )
                for item in displaced_assignments
            )
            if _artifact_load_disk_mb(node, beneficiary) > projected_disk:
                continue
        for assignment in displaced_assignments:
            simulated_assignments.remove(assignment)
            assignment_residency = node.residency(assignment.model_id)
            simulated_capacity[node.node_id] += _incremental_memory_mb(
                assignment_residency,
                assignment.memory_mb,
            )
            simulated_slots[node.node_id] -= 1
            simulated_occupied[node.node_id] = max(
                0,
                simulated_occupied[node.node_id] - 1,
            )
        selected: list[ModelResidency] = []
        for victim in victims:
            selected.append(victim)
            assignment = next(
                (
                    item
                    for item in simulated_assignments
                    if item.model_id == victim.model_id
                ),
                None,
            )
            if assignment is not None:
                simulated_assignments.remove(assignment)
                simulated_capacity[node.node_id] += _incremental_memory_mb(
                    victim,
                    assignment.memory_mb,
                )
                simulated_slots[node.node_id] -= 1
            simulated_capacity[node.node_id] += victim.memory_mb
            simulated_occupied[node.node_id] = max(
                0,
                simulated_occupied[node.node_id] - 1,
            )
            projected = replace(
                node,
                residencies=tuple(
                    item for item in node.residencies if item not in selected
                ),
            )
            if not _fits(
                projected,
                beneficiary,
                simulated_capacity,
                simulated_occupied,
                simulated_slots,
                simulated_assignments,
                profile_by_id,
            ):
                continue
            victim_profiles = [profile_by_id[item.model_id] for item in selected]
            beneficiary_warm_seconds = startup_seconds.get(
                (node.node_id, beneficiary.model_id),
                beneficiary.warm_seconds,
            )
            beneficiary_cached = (
                not beneficiary.artifact_sha256
                and beneficiary.model_id in node.cached_models
            ) or bool(
                residency
                and residency.managed
                and residency.state
                in (
                    ResidencyState.CACHED,
                    ResidencyState.DRAINING,
                    ResidencyState.FAILED,
                )
                and beneficiary.matches_artifact(residency)
            )
            beneficiary_startup_seconds = beneficiary_warm_seconds
            if not beneficiary_cached:
                beneficiary_startup_seconds += load_seconds.get(
                    (node.node_id, beneficiary.model_id),
                    beneficiary.load_seconds,
                )
            candidates.append(
                _PreemptionCandidate(
                    sort_key=(
                        sum(item.priority for item in victim_profiles),
                        sum(
                            demand_urgency_by_model.get(item.model_id, 0)
                            for item in selected
                        ),
                        sum(item.active_requests for item in selected),
                        sum(_preemption_state_cost(item.state) for item in selected),
                        node.queue_depth,
                        beneficiary_startup_seconds,
                        sum(
                            startup_seconds.get(
                                (node.node_id, item.model_id),
                                profile_by_id[item.model_id].warm_seconds,
                            )
                            for item in selected
                        ),
                        len(selected),
                        sum(item.memory_mb for item in selected),
                        node.node_id,
                    ),
                    node=node,
                    victims=tuple(selected),
                    displaced_assignments=displaced_assignments,
                )
            )
            if (
                required_victim_ids is not None
                and frozenset(item.model_id for item in selected)
                != required_victim_ids
            ):
                candidates.pop()
            break
    return candidates


def _stage_priority_preemption(
    beneficiary: ModelProfile,
    nodes: list[NodeSnapshot],
    assignments: list[PlacementAssignment],
    assigned_pairs: set[tuple[str, str]],
    assigned_domains: dict[str, set[str]],
    capacity: dict[str, int],
    occupied_models: dict[str, int],
    desired_model_slots: dict[str, int],
    disk_remaining: dict[str, int | None],
    profile_by_id: Mapping[str, ModelProfile],
    demand_urgency_by_model: Mapping[str, int],
    desired_by_model: Mapping[str, int],
    now: float,
    policy: PlannerPolicy,
    startup_seconds: Mapping[tuple[str, str], float],
    load_seconds: Mapping[tuple[str, str], float],
    *,
    require_new_domain: bool,
    existing_domains: set[str],
    required_node_id: str | None,
    excluded_nodes: set[str],
    max_victims: int,
    assignments_by_node: dict[str, list[PlacementAssignment]],
    candidate_cache: _PreemptionSearchCache | None,
    required_victim_ids: frozenset[str] | None,
) -> tuple[str, tuple[ModelResidency, ...]] | None:
    """Prove a lower-priority/evidence eviction path, then return its bounded next wave."""

    if candidate_cache is not None and candidate_cache.candidates is not None:
        candidates = [
            item
            for item in candidate_cache.candidates
            if item.node.node_id not in excluded_nodes
        ]
    else:
        candidates = _priority_preemption_candidates(
            beneficiary,
            nodes,
            assignments_by_node,
            assigned_pairs,
            capacity,
            occupied_models,
            desired_model_slots,
            disk_remaining,
            profile_by_id,
            demand_urgency_by_model,
            desired_by_model,
            now,
            policy,
            startup_seconds,
            load_seconds,
            required_node_id=required_node_id,
            required_victim_ids=required_victim_ids,
            excluded_nodes=excluded_nodes,
        )
        if candidate_cache is not None:
            candidate_cache.candidates = sorted(
                candidates,
                key=lambda item: item.sort_key,
            )

    if not candidates:
        return None
    if require_new_domain:
        new_domain_candidates = [
            item
            for item in candidates
            if (item.node.failure_domain or item.node.node_id) not in existing_domains
        ]
        if new_domain_candidates:
            candidates = new_domain_candidates
    selected_candidate = min(
        candidates,
        key=lambda item: item.sort_key,
    )
    selected_node = selected_candidate.node
    victims = selected_candidate.victims
    displaced_assignments = selected_candidate.displaced_assignments
    if candidate_cache is not None and candidate_cache.candidates is not None:
        candidate_cache.candidates.remove(selected_candidate)
    for assignment in displaced_assignments:
        assignments.remove(assignment)
        assignments_by_node[selected_node.node_id].remove(assignment)
        assigned_pairs.remove((assignment.model_id, selected_node.node_id))
        residency = selected_node.residency(assignment.model_id)
        capacity[selected_node.node_id] += _incremental_memory_mb(
            residency,
            assignment.memory_mb,
        )
        desired_model_slots[selected_node.node_id] -= 1
        if _adds_model_slot(residency):
            occupied_models[selected_node.node_id] = max(
                0,
                occupied_models[selected_node.node_id] - 1,
            )
        remaining_disk = disk_remaining[selected_node.node_id]
        if remaining_disk is not None:
            disk_remaining[selected_node.node_id] = (
                remaining_disk
                + _artifact_load_disk_mb(
                    selected_node,
                    profile_by_id[assignment.model_id],
                )
            )
        assigned_domains[assignment.model_id] = {
            node.failure_domain or node.node_id
            for node in nodes
            if (assignment.model_id, node.node_id) in assigned_pairs
        }
    victims = victims[:max_victims]
    for victim in victims:
        assignment = next(
            (
                item
                for item in assignments
                if item.node_id == selected_node.node_id
                and item.model_id == victim.model_id
            ),
            None,
        )
        if assignment is not None:
            assignments.remove(assignment)
            assignments_by_node[selected_node.node_id].remove(assignment)
            assigned_pairs.remove((victim.model_id, selected_node.node_id))
            capacity[selected_node.node_id] += _incremental_memory_mb(
                victim,
                assignment.memory_mb,
            )
            desired_model_slots[selected_node.node_id] -= 1
            assigned_domains[victim.model_id] = {
                node.failure_domain or node.node_id
                for node in nodes
                if (victim.model_id, node.node_id) in assigned_pairs
            }
        capacity[selected_node.node_id] += victim.memory_mb
        occupied_models[selected_node.node_id] = max(
            0,
            occupied_models[selected_node.node_id] - 1,
        )
    return selected_node.node_id, victims


def _preemption_state_cost(state: ResidencyState) -> int:
    """Rank how much live lifecycle progress an eviction would disrupt."""

    return {
        ResidencyState.FAILED: 0,
        ResidencyState.DRAINING: 1,
        ResidencyState.READY: 2,
    }.get(state, 3)


def _colocation_allowed(
    node: NodeSnapshot,
    model: ModelProfile,
    assignments: Iterable[PlacementAssignment],
    profile_by_id: Mapping[str, ModelProfile],
) -> bool:
    if (
        not model.max_colocated_models
        and not model.colocation_excludes
        and not any(
            profile.max_colocated_models or profile.colocation_excludes
            for profile in profile_by_id.values()
        )
    ):
        return True
    other_models = {
        residency.model_id
        for residency in node.residencies
        if residency.model_id != model.model_id and not _adds_model_slot(residency)
    }
    other_models.update(
        assignment.model_id
        for assignment in assignments
        if assignment.node_id == node.node_id and assignment.model_id != model.model_id
    )
    colocated_count = len(other_models) + 1
    if set(model.colocation_excludes).intersection(other_models):
        return False
    if model.max_colocated_models and colocated_count > model.max_colocated_models:
        return False
    for other_model in other_models:
        peer = profile_by_id.get(other_model)
        if peer is not None and (
            model.model_id in peer.colocation_excludes
            or (
                peer.max_colocated_models
                and colocated_count > peer.max_colocated_models
            )
        ):
            return False
    return True


def _candidate_score(
    node: NodeSnapshot,
    model: ModelProfile,
    remaining_mb: int,
    domains: set[str],
    policy: PlannerPolicy,
    *,
    forecast: DemandForecast | None,
    now: float,
    need_new_domain: bool,
    startup_seconds: Mapping[tuple[str, str], float],
    load_seconds: Mapping[tuple[str, str], float],
) -> tuple[float, tuple[str, ...]]:
    score = 0.0
    reasons: list[str] = []
    residency = node.residency(model.model_id)
    startup_key = (node.node_id, model.model_id)
    warm_seconds = startup_seconds.get(startup_key, model.warm_seconds)
    artifact_load_seconds = load_seconds.get(startup_key, model.load_seconds)
    performance_value_weight = _performance_value_weight(model, forecast)
    artifact_matches = model.matches_artifact(residency)
    if residency and residency.state == ResidencyState.READY and artifact_matches:
        score += 100_000.0
        reasons.append("already resident and ready")
    elif (
        residency
        and artifact_matches
        and residency.state
        in (
            ResidencyState.LOADING,
            ResidencyState.WARMING,
        )
    ):
        score += 75_000.0
        reasons.append("already loading")
    elif (not model.artifact_sha256 and model.model_id in node.cached_models) or (
        residency
        and artifact_matches
        and residency.state in (ResidencyState.CACHED, ResidencyState.FAILED)
    ):
        score += 20_000.0 - min(warm_seconds, 1_000_000_000_000.0) * 20.0
        reasons.append("weights cached locally")
    else:
        if model.artifact_size_mb and node.transfer_bandwidth_mbps:
            transfer_seconds = (
                model.artifact_size_mb
                * 8.0
                / node.transfer_bandwidth_mbps
                * (1.0 + node.active_transfers)
            )
            if transfer_seconds > artifact_load_seconds:
                artifact_load_seconds = transfer_seconds
                reasons.append("transfer-bandwidth cold-load estimate")
        cold_seconds = artifact_load_seconds + warm_seconds
        score -= min(cold_seconds, 1_000_000_000_000.0) * 20.0
        reasons.append("cold load required")
    if startup_key in startup_seconds and not (
        residency
        and artifact_matches
        and residency.state
        in (ResidencyState.READY, ResidencyState.LOADING, ResidencyState.WARMING)
    ):
        reasons.append("learned warm-start estimate")
    if startup_key in load_seconds and not (
        residency
        and artifact_matches
        and residency.state
        in (
            ResidencyState.READY,
            ResidencyState.LOADING,
            ResidencyState.WARMING,
            ResidencyState.CACHED,
            ResidencyState.DRAINING,
            ResidencyState.FAILED,
        )
    ):
        reasons.append("learned artifact-load estimate")
    if (
        residency
        and residency.state == ResidencyState.FAILED
        and residency.load_failures
    ):
        score -= min(residency.load_failures, 1_000) * policy.model_failure_penalty
        reasons.append("prior model failures")

    domain = node.failure_domain or node.node_id
    if domain not in domains:
        score += 250_000.0 if need_new_domain else 5_000.0
        reasons.append("adds a failure domain")
    elif need_new_domain:
        score -= 250_000.0
    model_memory_mb = model.memory_for(node.runtimes)
    after = remaining_mb - _incremental_memory_mb(residency, model_memory_mb)
    # Best fit preserves a large contiguous-capacity host for a future large model.
    score += 2_000.0 / (1.0 + after / max(model_memory_mb, 1))
    model_performance = node.performance(model.model_id)
    model_latency_weight = 0.0
    model_throughput_weight = 0.0
    if (
        model_performance is not None
        and model.artifact_sha256
        and model_performance.artifact_sha256 != model.artifact_sha256
    ):
        # A same-named model revision can have materially different kernels, quantization, and
        # serving behavior. Never let measurements from the old immutable artifact rank the new
        # one, even during the make-before-break transition where both revisions are visible.
        model_performance = None
    if model_performance is not None:
        performance_age = now - model_performance.updated_at
        if not model_performance.updated_at or not (
            -policy.max_future_clock_skew_seconds
            <= performance_age
            < policy.performance_ttl_seconds
        ):
            model_performance = None
        else:
            latency_confidence = min(
                1.0,
                model_performance.sample_count
                / policy.performance_full_confidence_samples,
            )
            throughput_confidence = min(
                1.0,
                model_performance.throughput_sample_count
                / policy.performance_full_confidence_samples,
            )
            freshness = (
                max(
                    0.0,
                    1.0 - max(0.0, performance_age) / policy.performance_ttl_seconds,
                )
                if policy.performance_ttl_seconds
                else 1.0
            )
            throughput_age = now - model_performance.throughput_updated_at
            throughput_freshness = (
                max(
                    0.0,
                    1.0 - max(0.0, throughput_age) / policy.performance_ttl_seconds,
                )
                if model_performance.throughput_updated_at
                and -policy.max_future_clock_skew_seconds
                <= throughput_age
                < policy.performance_ttl_seconds
                and policy.performance_ttl_seconds
                else 0.0
            )
            model_latency_weight = latency_confidence * freshness
            model_throughput_weight = throughput_confidence * throughput_freshness
            if not model_latency_weight and not model_throughput_weight:
                model_performance = None
    ready_models = sum(item.state == ResidencyState.READY for item in node.residencies)
    # A node-wide metric is safe for an empty/single-model engine, and remains a useful generic
    # benchmark for a cold host. Once an engine serves several models it is not attributable: use
    # only proxy measurements tagged with this exact model, or fall back to hardware priors.
    node_metric_is_attributable = ready_models <= 1 and not model.artifact_sha256
    if model_performance is not None:
        measured_tokens_per_second = (
            model_performance.tokens_per_second if model_throughput_weight > 0 else 0.0
        )
        measured_latency_ms = (
            model_performance.latency_ms if model_latency_weight > 0 else 0.0
        )
    else:
        measured_tokens_per_second = (
            node.tokens_per_second if node_metric_is_attributable else 0.0
        )
        measured_latency_ms = node.latency_ms if node_metric_is_attributable else 0.0
    if measured_tokens_per_second:
        throughput_weight = (
            model_throughput_weight if model_performance is not None else 1.0
        )
        score += (
            min(measured_tokens_per_second, 10_000.0)
            * 2.0
            * throughput_weight
            * performance_value_weight
        )
        reasons.append("measured throughput")
    hardware_weight = 1.0
    if measured_tokens_per_second:
        hardware_weight = (
            1.0 - model_throughput_weight if model_performance is not None else 0.0
        )
    if hardware_weight and (node.memory_bandwidth_gbps or node.compute_gflops):
        # Model serving is usually bandwidth-bound; compute is a smaller secondary prior. These
        # estimates break cold/unmeasured ties and blend out as proxy evidence matures. READY and
        # cached bonuses above are intentionally much larger, so hardware heterogeneity never
        # causes gratuitous migration.
        score += (
            min(node.memory_bandwidth_gbps, 2_000.0)
            * 10.0
            * hardware_weight
            * performance_value_weight
        )
        score += (
            min(math.log2(1.0 + node.compute_gflops) * 100.0, 2_000.0)
            * hardware_weight
            * performance_value_weight
        )
        reasons.append("hardware performance estimate")
    if hardware_weight and model.min_gpu_count > 1 and node.gpu_links:
        # A fast all-peer fabric improves tensor-parallel collectives. The hard compatibility gate
        # above proves the required clique; this small prior only breaks otherwise cold ties.
        fabric_bandwidth = max(item.bandwidth_gbps for item in node.gpu_links)
        score += min(fabric_bandwidth, 2_000.0) * 5.0 * hardware_weight
        reasons.append("GPU fabric bandwidth")
    if measured_latency_ms:
        latency_weight = model_latency_weight if model_performance is not None else 1.0
        score -= (
            min(measured_latency_ms, 60_000.0)
            / 50.0
            * latency_weight
            * performance_value_weight
        )
    if performance_value_weight > 1 and (
        measured_tokens_per_second
        or measured_latency_ms
        or node.memory_bandwidth_gbps
        or node.compute_gflops
    ):
        reasons.append("performance value amortized by demand")
    if node.max_concurrency > 0:
        utilization = node.active_requests / node.max_concurrency
        if utilization:
            score -= min(utilization, 2.0) * 10_000.0
            reasons.append("current request load")
    if node.queue_depth:
        score -= min(node.queue_depth, 100) * 500.0
        reasons.append("queued work")
    if node.state == NodeState.THROTTLED:
        score -= 20_000.0 * (1.0 - policy.throttled_capacity_fraction)
        reasons.append("host is throttled")
    score -= min(max(0, node.host_priority), 1_000_000_000_000) * 100.0
    return score, tuple(reasons)


def _performance_value_weight(
    model: ModelProfile,
    forecast: DemandForecast | None,
) -> float:
    """Bound how strongly sustained demand can amortize a faster host's cold start."""

    if forecast is None:
        return 1.0
    offered_concurrency = max(
        forecast.offered_concurrency,
        float(forecast.queue_depth),
    )
    if offered_concurrency <= 0 and forecast.requests_per_minute > 0:
        offered_concurrency = (
            forecast.requests_per_minute / 60.0 * model.expected_service_seconds
        )
    replica_capacity = model.replica_concurrency * model.target_utilization
    return min(8.0, max(1.0, offered_concurrency / replica_capacity))


def _assignment(
    model: ModelProfile,
    node: NodeSnapshot,
    *,
    index: int,
    score: float,
    reasons: tuple[str, ...],
) -> PlacementAssignment:
    residency = node.residency(model.model_id)
    selected_runtime = model.runtime_for(node.runtimes)
    # Residency is sticky across otherwise equivalent compatible runtimes. Avoid replacing a live
    # Ollama/vLLM/llama.cpp instance merely because another installed engine has a smaller modeled
    # footprint; migrations require an explicit replacement plan and canary, not lexical ordering.
    if (
        residency is not None
        and residency.runtime
        and residency.runtime in node.runtimes
        and residency.runtime in model.runtimes
    ):
        selected_runtime = residency.runtime
    return PlacementAssignment(
        model_id=model.model_id,
        node_id=node.node_id,
        memory_mb=model.memory_for_runtime(selected_runtime),
        replica_index=index,
        score=score,
        existing=bool(
            residency
            and residency.state == ResidencyState.READY
            and model.matches_artifact(residency)
        ),
        reasons=reasons,
        runtime=selected_runtime,
    )


def _place(
    assignment: PlacementAssignment,
    node: NodeSnapshot,
    assignments: list[PlacementAssignment],
    assigned_pairs: set[tuple[str, str]],
    domains: set[str],
    capacity: dict[str, int],
    occupied_models: dict[str, int],
    desired_model_slots: dict[str, int],
    desired_memory: dict[str, int],
    disk_remaining: dict[str, int | None],
    artifact_disk_mb: int,
) -> None:
    assignments.append(assignment)
    assigned_pairs.add((assignment.model_id, assignment.node_id))
    domains.add(node.failure_domain or node.node_id)
    residency = node.residency(assignment.model_id)
    capacity[node.node_id] -= _incremental_memory_mb(residency, assignment.memory_mb)
    desired_memory[node.node_id] += assignment.memory_mb
    desired_model_slots[node.node_id] += 1
    remaining_disk = disk_remaining[node.node_id]
    if remaining_disk is not None:
        disk_remaining[node.node_id] = remaining_disk - artifact_disk_mb
    if _adds_model_slot(residency):
        occupied_models[node.node_id] += 1


def _incremental_memory_mb(
    residency: ModelResidency | None,
    memory_mb: int,
) -> int:
    if residency is None or residency.state == ResidencyState.CACHED:
        return memory_mb
    if residency.state == ResidencyState.FAILED and not residency.managed:
        return memory_mb
    if not residency.managed:
        # An external live engine is usable inventory, not a process the allocator will resize.
        return 0
    return max(0, memory_mb - residency.memory_mb)


def _adds_model_slot(residency: ModelResidency | None) -> bool:
    """Whether assigning this model consumes a slot beyond the observed residency."""

    return (
        residency is None
        or residency.state == ResidencyState.CACHED
        or (residency.state == ResidencyState.FAILED and not residency.managed)
    )


def _require_unique(values: Iterable[str], kind: str) -> None:
    items = list(values)
    if len(items) != len(set(items)):
        raise ValueError(f"duplicate {kind} IDs are not allowed")


def _validated_startup_seconds(
    values: Mapping[tuple[str, str], float] | None,
) -> dict[tuple[str, str], float]:
    return _validated_duration_seconds(values, label="startup")


def _validated_load_seconds(
    values: Mapping[tuple[str, str], float] | None,
) -> dict[tuple[str, str], float]:
    return _validated_duration_seconds(values, label="load")


def _validated_duration_seconds(
    values: Mapping[tuple[str, str], float] | None,
    *,
    label: str,
) -> dict[tuple[str, str], float]:
    result: dict[tuple[str, str], float] = {}
    for key, raw_duration in (values or {}).items():
        if (
            not isinstance(key, tuple)
            or len(key) != 2
            or not all(isinstance(item, str) and item for item in key)
        ):
            raise ValueError(f"{label} estimate keys must contain node and model IDs")
        if isinstance(raw_duration, bool):
            raise ValueError(f"{label} estimates must be finite and non-negative")
        duration = float(raw_duration)
        if not math.isfinite(duration) or duration < 0:
            raise ValueError(f"{label} estimates must be finite and non-negative")
        result[key] = duration
    return result


def _input_digest(
    nodes: list[NodeSnapshot],
    models: list[ModelProfile],
    forecasts: dict[str, DemandForecast],
    *,
    desired_by_model: dict[str, int],
    assignments: list[PlacementAssignment],
    preemptions: list[PlacementPreemption],
    artifact_prefetches: list[ArtifactPrefetch],
    artifact_evictions: list[ArtifactEviction],
) -> str:
    return stable_digest(
        {
            "nodes": [node.to_dict() for node in nodes],
            "models": [model.to_dict() for model in models],
            "forecasts": [
                {
                    "model_id": item.model_id,
                    "requests_per_minute": item.requests_per_minute,
                    "offered_concurrency": item.offered_concurrency,
                    "queue_depth": item.queue_depth,
                    "p95_latency_ms": item.p95_latency_ms,
                    "error_rate": item.error_rate,
                    "trend_per_minute": item.trend_per_minute,
                    "confidence": item.confidence,
                    "correlated_requests_per_minute": item.correlated_requests_per_minute,
                    "correlation_confidence": item.correlation_confidence,
                    "correlation_sources": item.correlation_sources,
                    "prediction_lead_seconds": item.prediction_lead_seconds,
                    "observed_requests_per_minute": item.observed_requests_per_minute,
                    "canary_only": item.canary_only,
                    "preemption_authorized": item.preemption_authorized,
                    "preemption_node_id": item.preemption_node_id,
                    "preemption_victims": item.preemption_victims,
                    "sample_count": item.sample_count,
                    "updated_at": item.updated_at,
                }
                for item in sorted(forecasts.values(), key=lambda value: value.model_id)
            ],
            # These are the time-derived parts of desired state. They intentionally exclude scores
            # and prose so inconsequential diagnostics do not churn command generations.
            "desired_replicas": sorted(desired_by_model.items()),
            "assignments": [
                (item.model_id, item.node_id, item.memory_mb) for item in assignments
            ],
            "preemptions": [
                (item.node_id, item.model_id, item.for_model_id) for item in preemptions
            ],
            "artifact_prefetches": [
                (item.node_id, item.model_id) for item in artifact_prefetches
            ],
            "artifact_evictions": [
                (item.node_id, item.model_id, item.for_model_id)
                for item in artifact_evictions
            ],
        }
    )
