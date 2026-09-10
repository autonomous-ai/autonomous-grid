---
status: proposed
---

# Which grids are charged is a setting the operator writes, not a constant in three repositories

A grid charges for inference when two things are true at once: it is **of a type that may ever be
charged**, and its own per-grid switch is **on**. The second half already has an operator surface —
a route, a precondition gate, a refusal that names which condition failed. The first half is a
string literal, `permissioned-providers`, compared for equality in the control plane and compared
again in the relay, with no path between the two copies and no way to change either without a
release of both.

That was the right shape while exactly one type could be charged. It stops being the right shape the
moment somebody wants to charge a second one — because the change is a code change in two
repositories, deployed in an order nobody wrote down, to move a decision that is not an engineering
decision at all.

This decision makes the billable set a **setting the control plane owns**, gives the relay the
**conclusion for its own grid** rather than the policy, and adds the one bulk operation the
conjunction actually needs. It changes nothing about who may throw the switch, and nothing about the
default being off.

Six facts decided the shape, and most of them point away from the obvious implementation.

- **Widening the set changes nothing on its own.** Every grid is born free:
  `_billing_mode_a_new_grid_is_born_with` returns the off value unless the environment asks for
  charging *and* the preconditions pass, and its own source records that production has written
  `GRID_BILLING_MODE=local_free` explicitly since 2026-07-01. So the policy this decision makes
  writable moves only the left half of a conjunction, while nothing anywhere moves the right half
  for more than one grid at a time. A setting without a bulk operation is a switch wired to nothing.
- **The two credentials on the money surface are not the same value, and nothing makes them so.**
  `_require_report_key` reads `GRID_USAGE_REPORT_KEY`; `_require_system_admin` reads
  `GRID_ADMIN_API_KEY`. The shared precondition module's own docstring records that `.env.example`
  documents the two as the same value with nothing enforcing it. A bulk route on the wrong one hands
  the power to switch billing on to a credential the single-grid route refuses.
- **The single-grid switch reaches only managed grids.** `_require_report_key` answers 404 when
  `get_managed_port` is `None`, so a self-hosted grid's billing mode is unreachable by any route and
  stays at whatever it was born with. A bulk route phrased as "every grid of this type" would reach
  grids the switch it multiplies cannot.
- **The relay reads its per-network billing row in exactly one place.** `_read_network_billing_row`
  is the sole reader of `grid_local_network_state.billing_mode`, `_billing_state` is its sole
  consumer, and all three call sites on the inference path go through it. A conjunct added there
  really does govern the whole path — and equally, a conjunct wrong there is wrong everywhere at
  once, which is why the relay must receive a verdict and not a policy to re-evaluate.
- **The sync daemon pulls every 60 seconds** (`interval_seconds=60`, backoff capped at 300). A value
  written in the control plane reaches every live relay inside a minute with no push at all. That is
  what makes editing the set a better off switch than any bulk write could be.
- **A stored value that cannot be parsed is spelled exactly like one nobody ever wrote.** The
  platform-fee request model's own docstring records the trap and the only cure: refuse it at the
  write, because the reader's fallback makes a misspelling indistinguishable from an unset key. The
  same reader shape is about to hold a set instead of a number, where the mistake is worse — a
  misparsed set does not merely restore a default rate, it silently restores a different *policy*.

## Decision

### D-a — The billable set is a control-plane setting, its default is never empty, and an explicit empty set is allowed

`grid_config['billable_network_types']` holds a JSON array of network-type literals. The predicate
both doors onto charging already share reads it instead of comparing against one constant.

*Written* through a system-admin route that **refuses any element outside the known network types**
with a 422. Refusing at the write is what makes the stored value trustworthy, for the reason the
platform fee already records: the reader falls back, so a value that cannot be used is
indistinguishable from a key nobody ever set.

*Read* with two distinct failure answers, deliberately not the same one:

| what is stored | what the reader does |
|---|---|
| unset | the default `{permissioned-providers}`, **silently** |
| stored but unparseable as a whole | the default `{permissioned-providers}`, logged at ERROR |
| parses, but holds an element the vocabulary no longer knows | **drop that element, keep the rest**, logged at WARNING |

⚠️ **Amended in implementation (2026-09-10): the unset key is silent, and is its own row.** This
decision first grouped it with the unparseable one, at ERROR. That is wrong for the only reason that
matters here — unset is the state of *every* control plane that has never touched this setting, so
an ERROR per read puts one on every sync snapshot and every grid creation, everywhere, for ever. A
warning nobody can act on is how the ones that matter stop being read, which this repository already
decided once: `_billing_mode_a_new_grid_is_born_with`'s downgrade warning is gated on the operator
having actually asked for charging, and a test says so in those words. The distinction the two
failure answers exist to draw is between *nobody wrote one* and *somebody wrote one that does not
work* — and it is drawn better by silence versus ERROR than by two identical ERRORs.

⚠️ **The unparseable fallback is the DEFAULT set and deliberately not the empty one, and that is the
direction a reviewer will want reversed.** It looks wrong: an operator who wrote `[]`, or a set
excluding `permissioned-providers`, has their narrowing undone by a row that later becomes
unreadable, which is the *charging-somebody-who-should-not-be* direction. Three things decide it the
other way. The write is the only writer and cannot produce an unparseable value, so reaching this
state needs out-of-band access to the table rather than anything on the API. It is not silent — an
ERROR names the raw value, and the read endpoint reports `configured: true` beside a set the operator
never wrote, so both surfaces disagree with what they were told. And it is the same choice D-c makes
one decision down for the same reason: on a charging gate the *accident* answer should be **today's
behaviour**, not "off", because "off" means giving the compute away and is the failure this area has
already spent two amendments closing. A corrupted row that charges the class that was always charged
is recoverable in one write; one that silently stops every grid charging is discovered in a revenue
report.

⚠️ **A blank or non-string element is a shape error, not a retired word.** `normalize_network_type`
turns `None` and `""` into `permissioned` — a real type — so dropping-and-continuing there would
silently widen the billable set to a type nobody named. It takes the unparseable row, and the write
refuses it outright.

⚠️ **One unrecognised element must not discard the whole value.** A type retired from the vocabulary
in a later release leaves rows and settings spelling it; treating that as "unparseable" would revert
an operator's policy to the default without anything saying so — the same silent-restoration failure
the write-side refusal exists to prevent, arriving through the reader instead.

*The default is never empty.* An empty set means nobody is ever charged, which is a grid serving
compute for free in silence — the exact shape this area has already spent two amendments closing.

⚠️ **An explicitly written empty array is nonetheless allowed, and is the global off switch.** The
distinction is not pedantry: an empty set *arrived at* by a missing key or a parse failure is an
accident, and an empty set *written* by an operator is a decision. The read endpoint carries a
`configured` flag for exactly this, the same way the platform fee's does — nothing stored and a
stored value that happens to equal the default are the same number and only one of them means
somebody decided.

### D-b — The relay receives the conclusion for its own grid, never the policy

The sync snapshot carries `billing_eligible: bool` — the answer for *that one network*, computed
where the policy lives. The relay stores it and reads it. It never receives the set.

Duplicating the policy into the relay would put the same list in two repositories with no import
path between them, which is the worst category in the cross-repo register: two copies of a rule,
kept level by hand, that disagree silently and answer 200 in both directions. It would also give the
relay a second thing to get wrong — the set could arrive intact and the *comparison* still be wrong,
in a repository where the type string is an environment variable rather than a database row.

⚠️ The key is computed **in the sync-snapshot route only**, not in the shared network serializer.
That serializer runs behind six endpoints, one of which lists networks visible to any signed-in
account; the eligibility answer needs a settings read, so computing it there turns a listing of N
networks into N configuration reads for a field exactly one consumer has ever wanted.

### D-c — Not-yet-synced falls back to today's predicate, not to "free"

A `NULL` column means the snapshot has not landed yet. It falls back to the predicate in force
today — the relay's own network type compared against `permissioned-providers` — and not to `false`.

*Why the seemingly-unsafe direction is the safe one here.* `NULL` is the state of **every** grid
between master start and first sync, on **every** deploy, for up to the sync interval. Answering
`false` there would stop every charging grid charging for up to a minute after every restart,
repeatedly and without a word anywhere. "Fail closed" on a charging gate means *give the compute
away*; the closed direction on this particular knob is the one that keeps today's behaviour.

It also matches the column beside it: `billing_mode` already falls back to an environment value when
unsynced, recorded in its own source as being so a toggle is not clobbered back to nothing.

⚠️ The fallback predicate is spelled **once**, as a named constant. Two copies of it drift, and a
drifted fallback is invisible — it only shows during the window when nothing else is looking.

⚠️ The price of this direction is stated rather than hidden: removing a type from the set leaves a
relay that has not yet synced charging for up to the sync interval. That is bounded, it is the same
bound the per-grid switch already carries, and it is the cost of not re-breaking every restart.

### D-d — The type policy and the per-grid switch are an AND, and that is what makes the off switch lossless

```
charge  ⟺  billing_eligible  AND  billing_mode == 'public'
```

The two are **not** merged into one knob. The type answers *may this class of grid ever be charged*;
the per-grid mode answers *is this grid charging*. Collapsing them would remove the ability to
exempt a single grid, and would make a policy edit overwrite per-grid decisions that nothing else
records.

The conjunction is also what gives the two directions different costs, and the rest of this decision
follows from it:

| | conjuncts that must move | why |
|---|---|---|
| turning charging **on** | **both** | `false ∧ true` is `false`; reaching `true` requires raising both sides |
| turning charging **off** | **either one** | `false ∧ anything` is `false`; lowering one side is sufficient |

### D-e — There is a bulk switch on, and deliberately none off

`PUT /admin/billing-mode/by-type` raises the per-grid conjunct for every grid of a type. It exists
because D-d's first row says it must: without it, adding a type to the set charges nobody, since
every grid is born free.

**There is no bulk off, and its absence is a decision rather than an omission.** Removing the type
from the set already stops charging on every grid of that type, within the sync interval, in one
write, touching no per-grid row at all.

*Why the redundant route is also the destructive one.* `billing_mode` is the **record of per-grid
decisions**, some of them individual. A bulk off overwrites all of them with the same value, and
nothing anywhere remembers which grids were on. Re-widening the set afterwards cannot restore them;
bulk-on would turn on grids nobody ever decided to charge, including ones deliberately kept off.
Editing the set leaves every row untouched, so re-adding the type resumes exactly the state each
grid's operator chose.

⚠️ **Bulk on is destructive in the same way and survives anyway, and the difference is the reason.**
It overwrites deliberately-off grids too. It is kept because no lossless alternative exists — the
right-hand conjunct has no other lever — and it is fenced accordingly (D-f, and the per-grid
precondition run below). Bulk off's destruction is *gratuitous*: a lossless, faster, single-write
alternative exists. What disqualifies a route here is not that it overwrites; it is that it
overwrites when it did not have to.

⚠️ **Two doors onto one effect means the destructive one gets used**, because its name is the one
that matches what the operator is thinking. Nobody intending to stop charging a type reasons their
way to "edit the type set". This repository has the failure already: two doors onto one membership
removal, where the copy lost the refusal that mattered.

*What editing the set cannot express*, recorded so a future need reopens this deliberately: removing
the type is a **strictly wider** action than a bulk off — it also makes the per-grid switch refuse,
and downgrades at creation. "Stop charging these existing grids but keep the type billable" has no
expression here beyond the per-grid route, one grid at a time.

The bulk route's own shape:

- **The whole call is refused before any grid is touched** when the type is not in the billable set
  (409) or this control plane has no operator key (503). Every grid would fail identically, and the
  operator's actual mistake is the setting, not any grid.
- **After that it never fails as a whole.** The shared precondition runs **per grid**; a grid that
  fails is skipped and reported, never blocking the others. The answer is a report, not a status —
  there is no partial write that ends in a 500.
- **A grid already on is skipped and reported as such.** The call is idempotent; running it twice is
  safe, which matters for an operation whose first run may be interrupted.
- **`dry_run` is a field on the same route, not a preview endpoint.** The same code path computes the
  same report and simply does not write, so the preview cannot disagree with the action. A separate
  preview route is a second implementation of the decision, and it will drift from the first.
- **The sync push is best-effort per grid and reported per grid**, exactly as the single-grid switch
  already does. Nothing depends on it: the daemon delivers within the sync interval regardless.

⚠️ The call is synchronous over N grids — N updates and N pushes. No cap is imposed, because no type
today holds a number where that matters. It is recorded as the property to revisit first if one ever
does.

### D-f — The bulk switch takes the credential of the switch it multiplies, and reaches only what that switch reaches

**Credential.** The policy route is system-admin (`X-Admin-Key`): choosing which classes of grid may
be charged is a platform decision. The bulk route is the **report key** (`X-Report-Key`), the same
credential as the single-grid switch, because it exercises exactly that power N times.

⚠️ Giving the bulk route the admin key would hand the ability to switch billing on to a credential
the single-grid route deliberately refuses. The two keys are documented as the same value and
nothing enforces it, so this is reachable, not hypothetical — and it is the classic second door onto
a refusal, where the guard lives at the old call site and the new caller inherits none of it. A test
must set the two keys to **different** values and assert the admin key is refused here.

**Scope.** Managed grids only, skipping deleted ones — the scope `_require_report_key` already
enforces. A self-hosted grid runs on its owner's own machine with its own environment; the platform
charging it is a different conversation, not a side effect of a route about a type.

### D-g — The refusal sentences render from the set, and the precondition tuple does not change shape

The billable-type precondition carries both a refusal sentence and a clause the creation door's
downgrade warning names, and **both hardcode the literal `permissioned-providers` in prose**. Left
alone they would state a policy that is no longer true — a refusal that misinforms is worse than one
that merely refuses.

Both become templates rendered with the current set. The existing template mechanism already ignores
placeholders a sentence does not use, so the other precondition changes not one character. The
precondition tuple keeps its shape and its membership, so the test that parametrizes over it — the
one making a third condition prove itself at **both** doors — keeps working unchanged.

⚠️ The set renders as a sorted, comma-joined list of the literals rather than a container's own
representation: it is what an operator has to read, and the template mechanism already carries a
warning about braces in these strings.

⚠️ **The two refusal sentences are not the only prose asserting the retired rule, and the rest is
the same defect rather than tidying.** Fourteen further sites say it, counted. **Five** are in the
control plane's operator-facing environment example — its consumer-billing section header, its
minimum-balance note, and three lines of the block describing what turning charging on actually
does; that file is what an operator reads before touching any of this, so a stale claim there is
aimed at exactly the person about to act on it. **Nine** are on the relay side: three name a type
outright, and six describe the same gate abstractly — including the auto-routing switch's docstring,
which draws a contrast against billing's network-type gate that stops being true once that gate is a
synced column.

⚠️ **So the sweep cannot be scoped by grepping the type literal**, which finds eight of the fourteen
and misses the six that matter most — the ones a reader trusts precisely because they explain a
mechanism rather than quote a value. Sweep by meaning.

⚠️ **One of the sites is a test that asserts the retired behaviour, and its failure message argues
for restoring it.** The relay's read-count suite pins that a grid which is never charged opens no
database session at all — true only while the short circuit exists — and says so in words that read
as an instruction to put the short circuit back. A test encoding an overturned rule is retired with
the rule; the property that replaces it is that such a request now reads the row once. ⚠️ Its
sibling in the same file — pinning that the *shared* reader is ungated on network type, without
which the abort-behaviour suite passes by never reaching the database — survives, with its
justification rewritten rather than its assertion deleted. Retiring both together is the easy wrong
move, and it un-guards the other suite in silence.

### D-h — One value enters the lockstep register, and its order gates an operator's action rather than a deploy

`billing_eligible` on the sync snapshot — the control plane's snapshot builder against the relay's
snapshot reader and its new column. Absent, the column stays `NULL` and the relay falls back to
today's predicate (D-c), so **both directions degrade to current behaviour** and the value itself
needs no deploy order.

`billable_network_types` does **not** enter the register. It never crosses a wire, deliberately
(D-b), and both ends of both new routes are in the control plane.

⚠️ **There is nonetheless an order, and it is of a kind not otherwise in that table: it gates an
operator's action, not a release.** Widening the set while relays still fall back to the hardcoded
predicate means the switch stops refusing, the route answers success, and **nobody is charged** —
precisely the first of the two disasters the billing-on preconditions exist to prevent, restored by
the change that made them configurable.

What makes it enforceable rather than merely written down: a managed grid's master is spawned by the
control plane, so the relay version across the grids this can touch is a deploy the same operator
controls — and self-hosted grids are outside the bulk route's scope by D-f. The rule is therefore:
**land the relay's half across the fleet before widening the set past its default.**

### D-i — Three holes are recorded rather than closed

Each is real, each is out of scope, and each is named here so that "billing by grid type" is not
read as covering it.

1. **The media path is outside all of this.** It gates on the relay's `GRID_BILLING_MODE`
   environment value, read at import, in several places, and never consults the inference path's
   billing verdict — so it **already** ignores the network-type predicate today, before anything
   here. Its prices come from a provider-declared dictionary on node registration rather than from
   the price commands, and it spends a separate wallet with an escrow. The public CLI sends an empty
   pricing dictionary, so this is a latent channel rather than charging that is running.
2. **The gate and the settlement can disagree.** A request consults the billing verdict three times
   independently, and the settlement is a *different* HTTP request from the provider. A switch
   thrown in between makes them differ. Only one direction loses money — billable at the gate,
   free at settlement, so no cost is recorded and no usage is reported; the reverse is harmless
   because the gate already zeroed the rates that settlement uses. This predates this decision, and
   the bulk switch does not make any single request worse — it makes the window wider by moving many
   grids at once. ⚠️ **D-b widens it a second way, and the widening is in the INPUTS rather than in
   the window**: after D-b the verdict is a conjunction of two synced values, so `billing_eligible`
   arriving on a sync now flips it in exactly the same window `billing_mode` already could, and a
   grid whose TYPE policy changed mid-request loses the same money a toggled switch does. Nothing
   about the shape of the race changes — the count of independent reads is still three, and the
   losing direction is still billable-at-the-gate/free-at-settlement. The fix in shape: record the
   gate's verdict on the transaction and have settlement read it rather than re-ask, so the two
   agree by construction. That is a schema change and belongs to its own decision.
3. **The environment default for billing mode differs between the two repositories** — the relay
   defaults to charging, the control plane to free. No deploy order helps, and nothing here changes
   it.

## Consequences

- **A policy question stops being a code change in two repositories.** Which classes of grid may be
  charged becomes a setting with a route, a validated write and a read that says whether anybody
  ever set it — and the relay learns the answer without learning the rule.
- **Charging still requires two decisions, and one of them is still per grid.** Nothing here turns a
  grid on. Widening the set makes grids *eligible*; somebody still has to throw a switch, and the
  switch still refuses when it cannot work.
- **The safest off switch is the one that touches nothing.** Editing the set stops charging across a
  whole class within the sync interval and leaves every per-grid decision recoverable. That property
  is the reason a bulk off is absent, and it is worth more than the symmetry it costs.
- **The widest money operation in this area now exists, and is fenced by its narrowness rather than
  by its guard alone** — one credential, one scope, a per-grid precondition, an idempotent report and
  a dry run on the same code path. A guard is where the next same-class bug lands; the fences that
  hold are the ones that reduce what the route can reach.
- **One new failure is possible and it is operational, not architectural.** Widening the set before
  the fleet reads the conclusion charges nobody while reporting success. It is prevented by an order
  the same operator controls, and it is why that order is written down rather than assumed.
- **Engine earnings still cannot be withdrawn.** This decision makes it easier to switch charging on
  for many grids at once; it does not make what they earn collectable, and that remains the honest
  precondition for ever changing the default.

## Rollout order

- **The relay's half before the control plane's policy half.** Not a deploy ordering between two
  releases — both directions of the wire degrade to current behaviour (D-c, D-h) — but an ordering
  on the operator's own action: the set must not be widened past its default until the fleet reads
  the conclusion. Landing the two halves as separate pieces of work, with the policy half depending
  on the relay half, is what encodes it.
- **No order within the wire value itself.** An older relay ignores the key and keeps the hardcoded
  predicate; a newer relay against an older control plane reads `NULL` and does the same. Neither
  side is ever silently wrong about a grid.
- **No order for the setting, the two routes, or the rendered sentences.** All of them live in the
  control plane and add no wire value in either direction.
