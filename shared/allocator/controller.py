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
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from shared import jsonio
from shared.allocator.auth import validate_host_id
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
_MAX_REPORTED_ACTION_DURATION_SECONDS = 3_600.0
_STARTUP_ESTIMATE_SAMPLES = 8
_STARTUP_ESTIMATE_FULL_CONFIDENCE_SAMPLES = 4
_STARTUP_ESTIMATE_EWMA_ALPHA = 0.25
_STARTUP_ESTIMATE_TTL_SECONDS = 30 * 24 * 60 * 60
_MAX_JOINT_PORTFOLIO_CANDIDATES = 4
_MAX_JOINT_PORTFOLIO_EVALUATIONS = 64
_MAX_JOINT_EXPLORATION_MODELS = 1
_MAX_HOST_PRICES = 10_000


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
        self._host_prices: dict[str, float] = {}
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

    @property
    def host_prices(self) -> dict[str, float]:
        """Return a copy of controller-owned physical-host prices."""

        with self._lock:
            return dict(self._host_prices)

    def apply_host_prices(
        self,
        nodes: Iterable[NodeSnapshot],
        *,
        prices: Mapping[str, float] | None = None,
    ) -> tuple[NodeSnapshot, ...]:
        """Apply authoritative prices after discovery records are merged by physical host.

        The caller decides whether a non-registry snapshot is trusted. The local signaling server
        deliberately strips node claims first; other control planes may supply already-authorized
        snapshots. A registry entry always wins.
        """

        with self._lock:
            authoritative = dict(self._host_prices if prices is None else prices)
        return tuple(
            replace(
                node,
                cost_per_hour=authoritative[node.node_id],
                cost_known=True,
                cost_source="operator",
            )
            if node.node_id in authoritative
            else node
            for node in nodes
        )

    def set_host_price(
        self,
        host_id: str,
        cost_per_hour: float | None,
        *,
        allow_service_shortfall: bool = False,
        nodes: Iterable[NodeSnapshot] = (),
        now: float | None = None,
    ) -> dict[str, float]:
        """Set or clear an operator price as a durable, service-safe transaction."""

        host_id = str(host_id)
        validate_host_id(host_id)
        if cost_per_hour is not None:
            cost_per_hour = float(cost_per_hour)
            if not math.isfinite(cost_per_hour) or cost_per_hour < 0:
                raise ValueError("cost_per_hour must be finite and non-negative")
        if not isinstance(allow_service_shortfall, bool):
            raise ValueError("allow_service_shortfall must be a boolean")
        timestamp = time.time() if now is None else float(now)
        if not math.isfinite(timestamp) or timestamp < 0:
            raise ValueError("now must be finite and non-negative")
        current_nodes = tuple(nodes)
        with self._lock:
            checkpoint = self._checkpoint()
            proposed_prices = dict(self._host_prices)
            if cost_per_hour is None:
                proposed_prices.pop(host_id, None)
            else:
                if host_id not in proposed_prices and len(proposed_prices) >= _MAX_HOST_PRICES:
                    raise ValueError("allocator host price registry is full")
                proposed_prices[host_id] = cost_per_hour
            proposed_nodes = self.apply_host_prices(
                tuple(
                    replace(
                        node,
                        cost_per_hour=0.0,
                        cost_known=False,
                        cost_source="unknown",
                    )
                    if node.node_id == host_id and node.cost_source == "operator"
                    else node
                    for node in current_nodes
                ),
                prices=proposed_prices,
            )
            if current_nodes and not allow_service_shortfall:
                self._reject_minimum_coverage_regression(
                    current_nodes,
                    proposed_nodes,
                    timestamp,
                    "host price update",
                )
            self._host_prices = proposed_prices
            self._save_or_rollback(checkpoint)
            return dict(self._host_prices)

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

    def set_hourly_cost_budget(
        self,
        max_hourly_cost: float,
        *,
        allow_unknown_cost: bool = False,
        allow_service_shortfall: bool = False,
        nodes: Iterable[NodeSnapshot] = (),
        now: float | None = None,
    ) -> PlannerPolicy:
        """Persist a hard fleet placement budget; zero disables the ceiling.

        A policy update is a potentially destructive desired-state transaction. When live fleet
        state is supplied, reject any newly introduced minimum-replica shortfall unless the
        operator explicitly acknowledges that service tradeoff.
        """

        maximum = float(max_hourly_cost)
        if not math.isfinite(maximum) or maximum < 0:
            raise ValueError("max_hourly_cost must be finite and non-negative")
        if not isinstance(allow_unknown_cost, bool):
            raise ValueError("allow_unknown_cost must be a boolean")
        if not isinstance(allow_service_shortfall, bool):
            raise ValueError("allow_service_shortfall must be a boolean")
        timestamp = time.time() if now is None else float(now)
        if not math.isfinite(timestamp) or timestamp < 0:
            raise ValueError("now must be finite and non-negative")
        node_list = tuple(nodes)
        with self._lock:
            node_list = self.apply_host_prices(node_list)
            checkpoint = self._checkpoint()
            policy = replace(
                self.planner.policy,
                max_hourly_cost=maximum,
                allow_unknown_cost=allow_unknown_cost,
            )
            if node_list and not allow_service_shortfall:
                self._reject_minimum_coverage_regression(
                    node_list,
                    node_list,
                    timestamp,
                    "budget update",
                    proposed_planner=PlacementPlanner(policy),
                )
            self.planner = PlacementPlanner(policy)
            self._save_or_rollback(checkpoint)
            return policy

    def _reject_minimum_coverage_regression(
        self,
        current_nodes: tuple[NodeSnapshot, ...],
        proposed_nodes: tuple[NodeSnapshot, ...],
        timestamp: float,
        change: str,
        *,
        proposed_planner: PlacementPlanner | None = None,
    ) -> None:
        profiles = tuple(
            self._profiles[model_id]
            for model_id in sorted(self._profiles)
            if model_id not in self._retiring
        )
        placement_hints = self.planner.portfolio_placement_hints(
            current_nodes,
            profiles,
            now=timestamp,
        )
        forecasts = self._forecasts(
            timestamp,
            placement_hints=placement_hints,
            nodes=current_nodes,
        )
        current = self.planner.plan(
            current_nodes,
            profiles,
            forecasts,
            now=timestamp,
        )
        proposed = (proposed_planner or self.planner).plan(
            proposed_nodes, profiles, forecasts, now=timestamp
        )

        def minimum_coverage(plan: PlacementPlan, model_id: str) -> int:
            return sum(
                assignment.model_id == model_id for assignment in plan.assignments
            )

        desired = dict(current.desired_replicas)
        regressions = {
            profile.model_id: (
                minimum_coverage(current, profile.model_id),
                minimum_coverage(proposed, profile.model_id),
                desired.get(profile.model_id, profile.min_replicas),
            )
            for profile in profiles
            if desired.get(profile.model_id, profile.min_replicas) > 0
            and minimum_coverage(proposed, profile.model_id)
            < minimum_coverage(current, profile.model_id)
        }
        if regressions:
            detail = ", ".join(
                f"{model_id} {after}/{target} desired replicas (currently {before})"
                for model_id, (before, after, target) in sorted(regressions.items())
            )
            raise ValueError(
                f"{change} would reduce minimum service coverage: {detail}; "
                "repeat with allow_service_shortfall=true to acknowledge"
            )

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
        timestamp: float | None = None,
    ) -> bool:
        """Observe one completed request independently of any router implementation.

        Configured named models retain the existing direct scaling signal. Unknown/reserved model
        names additionally become portfolio-unbound workload pressure, allowing an inactive but
        configured capable model to receive a conservative canary placement at planning time.
        """

        served_artifact = canonical_sha256(served_artifact_sha256)
        direct_model = served_model or features.requested_model
        directly_observable = direct_model in self._observable_models
        # Model binding belongs to the incoming request, not to the router's eventual fallback.
        # An ``auto`` coding request that a ready generalist happens to serve is still unbound
        # coding demand: retaining that signal is what lets the allocator provision a better
        # specialist for later requests. At the same time, the served model receives ordinary
        # direct capacity demand so a useful fallback can scale while the specialist warms.
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
                timestamp=timestamp,
            )
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
            return self.intelligence.observe_model_evaluation(
                model_id,
                workload,
                artifact_sha256=configured_artifact,
                quality=quality,
                error=error,
                latency_ms=latency_ms,
                output_units=output_units,
                timestamp=timestamp,
            )

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
        placement_hints = self.planner.portfolio_placement_hints(
            node_list,
            profiles,
            now=timestamp,
        )
        forecasts = self._forecasts(
            timestamp,
            placement_hints=placement_hints,
            nodes=node_list,
        )
        startup_estimates, _ = self._learned_warm_estimates(now=timestamp)
        learned_by_model = {
            profile.model_id: profile.warm_seconds for profile in profiles
        }
        for (_, model_id), estimate in startup_estimates.items():
            learned_by_model[model_id] = max(
                learned_by_model.get(model_id, 0.0),
                estimate,
            )
        effective_profiles = tuple(
            replace(
                profile,
                warm_seconds=learned_by_model.get(
                    profile.model_id,
                    profile.warm_seconds,
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

            def delivery_rank(action: MutationAction) -> tuple[int, int]:
                beneficiary_id = preemption_by_pair.get(
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
                    or action.kind not in (ActionKind.DRAIN, ActionKind.UNLOAD)
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
                if action.kind in (ActionKind.DRAIN, ActionKind.UNLOAD)
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
            node_list = self.apply_host_prices(node_list)
            profiles = self.profiles
            placement_hints = self.planner.portfolio_placement_hints(
                node_list,
                profiles,
                now=timestamp,
            )
            forecasts, portfolio_selection = self._forecast_bundle(
                timestamp,
                placement_hints=placement_hints,
                nodes=node_list,
            )
            startup_estimates, startup_samples = self._learned_warm_estimates(
                now=timestamp
            )
            with self._demand_lock:
                workload_forecasts = self.intelligence.workload_forecasts(now=timestamp)
                portfolio_projections = self.intelligence.projections(
                    profiles,
                    now=timestamp,
                    placement_hints=placement_hints,
                    chosen_models=portfolio_selection,
                )
                projected_cohorts = {
                    str(row.get("workload") or ""): dict(row["cohort_evidence"])
                    for row in portfolio_projections
                    if row.get("workload") and row.get("cohort_evidence")
                }
                cohort_summaries = tuple(
                    projected_cohorts.get(str(row.get("workload") or ""), row)
                    for row in self.intelligence.cohort_summaries(now=timestamp)
                )
                model_workload_outcomes = self.intelligence.outcomes
            selected_portfolio_models = sorted(set((portfolio_selection or {}).values()))
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
            active_cost_nodes = tuple(
                node
                for node in node_list
                if any(
                    residency.state
                    not in (ResidencyState.CACHED, ResidencyState.FAILED)
                    for residency in node.residencies
                )
            )
            current_hourly_cost = sum(
                node.cost_per_hour for node in active_cost_nodes if node.cost_known
            )
            current_unknown_cost_nodes = tuple(
                sorted(node.node_id for node in active_cost_nodes if not node.cost_known)
            )
            maximum_hourly_cost = self.planner.policy.max_hourly_cost
            if not maximum_hourly_cost:
                budget_compliance = "disabled"
            elif current_hourly_cost > maximum_hourly_cost + 1e-12:
                budget_compliance = "over_budget"
            elif current_unknown_cost_nodes:
                budget_compliance = "unknown"
            else:
                budget_compliance = "within_budget"
            desired_hourly_cost = self._last_plan.hourly_cost if self._last_plan else 0.0
            desired_unknown_cost_nodes = (
                self._last_plan.unknown_cost_nodes if self._last_plan else ()
            )
            demand_weight = sum(max(0.0, item.requests_per_minute) for item in forecasts)
            demand_confidence = (
                sum(
                    max(0.0, item.requests_per_minute) * item.confidence
                    for item in forecasts
                )
                / demand_weight
                if demand_weight
                else 1.0
            )
            spend_forecast = {
                "basis": "desired_fleet_run_rate",
                "demand_confidence": demand_confidence,
                "complete": not desired_unknown_cost_nodes,
                "unknown_cost_nodes": list(desired_unknown_cost_nodes),
                "windows": [
                    {
                        "hours": hours,
                        "known_spend": desired_hourly_cost * hours,
                        "risk_adjusted_known_spend": desired_hourly_cost
                        * hours
                        * (1.0 + 0.25 * (1.0 - demand_confidence)),
                        "budget_limit": (
                            maximum_hourly_cost * hours if maximum_hourly_cost else 0.0
                        ),
                        "budget_headroom": (
                            max(0.0, maximum_hourly_cost - desired_hourly_cost) * hours
                            if maximum_hourly_cost
                            else 0.0
                        ),
                    }
                    for hours in (1, 24, 720)
                ],
            }
            capacity_recommendations = []
            if self._last_plan is not None:
                for constraint in self._last_plan.unsatisfied:
                    if constraint.missing_replicas <= 0:
                        continue
                    profile = self._profiles.get(constraint.model_id)
                    hint = placement_hints.get(constraint.model_id) or {}
                    if profile is None:
                        continue
                    cheapest_cost = float(hint.get("cost_per_hour") or 0.0)
                    capacity_recommendations.append(
                        {
                            "model_id": constraint.model_id,
                            "reason": constraint.code,
                            "missing_replicas": constraint.missing_replicas,
                            "minimum_memory_mb": profile.maximum_memory_mb,
                            "runtimes": list(profile.runtimes),
                            "backends": list(profile.backends),
                            "minimum_gpu_count": profile.min_gpu_count,
                            "minimum_gpu_memory_mb": profile.min_gpu_memory_mb,
                            "cheapest_known_host_cost_per_hour": cheapest_cost,
                            "minimum_additional_budget_per_hour": (
                                max(
                                    0.0,
                                    cheapest_cost
                                    - max(
                                        0.0,
                                        maximum_hourly_cost - desired_hourly_cost,
                                    ),
                                )
                                if maximum_hourly_cost
                                and constraint.code == "hourly_cost_budget"
                                else 0.0
                            ),
                        }
                    )
            return {
                "schema_version": SCHEMA_VERSION,
                "mode": self.mode.value,
                "controller_epoch": self._controller_epoch,
                "controller_term": self._controller_term,
                "controller_id": self._controller_id,
                "controller_lease_expires_at": self._controller_lease_expires_at,
                "planner_policy": asdict(self.planner.policy),
                "cost": {
                    "max_hourly_cost": maximum_hourly_cost,
                    "allow_unknown_cost": self.planner.policy.allow_unknown_cost,
                    "current_hourly_cost": current_hourly_cost,
                    "current_unknown_cost_nodes": list(current_unknown_cost_nodes),
                    "desired_hourly_cost": (
                        desired_hourly_cost
                    ),
                    "desired_unknown_cost_nodes": (
                        list(desired_unknown_cost_nodes)
                    ),
                    "compliance": budget_compliance,
                    "operator_host_prices": dict(sorted(self._host_prices.items())),
                },
                "spend_forecast": spend_forecast,
                "capacity_recommendations": capacity_recommendations,
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
                "cohort_summaries": list(cohort_summaries),
                "portfolio_projections": list(portfolio_projections),
                "portfolio_selection": dict(portfolio_selection or {}),
                "portfolio_policy": {
                    "joint": bool(portfolio_selection is not None),
                    "workloads": len(portfolio_selection or {}),
                    "selected_models": selected_portfolio_models,
                    "exploration_models": sorted(exploration_models),
                    "max_candidates_per_workload": _MAX_JOINT_PORTFOLIO_CANDIDATES,
                    "max_evaluations": _MAX_JOINT_PORTFOLIO_EVALUATIONS,
                    "max_exploration_models": _MAX_JOINT_EXPLORATION_MODELS,
                },
                "portfolio_placement_hints": [
                    placement_hints[model_id]
                    for model_id in sorted(placement_hints)
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
        forecasts, _ = self._forecast_bundle(
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
    ) -> tuple[tuple[DemandForecast, ...], dict[str, str] | None]:
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
        selection = self._joint_portfolio_selection(
            profiles,
            direct,
            node_list,
            now=now,
            placement_hints=placement_hints,
            intelligence=intelligence,
        )
        forecasts = intelligence.portfolio_forecasts(
            profiles,
            direct,
            now=now,
            placement_hints=placement_hints,
            chosen_models=selection,
        )
        return forecasts, selection

    def _joint_portfolio_selection(
        self,
        profiles: tuple[ModelProfile, ...],
        direct: tuple[DemandForecast, ...],
        nodes: tuple[NodeSnapshot, ...],
        *,
        now: float,
        placement_hints: Mapping[str, Mapping[str, Any]] | None,
        intelligence: WorkloadIntelligence,
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
        )
        active_rows = [
            row
            for row in projections
            if int(row.get("samples") or 0) >= intelligence.portfolio_min_samples
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

        # Begin from the exploitation-only portfolio. Coordinate descent may then spend the single
        # exploration slot where its optimism buys the most workload utility without ever entering
        # an invalid multi-experiment state that one-at-a-time moves cannot escape.
        selection = dict(exploitation_best)
        profile_by_id = {profile.model_id: profile for profile in profiles}
        baseline = self.planner.plan(nodes, profiles, direct, now=now)
        baseline_targets = dict(baseline.desired_replicas)
        evaluation_cache: dict[tuple[tuple[str, str], ...], tuple[float, ...]] = {}

        def metric(candidate_selection: Mapping[str, str]) -> tuple[float, ...]:
            key = tuple(sorted(candidate_selection.items()))
            cached = evaluation_cache.get(key)
            if cached is not None:
                return cached
            exploration_models = {
                model_id
                for workload, model_id in candidate_selection.items()
                if model_id != exploitation_best[workload]
            }
            if len(exploration_models) > _MAX_JOINT_EXPLORATION_MODELS:
                result = (float("-inf"),)
                evaluation_cache[key] = result
                return result
            forecasts = intelligence.portfolio_forecasts(
                profiles,
                direct,
                now=now,
                placement_hints=placement_hints,
                chosen_models=candidate_selection,
            )
            plan = self.planner.plan(nodes, profiles, forecasts, now=now)
            placed: dict[str, int] = {}
            for assignment in plan.assignments:
                placed[assignment.model_id] = placed.get(assignment.model_id, 0) + 1
            baseline_coverage = sum(
                min(placed.get(model_id, 0), target)
                * (1.0 + profile_by_id[model_id].priority)
                for model_id, target in baseline_targets.items()
                if target > 0 and model_id in profile_by_id
            )
            workload_coverage = 0.0
            utility = 0.0
            for workload, model_id in candidate_selection.items():
                row = row_by_workload[workload]
                weight = max(1e-6, float(row.get("requests_per_minute") or 0.0)) * (
                    0.5 + 0.5 * float(row.get("confidence") or 0.0)
                )
                desired = max(1, plan.target_for(model_id))
                ratio = min(1.0, placed.get(model_id, 0) / desired)
                workload_coverage += weight * ratio
                utility += weight * ratio * float(
                    candidate_by_workload[workload][model_id].get("score") or 0.0
                )
            missing = sum(item.missing_replicas for item in plan.unsatisfied)
            result = (
                baseline_coverage,
                workload_coverage,
                -float(missing),
                utility,
                -float(len(plan.unknown_cost_nodes)),
                -plan.hourly_cost,
            )
            evaluation_cache[key] = result
            return result

        # Heaviest and best-observed workloads choose first. Each trial still evaluates the full
        # current mapping, so an early specialist sees the capacity needs of every later workload.
        order = sorted(
            row_by_workload,
            key=lambda workload: (
                -float(row_by_workload[workload].get("requests_per_minute") or 0.0),
                -float(row_by_workload[workload].get("confidence") or 0.0),
                workload,
            ),
        )
        best_metric = metric(selection)
        shared_models = sorted(
            {
                model_id
                for model_ids in options.values()
                for model_id in model_ids
                if sum(model_id in choices for choices in options.values()) > 1
            }
        )
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

        samples: dict[tuple[str, str], list[float]] = {}
        for record in self._history:
            if (
                record.kind != ActionKind.WARM
                or record.status != MutationStatus.SUCCEEDED
                or record.duration_seconds <= 0
                or record.model_id not in self._profiles
                or record.artifact_sha256
                != self._profiles[record.model_id].artifact_sha256
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
            prior = self._profiles[key[1]].warm_seconds
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
            model_ids = tuple((self.demand.to_dict().get("models") or {}).keys())
            for model_id in model_ids:
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
            model_urgencies=plan.model_urgencies,
            hourly_cost=plan.hourly_cost,
            unknown_cost_nodes=plan.unknown_cost_nodes,
            hourly_cost_budget=plan.hourly_cost_budget,
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
            else:
                # UNLOAD's postcondition is a cached artifact without a live residency.
                # In particular READY is not resolution: a late, previously delivered unload
                # could race a newer warm and remove the newly admitted replica.
                resolved = residency is None or residency.state == ResidencyState.CACHED
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
                    action.kind in (ActionKind.DRAIN, ActionKind.UNLOAD) and pair in desired
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
            and action.kind in (ActionKind.DRAIN, ActionKind.UNLOAD)
        }
        unsafe_destructive_models.update(
            action.model_id for action in self._withdrawn_destructive.values()
        )
        for action_id, action in self._commands.items():
            if (
                action.kind not in (ActionKind.DRAIN, ActionKind.UNLOAD)
                or action.model_id not in unsafe_destructive_models
            ):
                continue
            stale_messages.setdefault(
                action_id,
                "another command in the same destructive batch became unsafe",
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
            and action.kind in (ActionKind.DRAIN, ActionKind.UNLOAD)
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
            "host_prices": dict(sorted(self._host_prices.items())),
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
            "planner": self.planner,
            "host_prices": dict(self._host_prices),
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
        self.planner = checkpoint["planner"]
        self._host_prices = checkpoint["host_prices"]
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
        host_prices = value.get("host_prices") or {}
        if not isinstance(host_prices, dict):
            raise ValueError("invalid persisted allocator host prices")
        self._host_prices = {}
        for host_id, raw_cost in host_prices.items():
            host_id = str(host_id)
            cost = float(raw_cost)
            try:
                validate_host_id(host_id)
            except ValueError as exc:
                raise ValueError("invalid persisted allocator host price") from exc
            if not math.isfinite(cost) or cost < 0:
                raise ValueError("invalid persisted allocator host price")
            self._host_prices[host_id] = cost
        if len(self._host_prices) > _MAX_HOST_PRICES:
            raise ValueError("persisted allocator host price registry is too large")
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
            action.kind not in (ActionKind.DRAIN, ActionKind.UNLOAD)
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


def _action_dict(action: MutationAction) -> dict[str, Any]:
    return {
        **asdict(action),
        "kind": action.kind.value,
        "dependencies": list(action.dependencies),
    }


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
        controller_term=int(value.get("controller_term") or 0),
        controller_id=str(value.get("controller_id") or ""),
        controller_lease_expires_at=float(
            value.get("controller_lease_expires_at") or 0.0
        ),
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
