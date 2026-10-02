"""The harness daemon's own reads, replayed against the master behind the proxy (grid-platform ticket 13).

The harness polls every grid a person can see with two CREDENTIAL-LESS reads, the overview and provider discovery
(harness `cli/src/lib/gridReader.ts`), and reads `code` and `last_known` off the proxy's refusal. Its types are
generated from `grid-protocol`'s schemas (ticket 12), so this driver checks every answer against the same schemas: no
harness checkout is needed, and a field the server renames fails here, not in somebody's model list.

Every refusal code a client branches on is driven too, with its status, and none of these reads may wake a grid.
"""
from __future__ import annotations

import time

import grid_protocol
import httpx
import pytest
from grid_protocol.constants import (
    FEATURE_RETIRED_CODE,
    GRID_ASLEEP_CODE,
    GRID_DELETED_CODE,
    GRID_MASTER_DOWN_CODE,
    GRID_STOPPED_CODE,
    NO_PROVIDERS_AVAILABLE_CODE,
    REFUSAL_STATUS,
)

from conformance.clients import Client, harness_pin, install
from conformance.stack import ENGINE_MODEL, INFERENCE_SCOPES, PROVIDER_SCOPES, Stack

READS = {"overview": "relay/v1/grid/overview", "discover": "nodes/discover"}
RECORD = {"nodes": [{"name": "conformance-sleeper", "engine": "vllm", "models": [ENGINE_MODEL]}], "ids": [ENGINE_MODEL]}


def _get(stack: Stack, read: str) -> httpx.Response:
    return httpx.get(f"{stack.grid_url}/{READS[read]}", timeout=30, headers={"User-Agent": "autonomous-harness/conformance (read)"})


@pytest.fixture
def provider(stack, tmp_path):
    """An engine joined with the harness's pinned `grid`, so the awake answers have a node and a model to describe.
    Per test: a test of a deleted grid ends an engine, and none may depend on running after it."""
    version = harness_pin()
    joined = Client(version, install(version), tmp_path / "provider")
    joined.sign_in(stack, token=stack.provider_token(), roles=["consumer", "provider"],
                   scopes=INFERENCE_SCOPES + PROVIDER_SCOPES)
    done = joined.run("join", stack.network_id, "--at", stack.engine_url, "-m", ENGINE_MODEL, "--name", "harness-view",
                      timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    yield joined
    stack.set_state("running")
    joined.run("leave", stack.network_id, timeout=60)


@pytest.mark.parametrize("read, schema", [("overview", "overview"), ("discover", "discover")])
def test_an_awake_grid_answers_in_the_shape_the_harness_reads(stack, provider, read, schema):
    deadline = time.monotonic() + 60
    while True:
        answer = _get(stack, read)
        assert answer.status_code == 200, answer.text
        body = answer.json()
        listed = body.get("nodes") if read == "overview" else body.get("providers")
        if listed or time.monotonic() > deadline:
            break
        time.sleep(1)

    grid_protocol.validator(schema).validate(body)
    assert listed, f"the joined engine never appeared in the {read}"
    if read == "overview":
        assert any(ENGINE_MODEL in (node.get("models") or []) for node in body["nodes"])
        # The harness's model list is the overview's top-level `models[].id` (gridReader.ts), not the nodes'.
        assert ENGINE_MODEL in {entry.get("id") for entry in body.get("models") or []}, body.get("models")
    else:
        # Discovery lists ROUTE ids; the harness reads the model's own name off its capabilities (gridReader.ts).
        raw = {entry.get("raw_model_id") for p in body["providers"]
               for entry in ((p.get("capabilities") or {}).get("models") or {}).values()}
        assert ENGINE_MODEL in raw, body


@pytest.mark.parametrize("read", sorted(READS))
def test_an_asleep_grid_refuses_with_grid_asleep_and_is_not_woken(stack, read):
    stack.set_state("asleep", last_known=RECORD)

    answer = _get(stack, read)

    assert answer.status_code == REFUSAL_STATUS[GRID_ASLEEP_CODE], answer.text
    body = answer.json()
    grid_protocol.validator("refusal", "GridAsleep").validate(body)
    assert body["code"] == GRID_ASLEEP_CODE
    if read == "overview":
        grid_protocol.validator("last-known").validate(body["last_known"])
        assert body["last_known"]["ids"] == RECORD["ids"]
    assert stack.state()["state"] == "asleep", "a credential-less read must never wake a grid"


@pytest.mark.parametrize("state, code, definition", [
    ("stopped", GRID_STOPPED_CODE, "GridStopped"),
    ("deleted", GRID_DELETED_CODE, "GridDeleted"),
])
def test_a_grid_that_is_not_up_answers_its_code(stack, state, code, definition):
    stack.set_state(state)

    answer = _get(stack, "overview")

    assert answer.status_code == REFUSAL_STATUS[code], answer.text
    body = answer.json()
    grid_protocol.validator("refusal", definition).validate(body)
    assert body["code"] == code
    assert stack.state()["state"] == state


def test_a_running_grid_whose_master_is_down_answers_grid_master_down(stack):
    with stack.master_held_down():
        answer = _get(stack, "overview")

    assert answer.status_code == REFUSAL_STATUS[GRID_MASTER_DOWN_CODE], answer.text
    body = answer.json()
    grid_protocol.validator("refusal", "GridMasterDown").validate(body)
    assert body["code"] == GRID_MASTER_DOWN_CODE


@pytest.mark.parametrize("path", ["relay/v1/media/jobs", "relay/v1/tasks", "relay/v1/projects", "relay/v1/git/p/info/refs"])
def test_a_retired_feature_is_refused_by_the_proxy(stack, path):
    answer = httpx.get(f"{stack.grid_url}/{path}", timeout=30)

    assert answer.status_code == REFUSAL_STATUS[FEATURE_RETIRED_CODE], answer.text
    body = answer.json()
    grid_protocol.validator("refusal", "FeatureRetired").validate(body)
    assert body["code"] == FEATURE_RETIRED_CODE


@pytest.mark.parametrize("openai_errors", [True, False], ids=["openai-envelope", "detail"])
def test_no_engine_for_the_model_is_no_providers_available(stack, openai_errors):
    """The relay's own refusal. In OpenAI's envelope, with its `code`, when the caller asked for OpenAI errors (the
    error-format header; an `lga_sk_` key does too); otherwise `{"detail": …}` with no code (`grid-protocol`
    `openai-error`)."""
    headers = {"Authorization": f"Bearer {stack.consumer_token()}"}
    if openai_errors:
        headers["X-Error-Format"] = "openai"
    answer = httpx.post(f"{stack.grid_url}/relay/v1/chat/completions", timeout=60, headers=headers,
                        json={"model": "conformance-nobody-serves-this", "messages": [{"role": "user", "content": "x"}]})

    assert answer.status_code == REFUSAL_STATUS[NO_PROVIDERS_AVAILABLE_CODE], answer.text
    body = answer.json()
    if openai_errors:
        grid_protocol.validator("openai-error", "NoProvidersAvailable").validate(body)
        assert body["error"]["code"] == NO_PROVIDERS_AVAILABLE_CODE
    else:
        assert isinstance(body.get("detail"), str) and "error" not in body
