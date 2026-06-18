# Grid

**Grid is the orchestration layer for local AI. It unifies the inference engines you
already run into one private endpoint — and ships with two defaults so a bare machine
works out of the box.**

You already run Ollama on your Mac, vLLM on your GPU box, LM Studio on your laptop, MLX on
your MacBook. Grid turns all of them into a single OpenAI-compatible endpoint on your
network — you migrate nothing and learn no new runtime. Text, images, and video, same
endpoint.

- **It has no engine of its own.** Grid orchestrates real engines — the ones you already
  run (Ollama, vLLM, LM Studio, MLX) and two open-source defaults it sets up for you
  (llama.cpp for text, ComfyUI for media). Stop Grid and your engines are untouched; it
  never reimplements inference or competes with the tools it runs.
- **One endpoint, every box.** Your app points at a single `OPENAI_BASE_URL`. Grid routes
  each request to whichever machine serves that model.
- **Private by default.** LAN-only, no auth, in-memory registry — nothing phones home,
  nothing leaves your network.

## 60-second quickstart

You need Python 3.11+ and [uv](https://docs.astral.sh/uv/). Install the CLI:

```bash
uv tool install -e . --force   # provides the `grid` command
```

**1. Create the endpoint** — run on any one machine; this is the address your apps use:

```bash
grid up home --port 8090
# -> grid_url=http://192.168.1.25:8090
```

**2. Join the engines you already run.** Run each command **on the machine that engine
runs on**, and put **that machine's LAN IP** in `--at` (not `localhost`). `grid join`
starts nothing — it advertises each endpoint and heartbeats it in the background:

```bash
# the Ollama on your Mac (192.168.1.10) — one endpoint can serve several models
grid join http://192.168.1.25:8090 \
  --at http://192.168.1.10:11434/v1 \
  -m llama3 -m qwen2.5-coder --name mac

# the vLLM on your GPU box (192.168.1.20)
grid join http://192.168.1.25:8090 \
  --at http://192.168.1.20:8000/v1 \
  -m mistral-large --name gpu

# the LM Studio on your laptop (192.168.1.30)
grid join http://192.168.1.25:8090 \
  --at http://192.168.1.30:1234/v1 \
  -m gemma2 --name laptop
```

> **Two gotchas:** use each engine's **LAN IP, not `localhost`**, and make sure the engine
> listens on the LAN (Ollama `OLLAMA_HOST=0.0.0.0`, LM Studio "Serve on Local Network",
> vLLM `--host 0.0.0.0`). The advertised `-m/--model` name is forwarded verbatim, so name a
> model that engine actually serves. Full LAN/IP notes in **[reference](docs/reference.md)**.
> Stop an engine later with `grid leave <grid> --engine <name>`.

**3. Point your app at the one endpoint:**

```bash
grid info home --env
# export OPENAI_BASE_URL="http://192.168.1.25:8090/v1"
# export OPENAI_API_KEY="local-lan"   # ignored by the server; only for SDK compatibility
```

Any OpenAI SDK now sees every model on every box:

```bash
curl http://192.168.1.25:8090/v1/models          # llama3, qwen2.5-coder, mistral-large, gemma2, ...

curl http://192.168.1.25:8090/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"mistral-large","messages":[{"role":"user","content":"hello"}]}'
```

The request for `mistral-large` is routed to your GPU box; `llama3` to your Mac. Same
endpoint, same API key, nothing migrated.

## How it works

```
                          your app (OpenAI SDK)
                                   │  OPENAI_BASE_URL = http://host:8090/v1
                                   ▼
                  ┌──────────────────────────────────┐
                  │   grid  (router + OpenAI proxy)    │  in-memory registry of engines
                  │   `grid up`                        │  routes /v1/* by the `model` field
                  └────────┬──────────────┬───────────┘
              routes "llama3"          routes "mistral-large"
                           │              │
                           ▼              ▼
                  ┌────────────────┐  ┌────────────────┐
                  │  your Mac      │  │  your GPU box  │
                  │  Ollama :11434 │  │  vLLM :8000    │   ← existing engines, unchanged
                  └────────────────┘  └────────────────┘
```

Three pieces, one CLI:

- **grid** (`grid up`) — the router + OpenAI-compatible proxy. Holds a live registry of
  engines and routes each `/v1` request to one that advertises the requested model.
- **engine** (`grid join`) — an inference endpoint registered into a grid and heartbeated
  in the background. Either an existing endpoint (`--at`) or a model Grid serves for you
  (`--serve`, below). Stop it with `grid leave`.
- **app** — anything that speaks the OpenAI API, pointed at the grid's `/v1`.

See **[ARCHITECTURE.md](ARCHITECTURE.md)** for the full request flow and
**[docs/reference.md](docs/reference.md)** for the complete command reference.

## Grid's two default engines

Don't have Ollama or LM Studio? You don't need them. Grid ships with two open-source
engines it sets up for you — `llama.cpp` (text) and ComfyUI (media) — so a bare machine
goes from zero to a working endpoint with one command. They install on first use, and Grid
launches and supervises them like any other provider. Already running an engine you like?
Point Grid at it instead (above).

```bash
grid engine install llama.cpp                # install/upgrade llama-server
grid pull qwen36-35b-a3b-mtp                 # platform-aware catalog, or any HF GGUF
grid join home --serve your-model.gguf
```

Media (images + video) via ComfyUI:

```bash
grid engine install comfyui
grid engine pull image_generation            # also: image_editing, i2v
grid join home --media --bundle image_generation
```

Hosting, media, and the raw HTTP API are documented in **[docs/reference.md](docs/reference.md)**.

## Contributing

Grid is built to be easy to pick up and contribute to — start with
**[CONTRIBUTING.md](CONTRIBUTING.md)** and **[ARCHITECTURE.md](ARCHITECTURE.md)**.
Good first contributions: add a model to the catalog (`grid/models/catalog.py`) or a
media bundle (`grid/models/media_bundles.py`).

Local state lives under `~/.grid` (override with the `GRID_HOME` environment variable).

## License

MIT — see [LICENSE](LICENSE).
