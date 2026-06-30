from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from shared.media.media_handler import MediaHandler


def create_app(*, comfyui_url: str) -> FastAPI:
    app = FastAPI(
        title="Grid Provider Media Server",
        description="Provider-local media workflow API backed by ComfyUI.",
        version="0.1.0",
    )
    app.state.handler = MediaHandler(comfyui_url=comfyui_url.rstrip("/"))

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.post("/media/image/generate")
    async def image_generate(request: Request):
        return await _handle_media(app, "media/image/generate", request)

    @app.post("/media/image/edit")
    async def image_edit(request: Request):
        return await _handle_media(app, "media/image/edit", request)

    @app.post("/media/video/i2v")
    async def i2v(request: Request):
        return await _handle_media(app, "media/video/i2v", request)

    return app


async def _handle_media(app: FastAPI, endpoint_path: str, request: Request) -> StreamingResponse:
    raw = await request.body()
    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        body = {"_invalid_json": True}

    def lines():
        if body.get("_invalid_json"):
            yield 'data: {"error": "Request body is not valid JSON"}\n\n'
            return
        for line in app.state.handler.handle_request(endpoint_path, body):
            yield _sse_line(line)

    return StreamingResponse(lines(), media_type="text/event-stream")


def _sse_line(line: str) -> str:
    if line.endswith("\n"):
        return line
    if line.startswith("data:") or line.startswith(":"):
        return f"{line}\n\n"
    return f"data: {line}\n\n"
