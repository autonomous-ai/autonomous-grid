---
status: proposed
---

# A task is not an inference transaction, and everything about it follows from that

The grid moves one shape of work today: a consumer POSTs an inference request, a provider claims it,
streams an answer back, and the transaction settles. Every mechanism around it — the work queue, the
mailbox, the reaper, escrow, provider-death cleanup — is built for a unit of work that lasts seconds,
is worthless if nobody is listening, and is paid for per token.

A **task** is none of those things. A user hands the grid a prompt and some files, disconnects, and
comes back later; a provider spawns Claude Code against a working copy and it runs for minutes or
hours; the result is not a token stream but a *changed directory*. Reusing the inference machinery
for it is not a shortcut — the existing mechanisms actively do the wrong thing:

- `_provider_queues` (relay.py) is an **in-memory** `asyncio.Queue` per provider, and deliberately
  so: *"Postgres is the wrong tool for this (high frequency, no need for durability — pending
  requests are stateless until a provider picks them up)"*. For a task whose creator has already
  disconnected, a relay restart would silently drop work nobody is waiting on to re-send.
- The same queue fixes the provider **at enqueue time**. A task that outlives its assigned provider
  can never be picked up by another one, which is the entire reason tasks are being built.
- `reap_timed_out_transactions` (credits.py) kills anything past `deadline_at`, and
  `inference_timeout_seconds` is 600. A task routinely outruns that.
- `cleanup_provider_inflight` (relay.py) **fails** a provider's in-flight rows when it goes offline.
  A task must be **requeued**. One function cannot hold both policies for the same table.
- Every terminal path calls `settle_failed_within` — escrow a task does not have.

## Decision

> **A task is a first-class unit of work with its own tables, its own dispatch channel, and its own
> lifecycle. It shares the relay's identity and transport, and nothing else. The inference path is
> not modified.**

Six decisions follow, each forced by the one above.

### D-a — Dispatch is a durable queue claimed at poll time

Tasks live in Postgres (`tasks`), not in memory, and providers claim them through a dedicated
`POST /relay/v1/tasks/claim` — never `/poll`. The claim is an atomic `SELECT … FOR UPDATE SKIP
LOCKED` + state transition, the same race-free batch pattern `reap_timed_out_transactions` already
uses.

`/poll`'s payload is a cross-repo wire contract (grid-src ↔ this repo ↔ grid-apis). Adding a second
work *kind* to it would put a durable, claim-time-routed mechanism inside a route built for an
ephemeral, enqueue-time-routed one, and would put the money path at risk for a feature that does not
touch money. A separate endpoint costs the provider one more loop.

### D-b — A task is claimable only after its input is committed

The client's prompt and files arrive together at `POST /tasks`. The relay validates them, cuts
`task/<task_id>` from the project's `main`, commits the input, and only then moves the task to
`queued`.

The alternative — client pushes, then creates the task — is two non-atomic steps whose failure modes
are both silent: create-then-push lets a provider claim and pull *before the files arrive*, and the
agent runs against missing input with nothing to indicate it. Committing first makes "the task
exists" and "its input is in git" the same event.

Filenames come from the client and are therefore hostile input. A path escaping `workspace/` is not a
tidiness concern: a file committed to `.git/hooks/` **executes on the provider at checkout**, and a
symlink to the provider's config dir routes its Claude subscription credential into the transcript,
which is then committed back to the user's repo. The relay rejects absolute paths, `..`, anything
touching `.git/` or `.grid/`, and symlinks, before the commit.

### D-c — The lease is the write credential

The relay fronts the git server; both clients and providers authenticate with the grid token they
already hold, over smart-HTTP. There are no SSH keys to provision — for anyone.

A provider may push to a project's repo **iff it currently holds that task's lease**. This is not two
mechanisms that happen to agree; it is one. Heartbeat alone cannot prevent a provider that was
declared dead — wrongly, because it merely stalled — from continuing to write while its replacement
writes too. Server-side authorization keyed on the lease is the fence, and it holds without the
losing provider ever learning it lost.

Renewal must prove the **child process**, not the network. The provider's supervisor holds the
child's `Popen` handle and renews only while `poll() is None`. Reading a pid from a record and
signalling it is exactly the hazard [ADR 0020](./0020-process-identity-at-run-record-seams.md) and
[ADR 0026](./0026-the-grid-servers-pid-is-a-claim-too.md) removed from the run-record seams, and it
is not reintroduced here. Renewal every 30s, lease TTL 120s — four beats of slack, because a tight
TTL converts "busy" into "dead" and spends a real attempt to learn nothing.

Liveness is **child-alive only**. Silence is not evidence of death: a task legitimately emits nothing
for ten minutes while a build or a test suite runs. A hung child is caught by the task's own
`deadline_at`, not by inferring death from quiet.

### D-d — Losing a lease requeues; the event log does not restart

A task whose lease expires returns to `queued` and is claimed by another provider, up to
`max_attempts`. This is the opposite of `cleanup_provider_inflight`'s policy and is the reason tasks
cannot share that table.

The cap is not ceremony: without it, a task that crashes its host is requeued into every provider in
the fleet in turn. Retry is safe for effects that live in git — the branch resets to the input commit
— and unsafe for anything else the agent did on its way out. That asymmetry is disclosed, not hidden:
the retry is announced in the stream.

One task keeps **one** event log and **one** monotonically increasing cursor across all attempts; a
new attempt appends a `task.attempt_started` marker. Truncating and rewriting from seq 0 would leave
a client parked at `after_seq=500` reading unrelated events with no way to detect it. A log per
attempt would force every client to implement "this stream ended, follow the pointer" — abandoning
the stable-id resume that `after_seq` exists to provide.

### D-e — Commits happen at terminal boundaries; `main` advances only on success

The provider commits when the task ends, succeed or fail, and pushes `task/<task_id>`. The relay
fast-forwards `main` **only on success**, so `main` is always a known-good state and always the base
the project's next task builds on. A failed attempt keeps its branch — inspectable, diffable,
cherry-pickable — and leaves `main` untouched.

Committing is driven by the **supervisor**, from the child's exit status — not by a Claude Code
`Stop` hook. A hook does not run when the process is killed, and an agent executing an arbitrary
prompt is not a trustworthy executor of its own completion protocol.

Mid-run checkpoints are deliberately **not** in the first slice. Resuming from one requires
`--resume` to accept a transcript truncated mid-turn — unverified, and the whole value of the feature
rests on it. The cost is measurable now: a Claude Code transcript reached 1.9 MB in a single session,
and git stores the whole blob per commit until it is packed.

### D-f — Observability is pushed, never requested

The client's view of a running task — the event stream and the working directory's shape — is
**published by the provider**, never pulled from it. Tree snapshots ride the existing heartbeat,
hashed so an unchanged tree costs nothing, and land in the same `task_events` log the client is
already reading.

A request/response "show me the tree" channel would make the wire bidirectional, obliging the
provider to keep answering control traffic while saturated with a task, and obliging both ends to
correlate replies. Publishing keeps one direction and one reader.

## Consequences

- The inference path is untouched. No wire-contract value is added to `/poll`, `/result`, the
  mailbox, or the heartbeat's existing keys.
- Providers gain a second claim loop and a supervisor process. Task capacity is configured
  **per provider** and is not `max_concurrency`: the real ceiling is the rate limit of the provider's
  own Claude subscription, shared by every child it spawns.
- Every provider must run tasks at an **identical absolute path** (`/var/grid/projects/<project_id>/
  workspace`). Claude Code derives a session's transcript directory from the working directory
  (`~/.claude/projects/<abs-cwd with / → ->/`), so a provider using a different prefix cannot
  `--resume` a session another one started.
- `CLAUDE_CONFIG_DIR` stays **fixed per provider** and holds the provider's own credential. Per-user
  config directories are not a rejected preference — they are **measured to be broken**: pointing the
  variable at a fresh directory yields `Not logged in · Please run /login` even on macOS, where the
  token lives in the Keychain, and seeding a minimal `.claude.json` does not restore it. A custom
  config dir demands its own credential material, which a per-user directory would then carry into
  the repo it is synced through. Per-project isolation is already provided by the cwd-derived
  transcript path, and `memory/` sits in that same directory, so one symlink captures both.
- The one-active-task-per-project rule is enforced by a partial unique index, not by convention.
- Deferred, with the shape kept open: mid-run checkpoints, task cancellation, per-task
  `retryable: false`, and git-LFS for large inputs.
