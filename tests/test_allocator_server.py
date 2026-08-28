from __future__ import annotations

import json
import ssl
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from cli import _main as cli_main
from local import config, runtime
from local import server as server_module
from local.server import create_app
from shared.allocator.auth import control_node_id, engine_node_id, mint_node_token

TOKEN = "allocator-test-token"
AUTH = {"X-Grid-Allocator-Token": TOKEN}
CONTROL_NODE_ID = control_node_id("host-1")


def _node_auth(host_id: str) -> dict[str, str]:
    return {"X-Grid-Allocator-Node-Token": mint_node_token(TOKEN, host_id)}


def _app(tmp_path: Path, *, token: str = TOKEN):
    state_path = tmp_path / "allocator.json"
    app = create_app(
        grid_id="ag-test",
        grid_name="test",
        allocator_state_path=state_path,
        allocator_control_token=token,
        allocator_interval_seconds=60,
    )
    return app, TestClient(app), state_path


def _profile(**overrides):
    return {
        "memory_mb": 8_000,
        "runtimes": ["llama.cpp"],
        "min_replicas": 1,
        "max_replicas": 1,
        "min_residency_seconds": 0,
        **overrides,
    }


def _managed_node(
    client: TestClient, *, node_id: str | None = None, host_id: str = "host-1"
):
    node_id = node_id or control_node_id(host_id)
    response = client.put(
        f"/nodes/{node_id}",
        headers=_node_auth(host_id),
        json={
            "role": "allocator",
            "host_id": host_id,
            "resources": {"capacity_mb": 16_000, "runtimes": ["llama.cpp"]},
            "allocator": {
                "managed": True,
                "cached_models": ["qwen"],
                "cost_per_hour": 2.5,
                "max_models": 2,
                "actuator_capabilities": ["load", "warm", "drain", "unload"],
            },
        },
    )
    assert response.status_code == 200, response.text


def _managed_engine(
    client: TestClient,
    *,
    node_id: str | None = None,
    model_id: str,
    host_id: str = "host-1",
    state: str = "ready",
    active_tasks: int = 0,
):
    node_id = node_id or engine_node_id(host_id, model_id)
    response = client.put(
        f"/nodes/{node_id}",
        headers=_node_auth(host_id),
        json={
            "role": "engine",
            "host_id": host_id,
            "models": [model_id],
            "endpoint_url": f"http://127.0.0.1:9000/v1/{model_id}",
            "load": {"active_tasks": active_tasks},
            "resources": {
                "capacity_mb": 16_000,
                "runtimes": ["llama.cpp"],
                "model_memory_mb": {model_id: 8_000},
            },
            "allocator": {
                "managed": True,
                "state": "draining" if state == "draining" else "accepting",
                "residencies": [
                    {"model_id": model_id, "memory_mb": 8_000, "state": state}
                ],
            },
        },
    )
    assert response.status_code == 200, response.text
    return response


def _queue_pinned_replacement_drain(app, client: TestClient):
    _managed_node(client, host_id="host-old")
    _managed_engine(client, host_id="host-old", model_id="qwen")
    _managed_node(client, host_id="host-new")
    _managed_engine(client, host_id="host-new", model_id="qwen")
    assert (
        client.put(
            "/allocator/models/qwen",
            headers=AUTH,
            json=_profile(pinned_nodes=["host-new"]),
        ).status_code
        == 200
    )
    assert (
        client.put(
            "/allocator/mode",
            headers=AUTH,
            json={"mode": "automatic"},
        ).status_code
        == 200
    )
    pending = app.state.allocator.status()["pending_commands"]
    drain = next(
        item
        for item in pending
        if item["kind"] == "drain" and item["node_id"] == "host-old"
    )
    return drain


def test_allocator_mutations_require_control_capability(tmp_path):
    _, client, _ = _app(tmp_path)

    assert client.put("/allocator/models/qwen", json=_profile()).status_code == 403
    assert (
        client.put(
            "/allocator/models/qwen",
            json=_profile(),
            headers={"X-Grid-Allocator-Token": "wrong"},
        ).status_code
        == 403
    )
    assert client.put("/allocator/mode", json={"mode": "observe"}).status_code == 403
    assert client.post("/allocator/tick").status_code == 403
    assert (
        client.put(
            "/nodes/spoofed",
            json={
                "role": "allocator",
                "host_id": "host-1",
                "allocator": {"managed": True},
            },
        ).status_code
        == 403
    )
    assert TOKEN not in client.get("/grid/info").text
    assert TOKEN not in client.get("/allocator/status").text

    # Bearer is accepted for clients whose HTTP stack already has an Authorization facility.
    response = client.put(
        "/allocator/models/qwen",
        json=_profile(),
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["model"]["model_id"] == "qwen"


def test_managed_registration_rejects_unsupported_allocator_schema(tmp_path):
    _, client, _ = _app(tmp_path)

    response = client.put(
        f"/nodes/{CONTROL_NODE_ID}",
        headers=_node_auth("host-1"),
        json={
            "role": "allocator",
            "host_id": "host-1",
            "resources": {"capacity_mb": 16_000, "runtimes": ["llama.cpp"]},
            "allocator": {"schema_version": 999, "managed": True},
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "unsupported allocator schema_version 999"


def test_allocator_model_mode_tick_and_persistence(tmp_path):
    _, client, state_path = _app(tmp_path)

    added = client.put("/allocator/models/qwen", json=_profile(), headers=AUTH)
    assert added.status_code == 200, added.text
    assert state_path.exists()
    changed = client.put("/allocator/mode", json={"mode": "observe"}, headers=AUTH)
    assert changed.status_code == 200
    assert changed.json()["mode"] == "observe"
    ticked = client.post("/allocator/tick", headers=AUTH)
    assert ticked.status_code == 200
    assert ticked.json()["mode"] == "observe"

    restored = create_app(
        grid_id="ag-test",
        grid_name="test",
        allocator_state_path=state_path,
        allocator_control_token=TOKEN,
    )
    status = TestClient(restored).get("/allocator/status").json()
    assert status["mode"] == "observe"
    assert [model["model_id"] for model in status["models"]] == ["qwen"]

    removed = client.delete("/allocator/models/qwen", headers=AUTH)
    assert removed.status_code == 200
    assert client.delete("/allocator/models/qwen", headers=AUTH).status_code == 404


def test_allocator_profile_route_supports_namespaced_model_ids(tmp_path):
    _, client, _ = _app(tmp_path)

    added = client.put(
        "/allocator/models/org/model%231",
        json=_profile(),
        headers=AUTH,
    )

    assert added.status_code == 200, added.text
    assert added.json()["model"]["model_id"] == "org/model#1"
    removed = client.delete("/allocator/models/org/model%231", headers=AUTH)
    assert removed.status_code == 200
    assert removed.json()["deleted"] == "org/model#1"


def test_request_demand_marks_allocator_dirty_for_an_event_driven_tick(tmp_path):
    app, client, _ = _app(tmp_path)
    assert (
        client.put("/allocator/models/qwen", json=_profile(), headers=AUTH).status_code
        == 200
    )
    before = app.state.allocator_dirty_revision

    response = client.post(
        "/v1/chat/completions",
        json={"model": "qwen", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 503
    assert app.state.allocator_dirty_revision == before + 1


def test_unconfigured_model_names_do_not_create_persisted_demand_series(tmp_path):
    app, client, _ = _app(tmp_path)
    before = app.state.allocator_dirty_revision

    response = client.post(
        "/v1/chat/completions",
        json={"model": "attacker-chosen-name", "messages": []},
    )

    assert response.status_code == 503
    assert app.state.allocator_dirty_revision == before
    assert app.state.allocator.demand.to_dict()["models"] == {}


def test_automatic_commands_are_visible_only_to_authenticated_host(tmp_path):
    _, client, _ = _app(tmp_path)
    _managed_node(client)
    assert (
        client.put("/allocator/models/qwen", json=_profile(), headers=AUTH).status_code
        == 200
    )
    automatic = client.put(
        "/allocator/mode",
        json={"mode": "automatic"},
        headers=AUTH,
    )
    assert automatic.status_code == 200, automatic.text

    anonymous = client.post("/nodes/heartbeat", json={"node_id": CONTROL_NODE_ID})
    assert anonymous.status_code == 403

    # The operator capability is deliberately not a worker-host capability.
    assert (
        client.post(
            "/nodes/heartbeat", json={"node_id": CONTROL_NODE_ID}, headers=AUTH
        ).status_code
        == 403
    )

    authorized = client.post(
        "/nodes/heartbeat",
        json={"node_id": CONTROL_NODE_ID},
        headers=_node_auth("host-1"),
    )
    assert authorized.status_code == 200, authorized.text
    commands = authorized.json()["allocator"]["commands"]
    assert len(commands) == 1
    assert commands[0]["node_id"] == "host-1"
    assert commands[0]["model_id"] == "qwen"
    assert commands[0]["kind"] == "warm"
    assert commands[0]["schema_version"] == 1

    unauthenticated_ack = client.post(
        "/nodes/heartbeat",
        json={
            "node_id": CONTROL_NODE_ID,
            "acknowledgements": [
                {"action_id": commands[0]["action_id"], "status": "running"}
            ],
        },
    )
    assert unauthenticated_ack.status_code == 403

    acknowledged = client.post(
        "/nodes/heartbeat",
        headers=_node_auth("host-1"),
        json={
            "node_id": CONTROL_NODE_ID,
            "acknowledgements": [
                {"action_id": commands[0]["action_id"], "status": "running"}
            ],
        },
    )
    assert acknowledged.status_code == 200, acknowledged.text
    history = client.get("/allocator/status").json()["history"]
    assert history[-1]["status"] == "running"


def test_stale_ack_is_ignored_without_poisoning_later_receipts(tmp_path):
    _, client, _ = _app(tmp_path)
    _managed_node(client)
    assert (
        client.put("/allocator/models/qwen", json=_profile(), headers=AUTH).status_code
        == 200
    )
    assert (
        client.put(
            "/allocator/mode", json={"mode": "automatic"}, headers=AUTH
        ).status_code
        == 200
    )
    command_response = client.post(
        "/nodes/heartbeat",
        headers=_node_auth("host-1"),
        json={"node_id": CONTROL_NODE_ID},
    )
    command = command_response.json()["allocator"]["commands"][0]

    response = client.post(
        "/nodes/heartbeat",
        headers=_node_auth("host-1"),
        json={
            "node_id": CONTROL_NODE_ID,
            "acknowledgements": [
                {"action_id": "unknown", "status": "succeeded"},
                {"action_id": command["action_id"], "status": "running"},
            ],
        },
    )
    assert response.status_code == 200, response.text
    results = response.json()["acknowledgements"]
    assert results[0]["result"] == "ignored"
    assert "unknown allocator action" in results[0]["error"]
    assert results[1] == {
        "action_id": command["action_id"],
        "status": "running",
        "result": "accepted",
    }
    history = client.get("/allocator/status").json()["history"]
    assert any(
        item["action_id"] == command["action_id"] and item["status"] == "running"
        for item in history
    )


def test_managed_record_cannot_be_downgraded_by_unauthenticated_overwrite(tmp_path):
    _, client, _ = _app(tmp_path)
    _managed_node(client)
    response = client.put(
        f"/nodes/{CONTROL_NODE_ID}",
        json={
            "role": "engine",
            "models": ["attacker-model"],
            "endpoint_url": "http://attacker.invalid/v1",
        },
    )
    assert response.status_code == 403
    nodes = client.get("/allocator/status").json()["nodes"]
    assert nodes[0]["node_id"] == "host-1"
    assert all(item["model_id"] != "attacker-model" for item in nodes[0]["residencies"])


def test_node_credentials_are_host_scoped_and_separate_from_operator_auth(tmp_path):
    _, client, _ = _app(tmp_path)
    host_b_node_id = control_node_id("host-b")
    payload = {
        "role": "allocator",
        "host_id": "host-b",
        "resources": {"capacity_mb": 16_000},
        "allocator": {"managed": True},
    }

    assert (
        client.put(f"/nodes/{host_b_node_id}", headers=AUTH, json=payload).status_code
        == 403
    )
    assert (
        client.put(
            f"/nodes/{host_b_node_id}", headers=_node_auth("host-a"), json=payload
        ).status_code
        == 403
    )
    assert (
        client.put(
            f"/nodes/{host_b_node_id}", headers=_node_auth("host-b"), json=payload
        ).status_code
        == 200
    )

    wrong_heartbeat = client.post(
        "/nodes/heartbeat",
        headers=_node_auth("host-a"),
        json={"node_id": host_b_node_id, "load": {"active_tasks": 9}},
    )
    assert wrong_heartbeat.status_code == 403
    assert client.get("/allocator/status").json()["nodes"][0]["active_requests"] == 0

    changed = client.put(
        f"/nodes/{host_b_node_id}",
        headers=_node_auth("host-b"),
        json={**payload, "host_id": "host-a"},
    )
    assert changed.status_code == 409
    assert (
        client.delete(
            f"/nodes/{host_b_node_id}", headers=_node_auth("host-a")
        ).status_code
        == 403
    )
    assert (
        client.delete(
            f"/nodes/{host_b_node_id}", headers=_node_auth("host-b")
        ).status_code
        == 200
    )


def test_host_credential_cannot_squat_another_hosts_registry_id(tmp_path):
    _, client, _ = _app(tmp_path)
    victim_id = control_node_id("host-a")
    attacker = client.put(
        f"/nodes/{victim_id}",
        headers=_node_auth("host-b"),
        json={
            "role": "allocator",
            "host_id": "host-b",
            "allocator": {"managed": True},
        },
    )
    assert attacker.status_code == 409

    legitimate = client.put(
        f"/nodes/{victim_id}",
        headers=_node_auth("host-a"),
        json={
            "role": "allocator",
            "host_id": "host-a",
            "allocator": {"managed": True},
        },
    )
    assert legitimate.status_code == 200


@pytest.mark.parametrize(
    "residencies",
    [
        {"not": "an array"},
        [
            {"model_id": "qwen", "memory_mb": 8_000},
            {"model_id": "qwen", "memory_mb": 8_000},
        ],
        [{"model_id": "qwen", "memory_mb": 10**1_000}],
    ],
)
def test_malformed_managed_residencies_are_rejected_without_poisoning_status(
    tmp_path,
    residencies,
):
    _, client, _ = _app(tmp_path)
    response = client.put(
        f"/nodes/{CONTROL_NODE_ID}",
        headers=_node_auth("host-1"),
        json={
            "role": "allocator",
            "host_id": "host-1",
            "allocator": {"managed": True, "residencies": residencies},
        },
    )

    assert response.status_code == 400
    assert client.get("/allocator/status").status_code == 200


def test_explicit_empty_data_tier_policy_fails_closed(tmp_path):
    _, client, _ = _app(tmp_path)
    response = client.put(
        f"/nodes/{CONTROL_NODE_ID}",
        headers=_node_auth("host-1"),
        json={
            "role": "allocator",
            "host_id": "host-1",
            "resources": {"capacity_mb": 16_000, "runtimes": ["llama.cpp"]},
            "allocator": {"managed": True, "allowed_data_tiers": []},
        },
    )
    assert response.status_code == 200
    assert (
        client.get("/allocator/status").json()["nodes"][0]["allowed_data_tiers"] == []
    )


def test_legacy_registration_bounds_node_and_model_identity(tmp_path):
    _, client, _ = _app(tmp_path)
    too_long = "x" * 1_025
    payload = {
        "role": "engine",
        "models": [too_long],
        "endpoint_url": "http://127.0.0.1:9000/v1",
    }
    assert client.put("/nodes/legacy", json=payload).status_code == 400
    assert (
        client.put(
            f"/nodes/{too_long}",
            json={**payload, "models": ["qwen"]},
        ).status_code
        == 400
    )
    assert client.get("/allocator/status").status_code == 200


def test_snapshot_aggregate_numeric_limits_cannot_freeze_allocator(tmp_path):
    _, client, _ = _app(tmp_path)
    response = client.put(
        f"/nodes/{CONTROL_NODE_ID}",
        headers=_node_auth("host-1"),
        json={
            "role": "allocator",
            "host_id": "host-1",
            "resources": {
                "capacity_mb": 1_000_000_000,
                "reserved_mb": 1_000_000_000,
                "runtimes": ["llama.cpp"],
            },
            "allocator": {
                "managed": True,
                "residencies": [
                    {
                        "model_id": "qwen",
                        "memory_mb": 1_000_000_000,
                        "state": "ready",
                    }
                ],
            },
        },
    )
    assert response.status_code == 200
    status = client.get("/allocator/status")
    assert status.status_code == 200
    assert status.json()["nodes"][0]["capacity_mb"] == 1_000_000_000


def test_managed_reregistration_preserves_proxy_owned_load(tmp_path):
    _, client, _ = _app(tmp_path)
    child_id = engine_node_id("host-1", "qwen")
    _managed_engine(
        client,
        node_id=child_id,
        model_id="qwen",
        active_tasks=2,
    )
    response = client.put(
        f"/nodes/{child_id}",
        headers=_node_auth("host-1"),
        json={
            "role": "engine",
            "host_id": "host-1",
            "models": ["qwen"],
            "endpoint_url": "http://127.0.0.1:9000/v1/qwen",
            "allocator": {
                "managed": True,
                "residencies": [
                    {"model_id": "qwen", "memory_mb": 8_000, "state": "ready"}
                ],
            },
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["node"]["load"]["active_tasks"] == 2

    heartbeat = client.post(
        "/nodes/heartbeat",
        headers=_node_auth("host-1"),
        json={"node_id": child_id},
    )
    assert heartbeat.status_code == 200
    assert heartbeat.json()["load"]["active_tasks"] == 2


def test_external_vllm_runtime_hint_is_routable_but_never_grants_management(tmp_path):
    app, client, _ = _app(tmp_path)
    response = client.put(
        "/nodes/legacy",
        json={
            "role": "engine",
            "models": ["qwen"],
            "endpoint_url": "http://127.0.0.1:9000/v1",
            "resources": {
                "capacity_mb": 16_000,
                "runtimes": ["vllm"],
                "backends": ["cuda"],
                "gpu_count": 2,
                "gpu_memory_mb": [96_000, 96_000],
            },
            "load": {"active_tasks": 0, "max_concurrency": 16},
        },
    )
    assert response.status_code == 200
    assert client.get("/nodes/discover", params={"model": "qwen"}).json()["engines"]

    status = client.get("/allocator/status").json()
    node = status["nodes"][0]
    assert node["runtimes"] == ["vllm"]
    assert node["backends"] == ["cuda"]
    assert node["manually_managed"] is True
    assert node["actuator_capabilities"] == []
    assert node["max_concurrency"] == 16
    assert node["gpu_count"] == 2
    assert node["gpu_memory_mb"] == [96_000, 96_000]
    assert node["residencies"][0]["state"] == "ready"
    assert node["residencies"][0]["managed"] is False
    discovered = client.get("/nodes/discover", params={"model": "qwen"}).json()[
        "engines"
    ][0]
    assert discovered["max_concurrency"] == 16
    engine = app.state.nodes["legacy"]
    assert server_module._choose_engine(app, "qwen") is engine
    server_module._change_active_tasks(engine, 16)
    assert server_module._choose_engine(app, "qwen") is None


@pytest.mark.parametrize("state", ["draining", "paused", "unhealthy", "quarantined"])
def test_protected_node_states_are_removed_from_routing(tmp_path, state):
    _, client, _ = _app(tmp_path)
    protected_id = engine_node_id("host-1", "qwen")
    response = client.put(
        f"/nodes/{protected_id}",
        headers=_node_auth("host-1"),
        json={
            "role": "engine",
            "host_id": "host-1",
            "models": ["qwen"],
            "endpoint_url": "http://127.0.0.1:9000/v1",
            "allocator": {"managed": True, "state": state},
        },
    )
    assert response.status_code == 200
    assert (
        client.get("/nodes/discover", params={"model": "qwen"}).json()["engines"] == []
    )


def test_engine_and_agent_records_for_one_host_do_not_double_capacity(tmp_path):
    _, client, _ = _app(tmp_path)
    _managed_node(client)
    child_id = engine_node_id("host-1", "qwen")
    response = client.put(
        f"/nodes/{child_id}",
        headers=_node_auth("host-1"),
        json={
            "role": "engine",
            "host_id": "host-1",
            "models": ["qwen"],
            "endpoint_url": "http://127.0.0.1:9000/v1",
            "resources": {"capacity_mb": 16_000, "runtimes": ["llama.cpp"]},
            "allocator": {
                "managed": True,
                "cost_per_hour": 2.5,
                "max_models": 4,
                "residencies": [
                    {"model_id": "qwen", "memory_mb": 8_000, "state": "ready"}
                ],
            },
        },
    )
    assert response.status_code == 200

    nodes = client.get("/allocator/status").json()["nodes"]
    assert len(nodes) == 1
    assert nodes[0]["node_id"] == "host-1"
    assert nodes[0]["capacity_mb"] == 16_000
    assert nodes[0]["cost_per_hour"] == 2.5
    assert nodes[0]["max_models"] == 2
    assert nodes[0]["max_concurrency"] == 1
    assert [item["model_id"] for item in nodes[0]["residencies"]] == ["qwen"]


def test_control_only_ready_replacement_cannot_drain_live_route(tmp_path):
    _, client, _ = _app(tmp_path)
    _managed_node(client, host_id="host-old")
    _managed_engine(client, host_id="host-old", model_id="qwen")
    _managed_node(client, host_id="host-new")
    control = client.post(
        "/nodes/heartbeat",
        headers=_node_auth("host-new"),
        json={
            "node_id": control_node_id("host-new"),
            "allocator": {
                "managed": True,
                "host_id": "host-new",
                "state": "accepting",
                "cached_models": ["qwen"],
                "actuator_capabilities": ["load", "warm", "drain", "unload"],
                "residencies": [
                    {
                        "model_id": "qwen",
                        "memory_mb": 8_000,
                        "state": "ready",
                        "managed": True,
                    }
                ],
            },
        },
    )
    assert control.status_code == 200, control.text
    assert (
        client.put(
            "/allocator/models/qwen",
            headers=AUTH,
            json=_profile(pinned_nodes=["host-new"]),
        ).status_code
        == 200
    )
    assert (
        client.put(
            "/allocator/mode",
            headers=AUTH,
            json={"mode": "automatic"},
        ).status_code
        == 200
    )

    before = client.get("/allocator/status").json()
    new_host = next(item for item in before["nodes"] if item["node_id"] == "host-new")
    assert new_host["residencies"][0]["state"] == "warming"
    assert not any(
        item["kind"] == "drain" and item["node_id"] == "host-old"
        for item in before["pending_commands"]
    )
    assert any(
        item["node_id"] == "host-old" and item["code"] == "replacement_not_ready"
        for item in before["reconciliation"]["deferred"]
    )

    _managed_engine(client, host_id="host-new", model_id="qwen")
    tick = client.post("/allocator/tick", headers=AUTH)
    assert tick.status_code == 200, tick.text
    after = client.get("/allocator/status").json()
    assert any(
        item["kind"] == "drain" and item["node_id"] == "host-old"
        for item in after["pending_commands"]
    )


@pytest.mark.parametrize(
    ("child_state", "control_state", "stale_child", "decision_accept"),
    [
        ("paused", "accepting", False, None),
        ("accepting", "draining", False, None),
        ("accepting", "draining", True, None),
        ("accepting", "accepting", True, None),
        ("accepting", "accepting", False, False),
    ],
)
def test_nonroutable_managed_child_cannot_supply_ready_evidence(
    tmp_path,
    child_state,
    control_state,
    stale_child,
    decision_accept,
):
    app, client, _ = _app(tmp_path)
    host_id = "host-new"
    _managed_node(client, host_id=host_id)
    control = app.state.nodes[control_node_id(host_id)]
    control.allocator.update(
        {
            "managed": True,
            "state": control_state,
            "residencies": [
                {
                    "model_id": "qwen",
                    "memory_mb": 8_000,
                    "state": "ready",
                    "managed": True,
                }
            ],
        }
    )
    child_id = engine_node_id(host_id, "qwen")
    response = client.put(
        f"/nodes/{child_id}",
        headers=_node_auth(host_id),
        json={
            "role": "engine",
            "host_id": host_id,
            "models": ["qwen"],
            "endpoint_url": "http://127.0.0.1:9000/v1/qwen",
            "resources": {"capacity_mb": 16_000, "runtimes": ["llama.cpp"]},
            "allocator": {
                "managed": True,
                "state": child_state,
                **(
                    {
                        "decision": {
                            "state": child_state,
                            "accept": decision_accept,
                        }
                    }
                    if decision_accept is not None
                    else {}
                ),
                "residencies": [
                    {
                        "model_id": "qwen",
                        "memory_mb": 8_000,
                        "state": "ready",
                        "managed": True,
                    }
                ],
            },
        },
    )
    assert response.status_code == 200, response.text
    if stale_child:
        child = app.state.nodes[child_id]
        child.proxy_active_tasks = 1
        child.last_heartbeat = time.time() - server_module.NODE_TTL_SECONDS - 1

    snapshot = server_module._allocator_snapshots(app)[0]
    expected_state = (
        server_module.ResidencyState.READY
        if control_state == "draining" and not stale_child
        else server_module.ResidencyState.WARMING
    )
    assert snapshot.residency("qwen").state == expected_state
    assert server_module._active_engines(app, "qwen") == []


@pytest.mark.parametrize(
    "control_state",
    ("draining", "paused", "unhealthy", "quarantined"),
)
def test_host_control_state_atomically_fences_all_child_routing(
    tmp_path,
    control_state,
):
    app, client, _ = _app(tmp_path)
    _managed_node(client)
    _managed_engine(client, model_id="qwen")
    _managed_engine(client, model_id="llama")
    assert len(client.get("/nodes/discover").json()["engines"]) == 2

    response = client.post(
        "/nodes/heartbeat",
        headers=_node_auth("host-1"),
        json={
            "node_id": CONTROL_NODE_ID,
            "allocator": {
                "managed": True,
                "host_id": "host-1",
                "state": control_state,
            },
        },
    )
    assert response.status_code == 200, response.text
    assert client.get("/nodes/discover").json()["engines"] == []
    # Routing state and process state are different facts. The child is still a live READY process
    # that reconciliation must see in order to drain and unload it after a replacement is ready.
    snapshot = server_module._allocator_snapshots(app)[0]
    assert snapshot.state == server_module.NodeState(control_state)
    assert snapshot.residency("qwen").state == server_module.ResidencyState.READY
    assert snapshot.residency("llama").state == server_module.ResidencyState.READY


def test_child_drain_does_not_poison_sibling_or_host_lifecycle(tmp_path):
    _, client, _ = _app(tmp_path)
    _managed_node(client)
    _managed_engine(
        client,
        model_id="qwen",
        state="draining",
    )
    _managed_engine(client, model_id="llama", active_tasks=5)

    assert (
        client.get("/nodes/discover", params={"model": "qwen"}).json()["engines"] == []
    )
    llama = client.get("/nodes/discover", params={"model": "llama"}).json()["engines"]
    assert [item["node_id"] for item in llama] == [engine_node_id("host-1", "llama")]

    host = client.get("/allocator/status").json()["nodes"][0]
    assert host["state"] == "accepting"
    residencies = {item["model_id"]: item for item in host["residencies"]}
    assert residencies["qwen"]["state"] == "draining"
    assert residencies["qwen"]["active_requests"] == 0
    assert residencies["llama"]["state"] == "ready"
    assert residencies["llama"]["active_requests"] == 5


def test_empty_heartbeat_preserves_proxy_owned_in_flight_load(tmp_path):
    _, client, _ = _app(tmp_path)
    response = client.put(
        "/nodes/child-engine",
        json={
            "role": "engine",
            "models": ["qwen"],
            "endpoint_url": "http://127.0.0.1:9000/v1",
            "load": {"active_tasks": 2},
        },
    )
    assert response.status_code == 200

    assert (
        client.post("/nodes/heartbeat", json={"node_id": "child-engine"}).status_code
        == 200
    )
    engine = client.get("/nodes/discover").json()["engines"][0]
    assert engine["load"]["active_tasks"] == 2

    assert (
        client.post(
            "/nodes/heartbeat",
            json={"node_id": "child-engine", "load": {"active_tasks": 1}},
        ).status_code
        == 200
    )
    engine = client.get("/nodes/discover").json()["engines"][0]
    assert engine["load"]["active_tasks"] == 1


def test_managed_runtime_and_proxy_activity_are_combined_without_double_counting(
    tmp_path,
):
    app, client, _ = _app(tmp_path)
    child_id = engine_node_id("host-1", "qwen")
    _managed_engine(client, node_id=child_id, model_id="qwen", active_tasks=2)
    engine = app.state.nodes[child_id]

    # llama.cpp's two busy slots already include the request currently owned by this proxy.
    server_module._change_active_tasks(engine, 1)
    assert engine.load["active_tasks"] == 2

    # A fresh runtime sample can fall to zero before the proxy response finalizer runs. The exact
    # proxy counter remains authoritative until its own finally block decrements it.
    response = client.post(
        "/nodes/heartbeat",
        headers=_node_auth("host-1"),
        json={"node_id": child_id, "load": {"active_tasks": 0}},
    )
    assert response.status_code == 200
    assert response.json()["load"]["active_tasks"] == 1
    assert (
        "reported_active_tasks"
        not in client.get("/nodes/discover").json()["engines"][0]
    )

    server_module._change_active_tasks(engine, -1)
    assert engine.load["active_tasks"] == 0


def test_managed_engine_key_is_private_and_forwarded_only_by_grid(
    tmp_path,
    monkeypatch,
):
    app, client, _ = _app(tmp_path)
    child_id = engine_node_id("host-1", "qwen")
    engine_key = "managed-engine-key-0123456789abcdef"
    response = client.put(
        f"/nodes/{child_id}",
        headers=_node_auth("host-1"),
        json={
            "role": "engine",
            "host_id": "host-1",
            "models": ["qwen"],
            "endpoint_url": "http://127.0.0.1:9000/v1",
            "engine_api_key": engine_key,
            "allocator": {"managed": True},
        },
    )
    assert response.status_code == 200, response.text
    assert "engine_api_key" not in response.json()["node"]
    assert "engine_api_key" not in client.get("/nodes/discover").json()["engines"][0]
    assert "engine_api_key" not in client.get("/allocator/status").text
    assert app.state.nodes[child_id].engine_api_key == engine_key

    seen: list[str | None] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"id": "completion"})

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        server_module.httpx,
        "AsyncClient",
        lambda *args, **kwargs: real_async_client(
            *args,
            **{**kwargs, "transport": httpx.MockTransport(upstream)},
        ),
    )
    proxied = client.post(
        "/v1/chat/completions",
        json={"model": "qwen", "messages": []},
    )
    assert proxied.status_code == 200
    assert seen == [f"Bearer {engine_key}"]


@pytest.mark.parametrize(
    "endpoint_url",
    (
        "http://127.0.0.1:9000/v1",
        "http://[::1]:9000/v1",
        "https://worker.internal:9000/v1",
    ),
)
def test_managed_transport_accepts_https_and_same_process_loopback(
    tmp_path,
    endpoint_url,
):
    _, client, _ = _app(tmp_path)
    response = client.put(
        f"/nodes/{engine_node_id('host-1', 'qwen')}",
        headers=_node_auth("host-1"),
        json={
            "role": "engine",
            "host_id": "host-1",
            "models": ["qwen"],
            "endpoint_url": endpoint_url,
            "allocator": {"managed": True},
        },
    )
    assert response.status_code == 200, response.text


def test_managed_transport_rejects_plaintext_lan_endpoint(tmp_path):
    _, client, _ = _app(tmp_path)
    response = client.put(
        f"/nodes/{engine_node_id('host-1', 'qwen')}",
        headers=_node_auth("host-1"),
        json={
            "role": "engine",
            "host_id": "host-1",
            "models": ["qwen"],
            "endpoint_url": "http://10.0.0.5:9000/v1",
            "allocator": {"managed": True},
        },
    )
    assert response.status_code == 400
    assert "end-to-end HTTPS" in response.text


def test_remote_registration_cannot_claim_central_loopback_plaintext():
    request = Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/nodes/example",
            "raw_path": b"/nodes/example",
            "query_string": b"",
            "headers": [],
            "scheme": "https",
            "server": ("grid.internal", 443),
            "client": ("10.0.0.5", 54_321),
        }
    )
    with pytest.raises(server_module.HTTPException, match="registering peer"):
        server_module._validate_managed_endpoint_transport(
            "http://127.0.0.1:9000/v1",
            request=request,
        )


def _memory_tls_handshake(client_context: ssl.SSLContext, hostname: str) -> None:
    fixture_dir = Path(__file__).parent / "fixtures"
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(
        fixture_dir / "allocator_tls_server.pem",
        fixture_dir / "allocator_tls_server.key",
    )
    client_in, client_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    server_in, server_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = client_context.wrap_bio(
        client_in,
        client_out,
        server_side=False,
        server_hostname=hostname,
    )
    server = server_context.wrap_bio(server_in, server_out, server_side=True)
    client_done = server_done = False
    for _ in range(20):
        if not client_done:
            try:
                client.do_handshake()
                client_done = True
            except ssl.SSLWantReadError:
                pass
        data = client_out.read()
        if data:
            server_in.write(data)
        if not server_done:
            try:
                server.do_handshake()
                server_done = True
            except ssl.SSLWantReadError:
                pass
        data = server_out.read()
        if data:
            client_in.write(data)
        if client_done and server_done:
            return
    raise AssertionError("in-memory TLS handshake did not finish")


def test_private_engine_ca_is_private_and_enforces_chain_and_hostname(tmp_path):
    app, client, _ = _app(tmp_path)
    ca_pem = (Path(__file__).parent / "fixtures" / "allocator_tls_ca.pem").read_text()
    child_id = engine_node_id("host-1", "qwen")
    response = client.put(
        f"/nodes/{child_id}",
        headers=_node_auth("host-1"),
        json={
            "role": "engine",
            "host_id": "host-1",
            "models": ["qwen"],
            "endpoint_url": "https://localhost:9000/v1",
            "allocator": {
                "managed": True,
                "engine_tls_ca_pem": ca_pem,
            },
        },
    )
    assert response.status_code == 200, response.text
    assert "BEGIN CERTIFICATE" not in response.text
    assert "BEGIN CERTIFICATE" not in client.get("/nodes/discover").text
    assert "engine_tls_ca_pem" not in client.get("/allocator/status").text
    node = app.state.nodes[child_id]
    assert node.engine_tls_ca_pem == ca_pem

    context = server_module._engine_tls_verify(node)
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    _memory_tls_handshake(context, "localhost")

    wrong_host_context = server_module._engine_tls_verify(node)
    assert isinstance(wrong_host_context, ssl.SSLContext)
    with pytest.raises(ssl.SSLCertVerificationError):
        _memory_tls_handshake(wrong_host_context, "127.0.0.1")


def test_explicit_empty_managed_load_clears_only_reported_activity(tmp_path):
    app, client, _ = _app(tmp_path)
    child_id = engine_node_id("host-1", "qwen")
    _managed_engine(client, node_id=child_id, model_id="qwen", active_tasks=2)
    engine = app.state.nodes[child_id]
    server_module._change_active_tasks(engine, 1)

    response = client.post(
        "/nodes/heartbeat",
        headers=_node_auth("host-1"),
        json={"node_id": child_id, "load": {}},
    )
    assert response.status_code == 200
    assert response.json()["load"]["active_tasks"] == 1


def test_explicit_request_fields_support_pydantic_one_and_two_names():
    class PydanticOneRequest:
        def __init__(self):
            self.__fields_set__ = {"load"}

    class PydanticTwoRequest:
        def __init__(self):
            self.model_fields_set = {"resources"}
            self.__fields_set__ = {"load"}

    assert server_module._request_fields_set(PydanticOneRequest()) == {"load"}
    assert server_module._request_fields_set(PydanticTwoRequest()) == {"resources"}


def test_allocator_failure_never_breaks_registration_or_heartbeat(
    tmp_path, monkeypatch
):
    app, client, _ = _app(tmp_path)

    def fail_tick(*args, **kwargs):
        raise RuntimeError("bad allocator telemetry")

    monkeypatch.setattr(app.state.allocator, "tick", fail_tick)
    registered = client.put(
        "/nodes/node-1",
        json={
            "role": "engine",
            "models": ["qwen"],
            "endpoint_url": "http://127.0.0.1:9000/v1",
        },
    )
    assert registered.status_code == 200
    assert (
        client.post("/nodes/heartbeat", json={"node_id": "node-1"}).status_code == 200
    )
    assert client.post("/allocator/tick", headers=AUTH).status_code == 200
    status = client.get("/allocator/status")
    assert status.status_code == 200
    assert status.json()["last_error"] == "bad allocator telemetry"


def test_allocator_status_is_read_only_and_manual_tick_runs_off_event_loop(
    tmp_path,
    monkeypatch,
):
    app, client, _ = _app(tmp_path)
    calls: list[int] = []
    original_tick = app.state.allocator.tick

    def tracked_tick(*args, **kwargs):
        calls.append(threading.get_ident())
        return original_tick(*args, **kwargs)

    async def request_thread():
        return {"thread_id": threading.get_ident()}

    app.get("/__test/request-thread")(request_thread)
    monkeypatch.setattr(app.state.allocator, "tick", tracked_tick)

    assert client.get("/allocator/status").status_code == 200
    assert calls == []
    event_loop_thread = client.get("/__test/request-thread").json()["thread_id"]
    assert client.post("/allocator/tick", headers=AUTH).status_code == 200
    assert len(calls) == 1
    assert calls[0] != event_loop_thread


def test_allocator_worker_coalesces_heartbeat_bursts(tmp_path, monkeypatch):
    state_path = tmp_path / "allocator.json"
    app = create_app(
        grid_id="ag-test",
        grid_name="test",
        allocator_state_path=state_path,
        allocator_control_token=TOKEN,
        allocator_interval_seconds=60,
        allocator_coalesce_seconds=0.08,
        allocator_min_tick_seconds=0,
    )
    calls: list[float] = []
    calls_lock = threading.Lock()
    ticked = threading.Event()
    original_tick = app.state.allocator.tick

    def tracked_tick(*args, **kwargs):
        with calls_lock:
            calls.append(time.monotonic())
        ticked.set()
        return original_tick(*args, **kwargs)

    monkeypatch.setattr(app.state.allocator, "tick", tracked_tick)
    with TestClient(app) as client:
        assert ticked.wait(1)
        ticked.clear()
        with calls_lock:
            calls.clear()

        registered = client.put(
            "/nodes/legacy",
            json={
                "role": "engine",
                "models": ["qwen"],
                "endpoint_url": "http://127.0.0.1:9000/v1",
            },
        )
        assert registered.status_code == 200
        assert ticked.wait(1)
        ticked.clear()
        with calls_lock:
            calls.clear()

        for _ in range(8):
            heartbeat = client.post(
                "/nodes/heartbeat",
                json={"node_id": "legacy"},
            )
            assert heartbeat.status_code == 200
        assert ticked.wait(1)
        time.sleep(0.12)
        with calls_lock:
            assert len(calls) == 1


@pytest.mark.parametrize(
    "state_contents",
    ["{not-json", json.dumps({"schema_version": 999})],
)
def test_invalid_allocator_state_is_quarantined_and_recovers_in_recommend_mode(
    tmp_path,
    state_contents,
):
    state_path = tmp_path / "allocator.json"
    state_path.write_text(state_contents)
    app = create_app(
        grid_id="ag-test",
        grid_name="test",
        allocator_state_path=state_path,
        allocator_control_token=TOKEN,
    )
    client = TestClient(app)

    status = client.get("/allocator/status")
    assert status.status_code == 200
    payload = status.json()
    assert payload["mode"] == "recommend"
    assert "recovered in recommend mode" in payload["warning"]
    quarantine_path = Path(payload["state_quarantine"])
    assert quarantine_path.exists()
    assert quarantine_path.read_text() == state_contents
    assert not state_path.exists()
    assert TOKEN not in status.text

    added = client.put("/allocator/models/qwen", json=_profile(), headers=AUTH)
    assert added.status_code == 200, added.text
    assert state_path.exists()
    # Recovery remains surfaced after successful persistence/ticks; it is not a transient log line.
    assert client.get("/allocator/status").json()["warning"] == payload["warning"]


def test_automatic_mode_is_denied_when_required_persistence_cannot_be_recovered(
    tmp_path,
    monkeypatch,
):
    state_path = tmp_path / "allocator.json"
    state_path.write_text("{broken", encoding="utf-8")
    original_replace = Path.replace

    def fail_quarantine(path: Path, target: Path):
        if path == state_path:
            raise OSError("read-only state directory")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_quarantine)
    app = create_app(
        grid_id="ag-test",
        grid_name="test",
        allocator_state_path=state_path,
        allocator_control_token=TOKEN,
    )
    client = TestClient(app)

    response = client.put(
        "/allocator/mode",
        json={"mode": "automatic"},
        headers=AUTH,
    )

    assert response.status_code == 409
    assert "requires durable allocator state" in response.json()["detail"]
    assert client.get("/allocator/status").json()["mode"] == "recommend"
    assert state_path.read_text(encoding="utf-8") == "{broken"


def test_automatic_mode_is_denied_without_a_durable_state_path():
    app = create_app(
        grid_id="ag-test",
        grid_name="test",
        allocator_control_token=TOKEN,
    )
    client = TestClient(app)

    response = client.put(
        "/allocator/mode",
        json={"mode": "automatic"},
        headers=AUTH,
    )

    assert response.status_code == 409
    assert "requires durable allocator state" in response.json()["detail"]
    assert client.get("/allocator/status").json()["mode"] == "recommend"


def test_throttled_engines_are_lower_priority_and_honor_admission_limit(tmp_path):
    app, client, _ = _app(tmp_path)
    _managed_engine(client, host_id="host-accepting", model_id="qwen")
    _managed_engine(client, host_id="host-throttled", model_id="qwen")
    accepting = app.state.nodes[engine_node_id("host-accepting", "qwen")]
    throttled = app.state.nodes[engine_node_id("host-throttled", "qwen")]
    accepting.allocator["max_concurrency"] = 1
    throttled.allocator.update(
        {
            "state": "throttled",
            "max_concurrency": 2,
            "decision": {
                "state": "throttled",
                "accept": True,
                "concurrency_multiplier": 0.5,
                "priority_multiplier": 0.5,
            },
        }
    )

    assert server_module._choose_engine(app, "qwen") is accepting
    accepting.load["active_tasks"] = 1
    assert server_module._choose_engine(app, "qwen") is throttled
    throttled.load["active_tasks"] = 1
    assert server_module._choose_engine(app, "qwen") is None
    throttled.load["active_tasks"] = 0
    throttled.allocator["max_concurrency"] = 0
    assert server_module._choose_engine(app, "qwen") is None


def test_router_normalizes_load_by_heterogeneous_engine_capacity(tmp_path):
    app, client, _ = _app(tmp_path)
    _managed_engine(client, host_id="narrow", model_id="qwen", active_tasks=1)
    _managed_engine(client, host_id="wide", model_id="qwen", active_tasks=2)
    narrow = app.state.nodes[engine_node_id("narrow", "qwen")]
    wide = app.state.nodes[engine_node_id("wide", "qwen")]
    narrow.allocator["max_concurrency"] = 2
    wide.allocator["max_concurrency"] = 16

    assert server_module._route_load_score(narrow) == 0.5
    assert server_module._route_load_score(wide) == 0.125
    assert server_module._choose_engine(app, "qwen") is wide

    # Once the wide engine is proportionally busier, the narrow engine becomes the better target.
    wide.load["active_tasks"] = 9
    assert server_module._choose_engine(app, "qwen") is narrow


def test_router_keeps_unknown_capacity_conservative_and_zero_capacity_closed(tmp_path):
    app, client, _ = _app(tmp_path)
    _managed_engine(client, host_id="unknown", model_id="qwen", active_tasks=1)
    _managed_engine(client, host_id="known", model_id="qwen", active_tasks=8)
    unknown = app.state.nodes[engine_node_id("unknown", "qwen")]
    known = app.state.nodes[engine_node_id("known", "qwen")]
    unknown.allocator.pop("max_concurrency", None)
    unknown.load.pop("max_concurrency", None)
    known.allocator["max_concurrency"] = 16

    assert server_module._route_load_score(unknown) == 1
    assert server_module._route_load_score(known) == 0.5
    assert server_module._choose_engine(app, "qwen") is known

    known.allocator["max_concurrency"] = 0
    assert server_module._route_load_score(known) == float("inf")
    assert server_module._choose_engine(app, "qwen") is unknown


def test_router_distributes_demand_in_proportion_to_engine_capacity(tmp_path):
    app, client, _ = _app(tmp_path)
    engines = {}
    for host_id, limit in (("narrow", 2), ("medium", 4), ("wide", 16)):
        _managed_engine(client, host_id=host_id, model_id="qwen")
        engine = app.state.nodes[engine_node_id(host_id, "qwen")]
        engine.allocator["max_concurrency"] = limit
        engines[host_id] = engine

    for _ in range(11):
        selected = server_module._choose_engine(app, "qwen")
        assert selected is not None
        server_module._change_active_tasks(selected, 1)

    assert {
        host_id: engine.load["active_tasks"] for host_id, engine in engines.items()
    } == {"narrow": 1, "medium": 2, "wide": 8}
    assert all(
        engine.load["active_tasks"] <= engine.allocator["max_concurrency"]
        for engine in engines.values()
    )


def test_proxy_owned_last_used_timestamp_survives_allocator_snapshot(tmp_path):
    app, client, _ = _app(tmp_path)
    _managed_engine(client, host_id="host-used", model_id="qwen")
    engine = app.state.nodes[engine_node_id("host-used", "qwen")]
    before = time.time()

    server_module._mark_engine_used(engine, "qwen")
    snapshot = server_module._allocator_snapshots(app)[0]
    residency = next(item for item in snapshot.residencies if item.model_id == "qwen")

    assert residency.last_used_at >= before
    assert residency.last_used_at == engine.model_last_used_at["qwen"]


def test_proxy_performance_ewma_is_private_and_overrides_reported_estimates(
    tmp_path,
    monkeypatch,
):
    app, client, _ = _app(tmp_path)
    _managed_engine(client, host_id="host-perf", model_id="qwen")
    child_id = engine_node_id("host-perf", "qwen")
    engine = app.state.nodes[child_id]
    clock = iter((102.0, 104.0))
    with monkeypatch.context() as context:
        context.setattr(server_module.time, "monotonic", lambda: next(clock))
        server_module._record_engine_performance(
            engine,
            100.0,
            model="qwen",
            status_code=200,
            response=httpx.Response(200, json={"usage": {"completion_tokens": 20}}),
        )
        server_module._record_engine_performance(
            engine,
            100.0,
            model="qwen",
            status_code=200,
            response=httpx.Response(200, json={"usage": {"completion_tokens": 80}}),
        )

    assert engine.proxy_performance_samples == 2
    assert engine.proxy_latency_ms == pytest.approx(2_400)
    assert engine.proxy_tokens_per_second == pytest.approx(12)
    assert engine.proxy_model_performance["qwen"].latency_ms == pytest.approx(2_400)
    assert engine.proxy_model_performance["qwen"].tokens_per_second == pytest.approx(12)
    assert engine.proxy_model_performance["qwen"].updated_at > 0
    public = engine.public_dict()
    assert "proxy_latency_ms" not in public
    assert "proxy_tokens_per_second" not in public
    assert "proxy_performance_samples" not in public
    assert "proxy_model_performance" not in public

    heartbeat = client.post(
        "/nodes/heartbeat",
        headers=_node_auth("host-perf"),
        json={
            "node_id": child_id,
            "load": {"latency_ms": 99_000, "tokens_per_second": 1},
        },
    )
    assert heartbeat.status_code == 200
    snapshot = server_module._allocator_snapshots(app)[0]
    assert snapshot.latency_ms == pytest.approx(2_400)
    assert snapshot.tokens_per_second == pytest.approx(12)
    assert snapshot.performance("qwen").latency_ms == pytest.approx(2_400)
    assert snapshot.performance("qwen").tokens_per_second == pytest.approx(12)
    assert snapshot.performance("qwen").updated_at > 0


def test_proxy_performance_ignores_errors_and_unusable_usage(tmp_path, monkeypatch):
    app, client, _ = _app(tmp_path)
    _managed_engine(client, host_id="host-perf", model_id="qwen")
    engine = app.state.nodes[engine_node_id("host-perf", "qwen")]
    monkeypatch.setattr(server_module.time, "monotonic", lambda: 105.0)

    server_module._record_engine_performance(
        engine,
        100.0,
        model="qwen",
        status_code=500,
        response=httpx.Response(500, json={"usage": {"completion_tokens": 100}}),
    )
    server_module._record_engine_performance(
        engine,
        100.0,
        model="qwen",
        status_code=200,
        response=httpx.Response(200, json={"usage": {"completion_tokens": True}}),
    )

    assert engine.proxy_performance_samples == 1
    assert engine.proxy_latency_ms == 5_000
    assert engine.proxy_tokens_per_second == 0


def test_proxy_performance_keeps_multi_model_measurements_isolated(
    tmp_path,
    monkeypatch,
):
    app, client, _ = _app(tmp_path)
    _managed_engine(client, host_id="host-per-model", model_id="qwen")
    engine = app.state.nodes[engine_node_id("host-per-model", "qwen")]
    engine.models.append("other")
    clock = iter((102.0, 110.0))
    monkeypatch.setattr(server_module.time, "monotonic", lambda: next(clock))

    server_module._record_engine_performance(
        engine,
        100.0,
        model="qwen",
        status_code=200,
        response=httpx.Response(200, json={"usage": {"completion_tokens": 20}}),
    )
    server_module._record_engine_performance(
        engine,
        100.0,
        model="other",
        status_code=200,
        response=httpx.Response(200, json={"usage": {"completion_tokens": 20}}),
    )

    assert engine.proxy_model_performance["qwen"].latency_ms == 2_000
    assert engine.proxy_model_performance["qwen"].tokens_per_second == 10
    assert engine.proxy_model_performance["other"].latency_ms == 10_000
    assert engine.proxy_model_performance["other"].tokens_per_second == 2
    snapshot = server_module._allocator_snapshots(app)[0]
    assert snapshot.performance("qwen").tokens_per_second == 10
    assert snapshot.performance("other").tokens_per_second == 2


def test_allocator_snapshot_normalizes_remote_wall_clocks_from_model_ages(tmp_path):
    app, _, _ = _app(tmp_path)
    received_at = time.time()
    app.state.nodes["skewed"] = server_module.Node(
        node_id="skewed",
        role="allocator",
        host_id="host-skewed",
        resources={"capacity_mb": 16_000, "runtimes": ["llama.cpp"]},
        allocator={
            "managed": True,
            "residencies": [
                {
                    "model_id": "qwen",
                    "memory_mb": 8_000,
                    "state": "ready",
                    # Deliberately contradictory remote wall-clock values. Only ages are evidence.
                    "loaded_at": received_at + 3_600,
                    "last_used_at": received_at - 3_600,
                    "loaded_age_seconds": 30,
                    "last_used_age_seconds": 10,
                }
            ],
        },
        last_heartbeat=received_at,
    )

    residency = server_module._allocator_snapshots(app)[0].residencies[0]
    assert residency.loaded_at == pytest.approx(received_at - 30)
    assert residency.last_used_at == pytest.approx(received_at - 10)


def test_missing_model_age_evidence_is_treated_as_fresh_not_old(tmp_path):
    app, _, _ = _app(tmp_path)
    received_at = time.time()
    app.state.nodes["legacy"] = server_module.Node(
        node_id="legacy",
        role="allocator",
        host_id="host-legacy",
        resources={"capacity_mb": 16_000, "runtimes": ["llama.cpp"]},
        allocator={
            "managed": True,
            "residencies": [
                {
                    "model_id": "qwen",
                    "memory_mb": 8_000,
                    "state": "ready",
                    "loaded_at": 1,
                    "last_used_at": 1,
                }
            ],
        },
        last_heartbeat=received_at,
    )

    residency = server_module._allocator_snapshots(app)[0].residencies[0]
    assert residency.loaded_at == pytest.approx(received_at)
    assert residency.last_used_at == pytest.approx(received_at)


def test_invalid_model_age_evidence_is_rejected_at_registration(tmp_path):
    _, client, _ = _app(tmp_path)
    response = client.put(
        f"/nodes/{engine_node_id('host-1', 'qwen')}",
        headers=_node_auth("host-1"),
        json={
            "role": "engine",
            "host_id": "host-1",
            "models": ["qwen"],
            "endpoint_url": "http://127.0.0.1:9000/v1",
            "allocator": {
                "managed": True,
                "residencies": [
                    {
                        "model_id": "qwen",
                        "memory_mb": 8_000,
                        "state": "ready",
                        "loaded_age_seconds": -1,
                        "last_used_age_seconds": 0,
                    }
                ],
            },
        },
    )
    assert response.status_code == 400
    assert "loaded_age_seconds" in response.text


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (400, False),
        (401, False),
        (404, False),
        (422, False),
        (429, True),
        (500, True),
        (503, True),
    ],
)
def test_only_retryable_upstream_statuses_create_allocator_error_pressure(
    status_code,
    expected,
):
    assert server_module._allocator_capacity_error(status_code) is expected


def test_allocator_snapshot_collection_purges_expired_registry_leases(tmp_path):
    app, _, _ = _app(tmp_path)
    app.state.nodes["stale"] = server_module.Node(
        node_id="stale",
        role="engine",
        models=["qwen"],
        endpoint_url="http://127.0.0.1:9000/v1",
        last_heartbeat=time.time() - server_module.NODE_TTL_SECONDS - 1,
    )

    assert server_module._allocator_snapshots(app) == ()
    assert "stale" not in app.state.nodes


def test_public_registry_has_a_hard_bound_after_stale_lease_gc(tmp_path, monkeypatch):
    _, client, _ = _app(tmp_path)
    monkeypatch.setattr(server_module, "MAX_REGISTRY_NODES", 1)
    payload = {
        "role": "engine",
        "models": ["qwen"],
        "endpoint_url": "http://127.0.0.1:9000/v1",
    }

    assert client.put("/nodes/one", json=payload).status_code == 200
    rejected = client.put("/nodes/two", json=payload)

    assert rejected.status_code == 503
    assert "capacity is exhausted" in rejected.json()["detail"]


def test_control_heartbeat_with_unprocessed_revision_never_receives_old_commands(
    tmp_path,
    monkeypatch,
):
    app, client, _ = _app(tmp_path)
    _managed_node(client)
    assert (
        client.put("/allocator/models/qwen", json=_profile(), headers=AUTH).status_code
        == 200
    )
    assert (
        client.put(
            "/allocator/mode", json={"mode": "automatic"}, headers=AUTH
        ).status_code
        == 200
    )
    assert app.state.allocator.status()["pending_commands"]

    async def never_processed(*_args, **_kwargs):
        return False

    monkeypatch.setattr(server_module, "_await_allocator_revision", never_processed)
    with TestClient(app) as running_client:
        response = running_client.post(
            "/nodes/heartbeat",
            headers=_node_auth("host-1"),
            json={"node_id": CONTROL_NODE_ID},
        )

    assert response.status_code == 200
    assert response.json()["allocator"]["commands"] == []


def test_identical_lease_churn_does_not_starve_destructive_delivery(
    tmp_path,
    monkeypatch,
):
    app, client, state_path = _app(tmp_path)
    drain = _queue_pinned_replacement_drain(app, client)

    async def process_poll_then_receive_identical_lease(_app, revision, **_kwargs):
        app.state.allocator_processed_revision = revision
        app.state.allocator_last_success_revision = revision
        app.state.allocator_last_success_safety_revision = (
            app.state.allocator_safety_revision
        )
        safety_revision = app.state.allocator_safety_revision
        for _ in range(64):
            server_module._mark_allocator_dirty(app, safety_changed=False)
        assert app.state.allocator_safety_revision == safety_revision
        return True

    monkeypatch.setattr(
        server_module,
        "_await_allocator_revision",
        process_poll_then_receive_identical_lease,
    )
    app.state.allocator_running = True
    response = client.post(
        "/nodes/heartbeat",
        headers=_node_auth("host-old"),
        json={
            "node_id": control_node_id("host-old"),
            "request_commands": True,
        },
    )

    assert response.status_code == 200
    assert [item["action_id"] for item in response.json()["allocator"]["commands"]] == [
        drain["action_id"]
    ]
    assert app.state.allocator_dirty_revision > app.state.allocator_processed_revision
    assert (
        drain["action_id"]
        in json.loads(state_path.read_text())["delivered_command_ids"]
    )


def test_destructive_delivery_requires_exact_safety_revision_match(
    tmp_path,
    monkeypatch,
):
    app, client, state_path = _app(tmp_path)
    drain = _queue_pinned_replacement_drain(app, client)

    async def process_poll_with_invalid_future_safety(_app, revision, **_kwargs):
        app.state.allocator_processed_revision = revision
        app.state.allocator_last_success_revision = revision
        app.state.allocator_last_success_safety_revision = (
            app.state.allocator_safety_revision + 1
        )
        return True

    monkeypatch.setattr(
        server_module,
        "_await_allocator_revision",
        process_poll_with_invalid_future_safety,
    )
    app.state.allocator_running = True
    response = client.post(
        "/nodes/heartbeat",
        headers=_node_auth("host-old"),
        json={
            "node_id": control_node_id("host-old"),
            "request_commands": True,
        },
    )

    assert response.status_code == 200
    assert response.json()["allocator"]["commands"] == []
    assert any(
        item["action_id"] == drain["action_id"]
        for item in app.state.allocator.status()["pending_commands"]
    )
    assert (
        drain["action_id"]
        not in json.loads(state_path.read_text())["delivered_command_ids"]
    )


def test_semantic_change_during_tick_withholds_destructive_delivery(
    tmp_path,
    monkeypatch,
):
    app, client, state_path = _app(tmp_path)
    drain = _queue_pinned_replacement_drain(app, client)
    replacement = app.state.nodes[engine_node_id("host-new", "qwen")]

    async def process_poll_then_lose_replacement(_app, revision, **_kwargs):
        app.state.allocator_processed_revision = revision
        app.state.allocator_last_success_revision = revision
        app.state.allocator_last_success_safety_revision = (
            app.state.allocator_safety_revision
        )
        replacement.models = []
        replacement.allocator["state"] = "unhealthy"
        replacement.allocator["residencies"][0]["state"] = "failed"
        server_module._mark_allocator_dirty(app, safety_changed=True)
        return True

    monkeypatch.setattr(
        server_module,
        "_await_allocator_revision",
        process_poll_then_lose_replacement,
    )
    app.state.allocator_running = True
    response = client.post(
        "/nodes/heartbeat",
        headers=_node_auth("host-old"),
        json={
            "node_id": control_node_id("host-old"),
            "request_commands": True,
        },
    )

    assert response.status_code == 200
    assert response.json()["allocator"]["commands"] == []
    assert any(
        item["action_id"] == drain["action_id"]
        for item in app.state.allocator.status()["pending_commands"]
    )
    assert (
        drain["action_id"]
        not in json.loads(state_path.read_text())["delivered_command_ids"]
    )


def test_near_expiry_ready_route_cannot_authorize_drain(tmp_path):
    app, client, _ = _app(tmp_path)
    _managed_node(client, host_id="host-old")
    _managed_engine(client, host_id="host-old", model_id="qwen")
    _managed_node(client, host_id="host-new")
    _managed_engine(client, host_id="host-new", model_id="qwen")
    replacement = app.state.nodes[engine_node_id("host-new", "qwen")]
    replacement.last_heartbeat = time.time() - (
        server_module.NODE_TTL_SECONDS
        - server_module.ALLOCATOR_DESTRUCTIVE_LEASE_MARGIN_SECONDS
        + 0.1
    )

    assert (
        client.put(
            "/allocator/models/qwen",
            headers=AUTH,
            json=_profile(pinned_nodes=["host-new"]),
        ).status_code
        == 200
    )
    assert (
        client.put(
            "/allocator/mode",
            headers=AUTH,
            json={"mode": "automatic"},
        ).status_code
        == 200
    )
    status = app.state.allocator.status(server_module._allocator_snapshots(app))

    new_host = next(item for item in status["nodes"] if item["node_id"] == "host-new")
    assert new_host["residencies"][0]["state"] == "warming"
    assert not any(item["kind"] == "drain" for item in status["pending_commands"])
    assert any(
        item["node_id"] == "host-old" and item["code"] == "replacement_not_ready"
        for item in status["reconciliation"]["deferred"]
    )


def test_replacement_expiry_during_tick_cannot_deliver_or_mark_drain(
    tmp_path,
    monkeypatch,
):
    app, client, state_path = _app(tmp_path)
    drain = _queue_pinned_replacement_drain(app, client)
    clock = [time.time()]
    replacement_ids = (
        control_node_id("host-new"),
        engine_node_id("host-new", "qwen"),
    )
    for node_id in replacement_ids:
        app.state.nodes[node_id].last_heartbeat = clock[0] - 49.0
    monkeypatch.setattr(server_module.time, "time", lambda: clock[0])

    async def process_poll_then_expire_replacement(_app, revision, **_kwargs):
        app.state.allocator_processed_revision = revision
        app.state.allocator_last_success_revision = revision
        app.state.allocator_last_success_safety_revision = (
            app.state.allocator_safety_revision
        )
        clock[0] += 12.0
        return True

    monkeypatch.setattr(
        server_module,
        "_await_allocator_revision",
        process_poll_then_expire_replacement,
    )
    app.state.allocator_running = True
    response = client.post(
        "/nodes/heartbeat",
        headers=_node_auth("host-old"),
        json={
            "node_id": control_node_id("host-old"),
            "request_commands": True,
        },
    )

    assert response.status_code == 200
    assert response.json()["allocator"]["commands"] == []
    assert any(
        item["action_id"] == drain["action_id"]
        for item in app.state.allocator.status()["pending_commands"]
    )
    assert (
        drain["action_id"]
        not in json.loads(state_path.read_text())["delivered_command_ids"]
    )
    active = server_module._active_engines(app, "qwen")
    assert [server_module._node_host_id(node) for node in active] == ["host-old"]


def test_slow_delivery_marker_save_revalidates_fresh_leases_before_drain(
    tmp_path,
    monkeypatch,
):
    app, client, state_path = _app(tmp_path)
    drain = _queue_pinned_replacement_drain(app, client)
    clock = [time.time()]
    for node_id in (
        control_node_id("host-new"),
        engine_node_id("host-new", "qwen"),
    ):
        app.state.nodes[node_id].last_heartbeat = clock[0] - (
            server_module.NODE_TTL_SECONDS
            - server_module.ALLOCATOR_DESTRUCTIVE_LEASE_MARGIN_SECONDS
            - 1.0
        )
    monkeypatch.setattr(server_module.time, "time", lambda: clock[0])

    async def process_poll(_app, revision, **_kwargs):
        app.state.allocator_processed_revision = revision
        app.state.allocator_last_success_revision = revision
        app.state.allocator_last_success_safety_revision = (
            app.state.allocator_safety_revision
        )
        return True

    original_save = app.state.allocator._save
    delivery_save_seen = False

    def slow_delivery_save():
        nonlocal delivery_save_seen
        delivered = app.state.allocator._delivered_command_ids
        if drain["action_id"] in delivered and not delivery_save_seen:
            delivery_save_seen = True
            clock[0] += 2.0
        original_save()

    monkeypatch.setattr(server_module, "_await_allocator_revision", process_poll)
    monkeypatch.setattr(app.state.allocator, "_save", slow_delivery_save)
    app.state.allocator_running = True
    response = client.post(
        "/nodes/heartbeat",
        headers=_node_auth("host-old"),
        json={"node_id": control_node_id("host-old"), "request_commands": True},
    )

    assert response.status_code == 200
    assert response.json()["allocator"]["commands"] == []
    assert delivery_save_seen
    assert (
        drain["action_id"]
        not in json.loads(state_path.read_text())["delivered_command_ids"]
    )


def test_failed_unsafe_marker_compensation_retains_conservative_uncertainty(
    tmp_path,
    monkeypatch,
):
    app, client, state_path = _app(tmp_path)
    drain = _queue_pinned_replacement_drain(app, client)
    clock = [time.time()]
    for node_id in (
        control_node_id("host-new"),
        engine_node_id("host-new", "qwen"),
    ):
        app.state.nodes[node_id].last_heartbeat = clock[0] - (
            server_module.NODE_TTL_SECONDS
            - server_module.ALLOCATOR_DESTRUCTIVE_LEASE_MARGIN_SECONDS
            - 1.0
        )
    monkeypatch.setattr(server_module.time, "time", lambda: clock[0])

    async def process_poll(_app, revision, **_kwargs):
        app.state.allocator_processed_revision = revision
        app.state.allocator_last_success_revision = revision
        app.state.allocator_last_success_safety_revision = (
            app.state.allocator_safety_revision
        )
        return True

    original_save = app.state.allocator._save
    delivery_save_seen = False

    def fail_compensating_save():
        nonlocal delivery_save_seen
        delivered = app.state.allocator._delivered_command_ids
        if drain["action_id"] in delivered and not delivery_save_seen:
            delivery_save_seen = True
            clock[0] += 2.0
            original_save()
            return
        if delivery_save_seen and drain["action_id"] not in delivered:
            raise OSError("injected compensation failure")
        original_save()

    monkeypatch.setattr(server_module, "_await_allocator_revision", process_poll)
    monkeypatch.setattr(app.state.allocator, "_save", fail_compensating_save)
    app.state.allocator_running = True
    response = client.post(
        "/nodes/heartbeat",
        headers=_node_auth("host-old"),
        json={"node_id": control_node_id("host-old"), "request_commands": True},
    )

    assert response.status_code == 200
    assert response.json()["allocator"]["commands"] == []
    assert (
        drain["action_id"]
        in json.loads(state_path.read_text())["delivered_command_ids"]
    )
    status = app.state.allocator.status()
    assert drain["action_id"] in status["delivered_pending_action_ids"]
    assert (
        "could not clear prepared destructive delivery markers"
        in status["last_delivery_safety_error"]
    )


def test_volatile_available_memory_churn_does_not_starve_destructive_delivery(
    tmp_path,
    monkeypatch,
):
    app, client, state_path = _app(tmp_path)
    drain = _queue_pinned_replacement_drain(app, client)
    _managed_node(client, host_id="host-noise")
    safety_revision = app.state.allocator_safety_revision
    dirty_revision = app.state.allocator_dirty_revision

    for index in range(32):
        response = client.post(
            "/nodes/heartbeat",
            headers=_node_auth("host-noise"),
            json={
                "node_id": control_node_id("host-noise"),
                "resources": {
                    "capacity_mb": 16_000,
                    "runtimes": ["llama.cpp"],
                    "available_mb": 8_000 + index,
                },
                "request_commands": False,
            },
        )
        assert response.status_code == 200

    assert app.state.allocator_dirty_revision == dirty_revision + 32
    assert app.state.allocator_safety_revision == safety_revision

    async def process_poll(_app, revision, **_kwargs):
        app.state.allocator_processed_revision = revision
        app.state.allocator_last_success_revision = revision
        app.state.allocator_last_success_safety_revision = (
            app.state.allocator_safety_revision
        )
        return True

    monkeypatch.setattr(server_module, "_await_allocator_revision", process_poll)
    app.state.allocator_running = True
    response = client.post(
        "/nodes/heartbeat",
        headers=_node_auth("host-old"),
        json={"node_id": control_node_id("host-old"), "request_commands": True},
    )

    assert response.status_code == 200
    assert [item["action_id"] for item in response.json()["allocator"]["commands"]] == [
        drain["action_id"]
    ]
    assert (
        drain["action_id"]
        in json.loads(state_path.read_text())["delivered_command_ids"]
    )


def test_lease_heartbeat_does_not_wait_poll_or_mark_command_delivered(
    tmp_path,
    monkeypatch,
):
    app, client, state_path = _app(tmp_path)
    _managed_node(client)
    assert (
        client.put("/allocator/models/qwen", json=_profile(), headers=AUTH).status_code
        == 200
    )
    assert (
        client.put(
            "/allocator/mode", json={"mode": "automatic"}, headers=AUTH
        ).status_code
        == 200
    )
    pending = app.state.allocator.status()["pending_commands"]
    assert len(pending) == 1
    action_id = pending[0]["action_id"]

    wait_calls = 0

    async def mark_revision_processed(_app, revision, **_kwargs):
        nonlocal wait_calls
        wait_calls += 1
        app.state.allocator_processed_revision = revision
        app.state.allocator_last_success_revision = revision
        app.state.allocator_last_success_safety_revision = (
            app.state.allocator_safety_revision
        )
        return True

    poll_calls = 0
    original_commands_for = app.state.allocator.commands_for

    def count_command_poll(*args, **kwargs):
        nonlocal poll_calls
        poll_calls += 1
        return original_commands_for(*args, **kwargs)

    monkeypatch.setattr(
        server_module, "_await_allocator_revision", mark_revision_processed
    )
    monkeypatch.setattr(app.state.allocator, "commands_for", count_command_poll)
    with TestClient(app) as running_client:
        lease = running_client.post(
            "/nodes/heartbeat",
            headers=_node_auth("host-1"),
            json={"node_id": CONTROL_NODE_ID, "request_commands": False},
        )
        assert lease.status_code == 200
        assert lease.json()["allocator"]["commands"] == []
        assert wait_calls == 0
        assert poll_calls == 0
        assert (
            action_id not in json.loads(state_path.read_text())["delivered_command_ids"]
        )

        control = running_client.post(
            "/nodes/heartbeat",
            headers=_node_auth("host-1"),
            json={"node_id": CONTROL_NODE_ID, "request_commands": True},
        )

    assert control.status_code == 200
    assert [item["action_id"] for item in control.json()["allocator"]["commands"]] == [
        action_id
    ]
    assert wait_calls == 1
    assert poll_calls == 1
    assert action_id in json.loads(state_path.read_text())["delivered_command_ids"]


def test_grid_config_mints_and_upgrades_a_persisted_allocator_token(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    cfg = runtime.init_grid_config(name="home", port=8090)
    token = cfg["allocator_control_token"]
    assert isinstance(token, str) and len(token) >= 32
    assert config.load_grid_config(cfg["grid_id"])["allocator_control_token"] == token

    cfg.pop("allocator_control_token")
    config.save_grid_config(cfg["grid_id"], cfg)
    monkeypatch.setattr(runtime.secrets, "token_urlsafe", lambda size: "upgraded-token")
    assert runtime.ensure_allocator_control_token(cfg) == "upgraded-token"
    assert runtime.ensure_allocator_control_token(cfg) == "upgraded-token"
    assert (
        config.load_grid_config(cfg["grid_id"])["allocator_control_token"]
        == "upgraded-token"
    )


def test_internal_server_wires_private_token_and_grid_scoped_state(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    cfg = runtime.init_grid_config(name="home", port=8090)
    captured = {}

    def fake_create_app(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("local.server.create_app", fake_create_app)
    monkeypatch.setattr(
        "uvicorn.run", lambda app, **kwargs: captured.update(uvicorn_app=app)
    )

    assert cli_main.cmd_internal_server(cfg["grid_id"]) == 0
    assert captured["grid_id"] == cfg["grid_id"]
    assert captured["allocator_control_token"] == cfg["allocator_control_token"]
    assert captured["allocator_state_path"] == (
        runtime.paths.grid_dir(cfg["grid_id"]) / runtime.ALLOCATOR_STATE_FILE
    )
