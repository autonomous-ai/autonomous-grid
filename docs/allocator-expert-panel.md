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

### Open panel priorities

1. Add confidence-aware, freshness-decayed portfolio exploration and optimize a joint model set,
   rather than independently choosing one greedy winner per workload.
2. Add explicit fleet cost budgets, unknown-cost handling, spend forecasts, and budget-constrained
   capacity recommendations.
3. Expand model utility evaluation beyond tiny exact-answer coding probes and exercise real vLLM,
   ComfyUI, artifact transfer, long-context, and multi-physical-node failure conditions.
4. Replace the local lease-file authority with a consensus-backed term allocator before running
   multiple active control-plane replicas on separate physical machines.

Every later panel should record the exact revision, workload, hardware, real-versus-modeled boundary,
scores, disagreements, reproduced failures, and whether each accepted finding gained an integration
test. A panel opinion without a reproducible counterexample or measurable acceptance gate remains a
hypothesis.
