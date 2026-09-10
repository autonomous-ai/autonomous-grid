import asyncio
import time

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from local.inflight import InflightTable
from local.poll import poll_router
from shared.allocator.auth import mint_node_token


def _app() -> tuple[FastAPI, TestClient, str]:
    app = FastAPI()
    app.state.allocator_control_token = "control-secret"
    app.state.inflight = InflightTable()
    app.include_router(poll_router)
    token = mint_node_token("control-secret", "host-1", ttl_seconds=3600)
    return app, TestClient(app), token


def test_poll_without_a_node_token_is_refused():
    _app_obj, client, _token = _app()
    response = client.get("/grid/v1/poll", params={"host_id": "host-1", "models": "m1"})
    assert response.status_code == 401


def test_poll_returns_a_transaction_the_node_can_serve():
    app, client, token = _app()
    txn = app.state.inflight.create(model="m1", body=b'{"model":"m1"}', is_stream=False)
    response = client.get(
        "/grid/v1/poll",
        params={"host_id": "host-1", "models": "m1"},
        headers={"x-grid-allocator-node-token": token},
    )
    assert response.status_code == 200
    assert response.json()["transaction_id"] == txn.id


def test_done_terminates_a_streaming_transaction():
    app, client, token = _app()
    txn = app.state.inflight.create(model="m1", body=b"{}", is_stream=True)
    app.state.inflight.claim(node_id="host-1", models=("m1",))
    app.state.inflight.publish(txn.id, b"chunk-1")

    response = client.post(
        f"/grid/v1/result/{txn.id}/done",
        headers={"x-grid-allocator-node-token": token, "x-grid-host-id": "host-1"},
    )
    assert response.status_code == 200
    assert response.json()["cancelled"] is False

    stored = app.state.inflight.get(txn.id)
    assert stored.state.value == "done"
    assert stored.result is None


def test_result_for_a_cancelled_transaction_tells_the_worker_to_stop():
    app, client, token = _app()
    txn = app.state.inflight.create(model="m1", body=b"{}", is_stream=True)
    app.state.inflight.claim(node_id="host-1", models=("m1",))
    app.state.inflight.cancel(txn.id, "consumer went away")
    response = client.post(
        f"/grid/v1/result/{txn.id}",
        content=b"chunk",
        headers={"x-grid-allocator-node-token": token, "x-grid-host-id": "host-1"},
    )
    assert response.status_code == 200
    assert response.json()["cancelled"] is True


def test_the_real_app_mounts_the_poll_routes():
    from local.server import create_app

    app = create_app(grid_id="g1", grid_name="grid-one")
    # app.routes wraps included routers lazily on this FastAPI version, so ask the
    # OpenAPI schema (which fully resolves them) for the flat path list instead.
    paths = set(app.openapi()["paths"])
    assert "/grid/v1/poll" in paths
    assert "/grid/v1/result/{txn_id}" in paths
    assert isinstance(app.state.inflight, InflightTable)


def test_choose_node_prefers_the_least_loaded_node_serving_the_model():
    from local.server import Node, _choose_node, create_app

    app = create_app(grid_id="g1", grid_name="grid-one")
    now = time.time()
    app.state.nodes = {
        "busy": Node(node_id="busy", role="engine", models=["m1"], host_id="h-busy",
                     load={"active_tasks": 5}, last_heartbeat=now),
        "idle": Node(node_id="idle", role="engine", models=["m1"], host_id="h-idle",
                     load={"active_tasks": 0}, last_heartbeat=now),
        "other": Node(node_id="other", role="engine", models=["m2"], host_id="h-other",
                      load={"active_tasks": 0}, last_heartbeat=now),
    }
    assert _choose_node(app, "m1") == "h-idle"
    assert _choose_node(app, "absent") is None


def test_pull_path_answers_503_when_no_node_advertises_the_model(monkeypatch):
    from local.server import create_app

    monkeypatch.setenv("GRID_LOCAL_PULL", "1")
    app = create_app(grid_id="g1", grid_name="grid-one")
    client = TestClient(app)
    response = client.post("/v1/chat/completions", json={"model": "absent"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "engine_unavailable"


@pytest.mark.asyncio
async def test_pull_path_registers_a_transaction_instead_of_dialling_an_engine(monkeypatch):
    from local.server import Node, _proxy_openai, create_app

    monkeypatch.setenv("GRID_LOCAL_PULL", "1")
    app = create_app(grid_id="g1", grid_name="grid-one")
    app.state.nodes = {
        "n1": Node(node_id="n1", role="engine", models=["m1"], host_id="h1",
                   load={"active_tasks": 0}, last_heartbeat=time.time()),
    }
    scope = {"type": "http", "method": "POST", "headers": []}
    request = Request(scope)
    request._body = b'{"model": "m1"}'

    created = []
    real_create = app.state.inflight.create

    def spying_create(**kwargs):
        txn = real_create(**kwargs)
        created.append(txn)
        return txn

    monkeypatch.setattr(app.state.inflight, "create", spying_create)

    # No worker will ever claim this, so the handler blocks awaiting the transaction.
    # A push-path dial would instead fail fast (no such engine listening) rather than hang --
    # the hang itself is the proof the pull branch, not _choose_engine, was taken.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            _proxy_openai(app, "chat/completions", request), timeout=0.3
        )

    assert [txn.model for txn in created] == ["m1"]


async def test_a_streamed_submit_reaches_the_consumer_before_the_worker_finishes():
    """The worker sends ONE request whose body is the engine's SSE as it arrives.

    Buffering it with `await request.body()` would hold every byte until the engine stopped
    writing -- which is exactly what a streamed request exists not to do, and would turn a
    minute-long answer into a minute of nothing followed by all of it at once. Read the body as
    it arrives and publish each piece, then end the consumer's stream when the body ends, so no
    separate /done is needed on this path.
    """

    import asyncio

    app, client, token = _app()
    txn = app.state.inflight.create(model="m1", body=b"{}", is_stream=True)
    app.state.inflight.claim(node_id="host-1", models=("m1",))

    received: list[bytes] = []

    async def consume() -> None:
        async for chunk in app.state.inflight.stream(txn.id):
            received.append(chunk)

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0)

    def body_pieces():
        yield b'data: {"delta":"GRID"}\n\n'
        yield b'data: {"delta":" OK"}\n\n'
        yield b"data: [DONE]\n\n"

    response = await asyncio.to_thread(
        client.post,
        f"/grid/v1/result/{txn.id}",
        content=body_pieces(),
        headers={
            "x-grid-allocator-node-token": token,
            "x-grid-host-id": "host-1",
            "content-type": "text/event-stream",
        },
    )
    assert response.status_code == 200
    assert response.json()["cancelled"] is False

    await asyncio.wait_for(consumer, timeout=2.0)
    assert b"".join(received) == (
        b'data: {"delta":"GRID"}\n\ndata: {"delta":" OK"}\n\ndata: [DONE]\n\n'
    )
    # NOT asserted here: that `received` arrived in pieces. Starlette's TestClient collapses a
    # generator body into one ASGI message before the route ever sees it, so through this client
    # a correct implementation and a buffering one are indistinguishable. The piece-by-piece
    # half is pinned by test_the_result_route_consumes_the_body_as_a_stream below, which drives
    # the route directly; what THIS test proves is the part that was actually broken end to end:
    # the consumer's stream carries the bytes and then ENDS.


async def test_the_result_route_consumes_the_body_as_a_stream():
    """`await request.body()` would hold a minute-long answer back for the whole minute."""

    from local import poll as poll_module

    app, _client, _token = _app()
    txn = app.state.inflight.create(model="m1", body=b"{}", is_stream=True)
    app.state.inflight.claim(node_id="host-1", models=("m1",))

    published: list[bytes] = []
    app.state.inflight.publish = lambda _id, chunk: (published.append(chunk), True)[1]
    app.state.inflight.finish = lambda _id, _result: True

    class _Request:
        app = None
        headers = {"x-grid-host-id": "host-1"}

        @staticmethod
        async def stream():
            for piece in (b"one", b"two", b"three"):
                yield piece

        @staticmethod
        async def body():
            raise AssertionError("the route buffered the whole body instead of streaming it")

    request = _Request()
    request.app = app
    monkey = poll_module._require_node
    poll_module._require_node = lambda *_a, **_k: None
    try:
        result = await poll_module.result(request, txn.id)
    finally:
        poll_module._require_node = monkey

    assert result == {"cancelled": False}
    assert published == [b"one", b"two", b"three"]


async def test_a_streamed_request_survives_long_enough_for_a_worker_to_claim_it(monkeypatch):
    """The response object exists long before a worker has polled for the work.

    MEASURED on the live grid: the consumer's POST answered 200 with zero bytes in the same
    millisecond the worker's poll answered 204 -- the transaction had already been cancelled by
    the time anyone asked for it. Cleanup that runs when the FUNCTION returns is right for the
    non-streamed path, which finishes its work before returning, and destroys the streamed one,
    which has not started. The streamed path's cleanup belongs to the generator.
    """

    import time as _time

    from local.server import Node, _proxy_openai, create_app

    app = create_app(grid_id="g1", grid_name="grid-one")
    app.state.nodes = {
        "n1": Node(
            node_id="n1", role="engine", models=["m1"], host_id="h1",
            load={"active_tasks": 0}, last_heartbeat=_time.time(),
        )
    }
    scope = {"type": "http", "method": "POST", "headers": []}
    request = Request(scope)
    request._body = b'{"model": "m1", "stream": true}'

    response = await _proxy_openai(app, "chat/completions", request)

    assert response.status_code == 200
    claimed = app.state.inflight.claim(node_id="h1", models=("m1",))
    assert claimed is not None, "the transaction was cancelled before any worker could claim it"
    assert claimed.is_stream is True


def _pull_app_with_node():
    import time as _time

    from local.server import Node, create_app

    app = create_app(grid_id="g1", grid_name="grid-one")
    app.state.nodes = {
        "n1": Node(
            node_id="n1", role="engine", models=["m1"], host_id="h1",
            load={"active_tasks": 0}, last_heartbeat=_time.time(),
        )
    }
    return app


async def test_the_pull_path_reports_demand_when_no_node_serves_the_model(monkeypatch):
    """The allocator allocates against demand, and demand rides the inference path.

    Every _observe_allocator_request lives in the push branch, so once pull became the default
    every text request became invisible to the allocator -- it could no longer see that anyone
    had asked for a model it was not running, which is the one signal that would make it warm
    one. A 503 is the strongest demand there is: someone wanted this model and nothing served it.
    """

    from local.server import _proxy_openai, create_app

    app = create_app(grid_id="g1", grid_name="grid-one")  # no nodes at all
    seen: list[dict] = []
    monkeypatch.setattr(
        "local.server._observe_allocator_request",
        lambda _app, model, _started, **kw: seen.append({"model": model, **kw}),
    )

    request = Request({"type": "http", "method": "POST", "headers": []})
    request._body = b'{"model": "m1"}'
    response = await _proxy_openai(app, "chat/completions", request)

    assert response.status_code == 503
    assert len(seen) == 1, "the allocator never heard that a model was asked for"
    assert seen[0]["model"] == "m1"
    assert seen[0]["error"] is True
    assert seen[0]["queue_depth"] == 1


async def test_the_pull_path_reports_a_served_request_with_its_token_count(monkeypatch):
    """Latency and output volume are how the allocator sizes what it already runs."""

    import asyncio
    import json as _json

    from local.server import _proxy_openai

    app = _pull_app_with_node()
    seen: list[dict] = []
    monkeypatch.setattr(
        "local.server._observe_allocator_request",
        lambda _app, model, _started, **kw: seen.append({"model": model, **kw}),
    )

    request = Request({"type": "http", "method": "POST", "headers": []})
    request._body = b'{"model": "m1"}'
    serving = asyncio.create_task(_proxy_openai(app, "chat/completions", request))
    await asyncio.sleep(0)

    claimed = app.state.inflight.claim(node_id="h1", models=("m1",))
    assert claimed is not None
    app.state.inflight.finish(
        claimed.id,
        _json.dumps({"choices": [], "usage": {"completion_tokens": 42}}).encode(),
    )

    response = await asyncio.wait_for(serving, timeout=2.0)
    assert response.status_code == 200
    assert len(seen) == 1
    assert seen[0]["error"] is False
    assert seen[0]["output_units"] == 42
    assert seen[0]["served_model"] == "m1"


async def test_a_repeatedly_failing_node_stops_being_picked(monkeypatch):
    """Recording faults is half of it; the picker has to act on them.

    `_choose_engine` skips a quarantined route, `_choose_node` did not -- so under pull a node
    whose engine had died kept winning every pick and failing every request, forever.
    """

    import time as _time

    from local.server import (
        Node, _choose_node, _proxy_route_is_quarantined, _record_proxy_route_outcome,
        create_app,
    )

    app = create_app(grid_id="g1", grid_name="grid-one")
    sick = Node(node_id="sick", role="engine", models=["m1"], host_id="h-sick",
                load={"active_tasks": 0}, last_heartbeat=_time.time())
    well = Node(node_id="well", role="engine", models=["m1"], host_id="h-well",
                load={"active_tasks": 5}, last_heartbeat=_time.time())
    app.state.nodes = {"sick": sick, "well": well}

    # Least-loaded wins while both are healthy.
    assert _choose_node(app, "m1") == "h-sick"

    for _ in range(10):
        _record_proxy_route_outcome(sick, "m1", transport_error=True)
    assert _proxy_route_is_quarantined(sick, "m1", now=_time.monotonic())

    assert _choose_node(app, "m1") == "h-well", "a quarantined node was still picked"


async def test_a_worker_that_errors_opens_the_circuit_against_its_own_node():
    """The fault must land on whoever CLAIMED the work, not on whoever was picked.

    Under push those are the same node. Under pull they need not be -- the picker only suggests,
    and the transaction records who actually took it -- so blaming the pick would open a circuit
    against a node that never saw the request.
    """

    import asyncio
    import time as _time

    from local.server import Node, _proxy_route_is_quarantined, _proxy_openai, create_app

    app = create_app(grid_id="g1", grid_name="grid-one")
    node = Node(node_id="n1", role="engine", models=["m1"], host_id="h1",
                load={"active_tasks": 0}, last_heartbeat=_time.time())
    app.state.nodes = {"n1": node}

    from local.server import _ROUTE_FAILURE_THRESHOLD

    for _ in range(_ROUTE_FAILURE_THRESHOLD):
        request = Request({"type": "http", "method": "POST", "headers": []})
        request._body = b'{"model": "m1"}'
        serving = asyncio.create_task(_proxy_openai(app, "chat/completions", request))
        await asyncio.sleep(0)
        claimed = app.state.inflight.claim(node_id="h1", models=("m1",))
        assert claimed is not None
        app.state.inflight.cancel(claimed.id, "engine blew up")
        await asyncio.wait_for(serving, timeout=2.0)

    assert _proxy_route_is_quarantined(node, "m1", now=_time.monotonic()), \
        "consecutive worker failures opened no circuit"

    # And the consequence the circuit exists for: the next caller is refused up front rather
    # than parked on a transaction that the same broken node would claim and fail again.
    request = Request({"type": "http", "method": "POST", "headers": []})
    request._body = b'{"model": "m1"}'
    assert (await _proxy_openai(app, "chat/completions", request)).status_code == 503


async def test_a_served_pull_request_is_offered_to_capture(monkeypatch):
    """Capture turns real traffic into training data; it lived only on the push branch.

    Opt-in and off by default, which is why nothing noticed -- and why deleting the push path
    with it still only there would have removed the only place the feature worked.
    """

    import asyncio
    import json as _json

    from local import server as server_module
    from local.server import _proxy_openai

    app = _pull_app_with_node()
    offered: list[tuple[dict, bytes]] = []
    monkeypatch.setattr(
        server_module,
        "_capture_result",
        lambda body, result, ref="": offered.append((body, result)),
        raising=False,
    )

    request = Request({"type": "http", "method": "POST", "headers": []})
    request._body = b'{"model": "m1"}'
    serving = asyncio.create_task(_proxy_openai(app, "chat/completions", request))
    await asyncio.sleep(0)
    claimed = app.state.inflight.claim(node_id="h1", models=("m1",))
    result = _json.dumps({"choices": [{"message": {"content": "hi"}}]}).encode()
    app.state.inflight.finish(claimed.id, result)
    await asyncio.wait_for(serving, timeout=2.0)

    assert offered == [({"model": "m1"}, result)]
