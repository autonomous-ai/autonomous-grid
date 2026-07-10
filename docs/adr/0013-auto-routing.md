---
status: accepted
---

# Auto routing: reserved `auto`, external Ranker chain, free-first pick

An app that requests the reserved model name `auto` asks the grid to pick the model. The
auto-router builds a candidate list from the models the grid currently serves (hard-filtered
by the request's needs — vision, tools, json_schema), sends a **bounded excerpt** of the
request — never the full conversation — to a **Ranker** (an external OpenAI-compatible LLM
endpoint), and receives a fitness-ranked shortlist (≤7). It picks the first ranked model
with a free engine; **busy never becomes refusal** (no model free → queue on the
highest-ranked queueable one; every queue full → the existing 503). Remote mode ships first;
local mode reuses the same portable pure-logic module against its in-memory registry.

Choices a future reader will otherwise re-litigate:

- **Bare `auto`, reserved, shadows any engine-advertised `auto` at dispatch.** Rejected
  `grid:auto`: namespaces mean engine kind (`openai:`, `comfyui:`), not master features.
- **Excerpt, not conversation** (~500-char system head + ~2000-char last-user tail + derived
  features; images become markers). Full `messages` was rejected: every `auto` request would
  ship the whole conversation to a third party, against the "models stay on your machines"
  promise, with cost/latency growing with history. Metadata-only was rejected: the ranker
  can't classify the task, and ranking degrades to capability matching.
- **The Ranker sees capabilities only — no pricing, no live load.** Fitness is its one job;
  cost and availability are decided locally at pick time, where the data is fresh.
- **A model-level layer in front of the untouched engine-level selection.** After the
  auto-router picks a model it rewrites the body to the real name and hands off; engine
  choice, queueing, claiming, streaming, and billing are unchanged (an `auto` request bills
  as the chosen model; the ranking call runs on the owner's own vendor key).
- **Ranker chain is fixed priority, not round-robin** — up to 3 entries
  `{base_url, api_key, model}`, tried in order per request, each behind a circuit breaker
  (3 consecutive failures → skipped 60s → half-open probe). All rankers down → a
  deterministic local fallback (most free capacity → price score → name) still serves the
  request: `auto`'s availability equals the grid's, not any vendor's. The machine never
  rewrites the configured order; removing a ranker is the owner's action.
- **Per-network config owned by the network creator** (`router_enabled` + ranker triples),
  managed through an owner-facing CLI command group following the membership pattern
  (session token, account-level), stored on the control plane, and delivered to running
  masters over the existing sync-snapshot — no restart, no epoch bump, works for
  server-hosted and self-hosted masters. A VM-global env key was rejected (no per-grid
  control); keys are never flags and are never printed.
