---
status: accepted
---

# Auto routing on the Responses dialect: same reserved name, endpoint-scoped pool

ADR 0013 shipped the reserved model name `auto` on the chat path only; ADR 0015 shipped codex
engines as a Responses-dialect passthrough where the app always names a real model. This ADR
extends `auto` to the Responses endpoint so a Codex-CLI-style app can say `model = "auto"` and
let the grid pick among the codex models the grid currently serves. Grill log: the 2026-07-17
session recorded in `.scratch/auto-router-codex/`.

Choices a future reader will otherwise re-litigate:

- **Bare `auto` stays the ONE reserved name; the candidate pool is endpoint-scoped, not
  kind-scoped.** On the Responses endpoint the pool is every served model advertising that
  endpoint (`endpoints: ["responses"]` — today codex models, tomorrow any Responses-capable
  kind), aggregated across seats exactly like chat candidates aggregate engines. Rejected
  `codex:auto`: it would mint a second reserved name per kind (each one shadowed, documented,
  and tested forever) and scope the pool by kind, arbitrarily excluding future
  Responses-capable kinds. Concession: `codex:auto` returns a 400 that names `auto` — a hint
  branch, not an alias; it never appears in a model list.

- **The `auto` branch derives capability requirements from the Responses body; the named-model
  branch keeps v1's endpoints-only passthrough.** Codex-subs deliberately skipped requirement
  derivation because the app picked the model and owns that choice. With `auto` the router
  picks on the app's behalf, so it must know the request's needs (image parts → vision,
  `tools` → tools) — and the body has to be parsed anyway to build the Advisor excerpt, so the
  hard filter reuses that one parse. The asymmetry is deliberate, not drift.

- **One excerpt shape across dialects** (symmetric mapping, no codex-specific excerpt): the
  system head is the head of `instructions` (fallback: the first system/developer message
  item); user turns are the last 3 `role: "user"` message items' text tails;
  `function_call` / `function_call_output` / `reasoning` items never leave the grid (the
  Responses-dialect equivalent of "never ship assistant/tool turns"); images become markers.
  Codex CLI's near-constant `instructions` boilerplate costs ~500 chars and is harmless — the
  rubric already classifies by the last ask, not the agent envelope. A codex-tuned excerpt was
  rejected: two shapes means two documented, separately calibrated formats, and it discards
  real `instructions` signal from non-Codex-CLI apps.

- **A `vendor_rank` candidate fact so an all-identical pool stays rankable.** The free-tier
  codex models tie on every rendered fact (ctx 272k, tools+vision, $0/$0), leaving the Advisor
  only names — and `gpt-5.6-terra` vs `gpt-5.6-luna` are post-cutoff slugs no advisor can
  rank, which hollows out 0013's "ranks on facts, not names" for this pool. The rank is a
  bounded int riding the existing capability envelope, sourced from the static whitelist's
  curated order (semantics we own: earlier = more capable); the vendor's observed `priority`
  field is corroborating evidence only, its semantics unverified. Rendered only when present;
  chat candidates without it are unchanged.
  _As built (issue 01):_ the master reads it from the untrusted capability envelope with a
  magnitude bound (`VENDOR_RANK_MAX`), dropping a malformed or pathological value; it does **not**
  verify the value against a whitelist and does **not** restrict which models may carry it —
  scoping `vendor_rank` to API-engine models (never a plain chat model, where a self-declared rank
  would jump a capacity+price tie) is the join-time CLI's responsibility (issue 03).

- **Deterministic-fallback tie-break: when blind, pick the least capable.** After the existing
  free-capacity → price keys, prefer the highest `vendor_rank` number (today: `gpt-5.4-mini`).
  This encodes the philosophy the chat fallback already has by accident of pricing ("blind →
  conserve the owner's spend"): a codex pool ties on capacity and price, so the old order
  decided by alphabet — right answer, wrong reason. Rejected quality-first (flagship for every
  trivial request burns the seat's 30-day window while nobody is ranking). The fallback
  remains an availability guarantee, not a quality guarantee.

- **The response carries the chosen advertised name, not `auto`.** The D-e mask (ADR 0015)
  targets the chosen model's advertised name (snapshot expansion included), plus the same
  transparency headers as chat: `X-Grid-Routed-Model` / `X-Grid-Router`. Echoing `auto` back
  was rejected: the app goes blind on which model served it, and the response would disagree
  with billing (which records the chosen model). E2E checkpoint, not assumed: Codex CLI
  tolerates `response.model ≠ requested` (evidence it does: the vendor itself silently
  reroutes models and the client survives unknown slugs with fallback metadata).

- **One config, shared with chat**: the same per-network `router_enabled`, the same Advisor
  chain + breakers, the same per-grid proxy key. A per-dialect toggle was rejected as surface
  without control — an app can always name a codex model directly, so a responses-only off
  switch protects no quota; it only disables the picking.

- **Quota-blind, reaffirmed.** The rich `x-codex-*` quota headers stay unread at serve time
  (codex-subs Out of Scope, kept): the router picks the *model*, the engine layer picks the
  *seat*, and a quota-aware pick is a separate feature (persist + thresholds + a per-seat data
  flow) — named follow-up, not scope creep here. Indirect protection already exists: the
  SIMPLE→cheapest rubric and the conservative fallback above.

Errors on this endpoint wear the vendor's `{"detail": ...}` shape like every other
`/responses` rejection (router disabled → 404 `auto_routing_disabled`; no Responses-capable
candidate → the no-providers/capability error; `auto` + `X-Target-Provider` → 400, the chat
rule extended). Remote mode only, by construction: API engines exist only in remote mode
(ADR 0012); the local phase-2 seam of ADR 0013 is untouched.
