# Grid Goal merge-readiness record

This record distinguishes software evidence from physical-machine evidence. A passing simulated
worker is not described as a laptop, and an inference node is not described as an execution node.
The detailed physical artifacts are indexed in
[goals-physical-test-log.md](goals-physical-test-log.md).

## Tested code revisions

- Public worker/CLI and acceptance harness: `a33302ce859595a7c4b1e825c6e8c75d4cabf534`
- Private relay: `374a3125f9296e453d8a340975eea14a78ad1bd2`
- Both tested runtime revisions were clean and pushed when these gates completed.

## Software gates

| Gate | Result | What it proves |
|---|---:|---|
| Full public suite | Candidate `a33302c` passed exact-head GitHub CI on Python 3.11, 3.12, 3.13, lint and Windows | CLI, providers, native harness adapters, sandbox, Git plane, physical-lab bootstrap, and existing Grid behavior |
| Private runbook release bundle | 184 passed (316.39s locally); exact-head GitHub relay CI passed | Goal creation, claims, retries, pause/cancel and duplicate-settlement races, budgets, subgoals and inherited tool authority, nested required-child propagation, sibling cancellation and explicit resume refusal, recoverable nested fan-in conflicts, concurrent child settlement on independent database connections, eval authority and proof compaction, retention, dead-branch pruning, inference attribution, capability matching, Git ref idempotency, and recovery from a relay death during continuation preparation |
| Relay Goal feature discovery | 1 passed | `/server/info` advertises additive `goals/v1` support for safe canary and fleet rollout |
| Private Goal migration suite | 14 passed | Older SQLite/PostgreSQL relay schemas upgrade to the complete Goal schema, including 64-bit counters |
| Settlement/Git compatibility sweep | 349 passed | Ordinary tasks, Git transport, transcript retention, WIP advancement, trunk apply, project initialization, and undo remain compatible with strict result-ref settlement |
| Task event boundary sweep | 59 passed | Terminal sequence, resumable streams, Unicode/size limits, and runtime-independent deeply nested JSON refusal |
| Broad private task/Git/migration sweep | 684 passed; 4 baseline failures | Ordinary task, reclaim, project-file, transcript, trunk-apply, and migration compatibility; the four failures reproduce unchanged on the pre-final-fixes revision |
| Cross-repository distributed matrix | 22 passed (337.17s) at the exact revisions above | Real relay HTTP/Git/task planes with isolated fake native Codex and Claude processes |
| Worker runtime provenance | 41 focused public checks plus the full suites above | Every required physical attempt can be gated to a clean Grid Git revision and exact native Codex/Claude version; forged event metadata is removed and replaced from the authenticated node registry |
| Pre-start claim recovery | 212 relay Goal/reclaim checks plus a real four-node cross-repository scenario | Three machines can claim and disappear before native start without spending attempt 1; the fourth worker completes attempt 1, stale claim ids remain fenced, and only actual Codex/Claude executions enter retry/training evidence |

The public suite ran in GitHub Actions on Python 3.11, 3.12 and 3.13; the private runbook bundle and
22-scenario matrix ran uninterrupted against the private and public-harness revisions above. The
evaluator audit also proves that:

- completion checks read the relay-resolved immutable result commit rather than a provider-supplied
  ref, and only lease-fenced run ids can be accepted with the terminal transition;
- file and JSON checks reject symlinks, Git links, protected paths, ambiguous/non-finite/deep JSON,
  malformed Unicode, oversized inputs, and damaged cached evidence;
- when valid verbose evidence exceeds its storage ceiling, Grid retains every immutable definition
  identity and check verdict while omitting previews; it never turns an accepted label or failed
  repair metric into a generic overflow marker. Malformed lone-surrogate infrastructure text is
  replaced before either direct error-column or JSON storage, so it cannot cause a second evaluator
  failure or poison a later UTF-8 API response;
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
- simultaneous terminal retries for one claim are coalesced before Git and evaluator work. The
  winner writes one accepted eval row and terminal event; the delayed duplicate receives the
  ordinary already-terminal `404`. A losing Git compare-and-swap is idempotent only when a strict
  re-read proves the exact requested commit landed, never merely an ancestor or divergent ref. If
  the first HTTP request is cancelled mid-eval, its lock is forgotten and an already-waiting
  duplicate settles the same claim without spending a retry;
- model registration, removal, role recovery, quota serving transitions and engine-health
  heartbeats invalidate Goal matching immediately, so recovery wakes the untouched attempt-zero
  row without waiting for a polling-cache expiry;
- remote Goal budgets and native counters are exact-JSON integers bounded so the maximum permitted
  eight-way, depth-three hierarchy cannot overflow signed database arithmetic. Goal budget, usage,
  time and child-accounting columns are `BIGINT` on PostgreSQL, and the startup migration widens
  pre-release `INTEGER` columns idempotently. Ten-million-token local-model Goals remain well inside
  that bound;
- a subgoal inherits the parent's exact canonical observe/act manifest. A child request cannot
  smuggle a new API origin or mutation tool into its stored policy or claim payload, while the
  parent-only `subgoals` scheduling capability is not needlessly required by the child.
- Claude subscription pressure is scoped to the Claude harness. A mixed provider continues to
  advertise Codex-only claim capacity, reapplies that exclusion after credential refresh, and omits
  the full-provider pause heartbeat while Codex remains available.

The final 22-scenario matrix was rerun in one uninterrupted invocation against both candidate
runtime revisions after the mixed-harness capacity change. It includes:

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
- an authenticated support-system verify eval: a node-local supervisor reads the authoritative
  post-action API, the relay rejects an intentionally incomplete first nomination, requires a fresh
  final-turn verification, and the offline verifier recomputes the result from exported events;
- a Codex parent failing after a durable child spawn, followed by a replacement Codex session that
  restates optional policy but receives the same single child identity and fan-in;
- image and API-origin capability matching;
- required and optional distributed subgoals with independent fan-in;
- a four-node hierarchy in which Codex A spawns a child, Codex B checkpoints and fails that child's
  first attempt, Claude C reclaims the exact child turn at attempt two and passes its independent
  eval, and Codex D resumes the parent only after relay-owned fan-in;
- parallel sibling fan-out in which Codex A reserves two distinct children in one native turn,
  Codex B and Claude C are simultaneously `running` on separate roots and different models, both
  make real Responses/Messages requests through distinct Grid inference providers, retain exact
  model/provider/executor/attempt/token attribution, pass immutable child evals, and Codex D
  resumes only after deterministic two-branch fan-in;
- required-child failure propagation in which Codex B returns a native semantic failure while
  Claude C is still running a required sibling; Grid blocks the parent, lease-fences and stops
  Claude, releases every child reservation, and retains one cancellation plus terminal marker in
  Goal evidence instead of orphaning work that can never fan in;
- nested dependency failure propagation is synchronous across every ancestor: live branches that
  can no longer fan in are cancelled, while blocked child identities remain inspectable. A nested
  fan-in conflict is distinguished as recoverable, leaves unrelated ancestor siblings running, and
  requires bottom-up child resume before ancestor resume;
- model and quota outages that preserve attempt zero until inference is ready.

The matrix harness also treats an atomically replaced workspace as a transient polling miss and
cancels every Goal created by a failed scenario during teardown. One assertion failure therefore
cannot leak queued work into the next scenario and create a misleading cascade of cross-test claims.
Its provider disks live under an atomically reserved one-character `/private/tmp` root, with a hard
31-character assertion on every task root. The uninterrupted 22-scenario rerun passed in 337.17
seconds without exercising the macOS path depth that can make sandbox commands fail with `E2BIG`.
Before that final run, both protocol-drift handoff scenarios passed together three times against
fresh relay processes (6/6), and four focused client tests passed for runtime quarantine recovery,
stale-claim decline, backward-compatible capability revalidation, and token refresh during decline.
An additional no-agent startup gate passed 8/8, including installing a configured harness after
provider startup and rejoining task claims without restarting inference.

The historical repository-wide private sweep is not used as a false green gate. The broader 20-file
compatibility sweep passed 684 tests and failed four: three domain-claim fixtures received `204`
instead of `200`, and one Python JSON-depth fixture expected a 5,000-level body to parse but the
installed decoder returned `400`. All four reproduced on private revision `7f16213`, before the
final child validation/schema/yield fixes. The candidate removes the runtime-dependent JSON
assumption: the event encoder now explicitly refuses excessive nesting, and its complete 59-test
suite passes. The unrelated three domain fixtures remain historical baseline failures rather than a
Goal release gate. The 684-test sweep ran at private revision `2bd0479`; later Goal-specific
hardening is validated by the exact 184-test private release bundle and complete 22-scenario
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
The operational commands, canary gates, and worker restart boundary are documented in
[goals-deployment.md](goals-deployment.md).

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

A hosted-remote preflight on 2026-08-31 found `forge` and all three intended physical nodes, but its
currently deployed relay does not advertise `goals/v1`; the public CLI correctly refused with
“This grid's relay does not support Grid Goal yet.” The preferred hosted physical gate therefore
still requires deployment of private relay PR #19. The pre-merge fallback is a disposable relay on
Machine C; its launcher can now supervise a loopback-only Cloudflare Quick Tunnel, verify public
reachability, and mint separate A/B identities without SSH.

The no-SSH lab now accepts `--joining-workers 2`, persists separate B/C credentials across relay
restarts, and refuses missing or duplicate physical node ids. Its 369-test Goal preflight and 34
lab-specific tests passed. Machine C is visible as `forge-gpu-2x4090`, but the disposable relay has
not yet been launched locally on C, so the three-machine hardware event remains unexecuted.

Do not label the release gate complete until that physical artifact passes, or until the project
owner explicitly waives it. Merging before then is an informed MVP decision, not the same claim as
completing the documented physical release gate.
