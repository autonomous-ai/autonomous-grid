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
grid agent install codex
codex --version
grid join <grid-name> --tasks --tasks-root /short/grid-tasks
```

Codex Goal workers require `codex-cli 0.150.1` or newer, the oldest release Grid has measured with
the persisted `thread/goal/*` and `thread/resume` app-server contract. Older or unreadable binaries
are not advertised to the distributed queue, so they cannot claim and strand a Goal.

The provider advertises each native Goal harness it can actually run. Restrict a node explicitly
with `GRID_TASK_AGENT_KINDS=codex` or `GRID_TASK_AGENT_KINDS=claude` when desired.
An empty, unsupported, or unavailable-only policy fails closed: the node retires task serving
without contacting the queue, while Grid inference remains online. Its local log names the policy
problem; restart task serving after correcting it.

The Goal's `--model` must name a model available through the Grid Responses API. Codex itself runs
on the provider; its model requests go back through Grid, so the machine executing the agent and the
machine serving the model may be different computers.

The native agent receives only a short-lived loopback proxy token, never the provider's Grid
credential. The proxy reads the provider's current node token for every model request. If that token
expires during a long Goal slice, it coordinates with the provider's normal refresh path and retries
the authentication refusal once; Claude evaluation calls and relay-internal subgoal actions use the
same live-token boundary.

## Run and control a Goal

```bash
# Set this in the service environment of every Codex node allowed to call the API. Exact origins
# only: no wildcards or paths. A node without this origin cannot claim the Goal.
export GRID_GOAL_TOOL_ORIGINS=http://support.internal

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
grid goal evidence <goal-id> --verify > goal-evidence.json
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
    {"type": "file", "name": "interactive page", "path": "index.html",
     "max_bytes": 20000, "contains": ["id=\"target\"", "script src=\"game.js\""]},
    {"type": "file", "name": "click updates score", "path": "game.js",
     "max_bytes": 20000, "contains": ["addEventListener('click'", "textContent"]},
    {"type": "file", "name": "styles", "path": "style.css", "min_bytes": 10},
    {"type": "file", "name": "instructions", "path": "README.md", "min_bytes": 10}
  ]
}
```

Each definition is immutable, and its hash includes the evaluator-semantics version. Every score is
stored with that definition hash, its turn, evaluator node, and exact Git commit. The guarded lease
transaction marks a score `accepted`; a stale provider's
completed evaluation remains visible as rejected audit evidence but cannot change Goal state or
enter authoritative training data. A failed accepted check keeps the Goal active and becomes
relay-authored guidance for the next worker. Evaluator infrastructure errors block the Goal and
remain `accepted: false`; after recovery, `grid goal resume` schedules a fresh nomination/eval.
With no eval manifest, native Goal completion remains the stopping decision.

A `sha256` file predicate must also declare `max_bytes`. The declared maxima across all SHA checks
may total at most 64 MiB. The relay streams those commit-pinned blobs through the hash instead of
buffering decompressed Git objects; size/existence checks read metadata only. This keeps a tiny,
highly compressed result push from expanding into unbounded evaluator memory.
A `contains` predicate requires one to sixteen unique literal UTF-8 strings and a `max_bytes` no
greater than 16 MiB; declared content-inspection maxima may total at most 64 MiB. Every literal must
occur in the exact commit-pinned blob. Grid searches the blob as a bounded stream, including matches
that cross stream chunks; it does not execute regexes or repository code on the relay.

When a different harness takes a later turn, Grid does not try to translate opaque Codex and Claude
session formats. It supplies the shared Git worktree plus a bounded relay-authored history of recent
turn outcomes, failed evals and child results. A first-time Claude worker includes that handoff in
the native `/goal` it creates, so switching harnesses does not silently reduce continuity to files
alone.

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
and independent child evals for every required child. The action's `required` field defaults to
`true`. Set it to `false` for bounded exploratory work: an optional child may omit evals, its failure
does not block the parent, and Grid merges its branch only if it completes cleanly. If a completed
optional branch conflicts, Grid skips it, records the conflicting paths on the child relation, and
includes that fact in the parent's next handoff. Each child is an ordinary
Goal conversation on the same distributed task table, so other machines can claim children
concurrently. The spawn action can select both a different harness and a different Grid-served
model for the child—for example, a Codex parent can delegate a bounded specialist task to Claude on
a cheaper model. If omitted, the child model inherits the parent's model.

The parent enters `waiting_children` after its turn checkpoints. It receives no new turn until every
child is terminal and all required children are complete, then resumes with every child id, status,
and eval summary. A failed or conflicting required child blocks the parent; a failed, missing, or
conflicting optional child is recorded in the resumed prompt without blocking it. Child slots and
token allocations are reserved atomically,
fan-out is limited to eight children, nesting is limited to three levels, and at least 1,000 parent
tokens remain for final fan-in. Subgoals are off by default because enabling them authorizes
autonomous parallel work and budget allocation.

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
child turn is exposed, so a retried tool call cannot publish an orphan or duplicate child. A
reserved child whose first turn was interrupted remains live—not failed—and the periodic
reconciler provisions it. The same sweep retries every `waiting_children` parent, including a Goal
that is itself a child, so a relay stop after child completion but before the fan-in callback cannot
strand the hierarchy. Concurrent relay reconcilers meet at Git's compare-and-swap; the loser rereads
the new parent tip and retries, where already-merged child commits are ancestry-idempotent.

## What moves between computers

A Goal is a conversation in the existing relay task table. One Codex Goal turn is one ordinary task
row with the same claim, lease, retry and reaper behavior as other distributed tasks.

At each successful turn boundary, the provider publishes two Git-backed checkpoints:

- the project tree on the conversation/task branches;
- native harness state on `refs/grid/agent/<goal-id>` (Codex state remains below
  `.grid/agent/codex`; Claude keeps its own opaque transcript files).

The worker reports the transcript commit it pushed, and the relay independently resolves the ref
before accepting the turn. Evidence records both the input and output transcript commits. The next
provider checks out the verified checkpoint before resuming its native thread; it does not fetch
another provider's filesystem, process memory or bearer token.

Codex embeds worker-local absolute rollout paths in its state database. Grid does not reuse those
paths: the checkpoint stores a path relative to `.grid/agent/codex/home`, and each successor rebases
it beneath its own task root before `thread/resume`. This is what allows nodes with different
`--tasks-root` values to resume the same native Goal.

If a provider disappears mid-turn, its lease expires and the relay requeues the same task row with
an incremented attempt number. The replacement starts from the last successful project and Codex
checkpoint. Work that existed only in the dead provider's uncommitted worktree is intentionally
discarded; external actions should therefore be idempotent.

A node-local failure before the native harness process starts (for example a binary removed after
capability advertisement or a local permission fault) follows the same bounded reclaim path instead
of terminally failing the Goal on one computer. If every capable node fails, the existing attempt
cap ends the Goal as `retries_exhausted` rather than retrying forever.

When Codex marks the Goal complete, the last task becomes terminal and no next task is queued. The
Goal disappears from the default `grid goal list`, while its Goal row, task attempts, events,
trajectory and counters remain available for audit and future `grid train` datasets. Unlike
ordinary task history, Goal branches and transcript refs do not expire by default. A relay that has
already exported them may set `GOAL_TRAJECTORY_RETENTION_SECONDS` to a positive retention window.
The evidence export is schema-versioned and includes each turn's prompt, output/error, harness,
execution node, inference usage, worktree commits, transcript input/output commits, recorded tool
events, and accepted or rejected independent eval runs. Inference usage is grouped by turn, exact
model, model-serving node, transaction state, Goal attempt, agent-executing node, and harness. The
attempt identity matters because a reclaimed turn keeps the same turn id while Codex on one machine
may be replaced by Claude on another. Failed requests remain useful failure evidence, but only
`completed` requests can prove that a turn actually executed through Grid inference.

Inference attribution is relay-enforced, not trusted from agent headers. If `X-Request-Id` names a
Goal turn, the request must come from that turn's current leased node, carry the matching
`X-Grid-Conversation`, and request the Goal's model. The relay checks once at ingress and again while
holding the turn row lock in the same database transaction that writes the inference row. A worker
that loses its lease during model routing therefore cannot manufacture training or release
evidence. A concrete Goal model is recorded exactly; an `auto`/effort Goal records the concrete
model Grid selected after validating the original routed model name.

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
      "record": "full",
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
      "record": "full",
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

Allowed modes are `observe`, `act` and `verify`. `observe` and `verify` are GET-only; `act` uses a
mutating method and receives an idempotency key. User manifests require absolute HTTP(S) URLs and
cannot contain headers, credentials, query strings, redirects, relative relay paths, or Grid's
reserved internal tool name. Grid emits an audit event for every request and result. By default
those events contain tool/call identity and success metadata only. Set
`"record": "full"` when the arguments and returned observation should become local Goal evidence
and future training data; credential-shaped object fields are recursively redacted and each stored
value is hard-bounded. Requests do not inherit proxy or `.netrc` credentials, redirects are not
followed, and arguments and response bodies are capped at 64 KiB. Business `act` calls carry a
deterministic key over the Goal, tool and canonical arguments, so the receiving API can reject the
same mutation after either worker failure or an independent-eval retry. Relay-internal actions are
turn-scoped because their authorization is tied to that lease. Grid credentials are never placed in
Codex's environment or stored in Git.

Tool request and result events are durability fences, not buffered console progress. Grid flushes
the request record before contacting the API. If that record cannot land, it does not make the
call. It flushes the result before returning it to Codex; if the API may have committed but the
result cannot be recorded, the turn fails and a replacement replays it with the same idempotency
key. This closes the crash window between a business side effect and the training/audit trajectory.
The relay overwrites every Goal tool event with the authenticated lease holder and live attempt; a
worker cannot claim another machine's action. Evidence records the non-secret key on both action
events so `grid goal evidence --verify` can require an exact request/result pair or prove that an
interrupted request was reconciled by a later attempt with the same key.

`GRID_GOAL_TOOL_ORIGINS` is a comma-separated, exact-origin allowlist controlled by each node
operator. It defaults to deny-all. Grid turns every manifest origin into an opaque scheduling
capability, so a Goal stays queued instead of being claimed by a node that cannot reach or is not
authorized for its business APIs. Configure the origin on every node eligible for failover. The
first MVP intentionally does not distribute arbitrary third-party secrets; business endpoints must
use network-local authentication or an operator-managed gateway. Only relay-authored built-ins,
such as `grid_spawn_subgoal`, may use the worker's Grid credential and relative relay URLs.

## Distributed handoff acceptance test

The cross-repository integration test starts the real private relay and three isolated provider
processes with three separate task roots. It kills A during feature 2 and B during features 3–4,
then proves B and C reclaimed the same relay rows and reconstructed only the last published Git
state. It also verifies the relay-authored transcript chain, so each turn's input checkpoint must
exactly equal the previous worker's published output checkpoint:

```bash
GRID_SRC_REPO=/path/to/autonomous-grid-cli uv run pytest \
  -q tests/e2e_cross_repo/e2e_goal.py -s
```

This is a single-host distributed-protocol emulation, not physical multi-machine proof. A release
candidate must also pass [the three-machine acceptance runbook](goals-three-machine-acceptance.md),
which forbids shared filesystems and records the node/lease/commit chain.

The automated scenarios record the logical nodes explicitly (each uses an isolated task root):

| Goal | Execution | Harnesses | Injected failure | Independent eval |
|---|---|---|---|---|
| Four-feature game | A -> B -> C | Codex -> Codex -> Codex | A dies in feature 2; B dies in 3–4 | HTML wiring, click/score behavior, styling and instructions |
| Mixed game | A -> B -> C | Codex -> Claude -> Codex | A and B die mid-feature | HTML wiring, click/score behavior, styling and instructions |
| Eval-repair game | A -> B -> C -> D | Codex -> Claude -> Codex -> Claude | A/B die; C nominates broken behavior | Failed C score plus passing D repair on exact commits |
| Crash-safe game | A -> B | Codex -> Codex | A's native harness crashes after writing partial work | HTML wiring, click/score behavior, styling and instructions |
| Crash-safe business action | A -> B | Codex -> Codex | API commits, then A's native harness crashes | One side effect; stable key; complete action evidence; passing proof |
| Image artifact | B polls; A executes | Claude rejected; Codex selected | Capability mismatch | PNG file and size |
| Support reply | A polls; B -> C execute | Codex | B dies after API commit; first eval fails | `DONE.md`; one API side effect |
| Required child | A parent; B child; C parent | Codex -> Claude -> Codex | Parent moves while child runs | Child and parent files |
| Optional child | A parent; B child; C parent | Codex | Child fails | Parent file; child failure retained |

Every scenario uses `fake-grid-model` so the test measures Grid's queue, agent harness, tool,
Git/checkpoint and eval protocols deterministically rather than model quality. The support-reply
scenario additionally proves an unauthorized but otherwise healthy node spends zero attempts, the
authorized successor restores Codex's dynamic tools, and all three writes (the failed attempt, its
lease retry, and the eval-repair turn) carry one identical idempotency key while the API performs
exactly one side effect. It also verifies each action event's relay-stamped turn, node and attempt.
The crash-safe game covers the complementary failure boundary to a killed machine: while A still
owns its lease, the supervisor flushes its native event history, publishes both worktree and
transcript checkpoints, and asks the relay to requeue the same turn immediately. B receives attempt
2 and proves it restored A's partial files and Codex thread. An abrupt process or machine loss cannot
make that handshake and therefore correctly falls back to the last checkpoint the relay had already
accepted.
Evidence includes a relay-computed `retry_checkpoint_chain` for every accepted native-harness
checkpoint. `grid goal evidence --verify` requires each retry event's exact worktree/transcript pins
to match the stored turn pins and proves both are Git-ancestral to that turn's final outputs. Every
retry also retains the relay-selected `previous_agent_kind` beside `previous_provider_id`; the turn
row can be reclaimed by a different harness, so its final `agent_kind` alone is not attempt history.
Attempt-start events are stamped from the live claim row, overriding any provider-supplied node,
attempt, or harness values before they enter training evidence.

Owner controls are ordered against checkpoint settlement, not treated as best-effort UI state. A
pause that races an accepted nonterminal checkpoint keeps its exact pins but makes the queued retry
unclaimable until resume. A cancel in the same window terminals the turn and rejects the late pins.
If the final allowed attempt has already terminalled as `retries_exhausted`, that terminal outcome
overrides a pause arriving in the short turn-to-Goal reconciliation gap; the Goal is `failed` and
cannot be resumed past its declared attempt budget.
