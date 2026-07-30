---
status: accepted
---

# Engine health in the heartbeat: a forbidden list of models, withheld at discovery

A remote provider's serve child heartbeats every 30s and never once asks the engine behind it
whether it is still there. So a `llama-server` that died — crashed, OOM-killed, stopped by hand —
leaves a live child advertising its models to the grid: `grid models` lists them, the relay routes
to them, and every job fails at forward time. On a busy grid the relay's reactive machinery
eventually notices (three failures inside ten minutes demote the node); on an **idle** grid there
are no failures to count, so nothing ever notices and the advertisement is wrong indefinitely.

This is the wire half of a truth the leave work fixed locally. Issue 05 stopped a dying child from
deleting a newer child's run record; issue 10 made the join no-op gate verify *registration*, not
process existence. Both taught the same lesson at file scope — "the process is alive" is not "the
service works" — and this ADR applies it at grid scope. Grill log + measured evidence:
`.scratch/grid-leave/` (PRD follow-up F3, issue 07).

Scope: this CLI **and** grid-src (the relay + the public overview); grid-apis has **no slice** —
the field never leaves the master. One new hand-duplicated wire value with no compile-time link
between the copies: the `load` key **`unhealthy_models`**, which joins the endpoint-path literal,
the per-model `endpoints` list, `output_cap` and `stream_only` in `CLAUDE.md`'s lockstep register.
Written **before** any code lands: the decisions below are normative, not descriptive.

Choices a future reader will otherwise re-litigate:

- **The field is a FORBIDDEN list — the models the provider currently cannot serve — never an
  allowlist of the ones it can.** The provider merges `load["unhealthy_models"]: list[str]` into the
  heartbeat it already sends; absent means nothing is withheld. This polarity is not a style
  preference, it is the only one that survives a mixed fleet, and it is the same inversion
  ADR 0018's `stream_only` axis reached from the other direction:

  | | old CLI | new CLI |
  |---|---|---|
  | **old master** | today | field stored in `NodeRow.load`, never read ⇒ today |
  | **new master** | field absent ⇒ nothing withheld ⇒ today | the only new behaviour |

  The same argument covers every degenerate case *inside* a new CLI, which is the part an allowlist
  cannot reach: the probe has not run yet, the probe raised, the probe budget expired, the identity
  serves no hardware engine. All of those emit **no key**, and no key means no change. Capacity can
  only be withdrawn by a probe that positively observed a transport failure, twice. Rejected
  **`healthy_models` as an allowlist**: absent would have to mean "no model is healthy", so every
  provider on an older CLI would be silently unlisted the moment the master shipped — a fleet-wide
  outage produced by a rollout, which is the exact failure class the lockstep register exists to
  prevent. Rejected **`engine_ok: false` as a positive-health boolean** for the same reason one step
  down: "false" and "not yet known" collapse into the same value, so a child that has not finished
  its first probe reads as sick.

  Corollary, and the thing to keep pinned by a test: for a healthy box the heartbeat payload is
  **byte-identical to today**. The key is omitted, not emitted empty.

- **The wire shape is per model, even though the evidence is per engine.** What the probe learns is
  that one *engine URL* stopped answering; the field expands that to the models routed behind it. On
  a single-engine box — most boxes — the result is indistinguishable from a box-level boolean, and
  claiming otherwise would overstate the probe's resolution. What justifies the per-model shape is
  the **union**: `grid join --all` and a `grid join --api` append serve several engines under one
  identity (ADR 0010), and a box-level bit there withdraws the healthy siblings along with the dead
  engine. That is not hypothetical — it is the Nightshift outage of 2026-07-20 rerun as a design
  choice, where one dead model demoted a three-model node every ten minutes and took its two working
  siblings down with it. Rejected **the box-level boolean**: cheaper on both sides, correct for the
  common box, and wrong in exactly the configuration the grid encourages people to run. Rejected
  **reporting engine URLs instead of model names** (`{"http://127.0.0.1:8080/v1": "unreachable"}`):
  it publishes this box's internal topology to the master for no gain, and the master would have to
  learn a route map it deliberately does not keep.

- **"Unreachable" means the TCP/HTTP conversation failed — nothing more.** The probe is a
  `GET {engine}/models` with a short timeout, and **only a transport failure counts**. Any HTTP
  status, 404 included, proves a server is listening and is treated as healthy. This is a narrower
  claim than the field's name, and deliberately so: the observable that motivated the issue is a
  *dead process*, and every widening of the claim adds a way to withdraw capacity that is actually
  fine. Rejected **treating 5xx as unhealthy**: an engine returning 503 under load is busy, not
  dead, and withdrawing it converts a queue into an outage. Rejected **an inference probe** (a
  one-token completion, the way join-time capability probing works): it spends real compute every
  30s per engine, it can time out on a legitimately busy engine, and against an API engine it would
  spend the operator's money. Consequence to state plainly rather than discover later: an engine
  whose HTTP server is up but whose **model failed to load** still advertises. That is a real gap,
  left open on purpose — the reactive per-(node, model) prune already covers it once traffic
  arrives, and closing it needs a claim about model state that no single endpoint answers portably.

- **Two consecutive failures withdraw; one success restores.** The asymmetry is the point: a
  withdrawal removes capacity from a working grid, a restoration only re-adds it, so the expensive
  direction gets the debounce and the cheap direction gets none. Rejected **withdrawing on the first
  failure**: a single dropped packet or a listener restarting between ticks would unlist a healthy
  model. Rejected **a decay/penalty window on the provider side**: the master re-reads the row's
  `load` on every read, so "still unhealthy" is re-asserted by every heartbeat and a stateful
  cooldown would only delay recovery.

- **Rate limiting is the heartbeat loop itself, and no second interval is introduced.** The probe
  runs once per heartbeat tick — one `GET /models` per hardware engine per 30s, never per job and
  never per poll. A nominal `PROBE_INTERVAL = 30.0` beside `relay.HEARTBEAT_INTERVAL = 30` was
  considered and rejected as a constant that can never fire: its only caller already waits the same
  30s *plus* heartbeat round-trip, so the guard would be true on every tick and could only be
  exercised against a fake clock. A guard that no production timing can trip is not a guard.

- **The probe runs AFTER the heartbeat it could have informed, costing one tick.** It occupies the
  slot `_maybe_refresh_codex` already established — supervised background work on the heartbeat
  thread that never raises. So a verdict formed on tick *N* ships on tick *N+1*, and with the
  two-failure debounce the worst case from engine death to withdrawal is **~90s**. Probing first
  would save a tick and cost up to the probe budget of heartbeat delay. Rejected, because the two
  are not comparable in consequence: a late heartbeat costs the node its 120s TTL and unlists
  **everything** it serves, while a late verdict costs one more tick of a stale advertisement that
  has already been wrong for minutes. The heartbeat's liveness is load-bearing; the health bit is
  not.

- **A round classifies per engine, and owes the ones it missed a probe next time.** Both properties
  exist because the sweep can fail *partially*, and a partial failure that pretends to be a whole
  one is how this feature would quietly break. Each URL's probe is guarded individually — the
  `orphan_sweep` discipline of classifying per pid — because one engine must never veto the round
  and freeze every sibling's verdict; the case is real rather than theoretical, since
  `httpx.InvalidURL` is not an `HTTPError` subclass and so a malformed engine URL escapes the
  probe's own guard. A probe that *raises* is recorded as **unchecked**, never as a failure:
  withdrawing capacity requires evidence, and a fault is not evidence. And because iteration order
  is the snapshot's stable route order, whatever a round could not check (budget or fault) is probed
  **first** in the next one — otherwise a box whose earlier engines reliably eat the budget starves
  the last one forever, and an engine already withdrawn could never receive the single success that
  restores it: a retry that can never succeed, which is worse than the bug this ADR fixes. Both the
  budget truncation and a probe fault are reported, the latter on the crossing only, since a
  malformed URL never fixes itself and is re-probed every round.

- **The probe is bounded against the TEARDOWN budget, not only against the heartbeat interval.**
  The obvious reading of "don't delay the heartbeat" weighs the sweep against the 30s tick and the
  120s TTL, and by that measure a 10s budget is comfortable. It misses the interaction that
  actually matters: on stop, `_serve_loop` joins the poll workers, then the heartbeat, then the
  reload daemon against **one shared `_DRAIN_TIMEOUT`** — and the reload daemon must get its turn,
  because it can have a `register_once` in flight whose PUT, landing after the caller's
  `unregister_node`, resurrects the node being torn down (ADR 0010 C5). A sweep that runs its full
  budget therefore spends the reload daemon's entire share from inside the heartbeat's join, and
  re-creates the zombie-advertisement class this epic exists to close. Two things follow. The sweep
  watches `state.stop` between engines, so a leave abandons the remaining ones (as *unchecked*,
  never judged) instead of being waited out. And one probe's own worst case is kept **below** the
  drain budget, which forces the timeout to be stated per phase: httpx expands a bare float to
  connect/read/write/pool independently, so a nominal 3s really means a slow connect *and then* a
  slow read — about 6s, longer than the whole drain. The two constants live in different modules
  with no import between them, so a test pins the relationship rather than a comment asking someone
  to remember it.

- **The master withholds at DISCOVERY, not in the failure-penalty box.** The issue's own wording
  asks to reuse "the existing per-(node, model) prune machinery". This reuses its *predicate*
  position and explicitly not its state: `registry.discover_providers` rebuilds each `ProviderInfo`
  with the withheld models removed, which is one edit that every current caller inherits — the
  selection path, the responses bare-name alias, `/relay/v1/models`, the auto-router candidate
  build, media selection, and `/nodes/discover`. Rejected **populating
  `_provider_model_pruned_until` from the heartbeat**: that box holds a pairing down for
  `provider_model_failure_penalty_seconds` (600s) after it is set, so an engine that came back would
  stay unlisted for ten more minutes — contradicting this issue's own "recovery on the next healthy
  heartbeat" criterion. A self-report is **live state**, not a failure event: it needs no clock, no
  expiry, and no in-memory dict, because the row is re-read on every request. The two surfaces the
  choke point cannot reach get their own gate for structural reasons, not by preference — the
  explicit-target path builds its provider from `get_node` rather than discovery (and answers with
  the existing **retryable** 503, never a capability 400, because the request is fine and the grid
  is briefly not), and the public overview reads `NodeRow` directly.

- **On the overview the filter keys on the ROUTE, never on the display name.** `_served_models`
  returns friendly display names, and its own docstring warns that those collapse collisions across
  routes — two engines advertising the same model name produce two routes and one name. Filtering by
  name would therefore drop a healthy sibling route whenever a dead engine happened to share its
  name, converting this feature into the very over-withdrawal the per-model shape exists to avoid.
  The drop happens inside the per-route loop, matching each route's own `raw_model_id`.
  Consequence, stated so it is not read as a bug: a node whose models are **all** withheld stays
  **listed** with an empty model set. It is alive and heartbeating, and hiding the box would hide
  the problem the operator needs to see.

- **A withheld model reads to a consumer exactly like a model the grid never served, and that is
  accepted.** Because the withdrawal happens inside `discover_providers`, the non-target selection
  path never reaches the `capable_but_pruned` branch that gives the reactive prune its own
  distinguishing *"temporarily unavailable, retry"* 503 — a health withdrawal lands in the generic
  `no_providers_available` bucket instead. The explicit-target path does distinguish (it has to gate
  separately anyway), so the two disagree in wording. Accepted rather than fixed, for two reasons:
  the message is not untrue (the grid genuinely serves nothing for that name right now), and it is
  the **same** message the consumer gets when a provider leaves — which is the consistency this
  whole feature is for, since "the engine died" and "the provider left" are the same fact from the
  grid's side. Rejected **a second discovery pass on the 503 path** to recover the distinction: it
  buys nicer wording on an already-failing request at the cost of another query and a second place
  where withholding is decided. Recorded because a reader comparing the two 503s will otherwise read
  the difference as an oversight.

- **The public overview says a model is gone, never why.** A node whose engine died shows
  `"models": []` with `"online": true`, and the curated catalog entry for that model disappears from
  the grid page — with no `unhealthy`/`degraded` field anywhere in the public JSON to separate
  "briefly unhealthy" from "provider left". That is deliberate (the same reasoning as not putting
  engine URLs on the wire), but it leaves a real blind spot for anything consuming
  `/relay/v1/grid/overview` directly — a dashboard cannot show "degraded" because nothing tells it.
  The master log is where that event exists; adding a public field is a consumer-facing API change
  and belongs to whoever owns that surface, not to this slice.

Deliberately unchanged, so the withdrawal is not read as wider than it is: **media engines are not
probed** — media models never enter the routing snapshot the probe reads (they route through the
box's ComfyUI URL and its own model list), so their exclusion is enforced by the data shape rather
than by a rule someone must remember; **API engines are not probed**, because a vendor's uptime is
not this box's health and pinging a metered key or a flat-rate seat every 30s spends the operator's
credential to learn something the grid cannot act on anyway; **`registry.live_models_for_provider`
is untouched**, though it mirrors the overview's model derivation and a reader will find it —
unhealthy is not unregistered, and a transient probe must not churn the admin model catalog's
`off` switches; **local mode is untouched**, having no heartbeat of this kind; and there is **no
master-side kill switch** in this slice — the master honours what a provider reports, so the way to
stop withholding is to fix or leave the engine.

Rollout order is free in both directions, which is the whole payoff of the polarity above: a CLI
that reports the field to a master that ignores it behaves exactly as today, and a master that reads
the field from CLIs that never send it behaves exactly as today. Neither half is urgent, and neither
half can break the other.
