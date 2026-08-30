# Grid Goal merge-readiness record

This record distinguishes software evidence from physical-machine evidence. A passing simulated
worker is not described as a laptop, and an inference node is not described as an execution node.
The detailed physical artifacts are indexed in
[goals-physical-test-log.md](goals-physical-test-log.md).

## Tested code revisions

- Public worker/CLI: `ddf35d174c67480462d24b853fcc46aadd0cc22e`
- Private relay: `2bd0479519a02b49501118edb343d0dd8139bc5a`
- Both `grid-goal-distributed` code revisions were clean and pushed when these gates completed.
  The public branch's following documentation-only commit adds this record and changes no runtime
  or test code.

## Software gates

| Gate | Result | What it proves |
|---|---:|---|
| Full public suite | 3,336 passed, 56 skipped, 7 deselected | CLI, providers, native harness adapters, sandbox, Git plane, and existing Grid behavior |
| Private Goal + Goal migration suite | 122 passed | Goal creation, claims, retries, pause/cancel races, budgets, subgoals, eval authority, evidence, inference attribution, capability matching, and schema upgrades |
| Broad private task/Git/migration sweep | 684 passed; 4 baseline failures | Ordinary task, reclaim, project-file, transcript, trunk-apply, and migration compatibility; the four failures reproduce unchanged on the pre-final-fixes revision |
| Cross-repository distributed matrix | 17 passed | Real relay HTTP/Git/task planes with isolated fake native Codex and Claude processes |
| Claim-ingress regression | 3 passed | Disconnect during assignment remains cancellable and mixed Goal attempts retain exact inference identity |

The final 17-scenario matrix was run in one uninterrupted invocation against both candidate
revisions. It includes:

- three execution nodes completing one game across two abrupt lease reclaims;
- Codex to Claude to Codex continuation from separate local roots;
- a four-node Codex/Claude eval-repair flow that verifies Claude receives the exact immutable eval
  path and literals, not merely a prose failure;
- native Codex checkpoint recovery on another machine root;
- Codex and Claude protocol-drift quarantine and handoff;
- action idempotency before and after a worker crash;
- a Codex parent failing after a durable child spawn, followed by a replacement Codex session that
  restates optional policy but receives the same single child identity and fan-in;
- image and API-origin capability matching;
- required and optional distributed subgoals with independent fan-in;
- model and quota outages that preserve attempt zero until inference is ready.

The repository-wide private suite is not used as a false green gate. The broader 20-file compatibility
sweep passed 684 tests and failed four: three domain-claim fixtures received `204` instead of `200`,
and one Python JSON-depth fixture expected a 5,000-level body to parse but the installed decoder
returned `400`. All four fail identically on private revision `7f16213`, before the final child
validation/schema/yield fixes; none touches the Goal changes between that revision and the candidate.
Other stale legacy tests on current `main` also independently fail against current contracts, such as
constructing `AccountRow(node_id=...)` after the model moved to `user_id`. The dedicated Goal suite,
the complete public suite, and the real cross-repository seam were therefore retained as the release
gates. During the audit, actual compatibility regressions were fixed and covered before these gates
were repeated.

## Merge and rollout order

Merge and deploy the private relay first, then merge the public worker/CLI. Existing workers omit
Goal harness profiles and therefore cannot claim Codex Goal rows from the upgraded relay. The public
CLI also detects an older relay that lacks Goal routes and explains the version mismatch instead of
silently treating an ordinary task as a Goal. This order keeps ordinary tasks compatible throughout
the rollout and avoids publishing a Goal command whose control plane has not been deployed yet.

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

A second live hierarchy, parent `ca7eacfe-2bcc-41bb-95ee-e6babdb21335` with child
`fa3f9630-f0cc-4faa-ae05-cbb220057077`, proved native Codex fan-out, a native Claude child, one Git
fan-in, a resumed Codex parent, exact hierarchy token settlement, and four accepted independent evals
(one child plus three parent) through local `Qwen3.6-35B-A3B` inference. Its two signed executor
identities ran on one physical Mac, so it proves mixed-harness protocol integration but does not
replace the remaining multi-physical-machine gate.

## Remaining release gate

[goals-three-machine-acceptance.md](goals-three-machine-acceptance.md) still requires three physical
computers and distinct local disks for Codex to Claude to Codex, with two abrupt losses. The
single-host four-node matrix proves the protocol and harness integration but cannot prove laptop
sleep, three independent filesystems, or the Claude binary on a second physical worker.

Do not label the release gate complete until that physical artifact passes, or until the project
owner explicitly waives it. Merging before then is an informed MVP decision, not the same claim as
completing the documented physical release gate.
