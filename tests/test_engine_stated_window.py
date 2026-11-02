"""An engine joined with `--at` and no `--ctx-size` advertises the window its own `/models` states.

vLLM's entries say `max_model_len` (measured: 262144). Without a window the relay never compacts for the engine
(`request_body.compaction_need`), so a long conversation goes out whole and the engine refuses it. The value is remote
input, and an engine that states nothing must stay exactly as unknown as it was.
"""
from __future__ import annotations

import http.server
import json
import threading

import httpx
import pytest

from remote import probe, serve

NO_FEATURES = {"vision": False, "json_object": False, "json_schema": False, "tools": False,
               "parallel_tool_calls": False}
URL = "http://engine.example/v1"


def _listing(*entries: dict) -> httpx.Response:
    return httpx.Response(200, json={"object": "list", "data": list(entries)})


def _answering(monkeypatch, response: httpx.Response | None) -> list[str]:
    """`probe._get` answers ``response``; returns the URLs it was asked for."""
    asked: list[str] = []

    def get(url, *, timeout):
        asked.append(url)
        return response

    monkeypatch.setattr(probe, "_get", get)
    return asked


def _stub_the_other_probes(monkeypatch) -> None:
    monkeypatch.setattr(probe, "probe_responses_endpoint", lambda *a, **k: False)
    monkeypatch.setattr(probe, "probe_systemone", lambda *a, **k: False)
    monkeypatch.setattr(probe, "probe_llama_capabilities", lambda url, model: dict(NO_FEATURES))


class TestStatedContextWindows:
    def test_vllm_states_its_window_per_model(self, monkeypatch):
        asked = _answering(monkeypatch, _listing({"id": "a", "max_model_len": 262144},
                                                 {"id": "b", "max_model_len": 32768}))

        assert probe.stated_context_windows(URL) == {"a": 262144, "b": 32768}
        assert asked == [f"{URL}/models"]

    @pytest.mark.parametrize("stated", ["262144", 262144.0, True, False, 0, -1, None, [262144], {"n": 1}],
                             ids=repr)
    def test_a_value_that_is_not_a_positive_int_is_unknown(self, monkeypatch, stated):
        _answering(monkeypatch, _listing({"id": "bad", "max_model_len": stated}, {"id": "good", "max_model_len": 8192}))

        assert probe.stated_context_windows(URL) == {"good": 8192}  # the positive control sits in the same answer

    def test_an_entry_that_states_nothing_is_unknown(self, monkeypatch):
        _answering(monkeypatch, _listing({"id": "ollama-style"}))

        assert probe.stated_context_windows(URL) == {}

    @pytest.mark.parametrize("entry", ["text", 7, None, {"max_model_len": 8192}, {"id": 7, "max_model_len": 8192},
                                       {"id": ["a"], "max_model_len": 8192}], ids=repr)
    def test_an_entry_without_a_name_is_skipped(self, monkeypatch, entry):
        _answering(monkeypatch, _listing(entry, {"id": "good", "max_model_len": 8192}))

        assert probe.stated_context_windows(URL) == {"good": 8192}

    @pytest.mark.parametrize("response", [
        None,
        httpx.Response(404, json={"data": [{"id": "a", "max_model_len": 8192}]}),
        httpx.Response(200, content=b"<html>not json</html>"),
        httpx.Response(200, content=b"\xff\xfe\xff"),
        httpx.Response(200, content=b"[" * 200_000),
        httpx.Response(200, content=b"1" * 5000),
        httpx.Response(200, json=[{"id": "a", "max_model_len": 8192}]),
        httpx.Response(200, json={"data": {"id": "a", "max_model_len": 8192}}),
        httpx.Response(200, json={"data": None}),
        httpx.Response(200, json={}),
    ], ids=["transport-failure", "404", "html", "bad-bytes", "deep-nesting", "huge-number", "list-body", "data-dict",
            "data-null", "no-data"])
    def test_an_answer_of_the_wrong_shape_is_unknown_and_never_raises(self, monkeypatch, response):
        _answering(monkeypatch, response)

        assert probe.stated_context_windows(URL) == {}

    def test_over_real_http(self):
        body = json.dumps({"data": [{"id": "m", "max_model_len": 262144}]}).encode()

        class Engine(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200 if self.path == "/v1/models" else 404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Engine)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            assert probe.stated_context_windows(f"http://127.0.0.1:{server.server_port}/v1") == {"m": 262144}
        finally:
            server.shutdown()
            server.server_close()


class TestTheSpecAdvertisesIt:
    def test_an_engine_with_no_ctx_size_advertises_what_it_states(self, monkeypatch):
        _stub_the_other_probes(monkeypatch)
        _answering(monkeypatch, _listing({"id": "m", "max_model_len": 262144}))

        entry = serve._probe_spec_caps(URL, ["m"], ["m"], None)["models"]["m"]

        assert entry["context_window"] == 262144
        assert entry["limits"]["max_context_tokens"] == 262144

    def test_ctx_size_wins_and_the_engine_is_not_asked(self, monkeypatch):
        _stub_the_other_probes(monkeypatch)
        asked = _answering(monkeypatch, _listing({"id": "m", "max_model_len": 262144}))

        entry = serve._probe_spec_caps(URL, ["m"], ["m"], 65536)["models"]["m"]

        assert entry["context_window"] == 65536
        assert asked == []

    def test_an_engine_that_states_nothing_stays_unknown(self, monkeypatch):
        """Omitted, never defaulted: a stated window is what lets the router filter an engine out."""
        _stub_the_other_probes(monkeypatch)
        _answering(monkeypatch, _listing({"id": "m"}))

        entry = serve._probe_spec_caps(URL, ["m"], ["m"], None)["models"]["m"]

        assert "context_window" not in entry
        assert "max_context_tokens" not in entry["limits"]

    def test_the_window_is_read_by_the_upstream_name_and_advertised_under_the_alias(self, monkeypatch):
        _stub_the_other_probes(monkeypatch)
        _answering(monkeypatch, _listing({"id": "real", "max_model_len": 4096}, {"id": "pub", "max_model_len": 9999}))

        models = serve._probe_spec_caps(URL, ["pub"], ["real"], None)["models"]

        assert models["pub"]["context_window"] == 4096

    def test_each_model_of_one_engine_gets_its_own_window_from_one_read(self, monkeypatch):
        _stub_the_other_probes(monkeypatch)
        asked = _answering(monkeypatch, _listing({"id": "a", "max_model_len": 1024}, {"id": "b", "max_model_len": 2048}))

        models = serve._probe_spec_caps(URL, ["a", "b", "c"], ["a", "b", "c"], None)["models"]

        assert models["a"]["context_window"] == 1024
        assert models["b"]["context_window"] == 2048
        assert "context_window" not in models["c"]
        assert asked == [f"{URL}/models"]

    def test_an_api_engine_never_asks_its_vendor(self, monkeypatch):
        asked = _answering(monkeypatch, _listing({"id": "gpt-5.5", "max_model_len": 1}))

        serve._probe_spec_caps("https://api.openai.com/v1", ["openai:gpt-5.5"], ["gpt-5.5"], None, api_kind="openai")

        assert asked == []
