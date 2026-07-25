"""`grid train` run configuration: one TOML file describing model, rollouts, rewards, trainer.

TOML (not CLI flags) because a training run has too many knobs to type and the file doubles as
the run's record — it is copied into the artifacts directory at launch. Read with stdlib
`tomllib`; validation is eager and total so a typo fails at parse time, not at step 400.
"""
from __future__ import annotations

import dataclasses
import tomllib
from pathlib import Path

SAMPLE = """\
# grid train — run configuration (see docs/adr/0019-rl-training-plane.md)

[model]
# HF id or local path of the base checkpoint the trainer loads. Must be the SAME model the
# rollout engine serves — the rollout contract trains on the engine's sampled token ids.
name = "Qwen/Qwen3-4B-Instruct-2507"

[rollout]
# Any OpenAI-compatible /v1 root that can return per-token ids + logprobs (vLLM engines do;
# see `grid train doctor`). Point it at a grid endpoint (local proxy or hosted relay).
base_url = "http://127.0.0.1:8000/v1"
api_key_env = "GRID_TRAIN_API_KEY"   # env var holding the bearer key ("" = none)
model = ""                           # served model name; defaults to [model].name
# target_provider = "node-01"        # optional: pin rollouts to one engine (X-Target-Provider)
max_tokens = 512
temperature = 1.0
timeout_seconds = 300

[data]
# Prompts: a JSONL file with {"prompt": "..."} per line, or a verifiers environment.
# prompts_jsonl = "tasks.jsonl"
# verifiers_env = "example-env"      # pip-installed verifiers environment id
# verifiers_env_args = {}

[rewards]
# Python file exporting reward functions (TRL signature:
#   def reward_xyz(prompts, completions, **kw) -> list[float]
# ). All top-level callables named reward_* are used. With [data].verifiers_env set and no
# file given, the environment's own rubric is used instead.
# python_file = "rewards.py"

[trainer]
group_size = 8            # completions scored per prompt (GRPO group)
steps = 100
learning_rate = 1e-5
per_device_batch = 2
gradient_accumulation = 4
lora_rank = 16
lora_alpha = 32
save_every_steps = 20
output_dir = ""           # default: ~/.grid/artifacts/train/<run-name>

[deploy]
# Serving nodes to hot-load the adapter onto after training (vLLM with runtime LoRA updates
# enabled). Each entry is a /v1 root. `grid train deploy` also does this standalone.
# nodes = ["http://127.0.0.1:8000/v1"]
# adapter_name = "grid-climb-1"
"""


class ConfigError(SystemExit):
    """Bad config = actionable CLI error, not a traceback."""

    def __init__(self, message: str) -> None:
        super().__init__(f"grid train: {message}")


@dataclasses.dataclass(frozen=True)
class RolloutConfig:
    base_url: str
    api_key_env: str = "GRID_TRAIN_API_KEY"
    model: str = ""
    target_provider: str = ""
    max_tokens: int = 512
    temperature: float = 1.0
    timeout_seconds: float = 300.0


@dataclasses.dataclass(frozen=True)
class DataConfig:
    prompts_jsonl: str = ""
    verifiers_env: str = ""
    verifiers_env_args: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class RewardsConfig:
    python_file: str = ""


@dataclasses.dataclass(frozen=True)
class TrainerConfig:
    group_size: int = 8
    steps: int = 100
    learning_rate: float = 1e-5
    per_device_batch: int = 2
    gradient_accumulation: int = 4
    lora_rank: int = 16
    lora_alpha: int = 32
    save_every_steps: int = 20
    output_dir: str = ""


@dataclasses.dataclass(frozen=True)
class DeployConfig:
    nodes: tuple[str, ...] = ()
    adapter_name: str = ""


@dataclasses.dataclass(frozen=True)
class TrainRunConfig:
    model_name: str
    rollout: RolloutConfig
    data: DataConfig
    rewards: RewardsConfig
    trainer: TrainerConfig
    deploy: DeployConfig
    source_path: Path

    @property
    def rollout_model(self) -> str:
        return self.rollout.model or self.model_name


def _section(raw: dict, name: str) -> dict:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    return value


def _build(cls, raw: dict, section: str):
    """Dataclass from a TOML table, rejecting unknown keys (typos must fail loudly)."""
    fields = {f.name: f for f in dataclasses.fields(cls)}
    unknown = set(raw) - set(fields)
    if unknown:
        raise ConfigError(f"[{section}] unknown key(s): {', '.join(sorted(unknown))}")
    coerced = {}
    for key, value in raw.items():
        if fields[key].type in ("tuple[str, ...]",) and isinstance(value, list):
            value = tuple(str(v) for v in value)
        coerced[key] = value
    try:
        return cls(**coerced)
    except TypeError as exc:  # wrong value type for a field
        raise ConfigError(f"[{section}]: {exc}") from exc


def load_config(path: str | Path) -> TrainRunConfig:
    path = Path(path).expanduser()
    if not path.is_file():
        raise ConfigError(f"config not found: {path} (run `grid train init` for a template)")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    model = _section(raw, "model")
    name = model.get("name", "")
    if not name or not isinstance(name, str):
        raise ConfigError("[model].name is required")

    rollout = _build(RolloutConfig, _section(raw, "rollout"), "rollout")
    if not rollout.base_url:
        raise ConfigError("[rollout].base_url is required")

    data = _build(DataConfig, _section(raw, "data"), "data")
    rewards = _build(RewardsConfig, _section(raw, "rewards"), "rewards")
    if not data.prompts_jsonl and not data.verifiers_env:
        raise ConfigError("[data] needs prompts_jsonl or verifiers_env")
    if not rewards.python_file and not data.verifiers_env:
        raise ConfigError("[rewards].python_file is required unless [data].verifiers_env is set")

    trainer = _build(TrainerConfig, _section(raw, "trainer"), "trainer")
    if trainer.group_size < 2:
        raise ConfigError("[trainer].group_size must be >= 2 (GRPO scores within a group)")

    deploy = _build(DeployConfig, _section(raw, "deploy"), "deploy")
    return TrainRunConfig(
        model_name=name,
        rollout=rollout,
        data=data,
        rewards=rewards,
        trainer=trainer,
        deploy=deploy,
        source_path=path,
    )
