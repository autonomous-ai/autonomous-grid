# Agent Orchestration Patterns — panel review log

This file is the loop for the **agent layer** (the second, shorter half of the
catalog). It mirrors the model layer's panel and logs each round's critique,
the changes it drove, and the items it left open.

## The panel

Four lenses, one per reviewer role:

1. **Design Patterns (GoF)** — structure, writing voice, diagram conventions of
   *Design Patterns: Elements of Reusable Object-Oriented Software*.
2. **Software architecture** — terminology precision, cross-pattern consistency,
   actionability.
3. **AI / ML engineering** — correctness of citations and statistics.
4. **Local AI / on-device** — realism of the hardware and token economics.

(Working note: I attempted to run these as four live sub-agents; the sub-agent
runtime in this session did not persist them, so I ran the four lenses directly.
The lens split and its findings below are unchanged by that.)

---

## Round 14 — Known Uses for the agent layer

**Critique (GoF lens).** The model layer's *Known Uses* round closed with
27/27 patterns anchored to real systems, but the agent layer — which is
where the "which agent acts, and how do co-located agents stay honest" claims
live — had no `Known Uses` at all. A reader of e.g. the act-gate or the
only-one-ledger pattern had prose assurance but no "who already runs this"
grounding, which is exactly the GoF section that turns an abstraction into a
credible one. The agent catalog's "How to read a pattern" skeleton also
silently shipped `Sample Code` without announcing the `Known Uses` it 
already had — an inconsistency with the model layer's announced skeleton.

**Changes applied.** Added **Known Uses** to all 7 agent patterns, placed
between `Consequences` and `Failure mode` (GoF: talk costs, then name who
actually does this, then say how it breaks), each anchored to a concrete real
discipline rather than invented rhetoric:

- #1 act-gate → PostgreSQL advisory locks / etcd leader leases / actor
  single-threaded mailboxes / etag-or-CAS mutations (N−1 may read, one may
  write).
- #2 warm/handoff → connection pools, OS process/container lifecycle, browser
  session-restore, keep-alive caches (expensive spawn becomes a cheap resume).
- #3 harness routing → RBAC role→lane→gate policy routing; CI platforms route
  a job to the runner that may run it.
- #4 seat-as-executor → preemptive scheduling, cloud spot/preemptible
  instances, Kubernetes eviction (preemptible + restartable from checkpoint).
- #5 staged admission → canary → controlled → full rollouts, CI trust ladders
  (build → test → integration → production).
- #6 deterministic-authority → tests / schema checks / CI as the ground-truth
  authority; a reward model is a verifier the same way a test is.
- #7 only-one-ledger → write-ahead logging (PostgreSQL WAL, SQLite rollback
  journal), append-only event logs as the single source of truth.

Also updated the agent layer's "How to read a pattern" front-matter list to
announce **Known Uses** between `Consequences` and `Failure mode`, matching the
model layer.

**Verification.** Section-order audit: `NONE` out of order across all 7.
`**Known Uses.**` count = 7. All python fences `ast.parse` clean, backticks
balanced, no blank-line artifacts. Pure additions — no existing fact touched.

**Round 14 finding.** The agent catalog is now coherent against its announced
skeleton: 7/7 patterns carry prose, figure, Known Uses, and Sample Code. The
remaining candidate work is cross-catalog symmetry and a final holistic read of
both front matters (the model layer already has the figure legend, decision
order, and catalog map; the agent layer should re-check the same aids), plus
re-running the diagram beauty pass on both catalogs.

---

## Round 15 — cross-catalog verification + figure-count correction

**Critique (architecture lens).** The agent layer's "How to read the figures"
claimed the visual language appeared in "all eight figures," but the catalog
shapes only **seven** pattern figures (the `index.svg` map and the `e2e.svg`
whole-system figure are auxiliary, exactly as the model layer counts its 27
pattern figures against its 28 SVGs). A mis-stated figure count is the kind of
small factual drift the panel exists to catch.

**Changes applied.** Corrected the legend to "all seven figures." Re-ran both
diagram generators: model 27/27 `verok`, agent 7/7 `verify ok`, regenerated
SVGs byte-identical (no drift, `git status` clean). Ran a single cross-catalog
verification: section order uniform on the 12-element skeleton; `ast.parse`
clean on all python fences (27 model + 7 agent); backticks balanced; all 28
model + 9 agent image refs resolve; blank-line hygiene clean.

**Verification.** Pure corrections — no prose or figure re-edited. The false
"order failures" some scripts report for the model layer are the 12 patterns
that intentionally lack `Refinements` (guidance is already cohesive narrative),
which is the announced "where guidance separates" rule and not a defect.

**Round 15 finding.** Both catalogs are internally consistent and verified;
the plateau is stable. Remaining high-leverage (non-churn) candidates are a
final human read of the assembled front matters end to end and, if more beauty
is wanted, an optional palette/typography revision with fresh PNG renders.
