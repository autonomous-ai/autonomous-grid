# Deploying Grid Goal

Grid Goal is a two-layer rollout. Each Grid needs one Goal-capable relay/master, and each computer
that should execute Goal turns needs the Goal-capable public CLI plus an installed agent harness.
Inference-only nodes do not execute Goal turns and do not need to restart for this release.

## Release order

1. Deploy the private relay branch to one disposable or canary Grid.
2. Upgrade three canary task workers and pass
   [the three-machine acceptance test](goals-three-machine-acceptance.md).
3. Merge and release the private relay.
4. Upgrade Grid masters one at a time. Do not run old and new masters against the same database
   while startup migrations are running.
5. Merge and release the public CLI.
6. Upgrade and restart every task worker that should claim Goal turns.

This private-first order preserves ordinary distributed tasks throughout the rollout. A new public
CLI also reports an old relay as unsupported instead of silently treating a Goal as an ordinary
task.

## Upgrade one managed Grid master

Back up the Grid database, update the private CLI checkout or package on the master host, then run:

```bash
GRID_HOME=<creator-grid-home> grid network restart-server <grid-name>
```

The command restarts the relay/master and sync daemon while leaving PostgreSQL running. The new
master applies additive startup migrations. If the Grid is hosted elsewhere, run the equivalent
deployment on that master host; a member laptop cannot upgrade a remote Grid's relay.

Verify the relay advertises the Goal protocol:

```bash
curl -fsS <relay-url>/server/info | jq -e '.features | index("goals/v1")'
GRID_HOME=<member-grid-home> grid goal list --grid <grid-name> --all --json
```

An old relay omits `features` and the Goal command explains that the relay does not support Grid
Goal yet.

## Upgrade a task worker

Install or update the public CLI, install the first supported native harness, and restart the
task-only worker:

```bash
grid agent install codex
GRID_TASK_AGENT_KINDS=codex GRID_HOME=<member-grid-home> \
  grid join <grid-name> --tasks-only --name <worker-name> --max-tasks 1 \
  --tasks-root <durable-work-root>
```

Set `GRID_TASK_AGENT_KINDS=codex,claude` only on workers where both harnesses are installed and
verified. Use a durable work root with enough free disk for repository checkouts. Existing
inference engines stay connected and continue serving models; they do not need Goal code unless
they also execute agent tasks.

## Canary gate

Before widening the rollout, require all of the following on the canary Grid:

- the relay reports `goals/v1`;
- at least three distinct physical task workers are online;
- Codex creates a checkpoint, Claude continues it after an abrupt worker loss, and Codex finishes
  it after a second loss;
- the final independent eval passes and the Goal leaves the active task list;
- the evidence report identifies three physical nodes, two reclaimed leases, preserved native
  transcripts, Grid-routed inference, and zero outstanding token reservations.

See [Grid Goal merge readiness](goals-merge-readiness.md) for the current evidence and the exact
remaining release gate.

## Fleet rollout

The repositories currently provide per-Grid restart commands, not a fleet orchestrator. A fleet
deployment service must enumerate master hosts, back up and upgrade each Grid sequentially, verify
`goals/v1`, then roll the task workers. A failed canary should stop that rollout; restore the prior
package or checkout and restart the affected master rather than mixing two master versions against
one database.
