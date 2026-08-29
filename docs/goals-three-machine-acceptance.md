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
   file evals for `index.html`, `game.js`, `style.css`, and `README.md`.
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

## Required evidence

Save the relay-authored JSON artifact with `grid goal evidence <goal-id> > goal-evidence.json`.
A release is not accepted from screenshots alone.

| Evidence | Required assertion |
|---|---|
| Goal | id, objective, done condition, allowed harnesses, eval definition hashes |
| Machine inventory | physical hostname, Grid node id, OS, harness and harness version for A/B/C |
| Model assignment | requested model and every Grid inference provider node used per turn |
| Turn 1 | A node id, Codex, attempt 1, input commit, result commit |
| Turn 2 | A attempt 1 lease expiry; B attempt 2, Claude, same turn id, result commit |
| Turn 3 | B attempt 1 lease expiry; C attempt 2, Codex, same turn id, result commit |
| Isolation | both uncommitted markers absent on replacement machines and final tree |
| Evaluation | each definition hash, evaluator node, exact result commit, score and evidence |
| Terminal state | Goal `complete`; zero queued/running turns for its conversation |

The commit chain must be ancestral and every replacement must fetch through the relay's Git URL.
Opaque Codex and Claude histories may remain harness-specific; cross-harness continuity comes from
the shared committed tree, Goal metadata, events, and relay-authored eval evidence. A same-harness
worker should additionally restore that harness's private transcript side-ref.

Every terminal evaluation row used as proof must have `accepted: true`. A row with
`accepted: false` proves only that a stale provider evaluated something after losing its lease; it
must remain in the audit record and must not be counted toward completion or future training data.

## Failure criteria

Fail the release if a replacement needs the previous laptop, sees an uncommitted marker, receives a
new turn id instead of reclaiming the expired row, completes without the independent eval, loses a
successful committed checkpoint, exposes a Grid bearer token to the agent process, or leaves active
work after terminal completion.
