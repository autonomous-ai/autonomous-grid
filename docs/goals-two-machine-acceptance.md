# Grid Goal two-machine acceptance

This is the first physical-machine gate for Grid Goal. It proves a Goal starts under native Codex
on machine A, survives the abrupt loss of A, and continues from relay-owned Git and trajectory
state under a native harness on machine B. It complements, but does not replace, the complete
[three-machine release gate](goals-three-machine-acceptance.md).

The game is only the workload. The actual metric for this gate is: one completed Goal, one exact
turn reclaimed after lease expiry, at least two relay-authenticated execution node ids, a continuous
Git/transcript chain, Grid-attributed inference for every settled turn, and accepted independent
evals on the final commit.

No SSH is used in this test. Each operator runs Grid locally on their own computer. HTTP, the
relay-owned Git endpoint, and the distributed task table are the only machine-to-machine paths.

## Rules

- Use two physical computers, two Grid node identities, and two local task roots.
- Do not share, sync, mount, copy, AirDrop, or otherwise transfer either task root.
- Machine A is Codex-only. Machine B is Claude-only for the mixed-harness run. If no model has both
  a tool-capable Responses route and a tool-capable Messages/chat route, first run Codex on both
  machines and record that limitation; do not pretend a Responses-only model proves Claude.
- Stop every unintended task provider for the project. Inference-only nodes may remain online.
- Start B's task provider only after A has accepted turn 1 and begun the following turn. This makes
  the first assignment deterministic instead of relying on scheduler luck.
- Interrupt A abruptly after the relay has durably recorded `task.attempt_started` for that next
  turn. Closing the laptop or killing the provider process is valid. `grid leave`, Ctrl-C, and a
  copied workspace are not valid simulations of abrupt loss.
- Treat relay evidence—not hostnames written by the agent—as the authority for node, harness,
  attempt, model, inference provider, commits, and eval results.

## 1. Preflight both computers

Both public checkouts must be clean and at the same pushed `grid-goal-distributed` commit. The
relay must run the matching private `grid-goal-distributed` commit. Record the SHAs before work.
Before installing or starting a task provider, prove that the selected relay actually has the Goal
routes:

```bash
uv run grid goal list --all --json
```

Any nonzero exit fails preflight. In particular, a response saying the relay does not support Grid
Goal means the provider branch is ahead of the hosted relay. Do not continue with a task-only relay
and call the result a Goal test. For this pre-merge gate, use the no-SSH disposable relay below;
after the matching relay is hosted, skip that subsection and use the hosted Grid normally.

### Pre-merge relay, without SSH

Machine B runs the matching relay checkout locally and stays awake. This is an acceptance-test
topology, not a production deployment and not a replacement for the hosted relay. On B, in a
separate clean private-repository checkout:

```bash
git clone --branch grid-goal-distributed \
  https://github.com/autonomous-ai/autonomous-grid-cli \
  /private/tmp/autonomous-grid-cli-goal-relay
cd /private/tmp/autonomous-grid-cli-goal-relay
uv sync
```

Then, from B's clean public `autonomous-grid` worktree, start the test relay in a terminal that will
remain open:

```bash
uv run python tests/e2e_cross_repo/physical_goal_lab.py relay \
  --relay-repo /private/tmp/autonomous-grid-cli-goal-relay \
  --root /private/tmp/grid-goal-physical
```

The helper discovers B's reachable address itself; no IP lookup and no remote login is normally
needed. If B has several network interfaces and discovery chooses one A cannot reach, pass
`--advertise-host <reachable-LAN-or-VPN-address>` explicitly. The helper starts the relay, creates
distinct short-lived identities for A and B, writes B's isolated Grid home, and prints one
disposable pairing bundle for A. It never copies a task root.

On A, from the same public commit:

```bash
uv run python tests/e2e_cross_repo/physical_goal_lab.py configure \
  --home /private/tmp/grid-goal-lab-a
```

Paste B's pairing bundle at the hidden prompt. It does not enter shell history. The two isolated
Grid homes for the rest of this run are:

```text
A: /private/tmp/grid-goal-lab-a
B: /private/tmp/grid-goal-physical/grid-home-b
```

Verify both machines through Grid itself:

```bash
# A
GRID_HOME=/private/tmp/grid-goal-lab-a uv run grid goal list --all --json

# B
GRID_HOME=/private/tmp/grid-goal-physical/grid-home-b \
  uv run grid goal list --all --json
```

Both calls must return the same Goal table. Keep B's relay terminal open. All later commands in
this document must use the machine's isolated `GRID_HOME` prefix and the Grid name
`goal-physical`. Do not run `grid login` or `grid use` inside these homes; pairing already wrote the
exact disposable Grid record. Model inference must still be provided by an engine joined to this
test Grid—running the relay does not make the relay an inference engine.

On machine A:

```bash
cd /path/to/autonomous-grid
git status --short
git rev-parse HEAD
codex --version
uv run grid agent install codex
uv run grid agent status
```

On machine B, prepare but do not join task serving yet:

```bash
cd /path/to/autonomous-grid
git status --short
git fetch origin
git switch grid-goal-distributed
git pull --ff-only
uv sync
uv run grid mode remote
uv run grid login
uv run grid use <grid>
mkdir -p /private/tmp/grid-goal-b
chmod 700 /private/tmp/grid-goal-b
git rev-parse HEAD
codex --version
claude --version
uv run grid agent install codex
uv run grid agent status
uv run grid info <grid>
uv run grid models <grid> --verbose
uv run grid engines <grid> --json
```

For the disposable pre-merge topology, omit `grid mode remote`, `grid login`, and `grid use` above,
and prefix the remaining Grid commands with B's isolated `GRID_HOME` as shown in the preceding
subsection.

Do not discard local changes to make this preflight pass. Stop and preserve them. Do not start B's
task provider yet; a provider already serving tasks must be removed from this isolated test before
the Goal is created.

## 2. Select the model and harness pair

Choose one concrete model from `grid models <grid> --verbose`. For Codex to run, the model must
have a live tool-capable Responses path. For Claude to run, the same model must have a live
tool-capable Messages/chat path. Validate the native binaries, not merely their filenames:

```bash
# A must report Codex installed and native Goal capable.
uv run grid agent status

# B must report Claude installed; the runtime canary remains authoritative because Claude has no
# static native-Goal protocol schema.
claude --version
```

If the model is not compatible with both dialects, set B to Codex for the first run. Run the mixed
Codex-to-Claude variant later after a compatible route is present.

## 3. Start only machine A

Use a unique short path on A. If A already has the selected inference engine joined, include
`--respawn` so the explicit task settings reach the detached provider.

```bash
mkdir -p /private/tmp/grid-goal-a
chmod 700 /private/tmp/grid-goal-a

GRID_TASK_AGENT_KINDS=codex \
uv run grid join <grid> \
  --api codex \
  --tasks \
  --max-tasks 1 \
  --tasks-root /private/tmp/grid-goal-a \
  --name goal-worker-a \
  --respawn
```

Confirm that exactly A is task-serving. Save the output:

```bash
uv run grid engines <grid> --json > two-machine-engines-before.json
```

## 4. Create an empty project and the staged Goal

```bash
uv run grid project create --name grid-courier-two-machine --json
uv run grid project init <project-id>
```

Start the Goal from the public repository root so the checked-in eval path resolves:

```bash
uv run grid goal run --project <project-id> \
  --name grid-courier-two-machine \
  --objective "Build a dependency-free browser game named Grid Courier. Work in durable stages. In the first native Goal slice, create the page, styling, core movement loop, PLAN.md, and HANDOFF.md containing A_ACCEPTED_CHECKPOINT; deliberately leave collision, scoring, lives, restart, persistence, tests, and final documentation unfinished so the native Goal remains active. In later slices, inspect the existing files and relay handoff before changing them; add B_RECONSTRUCTED_CHECKPOINT only after observing A_ACCEPTED_CHECKPOINT, then finish every remaining feature and nominate completion. Do not claim a machine identity from the prompt: Grid evidence is authoritative." \
  --done-when "The game is playable with keyboard and pointer controls, collision, score, lives, restart, persistent high score, responsive accessible styling, instructions, and browser-independent tests; every attached eval passes and HANDOFF.md contains both staged checkpoint markers." \
  --model <model> \
  --token-budget 120000 \
  --agent auto \
  --evals docs/fixtures/two-machine-game-evals.json \
  --json
```

Record the returned Goal id. Reusing this command after an uncertain response requires the exact
idempotency key printed by the failure; never create a second Goal merely because an acknowledgement
was lost.

## 5. Wait for A's accepted checkpoint

Poll without `--verify` while the Goal is active:

```bash
uv run grid goal status <goal-id> --json
uv run grid goal evidence <goal-id> > two-machine-before-loss.json
```

Do not interrupt A until all of these are true in relay evidence:

- A has a relay-stamped `task.attempt_started` for attempt 1.
- One completed turn from A has a non-null `result_commit` and `transcript_result_commit`.
- The Goal is still active and the next turn exists.
- A has a relay-stamped `task.attempt_started` for attempt 1 of that next turn.

Inspect the accepted project state from the relay, not A's task root:

```bash
uv run grid project file --project <project-id> HANDOFF.md
```

It must contain `A_ACCEPTED_CHECKPOINT` and must not contain
`B_RECONSTRUCTED_CHECKPOINT`.

## 6. Lose A, then start B

Close or power off A immediately. Do not run `grid leave`, and do not copy its task root. On B,
start exactly one task worker from the empty B root.

Mixed-harness variant:

```bash
GRID_TASK_AGENT_KINDS=claude \
uv run grid join <grid> \
  --api claude \
  --tasks \
  --max-tasks 1 \
  --tasks-root /private/tmp/grid-goal-b \
  --name goal-worker-b
```

Codex-on-both fallback:

```bash
GRID_TASK_AGENT_KINDS=codex \
uv run grid join <grid> \
  --api codex \
  --tasks \
  --max-tasks 1 \
  --tasks-root /private/tmp/grid-goal-b \
  --name goal-worker-b
```

Wait for lease expiry and completion. Do not manually clone or seed the Goal repository. B must
fetch the pinned worktree and transcript refs through the relay Git endpoint.

## 7. Verify independently

```bash
uv run grid goal status <goal-id> --json
uv run grid goal evidence <goal-id> --verify \
  --min-execution-nodes 2 \
  --require-inference > two-machine-final-evidence.json
uv run grid goal list --json
uv run grid project download --project <project-id> --output grid-courier-result.zip
```

The verifier must exit zero. Then inspect `two-machine-final-evidence.json` and require:

- The interrupted turn id is unchanged and settles at attempt 2 on B.
- Its authoritative retry says `lease_expired`, names A as `previous_provider_id`, and names the
  expected previous harness.
- The retry predecessor has a matching relay-stamped `task.attempt_started`; an uncorroborated
  retry label cannot manufacture a second execution node.
- B's input commit is A's accepted result lineage, and the worktree chain is ancestral.
- The transcript input/output pins form a continuous chain. For a harness switch, Grid preserves
  shared trajectory/history while each native harness keeps its own opaque session format.
- Every settled turn has completed inference attributed to a Grid inference node and to the exact
  requested model. Agent execution and inference may legitimately name different nodes.
- Every final eval row is relay-authored, matches its immutable definition hash and final result
  commit, and is accepted with a passing score.
- The Goal is `complete`, `grid goal list --json` contains no active copy, and no queued/running
  task remains for its conversation.
- The final `HANDOFF.md` contains both markers. This is useful workload continuity evidence, but
  it never substitutes for the relay-authored node/attempt/commit chain above.

Unzip the result outside both task roots and run the agent-authored behavior tests as an additional
review, not as a substitute for the relay eval:

```bash
unzip grid-courier-result.zip -d /private/tmp/grid-courier-review
cd /private/tmp/grid-courier-review
node --test tests/game.test.mjs
python3 -m http.server 8000
```

Open the served game, exercise keyboard and pointer controls, lose all lives, restart, reload, and
confirm the high score persists. Record the test output and reviewer result beside the evidence.

## Failure conditions

Fail the test if A completes the entire Goal before B joins, B starts at attempt 1 of a new turn
instead of reclaiming A's interrupted turn, B needs A's disk, a checkpoint or transcript fetch
silently falls back to fresh state, either marker is missing, the final eval is not tied to the
exact result commit, the evidence verifier fails, or any Goal work remains active after completion.

Also fail the mixed-harness claim if the evidence does not explicitly show Codex on A and Claude
on B. A Claude executable present on B, or a model name that sounds compatible, is not proof.
