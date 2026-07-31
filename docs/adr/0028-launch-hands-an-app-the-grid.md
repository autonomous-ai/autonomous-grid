---
status: accepted
---

# `grid launch` hands a third-party app the grid, in that app's process and nowhere else

A user can already drive Claude Code against a remote grid, and people do — but the recipe is
folklore. It starts at `grid info --env`, which prints `OPENAI_BASE_URL="{base}/relay/v1"` and
`OPENAI_API_KEY="{access_token}"` (`cli/remote_grid.cmd_remote_info`), and then asks the user to know
four undocumented things: that Claude Code speaks the Anthropic dialect, so the endpoint is the same
base **without** `/v1` (the client appends `/v1/messages` itself); that the same token goes into both
`ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_API_KEY`; that Claude Code resolves a model per *tier*, so
seven more variables must be set or it asks the grid for a model named `claude-opus-…` that no engine
serves; and that all of it must be exported into the shell before the binary starts.

Nothing about that chain is discoverable, and every step of it is a place to get one string wrong and
receive an authentication or model error that names none of the actual causes.

## Decision

> **`grid launch <target>` starts a third-party app already pointed at the active grid. The endpoint,
> the credential and the model names are set in the *child process environment only* — never exported
> to the user's shell, never written to a config file, never to `os.environ` of the CLI itself.**

The verb is new and the noun is new: a **launch target** is an app `grid launch` starts for the user.
`claude` (Claude Code) is the first and, in this slice, the only one.

The scope is deliberately "hand over and get out of the way". `grid launch` does not configure the
app, does not own its settings, and leaves nothing behind — so there is no `--restore` to undo, and
closing the app is the whole cleanup.

### The env block

| variable | value |
|---|---|
| `ANTHROPIC_BASE_URL` | `resolve_relay_base(...)` + `/relay` — **no** `/v1` |
| `ANTHROPIC_AUTH_TOKEN` (~~`ANTHROPIC_API_KEY`~~ — see below) | the grid's `access_token` |
| ~~`ANTHROPIC_MODEL`, `ANTHROPIC_SMALL_FAST_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL`~~ | ~~the tier table~~ |
| ~~`ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU,FABLE}_MODEL`~~ | ~~the tier table~~ |

The tier table is **one hardcoded constant** in this slice, not discovered from the grid. It is the
single place a later slice replaces with discovery, and it is the reason the preflight below exists.

> ### Amended — the seven model variables are no longer set
>
> The env block is now **three keys**: the base URL and the two token variables. The tier table, the
> required/optional split, the remap, and the preflight that checked those names are all gone.
>
> The decision above was made to stop Claude Code asking a grid for a real Anthropic model name no
> grid serves. It bought that by making this command decide which model a user's session runs on —
> a choice it has no standing to make, and one that silently overrode the user's own `settings.json`,
> their `/model` default and any `ANTHROPIC_MODEL` already in their shell. Handing an app the grid is
> the feature; choosing their model for them was never part of it.
>
> **What this costs, stated plainly:** a model the grid does not serve now fails at the user's first
> prompt instead of at launch, and Grid cannot warn about it in advance, because it no longer knows
> what the app is going to ask for. That is the trade — the launcher stops being wrong about the
> model, and stops being able to be right about it.
>
> Preflight survives only as the one model fact that holds whatever the app asks: a grid serving
> nothing can serve nothing, so an empty grid is still refused before the app starts. Everything else
> in this ADR — the child-process-only environment, the base URL with no `/v1`, the rejected
> alternatives, `--print-env`'s carve-out — stands unchanged.
>
> ### Amended again — only `ANTHROPIC_AUTH_TOKEN`, never both
>
> The premise above ("both, because which one Claude Code reads depends on the path it takes to
> authenticate") is **false**, and the app is the one that says so. With both set it opens with
>
> > `Both ANTHROPIC_AUTH_TOKEN and ANTHROPIC_API_KEY set · auth may not work as expected`
>
> and instructs the user to unset one. Observed on Claude Code 2.1.220, on the first real
> `grid launch claude` after v0.3.11 shipped.
>
> Nothing was bought for that warning. grid-src's `_bearer_or_api_key` accepts either header but
> **prefers `Authorization: Bearer` whenever both arrive** — which is precisely what
> `ANTHROPIC_AUTH_TOKEN` produces — so `ANTHROPIC_API_KEY` never decided a single request. Its own
> comment names the reason to prefer Bearer: when both are present, `x-api-key` *"likely holds the
> caller's real Anthropic key, not the grid token"*. So this ADR was setting a variable the server
> deliberately ignores, into the slot a user's own credential occupies. The env block is two keys.

## Considered options

**`grid agent run claude`, rejected.** `agent` already names something else here — hermes and codex,
the agents the CLI *itself* drives to run tools in `grid chat` (`cli/agent.py`). A launch target is
the mirror image: the user drives it and the CLI only mints its environment. Overloading the word
would also have produced `grid agent install codex` and `grid agent run codex` side by side, meaning
different things, while `codex` is *already* a third thing — an API engine kind
([ADR 0015](./0015-codex-subscription-engine.md)).

**Local mode, rejected for now.** `local/server.py` serves `/v1/chat/completions`, `/v1/completions`,
`/v1/models` and media — there is no `/v1/messages`, and Claude Code speaks nothing else. Adding it
would mean carrying grid-src's Anthropic↔OpenAI translation (its own module, including a
stream-rebuilding state machine) into this repo across a seam that has no code dependency, which is
exactly the class of hand-duplication that compiles, passes every test, and diverges at runtime. So
in local mode the command refuses, and it says *why* — the dialect — rather than "not supported".

**An isolated config dir (`CLAUDE_CONFIG_DIR`), rejected.** It would keep grid sessions clear of the
user's Anthropic account, at the price of handing them a Claude Code with none of their MCP servers,
skills, permission allowlist or history. The value of the app is mostly in that accumulated
configuration; a launch that strips it would send users straight back to exporting by hand.

**A pinned, SHA-256-verified binary in `~/.grid/bin`, rejected** — the shape
`shared/agent/codex_installer` uses. It fits Codex because Codex is a static asset on a GitHub
release. Claude Code manages its own versions, so pinning would make this repo the owner of a number
it does not control and would ship users a stale agent. `grid launch` instead offers to run the
vendor's own installer, and only when attached to a TTY.

**`auto` as the default model, rejected.** The grid's own router would need no configuration at all,
but Claude Code issues many small requests per turn, so an advisor hop lands on all of them, and the
model can differ turn to turn — which makes "it got worse today" unfalsifiable. `auto` stays
available as an explicit model name; it is not the default.

**An authenticated preflight, rejected.** `GET /relay/v1/models` requires `inference:models` and
would prove the token *and* list the models in one call. The public
`GET /relay/v1/grid/overview` was chosen instead because `cli/remote_overview._fetch_overview`
already reads it. See the consequence below.

## Consequences

- **Preflight proves model presence, not credential validity.** The overview route is public and
  ignores the Bearer token. A locally-absent token is still caught (the same check and the same
  "run `grid login`" message `info --env` uses), but an *expired* one passes preflight and fails
  inside the app. Accepted knowingly; the authenticated route above is the fix if it bites.
  **It bit — [ADR 0029](./0029-the-credential-is-checked-before-it-is-handed-over.md) takes that fix,
  and adds the offline expiry check and the in-place refresh the authenticated route alone cannot
  give.**
- **`settings.json` outranks us.** Claude Code's user settings can carry an `env` block whose values
  override a shell export. A user with `ANTHROPIC_BASE_URL` there silently defeats `grid launch`, so
  the command reads that file and warns — it does not edit it.
- **`--print-env` is the second deliberate exception** to the rule that no command prints a token
  ([ADR 0003](./0003-remote-grid-lifecycle.md) §6). It carries the same justification as
  `info --env`: an explicit, user-requested disclosure of the caller's own token to the caller's own
  shell. Every other path stays token-free.
- ~~**A missing tier model fails at launch, loudly, not at request time.** With the tier table
  hardcoded, a grid that does not serve it is a launch-time refusal naming what is missing and what
  the grid does serve. The two tiers every session uses are required; the `/model`-only tiers fall
  back to the main model rather than blocking the launch or being left unset — unset would send
  Claude Code asking for a real Anthropic model name that no grid serves.~~
  **Reversed by the amendment above.** No model variable is set, so a model the grid does not serve
  fails at the first request — the launcher no longer knows what will be asked for. Only an empty
  grid is still a launch-time refusal.
- **`--local` / `--remote` are stripped from anywhere in argv** by `cli.dispatch.resolve_override`,
  including after the `--` separator, so those two exact tokens cannot be passed through to the
  launched app.
