# Grid Goal merge-readiness record

This record distinguishes software evidence from physical-machine evidence. A passing simulated
worker is not described as a laptop, and an inference node is not described as an execution node.
The detailed physical artifacts are indexed in
[goals-physical-test-log.md](goals-physical-test-log.md).

## Tested code revisions

- Public worker/CLI: `0c4ac0ff01afc26cabcb9c89bba558e3c3bb64f6`
- Private relay: `7f162130fb852d2a998797d91af036c96a65d94e`
- Both `grid-goal-distributed` code revisions were clean and pushed when these gates completed.
  The public branch's following documentation-only commit adds this record and changes no runtime
  or test code.

## Software gates

| Gate | Result | What it proves |
|---|---:|---|
| Full public suite | 3,319 passed, 56 skipped, 7 deselected | CLI, providers, native harness adapters, sandbox, Git plane, and existing Grid behavior |
| Private Goal suite | 109 passed | Goal creation, claims, retries, pause/cancel races, budgets, subgoals, eval authority, evidence, inference attribution, and capability matching |
| Changed private task/Git/migration suites | 339 passed | Goal changes preserve the existing distributed task, reclaim, migration, project-file, transcript, and trunk-apply contracts |
| Cross-repository distributed matrix | 16 passed | Real relay HTTP/Git/task planes with isolated fake native Codex and Claude processes |
| Claim-ingress regression | 3 passed | Disconnect during assignment remains cancellable and mixed Goal attempts retain exact inference identity |

The final 16-scenario matrix was run in one uninterrupted invocation against both candidate
revisions. It includes:

- three execution nodes completing one game across two abrupt lease reclaims;
- Codex to Claude to Codex continuation from separate local roots;
- a four-node Codex/Claude eval-repair flow that verifies Claude receives the exact immutable eval
  path and literals, not merely a prose failure;
- native Codex checkpoint recovery on another machine root;
- Codex and Claude protocol-drift quarantine and handoff;
- action idempotency before and after a worker crash;
- image and API-origin capability matching;
- required and optional distributed subgoals with independent fan-in;
- model and quota outages that preserve attempt zero until inference is ready.

The repository-wide private suite is not used as a false green gate: current `main` contains stale
legacy tests that independently fail against current `main` contracts. Examples include constructing
`AccountRow(node_id=...)` after the model moved to `user_id`, expecting Advisor context-window text
that current routing intentionally omits, and expecting the old lowercase `auto` listing. The Goal
suite and every existing suite for files changed by Goal were therefore run explicitly. During that
audit, an actual compatibility regression in inference dispatch was found, fixed, and covered before
the gates above were repeated.

## Physical evidence completed

Goal `76b79310-8f03-4737-bcc6-df1128946846` completed on disposable Grid `goal-physical`:

- execution node B: `goal-machine-b-48759900-4980-4b1a-ae6b-926e62ddd835`, macOS x86_64;
- execution node A: `goal-b-c19853f3-8a8f-4a8d-a961-87e79b087a99`, macOS arm64;
- B completed the first turn, claimed the next turn, and was killed with `SIGKILL`;
- A reclaimed that exact turn at attempt 2 from relay-owned Git and native Codex history;
- local `Qwen3.6-35B-A3B` inference flowed through Grid and was attributed separately from agent
  execution;
- the Goal completed after 11 turns using 9,499,857 of 10,000,000 accounted tokens;
- all 6 immutable relay evals passed on commit
  `471bc335650a4c92c0a10ba4d7dbe0ce5aec4078`;
- the independent game suite passed 34 of 34 tests;
- strict evidence verification passed with two execution nodes and required Grid inference.

This proves real networking, abrupt worker loss, cross-machine reconstruction, continued native
Codex history, local Grid inference, and independent eval enforcement. It does not prove a physical
Claude handoff because the selected local model exposed a Codex-compatible Responses route but no
compatible Claude Messages route.

## Remaining release gate

[goals-three-machine-acceptance.md](goals-three-machine-acceptance.md) still requires three physical
computers and distinct local disks for Codex to Claude to Codex, with two abrupt losses. The
single-host four-node matrix proves the protocol and harness integration but cannot prove laptop
sleep, three independent filesystems, or the Claude binary on a second physical worker.

Do not label the release gate complete until that physical artifact passes, or until the project
owner explicitly waives it. Merging before then is an informed MVP decision, not the same claim as
completing the documented physical release gate.
