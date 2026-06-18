# Grid CLI

Grid should feel like the command line for a local AI grid: bring one endpoint online,
join the engines you already run, see what models are live, and use them immediately.
Internal protocol words stay out of the user-facing CLI.

## Vocabulary

```
grid      a named local AI endpoint, usually `home` or `work`
grid_url  the URL engines join; apps call it through `/v1`
engine    something that runs models: Ollama, LM Studio, vLLM, MLX, llama.cpp, ComfyUI
join      connect this machine or engine to a grid
model     a live capability exposed by joined engines
```

Do not use `provider`, `consumer`, or `signaling` in CLI output or first-run docs.
Avoid `network` as a product noun. Those are implementation terms for architecture docs
and code.

## Design Rules

- The common path is one screen: `grid up`, `grid join`, `grid models`, `grid chat`.
- `home` is the default grid. Users name a grid only when they have several.
- `up` is idempotent: create if missing, start if stopped, print the same contract every time.
- Default output is human-readable. Every state-reading command supports `--json`.
- Use examples before exhaustive flags in help text.
- `--name` names an engine; `[grid]` names a grid.
- `--at <url>` always means an existing engine endpoint.
- `--serve <model>` always means Grid starts its default text engine, then joins it.
- `--media` always means Grid starts or joins the default media engine.

## Top Level

```
grid                                  # overview: default grid, endpoint, engines, models, next steps
grid --help                           # concise help with common examples first
grid <command> --help
grid version
```

Bare `grid` is not just help. It is the dashboard for a terminal:

```text
Grid: home
grid_url: http://192.168.1.25:8090
engines: 3 live
models: qwen36-27b-mtp, gemma4-31b, devstral-small-2

Next:
  grid join
  grid chat -m qwen36-27b-mtp "hello"
  grid info --env
```

If no grid exists yet, bare `grid` should show the shortest successful path:

```text
No grid yet.

Start one:
  grid up

Then join an engine:
  grid join
```

## Grid Lifecycle

```
grid up [name]                        # create/start a grid; default: home
grid down [name]                      # stop a local grid; config persists
grid ls [--json]                      # list saved grids
grid info [grid] [--json]             # endpoint, key, engines, live models
grid info [grid] --env                # print OPENAI_* exports
```

`grid up` output is stable and scriptable:

```text
grid=home
grid_url=http://192.168.1.25:8090
```

No separate `create`, `start`, or `use` in the main surface. They make first use feel
like infrastructure management. `up` is the user-level operation.

## Engines

```
grid join [grid]                                      # auto-detect local engines
grid join [grid] --all                                # join every detected engine
grid join [grid] --at <url> -m <model>... [--name <id>]
grid join [grid] --serve <model> [--name <id>]
grid join [grid] --media [--bundle <bundle>]... [--name <id>]
grid leave [grid] [--engine <id>] [--all]
grid engines [grid] [--json]                          # live engines joined to a grid
```

`grid join` with no flags should detect local engines in this order:

1. Ollama
2. LM Studio
3. vLLM
4. MLX
5. llama.cpp
6. ComfyUI

When detection finds more than one engine, print the plan and ask for confirmation in
interactive terminals. In non-interactive mode, require `--all`, `--engine <kind>`, or
explicit `--at`.

Example detection output:

```text
Detected engines on this machine:

  mac-studio     MLX        http://192.168.1.10:8080/v1      gemma4-31b
  gpu-4090       vLLM       http://192.168.1.20:8000/v1      devstral-small-2

Join them:
  grid join --all
  grid join --engine gpu-4090
```

Engine IDs are local names shown by `grid engines`, `grid info`, and
`grid models --verbose`; they are accepted by `grid leave --engine <id>`.

## Models

```
grid models [grid] [--verbose] [--json] # live models the grid can run now
grid catalog [--json]                   # models Grid can pull
grid pull <model>                       # pull a model for the default text engine
grid rm <model> [--yes]                 # remove a pulled model
```

`grid models` answers the orchestration question: what can this grid run right now?

Default:

```text
qwen36-27b-mtp
gemma4-31b
glm-4.5-air
devstral-small-2
comfyui:image_generation
```

Examples should intentionally mix model families. Grid is an orchestration layer, not a
launcher for one model vendor.

Verbose:

```text
MODEL                     ENGINE       WHERE
gemma4-31b                mac-studio   http://192.168.1.10:8080/v1
qwen36-27b-mtp            gpu-3090     http://192.168.1.20:8000/v1
devstral-small-2          gpu-4090     http://192.168.1.30:8000/v1
glm-4.5-air               gpu-5090     http://192.168.1.40:8000/v1
comfyui:image_generation  media-mac    http://192.168.1.30:8190
```

## Use

```
grid chat -m <model> "<message>" [--json]
grid image "<prompt>" [-o <dir>]
grid edit "<prompt>" -i <img>... [-o <dir>]
grid video "<prompt>" -i <img> [-o <dir>]
```

These are smoke tests and useful daily commands. They go through the grid, not directly
to an engine. Their errors should name the missing model, the selected grid, and the next
diagnostic command:

```text
No live model named `qwen36-27b-mtp` on grid `home`.

See live models:
  grid models

Check engines:
  grid engines
```

## Engine Setup

```
grid engine install llama.cpp          # default text engine
grid engine install comfyui            # default media engine
grid engine pull <bundle>              # ComfyUI media bundle: image_generation, image_editing, i2v
```

Grid has no inference engine of its own. These commands install open-source default
engines so a bare machine can join a grid without Ollama, LM Studio, or vLLM.

## Aliases

```
grid list                              # alias for grid ls
grid remove <model> [--yes]            # alias for grid rm
```

Aliases are for familiarity, but docs should teach the shorter form.

## Output Contract

Human output uses these names exactly:

```
grid
grid_url
engines
models
```

`grid_url` is the primary URL. `OPENAI_BASE_URL` is derived as `${grid_url}/v1` and is
shown only where OpenAI-compatible app integration needs copy-pasteable environment
variables.

Environment output from `grid info --env`:

```bash
export OPENAI_BASE_URL="http://192.168.1.25:8090/v1"
export OPENAI_API_KEY="local-grid"
```

JSON output should use snake_case keys and include enough detail for scripts:

```json
{
  "grid": "home",
  "grid_url": "http://192.168.1.25:8090",
  "engines": [],
  "models": []
}
```

## First-Run Happy Path

```bash
grid up
grid join
grid models
grid chat -m qwen36-27b-mtp "hello"
eval "$(grid info --env)"
```

For a machine with no engine:

```bash
grid up
grid engine install llama.cpp
grid pull qwen36-35b-a3b-mtp
grid join --serve qwen36-35b-a3b-mtp
grid chat -m qwen36-35b-a3b-mtp "hello"
```
