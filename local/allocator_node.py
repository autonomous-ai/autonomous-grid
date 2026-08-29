"""Heartbeat and command loop for one allocator-managed local Grid host."""

from __future__ import annotations

import math
import os
import socket
import sys
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from local import runtime as local_runtime
from shared import jsonio
from shared.allocator.auth import (
    control_node_id,
    engine_node_id,
    secure_control_transport,
)
from shared.allocator.models import (
    ActionKind,
    MutationAction,
    NodeState,
    ResidencyState,
)
from shared.allocator.runtime import (
    ManagedModelRuntime,
    ManagedResidency,
    shutdown_request_path,
)
from shared.system.device_info import collect_device_info

DEFAULT_SHUTDOWN_DRAIN_TIMEOUT_SECONDS = 15.0
DEFAULT_SHUTDOWN_POLL_INTERVAL_SECONDS = 0.25
SHUTDOWN_REQUEST_POLL_SECONDS = 0.25
DEFAULT_REGISTRY_TTL_SECONDS = 60.0
MAX_REGISTRY_TTL_SECONDS = 300.0
MAX_SHUTDOWN_REQUEST_TIMEOUT_SECONDS = 1.0
DEFAULT_HEARTBEAT_CYCLE_TIMEOUT_SECONDS = 30.0
MAX_CHILD_ACTIVITY_WORKERS = 32
_ACTIVITY_UNSET = object()


class AllocatorNodeAgent:
    """Publish actual host state, receive desired-state commands, and expose ready children.

    A single control record advertises physical capacity and receives commands for the stable
    ``host_id``.  Each ready model has a separate engine record because llama.cpp binds one port per
    process.  Those records share the same host id, so the controller merges their activity without
    multiplying physical memory.
    """

    def __init__(
        self,
        *,
        grid_url: str,
        control_token: str,
        runtime: ManagedModelRuntime,
        advertise_host: str | None = None,
        client: httpx.Client | None = None,
        resource_collector: Callable[[], dict[str, Any]] = collect_device_info,
        heartbeat_interval: float = 15.0,
        shutdown_drain_timeout: float = DEFAULT_SHUTDOWN_DRAIN_TIMEOUT_SECONDS,
        shutdown_poll_interval: float = DEFAULT_SHUTDOWN_POLL_INTERVAL_SECONDS,
        registry_ttl_seconds: float = DEFAULT_REGISTRY_TTL_SECONDS,
        heartbeat_cycle_timeout: float = DEFAULT_HEARTBEAT_CYCLE_TIMEOUT_SECONDS,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        instance_id: str | None = None,
        startup_path: Path | None = None,
        allow_insecure_http: bool = False,
    ) -> None:
        if not control_token:
            raise ValueError("allocator node requires a node credential")
        if not math.isfinite(heartbeat_interval) or heartbeat_interval <= 0:
            raise ValueError("heartbeat_interval must be positive")
        if not math.isfinite(shutdown_drain_timeout) or shutdown_drain_timeout < 0:
            raise ValueError("shutdown_drain_timeout must be non-negative")
        if not math.isfinite(shutdown_poll_interval) or shutdown_poll_interval <= 0:
            raise ValueError("shutdown_poll_interval must be positive")
        if (
            not math.isfinite(registry_ttl_seconds)
            or registry_ttl_seconds < DEFAULT_REGISTRY_TTL_SECONDS
            or registry_ttl_seconds > MAX_REGISTRY_TTL_SECONDS
        ):
            raise ValueError("registry_ttl_seconds must be between 60 and 300 seconds")
        if not math.isfinite(heartbeat_cycle_timeout) or heartbeat_cycle_timeout <= 0:
            raise ValueError("heartbeat_cycle_timeout must be positive")
        if (
            heartbeat_interval >= registry_ttl_seconds
            or heartbeat_cycle_timeout >= registry_ttl_seconds
        ):
            raise ValueError(
                "heartbeat interval and cycle timeout must each be shorter than the "
                "registry TTL"
            )
        normalized_instance_id = str(instance_id or "").strip()
        if bool(normalized_instance_id) != bool(startup_path):
            raise ValueError("instance_id and startup_path must be provided together")
        self.grid_url = _validated_grid_url(
            grid_url,
            allow_insecure_http=allow_insecure_http,
        )
        self.control_token = control_token
        self.runtime = runtime
        self.shutdown_request_file = shutdown_request_path(runtime.state_path)
        # A request is scoped to one running daemon.  Consuming leftovers before registration
        # prevents yesterday's completed stop from killing a newly started node.
        self.shutdown_request_file.unlink(missing_ok=True)
        selected_host = advertise_host or local_runtime.detect_local_ip_for_url(self.grid_url)
        # Keep a canonical raw host internally. URL formatting belongs at the single endpoint
        # construction boundary; retaining brackets/%25 here double-encodes scoped IPv6 on its
        # second pass through shared.runtime.endpoint_for().
        self.advertise_host = _canonical_advertise_host(selected_host)
        self.client = client or httpx.Client(
            timeout=10.0,
            # A loopback HTTP credential must never follow HTTP_PROXY. HTTPS may safely use a
            # CONNECT proxy because the operator/host token remains inside end-to-end TLS.
            trust_env=urlsplit(self.grid_url).scheme == "https",
        )
        self.resource_collector = resource_collector
        self.heartbeat_interval = float(heartbeat_interval)
        self.shutdown_drain_timeout = float(shutdown_drain_timeout)
        self.shutdown_poll_interval = float(shutdown_poll_interval)
        self.registry_ttl_seconds = float(registry_ttl_seconds)
        self.heartbeat_cycle_timeout = float(heartbeat_cycle_timeout)
        self._sleep = sleeper
        self._monotonic = monotonic
        self.instance_id = normalized_instance_id
        self.startup_path = Path(startup_path) if startup_path is not None else None
        self._registered_at: float | None = None
        self.node_id = control_node_id(runtime.host_id)
        # Deterministic engine ids survive a node-agent restart. Seed every persisted model as a
        # tombstone so the first sync removes registry records for models that are no longer live.
        self._registered_engines: dict[str, int] = {
            residency.model_id: -1 for residency in runtime.residencies
        }
        self._engine_sync_cursor = 0
        # A replacement daemon cannot know whether deterministic child records from its persisted
        # residencies still exist or which port they advertise. Its first host record must remain
        # DRAINING until a complete child sync deletes or overwrites every startup tombstone.
        self._registry_cleanup_fenced = bool(self._registered_engines)
        self._control_registered = False
        # The server owns lease timestamps, so a client can never know whether a timed-out write
        # committed. Track the latest *attempt* that could have made a route accepting and use that
        # conservative boundary before stopping children without a confirmed routing fence.
        self._last_routable_registry_attempt_at: float | None = None
        self._resources: dict[str, Any] | None = None
        self._shutdown_complete = False
        self._shutdown_requested = threading.Event()
        self.last_error = ""

    @property
    def resources(self) -> dict[str, Any]:
        if self._resources is None:
            self._refresh_resources()
        assert self._resources is not None
        return dict(self._resources)

    def _refresh_resources(self) -> None:
        self._resources = _allocator_resources(
            self.resource_collector(),
            self.runtime.residencies,
        )

    def heartbeat_once(self) -> None:
        cycle_deadline = self._monotonic() + self.heartbeat_cycle_timeout
        cycle_error = ""
        try:
            self.runtime.evaluate_host()
        except OSError as exc:
            # The decision is installed in memory before its durable write. Local safety must still
            # reach the routing fence when a disk becomes read-only; keep the persistence failure
            # visible while continuing this heartbeat.
            cycle_error = f"could not persist local protection state: {exc}"
        # Capacity is telemetry, not machine identity. Refresh it before every registration and
        # command boundary so employee workloads and GPU use are visible to the next global plan.
        self._refresh_resources()

        # Renew the authoritative O(1) host lease before any per-model health or activity probe.
        # The latest local safety decision is already present, so this early keepalive cannot
        # briefly reopen a host that just entered PAUSED/UNHEALTHY protection.
        self._heartbeat_control_lease(deadline=cycle_deadline)

        reconcile_health = getattr(self.runtime, "reconcile_process_health", None)
        if callable(reconcile_health):
            try:
                reconcile_health(deadline=cycle_deadline, max_workers=16)
            except OSError as exc:
                # Health reconciliation installs the fail-safe in-memory residency before its
                # durable write. A read-only disk must not keep advertising a child that has just
                # been proven dead or unready; continue to the registry fence and surface the
                # persistence fault through node status.
                health_error = f"could not persist managed-process health: {exc}"
                cycle_error = f"{cycle_error}; {health_error}" if cycle_error else health_error

        # The early heartbeat deliberately carries no acknowledgements: a completed DRAIN receipt
        # may only reach the controller after its child route has published DRAINING below.
        deferred = self._sync_engine_nodes_or_fence(deadline=cycle_deadline)
        if deferred or self._monotonic() >= cycle_deadline:
            cycle_error = _child_sync_deadline_message(deferred)
            if self._deferred_requires_route_fence(deferred):
                # Health probing is allowed to consume the normal cycle budget, but a proven or
                # fail-safe non-routable child must not remain behind the ACCEPTING host lease that
                # was renewed at the start of this cycle. Give the safety fence its own bounded
                # request budget and retain it until a later complete child sync.
                self._publish_emergency_registry_fence()
            self._write_startup_marker()
            self.last_error = cycle_error
            return

        response, sent = self._post_control(deadline=cycle_deadline)
        self.runtime.mark_acknowledged(sent)
        commands = ((response.get("allocator") or {}).get("commands") or ())
        began = False
        for raw in commands:
            if not isinstance(raw, Mapping):
                continue
            try:
                command = MutationAction.from_dict(raw)
                if command.kind == ActionKind.WARM:
                    available_mb = self.resources.get("available_mb")
                    required_mb = _incremental_warm_memory_mb(
                        command,
                        self.runtime.residencies,
                    )
                    if not isinstance(available_mb, int) or available_mb < required_mb:
                        available_text = (
                            "unknown" if not isinstance(available_mb, int) else str(available_mb)
                        )
                        self.runtime.reject(
                            command,
                            "local capacity changed before warm: "
                            f"requires {required_mb} MB, available {available_text} MB",
                        )
                        began = True
                        continue
                began = self.runtime.begin(command) is not None or began
            except (KeyError, TypeError, ValueError) as exc:
                cycle_error = f"invalid allocator command: {exc}"
            except OSError as exc:
                # Runtime.begin is transactional: a failed state write rolls the RUNNING receipt
                # back, so the controller can safely redeliver. Keep the already-published route
                # fence and complete this heartbeat instead of turning a local disk fault into an
                # uncaught node-loop failure.
                command_error = f"could not persist allocator command start: {exc}"
                cycle_error = (
                    f"{cycle_error}; {command_error}" if cycle_error else command_error
                )
        if began:
            # Publish an immediate RUNNING/cancelled receipt instead of waiting a whole heartbeat.
            # Quick transitions (load/drain/unload) also get one chance to update routing now.
            self.runtime.wait_idle(
                min(0.05, max(0.0, cycle_deadline - self._monotonic()))
            )
            deferred = self._sync_engine_nodes_or_fence(deadline=cycle_deadline)
            if deferred or self._monotonic() >= cycle_deadline:
                cycle_error = _child_sync_deadline_message(deferred)
                if self._deferred_requires_route_fence(deferred):
                    # A command can make a previously READY child non-routable after the early
                    # ACCEPTING host lease and first child sync. If its immediate follow-up sync
                    # misses the ordinary cycle deadline, publish the same independent safety
                    # fence used for health transitions before retaining the receipt.
                    self._publish_emergency_registry_fence()
            else:
                _, sent = self._post_control(deadline=cycle_deadline)
                self.runtime.mark_acknowledged(sent)
        self._write_startup_marker()
        self.last_error = cycle_error

    def request_shutdown(self) -> None:
        """Request cooperative shutdown without interrupting an in-progress network call."""

        self._shutdown_requested.set()

    def run_forever(self) -> int:
        exit_code = 0
        credential_rejected = False
        try:
            while True:
                if self._shutdown_requested.is_set() or self._consume_shutdown_request():
                    break
                cycle_started = self._monotonic()
                try:
                    self.heartbeat_once()
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code in (401, 403):
                        self.last_error = str(exc)
                        print(
                            "Allocator node credential was rejected; keeping local runtimes "
                            "available until stale registry routes expire, then exiting.",
                            file=sys.stderr,
                        )
                        exit_code = 1
                        credential_rejected = True
                        break
                    self.last_error = str(exc)
                    print(f"Allocator heartbeat failed: {exc}", file=sys.stderr)
                except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
                    self.last_error = str(exc)
                    print(f"Allocator heartbeat failed: {exc}", file=sys.stderr)
                if self._wait_for_shutdown_request(
                    deadline=cycle_started + self.heartbeat_interval
                ):
                    break
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown_for_exit(wait_for_registry_expiry=credential_rejected)
        return exit_code

    def _shutdown_for_exit(self, *, wait_for_registry_expiry: bool) -> None:
        """Finish production shutdown without abandoning a retryable pre-fence failure.

        ``shutdown`` remains one-attempt and retryable for embedded callers. The daemon entry point,
        however, has no caller that can reuse this object after ``run_forever`` returns. Retry the
        DRAINING write while its last possibly-accepting lease is live; if control never recovers,
        stop only after that lease has certainly expired.
        """

        expiry_deadline = self._routable_registry_expiry_deadline()
        if wait_for_registry_expiry:
            self.shutdown(
                wait_for_registry_expiry=True,
                _registry_expiry_deadline=expiry_deadline,
            )
            return

        while True:
            try:
                self.shutdown()
                return
            except (httpx.HTTPError, OSError, RuntimeError, TypeError, ValueError) as exc:
                # Once begin_shutdown has run, the host fence was already confirmed. Preserve the
                # existing cleanup error for the CLI/supervisor; only a pre-fence failure needs this
                # process to stay alive and retry.
                if self.runtime.shutting_down:
                    raise
                self.last_error = str(exc)
                remaining = expiry_deadline - self._monotonic()
                if remaining <= 0:
                    self.shutdown(
                        wait_for_registry_expiry=True,
                        _registry_expiry_deadline=expiry_deadline,
                    )
                    return
                self._sleep(min(self.shutdown_poll_interval, remaining))

    def shutdown(
        self,
        *,
        force: bool = False,
        drain_timeout: float | None = None,
        wait_for_registry_expiry: bool = False,
        _registry_expiry_deadline: float | None = None,
    ) -> None:
        """Gate routing, drain proxy-owned requests, then stop and unregister local runtimes.

        ``force`` skips the bounded drain wait but still fences in-flight model startup before
        terminating processes.  Normal SIGTERM and CLI/grid shutdown use the graceful default.
        """

        if self._shutdown_complete:
            return
        timeout = self.shutdown_drain_timeout if drain_timeout is None else float(drain_timeout)
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("drain_timeout must be non-negative")

        deadline = self._monotonic()
        shutdown_error: BaseException | None = None
        routing_fenced = force
        registry_expired = False
        try:
            # A rejected credential cannot fence or delete its old records. Stopping immediately
            # would leave those records routable to dead ports. Keep serving for one bounded TTL.
            if wait_for_registry_expiry and not force:
                expiry_deadline = (
                    self._routable_registry_expiry_deadline()
                    if _registry_expiry_deadline is None
                    else float(_registry_expiry_deadline)
                )
                while self._monotonic() < expiry_deadline:
                    self._sleep(
                        min(
                            SHUTDOWN_REQUEST_POLL_SECONDS,
                            expiry_deadline - self._monotonic(),
                        )
                    )
                # No routable registry refresh happens during this wait. The fixed deadline is
                # anchored to the latest write attempt that might have committed, so a timed-out
                # heartbeat immediately before shutdown still receives its complete advertised
                # TTL. A second local stop request must not shorten this safety interval.
                registry_expired = True
            deadline = self._monotonic() + (
                MAX_SHUTDOWN_REQUEST_TIMEOUT_SECONDS if force else timeout
            )

            if not force and not registry_expired:
                if self._monotonic() >= deadline:
                    raise RuntimeError(
                        "allocator shutdown could not confirm its routing fence before the "
                        "graceful deadline"
                    )
                # Publish the authoritative host-wide DRAINING fence before begin_shutdown can
                # cancel a child that is still warming. Until this request succeeds, every old
                # registry route still points to a live process and shutdown remains retryable.
                self._heartbeat_shutdown_control(
                    deadline=min(
                        deadline,
                        self._monotonic() + MAX_SHUTDOWN_REQUEST_TIMEOUT_SECONDS,
                    )
                )
                routing_fenced = True

            self.runtime.begin_shutdown()
            live_models = {
                residency.model_id
                for residency in self.runtime.residencies
                if residency.handle is not None
            }
            if not force:
                pending = set(live_models)
                while pending:
                    if self._monotonic() >= deadline:
                        break
                    if registry_expired:
                        # A rejected credential cannot read the server-owned counters. Once the
                        # lease is no longer routable, llama.cpp's local slot table is the only
                        # authoritative way to let already-running streams finish.
                        activity = {
                            model_id: self._runtime_active_tasks(model_id)
                            for model_id in sorted(pending)
                        }
                    else:
                        activity = self._shutdown_engine_activity(
                            pending,
                            deadline=deadline,
                        )
                    pending = {
                        model_id
                        for model_id in pending
                        if activity.get(model_id) is None or activity[model_id] > 0
                    }
                    if not pending:
                        break
                    remaining = deadline - self._monotonic()
                    if remaining <= 0:
                        break
                    self._sleep(min(self.shutdown_poll_interval, remaining))
        except BaseException as exc:  # noqa: BLE001 - cleanup must continue before propagation
            shutdown_error = exc

        # A graceful stop may free a child port only after the registry has acknowledged a
        # host-wide route fence, or after every old lease has certainly aged out. In particular,
        # a network error while publishing DRAINING is not a best-effort cleanup condition: keep
        # serving and let a later shutdown retry the fence.
        if not (routing_fenced or registry_expired):
            assert shutdown_error is not None
            raise shutdown_error

        try:
            remaining = max(0.0, deadline - self._monotonic())
            self.runtime.stop_all(
                wait_timeout=0.0 if force else min(5.0, remaining),
                force=force,
            )
        except BaseException as exc:  # noqa: BLE001 - unregister dead/failed children before raise
            if shutdown_error is None:
                shutdown_error = exc

        # Best-effort cleanup is kept within the same absolute graceful deadline. Failed deletes
        # remain tombstones, so an explicit second shutdown call can retry instead of forgetting.
        if not wait_for_registry_expiry:
            for model_id in list(self._registered_engines):
                if self._monotonic() >= deadline:
                    break
                if self._delete_node(
                    engine_node_id(self.runtime.host_id, model_id),
                    deadline=min(
                        deadline,
                        self._monotonic() + MAX_SHUTDOWN_REQUEST_TIMEOUT_SECONDS,
                    ),
                ):
                    self._registered_engines.pop(model_id, None)
            # The host DRAINING record is the authoritative fallback fence for any child delete
            # that failed. Removing it would make a stale READY child routable again after its
            # process has stopped. Only unregister the host once no child tombstones remain; if
            # cleanup was incomplete, lease ordering makes the older child expire no later than
            # this newer host fence.
            if not self._registered_engines and self._delete_node(
                self.node_id,
                deadline=min(
                    deadline,
                    self._monotonic() + MAX_SHUTDOWN_REQUEST_TIMEOUT_SECONDS,
                ),
            ):
                self._control_registered = False
        if shutdown_error is None:
            self._remove_startup_marker()
            # A locally stopped runtime is safe behind the retained DRAINING fence, but keep this
            # object retryable until every reachable registry record is actually gone. The normal
            # daemon may exit and let leases expire; embedded callers can call shutdown again to
            # finish transient cleanup without resurrecting any process.
            self._shutdown_complete = wait_for_registry_expiry or (
                not self._registered_engines and not self._control_registered
            )
        else:
            raise shutdown_error

    def _register_control(self, *, deadline: float | None = None) -> None:
        envelope = self._allocator_envelope()
        envelope["max_concurrency"] = 0
        self._note_routable_registry_attempt()
        try:
            response = self.client.put(
                f"{self.grid_url}/nodes/{self.node_id}",
                headers=self._node_headers(),
                json={
                    "role": "allocator",
                    "models": [],
                    "name": socket.gethostname() or self.runtime.host_id,
                    "host_id": self.runtime.host_id,
                    "resources": self.resources,
                    "allocator": envelope,
                },
                **self._timeout_kwargs(deadline),
            )
        finally:
            # A slow or timed-out request can commit immediately before it completes locally. The
            # server lease therefore starts no earlier than this conservative completion sample.
            self._note_routable_registry_attempt()
        response.raise_for_status()
        self._observe_registry_ttl_response(response)
        self._control_registered = True

    def _post_control(
        self,
        *,
        deadline: float | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, str]]]:
        acknowledgements = self.runtime.acknowledgements()
        envelope = self._allocator_envelope()
        envelope["max_concurrency"] = 0
        self._note_routable_registry_attempt()
        try:
            response = self.client.post(
                f"{self.grid_url}/nodes/heartbeat",
                headers=self._node_headers(),
                json={
                    "node_id": self.node_id,
                    "resources": self.resources,
                    "allocator": envelope,
                    "acknowledgements": acknowledgements,
                },
                **self._timeout_kwargs(deadline),
            )
        finally:
            self._note_routable_registry_attempt()
        if response.status_code == 404:
            self._control_registered = False
            self._register_control(deadline=deadline)
            self._note_routable_registry_attempt()
            try:
                response = self.client.post(
                    f"{self.grid_url}/nodes/heartbeat",
                    headers=self._node_headers(),
                    json={
                        "node_id": self.node_id,
                        "resources": self.resources,
                        "allocator": envelope,
                        "acknowledgements": acknowledgements,
                    },
                    **self._timeout_kwargs(deadline),
                )
            finally:
                self._note_routable_registry_attempt()
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise TypeError("allocator heartbeat returned a non-object response")
        self._observe_registry_ttl(payload)
        return payload, acknowledgements

    def _sync_engine_nodes(self, *, deadline: float | None = None) -> tuple[str, ...]:
        active = {
            item.model_id: item
            for item in self.runtime.residencies
            if item.handle is not None
            and item.state in (ResidencyState.READY, ResidencyState.DRAINING)
        }
        retired = sorted(set(self._registered_engines).difference(active))
        if retired or any(
            residency.state == ResidencyState.DRAINING
            or residency.handle is None
            or self._registered_engines.get(model_id) != residency.handle.port
            for model_id, residency in active.items()
        ):
            # The early host lease may be ACCEPTING. From this point until every critical child
            # delete/fence/endpoint update succeeds, any exception must fall back to an independent
            # host-wide fence. A complete sync below is the sole place that clears this latch.
            self._registry_cleanup_fenced = True
        unsafe_retired: list[str] = []
        for index, model_id in enumerate(retired):
            if deadline is not None and self._monotonic() >= deadline:
                return tuple(retired[index:] + sorted(active))
            if self._delete_node(
                engine_node_id(self.runtime.host_id, model_id),
                deadline=deadline,
            ):
                self._registered_engines.pop(model_id, None)
                continue
            fenced, absent = self._fence_retired_engine(model_id, deadline=deadline)
            if absent:
                self._registered_engines.pop(model_id, None)
            elif not fenced:
                unsafe_retired.append(model_id)

        if unsafe_retired:
            # The model-specific record could not be deleted or fenced. Fall back to the
            # authoritative host control record before returning without acknowledgements. Keep
            # that fence across later cycles until every stale child route is safe.
            self._publish_emergency_registry_fence()
            return tuple(unsafe_retired + sorted(active))
        # DRAINING children are first because a dependent UNLOAD receipt must never be released
        # while that child's old accepting registry record remains routable. Rotate the READY tail
        # after a deadline-limited cycle so a persistently slow registry cannot starve high model
        # ids until their leases expire.
        draining = sorted(
            (
                item
                for item in active.items()
                if item[1].state == ResidencyState.DRAINING
            ),
            key=lambda item: item[0],
        )
        ready = sorted(
            (
                item
                for item in active.items()
                if item[1].state != ResidencyState.DRAINING
            ),
            key=lambda item: item[0],
        )
        ready_offset = self._engine_sync_cursor % len(ready) if ready else 0
        rotated_ready = ready[ready_offset:] + ready[:ready_offset]
        ordered = draining + rotated_ready
        activity, unsampled = self._parallel_runtime_activity(
            [residency for _, residency in ordered],
            deadline=deadline,
        )
        if unsampled:
            return tuple(model_id for model_id, _ in ordered)

        ready_processed = 0
        for index, (model_id, residency) in enumerate(ordered):
            if deadline is not None and self._monotonic() >= deadline:
                if ready:
                    self._engine_sync_cursor = (
                        ready_offset + ready_processed
                    ) % len(ready)
                return tuple(model for model, _ in ordered[index:])
            assert residency.handle is not None
            prior_port = self._registered_engines.get(model_id)
            if prior_port != residency.handle.port:
                self._register_engine(
                    residency,
                    deadline=deadline,
                    active_tasks=activity[model_id],
                )
                self._registered_engines[model_id] = residency.handle.port
                if residency.state != ResidencyState.DRAINING:
                    ready_processed += 1
                continue
            self._heartbeat_engine(
                residency,
                deadline=deadline,
                active_tasks=activity[model_id],
            )
            if residency.state != ResidencyState.DRAINING:
                ready_processed += 1
        if ready:
            self._engine_sync_cursor = (
                ready_offset + ready_processed
            ) % len(ready)
        # A host-wide fallback fence can reopen only after every surviving child has published its
        # own current state. The caller's final control heartbeat performs that ordered reopen.
        self._registry_cleanup_fenced = False
        return ()

    def _sync_engine_nodes_or_fence(
        self,
        *,
        deadline: float | None = None,
    ) -> tuple[str, ...]:
        """Run child sync and fence the host if a safety-critical update raises."""

        try:
            return self._sync_engine_nodes(deadline=deadline)
        except (httpx.HTTPError, OSError, RuntimeError, TypeError, ValueError) as exc:
            if self._registry_cleanup_fenced:
                try:
                    self._publish_emergency_registry_fence()
                except (httpx.HTTPError, OSError, RuntimeError, TypeError, ValueError) as fence_exc:
                    exc.add_note(f"emergency allocator route fence also failed: {fence_exc}")
            raise

    def _parallel_runtime_activity(
        self,
        residencies: list[ManagedResidency],
        *,
        deadline: float | None,
    ) -> tuple[dict[str, int | None], tuple[str, ...]]:
        if not residencies:
            return {}, ()
        if deadline is not None and self._monotonic() >= deadline:
            return {}, tuple(item.model_id for item in residencies)

        executor = ThreadPoolExecutor(
            max_workers=min(MAX_CHILD_ACTIVITY_WORKERS, len(residencies)),
            thread_name_prefix="grid-allocator-activity",
        )
        futures = {
            executor.submit(self._runtime_active_tasks, residency.model_id): residency.model_id
            for residency in residencies
        }
        timeout = (
            None
            if deadline is None
            else max(0.0, deadline - self._monotonic())
        )
        completed, pending = wait(futures, timeout=timeout)
        samples = {futures[future]: future.result() for future in completed}
        unsampled = tuple(
            sorted(futures[future] for future in pending)
        )
        for future in pending:
            future.cancel()
        # Production slot probes carry their own one-second HTTP timeout. Do not make the heartbeat
        # overrun its absolute deadline by synchronously joining a third-party probe that violates
        # that contract; at most this bounded worker set can linger until its probe returns.
        executor.shutdown(wait=not pending, cancel_futures=True)
        return samples, unsampled

    def _register_engine(
        self,
        residency: ManagedResidency,
        *,
        deadline: float | None = None,
        active_tasks: int | None | object = _ACTIVITY_UNSET,
    ) -> dict[str, Any]:
        assert residency.handle is not None
        node_id = engine_node_id(self.runtime.host_id, residency.model_id)
        # PUT is idempotent at this deterministic id, but its response can be lost after the server
        # commits a READY route. Install an unresolved tombstone before sending so every ambiguous
        # outcome retries PUT (rather than POSTing to a possibly stale port) and a later FAILED or
        # absent residency still deletes/fences the route. Only a fully validated response replaces
        # this sentinel with the current port.
        self._registered_engines[residency.model_id] = -1
        if active_tasks is _ACTIVITY_UNSET:
            active_tasks = self._runtime_active_tasks(residency.model_id)
        body: dict[str, Any] = {
            "role": "engine",
            "models": [residency.model_id],
            "endpoint_url": self.runtime.endpoint_for(
                residency.model_id,
                host=self.advertise_host,
            ),
            "name": f"managed:{residency.model_id}",
            "host_id": self.runtime.host_id,
            "resources": {
                **self.resources,
                "model_memory_mb": {residency.model_id: residency.memory_mb},
            },
            "allocator": self._engine_envelope(residency),
        }
        engine_api_key = getattr(self.runtime, "engine_api_key", "")
        if isinstance(engine_api_key, str) and engine_api_key:
            body["engine_api_key"] = engine_api_key
        if active_tasks is not None:
            body["load"] = {"active_tasks": active_tasks}
        routable_attempt = residency.state == ResidencyState.READY
        if routable_attempt:
            self._note_routable_registry_attempt()
        try:
            response = self.client.put(
                f"{self.grid_url}/nodes/{node_id}",
                headers=self._node_headers(),
                json=body,
                **self._timeout_kwargs(deadline),
            )
        finally:
            if routable_attempt:
                self._note_routable_registry_attempt()
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise TypeError("allocator engine registration returned a non-object response")
        self._observe_registry_ttl(payload)
        self._registered_engines[residency.model_id] = residency.handle.port
        return payload

    def _heartbeat_engine(
        self,
        residency: ManagedResidency,
        *,
        deadline: float | None = None,
        active_tasks: int | None | object = _ACTIVITY_UNSET,
    ) -> dict[str, Any]:
        node_id = engine_node_id(self.runtime.host_id, residency.model_id)
        if active_tasks is _ACTIVITY_UNSET:
            active_tasks = self._runtime_active_tasks(residency.model_id)
        body: dict[str, Any] = {
            "node_id": node_id,
            # Resource truth is refreshed on every child heartbeat. The server merges allocator
            # records by host; omitting this would let an old child record pin a superseded high
            # reserve after the control record reported reclaimed capacity.
            "resources": {
                **self.resources,
                "model_memory_mb": {residency.model_id: residency.memory_mb},
            },
            "allocator": self._engine_envelope(residency),
        }
        if active_tasks is not None:
            # llama.cpp's slot table sees both direct LAN calls and Grid-proxied calls. The server
            # keeps this sampled total separate from its exact proxy-owned in-flight counter.
            body["load"] = {"active_tasks": active_tasks}
        routable_attempt = residency.state == ResidencyState.READY
        if routable_attempt:
            self._note_routable_registry_attempt()
        try:
            response = self.client.post(
                f"{self.grid_url}/nodes/heartbeat",
                headers=self._node_headers(),
                json=body,
                **self._timeout_kwargs(deadline),
            )
        finally:
            if routable_attempt:
                self._note_routable_registry_attempt()
        if response.status_code == 404:
            return self._register_engine(
                residency,
                deadline=deadline,
                active_tasks=active_tasks,
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise TypeError("allocator engine heartbeat returned a non-object response")
        self._observe_registry_ttl(payload)
        usage = payload.get("model_last_used_at")
        if isinstance(usage, Mapping) and residency.model_id in usage:
            try:
                self.runtime.record_model_used(
                    residency.model_id,
                    float(usage[residency.model_id]),
                )
            except (TypeError, ValueError, OverflowError):
                pass
        return payload

    def _runtime_active_tasks(self, model_id: str) -> int | None:
        probe = getattr(self.runtime, "active_requests", None)
        if not callable(probe):
            return None
        try:
            value = probe(model_id)
        except (OSError, RuntimeError, TypeError, ValueError):
            return None
        if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            count = int(value)
        except (OverflowError, ValueError):
            return None
        if count != value or count < 0:
            return None
        return count

    def _allocator_envelope(self) -> dict[str, Any]:
        envelope = self.runtime.allocator_envelope()
        envelope["cost_per_hour"] = self.resources.get("cost_per_hour", 0.0)
        envelope["cost_known"] = "cost_per_hour" in self.resources
        envelope["host_priority"] = self.resources.get("host_priority", 0)
        return envelope

    def _engine_envelope(self, residency: ManagedResidency) -> dict[str, Any]:
        envelope = self._allocator_envelope()
        envelope["residencies"] = [
            row
            for row in envelope["residencies"]
            if row.get("model_id") == residency.model_id
        ]
        envelope["max_concurrency"] = 1
        if self.runtime.shutting_down or residency.state == ResidencyState.DRAINING:
            envelope["state"] = NodeState.DRAINING.value
            # The registry gives a decision object precedence over top-level state.  Omitting the
            # accepting local decision here makes the per-model drain authoritative for routing.
            envelope["decision"] = None
        return envelope

    def _delete_node(self, node_id: str, *, deadline: float | None = None) -> bool:
        if deadline is not None and self._monotonic() >= deadline:
            return False
        try:
            response = self.client.delete(
                f"{self.grid_url}/nodes/{node_id}",
                headers=self._node_headers(),
                **self._timeout_kwargs(deadline),
            )
            if response.status_code not in (200, 404):
                response.raise_for_status()
        except (httpx.HTTPError, OSError, RuntimeError, TypeError, ValueError):
            return False
        return True

    def _heartbeat_control_lease(self, *, deadline: float | None = None) -> None:
        """Renew the O(1) host lease without releasing action acknowledgements."""

        if not self._control_registered:
            if self._registry_cleanup_fenced:
                self._heartbeat_shutdown_control(deadline=deadline)
            else:
                self._register_control(deadline=deadline)
            return
        envelope = self._allocator_envelope()
        if self._registry_cleanup_fenced:
            envelope["state"] = NodeState.DRAINING.value
            envelope["decision"] = None
        envelope["max_concurrency"] = 0
        routable_attempt = not self._registry_cleanup_fenced
        if routable_attempt:
            self._note_routable_registry_attempt()
        try:
            response = self.client.post(
                f"{self.grid_url}/nodes/heartbeat",
                headers=self._node_headers(),
                json={
                    "node_id": self.node_id,
                    "resources": self.resources,
                    "allocator": envelope,
                    # This lease response is discarded. Only _post_control may poll commands and
                    # mark them delivered at the controller.
                    "request_commands": False,
                },
                **self._timeout_kwargs(deadline),
            )
        finally:
            if routable_attempt:
                self._note_routable_registry_attempt()
        if response.status_code == 404:
            self._control_registered = False
            if self._registry_cleanup_fenced:
                self._heartbeat_shutdown_control(deadline=deadline)
            else:
                self._register_control(deadline=deadline)
            return
        response.raise_for_status()
        self._observe_registry_ttl_response(response)

    def _fence_retired_engine(
        self,
        model_id: str,
        *,
        deadline: float | None = None,
    ) -> tuple[bool, bool]:
        """Fence one stale child after DELETE failure; return (fenced, already_absent)."""

        if deadline is not None and self._monotonic() >= deadline:
            return False, False
        envelope = self._allocator_envelope()
        envelope["state"] = NodeState.DRAINING.value
        envelope["decision"] = None
        envelope["max_concurrency"] = 0
        envelope["residencies"] = [
            row
            for row in envelope.get("residencies") or ()
            if isinstance(row, Mapping) and row.get("model_id") == model_id
        ]
        try:
            response = self.client.post(
                f"{self.grid_url}/nodes/heartbeat",
                headers=self._node_headers(),
                json={
                    "node_id": engine_node_id(self.runtime.host_id, model_id),
                    "allocator": envelope,
                },
                **self._timeout_kwargs(deadline),
            )
            if response.status_code == 404:
                return True, True
            response.raise_for_status()
        except (httpx.HTTPError, OSError, RuntimeError, TypeError, ValueError):
            return False, False
        return True, False

    def _heartbeat_shutdown_control(self, *, deadline: float | None = None) -> None:
        envelope = self._allocator_envelope()
        envelope["state"] = NodeState.DRAINING.value
        envelope["decision"] = None
        envelope["max_concurrency"] = 0
        if not self._control_registered:
            response = self.client.put(
                f"{self.grid_url}/nodes/{self.node_id}",
                headers=self._node_headers(),
                json={
                    "role": "allocator",
                    "models": [],
                    "name": socket.gethostname() or self.runtime.host_id,
                    "host_id": self.runtime.host_id,
                    "resources": self.resources,
                    "allocator": envelope,
                },
                **self._timeout_kwargs(deadline),
            )
            response.raise_for_status()
            self._observe_registry_ttl_response(response)
            self._control_registered = True
            return
        response = self.client.post(
            f"{self.grid_url}/nodes/heartbeat",
            headers=self._node_headers(),
            json={
                "node_id": self.node_id,
                "resources": self.resources,
                "allocator": envelope,
                "request_commands": False,
            },
            **self._timeout_kwargs(deadline),
        )
        if response.status_code == 404:
            self._control_registered = False
            self._heartbeat_shutdown_control(deadline=deadline)
            return
        response.raise_for_status()
        self._observe_registry_ttl_response(response)

    def _shutdown_engine_activity(
        self,
        model_ids: set[str],
        *,
        deadline: float | None = None,
    ) -> dict[str, int | None]:
        residencies = {item.model_id: item for item in self.runtime.residencies}
        activity: dict[str, int | None] = {}
        for model_id in sorted(model_ids):
            if deadline is not None and self._monotonic() >= deadline:
                activity[model_id] = None
                continue
            residency = residencies.get(model_id)
            if residency is None or residency.handle is None:
                activity[model_id] = 0
                continue
            try:
                operation_deadline = (
                    None
                    if deadline is None
                    else min(
                        deadline,
                        self._monotonic() + MAX_SHUTDOWN_REQUEST_TIMEOUT_SECONDS,
                    )
                )
                prior_port = self._registered_engines.get(model_id)
                if prior_port != residency.handle.port:
                    payload = self._register_engine(
                        residency,
                        deadline=operation_deadline,
                    )
                    self._registered_engines[model_id] = residency.handle.port
                else:
                    payload = self._heartbeat_engine(
                        residency,
                        deadline=operation_deadline,
                    )
                activity[model_id] = _response_active_tasks(payload)
            except (httpx.HTTPError, OSError, RuntimeError, TypeError, ValueError):
                # Unknown is deliberately not zero: when the control plane is unreachable we keep
                # the process alive until the bounded deadline rather than cutting a likely stream.
                activity[model_id] = None
        return activity

    def _deferred_requires_route_fence(self, deferred: tuple[str, ...]) -> bool:
        """Whether a deferred child may still have a more-permissive registry record."""

        residencies = {item.model_id: item for item in self.runtime.residencies}
        for model_id in deferred:
            prior_port = self._registered_engines.get(model_id)
            if prior_port is None:
                continue
            residency = residencies.get(model_id)
            if (
                residency is None
                or residency.state != ResidencyState.READY
                or residency.handle is None
                or residency.handle.port != prior_port
            ):
                return True
        return False

    def _publish_emergency_registry_fence(self) -> None:
        """Publish and retain a host-wide fence outside the ordinary cycle budget."""

        self._registry_cleanup_fenced = True
        self._heartbeat_shutdown_control(
            deadline=self._monotonic() + MAX_SHUTDOWN_REQUEST_TIMEOUT_SECONDS
        )

    def _note_routable_registry_attempt(self) -> None:
        attempted_at = self._monotonic()
        prior = self._last_routable_registry_attempt_at
        self._last_routable_registry_attempt_at = (
            attempted_at if prior is None else max(prior, attempted_at)
        )

    def _observe_registry_ttl_response(self, response: httpx.Response) -> None:
        try:
            payload = response.json()
        except (TypeError, ValueError):
            return
        if isinstance(payload, Mapping):
            self._observe_registry_ttl(payload)

    def _observe_registry_ttl(self, payload: Mapping[str, Any]) -> None:
        raw_ttl: Any = payload.get("ttl_seconds")
        nested = payload.get("node")
        if raw_ttl is None and isinstance(nested, Mapping):
            raw_ttl = nested.get("ttl_seconds")
        if isinstance(raw_ttl, bool) or not isinstance(raw_ttl, (int, float)):
            return
        ttl = float(raw_ttl)
        if math.isfinite(ttl) and ttl > 0:
            # Authenticated server metadata is authoritative. A newer server may retain routes
            # longer than this client's built-in contract; extending the local fallback is always
            # safe, while shortening it could stop children behind a still-accepting lease.
            self.registry_ttl_seconds = max(self.registry_ttl_seconds, ttl)

    def _routable_registry_expiry_deadline(self) -> float:
        now = self._monotonic()
        attempted_at = self._last_routable_registry_attempt_at
        if attempted_at is None or attempted_at > now:
            # A replacement daemon may inherit server records written by its predecessor without
            # having observed their renewal time. Likewise, an injected/restarted monotonic clock
            # can have no meaningful ordering against an in-memory prior sample. One complete TTL
            # from now is the only safe bound in either case.
            return now + self.registry_ttl_seconds
        return max(now, attempted_at + self.registry_ttl_seconds)

    def _node_headers(self) -> dict[str, str]:
        return {"X-Grid-Allocator-Node-Token": self.control_token}

    def _wait_for_shutdown_request(self, *, deadline: float | None = None) -> bool:
        if deadline is None:
            deadline = self._monotonic() + self.heartbeat_interval
        while True:
            if self._shutdown_requested.is_set() or self._consume_shutdown_request():
                return True
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return False
            self._sleep(min(SHUTDOWN_REQUEST_POLL_SECONDS, remaining))

    def _consume_shutdown_request(self) -> bool:
        if self.instance_id:
            try:
                request = jsonio.load_json(self.shutdown_request_file)
            except (OSError, SystemExit):
                self.shutdown_request_file.unlink(missing_ok=True)
                return False
            if request.get("instance_id") != self.instance_id:
                self.shutdown_request_file.unlink(missing_ok=True)
                return False
        try:
            self.shutdown_request_file.unlink()
        except FileNotFoundError:
            return False
        return True

    def _timeout_kwargs(self, deadline: float | None) -> dict[str, float]:
        if deadline is None:
            return {}
        remaining = max(0.001, deadline - self._monotonic())
        return {"timeout": remaining}

    def _write_startup_marker(self) -> None:
        if self.startup_path is None:
            return
        now = time.time()
        if self._registered_at is None:
            self._registered_at = now
        jsonio.atomic_write_json(
            self.startup_path,
            {
                "instance_id": self.instance_id,
                "pid": os.getpid(),
                "host_id": self.runtime.host_id,
                "registered_at": self._registered_at,
                "last_seen_at": now,
            },
        )

    def _remove_startup_marker(self) -> None:
        if self.startup_path is None:
            return
        try:
            marker = jsonio.load_json(self.startup_path)
        except (OSError, SystemExit):
            return
        if marker.get("instance_id") != self.instance_id:
            return
        try:
            self.startup_path.unlink()
        except FileNotFoundError:
            pass


def _response_active_tasks(payload: Mapping[str, Any]) -> int | None:
    raw_load: Any = payload.get("load")
    if raw_load is None and isinstance(payload.get("node"), Mapping):
        raw_load = payload["node"].get("load")
    if not isinstance(raw_load, Mapping):
        return None
    value = raw_load.get("active_tasks", 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    try:
        count = int(value)
    except (OverflowError, ValueError):
        return None
    return count if count == value else None


def _child_sync_deadline_message(deferred: tuple[str, ...]) -> str:
    suffix = f"; {len(deferred)} child record(s) deferred" if deferred else ""
    return f"allocator child registry sync exceeded its heartbeat deadline{suffix}"


def _incremental_warm_memory_mb(
    action: MutationAction,
    residencies: tuple[ManagedResidency, ...],
) -> int:
    """Return only memory a WARM would add beyond a proven resident process.

    Host telemetry already subtracts managed residency memory from ``available_mb``. Charging the
    full profile again would permanently reject a DRAINING rebound or proven FAILED-process
    replacement on a full host. A live FAILED handle is safe to charge incrementally because the
    runtime will either re-admit it or prove exact ownership and direct idleness before stopping it;
    ambiguous recovery fails closed. Handle-less records still require full free capacity.
    """

    current = next(
        (residency for residency in residencies if residency.model_id == action.model_id),
        None,
    )
    if (
        current is not None
        and current.handle is not None
        and current.state
        in (ResidencyState.READY, ResidencyState.DRAINING, ResidencyState.FAILED)
    ):
        return max(0, action.memory_mb - current.memory_mb)
    return action.memory_mb


def _canonical_advertise_host(value: str) -> str:
    """Return an unbracketed host with an RFC 6874 zone delimiter decoded exactly once."""

    formatted = local_runtime.url_host(value)
    if formatted.startswith("[") and formatted.endswith("]"):
        return formatted[1:-1].replace("%25", "%")
    return formatted


def _validated_grid_url(value: str, *, allow_insecure_http: bool) -> str:
    url = str(value).strip().rstrip("/")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
    except ValueError as exc:
        raise ValueError("allocator grid URL is invalid") from exc
    if not host or parsed.scheme not in ("http", "https"):
        raise ValueError("allocator grid URL must be an absolute HTTP(S) URL")
    if secure_control_transport(url) or (allow_insecure_http and parsed.scheme == "http"):
        return url
    raise ValueError(
        "refusing to send an allocator node credential over non-loopback HTTP; "
        "use HTTPS or explicitly allow insecure HTTP"
    )


def _allocator_resources(
    info: Mapping[str, Any],
    residencies: tuple[ManagedResidency, ...] = (),
) -> dict[str, Any]:
    usable_bytes = _nonnegative_int(info.get("usable_bytes"))
    backend = str(info.get("backend") or "cpu")
    machine = info.get("machine") if isinstance(info.get("machine"), Mapping) else {}
    memory = info.get("memory") if isinstance(info.get("memory"), Mapping) else {}
    platform = str(machine.get("platform") or "unknown")
    failure_domain = str(
        info.get("failure_domain")
        or machine.get("hostname")
        or socket.gethostname()
        or platform
    )
    managed_mb = sum(
        residency.memory_mb
        for residency in residencies
        if residency.handle is not None
        and residency.state != ResidencyState.CACHED
    )
    fallback_capacity_mb = usable_bytes // (1024 * 1024)
    capacity_mb = fallback_capacity_mb
    reserved_mb = 0
    available_mb: int | None = max(0, fallback_capacity_mb - managed_mb)

    if backend == "cuda":
        gpus = info.get("gpus") if isinstance(info.get("gpus"), list) else []
        total_mb = sum(
            _nonnegative_number(gpu.get("memory_total_mb"))
            for gpu in gpus
            if isinstance(gpu, Mapping)
        )
        used_mb = sum(
            _nonnegative_number(gpu.get("memory_used_mb"))
            for gpu in gpus
            if isinstance(gpu, Mapping)
        )
        if total_mb > 0:
            capacity_mb = int(total_mb)
            used_mb = min(total_mb, used_mb)
            reserved_mb = max(0, math.ceil(used_mb - managed_mb))
            available_mb = max(0, int(total_mb - used_mb))
    else:
        total_mb = _nonnegative_number(memory.get("total_gb")) * 1024
        available_value = memory.get("available_gb")
        available_system_mb = (
            _nonnegative_number(available_value) * 1024
            if available_value is not None
            else None
        )
        if total_mb > 0:
            static_reserve_mb = max(3 * 1024, math.ceil(total_mb * 0.15))
            capacity_mb = max(0, int(total_mb) - static_reserve_mb)
            if available_system_mb is None:
                available_mb = None
            else:
                available_system_mb = min(total_mb, available_system_mb)
                used_system_mb = total_mb - available_system_mb
                reserved_mb = max(
                    0,
                    math.ceil(used_system_mb - static_reserve_mb - managed_mb),
                )
                available_mb = max(
                    0,
                    capacity_mb - reserved_mb - managed_mb,
                )
    gpu_memory_mb = tuple(
        int(memory_mb)
        for gpu in (
            info.get("gpus") if isinstance(info.get("gpus"), list) else []
        )
        if isinstance(gpu, Mapping)
        and (memory_mb := _nonnegative_number(gpu.get("memory_total_mb"))) > 0
    )
    gpu_count = len(gpu_memory_mb)
    if backend == "metal":
        # Apple GPUs share the system memory pool. Represent that topology as one accelerator with
        # the same conservative usable capacity already enforced by the allocator.
        gpu_count = max(1, gpu_count)
        if not gpu_memory_mb and capacity_mb > 0:
            gpu_memory_mb = (capacity_mb,)
    return {
        "capacity_mb": capacity_mb,
        "reserved_mb": min(reserved_mb, capacity_mb),
        # This field is a local admission fence. The controller independently derives capacity from
        # capacity-reserved-residencies and ignores this convenience value.
        "available_mb": available_mb,
        "runtimes": ["llama.cpp"],
        "backends": [backend],
        "gpu_count": gpu_count,
        "gpu_memory_mb": list(gpu_memory_mb),
        "failure_domain": failure_domain,
        "tags": [platform, backend],
        "memory_bandwidth_gbps": _nonnegative_number(
            info.get("mem_bandwidth_gbps")
        ),
        "compute_gflops": _nonnegative_number(info.get("compute_gflops")),
        # Optional operator/accounting metadata. Device discovery cannot infer electricity,
        # depreciation, or rental price, but a deployment or logical test can provide it and the
        # global placement scorer already knows how to prefer cheaper eligible nodes.
        "cost_per_hour": _nonnegative_number(info.get("cost_per_hour")),
        "host_priority": _nonnegative_int(info.get("host_priority")),
        "allowed_data_tiers": ["public", "internal"],
    }


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _nonnegative_number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return number if math.isfinite(number) and number >= 0 else 0.0
