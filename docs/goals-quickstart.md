# Distributed Goals — Codex across Grid nodes

`grid goal` gives Codex a durable objective with one measurable stopping condition. It is remote
mode only. Model inference goes through Grid; agent execution moves among computers already serving
distributed tasks.

## Set up

Create or import a project exactly as for a distributed task:

```bash
grid mode remote
grid project create --name click-game
grid project init <project-id>
```

On every computer allowed to execute Goals, install Codex and run the normal task provider:

```bash
codex --version
grid join <grid-name> --tasks --tasks-root /short/grid-tasks
```

The provider advertises Codex capability only when the `codex` executable is present. Computers
without it continue serving Claude tasks and inference, but do not claim Codex Goal turns.

The Goal's `--model` must name a model available through the Grid Responses API. Codex itself runs
on the provider; its model requests go back through Grid, so the machine executing the agent and the
machine serving the model may be different computers.

## Run and control a Goal

```bash
grid goal run --project <project-id> \
  --objective "Build a small playable browser click game" \
  --done-when "index.html, game.js, style.css and README.md exist and the game works" \
  --model <model-name> \
  --token-budget 100000
```

The command prints a Goal id. Use that id to inspect or control the run:

```bash
grid goal list                    # active, paused and blocked
grid goal list --all              # includes ended Goal history
grid goal status <goal-id>
grid goal pause <goal-id>         # current leased turn may finish; no next turn is queued
grid goal resume <goal-id>
grid goal cancel <goal-id>        # ends the Goal and cancels queued/running work
```

`objective` says what to achieve. `done-when` is one clear, verifiable finish line. Codex owns the
decision to stop through its native Goal mechanism; Grid records the resulting native Goal status
and counters rather than grading the work with a second agent.

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

This is topology-equivalent to three machines but still runs on one test host. A release candidate
should repeat the scenario on three physical nodes to cover networking, sleep and process-manager
behavior outside the deterministic integration harness.
