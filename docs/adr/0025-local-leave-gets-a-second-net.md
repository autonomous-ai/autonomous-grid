---
status: accepted
---

# Local `grid leave` gets a second net, and says what it could not establish

[ADR 0020](./0020-process-identity-at-run-record-seams.md) gave the shared teardown a third answer —
`Teardown.verified`, "we either stopped the recorded child *as ours*, or positively proved nothing of
ours is left". Remote consumes it: a leave that is unverified **and** could not read the process table
has checked nothing, so it exits non-zero after its backstop rather than printing a qualified "Left".

Local read only `survivor`. `cli/provider.cmd_leave` printed `Left engine <id> on <grid>.` for every
`Teardown(survivor=0)`, including the regimes the verdict logic can now positively identify as
unproven — a recycled pid (`RecordVerdict.NOT_OURS`), a long-dead pid whose stamped `pgid` still names
a live group nothing can authenticate, and a container zombie still occupying its own group. And local
had no argv sweep at all: no `orphan_sweep` reference existed anywhere under `cli/provider.py` or
`local/`. Not a regression — before ADR 0020 the same regimes silently no-op'd through `terminate_pid`
on a stale pid and printed the same line — but the CLI now *knows* and did not say.

Filed as `.scratch/grid-leave/` issue 17 (PRD follow-up F15) by issue 08's review gate, which
deliberately did not widen into local. Scope: **this CLI only** — no wire value, no master or
control-plane slice, nothing for the lockstep register in `CLAUDE.md`.

Choices a future reader will otherwise re-litigate:

- **Local gets the sweep, because it is the only net local can have.** The obvious cheaper answers —
  fail loud on a bad verdict, or footnote the success line — were rejected on measurement, not taste.
  Remote survives a missed child because `grid leave` sends an authoritative relay deregister (`PUT`
  role=`consumer`) that a surviving child cannot undo; the models drop whatever the sweep did. Local
  has no equivalent and cannot have one: the only CLI-side verb is `DELETE /nodes/{id}`, and a
  surviving child's next heartbeat 404s and **re-registers** (`cli/provider._heartbeat`), putting the
  models back within one 15s beat. So a stranded local child pins its models in `grid models`
  indefinitely, where a stranded remote one costs ~120 seconds.

  That also settles the *reporting* question, which is otherwise a matter of opinion. With no second
  net, "unverified" is permanent in the container regime this feature was filed about — PID 1 never
  reaps, the corpse occupies its process group forever, and every teardown declines to judge it. A
  rule keyed on the verdict alone would therefore print a warning, or fail, on **every** leave in
  exactly the environment that needs one. The sweep is what lets the CLI be quiet with *evidence*
  ("read the table, nothing of ours is running") and loud only when it truly established nothing.

- **The unverified rule is remote's, verbatim — including the never-stamped `pid: 0`.** A record whose
  pid was never stamped classifies `verified=False` like every alarming case, which is what rules out
  the naive "fail loud on unverified". It is tempting to answer that by excusing it — "a record that
  never named a process makes no claim" — and that would be wrong twice: it contradicts
  `remote_provider._full_leave_reap`, and in the join write-race the record reads `pid: 0` *because a
  child was just spawned*, so the claim is implicit rather than absent. Local appends the `0` exactly
  as remote does. The ordinary case stays quiet because the **conjunction** holds: the sweep read the
  table, so `scanned` is True and nothing fires. The `0` is filtered out of the failure *message*
  only, where there is no pid to name.

- **How much of the argv a leave pins is how much it is claiming.** The sweep matcher now takes the
  marker and however many leading positionals the caller means: remote pins the network id, a local
  whole-grid leave pins the grid id, and a local `--engine <x>` leave pins the grid id *and* the
  engine id. The dispatch arity is unchanged at two, so ADR 0024's argument-count discriminator — the
  thing that separates a serve child from an operator's own `pgrep` — guards both markers, and its
  lockstep note now names both ends of it.

  Local **diverges from remote deliberately** on the last-engine case. Remote routes a `--engine
  <last>` drop through the full identity teardown, because its identity is a singleton and the last
  engine *is* the whole thing. Local runs one child per engine, so `--engine` is always targeted and
  always pinned; bare `grid leave` is the repair verb. A one-engine grid therefore gets a grid-wide
  sweep from `grid leave` and a pinned one from `grid leave --engine <same id>` — predictable in a way
  "pins unless it happens to be the last one" would not be. The consequence is stated rather than
  hidden: a targeted leave will not reap *another* engine's record-less orphan, and is not meant to.

- **A record vouches for a child, and the read order is what makes that true.** A grid-wide sweep is
  the only thing that can reap a record-less orphan, and it is also the thing that could kill a
  healthy engine somebody just started: local `grid leave` holds **no file lock**, and neither does
  local `grid join`, so a lock in leave alone would protect nothing. A join landing mid-teardown
  writes its `pid: 0` placeholder, spawns a child carrying this grid's marker, and would be swept —
  for the whole leave, including up to 25 seconds of stop grace.

  What closes it is an ordering invariant already in the spawn path: `cli/provider._spawn_engine`
  writes the record **before** it calls `Popen`, so a child that exists always has a record. Read the
  process table first and the records second, and every child in that table is in that snapshot. So a
  match whose engine id still has a record is somebody's live engine and is spared; a match with no
  record at all is the orphan. Re-reading the records first leaves the hole wide open, which is why
  `scan_engine_children` and `terminate_matches` are separate calls rather than one `sweep_orphans` —
  the split exists to hold that order, and a mutation that swaps it goes red.

  **Every record present at the re-read vouches, including this leave's own targets.** Subtracting the
  target ids is the obvious version and it is wrong, because `stop_engine` has already unlinked the
  record of every target it confirmed dead: a target's record that is *back* was written after we
  removed it, which is exactly a `grid join --name <same id>` that raced us — and subtracting would
  sweep that re-joined child and announce it as a reaped orphan. A target we could not confirm dead
  keeps its record and is vouched for here too, which costs nothing: its pid is already excluded and
  it is already being reported as a survivor.

- **A failure *deciding* must not erase what the scan already established.** The vouching re-read can
  raise — `jsonio` turns an unreadable record into `SystemExit`, and local leave holds no lock, so a
  concurrent join or leave on a sibling engine can do it at exactly that moment. Wrapping the table
  read and the re-read in one `except` threw away a positively-identified live orphan and blamed the
  process table, which had been read perfectly: exit 0, no pid named, stray child still serving — this
  issue's own bug, one layer up. (The same mistake `orphan_sweep.terminate` documents per pid one
  level down.) The guards are therefore separate, and a re-read failure keeps `scanned=True` and
  reports the matches as **survivors** — nothing is signalled, because we genuinely cannot tell an
  orphan from a racing join, but a live child of this grid that we did not stop is precisely what
  `survivors` means, so leave names the pid and fails loud instead of printing a clean success over
  it. Found by the silent-failure review gate; both regimes were reproduced against the real code
  before the fix.

- **The sweep module moves to `shared/`.** It serves both modes now, exactly as
  `shared/run_records.py` holds the record format and teardown they share (DECISIONS D17). Keeping it
  under `remote/` would have made the *local* handler import `remote.*` — the dependency
  `cli/provider.py` already refuses for the API key store — and it retires the awkwardness
  `shared/win_paths.py` exists to work around.

- **Local's caveats cannot promise a TTL drop.** The three notes are worded per mode and only their
  *precedence* is shared (`orphan_sweep.caveats`). Remote's say a stray child's models drop after the
  ~120s node TTL, which is true there only because the backstop already flipped the node to consumer.
  Locally a stray child keeps heartbeating and its models never drop at all, so local's notes name the
  remedy instead of a deadline. A shared string here would have been a false statement in one of the
  two modes.

## Consequences

`grid leave` in local mode is now authoritative and idempotent in the same sense remote's is, with one
honest gap: it can *reap* what it finds but it cannot *deregister*, so the grid learns about a
teardown only when the child's own exit-path `DELETE` lands or its heartbeat lapses at the 60s local
TTL (`local/server.py`). A bare `grid leave` with no records is a first-class repair verb rather than
the `No engines joined` dead end, and `--all` with no records no longer exits silently.

Three new ways for the command to be non-silent, none of them a false alarm: a surviving child (record
or swept) exits non-zero naming it — as a process *group* where that is what survived; an unverified
record plus an unreadable process table exits non-zero saying nothing was established; and `foreign` /
a partly-hidden table / an unreadable one qualify the success line at exit 0.

Residuals, named rather than fixed. There is still no local equivalent of the relay backstop, so a
`foreign` match — another user's child of this grid — keeps serving models here until its owner or an
elevated `grid leave` stops it. And the record-vouching rule is only as good as the spawn ordering it
rests on: a future join path that spawns before it writes would reopen the race silently, which is why
the invariant is stated at both ends.

**The sweep's scope is the process table, not `GRID_HOME`.** Raised by the security review as a
suspected new cross-grid kill path; measured down to this. Two `GRID_HOME`s that share a process table
and a grid id — containers run with `--pid=host` from one image, or a copied `~/.grid/grids/<dir>`
whose `grid_id` field was never regenerated — will have a leave in one reach the other's children.
That is ADR 0024's accepted property (a sweep is machine-wide and scoped by *grid id*), not something
local introduces: remote's sweep reads the same whole table for its `network_id`. The reported
single-`GRID_HOME` variant does not exist, and the check is worth recording because it is
counter-intuitive: run records are keyed by grid id, so two configs sharing one make `read_records`
return the *other* grid's records — the record path already acts on them, the empty-records repair
branch is never reached, and the aliasing predates this change (before it, that leave also unlinked
those records unconditionally). The honest statement is that two grids sharing an id are one grid to
every part of this CLI, and leaving it reaps its children wherever they are.

⚠️ **Half of that residual has since been overturned — see the `GRID_HOME` bullet added to
[ADR 0024](./0024-what-the-argv-sweep-may-kill-and-what-it-can-see.md).** The paragraph above answers
for a *local* grid id, where a collision is an accident and the two configs really are one grid; that
part stands. It was written as though the same reasoning covered **remote**, and it does not: a
network id is shared **by design**, so two accounts joining one grid from one provider box is the
ordinary case rather than a misconfiguration — and dev-VM finding E-03 is one operator's `grid leave`
tearing down the other's provider at exit 0 in silence. A sweep now drops any match it can prove runs
from a different `GRID_HOME`. Recorded here rather than edited away, because the reasoning that
generalised a local property to the remote one is what a future reader needs to see.

**The `__engine` marker's collision surface is measured, not assumed.** ADR 0024 justified the
argument-count discriminator against a real process table for `__remote-engine`; the shorter local
marker deserved the same and had not had it. Measured on a developer box with a full desktop and
toolchain: **935 processes, zero** carrying `__engine` as a whole argv token at any arity. The count
guard bounds what is left.
