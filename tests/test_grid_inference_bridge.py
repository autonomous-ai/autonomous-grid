"""Focused checks for the physical Goal lab's Grid-to-Grid inference bridge."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest


_SOURCE = Path(__file__).parent / "e2e_cross_repo" / "grid_inference_bridge.py"
_SPEC = importlib.util.spec_from_file_location("grid_inference_bridge", _SOURCE)
assert _SPEC and _SPEC.loader
bridge = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bridge)


def _response(models):
    return httpx.Response(
        200, request=httpx.Request("GET", "https://grid.test/relay/v1/models"),
        json={"data": [{"id": model} for model in models]})


def test_model_ids_accepts_only_string_ids():
    assert bridge._model_ids({"data": [
        {"id": "Exact-Model"}, {"id": 3}, {}, "bad",
    ]}) == ["Exact-Model"]


def test_require_exact_model_refuses_case_mismatch_with_correction(monkeypatch):
    monkeypatch.setattr(bridge.httpx, "get", lambda *_args, **_kwargs: _response(["Qwen-Exact"]))
    source = bridge.SourceGrid("https://grid.test/relay/v1", "secret")

    with pytest.raises(SystemExit, match="case-sensitive.*Qwen-Exact"):
        bridge.require_exact_model(source, "qwen-exact")


def test_require_exact_model_refuses_absent_model(monkeypatch):
    monkeypatch.setattr(bridge.httpx, "get", lambda *_args, **_kwargs: _response(["Model-A"]))
    source = bridge.SourceGrid("https://grid.test/relay/v1", "secret")

    with pytest.raises(SystemExit, match="does not serve.*Available: Model-A"):
        bridge.require_exact_model(source, "Model-B")


def test_bridge_is_loopback_and_exposes_only_needed_routes():
    source = bridge.SourceGrid("https://grid.test/relay/v1", "secret")
    server = bridge.Bridge(source)
    try:
        host, port = server.server.server_address
        assert host == "127.0.0.1"
        assert server.base_url == f"http://127.0.0.1:{port}/v1"
        assert bridge._FORWARDED == {
            "/responses", "/chat/completions", "/completions", "/models"}
    finally:
        server.server.server_close()


def test_bridge_replaces_stale_provider_and_unregisters_on_normal_exit(tmp_path, monkeypatch):
    target_home = tmp_path / "target-home"
    target_home.mkdir()
    (target_home / "credentials.toml").write_text("paired", encoding="utf-8")
    calls = []
    stopped = []

    class FakeBridge:
        base_url = "http://127.0.0.1:54321/v1"

        def __init__(self, _source, _port):
            pass

        def start(self):
            pass

        def stop(self):
            stopped.append(True)

    monkeypatch.setattr(bridge, "_source_grid", lambda _name: bridge.SourceGrid(
        "https://source.test/relay/v1", "secret"))
    monkeypatch.setattr(bridge, "require_exact_model", lambda _source, model: model)
    monkeypatch.setattr(bridge, "Bridge", FakeBridge)
    monkeypatch.setattr(bridge.threading, "Event", lambda: SimpleNamespace(wait=lambda: None))
    import cli
    monkeypatch.setattr(cli, "main", lambda argv: calls.append(argv) or 0)

    result = bridge.run(SimpleNamespace(
        source_grid="source", target_grid="goal-physical", target_home=str(target_home),
        model="Exact-Model", name="relay-host", tasks_root=str(tmp_path / "tasks"),
        max_tasks=1, port=0))

    assert result == 0
    assert calls[0] == ["leave", "goal-physical"]
    assert calls[1][:2] == ["join", "goal-physical"]
    assert calls[2] == ["leave", "goal-physical"]
    assert stopped == [True]
