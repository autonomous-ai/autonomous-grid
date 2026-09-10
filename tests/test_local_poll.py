from fastapi import FastAPI
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
