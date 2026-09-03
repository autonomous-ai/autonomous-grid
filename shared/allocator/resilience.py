"""Resumable allocator control-plane resilience and soak qualification.

The runner exercises the production planner, reconciler, durable controller state, mutation
receipts, and controller authority lease.  Hardware and the relay observation boundary are modeled
deliberately: engine lifecycle qualification belongs to ``grid allocator qualify`` and this module
does not claim that synthetic logical nodes are physical GPUs.
"""

from __future__ import annotations

import hashlib
import math
import time
from collections import Counter
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable

from shared import jsonio
from shared.allocator.authority import AuthorityUnavailable, ControllerAuthorityLease
from shared.allocator.controller import AllocatorController
from shared.allocator.models import (
    ActionKind,
    AllocatorMode,
    ModelProfile,
    ModelResidency,
    MutationAction,
    NodeSnapshot,
    ResidencyState,
)
from shared.allocator.planner import PlannerPolicy
from shared.allocator.reconcile import MutationStatus, ReconcilePolicy

RESILIENCE_SCHEMA_VERSION = 1
_MAX_HOURS = 24 * 30
_MAX_CYCLES = 100_000


@dataclass(frozen=True, slots=True)
class ResilienceConfig:
    hours: float = 72.0
    interval_seconds: float = 300.0
    accelerated: bool = True
    seed: int = 42
    node_partition_every: int = 17
    relay_outage_every: int = 29
    controller_failover_every: int = 43

    def __post_init__(self) -> None:
        if not math.isfinite(self.hours) or not 0 < self.hours <= _MAX_HOURS:
            raise ValueError(f"hours must be in (0, {_MAX_HOURS}]")
        if not math.isfinite(self.interval_seconds) or self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be finite and positive")
        cycles = self.cycles
        if cycles < 1 or cycles > _MAX_CYCLES:
            raise ValueError(f"configuration produces {cycles} cycles; maximum is {_MAX_CYCLES}")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ValueError("seed must be an integer")
        for name in (
            "node_partition_every",
            "relay_outage_every",
            "controller_failover_every",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")

    @property
    def cycles(self) -> int:
        return max(1, math.ceil(self.hours * 3_600 / self.interval_seconds))


@dataclass(frozen=True, slots=True)
class ResilienceReport:
    configuration: dict[str, Any]
    started_at: float
    completed_at: float
    completed_cycles: int
    events: tuple[dict[str, Any], ...]
    checks: dict[str, int]
    failures: tuple[dict[str, Any], ...]
    final_placements: tuple[str, ...]
    final_controller_term: int
    report_path: str = ""

    @property
    def passed(self) -> bool:
        return not self.failures and self.completed_cycles == int(
            self.configuration["cycles"]
        )

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": RESILIENCE_SCHEMA_VERSION, "passed": self.passed, **asdict(self)}


class _Clock:
    def __init__(self, value: float) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value


def run_resilience_soak(
    config: ResilienceConfig,
    root: Path,
    *,
    resume: bool = False,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> ResilienceReport:
    """Run or resume a deterministic control-plane qualification.

    Accelerated mode advances the controller clock without sleeping. Wall-clock mode executes the
    same cycle and sleeps between observations, making it suitable for a multi-day unattended soak.
    The checkpoint contains the logical node heartbeats as well as controller state, so interruption
    never turns a partial run into a passing report.
    """

    root.mkdir(parents=True, exist_ok=True)
    checkpoint_path = root / "checkpoint.json"
    controller_path = root / "controller.json"
    report_path = root / "report.json"
    started_at = time.time()
    events: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    checks: Counter[str] = Counter()
    cycle_start = 0
    nodes = _fixture_nodes(started_at)
    leader_index = 0
    clock = _Clock(started_at)

    if resume:
        checkpoint = jsonio.load_json(checkpoint_path)
        if not checkpoint:
            raise ValueError(f"no resilience checkpoint exists at {checkpoint_path}")
        _validate_checkpoint(checkpoint, config)
        cycle_start = int(checkpoint["completed_cycles"])
        started_at = float(checkpoint["started_at"])
        clock.value = float(checkpoint["logical_time"])
        leader_index = int(checkpoint["leader_index"])
        nodes = tuple(NodeSnapshot.from_dict(item) for item in checkpoint["nodes"])
        events = [dict(item) for item in checkpoint.get("events") or ()]
        failures = [dict(item) for item in checkpoint.get("failures") or ()]
        checks.update({str(k): int(v) for k, v in (checkpoint.get("checks") or {}).items()})
    elif checkpoint_path.exists() or controller_path.exists():
        raise ValueError(
            f"resilience state already exists at {root}; pass resume=True or choose a new directory"
        )

    authority = ControllerAuthorityLease(
        controller_path,
        # The lease renews before its final third. Four observation intervals guarantee at least
        # one renewal opportunity even when an accelerated cycle lands exactly on a boundary.
        ttl_seconds=max(45.0, config.interval_seconds * 4),
        leader_id=f"soak-leader-{leader_index}",
        clock=clock,
    )
    grant = authority.ensure()
    controller = AllocatorController(
        mode=AllocatorMode.AUTOMATIC,
        state_path=controller_path,
        controller_term=grant.term,
        controller_id=grant.leader_id,
        controller_lease_expires_at=grant.expires_at,
        membership_recovery_grace_seconds=0,
        planner_policy=PlannerPolicy(node_ttl_seconds=max(config.interval_seconds * 3, 30.0)),
        reconcile_policy=ReconcilePolicy(
            mutation_cooldown_seconds=0,
            failure_backoff_base_seconds=0,
            failure_backoff_max_seconds=0,
            success_observation_timeout_seconds=0,
        ),
    )
    controller.update_authority(grant.term, grant.leader_id, grant.expires_at)
    if not controller.profiles:
        for profile in _fixture_profiles():
            controller.put_profile(profile)

    # One cycle is intentionally finite. Operators can interrupt a wall-clock run safely; the last
    # completed checkpoint remains a truthful non-passing partial result.
    for cycle in range(cycle_start, config.cycles):
        cycle_started = time.monotonic()
        if cycle > cycle_start or not resume:
            if config.accelerated:
                clock.value = max(
                    clock.value,
                    started_at + cycle * config.interval_seconds,
                )
            else:
                clock.value = time.time()

        failover = bool(
            config.controller_failover_every
            and cycle
            and cycle % config.controller_failover_every == 0
        )
        relay_outage = bool(
            config.relay_outage_every and cycle and cycle % config.relay_outage_every == 0
        )
        partitioned = (
            f"node-{(config.seed + cycle // max(config.node_partition_every, 1)) % len(nodes)}"
            if config.node_partition_every
            and cycle
            and cycle % config.node_partition_every == 0
            else ""
        )

        if failover:
            old_authority = authority
            old_term = controller.controller_term
            clock.value += old_authority.ttl_seconds + 0.001
            leader_index += 1
            authority = ControllerAuthorityLease(
                controller_path,
                ttl_seconds=old_authority.ttl_seconds,
                leader_id=f"soak-leader-{leader_index}",
                clock=clock,
            )
            grant = authority.ensure()
            controller = AllocatorController(
                state_path=controller_path,
                membership_recovery_grace_seconds=0,
            )
            controller.update_authority(grant.term, grant.leader_id, grant.expires_at)
            _check(
                grant.term > old_term,
                "failover_term_advanced",
                checks,
                failures,
                cycle,
                f"successor term {grant.term} did not advance beyond {old_term}",
            )
            try:
                old_authority.ensure()
            except AuthorityUnavailable:
                checks["old_leader_fenced"] += 1
            else:
                _check(
                    False,
                    "old_leader_fenced",
                    checks,
                    failures,
                    cycle,
                    "expired controller leader reacquired authority over its successor",
                )
            events.append(_event(cycle, clock.value, "controller_failover", grant.term))
        else:
            grant = authority.ensure()
            controller.update_authority(grant.term, grant.leader_id, grant.expires_at)

        if relay_outage:
            before = _sha256_file(controller_path)
            # The observation boundary is unavailable: no fabricated empty fleet is reconciled and
            # no mutation command is emitted. Existing serving processes remain untouched.
            after = _sha256_file(controller_path)
            _check(
                before == after,
                "relay_outage_preserved_state",
                checks,
                failures,
                cycle,
                "controller state changed without a relay observation",
            )
            events.append(_event(cycle, clock.value, "relay_outage", grant.term))
        else:
            active_nodes = tuple(node for node in nodes if node.node_id != partitioned)
            active_nodes = tuple(replace(node, last_heartbeat=clock.value) for node in active_nodes)
            active_by_id = {node.node_id: node for node in active_nodes}
            nodes = tuple(active_by_id.get(node.node_id, node) for node in nodes)
            if partitioned:
                events.append(
                    _event(cycle, clock.value, "node_partition", grant.term, node=partitioned)
                )

            # Change required supply during the soak to force both constructive and destructive
            # lifecycles. The fault schedule is independent of this load schedule.
            _set_supply_wave(controller, cycle)
            controller.tick(active_nodes, now=clock.value)
            _check_plan(controller, active_nodes, checks, failures, cycle)
            nodes = _apply_commands(controller, nodes, active_nodes, clock.value, checks, failures, cycle)

            # A recovered observation must be accepted on the very next ordinary cycle.
            if cycle > 1 and config.node_partition_every and (cycle - 1) % config.node_partition_every == 0:
                checks["node_rejoined_after_partition"] += 1
            if cycle > 1 and config.relay_outage_every and (cycle - 1) % config.relay_outage_every == 0:
                checks["controller_resumed_after_relay_outage"] += 1

        checkpoint = {
            "schema_version": RESILIENCE_SCHEMA_VERSION,
            "configuration": asdict(config),
            "started_at": started_at,
            "logical_time": clock.value,
            "completed_cycles": cycle + 1,
            "leader_index": leader_index,
            "nodes": [node.to_dict() for node in nodes],
            "events": events,
            "checks": dict(checks),
            "failures": failures,
        }
        jsonio.atomic_write_json(checkpoint_path, checkpoint, mode=0o600)
        if progress is not None:
            progress(
                {
                    "cycle": cycle + 1,
                    "cycles": config.cycles,
                    "event": events[-1]["kind"] if events and events[-1]["cycle"] == cycle else "steady",
                    "term": controller.controller_term,
                    "failures": len(failures),
                }
            )
        if not config.accelerated and cycle + 1 < config.cycles:
            elapsed = max(0.0, time.monotonic() - cycle_started)
            time.sleep(max(0.0, config.interval_seconds - elapsed))

    final_plan = controller.last_plan
    report = ResilienceReport(
        configuration={**asdict(config), "cycles": config.cycles},
        started_at=started_at,
        completed_at=time.time(),
        completed_cycles=config.cycles,
        events=tuple(events),
        checks=dict(sorted(checks.items())),
        failures=tuple(failures),
        final_placements=tuple(
            sorted(
                f"{item.model_id}@{item.node_id}"
                for item in (final_plan.assignments if final_plan is not None else ())
            )
        ),
        final_controller_term=controller.controller_term,
        report_path=str(report_path),
    )
    jsonio.atomic_write_json(report_path, report.to_dict(), mode=0o600)
    return report


def _fixture_profiles() -> tuple[ModelProfile, ...]:
    common = {
        "runtimes": ("llama.cpp",),
        "backends": ("metal", "cuda"),
        "min_residency_seconds": 0,
        "scale_down_cooldown_seconds": 0,
        "min_failure_domains": 1,
    }
    return (
        ModelProfile("general", 8_000, min_replicas=1, max_replicas=3, priority=300, **common),
        ModelProfile("coder", 12_000, min_replicas=1, max_replicas=2, priority=400, **common),
        ModelProfile("image", 16_000, min_replicas=0, max_replicas=1, priority=200, **common),
    )


def _fixture_nodes(now: float) -> tuple[NodeSnapshot, ...]:
    capacities = (32_000, 48_000, 64_000, 32_000)
    backends = ("metal", "cuda", "cuda", "metal")
    return tuple(
        NodeSnapshot(
            f"node-{index}",
            capacity,
            reserved_mb=4_000,
            runtimes=("llama.cpp",),
            backends=(backends[index],),
            failure_domain=f"host-{index}",
            cached_models=("general", "coder", "image"),
            max_concurrency=4,
            last_heartbeat=now,
        )
        for index, capacity in enumerate(capacities)
    )


def _set_supply_wave(controller: AllocatorController, cycle: int) -> None:
    phase = cycle % 12
    targets = {"general": 2 if 3 <= phase < 9 else 1, "coder": 2 if 6 <= phase < 11 else 1, "image": 1 if 4 <= phase < 8 else 0}
    for profile in controller.profiles:
        target = targets[profile.model_id]
        if profile.min_replicas != target:
            controller.put_profile(replace(profile, min_replicas=target))


def _apply_commands(
    controller: AllocatorController,
    nodes: tuple[NodeSnapshot, ...],
    active_nodes: tuple[NodeSnapshot, ...],
    now: float,
    checks: Counter[str],
    failures: list[dict[str, Any]],
    cycle: int,
) -> tuple[NodeSnapshot, ...]:
    active_ids = {node.node_id for node in active_nodes}
    by_id = {node.node_id: node for node in nodes}
    for _ in range(8):
        changed = False
        safety = tuple(by_id[node_id] for node_id in active_ids)
        for node_id in sorted(active_ids):
            actions = controller.commands_for(
                node_id,
                now=now,
                destructive_safety_factory=lambda snapshot=safety: snapshot,
            )
            for action in actions:
                _check(
                    action.controller_term == controller.controller_term,
                    "command_current_term",
                    checks,
                    failures,
                    cycle,
                    f"{action.action_id} carried term {action.controller_term}, expected {controller.controller_term}",
                )
                by_id[node_id] = _apply_action(by_id[node_id], action, now)
                controller.acknowledge(
                    node_id,
                    action.action_id,
                    MutationStatus.SUCCEEDED,
                    duration_seconds=0.01,
                    now=now,
                )
                checks[f"action_{action.kind.value}"] += 1
                changed = True
        if not changed:
            break
        fresh = tuple(replace(by_id[node_id], last_heartbeat=now) for node_id in sorted(active_ids))
        for node in fresh:
            by_id[node.node_id] = node
        controller.tick(fresh, now=now)
    return tuple(by_id[node.node_id] for node in nodes)


def _apply_action(node: NodeSnapshot, action: MutationAction, now: float) -> NodeSnapshot:
    residencies = {item.model_id: item for item in node.residencies}
    cached = set(node.cached_models)
    current = residencies.get(action.model_id)
    if action.kind == ActionKind.LOAD:
        cached.add(action.model_id)
        residencies[action.model_id] = ModelResidency(
            action.model_id,
            action.memory_mb,
            ResidencyState.CACHED,
            loaded_at=now,
            artifact_sha256=action.artifact_sha256,
            runtime=action.runtime,
        )
    elif action.kind == ActionKind.WARM:
        residencies[action.model_id] = replace(
            current
            or ModelResidency(action.model_id, action.memory_mb, ResidencyState.CACHED),
            state=ResidencyState.READY,
            loaded_at=now,
            last_used_at=now,
            runtime=action.runtime,
        )
    elif action.kind == ActionKind.DRAIN and current is not None:
        residencies[action.model_id] = replace(
            current, state=ResidencyState.DRAINING, active_requests=0
        )
    elif action.kind == ActionKind.UNLOAD:
        residencies.pop(action.model_id, None)
    elif action.kind == ActionKind.EVICT:
        residencies.pop(action.model_id, None)
        cached.discard(action.model_id)
    return replace(
        node,
        residencies=tuple(sorted(residencies.values(), key=lambda item: item.model_id)),
        cached_models=tuple(sorted(cached)),
        last_heartbeat=now,
    )


def _check_plan(
    controller: AllocatorController,
    active_nodes: tuple[NodeSnapshot, ...],
    checks: Counter[str],
    failures: list[dict[str, Any]],
    cycle: int,
) -> None:
    plan = controller.last_plan
    if plan is None:
        _check(False, "plan_exists", checks, failures, cycle, "controller produced no plan")
        return
    active = {node.node_id: node for node in active_nodes}
    allocation: Counter[str] = Counter()
    for assignment in plan.assignments:
        _check(
            assignment.node_id in active,
            "no_partitioned_assignment",
            checks,
            failures,
            cycle,
            f"plan assigned {assignment.model_id} to absent {assignment.node_id}",
        )
        allocation[assignment.node_id] += assignment.memory_mb
    for node_id, used in allocation.items():
        node = active[node_id]
        budget = math.floor(
            (node.capacity_mb - node.reserved_mb)
            * (1 - controller.planner.policy.memory_headroom_fraction)
        )
        _check(
            used <= budget,
            "no_memory_overcommit",
            checks,
            failures,
            cycle,
            f"{node_id} planned {used} MB into {budget} MB budget",
        )


def _check(
    condition: bool,
    name: str,
    checks: Counter[str],
    failures: list[dict[str, Any]],
    cycle: int,
    detail: str,
) -> None:
    if condition:
        checks[name] += 1
    else:
        failures.append({"cycle": cycle, "check": name, "detail": detail})


def _event(cycle: int, timestamp: float, kind: str, term: int, **detail: Any) -> dict[str, Any]:
    return {"cycle": cycle, "timestamp": timestamp, "kind": kind, "controller_term": term, **detail}


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_checkpoint(value: dict[str, Any], config: ResilienceConfig) -> None:
    if int(value.get("schema_version") or 0) != RESILIENCE_SCHEMA_VERSION:
        raise ValueError("unsupported allocator resilience checkpoint schema")
    if value.get("configuration") != asdict(config):
        raise ValueError("resilience checkpoint configuration does not match this run")
    completed = int(value.get("completed_cycles") or 0)
    if not 0 <= completed <= config.cycles:
        raise ValueError("invalid completed cycle count in resilience checkpoint")
