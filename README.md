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
grid network create home --port 8090
# -> signaling_url=http://192.168.1.25:8090
```

**2. Point Grid at engines you already run.** Run each command **on the machine that
engine runs on**, and put **that machine's LAN IP** in `--endpoint-url` (not `localhost`).
Grid starts nothing — it advertises each endpoint and heartbeats it:

```bash
# the Ollama on your Mac (192.168.1.10) — one endpoint can serve several models
grid provider start --network http://192.168.1.25:8090 \
  --endpoint-url http://192.168.1.10:11434/v1 \
  --model llama3 --model qwen2.5-coder

# the vLLM on your GPU box (192.168.1.20)
grid provider start --network http://192.168.1.25:8090 \
  --endpoint-url http://192.168.1.20:8000/v1 \
  --model mistral-large

# the LM Studio on your laptop (192.168.1.30)
grid provider start --network http://192.168.1.25:8090 \
  --endpoint-url http://192.168.1.30:1234/v1 \
  --model gemma2
```

> **Two gotchas:** use each engine's **LAN IP, not `localhost`**, and make sure the engine
> listens on the LAN (Ollama `OLLAMA_HOST=0.0.0.0`, LM Studio "Serve on Local Network",
> vLLM `--host 0.0.0.0`). The advertised `--model` is forwarded verbatim, so name a model
> that engine actually serves. Full LAN/IP notes in **[reference](docs/reference.md)**.

**3. Point your app at the one endpoint:**

```bash
grid consumer env --network home
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
                  │   grid network                    │  in-memory registry of providers
                  │   (signaling + OpenAI proxy)      │  routes /v1/* by the `model` field
                  └────────┬──────────────┬───────────┘
              routes "llama3"          routes "mistral-large"
                           │              │
                           ▼              ▼
                  ┌────────────────┐  ┌────────────────┐
                  │  your Mac      │  │  your GPU box  │
                  │  Ollama :11434 │  │  vLLM :8000    │   ← existing engines, unchanged
                  └────────────────┘  └────────────────┘
```

Three roles, one CLI:

- **network** — the signaling server + OpenAI-compatible proxy. Holds a live registry
  of providers and routes each `/v1` request to one that advertises the requested model.
- **provider** — registers an engine into a network and heartbeats it. Either an
  existing endpoint (`--endpoint-url`) or, optionally, a model Grid hosts for you (below).
- **consumer** — anything that speaks the OpenAI API, pointed at the network's `/v1`.

See **[ARCHITECTURE.md](ARCHITECTURE.md)** for the full request flow and
**[docs/reference.md](docs/reference.md)** for the complete command reference.

## Grid's two default engines

Don't have Ollama or LM Studio? You don't need them. Grid ships with two open-source
engines it sets up for you — `llama.cpp` (text) and ComfyUI (media) — so a bare machine
goes from zero to a working endpoint with one command. They install on first use, and Grid
launches and supervises them like any other provider. Already running an engine you like?
Point Grid at it instead (above).

```bash
grid llama.cpp install                       # install/upgrade llama-server
grid models pull qwen36-35b-a3b-mtp          # platform-aware catalog, or any HF GGUF
grid provider start --network home --model your-model.gguf
```

Media (images + video) via ComfyUI:

```bash
grid media install
grid media pull image_generation             # also: image_editing, i2v
grid provider start --network home --enable-media --media-bundle image_generation
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
