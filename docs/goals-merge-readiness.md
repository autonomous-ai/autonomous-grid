# Grid Goal merge-readiness record

This record distinguishes software evidence from physical-machine evidence. A passing simulated
worker is not described as a laptop, and an inference node is not described as an execution node.
The detailed physical artifacts are indexed in
[goals-physical-test-log.md](goals-physical-test-log.md).

## Tested code revisions

- Public worker/CLI: `7d06ebfca1d8bfbef7e43a82cd235db2e30ed6e0`
- Private relay: `c256e564cda6c0936641ec59782359ee4b678632`
- Both `grid-goal-distributed` code revisions were clean and pushed when these gates completed.
  The public working tree contained only the documentation changes recorded by the following
  documentation-only commit; it changes no runtime or test code.

## Software gates

| Gate | Result | What it proves |
|---|---:|---|
| Full public suite | 3,344 passed, 57 skipped, 7 deselected | CLI, providers, native harness adapters, sandbox, Git plane, physical-lab bootstrap, and existing Grid behavior |
| Private runbook release bundle | 146 passed | Goal creation, claims, retries, pause/cancel races, budgets, subgoals, eval authority and proof compaction, retention, dead-branch pruning, inference attribution, capability matching, and recovery from a relay death during continuation preparation |
| Private Goal migration suite | 14 passed | Older SQLite/PostgreSQL relay schemas upgrade to the complete Goal schema, including 64-bit counters |
| Settlement/Git compatibility sweep | 349 passed | Ordinary tasks, Git transport, transcript retention, WIP advancement, trunk apply, project initialization, and undo remain compatible with strict result-ref settlement |
| Task event boundary sweep | 59 passed | Terminal sequence, resumable streams, Unicode/size limits, and runtime-independent deeply nested JSON refusal |
| Broad private task/Git/migration sweep | 684 passed; 4 baseline failures | Ordinary task, reclaim, project-file, transcript, trunk-apply, and migration compatibility; the four failures reproduce unchanged on the pre-final-fixes revision |
| Cross-repository distributed matrix | 18 passed | Real relay HTTP/Git/task planes with isolated fake native Codex and Claude processes |

The full public suite, private runbook bundle, migration suite, and 18-scenario matrix were each
run uninterrupted against the exact revisions above. The evaluator audit also proves that:

- completion checks read the relay-resolved immutable result commit rather than a provider-supplied
  ref, and only lease-fenced run ids can be accepted with the terminal transition;
- file and JSON checks reject symlinks, Git links, protected paths, ambiguous/non-finite/deep JSON,
  malformed Unicode, oversized inputs, and damaged cached evidence;
- when valid verbose evidence exceeds its storage ceiling, Grid retains every immutable definition
  identity and check verdict while omitting previews; it never turns an accepted label or failed
  repair metric into a generic overflow marker. Lone-surrogate infrastructure text is escaped as
  audit JSON instead of causing a second evaluator failure;
- all checks in a nomination share a 45-second evaluator deadline, and result-ref resolution,
  transcript resolution, evaluation and WIP advancement share one 50-second aggregate deadline
  inside the worker's 60-second result timeout. A Git error cannot be read as an absent ref, and
  one infrastructure failure prevents additional evaluator subprocesses from multiplying it;
- after the native child exits and renewal stops, an exact-claim-fenced 70-second settlement lease
  protects relay-owned Git/eval work from both lease reclaim and the run-deadline sweep. It never
  shortens the normal 120-second lease, and the final terminal write repeats the claim fence;
- terminal acknowledgment is sent before idempotent continuation/fan-in preparation; periodic
  promotion and Goal reconciliation are the durable crash backstop for that post-response work.
  A globally bounded stale-prepare sweep also recovers the narrower crash window after the next
  turn is inserted but before its Git input reaches `queued`: it atomically fails the abandoned
  row, records and publishes one terminal event, and lets reconciliation create one replacement;
- model registration, removal, role recovery, quota serving transitions and engine-health
  heartbeats invalidate Goal matching immediately, so recovery wakes the untouched attempt-zero
  row without waiting for a polling-cache expiry;
- remote Goal budgets and native counters are exact-JSON integers bounded so the maximum permitted
  eight-way, depth-three hierarchy cannot overflow signed database arithmetic. Goal budget, usage,
  time and child-accounting columns are `BIGINT` on PostgreSQL, and the startup migration widens
  pre-release `INTEGER` columns idempotently. Ten-million-token local-model Goals remain well inside
  that bound.

The final 18-scenario matrix was run in one uninterrupted invocation against both candidate
revisions. It includes:

- a real relay timer recovering the exact stale `preparing` row left by a continuation-preparation
  crash, publishing one terminal event and creating one attempt-zero replacement without another
  user mutation request;
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

The historical repository-wide private sweep is not used as a false green gate. The broader 20-file
compatibility sweep passed 684 tests and failed four: three domain-claim fixtures received `204`
instead of `200`, and one Python JSON-depth fixture expected a 5,000-level body to parse but the
installed decoder returned `400`. All four reproduced on private revision `7f16213`, before the
final child validation/schema/yield fixes. The candidate removes the runtime-dependent JSON
assumption: the event encoder now explicitly refuses excessive nesting, and its complete 59-test
suite passes. The unrelated three domain fixtures remain historical baseline failures rather than a
Goal release gate. The 684-test sweep ran at private revision `2bd0479`; later Goal-specific
hardening is validated by the exact 142-test private release bundle and complete 18-scenario
cross-repository matrix above.
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

The no-SSH lab now accepts `--joining-workers 2`, persists separate B/C credentials across relay
restarts, and refuses missing or duplicate physical node ids. Its 369-test Goal preflight and 22
lab-specific tests passed. At the time of this record, only the relay-host Mac task identities were
online; no second and third physical worker were available, so the hardware event itself remains
unexecuted.

Do not label the release gate complete until that physical artifact passes, or until the project
owner explicitly waives it. Merging before then is an informed MVP decision, not the same claim as
completing the documented physical release gate.
