# Grid

**Point Grid at the AI engines you already run, and they become one private endpoint.**

You already run Ollama on your Mac, vLLM on your GPU box, LM Studio on your laptop.
Point Grid at them — now they're one private endpoint. Your app talks to all your
machines and all your engines at once, and you replaced nothing. Plus images and
video, same endpoint.

Grid is a LAN-only, OpenAI-compatible aggregating proxy. It sits on top of everything
you already have and unifies it — no migration, no new runtime to learn — and nothing
leaves your network.

- **Replaced nothing.** Grid advertises your *existing* OpenAI-compatible servers
  (Ollama, vLLM, LM Studio, llama.cpp, …). It doesn't replace or restart them.
- **One endpoint, every box.** Your app points at a single `OPENAI_BASE_URL`. Grid
  routes each request to whichever machine serves that model.
- **Private by default.** LAN-only, no auth, in-memory registry. Nothing phones home;
  nothing leaves your network.
- **Text + images + video.** Chat/completions and ComfyUI-backed image generation,
  image editing, and image-to-video all ride the same `/v1` endpoint.

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

**2. Point Grid at engines you already run.** One small command per engine; they all
join the same network. Grid starts nothing — it advertises each endpoint and
heartbeats it:

```bash
# the Ollama already running on this Mac (one endpoint can serve several models)
grid provider start --network home \
  --endpoint-url http://localhost:11434/v1 \
  --model llama3 --model qwen2.5-coder

# the vLLM already running on your GPU box
grid provider start --network http://192.168.1.25:8090 \
  --endpoint-url http://localhost:8000/v1 \
  --model mistral-large

# the LM Studio already running on your laptop
grid provider start --network http://192.168.1.25:8090 \
  --endpoint-url http://localhost:1234/v1 \
  --model gemma2
```

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

## Grid can also host models for you

No engine running on a box yet? Grid can launch `llama.cpp` (text) and ComfyUI (media)
for you, so a fresh machine becomes a provider with one command.

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
