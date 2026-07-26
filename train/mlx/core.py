"""The GRPO mechanics on Apple Silicon, with no opinion about the task.

`grpo_hello.py` proved this loop on word reversal; `grpo.py` drives the same loop from a
`grid-train.toml`. The arithmetic that makes it correct is subtle enough that having two copies
would be a bug waiting for whichever copy someone forgets to fix, so it lives here once.

Correctness notes, deliberate and load-bearing (ADR 0019):

- Training logprobs are computed over [prompt + completion] with completion positions masked in —
  i.e. conditioned on the prompt. The community MLX GRPO we evaluated dropped the prompt, which
  trains the wrong distribution entirely.
- The sampled-token logprobs from generation are the *behaviour* policy; the loss uses a clipped
  importance ratio against them, TRL-style. With one update per batch the ratio starts at 1 and
  the clip is inert — kept so the semantics survive a future multi-epoch setting.
- Sampling is temperature-1 categorical over the same distribution the loss differentiates, so
  behaviour and trainable logprobs are exactly comparable.
- `scale` is peft's `lora_alpha / r`, in the same units (adapters.py). The torch twin's
  `lora_alpha = 2 * rank` is `lora_scale=2.0` here. mlx-lm's own examples use 20.0, and taking
  that number put the MLX adapter ten times louder than the peft one for the same nominal rank —
  a correctness fault before a stability one, because an adapter converted between backends must
  mean the same thing on both. It also collapsed a 0.705 baseline to 0.017 with the suite green.
- The same `scale` is written into adapter_config.json, because mlx-lm rebuilds the LoRA layers
  from that file when serving. A config disagreeing with the run is the identical 10x fault moved
  out of the loop and into the artifact.
"""
from __future__ import annotations

import json
from pathlib import Path


def import_mlx():
    """Import the MLX stack, or exit with the one instruction that fixes it."""
    try:
        import mlx.core as mx
        import mlx.optimizers as optim
        from mlx import nn
        from mlx_lm import load
        from mlx_lm.models.cache import make_prompt_cache
        from mlx_lm.tuner.utils import linear_to_lora_layers
    except ImportError as exc:
        raise SystemExit(
            f"grid train (mlx): missing dependency ({exc.name}) — needs an Apple Silicon Mac "
            "with `pip install mlx-lm` (or `pip install 'grid[train]'`)."
        ) from exc
    return mx, nn, optim, load, make_prompt_cache, linear_to_lora_layers


class Policy:
    """A LoRA-adapted MLX model you can sample from and take GRPO steps on."""

    def __init__(self, model_name: str, *, lora_rank: int = 16, lora_scale: float = 2.0,
                 lora_layers: int = -1, learning_rate: float = 1e-4, clip_eps: float = 0.2,
                 seed: int = 17) -> None:
        mx, nn, optim, load, make_prompt_cache, to_lora = import_mlx()
        self._mx, self._nn, self._make_cache = mx, nn, make_prompt_cache
        mx.random.seed(seed)

        self.model_name = model_name
        self.model, self.tokenizer = load(model_name)
        self.model.freeze()
        # -1 = every transformer block, the closest MLX equivalent of the torch twin's
        # target_modules="all-linear". Adapting only the last N trains far fewer parameters and
        # roughly halves the climb over the same step budget.
        self.lora_layers = len(self.model.layers) if lora_layers < 0 else lora_layers
        self.lora_rank, self.lora_scale = lora_rank, lora_scale
        to_lora(self.model, self.lora_layers,
                {"rank": lora_rank, "scale": lora_scale, "dropout": 0.0})
        self.optimizer = optim.Adam(learning_rate=learning_rate)
        self.eos_ids = set(self.tokenizer.eos_token_ids)
        self.clip_eps = clip_eps

        def loss_fn(model, inputs, targets, mask, behavior_lp, advantages):
            logits = model(inputs)
            token_lp = -nn.losses.cross_entropy(logits, targets, reduction="none")
            ratio = mx.exp(token_lp - behavior_lp)
            clipped = mx.clip(ratio, 1.0 - self.clip_eps, 1.0 + self.clip_eps)
            adv = advantages[:, None]
            per_token = -mx.minimum(ratio * adv, clipped * adv)
            return (per_token * mask).sum() / mx.maximum(mask.sum(), 1)

        self._loss_and_grad = nn.value_and_grad(self.model, loss_fn)

    # ---- sampling ------------------------------------------------------
    def encode(self, messages: list[dict]) -> list[int]:
        return self.tokenizer.apply_chat_template(messages, add_generation_prompt=True)

    def sample(self, prompt_ids: list[int], *, max_tokens: int,
               greedy: bool = False) -> tuple[list[int], list[float], str]:
        """One completion from the current policy: (token ids, sampled logprobs, text)."""
        mx = self._mx
        cache = self._make_cache(self.model)
        logits = self.model(mx.array(prompt_ids)[None], cache=cache)[:, -1, :]
        ids: list[int] = []
        logprobs: list[float] = []
        for _ in range(max_tokens):
            logp = logits - mx.logsumexp(logits, axis=-1, keepdims=True)
            token = mx.argmax(logp, axis=-1) if greedy else mx.random.categorical(logits)
            token_id = token.item()
            ids.append(token_id)
            logprobs.append(logp[0, token_id].item())
            if token_id in self.eos_ids:
                break
            logits = self.model(token[None], cache=cache)[:, -1, :]
        # The loss trains on the eos token — stopping is part of the action — but the *graded*
        # text must exclude it, or a decoded "<|im_end|>" is normalised into junk and silently
        # caps the reward while punishing the model for stopping.
        return ids, logprobs, self.tokenizer.decode(ids, skip_special_tokens=True)

    # ---- learning ------------------------------------------------------
    def step(self, prompt_ids: list[int], completions: list[tuple[list[int], list[float]]],
             advantages: list[float]) -> float:
        """One clipped policy-gradient update on a padded [group, seq] batch."""
        mx = self._mx
        p_len = len(prompt_ids)
        longest = max(len(ids) for ids, _ in completions)
        total = p_len + longest
        batch_inputs, batch_targets, batch_mask, batch_blp = [], [], [], []
        for ids, lps in completions:
            full = prompt_ids + ids + [0] * (longest - len(ids))
            batch_inputs.append(full[: total - 1])
            batch_targets.append(full[1:total])
            # Completion token j sits at full[p_len + j]; as a *target* it is predicted at
            # position p_len - 1 + j. Prompt and pad positions stay masked out.
            row_mask = [0.0] * (total - 1)
            row_lp = [0.0] * (total - 1)
            for j in range(len(ids)):
                row_mask[p_len - 1 + j] = 1.0
                row_lp[p_len - 1 + j] = lps[j]
            batch_mask.append(row_mask)
            batch_blp.append(row_lp)
        loss, grads = self._loss_and_grad(
            self.model,
            mx.array(batch_inputs), mx.array(batch_targets),
            mx.array(batch_mask), mx.array(batch_blp), mx.array(advantages),
        )
        self.optimizer.update(self.model, grads)
        mx.eval(self.model.parameters(), self.optimizer.state, loss)
        return loss.item()

    # ---- artifact ------------------------------------------------------
    def save_adapter(self, adapter_dir: Path) -> Path:
        """Weights plus the config mlx_lm rebuilds the LoRA layers from when serving.

        Both values are what training actually used, not what the flags said: `load_adapters`
        reads this file, so a `scale` that disagrees with the run applies the trained delta at
        the wrong magnitude on every serving node. `num_layers` is written resolved rather than
        as a -1 sentinel so it does not depend on mlx-lm clamping a negative index.
        """
        from mlx.utils import tree_flatten

        adapter_dir = Path(adapter_dir)
        adapter_dir.mkdir(parents=True, exist_ok=True)
        self._mx.save_safetensors(
            str(adapter_dir / "adapters.safetensors"),
            dict(tree_flatten(self.model.trainable_parameters())),
        )
        (adapter_dir / "adapter_config.json").write_text(
            json.dumps({
                "fine_tune_type": "lora",
                "num_layers": self.lora_layers,
                "lora_parameters": {"rank": self.lora_rank, "scale": self.lora_scale,
                                    "dropout": 0.0},
            }, indent=2) + "\n",
            encoding="utf-8",
        )
        return adapter_dir
