"""Thread-safe global allocation loop and desired-state command queue.

The controller is usable by the in-process local Grid server.  The hosted control plane can consume
the same pure planner/reconciler later, but owns its own durable database and wire authentication.
Automatic mode is opt-in; recommend mode is the safe default.
"""

from __future__ import annotations

import math
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from shared import jsonio
from shared.allocator.demand import DemandTracker
from shared.allocator.models import (
    SCHEMA_VERSION,
    ActionKind,
    AllocatorMode,
    DemandForecast,
    ModelProfile,
    MutationAction,
    NodeSnapshot,
    PlacementPlan,
    ResidencyState,
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
    ) -> None:
        if max_history < 1:
            raise ValueError("max_history must be positive")
        if (
            not math.isfinite(membership_recovery_grace_seconds)
            or membership_recovery_grace_seconds < 0
        ):
            raise ValueError("membership_recovery_grace_seconds must be finite and non-negative")
        self.mode = AllocatorMode(mode)
        self.planner = PlacementPlanner(planner_policy)
        self.reconciler = Reconciler(reconcile_policy)
        self.demand = DemandTracker()
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

    def set_mode(self, mode: AllocatorMode) -> None:
        with self._lock:
            checkpoint = self._checkpoint()
            self.mode = AllocatorMode(mode)
            if self.mode != AllocatorMode.AUTOMATIC:
                self._cancel_all_pending("automatic allocation was disabled")
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
            return True

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
                self._last_tick_duration_seconds = max(
                    0.0,
                    time.monotonic() - started_at,
                )
                return result
            except jsonio.AtomicWriteCommittedError:
                raise
            except BaseException:
                # Planner/reconciler/action construction can fail before the persistence helper is
                # reached. Restore all controller transaction state, while deliberately retaining
                # independently locked request telemetry that arrived during the failed tick.
                self._rollback(checkpoint)
                raise

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
        forecasts = self._forecasts(timestamp)
        profiles = self.profiles
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
        raw_plan = self.planner.plan(
            node_list,
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
        result = self.reconciler.reconcile(
            plan,
            node_list,
            profiles,
            self._history,
            mode=self.mode,
            now=timestamp,
            blocked_until=self._mutation_blocks,
            blocked_causes=self._mutation_block_causes,
            blocked_destructive_models=blocked_destructive_models,
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
            commands = tuple(
                action
                for action in sorted(
                    self._commands.values(),
                    key=lambda item: (item.not_before, item.created_at, item.action_id),
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
        with self._lock:
            forecasts = self._forecasts(timestamp)
            startup_estimates, startup_samples = self._learned_warm_estimates(
                now=timestamp
            )
            return {
                "schema_version": SCHEMA_VERSION,
                "mode": self.mode.value,
                "controller_epoch": self._controller_epoch,
                "plan_sequence": self._plan_sequence,
                "last_tick_at": self._last_tick_at,
                "last_tick_duration_seconds": self._last_tick_duration_seconds,
                "nodes": [node.to_dict() for node in sorted(nodes, key=lambda item: item.node_id)],
                "models": [
                    {
                        **profile.to_dict(),
                        "retiring": profile.model_id in self._retiring,
                    }
                    for profile in self.profiles
                ],
                "retiring_models": sorted(self._retiring),
                "forecasts": [asdict(item) for item in forecasts],
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

    def _append_record(self, record: MutationRecord) -> None:
        # One latest record per action/status transition is enough; repeated delivery acks are
        # idempotent and should not exhaust bounded history.
        if self._history and self._history[-1] == record:
            return
        self._history.append(record)
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

    def _forecasts(self, now: float) -> tuple[DemandForecast, ...]:
        active_models = tuple(
            model_id for model_id in self._profiles if model_id not in self._retiring
        )
        with self._demand_lock:
            return self.demand.forecasts(active_models, now=now)

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
            sequenced.append(replace(action, action_id=action_id))
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

    def _cancel_command(self, action: MutationAction, now: float, message: str) -> None:
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
            )
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
        self._trim_history()
        self._cancel_dependents(
            action.action_id,
            now,
            f"prerequisite {action.action_id} was cancelled",
        )
        self._restored_command_ids.discard(action.action_id)
        if not self._restored_command_ids:
            self._membership_recovery_started_at = None

    def _cancel_dependents(self, action_id: str, now: float, message: str) -> None:
        for dependent in list(self._commands.values()):
            if action_id in dependent.dependencies:
                self._cancel_command(dependent, now, message)

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
        payload = {
            "schema_version": SCHEMA_VERSION,
            "mode": self.mode.value,
            "controller_epoch": self._controller_epoch,
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
