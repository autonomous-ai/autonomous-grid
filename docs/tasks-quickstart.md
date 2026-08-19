# Distributed tasks — command guide

Hand the grid a coding task, let a provider run an agent on it, read the result back.

A **task** is a conversation that outlives the request that created it. You post a prompt; a provider
claims the work, runs a coding agent against your project's code, pushes the result, and reports. You
come back later for it, and you reply into the same conversation. Nothing is held open, so one run
can take an hour.

**Two words carry the weight on this page.** A *task* is the conversation — one Claude Code session,
something you come back to. A *turn* is one run inside it: one message you send, one agent run, one
provider. `grid task create` starts a conversation and its first turn, `grid task send` adds the next
turn, and every other `grid task` command — `get`, `follow`, `fetch`, `cancel` — addresses a single
**turn**.

Everything here is **remote mode only** (`grid mode remote`).

---

## The pieces, in the order you meet them

| Thing | What it is |
|---|---|
| **project** | A git repository the grid hosts, with its own members. Addressed by **id**, never by name. |
| **`main`** | The project's trunk, and everybody's work. It is created once — by `init` or by `import` — and afterwards **the grid moves it itself**, applying every turn that succeeds. |
| **task** | A conversation: one Claude Code session, many turns, one thing you name and return to. |
| **turn** | One message and one agent run inside a task. Cut from the conversation's branch, settles back onto it. |
| **`wip/<conversation-id>`** | A conversation's branch — **one per conversation**, not one per person. You never push to it; the grid writes it, and applies what it holds to `main`. |
| **member_key** | A 32-hex id for you *in this grid*. Printed by `grid project member list`. Not your email, and **different in every grid**. |
| **provider** | A machine running `grid join` with task serving on. It runs the agent and pays for it with its own Claude subscription. |

---

## Setting up a project

```bash
grid project create --name my-app             # prints the project id — keep it
grid project list                             # id, your role, name
grid project list --all                       # ...including archived ones, marked
```

A new project has **no `main`**, and a task cannot be created without one. There are **two** ways to
give it one, and you cannot `git push main` yourself either way.

**Starting from nothing:**

```bash
grid project init <project-id>                # one empty commit becomes `main`
```

**Bringing a repository you already have:**

```bash
grid project import ./my-app <project-id>     # local repo → the project's trunk
grid project import ./my-app <project-id> --branch release
```

Import is slow on a big repository (the relay reads every tree the history reaches; ~20s on 29,000
commits) and it is refused if the repository has a submodule, a path under `.grid/`, or a symlink
pointing outside the repo. `.claude/` is fine. Git LFS imports with a warning: an agent will see
pointer files.

⚠️ **Pick the right one — the choice cannot be taken back.** Both are refused on a project that
already has a trunk, so a project you initialized empty can never import a repository afterwards.
Recovering means a new project, and a new id. Nothing does either on your behalf.

`grid project delete` does **not** get the id back, and never will: a project with a trunk is
exactly what it refuses. What it is for is the project you created by a typo and never used. Once
this one has a trunk, `grid project archive` is what gets it out of your list.

A refused import leaves the project with **no trunk on purpose** — fix what it names and import
again, or import into a fresh project.

### Members

```bash
grid project member list <project-id>                        # member keys + emails
grid project member add  <project-id> --email you@corp.com
grid project member remove <project-id> <member_key>
```

Someone must have **signed in to the grid at least once** before they can be added to a project on
it — the relay only knows people it has authenticated. If `member add` says no member has that
address, have them run `grid login` and then any relay command (`grid project list` will do).

---

## Running tasks

```bash
grid task create <project-id> --prompt 'add a retry to the upload path'
grid task create <project-id> --prompt 'use this config' --file ./conf.toml:config/conf.toml
grid task create <project-id> --prompt 'make these pass' --dir ./fixtures:test/data
grid task send   <conversation-id> --prompt 'now cover the timeout case'
```

`create` starts a conversation; `send` adds the next turn to one that already exists, so the agent
still knows what you were talking about. **`create` prints the conversation id** — keep it, because
it is what `send`, `grid project commit` and `grid project wip reset` all take. `grid task get
<turn-id> --json` reports the conversation a turn belongs to if you lose it.

Turns of one conversation run **in the order you sent them, one at a time**, so you can type ahead;
different conversations run alongside each other, yours and everybody else's. Only the person who
started a conversation can send into it — a colleague reads its turns and works alongside you by
starting their own.

> ⚠️ A message sent **while an earlier turn is still running** is accepted and queued, but it starts
> from the project as it stood before that earlier turn, so the two do not yet combine and the second
> may end `retries_exhausted`. Until that is fixed, reply after the previous answer comes back.

Every command on this page that takes a project id also accepts it as `--project <project-id>`, and
the two are the same thing — `grid task list <id>` and `grid task list --project <id>` do exactly
the same. Both spellings work in both command groups, so it never depends on which one you are in.
The commands addressed by a **conversation** — `grid task send` and `grid project commit` — take it
positionally and have no flag form; `grid project wip reset <project-id> <conversation-id>` takes
both, and only the project id has one.

`--file LOCAL[:DEST]` uploads a file with the task, repeatable. It is committed **before** any
provider can claim the task, so the agent always finds it.

`--dir LOCAL[:DEST]` uploads a folder the same way. Inside a git work tree your `.gitignore` is
honoured, and `.git/`, `.grid/`, `.claude/`, `.mcp.json` and symlinks are skipped — every skipped
path is printed, so nothing goes missing quietly. The two flags share one budget of 200 files and
20 MB. For a whole repository use `grid project import` instead; `--dir` is for the folder of
fixtures or assets that is not one.

**Starting from nothing, in one call:**

```bash
grid task create <project-id> --init-project --prompt 'scaffold a FastAPI service'
```

`--init-project` gives the project its empty trunk first, then runs the task — `grid project init`
and `grid task create` in one. Same one-way door as `init` itself: a project that has a trunk can
never import an existing repository, so use `grid project import` if you have one. Your uploaded
files go on **the new conversation's branch**; they reach the trunk when the turn succeeds, not
before. Passing it at a project that already has a trunk does nothing and the task simply runs.

With no project id at all, the task goes to your own project called `default` **if you already have
one**. If you do not, nothing is created and the command tells you which project it needs.
`task create` and `task list` are the two commands that may leave it out, and they mean different
things by it: `create` falls back to your `default` project, while `list` with no project shows your
own turns **across every project you can reach** — one list, one cursor, which is what an
application's home screen is.

```bash
grid task follow <task-id>          # live: tool calls, output, terminal state
grid task get    <task-id>          # one shot: state, provider, result
grid task list   [<project-id>] [--all] [--state running] [--limit 50]
grid task fetch  <task-id> --into /tmp/result
grid task cancel <task-id>
```

`<task-id>` is a **turn's** id, which is what these five commands address — it is a different id from
the conversation's, and both are printed when a turn is created.

To watch the **whole conversation** rather than one turn — every turn's output in order, including
steps the grid adds itself when your work collides with a colleague's:

```bash
grid task follow --conversation <conversation-id>
```

It ends when the conversation goes quiet, and exits `0` for having watched it to that end. It does
not report a turn's outcome; a conversation does not have one.

**Create and watch in one command**, and act on how it went:

```bash
grid task create <project-id> --prompt 'add a retry' --follow && ./deploy.sh
```

`--follow` prints the id, then watches the turn with the same resumable stream `grid task follow`
uses. Ctrl-C stops the watching, never the turn — the id is on screen first so you can reattach.

Both `follow` and `get` exit with the turn's own outcome. `get` has a third code, for a turn that has
not finished:

| state | `grid task get` exit |
|---|---|
| `completed` | `0` |
| `failed`, `timed_out` | `1` |
| `preparing`, `queued`, `running` | `2` |

```bash
until grid task get "$id"; do
  rc=$?
  [ "$rc" -eq 2 ] || exit "$rc"   # it finished, and not well
  sleep 30
done
```

Read the code in that loop: `until grid task get "$id"; do sleep 30; done` alone ends only on
success, so it waits forever on a task that failed. `2` is the only code meaning "ask again" — `1`
covers a failed task *and* a command that could not reach the relay. Name the variable `rc`, not
`status`: in zsh `status` is a read-only alias for `$?`, so that loop would bail on its first poll
and blame a running task.

`--all` on `task list` shows every member's turns, not only yours. It needs a project id: the
grid's work is listed one project at a time, and "everyone's, everywhere" is refused rather than
quietly narrowed back to your own.

**One turn of a conversation runs at a time, and that is the only queue there is.** Creating a
conversation never fails for want of capacity, and your other conversations keep running while one of
them works. A turn that cannot start yet waits in the project's queue; `grid project status` says how
deep that queue is and how many providers could be taking it.

`cancel` frees the conversation to take its next turn immediately; the agent stops within about half
a minute. Nothing is rewound — the turn's branch is left where the agent got to, so `fetch` still
works on it — and **the conversation survives**, so the next message you send continues where it left
off. Any member may cancel any turn in the project, and the event log records who did.

---

## Working with the result

```bash
grid project clone <project-id> /tmp/my-app     # a real git repo, on the project's TRUNK
grid project status <project-id>                # the trunk, your turns in flight, the queue
```

The clone puts you on `main`, because the trunk is now everybody's work — the grid applies every
successful turn to it, yours included. It runs `grid credential` whenever git needs a token, so
nothing is written to disk and a refreshed token is picked up automatically. **`git push` is
refused** — that is the design, not a permission to request: the project is written by the grid alone
so a turn running right now cannot have the ground moved under it.

Re-running `clone` on the same directory updates it. Day to day, plain `git fetch` is lighter.

To land a change without an agent:

```bash
grid project commit <conversation-id> -m 'fix the last line' --file ./patch.py:src/app.py
grid project commit <conversation-id> -m 'add the fixtures' --dir ./fixtures:test/data
grid project commit <conversation-id> -m 'drop the old shim' --delete src/shim.py
```

It takes a **conversation id, and no project id** — the change goes onto that conversation's branch,
so the next message you send there starts from it, and the grid applies it to the trunk by itself
exactly as it applies a turn. There is nothing to run afterwards. It is refused while that
conversation has a turn in flight, naming the turn; your other conversations are unaffected.

Executable bits look after themselves: editing a file the project already has as executable keeps it
executable. `--delete` on a path that is not in that conversation's branch is **refused** rather than
quietly ignored — git's own answer there is to report success and do nothing.

---

## Sharing work: there is nothing to run

**The grid applies every successful turn to `main` itself** — a fast-forward when it can, and a merge
commit when git can combine the two. Your work appears in the project because it finished, not
because you asked for it.

When git cannot combine them — you and somebody else changed the same lines — nothing moves yet, and
the turn is held. Resolving that inside the conversation that caused the collision is the next
release's work; until it lands, a held turn is what you see, and it is the one case where a finished
turn does not show up as a moved trunk commit.

`grid project promote`, `grid project integrate` and `grid project check` **no longer exist**. They
were built for a developer holding a release: `main` was a branch somebody decided to move, and once
one person had moved it everybody else had to integrate before they could. Nothing in that loop can
be done by somebody who does not read diffs, and the visible symptom was work that finished and then
never appeared.

Two things that follow, and they are the cost rather than the benefit:

- **The trunk has no known-good property left.** Nothing asserts that `main` builds or runs. Nothing
  did before either, except a person's judgement at promote time — and that person is gone.
- **A conversation of five turns puts four intermediate states into the project.** Colleagues used to
  see each other's work when somebody decided it was ready. They now see it at every turn. That is
  the deliberate trade for removing the human, not an oversight.

The apply happens **just after** the turn is reported finished, not in the same instant, so "done"
and "in the project" are a moment apart. Watch `grid project status`: the trunk's own commit moves
whenever anybody's work lands, so one id is the whole change signal.

### When a conversation's branch needs rewinding

```bash
grid project wip reset <project-id> <conversation-id> --commit <sha>
```

This survived the commands above, deliberately. If a turn's result is written into git but its
completion is then interrupted, the conversation's branch is left ahead of the turn branch, every
retry is refused, and that conversation's *next* turn would be cut from the lost attempt's work.
Nothing else moves a branch backwards — members do not push, the grid's apply only ever writes the
trunk, and there is no revert.

`grid task get <turn-id> --json` reports the `base_commit` a turn was cut from, which is usually the
commit you want. It is refused while that conversation has a turn running.

---

## Commands that are easy to confuse

### `grid project archive` vs `grid project delete`

| | What happens | Reversible | Allowed when |
|---|---|---|---|
| `grid project archive <id>` | stops new work, hides it from `list` | **yes**, `unarchive` | always |
| `grid project delete <id>` | removes the project **and its repository** | **no** | it has no trunk and has never had a task |

Archive is the one you want almost every time. It destroys nothing — the repository is kept and
every read still works, so `clone`, `status`, `task list` and `task fetch` all carry on — and
`grid project unarchive <id>` puts it back completely.

Delete exists for the project created by a typo. It is refused for anything that holds work, naming
archive, so it cannot take a colleague's conversation with it.

Neither cancels a turn that is already running. Archiving stops new work **starting**; to stop work
that has already started, use `grid task cancel <task-id>`.

### `grid members` vs `grid project member`

| | Scope | Email is |
|---|---|---|
| `grid members add <grid> <email> --role both` | the **grid** — who may use or serve it | a **positional** |
| `grid project member add <project-id> --email <email>` | a **project** inside that grid | a **flag** |

Different scopes and different argument shapes. Grid membership comes first; a project membership
for someone who is not on the grid is refused. And someone added to the grid *after* they signed in
must `grid login` again — scopes are baked into the token when it is minted, so `grid sync` is not
enough.

### `grid task create` vs `grid task send`

Both send a message to an agent. One of them starts a new conversation.

| | Takes | Starts a new session? |
|---|---|---|
| `create <pid> --prompt …` | a **project** id (or none, for your `default`) | **yes** — a fresh conversation, and prints its id |
| `send <cid> --prompt …` | a **conversation** id | **no** — the same session, still knowing what you said |

Reach for `send` when the answer was nearly right; reach for `create` when you are starting something
unrelated. Two conversations run alongside each other, so a second `create` is not a queue.

### `grid project commit` vs `grid task create --file`

Both put files in. Only one of them then lets an agent loose on them.

- `project commit <conversation-id>` — the files land on that conversation's branch, full stop, and
  the grid applies them to the trunk. This is "the agent got it 90% right, let me fix the last line".
- `task create --file` (and `task send --file`) — the files are committed **and then** an agent runs,
  which may change the very line you were fixing.

Both occupy a conversation's one running turn while they work, so neither can land underneath a run
of its own.

### `grid project clone` vs `grid task fetch`

- `clone` — a working copy of the **project**, on the trunk, that you keep and update. For working.
- `fetch` — the result of **one turn**, into a directory. For reading what an agent did.

`fetch` without `--into` creates `./<task-id>` in the current directory. Run it inside another git
repo and you get nested repos that `git status` shows as untracked — and a mistaken `cd` then makes
`git log` answer from the enclosing repo, which looks completely valid. **Fetch outside any repo.**

### `grid project refresh` vs `grid project status`

Only one of them prints a "behind", and they answer different questions.

| | Reports | Asks | Needs |
|---|---|---|---|
| `refresh <pid>` | your **clone** against the grid's copy of the branch you are on | nothing — git only | to be run in a clone |
| `status <pid>` | where the **project** is: its trunk, your turns in flight, the queue | the relay | nothing local |

So `refresh` answers "has anything landed since I last looked", and `status` answers "what is
happening in this project right now". `status` stopped measuring a distance when promote went: the
distance it used to print was how far a branch was from being promotable. `refresh` never moves your
files; `status` never looks at them.

### `grid project refresh` vs re-running `grid project clone`

Both update a clone. Only one can lose work.

- `refresh` — fetches and reports. Never touches your working tree, so it works with local commits, a
  dirty tree, or a turn in flight.
- `clone` over the same directory — **resets** your branch to the fetched tip, and therefore refuses
  outright when you have commits the grid has not seen.

### the project id vs `--grid`

- `<project-id>`, or `--project <id>` — which project, a uuid from `grid project list`. **A
  positional id and `--project` are the same thing** on every command that takes one; giving both
  with different values is refused rather than one being quietly preferred.
- `--grid <name-or-id>` — which grid, when you have more than one. Defaults to the active grid.
  Always a flag, never positional.

### `grid task list` vs `grid list`

`grid task list <project-id>` lists a project's turns, and `grid task list` with no id lists your
own across every project. `grid list` (alias of `grid ls`) lists **grids**.

### `grid task cancel` vs `grid leave`

`cancel` stops one turn. `leave` stops this machine serving a grid at all — it is a provider
command and has nothing to do with tasks you submitted.

### `main` vs a conversation's branch

`grid project clone` puts you on `main`, and that is the change worth internalising: the trunk holds
everybody's finished work, because the grid applies every successful turn to it. A conversation's own
branch, `wip/<conversation-id>`, is the scratch space in between — a turn is cut from it and settles
back onto it, **whether the turn succeeded or failed**, which is what lets the next message carry on
from where the last one broke. Only success reaches `main`.

You never name a conversation's branch to git. The two commands that take a conversation id —
`grid project commit` and `grid project wip reset` — are the whole of your dealings with it.

### member_key vs email vs the id in your token

`member_key` is `sha256(user_id)` truncated, and `user_id` includes the grid — so **the same person
has a different member_key in every grid**. Copying a key from another grid's notes gives you a
`grid project member remove` aimed at somebody who is not there. Always read it from
`grid project member list <project-id>`. It is no longer a branch name: branches are named after
conversations.

---

## Running a provider

A provider needs a Claude Code login, and it pays for every task out of that subscription. Task
serving is **opt-in and off by default**.

```bash
export GRID_TASKS=1                 # required — nothing claims tasks without it
export GRID_MAX_TASKS=2             # turns run at once (default 1)
export GRID_TASK_ROOT=/var/grid     # where workspaces live — keep it SHORT (see below)
grid join <grid> --api claude       # a provider must join an engine; the task loop lives inside it
```

| Variable | Default | Meaning |
|---|---|---|
| `GRID_TASKS` | off | `1`/`true`/`yes`/`on` to claim tasks |
| `GRID_MAX_TASKS` | `1` | concurrent turns |
| `GRID_TASK_ROOT` | `/var/grid` | workspace root — `<root>/projects/<project>/<member>/<conversation>/workspace`, with the member's one copy of the project's history beside it at `<member>/store.git`. **Keep it short**: the whole path becomes one directory name for the agent's transcript, and grid adds ~126 characters below the root. A provider refuses a workspace whose name would exceed the limit rather than losing the conversation silently |
| `GRID_TASK_MAX_WORKSPACES` | `8` | how many conversations keep a working directory here. Past this the least recently used are deleted before the next turn — a turn is never refused for disk, and a workspace in use is never touched. An evicted conversation comes back on its next turn, same session and same files, at the cost of one fetch |
| `GRID_TASK_MIN_FREE_GB` | off | keep evicting while free space on the task root's filesystem is below this |
| `GRID_TASK_TIMEOUT_SECONDS` | `3600` | budget for one run |
| `GRID_TASK_SANDBOX` | on | `0` disables confinement — for debugging only |
| `GRID_TASK_ALLOWED_DOMAINS` | — | hosts a task's own commands may reach |
| `GRID_TASK_ENV_PASSTHROUGH` | — | variables to pass to the agent |
| `GRID_TASK_CLAUDE_CONFIG_DIR` | — | a dedicated Claude config directory |

**On Linux**, install `bubblewrap` **and** `socat`, and Claude Code ≥ 2.1.221. Without them the
provider refuses to run tasks rather than running them unconfined.

**On macOS**, leave `GRID_TASK_CLAUDE_CONFIG_DIR` unset. The credential lives in the Keychain, and
setting the variable — even to the default path — makes Claude Code look for a credentials file that
is not there. The symptom is every task failing with `the agent exited 1:` and an empty message;
`grid task follow` shows `Not logged in`.

On macOS the sandbox also blocks the system trust-evaluation service, so a package manager
that uses the system trust store cannot verify a certificate inside a task — `pip install` fails with
`SSLError(... 'OSStatus -26276')` even with the index host allowed, while `curl` is unaffected, which
makes it look like a network fault. Measured on the same machine under the same policy, `uv pip
install` succeeds: it carries its own roots. Prefer `uv` in a task's prompt, or run providers on
Linux for work that installs dependencies.

The agent runs with the operator's **user-scope** settings and never the repository's, so a
`.claude/settings.json` arriving in a task's branch cannot run anything. One consequence worth
knowing: a repository's `CLAUDE.md` is **not** loaded into the agent's context either, though it
stays on disk and readable.

---

## Working as a team

The shape below is what the design assumes. It is not enforced — nothing reviews what an agent
wrote before it reaches the trunk — so treat it as the agreement a team makes with itself.

### Once, when the project starts

1. One person runs `grid project create`, then gives the project a trunk — `grid project import` for
   a repository that already exists, or `grid project init` to start empty. Import the repository you
   actually want, with its history; you get one shot per project, and it is the same shot either
   way.
2. That person adds everyone: `grid members add` for the grid, then
   `grid project member add` for the project.
3. Everyone runs `grid project clone` and keeps that working copy.
4. Decide who runs providers. One provider serves the whole team, and `GRID_MAX_TASKS` is how many
   turns the team can run at once — not how many each person gets. What each person gets is the
   grid's own **running cap** (`TASK_MEMBER_RUNNING_CAP`, 3 by default, set on the relay): the most
   turns one member may have running at a time, so somebody working quickly cannot put twenty turns
   in front of a colleague's one. Nothing is ever refused because of it — a turn over the cap waits
   for one of that person's own to finish, and `grid project status` shows where they stand.

### Every day

Each member works in their **own conversations**, and a conversation's branch is nobody else's. Turns
inside one are sequential; everything else runs at the same time as everything else, so two people —
or two of your own conversations — never block each other.

```
morning     grid project status <pid>          # what did the team land overnight?
            git -C ~/my-app fetch             # or grid project refresh, in the clone

work        grid task create <pid> --prompt '…'         # a new conversation
            grid task follow <turn-id>
            grid task send   <conversation-id> --prompt '…'   # the answer was nearly right
            grid task fetch  <turn-id> --into /tmp/r          # read it
            grid project commit <conversation-id> -m '…' --file …   # touch up without an agent
```

There is no third stage. Every turn that succeeds is applied to `main` by the grid, so the work you
did this morning is in the project before you go looking for it.

### The four rules that matter

**1. The trunk moves without asking, so read it often.** `main` now changes whenever anybody's turn
lands — several times a day on a team of five. Fetching in the morning is no longer a courtesy;
it is how you find out what the project became overnight.

**2. Nothing reviews what an agent wrote.** There is no gate between an agent's code and the trunk,
and there was only ever one before — a person's judgement at promote time. `main` is not a
known-good branch: nothing asserts that it builds or that its tests pass. If your team needs that
property, it is work you have to add.

**3. A conversation is the unit of work, so name it in your head.** Five turns in one conversation
put four intermediate states into the project, which colleagues see. That is the price of nobody
having to promote. Prefer one conversation that finishes a thing to five that each leave it half
done, and start a separate conversation for unrelated work rather than steering an existing one.

**4. Spread work across conversations, not across turns.** Turns of one conversation are strictly
ordered, so three follow-up messages cost three waits; three *conversations* cost one, because they
run at the same time. And for now, wait for each answer before replying — a message sent while the
previous turn is still running is accepted but does not yet build on it. If a turn is stuck,
`grid task cancel` frees its conversation immediately, and the conversation itself survives.

### Reading the team's state

```bash
grid project status <pid>                    # the trunk, your turns in flight, the queue
grid task list <pid> --all                   # what the whole team is running right now
grid project member list <pid>               # who is in the project, and their keys
```

The trunk's commit is the whole change signal: it moves whenever anybody's work lands, so an
application — or a person — watches one id instead of one per member.

### What no one can do

- Push to the project. Work lands through a turn or `grid project commit`.
- Move `main` by hand. It is created by `init` or `import`, once, and afterwards only the grid's own
  apply advances it.
- Run two turns of one conversation at once, or move a branch a running turn was cut from.
- Read another project's code, even knowing its id.
