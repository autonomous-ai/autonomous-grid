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
  The bare `[agents.md](agents.md)` link (which 404'd locally) became plain
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
