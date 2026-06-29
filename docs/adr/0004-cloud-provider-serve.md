# ADR 0004 — Unified `grid join`: cloud provider serve (single engine)

Status: accepted (2026-06-28)

## Context

ADR 0001 set up modes + dispatch; 0002 sign-in + the per-grid `access_token`; 0003 the cloud grid
lifecycle (`grid up/down/ls/info`) and the `signaling_url` relay base. This slice makes `grid join`
— the one verb that makes a machine serve models — work in **cloud mode**, and lands the **unified
`grid join` flag set**.

In LAN, `grid join` writes an engine record and spawns a detached `__engine` child that push-registers
the in-memory grid proxy and heartbeats it. Cloud is the same verb with a different loop: bring the
engine up through the **shared** engine layer, then run a detached loop that **registers capabilities
with the hosted relay, long-polls it for work, forwards each claimed job to the local engine, and
heartbeats**; `grid leave` stops and unregisters it. The relay contract is ported from the reference
client `grid-src/grid_cli/provider_runtime/` (poll/register/heartbeat/probe); the proprietary backend
stays out.

Hard invariant: LAN `grid join` behaviour and the whole existing suite stay green; no off-LAN calls
leak into `shared/`/`lan/`. One engine per `grid join` here — multi-engine routing (D9) is a later slice.

## Decisions

1. **New internal `__cloud-engine <grid_id> <engine_id>` seam.** Dispatched in `cli/_main.py:_maybe_internal`
   into a new `cloud/serve.py`; LAN's `__engine` → `cli/provider._run_engine` is untouched. The mode
   boundary stays a folder boundary (D15/D17): only the serve loop differs.

2. **The engine record + `grid leave` are shared.** Record I/O and the SIGTERM→SIGKILL teardown moved
   from `cli/provider.py` into **`shared/run_records.py`** (JSON via `shared.jsonio`, so `shared/` gains
   no `lan` import); `cli/provider.py` keeps its `_*` names as thin wrappers, so LAN behaviour and the
   existing test surface are byte-identical. Cloud (`cloud/serve.py`, `cli/cloud_provider.py`) reuses the
   same module — no `cloud → cli` back-dependency.

3. **Secrets never touch the run record.** The record under `~/.grid/run/engines/<network_id>/` holds
   only non-secret routing (`signaling_url`, models, ports, `--engine-label`/pricing/concurrency). The
   detached loop loads the per-grid `access_token`/`refresh_token` from `credentials.toml` (`0o600`) by
   `network_id` at runtime — never an `Authorization: Bearer None`. A bundle missing `access_token` is a
   clean `SystemExit` at the join boundary **and** the top of the loop.

4. **Refresh-on-401.** On a relay 401 the loop calls `control_plane.refresh_network_token(network_id,
   refresh_token)` → `POST /v1/grid/tokens/{network_id}` body `{"refresh_token":…}` (**unauthenticated** —
   the refresh token is the credential), persists the new bundle via `credentials.update_network_tokens`,
   and retries once. A worker first adopts a token another worker/process already stored before hitting
   the network. Refresh failure stops the loop cleanly (it does not crash mid-iteration).

5. **Full live capability probe.** `cloud/probe.py` (ported from the reference `engine/probe.py` +
   `provider/benchmark.py`) reads the engine's `/props` + `/models` and live-tests
   `json_object`/`json_schema`/`tools`/`parallel_tool_calls`/`vision`, plus a `tok_s` benchmark, to build
   the `{schema_version: 1, models: {…}}` envelope the relay requires. A failed probe degrades silently
   to a text-only envelope — a node still registers, just with fewer features.

6. **Relay contract = the reference paths**, base = the grid's `signaling_url`, `Authorization: Bearer
   {access_token}`: `PUT /nodes/{id}` · `POST /nodes/heartbeat` (body `{load}` only — the token
   identifies the node) · `GET /relay/v1/poll` (200 job / 204 none / 401 refresh) · `POST
   /relay/v1/{response,error}/{txn}`; unregister flips the role to `consumer` (best-effort drain).
   `cloud/relay.py` uses a **status-aware** policy (204/401/404 are outcomes, not fatal) — distinct from
   `control_plane._send`, which raises on every `≥400` — so the long-running loop refreshes / re-registers
   / backs off instead of dying. Heartbeat cadence is a fixed **30s** (the LAN `--heartbeat-interval`
   governs LAN only). The relay's poll response is **untrusted input**: the supplied `endpoint_path` is
   forwarded to the local engine only if it is on a small allowlist (`chat/completions`, `completions`),
   so a buggy or compromised relay cannot use a traversal path to probe other local endpoints; a
   malformed job is dropped without killing the loop. Token refresh is serialized by a second lock and
   runs with the data lock released, so it never blocks the heartbeat thread or double-spends a refresh
   token.

7. **One engine, gated flags.** Cloud `--all` and multi-engine routing (US 27) are rejected with
   guidance; auto-detect finding >1 engine errors. `--media` serving in cloud is rejected for now (a later
   slice). The unified parser is one union; each handler validates per mode using the stamped `args.mode`
   seam: LAN `cmd_join` rejects the cloud-only flags, cloud `cmd_cloud_join` rejects `--advertise-host` /
   `--media`. Cloud-only flags default `None` so a wrong-mode use is detectable. **`--node-name` is
   dropped** — it surfaces the forbidden term `node` (vocabulary discipline) and is absent from DECISIONS
   D6/D8; the relay `meta.name` derives from `--name`/engine_id (D7). `--embedding-*` is deferred (no
   embedding serve path yet).

8. **`grid join` runs detached; the grid must be up.** `cmd_cloud_join` resolves the active grid (reusing
   `cli/cloud_grid`'s selection), reads the live `signaling_url` from `…/status`, and refuses to spawn if
   the grid isn't running or has no relay address (a clear "run `grid up` first" instead of a background
   loop that can never register). It writes the shared record, spawns `__cloud-engine`, and confirms only
   that the process stayed alive — the relay isn't locally pollable, so the success message says
   "starting", never a false "registered". `grid leave` reuses the shared `stop_engine`.

## Consequences

- LAN is untouched: the extraction is a pure refactor, the cloud-only flags are rejected in LAN, and no
  new code runs for LAN users.
- The relay layer is the one place the provider-side wire contract lives; it is the legacy-reference
  contract (tests mock the relay via `httpx.MockTransport`), adjustable if the hosted relay's provider API
  diverges.
- Tokens live only in `credentials.toml`; the run record is safe to read from any process. Refresh
  rewrites the credential store from the detached loop through the same hardened atomic writer.
- The detached seam is the single plug-in point a future multi-engine / cloud-media slice extends; the
  classification + flag-gating tests keep a new command from silently running the wrong mode's code.
