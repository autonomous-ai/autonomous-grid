"""Bounded clairvoyant placement benchmark for the allocator scenario lab.

The production allocator cannot know the future.  This module deliberately can: it searches every
compatible placement on a small logical fleet and scores the exact request trace after the run.  It
is a development benchmark, not a production policy.  A gap tells us that better forecasting and
placement could help; a small gap tells us to stop tuning placement and investigate capacity,
catalog, or routing instead.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import product
from typing import Mapping

from shared.allocator.models import ModelProfile, NodeSnapshot, NodeState
from shared.allocator.planner import PlannerPolicy


MAX_ORACLE_MACHINES = 4
MAX_ORACLE_MODELS = 9
MAX_ORACLE_MINUTES = 240
_EPSILON = 1e-9
_MAX_ORACLE_TIE_STATES = 256


@dataclass(frozen=True, slots=True)
class OracleMinuteDemand:
    requests: tuple[tuple[str, int], ...]
    service_seconds: tuple[tuple[str, float], ...]


@dataclass(slots=True)
class _Edge:
    to: int
    reverse: int
    capacity: float
    cost: float
    initial_capacity: float


def run_small_fleet_oracle(
    *,
    nodes_by_minute: tuple[tuple[NodeSnapshot, ...], ...],
    profiles: tuple[ModelProfile, ...],
    demands: tuple[OracleMinuteDemand, ...],
    direct_models: Mapping[str, str],
    artifact_sizes: Mapping[str, int],
    actual_served: float,
    policy: PlannerPolicy,
) -> dict[str, object]:
    """Return an exact placement ceiling for a bounded, already-observed demand trace.

    Placements are held for a conservative two-minute block.  The benchmark knows every future
    request and assumes any individually admissible artifact can be prefetched.  Artifact use is
    audited after the schedule so the report distinguishes a placement-only opportunity from one
    that also needs a better cache plan.
    """

    if not nodes_by_minute or len(nodes_by_minute) != len(demands):
        raise ValueError("oracle requires one non-empty node snapshot per demand minute")
    machine_count = len(nodes_by_minute[0])
    if not 1 <= machine_count <= MAX_ORACLE_MACHINES:
        raise ValueError(
            f"oracle supports 1-{MAX_ORACLE_MACHINES} machines; got {machine_count}"
        )
    if not 1 <= len(profiles) <= MAX_ORACLE_MODELS:
        raise ValueError(
            f"oracle supports 1-{MAX_ORACLE_MODELS} models; got {len(profiles)}"
        )
    if len(demands) > MAX_ORACLE_MINUTES:
        raise ValueError(
            f"oracle supports at most {MAX_ORACLE_MINUTES} minutes; got {len(demands)}"
        )
    if any(len(nodes) != machine_count for nodes in nodes_by_minute):
        raise ValueError("oracle node inventory changed shape during the trace")

    profile_by_id = {profile.model_id: profile for profile in profiles}
    # A new model is unavailable during the tick that requested its load. Holding the resulting
    # placement for two scored minutes also respects the scenario profiles' 60-second minimum
    # residency without pretending an adjacent-tick reversal is executable.
    hold_minutes = max(
        2,
        1
        + math.ceil(
            max((profile.min_residency_seconds for profile in profiles), default=0.0)
            / 60.0
        ),
    )
    blocks = tuple(
        tuple(range(start, min(len(demands), start + hold_minutes)))
        for start in range(0, len(demands), hold_minutes)
    )

    candidate_blocks: list[tuple[tuple[str, ...], ...]] = []
    states_evaluated = 0
    tie_states_pruned = False
    for minute_indexes in blocks:
        states = _feasible_states(
            tuple(nodes_by_minute[index] for index in minute_indexes),
            profiles,
            policy,
        )
        if not states:
            raise ValueError("oracle found no feasible placement for a time block")
        scores: dict[tuple[str, ...], float] = {}
        best = -math.inf
        for state in states:
            score = sum(
                _service_for_state(
                    state,
                    nodes_by_minute[index],
                    profile_by_id,
                    demands[index],
                    direct_models,
                    policy,
                )[0]
                for index in minute_indexes
            )
            scores[state] = score
            best = max(best, score)
        winners = tuple(
            state for state, score in scores.items() if abs(score - best) <= _EPSILON
        )
        if len(winners) > _MAX_ORACLE_TIE_STATES:
            # The service ceiling is already exact at this point. Bound only the secondary
            # minimum-churn path search, which can otherwise become quadratic across thousands of
            # equally scoring placements during a zero-demand block.
            tie_states_pruned = True
            winners = tuple(
                sorted(
                    winners,
                    key=lambda state: (
                        sum(bool(model_id) for model_id in state),
                        state,
                    ),
                )[:_MAX_ORACLE_TIE_STATES]
            )
        candidate_blocks.append(winners)
        states_evaluated += len(states)

    # Service is the primary objective. Among all service-maximizing schedules, select the one
    # with the fewest model mutations so duplicated hosts do not create an arbitrary churny path.
    costs: dict[tuple[str, ...], tuple[int, tuple[tuple[str, ...], ...]]] = {
        state: (
            sum(bool(model_id) for model_id in state),
            (state,),
        )
        for state in candidate_blocks[0]
    }
    for winners in candidate_blocks[1:]:
        next_costs: dict[tuple[str, ...], tuple[int, tuple[tuple[str, ...], ...]]] = {}
        for state in winners:
            best_path = min(
                (
                    prior_cost + _mutation_count(prior, state),
                    path + (state,),
                )
                for prior, (prior_cost, path) in costs.items()
            )
            next_costs[state] = best_path
        costs = next_costs
    mutation_count, block_schedule = min(costs.values())

    served_by_workload: dict[str, float] = {}
    total_served = 0.0
    minute_schedule: list[tuple[str, ...]] = []
    for state, minute_indexes in zip(block_schedule, blocks, strict=True):
        for index in minute_indexes:
            served, workload_served = _service_for_state(
                state,
                nodes_by_minute[index],
                profile_by_id,
                demands[index],
                direct_models,
                policy,
            )
            total_served += served
            minute_schedule.append(state)
            for workload, value in workload_served.items():
                served_by_workload[workload] = served_by_workload.get(workload, 0.0) + value

    total_requests = sum(
        count for demand in demands for _, count in demand.requests
    )
    schedule_rows = _schedule_rows(tuple(minute_schedule), nodes_by_minute[0])
    artifact_feasible, artifact_overage = _artifact_audit(
        tuple(minute_schedule),
        nodes_by_minute[0],
        artifact_sizes,
    )
    startup_feasible = all(
        profile.load_seconds + profile.warm_seconds <= 60.0 for profile in profiles
    )
    gain = max(0.0, total_served - actual_served)
    return {
        "kind": "clairvoyant exhaustive placement benchmark",
        "hold_minutes": hold_minutes,
        "service_ceiling_equivalent": round(total_served, 2),
        "service_ceiling_pct": round(100.0 * total_served / max(1, total_requests), 2),
        "potential_gain_requests": round(gain, 2),
        "potential_gain_pct_points": round(100.0 * gain / max(1, total_requests), 2),
        "mutations": mutation_count,
        "mutation_search_exact": not tie_states_pruned,
        "states_evaluated": states_evaluated,
        "artifact_feasible": artifact_feasible,
        "artifact_overage_mb": artifact_overage,
        "one_minute_startup_feasible": startup_feasible,
        "per_workload_served_equivalent": {
            workload: round(value, 2)
            for workload, value in sorted(served_by_workload.items())
        },
        "schedule": schedule_rows,
        "interpretation": (
            "hindsight placement opportunity is demonstrated"
            if artifact_feasible and startup_feasible and gain > _EPSILON
            else (
                "the optimistic gap also requires artifact/cache preparation"
                if gain > _EPSILON
                else "no placement opportunity was found under this benchmark"
            )
        ),
        "assumptions": (
            "perfect future demand knowledge",
            "two-minute-or-longer placement holds",
            "one-minute load-to-ready actuation",
            "optimal routing across resident capable models",
            "runtime, backend, memory, node state, replica bounds, and model slots enforced",
            "artifact downloads audited cumulatively after schedule selection",
            "scale-down cooldown is treated as a tunable policy, not a hard schedule fence",
            "secondary mutation tie search is capped at 256 equal-service placements per block",
        ),
    }


def _feasible_states(
    block_nodes: tuple[tuple[NodeSnapshot, ...], ...],
    profiles: tuple[ModelProfile, ...],
    policy: PlannerPolicy,
) -> tuple[tuple[str, ...], ...]:
    options: list[tuple[str, ...]] = []
    for node_index in range(len(block_nodes[0])):
        snapshots = tuple(nodes[node_index] for nodes in block_nodes)
        model_options = [""]
        for profile in profiles:
            if all(_fits(node, profile, policy) for node in snapshots):
                model_options.append(profile.model_id)
        options.append(tuple(model_options))

    states: list[tuple[str, ...]] = []
    for state in product(*options):
        counts = {profile.model_id: state.count(profile.model_id) for profile in profiles}
        if any(
            count < profile.min_replicas or count > profile.max_replicas
            for profile in profiles
            if (count := counts[profile.model_id]) or profile.min_replicas
        ):
            continue
        states.append(tuple(state))
    return tuple(states)


def _fits(node: NodeSnapshot, profile: ModelProfile, policy: PlannerPolicy) -> bool:
    if node.state not in (NodeState.ACCEPTING, NodeState.THROTTLED):
        return False
    if not set(profile.runtimes).intersection(node.runtimes):
        return False
    if not set(profile.backends).intersection(node.backends):
        return False
    if node.allowed_models and profile.model_id not in node.allowed_models:
        return False
    if profile.model_id in node.denied_models:
        return False
    state_fraction = (
        policy.throttled_capacity_fraction if node.state == NodeState.THROTTLED else 1.0
    )
    budget = math.floor(
        node.usable_capacity_mb * state_fraction * (1.0 - policy.memory_headroom_fraction)
    )
    return profile.memory_for(node.runtimes) <= budget


def _service_for_state(
    state: tuple[str, ...],
    nodes: tuple[NodeSnapshot, ...],
    profiles: Mapping[str, ModelProfile],
    demand: OracleMinuteDemand,
    direct_models: Mapping[str, str],
    policy: PlannerPolicy,
) -> tuple[float, dict[str, float]]:
    capacity: dict[str, float] = {}
    for model_id, node in zip(state, nodes, strict=True):
        if not model_id or node.state not in (NodeState.ACCEPTING, NodeState.THROTTLED):
            continue
        profile = profiles[model_id]
        fraction = (
            policy.throttled_capacity_fraction
            if node.state == NodeState.THROTTLED
            else 1.0
        )
        capacity[model_id] = capacity.get(model_id, 0.0) + (
            profile.replica_concurrency * profile.target_utilization * fraction
        )
    return _optimal_routing(capacity, profiles, demand, direct_models)


def _optimal_routing(
    capacity: Mapping[str, float],
    profiles: Mapping[str, ModelProfile],
    demand: OracleMinuteDemand,
    direct_models: Mapping[str, str],
) -> tuple[float, dict[str, float]]:
    requests = {workload: count for workload, count in demand.requests if count > 0}
    service = {
        workload: seconds
        for workload, seconds in demand.service_seconds
        if seconds > _EPSILON and requests.get(workload, 0) > 0
    }
    model_ids = tuple(sorted(model_id for model_id, value in capacity.items() if value > 0))
    workloads = tuple(sorted(service))
    if not model_ids or not workloads:
        return 0.0, {}

    source = 0
    model_offset = 1
    workload_offset = model_offset + len(model_ids)
    sink = workload_offset + len(workloads)
    graph: list[list[_Edge]] = [[] for _ in range(sink + 1)]
    for model_index, model_id in enumerate(model_ids):
        _add_edge(graph, source, model_offset + model_index, capacity[model_id], 0.0)
    sink_edges: dict[str, _Edge] = {}
    for workload_index, workload in enumerate(workloads):
        concurrency = service[workload] / 60.0
        node = workload_offset + workload_index
        sink_edges[workload] = _add_edge(graph, node, sink, concurrency, 0.0)
        named_model = direct_models.get(workload, "")
        value_per_concurrency = requests[workload] / concurrency
        for model_index, model_id in enumerate(model_ids):
            if named_model:
                eligible = model_id == named_model
            else:
                eligible = profiles[model_id].workload_score(workload) > 0
            if eligible:
                _add_edge(
                    graph,
                    model_offset + model_index,
                    node,
                    concurrency,
                    -value_per_concurrency,
                )

    _min_cost_max_flow(graph, source, sink)
    served_by_workload = {
        workload: min(
            float(requests[workload]),
            (edge.initial_capacity - edge.capacity)
            * requests[workload]
            / edge.initial_capacity,
        )
        for workload, edge in sink_edges.items()
        if edge.initial_capacity - edge.capacity > _EPSILON
    }
    return sum(served_by_workload.values()), served_by_workload


def _add_edge(
    graph: list[list[_Edge]],
    source: int,
    target: int,
    capacity: float,
    cost: float,
) -> _Edge:
    forward = _Edge(target, len(graph[target]), capacity, cost, capacity)
    reverse = _Edge(source, len(graph[source]), 0.0, -cost, 0.0)
    graph[source].append(forward)
    graph[target].append(reverse)
    return forward


def _min_cost_max_flow(graph: list[list[_Edge]], source: int, sink: int) -> None:
    node_count = len(graph)
    while True:
        distance = [math.inf] * node_count
        previous: list[tuple[int, int] | None] = [None] * node_count
        distance[source] = 0.0
        for _ in range(node_count - 1):
            changed = False
            for node, edges in enumerate(graph):
                if not math.isfinite(distance[node]):
                    continue
                for edge_index, edge in enumerate(edges):
                    candidate = distance[node] + edge.cost
                    if edge.capacity > _EPSILON and candidate < distance[edge.to] - _EPSILON:
                        distance[edge.to] = candidate
                        previous[edge.to] = (node, edge_index)
                        changed = True
            if not changed:
                break
        if previous[sink] is None:
            return
        amount = math.inf
        node = sink
        while node != source:
            prior, edge_index = previous[node] or (-1, -1)
            if prior < 0:
                return
            amount = min(amount, graph[prior][edge_index].capacity)
            node = prior
        node = sink
        while node != source:
            prior, edge_index = previous[node] or (-1, -1)
            edge = graph[prior][edge_index]
            edge.capacity -= amount
            graph[node][edge.reverse].capacity += amount
            node = prior


def _mutation_count(before: tuple[str, ...], after: tuple[str, ...]) -> int:
    return sum(
        int(bool(old)) + int(bool(new))
        for old, new in zip(before, after, strict=True)
        if old != new
    )


def _schedule_rows(
    schedule: tuple[tuple[str, ...], ...],
    initial_nodes: tuple[NodeSnapshot, ...],
) -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = []
    prior = ("",) * len(initial_nodes)
    for minute, state in enumerate(schedule):
        if minute and state == prior:
            continue
        loads = tuple(
            f"{new}@{initial_nodes[index].node_id}"
            for index, (old, new) in enumerate(zip(prior, state, strict=True))
            if old != new and new
        )
        unloads = tuple(
            f"{old}@{initial_nodes[index].node_id}"
            for index, (old, new) in enumerate(zip(prior, state, strict=True))
            if old != new and old
        )
        rows.append(
            {
                "minute": minute,
                "placements": {
                    initial_nodes[index].node_id: model_id
                    for index, model_id in enumerate(state)
                    if model_id
                },
                "loads": loads,
                "unloads": unloads,
            }
        )
        prior = state
    return tuple(rows)


def _artifact_audit(
    schedule: tuple[tuple[str, ...], ...],
    initial_nodes: tuple[NodeSnapshot, ...],
    artifact_sizes: Mapping[str, int],
) -> tuple[bool, dict[str, int]]:
    overage: dict[str, int] = {}
    for index, node in enumerate(initial_nodes):
        downloads = {
            state[index]
            for state in schedule
            if state[index] and state[index] not in node.cached_models
        }
        required = sum(artifact_sizes[model_id] for model_id in downloads)
        available = node.disk_available_mb or 0
        if required > available:
            overage[node.node_id] = required - available
    return not overage, dict(sorted(overage.items()))
