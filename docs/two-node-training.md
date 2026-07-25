# Two-node training on a grid: Mac + Mac (no NVIDIA, no vLLM)

A worked runbook for the smallest real distributed setup: **one machine generates the rollouts,
another runs the trainer**, on hardware that is already on your desk. The example pair is an
Intel iMac and an M2 MacBook Pro, which between them cover both backends Grid supports for
training.

## Why this works without vLLM

RL training needs two things from whatever generates a completion, and no chat API returns
them: the **token ids the engine actually sampled**, and the **logprob it assigned each one**
(the behavior policy the loss compares against). vLLM happens to expose both
(`logprobs` + `return_tokens_as_token_ids`), which is why it was the v1 requirement — not
because CUDA is required by the math.

`train/mlx/rollout_server.py` serves that same contract from MLX, so an Apple-Silicon Mac is a
first-class rollout node. `train/adapters.py` converts LoRA adapters between the torch/peft and
MLX layouts, so the trainer and the rollout nodes no longer have to share a backend.

## Roles

| Role | What it does | Runs on |
|---|---|---|
| **Rollout node(s)** | samples completions with ids+logprobs; hot-reloads the adapter each sync | any Mac (MLX), or a vLLM box |
| **Trainer** | grades rollouts, computes GRPO updates, owns the eval | the machine with the most compute |
| **Grid master** | one endpoint, node registry, dashboard | anywhere (it is not compute-bound) |

The trainer always runs the **held-out eval on its own weights** — never through a rollout node.
A remote eval measures whichever weights that node last loaded, which silently reports the base
model's score forever. (This was a real bug, found by running it: the eval line was flat at
0.188 for the whole run while training was working.)

## Runbook (Intel iMac + M2 MacBook)

### 1. On the M2 — the rollout node

```bash
git clone https://github.com/autonomous-ai/autonomous-grid.git && cd autonomous-grid
git checkout grid-rl
python3 -m venv .venv && source .venv/bin/activate && pip install -e . && pip install mlx-lm
python -m train.mlx.rollout_server --model mlx-community/SmolLM2-135M-Instruct --port 8080
```

Optionally join it to the grid so it also serves ordinary inference — `grid join` probes MLX on
port 8080 and finds this server, because it answers `GET /v1/models` like any MLX engine.

### 2. On the iMac — the trainer

```bash
cd autonomous-grid && git checkout grid-rl
./.venv-test/bin/python -m train.torch_grpo_hello \
  --rollout-url http://<m2-hostname>.local:8080/v1 \
  --sync-every 5 --steps 60
```

Pass `--rollout-url` more than once to pool several nodes (groups are round-robined whole, so
each group keeps its prompt-prefix locality on one node).

### 3. Watch it

```bash
./.venv-test/bin/grid train ui     # http://127.0.0.1:8321
```

## The weight-sync loop, and the honest constraint

Every `--sync-every N` steps the trainer saves its adapter and calls each node's
`/reload_adapter` (or vLLM's `/load_lora_adapter`). Without this the nodes keep sampling the
**base** model forever: training still limps along off-policy from stale samples, but the
reward curve stays flat and you learn nothing. A node that fails to reload is reported and
skipped — one sleeping laptop must never kill the run.

**The constraint to know:** the adapter file the trainer writes is in its own backend's format,
and `/reload_adapter` on an MLX node wants MLX-format weights. Same-backend pairs (torch
trainer → torch/vLLM nodes, or MLX trainer → MLX nodes) sync directly. For a **cross-backend**
pair, convert first:

```python
from train.adapters import convert
convert("~/.grid/artifacts/train/<run>/adapter-live", "/shared/adapter-mlx")  # peft -> mlx
```

Automating that conversion inside the sync loop is the next slice; today it is one call, and
the direction that needs it is torch-trainer → MLX-nodes.

Also true today: the adapter path must be readable by the node doing the reload (a synced or
shared directory). A push-over-the-wire artifact plane is ADR 0019 phase 2.

## Which topology to pick

- **All-Mac shop, one strong Mac:** train on the M-series Mac with `train.mlx.grpo_hello`;
  point extra Macs at it as rollout nodes (same backend, sync works directly).
- **Mixed Intel/Apple, as in this runbook:** rollouts on the M2 (its GPU is the fast part),
  trainer on the iMac, one adapter conversion per sync.
- **You own a 4090/RTX-class card:** trainer + vLLM rollouts on that box, Macs as extra rollout
  capacity, judges, and evals. Highest throughput per watt of engineering.
