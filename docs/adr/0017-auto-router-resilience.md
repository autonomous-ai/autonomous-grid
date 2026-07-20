---
status: accepted
---

# Auto-router resilience: per-(node,model) health, a truthful empty-pool contract, startup servability checks

The 2026-07-16 Nightshift outage traced to one provider advertising a model its engine no longer served:
the engine's 503s tripped the per-**node** demotion, which emptied the `auto` candidate pool, which
rendered a **400 "…supports tools"** — a transient outage dressed as a permanent capability error — and
the whole thing flapped ~every 10 minutes as the demotion self-expired. This ADR records how a single bad
`(provider, model)` is made unable to take down `auto`. Grill log: `.scratch/auto-router-resilience/`
(PRD + decisions). Scope: issues 1/3/4 implemented; issue 2 (failover) design-only.

Choices a future reader will otherwise re-litigate:

- **Provider health is keyed per-(node, model) and classified by failure reason — not per-node.** A
  *model-level* failure (the engine's error body says it cannot serve the model) sets aside just that
  `(node, model)` pairing (**prune**); a *box-level* failure (timeout, connection refused, no engine
  status) still steps the whole node down (**demotion**), because a sick box is sick for every model.
  Rejected **per-node + a "never demote the last live provider" exemption**: it keeps a broken provider in
  rotation, so the consumer keeps hitting the failing engine (billed $0, but a wall of failures) instead of
  a clean, truthful 503 — and it leaves the root (advertised ≠ servable) unaddressed. Cheap because the
  failure-recording point already holds both the node id and the model on the transaction, and the engine
  status is already parsed. Concession: state stays **in-memory** (reset on respawn) and **reuses** the
  existing failure threshold/window/penalty, re-keyed — a persisted or escalating variant is a follow-up,
  not a prerequisite.

- **An empty candidate pool returns one of three answers, by cause — a demotion-emptied pool is now a
  503, not a 400.** No live model → 503 `no_providers_available`; otherwise-capable providers all
  transiently unhealthy → **503 `provider_temporarily_unavailable`**; live healthy providers but a genuine
  capability gap → 400 naming the **real** unsupported capability (aggregated from the capability check's
  own reason). The surprising part: the same "no candidate" condition can now be a 400 *or* a 503,
  deliberately — because the old code rebuilt the error from the *request's* requested parameters, which is
  exactly why a transient demotion read as "your request wants tools." Rejected **everything-503** (a real
  capability mismatch — vision on a text-only grid — would then say "retry" forever) and **message-only,
  keep the 400** (leaves the retryability lie intact). The capability check already returned its true
  reason; it was being discarded.

- **Providers verify their advertised set against the engine at startup, best-effort — runtime
  re-verification is deliberately NOT built.** When the engine's model list is non-empty and excludes an
  advertised model, the provider drops it and warns loudly (failing startup if nothing remains); when the
  list is empty or unreachable, it advertises as before (non-breaking). The existing "exactly one model
  served, assume that's the one" fallback — which hid the mismatch — is closed. Rejected **hard fail-fast**
  (breaks providers whose serving stack has a flaky/absent model list) and **periodic runtime reconcile
  now** (loop complexity + a provider redeploy + the same reliability caveat, when the master-side
  per-(node,model) prune already catches models that vanish after startup, reactively). Defense at the
  source *and* master-side, because a provider is untrusted: a buggy or non-upgraded one must not be able
  to take down `auto`.

- **Cross-provider failover is designed, not built — because the incident was single-provider.** With one
  live provider there is no rank-2 to fall to, so failover would not have prevented this outage; it is
  multi-provider hardening on a different axis. The recorded target: on a retryable poll-back failure,
  *before any chunk is delivered*, re-select the next healthy provider and re-enqueue the same transaction
  (idempotency key + request hash make it safe) while the consumer stays attached to its mailbox stream —
  bounded by a retry count and the transaction deadline. The hard constraint that makes it non-trivial: a
  mid-stream failure cannot be un-sent and must terminate, which matters most on the always-streaming
  responses/codex path. A cheaper interim (a retryable terminal signal plus the reactive prune) falls out
  of the three fixes above almost for free; build the transparent version only if that proves insufficient.
  Its implementable expansion — hook point, gates, transaction-state transitions, bounds, and the explicit
  "is the interim good enough?" test — is the **§ Failover design (issue 2)** section below.

Master relay (`grid_cli/private_server/`) and provider runtime (`grid_cli/provider_runtime/`) both change;
both are in grid-src. Basing the work on `ae0cc69` keeps the responses-auto path, so the master-side error
contract and health keying cover `chat/completions` and `responses` in one place (ADR 0016's shared path).

## Post-review follow-ups (issue 02, deferred — not prerequisites)

Surfaced by the four-reviewer pass on the per-(node, model) implementation; accepted as tracked follow-ups
rather than expand that slice. Both are bounded and neither reopens the outage this ADR closes.

- **Sick-box evasion via always-model-level error text.** Because classification keys off the provider's own
  error string, a genuinely sick box can word every failure as "model not found" and be *pruned per model*
  rather than *demoted as a node* — so it stays in rotation for `threshold × (its model count)` failed jobs
  instead of a fixed `threshold`. Bounded (consumers still get the truthful retryable 503, and a box that
  fails structurally — timeout / connection-refused — has no model-level text so it still demotes at once).
  Fix when warranted: an aggregate per-node failure tally, incremented on model-level failures too, that
  trips a node demotion once one node accumulates prunes across too many *distinct* models in the window —
  keeping the surgical single-bad-model benefit while capping the "budget scales with advertised models" gap.
- **Health-dict eviction is recency-ordered, not cooldown-aware.** The `(node, model)` health dicts are
  capped (`PROVIDER_MODEL_HEALTH_MAX_ENTRIES`, default 10000) with least-recently-failed eviction — bounded
  memory and O(1), but under a sustained flood of novel model keys it can evict an unrelated pairing whose
  prune cooldown is still active, ending that penalty early (self-correcting: the pairing re-prunes on its
  next failure). Not reachable in normal operation (a real grid has far fewer than 10k pairings). A
  cooldown-aware eviction (prefer entries past their `until`) was rejected for this pass because it is O(n)
  per insert *when over the cap* — i.e. O(n²) under the very flood it defends against, trading a bounded-
  memory attack for a CPU one; revisit only if the flood becomes a real threat.

## Failover design (issue 2 — design-only; not built this pass)

Cross-provider failover is the *transparent* version of what 1+3+4 already do reactively: when a provider's
engine fails a job, re-send that same job to the next healthy provider so the consumer never sees the blip.
It is **designed here, not built** — the Nightshift incident was single-provider, where there is no rank-2 to
fall to, so failover would not have prevented it (scope bullet above). This section records the target so a
later pass can build it without re-deriving the seams. All references are grid-src @ `7cfed10`
(`fix/auto-router-resilience`); nothing below changes runtime behaviour, and there is no code or test.

### Where it hooks in

`provider_error` (`relay.py:3966`, `POST /error/{txn}`) is the seam. Today, under a `SELECT … FOR UPDATE` on
the transaction, it does four things in order: `_record_provider_failure` (classify → prune `(node,model)` or
demote the node), `UPDATE state="failed"`, `settle_failed_within(…, 0, …)` ($0), then post-commit
`_live_bus.publish_terminal`. Transparent re-dispatch **intercepts between the classify and the mark-failed**:
if the failure is *retryable* **and** *no chunk has been delivered* **and** *budget remains*, re-select the
next healthy provider and re-enqueue instead of terminating. A mid-stream engine break is reported only after
the provider has already POSTed chunks via `POST /response/{txn}`, so it fails the "no chunk delivered" gate
and terminates unchanged — the gate is what separates the two paths, not a second code site.

### Retryable vs terminal

The distinguishing question is fault attribution: is the failure the *request's* fault (terminal — another
provider of the same model would fail identically) or the *engine/provider's* fault (retryable — worth another
seat)? The existing `_terminal_error` (`relay.py:1237`) and `_is_model_level_failure` (`relay.py:844`) already
draw this line:

- **Retryable (re-dispatch-eligible):** a model-level "unservable" error (→ this pairing is already pruned),
  an upstream **5xx**, a **timeout**, a **429** (rate-limited — another seat may be free), and upstream
  **401/403** (the *provider's* vendor credentials, not the consumer's request — provider-specific, so a
  different provider is worth trying).
- **Terminal (never re-dispatch):** a genuine upstream **request 4xx** (400/413/422 …) that `_terminal_error`
  passes through — the request really is invalid for this model, so re-sending it elsewhere only wastes a
  second provider and delays the truthful error.

Implementation is a small predicate over the already-parsed engine status — no new plumbing (the status is
extracted by `_ENGINE_ERROR_RE`, `relay.py:1234`; the reason is already in hand at the hook).

### The pre-first-chunk gate

The gate is **"has any byte reached the consumer's mailbox?"**, and the mailbox answers it exactly: the
`ChunkRow` table (`db.py:155`, PK `(transaction_id, seq)`) is the *only* thing `_stream_from_mailbox`
(`relay.py:1398`) ever yields to the consumer besides the terminal render and keepalives. So the gate is
`SELECT 1 FROM relay_chunks WHERE transaction_id = :txn LIMIT 1` — **no row ⇒ nothing delivered ⇒ safe to
re-dispatch.** Do **not** gate on `state`: `state="streaming"` is set when the provider *claims* the job at
poll (`provider_poll`, `relay.py:3057`), long before any byte, so it would forbid a perfectly safe retry.
Evaluate the gate under the same `FOR UPDATE` lock as the state reset so it cannot race a concurrent
`_insert_chunk` (`relay.py:3648`); a chunk that commits after the gate but before re-enqueue is the residual
edge, bounded by the fact that a provider reporting an error via `/error` is by construction not also
streaming success via `/response`.

### Re-enqueue + transaction state changes

When all three gates pass:

1. **Re-select** the next healthy provider for the *same real model* (`txn.model` already holds the routed
   real name, not `auto`). Reuse `_select_provider(...)`; the just-failed pairing is **already excluded**,
   because `_record_provider_failure` pruned/demoted it one step earlier — the reactive prune *is* the
   steering mechanism. If none is found, fall through to terminate (below).
2. **Retain the request.** The provider-facing body lives only in the in-memory `PendingRequest`
   (`relay.py:2462`), never on the txn — so re-dispatch needs it kept. Recommended: an **in-memory map keyed
   by `txn_id`**, dropped on any terminal, holding the body + derived `requirements`. This matches the
   in-memory health state (both die on respawn, which is fine — a respawn already fails in-flight txns), and
   it adds **no request bytes at rest** (a persisted `request_body` column would). Master-respawn continuation
   is explicitly out of scope, exactly as for demotion state.
3. **Reset the row** under the lock: `state="queued"` (re-arm the poll), `provider_node_id = new_node`,
   increment a new `retry_count`, **keep `status="pending"`** (do *not* settle), and **do not**
   `publish_terminal`. Refresh `input_rate`/`output_rate` to the new provider's price (they were fixed to the
   first provider's rate at creation, `relay.py:2408`) so the winner is paid and the consumer billed at the
   rate that actually served them.
4. **Re-enqueue** a fresh `PendingRequest` onto `_get_provider_queue(new_node)`; a `QueueFull` counts as this
   attempt failing (try the next provider, or terminate).

The consumer's `_stream_from_mailbox` loop never saw a terminal, so it keeps waiting across the whole
re-dispatch — invisibly; the 15 s keepalives hold the socket open through the gap.

| Phase | `state` | `status` | `provider_node_id` |
|---|---|---|---|
| created | `queued` | `pending` | provider #1 |
| claimed at poll | `streaming` | `pending` | #1 |
| retryable pre-chunk failure → re-dispatch | `queued` (`retry_count`++) | `pending` (unsettled) | provider #2 |
| … | `streaming` | `pending` | #2 |
| win | `completed` | `completed` (settle at #2's rate) | winner |
| exhausted / no provider / deadline | `failed` / `timed_out` | `failed` / `partial` ($0) | last tried |

On idempotency: `idempotency_key` (client `Idempotency-Key` header) + `request_hash` (`relay.py:2306`, hashed
*pre*-routing) protect **client-driven** re-sends — the interim-B path, where the app itself retries and
`_find_idempotent_transaction` de-dupes it onto the existing txn. Transparent A re-enqueues **internally** on
one txn, so its safety rests on the gate + the still-`pending` (unsettled) row + the retained body, not on
those keys; the two mechanisms are complementary, not substitutes.

### Bounds — terminate truthfully on whichever fires first

Three independent limits; whichever fires first ends the retry loop and renders the truthful terminal error:

- **Retry count** — a new `retry_count`, capped by `PROVIDER_FAILOVER_MAX_RETRIES` (new env knob mirroring the
  `PROVIDER_FAILURE_*` family; a small default such as 2). At the cap, terminate.
- **Deadline** — the existing `deadline_at` = `created_at + inference_timeout_seconds` (600 s, `db.py:99`),
  enforced by the reaper (`idx_transactions_deadline_active`). Re-dispatch does **not** extend it, so total
  failover time stays inside the budget the consumer already agreed to; a deadline reached mid-retry fails the
  txn to `timed_out` (504) regardless of `retry_count`.
- **Provider pool** — no next healthy provider ⇒ terminate immediately. This is the single-provider incident
  case: nothing to fail to → the truthful `503 provider_temporarily_unavailable` from issue 01.

### Billing — unchanged, correct by omission

`settle_transaction` / `settle_failed` (`credits.py:112` / `:198`) are idempotent on `status=="pending"`.
Because a re-dispatched attempt **does not settle**, the txn stays `pending` across every failed attempt and
exactly **one** settlement ever fires: the winning attempt (`settle_transaction`, at the winner's refreshed
rate) or the final exhaustion / reaper (`settle_failed`, `$0` — "consumer pays nothing for a failed task").
"Failed attempts $0, only the winner bills" therefore needs **no billing change** — it is a consequence of not
settling on re-dispatch, plus the rate refresh in step 3.

### Responses / codex path

Pre-first-chunk failover is dialect-agnostic — the mailbox is the same, and a responses consumer that has seen
no `response.created` yet cannot tell a retry happened. The **hard constraint** lives entirely on the
*post*-first-chunk side and is a *correctness* invariant, not an optimisation: responses is always-streaming,
so once any `response.*` event is in the mailbox, a second provider would emit a fresh `response.created` —
two in one stream, which a Codex client cannot reconcile. The "no `ChunkRow`" gate is exactly what forbids
that. On termination the responses failure render is already `_responses_failure_block` (`relay.py:1280`,
wired at `_stream_from_mailbox` `relay.py:1508`): a single `response.failed` event coded `server_error` (or
`rate_limit_exceeded` for a 429) — a coherent, backoff-able terminal outcome that needs no change. Failover
simply runs *before* that render when eligible. (A codex seat is flat-rate and usually pinned to
`max_concurrency = 1`, so the realistic multi-provider case here is several seats; the same gate and logic
apply.)

### The interim "B" — already shipped by 1+2, and its "good enough?" test

The cheap interim falls out of the three fixes with no extra work, and is live @ `7cfed10`:

1. **Prune** — the failure sets aside the bad `(node,model)` (`_record_provider_failure`, issue 02).
2. **Steer the re-send** — a re-sent `auto` request drops the pruned pairing in `_build_auto_candidates`
   (`relay.py:2006`) and ranks the survivors, so it lands on rank-2; if every capable pairing is pruned it
   returns the retryable **`503 provider_temporarily_unavailable`** (`_apply_auto_routing:2171`, issue 01)
   rather than the old capability-400 lie.
3. **Retryable terminal signal on the failed job itself** — a non-stream job gets a real **502/504** through
   the bounded status-wait (`relay_nonstream_status_wait_seconds = 50`, `config.py:89`); responses gets the
   `response.failed` block above; **streaming chat** gets the error envelope *in-band* (200 headers are long
   sent, so no retryable HTTP status is left — the one place B is structurally weakest).

So **B** = "the client re-sends and the grid steers it to health"; **A** = "the grid re-sends transparently, so
even the first request succeeds." Build **A** only when **all** of these hold — otherwise B is the answer:

- **Multi-provider is the norm on the grid.** Single-provider (the incident) has no rank-2, so A cannot help
  there at all; A only earns its complexity where a healthy alternate usually exists.
- **A client-visible one-request failure actually hurts.** OpenAI SDKs retry 502/503/504 with backoff, so B is
  already transparent for them; A matters for non-retrying clients and for **streaming-chat** clients, whose
  in-band error carries no retryable status and so does not auto-retry.
- **The complexity is paid for.** A costs body/requirements retention, a `retry_count` column, the under-lock
  chunk-gate + rate refresh, conditional settlement, and the responses double-`response.created` guard — each
  small, but the gate-vs-concurrent-insert race needs care.

**Recorded decision:** ship 1+3+4 (done), run **B** in production, and revisit **A** only if telemetry shows
the overlap — multi-provider grids *and* client-visible failures that B's retryable signal does not absorb
(chiefly streaming-chat clients that do not retry). The streaming-chat gap is the one B structurally cannot
close (a 200 has already committed the status), and is the strongest single reason A would ever be built.
