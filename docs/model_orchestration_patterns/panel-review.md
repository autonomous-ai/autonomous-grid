# Orchestration Patterns — panel review log

This file is the loop: each round is a critique, the changes it drove, and the
items it left open. It exists so the catalog's quality has a record and the
next round starts from the last one instead of the first.

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

## Round 1 — critique

**GoF lens.** The catalog already carries the right spine (Intent, the
structure diagram, tradeoffs, related patterns), but the section names drifted
off the book's vocabulary (`Motive`, `Tradeoffs`), it had no `Also Known As`,
no `Related Patterns` footer, no up-front statement of how a pattern is
documented, and several patterns carried duplicated `Mechanics` blocks where
the second block was really implementation guidance.

**Architecture lens.** The `#number` cross-referencing is good, but a reader
can't tell at a glance where a pattern's liabilities live, and the catalog
promises companion docs (`ROUTER.md`, `agents.md`, `router-execution.md`) that
are not in this repository.

**AI/ML lens.** The statistical claims are sound and well-attributed
(self-consistency/Wang 2022, Markowitz, CVaR, PID, Condorcet/Arrow, Thompson,
trial-sequential analysis, speculative decoding). The "failure modes" are the
correct ones for the algorithms described; the main risk is *overreach* from
citation to local claim, which the "On the Grid stack" blocks mostly keep
honest.

**Local-AI lens.** The single-GPU caveats (co-resident vs. VRAM-swapped,
serialized reads, seat contention) are the strongest part of the document; the
risk is that a pattern's diagram or prose re-asserts an "in parallel" promise
a one-box reader cannot keep.

## Round 1 — changes applied

- **GoF vocabulary**: `Motive` → `Motivation`, `Tradeoffs` → `Consequences`.
- **`Also Known As`** added to all 27 patterns (one line each).
- **`Related Patterns`** added to all 27 patterns, cross-referencing the
  families already implied by the `#`-numbers.
- **`How to read a pattern`** preface: the fixed skeleton (Intent / Also Known
  As / Motivation / Structure / Mechanics / Consequences / Failure mode /
  On the Grid stack + Related Patterns) announced up front, as GoF does.
- **`Mechanics` → `Refinements`** for the second, implementation-flavored
  `Mechanics` block in patterns 2, 4, 6, 7, 8, 9, 10, matching GoF's
  separation of *how it works* from *how to build it*.
- **Diagrams**: all 27 pattern figures + the composed `index.svg` were rebuilt
  from a single token-driven engine (`build_diagrams.py`) — one geometry, one
  palette (docs/STYLE.md), auto-sized canvases, and a built-in checker that
  asserts labels fit and nodes never overlap. The old hand-authored SVGs had
  drifted widths, misaligned labels, and couldn't be re-rendered.

## Round 1 — open items (next rounds)

1. **Companion docs**: `ROUTER.md`, `agents.md`, `router-execution.md` are
   referenced but absent from this repo. Decide whether they ship here (create
   them) or the references are cross-repo links.
2. **Style-guide refs**: `knowledge/diagram-style.md` and
   `knowledge/technical-writing-style.md` point outside this repo; the local
   equivalents are `docs/STYLE.md` / `docs/DIAGRAMS.md` (point to them or
   vendor the standard).
3. **Per-pattern prose**: the dense body *prose* (as opposed to structure) is
   only partially edited; a full sentence-by-sentence tightening under the GoF
   voice is a separate round.
4. **Shortening**: the "On the Grid stack" blocks are long; consider a
   two-line version plus a collapsible detail for faster scanning.
5. **Overreach audit**: re-read each pattern for any local claim the citation
   does not actually license (the single most important soundness pass).

*Next round: pick up the open items, then re-review.*

---

## Round 2 — critique & changes

**Critique.** The diagram figures were rebuilt blind (no visual render was
reviewable in-session), so the round's job was to make the *layout provably
correct* rather than merely asserted: node boxes could be guaranteed non-
overlapping, but edge labels could still land on an unrelated node, and
return-loop bands could cut through a lower row of nodes.

**Changes applied.**
- Extended the generator's built-in verifier to also flag **edge labels that
  overlap an unrelated node** (previously only node-node overlap and
  label-fits-node were checked).
- Fixed the two collisions it found: `materialized`'s cache-hit shortcut was
  crossing the `pattern` node (moved the compute path to a lower row so the
  dashed hit path is clear); `canary`'s `shadow` label overlapped the
  `incumbent` node (removed the forced position, let it auto-place below).
- Fixed **return-loop bands** (`delphi`, `debate`, `pheromone`, `verify`) so
  the dashed "revise/again/re-command/retry" loop drops *below the lowest
  node* instead of cutting through a lower row.
- Full set re-renders clean: `27/27` figure checks pass, no problems.

**Status.** Diagrams: consistent, re-generatable, geometry-verified. Structural
prose (GoF skeleton): complete across all 27 patterns. Remaining open items:
{1 companion docs, 2 style-guide refs, 3 sentence-level prose tightening,
4 shortening the On-the-Grid blocks, 5 overreach audit}.

---

## Round 3 — overreach / citations audit

The highest-soundness risk in a pattern catalog is a local claim that its
citation does not actually license. Findings from a full re-read, pattern by
pattern:

- **Fan-Out (#2)** — self-consistency (Wang et al. 2022). Claim sound: majority
  over *independent* samples beats greedy decoding. The doc already carries the
  load-bearing caveat (unanimity is evidence only conditional on decorrelation)
  and the `2-of-3 shared-family quorum` honesty. **No overreach.**
- **Brute-Force (#6)** — best-of-N *selection*. The doc correctly separates
  self-consistency's vote (cited) from selection's floor bound (best-of-N is
  never worse than one greedy draw) and refuses to let an LLM picker masquerade
  as a verifier. **No overreach.**
- **Markowitz (#12)** — portfolio variance minimization. Correctly requires a
  covariance computed over *labeled outcomes*, not raw disagreement, and a
  min-observation gate before weights are trusted (`unmeasured|measured`).
  **No overreach.**
- **CVaR (#19)** — requires a priced loss, not a difficulty weight "in
  costume"; degrades to the mean on a thin tail. **No overreach.**
- **Condorcet (#25)** — correctly invokes Arrow, requires *measured*
  independence of voters, and reports the fallback pooling used. **No overreach.**
- **PID (#13) / Thompson (#27) / Trial-Sequential (#22) / Screening (#24)** —
  each names its sensor/sample/ground-truth dependency and degrades honestly
  when it is absent. **No overreach.**

What the audit *did* find is structural, not factual: several of these
"degrade honestly" clauses are the document's best material, but they are
buried in long `Refinements` paragraphs. The Round-4 opportunity is to surface
**the single honest-degradation sentence** in each pattern (e.g. "gate on a
min-observation count") as a scannable line rather than prose.

---

## Round 4 — prose restructuring (the honest-degradation surfacing)

**Critique.** The Round-3 audit identified that each pattern's best material —
its single "degrade honestly" build rule — was buried in long prose walls (the
`Refinements` guidance sat inside `Failure mode` or a duplicate `Mechanics`).
GoF separates *how it fails* from *how to build it*.

**Changes applied.**
- Split the longest `Failure mode` blocks into a focused `Failure mode` + a
  numbered **`Refinements`** list of the concrete build rules, in patterns
  **13, 15, 16, 17, 11, 21** (preserving every fact; only reorganizing).
- Trimmed redundant restatement added earlier (e.g. #13's `On the Grid` no
  longer re-repeats the refinement rules).
- Updated the "How to read a pattern" preface to name `Refinements` in the
  announced skeleton, so the front matter matches reality.
- Full structure integrity re-verified: all 27 patterns carry the complete
  skeleton (`Intent · Also Known As · Motivation · Structure · Mechanics ·
  Consequences · Failure mode · On the Grid stack · Related Patterns`), with
  `Refinements` where implementation guidance exists, and no missing or
  duplicate headings.

---

## Round 5 — the On-the-Grid trimming (open item 4)

**Changes applied.** Compressed the two longest `On the Grid stack` blocks
(`#24` Type-Revelation Screening, `#25` Condorcet) to roughly two-thirds, keeping
every concrete fact (probe set, calibrated profile, VRAM-swap bound, admission
threshold; the 4/3/3 tournament, extraction-vs-tally latency, residency gate,
Copeland fallback, forced-diversity guardrail) while removing restated padding.
Full structure integrity re-verified: all 27 patterns complete, no missing or
duplicate headings.

---

## Round 6 — companion-doc provenance, decision-key, live-panel retry

**Changes applied.**
- **Cross-repo provenance (open item 1).** Confirmed `ROUTER.md` and
  `router-execution.md` live in the sibling research doc at
  `autonomous-org/projects/grid-orchestration/`; `agents.md` exists nowhere.
  The bare `agents.md` link (which 404'd locally) became plain
  text, and a front-matter note now states where the companions live and that
  `agents.md` is the not-yet-published agent-layer half. No dangling git link
  remains.
- **Decision order (new, GoF "how to use this book" aid).** Added a compact
  **"Choosing a pattern — the decision order"** block after the one-sentence
  table: a prioritized 9-step procedure (which question binds first decides
  the leaf) plus a closing note that #5/#6/#8 are selectors/coverage, not
  leaves. Non-redundant with the table — the table lists *what* each shape
  does, the key orders *which* to reach for.
- **Live sub-agent panel retried.** Spawned four judge agents (GoF / arch /
  AI-ML / local-AI) in a fresh session; the runtime again failed to persist
  them (`not_found` on wait). Ran the four lenses directly instead (below).

**Verification.** README structure integrity: all 27 skeletons complete, no
missing/duplicate headings. Diagrams: 27/27 `verok`.

**Round 6 finding (direct lenses).** The prose is now at the point where the
remaining gains are *consistency* and *spot tightening*, not restructure. The
four lenses' concrete flags to fold in next:
1. Cross-check the one-sentence table's "Use it when" text against each
   pattern's `Failure mode` for any promise the rest of the entry doesn't
   keep.
2. Verify the decision-key's step numbering and #-targets exactly match the
   catalogue's own cross-references.
3. Keep scanning for passive-voice residue in the `Motivation`/`Consequences`
   of the later patterns (#18–#27), which were edited least.

---

## Round 7 — Refinements coverage, prose scan, front-matter fix

**Changes applied.**
- **`Refinements` expanded (open item from Round 4/6).** Split the two
  remaining build-rule walls — **#20** Circuit Breaker (five rules: measured
  threshold, named degrade path, metered recovery probes, mid-request-trip
  state change, envelope honesty) and **#24** Type-Revelation (five rules:
  rotate the exam, stay mostly type-driven, atomic preempted updates, durable
  type-map, slack-valid probes) — out of their `Mechanics` into numbered
  `Refinements`, matching the 13 patterns already split. `Refinements` now
  appears in 15 of 27 patterns. Every fact preserved (verified by token
  presence + structure check).
- **Passive-voice / verbosity scan:** swept all 27 for weak constructs
  (`in order to`, `It is`, `there is`, `due to the fact`, etc.) — the doc is
  clean; no edit-worthy residue found in the least-edited patterns (#18–#27).
- **Front-matter bug fix:** the Round-6 decision-key insertion left a stray
  `**Two levers` fragment duplicating the next heading; removed, and aligned
  list-item continuation indentation.

**Verification.** Structure integrity: all 27 skeletons complete; `Refinements`
15/27. Facts preserved; diagrams 27/27 `verok`.

**Round 7 finding (direct lenses).** The catalog has now absorbed the GoF
restructure (`Motivation`/`Consequences`, `Also Known As`, `Related Patterns`,
`Refinements`, decision-order key). Remaining gains are *audit* (statement-vs-
diagram consistency per pattern) and *front-matter polish*, not restructure.
**Open:** (a) confirm each pattern's `Structure` caption matches its SVG
figure exactly; (b) confirm the decision-key's step targets (#-numbers) match
the catalogue cross-references (spot-checked correct); (c) consider a one-page
"catalog map" figure linking the 27 by the listed families.

---

## Round 8 — statement-vs-diagram audit + catalog map

**Changes applied.** All 27 patterns carry a figure with a caption; verified
one-to-one (`images/*.svg`, no missing/duplicate images, captions consistent
with each pattern's `Structure`). The composed overview `images/index.svg`
(which renders all 27 patterns, verified present by title) was an **orphan
asset** — added a "The catalog, as one figure" pointer with the index at the
catalog's entry (before pattern #1), framing the families as
primitive → composition → stateful → epistemic → machinery. Structure
integrity re-verified: all 27 complete.

## Round 9 — related-pattern network audit

**Finding: clean, no edit.** Audited every `Related Patterns` footer: all
cross-references fall in 1–27, none out of range, and the apparent
"self-references" (#6, #7, #8, #16, #19, #23, #25, #26, #27) are legitimate
prose ("#6's `best`", "#8 to make `best` deterministic", "#27 escapes") —
a pattern naming its own node/role in a comparator sentence, not an erroneous
pointer. The family graph is internally consistent; no change made.

**Panel status.** The catalog has absorbed the GoF restructure and now passes
every audit thrown at it (structure, diagrams, statements-vs-figures, related-
pattern graph). Further rounds are converging on verification passing clean;
remaining open item is the optional `agents.md` companion (external to this
repo) and any fresh judge critique.

---

## Round 10 — figure legend + visual deliverables

**Changes applied.** Added a **"How to read the figures"** field guide in the
front matter, paired with "How to read a pattern": the four node roles
(coral = request in/out, green = work, purple = decision, dashed edge = return
loop) stated once, so any of the 27 diagrams is legible without a cross-doc
trip to `docs/DIAGRAMS.md`. The role→color mapping was verified against
`build_diagrams.py` (`work`=green, `decide`=purple, `terminal`=coral,
`dashed`=revise/return). Structure integrity re-verified: all 27 complete.

**Visual deliverables (in /tmp, not committed).** Rendered all 27 figures +
the composed `index.svg` to PNGs at their true viewBox aspect ratios via
headless Chrome (`/tmp/op_all/fresh/`), and built a self-contained
`/tmp/op_all/index.html` that inlines every SVG with captions for a single-
glance review of the whole catalog. `view_image` is unavailable in-session,
so the geometry verifier in the generator is the in-session guarantee.

**Round 10 finding.** Front matter now fully self-contains the reading
instructions (skeleton + figure legend + decision order + catalog map). The
catalog is at a strong, internally consistent state.

---

## Round 11 — section-order standardization (fixed-skeleton consistency)

**Critique (architecture lens).** The front-matter skeleton announced
`Mechanics → Consequences → Failure mode → Refinements → On the Grid`, but the
patterns placed `Refinements` inconsistently: 10 correctly after `Failure
mode`, but #2/#7/#10 before it, and #20/#24 (the Round-7 splits) directly
after `Mechanics`. A "fixed skeleton" promise requires one order.

**Changes applied.** Relocated every `Refinements` block to sit between
`Failure mode` and `On the Grid stack` — the order the front matter announces
and the majority already use ⇒ uniform across all 15 patterns. Fixed the
front-matter announcement to the same order. (First attempt at a programmatic
pass corrupted the file; rolled back via `git checkout`, then redid it as a
surgical block-swap with the section-header parser corrected for prose-on-the-
same-line headers.)

**Verification.** Order audit: `NONE` out of order across all 27. Structure:
all 27 skeletons complete. Word count identical before/after (27187 = 27187)
and diff is 49/49 lines = pure reorder, zero content change — every fact
preserved.

---

## Round 12 — GoF Sample Code: complete the catalog (all 27)

**Critique (GoF lens).** The book's *Sample Code* — a compact, parameter-
named sketch added to every pattern — is one of its most-used sections, but
this catalog only had it in the 7-pattern agent catalog (done earlier) and the
first 19 model patterns. Patterns #20–#27 were the gap, exactly the machinery
patterns where a runnable-in-spirit sketch (fail-fast breaker, anonymous
median rounds, pre-registered evidence barrier, consequence-priced shelf,
screening battery, pairwise tournament, slack steal, posterior draw) most
helps a practitioner.

**Changes applied.** Added **Sample Code** to the final eight model patterns
(#20 Circuit Breaker+Bulkhead, #21 Delphi, #22 Trial Sequential, #23
Evidence-Bar Ladder, #24 Screening, #25 Condorcet, #26 Slack-Stealing, #27
Thompson Router), each inserted between `Refinements`/`Consequences` and `On
the Grid stack`, matching the announced skeleton and the established header/
comment-caveat style. Every snippet carries the pattern's honest caveat in a
comment (grounded → measured threshold; single-node → bulkhead is a fiction;
refusal → never front-run a streak; screening → only in idle + <2 resident
refuses; condorcet → never tally over N−1; slack → idlers only in the slack
window; thompson → only verified tool-grounded labels).

**Verification.** `ast.parse` on all 27 fences: no SyntaxError. Triple-backtick
balance: even. No 4+-blank-line artifacts. Section-order audit: `NONE` out of
order across all 27 (Sample Code sits before `On the Grid stack` in every
pattern). All 27 model patterns now carry Sample Code (100%), matching the
agent catalog's 7/7.

**Round 12 finding.** The model catalog is now prose + figure + sample-code
complete for every pattern. Remaining candidate work: a "Known Uses" column
in the GoF sense (real deployments per pattern), and re-running the cross-
catalog audits after the rename.

---

## Round 13 — GoF Known Uses: anchor the catalog (27/27)

**Critique (GoF lens).** The book's *Known Uses* closes each pattern by naming
real systems that already run the shape — the abstraction is anchored, not
invented. This catalog had the section nowhere; a reader had no reason to
believe e.g. Fan-Out or Trial-Sequential were real techniques rather than
rhetoric.

**Changes applied.** Added **Known Uses** to all 27 model patterns, placed
between `Consequences` and `Failure mode` (GoF: talk costs, then name who
actually does this, then say how it breaks). Anchored each shape to a concrete
real deployment: self-consistency (Fan-Out), plan-and-execute (Master/Slave),
best-of-N (Brute-Force), reward-model reranking (Verifier Gate), multi-agent
debate, correlation-aware ensembling, PBFT (Byzantine), MapReduce speculative
execution (Straggler), Hystrix/resilience4j (Circuit Breaker), RAND Delphi,
group-sequential TSA, legal evidentiary standards (Evidence-Bar), screening
in the economics of information, ranked-choice/Condorcet, EDF/Kubernetes
preemption (Slack-Stealing), industrial Thompson sampling. Updated the
"How to read a pattern" front-matter list to announce `Known Uses`.

**Verification.** Section-order audit: `NONE` out of order across all 27
(Model order is now Consistent ☑). `**Known Uses.**` count = 27. All python
fences `ast.parse` clean, backticks balanced, no blank-line artifacts. Pure
additions — no existing fact touched.

**Round 13 finding.** The model catalog is now complete against the announced
skeleton (Intent · AKA · Motivation · Structure · Mechanics · Consequences ·
**Known Uses** · Failure mode · Refinements · **Sample Code** · On the Grid ·
Related) — every pattern carries prose, figure, Known Uses, and Sample Code
(27/27 × 4). Remaining candidate work: extend Known Uses to the 7-pattern
agent catalog for symmetry, and a final holistic read of the assembled front
matter.

---

## Round 14 — agent-layer symmetry (closes Round 13's open item)

**Finding.** Round 13's "remaining candidate work — extend Known Uses to the
7-pattern agent catalog for symmetry" is now closed. The agent layer shipped
`Known Uses` in all 7 patterns (each anchored to a real discipline: advisory
locks/leader leases, connection pooling, RBAC role→lane→gate, preemptive
scheduling, canary/CI trust ladders, tests-as-ground-truth, write-ahead
logging) and announced the section in the agent "How to read a pattern"
front matter. Both catalogs now carry the same 12-element skeleton, verified
in a single cross-catalog pass: order uniform, `ast.parse` clean, backticks
balanced, all image refs resolve (28 model, 9 agent), diagrams regenerate
identically with geometry checks green (27/27 model `verok`, 7/7 agent).

**Round 14 finding.** The model-layer open item is resolved; the two catalogs
are symmetric against the announced skeleton. Correctness note: 12 model
patterns intentionally have no `Refinements` (their guidance is already
narrative/cohesive), which is the announced "where guidance separates" rule,
not an audit failure.

---

## Round 15 — diagram label-to-label collision, fixed everywhere (user report)

**Report.** The user flagged pattern #2 (`fan-out`) as unreadable: the edge
labels `a single answer` and `ties → expand` sat on edges that share the `vote`
node, so their default midpoints landed at nearly the same coordinates and
rendered as stacked, garbled text.

**Root cause.** `Diagram.verify()` checked node-vs-label boxes and node-vs-node
overlap but never **label-vs-label**. Both `fanout()` edges stem from `vote`,
so the shared-vertex labels collided silently — the geometry verifier couldn't
see it, so it shipped as "verok".

**Fix (applied to the shared `Diagram` in `build_diagrams.py`, which both the
model and agent catalogs import, so it propagates to all 27 + 7 + 2 aux
figures).**
- New `Diagram._label_boxes(W, Hh)`: computes every edge label's placement box
  and **iteratively spreads colliding pairs** along their smallest-overlap axis
  (up to 60 passes), then clamps boxes inside the viewBox.
- `render()`/`_elabel()` now draw from these resolved boxes, so the SVG matches
  the verified positions.
- `verify()` now checks the rendered label boxes for **label-vs-label**
  collisions (its safety net) as well as the existing viewBox/node checks.

**Result.** After regeneration all 27 model and 7 agent figures report `verok` /
`verify ok` with zero label-label overlaps. In `fanout.svg`, `a single answer`
(mid) and `ties → expand` (right) are separated by a full line height instead of
stacked. Fresh PNGs re-rendered at true aspect ratio for eyeballing.

**Panel note.** The 4-lens live sub-agent runtime still fails to persist
(`not_found`), so this round ran the GoF/architecture "diagram legibility" lens
directly and is logged as usual.

---

## Round 16 — min-clearance upgrade hardens the label fix (0 residual overlaps)

**Follow-up to Round 15.** Strict overlap-only spreading left a near-miss:
`A:lifecycle`'s `resume` and `restore` edge labels touched text boxes with
`ox = 0` (bounding boxes adjacent but not intersecting). Legibility requires a
guaranteed **minimum gap**, not merely "no intersection".

**Fix.** Upgraded `Diagram._label_boxes` to enforce a fixed `GAP = 8.0` (px)
clearance between any two resolved edge labels, raising the spreading iteration
cap to 80 passes. Labels are spread along the smallest-clearance axis and
clamped inside the viewBox so nothing is pushed out of frame.

**Verification (provable).**
- `python3 /tmp/label_audit.py` across all catalogs reports
  **0 residual label-label overlaps, 36/36 figures** (27 model + 7 agent + 2 aux).
- Both generators re-run green: model `27/27 verok`, agent `8/8` (verified).
- Re-rasterized all 36 SVGs to fresh true-aspect PNGs (`/tmp/op_fresh4/`) and
  rebuilt the single-pane viewer (`/tmp/op_all/index.html`, 37 cards) so every
  figure can be eyeballed in one page.

**Commit:** `516a00b` (enforce min 8px clearance between edge labels).

---

## Round 17 — refresh example models to current SoTA local names

**Critique (Local-AI lens).** `glm-4.6` and the rostered `qwen36-*` names in
the `On the Grid stack` examples were lagging the current local-model
generation ("qwen 3.8 27B", GLM-5.2). Example stack names are illustrative
placeholders, but a stale tag still dates the catalog.

**Changes applied.** Pure token rename in the model README text (the model
layer's figures are generated from a token set that carries no model names, so
no model SVGs required a rebuild): `qwen36-27b[-mtp]` → `qwen38-27b[-mtp]`,
`qwen36-35b[-a3b-mtp]` → `qwen38-35b[-a3b-mtp]`, `glm-4.6` → `glm-5.2`.
Kept `deepseek-v4-flash` and `qwen3-coder`.

**Verification.** Backticks and bold balanced; zero residual old tokens; prose
family references ("the Qwen tail") untouched. No structural change.

---

## Round 34 — GoF lens: model layer lacked per-pattern Applicability

**Critique (GoF lens).** GoF's canonical skeleton places **Applicability** — a
crisp "use it when / avoid it when" fork — immediately after *Motivation*, and
the model layer (unlike the agent layer, Round 33) had none of it as a
per-pattern section. The one-sentence table held every positive half in the
front matter; a reader landing mid-pattern had only the abstract Motivation,
with the "when to actually pick this" left implicit.

**Changes applied.** Added **Applicability** to all 27 model patterns, after
*Motivation* and before *Structure*. Each section is sourced from two things
already in the doc: the positive ("Use it when") from that pattern's own
one-sentence-table row, and the negative ("Avoid it when") from that pattern's
own *Failure mode* — e.g. #2 fan-out's unanimous-but-wrong, #7's baked-in bias
(→ #12), #8's rubber-stamping verifier, #13's ungrounded confidence proxy,
#22's pre-registration-as-theater, #26's non-preemptible background work.
Added **Applicability** to the model layer's "How to read a pattern" skeleton
announcement so the announced skeleton matches reality. Wrapped all inserted
lines to the catalog's ~76-char toolwidth and normalized blank-line spacing.

**Verification.** 27 pattern sections intact; every pattern now carries the
annotated skeleton with Applicability between Motivation and Structure
(script-checked, order bad: 0 across all 27); markdown globally balanced
(backtick/bold); collapsed stray triple blank lines.

**Open items.** Both layers now carry the full GoF skeleton with Applicability.
Next target: the model layer's diagram **edge-label clearance** audit — the
same label-to-label and label-vs-node collision class the agent layer recently
fixed — and a read of the two sample-code blocks against `STYLE.md`.

**Commit:** (R34 — this commit).

## Round 36 — style pass: normalize "-ly adverb" hyphenation (both layers)

**Critique.** Word-by-word review turned up inconsistent hyphenation of
compound modifiers built from an adverb ending in `-ly`. Per standard English
style (Chicago/AP), an adverb ending in `-ly` is never hyphenated onto the
word it modifies, so these were genuine errors, not voice choices.

**Changes applied (model layer, 14 sites):** `confidently wrong mean` /
`median`, `negatively correlated pairs` (×2), `differently initialized
models`, `mutually exclusive camps` (×2) / `readings`, `speculatively
cancelled waste`, `confidently stale answer` (×2), `newly admitted model`,
`newly good arm`, `genuinely evaluative ones`. Coined terms of art were left
intact: `exactly-once`, `atomically-or-nothing`, `early-exit`, `reply-count`,
`Early-return`.

**Changes applied (agent layer, 1 site):** `externally observable action`.
Left the compound noun `nightly-batch deadline` intact.

**Verification.** Markdown balanced (backtick + bold even) both layers;
structure intact (27 + 7 patterns, 27 + 7 Intent). Diff was pure
hyphenation normalization, meaning preserved.

**Commit/push.** (R36 — this commit).

## Round 41

**Change.** Added the model layer's missing GoF-signature closing: a **worked
case study — "One request, walked through the catalog"** — placed ahead of the
"Putting them together" synthesis, directly mirroring the agent layer's
one-box/one-defect walk. It drives a concrete correctness-gating request (a
lock-ordering deadlock-free verdict) through the actual shapes, so the
synthesis stops being theory:

- **The spend (#5)** — a formal, expensive-to-be-wrong property routes to
  spend-N, not Mate-in-One; #5 reads shape to choose.
- **The fan (#2, #11, #12)** — N drafts from the live node inventory, forced
  to diverge before the vote (#11), correlation-weighted (#12).
- **The certify (#8, #22)** — a lockdep-style deterministic check outranks the
  pool (#8); #22 pre-registers N so four-of-four can't certify on a lucky
  streak.
- **The state (#17, #24)** — semantic-key cache; type-probe in idle.
- **The risk (#19, #20)** — CVaR tail-priced spend; breaker trips a toxic
  class and escalates instead of re-spending.
- **The economics** — remote forces Mate-in-One-once; local buys
  spend-N-certified-by-fact. The two levers, made concrete.

Every `#N` used is a real model-layer pattern with its catalog semantics
preserved; no fact invented.

**Verification.** Markdown balanced (backticks/bold); 27 pattern headers
intact; all case-study refs in range 1–27. Diagrams untouched.

**Commit/push.** (R41 — this commit).

## Round 41b — deep per-claim audit of the case study (verified, no change)

Completed the missing deep audit of "One request, walked through the catalog":
read the narrative in full and checked each claim's cited pattern against that
pattern's own `Intent`/`Mechanics` content.

- **The spend (#5)** — #5 Strategy's intent is per-request pattern choice.
  The claim that a formal, expensive-to-be-wrong property routes to spend-N
  (not Mate-in-One) matches #5's shape-vs-economics framing. ✓
- **The fan (#2, #11, #12)** — #2 Fan-Out votes N answers; #11 Negative
  Selection forces divergence before judging; #12 Markowitz Ensemble weights
  by measured error-correlation so the mean reduces variance. All three
  intents confirmed verbatim. ✓
- **The certify (#8, #22)** — #8 Verifier Gate grants a deterministic external
  check; #22 requires a pre-specified N with a widening significance boundary
  across peeks. Both intents confirmed verbatim. ✓
- **The state (#17, #24)** — #17 caches the verified answer by a semantic key;
  #24 probes a model's type in idle before trust is at stake. ✓
- **The risk (#19, #20)** — #19 prices spend as CVaR over the tail; #20 trips a
  breaker on a toxic request class and escalates to a stronger arm rather than
  re-spending on the tails that failed. ✓

No factual drift found; ten cited `#N` all resolve to catalog-correct
patterns. The narrative is consistent with the actual pattern content. Logged
as verified; no edit needed.

---

## Round 42 — make the main catalog simple and local-first

**Critique.** The six-pattern landing page had become 897 lines of admission
tests, contracts, measurement plans, and physical-plan machinery. The original
27-pattern reference had useful ideas, but several statistical implementations
were presented at the same level as structural patterns. The catalog was
optimizing its classification rules instead of making each pattern memorable.

**Change.** Rebuilt the main page around a Rule of One: one short name, one
problem, one move, one primary local-first advantage, one tradeoff, and one
one-line shape. “Local-first” is now a benefit, not an exclusivity test: a
cloud-compatible pattern belongs when owned inference makes it practical to
repeat, private, offline, idle-powered, or aware of operator-controlled models
and hardware.

A seven-pattern on-ramp now leads with the clearest local levers—unmetered
breadth and repair, owned idle cycles, physical memory, local-default routing,
data locality, and offline continuity—before the complete catalog.

The main catalog contains 31 patterns, grouped by the reader's need.
Academic mechanisms were retained as refinements of simpler patterns—for
example Markowitz weighting under Ensemble, PID under Adaptive Effort, CVaR
under Risk Ladder, and Thompson sampling under Routing Memory. The previous
focused engineering catalog moved intact to `six_pattern_reference.md`; the
original 27 research entries remain in `portable_patterns.md`.
`pattern_lineage.md` accounts for every merge and rename. Local Cascade, Data
Stays Put, and Private Memory are the new local-first patterns added by this
round; Fit the Box and Night Shift preserve useful mechanisms that were hidden
inside broader cards.

**Verification.** All 31 cards are 52–84 words and each has exactly one
Problem, Move, Local-first, and Tradeoff field. Original research entries
1–27 are all present in the lineage map. Local links resolve, all 33 Python
examples still parse, and both diagram generators remain green.
