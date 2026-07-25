"""Unit tests for the `grid train` plane's torch-free half: config parsing, the rollout
contract (ids+logprobs, fail-closed), the TRL adapter's grouping, and deploy result handling.
All HTTP goes through httpx.MockTransport — no network, no ML deps."""
from __future__ import annotations

import json

import httpx
import pytest

from train.config import load_config
from train.deploy import deploy_adapter
from train.rollout import (
    GridRolloutClient,
    build_rollout_func,
    parse_completion_choice,
    probe_endpoint,
)

MINIMAL_CONFIG = """
[model]
name = "Qwen/Qwen3-4B-Instruct-2507"

[rollout]
base_url = "http://127.0.0.1:8000/v1"

[data]
prompts_jsonl = "{prompts}"

[rewards]
python_file = "{rewards}"
"""


def _write_config(tmp_path, body: str):
    prompts = tmp_path / "tasks.jsonl"
    prompts.write_text('{"prompt": "hello"}\n', encoding="utf-8")
    rewards = tmp_path / "rewards.py"
    rewards.write_text(
        "def reward_length(prompts, completions=None, **kw):\n"
        "    return [float(len(c)) for c in completions]\n",
        encoding="utf-8",
    )
    path = tmp_path / "grid-train.toml"
    path.write_text(
        body.format(prompts=prompts.as_posix(), rewards=rewards.as_posix()), encoding="utf-8"
    )
    return path


def test_load_config_minimal(tmp_path):
    cfg = load_config(_write_config(tmp_path, MINIMAL_CONFIG))
    assert cfg.model_name == "Qwen/Qwen3-4B-Instruct-2507"
    assert cfg.rollout_model == cfg.model_name  # defaults to the trainer model
    assert cfg.trainer.group_size == 8


def test_load_config_rejects_unknown_key(tmp_path):
    bad = MINIMAL_CONFIG + "\n[trainer]\ngroup_sise = 4\n"
    with pytest.raises(SystemExit, match="unknown key"):
        load_config(_write_config(tmp_path, bad))


def test_load_config_requires_reward_source(tmp_path):
    prompts = tmp_path / "tasks.jsonl"
    prompts.write_text('{"prompt": "hello"}\n', encoding="utf-8")
    path = tmp_path / "grid-train.toml"
    path.write_text(
        f'[model]\nname = "m"\n[rollout]\nbase_url = "http://x/v1"\n'
        f'[data]\nprompts_jsonl = "{prompts.as_posix()}"\n',
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="rewards"):
        load_config(path)


def _choice(token_ids, logprobs, text="ok", index=0):
    return {
        "index": index,
        "text": text,
        "logprobs": {
            "tokens": [f"token_id:{t}" for t in token_ids],
            "token_logprobs": logprobs,
        },
    }


def test_parse_completion_choice_happy():
    rollout = parse_completion_choice(_choice([1, 2, 3], [-0.1, -0.2, -0.3]))
    assert rollout.token_ids == (1, 2, 3)
    assert rollout.logprobs == (-0.1, -0.2, -0.3)


def test_parse_completion_choice_rejects_plain_tokens():
    choice = {"text": "hi", "logprobs": {"tokens": ["hi"], "token_logprobs": [-0.5]}}
    with pytest.raises(SystemExit, match="token ids"):
        parse_completion_choice(choice)


def test_parse_completion_choice_rejects_missing_logprobs():
    with pytest.raises(SystemExit, match="no logprobs"):
        parse_completion_choice({"text": "hi", "logprobs": None})


def _completions_transport(recorder=None):
    """Fake vLLM /completions honouring n + return_tokens_as_token_ids."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if recorder is not None:
            recorder.append((dict(request.headers), body))
        n = body.get("n", 1)
        choices = [_choice([10 + i, 20 + i], [-0.5, -0.25], text=f"c{i}", index=i) for i in range(n)]
        return httpx.Response(200, json={"choices": choices})

    return httpx.MockTransport(handler)


def _rollout_cfg(**overrides):
    from train.config import RolloutConfig

    return RolloutConfig(base_url="http://grid.test/v1", **overrides)


def test_generate_group_orders_and_sizes():
    client = GridRolloutClient(_rollout_cfg(), "m", transport=_completions_transport())
    rollouts = client.generate_group("p", 3)
    assert [r.text for r in rollouts] == ["c0", "c1", "c2"]
    assert rollouts[1].token_ids == (11, 21)
    client.close()


def test_target_provider_header_sent():
    seen: list = []
    client = GridRolloutClient(
        _rollout_cfg(target_provider="node-01"), "m", transport=_completions_transport(seen)
    )
    client.generate_group("p", 1)
    client.close()
    headers, _ = seen[0]
    assert headers.get("x-target-provider") == "node-01"


def test_probe_endpoint_fail_closed_on_http_error():
    transport = httpx.MockTransport(lambda req: httpx.Response(400, text="tools unsupported"))
    result = probe_endpoint(_rollout_cfg(), "m", transport=transport)
    assert result["ok"] is False
    assert "400" in result["detail"]


def test_probe_endpoint_ok():
    result = probe_endpoint(_rollout_cfg(), "m", transport=_completions_transport())
    assert result["ok"] is True


class _FakeTokenizer:
    def __call__(self, text, add_special_tokens=False):
        return {"input_ids": [len(text)]}


def test_rollout_func_coalesces_repeated_prompts(monkeypatch):
    seen: list = []
    transport = _completions_transport(seen)
    monkeypatch.setattr(
        "train.rollout.GridRolloutClient",
        lambda cfg, model: GridRolloutClient(cfg, model, transport=transport),
    )
    func = build_rollout_func(_rollout_cfg(), "m", _FakeTokenizer())
    out = func(["aa", "aa", "bb"])  # TRL passes prompts repeated per generation
    func.close()
    # Two requests: n=2 for "aa", n=1 for "bb" — the group goes to the engine whole.
    assert [b["n"] for _, b in seen] == [2, 1]
    assert len(out["prompt_ids"]) == 3
    assert out["prompt_ids"][0] == [2] and out["prompt_ids"][2] == [2]
    assert out["completions_text"] == ["c0", "c1", "c0"]
    assert all(len(ids) == 2 for ids in out["completion_ids"])


def _adapter_dir(tmp_path):
    d = tmp_path / "adapter"
    d.mkdir()
    (d / "adapter_config.json").write_text("{}", encoding="utf-8")
    return d


def test_deploy_reports_per_node(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/unload_lora_adapter":
            return httpx.Response(404)  # nothing loaded yet — must be ignored
        return httpx.Response(200, json={})

    results = deploy_adapter(
        _adapter_dir(tmp_path),
        ["http://a.test/v1"],
        "climb-1",
        transport=httpx.MockTransport(handler),
    )
    assert results == [{"node": "http://a.test/v1", "ok": True, "detail": "serving as 'climb-1'"}]


def test_deploy_explains_missing_runtime_lora(tmp_path):
    transport = httpx.MockTransport(lambda req: httpx.Response(404))
    results = deploy_adapter(
        _adapter_dir(tmp_path), ["http://a.test/v1"], "climb-1", transport=transport
    )
    assert results[0]["ok"] is False
    assert "VLLM_ALLOW_RUNTIME_LORA_UPDATING" in results[0]["detail"]


def test_deploy_rejects_non_adapter(tmp_path):
    with pytest.raises(SystemExit, match="adapter_config.json"):
        deploy_adapter(tmp_path, ["http://a.test/v1"], "climb-1")


def test_split_holdout_seeded_and_bounded():
    from train.run import split_holdout

    prompts = [f"p{i}" for i in range(100)]
    train_a, eval_a = split_holdout(prompts)
    train_b, eval_b = split_holdout(prompts)
    assert (train_a, eval_a) == (train_b, eval_b)  # seeded: same split every run
    assert len(eval_a) == 10 and len(train_a) == 90
    assert not set(train_a) & set(eval_a)
    # Tiny datasets keep at least half for training.
    train_c, eval_c = split_holdout([f"p{i}" for i in range(6)])
    assert len(eval_c) == 3 and len(train_c) == 3
