# Contributing to Grid

Thanks for considering a contribution! Grid is a small, readable codebase by design —
this guide gets you from clone to merged PR quickly. Skim
[ARCHITECTURE.md](ARCHITECTURE.md) first; it's the map. The user-facing command contract
is [cli.md](cli.md).

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
behavior change — `tests/test_local_cli.py` shows the patterns (FastAPI `TestClient`,
monkeypatching subprocess launches).

## Project layout

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full component map and request-flow
walkthroughs. In short:

- `server.py` — the grid server / OpenAI-compatible proxy
- `cli/` — the CLI, split by command group (`parser.py` builds the command tree;
  `grid.py` holds `up`/`down`/`info`, `provider.py` holds the `join`/`leave` engine
  lifecycle, `engine.py` holds built-in-engine setup, `models.py` and `request.py` the rest)
- `provider/`, `models/`, `engine/`, `system/` — engine-side media, model management,
  built-in engine lifecycle, and host/GPU detection

## Common contributions

These are well-bounded and make great first PRs:

**Add a model to the catalog** — edit `models/catalog.py`. The catalog is
platform-aware (Apple Silicon vs NVIDIA); add the entry and it shows up in
`grid catalog` and `grid pull <name>`.

**Add a media bundle** — edit `models/media_bundles.py`. A bundle is a tuple of
`FileSpec(hf_repo, hf_path, subdir)` entries plus a `comfyui:*` capability name. Wire
any new ComfyUI workflow JSON into `provider/media_handler.py`.

**Add a CLI command** — add the subparser in `cli/parser.py`'s `build_parser()` (keep it
in sync with `docs/cli.md`), write a `cmd_<name>(args) -> int` handler in the matching
group module (e.g. `cli/grid.py`), and connect them with `set_defaults(handler=cmd_<name>)`.
Re-export the handler from `cli/__init__.py`. Existing `cmd_*` functions are the template.

**Tune engine gating** — `provider/media_gating.py` decides which media bundles a host
advertises based on VRAM.

## Vocabulary

User-facing output and docs use **grid / engine / app** (see `docs/cli.md`). Avoid
`provider`, `consumer`, `signaling`, and `network` as product nouns — they only survive
as implementation details (`node` in the registry, the `provider/` package name).

## Code style

- Match the surrounding code: type hints, `from __future__ import annotations`, module
  docstrings explaining *why*.
- Prefer small, focused functions; keep the CLI handlers thin and push logic into the
  relevant top-level module or package.
- Keep the local-only, unauthenticated assumptions intact (see ARCHITECTURE.md). Don't add
  network calls that leave the local or features that require auth.
- Vendored files (annotated in their docstrings) should keep edits bracketed and minimal.

## Commits & pull requests

- Write clear, scoped commits; one logical change per commit.
- Run `uv run --extra dev pytest` before pushing.
- Open a PR against `main` describing what changed and why, with a test for any behavior
  change. Link the issue it addresses if there is one.
