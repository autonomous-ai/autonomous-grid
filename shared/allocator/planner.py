"""Deterministic, capacity-aware model placement for a heterogeneous Grid.

This is intentionally a transparent heuristic rather than a black-box solver.  It computes replica
need from observed concurrency, preserves warm state, spreads replicas across failure domains, and
uses best-fit placement to avoid memory fragmentation.  Every shortfall is returned as data; the
planner never overcommits a node to make a dashboard look green.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from itertools import combinations

from shared.allocator.models import (
    MAX_COUNTER,
    DemandForecast,
    ModelProfile,
    ModelResidency,
    NodeSnapshot,
    NodeState,
    PlacementAssignment,
    PlacementPlan,
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
            not math.isfinite(self.latency_pressure_limit)
            or not math.isfinite(self.error_pressure_limit)
            or self.latency_pressure_limit < 1
            or self.error_pressure_limit < 0
        ):
            raise ValueError("pressure limits are invalid")
        if not 0 < self.throttled_capacity_fraction <= 1:
            raise ValueError("throttled_capacity_fraction must be in (0, 1]")


_MAX_REPACK_SEARCH_STATES = 10_000
_MAX_REPACK_DEPTH = 64


@dataclass(slots=True)
class _RepackSearchState:
    """Bound one deterministic augmenting search and prune placement cycles."""

    explored_steps: int = 0
    visited: set[
        tuple[
            tuple[tuple[str, int, int], ...],
            tuple[tuple[str, str, int], ...],
        ]
    ] = field(default_factory=set)

    def consume_step(self) -> bool:
        if self.explored_steps >= _MAX_REPACK_SEARCH_STATES:
            return False
        self.explored_steps += 1
        return True


@dataclass(frozen=True, slots=True)
class _PendingReplica:
    model_id: str
    replica_index: int
    domain_floor: int


class PlacementPlanner:
    def __init__(self, policy: PlannerPolicy | None = None) -> None:
        self.policy = policy or PlannerPolicy()

    def plan(
        self,
        nodes: Iterable[NodeSnapshot],
        models: Iterable[ModelProfile],
        forecasts: Iterable[DemandForecast] = (),
        *,
        now: float | None = None,
        startup_seconds: Mapping[tuple[str, str], float] | None = None,
    ) -> PlacementPlan:
        timestamp = time.time() if now is None else float(now)
        if not math.isfinite(timestamp) or timestamp < 0:
            raise ValueError("now must be finite and non-negative")
        node_list = sorted(nodes, key=lambda item: item.node_id)
        model_list = sorted(models, key=lambda item: item.model_id)
        _require_unique((node.node_id for node in node_list), "node")
        _require_unique((model.model_id for model in model_list), "model")
        forecast_list = sorted(forecasts, key=lambda item: item.model_id)
        _require_unique((item.model_id for item in forecast_list), "forecast model")
        forecast_by_model = {item.model_id: item for item in forecast_list}
        startup_by_pair = _validated_startup_seconds(startup_seconds)
        capacity = {}
        for node in node_list:
            usable = (node.capacity_mb - node.reserved_mb) * (
                1.0 - self.policy.memory_headroom_fraction
            )
            if node.state == NodeState.THROTTLED:
                usable *= self.policy.throttled_capacity_fraction
            capacity[node.node_id] = max(0, math.floor(usable))
        occupied_models: dict[str, int] = {node.node_id: 0 for node in node_list}
        desired_model_slots: dict[str, int] = {node.node_id: 0 for node in node_list}
        assignments: list[PlacementAssignment] = []
        assigned_pairs: set[tuple[str, str]] = set()
        assigned_domains: dict[str, set[str]] = {}
        unsatisfied: list[UnsatisfiedConstraint] = []

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

        desired_by_model = {
            model.model_id: desired_replica_count(
                model,
                forecast_by_model.get(model.model_id),
                nodes=node_list,
                now=timestamp,
                policy=self.policy,
            )
            for model in model_list
        }

        # Higher-priority and larger models place first.  Larger-first is the standard bin-packing
        # guard against a collection of small replicas fragmenting every host before a large model.
        def eligible_host_count(model: ModelProfile) -> int:
            return sum(
                _ineligible_reason(
                    node,
                    model,
                    timestamp,
                    self.policy,
                    for_new=(
                        (residency := node.residency(model.model_id)) is None
                        or residency.state
                        in (
                            ResidencyState.CACHED,
                            ResidencyState.FAILED,
                            ResidencyState.DRAINING,
                        )
                    ),
                )
                is None
                and _fits(
                    node,
                    model,
                    capacity,
                    occupied_models,
                    desired_model_slots,
                )
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
                residency = node.residency(model.model_id) if node is not None else None
                for_new = residency is None or residency.state in (
                    ResidencyState.CACHED,
                    ResidencyState.FAILED,
                    ResidencyState.DRAINING,
                )
                reason = _ineligible_reason(
                    node,
                    model,
                    timestamp,
                    self.policy,
                    for_new=for_new,
                )
                if (
                    node is None
                    or reason is not None
                    or not _fits(
                        node,
                        model,
                        capacity,
                        occupied_models,
                        desired_model_slots,
                    )
                ):
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
                )
                pinned_successes_by_model[model.model_id] += 1

        # Recompute scarcity after hard pins have consumed their declared capacity. Within one
        # administrator-priority class, constrained models go before flexible models.
        order = sorted(
            model_list,
            key=lambda item: (
                -item.priority,
                eligible_host_count(item),
                -item.maximum_memory_mb,
                item.model_id,
            ),
        )
        profile_by_id = {item.model_id: item for item in model_list}
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

        def snapshot_placement_state() -> tuple[
            list[PlacementAssignment],
            set[tuple[str, str]],
            dict[str, set[str]],
            dict[str, int],
            dict[str, int],
            dict[str, int],
        ]:
            return (
                list(assignments),
                set(assigned_pairs),
                {key: set(value) for key, value in assigned_domains.items()},
                dict(capacity),
                dict(occupied_models),
                dict(desired_model_slots),
            )

        def restore_placement_state(
            state: tuple[
                list[PlacementAssignment],
                set[tuple[str, str]],
                dict[str, set[str]],
                dict[str, int],
                dict[str, int],
                dict[str, int],
            ],
        ) -> None:
            (
                saved_assignments,
                saved_pairs,
                saved_domains,
                saved_capacity,
                saved_occupied,
                saved_slots,
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

        def remove_regular_assignment(assignment: PlacementAssignment) -> None:
            assignment_node = node_by_id[assignment.node_id]
            residency = assignment_node.residency(assignment.model_id)
            assignments.remove(assignment)
            assigned_pairs.remove((assignment.model_id, assignment.node_id))
            capacity[assignment.node_id] += _incremental_memory_mb(
                residency,
                assignment.memory_mb,
            )
            desired_model_slots[assignment.node_id] -= 1
            if _adds_model_slot(residency):
                occupied_models[assignment.node_id] -= 1
            assigned_domains[assignment.model_id] = {
                node_by_id[item.node_id].failure_domain or item.node_id
                for item in assignments
                if item.model_id == assignment.model_id
            }

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
                # Each missing replica gets the same bounded search effort. A prior failed search
                # or unrelated ineligible inventory cannot change an otherwise identical result.
                search_state = _RepackSearchState()
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
                residency = candidate_node.residency(placement_model.model_id)
                for_new = residency is None or residency.state in (
                    ResidencyState.CACHED,
                    ResidencyState.FAILED,
                    ResidencyState.DRAINING,
                )
                if (
                    _ineligible_reason(
                        candidate_node,
                        placement_model,
                        timestamp,
                        self.policy,
                        for_new=for_new,
                    )
                    is not None
                ):
                    continue
                compatible_nodes.append(candidate_node)
                if not _fits(
                    candidate_node,
                    placement_model,
                    capacity,
                    occupied_models,
                    desired_model_slots,
                ):
                    continue
                candidate_domain = (
                    candidate_node.failure_domain or candidate_node.node_id
                )
                if len(domains | {candidate_domain}) < required_domain_floor:
                    continue
                score, reasons = _candidate_score(
                    candidate_node,
                    placement_model,
                    capacity[candidate_node.node_id],
                    domains,
                    self.policy,
                    now=timestamp,
                    need_new_domain=(
                        len(domains) < min(placement_model.min_failure_domains, target)
                    ),
                    startup_seconds=startup_by_pair,
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
                        if not _fits(
                            target_node,
                            placement_model,
                            capacity,
                            occupied_models,
                            desired_model_slots,
                        ):
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
                        score, reasons = _candidate_score(
                            target_node,
                            placement_model,
                            capacity[target_node.node_id],
                            placement_domains,
                            self.policy,
                            now=timestamp,
                            need_new_domain=(
                                len(placement_domains)
                                < min(placement_model.min_failure_domains, target)
                            ),
                            startup_seconds=startup_by_pair,
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

        def place_next_replica(model: ModelProfile) -> bool:
            target = desired_by_model[model.model_id]
            domains = assigned_domains.setdefault(model.model_id, set())
            candidates: list[tuple[float, str, NodeSnapshot, tuple[str, ...]]] = []
            compatible_new_domain_exists = False
            for node in node_list:
                if (model.model_id, node.node_id) in assigned_pairs:
                    continue
                residency = node.residency(model.model_id)
                for_new = residency is None or residency.state in (
                    ResidencyState.CACHED,
                    ResidencyState.FAILED,
                    ResidencyState.DRAINING,
                )
                reason = _ineligible_reason(
                    node, model, timestamp, self.policy, for_new=for_new
                )
                if reason is not None:
                    continue
                if (node.failure_domain or node.node_id) not in domains:
                    compatible_new_domain_exists = True
                if not _fits(
                    node,
                    model,
                    capacity,
                    occupied_models,
                    desired_model_slots,
                ):
                    continue
                score, reasons = _candidate_score(
                    node,
                    model,
                    capacity[node.node_id],
                    domains,
                    self.policy,
                    now=timestamp,
                    need_new_domain=(
                        len(domains) < min(model.min_failure_domains, target)
                    ),
                    startup_seconds=startup_by_pair,
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
            )
            return True

        priorities = sorted({model.priority for model in order}, reverse=True)
        for priority in priorities:
            priority_models = [model for model in order if model.priority == priority]
            blocked: set[str] = set()
            while True:
                placed_by_model = {
                    model.model_id: sum(
                        1 for item in assignments if item.model_id == model.model_id
                    )
                    for model in priority_models
                }
                unfinished = [
                    model
                    for model in priority_models
                    if model.model_id not in blocked
                    and placed_by_model[model.model_id] < placement_goal(model)
                ]
                if not unfinished:
                    break
                minimum_placed = min(
                    placed_by_model[model.model_id] for model in unfinished
                )
                current_level = [
                    model
                    for model in unfinished
                    if placed_by_model[model.model_id] == minimum_placed
                ]
                progress = False
                for model in current_level:
                    progress = place_next_replica(model) or progress
                if not progress:
                    # A model with no feasible next placement must not strand capacity usable by an
                    # equally important peer. Adding other replicas cannot create net resources;
                    # bounded repacking already exhausted every admissible rearrangement here.
                    blocked.update(model.model_id for model in current_level)

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
                    if _ineligible_reason(
                        node, model, timestamp, self.policy, for_new=True
                    )
                    is None
                ]
                code = "insufficient_capacity" if eligible else "no_eligible_nodes"
                unsatisfied.append(
                    UnsatisfiedConstraint(
                        model_id=model.model_id,
                        code=code,
                        message=f"Placed {placed} of {target} desired replicas",
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
        # Wall-clock time is not itself a desired-state input, but TTL and scale-down boundaries
        # derived from it are. Include the resulting targets and placements so the controller
        # advances its logical generation exactly when a time-sensitive decision changes. A late
        # command from the old state can then be fenced by the node runtime.
        input_digest = _input_digest(
            node_list,
            model_list,
            forecast_by_model,
            desired_by_model=desired_by_model,
            assignments=assignments,
        )
        objective = sum(item.score for item in assignments) - 1_000_000 * sum(
            item.missing_replicas for item in unsatisfied
        )
        return PlacementPlan(
            generation=new_generation(input_digest, timestamp),
            created_at=timestamp,
            assignments=tuple(assignments),
            desired_replicas=tuple(sorted(desired_by_model.items())),
            unsatisfied=tuple(unsatisfied),
            objective_score=objective,
            input_digest=input_digest,
        )


def desired_replica_count(
    model: ModelProfile,
    forecast: DemandForecast | None,
    *,
    nodes: Iterable[NodeSnapshot] = (),
    now: float | None = None,
    policy: PlannerPolicy | None = None,
) -> int:
    policy = policy or PlannerPolicy()
    timestamp = time.time() if now is None else float(now)
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
        )
        concurrency = offered_concurrency * (1.0 + policy.demand_headroom_fraction)
        required_capacity = _bounded_replica_ceil(
            concurrency / model.target_utilization,
            MAX_COUNTER,
        )
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
            nodes,
            timestamp,
            policy,
        )
        # A queue or degraded service level is direct evidence that the current ready set is not
        # meeting demand, even if one engine advertises a wide theoretical batch. Add one replica
        # instead of allowing a generous max_concurrency declaration to mask observed pressure.
        if ready_replicas and (
            forecast.queue_depth
            or (model.latency_slo_ms and forecast.p95_latency_ms > model.latency_slo_ms)
            or forecast.error_rate
        ):
            target = max(target, min(model.max_replicas, ready_replicas + 1))
        target = max(model.min_replicas, target)

    # Recently-used ready replicas are retained even after a traffic dip.  This is the global
    # hysteresis that makes placement migration-frugal; the reconciler independently gates mutation.
    if policy.preserve_recent_residencies:
        recent = 0
        for node in nodes:
            residency = node.residency(model.model_id)
            if residency is None or residency.state != ResidencyState.READY:
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
) -> float:
    """Project a rising workload across the time needed to make a new replica ready.

    DemandTracker already includes a short one-bucket trend in its base forecast. This additional
    horizon is model-specific: a model that takes two minutes to load must start earlier than one
    that warms in two seconds. Confidence and a hard growth ratio bound noisy or imported trends.
    Negative trends never accelerate scale-down; residency cooldown remains its sole authority.
    """

    startup_horizon = min(
        policy.max_predictive_lookahead_seconds,
        model.load_seconds + model.warm_seconds,
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
        forecast.trend_per_minute
        * (startup_horizon / 60.0)
        * forecast.confidence
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
        for_new = residency is None or residency.state in (
            ResidencyState.CACHED,
            ResidencyState.FAILED,
            ResidencyState.DRAINING,
        )
        if (
            _ineligible_reason(node, model, timestamp, policy, for_new=for_new)
            is not None
        ):
            continue
        is_ready = bool(residency and residency.state == ResidencyState.READY)
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
    if not set(model.required_tags).issubset(node.tags):
        return "required node tags are missing"
    if set(model.forbidden_tags).intersection(node.tags):
        return "node has a forbidden tag"
    if for_new and (node.manually_managed or "warm" not in node.actuator_capabilities):
        return "node cannot be actuated"
    artifact_cached = model.model_id in node.cached_models or bool(
        residency
        and residency.managed
        and residency.state in (ResidencyState.CACHED, ResidencyState.DRAINING)
    )
    if for_new and not artifact_cached and "load" not in node.actuator_capabilities:
        return "node cannot load uncached model weights"
    return None


def _fits(
    node: NodeSnapshot,
    model: ModelProfile,
    capacity: dict[str, int],
    occupied_models: dict[str, int],
    desired_model_slots: dict[str, int],
) -> bool:
    residency = node.residency(model.model_id)
    incremental_mb = _incremental_memory_mb(residency, model.memory_for(node.runtimes))
    if capacity[node.node_id] < incremental_mb:
        return False
    adds_slot = _adds_model_slot(residency)
    if node.max_models is None:
        return True
    if desired_model_slots[node.node_id] >= node.max_models:
        return False
    return not adds_slot or occupied_models[node.node_id] < node.max_models


def _candidate_score(
    node: NodeSnapshot,
    model: ModelProfile,
    remaining_mb: int,
    domains: set[str],
    policy: PlannerPolicy,
    *,
    now: float,
    need_new_domain: bool,
    startup_seconds: Mapping[tuple[str, str], float],
) -> tuple[float, tuple[str, ...]]:
    score = 0.0
    reasons: list[str] = []
    residency = node.residency(model.model_id)
    startup_key = (node.node_id, model.model_id)
    warm_seconds = startup_seconds.get(startup_key, model.warm_seconds)
    if residency and residency.state == ResidencyState.READY:
        score += 100_000.0
        reasons.append("already resident and ready")
    elif residency and residency.state in (
        ResidencyState.LOADING,
        ResidencyState.WARMING,
    ):
        score += 75_000.0
        reasons.append("already loading")
    elif model.model_id in node.cached_models or (
        residency and residency.state == ResidencyState.CACHED
    ):
        score += 20_000.0 - min(warm_seconds, 1_000_000_000_000.0) * 20.0
        reasons.append("weights cached locally")
    else:
        cold_seconds = model.load_seconds + warm_seconds
        score -= min(cold_seconds, 1_000_000_000_000.0) * 20.0
        reasons.append("cold load required")
    if startup_key in startup_seconds and not (
        residency
        and residency.state
        in (ResidencyState.READY, ResidencyState.LOADING, ResidencyState.WARMING)
    ):
        reasons.append("learned warm-start estimate")
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
                max(0.0, 1.0 - max(0.0, performance_age) / policy.performance_ttl_seconds)
                if policy.performance_ttl_seconds
                else 1.0
            )
            throughput_age = now - model_performance.throughput_updated_at
            throughput_freshness = (
                max(
                    0.0,
                    1.0
                    - max(0.0, throughput_age) / policy.performance_ttl_seconds,
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
    node_metric_is_attributable = ready_models <= 1
    if model_performance is not None:
        measured_tokens_per_second = (
            model_performance.tokens_per_second
            if model_throughput_weight > 0
            else 0.0
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
        score += min(measured_tokens_per_second, 10_000.0) * 2.0 * throughput_weight
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
        score += min(node.memory_bandwidth_gbps, 2_000.0) * 10.0 * hardware_weight
        score += (
            min(math.log2(1.0 + node.compute_gflops) * 100.0, 2_000.0)
            * hardware_weight
        )
        reasons.append("hardware performance estimate")
    if measured_latency_ms:
        latency_weight = model_latency_weight if model_performance is not None else 1.0
        score -= min(measured_latency_ms, 60_000.0) / 50.0 * latency_weight
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
    score -= min(node.cost_per_hour, 1_000_000_000_000.0) * 1_000.0
    score -= min(max(0, node.host_priority), 1_000_000_000_000) * 100.0
    return score, tuple(reasons)


def _assignment(
    model: ModelProfile,
    node: NodeSnapshot,
    *,
    index: int,
    score: float,
    reasons: tuple[str, ...],
) -> PlacementAssignment:
    residency = node.residency(model.model_id)
    return PlacementAssignment(
        model_id=model.model_id,
        node_id=node.node_id,
        memory_mb=model.memory_for(node.runtimes),
        replica_index=index,
        score=score,
        existing=bool(residency and residency.state == ResidencyState.READY),
        reasons=reasons,
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
) -> None:
    assignments.append(assignment)
    assigned_pairs.add((assignment.model_id, assignment.node_id))
    domains.add(node.failure_domain or node.node_id)
    residency = node.residency(assignment.model_id)
    capacity[node.node_id] -= _incremental_memory_mb(residency, assignment.memory_mb)
    desired_model_slots[node.node_id] += 1
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
    result: dict[tuple[str, str], float] = {}
    for key, raw_duration in (values or {}).items():
        if (
            not isinstance(key, tuple)
            or len(key) != 2
            or not all(isinstance(item, str) and item for item in key)
        ):
            raise ValueError("startup estimate keys must contain node and model IDs")
        if isinstance(raw_duration, bool):
            raise ValueError("startup estimates must be finite and non-negative")
        duration = float(raw_duration)
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("startup estimates must be finite and non-negative")
        result[key] = duration
    return result


def _input_digest(
    nodes: list[NodeSnapshot],
    models: list[ModelProfile],
    forecasts: dict[str, DemandForecast],
    *,
    desired_by_model: dict[str, int],
    assignments: list[PlacementAssignment],
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
        }
    )
