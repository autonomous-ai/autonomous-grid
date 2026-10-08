"""System One decision models on a remote engine: probed, advertised, routed and forwarded.

A decision model (Laya on llama.cpp, Nimble on Ollama) answers TypeSafe's System One questions at
`/v1/systemone` instead of chatting. The relay already serves that endpoint; these pin the node's
half — that it notices such a model, advertises exactly what it serves, answers Jev's default model
names with it, and forwards its jobs whole. Every HTTP call goes through `httpx.MockTransport`.
"""
from __future__ import annotations

import json
import struct

import httpx
import pytest

from remote import probe, relay, serve
from shared import paths
from shared.engine import launcher
from shared.models import catalog, gguf

DECISION = {
    "model": "Laya-Q8_0.gguf",
    "answers": {
        "refund": {"type": "noul", "noul": 0.93},
        "team": {"type": "choice", "choice": "billing", "probabilities": {"billing": 0.8, "technical": 0.2},
                 "confidence": 0.3},
        "urgency": {"type": "score", "score": 0.6, "legend": {"0": "not urgent", "1": "urgent"},
                    "probabilities": {"0": 0.4, "1": 0.6}, "confidence": 0.03},
    },
    "usage": {"input_tokens": 120, "output_tokens": 0},
}
NO_FEATURES = dict.fromkeys(probe.PROBED_FEATURES, False)


def _mock_http(monkeypatch, handler, _real=httpx.Client):
    """Every `httpx.Client` answers through ``handler``; returns the requests it saw."""
    seen: list[httpx.Request] = []

    def wrapped(request):
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(
        httpx, "Client", lambda *a, **k: _real(*a, **{**k, "transport": httpx.MockTransport(wrapped)}),
    )
    return seen


# --- probe_systemone / probe_chats -------------------------------------------------------------


def test_a_decision_answer_passes_the_probe(monkeypatch):
    seen = _mock_http(monkeypatch, lambda request: httpx.Response(200, json=DECISION))

    assert probe.probe_systemone("http://engine.example/v1", "Laya-Q8_0.gguf") is True
    (request,) = seen
    assert str(request.url) == "http://engine.example/v1/systemone"
    body = json.loads(request.content)
    assert body["model"] == "Laya-Q8_0.gguf"
    assert {q["type"] for q in body["questions"].values()} == {"noul", "choice", "score"}


@pytest.mark.parametrize("response", [
    httpx.Response(404, json={"error": "not found"}),
    # llama.cpp's answer for a chat model, and an engine that 500s a request it cannot decide.
    httpx.Response(400, json={"error": {"message": "model is not a decision model"}}),
    httpx.Response(500, json={"error": "boom"}),
    # A 200 that is not a decision: a catch-all page, a chat completion.
    httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]}),
    httpx.Response(200, text="<html>ok</html>"),
], ids=["404", "400", "500", "chat-body", "html"])
def test_anything_but_a_decision_fails_the_probe(monkeypatch, response):
    _mock_http(monkeypatch, lambda request: response)
    assert probe.probe_systemone("http://engine.example/v1", "m") is False


@pytest.mark.parametrize("broken", [
    lambda answers: answers.pop("urgency"),
    lambda answers: answers["team"].update(type="score"),
    lambda answers: answers["refund"].update(noul=True),
    lambda answers: answers["refund"].pop("noul"),
    lambda answers: answers["team"].update(probabilities={}),
    lambda answers: answers["urgency"].pop("probabilities"),
], ids=["missing-question", "wrong-type", "bool-noul", "no-noul", "empty-probabilities", "no-probabilities"])
def test_an_incomplete_decision_fails_the_probe(monkeypatch, broken):
    body = json.loads(json.dumps(DECISION))
    broken(body["answers"])
    _mock_http(monkeypatch, lambda request: httpx.Response(200, json=body))
    assert probe.probe_systemone("http://engine.example/v1", "m") is False


def test_an_unreachable_engine_fails_the_probe(monkeypatch):
    def boom(request):
        raise httpx.ConnectError("unreachable")

    _mock_http(monkeypatch, boom)
    assert probe.probe_systemone("http://engine.example/v1", "m") is False


def test_probe_chats_reads_a_one_token_completion(monkeypatch):
    seen = _mock_http(monkeypatch, lambda request: httpx.Response(200, json={"choices": []}))
    assert probe.probe_chats("http://engine.example/v1", "m") is True
    assert seen[0].url.path == "/v1/chat/completions"
    assert json.loads(seen[0].content)["max_tokens"] == 1

    # llama.cpp's answer for a decision model.
    _mock_http(monkeypatch, lambda request: httpx.Response(
        500, json={"error": {"message": "the current context does not support logits computation"}}))
    assert probe.probe_chats("http://engine.example/v1", "m") is False


# --- _probe_spec_caps ---------------------------------------------------------------------------


def _stub_engine(monkeypatch, *, deciders=(), chatters=None):
    """Stub the HTTP layers of a hardware probe. ``deciders`` answer System One; ``chatters`` answer
    chat (default: every model that is not a decider). Returns the calls each probe received."""
    calls = {"systemone": [], "chats": [], "features": []}

    def systemone(url, model, **kw):
        calls["systemone"].append(model)
        return model in deciders

    def chats(url, model, **kw):
        calls["chats"].append(model)
        return model in chatters if chatters is not None else model not in deciders

    def features(url, model):
        calls["features"].append(model)
        return dict(NO_FEATURES)

    monkeypatch.setattr(probe, "probe_responses_endpoint", lambda *a, **k: False)
    monkeypatch.setattr(probe, "probe_systemone", systemone)
    monkeypatch.setattr(probe, "probe_chats", chats)
    monkeypatch.setattr(probe, "probe_llama_capabilities", features)
    return calls


def test_a_decision_model_advertises_systemone_and_nothing_else(monkeypatch):
    calls = _stub_engine(monkeypatch, deciders={"laya"})

    caps = serve._probe_spec_caps("http://engine.example/v1", ["laya"], ["laya"], None)

    assert caps["models"]["laya"] == probe.systemone_entry()
    assert caps["models"]["laya"]["endpoints"] == ["systemone"]
    assert calls["features"] == []  # the chat feature probes are skipped, not run to fail


def test_a_chat_model_is_unchanged_and_never_asked_to_chat(monkeypatch):
    calls = _stub_engine(monkeypatch)

    caps = serve._probe_spec_caps("http://engine.example/v1", ["qwen"], ["qwen"], None)

    assert caps["models"]["qwen"]["endpoints"] == ["chat/completions", "completions"]
    assert calls["systemone"] == ["qwen"]
    assert calls["chats"] == []  # only a model that decided is ever asked whether it also chats


def test_a_model_that_decides_and_chats_advertises_both(monkeypatch):
    _stub_engine(monkeypatch, deciders={"both"}, chatters={"both"})

    entry = serve._probe_spec_caps("http://engine.example/v1", ["both"], ["both"], None)["models"]["both"]

    assert entry["endpoints"] == ["chat/completions", "completions", "systemone"]
    assert "features" in entry and "vision" in entry["features"]


def test_one_engine_serving_chat_and_decision_models_probes_each_by_its_engine_name(monkeypatch):
    # An Ollama behind `--advertise-as`: probed by the upstream name, keyed by the advertised one.
    calls = _stub_engine(monkeypatch, deciders={"nimble:latest"})

    caps = serve._probe_spec_caps(
        "http://ollama.example/v1", ["chat", "decide"], ["qwen3:4b", "nimble:latest"], None)

    assert calls["systemone"] == ["qwen3:4b", "nimble:latest"]
    assert caps["models"]["decide"]["endpoints"] == ["systemone"]
    assert "systemone" not in caps["models"]["chat"]["endpoints"]


# --- aliases ------------------------------------------------------------------------------------


def _engine(url, models, caps):
    return (url, list(models), list(models), {"schema_version": 1, "models": caps})


CHAT = {"endpoints": ["chat/completions", "completions"], "features": {}}


def test_jev_default_names_reach_the_first_decision_model():
    routes, upstream, models, caps, _ = serve._build_routing([
        _engine("http://chat/v1", ["qwen"], {"qwen": CHAT}),
        _engine("http://laya/v1", ["laya-english"], {"laya-english": probe.systemone_entry()}),
        _engine("http://kev/v1", ["kev"], {"kev": probe.systemone_entry()}),
    ])

    assert models[:3] == ["qwen", "laya-english", "kev"]
    for alias in ("default", "laya", "jev-latest", "jev-preview", "openjev-latest"):
        assert alias in models
        assert routes[alias] == "http://laya/v1"
        assert upstream[alias] == "laya-english"
        assert caps["models"][alias] == probe.systemone_entry()
    # The relay refuses a registration whose caps keys are not exactly its model list.
    assert set(caps["models"]) == set(models)


def test_aliases_never_shadow_a_real_model():
    routes, _, models, caps, _ = serve._build_routing([
        _engine("http://chat/v1", ["default"], {"default": CHAT}),
        _engine("http://laya/v1", ["laya"], {"laya": probe.systemone_entry()}),
    ])

    assert routes["default"] == "http://chat/v1" and caps["models"]["default"] == CHAT
    assert routes["laya"] == "http://laya/v1"
    assert models.count("default") == 1 and models.count("laya") == 1
    assert routes["jev-latest"] == "http://laya/v1"


def test_no_aliases_without_a_decision_only_model():
    both = {**CHAT, "endpoints": ["chat/completions", "completions", "systemone"]}
    _, _, models, _, _ = serve._build_routing([_engine("http://e/v1", ["qwen", "both"], {"qwen": CHAT, "both": both})])

    assert models == ["qwen", "both"]  # a model that also chats would list an alias as a chat model


# --- handle_job ---------------------------------------------------------------------------------


def _state(monkeypatch, tmp_path, models, capabilities, routes=None, upstream=None):
    from shared.system import host

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    monkeypatch.setattr(host, "platform_kind", lambda: "linux")
    monkeypatch.setattr(host, "disk_gb", lambda path: None)
    return serve._ServeState(
        signaling_url="https://relay.example", node_id="node-1", network_id="n1",
        llm_url="http://127.0.0.1:8081/v1", access_token="AT", refresh_token="RT",
        models=models, capabilities={"schema_version": 1, "models": capabilities},
        meta={"name": "e1", "engine": "llama.cpp"}, pricing={}, max_concurrency=1,
        routes=routes, upstream=upstream,
    )


def _capture_relay(monkeypatch):
    captured = {}
    monkeypatch.setattr(relay, "submit_response", lambda url, tok, txn, *, content, stream: captured.update(
        content=content, stream=stream))
    monkeypatch.setattr(relay, "submit_error", lambda url, tok, txn, *, message, tokens_delivered=0: captured.update(
        error=message))
    return captured


@pytest.mark.parametrize("is_stream", [False, True])
def test_a_decision_job_is_forwarded_whole_to_systemone(monkeypatch, tmp_path, is_stream):
    state = _state(monkeypatch, tmp_path, ["laya"], {"laya": probe.systemone_entry()})
    captured = _capture_relay(monkeypatch)
    seen = _mock_http(monkeypatch, lambda request: httpx.Response(200, json=DECISION))
    job_body = {"model": "laya", "state": "Refund me.", "questions": {"q": {"type": "noul", "instructions": "?"}}}

    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "systemone", "body": job_body,
                             "is_stream": is_stream, "wire_format": "systemone"})

    assert "error" not in captured
    assert captured["stream"] is False and json.loads(captured["content"]) == DECISION
    (request,) = seen
    assert str(request.url) == "http://127.0.0.1:8081/v1/systemone"
    assert json.loads(request.content) == job_body


def test_a_decision_job_for_a_chat_model_is_refused_not_forwarded(monkeypatch, tmp_path):
    state = _state(monkeypatch, tmp_path, ["qwen"], {"qwen": CHAT})
    captured = _capture_relay(monkeypatch)
    _mock_http(monkeypatch, lambda request: pytest.fail("a chat model's engine must never see a decision"))

    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "systemone",
                             "body": {"model": "qwen", "state": "x", "questions": {}}, "is_stream": False})

    assert captured["error"] == "unsupported endpoint: 'systemone'"


def test_an_alias_is_forwarded_under_the_engine_s_own_name(monkeypatch, tmp_path):
    # An Ollama behind `--advertise-as`: advertised as `nimble`, known to the engine as `nimble:latest`.
    routes, upstream, models, caps, _ = serve._build_routing([(
        "http://127.0.0.1:8081/v1", ["nimble"], ["nimble:latest"],
        {"schema_version": 1, "models": {"nimble": probe.systemone_entry()}},
    )])
    state = _state(monkeypatch, tmp_path, models, caps["models"], routes=routes, upstream=upstream)
    _capture_relay(monkeypatch)
    seen = _mock_http(monkeypatch, lambda request: httpx.Response(200, json=DECISION))

    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "systemone",
                             "body": {"model": "jev-latest", "state": "x", "questions": {}}, "is_stream": False})

    assert json.loads(seen[0].content)["model"] == "nimble:latest"


# --- the GGUF, the launch, the catalog ----------------------------------------------------------


def _write_gguf(path, strings):
    blob = b""
    for key, value in strings.items():
        blob += struct.pack("<Q", len(key)) + key.encode() + struct.pack("<I", 8)
        blob += struct.pack("<Q", len(value)) + value.encode()
    path.write_bytes(b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 0) + struct.pack("<Q", len(strings)) + blob)


def test_decision_type_reads_the_gguf_header(tmp_path):
    laya, qwen, junk = tmp_path / "laya.gguf", tmp_path / "qwen.gguf", tmp_path / "junk.gguf"
    _write_gguf(laya, {"general.architecture": "modern-bert", "modern-bert.decision.type": "laya"})
    _write_gguf(qwen, {"general.architecture": "qwen3"})
    junk.write_bytes(b"not a gguf")

    assert gguf.decision_type(laya) == "laya"
    assert gguf.decision_type(qwen) is None
    assert gguf.decision_type(junk) is None
    assert gguf.decision_type(tmp_path / "missing.gguf") is None


class _FakeProc:
    pid = 12345

    def poll(self):
        return None


def _launch(monkeypatch, tmp_path, *, decision, build):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    model_path = paths.models_dir() / "model.gguf"
    model_path.parent.mkdir(parents=True, exist_ok=True)
    _write_gguf(model_path, {"modern-bert.decision.type": "laya"} if decision else {"general.architecture": "qwen3"})
    monkeypatch.setattr(launcher, "llama_server_path", lambda: "/usr/local/bin/llama-server")
    monkeypatch.setattr(launcher, "parse_version", lambda timeout=5.0: build)
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda cmd, **kw: _FakeProc())
    return launcher.start_llm("model.gguf", port=8081)


def test_an_old_engine_refuses_a_decision_model_by_name(monkeypatch, tmp_path):
    with pytest.raises(SystemExit) as exc:
        _launch(monkeypatch, tmp_path, decision=True, build=10369)
    message = str(exc.value)
    assert "decision model" in message and str(launcher.MIN_DECISION_BUILD) in message
    assert "grid engine install llama.cpp" in message


@pytest.mark.parametrize("decision, build", [(True, launcher.MIN_DECISION_BUILD), (True, None), (False, 10369)])
def test_a_new_engine_or_a_chat_model_launches(monkeypatch, tmp_path, decision, build):
    assert _launch(monkeypatch, tmp_path, decision=decision, build=build).port == 8081


@pytest.mark.parametrize("output, build", [
    ("version: 10369 (6e62ba538)\nbuilt with AppleClang", 10369),               # grid's old pin
    ("version: 0.5.0 (build 11146, commit 7fe450e19)", 11146),                  # Homebrew
    ("load_backend: loaded\nversion: 0.5.0-dev (build 11378, commit edd6e2bbd)", 11378),  # the new pin
    ("llama-server: unknown option", None),
])
def test_parse_version_reads_old_and_semver_build_lines(monkeypatch, output, build):
    monkeypatch.setattr(launcher, "llama_server_path", lambda: "/usr/local/bin/llama-server")
    monkeypatch.setattr(launcher.subprocess, "run",
                        lambda *a, **k: launcher.subprocess.CompletedProcess(a, 0, stdout="", stderr=output))
    assert launcher.parse_version() == build


def test_the_catalog_offers_laya_as_a_decision_model_everywhere():
    (entry,) = [e for e in catalog.CATALOG if e.kind == "decision"]
    assert catalog.pull_spec(entry) == "ggml-org/Laya-GGUF:Laya-Q8_0.gguf"
    for target in (catalog.TARGET_APPLE_SILICON, catalog.TARGET_NVIDIA):
        assert entry in catalog.recommended_entries(target)
    assert "decision" in catalog.format_catalog_entry(entry)


# --- what `grid join` says to try next ---------------------------------------------------------


def test_a_joined_decision_model_is_offered_a_decision_never_grid_chat(monkeypatch, tmp_path):
    import argparse
    import shlex

    from cli import provider, remote_provider

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    paths.models_dir().mkdir(parents=True, exist_ok=True)
    _write_gguf(paths.models_dir() / "Laya-Q8_0.gguf", {"modern-bert.decision.type": "laya"})
    _write_gguf(paths.models_dir() / "qwen.gguf", {"general.architecture": "qwen3"})
    asked = []
    monkeypatch.setattr(probe, "probe_systemone", lambda url, model, **kw: asked.append((url, model)) or model == "nimble")

    serve_laya = argparse.Namespace(serve="Laya-Q8_0.gguf", at=None, models=[], mmproj=None)
    serve_qwen = argparse.Namespace(serve="qwen.gguf", at=None, models=[], mmproj=None)
    at_nimble = argparse.Namespace(serve=None, at="http://127.0.0.1:11434/v1", models=["nimble"], mmproj=None)
    assert provider.serves_decisions(serve_laya) and not provider.serves_decisions(serve_qwen)
    assert provider.serves_decisions(at_nimble) and asked == [("http://127.0.0.1:11434/v1", "nimble")]
    # A vendor's API sees no traffic at join — not even this probe.
    vendor = argparse.Namespace(serve=None, at="https://api.openai.com/v1", models=["gpt-5.5"], api="openai", mmproj=None)
    assert not provider.serves_decisions(vendor) and len(asked) == 1

    hints = remote_provider._next_hints(serve_laya, "Laya-Q8_0", "'my grid'")
    assert hints[0] == """  eval "$(grid info 'my grid' --env)\""""
    assert not any("grid chat" in line for line in hints)
    # The body is one shell word that parses back to a valid System One request.
    body = json.loads(shlex.split(hints[-1])[-1])
    assert body["model"] == "Laya-Q8_0" and body["questions"]["refund"]["type"] == "noul"
    assert remote_provider._next_hints(serve_qwen, "qwen", "home")[0].startswith("  grid chat -m qwen")


def test_a_slow_first_version_read_is_waited_for_before_a_decision_model_launches(monkeypatch, tmp_path):
    """A cold binary can take longer than the usual read; a decision model asks again, longer."""
    reads = iter([None, 10369])
    asked = []

    def version(timeout=5.0):
        asked.append(timeout)
        return next(reads)

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    paths.models_dir().mkdir(parents=True, exist_ok=True)
    _write_gguf(paths.models_dir() / "laya.gguf", {"modern-bert.decision.type": "laya"})
    monkeypatch.setattr(launcher, "parse_version", version)
    with pytest.raises(SystemExit, match="build 10369 cannot serve"):
        launcher.assert_serves(paths.models_dir() / "laya.gguf")
    assert asked == [5.0, 30.0]


def test_a_remote_join_refuses_a_decision_model_on_an_old_engine_before_it_spawns(monkeypatch, tmp_path):
    import argparse

    from cli import remote_provider

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    paths.models_dir().mkdir(parents=True, exist_ok=True)
    _write_gguf(paths.models_dir() / "Laya-Q8_0.gguf", {"modern-bert.decision.type": "laya"})
    args = argparse.Namespace(at=None, serve="Laya-Q8_0.gguf", models=[], media=False)
    monkeypatch.setattr(launcher, "parse_version", lambda timeout=5.0: 11146)
    with pytest.raises(SystemExit, match="grid engine install llama.cpp"):
        remote_provider._resolve_serve_targets(args)
    monkeypatch.setattr(launcher, "parse_version", lambda timeout=5.0: launcher.MIN_DECISION_BUILD)
    specs, _ = remote_provider._resolve_serve_targets(args)
    assert specs[0]["models"] == ["Laya-Q8_0.gguf"]


def test_a_child_older_than_system_one_is_respawned_rather_than_reloaded():
    """A decision model hot-reloaded into a child that predates System One would be advertised as chat
    and every decision refused. Such a child never stamped `serves_systemone`; the join respawns it."""
    from cli import remote_provider

    external = [{"endpoint_url": "http://127.0.0.1:50104/v1", "models": ["laya-english"]}]
    record = {"engines": external}
    current = {"engine_id": "remote", "reload_signal": "sighup", "serves_systemone": True, "engines": []}
    older = {key: value for key, value in current.items() if key != "serves_systemone"}
    assert remote_provider._hot_reloadable([current], external, record) is True
    assert remote_provider._hot_reloadable([older], external, record) is False


def test_an_engine_joins_or_leaves_beside_a_running_built_in_by_reload_not_respawn():
    """Starting or stopping a Jev model beside a chat model the identity runs itself (`--serve`) respawned
    the identity — the reload gate refused any built-in — and the respawn reloaded the whole chat model. A
    child that keeps the built-ins it runs (`reloads_builtins`) now takes the reload when they are exactly
    those, unchanged; a built-in added, retuned or dropped, or an older child, still respawns."""
    from cli import remote_provider

    qwen = {"endpoint_url": None, "models": ["Qwen-Q5.gguf"],
            "launch": {"endpoint_port": 64101, "ctx_size": 262144, "parallel": 1}}
    kev = {"endpoint_url": "http://127.0.0.1:50872/v1", "models": ["kev-0.8b"]}
    live = {"engine_id": "remote", "reload_signal": "sighup", "serves_systemone": True,
            "reloads_builtins": True, "engines": [qwen]}
    assert remote_provider._hot_reloadable([live], [qwen, kev], {"engines": [qwen, kev]}) is True
    assert remote_provider._hot_reloadable(
        [{**live, "engines": [qwen, kev]}], [qwen], {"engines": [qwen]}) is True

    older = {key: value for key, value in live.items() if key != "reloads_builtins"}
    assert remote_provider._hot_reloadable([older], [qwen, kev], {"engines": [qwen, kev]}) is False
    retuned = {**qwen, "launch": {**qwen["launch"], "ctx_size": 65536}}
    assert remote_provider._hot_reloadable([live], [retuned, kev], {"engines": [retuned, kev]}) is False
    other = {"endpoint_url": None, "models": ["Other.gguf"], "launch": {"endpoint_port": 8082}}
    assert remote_provider._hot_reloadable([live], [qwen, other], {"engines": [qwen, other]}) is False
    assert remote_provider._hot_reloadable([live], [kev], {"engines": [kev]}) is False
