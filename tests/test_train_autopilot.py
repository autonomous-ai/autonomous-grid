

# --- the gate has to be able to score tonight's work ---------------------------------------

def test_captured_examples_become_answer_keys_the_graders_can_use(tmp_path):
    """A grader with no reference returns 0.5 — for both models — so the gate can never pass.

    That is not a safe default, it is a silent ceiling: the night trains correctly and the model
    can never ship. Merging tonight's references into the graders' lookup is what fixes it.
    """
    import json

    from train.autopilot import merge_references

    rewards_dir, dataset = tmp_path / "workspace", tmp_path / "autopilot-data"
    rewards_dir.mkdir()
    dataset.mkdir()
    (rewards_dir / "refs.jsonl").write_text(
        json.dumps({"prompt": "from the original export", "reference": "old answer"}) + "\n",
        encoding="utf-8")
    (dataset / "refs.jsonl").write_text(
        json.dumps({"prompt": "captured last week", "reference": "what the human sent"}) + "\n"
        + json.dumps({"prompt": "from the original export", "reference": "corrected since"}) + "\n",
        encoding="utf-8")

    known = merge_references(rewards_dir, dataset)
    rows = {json.loads(line)["prompt"]: json.loads(line)["reference"]
            for line in (rewards_dir / "refs.jsonl").read_text(encoding="utf-8").splitlines()}
    assert known == 2
    assert rows["captured last week"] == "what the human sent"
    assert rows["from the original export"] == "corrected since"    # tonight's version wins


def test_an_answer_key_pack_is_judged_on_work_that_has_an_answer_key(tmp_path):
    """Captured traffic has no category label, and inventing one would hollow out the gate."""
    from train.autopilot import _pick_eval_source
    from train.config import (
        DataConfig,
        DeployConfig,
        RewardsConfig,
        RolloutConfig,
        TrainerConfig,
        TrainRunConfig,
    )

    workspace = tmp_path / "ws"
    (workspace / "run").mkdir(parents=True)
    (workspace / "labels.jsonl").write_text("{}\n", encoding="utf-8")
    (workspace / "rewards.py").write_text("", encoding="utf-8")
    out = tmp_path / "out"
    (out / "older-run").mkdir(parents=True)
    (out / "older-run" / "eval_prompts.jsonl").write_text('{"prompt": "labelled"}\n',
                                                          encoding="utf-8")
    run_dir = tmp_path / "tonight"
    run_dir.mkdir()
    (run_dir / "eval_prompts.jsonl").write_text('{"prompt": "captured"}\n', encoding="utf-8")

    cfg = TrainRunConfig(
        model_name="m",
        rollout=RolloutConfig(base_url="http://x/v1"),
        data=DataConfig(prompts_jsonl=str(workspace / "prompts.jsonl")),
        rewards=RewardsConfig(python_file=str(workspace / "rewards.py")),
        trainer=TrainerConfig(output_dir=str(out)),
        deploy=DeployConfig(nodes=("http://x/v1",), adapter_name="m"),
        source_path=workspace / "grid-train.toml",
    )
    chosen = _pick_eval_source(cfg, run_dir, tmp_path / "dataset")
    assert chosen == out / "older-run"          # the labelled holdout, not tonight's captured one


def test_a_night_with_traffic_for_a_different_model_says_so(tmp_path, monkeypatch):
    """0 examples has two causes: nobody used the grid, or nobody used THIS model. Only one of
    them fixes itself overnight, so they must not print the same sentence."""
    from train import autopilot
    from train.capture import Example
    from train.config import (
        DataConfig,
        DeployConfig,
        RewardsConfig,
        RolloutConfig,
        TrainerConfig,
        TrainRunConfig,
    )

    cfg = TrainRunConfig(
        model_name="base/model",
        rollout=RolloutConfig(base_url="http://x/v1"),
        data=DataConfig(prompts_jsonl=str(tmp_path / "p.jsonl")),
        rewards=RewardsConfig(python_file=str(tmp_path / "r.py")),
        trainer=TrainerConfig(output_dir=str(tmp_path / "out")),
        deploy=DeployConfig(nodes=("http://x/v1",), adapter_name="support-replies"),
        source_path=tmp_path / "grid-train.toml",
    )

    def build(days=30, *, include_accepted=True, models=None):
        # Plenty of captured work, none of it answered by this model.
        return [] if models else [Example("q", "a", "edited", 1.0)] * 50

    monkeypatch.setattr("train.capture.build_examples", build)
    monkeypatch.setattr("train.capture.prune", lambda **kw: None)
    monkeypatch.setattr(autopilot, "host_is_free", lambda **kw: (True, "idle"), raising=False)
    monkeypatch.setattr("train.nightly.host_is_free", lambda **kw: (True, "idle"))

    result = autopilot.run_cycle(cfg)
    assert result.stage == "waiting"
    assert "none of it was answered by this model" in result.detail
    assert "support-replies" in result.detail
