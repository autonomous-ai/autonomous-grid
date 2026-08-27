---
status: proposed
---

# The starter engine has a meter and a clock, and neither of them lives where the money is spent

A grid created from the web front end stands an engine up for itself: the control plane spawns the
public CLI into a per-grid home and joins it with a credential the member never holds, forwarding
every request through a passthrough the control plane owns. It shipped in public CLI v0.3.26 and it
is live. It has no ceiling and no end — the PRD said so in as many words and deferred it:

> an unbounded number of strangers can spend the operator's money on a public grid. Accepted
> deliberately; it is the first thing a ceiling should cover.

This is that ceiling, plus the thing the PRD did not name: an engine that never stops is a bill that
never stops, even on a grid nobody has opened in a month.

Five facts decided the shape, and four of them are the opposite of what the obvious implementation
assumes.

- **`auto` prefers the engine the operator pays for.** `auto_router.order_candidates`' first sort key
  is `-free_capacity`, and `shared/run_records.effective_max_concurrency` returns
  `API_ONLY_DEFAULT_CONCURRENCY` (4) for an identity whose every engine is an API engine — which the
  starter engine is — against `1` for a member's llama.cpp or Ollama box. So today the paid engine
  sorts *ahead* of the hardware a member contributed. Nothing has to be broken for the bill to grow;
  it is the resting state.
- **A turn is many requests.** `db.TransactionRow.conversation_id`'s own note records an agent turn
  measured at **156 `POST /messages` in ten minutes**. A ceiling counted in HTTP requests refuses a
  person in the middle of their first sentence.
- **The per-turn key is chosen by the caller.** `turn_id` is the caller's `X-Request-Id`
  (`relay._read_turn_id`), and the column is nullable. A client that sends none, and a client that
  sends one value forever, both defeat a turn count — in opposite directions.
- **The passthrough knows nothing about who is spending.** Every grid's starter engine presents the
  same `GRID_OPENROUTER_PROXY_TOKEN`; the forwarded body carries a model and no identity, and no
  ledger row is written. Meanwhile `relay.settle` marks a free network's transaction at $0 with *"no
  charge, no report"*, so `_report_usage` never fires for exactly these grids. **The operator has no
  record, at any granularity, of what this feature costs.**
- **The relay cannot stop it.** The starter engine is a subprocess of the control plane;
  `first_provider.stop_first_provider` is the only thing that can end it, and today it is called from
  one place — deleting the grid.

## Decision

### D-a — The meter is enforced at the relay, keyed on the grid, and counts only what the starter engine served

The relay is the only component that holds all three facts at once: who is asking, which engine
answered, and how long ago. The passthrough holds none of them and would need a new identity on the
wire to get any.

Keyed on the **grid**, not the member: a per-member allowance multiplies the operator's cost by the
size of the grid, which inverts the intent. Keyed on the grid, inviting people costs the operator
nothing — a property the invitation copy has to respect, because inviting a *consumer* now makes the
allowance run out faster rather than slower. The call to action is people with machines.

Counted only against transactions the **starter engine** served (`transactions.provider_node_id`).
Counting a member's own hardware would make the allowance a product cap rather than a cost control,
and would make the popup's advice false: start your own engine, be refused anyway.

### D-b — A turn is what a person is told; a raw request ceiling is what actually holds

Two counters over the same rows in the same window:

- **turns** — `COUNT(DISTINCT turn_id)`, the number a popup can say out loud;
- **requests** — `COUNT(*)`, which no header can talk its way out of.

Whichever is reached first refuses. The turn count alone is defeated by one constant `X-Request-Id`;
the request count alone refuses an ordinary person mid-sentence. Neither is redundant, and the second
costs one more aggregate in a query already being run.

⚠️ A NULL `turn_id` counts as a turn of its own, never as one shared bucket. Every caller that sends
no `X-Request-Id` would otherwise share a single turn forever, which is the same hole as the constant
header with none of the intent behind it.

### D-c — The starter engine is recognised by its service kind, compared for equality

The relay identifies it as a node whose `meta.engine` **equals** a member of a closed set of
grid-run service kinds — the kinds whose `ApiWhitelist.member_joinable` is false. It is not
recognised by its display name: `meta.name` is `"Grid"`, and a member is free to name their own
machine that.

This needs no rollout at all: `cli/remote_provider` already sets `engine_label` to the kind and
`remote/serve._meta` already sends it, on the CLI running in production today. The cost is a value
hand-duplicated across repos, kept in lockstep by a test on both sides — the same discipline
`SERVED_MODEL_IDS` ↔ `OPENROUTER_WHITELIST` already lives under.

A control-plane route announcing the node id to the relay was considered and rejected as too
expensive for one bit: it is a new cross-repo seam that would additionally have to survive master
restarts and re-joins.

⚠️ The failure direction is silent and it points at the operator's wallet: a second grid-run kind
added to the CLI and not to this set serves traffic nobody counts. That is what the lockstep test is
for, and why it is a condition of the work rather than a nicety.

### D-d — Fixed five-hour windows, anchored on when the starter engine arrived

`web_search.ALLOWANCE_WINDOW_SECONDS` is rolling, and its comment gives the reason: a calendar reset
lets an account spend two allowances back to back across the boundary. That argument is much weaker
here, because **D-f bounds the whole lifetime** — total spend is `≈5 windows × ceiling` whatever
shape the window has, so the shape is a UX question rather than a money question.

Fixed wins it on two counts. It is the only shape a countdown can be honest about ("returns at
14:30"), where rolling hands back one turn at a time and re-opens the popup every few minutes. And
an anchored fixed window needs **no stored state at all**: the window index is arithmetic on one
timestamp, so the decision is one `COUNT` with a range, over a table (`transactions`) whose own
24-hour TTL already covers every window that can be asked about.

**The anchor at the relay is when the starter engine's node was first seen there**, not the grid's
creation timestamp. The relay already holds the first, needs a new wire value to learn the second,
and the first is anyway the semantically right one: it is when the thing being metered arrived. The
two differ by the seconds between the grid being created and its engine joining, which is nothing at
a five-hour granularity — and the clock in D-f, which lives where `network.created_at` is
authoritative, uses that instead. Two timestamps, each read where it is already true, rather than
one carried across a boundary to be re-derived on the far side.

⚠️ The cost of that choice: a node row recreated at the relay moves the anchor and starts a fresh
window. It is bounded — D-f retires the engine on the control plane's own clock regardless — and it
is not member-reachable, because no member can rejoin a grid-run kind.

### D-e — Exhaustion excludes the starter engine from selection; it refuses only when nothing else can serve

The check runs **after** provider selection, not at the door. A request that `auto` would route to a
member's engine never asks about the allowance at all. When the allowance is spent, the starter
engine is dropped from the candidate set exactly the way `X-Allow-Self-Provider: false` already drops
a self-provider (`relay._select_provider`), and the refusal is what falls out when nothing else is
left.

Refusing at the door would refuse a grid that has its own hardware, which is precisely the grid the
operator is paying nothing for.

The refusal carries a machine-readable `code` beside the sentence, for the reason `web_search`'s
`ALLOWANCE_CODE` carries one: it shares its status with "the engine is busy", and the two want
opposite actions — one lifts in seconds, the other in hours. It also carries the window's absolute
reset time, not only a duration, because a popup can sit open for minutes.

### D-f — The starter engine retires 24 hours after the grid is created, and the control plane owns the clock

A periodic sweep in the control plane stops every starter engine whose grid is older than 24 hours.
Not a timer armed at creation: a timer dies with the process that holds it, and a control-plane
restart would leave engines running forever with nothing to say so — fail-open, silent, on the
spending path. A sweep re-derives the answer from `network.created_at` every pass, so a restart costs
nothing and a failed stop is retried by the next pass rather than lost. **Idempotence and retry are
acceptance criteria, not implementation detail.**

The stop must also make the engine leave the grid, not merely die. A stopped process leaves its node
row at the relay until the heartbeat ages out, and `auto` can still pick it in that gap and fail the
request. `stop_first_provider` already runs `grid --remote leave` before removing the home on the
delete path, for a neighbouring reason; this is the second caller of the same sequence.

Retirement is one-way. A grid whose member later removes their own engine does not get the starter
engine back — that is the product's answer to "why would I ever contribute hardware".

⚠️ Retiring on a fixed 24 hours rather than on inactivity is deliberate and it is the expensive
direction for the *member*: a grid in active daily use loses its engine at the same hour as a grid
nobody opened. The alternative — extend while in use — makes the operator's cost per grid unbounded
again, which is the thing this ADR exists to stop.

### D-g — The relay answers the meter; the app derives the clock

The relay serves one new read for the allowance (`used`, `ceiling`, `reset_at`, and the request-count
pair). It is a **new route**, so a relay that does not have it answers a bare 404 and the app knows
immediately. Adding the keys to an existing read instead would make "this relay is too old" and
"you have allowance left" indistinguishable, which is the failure this repo has already paid for
more than once.

The relay is deliberately **not** told about the 24-hour clock. After retirement the starter engine's
node is gone, so the relay could not distinguish "retired" from "never had one" without being handed
a lifetime it otherwise has no use for. The app can already tell: it holds the grid's creation time
and its machine list, and *older than 24 hours with no machines* is the whole of the derivation.

### D-h — The display may fail open; the enforcement may not

A failed read of the allowance means the app shows no popup and the person keeps chatting — the
relay is still refusing at the right point, so nothing was spent that should not have been. The
inverse — blocking a person because a `GET` failed — protects no money and breaks a working grid.

### D-i — `auto` ranks grid-run models last, through a key that is a constant for everyone else

A new leading sort key in `order_candidates`, ahead of `-free_capacity`, worth `0` for every
candidate and `1` for a model served only by a grid-run kind. A pool containing no such model
therefore orders byte-for-byte as it does today, which is the property that keeps every existing
routing test and the routing ledger meaningful.

It ranks last rather than being filtered out: when a member's engine is busy, `auto`'s free-first
walk still reaches the starter engine instead of making the person wait, and that request costs money
and is counted. Floor, not default.

⚠️ Two things this does not do, both of which will look like bugs to somebody:
`order_candidates` is also what cuts the Advisor's shortlist (`[:ADVISOR_CANDIDATES_MAX]`, 50), so on
a grid with more than fifty candidate models the grid-run ones stop being *considered* rather than
merely ranked — an effect stronger than the word "prefer" suggests. And a member who picks a
grid-run model **by name** in the picker still reaches it; ordering has nothing to say about a
request that named its model.

### D-j — The nudge is per member; the ceiling is per grid

The prompt to contribute an engine or invite people appears on a member's own fifth turn, once. The
ceiling stays per grid (D-a). Two counters over one table with two different `WHERE` clauses, and
they are keyed differently on purpose: a ceiling is about the operator's money, an invitation is
about one person's experience of the product. Keyed on the grid, the nudge would greet every invited
member with a request for help on their very first message.

### D-k — A request already handed to the starter engine counts, whatever happens next

The count is of transaction rows assigned to the starter engine, ignoring `status`. An assignment
means the vendor was called, so the money is gone whether the stream completed, timed out, or the
client hung up. Only a request refused before assignment costs nothing and is not counted.

### D-l — The passthrough records what it spent, or every number in this ADR is permanently a guess

Each forwarded call logs the model and the `usage` block the vendor returned. No identity, no
`network_id`, no new wire value — an aggregate, because the question it has to answer is *what does
this feature cost per day*, which is the question that sets every ceiling below.

Without it the operator's only artefact is an undifferentiated vendor invoice, and there is no path
from "the ceiling is 50" to "the ceiling should be 30", ever. Per-grid attribution would require an
identity on a wire that deliberately has none, and is not proposed.

### D-m — The numbers are starting values read from the environment

Fifth turn for the nudge; **50 turns and 1,000 requests per five-hour window**; twenty-four hours to
retirement. All read at call time from the environment, on the model of `web_search.daily_allowance`,
so the number an operator most wants to move is not the one that needs a release to move.

They are chosen, not measured — and D-l is what will eventually replace them with measurements.
50 turns per five hours is about ten an hour, and with retirement it caps a grid's entire life at
roughly 240 turns.

## Consequences

- **Creating a second grid buys a second allowance.** There is no cross-grid ceiling per account; the
  relay has no cross-grid view and adding one is a control-plane feature of its own. What bounds it
  instead is that each grid only spends for its first 24 hours, and that unbounded grid creation
  already fails louder elsewhere — the dev VM exhausts memory at roughly sixteen grids.
- **Non-app clients get a request-counted tier, not a turn-counted one.** An agent CLI pointed at a
  grid sends no `X-Request-Id`, so by D-b every call is its own turn and the ceiling arrives inside
  the first agent turn. This tier is for chat in the app; that is a product decision, and it is
  visible here rather than discovered later.
- **A grid can go quiet at hour 24 with nothing wrong with it.** Everything the member sees at that
  point comes from the app deriving retirement (D-g). If the app does not do that work, the grid
  simply answers *no providers available* — which is exactly the state the starter engine was built
  to remove, arriving a day later with no explanation.
- **The vocabulary and the code disagree on purpose.** Product surfaces, glossary and this ADR say
  **starter engine**; the shipped modules keep saying `first_provider`. Renaming live code is a
  separate change with its own risk, and mixing it into this one would bury the review.

## Rollout order

- **Relay before app.** The app's allowance read is a new route; without it the app shows nothing and
  the grid still works (D-h).
- **No order for the recognition rule.** `meta.engine` already arrives from the CLI in production
  (D-c).
- **No order for the sweep.** It is control-plane-internal and touches no wire value (D-f).
- **The passthrough's usage log is independent** and should land first, because it is the only thing
  that can tell anyone whether D-m's numbers were right.
