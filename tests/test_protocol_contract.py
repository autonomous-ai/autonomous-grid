"""What `grid-protocol` says, checked against itself, against this CLI, and against real answers.

Three kinds of check, all of which run in this repository's CI (none needs a sibling checkout):

1. **The contract's own rules** — the refusal codes the platform speaks and the status each travels with, and
   the rules BETWEEN timing values that JSON Schema cannot express (a parked probe fits twice in the boot
   hold; a model outlives a node TTL and a beat; …).
2. **This CLI speaks it** — every value the public CLI hand-writes today equals the schema's. When the CLI
   imports `grid_protocol` instead (its next release), these become redundant rather than wrong.
3. **Real answers fit it** — each recording under ``protocol/recordings/`` is an answer taken from a real
   server (the DEV VM, credential-less), sanitized, and validated against the shape it claims to be.
"""
from __future__ import annotations

import ast
import json
import pathlib
import re

import grid_protocol
import httpx
import pytest
from grid_protocol import constants as protocol

ROOT = pathlib.Path(__file__).resolve().parent.parent
RECORDINGS = ROOT / "protocol" / "recordings"

#: The refusal codes ticket 12 models, with the status each travels with. Written out, not read from the
#: schema: a pin that compares the schema with itself checks nothing.
REFUSALS = {
    "grid_asleep": 503,
    "grid_stopped": 503,
    "grid_deleted": 410,
    "grid_master_down": 503,
    "no_providers_available": 503,
    "feature_retired": 410,
    "relay_restarting": 503,
}
#: The three this CLI acts on. The rest it must never parse: renaming one must not change what a provider does.
PARSED_BY_THIS_CLI = {"grid_asleep", "grid_stopped", "grid_deleted"}

#: Shapes with no recording yet, and why. Everything else a schema defines as an ANSWER must have one.
NOT_YET_RECORDED = {
    ("refusal", "FeatureRetired"): "no master sends it until Phase B 09 is built and on DEV (ticket 11)",
    ("openai-error", "RelayRestarting"): "only Phase B 11's branch sends it; recorded once it runs on DEV (ticket 11)",
}
#: Shapes that are requests or local values, not answers a server sends. The requests are validated against what
#: this CLI really sends, at its HTTP boundary (section 2b); the timing values by the rules in section 1.
NOT_ANSWERS = {
    ("node-registration", None), ("node-heartbeat", None), ("node-heartbeat", "NodeLoad"),
    ("node-registration", "NodeCapabilities"), ("node-registration", "ModelCapability"),
    ("node-registration", "NodeMeta"), ("node-upload", "ErrorReport"), ("timing", None),
    ("cli-error", None),
}


# ---- 1. the contract's own rules ------------------------------------------------------------------------

def test_the_platform_speaks_exactly_these_refusal_codes_with_these_statuses():
    assert dict(protocol.REFUSAL_STATUS) == REFUSALS


def test_a_parked_provider_probes_twice_inside_a_woken_masters_boot_hold():
    # A woken master starts with every provider past its TTL. It holds a request that finds nobody for
    # BOOT_HOLD_SECONDS; a provider parked on the sleeping grid must beat inside that, with a spare.
    assert protocol.ASLEEP_PROBE_SECONDS * 2 <= protocol.BOOT_HOLD_SECONDS
    assert protocol.AFTER_A_FAILED_BEAT_SECONDS * 2 <= protocol.BOOT_HOLD_SECONDS
    assert protocol.BOOT_HOLD_SECONDS < protocol.BOOT_HOLD_WINDOW_SECONDS


def test_a_node_beats_several_times_inside_its_ttl():
    assert protocol.HEARTBEAT_INTERVAL_SECONDS * 2 <= protocol.NODE_TTL_SECONDS


def test_what_waits_out_a_node_ttl_and_a_beat_waits_at_least_that_long():
    ttl_and_a_beat = protocol.NODE_TTL_SECONDS + protocol.HEARTBEAT_INTERVAL_SECONDS

    assert protocol.MODEL_RETENTION_SECONDS >= ttl_and_a_beat
    assert protocol.ACCESS_LOSS_SETTLE_SECONDS >= ttl_and_a_beat
    assert protocol.SLEEP_RECORD_MIN_UPTIME_SECONDS > ttl_and_a_beat


def test_the_last_known_record_dies_before_the_master_forgets_its_nodes():
    assert protocol.LAST_KNOWN_MAX_AGE_SECONDS < protocol.MASTER_NODE_PRUNE_SECONDS
    age = grid_protocol.load_schema("last-known")["properties"]["age_seconds"]
    assert age["maximum"] == protocol.LAST_KNOWN_MAX_AGE_SECONDS


def test_each_refusal_code_sits_beside_detail_not_under_it():
    # A code moved under `detail` is one this CLI never finds (`relay._answer_code` reads the top level).
    for definition in ("GridAsleep", "GridStopped", "GridDeleted", "GridMasterDown", "FeatureRetired"):
        case = grid_protocol.load_schema("refusal")["$defs"][definition]
        assert case["required"] == ["detail", "code"], definition


# ---- 2. this CLI speaks it -------------------------------------------------------------------------------

def test_this_cli_names_each_code_it_acts_on_as_the_schema_does():
    from remote import relay

    assert relay.GRID_ASLEEP_CODE == protocol.GRID_ASLEEP_CODE
    assert relay.GRID_STOPPED_CODE == protocol.GRID_STOPPED_CODE
    assert relay.GRID_DELETED_CODE == protocol.GRID_DELETED_CODE
    assert {relay.GRID_ASLEEP_CODE, relay.GRID_STOPPED_CODE, relay.GRID_DELETED_CODE} == PARSED_BY_THIS_CLI


def _string_constants(folder: pathlib.Path) -> set[str]:
    found: set[str] = set()
    for path in folder.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                found.add(node.value)
    return found


def test_this_cli_never_spells_a_code_it_must_not_parse():
    # Every package the wheel ships (pyproject.toml's `packages.find`).
    shipped = ("cli", "remote", "shared", "local", "doggi", "train")
    spelled = set().union(*(_string_constants(ROOT / package) for package in shipped))

    assert spelled & (set(REFUSALS) - PARSED_BY_THIS_CLI) == set()


def test_this_cli_reads_the_schemas_paths_and_flag():
    from cli import remote_overview

    assert remote_overview.OVERVIEW_PATH == protocol.OVERVIEW_PATH
    assert remote_overview.DISCOVER_PATH == protocol.DISCOVER_PATH
    assert remote_overview.NO_WAKE_FLAG == protocol.NO_WAKE_FLAG


def test_this_cli_beats_and_probes_on_the_schemas_clock():
    from remote import relay, serve

    assert relay.HEARTBEAT_INTERVAL == protocol.HEARTBEAT_INTERVAL_SECONDS
    assert serve._ASLEEP_PROBE_SECONDS == protocol.ASLEEP_PROBE_SECONDS
    assert serve._STOPPED_PROBE_SECONDS == protocol.STOPPED_PROBE_SECONDS
    assert serve._AFTER_A_FAILED_BEAT_SECONDS == protocol.AFTER_A_FAILED_BEAT_SECONDS
    # Its HTTP timeout on a poll must outlast the master's long-poll, or every idle poll is an error.
    assert relay.POLL_TIMEOUT > protocol.POLL_WINDOW_SECONDS


def test_this_cli_reports_unhealthy_models_under_the_schemas_key():
    from remote import engine_health

    assert engine_health.UNHEALTHY_LOAD_KEY == protocol.UNHEALTHY_MODELS_LOAD_KEY


def test_this_cli_sends_the_capability_envelope_at_the_schemas_version():
    from remote import probe

    envelope = probe.envelope("qwen3.5-2b", {feature: True for feature in probe.PROBED_FEATURES}, 32768,
                              endpoints=["chat/completions", "responses"])

    assert envelope["schema_version"] == protocol.CAPABILITIES_SCHEMA_VERSION
    assert grid_protocol.validator("node-registration", "NodeCapabilities").is_valid(envelope)


# ---- 2b. this CLI's node traffic, at its HTTP boundary ---------------------------------------------------

RELAY_URL = "https://relay.example"


class _Relay:
    """Stands where the relay would: records each request this CLI sends and answers in the protocol's shapes."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str, object]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        is_json = request.headers.get("content-type", "").startswith("application/json")
        self.seen.append((request.method, request.url.path, json.loads(request.content) if is_json else None))
        path = request.url.path
        if path == protocol.HEARTBEAT_PATH:
            return httpx.Response(200, json={"status": "ok", "ttl_seconds": protocol.NODE_TTL_SECONDS})
        if path == protocol.POLL_PATH:
            return httpx.Response(204)
        if path.startswith(protocol.ERROR_REPORT_PATH.split("{")[0]):
            return httpx.Response(200, json={"status": "failed"})
        if path.startswith(protocol.RESPONSE_PATH.split("{")[0]):
            return httpx.Response(200, json={"status": "delivered"})
        return httpx.Response(200, json={"status": "updated"})


@pytest.fixture
def relay_double(monkeypatch):
    """Every `httpx.Client` this CLI opens talks to a `_Relay` instead of the network."""
    double, real_client = _Relay(), httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: real_client(
        *args, transport=httpx.MockTransport(double), **kwargs))
    return double


def _assert_fits(body: object, schema: str, definition: str | None = None) -> None:
    errors = list(grid_protocol.validator(schema, definition).iter_errors(body))
    assert not errors, [f"{list(error.path)}: {error.message}" for error in errors[:5]]


def _serve_state(monkeypatch, tmp_path):
    """A serving engine as `grid join` builds one (the construction `test_grid_asleep._serve_state` uses)."""
    from remote import serve
    from shared.system import host

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    monkeypatch.setattr(host, "platform_kind", lambda: "linux")
    monkeypatch.setattr(host, "disk_gb", lambda path: None)
    return serve._ServeState(
        signaling_url=RELAY_URL, node_id="node-1", network_id="n1", llm_url="http://127.0.0.1:8081/v1",
        access_token="AT", refresh_token="RT", models=["qwen3.5-2b"],
        capabilities={"schema_version": 1, "models": {}}, meta={"name": "e1", "engine": "llama.cpp"},
        pricing={}, max_concurrency=1)


def test_this_cli_registers_and_leaves_on_the_schemas_route_in_its_shape(relay_double):
    from remote import probe, relay, serve

    record = {"meta_name": "node-a", "meta_name_chosen": True, "engines": [{"engine_label": "llama.cpp"}]}
    capabilities = probe.envelope("qwen3.5-2b", {feature: True for feature in probe.PROBED_FEATURES}, 32768)
    relay.register_node(RELAY_URL, "AT", "node-1", models=["qwen3.5-2b"], capabilities=capabilities,
                        meta=serve._meta(record, "e1"), pricing={}, max_concurrency=2)
    relay.unregister_node(RELAY_URL, "AT", "node-1")

    (method, path, body), (left_method, left_path, left_body) = relay_double.seen
    assert (method, path) == ("PUT", protocol.NODE_PATH.format(node_id="node-1"))
    _assert_fits(body, "node-registration")
    assert (left_method, left_path) == ("PUT", path)
    _assert_fits(left_body, "node-registration")
    assert left_body["role"] == "consumer" and left_body["models"] == []


def test_this_cli_beats_on_the_schemas_route_with_its_real_load_and_meta(relay_double, monkeypatch, tmp_path):
    from remote import relay
    from shared.system import node_hardware

    state = _serve_state(monkeypatch, tmp_path)

    assert relay.heartbeat(RELAY_URL, "AT", load=state.load(), meta=node_hardware.meta_fields()) == "ok"

    ((method, path, body),) = relay_double.seen
    assert (method, path) == ("POST", protocol.HEARTBEAT_PATH)
    _assert_fits(body, "node-heartbeat")


def test_this_cli_polls_and_answers_on_the_schemas_routes(relay_double):
    from remote import relay

    assert relay.poll(RELAY_URL, "AT") is None  # 204: no work
    relay.submit_response(RELAY_URL, "AT", "txn-1", content=b'{"id": "x"}', stream=False)
    relay.submit_error(RELAY_URL, "AT", "txn-2", message="the engine failed", tokens_delivered=0)

    (poll, result, report) = relay_double.seen
    assert poll[:2] == ("GET", protocol.POLL_PATH)
    assert result[:2] == ("POST", protocol.RESPONSE_PATH.format(transaction_id="txn-1"))
    assert report[:2] == ("POST", protocol.ERROR_REPORT_PATH.format(transaction_id="txn-2"))
    _assert_fits(report[2], "node-upload", "ErrorReport")
    assert all(path.startswith(protocol.RELAY_PATH + "/") for _method, path, _body in (poll, result, report))


# ---- 3. real answers fit it ------------------------------------------------------------------------------

def _recordings() -> list[pathlib.Path]:
    return sorted(RECORDINGS.glob("*.json"))


@pytest.mark.parametrize("path", _recordings(), ids=lambda path: path.stem)
def test_a_recorded_answer_fits_the_shape_it_claims(path):
    recording = json.loads(path.read_text(encoding="utf-8"))
    validator = grid_protocol.validator(recording["schema"], recording.get("definition"))

    errors = sorted(validator.iter_errors(recording["body"]), key=lambda error: list(error.path))

    assert not errors, "\n".join(f"{list(error.path)}: {error.message}" for error in errors[:10])
    body = recording["body"]
    envelope = body.get("error") if isinstance(body.get("error"), dict) else {}
    code = body.get("code") or envelope.get("code")
    if code in REFUSALS:
        assert recording["status"] == REFUSALS[code], f"{code} came with {recording['status']}"


def test_every_answer_shape_has_a_recording_or_a_stated_reason():
    recorded = {(json.loads(path.read_text())["schema"], json.loads(path.read_text()).get("definition"))
                for path in _recordings()}
    answers = set()
    for name in grid_protocol.schema_names():
        schema = grid_protocol.load_schema(name)
        answers.add((name, None))
        answers |= {(name, definition) for definition in schema.get("$defs", {})}
    nested = {  # definitions only ever met inside another answer, which its recording covers
        ("overview", "Answered"), ("overview", "OverviewModel"), ("overview", "OverviewNode"),
        ("overview", "NodeModelCapability"), ("overview", "NodeAnswered"), ("discover", "DiscoveredProvider"),
        ("discover", "DiscoveredCapabilities"), ("discover", "DiscoveredModel"), ("last-known", "LastKnownNode"),
        ("models", "ModelEntry"), ("last-known", None),
    }

    missing = answers - recorded - set(NOT_YET_RECORDED) - NOT_ANSWERS - nested

    assert not missing, f"answers with no recording and no stated reason: {sorted(missing, key=str)}"


_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_NODE_ID = re.compile(r"grid-[0-9a-f]{32}")
#: The sanitizer numbers what it replaces from 1; a real node id is 128 random bits.
_MAX_PLACEHOLDER_ID = 0xFFFF


def test_a_recording_is_sanitized():
    # The public repository must never carry a person's address, a machine's own name, a token or an address
    # of ours — anywhere in the file, `source` included.
    for path in _recordings():
        text = path.read_text(encoding="utf-8")
        assert json.loads(text).get("sanitized") is True, f"{path.name} does not say it was sanitized"
        for marker in ("lga_sk_", "Bearer ", "eyJ"):
            assert marker not in text, f"{path.name} carries {marker!r}"
        assert not _IPV4.search(text), f"{path.name} carries an IP address: {_IPV4.search(text).group(0)}"
        strangers = [email for email in _EMAIL.findall(text) if not email.endswith("@example.com")]
        assert not strangers, f"{path.name} carries a real address"
        real_ids = [node for node in _NODE_ID.findall(text) if int(node[5:], 16) > _MAX_PLACEHOLDER_ID]
        assert not real_ids, f"{path.name} carries a node id the sanitizer did not replace"
