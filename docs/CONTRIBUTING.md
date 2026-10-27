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
uv run --extra dev pytest -n auto   # one worker per core, as CI runs it; drop -n auto to run serially
```

> The `--extra dev` is required: plain `uv run pytest` fails because `pytest` lives in
> the optional `dev` dependency group and isn't installed otherwise.

All tests live in `tests/`. Please keep the suite green and add a test with any
behavior change — `tests/test_local_cli.py` shows the patterns (FastAPI `TestClient`,
monkeypatching subprocess launches).

## Project layout

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full component map and request-flow
walkthroughs. In short:

- `cli/` — the CLI, split by command group. `parser.py` builds the command tree and
  `dispatch.py` sends each command to its mode's handler. `grid.py` holds `start`/`stop`/`info`,
  `provider.py` the `join`/`leave` engine lifecycle, `engine.py` built-in-engine setup, and
  `models.py` and `request.py` the rest; the `remote_*.py` modules are the remote-mode handlers.
- `local/` — local mode: the grid server and OpenAI-compatible proxy (`local/server.py`) and the
  engine-side media server
- `remote/` — remote mode: the control-plane and relay clients, and the serve child that serves
  a remote grid (`remote/serve.py`)
- `shared/` — what both modes use: the model catalog and media bundles (`shared/models/`),
  engine-side media (`shared/media/`), built-in engines (`shared/engine/`), host and GPU
  detection (`shared/system/`), and the run records
- `train/` — `grid train`

## Common contributions

These are well-bounded and make great first PRs:

**Add a model to the catalog** — edit `shared/models/catalog.py`. The catalog is
platform-aware (Apple Silicon vs NVIDIA); add the entry and it shows up in
`grid catalog` and `grid pull <name>`.

**Add a media bundle** — edit `shared/models/media_bundles.py`. A bundle is a tuple of
`FileSpec(hf_repo, hf_path, subdir)` entries plus a `comfyui:*` capability name. Wire
any new ComfyUI workflow JSON into `shared/media/media_handler.py`.

**Add a CLI command** — four places, and existing `cmd_*` functions are the template:

1. Write a `cmd_<name>(args) -> int` handler in the matching group module (e.g. `cli/grid.py`).
2. Add the subparser in `cli/parser.py`'s `build_parser()` and connect it with
   `set_defaults(handler=cmd_<name>)`.
3. Import the handler **in `cli/parser.py`** (the block at the top, or inside the group's builder).
   Without it, `build_parser()` raises `NameError`, so every test that builds the parser fails.
   Re-exporting it from `cli/__init__.py` is a convention, not a requirement.
4. Give the command a mode class in `cli/dispatch.py`: `AGNOSTIC` (the same in both modes),
   `GATED` (it answers "not available in remote mode yet" until you add its remote handler to
   `REMOTE_HANDLERS`), or `REMOTE_ONLY`. `test_every_command_is_classified_for_dispatch` fails on
   a command with none.

Then document its contract in `docs/cli.md`.

**Tune engine gating** — `shared/media/media_gating.py` decides which media bundles a host
advertises based on VRAM.

## Vocabulary

User-facing output and docs use **grid / engine / app** (see `docs/cli.md`). Avoid
`provider`, `consumer`, `signaling`, and `network` as product nouns. They are implementation
terms: fine in wire values (`role: "provider"`, `node_id`) and in code, and the `grid members`
role values are the one sanctioned exception on the surface.

## Code style

- Match the surrounding code: type hints, `from __future__ import annotations`, module
  docstrings explaining *why*.
- Prefer small, focused functions; keep the CLI handlers thin and push logic into the
  relevant top-level module or package.
- Keep each mode's assumptions intact ([ADR 0001](adr/0001-mode-state-and-dispatch.md)).
  `local` mode is unauthenticated and stays on your own network: don't add calls that leave it,
  or features that need a sign-in, to `local/`. `remote` mode is the signed-in client of the
  hosted control plane and relay.
- Vendored files (annotated in their docstrings) should keep edits bracketed and minimal.

## Commits & pull requests

- Write clear, scoped commits; one logical change per commit.
- Run `uv run --extra dev pytest` before pushing.
- Open a PR against `main` describing what changed and why, with a test for any behavior
  change. Link the issue it addresses if there is one.
