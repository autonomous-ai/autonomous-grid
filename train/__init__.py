"""Grid's RL training plane (`grid train`): hill-climb small models on your own machines.

The design (ADR 0019): Grid stays the fleet orchestrator; the trainer is a *consumer* of the
grid, not a second control plane. Rollout generation — the ~70-80% of RL fine-tuning that is
plain inference — runs on grid engines through the same OpenAI-compatible endpoint every other
consumer uses. The weight-update step runs wherever this CLI is invoked (one CUDA box for TRL,
a Mac for the future MLX lane), and the trained LoRA adapter is deployed back to serving nodes.

Split from the heavy ML stack on purpose: this package's rollout/config/deploy modules run on
the core CLI dependencies (httpx, stdlib) and are unit-tested in this repo; `train.run` is the
only module that imports torch/trl/peft, lazily, behind the `grid[train]` extra.
"""
