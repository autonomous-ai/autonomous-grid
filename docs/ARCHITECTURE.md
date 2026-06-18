# Architecture

Grid turns the AI engines already running on your LAN into a single
OpenAI-compatible endpoint. This document explains the moving parts and traces a
request through the code so you can find where to make a change.

> **Vocabulary.** The product words are **grid**, **engine**, and **app** (see
> [cli.md](cli.md)). The code uses the same words; the only older nouns that
> survive are `node` (a registry entry) inside `server.py`, and the `provider/` package
> name — both predate the rename and are called out below.

## The three roles

Everything is one CLI (`grid`); a machine plays one or more roles:

| Role | Brought up by | What it is |
| --- | --- | --- |
| **grid** | `grid up` | The grid server: an OpenAI-compatible proxy plus an in-memory registry of engines. Routes every `/v1` request to an engine. |
| **engine** | `grid join` | Something that runs models (Ollama, vLLM, LM Studio, MLX, llama.cpp, ComfyUI). Registers into a grid and heartbeats it. |
| **app** | any OpenAI SDK | Points at the grid's `/v1` base URL (`grid info --env`). |

A grid is the only long-lived shared state, and it is deliberately tiny: a dict of
nodes in memory, no database, no auth, LAN-only.

## Component map

```
.   (repo root)
├── cli/                The CLI, split by command group. `parser.py` builds the tree
│                       (mirrors docs/cli.md); `_main.py` is the entry point + internal
│                       subcommand dispatch. Handlers live in:
│   ├── grid.py           `grid up` / `down` / `ls` / `info` / `version` / overview
│   ├── provider.py       `grid join` / `leave` / `engines` / `models` — the engine
│   │                     lifecycle (file name predates the rename)
│   ├── engine.py         `grid engine install|pull|status|start|stop` (built-in engines)
│   ├── models.py         `grid catalog` / `pull` / `rm`
│   └── request.py        `grid chat` / `image` / `edit` / `video`
├── server.py           The grid server / OpenAI-compatible proxy (FastAPI `create_app`).
├── runtime.py          grid_url + engine endpoint URL resolution; grid server lifecycle.
├── config.py           Saved grids under ~/.grid/grids/; `select_grid`.
├── paths.py            ~/.grid filesystem layout.
├── media_runtime.py    Starts/stops the engine-local media server subprocess.
├── provider/           The engine-side media server (package name predates the rename).
│   ├── media_server.py   FastAPI app exposing /media/* (create_app).
│   ├── media_handler.py  Drives ComfyUI: submit workflow, track progress, collect outputs.
│   ├── media_gating.py   Decides which media bundles a host can advertise (by VRAM).
│   └── workflows/*.json  ComfyUI prompt graphs (image gen / edit / i2v).
├── models/
│   ├── catalog.py        Platform-aware model catalog (`grid catalog`).
│   ├── store.py          Local GGUF model files under ~/.grid/models.
│   ├── download.py       Hugging Face downloads.
│   └── media_bundles.py  ComfyUI model bundles + advertised capability names.
├── engine/
│   ├── installer.py      Install/upgrade llama.cpp.
│   ├── launcher.py       Start/stop a local llama-server.
│   └── comfyui.py        ComfyUI install + lifecycle.
└── system/
    ├── detect.py         Detect engines already running on this box (`grid join`).
    ├── host.py           Host metrics.
    └── gpu.py            NVIDIA GPU discovery via nvidia-smi.
```

## The registry

The grid server holds `app.state.nodes`, a `{node_id: Node}` dict (`server.py`). A `Node`
records its `role` (`"engine"` / `"app"` / `"both"`), advertised `models`, an
`endpoint_url` (text) and/or `media_url` (media), plus `load` and `last_heartbeat`. Key
constants:

- `NODE_TTL_SECONDS = 60` — an engine that hasn't heartbeat in 60s is dropped lazily
  the next time the registry is read.
- `ENGINE_TIMEOUT_SECONDS = 600` — how long the proxy waits on an upstream engine.

Engine selection is **load-aware**: `_active_engines(model)` filters to fresh engines
advertising the model and sorts by `active_tasks` (then heartbeat recency);
`_choose_engine(model)` takes the least-loaded one.

## Request flow — text (`/v1/chat/completions`)

```
app ──POST /v1/chat/completions {"model":"llama3",...}──▶ grid server (server.py)
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

1. `server.py:_proxy_openai` parses the JSON body and reads `model`.
2. `_choose_engine` picks an active engine advertising that model.
3. The **raw request body is forwarded unchanged** to `{endpoint_url}/chat/completions`
   — so the advertised model name must be one the upstream engine recognizes. If
   `stream: true`, the response is proxied chunk-by-chunk (SSE passthrough); otherwise
   it's returned whole.
4. The upstream is whatever the engine registered: one you already run, or a
   `llama-server` Grid launched for you (`grid join --serve`).

`/v1/completions` follows the identical path; `/v1/models` returns the de-duplicated
union of every active engine's models. `/nodes/discover` returns the live engines as
`{"engines": [...]}`, which `grid engines` / `grid models` render.

## Request flow — media (`/v1/media/*`)

Media uses fixed model names (`comfyui:image_generation`, `comfyui:image_editing`,
`comfyui:i2v`) instead of a body `model` field.

1. `server.py:_proxy_media` maps the route to its `comfyui:*` model and
   `_choose_engine` finds an engine advertising it.
2. The body is forwarded to `{engine.media_url}/media/...` and the SSE stream
   (progress events, then a result event with base64 `output_files`) is proxied back.
3. On the engine, `provider/media_server.py` hands the request to
   `provider/media_handler.py`, which loads a workflow from `provider/workflows/`,
   submits it to ComfyUI, tracks progress over WebSocket (HTTP polling fallback), and
   collects the output files.

## Engine lifecycle (`grid join`)

`cli/provider.py:cmd_join`:

1. Resolve what to join — an existing endpoint (`--at <url>` + `-m <model>`), the
   built-in engine (`--serve <model>`), media (`--media`), or auto-detect with
   `system/detect.py`.
2. Write an engine record under `~/.grid/run/engines/<grid>/` (`paths.engines_dir`) and
   spawn a **detached** `__engine <grid_id> <engine_id>` subprocess for the heartbeat loop.
3. That subprocess (`run_engine_from_record` → `_run_engine`): optionally launch a local
   `llama-server` (`engine/launcher.py`) and/or the media server (`media_runtime.py`),
   then `PUT /nodes/{id}` with the advertised models, `endpoint_url`, and `media_url`, and
   heartbeat every `--heartbeat-interval` seconds (role `"engine"`).
4. `grid leave` SIGTERMs the detached process, which unregisters (`DELETE /nodes/{id}`)
   and stops anything it started.

## Internal subcommands

`grid up` and `grid join` spawn detached children via hidden CLI subcommands:
`__server <grid_id>` (the grid server), `__engine <grid_id> <engine_id>` (an engine's
heartbeat loop), and `__media-server` (the engine-side media API). These are process
plumbing, not part of the user-facing surface.

## Design constraints worth knowing

- **LAN-only, unauthenticated.** The server ignores auth headers and binds the LAN; the
  `OPENAI_API_KEY` apps set is only for SDK compatibility. Don't add features that assume
  an authenticated, internet-facing deployment.
- **Stateless registry.** Node state is in memory and TTL-expired; restarting a grid
  forgets its engines (they re-register on their next heartbeat cycle).
- **Vendored media stack.** Parts of `provider/` and `engine/` are vendored from an
  upstream desktop app and annotated as such in their docstrings; keep vendored edits
  bracketed and minimal so they're easy to re-sync.
