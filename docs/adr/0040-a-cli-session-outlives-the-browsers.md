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

## Consequences

- **The exposure from a stolen `credentials.toml` grows by 365×**, and no mechanism shortens it. The
  account section of that file is now a year-long bearer credential for the control plane. The
  per-grid tokens beside it were already year-long; the difference is that those can be killed by
  removing the member, and this one cannot.
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
- **`GRID_SESSION_JWT_SECRET` rotation becomes a heavier hammer, and stays the only one.** It was
  already the documented reset after a `grid_users` wipe. It is now also the only answer to a single
  compromised laptop, and it signs out every account on the platform to get there.
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
