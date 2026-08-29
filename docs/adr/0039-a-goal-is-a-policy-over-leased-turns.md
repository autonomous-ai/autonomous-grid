# ADR 0039: A Goal is a policy over leased turns

Status: accepted for implementation

## Context

Grid already has the durable machinery a long-running agent needs: a conversation owns ordered
turns, any eligible provider can claim a turn, a lease can expire and be reclaimed, project state
travels through Git, and private agent state travels through the conversation-scoped
`refs/grid/agent/<conversation-id>` side ref. A Grid Goal must reuse those mechanisms. It must not
introduce a second queue, a shared filesystem, or a relay-owned agent process.

The first Goal slice pinned `codex` on the conversation. That proves Codex handoff, but it cannot
express a Goal which Codex starts and Claude Code continues, or a Goal requiring a capability such
as image generation. It also treats the native harness status as the only completion verdict. That
is useful progress evidence, but it is not a reproducible business outcome evaluation.

## Decision

### One Goal, ordinary distributed turns

A Goal is a task conversation plus Goal policy. One native harness iteration is one ordinary leased
turn. The existing queue, lease, reaper, Git result branch, WIP branch, task events, and agent side
ref remain authoritative.

No worker may read another worker's disk. A successor reconstructs only:

1. the pinned input/result commit from the relay Git plane;
2. `refs/grid/agent/<goal-id>` at the pinned checkpoint; and
3. immutable relay events and evaluation results included in its next prompt.

Uncommitted work from an expired lease is deliberately lost. This is the fence that prevents a late
worker from publishing over its successor.

### Harness policy and per-turn assignment

A Goal stores an ordered allow-list of harnesses (`codex`, `claude`) and required capabilities.
Every provider claim advertises harness profiles, not only machine-wide booleans:

```json
{
  "agent_profiles": [
    {"kind": "codex", "capabilities": ["dynamic_tools", "image_generation"]},
    {"kind": "claude", "capabilities": ["mcp"]}
  ]
}
```

The relay offers a Goal turn only when at least one advertised profile is allowed by the Goal and
contains every required capability. The chosen harness is written onto the turn in the same
transaction that grants its lease. Result validation uses this turn-level value; it must never
recompute the choice from the Goal or from the reporting node.

Grid refuses operator attempts to advertise `dynamic_tools`, `subgoals`, or `image_generation` on
Claude because this runner wires none of those surfaces into Claude Code. A Codex profile may
advertise `image_generation` only when that node's operator has actually configured the integration.

Ordinary task conversations retain their existing Claude default. A Goal created without an agent
policy retains the existing Codex-only behavior. An explicit `auto` policy permits both harnesses.
The ordered list is a deterministic preference when one node can run several harnesses; failover
still allows a later harness when that is what an eligible node advertises. Grid does not claim
round-robin fairness in this slice.

Capabilities are operator-advertised, fail closed, and scoped to a harness. A binary is advertised
only after its version proves the native distributed-Goal contract Grid uses (`codex-cli 0.150.1`
or newer for Codex; Claude has its own measured resume floor). Installation is not evidence that
image generation, browser control, an MCP server, or a privileged API is configured. Unknown
required capabilities leave the Goal queued rather than spending attempts on unsuitable workers.

Business HTTP tools use the same matching rule. The relay canonicalizes every manifest origin and
adds an opaque `tool_origin.<hash>` requirement. A Codex provider advertises that capability only
for exact origins in its operator-controlled `GRID_GOAL_TOOL_ORIGINS` allowlist. This binds both
authorization and reachability to scheduling: a node which cannot call the API never claims the
Goal. User-authored manifests cannot request Grid authentication, headers, relative URLs, or the
relay's reserved internal action name. Only a relay-authored tool marker unlocks a relative action
against the exact selected relay origin. Workers enforce these rules again because a mixed-version
fleet must fail closed even when an older relay accepted an unsafe manifest.

Tool request/result events are stronger than ordinary progress events. The request is synchronously
accepted by the relay before the worker contacts an API. The result is synchronously accepted before
it is returned to Codex. Losing the result after a possible side effect fails the leased turn; its
replacement uses the same content-derived idempotency key, so a conforming API can return the
committed outcome without repeating the mutation. External keys are scoped to Goal, tool and
canonical arguments, which also prevents a later independent-eval repair turn from repeating the
same mutation. Relay-internal actions remain turn-scoped because their authority belongs to the
lease. This yields a durable action trajectory even across the otherwise unavoidable crash window
after a remote commit. The relay overwrites provider-supplied Goal event attribution with the
authenticated live lease holder and attempt. The evidence verifier rejects orphan results,
unresolved mutations, malformed idempotency keys, and tool events without that attribution; a
killed attempt's unmatched action request is valid only when a later result reconciles the same
Goal-wide key.

### Native Goal mechanisms remain authoritative progress loops

Grid invokes Codex's native Goal API and Claude Code's native `/goal`; it does not reproduce their
prompts or stopping logic.

Each harness owns its native session format. Codex and Claude sessions are not convertible. The
agent side ref may contain both checkpoint namespaces, and a harness resumes its own last native
session when it returns to the Goal. A different harness receives the shared Git tree plus a concise
relay-authored handoff block describing the bounded recent turn history, failed evaluations and
child results. A harness joining for the first time feeds that handoff into the native Goal it
creates; a returning harness receives it as the next native turn. "Mixed-agent resume" therefore
means shared Goal continuity, not deserializing Claude history into Codex or vice versa.

Codex's state database records the rollout JSONL by an absolute machine-local path. Copying that
database and calling `thread/resume` by id fails when the prior worker root is absent. Grid therefore
stores the rollout's path relative to the checkpointed Codex home, resolves it beneath the successor
worker's home, and supplies that relocated absolute `path` to `thread/resume`. Paths that escape the
copied home, are missing, or are ambiguous fail closed. This was measured against real Codex 0.150.1
with worker A's original home taken offline before worker B resumed.

Dynamic tool declarations are native thread state rather than `thread/resume` parameters. Real
Codex 0.150.1 was also measured by starting a thread with a custom tool, completing a turn against a
captured Responses endpoint, terminating app-server, resuming by the relocated rollout path in a
new app-server process, and completing another turn: both model requests contained the custom tool.
Grid therefore supplies dynamic tools at `thread/start` and lets native Codex restore them from its
rollout; the cross-repository fake must persist the same state or it is not a faithful resume test.

Grid records the native status, evaluator reason when the harness exposes one, model, harness
version, token/time usage, and checkpoint commit for every attempt. Native status can request
another turn, pause for a product limit, or nominate completion.

### Independent, pinned evaluation gates completion

The worker that acts may not write its evaluation verdict. A native Goal saying `complete` is a
completion nomination. Grid marks the Goal complete only after every required evaluation passes
against the exact result commit produced by that turn.

An evaluation definition is versioned and immutable once a Goal starts. The first implemented,
relay-local deterministic kind is:

- `file`: path exists, regular-file type, size, and SHA-256 predicates.

SHA-256 predicates require an explicit `max_bytes`; their declared aggregate is capped at 64 MiB
per completion nomination. The evaluator hashes Git blobs as a bounded stream and verifies the
stream length against Git metadata. This is separate from the compressed push limit: a small pack
can contain an enormous repeated blob. Legacy definitions still face the runtime budget and become
deterministic failed evidence rather than allocating the object in relay memory; malformed stored
definitions create audit-only evaluator errors and block for operator recovery instead of returning
an endless result-settlement 500.

The evaluator semantics version is part of each canonical definition and therefore its hash.
Definitions created before this field existed are version 1; a future implementation must add an
explicit version branch rather than silently changing what an existing metric means.

Planned evaluator-node kinds, which are not part of the first release gate, are:

- `command`: argv (never a shell string), timeout, expected exit code, optional bounded output
  matcher, executed in a read-only checkout by an evaluator-capable node;
- `http`: named observe-only capability, bounded request, and status/JSON predicate;
- `json`: schema and exact/numeric predicates over an artifact or observe response.

Every evaluation run stores Goal id, turn id, result commit, definition hash, evaluator node,
started/completed timestamps, pass/score, and bounded structured evidence. A run for commit A can
never complete commit B. Evaluation jobs use leases and may be retried, but their idempotency key is
`(goal, turn, result commit, definition hash)` so one turn's retry cannot produce two verdicts and a
later turn cannot inherit the earlier attempt's acceptance state merely because it reached the same
commit. The guarded provider-lease transaction marks a verdict authoritative. Rejected attempts
remain audit evidence with `accepted=false` and cannot change Goal state or enter training outcomes.

If native completion is nominated and evaluation fails, the Goal stays active. The next turn gets
the failed checks and evidence as a relay-authored handoff block. In the synchronous file-eval slice,
an evaluator infrastructure error leaves the Goal `blocked`, never complete; leased asynchronous
evaluators will introduce an `evaluating` state. User cancellation wins every race.
Infrastructure-error rows remain audit-only with `accepted=false`; holding the winning provider
lease cannot turn a failed evaluator into a training label. Resume queues a fresh Goal turn so the
recovered evaluator scores a new nomination.

### Subgoals are Goals with bounded dependencies

A Goal may create child Goals only through a Grid-provided action capability. The relay records
`parent_goal_id`, an idempotency key, required/optional dependency status, depth, and the budget
allocation. The parent enters `waiting_children` once its current turn checkpoints. Children use
ordinary Goal rows and ordinary task claims. Completed child branches are merged into the parent
conversation branch before it becomes active again. A clean merge is relay-side Git plumbing. A
required conflict blocks the parent with the child id and paths rather than resuming from stale
state. A conflicting optional branch is skipped, with its outcome and paths persisted on the edge
and included in the next handoff; optional exploration can never block the parent. A
future slice may turn that blocked merge into an ordinary leased conflict-resolution turn, but
bypassing the missing ancestry is never an option.

The relay enforces maximum depth, children per Goal, cumulative token budget, and unique idempotency
keys. The child identity, immutable spec, edge and budget reservation commit before its first turn
is exposed; an idempotent retry can finish publication after a crash but cannot alter or duplicate
the child. A status-less reserved child is treated as live until provisioning recovers it. The
periodic reconciler scans both active Goals missing a continuation and every waiting parent missing
its fan-in callback; it addresses the parent directly so nested Goal hierarchies cannot confuse a
waiting child-parent with its own parent. Required children must independently pass their
evaluations. Their pinned result commits and summaries are included in the parent's next handoff.
Optional children may omit evaluations;
their failure is preserved as trajectory evidence but cannot block the parent, and their branch is
merged only when they complete. The parent waits until every child is terminal so it receives one
deterministic snapshot of all child outcomes. Child turns advance only their own branch and never
publish directly to global `main`; explicit fan-in merges completed child branches, and only the
root Goal enters trunk apply. A parent never reads a child's live workspace.

Pause and cancel apply to the dependency subtree. A pause does not kill a leased process; every
affected Goal preserves `paused` when that slice reports and receives no continuation. Resume
restores only descendants marked as paused by that ancestor and their exact prior states, leaving
an independently paused child alone. Cancel recursively fences queued/running turns, and a prepare
that finishes after cancellation terminals itself without ever becoming claimable.

### Evidence required for release

The feature is not production-proven by processes sharing one host. Release requires both:

1. deterministic cross-repository tests with separate roots, real relay/task/Git planes, forced
   lease loss, adversarial races, and fake harnesses; and
2. a three-physical-node run using real Codex/Claude binaries and Grid inference, with node ids,
   harness/model assignments, task attempts, worktree commits, relay-verified transcript input and
   output commits, native checkpoints, evaluations, and final artifacts recorded in the test
   report.

Required scenarios include Codex A -> Claude B -> Codex C, a capability-constrained image Goal that
Claude cannot claim, evaluator retry and stale-commit rejection, cancellation during evaluation,
an origin-constrained business Goal that fails over after an idempotent action commits, and a parent
Goal whose independently claimed children fan in after all required evaluations pass.

## Consequences

Grid stays a distributed control plane around the native agent products rather than becoming a new
agent harness. Cross-machine recovery continues to have the same Git and lease semantics as tasks.
Mixed harnesses gain shared project continuity but intentionally do not pretend to share opaque
native transcripts. Completion becomes slower than trusting the acting model, but reproducible eval
evidence is what makes Goal trajectories useful for later SFT or RL training.
