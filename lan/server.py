from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from lan.runtime import GRID_TYPE


NODE_TTL_SECONDS = 60
ENGINE_TIMEOUT_SECONDS = 600


class NodeCreateRequest(BaseModel):
    role: Literal["engine", "app", "both"] = "app"
    name: str | None = None


class NodeUpdateRequest(BaseModel):
    role: Literal["engine", "app", "both"] = "engine"
    models: list[str] = Field(default_factory=list)
    endpoint_url: str | None = None
    media_url: str | None = None
    pricing: dict[str, Any] = Field(default_factory=dict)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    load: dict[str, Any] = Field(default_factory=dict)
    name: str | None = None


class HeartbeatRequest(BaseModel):
    node_id: str
    load: dict[str, Any] = Field(default_factory=dict)


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
    first_seen_at: str = field(default_factory=lambda: _utc_now_iso())
    last_heartbeat: float = field(default_factory=time.time)

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["last_heartbeat_at"] = datetime.fromtimestamp(
            self.last_heartbeat,
            timezone.utc,
        ).isoformat()
        data["ttl_seconds"] = NODE_TTL_SECONDS
        return data


def create_app(*, grid_id: str, grid_name: str) -> FastAPI:
    app = FastAPI(
        title="Grid LAN Signaling Server",
        description="Unauthenticated LAN-only engine discovery and OpenAI-compatible request proxy.",
        version="0.1.0",
    )
    app.state.nodes = {}
    app.state.grid_id = grid_id
    app.state.grid_name = grid_name

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
        node_id = str(uuid.uuid4())
        node = Node(node_id=node_id, role=req.role, name=req.name)
        _nodes(app)[node_id] = node
        return {"node_id": node_id, "role": req.role}

    @app.put("/nodes/{node_id}")
    async def update_node(node_id: str, req: NodeUpdateRequest):
        if req.role in ("engine", "both") and not req.models:
            raise HTTPException(status_code=400, detail="at least one model is required for engines")
        if req.role in ("engine", "both"):
            text_models = [model for model in req.models if not model.startswith("comfyui:")]
            media_models = [model for model in req.models if model.startswith("comfyui:")]
            if text_models and not req.endpoint_url:
                raise HTTPException(status_code=400, detail="endpoint_url is required for text engines")
            if media_models and not req.media_url:
                raise HTTPException(status_code=400, detail="media_url is required for media engines")
        existing = _nodes(app).get(node_id)
        node = existing or Node(node_id=node_id, role=req.role)
        node.role = req.role
        node.models = list(dict.fromkeys(req.models))
        node.endpoint_url = req.endpoint_url.rstrip("/") if req.endpoint_url else None
        node.media_url = req.media_url.rstrip("/") if req.media_url else None
        node.pricing = dict(req.pricing)
        node.capabilities = dict(req.capabilities)
        node.load = dict(req.load)
        node.name = req.name
        node.last_heartbeat = time.time()
        _nodes(app)[node_id] = node
        return {"status": "updated", "node": node.public_dict()}

    @app.post("/nodes/heartbeat")
    async def heartbeat(req: HeartbeatRequest):
        node = _nodes(app).get(req.node_id)
        if not node:
            raise HTTPException(status_code=404, detail="node not found")
        node.load = dict(req.load)
        node.last_heartbeat = time.time()
        return {"ttl_seconds": NODE_TTL_SECONDS}

    @app.delete("/nodes/{node_id}")
    async def unregister(node_id: str):
        removed = _nodes(app).pop(node_id, None)
        if not removed:
            raise HTTPException(status_code=404, detail="node not found")
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
                data.append({"id": model, "object": "model", "created": created, "owned_by": "lan"})
        return {"object": "list", "data": data}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        return await _proxy_openai(app, "chat/completions", request)

    @app.post("/v1/completions")
    async def completions(request: Request):
        return await _proxy_openai(app, "completions", request)

    @app.post("/v1/media/image/generate")
    async def media_image_generate(request: Request):
        return await _proxy_media(app, "media/image/generate", "comfyui:image_generation", request)

    @app.post("/v1/media/image/edit")
    async def media_image_edit(request: Request):
        return await _proxy_media(app, "media/image/edit", "comfyui:image_editing", request)

    @app.post("/v1/media/video/i2v")
    async def media_i2v(request: Request):
        return await _proxy_media(app, "media/video/i2v", "comfyui:i2v", request)

    return app


async def _proxy_openai(app: FastAPI, endpoint_path: str, request: Request) -> Response:
    raw_body = await request.body()
    try:
        body = json.loads(raw_body or b"{}")
    except json.JSONDecodeError:
        return _openai_error(400, "Request body is not valid JSON", "invalid_json")
    if not isinstance(body, dict):
        return _openai_error(400, "Request body must be a JSON object", "invalid_request")
    model = body.get("model")
    if not isinstance(model, str) or not model:
        return _openai_error(400, "model is required", "invalid_request")

    engine = _choose_engine(app, model)
    if not engine:
        return _openai_error(503, f"No active LAN engine for model {model!r}", "engine_unavailable")

    url = f"{engine.endpoint_url.rstrip('/')}/{endpoint_path}"
    headers = {"content-type": request.headers.get("content-type", "application/json")}
    timeout = httpx.Timeout(ENGINE_TIMEOUT_SECONDS, read=None if body.get("stream") else ENGINE_TIMEOUT_SECONDS)

    if body.get("stream"):
        client = httpx.AsyncClient(timeout=timeout)
        engine_request = client.build_request("POST", url, content=raw_body, headers=headers)
        try:
            engine_response = await client.send(engine_request, stream=True)
        except httpx.RequestError as exc:
            await client.aclose()
            return _openai_error(502, f"Engine request failed: {exc}", "engine_error")

        async def stream_response():
            try:
                async for chunk in engine_response.aiter_raw():
                    yield chunk
            finally:
                await engine_response.aclose()
                await client.aclose()

        return StreamingResponse(
            stream_response(),
            status_code=engine_response.status_code,
            media_type=engine_response.headers.get("content-type", "text/event-stream"),
        )

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            engine_response = await client.post(url, content=raw_body, headers=headers)
    except httpx.RequestError as exc:
        return _openai_error(502, f"Engine request failed: {exc}", "engine_error")

    return Response(
        content=engine_response.content,
        status_code=engine_response.status_code,
        media_type=engine_response.headers.get("content-type", "application/json"),
    )


async def _proxy_media(
    app: FastAPI,
    endpoint_path: str,
    model: str,
    request: Request,
) -> Response:
    raw_body = await request.body()
    engine = _choose_engine(app, model)
    if not engine:
        return _openai_error(503, f"No active LAN media engine for {model!r}", "engine_unavailable")
    if not engine.media_url:
        return _openai_error(503, f"Engine {engine.node_id} did not advertise a media URL", "engine_unavailable")

    client = httpx.AsyncClient(timeout=httpx.Timeout(ENGINE_TIMEOUT_SECONDS, read=None))
    media_request = client.build_request(
        "POST",
        f"{engine.media_url.rstrip('/')}/{endpoint_path}",
        content=raw_body,
        headers={"content-type": request.headers.get("content-type", "application/json")},
    )
    try:
        engine_response = await client.send(media_request, stream=True)
    except httpx.RequestError as exc:
        await client.aclose()
        return _openai_error(502, f"Engine media request failed: {exc}", "engine_error")

    async def stream_response():
        try:
            async for chunk in engine_response.aiter_raw():
                yield chunk
        finally:
            await engine_response.aclose()
            await client.aclose()

    return StreamingResponse(
        stream_response(),
        status_code=engine_response.status_code,
        media_type=engine_response.headers.get("content-type", "text/event-stream"),
    )


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


def _active_engines(app: FastAPI, model: str | None = None) -> list[Node]:
    now = time.time()
    engines: list[Node] = []
    stale_ids: list[str] = []
    for node_id, node in _nodes(app).items():
        if now - node.last_heartbeat > NODE_TTL_SECONDS:
            stale_ids.append(node_id)
            continue
        if node.role not in ("engine", "both"):
            continue
        if model and model not in node.models:
            continue
        engines.append(node)
    for node_id in stale_ids:
        _nodes(app).pop(node_id, None)
    engines.sort(key=lambda item: (_load_score(item.load), item.last_heartbeat))
    return engines


def _choose_engine(app: FastAPI, model: str) -> Node | None:
    engines = _active_engines(app, model)
    return engines[0] if engines else None


def _engine_dict(node: Node) -> dict[str, Any]:
    data = node.public_dict()
    data.pop("last_heartbeat", None)
    return data


def _load_score(load: dict[str, Any]) -> float:
    active = load.get("active_tasks")
    if isinstance(active, (int, float)) and active >= 0:
        return float(active)
    return 0.0


def _openai_error(status_code: int, message: str, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": "invalid_request_error" if status_code < 500 else "server_error",
                "param": None,
                "code": code,
            }
        },
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
