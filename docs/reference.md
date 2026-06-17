# Grid command reference

The complete CLI surface. For the what/why and a 60-second quickstart, see the
[README](../README.md); for how requests flow through the code, see
[ARCHITECTURE.md](../ARCHITECTURE.md).

Local state is stored under `~/.grid` unless the `GRID_HOME` environment variable is set.

---

## Networks

A network is a signaling server: an OpenAI-compatible proxy plus a live registry of
providers. Run it on the device that should host the endpoint.

```bash
grid network create home --port 8090
```

This starts a local FastAPI signaling server bound to `0.0.0.0` and prints a LAN
signaling URL:

```text
signaling_url=http://192.168.1.25:8090
```

Any device can use that signaling URL directly wherever a command accepts `--network`.

```bash
grid network list                 # saved networks
grid network status home          # show signaling URL + state
grid network start home           # start a previously-created managed server
grid network stop home            # stop a local managed server
```

---

## Providers

A provider registers an engine into a network and heartbeats it until you stop it
(Ctrl-C unregisters).

### Point at an engine you already run (recommended)

Ollama, vLLM, LM Studio, and llama.cpp all expose an OpenAI-compatible `/v1` endpoint.
`--endpoint-url` advertises that existing server without launching anything new.
`--model` is repeatable, so one endpoint can advertise several model names:

```bash
grid provider start --network home \
  --endpoint-url http://localhost:11434/v1 \
  --model llama3 --model qwen2.5-coder
```

The advertised model name is what consumers request, and it is forwarded verbatim to
your engine — so advertise the names your engine already recognizes. Use
`--advertise-as` only when you want a different routing name (provide it once per
`--model`):

```bash
grid provider start --network home \
  --endpoint-url http://192.168.1.50:8081/v1 \
  --model my-real-model-name \
  --advertise-as qwen-local
```

### Let Grid host a local model for you

With `--model` and no `--endpoint-url`, Grid launches a local `llama-server` for a
single GGUF model under `~/.grid/models/`:

```bash
grid provider start --network home --model Qwen3.5-2B-UD-IQ2_M.gguf
```

From another LAN device, pass the signaling URL directly:

```bash
grid provider start --network http://192.168.1.25:8090 --model Qwen3.5-2B-UD-IQ2_M.gguf
```

### Provider with media enabled

```bash
grid provider start \
  --network home \
  --model Qwen3.5-2B-UD-IQ2_M.gguf \
  --enable-media \
  --media-bundle image_generation \
  --media-bundle image_editing \
  --media-bundle i2v
```

Media-only provider (no text model):

```bash
grid provider start --network home --enable-media --media-bundle image_editing
```

If `--media-bundle` is omitted, `--enable-media` advertises every installed bundle that
passes the host memory gate. The provider starts ComfyUI if needed, starts a
provider-local media API, and advertises these LAN models:

```text
comfyui:image_generation
comfyui:image_editing
comfyui:i2v
```

### Useful provider flags

| Flag | Purpose |
| --- | --- |
| `--endpoint-url URL` | Advertise an existing OpenAI-compatible endpoint (no local launch). |
| `--model NAME` | Model to advertise (repeatable with `--endpoint-url`; exactly one for local launch). |
| `--advertise-as NAME` | Routing name override; once per `--model`. |
| `--endpoint-port N` | Local llama-server port (default 8081). |
| `--advertise-host HOST` | Host other devices should reach this provider on. |
| `--enable-media` | Advertise ComfyUI media capabilities. |
| `--media-bundle NAME` | `image_generation` \| `image_editing` \| `i2v` (repeatable). |
| `--heartbeat-interval S` | Seconds between heartbeats (default 15). |
| `--ctx-size / --n-predict / --parallel / --temp / --flash-attn / --reasoning-budget` | Passed to a Grid-launched llama-server. |

List active providers:

```bash
grid provider list --network home
grid provider list --network home --model llama3
```

---

## Hosting text models (llama.cpp)

Provider nodes that don't point at an existing endpoint need `llama-server` and at
least one GGUF model under `~/.grid/models/`.

```bash
grid llama.cpp install                 # install or upgrade llama-server
grid llama.cpp install --from-source   # build locally (Metal on macOS, CUDA on NVIDIA)
```

- Apple Silicon macOS uses Homebrew's `llama.cpp` formula by default; `--from-source`
  builds a Metal backend locally.
- Linux NVIDIA hosts use the pinned-tarball path when available; `--from-source` builds
  locally with CUDA.

Manage local models:

```bash
grid models list                       # local GGUF files
grid models list --catalog             # platform-aware catalog
grid models pull qwen36-35b-a3b-mtp    # Apple Silicon catalog model
grid models pull qwen36-27b-mtp        # NVIDIA CUDA catalog model
grid models pull unsloth/Qwen3.6-35B-A3B-MTP-GGUF:Qwen3.6-35B-A3B-UD-IQ3_S.gguf  # any HF GGUF
grid models rm your-model.gguf --yes
```

---

## Media (ComfyUI)

```bash
grid media install                     # install ComfyUI + media runtime deps
grid media pull image_generation       # download a bundle
grid media pull image_editing
grid media pull i2v
grid media status                      # install + runtime status
```

Manage ComfyUI directly when needed:

```bash
grid media start --detach
grid media stop
```

---

## Consumers

Consumers use the signaling server as an OpenAI-compatible base URL:

```bash
grid consumer env --network home
# or from another device:
grid consumer env --network http://192.168.1.25:8090
```

This prints:

```bash
export OPENAI_BASE_URL="http://192.168.1.25:8090/v1"
export OPENAI_API_KEY="local-lan"
```

The API key is only for OpenAI SDK compatibility; the server ignores authorization headers.

### Smoke-test through the CLI

```bash
grid request chat --network home --model llama3 --message "hello"

grid request media image-generate \
  --network home \
  --prompt "a compact walnut desk beside a sunlit window"

grid request media image-edit \
  --network home \
  --prompt "make the chair red" \
  --image input.png

grid request media i2v \
  --network home \
  --prompt "slow cinematic push in" \
  --image input.png
```

---

## Direct media HTTP API

You can call the signaling server directly without the CLI. Get the signaling URL with
`grid network status home`, then POST to `/v1/media/*`. Responses are SSE streams:
progress events followed by a result event whose `output_files` entries contain
base64-encoded media.

Image generation:

```bash
curl -N \
  -H "Content-Type: application/json" \
  -X POST http://192.168.1.25:8090/v1/media/image/generate \
  -d '{
    "prompt": "a compact walnut desk beside a sunlit window",
    "width": 720,
    "height": 720,
    "steps": 4
  }'
```

Image editing:

```bash
IMAGE_BASE64="$(base64 -i input.png | tr -d '\n')"

curl -N \
  -H "Content-Type: application/json" \
  -X POST http://192.168.1.25:8090/v1/media/image/edit \
  -d "{
    \"prompt\": \"make the chair red\",
    \"steps\": 4,
    \"input_images\": [
      { \"filename\": \"input.png\", \"content_base64\": \"${IMAGE_BASE64}\" }
    ]
  }"
```

Image-to-video:

```bash
IMAGE_BASE64="$(base64 -i input.png | tr -d '\n')"

curl -N \
  -H "Content-Type: application/json" \
  -X POST http://192.168.1.25:8090/v1/media/video/i2v \
  -d "{
    \"prompt\": \"slow cinematic push in\",
    \"duration\": \"5s\",
    \"aspect_ratio\": \"2:3\",
    \"input_image\": { \"filename\": \"input.png\", \"content_base64\": \"${IMAGE_BASE64}\" }
  }"
```

---

## Quick command index

```bash
grid network create|start|stop|status|list
grid provider start|list
grid llama.cpp install [--from-source]
grid models list|pull|rm
grid media install|pull|status|start|stop
grid consumer env
grid request chat
grid request media image-generate|image-edit|i2v
```
