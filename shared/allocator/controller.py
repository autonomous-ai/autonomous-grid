"""Thread-safe global allocation loop and desired-state command queue.

The controller is usable by the in-process local Grid server.  The hosted control plane can consume
the same pure planner/reconciler later, but owns its own durable database and wire authentication.
Automatic mode is opt-in; recommend mode is the safe default.
"""

from __future__ import annotations

import heapq
import math
import threading
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from shared import jsonio
from shared.allocator.demand import DemandTracker
from shared.allocator.intelligence import RequestFeatures, WorkloadIntelligence
from shared.allocator.models import (
    MAX_COUNTER,
    MAX_ID_LENGTH,
    SCHEMA_VERSION,
    ActionKind,
    AllocatorMode,
    DemandForecast,
    ModelProfile,
    ModelResidency,
    MutationAction,
    NodeSnapshot,
    NodeState,
    PlacementPlan,
    ResidencyState,
    canonical_sha256,
)
from shared.allocator.planner import PlacementPlanner, PlannerPolicy
from shared.allocator.reconcile import (
    MutationRecord,
    MutationStatus,
    ReconcilePolicy,
    Reconciler,
    ReconcileResult,
)

_TERMINAL = {MutationStatus.SUCCEEDED, MutationStatus.FAILED, MutationStatus.CANCELLED}
_DESTRUCTIVE_ACTION_KINDS = frozenset(
    {ActionKind.DRAIN, ActionKind.UNLOAD, ActionKind.EVICT}
)
_MAX_REPORTED_ACTION_DURATION_SECONDS = 3_600.0
_STARTUP_ESTIMATE_SAMPLES = 8
_STARTUP_ESTIMATE_FULL_CONFIDENCE_SAMPLES = 4
_STARTUP_ESTIMATE_EWMA_ALPHA = 0.25
_STARTUP_ESTIMATE_TTL_SECONDS = 30 * 24 * 60 * 60
_MAX_JOINT_PORTFOLIO_CANDIDATES = 4
_MAX_JOINT_PORTFOLIO_EVALUATIONS = 64
_MAX_JOINT_EXPLORATION_MODELS = 1
_MAX_JOINT_PREEMPTION_MODELS = 1
_SPARE_CANARY_RESERVE_SLOTS = 2
_SCARCE_REBALANCE_MIN_PRESSURE_GAIN = 1.5
_PORTFOLIO_FAILURE_HYSTERESIS_STEP = 0.5
_PORTFOLIO_PRESSURE_HYSTERESIS_STEP = 2.0


@dataclass(frozen=True, slots=True)
class _PortfolioBundleCache:
    timestamp: float
    nodes: tuple[NodeSnapshot, ...]
    profiles: tuple[ModelProfile, ...]
    telemetry_revision: int
    forecasts: tuple[DemandForecast, ...]
    selection: dict[str, str] | None
    selection_hints: dict[str, dict[str, Any]]


class AllocatorController:
    def __init__(
        self,
        *,
        mode: AllocatorMode = AllocatorMode.RECOMMEND,
        planner_policy: PlannerPolicy | None = None,
        reconcile_policy: ReconcilePolicy | None = None,
        state_path: Path | None = None,
        max_history: int = 1_000,
        membership_recovery_grace_seconds: float = 90.0,
        controller_term: int = 1,
        controller_id: str | None = None,
        controller_lease_expires_at: float = 0.0,
    ) -> None:
        if max_history < 1:
            raise ValueError("max_history must be positive")
        if (
            not math.isfinite(membership_recovery_grace_seconds)
            or membership_recovery_grace_seconds < 0
        ):
            raise ValueError("membership_recovery_grace_seconds must be finite and non-negative")
        if (
            isinstance(controller_term, bool)
            or not isinstance(controller_term, int)
            or not 0 < controller_term <= MAX_COUNTER
        ):
            raise ValueError("controller_term must be a positive supported integer")
        if controller_id is not None and (
            not controller_id or len(controller_id) > MAX_ID_LENGTH
        ):
            raise ValueError("controller_id must be non-empty and bounded")
        if (
            not math.isfinite(controller_lease_expires_at)
            or controller_lease_expires_at < 0
        ):
            raise ValueError("controller_lease_expires_at must be finite and non-negative")
        self.mode = AllocatorMode(mode)
        self.planner = PlacementPlanner(planner_policy)
        self.reconciler = Reconciler(reconcile_policy)
        self.demand = DemandTracker()
        self.intelligence = WorkloadIntelligence()
        self.state_path = state_path
        self.max_history = max_history
        self.membership_recovery_grace_seconds = float(membership_recovery_grace_seconds)
        self._profiles: dict[str, ModelProfile] = {}
        self._retiring: set[str] = set()
        self._history: list[MutationRecord] = []
        self._commands: dict[str, MutationAction] = {}
        self._delivered_command_ids: set[str] = set()
        self._withdrawn_destructive: dict[str, MutationAction] = {}
        self._failure_streaks: dict[tuple[ActionKind, str, str], int] = {}
        self._mutation_blocks: dict[tuple[ActionKind, str, str], float] = {}
        self._mutation_block_delays: dict[tuple[ActionKind, str, str], float] = {}
        self._mutation_block_causes: dict[
            tuple[ActionKind, str, str], MutationStatus
        ] = {}
        self._controller_epoch = uuid.uuid4().hex
        self._controller_term = controller_term
        self._controller_id = controller_id or self._controller_epoch
        self._controller_lease_expires_at = controller_lease_expires_at
        self._plan_sequence = 0
        self._action_sequence = 0
        self._last_plan_input_digest = ""
        self._last_plan_generation = ""
        self._restored_command_ids: set[str] = set()
        self._membership_recovery_started_at: float | None = None
        self._last_plan: PlacementPlan | None = None
        self._last_result: ReconcileResult | None = None
        self._last_tick_at = 0.0
        self._last_tick_duration_seconds = 0.0
        self._telemetry_revision = 0
        self._portfolio_bundle_cache: _PortfolioBundleCache | None = None
        self._last_delivery_safety_error = ""
        self._lock = threading.RLock()
        # Demand completion runs on the inference event-loop thread. Planning can legitimately
        # spend seconds under the controller lock on a large fleet, so telemetry has its own short
        # critical section and an atomically replaced allow-list. This keeps stream finalizers from
        # waiting behind placement/backtracking or a durable controller fsync.
        self._demand_lock = threading.Lock()
        self._observable_models: frozenset[str] = frozenset()
        self._observable_artifacts: dict[str, str] = {}
        if state_path and state_path.exists():
            self._restore(jsonio.load_json(state_path))
            self._restored_command_ids = set(self._commands)
        self._refresh_observable_models_locked()

    @property
    def profiles(self) -> tuple[ModelProfile, ...]:
        with self._lock:
            return tuple(self._profiles[key] for key in sorted(self._profiles))

    @property
    def history(self) -> tuple[MutationRecord, ...]:
        with self._lock:
            return tuple(self._history)

    @property
    def last_plan(self) -> PlacementPlan | None:
        with self._lock:
            return self._last_plan

    @property
    def controller_term(self) -> int:
        with self._lock:
            return self._controller_term
    def set_mode(self, mode: AllocatorMode) -> None:
        with self._lock:
            checkpoint = self._checkpoint()
            self.mode = AllocatorMode(mode)
            if self.mode != AllocatorMode.AUTOMATIC:
                self._cancel_all_pending("automatic allocation was disabled")
            self._save_or_rollback(checkpoint)

    def update_authority(
        self,
        term: int,
        controller_id: str,
        lease_expires_at: float,
    ) -> None:
        """Adopt a live lease and re-fence every durable pending command atomically."""

        if (
            isinstance(term, bool)
            or not isinstance(term, int)
            or not 0 < term <= MAX_COUNTER
        ):
            raise ValueError("controller authority term is invalid")
        controller_id = str(controller_id)
        if not controller_id or len(controller_id) > MAX_ID_LENGTH:
            raise ValueError("controller authority id is invalid")
        lease_expires_at = float(lease_expires_at)
        if not math.isfinite(lease_expires_at) or lease_expires_at <= 0:
            raise ValueError("controller authority lease expiry is invalid")
        with self._lock:
            if term < self._controller_term:
                raise ValueError("cannot move allocator controller authority backward")
            if (
                term == self._controller_term
                and controller_id != self._controller_id
                and self._controller_lease_expires_at > 0
            ):
                raise ValueError("cannot replace allocator leader within one term")
            if (
                term == self._controller_term
                and controller_id == self._controller_id
                and lease_expires_at == self._controller_lease_expires_at
            ):
                return
            checkpoint = self._checkpoint()
            self._controller_term = term
            self._controller_id = controller_id
            self._controller_lease_expires_at = lease_expires_at

            def refence(action: MutationAction) -> MutationAction:
                return replace(
                    action,
                    controller_term=term,
                    controller_id=controller_id,
                    controller_lease_expires_at=lease_expires_at,
                )

            self._commands = {
                action_id: refence(action)
                for action_id, action in self._commands.items()
            }
            self._withdrawn_destructive = {
                action_id: refence(action)
                for action_id, action in self._withdrawn_destructive.items()
            }
            if self._last_result is not None:
                self._last_result = replace(
                    self._last_result,
                    actions=tuple(refence(action) for action in self._last_result.actions),
                )
            self._save_or_rollback(checkpoint)

    def put_profile(self, profile: ModelProfile) -> None:
        with self._lock:
            checkpoint = self._checkpoint()
            self._profiles[profile.model_id] = profile
            self._retiring.discard(profile.model_id)
            self._refresh_observable_models_locked()
            self._save_or_rollback(checkpoint)

    def remove_profile(self, model_id: str) -> bool:
        with self._lock:
            profile = self._profiles.get(model_id)
            if profile is None or model_id in self._retiring:
                return False
            checkpoint = self._checkpoint()
            # Profile deletion is a desired-state transition, not a metadata deletion.  Keeping a
            # durable zero-replica tombstone lets an offline managed node return days later and
            # still receive the drain/unload sequence.
            self._profiles[model_id] = replace(
                profile,
                min_replicas=0,
                max_replicas=0,
                pinned_nodes=(),
            )
            self._retiring.add(model_id)
            self._cancel_commands_for_model(
                model_id,
                "model profile was retired",
                kinds=(ActionKind.LOAD, ActionKind.WARM),
            )
            self._save_or_rollback(checkpoint)
            # Persist the desired-state tombstone first. If that succeeds, stale demand is already
            # inert because retiring models are excluded from forecasts. Clearing it afterward
            # avoids letting a failed controller transaction roll back over telemetry that arrived
            # concurrently for other still-active models. A restart also prunes retiring keys.
            with self._demand_lock:
                self.demand.clear(model_id)
            return True

    def observe(
        self,
        model_id: str,
        *,
        service_seconds: float,
        latency_ms: float | None = None,
        queue_depth: int = 0,
        error: bool = False,
        timestamp: float | None = None,
    ) -> bool:
        # The inference surface is intentionally permissionless on a local Grid. Recording
        # arbitrary requested model names would let one client create an unbounded number of
        # demand-series keys and persist them forever. The immutable cache avoids taking the main
        # controller lock; the second check closes a concurrent profile-retirement race.
        if model_id not in self._observable_models:
            return False
        with self._demand_lock:
            if model_id not in self._observable_models:
                return False
            self.demand.observe(
                model_id,
                service_seconds=service_seconds,
                latency_ms=latency_ms,
                queue_depth=queue_depth,
                errors=int(error),
                timestamp=timestamp,
            )
            self._telemetry_revision += 1
            return True

    def observe_lifecycle(
        self,
        features: RequestFeatures,
        *,
        served_model: str = "",
        served_artifact_sha256: str = "",
        service_seconds: float,
        latency_ms: float | None = None,
        queue_depth: int = 0,
        error: bool = False,
        output_units: int = 0,
        quality: float | None = None,
        workflow_key: str = "",
        timestamp: float | None = None,
    ) -> bool:
        """Observe one completed request independently of any router implementation.

        Configured named models retain the existing direct scaling signal. Unknown/reserved model
        names additionally become portfolio-unbound workload pressure, allowing an inactive but
        configured capable model to receive a conservative canary placement at planning time.
        """

        served_artifact = canonical_sha256(served_artifact_sha256)
        # Direct demand follows the model the caller named, never the router's fallback choice.
        # Treating an ``auto`` request as direct demand for whichever ready model happened to serve
        # it lets routing accidents pin the wrong supply and can starve a missing specialist. The
        # served model still receives outcome evidence below; the workload forecast owns future
        # model selection.
        direct_model = features.requested_model
        directly_observable = direct_model in self._observable_models
        # Model binding belongs to the incoming request, not to the router's eventual fallback.
        # An ``auto`` coding request that a ready generalist happens to serve is still unbound
        # coding demand: retaining that signal is what lets the allocator provision a better
        # specialist for later requests. The served fallback contributes measured outcome evidence,
        # but it does not become caller-requested capacity merely because the router selected it.
        portfolio_unbound = features.requested_model not in self._observable_models
        with self._demand_lock:
            # Close a profile-retirement race after acquiring the telemetry lock.
            directly_observable = direct_model in self._observable_models
            portfolio_unbound = (
                features.requested_model not in self._observable_models
            )
            observable_artifact = self._observable_artifacts.get(served_model, "")
            if directly_observable:
                self.demand.observe(
                    direct_model,
                    service_seconds=service_seconds,
                    latency_ms=latency_ms,
                    queue_depth=queue_depth,
                    errors=int(error),
                    timestamp=timestamp,
                )
            self.intelligence.observe(
                features,
                served_model=(served_model if served_model in self._observable_models else ""),
                served_artifact_sha256=(served_artifact or observable_artifact),
                portfolio_unbound=portfolio_unbound,
                service_seconds=service_seconds,
                latency_ms=latency_ms,
                queue_depth=queue_depth,
                error=error,
                output_units=output_units,
                quality=quality,
                workflow_key=workflow_key,
                timestamp=timestamp,
            )
            self._telemetry_revision += 1
        return True

    def observe_evaluation(
        self,
        model_id: str,
        workload: str,
        *,
        artifact_sha256: str = "",
        quality: float,
        error: bool = False,
        latency_ms: float = 0.0,
        output_units: int = 0,
        timestamp: float | None = None,
    ):
        """Record authenticated quality evidence for one configured model.

        Evaluations deliberately do not touch the direct or unbound demand trackers. They improve
        future portfolio choice without allowing a benchmark run to provision itself.
        """

        artifact = canonical_sha256(artifact_sha256)
        if model_id not in self._observable_models:
            raise KeyError("allocator model profile not found")
        with self._demand_lock:
            if model_id not in self._observable_models:
                raise KeyError("allocator model profile not found")
            configured_artifact = self._observable_artifacts[model_id]
            if artifact and artifact != configured_artifact:
                raise ValueError(
                    "evaluation artifact_sha256 does not match the configured model revision"
                )
            outcome = self.intelligence.observe_model_evaluation(
                model_id,
                workload,
                artifact_sha256=configured_artifact,
                quality=quality,
                error=error,
                latency_ms=latency_ms,
                output_units=output_units,
                timestamp=timestamp,
            )
            self._telemetry_revision += 1
            return outcome

    def tick(
        self,
        nodes: Iterable[NodeSnapshot],
        *,
        now: float | None = None,
    ) -> ReconcileResult:
        timestamp = time.time() if now is None else float(now)
        if not math.isfinite(timestamp) or timestamp < 0:
            raise ValueError("now must be finite and non-negative")
        node_list = tuple(nodes)
        with self._lock:
            checkpoint = self._checkpoint()
            started_at = time.monotonic()
            try:
                result = self._tick_locked(node_list, timestamp, checkpoint)
            except jsonio.AtomicWriteCommittedError:
                raise
            except BaseException:
                # Planner/reconciler/action construction can fail before the persistence helper is
                # reached. Restore all controller transaction state, while deliberately retaining
                # independently locked request telemetry that arrived during the failed tick.
                self._rollback(checkpoint)
                raise
            try:
                self._last_tick_duration_seconds = max(
                    0.0,
                    time.monotonic() - started_at,
                )
            except Exception:  # pragma: no cover - platform clock failures are non-actionable
                # Observability is best-effort and must never fail or roll back committed desired
                # state. A later successful tick replaces this sentinel.
                self._last_tick_duration_seconds = 0.0
            return result

    def _tick_locked(
        self,
        node_list: tuple[NodeSnapshot, ...],
        timestamp: float,
        checkpoint: dict[str, Any],
    ) -> ReconcileResult:
        bounded_blocks: dict[tuple[ActionKind, str, str], float] = {}
        bounded_delays: dict[tuple[ActionKind, str, str], float] = {}
        bounded_causes: dict[
            tuple[ActionKind, str, str], MutationStatus
        ] = {}
        default_guard = max(
            self.reconciler.policy.failure_backoff_max_seconds,
            self.reconciler.policy.success_observation_timeout_seconds,
            self.reconciler.policy.mutation_cooldown_seconds,
        )
        for key, blocked_until in self._mutation_blocks.items():
            delay = self._mutation_block_delays.get(key, default_guard)
            bounded = min(blocked_until, timestamp + delay)
            if bounded > timestamp:
                bounded_blocks[key] = bounded
                bounded_delays[key] = delay
                cause = self._mutation_block_causes.get(key)
                if cause is not None:
                    bounded_causes[key] = cause
        self._mutation_blocks = bounded_blocks
        self._mutation_block_delays = bounded_delays
        self._mutation_block_causes = bounded_causes
        profiles = self.profiles
        startup_estimates, _ = self._learned_warm_estimates(now=timestamp)
        load_estimates, _ = self._learned_load_estimates(now=timestamp)
        placement_hints = self.planner.portfolio_placement_hints(
            node_list,
            profiles,
            now=timestamp,
            startup_seconds=startup_estimates,
            load_seconds=load_estimates,
        )
        forecasts = self._forecasts(
            timestamp,
            placement_hints=placement_hints,
            nodes=node_list,
        )
        learned_by_model = {
            profile.model_id: profile.warm_seconds for profile in profiles
        }
        for (_, model_id), estimate in startup_estimates.items():
            learned_by_model[model_id] = max(
                learned_by_model.get(model_id, 0.0),
                estimate,
            )
        learned_load_by_model = {
            profile.model_id: profile.load_seconds for profile in profiles
        }
        for (_, model_id), estimate in load_estimates.items():
            learned_load_by_model[model_id] = max(
                learned_load_by_model.get(model_id, 0.0),
                estimate,
            )
        effective_profiles = tuple(
            replace(
                profile,
                warm_seconds=learned_by_model.get(
                    profile.model_id,
                    profile.warm_seconds,
                ),
                load_seconds=learned_load_by_model.get(
                    profile.model_id,
                    profile.load_seconds,
                ),
            )
            for profile in profiles
        )
        self._resolve_withdrawn_destructive(node_list)
        planning_nodes = self._overlay_delivered_constructive_intent(
            node_list,
            effective_profiles,
        )
        raw_plan = self.planner.plan(
            planning_nodes,
            effective_profiles,
            forecasts,
            now=timestamp,
            startup_seconds=startup_estimates,
            load_seconds=load_estimates,
        )
        plan = self._version_plan(raw_plan)
        self._resolve_revalidated_withdrawn_destructive(
            plan,
            node_list,
            profiles,
            now=timestamp,
        )
        if self._restored_command_ids and self._membership_recovery_started_at is None:
            self._membership_recovery_started_at = timestamp
        elif (
            self._restored_command_ids
            and self._membership_recovery_started_at is not None
            and self._membership_recovery_started_at > timestamp
        ):
            # The grace starts after restart, so a wall-clock rollback must rebase its in-memory
            # anchor instead of preserving a future deadline until the corrected clock catches up.
            self._membership_recovery_started_at = timestamp
        blocked_destructive_models = self._cancel_stale_commands(
            plan,
            timestamp,
            node_list,
            profiles,
        )
        self._reprioritize_undelivered_constructive(
            plan,
            node_list,
            profiles,
            now=timestamp,
        )
        result = self.reconciler.reconcile(
            plan,
            planning_nodes,
            profiles,
            self._history,
            mode=self.mode,
            now=timestamp,
            blocked_until=self._mutation_blocks,
            blocked_causes=self._mutation_block_causes,
            blocked_destructive_models=blocked_destructive_models,
            startup_seconds=startup_estimates,
            load_seconds=load_estimates,
        )
        if self.mode == AllocatorMode.AUTOMATIC:
            result = self._sequence_actions(result)
        self._last_plan = plan
        self._last_result = result
        self._last_tick_at = timestamp
        if self.mode == AllocatorMode.AUTOMATIC:
            for action in result.executable_actions:
                if action.action_id in self._commands:
                    continue
                self._commands[action.action_id] = action
                self._append_record(
                    MutationRecord(
                        action_id=action.action_id,
                        kind=action.kind,
                        node_id=action.node_id,
                        model_id=action.model_id,
                        status=MutationStatus.PENDING,
                        attempted_at=timestamp,
                        artifact_sha256=action.artifact_sha256,
                        failures=self._failure_streak(
                            action.kind,
                            action.node_id,
                            action.model_id,
                        ),
                    )
                )
        self._save_or_rollback(checkpoint)
        return result

    def _overlay_delivered_constructive_intent(
        self,
        nodes: tuple[NodeSnapshot, ...],
        profiles: tuple[ModelProfile, ...],
    ) -> tuple[NodeSnapshot, ...]:
        """Keep an executing load/warm stable until its next factual heartbeat arrives.

        Command delivery and node telemetry are separate authenticated heartbeats. During that
        small gap, planning from the older snapshot can move the same desired replica elsewhere,
        cancel work that is already running, and pay for two cold starts. A delivered constructive
        command is bounded, durable evidence of an in-progress residency; destructive safety still
        uses the unmodified factual snapshots elsewhere in the tick.
        """

        if not self._delivered_command_ids:
            return nodes
        profile_by_id = {profile.model_id: profile for profile in profiles}
        intents: dict[str, list[MutationAction]] = {}
        for action in self._commands.values():
            if (
                action.action_id in self._delivered_command_ids
                and action.kind in (ActionKind.LOAD, ActionKind.WARM)
                and action.model_id in profile_by_id
            ):
                intents.setdefault(action.node_id, []).append(action)
        if not intents:
            return nodes

        overlaid: list[NodeSnapshot] = []
        for node in nodes:
            actions = intents.get(node.node_id)
            if not actions:
                overlaid.append(node)
                continue
            residencies = {item.model_id: item for item in node.residencies}
            for action in sorted(actions, key=lambda item: (item.created_at, item.action_id)):
                profile = profile_by_id[action.model_id]
                if (
                    action.memory_mb != profile.memory_for(node.runtimes)
                    or action.artifact_sha256 != profile.artifact_sha256
                ):
                    # Profile changes invalidate the old command; let normal stale-command
                    # cancellation replace it instead of preserving an obsolete intent.
                    continue
                current = residencies.get(action.model_id)
                if current is not None and not profile.matches_artifact(current):
                    # A different live revision needs the ordinary make-before-break path; one
                    # model-id slot cannot safely represent both artifacts synthetically.
                    continue
                if (
                    current is not None
                    and current.state == ResidencyState.READY
                    and profile.matches_artifact(current)
                ):
                    continue
                intended_state = (
                    ResidencyState.WARMING
                    if action.kind == ActionKind.WARM
                    else ResidencyState.LOADING
                )
                if current is None:
                    residencies[action.model_id] = ModelResidency(
                        action.model_id,
                        action.memory_mb,
                        intended_state,
                        loaded_at=action.created_at,
                        managed=True,
                        artifact_sha256=action.artifact_sha256,
                    )
                else:
                    residencies[action.model_id] = replace(
                        current,
                        memory_mb=action.memory_mb,
                        state=intended_state,
                        managed=True,
                        artifact_sha256=action.artifact_sha256,
                    )
            overlaid.append(replace(node, residencies=tuple(residencies.values())))
        return tuple(overlaid)

    def commands_for(
        self,
        node_id: str,
        *,
        now: float | None = None,
        include_destructive: bool = True,
        destructive_safety_factory: Callable[[], Iterable[NodeSnapshot]] | None = None,
    ) -> tuple[MutationAction, ...]:
        timestamp = time.time() if now is None else float(now)
        if not math.isfinite(timestamp) or timestamp < 0:
            raise ValueError("now must be finite and non-negative")
        with self._lock:
            urgency_by_model = dict(
                self._last_plan.model_urgencies if self._last_plan is not None else ()
            )
            preemption_by_pair = {
                (item.node_id, item.model_id): item.for_model_id
                for item in (
                    self._last_plan.preemptions if self._last_plan is not None else ()
                )
            }
            eviction_beneficiary_by_pair = {
                (item.node_id, item.model_id): item.for_model_id
                for item in (
                    self._last_plan.artifact_evictions
                    if self._last_plan is not None
                    else ()
                )
                if item.for_model_id
            }

            def delivery_rank(action: MutationAction) -> tuple[int, int]:
                beneficiary_id = preemption_by_pair.get(
                    (action.node_id, action.model_id),
                    "",
                )
                if not beneficiary_id and action.kind == ActionKind.EVICT:
                    beneficiary_id = eviction_beneficiary_by_pair.get(
                        (action.node_id, action.model_id),
                        "",
                    )
                if action.kind in (ActionKind.LOAD, ActionKind.WARM):
                    beneficiary_id = action.model_id
                if not beneficiary_id:
                    return (-1, -1)
                profile = self._profiles.get(beneficiary_id)
                return (
                    profile.priority if profile is not None else 0,
                    urgency_by_model.get(beneficiary_id, 0),
                )

            def delivery_key(action: MutationAction) -> tuple[int, int, float, float]:
                priority, urgency = delivery_rank(action)
                return (-priority, -urgency, action.not_before, action.created_at)

            commands = tuple(
                action
                for action in sorted(
                    self._commands.values(),
                    # Stable sorting retains the reconciler's insertion order within a service
                    # class, including LOAD before its dependent WARM.
                    key=delivery_key,
                )
                if action.node_id == node_id
                and action.not_before <= timestamp
                and (
                    include_destructive
                    or action.kind not in _DESTRUCTIVE_ACTION_KINDS
                )
            )
            newly_delivered = {
                action.action_id
                for action in commands
                if action.action_id not in self._delivered_command_ids
            }
            if newly_delivered:
                checkpoint = self._checkpoint()
                self._delivered_command_ids.update(newly_delivered)
                self._save_or_rollback(checkpoint)

            destructive = tuple(
                action
                for action in commands
                if action.kind in _DESTRUCTIVE_ACTION_KINDS
            )
            if not destructive or destructive_safety_factory is None:
                return commands

            # Delivery is a second safety boundary after planning. Persist the delivery marker first:
            # that fsync may block without consuming a route lease after the final proof. Only then,
            # while still holding the command lock, ask the server for a fresh raw-registry cut and
            # revalidate the complete queued destructive batch. The safe path performs no blocking
            # persistence after this proof.
            unsafe_destructive_models = {action.model_id for action in destructive}
            self._last_delivery_safety_error = ""
            try:
                safety_nodes = tuple(destructive_safety_factory())
                validation_timestamp = time.time()
                if not math.isfinite(validation_timestamp) or validation_timestamp < 0:
                    raise ValueError("delivery validation time must be finite and non-negative")
                if self._last_plan is not None:
                    deferrals = self.reconciler.destructive_command_deferrals(
                        self._last_plan,
                        safety_nodes,
                        self._profiles.values(),
                        self._commands.values(),
                        now=validation_timestamp,
                    )
                    unsafe_destructive_models = {
                        self._commands[action_id].model_id
                        for action_id in deferrals
                        if action_id in self._commands
                    }
            except Exception as exc:  # noqa: BLE001 - inability to prove safety suppresses destruction
                self._last_delivery_safety_error = (
                    f"destructive delivery safety validation failed: {exc}"[:500]
                )

            if not unsafe_destructive_models:
                return commands

            unsafe_actions = tuple(
                action
                for action in destructive
                if action.model_id in unsafe_destructive_models
            )
            unsafe_action_ids = {action.action_id for action in unsafe_actions}
            unsafe_newly_delivered = unsafe_action_ids.intersection(newly_delivered)
            if unsafe_newly_delivered:
                # The response has not been emitted, so normally undo markers prepared by this poll.
                # If the compensating write fails before commit, _save_or_rollback restores the
                # conservative false-delivery marker. If replace committed but its directory barrier
                # failed, memory and the visible file retain the removal. Either way the command is
                # suppressed from this response.
                marker_checkpoint = self._checkpoint()
                self._delivered_command_ids.difference_update(unsafe_newly_delivered)
                try:
                    self._save_or_rollback(marker_checkpoint)
                except Exception as exc:  # noqa: BLE001 - keep availability independent
                    compensation_error = (
                        "could not clear prepared destructive delivery markers: "
                        f"{exc}"
                    )
                    self._last_delivery_safety_error = (
                        f"{self._last_delivery_safety_error}; {compensation_error}"
                        if self._last_delivery_safety_error
                        else compensation_error
                    )[:500]
            return tuple(
                action for action in commands if action.action_id not in unsafe_action_ids
            )

    def acknowledge(
        self,
        node_id: str,
        action_id: str,
        status: MutationStatus,
        *,
        message: str = "",
        duration_seconds: Any = 0.0,
        artifact_fetched: bool = False,
        now: float | None = None,
    ) -> MutationRecord:
        timestamp = time.time() if now is None else float(now)
        if not math.isfinite(timestamp) or timestamp < 0:
            raise ValueError("now must be finite and non-negative")
        # Normalize potentially surprising string subclasses before the transaction checkpoint.
        # Nothing after this point may reject caller-controlled scalar input after mutating retry
        # state.
        message = str(message)[:500]
        duration = _bounded_action_duration(duration_seconds)
        status = MutationStatus(status)
        if not isinstance(artifact_fetched, bool):
            raise ValueError("artifact_fetched must be boolean")
        with self._lock:
            checkpoint = self._checkpoint()
            action = self._commands.get(action_id)
            prior = self._latest_record(action_id)
            if action is not None and action.node_id != node_id:
                raise KeyError("unknown allocator action for this node")
            if action is None and (prior is None or prior.node_id != node_id):
                raise KeyError("unknown allocator action for this node")

            if prior and prior.status in _TERMINAL:
                if prior.status == status:
                    if action_id in self._withdrawn_destructive:
                        self._resolve_delivered_action(action_id)
                        self._save_or_rollback(checkpoint)
                    return prior
                # A node can finish after the controller cancelled an in-flight command.  Its
                # authenticated terminal receipt is still useful factual state.  Other conflicting
                # terminal replays are ignored so a stale heartbeat cannot rewrite history or fail
                # the entire heartbeat transaction.
                if prior.status != MutationStatus.CANCELLED or status not in (
                    MutationStatus.SUCCEEDED,
                    MutationStatus.FAILED,
                ):
                    return prior
            elif action is None and status not in _TERMINAL:
                # The command was removed locally, but the node is replaying an older non-terminal
                # observation.  Preserve the known state and let a later terminal receipt settle it.
                assert prior is not None
                return prior

            source = action or prior
            assert source is not None
            failures = self._record_failure_outcome(
                source.kind,
                source.node_id,
                source.model_id,
                status,
                timestamp,
            )
            record = MutationRecord(
                action_id=action_id,
                kind=source.kind,
                node_id=source.node_id,
                model_id=source.model_id,
                status=status,
                attempted_at=(
                    prior.attempted_at
                    if prior
                    else (action.created_at if action is not None else timestamp)
                ),
                completed_at=(timestamp if status in _TERMINAL else 0.0),
                duration_seconds=(duration if status in _TERMINAL else 0.0),
                failures=failures,
                message=message,
                artifact_sha256=source.artifact_sha256,
                artifact_fetched=(
                    artifact_fetched
                    if status == MutationStatus.SUCCEEDED
                    and source.kind == ActionKind.LOAD
                    else False
                ),
            )
            self._append_record(record)
            if status in _TERMINAL:
                self._commands.pop(action_id, None)
                self._resolve_delivered_action(action_id)
                self._trim_history()
                if status != MutationStatus.SUCCEEDED:
                    self._cancel_dependents(
                        action_id,
                        timestamp,
                        f"prerequisite {action_id} ended as {status.value}",
                    )
                self._restored_command_ids.discard(action_id)
                if not self._restored_command_ids:
                    self._membership_recovery_started_at = None
            self._save_or_rollback(checkpoint)
            return record

    def status(self, nodes: Iterable[NodeSnapshot] = (), *, now: float | None = None) -> dict[str, Any]:
        timestamp = time.time() if now is None else float(now)
        node_list = tuple(nodes)
        with self._lock:
            profiles = self.profiles
            startup_estimates, startup_samples = self._learned_warm_estimates(
                now=timestamp
            )
            load_estimates, load_samples = self._learned_load_estimates(
                now=timestamp
            )
            with self._demand_lock:
                telemetry_revision = self._telemetry_revision
            cache = self._portfolio_bundle_cache
            active_profiles = tuple(
                profile
                for profile in profiles
                if profile.model_id not in self._retiring
            )
            portfolio_cache_hit = bool(
                cache is not None
                and cache.timestamp == timestamp
                and cache.nodes
                == tuple(sorted(node_list, key=lambda item: item.node_id))
                and cache.profiles == active_profiles
                and cache.telemetry_revision == telemetry_revision
            )
            if portfolio_cache_hit:
                assert cache is not None
                forecasts = cache.forecasts
                portfolio_selection = (
                    dict(cache.selection) if cache.selection is not None else None
                )
                # Status payloads are ordinary mutable dictionaries. Do not let an in-process
                # consumer mutate the cached nested diagnostics seen by a later caller.
                selection_hints = deepcopy(cache.selection_hints)
            else:
                placement_hints = self.planner.portfolio_placement_hints(
                    node_list,
                    profiles,
                    now=timestamp,
                    startup_seconds=startup_estimates,
                    load_seconds=load_estimates,
                )
                forecasts, portfolio_selection, selection_hints = self._forecast_bundle(
                    timestamp,
                    placement_hints=placement_hints,
                    nodes=node_list,
                )
            with self._demand_lock:
                workload_forecasts = self.intelligence.workload_forecasts(now=timestamp)
                portfolio_projections = self.intelligence.projections(
                    profiles,
                    now=timestamp,
                    placement_hints=selection_hints,
                    chosen_models=portfolio_selection,
                )
                model_workload_outcomes = self.intelligence.outcomes
            portfolio_admissions = _portfolio_admissions(
                portfolio_projections,
                node_list,
                profiles,
                self._last_plan,
                self._last_result,
            )
            selected_portfolio_models = sorted(
                {model_id for model_id in (portfolio_selection or {}).values() if model_id}
            )
            exploration_models: set[str] = set()
            for row in portfolio_projections:
                workload = str(row.get("workload") or "")
                selected = (portfolio_selection or {}).get(workload)
                selectable = [
                    candidate
                    for candidate in row.get("candidates") or ()
                    if candidate.get("selectable")
                ]
                if not selected or not selectable:
                    continue
                exploitation = max(
                    selectable,
                    key=lambda candidate: (
                        float(candidate.get("exploitation_score") or 0.0),
                        str(candidate.get("model_id") or ""),
                    ),
                )
                if selected != str(exploitation.get("model_id") or ""):
                    exploration_models.add(selected)
            return {
                "schema_version": SCHEMA_VERSION,
                "mode": self.mode.value,
                "controller_epoch": self._controller_epoch,
                "controller_term": self._controller_term,
                "controller_id": self._controller_id,
                "controller_lease_expires_at": self._controller_lease_expires_at,
                "plan_sequence": self._plan_sequence,
                "last_tick_at": self._last_tick_at,
                "last_tick_duration_seconds": self._last_tick_duration_seconds,
                "nodes": [
                    node.to_dict() for node in sorted(node_list, key=lambda item: item.node_id)
                ],
                "models": [
                    {
                        **profile.to_dict(),
                        "retiring": profile.model_id in self._retiring,
                    }
                    for profile in self.profiles
                ],
                "retiring_models": sorted(self._retiring),
                "forecasts": [asdict(item) for item in forecasts],
                "workload_forecasts": [
                    {**asdict(item), "workload": item.model_id}
                    for item in workload_forecasts
                ],
                "portfolio_projections": list(portfolio_projections),
                "portfolio_selection": dict(portfolio_selection or {}),
                "portfolio_admissions": portfolio_admissions,
                "portfolio_policy": {
                    "joint": bool(portfolio_selection is not None),
                    "snapshot_cache_hit": portfolio_cache_hit,
                    "objective": (
                        "preserve required service; maximize admitted workload coverage; "
                        "restore recently failing workloads among equally broad portfolios; "
                        "cross material failure/pressure bands before changing a stable portfolio; "
                        "maximize service-time-aware resource pressure and request coverage; "
                        "minimize replica shortfall; maximize measured model utility"
                    ),
                    "workloads": len(portfolio_selection or {}),
                    "selected_models": selected_portfolio_models,
                    "exploration_models": sorted(exploration_models),
                    "max_candidates_per_workload": _MAX_JOINT_PORTFOLIO_CANDIDATES,
                    "max_evaluations": _MAX_JOINT_PORTFOLIO_EVALUATIONS,
                    "max_exploration_models": _MAX_JOINT_EXPLORATION_MODELS,
                    "max_preemption_models": _MAX_JOINT_PREEMPTION_MODELS,
                    "minimum_evidence_samples": (
                        self.intelligence.portfolio_min_samples
                    ),
                    "minimum_evidence_offered_concurrency": (
                        self.intelligence.portfolio_min_offered_concurrency
                    ),
                },
                "portfolio_placement_hints": [
                    selection_hints[model_id]
                    for model_id in sorted(selection_hints)
                    if self._profiles[model_id].workload_scores
                ],
                "model_workload_outcomes": [
                    asdict(item) for item in model_workload_outcomes
                ],
                "learned_warm_seconds": [
                    {
                        "node_id": node_id,
                        "model_id": model_id,
                        "seconds": estimate,
                        "samples": startup_samples[(node_id, model_id)],
                    }
                    for (node_id, model_id), estimate in sorted(
                        startup_estimates.items()
                    )
                ],
                "learned_load_seconds": [
                    {
                        "node_id": node_id,
                        "model_id": model_id,
                        "seconds": estimate,
                        "samples": load_samples[(node_id, model_id)],
                    }
                    for (node_id, model_id), estimate in sorted(
                        load_estimates.items()
                    )
                ],
                "plan": self._last_plan.to_dict() if self._last_plan else None,
                "reconciliation": _result_dict(self._last_result),
                "pending_commands": [_action_dict(item) for item in self._commands.values()],
                "delivered_pending_action_ids": sorted(
                    set(self._commands).intersection(self._delivered_command_ids)
                ),
                "last_delivery_safety_error": self._last_delivery_safety_error,
                "withdrawn_destructive": [
                    _action_dict(item) for item in self._withdrawn_destructive.values()
                ],
                "history": [_record_dict(item) for item in self._history[-100:]],
            }

    def _append_record(self, record: MutationRecord, *, trim: bool = True) -> None:
        # One latest record per action/status transition is enough; repeated delivery acks are
        # idempotent and should not exhaust bounded history.
        if self._history and self._history[-1] == record:
            return
        self._history.append(record)
        if trim:
            self._trim_history()

    def _trim_history(self) -> None:
        """Bound completed history without ever forgetting an active command.

        The mutation governor and duplicate suppression rely on the latest row for every command
        still in ``_commands``. A withdrawn destructive command also retains its CANCELLED row until
        node state or a terminal receipt resolves possible late delivery. Those rows are exempt from
        the terminal-history cap; their count is bounded by the reconciler's mutation budget.
        """

        active_ids = set(self._commands).union(self._withdrawn_destructive)
        latest_active_index: dict[str, int] = {}
        terminal_indexes: list[int] = []
        for index, item in enumerate(self._history):
            if item.action_id in active_ids:
                latest_active_index[item.action_id] = index
            else:
                terminal_indexes.append(index)
        keep = set(latest_active_index.values())
        keep.update(terminal_indexes[-self.max_history :])
        if len(keep) != len(self._history):
            self._history = [
                item for index, item in enumerate(self._history) if index in keep
            ]

    def _latest_record(self, action_id: str) -> MutationRecord | None:
        return next((item for item in reversed(self._history) if item.action_id == action_id), None)

    def _forecasts(
        self,
        now: float,
        *,
        placement_hints: Mapping[str, Mapping[str, Any]] | None = None,
        nodes: Iterable[NodeSnapshot] = (),
    ) -> tuple[DemandForecast, ...]:
        forecasts, _, _ = self._forecast_bundle(
            now,
            placement_hints=placement_hints,
            nodes=nodes,
        )
        return forecasts

    def _forecast_bundle(
        self,
        now: float,
        *,
        placement_hints: Mapping[str, Mapping[str, Any]] | None = None,
        nodes: Iterable[NodeSnapshot] = (),
    ) -> tuple[
        tuple[DemandForecast, ...],
        dict[str, str] | None,
        dict[str, dict[str, Any]],
    ]:
        active_models = tuple(
            model_id for model_id in self._profiles if model_id not in self._retiring
        )
        profiles = tuple(self._profiles[model_id] for model_id in active_models)
        node_list = tuple(nodes)
        with self._demand_lock:
            direct = self.demand.forecasts(active_models, now=now)
            # Planner-backed portfolio search may evaluate dozens of complete fleet plans. Snapshot
            # bounded telemetry under its own mutex, then release it before any optimization so
            # inference finalizers can keep recording demand while the controller holds `_lock`.
            intelligence = WorkloadIntelligence.from_dict(self.intelligence.to_dict())
            telemetry_revision = self._telemetry_revision
        workload_forecasts = intelligence.portfolio_workload_forecast_map(now=now)
        selection_hints = _portfolio_selection_hints(
            profiles,
            direct,
            intelligence,
            placement_hints,
            node_list,
            self.planner,
            now=now,
            workload_forecasts=workload_forecasts,
        )
        if node_list:
            selection_hints = _spare_canary_hints(
                profiles,
                direct,
                intelligence,
                selection_hints,
                node_list,
                self.planner,
                now=now,
                workload_forecasts=workload_forecasts,
            )
        selection = self._joint_portfolio_selection(
            profiles,
            direct,
            node_list,
            now=now,
            placement_hints=selection_hints,
            intelligence=intelligence,
            workload_forecasts=workload_forecasts,
        )
        forecasts = intelligence.portfolio_forecasts(
            profiles,
            direct,
            now=now,
            placement_hints=selection_hints,
            chosen_models=selection,
            workload_forecasts=workload_forecasts,
        )
        self._portfolio_bundle_cache = _PortfolioBundleCache(
            timestamp=now,
            nodes=tuple(sorted(node_list, key=lambda item: item.node_id)),
            profiles=tuple(sorted(profiles, key=lambda item: item.model_id)),
            telemetry_revision=telemetry_revision,
            forecasts=forecasts,
            selection=(dict(selection) if selection is not None else None),
            selection_hints=deepcopy(selection_hints),
        )
        return forecasts, selection, selection_hints

    def _joint_portfolio_selection(
        self,
        profiles: tuple[ModelProfile, ...],
        direct: tuple[DemandForecast, ...],
        nodes: tuple[NodeSnapshot, ...],
        *,
        now: float,
        placement_hints: Mapping[str, Mapping[str, Any]] | None,
        intelligence: WorkloadIntelligence,
        workload_forecasts: Mapping[str, DemandForecast],
    ) -> dict[str, str] | None:
        """Coordinate workload choices against one real fleet plan.

        Independent argmax decisions can each be feasible in isolation while their combined model
        set cannot coexist. A bounded deterministic coordinate search evaluates complete portfolios
        with the authoritative placement planner. The cap keeps planning work independent of user
        cardinality and prevents a large catalog from turning one request burst into an unbounded
        combinatorial search.
        """

        if len(nodes) == 0:
            return None
        projections = intelligence.projections(
            profiles,
            now=now,
            placement_hints=placement_hints,
            workload_forecasts=workload_forecasts,
        )
        exclusive_device_models = {
            profile.model_id for profile in profiles if profile.replica_concurrency == 1
        }
        active_rows = [
            row
            for row in projections
            if intelligence.portfolio_evidence_ready(
                int(row.get("samples") or 0),
                float(row.get("offered_concurrency") or 0.0),
                immediately_feasible=(
                    str(row.get("workload") or "") in {"image", "video"}
                    and str(row.get("chosen_model") or "")
                    in exclusive_device_models
                    and (row.get("placement") or {}).get("spare_canary_allowed")
                    is True
                ),
            )
            and float(row.get("requests_per_minute") or 0.0) > 0
            and any(candidate.get("selectable") for candidate in row.get("candidates") or ())
        ]
        if len(active_rows) <= 1:
            return None

        row_by_workload = {str(row["workload"]): row for row in active_rows}
        selectable_by_workload = {
            workload: [
                candidate
                for candidate in row.get("candidates") or ()
                if candidate.get("selectable")
            ]
            for workload, row in row_by_workload.items()
        }
        candidate_breadth: dict[str, int] = {}
        for selectable in selectable_by_workload.values():
            for candidate in selectable:
                model_id = str(candidate["model_id"])
                candidate_breadth[model_id] = candidate_breadth.get(model_id, 0) + 1
        options: dict[str, tuple[str, ...]] = {}
        exploitation_best: dict[str, str] = {}
        candidate_by_workload: dict[str, dict[str, Mapping[str, Any]]] = {}
        for workload, row in row_by_workload.items():
            selectable = selectable_by_workload[workload]
            exploitation = max(
                selectable,
                key=lambda candidate: (
                    float(candidate.get("exploitation_score") or 0.0),
                    str(candidate.get("model_id") or ""),
                ),
            )
            exploitation_best[workload] = str(exploitation["model_id"])
            shared = max(
                (
                    candidate
                    for candidate in selectable
                    if candidate_breadth[str(candidate["model_id"])] > 1
                ),
                key=lambda candidate: (
                    candidate_breadth[str(candidate["model_id"])],
                    float(candidate.get("score") or 0.0),
                    str(candidate["model_id"]),
                ),
                default=None,
            )
            # Keep the search bound but reserve representation for exploitation and the broadest
            # cross-workload model. Without this diversity slot, four narrowly higher-ranked
            # specialists can hide the only shared portfolio that fits the fleet.
            bounded: list[Mapping[str, Any]] = []
            for candidate in (exploitation, shared, *selectable):
                if candidate is None or candidate in bounded:
                    continue
                bounded.append(candidate)
                if len(bounded) >= _MAX_JOINT_PORTFOLIO_CANDIDATES:
                    break
            options[workload] = tuple(str(candidate["model_id"]) for candidate in bounded)
            candidate_by_workload[workload] = {
                str(candidate["model_id"]): candidate for candidate in selectable
            }

        candidate_models = sorted(
            {
                model_id
                for model_ids in options.values()
                for model_id in model_ids
            }
        )
        nominal_slot_budget = sum(
            node.max_models if node.max_models is not None else len(candidate_models)
            for node in nodes
        )
        slot_budget = min(
            len(candidate_models),
            8,
            nominal_slot_budget,
        )
        workload_catalog_size = sum(
            profile.max_replicas > 0 and bool(profile.workload_scores)
            for profile in profiles
        )
        # Scarcity is a fleet/catalog property, not a transient active-row property. Using only
        # today's mature workloads starts in the roomy objective, fills every slot with early
        # traffic, then discovers scarcity too late to avoid residency lock-in. Likewise, a short
        # node outage must not flip an otherwise roomy fleet into a different portfolio policy.
        scarce_portfolio = nominal_slot_budget < workload_catalog_size

        # Begin from the exploitation-only portfolio. Coordinate descent may then spend the single
        # exploration slot where its optimism buys the most workload utility without ever entering
        # an invalid multi-experiment state that one-at-a-time moves cannot escape.
        selection = dict(exploitation_best)
        profile_by_id = {profile.model_id: profile for profile in profiles}
        # These plans rank counterfactual portfolios only. They are never reconciled or sent to a
        # node, so executable-plan identity hashing would serialize the same fleet dozens of times
        # without adding fencing safety. The authoritative plan built by `_tick_locked` retains its
        # normal digest and generation.
        baseline = self.planner.plan(
            nodes,
            profiles,
            direct,
            now=now,
            compute_input_digest=False,
        )
        baseline_targets = dict(baseline.desired_replicas)
        direct_by_model = {forecast.model_id: forecast for forecast in direct}

        def direct_pressure(forecast: DemandForecast | None) -> bool:
            if forecast is None:
                return False
            observed_rate = forecast.observed_requests_per_minute
            if not observed_rate and not forecast.correlation_sources:
                observed_rate = forecast.requests_per_minute
            return bool(
                observed_rate > 0
                or forecast.queue_depth > 0
                or forecast.p95_latency_ms > 0
                or forecast.error_rate > 0
                or (
                    forecast.offered_concurrency > 0
                    and not forecast.correlation_sources
                )
            )

        baseline_floor_targets = {
            model_id: min(
                target,
                max(
                    profile_by_id[model_id].min_replicas,
                    len(profile_by_id[model_id].pinned_nodes),
                    int(direct_pressure(direct_by_model.get(model_id))),
                ),
            )
            for model_id, target in baseline_targets.items()
            if target > 0 and model_id in profile_by_id
        }
        horizon_planner = PlacementPlanner(
            replace(self.planner.policy, preserve_recent_residencies=False)
        )
        evaluation_cache: dict[tuple[tuple[str, str], ...], tuple[float, ...]] = {}

        def metric(candidate_selection: Mapping[str, str]) -> tuple[float, ...]:
            key = tuple(sorted(candidate_selection.items()))
            cached = evaluation_cache.get(key)
            if cached is not None:
                return cached
            exploration_models = {
                model_id
                for workload, model_id in candidate_selection.items()
                if model_id
                and model_id != exploitation_best[workload]
                and (
                    not scarce_portfolio
                    or (
                        float(
                            candidate_by_workload[workload][model_id]
                            .get("evidence", {})
                            .get("effective_requests")
                            or 0.0
                        )
                        - float(
                            candidate_by_workload[workload][model_id]
                            .get("evidence", {})
                            .get("effective_errors")
                            or 0.0
                        )
                        < 0.5
                    )
                )
            }
            if len(exploration_models) > _MAX_JOINT_EXPLORATION_MODELS:
                result = (float("-inf"),)
                evaluation_cache[key] = result
                return result
            preemption_only_models = {
                model_id
                for workload, model_id in candidate_selection.items()
                if model_id
                and (
                    candidate_by_workload[workload][model_id]
                    .get("placement", {})
                    .get("feasible_after_preemption")
                    is True
                    and candidate_by_workload[workload][model_id]
                    .get("placement", {})
                    .get("feasible_now")
                    is not True
                )
            }
            if len(preemption_only_models) > _MAX_JOINT_PREEMPTION_MODELS:
                result = (float("-inf"),)
                evaluation_cache[key] = result
                return result
            forecasts = intelligence.portfolio_forecasts(
                profiles,
                direct,
                now=now,
                placement_hints=placement_hints,
                chosen_models=candidate_selection,
                workload_forecasts=workload_forecasts,
            )
            plan = self.planner.plan(
                nodes,
                profiles,
                forecasts,
                now=now,
                compute_input_digest=False,
            )
            # Current hysteresis is authoritative for the mutations issued on this tick, but it
            # must not make a better stable portfolio invisible. Evaluate the same forecast after
            # recent-residency holds expire; the live planner will reach that target safely over
            # subsequent ticks instead of the selector repeatedly recommitting to incumbents just
            # because they are incumbents.
            horizon_plan = (
                horizon_planner.plan(
                    nodes,
                    profiles,
                    forecasts,
                    now=now,
                    compute_input_digest=False,
                )
                if scarce_portfolio
                else plan
            )
            placed: dict[str, int] = {}
            for assignment in plan.assignments:
                placed[assignment.model_id] = placed.get(assignment.model_id, 0) + 1
            horizon_placed: dict[str, int] = {}
            for assignment in horizon_plan.assignments:
                horizon_placed[assignment.model_id] = (
                    horizon_placed.get(assignment.model_id, 0) + 1
                )
            horizon_authorized_preemption_models = {
                item.for_model_id
                for item in horizon_plan.preemptions
                if item.for_model_id
            }
            horizon_service_replicas = dict(horizon_placed)
            horizon_staged_replicas: Counter[str] = Counter()
            for node_id, model_id in {
                (item.node_id, item.for_model_id)
                for item in horizon_plan.preemptions
                if item.for_model_id
            }:
                if not any(
                    assignment.node_id == node_id
                    and assignment.model_id == model_id
                    for assignment in horizon_plan.assignments
                ):
                    horizon_staged_replicas[model_id] += 1
                    horizon_service_replicas[model_id] = (
                        horizon_service_replicas.get(model_id, 0) + 1
                    )
            # A placement hint proves only that some structurally safe transition exists. The
            # evaluated full-fleet plan is authoritative about whether that transition remains
            # safe alongside every other selected workload. Never credit a preemption-only model
            # unless this exact plan places it or explicitly stages a victim for it.
            if any(
                horizon_placed.get(model_id, 0) == 0
                and model_id not in horizon_authorized_preemption_models
                for model_id in preemption_only_models
            ):
                result = (float("-inf"),)
                evaluation_cache[key] = result
                return result
            baseline_target_coverage = sum(
                min(placed.get(model_id, 0), target)
                * (1.0 + profile_by_id[model_id].priority)
                for model_id, target in baseline_targets.items()
                if target > 0 and model_id in profile_by_id
            )
            horizon_baseline_target_coverage = sum(
                min(horizon_service_replicas.get(model_id, 0), target)
                * (1.0 + profile_by_id[model_id].priority)
                for model_id, target in baseline_targets.items()
                if target > 0 and model_id in profile_by_id
            )
            baseline_floor_coverage = sum(
                min(placed.get(model_id, 0), target)
                * (1.0 + profile_by_id[model_id].priority)
                for model_id, target in baseline_floor_targets.items()
                if target > 0
            )
            horizon_baseline_floor_coverage = sum(
                min(horizon_service_replicas.get(model_id, 0), target)
                * (1.0 + profile_by_id[model_id].priority)
                for model_id, target in baseline_floor_targets.items()
                if target > 0
            )
            pressure_coverage = 0.0
            request_coverage = 0.0
            current_pressure_coverage = 0.0
            workload_coverage = 0.0
            failing_workload_coverage = 0.0
            utility = 0.0
            for workload, model_id in candidate_selection.items():
                if not model_id:
                    continue
                row = row_by_workload[workload]
                confidence_weight = (
                    0.5 + 0.5 * float(row.get("confidence") or 0.0)
                )
                # Allocation pressure is service-time aware. Requests per minute alone makes one
                # long image/video job look cheaper than a short embedding call even when it holds
                # a device orders of magnitude longer. Offered concurrency is Little's-law load
                # plus observed queue depth, so use it for the primary capacity objective while
                # retaining request coverage as the next tie-break.
                pressure_weight = max(
                    1e-6,
                    float(row.get("offered_concurrency") or 0.0),
                ) * confidence_weight
                request_weight = max(
                    1e-6,
                    float(row.get("requests_per_minute") or 0.0),
                ) * confidence_weight
                desired = max(1, horizon_plan.target_for(model_id))
                ratio = min(
                    1.0,
                    horizon_service_replicas.get(model_id, 0) / desired,
                )
                current_desired = max(1, plan.target_for(model_id))
                current_ratio = min(
                    1.0,
                    placed.get(model_id, 0) / current_desired,
                )
                # Admission comes before throughput optimization. In a scarce fleet, a broad
                # already-resident model may cover several user needs well enough and free the
                # last accelerator for a workload that otherwise receives zero service. Counting
                # represented workloads here lets the joint search discover that consolidation;
                # pressure and request volume still rank equally broad portfolios below.
                workload_coverage += float(
                    horizon_service_replicas.get(model_id, 0) > 0
                )
                # Among portfolios that admit the same number of distinct workloads, restore
                # service to demand that is measurably failing before optimizing aggregate
                # throughput. This is a bounded-window signal, not durable service debt: hard
                # placement constraints and the normal residency governor remain authoritative.
                failing_workload_coverage += (
                    float(row.get("error_rate") or 0.0) * confidence_weight * ratio
                )
                pressure_coverage += pressure_weight * ratio
                request_coverage += request_weight * ratio
                current_pressure_coverage += pressure_weight * current_ratio
                utility += pressure_weight * ratio * float(
                    candidate_by_workload[workload][model_id].get("score") or 0.0
                )
            missing = sum(item.missing_replicas for item in plan.unsatisfied)
            current_service_pairs = {
                (node.node_id, residency.model_id)
                for node in nodes
                for residency in node.residencies
                if residency.state == ResidencyState.READY
                and residency.model_id in profile_by_id
                and profile_by_id[residency.model_id].matches_artifact(residency)
            }
            horizon_service_pairs = {
                (item.node_id, item.model_id) for item in horizon_plan.assignments
            }
            horizon_service_pairs.update(
                (item.node_id, item.for_model_id)
                for item in horizon_plan.preemptions
                if item.for_model_id
            )
            transition_changes = len(
                current_service_pairs.symmetric_difference(horizon_service_pairs)
            )
            failure_band = math.floor(
                failing_workload_coverage
                / _PORTFOLIO_FAILURE_HYSTERESIS_STEP
                + 1e-9
            )
            pressure_band = math.floor(
                pressure_coverage
                / _PORTFOLIO_PRESSURE_HYSTERESIS_STEP
                + 1e-9
            )
            result = (
                (
                    horizon_baseline_floor_coverage,
                    baseline_floor_coverage,
                    workload_coverage,
                    float(failure_band),
                    float(pressure_band),
                    -float(transition_changes),
                    failing_workload_coverage,
                    pressure_coverage,
                    request_coverage,
                    -float(sum(horizon_service_replicas.values())),
                    current_pressure_coverage,
                    horizon_baseline_target_coverage,
                    baseline_target_coverage,
                    -float(
                        sum(
                            max(
                                0,
                                item.missing_replicas
                                - horizon_staged_replicas[item.model_id],
                            )
                            for item in horizon_plan.unsatisfied
                        )
                    ),
                    -float(missing),
                    utility,
                )
                if scarce_portfolio
                else (
                    baseline_target_coverage,
                    pressure_coverage,
                    request_coverage,
                    -float(missing),
                    utility,
                )
            )
            evaluation_cache[key] = result
            return result

        # Heaviest and best-observed workloads choose first. Use offered concurrency before raw
        # request count so a slow image/video job receives its true device-time weight rather than
        # losing the bounded search budget to many cheap embedding calls. Each trial still evaluates
        # the full current mapping, so an early specialist sees every later workload's capacity.
        order = sorted(
            row_by_workload,
            key=lambda workload: (
                -float(row_by_workload[workload].get("offered_concurrency") or 0.0),
                -float(row_by_workload[workload].get("requests_per_minute") or 0.0),
                -float(row_by_workload[workload].get("confidence") or 0.0),
                workload,
            ),
        )
        best_metric = metric(selection)
        def subset_selection(model_subset: frozenset[str]) -> dict[str, str]:
            result: dict[str, str] = {}
            for workload, candidates in candidate_by_workload.items():
                admitted = [
                    candidate
                    for model_id, candidate in candidates.items()
                    if model_id in model_subset
                ]
                if not admitted:
                    # An explicit empty choice tells portfolio projection to defer this workload;
                    # absence would fall back to its independent argmax and recreate an unbounded
                    # desired portfolio.
                    result[workload] = ""
                    continue
                chosen = max(
                    admitted,
                    key=lambda candidate: (
                        float(candidate.get("exploitation_score") or 0.0),
                        float(candidate.get("score") or 0.0),
                        str(candidate.get("model_id") or ""),
                    ),
                )
                result[workload] = str(chosen["model_id"])
            return result

        def cheap_subset_metric(model_subset: frozenset[str]) -> tuple[float, ...]:
            covered = 0.0
            failing = 0.0
            pressure = 0.0
            requests = 0.0
            utility = 0.0
            for workload, candidates in candidate_by_workload.items():
                admitted = [
                    candidate
                    for model_id, candidate in candidates.items()
                    if model_id in model_subset
                ]
                if not admitted:
                    continue
                row = row_by_workload[workload]
                confidence = 0.5 + 0.5 * float(row.get("confidence") or 0.0)
                covered += 1.0
                failing += confidence * float(row.get("error_rate") or 0.0)
                pressure += confidence * float(row.get("offered_concurrency") or 0.0)
                requests += confidence * float(row.get("requests_per_minute") or 0.0)
                utility += max(float(item.get("score") or 0.0) for item in admitted)
            marginal_models = sum(
                model_id not in baseline_targets or baseline_targets.get(model_id, 0) <= 0
                for model_id in model_subset
            )
            return (
                covered,
                failing,
                pressure,
                requests,
                -float(marginal_models),
                utility,
            )

        # On a small candidate catalog, explicitly search admitted model sets. This supplies the
        # missing M1 decision: workloads with no selected resident model are deliberately deferred
        # instead of all generating desired replicas and leaving accidental model-ordering to pick
        # the losers. Larger catalogs use the same bounded top-64 set-cover candidates.
        subsets: list[frozenset[str]] = []
        if slot_budget < len(candidate_models):
            beam = [frozenset()]
            discovered: set[frozenset[str]] = set()
            for _size in range(1, slot_budget + 1):
                expanded = {
                    frozenset((*model_subset, model_id))
                    for model_subset in beam
                    for model_id in candidate_models
                    if model_id not in model_subset
                }
                beam = sorted(
                    expanded,
                    key=lambda model_subset: (
                        cheap_subset_metric(model_subset),
                        tuple(sorted(model_subset)),
                    ),
                    reverse=True,
                )[:_MAX_JOINT_PORTFOLIO_EVALUATIONS]
                discovered.update(beam)
            subsets = list(discovered)
        subsets.sort(
            key=lambda model_subset: (
                cheap_subset_metric(model_subset),
                tuple(sorted(model_subset)),
            ),
            reverse=True,
        )
        for model_subset in subsets:
            if len(evaluation_cache) >= _MAX_JOINT_PORTFOLIO_EVALUATIONS:
                break
            trial = subset_selection(model_subset)
            trial_metric = metric(trial)
            if trial_metric > best_metric:
                selection = trial
                best_metric = trial_metric
        shared_models = sorted(
            {
                model_id
                for model_ids in options.values()
                for model_id in model_ids
                if sum(model_id in choices for choices in options.values()) > 1
            }
        )
        if not subsets:
            # When every candidate model can fit, the cheaper shared-model seeds retain the
            # original bounded coordinate search. Scarce fleets use the explicit set-cover search
            # above, which already crosses multi-move local optima.
            for model_id in shared_models:
                if len(evaluation_cache) >= _MAX_JOINT_PORTFOLIO_EVALUATIONS:
                    break
                trial = dict(selection)
                for workload, model_ids in options.items():
                    if model_id in model_ids:
                        trial[workload] = model_id
                trial_metric = metric(trial)
                if trial_metric > best_metric:
                    selection = trial
                    best_metric = trial_metric
        for _pass in range(2):
            changed = False
            for workload in order:
                best_model = selection[workload]
                best_metric = metric(selection)
                for model_id in options[workload]:
                    if len(evaluation_cache) >= _MAX_JOINT_PORTFOLIO_EVALUATIONS:
                        break
                    trial = dict(selection)
                    trial[workload] = model_id
                    trial_metric = metric(trial)
                    if trial_metric > best_metric:
                        best_model = model_id
                        best_metric = trial_metric
                if best_model != selection[workload]:
                    selection[workload] = best_model
                    changed = True
            if not changed or len(evaluation_cache) >= _MAX_JOINT_PORTFOLIO_EVALUATIONS:
                break
        return dict(sorted(selection.items()))

    def _learned_warm_estimates(
        self,
        *,
        now: float,
    ) -> tuple[
        dict[tuple[str, str], float],
        dict[tuple[str, str], int],
    ]:
        """Blend bounded successful warm timings with each model's configured prior."""

        return self._learned_action_estimates(
            ActionKind.WARM,
            prior_field="warm_seconds",
            now=now,
        )

    def _learned_load_estimates(
        self,
        *,
        now: float,
    ) -> tuple[
        dict[tuple[str, str], float],
        dict[tuple[str, str], int],
    ]:
        """Blend real artifact-transfer timings without learning cache verification as a fetch."""

        return self._learned_action_estimates(
            ActionKind.LOAD,
            prior_field="load_seconds",
            now=now,
            require_artifact_fetch=True,
        )

    def _learned_action_estimates(
        self,
        kind: ActionKind,
        *,
        prior_field: str,
        now: float,
        require_artifact_fetch: bool = False,
    ) -> tuple[
        dict[tuple[str, str], float],
        dict[tuple[str, str], int],
    ]:
        """Return bounded per-host timings tied to the current immutable artifact revision."""

        samples: dict[tuple[str, str], list[float]] = {}
        for record in self._history:
            profile = self._profiles.get(record.model_id)
            if (
                record.kind != kind
                or record.status != MutationStatus.SUCCEEDED
                or record.duration_seconds <= 0
                or profile is None
                or (
                    require_artifact_fetch
                    and (not profile.artifact_source or not record.artifact_fetched)
                )
                or record.artifact_sha256
                != profile.artifact_sha256
                or record.completed_at > now
                or now - record.completed_at >= _STARTUP_ESTIMATE_TTL_SECONDS
            ):
                continue
            key = (record.node_id, record.model_id)
            values = samples.setdefault(key, [])
            values.append(record.duration_seconds)
            if len(values) > _STARTUP_ESTIMATE_SAMPLES:
                del values[0]

        estimates: dict[tuple[str, str], float] = {}
        counts: dict[tuple[str, str], int] = {}
        for key, values in samples.items():
            observed = values[0]
            for value in values[1:]:
                observed += _STARTUP_ESTIMATE_EWMA_ALPHA * (value - observed)
            confidence = min(
                1.0,
                len(values) / _STARTUP_ESTIMATE_FULL_CONFIDENCE_SAMPLES,
            )
            prior = float(getattr(self._profiles[key[1]], prior_field))
            estimates[key] = (1.0 - confidence) * prior + confidence * observed
            counts[key] = len(values)
        return estimates, counts

    def _refresh_observable_models_locked(self) -> None:
        """Publish the configured non-retiring demand keys without blocking inference on plans."""

        self._observable_models = frozenset(self._profiles).difference(self._retiring)
        self._observable_artifacts = {
            model_id: self._profiles[model_id].artifact_sha256
            for model_id in self._observable_models
        }

    def _prune_unobservable_demand(self) -> None:
        """Drop telemetry keys that cannot influence any configured active profile."""

        with self._demand_lock:
            for model_id in self.demand.model_ids():
                if model_id not in self._observable_models:
                    self.demand.clear(model_id)

    def _version_plan(self, plan: PlacementPlan) -> PlacementPlan:
        if (
            self._last_plan_generation
            and plan.input_digest == self._last_plan_input_digest
        ):
            generation = self._last_plan_generation
        else:
            self._plan_sequence += 1
            generation = (
                f"{self._controller_epoch}:{self._plan_sequence:020d}:"
                f"{plan.input_digest[:12]}"
            )
            self._last_plan_input_digest = plan.input_digest
            self._last_plan_generation = generation
        return PlacementPlan(
            generation=generation,
            created_at=plan.created_at,
            assignments=plan.assignments,
            desired_replicas=plan.desired_replicas,
            unsatisfied=plan.unsatisfied,
            objective_score=plan.objective_score,
            input_digest=plan.input_digest,
            preemptions=plan.preemptions,
            artifact_prefetches=plan.artifact_prefetches,
            model_urgencies=plan.model_urgencies,
            artifact_evictions=plan.artifact_evictions,
        )

    def _sequence_actions(self, result: ReconcileResult) -> ReconcileResult:
        """Give every executable attempt a durable, never-reused controller identity.

        Terminal history is intentionally bounded, while node receipt caches outlive individual
        records. Chaining IDs from retained history can therefore reuse an evicted command ID and
        make the node replay an old success. A persisted controller-wide sequence is O(1), survives
        restart, and changes only for actions that are actually offered for execution.
        """

        if not result.actions:
            return result
        id_map: dict[str, str] = {}
        sequenced: list[MutationAction] = []
        for action in result.actions:
            self._action_sequence += 1
            action_id = MutationAction.stable_id(
                action.kind,
                action.node_id,
                action.model_id,
                (
                    f"controller-attempt-v1:{self._controller_epoch}:"
                    f"{self._action_sequence:020d}"
                ),
            )
            id_map[action.action_id] = action_id
            sequenced.append(
                replace(
                    action,
                    action_id=action_id,
                    controller_term=self._controller_term,
                    controller_id=self._controller_id,
                    controller_lease_expires_at=self._controller_lease_expires_at,
                )
            )
        sequenced = [
            replace(
                action,
                dependencies=tuple(id_map.get(item, item) for item in action.dependencies),
            )
            for action in sequenced
        ]
        return ReconcileResult(
            result.plan_generation,
            result.mode,
            tuple(sequenced),
            result.deferred,
        )

    def _failure_streak(self, kind: ActionKind, node_id: str, model_id: str) -> int:
        return self._failure_streaks.get((kind, node_id, model_id), 0)

    def _record_failure_outcome(
        self,
        kind: ActionKind,
        node_id: str,
        model_id: str,
        status: MutationStatus,
        now: float,
    ) -> int:
        key = (kind, node_id, model_id)
        if status == MutationStatus.FAILED:
            failures = self._failure_streaks.get(key, 0) + 1
            self._failure_streaks[key] = failures
            delay = _failure_backoff_seconds(
                self.reconciler.policy,
                failures,
            )
            self._mutation_blocks[key] = now + delay
            self._mutation_block_delays[key] = delay
            self._mutation_block_causes[key] = MutationStatus.FAILED
            return failures
        if status == MutationStatus.SUCCEEDED:
            self._failure_streaks.pop(key, None)
            self._mutation_blocks[key] = (
                now + self.reconciler.policy.success_observation_timeout_seconds
            )
            self._mutation_block_delays[key] = (
                self.reconciler.policy.success_observation_timeout_seconds
            )
            self._mutation_block_causes[key] = MutationStatus.SUCCEEDED
            return 0
        if status == MutationStatus.CANCELLED:
            prior_deadline = self._mutation_blocks.get(key, 0.0)
            prior_delay = self._mutation_block_delays.get(key, 0.0)
            cancelled_until = now + self.reconciler.policy.mutation_cooldown_seconds
            self._mutation_blocks[key] = max(prior_deadline, cancelled_until)
            self._mutation_block_delays[key] = max(
                prior_delay,
                self.reconciler.policy.mutation_cooldown_seconds,
            )
            if cancelled_until >= prior_deadline:
                self._mutation_block_causes[key] = MutationStatus.CANCELLED
        return self._failure_streaks.get(key, 0)

    def _resolve_delivered_action(self, action_id: str) -> None:
        self._delivered_command_ids.discard(action_id)
        self._withdrawn_destructive.pop(action_id, None)

    def _resolve_withdrawn_destructive(
        self,
        nodes: tuple[NodeSnapshot, ...],
    ) -> None:
        """Release destructive uncertainty only after the node state makes late delivery harmless."""

        node_by_id = {node.node_id: node for node in nodes}
        for action_id, action in list(self._withdrawn_destructive.items()):
            node = node_by_id.get(action.node_id)
            if node is None:
                continue
            residency = node.residency(action.model_id)
            if action.kind == ActionKind.DRAIN:
                # DRAIN's durable postcondition is DRAINING. CACHED/absent is even stronger
                # evidence that no serving replica remains. LOADING, WARMING, READY, and FAILED
                # do not settle the race: a late DRAIN can still disrupt a later readmission.
                resolved = residency is None or residency.state in (
                    ResidencyState.CACHED,
                    ResidencyState.DRAINING,
                )
            elif action.kind == ActionKind.UNLOAD:
                # UNLOAD's postcondition is a cached artifact without a live residency.
                # In particular READY is not resolution: a late, previously delivered unload
                # could race a newer warm and remove the newly admitted replica.
                resolved = residency is None or residency.state == ResidencyState.CACHED
            else:
                # EVICT removes both the managed cache residency and its exact artifact. A CACHED
                # heartbeat therefore proves the old command has not reached its postcondition.
                resolved = residency is None
            if resolved:
                self._resolve_delivered_action(action_id)

    def _resolve_revalidated_withdrawn_destructive(
        self,
        plan: PlacementPlan,
        nodes: tuple[NodeSnapshot, ...],
        profiles: tuple[ModelProfile, ...],
        *,
        now: float,
    ) -> None:
        """Release uncertainty when the same destructive outcome is safe and desired again.

        A withdrawn command remains dangerous while its pair has been readmitted or a current
        reconciler guard (minimum residency, replacement readiness, active requests, diversity)
        would reject it. Once the pair is absent from the new plan *and* those guards accept the
        original command, late delivery and a newly issued command have the same postcondition.
        Keeping the uncertainty fence at that point deadlocks scale-down forever because READY can
        never satisfy DRAIN's old postcondition without receiving the command the fence blocks.
        """

        if not self._withdrawn_destructive:
            return
        deferred = self.reconciler.destructive_command_deferrals(
            plan,
            nodes,
            profiles,
            self._withdrawn_destructive.values(),
            now=now,
        )
        desired = plan.desired_pairs
        for action_id, action in list(self._withdrawn_destructive.items()):
            if (action.node_id, action.model_id) in desired:
                continue
            if action_id not in deferred:
                self._resolve_delivered_action(action_id)

    def _cancel_stale_commands(
        self,
        plan: PlacementPlan,
        now: float,
        nodes: tuple[NodeSnapshot, ...],
        profiles: tuple[ModelProfile, ...],
    ) -> set[str]:
        live_node_ids = {node.node_id for node in nodes}
        desired = plan.desired_pairs
        assignments = {
            (assignment.node_id, assignment.model_id): assignment
            for assignment in plan.assignments
        }
        destructive_deferrals = self.reconciler.destructive_command_deferrals(
            plan,
            nodes,
            profiles,
            self._commands.values(),
            now=now,
        )
        prior_eviction_group_by_pair = {
            (item.node_id, item.model_id): (item.node_id, item.for_model_id)
            for item in (
                self._last_plan.artifact_evictions
                if self._last_plan is not None
                else ()
            )
            if item.for_model_id
        }
        stale_messages: dict[str, str] = {}
        for action_id, action in list(self._commands.items()):
            if action_id in self._restored_command_ids:
                if action.node_id not in live_node_ids:
                    recovery_started = self._membership_recovery_started_at
                    if (
                        recovery_started is not None
                        and now < recovery_started + self.membership_recovery_grace_seconds
                    ):
                        continue
                self._restored_command_ids.discard(action_id)

            pair = (action.node_id, action.model_id)
            if action.node_id not in live_node_ids:
                stale = True
                message = "target node is not live"
            else:
                stale = (
                    action.kind in (ActionKind.LOAD, ActionKind.WARM) and pair not in desired
                ) or (
                    action.kind in _DESTRUCTIVE_ACTION_KINDS and pair in desired
                )
                message = "desired placement changed before execution"
                if stale and action.kind in (ActionKind.LOAD, ActionKind.WARM):
                    desired_nodes = sorted(
                        assignment.node_id
                        for assignment in plan.assignments
                        if assignment.model_id == action.model_id
                    )
                    residency = next(
                        (
                            node.residency(action.model_id)
                            for node in nodes
                            if node.node_id == action.node_id
                        ),
                        None,
                    )
                    message += (
                        f" (desired nodes: {desired_nodes or ['none']}; "
                        f"observed state: {residency.state.value if residency else 'absent'}; "
                        f"delivered: {action_id in self._delivered_command_ids})"
                    )
                if (
                    not stale
                    and action.kind in (ActionKind.LOAD, ActionKind.WARM)
                    and assignments[pair].memory_mb != action.memory_mb
                ):
                    stale = True
                    message = "model profile changed before execution"
                destructive_deferral = destructive_deferrals.get(action_id)
                if not stale and destructive_deferral is not None:
                    stale = True
                    message = (
                        "destructive safety changed before execution: "
                        f"{destructive_deferral.message}"
                    )
            if stale:
                stale_messages[action_id] = message

        # PENDING only means the controller has not received a RUNNING receipt; the command may
        # already be executing on the node. If any member of one model's destructive batch becomes
        # unsafe, withdraw the whole batch. Retaining a nominally safe subset could combine with a
        # late success from a cancelled member and cross the replacement or diversity floor.
        unsafe_destructive_models = {
            action.model_id
            for action_id, action in self._commands.items()
            if action_id in stale_messages
            and action.kind in _DESTRUCTIVE_ACTION_KINDS
        }
        unsafe_destructive_models.update(
            action.model_id for action in self._withdrawn_destructive.values()
        )
        unsafe_replacement_groups = {
            prior_eviction_group_by_pair[(action.node_id, action.model_id)]
            for action_id, action in self._commands.items()
            if action_id in stale_messages
            and action.kind == ActionKind.EVICT
            and (action.node_id, action.model_id) in prior_eviction_group_by_pair
        }
        for action_id, action in self._commands.items():
            replacement_group = prior_eviction_group_by_pair.get(
                (action.node_id, action.model_id)
            )
            if (
                (
                    action.kind not in _DESTRUCTIVE_ACTION_KINDS
                    or action.model_id not in unsafe_destructive_models
                )
                and replacement_group not in unsafe_replacement_groups
            ):
                continue
            unsafe_destructive_models.add(action.model_id)
            stale_messages.setdefault(
                action_id,
                (
                    "another predictive-cache eviction in the same replacement "
                    "group became unsafe"
                    if replacement_group in unsafe_replacement_groups
                    else "another command in the same destructive batch became unsafe"
                ),
            )

        for action_id, message in stale_messages.items():
            action = self._commands.get(action_id)
            if action is not None:
                self._cancel_command(action, now, message)
        if not self._restored_command_ids:
            self._membership_recovery_started_at = None
        return unsafe_destructive_models

    def _reprioritize_undelivered_constructive(
        self,
        plan: PlacementPlan,
        nodes: tuple[NodeSnapshot, ...],
        profiles: tuple[ModelProfile, ...],
        *,
        now: float,
    ) -> None:
        """Free scarce mutation slots for strictly more important service.

        A PENDING receipt does not prove whether a polled command has started, so only commands
        that have never been delivered are eligible. Equal service classes retain FIFO stability;
        reprioritization is reserved for a higher administrator priority or demand-urgency tier.
        """

        if not self._commands:
            return
        node_by_id = {node.node_id: node for node in nodes}
        profile_by_id = {profile.model_id: profile for profile in profiles}
        urgency_by_model = dict(plan.model_urgencies)

        def service_rank(model_id: str) -> tuple[int, int]:
            profile = profile_by_id.get(model_id)
            return (
                profile.priority if profile is not None else 0,
                urgency_by_model.get(model_id, 0),
            )

        constructive = (ActionKind.LOAD, ActionKind.WARM)
        active_pairs = {
            (action.node_id, action.model_id)
            for action in self._commands.values()
            if action.kind in constructive
        }
        active_by_node: dict[str, int] = {}
        for action in self._commands.values():
            active_by_node[action.node_id] = active_by_node.get(action.node_id, 0) + 1
        active_count = len(self._commands)
        action_by_id = dict(self._commands)
        dependants_by_id: dict[str, list[str]] = {}
        for action in self._commands.values():
            for dependency in action.dependencies:
                dependants_by_id.setdefault(dependency, []).append(action.action_id)

        candidate_heaps: dict[str, list[tuple[int, int, int, float, str]]] = {
            "": []
        }
        for action in self._commands.values():
            if (
                action.kind not in constructive
                or action.action_id in self._delivered_command_ids
            ):
                continue
            rank = service_rank(action.model_id)
            candidate = (
                rank[0],
                rank[1],
                len(dependants_by_id.get(action.action_id, ())),
                action.created_at,
                action.action_id,
            )
            candidate_heaps[""].append(candidate)
            candidate_heaps.setdefault(action.node_id, []).append(candidate)
        for candidates in candidate_heaps.values():
            heapq.heapify(candidates)

        def lowest_candidate(
            node_id: str,
            waiting_rank: tuple[int, int],
        ) -> MutationAction | None:
            candidates = candidate_heaps.get(node_id, [])
            while candidates:
                priority, urgency, _dependants, _created_at, action_id = candidates[0]
                action = self._commands.get(action_id)
                if action is None or action_id in self._delivered_command_ids:
                    heapq.heappop(candidates)
                    continue
                if (priority, urgency) >= waiting_rank:
                    return None
                return action
            return None

        def dependency_closure(action_id: str) -> set[str]:
            removed: set[str] = set()
            pending = [action_id]
            while pending:
                candidate_id = pending.pop()
                if candidate_id in removed or candidate_id not in self._commands:
                    continue
                removed.add(candidate_id)
                pending.extend(dependants_by_id.get(candidate_id, ()))
            return removed

        waiting: list[tuple[tuple[int, int], str, str]] = []
        for assignment in plan.assignments:
            pair = (assignment.node_id, assignment.model_id)
            if pair in active_pairs:
                continue
            node = node_by_id.get(assignment.node_id)
            profile = profile_by_id.get(assignment.model_id)
            residency = node.residency(assignment.model_id) if node is not None else None
            if node is None or profile is None or (
                residency is not None
                and residency.state == ResidencyState.READY
                and profile.matches_artifact(residency)
            ):
                continue
            waiting.append(
                (service_rank(assignment.model_id), assignment.node_id, assignment.model_id)
            )
        waiting.sort(key=lambda item: (-item[0][0], -item[0][1], item[1], item[2]))

        cancelled_any = False
        for waiting_rank, node_id, _model_id in waiting:
            global_full = active_count >= self.reconciler.policy.max_concurrent_mutations
            node_full = (
                active_by_node.get(node_id, 0)
                >= self.reconciler.policy.max_mutations_per_node
            )
            if not global_full and not node_full:
                active_count += 1
                active_by_node[node_id] = active_by_node.get(node_id, 0) + 1
                continue
            victim = lowest_candidate(node_id if node_full else "", waiting_rank)
            if victim is None:
                continue
            removed_ids = dependency_closure(victim.action_id)
            self._cancel_command(
                victim,
                now,
                "undelivered mutation yielded to higher-priority service",
                dependants_by_id=dependants_by_id,
                trim_history=False,
            )
            cancelled_any = True
            # Cancelling a prerequisite recursively cancels queued dependants.
            active_count -= len(removed_ids)
            for removed_id in removed_ids:
                removed = action_by_id[removed_id]
                active_by_node[removed.node_id] -= 1
            # Reserve the slot for this waiting assignment so one cancellation cannot be credited
            # repeatedly while scanning the remainder of the desired plan.
            active_count += 1
            active_by_node[node_id] = active_by_node.get(node_id, 0) + 1
        if cancelled_any:
            self._trim_history()

    def _cancel_command(
        self,
        action: MutationAction,
        now: float,
        message: str,
        *,
        dependants_by_id: Mapping[str, Iterable[str]] | None = None,
        trim_history: bool = True,
    ) -> None:
        if action.action_id not in self._commands:
            return
        self._append_record(
            MutationRecord(
                action_id=action.action_id,
                kind=action.kind,
                node_id=action.node_id,
                model_id=action.model_id,
                status=MutationStatus.CANCELLED,
                attempted_at=action.created_at,
                completed_at=now,
                failures=self._failure_streak(action.kind, action.node_id, action.model_id),
                message=message,
                artifact_sha256=action.artifact_sha256,
            ),
            trim=trim_history,
        )
        if (
            action.action_id in self._delivered_command_ids
            and action.kind in _DESTRUCTIVE_ACTION_KINDS
        ):
            self._withdrawn_destructive[action.action_id] = action
        self._commands.pop(action.action_id, None)
        if action.action_id not in self._withdrawn_destructive:
            self._delivered_command_ids.discard(action.action_id)
        key = (action.kind, action.node_id, action.model_id)
        prior_deadline = self._mutation_blocks.get(key, 0.0)
        cancelled_until = now + self.reconciler.policy.mutation_cooldown_seconds
        self._mutation_blocks[key] = max(prior_deadline, cancelled_until)
        self._mutation_block_delays[key] = max(
            self._mutation_block_delays.get(key, 0.0),
            self.reconciler.policy.mutation_cooldown_seconds,
        )
        if cancelled_until >= prior_deadline:
            self._mutation_block_causes[key] = MutationStatus.CANCELLED
        if trim_history:
            self._trim_history()
        self._cancel_dependents(
            action.action_id,
            now,
            f"prerequisite {action.action_id} was cancelled",
            dependants_by_id=dependants_by_id,
            trim_history=trim_history,
        )
        self._restored_command_ids.discard(action.action_id)
        if not self._restored_command_ids:
            self._membership_recovery_started_at = None

    def _cancel_dependents(
        self,
        action_id: str,
        now: float,
        message: str,
        *,
        dependants_by_id: Mapping[str, Iterable[str]] | None = None,
        trim_history: bool = True,
    ) -> None:
        if dependants_by_id is None:
            dependents = tuple(
                action.action_id
                for action in self._commands.values()
                if action_id in action.dependencies
            )
        else:
            dependents = tuple(dependants_by_id.get(action_id, ()))
        for dependent_id in dependents:
            dependent = self._commands.get(dependent_id)
            if dependent is not None:
                self._cancel_command(
                    dependent,
                    now,
                    message,
                    dependants_by_id=dependants_by_id,
                    trim_history=trim_history,
                )

    def _cancel_commands_for_model(
        self,
        model_id: str,
        message: str,
        *,
        kinds: tuple[ActionKind, ...] | None = None,
    ) -> None:
        now = time.time()
        for action in list(self._commands.values()):
            if action.model_id != model_id or (kinds is not None and action.kind not in kinds):
                continue
            self._cancel_command(action, now, message)

    def _cancel_all_pending(self, message: str) -> None:
        now = time.time()
        for action in list(self._commands.values()):
            self._cancel_command(action, now, message)

    def _save(self) -> None:
        if self.state_path is None:
            return
        with self._demand_lock:
            demand = self.demand.to_dict()
            intelligence = self.intelligence.to_dict()
        payload = {
            "schema_version": SCHEMA_VERSION,
            "mode": self.mode.value,
            "controller_epoch": self._controller_epoch,
            "controller_term": self._controller_term,
            "controller_id": self._controller_id,
            "controller_lease_expires_at": self._controller_lease_expires_at,
            "plan_sequence": self._plan_sequence,
            "action_sequence": self._action_sequence,
            "last_plan_input_digest": self._last_plan_input_digest,
            "last_plan_generation": self._last_plan_generation,
            "membership_recovery_grace_seconds": self.membership_recovery_grace_seconds,
            "planner_policy": asdict(self.planner.policy),
            "reconcile_policy": asdict(self.reconciler.policy),
            "profiles": [profile.to_dict() for profile in self.profiles],
            "retiring_models": sorted(self._retiring),
            "demand": demand,
            "intelligence": intelligence,
            "history": [_record_dict(item) for item in self._history],
            "commands": [_action_dict(item) for item in self._commands.values()],
            "delivered_command_ids": sorted(self._delivered_command_ids),
            "withdrawn_destructive": [
                _action_dict(item) for item in self._withdrawn_destructive.values()
            ],
            "failure_streaks": [
                {
                    "kind": kind.value,
                    "node_id": node_id,
                    "model_id": model_id,
                    "count": count,
                }
                for (kind, node_id, model_id), count in sorted(
                    self._failure_streaks.items(),
                    key=lambda item: (item[0][0].value, item[0][1], item[0][2]),
                )
            ],
            "mutation_blocks": [
                {
                    "kind": kind.value,
                    "node_id": node_id,
                    "model_id": model_id,
                    "blocked_until": blocked_until,
                    "max_delay": self._mutation_block_delays.get(
                        (kind, node_id, model_id),
                        0.0,
                    ),
                    "cause": (
                        self._mutation_block_causes[(kind, node_id, model_id)].value
                        if (kind, node_id, model_id) in self._mutation_block_causes
                        else None
                    ),
                }
                for (kind, node_id, model_id), blocked_until in sorted(
                    self._mutation_blocks.items(),
                    key=lambda item: (item[0][0].value, item[0][1], item[0][2]),
                )
            ],
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        jsonio.atomic_write_json(self.state_path, payload)

    def _checkpoint(self) -> dict[str, Any]:
        """Capture mutable controller state so a failed durable write cannot leak commands.

        All callers hold ``_lock``. Demand deliberately is not transaction state: request
        completion writes it under a separate lock while planning/fsync may be in progress. A
        command persistence rollback must never erase telemetry that arrived after this checkpoint.
        """

        return {
            "mode": self.mode,
            "profiles": dict(self._profiles),
            "retiring": set(self._retiring),
            "history": list(self._history),
            "commands": dict(self._commands),
            "delivered_command_ids": set(self._delivered_command_ids),
            "withdrawn_destructive": dict(self._withdrawn_destructive),
            "failure_streaks": dict(self._failure_streaks),
            "mutation_blocks": dict(self._mutation_blocks),
            "mutation_block_delays": dict(self._mutation_block_delays),
            "mutation_block_causes": dict(self._mutation_block_causes),
            "controller_term": self._controller_term,
            "controller_id": self._controller_id,
            "controller_lease_expires_at": self._controller_lease_expires_at,
            "plan_sequence": self._plan_sequence,
            "action_sequence": self._action_sequence,
            "last_plan_input_digest": self._last_plan_input_digest,
            "last_plan_generation": self._last_plan_generation,
            "restored_command_ids": set(self._restored_command_ids),
            "membership_recovery_started_at": self._membership_recovery_started_at,
            "last_plan": self._last_plan,
            "last_result": self._last_result,
            "last_tick_at": self._last_tick_at,
        }

    def _save_or_rollback(self, checkpoint: dict[str, Any]) -> None:
        try:
            self._save()
        except jsonio.AtomicWriteCommittedError:
            # os.replace is the persistence linearization point. A later directory-fsync failure
            # means crash durability is uncertain, but rolling memory back would immediately split
            # the live controller from the already-visible target file.
            self._refresh_observable_models_locked()
            raise
        except BaseException:
            self._rollback(checkpoint)
            raise
        self._refresh_observable_models_locked()

    def _rollback(self, checkpoint: dict[str, Any]) -> None:
        """Restore controller-owned transaction state without rewinding request telemetry."""

        self.mode = checkpoint["mode"]
        self._profiles = checkpoint["profiles"]
        self._retiring = checkpoint["retiring"]
        self._history = checkpoint["history"]
        self._commands = checkpoint["commands"]
        self._delivered_command_ids = checkpoint["delivered_command_ids"]
        self._withdrawn_destructive = checkpoint["withdrawn_destructive"]
        self._failure_streaks = checkpoint["failure_streaks"]
        self._mutation_blocks = checkpoint["mutation_blocks"]
        self._mutation_block_delays = checkpoint["mutation_block_delays"]
        self._mutation_block_causes = checkpoint["mutation_block_causes"]
        self._controller_term = checkpoint["controller_term"]
        self._controller_id = checkpoint["controller_id"]
        self._controller_lease_expires_at = checkpoint[
            "controller_lease_expires_at"
        ]
        self._plan_sequence = checkpoint["plan_sequence"]
        self._action_sequence = checkpoint["action_sequence"]
        self._last_plan_input_digest = checkpoint["last_plan_input_digest"]
        self._last_plan_generation = checkpoint["last_plan_generation"]
        self._restored_command_ids = checkpoint["restored_command_ids"]
        self._membership_recovery_started_at = checkpoint[
            "membership_recovery_started_at"
        ]
        self._last_plan = checkpoint["last_plan"]
        self._last_result = checkpoint["last_result"]
        self._last_tick_at = checkpoint["last_tick_at"]
        self._refresh_observable_models_locked()
        self._prune_unobservable_demand()

    def _restore(self, value: dict[str, Any]) -> None:
        if not value:
            return
        if int(value.get("schema_version", 0)) != SCHEMA_VERSION:
            raise ValueError("unsupported allocator controller schema")
        self.mode = AllocatorMode(value.get("mode", AllocatorMode.RECOMMEND))
        persisted_epoch = value.get("controller_epoch")
        if persisted_epoch is not None:
            epoch = str(persisted_epoch)
            if len(epoch) != 32 or any(character not in "0123456789abcdef" for character in epoch):
                raise ValueError("invalid persisted allocator controller epoch")
            self._controller_epoch = epoch
        if "controller_term" in value:
            term = int(value["controller_term"])
            controller_id = str(value.get("controller_id") or "")
            lease_expires_at = float(value.get("controller_lease_expires_at") or 0.0)
            if (
                not 0 < term <= MAX_COUNTER
                or not controller_id
                or len(controller_id) > MAX_ID_LENGTH
                or not math.isfinite(lease_expires_at)
                or lease_expires_at < 0
            ):
                raise ValueError("invalid persisted allocator controller authority")
            self._controller_term = term
            self._controller_id = controller_id
            self._controller_lease_expires_at = lease_expires_at
        sequence = int(value.get("plan_sequence") or 0)
        if sequence < 0:
            raise ValueError("invalid persisted allocator plan sequence")
        self._plan_sequence = sequence
        action_sequence = int(value.get("action_sequence") or 0)
        if action_sequence < 0:
            raise ValueError("invalid persisted allocator action sequence")
        self._action_sequence = action_sequence
        self._last_plan_input_digest = str(value.get("last_plan_input_digest") or "")
        self._last_plan_generation = str(value.get("last_plan_generation") or "")
        if self._last_plan_generation:
            parts = self._last_plan_generation.split(":")
            if (
                len(parts) != 3
                or parts[0] != self._controller_epoch
                or len(parts[1]) != 20
                or not parts[1].isdigit()
                or int(parts[1]) != self._plan_sequence
                or len(parts[2]) != 12
                or any(character not in "0123456789abcdef" for character in parts[2])
                or len(self._last_plan_input_digest) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in self._last_plan_input_digest
                )
                or parts[2] != self._last_plan_input_digest[:12]
            ):
                raise ValueError("invalid persisted allocator plan generation")
        elif self._last_plan_input_digest or self._plan_sequence:
            raise ValueError("persisted allocator plan generation is incomplete")
        if "membership_recovery_grace_seconds" in value:
            grace = float(value["membership_recovery_grace_seconds"])
            if not math.isfinite(grace) or grace < 0:
                raise ValueError("invalid persisted membership recovery grace")
            self.membership_recovery_grace_seconds = grace
        self.planner = PlacementPlanner(PlannerPolicy(**dict(value.get("planner_policy") or {})))
        self.reconciler = Reconciler(
            ReconcilePolicy(**dict(value.get("reconcile_policy") or {}))
        )
        self._profiles = {
            profile.model_id: profile
            for profile in (
                ModelProfile.from_dict(row) for row in value.get("profiles") or ()
            )
        }
        self._retiring = {str(model_id) for model_id in value.get("retiring_models") or ()}
        for model_id in self._retiring:
            profile = self._profiles.get(model_id)
            if (
                profile is None
                or profile.min_replicas != 0
                or profile.max_replicas != 0
                or profile.pinned_nodes
            ):
                raise ValueError("invalid persisted allocator retirement tombstone")
        demand = value.get("demand")
        if demand:
            self.demand = DemandTracker.from_dict(demand)
        intelligence = value.get("intelligence")
        if intelligence:
            self.intelligence = WorkloadIntelligence.from_dict(intelligence)
        self._refresh_observable_models_locked()
        self._prune_unobservable_demand()
        self._history = [_record_from_dict(row) for row in value.get("history") or ()]
        self._commands = {
            action.action_id: action
            for action in (_action_from_dict(row) for row in value.get("commands") or ())
        }
        withdrawn_rows = value.get("withdrawn_destructive") or ()
        self._withdrawn_destructive = {
            action.action_id: action
            for action in (_action_from_dict(row) for row in withdrawn_rows)
        }
        if any(
            action.kind not in _DESTRUCTIVE_ACTION_KINDS
            for action in self._withdrawn_destructive.values()
        ) or set(self._commands).intersection(self._withdrawn_destructive):
            raise ValueError("invalid persisted withdrawn allocator command")
        delivered_rows = value.get("delivered_command_ids")
        self._delivered_command_ids = (
            {str(action_id) for action_id in delivered_rows or ()}
            if delivered_rows is not None
            else set(self._commands)
        )
        known_deliveries = set(self._commands).union(self._withdrawn_destructive)
        if (
            not self._delivered_command_ids.issubset(known_deliveries)
            or not set(self._withdrawn_destructive).issubset(self._delivered_command_ids)
        ):
            raise ValueError("invalid persisted allocator delivery state")
        for action_id in self._withdrawn_destructive:
            latest = self._latest_record(action_id)
            if latest is None or latest.status != MutationStatus.CANCELLED:
                raise ValueError("invalid persisted withdrawn allocator command")
        self._trim_history()
        failure_rows = value.get("failure_streaks")
        if failure_rows is None:
            self._rebuild_failure_streaks()
        else:
            self._failure_streaks = {}
            for row in failure_rows:
                count = int(row.get("count") or 0)
                if count < 0:
                    raise ValueError("invalid persisted allocator failure streak")
                if count:
                    self._failure_streaks[
                        (
                            ActionKind(row["kind"]),
                            str(row["node_id"]),
                            str(row["model_id"]),
                        )
                    ] = count
        block_rows = value.get("mutation_blocks")
        if block_rows is None:
            self._rebuild_mutation_blocks()
        else:
            self._mutation_blocks = {}
            self._mutation_block_delays = {}
            self._mutation_block_causes = {}
            for row in block_rows:
                blocked_until = float(row.get("blocked_until") or 0.0)
                max_delay = float(row.get("max_delay") or 0.0)
                if (
                    not math.isfinite(blocked_until)
                    or blocked_until < 0
                    or not math.isfinite(max_delay)
                    or max_delay < 0
                ):
                    raise ValueError("invalid persisted allocator mutation block")
                if blocked_until:
                    key = (
                        ActionKind(row["kind"]),
                        str(row["node_id"]),
                        str(row["model_id"]),
                    )
                    self._mutation_blocks[key] = blocked_until
                    self._mutation_block_delays[key] = max_delay
                    raw_cause = row.get("cause")
                    if raw_cause is not None:
                        cause = MutationStatus(raw_cause)
                        if cause not in _TERMINAL:
                            raise ValueError("invalid persisted allocator mutation block cause")
                        self._mutation_block_causes[key] = cause
                    else:
                        # Backward compatibility for state written before block provenance was
                        # explicit. Retained history is authoritative when it is still available;
                        # an evicted/unknown cause remains fail-closed and cannot bypass a block.
                        latest = next(
                            (
                                record
                                for record in reversed(self._history)
                                if (record.kind, record.node_id, record.model_id) == key
                                and record.status in _TERMINAL
                            ),
                            None,
                        )
                        if latest is not None:
                            self._mutation_block_causes[key] = latest.status

    def _rebuild_failure_streaks(self) -> None:
        self._failure_streaks = {}
        for record in self._history:
            key = (record.kind, record.node_id, record.model_id)
            if record.status == MutationStatus.FAILED:
                self._failure_streaks[key] = max(
                    self._failure_streaks.get(key, 0) + 1,
                    record.failures,
                )
            elif record.status == MutationStatus.SUCCEEDED:
                self._failure_streaks.pop(key, None)

    def _rebuild_mutation_blocks(self) -> None:
        self._mutation_blocks = {}
        self._mutation_block_delays = {}
        self._mutation_block_causes = {}
        for record in self._history:
            anchor = record.completed_at or record.attempted_at
            key = (record.kind, record.node_id, record.model_id)
            if record.status == MutationStatus.FAILED:
                delay = _failure_backoff_seconds(
                    self.reconciler.policy,
                    record.failures,
                )
                self._mutation_blocks[key] = anchor + delay
                self._mutation_block_delays[key] = delay
                self._mutation_block_causes[key] = MutationStatus.FAILED
            elif record.status == MutationStatus.SUCCEEDED:
                self._mutation_blocks[key] = (
                    anchor + self.reconciler.policy.success_observation_timeout_seconds
                )
                self._mutation_block_delays[key] = (
                    self.reconciler.policy.success_observation_timeout_seconds
                )
                self._mutation_block_causes[key] = MutationStatus.SUCCEEDED
            elif record.status == MutationStatus.CANCELLED:
                prior_deadline = self._mutation_blocks.get(key, 0.0)
                cancelled_until = anchor + self.reconciler.policy.mutation_cooldown_seconds
                self._mutation_blocks[key] = max(prior_deadline, cancelled_until)
                self._mutation_block_delays[key] = max(
                    self._mutation_block_delays.get(key, 0.0),
                    self.reconciler.policy.mutation_cooldown_seconds,
                )
                if cancelled_until >= prior_deadline:
                    self._mutation_block_causes[key] = MutationStatus.CANCELLED


def _failure_backoff_seconds(policy: ReconcilePolicy, failures: int) -> float:
    if failures <= 0 or not policy.failure_backoff_base_seconds:
        return 0.0
    try:
        delay = math.ldexp(policy.failure_backoff_base_seconds, failures - 1)
    except (OverflowError, ValueError):
        return policy.failure_backoff_max_seconds
    if not math.isfinite(delay):
        return policy.failure_backoff_max_seconds
    return min(policy.failure_backoff_max_seconds, delay)


def _spare_canary_hints(
    profiles: Iterable[ModelProfile],
    direct: Iterable[DemandForecast],
    intelligence: WorkloadIntelligence,
    placement_hints: Mapping[str, Mapping[str, Any]],
    nodes: Iterable[NodeSnapshot],
    planner: PlacementPlanner,
    *,
    now: float,
    workload_forecasts: Mapping[str, DemandForecast] | None = None,
) -> dict[str, dict[str, Any]]:
    """Authorize weak media canaries only after proving fleet-level spare capacity.

    Per-model feasibility answers whether a model fits *somewhere*. It cannot prove that the same
    slot is not already needed by stronger demand elsewhere in the portfolio. Plan every ordinary
    workload first, preserve finite slots for one unseen workload and one node failure, and require
    another compatible placement for the canary. A model already selected by ordinary evidence
    may be reused without spending another speculative slot.
    """

    profile_list = tuple(profiles)
    direct_list = tuple(direct)
    node_list = tuple(nodes)
    result = {
        profile.model_id: {
            **dict(placement_hints.get(profile.model_id) or {}),
            "spare_canary_allowed": False,
            "spare_canary_reason": "fleet-level spare capacity not proven",
        }
        for profile in profile_list
    }
    projections = intelligence.projections(
        profile_list,
        now=now,
        placement_hints=result,
        workload_forecasts=workload_forecasts,
    )
    ordinary_rows = tuple(
        row
        for row in projections
        if float(row.get("requests_per_minute") or 0.0) > 0
        and intelligence.portfolio_evidence_ready(
            int(row.get("samples") or 0),
            float(row.get("offered_concurrency") or 0.0),
        )
        and str(row.get("chosen_model") or "")
    )
    ordinary_selection = {
        str(row["workload"]): str(row["chosen_model"]) for row in ordinary_rows
    }
    ordinary_forecasts = intelligence.portfolio_forecasts(
        profile_list,
        direct_list,
        now=now,
        placement_hints=result,
        chosen_models=ordinary_selection,
        workload_forecasts=workload_forecasts,
    )
    ordinary_plan = planner.plan(
        node_list,
        profile_list,
        ordinary_forecasts,
        now=now,
        compute_input_digest=False,
    )
    ordinary_models = {assignment.model_id for assignment in ordinary_plan.assignments}
    for model_id in ordinary_models:
        if model_id in result:
            result[model_id]["spare_canary_allowed"] = True
            result[model_id]["spare_canary_reason"] = (
                "reuses capacity already selected by stronger evidence"
            )

    # Unknown slot limits do not count as hypothetical free capacity. A stale or non-accepting node
    # contributes nothing: its old residencies are observations, not operational reserve.
    finite_slots = 0
    for node in node_list:
        live_slots = sum(
            residency.state != ResidencyState.CACHED
            and not (
                residency.state == ResidencyState.FAILED and not residency.managed
            )
            for residency in node.residencies
        )
        heartbeat_age = now - node.last_heartbeat
        heartbeat_fresh = bool(
            node.last_heartbeat > 0
            and heartbeat_age >= -planner.policy.max_future_clock_skew_seconds
            and (
                not planner.policy.node_ttl_seconds
                or heartbeat_age <= planner.policy.node_ttl_seconds
            )
        )
        if (
            node.state in (NodeState.ACCEPTING, NodeState.THROTTLED)
            and not node.manually_managed
            and heartbeat_fresh
            and node.max_models is not None
        ):
            finite_slots += max(live_slots, node.max_models)
        elif (
            node.state in (NodeState.ACCEPTING, NodeState.THROTTLED)
            and not node.manually_managed
            and heartbeat_fresh
        ):
            finite_slots += live_slots
    spare_slots = max(0, finite_slots - len(ordinary_plan.assignments))
    compatible_catalog_models = sum(
        profile.max_replicas > 0
        and (
            result[profile.model_id].get("hard_compatible") is True
            or result[profile.model_id].get("feasible_now") is True
        )
        for profile in profile_list
    )
    catalog_surplus = max(0, finite_slots - compatible_catalog_models)
    # Keep one slot for an unseen workload class and one for a node failure. Spending either on a
    # weak signal turns exploration into an availability regression on small fleets. Also require
    # surplus beyond one feasible copy of every configured model: when M can already fill N, the
    # allocator has a model-selection problem rather than genuinely spare exploration capacity.
    speculative_budget = max(
        0,
        min(spare_slots, catalog_surplus) - _SPARE_CANARY_RESERVE_SLOTS,
    )
    if ordinary_plan.unsatisfied:
        speculative_budget = 0

    profile_by_id = {profile.model_id: profile for profile in profile_list}
    weak_rows = sorted(
        (
            row
            for row in projections
            if str(row.get("workload") or "") in {"image", "video"}
            and float(row.get("requests_per_minute") or 0.0) > 0
            and not intelligence.portfolio_evidence_ready(
                int(row.get("samples") or 0),
                float(row.get("offered_concurrency") or 0.0),
            )
            and intelligence.portfolio_evidence_ready(
                int(row.get("samples") or 0),
                float(row.get("offered_concurrency") or 0.0),
                immediately_feasible=True,
            )
        ),
        key=lambda row: (
            -float(row.get("offered_concurrency") or 0.0),
            str(row.get("workload") or ""),
        ),
    )
    authorized_new_models: set[str] = set()
    for row in weak_rows:
        model_id = str(row.get("chosen_model") or "")
        profile = profile_by_id.get(model_id)
        hint = result.get(model_id)
        if profile is None or hint is None:
            continue
        successful_evidence = float(row.get("successful_outcome_evidence") or 0.0)
        if successful_evidence >= 0.5:
            hint["spare_canary_allowed"] = True
            hint["spare_canary_reason"] = "a recent successful canary validated the model"
            continue
        if model_id in ordinary_models:
            continue
        if (
            profile.replica_concurrency != 1
            or hint.get("feasible_now") is not True
            or int(hint.get("eligible_nodes") or 0) < 2
            or speculative_budget <= 0
        ):
            continue
        hint["spare_canary_allowed"] = True
        hint["spare_canary_reason"] = (
            "ordinary demand fits with workload, failure, and compatible reserves"
        )
        if model_id not in authorized_new_models:
            speculative_budget -= 1
            authorized_new_models.add(model_id)
    return result


def _portfolio_selection_hints(
    profiles: Iterable[ModelProfile],
    direct: Iterable[DemandForecast],
    intelligence: WorkloadIntelligence,
    placement_hints: Mapping[str, Mapping[str, Any]] | None,
    nodes: Iterable[NodeSnapshot],
    planner: PlacementPlanner,
    *,
    now: float,
    workload_forecasts: Mapping[str, DemandForecast] | None = None,
) -> dict[str, dict[str, Any]]:
    """Fence speculative replacement against demand visible in the same snapshot.

    Fleet feasibility is occupancy-aware but intentionally demand-agnostic. Before portfolio
    selection treats a safe-preemption path as selectable, protect every required baseline and
    directly demanded model. Workload-capable victims retain the demand-derived replica target (and
    at least one viable fallback), but a copy above that floor may be reclaimed for a missing
    workload. The final planner remains authoritative and can reject a path when full-fleet
    interactions reveal another conflict.
    """

    profile_list = tuple(profiles)
    direct_list = tuple(direct)
    node_list = tuple(nodes)
    if placement_hints is None:
        return {
            profile.model_id: {
                "model_id": profile.model_id,
                "feasible": True,
                "feasible_now": True,
                "feasible_after_preemption": False,
                "portfolio_preemption_safe": False,
                "portfolio_preemption_blockers": [],
                "reason": "placement hints unavailable",
            }
            for profile in profile_list
        }
    if not any(
        hint.get("feasible_after_preemption") is True
        for hint in placement_hints.values()
    ):
        return {
            profile.model_id: {
                **dict(placement_hints.get(profile.model_id) or {}),
                "portfolio_preemption_safe": False,
                "portfolio_preemption_blockers": [],
            }
            for profile in profile_list
        }
    projections = intelligence.projections(
        profile_list,
        now=now,
        workload_forecasts=workload_forecasts,
    )
    active_workloads = {
        str(row.get("workload") or "")
        for row in projections
        if intelligence.portfolio_evidence_ready(
            int(row.get("samples") or 0),
            float(row.get("offered_concurrency") or 0.0),
        )
        and float(row.get("requests_per_minute") or 0.0) > 0
    }
    preferred_selection = {
        str(row.get("workload") or ""): str(row.get("chosen_model") or "")
        for row in projections
        if str(row.get("workload") or "") in active_workloads
        and str(row.get("chosen_model") or "")
    }
    projection_by_workload = {
        str(row.get("workload") or ""): row for row in projections
    }

    def workload_rebalance_pressure(workload: str) -> float:
        row = projection_by_workload.get(workload) or {}
        confidence = 0.5 + 0.5 * float(row.get("confidence") or 0.0)
        # Device-time pressure is the primary resource signal. Give current failures bounded
        # urgency so a missing high-cost workload can displace a healthy low-cost speculative
        # residency, but never manufacture authority over direct, pinned, or baseline service.
        failure_multiplier = 1.0 + float(row.get("error_rate") or 0.0)
        return (
            max(1e-6, float(row.get("offered_concurrency") or 0.0))
            * confidence
            * failure_multiplier
        )

    pressure_by_preferred_model: defaultdict[str, float] = defaultdict(float)
    for workload, model_id in preferred_selection.items():
        pressure_by_preferred_model[model_id] += workload_rebalance_pressure(workload)
    protection_forecasts = intelligence.portfolio_forecasts(
        profile_list,
        direct_list,
        now=now,
        chosen_models=preferred_selection,
        workload_forecasts=workload_forecasts,
    )
    protection_plan = planner.plan(
        node_list,
        profile_list,
        protection_forecasts,
        now=now,
        compute_input_digest=False,
    )
    demand_replica_floors = dict(protection_plan.desired_replicas)
    preferred_models = frozenset(preferred_selection.values())

    def has_direct_pressure(forecast: DemandForecast) -> bool:
        observed_rate = forecast.observed_requests_per_minute
        if not observed_rate and not forecast.correlation_sources:
            observed_rate = forecast.requests_per_minute
        return bool(
            observed_rate > 0
            or forecast.queue_depth > 0
            or forecast.p95_latency_ms > 0
            or forecast.error_rate > 0
            or (forecast.offered_concurrency > 0 and not forecast.correlation_sources)
        )

    structural_forecasts = {
        forecast.model_id: forecast
        for forecast in direct_list
        if has_direct_pressure(forecast)
    }
    for model_id in preferred_models:
        structural_forecasts.setdefault(
            model_id,
            DemandForecast(
                model_id=model_id,
                requests_per_minute=0.01,
                observed_requests_per_minute=0.01,
                offered_concurrency=0.01,
                confidence=1.0,
                sample_count=1,
                updated_at=now,
            ),
        )
    structural_planner = PlacementPlanner(
        replace(planner.policy, preserve_recent_residencies=False)
    )

    def structural_node(node: NodeSnapshot) -> NodeSnapshot:
        # Test whether the preferred one-copy portfolio fits after allocator-owned work may move.
        # External inventory, pinned processes, and a manually managed host are not hypothetical
        # free capacity and must remain occupied in this lower-bound proof.
        retained = tuple(
            residency
            for residency in node.residencies
            if node.manually_managed or not residency.managed or residency.pinned
        )
        return replace(node, residencies=retained)

    structural_plan = structural_planner.plan(
        tuple(structural_node(node) for node in node_list),
        profile_list,
        tuple(structural_forecasts.values()),
        now=now,
        compute_input_digest=False,
    )
    complete_portfolio_feasible = all(
        structural_plan.nodes_for(model_id) for model_id in preferred_models
    )
    hard_protected_models = {
        profile.model_id
        for profile in profile_list
        if profile.min_replicas > 0
        or profile.pinned_nodes
    }
    for forecast in direct_list:
        if has_direct_pressure(forecast):
            hard_protected_models.add(forecast.model_id)

    active_capable_models = {
        profile.model_id
        for profile in profile_list
        if any(profile.workload_score(workload) > 0 for workload in active_workloads)
    }
    profile_by_id = {profile.model_id: profile for profile in profile_list}
    ready_replicas: Counter[str] = Counter()
    for node in node_list:
        age = now - node.last_heartbeat
        if (
            node.state not in (NodeState.ACCEPTING, NodeState.THROTTLED)
            or age < -planner.policy.max_future_clock_skew_seconds
            or age > planner.policy.node_ttl_seconds
        ):
            continue
        for residency in node.residencies:
            profile = profile_by_id.get(residency.model_id)
            if (
                profile is not None
                and residency.state == ResidencyState.READY
                and profile.matches_artifact(residency)
            ):
                ready_replicas[residency.model_id] += 1

    guarded: dict[str, dict[str, Any]] = {}
    for profile in profile_list:
        hint = dict(placement_hints.get(profile.model_id) or {})
        paths = [dict(path) for path in hint.get("preemption_paths") or ()]
        if hint.get("feasible_after_preemption") is True and not paths:
            paths = [
                {
                    key: hint.get(key)
                    for key in (
                        "startup_seconds",
                        "host_priority",
                        "best_node_id",
                        "preemption_victims",
                        "relocation_targets",
                    )
                }
            ]

        def path_blockers(path: Mapping[str, Any]) -> set[str]:
            victims = tuple(
                str(item) for item in path.get("preemption_victims") or ()
            )
            victim_node = next(
                (
                    node
                    for node in node_list
                    if node.node_id == str(path.get("best_node_id") or "")
                ),
                None,
            )
            removed_ready: Counter[str] = Counter()
            if victim_node is not None:
                for victim_id in set(victims):
                    residency = victim_node.residency(victim_id)
                    victim_profile = profile_by_id.get(victim_id)
                    if (
                        residency is not None
                        and victim_profile is not None
                        and residency.state == ResidencyState.READY
                        and victim_profile.matches_artifact(residency)
                    ):
                        removed_ready[victim_id] += 1
            blockers: set[str] = set()
            for victim_id in victims:
                if victim_id in hard_protected_models:
                    blockers.add(victim_id)
                    continue
                removes_last_required_copy = (
                    victim_id in active_capable_models
                    and ready_replicas[victim_id] - removed_ready[victim_id]
                    < (
                        1
                        if complete_portfolio_feasible
                        else max(1, demand_replica_floors.get(victim_id, 0))
                    )
                )
                if not removes_last_required_copy:
                    continue
                victim_workloads = {
                    workload
                    for workload, preferred_model in preferred_selection.items()
                    if preferred_model == victim_id
                }
                # A sole speculative residency is not sacred when the proposed replacement can
                # continue every active workload that justified it. This is the safe consolidation
                # path from a narrow research model to a code model that serves both research and
                # an emerging coding surge. Direct, pinned, and minimum replicas remain hard
                # blockers above; the authoritative planner still stages make-before-break.
                replacement_preserves_coverage = bool(victim_workloads) and all(
                    profile.workload_score(workload) > 0
                    for workload in victim_workloads
                )
                beneficiary_pressure = pressure_by_preferred_model[profile.model_id]
                victim_pressure = pressure_by_preferred_model[victim_id]
                pressure_rebalance = bool(
                    not complete_portfolio_feasible
                    and beneficiary_pressure
                    >= max(1e-6, victim_pressure)
                    * _SCARCE_REBALANCE_MIN_PRESSURE_GAIN
                )
                if not replacement_preserves_coverage and not pressure_rebalance:
                    blockers.add(victim_id)
            return blockers

        safe_path: dict[str, Any] | None = None
        all_blockers: set[str] = set()
        for path in paths:
            blockers = path_blockers(path)
            all_blockers.update(blockers)
            if not blockers:
                safe_path = path
                break
        if safe_path is not None:
            for key in (
                "startup_seconds",
                "host_priority",
                "best_node_id",
                "preemption_victims",
                "relocation_targets",
            ):
                hint[key] = safe_path.get(key)
            sorted_blockers: list[str] = []
        else:
            sorted_blockers = sorted(all_blockers)
        preemption_safe = bool(
            hint.get("feasible_after_preemption") is True and safe_path is not None
        )
        hint["portfolio_preemption_safe"] = preemption_safe
        hint["portfolio_preemption_blockers"] = sorted_blockers
        hint["portfolio_complete_feasible"] = complete_portfolio_feasible
        beneficiary_pressure = pressure_by_preferred_model[profile.model_id]
        victim_ids = sorted(
            {
                str(victim)
                for path in paths
                for victim in path.get("preemption_victims") or ()
            }
        )
        hint["portfolio_rebalance_pressure"] = round(beneficiary_pressure, 6)
        hint["portfolio_rebalance_required_pressure"] = round(
            min(
                (
                    pressure_by_preferred_model[victim_id]
                    * _SCARCE_REBALANCE_MIN_PRESSURE_GAIN
                    for victim_id in victim_ids
                    if victim_id not in hard_protected_models
                ),
                default=0.0,
            ),
            6,
        )
        hint["portfolio_rebalance_victim_pressures"] = {
            victim_id: round(pressure_by_preferred_model[victim_id], 6)
            for victim_id in victim_ids
        }
        if hint.get("feasible_after_preemption") is True and sorted_blockers:
            hint["reason"] = (
                str(hint.get("reason") or "planner-authorized preemption path")
                + "; protected by current workload demand: "
                + ", ".join(sorted_blockers)
            )
        guarded[profile.model_id] = hint
    return guarded


def _portfolio_admissions(
    projections: Iterable[Mapping[str, Any]],
    nodes: Iterable[NodeSnapshot],
    profiles: Iterable[ModelProfile],
    plan: PlacementPlan | None,
    result: ReconcileResult | None,
) -> list[dict[str, Any]]:
    """Explain workload admission from the same evidence used by the allocator.

    This is intentionally diagnostic, not another policy or solver. It classifies the latest joint
    portfolio choice against the authoritative plan, observed runtime state, and planner placement
    hint so operators can distinguish an impossible workload from contention or normal startup.
    """

    node_list = tuple(nodes)
    profile_by_id = {profile.model_id: profile for profile in profiles}
    planned_by_model: dict[str, int] = {}
    desired_by_model: dict[str, int] = {}
    missing_by_model: dict[str, int] = {}
    if plan is not None:
        desired_by_model = dict(plan.desired_replicas)
        for assignment in plan.assignments:
            planned_by_model[assignment.model_id] = (
                planned_by_model.get(assignment.model_id, 0) + 1
            )
        for constraint in plan.unsatisfied:
            missing_by_model[constraint.model_id] = max(
                missing_by_model.get(constraint.model_id, 0),
                constraint.missing_replicas,
            )
    starting_models = {
        action.model_id
        for action in (result.actions if result is not None else ())
        if action.kind in (ActionKind.LOAD, ActionKind.WARM)
    }
    rows: list[dict[str, Any]] = []
    for projection in sorted(projections, key=lambda item: str(item.get("workload") or "")):
        workload = str(projection.get("workload") or "")
        requests_per_minute = float(projection.get("requests_per_minute") or 0.0)
        offered_concurrency = float(projection.get("offered_concurrency") or 0.0)
        if requests_per_minute <= 0 and offered_concurrency <= 0:
            continue
        model_id = str(projection.get("chosen_model") or "")
        candidates = tuple(projection.get("candidates") or ())
        base: dict[str, Any] = {
            "workload": workload,
            "model_id": model_id,
            "requests_per_minute": requests_per_minute,
            "offered_concurrency": offered_concurrency,
            "candidate_models": sorted(
                {
                    str(candidate.get("model_id") or "")
                    for candidate in candidates
                    if candidate.get("model_id")
                }
            ),
        }
        correlation_sources = tuple(
            str(item)
            for item in projection.get("demand_correlation_sources") or ()
            if str(item)
        )
        if correlation_sources:
            base["demand_correlation_sources"] = correlation_sources
            base["demand_correlation_confidence"] = float(
                projection.get("demand_correlation_confidence") or 0.0
            )
            prediction_lead_seconds = projection.get("prediction_lead_seconds")
            if prediction_lead_seconds is not None:
                base["prediction_lead_seconds"] = float(
                    prediction_lead_seconds
                )
        if not model_id:
            blocked_candidate = next(
                (
                    candidate
                    for candidate in candidates
                    if (candidate.get("placement") or {}).get(
                        "feasible_after_preemption"
                    )
                    is True
                ),
                None,
            )
            compatible_candidate = next(
                (
                    candidate
                    for candidate in candidates
                    if (candidate.get("placement") or {}).get("hard_compatible")
                    is True
                ),
                None,
            )
            diagnostic_candidate = blocked_candidate or compatible_candidate
            placement = dict(
                (diagnostic_candidate or {}).get("placement") or {}
            )
            if projection.get("deferred") is True:
                state = "deferred"
                reason = str(
                    projection.get("reason")
                    or "joint portfolio deferred under current capacity"
                )
            elif blocked_candidate is not None:
                state = "blocked-by-residency"
                reason = str(
                    placement.get("reason")
                    or "compatible capacity requires a safe residency transition"
                )
            elif compatible_candidate is not None:
                state = "capacity-contended"
                reason = str(
                    placement.get("reason")
                    or "compatible hardware exists but current capacity cannot admit the model"
                )
            else:
                state = "infeasible"
                reason = str(projection.get("reason") or "no selectable model")
            base.update(
                state=state,
                reason=reason,
                desired_replicas=0,
                planned_replicas=0,
                ready_replicas=0,
                missing_replicas=0,
                eligible_nodes=int(placement.get("eligible_nodes") or 0),
                startup_seconds=float(placement.get("startup_seconds") or 0.0),
                blocking_models=list(
                    placement.get("portfolio_preemption_blockers")
                    or placement.get("preemption_victims")
                    or ()
                ),
                rebalance_pressure=float(
                    placement.get("portfolio_rebalance_pressure") or 0.0
                ),
                rebalance_required_pressure=float(
                    placement.get("portfolio_rebalance_required_pressure") or 0.0
                ),
            )
            rows.append(base)
            continue

        profile = profile_by_id.get(model_id)
        placement = dict(projection.get("placement") or {})
        desired = desired_by_model.get(model_id, 0)
        planned = planned_by_model.get(model_id, 0)
        missing = max(
            missing_by_model.get(model_id, 0),
            max(0, desired - planned),
        )
        ready = 0
        live_starting = False
        for node in node_list:
            if node.state not in (NodeState.ACCEPTING, NodeState.THROTTLED):
                continue
            residency = node.residency(model_id)
            if residency is None:
                continue
            if residency.state in (ResidencyState.LOADING, ResidencyState.WARMING):
                live_starting = True
            if (
                residency.state == ResidencyState.READY
                and profile is not None
                and profile.matches_artifact(residency)
            ):
                ready += 1
        base.update(
            desired_replicas=desired,
            planned_replicas=planned,
            ready_replicas=ready,
            missing_replicas=missing,
            eligible_nodes=int(placement.get("eligible_nodes") or 0),
            startup_seconds=float(placement.get("startup_seconds") or 0.0),
            blocking_models=list(
                placement.get("portfolio_preemption_blockers")
                or placement.get("preemption_victims")
                or ()
            ),
        )
        if plan is None or (desired == 0 and planned == 0):
            base.update(
                state="awaiting-plan",
                reason="current workload choice is not represented in the latest allocation plan",
            )
        elif ready > 0 and (missing > 0 or ready < desired):
            base.update(
                state="undersupplied",
                reason=(
                    f"{ready} ready of {desired} desired replicas; "
                    f"{max(missing, desired - ready)} still missing"
                ),
            )
        elif ready > 0:
            base.update(
                state="ready",
                reason=f"{ready} ready replica{'s' if ready != 1 else ''} serving the workload",
            )
        elif live_starting or model_id in starting_models:
            base.update(
                state="starting",
                reason="selected capacity is loading or warming and is not ready yet",
            )
        elif planned > 0:
            base.update(
                state="planned",
                reason="selected in the latest plan; runtime startup is not yet visible",
            )
        elif placement.get("feasible_after_preemption") is True:
            base.update(
                state="blocked-by-residency",
                reason=str(
                    placement.get("reason")
                    or "compatible capacity requires a safe residency transition"
                ),
            )
        elif placement.get("feasible_now") is True or placement.get("feasible") is True:
            base.update(
                state="capacity-contended",
                reason="fleet-feasible alone, but the latest joint plan admitted other demand",
            )
        elif placement.get("hard_compatible") is True:
            base.update(
                state="capacity-contended",
                reason=str(
                    placement.get("reason")
                    or "compatible hardware exists but current capacity cannot admit the model"
                ),
            )
        else:
            base.update(
                state="infeasible",
                reason=str(
                    placement.get("reason")
                    or projection.get("reason")
                    or "no compatible live host"
                ),
            )
        rows.append(base)
    return rows


def _action_dict(action: MutationAction) -> dict[str, Any]:
    payload = {
        **asdict(action),
        "kind": action.kind.value,
        "dependencies": list(action.dependencies),
    }
    if not action.predictive_prefetch:
        payload.pop("predictive_prefetch", None)
    return payload


def _action_from_dict(value: dict[str, Any]) -> MutationAction:
    return MutationAction(
        action_id=str(value["action_id"]),
        kind=ActionKind(value["kind"]),
        node_id=str(value["node_id"]),
        model_id=str(value["model_id"]),
        memory_mb=int(value["memory_mb"]),
        reason=str(value["reason"]),
        plan_generation=str(value["plan_generation"]),
        created_at=float(value["created_at"]),
        not_before=float(value.get("not_before") or 0.0),
        dependencies=tuple(value.get("dependencies") or ()),
        executable=bool(value.get("executable", False)),
        artifact_sha256=value.get("artifact_sha256") or "",
        artifact_source=str(value.get("artifact_source") or ""),
        artifact_size_mb=int(value.get("artifact_size_mb") or 0),
        controller_term=int(value.get("controller_term") or 0),
        controller_id=str(value.get("controller_id") or ""),
        controller_lease_expires_at=float(
            value.get("controller_lease_expires_at") or 0.0
        ),
        predictive_prefetch=bool(value.get("predictive_prefetch", False)),
    )


def _record_dict(record: MutationRecord) -> dict[str, Any]:
    return {**asdict(record), "kind": record.kind.value, "status": record.status.value}


def _record_from_dict(value: dict[str, Any]) -> MutationRecord:
    return MutationRecord(
        action_id=str(value["action_id"]),
        kind=ActionKind(value["kind"]),
        node_id=str(value["node_id"]),
        model_id=str(value["model_id"]),
        status=MutationStatus(value["status"]),
        attempted_at=float(value["attempted_at"]),
        completed_at=float(value.get("completed_at") or 0.0),
        duration_seconds=_bounded_action_duration(value.get("duration_seconds")),
        failures=int(value.get("failures") or 0),
        message=str(value.get("message") or ""),
        artifact_sha256=value.get("artifact_sha256") or "",
        artifact_fetched=bool(value.get("artifact_fetched", False)),
    )


def _bounded_action_duration(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        duration = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if (
        not math.isfinite(duration)
        or duration <= 0
        or duration > _MAX_REPORTED_ACTION_DURATION_SECONDS
    ):
        return 0.0
    return duration


def _result_dict(result: ReconcileResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "plan_generation": result.plan_generation,
        "mode": result.mode.value,
        "actions": [_action_dict(item) for item in result.actions],
        "deferred": [
            {**asdict(item), "kind": item.kind.value}
            for item in result.deferred
        ],
    }
