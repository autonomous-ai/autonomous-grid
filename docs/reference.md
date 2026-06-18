# Grid reference

The deep reference — engine reachability, the full `grid join` flag set, the built-in
engines, media bundles, the raw HTTP API, and troubleshooting. For the command contract
and the common path, see **[docs/cli.md](cli.md)**; for the what/why and a quickstart, see
the [README](../README.md); for how requests flow through the code, see
[ARCHITECTURE.md](ARCHITECTURE.md).

Local state is stored under `~/.grid` unless the `GRID_HOME` environment variable is set.

---

## Joining an engine you already run

Ollama, vLLM, LM Studio, MLX, and llama.cpp all expose an OpenAI-compatible `/v1`
endpoint. `grid join --at <url>` advertises that existing engine without launching
anything new. Run it on the engine's machine and use **that machine's LAN IP** (not
`localhost`) so the grid server can reach it. `-m/--model` is repeatable, so one endpoint
can advertise several model names:

```bash
grid join home --at http://192.168.1.10:11434/v1 -m llama3 -m qwen2.5-coder
```

> **`--at <url>` must be reachable from the grid server**, which proxies requests from
> its own process:
> - Use the engine machine's LAN IP, not `localhost` (localhost only works if the engine
>   runs on the grid-server machine). `--advertise-host` does not apply to `--at` — put
>   the host directly in the URL.
> - The engine must listen on the LAN: Ollama `OLLAMA_HOST=0.0.0.0`; LM Studio "Serve on
>   Local Network"; vLLM `--host 0.0.0.0`.
> - Verify from the grid-server machine: `curl http://192.168.1.10:11434/v1/models`.

The advertised model name is what apps request, and it is forwarded verbatim to your
engine — so advertise the names your engine already recognizes. Use `--advertise-as` only
when you want a different routing name (provide it once per `-m/--model`):

```bash
grid join home --at http://192.168.1.50:8081/v1 -m my-real-model-name --advertise-as qwen-local
```

`grid join` with no `--at`/`--serve`/`--media` auto-detects engines already running on
this machine (Ollama, LM Studio, vLLM, MLX, llama.cpp, ComfyUI) and joins them; in a
non-interactive shell pass `--all` or `--engine <kind>`.

### `grid join` flags

| Flag | Purpose |
| --- | --- |
| `--at URL` | Advertise an existing OpenAI-compatible engine (no local launch). |
| `-m, --model NAME` | Model to advertise (repeatable with `--at`). |
| `--serve MODEL` | Launch the built-in llama.cpp engine for one model, then join it. |
| `--media [--bundle B]` | Join this box as a ComfyUI media engine (bundle repeatable). |
| `--advertise-as NAME` | Routing-name override; once per `-m/--model`. |
| `--name ID` | Engine id (used by `grid leave --engine <id>`). |
| `--all` / `--engine KIND` | Join every / one detected engine (for non-interactive shells). |
| `--advertise-host HOST` | Host other machines should reach a Grid-launched engine on. |
| `--endpoint-port N` | Built-in llama-server port (default 8081). |
| `--heartbeat-interval S` | Seconds between heartbeats (default 15). |
| `--ctx-size / --n-predict / --parallel / --temp / --flash-attn / --reasoning-budget` | Passed to a Grid-launched llama-server. |

Inspect and stop what's live:

```bash
grid engines home               # engines joined to the grid
grid models home --verbose      # models + which engine serves each
grid leave home --engine <id>   # or --all
```

---

## Built-in engines

No engine on a box yet? Grid ships two open-source engines it sets up for you.

**Text (llama.cpp):**

```bash
grid engine install llama.cpp                 # Homebrew (macOS) or pinned tarball (Linux NVIDIA)
grid engine install llama.cpp --from-source   # build locally (Metal on macOS, CUDA on NVIDIA)
grid catalog                                  # models Grid can pull
grid pull qwen36-35b-a3b-mtp                  # a catalog label, or any '<hf-repo>:<file>'
grid pull unsloth/Qwen3.6-35B-A3B-MTP-GGUF:Qwen3.6-35B-A3B-UD-IQ3_S.gguf
grid rm your-model.gguf --yes
grid join home --serve qwen36-35b-a3b-mtp     # launch llama.cpp for it, then join
```

- Apple Silicon macOS uses Homebrew's `llama.cpp` formula by default; `--from-source`
  builds a Metal backend locally.
- Linux NVIDIA hosts use a pinned tarball when available; `--from-source` builds locally
  with CUDA (`--target-sm sm_XX` to override the detected GPU).

**Media (ComfyUI):**

```bash
grid engine install comfyui
grid engine pull image_generation             # also: image_editing, i2v
grid join home --media --bundle image_generation
grid engine status                            # ComfyUI install + runtime status
grid engine start --detach                    # manage ComfyUI directly
grid engine stop
```

If `--bundle` is omitted, `grid join --media` advertises every installed bundle that
passes the host's VRAM gate, as these models:

```text
comfyui:image_generation
comfyui:image_editing
comfyui:i2v
```

---

## Using a grid from your app

```bash
eval "$(grid info --env)"
# export OPENAI_BASE_URL="http://192.168.1.25:8090/v1"
# export OPENAI_API_KEY="local-grid"   # ignored by the server; only for SDK compatibility
```

Smoke-test through the grid (these route through `grid_url`, not directly to an engine):

```bash
grid chat -m llama3 "hello"
grid image "a compact walnut desk beside a sunlit window"
grid edit "make the chair red" -i input.png
grid video "slow cinematic push in" -i input.png
```

---

## Direct HTTP API

Apps talk to a grid as a normal OpenAI endpoint at `grid_url/v1`. Media also has a raw SSE
API — progress events, then a result event whose `output_files` entries are base64-encoded
media. Get `grid_url` from `grid info`.

Image generation:

```bash
curl -N -H "Content-Type: application/json" \
  -X POST http://192.168.1.25:8090/v1/media/image/generate \
  -d '{ "prompt": "a compact walnut desk beside a sunlit window", "width": 720, "height": 720, "steps": 4 }'
```

Image editing:

```bash
IMAGE_BASE64="$(base64 -i input.png | tr -d '\n')"
curl -N -H "Content-Type: application/json" \
  -X POST http://192.168.1.25:8090/v1/media/image/edit \
  -d "{ \"prompt\": \"make the chair red\", \"steps\": 4, \"input_images\": [ { \"filename\": \"input.png\", \"content_base64\": \"${IMAGE_BASE64}\" } ] }"
```

Image-to-video:

```bash
IMAGE_BASE64="$(base64 -i input.png | tr -d '\n')"
curl -N -H "Content-Type: application/json" \
  -X POST http://192.168.1.25:8090/v1/media/video/i2v \
  -d "{ \"prompt\": \"slow cinematic push in\", \"duration\": \"5s\", \"aspect_ratio\": \"2:3\", \"input_image\": { \"filename\": \"input.png\", \"content_base64\": \"${IMAGE_BASE64}\" } }"
```

---

## Troubleshooting connections

All three come back from the grid's `/v1` endpoint:

| Error | Likely cause |
| --- | --- |
| `No active LAN engine for model 'X'` (Grid; `code: engine_unavailable`) | No live engine advertises exactly `X`. Check `grid models` / `grid engines`; the requested name must match an advertised model exactly (case-sensitive). |
| `Engine request failed: All connection attempts failed` (Grid; `code: engine_error`) | The grid server can't open a connection to the engine's `endpoint_url`. The URL points at `localhost`/an unreachable host, the engine listens only on loopback, or a firewall blocks the port. |
| `model 'X' not found`, `type: not_found_error`, no Grid `code` | The request *reached* an engine, but that engine doesn't serve `X`. Usually a `localhost` endpoint that resolved to the wrong machine, or an advertised name the engine doesn't recognize (Grid forwards `model` verbatim). |

Make the engine listen on the LAN (not just loopback): Ollama → `OLLAMA_HOST=0.0.0.0`
then restart; LM Studio → enable "Serve on Local Network"; vLLM → `--host 0.0.0.0`.

Two checks that resolve most issues:

```bash
# From the GRID-SERVER machine — proves reachability and lists the names the engine serves:
curl http://<engine-ip>:<port>/v1/models

# What each engine advertises. Note: this only proves the engine registered + heartbeats —
# it does NOT verify reachability or model names.
grid engines home
```
