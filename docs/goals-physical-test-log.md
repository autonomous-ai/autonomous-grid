# Grid Goal physical test log

This is an evidence index for physical-machine runs. It records observed relay identities and
immutable Goal artifacts; agent-authored hostnames are never treated as proof. Pairing bundles,
access tokens, and local credentials must not be added here.

## 2026-08-30 — hosted remote relay preflight

- Machine A was signed into its existing remote-mode account.
- A fresh `grid sync --json` returned five existing remote Grids but did not return `forge`;
  `grid engines forge --json` therefore failed with `Grid not found`.
- The already-visible live Grid `nd-task-e2e-pp-0830` had two online inference engines: an Apple M1
  Max MacBook Pro serving Claude models and a Linux server serving tool-capable Responses models.
- `grid goal list --grid nd-task-e2e-pp-0830 --all --json` reached its hosted relay and failed with
  `This grid's relay does not support Grid Goal yet.` This is the expected additive feature probe
  against a relay that has not deployed the private Goal branch; it is not a failed Goal attempt.
- Required next action: invite Machine A's login to `forge` with role `both`, deploy private relay
  PR #19 on the hosted master, and repeat the read-only Goal list probe before joining task workers.
- No IP address, SSH connection, LAN bundle, Goal row, or agent execution was used in this probe.

## 2026-08-31 — Forge three-machine branch rollout preflight

- Hosted permissioned-public Grid: `forge`. This is a remote-mode test; no LAN relay, SSH tunnel or
  copied pairing credential is part of the topology.
- Three physical execution machines were online: `forge-machine-a` (MacBook Pro, Apple M2 Max,
  64 GB, macOS arm64), `forge-machine-b` (AMD Radeon Pro 5500 XT, 8 GB, macOS x86_64), and
  `forge-gpu-2x4090` (two NVIDIA RTX 4090 D cards, 48 GB total VRAM, Linux).
- A separate hosted inference node named `Grid` advertised Responses routes for
  `deepseek/deepseek-v4-flash-0731` (1,048,576-token context) and `qwen/qwen3.8-27b`
  (1,000,000-token context). It is an inference server, not counted as a physical Goal executor.
- Public worker/CLI branch head `e571870d830d67bdc31a59db073161848c417ee2` passed 3,355 public
  tests and the 21-scenario cross-repository Goal matrix. Private relay head `b56f0fc` passed the
  171-test relay release bundle and the same matrix. Exact-head GitHub CI passed for both PRs.
- Machine A was respawned from the current clean public branch head with Codex-only task policy,
  task root `/private/tmp/fg-a`, and task claiming enabled. Machines B and C must fetch that same
  branch head and respawn before the physical Goal is created; a node merely remaining online is
  not evidence that its long-lived worker reloaded the checkout.
- The hosted relay still answered `This grid's relay does not support Grid Goal yet.` before its
  pending private branch deployment. Therefore no Forge Goal row or attempt was fabricated in this
  preflight; private relay deployment remains the physical gate.
- The candidate now snapshots Grid package version, clean Git revision, selected harness and native
  agent version onto every authenticated attempt start. The Forge acceptance command derives
  `--require-worker-revision "$(git rev-parse --short HEAD)"` from the shared checkout, so an old
  long-lived B/C worker or dirty checkout fails the evidence gate even if its node name appears
  online.

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

## 2026-08-30 — native Codex → Claude → Codex retry chain

This run proves mixed native harness continuation and fail-closed evaluation behavior across
distinct relay-signed Grid node identities. Both task workers happened to run on the relay-host
Mac because the second physical computer was offline, so this supplements rather than satisfies
the final two-physical-machine mixed-harness gate.

### Topology and workload

- Public worker commit: `fce01c2`; private relay commit: `7f16213`.
- Project: `72453d13-64ac-49a2-9194-b1fc1995eed7`.
- Goal: `46fa2a0b-4723-4bdd-aa69-11d4934c4344`.
- Exact Grid model: `Qwen3.6-35B-A3B`; inference node:
  `goal-b-c19853f3-8a8f-4a8d-a961-87e79b087a99`.
- Codex execution identity: `goal-machine-b-48759900-4980-4b1a-ae6b-926e62ddd835`.
- Claude execution identity: `goal-a-e1891074-24d0-4d3e-a67a-dba33e3eb377`.
- Objective: produce three ordered handoff phase files and a summary, with four immutable
  commit-pinned file evals.

### Handoff and failure sequence

- Codex completed turn `67ec49aa-e85d-493a-96bb-50789a6319ba` at commit
  `bb9cbdebaba4f3883ccd8444174944b4d227444b`; the Goal remained active.
- Codex claimed follow-up turn `b6bd871c-4a40-4be0-8ee7-03da30dc8ce2`, then its worker was
  stopped. The relay recorded attempt 1 as `lease_expired`. Its uncommitted files did not leak into
  the next attempt; Claude started from the last durable project commit.
- Claude reclaimed the same turn as attempt 2 and continued from Codex's committed phase. Claude's
  native `/goal` sentinel was present, but that run exited without a terminal native evaluator
  attachment. Grid therefore refused to accept Claude's self-reported completion and requeued with
  `native_harness_failure`.
- Before requeue, Grid preserved Claude's worktree commit
  `eb06c4b43272e13a2b72dcd0f393fbed476cde3f` and transcript commit
  `4ae649a0bd17cc7d9c3c3c7afc13453dbd087d20`.
- Codex reclaimed attempt 3, fetched both checkpoints, and completed at commit
  `497806ce1a401344d0f70746c3feb7cba0a92f14`. Evidence proves both checkpoint commits are ancestors
  of their corresponding final commits.

### Result and eval evidence

- Status: `complete`; two settled native Goal turns; 251,826 accounted Goal tokens.
- Evidence records all three attempts and both harnesses, plus four Grid-attributed inference
  summaries for Codex attempt 1, interrupted Codex attempt 1 of the follow-up, Claude attempt 2,
  and completing Codex attempt 3.
- The relay independently evaluated the exact final commit. All four checks scored `1.0`, were
  accepted, and verified the phase markers and final summary.
- The run exposed two operator hardening items now covered by regression tests: Ctrl-C on
  `grid task follow` must detach cleanly without a traceback or cancellation, and physical-lab
  preflight must compare signed node IDs rather than process names.

## 2026-08-30 — native Codex parent → Claude child → Codex fan-in

This run exercises the real built-in `grid_spawn_subgoal` tool, a native Claude child Goal, Git
fan-in, a resumed native Codex parent, and independent child and parent evals. The two signed task
identities ran on one physical Mac, so this is mixed-harness protocol evidence rather than the final
three-physical-machine gate.

### Defect found by the first run

- Parent Goal `20da64ec-c211-4fd5-ac79-f57c1bfe6eb5` spawned child
  `bab929ff-8cdc-4b33-b2ba-3f0ebcac7a15`, then its Codex worker was killed after the durable action.
- Attempt 2 reconstructed the same child objective with different optional eval fields. The old
  exact-body action hash treated it as new and spawned `5da80387-a163-4420-bb49-ba74085223cd`.
- Both children completed. Git correctly refused to silently choose between their conflicting
  `CHILD.md` files and blocked the parent, proving the fan-in conflict fence worked while exposing
  the action-identity bug.
- Public commit `6f85936` keys an internal child delegation by normalized objective within the
  stable parent turn. Distinct sibling work must use distinct objectives. A deterministic
  four-node cross-repository test now fails on the old behavior and proves a replacement Codex
  session can restate optional policy while receiving the original child id.

### Passing hierarchy

- Public worker commit: `6f85936`; private relay commit: `2bd0479`.
- Project: `edad7eea-4d14-4b75-ae05-b5cff2ad055f`.
- Parent Goal: `ca7eacfe-2bcc-41bb-95ee-e6babdb21335`; child Goal:
  `fa3f9630-f0cc-4faa-ae05-cbb220057077`.
- Codex execution identity: `goal-machine-b-48759900-4980-4b1a-ae6b-926e62ddd835`;
  Claude execution identity: `goal-a-e1891074-24d0-4d3e-a67a-dba33e3eb377`.
- Exact model: `Qwen3.6-35B-A3B`; inference identity:
  `goal-b-c19853f3-8a8f-4a8d-a961-87e79b087a99`.
- Codex parent turn `056e0b72-2f88-449c-af9d-0f7344d8959d` spawned exactly one child and ended
  immediately at commit `09c92d2ccd89f2e6890c8b132fb7147778d4eb34`; the parent entered
  `waiting_children` rather than polling a workspace that could not yet contain child output.
- Claude child turn `122cd254-4314-4a93-95f6-7f4e1e8e39aa` completed at commit
  `377e2cef5837d26effb1313572f5f13c606cd4b4`. Its independent file eval passed at score `1.0`.
- Grid merged the child branch and scheduled Codex parent turn
  `56c8b306-13e3-47c7-9aa7-bc09492ade10` from fan-in input commit
  `ecea0a73f1c0fa0affce646af8419d9ae4fed3c8`. Codex completed the parent at
  `a69ede94aa984ab0e68f45ec1791023c872b0a19`.
- All three immutable parent evals passed at score `1.0`: the merged child marker, the parent fan-in
  proof, and three exact JSON outcome checks.
- Accounting reconciled to 161,253 parent tokens plus 14,942 descendant tokens = 176,195 total;
  the child reservation returned to zero and the relation recorded one actual charge.
- Strict evidence verification passed separately for parent and child with required Grid inference.
  Parent snapshot: `f48eb5b06af477f86a622e5b3247e95c827c450a92f69ad5c23d6837469cd185`;
  child snapshot: `13734f5ad45d1b046ee0c240e97fa8f1c7a3145f0fec52450cbc6a983058916d`.

The attempted manual worker kill on the passing run landed after the first parent turn had already
settled, so it is not described as a live retry. The checked-in deterministic test covers the exact
post-spawn native failure window with four isolated node roots and proves two attempts emit one
stable action key, return one child id, and fan in one branch.

## 2026-08-30 — `nd-task-e2e-pp-0830` live preflight

This was a compatibility preflight, not a completed Goal, and is recorded explicitly so an older
relay or inference-only node is not mistaken for distributed Goal evidence.

- Remote membership sync discovered Grid `nd-task-e2e-pp-0830`
  (`grid-4a3faca05af44efb`, `permissioned-providers`).
- One online Linux inference node named `Grid` advertised Responses routes for
  `deepseek/deepseek-v4-flash-0731` (1,048,576-token context) and `qwen/qwen3.8-27b`
  (1,000,000-token context).
- `grid goal list --grid nd-task-e2e-pp-0830 --all --json` failed with the CLI's explicit rolling
  upgrade error: `This grid's relay does not support Grid Goal yet.`
- `grid engines` showed no task-only Codex or Claude worker. The online inference node is not
  counted as an agent executor.
- No Goal was created and no worker was joined after the failed preflight. The next valid run needs
  the private relay upgraded to the recorded release-candidate revision, then at least one native
  Goal-capable task worker joined with `--tasks-only`.

## 2026-08-31 — `forge` hosted remote preflight

This is the intended three-physical-machine remote-mode grid. It is not yet counted as a passing
Goal because the server preflight correctly stopped before creation.

- Remote sync discovered `forge` as `permissioned-public` with three physical nodes online:
  `forge-machine-a` (Apple M2 Max, 64 GiB, macOS arm64), `forge-machine-b` (AMD Radeon Pro 5500 XT,
  8 GiB, macOS x86_64), and `forge-gpu-2x4090` (two RTX 4090 D GPUs, Linux). A separate hosted
  inference server named `Grid` also advertised million-token Responses routes; it is not counted
  as one of the three execution machines.
- `grid goal list --grid forge --all --json` returned the explicit rolling-upgrade refusal:
  `This grid's relay does not support Grid Goal yet.` Membership and remote networking work; the
  hosted master is still running pre-Goal private relay code.
- A dedicated empty acceptance project was initialized successfully through the hosted project
  plane: `grid-goal-forge-acceptance-20260831`, id
  `c1b9a9cf-dc04-4b07-b7d5-94a7846b8a08`, trunk commit
  `031471c999ab9d48f16961f4c623e10fe7cf3b7a`. This proves project membership and Git bootstrap are
  ready without pretending the missing Goal route works.
- Two isolated initialized canary projects are also ready: Codex project
  `f42a27c2-432c-4095-826b-6c93109e7ca9` at trunk
  `4c5f2f20b2dfc90f1853bcd998a5f7a48fbc7327`, and Claude project
  `2c044f19-8765-48cd-8891-5fe11451b33e` at trunk
  `db418514f18fe29d36e65deccc60dd4da74ec75d`. Their fixed commands and independent eval are in the
  three-machine runbook; they prevent harness/model discovery from contaminating the game project.
- Private branch `grid-goal-distributed` is green at
  `2b1084a3c67ee6f81ea596d3a56414309df5b097`. It may be temporarily deployed before merge. Merely
  checking out the public branch on the three workers cannot add the missing hosted endpoints.
- No SSH, LAN address, local relay or pairing bundle was used. No Goal was created, so this is a
  deployment preflight rather than execution evidence.
- Machine A was then respawned from the public feature branch as `forge-machine-a` with
  `GRID_TASK_AGENT_KINDS=codex`, `max_tasks=1`, and isolated root `/private/tmp/fg-a`. The same
  remote node retained its existing inference engine and printed `tasks=on`; it is polling the
  existing distributed queue and will become Goal-eligible when the hosted relay advertises
  `goals/v1`. This is worker readiness, not a passing Goal.

## 2026-08-31 — authenticated business outcome matrix

- Public runtime/harness `11bece7bb0d3809ee3b28bc820da25d0d11e96eb` and private relay
  `2b1084a3c67ee6f81ea596d3a56414309df5b097` passed the complete 21-scenario cross-repository
  matrix in one uninterrupted 301.54-second invocation.
- In the upgraded support Goal, unauthorized node A spent zero attempts. Codex B observed ticket
  T-42, committed one idempotent reply, and died. Codex C reclaimed the exact turn from an isolated
  root, replayed without another side effect, and called a node-local `check_ticket` verify tool.
- C's first completion nomination deliberately failed its file/JSON evidence. A fresh turn repeated
  both the action reconciliation and authoritative verification. The business API recorded exactly
  one side effect and two verify reads.
- Grid stored six independent eval runs across the two nominations: the designed first-turn
  failures plus four passes. The final `verify` run was relay-authored and bound to C's exact turn,
  attempt, tool, arguments, call id, request sequence and result sequence. Offline verification
  independently selected the same exported request/result pair and recomputed all four predicates.

## 2026-08-31 — remote C launcher and 22-scenario rerun

- Public candidate `ecec3a4a5fa547271f75dc8fce7af5b4d1680917` and private relay
  `374a3125f9296e453d8a340975eea14a78ad1bd2` passed the complete 22-scenario cross-repository Goal
  matrix: 22/22 in 333.36 seconds.
- The first uninterrupted invocation passed 21/22 in 327.09 seconds. The failing Codex protocol-
  drift assertion observed a safe transient `running` state after one worker logged local
  quarantine while a sibling pre-quarantine long-poll was still declining its stale delivery. The
  gate now waits for all finite stale polls to drain while requiring the same turn at attempt 1;
  both Claude-to-Codex and Codex-to-Claude drift scenarios then passed together (2/2 in 24.06s).
- The Machine C launcher now has an integrated `--cloudflared` mode. It binds the relay only to
  loopback, discovers the generated HTTPS root, verifies the public health route before revealing
  either worker credential, drains tunnel logs, supervises relay and tunnel shutdown together, and
  refuses a credential-suppressing Quick Tunnel restart whose hostname would change.
- Offline verification now also binds native Codex cumulative turn/token counters to the accepted
  relay trajectory, rejects counter rollback or overstatement, rejects duplicate native verdicts,
  and checks optional Claude `/goal` sentinel uniqueness and ordering. The complete real matrix
  passed after these rules were enabled.
- The physical launcher suite passed 34/34; the focused Goal CLI/native-harness/launcher bundle
  passed 104/104; repository lint passed. The ordinary sandboxed full-suite attempt was discarded
  as release evidence because optional MLX aborted the interpreter and loopback-proxy tests were
  denied by the sandbox; the same focused proxy tests passed when loopback was permitted.
- This is software evidence, not the outstanding physical result. `forge-gpu-2x4090` is online as
  Machine C, but no disposable Goal relay has yet been launched on it and no A-to-B-to-C physical
  Goal was created in this entry.
