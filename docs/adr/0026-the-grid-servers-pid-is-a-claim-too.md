---
status: accepted
---

# The grid server's pid is a claim too, and `grid stop` now says what it could not establish

[ADR 0020](./0020-process-identity-at-run-record-seams.md) replaced "does this pid exist" with a
verifiable identity at every **run record** seam. `grid stop` was never reached by it, because the
local grid server's pid lives in the *grid config* — a different record, written once at `start_grid`
— and so it kept the whole pre-0020 hazard set in one function:

```python
pid = int(cfg.get("server_pid") or 0)          # ValueError on a hand-edited "abc"
if not pid: return
try:
    if hasattr(os, "killpg"):
        os.killpg(pid, 15)                      # a whole process GROUP, by a bare number
except ProcessLookupError:                      # does NOT catch OverflowError (an ArithmeticError)
    pass
cfg["server_pid"] = 0                           # written unconditionally, even if nothing died
```

A recycled `server_pid` therefore SIGTERMed an unrelated process group the operator owns; a corrupt
one crashed `grid stop` *and* `grid start`; and a server that ignored SIGTERM survived **and** lost the
only handle to it, after which `grid start` dead-ends on `Port 8090 is already in use` with nothing left
able to stop what is holding it — the unconditional-write shape that was the grid-leave root cause.
`local/runtime._pid_alive` was a second, weaker copy of `run_records.pid_alive`: no range guard, no
zombie-awareness, and `PermissionError ⊂ OSError` made it call EPERM *dead* where the shared one calls
it alive.

**This is the last such site in the grid-lifecycle path, not in the CLI.** `shared.engine.comfyui`'s
`stop_running` is the other: `os.kill(pid, 15)` on a pid read from a file, no range guard — and
`os.kill(-N, sig)` addresses process **group** N — no liveness probe, then an unconditional unlink and
a success message regardless. It belongs to a still-open sibling follow-up and is deliberately
untouched here; a future reader must not take this ADR as closing the class.

Scope: **this CLI only.** No wire value, no master or control-plane slice, nothing for the lockstep
register in `CLAUDE.md`.

Choices a future reader will otherwise re-litigate:

- **The identity decides what may be signalled; the port decides whether it worked.** These are
  different questions and the obvious single answers each fail one of them. A health check *cannot*
  authorise the signal: "port 8090 answers" says nothing about whether `server_pid` is that listener,
  so a recycled pid plus a healthy grid still kills the wrong group — and gating the signal on health
  would additionally make a *wedged* server (alive, not answering) permanently unstoppable, in the one
  case an operator most needs the command. The identity alone cannot report: a config that can never
  be verified classifies `verified=False` exactly like every alarming case, so a rule keyed on it
  would fail on an **already-stopped** grid forever — `server_pid: 0` ⇒ `DEAD` ⇒ unverified — and
  there is no `grid rm` to escape with. The port probe is what makes an unprovable pid **converge**.

- **Only a refused connection is proof — and that question is asked at the socket, not read off an
  exception type.** The probe is tri-state (`True` still serving / `False` nothing listening / `None`
  could not tell) because it is the thing that clears the identity and exits 0. The first
  implementation took `httpx.ConnectError` to mean "refused", which is **wrong and was reproduced**:
  `socket.create_connection` resolves DNS *and* connects in one call, and `socket.gaierror`,
  `ENETUNREACH` and `EHOSTUNREACH` are all plain `OSError`s, so httpcore maps every one of them to the
  same `ConnectError` a genuine refusal produces. `grid stop` on a host that had merely become
  unreachable therefore printed "is down", exited 0, and cleared the recorded pid over a live server —
  reachable through the very `--host` support above, e.g. a laptop that roamed networks between
  `grid start` and `grid stop`. So absence is now decided by a raw `socket.create_connection` and the
  precise builtin `ConnectionRefusedError` (PEP 3151 gives every errno its own subclass), which needs
  no knowledge of httpx's or httpcore's exception wrapping — the layer where the distinction was lost.
  The HTTP call is still made, but only to answer the *other* half: `/grid/info` returns the `grid_id`,
  so the probe is identifying rather than merely "something is listening". A timeout, a torn-up
  config, or a reply that is not a `/grid/info` all mean we did not find out.

  Two consequences of a probe reading a config field as a *request target*. The `host` is validated as
  a bind address — a value carrying URL syntax is refused, because `evil.com/x` silently becomes host
  `evil.com` with the configured port parsed as part of the **path** (so the probe reaches port 80),
  and `a@evil.com` hides the real host behind userinfo; no address a server can bind contains one of
  those characters, so refusing costs nothing and gives a mangled `host` the same honest "we never
  asked" an unusable `port` already gets. And the foreign `grid_id` named in the mismatch note is
  whatever the process holding our port chose to send, so it is truncated and `repr`'d before it
  reaches the terminal rather than printed raw.

- **The probe addresses the host the server bound.** `grid start --host 10.0.0.5` really binds only that
  address, while both health probes asked `127.0.0.1` — and `start_grid` saves the pid *before*
  `wait_for_health`, so a live server with a recorded pid is reachable in one command. A loopback-only
  probe would then report that server as proof the grid had stopped. One helper maps a wildcard bind
  (`0.0.0.0`, `::`, empty) to loopback and addresses anything specific as itself; `wait_for_health`
  uses the same one, so the two cannot disagree about where the server is. That incidentally makes
  `grid start --host <lan-ip>` succeed where it used to time out.

- **The identity is cleared only on success — and *kept* when nothing was established.** One rule,
  read by both the config write and the message, so they cannot disagree. This deliberately diverges
  from the sibling rule in local `grid leave`, which *drops* an unverifiable record because "an
  unverifiable record stays unverifiable forever" and keeping it would never converge. That argument
  does not transfer: there the record was the only thing that could converge, here the **port probe**
  is, and it is orthogonal to the pid. A cleared identity still yields unverified + unprobeable and
  fails identically, so clearing buys nothing and throws away the only handle a retry has — there is
  no argv sweep behind this one. A never-stamped `pid: 0` is kept for the same reason rather than
  special-cased.

- **Prefixed config keys, not a nested identity object.** The config already names the pid as
  `server_pid`, so the stamp lands beside it as `server_pid_start_time` and `server_pgid`. Nesting
  `identity_stamp`'s dict whole would have been less code and puts **two pids on disk** for a
  hand-edit or a partial write to disagree about. `run_records.IDENTITY_FIELDS` exists for the
  *reader* — a writer derives the key list from `identity_stamp`'s own dict and gets a fourth field
  for free, while a reader assembling that dict cannot, and the obvious substitute (a
  `startswith("server_")` sweep) would silently fold any future unrelated `server_*` key into a record
  the teardown then signals on.

- **No argv sweep for `__server`.** Local `grid leave` got one
  ([ADR 0025](./0025-local-leave-gets-a-second-net.md)); this does not, and the reason is
  [ADR 0024](./0024-what-the-argv-sweep-may-kill-and-what-it-can-see.md)'s own discriminator. A serve
  child takes **two** positionals after its marker, which is what tells it from an operator's
  `pgrep`/`watch`/wrapper carrying the same tokens. `__server <grid_id>` takes **one**, so
  `pgrep -f "__server ag-home-x"` renders as marker + exactly one token and would be force-killed. The
  port probe is a better and cheaper second net here precisely because a grid server, unlike an engine
  child, is HTTP-reachable and says which grid it is.

- **`grid stop` is now synchronous, and can take up to ~51 seconds.** It was fire-and-forget: one
  `killpg`, no wait, no confirmation. It now spends the shared teardown's 25s stop grace, a ~1s reap
  settle, and — only when the recorded pid has gone while its process group has not — another 25s on
  the group. A healthy exit is unaffected: every wait polls and returns the moment the process dies.
  This is a contract change, not an implementation detail, which is why it is stated in the command
  reference too.

- **Two inherited behaviours worth knowing before reading a transcript.** On Windows the teardown is
  now `taskkill /F /T` — a forced **tree** kill — where it was a forced kill of that process alone; the
  grid server spawns nothing, so in practice they are identical, but it is a widening. And the grid
  server now rides the *engine* record shape, so a failed group teardown prints "Could not stop every
  process in **serve group** …". The wording is left shared rather than special-cased: one string, one
  behaviour, and a lifecycle-specific copy is a second thing to drift.

## Consequences

`grid stop` can now fail, in three ways, none of them a false alarm: a server that outlived SIGKILL
(named as a pid, or as a process *group* where that is what survived, with a remedy that reaches it),
a grid still answering on its port (named with the port and how to find what holds it), and a teardown
that could neither verify the pid nor reach the port (named with the check to run by hand). In every
one the recorded identity is kept. A corrupt `server_pid` is a note on stderr and a no-op rather than a
traceback, on both `grid stop` and `grid start`; whether it *mattered* is the probe's answer, not the
note's.

Upgrade is free in both directions and needs no migration: a config with no token reads as
`LIVE_UNVERIFIED` and is stopped exactly as it is today, and the next `grid start` stamps it.

Residuals, named rather than fixed.

An unusable `port` in the config makes the probe permanently unavailable, so a `grid stop` that also
cannot verify its pid fails until the port is corrected — *usually*, not always: the teardown can
still return `verified=True` on its own if the recorded pid is gone and the stamped `pgid` names an
**empty** process group, which is genuine positive evidence (nothing is running in it, whoever it
belonged to) rather than a hole. Either way the message names the field and its value, which is the
only remedy that exists — the alternative, treating "could not ask" as "nothing answered", is the
exact laundering above.

**The identity check is not a security barrier, and should not be read as one.** It defends against
*accident* — a recycled pid, a stale config, a corrupt value — not against someone who can write
`config.json`. A hand-crafted config that simply **omits** `server_pid_start_time` lands in
`LIVE_UNVERIFIED`, which is signallable by design so that upgrades never break; a *forged* token is
therefore strictly worse for an attacker than no token at all, so there is nothing to harden by
making forgery harder. Two things did widen: `server_pgid` is a **new second, independently chosen**
signalling target where the old code reused one number, and the escalation now reaches SIGKILL where
it used to send a single SIGTERM. The precondition is write access to a `0600` file under `~/.grid`,
i.e. code execution as that same account — which already exceeds what signalling a process buys — so
this is an amplified footgun rather than an escalation, and it is stated instead of implied. Narrowing
it (requiring provenance before the *group* is signalled, which no genuine upgrade needs, since a
pre-token config carries no `pgid` either) belongs to the shared teardown in
[ADR 0020](./0020-process-identity-at-run-record-seams.md) and is filed as a follow-up rather than
done here.

One inherited hazard was fixed rather than inherited: `pid_alive` reports EPERM as *alive*, so a
drifted `server_pid` on another user's process reached `terminate_pid` and raised `PermissionError` —
a raw traceback out of `grid stop`, contradicting this ADR's own promise. It is caught at the call
site and **not** inside `terminate_pid`, because `orphan_sweep.terminate` depends on that exception
escaping to classify a swept match as `foreign`; swallowing it there would blind both modes'
`grid leave` to another user's serve child. Nothing is concluded from it — the port still decides.
