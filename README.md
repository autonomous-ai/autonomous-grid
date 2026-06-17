## Install For Development

```bash
uv run grid --version
```

or install the editable CLI:

```bash
uv tool install -e . --force
```

## Create A LAN Network

Run this on the device that will host the signaling server:

```bash
grid network create home --port 8090
```

The command starts a local FastAPI signaling server bound to `0.0.0.0` and prints a LAN signaling URL such as:

```text
signaling_url=http://192.168.1.25:8090
```

Other devices can use that signaling URL directly wherever a command accepts `--network`.

## Start A Provider

Provider nodes need `llama-server` and at least one GGUF model under `~/.grid/models/`.

Install or upgrade `llama-server`:

```bash
grid llama.cpp install
grid llama.cpp install --from-source
```

The default behavior follows the referenced Grid CLI:

- Apple Silicon macOS uses Homebrew's `llama.cpp` formula by default; `--from-source` builds a Metal backend locally.
- Linux NVIDIA hosts use the pinned-tarball path when available; `--from-source` builds locally with CUDA.

List local models and the platform-aware catalog:

```bash
grid models list
grid models list --catalog
```

Pull the Apple Silicon catalog model:

```bash
grid models pull qwen36-35b-a3b-mtp
```

Pull the NVIDIA CUDA catalog model:

```bash
grid models pull qwen36-27b-mtp
```

Pull an arbitrary Hugging Face GGUF file:

```bash
grid models pull unsloth/Qwen3.6-35B-A3B-MTP-GGUF:Qwen3.6-35B-A3B-UD-IQ3_S.gguf
```

Remove a local model:

```bash
grid models rm your-model.gguf --yes
```

## Optional Media Provider Setup

```bash
grid media install
```

Download one or more media bundles:

```bash
grid media pull image_generation
grid media pull image_editing
grid media pull i2v
grid media status
```

You can manage ComfyUI directly when needed:

```bash
grid media start --detach
grid media stop
```

Start a provider. By default this starts local `llama-server` on `0.0.0.0:8081`, waits for it to become ready, then advertises it on the LAN signaling server:

```bash
grid provider start \
  --network home \
  --model Qwen3.5-2B-UD-IQ2_M.gguf
```

Advertise a shorter routing name for a local GGUF model:

```bash
grid provider start \
  --network home \
  --model your-model.gguf \
  --advertise-as your-model
```

From another LAN device, pass the signaling URL directly:

```bash
grid provider start \
  --network http://192.168.1.25:8090 \
  --model Qwen3.5-2B-UD-IQ2_M.gguf
```

Start the provider with media enabled:

```bash
grid provider start \
  --network home \
  --model Qwen3.5-2B-UD-IQ2_M.gguf \
  --enable-media \
  --media-bundle image_generation \
  --media-bundle image_editing \
  --media-bundle i2v
```

Start a media-only provider without a llama-server/text model:

```bash
grid provider start \
  --network home \
  --enable-media \
  --media-bundle image_editing
```

If `--media-bundle` is omitted, `--enable-media` advertises every installed bundle that passes the host memory gate. The provider starts ComfyUI if needed, starts a provider-local media API, and advertises these LAN models:

```text
comfyui:image_generation
comfyui:image_editing
comfyui:i2v
```

If you already run an OpenAI-compatible provider yourself, pass `--endpoint-url` and the CLI will only advertise and heartbeat that existing endpoint:

```bash
grid provider start \
  --network home \
  --model qwen-local \
  --endpoint-url http://192.168.1.50:8081/v1
```

## Use A Consumer

Consumers can use the signaling server as an OpenAI-compatible base URL:

```bash
grid consumer env --network home
```

From another LAN device:

```bash
grid consumer env --network http://192.168.1.25:8090
```

This prints:

```bash
export OPENAI_BASE_URL="http://192.168.1.25:8090/v1"
export OPENAI_API_KEY="local-lan"
```

The API key value is only for OpenAI SDK compatibility; the server ignores authorization headers.

Smoke-test a request:

```bash
grid request chat --network home --model qwen-local --message "hello"
```

Send media requests:

```bash
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

## Direct Media HTTP API

You can call the LAN signaling server directly without the CLI. First get the signaling URL:

```bash
grid network status home
```

Use the printed `signaling_url`, for example `http://192.168.1.25:8090`.

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
      {
        \"filename\": \"input.png\",
        \"content_base64\": \"${IMAGE_BASE64}\"
      }
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
    \"input_image\": {
      \"filename\": \"input.png\",
      \"content_base64\": \"${IMAGE_BASE64}\"
    }
  }"
```

The response is an SSE stream. Progress events are followed by a result event whose `output_files` entries contain base64-encoded media.

## Useful Commands

```bash
grid network list
grid network status home
grid provider list --network home
grid media status
grid network stop home
```

Local state is stored under `~/.grid` unless `GRID_HOME` is set.
