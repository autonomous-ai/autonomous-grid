<div align="center">

# Grid

### The orchestration layer for local AI.

Grid unifies the inference engines you already run — **Ollama, vLLM, LM Studio, MLX, llama.cpp, ComfyUI** —
behind **one OpenAI-compatible endpoint** on your LAN.
**It runs no models of its own. It routes.**

[![CI](https://github.com/autonomous-ai/autonomous-grid/actions/workflows/ci.yml/badge.svg)](https://github.com/autonomous-ai/autonomous-grid/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](#contributing)

[**Quickstart**](#quickstart) · [How it works](#how-it-works) · [CLI reference](docs/cli.md) · [Contributing](#contributing)

<img src="docs/home-grid.svg" alt="Your Home Grid sits above your engines — apps call one endpoint, Grid routes each request to whichever machine serves the model" width="860">

</div>

## Quickstart

Turn the machines you already own into one AI endpoint — in four steps.
Most homes have idle compute scattered across a few boxes; Grid pools it.

> Install the CLI on each machine: Python 3.11+ and [uv](https://docs.astral.sh/uv/), then
> `uv tool install -e . --force`.

**1 · Create your grid** — on any one machine:

```bash
grid up
# grid=home
# grid_url=http://192.168.1.25:8090      ← the one address everything uses
```

**2 · Add your Mac** — run this on the Mac; Grid finds the engine it's running (MLX, Ollama, or LM Studio) and joins it:

```bash
grid join http://192.168.1.25:8090
# joined  mac-studio · MLX · gemma4-31b
```

**3 · Add your NVIDIA box** — run this on the GPU machine; Grid finds vLLM or llama.cpp:

```bash
grid join http://192.168.1.25:8090
# joined  gpu-4090 · vLLM · devstral-small-2
```

Two different machines, two different frameworks — now one endpoint serves every model on both:

```bash
grid models
# gemma4-31b        mac-studio   (MLX)
# devstral-small-2  gpu-4090     (vLLM)
```

**4 · Point your apps at the grid.** Grab the endpoint, then wire up any OpenAI client.
Pick a model from `grid models` — Grid routes it to whichever machine serves it.

```bash
grid info
# grid_url         http://192.168.1.25:8090
# openai_base_url  http://192.168.1.25:8090/v1
# api key          local-grid     (any value works — auth is off on your LAN)
```

**OpenClaw** — add Grid as a provider in `~/.openclaw/openclaw.json` ([docs](https://docs.openclaw.ai/concepts/model-providers)):

```json
{
  "agents": { "defaults": { "model": { "primary": "grid/devstral-small-2" } } },
  "models": {
    "providers": {
      "grid": {
        "baseUrl": "http://192.168.1.25:8090/v1",
        "apiKey": "local-grid",
        "api": "openai-completions",
        "models": [{ "id": "devstral-small-2", "name": "Devstral (via Grid)" }]
      }
    }
  }
}
```

**Hermes** — set the endpoint in `~/.hermes/config.yaml` ([docs](https://hermes-agent.nousresearch.com/docs/user-guide/configuration)):

```yaml
model:
  provider: custom
  default: devstral-small-2
  base_url: http://192.168.1.25:8090/v1
```

```bash
echo 'OPENAI_API_KEY=local-grid' >> ~/.hermes/.env     # any value; Grid ignores it
```

**Your own app** — point any OpenAI SDK at the grid:

```python
from openai import OpenAI

client = OpenAI(base_url="http://192.168.1.25:8090/v1", api_key="local-grid")
client.chat.completions.create(
    model="devstral-small-2",                # Grid routes this to the 4090 box automatically
    messages=[{"role": "user", "content": "hello"}],
)
```

**The aha:** your Mac's MLX model and your GPU box's vLLM model now answer at one address, behind
one API key — routed automatically. Add another machine with `grid join`, and every app instantly
has more models and more capacity. Nothing migrated; your engines run exactly as before.

<details>
<summary><b>No engine on a machine yet? Grid can set one up</b></summary>

<br>

Grid doesn't ship a runtime, but it knows how to install and join two open-source engines, so a
bare machine can join a grid without Ollama, vLLM, or LM Studio:

```bash
grid engine install llama.cpp           # default text engine
grid pull qwen36-35b-a3b-mtp            # see `grid catalog`, or any HF GGUF
grid join --serve qwen36-35b-a3b-mtp

grid engine install comfyui             # default media engine (images + video)
grid engine pull image_generation       # also: image_editing, i2v
grid join --media --bundle image_generation
grid image "a compact walnut desk beside a sunlit window"
```

</details>

## How it works

Grid sits **above** your engines — the same way an API gateway sits above your services or
Tailscale sits above your network (see the diagram). The machines you own are the inference
engines, your grid is the one address everything talks through, and your apps draw from it.

- **the grid** — one private endpoint that routes each request to a machine serving that model. Create it with `grid up`.
- **engines** — the tools you already run (Ollama, vLLM, LM Studio, MLX, llama.cpp, ComfyUI). Run `grid join <grid-url>` on a machine and Grid advertises its engines and heartbeats them; it never restarts or replaces them.
- **apps** — anything that speaks the OpenAI API, pointed at the URL `grid info` prints. Text on `/v1/chat`, images and video on `/v1/media`.

Full request flow in **[ARCHITECTURE.md](ARCHITECTURE.md)**; the complete command surface in **[docs/cli.md](docs/cli.md)**.

## Contributing

Grid is small and readable by design — clone to PR in minutes.

```bash
git clone https://github.com/autonomous-ai/autonomous-grid
cd autonomous-grid
uv sync --extra dev
uv run --extra dev pytest
```

Good first PRs: add a model to the catalog (`models/catalog.py`) or a media bundle
(`models/media_bundles.py`). Start with **[CONTRIBUTING.md](CONTRIBUTING.md)** and
**[ARCHITECTURE.md](ARCHITECTURE.md)**.

Local state lives under `~/.grid` (override with `GRID_HOME`).

## License

MIT — see [LICENSE](LICENSE).
