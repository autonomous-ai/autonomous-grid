# Omagrid quickstart — use the grid from opencode on Omarchy

Omagrid is a shared grid: Omarchy machines serving models to each other. This is the early-access
path, end to end: sign in, pick the grid, and let **opencode** wire itself up. You type two commands
by hand. opencode does the rest.

## What you need

- An Omarchy machine (any Linux or macOS box with the `grid` CLI works the same way).
- Early access to Omagrid. Not on it yet? DM [@dee_hw](https://x.com/dee_hw).
- [opencode](https://opencode.ai) installed.

Check the CLI is there with `grid --version`. If it isn't:

```bash
curl -fsSL https://grid.autonomous.ai/install.sh | bash
```

## 1. Sign in

```bash
grid login --no-browser
```

It prints a URL and a code. Open the URL on any device, approve with Google, type the code.
Sign-in lists the grids you can reach. `Omagrid` should be one of them; if it isn't, you're not on
the early-access list yet.

## 2. Pick the grid

```bash
grid use Omagrid
```

That is the last command you run by hand. Everything after this, opencode runs for you.

## 3. Let opencode connect itself

Start opencode from any directory:

```bash
opencode
```

Then paste these two prompts, one at a time:

> read https://autonomous.ai/grid/opencode.txt and create a skill for it

> connect opencode to the grid

The first gives opencode the operating manual for the `grid` CLI. The second has it read the
grid's model list, write the Omagrid provider into your global opencode config
(`~/.config/opencode/opencode.json`), and prove the path answers with a test request. It will end by
telling you to restart.

## 4. Restart and pick a model

Quit opencode and start it again. Then:

1. Type `/models`
2. Search `Omagrid`
3. Pick a model served by Omagrid

Start chatting. Every request now runs on Omagrid machines.

## 5. Ask opencode about the grid

With the skill loaded, opencode can operate the grid for you in plain English:

- **"show me the grid's 24h stats"** — uptime, memory pool, and a card per machine.
- **"show me usage by model"** — which models did the work. Also by member or by engine.
- **"contribute this machine to Omagrid"** — install an engine, pull a model, and serve it so
  the whole grid gets more capacity. More Omarchy machines, more models.

Under the hood these are `grid stats --verbose`, `grid usage --by …`, and `grid join --serve …`.
You can run them yourself any time.

## When something fails

- **`Unauthorized` inside opencode** — a project-level `./opencode.json` is overriding the global
  provider, or the token went stale. Ask opencode to "connect opencode to the grid" again.
- **"You're not signed in"** — run `grid login --no-browser` yourself. That one needs a browser
  and a code only you can type.
- **A model 404s** — nothing is serving it right now. Ask for stats to see what each machine has
  loaded.
- **`grid: command not found`** — open a new terminal, or run the install line above.

## Where to go next

- [CLI reference](./cli.md) for every command opencode is running on your behalf.
- [Claude Code quickstart](./claude-code-quickstart.md) and [Codex quickstart](./codex-quickstart.md)
  to point other agents at the same grid.
- [Working from anywhere](../README.md#working-from-anywhere) for how remote mode works.
