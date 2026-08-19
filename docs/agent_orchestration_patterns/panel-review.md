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

---

## Round 20 — Seat-pool coding-engine precision

**Critique (precision lens, carried over from the model layer's 8-hour ask).**
The front-matter seat pool listed the coding-agent engines as
"(Cursor, OpenCode, Pi, Command Code, Devin, Muse Code, Amp)". A hallucinated
product name is exactly the "precision" defect the user asked to eliminate, so
this round verified each name against a live source rather than trusting it.

**Source checks (this round).**
- **Command Code** — confirmed real and current: `commandcode.ai` /
  `CommandCodeAI/command-code`, npm `command-code@0.51.0` (2026-07-16). Its
  differentiator is a learned *Taste* preference compiler (imports
  Claude/Codex/Cursor sessions, writes portable `taste.md` packages). Keep.
- **Aider** — confirmed real: `Aider-AI/aider` (the AI pair-programming CLI).
  Added as a verified seat-pool member.
- **Devin** — confirmed real (Cognition's autonomous software engineer; the
  open-source alternative is `OpenDevin`). Keep.
- **Cursor** — confirmed real (Anysphere's agentic editor). Keep.
- **OpenCode** — confirmed real (`anomalyco/opencode`, "the open source coding
  agent"). Keep.
- **Pi** — already confirmed earlier (`earendil-works/pi`). Keep.
- **Muse Code** — *not* confirmable as a product; only unrelated personal
  repos (`lumine1120/muse-code`) surfaced. Removed.
- **Amp** — *not* confirmable as a coding agent; only unrelated repos
  surfaced. Removed.

**Changes applied.** Seat pool now reads
"(Cursor, OpenCode, Pi, Command Code, Devin, Aider)" — every name verified
against a live source; the two unverifiable names removed. This is the model
layer's "names are parameters, not a billable stack" rule applied to the
*engines* rather than the models, and it keeps the pool illustrative-but-true.

**Round state.** The seat pool is now *sourced*, not asserted from memory.
Remaining open items (unchanged in priority): the optional external
`agents.md` companion, and the per-pattern **Known Uses precision pass**
(7 patterns — make the "who already runs this" exemplars current and concrete).

---

## Round 21 — diagram-label collision fix + last cloud-model example gone

**Critique (local-AI lens + a rendered-figure bug report).** The user pasted
four screenshots of figures with genuinely unreadable text: an edge label
overdrawing its endpoint node (`average the answers` over `mean`), a
multi-line node-label overflow, a label clipped off the left canvas edge
(`d → reinforce, others decay`), and an edge label (`overdue`) landing under a
node. My earlier label-to-label pass had never enforced **label-vs-node
clearance**, **on-canvas clamping**, or **anchor-aware extents** — and the
gap-shrink logic was measuring the wrong span, so it never shrank labels that
were wider than the space between their endpoint nodes.

**Root cause (found, not guessed).** In `_label_boxes` (the shared engine that
`docs/model_orchestration_patterns/build_diagrams.py` exports to the agent
layer's generator), pass-0's "fit the label to its gap" computed
`left = max(node_right_edges)` and `right = min(node_left_edges)` — the
*outer* span of the two endpoint nodes, not the *inner* gap. For a horizontal
edge like `worker → ans` that produced `left=1092`, `right=605`, `avail=-491`,
so the `if avail > 0` guard never shrank the label and it overhung its
endpoint node by ~20px. Exactly the screenshot defect.

**Changes applied (both catalogs, since they share the engine).**
- Rewrote the boundary computation: a node left of the label center bounds the
  label on its right edge, a node right of center bounds it on its left edge —
  so `avail` is now the true gap (e.g. `194`, not `-491`).
- Changed the shrink to step the font size down (with a `MIN_EDGE_FS = 12`
  floor) using the *real* `text_w` width until the label actually fits, instead
  of the `0.6 * len` estimate that could round up and still overflow.
- Fixed `_elabel` to unpack the full 8-field box returned by the resolver.
- Verified: all **27 model** and **8 agent** figures regenerate `verok` /
`verify ok`, and the residual-overlap diagnostic reports **0 issues** across
both catalogs (was 4).

**Precision (the user's standing rule: examples must be current *and* local).**
The strategy layer's advisor was still "the small ranker LLM, `gpt-5-mini`" —
the last non-local model used as a build example. Swapped the default to the
local `qwen3-coder`. Every build example in the catalog now names a current
local SoTA model (`qwen38-27b-mtp`, `qwen38-35b-a3b-mtp`, `glm-5.2`,
`deepseek-v4-flash`, `qwen3-coder`).

**Commit/push.** Diagram fix committed and pushed as `ce675c9`; this round's
ranker precision is committed alongside this log entry. All checks green.

---

## Round 22 — figure accessibility (`<title>`) + one latent label collision

**Finding.** Every SVG `<svg>` root carried an empty (or absent) `<title>`.
The README has caption alt-text, but the figures themselves did not
self-describe — a real accessibility/GoF-alignment gap: GoF's figures are
self-contained and captioned, and a screen reader should be able to name a
diagram on its own.

**Changes applied (both catalogs, one shared engine).**
- Added a descriptive `<title id="t">` to every figure root (the builder's
  human-readable `Diagram` title) plus `role="img" aria-labelledby="t"`, so
  each SVG names itself. `index.svg` in both catalogs got a title too.
- All **28 model** + **9 agent** SVGs now carry titles.
- The shared-engine change surfaced a **latent** label collision in agent
  `lifecycle.svg`: `spawn (cold)` (≈87px at min font) could not fit the 81px
  gap between `job` and `seat`, so it overdrew the `job` node. The cold/warm
  contrast is already carried by the `warm → resume` edge and the `warm`
  node's `note`, so the label became `spawn` — every documented transition
  still authentic, and the figure now verifies clean.

**Verified.** Both generators regen green (`verok` / `verify ok` for all
patterns), residual-overlap diagnostic = **0 issues**. PNG contact sheets
re-rendered under `/tmp/model_fresh` and `/tmp/agent_fresh2`.

**Commit/push.** `05e609b`. All checks green.

---

## Round 23 — provenance verification of the meta-harness framing

**Lens: local-AI / on-device + software architecture.** The landscape claim
carries real names and licenses, so it must be *verifiable* — a catalog that
cites "the clearest working example" of the meta-harness shape should get the
attribution exactly right.

**Finding.** The README introduced Omnigent as "**Omnigent** (Databricks /
Matei Zaharia, Apache-2.0, `github.com/omnigent-ai/omnigent`)". Checking the
source: `pyproject.toml` lists `authors = [Databricks, Inc.]`, and a code
search of the repository turns up **zero** occurrences of "Zaharia". The
Matei-Zaharia association is plausible (he co-founded Databricks) but it is
not what the artifact itself states — and this catalog's standard is
"verify the source, then repeat it."

**Change.** Rewrote the parenthetical to the verifiable author, **Omnigent
(Databricks, Inc., Apache-2.0, ...)**. Also tightened one policy description:
Omnigent's own docs describe a "stricter session rules checked first" ordering,
not "the session level wins" — aligned the sentence to the source wording so
the three-level ALLOW/DENY/ASK stack is described the way Omnigent actually
specifies it.

**Not changed.** The `Polly`/`Debby` mechanics (cross-vendor reviewer, two-head
debate, three-level stack) were re-verified against `examples/polly/config.yaml`
and remain accurate; the "reviewer from a *different* harness" table row at
the catalog's reviewer pattern is also consistent with Omnigent's Polly rule.

**Commit/push.** `12428b4` (README precision) — this log entry is committed
with it. All checks green.

---

## Round 24 — harness provenance audit (all five home-bases live-verified)

**Lens: local-AI / on-device + software architecture.** The "Where these
claims come from" block asserts that every home-base is a real, current,
checkable project and invites the reader to "point the list above at those
sources." That is a load-bearing promise, so this round held each name to its
source.

**Verified against live GitHub (all pass).**
- Repo paths resolve exactly: `openclaw/openclaw`, `NousResearch/hermes-agent`,
  `anthropics/claude-code`, `openai/codex`, `earendil-works/pi`.
- The Pi property the catalog says it "leans on hardest" is verbatim in Pi's
  README: *"Pi does not include a built-in permission system… runs with the
  permissions of the user and process that launched it."* Pi's "agent runtime
  + coding-agent CLI + unified multi-provider LLM API" anatomy also matches.
- Hermes Agent's README confirms it is built by Nous Research (MIT), is
  model-agnostic (Nous Portal / OpenRouter / OpenAI / own endpoint), and runs
  the memory/skills/self-evolution loop the catalog describes.

**One real precision gap found and fixed.** Hermes Agent's README also says it
"lives where you do — Telegram, Discord, Slack, WhatsApp, Signal, CLI — all
from a single gateway process," on local/Docker/SSH or a serverless VM. The
catalog had assigned **Channel layer → OpenClaw** and **Memory layer → Hermes
Agent** without acknowledging Hermes is channel-native too, which could mislead
a reader into thinking OpenClaw is the only channel home. Added a grounded
parenthetical to the Memory-layer bullet: the layer pick is about which value
is *distinctive*, not exclusive — OpenClaw when the task lives in the channel,
Hermes when the value has to *remember*.

**Commit/push.** `07c37fd`. All checks green.

---

## Round 25 — re-verification of the four reported figure bugs

**Lens: GoF / diagram conventions + software architecture.** The user
photographed four distinct rendering faults across the figures; each was
filed as a real defect, not an aesthetic nit. This round re-confirmed, on the
committed render, that every one is gone — coordinate-level, not just by the
residual-overlap count.

- **`single` / `expand` touching (fan-out #2):** the figure was redesigned so
  the two edge labels no longer share a midpoint — the fan now carries
  `same prompt`, `ties → expand`, and `a single answer` on distinct paths, and
  `expand` is a named node, not a stacked label.
- **`average the answers` over the `mean` node (ensemble):** the edge label now
  sits above the node at y=192 vs the node at y=220 (font 14 vs 28), clear of
  it and its edges.
- **`N identical` multi-line overflow (brute-force):** now a single inline edge
  label above the workers, no wrapped box colliding with an edge label.
- **`d → reinforce, others decay` clipped off the left (pheromone):** no
  negative-x text remains in either catalog (0 hits across all SVGs); the label
  was also rewritten to the clearer `verified → reinforce, others decay`
  (anchor=x, right edge at x≈477, text runs left to ≈213 — on-canvas).
- **`overdue` under a node (straggler):** the label moved to the clear space
  above (y=141), no node beneath it.

Global regression: `grep -c '<text x="-"'` = 0; per-diagram label boxes +
on-canvas checks = **0 issues**; both generators regen `verok` / `verify ok`.

**Commit/push.** No content change this round — a verification-only pass; the
fixes were already committed in the preceding rounds. Logged for the audit
trail.

---

## Round 26 — provenance for the seat-pool coding engines

**Lens: precision / freshness + software architecture.** Round 20 verified the
seat-pool *names* (kept Cursor, OpenCode, Pi, Command Code, Devin, Aider;
dropped the unverifiable Muse Code and Amp). What remained ungrounded was the
provenance paragraph: "Where these claims come from" listed repos only for the
five home-bases (OpenClaw, Hermes Agent, Claude Code, Codex, Pi, Claude
Cowork) and was silent on the six coding engines the same front matter names.
A reader could not point any seat-pool engine at a source, which undercuts
the section's own promise that "every base is a real, current, checkable
thing." This round carried Round 20's verified facts into that paragraph.

**Source checks (re-verified live this round).**
- **OpenCode** — `sst/opencode` redirects to `anomalyco/opencode`, "the open
  source coding agent." Two "OpenCode" projects now exist; the other,
  `opencode-ai/opencode` (Charm team), is **archived** and continues as
  `charmbracelet/crush`. The working-tree engine the catalog names is the
  SST/Anomaly lineage — flagged the ambiguity so a reader does not click the
  dead one.
- **Aider** — `Aider-AI/aider`, "AI pair programming in your terminal," ~6.8M
  PyPI installs (`aider-chat`).
- **Command Code** — `CommandCodeAI/command-code` (3735★, live), npm
  `command-code@1.28.1`, "the coding agent that continuously learns your
  coding taste"; a learned *Taste* preference compiler importing
  Claude/Codex/Cursor sessions into portable `taste.md` packages, with a
  "Command Code Go" subscription tier and Pi / Hermes Agent / DeepSeek Harness
  API adapters already shipping.
- **Cursor** — managed desktop IDE (cursor.com), no public product repo.
- **Devin** — Cognition's managed agent (devin.ai), no public product repo
  (only `CognitionAI/devin-swebench-results`, results/methodology).

**Changes applied.** Expanded the provenance paragraph to ground all six
seat-pool engines — including the two-OpenCode disambiguation and the honest
"closed-harness same as Claude Cowork" framing for Cursor/Devin. Also corrected
a draft line that said Command Code "publishes no open repo to anchor yet"
(verified it does).

**Verification.** Agent + model generators regen clean; geometry `diag3.py` =
0 issues; all 7 agent patterns structurally intact.

**Commit/push.** `032de24` (initial provenance), then the Command Code
correction in the same working round.

---

## Round 27 — "which exec seat for which shape" decider

**Lens: software architecture + the user's explicit "when to use what" ask.**
Round 26 grounded the seat-pool engines in provenance but they were still only
*named* — nothing told a reader when to pick Aider over OpenCode over Command
Code over Codex over Claude Code over Pi at the lane. Pattern 3 routes by
*layer* (role → lane → gate, four lanes) and does not help choose among the
coding engines that can all occupy the exec seat. That is the exact
"understand their strengths and weaknesses and when to use what" the user
asked for, so this round added a compact decider directly after the seat-pool
sentence in the front matter.

**What it says (each grounded in a verified differentiator):**
- **Aider** (`aider-chat`) — thin, git-native pair-programming seat for an
  experienced dev driving small-to-medium diffs.
- **OpenCode** (`anomalyco/opencode`) — open terminal TUI; configurable,
  auditable, hackable across many models.
- **Command Code** (`command-code`) — the seat that *learns your taste*
  (Taste compiler, portable `taste.md`).
- **Codex vs Claude Code** — the two defaults split by reach: Codex the
  supervised sandboxed exec + worktrees/PR pipelines (fleet), Claude Code the
  ergonomic single-developer workspace (one repo done well).
- **Pi** — minimal DIY seat (no permissions/plan/sub-agents); **Cursor/Devin**
  — managed GUI/fleet ends when you hand the loop to a product, not a lane.

**Verification.** Agent + model regens clean; geometry 0 issues; all 7 agent
patterns structurally intact.

**Commit/push.** `f0e71dd`.

---

## Round 28 — model-token consistency + description-of-divergence audit

**Lens: AI/ML precision + cross-pattern consistency.** Re-reading Patterns 4–7
word-by-word surfaced a naming drift the greps had been hiding: the same
canonical resident models were written two ways in different sections —
`qwen38-27b` / `qwen38-35b` (bare, in the worked example and Pattern 4's On
the Grid stack) versus `qwen38-27b-mtp` / `qwen38-35b-a3b-mtp` (the canonical,
suffixed forms Pattern 3's lane map and the front-matter roster use). On a
catalog whose whole promise is *precise* identifiers, two spellings of one
model read as two models.

**Second defect, same lens.** The worked example's fix-B row gave **OpenCode**
the bare `qwen38-35b` — the *same model number* as the Claude Code write seat —
while its own copy claims "a fully different harness+**model** tail." The claim
was false on the "model" half: the divergence was harness-only, one model
reused. A reviewer following #6 would flag that the "diverges on purpose" arm
wasn't diverging.

**Changes applied.**
- Standardized every resident model token to its canonical suffixed form:
  `qwen38-27b` → `qwen38-27b-mtp`, `qwen38-35b` → `qwen38-35b-a3b-mtp`
  (Pattern 4 On the Grid, worked-example repro seat, certify call, Claude Code
  write seat).
- Gave fix B's **OpenCode** a genuinely distinct open-weight coder,
  `qwen3-coder`, so the draft arm now truly differs from both fix A
  (`deepseek-v4-flash` on Hermes) and the write seat (`qwen38-35b-a3b-mtp` on
  Claude Code) — harness and model both diverge, which is what #6's weak arm
  actually requires.

**Verification.** `grep` confirms zero remaining bare `qwen38-27b`/`qwen38-35b`
forms; the five spins of `qwen38-27b-mtp`, four of `qwen38-35b-a3b-mtp`, and
six of `qwen3-coder` are the only spellings. Agent + model diagram regens clean
(up to `index.svg`); geometry verifier 0 issues; structure intact.

**Commit/push.** `a9f68ce`.

## Round 31 — GoF lens: the framework layers lacked a scannable index

**Critique.** The five "home base" layers (Channel/Memory/Desktop/Coding/
Command-center) and their "grab it when" were carried entirely in prose
bullets under *Which harness for which job*. GoF leads with a pattern-summary
table before the deep dive; a practitioner reaching for "which home-base when"
had to read ~140 lines of prose to get a one-line answer. The user's own ask —
"understand their strengths and weaknesses and when to use what" — is served
best by an at-a-glance index first, then the nuance.

**Changes applied.**
- Added a compact **Five home bases, at a glance** table (Layer · Home base ·
Distinctive strength · Grab it when) directly under the *Which harness for
which job* intro, summarizing the five layers with a one-line "grab it when"
each. The prose bullets below retain the full *why* and *when-not*, so the
table is a summary index, not a duplicate.

**Verification.** Table uniform (5 pipes/row × 5 rows); markdown globally
balanced (backtick/bold); both diagram regens green; geometry 0 issues;
7 pattern sections + full skeleton intact.

**Commit/push.** (R31 — this commit).

## Round 32 — GoF lens: exec-seat coding engines had no summary index

**Critique.** R31 added a summary-first table for the five *home bases* (the
layer→harness map), but the six *coding-engine exec seats*
(Aider/OpenCode/Command Code/Codex/Claude Code/Pi/Cursor/Devin) were still
carried only in prose bullets — the other half of the user's exact "when to
use what" ask. A reader deciding among coding engines had no one-line index.

**Changes applied.**
- Added an **Exec seats, at a glance** table (Exec seat · Default posture ·
Reach for it when) right under the *Which exec seat for which shape* intro,
mirroring the R31 home-bases table and the GoF summary-then-prose shape. The
prose bullets remain the *why* and *when-not*.

**Verification.** Both new tables uniform (home-bases 5 pipes/row; exec-seat 4
pipes/row); markdown globally balanced; both diagram regens green; geometry 0
issues; structure intact.

**Commit/push.** (R32 — this commit).

## Round 33 — GoF lens: catalogs lacked per-pattern Applicability

**Critique.** GoF's canonical skeleton places **Applicability** — a crisp
"use it when / avoid it when" — right after *Motivation*, and both layers
were missing it as a per-pattern section. The one-sentence tables held the
"use it when" content in the front matter only; a reader who landed mid-pattern
had no positive/negative fork. GoF pairs Applicability with the negative case
from each pattern's own *Failure mode*, which is the signature move this
catalog was still lacking.

**Changes applied.**
- Added a **Applicability** section to all 7 agent patterns, immediately after
  *Motivation* and before *Structure* (per pattern: act-gate single-writer;
  session residency; harness routing; canary/shadow; credential/tool-sink;
  label-routing equity; only-one-ledger).
- Each Applicability carries the positive ("use it when") and the negative
  ("avoid it when") sourced from that pattern's own *Failure mode* — e.g. a
  gate asserted in a prompt is a hope, not a gate.
- Added **Applicability** to the "How to read a pattern" skeleton announcement
  so the announced skeleton matches reality.

**Verification.** 7 pattern sections intact; every pattern now has Intent ·
AKA · Motivation · Applicability · Structure · Mechanics · Consequences ·
Known Uses · Failure mode · Refinements · Sample Code · On the Grid stack ·
Related Patterns in correct order (script-checked, all OK); markdown globally
balanced (backtick/bold); blank-line spacing normalized before each Structure.

**Open items.** Extend the same Applicability pass to the model layer (27
patterns) — the positive halves are in its one-sentence table, the negatives in
its Failure modes.

**Commit/push.** (R33 — this commit).

## Round 35 — precision pass: cross-ref network, geometry clearance, consistency

**Critique.** The agent layer's prose leans on numbered cross-references to the
model layer. "Clearer and more precise" (the user's standing ask) means those
refs must be *factually* correct — the cited pattern number must actually hold
the concept named. Also open from R34: confirming the model+agent edge-label
clearance audit really is clean, and a word-by-word read of patterns #2-#7
(the priority layer) for residual inconsistencies.

**Changes applied.**
- Cross-ref integrity audit: verified every model-layer number cited in the
  agent layer holds the concept it is named for — #16 Straggler Backup (the
  "straggler re-cut"), #24 Type-Revelation (probing unknown models), #26
  Slack-Stealing Scheduler, #18 Canary Trust-Equity (admit-a-model), #2/#11
  (family divergence), #12 Markowitz (fallback-vs-resident). All correct —
  these are intentional inter-layer refs in narrative prose, not self-refs.
- Agent `Related Patterns` footers confirmed all in range (1..7). No change
  needed; audit records the resolution.
- Geometry clearance: re-ran the extended audit (label-to-label, label-vs-node,
  node-sub overflow, off-canvas) over both layers' current SVGs — `TOTAL
  ISSUES 0`. Closes the R34 open item.
- Word-by-word pass over all seven agent patterns (#2-#7 read in full). Fixed
  the one genuine inconsistency: #2's hyphenation `politely-named` →
  `politely named` (adverbs ending in `-ly` are not hyphenated in compounds).
  No other drift found — prose already consistent and precise.

**Verification.** Structure intact (7 pattern sections, correct skeleton
order); markdown balanced; both layers' build green; geometry 0 issues.

**Open items.** The `agents.md` companion doc remains honestly described as
unpublished in the front-matter provenance — still the largest outstanding
piece; a full write is the next substantive candidate.

**Commit/push.** (R35 — this commit).

## Round 36 — style pass: "-ly adverb" hyphenation (cross-layer)

Same word-by-word normalization as the model layer: adverb+modifier
compounds formed from an `-ly` adverb are unhyphenated. Agent layer fixed one
site — `externally observable action`; `nightly-batch deadline` kept as a
compound noun. See the model-layer log for the shared rationale and the 14
model-layer sites. Verified markdown balanced and structure intact.

**Commit/push.** (R36 — this commit).

## Round 37 — author the `agents.md` agent-layer execution companion

Resolved the largest outstanding open item. Authored
[`agents.md`](agents.md) in this directory — the agent-layer execution
companion mirroring the model layer's `router-execution.md` shape (honest
baseline + the concrete execution model the seven patterns run on), with:

- The **execution primitive**: one loop = route lane → residency first →
  act-gate → act → ledger, and the "one seat, one actor" constraints.
- **Session lifecycle as the join** (spawn/warm/handoff/kill) — the agent
  layer's re-join on top of the model layer's spawn/pool join.
- **The divergence unit**: harness × model tail (weak reflex-only vs strong
  family divergence), with the same-model caveat against over-claiming #2/#11
  family independence.
- A **harness × model seat table** keyed to current local SoTA
  (`deepseek-v4-flash`, `glm-5.2`, `qwen3-coder`, `qwen38-27b-mtp`,
  `qwen38-35b-a3b-mtp`) across Hermes ACP, OpenClaw, Claude Code, Codex, Pi,
  and OpenCode.
- **Staged admission** as earned (shadow → bounded → full), **background in
  idle slack** (#4), and **exact-once durable ledger** (#7) with no second box.
- A **worked ledger walk** end-to-end (repro → two divergent fixes →
  cross-lane review → preempt → certify-by-fact → shipped), using current
  models on a concrete 48 GB box.
- Wired the new companion into the README's **Read with** so no reference
  dangles; removed the "unpublished" framing.

**Verification.** New doc: markdown balanced, all agent-layer `#N` refs in
range (the lone `#11` is explicitly flagged as a model-layer family-divergence
ref), all current model names present. README structure intact after the
Read-with edit.

**Commit/push.** (R37 — this commit).

## Round 38

**Change.** Upgraded agent pattern #5's `Sample Code` from a toy two-line
admittance sketch into a working three-stage sketch (shadow → bounded → full)
that is honest to the layer's own constraints and cross-referenced:

- Each stage transition is a **ledger event** (`#7`), and the deterministic
  arm **scores ground-truth-first** (`#6`: fact arm, then weak diagnostics) —
  so the sketch exercises the two most load-bearing agent-layer guarantees.
- **Idle-row constraint** surfaced (`#4`): a failing shadow harness keeps
  scoring in the router's idle slack rather than burning an act seat.
- **Round-key grant** as the bounded→full reward (`#1`): the act step is gated
  behind a router quorum plus a fresh `round_id` key, keeping one-seat-one-actor.
- Renamed like nits to the rest of the layer (`BOUNDED_QUORUM`, `STAGES`), and
  every name stays a parameter per "A word on the examples".

**Verification.** Markdown balanced (backticks/bold), README structure intact,
all `#N` refs in range. Pure additive edit — no fact removed.

**Commit/push.** (R38 — this commit).


## Round 39

**Change.** Known Uses precision pass on the agent layer (patterns #1–#7).
The seven `Known Uses` blocks anchored the shapes only to classic systems
(advisory locks, connection pools, RBAC, K8s eviction, canary, CI, WAL). Left
those anchors intact and appended one precise *modern local-stack instance*
per pattern, so a reader sees the shape already running in the tools this
catalog names — Codex per-repo worktree single-flight and PR merge gating
(#1), the Ollama/llama.cpp keep-alive and Codex warm pool and Hermes single
gateway process (#2), OpenClaw plugin scoping to the channel + the
harness×model seat table (#3), Ollama/vLLM continuous batching preempting for
the deadline-bearing request (#4), the read-only shadow in idle slack + the
`round_id`-keyed act step at the top rung (#5), the suite/conformance gate in
the router's own pipeline (#6), and the single append-only JSONL/SQLite-WAL
ledger fsync'd on every act with periodic off-box export (#7). Each instance
is consistent with — and never contradicts — the framework claims already made
in the "Which harness for which job" section and `agents.md`.

**Verification.** Markdown balanced (backticks/bold); structure intact; all
frame names stay parameter placeholders. Purely additive prose — no fact
removed or contradicted.

**Commit/push.** (R39 — this commit).

## Round 40

**Change.** Added the agent layer's organizing thesis — **"Three binds, seven
shapes"** — placed directly before the one-sentence table, mirroring the model
layer's "Two levers, twenty-seven shapes" intro. It gives the seven agent
patterns the same single-shot framing the model catalog has: the model layer
decides *how many samples and how they pool*; the agent layer decides what a
sample is *allowed to do* — defending the same three scarce things once each:

- **The seat** (VRAM residency): #4 seat-as-executor, #2 session lifecycle.
- **The act** (world-touching write): #1 act-gate, #3 route across lanes.
- **The fact** (what may certify): #5 staged admission, #6 verifier, #7 ledger.

Closes with the natural read order (seat → act → fact) without contradicting
the runtime decision-order key (which correctly still leads with #1's act
question). Cross-checks every `#N` tag, all agent-layer refs in range.

**Verification.** Markdown balanced (backticks/bold); structure intact (7
patterns + skeleton); no claim contradicts the existing decision-order key or
anything in `agents.md`. Repairs one factual slip during drafting (the thesis
initially claimed entries run in family order — they run numerically #1–#7),
caught before commit.

**Commit/push.** (R40 — this commit).

## Round 41

**Change.** Model layer closed the GoF-signature gap the agent layer already
had: added **"One request, walked through the catalog"** — a worked case study
that drives one correctness-gating request (a lock-ordering deadlock-free
verdict) through the actual shapes it invokes (spend #5 → fan #2/#11/#12 with
forced divergence + correlation weighting → certify #8/#22 with a
pre-registered N → state #17/#24 → risk #19/#20 CVaR + breaker), and closes on
why local flips the economics. Mirrors the agent layer's "one box, one defect".
Verified all `#N` refs are in the model layer's 1–27 and every pattern header
survived. Purely additive.

## Round 42

**Change.** 4-lens judge pass over R38–R41 content, run directly (the sub-agent
runtime still fails to persist judges — logged as-is). Findings:

- **GoF lens** — both worked case studies are strong; the one consistent
  scannability gap is that the agent catalog's "catalog, as one figure"
  (`index.svg`) caption carried no organizing framing, while the model layer's
  identical figure names its taxonomy ("arrayed primitive → composition →
  stateful → epistemic → machinery"). **Applied:** reworded the agent caption
  to "seven patterns arrayed by the three binds — seat, act, fact — and the
  end-to-end system," tying the figure to the R40 thesis instead of an orphan.
- **Architecture lens** — re-verified the agent #5 `admit()` Sample Code three
  stages are internally consistent (shadow → bounded → full, each a ledger
  event) and match the On-the-Grid prose; no drift found.
- **AI-ML / local-AI lenses** — confirmed all model names in both catalogs'
  pattern examples are current SoTA (`qwen38-27b-mtp`, `qwen38-35b-a3b-mtp`,
  `qwen3-coder`, `glm-5.2`, `deepseek-v4-flash`) and the model-layer #6
  attribution agent #1 relies on ("only one worker may act") is a real
  refinement of model Brute-Force, not a fabrication.

**Verification.** Agent `index.svg` confirmed to contain all seven pattern
titles; Markdown rebalanced; single targeted edit, no structural change.

**Commit/push.** (R42 — this commit).

## Round 43 — agent-layer precision pass: sample-code defects + case-study audit

**What was reviewed this round.** A word-by-word precision pass over the whole
agent catalog (front matter + patterns 1–7), plus a cross-reference and
cross-file consistency audit.

**Found and fixed (two real defects in the sample code).**
- **#2 Session lifecycle, `handoff` contradicted its own prose.** The code did
  `_persist()` then `_load(prompt)`, but `_load` built a cold `Session(prompt)`,
  discarding the snapshot it had just frozen — so `handoff` was
  indistinguishable from `spawn`, contradicting the documented "restore a
  `round_id`-frozen session snapshot instead of starting cold." Fixed `_load`
  to restore the snapshot when one exists (else cold-start on a fresh seat).
- **#5 Staged admission, dead constants.** `STAGES` and `BOUNDED_QUORUM = 2`
  were defined but never used (the quorum gates the *bounded act step*, not
  promotion, so it has no role in `admit`). Removed both; folded the quorum's
  role into the leading comment.

**Audited and verified clean (no change needed).**
- Every model-layer `#N` attribution in the agent catalog is accurate: #6
  Brute-Force caps fan writes, #16 Straggler Backup re-cut for sessions, #26
  Slack-Stealing scheduler, #24 probe-new-models-in-idle, #18 model admission,
  #12 swap-cost pricing. Confirmed against the model README.
- R41 model-layer case study "One request, walked through the catalog": all
  nine `#N` claims (#5/#8/#11/#12/#17/#19/#20/#22/#24, plus #2 Fan-Out) map to
  the correct model patterns.
- The two catalogs' organizing theses are symmetric ("Two levers, twenty-seven
  shapes" / "Three binds, seven shapes"), and the R42 caption framing is
  accurate.
- Models in every agent example are current local SoTA
  (`qwen38-27b-mtp`, `qwen38-35b-a3b-mtp`, `qwen3-coder`, `glm-5.2`,
  `deepseek-v4-flash`).
- Markdown balanced; 7/7 agent section skeletons intact; geometry
  `TOTAL ISSUES 0`.

## Round 44 — decision-order cross-file reconciliation

**Found.** The README's "Choosing a pattern — the decision order" omitted **#2
(session lifecycle)** entirely — it listed #1 → #3 → #4 → #5/#6 → #7 — while
`agents.md`'s concrete procedure includes `#2 warm` as its own step 3. The
catalog's two decision procedures disagreed, and one of the seven patterns had
no decided position in the "which question first" order.

**Fixed.** Inserted a lifecycle question — "Is the session already warm?" → #2 —
between the route (#3) and background (#4) steps, renumbering 3–5 → 4–6, so the
decision order now enumerates all seven patterns in the same sequence
`agents.md` uses (1 act-gate → 3 route → 2 warm → 4 background → 5/6 trust →
7 ledger). Also re-swept the other sample-code blocks (#4 scheduler, #7 append)
for the #2-class defect (code contradicting its own prose) — both are clean.

**Verified.** backticks/bold balanced, 7/7 agent skeletons intact, decision
order steps 1–6 present, no dangling "A word on the examples" reference
(defined at README line 231), geometry still `TOTAL ISSUES 0`.

## Round 45 — verifier-≠-generator check closed; #5 sample-code bar made cumulative

**Lens: code-review / architecture (run directly).**
- Confirmed the canonical generator's inline `verify()` (imported into the agent
  catalog from the model `Diagram`) enforces all four overlap classes the user
  screenshotted: node-label width overflow, label clipped by viewBox,
  label-vs-node, and label-vs-label. Both builders re-run clean (27 model
  `verok`, 8 agent `verify ok`); standalone `/tmp/diag4.py` reports
  `TOTAL ISSUES 0`. The inviolate guarantee is real — no generator gap.
- **Fixed a code/prose contradiction in #5 Staged admission's `admit` sketch:**
  prose sets a cumulative bar ("≥ 20 labeled wins"), but the code scored a
  single shadow run (`score.wins`). Now `wins = ledger.cumulative_wins(harness)
  + run.wins`, consistent with the replay-from-the-log pattern the sketch
  already uses (`ledger.stage`) and with #7's append-only ledger. Bounded→full
  now also consumes the accumulated count. Zero diagram change.
- Markdown re-verified balanced (fences 14, backticks/bold even, 7 headers).

## Round 46 — deep per-claim audit of agent→model cross-references

**Lens: traceability / precision (run directly).**
- Extracted every `#N` in the agent doc with N>7 (necessarily a model-layer
  ref) plus every "model-layer (#N)" attribution. Audited each against the
  owning model pattern's content.
- **Fixed one genuine error:** agent #4's residency budget cited `#1's
  swap_cost` — Mate-in-One (#1) has no swap-cost concept. The true owner is
  **#12 Markowitz** (its single-box analysis: "on one GPU the pattern degrades
  toward #7 plus swap cost, which the latency number has to include"). Now
  `#12's swap_cost`, consistent with the agent #6 fallback arm which already
  cites #12 for the same serial-VRAM-swap cost.
- Re-verified accurate: #6 Brute-Force caps its fan (cap N) ✓; #16 Straggler
  ✓; #18 Canary "admits a model" ✓; #24 probes unknown models in idle ✓; #26
  slack-stealing ✓; #2/#11 family independence ✓.
- Markdown balanced; 7 agent headers intact. No diagram change.
