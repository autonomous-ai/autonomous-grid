"""`grid models | engines | stats --no-wake`: read a remote grid without waking it (grid-reads-without-waking 01).

The control plane's proxy wakes a sleeping grid for a signed-in read of its overview, and answers a
credential-less one at once with ``503 grid_asleep`` — waking nothing. ``--no-wake`` is how the
harness's Model Manager viewer (and its daemon, as a fallback) reads a grid the second way, and every
assertion here is on what the fake transport actually RECEIVED: no ``Authorization`` under the flag, a
Bearer without it, no request at all when the owner status already says ``asleep``.

Also here, because they ship in the same release: the ``grid_asleep`` code in the ``--json`` error
envelope, ``grid models``' exact-case ids, and the versioned User-Agent.
"""
from __future__ import annotations

import json
import re

import httpx
import pytest

import cli
from local import runtime
from shared import state
from shared._version import __version__
from tests._remote_seed import seed_remote_grid

_OVERVIEW = {
    "grid": {"state": "running"},
    "stats": {"models": 1, "nodes": 1, "concurrent_capacity": 2, "uptime_pct": 99.9},
    "models": [],
    "nodes": [{
        "name": "mac-studio", "device": "Mac Studio", "engine": "MLX", "memory_gb": 192,
        "model": "glm-5.2", "models": ["glm-5.2"], "responses_models": [],
        "throughput_tok_s": 58.0, "max_concurrency": 2, "online": True,
    }],
}

_ASLEEP = {"detail": "grid is asleep and this request does not wake it — a person's request wakes it",
           "code": "grid_asleep"}


_seed = seed_remote_grid


def _relay(monkeypatch, routes, _real=httpx.Client):
    """Serve the relay through a MockTransport: ``routes`` maps a path to ``(status, json)``. Returns
    every request the transport received, as ``{method, path, headers}``."""
    from remote import relay

    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({
            "method": request.method, "path": request.url.path,
            "headers": {k.lower(): v for k, v in request.headers.items()},
        })
        status, body = routes.get(request.url.path, (404, {"detail": "Not Found"}))
        return httpx.Response(status, json=body)

    monkeypatch.setattr(
        relay.httpx, "Client",
        lambda *a, **k: _real(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )
    return seen


def _envelope(capsys):
    captured = capsys.readouterr()
    return captured.out, json.loads(captured.err.strip().splitlines()[-1])


_COMMANDS = ["models", "engines", "stats"]


# --- the credential --------------------------------------------------------------------------------


@pytest.mark.parametrize("command", _COMMANDS)
def test_no_wake_sends_no_credential(monkeypatch, tmp_path, command):
    _seed(monkeypatch, tmp_path)
    seen = _relay(monkeypatch, {"/relay/v1/grid/overview": (200, _OVERVIEW)})

    assert cli.main([command, "--no-wake"]) == 0

    assert [r["path"] for r in seen][:1] == ["/relay/v1/grid/overview"]
    for request in seen:
        assert "authorization" not in request["headers"], request
        assert "x-api-key" not in request["headers"], request


@pytest.mark.parametrize("command", _COMMANDS)
def test_without_no_wake_the_bearer_is_sent_as_today(monkeypatch, tmp_path, command):
    _seed(monkeypatch, tmp_path)
    seen = _relay(monkeypatch, {"/relay/v1/grid/overview": (200, _OVERVIEW)})

    assert cli.main([command]) == 0

    assert seen[0]["path"] == "/relay/v1/grid/overview"
    assert seen[0]["headers"]["authorization"] == "Bearer AT"


# --- asleep ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("command", _COMMANDS)
def test_an_owner_status_of_asleep_is_reported_with_no_proxy_request(monkeypatch, tmp_path, capsys, command):
    _seed(monkeypatch, tmp_path, state_word="asleep")
    seen = _relay(monkeypatch, {"/relay/v1/grid/overview": (200, _OVERVIEW)})

    with pytest.raises(SystemExit) as caught:
        cli.main([command, "--no-wake", "--json"])

    assert seen == [], "the owner status already said asleep: nothing is asked of the proxy"
    out, envelope = _envelope(capsys)
    assert out == ""
    assert envelope["error"]["code"] == "grid_asleep"
    assert envelope["error"]["status"] is None, "no proxy answered"
    assert "asleep" in str(caught.value)


def test_an_owner_status_of_asleep_says_so_in_words(monkeypatch, tmp_path, capsys):
    _seed(monkeypatch, tmp_path, state_word="asleep")
    _relay(monkeypatch, {})

    with pytest.raises(SystemExit) as caught:
        cli.main(["models", "--no-wake"])

    message = str(caught.value)
    assert "team" in message and "asleep" in message
    assert "--no-wake" in message, "the sentence names how to read it anyway"
    assert capsys.readouterr().err == "", "no envelope without --json"


@pytest.mark.parametrize("command", _COMMANDS)
def test_without_no_wake_an_asleep_owner_status_is_read_as_today(monkeypatch, tmp_path, command):
    """`asleep` is a callable state (idle-sleep issue 04): the signed-in read is what wakes it."""
    _seed(monkeypatch, tmp_path, state_word="asleep")
    seen = _relay(monkeypatch, {"/relay/v1/grid/overview": (200, _OVERVIEW)})

    assert cli.main([command]) == 0
    assert seen[0]["headers"]["authorization"] == "Bearer AT"


@pytest.mark.parametrize("command", _COMMANDS)
def test_the_proxys_asleep_answer_puts_grid_asleep_in_the_envelope(monkeypatch, tmp_path, capsys, command):
    """A member cannot read the owner status, so the proxy is the one that says it."""
    _seed(monkeypatch, tmp_path)
    _relay(monkeypatch, {"/relay/v1/grid/overview": (503, _ASLEEP)})

    with pytest.raises(SystemExit):
        cli.main([command, "--no-wake", "--json"])

    out, envelope = _envelope(capsys)
    assert out == ""
    assert envelope["error"]["code"] == "grid_asleep"
    assert envelope["error"]["status"] == 503


def test_the_asleep_answer_carrying_a_record_is_still_just_asleep(monkeypatch, tmp_path, capsys):
    body = {**_ASLEEP, "last_known": {"age_seconds": 60, "nodes": [], "ids": []}}
    _seed(monkeypatch, tmp_path)
    _relay(monkeypatch, {"/relay/v1/grid/overview": (503, body)})

    with pytest.raises(SystemExit):
        cli.main(["models", "--no-wake", "--json"])

    out, envelope = _envelope(capsys)
    assert out == "" and envelope["error"]["code"] == "grid_asleep"


@pytest.mark.parametrize("body", [
    {"detail": "grid's master is not answering, and whether it is asleep could not be read"},
    {"detail": "grid's master is down, not asleep", "code": "grid_master_down"},
    {"detail": "grid was stopped by its owner", "code": "grid_stopped"},
    {"detail": "grid is asleep", "code": "GRID_ASLEEP"},  # equality, never a family resemblance
    "not even an object",
])
def test_any_other_refusal_keeps_todays_output_and_a_null_code(monkeypatch, tmp_path, capsys, body):
    _seed(monkeypatch, tmp_path)
    _relay(monkeypatch, {"/relay/v1/grid/overview": (503, body)})

    with pytest.raises(SystemExit) as caught:
        cli.main(["models", "--no-wake", "--json"])

    out, envelope = _envelope(capsys)
    assert out == ""
    assert envelope["error"]["code"] is None
    sent = httpx.Response(503, json=body).text  # the very bytes the fake proxy answered with
    assert str(caught.value) == f"Grid team overview failed (503): {sent[:200]}"


# --- local mode ------------------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["models", "engines"])
def test_no_wake_is_accepted_and_ignored_in_local_mode(monkeypatch, tmp_path, capsys, command):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    state.set_mode("local")
    runtime.init_grid_config(name="home", port=8090)
    engines = [{"name": "mac", "endpoint_url": "http://192.168.1.10:8080/v1", "models": ["gemma4-31b"]}]
    monkeypatch.setattr(cli.provider, "_discover", lambda cfg: engines)

    assert cli.main([command, "home", "--no-wake"]) == 0
    assert "gemma4-31b" in capsys.readouterr().out


# --- exact case ------------------------------------------------------------------------------------


def _write_run_record(tmp_path, record):
    from shared import run_records

    run_records.write_record("n1", "remote", {"engine_id": "remote", **record})


def _discovery(raw_ids):
    return {"providers": [{
        "node_id": "node-1",
        "models": [f"provider:node-1:r{i}" for i in range(len(raw_ids))],
        "capabilities": {"models": {
            f"provider:node-1:r{i}": {"raw_model_id": raw} for i, raw in enumerate(raw_ids)
        }},
    }]}


def _overview_serving(*models, responses=()):
    node = {**_OVERVIEW["nodes"][0], "models": list(models), "responses_models": list(responses)}
    return {**_OVERVIEW, "nodes": [node]}


def _models_json(capsys):
    return json.loads(capsys.readouterr().out)


def test_models_take_their_case_from_this_computers_run_records(monkeypatch, tmp_path, capsys):
    _seed(monkeypatch, tmp_path)
    _write_run_record(tmp_path, {"models": ["Qwen3.5-2B-Q4_K_M.gguf"], "advertise_as": []})
    seen = _relay(monkeypatch, {"/relay/v1/grid/overview": (200, _overview_serving("qwen3.5-2b-q4_k_m"))})

    assert cli.main(["models", "--json", "--no-wake"]) == 0

    assert [row["model"] for row in _models_json(capsys)] == ["Qwen3.5-2B-Q4_K_M"]
    assert [r["path"] for r in seen] == ["/relay/v1/grid/overview"], "resolved: discovery is not read"


def test_a_run_records_advertised_name_is_read_not_only_its_file_name(monkeypatch, tmp_path, capsys):
    """`advertise_as` is what was registered; `models` is only the file the engine loaded."""
    _seed(monkeypatch, tmp_path)
    _write_run_record(tmp_path, {"models": ["qwen-file.GGUF"], "advertise_as": ["Qwen-Coder"]})
    _relay(monkeypatch, {"/relay/v1/grid/overview": (200, _overview_serving("qwen-coder"))})

    assert cli.main(["models", "--json"]) == 0
    assert [row["model"] for row in _models_json(capsys)] == ["Qwen-Coder"]


def test_models_nobody_here_serves_take_their_case_from_provider_discovery(monkeypatch, tmp_path, capsys):
    _seed(monkeypatch, tmp_path)
    seen = _relay(monkeypatch, {
        "/relay/v1/grid/overview": (200, _overview_serving("glm-5.2-air", "qwen3-coder-30b")),
        "/nodes/discover": (200, _discovery(["GLM-5.2-Air.gguf", "Qwen3-Coder-30B"])),
    })

    assert cli.main(["models", "--json", "--no-wake"]) == 0

    assert [row["model"] for row in _models_json(capsys)] == ["GLM-5.2-Air", "Qwen3-Coder-30B"]
    discover = [r for r in seen if r["path"] == "/nodes/discover"]
    assert len(discover) == 1
    assert "authorization" not in discover[0]["headers"], "--no-wake reads discovery without one too"


@pytest.mark.parametrize("answer", [
    (500, {"detail": "boom"}),
    (200, {"providers": "not a list"}),
    (200, ["not", "an", "object"]),
    (503, _ASLEEP),
])
def test_a_failed_discovery_keeps_todays_ids(monkeypatch, tmp_path, capsys, answer):
    _seed(monkeypatch, tmp_path)
    _relay(monkeypatch, {
        "/relay/v1/grid/overview": (200, _overview_serving("glm-5.2-air")),
        "/nodes/discover": answer,
    })

    assert cli.main(["models", "--json"]) == 0
    assert [row["model"] for row in _models_json(capsys)] == ["glm-5.2-air"]


def test_an_unreadable_run_record_keeps_todays_ids(monkeypatch, tmp_path, capsys):
    from shared import paths

    _seed(monkeypatch, tmp_path)
    directory = paths.engines_dir("n1")
    directory.mkdir(parents=True)
    (directory / "remote.json").write_text("{ not json")
    _relay(monkeypatch, {"/relay/v1/grid/overview": (200, _overview_serving("glm-5.2-air"))})

    assert cli.main(["models", "--json"]) == 0
    assert [row["model"] for row in _models_json(capsys)] == ["glm-5.2-air"]


def test_an_upper_case_id_is_never_replaced_by_its_lower_case_form(monkeypatch, tmp_path, capsys):
    """The curated list is read first (today's source). A lower-case curated id must not shadow the
    exact case discovery knows; an upper-case one must not be shadowed by anything."""
    _seed(monkeypatch, tmp_path)
    overview = {**_overview_serving("glm-5.2", "kimi-k2"),
                "models": [{"id": "glm-5.2"}, {"id": "Kimi-K2"}]}
    _relay(monkeypatch, {
        "/relay/v1/grid/overview": (200, overview),
        "/nodes/discover": (200, _discovery(["GLM-5.2", "kimi-k2"])),
    })

    assert cli.main(["models", "--json"]) == 0
    assert [row["model"] for row in _models_json(capsys)] == ["GLM-5.2", "Kimi-K2"]


def test_an_exact_case_model_keeps_its_responses_badge(monkeypatch, tmp_path, capsys):
    """The overview's `responses_models` is lower-cased like its `models`; the badge is matched by
    name, so it must survive the id's case being restored."""
    _seed(monkeypatch, tmp_path)
    _relay(monkeypatch, {
        "/relay/v1/grid/overview": (200, _overview_serving("gpt-5.4-mini", responses=["gpt-5.4-mini"])),
        "/nodes/discover": (200, _discovery(["GPT-5.4-Mini"])),
    })

    assert cli.main(["models", "--json"]) == 0
    assert _models_json(capsys) == [
        {"model": "GPT-5.4-Mini", "engine": "MLX", "node": "mac-studio", "responses": True},
    ]


# --- the User-Agent --------------------------------------------------------------------------------

_UA = re.compile(r"^grid-cli/[0-9][^ ]*( \(no-wake\))?$")


def test_the_user_agent_names_the_version_and_says_when_no_credential_is_sent(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    seen = _relay(monkeypatch, {"/relay/v1/grid/overview": (200, _OVERVIEW)})

    cli.main(["models"])
    cli.main(["models", "--no-wake"])

    signed, unsigned = seen[0]["headers"]["user-agent"], seen[-1]["headers"]["user-agent"]
    assert _UA.match(signed) and _UA.match(unsigned), (signed, unsigned)
    assert signed == f"grid-cli/{__version__}"
    assert unsigned == f"grid-cli/{__version__} (no-wake)"


def test_the_control_plane_client_names_the_version_too():
    """Never `(no-wake)` there: the control plane is not behind the proxy, so the suffix — which the
    operator reads as "this request could not have woken a grid" — would only mislabel a sign-in."""
    from remote import control_plane

    with control_plane._client("https://api.example", "sess") as client:
        assert client.headers["user-agent"] == f"grid-cli/{__version__}"
    with control_plane._client("https://api.example") as client:
        assert client.headers["user-agent"] == f"grid-cli/{__version__}"
