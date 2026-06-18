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

[**Quickstart**](#quickstart) · [How it works](#how-it-works) · [Grid vs. running an engine directly](#grid-vs-running-an-engine-directly) · [CLI reference](docs/cli.md) · [Contributing](#contributing)

<img src="docs/home-grid.svg" alt="Grid sits above your engines — apps call one endpoint, Grid routes each request to whichever machine serves the model" width="860">

</div>

## Grid is / Grid is not

Grid is a **layer above** your inference engines — the same way an API gateway sits above your
services or Tailscale sits above your network. It is not another engine.

| ✅ Grid **is** | ❌ Grid is **not** |
|---|---|
| an orchestration layer that **routes** to the engines you already run | an inference engine or model runtime — **it runs no models of its own** |
| **one OpenAI-compatible endpoint** for every engine on your LAN | a replacement for Ollama / vLLM / LM Studio / MLX / llama.cpp / ComfyUI — it sits **above** them |
| a live registry that **auto-discovers** the models your machines serve | a cloud service — nothing leaves your network |
| private by default (LAN-only, no auth) | a new API your apps must learn — it's just OpenAI |

## Quickstart

> Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv tool install -e . --force        # installs the `grid` command
```

```bash
grid up                             # 1. bring your grid online (one endpoint)
grid join                           # 2. auto-detect & join the engines on this machine
grid models                         # 3. every live model across every machine you've joined
grid chat -m <model> "hello"        #    talk to any of them through the one endpoint
```

Point **any OpenAI SDK** at it — no new client, no migration:

```bash
eval "$(grid info --env)"           # exports OPENAI_BASE_URL + OPENAI_API_KEY
```

```python
from openai import OpenAI

client = OpenAI()                   # reads OPENAI_BASE_URL + OPENAI_API_KEY
client.chat.completions.create(
    model="<model>",                # any model `grid models` lists
    messages=[{"role": "user", "content": "hello"}],
)
```

**The aha:** every model on every machine you own, behind one base URL — and Grid never touched your engines.

<details>
<summary><b>Join engines on other machines</b></summary>

<br>

Run `grid join <grid-url>` on each machine. Auto-detect handles the common case; name a specific endpoint with `--at`:

```bash
# on a GPU box running vLLM
grid join http://192.168.1.25:8090 --at http://localhost:8000/v1 -m devstral-small-2 --name gpu-4090
```

The engine must be reachable from the machine running the grid — bind it to the LAN
(Ollama `OLLAMA_HOST=0.0.0.0`, LM Studio "Serve on Local Network", vLLM `--host 0.0.0.0`).
More in [docs/reference.md](docs/reference.md).

</details>

<details>
<summary><b>No engine yet? Grid can set one up for you</b></summary>

<br>

Grid doesn't ship a runtime, but it knows how to install and join two open-source engines so a
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

Grid sits **above** your engines (see the diagram). The machines you own are the inference engines,
your grid is the one address everything talks through, and your apps draw from it.

- **the grid** — one private endpoint that routes each request to a machine serving that model. Bring it up with `grid up`.
- **engines** — the tools you already run (Ollama, vLLM, LM Studio, MLX, llama.cpp, ComfyUI), each joined with `grid join`. Grid advertises them and heartbeats them; it never restarts or replaces them.
- **apps** — anything that speaks the OpenAI API, pointed at the URL `grid info` prints. Text on `/v1/chat`, images and video on `/v1/media`.

Full request flow in **[ARCHITECTURE.md](ARCHITECTURE.md)**; the complete command surface in **[docs/cli.md](docs/cli.md)**.

## Grid vs. running an engine directly

Grid isn't competing with Ollama or vLLM — it's the layer that makes all of them answer at one address.

|  | **Grid** | A single engine (e.g. Ollama) | A cloud aggregator (e.g. OpenRouter) |
|---|:---:|:---:|:---:|
| Runs inference itself | **No — routes to yours** | Yes | Yes (in the cloud) |
| Unifies the engines you already run | **Yes** | No | No |
| Pools models across many machines | **Yes** | No | — |
| One OpenAI-compatible endpoint | Yes | Yes (one engine) | Yes |
| Stays on your LAN / fully private | **Yes** | Yes | No |
| Uses hardware you already own | **Yes** | Yes (one box) | No |

## Contributing

Grid is small and readable by design — clone to PR in minutes.

```bash
git clone https://github.com/autonomous-ai/autonomous-grid
cd autonomous-grid
uv sync --extra dev
uv run --extra dev pytest
```

Good first PRs: add a model to the catalog (`grid/models/catalog.py`) or a media bundle
(`grid/models/media_bundles.py`). Start with **[CONTRIBUTING.md](CONTRIBUTING.md)** and
**[ARCHITECTURE.md](ARCHITECTURE.md)**.

Local state lives under `~/.grid` (override with `GRID_HOME`).

## License

MIT — see [LICENSE](LICENSE).
