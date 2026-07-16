# Architecture

Grid turns the AI engines you already run into a single OpenAI-compatible endpoint. It works
in two **modes** that share a vocabulary and an engine/model/media layer but route requests
differently:

- **`local`** (default) — an in-memory proxy on your local network. Engines push their endpoint
  to a grid; the grid **forwards** each request straight to the matching engine.
- **`remote`** — a signed-in thin client to autonomous's hosted relay. Engines **poll** the
  relay for work over the network; apps consume through the relay with a per-grid token.

This document explains the moving parts and traces a request through the code in each mode so
you can find where to make a change.

> **Vocabulary.** The product words are **grid** / **remote grid**, **engine**, and **app**
> (see [cli.md](cli.md)). The code uses the same words;
> the only older noun that survives near the surface is `node` (a registry entry / relay
> `node_id`). Internal API ids — the `managed-networks` control-plane path, `signaling_url` for
> the relay base — are code-level, never product terms.

## The two modes and three roles

Everything is one CLI (`grid`); a machine plays one or more roles. The roles are the same in
both modes — only the wire between **grid** and **engine** changes:

| Role | Brought up by | What it is |
| --- | --- | --- |
| **grid** | `grid up` | The endpoint apps point at. In `local`, the grid server: an OpenAI-compatible proxy + in-memory registry running on your box. In `remote`, an **remote grid** hosted on autonomous's relay. |
| **engine** | `grid join` | Something that runs models (Ollama, vLLM, LM Studio, MLX, llama.cpp, ComfyUI). In `local` it registers into the grid and is forwarded requests; in `remote` it polls the relay for work. |
| **app** | any OpenAI SDK | Points at the grid's `/v1` base URL (`grid info --env`). In `remote` the base is the relay and the key is a per-grid access token. |

In **`local`** the grid is the only long-lived shared state and it is deliberately tiny: a dict
of nodes in memory, no database, no auth, on the local only. In **`remote`** the long-lived state
(grids, membership, queued work) lives on autonomous's hosted relay; this repo is only the thin
client — the engine that serves a grid and the app that consumes it, plus local sign-in
credentials.

## Component map

The code is four top-level packages with the mode boundary enforced by folders: `cli/` (the
command surface), `shared/` (used by both modes), `local/` (local mode), and `remote/` (remote mode).
`local/` and `remote/` import `shared/`, never each other.

```
.   (repo root)
├── cli/                The command surface, split by group. `parser.py` builds the tree
│   │                   (mirrors docs/cli.md); `_main.py` is the entry point + internal
│   │                   subcommand dispatch; `dispatch.py` resolves the mode and routes.
│   ├── parser.py            Argparse tree for every command + flag.
│   ├── _main.py             Entry point; `_maybe_internal` dispatches the hidden `__*` children.
│   ├── dispatch.py          Mode resolution + routing (AGNOSTIC / GATED / REMOTE_HANDLERS / REMOTE_ONLY).
│   ├── mode.py              `grid mode` / `grid use` (mode + active-grid selection).
│   ├── grid.py              `grid up` / `down` / `ls` / `info` / `version` / overview (local).
│   ├── provider.py          `grid join` / `leave` / `engines` / `models` — local engine lifecycle
│   │                        (file name predates the rename).
│   ├── request.py           `grid chat` / `image` / `edit` / `video` (local).
│   ├── engine.py            `grid engine install|pull|status|start|stop` (built-in engines).
│   ├── models.py            `grid catalog` / `pull` / `rm`.
│   ├── auth.py              `grid login` / `logout` (remote sign-in).
│   ├── codex_signin.py      The `--api codex` OAuth sign-in UX (browser + `--no-browser` paste flow).
│   ├── remote_grid.py     Remote `up` / `down` / `ls` / `info` + `members`.
│   ├── remote_provider.py Remote `join` / `leave` (serve a remote grid).
│   ├── remote_request.py  Remote `chat` / `image` / `edit` / `video` (consume via relay).
│   ├── remote_router.py   Remote `grid router` — owner config for auto-routing (model `auto`).
│   └── media_io.py          Shared media SSE/file IO used by local + remote request handlers.
├── shared/             Used by both modes.
│   ├── state.py             Persisted mode pointer + per-mode active grid (~/.grid/state.json).
│   ├── paths.py             ~/.grid filesystem layout.
│   ├── run_records.py       Detached-engine run record + `grid leave` teardown (local + remote).
│   ├── jsonio.py            Atomic JSON read/write helpers.
│   ├── engine/              Install/launch llama.cpp + ComfyUI lifecycle.
│   ├── models/              Catalog, local GGUF store, downloads, media bundles.
│   ├── media/               ComfyUI-driving media handler + workflow JSON (vendored).
│   └── system/              Detect running engines, host metrics, GPU discovery.
├── local/                local mode.
│   ├── server.py            The grid server / OpenAI-compatible proxy (FastAPI `create_app`).
│   ├── runtime.py           grid_url + endpoint resolution; grid server lifecycle.
│   ├── config.py            Saved grids under ~/.grid/grids/; `select_grid`.
│   ├── media_server.py      The engine-side media API (FastAPI, exposes /media/*).
│   └── media_runtime.py     Starts/stops the engine-local media server subprocess.
└── remote/           Remote mode (thin client).
    ├── control_plane.py     Auth / device-code login / tokens / managed-networks HTTP.
    ├── credentials.py       The 0o600 token store (~/.grid/credentials.toml).
    ├── api_keys.py          The kind-keyed vendor-credential store (~/.grid/api_keys.toml, 0o600):
    │                        API keys, and the codex OAuth bundle + its cross-process rotation CAS.
    ├── codex_oauth.py       Codex OAuth PKCE wire: authorize URL, code exchange, refresh grant.
    ├── codex_auth.py        Codex seat decoding: access-token JWT claims → account id + plan tier.
    ├── codex_callback.py    One-shot localhost OAuth callback listener + pasted-redirect parsing.
    ├── codex_probe.py       The free join probe (vendor model listing): egress IP, seat liveness,
    │                        and the seat's real entitled model set.
    ├── relay.py             Relay HTTP wire: provider poll/heartbeat + consumer client/headers.
    ├── serve.py             The detached poll → forward → submit serve loop.
    └── probe.py             Capability probe + benchmark for what the relay needs to register.
```

## local mode

### The registry

The grid server holds `app.state.nodes`, a `{node_id: Node}` dict (`local/server.py`). A `Node`
records its `role` (`"engine"` / `"app"` / `"both"`), advertised `models`, an `endpoint_url`
(text) and/or `media_url` (media), plus `load` and `last_heartbeat`. Key constants:

- `NODE_TTL_SECONDS = 60` — an engine that hasn't heartbeat in 60s is dropped lazily the next
  time the registry is read.
- `ENGINE_TIMEOUT_SECONDS = 600` — how long the proxy waits on an upstream engine.

Engine selection is **load-aware**: `_active_engines(model)` filters to fresh engines
advertising the model and sorts by `active_tasks` (then heartbeat recency);
`_choose_engine(model)` takes the least-loaded one.

### Request flow — text (`/v1/chat/completions`)

```
app ──POST /v1/chat/completions {"model":"llama3",...}──▶ grid server (local/server.py)
                                                             │
                                  _proxy_openai: read body["model"]
                                  _choose_engine("llama3")  ── least-loaded match
                                                             │
                     forward RAW body ──▶ {engine.endpoint_url}/chat/completions
                                                             │
                                         (Ollama / vLLM / LM Studio / grid llama-server)
                                                             │
                     stream response ◀──────────────────────┘
```

1. `local/server.py:_proxy_openai` parses the JSON body and reads `model`.
2. `_choose_engine` picks an active engine advertising that model.
3. The **raw request body is forwarded unchanged** to `{endpoint_url}/chat/completions` — so
   the advertised model name must be one the upstream engine recognizes. If `stream: true`, the
   response is proxied chunk-by-chunk (SSE passthrough); otherwise it's returned whole.
4. The upstream is whatever the engine registered: one you already run, or a `llama-server`
   Grid launched for you (`grid join --serve`).

`/v1/completions` follows the identical path; `/v1/models` returns the de-duplicated union of
every active engine's models. `/nodes/discover` returns the live engines as `{"engines": [...]}`,
which `grid engines` / `grid models` render.

### Request flow — media (`/v1/media/*`)

Media uses fixed model names (`comfyui:image_generation`, `comfyui:image_editing`,
`comfyui:i2v`) instead of a body `model` field.

1. `local/server.py:_proxy_media` maps the route to its `comfyui:*` model and `_choose_engine`
   finds an engine advertising it.
2. The body is forwarded to `{engine.media_url}/media/...` and the SSE stream (progress events,
   then a result event with base64 `output_files`) is proxied back.
3. On the engine, `local/media_server.py` hands the request to `shared/media/media_handler.py`,
   which loads a workflow from `shared/media/workflows/`, submits it to ComfyUI, tracks progress
   over WebSocket (HTTP polling fallback), and collects the output files.

### Engine lifecycle (`grid join`)

`cli/provider.py:cmd_join`:

1. Resolve what to join — an existing endpoint (`--at <url>` + `-m <model>`), the built-in
   engine (`--serve <model>`), media (`--media`), or auto-detect with `shared/system/detect.py`.
2. Write an engine record under `~/.grid/run/engines/<grid>/` (`shared/run_records.py`) and spawn
   a **detached** `__engine <grid_id> <engine_id>` subprocess for the heartbeat loop.
3. That subprocess (`cli/provider.py:run_engine_from_record` → `_run_engine`): optionally launch
   a local `llama-server` (`shared/engine/launcher.py`) and/or the media server
   (`local/media_runtime.py`), then `PUT /nodes/{id}` with the advertised models, `endpoint_url`,
   and `media_url`, and heartbeat every `--heartbeat-interval` seconds (role `"engine"`).
4. `grid leave` SIGTERMs the detached process, which unregisters (`DELETE /nodes/{id}`) and stops
   anything it started.

## Remote mode

A remote grid is hosted on autonomous's relay; this repo only runs the engine that serves it and
the app that consumes it. Both authenticate every relay call with the grid's per-grid **access
token** (Bearer). The relay base URL (`signaling_url` internally) is **resolved live** from the
grid's status each time — it is never persisted at sign-in (ADR 0003 / ADR 0005).

### Request flow — consume (`grid chat` / `image` / `edit` / `video`)

```
app (grid chat) ──POST /relay/v1/chat/completions (Bearer token)──▶ hosted relay
                                                                       │ enqueue job
engine ──GET /relay/v1/poll (long-poll)──▶ claims job ◀────────────────┘
   │ forward body to local engine, read its reply
   └──POST /relay/v1/response/{txn}──▶ relay ──result──▶ app (grid chat)
```

1. `cli/remote_request.py:_resolve` gates in order — signed in → a grid resolves → it has an
   access token → it is up — and reads the relay base from the grid's live `…/status`.
2. The handler POSTs to `{relay}/relay/v1/chat/completions` (media → `/relay/v1/media/*`,
   consumed as an SSE stream) with the Bearer token and the optional remote-only routing headers
   (`--target-provider` → `X-Target-Provider`, `--allow-self-provider` → `X-Allow-Self-Provider`).
3. The relay queues the job for a serving engine and returns the engine's result to the app
   (whole for chat, streamed SSE for media). A 401 is a clean "run `grid login`" — the one-shot
   consume path does not refresh the token. See [ADR 0005](adr/0005-remote-consume.md).
4. When the app sends the reserved model `model: "auto"` (and the owner enabled routing with
   `grid router`), the **relay** picks the real model *before* engine selection — its **Auto-router**
   ranks the grid's live candidate models via an external Advisor and rewrites the body to the chosen
   name, then the normal engine selection above runs unchanged. This client only sends `auto` and
   reads the pick back from the `X-Grid-Routed-Model` / `X-Grid-Router` response headers; the routing
   logic itself lives server-side (see below). See [ADR 0013](adr/0013-auto-routing.md).

**The `responses` endpoint (codex consumers) has no CLI verb.** A grid serving `codex:*` models is
consumed by an **external Codex app** pointed at `POST {relay}/relay/v1/responses` with the same
relay base URL + Bearer token that `grid info --env` prints — the OpenAI **Responses API**, streamed
SSE end-to-end ([docs/cli.md](cli.md#pointing-a-codex-app-at-your-grid-using-codex-models)). Each
engine kind serves exactly its own endpoints (the **per-kind endpoint matrix**, ADR 0015 D-b): codex
⇒ `responses` only, openai ⇒ `chat/completions` only, hardware ⇒ the chat pair — enforced at the
relay by each model's advertised `endpoints` capability, and again by the provider before forwarding.
`grid chat -m codex:*` is therefore refused client-side with the point-a-Codex-app guidance, and a
chat job can never reach a codex seat. Two retention truths ride this path: requests to `codex:*`
models **leave the grid for the vendor**, and the relay retains stream chunks for its task TTL like
every endpoint — even though the vendor itself is forced stateless (`store:false`, full history
resent every turn). See [ADR 0015](adr/0015-codex-subscription-engine.md).

### Engine lifecycle (`grid join` in remote mode)

`cli/remote_provider.py:cmd_remote_join` writes the same kind of engine record (shared
`shared/run_records.py`) and spawns a detached `__remote-engine <network_id> <engine_id>` instead
of `__engine`. That subprocess (`remote/serve.py:run_remote_engine_from_record`):

1. Brings the engine(s) up through the **same shared layer** as local, and probes capabilities
   (`remote/probe.py`) into the envelope the relay requires.
2. Registers with the relay (`PUT /nodes/{node_id}` via `remote/relay.py`), then loops
   **poll → forward → submit**: long-poll `GET /relay/v1/poll`, forward each claimed job to the
   local engine, and post the result back (`POST /relay/v1/response/{txn}`, or `/error/{txn}`).
   `--max-concurrency N` runs N such poll workers under the one identity, serving N jobs at once
   (see [ADR 0009](adr/0009-remote-provider-concurrency.md)).
3. A heartbeat thread keeps the node live (`POST /nodes/heartbeat`); a 401 on any call refreshes
   the per-grid token (`control_plane.refresh_network_token`) and retries.
4. `grid join --all` serves several local engines under **one** identity: it registers the union
   of their models and routes each polled job to the engine serving the requested `body["model"]`
   (first-detected wins on a duplicate). See [ADR 0007](adr/0007-remote-multi-engine-routing.md).
5. `grid join --media` also brings up ComfyUI + the media server, registers the `comfyui:*`
   workflows the host's VRAM gates in, and forwards `media/*` jobs to the media server on loopback
   (always streamed SSE) — media-only or alongside a text engine. See
   [ADR 0008](adr/0008-remote-media-serve.md).
6. `grid join --api <kind>` serves an **API engine** (v1: `openai`): the join resolves the key
   (env var → machine-local key store → hidden prompt), validates it against the vendor's model
   listing, and stores it in `~/.grid/api_keys.toml` (`0o600`, survives `grid logout`; a new env
   value overwrites it — rotation restarts the engine). With no `-m` it serves the whole whitelist
   ∩ key-visible models. The record's spec carries kind + vendor base URL + advertised `openai:*`
   names (never the key — the detached loop reads the key store at startup), and the loop registers
   those models with **static** whitelist capabilities (`shared/models/api_catalog.py` — the vendor
   is never probed, and only `chat/completions` is advertised/served: a legacy `completions` job
   gets a structured error, never a forward) and forwards their `chat/completions` jobs to the
   vendor with a `Authorization: Bearer` header and the advertised→vendor model rewrite. A vendor
   401 is a job error in a separate auth domain — it never triggers the relay-token refresh in
   step 3, never unregisters the engine, and (like 403/429) warns on the engine log. An API-only
   identity defaults to 8 poll workers (`--max-concurrency` still wins); any hardware engine in the
   union keeps the default of 1. See [ADR 0012](adr/0012-api-engines.md).
7. `grid join --api codex` serves a **subscription seat** through the same loop, with four codex
   deltas ([ADR 0015](adr/0015-codex-subscription-engine.md)):
   - **Credential.** An OAuth bundle (access + rotating single-use refresh token), not a key: the
     join signs in via the CLI's own PKCE flow (`cli/codex_signin.py`, `remote/codex_oauth.py`) and
     stores the bundle in the same kind-keyed store. There is **no env-var input path**, and
     `~/.codex/auth.json` is never read or written. The serve loop primes the seat into a holder
     **outside** the routing snapshot, re-resolved by kind at forward time — a token rotation must
     not rebuild routing or race a hot-reload swap.
   - **Endpoint matrix.** Codex engines advertise `endpoints: ["responses"]` and the loop refuses
     any other job with a structured error after routing (including via the single-URL fallback) —
     never a translation. Forwarding is verbatim passthrough: fresh per-attempt headers (Bearer +
     the account-id header derived from the token's own claim), the body untouched, and the reply
     always streamed — upstream SSE regrouped into whole `event:`+`data:` **event blocks** so the
     relay's line-oriented mailbox never tears a pair (byte-fidelity is pinned by a fixture shared
     with grid-src).
   - **Refresh discipline.** Upstream 401 → refresh → retry **once** (codex only; openai keeps
     job-error-without-retry), plus a proactive refresh on the heartbeat tick so an idle grid still
     rotates. The refresh is a cross-process CAS under the store's file lock (N grids on one box
     share ONE seat), journaled so a crash between the vendor exchange and the persist is
     *detectable* ("sign in again") rather than a silent zombie; shutdown drains a mid-flight
     exchange before the process dies.
   - **Seat-safe default.** A codex-containing union pins the poll-worker default to **1** — a
     flat-rate seat is never hammered eight-wide by default; explicit `--max-concurrency` wins.
     Every forwarded job **spends the seat's own monthly Codex allowance**, and the join's free
     probe (the vendor's model listing) refuses up front when Cloudflare challenges the machine's
     egress IP — a datacenter/VPS address typically cannot serve a seat, and finding out at join
     beats finding out per-job.
8. `grid leave` SIGTERMs the subprocess, which flips the node back to `consumer` so the relay
   drains queued work, and stops anything it launched. See
   [ADR 0004](adr/0004-remote-provider-serve.md).

## Internal subcommands

`grid up` and `grid join` spawn detached children via hidden CLI subcommands (dispatched in
`cli/_main.py:_maybe_internal`) — process plumbing, not part of the user-facing surface:

- `__server <grid_id>` — the local grid server (`local/server.py`).
- `__engine <grid_id> <engine_id>` — a local engine's heartbeat loop (`cli/provider.py`).
- `__remote-engine <network_id> <engine_id>` — a remote engine's serve loop (`remote/serve.py`).
- `__media-server` — the engine-side media API (`local/media_server.py`).

`grid leave` SIGTERMs the engine child (`__engine` in local, `__remote-engine` in remote mode); the engine
record and teardown are shared (`shared/run_records.py`).

## Design constraints worth knowing

- **local mode is local-only and unauthenticated.** The grid server ignores auth headers and binds
  the local; the `OPENAI_API_KEY` apps set is only for SDK compatibility. Don't add features that
  assume an authenticated, remote-facing deployment *to local mode*.
- **local registry is stateless.** Node state is in memory and TTL-expired; restarting a grid
  forgets its engines (they re-register on their next heartbeat cycle).
- **Remote mode is a thin client.** It authenticates (per-grid access tokens, refreshed on 401)
  and makes off-local calls to the relay, but the hosted backend — the relay service, its Postgres,
  billing — and heavy server dependencies stay out of this repo (DECISIONS D1, D14). Remote admin
  here is allowlist-only (`grid members`); richer management lives on the website (D13).
- **Auto-routing (`auto`) decides server-side.** This repo ships only the owner CLI (`grid router`,
  which writes per-network config through the control plane) and the consumer's `auto` request. The
  Auto-router itself — candidate ranking, the Advisor chain, circuit breakers, free-first pick, and the
  bounded excerpt that is the only request data leaving the grid — lives in the relay/master (grid-src),
  consistent with "the backend stays out of this repo". Don't reimplement routing here. See
  [ADR 0013](adr/0013-auto-routing.md).
- **Vendored media stack.** Parts of `shared/media/` and `shared/engine/` are vendored from an
  upstream desktop app and annotated as such in their docstrings; keep vendored edits bracketed
  and minimal so they're easy to re-sync.
