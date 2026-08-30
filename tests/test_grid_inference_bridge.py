"""Focused checks for the physical Goal lab's Grid-to-Grid inference bridge."""
from __future__ import annotations

import importlib.util
from pathlib import Path

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
