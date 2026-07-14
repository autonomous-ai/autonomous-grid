---
status: accepted
---

# Auto routing: reserved `auto`, external Advisor chain, free-first pick

> **Revised 2026-07-10, pre-merge** (nothing shipped): **Rankers became Advisors** — picked from a
> platform catalog by `provider[:model]` name and called through the platform's LLM proxy on a
> control-plane-allocated per-grid key. The owner no longer supplies a base URL or an API key.
> Grill log: `.scratch/auto-router/regrill-2026-07-10-provider-spec.md`.

An app that requests the reserved model name `auto` asks the grid to pick the model. The
auto-router builds a candidate list from the models the grid currently serves (hard-filtered
by the request's needs — vision, tools, json_schema), sends a **bounded excerpt** of the
request — never the full conversation — to an **Advisor** (an external LLM picked from the
platform's advisor catalog, called through the platform's LLM proxy), and receives a
fitness-ranked shortlist (≤7). It picks the first ranked model with a free engine; **busy
never becomes refusal** (no model free → queue on the highest-ranked queueable one; every
queue full → the existing 503). Remote mode ships first; local mode reuses the same portable
pure-logic module against its in-memory registry.

Choices a future reader will otherwise re-litigate:

- **Bare `auto`, reserved, shadows any engine-advertised `auto` at dispatch.** Rejected
  `grid:auto`: namespaces mean engine kind (`openai:`, `comfyui:`), not master features.
- **Excerpt, not conversation** (~500-char system head + the ~2000-char tails of the **last 3
  user turns**, oldest→newest + derived features; images become markers). The last-3-user-turns
  window (not just the final message) means a terse final turn ("continue", "fix that") still
  carries the task/domain set in the turns leading up to it, without ever shipping assistant/tool
  turns or older user turns. Full `messages` was rejected: every `auto` request would ship the whole
  conversation to a third party, against the "models stay on your machines" promise, with cost/latency
  growing with history. Metadata-only was rejected: the advisor can't classify the task, and ranking
  degrades to capability matching.
- **The Advisor ranks on model facts, not names — but never on pricing or live load.**
  *(Pricing clause superseded by [0014](./0014-advisor-price-visibility.md), 2026-07-14: the
  Advisor now sees each candidate's price; free capacity and throughput remain never-sent.)* Each
  candidate is rendered as its own line carrying the model name, its **context window**, and its
  **capability names** (`tools`, `vision`, …) — owner-side facts about the grid's own engines, so the
  Advisor ranks on data rather than guessing a model's strengths from its name. The system prompt
  carries a **classification-first rubric** *(revision, converged via live A/B against the real
  advisor proxy: a "prefer smallest" wording under-provisioned hard requests onto tiny models, and
  an "adequacy first" wording over-provisioned trivial ones onto the API model)*: the Advisor first
  classifies the request as SIMPLE (greetings, short factual questions — anything a small model
  answers correctly) or DEMANDING (math/proofs, non-trivial code, multi-step reasoning, long or
  specialized content), then maps the class to the candidate spectrum — SIMPLE → the
  smallest/cheapest adequate candidate (the grid owner pays for every token), DEMANDING → the most
  capable candidates, never a sub-billion model (a parameter size in the name is treated as a
  fact). Candidate-list order is declared arbitrary (the free-capacity sort put the API engine
  first, which biased rankings) and uncertainty breaks toward DEMANDING. Held stable across both
  non-reasoning advisors (gpt-4.1-mini, gpt-4o-mini).
  Still never sent: per-engine **pricing** *(reversed by [0014](./0014-advisor-price-visibility.md))*,
  **free capacity**, and **throughput** — cost and availability are decided locally at pick
  time, where the data is fresh. The Advisor sees at most
  **50 candidates** (a bounded, deterministically-ordered slice) so the prompt can't grow without
  limit on a large grid. This is grid-side engine metadata (from whichever nodes registered as
  providers), not consumer request data, so the **consumer privacy surface is unchanged** — the
  excerpt (what leaves the grid *about the request*) is exactly as before; provider-supplied
  capability names are additionally bounded to a known vocabulary so a node can't inject arbitrary
  strings into the ranking prompt. A context window is included only when actually known: the probe
  omits an unknown window rather than baking a default, and the master additionally treats a reported
  `128000` as unknown — because that is both the CLI's own default `--ctx-size` and the old bake
  value, so it is indistinguishable from "the operator never specified one." A real ctx signal thus
  requires an explicit non-default `--ctx-size` (or a future probe of the engine's true window); it is
  a standing rule, not a transitional one.
- **A model-level layer in front of the untouched engine-level selection.** After the
  auto-router picks a model it rewrites the body to the real name and hands off; engine
  choice, queueing, claiming, streaming, and billing are unchanged (an `auto` request bills
  as the chosen model; the ranking call is billed to the platform's proxy key — not the
  owner, not the consumer).
- **Advisors come from a platform catalog, not from owner-supplied endpoints** *(revision)*.
  An advisor is a `{provider, model}` pair validated against a control-plane catalog
  (per-provider model whitelist + default model; v1: `openai` with bare model names —
  `gpt-4.1-mini` default, `gpt-5-mini`, `gpt-5-nano`, `gpt-4o-mini`); the catalog maps the
  provider to the platform LLM proxy's URL (control-plane env). BYO `--base-url` was
  **dropped, not hidden**: no user-supplied advisor URLs means no owner key custody, no SSRF
  surface at the write path, and vendors/models are added server-side with no CLI release.
  `grid router models` lists the catalog; an off-whitelist model is a 400 naming the valid
  models. The same provider may repeat in the chain with different models — with a
  one-provider catalog that is the only route to a real failover chain.
- **One per-grid proxy key, allocated by the control plane** *(revision)* — minted lazily on
  the grid's first successful `set-advisors` through the proxy's key-management API, scoped
  to the whitelisted advisor models, reused for the network's lifetime. Mint failure fails
  the set cleanly (nothing saved). The owner never handles a key; it rides only the owner's
  snapshot to the running master (which must hold it to dial the proxy) and is masked on
  every other read. An owner *can* extract it there by design — accepted; damage is bounded
  by the models scope (budget/rate caps at mint are a named follow-up, settable manually on
  the proxy meanwhile).
- **Advisor chain is fixed priority, not round-robin** — up to 3 entries, set
  **replace-all** in one command (token order = priority), tried in order per request, each
  behind a circuit breaker (3 consecutive failures → skipped 60s → half-open probe). All
  advisors down → a deterministic local fallback (most free capacity → price score → name)
  still serves the request: `auto`'s availability equals the grid's, not any vendor's. The
  machine never rewrites the configured order; removing an advisor is the owner's action.
- **Per-network config owned by the network creator** (`router_enabled` + advisor pairs +
  the per-grid key), managed through an owner-facing CLI command group following the
  membership pattern (session token, account-level), stored on the control plane, and
  delivered to running masters over the existing sync-snapshot — no restart, no epoch bump,
  works for server-hosted and self-hosted masters. The snapshot **materializes**
  `{base_url, api_key, model}` triples from the catalog at read time, so the master-side
  contract (and implementation) is unchanged by the revision and moving the proxy is an env
  change, not a data migration. A VM-global env key was rejected (no per-grid control); keys
  are never flags and are never printed — `status` returns neither key nor URL.
