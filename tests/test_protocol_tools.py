"""The tools that make `protocol/recordings/` — so a recording can be made again from this repository.

`protocol/tools/sanitize.py` replaces what identifies a person, a machine or our infrastructure in an answer while
keeping its shape; `protocol/tools/record_reads.py` takes one credential-less read from a grid and writes it as a
recording, validated and sanitized. The recordings already committed must be what the sanitizer makes of them: if
sanitizing one again changes it, the tool and the recordings have drifted apart.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import httpx
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOLS = ROOT / "protocol" / "tools"
RECORDINGS = ROOT / "protocol" / "recordings"

REAL_NODE = "grid-0675f6a9e1bd84c5071049c4a2dc1553"
OTHER_NODE = "grid-9f3a5c0e7b2d4e6f8a1b3c5d7e9f0a2b"


def _tool(name: str):
    spec = importlib.util.spec_from_file_location(f"protocol_tools_{name}", TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sanitize = _tool("sanitize")
record_reads = _tool("record_reads")


def _overview() -> dict:
    return {
        "nodes": [
            {"name": "alices-laptop", "provider_email": "owner@some-company.test", "device": "Cloud-Premium-Intel",
             "models": ["qwen3.5-2b"], "engine": "llama.cpp"},
            {"name": "gpu-box", "provider_email": None, "device": "", "models": [], "engine": "vllm"},
        ],
        "models": [{"id": "qwen3.5-2b", "name": "Qwen 3.5 2B"}],
    }


def _discovery() -> dict:
    route = f"provider:{REAL_NODE}:NTWj54ka9e-2"
    return {"providers": [{
        "node_id": REAL_NODE, "models": [route], "endpoint_url": "http://10.0.0.5:8080",
        "meta": {"name": "alices-laptop", "device": "Cloud-Premium-Intel"},
        "capabilities": {"models": {route: {"raw_model_id": "Qwen3.5-2B", "public_model_id": route,
                                            "provider_group_id": "provider-group:NTWj54ka9e-2"}}},
    }]}


# ---- the sanitizer ------------------------------------------------------------------------------------------

def test_a_machine_name_an_address_and_a_device_are_replaced_and_the_shape_is_kept():
    out = sanitize.Sanitizer().body(_overview())

    assert [node["name"] for node in out["nodes"]] == ["node-a", "node-b"]
    assert out["nodes"][0]["provider_email"] == "provider@example.com"
    assert out["nodes"][1]["provider_email"] is None
    assert out["nodes"][0]["device"] == "example-device"
    assert out["nodes"][1]["device"] == ""
    assert out["models"] == [{"id": "qwen3.5-2b", "name": "Qwen 3.5 2B"}], "a model's public name is not a secret"


def test_a_node_id_is_replaced_everywhere_it_appears_and_always_by_the_same_placeholder():
    out = sanitize.Sanitizer().body(_discovery())
    text = json.dumps(out)

    assert REAL_NODE not in text and "NTWj54ka9e-2" not in text
    provider = out["providers"][0]
    route = provider["models"][0]
    assert provider["node_id"] == "grid-" + "0" * 31 + "1"
    assert route == f"provider:{provider['node_id']}:model0000001"
    assert list(provider["capabilities"]["models"]) == [route], "a route id used as a key is replaced too"
    assert provider["capabilities"]["models"][route]["provider_group_id"] == "provider-group:model0000001"
    assert provider["meta"] == {"name": "node-a", "device": "example-device"}
    assert provider["endpoint_url"] == "https://provider.example.com"
    assert provider["capabilities"]["models"][route]["raw_model_id"] == "Qwen3.5-2B"


def test_two_machines_get_two_placeholders():
    body = {"providers": [{"node_id": REAL_NODE}, {"node_id": OTHER_NODE}, {"node_id": REAL_NODE}]}

    ids = [provider["node_id"] for provider in sanitize.Sanitizer().body(body)["providers"]]

    assert ids[0] == ids[2] != ids[1]


def test_sanitizing_twice_changes_nothing_more():
    once = sanitize.Sanitizer().body(_discovery())

    assert sanitize.Sanitizer().body(once) == once


def test_the_answer_given_is_not_changed_in_place():
    answer = _overview()
    before = json.dumps(answer, sort_keys=True)

    sanitize.Sanitizer().body(answer)

    assert json.dumps(answer, sort_keys=True) == before


@pytest.mark.parametrize("leak", ["reached 203.0.113.7 first", "Bearer abc.def", "lga_sk_0123456789", "eyJhbGciOi"])
def test_a_value_it_cannot_safely_replace_stops_the_recording(leak):
    # Fail closed: an IP address or a credential has no placeholder that keeps the answer honest, so a person looks.
    with pytest.raises(sanitize.Unsafe):
        sanitize.Sanitizer().body({"detail": leak})


def test_a_recording_is_marked_sanitized():
    recording = {"schema": "overview", "definition": None, "status": 200, "body": _overview()}

    out = sanitize.Sanitizer().recording(recording)

    assert out["sanitized"] is True and out["body"]["nodes"][0]["name"] == "node-a"


@pytest.mark.parametrize("path", sorted(RECORDINGS.glob("*.json")), ids=lambda path: path.stem)
def test_every_committed_recording_is_what_the_sanitizer_makes_of_it(path):
    recording = json.loads(path.read_text(encoding="utf-8"))

    assert sanitize.Sanitizer().recording(recording) == recording


# ---- recording a read ---------------------------------------------------------------------------------------

def _grid(answer: httpx.Response, seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer
    return httpx.MockTransport(handler)


def test_a_read_is_taken_without_a_credential_validated_sanitized_and_written(tmp_path):
    seen: list[httpx.Request] = []
    answer = httpx.Response(503, json={"detail": "resting", "code": "grid_asleep", "last_known": {
        "age_seconds": 12, "nodes": [{"name": "alices-laptop", "engine": "llama.cpp", "models": ["m"]}], "ids": ["M"]}})

    written = record_reads.record(
        "https://grid.example/g1/relay/v1/grid/overview", schema="refusal", definition="GridAsleep",
        stem="refusal--GridAsleep--case", source="a test", out=tmp_path, transport=_grid(answer, seen))

    (request,) = seen
    assert "authorization" not in request.headers and "x-api-key" not in request.headers
    recording = json.loads(written.read_text())
    assert written.name == "refusal--GridAsleep--case.json"
    assert recording["status"] == 503 and recording["sanitized"] is True
    assert recording["body"]["last_known"]["nodes"][0]["name"] == "node-a"
    assert recording["request"] == "GET /relay/v1/grid/overview"


def test_an_answer_that_is_not_the_named_shape_is_not_written(tmp_path):
    answer = httpx.Response(503, json={"detail": "resting", "code": "grid_sleeping"})

    with pytest.raises(record_reads.NotTheShape, match="GridAsleep"):
        record_reads.record("https://grid.example/g1/nodes/discover", schema="refusal", definition="GridAsleep",
                            stem="x", source="a test", out=tmp_path, transport=_grid(answer, []))

    assert list(tmp_path.iterdir()) == []


def test_a_coded_refusal_with_the_wrong_status_is_not_written(tmp_path):
    answer = httpx.Response(500, json={"detail": "resting", "code": "grid_asleep"})

    with pytest.raises(record_reads.NotTheShape, match="503"):
        record_reads.record("https://grid.example/g1/nodes/discover", schema="refusal", definition="GridAsleep",
                            stem="x", source="a test", out=tmp_path, transport=_grid(answer, []))


def test_an_address_carrying_a_credential_is_refused(tmp_path):
    with pytest.raises(ValueError, match="credential"):
        record_reads.record("https://user:secret@grid.example/g1/nodes/discover", schema="discover", definition=None,
                            stem="x", source="a test", out=tmp_path, transport=_grid(httpx.Response(200), []))
