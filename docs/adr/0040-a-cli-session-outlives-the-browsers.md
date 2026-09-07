---
status: proposed
---

# A CLI session outlives the browser's, and nothing can take either one back

Signing in to a grid mints an **account session token**: HS256, identity only, no roles, and the
bearer every control-plane call carries. One function mints it — `grid_networks/tokens.issue_session`
— and until now it read one constant, `GRID_SESSION_TTL_SECONDS`, defaulted to 24 hours. Two very
different callers drew from that one number:

- the **website**, `login.html` → `POST /v1/grid/auth/google`, where the session sits in a browser
  that may be on a shared or public computer;
- the **CLI**, `grid login` → `POST /v1/grid/auth/device/poll`, where the session sits in
  `~/.grid/credentials.toml` on a machine its owner chose.

The number was chosen for the first and inherited by the second, and for the second it is wrong in a
way that costs a person something every morning. There is **no refresh route for a session token**.
`grid sync`, written to be "the no-re-auth complement to re-running `grid login`", authenticates with
the very token that expired. So a lapsed session is repaired only by re-running the device flow: a
printed URL, a code, a browser, a Google approval. Until that is done `grid start`, `grid stop`,
`grid join`, `grid task`, `grid launch`, `grid overview`, `grid price`, `grid router` and
`grid members …` all refuse — and only `grid sync` says why; the rest surface the raw control-plane
dump, which names no remedy. On a headless box there is no browser to open at all. (`grid leave` is
the one exception, deliberately: ADR 0023 made it read the session *softly*, because the state it
most needs to repair is the one where the credentials are already gone.)

Raising the number is therefore obvious. What is *not* obvious, and is the reason this decision is
written down, is what a longer life costs — because a grid session token **cannot be taken back**.

## What was measured

Three facts, each read off the code rather than assumed. They are what the trade-off actually rests
on, and a future reader who does not have them will read the decision below as a mistake.

**A session token is unrevocable, and there is nothing to add a revocation to.** `verify_session`
is a bare stateless decode — signature, audience, issuer, expiry, and nothing else. The claims carry
no `jti`. No table records an issued session, so no lookup could be added without first inventing
the row to look up, and no epoch rides the token, so nothing on the account side can invalidate one
already out. The only lever that exists is rotating `GRID_SESSION_JWT_SECRET`, which signs *everyone*
out at once — the operator's hard reset, documented as exactly that, not a per-account revocation.

**The per-grid access token is also a year old, and that is not the same risk.** `issue_access_token`
already mints for `GRID_ACCESS_TOKEN_TTL_SECONDS`, defaulted to 365 days. It gets away with it
because it carries `member_epoch` and `network_epoch`, and the relay actively rejects a stale one:
`grid_auth.py` 401s on `claims["member_epoch"] < member.member_epoch` and again on
`claims["network_epoch"] < state.network_epoch`. **Nothing equivalent exists for the session token.**
The two tokens sharing a lifetime is a coincidence of numbers, not a shared safety property, and
reading it as one is the specific mistake this ADR exists to prevent.

⚠️ Two details of that lever, because the short version of it is over-broad in a way that matters
here. The `network_epoch` check is unconditional, so *bumping the network* kills every outstanding
token on any grid. The `member_epoch` check is **not**: `_requires_allowlist` returns `False` for
`permissionless`, `private-domain`, `domain-restricted` and `os-community`, so on those four types
removing a member does not invalidate the tokens they already hold — the denylist (a 403) is the
per-account lever there instead. `private-domain` is the type `/auth/google` auto-provisions two
lines from the code this ADR changes, so the exception is not a corner. The conclusion survives:
the access token has a per-account revocation lever on every type, by one mechanism or the other,
and the session token has none.

**A serve loop never touches a session token.** `remote/serve.py` authenticates entirely with the
per-grid `access_token`, and renews it through `refresh_network_token`, which is unauthenticated by
design — the refresh token in the body *is* the credential. Every caller of
`credentials.require_session()` is a `cli/` command a person typed. So the population this change
extends is exactly *people at terminals*: a headless provider running for months is unaffected by
the old TTL and unaffected by the new one.

## Decision

### D-a — The two mints stop sharing a number

`issue_session` grows an explicit lifetime parameter, and each mint site passes the one it means.

| | lifetime | variable | route |
|---|---|---|---|
| website session | **24h, unchanged** | `GRID_SESSION_TTL_SECONDS` | `POST /auth/google` |
| CLI session | **1 year** | `GRID_CLI_SESSION_TTL_SECONDS` (new) | `POST /auth/device/poll` |

`GRID_SESSION_TTL_SECONDS` keeps its name and its meaning. Renaming it to something website-specific
would have read better and was rejected: it is an existing deployment variable, and a rename is a
change where an environment file missed in the edit silently reverts the website to a compiled-in
default — a regression nothing in the code would report. The variable that moves is the one that did
not exist yesterday.

⚠️ **The year is keyed on the route, not on any evidence of who called it.** Nothing on the device
flow establishes a CLI: `/auth/device/start` and `/auth/device/poll` both take no `Authorization`
header, the `device_code` in the body is the only credential, and both sit behind the same CORS
policy as the FE. So anyone who can drive a person to approve a code at `/grid/device-login` gets
the year — the classic device-code phishing shape, now with a 365× prize and no revocation. That is
a real widening and it is accepted here rather than hidden: closing it means giving the flow
something a browser cannot forge, which is a change to the device flow itself and not to a TTL. The
decision this ADR takes is *which route mints which lifetime*; it does not claim the route can tell
a terminal from a tab.

The audience claim stays `grid:website-session` for both. It is wrong for the CLI and always was, but
it is load-bearing on both sides of `tokens.py`: `verify_session` requires it, so changing the string
invalidates every outstanding session on deploy — every signed-in account, website and CLI alike,
signed out to make a name read better. It is a rename worth doing beside a secret rotation, and not
inside a change whose entire point is to stop signing people out.

### D-b — The default is the *short* one, so a future mint inherits caution

`issue_session(user)` with no lifetime still means 24 hours. Two callers rely on that and are
deliberately left alone: `os_networks` and `domain_networks` each mint a session while
auto-provisioning a grid, purely to hand to a shell-out that happens immediately. They have no
reason to outlive it, and a token minted for a subprocess is precisely the one that should not live
a year.

*(Both of those mints have since moved down one layer into `managed_shellout.shellout_session`, the
single mint behind every seeded `GRID_HOME`. The rule is unchanged and now covers more: see the
Consequences bullet below.)*

The direction matters more than the two callers. A third mint site added later and given no thought
gets the conservative number. Had the default been the year, the same carelessness would have
produced an unrevocable year-long token for something that needed thirty seconds.

### D-c — The lifetime is an environment variable, because it is the only handle on it

Given D-a's finding, `GRID_CLI_SESSION_TTL_SECONDS` is not a tuning knob — it is the sole control
the operator has over how long a compromised laptop's session stays useful, short of rotating the
secret and signing out every account on the platform. Shortening it takes an env edit and a
restart, no release. It changes nothing already issued, which is the whole problem restated, but it
bounds the exposure from the moment it lands.

### D-d — Both sign-in routes move onto one helper first

`/auth/google` and `/auth/device/poll` each repeated the same three steps: upsert the account, mint
the session, build the reply. Splitting the TTL means editing both, and two copies of a three-step
sequence where the copies must now *differ in exactly one step* is how the wrong constant reaches
one of them.

So `handler._sign_in` does the three steps and takes the lifetime as a parameter. Both routes are
moved onto it and their replies stay byte-identical — the caller owns the envelope, so the poll's
`status` key stays first. The prefactor lands with the split rather than before it because its only
purpose is to make the split expressible in one place.

### D-e — Revocation, as an account epoch rather than a token registry

*(Amendment. `harness-grid-login` issue 07, grid-apis. The alternative rejected above is taken, in a
shape the cost it was rejected on does not reach.)*

The rejection stands as written: a `jti`, a table of issued sessions and a per-request lookup would
put **a database read on every authenticated control-plane request**, and that is a latency and
availability change to the whole surface. What the rejection did not weigh is that the *other* token
in this system solves the same problem without any of that. `issue_access_token` mints for a year and
gets away with it because the relay compares a number in the claims to a number it already holds —
`member_epoch`, `network_epoch`. Nothing is looked up per token, because the thing being revoked is
not the token.

So the session token gains a `session_epoch` claim, and `grid_users` gains a `session_epoch` column:

- **One row per account, never one per token.** There is no registry, nothing grows with sign-ins,
  and nothing needs collecting. Revoking is `session_epoch + 1` on one row, and it invalidates every
  session that account holds and nothing else. Per-grid access tokens are untouched — they have their
  own epochs and their own levers.
- **`verify_session` compares against a CACHED epoch**, 30-second TTL, so the steady-state cost is a
  dict lookup rather than the per-request read this ADR priced. That bound is the whole justification:
  if the check ever grows a database read per request it has become the alternative rejected above and
  needs re-arguing, not merging. What the TTL costs is that a revocation lands within 30 seconds
  everywhere rather than instantly, and it can only ever be late in the harmless direction — a stale
  entry is *lower* than the truth, and a lower enforced epoch refuses nothing a higher one would have
  allowed.
- **The comparison is `claims["session_epoch"] < current`, never `!=`, and the floor is 0.** Both
  halves are one decision and both are the difference between shipping this and causing the outage it
  exists to make unnecessary. Every token minted before this shipped carries **no claim at all**; it
  is read as the floor, and the floor is what the column defaults to, so `0 < 0` is false and nothing
  outstanding is refused. Starting the column at 1 — the house convention, which `member_epoch` and
  `network_epoch` both follow — signs out every signed-in account on the platform on deploy. And a
  token whose epoch is *ahead* of a worker's cached value is an ordinary sign-in served elsewhere, not
  a forgery, which is what `!=` would get wrong.
- **Grandfathered on the deploy, not grandfathered forever.** Because a claim-less token reads as the
  floor rather than being exempted from the check, the first revocation an account makes takes those
  tokens with it. The lost laptop this exists for is holding exactly one of them.
- **`grid logout --everywhere` is the verb**, and `grid logout` is unchanged: still local, still a
  credential delete, still the per-machine answer (see the Consequences bullet below, which stands).
  The revoke runs after ADR 0023's serve-child teardown and before the delete — the teardown's
  deregisters are authoritative only while the credentials exist, and the revoke is authorized by the
  session token the delete destroys.

⚠️ **`GRID_SESSION_JWT_SECRET` rotation is still the only lever for one case**: a session whose
`grid_users` row has been deleted. There is nowhere to record the bump, so the route refuses rather
than answering 200 over a revocation that did not happen — with a **401, deliberately not a 404**.
On a new route 404 already means something to the caller: the CLI reads it as "this control plane
predates the route" and says so in those words, and answering 404 here would tell somebody on a
current control plane that their control plane is out of date, hiding the real cause. 401 is also
what the state is: the session is intact but names nobody, so it authorizes nothing.

⚠️ **`grid logout --everywhere` on a machine whose session is ALREADY revoked still signs that
machine out.** The 401 does not refuse the local sign-out, because the credential a refusal would
keep — "so a retry can reach the route" — is the one credential that will never be accepted again.
Refusing there would mean the flag could never sign that machine out at all. A 404 keeps the
credentials, for the opposite reason: there the session is still good and is the handle that works
the moment the control plane catches up.

⚠️ **The device flow is still unauthenticated by design (D-a), and this changes nothing about that.**
Anyone who gets a person to approve a code at `/grid/device-login` still walks away with a year — the
difference is only that it can now be taken back. The remaining control point is the approval page
itself, which should say what is being approved *and for how long*; it lives on the public website,
which is in none of these repositories. Making the flow prove its caller is a CLI (a `device_id`,
say) was considered and refused: a browser can send one just as easily, so it would buy the
appearance of a barrier rather than a barrier.

## Rejected alternatives

**Raise both to a year.** One number, one edit, no new variable, and the same daily annoyance gone.
Rejected on the population, not on the mechanism: the website's session lives in a browser, and a
browser is the one place a grid credential routinely sits on a computer its owner does not control.
A year-long session on a library machine is a year-long session for whoever sits down next, and it
cannot be revoked. The CLI's session lives in a file, on a machine somebody chose, next to
`credentials.toml`'s per-grid tokens — which are *already* a year long. The CLI change adds no new
class of exposure; the website change would.

**Raise, and build revocation.** The honest version: a `jti`, a table of issued sessions, a lookup
in `verify_session`, and a route to kill one. It would make the session token as answerable as the
access token already is, and it is the right long-term shape.

Rejected as this change's scope for two reasons. It puts a **database read on every authenticated
control-plane request**, where today there is none — `verify_session` is a pure decode, called by
every route, and making it stateful is a latency and availability change to the whole surface, not
a feature. And it is not what removes the daily re-auth: the re-auth is removed by the TTL, and the
revocation is a separate safety property that can be added later without re-deciding this. Adding
both at once means shipping neither until both are right.

What is *not* a defence, and should not be written down as one: "the token is short-lived, so
revocation does not matter." That was never true — 24 hours of unrevocable access is already 24
hours — and after this change it is not even approximately true.

**Taken, later, in a cheaper shape — see D-e.** The rejection above costed one design and was
right about it. It did not cost the design the access token already uses, and that one carries none
of the price this paragraph refuses to pay.

## Consequences

- **The exposure from a stolen `credentials.toml` grows by 365×**, and no mechanism shortens it. The
  account section of that file is now a year-long bearer credential for the control plane. The
  per-grid tokens beside it were already year-long; the difference is that those can be killed by
  removing the member, and this one cannot.

  **The second sentence is now false** (D-e): the session can be killed too, by bumping the account's
  epoch. The first stands — the *lifetime* is still a year and nothing shortens it. What changed is
  that the year is no longer unanswerable.
- ⚠️ **The control plane writes its own copy, and that copy is now a year old too.** Every
  `managed-*` route seeds the caller's raw bearer into a per-admin `credentials.toml` on the
  control-plane VM (`managed_shellout.seed_caller_home_env` → `managed_homes.seed_home_at`), mode
  `0600`, so the shelled-out `grid` CLI finds a logged-in profile. Nothing prunes it. What used to
  be at rest there for at most a day is now at rest for a year, and the managed-networks PRD says in
  as many words that it was relying on the 24h expiry. Seeding a **freshly minted** short session
  instead of echoing the caller's bearer would fix it, and is exactly the case D-b's default exists
  for. Nothing measured objects: `managed_reconcile`'s own docstring records that `grid network
  start` is purely local and needs **no** session token, so the one reader outside a request never
  looks at the seeded one, and every other reader of a home sits inside a request that already
  carries a live bearer. It is still that feature's change to make rather than this one's — it is
  recorded here because this change is what turned it into a question.

  **Done** (`harness-grid-login` issue 06, grid-apis): `managed_shellout.shellout_session` mints the
  seeded token — same account, D-b's short default — and `seed_caller_home_env` no longer *takes* a
  token, so no route can put a request bearer on that disk. One reader turned out to sit outside its
  seeding request after all, and it was checked rather than assumed: the first provider's home is
  swept by `grid --remote leave` weeks later and never re-seeded, but that leave stops its child by
  run-record pid, deregisters with the per-grid `access_token`, and consults the session only to
  resolve a relay URL the join already wrote — degrading to the ~120s node TTL at exit 0 if it
  cannot (ADR 0023). `grid network delete` and `restart-server` are local like `start`. What is
  *not* done is a reaper for stale homes: the credential is short now, the disk still never shrinks.
- **`GRID_SESSION_JWT_SECRET` rotation becomes a heavier hammer, and stays the only one.** It was
  already the documented reset after a `grid_users` wipe. It is now also the only answer to a single
  compromised laptop, and it signs out every account on the platform to get there.

  **No longer true, and D-e above is why** (`harness-grid-login` issue 07): a single compromised
  laptop is now `grid logout --everywhere`, which moves one account's number and touches nobody
  else's session. The rotation is back to being what it always was — the reset after a `grid_users`
  wipe — plus the one case the epoch cannot reach, a session whose account row is already gone.
- **Nothing is retroactive, in either direction.** Sessions minted before this deploy keep their day;
  sessions minted after keep their year even if the variable is later lowered. Shortening the
  variable strands nobody and rescues nobody already issued.
- **No rollout order.** The control plane is the only repo that mints or reads a session's expiry;
  the CLI stores the token opaquely and never inspects it. An old CLI against a new control plane
  simply stops needing to re-authenticate. This is the one part of the `harness-grid-login` feature
  with no cross-repo seam, which is why it ships on its own.
- **`grid logout` is unchanged and is still the per-machine answer.** It deletes the stored
  credentials, which is the only way a specific session stops being usable — a local delete, not a
  revocation. A token copied off the machine first is unaffected, and always was.
