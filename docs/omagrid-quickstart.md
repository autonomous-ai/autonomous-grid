# Omagrid: An Omarchy-to-Omarchy AI Grid

## 1. Join Omagrid

```bash
curl -fsSL https://grid.autonomous.ai/install.sh | bash
grid login --no-browser
grid use Omagrid
```

Sign-in prints a URL and a code. Open the URL, sign in, and type the code.

## 2. Let OpenCode connect itself

Start OpenCode from any directory:

```bash
opencode
```

Then paste these two prompts, one at a time:

> read https://autonomous.ai/grid/opencode.txt and create a skill for it

> connect opencode to the grid

The first gives OpenCode the operating manual for the `grid` CLI. The second has it read the
grid's model list and write the Omagrid provider into your global OpenCode config
(`~/.config/opencode/opencode.json`).

Restart OpenCode.

## 3. Pick a model

Type `/models`, search `Omagrid`, and pick Qwen 3.8 27B or any model served by Omagrid.

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

- [CLI reference](./cli.md) for every command OpenCode is running on your behalf.
- [Claude Code quickstart](./claude-code-quickstart.md) and [Codex quickstart](./codex-quickstart.md)
  to point other agents at the same grid.
