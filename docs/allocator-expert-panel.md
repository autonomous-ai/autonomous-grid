# Allocator expert panel

This is a living, evidence-linked review of Grid's allocator. It prevents “smart” from becoming a
list of features without an adversarial assessment of whether the complete control loop works.

## 2026-08-29 review

Three independent AI expert-role judges reviewed the README, allocator implementation, ADRs,
tests, and the running four-logical-node product. They did not edit the implementation. The AI
serving judge also sent a real named-model request; the distributed-systems and SRE judges inspected
live status, placement explanations, and lifecycle history. This is a phase-one engineering panel,
not a substitute for the planned human and multi-physical-machine evaluation.

| Judge | Overall | Strongest evidence | Highest-risk finding |
|---|---:|---|---|
| AI inference serving | 5.8/10 intelligence; 8.5/10 safety substrate | heterogeneous placement, lifecycle safety | current-headroom filtering prevented trusted pressure from reaching legal preemption |
| Distributed systems | 7.1/10 local; 5.8/10 production-distributed | constraints, recovery, idempotency | no monotonic leader term/lease for an active-active controller |
| SRE, fairness, operations | 5.5/10 | fail-closed mutation safety and rich JSON evidence | caller-selected affinity buckets could spoof service-level urgency |

The panel agreed that Grid is substantially stronger at safe placement and lifecycle control than at
joint online portfolio optimization. It also agreed that confidence-aware exploration and hard cost
budgets matter, but correctness boundaries must come first.

### Findings accepted and fixed in this milestone

1. **Untrusted breadth had too much authority.** `X-Grid-Affinity-Key` remains useful for sticky
   routing and bounded observability, but never promotes allocation urgency. Only a short-lived,
   control-signed attestation bound to that exact opaque digest counts as trusted tenant evidence.
   Three cohorts must each contribute four recent samples; a `10 + 1 + 1` distribution cannot
   graduate.
2. **Portfolio admission confused occupancy with compatibility.** Hints now separately expose
   current fit, hard compatibility, and fit after removing eligible managed speculation. A canary
   still requires current headroom. Trusted broad pressure may consider the latter path, after which
   the authoritative planner independently proves priority, evidence, pins, ownership, active work,
   capacity, and the victim transition.
3. **Human explanations hid important evidence.** Logical Grid status now prints active hourly
   cost, observed/attested/qualifying cohort counts, graduation, chosen portfolio model, reason,
   target host, and proposed speculative victims. Status-wide cohort SLOs use the selected model's
   configured latency objective rather than error-only accounting when a portfolio projection
   exists.
4. **Controller epochs were arrival-ordered, not authority-ordered.** Automatic mode now uses a
   durable renewable single-writer lease with a monotonic takeover term. Commands carry the term,
   leader identity, and lease deadline. Every managed runtime persists its highest term, binds one
   leader per term, rejects expired or lower-term work before a side effect, and preserves that
   fence across restart. Tests cover lease contention, renewal, expiry, takeover, conflicting
   leaders, reordered delivery, restart, and same-action receipt replay.
5. **Outcome evidence never expired and selection had no exploration pressure.** Model/workload
   confidence now decays with a seven-day half-life, quality and request evidence have separate
   sample thresholds, and an uncertainty bonus grants bounded canaries to feasible cold peers. The
   bonus decays to zero with evidence and is smaller than the penalty for a preemption-only arm.
6. **Cost was only a soft ranking hint.** Operators can now set a durable hard hourly fleet ceiling.
   Selected-host cost is charged once across colocated models, unknown prices fail closed unless
   explicitly allowed, and a tighter ceiling stages removal only for Grid-owned unpinned work.
   Status separates affordable desired cost from current live cost so pinned, external, or manual
   overage cannot masquerade as compliance.
7. **Portfolio choices were independent per workload.** Multi-workload demand now enters a bounded
   joint search whose complete candidate sets are evaluated by the real placement planner. It can
   reuse one generalist when independently preferred specialists cannot coexist, preserves baseline
   and direct service first, and limits uncertainty-driven exploration to one distinct model across
   the fleet. Status exposes the chosen mapping and exploration slot.

### Open panel priorities

1. Extend the new hourly placement ceiling into spend forecasts, cumulative budget windows, and
   budget-constrained capacity recommendations.
2. Expand model utility evaluation beyond tiny exact-answer coding probes and exercise real vLLM,
   ComfyUI, artifact transfer, long-context, and multi-physical-node failure conditions.
3. Replace the local lease-file authority with a consensus-backed term allocator before running
   multiple active control-plane replicas on separate physical machines.

Every later panel should record the exact revision, workload, hardware, real-versus-modeled boundary,
scores, disagreements, reproduced failures, and whether each accepted finding gained an integration
test. A panel opinion without a reproducible counterexample or measurable acceptance gate remains a
hypothesis.

## 2026-08-29 economics follow-up

The same three independent roles reviewed revision `6e76c1d` specifically for price provenance,
budget safety, distributed ownership, serving-quality tradeoffs, and cohort fairness. Their shared
P0 finding was reproducible: a host-scoped worker credential could report its own zero price and
make that host appear affordable. The serving judge rated accounting integrity 2/10 at that
revision; the distributed-systems judge rated the implemented follow-up design 8/10 for pricing
integrity and restart recovery while retaining a 4/10 multi-controller score.

Revisions `83f3dde` and `77a8646` accepted and tested the immediate findings:

1. Physical-host prices are now a bounded, durable operator registry. Worker values are stripped
   before accounting, the registry is applied once after child records are merged, and direct
   controller callers reapply it inside the planning lock. Explicit zero remains known-free;
   absence is unknown. The authenticated API/CLI, persistence, rollback, restart, forged-node,
   one-host/children, provenance, and direct-controller paths have regression coverage.
2. Host-price and budget edits are desired-state transactions. Both compare current and proposed
   placement against one demand forecast and reject a coverage loss unless the operator gives a
   one-time `allow-service-shortfall` acknowledgement. This now protects active demand/SLO-driven
   replicas even when a model's configured minimum is zero.
3. A real four-logical-node test used llama.cpp/Metal and ComfyUI/MPS. Raising the coder host from
   `$0.80/h` to `$20/h` made a `$1/h` budget reject with the exact `1 -> 0` coder impact. After an
   explicit acknowledgement, Grid drained and unloaded the real coder. Lowering that host to
   `$0.30/h` caused Grid to load and warm the measured coder winner again; desired known cost
   converged to `$0.55/h`. The test Grid was then restored to no ceiling and its original
   `$0.05/$0.20/$0.80` operator prices.

Two subsequent changes closed more of the follow-up. Equal-share scarcity ties now use trusted
cohort/SLO harm and measured load rather than model-name order (`39a27ba`), with a name-swap
metamorphic test. Budget and price writes also acquire the durable controller authority lease before
mutation, so a live standby receives HTTP 409 and takeover advances the term even if both processes
have not yet entered automatic mode. A higher-term successor reloads the last durable controller
state before persisting its term, preventing stale standby memory from erasing the former leader's
host prices or budget.

Revisions `6cdb91c` and `894cb55` then closed the stale-writer and acknowledgement-audit gates.
Every price or budget mutation now requires an expected economics revision. The CLI reads that
revision immediately before a write unless the operator supplies one explicitly; the API rejects a
missing precondition with HTTP 428 and a stale precondition with HTTP 409. Material changes advance
one monotonic revision, no-ops do not, and controller takeover reloads the durable revision before
accepting a write. The bounded durable audit records controller term and identity, acknowledgement,
before/after policy or price, and the exact before/after/desired replica impact. Tests cover stale
concurrent writers, rollback, restart, takeover, missing preconditions, and audit persistence.

A fresh four-logical-node run at revision 4 reproduced the complete real lifecycle. A client using
stale revision 3 was rejected without mutation. An acknowledged `$1/h` ceiling committed revision 5,
recorded the baseline's exact `3 -> 2` replica impact against desired 3, chose the two cheapest hosts
at `$0.25/h`, and completed real drain and unload actions on the `$20/h` llama.cpp host. Disabling the
ceiling committed revision 6 and loaded/warmed that host back to ready. Restoring its operator price
to `$0.80/h` committed revision 7; all three text replicas and the immutable ComfyUI/MPS inventory
were healthy afterward.

Open findings are an atomic multi-change transaction for changing several prices and a budget with
one revision (avoiding intermediate policies), richer per-cohort loss previews, and consensus-backed
authority for multiple active physical control-plane replicas. Those are the next acceptance gates;
they are not claimed as solved by the CAS milestone.
