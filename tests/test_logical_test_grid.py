from __future__ import annotations

from fastapi.testclient import TestClient

from local.logical_test_grid import _profile, install_demand_simulation_routes
from shared.allocator.models import ModelProfile
from local.server import create_app


def test_demand_simulation_routes_are_authenticated_and_clearable(tmp_path):
    app = create_app(
        grid_id="logical-test",
        grid_name="logical-test",
        allocator_state_path=tmp_path / "controller.json",
        allocator_control_token="control",
        allocator_interval_seconds=3_600,
    )
    app.state.allocator.put_profile(_profile("tiny.gguf", 4, ""))
    install_demand_simulation_routes(app, model="tiny.gguf", control_token="control")

    with TestClient(app) as client:
        assert client.post("/test/demand", json={"requests": 3}).status_code == 403
        response = client.post(
            "/test/demand",
            headers={"X-Grid-Allocator-Token": "control"},
            json={"requests": 3, "service_seconds": 2},
        )
        assert response.status_code == 200
        assert response.json()["accepted"] == 3
        assert response.json()["forecast"]["sample_count"] == 3

        response = client.delete(
            "/test/demand",
            headers={"X-Grid-Allocator-Token": "control"},
        )
        assert response.status_code == 200
        assert app.state.allocator.demand.forecast("tiny.gguf").sample_count == 0


def test_demand_simulation_route_rejects_invalid_burst(tmp_path):
    app = create_app(
        grid_id="logical-test",
        grid_name="logical-test",
        allocator_state_path=tmp_path / "controller.json",
        allocator_control_token="control",
        allocator_interval_seconds=3_600,
    )
    app.state.allocator.put_profile(_profile("tiny.gguf", 2, ""))
    install_demand_simulation_routes(app, model="tiny.gguf", control_token="control")

    with TestClient(app) as client:
        response = client.post(
            "/test/demand",
            headers={"X-Grid-Allocator-Token": "control"},
            json={"requests": 0},
        )
    assert response.status_code == 400


def test_exchange_replay_is_classified_and_projected_without_router_input(tmp_path):
    app = create_app(
        grid_id="logical-test",
        grid_name="logical-test",
        allocator_state_path=tmp_path / "controller.json",
        allocator_control_token="control",
        allocator_interval_seconds=3_600,
    )
    app.state.allocator.put_profile(
        ModelProfile(
            model_id="coder.gguf",
            memory_mb=256,
            min_replicas=0,
            max_replicas=4,
            workload_scores=(("coding", 1.0),),
        )
    )
    install_demand_simulation_routes(app, model="baseline.gguf", control_token="control")

    with TestClient(app) as client:
        response = client.post(
            "/test/exchanges",
            headers={"X-Grid-Allocator-Token": "control"},
            json={
                "requests": 3,
                "request": {
                    "model": "auto",
                    "messages": [
                        {"role": "user", "content": "Debug this Python API and add tests"}
                    ],
                },
                "service_seconds": 4,
                "output_units": 100,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["features"]["workload"] == "coding"
    assert payload["portfolio"][0]["chosen_model"] == "coder.gguf"
    assert app.state.allocator.demand.to_dict()["models"] == {}
    assert set(app.state.allocator.intelligence.unbound_demand.to_dict()["models"]) == {
        "coding"
    }
