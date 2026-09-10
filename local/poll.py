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


def _worker_id(request: Request, host_id: str, node_id: str, models: tuple[str, ...]) -> str:
    """Who this caller may claim work as, or 401.

    Two kinds of worker reach this router and they prove themselves differently.

    An allocator-managed node proves a host_id with its control token, exactly as it does to
    change its own registration -- unchanged.

    A plain engine (`grid join`) has no token to prove anything with, because registering one
    never required a token in the first place. Its credential is that registration: it may claim
    work only for models it currently advertises. That is precisely the exposure the push path
    already granted it -- the grid dialled whatever endpoint such a node advertised -- so this
    adds no reach, it only reverses the direction of the connection.
    """

    from .server import _allocator_node_control_valid, _nodes

    if host_id:
        if not _allocator_node_control_valid(request.app, request, host_id):
            raise HTTPException(
                status_code=401,
                detail="A valid host-scoped allocator node token is required",
            )
        return host_id

    node = _nodes(request.app).get(node_id) if node_id else None
    if node is None or node.role != "engine":
        raise HTTPException(status_code=401, detail="A registered engine node is required")
    if node.host_id:
        # An allocator-managed node must not be able to sidestep its own token by asking under
        # its node_id instead.
        raise HTTPException(
            status_code=401,
            detail="A valid host-scoped allocator node token is required",
        )
    advertised = set(node.models or ())
    if models and not set(models).issubset(advertised):
        raise HTTPException(
            status_code=403, detail="This engine does not advertise that model"
        )
    return node_id


def _require_node(request: Request, host_id: str) -> None:
    from .server import _allocator_node_control_valid

    if not host_id or not _allocator_node_control_valid(request.app, request, host_id):
        raise HTTPException(
            status_code=401,
            detail="A valid host-scoped allocator node token is required",
        )


@poll_router.get("/grid/v1/poll")
async def poll(
    request: Request, host_id: str = "", node_id: str = "", models: str = ""
) -> Response:
    wanted = tuple(m for m in models.split(",") if m)
    worker = _worker_id(request, host_id, node_id, wanted)
    table = request.app.state.inflight

    txn = table.claim(node_id=worker, models=wanted)
    if txn is None:
        await table.wait_for_work(POLL_WINDOW_SECONDS)
        txn = table.claim(node_id=worker, models=wanted)

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
    worker = _worker_id(
        request,
        request.headers.get("x-grid-host-id", ""),
        request.headers.get("x-grid-node-id", ""),
        (),
    )

    table = request.app.state.inflight
    txn = table.get(txn_id)
    if txn is not None and txn.node_id != worker:
        # Only the worker that claimed this may settle it. Otherwise any registered engine could
        # answer for work it never received -- and under pull it can now reach these routes.
        raise HTTPException(status_code=403, detail="This transaction belongs to another worker")
    if txn is None or not txn.is_stream:
        return {"cancelled": not table.finish(txn_id, await request.body())}

    # The worker sends the engine's SSE as this request's body while the engine is still
    # writing it, so read it as it arrives. Buffering with `await request.body()` would hold
    # every byte until the engine stopped -- the one thing a streamed answer exists to avoid.
    accepted = True
    async for chunk in request.stream():
        if chunk:
            accepted = table.publish(txn_id, chunk)
            if not accepted:
                break
    # The body ending IS the end of the answer, so end the consumer's stream here rather than
    # waiting for a separate /done the worker would have to remember to send.
    if accepted:
        accepted = table.finish(txn_id, None)
    return {"cancelled": not accepted}


@poll_router.post("/grid/v1/result/{txn_id}/done")
async def done(request: Request, txn_id: str) -> dict:
    worker = _worker_id(
        request,
        request.headers.get("x-grid-host-id", ""),
        request.headers.get("x-grid-node-id", ""),
        (),
    )

    table = request.app.state.inflight
    txn = table.get(txn_id)
    if txn is not None and txn.node_id != worker:
        raise HTTPException(status_code=403, detail="This transaction belongs to another worker")
    accepted = table.finish(txn_id, None)
    return {"cancelled": not accepted}


@poll_router.post("/grid/v1/error/{txn_id}")
async def error(request: Request, txn_id: str) -> dict:
    worker = _worker_id(
        request,
        request.headers.get("x-grid-host-id", ""),
        request.headers.get("x-grid-node-id", ""),
        (),
    )

    payload = await request.body()
    message = payload.decode("utf-8", errors="replace")[:500]
    table = request.app.state.inflight
    txn = table.get(txn_id)
    if txn is not None and txn.node_id != worker:
        raise HTTPException(status_code=403, detail="This transaction belongs to another worker")
    table.cancel(txn_id, message or "worker reported a failure")

    return {"cancelled": True}
