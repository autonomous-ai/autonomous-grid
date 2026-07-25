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

### Measured: disaggregated beats single-process, and sync cadence is the dial

Same task, same model (SmolLM2-135M), same CPU box. "Disaggregated" = trainer and rollout engine
in **separate processes over HTTP**, the two-node topology with both halves on one machine:

| Topology | Weight sync | Steps | Held-out eval | Verdict |
|---|---|---|---|---|
| single process | n/a | 60 | 0.220 → 0.674 | confirmed |
| disaggregated | every 5 steps | 40 | 0.188 → 0.261 | **below the bar** |
| disaggregated | every 2 steps | 60 | 0.188 → **0.768** | confirmed |

Two things follow. **Disaggregation costs nothing when the policy is kept current** — the
every-2 run beat the single-process run outright. And **cadence, not distribution, is the
dial**: the same topology learned 8× less per step at a 5-step cadence. Hence `sync_every`
defaults to **2**, and raising it is a deliberate trade (worth it only when a sync costs more
than a step — big models over slow links).

Corollary that closed a real hole: `grid train run` (the TRL path) had **no** sync — TRL only
syncs the vLLM fleets it manages itself, and with `rollout_func` the fleet is ours. A
`TrainerCallback` now pushes the adapter on the same cadence; without it the production path
would have trained on base-model rollouts for every run.

### Later decisions (2026-07-25, same day)

- **D11 — Two deployments, one command surface.** LAN mode (one building; the local proxy is the
  endpoint; the trainer can push adapters straight to nodes) and relay mode (offices apart; nodes
  poll the hosted relay outbound, which is what lets a laptop elsewhere serve without an open
  port). `train/endpoints.py` resolves either without anyone pasting a URL, and states the honest
  asymmetry rather than hiding it: **through the relay the trainer cannot reach the nodes to push
  new weights**, so relay-mode training either keeps its serving machines reachable (same office,
  VPN) or runs off-policy at a real cost in learning speed. Relay-side artifact distribution is
  phase 2 and lives in the closed repo.
- **D12 — Training is local, in every topology.** The three grid shapes describe where *inference*
  is served and what it costs (`docs/topologies.md`). Training is not a topology choice: API nodes
  cannot train at any price, and renting compute by the hour is the expensive way to do the one job
  idle office hardware suits.
- **D13 — The nightly cycle is one process per night, in a fixed order.** Idle check → train →
  prove → ship-only-on-a-pass (`train/nightly.py`). Not a daemon: cron and launchd are better
  schedulers than anything shipped here, and a process that exits is one an operator can reason
  about at 3am. A night that produces nothing better is a *success for the customer* — production
  is untouched — and a non-zero exit for the operator.
- **D14 — Host priority is code, not prose** (`train/hostsignals.py`). Mains power and keyboard idle
  gate every unattended run; unknown counts as free so a machine that cannot report never vetoes
  the feature. This is the commitment server-oriented orchestrators don't make, and training is
  where it becomes load-bearing: a run is sustained 100% duty, not a burst.
- **D15 — A browser interface, same engine** (`train/web/`). The CLI stays the engineer's surface;
  `grid train web` gives a support or sales manager the same five steps with the vocabulary of
  their job. It writes an ordinary `grid-train.toml` and launches an ordinary `grid train run`
  subprocess — a test pins that the CLI can load what the browser generated, so the two surfaces
  can never drift into separate products.

### 2026-07-26: learning from served work, and what "unattended" is allowed to mean

- **D16 — A model's own unjudged output is never trained on** (`train/capture.py`). Served requests
  are stored as candidate examples, but only three signals make one trainable, and each costs a
  human nothing: **a correction** (the person edited the answer — their version is ground truth,
  weight 1.0), **a teacher** (a stronger model answered a hard request — 0.8), and **acceptance**
  (sent as-is — 0.6). Discarded answers are kept for the record and never imitated; unjudged ones
  never become examples at all. This is the line that makes "zero human intervention" honest rather
  than a euphemism for a model drifting into agreement with itself.
- **D17 — Capture is off until someone turns it on, local-file-only, redacted, and pruned.** No
  upload, no aggregation, one JSONL per day on the machine that served the request. `POST
  /v1/feedback` + an `X-Grid-Request-Id` header let an app report what the human did; it no-ops
  when collecting is off so instrumenting an app is safe before anyone opts in.
- **D18 — Nothing about capture may ever be on a customer's critical path.** Learned the hard way:
  a quadratic redaction pattern plus a cap applied *after* scanning made one unauthenticated
  request worth ~34 seconds of blocked event loop. The rules now: bound every quantifier, clip
  before you scan, refuse bodies too large to be examples, and do the writing on a bounded
  background queue where a stalled disk costs examples rather than latency.
- **D19 — Autopilot refuses more often than it runs** (`train/autopilot.py`). It waits when there is
  too little to learn from (waiting is a *correct* outcome, not a failure), leaves a machine alone
  when someone is using it, and ships only past the same eval gate as every other path. A night that
  produced nothing better is a success for the customer and a non-zero exit for the operator.
- **D20 — Both training stages write the same artifacts, including a held-out set.** An imitation
  run reserves a seeded 10% before training and excludes it, so "is it better" is answerable on
  either rung and the browser flow cannot dead-end one click from its payoff.
- **D21 — The browser and the CLI are one product.** The browser writes an ordinary
  `grid-train.toml` and launches an ordinary `grid train` subprocess; a test pins that the CLI loads
  what the browser generated. Options are submitted as the *labels she read*, never as addresses,
  model ids or list positions — so nothing a form can say becomes a download URL or a host to aim a
  trainer at, and a list that re-sorts between draw and submit cannot silently change her choice.
- **D22 — Every screen must survive being read literally.** "Nothing is served until you press the
  button" means training never deploys; "learning now" must be false the moment the trainer dies;
  a button that cannot install a schedule must not claim to. Four of tonight's confirmed defects
  were sentences the code no longer honoured.

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
