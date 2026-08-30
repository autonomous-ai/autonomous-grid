# Grid Goal physical test log

This is an evidence index for physical-machine runs. It records observed relay identities and
immutable Goal artifacts; agent-authored hostnames are never treated as proof. Pairing bundles,
access tokens, and local credentials must not be added here.

## 2026-08-30 — native Codex rollout preflight

### Topology

- Disposable Grid: `goal-physical`
- Relay: physical machine A at `192.168.86.196:8090`
- Relay implementation: private relay commit `35eceeb`
- Public worker implementation after fix: `602fbd0` (`Handle deferred Codex Goal rollouts`)
- Task execution node: `goal-b-c19853f3-8a8f-4a8d-a961-87e79b087a99`, displayed as
  `grid-goal-relay-host` (MacBook Pro, Apple M2 Max, 64 GB)
- Goal model: `Qwen3.6-35B-A3B`, Responses-capable on the disposable Grid
- Test-only inference path: `goal-physical` loopback bridge to the existing `autonomous.ai` Grid.
  The target relay attributes the request to the signed bridge node above; it does not claim that
  node physically hosted the source model.
- Physical machine B at `192.168.86.176` was not connected for this preflight. This run proves one
  native worker only and is not the two-machine handoff gate.

### Failure that found the bug

- Goal: `921c75ae-e030-427f-ab50-be14ee215d65`
- Turn: `aa62c21a-d66d-4048-87b2-6e8ff48386d4`
- Result: `failed`, zero completed turns, three attempts, `retries_exhausted`
- Exact worker error on every attempt: `Codex's distributed Goal rollout does not exist`
- Cause: Codex 0.150.1 returns the future JSONL path from `thread/start` before the first
  `thread/goal/set` activation creates the file. Grid incorrectly required the file immediately.
- Evidence correctly contained retry worktree/transcript checkpoints and no inference or eval run;
  therefore this was not misdiagnosed as a model-routing or evaluation failure.

The fix persists the contained future path before activation, requires the file after a completed
turn, restarts a missing rollout only when zero native turns completed, and fails closed if real
native history goes missing. The focused suite passed 18 tests and the combined native-agent/Goal
suite passed 355 tests before the live rerun.

### Passing rerun

- Project: `c9467abf-4148-4bfa-9290-4ad4195e672f`
- Goal: `aa3f7c30-26f2-46d7-9e4a-8fd918e48cc8`
- Turn: `da36944b-f735-4ebe-a12b-fac584b6800e`
- Objective: create a 27-byte `proof.txt` containing the exact line
  `GRID_GOAL_CODEX_ROLLOUT_OK`
- Result: `complete` in one native Codex turn on attempt 1
- Project input commit: `78fde70fa8b8f3f9c8b0c17c4fdd0bd86324ab0c`
- Project result commit: `c9ba64e186d4673f2443994c9ddb0d08abd60dcc`
- Transcript result commit: `145509864575e9b01b7c4e783cb53184ded282a8`
- Inference: four completed Grid-attributed requests, 42,107 input tokens and 649 output tokens
- Independent relay eval: evaluator `relay`, score `1.0`, accepted, exact size 27 bytes and required
  literal found
- Verification command exited zero with terminal-turn, transcript, inference, and eval checks:

  ```bash
  GRID_HOME=<relay-node-home> uv run grid --json goal evidence \
    aa3f7c30-26f2-46d7-9e4a-8fd918e48cc8 \
    --grid goal-physical --verify --min-execution-nodes 1 --require-inference
  ```

### Findings retained for later gates

- The Goal reported 42,756 cumulative tokens against a 20,000-token budget because a single native
  turn can cross the limit before Grid observes its final usage. No later turn was scheduled, and
  the completed/eval-passing outcome was retained. The multi-turn budget gate must explicitly test
  this slice-boundary behavior; a token budget is not a hard mid-request cancellation limit.
- Restarting the test inference bridge originally merged its new loopback endpoint behind a dead
  old endpoint. Commit `ade3904` makes the helper unregister stale state before join and unregister
  itself on normal shutdown. It also replaces physical A/B bootstrap labels with relay-host and
  joining-worker roles. The combined regression suite passed 373 tests.
- Required next proof: two distinct relay-signed execution node IDs, one abrupt lease reclaim, a
  portable native transcript/worktree continuation, Grid-attributed inference for every settled
  turn, and accepted independent evals on the final commit.

## 2026-08-30 — two-machine Grid Courier handoff

### Topology and immutable identity

- Disposable Grid: `goal-physical`; relay: physical machine A at `192.168.86.196:8090`.
- Joining worker B: `goal-machine-b-48759900-4980-4b1a-ae6b-926e62ddd835`, displayed as
  `grid-goal-machine-b` (macOS x86_64, AMD Radeon Pro 5500 XT).
- Relay-host worker A: `goal-b-c19853f3-8a8f-4a8d-a961-87e79b087a99`, displayed as
  `grid-goal-relay-host` (macOS arm64, Apple M2 Max).
- Model: `Qwen3.6-35B-A3B`, requested by Codex through the disposable Grid and bridged to the
  existing `autonomous.ai` Grid. Relay evidence attributes execution and inference separately.
- Final public worker commit: `ec431ad`; final private relay commit: `7d65335`.

### Goal and physical failure sequence

- Project: `5ace7965-5596-4a2b-a126-7df0b10edebe`.
- Goal: `76b79310-8f03-4737-bcc6-df1128946846`.
- Machine B completed turn `3fbe95e8-e5aa-4d95-9ee6-e14764314d8a`, producing worktree commit
  `c324c11db01f014569b7ba2c360d5df653329612` and transcript commit
  `dff75ab6c29c36cf17a75a6eb1cb888d8bdd3dd3`.
- B claimed the next turn, then its worker process was killed with `SIGKILL`. The relay recorded
  `task.retry` with `reason=lease_expired` and the exact B node ID.
- A claimed attempt 2 of the same turn `45e4d154-2478-4fbb-83f3-86e2b9ca66b0`, fetched B's Git and
  native transcript checkpoints, and completed with worktree commit
  `fd83ff53114b9bbe1d7806c7446f12527396da9f` and transcript commit
  `213adba6e654a6c740a97013b67a9cac3cf6df59`.
- The evidence export contains both signed execution node IDs. Every adjacent completed turn is an
  ancestor of the next input, `transcript_pruned=false`, and no turn branch was pruned.

### Bugs found and corrected during the run

- The initial 1,000,000-token cap ended at a native slice boundary with 2,010,656 accounted tokens
  and three of six evals passing. The same Goal was safely extended in place to 10,000,000 tokens;
  its turns, trajectory, prior evals, and usage were preserved.
- A resumed native Codex Goal was not receiving the relay's new continuation prompt. Rewriting the
  stored objective was insufficient because Codex retained the original objective. The worker now
  fences a `turn/steer` call to the exact started turn and fails the attempt if steering is rejected.
- Failure feedback originally named failed checks but omitted their immutable paths and literals.
  This caused the local model to guess `INSTRUCTIONS.md` and `tests/test-game.html`. The relay now
  includes each exact failed eval contract; the worker reconstructs the same guidance from signed
  Goal metadata for compatibility with older claim payloads.
- Grid rejected multiple native self-completion claims. It first rejected missing `README.md` and
  `tests/game.test.mjs`, then rejected `README.md` because the exact case-sensitive literal `Tests`
  was absent. The Goal remained active until fresh result commits satisfied the relay-owned evals.

### Final result

- Status: `complete`; 11 completed distributed turns; 9,499,857 of 10,000,000 tokens consumed.
- Final result commit: `471bc335650a4c92c0a10ba4d7dbe0ce5aec4078`.
- All six immutable relay evals passed on that exact commit. The evidence contains 36 accepted eval
  rows across completion nominations.
- Independent command run against the clean final worktree at that commit:

  ```bash
  node --test tests/game.test.mjs
  ```

  Result: 34 tests passed, zero failed.
- Grid recorded 18 inference-attempt summaries: 153 requests, 10,996,794 input tokens, and 93,199
  output tokens. Failed/interrupted attempts remain in the evidence instead of being erased.
- Strict verification exited zero:

  ```bash
  GRID_HOME=<paired-home> uv run grid goal evidence \
    76b79310-8f03-4737-bcc6-df1128946846 \
    --grid goal-physical --verify --min-execution-nodes 2 --require-inference
  ```

  Evidence snapshot: `6f01972c2339307e5eb9fb302036f300055ddc961dc074d16fcca2ea6185dba3`.
