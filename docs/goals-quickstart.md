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
Because Codex app-server is experimental, the version is only the first gate. Grid also asks the
exact installed executable to generate its experimental protocol schema under a temporary
`CODEX_HOME` and verifies every lifecycle, Goal, event and dynamic-tool method the runner uses.
The result is cached by executable revision, so task polling does not repeatedly launch the probe.
If a method nevertheless returns JSON-RPC `method not found`, or Codex reports a native Goal status
Grid cannot interpret, that executable revision is quarantined in the provider process: the leased
turn is handed back through the bounded retry path and the node stops advertising `native_goal`
before it can consume the remaining attempts. Updating the executable creates a new revision and
reruns the probe; a running Codex-only task poller backs off while quarantined and automatically
rejoins when that repaired revision passes, without taking Grid inference offline. `grid agent
status` must say `Codex: installed` before physical acceptance.

Claude Goal workers require Claude Code `2.1.239` or newer, the first measured release that restores
an active native `/goal` through every `--resume` route Grid uses. Claude exposes no equivalent
machine-readable Goal schema, so Grid combines that measured floor with runtime fail-closed
validation: a cleanly exited slice must emit the native `goal_status` evaluator attachment, and a
terminal attachment cannot say the condition is both met and impossible. Deterministic attachment
drift quarantines only that executable revision from `native_goal`; the same Claude installation
can still claim ordinary tasks, and replacing or repairing it automatically reruns admission.

Capability admission is rechecked after claim delivery and before Grid records
`task.attempt_started`. This matters when one node runs several task workers: a second long-poll may
still hold the capability snapshot from just before its sibling quarantined Codex or Claude. Grid
declines that exact delivered lease, identified by an opaque random claim id, and the relay returns
the turn to the queue with its attempt counter restored. No checkout, native process, model request,
tool call, or business action starts. A delayed duplicate cannot revoke a newer lease, and a decline
is refused once attempt-start exists; failures after that fence use the ordinary checkpoint/retry
protocol instead.

The provider advertises each native Goal harness it can actually run. Restrict a node explicitly
with `GRID_TASK_AGENT_KINDS=codex` or `GRID_TASK_AGENT_KINDS=claude` when desired.
An empty or wholly unsupported policy fails closed: the node retires task serving without contacting
the queue, while Grid inference remains online. A valid configured harness whose binary is
temporarily absent or quarantined takes a paced local suspension instead; installing or replacing
the executable makes the running provider rejoin task claims without restarting inference.

Claude subscription capacity is harness-specific. A rejected Claude rate-limit window withdraws
Claude from subsequent claims until its stated reset, but a mixed node continues advertising Codex
and can execute Codex Goals through Grid inference. The heartbeat reports the provider as fully
task-paused only when no independent Codex harness remains available; it never tells the team that
the whole node withdrew while Codex is still claiming work.

The Goal's `--model` must name a tool-capable model available through the Grid endpoint used by an
allowed harness: Responses for Codex, or the translated Messages/chat path for Claude Code. Codex
or Claude runs on the task provider; its model requests go back through Grid, so the machine
executing the agent and the machine serving the model may be different computers.

Inference unavailability is a queue condition, not an agent failure. If no live, healthy route can
serve the requested model and harness dialect—including when every matching route is temporarily
demoted, model-pruned, or explicitly reports exhausted quota—Grid leaves the Goal's turn queued
with `attempt: 0`; it does not launch a native agent, manufacture a retry, or consume the Goal's
attempt budget. The same row becomes claimable shortly after a matching inference node joins or
recovers. Mixed-harness Goals
apply this per harness: an allowed Claude worker cannot claim a Responses-only model, while an
allowed Codex worker can, and the choice can change on a later turn as Grid routes change. `auto`
and effort-mode Goals likewise wait until the router is enabled and has a compatible live pool.
Valid Fixed/Dynamic effort plans encoded in the model string wait until every explicitly pinned
model is present in the compatible auto-routing pool; Grid never starts a partial pin that the
inference router would reject afterward.

This readiness check does not pin the task to the model-serving machine or reserve an inference
slot. Capacity, requester-specific trust/allowance, and a provider disappearing after claim remain
request-time facts handled by the inference router. Readiness is cached for at most one second so a
polling fleet does not scan the entire model registry on every half-second task poll. Model
registration/removal, provider-role recovery, router demotion/recovery, quota serving transitions,
and unhealthy-model heartbeat changes invalidate it immediately; unrelated GPU/load telemetry does
not churn the cache.

`grid goal status` prints `waiting for compatible Grid inference` while this gate is holding the
queued turn, or names the ready harnesses. The human `list` view labels the former
`waiting-model`; JSON responses carry `model_readiness: {state, agents}`. This is dynamic routing
diagnosis, not a new durable Goal status, so the Goal itself remains `active` and its attempt stays
zero.

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

`grid goal run` gives the create request a unique idempotency key and retries one ambiguous
transport failure with that same key. If both acknowledgements are lost, the error prints the key;
rerun the exact command with `--idempotency-key <key>`. The relay then returns or finishes the
original Goal instead of creating a second autonomous Goal tree. A key is scoped to the member and
cannot be reused for a different Goal request.

The command prints a Goal id. Use that id to inspect or control the run:

```bash
grid goal list                    # active, paused and blocked
grid goal list --all              # includes ended Goal history
grid goal status <goal-id>
grid goal evidence <goal-id> --verify > goal-evidence.json
# For an unmerged/controlled rollout, also prove every attempt used that clean worker checkout:
grid goal evidence <goal-id> --verify --require-worker-revision "$(git rev-parse --short HEAD)"
grid goal pause <goal-id>         # current leased turn may finish; no next turn is queued
grid goal resume <goal-id>
grid goal resume <goal-id> --token-budget 10000000  # extend a budget-limited root Goal
grid goal cancel <goal-id>        # ends the Goal and cancels queued/running work
```

For a Goal with children, pause and cancel are hierarchical. Pause lets already leased slices end
but prevents every live descendant from receiving another turn; resume restores only descendants
paused by that parent, so a child paused independently stays paused. Cancel recursively terminals
queued/running descendants, including a child whose relay Git preparation was in flight.

Pause is an overlay, not a rollback. If the in-flight slice passes the final eval, fails terminally,
or exhausts its budget after pause lands, Grid stores that underlying outcome while still showing
`paused`. Plain resume reveals the stored terminal state and queues nothing. Completed, failed and
cancelled Goals cannot be revived. A budget-limited root Goal is the intentional exception: resume
it with a larger cumulative `--token-budget` and Grid queues the next turn from the same branch and
native transcript. The new cap must exceed both the old cap and all consumed/reserved tokens;
replaying the same successful extension is idempotent. Child allocations remain parent-managed.
Budgets and native usage counters may be as large as `9,007,199,254,740,991` (the largest exact JSON
integer), so million- or ten-million-token local-model runs are ordinary supported values. The bound
also guarantees that the largest allowed subgoal hierarchy cannot overflow relay database sums.
If a paused parent reveals another terminal outcome, Grid cancels its still-live descendant turns
rather than waking workers whose results can no longer be reconciled into that parent.

Repeated pause is idempotent and never overwrites that saved outcome. One deliberate hierarchical
case remains useful: directly pausing a child already paused by its parent detaches it from the
parent's cascade while preserving the child's saved state. Pause ownership moves with that child's
whole subtree, so resuming the parent cannot wake grandchildren underneath the still-paused child;
resuming the child later restores only its own cascade. A descendant cannot be resumed while any
ancestor is still paused; Grid returns `goal_ancestor_paused` with the ancestor Goal id instead of
letting child work bypass the hierarchy-wide stop.

The same dependency fence applies to every terminal parent path, not only pause/resume: an explicit
failed result, deadline, or exhausted retry cap cancels queued/running descendants through the
ordinary task cancellation protocol. Providers are notified by the lease fence, and completed
children remain available as trajectory history.

Each claim also carries a random opaque generation. A native Goal worker must present that exact
generation on lease renewal, progress events, retry checkpoints, terminal results, Git fetch/push,
Grid inference, and relay-internal actions such as spawning a subgoal. Node identity alone is
insufficient: after a lease moves—or even when the same node immediately reclaims the same turn—a
delayed process from the prior claim is rejected. The generation is relay-issued and never exposed
to the native Codex or Claude process; only the local Grid supervisor and its loopback inference
proxy carry it.

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

Structured artifact outcomes can use a commit-pinned JSON metric instead of brittle text matching:

```json
{"version": 1, "evals": [{
  "type": "json", "name": "support outcome", "path": "metrics.json", "max_bytes": 20000,
  "checks": [
    {"pointer": "/tickets/resolved", "op": "greater_or_equal", "value": 100},
    {"pointer": "/errors", "op": "equals", "value": 0},
    {"pointer": "/status", "op": "equals", "value": "ready"},
    {"pointer": "/debug", "op": "exists", "value": false}
  ]
}]}
```

The relay independently computes these predicates, but a repository artifact is only as trustworthy
as its producer. If the acting agent writes `metrics.json` itself, the check proves what that file
says—not that an external business system agrees. Use JSON metrics only when a trusted test,
importer, or separately controlled system produces the artifact. File/JSON checks are strong for
exact artifact contracts; they are not a substitute for an external KPI oracle. For a business
outcome, bind a `verify` eval to a named GET-only Goal tool whose result is recorded in full:

```json
{"version": 1, "evals": [{
  "type": "verify", "name": "ticket resolved", "tool": "check_ticket",
  "arguments": {"ticket_id": "T-42"},
  "checks": [
    {"pointer": "/status_code", "op": "equals", "value": 200},
    {"pointer": "/body/status", "op": "equals", "value": "resolved"}
  ]
}]}
```

The named tool must exist in the Goal manifest with `"mode": "verify"`, a GET method, and
`"record": "full"`. The node supervisor—not the acting native process—contacts the approved local
business API and durably records the request and result. At completion, the hosted relay scores only
the final live attempt's latest exact-argument request/result pair, with matching tool, call id,
provider, attempt and event order. A passing read from a worker that later died cannot complete its
replacement's nomination. Oversized, unsuccessful, missing, stale or unmatched results fail the
metric and produce a repair turn. The relay stores every binary score and provenance tuple; offline
evidence verification independently finds the same event pair and recomputes every JSON predicate.
The private API remains local to the eligible Grid node—the hosted relay never needs network access
to it. Credential-shaped argument keys are rejected when the metric is created: the worker would
redact them from evidence, so accepting such a contract would both store a secret-shaped policy
value and create a Goal that could never match its durable request.

Pointers use RFC 6901 escaping. Supported operations are `equals`, `not_equals`,
`greater_or_equal`, `less_or_equal`, and `exists`. Numeric comparisons reject booleans and
non-finite values. A JSON file is capped at 4 MiB, all JSON evals at 16 MiB per nomination, and
parsing is bounded by depth and value count. Duplicate object keys and unpaired Unicode surrogates
are rejected as ambiguous data. Invalid JSON is a measured failure the next Goal turn can repair;
Git/read failures remain blocking evaluator infrastructure errors.

All definitions in one completion nomination share a 45-second wall-clock evaluator deadline.
Result-ref resolution, transcript-ref resolution, evaluation, and conversation-branch advancement
also share one 50-second aggregate deadline; each stage spends what remains instead of starting a
fresh Git timeout. The Goal worker has already stopped lease renewal and its terminal-report request
waits 60 seconds, leaving ten seconds for the fenced transaction and response. At authenticated
result ingress, the relay extends that exact claim's lease and run deadline to at least 70 seconds
without shortening a longer configured lease; a short task TTL therefore cannot reclaim valid work
while relay-owned settlement is running. Git failure is distinct from an absent ref and leaves the
turn running instead of settling a false empty result. One evaluator infrastructure error records
blocked audit rows for the remaining definitions without starting more Git subprocesses; a single
wedged repository therefore cannot multiply its timeout by every check in the manifest. After the
terminal transaction commits, Grid sends the result response before idempotent continuation and
child fan-in preparation. Periodic reconciliation recovers that post-response work after a crash;
a bounded stale-prepare sweep also catches a relay death after the continuation row was inserted
but before its Git input became claimable. Cleanup writes the failed state and one terminal event
atomically, then reconciliation creates exactly one replacement turn.

Each definition is immutable, and its hash includes the evaluator-semantics version. Every score is
stored with that definition hash, its turn, evaluator node, and exact Git commit. The guarded lease
transaction marks only the exact run ids returned by that independent evaluation `accepted`; it
never accepts every row that happens to share the same turn and commit. A stale provider's
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
a cheaper model. If omitted, the child model inherits the parent's model. A parent may call the
spawn action repeatedly for distinct independent objectives in one native turn; after its final
spawn it ends the turn rather than polling, and Grid exposes the sibling children concurrently.

The parent enters `waiting_children` after its turn checkpoints. It receives no new turn until every
child is terminal and all required children are complete, then resumes with every child id, status,
and eval summary. A failed required child blocks the parent and immediately cancels every live
sibling branch because none can be consumed after that dependency fails; Grid releases their token
reservations and exports each cancellation in Goal evidence. `grid goal resume` then refuses with
`goal_required_child_failed` instead of returning a misleading no-op—cancel the blocked tree or
start a replacement Goal. A conflicting required child also blocks the parent but remains
retryable because resume reruns deterministic fan-in. Nested terminal dependency failures propagate
to every ancestor immediately and cancel work that can no longer be consumed. Nested fan-in
conflicts remain recoverable: Grid preserves the blocked child, leaves unrelated ancestor siblings
running, and directs the operator to resume that child before its ancestor. A failed, missing, or
conflicting optional child is recorded in the resumed prompt without blocking it. Child slots and
token allocations are reserved atomically,
fan-out is limited to eight children, nesting is limited to three levels, and at least 1,000 parent
tokens remain for final fan-in. Subgoals are off by default because enabling them authorizes
autonomous parallel work and budget allocation.

Within one parent turn, the normalized child objective is the stable delegation identity. If a
worker dies after spawning a child, its replacement may reconstruct optional eval or routing fields
differently and still receives the original child instead of creating duplicate work. Give sibling
children distinct objectives when they are intentionally separate delegations.

A live child reserves its full allocation against the parent's cumulative token cap. When that
child terminals, Grid releases the allocation and charges the child's actual usage exactly once;
for nested Goals that actual usage already includes all descendants. `grid goal status` reports
the total actual usage plus any allocation still reserved for live children. If parent plus
descendant usage reaches the cap, Grid can still fan in the finished branches, then ends the parent
as `budget_limited` without scheduling another agent turn. Budgets are enforced at accepted native
slice boundaries, so one in-flight local-model slice can report more tokens than the prior cap.

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

A delivered claim whose harness became ineligible while its long-poll was waiting is earlier than
that failure boundary: the provider revalidates the relay-supplied required capabilities before
attempt-start and returns the untouched lease immediately. That delivery race consumes no attempt.
During a provider-first rolling upgrade, an older relay may not yet expose the decline route; the
worker still starts no agent and safely falls back to lease-expiry recovery.

When Codex marks the Goal complete, the last task becomes terminal and no next task is queued. The
Goal disappears from the default `grid goal list`, while its Goal row, task attempts, events,
trajectory and counters remain available for audit and future `grid train` datasets. Unlike
ordinary task history, Goal branches and transcript refs do not expire by default. A relay that has
already exported them may set `GOAL_TRAJECTORY_RETENTION_SECONDS` to a positive retention window.
That clock starts only after the whole Goal is terminal and is measured from its latest completed
turn. A paused, blocked, active, or `waiting_children` Goal is never eligible, so a long child run or
operator pause cannot erase the parent checkpoint it still needs to resume.
The evidence export is schema-versioned and includes each turn's prompt, output/error, harness,
execution node, inference usage, worktree commits, transcript input/output commits, recorded tool
events, and accepted or rejected independent eval runs. Inference usage is grouped by turn, exact
model, model-serving node, transaction state, Goal attempt, agent-executing node, and harness. The
attempt identity matters because a reclaimed turn keeps the same turn id while Codex on one machine
may be replaced by Claude on another. Failed requests remain useful failure evidence, but only
`completed` requests can prove that a turn actually executed through Grid inference.

The CLI requests evidence in bounded 20-turn pages. Each relay page loads only those turns and their
events, inference, evals, and Git ancestry edges; the client requires contiguous cursors, a stable
Goal/relationship snapshot, unique turn ids, the declared total, and one relay-authored evidence
fingerprint before assembling the familiar schema. The relay checks that fingerprint both before
and after each page query, so a late rejected eval, action audit event, inference settlement, or turn
update is caught even when the Goal's visible status did not change. `grid goal evidence --verify`
additionally requires exactly one ancestry edge per turn handoff. A Goal that changes during an
active export fails clearly and can be retried instead of producing a mixed-time training record.
Relays predating pagination still return the legacy whole record and remain readable.

Malformed stored event or evaluator JSON never makes the whole evidence export unavailable. Grid
exports an explicit corruption marker so an operator can inspect the surviving trajectory, while
`grid goal evidence --verify` rejects the record. Damaged evidence therefore remains auditable but
cannot become release proof or training data.
Valid but verbose evidence is compacted structurally, not replaced by a generic overflow marker.
Every failed definition id survives so Codex or Claude receives its exact immutable repair contract;
every oversized accepted run retains the ordered check verdict vector while omitting bulky previews.
If even that bounded proof cannot fit after a future schema change, completion fails closed rather
than accepting a score whose evidence disappeared.
If a completion retry encounters a damaged cached evaluator verdict, the relay atomically
downgrades it to an unaccepted infrastructure error and blocks the Goal instead of returning 500 or
re-blessing a stale passing label. Parseable cached evidence is also rejected when its immutable
definition identity, relay provenance, state, pass label, score, and error marker disagree. If an
eligible run disappears or changes between evaluation and terminal settlement, the whole terminal
transaction rolls back; the provider's ordinary result retry recreates and accepts exact evidence.
Result settlement also checks the live provider lease before resolving Git or starting an evaluator,
then checks it again in the terminal transaction. An unrelated or already-stale node therefore
cannot manufacture audit rows or amplify bounded content checks; a legitimate worker that loses its
lease during evaluation may leave an inspectable but unaccepted verdict.
Offline verification checks the converse of final-pass proof as well: every `accepted: true` run
must belong to the immutable manifest, score its own completed turn commit, carry a consistent
binary verdict, and be relay-authored. An extra accepted label cannot hide beside a valid witness.

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
    },
    {
      "name": "check_ticket",
      "mode": "verify",
      "record": "full",
      "description": "Read the authoritative post-action ticket state",
      "input_schema": {
        "type": "object",
        "properties": {"ticket_id": {"type": "string"}},
        "required": ["ticket_id"]
      },
      "http": {"method": "GET", "url": "http://support.internal/tickets/check"}
    }
  ]
}
```

```bash
grid goal run --project <project-id> \
  --objective "Resolve every ticket in the assigned queue" \
  --done-when "the queue reports zero assigned unresolved tickets" \
  --model <model-name> --tools ./support-tools.json --evals ./support-evals.json
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

Tool JSON is limited to 64 nesting levels. Non-finite or deeper action arguments are rejected before
the request audit and before any HTTP call; a deeply nested response is retained as bounded text so
it cannot crash the Goal worker or erase the result-side durability fence.

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
| Root creation replay | Client -> relay -> queue | N/A before claim | First POST acknowledgement is replayed; changed body reuses key | One Goal id and one first turn; key conflict rejected |
| Continuation-prepare crash | Relay timer | N/A before replacement claim | Relay dies after inserting the next turn but before its Git input becomes claimable | One failed abandoned row, one terminal event, and one queued attempt-zero replacement without a client cleanup request |
| Model arrival | A polls; inference-only C joins | Codex on A | Requested model is absent for several polls | Same row stays at attempt 0; `READY.md`; first and only claim is attempt 1 |
| Quota recovery | A polls; inference-only C stays registered | Codex on A | C advertises the model but reports `quota.serving: false` | Same row/evidence stay untouched until C's healthy heartbeat; first claim is attempt 1 |
| Four-feature game | A -> B -> C | Codex -> Codex -> Codex | A dies in feature 2; B dies in 3–4 | HTML wiring, click/score behavior, styling and instructions |
| Mixed game | A -> B -> C | Codex -> Claude -> Codex | A and B die mid-feature | HTML wiring, click/score behavior, styling and instructions |
| Eval-repair game | A -> B -> C -> D | Codex -> Claude -> Codex -> Claude | A/B die; C nominates broken behavior | Failed C score plus passing D repair on exact commits |
| Crash-safe game | A -> B | Codex -> Codex | A's native harness crashes after writing partial work | HTML wiring, click/score behavior, styling and instructions |
| Claude protocol drift | A (2 workers) -> B | Claude -> Codex | A exits cleanly without its native evaluator attachment while worker 2 holds a stale claim poll | Worker 2 declines the delivered retry without spending an attempt; A stays online without `native_goal`; B receives attempt 2 and accepted Git/transcript pins |
| Codex protocol drift | A (4 workers) -> B | Codex -> Claude | A's schema passes but a required runtime method disappears while three sibling workers hold stale claim polls | All three delivered retries are declined without spending an attempt; A stays online without `native_goal`; B receives attempt 2 and accepted Git/transcript pins |
| Crash-safe business action | A -> B | Codex -> Codex | API commits, then A's native harness crashes | One side effect; stable key; complete action evidence; passing proof |
| Business result-window death | B -> C | Codex -> Codex | B is SIGKILLed after API commit, before the result event | One side effect; unmatched request reconciled by C's stable-key replay |
| Image artifact | B polls; A executes | Claude rejected; Codex selected | Capability mismatch | PNG file and size |
| Support reply | A polls; B -> C execute | Codex | B dies after API commit; first eval fails | `DONE.md`; JSON artifact; two fresh authenticated API verifications; one API side effect |
| Required child | A parent; B child; C parent | Codex -> Claude -> Codex | Parent moves while child runs | Child and parent files |
| Mixed child reclaim | A parent -> B child -> C child -> D parent | Codex -> Codex -> Claude -> Codex | B's native child harness fails after partial work | Same child turn at attempt 2; accepted child eval; one fan-in; completed parent |
| Parallel child fan-out | A parent -> B/C children -> D parent; inference E/F | Codex -> Codex + Claude -> Codex | Two required children run simultaneously on distinct roots/models and issue real Responses/Messages calls through distinct Grid providers | Exact per-turn inference attribution; two accepted child evals; deterministic two-branch fan-in; completed parent |
| Required child failure | A parent -> B/C children | Codex -> Codex + Claude | B returns a native failure while C is still running a required sibling | Parent blocks; C is cancelled and lease-fenced; reservations reach zero; cancellation remains in evidence |
| Optional child | A parent; B child; C parent | Codex | Child fails | Parent file; child failure retained |

Every agent-running scenario except Model arrival and Quota recovery uses `fake-grid-model` (the
required child uses `fake-grid-child-model`) so the test measures Grid's queue, agent harness, tool,
Git/checkpoint and eval protocols deterministically rather than model quality. Model arrival asks
for `late-grid-model`: A polls while no node serves it, then inference-only C advertises it. Quota
recovery asks for `quota-recovery-model`: C advertises it throughout, but first heartbeats
`quota.serving: false` and later `true`. Both tests require no attempt-start or retry evidence while
inference is unavailable and exactly one attempt on A after recovery. The support-reply
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
attempt, or harness values before they enter training evidence. The verifier requires exactly one
retry for every prior attempt and counts a reclaimed machine only when that retry has one matching
relay-stamped attempt-start identity; an orphan or duplicate retry cannot prove a handoff.
For Goal turns, the provider flushes that start marker synchronously before launching Codex or
Claude. The relay deduplicates a replay after an ambiguous response; if no durable acknowledgement
arrives, the provider starts no native work or business action and leaves the lease for recovery.

Owner controls are ordered against checkpoint settlement, not treated as best-effort UI state. A
pause that races an accepted nonterminal checkpoint keeps its exact pins but makes the queued retry
unclaimable until resume. A cancel in the same window terminals the turn and rejects the late pins.
If the final allowed attempt has already terminalled as `retries_exhausted`, that terminal outcome
overrides a pause arriving in the short turn-to-Goal reconciliation gap; the Goal is `failed` and
cannot be resumed past its declared attempt budget.
Likewise, a leased slice that settles while paused advances the pause's saved underlying state. A
passing final eval resumes directly to `complete`; terminal failure and budget exhaustion resume to
their terminal states, never to a fabricated repair turn.
