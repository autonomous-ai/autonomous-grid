# Contributing to Grid

Thanks for considering a contribution! Grid is a small, readable codebase by design —
this guide gets you from clone to merged PR quickly. Skim
[ARCHITECTURE.md](ARCHITECTURE.md) first; it's the map.

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)

## Setup

```bash
git clone <your-fork-url>
cd autonomous-grid
uv sync --extra dev          # create the env with dev (test) dependencies
uv tool install -e . --force # optional: put the `grid` command on your PATH
```

## Running tests

```bash
uv run --extra dev pytest
```

> The `--extra dev` is required: plain `uv run pytest` fails because `pytest` lives in
> the optional `dev` dependency group and isn't installed otherwise.

All tests live in `tests/`. Please keep the suite green and add a test with any
behavior change — `tests/test_lan_cli.py` shows the patterns (FastAPI `TestClient`,
monkeypatching subprocess launches).

## Project layout

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full component map and request-flow
walkthroughs. In short:

- `grid/server.py` — the signaling server / OpenAI-compatible proxy
- `grid/cli.py` — argument parsing + every `cmd_*` command handler
- `grid/provider/`, `grid/models/`, `grid/engine/`, `grid/system/` — provider media,
  model management, engine lifecycle, and host/GPU info

## Common contributions

These are well-bounded and make great first PRs:

**Add a model to the catalog** — edit `grid/models/catalog.py`. The catalog is
platform-aware (Apple Silicon vs NVIDIA); add the entry and it shows up in
`grid models list --catalog` and `grid models pull <name>`.

**Add a media bundle** — edit `grid/models/media_bundles.py`. A bundle is a tuple of
`FileSpec(hf_repo, hf_path, subdir)` entries plus a `comfyui:*` capability name. Wire
any new ComfyUI workflow JSON into `grid/provider/media_handler.py`.

**Add a CLI command** — in `grid/cli.py`: add a subparser in `build_parser()`, write a
`cmd_<name>(args) -> int` handler, and connect them with
`parser.set_defaults(handler=cmd_<name>)`. Existing `cmd_*` functions are the template.

**Tune provider gating** — `grid/provider/media_gating.py` decides which media bundles a
host advertises based on VRAM.

## Code style

- Match the surrounding code: type hints, `from __future__ import annotations`, module
  docstrings explaining *why*.
- Prefer small, focused functions; keep the CLI handlers thin and push logic into the
  relevant `grid/` module.
- Keep the LAN-only, unauthenticated assumptions intact (see ARCHITECTURE.md). Don't add
  network calls that leave the LAN or features that require auth.
- Vendored files (annotated in their docstrings) should keep edits bracketed and minimal.

## Commits & pull requests

- Write clear, scoped commits; one logical change per commit.
- Run `uv run --extra dev pytest` before pushing.
- Open a PR against `main` describing what changed and why, with a test for any behavior
  change. Link the issue it addresses if there is one.
