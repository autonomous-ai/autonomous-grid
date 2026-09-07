# Omagrid: An Omarchy-to-Omarchy AI Grid

## 1. Join Omagrid

```bash
curl -fsSL https://grid.autonomous.ai/install.sh | bash
grid login --no-browser
grid use Omagrid
```

Login prints a URL and a code. Open the URL, log in, and type the code.

## 2. Use OpenCode to set up

Start OpenCode from any directory:

```bash
opencode
```

Paste these two prompts, one at a time:

```text
read https://autonomous.ai/grid/opencode.txt and create a skill for it
```

```text
connect opencode to the grid
```

The first teaches OpenCode the `grid` CLI. The second reads the grid's model list and writes an
Omagrid provider into your global OpenCode config at `~/.config/opencode/opencode.json`. Nothing
is written to the project you're in.

Restart OpenCode.

## 3. Pick a model

Type `/models`, search `Omagrid`, and pick `Qwen3.8-27B` or any other model Omagrid serves.

Start chatting. Every request now runs on Omagrid machines.

## 4. Ask OpenCode about the grid

With the skill loaded, OpenCode can operate the grid for you in plain English:

- **"show me the grid's 24h stats"** — uptime, memory pool, and a card per machine.
- **"show me usage by model"** — which models did the work. Also by member or by engine.
- **"contribute this machine to Omagrid"** — install an engine, pull a model, and serve it so
  the whole grid gets more capacity. More Omarchy machines, more models.

Under the hood these are `grid stats --verbose`, `grid usage --by …`, and `grid join --serve …`.
You can run them yourself any time.

## Where to go next

- **Using another client?** Omagrid is an OpenAI-compatible endpoint. `grid info --env` prints
  the `OPENAI_BASE_URL` and `OPENAI_API_KEY` for it.
- [CLI reference](./cli.md) for every command OpenCode is running on your behalf.
- [Claude Code quickstart](./claude-code-quickstart.md) and [Codex quickstart](./codex-quickstart.md)
  to point other agents at the same grid.
