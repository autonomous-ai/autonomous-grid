# Omagrid: An Omarchy-to-Omarchy AI Grid

## 1. Install Grid

```bash
curl -fsSL https://grid.autonomous.ai/install.sh | bash
grid login --no-browser
```

Login prints a URL and a code. Open the URL, log in, and type the code. This is the only step
you do by hand — everything after it you ask OpenCode for.

## 2. Use OpenCode to set up

Start OpenCode from any directory:

```bash
opencode
```

Paste these two prompts, one at a time:

```text
read https://github.com/autonomous-ai/autonomous-grid/blob/main/docs/opencode.txt and create a skill for it
```

```text
use Omagrid and connect opencode to the grid
```

The first teaches OpenCode the `grid` CLI. The second picks Omagrid as your grid, reads its
model list, and writes an Omagrid provider into your global OpenCode config at
`~/.config/opencode/opencode.json`. Nothing is written to the project you're in.

Restart OpenCode.

## 3. Pick a model

Type `/models`, search `Omagrid`, and pick `Qwen3.8-27B` or any other model Omagrid serves.

Start chatting. Every request now runs on Omagrid machines.

## 4. Power Omagrid

Everything so far uses the grid. This step adds to it: your machine runs a model that everyone
on Omagrid can call. More Omarchy machines, more models.

Ask OpenCode:

```text
contribute this machine to Omagrid
```

It picks a model your machine can run, installs the engine, downloads the model, and joins it
to the grid. The download is a few gigabytes and takes a while — OpenCode tells you the size
before it starts.

Say what you want and it will follow:

- **"use a smaller model, I'm still working on this machine"** — leaves memory free for you.
- **"take only 2 requests at a time"** — how busy your machine gets.
- **"give it a 128k context window"** — how much each request can read.

And afterwards:

- **"is my machine serving?"** — it shows up by name, with what it has served so far.
- **"stop serving"** — leaves the grid. The model stays downloaded, so joining again is
  quick.

Under the hood these are `grid engine install`, `grid pull`, `grid join --serve …`,
`grid stats --verbose`, and `grid leave`.

## 5. Ask OpenCode about the grid

With the skill loaded, OpenCode can operate the grid for you in plain English:

- **"show me the grid's 24h stats"** — uptime, memory pool, and a card per machine.
- **"show me usage by model"** — which models did the work. Also by member or by engine.
- **"which machine is carrying the grid right now?"** — the same readings, read for you.

Under the hood these are `grid stats --verbose` and `grid usage --by …`. You can run them
yourself any time.

## Where to go next

- **Using another client?** Omagrid is an OpenAI-compatible endpoint. `grid info --env` prints
  the `OPENAI_BASE_URL` and `OPENAI_API_KEY` for it.
- [CLI reference](./cli.md) for every command OpenCode is running on your behalf.
- [Claude Code quickstart](./claude-code-quickstart.md) and [Codex quickstart](./codex-quickstart.md)
  to point other agents at the same grid.
