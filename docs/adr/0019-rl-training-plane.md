---
status: proposed
---

# `grid train`: an RL training plane where the grid serves the rollouts

A grid already runs inference on the machines a team owns. RL fine-tuning (GRPO-family) is
70–80% rollout generation — which *is* inference — so the grid is most of a training system
already. This ADR adds the missing quarter as a consumer of the grid, not a second control
plane: a `grid train` command that drives TRL's GRPOTrainer on the box it is invoked on, sources
every rollout from a grid endpoint, grades them against user-supplied rewards (a Python file of
graders, or a `verifiers` environment), and deploys the resulting LoRA adapter back onto serving
nodes. Small models hill-climbed on private data, with the data, the rollouts, and the weights
never leaving the owner's network.

Written alongside the first slice on the `grid-rl` branch; normative for what ships there.

## Decisions

- **D1 — The trainer is a grid *consumer*.** No trainer engine kind, no relay changes, no new
  wire endpoints in this slice. `train/rollout.py` speaks the ordinary OpenAI-compatible
  `/completions` with a bearer key; a hosted-relay URL and a local proxy URL work identically,
  so the command is mode-agnostic in dispatch (like `engine`/`agent`). Orchestration stays
  Grid's job; learning is TRL's; the seam between them is one experimental hook (D3).

- **D2 — The rollout contract is ids + logprobs, verified up front, failing closed.** The RL
  loss needs the *engine's* sampled token ids and their logprobs (behavior policy); client-side
  re-tokenization can silently disagree with what the engine sampled. We require
  `logprobs` + vLLM's `return_tokens_as_token_ids` and refuse plain-text tokens
  (`train/rollout.py::_parse_token`). `grid train doctor` / a pre-run probe performs a 1-token
  generation and reports truthfully — the ADR 0018 probe pattern applied to training. Practical
  consequence, stated honestly: **vLLM engines serve training rollouts; llama.cpp/Ollama/MLX
  engines don't (yet)** — they remain eval/judge/teacher capacity. Extending the contract to
  them is a per-engine shim project, deliberately out of this slice.

- **D3 — TRL 1.x `rollout_func` is the trainer seam, treated as disposable glue.** The hook
  inverts control — rollouts come from outside, TRL keeps the loss/optimizer machinery and its
  off-policy importance-sampling correction (which absorbs the drift from adapters going stale
  on serving nodes between updates). It is experimental upstream: the adapter keeps a
  keyword-tolerant signature, `run.py` checks for the hook's existence and fails with an install
  hint, and pyproject pins `trl>=1.9,<2`. If the hook moves, only `train/rollout.py` moves with
  it. (Why not prime-rl: its rollout contract requires vLLM-internal token endpoints plus
  pause/update-weights control of every engine — a second fleet manager beside the grid. Its
  async orchestration ideas belong in the grid itself, later.)

- **D4 — Rewards are the customer's ground truth, in two shapes only.** A Python file of
  `reward_*(prompts, completions, **kw) -> list[float]` functions (zero-dependency, points at
  ERP rows / test suites / ticket outcomes), or a `verifiers` environment id (community-standard
  format, existing catalog; its rubric is wrapped to the TRL signature). Rollout text graded is
  the text the engine actually produced (`completions_text` passthrough), never re-decoded ids.

- **D5 — Artifacts land in `~/.grid/artifacts/train/<run>/`; deploy is vLLM runtime-LoRA.**
  Each run directory holds the copied config (the run's record), checkpoints, the final
  `adapter/`, and `run.json`. `grid train deploy` hot-loads the adapter under a stable name via
  `POST /unload_lora_adapter` + `/load_lora_adapter` per node — idempotent, so nightly re-climbs
  keep one model name the `auto` router can go on routing to. Known limit, accepted for v1: the
  adapter path must be visible to the serving process (shared/synced directory across boxes); a
  real artifact push/pull plane over the relay is phase 2 and would follow the media pattern.

- **D6 — Heavy deps stay behind `grid[train]` and one lazy module.** Only `train/run.py` imports
  torch/trl/peft/datasets; config, rollout parsing, rewards loading, and deploy run on the core
  CLI deps and carry the unit tests. The base install and every other command are unaffected.

## Amendment (2026-07-25): the training rollout contract is not vLLM-only

D2 said "vLLM engines serve training rollouts; llama.cpp/Ollama/MLX don't (yet)." That was a
statement about which servers *happened* to expose ids+logprobs, and it read as a hardware
requirement. Corrected: the contract is a wire contract, and any engine can serve it.

- **D7 — Grid ships its own MLX rollout engine** (`train/mlx/rollout_server.py`): the same
  `POST /v1/completions` with `logprobs` + `return_tokens_as_token_ids`, plus
  `GET /v1/models` (so `grid join`'s existing MLX probe on port 8080 discovers it) and
  `POST /reload_adapter`. It **fails closed with 400** when a caller omits the training fields —
  a chat-shaped request must be told, not quietly served untrainable text. An Apple-Silicon Mac
  is therefore a first-class *training* node, and an all-Mac fleet needs no CUDA and no vLLM.
- **D8 — Held-out eval always runs on the trainer's own weights.** Found by running it: routing
  eval through a remote engine reports whatever weights that node last loaded, so the eval line
  sat frozen at 0.188 for an entire run while training was fine. `sample_group(..., local=True)`
  is the eval path; remote is training-rollouts-only.
- **D9 — Weight sync is mandatory, not an optimization** (`train/sync.py`): every N steps the
  trainer saves its adapter and calls `/reload_adapter` (or vLLM's `/load_lora_adapter`) on each
  node. Without it, remote engines sample the *base* model forever — the run limps off-policy
  and the reward curve never moves (also observed, not theorized). Node failures are reported
  and skipped, never fatal: one sleeping laptop must not kill a training run.
- **D10 — Cross-backend fleets get an adapter converter** (`train/adapters.py`): peft and MLX
  store the same low-rank pair with different key names and transposed layouts, so a file copy
  silently fails to load. Conversion is exact, pure-numpy (no torch/mlx import needed), and
  both directions are round-trip tested. Same-backend pairs sync directly; a torch trainer
  feeding MLX nodes converts first — **auto-conversion inside the sync loop is the next slice.**

Runbook: `docs/two-node-training.md`.

## Consequences / open items

- **Security precondition for anything beyond a trusted bench:** local mode's unauthenticated
  `PUT /nodes/{id}` + CORS `*` means a LAN peer can redirect a node's `endpoint_url` — with
  training in the picture that's a rollout/adapter poisoning vector, not just DoS. Tracked as
  the local-auth-vs-relay-on-prem fork; this slice does not widen the exposure (it adds no new
  server surface) but inherits it.
- Scheduling is manual in this slice: `[rollout].target_provider` pins rollouts to one engine
  (the existing `X-Target-Provider` header); pool isolation = a second grid. Device-info-driven
  role assignment and host-priority (battery/thermal/idle) gating of training load are the
  placement brain's phase-2 work.
- The MLX trainer lane (Apple-Silicon anchor) mirrors this exact rollout contract when built —
  which is the reason the contract lives in `train/rollout.py` and not inside TRL glue.
- No `grid train` in remote-mode marketing until the reliability floor (ADR 0017 issue 2) and
  the fork above are resolved.
