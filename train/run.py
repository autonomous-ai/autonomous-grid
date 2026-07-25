"""`grid train run`: wire config + rollouts + rewards into TRL's GRPOTrainer and climb.

This is the only module that touches the ML stack (torch/transformers/trl/peft/datasets), all
imported lazily so the rest of `grid train` — and the whole CLI — works without them. The
trainer runs wherever this command is invoked: the CUDA anchor box in v1 (TRL requires it); the
grid serves rollouts (train/rollout.py) and receives the adapter back (train/deploy.py).

Version note: TRL's `rollout_func` hook is experimental (accepted keyword on GRPOTrainer as of
TRL 1.9). We detect its absence up front and say exactly what to install rather than failing
mid-run; the pin lives in pyproject's `train` extra.
"""
from __future__ import annotations

import dataclasses
import inspect
import json
import shutil
import time
from pathlib import Path

from shared import paths

from .config import ConfigError, TrainRunConfig
from .rewards import load_prompts, load_reward_funcs
from .rollout import build_rollout_func, probe_endpoint

_INSTALL_HINT = "pip install 'grid[train]'  (CUDA box; see docs/adr/0019-rl-training-plane.md)"


def _import_stack():
    try:
        import datasets
        import peft
        import transformers
        import trl
    except ImportError as exc:
        raise ConfigError(f"missing training dependency ({exc.name}) — {_INSTALL_HINT}") from exc
    return datasets, peft, trl, transformers


def artifacts_root() -> Path:
    return paths.grid_home() / "artifacts" / "train"


def _run_dir(cfg: TrainRunConfig) -> Path:
    if cfg.trainer.output_dir:
        return Path(cfg.trainer.output_dir).expanduser()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    model_slug = cfg.model_name.rsplit("/", 1)[-1].lower()
    return artifacts_root() / f"{model_slug}-{stamp}"


def run_training(cfg: TrainRunConfig) -> Path:
    datasets, peft, trl, transformers = _import_stack()

    if "rollout_func" not in inspect.signature(trl.GRPOTrainer.__init__).parameters:
        raise ConfigError(
            f"this TRL version ({trl.__version__}) has no rollout_func hook — {_INSTALL_HINT}"
        )

    # Probe before loading gigabytes: a chat-only endpoint must fail here, not at step one.
    result = probe_endpoint(cfg.rollout, cfg.rollout_model)
    if not result["ok"]:
        raise ConfigError(f"rollout endpoint can't serve training rollouts: {result['detail']}")

    prompts = load_prompts(cfg.data)
    reward_funcs = load_reward_funcs(cfg.rewards, cfg.data)
    run_dir = _run_dir(cfg)
    run_dir.mkdir(parents=True, exist_ok=True)
    # The config is the run's record; copy it in before anything can fail.
    shutil.copy2(cfg.source_path, run_dir / "config.toml")

    tokenizer = transformers.AutoTokenizer.from_pretrained(cfg.model_name)
    rollout_func = build_rollout_func(cfg.rollout, cfg.rollout_model, tokenizer)

    grpo_config = trl.GRPOConfig(
        output_dir=str(run_dir / "checkpoints"),
        num_generations=cfg.trainer.group_size,
        max_steps=cfg.trainer.steps,
        learning_rate=cfg.trainer.learning_rate,
        per_device_train_batch_size=cfg.trainer.per_device_batch,
        gradient_accumulation_steps=cfg.trainer.gradient_accumulation,
        max_completion_length=cfg.rollout.max_tokens,
        temperature=cfg.rollout.temperature,
        save_steps=cfg.trainer.save_every_steps,
        logging_steps=1,
        report_to=[],
    )
    lora = peft.LoraConfig(
        r=cfg.trainer.lora_rank,
        lora_alpha=cfg.trainer.lora_alpha,
        task_type="CAUSAL_LM",
        target_modules="all-linear",
    )
    dataset = datasets.Dataset.from_list([{"prompt": p} for p in prompts])

    trainer = trl.GRPOTrainer(
        model=cfg.model_name,
        args=grpo_config,
        train_dataset=dataset,
        reward_funcs=reward_funcs,
        peft_config=lora,
        processing_class=tokenizer,
        rollout_func=rollout_func,
    )
    try:
        trainer.train()
    finally:
        rollout_func.close()

    adapter_dir = run_dir / "adapter"
    trainer.save_model(str(adapter_dir))
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "model": cfg.model_name,
                "rollout_endpoint": cfg.rollout.base_url,
                "steps": cfg.trainer.steps,
                "prompts": len(prompts),
                "trainer": dataclasses.asdict(cfg.trainer),
                "adapter": str(adapter_dir),
                "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return adapter_dir


def doctor(cfg: TrainRunConfig) -> dict:
    """Readiness report for `grid train doctor`: deps, endpoint, data/rewards — no training."""
    report: dict = {"deps": {}, "endpoint": {}, "data": {}}

    for module in ("torch", "transformers", "trl", "peft", "datasets", "verifiers"):
        try:
            imported = __import__(module)
            report["deps"][module] = getattr(imported, "__version__", "present")
        except ImportError:
            report["deps"][module] = None

    report["endpoint"] = probe_endpoint(cfg.rollout, cfg.rollout_model)

    try:
        prompts = load_prompts(cfg.data)
        rewards = load_reward_funcs(cfg.rewards, cfg.data)
        report["data"] = {"ok": True, "prompts": len(prompts), "reward_funcs": len(rewards)}
    except SystemExit as exc:
        report["data"] = {"ok": False, "detail": str(exc)}
    return report
