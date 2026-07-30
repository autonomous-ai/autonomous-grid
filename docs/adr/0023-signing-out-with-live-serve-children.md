---
status: accepted
---

# Signing out stops serving first, and never removes the last handle on a live child

`grid logout` was eight lines: delete `credentials.toml`, clear the active pointer, print
"Signed out." It was also, measurably, the flow that made a live serve child permanent.

Two facts compound. A detached serve child loads its per-grid access token **once, at spawn**, and
that token's TTL is about a year; its refresh path re-reads `credentials.toml` only opportunistically
and falls back to the copy it holds in memory when the file is gone. So a signed-out box keeps
registering and heartbeating as a **provider**, advertising models to consumers, indefinitely. And
`credentials.toml` is simultaneously the only index of which grids this box knows *and* the gate every
remote verb passes through — `grid leave`, the one verb that could stop the child, answered "You're
not signed in" over run records that were still on disk and still correct.

Found by issue 05's deletion audit, which was looking for what *deleted* run records and cleared
logout of that charge: logout does not destroy records, it removes the only handle able to act on
them. Two other flows reach the same state — `grid sync` and `grid login` both replace `[[networks]]`
wholesale, so a grid dropping out of either takes its token with it while its child keeps polling.

Scope: **this CLI only.** No wire value, no master or control-plane slice, nothing for the lockstep
register in `CLAUDE.md`. Evidence and design: `.scratch/grid-leave/` (PRD follow-up F11, issue 13).

Choices a future reader will otherwise re-litigate:

- **Teardown, then delete — and the order is the whole mechanism.** Logout runs the existing
  authoritative full-leave teardown (kill by record identity per
  [ADR 0020](./0020-process-identity-at-run-record-seams.md), argv sweep, then the backstop
  `PUT role=consumer`) *before* `clear_credentials`. That is not a convenience: the backstop is
  addressed by the node id decoded from the per-grid access token's JWT claim, so the only moment it
  can be sent at all is while the store still exists. Anything needing a token runs first, or not at
  all. The alternative shapes were considered and rejected on one fact each — *refuse and let the
  operator run `grid leave`* leaves the box advertising until they do, which is exactly what an
  operator who believes they signed out will never do; *rescue only* leaves the grid page stale for
  the ~120s node TTL and only if they remember. Both were declined as the primary fix; the second
  ships anyway, as the net below.

- **The scope is run-record *directories*, not the credential list and not record files.**
  `read_records` answers `{}` for two states that must never be conflated: a grid never joined, and a
  grid whose record was unlinked out from under a **live** child — the untracked-orphan class this
  whole feature exists for, and the fingerprint issue 05 found nine of on the reporting machine
  (record gone, directory and child log intact). A record-file-scoped sign-out would report a clean
  teardown on precisely the box that still has one heartbeating. `run_records.known_grid_ids()` reads
  the directories instead, so it sees both. A box that never joined anything has no directory at all,
  which is what keeps an ordinary logout free of any process scan or network call — it must still
  work on a laptop that is offline.

- **The run tree is shared by both modes, so the scope needs a second filter.** `~/.grid/run/engines`
  holds a directory per grid of *either* mode — a local grid's `llama.cpp`/`ollama` engines sit beside a
  remote grid's `remote` identity, and nothing in the path distinguishes them. Widening the scope to
  directories therefore reached local engines, and a first cut of this change would have stopped a live
  local-mode engine on `grid logout`: a grid with no account behind it, that sign-out says nothing about,
  whose endpoint the operator never mentioned. The discriminator is a record's **`signaling_url`** —
  chosen because it *is* the difference rather than a proxy for it: a remote child polls a relay and
  records the one it polls, while a local engine is pushed to and records only its own endpoint. The
  argv sweep needs no equivalent (it matches `__remote-engine <network_id>`, so a local engine cannot
  produce a hit), which also fixes the fail direction: a remote record somehow missing the field is
  skipped by the record path and still found by the sweep. A false negative costs one extra
  `grid leave`; a false positive would take down someone's working local endpoint.

- **A grid we still hold a token for blocks the sign-out; one we do not, does not.** When the
  teardown cannot confirm a bundled grid's child is gone, logout keeps `credentials.toml` **and** the
  active pointer, and exits non-zero naming the pid — the same honesty `grid leave` already applies
  when it keeps a survivor's record. The tokens are the handle a retried leave needs to deregister
  authoritatively, so they are worth refusing for. A grid whose bundle an earlier sync/login overwrite
  already dropped has no such handle to preserve: it is reaped, reported, and never blocks. Clearing
  the active pointer while keeping the credentials was rejected outright — a pointer cleared over live
  credentials is a half-state no command reports.

- **`--force` overrides the refusal, never the attempt.** A child owned by another user, or wedged in
  a container, can be genuinely unstoppable, and a box's owner must always be able to remove their own
  credentials from it. So `--force` still runs the full teardown and still sends the deregister; it
  only declines to keep the store afterwards. It is not silent: the survivor is named on stderr with
  the `grid leave <grid-id>` that reaches it, which works precisely because of the next decision.

- **`grid leave` gains a signed-out rescue path, and a credential bundle always beats it.** Named with
  an explicit grid id, leave now reaps a child with no credentials for that grid at all: record
  teardown plus argv sweep, no backstop, node TTL as the fallback, said out loud rather than implied.
  This is what makes the dead end structurally impossible rather than merely unlikely, and it is the
  only thing that repairs the machines already in the bad state — an operator who signed out on a
  build that did not tear down. Two guards are load-bearing. A stored bundle wins whenever one
  matches, because the rescue bundle carries no `access_token` and resolving one for a grid we *are*
  signed into would silently skip the deregister and leave the node registered — a regression wearing
  a fix. And the fallback demands an explicit id, never a bare or active-grid resolution:
  `~/.grid/run/engines` accumulates a directory per grid a box has ever joined (twenty on the
  reporter's Mac), and naming one is the operator's consent. A third guard is a refusal: a
  `--engine` **shrink** is rejected on this path, because a shrink keeps the identity serving by
  respawning it with the reduced union, and the respawned child must register with a token it does not
  have — so it would stop a working engine and start one that dies on its first relay call. The whole
  identity is the only thing a signed-out leave can honestly tear down, and it says so.
  `require_session()` has a dozen call sites across `cli/`; only the leave path gets a soft variant.
  That is deliberate — leave is the repair verb, and the state it most needs to repair is the one where
  the credentials are gone.

- **`grid sync` and `grid login` warn; they do not tear down.** Both keep their authoritative
  overwrite. The asymmetry with logout is the point: logout is an operator's intent to stop using this
  box, while a control-plane answer is not — a transient one returning fewer grids would destroy
  working capacity that nothing was wrong with, unrecoverably without a re-join. So they name what
  they stranded, the pid, and the `grid leave <id>` that still reaches it. Dropped grids are diffed on
  `network_id`, never on the display name, or a grid renamed on the website would read as one
  vanishing and another appearing and warn about a child nothing stranded. This means issue 13's
  acceptance criterion 3 — "the sync path reaches the same outcome as logout" — ships **restated**:
  what is guaranteed is that neither path leaves an *unrepairable* state, not that both stop engines.

- **The lock covers the decision, not just the kill.** Each grid is decided *and* torn down under the
  same record `file_lock` that `grid leave` holds, so a concurrent `grid join` either finished — its
  record is visible to the read inside the lock — or waits behind us. The single process-table read
  cannot be inside a per-grid lock, because it answers for every grid at once, and that is not an
  oversight but a cost decision: a cold PowerShell + WMI enumeration is seconds, so one read per grid
  would tax a Windows sign-out in proportion to how many grids the box has ever joined. The residual
  is a *record-less* child spawned between that read and a grid's lock, missed by this logout and
  caught by the next `grid leave <id>`. It is narrow because a completed join always writes its record
  under the same lock.

- **What the sign-out could not check travels back with it.** The scope optimisation above — only act on
  grids with evidence of a live child — is also the one place this change could have lost the rule the
  rest of the family enforces. A record-less orphan is visible *only* in the process table, so when that
  table cannot be read a grid with no live record has produced **no evidence**, not "no child"; skipping
  it silently is how a sign-out would report a clean exit over a live provider and then delete the
  credentials. So `stop_serving` returns the grids it could establish nothing about alongside its
  outcomes, and logout names each one. It does **not** refuse: a box with several stale record
  directories would then be unable to sign out at all, for a condition signing out cannot fix. And
  crucially it still **deregisters** such a grid while it holds the token. The sweep is a diagnostic and
  the backstop is the mechanism of record — which is exactly why `grid leave` sends it unconditionally —
  and the backstop needs no process table, so withholding it here would leave the grid advertising this
  box's models over a child the sign-out could not even look for. The flip is idempotent and
  resurrection-proof, so sending it for a grid that turns out not to be serving costs one request; a
  grid with no bundle has no token and gets the warning alone, with the TTL named as its fallback. The
  remedy printed is real only because the rescue path above ships with it: `grid leave <grid-id>` needs
  no credential store, so it still works after the delete. `grid sync`/`grid login` carry the same flag
  and name each dropped grid they could not check — their warning is the *only* place a stranded child
  can surface at all, since the overwrite has already taken the token. One primitive still has no flag to
  carry — `known_grid_ids()` cannot distinguish a missing run tree from an unreadable one — so a
  non-`FileNotFoundError` there prints a note rather than answering "no grids" in silence.

- **What is refused is named before what is refused about.** A sign-out can fail on several grids at
  once, and only the bundled ones refuse. Raising on the first would hide an unbundled survivor
  completely: the operator clears the block, retries, and meets a second failure nobody mentioned. So
  everything the sign-out is *walking away from* is printed first, and the blocked grids are excluded
  from that list precisely because their credentials are being **kept** — telling them "signing out
  removed the credentials that could deregister it" would be false. They are named by the refusal
  instead, and join the walked-away list only when `--force` really does walk away from them.

- **`--json` gained a key rather than a conditional one.** `grid logout --json` now emits
  `{"signed_out": …, "stopped": [{"grid": …, "deregistered": …}]}`, with `stopped` present and empty on
  a box serving nothing. A script that signs a machine out needs to know whether a workload stopped on
  the way; a key that appears only sometimes is worse than one that is sometimes empty. No token
  reaches the payload, as on every other `--json` path on this surface.
