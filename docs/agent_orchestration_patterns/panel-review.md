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

---

## Round 16 — label-legibility hardening + the harness-selection guide

**Critique (GoF / Local-AI lens).** A real rendering fault surfaced on the
`fan-out` (#2) figure: the `single` and `expand` edge labels were stacked at
the same edge midpoint, producing garbled, unreadable text. The verifier
checked node-vs-label overlap but never **label-to-label** collision, so the
defect shipped past the geometry gate. Independently, the architecture lens
flagged that the lane table told *how to drive* a session but not *which
harness to drive* — the frameworks (OpenClaw, Hermes, Claude Cowork, Claude
Code, Codex, Pi) were named but their strengths, weaknesses, and when-to-pick
were not stated as a decision aid.

**Changes applied.**
- **Provable label clearance** in `build_diagrams.py`: upgraded
  `_label_boxes` to enforce a fixed `GAP = 8.0` px minimum clearance between
  any two edge labels and node boxes, with 80 spreading passes clamped to the
  viewBox. `fanout.svg` verified: `a single answer` @y294 and
  `ties → expand` @y328 now clear each other.
- **"Which harness for which job"** guide added after the lane table: a
  five-layer taxonomy — **Channel=OpenClaw**, **Memory=Hermes Agent**,
  **Desktop=Claude Cowork**, **Coding=Claude Code**,
  **Command-center=Codex** — plus **Pi** (minimal auditable seat) and the
  "home bases reach outward, the layer picks the harness" rule.
- Wired the layer/harness decision into "Choosing a pattern — decision order"
  step 2 (`layer picks the harness, residency picks the lane`).

**Verification.** Label audit: 0 residual label-label overlaps across all 36
figures. Agent generator re-run green. Markdown balanced. All facts preserved
(pure additions + the drawing fix).

---

## Round 17 — refresh example models to current SoTA local names

**Critique (Local-AI lens).** The example models were drifting out of date:
`glm-4.6` is superseded (GLM-5.2 is current), and the rostered `qwen36-*`
family names lag the current "qwen 3.8 27B" generation. Example stack tags are
illustrative placeholders (per *A word on the examples*), but a stale name on
a figure still reads as the catalog being behind the hardware it claims.

**Changes applied.** Coordinated, figure-safe rename across both catalogs and
the agent generator (model figures carry no model names, so only the agent
figures needed a rebuild):
- `qwen36-27b[-mtp]` → `qwen38-27b[-mtp]`, `qwen36-35b[-a3b-mtp]` →
  `qwen38-35b[-a3b-mtp]` (user-confirmed current local pick), and
  `glm-4.6` → `glm-5.2`.
- Kept `deepseek-v4-flash` (user-confirmed) and `qwen3-coder` untouched.
- Regenerated all 9 agent figures; 30/30 model-tag labels refreshed, 0 old
  tokens remain.

**Verification.** Agent generator `9/9` figures `verify ok`. Backticks and bold
balanced in both READMEs. Rename confined to the pattern catalogs — operational
docs (`cli.md`, `reference.md`, protocol drafts) left untouched to avoid
decoupling from the real CLI model catalog.

---

## Round 18 — consistency audits + deepseek exemplar alignment

**Critique (Architecture lens).** Round 17 renamed the rostered `qwen36`/`glm-4.6`
tags but left two prose exemplars in the model layer still speaking the older
`deepseek-v3` family while the agent figures and README already used
`deepseek-v4-flash` — an internal drift between "the deepseek that appears in
examples" and "the deepseek drawn on figures." The same lens re-checks the
agent layer for structural and cross-reference drift, since those were the two
bug classes the model layer had to fix in earlier rounds.

**Changes applied.** Aligned the two model-layer `deepseek-v3` exemplars to
`deepseek-v4-flash` (pure token rename, no structural change), so every deepseek
reference across both catalogs names the current flash-tier model.

**Verification (all green).**
- **Stale-token scan:** `qwen36`, `glm-4.6`, `llama-3` — 0 hits across both
  READMEs and the agent figures.
- **Section order:** all 7 agent patterns carry the 12-element skeleton in the
  exact announced order (Intent…Known Uses…Failure mode…Refinements…Sample Code…
  On the Grid stack…Related Patterns) — no announce-vs-actual drift.
- **Cross-reference integrity:** every model-layer `#N` the agent patterns lift
  maps correctly — `#16` straggler→handoff, `#24` screening→probing unknowns,
  `#26` slack-stealing→seat-as-executor, `#18` canary→admission, `#12` Markowitz,
  `#2/#11` fan-out/negative-selection→family independence.
- **Figures:** agent generator `9/9` re-ran `verify ok`; working tree byte-clean
  (no spurious regeneration), confirming the committed SVGs already carry the
  label-collision fix.

**Round state.** The agent layer is at a high, audited plateau: uniform
skeleton, accurate cross-refs, current model names, and a framework home-base
guide (OpenClaw/Hermes/Claude Cowork/Claude Code/Codex/Pi) that already
satisfies "when to use what." Further blind edits risk churn; remaining
high-leverage work is chiefly verifying 2026-era framework facts against
sources and the diagram-beauty pass.

---

## Round 19 — ground the harness home-bases; kill the ACP/Agent conflation

**Critique (Local-AI + GoF lens).** The "which harness for which job" guide was
the user's explicit ask ("understand their strengths and weaknesses and when to
use what") and it was *right in shape* — but it read as taste, not fact. A
Design-Patterns-grade catalog must make its claims auditable, and a
five-product taxonomy needs (a) a source a reader can point at and (b) no
term a reader can mis-read. Two real gaps surfaced: (1) none of the five
home-bases had a live, checkable provenance, and (2) the doc used "Hermes ACP"
(8×, a *lane*/transport) and "Hermes Agent" (4×, the Nous *product*) in a way
that invites the exact conflation the taxonomy is trying to prevent.

**Changes applied (all grounded against live sources this round, then committed).**
- **Provenance footnote** after the when-not-to list: names each home-base's
  real, current source — OpenClaw `openclaw/openclaw` ("your own personal AI
  assistant… any OS, any platform"), Hermes Agent `NousResearch/hermes-agent`
  ("the agent that grows with you" + self-evolution / agent-governance
  siblings), Claude Code `anthropics/claude-code`, Codex `openai/codex`, Pi
  `earendil-works/pi` (quoted for the property the catalog leans on hardest:
  *no built-in permission system; runs with the user's permissions*), and
  Claude Cowork (Anthropic managed desktop; name confirmed via the many "Open
  Source version of Claude Cowork" reimplementations, since there is no public
  repo). Closes with the invariant: the rosters move, the layers do not.
- **ACP ≠ Hermes Agent** one-liner under the lane table: ACP = **Agent Client
  Protocol** (`agentclientprotocol/agent-client-protocol`, "a protocol for
  connecting any editor to any agent") — the wire; Hermes Agent = the memory
  product that speaks it. Named which term this catalog writes for which.
- Re-rendered the 9 agent figures to fresh PNGs (true-aspect, 2×) and ran an
  independent per-figure label-bounding-box overlap check: **0 residual
  overlaps** on all 8 pattern figures; the earlier `index.svg` "collisions" are
  my checker ignoring the `translate(30,cursor)` tile offsets (false positives).

**Verification.** 9/9 agent figures `verify ok` on re-run. Backticks 314 and
bold runs even in the agent README. All 9 image refs resolve; working tree
clean after two pushes (`518df60` provenance, `b5a8510` ACP/Agent).

**Source checks (this round).** `openclaw/openclaw` 200; `anthropics/claude-code`,
`openai/codex`, `NousResearch/hermes-agent`, `earendil-works/pi` (93.7k★),
`agentclientprotocol/agent-client-protocol` (4k★), `openclaw/acpx`, and
Claude-Cowork reimplementations all confirmed; `Qwen/Qwen3.8-27B` and
`QwenLM/Qwen3-Coder` confirm the current code-specialist line, so `qwen3-coder`
is **not** stale — it is the real Qwen code variant, distinct from the
`qwen38-27b` base seat.

**Round state.** The agent layer's harness taxonomy is now *asserted from a
source*, not from taste, and its two most-conflatable terms are disambiguated
in-line. Next high-leverage items: write the missing `agents.md` agent-layer
companion (referenced, absent), and a per-pattern "Known Uses" precision pass.
