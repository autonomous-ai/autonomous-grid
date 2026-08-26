---
status: proposed
---

# A web search is not an inference, and its key never leaves the control plane

Every agent a grid drives can already search the web, and it does so on the user's own machine. The
app writes three scripts into `~/.grid/app/agent-scripts/` and a guide that names them by absolute
path; the agent reads the guide, then runs one of them under its Bash tool:

```
"$uv" run --with ddgs        python3 …/search.py "<query>"
"$uv" run --with trafilatura python3 …/read.py   "<url>"
"$uv" run --with playwright  python3 …/browse.py "<url>"
```

So a search is a DuckDuckGo query from a residential IP, a read is a plain fetch, and the first
JS-heavy page costs a ~170 MB Chromium download. Three things are wrong with that, in ascending
order of how much they cost:

- **`uv` is a runtime dependency of answering a question.** Each script resolves its package on
  first use. When that fails the script exits 2 and the guide's only advice is that web access is
  not available right now.
- **DuckDuckGo rate-limits a burst**, which the guide itself has to warn the agent about. A grid
  that runs `/loop` and `/goal` — work that outlives a turn — is exactly the caller that bursts.
- **The grid carries none of it.** The user's machine, the user's IP, the user's bandwidth. A grid
  is supposed to be the thing that answers; here it is the one component not involved.

The fix looks obvious — put the search behind the control plane, which is the only place that can
hold a vendor key without handing it out — and the obvious implementation is wrong in four places,
because the platform already has rules that decide what this operation is allowed to mean.

- **A master inherits the control plane's entire environment minus five names.**
  `managed_shellout.subprocess_env` builds the subprocess env as
  `{k: v for k, v in os.environ.items() if k not in SUBPROCESS_ENV_BLOCKLIST}`, and that blocklist
  holds `GRID_SESSION_JWT_SECRET`, `JWT_SECRET`, `GRID_GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_ID`,
  `GRID_DATABASE_URL` — nothing else. Its docstring says the exclusion covers "the subprocess **and
  the master service it spawns**". It is a denylist, so *"the control plane holds the key"* is not a
  property of putting the key in the control plane's environment. `DEEPGRAM_API_KEY` is in that
  environment today and reaches every master by exactly this route.
- **Charging is a statement about whose hardware served the request.** `relay._billing_on` returns
  `False` unless the grid is `permissioned-providers` *and* its billing mode is `public`. On a
  company grid, inference is free because the company's own machines answered it. Nothing about
  that reasoning transfers to a vendor the operator pays per call.
- **The relay already calls the control plane, and it already reports usage.** `_fetch_min_balance`,
  `_fetch_authoritative_balance` and `_report_usage` (`relay.py:5550`, `:5609`, `:5626`) each
  present `X-Admin-Key` to `/internal/min-balance`, `/internal/accounts/{google_sub}/balance` and
  `/internal/usage`. So the seam this feature needs is not new territory; it is a road with traffic
  on it.
- **The relay cannot verify the credential the control plane uses.** A session token is HS256 and
  24 hours (`tokens.py:23`); a per-grid access token is RS256 and 365 days (`:22`), verified against
  JWKS. The relay is a relying party holding no signing key, so it can read the second and not the
  first — which decides, on its own, which credential a script must present.

## Decision

### D-a — The Exa key never leaves the control plane, and one line in a denylist is what makes that true

`EXA_API_KEY` is read by `os.getenv` **inside** the client module and never returned, on the model
`transcription.py` already sets for Deepgram: no FastAPI, no database, a blocking `requests.post`
with a sized timeout constant, two typed errors, and defensive parsing before any indexing. The
route maps them to 503 unconfigured / 502 upstream.

`EXA_API_KEY` is **added to `SUBPROCESS_ENV_BLOCKLIST`**. Without that line the key is present in
the environment of every master the control plane spawns, and nothing anywhere fails — the feature
works, the tests pass, and the premise it was built on is simply untrue. The line is the decision;
the module structure is only good manners.

This is a deliberate asymmetry with Advisor keys, which ride the sync snapshot to running masters
because a master must call the Advisor itself. The Exa key must not, because a master is not
guaranteed to be a machine the operator owns. Do not "fix" this into consistency.

### D-b — The relay is in the path for accounting, not for authentication

Two routes on the relay, `POST {RELAY_PATH}/web/search` and `POST {RELAY_PATH}/web/contents`. The
caller presents the per-grid access token as `Authorization: Bearer`; the relay verifies it with
`verify_grid_token`, mints the `request_id`, and forwards to the control plane's `/internal/web/*`
with `X-Admin-Key`, carrying `google_sub`, `network_id` and that `request_id`.

The relay is **not** load-bearing for auth — the control plane could authenticate the app's session
token directly, in one line, with dozens of precedents. It is load-bearing for two other things:
every web search is attributable to a grid at the point where grids are distinguishable, and
per-grid policy has somewhere to live when it is wanted. That is worth stating because the cost is
real and paid up front: web search now requires a grid, and it does not work in local mode at all.
Both are accepted.

`request_id` is minted by the relay and never by the caller. The ledger's idempotency claim is a
unique index on it, so a caller that supplies its own can replay one.

### D-c — A web search is free to the member, metered at cost, and bounded by quota — it is not `_billing_on()`

Every successful web search writes one `grid_account_ledger` row: `entry_type = 'web_search'`,
`amount_usd` = the `costDollars` Exa reports for that call, `network_id` = the grid it came through,
and **no change to the balance**. Nobody's wallet is debited and no minimum balance gates the call.

Reusing `_billing_on()` here would have been the obvious move and is the trap: on a company grid it
returns `False`, so web search would be free, unmetered and unbounded on precisely the grids where
inference is free for a reason that does not apply. The operator pays Exa on every grid.

Charging is deferred rather than designed away. Because the cost recorded is the vendor's own
figure and not an estimate, turning charging on later is a change of policy against months of real
data, instead of a price picked today by guessing. The ledger row is what makes that a flip rather
than a rewrite.

⚠️ The row is written **after** Exa answers, not reserved before it. Two concurrent searches can
therefore both pass a quota check and overshoot it by a few. That is the correct trade: a quota
should count what happened, and the alternative charges a member for a search the vendor failed to
perform.

### D-d — The quota is per account, never per grid

The bound is a count on `grid_account_ledger` keyed by `google_sub` over a rolling window,
evaluated in the control plane. It needs no new table and no new index: `idx_grid_account_ledger_sub
(google_sub, created_at)` is already the shape of the query.

Per-`(member, grid)` was rejected. Creating grids is bounded — the free plan allows one, and asking
for a second answers `402 free_network_limit` — but **being invited to a grid is free and
unbounded**, so a per-grid key multiplies a person's quota by the number of grids they can get
invited to. It would also still have to be enforced in the control plane, since a relay sees only
its own grid; it is therefore not simpler than the account key, only wider.

The deciding argument is direction. A per-grid ceiling can be added later as one more `AND` on the
same query and can only narrow what a member may do. Discovering the multiplication after shipping
per-grid means taking away an allowance people already have.

### D-e — The agent's credential is an environment variable the app hands down, never a file the script reads

The app sets `GRID_RELAY_URL` and `GRID_RELAY_TOKEN` in the environment of every agent process it
spawns, alongside the connection variables it already sets there. The scripts read
`os.environ` and nothing else.

Two alternatives were rejected for specific reasons:

- **Reading the vendor variables already present** (`ANTHROPIC_AUTH_TOKEN`, `GRID_APP_API_KEY`) is a
  credential-leak shape, not merely untidy. The app's own process may already carry an `ANTHROPIC_*`
  exported by a developer's shell — this is documented at the spawn site, and the `dropEnvironment`
  parameter exists because of it. A script reading that name can pick up a person's real vendor key
  and post it to a relay.
- **Reading `~/.grid/credentials.toml`** makes a generated Python file the third reader of the
  credential layout *and* of which grid is active — and the second of those is deliberately not in
  that file at all, because the active-grid selection has a single source of truth elsewhere. Two
  hand-duplicated contracts, in a file written by another repo.

⚠️ The pair must be set in **three** places — the Claude Code environment builder, the Codex
environment path, and the Hermes gateway's. Three hand-maintained sites for one contract, with
nothing pinning them together: a missing pair does not fail, it silently removes web access from one
agent. A test that reads all three is part of this decision, not a follow-up to it.

⚠️ The Hermes gateway is long-lived and takes its environment at start, not per turn, so a grid
switch leaves it holding the previous grid's token until it restarts. Claude Code takes a fresh
environment every turn and does not have this problem.

### D-f — Repointing the scripts and taking the vendor's web tools away are two slices, and only the second needs a measurement

Repointing the scripts is a pure improvement: it removes the DuckDuckGo rate limit, `uv`, the
on-demand package fetch and the Chromium download, and it is correct whether or not the vendor's own
web tools exist. It ships alone.

Disabling them is a separate slice and is gated on evidence. `--disallowedTools WebSearch WebFetch`
provably removes both from the request — measured on the wire, both present in the control capture
and both absent in the disallowed one. What is **not** measured is whether the model then reaches
for the guide instead. With the vendor tool gone the path to the web runs through three model
decisions — call the guide, read its body, run the command — and a model that stops at any of them
answers that it has no web access, with a clean log and nothing to alarm on.

The existing probe harness cannot answer this: it hands the model a scripted tool call precisely so
that runs do not depend on the model choosing correctly. Measuring retrieval needs a different
harness, and the tools stay on until it says so.

⚠️ Codex's own off switch fails **open** on a misspelled key: unknown keys are dropped by the
runtime parser, so a typo leaves web search on. `--strict-config` is what closes that and is not the
default. The key that most looks like the switch, `tools.web_search=false`, is accepted and
discarded — exit 0, no effect.

### D-g — Reading a page goes through Exa as well, and `browse.py` is deleted

`read.py` calls `/web/contents`; `browse.py` is removed along with the Chromium download it needed
and the exit code that asked for one, because a JavaScript-built page is what Exa's contents
endpoint is for. Both remaining scripts are then standard-library only and **the web guide stops
needing `uv` at all**.

That last clause is the decision. Keeping a local reader would keep `uv`, keep the package fetch,
and keep the failure mode they produce — and would buy the ability to read a page only the user's
machine can reach, which is what the agent's shell is for and not what a guide called *search and
read the web* is for.

⚠️ The research guide names the same two scripts and takes the same `uv` path. Removing `uv` from
one guide and not the other leaves the second telling an agent to run `uv run --with ddgs` against a
script that no longer needs it and a package no longer used.

### D-h — `type` is fixed at `auto`, and `contents` is not optional

⚠️ A bare Exa search returns **no snippet** — the result carries title, url, published date, author
and images, and text/highlights/summary appear only when `contents` is asked for. Calling it without
`contents` would give the model titles and URLs and nothing to choose between them: strictly worse
than what `ddgs` returns today.

So every search asks for `contents: {highlights: true}`, the vendor's own recommended setting for
agentic use — bare, with no excerpt-length options; the ones that used to size them are deprecated.
Highlights are query-relevant excerpts and cost the same as an equal number of raw leading
characters, because contents is priced per page per content type and not per byte.

⚠️ **The two endpoints nest content options differently, and the vendor calls this the most
important shape difference on its platform**: on search they go inside `contents`, on contents they
are top-level. Reading a page therefore asks for top-level `text`, not `highlights` — and never both,
because stacking content types is billed per type and returns something nobody asked for.

**The request carries the query and a result count, and nothing else.** No category, no domain
allow/deny list, no published-date window, no freshness control. Over-decorating the request is the
vendor's own named "most common integration mistake", and every one of those knobs is a filter no
user story here asks for; source preference belongs in the query the person actually typed. The
result count is the one exception and is deliberate rather than boilerplate: the search script has
taken `--max` since it was written, so the count is an existing product surface and not a parameter
invented for the wire.

That flat price is also why full text is **not** requested inline. It would cost nothing extra at
the vendor and roughly twelve thousand tokens of the agent's context per search, delivered through
the shell's standard output. The binding constraint on this feature is the model's context, not the
Exa invoice, and on a company grid those tokens are real inference spend.

`type` is fixed and not exposed. `instant`, `fast` and `auto` all cost the same, so there is nothing
to choose between them on price; the deep variants cost up to roughly twice as much. After D-c the
operator pays for every call, so an agent free to pick `deep-reasoning` inside an unattended loop is
a cost hole this design would have opened for itself.

### D-i — There is no fleet rate limiter; the vendor's 429 is the limiter

Exa's search endpoint is bounded at ten queries per second across the whole fleet. When it refuses,
the control plane turns the refusal into a sentence the agent can act on, and the guide tells it to
wait once rather than retry hard.

No limiter is built. There is no database-backed limiter or request counter anywhere in the control
plane today, so this would be greenfield; the in-memory sliding window that does exist keeps a
separate window per worker and its own docstring rules it out as an abuse control, naming the
database as where such a thing would have to live. A local limiter would also have to be sized under
a number the vendor owns and can change, and it converts a vendor 429 into a Grid 429 — the same
experience, more code.

The reason this is safe to defer is D-c: the ledger records every call, so if refusals become
common the limiter gets sized from measurements instead of from a guess.

## Consequences

- **Web search requires a grid**, by the same rule as inference. A signed-in user with no grid
  cannot use it. Local mode has no control plane and is out of scope.
- **Rollout order**: control plane, then relay, then the app. In between, a script meets a bare 404
  from a relay that does not serve the route yet, and prints a sentence saying so. There is
  deliberately **no fallback to `ddgs`** — a fallback would restore the `uv` dependency D-g exists to
  remove, and would leave two search backends live with no way to tell which answered.
- **`exa-py` is not used**, in any repo. The scripts never speak to Exa, so they need nothing; and
  the control plane's outbound HTTP is uniformly `requests`, while the SDK would pull in httpx and
  the whole OpenAI client to make two JSON posts.
- **The relay's existing five-second timeout to the control plane is too short for this path** and
  is not reused. A search can take longer than the calls that constant was chosen for.
- **The operator's key breadth is untouched and is not this feature's to fix.** `X-Admin-Key` also
  authorizes crediting any wallet and reading any ledger, and masters hold it because of the same
  denylist D-a amends. Narrowing that means making `/internal/usage` accept a second key first and
  migrating every master — a change on a live billing path, with its own ordering, and it does not
  belong inside a new feature.
