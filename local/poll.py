"""Worker-facing routes: poll for work, report a result, report an error.

This module is the only thing a worker on another machine ever talks to.
Nothing here dials out to a worker.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request, Response

# Must stay shorter than the worker's own poll timeout (35.0s, node_poll.py), or
# every idle poll cycle reads to the worker as a transport error, not "no work yet".
POLL_WINDOW_SECONDS = 30.0

poll_router = APIRouter()


def _require_node(request: Request, host_id: str) -> None:
    from .server import _allocator_node_control_valid

    if not host_id or not _allocator_node_control_valid(request.app, request, host_id):
        raise HTTPException(
            status_code=401,
            detail="A valid host-scoped allocator node token is required",
        )


@poll_router.get("/grid/v1/poll")
async def poll(request: Request, host_id: str = "", models: str = "") -> Response:
    _require_node(request, host_id)
    wanted = tuple(m for m in models.split(",") if m)
    table = request.app.state.inflight

    txn = table.claim(node_id=host_id, models=wanted)
    if txn is None:
        await table.wait_for_work(POLL_WINDOW_SECONDS)
        txn = table.claim(node_id=host_id, models=wanted)

    if txn is None:
        return Response(status_code=204)

    return Response(
        content=json.dumps(
            {
                "transaction_id": txn.id,
                "model": txn.model,
                "stream": txn.is_stream,
                "body": json.loads(txn.body),
            }
        ),
        media_type="application/json",
    )


@poll_router.post("/grid/v1/result/{txn_id}")
async def result(request: Request, txn_id: str) -> dict:
    host_id = request.headers.get("x-grid-host-id", "")
    _require_node(request, host_id)

    payload = await request.body()
    table = request.app.state.inflight
    txn = table.get(txn_id)
    if txn is not None and txn.is_stream:
        accepted = table.publish(txn_id, payload)
    else:
        accepted = table.finish(txn_id, payload)

    return {"cancelled": not accepted}


@poll_router.post("/grid/v1/result/{txn_id}/done")
async def done(request: Request, txn_id: str) -> dict:
    host_id = request.headers.get("x-grid-host-id", "")
    _require_node(request, host_id)

    table = request.app.state.inflight
    accepted = table.finish(txn_id, None)
    return {"cancelled": not accepted}


@poll_router.post("/grid/v1/error/{txn_id}")
async def error(request: Request, txn_id: str) -> dict:
    host_id = request.headers.get("x-grid-host-id", "")
    _require_node(request, host_id)

    payload = await request.body()
    message = payload.decode("utf-8", errors="replace")[:500]
    table = request.app.state.inflight
    table.cancel(txn_id, message or "worker reported a failure")

    return {"cancelled": True}
