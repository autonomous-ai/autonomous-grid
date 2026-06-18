# Architecture

Grid turns the AI engines already running on your LAN into a single
OpenAI-compatible endpoint. This document explains the moving parts and traces a
request through the code so you can find where to make a change.

## The three roles

Everything is one CLI (`grid`); a machine plays one or more roles:

| Role | Started by | What it is |
| --- | --- | --- |
| **network** | `grid network create` | The signaling server + OpenAI-compatible proxy. Keeps an in-memory registry of providers and routes every `/v1` request to one. |
| **provider** | `grid provider start` | Registers an engine (existing, or one Grid launches) into a network and heartbeats it. |
| **consumer** | any OpenAI SDK | Points at the network's `/v1` base URL. |

A network is the only long-lived shared state, and it is deliberately tiny: a dict of
nodes in memory, no database, no auth, LAN-only.

## Component map

```
.   (repo root)
├── cli/                The CLI, split by command group. `parser.py` builds the command tree;
│                       `network.py` / `provider.py` / `models.py` / `media.py` / `consumer.py` /
│                       `request.py` hold each group's `cmd_*` handlers. Entry point `grid`/`agrid`.
├── server.py           The signaling server / OpenAI-compatible proxy (FastAPI `create_app`).
├── runtime.py          Network URL + provider endpoint URL resolution.
├── config.py           Saved networks under ~/.grid; `select_network`.
├── paths.py            ~/.grid filesystem layout.
├── media_runtime.py    Starts/stops the provider-local media server subprocess.
├── provider/
│   ├── media_server.py   Provider-local FastAPI app exposing /media/* (create_app).
│   ├── media_handler.py  Drives ComfyUI: submit workflow, track progress, collect outputs.
│   ├── media_gating.py   Decides which media bundles a host can advertise (by VRAM).
│   └── workflows/*.json  ComfyUI prompt graphs (image gen / edit / i2v).
├── models/
│   ├── catalog.py        Platform-aware model catalog (`grid models list --catalog`).
│   ├── store.py          Local GGUF model files under ~/.grid/models.
│   ├── download.py       Hugging Face downloads.
│   └── media_bundles.py  ComfyUI model bundles + advertised capability names.
├── engine/
│   ├── installer.py      Install/upgrade llama.cpp.
│   ├── launcher.py       Start/stop a local llama-server.
│   └── comfyui.py        ComfyUI install + lifecycle.
└── system/
    ├── host.py           Host metrics (parity with the desktop app).
    └── gpu.py            NVIDIA GPU discovery via nvidia-smi.
```

## The registry

The network holds `app.state.nodes`, a `{node_id: Node}` dict (`server.py`). A `Node`
records its `role`, advertised `models`, an `endpoint_url` (text) and/or `media_url`
(media), plus `load` and `last_heartbeat`. Key constants:

- `NODE_TTL_SECONDS = 60` — a provider that hasn't heartbeat in 60s is dropped lazily
  the next time the registry is read.
- `PROVIDER_TIMEOUT_SECONDS = 600` — how long the proxy waits on an upstream engine.

Provider selection is **load-aware**: `_active_providers(model)` filters to fresh
providers advertising the model and sorts by `active_tasks` (then heartbeat recency);
`_choose_provider(model)` takes the least-loaded one (`server.py:295-316`).

## Request flow — text (`/v1/chat/completions`)

```
consumer ──POST /v1/chat/completions {"model":"llama3",...}──▶ network (server.py)
                                                                  │
                                       _proxy_openai: read body["model"]
                                       _choose_provider("llama3")  ── least-loaded match
                                                                  │
                          forward RAW body ──▶ {provider.endpoint_url}/chat/completions
                                                                  │
                                              (Ollama / vLLM / LM Studio / grid llama-server)
                                                                  │
                          stream response ◀───────────────────────┘
```

1. `server.py:_proxy_openai` parses the JSON body and reads `model`.
2. `_choose_provider` picks an active provider advertising that model.
3. The **raw request body is forwarded unchanged** to `{endpoint_url}/chat/completions`
   — so the advertised model name must be one the upstream engine recognizes. If
   `stream: true`, the response is proxied chunk-by-chunk (SSE passthrough); otherwise
   it's returned whole.
4. The upstream is whatever the provider registered: an engine you already run, or a
   `llama-server` Grid launched for you.

`/v1/completions` follows the identical path; `/v1/models` (`server.py:146`) returns the
de-duplicated union of every active provider's models.

## Request flow — media (`/v1/media/*`)

Media uses fixed model names (`comfyui:image_generation`, `comfyui:image_editing`,
`comfyui:i2v`) instead of a body `model` field.

1. `server.py:_proxy_media` maps the route to its `comfyui:*` model and
   `_choose_provider` finds a provider advertising it.
2. The body is forwarded to `{provider.media_url}/media/...` and the SSE stream
   (progress events, then a result event with base64 `output_files`) is proxied back.
3. On the provider, `provider/media_server.py` hands the request to
   `provider/media_handler.py`, which loads a workflow from `provider/workflows/`,
   submits it to ComfyUI, tracks progress over WebSocket (HTTP polling fallback), and
   collects the output files.

## Provider lifecycle (`grid provider start`)

`cli/provider.py:cmd_provider_start`:

1. Resolve the text endpoint — use `--endpoint-url` as-is, **or** launch a local
   `llama-server` (`engine/launcher.py`) for a single GGUF `--model`.
2. If `--enable-media`, start ComfyUI + the provider media server
   (`media_runtime.py`) and add the gated `comfyui:*` models.
3. `POST /nodes` to get a `node_id`, then `PUT /nodes/{id}` with the advertised models,
   `endpoint_url`, and `media_url`.
4. Loop: heartbeat every `--heartbeat-interval` seconds.
5. On Ctrl-C / exit: `DELETE /nodes/{id}` and stop any processes Grid started.

## Internal subcommands

`grid network create` and `grid provider start --enable-media` spawn child processes
that run the servers via hidden CLI subcommands (e.g. the signaling server and the
provider media server). These are an implementation detail of process management, not
part of the user-facing command surface.

## Design constraints worth knowing

- **LAN-only, unauthenticated.** The server ignores auth headers and binds the LAN;
  the `OPENAI_API_KEY` consumers set is only for SDK compatibility. Don't add features
  that assume an authenticated, internet-facing deployment.
- **Stateless registry.** Node state is in memory and TTL-expired; restarting a network
  forgets its providers (they re-register on their next heartbeat cycle).
- **Vendored media stack.** Parts of `provider/` and `engine/` are vendored from an
  upstream desktop app and annotated as such in their docstrings; keep vendored edits
  bracketed and minimal so they're easy to re-sync.
