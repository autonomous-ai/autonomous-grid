<div align="center">

# ⚡ Grid

### One private endpoint for every AI engine you run.

Point Grid at the **Ollama, vLLM, LM Studio, MLX, and llama.cpp** boxes you already have.
It turns them into a single **OpenAI-compatible API** on your network — text, images, and video.

[![CI](https://github.com/autonomous-ai/autonomous-grid/actions/workflows/ci.yml/badge.svg)](https://github.com/autonomous-ai/autonomous-grid/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](#contributing)

[**Quickstart**](#quickstart) · [How it works](#how-it-works) · [CLI reference](docs/cli.md) · [Contributing](#contributing)

<img src="docs/home-grid.svg" alt="Your Home Grid — your machines feed one private endpoint; your apps draw from it" width="860">

</div>

## Why Grid

- ⚡ **One endpoint, every engine.** Your app points at a single `OPENAI_BASE_URL`; Grid routes each request to whichever machine serves that model.
- 🔌 **It aggregates, it doesn't replace.** Grid has no inference engine of its own — it advertises the Ollama / vLLM / LM Studio / MLX / llama.cpp servers you already run. Stop Grid and they're untouched.
- 🖼️ **Text, images, and video.** Chat/completions plus ComfyUI-backed image generation, editing, and image-to-video — all on the same `/v1`.
- 🔒 **Private by default.** LAN-only, no auth, in-memory registry. Nothing phones home, nothing leaves your network.
- 🔋 **Batteries included.** No engine on a box yet? Grid sets up `llama.cpp` (text) and ComfyUI (media) for you.
- 🪶 **Tiny and readable.** Pure Python, OpenAI-compatible passthrough — easy to read, easy to contribute to.

## Quickstart

> Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv tool install -e . --force        # installs the `grid` command
```

```bash
grid up                             # 1. bring your grid online
grid join                           # 2. auto-detect & join local engines (Ollama, LM Studio, MLX, vLLM…)
grid models                         # 3. list every live model across your machines
grid chat -m <model> "hello"        #    talk to any of them through one endpoint
```

Point **any OpenAI SDK** at it:

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

**That's the aha:** every model on every machine, one base URL, nothing migrated.

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
<summary><b>No engine yet? Grid ships with two</b></summary>

<br>

A bare machine goes from zero to a working endpoint with a couple of commands — Grid installs
the engine on first use and joins it like any other.

```bash
grid engine install llama.cpp           # built-in text engine
grid pull qwen36-35b-a3b-mtp            # see `grid catalog`, or any HF GGUF
grid join --serve qwen36-35b-a3b-mtp

grid engine install comfyui             # built-in media engine
grid engine pull image_generation       # also: image_editing, i2v
grid join --media --bundle image_generation
grid image "a compact walnut desk beside a sunlit window"
```

</details>

## How it works

The machines you own are the generators, your grid is the shared supply on one address, and
your apps are the homes that draw from it (see the diagram above).

- **the grid** — one private endpoint that routes each request to a machine serving that model. Bring it up with `grid up`.
- **engines** — the tools you already run (Ollama, vLLM, LM Studio, MLX, llama.cpp, ComfyUI), each joined with `grid join`.
- **apps** — anything that speaks the OpenAI API, pointed at the URL `grid info` prints.

Full request flow in **[ARCHITECTURE.md](ARCHITECTURE.md)**; the complete command surface in **[docs/cli.md](docs/cli.md)**.

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
