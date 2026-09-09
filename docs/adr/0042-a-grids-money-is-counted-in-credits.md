---
status: proposed
---

# A grid's money is counted in credits, and the two places a dollar becomes one are named

A grid can already charge for inference. The rates are a member's own, the deduction is atomic and
idempotent, and the gate that refuses an empty wallet works. What none of it has is a **unit anybody
can use**, a **name for the seam where real money meets it**, or a **switch an operator can throw
without guessing**.

The wallet is denominated in dollars, and one request costs a fraction of a cent. The number a
member would be shown is `0.0043` — not a quantity anyone budgets against, watches go down, or
recognises as the thing they bought. Meanwhile the app's own copy for the refusal reads *"You're out
of credit on this grid — top up your balance"*. The product has been speaking credits while every
wire and every column said dollars; this decision makes the wire agree with the copy rather than the
other way round.

Six facts decided the shape, and most of them point away from the obvious implementation.

- **A market reference price and a member's own price share one sort key.** grid-src's
  `db.GridReferencePricingRow.price_score` says so outright — *"Same scalar and same weighting as
  `grid_chat_pricing.price_score`, so the two are comparable in one sort key"* — and the reference
  table is crawled from a vendor in dollars per million tokens. Re-denominate one side alone and
  engine selection breaks, silently, in a direction no money test can see.
- **A renamed wire field refuses; a re-denominated one does not.** Measured on the control plane's
  own schema library (pydantic 2.13.5): a handler requiring `cost_credits`, fed an old relay's
  `{request_id, cost_usd}`, raises `ValidationError: cost_credits Field required`. The extra old
  field is ignored — it is the *missing required* one that refuses. Keep the name and change the
  unit and nothing anywhere notices being wrong by a thousand.
- **The usage report throws its answer away, and its own sibling does not.** `relay._report_usage`
  does `await client.post(...)` and discards the result; `httpx` does not raise on 4xx or 5xx.
  Twenty lines above it, `_fetch_authoritative_balance` checks `r.status_code < 400`. The asymmetry
  is the whole silent-revenue-loss hole, and closing it is one `if`.
- **The control plane has no migration framework.** `grid_networks/db.py::init_schema` replays a
  fixed statement tuple on every startup, in one transaction. Every schema change has to be
  re-runnable, and PostgreSQL has no `RENAME COLUMN IF EXISTS`.
- **Two wallets exist in two units, and the bridge between them still compiles.**
  `grid_auth.apply_sync_snapshot` still reads `snapshot["accounts"]` and writes the control plane's
  *dollar* balance into `AccountRow.credit_balance`, a column the media path spends at 100 credits
  per dollar. The loop never runs today only because the control plane stopped sending the key and
  says so in its own source. It is one restored wire field from a hundred-fold collision.
- **Nobody can take money out.** `payout|withdraw|cash.?out|payable` matches nothing outside
  unrelated prose in any of the three repositories. An engine's earnings accumulate and can only
  ever be spent back inside the grid.

## Decision

### D-a — A credit is a thousandth of a dollar, and the ratio has no runtime writer

`CREDITS_PER_USD = 1000`. One credit is $0.001, so an ordinary request costs single-digit to
low-double-digit credits — a whole number a member can read, compare and budget with.

*Why a non-currency unit at all.* The product does not present a currency balance to end users, and
an inference request routinely costs well under a cent. A wallet that reads `$0.0043` is not a
wallet: there is no meaningful digit in it, nothing to watch move, and no way to tell an expensive
question from a cheap one.

*Why a constant and not a setting.* This is deliberately the opposite of the platform cut, which is
**gaining** a runtime writer in D-j. A ratio that can change while the system is running is a number
that makes two screens disagree with no ledger row to explain the difference: rows written before
the change and rows written after are in different units, and nothing in either record says which.
Changing it is a release, on purpose.

### D-b — The ratio applies at exactly two boundaries, and inside the report function rather than at its call site

| boundary | direction | repository |
|---|---|---|
| `relay._report_usage` — the cost settlement computed, leaving the grid as `cost_credits` | dollars → credits | grid-src |
| `store.apply_topup_credit` — dollars actually paid, becoming wallet credit | dollars → credits | grid-apis |

These are not accidental duplication. They are the only two places in the product where real
currency genuinely meets product credit; everything between them is credits end to end.

⚠️ **The multiplication lives inside `_report_usage`, not at the settle call site in
`provider_response`.** `_report_usage` is the single point where a cost leaves the grid *and* the
one function an existing test already drives directly — `test_billing_settle.py` patches
`httpx.AsyncClient` and calls it. Put the multiplication at the call site and no function-seam test
can observe it; only a much heavier route test could, in a suite that already carries
order-sensitive failures. The seam decides where the arithmetic goes.

⚠️ **Settlement arithmetic stays in dollars and the ratio is applied after it**, so every existing
case asserting what `settle` *computed* stays green **unchanged**. Needing to edit one of those is
the signal that the conversion went in the wrong place, and it is worth more than a comment.

⚠️ **The report-payload case is the deliberate exception, and reading the rule without it would
block this change.** `test_billing_settle.py` drives both: it tests settle's arithmetic *and* it
drives `_report_usage` behind a faked client and asserts `cost_usd == 0.5` on the captured body.
That one assertion is precisely what D-c renames and this decision re-scales — it becomes
`cost_credits == 500`, and it is the test that proves the ratio is applied **once**, on the way out.
Taken as *no case in that file may change*, the rule would push the multiplication back to the call
site, where nothing can observe it at all.

#### Engine prices, price scores, reference pricing and engine selection stay in dollars per million tokens

`grid price set --input 3.0` keeps meaning $3.00 per million tokens — the unit every model vendor
publishes, so a member setting a price copies a figure rather than converting one. The public CLI
therefore gains **no new command** and its price command is untouched.

This is the decision most likely to be "finished" by a later reader, and the reason not to is not a
matter of taste:

`grid_reference_pricing.price_score` is crawled in dollars per million tokens, and
`model_catalog.resolve_price_score` falls back to it for any engine that never stated a price of
its own. grid-src's schema says what that requires — *"Same scalar and same weighting as
`grid_chat_pricing.price_score`, so the two are comparable in one sort key"*. Re-denominate a
member's own price into credits while the reference table stays in dollars and **every engine that
stated a price sorts a thousand times more expensive than every engine that did not**, so the
least-configured engine wins every request.

⚠️ **That failure lands in routing, not in billing.** Charges stay correct, ledgers balance, and
every money test in both repositories stays green while traffic quietly stops reaching the engines
that priced themselves. Nothing in either suite is positioned to see it. This paragraph exists so
that the next person to notice two units in one system has the reason in front of them before they
unify them.

### D-c — Every wire field whose unit changes changes its name

| route | old field | new field |
|---|---|---|
| `POST /v1/grid/internal/usage` | `cost_usd` | `cost_credits` |
| `GET /v1/grid/internal/accounts/{sub}/balance` | `balance` | `balance_credits` |
| `GET /v1/grid/internal/min-balance` | `min_balance_usd` | `min_balance_credits` |
| `GET /v1/grid/account` | `balance`, `total_spent` | `balance_credits`, `total_spent_credits` |
| both ledger reads | `amount_usd` | `amount_credits` |

A unit change under a stable field name is exactly the silent thousand-fold failure this ADR exists
to prevent, and this repository's own lockstep rule already names the asymmetry: **a new key on an
existing route degrades silently, and a rename is the loud one.** Here the rename is load-bearing in
both directions of a partial rollout, for two different reasons:

- **The report refuses loudly.** An old relay's body against a handler requiring `cost_credits` is a
  validation failure — measured, not assumed. ⚠️ It is loud *only because D-h taught the relay to
  read the status*. Before that change the same 422 was discarded and the grid served free in
  silence, which is the defect this feature exists to close and would have been reintroduced by its
  own rollout.
- **The reads refuse service — on the rename, and only on the rename.** The relay reads each of
  these with a default, so a field it cannot find *on a 200* yields that default. Zero is below
  every threshold, so an out-of-step relay **refuses every request** rather than serving free. Fail
  closed for a renamed key, deliberately.

⚠️ **Amended 2026-09-08 — "the worst case is a grid that stops, not a grid that gives itself away"
was wrong as a general claim about the gate, and is corrected here.** It holds for the renamed key,
because a renamed key still arrives on a `200` and defaults to zero. It does **not** hold when the
control plane *refuses or is unreachable*: `relay._fetch_authoritative_balance` returns `None` on
four paths — no configured URL, no subject, any exception, and by fall-through on
**`status_code >= 400`** — and `relay.py:5476-5478` then falls back to the legacy media wallet,
which `auth.py:107` seeds at `100.0` "welcome credits" against a `0.5` floor. A grid whose control
plane is down therefore **serves free**, the exact outcome the original sentence claimed could not
happen.

⚠️ After D-a and D-c land, that same path **inverts**: the floor becomes the credit figure (500)
while the media wallet still holds `100.0`, so `100 < 500` refuses *everyone*. One code path, two
opposite wrong answers, on either side of this ADR's own change.

⚠️ **Amended 2026-09-09 — "refuses *everyone*" is wrong, and the correction is the subject of D-f's
amendment below.** It holds only for a member whose media wallet still holds the `100.0` signup seed.
`payments.py` adds to that wallet on a live path, so a member holding `≥ 500` media credits was
**served free** on the same outage, from the same line. The fault was never a direction: it was
**data-dependence**. The fallback is deleted as of issue 10 — see D-f.

⚠️ **The relay's fallback for the minimum must be the credit figure (500), never the old dollar one
(0.5).** Carrying `0.5` across leaves the gate at a thousandth of its intended height, and nothing
anywhere reports it. It is the single most likely copy-paste in this change, and it is the reason
the minimum has a register row of its own.

⚠️ **The relay's locally cached threshold moves to a NEW column rather than being reinterpreted.** A
grid that already cached `0.5` dollars would otherwise enforce `0.5` *credits* — the same
thousand-fold gap, on a grid nobody redeployed. A new column reads as absent and re-fetches.

### D-d — Rollout: the control plane before the relay

Both directions refuse — one loudly, one by refusing service — so the order is a safety convenience
rather than a correctness requirement. It is this way for two reasons: the wallet's unit and its
ledger's unit have to change together and they live in one repository, and grid-apis is the *server*
on every one of these routes, which is the shape every other control-plane ordering in the register
already takes.

### D-e — The wallet's schema change is destructive and one-way

Per renamed column, two statements, both re-runnable:

```sql
ALTER TABLE grid_account ADD COLUMN IF NOT EXISTS balance_credits DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE grid_account DROP COLUMN IF EXISTS balance;
```

`init_schema` replays the whole tuple on every startup, and PostgreSQL has no bare
`RENAME COLUMN IF EXISTS` — the file's one rename precedent,
`ALTER TABLE IF EXISTS grid_member_invite RENAME TO grid_pending_charge`, works only because
`IF EXISTS` there applies to a *table*. Add-then-drop is the plainest re-runnable spelling of the
decision below.

⚠️ **Amended 2026-09-08, during issue 04: it is NOT the only spelling available, and this ADR said it
was.** A `DO $$ … END $$` block testing `information_schema.columns` — the shape `db.py` already uses
for `ck_grid_networks_os_community_access_os` — can `RENAME COLUMN` and then multiply by the ratio,
re-runnably and without losing a value. The reason not to is the decision in the next paragraph, not
the absence of a way; "there is no other way" is a reason a later reader can disprove in one grep,
and disproving it would reopen a decision that was actually taken on purpose.

Old dollar balances are **discarded**. That is authorised because self-serve payment has never
shipped, so no wallet holds money anyone *paid* for.

⚠️ **Amended 2026-09-08: that sentence is narrower than what the change discards.** Two paths that
move balance HAVE shipped — the operator's own top-up (`POST /admin/accounts/{sub}/topup`) and an
engine's revenue share (the ledger's `earning` rows) — and both are zeroed too. With a 500-credit
gate, an account that held such a balance is refused on a billing-on grid until somebody credits it
again. The decision stands (no grid has billing on, and the fleet's wallets are expected to be
empty), but it is a decision about *real rows*, so the deployment note carries the count-before-you-
deploy query rather than the assurance.

⚠️ **There is no undo.** Rolling back to the previous release re-runs the old
`ADD COLUMN IF NOT EXISTS balance` and every wallet reads empty. This belongs in the **deployment
note**, not only here — a rollback is the one operation whose operator will not be reading an ADR.

### D-f — The legacy per-grid wallet stays for media, fenced so it cannot be mistaken for this one

`AccountRow.credit_balance` becomes **`media_credit_balance`**. It keeps serving the media escrow
path, which is out of scope; the rename is what stops it being read as the wallet this ADR
introduces.

Three things are **deleted**, and they go together because together they are one restored wire field
away from writing control-plane dollars into a credits column:

1. **The `snapshot["accounts"]` loop** in `grid_auth.apply_sync_snapshot`. It assigns the control
   plane's balance verbatim into that column. Today that is a **dollar** figure landing in a column
   the media path spends at a hundred credits to the dollar; after D-a it is a **credit** figure on
   a different scale again. Either way the number means something else the moment it is written, and
   nothing compares the two. It never iterates today only because the control plane's snapshot
   builder stopped sending the key and records why in its own source — latent, not live, and one
   restored wire field from firing.
2. **`grid_auth.resolve_local_rate` and `GridLocalPricingRow`** — the read-only price replica and its
   resolver, with **zero callers**, superseded by `model_catalog.resolve_chat_rate`.
3. **The test covering both.** It hand-builds a snapshot and asserts the master writes both
   replicas; it never speaks to the control plane's actual builder, so it is green over a seam that
   no longer exists and would stay green whatever anyone did to the wire.

⚠️ The test is deleted **with** the code rather than left behind. A suite that keeps proving a
contract nothing speaks is worse than no test: it spends review attention and reports confidence
about a seam that is gone.

⚠️ **Amended 2026-09-08 — the fence is incomplete, and this ADR did not say so.** The rename stops
the *snapshot writer* (deletion 1 above) and D-g pins the *escrow caller*, but a third reader was
never enumerated: the inference path's own balance gate. `relay.py:5477` calls `credits.get_balance`,
which after the rename returns `account.media_credit_balance` (`credits.py:29`) — so the media
wallet is still consulted **on the inference path**, as the fallback taken whenever the authoritative
read yields `None`. Renaming the column did not stop that; it renamed the column being read.

Closing it changes money behaviour and belongs to its own ticket rather than to this prefactor. The
options are to **drop the fallback** — refuse when the authoritative balance is unavailable, making
the gate fail closed in fact rather than in prose — or to **keep it and say so in D-g**, recording
that the inference path has a second, media-funded door. What must not stand is the present
position, in which this ADR asserts a fence the code does not have.

⚠️ **Amended 2026-09-09 (issue 10) — the fence is now closed, and the fault it left open was
DATA-DEPENDENCE rather than a direction.** The first option above is taken: the fallback is deleted,
and **a balance that cannot be established refuses the request**. Four things travel with that.

**What the open fence actually cost.** Both this decision and D-c's amendment described the hole as
having one direction at a time — first "serves free", then, after D-a, "refuses everyone". Neither
is true, because the number being read belongs to a different product. `auth.py:107` seeds the media
wallet at `100.0` and `payments.py:24,79` adds more, on a path `server.py:31` imports, so on one
unreachable control plane:

| the member's media wallet | the gate did |
|---|---|
| `100.0` (the signup seed) | refuse |
| `≥ 500` (bought media credits) | **serve free** |

One line, opposite outcomes, selected by a figure no part of the inference path owns or can see.
"Serves itself away" was never closed by D-a and D-c; it was **narrowed** to the members who happen
to have bought media credit — which is the worst shape for the failure to take, because it is
invisible on every grid where nobody has.

**The refusal is a 503 carrying a sentence, and deliberately not a 402.** Payment is not what is
required when a grid cannot reach its own authority on money: the member's wallet may be perfectly
funded, and D-l's `Insufficient balance` sentence would send them to top it up — somewhere that will
fix nothing. `503` is simply the true statement. It is also free: the public CLI, which is the client
here, prints any body at or above 400 verbatim and exits 1 (`cli/remote_request.py:103-105`), so the
status buys no client behaviour and the **sentence is the whole contract**. It is pinned as a
sentence and not as a refusal `code` — D-l's count of three parsed codes is deliberate, and this is
not the fourth.

**Two refusals, because there are two remedies.** *This grid was never configured to reach a control
plane* is fixed on the box in a minute; *the control plane could not be reached* is not fixed there
at all. `_fetch_authoritative_balance` answers `None` to both — and to a `>= 400`, which leaves it by
a bare fall-through rather than a `return`, the path a reader misses — so the gate asks the
configuration question **before** the read, and the `None` that comes back can then mean only one
thing. The refused-read and dead-socket cases stay one refusal: an operator can act on neither from
here.

⚠️ **Unchanged, and the reason the rename is safe: a `200` without `balance_credits` still reads
`0.0`.** That is a number, not a failure, so it falls through to the min-balance gate and its 402
exactly as D-c specifies. Turning it into this decision's 503 would tell an operator their control
plane is down while it answers perfectly.

**Rejected: caching the balance locally.** Recorded here so it is not proposed again during the
first outage. It fails on the architecture, not on cost:

- the wallet is **global per person** — `grid_account`'s primary key is `google_sub`, and the table
  has no network column — while every grid is a separate master with its own database. A per-grid
  cache is therefore *N stale copies of one wallet that cannot see each other*, and the over-spend an
  outage permits stops being one request and becomes one per grid the member is on;
- the only store that could hold a **shared** copy is the control plane, which is the thing that is
  down;
- the relay's local state table (`grid_local_network_state`) already caches `min_balance_credits` and
  deliberately holds **no** balance column, for the reason written at grid-apis'
  `store.apply_usage_charge`: the master keeps no balance, so concurrent requests across grids cannot
  desync a local copy. A cache overturns that, and the reason it was written has not changed.

What a refusal costs is **availability during a control-plane outage**. Measured 2026-09-09: one grid
on prod has billing on (121 of 122 are `local_free`), so today that is close to nothing. If it ever
stops being nothing, the answer is a control plane that stays up or a read replica — not a cache at
the edge.

### D-g — `_billing_on()` is the sole authority on the inference path, and always was

The media escrow reads an environment variable directly rather than the billing predicate, and that
reads at first glance like a second switch for one decision. It is not: `hold_escrow_within`'s only
caller is `_create_media_transaction`. These are **one switch each on two different paths**.

No behaviour changes. What is added is a pin on the **caller count**, so that a future inference
caller cannot silently inherit the media switch. A second door onto a refusal inherits none of the
first door's guards, and a count is what a test can assert where there is no behaviour change to
assert instead.

### D-h — The usage report stops being fire-and-forget

Three layers, in order:

1. **Check the status.** Anything at or above 400 is a failure. This is the single most valuable
   line in the feature: without it D-c's loud direction is not loud at all.
2. **Bounded retry with backoff, on server errors and timeouts only — never on a client error.** A
   4xx is a body the control plane refused, and resending a refused body is a loop that ends when
   somebody notices the log.
3. **An outbox row on final failure, logged at error level.** `request_id` is already the control
   plane's idempotency key, so replay is safe *by construction*: a replayed report that already
   landed reports itself as not applied and moves nothing.

⚠️ **The member is never blocked.** Their answer is delivered before any of this runs. That property
is deliberate and is preserved — billing must never be able to slow down or fail a request.

⚠️ **A new module inside the vendored server package must be registered in the application loader's
ordered module list, leaf-first.** Omit it and every unit test in that repository stays green while
the live service dies at startup. Two regression tests already guard this, and they must actually be
run.

### D-i — The engine's share is applied in the same transaction as the member's charge

Today the charge and the share open two separate pooled connections — two transactions — and the
second sits inside a bare `except Exception` with a single `warning`. When it fails the member is
charged, the engine is not paid, and nothing exists to reconcile from.

One store operation, one connection, one transaction: the engine is paid **if and only if** the
member is charged. If it fails, both roll back and D-h's outbox replays the whole report, safely,
because both halves are idempotent on keys derived from the same `request_id`.

⚠️ **An engine nobody can be paid for is an ordinary branch, not a failure.** An unresolvable engine
account, or one that resolves to the member's own, means *nobody to pay* — the charge must still
stand. Only a genuine write error rolls anything back. Reading these as failures would refund
members for requests they made.

⚠️ **The cut is read before the transaction opens, not inside it.** That read takes its own pooled
connection; leaving it inside deadlocks against the pool under load.

### D-j — The platform's cut gets a real writer, and it refuses a value meaning "never pay the engine"

A read and a write endpoint on the same operator gate as their administrative neighbours.

- **The write validates the range and refuses outside it.** A cut of `1.0` means the engine is never
  paid, with nothing anywhere to say so. Today the setter takes a bare string and the reader falls
  back to the default on anything unparseable — so a typo currently reads as *unset*, which is not
  the same thing and must stop being spelled the same way.
- **The read distinguishes "never set, default in force" from "set to exactly the default value."**
  Through the reader alone those two states are identical, and only one of them means somebody
  decided.
- **No CLI and no route in this repository.** The cut is an operator's knob, not a grid member's.

### D-k — Billing stays off by default, a grid's own owner still cannot switch it on, and switching it on where it cannot work is refused

Two things are unchanged and were already right: the default is off, and the report-key gate keeps a
grid's own creator from flipping it — charging members is a platform decision.

What is added is that turning it **on** now checks two preconditions, and names the one that failed:

1. **The grid is of the one type that can be billed.** Otherwise the relay's own predicate answers
   false, nobody is ever charged, and the route answers `200` while the operator believes billing is
   live.
2. **This control plane has its operator key configured.** Otherwise the usage report *and* the
   balance read both answer *service unavailable*, the relay's gate falls back to zero, and **every
   request on that grid is refused for insufficient balance**.

Two opposite disasters — *charges nobody* and *blocks everybody* — that today share one success
response. Both conditions are knowable where the switch is thrown, which is why the check lives
there.

⚠️ **The control plane cannot see the relay's own report URL**; that is the relay's environment.
Condition 2 is the half that *is* observable from here, and it covers the same bricking symptom.

⚠️ **The honest precondition for ever changing the default is a withdrawal path, and there is
none.** Verified across all three repositories. Everything in this ADR can ship and be correct, and
a member's engine will earn credits it has no way to take out. Switching billing on before that
exists promises people money they cannot have.

### D-l — Five values enter the cross-repo lockstep register, and one of them has no half in this repository

The register contains **zero** billing rows today while four live cross-repository contracts run
through this area, and a fifth was not known to exist. Each row carries its own failure mode,
because they genuinely differ:

| value | absent ⇒ | order |
|---|---|---|
| the usage-report route and `cost_credits` | a loud validation refusal — ⚠️ **but only because D-h made the relay read the status**; before that it was silent and free | control plane first |
| the balance route and `balance_credits` | the relay's default of zero ⇒ every request refused. Fail closed for the **rename** — ⚠️ but see D-c's amendment: a control plane that *refuses* or is unreachable yields `None`, not zero, which since issue 10 is a **503 of its own** rather than a fall-through to the media wallet (D-f, amended 2026-09-09) | control plane first |
| the minimum route and `min_balance_credits` | ⚠️ the relay's fallback must be the credit figure, not the old dollar one | control plane first |
| `CREDITS_PER_USD` itself | ⚠️ **nothing degrades — the two sides simply disagree.** No missing route, no validation failure, no postcondition to check. The only value here whose failure is pure arithmetic | no order helps |
| the refusal sentence the app matches | grid-src ↔ the app, **no half in this repository**; a reword makes the app render the relay's raw sentence, ⚠️ which prints the viewer's exact balance and the grid's threshold into their chat window | no order helps |

⚠️ **The last row stays prose, and that is a decision rather than an omission.** The obvious
"improvement" is to give the refusal a machine-readable code the app can key on. This repository
deliberately keeps the count of *parsed* refusal codes at three, on the grounds that every relay
message already names the way forward and a fourth reader is a fourth thing a reworded relay can
break. What a test pins here is the **phrase**, and the phrase is what must survive this change
byte for byte — only its numbers become credits.

⚠️ **A green continuous-integration run proves nothing about any of these.** Every cross-repository
assertion in this repository skips unless the sibling worktree sits beside it, which it never does
in CI.

## Consequences

- **Two units live in one system on purpose, and one of them is invisible to every money test.**
  Prices are dollars per million tokens; wallets, ledgers and refusals are credits. D-b is the whole
  defence, and the failure mode it prevents shows up in *routing*.
- **Old wallet balances are gone at deploy and cannot come back** (D-e). Acceptable only while no
  wallet holds paid-for money, which is a property of today, not a property of the design.
- **A partial rollout stops a grid rather than giving it away** (D-c). That is the intended
  trade — an operator would rather explain an outage than a month of free inference — but it does
  mean the deploy order is worth following even though neither direction is silently wrong.
- **The platform cut becomes changeable at runtime while the ratio does not** (D-a, D-j). The two
  numbers look alike and are governed opposite ways: a cut applies to future requests and each one
  records what it used; a ratio would silently re-denominate the meaning of rows already written.
- **Billing remains off everywhere.** Nothing here switches a grid on. What changes is that turning
  one on is now a decision an operator can make with the failure modes visible (D-k), instead of a
  coin flip between two disasters that share a success response.
- **Engine earnings still cannot be withdrawn.** This ADR makes them countable and honest; it does
  not make them collectable, and that gap is the reason the default stays where it is.

## Rollout order

- **Control plane before the relay** for the report, the balance read and the minimum read (D-c,
  D-d). Both directions refuse, so this is a convenience; it is the wallet and its ledger having to
  move together that fixes which side goes first.
- ⚠️ **The relay reading the report's status ships BEFORE the field rename** (D-h before D-c). The
  rename's loud direction is loud only because the relay checks. Landing the rename first swallows
  every mismatch in the skew window in silence — the exact defect this work exists to close,
  reintroduced by its own rollout.
- **No order helps for the ratio** (D-a). Nothing 404s and nothing refuses; the two sides simply
  disagree, and charges and top-ups run on two scales until somebody reconciles by hand.
- **No order helps for the refusal sentence** (D-l). Both halves are grid-src and the app, with
  nothing in this repository in the path; a reword takes effect the moment the relay deploys and no
  app release sequence brings the matched phrase back.
- **No order for the wallet fencing, the one transaction, the fee's operator surface, the readiness
  gate, or the outbox itself** (D-f, D-g, D-i, D-j, D-k, and D-h's second and third layers). Each is
  internal to one repository and adds no wire value in either direction. ⚠️ **D-h's first layer —
  reading the report's status — is the exception directly above**, and it is the only hard ordering
  constraint in this ADR; everything else here is a convenience.
