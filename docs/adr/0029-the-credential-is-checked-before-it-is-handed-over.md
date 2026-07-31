---
status: accepted
---

# `grid launch` checks the credential before it hands it over, and refreshes it in place when it can

[ADR 0028](./0028-launch-hands-an-app-the-grid.md) chose the **public** `GET /relay/v1/grid/overview`
for the launch preflight, because `cli/remote_overview` already read it, and recorded the cost in its
first consequence: *"Preflight proves model presence, not credential validity … Accepted knowingly;
the authenticated route above is the fix if it bites."* This is that fix.

It bites harder than "the user sees an error later", because of **where** the error lands and **what
it looks like when it gets there**. The relay's 401 leaves through `_run_anthropic_endpoint`, which
re-dresses it in Anthropic's envelope with `error_type_for_status(401)` = `authentication_error` — the
vendor's own "your credentials are bad" type. Claude Code reads that as an Anthropic auth problem, at
the first prompt, after the app owns the terminal and the launcher is gone. The only thread back to
the real cause is the substring `Invalid Grid token: …` inside the message. Nothing names `grid`, and
the command that fixes it is in another terminal.

The class is also wider than the word "expired". A grid token is rejected for five different reasons,
and only some are repairable:

| cause | the relay says | repairable locally |
|---|---|---|
| past `exp` (365 d) | `Invalid Grid token: …` (401) | yes — refresh |
| member roles changed → `member_epoch` bump | `Grid member token epoch is stale` (401) | yes — refresh |
| membership/denylist rotated → `network_epoch` bump | `Grid network token epoch is stale` (401) | yes — refresh |
| removed / inactive / denied | 403 | **no** |
| token carries no inference scope | 403 `Missing required scope: …` | **no** |

On an active shared grid the two epoch bumps are likelier than a 365-day expiry, so a fix aimed only
at `exp` would solve the smaller half.

## Decision

> **No launch hands over a credential it has not checked. The check is offline first and authenticated
> second; a credential that is merely stale is refreshed in place, silently, and one that cannot be
> repaired is refused before the app starts — naming the cause and the command that fixes it.**

Three layers, one refresh budget for the whole run:

| | what it does | what only it catches |
|---|---|---|
| 1 | decode the JWT's `exp`, compare to the clock with a **24 h margin** | a token that is valid *now* and dies mid-session |
| 2 | exchange the refresh token for a fresh one | — (this is the repair, not a check) |
| 3 | `GET /relay/v1/models`, body discarded | epoch staleness, revocation, missing scope, a slow clock |

Neither check subsumes the other, which is why both ship. The probe returns **200** for a token with
four minutes left on it, so only layer 1 protects a work session; and layer 1 cannot see an epoch
bump, a revocation, or its own clock being wrong, so only layer 3 protects against those.

The margin is 24 h rather than `remote/serve.py`'s `_CODEX_EXPIRY_MARGIN = 600.0` because what is
being protected is a human work session, not a poll iteration. It is 0.27 % of a 365-day token's life,
so it cannot cause spurious refreshes in practice.

**`inference:models` is a sound proxy for `inference:create`.** `role_scopes` only ever writes the
inference scopes as one bundle (`scopes.update(INFERENCE_SCOPES)`), so a token holds
`inference:models` — which the probe needs — exactly when it holds the `inference:create` that
`/messages` needs. The probe therefore produces no refusal the app would not have produced itself.

### The order: credential, then models

Preflight's two halves swap. The asymmetry is the reason: a dead credential invalidates the model
advice — *"run `grid join` on an engine that serves `claude:opus`"* sends the user off to do work that
will not help — while a missing model does not invalidate the credential advice.

### A one-shot command may now refresh, which reverses a recorded decision

`cli/remote_request.py` states, and has always stated, that *"refresh-on-401 stays in the long-running
serve loop ([ADR 0004](./0004-remote-provider-serve.md)), not on this one-shot path"*. That decision
stands for `chat`/`image`/`edit`/`video` and is not touched here. `launch` is exempt on a mechanism,
not on a preference:

- A `grid chat` failure costs **one re-run**. It surfaces in about a second, in the user's own
  terminal, through a message the CLI wrote. "Run `grid login`, then retry" is a complete remedy.
- A `grid launch` failure is handed to **a different, long-lived process** whose error surface we do
  not control, and it surfaces minutes later dressed as the vendor's own auth error. The credential
  has to be good for the *session's* lifetime, not for one request — which is a different requirement,
  and the only one of the two that a margin and a repair can satisfy.

### The write contract

`grid launch`, `--print-env` included, now writes `~/.grid/credentials.toml`.

ADR 0028's *"never exported to the user's shell, never written to a config file, never to
`os.environ`"* is a rule about **what the app is given** — the endpoint, the credential and the model
names — not about Grid's own credential store, which `grid login` and `grid sync` already rewrite.
Refreshing our own cached credential is maintenance of our own state; nothing is left behind for the
*app*, closing it remains the entire cleanup, and there is still no `--restore`. `--print-env` still
starts nothing: a probe is a read, unlike resolving the binary, which can spawn an installer and is
therefore still skipped there.

### Failing open, and the clause that bounds it

The rule this feature already follows elsewhere (`shared/launch/claude_install._warn_unchecked`,
`shared/launch/claude._settings_paths`) is that **a check that could not run says so, and never costs
the user a launch _that would have worked_.** The italicised clause decides every ambiguous case:

- Token **already expired**, and the refresh fails → **refuse**. The launch would not have worked, so
  failing open would only guarantee the in-app failure this exists to prevent. A refresh that failed
  because the *control plane* is broken (503, transport) still refuses, but says so and says retry —
  it does not send the user to `grid login`, which fixes nothing.
- Token **within the margin** but still valid, and the refresh fails → **warn and launch**. The
  credential works right now; a control-plane outage must not take a working launch away.
- Probe throttled (429), 5xx, transport, timeout → **warn and launch**. Nothing was learned.
- Token is not a decodable JWT → **"cannot tell", not "expired"** — fall through to the probe.

## Considered options

**Refuse instead of refresh, rejected.** It would bring back the recipe this feature exists to
delete: *"your token expired — run `grid login`, then `grid launch` again."* And `grid login` opens a
browser to obtain something the stored refresh token already yields offline.

**`grid sync` as the remedy, rejected.** It re-issues every bundle, but it authenticates with the
**session** token (24 h) while the access token lives 365 d and the refresh token 2 y — so it is
expired in precisely the situation where the access token is, and fails exactly when it is needed. The
refresh exchange is unauthenticated by design (the refresh token *is* the credential), which is what
makes it the only remedy that reliably works.

**The offline check alone, rejected.** Cheapest and catches the named case, but cannot see an epoch
bump, a revocation or a missing scope — and a machine whose clock runs slow reads an expired token as
valid, which is the one failure mode a clock-based check cannot self-diagnose.

**The probe alone, rejected.** It answers 200 for a token that expires in four minutes, so it cannot
protect a session that outlives the token. It is a check of *now*; a launch is a bet on *later*.

**Letting the probe supply the model list too, rejected.** `GET /relay/v1/models` returns the models,
so folding preflight into it would cost no extra round-trip at all. But `grid models` and
`grid engines` render the **overview**, and preflight sharing that read is what lets its refusal say
*"Models on `<grid>`: …"* and mean the same thing the user's next command will print. Buying back one
round-trip by making the refusal and the diagnostic disagree is a bad trade.

**Reading the stored `expires_at`, rejected.** The login bundle carries it, but
`credentials.update_network_tokens` does not rewrite it, so it is wrong for every token the serve loop
has ever refreshed. The JWT is self-describing and cannot drift.

**Validating with a real `/messages` request, rejected.** It would prove the credential end to end for
the exact route the app uses — by creating and billing a job.

## Consequences

- **`grid launch` mutates `~/.grid/credentials.toml`** when, and only when, the stored credential
  needed repairing. A healthy token writes nothing.
- **A concurrently running serve loop's in-memory refresh token goes stale** the moment a launch
  rotates it, and stays stale until that loop's next 401 — at which point
  `remote/serve.py:_ServeState.refresh` re-reads the file and adopts the token this process stored,
  which it already does for exactly this reason. So it self-heals, but not immediately, and a reader
  should not have to rediscover that.
- **Refresh-token rotation is unconditional and destructive.** The control plane mints a new refresh
  token on *every* exchange and the old one stops matching at once, while
  `credentials.update_network_tokens`' `refresh_token` parameter is **optional** — so any caller that
  persists only the access token permanently destroys the account's refresh credential on that
  machine, sending every later launch *and* the serve loop back to `grid login`. Recorded here because
  the failure is silent, is not what the caller was thinking about, and passes any test written about
  the access token.
- **Every launch costs one authenticated relay round-trip**, bounded by an explicit per-phase timeout
  so the fail-open branch is reached in ~8 s rather than the 15 s a one-shot relay call uses. The
  overview read is unchanged and still public.
- **The 403 outcomes have no local remedy** and are refused as such. The scope refusal names the roles
  that grant inference (`consumer`, `both`) rather than telling the user to ask the grid's owner: the
  control plane's owner fallback synthesises `roles=["admin"]`, whose scopes carry no inference at
  all, so this refusal can land in front of the owner.
- **`grid info --env` still hands out an unchecked token** for OpenAI clients, with the identical
  failure mode. It is [ADR 0005](./0005-remote-consume.md)'s surface, so it is not changed here; the
  gate is shaped to be its second caller.
