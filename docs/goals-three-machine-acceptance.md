# Grid Goal three-machine acceptance

This is the release gate for multi-machine Goal failover. The single-host E2E suite is necessary
protocol coverage, but it does not prove networking, laptop sleep, process supervision, or clean
reconstruction on another computer.

## Topology

- One reachable Grid relay and project with an initialized trunk.
- Machine A: a Grid task provider advertising only Codex.
- Machine B: a different task provider advertising only Claude Code.
- Machine C: a third provider advertising only Codex.
- Three different node identities and three local task roots on locally attached disks.
- No NFS, SMB, synced folder, shared container volume, or manual copying between task roots.
- All three providers point at the same relay. Agent model requests point back through Grid
  inference; record the model-serving node separately from the agent-executing node.

## Version and endpoint preflight

Install the same release-candidate commits on the relay and every provider. Record both repository
SHAs in the evidence artifact; a source checkout on one node and an older packaged binary on another
is a failed preflight, not a compatibility test.

Before creating work, the owner machine must get a non-404 response from:

```bash
grid goal list --all --json
```

On A and C, `grid agent status` must report Codex installed. On B, `claude --version` must succeed;
the Claude-only canary below is the authoritative native-Goal wiring check. Stop every other
task-serving provider for this project during the run; inference-only engines may stay online. The
three intended task nodes must use distinct node ids and the expected harness allowlist below.

Start each worker with an explicit harness policy and unique local root:

```bash
# A and C
GRID_TASK_AGENT_KINDS=codex grid join <grid> --tasks --tasks-root <local-path>

# B
GRID_TASK_AGENT_KINDS=claude grid join <grid> --tasks --tasks-root <local-path>
```

Do not continue until a Codex-only canary Goal is claimed by A or C and a Claude-only canary Goal is
claimed by B. Cancel both canaries and save their evidence. This detects a worker that is online for
inference but not actually polling the distributed task queue.

## Scenario

1. Import or initialize the game project and start a Goal allowing both harnesses. Attach immutable
   file evals for `index.html`, `game.js`, `style.css`, and `README.md`; require bounded literal
   evidence for the HTML/JS wiring, click handler, score update, and visible styling rather than
   accepting filenames alone.
2. Leave only A serving tasks. Confirm A completes feature 1, receives turn 2, and creates an
   uncommitted marker while working on feature 2.
3. Close or power off A. Do not stop it gracefully and do not copy its task root.
4. Start B. After the lease expires, confirm B reclaims the same turn id at attempt 2, sees feature
   1 from Git, and cannot see A's uncommitted marker. Let B finish feature 2 and start turn 3.
5. Create another uncommitted marker on B, then close or power off B without a graceful shutdown.
6. Start C. Confirm C reclaims the same turn 3 at attempt 2, sees committed work from A and B, sees
   neither uncommitted marker, finishes features 3 and 4, and nominates completion.
7. Confirm Grid's independent checks pass against C's exact result commit. Confirm the Goal becomes
   `complete` and no active Goal task remains.

## Detected native-harness crash scenario

Run a second Goal to verify recovery while the machine remains alive but Codex fails after spawning:

1. Leave only A serving, start a Codex Goal, and make the native harness fail after creating partial
   committed files and native thread history but before a terminal Goal verdict.
2. Confirm A's supervisor publishes exact worktree and transcript checkpoint pins and the relay
   immediately requeues the same turn with reason `native_harness_failure`; do not wait for lease
   expiry.
3. Withdraw A and start C from an empty local task root. Confirm C claims attempt 2, receives both
   accepted pins, restores the partial files and Codex thread beneath C's own root, and completes.
4. Verify the final evals and evidence as below with `--min-execution-nodes 2`.

This is deliberately separate from powering off A: an abruptly lost machine cannot publish state
that the relay never accepted. After abrupt loss, replacement workers must ignore unacknowledged
partial pushes and resume only the prior accepted checkpoint.

## Required evidence

Save and verify the relay-authored JSON artifact with:

```bash
grid goal evidence <goal-id> --verify \
  --min-execution-nodes 3 --require-inference > goal-evidence.json
```

The command exits nonzero if the Goal is not complete, fewer than three task nodes executed it, any
turn lacks a completed request attributed to a Grid inference node, a concrete-model Goal has an
attributed request naming a different model, a reclaimed turn lacks authoritative retry
evidence, a transcript handoff is broken, a prior result is not Git-ancestral to the next pinned
input, or a required final eval has no accepted passing run. Failed/in-flight inference rows remain
audit evidence but never satisfy this gate. For an `auto`/effort Goal, the evidence names the actual
model selected by Grid. The relay admits Goal-attributed inference only from the current leased task
node with the matching conversation and requested Goal model, and repeats that check atomically at
transaction insertion so a lease lost during routing cannot leave forged evidence. A release is
not accepted from screenshots alone.

| Evidence | Required assertion |
|---|---|
| Goal | id, objective, done condition, allowed harnesses, eval definition hashes |
| Machine inventory | physical hostname, Grid node id, OS, harness and harness version for A/B/C |
| Model assignment | requested model and every Grid inference provider node used per turn |
| Inference attempt | each request group's Goal attempt, agent execution node and Codex/Claude harness |
| Turn 1 | A node id, Codex, attempt 1, input/result commits, null transcript input, non-null transcript output |
| Turn 2 | Retry names A and its Codex harness; B attempt 2, Claude, same turn id, result commit and transcript input/output |
| Turn 3 | Retry names B and its Claude harness; C attempt 2, Codex, same turn id, result commit and transcript input/output |
| Isolation | both uncommitted markers absent on replacement machines and final tree |
| Native path portability | B and C resume Codex using rollout paths beneath their own distinct task roots; A's absolute path is never reused |
| Detected harness crash | Retry reason is `native_harness_failure`; accepted worktree/transcript checkpoint pins become attempt 2 inputs |
| Evaluation | each definition hash, evaluator node, exact result commit, score and evidence |
| Terminal state | Goal `complete`; zero queued/running turns for its conversation |

The worktree commit chain must be ancestral and every replacement must fetch through the relay's
Git URL. The evidence must also form an exact transcript chain: turn 2's `transcript_commit` equals
turn 1's `transcript_result_commit`, and turn 3's input equals turn 2's output. The relay resolves
each output from `refs/grid/agent/<goal-id>` rather than trusting the worker's report. Opaque Codex
and Claude records inside that checkpoint remain harness-specific; a returning harness restores its
own native state while every harness receives the shared tree, Goal metadata and event history.
For detected native-harness retries, the evidence artifact's `retry_checkpoint_chain` must match the
retry event sequence and exact accepted pins, with both `worktree_ancestor` and
`transcript_ancestor` true against the turn's final result commits.

For each reclaimed turn, use the relay-authored `task.retry.previous_provider_id` and
`previous_agent_kind` as the authority for the machine and harness that disappeared, and the
settled turn's `provider_node_id` plus `agent_kind` as the authority for its replacement. A
`task.attempt_started` event is useful corroboration but is not required for the killed attempt: an
abrupt power loss can happen before that best-effort event is flushed. When it does arrive, the
relay overwrites its node, attempt, and harness fields from the live claim. `--min-execution-nodes`
counts both the settled providers and relay-authored retry predecessors; it never counts a start
marker. The replacement's attempt number must still be 2 and the retry reason must be
`lease_expired`.

Every terminal evaluation row used as proof must belong to the final turn and final result commit,
match both the immutable definition id and hash, name its evaluator, and have `accepted: true` with
an acceptance timestamp. A row with
`accepted: false` proves only that a stale provider evaluated something after losing its lease; it
must remain in the audit record and must not be counted toward completion or future training data.
`grid goal evidence --verify` also recomputes each exported definition hash. The release fails if a
file evaluator names anything other than the relay evaluator, even when its score says it passed.

## Failure criteria

Fail the release if a replacement needs the previous laptop, sees an uncommitted marker, receives a
new turn id instead of reclaiming the expired row, completes without the independent eval, loses a
successful committed checkpoint, exposes a Grid bearer token to the agent process, or leaves active
work after terminal completion. Also fail if pause discards an accepted checkpoint, cancellation
publishes a checkpoint that lost its lease race, or a paused Goal can be resumed after its final
attempt already ended with `retries_exhausted`. Pause a separate Goal while its final passing eval
is in flight; after settlement it must remain visibly paused, then resume directly to `complete`
without adding a turn or claimable task.
