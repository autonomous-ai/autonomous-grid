"""The gate, against an engine that holds exactly one set of weights.

Grid's own MLX server is one of those: `reload_adapter` mutates the model in place and the adapter
name is only a label, so *loading anything replaces what the node was serving*. That is the
product's primary path — an all-Mac office — and it breaks two comfortable assumptions:

  * you cannot "stage" a candidate beside the incumbent, so the incumbent has to be scored FIRST,
    while it is still the thing the node holds;
  * a night that ends in a refusal has displaced the customer's model, so "nothing changed" is
    only true if the incumbent is put back.

Both were real defects, in a fix written to repair a different real defect. These tests drive the
whole path — deploy, sync, HTTP — against a stand-in with the MLX engine's exact semantics, so the
next rearrangement of this code has to keep meaning what it says.
"""
from __future__ import annotations

import json

import httpx
import pytest

from train.config import (
    DataConfig,
    DeployConfig,
    RewardsConfig,
    RolloutConfig,
    TrainerConfig,
    TrainRunConfig,
)
from train.evaluate import prove_candidate

PROMPTS = ["reverse: alpha beta", "reverse: gamma delta", "reverse: epsilon zeta"]
# What each set of weights answers, and what that answer is worth.
ANSWERS = {"base": "bad", "night-1": "ok", "night-2": "great"}
WORTH = {"bad": 0.0, "ok": 0.5, "great": 1.0}


class OneSlotEngine:
    """One model, one adapter slot, and the name is a label — MlxEngine's semantics."""

    def __init__(self) -> None:
        self.weights = "base"
        self.name = "mlx-community/SmolLM2-135M-Instruct"
        self.loads: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content or b"{}")
        if path.endswith(("/reload_adapter", "/load_lora_adapter")):
            adapter = body.get("adapter_dir") or body.get("lora_path") or ""
            name = body.get("name") or body.get("lora_name") or ""
            self.weights = (adapter.rstrip("/").rsplit("/", 1)[-1])   # the directory names them
            self.name = name or self.name
            self.loads.append((self.weights, self.name))
            return httpx.Response(200, json={"status": "applied"})
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": self.name}]})
        if path.endswith("/completions"):
            asked = body.get("model", "")
            if asked != self.name:            # fails closed on a name it does not hold
                return httpx.Response(404, json={"error": f"serving {self.name}, not {asked}"})
            text = ANSWERS[self.weights]      # ...but the weights are whatever was last loaded
            return httpx.Response(200, json={
                "choices": [{"text": text, "message": {"content": text},
                             "logprobs": {"tokens": ["token_id:1"], "token_logprobs": [-0.1]}}]})
        return httpx.Response(404, json={"error": path})


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("GRID_HOME", str(tmp_path / "home"))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "eval_prompts.jsonl").write_text(
        "".join(json.dumps({"prompt": p}) + "\n" for p in PROMPTS), encoding="utf-8")
    (tmp_path / "rewards.py").write_text(
        "WORTH = " + json.dumps(WORTH) + "\n"
        "def reward_quality(prompts, completions=None, completions_text=None, **kw):\n"
        "    texts = completions_text if completions_text is not None else completions\n"
        "    return [WORTH.get(t.strip(), 0.0) for t in texts]\n", encoding="utf-8")
    (tmp_path / "prompts.jsonl").write_text(
        "".join(json.dumps({"prompt": p}) + "\n" for p in PROMPTS), encoding="utf-8")

    def adapter(name: str):
        path = tmp_path / name
        path.mkdir(exist_ok=True)
        (path / "adapter_config.json").write_text("{}", encoding="utf-8")
        (path / "adapter_model.safetensors").write_text("weights", encoding="utf-8")
        (path / "adapters.safetensors").write_text("weights", encoding="utf-8")
        return path

    cfg = TrainRunConfig(
        model_name="mlx-community/SmolLM2-135M-Instruct",
        rollout=RolloutConfig(base_url="http://mac.local/v1"),
        data=DataConfig(prompts_jsonl=str(tmp_path / "prompts.jsonl")),
        rewards=RewardsConfig(python_file=str(tmp_path / "rewards.py")),
        trainer=TrainerConfig(output_dir=str(tmp_path / "out")),
        deploy=DeployConfig(nodes=("http://mac.local/v1",), adapter_name="support-v1"),
        source_path=tmp_path / "grid-train.toml",
    )
    return {"cfg": cfg, "run_dir": run_dir, "adapter": adapter, "tmp": tmp_path}


def test_a_real_improvement_is_not_scored_as_no_change(setup, monkeypatch):
    """The bug this file exists for: staging first made both sides the same weights, so a genuine
    gain measured exactly +0.000 — and with greedy decoding, every night, forever."""
    engine = OneSlotEngine()
    engine.weights, engine.name = "night-1", "support-v1"      # what customers use today
    transport = httpx.MockTransport(engine.handler)

    result = prove_candidate(setup["cfg"], setup["run_dir"], setup["adapter"]("night-2"),
                             "support-v1", transport=transport)
    assert result["before"]["overall"] == 0.5                  # the incumbent, measured as itself
    assert result["after"]["overall"] == 1.0                   # the candidate, measured as itself
    assert result["delta"] == 0.5 and result["passed"]


def test_the_incumbent_is_scored_before_anything_displaces_it(setup):
    engine = OneSlotEngine()
    engine.weights, engine.name = "night-1", "support-v1"
    transport = httpx.MockTransport(engine.handler)
    prove_candidate(setup["cfg"], setup["run_dir"], setup["adapter"]("night-2"), "support-v1",
                    transport=transport)
    # One load, and it happened after the incumbent had already been scored (proved by the
    # before-score being the incumbent's answer, above).
    assert [name for _, name in engine.loads] == ["support-v1-candidate"]


def test_a_refused_night_puts_the_customers_model_back(setup):
    """A one-slot engine is holding the rejected candidate when the check ends. "Nothing changed
    tonight" has to be true, not just printed."""
    from train.deploy import record_deploy

    engine = OneSlotEngine()
    engine.weights, engine.name = "night-1", "support-v1"
    transport = httpx.MockTransport(engine.handler)
    good = setup["adapter"]("night-1")
    record_deploy("support-v1", good)                          # what the ledger knows

    worse = setup["tmp"] / "base"                              # a candidate that scores lower
    worse.mkdir()
    (worse / "adapter_config.json").write_text("{}", encoding="utf-8")
    (worse / "adapters.safetensors").write_text("w", encoding="utf-8")

    result = prove_candidate(setup["cfg"], setup["run_dir"], worse, "support-v1",
                             transport=transport)
    assert not result["passed"]
    assert result["restored"] is True
    # The ledger keeps a COPY (run/adapter is overwritten by the next training run), so the
    # restored directory is that copy — what matters is that the weights are the incumbent's.
    assert engine.weights.startswith("2026") or engine.weights == "night-1"
    assert engine.name == "support-v1"                         # answering to its own name again


def test_when_it_cannot_be_put_back_the_verdict_says_so(setup):
    """No ledger entry — so the honest outcome is a refusal that names the problem, not silence."""
    engine = OneSlotEngine()
    engine.weights, engine.name = "night-1", "support-v1"
    transport = httpx.MockTransport(engine.handler)
    worse = setup["tmp"] / "base"
    worse.mkdir()
    (worse / "adapter_config.json").write_text("{}", encoding="utf-8")
    (worse / "adapters.safetensors").write_text("w", encoding="utf-8")

    result = prove_candidate(setup["cfg"], setup["run_dir"], worse, "support-v1",
                             transport=transport)
    assert not result["passed"] and result["restored"] is False
    assert "restart the engine or deploy support-v1" in result["verdict"]


def test_the_first_night_compares_against_the_base_model(setup):
    """Nothing trained is serving yet: the thing to beat is the model as it came."""
    engine = OneSlotEngine()          # name is the base id, weights are the base
    transport = httpx.MockTransport(engine.handler)
    result = prove_candidate(setup["cfg"], setup["run_dir"], setup["adapter"]("night-1"),
                             "support-v1", transport=transport)
    assert result["incumbent"] == "mlx-community/SmolLM2-135M-Instruct"
    assert result["before"]["overall"] == 0.0 and result["after"]["overall"] == 0.5
    assert result["passed"]


def test_a_node_that_will_not_load_it_is_not_a_pass(setup):
    engine = OneSlotEngine()
    engine.weights, engine.name = "night-1", "support-v1"

    def refuse(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(("reload_adapter", "load_lora_adapter")):
            return httpx.Response(500, json={"error": "out of memory"})
        return engine.handler(request)

    from train.evaluate import CandidateNotLoaded

    with pytest.raises(CandidateNotLoaded):
        prove_candidate(setup["cfg"], setup["run_dir"], setup["adapter"]("night-2"), "support-v1",
                        transport=httpx.MockTransport(refuse))
    assert engine.weights == "night-1"          # and the customer's model is untouched


def test_a_passing_check_leaves_the_customers_name_answering(setup):
    """A check is an observation. Restoring only on a refusal left the node answering to
    "<name>-candidate" after a PASS, so the name customers point at 404'd until someone pressed
    a button that might be hours away."""
    from train.deploy import record_deploy

    engine = OneSlotEngine()
    engine.weights, engine.name = "night-1", "support-v1"
    record_deploy("support-v1", setup["adapter"]("night-1"))
    transport = httpx.MockTransport(engine.handler)

    result = prove_candidate(setup["cfg"], setup["run_dir"], setup["adapter"]("night-2"),
                             "support-v1", transport=transport)
    assert result["passed"]                       # the candidate won...
    assert engine.name == "support-v1"            # ...and the node is still answering customers
    assert result["restored"] is True


def test_the_ledger_survives_the_run_directory_being_retrained(setup):
    """The ledger used to hold a path, and the path was run/adapter — overwritten by the next
    training run. "Restore what was serving" then re-served the model that had just lost."""
    from train.deploy import last_deployed, record_deploy

    live = setup["tmp"] / "run" / "adapter"
    live.mkdir(parents=True)
    (live / "adapter_config.json").write_text("{}", encoding="utf-8")
    (live / "adapters.safetensors").write_text("the good model", encoding="utf-8")
    record_deploy("support-v1", live)

    (live / "adapters.safetensors").write_text("tonight's worse model", encoding="utf-8")
    kept = last_deployed("support-v1")
    assert kept is not None and kept != live
    assert (kept / "adapters.safetensors").read_text(encoding="utf-8") == "the good model"
