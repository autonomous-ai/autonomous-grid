---
status: proposed
---

# A billing misconfiguration is found by asking, not by noticing the revenue report is wrong

Every gate onto charging in this product is a gate *at the moment somebody throws a switch*. The
per-grid switch refuses when charging could not work; the creation door downgrades and warns. Both
are correct and both share one predicate, which ADR 0042 D-k and ADR 0043 D-a spent two decisions
getting right.

None of them answers the question an operator actually has, which is not *may I turn this on* but
**is what I already turned on still working**. Nothing in this system reconciles the state it is in
against the policy in force. The only instrument that reports a billing misconfiguration today is
the revenue report, weeks later, and it reports the symptom rather than the cause.

ADR 0043 made this materially worse in one specific way, and that is what forces the decision now.

## The state ADR 0043 D-a made reachable, and nothing reports

Editing the billable set deliberately **does not touch any grid's `billing_mode`** (ADR 0043 D-e).
That is right — it is exactly what makes narrowing the set a lossless off switch, recoverable by
re-widening. But the state it leaves behind did not exist before, because before D-a the set was a
constant and nobody could narrow it:

```
        billable_network_types           each grid's own switch
        (the operator's policy)          (the record of per-grid decisions)
                 │                                  │
                 └──────────── AND ─────────────────┘
                                │
     narrow the set  ─────────► false ∧ public  =  nobody is charged
                                │
                    the relay is right, the row still says `public`,
                    and NOTHING anywhere compares the two
```

A grid sitting at `billing_mode = 'public'` whose type is no longer in the set is **exactly disaster
one** — *charges nobody while reporting itself as charging* — the disaster ADR 0042 D-k's
preconditions exist to prevent. The preconditions prevent it being *entered through a door*. They
cannot prevent it being *arrived at* by a policy edit, because the policy edit does not go through
either door.

Four further misconfigurations are reachable today and none of them is reported either. Each was
verified in the code rather than assumed:

- **The two money credentials are never compared.** `_require_system_admin` reads
  `GRID_ADMIN_API_KEY`; `_require_report_key` reads `GRID_USAGE_REPORT_KEY` (`handler.py:808`).
  `.env.example` documents them as the same value and nothing enforces it. Set differently, the
  policy route works and the per-grid switch it multiplies refuses.
- **`GRID_BILLING_MODE` swallows every typo.** `normalize_billing_mode` (`store.py:700`) returns
  `public` only for a spelling of `public` and `local_free` for *everything else* — so `publik`
  reads as free, silently, on a control plane whose operator believes it is charging. ⚠️ This is
  deliberate and must stay: an environment variable has no request model in front of it, so the
  reader has to agree with what the store writes. The defect is that nothing *says* the value was
  not understood.
- **The environment default differs between the two repositories.** grid-src `config.py:631`
  defaults `GRID_BILLING_MODE` to **`public`** (charging); this control plane defaults it to
  `local_free`. Recorded as an open hole in ADR 0043 D-i, item 3.
- **A stored setting that cannot be parsed restores a policy nobody wrote.** ADR 0043 D-a chose the
  default over the empty set there, deliberately, and made it loud at ERROR — but an ERROR in a log
  is only found by somebody already looking.

## Decision

### D-a — There is one read-only route that answers *is billing configured correctly*, and it counts state rather than describing settings

`GET /admin/billing-preflight`, system-admin. It answers what this control plane can actually
observe, and its headline is not a setting at all: **the grids whose `billing_mode` says they are
charging and whose type is not in the billable set.**

A route rather than a startup check, because the interesting failure is *state*, and state changes
after boot — every one of the conditions above can become true on a running control plane, most of
them through a route this same operator called.

⚠️ **It counts rows; it does not re-derive the policy.** The predicate is
`billing_activation.is_billable_network_type` against `handler._billable_network_types`, the same two
functions both doors use. A second implementation of *which grids are billable* is precisely the
defect this whole area exists to avoid, and a diagnostic that disagrees with the gate is worse than
no diagnostic — it would be believed.

⚠️ **The list is capped and reported with its own total.** An unbounded query on a table that holds
every grid on the platform is not a diagnostic, it is an outage waiting for the fleet to grow. The
count is the fact; the sample is the convenience.

### D-b — It reports only what this side can see, and says so where it cannot

Two things an operator will expect from it are **not answerable here**, and inventing a field for
either would be worse than the gap:

| the question | why this control plane cannot answer it |
|---|---|
| has the fleet actually read `billing_eligible` yet? | `grid_networks` carries no sync timestamp — `last_seen_at` is a device row, not a snapshot one. The relay's copy lives in the relay's database |
| is the relay's own `GRID_USAGE_REPORT_URL` set? | it lives in the relay's environment and the control plane never sees it — the same limit `failing_precondition` already records for the same value |

This is the discipline `failing_precondition`'s docstring already sets: name the half that is
observable from this side, and say plainly that reaching for the other half is not a gap to fill
later because there is nothing here to read.

⚠️ **The credential check reports a boolean and never a value.** Whether the two keys match is
computed with `hmac.compare_digest` and only the answer crosses the wire. And when either is unset
the answer is `null`, not `false` — "I cannot say" and "they differ" are two different operator
actions, and collapsing them sends somebody to rotate a key that was never set.

### D-c — The environment is validated once at boot, and it LOGS rather than refuses

A `GRID_BILLING_MODE` this control plane could not understand is reported at ERROR when the process
starts, naming what was written and what is actually in force.

**It does not refuse to boot, and that is not timidity.** ADR 0042 D-k already decided this exact
direction one level down: the creation door never refuses over a mis-set billing variable, because
turning it into a failure to register grids at all *trades a quiet revenue bug for a loud outage*.
Refusing to start the whole control plane for the same condition is that same trade, escalated —
and the value in force (`local_free`) is safe, which is what distinguishes this from
`MIN_BALANCE_CREDITS`, where a non-finite environment value leaves **no usable number at all** and
therefore rightly raises at import (`handler.py:4103`).

⚠️ **The check compares the RAW value against the normalized one**, and fires only when they
disagree and the raw value was non-empty. Unset is not a misconfiguration — it is every deployment
that never asked for charging, and warning about it is the noise that stops real warnings being read
(the same reason ADR 0043 D-a made the unset setting silent, and the same reason the creation
door's downgrade warning is gated on the operator having actually asked).

### D-d — grid-src's environment default becomes `local_free`, closing ADR 0043 D-i item 3

`config.py:631`'s `os.getenv("GRID_BILLING_MODE", "public")` becomes `local_free`, matching this
control plane and matching every other default in the area.

*Why this is safe to change rather than merely tidy.* Production has written the value explicitly
since 2026-07-01, so no running grid depends on the default; the change only reaches a master
started **outside** `network_runtime`, which is the case that today charges by default with nobody
having asked. The direction of the change is from *charge unless told otherwise* to *do not charge
unless told to*, which is the only direction a default on a money switch may have.

⚠️ It is a **behaviour** change in a second repository and therefore its own piece of work, not a
side effect of the diagnostic. It needs no deploy order: a master reading the new default charges
strictly less than one reading the old, and every grid that was deliberately charging says so in its
own environment or in its synced `billing_mode`.

## Consequences

- **The question stops being unanswerable.** "Is billing configured correctly" becomes one
  authenticated GET whose headline is a count of grids in the state the whole area exists to
  prevent — including grids that reached it before anybody thought to look.
- **The diagnostic cannot drift from the gate**, because it calls the gate's own predicate. That is
  the property worth more than anything the route reports.
- **Two honest gaps are documented rather than papered over.** An operator reading this route knows
  it says nothing about whether the fleet has synced, which is exactly the thing they must check
  another way before widening the set.
- **One class of silent typo becomes a line in the log at boot**, and the value in force stays the
  safe one either way.
- **A money default stops meaning "charge".** The remaining asymmetry in ADR 0043 D-i is item 1 (the
  media path) and item 2 (gate/settle can disagree); item 3 is closed by D-d.
- **This adds a fourth admin route to the billing surface and no new wire value.** Nothing here
  crosses a relay seam, so nothing here enters the cross-repo lockstep register.

## Rollout order

- **None between the three pieces.** The route is read-only, the boot check only logs, and the
  default change is confined to grid-src and strictly reduces charging.
- ⚠️ **But an ordering on the operator, inherited from ADR 0043**: the preflight route is what makes
  *narrowing* the set safe to reason about afterwards, so it is worth having before the set is
  edited in anger. It does **not** replace the requirement that the fleet reads `billing_eligible`
  before the set is widened — the route cannot see whether the fleet has, which is D-b's first row.
