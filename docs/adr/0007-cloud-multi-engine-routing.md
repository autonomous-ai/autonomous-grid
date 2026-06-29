# ADR 0007 — Cloud multi-engine routing (`grid join --all`, one identity, model→engine)

Status: accepted (2026-06-28)

## Context

ADR 0004 stood up the cloud provider serve loop for **one** engine and §7 deliberately deferred
multi-engine: `--all` was rejected and auto-detecting >1 engine errored. This slice flips that guard
so one cloud identity can serve several local engines at once (DECISIONS **D9**; PRD User Story 27).
`grid join --all` (or a confirmed bare auto-detect of several) brings up every detected engine,
registers the **union** of their models under one `node_id`, and the serve loop keeps a
`model → llm_url` table, forwarding each claimed job to the local engine that serves its requested
model. Builds directly on ADR 0004 (the `__cloud-engine` loop, relay contract, refresh-on-401) and
DECISIONS D17 (shared engine bring-up + a mode-specific serve loop). Parent: issue
`…/issues/08-multi-engine-routing.md`.

**Port-source note.** grid-src `provider_runtime/poll_worker.py` has no model→engine table — it
forwards to a single `llm_url`, and grid-src ran one identity *per* engine. So this client-side
routing is **net-new** here, not a port; the reference still informs the loop shape (poll → forward →
submit, heartbeat cadence) but the routing decisions below were taken fresh and confirmed with the
maintainer this session.

Hard invariant: LAN stays LAN-only / unauthenticated / stateless; single-engine cloud serve and the
whole existing suite stay green; cloud reaches the relay only through `cloud/`; tokens are never
printed. Vocabulary: `engine` / `grid` / `model` on the surface — `node_id` is an internal id, and
`provider` / `consumer` / `signaling` / `network` / bare `node` stay off it.

## Decisions

1. **One `grid join` = one identity; `--all` is the union mechanism; repeated joins stay separate.**
   Each `grid join` writes one run record (carrying an `engines: [...]` list) + one `node_id` + one
   detached `__cloud-engine` process. `--all` (or an interactive "join all" confirm) gathers every
   detected engine into that single record; a repeated `grid join` on the same box is a *separate*
   identity (status quo), not a merge — merging into a live detached process would need new IPC and
   buys nothing the acceptance criteria ask for. `grid leave` (sole / `--engine <id>` / `--all`) is
   unchanged and SIGTERMs the one process, which unregisters the `node_id` and stops anything it
   launched — "stops all engines started under that identity". (The issue's "or repeated joins on one
   machine" prose is reconciled to this in the issue file.)

2. **Routing is client-side on `body["model"]`; first-detected wins.** `cloud/serve._build_routing`
   folds `[(llm_url, models, caps), …]` (detect order) into `routes: {model→llm_url}`, the union model
   list, and a merged caps envelope. The **first** engine to advertise a model wins the route; a later
   duplicate (e.g. Ollama and LM Studio both serving `llama3`) is dropped with a warning line, never an
   error — the union still advertises the model once. `relay.py` is unchanged: the poll job already
   carries `body`, so `handle_job` reads `body["model"]`, looks it up, and forwards to that engine.
   `_ServeState.route()` falls back to the sole engine when the model is missing/unknown but only one
   engine is registered (single-engine compatibility); with several engines and no match it returns
   `None` and the job is failed with "no engine serves model …", never silently mis-routed.

3. **One heartbeat, aggregate load, merged capabilities.** The identity has one `node_id`, one
   heartbeat thread, and `load = {"active_tasks": <aggregate inflight>}` across the single poll loop.
   Capabilities are probed **once per engine** (its first model, as the single-engine path did) into a
   one-key envelope, then merged first-wins into one `{schema_version:1, models:{…}}`; sibling models
   on a multi-model engine stay capability-less exactly as before — we do **not** fabricate entries for
   them. A failed probe degrades to `{}` and is tolerated by the merge (registers text-only). Known
   limitation: `max_concurrency` is a single aggregate value advertised for the identity, not tracked
   per engine — acceptable for this slice; revisit if per-engine batch widths diverge materially.

4. **Multi-engine is external-only; the built-in launch stays single-engine.** `--all` only gathers
   already-running engines (each has an `endpoint_url`), so nothing is launched for a multi-engine
   record, and `_bring_up_engines` rejects a multi-engine record that would need a built-in launch
   (a spec with no `endpoint_url`). The built-in `llama-server` path (`--serve`, one model) is
   unchanged. `--advertise-as` is rejected together with `--all`: the table is keyed on the
   **advertised** name and the body is forwarded unchanged, so advertised must equal the engine's real
   model name — single-engine `--serve` gets away with aliasing only because it sets `alias=` on
   launch, which detected external engines have no hook for. Cloud media engines are still rejected (a
   lone one) or skipped with a note (under `--all`); cloud media serving remains a later slice.

5. **The record shape is additive and back-compatible.** `_build_record` adds `engines: [{endpoint_url,
   models, engine_label}, …]` and keeps the top-level `endpoint_url` (the sole engine's, or `None` for
   several) + `models` (the union) for display and back-compat. The shared run-record layer
   (`shared/run_records.py`) and `grid leave` are untouched: a multi-engine record still has one
   `engine_id` and one `pid`. `_bring_up_engines` falls back to synthesising a single spec from the
   flat fields, so a record written before this slice still serves.

## Consequences

- Single-engine cloud serve is unchanged: `_ServeState` still constructs from `llm_url` + `models`
  (routes derived), and a job without a usable `model` still forwards to the sole engine. The whole
  existing suite stays green; new tests cover the routing helper, model routing in `handle_job`,
  multi-engine `--all` records, and the bring-up wiring against a mocked relay/probe.
- Duplicate-model routing is deterministic (first detected) but not load-aware; round-robin /
  least-loaded is intentionally out of scope (no precedent in the port source, no acceptance criterion).
- The grid page shows a multi-engine identity by its gathered kinds (e.g. `ollama+vllm`) when no
  `--engine-label` is given.
- LAN is untouched. The routing + bring-up live entirely in `cloud/serve.py` + `cli/cloud_provider.py`;
  `relay.py` and the shared record layer did not change.
