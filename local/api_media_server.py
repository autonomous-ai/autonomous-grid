"""The engine-side media API for an **API media engine** (a vendor gateway, e.g. doggi).

The local grid proxy forwards media jobs to an engine's ``media_url`` and expects the grid media
protocol there: ``POST /media/{image/generate,image/edit,video/i2v}`` answered with an SSE stream of
``progress`` / ``result`` / ``[DONE]`` events. A vendor gateway speaks its own dialect instead
(doggi: ``POST /media/generations`` + poll + download), so something has to translate.

That translation already exists — ``shared/handlers`` — and is what the remote serve loop uses. This
module is the *local* mounting of it: the same handler behind the same routes ``local/media_server.py``
serves for ComfyUI, so the grid proxy cannot tell the two apart and needs no vendor knowledge.

    grid proxy ──/media/image/generate──▶ this server ──handler──▶ vendor gateway

Binds loopback by default (see ``cli/provider``): the vendor credential lives in this process, and
the LAN-facing surface should stay the grid proxy, not the credential holder.
"""
from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from shared.handlers import HANDLERS


def create_app(*, api_kind: str, base_url: str, api_key: str) -> FastAPI:
    handler_cls = HANDLERS.get(api_kind)
    if handler_cls is None:
        supported = ", ".join(sorted(HANDLERS)) or "none"
        raise SystemExit(f"No media handler for API kind {api_kind!r}. Supported: {supported}.")

    app = FastAPI(
        title="Grid API Media Server",
        description=f"Engine-local media API backed by the {api_kind} gateway.",
        version="0.1.0",
    )
    app.state.handler = handler_cls(base_url=base_url, api_key=api_key)
    app.state.api_kind = api_kind

    @app.get("/health")
    async def health():
        return {"ok": True, "api_kind": api_kind}

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
        body = None

    def lines():
        if not isinstance(body, dict):
            yield 'data: {"error": "Request body is not valid JSON"}\n\n'
            return
        # A vendor failure (bad key, quota, a task that finished with no files) must reach the
        # consumer as an `error` event, not a torn stream or a traceback in the log: by the time
        # this generator runs the response has already begun, so there is no status code left to
        # set. Mirrors how the remote serve loop reports the same failures.
        try:
            for line in app.state.handler.forward(body, endpoint_path):
                yield _sse_line(line)
        except Exception as exc:  # noqa: BLE001 - any vendor error becomes one SSE error event
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"

    return StreamingResponse(lines(), media_type="text/event-stream")


def _sse_line(line: str) -> str:
    """Normalise a handler line to a complete SSE frame (handlers already emit `data: …\\n\\n`)."""
    if line.endswith("\n\n"):
        return line
    if line.endswith("\n"):
        return line + "\n"
    if line.startswith("data:") or line.startswith(":"):
        return f"{line}\n\n"
    return f"data: {line}\n\n"
