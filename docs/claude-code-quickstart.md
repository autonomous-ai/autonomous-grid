# Claude Code quickstart — run Claude Code on your own grid

This walks the whole path end to end: install `grid`, sign in, choose a remote grid, and start
**Claude Code** already pointed at it. One command does the last part:

```bash
grid launch claude
```

The endpoint and the credential go into Claude Code's own process environment and nowhere else —
nothing is exported to your shell, nothing is written to any config file, and closing the app is
the entire cleanup. The model stays your choice (step 3). Reference detail lives in [cli.md](./cli.md#launch) and
[ADR 0028](./adr/0028-launch-hands-an-app-the-grid.md).

Two roles appear below. The **provider** serves the models Claude Code will use. The **consumer**
runs `grid launch claude`. They can be the same person on the same machine — the steps don't change.

## What you need

- The `grid` CLI, in **remote** mode, signed in, with an active grid (below).
- Claude Code. You don't have to install it first: `grid launch claude` searches your `PATH` and the
  two places Claude Code installs itself into, and offers to run the vendor's own installer if it
  finds nothing.
- A grid that serves a model you can point Claude Code at. `grid launch` picks no model for you,
  so see step 3 — asking for a model the grid doesn't serve is the most common first failure.

**`grid launch` is remote-only, and the reason is the dialect.** Claude Code speaks the Anthropic
Messages dialect and nothing else. A remote grid's relay translates a Messages request to
`chat/completions` before it routes, so any chat model can back the app. A local grid has no
`/v1/messages` at all, so in `local` mode the command refuses and names that — it is a design
boundary, not a missing flag.

## 1. Install grid and sign in

```bash
curl -fsSL https://grid.autonomous.ai/install.sh | bash   # macOS / Linux
grid mode remote                                          # persist remote mode
grid login                                                # device-code sign-in to the hosted relay
```

`grid login` prints a sign-in URL and code and opens a browser at it; `--no-browser` prints the URL
instead, for a headless box.

## 2. Choose a grid

```bash
grid ls                    # grids you can reach
grid use <name>            # pick the active grid
grid                       # overview: mode, active grid, engines, models
```

`grid use` is the one place you choose where work runs — `grid launch claude` follows it. To launch
against a different grid just once, name it: `grid launch claude <name>` (a grid name or an `ag-…`
id, exactly like `grid info` / `grid models` / `grid engines`).

The grid must be **up** (`grid up`) and your sign-in must be a member of it.

## 3. Pick a model the grid serves

**`grid launch` chooses no model for you.** It sets the endpoint and the credential and nothing else,
so Claude Code resolves a model exactly as it does everywhere else: its own defaults, your
`settings.json`, and `/model` inside the app. That choice stays yours — but it means **you** have to
point it at something the grid actually serves.

`grid models` is the authoritative list:

```bash
grid models                # bare model ids, one per line
```

```text
glm-5.2
qwen36-30b
```

Then tell Claude Code to use one — in your `settings.json`, or in the shell you launch from:

```bash
ANTHROPIC_MODEL=glm-5.2 grid launch claude
```

The one thing checked in advance is that the grid is serving *something*; what it is, and whether the
app asks for it, is not something `grid launch` can know:

```text
Grid team serves no models yet, so Claude Code has nothing to talk to.
Run `grid join` on a machine with an engine, then `grid launch claude` again.
```

A model the grid doesn't serve therefore fails at your **first prompt**, not at launch. `grid models`
before your first run is the way to avoid it.

### Serving models to the grid (the provider's side)

There is no `claude` engine kind to join in this release, and you don't need one. Because the relay
translates Messages to `chat/completions` before routing, **any chat model can back Claude Code** —
so an ordinary join is all it takes:

```bash
grid join research --at http://192.168.1.20:8000/v1 -m qwen36-30b -m gemma4-4b
```

If you prefer the app to see different names than the engine serves, `grid join` can advertise
aliases — `-m real=advertised`, or the equivalent `--advertise-as`. Aliasing is **single-engine
only**: one join, one engine, aliases for every `-m` in that command, and not an append onto an
identity already serving. Details and the mode gating are in
[cli.md → `grid join` in remote mode](./cli.md#grid-join-in-remote-mode).

Then confirm from the consumer's side with `grid models` again.

## 4. Launch

```bash
grid launch claude
```

One line on stderr names the grid, then Claude Code owns the terminal exactly as it does when you
run the binary yourself — same Ctrl-C, same exit code:

```text
Starting Claude Code on grid team.
```

Not sure what can be launched? Bare `grid launch` lists the targets and exits 0:

```bash
grid launch
```

```text
Launch targets:
  claude	Claude Code

Start one with `grid launch <target>`.
```

### Passing Claude Code its own arguments

Everything after the **first** `--` goes to the app, in order and unread — the launcher is never a
ceiling on the app:

```bash
grid launch claude -- --continue
grid launch claude -- -p 'summarise this repo'
grid launch claude research -- --continue      # …on a grid other than the active one
```

Two words are the exception: `--local` and `--remote` are grid's own one-shot mode override and are
stripped from anywhere on the line, separator included, so neither reaches the app. A warning says so
when one is taken.

### Managing your own shell instead

`--print-env` prints exactly what a launch would inject and starts nothing:

```bash
grid launch claude --print-env
```

```bash
export ANTHROPIC_BASE_URL='https://relay.example/relay'
export ANTHROPIC_AUTH_TOKEN='<your access token>'
```

Note the base URL carries **no** `/v1` — Claude Code appends `/v1/messages` itself, so the `/v1` that
`grid info --env` prints for OpenAI clients would 404 every request here. That one character is why
this command exists.

Like `grid info --env`, this prints your access token; unlike it, warnings stay on stderr, so
`eval "$(grid launch claude --print-env)"` evaluates only the exports.

## What to expect on this path

- **Your real Claude Code.** MCP servers, skills, permission allowlist, project history — all of it,
  unchanged. Nothing is isolated and nothing is written on your behalf.
- **Your own settings can override it.** Claude Code's `settings.json` `env` block outranks an
  injected variable, so a stray `ANTHROPIC_BASE_URL` there silently defeats the launch. The command
  reads that file and warns in one line — it never edits it.
- **Your credential is checked, and quietly repaired.** Before anything is handed over, `grid launch`
  reads the token's own expiry and asks the grid whether it will accept it. An expired token — or one
  the grid refuses because your membership changed — is **refreshed in place** and the launch carries
  on, with one line on stderr. Nothing that cannot be repaired reaches the app: it is refused here,
  naming which of "sign in again", "ask about your membership" or "try again shortly" applies.
- **A check that fails open never costs you a launch.** If the grid is unreachable, rate-limiting, or
  erroring, `grid launch` says so and starts the app anyway rather than guessing your token is bad.
- **The model is yours, including `auto`.** `grid launch` sets no model variable, so if your grid's
  owner has enabled auto-routing you can set `ANTHROPIC_MODEL=auto` yourself and let the grid pick.
  Worth knowing before you do: Claude Code issues many small requests per turn, so an advisor hop
  lands on all of them and the model can change turn to turn — which makes "it got worse today"
  hard to pin down.
- **Nothing to undo.** No config file, no shell export, no `--restore`. Quit the app and it's over.

## If it doesn't launch

| what you see | what it means |
|---|---|
| "is a remote-mode command" | You're in local mode. Run `grid mode remote` (or pass `--remote`) — a local grid does not serve the Anthropic Messages dialect. |
| "You're not signed in" | No stored credential on this machine. Run `grid login`. |
| "serves no models yet" | Nothing is joined to the grid. `grid join` on a machine with an engine (step 3). |
| A model error at your first prompt | The app asked for a model this grid doesn't serve — `grid launch` sets no model, so it can't catch this for you. Compare `grid models` against what your Claude Code config resolves to. |
| "Claude Code isn't installed" | Nothing on your `PATH` or in either place Claude Code installs to. On a terminal you're offered the vendor's installer; with no terminal (CI, a script) the install command is printed and the launch exits non-zero. |
| "Warning: Claude Code's own settings set …" | An `env` block in your `settings.json` overrides what was injected. Remove the colliding key — `grid launch` never edits that file. |
| "couldn't check … launching anyway" | The credential check could not reach the grid, so it was skipped rather than guessed at. If the app then fails to authenticate, that warning is why. |
| Requests fail *inside* the app | The credential is checked and renewed before hand-over, so this is more likely a model or settings problem. `grid launch claude --print-env` shows exactly what was handed over. |
