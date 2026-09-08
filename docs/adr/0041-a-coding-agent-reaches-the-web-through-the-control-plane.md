---
status: proposed
---

# A coding agent reaches the web through the control plane, and the relay is not in the path

ADR 0036 put a web search behind the control plane and gave the agents *a grid drives* two scripts
to run under their Bash tool. That serves the app, which spawns those agents itself and can set
`GRID_RELAY_URL` + `GRID_RELAY_TOKEN` in their environment.

It serves nothing else. The coding agents people actually run — Claude Code, Codex, opencode — are
started by the person, not by the app, so nothing puts that pair in their environment; and they do
not discover capability by reading a guide and shelling out. They discover it over **MCP**. So the
grid's web access, which is already paid for and already metered, is invisible to every harness
outside the app.

The obvious implementation is to point an MCP server at the relay's `POST {RELAY_PATH}/web/search`,
and it is wrong in three ways that only show up later:

- **A relay is per-grid and is a machine that can be down.** An MCP server's URL goes into a config
  file and stays there. Pointing it at one grid's relay makes web search stop working when that grid
  stops — for a capability whose answers do not depend on the grid at all.
- **The relay verifies, but it cannot be the thing a harness talks to.** It answers on the grid's own
  address, which for a self-hosted grid is a LAN address the harness may not be able to reach.
- **It buys nothing.** The relay's stated reason for standing in the path (0036 D-b) is *accounting* —
  making a search attributable to a grid. That attribution survives without it: `network_id` is a
  claim inside the per-grid access token, so the control plane can read it from the credential the
  caller already presents.

## Decision

### D-a — The MCP server is the control plane's, and this deliberately overturns 0036 D-b for this caller

`POST /v1/grid/web-mcp` is mounted in the control plane. The relay is **not** in the path.

⚠️ **0036 D-b argues the opposite, and it is still right about the caller it was written for.** Its
two reasons were attribution and *"per-grid policy has somewhere to live the day it is wanted"*. The
first is kept — `network_id` rides the token. The second is genuinely **given up on this path**: a
policy keyed on a grid would have to be enforced in the control plane against the token's claim
rather than by the grid's own relay. That is the price, it is paid knowingly, and it buys a
capability that works when the grid is asleep and from a harness that cannot reach a LAN address.

Anyone reading 0036 alone will see an inconsistency and be tempted to route this through the relay
"for consistency". That change would re-introduce every problem in the section above. This decision
is the reason not to make it.

### D-b — The credential is the per-grid access token, verified in FULL — and `verify_access_token` is not full

The caller presents `Authorization: Bearer <per-grid access token>` — the RS256 / 365-day token in
`~/.grid/credentials.toml`, the same one the app hands its own agents. Not the session token: it is
24 hours, which would mean re-pasting a config every day.

⚠️ **`tokens.verify_access_token` checks signature, `iss`, `aud`, `exp` and scope, and nothing else.**
It does not read the member row, and it does not compare `member_epoch` or `network_epoch`. In the
control plane it has exactly one existing caller (the sync snapshot). The checks it is missing are
not missing by accident — they are the **relay's**, at `grid_auth.py`, and taking the relay out of
the path takes them out with it.

⚠️ **The missing checks must NOT be copied from the relay.** The relay's are network-type-dependent
policy — `_requires_allowlist`, `_is_open_consumer`, `DENYLIST_NETWORK_TYPES` — three functions whose
own comments say that copying one into the other is the failure they exist to prevent. Duplicating
that trio into the control plane would create a brand-new hand-maintained policy surface between two
repositories, for a decision the control plane already owns.

Instead this path asks the control plane's **own** authority: `store.member_for_access(network_id,
email, google_sub)` — the single function every one of the four `issue_access_token` call sites
consults to decide whether this account may hold a token on this grid *at all*. It already handles
network status, `member.status`, the owner-without-an-active-row arm, permissionless access,
domain-restricted admission, and the denylist with the correct per-type cohort. `None` means refuse.

So the check is: verify the signature → `member_for_access` → compare `member_epoch` and
`network_epoch`, `<` and never `!=`. Nothing about it is a copy.

⚠️ **`member_for_access` alone is NOT equivalent to the rule that minted the token, and this
decision originally said it was.** That sentence was wrong, and it was wrong in production: the
function takes an `os_token` — the operating system a CLI claimed at sign-in — and on an
`os-community` grid that argument is *the only thing* that admits anybody (ADR 0039 D-e).
Membership there is not row-backed. Nothing stores the OS, no claim carries it, and an MCP caller
is a harness that never had one, so the call answers `None` for **every OS grid** — which is the
commonest kind, and includes the grid this was first tried on. Web tools were dead for all of them
while every test passed.

Where `member_for_access` answers `None`, the fallback is the control plane's own half of the
cohort rule, `store.denylist_governs` — whose docstring already names grid-src's
`_is_open_consumer` as the other half and says the two must agree. For a cohort the denylist
governs, *being un-denied is the membership test*, which is exactly what the relay does with these
tokens. Nothing is relaxed elsewhere: on every other type `member_for_access` returns `None` only
when it means to refuse, and the denial check refuses those again. A member with no row also has no
`member_epoch` to compare, so the network epoch is that cohort's only revocation lever — the same
division grid-src makes.

⚠️ Two things made this escape. The obvious one: no test used `os-community`. The subtler one, and
the reason the first fix's tests *also* proved nothing — a test grid created through the API is
**owned** by its member, and `member_for_access` has an owner arm that admits from the network row
alone. A precondition asserting `member_for_access(..., google_sub=None) is None` passed while the
gate, which passes the real `google_sub`, took the owner arm. The fixture must hold a token for
somebody who is **not** the owner, and its precondition must be asserted with the argument the gate
actually uses.

Without it, somebody removed from every grid keeps a working credential for **365 days**. The harm
is bounded — a web search is free to the member and the allowance is keyed on the account, so they
spend only their own — but it is the operator's vendor bill, and nothing anywhere reports it.
⚠️ Removal already bumps `member_epoch` (`store.remove_member`), so the epoch comparison catches it
even where the row survives as `inactive`; the two checks overlap on purpose.

**No scope is required.** A web search is free to the member (0036 D-c), so every role including
`consumer` may make one. A new `web:search` scope was rejected outright: `role_scopes()` would have
to change and **every token in circulation lacks it**, so the day it shipped every existing user
would be refused with a valid token — a breaking change that verifies, then fails.

### D-c — One gate, at the ASGI layer, and its 401 body is text a person reads

The bearer gate refuses before MCP sees the request. **It is the only credential check, and a
second one inside each tool was considered and dropped**: the server is stateless, so every
`tools/call` carries its own header and runs the gate again — an in-tool re-verification would
re-derive, from the same bytes in the same request, an answer the gate reached microseconds earlier.
That is not defence in depth, it is a second copy of the rule for the next person to fix only one of.

What the tools *do* check is that a verified caller is present at all (`current_caller`). That fires
only when the gate is not in front of them — a wiring fault — and it raises rather than falling back
to an anonymous identity, because the fallback would go on to write a ledger row belonging to nobody.

So the 401 body carries the whole of the user-facing message. Measured: Claude Code reports
*"Server rejected the configured Authorization header (HTTP 401)"* **and prints the server's own JSON
body alongside it**. That makes the body the one place a person is told what to do, so it names
`grid mcp config <grid>` rather than the `{"error": "unauthorized"}` the analytics server answers a
trusted bot with.

⚠️ **The body must never say *why*.** "You were removed from that grid" and "your token expired" are
deliberately the same sentence: anyone holding any string can read this reply, and a refusal that
distinguishes them is an oracle for which credentials are real.

Identity travels from the gate to the tools through a `contextvar`, not an SDK request object: the
tool functions never see a request, and a `contextvar` is the one mechanism that cannot break under
an SDK upgrade. ⚠️ If it breaks, a search still returns results and only the **ledger** is wrong —
so the test for it asserts on the row, never on the reply.

### D-d — Every harness takes a literal `Authorization` header; the token never goes in an environment variable

Measured on the wire 2026-09-07 — Claude Code 2.1.263 and Codex 0.144.6 — against a header-logging
listener, not read off vendor documentation:

| harness | where the header goes |
|---|---|
| Claude Code | `claude mcp add --transport http … --header "Authorization: Bearer <tok>"` → `mcpServers.<n>` in `~/.claude.json` |
| Codex | `http_headers = { Authorization = "Bearer <tok>" }` under `[mcp_servers.<n>]` |
| GitHub Copilot CLI | `copilot mcp add --transport http --header "Authorization: Bearer <tok>" <n> <url>` → `mcpServers.<n>` in `~/.copilot/mcp-config.json` |
| opencode | `"headers": {"Authorization": "Bearer <tok>"}`, `type: "remote"`; its `mcp add --header` takes ⚠️ `KEY=VALUE`, not `Key: value` |
| Hermes | `headers: {Authorization: "Bearer <tok>"}` under `mcp_servers.<n>` in `~/.hermes/config.yaml` |
| pi | ⚠️ **nowhere — pi ships no MCP client at all**, by design ("No MCP." in its own README) |

Extended 2026-09-08 to the three harnesses above, the same way and to the same standard: each binary
run against a throwaway home so the file *it* writes could be read back, then a header-logging
listener with a positive control proving what actually goes on the wire (Copilot 1.0.83, opencode
1.18.29, Hermes 0.21.1 all send the literal header; Codex 0.153.4 still has no `--header`). The
Hermes and opencode configs were then pointed at the **live** control plane with a real per-grid
token: both connect and discover both tools.

⚠️ **The server name keeps its hyphen everywhere, Codex included.** `codex mcp add` writes
`[mcp_servers.grid-web]` itself, so a hyphen is a legal TOML bare key here; spelling it `grid_web`
for Codex alone — which this command did until it was measured — renames the tools for that one
harness while every other harness sees `grid-web`.

⚠️ **Hermes's block is YAML, so its indentation is part of the document.** TOML and JSON ignore
leading whitespace and had been printed indented for readability; a `mcp_servers:` printed the same
way is a block that cannot be pasted. File blocks now start at column zero and only shell commands
are indented.

**pi is a listed target that refuses.** Leaving it out of `--harness`'s choices would answer
argparse's "invalid choice", which reads as an oversight in this command rather than a fact about
pi; and the refusal is decided **before** a grid is resolved, so a signed-out machine is told about
pi rather than told to run `grid login` for a config that would not exist either way.

⚠️ **Codex's `mcp add` flags hide the field that works.** It offers only `--bearer-token-env-var`
and the OAuth flags; there is no `--header`. `http_headers` is nonetheless a first-class field —
`codex mcp get` displays it and *masks the value*, which it never does for a key it does not know —
and it is sent on every request. `codex mcp add` simply cannot write it, so `grid mcp config` prints
a TOML block for Codex rather than a command.

An environment variable was the first plan and is now explicitly rejected. `env_http_headers` and
`bearer_token_env_var` both work, but a long-lived credential in a shell environment is readable by
every child process of that shell — the hazard the app's own `dropEnvironment` exists to prevent —
and it buys nothing once a literal header is available everywhere.

⚠️ Two traps for anyone who does reach for the env-var forms: `env_http_headers` holds the **whole
header value** (`"Bearer <tok>"`) while `bearer_token_env_var` holds a **bare token**, and
`bearer_token_env_var` sends no header at all during OAuth discovery. Confusing them is a silent 401.
⚠️ And `codex mcp` **refuses** `--strict-config`; on other subcommands it does not validate keys
inside `mcp_servers` at all, so a misspelled field there is dropped in silence.

The whole loop was then run against a locally-hosted control plane with a genuinely minted RS256
per-grid token, rather than asserted: `grid mcp config`'s **printed** Claude Code command, executed
verbatim, reports `✔ Connected`; its **printed** Codex block, pasted verbatim, is read back with the
header masked and reaches the server with **no 401** (a 406 on the discovery `GET`, which is content
negotiation and proves the credential arrived); both tools answer, and the ledger rows land under the
caller's own `google_sub` and `network_id`. A wrong token is reported by Claude Code with this
server's own `detail` sentence printed to the user — which is the whole of D-c, observed rather than
assumed.

### D-e — Its own mount, its own flag, and it never touches the analytics server

`/v1/grid/web-mcp` is a second `FastMCP` instance with its own `GRID_WEB_MCP_ENABLED`, mounted
beside `/v1/grid/mcp` and sharing no code path with it beyond the shape.

They must not share a switch. The analytics server's tools run read-only SQL against the
control-plane database and fan out over **every** network's database; it is gated by a single shared
secret because its caller is a trusted internal bot. Web tools are for end users. One flag turning
both on is one operator mistake away from exposing the first to the second's audience.

The transport settings are copied verbatim because they are load-bearing and already proven in
production: `stateless_http=True`, `json_response=True` (no long-lived SSE stream for Cloudflare to
idle-close) and DNS-rebinding protection off (the app sees the proxied `Host`). Measured against the
dev control plane through Cloudflare: `initialize` answers **200 in 72 ms**, and `tools/list`
succeeds immediately afterwards **with no session id**, which is what stateless means in practice.

⚠️ A mount does **not** appear in `openapi.json`. The house recipe for proving a deploy — grepping
the OpenAPI paths — silently proves nothing here; probe the endpoint itself.
⚠️ A request to `/v1/grid/web-mcp` without the trailing slash answers **307**. Clients that follow
it are fine (Claude Code does, measured), but every URL this repo prints carries the slash.

### D-f — Two tools, and the read takes a list

`web_search(query, num_results)` and `web_read(urls, max_chars)` — one per existing control-plane
route, so `web_search.search()` / `contents()`, `_allowance_refusal` and `_record_web_search` are
reused unchanged and no new vendor code is written. Exa's other endpoints (`find_similar`, `answer`,
`research`) are each new vendor code, new cost parsing and, for `research`, a different cost model
entirely; they are a separate feature, not a bigger tool list.

`web_read` keeps the wire's list rather than the app script's single URL. **The allowance counts
ledger rows, not pages**, so reading five pages in one call costs one unit where five calls cost
five — and comparing sources is the ordinary case for a coding agent.

Each page's text is truncated at `max_chars`, default **6000**, matching the app's `read.py`. A tool
result goes straight into the model's context, where a script's stdout did not.

### D-g — A per-account rate limit, because taking the relay out took one away

A `SlidingWindowLimiter` keyed on `google_sub`. The daily allowance bounds the *day*; nothing bounds
a minute, and Exa's `/search` is capped at ten queries per second across the whole fleet — so one
client in a retry loop makes every other user's search fail. The relay was absorbing that shape.
Per-process and therefore `limit × workers`, which is right for a stuck client and, as
`ratelimit.py` says of itself, would not be right as an abuse control.

### D-h — `grid mcp config` renews the credential it is about to print, and only then

The command used to be pure local work: read the store, print. That made D-c's 401 body — *"This
grid credential was refused. Run `grid mcp config <grid>` to get a fresh one"* — untrue in the case
it exists for. A refused credential is usually an expired one, and re-running a command that reprints
the same expired token from the same file is a loop, not a remedy.

So the offline half of ADR 0029's credential check is reused here: read `exp` out of the token, and
if it is inside the margin exchange the stored refresh credential for a fresh 365-day one, persisting
**both** halves (⚠️ the control plane rotates the refresh token on every exchange; storing the access
token alone destroys this machine's ability to renew anything again — silently, and not until some
later day). One implementation, one place, in `cli/grid_credential.py`.

**The margin is 30 days, not the launch path's 24 hours.** They protect different deadlines. A launch
protects a work session starting now, so a day of headroom is generous; what this command prints is
pasted into a config file and left there for months, and a token with 25 hours left passes a one-day
margin and hands somebody a harness that dies tomorrow. Thirty days is 8% of the token's life, so it
cannot cause a renewal that would not have happened within the month anyway.

⚠️ **This is the one place that must be LESS forgiving than `grid launch`.** That command warns about
an unrepairable token and lets its relay probe decide, because the probe is authoritative and a wrong
local clock must not cost a launch that would have worked. There is no probe here — the web-tools
server is the control plane's, not the grid's — so an expired token with no refresh credential is
**refused** rather than printed: the alternative writes a certainly-dead credential into a harness's
config file, where the 401 that follows tells the user to re-run the command that gave it to them.
A renewal that *fails* on a token which still works stays a warning on stderr, for the opposite
reason: nothing was learned, and a control-plane hiccup must not cost somebody a config.

**It reaches the network only when it is about to print the credential.** With no `--harness` the
command lists the harnesses and prints no token, so it needs no fresh one — and an arg-less
invocation must not rotate this machine's refresh credential to draw a menu. That listing view also
exists so the command people run first, often on a shared screen, does not put a live 365-day
credential on it to answer a question about which harnesses are supported. `grid mcp token`, which
existed only to print that credential with nothing else around it, is **removed**; `--json` carries
the same three values in a shape a script can read, and one command fewer can drift from the other.
