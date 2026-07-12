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
- **Excerpt, not conversation** (~500-char system head + ~2000-char last-user tail + derived
  features; images become markers). Full `messages` was rejected: every `auto` request would
  ship the whole conversation to a third party, against the "models stay on your machines"
  promise, with cost/latency growing with history. Metadata-only was rejected: the advisor
  can't classify the task, and ranking degrades to capability matching.
- **The Advisor sees capabilities only — no pricing, no live load.** Fitness is its one job;
  cost and availability are decided locally at pick time, where the data is fresh.
- **A model-level layer in front of the untouched engine-level selection.** After the
  auto-router picks a model it rewrites the body to the real name and hands off; engine
  choice, queueing, claiming, streaming, and billing are unchanged (an `auto` request bills
  as the chosen model; the ranking call is billed to the platform's proxy key — not the
  owner, not the consumer).
- **Advisors come from a platform catalog, not from owner-supplied endpoints** *(revision)*.
  An advisor is a `{provider, model}` pair validated against a control-plane catalog
  (per-provider model whitelist + default model; v1: `openai` with bare model names —
  `gpt-5-mini` default, `gpt-5-nano`, `gpt-4.1-mini`, `gpt-4o-mini`); the catalog maps the
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
