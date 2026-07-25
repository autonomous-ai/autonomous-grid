"""`grid train sft`: stage one — learn from the answers your team already wrote.

Every pack is a ladder, and this is its first rung. RL sharpens a model that can already *nearly*
do the job; imitation is how it gets there, and imitation is cheap — resolved tickets and closed
leads are thousands of worked examples nobody had to label. Two consequences that matter:

* **It runs on a Mac today.** No rollout server, no second machine, no NVIDIA card: MLX trains a
  LoRA on Apple Silicon directly, so the fastest path to a first useful model needs nothing a
  support or sales team doesn't already own.
* **It is what makes the RL pass worth running.** Starting GRPO from a checkpoint that already
  writes in your voice means the reward shapes something real instead of teaching basics.

Both backends write the same artifacts as an RL run — `adapter/`, `run.json`, `log.jsonl` — so the
dashboard, the eval gate and the deploy path treat an SFT model exactly like any other.
"""
from __future__ import annotations

import inspect
import json
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .config import ConfigError, TrainRunConfig
from .run import artifacts_root


def sft_data_file(cfg: TrainRunConfig) -> Path:
    """`sft.jsonl` sits beside the prompts file the packs and the web interface generate."""
    prompts = Path(cfg.data.prompts_jsonl).expanduser()
    candidate = prompts.parent / "sft.jsonl"
    if not candidate.is_file():
        raise ConfigError(
            f"no examples to imitate at {candidate} — the packs and the web interface write it "
            "next to prompts.jsonl, so run their prepare step first"
        )
    return candidate


def load_examples(path: Path, minimum: int = 20) -> list[dict]:
    """Read chat-format rows: {"messages": [{role, content}, …]}."""
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path}:{number}: not valid JSON ({exc})") from exc
        messages = row.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ConfigError(f'{path}:{number}: expected {{"messages": [...]}}')
        rows.append({"messages": messages})
    if len(rows) < minimum:
        raise ConfigError(
            f"{path}: only {len(rows)} examples — imitation wants a few hundred to be worth a run"
        )
    return rows


def pick_backend(preferred: str = "auto") -> str:
    """'mlx' on Apple Silicon with mlx-lm installed, else 'torch'."""
    if preferred != "auto":
        return preferred
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        try:
            import mlx_lm  # noqa: F401
        except ImportError:
            return "torch"
        return "mlx"
    return "torch"


def _run_dir(cfg: TrainRunConfig, backend: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    slug = cfg.model_name.rsplit("/", 1)[-1].lower()
    base = Path(cfg.trainer.output_dir).expanduser() if cfg.trainer.output_dir else artifacts_root()
    return base / f"sft-{backend}-{slug}-{stamp}"


# --- the Apple-Silicon path ----------------------------------------------------------------

def mlx_command(cfg: TrainRunConfig, data_dir: Path, adapter_dir: Path, *,
                iters: int, layers: int) -> list[str]:
    """The mlx-lm training command. Split out so a test can pin it without running training.

    `-c` rather than `-m`: the console script may not be on PATH and the module's `__main__` guard
    has moved between releases, but the entry function is stable.
    """
    return [
        sys.executable, "-c", "from mlx_lm.lora import main; main()",
        "--model", cfg.model_name,
        "--train",
        "--data", str(data_dir),
        "--adapter-path", str(adapter_dir),
        "--batch-size", str(max(1, cfg.trainer.per_device_batch)),
        "--num-layers", str(layers),
        "--iters", str(iters),
        "--learning-rate", str(cfg.trainer.learning_rate),
        "--steps-per-report", "10",
    ]


def run_sft_mlx(cfg: TrainRunConfig, examples: list[dict], run_dir: Path, *,
                iters: int | None = None, layers: int = 8) -> Path:
    """Train a LoRA with mlx-lm's own trainer, driven as a subprocess.

    Deliberately its trainer rather than a hand-rolled one: mlx-lm already handles batching, the
    validation split, checkpointing and the LoRA plumbing, and it is the interface Apple keeps
    stable. We own the data layout and the artifact layout; they own the training loop.
    """
    data_dir = run_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    # mlx-lm wants train.jsonl + valid.jsonl in one directory.
    split = max(1, len(examples) // 10)
    write_jsonl(data_dir / "valid.jsonl", examples[:split])
    write_jsonl(data_dir / "train.jsonl", examples[split:])

    adapter_dir = run_dir / "adapter"
    command = mlx_command(
        cfg, data_dir, adapter_dir,
        iters=iters or max(100, min(1200, len(examples) * 2)), layers=layers,
    )
    log_path = run_dir / "train.log"
    with log_path.open("w", encoding="utf-8") as handle:
        handle.write(" ".join(command) + "\n\n")
        handle.flush()
        completed = subprocess.run(command, stdout=handle, stderr=subprocess.STDOUT, check=False)
    if completed.returncode != 0:
        tail = "\n".join(log_path.read_text(encoding="utf-8").splitlines()[-12:])
        raise ConfigError(f"mlx-lm training failed (exit {completed.returncode}):\n{tail}")
    log_from_mlx(log_path, run_dir / "log.jsonl")
    return adapter_dir


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


_MLX_REPORT = re.compile(r"Iter (\d+):\s+(?:Train|Val) loss ([\d.]+)", re.IGNORECASE)


def log_from_mlx(train_log: Path, out: Path) -> None:
    """Turn mlx-lm's progress lines into the JSONL the dashboard already reads.

    Its report line is `Iter 10: Train loss 2.318, Learning Rate 1.000e-05, …`. Loss is the honest
    signal for imitation — there is no reward at this stage — so it goes in the same `loss` field
    an RL run writes, and the dashboard plots whatever it finds.
    """
    lines = [
        json.dumps({"step": int(match.group(1)), "loss": float(match.group(2))})
        for match in (
            _MLX_REPORT.search(line)
            for line in train_log.read_text(encoding="utf-8", errors="replace").splitlines()
        )
        if match
    ]
    if lines:
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- the torch path (CUDA, or CPU for small models) ---------------------------------------

def run_sft_torch(cfg: TrainRunConfig, examples: list[dict], run_dir: Path, *,
                  epochs: float = 1.0) -> Path:
    """TRL's SFTTrainer with a LoRA.

    Version-tolerant on purpose: the chat template is rendered here into a plain text column,
    which every TRL release accepts, rather than relying on whichever chat handling a given
    version shipped. Keywords that moved between releases are matched against the signature.
    """
    try:
        import datasets
        import peft
        import transformers
        import trl
    except ImportError as exc:
        raise ConfigError(
            f"missing training dependency ({exc.name}) — pip install 'grid[train]'"
        ) from exc

    tokenizer = transformers.AutoTokenizer.from_pretrained(cfg.model_name)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    dataset = datasets.Dataset.from_list([
        {"text": tokenizer.apply_chat_template(row["messages"], tokenize=False)}
        for row in examples
    ])

    sft_config = _build_sft_config(trl, {
        "output_dir": str(run_dir / "checkpoints"),
        "num_train_epochs": epochs,
        "per_device_train_batch_size": max(1, cfg.trainer.per_device_batch),
        "gradient_accumulation_steps": max(1, cfg.trainer.gradient_accumulation),
        "learning_rate": cfg.trainer.learning_rate,
        "logging_steps": 5,
        "save_strategy": "no",
        "report_to": [],
    })
    lora = peft.LoraConfig(
        r=cfg.trainer.lora_rank,
        lora_alpha=cfg.trainer.lora_alpha,
        lora_dropout=0.0,
        task_type="CAUSAL_LM",
        target_modules="all-linear",
    )
    kwargs: dict = {
        "model": cfg.model_name,
        "train_dataset": dataset,
        "peft_config": lora,
        "args": sft_config,
    }
    parameters = inspect.signature(trl.SFTTrainer.__init__).parameters
    if "processing_class" in parameters:
        kwargs["processing_class"] = tokenizer
    elif "tokenizer" in parameters:
        kwargs["tokenizer"] = tokenizer
    if "dataset_text_field" in parameters:
        kwargs["dataset_text_field"] = "text"

    trainer = trl.SFTTrainer(**kwargs)
    trainer.add_callback(_jsonl_logger(transformers, run_dir / "log.jsonl"))
    trainer.train()
    adapter_dir = run_dir / "adapter"
    trainer.save_model(str(adapter_dir))
    return adapter_dir


def _build_sft_config(trl, kwargs: dict):
    """SFTConfig gained and lost keywords across releases — pass only what this one takes."""
    accepted = inspect.signature(trl.SFTConfig.__init__).parameters
    for key, value in (("dataset_text_field", "text"), ("max_seq_length", 1024)):
        if key in accepted:
            kwargs[key] = value
    return trl.SFTConfig(**{k: v for k, v in kwargs.items() if k in accepted})


def _jsonl_logger(transformers, path: Path):
    class GridJsonlLog(transformers.TrainerCallback):
        def on_log(self, args, state, control, logs=None, **kwargs):
            if logs and "loss" in logs:
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"step": state.global_step,
                                         "loss": round(float(logs["loss"]), 5)}) + "\n")
            return control

    return GridJsonlLog()


# --- the entry point ------------------------------------------------------------------------

def run_sft(cfg: TrainRunConfig, *, backend: str = "auto", iters: int | None = None,
            run_dir: Path | str | None = None) -> Path:
    """Train stage one and return the adapter directory.

    `run_dir` lets a caller put the artifacts where its readers already look — the browser's
    progress page and the dashboard both watch `run/log.jsonl`, so a run started from a browser
    must land there rather than in a timestamped sibling nobody is watching.
    """
    started = time.time()
    examples = load_examples(sft_data_file(cfg))
    chosen = pick_backend(backend)
    run_dir = Path(run_dir).expanduser() if run_dir else _run_dir(cfg, chosen)
    run_dir.mkdir(parents=True, exist_ok=True)
    if cfg.source_path.is_file():
        shutil.copy2(cfg.source_path, run_dir / "config.toml")

    adapter = (run_sft_mlx(cfg, examples, run_dir, iters=iters) if chosen == "mlx"
               else run_sft_torch(cfg, examples, run_dir))

    (run_dir / "run.json").write_text(
        json.dumps({
            "stage": "sft",
            "backend": chosen,
            "model": cfg.model_name,
            "examples": len(examples),
            "adapter": str(adapter),
            "minutes": round((time.time() - started) / 60, 1),
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }, indent=2) + "\n",
        encoding="utf-8",
    )
    return adapter
