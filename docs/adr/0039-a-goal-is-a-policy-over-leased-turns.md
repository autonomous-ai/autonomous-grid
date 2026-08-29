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

Ordinary task conversations retain their existing Claude default. A Goal created without an agent
policy retains the existing Codex-only behavior. An explicit `auto` policy permits both harnesses.
The ordered list is a deterministic preference when one node can run several harnesses; failover
still allows a later harness when that is what an eligible node advertises. Grid does not claim
round-robin fairness in this slice.

Capabilities are operator-advertised, fail closed, and scoped to a harness. Installing a binary is
enough to advertise the harness itself; it is not evidence that image generation, browser control,
an MCP server, or a privileged API is configured. Unknown required capabilities leave the Goal
queued rather than spending attempts on unsuitable workers.

### Native Goal mechanisms remain authoritative progress loops

Grid invokes Codex's native Goal API and Claude Code's native `/goal`; it does not reproduce their
prompts or stopping logic.

Each harness owns its native session format. Codex and Claude sessions are not convertible. The
agent side ref may contain both checkpoint namespaces, and a harness resumes its own last native
session when it returns to the Goal. A different harness receives the shared Git tree plus a concise
handoff block describing completed turns and failed evaluations. "Mixed-agent resume" therefore
means shared Goal continuity, not deserializing Claude history into Codex or vice versa.

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
conversation branch before it becomes active again. A clean merge is relay-side Git plumbing; a
conflict blocks the parent with the child id and paths rather than resuming from stale state. A
future slice may turn that blocked merge into an ordinary leased conflict-resolution turn, but
bypassing the missing ancestry is never an option.

The relay enforces maximum depth, children per Goal, cumulative token budget, and unique idempotency
keys. The child identity, immutable spec, edge and budget reservation commit before its first turn
is exposed; an idempotent retry can finish publication after a crash but cannot alter or duplicate
the child. Required children must independently pass their evaluations. Their pinned result commits
and summaries are included in the parent's next handoff. Child turns advance only their own branch
and never publish directly to global `main`; explicit fan-in merges committed child branches, and
only the root Goal enters trunk apply. A parent never reads a child's live workspace.

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
and a parent Goal whose independently claimed children fan in after all required evaluations pass.

## Consequences

Grid stays a distributed control plane around the native agent products rather than becoming a new
agent harness. Cross-machine recovery continues to have the same Git and lease semantics as tasks.
Mixed harnesses gain shared project continuity but intentionally do not pretend to share opaque
native transcripts. Completion becomes slower than trusting the acting model, but reproducible eval
evidence is what makes Goal trajectories useful for later SFT or RL training.
