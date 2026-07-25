"""Does the REAL server contract (build_app + _served) drop <name> after a staged load?"""
from __future__ import annotations

from fastapi.testclient import TestClient

from train.mlx.rollout_server import build_app


class FakeMlx:
    """MlxEngine's exact name semantics (see MlxEngine.served_names / reload_adapter)."""
    def __init__(self):
        self.model_id = "mlx-community/SmolLM2-135M-Instruct"
        self.adapter_name = "support-v1"
        self.weights = "night-1"

    def served_names(self):
        return tuple(n for n in (self.model_id, self.adapter_name) if n)

    def reload_adapter(self, adapter_dir: str, name: str = "") -> str:
        self.weights = adapter_dir
        self.adapter_name = name or adapter_dir
        return "applied"

    def generate(self, prompt, n, max_tokens, temperature):
        return [(list(range(3)), [-0.1, -0.1, -0.1], self.weights)]


def test_served_names_after_staged_load():
    e = FakeMlx()
    c = TestClient(build_app(e))
    body = {"model": "support-v1", "prompt": "hi", "logprobs": 1,
            "return_tokens_as_token_ids": True}
    print("before:", c.post("/v1/completions", json=body).status_code, e.served_names())
    c.post("/reload_adapter", json={"adapter_dir": "/tmp/night-2", "name": "support-v1-candidate"})
    r = c.post("/v1/completions", json=body)
    print("after :", r.status_code, r.json(), e.served_names())
    assert r.status_code == 404
