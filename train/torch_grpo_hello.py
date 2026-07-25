"""RL hello world, torch edition: the same GRPO climb on any box — CUDA, or plain CPU.

    python -m train.torch_grpo_hello                # CUDA box: minutes. CPU: ~1 hour.
    python -m train.torch_grpo_hello --steps 40 --min-words 3 --max-words 4

This is the *reference twin* of `train.mlx.grpo_hello`: same task, same reward, same GRPO
semantics (prompt-conditioned completion logprobs, clipped ratio against the sampled behavior
logprobs, group-relative advantages), implemented on torch + peft-LoRA. It exists for three
reasons: (1) it runs on machines MLX can't touch — including CPU-only, which makes it the
"prove the loop today, anywhere" artifact; (2) it is the smoke test for the CUDA trainer lane;
(3) any numerical disagreement between the twins on the shared task is a bug detector for both.

Sampling is a manual batched decode loop (not model.generate) so the recorded behavior
logprobs come from exactly the categorical distribution being sampled — generate()'s
generation_config defaults (top-p/top-k) would silently reshape the distribution out from
under the logprobs.

Defaults are sized for a CPU proof: a 135M instruct model, 3-4 word tasks, 60 steps.
On a CUDA box, raise --steps / word counts and point --model at a 0.5-4B checkpoint.
"""
from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

from .hello_task import (
    SYSTEM_PROMPT,
    group_advantages,
    make_eval_set,
    make_task,
    reward,
)

DEFAULT_MODEL = "HuggingFaceTB/SmolLM2-135M-Instruct"


def _import_stack():
    try:
        import torch
        from peft import LoraConfig, get_peft_model
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as exc:
        raise SystemExit(
            f"grid train (torch): missing dependency ({exc.name}) — pip install 'grid[train]' "
            "(or: pip install torch transformers peft)."
        ) from exc
    return torch, LoraConfig, get_peft_model, AutoModelForCausalLM, AutoTokenizer


def main() -> int:
    parser = argparse.ArgumentParser(description="GRPO hello world on torch (CUDA or CPU)")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--steps", type=int, default=60)
    parser.add_argument("--group-size", type=int, default=8)
    parser.add_argument("--max-tokens", type=int, default=40)
    parser.add_argument("--learning-rate", type=float, default=1e-4)
    parser.add_argument("--lora-rank", type=int, default=16)
    parser.add_argument("--clip-eps", type=float, default=0.2)
    parser.add_argument("--min-words", type=int, default=3)
    parser.add_argument("--max-words", type=int, default=4)
    parser.add_argument("--eval-every", type=int, default=10)
    parser.add_argument("--eval-tasks", type=int, default=16)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--device", default="auto", help="auto | cuda | cpu")
    parser.add_argument(
        "--rollout-url",
        default=None,
        help="Serve rollouts remotely from this /v1 root (a grid endpoint or the MLX rollout "
        "server) instead of sampling in-process. The remote engine MUST serve the same model. "
        "Repeatable via --rollout-url a --rollout-url b to pool several nodes.",
        action="append",
    )
    parser.add_argument(
        "--sync-every",
        type=int,
        default=2,
        help="Push the adapter to the rollout nodes every N steps (0 = never, pure off-policy). "
        "Cadence dominates the learning rate: measured on this task, every-5 climbed +0.07 "
        "while every-2 climbed +0.58. Raise it only if a sync costs more than a step.",
    )
    args = parser.parse_args()

    torch, LoraConfig, get_peft_model, AutoModelForCausalLM, AutoTokenizer = _import_stack()
    torch.manual_seed(args.seed)
    rng = random.Random(args.seed)
    device = (
        args.device
        if args.device != "auto"
        else ("cuda" if torch.cuda.is_available() else "cpu")
    )
    dtype = torch.bfloat16 if device == "cuda" else torch.float32

    print(f"Loading {args.model} on {device} …")
    tokenizer = AutoTokenizer.from_pretrained(args.model)
    model = AutoModelForCausalLM.from_pretrained(args.model, torch_dtype=dtype).to(device)
    model = get_peft_model(
        model,
        LoraConfig(
            r=args.lora_rank,
            lora_alpha=2 * args.lora_rank,
            lora_dropout=0.0,
            task_type="CAUSAL_LM",
            target_modules="all-linear",
        ),
    )
    optimizer = torch.optim.AdamW(
        [p for p in model.parameters() if p.requires_grad], lr=args.learning_rate
    )
    eos_id = tokenizer.eos_token_id

    run_dir = (
        Path("~/.grid/artifacts/train").expanduser()
        / f"torch-hello-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "log.jsonl"

    def encode_prompt(user_text: str) -> list[int]:
        return tokenizer.apply_chat_template(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ],
            add_generation_prompt=True,
        )

    rollout_nodes = list(args.rollout_url or ())
    remotes = []
    if rollout_nodes:
        from .config import RolloutConfig
        from .rollout import GridRolloutClient

        remotes = [
            GridRolloutClient(
                RolloutConfig(base_url=url, max_tokens=args.max_tokens), args.model
            )
            for url in rollout_nodes
        ]
    remote = remotes[0] if remotes else None

    def sample_group_remote(prompt_ids: list[int], n: int, greedy: bool = False):
        # The remote engine tokenizes the same chat text with the same model's tokenizer, so
        # its sampled ids align with the trainer's vocabulary. Behavior logprobs come from the
        # engine — the loss's clipped ratio handles the (bounded) off-policy gap.
        # Round-robin across nodes by step so a pool shares the load; one group stays whole on
        # one node (prompt-prefix locality). Token ids go over the wire, not text — the engine
        # then conditions on exactly the prefix this trainer trains on.
        client = remotes[sample_group_remote.turn % len(remotes)]
        sample_group_remote.turn += 1
        rollouts = client.generate_group(prompt_ids, n, temperature=0.0 if greedy else None)
        return [(list(r.token_ids), list(r.logprobs), r.text) for r in rollouts]

    sample_group_remote.turn = 0

    def sync_adapter(step: int) -> str:
        """Save the adapter and reload it on every rollout node."""
        from .sync import push_adapter, summarize

        staging = run_dir / "adapter-live"
        model.save_pretrained(str(staging))
        return summarize(push_adapter(staging, rollout_nodes))

    @torch.no_grad()
    def sample_group(prompt_ids: list[int], n: int, greedy: bool = False, local: bool = False):
        """n completions: [(token_ids, behavior_logprobs, text)], eos-terminated.

        Training rollouts go remote when --rollout-url is set (the grid serves them).
        `local=True` forces the trainer's own weights — used by the held-out eval, which MUST
        measure the model being trained; measuring the remote engine would report the base
        model's score forever (a frozen eval line, learned the hard way).
        """
        if remote is not None and not local:
            return sample_group_remote(prompt_ids, n, greedy)
        model.eval()
        tokens = torch.tensor([prompt_ids] * n, device=device)
        past = None
        done = torch.zeros(n, dtype=torch.bool, device=device)
        ids: list[list[int]] = [[] for _ in range(n)]
        lps: list[list[float]] = [[] for _ in range(n)]
        inputs = tokens
        for _ in range(args.max_tokens):
            out = model(input_ids=inputs, past_key_values=past, use_cache=True)
            past = out.past_key_values
            logp = torch.log_softmax(out.logits[:, -1, :].float(), dim=-1)
            chosen = (
                logp.argmax(dim=-1)
                if greedy
                else torch.multinomial(logp.exp(), num_samples=1).squeeze(1)
            )
            chosen_lp = logp.gather(1, chosen[:, None]).squeeze(1)
            for i in range(n):
                if not done[i]:
                    ids[i].append(chosen[i].item())
                    lps[i].append(chosen_lp[i].item())
            done = done | (chosen == eos_id)
            if bool(done.all()):
                break
            inputs = chosen[:, None]
        return [
            # Graded text excludes special tokens (eos) — same rationale as the MLX twin.
            (ids[i], lps[i], tokenizer.decode(ids[i], skip_special_tokens=True))
            for i in range(n)
        ]

    def train_step(prompt_ids: list[int], completions, advantages: list[float]) -> float:
        model.train()
        p_len = len(prompt_ids)
        longest = max(len(ids) for ids, _, _ in completions)
        total = p_len + longest
        batch, mask, blp = [], [], []
        for ids, lp, _ in completions:
            batch.append(prompt_ids + ids + [eos_id] * (longest - len(ids)))
            # Completion token j (at full[p_len + j]) is the target of position p_len - 1 + j.
            row_mask = [0.0] * (total - 1)
            row_lp = [0.0] * (total - 1)
            for j in range(len(ids)):
                row_mask[p_len - 1 + j] = 1.0
                row_lp[p_len - 1 + j] = lp[j]
            mask.append(row_mask)
            blp.append(row_lp)
        full = torch.tensor(batch, device=device)
        inputs, targets = full[:, :-1], full[:, 1:]
        logits = model(input_ids=inputs).logits.float()
        token_lp = torch.log_softmax(logits, dim=-1).gather(2, targets[:, :, None]).squeeze(2)
        mask_t = torch.tensor(mask, device=device)
        ratio = torch.exp(token_lp - torch.tensor(blp, device=device))
        clipped = torch.clamp(ratio, 1.0 - args.clip_eps, 1.0 + args.clip_eps)
        adv = torch.tensor(advantages, device=device)[:, None]
        loss = (-torch.minimum(ratio * adv, clipped * adv) * mask_t).sum() / mask_t.sum().clamp(1)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        return loss.item()

    eval_set = make_eval_set(
        seed=args.seed + 1000, n=args.eval_tasks, min_words=args.min_words, max_words=args.max_words
    )
    started = time.time()

    def evaluate() -> float:
        scores = []
        for user_text, target in eval_set:
            (_, _, text) = sample_group(encode_prompt(user_text), 1, greedy=True, local=True)[0]
            scores.append(reward(text, target))
        return sum(scores) / len(scores)

    baseline = evaluate()
    print(f"step 0  eval {baseline:.3f}  (baseline before any training)")

    final = baseline
    for step in range(1, args.steps + 1):
        user_text, target = make_task(rng, args.min_words, args.max_words)
        prompt_ids = encode_prompt(user_text)
        group = sample_group(prompt_ids, args.group_size)
        rewards = [reward(text, target) for _, _, text in group]
        advantages = group_advantages(rewards)
        mean_reward = sum(rewards) / len(rewards)
        record = {"step": step, "reward_mean": round(mean_reward, 4)}
        if any(advantages):
            record["loss"] = round(train_step(prompt_ids, group, advantages), 5)
        else:
            record["skipped"] = "no reward spread in group"
        if rollout_nodes and args.sync_every and step % args.sync_every == 0:
            record["sync"] = sync_adapter(step)
            print(f"step {step:>3}  {record['sync']}")
        if step % args.eval_every == 0 or step == args.steps:
            final = evaluate()
            record["eval"] = round(final, 4)
            print(
                f"step {step:>3}  reward {mean_reward:.3f}  eval {final:.3f}  "
                f"({time.time() - started:.0f}s)"
            )
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    for client in remotes:
        client.close()
    model.save_pretrained(str(run_dir / "adapters"))
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "backend": f"torch/{device}",
                "rollout_source": args.rollout_url or "in-process",
                "model": args.model,
                "steps": args.steps,
                "baseline_eval": round(baseline, 4),
                "final_eval": round(final, 4),
                "minutes": round((time.time() - started) / 60, 1),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\nBaseline {baseline:.3f} -> final {final:.3f}   artifacts: {run_dir}")
    verdict = (final - baseline >= 0.15) or (final >= 0.95 and final > baseline)
    print("CLIMB CONFIRMED" if verdict else "No meaningful climb — inspect log.jsonl")
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())
