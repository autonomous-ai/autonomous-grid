"""`grid train run --backend mlx`: the feedback stage on Apple Silicon, no CUDA.

The torch path (`train/run.py`) is TRL's GRPOTrainer and wants a CUDA box. On a Mac it does not
merely run slowly — a measured attempt on an M2 Max spent 84 CPU-minutes stuck loading weights
and never reached step one. That left Apple Silicon able to do the imitation stage and not the
feedback stage, even though `train/mlx/core.py` had a working GRPO loop the whole time. This
module is the wiring that was missing: the same loop, driven by a `grid-train.toml` instead of a
hard-coded word-reversal task.

Sampling happens in-process rather than over the rollout contract. On one machine that is simply
what is fastest — no HTTP, no serialisation, and the sampled logprobs are already exact instead of
being reconstructed from an engine's response. Spreading rollouts over a fleet is what the torch
path plus `grid train serve` is for, and is the reason the contract exists at all.

Two things it keeps from the torch path deliberately, because differing on either would make the
backends incomparable:

  * `lora_scale = lora_alpha / lora_rank`. peft multiplies the adapter branch by that ratio and
    mlx-lm multiplies by `scale` directly (adapters.py), so an adapter has to be built from the
    same number on both sides or a converted one means something different on each.
  * Multiple `reward_*` functions are SUMMED, which is what TRL does with `reward_funcs`, and
    each is called batched — `(prompts, completions_text=...)` — so a rewards.py written for one
    backend scores identically on the other.
"""
from __future__ import annotations

import json
import random
import time
from pathlib import Path

from ..config import TrainRunConfig
from ..hello_task import group_advantages
from ..rewards import load_prompts, load_reward_funcs
from .core import Policy

SYSTEM_PROMPT = "You are the support agent. Read the ticket and draft the reply to send."
HOLDOUT = 0.1          # matches split_holdout in train/sft.py
MIN_EVAL, MAX_EVAL = 4, 24


def split_holdout(prompts: list[str], seed: int = 17) -> tuple[list[str], list[str]]:
    """Held-out prompts and training prompts, disjoint and stable across runs.

    One shuffle, both halves taken from it. The torch side once shuffled to choose what to
    withhold and then sliced the ORIGINAL list to choose what to train on, so 8 of every 10
    "unseen" prompts had been trained on and a model could pass the gate by memorising.
    """
    order = list(range(len(prompts)))
    random.Random(seed).shuffle(order)
    n_held = max(MIN_EVAL, min(MAX_EVAL, round(len(prompts) * HOLDOUT)))
    n_held = min(n_held, max(1, len(prompts) // 2))
    held = [prompts[i] for i in order[:n_held]]
    learn = [prompts[i] for i in order[n_held:]]
    return held, learn


def score(reward_funcs: list, prompts: list[str], texts: list[str]) -> list[float]:
    """Total reward per completion: every reward_* summed, TRL's convention.

    Called batched because that is the signature packs are written against. A reward that returns
    a scalar or a short list is a bug in that rewards.py, and is reported as one rather than
    quietly broadcast into a shape that trains on nonsense.
    """
    totals = [0.0] * len(texts)
    for func in reward_funcs:
        out = func(prompts, completions=texts, completions_text=texts)
        if not isinstance(out, (list, tuple)) or len(out) != len(texts):
            name = getattr(func, "__name__", "a reward function")
            raise SystemExit(
                f"grid train: {name} returned {type(out).__name__} of "
                f"{len(out) if hasattr(out, '__len__') else '?'} for {len(texts)} completions — "
                "a reward_* function must return one score per completion."
            )
        for i, value in enumerate(out):
            totals[i] += float(value)
    return totals


def run_grpo(cfg: TrainRunConfig, *, run_dir: str | Path | None = None,
             steps: int | None = None, max_tokens: int | None = None,
             eval_every: int = 10, seed: int = 17) -> Path:
    """Climb on the config's prompts and rewards. Returns the adapter directory."""
    steps = steps or cfg.trainer.steps
    max_tokens = max_tokens or cfg.rollout.max_tokens
    prompts = load_prompts(cfg.data)
    reward_funcs = load_reward_funcs(cfg.rewards, cfg.data)
    held, learn = split_holdout(prompts, seed)

    run_dir = Path(run_dir).expanduser() if run_dir else (
        Path("~/.grid/artifacts/train").expanduser()
        / f"grpo-mlx-{Path(cfg.model_name).name.lower()}-{time.strftime('%Y%m%d-%H%M%S')}")
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "log.jsonl"

    print(f"Loading {cfg.model_name} …")
    policy = Policy(
        cfg.model_name,
        lora_rank=cfg.trainer.lora_rank,
        # peft's lora_alpha / r, in mlx-lm's units. See the module docstring.
        lora_scale=cfg.trainer.lora_alpha / max(cfg.trainer.lora_rank, 1),
        learning_rate=cfg.trainer.learning_rate,
        seed=seed,
    )

    def encode(prompt: str) -> list[int]:
        return policy.encode([{"role": "system", "content": SYSTEM_PROMPT},
                              {"role": "user", "content": prompt}])

    def evaluate() -> float:
        texts = [policy.sample(encode(p), max_tokens=max_tokens, greedy=True)[2] for p in held]
        return sum(score(reward_funcs, held, texts)) / len(held)

    started = time.time()
    baseline = evaluate()
    print(f"step 0  eval {baseline:.3f}  (baseline before any training)")
    # The baseline goes to the log FILE, not just the terminal. It is the number that decides
    # whether a run helped or hurt, and four defects once hid behind its absence — a reader
    # holding only log.jsonl cannot tell a climb from a collapse without it.
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"step": 0, "eval": round(baseline, 4)}) + "\n")

    spent = {"sample_s": 0.0, "update_s": 0.0, "eval_s": 0.0}
    rng = random.Random(seed)
    final = baseline
    for step in range(1, steps + 1):
        prompt = rng.choice(learn)
        prompt_ids = encode(prompt)
        completions, texts = [], []
        t0 = time.time()
        for _ in range(cfg.trainer.group_size):
            ids, lps, text = policy.sample(prompt_ids, max_tokens=max_tokens)
            completions.append((ids, lps))
            texts.append(text)
        spent["sample_s"] += time.time() - t0

        rewards = score(reward_funcs, [prompt] * len(texts), texts)
        advantages = group_advantages(rewards)
        record = {"step": step, "reward_mean": round(sum(rewards) / len(rewards), 4)}
        if any(advantages):
            t0 = time.time()
            record["loss"] = round(policy.step(prompt_ids, completions, advantages), 5)
            spent["update_s"] += time.time() - t0
        else:
            # Every completion scored the same, so the group's own mean is the bar and nothing
            # is above it. There is no gradient to take; skipping is correct, not a failure.
            record["skipped"] = "no reward spread in group"
        if step % eval_every == 0 or step == steps:
            t0 = time.time()
            final = evaluate()
            record["eval"] = round(final, 4)
            spent["eval_s"] += time.time() - t0
            print(f"step {step:>3}  reward {record['reward_mean']:.3f}  eval {final:.3f}  "
                  f"({time.time() - started:.0f}s)")
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    adapter_dir = policy.save_adapter(run_dir / "adapter")
    (run_dir / "run.json").write_text(json.dumps({
        "stage": "grpo", "backend": "mlx", "model": cfg.model_name,
        "prompts": len(learn), "held_out": len(held), "steps": steps,
        "group_size": cfg.trainer.group_size,
        "baseline_eval": round(baseline, 4), "final_eval": round(final, 4),
        "adapter": str(adapter_dir),
        "minutes": round((time.time() - started) / 60, 1),
        "sample_s": round(spent["sample_s"], 1), "update_s": round(spent["update_s"], 1),
        "sampling_share": round(
            spent["sample_s"] / max(spent["sample_s"] + spent["update_s"], 1e-9), 3),
    }, indent=2) + "\n", encoding="utf-8")

    print(f"\nBaseline {baseline:.3f} -> final {final:.3f}   adapter: {adapter_dir}")
    print(f"time: sampling {spent['sample_s']:.0f}s · update {spent['update_s']:.0f}s  ->  "
          f"sampling is "
          f"{100 * spent['sample_s'] / max(spent['sample_s'] + spent['update_s'], 1e-9):.0f}%"
          " of the splittable work")
    return adapter_dir
