from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import secrets
import ssl
import statistics
import time
import uuid
from collections.abc import Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from local.runtime import GRID_TYPE
from shared.allocator.auth import control_node_id, engine_node_id, verify_node_token
from shared.allocator.controller import AllocatorController
from shared.allocator.models import (
    MAX_COUNTER,
    MAX_ID_LENGTH,
    MAX_MEMORY_MB,
    SCHEMA_VERSION,
    AllocatorMode,
    ModelPerformance,
    ModelProfile,
    ModelResidency,
    MutationAction,
    NodeSnapshot,
    NodeState,
    ResidencyState,
    stable_digest,
)
from shared.allocator.reconcile import MutationStatus
from shared.media import media_gating

NODE_TTL_SECONDS = 60
ENGINE_TIMEOUT_SECONDS = 600
MAX_REGISTRY_NODES = 10_000
MAX_PUBLIC_REGISTRY_NODES = 9_000
MAX_MANAGED_NODES_PER_HOST = 101
MAX_FUTURE_LEASE_SKEW_SECONDS = 30
MAX_MODEL_AGE_SECONDS = 10 * 365 * 24 * 60 * 60
ALLOCATOR_REVISION_WAIT_TIMEOUT_SECONDS = 10.0
# A destructive proof must survive not only the server-side revision wait but also response delivery
# and the managed node's immediate durable action-start boundary. Production node cycles are bounded
# at 30 seconds; a route with less remaining lease is inventory, not replacement evidence.
ALLOCATOR_DESTRUCTIVE_LEASE_MARGIN_SECONDS = 30.0
_ROUTABLE_NODE_STATES = frozenset((NodeState.ACCEPTING, NodeState.THROTTLED))


class NodeCreateRequest(BaseModel):
    role: Literal["engine", "app", "both", "allocator"] = "app"
    name: str | None = None


class NodeUpdateRequest(BaseModel):
    role: Literal["engine", "app", "both", "allocator"] = "engine"
    models: list[str] = Field(default_factory=list)
    endpoint_url: str | None = None
    media_url: str | None = None
    pricing: dict[str, Any] = Field(default_factory=dict)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    load: dict[str, Any] = Field(default_factory=dict)
    name: str | None = None
    # advertised model → the name the engine answers to (`--advertise-as` maps a consumer-facing alias
    # back to the engine's real model name). Empty means identity; only external `--at` aliases differ.
    upstream: dict[str, str] = Field(default_factory=dict)
    # Versioned allocator data is deliberately namespaced. `models` above remains the set that is
    # READY and routable right now; cached/loading/desired state must never leak into it.
    host_id: str | None = None
    resources: dict[str, Any] = Field(default_factory=dict)
    allocator: dict[str, Any] = Field(default_factory=dict)
    # Private hop credential for allocator-managed llama.cpp. It is accepted only on authenticated
    # managed registration and is never returned by discovery/status surfaces.
    engine_api_key: str | None = None


class HeartbeatRequest(BaseModel):
    node_id: str
    load: dict[str, Any] = Field(default_factory=dict)
    resources: dict[str, Any] = Field(default_factory=dict)
    allocator: dict[str, Any] = Field(default_factory=dict)
    acknowledgements: list[dict[str, Any]] = Field(default_factory=list)
    # Lease/fence heartbeats update registry truth but deliberately do not cross the durable
    # command-delivery boundary. Default true preserves the public heartbeat contract for older
    # nodes, whose ordinary control heartbeat has always polled commands.
    request_commands: bool = True


def _request_fields_set(value: BaseModel) -> set[str]:
    """Return explicitly supplied fields on both Pydantic 2 and Pydantic 1."""

    fields = getattr(value, "model_fields_set", None)
    if fields is None:
        fields = getattr(value, "__fields_set__", set())
    return set(fields or ())


@dataclass
class _ProxyModelPerformance:
    latency_ms: float = 0.0
    tokens_per_second: float = 0.0
    samples: int = 0
    updated_at: float = 0.0


@dataclass
class Node:
    node_id: str
    role: str
    models: list[str] = field(default_factory=list)
    endpoint_url: str | None = None
    media_url: str | None = None
    pricing: dict[str, Any] = field(default_factory=dict)
    capabilities: dict[str, Any] = field(default_factory=dict)
    load: dict[str, Any] = field(default_factory=dict)
    name: str | None = None
    upstream: dict[str, str] = field(default_factory=dict)
    host_id: str | None = None
    resources: dict[str, Any] = field(default_factory=dict)
    allocator: dict[str, Any] = field(default_factory=dict)
    engine_api_key: str = ""
    engine_tls_ca_pem: str = ""
    # Exact in-process work count owned by the proxy. It cannot be forged by a registration and is
    # the only reason a stale managed lease is retained as a drain tombstone.
    proxy_active_tasks: int = 0
    # Runtime-reported work is sampled from llama.cpp's slot table. For managed engines that count
    # already includes requests routed through this proxy, so the public load is the maximum of the
    # sampled runtime count and the exact live proxy count (not their double-counted sum).
    reported_active_tasks: int = 0
    # Server-measured, bounded performance EWMAs. These are allocator inputs, not public engine
    # metadata: a managed heartbeat cannot overwrite them, and discovery never exposes them.
    proxy_latency_ms: float = 0.0
    proxy_tokens_per_second: float = 0.0
    proxy_performance_samples: int = 0
    proxy_model_performance: dict[str, _ProxyModelPerformance] = field(
        default_factory=dict
    )
    # Server-owned request timestamps. Managed heartbeats must not overwrite these with a runtime
    # timestamp that predates work the proxy has already routed.
    model_last_used_at: dict[str, float] = field(default_factory=dict)
    first_seen_at: str = field(default_factory=lambda: _utc_now_iso())
    last_heartbeat: float = field(default_factory=time.time)

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("model_last_used_at", None)
        data.pop("proxy_active_tasks", None)
        data.pop("reported_active_tasks", None)
        data.pop("proxy_latency_ms", None)
        data.pop("proxy_tokens_per_second", None)
        data.pop("proxy_performance_samples", None)
        data.pop("proxy_model_performance", None)
        data.pop("engine_api_key", None)
        data.pop("engine_tls_ca_pem", None)
        data["last_heartbeat_at"] = datetime.fromtimestamp(
            self.last_heartbeat,
            UTC,
        ).isoformat()
        data["ttl_seconds"] = NODE_TTL_SECONDS
        return data


def _load_allocator_controller(
    state_path: Path | None,
) -> tuple[AllocatorController, str, str]:
    """Restore allocator state without making a corrupt control file a serving outage.

    The invalid file is moved aside rather than overwritten.  The warning remains visible on the
    status surface after later successful ticks so an operator can inspect or recover it.
    """

    try:
        return AllocatorController(state_path=state_path), "", ""
    except (
        Exception,  # noqa: BLE001 - state parsers may surface third-party exception types
        SystemExit,
    ) as exc:  # corrupted state may fail in JSON or schema code
        warning = f"Allocator state was invalid; recovered in recommend mode: {exc}"
        quarantine_path = ""
        recovered_state_path: Path | None = state_path
        if state_path is not None and state_path.exists():
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            candidate = state_path.with_name(
                f"{state_path.name}.invalid-{stamp}-{uuid.uuid4().hex[:8]}"
            )
            try:
                state_path.replace(candidate)
                quarantine_path = str(candidate)
            except OSError as quarantine_error:
                # Never overwrite a state file that we failed to preserve.
                recovered_state_path = None
                warning = (
                    f"{warning}; could not quarantine it, so persistence is disabled: "
                    f"{quarantine_error}"
                )
        return (
            AllocatorController(
                mode=AllocatorMode.RECOMMEND,
                state_path=recovered_state_path,
            ),
            warning,
            quarantine_path,
        )


def create_app(
    *,
    grid_id: str,
    grid_name: str,
    allocator_state_path: Path | None = None,
    allocator_control_token: str = "",
    allocator_interval_seconds: float = 15.0,
    allocator_coalesce_seconds: float = 0.1,
    allocator_min_tick_seconds: float = 0.25,
) -> FastAPI:
    if allocator_interval_seconds <= 0:
        raise ValueError("allocator_interval_seconds must be positive")
    if allocator_coalesce_seconds < 0:
        raise ValueError("allocator_coalesce_seconds must be non-negative")
    if allocator_min_tick_seconds < 0:
        raise ValueError("allocator_min_tick_seconds must be non-negative")

    @asynccontextmanager
    async def lifespan(lifespan_app: FastAPI):
        lifespan_app.state.allocator_running = True
        _mark_allocator_dirty(lifespan_app)
        if lifespan_app.state.allocator_task is None:
            lifespan_app.state.allocator_task = asyncio.create_task(
                _allocator_loop(lifespan_app)
            )
        try:
            yield
        finally:
            lifespan_app.state.allocator_running = False
            task = lifespan_app.state.allocator_task
            lifespan_app.state.allocator_task = None
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(
        title="Grid Local Signaling Server",
        description=(
            "Local engine discovery and OpenAI-compatible request proxy with authenticated "
            "allocator control."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.nodes = {}
    app.state.grid_id = grid_id
    app.state.grid_name = grid_name
    allocator, warning, quarantine_path = _load_allocator_controller(
        allocator_state_path
    )
    app.state.allocator = allocator
    app.state.allocator_control_token = allocator_control_token
    app.state.allocator_interval_seconds = float(allocator_interval_seconds)
    app.state.allocator_coalesce_seconds = float(allocator_coalesce_seconds)
    app.state.allocator_min_tick_seconds = float(allocator_min_tick_seconds)
    app.state.allocator_task = None
    app.state.allocator_running = False
    app.state.allocator_last_error = ""
    app.state.allocator_warning = warning
    app.state.allocator_state_quarantine = quarantine_path
    app.state.allocator_dirty_event = asyncio.Event()
    app.state.allocator_tick_lock = asyncio.Lock()
    app.state.allocator_tick_condition = asyncio.Condition()
    app.state.allocator_dirty_revision = 0
    app.state.allocator_processed_revision = 0
    app.state.allocator_safety_revision = 0
    app.state.allocator_processed_safety_revision = 0
    app.state.allocator_last_success_revision = 0
    app.state.allocator_last_success_safety_revision = 0
    app.state.allocator_last_error_revision = 0
    app.state.allocator_last_tick_monotonic = 0.0

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/")
    async def root():
        return _grid_info(app)

    @app.get("/grid/info")
    async def grid_info():
        return _grid_info(app)

    @app.post("/nodes")
    async def create_node(req: NodeCreateRequest):
        if req.role == "allocator":
            raise HTTPException(
                status_code=400,
                detail="allocator nodes must register with PUT and a stable host_id",
            )
        _ensure_registry_capacity(app, managed=False)
        node_id = str(uuid.uuid4())
        node = Node(node_id=node_id, role=req.role, name=req.name)
        _nodes(app)[node_id] = node
        return {"node_id": node_id, "role": req.role}

    @app.put("/nodes/{node_id}")
    async def update_node(node_id: str, req: NodeUpdateRequest, request: Request):
        _validate_public_registry_input(node_id, req.models)
        existing = _nodes(app).get(node_id)
        existing_host_id = _node_host_id(existing)
        incoming_host_id = _incoming_host_id(req.host_id, req.allocator)
        if (
            existing_host_id is not None
            and incoming_host_id is not None
            and incoming_host_id != existing_host_id
        ):
            raise HTTPException(
                status_code=409,
                detail="an allocator-managed node cannot change host_id",
            )
        managed = (
            _managed_node(existing)
            or req.role == "allocator"
            or req.host_id is not None
            or bool(req.allocator)
        )
        host_id = incoming_host_id or existing_host_id
        if managed:
            if host_id is None:
                raise HTTPException(
                    status_code=400,
                    detail="allocator-managed nodes require a stable host_id",
                )
            _require_allocator_node_control(app, request, host_id)
            _validate_managed_registry_identity(node_id, req.role, host_id, req.models)
            _validate_allocator_envelope(req.allocator)
            if req.engine_api_key is not None:
                _validate_engine_api_key(req.engine_api_key)
            if req.endpoint_url:
                _validate_managed_endpoint_transport(req.endpoint_url, request=request)
            if req.media_url:
                _validate_managed_endpoint_transport(req.media_url, request=request)
        elif req.engine_api_key is not None:
            raise HTTPException(
                status_code=400,
                detail="engine_api_key is reserved for allocator-managed engines",
            )
        if req.role in ("engine", "both") and not req.models:
            raise HTTPException(
                status_code=400, detail="at least one model is required for engines"
            )
        if req.role in ("engine", "both"):
            media_models = [
                model
                for model in req.models
                if _is_media_model(model, req.capabilities)
            ]
            text_models = [
                model for model in req.models if model not in set(media_models)
            ]
            if text_models and not req.endpoint_url:
                raise HTTPException(
                    status_code=400, detail="endpoint_url is required for text engines"
                )
            if media_models and not req.media_url:
                raise HTTPException(
                    status_code=400, detail="media_url is required for media engines"
                )
        transitioning_to_managed = managed and not _managed_node(existing)
        if existing is None or transitioning_to_managed:
            _ensure_registry_capacity(
                app,
                managed=managed,
                host_id=host_id,
                adds_record=existing is None,
            )
        node = existing or Node(node_id=node_id, role=req.role)
        node.role = req.role
        node.models = list(dict.fromkeys(req.models))
        node.endpoint_url = req.endpoint_url.rstrip("/") if req.endpoint_url else None
        node.media_url = req.media_url.rstrip("/") if req.media_url else None
        node.pricing = dict(req.pricing)
        node.capabilities = dict(req.capabilities)
        # The proxy owns its exact in-flight count; registration owns only the reported component.
        # Omission preserves telemetry, while an explicitly empty load resets the reported part.
        supplied_fields = _request_fields_set(req)
        if existing is None or "load" in supplied_fields:
            _set_reported_load(node, req.load, managed=managed)
        node.name = req.name
        node.upstream = dict(req.upstream)
        node.host_id = host_id
        node.resources = dict(req.resources)
        private_ca_pem = _managed_tls_ca_pem(req.allocator)
        node.allocator = dict(req.allocator)
        node.allocator.pop("engine_tls_ca_pem", None)
        if req.engine_api_key is not None:
            node.engine_api_key = req.engine_api_key
        node.engine_tls_ca_pem = private_ca_pem
        node.model_last_used_at = {
            model: timestamp
            for model, timestamp in node.model_last_used_at.items()
            if model in node.models
        }
        node.last_heartbeat = time.time()
        _nodes(app)[node_id] = node
        _mark_allocator_dirty(app)
        return {"status": "updated", "node": node.public_dict()}

    @app.post("/nodes/heartbeat")
    async def heartbeat(req: HeartbeatRequest, request: Request):
        node = _nodes(app).get(req.node_id)
        if not node:
            raise HTTPException(status_code=404, detail="node not found")
        existing_host_id = _node_host_id(node)
        incoming_host_id = _incoming_host_id(None, req.allocator)
        if (
            existing_host_id is not None
            and incoming_host_id is not None
            and incoming_host_id != existing_host_id
        ):
            raise HTTPException(
                status_code=409,
                detail="an allocator-managed node cannot change host_id",
            )
        managed = (
            _managed_node(node) or bool(req.allocator) or bool(req.acknowledgements)
        )
        if managed and not _managed_node(node):
            raise HTTPException(
                status_code=409,
                detail="managed nodes must establish their deterministic identity with PUT first",
            )
        host_id = incoming_host_id or existing_host_id
        if managed:
            if host_id is None:
                raise HTTPException(
                    status_code=400,
                    detail="allocator-managed heartbeats require a stable host_id",
                )
            _require_allocator_node_control(app, request, host_id)
            _validate_allocator_envelope(req.allocator)
        prior_safety_digest = _node_allocator_safety_digest(node)
        # A managed child reports runtime slot activity without owning the proxy's exact in-flight
        # request counter. Omission preserves the last runtime sample; explicit empty clears it.
        supplied_fields = _request_fields_set(req)
        if "load" in supplied_fields:
            _set_reported_load(node, req.load, managed=managed)
        if req.resources:
            node.resources = dict(req.resources)
        if req.allocator:
            private_ca_pem = _managed_tls_ca_pem(req.allocator)
            node.allocator = dict(req.allocator)
            node.allocator.pop("engine_tls_ca_pem", None)
            node.engine_tls_ca_pem = private_ca_pem
            node.host_id = host_id
        node.last_heartbeat = time.time()
        acknowledgement_results: list[dict[str, Any]] = []
        if req.acknowledgements:
            for acknowledgement in req.acknowledgements:
                action_id = str(acknowledgement.get("action_id") or "")
                supplied_status = str(acknowledgement.get("status") or "")
                try:
                    record = _allocator(app).acknowledge(
                        host_id or node.node_id,
                        action_id,
                        MutationStatus(supplied_status),
                        message=str(acknowledgement.get("message") or ""),
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    # A receipt can outlive bounded controller history or race cancellation. It is
                    # consumed, reported, and never allowed to poison later receipts/commands.
                    acknowledgement_results.append(
                        {
                            "action_id": action_id,
                            "status": supplied_status,
                            "result": "ignored",
                            "error": str(exc),
                        }
                    )
                    continue
                acknowledgement_results.append(
                    {
                        "action_id": action_id,
                        "status": record.status.value,
                        "result": "accepted",
                    }
                )
        revision = _mark_allocator_dirty(
            app,
            safety_changed=(
                prior_safety_digest != _node_allocator_safety_digest(node)
                or bool(req.acknowledgements)
            ),
        )
        # Only an explicit host command poll is the delivery boundary. Lease/fence heartbeats must
        # remain O(1): they publish safety state, mark the allocator dirty, and return without
        # waiting for a plan that the caller will intentionally discard.
        revision_processed = False
        if managed and node.role == "allocator" and req.request_commands:
            if app.state.allocator_running:
                revision_processed = await _await_allocator_revision(app, revision)
            else:
                await _run_allocator_tick(app)
                revision_processed = (
                    int(app.state.allocator_processed_revision) >= revision
                )
        commands = ()
        successful_revision = int(app.state.allocator_last_success_revision) >= revision
        error_blocks_revision = bool(app.state.allocator_last_error) and (
            int(app.state.allocator_last_error_revision) >= revision
        )
        if (
            managed
            and node.role == "allocator"
            and req.request_commands
            and revision_processed
            and successful_revision
            and not error_blocks_revision
        ):
            destructive_safety_current = int(
                app.state.allocator_last_success_safety_revision
            ) == int(app.state.allocator_safety_revision)
            commands = _allocator(app).commands_for(
                host_id or node.node_id,
                include_destructive=destructive_safety_current,
                destructive_safety_factory=(
                    (lambda: _allocator_snapshots(app))
                    if destructive_safety_current
                    else None
                ),
            )
        return {
            "ttl_seconds": NODE_TTL_SECONDS,
            "load": dict(node.load),
            "model_last_used_at": dict(node.model_last_used_at),
            "acknowledgements": acknowledgement_results,
            "allocator": {
                "mode": _allocator(app).mode.value,
                "commands": [_allocator_action_dict(item) for item in commands],
            },
        }

    @app.delete("/nodes/{node_id}")
    async def unregister(node_id: str, request: Request):
        existing = _nodes(app).get(node_id)
        if not existing:
            raise HTTPException(status_code=404, detail="node not found")
        if _managed_node(existing):
            host_id = _node_host_id(existing)
            if host_id is None:
                raise HTTPException(
                    status_code=400,
                    detail="allocator-managed nodes require a stable host_id",
                )
            _require_allocator_node_control(app, request, host_id)
        _nodes(app).pop(node_id, None)
        _mark_allocator_dirty(app)
        return {"status": "unregistered"}

    @app.get("/nodes/discover")
    async def discover(model: str | None = None):
        return {"engines": [_engine_dict(p) for p in _active_engines(app, model)]}

    @app.get("/v1/models")
    async def models():
        created = int(time.time())
        seen: set[str] = set()
        data = []
        for engine in _active_engines(app):
            for model in engine.models:
                if model in seen:
                    continue
                seen.add(model)
                data.append(
                    {
                        "id": model,
                        "object": "model",
                        "created": created,
                        "owned_by": "lan",
                    }
                )
        return {"object": "list", "data": data}

    @app.get("/allocator/status")
    async def allocator_status():
        return {
            **_allocator(app).status(_allocator_snapshots(app)),
            "last_error": app.state.allocator_last_error,
            "last_error_revision": app.state.allocator_last_error_revision,
            "dirty_revision": app.state.allocator_dirty_revision,
            "processed_revision": app.state.allocator_processed_revision,
            "last_success_revision": app.state.allocator_last_success_revision,
            "safety_revision": app.state.allocator_safety_revision,
            "processed_safety_revision": (
                app.state.allocator_processed_safety_revision
            ),
            "last_success_safety_revision": (
                app.state.allocator_last_success_safety_revision
            ),
            "warning": app.state.allocator_warning,
            "state_quarantine": app.state.allocator_state_quarantine,
        }

    @app.put("/allocator/models/{model_id:path}")
    async def allocator_put_model(model_id: str, request: Request):
        _require_allocator_control(app, request)
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(
                status_code=400, detail="Request body is not valid JSON"
            ) from exc
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=400, detail="Request body must be a JSON object"
            )
        body = {**body, "model_id": model_id}
        try:
            profile = ModelProfile.from_dict(body)
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _allocator(app).put_profile(profile)
        _mark_allocator_dirty(app)
        result = await _run_allocator_tick(app)
        return {
            "model": profile.to_dict(),
            "reconciliation": _reconcile_summary(result),
        }

    @app.delete("/allocator/models/{model_id:path}")
    async def allocator_delete_model(model_id: str, request: Request):
        _require_allocator_control(app, request)
        if not _allocator(app).remove_profile(model_id):
            raise HTTPException(
                status_code=404, detail="allocator model profile not found"
            )
        _mark_allocator_dirty(app)
        result = await _run_allocator_tick(app)
        return {"deleted": model_id, "reconciliation": _reconcile_summary(result)}

    @app.put("/allocator/mode")
    async def allocator_set_mode(request: Request):
        _require_allocator_control(app, request)
        try:
            body = await request.json()
            mode = AllocatorMode(str(body.get("mode")))
        except (AttributeError, json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail="mode must be one of observe, recommend, automatic",
            ) from exc
        if mode == AllocatorMode.AUTOMATIC and _allocator(app).state_path is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "automatic mode requires durable allocator state; repair the state path and "
                    "restart the signaling server"
                ),
            )
        _allocator(app).set_mode(mode)
        _mark_allocator_dirty(app)
        result = await _run_allocator_tick(app)
        return {"mode": mode.value, "reconciliation": _reconcile_summary(result)}

    @app.post("/allocator/tick")
    async def allocator_tick(request: Request):
        _require_allocator_control(app, request)
        _mark_allocator_dirty(app, safety_changed=False)
        return _reconcile_summary(await _run_allocator_tick(app))

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        return await _proxy_openai(app, "chat/completions", request)

    @app.post("/v1/completions")
    async def completions(request: Request):
        return await _proxy_openai(app, "completions", request)

    @app.post("/v1/responses")
    async def responses(request: Request):
        return await _proxy_openai(app, "responses", request)

    @app.post("/v1/feedback")
    async def feedback(request: Request):
        """What the human did with an answer — the one signal that makes unattended training honest.

        An app quotes back the `X-Grid-Request-Id` it received and says whether the answer was sent
        as-is, edited (with the corrected text — the most valuable row we can store), or discarded.
        No-ops when capture is off, so instrumenting an app is safe before anyone opts in.
        """
        try:
            payload = json.loads(await request.body() or b"{}")
        except json.JSONDecodeError:
            return _openai_error(400, "Request body is not valid JSON", "invalid_json")
        if not isinstance(payload, dict):
            return _openai_error(
                400, "Request body must be a JSON object", "invalid_request"
            )
        request_id = payload.get("request_id") or payload.get("id")
        verdict = payload.get("verdict")
        if not isinstance(request_id, str) or not request_id:
            return _openai_error(400, "request_id is required", "invalid_request")

        from train.capture import VERDICTS, load_policy, record_feedback

        if not isinstance(verdict, str) or verdict not in VERDICTS:
            return _openai_error(
                400, f"verdict must be one of: {', '.join(VERDICTS)}", "invalid_request"
            )
        if not load_policy().enabled:
            # Not an error: an instrumented app shouldn't break because the owner hasn't opted in.
            return {"stored": False, "reason": "collecting is off on this grid"}
        final_text = payload.get("final_text")
        stored = record_feedback(
            request_id,
            verdict,
            final_text=final_text if isinstance(final_text, str) else "",
        )
        return {"stored": bool(stored)}

    @app.post("/v1/media/image/generate")
    async def media_image_generate(request: Request):
        path = "media/image/generate"
        return await _proxy_media(
            app, path, media_gating.ENDPOINT_MODELS[path], request
        )

    @app.post("/v1/media/image/edit")
    async def media_image_edit(request: Request):
        path = "media/image/edit"
        return await _proxy_media(
            app, path, media_gating.ENDPOINT_MODELS[path], request
        )

    @app.post("/v1/media/video/i2v")
    async def media_i2v(request: Request):
        path = "media/video/i2v"
        return await _proxy_media(
            app, path, media_gating.ENDPOINT_MODELS[path], request
        )

    return app


async def _proxy_openai(app: FastAPI, endpoint_path: str, request: Request) -> Response:
    started_at = time.monotonic()
    raw_body = await request.body()
    try:
        body = json.loads(raw_body or b"{}")
    except json.JSONDecodeError:
        return _openai_error(400, "Request body is not valid JSON", "invalid_json")
    if not isinstance(body, dict):
        return _openai_error(
            400, "Request body must be a JSON object", "invalid_request"
        )
    model = body.get("model")
    if not isinstance(model, str) or not model:
        return _openai_error(400, "model is required", "invalid_request")

    engine = _choose_engine(app, model)
    if not engine:
        _observe_allocator_request(app, model, started_at, error=True, queue_depth=1)
        return _openai_error(
            503, f"No active local engine for model {model!r}", "engine_unavailable"
        )
    _mark_engine_used(engine, model)
    _change_active_tasks(engine, 1)

    # An external engine advertised under `--advertise-as` only knows its real model name; rewrite the
    # body's model alias→real before forwarding. Re-serialise only when it differs — otherwise forward
    # the original bytes untouched (no mapping / built-in / no alias, where advertised == real).
    upstream_model = engine.upstream.get(model)
    if upstream_model and upstream_model != model:
        raw_body = json.dumps({**body, "model": upstream_model}).encode()

    url = f"{engine.endpoint_url.rstrip('/')}/{endpoint_path}"
    headers = {"content-type": request.headers.get("content-type", "application/json")}
    if engine.engine_api_key:
        headers["authorization"] = f"Bearer {engine.engine_api_key}"
    timeout = httpx.Timeout(
        ENGINE_TIMEOUT_SECONDS,
        read=None if body.get("stream") else ENGINE_TIMEOUT_SECONDS,
    )

    if body.get("stream"):
        client = httpx.AsyncClient(
            timeout=timeout,
            trust_env=False,
            verify=_engine_tls_verify(engine),
        )
        engine_request = client.build_request(
            "POST", url, content=raw_body, headers=headers
        )
        try:
            engine_response = await client.send(engine_request, stream=True)
        except httpx.RequestError as exc:
            await client.aclose()
            _change_active_tasks(engine, -1)
            _observe_allocator_request(app, model, started_at, error=True)
            return _openai_error(502, f"Engine request failed: {exc}", "engine_error")

        # Streamed answers are captured too. Every chat interface streams, so skipping this branch
        # meant the autopilot never saw the traffic that matters most. The accumulator is bounded
        # and the store happens after the last chunk has already reached the client, so nothing
        # here can slow a stream down.
        collector = _StreamCollector(body) if _capture_enabled() else None

        async def stream_response():
            try:
                async for chunk in engine_response.aiter_raw():
                    if collector is not None:
                        collector.feed(chunk)
                    yield chunk
            finally:
                await engine_response.aclose()
                await client.aclose()
                _change_active_tasks(engine, -1)
                _observe_allocator_request(
                    app,
                    model,
                    started_at,
                    error=_allocator_capacity_error(engine_response.status_code),
                )
                _record_engine_performance(
                    engine,
                    started_at,
                    model=model,
                    status_code=engine_response.status_code,
                )
                if collector is not None:
                    collector.store()

        headers_stream = {}
        if collector is not None:
            headers_stream["X-Grid-Request-Id"] = collector.request_id

        return StreamingResponse(
            stream_response(),
            status_code=engine_response.status_code,
            media_type=engine_response.headers.get("content-type", "text/event-stream"),
            headers=headers_stream or None,
        )

    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            trust_env=False,
            verify=_engine_tls_verify(engine),
        ) as client:
            engine_response = await client.post(url, content=raw_body, headers=headers)
    except httpx.RequestError as exc:
        _change_active_tasks(engine, -1)
        _observe_allocator_request(app, model, started_at, error=True)
        return _openai_error(502, f"Engine request failed: {exc}", "engine_error")
    _change_active_tasks(engine, -1)
    _observe_allocator_request(
        app,
        model,
        started_at,
        error=_allocator_capacity_error(engine_response.status_code),
    )
    _record_engine_performance(
        engine,
        started_at,
        model=model,
        status_code=engine_response.status_code,
        response=engine_response,
    )

    headers_out = {}
    if engine_response.status_code == 200:
        # Learning from the work the grid is already doing (train/capture.py). Off unless the owner
        # turned it on, local-file-only, and wrapped so that nothing about it can fail a customer's
        # request — a capture problem must cost an example, never an answer.
        # X-Grid-Ref ties this answer to the record it is about (a ticket, a deal), so the
        # nightly cycle can ask that system what happened instead of waiting to be told.
        captured = _capture_exchange(
            body, engine_response, request.headers.get("x-grid-ref", "")
        )
        if captured:
            # The id an app quotes back on POST /v1/feedback to say what the human did with this
            # answer — the signal that makes unattended training honest.
            headers_out["X-Grid-Request-Id"] = captured

    return Response(
        content=engine_response.content,
        status_code=engine_response.status_code,
        media_type=engine_response.headers.get("content-type", "application/json"),
        headers=headers_out or None,
    )


# A body larger than this is a file dump, not a training example — and parsing multi-megabyte JSON
# a second time to look at it would be work done on a customer's request for nothing.
_MAX_CAPTURE_BODY = 256 * 1024


def _capture_enabled() -> bool:
    try:
        from train.capture import load_policy

        return load_policy().enabled
    except Exception:  # noqa: BLE001 — never let this decide anything about serving
        return False


class _StreamCollector:
    """Reassembles a streamed answer so it can become a training example.

    Two rules make this safe on a serving path: the buffer is bounded (a long stream stops being a
    candidate example rather than growing without limit), and nothing is stored until after the
    final chunk has been handed to the client.
    """

    def __init__(self, body: dict, limit: int = 16_000) -> None:
        self.request_id = uuid.uuid4().hex[:16]
        self._prompt = _prompt_text(body)
        self._model = str(body.get("model") or "")
        self._parts: list[str] = []
        self._size = 0
        self._limit = limit

    def feed(self, chunk: bytes) -> None:
        if self._size >= self._limit:
            return
        try:
            text = chunk.decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            return
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                delta = json.loads(payload)
            except json.JSONDecodeError:
                continue
            piece = _stream_delta(delta)
            if piece:
                self._parts.append(piece)
                self._size += len(piece)

    def store(self) -> None:
        try:
            from train.capture import clip, record

            answer = clip("".join(self._parts))
            if self._prompt and answer:
                record(
                    clip(self._prompt),
                    answer,
                    model=self._model,
                    request_id=self.request_id,
                )
        except Exception:  # noqa: BLE001 — a capture problem costs an example, never a response
            return


def _stream_delta(payload: dict) -> str:
    """The text in one streamed chunk, whichever dialect it arrived in."""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    delta = first.get("delta")
    if isinstance(delta, dict) and isinstance(delta.get("content"), str):
        return delta["content"]
    if isinstance(first.get("text"), str):
        return first["text"]
    return ""


def _capture_exchange(
    body: dict, engine_response: httpx.Response, ref: str = ""
) -> str | None:
    """Best-effort: store this prompt/answer pair if capture is enabled. Never raises, never waits.

    The expensive parts (redaction, the append) happen on capture's own writer thread; what runs
    here is a policy check, one JSON parse of a bounded body, and two string clips.
    """
    try:
        from train.capture import clip, load_policy, record

        policy = load_policy()
        if not policy.enabled:
            return None
        if len(engine_response.content) > _MAX_CAPTURE_BODY:
            return None
        prompt = clip(_prompt_text(body))
        answer = clip(_answer_text(engine_response.json()))
        if not prompt or not answer:
            return None
        return record(
            prompt, answer, model=str(body.get("model") or ""), policy=policy, ref=ref
        )
    except Exception:  # noqa: BLE001 — serving must be unaffected by anything in here
        return None


def _prompt_text(body: dict) -> str:
    """The request's text, whichever dialect it arrived in."""
    prompt = body.get("prompt")
    if isinstance(prompt, str):
        return prompt
    messages = body.get("messages")
    if isinstance(messages, list):
        # The last user turn is the work; earlier turns are context we don't train on directly.
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") == "user":
                content = message.get("content")
                if isinstance(content, str):
                    return content
    return ""


def _answer_text(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    if isinstance(first.get("text"), str):
        return first["text"]
    message = first.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    return ""


async def _proxy_media(
    app: FastAPI,
    endpoint_path: str,
    default_model: str,
    request: Request,
) -> Response:
    raw_body = await request.body()

    try:
        body = json.loads(raw_body or b"{}")
    except json.JSONDecodeError:
        return _openai_error(400, "Request body is not valid JSON", "invalid_json")

    if not isinstance(body, dict):
        return _openai_error(
            400, "Request body must be a JSON object", "invalid_request"
        )

    model = body.get("model") or default_model
    if not isinstance(model, str):
        return _openai_error(400, "model must be a string", "invalid_request")

    # Only a built-in name can be checked against the route; a non-builtin (an API media model) is
    # not this proxy's to validate — it either resolves to an engine below or 503s.
    if (
        media_gating.is_builtin_model(model)
        and media_gating.endpoint_for_model(model) != endpoint_path
    ):
        return _openai_error(
            400,
            f"Model {model!r} does not serve this endpoint. "
            f"/v1/{endpoint_path} serves {default_model!r}.",
            "invalid_request",
        )

    # media=True: only engines that actually advertise a media URL are candidates, so a text-only
    # or stale registration of the same model can never win the pick and 503 a healthy grid.
    engine = _choose_engine(app, model, media=True)

    if not engine:
        return _openai_error(
            503, f"No active local media engine for {model!r}", "engine_unavailable"
        )

    _mark_engine_used(engine, model)
    _change_active_tasks(engine, 1)
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(ENGINE_TIMEOUT_SECONDS, read=None),
        trust_env=False,
        verify=_engine_tls_verify(engine),
    )
    media_request = client.build_request(
        "POST",
        f"{engine.media_url.rstrip('/')}/{endpoint_path}",
        content=raw_body,
        headers={
            "content-type": request.headers.get("content-type", "application/json"),
            **(
                {"authorization": f"Bearer {engine.engine_api_key}"}
                if engine.engine_api_key
                else {}
            ),
        },
    )
    try:
        engine_response = await client.send(media_request, stream=True)
    except httpx.RequestError as exc:
        await client.aclose()
        _change_active_tasks(engine, -1)
        return _openai_error(502, f"Engine media request failed: {exc}", "engine_error")

    async def stream_response():
        try:
            async for chunk in engine_response.aiter_raw():
                yield chunk
        finally:
            await engine_response.aclose()
            await client.aclose()
            _change_active_tasks(engine, -1)

    return StreamingResponse(
        stream_response(),
        status_code=engine_response.status_code,
        media_type=engine_response.headers.get("content-type", "text/event-stream"),
    )


def _is_media_model(model: str, capabilities: dict[str, Any]) -> bool:
    """Whether an advertised model is served over the media routes rather than `/v1/chat`.

    The engine's own capability envelope decides (``endpoints: ["media"]``) — that is what the
    engine advertises for BOTH built-in and API media models, and it is what the relay uses in
    remote mode. The `comfyui:` prefix is the fallback for an engine that sent no capabilities,
    which is every pre-existing local media engine; keying off the prefix ALONE would classify a
    vendor media model (`doggi:*`) as a text engine and demand an `endpoint_url` it has no use for.
    """
    entry = ((capabilities or {}).get("models") or {}).get(model) or {}
    endpoints = entry.get("endpoints")
    if isinstance(endpoints, list) and endpoints:
        return "media" in endpoints
    return media_gating.is_builtin_model(model)


def _grid_info(app: FastAPI) -> dict[str, Any]:
    return {
        "grid_id": app.state.grid_id,
        "name": app.state.grid_name,
        "grid_type": GRID_TYPE,
        "auth_required": False,
        "lan_only": True,
        "node_ttl_seconds": NODE_TTL_SECONDS,
        "engines_online": len(_active_engines(app)),
    }


def _nodes(app: FastAPI) -> dict[str, Node]:
    return app.state.nodes


def _purge_stale_nodes(app: FastAPI, *, now: float | None = None) -> int:
    timestamp = time.time() if now is None else now
    stale_ids = [
        node_id
        for node_id, node in _nodes(app).items()
        if (
            timestamp - node.last_heartbeat > NODE_TTL_SECONDS
            or node.last_heartbeat > timestamp + MAX_FUTURE_LEASE_SKEW_SECONDS
        )
        # A managed streaming request owns this exact Node object's server-side counter. Retain it
        # as a short-lived tombstone until the response finalizer decrements the counter; replacing
        # it under the deterministic ID would falsely report zero and permit an unsafe UNLOAD.
        and not (_managed_node(node) and node.proxy_active_tasks > 0)
    ]
    for node_id in stale_ids:
        _nodes(app).pop(node_id, None)
    return len(stale_ids)


def _ensure_registry_capacity(
    app: FastAPI,
    *,
    managed: bool,
    host_id: str | None = None,
    adds_record: bool = True,
) -> None:
    _purge_stale_nodes(app)
    if adds_record and len(_nodes(app)) >= MAX_REGISTRY_NODES:
        raise HTTPException(
            status_code=503,
            detail="node registry capacity is exhausted; retry after stale leases expire",
        )
    if (
        adds_record
        and not managed
        and sum(not _managed_node(node) for node in _nodes(app).values())
        >= (MAX_PUBLIC_REGISTRY_NODES)
    ):
        raise HTTPException(
            status_code=503,
            detail="public node registry capacity is exhausted; retry after stale leases expire",
        )
    if (
        managed
        and host_id is not None
        and sum(_node_host_id(node) == host_id for node in _nodes(app).values())
        >= MAX_MANAGED_NODES_PER_HOST
    ):
        raise HTTPException(
            status_code=503,
            detail="managed host registry capacity is exhausted",
        )


def _active_engines(app: FastAPI, model: str | None = None) -> list[Node]:
    now = time.time()
    _purge_stale_nodes(app, now=now)
    engines: list[Node] = []
    live_nodes = [
        node for node in _nodes(app).values() if _node_lease_is_live(node, now=now)
    ]
    host_control_states: dict[str, NodeState] = {}
    state_rank = {state: index for index, state in enumerate(NodeState)}
    for node in live_nodes:
        if node.role != "allocator":
            continue
        host_id = _node_host_id(node)
        if host_id is None:
            continue
        state = _node_allocator_state(node)
        current = host_control_states.get(host_id)
        if current is None or state_rank[state] > state_rank[current]:
            host_control_states[host_id] = state

    for node in live_nodes:
        if node.role not in ("engine", "both"):
            continue
        if _node_allocator_state(node) not in _ROUTABLE_NODE_STATES:
            continue
        decision = (
            node.allocator.get("decision") if isinstance(node.allocator, dict) else None
        )
        if isinstance(decision, dict) and decision.get("accept") is False:
            continue
        host_id = _node_host_id(node)
        if (
            host_id is not None
            and host_control_states.get(host_id, NodeState.ACCEPTING)
            not in _ROUTABLE_NODE_STATES
        ):
            continue
        if model and model not in node.models:
            continue
        engines.append(node)
    engines.sort(
        key=lambda item: (
            _route_priority(item),
            _route_load_score(item),
            _load_score(item.load),
            _route_lease_age(item, now=now),
            item.node_id,
        )
    )
    return engines


def _node_lease_is_live(node: Node, *, now: float) -> bool:
    """Match routing eligibility without trusting retained stale/future tombstones."""

    return (
        now - node.last_heartbeat <= NODE_TTL_SECONDS
        and node.last_heartbeat <= now + MAX_FUTURE_LEASE_SKEW_SECONDS
    )


def _node_lease_has_allocator_safety_headroom(node: Node, *, now: float) -> bool:
    """Require a READY route to outlive delivery through the node action-start boundary."""

    return (
        now - node.last_heartbeat
        < NODE_TTL_SECONDS - ALLOCATOR_DESTRUCTIVE_LEASE_MARGIN_SECONDS
        and node.last_heartbeat <= now + MAX_FUTURE_LEASE_SKEW_SECONDS
    )


def _node_allocator_state(node: Node) -> NodeState:
    allocator = node.allocator if isinstance(node.allocator, dict) else {}
    value = allocator.get("state")
    decision = allocator.get("decision")
    if isinstance(decision, dict):
        value = decision.get("state", value)
    try:
        return NodeState(str(value or NodeState.ACCEPTING))
    except ValueError:
        return NodeState.UNHEALTHY


def _choose_engine(app: FastAPI, model: str, *, media: bool = False) -> Node | None:
    """The least-loaded live engine serving ``model``.

    ``media=True`` additionally requires an advertised ``media_url``. Without that filter, an engine
    that lists a media model but cannot serve it — a text-only registration, or a stale node left by
    an older/hand-rolled registration — can win the pick purely on load and turn a working grid into
    a hard 503, even while a healthy media engine sits right beside it.
    """
    # Inventory/discovery remains stable while an engine is busy. Admission capacity is a routing
    # concern only; applying it in _active_engines made /grid/info, discovery, and /v1/models
    # flicker to empty whenever a healthy single-concurrency engine served one request.
    engines = [
        engine
        for engine in _active_engines(app, model)
        if (
            (limit := _node_concurrency_limit(engine)) is None
            or _load_score(engine.load) < limit
        )
    ]
    if media:
        engines = [engine for engine in engines if engine.media_url]
    now = time.time()
    latency_baseline = _route_latency_baseline(engines, model=model, now=now)
    engines.sort(
        key=lambda engine: (
            _route_priority(engine),
            _route_expected_completion_score(
                engine,
                model=model,
                now=now,
                latency_baseline_ms=latency_baseline,
            ),
            _route_load_score(engine),
            _route_lease_age(engine, now=now),
            engine.node_id,
        )
    )
    return engines[0] if engines else None


def _engine_dict(node: Node) -> dict[str, Any]:
    data = node.public_dict()
    data.pop("last_heartbeat", None)
    data["max_concurrency"] = _node_concurrency_limit(node)
    return data


def _load_score(load: dict[str, Any]) -> float:
    active = load.get("active_tasks")
    if (
        not isinstance(active, bool)
        and isinstance(active, (int, float))
        and active >= 0
    ):
        return float(active)
    return 0.0


def _route_priority(node: Node) -> float:
    """Prefer fully accepting hosts while still using throttled capacity when needed."""

    if _node_allocator_state(node) != NodeState.THROTTLED:
        return 0.0
    decision = (
        node.allocator.get("decision") if isinstance(node.allocator, dict) else None
    )
    value = decision.get("priority_multiplier") if isinstance(decision, dict) else 0.5
    try:
        multiplier = float(value)
    except (TypeError, ValueError, OverflowError):
        multiplier = 0.0
    if not math.isfinite(multiplier):
        multiplier = 0.0
    return 1.0 - min(1.0, max(0.0, multiplier))


def _route_load_score(node: Node) -> float:
    """Compare heterogeneous engines by occupied fraction, not raw request count.

    Every candidate has already passed model-locality filtering, so routing locality is binary and
    equal. The remaining load term is current occupancy divided by the effective admission width.
    Engines without a declared width retain the legacy raw-load score rather than inventing
    capacity, while a zero-width engine sorts last (and is independently rejected for admission).
    """

    active = _load_score(node.load)
    limit = _node_concurrency_limit(node)
    if limit is None:
        return active
    if limit <= 0:
        return math.inf
    return active / limit


def _route_lease_age(node: Node, *, now: float) -> float:
    """Prefer fresh equivalent engines without rewarding a future-skewed heartbeat."""

    return max(0.0, now - node.last_heartbeat)


_ROUTE_PERFORMANCE_TTL_SECONDS = 900.0
_ROUTE_PERFORMANCE_FULL_SAMPLES = 8


def _route_latency_baseline(
    engines: list[Node],
    *,
    model: str,
    now: float,
) -> float:
    measurements = [
        performance.latency_ms
        for engine in engines
        if (performance := _fresh_route_performance(engine, model=model, now=now))
        is not None
        and performance.latency_ms > 0
    ]
    return statistics.median(measurements) if measurements else 1.0


def _fresh_route_performance(
    node: Node,
    *,
    model: str,
    now: float,
) -> _ProxyModelPerformance | None:
    performance = node.proxy_model_performance.get(model)
    if performance is None or performance.samples <= 0 or performance.latency_ms <= 0:
        return None
    age = now - performance.updated_at
    if not -MAX_FUTURE_LEASE_SKEW_SECONDS <= age < _ROUTE_PERFORMANCE_TTL_SECONDS:
        return None
    return performance


def _route_expected_completion_score(
    node: Node,
    *,
    model: str,
    now: float,
    latency_baseline_ms: float,
) -> float:
    performance = _fresh_route_performance(node, model=model, now=now)
    estimated_latency = latency_baseline_ms
    if performance is not None:
        age = max(0.0, now - performance.updated_at)
        confidence = min(1.0, performance.samples / _ROUTE_PERFORMANCE_FULL_SAMPLES)
        freshness = max(0.0, 1.0 - age / _ROUTE_PERFORMANCE_TTL_SECONDS)
        weight = confidence * freshness
        estimated_latency = (
            weight * performance.latency_ms + (1.0 - weight) * latency_baseline_ms
        )
    active = _load_score(node.load)
    limit = _node_concurrency_limit(node)
    service_waves = active + 1.0 if limit is None else (active + 1.0) / max(1, limit)
    return estimated_latency * service_waves


def _node_concurrency_limit(node: Node) -> int | None:
    allocator = node.allocator if isinstance(node.allocator, dict) else {}
    value = allocator.get("max_concurrency", node.load.get("max_concurrency"))
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        maximum = int(value)
    except (OverflowError, ValueError):
        return None
    if maximum < 0 or maximum != value:
        return None
    if maximum == 0:
        return 0
    multiplier = 1.0
    if _node_allocator_state(node) == NodeState.THROTTLED:
        decision = allocator.get("decision")
        raw = (
            decision.get("concurrency_multiplier")
            if isinstance(decision, dict)
            else 0.5
        )
        try:
            multiplier = float(raw)
        except (TypeError, ValueError, OverflowError):
            multiplier = 0.0
        if not math.isfinite(multiplier):
            multiplier = 0.0
        multiplier = min(1.0, max(0.0, multiplier))
    return max(0, math.ceil(maximum * multiplier))


def _allocator(app: FastAPI) -> AllocatorController:
    return app.state.allocator


def _managed_node(node: Node | None) -> bool:
    return bool(
        node is not None
        and (node.role == "allocator" or node.host_id is not None or node.allocator)
    )


def _validate_public_registry_input(node_id: str, models: list[str]) -> None:
    if not node_id or len(node_id) > MAX_ID_LENGTH:
        raise HTTPException(
            status_code=400, detail="node_id is outside the supported range"
        )
    if len(models) > 4_096:
        raise HTTPException(status_code=400, detail="too many advertised models")
    if any(not model or len(model) > MAX_ID_LENGTH for model in models):
        raise HTTPException(
            status_code=400, detail="model id is outside the supported range"
        )


def _validate_managed_registry_identity(
    node_id: str,
    role: str,
    host_id: str,
    models: list[str],
) -> None:
    """Prevent one host credential from squatting another host's registry IDs."""

    try:
        if role == "allocator":
            expected = control_node_id(host_id)
        elif role in ("engine", "both"):
            if len(models) != 1:
                raise ValueError(
                    "a managed engine record must advertise exactly one model"
                )
            expected = engine_node_id(host_id, models[0])
        else:
            raise ValueError("a managed registry record must be an allocator or engine")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not secrets.compare_digest(node_id, expected):
        raise HTTPException(
            status_code=409,
            detail="managed node_id does not match its authenticated host and role",
        )


def _validate_allocator_envelope(value: dict[str, Any]) -> None:
    schema_version = value.get("schema_version", SCHEMA_VERSION)
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise HTTPException(
            status_code=400,
            detail="allocator schema_version must be an integer",
        )
    if schema_version != SCHEMA_VERSION:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported allocator schema_version {schema_version}",
        )
    rows = value.get("residencies")
    if rows is None:
        return
    if not isinstance(rows, list):
        raise HTTPException(
            status_code=400,
            detail="allocator residencies must be a JSON array",
        )
    if len(rows) > 4_096:
        raise HTTPException(status_code=400, detail="too many allocator residencies")
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise HTTPException(
                status_code=400,
                detail="each allocator residency must be a JSON object",
            )
        try:
            residency = ModelResidency.from_dict(row)
        except (KeyError, OverflowError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if residency.model_id in seen:
            raise HTTPException(
                status_code=400,
                detail=f"duplicate allocator residency for {residency.model_id!r}",
            )
        seen.add(residency.model_id)
        for field_name in ("loaded_age_seconds", "last_used_age_seconds"):
            if field_name in row and _bounded_model_age(row.get(field_name)) is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"{field_name} must be finite and between 0 and "
                    f"{MAX_MODEL_AGE_SECONDS}",
                )


def _validate_engine_api_key(value: str) -> None:
    if (
        not isinstance(value, str)
        or not 32 <= len(value) <= 512
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise HTTPException(
            status_code=400,
            detail="engine_api_key must be 32-512 visible ASCII characters",
        )


def _bounded_model_age(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        age = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(age) or age < 0 or age > MAX_MODEL_AGE_SECONDS:
        return None
    return age


def _validate_managed_endpoint_transport(value: str, *, request: Request) -> None:
    """Require authenticated managed traffic to be TLS or same-machine loopback."""

    try:
        parsed = urlsplit(str(value))
        host = parsed.hostname or ""
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="managed endpoint URL is invalid"
        ) from exc
    if parsed.username is not None or parsed.password is not None:
        raise HTTPException(
            status_code=400,
            detail="managed endpoint URL must not contain user information",
        )
    if parsed.scheme == "https" and host:
        return
    peer_host = request.client.host if request.client is not None else ""
    local_test_client = peer_host == "testclient"
    if (
        parsed.scheme == "http"
        and _literal_loopback_host(host)
        and (_literal_loopback_host(peer_host) or local_test_client)
    ):
        return
    raise HTTPException(
        status_code=400,
        detail=(
            "managed non-loopback endpoints require end-to-end HTTPS; "
            "plaintext HTTP is allowed only when both endpoint and registering peer are "
            "literal loopback addresses"
        ),
    )


def _literal_loopback_host(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _managed_tls_ca_pem(allocator: Mapping[str, Any]) -> str:
    raw = allocator.get("engine_tls_ca_pem")
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise HTTPException(status_code=400, detail="engine TLS CA must be PEM text")
    try:
        size = len(raw.encode("ascii"))
    except UnicodeEncodeError as exc:
        raise HTTPException(
            status_code=400, detail="engine TLS CA must be ASCII PEM"
        ) from exc
    if size > 65_536 or "-----BEGIN CERTIFICATE-----" not in raw:
        raise HTTPException(
            status_code=400,
            detail="engine TLS CA must contain at most 64 KiB of PEM certificates",
        )
    try:
        context = ssl.create_default_context()
        context.load_verify_locations(cadata=raw)
    except (OSError, ssl.SSLError) as exc:
        raise HTTPException(
            status_code=400, detail="engine TLS CA PEM is invalid"
        ) from exc
    return raw


def _engine_tls_verify(engine: Node) -> ssl.SSLContext | bool:
    if not engine.engine_tls_ca_pem:
        return True
    context = ssl.create_default_context()
    context.load_verify_locations(cadata=engine.engine_tls_ca_pem)
    return context


def _node_host_id(node: Node | None) -> str | None:
    if node is None:
        return None
    if isinstance(node.host_id, str) and node.host_id:
        return node.host_id
    if isinstance(node.allocator, dict):
        value = node.allocator.get("host_id")
        if isinstance(value, str) and value:
            return value
    return None


def _incoming_host_id(
    explicit_host_id: str | None,
    allocator: dict[str, Any],
) -> str | None:
    envelope_host_id: str | None = None
    if "host_id" in allocator and allocator.get("host_id") is not None:
        value = allocator.get("host_id")
        if not isinstance(value, str) or not value:
            raise HTTPException(
                status_code=400,
                detail="allocator host_id must be a non-empty string",
            )
        envelope_host_id = value
    if explicit_host_id is not None and not explicit_host_id:
        raise HTTPException(
            status_code=400, detail="host_id must be a non-empty string"
        )
    if (
        explicit_host_id is not None
        and envelope_host_id is not None
        and explicit_host_id != envelope_host_id
    ):
        raise HTTPException(
            status_code=409,
            detail="top-level and allocator host_id values must match",
        )
    return explicit_host_id or envelope_host_id


def _allocator_control_valid(app: FastAPI, request: Request) -> bool:
    expected = str(app.state.allocator_control_token or "")
    if not expected:
        return False
    supplied = request.headers.get("x-grid-allocator-token", "")
    authorization = request.headers.get("authorization", "")
    if not supplied and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    return bool(supplied) and secrets.compare_digest(supplied, expected)


def _require_allocator_control(app: FastAPI, request: Request) -> None:
    if not _allocator_control_valid(app, request):
        raise HTTPException(
            status_code=403,
            detail="A valid local allocator control token is required",
        )


def _allocator_node_control_valid(app: FastAPI, request: Request, host_id: str) -> bool:
    supplied = request.headers.get("x-grid-allocator-node-token", "")
    if not supplied:
        return False
    try:
        verify_node_token(
            supplied,
            str(app.state.allocator_control_token or ""),
            host_id,
        )
    except (TypeError, ValueError):
        return False
    return True


def _require_allocator_node_control(
    app: FastAPI,
    request: Request,
    host_id: str,
) -> None:
    if not _allocator_node_control_valid(app, request, host_id):
        raise HTTPException(
            status_code=403,
            detail="A valid host-scoped allocator node token is required",
        )


def _node_allocator_safety_digest(node: Node) -> str:
    """Fingerprint allocator semantics while ignoring lease and relative-age churn."""

    allocator = dict(node.allocator) if isinstance(node.allocator, dict) else {}
    resources = node.resources if isinstance(node.resources, dict) else {}
    # The local node also reports volatile launch telemetry such as available_mb. Local admission
    # rechecks it before WARM, but the global snapshot deliberately does not use it for placement or
    # destructive safety. Hash only fields consumed by _allocator_snapshots so unrelated telemetry
    # cannot continuously invalidate every model's destructive delivery cut.
    safety_resources = {
        key: resources.get(key)
        for key in (
            "capacity_mb",
            "memory_capacity_mb",
            "vram_mb",
            "reserved_mb",
            "backends",
            "backend",
            "runtimes",
            "runtime",
            "failure_domain",
            "allowed_data_tiers",
            "tags",
            "model_memory_mb",
        )
        if key in resources
    }
    decision = allocator.get("decision")
    if isinstance(decision, Mapping):
        # evaluated_at, retry countdowns, and persisted debounce bookkeeping move every lease but
        # do not alter routing or destructive safety until one of these effective fields changes.
        allocator["decision"] = {
            key: decision.get(key)
            for key in (
                "state",
                "accept",
                "concurrency_multiplier",
                "priority_multiplier",
            )
            if key in decision
        }
    raw_residencies = allocator.get("residencies")
    if isinstance(raw_residencies, list):
        normalized_residencies: list[dict[str, Any] | Any] = []
        for row in raw_residencies:
            if not isinstance(row, Mapping):
                normalized_residencies.append(row)
                continue
            normalized = dict(row)
            # Remote ages rise on every identical heartbeat. Their corresponding remote absolute
            # timestamps remain stable for one residency and reset when it is actually reloaded or
            # used, so retain those timestamps and exclude only the derived countdown fields.
            normalized.pop("loaded_age_seconds", None)
            normalized.pop("last_used_age_seconds", None)
            normalized_residencies.append(normalized)
        allocator["residencies"] = sorted(
            normalized_residencies,
            key=lambda row: (
                str(row.get("model_id") or "")
                if isinstance(row, Mapping)
                else repr(row)
            ),
        )
    return stable_digest(
        {
            "role": node.role,
            "models": sorted(node.models),
            "endpoint_url": node.endpoint_url,
            "media_url": node.media_url,
            "upstream": node.upstream,
            "host_id": node.host_id,
            "resources": safety_resources,
            "allocator": allocator,
            "load": node.load,
            "proxy_active_tasks": node.proxy_active_tasks,
            "reported_active_tasks": node.reported_active_tasks,
            "model_last_used_at": node.model_last_used_at,
        }
    )


def _mark_allocator_dirty(app: FastAPI, *, safety_changed: bool = True) -> int:
    app.state.allocator_dirty_revision += 1
    if safety_changed:
        app.state.allocator_safety_revision += 1
    app.state.allocator_dirty_event.set()
    return int(app.state.allocator_dirty_revision)


def _allocator_tick_sync(
    controller: AllocatorController,
    snapshots: tuple[NodeSnapshot, ...],
):
    try:
        return controller.tick(snapshots), ""
    except Exception as exc:  # noqa: BLE001 - control-loop bugs must not interrupt inference
        # The controller owns a last-known-good plan. Broad containment here is deliberate: an
        # allocator plugin or persistence failure is a status warning, never a serving failure.
        return None, str(exc)


async def _run_allocator_tick(app: FastAPI):
    async with app.state.allocator_tick_lock:
        target_revision = int(app.state.allocator_dirty_revision)
        target_safety_revision = int(app.state.allocator_safety_revision)
        try:
            snapshots = _allocator_snapshots(app)
            result, error = await asyncio.to_thread(
                _allocator_tick_sync,
                _allocator(app),
                snapshots,
            )
        except Exception as exc:  # noqa: BLE001 - preserve the allocator worker boundary
            result, error = None, str(exc)
        app.state.allocator_last_error = error
        if error:
            app.state.allocator_last_error_revision = target_revision
        else:
            app.state.allocator_last_error_revision = 0
            app.state.allocator_last_success_revision = max(
                int(app.state.allocator_last_success_revision),
                target_revision,
            )
            app.state.allocator_last_success_safety_revision = max(
                int(app.state.allocator_last_success_safety_revision),
                target_safety_revision,
            )
        app.state.allocator_last_tick_monotonic = time.monotonic()
        app.state.allocator_processed_revision = max(
            int(app.state.allocator_processed_revision),
            target_revision,
        )
        app.state.allocator_processed_safety_revision = max(
            int(app.state.allocator_processed_safety_revision),
            target_safety_revision,
        )
        if int(app.state.allocator_dirty_revision) == target_revision:
            app.state.allocator_dirty_event.clear()
        else:
            app.state.allocator_dirty_event.set()
        async with app.state.allocator_tick_condition:
            app.state.allocator_tick_condition.notify_all()
        return result


async def _await_allocator_revision(
    app: FastAPI,
    revision: int,
    *,
    timeout_seconds: float = ALLOCATOR_REVISION_WAIT_TIMEOUT_SECONDS,
) -> bool:
    if int(app.state.allocator_processed_revision) >= revision:
        return True

    async def wait_until_processed() -> None:
        async with app.state.allocator_tick_condition:
            await app.state.allocator_tick_condition.wait_for(
                lambda: int(app.state.allocator_processed_revision) >= revision
            )

    try:
        await asyncio.wait_for(wait_until_processed(), timeout=timeout_seconds)
    except TimeoutError:
        # A stalled allocator cannot stall a node heartbeat. The persistent error surface and next
        # periodic iteration retain enough evidence for an operator to diagnose it.
        return False
    return int(app.state.allocator_processed_revision) >= revision


async def _allocator_loop(app: FastAPI) -> None:
    next_periodic = time.monotonic()
    while True:
        delay = max(0.0, next_periodic - time.monotonic())
        try:
            await asyncio.wait_for(
                app.state.allocator_dirty_event.wait(), timeout=delay
            )
        except TimeoutError:
            pass

        if (
            app.state.allocator_dirty_event.is_set()
            and app.state.allocator_coalesce_seconds
        ):
            await asyncio.sleep(app.state.allocator_coalesce_seconds)

        since_last = time.monotonic() - float(app.state.allocator_last_tick_monotonic)
        minimum = float(app.state.allocator_min_tick_seconds)
        if app.state.allocator_dirty_event.is_set() and since_last < minimum:
            await asyncio.sleep(minimum - since_last)

        await _run_allocator_tick(app)
        next_periodic = time.monotonic() + float(app.state.allocator_interval_seconds)


@dataclass(frozen=True, slots=True)
class _AllocatorSnapshotMember:
    snapshot: NodeSnapshot
    role: str
    malformed: bool = False
    admitted_models: frozenset[str] = frozenset()
    safety_lease: bool = False


def _allocator_snapshots(app: FastAPI) -> tuple[NodeSnapshot, ...]:
    profiles = {profile.model_id: profile for profile in _allocator(app).profiles}
    raw: list[_AllocatorSnapshotMember] = []
    now = time.time()
    _purge_stale_nodes(app, now=now)
    for node in _nodes(app).values():
        allocator = node.allocator if isinstance(node.allocator, dict) else {}
        resources = node.resources if isinstance(node.resources, dict) else {}
        host_id = str(node.host_id or allocator.get("host_id") or node.node_id)
        capacity_mb = _first_nonnegative_int(
            resources.get("capacity_mb"),
            resources.get("memory_capacity_mb"),
            resources.get("vram_mb"),
            node.load.get("memory_total_mb"),
        )
        reserved_mb = _first_nonnegative_int(resources.get("reserved_mb"))
        node_active_requests = _first_nonnegative_int(node.load.get("active_tasks"))
        residencies_by_model: dict[str, ModelResidency] = {}
        raw_residencies = allocator.get("residencies")
        malformed = raw_residencies is not None and not isinstance(
            raw_residencies, list
        )
        rows = raw_residencies if isinstance(raw_residencies, list) else ()
        for row in rows:
            if not isinstance(row, dict):
                malformed = True
                continue
            try:
                loaded_age = _bounded_model_age(row.get("loaded_age_seconds"))
                last_used_age = _bounded_model_age(row.get("last_used_age_seconds"))
                # Timestamps originate on another wall clock. Age evidence is relative to that
                # same clock, so reconstruct server-local timestamps at receipt time. Legacy or
                # malformed rows receive age zero: conservative freshness can delay an unload but
                # can never bypass minimum residency or cooldown.
                normalized_row = {
                    **row,
                    "loaded_at": max(
                        0.0,
                        node.last_heartbeat
                        - (loaded_age if loaded_age is not None else 0.0),
                    ),
                    "last_used_at": max(
                        0.0,
                        node.last_heartbeat
                        - (last_used_age if last_used_age is not None else 0.0),
                    ),
                }
                residency = ModelResidency.from_dict(normalized_row)
                if (
                    node.role in ("engine", "both")
                    and residency.model_id in node.models
                ):
                    residency = replace(
                        residency,
                        last_used_at=max(
                            residency.last_used_at,
                            node.model_last_used_at.get(residency.model_id, 0.0),
                        ),
                        active_requests=max(
                            residency.active_requests,
                            node_active_requests,
                        ),
                    )
                prior = residencies_by_model.get(residency.model_id)
                if prior is not None:
                    malformed = True
                    residency = replace(
                        prior,
                        memory_mb=max(prior.memory_mb, residency.memory_mb),
                        active_requests=max(
                            prior.active_requests,
                            residency.active_requests,
                        ),
                    )
                residencies_by_model[residency.model_id] = residency
            except (KeyError, OverflowError, TypeError, ValueError):
                malformed = True
                continue
        residencies = list(residencies_by_model.values())
        known = {item.model_id for item in residencies}
        model_memory = resources.get("model_memory_mb") or {}
        for model_id in node.models:
            if model_id in known:
                continue
            profile = profiles.get(model_id)
            memory_mb = _positive_int(
                model_memory.get(model_id) if isinstance(model_memory, dict) else None,
                profile.memory_mb if profile else None,
                max(1, capacity_mb // max(1, len(node.models))),
            )
            residencies.append(
                ModelResidency(
                    model_id=model_id,
                    memory_mb=memory_mb,
                    state=ResidencyState.READY,
                    loaded_at=node.last_heartbeat,
                    last_used_at=node.model_last_used_at.get(model_id, 0.0),
                    managed=bool(allocator.get("managed", False)),
                    active_requests=(
                        node_active_requests if node.role in ("engine", "both") else 0
                    ),
                )
            )
        resident_memory = min(
            MAX_MEMORY_MB,
            sum(
                item.memory_mb
                for item in residencies
                if item.state not in (ResidencyState.CACHED, ResidencyState.FAILED)
            ),
        )
        capacity_mb = min(
            MAX_MEMORY_MB,
            max(capacity_mb, reserved_mb + resident_memory),
        )
        gpu_memory_mb = _gpu_memory_tuple(resources)
        state_value = allocator.get("state")
        decision = allocator.get("decision")
        if isinstance(decision, dict):
            state_value = decision.get("state", state_value)
        try:
            state = NodeState(str(state_value or NodeState.ACCEPTING))
        except ValueError:
            state = NodeState.UNHEALTHY
        if "allowed_data_tiers" in allocator:
            allowed_tiers = _string_tuple(allocator.get("allowed_data_tiers"))
        elif "allowed_data_tiers" in resources:
            allowed_tiers = _string_tuple(resources.get("allowed_data_tiers"))
        else:
            allowed_tiers = ("public", "internal")
        if malformed:
            state = NodeState.UNHEALTHY
        raw.append(
            _AllocatorSnapshotMember(
                role=node.role,
                malformed=malformed,
                admitted_models=(
                    frozenset(node.models)
                    if (
                        node.role in ("engine", "both")
                        and _node_lease_has_allocator_safety_headroom(
                            node,
                            now=now,
                        )
                        and state in _ROUTABLE_NODE_STATES
                        and not (
                            isinstance(decision, dict)
                            and decision.get("accept") is False
                        )
                    )
                    else frozenset()
                ),
                safety_lease=_node_lease_has_allocator_safety_headroom(
                    node,
                    now=now,
                ),
                snapshot=NodeSnapshot(
                    node_id=host_id,
                    capacity_mb=capacity_mb,
                    reserved_mb=min(reserved_mb, capacity_mb),
                    backends=_string_tuple(
                        resources.get("backends") or resources.get("backend")
                    ),
                    runtimes=_string_tuple(
                        resources.get("runtimes") or resources.get("runtime")
                    ),
                    state=state,
                    failure_domain=str(
                        allocator.get("failure_domain")
                        or resources.get("failure_domain")
                        or ""
                    ),
                    allowed_data_tiers=allowed_tiers,
                    allowed_models=_string_tuple(allocator.get("allowed_models")),
                    denied_models=_string_tuple(allocator.get("denied_models")),
                    tags=_string_tuple(allocator.get("tags") or resources.get("tags")),
                    max_models=_optional_nonnegative_int(allocator.get("max_models")),
                    residencies=tuple(residencies),
                    cached_models=_string_tuple(allocator.get("cached_models")),
                    active_requests=node_active_requests,
                    max_concurrency=_first_nonnegative_int(
                        allocator.get("max_concurrency"),
                        node.load.get("max_concurrency"),
                        1 if node.role in ("engine", "both") else 0,
                    ),
                    queue_depth=_first_nonnegative_int(node.load.get("queue_depth")),
                    tokens_per_second=(
                        node.proxy_tokens_per_second
                        or _nonnegative_float(node.load.get("tokens_per_second"))
                    ),
                    latency_ms=(
                        node.proxy_latency_ms
                        or _nonnegative_float(node.load.get("latency_ms"))
                    ),
                    model_performance=tuple(
                        ModelPerformance(
                            model_id=model_id,
                            latency_ms=performance.latency_ms,
                            tokens_per_second=performance.tokens_per_second,
                            sample_count=performance.samples,
                            updated_at=performance.updated_at,
                        )
                        for model_id, performance in sorted(
                            node.proxy_model_performance.items()
                        )
                        if model_id in node.models
                    ),
                    memory_bandwidth_gbps=_nonnegative_float(
                        resources.get("memory_bandwidth_gbps")
                    ),
                    compute_gflops=_nonnegative_float(resources.get("compute_gflops")),
                    gpu_count=max(
                        len(gpu_memory_mb),
                        _first_nonnegative_int(
                            resources.get("gpu_count"),
                            node.load.get("gpu_count"),
                        ),
                    ),
                    gpu_memory_mb=gpu_memory_mb,
                    cost_per_hour=_nonnegative_float(allocator.get("cost_per_hour")),
                    host_priority=_first_nonnegative_int(
                        allocator.get("host_priority")
                    ),
                    last_heartbeat=node.last_heartbeat,
                    mutation_cooldown_until=_nonnegative_float(
                        allocator.get("mutation_cooldown_until")
                    ),
                    actuator_capabilities=(
                        ()
                        if malformed
                        else _string_tuple(allocator.get("actuator_capabilities"))
                    ),
                    manually_managed=bool(
                        malformed or allocator.get("manually_managed", not allocator)
                    ),
                ),
            )
        )
    return _merge_allocator_hosts(raw)


def _merge_allocator_hosts(
    nodes: list[_AllocatorSnapshotMember],
) -> tuple[NodeSnapshot, ...]:
    grouped: dict[str, list[_AllocatorSnapshotMember]] = {}
    for member in nodes:
        grouped.setdefault(member.snapshot.node_id, []).append(member)
    merged: list[NodeSnapshot] = []
    state_rank = {state: index for index, state in enumerate(NodeState)}
    residency_rank = {
        ResidencyState.FAILED: 0,
        ResidencyState.CACHED: 1,
        ResidencyState.LOADING: 2,
        ResidencyState.WARMING: 3,
        ResidencyState.READY: 4,
        ResidencyState.DRAINING: 5,
    }
    for host_id, records in sorted(grouped.items()):
        members = [record.snapshot for record in records]
        malformed = any(record.malformed for record in records)
        control_records = [record for record in records if record.role == "allocator"]
        control_members = [record.snapshot for record in control_records]
        # Physical host lifecycle belongs to the control record. A model child entering DRAINING
        # must remove only that replica from routing, not mark every sibling on the host draining.
        restrictive = max(
            control_members or members,
            key=lambda item: state_rank[item.state],
        )
        host_admits_ready = (
            not malformed
            and (not control_members or restrictive.state in _ROUTABLE_NODE_STATES)
            and all(record.safety_lease for record in control_records)
        )
        residency_candidates: dict[
            str,
            list[tuple[_AllocatorSnapshotMember, ModelResidency]],
        ] = {}
        for record in records:
            for residency in record.snapshot.residencies:
                residency_candidates.setdefault(residency.model_id, []).append(
                    (record, residency)
                )
        residencies: dict[str, ModelResidency] = {}
        for model_id, candidates in residency_candidates.items():
            engine_candidates = [
                candidate
                for candidate in candidates
                if candidate[0].role in ("engine", "both")
            ]
            authoritative = engine_candidates or candidates
            _, selected = max(
                authoritative,
                key=lambda candidate: (
                    candidate[0].snapshot.last_heartbeat,
                    residency_rank[candidate[1].state],
                ),
            )
            if selected.state == ResidencyState.READY:
                route_proven = host_admits_ready and any(
                    model_id in record.admitted_models
                    and candidate.state == ResidencyState.READY
                    and (not selected.managed or candidate.managed)
                    for record, candidate in candidates
                )
                corroborated_live_child = any(
                    record.role in ("engine", "both")
                    and record.safety_lease
                    and candidate.state == ResidencyState.READY
                    for record, candidate in candidates
                )
                intentionally_host_fenced = (
                    corroborated_live_child
                    and bool(control_members)
                    and restrictive.state not in _ROUTABLE_NODE_STATES
                )
                if not route_proven and not intentionally_host_fenced:
                    # The control runtime can truthfully own a live READY process before its child
                    # route is registered. Preserve its resident memory/slot and suppress a
                    # duplicate WARM, but never use that control-only claim as replacement proof.
                    selected = replace(selected, state=ResidencyState.WARMING)
            residencies[model_id] = replace(
                selected,
                memory_mb=max(item.memory_mb for _, item in candidates),
                pinned=any(item.pinned for _, item in candidates),
                managed=all(item.managed for _, item in candidates),
                active_requests=max(item.active_requests for _, item in candidates),
            )
        allowed_sets = [set(member.allowed_data_tiers) for member in members]
        allowed_tiers = set.intersection(*allowed_sets) if allowed_sets else set()
        performance_by_model: dict[str, list[ModelPerformance]] = {}
        for member in members:
            for performance in member.model_performance:
                if performance.model_id in residencies:
                    performance_by_model.setdefault(
                        performance.model_id,
                        [],
                    ).append(performance)
        merged.append(
            NodeSnapshot(
                node_id=host_id,
                capacity_mb=(
                    capacity_mb := max(member.capacity_mb for member in members)
                ),
                reserved_mb=min(
                    capacity_mb,
                    max(member.reserved_mb for member in members),
                ),
                backends=_union(member.backends for member in members),
                runtimes=_union(member.runtimes for member in members),
                state=(NodeState.UNHEALTHY if malformed else restrictive.state),
                failure_domain=next(
                    (item.failure_domain for item in members if item.failure_domain), ""
                ),
                allowed_data_tiers=tuple(sorted(allowed_tiers)),
                allowed_models=_intersect_allowlists(
                    member.allowed_models for member in members
                ),
                denied_models=_union(member.denied_models for member in members),
                tags=_union(member.tags for member in members),
                max_models=min(
                    (
                        member.max_models
                        for member in members
                        if member.max_models is not None
                    ),
                    default=None,
                ),
                residencies=tuple(
                    sorted(residencies.values(), key=lambda item: item.model_id)
                ),
                cached_models=_union(member.cached_models for member in members),
                active_requests=min(
                    MAX_COUNTER,
                    sum(member.active_requests for member in members),
                ),
                max_concurrency=min(
                    MAX_COUNTER,
                    sum(member.max_concurrency for member in members),
                ),
                queue_depth=min(
                    MAX_COUNTER,
                    sum(member.queue_depth for member in members),
                ),
                tokens_per_second=min(
                    1_000_000_000_000.0,
                    sum(member.tokens_per_second for member in members),
                ),
                latency_ms=max(member.latency_ms for member in members),
                model_performance=tuple(
                    ModelPerformance(
                        model_id=model_id,
                        tokens_per_second=min(
                            1_000_000_000_000.0,
                            sum(item.tokens_per_second for item in samples),
                        ),
                        latency_ms=max(item.latency_ms for item in samples),
                        sample_count=min(
                            MAX_COUNTER,
                            sum(item.sample_count for item in samples),
                        ),
                        # The merged value sums every child. It remains attributable only while
                        # every contributing measurement is fresh, so retain the oldest timestamp.
                        updated_at=min(item.updated_at for item in samples),
                    )
                    for model_id, samples in sorted(performance_by_model.items())
                ),
                # Capacity and child records repeat physical-host capability; never add them.
                memory_bandwidth_gbps=max(
                    member.memory_bandwidth_gbps for member in members
                ),
                compute_gflops=max(member.compute_gflops for member in members),
                gpu_count=max(member.gpu_count for member in members),
                gpu_memory_mb=max(
                    (member.gpu_memory_mb for member in members),
                    key=lambda values: (len(values), sum(values), values),
                ),
                # Capacity-node and child-engine records describe one physical host, so host cost
                # is metadata rather than an additive per-record charge.
                cost_per_hour=max(member.cost_per_hour for member in members),
                host_priority=max(member.host_priority for member in members),
                last_heartbeat=max(member.last_heartbeat for member in members),
                mutation_cooldown_until=max(
                    member.mutation_cooldown_until for member in members
                ),
                actuator_capabilities=(
                    ()
                    if malformed
                    else _union(member.actuator_capabilities for member in members)
                ),
                manually_managed=(
                    malformed or all(member.manually_managed for member in members)
                ),
            )
        )
    return tuple(merged)


def _allocator_action_dict(action: MutationAction) -> dict[str, Any]:
    return action.to_dict()


def _reconcile_summary(result) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "generation": result.plan_generation,
        "mode": result.mode.value,
        "actions": [_allocator_action_dict(item) for item in result.actions],
        "deferred": [
            {**asdict(item), "kind": item.kind.value} for item in result.deferred
        ],
    }


def _observe_allocator_request(
    app: FastAPI,
    model: str,
    started_at: float,
    *,
    error: bool,
    queue_depth: int = 0,
) -> None:
    elapsed = max(0.0, time.monotonic() - started_at)
    try:
        recorded = _allocator(app).observe(
            model,
            service_seconds=elapsed,
            latency_ms=elapsed * 1_000.0,
            queue_depth=queue_depth,
            error=error,
        )
        if recorded:
            _mark_allocator_dirty(app)
    except (TypeError, ValueError):
        return


def _allocator_capacity_error(status_code: int) -> bool:
    """Only retryable saturation/server failures should increase replica pressure."""

    return status_code == 429 or status_code >= 500


_ENGINE_PERFORMANCE_EWMA_ALPHA = 0.20
_MAX_PERFORMANCE_RESPONSE_BYTES = 1_000_000


def _record_engine_performance(
    engine: Node,
    started_at: float,
    *,
    model: str,
    status_code: int,
    response: httpx.Response | None = None,
) -> None:
    """Attribute successful proxy work to one engine without retaining request content."""

    if status_code < 200 or status_code >= 300:
        return
    elapsed = max(0.0, time.monotonic() - started_at)
    latency_ms = min(1_000_000_000_000.0, elapsed * 1_000.0)
    first = engine.proxy_performance_samples == 0
    alpha = 1.0 if first else _ENGINE_PERFORMANCE_EWMA_ALPHA
    engine.proxy_latency_ms = (
        latency_ms
        if first
        else alpha * latency_ms + (1.0 - alpha) * engine.proxy_latency_ms
    )
    engine.proxy_performance_samples = min(
        MAX_COUNTER,
        engine.proxy_performance_samples + 1,
    )
    model_performance = engine.proxy_model_performance.setdefault(
        model,
        _ProxyModelPerformance(),
    )
    model_first = model_performance.samples == 0
    model_alpha = 1.0 if model_first else _ENGINE_PERFORMANCE_EWMA_ALPHA
    model_performance.latency_ms = (
        latency_ms
        if model_first
        else model_alpha * latency_ms
        + (1.0 - model_alpha) * model_performance.latency_ms
    )
    model_performance.samples = min(MAX_COUNTER, model_performance.samples + 1)
    model_performance.updated_at = max(0.0, time.time())

    if (
        response is None
        or elapsed <= 0
        or len(response.content) > _MAX_PERFORMANCE_RESPONSE_BYTES
    ):
        return
    try:
        payload = response.json()
        usage = payload.get("usage") if isinstance(payload, dict) else None
        completion_tokens = (
            usage.get("completion_tokens") if isinstance(usage, dict) else None
        )
        if (
            isinstance(completion_tokens, bool)
            or not isinstance(completion_tokens, (int, float))
            or completion_tokens <= 0
        ):
            return
        measured = min(1_000_000_000_000.0, float(completion_tokens) / elapsed)
    except (TypeError, ValueError):
        return
    engine.proxy_tokens_per_second = (
        measured
        if engine.proxy_tokens_per_second == 0
        else alpha * measured + (1.0 - alpha) * engine.proxy_tokens_per_second
    )
    model_performance.tokens_per_second = (
        measured
        if model_performance.tokens_per_second == 0
        else model_alpha * measured
        + (1.0 - model_alpha) * model_performance.tokens_per_second
    )


def _change_active_tasks(node: Node, delta: int) -> None:
    node.proxy_active_tasks = max(0, node.proxy_active_tasks + delta)
    _sync_active_tasks(node, managed=_managed_node(node))


def _set_reported_load(node: Node, load: Mapping[str, Any], *, managed: bool) -> None:
    node.load = dict(load)
    node.reported_active_tasks = _strict_active_tasks(node.load.get("active_tasks"))
    _sync_active_tasks(node, managed=managed)


def _sync_active_tasks(node: Node, *, managed: bool) -> None:
    if managed:
        # llama.cpp's slot sample is process-wide and therefore already contains proxied work.
        active = max(node.reported_active_tasks, node.proxy_active_tasks)
    else:
        # Legacy engines may report work from a separate service while Grid owns additional proxy
        # requests, matching the historic additive behavior for public registrations.
        active = min(MAX_COUNTER, node.reported_active_tasks + node.proxy_active_tasks)
    node.load["active_tasks"] = active


def _strict_active_tasks(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    try:
        count = int(value)
    except (OverflowError, ValueError):
        return 0
    if count != value or count < 0:
        return 0
    return min(MAX_COUNTER, count)


def _mark_engine_used(node: Node, model_id: str) -> None:
    node.model_last_used_at[model_id] = max(
        node.model_last_used_at.get(model_id, 0.0),
        time.time(),
    )


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value else ()
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    return tuple(dict.fromkeys(str(item) for item in value if str(item)))


def _first_nonnegative_int(*values: Any) -> int:
    for value in values:
        try:
            number = int(float(value))
        except (TypeError, ValueError, OverflowError):
            continue
        if 0 <= number <= MAX_COUNTER:
            return number
    return 0


def _positive_int(*values: Any) -> int:
    return max(1, _first_nonnegative_int(*values))


def _gpu_memory_tuple(resources: Mapping[str, Any]) -> tuple[int, ...]:
    values = resources.get("gpu_memory_mb")
    if not isinstance(values, (list, tuple)):
        gpus = resources.get("gpus")
        values = (
            [item.get("memory_total_mb") for item in gpus if isinstance(item, Mapping)]
            if isinstance(gpus, list)
            else []
        )
    parsed = []
    for value in values:
        memory_mb = _first_nonnegative_int(value)
        if 0 < memory_mb <= MAX_MEMORY_MB:
            parsed.append(memory_mb)
    return tuple(sorted(parsed, reverse=True))


def _optional_nonnegative_int(value: Any) -> int | None:
    if value is None:
        return None
    return _first_nonnegative_int(value)


def _nonnegative_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return (
        min(number, 1_000_000_000_000.0)
        if math.isfinite(number) and number >= 0
        else 0.0
    )


def _union(groups) -> tuple[str, ...]:
    return tuple(sorted({item for group in groups for item in group}))


def _intersect_allowlists(groups) -> tuple[str, ...]:
    lists = [set(group) for group in groups if group]
    return tuple(sorted(set.intersection(*lists))) if lists else ()


def _openai_error(status_code: int, message: str, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": "invalid_request_error"
                if status_code < 500
                else "server_error",
                "param": None,
                "code": code,
            }
        },
    )


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()
