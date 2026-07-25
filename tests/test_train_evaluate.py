"""Eval-gate tests: scoring on held-out work, the verdict rules, and the card.

The gate's job is to say no, so most of these check that it says no for the right reasons.
"""
from __future__ import annotations

import json

import httpx
import pytest

from train.config import DataConfig, RewardsConfig, RolloutConfig, TrainRunConfig
from train.evaluate import (
    Scored,
    card_html,
    compare,
    load_eval_prompts,
    run_eval,
    score_model,
)


def _cfg(tmp_path, rewards_body: str) -> TrainRunConfig:
    rewards = tmp_path / "rewards.py"
    rewards.write_text(rewards_body, encoding="utf-8")
    prompts = tmp_path / "p.jsonl"
    prompts.write_text('{"prompt": "x"}\n', encoding="utf-8")
    return TrainRunConfig(
        model_name="base/model",
        rollout=RolloutConfig(base_url="http://grid.test/v1"),
        data=DataConfig(prompts_jsonl=str(prompts)),
        rewards=RewardsConfig(python_file=str(rewards)),
        trainer=__import__("train.config", fromlist=["TrainerConfig"]).TrainerConfig(),
        deploy=__import__("train.config", fromlist=["DeployConfig"]).DeployConfig(),
        source_path=tmp_path / "grid-train.toml",
    )


def _run_dir(tmp_path, prompts=("alpha", "beta", "gamma")):
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    (run / "eval_prompts.jsonl").write_text(
        "".join(json.dumps({"prompt": p}) + "\n" for p in prompts), encoding="utf-8"
    )
    return run


def _transport(answers: dict[str, str]):
    """Fake endpoint: the reply depends on which model was asked, so before/after differ."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        text = answers.get(body["model"], "")
        return httpx.Response(200, json={"choices": [{
            "index": 0, "text": text,
            "logprobs": {"tokens": ["token_id:1"], "token_logprobs": [-0.1]},
        }]})

    return httpx.MockTransport(handler), seen


def test_load_eval_prompts_and_missing_file(tmp_path):
    run = _run_dir(tmp_path)
    assert load_eval_prompts(run) == ["alpha", "beta", "gamma"]
    (run / "eval_prompts.jsonl").unlink()
    with pytest.raises(SystemExit, match="no held-out set"):
        load_eval_prompts(run)


def test_score_model_uses_greedy_and_reports_per_grader(tmp_path):
    cfg = _cfg(tmp_path, "def reward_len(prompts, completions=None, **kw):\n"
                         "    return [min(len(c) / 10, 1.0) for c in completions]\n")
    from train.rewards import load_reward_funcs

    transport, seen = _transport({"m": "0123456789"})
    scored = score_model(cfg, "m", ["a", "b"], load_reward_funcs(cfg.rewards, cfg.data),
                         transport=transport)
    assert scored.n == 2
    assert scored.per_grader == {"len": 1.0}
    assert scored.overall == 1.0
    assert all(body["temperature"] == 0.0 for body in seen)  # deterministic comparison
    assert scored.samples[0]["completion"] == "0123456789"


def _scored(overall, per, name="m"):
    return Scored(model=name, n=10, per_grader=per, overall=overall, samples=[])


def test_compare_passes_on_real_gain():
    result = compare(_scored(0.50, {"a": 0.5}), _scored(0.70, {"a": 0.7}))
    assert result["passed"] is True
    assert result["delta"] == 0.2
    assert "better by" in result["verdict"]


def test_compare_blocks_noise():
    result = compare(_scored(0.500, {"a": 0.5}), _scored(0.505, {"a": 0.505}))
    assert result["passed"] is False
    assert "no meaningful gain" in result["verdict"]


def test_compare_blocks_a_regression_even_when_overall_rises():
    """The failure mode this exists for: tidier but less correct."""
    before = _scored(0.50, {"correct": 0.60, "format": 0.40})
    after = _scored(0.60, {"correct": 0.40, "format": 0.80})
    result = compare(before, after)
    assert result["passed"] is False
    assert result["regressions"] == ["correct"]
    assert "correct" in result["verdict"]


def test_run_eval_writes_both_card_files_and_verdict(tmp_path):
    cfg = _cfg(tmp_path, "def reward_hit(prompts, completions=None, **kw):\n"
                         "    return [1.0 if 'good' in c else 0.0 for c in completions]\n")
    run = _run_dir(tmp_path)
    transport, _ = _transport({"base/model": "bad answer", "candidate": "good answer"})
    result = run_eval(cfg, run, "candidate", transport=transport)
    assert result["passed"] is True
    assert result["before"]["per_grader"]["hit"] == 0.0
    assert result["after"]["per_grader"]["hit"] == 1.0
    assert (run / "eval-card.json").is_file()
    card = (run / "eval-card.html").read_text(encoding="utf-8")
    assert "ready to serve" in card
    assert "good answer" in card  # the qualitative half: real answers, side by side


def test_card_html_escapes_and_marks_failure():
    result = compare(_scored(0.8, {"a": 0.8}), _scored(0.4, {"a": 0.4}))
    result["before"]["samples"] = [{"prompt": "<script>x</script>", "completion": "a & b", "scores": {}}]
    result["after"]["samples"] = [{"prompt": "<script>x</script>", "completion": "c", "scores": {}}]
    page = card_html(result, "run-1")
    assert "not ready" in page
    assert "<script>x</script>" not in page  # escaped
    assert "&lt;script&gt;" in page
