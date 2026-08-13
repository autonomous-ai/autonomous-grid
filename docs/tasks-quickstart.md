# Distributed tasks — command guide

Hand the grid a coding task, let a provider run an agent on it, read the result back.

A **task** is work that outlives the request that created it. You post a prompt; a provider claims
it, runs a coding agent against your project's code, pushes the result, and reports. You come back
later for it. Nothing is held open, so a task can take an hour.

Everything here is **remote mode only** (`grid mode remote`).

---

## The pieces, in the order you meet them

| Thing | What it is |
|---|---|
| **project** | A git repository the grid hosts, with its own members. Addressed by **id**, never by name. |
| **`main`** | The project's trunk. It is created once — by `init` or by `import` — and afterwards only a promote moves it. |
| **your WIP branch** | `wip/<member_key>` — where your work lands. One per member. You never push to it; the grid writes it. |
| **task** | One agent run. Cut from your WIP branch, settles back onto it. |
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
grid task create --project <project-id> --prompt 'add a retry to the upload path'
grid task create --project <project-id> --prompt 'use this config' --file ./conf.toml:config/conf.toml
```

`--file LOCAL[:DEST]` uploads a file with the task, repeatable. It is committed **before** any
provider can claim the task, so the agent always finds it.

**Starting from nothing, in one call:**

```bash
grid task create --project <project-id> --init-project --prompt 'scaffold a FastAPI service'
```

`--init-project` gives the project its empty trunk first, then runs the task — `grid project init`
and `grid task create` in one. Same one-way door as `init` itself: a project that has a trunk can
never import an existing repository, so use `grid project import` if you have one. Your uploaded
files go on **your branch**, never on the trunk. Passing it at a project that already has a trunk
does nothing and the task simply runs.

With no `--project` at all, the task goes to your own project called `default` **if you already have
one**. If you do not, nothing is created and the command tells you which project it needs.

```bash
grid task follow <task-id>          # live: tool calls, output, terminal state
grid task get    <task-id>          # one shot: state, provider, result
grid task list   --project <project-id> [--all] [--state running] [--limit 50]
grid task fetch  <task-id> --into /tmp/result
grid task cancel <task-id>
```

`--all` on `task list` shows every member's tasks, not only yours.

**You may have one task in flight per project at a time.** A colleague's task in the same project
does not block yours — the limit is per member, not per project. Creating a second one while yours
is running is refused, and the refusal names the task holding your slot.

`cancel` frees your slot immediately; the agent stops within about half a minute. Nothing is
rewound — the task's branch is left where the agent got to, so `fetch` still works on it. Any member
may cancel any task in the project, and the event log records who did.

---

## Working with the result

```bash
grid project clone <project-id> /tmp/my-app     # a real git repo, on YOUR branch
grid project status <project-id>                # main, your branch, ahead/behind, your task slot
```

The clone runs `grid credential` whenever git needs a token, so nothing is written to disk and a
refreshed token is picked up automatically. **`git push` is refused** — that is the design, not a
permission to request: your branch is written by the grid alone so a running task cannot have the
ground moved under it.

Re-running `clone` on the same directory updates it. Day to day, plain `git fetch` is lighter.

To land a change without an agent:

```bash
grid project commit <project-id> -m 'fix the last line' --file ./patch.py:src/app.py
grid project commit <project-id> -m 'drop the old shim' --delete src/shim.py
```

Executable bits look after themselves: editing a file the project already has as executable keeps it
executable. `--delete` on a path that is not in your branch is **refused** rather than quietly
ignored — git's own answer there is to report success and do nothing.

---

## Sharing work: integrate, then promote

`main` moves only when somebody promotes. So the first promote leaves everyone else cut from a trunk
that is now history, and integration is the way back.

```bash
grid project check     <project-id>          # what would integrating do? costs nothing
grid project integrate <project-id>          # bring main into YOUR branch
grid project promote   <project-id> <member_key>   # move main to that member's branch
```

`integrate` reports one of four outcomes:

| `status` | Meaning |
|---|---|
| `up_to_date` | your branch already has everything on `main` |
| `fast_forward` | your branch moved straight onto `main` |
| `merged` | the two were merged; a merge commit is on your branch |
| `merge_task` | you and somebody else changed the same lines — the grid queued an **agent** to resolve it |

A `merge_task` costs an agent run and holds your task slot; nothing has moved when the command
returns. Watch it with `grid task follow`, read the resolution, then promote.

The grid verifies that the merge **happened** — that the result really contains `main`. It cannot
verify that the resolution is *right*. Read it before you promote.

`promote` is fast-forward only. A branch behind `main` is refused, saying how far behind.

> **Promote cannot be undone by pushing, and there is no revert for it in this release.** The commit
> it replaced is printed; putting it back is an operation on the relay itself. Code an agent wrote
> reaches `main` if you promote without reading it.

Recovering a WIP branch left ahead of a lost attempt:

```bash
grid project wip reset <project-id> <member_key> --commit <sha>
```

`grid task get` prints the `base_commit` a task was cut from, which is usually the commit you want.

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
archive, so it cannot take a colleague's unpromoted branch with it.

Neither cancels a task that is already running. Archiving stops new work **starting**; to stop work
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

### `grid project promote` vs `grid project integrate`

They move in **opposite directions**, and only one takes a member key.

| | Direction | Takes a member key? | Costs |
|---|---|---|---|
| `promote <pid> <key>` | that member's WIP → `main` | **yes** — any member's | nothing |
| `integrate <pid>` | `main` → **your** WIP | **no**, always yours | your task slot; an agent if it conflicts |

`integrate` takes no key because the relay holds *your* task slot while it works — a request naming
someone else's branch would take the wrong person's slot while moving a ref their running task was
cut from.

### `grid project check` vs `grid project integrate`

Same question, one is free.

- `check` — a pure read. No ref moves, no task is created, no slot is held. It answers even while
  you have a task in flight, which is exactly when `integrate` refuses you.
- `integrate` — actually does it, and on a conflict queues a paid agent run.

Ask with `check` first when you are deciding; use `integrate` when you have decided.

### `grid project commit` vs `grid task create --file`

Both put files in. Only one of them then lets an agent loose on them.

- `project commit` — the files land on your branch, full stop. This is "the agent got it 90% right,
  let me fix the last line".
- `task create --file` — the files are committed **and then** an agent runs, which may change the
  very line you were fixing.

Both spend your one task slot while they work.

### `grid project clone` vs `grid task fetch`

- `clone` — a working copy of the **project**, on your branch, that you keep and update. For working.
- `fetch` — the result of **one task**, into a directory. For reading what an agent did.

`fetch` without `--into` creates `./<task-id>` in the current directory. Run it inside another git
repo and you get nested repos that `git status` shows as untracked — and a mistaken `cd` then makes
`git log` answer from the enclosing repo, which looks completely valid. **Fetch outside any repo.**

### `grid project refresh` vs `grid project status`

Both print a "behind", and they mean different things.

| | Compares | Asks | Needs |
|---|---|---|---|
| `refresh <pid>` | your **clone** against the grid's copy of the branch you are on | nothing — git only | to be run in a clone |
| `status <pid>` | your **branch** against `main` | the relay | nothing local |

So `refresh` answers "has anything landed since I last looked", and `status` answers "am I far enough
along to promote". `refresh` never moves your files; `status` never looks at them.

### `grid project refresh` vs re-running `grid project clone`

Both update a clone. Only one can lose work.

- `refresh` — fetches and reports. Never touches your working tree, so it works with local commits, a
  dirty tree, or a task in flight.
- `clone` over the same directory — **resets** your branch to the fetched tip, and therefore refuses
  outright when you have commits the grid has not seen.

### `--project` vs `--grid`

- `--project <id>` — which project, a uuid from `grid project list`.
- `--grid <name-or-id>` — which grid, when you have more than one. Defaults to the active grid.

### `grid task list` vs `grid list`

`grid task list --project <id>` lists tasks. `grid list` (alias of `grid ls`) lists **grids**.

### `grid task cancel` vs `grid leave`

`cancel` stops one task. `leave` stops this machine serving a grid at all — it is a provider
command and has nothing to do with tasks you submitted.

### `main` vs your branch

`grid project clone` puts you on `wip/<your key>`, not on `main`, and that is deliberate: `main`
moves only when somebody promotes, so it may hold none of your work. `grid project status` prints
both.

### member_key vs email vs the id in your token

`member_key` is `sha256(user_id)` truncated, and `user_id` includes the grid — so **the same person
has a different member_key in every grid**. Copying a key from another grid's notes gives you a
`promote` aimed at a branch that does not exist. Always read it from
`grid project member list <project-id>`.

---

## Running a provider

A provider needs a Claude Code login, and it pays for every task out of that subscription. Task
serving is **opt-in and off by default**.

```bash
export GRID_TASKS=1                 # required — nothing claims tasks without it
export GRID_MAX_TASKS=2             # tasks run at once (default 1)
export GRID_TASK_ROOT=/var/grid     # where workspaces live
grid join <grid> --api claude       # a provider must join an engine; the task loop lives inside it
```

| Variable | Default | Meaning |
|---|---|---|
| `GRID_TASKS` | off | `1`/`true`/`yes`/`on` to claim tasks |
| `GRID_MAX_TASKS` | `1` | concurrent tasks |
| `GRID_TASK_ROOT` | `/var/grid` | workspace root |
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

The shape below is what the design assumes. It is not enforced — nothing stops you promoting
unreviewed code — so treat it as the agreement a team makes with itself.

### Once, when the project starts

1. One person runs `grid project create`, then gives the project a trunk — `grid project import` for
   a repository that already exists, or `grid project init` to start empty. Import the repository you
   actually want, with its history; you get one shot per project, and it is the same shot either
   way.
2. That person adds everyone: `grid members add` for the grid, then
   `grid project member add` for the project.
3. Everyone runs `grid project clone` and keeps that working copy.
4. Decide who runs providers. One provider serves the whole team, and `GRID_MAX_TASKS` is how many
   tasks the team can run at once — not how many each person gets.

### Every day

Each member works on their **own WIP branch**, which nobody else can move. So the branch is never
contended, and two members never block each other's tasks.

```
morning     grid project status <pid>          # did main move overnight?
            grid project check  <pid>          # free — would integrating be clean?
            grid project integrate <pid>       # if it moved, take it now, not at release time

work        grid task create --project <pid> --prompt '…'
            grid task follow <task-id>
            grid task fetch  <task-id> --into /tmp/r   # read it
            grid project commit <pid> -m '…' --file …  # touch up without an agent

release     grid project integrate <pid>       # again, main may have moved while you worked
            <read the diff>
            grid project promote <pid> <your key>
```

### The four rules that matter

**1. Integrate early, promote small.** `main` moves only on a promote, so every promote invalidates
everyone else's branch until they integrate. A team that promotes daily has cheap integrations; a
team that promotes monthly has one enormous conflict and a merge task for each member.

**2. Read before you promote.** Promote is the only thing that moves `main`, it is fast-forward
only, and **it cannot be reverted in this release**. An agent's code reaches your trunk the moment
you promote. `grid project clone` + `git log main..wip/<your key>` is the review.

**3. A merge task is a merge, not a verdict.** When integration conflicts, an agent resolves it and
the grid checks the merge *happened* — not that it is *correct*. Somebody has to read a merge task's
result, and the person who queued it is the one who knows what both sides meant.

**4. One task in flight each — plan around it, not against it.** Splitting one big prompt into three
sequential tasks costs three waits. Splitting work across three *people* costs one. If a task is
blocking you, `grid task cancel` gives your slot back immediately.

### Reading the team's state

```bash
grid project status <pid>                    # your branch, and who holds your slot
grid task list --project <pid> --all         # what the whole team is running right now
grid project member list <pid>               # keys, for promoting somebody else's branch
```

Any member may promote any member's branch, including someone who has left — nothing else can move
their branch once they are gone.

### What no one can do

- Push to the project. Work lands through a task, `project commit`, or `integrate`.
- Move `main` other than by promoting. It is created by `init` or `import`, once, and never again.
- Take somebody else's task slot, or move a branch a running task was cut from.
- Read another project's code, even knowing its id.
