# Distributed Goals — native agents across Grid nodes

`grid goal` gives an agent a durable objective with one measurable stopping condition. It is remote
mode only. Model inference goes through Grid; agent execution moves among computers already serving
distributed tasks.

## Set up

Create or import a project exactly as for a distributed task:

```bash
grid mode remote
grid project create --name click-game
grid project init <project-id>
```

On every computer allowed to execute Goals, install Codex or Claude Code and run the normal task
provider:

```bash
codex --version
grid join <grid-name> --tasks --tasks-root /short/grid-tasks
```

The provider advertises each native Goal harness it can actually run. Restrict a node explicitly
with `GRID_TASK_AGENT_KINDS=codex` or `GRID_TASK_AGENT_KINDS=claude` when desired.

The Goal's `--model` must name a model available through the Grid Responses API. Codex itself runs
on the provider; its model requests go back through Grid, so the machine executing the agent and the
machine serving the model may be different computers.

## Run and control a Goal

```bash
grid goal run --project <project-id> \
  --objective "Build a small playable browser click game" \
  --done-when "index.html, game.js, style.css and README.md exist and the game works" \
  --model <model-name> \
  --token-budget 100000 \
  --agent auto \
  --evals ./game-evals.json
```

The command prints a Goal id. Use that id to inspect or control the run:

```bash
grid goal list                    # active, paused and blocked
grid goal list --all              # includes ended Goal history
grid goal status <goal-id>
grid goal evidence <goal-id> > goal-evidence.json
grid goal pause <goal-id>         # current leased turn may finish; no next turn is queued
grid goal resume <goal-id>
grid goal cancel <goal-id>        # ends the Goal and cancels queued/running work
```

For a Goal with children, pause and cancel are hierarchical. Pause lets already leased slices end
but prevents every live descendant from receiving another turn; resume restores only descendants
paused by that parent, so a child paused independently stays paused. Cancel recursively terminals
queued/running descendants, including a child whose relay Git preparation was in flight.

`objective` says what to achieve. `done-when` is one clear, verifiable finish line. The native Goal
mechanism decides when to nominate completion. If independent evals are configured, Grid checks the
exact result commit and is the only component allowed to accept that nomination.

For example, `game-evals.json` can require artifacts without trusting the acting agent's report:

```json
{
  "version": 1,
  "evals": [
    {"type": "file", "name": "page", "path": "index.html", "min_bytes": 10},
    {"type": "file", "name": "logic", "path": "game.js", "min_bytes": 10},
    {"type": "file", "name": "styles", "path": "style.css", "min_bytes": 10},
    {"type": "file", "name": "instructions", "path": "README.md", "min_bytes": 10}
  ]
}
```

Each definition is immutable. Every score is stored with its definition hash, turn, evaluator node,
and exact Git commit. The guarded lease transaction marks a score `accepted`; a stale provider's
completed evaluation remains visible as rejected audit evidence but cannot change Goal state or
enter authoritative training data. A failed accepted check keeps the Goal active and becomes
relay-authored guidance for the next worker. With no eval manifest, native Goal completion remains
the stopping decision.

## Distributed child Goals

Opt in when a parent is allowed to fan work out to other Grid agents:

```bash
grid goal run --project <project-id> \
  --objective "Ship the browser game" \
  --done-when "implementation, instructions and independent checks pass" \
  --model <model-name> --allow-subgoals --agent codex
```

Codex then receives a built-in `grid_spawn_subgoal` action. The relay accepts it only from the node
currently leasing that parent turn and requires an idempotency key, a bounded child token budget,
and independent child evals. Each child is an ordinary Goal conversation on the same distributed
task table, so other machines can claim children concurrently; a child may use Claude even though
the spawning parent uses Codex.

The parent enters `waiting_children` after its turn checkpoints. It receives no new turn until all
required children are complete, then resumes with child ids, statuses, and eval summaries. Child
slots and token allocations are reserved atomically, fan-out is limited to eight children, nesting
is limited to three levels, and at least 1,000 parent tokens remain for final fan-in. Subgoals are
off by default because enabling them authorizes autonomous parallel work and budget allocation.

Harness capabilities are scheduled honestly:

| Harness | Native Goal | Goal HTTP tools | Spawn child Goals |
|---|---:|---:|---:|
| Codex | yes | yes | yes |
| Claude Code | yes | no | no |

`--agent auto` may hand ordinary child work to Claude, but a Goal requiring tools or subgoal spawn
waits for a compatible Codex node. Environment capability declarations cannot override these three
runner-wiring facts; custom harness-native capabilities remain operator-declarable.

Before resuming, Grid merges every completed child's conversation branch into the parent's branch
in the bare relay repository. Clean sibling changes therefore appear in the parent's next pinned
input commit, rather than only in its prompt. A conflicting child leaves the parent `blocked` with
the child id and conflicting paths; Grid never resumes the parent from a tree that omitted the
child's accepted work. Child turns never publish directly to global `main`: they advance only their
child branch, parent fan-in accepts them, and the root Goal alone enters the ordinary trunk-apply
pipeline. Spawn identity, immutable spec, dependency edge and budget reservation commit before the
child turn is exposed, so a retried tool call cannot publish an orphan or duplicate child.

## What moves between computers

A Goal is a conversation in the existing relay task table. One Codex Goal turn is one ordinary task
row with the same claim, lease, retry and reaper behavior as other distributed tasks.

At each successful turn boundary, the provider publishes two Git-backed checkpoints:

- the project tree on the conversation/task branches;
- Codex's durable thread state below `.grid/agent/codex` on the private agent side-ref.

The next provider checks out both before resuming the same Codex thread. It does not fetch another
provider's filesystem, process memory or bearer token.

If a provider disappears mid-turn, its lease expires and the relay requeues the same task row with
an incremented attempt number. The replacement starts from the last successful project and Codex
checkpoint. Work that existed only in the dead provider's uncommitted worktree is intentionally
discarded; external actions should therefore be idempotent.

When Codex marks the Goal complete, the last task becomes terminal and no next task is queued. The
Goal disappears from the default `grid goal list`, while its Goal row, task attempts, events,
trajectory and counters remain available for audit and future `grid train` datasets.

## Give Codex business read/write tools

The project repository is Codex's file observation/action surface. For business systems, pass a JSON
manifest with explicit HTTP capabilities:

```json
{
  "version": 1,
  "tools": [
    {
      "name": "read_ticket",
      "mode": "observe",
      "description": "Read one customer ticket",
      "input_schema": {
        "type": "object",
        "properties": {"ticket_id": {"type": "string"}},
        "required": ["ticket_id"]
      },
      "http": {"method": "GET", "url": "http://support.internal/tickets/read"}
    },
    {
      "name": "send_reply",
      "mode": "act",
      "description": "Send an approved reply",
      "input_schema": {
        "type": "object",
        "properties": {
          "ticket_id": {"type": "string"},
          "reply": {"type": "string"}
        },
        "required": ["ticket_id", "reply"]
      },
      "http": {"method": "POST", "url": "http://support.internal/tickets/reply"}
    }
  ]
}
```

```bash
grid goal run --project <project-id> \
  --objective "Resolve every ticket in the assigned queue" \
  --done-when "the queue reports zero assigned unresolved tickets" \
  --model <model-name> --tools ./support-tools.json
```

Allowed modes are `observe`, `act` and `verify`. Grid emits an audit event for every request and
result. `act` calls carry a deterministic idempotency key scoped to the Goal turn, so the receiving
API can reject a duplicate after a worker failure. Grid credentials are never placed in Codex's
environment or stored in Git.

The first MVP intentionally does not distribute arbitrary third-party secrets. A tool can request
Grid authentication only for a URL on the selected relay origin; other internal endpoints must
handle their own network-local authentication.

## Distributed handoff acceptance test

The cross-repository integration test starts the real private relay and three isolated provider
processes with three separate task roots. It kills A during feature 2 and B during features 3–4,
then proves B and C reclaimed the same relay rows and reconstructed only the last published Git
state:

```bash
GRID_SRC_REPO=/path/to/autonomous-grid-cli uv run pytest \
  -q tests/e2e_cross_repo/e2e_goal.py -s
```

This is a single-host distributed-protocol emulation, not physical multi-machine proof. A release
candidate must also pass [the three-machine acceptance runbook](goals-three-machine-acceptance.md),
which forbids shared filesystems and records the node/lease/commit chain.
