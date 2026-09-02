from __future__ import annotations

from pathlib import Path

from remote.allocator_routes import RemoteProviderRoutePublisher
from shared import jsonio, run_records
from shared.allocator.models import ResidencyState
from shared.allocator.runtime import ManagedResidency, RuntimeHandle


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Client:
    def __init__(self, node):
        self.node = node

    def get(self, _url):
        return _Response({"nodes": [self.node]})


def _ready(model: str, port: int, runtime: str = "llama.cpp") -> ManagedResidency:
    return ManagedResidency(
        model_id=model,
        memory_mb=256,
        state=ResidencyState.READY,
        handle=RuntimeHandle(pid=999, port=port, runtime=runtime),
        runtime=runtime,
    )


def _seed(monkeypatch, tmp_path: Path, *, engines):
    record_path = tmp_path / "remote.json"
    record = {
        "engine_id": "remote",
        "grid_id": "grid-forge",
        "pid": 123,
        "reload_signal": "sighup",
        "signaling_url": "https://forge.example",
        "meta_name": "forge-node",
        "models": [model for spec in engines for model in spec.get("models", [])],
        "engines": engines,
    }
    jsonio.atomic_write_json(record_path, record)
    monkeypatch.setattr(run_records, "record_path", lambda *_args: record_path)
    monkeypatch.setattr(run_records, "record_alive", lambda _record: True)
    monkeypatch.setattr(run_records, "recorded_pid", lambda _record: 123)
    monkeypatch.setattr(run_records, "write_record", lambda _g, _e, value: jsonio.atomic_write_json(record_path, value))
    signals = []
    monkeypatch.setattr("remote.allocator_routes.os.kill", lambda pid, sig: signals.append((pid, sig)))
    return record_path, signals


def test_remote_publisher_preserves_user_engine_and_adds_managed_route(monkeypatch, tmp_path):
    key_file = tmp_path / "engine.key"
    key_file.write_text("allocator-engine-secret-123456\n")
    key_file.chmod(0o600)
    user = {
        "endpoint_url": "http://127.0.0.1:8000/v1",
        "models": ["qwen"],
        "engine_label": "vLLM",
    }
    path, signals = _seed(monkeypatch, tmp_path, engines=[user])
    client = _Client(
        {
            "name": "forge-node",
            "models": ["qwen", "smollm2-135m-instruct-q3_k_m"],
        }
    )
    publisher = RemoteProviderRoutePublisher(
        "grid-forge", "host-c", engine_api_key_file=key_file, client=client
    )

    assert publisher.sync([_ready("SmolLM2-135M-Instruct-Q3_K_M.gguf", 18081)]) == ()

    stored = jsonio.load_json(path)
    assert stored["engines"][0] == user
    assert stored["engines"][1]["allocator_host_id"] == "host-c"
    assert stored["engines"][1]["endpoint_url"] == "http://127.0.0.1:18081/v1"
    assert stored["engines"][1]["allocator_api_key_file"] == str(key_file.resolve())
    assert stored["models"] == ["qwen", "SmolLM2-135M-Instruct-Q3_K_M.gguf"]
    assert len(signals) == 1


def test_remote_publisher_fence_removes_only_its_managed_routes(monkeypatch, tmp_path):
    user = {"endpoint_url": "http://127.0.0.1:8000/v1", "models": ["qwen"]}
    owned = {
        "endpoint_url": "http://127.0.0.1:18081/v1",
        "models": ["smollm"],
        "allocator_host_id": "host-c",
    }
    other = {
        "endpoint_url": "http://127.0.0.1:18082/v1",
        "models": ["other"],
        "allocator_host_id": "host-other",
    }
    path, _ = _seed(monkeypatch, tmp_path, engines=[user, owned, other])
    client = _Client({"name": "forge-node", "models": ["qwen", "other"]})
    publisher = RemoteProviderRoutePublisher(
        "grid-forge", "host-c", client=client
    )

    assert publisher.fence() == ()

    stored = jsonio.load_json(path)
    assert stored["engines"] == [user, other]
    assert stored["models"] == ["qwen", "other"]


def test_remote_publisher_refuses_managed_route_without_engine_credential(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path, engines=[])
    publisher = RemoteProviderRoutePublisher(
        "grid-forge",
        "host-c",
        client=_Client({"name": "forge-node", "models": []}),
    )

    try:
        publisher.sync([_ready("smollm.gguf", 18081)])
    except RuntimeError as exc:
        assert "API key file" in str(exc)
    else:
        raise AssertionError("managed route was published without its engine credential")


def test_remote_publisher_adds_and_removes_managed_media_without_clobbering_user_media(
    monkeypatch, tmp_path
):
    key_file = tmp_path / "engine.key"
    key_file.write_text("allocator-engine-secret-123456\n")
    user = {"endpoint_url": "http://127.0.0.1:8000/v1", "models": ["qwen"]}
    path, _ = _seed(monkeypatch, tmp_path, engines=[user])
    record = jsonio.load_json(path)
    record["media"] = True
    record["media_bundles"] = ["z_image"]
    jsonio.atomic_write_json(path, record)
    client = _Client(
        {
            "name": "forge-node",
            "models": ["qwen", "comfyui:z_image", "comfyui:image_generation"],
        }
    )
    publisher = RemoteProviderRoutePublisher(
        "grid-forge", "host-c", engine_api_key_file=key_file, client=client
    )

    assert publisher.sync(
        [_ready("comfyui:image_generation", 8188, "comfyui")]
    ) == ()
    stored = jsonio.load_json(path)
    assert stored["engines"] == [user]
    assert stored["media"] is True
    assert stored["media_bundles"] == ["z_image", "image_generation"]
    assert stored["allocator_media_models"] == ["comfyui:image_generation"]

    client.node["models"] = ["qwen", "comfyui:z_image"]
    assert publisher.fence() == ()
    restored = jsonio.load_json(path)
    assert restored["media"] is True
    assert restored["media_bundles"] == ["z_image"]
    assert "allocator_media_host_id" not in restored
