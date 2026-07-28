---
status: accepted
---

# A run record's pid is a claim, not a handle: identity and state at every seam

`os.kill(pid, 0)` answers "does this pid exist". Every seam in the CLI that reads a run record's
`pid` was using that answer for two questions it cannot settle — *is our serve child running?* and
*is this pid still ours?* — and both wrong answers reached production.

A **zombie** answers "alive". In a container whose PID 1 never reaps (`docker run` without `--init`,
PID 1 = `tail -f /dev/null`), every serve-child death mints one permanently. Field-confirmed
2026-07-27: `grid join` printed **"Already serving …; nothing to append."** and exited 0 over a dead
engine while `grid models` stayed empty, and every teardown SIGTERMed the corpse, burned the full
25s stop grace, SIGKILLed it, still read it as alive and reported an unstoppable survivor — which,
with this branch's honest-teardown gate, made a bare `grid leave` fail on **every retry, forever**.
The argv sweep cannot rescue that: a zombie's command line is empty, so it matches nothing.

A **recycled** pid also answers "alive", about somebody else. `_hot_reload_identity` sends `SIGHUP`,
whose default disposition is *terminate*, and `_respawn_identity` escalates SIGTERM → SIGKILL of the
target's whole process **group**. So a stale record could make `grid join` destroy an unrelated
process tree the operator owns — the CLI's own bookkeeping turned into a weapon — and then abort
with "Could not stop the engine(s) …" (measured: 26.8s spent attacking a bystander).

Scope: **this CLI only.** No wire value, no master or control-plane slice, nothing for the lockstep
register in `CLAUDE.md`. Evidence and the locked design: `.scratch/grid-leave/` (PRD follow-up F6,
issue 08 — Layer 1 of the approved three-layer proposal; issue 10 is Layer 2, issue 11 Layer 3).

Choices a future reader will otherwise re-litigate:

- **The identity is `(pid, start_time)`, and the start time comes from the kernel.** Linux reads
  `/proc/<pid>/stat` (field 22, parsed after the **last** `)` — the `comm` is chosen by the process
  and is not escaped, so it can contain spaces and parens); macOS runs one `ps -p <pid> -o
  state=,lstart=` under `TZ=UTC LC_ALL=C`, so the token cannot drift with the operator's locale;
  Windows reads `GetProcessTimes`. It is an **opaque string**, compared only for equality — never
  parsed, never ordered. Rejected a wall-clock stamp taken by the spawner: it is written by the same
  process whose crash produces the drift, and it says nothing about the process that now holds the
  pid.

  Two properties of the token are accepted rather than solved. On macOS `lstart` has **one-second**
  resolution (Linux's `starttime` is in clock ticks), so two processes starting inside the same
  second are indistinguishable there — reachable only for a sub-second-lived prior occupant of a
  pid, which a serve child never is. And the whole probe is **advisory**: if it fails systemically
  the CLI reverts to pre-token behaviour, which is why that failure is announced once per process on
  stderr rather than being silent.

- **A fact we could not read is `None`, never a placeholder.** This is the single rule the upgrade
  path rests on. A sentinel would compare *equal* across unrelated processes — turning "we cannot
  tell" into a confident match at the one seam where being wrong means signalling somebody else's
  process — and it would make every record written before tokens existed read as a **mismatch**
  (the verdict that withdraws trust) rather than as unverifiable (the verdict that preserves
  today's behaviour). `None` on either side is neither a match nor a mismatch.

- **`pid_alive` is not changed.** It keeps meaning "does this pid exist", because the orphan sweep's
  pid-exclusion logic depends on that meaning and 55 tests fake it to fabricate liveness. The new
  answers are *additive*: ask `pid_alive` first, and only then the richer probe. An unreadable
  process is "not a zombie, no token" — precisely the behaviour the CLI had before. So the probe can
  only ever *correct* a false "alive"; it can never authorise a signal a bare pid would not have.
  For the same reason the probe **never raises**: it runs in `grid join` immediately after the child
  is spawned, and an exception there would leave a live child with no record — the untracked orphan
  this work exists to prevent, manufactured by the stamp meant to prevent it.

- **Five verdicts, and only two may be signalled.** `LIVE_OURS` (token matches) and
  `LIVE_UNVERIFIED` (no token, or unreadable) are signallable — the second because refusing there
  would be a *new* failure on upgrade. `ZOMBIE` is **not alive**: the join gate respawns instead of
  no-opping, and the terminator confirms death immediately. `LIVE_OTHER` (token mismatch) is a
  recycled pid: never signalled, treated as stale-dead, left to the argv sweep, which matches by
  argv content and is identity-proof by construction. `DEAD` is no process at all.

- **`terminate_pid` stays token-free.** The sweep hands it pids matched from the process table, which
  carry no token by construction; making the token *required* there would silently stop it killing
  anything. Identity is the record-aware caller's job (`terminate_recorded`). `terminate_pid` gains
  only zombie-awareness.

- **The process group is the backstop the argv sweep cannot be, and it is stamped, not derived.** A
  `grid leave` can win the record `file_lock` before a just-spawned child self-stamps — POSIX
  `flock` has no ordering — so the record holds the *launcher shim*'s pid (the Nuitka `--onefile`
  bootstrap, the uv trampoline) while the real engine is a **member** of that shim's session group.
  A group id is not recycled while the group has members, so the stamped `pgid` still names our own
  descendants after the shim is gone: a reap that needs **no process table**, which is the one
  regime where the sweep is blind. Stamped rather than re-derived because `os.getpgid()` raises
  `ProcessLookupError` for a zombie on macOS — failing in exactly the regime it is needed for — and
  because writing `pid`, `pid_start_time` and `pgid` in one atomic record write is what lets a
  verified pid vouch for the group id beside it.

- **The group is probed always and signalled narrowly.** Signal 0 delivers nothing, so *reading* is
  free and an empty group is positive proof that nothing this box spawned is left — obtained without
  `ps`. *Signalling* requires provenance, and two limits are deliberate. Once the recorded pid has
  been reaped, nothing can authenticate a group id at all (`killpg(pgid, 0)` proves a group exists,
  not that it is ours, and group ids **are** reusable once empty), so an ancient record is left to
  the sweep. And while the recorded pid is a **zombie** the probe is uninformative, because the
  corpse itself occupies the group — escalating there would signal a group with nothing running in
  it, burn the stop grace on a process that cannot be reaped, and report the corpse as a survivor:
  the same dead-end, one level up. A live sibling hiding behind our corpse is the sweep's job.

- **`killpg(0, …)` addresses the caller's own group**, so `recorded_pgid` is stricter than
  `recorded_pid`: `0`, negative, out-of-range and every non-`int` shape answer `None`, and `None`
  never reaches a signal call. `recorded_pid` may return `0` for "never stamped" because a pid of
  `0` reaches a documented no-op; a **pgid** of `0` — which the same join write-race produces —
  would make `grid leave` SIGKILL itself and the operator's shell job.

- **A leave that verified nothing and scanned nothing fails loud.** Two independent nets catch a
  stranded child: the record (stop it by identity) and the argv sweep (find it in the process
  table). Either failing alone is fine and silent — that is what the other is for. **Both** failing
  at once is residual (b): leave printed `Left <grid>.` with a footnote and exited 0 having
  confirmed nothing, so the operator had no signal while a live orphan kept heartbeating. It now
  exits non-zero after the backstop deregister has fired. The record is **not** kept in that case,
  unlike a surviving-child failure: there is no live process it is a handle to, and an unverifiable
  record stays unverifiable forever, so keeping it would recreate the never-converging retry loop
  this ADR exists to end. The retry runs the bare idempotent-repair path instead. A record whose
  corpse is **permanent** — the container case, where PID 1 never reaps — is unverified for the same
  reason: its group can never be read, so claiming a proven teardown there would let `grid leave`
  print "Left …" over a live `llama-server` the argv sweep was never going to match.

- **A re-join inherits the identity's last known configuration when nothing is live.** Making a
  zombie count as dead empties the union the join merges into, so `grid join --at <second-engine>`
  onto a crashed identity would silently re-serve only the second engine and drop the first — a
  quieter failure than the "Already serving" lie it replaced. Union, media, bundles and concurrency
  inherit **together**, and the `--advertise-as` guard reads the inherited set too, because aliases
  are positionally keyed to a record's models and are not carried by the spec merge: an aliased
  identity is refused with the existing leave-then-rejoin instruction rather than silently
  de-aliased. Liveness still decides only hot-reload-vs-respawn, and a dead identity never no-ops.

Upgrade is free in both directions and needs no coordination: a record without a token gets the
zombie-aware state check and otherwise today's behaviour, and the child's next self-stamp adds the
token and the group. The residual verify-then-signal TOCTOU (the pid recycled in the microseconds
between the check and the signal) is unchanged and unfixable without `pidfd`; it is now measured in
microseconds rather than in days.
