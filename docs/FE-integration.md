# FE integration — driving the grid CLI from a mobile app

**Audience: a coding agent building the client.** Everything here is a contract verified against the
CLI's own source, not a description of intent. Where a number appears it was measured. Follow it
literally; where it says "branch on this", branch on exactly that and nothing else.

The feature is **distributed tasks for non-developers** (ADR 0034): a person describes work in
words, some machine runs a coding agent on the project, and the result appears in the project by
itself. Nobody merges, approves, or types a git command — **and your UI must not reintroduce any of
that.** §7 is not styling advice; it is the product.

Companion documents: [`tasks-quickstart.md`](./tasks-quickstart.md) is the full command reference,
[`tasks-macos-walkthrough.md`](./tasks-macos-walkthrough.md) is a human walkthrough of the same flow.

---

## 1. Read this before you design anything

### The CLI cannot run on the phone

`grid` is a Python wheel plus macOS/Linux binaries. iOS does not permit spawning arbitrary
executables at all; there is no Android build. **"Wrapping the CLI" therefore always means the CLI
runs somewhere else and the app drives it.** Decide which of these you are building before writing
a line:

| | **A · Companion host** (recommended for v1) | **B · Direct relay HTTP** |
|---|---|---|
| Shape | `grid … --json` runs on a machine the user owns; a thin shim spawns it and forwards stdout / stderr / exit code. The app talks to the shim | The app calls the relay's `/relay/v1` routes itself with a grid access token |
| You get for free | credential storage and refresh, the postcondition guards (§6), the refusal vocabulary, the git plane | nothing — you re-implement all of it |
| You must build | one process-spawning shim | token lifecycle, every guard in §6, and you own the cross-repo wire contracts by hand |
| Blocks on | a host being reachable | nothing |

**The non-dev flow needs no local git.** `task create/send/get/list/follow/cancel/undo` and
`project files/file/download/status/diff` are all plain relay calls — issue 45 exists precisely so a
user's machine needs no developer tools. Only `project import`, `project clone`, `task fetch` and
`project refresh` need git, and none of them are on the path a non-dev user walks. So **B is viable
later**; start with A because the CLI encodes guards you would otherwise discover in production.

**Either way the document shapes below are identical**, because `--json` on stdout is the relay's own
document passed through unchanged — the CLI validates a few postconditions but never reshapes a
payload.

### Auth is out of band

`grid login` is a **device-code flow that requires a browser** and writes credentials to `~/.grid`
on the host. Your app cannot perform it. Provision the host once, out of band; the app assumes a
logged-in host. A 24-hour session is refreshed by `grid sync` without a browser.

> ⚠️ `grid sync` prints `Your grid session has expired…` and **still exits 0**. Never treat its exit
> code as proof of a live session — check that the command you actually wanted succeeded.

---

## 2. The domain model

Five nouns. Get the first two right and most bugs disappear.

| noun | what it is | id shape | your app |
|---|---|---|---|
| **project** | a repository the grid hosts, with members. Addressed by **id, never by name** | uuid | a workspace / folder the user picks |
| **conversation** | one agent session you return to; many tasks. **This is the thread the user comes back to** | uuid | a chat thread |
| **task** | one message + one agent run inside a conversation. **This is what the user calls "a task"** | uuid | one message bubble + its result |
| **member** | a person on the grid, identified inside a project by `member_key` | 32-hex | an avatar. `member_key` is **per grid** — it is not an email and not a user id |
| **worktree** | the provider's checkout of one conversation | — | **never surface it.** Listed here only so you do not mistake a provider-side concept for something the app owns |

> ⚠️ **Three JSON keys still spell a task `turn`**: `active_turns`, `member_running_turns` and
> `member_running_cap`. Read them by those exact names — but show the user the word **task**. The
> CLI's own help, output and refusals say `task` everywhere; `turn` is not a word your users see.

Also: **`main`** is the project's trunk — everybody's finished work. Created once, then **the grid
moves it**. There is no branch for the app to choose, show, or switch.

### The one distinction that breaks apps

`grid task create` returns **two** ids and they address different objects:

```
{ "id": "<task-id>", "conversation_id": "<conversation-id>", ... }
```

| takes a **task** id | takes a **conversation** id |
|---|---|
| `task get`, `task follow <id>`, `task cancel`, `task diff`, `task undo`, `task fetch` | `task send`, `task follow --conversation <id>`, `project commit` |

Store both on every message you render. Passing one where the other belongs is a 404 that reads like
"it disappeared".

### Ordering rules — the whole concurrency model

- **Inside one conversation**: messages run **strictly in the order sent, one at a time**. The user
  may type ahead; queue them and show them as pending. There is no parallelism to expose.
- **Across conversations**: everything runs at once. A colleague never blocks the user.
- **Only the person who started a conversation can send into it.** A colleague can read its tasks.
  "Reply" on someone else's thread must create a *new* conversation, not fail.

---

## 3. The machine contract

### stdout, stderr, exit code

- `--json` puts the relay's document on **stdout**. Pass it through; do not reshape.
- Refusals go to **stderr** as one line of JSON, and **the human sentence follows on the next
  line**. ⚠️ **Parse the FIRST line only** — `JSON.parse(wholeStderr)` fails.

```json
{"error":{"code":"project_has_no_trunk","message":"…","status":422}}
```

| field | meaning |
|---|---|
| `code` | machine-readable refusal code, or `null` for a local refusal / a relay that sent plain prose |
| `message` | **never empty**, guaranteed. Show it to the user |
| `status` | the relay's HTTP status, or `null` if the relay was never reached. `null` means *"we could not ask"* — not a verdict. Do not cache a `null`-status failure as a fact about the project |

### Exit codes

| code | meaning |
|---|---|
| `0` | success |
| `1` | failed, timed out, **or any refusal**, **or the relay was unreachable** |
| `2` | **not finished yet** — and this is the *only* code a poller may read as "ask again" |

`2` exists so waiting is expressible: `0` would claim an outcome nobody reached, `1` would claim it
went wrong. A state this CLI cannot place — one a newer relay invented — is reported as **unfinished**
with a note on stderr, never as success or failure. Mirror that: unknown state ⇒ keep polling.

### Task states

```
preparing → queued → running → completed | failed | timed_out
```

Terminal = `completed`, `failed`, `timed_out`. A **cancelled** task arrives as `failed` with
`error = "cancelled"`. Anything else you receive: treat as non-terminal and keep polling.

### The event stream

`task follow --json` writes **one JSON object per line** on stdout:

```json
{"seq": 12, "event": {"type": "task.output", "...": "..."}}
{"seq": 13, "task_id": "<task-id>", "event": {"type": "...", "...": "..."}}
```

`task_id` is present only when following a **conversation** (you need it to attribute each event to a
task). Rules:

| | task follow | conversation follow |
|---|---|---|
| ends on event `type` | `task.terminal` (carries `state`) | `conversation.idle` |
| exit code | `0` iff the terminal event's `state == "completed"` | `0` iff the stream was watched to its end — **not** the last task's outcome |
| cursor | `--after-seq <last seq seen>`; default `-1` = from the start | same, **but a conversation's sequence is its own and is not a task's** |

Resuming with `--after-seq n` replays exactly what follows — **no gap and no repeat**. Persist the
cursor per stream; on reconnect, resume rather than restart, or the user sees the conversation twice.

A conversation has **no terminal state** — "is this finished?" is not a fact the grid owns. Do not
invent a "done" badge for a thread; `conversation.idle` means *quiet right now*.

---

## 4. Command surface the app needs

Every command also takes `--grid <id>` and `--json`.

| what the user is doing | command | returns |
|---|---|---|
| list workspaces | `project list --json` | `{"projects":[…]}` |
| new workspace, ready at once | `project create --name <n> --empty --json` | `{"id":…, "bootstrap":{"status":"initialized","commit":…,"trunk":"main"}}` |
| first message | `task create --project <pid> --prompt <p> --json` | `{"id":<task>, "conversation_id":<conv>, "state":…}` |
| next message | `task send <conv> --prompt <p> --json` | a task document |
| attach files to a message | add `--file LOCAL[:DEST]` (repeatable) | committed **before** any provider claims the task |
| watch one task | `task follow <task> --json --after-seq <n>` | event lines |
| watch a thread | `task follow --conversation <conv> --json --after-seq <n>` | event lines with `task_id` |
| poll one task | `task get <task> --json` | task document; **exit 2 = ask again** |
| thread list / home screen | `task list --json` (no `--project`) | `{"tasks":[…], "next_after":…}` — the caller's own conversations **across every project** |
| one project's activity | `task list --project <pid> --all --json` | the whole team's; `--all` **requires** a project |
| browse the project | `project files <pid> [path] --json` | `{"entries":[{"name","type","size","executable"}], "commit":…}` |
| open a file | `project file <pid> <path> --json` | `{"content","encoding","size","limit","too_large","truncated"}` |
| export | `project download <pid> --output <f>.zip --json` | `{"project_id","path","bytes"}` |
| what did this change | `task diff <task> --json` | the change, and **who asked for it** |
| take it back | `task undo <task> --json` | `{"undone":true,"trunk_commit":…}` |
| stop a run | `task cancel <task> --json` | the conversation survives |
| where is the project | `project status <pid> --json` | see below — this is your change signal |
| invite | `project member add <pid> --email <e> --json` | they must have signed in to the grid once |

**Pagination**: `task list` returns `next_after` when there is more; pass it as `--after`. Absent ⇒
last page. `--limit` defaults to 50, max 200.

### `project status --json` — the keys worth reading

| key | type | use |
|---|---|---|
| `main_commit` | string | **the project's change signal.** Diff it against the one you hold to notice that anybody's work landed. This is how you detect the apply in Step 4 |
| `trunk` | string | the trunk's **ref name** (`"main"`), *not* an object. ⚠️ Never render it — it is a git word on a non-dev home screen, which is why the CLI deliberately prints `version=<main_commit>` instead |
| `active_turns` | array of `{id, state, created_at}` | the caller's tasks in flight. **Absent ⇒ show nothing** — never treat a missing key as zero |
| `queue` | `{queued, running}` | why nothing is moving, project-wide |
| `member_running_turns` / `member_running_cap` | int | how much of their own allowance the user is using. **Both must be present integers or say nothing.** ⚠️ `running > cap` is a legitimate reading, not a bug to clamp — a merge task is exempt from the cap but still counts |
| `archived` | bool | `=== true` ⇒ read-only |
| `visibility` | string | `=== "private"` ⇒ members only. Absent ⇒ grid-visible |
| `providers` | — | who could take the work |

---

## 5. The end-to-end flow, step by step

### Step 0 — host is logged in

Out of band (§1). Verify cheaply at app start with any read, e.g. `project list --json`.

### Step 1 — get a project that can accept work

A new project has **no trunk**, and a task cannot be cut from nothing. For a non-dev app there is
exactly one correct call:

```
project create --name <name> --empty --json
```

⚠️ **Check the postcondition, do not trust the 201.** `bootstrap` is the one key in this whole plane
that degrades **silently** against an older relay — the key is dropped, you get a 201, and the
project has no trunk:

```ts
if (doc.bootstrap?.status !== "initialized") throw new Error("project was not bootstrapped");
```

`--empty` is **irreversible**: a project that holds anything can never import an existing repository.
That is fine for an app that starts from nothing; never offer both paths in the same wizard.

If you ever see `code: "project_has_no_trunk"`, the project is unusable — do not retry the task, fix
the project. This is one of only two codes you branch on (§6).

### Step 2 — the first message

```
task create --project <pid> --prompt "<what the user typed>" --json
```

Store `id` (task) **and** `conversation_id`. Render the thread from the conversation id from now on.

### Step 3 — watch it

Prefer **conversation** follow for a chat UI — one stream carries every task plus the steps the grid
adds itself:

```
task follow --conversation <conv> --json --after-seq <cursor>
```

Persist `seq` after every line. On reconnect, resume from it.

If you cannot hold a long-lived process (mobile background limits make this normal), poll instead:

```ts
// exit 2 is the ONLY "ask again"
const rc = await run(["task","get",turnId,"--json"]);
if (rc === 2) { scheduleRetry(); } else if (rc === 0) { done(); } else { showRefusal(); }
```

### Step 4 — ⚠️ the task finishing is not the project changing

`--follow` returning, and `state: "completed"`, mean **the task reported**. The grid applies the
result to the project a moment later — **measured at about 3 seconds** on a small project. The
relay does this outside the report on purpose, because the provider's lease has only ~25–30s left at
that moment and a merge does not fit in it.

So: **never read a file immediately after a task completes and render it as the result.** Poll
`project status --json` and wait for **`main_commit`** to change from the value you held before the
task. An app that skips this shows the user their old content and looks broken.

### Step 5 — show the result

`project files` / `project file` for browsing, `task diff <task>` for "what did this message change".
`diff` also names **who asked for it**, which is what makes a shared project legible.

### Step 6 — the next message

```
task send <conv> --prompt "<…>" --json
```

It runs after whatever that conversation is already doing. Let the user type ahead freely; show
queued messages as pending. Tasks **compose** — the second builds on the first, it does not start
over.

### Step 7 — steps the grid takes on its own

When two people touch the same file, the grid puts a **merge task** into the losing conversation and
an agent reconciles it. Your app will receive tasks nobody typed.

```ts
const isGridStep = task.kind === "merge";   // EQUALITY, never truthiness
```

- `kind === "merge"` ⇒ render as **a step the grid took**, with your own words. Never as a message
  from the user.
- `kind` **missing or anything else** ⇒ a person's message. A missing key must never invent a
  refusal or a special case.
- ⚠️ A merge task's `result_text` is the **model's own output** and is full of git vocabulary
  (`git add`, `<<<<<<<`, "committed as f0e7cfa"). Do **not** show it raw to a non-dev user. `kind` is
  what lets you avoid it.

### Step 8 — undoing

```
task undo <task> --json
```

Removes exactly what that one task changed; everything done since stays. Only the person who asked
for the task, or the project's owner, may undo it. Expect these refusals and show `message`:

| code | status | meaning |
|---|---|---|
| `undo_conflicts` | 409 | somebody built on the same files. It names them. The way forward is a new message, not a retry |
| `already_undone` | 409 | undone before |
| `nothing_to_undo` | — | the task failed, or its result has not reached the project yet |

### Step 9 — cancelling

```
task cancel <task> --json
```

The **conversation survives**; the next message continues. The agent stops within ~30s, on the
provider's next lease renewal. Any project member may cancel any task in it — often the person who
needs to is not the one who started it.

> ⚠️ **Do not promise the user their work is kept.** `grid task cancel --help` says `task fetch`
> still works on a cancelled task; measured, it returns the tree from *before* the agent ran. Nothing
> that had already landed is lost, but a cancelled task has no result to recover.

---

## 6. The five failures that will not announce themselves

Every one of these has already been got wrong once. All are silent: green paths, wrong behaviour.

1. **A new key on an existing endpoint degrades silently; a new route gives a loud 404.** So verify
   new keys by their **postcondition in the reply** — `bootstrap.status === "initialized"` after
   create, the echoed `project_id` after `task create`. Neither prevents a bad write; both stop you
   reporting it as success.
2. **Compare wire enums for equality, never truthiness.** `kind`, `archived`, `visibility`,
   `undone`, `serves_you`, the cap keys. `if (task.kind)` is a bug; `if (task.kind === "merge")` is
   the contract. A missing key must never produce a refusal.
3. **Exit `2` is the only "ask again".** `1` covers a failed task *and* an unreachable relay, so a
   poller that retries on `1` loops forever on a genuine failure. And `until grid task get "$id"`
   without an `rc` check waits forever on a failed task.
4. **Read the first line of stderr.** JSON on line 1, an English sentence on line 2.
5. **Do not branch on more than two refusal codes.** Exactly `project_has_no_trunk` and
   `project_already_has_trunk` are worth reading; **display every other refusal verbatim**. Each
   relay message already names the way forward, and every extra code you parse is another thing a
   reworded relay breaks. If you want to pin something in a test, pin the *remedy sentence*, not the
   code.

Two more that are about state rather than parsing:

- **Archived project**: every read still works; writes are refused `project_archived` (409) naming
  `grid project unarchive`. Render it read-only rather than broken.
- **Private project**: a non-member's read is **byte-identical** to the refusal for a project id that
  does not exist. You cannot tell "forbidden" from "absent", and you must not try — that is the
  design.

---

## 7. UI rules that come from the product, not from taste

ADR 0034's premise is that the user never learns git. The CLI's own surface is scanned against a
git-vocabulary denylist. Your app is the other half of that, and nothing enforces it for you.

- **Never show**: branch, merge, rebase, commit, ref, trunk, HEAD, conflict markers, SHAs.
- **"Where is my work?"** is answered by `project files` / `project file` / `task diff`, not by a
  history view.
- **A merge task is a step, not a message** (§7 above).
- **There is no approve button.** Finished work reaches the project by itself; the affordance is
  `task undo` *afterwards*, which is a different interaction from a review queue. Do not build one.
- **There is no "end conversation".** The grid holds no such state.
- **Do not show a progress percentage.** A task has states, not progress; `preparing → queued →
  running` is the honest display, and `queued` means "waiting for a machine", which is worth saying.

---

## 8. Reference driver

```ts
type Refusal = { code: string | null; message: string; status: number | null };

async function grid(args: string[]): Promise<{rc: number; out: string; err: string}> {
  // Architecture A: spawn on the companion host. --json is always appended.
  return spawnOnHost("grid", [...args, "--json"]);
}

function refusalOf(stderr: string): Refusal | null {
  const firstLine = stderr.split("\n", 1)[0];           // ← line 1 only
  try { return JSON.parse(firstLine).error ?? null; } catch { return null; }
}

async function createProject(name: string) {
  const { rc, out, err } = await grid(["project", "create", "--name", name, "--empty"]);
  if (rc !== 0) throw refusalOf(err) ?? new Error("project create failed");
  const doc = JSON.parse(out);
  if (doc.bootstrap?.status !== "initialized") throw new Error("not bootstrapped");  // postcondition
  return doc.id as string;
}

async function firstMessage(projectId: string, prompt: string) {
  const { rc, out, err } = await grid(["task", "create", "--project", projectId, "--prompt", prompt]);
  if (rc !== 0) {
    const r = refusalOf(err);
    if (r?.code === "project_has_no_trunk") throw new Error("project unusable — do not retry");
    throw new Error(r?.message ?? "could not start");
  }
  const doc = JSON.parse(out);
  return { turnId: doc.id as string, conversationId: doc.conversation_id as string };
}

async function awaitTurn(turnId: string): Promise<"completed" | "failed"> {
  for (;;) {
    const { rc } = await grid(["task", "get", turnId]);
    if (rc === 2) { await sleep(5000); continue; }      // the ONLY ask-again
    return rc === 0 ? "completed" : "failed";
  }
}

// ⚠️ completed !== visible. Wait for the project to move (~3s, measured).
// `main_commit` is the project's change signal — the CLI's own source says an application
// diffs it against the one it holds to notice that anybody's work landed.
async function awaitApplied(projectId: string, mainCommitBefore: string) {
  for (;;) {
    const { out } = await grid(["project", "status", projectId]);
    if (JSON.parse(out).main_commit !== mainCommitBefore) return;
    await sleep(2000);
  }
}

async function* followConversation(conversationId: string, cursor: number) {
  // Each stdout line is {seq, task_id?, event:{type,...}}; persist seq, resume with --after-seq.
  for await (const line of streamLines(
      ["task", "follow", "--conversation", conversationId, "--after-seq", String(cursor)])) {
    const { seq, task_id, event } = JSON.parse(line);
    yield { seq, turnId: task_id as string | undefined, event };
    if (event.type === "conversation.idle") return;     // quiet — NOT "finished"
  }
}
```

---

## 9. Acceptance checklist

Before calling the integration done, prove each of these against a live grid:

- [ ] Two ids are stored per message; `send` is never given a task id
- [ ] `bootstrap.status === "initialized"` is asserted after every project create
- [ ] A poller treats **only** exit 2 as retryable, and terminates on 1
- [ ] Refusals are parsed from **line 1** of stderr, and `message` is what the user sees
- [ ] Exactly two refusal codes are branched on; everything else is displayed verbatim
- [ ] `kind === "merge"` renders as a grid step, and its `result_text` is never shown raw
- [ ] The UI waits for the project to move before showing a task's result as visible
- [ ] Stream reconnect uses `--after-seq` and produces no duplicate bubbles
- [ ] Typing ahead in one conversation queues in order and is shown as pending
- [ ] "Reply" on a colleague's conversation starts a new one instead of failing
- [ ] An archived project renders read-only; a private one is indistinguishable from absent
- [ ] No git vocabulary appears anywhere in the UI, including error surfaces
