# Grid CLI

Grid should feel like the command line for a local AI grid: bring one endpoint online,
join the engines you already run, see what models are live, and use them immediately.
Internal protocol words stay out of the user-facing CLI.

## Vocabulary

```
grid      a named local AI endpoint, usually `home` or `work`
grid_url  the URL engines join; apps call it through `/v1`
engine    a running instance joined to a grid: Ollama, LM Studio, vLLM, MLX, llama.cpp, ComfyUI
kind      an engine's type (ollama, vllm, mlx, llama.cpp, comfyui) — filter auto-detect with --kind
join      connect this machine or engine to a grid
model     a live capability exposed by joined engines
mode      which world the CLI targets: `local` (default) or `remote`
```

Do not use `provider`, `consumer`, or `signaling` in CLI output or first-run docs — with one
exception: `consumer` / `provider` / `both` are the sanctioned **role** values on the remote-only
`grid members` command (a member's permission label, not the engine/app it names). Avoid `network`
as a product noun. Those are implementation terms for architecture docs and code.

## Design Rules

- The common path is one screen: `grid up`, `grid join`, `grid models`, `grid chat`.
- `home` is the default grid. Users name a grid only when they have several.
- `up` is idempotent: create if missing, start if stopped, print the same contract every time.
- Default output is human-readable. Every state-reading command supports `--json`.
- Use examples before exhaustive flags in help text.
- `--name` names an engine; `[grid]` names a grid.
- `--at <url>` always means an existing engine endpoint.
- `--serve <model>` always means Grid starts its default text engine, then joins it.
- `--media` always means Grid starts or joins the default media engine.

## Top Level

```
grid                                  # overview: mode, active grid, endpoint, engines, models, next steps
grid --help                           # concise help with common examples first
grid <command> --help
grid version
grid [--local | --remote] <command>      # override the active mode for one command
```

Bare `grid` is not just help. It is the dashboard for a terminal:

```text
mode: local
Grid: home
grid_url: http://192.168.1.25:8090
engines: 3 live
models: qwen36-27b-mtp, gemma4-31b, devstral-small-2

Next:
  grid join
  grid chat -m qwen36-27b-mtp "hello"
  grid info --env
```

If no grid exists yet, bare `grid` should show the shortest successful path:

```text
mode: local

No grid yet.

Start one:
  grid up

Then join an engine:
  grid join
```

In `remote` mode bare `grid` shows the mode and your active remote grid, then the next steps:

```text
mode: remote
active grid: research

Manage your remote grids with `grid up` / `ls` / `info`, serve models with `grid join`,
and use them with `grid chat -m <model> "…"`.
```

(Signed in but no grid selected yet, it prints `active grid: (none)` — run `grid ls` then
`grid use <name>`.)

## Modes

Grid runs in one of two modes. **`local`** (the default) is everything documented here: an
unauthenticated in-memory grid on your local network. **`remote`** is a signed-in thin client to
autonomous's hosted relay: sign in with `grid login`, then bring up and manage hosted **remote
grids** with the same `up`/`down`/`ls`/`info` verbs, serve them (`join`/`leave`), consume them
(`chat`/`image`/`edit`/`video`), price your served models (`grid price`), and manage who may join or
use them (`grid members`).

```
grid mode                             # print the current mode
grid mode local | remote                 # switch and persist the mode
grid use [name] [--json]              # show or set the active grid for the current mode
grid use --none                       # clear the active grid for the current mode
grid [--local | --remote] <command>      # override the mode for a single command
```

The mode is persisted in `~/.grid/state.json` (default `local`); each mode remembers its own
active grid. Which mode a command runs in is resolved as `--local`/`--remote` (one command) > the
persisted mode > `local`. `grid use <name>` sets the persistent default grid, so `grid chat` /
`grid info` / `grid models` target it without naming it — an explicit `[grid]` positional still
wins, and a stale selection (its grid was removed) is ignored.

In `remote` mode the grid lifecycle (`up`/`down`/`ls`/`info`), live reads (`engines`/`models`),
sign-in (`login`/`logout`), serving (`join`/`leave`), consuming (`chat`/`image`/`edit`/`video`), and
membership admin (`grid members`) all work. `grid members` is remote-only — in `local` mode it exits with guidance to switch. The shared
local commands (`catalog`, `pull`, `rm`, `engine …`) work in either mode. A machine with no state
file behaves exactly as a `local`-only install.

Notes:
- `--json` goes after the subcommand (`grid info --json`); bare `grid --json` prints the
  overview as JSON, including a `mode` key.
- `--local`/`--remote` may appear anywhere on the line, but are not listed in per-command `--help`.

## Sign in

```
grid login [--no-browser] [--json]    # sign in to remote mode (device-code flow)
grid logout [--json]                  # clear stored remote credentials
grid sync [--json]                    # refresh your remote grids without signing in again
```

**Remote-only.** `grid login` signs you in to autonomous's hosted relay with a device-code
flow — it opens a browser, or with `--no-browser` prints the URL and code to enter on another
device (for headless machines) — and stores your credentials under `~/.grid`. Signing in does
**not** pick an active grid: run `grid ls` to see the remote grids you can reach, then
`grid use <name>` (or name one per command). `grid logout` clears the stored credentials.
`grid sync` re-fetches your grids and tokens using your saved sign-in (no browser), so a grid
created on the website or one you were just added to appears after `grid sync` — it never changes
your active grid, and an expired session tells you to run `grid login`. In `local` mode these
commands exit with guidance to switch — sign-in is a remote concept. See
[ADR 0002](./adr/0002-remote-sign-in.md).

## Grid Lifecycle

```
grid up [name] [--type <t>]           # create/start a grid by name or id (--type: remote grid type on create)
grid down [name]                      # stop a grid; the grid/config persists
grid ls [--json]                      # list saved grids (name, id, where, url)
grid info [grid] [--json]             # endpoint, key, engines, live models
grid info [grid] --env                # print OPENAI_* exports (local key, or remote relay URL + token)
```

`grid up` output is stable and scriptable:

```text
grid=home
grid_url=http://192.168.1.25:8090
```

No separate `create` or `start` in the main surface — `up` is the single lifecycle verb, so
first use feels like one operation rather than infrastructure management. (`grid use` only sets
which grid is *active*; it is a selection pointer, not a lifecycle step — see Modes.)

In `remote` mode these same verbs act on hosted **remote grids**: `grid up <name>` create-or-starts
one — `--type` is `permissioned-public` (default) or `permissioned-providers`, set on create, and
creating needs an explicit name (no auto-`home`). `grid down` stops it (the grid persists),
`grid ls` lists the grids your sign-in fetched (local — no network call), and `grid info` shows a
grid's `status` and `grid_url`. `grid info --env` prints the grid's relay base URL plus your access
token so any OpenAI SDK can call it (the relay address is read live from the grid, so it must be up).
See [ADR 0003](./adr/0003-remote-grid-lifecycle.md).

## Engines

```
grid join [grid]                                      # auto-detect local engines
grid join [grid] --all                                # join every detected engine
grid join [grid] --at <url> -m <model>... [--name <id>]
grid join [grid] --serve <model> [--name <id>]
grid join [grid] --media [--bundle <bundle>]... [--name <id>]
grid join [grid] --api <kind> [-m <model>...]         # remote only: join a third-party API engine (v1: openai)
grid leave [grid] [--engine <sel>] [--all]            # <sel>: engine id, endpoint URL, served model, or :port fragment
grid engine ls [grid] [--json]                        # live engines joined to a grid (legacy alias: grid engines)
```

`grid join` with no flags should detect local engines in this order:

1. Ollama
2. LM Studio
3. vLLM
4. MLX
5. llama.cpp
6. ComfyUI

When detection finds more than one engine, print the plan and ask for confirmation in
interactive terminals. In non-interactive mode, require `--all`, `--kind <kind>`, or
explicit `--at`.

Example detection output:

```text
Detected engines on this machine:

  mlx          http://192.168.1.10:8080/v1        gemma4-31b
  vllm         http://192.168.1.20:8000/v1        devstral-small-2

Join them:
  grid join --all
  grid join --kind <kind>
```

Engine IDs are local names shown by `grid engine ls`, `grid info`, and `grid models --verbose`.
`grid leave --engine <sel>` takes an exact engine id, or — tried in that order — an endpoint URL,
a served model, or a URL fragment such as `:8000`.

### `grid join` in remote mode

In remote mode the same verb serves your models on a remote grid: it brings the engine up the same
way, then runs a detached loop that registers the engine's capabilities with the hosted relay,
long-polls it for work, forwards each claimed job to the local engine, and heartbeats — `grid
leave` stops and unregisters it. You must be signed in and the grid must be up (`grid up`). `grid
join --all` serves several detected engines under **one** identity: it advertises the union of their
models and routes each job to the engine that serves the requested model (first-detected wins when
two engines share a model name).

`grid join --media [--bundle <bundle>]...` serves this box's built-in media (ComfyUI) engine to the
relay — media-only, or alongside a text engine (`--serve`/`--at` + `--media`). The serve loop brings
up ComfyUI + the media server, registers the `comfyui:*` workflows the host's VRAM gates in, and the
relay forwards `media/*` jobs to the media server on loopback; the SSE (progress + base64 result
files) streams back exactly as in local mode.

`grid join --api <kind> [-m <model>...]` (v1: `openai`) joins an **API engine** — a third-party LLM
API service served through your own key ([ADR 0012](./adr/0012-api-engines.md)). The key is
resolved in order: the `OPENAI_API_KEY` env var, else the machine-local key store, else a hidden
interactive prompt (there is deliberately no `--api-key` flag; non-interactive with no key anywhere
is a clear error). It is validated at join time against the vendor's model listing — an invalid key
is a terminal error and nothing is spawned or stored. A validated key is saved to
`~/.grid/api_keys.toml` (`0o600`, keyed by service kind): later joins and the detached serve
process read it from there, and `grid logout` leaves it intact (it belongs to your vendor account,
not your grid sign-in). Re-joining with a new env value overwrites the stored key and restarts the
engine — **rotation is one command**. A bare `grid join` (auto-detect) never joins an API engine
just because a key file exists; `--api` is always explicit.

With no `-m` the join serves the **whole whitelist ∩ the models your key can see** (skipped
whitelist models are reported; an empty intersection errors). `-m` narrows to whitelisted
`openai:*` models (`grid catalog --api openai` shows them); a whitelisted model your key can't see
is skipped with a note, and a name outside the whitelist errors listing the valid names. The serve
loop registers the models with their **static** whitelist capabilities — the vendor is never probed
or benchmarked — and forwards each `chat/completions` job to the vendor with your key, rewriting
the advertised `openai:<name>` to the vendor's `<name>`; SSE streams pass through unchanged. An API
engine serves `chat/completions` **only** — a legacy `completions` job gets a structured "not
served" error and is never forwarded. A vendor error (401/429/5xx) surfaces as that job's error
with the upstream status, never touching your grid sign-in and never unregistering the engine; an
auth/quota failure (401/403/429) additionally warns in the engine's log so a revoked key or
exhausted quota is visible to you, not just to consumers. **Requests to `openai:*` models leave the
grid for the vendor**, under your key and your own OpenAI account's terms. `--api` is mutually
exclusive with `--at`/`--serve`/`--advertise-as`/`--media`/`--bundle` in one invocation — join
other engines with a separate (additive) `grid join`.

An API engine merges into your grid's **one serving identity** exactly like a hardware engine.
`grid join --api openai` onto an identity already serving other engines appends to the union and
**hot-reloads in place** — no restart, no dropped in-flight requests (the vendor key is re-read from
the key store on reload, so the appended engine forwards with auth immediately). `grid leave
--engine openai` drops just the API engine and re-advertises the survivors; removing the last engine
tears the identity down. To **narrow** an already-served set, `grid leave --engine openai` then
re-join with the `-m` subset you want — a join only ever adds models to the union, never removes them.

The `grid join` flag set is the union of both modes, gated by mode:

- **Both modes:** `--at` / `--serve` / `-m,--model` / `--kind <kind>` (alias `--engine`) / `--name`
  / `--all`, `--advertise-as` (or inline `-m real=pub`), `--endpoint-port` (alias `--llama-port`),
  the llama tuning flags (`--ctx-size --n-predict --parallel --flash-attn --temp --reasoning-budget`),
  and the media flags `--media` / `--bundle <bundle>` / `--comfyui-port` / `--media-port`.
- **local-only:** `--advertise-host` (a remote engine polls the relay outbound — there is no inbound
  endpoint to advertise).
- **Remote-only:** `--api <kind>` (join a third-party API engine; `-m` optionally narrows the
  whitelist, omitted = every whitelisted model the key can see), and `--max-concurrency` (how many
  requests this engine serves at once; the provider runs one poll worker per slot — default 1, or
  8 when the identity serves only API engines).
- **Deprecated:** `--engine-label` — the grid page now derives the engine kind automatically, so it is
  accepted but inert (still matched by `grid leave --engine <label>`); `--pricing-input` /
  `--pricing-output` — kept so old invocations don't hard-error, but they no longer advertise a price.
  Set your authoritative per-model price with `grid price set` (see [Price](#price)) instead.

A flag used in the wrong mode fails with a clear message. (`--advertise-as` is single-engine only and
is rejected with `--all`.) See [ADR 0004](./adr/0004-remote-provider-serve.md),
[ADR 0007](./adr/0007-remote-multi-engine-routing.md), and
[ADR 0008](./adr/0008-remote-media-serve.md).

## Models

```
grid models [grid] [--verbose] [--json] # live models the grid can run now
grid catalog [--json]                   # models Grid can pull
grid catalog --api <kind> [--json]      # API-engine whitelist for a service kind (v1: openai)
grid pull <model>                       # pull a model for the default text engine
grid rm <model> [--yes]                 # remove a pulled model
```

`grid models` answers the orchestration question: what can this grid run right now?

Default:

```text
qwen36-27b-mtp
gemma4-31b
glm-4.5-air
devstral-small-2
comfyui:image_generation
```

Examples should intentionally mix model families. Grid is an orchestration layer, not a
launcher for one model vendor.

Verbose:

```text
MODEL                     ENGINE       WHERE
gemma4-31b                mac-studio   http://192.168.1.10:8080/v1
qwen36-27b-mtp            gpu-3090     http://192.168.1.20:8000/v1
devstral-small-2          gpu-4090     http://192.168.1.30:8000/v1
glm-4.5-air               gpu-5090     http://192.168.1.40:8000/v1
comfyui:image_generation  media-mac    http://192.168.1.30:8190
```

In `remote` mode `grid models` and `grid engines` read the grid's live overview from its public
relay endpoint (no token needed, so they work even before `grid sync`). The output is the same
shape, but `--verbose` shows the **node** serving each model instead of a local `WHERE` URL —
remote engines sit behind the relay, not at an address you call directly.

`grid catalog --api <kind>` answers the discovery question for **API engines**: which models
would a `grid join --api <kind>` serve? It prints a curated, static whitelist with each model's
capabilities and context window — no key needed, no network call (the same posture as the
"Grid can pull" catalog). The table carries the date it was last verified against the vendor's
documentation, and an unknown kind is a clear error listing the supported kinds. Models are
advertised under namespaced names (`openai:gpt-5.5`), so it is visible in every model list that
requests to them leave the grid for the vendor. `--json` emits the same table machine-readable.

```text
Models a `grid join --api openai` would serve (verified 2026-07-08):
  openai:gpt-5.5           1,050,000 ctx   tools, vision, json, structured
  openai:gpt-5.4           1,050,000 ctx   tools, vision, json, structured
  openai:gpt-5.4-mini        400,000 ctx   tools, vision, json, structured
  openai:gpt-5.4-nano        400,000 ctx   tools, vision, json, structured

No key needed to view. Requests to openai:* models leave the grid for the vendor.
```

See [ADR 0012](./adr/0012-api-engines.md) for the decisions behind the CLI-shipped whitelist,
the `openai:*` namespacing, and the key-store lifecycle.

## Use

```
grid chat -m <model> "<message>" [--json] [--target-provider <id>] [--allow-self-provider]
grid image "<prompt>" [-o <dir>] [--target-provider <id>] [--allow-self-provider]
grid edit "<prompt>" -i <img>... [-o <dir>] [--target-provider <id>] [--allow-self-provider]
grid video "<prompt>" -i <img> [-o <dir>] [--target-provider <id>] [--allow-self-provider]
```

These are smoke tests and useful daily commands. The same verbs work in both modes: in `local`
they go through the local grid proxy, in `remote` through the grid's relay with your access token.
`--target-provider` (pin the request to a specific engine) and `--allow-self-provider` (let your
own engine serve it) are **remote-only** — using them in `local` mode is a clear error. Their errors
should name the missing model, the selected grid, and the next diagnostic command:

```text
No live model named `qwen36-27b-mtp` on grid `home`.

See live models:
  grid models

Check engines:
  grid engines
```

**`-m auto`** lets the grid pick the model, when its owner has enabled auto-routing (`grid router`).
`grid chat -m auto "…"` sends the reserved name `auto`; the reply comes back from whichever capable
model the grid ranked and had free, and the `X-Grid-Routed-Model` response header (and the reply's
`model` field) name it. On a grid without routing enabled, `auto` is a clear "not enabled" error. See
[Router](#router).

## Members

```
grid members add [grid] <email> [--role consumer|provider|both] [--json]   # default role: both
grid members remove [grid] <email> [--json]
grid members list [grid] [--json]
```

**Remote-only.** Manage who may use or serve a remote grid you own. `[grid]` follows the usual
selection rules (the active grid when omitted); `add`/`remove` take a member `email`, and `--role`
is `consumer` (use models), `provider` (serve models), or `both`. `grid members list` prints each
member's email and roles (`--json` for the raw list). These authenticate with your account sign-in
(not a per-grid token) and don't need the grid to be running. In `local` mode the command exits with
guidance to switch — membership is a remote concept. See [ADR 0006](./adr/0006-remote-membership.md).

## Price

```
grid price set -m <model> [--type chat] --input <usd> --output <usd> [--cache <usd>] \
               [--name <str>] [--maker <str>] [--status <str>] [--context-length <n>] [--grid <grid>]
grid price rm  -m <model> [--grid <grid>]            # alias: grid price delete
grid price show [--grid <grid>] [--json]
```

**Remote-only.** Set this engine's **authoritative** price for a model it serves — the rate the relay
uses to bill and to pick the cheapest engine (it replaces the deprecated advertise-only
`grid join --pricing-input/--pricing-output`). Rates are **USD per 1,000,000 tokens**; `--cache`
defaults to 0. `--type` defaults to `chat`; `image`/`video` aren't priced yet (the command rejects
them). `[grid]`/`--grid` follows the usual selection (active grid when omitted) and the call uses the
grid's per-grid access token.

`set` can also record optional model **metadata** on the same relay endpoint — `--name` (display
name), `--maker` (vendor), `--status` (e.g. `available`), and `--context-length` (max tokens). Each is
sent only when given, so a rates-only `set` stays minimal and doesn't clobber metadata set earlier.

`set` requires the engine to be **joined and serving the model** — the relay rejects a price for a
model you aren't currently serving (`grid join` first). `rm` does not (you can clean up a price after
`grid leave`). `show` lists the grid's models and prices. In `local` mode the command exits with
guidance to switch.

## Router

```
grid router status  [--grid <grid>] [--json]
grid router enable  [--grid <grid>] [--json]
grid router disable [--grid <grid>] [--json]
grid router models  [--json]
grid router set-advisors   <provider[:model]> [<provider[:model]> …] [--grid <grid>] [--json]
grid router remove-advisor <provider[:model]> [--grid <grid>] [--json]
```

**Remote-only.** Configure **auto-routing** for a grid you own: an app that requests the reserved model
`auto` has the grid pick a model for the request, ranked by an external **Advisor** (see
[ADR 0013](./adr/0013-auto-routing.md)). An Advisor is a `provider[:model]` pair you pick **by name** from
the platform catalog. **Start with `grid router models`** — it lists the providers and their whitelisted
models (the default marked) — then name advisors from that list; a bare `provider` uses its default model.
You supply neither a URL nor a key: the platform carries both. `enable`/`disable` turn routing on and off;
`set-advisors` **replaces the whole chain** with up to three advisors in priority order (the same provider
may repeat with a different model — with a one-provider catalog, the only route to a real failover chain —
but a duplicated exact `provider:model` pair is rejected); `remove-advisor` drops one by name (an exact
`provider:model`, or a bare `provider` to remove all of its entries); `status` shows the enabled state and the chain as ordered
`provider:model` tokens — **never a key or URL**, in either human or `--json` output.

Every subcommand that acts on a grid selects it with `--grid` (active grid when omitted); `set-advisors` and
`remove-advisor` take their advisor tokens positionally, and `models` needs no grid at all. Like membership,
these authenticate with your account sign-in (not a per-grid token) and don't need the grid running; in
`local` mode the command exits with guidance to switch. A change that couldn't be pushed to the running grid
yet is reported as saved and will apply shortly.

**Chain + fallback.** The Advisors are tried strictly in priority order (1 → 2 → 3, never reordered),
advancing on failure. Each has a circuit breaker — 3 consecutive failures skip it for 60 s, then one
half-open probe re-tries it — so a dead vendor doesn't tax every request. If every Advisor is down the
grid still serves from a deterministic local pick (most free capacity → cheapest → name), stamped
`X-Grid-Router: fallback`. The ranking call runs on the platform's advisor-proxy key (not your key, and not
the consumer's); the served request bills the consumer as the chosen model.

**Consuming `auto`.** An app requests the reserved model `auto` on `chat/completions` (streaming or
not); the response `model` and the `X-Grid-Routed-Model` header carry the real model, and
`X-Grid-Router` is `ranked` or `fallback`. `auto` appears in `/v1/models` (as `owned_by:
"grid-router"`) only while routing is enabled; disabled → a clear "auto routing is not enabled" error.
`auto` is chat-only — the legacy `completions` endpoint and an `X-Target-Provider` header each reject
it, and media models are never candidates.

### Auto-routing transparency

When routing is enabled, an `auto` request sends a **bounded excerpt of the request** plus a
**short list of your grid's own candidate models** to each Advisor in turn. This table is the complete
set of request data that leaves the grid; the full conversation never does.

| Field | What it is | Bound |
|---|---|---|
| system head | head of the first `system` message, truncated | ≤ 500 chars |
| recent user tails | tails of the **last 3 `user` messages** (oldest→newest), each truncated — so a terse final message still carries the task context set in the turns leading up to it | ≤ 2000 chars each |
| message count | number of messages in the request | integer |
| approx input size | total characters across all message content | integer |
| tool names | declared function **names** only — never arguments or JSON schemas | list of names |
| images present | whether any image/binary part exists (each becomes a `[image]` marker) | yes / no |
| requested output size | the request's `max_tokens`, if set | integer or unset |

- **Candidate metadata (grid-side, not request data)** — alongside the excerpt, the Advisor is given
  one line per candidate model: the model **name**, its **capability names** (`tools`, `vision`, …,
  bounded to a known vocabulary so a provider can't inject arbitrary text), its **context window**
  (included only when known), and its **price** — the cheapest serving engine's rates, rendered
  `price: $<in> in / $<out> out per 1M`, or `$0 in / $0 out` for a model nobody priced (which is
  what it bills). This is information about the **engines your grid's providers serve**, not about
  the consumer's request, so it does not widen the request privacy surface above. It is capped at
  **50 candidates**. Per-engine **free capacity and throughput are never included** — those change
  by the second, so they stay on the grid and decide the local pick.
- **When** — only while `grid router` is **enabled**; a disabled grid makes no outbound Advisor call.
- **To whom** — the Advisors you configured, in priority order (advisor 1, then 2, 3 on failure), each
  reached **through the platform's LLM proxy** — you never hold, store, or hand out an advisor key or URL.
- **On whose account** — the ranking call runs on the platform's advisor-proxy key (not your key, and
  not the consumer's); the served request is billed to the consumer as the chosen model.
- **Never sent** — the full conversation, `assistant`/`tool` turns, `user` turns older than the last
  three, tool-call arguments or schemas, raw image/audio bytes or URLs, per-engine
  pricing/capacity/throughput, or any API key.

See [ADR 0013](./adr/0013-auto-routing.md) for the reserved-name, excerpt-not-conversation, and
fixed-priority-chain decisions.

## Engine Setup

```
grid engine install llama.cpp          # default text engine
grid engine install comfyui            # default media engine
grid engine pull <bundle>              # ComfyUI media bundle: image_generation, image_editing, i2v
grid engine ls [grid] [--json]         # live engines joined to a grid (same view as grid engines)
```

Grid has no inference engine of its own. These commands install open-source default
engines so a bare machine can join a grid without Ollama, LM Studio, or vLLM.

## Aliases

```
grid list                              # alias for grid ls
grid engine list                       # alias for grid engine ls
grid engines                           # legacy alias for grid engine ls
grid remove <model> [--yes]            # alias for grid rm
```

Aliases are for familiarity, but docs should teach the shorter form.

## Output Contract

Human output uses these names exactly:

```
grid
grid_url
engines
models
```

`grid_url` is the primary URL. `OPENAI_BASE_URL` is derived as `${grid_url}/v1` and is
shown only where OpenAI-compatible app integration needs copy-pasteable environment
variables.

Environment output from `grid info --env` (local — the key is a placeholder, the grid is unauthenticated):

```bash
export OPENAI_BASE_URL="http://192.168.1.25:8090/v1"
export OPENAI_API_KEY="local-grid"
```

In `remote` mode the base is the grid's relay and the key is your real per-grid access token — the one
command that prints a token (like `gh auth token`):

```bash
export OPENAI_BASE_URL="https://relay.example/relay/v1"
export OPENAI_API_KEY="<your access token>"
```

JSON output should use snake_case keys and include enough detail for scripts:

```json
{
  "grid": "home",
  "grid_url": "http://192.168.1.25:8090",
  "engines": [],
  "models": []
}
```

## First-Run Happy Path

```bash
grid up
grid join
grid models
grid chat -m qwen36-27b-mtp "hello"
eval "$(grid info --env)"
```

For a machine with no engine:

```bash
grid up
grid engine install llama.cpp
grid pull qwen36-35b-a3b-mtp
grid join --serve qwen36-35b-a3b-mtp
grid chat -m qwen36-35b-a3b-mtp "hello"
```
