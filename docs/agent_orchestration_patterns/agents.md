# How the Agent Layer Executes — the session/seat model the agent patterns run on

The agent catalog (`README.md` in this directory)  describes *which agent gets
the task and whether it may act*. This is the *how* — the execution primitive
those seven patterns run on, made concrete the way
`router-execution.md` makes the model layer's joins concrete. Read the model
layer's `ROUTER.md` for what the current router does; this is what every agent
pattern assumes underneath.

One sentence on the cover: **every agent pattern is a pair of constraints —
one seat, one actor — and the joins (spawn/warm, handoff, eviction, admission,
the ledger) are where a real multi-agent box actually breaks.** The model layer
makes the *answer* reliable; the agent layer makes the *action* reliable. Get
the joins right and the seven patterns become scheduled forms of one loop.

---

## The execution primitive

Give the agent router **one** loop, and all seven patterns are states of it.
Strip everything away and the layer reduces to a single choice per request:

```
request ──► route the lane   (which harness × model tail, by the act contract)
        ──► residency first  (is that lane on the resident seat? else defer)
        ──► the act-gate     (N−1 read-only sessions, one selected actor)
        ──► the act          (one idempotent, round_id-keyed mutation)
        ──► the ledger       (every durable event appended, fsync, one box)
```

- **The scarce thing is the seat, not the token.** N for any fan comes from
  *live free seats* (VRAM residency), not a constant: a 1–2-seat box holds
  ~1–2 resident sessions, so a 3-agent fan is queued serial swaps on the
  critical path, each costing seconds of load. Spawn `min(N, free seats)`
  parallel and queue the rest.
- **Only one worker may act.** A redundant agent has one job that is cheap
  precisely because it is safe: the read-only half of the fan. The actor is
  selected (purple) and its mutation is green and exactly one — `round_id`
  keyed, so a retry reaps the same effect instead of re-executing it.
- **State is durable product.** An agent's value is its context, and that
  context is crash-recoverable on a local box — but only if it is *managed*:
  spawned, warmed, handed off, resumed, killed. No pattern may let a seat die
  without first snapshotting.

## The seat is the whole economy (capacity is the input)

The agent router holds the same **live-node inventory** the model layer does,
but the scarce unit is the *session seat*, not a VRAM load: per harness lane,
per model tail, a seat holds one resident session with its working tree,
context, tool scope, and credentials. Residency is the first question, before
any lane or pattern choice:

- An agent on a resident seat is a context swap away — cheap.
- An agent on a lane the box isn't running requires a **swap** the request has
  to pay for in load time, and a swap mistakes a load cost for a capability.
- A resident-model *battery* (several seats co-resident) is a second resource
  scheduler — VRAM residency — that wall-clock scheduling does not arbitrate;
  a battery can starve the foreground even where the time-schedule is right.

So the routing rule is: **route the lane by the act contract, then residency,
then model; never force a lane the box isn't running.** Defer, don't eject.

## Session lifecycle is the join (spawn / warm / handoff / kill)

The model layer's joins are *parallelism* joins — spawn then pool. The agent
layer re-joins on top of a *lifecycle* join, because a session outlives the
single request:

- **Spawn** — cold start on a free seat. Cheap in code, costly in wall-clock;
  it is the load the whole fan pays.
- **Warm** — stay resident, reuse context. The mode that makes fanning wide on
  one seat affordable.
- **Handoff** — freeze to snapshot, then move; the seam between lanes and the
  seam before an eviction.
- **Kill / cancel** — the cheapest primitive and the one a fan needs most:
  a loser is killed, never left to keep acting in the background. Every kill is
  preceded by the snapshot, so a seat frees whole.

A preempted background seat (a shadow, a resume, an eviction) must come back
from its last snapshot, not from a cold start — that is the dashed "resume"
edge of the diagrams and the guarantee the lifecycle exists to pay.

## The act-gate is the constraint every fan inherits (#1)

Any fan that spawns N agents spawns **N−1 read-only sessions and one actor**;
the selected agent carries the world-touching step, and that step is one
idempotent, checkable mutation. On a 1–2-seat box "simultaneous" is the wrong
mental model — the fan is **serial swaps**, the sessions don't co-reside, so
the gate is enforced per-invocation and N is a scheduler choice, not N seated
actors. A gate asserted only in a prompt (a read-only lane that can still
`git push`) is a hope, not a gate.

## The divergence unit is the harness × model tail, not the model or the lane

Routing across *lanes* only is weak divergence. Two agents on the same
harness + same model share one training tail because they share the model —
the harness contributes the reflex (prompt, tool schema, exec behavior),
nothing to the tail. Real divergence needs **either** a different harness
*or* a different model tail:

| Desired divergence | Minimum unit | Example on this box |
|--------------------|--------------|---------------------|
| Weak / reflex-only  | same model, different harness | `qwen3-coder` on **Claude Code** vs `qwen3-coder` on **Codex** |
| Strong / family      | different model tail | `qwen38-35b-a3b-mtp` vs `glm-5.2` — cross-vendor |
| Strongest / both     | harness **and** model tail | `deepseek-v4-flash` on **Hermes ACP** vs `glm-5.2` on **Codex** |

That pairing *is* the routing decision. Same-model, different-harness is a
useful second draft (a genuinely different tool reflex), but it is not family
independence; do not sell a same-model pair as the divergence #2/#11 in the
model layer buy with different families.

## Harness × model seats, as the parameters the router holds

Every worker node in a figure, and every row below, is an *illustrative
concrete build* — a real current name standing in for "whatever is resident
on your box" (per "A word on the examples" in the README):

| Lane / exec seat | Model tail (illustrative) | Default posture | Pick it for |
|------------------|---------------------------|-----------------|-------------|
| Hermes ACP | `deepseek-v4-flash` | read-only by default; structured tool calls | the controlled fan where a worker may act in a narrow, verifiable scope |
| OpenClaw | `qwen38-35b-a3b-mtp` | fan-out worker; governance is yours | the task starts in a channel and spans several tools |
| Claude Code | `qwen38-27b-mtp` | `--no-tools` read-only; repo-aware depth | one repo done well |
| Codex | `qwen38-27b-mtp` | sandboxed `exec --json`; worktrees/PRs | N supervised lanes across repos |
| Pi | `qwen3-coder` | thinnest: no plan/sub-agents; under *your* loop | the most auditable seat under your own orchestration |
| OpenCode | `qwen3-coder` | open terminal TUI, configurable | the harness itself transparent and yours |
| Aider / Command Code | `glm-5.2` | git-native pair seat / learns your taste | small diffs you steer; taste that follows you |

The **reviewer** is always on a *different* tail than the writer: fix A written
on Hermes ACP is reviewed on the OpenCode lane and vice versa — the Polly rule,
and exactly the "weak arm diverges on purpose" of the verifier (#6).

## Background runs in idle slack, never at a live request's expense (#4)

The shadow in staged admission (#5), the warming in session lifecycle (#2), and
any learner accumulation are **background** work, and they share the same
one-or-two seats as live traffic. So they need real scheduling, not "run in
idle": a definition of idle (a free seat, not the wall clock); preemption (a
background task is cancelled the instant a live request wants its seat); and a
VRAM-residency bound (background never swaps a model a live request needs).
Under EDF-style admission this is **slack-stealing**: background runs only in
the slack live deadlines leave free. A shadow that starves a live request has
inverted the order the whole layer depends on.

## Staged admission is earned, not granted (#5)

A harness you don't trust yet enters as a **shadow** — the read-only half of
the fan on live traffic — scored against the ground-truth authority (#6),
not self-graded. Only when it clears the bar does it promote: shadow → bounded
act (its act step gated behind the router's quorum) → full act. Every crossing
is a ledger event, so a replay answers "when did this harness earn the right
to push?" Run the shadow in the idle row of #4.

## The ledger is exact-once and durable, and there is no second box (#7)

The agent layer has no replication to fall back on — a single box takes the
request, the sessions, *and* the in-flight ledger together — so every
integrity property is a *log* property, and none may be assumed:

- **Exactly-once append.** Key every ledger event
  `(round_id, role, outcome_event)` and dedupe on the key; a client retrying
  at-least-once must not double-count an act or inflate an admission count.
- **Durability (fsync/WAL + replay).** The snapshot and the act log are
  *reset on power loss* unless they are write-ahead durable. The snapshot of a
  session, and every admission / act / review line, is fsync'd before the seat
  frees — a preempted or wiped seat resumes from its last snapshot, never a
  cold start.
- **One log, exported off-box.** Snapshots, reputation, and every act event
  append to one log that is exported to a different medium on a cadence; a
  wiped box loses a day, not the audit of what the fleet touched.

## The one decision, as a concrete procedure

Combining the seven patterns into a runnable router:

1. **#1 the act-gate first.** Will this touch the world? If yes, an actor
   must be selected and exactly one; if no enforceable gate exists on a
   resident seat, defer the task — never route it onto a weaker lane.
2. **#3 route the lane.** Which harness × model tail by the act contract;
   residency first, then model. Never force a lane the box isn't running.
3. **#2 warm the seat.** Reuse the resident session's context before paying a
   spawn; hand off only across a real seam.
4. **#4 schedule the seats.** Spawn `min(N, free)`, evict to snapshot on a
   live arrival; run any shadow/learner only in idle slack.
5. **#5 admit the new.** A harness without a score is shadow read-only until
   it clears the bar; promote shadow → bounded → full, each a ledger event.
6. **#6 certify by fact, not session.** A test/schema certifies; two
   agreeing tails only *propose* (`proposed_by: consensus`), never certify a
   trust-affecting label.
7. **#7 append it all.** Every decision, act, and admission goes to the one
   fsync'd log, exported off-box.

## A worked ledger walk (one request, end to end)

You own an RTX 6000 Ada (48 GB) with two resident seats:
`qwen38-27b-mtp` on **Codex** (`exec --json`, sandboxed) and
`qwen38-35b-a3b-mtp` on **OpenClaw**. t=0 a request arrives

> `"our dispatcher hangs ~96 s on a flaky upstream; reproduce, fix, and
> certify the fix"` — depth-of-thought budget 4 (worth a small fan).

1. **#2 spawn/warm.** The fan needs 3 reader sessions; the box holds 2, so
   the router queues the third. The threaded seat is reused warm; the third
   is a serial swap after.
2. **#1 split.** N−1 read-only shells + one actor. The losers repro and
   propose; only the actor writes.
3. **#6 the reviewers diverge on purpose.** Fix A is `deepseek-v4-flash` on
   **Hermes ACP** (read-only by default); fix B is `glm-5.2` on **Codex** —
   a genuinely different harness × model tail, real divergence, not twin
   priors. Each diff is then reviewed on the *other* lane.
4. **#4 the preempt.** A live user request lands mid-fan; the third queued
   session (the warm candidate worktree) is evicted to snapshot and yields;
   the live request runs; the fan resumes from snapshot — no cold start.
5. **#7 the ledger.** The fan dispatch, both drafts, both cross-lane reviews,
   and the `git push` each append `(round_id, role, event)` to the one
   fsync'd log, deduped on the key, exported one copy to the NAS per round.
6. **#6 certify.** The merged fix is re-run under Codex `exec --json`
   (`qwen38-27b-mtp · test`) against the repro harness until the mechanical
   check is green — a deterministic external fact sharing none of the writer's
   prior. Only that pass reaches the `shipped fix` exit; the consensus arm
   proposes-and-logs but never certifies.
7. **#5 if a new engine were in the pool.** Had the fan wanted a harness the
   box had no score for, it would have run shadow-only in the #4 idle row,
   never an act step, until it cleared the bar.

The replay of those ledger lines answers, after the box wipes: which agents
touched the world, when, in what order, and whether each act was certified by
a fact. That replay is the product the layer's patterns exist to make one
box able to produce.

---

**Read with.** `README.md` in this directory (the seven patterns this
execution model runs on); the model layer's `ROUTER.md` (what the router does
today) and `router-execution.md` (the model layer's joins). Draw before you
write: `knowledge/diagram-style.md`, `knowledge/technical-writing-style.md`.
