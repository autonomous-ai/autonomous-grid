# Two people and one provider, on one Mac

A distributed task is a conversation you hand to the grid: you post a prompt, some machine runs a
coding agent against your project, and the result appears in the project by itself. This page is the
**run** — three terminals on one Mac, in order, from nothing to two colleagues working in the same
project at the same time.

[`tasks-quickstart.md`](./tasks-quickstart.md) is the reference: every command, every environment
variable, every pair of commands that are easy to confuse. Read this one first if you have never
seen the feature work; read that one when you want to know what a flag does.

Everything below was exercised end to end on macOS against a live relay — Claude Code **2.1.234**,
git **2.54.0**, grid **0.3.19** — with two real accounts, one provider, and about 35 real agent
tasks. Where a number appears (24 seconds, 3 seconds, 1 second) it was measured on a three-file
project, not estimated. The four traps in §0 are the ones that actually cost time on that run.

---

## What you need

| | |
|---|---|
| **Claude Code ≥ 2.1.221**, signed in | The provider runs it and pays for every task out of that subscription. Below 2.1.221 the provider refuses to start rather than run agents unconfined |
| **The `grid` CLI**, remote mode | `curl -fsSL https://raw.githubusercontent.com/autonomous-ai/autonomous-grid/main/install.sh \| sh` |
| **Two accounts on the same grid** | Two people is the point. One is the grid's owner; the second is an ordinary member |
| **git** | Only for making the sample repository, and only on the machine that imports it. Reading a project needs no git at all — that is §7 |

You do **not** need a second computer. Two identities live side by side on one Mac, because every
`grid` command reads `GRID_HOME` for "who am I".

---

## The shape

```
  one Mac                                              elsewhere
  ┌────────────────────────────────────────────┐
  │ terminal 1   GRID_HOME=~/.grid-alice       │       ┌──────────────────┐
  │              Alice — grid owner, client    │ ────▶ │  the relay       │
  │                                            │       │                  │
  │ terminal 2   GRID_HOME=~/.grid-bob         │ ────▶ │  owns `main`     │
  │              Bob — second member, client   │       │  hands out tasks │
  │                                            │       │                  │
  │ terminal 3   the provider                  │ ◀──── │  applies results │
  │              GRID_TASKS=1 grid join        │       └──────────────────┘
  │              └─ runs `claude`, once a task │
  └────────────────────────────────────────────┘
```

**A provider serves the grid, not a person.** It claims whatever task is next, whoever asked for it,
so it can run out of Alice's `GRID_HOME` and still run Bob's work. Keep it in its own terminal
anyway: it is a long-lived process and you will want to read its log.

Set this up once, in each of the first two terminals:

```bash
# terminal 1
export GRID_HOME=~/.grid-alice
# terminal 2
export GRID_HOME=~/.grid-bob
```

Every `grid` command below runs in the terminal whose person it names.

---

## 0. Clear four traps before you start

Each of these fails **quietly** — nothing crashes, and the symptom shows up somewhere else entirely.
Five minutes here saves an afternoon.

### T1 · `claude` on your `PATH` may not be the real one

The provider resolves `claude` from `PATH` **first**, and keeps what it finds. Editors and terminal
wrappers routinely put a temporary shim earlier on `PATH`; on the measured run a provider had been
serving for six days through a shim whose file no longer existed.

```bash
which claude          # if this is not ~/.local/bin/claude (or your real install), read on
claude --version      # must be >= 2.1.221
```

**Fix:** start the provider with the real directory first on `PATH` — it is already in the §3
command. Do not rely on whatever `PATH` your terminal happens to have.

### T2 · Each `GRID_HOME` remembers the control plane it signed into

`grid login` writes the control-plane URL into that home's `credentials.toml`, and from then on **it
wins over `GRID_CONTROL_PLANE_URL`**. Setting the environment variable and expecting it to move an
existing home is how a grid gets created somewhere you did not mean.

```bash
grep -m1 '^api_url' ~/.grid-alice/credentials.toml
grep -m1 '^api_url' ~/.grid-bob/credentials.toml
```

Both should name the same control plane. `grid ls` will **not** tell you — it reads a local cache
and never calls the control plane, so it happily lists grids from a different one.

**Fix:** for a fresh start, use home directories that have never been logged in, and let `grid login`
set them.

### T3 · `GRID_TASK_ROOT` must live **outside** `$HOME`

The agent sandbox denies the whole of `$HOME` and re-allows the workspace inside it. On macOS that
re-allow does not reach the permission layer that governs the agent's `Write` tool: put the task root
in your home and **the agent's file writes are refused**, silently. Measured on 2.1.234, same prompt
both ways:

| `GRID_TASK_ROOT` | agent's `Write` | tasks to finish |
|---|---|---|
| `~/grid-tasks` (inside `$HOME`) | refused — the agent works around it with shell redirects | 5, 24s |
| `/Users/Shared/grid-tasks` | works first time | 3, 15s |

The task still reports `completed`, which is what makes it expensive to find.

```bash
mkdir -p /Users/Shared/grid-tasks
```

Keep the path **short**. Grid adds ~126 characters below the root, and the whole path becomes one
directory name for the agent's transcript; past about 200 characters the name is truncated and the
conversation cannot be found again. `/Users/Shared/grid-tasks` leaves plenty of room.

### T4 · The Claude seat wants port 8099

`--api claude` starts a local seat on **8099** (codex uses 8098). A second one dies *after* printing
its banner, so it looks like it started.

```bash
lsof -nP -iTCP:8099 -sTCP:LISTEN     # must be empty
```

**Fix:** stop the process that holds it, or pass `--seat-port 8199` in §3.

---

## 1. Sign in as two people

```bash
# terminal 1
grid login          # opens a browser; sign in as alice@example.com
# terminal 2
grid login          # sign in as bob@example.com
```

Sessions last 24 hours. When one expires, `grid sync` refreshes it without a browser.

> ⚠️ `grid sync` prints `Your grid session has expired…` and still **exits 0**. A script cannot tell
> success from expiry by exit code alone; read the output, or check that the command you actually
> wanted succeeded.

---

## 2. Create the grid and admit Bob

```bash
# terminal 1 — Alice
grid start demo                                   # creates it on first run
grid sync
grid members add demo bob@example.com --role both
```

```bash
# terminal 2 — Bob
grid sync                                      # the new grid appears; no second login needed
```

If `grid start` is refused with `domain_network_locked`, your account cannot create the default network
type; use `grid start demo --type permissioned-providers`.

If `grid join` in the next step answers `503 routing temporarily unavailable`, the grid is still
warming up. Wait about 90 seconds and run it again.

---

## 3. Start the provider (terminal 3)

```bash
PATH="$HOME/.local/bin:$PATH" \
GRID_HOME=~/.grid-alice \
GRID_TASKS=1 \
GRID_MAX_TASKS=2 \
GRID_TASK_ROOT=/Users/Shared/grid-tasks \
grid join demo --api claude
```

| | |
|---|---|
| `GRID_TASKS=1` | **Required.** Task serving is off by default and can never turn itself on — it spends your Claude subscription |
| `GRID_MAX_TASKS=2` | Two tasks at once. §9 needs it: with one worker the two colleagues simply take tasks and never meet |
| `GRID_TASK_ROOT` | See T3 |
| `PATH=…` | See T1 |

Leave it running. It claims work every 30 seconds; an idle claim is a `204`, and that is the healthy
state — not an error.

**Two notes on the subscription.** Every agent this provider runs draws on the same Claude
subscription, so that rate limit — not your CPU — is the ceiling. When the window is spent the
provider **stops claiming and resumes when the vendor's window resets**; you see it as a
`task.rate_limit` line rather than as failures. And `GRID_MAX_TASKS` above 1 puts two Claude Code
children under one config directory, which `cli.md` flags as unverified; 2 is what the measured run
used, without incident. Do not raise it far on a provider you cannot watch.

**About `GRID_TASK_CLAUDE_CONFIG_DIR`:** leave it unset and every task inherits *your* Claude
settings — your hooks, your `~/.claude/CLAUDE.md`, your skills. That is usually not what you want on
a machine you also work on. Pointing it at a dedicated directory works, but that directory needs its
own login first, or every task fails with an empty `the agent exited 1:` and `Not logged in`:

```bash
CLAUDE_CONFIG_DIR=~/.claude-grid-provider claude       # then /login, then quit
CLAUDE_CONFIG_DIR=~/.claude-grid-provider claude -p "reply with OK"   # must answer
```

**A task's own files cannot configure the agent that reads them.** The agent loads the operator's
user-scope settings and never the repository's, so a `.claude/settings.json` arriving in somebody's
task cannot run anything on your machine. The consequence is worth knowing before you write prompts:
**a repository's own `CLAUDE.md` is not in the agent's context either**, though it stays on disk and
the agent can read it if you ask. Put the standing instructions in the prompt, or tell the agent to
read the file.

---

## 4. Alice's first project, and her first task

A project is a git repository the grid hosts. It has **no trunk** when created, and a task cannot be
cut from nothing — so give it one of the two possible starts. **The choice cannot be taken back.**

**Starting from nothing:**

```bash
grid project create --name scratch --empty --json     # ready to work in immediately
```

**Bringing a repository you already have** — make one to play with:

```bash
mkdir -p ~/fixture && cd ~/fixture && git init -q
printf 'RED\n' > colors.txt
printf '# app\n' > README.md
printf '#!/bin/sh\necho hi\n' > run.sh && chmod +x run.sh
git add -A && git commit -qm "start" && cd -

grid project create --name app --json                 # note the id it prints — no trunk yet
grid project import ~/fixture <project-id>
```

Import took **about a second** on those three files; on a 29,000-commit repository it is about
twenty seconds, because the relay reads every tree the history reaches before `main` exists. It is
refused — leaving the project with no trunk, deliberately — if the repository has a submodule, a
path under `.grid/`, or a symlink pointing outside itself. `.claude/` is fine. Executable bits
survive: `run.sh` comes back as executable through import *and* through every read below.

Now the first task:

```bash
grid task create --project <project-id> \
  --prompt "replace the contents of colors.txt with the single word BLUE" \
  --follow
```

`--follow` streams the agent and exits with the task's outcome. The three-file version of this
finished in **24 seconds**.

> ⚠️ **`--follow` returning does not mean the project shows it yet.** The task reports first and the
> grid applies it to `main` a moment later — measured at **about 3 seconds** on this project. A
> script that reads the file the instant `--follow` returns reads the *old* value. Poll
> `grid project status --json` (or the file itself) instead of assuming.

Keep two ids from the output: the **task id** and the **conversation id**. They are different
objects, and the distinction is the one thing worth internalising here — the conversation is the
Claude session you come back to, a task is one message and one agent run inside it.

---

## 5. A conversation is many tasks

```bash
grid task send <conversation-id> \
  --prompt "append the word GREEN on a new line of colors.txt" --follow
```

`colors.txt` now reads `BLUE` then `GREEN`. The second task **built on** the first rather than
starting from the project as it was when the conversation began — each task is cut from where the
previous one left off.

You can type ahead. Send three messages without waiting:

```bash
grid task send <conversation-id> --prompt "add a line ONE to colors.txt"
grid task send <conversation-id> --prompt "add a line TWO to colors.txt"
grid task send <conversation-id> --prompt "add a line THREE to colors.txt"
grid task list --project <project-id> --json
```

They run **in the order you sent them, one at a time**. Inside one conversation there is no
concurrency; across conversations there is nothing but.

Only the person who started a conversation can send into it. A colleague reads it and starts their
own.

---

## 6. Watch the whole conversation, not one task

```bash
grid task follow --conversation <conversation-id>
```

One stream carrying every task in order — including steps the grid added itself — ending with
`conversation.idle` when it goes quiet. On the measured run this replayed events 0 through 43 across
five tasks with nothing missing and nothing repeated.

Interrupt it and pick up where you stopped:

```bash
grid task follow --conversation <conversation-id> --after-seq 43
```

You get 44 onward. A conversation's sequence numbers are its own — they are not a task's.

---

## 7. Read the project on a machine with no git

This is what makes the feature usable by people who do not want a checkout.

```bash
grid project files <project-id>                    # top level
grid project files <project-id> src                # inside a folder
grid project file  <project-id> colors.txt         # one file, printed
grid project file  <project-id> logo.png --output logo.png
grid project download <project-id> --output app.zip
```

None of these shells out to git — verified with `PATH` pointing at an empty directory, where even
`sh` was missing, and they still answered. What you see is the project as it stands now: everybody's
finished work, applied as each task completed.

Edges answer deliberately rather than guessing. A path is taken **literally**, so `*.txt` is a file
named `*.txt` and not a wildcard; it comes back `no_such_path` rather than silently matching
something. `../../etc/passwd` is `invalid_request`. A file too large to print names its size and
points at `download`.

To see what one task did, and who asked for it:

```bash
grid task diff <task-id>
```

---

## 8. Bring Bob into the project

```bash
# terminal 1 — Alice
grid project member add <project-id> --email bob@example.com
grid project member list <project-id>
```

Bob must have signed in to the grid at least once (§1–2) before he can be added.

> **On grid-wide sharing.** `grid project share` records that anyone on the grid may work in the
> project without being added — but it only takes **effect** on a grid configured to share projects
> that way, which is a per-email-domain grid the CLI cannot create. On the network types `grid start`
> makes, the CLI says so plainly: *"It is recorded, but this grid does not share projects grid-wide
> — only its members can reach it, exactly as before."* So add Bob explicitly, as above.
>
> `grid project private` restricts a project to its members, and it is honest everywhere. A
> non-member reading a private project gets **byte-for-byte the same refusal** as for a project id
> that does not exist — there is no way to discover that it is there.

Now Bob works:

```bash
# terminal 2 — Bob
grid task create --project <project-id> --prompt "add a file bob.txt containing hello" --follow
grid task list --project <project-id> --all        # what the team ran, not only your own
```

---

## 9. Two people at once

This is the part that has no equivalent in a single-user tool, and the part worth watching the
provider's log during.

**Different files.** Have Alice and Bob each start a task touching a different file, within a few
seconds of each other:

```bash
# terminal 1 — Alice
grid task create --project <pid> --prompt "create alpha.txt containing A" --follow
```
```bash
# terminal 2 — Bob, within a few seconds
grid task create --project <pid> --prompt "create beta.txt containing B" --follow
```

Both complete. **Neither is refused for racing**, and both land on `main` — one of them arriving as
a merge the grid made itself. Nobody ran a command to make that happen.

**The same file.** Now have both change `colors.txt`:

```bash
# terminal 1 — Alice
grid task create --project <pid> --prompt "make colors.txt say ALPHAONLY"
```
```bash
# terminal 2 — Bob, within a few seconds
grid task create --project <pid> --prompt "make colors.txt say BETAONLY"
```

One wins. The loser does not fail and is not asked to fix anything: the grid puts a **merge task**
into that person's own conversation, resuming the same agent session that caused the collision, and
the agent reconciles. On the measured run the result kept both intentions — `colors.txt` ended up
holding `ALPHAONLY` and `BETAONLY` — and any messages the loser had already typed ahead ran
afterwards, in order.

```bash
grid task list --project <pid> --all --json \
  | python3 -c 'import json,sys; [print(t["id"], t.get("kind")) for t in json.load(sys.stdin)["tasks"]]'
```

A merge task is `"kind": "merge"`; a message you typed is `"message"`. An application should render
the first as a step the grid took and never as something a person said.

> The merge task's own result text comes from the model, and it talks like a developer — it will say
> things like *"merge resolved and committed as f0e7cfa"*. `kind` is how you avoid showing that to
> someone who does not want to know the project is a git repository.

**If you have `grid project status --json` handy**, it reports `member_running_turns` and
`member_running_cap`: how many tasks of yours are running, and how many the grid will run at once for
one person. A task held back by that cap is queued, never refused, and never expires because of it —
and a merge task is exempt, so a person at their cap whose work collides is not deadlocked.

---

## 10. Taking it back

**Undo** — the change reaches the project with nobody asked to approve it, so this is how you decline
afterwards:

```bash
grid task undo <task-id>
```

It removes exactly what that one task changed. Everything done since **stays**: the project is not
rewound, it simply no longer contains that change, and `main` moves *forward* to say so. On the
measured run, undoing a middle task left both the task before it and the task after it intact.

If somebody has since built on the same files, it refuses with `undo_conflicts` (exit 1) and names
them — no git vocabulary, and `main` does not move. Ask for what you want in a new message instead.
Undoing twice is `already_undone`. Only the person who asked for the task, or the project's owner,
may undo it.

**Cancel** — stop a task that has not finished. The conversation survives; the next message you send
continues:

```bash
grid task cancel <task-id>
```

The agent stops within about half a minute, on the provider's next lease renewal.

> ⚠️ `grid task cancel --help` says *"Nothing is rewound: whatever the agent had already done is
> kept, so `grid task fetch` still works on it."* **Measured, it does not:** a cancelled task has no
> result, and `grid task fetch` returns the tree from *before* the agent did anything. Nothing is
> lost that had landed — the next message simply starts a fresh session from the last good state —
> but do not plan on recovering a cancelled task's work. This is a wrong promise in the help text,
> not a data-loss bug.

---

## 11. Driving it from a script

The whole flow is expressible in JSON and exit codes, with no English parsed anywhere. `grid task
get` is the piece that makes waiting expressible:

| exit | meaning |
|---|---|
| `0` | completed |
| `1` | failed, timed out, **or any refusal** |
| `2` | not finished yet — the only code a poller may read as "ask again" |

```bash
until grid task get "$id" >/dev/null; do
  rc=$?
  [ "$rc" -eq 2 ] || exit "$rc"        # it finished, and not well
  sleep 10
done
```

Dropping the `rc` check tasks this into a loop that waits forever on a task that failed.

Refusals arrive as `{"error":{"code":…,"message":…,"status":…}}`. Two gotchas measured on the run:

- **`--json` writes the envelope to stderr, and stdout stays empty.** Read stderr for the reason.
- **Read the FIRST line of stderr.** Some refusals put the JSON on line 1 and a plain English
  sentence on line 2; `json.load(stderr)` on the whole stream fails.

Useful machine-readable reads: `grid project create --json` (check `bootstrap.status` is
`initialized`), `grid project status --json`, `grid task list --json` with no `--project` for your
conversations across every project you can reach.

---

## 12. Stop and clean up

```bash
# terminal 3: Ctrl-C, then
grid leave demo                 # stop and unregister the engine
# terminal 1
grid project archive <project-id>     # keeps everything, accepts no new work
grid stop demo                        # take the grid offline; config persists
```

`archive` destroys nothing and every read still works — it is the right way to retire a project.
`grid project delete` refuses anything that holds work (`project_not_empty`), by design; it exists
for the project you created by a typo.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Every task fails, `the agent exited 1:` with no message | `GRID_TASK_CLAUDE_CONFIG_DIR` points at a directory with no login | `/login` in that directory, or unset the variable (§3) |
| `Claude Code isn't installed on this provider` | `PATH` had a shim that has since vanished | Restart the provider with the real directory first on `PATH` (T1) |
| Tasks complete but the agent says its writes were blocked | Task root inside `$HOME` | Move `GRID_TASK_ROOT` outside `$HOME` (T3) |
| Provider printed its banner then went quiet | Port 8099 already held by another seat | `lsof -nP -iTCP:8099`; free it or `--seat-port 8199` (T4) |
| `grid join` → `503 routing temporarily unavailable` | Grid still warming up | Wait ~90s, retry |
| A grid appeared somewhere you did not expect | That home's stored `api_url` outranks `GRID_CONTROL_PLANE_URL` | T2 — check the file, not the environment |
| Nothing is claimed, no errors | `GRID_TASKS` not set | It is off by default and never turns itself on |
| Provider stopped claiming, `task.rate_limit` in the log | Subscription window spent | It resumes by itself when the vendor's window resets |
| `grid leave demo` → `Grid not found: 'demo'` while `grid ls` lists it | Observed; cause not established | Use the grid id: `grid leave grid-…` |
| `git push` to a project is refused | The grid is `main`'s only writer | `grid task create`, or `grid project commit <conversation-id>` for files with no agent |

---

## What this walkthrough does not show you

Stated so the sections above are not read as more than they are:

- **Two providers.** One machine claims everything here. Cross-provider resume — a conversation
  continuing on a different provider — is designed for and tested elsewhere, but you cannot see it
  with one node. (You *can* see the mechanism: stop the provider, delete `GRID_TASK_ROOT` entirely,
  restart, and send another message. The agent still remembers the conversation, because the session
  travels with the project and not with the disk.)
- **A large repository.** Three files. Import cost and per-conversation disk cost both scale, and a
  conversation costs a working tree rather than a second copy of history — about 306 MiB against
  1.04 GiB on a 792 MiB repository.
- **Anything under load.** Two members and two workers is enough to show the behaviour and nowhere
  near enough to say anything about throughput.
