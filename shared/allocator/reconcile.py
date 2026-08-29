"""Turn a desired placement into safe, idempotent node mutations.

Planning and execution are separate on purpose.  The planner may decide a model belongs elsewhere;
the reconciler must still prove that a replacement is ready, respect host cooldowns, drain work, and
stay inside a mutation budget before anything destructive is allowed.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from shared.allocator.models import (
    ActionKind,
    AllocatorMode,
    ModelProfile,
    ModelResidency,
    MutationAction,
    NodeSnapshot,
    NodeState,
    PlacementPlan,
    ResidencyState,
    canonical_sha256,
    stable_digest,
)


class MutationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class MutationRecord:
    action_id: str
    kind: ActionKind
    node_id: str
    model_id: str
    status: MutationStatus
    attempted_at: float
    completed_at: float = 0.0
    duration_seconds: float = 0.0
    failures: int = 0
    message: str = ""
    artifact_sha256: str = ""

    def __post_init__(self) -> None:
        if not self.action_id or not self.node_id or not self.model_id:
            raise ValueError("mutation record identity and target are required")
        if not isinstance(self.kind, ActionKind):
            object.__setattr__(self, "kind", ActionKind(self.kind))
        if not isinstance(self.status, MutationStatus):
            object.__setattr__(self, "status", MutationStatus(self.status))
        for name in ("attempted_at", "completed_at", "duration_seconds"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.failures < 0:
            raise ValueError("failures must be non-negative")
        object.__setattr__(
            self,
            "artifact_sha256",
            canonical_sha256(self.artifact_sha256),
        )


@dataclass(frozen=True, slots=True)
class DeferredMutation:
    kind: ActionKind
    node_id: str
    model_id: str
    code: str
    message: str
    retry_at: float = 0.0


@dataclass(frozen=True, slots=True)
class ReconcilePolicy:
    max_concurrent_mutations: int = 4
    max_mutations_per_node: int = 1
    mutation_cooldown_seconds: float = 60.0
    failure_backoff_base_seconds: float = 30.0
    failure_backoff_max_seconds: float = 3_600.0
    success_observation_timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        if self.max_concurrent_mutations < 1 or self.max_mutations_per_node < 1:
            raise ValueError("mutation limits must be positive")
        for name in (
            "mutation_cooldown_seconds",
            "failure_backoff_base_seconds",
            "failure_backoff_max_seconds",
            "success_observation_timeout_seconds",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.failure_backoff_max_seconds < self.failure_backoff_base_seconds:
            raise ValueError("maximum failure backoff cannot be smaller than its base")


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    plan_generation: str
    mode: AllocatorMode
    actions: tuple[MutationAction, ...] = ()
    deferred: tuple[DeferredMutation, ...] = ()

    @property
    def executable_actions(self) -> tuple[MutationAction, ...]:
        return tuple(action for action in self.actions if action.executable)


@dataclass(slots=True)
class _DestructiveSafetyState:
    ready_pairs: set[tuple[str, str]]
    managed_ready_pairs: set[tuple[str, str]]
    domain_counts: dict[str, dict[str, int]]
    domain_floors: dict[str, int]
    desired_nodes: dict[str, tuple[str, ...]]
    target_by_model: dict[str, int]


class Reconciler:
    def __init__(self, policy: ReconcilePolicy | None = None) -> None:
        self.policy = policy or ReconcilePolicy()

    def destructive_command_deferrals(
        self,
        plan: PlacementPlan,
        nodes: Iterable[NodeSnapshot],
        profiles: Iterable[ModelProfile],
        actions: Iterable[MutationAction],
        *,
        now: float | None = None,
    ) -> dict[str, DeferredMutation]:
        """Return queued destructive commands that are no longer safe to deliver.

        Pending commands outlive the reconciliation tick that created them.  A later profile or
        heartbeat can therefore invalidate replacement readiness, ownership, minimum-residency, or
        failure-domain evidence while the pair remains obsolete.  Re-evaluate those safety facts in
        deterministic command order and project only the DRAIN commands that remain valid, so a
        batch cannot jointly cross the live diversity floor.
        """

        timestamp = time.time() if now is None else float(now)
        if not math.isfinite(timestamp) or timestamp < 0:
            raise ValueError("now must be finite and non-negative")
        destructive = sorted(
            (
                action
                for action in actions
                if action.kind in (ActionKind.DRAIN, ActionKind.UNLOAD)
            ),
            key=lambda action: (action.created_at, action.action_id),
        )
        if not destructive:
            return {}
        node_by_id = {node.node_id: node for node in nodes}
        residency_by_pair = {
            (node.node_id, residency.model_id): residency
            for node in node_by_id.values()
            for residency in node.residencies
        }
        profile_by_id = {profile.model_id: profile for profile in profiles}
        safety = _destructive_safety_state(plan, node_by_id, profile_by_id)
        desired_pairs = plan.desired_pairs
        preempted_pairs = plan.preempted_pairs
        deferrals: dict[str, DeferredMutation] = {}
        for action in destructive:
            pair = (action.node_id, action.model_id)
            priority_preemption = pair in preempted_pairs
            if pair in desired_pairs and not priority_preemption:
                deferrals[action.action_id] = DeferredMutation(
                    action.kind,
                    action.node_id,
                    action.model_id,
                    "desired_again",
                    "The residency is part of the current desired placement",
                )
                continue
            node = node_by_id.get(action.node_id)
            profile = profile_by_id.get(action.model_id)
            residency = residency_by_pair.get(pair)
            if node is None or profile is None or residency is None:
                deferrals[action.action_id] = DeferredMutation(
                    action.kind,
                    action.node_id,
                    action.model_id,
                    "target_missing",
                    "The target node, model profile, or residency no longer exists",
                )
                continue
            deferral = _destructive_deferral(
                action.kind,
                node,
                residency,
                profile,
                safety,
                timestamp,
                priority_preemption=priority_preemption,
                allow_applied_state=True,
            )
            if deferral is not None:
                deferrals[action.action_id] = deferral
                continue
            if action.kind == ActionKind.DRAIN and residency.state == ResidencyState.READY:
                _project_drain(safety, node, profile.model_id)
        return deferrals

    def reconcile(
        self,
        plan: PlacementPlan,
        nodes: Iterable[NodeSnapshot],
        profiles: Iterable[ModelProfile],
        history: Iterable[MutationRecord] = (),
        *,
        mode: AllocatorMode = AllocatorMode.RECOMMEND,
        now: float | None = None,
        blocked_until: Mapping[tuple[ActionKind, str, str], float] | None = None,
        blocked_causes: Mapping[
            tuple[ActionKind, str, str], MutationStatus | str
        ] | None = None,
        blocked_destructive_models: Iterable[str] = (),
        startup_seconds: Mapping[tuple[str, str], float] | None = None,
    ) -> ReconcileResult:
        timestamp = time.time() if now is None else float(now)
        if not math.isfinite(timestamp) or timestamp < 0:
            raise ValueError("now must be finite and non-negative")
        if not isinstance(mode, AllocatorMode):
            mode = AllocatorMode(mode)
        startup_by_pair: dict[tuple[str, str], float] = {}
        for key, raw_duration in (startup_seconds or {}).items():
            if (
                not isinstance(key, tuple)
                or len(key) != 2
                or not all(isinstance(item, str) and item for item in key)
                or isinstance(raw_duration, bool)
            ):
                raise ValueError("startup estimates must identify a node/model pair")
            duration = float(raw_duration)
            if not math.isfinite(duration) or duration < 0:
                raise ValueError("startup estimates must be finite and non-negative")
            startup_by_pair[key] = duration
        node_by_id = {node.node_id: node for node in nodes}
        residency_by_pair = {
            (node.node_id, residency.model_id): residency
            for node in node_by_id.values()
            for residency in node.residencies
        }
        profile_by_id = {profile.model_id: profile for profile in profiles}
        # Controller history is an append-only transition log: one action commonly has PENDING,
        # RUNNING, then SUCCEEDED rows.  Only its latest row is current state.  Treating the old
        # PENDING row as still active would permanently consume the node mutation budget and make a
        # later scale-down impossible.
        records = _latest_action_records(history)
        history_by_transition: dict[
            tuple[ActionKind, str, str, str], list[MutationRecord]
        ] = {}
        for record in records:
            history_by_transition.setdefault(
                (
                    record.kind,
                    record.node_id,
                    record.model_id,
                    record.artifact_sha256,
                ),
                [],
            ).append(record)
        # A controller-supplied block map is the durable cooldown authority.  In particular it
        # has already bounded persisted wall-clock deadlines after a clock rollback.  Recomputing
        # a terminal history deadline from a future-dated receipt would create a sliding
        # ``now + delay`` horizon that never expires while the clock catches up.
        history_cooldowns = blocked_until is None
        mutation_blocks = dict(blocked_until or {})
        if any(not math.isfinite(value) or value < 0 for value in mutation_blocks.values()):
            raise ValueError("mutation blocked-until values must be finite and non-negative")
        mutation_block_causes: dict[
            tuple[ActionKind, str, str], MutationStatus
        ] = {}
        destructive_model_blocks = {str(model_id) for model_id in blocked_destructive_models}
        for key, raw_cause in (blocked_causes or {}).items():
            cause = (
                raw_cause
                if isinstance(raw_cause, MutationStatus)
                else MutationStatus(raw_cause)
            )
            if cause not in (
                MutationStatus.SUCCEEDED,
                MutationStatus.FAILED,
                MutationStatus.CANCELLED,
            ):
                raise ValueError("mutation block causes must be terminal outcomes")
            if key in mutation_blocks:
                mutation_block_causes[key] = cause
        active_records = [
            item for item in records if item.status in (MutationStatus.PENDING, MutationStatus.RUNNING)
        ]
        active_by_node: dict[str, int] = {}
        for record in active_records:
            active_by_node[record.node_id] = active_by_node.get(record.node_id, 0) + 1
        budget = max(0, self.policy.max_concurrent_mutations - len(active_records))

        proposals: list[MutationAction] = []
        deferred: list[DeferredMutation] = []
        desired = plan.desired_pairs
        urgency_by_model = dict(plan.model_urgencies)
        replica_index_by_pair = {
            (assignment.node_id, assignment.model_id): assignment.replica_index
            for assignment in plan.assignments
        }
        next_preemption_round: dict[str, int] = {}
        for assignment in plan.assignments:
            next_preemption_round[assignment.model_id] = max(
                next_preemption_round.get(assignment.model_id, 0),
                assignment.replica_index + 1,
            )
        preemption_round_by_pair: dict[tuple[str, str], int] = {}
        preemption_group_round: dict[tuple[str, str], int] = {}
        preemption_group_by_pair: dict[tuple[str, str], tuple[str, str]] = {}
        for preemption in plan.preemptions:
            if not preemption.for_model_id:
                continue
            group = (preemption.node_id, preemption.for_model_id)
            if group not in preemption_group_round:
                preemption_group_round[group] = next_preemption_round.get(
                    preemption.for_model_id,
                    0,
                )
                next_preemption_round[preemption.for_model_id] = (
                    preemption_group_round[group] + 1
                )
            pair = (preemption.node_id, preemption.model_id)
            preemption_round_by_pair[pair] = preemption_group_round[group]
            preemption_group_by_pair[pair] = group
        preemption_release_work_by_group: dict[tuple[str, str], int] = {}
        preemption_wait_by_group: dict[tuple[str, str], float] = {}
        for pair, group in preemption_group_by_pair.items():
            victim = residency_by_pair.get(pair)
            victim_profile = profile_by_id.get(pair[1])
            if victim is None or victim_profile is None:
                continue
            remaining_transitions = (
                2 if victim.state == ResidencyState.READY else 1
            )
            preemption_release_work_by_group[group] = (
                preemption_release_work_by_group.get(group, 0)
                + remaining_transitions
            )
            preemption_wait_by_group[group] = max(
                preemption_wait_by_group.get(group, 0.0),
                victim.active_requests * victim_profile.expected_service_seconds,
            )
        preemptions = {
            (item.node_id, item.model_id): item for item in plan.preemptions
        }
        safety = _destructive_safety_state(plan, node_by_id, profile_by_id)
        ready_pairs = safety.ready_pairs
        unsatisfied_direct_models = {
            item.model_id
            for item in plan.unsatisfied
            if item.missing_replicas > 0
            and urgency_by_model.get(item.model_id, 0) >= 2
        }
        # Do not let a permanently impossible direct model idle unrelated capacity. Speculative
        # availability waits only while a concrete assignment for unsatisfied direct service is
        # itself converging. The planner has already given direct work first choice of every
        # compatible node and staged any capacity-releasing preemption it can safely perform.
        service_capacity_unsatisfied = any(
            model_id in unsatisfied_direct_models
            and (node_id, model_id) not in ready_pairs
            for node_id, model_id in replica_index_by_pair
        )
        active_drain_pairs: set[tuple[str, str]] = set()
        for record in active_records:
            if record.kind != ActionKind.DRAIN:
                continue
            candidate = node_by_id.get(record.node_id)
            if candidate is None:
                continue
            current = residency_by_pair.get((record.node_id, record.model_id))
            if current is None or current.state != ResidencyState.READY:
                continue
            active_drain_pairs.add((record.node_id, record.model_id))
            _project_drain(safety, candidate, record.model_id)

        # Availability comes first: cache/download and warm every missing desired replica before any
        # obsolete one is considered for removal.
        for assignment in plan.assignments:
            pair = (assignment.node_id, assignment.model_id)
            if pair in ready_pairs:
                continue
            if (
                service_capacity_unsatisfied
                and urgency_by_model.get(assignment.model_id, 0) < 2
            ):
                deferred.append(
                    DeferredMutation(
                        ActionKind.WARM,
                        assignment.node_id,
                        assignment.model_id,
                        "service_capacity_unsatisfied",
                        "Speculative capacity waits until direct service demand converges",
                    )
                )
                continue
            node = node_by_id.get(assignment.node_id)
            profile = profile_by_id.get(assignment.model_id)
            if node is None or profile is None:
                deferred.append(
                    DeferredMutation(
                        ActionKind.WARM,
                        assignment.node_id,
                        assignment.model_id,
                        "target_missing",
                        "The target node or model profile no longer exists",
                    )
                )
                continue
            residency = residency_by_pair.get(pair)
            if residency and residency.state in (ResidencyState.LOADING, ResidencyState.WARMING):
                deferred.append(
                    DeferredMutation(
                        ActionKind.WARM,
                        node.node_id,
                        profile.model_id,
                        "already_in_progress",
                        f"Model is already {residency.state.value}",
                    )
                )
                continue
            cached = (
                (not profile.artifact_sha256 and profile.model_id in node.cached_models)
                or bool(
                    residency
                    and residency.state
                    in (
                        ResidencyState.CACHED,
                        ResidencyState.DRAINING,
                        ResidencyState.FAILED,
                    )
                    and profile.matches_artifact(residency)
                )
            )
            authoritative_rewarm_state = bool(
                residency
                and residency.state in (
                    ResidencyState.CACHED,
                    ResidencyState.DRAINING,
                    ResidencyState.FAILED,
                )
            )
            load_action: MutationAction | None = None
            if not cached:
                load_action = self._proposal(
                    ActionKind.LOAD,
                    node,
                    profile,
                    plan,
                    timestamp,
                    "Download or verify model weights on the selected node",
                    history_by_transition=history_by_transition,
                    mode=mode,
                    blocked_until=mutation_blocks,
                    blocked_causes=mutation_block_causes,
                    history_cooldowns=history_cooldowns,
                    memory_mb=assignment.memory_mb,
                )
                if load_action is None:
                    deferred.extend(
                        self._why_deferred(
                            ActionKind.LOAD,
                            node,
                            profile,
                            history_by_transition,
                            timestamp,
                            mode=mode,
                            blocked_until=mutation_blocks,
                            blocked_causes=mutation_block_causes,
                            history_cooldowns=history_cooldowns,
                        )
                    )
                    deferred.append(
                        DeferredMutation(
                            ActionKind.WARM,
                            node.node_id,
                            profile.model_id,
                            "artifact_not_cached",
                            "Model weights must be cached before the model can warm",
                        )
                    )
                    continue
                else:
                    proposals.append(load_action)
            warm_dependencies = (load_action.action_id,) if load_action else ()
            warm_action = self._proposal(
                ActionKind.WARM,
                node,
                profile,
                plan,
                timestamp,
                "Start the model and wait for its readiness probe",
                dependencies=warm_dependencies,
                history_by_transition=history_by_transition,
                mode=mode,
                blocked_until=mutation_blocks,
                blocked_causes=mutation_block_causes,
                history_cooldowns=history_cooldowns,
                bypass_success_observation=authoritative_rewarm_state,
                memory_mb=assignment.memory_mb,
            )
            if warm_action is None:
                deferred.extend(
                    self._why_deferred(
                        ActionKind.WARM,
                        node,
                        profile,
                        history_by_transition,
                        timestamp,
                        mode=mode,
                        blocked_until=mutation_blocks,
                        blocked_causes=mutation_block_causes,
                        history_cooldowns=history_cooldowns,
                        bypass_success_observation=authoritative_rewarm_state,
                    )
                )
            else:
                proposals.append(warm_action)

        # Destructive work is staged. READY -> DRAIN only when every desired replacement is ready;
        # DRAINING -> UNLOAD only after the node reports no active requests.
        for node in sorted(node_by_id.values(), key=lambda item: item.node_id):
            for residency in sorted(node.residencies, key=lambda item: item.model_id):
                pair = (node.node_id, residency.model_id)
                priority_preemption = preemptions.get(pair)
                if (pair in desired and priority_preemption is None) or residency.state in (
                    ResidencyState.CACHED,
                    ResidencyState.LOADING,
                    ResidencyState.WARMING,
                ):
                    continue
                profile = profile_by_id.get(residency.model_id)
                if profile is None:
                    deferred.append(
                        DeferredMutation(
                            ActionKind.UNLOAD,
                            node.node_id,
                            residency.model_id,
                            "not_allocator_owned",
                            "The residency is pinned, external, or manually managed",
                        )
                    )
                    continue
                action_kind = (
                    ActionKind.DRAIN
                    if residency.state == ResidencyState.READY
                    else ActionKind.UNLOAD
                )
                safety_deferral = _destructive_deferral(
                    action_kind,
                    node,
                    residency,
                    profile,
                    safety,
                    timestamp,
                    priority_preemption=priority_preemption is not None,
                    diversity_already_projected=(
                        action_kind == ActionKind.DRAIN and pair in active_drain_pairs
                    ),
                )
                if safety_deferral is not None:
                    deferred.append(safety_deferral)
                    continue
                if profile.model_id in destructive_model_blocks:
                    deferred.append(
                        DeferredMutation(
                            action_kind,
                            node.node_id,
                            profile.model_id,
                            "destructive_outcome_unresolved",
                            (
                                "A withdrawn destructive command may still be applied; "
                                "waiting for authoritative node state or a terminal receipt"
                            ),
                        )
                    )
                    continue
                if action_kind == ActionKind.DRAIN:
                    action = self._proposal(
                        ActionKind.DRAIN,
                        node,
                        profile,
                        plan,
                        timestamp,
                        (
                            "Yield scarce capacity to higher-priority model "
                            f"{priority_preemption.for_model_id!r}"
                            if priority_preemption is not None
                            and priority_preemption.for_model_id
                            else "Enforce the host model-capacity policy"
                            if priority_preemption is not None
                            else "Stop assigning new work before removing the obsolete replica"
                        ),
                        history_by_transition=history_by_transition,
                        mode=mode,
                        blocked_until=mutation_blocks,
                        blocked_causes=mutation_block_causes,
                        history_cooldowns=history_cooldowns,
                        bypass_success_observation=True,
                        residency=residency,
                    )
                else:
                    action = self._proposal(
                        ActionKind.UNLOAD,
                        node,
                        profile,
                        plan,
                        timestamp,
                        "Release model memory after the replica drained",
                        history_by_transition=history_by_transition,
                        mode=mode,
                        blocked_until=mutation_blocks,
                        blocked_causes=mutation_block_causes,
                        history_cooldowns=history_cooldowns,
                        bypass_success_observation=True,
                        residency=residency,
                    )
                if action is None:
                    deferred.extend(
                        self._why_deferred(
                            action_kind,
                            node,
                            profile,
                            history_by_transition,
                            timestamp,
                            mode=mode,
                            blocked_until=mutation_blocks,
                            blocked_causes=mutation_block_causes,
                            history_cooldowns=history_cooldowns,
                            bypass_success_observation=True,
                            residency=residency,
                        )
                    )
                else:
                    proposals.append(action)
                    if action.kind == ActionKind.DRAIN:
                        _project_drain(safety, node, profile.model_id)

        # Availability mutations have precedence over removals.  The governor limits executable
        # work, while recommend mode can show the full proposal set for human review.
        lifecycle_priority = {
            ActionKind.LOAD: 0,
            ActionKind.WARM: 1,
            ActionKind.DRAIN: 2,
            ActionKind.UNLOAD: 3,
        }

        def service_priority(action: MutationAction) -> tuple[int, int]:
            preemption = preemptions.get((action.node_id, action.model_id))
            if preemption is not None and preemption.for_model_id:
                beneficiary = profile_by_id.get(preemption.for_model_id)
                if beneficiary is not None:
                    return (
                        beneficiary.priority,
                        urgency_by_model.get(beneficiary.model_id, 0),
                    )
            if action.kind in (ActionKind.LOAD, ActionKind.WARM):
                profile = profile_by_id.get(action.model_id)
                return (
                    profile.priority if profile is not None else 0,
                    urgency_by_model.get(action.model_id, 0),
                )
            # Routine removal is maintenance, not service for the retired model. Keep it behind
            # every availability action and explicit capacity-unlocking preemption.
            return (-1, -1)

        def time_to_ready(action: MutationAction) -> float:
            """Estimate the beneficiary's remaining critical path for service.

            A dependent WARM retains the full cold path so it cannot jump ahead of the LOAD that
            makes its dependency deliverable.
            """

            preemption = preemptions.get((action.node_id, action.model_id))
            model_id = (
                preemption.for_model_id
                if preemption is not None and preemption.for_model_id
                else action.model_id
            )
            profile = profile_by_id.get(model_id)
            if profile is None:
                return math.inf
            warm_seconds = startup_by_pair.get(
                (action.node_id, model_id),
                profile.warm_seconds,
            )
            if action.kind == ActionKind.WARM and not action.dependencies:
                return warm_seconds
            if action.kind in (ActionKind.LOAD, ActionKind.WARM):
                return profile.load_seconds + warm_seconds
            if preemption is not None:
                group = preemption_group_by_pair.get(
                    (action.node_id, action.model_id)
                )
                wait_seconds = preemption_wait_by_group.get(group, 0.0)
                return profile.load_seconds + warm_seconds + wait_seconds
            return math.inf

        def action_stage(action: MutationAction) -> int:
            if (action.node_id, action.model_id) in preemptions:
                # An already-drained victim is one transition from free capacity. Starting another
                # drain first would add a full controller/heartbeat round to the beneficiary path.
                return 0 if action.kind == ActionKind.UNLOAD else 1
            return lifecycle_priority[action.kind]

        def proposal_sort_key(
            action: MutationAction,
        ) -> tuple[int, int, float, int, int, int, str, str]:
            admin_priority, demand_urgency = service_priority(action)
            pair = (action.node_id, action.model_id)
            preemption_group = preemption_group_by_pair.get(pair)
            return (
                -admin_priority,
                -demand_urgency,
                time_to_ready(action),
                preemption_release_work_by_group.get(preemption_group, 0),
                action_stage(action),
                preemption_round_by_pair.get(
                    pair,
                    replica_index_by_pair.get(pair, 0),
                ),
                action.node_id,
                action.model_id,
            )

        proposals.sort(key=proposal_sort_key)
        selected: list[MutationAction] = []
        scheduled_by_node = dict(active_by_node)
        for proposal in proposals:
            if mode == AllocatorMode.OBSERVE:
                deferred.append(
                    DeferredMutation(
                        proposal.kind,
                        proposal.node_id,
                        proposal.model_id,
                        "observe_mode",
                        "Observe mode records drift but proposes no mutations",
                    )
                )
                continue
            if mode == AllocatorMode.AUTOMATIC:
                if scheduled_by_node.get(proposal.node_id, 0) >= self.policy.max_mutations_per_node:
                    deferred.append(
                        DeferredMutation(
                            proposal.kind,
                            proposal.node_id,
                            proposal.model_id,
                            "node_mutation_limit",
                            "Another mutation already owns this node",
                        )
                    )
                    continue
                if budget <= 0:
                    deferred.append(
                        DeferredMutation(
                            proposal.kind,
                            proposal.node_id,
                            proposal.model_id,
                            "global_mutation_limit",
                            "The global mutation budget is full",
                        )
                    )
                    continue
                proposal = MutationAction(
                    action_id=proposal.action_id,
                    kind=proposal.kind,
                    node_id=proposal.node_id,
                    model_id=proposal.model_id,
                    memory_mb=proposal.memory_mb,
                    reason=proposal.reason,
                    plan_generation=proposal.plan_generation,
                    created_at=proposal.created_at,
                    not_before=proposal.not_before,
                    dependencies=proposal.dependencies,
                    executable=True,
                    artifact_sha256=proposal.artifact_sha256,
                )
                budget -= 1
                scheduled_by_node[proposal.node_id] = scheduled_by_node.get(proposal.node_id, 0) + 1
            selected.append(proposal)
        deferred.sort(key=lambda item: (item.node_id, item.model_id, item.kind.value, item.code))
        return ReconcileResult(plan.generation, mode, tuple(selected), tuple(deferred))

    def _proposal(
        self,
        kind: ActionKind,
        node: NodeSnapshot,
        profile: ModelProfile,
        plan: PlacementPlan,
        now: float,
        reason: str,
        *,
        dependencies: tuple[str, ...] = (),
        history_by_transition: Mapping[
            tuple[ActionKind, str, str, str], Sequence[MutationRecord]
        ],
        mode: AllocatorMode,
        blocked_until: Mapping[tuple[ActionKind, str, str], float],
        blocked_causes: Mapping[tuple[ActionKind, str, str], MutationStatus],
        history_cooldowns: bool,
        bypass_success_observation: bool = False,
        memory_mb: int | None = None,
        residency: ModelResidency | None = None,
    ) -> MutationAction | None:
        if kind.value not in node.actuator_capabilities or node.manually_managed:
            return None
        artifact_sha256 = _action_artifact_sha256(
            kind,
            node,
            profile,
            residency=residency,
        )
        matching = history_by_transition.get(
            (kind, node.node_id, profile.model_id, artifact_sha256),
            (),
        )
        latest_matching = matching[-1] if matching else None
        key = (kind, node.node_id, profile.model_id)
        block_applies = latest_matching is not None or not artifact_sha256
        block_cause = blocked_causes.get(key) if block_applies else None
        observed_prior_success = bool(
            bypass_success_observation
            and (
                (
                    block_cause == MutationStatus.SUCCEEDED
                    and (
                        latest_matching is None
                        or latest_matching.status == MutationStatus.SUCCEEDED
                    )
                )
                or (
                    block_cause is None
                    and latest_matching is not None
                    and latest_matching.status == MutationStatus.SUCCEEDED
                )
            )
        )
        cooldown_until = max(
            node.mutation_cooldown_until,
            (
                self._history_blocked_until(
                    matching,
                    now,
                )
                if history_cooldowns and not observed_prior_success
                else 0.0
            ),
            (
                0.0
                if observed_prior_success
                else (
                    blocked_until.get(key, 0.0)
                    if block_applies
                    else 0.0
                )
            ),
        )
        if cooldown_until > now:
            return None
        if any(
            item.status in (MutationStatus.PENDING, MutationStatus.RUNNING)
            for item in matching
        ):
            return None
        # A plan generation is stable while its inputs are stable, but a failed/succeeded command
        # must not reuse the same action ID: node runtimes cache terminal receipts by action ID.
        # The identity of all retained attempts keeps the next proposal deterministic for the same
        # append-only history while producing a fresh ID after each terminal attempt.  It is immune
        # to wall-clock rollback and to a late receipt for an older action arriving last.
        attempt_ids = sorted({item.action_id for item in matching})
        attempt_identity = stable_digest(attempt_ids)[:16] if attempt_ids else "initial"
        transition = f"{plan.generation}:attempts:{attempt_identity}"
        action_memory_mb = (
            profile.memory_for(node.runtimes) if memory_mb is None else memory_mb
        )
        return MutationAction(
            action_id=MutationAction.stable_id(kind, node.node_id, profile.model_id, transition),
            kind=kind,
            node_id=node.node_id,
            model_id=profile.model_id,
            memory_mb=action_memory_mb,
            reason=reason,
            plan_generation=plan.generation,
            created_at=now,
            not_before=cooldown_until,
            dependencies=dependencies,
            executable=mode == AllocatorMode.AUTOMATIC,
            artifact_sha256=artifact_sha256,
        )

    def _history_blocked_until(
        self,
        matching: Sequence[MutationRecord],
        now: float,
    ) -> float:
        if not matching:
            return 0.0
        latest = matching[-1]
        anchor = min(latest.completed_at or latest.attempted_at, now)
        if latest.status == MutationStatus.FAILED:
            delay = _failure_backoff(self.policy, latest.failures)
            return anchor + delay
        if latest.status == MutationStatus.SUCCEEDED:
            return anchor + self.policy.success_observation_timeout_seconds
        if latest.status in (MutationStatus.PENDING, MutationStatus.RUNNING):
            return math.inf
        cooldown_until = anchor + self.policy.mutation_cooldown_seconds
        if latest.status == MutationStatus.CANCELLED and latest.failures:
            failure_delay = _failure_backoff(self.policy, latest.failures)
            return max(cooldown_until, anchor + failure_delay)
        return cooldown_until

    def _why_deferred(
        self,
        kind: ActionKind,
        node: NodeSnapshot,
        profile: ModelProfile,
        history_by_transition: Mapping[
            tuple[ActionKind, str, str, str], Sequence[MutationRecord]
        ],
        now: float,
        *,
        mode: AllocatorMode,
        blocked_until: Mapping[tuple[ActionKind, str, str], float],
        blocked_causes: Mapping[tuple[ActionKind, str, str], MutationStatus],
        history_cooldowns: bool,
        bypass_success_observation: bool = False,
        residency: ModelResidency | None = None,
    ) -> list[DeferredMutation]:
        if node.manually_managed or kind.value not in node.actuator_capabilities:
            return [
                DeferredMutation(
                    kind,
                    node.node_id,
                    profile.model_id,
                    "actuator_unavailable",
                    f"Node does not permit allocator action {kind.value!r}",
                )
            ]
        artifact_sha256 = _action_artifact_sha256(
            kind,
            node,
            profile,
            residency=residency,
        )
        matching = history_by_transition.get(
            (kind, node.node_id, profile.model_id, artifact_sha256),
            (),
        )
        if any(
            item.status in (MutationStatus.PENDING, MutationStatus.RUNNING)
            for item in matching
        ):
            return [
                DeferredMutation(
                    kind,
                    node.node_id,
                    profile.model_id,
                    "already_in_progress",
                    "An equivalent mutation is already pending or running",
                )
            ]
        latest_matching = matching[-1] if matching else None
        key = (kind, node.node_id, profile.model_id)
        block_applies = latest_matching is not None or not artifact_sha256
        block_cause = blocked_causes.get(key) if block_applies else None
        observed_prior_success = bool(
            bypass_success_observation
            and (
                (
                    block_cause == MutationStatus.SUCCEEDED
                    and (
                        latest_matching is None
                        or latest_matching.status == MutationStatus.SUCCEEDED
                    )
                )
                or (
                    block_cause is None
                    and latest_matching is not None
                    and latest_matching.status == MutationStatus.SUCCEEDED
                )
            )
        )
        retry_at = max(
            node.mutation_cooldown_until,
            (
                self._history_blocked_until(
                    matching,
                    now,
                )
                if history_cooldowns and not observed_prior_success
                else 0.0
            ),
            (
                0.0
                if observed_prior_success
                else (
                    blocked_until.get(key, 0.0)
                    if block_applies
                    else 0.0
                )
            ),
        )
        return [
            DeferredMutation(
                kind,
                node.node_id,
                profile.model_id,
                "cooldown" if retry_at > now else f"{mode.value}_mode",
                "Mutation is waiting for its safety cooldown"
                if retry_at > now
                else "Mutation is not executable in the current mode",
                retry_at,
            )
        ]


def _destructive_safety_state(
    plan: PlacementPlan,
    node_by_id: Mapping[str, NodeSnapshot],
    profile_by_id: Mapping[str, ModelProfile],
) -> _DestructiveSafetyState:
    routable_nodes = tuple(
        node
        for node in node_by_id.values()
        if node.state in (NodeState.ACCEPTING, NodeState.THROTTLED)
    )
    ready_pairs: set[tuple[str, str]] = set()
    managed_ready_pairs: set[tuple[str, str]] = set()
    domain_counts: dict[str, dict[str, int]] = {}
    # Index actual READY inventory in one fleet scan. Iterating every node once per configured
    # model made an otherwise empty large fleet quadratic even though no destructive work existed.
    for node in routable_nodes:
        domain = node.failure_domain or node.node_id
        for residency in node.residencies:
            profile = profile_by_id.get(residency.model_id)
            if (
                residency.state != ResidencyState.READY
                or profile is None
                or not profile.matches_artifact(residency)
            ):
                continue
            pair = (node.node_id, residency.model_id)
            ready_pairs.add(pair)
            if residency.managed:
                managed_ready_pairs.add(pair)
            counts = domain_counts.setdefault(residency.model_id, {})
            counts[domain] = counts.get(domain, 0) + 1

    target_by_model = dict(plan.desired_replicas)
    desired_node_lists: dict[str, list[str]] = {}
    for assignment in plan.assignments:
        desired_node_lists.setdefault(assignment.model_id, []).append(
            assignment.node_id
        )
    desired_nodes = {
        model_id: tuple(node_ids)
        for model_id, node_ids in desired_node_lists.items()
    }
    domain_floors: dict[str, int] = {}
    for profile in profile_by_id.values():
        counts = domain_counts.setdefault(profile.model_id, {})
        required = min(
            profile.min_failure_domains,
            target_by_model.get(profile.model_id, 0),
        )
        domain_floors[profile.model_id] = min(len(counts), required)
    return _DestructiveSafetyState(
        ready_pairs,
        managed_ready_pairs,
        domain_counts,
        domain_floors,
        desired_nodes,
        target_by_model,
    )


def _action_artifact_sha256(
    kind: ActionKind,
    node: NodeSnapshot,
    profile: ModelProfile,
    *,
    residency: ModelResidency | None = None,
) -> str:
    """Bind constructive work to desired weights and destructive work to observed weights."""

    if kind in (ActionKind.LOAD, ActionKind.WARM):
        return profile.artifact_sha256
    if residency is None:
        residency = node.residency(profile.model_id)
    return residency.artifact_sha256 if residency is not None else ""


def _project_drain(
    safety: _DestructiveSafetyState,
    node: NodeSnapshot,
    model_id: str,
) -> None:
    counts = safety.domain_counts.get(model_id)
    if counts is None:
        return
    domain = node.failure_domain or node.node_id
    current = counts.get(domain, 0)
    if current <= 1:
        counts.pop(domain, None)
    else:
        counts[domain] = current - 1


def _destructive_deferral(
    kind: ActionKind,
    node: NodeSnapshot,
    residency: ModelResidency,
    profile: ModelProfile,
    safety: _DestructiveSafetyState,
    now: float,
    *,
    priority_preemption: bool = False,
    allow_applied_state: bool = False,
    diversity_already_projected: bool = False,
) -> DeferredMutation | None:
    if not residency.managed or residency.pinned or node.manually_managed:
        return DeferredMutation(
            kind,
            node.node_id,
            residency.model_id,
            "not_allocator_owned",
            "The residency is pinned, external, or manually managed",
        )
    if kind == ActionKind.DRAIN:
        valid_states = {ResidencyState.READY}
        if allow_applied_state:
            valid_states.add(ResidencyState.DRAINING)
    elif kind == ActionKind.UNLOAD:
        valid_states = {ResidencyState.DRAINING, ResidencyState.FAILED}
        if allow_applied_state:
            valid_states.add(ResidencyState.CACHED)
    else:
        raise ValueError("destructive safety requires a drain or unload action")
    if residency.state not in valid_states:
        return DeferredMutation(
            kind,
            node.node_id,
            residency.model_id,
            "residency_state_changed",
            f"Residency state changed to {residency.state.value}",
        )

    desired_nodes = safety.desired_nodes.get(profile.model_id, ())
    required_ready = safety.target_by_model.get(profile.model_id, 0)
    # Priority preemption may intentionally reduce a lower-priority model below its target when no
    # alternative capacity exists. A relocation is different: when the plan contains enough
    # destination assignments to retain the victim's full target, make those replacements READY
    # before releasing the old host. This is the make-before-break boundary between safe repacking
    # and an acknowledged service reduction.
    replacement_required = not priority_preemption or len(desired_nodes) >= required_ready
    if replacement_required:
        ready_desired = sum(
            (desired_node, profile.model_id) in safety.ready_pairs
            for desired_node in desired_nodes
        )
        if required_ready and ready_desired < required_ready:
            return DeferredMutation(
                kind,
                node.node_id,
                profile.model_id,
                "replacement_not_ready",
                f"Only {ready_desired} of {required_ready} required replacements are ready",
            )

        # Permissionless legacy engines are useful inventory, but an unauthenticated endpoint must
        # never be sufficient evidence to remove the last allocator-owned baseline.
        required_managed = (
            min(required_ready, max(1, profile.min_replicas)) if required_ready else 0
        )
        ready_managed = sum(
            (desired_node, profile.model_id) in safety.managed_ready_pairs
            for desired_node in desired_nodes
        )
        if ready_managed < required_managed:
            return DeferredMutation(
                kind,
                node.node_id,
                profile.model_id,
                "trusted_replacement_not_ready",
                (
                    f"Only {ready_managed} of {required_managed} required managed "
                    "replacements are ready"
                ),
            )

    if (
        kind == ActionKind.DRAIN
        and residency.state == ResidencyState.READY
        and replacement_required
        and not diversity_already_projected
    ):
        counts = safety.domain_counts[profile.model_id]
        domain = node.failure_domain or node.node_id
        remaining_domain_count = len(counts) - int(counts.get(domain, 0) == 1)
        # Conflicting hard pins can make the desired plan unable to satisfy its domain requirement.
        # Never converge by destroying diversity the live fleet already has.
        if remaining_domain_count < safety.domain_floors[profile.model_id]:
            return DeferredMutation(
                kind,
                node.node_id,
                profile.model_id,
                "failure_domain_replacement_not_ready",
                "Draining this replica would reduce live failure-domain diversity",
            )

    # A future wall-clock timestamp is not trustworthy age evidence. Treat it as newly loaded, not
    # infinitely old: removal safety must survive positive node skew.
    age = now - residency.loaded_at if 0 < residency.loaded_at <= now else 0.0
    if age < profile.min_residency_seconds:
        return DeferredMutation(
            kind,
            node.node_id,
            profile.model_id,
            "minimum_residency",
            "Minimum residency time has not elapsed",
            now + (profile.min_residency_seconds - age),
        )
    if (
        kind == ActionKind.UNLOAD
        and residency.state == ResidencyState.DRAINING
        and residency.active_requests
    ):
        return DeferredMutation(
            kind,
            node.node_id,
            profile.model_id,
            "requests_in_flight",
            f"Waiting for {residency.active_requests} active requests to finish",
        )
    if kind.value not in node.actuator_capabilities:
        return DeferredMutation(
            kind,
            node.node_id,
            profile.model_id,
            "actuator_unavailable",
            f"Node does not permit allocator action {kind.value!r}",
        )
    return None


def _latest_action_records(history: Iterable[MutationRecord]) -> list[MutationRecord]:
    latest: dict[str, tuple[int, MutationRecord]] = {}
    for index, record in enumerate(history):
        latest[record.action_id] = (index, record)
    return [item[1] for item in sorted(latest.values(), key=lambda item: item[0])]


def _failure_backoff(policy: ReconcilePolicy, failures: int) -> float:
    if not policy.failure_backoff_base_seconds:
        return 0.0
    try:
        delay = math.ldexp(
            policy.failure_backoff_base_seconds,
            max(0, failures - 1),
        )
    except (OverflowError, ValueError):
        return policy.failure_backoff_max_seconds
    if not math.isfinite(delay):
        return policy.failure_backoff_max_seconds
    return min(policy.failure_backoff_max_seconds, delay)
