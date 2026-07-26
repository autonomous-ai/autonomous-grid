"""RL hello world on Apple Silicon: GRPO word-reversal climb, one Mac, no CUDA.

    python -m train.mlx.grpo_hello                  # ~1 min on an M-series Mac
    python -m train.mlx.grpo_hello --steps 40       # shorter proof

Each step: sample a group of completions for one task from the current policy (recording the
sampled token ids and their logprobs — the behavior policy), grade them with the deterministic
reward, compute group-relative advantages, and take one clipped policy-gradient step on LoRA
adapters. Held-out eval (greedy decode) prints every --eval-every steps; success is the eval
score climbing well above its step-0 baseline. Artifacts (JSONL log, adapters, summary) land
in ~/.grid/artifacts/train/.

Correctness notes, deliberate and load-bearing:
- Training logprobs are computed over [prompt + completion] with completion positions masked
  in — i.e. conditioned on the prompt. (The community MLX GRPO we evaluated dropped the
  prompt; that trains the wrong distribution. ADR 0019.)
- The sampled-token logprobs from generation are the behavior policy; the loss uses a clipped
  importance ratio against them, TRL-style. With one update per batch the ratio starts at 1
  and the clip is inert — kept anyway so the semantics survive a future multi-epoch setting.
- Sampling is temperature-1 categorical over the same distribution the loss differentiates,
  so behavior and trainable logprobs are exactly comparable.
- The LoRA `scale` is the peft `lora_alpha / r`, in the same units (adapters.py:135) — the torch
  twin's `lora_alpha = 2 * rank` is this file's `--lora-scale 2.0`. mlx-lm's own examples use
  20.0, and taking that number here put the MLX adapter ten times louder than the peft one for
  the same nominal rank. That is a correctness problem before it is a stability one: an adapter
  converted between the two backends must mean the same thing on both, or a mixed fleet serves
  a different model than it trained. It was also unstable at the shipped learning rate — a
  0.705 baseline fell to 0.017, with the whole suite still green.
- The same `scale` is written into adapter_config.json, because mlx-lm rebuilds the LoRA layers
  from that file when serving. A config that disagrees with the run is the identical 10x fault,
  moved from the training loop into the artifact.
- The defaults mirror the torch twin's — same model, same 3-4 word task, same steps, tokens,
  rank, eval size and effective LoRA scale — so the two backends' numbers can be read side by
  side. Two differences remain and are deliberate: the optimiser (mlx `Adam` vs torch `AdamW`,
  which applies 0.01 weight decay), and how "all linear layers" is reached — `all-linear` in
  peft against every transformer block here.

Requires Apple Silicon (`pip install mlx-lm`). Verified against mlx-lm 0.31.3 APIs, and run on
an M2 Max: 0.216 -> 0.845 held-out in 62s over 60 steps.
"""
from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

from ..hello_task import (
    SYSTEM_PROMPT,
    group_advantages,
    make_eval_set,
    make_task,
    reward,
)
from .core import Policy

# The same model the torch twin starts from, so the two hello worlds report comparable numbers.
# It is deliberately weak: Qwen2.5-0.5B already scores ~0.84 on this task untrained, and a smoke
# test whose baseline is near the ceiling cannot show a climb no matter how well the loop works.
DEFAULT_MODEL = "mlx-community/SmolLM2-135M-Instruct"



def main() -> int:
    parser = argparse.ArgumentParser(description="GRPO hello world on Apple Silicon (MLX)")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--steps", type=int, default=60)
    parser.add_argument("--group-size", type=int, default=8)
    parser.add_argument("--max-tokens", type=int, default=40)
    parser.add_argument("--learning-rate", type=float, default=1e-4)
    parser.add_argument("--lora-rank", type=int, default=16)
    # -1 = every transformer block, the closest MLX equivalent of the torch twin's
    # `target_modules="all-linear"`. Adapting only the last 8 blocks trains far fewer parameters
    # and roughly halves the climb over the same 60 steps.
    parser.add_argument("--lora-layers", type=int, default=-1)
    # mlx-lm multiplies the LoRA branch by `scale` directly; peft multiplies by lora_alpha/r
    # (adapters.py:135). The torch twin uses lora_alpha = 2 * rank, so its effective scale is
    # 2.0 — this is the same knob, in the same units, and the two must agree or a converted
    # adapter means something different on either side of the fleet.
    parser.add_argument("--lora-scale", type=float, default=2.0)
    parser.add_argument("--clip-eps", type=float, default=0.2)
    parser.add_argument("--eval-every", type=int, default=10)
    parser.add_argument("--eval-tasks", type=int, default=16)
    # Task size, matching the torch twin. Reversing six words is a materially harder job than
    # reversing four, so leaving these unexposed made the two hello worlds measure different
    # tasks and report the numbers as if they were comparable.
    parser.add_argument("--min-words", type=int, default=3)
    parser.add_argument("--max-words", type=int, default=4)
    parser.add_argument("--seed", type=int, default=17)
    args = parser.parse_args()

    rng = random.Random(args.seed)

    print(f"Loading {args.model} …")
    policy = Policy(
        args.model,
        lora_rank=args.lora_rank, lora_scale=args.lora_scale, lora_layers=args.lora_layers,
        learning_rate=args.learning_rate, clip_eps=args.clip_eps, seed=args.seed,
    )

    run_dir = (
        Path("~/.grid/artifacts/train").expanduser()
        / f"mlx-hello-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "log.jsonl"

    def encode_prompt(user_text: str) -> list[int]:
        return policy.encode([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ])

    eval_set = make_eval_set(
        seed=args.seed + 1000,
        n=args.eval_tasks,
        min_words=args.min_words,
        max_words=args.max_words,
    )
    started = time.time()  # before the baseline eval: reported minutes = what the user waited

    def evaluate() -> float:
        scores = []
        for user_text, target in eval_set:
            _, _, text = policy.sample(encode_prompt(user_text),
                                       max_tokens=args.max_tokens, greedy=True)
            scores.append(reward(text, target))
        return sum(scores) / len(scores)

    baseline = evaluate()
    print(f"step 0  eval {baseline:.3f}  (baseline before any training)")
    # The baseline goes to the log *file*, not just the in-memory history: it is the number that
    # decides whether a run helped or hurt, and a reader holding only log.jsonl cannot tell a
    # climb from a collapse without it.
    history = [{"step": 0, "eval": round(baseline, 4)}]
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(history[0]) + "\n")

    # Where the time actually goes. The premise of putting training on a grid is that sampling
    # dominates and sampling is the part that fans out; that split is quoted from the field
    # (README section 3) rather than measured here, and never on this backend at all. Timing the
    # two phases costs nothing and turns the claim into something each run reports about itself.
    spent = {"sample_s": 0.0, "update_s": 0.0, "eval_s": 0.0}

    for step in range(1, args.steps + 1):
        user_text, target = make_task(rng, args.min_words, args.max_words)
        prompt_ids = encode_prompt(user_text)
        completions, rewards = [], []
        t0 = time.time()
        for _ in range(args.group_size):
            ids, lps, text = policy.sample(prompt_ids, max_tokens=args.max_tokens)
            completions.append((ids, lps))
            rewards.append(reward(text, target))
        spent["sample_s"] += time.time() - t0
        advantages = group_advantages(rewards)
        mean_reward = sum(rewards) / len(rewards)
        record = {"step": step, "reward_mean": round(mean_reward, 4)}
        if any(advantages):
            t0 = time.time()
            record["loss"] = round(policy.step(prompt_ids, completions, advantages), 5)
            spent["update_s"] += time.time() - t0
        else:
            record["skipped"] = "no reward spread in group"
        if step % args.eval_every == 0 or step == args.steps:
            t0 = time.time()
            record["eval"] = round(evaluate(), 4)
            spent["eval_s"] += time.time() - t0
            print(
                f"step {step:>3}  reward {mean_reward:.3f}  eval {record['eval']:.3f}  "
                f"({time.time() - started:.0f}s)"
            )
        history.append(record)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    final = history[-1].get("eval", 0.0)
    policy.save_adapter(run_dir / "adapters")
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "model": args.model,
                "steps": args.steps,
                "baseline_eval": round(baseline, 4),
                "final_eval": round(final, 4),
                "minutes": round((time.time() - started) / 60, 1),
                "sample_s": round(spent["sample_s"], 1),
                "update_s": round(spent["update_s"], 1),
                "eval_s": round(spent["eval_s"], 1),
                # Of the work that a grid could split, how much is the part that fans out.
                "sampling_share": round(
                    spent["sample_s"] / max(spent["sample_s"] + spent["update_s"], 1e-9), 3
                ),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\nBaseline {baseline:.3f} -> final {final:.3f}   artifacts: {run_dir}")
    print(
        f"time: sampling {spent['sample_s']:.0f}s · update {spent['update_s']:.0f}s · "
        f"eval {spent['eval_s']:.0f}s  ->  sampling is "
        f"{100 * spent['sample_s'] / max(spent['sample_s'] + spent['update_s'], 1e-9):.0f}% "
        f"of the splittable work"
    )
    # Ceiling-aware verdict: a strong base model can leave <0.15 of headroom; near-perfect
    # final performance is a pass even when the delta is small.
    verdict = (final - baseline >= 0.15) or (final >= 0.95 and final > baseline)
    print("CLIMB CONFIRMED" if verdict else "No meaningful climb — inspect log.jsonl")
    return 0 if verdict else 1



if __name__ == "__main__":
    raise SystemExit(main())
