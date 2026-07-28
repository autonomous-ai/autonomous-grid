"""Reward + prompt sources for `grid train`: a JSONL of prompts with Python graders, or a
verifiers environment.

Two deliberate shapes and no third: a plain Python file of `reward_*` functions is the zero-
dependency path a customer can write against their own ground truth (ERP rows, test suites,
resolved tickets); a `verifiers` environment id buys the community-standard format and its
existing catalog. Both reduce to TRL's native reward-function signature so the trainer sees one
interface.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from .config import ConfigError, DataConfig, RewardsConfig


def load_prompts(data: DataConfig) -> list[str]:
    if data.prompts_jsonl:
        path = Path(data.prompts_jsonl).expanduser()
        if not path.is_file():
            raise ConfigError(f"[data].prompts_jsonl not found: {path}")
        prompts: list[str] = []
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ConfigError(f"{path}:{line_no}: not valid JSON ({exc})") from exc
            prompt = record.get("prompt") if isinstance(record, dict) else None
            if not isinstance(prompt, str) or not prompt:
                raise ConfigError(f'{path}:{line_no}: expected {{"prompt": "..."}}')
            prompts.append(prompt)
        if not prompts:
            raise ConfigError(f"{path}: no prompts")
        return prompts
    return _verifiers_prompts(data)


def load_reward_funcs(rewards: RewardsConfig, data: DataConfig) -> list:
    if rewards.python_file:
        return _python_rewards(Path(rewards.python_file).expanduser())
    return _verifiers_rewards(data)


def _python_rewards(path: Path) -> list:
    if not path.is_file():
        raise ConfigError(f"[rewards].python_file not found: {path}")
    spec = importlib.util.spec_from_file_location("grid_train_rewards", path)
    if spec is None or spec.loader is None:
        raise ConfigError(f"cannot import rewards file: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    funcs = [
        getattr(module, name)
        for name in sorted(dir(module))
        if name.startswith("reward_") and callable(getattr(module, name))
    ]
    if not funcs:
        raise ConfigError(f"{path}: no reward_* functions found")
    return funcs


def _load_environment(data: DataConfig):
    try:
        import verifiers
    except ImportError as exc:
        raise ConfigError(
            "[data].verifiers_env needs the `verifiers` package — pip install 'grid[train]'"
        ) from exc
    try:
        return verifiers.load_environment(data.verifiers_env, **(data.verifiers_env_args or {}))
    except Exception as exc:  # their loader raises plain Exception subclasses
        raise ConfigError(f"verifiers environment {data.verifiers_env!r} failed to load: {exc}") from exc


def _verifiers_prompts(data: DataConfig) -> list[str]:
    env = _load_environment(data)
    dataset = getattr(env, "dataset", None)
    if dataset is None:
        raise ConfigError(
            f"verifiers env {data.verifiers_env!r} exposes no dataset; provide [data].prompts_jsonl"
        )
    prompts: list[str] = []
    for row in dataset:
        value = row.get("question") or row.get("prompt")
        if isinstance(value, list):  # chat-message form; single-turn user text is the v0 scope
            value = next((m.get("content") for m in reversed(value) if m.get("role") == "user"), None)
        if isinstance(value, str) and value:
            prompts.append(value)
    if not prompts:
        raise ConfigError(f"verifiers env {data.verifiers_env!r}: no usable prompts in its dataset")
    return prompts


def _verifiers_rewards(data: DataConfig) -> list:
    env = _load_environment(data)
    rubric = getattr(env, "rubric", None)
    funcs = list(getattr(rubric, "reward_funcs", []) or [])
    if not funcs:
        raise ConfigError(
            f"verifiers env {data.verifiers_env!r} has no rubric reward functions; "
            "provide [rewards].python_file"
        )

    def wrap(func):
        # verifiers rubric functions take (prompt, completion, **kw) per-sample; TRL wants
        # batch-in, batch-out. `completions_text` (from the rollout adapter) carries the
        # engine's actual text.
        def batched(prompts, completions=None, completions_text=None, **kwargs):
            texts = completions_text if completions_text is not None else completions
            return [float(func(prompt=p, completion=t)) for p, t in zip(prompts, texts)]

        batched.__name__ = getattr(func, "__name__", "verifiers_reward")
        return batched

    return [wrap(f) for f in funcs]
