"""Round-trip contract test: the MLX rollout server's responses parsed by the trainer's own
client. A fake engine stands in for MLX (this repo's CI is not Apple Silicon) — what's under
test is the wire contract both sides must agree on, which is where a break would hide."""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from train.config import RolloutConfig
from train.mlx.rollout_server import build_app
from train.rollout import GridRolloutClient, probe_endpoint


class FakeEngine:
    """Deterministic stand-in with the same interface MlxEngine exposes."""

    model_id = "fake/SmolLM2-135M-Instruct"

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, int, float]] = []
        self.adapters: list[str] = []

    def generate(self, prompt: str, n: int, max_tokens: int, temperature: float):
        self.calls.append((prompt, n, max_tokens, temperature))
        # Greedy (temperature 0) returns identical samples; sampling varies per index.
        return [
            ([100 + (0 if temperature == 0 else i), 200], [-0.5, -0.25], f"out{0 if temperature == 0 else i}")
            for i in range(n)
        ]

    def reload_adapter(self, adapter_dir: str) -> str:
        if "missing" in adapter_dir:
            raise FileNotFoundError(f"no adapters.safetensors in {adapter_dir}")
        self.adapters.append(adapter_dir)
        return "applied"


@pytest.fixture
def engine_and_client():
    engine = FakeEngine()
    server = TestClient(build_app(engine))
    # Route the trainer's httpx client into the in-process app: this is the real client code
    # (headers, body shape, parsing) talking to the real server code.
    return engine, httpx.MockTransport(lambda request: _forward(server, request))


def _forward(server: TestClient, request: httpx.Request) -> httpx.Response:
    response = server.request(
        request.method, request.url.path, content=request.content, headers=dict(request.headers)
    )
    return httpx.Response(
        response.status_code,
        content=response.content,
        headers={"content-type": "application/json"},
    )


def _client(transport, **cfg_kwargs):
    return GridRolloutClient(
        RolloutConfig(base_url="http://mac.local:8080/v1", **cfg_kwargs),
        FakeEngine.model_id,
        transport=transport,
    )


def test_round_trip_group_sampling(engine_and_client):
    engine, transport = engine_and_client
    client = _client(transport, max_tokens=40)
    rollouts = client.generate_group("Reverse the words: red house tree", 3)
    client.close()
    assert len(rollouts) == 3
    # Token ids survived the "token_id:<int>" encoding intact — the contract's whole point.
    assert rollouts[0].token_ids == (100, 200)
    assert rollouts[2].token_ids == (102, 200)
    assert rollouts[0].logprobs == (-0.5, -0.25)
    assert rollouts[1].text == "out1"
    _prompt, n, max_tokens, temperature = engine.calls[0]
    assert (n, max_tokens, temperature) == (3, 40, 1.0)


def test_round_trip_greedy_for_eval(engine_and_client):
    engine, transport = engine_and_client
    client = _client(transport)
    client.generate_group("p", 1, temperature=0.0)
    client.close()
    assert engine.calls[0][3] == 0.0  # eval path asks for greedy


def test_probe_endpoint_accepts_this_server(engine_and_client):
    _, transport = engine_and_client
    result = probe_endpoint(RolloutConfig(base_url="http://mac.local:8080/v1"),
                            FakeEngine.model_id, transport=transport)
    assert result["ok"] is True
    assert "2 tokens" in result["detail"]


def test_server_rejects_requests_without_the_training_fields():
    engine = FakeEngine()
    server = TestClient(build_app(engine))
    # A plain chat-style completion request must fail loudly, not return untrainable text.
    response = server.post("/v1/completions", json={"prompt": "hi", "n": 1})
    assert response.status_code == 400
    assert "return_tokens_as_token_ids" in response.text
    assert engine.calls == []


def test_server_validates_prompt_and_n():
    server = TestClient(build_app(FakeEngine()))
    base = {"logprobs": 1, "return_tokens_as_token_ids": True}
    assert server.post("/v1/completions", json={**base, "prompt": ""}).status_code == 400
    assert server.post("/v1/completions", json={**base, "prompt": "p", "n": 99}).status_code == 400


def test_models_endpoint_makes_grid_join_detect_it():
    server = TestClient(build_app(FakeEngine()))
    body = server.get("/v1/models").json()
    # `grid join` probes MLX on port 8080 via GET /v1/models -> data[].id
    assert body["data"][0]["id"] == FakeEngine.model_id


def test_reload_adapter_ok_and_missing():
    engine = FakeEngine()
    server = TestClient(build_app(engine))
    ok = server.post("/reload_adapter", json={"adapter_dir": "/tmp/run/adapters"})
    assert ok.status_code == 200 and ok.json() == {"status": "applied"}
    assert engine.adapters == ["/tmp/run/adapters"]
    assert server.post("/reload_adapter", json={"adapter_dir": "/tmp/missing"}).status_code == 404
    assert server.post("/reload_adapter", json={}).status_code == 400
