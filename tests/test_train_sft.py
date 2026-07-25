"""Stage-one (imitation) tests: data loading, backend choice, the MLX command, and the web
interface producing examples the CLI's SFT path can actually read.

The last one is the point: a manager who used the browser must be able to run the Mac-native
imitation stage, and that only works if the browser writes the same `sft.jsonl` the packs do.
"""
from __future__ import annotations

import json

import pytest

from train.config import (
    DataConfig,
    DeployConfig,
    RewardsConfig,
    RolloutConfig,
    TrainerConfig,
    TrainRunConfig,
)
from train.sft import load_examples, mlx_command, pick_backend, sft_data_file


def _cfg(tmp_path, model="HuggingFaceTB/SmolLM2-135M-Instruct") -> TrainRunConfig:
    return TrainRunConfig(
        model_name=model,
        rollout=RolloutConfig(base_url="http://grid.test/v1"),
        data=DataConfig(prompts_jsonl=str(tmp_path / "prompts.jsonl")),
        rewards=RewardsConfig(python_file=str(tmp_path / "rewards.py")),
        trainer=TrainerConfig(output_dir=str(tmp_path / "out"), per_device_batch=2,
                              learning_rate=1e-5),
        deploy=DeployConfig(),
        source_path=tmp_path / "grid-train.toml",
    )


def _write_sft(tmp_path, n=40):
    rows = [
        {"messages": [
            {"role": "system", "content": "You are the support agent."},
            {"role": "user", "content": f"Ticket {i}: my desk shows E07"},
            {"role": "assistant", "content": "Unplug it for 60 seconds, then hold DOWN."},
        ]}
        for i in range(n)
    ]
    (tmp_path / "sft.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    return tmp_path / "sft.jsonl"


def test_sft_data_file_is_found_beside_the_prompts(tmp_path):
    _write_sft(tmp_path)
    assert sft_data_file(_cfg(tmp_path)).name == "sft.jsonl"


def test_missing_examples_say_where_they_come_from(tmp_path):
    with pytest.raises(SystemExit, match="prepare step"):
        sft_data_file(_cfg(tmp_path))


def test_load_examples_reads_chat_rows(tmp_path):
    path = _write_sft(tmp_path, 40)
    rows = load_examples(path)
    assert len(rows) == 40
    assert rows[0]["messages"][-1]["role"] == "assistant"


def test_load_examples_refuses_a_handful(tmp_path):
    path = _write_sft(tmp_path, 5)
    with pytest.raises(SystemExit, match="only 5 examples"):
        load_examples(path)


def test_load_examples_rejects_the_wrong_shape(tmp_path):
    (tmp_path / "sft.jsonl").write_text('{"prompt": "hi", "reply": "there"}\n', encoding="utf-8")
    with pytest.raises(SystemExit, match="messages"):
        load_examples(tmp_path / "sft.jsonl", minimum=1)


def test_backend_choice_is_explicit_or_platform_led(monkeypatch):
    assert pick_backend("torch") == "torch"
    assert pick_backend("mlx") == "mlx"
    monkeypatch.setattr("platform.system", lambda: "Linux")
    assert pick_backend("auto") == "torch"
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    assert pick_backend("auto") == "torch"     # Intel Mac: MLX cannot run here


def test_mlx_command_shape(tmp_path):
    """Pin the command: a silent flag rename upstream should fail here, not in a user's run."""
    cfg = _cfg(tmp_path)
    command = mlx_command(cfg, tmp_path / "data", tmp_path / "adapter", iters=300, layers=8)
    assert command[1:3] == ["-c", "from mlx_lm.lora import main; main()"]
    joined = " ".join(command)
    for flag in ("--model", "--train", "--data", "--adapter-path", "--batch-size",
                 "--num-layers", "--iters", "--learning-rate", "--steps-per-report"):
        assert flag in joined
    assert "--iters 300" in joined
    assert cfg.model_name in joined


def test_mlx_log_is_translated_for_the_dashboard(tmp_path):
    from train.sft import log_from_mlx

    (tmp_path / "train.log").write_text(
        "Loading pretrained model\n"
        "Iter 10: Train loss 2.318, Learning Rate 1.000e-05, It/sec 1.2\n"
        "Iter 20: Val loss 1.902, Val took 3.1s\n"
        "Iter 20: Train loss 1.880, Learning Rate 1.000e-05\n",
        encoding="utf-8",
    )
    log_from_mlx(tmp_path / "train.log", tmp_path / "log.jsonl")
    rows = [json.loads(line) for line in
            (tmp_path / "log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["step"] for r in rows] == [10, 20, 20]
    assert rows[0]["loss"] == 2.318


# --- the seam that matters: browser output feeding the CLI's imitation stage ---------------

def test_web_prepare_writes_sft_examples_the_cli_can_read(tmp_path):
    """A manager who used the browser must be able to run stage one."""
    from train.web import prepare

    rows = [{"prompt": f"Subject: desk {i}\n\nIt shows E07", "reference": "Unplug for 60 seconds."}
            for i in range(30)]
    prepare.write_task_files("support-replies", rows, tmp_path)
    assert (tmp_path / "sft.jsonl").is_file()
    examples = load_examples(tmp_path / "sft.jsonl", minimum=30)
    assert len(examples) == 30
    roles = [m["role"] for m in examples[0]["messages"]]
    assert roles == ["system", "user", "assistant"]
    assert "support agent" in examples[0]["messages"][0]["content"]
    assert examples[0]["messages"][-1]["content"] == "Unplug for 60 seconds."


def test_web_prepare_writes_lead_examples_in_the_graded_shape(tmp_path):
    from train.web import prepare

    rows = [{"prompt": f"Lead: firm {i} wants 30 desks", "label": "hot"} for i in range(25)]
    prepare.write_task_files("sales-triage", rows, tmp_path)
    examples = load_examples(tmp_path / "sft.jsonl", minimum=25)
    # The ideal answer is the shape the graders check, carrying the true priority.
    assert examples[0]["messages"][-1]["content"].startswith("PRIORITY: hot")
    assert "NEXT:" in examples[0]["messages"][-1]["content"]
