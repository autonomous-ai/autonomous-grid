---
status: accepted
---

# "Already serving" has to prove service, not process existence

A running serve child is not an engine on the grid. [ADR 0020](./0020-process-identity-at-run-record-seams.md)
closed the half of that gap where the record's pid was a corpse; this closes the half no liveness
check can reach — a **genuinely live** child that never registered with the relay, or stopped
heartbeating.

Field-confirmed 2026-07-27, the same afternoon as the zombie: a `grid join` issued while the grid's
master was mid-respawn (relay answering 503) produced a child that sat in bring-up for 8+ minutes —
one thread, parked in a socket wait, a **0-byte log**. The idempotent re-join gate then reported
**"Already serving …; nothing to append."** at exit 0, truthfully about the process and uselessly
about the grid, while `grid models` showed nothing from the box. The engine endpoint and the relay
both probed healthy from that same box minutes later; the child never recovered and had to be
leave-and-rejoined by hand.

Scope: **this CLI only.** No wire value, no master or control-plane slice, nothing for the lockstep
register in `CLAUDE.md`. This is the *local-record* slice of the same truth
[ADR 0019](./0019-engine-health-in-the-heartbeat.md) carries on the wire — kept where the CLI can
read it offline, with no protocol change. Evidence and design: `.scratch/grid-leave/` (PRD follow-up
F8, issue 10 — Layer 2 of the approved three-layer proposal; issue 08 was Layer 1, issue 11 is
Layer 3).

Choices a future reader will otherwise re-litigate:

- **Two facts, in two different places, because they answer two different questions.**
  `registered_at` on the run record answers *did this process ever get on the grid* — a one-shot
  fact, so a locked record write costs nothing and it can live beside the routing it belongs to. The
  last successful heartbeat answers *is it still there*, at a 30s cadence for the life of the engine,
  and it is deliberately **not** a record field: writing one would take the same `file_lock` a
  `grid join`/`grid leave` serializes its union merge on, ~2 880 times a day, to store a number the
  filesystem already keeps. It is the **mtime of a sidecar file** (`<engine_id>.heartbeat`) beside
  the record — no lock, no parse, no content.

- **The sidecar's absence is the upgrade marker.** This is why the change needs no version field, no
  capability flag, and no `CLAUDE.md` lockstep entry. A child that reports service truth creates the
  sidecar in its startup self-stamp; therefore *no sidecar ⇒ the running child is not a build that
  reports ⇒ the gate says nothing new and behaves exactly as it did*. The same rule catches the case
  a version field would have got wrong: the self-stamp is best-effort, so a **new** build whose stamp
  failed also reads as "not reporting" rather than as "broken". This is ADR 0020's "a fact we could
  not read is `None`, never a placeholder", one layer up.

  The sidecar is created in the self-stamp rather than at first registration for a specific reason:
  the incident regime is a child wedged *before* it ever registers. Creating it at registration would
  leave exactly that child indistinguishable from an older build's.

- **The sidecar must not be named `*.json`.** `run_records.read_records` globs `*.json` and hands
  every hit to `jsonio.load_json`, which raises `SystemExit` on a bad parse — and the sidecar's
  content is deliberately nothing. A `.json` name would make one empty file break every record read
  in the CLI, on the single path `grid join`, `grid leave` and `grid models` all go through. A rename
  is the plausible future mistake, so a test pins it.

- **Exit code stays 0; the message is the fix.** Declining to act on an identical re-join is still
  correct — the gate's job is to not restart things needlessly, and it has no way to distinguish
  "wedged" from "slow" with certainty. What was wrong was the *claim*. So the gate reports the state
  it can prove — process up, this pid, not registered / not heartbeating since, the last register
  error, the log path — and offers `--respawn`. This matches the `last_reload_error` precedent
  already on this branch: a fact the detached child recorded, surfaced by the next CLI command that
  would otherwise print a reassuring line over it.

- **90s staleness, not the 2× the issue sketched.** A *successful* heartbeat tick's own worst case is
  already ~65s. The sidecar is touched the moment the heartbeat returns — the earliest honest point —
  but the rest of the tick runs after it: `_maybe_refresh_codex` (15s), the ADR 0019 engine probe
  (10s), the 30s wait, then the next beat's own 10s timeout. A 404 pushes it past 90s, because
  `heartbeat_once` re-registers **inline** (15s, plus a possible 401 refresh-and-retry). So 60s would
  not be tight, it would be wrong — routinely accusing healthy engines. 90s also stays under the
  relay's 120s node TTL, so the CLI still knows before the grid gives up.

  The residual >90s compound tail is **accepted, not solved**, and the reason it is survivable is the
  next decision.

- **The remedy is never an instruction.** Both verdicts are inferred from timestamps, and acting on a
  wrong one costs an operator a working engine and its in-flight requests. So the suggestion is
  conditional (*"if it stays this way…"*), and below a **300s bring-up window** it is withheld
  entirely: `_await_remote_engine_start` waits 3s, but a real bring-up — a large GGUF load, a ComfyUI
  start — takes minutes, and the child stamps its sidecar *before* bringing engines up. Without that
  window, a re-join a minute into a four-minute model load would report the same "up but not
  registered" as the wedged child and tell the operator to kill it — repeatably, on every re-join.
  Inside the window the state reads as *starting* and points at the log.

- **`grid join --respawn` is a modifier AND a bare restart.** It suppresses both the no-op and the
  SIGHUP — the same shape `rotated_live` already uses, and for a sharper reason: a hot-reload
  re-reads the record, which does nothing for a child whose problem is that it never registered.
  Because the remedy it replaces (`grid leave` then `grid join`) is what an operator types with no
  arguments, a bare `grid join --respawn` also has to work — but auto-detect probes loopback only, so
  an identity serving `--at <otherhost>` or an API engine has nothing to find. Target resolution's
  refusal is therefore **deferred** until the records have been read: with a live identity, its own
  union is what gets restarted; with none, the original error is raised untouched. Only a join that
  names *nothing* defers — with `--at`/`--serve`/`-m`/`--media`/`--kind` the operator stated an
  intent, and swallowing *that* refusal would hide a typo.

- **Service truth belongs to the process, so it travels exactly like the identity stamp.** A
  hot-reload is the same process and carries it forward (`carry_service_truth`); a respawn is a new
  one and must not (`clear_service_truth`, beside the existing "never a stale token" — and
  `started_at` is refreshed with it, so "up 42s" measures *this* process). Both live at the two
  functions that already own that distinction, which is what makes `_leave_one_engine` correct
  without being touched: it rebuilds its record by copying a survivor's, which is right on its
  hot-reload branch and cleared on its respawn branch.

- **`last_register_error` diverges from `last_reload_error` on the shrink, deliberately.**
  `_leave_one_engine` pops `last_reload_error` ("a fresh lifecycle attempt shouldn't inherit a stale
  failure") but is not extended to pop this one. A reload failure is sticky until the next reload; a
  *register* failure self-heals on the next successful heartbeat ~30s later, and the shrink's respawn
  branch already clears it at the choke point above while its hot-reload branch keeps the same live
  process — where the failure is still true.

- **Not written from the reload path.** A post-swap re-register failure leaves the relay holding the
  *old* union while the identity stays registered and heartbeating — so neither gate branch fires and
  the field would sit unread. That case already has `last_reload_error`, which the gate already
  surfaces. Keeping one meaning per field is worth more than the coverage.

- **One helper for the locked read-modify-write, not three copies.** `run_records.mutate_record` owns
  the three properties every writer here needs and each had been remembering separately: the record
  lock the CLI's join merge also takes ([ADR 0010](./0010-remote-join-append.md) F3), a **no-op when
  the record is gone** (a live child with no record is the untracked orphan this whole feature
  exists to prevent — re-creating one from a stale in-memory copy is the other way to manufacture
  it), and no rewrite when nothing changed, so a relay that has been down for a day costs one write
  rather than one per beat. It *raises*; callers keep their own never-raise wrapper, where the
  warning text belongs. `remote/serve._set_last_reload_error` was moved onto it.

- **Removal is one function.** `run_records.remove_record` unlinks the record and its sidecar
  together, at all four sites. The sidecar has to go wherever the record goes, and enumerating the
  sites by hand is precisely how a fifth one gets added later without it — leaving a heartbeat file
  with nothing to explain it, in the directory whose stray `.log`/`.lock` leftovers were the evidence
  trail this feature was diagnosed from.

- **`registered_at` is the one fact here that fails *closed*, so its write is retried.** Every other
  write fails open — a lost sidecar reads as "not a reporting build", a lost error reads as "no
  error" — but an absent `registered_at` reads as a confident *"has not registered"*. Since the
  bookkeeping wrapper swallows write failures by design (a disk hiccup must not stop a serving
  engine), a single lost write would otherwise leave a genuinely registered, happily heartbeating
  engine accused for as long as it runs — and the operator's natural response, re-running the same
  join, is the read-only no-op path that can never heal it. So the heartbeat loop retries the stamp
  until it lands, and the in-memory flags that make the healthy path free track what actually
  **landed on disk**, never what was merely attempted.

Consequences. A stuck or silently-dead engine is now visible from `grid join` alone, offline, with no
relay call — naming the pid, the age, the last error and the log. The cost is one `utime` per 30s per
engine and one record write per registration. Three things are knowingly left open: the >90s tail
above; the fact that a **bring-up** failure only reaches `last_register_error` on the path that
survives to write it — the child that dies at startup usually takes its record with it; and a
**persistently** unwritable record directory, where the child's sidecar never appears and the gate
therefore stays silent, indistinguishable from an older build. The last is the upgrade rule working
as designed rather than a gap in it — the pid stamp beside it has always accepted the same risk — but
closing it needs a general "this engine's state directory is unwritable" signal, which is larger than
this decision. Giving
bring-up a retry loop, a deadline and a narrated log is issue 11, which this deliberately does not
do; 10 makes the CLI read the narration honestly in the meantime.
