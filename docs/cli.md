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
launch    start an app already pointed at your grid; the app it starts is a launch target
pack      a bundled starting point for training on business data (`grid train packs`)
adapter   the small add-on layer a training run produces (LoRA) — what moves between machines
gate      the check that refuses to serve a trained model unless it beat the one you serve
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
grid --version                        # same output as `grid version`
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

Sign in with `grid login`, then manage your remote grids with `grid up`/`ls`/`info`, serve models
with `grid join`, and use them with `grid chat -m <model> "…"`.
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
grid mode [--json]                    # print the current mode
grid mode local | remote                 # switch and persist the mode
grid use [name] [--json]              # show or set the active grid for the current mode
grid use --none                       # clear the active grid for the current mode
grid [--local | --remote] <command>      # override the mode for a single command
```

The mode is persisted in `~/.grid/state.json` (default `local`); each mode remembers its own
active grid. Which mode a command runs in is resolved as `--local`/`--remote` (one command) > the
persisted mode > `local`. `grid use <name>` sets the persistent default grid, so `grid chat` /
`grid info` / `grid models` target it without naming it — naming a grid explicitly still wins (the
`[grid]` positional on `info`/`models`/`engines`, `--grid` on `chat`/`image`/`edit`/`video`), and a
stale selection (its grid was removed) is ignored.

In `remote` mode the grid lifecycle (`up`/`down`/`ls`/`info`), live reads (`engines`/`models`),
sign-in (`login`/`logout`), serving (`join`/`leave`), consuming (`chat`/`image`/`edit`/`video`),
handing an app the grid (`grid launch`), and
membership admin (`grid members`) all work. `grid members` and `grid launch` are remote-only — in
`local` mode each exits with guidance to switch, naming its own reason (sign-in for `members`, the
dialect the launched app speaks for `launch` — see [Launch](#launch)). The shared
local commands (`catalog`, `pull`, `rm`/`remove`, `ctx`, `device-info`, `engine …`, `agent …`,
`train …`) work in either mode. A machine with no state
file behaves exactly as a `local`-only install.

Notes:
- `--json` goes after the subcommand (`grid info --json`); bare `grid --json` prints the
  overview as JSON, including a `mode` key.
- `--local`/`--remote` may appear anywhere on the line, but are not listed in per-command `--help`.
- `grid mode` is the exception to that override: it reads and writes the *persisted* mode, so
  `grid --remote mode` still prints the persisted one.

## Sign in

```
grid login [--no-browser] [--json]    # sign in to remote mode (device-code flow)
grid logout [--force] [--json]        # stop serving, then clear stored remote credentials
grid sync [--json]                    # refresh your remote grids without signing in again
```

**Remote-only.** `grid login` signs you in to autonomous's hosted relay with a device-code
flow — it prints the sign-in URL and code, and opens a browser at that URL unless you pass
`--no-browser` (for headless machines) — and stores your credentials under `~/.grid`. Signing in does
**not** pick an active grid: run `grid ls` to see the remote grids you can reach, then
`grid use <name>` (or name one per command).

`grid logout` **stops serving, then clears the stored credentials and the remote mode's active
grid** — in that order, because a serve
child holds its access token in memory from the moment it starts (that token lasts about a year), so
deleting the file never stopped it: the box kept advertising models as a provider, and `grid leave`
answered "You're not signed in" over records that were still correct. So logout tears down whatever
this box is serving first, deregistering each grid while it still has the token to do so, and reports
what it stopped. If it cannot confirm a child stopped on a grid whose token you still have, it **keeps
your credentials** and exits non-zero naming the pid — they are the only handle a retried `grid leave`
has. `--force` signs out anyway (it still tries first, and still tells you what survived). On a box
that is serving nothing, logout is what it always was: local, offline, instant. `device.toml` and
`api_keys.toml` are untouched either way.

`grid sync` re-fetches your grids and tokens using your saved sign-in (no browser), so a grid
created on the website or one you were just added to appears after `grid sync` — it never changes
your active grid, and an expired session tells you to run `grid login`. If its refresh (or a
`grid login` as a different account) drops a grid this box is still serving, it says so, naming the
process and the `grid leave <grid-id>` that stops it — neither command kills an engine on your behalf,
because a control-plane answer is not a decision to stop serving. In `local` mode these
commands exit with guidance to switch — sign-in is a remote concept. See
[ADR 0002](./adr/0002-remote-sign-in.md) and
[ADR 0023](./adr/0023-signing-out-with-live-serve-children.md).

## Grid Lifecycle

```
grid up [name] [--type <t>] [--port <n>] [--host <h>] [--advertise-host <h>]
grid down [name]                      # stop a grid (may fail loud); the grid/config persists
grid ls [--json]                      # saved grids (local: name, id, where, url · remote: name, id, type)
grid info [grid] [--json]             # grid, grid_url, live engine count, live models
grid info [grid] --env                # print OPENAI_* exports (local key, or remote relay URL + token)
```

`grid up` output is stable and scriptable:

```text
grid=home
grid_url=http://192.168.1.25:8090
```

`--port` (default 8090) and `--host` (default 0.0.0.0) are the port and interface the local grid
server binds; `--advertise-host` overrides the host published in `grid_url` (otherwise the detected
LAN IP). Those three are local-only, and `--type` is remote-only (the grid type, set on create).

No separate `create` or `start` in the main surface — `up` is the single lifecycle verb, so
first use feels like one operation rather than infrastructure management. (`grid use` only sets
which grid is *active*; it is a selection pointer, not a lifecycle step — see Modes.)

In `local` mode **`grid down` waits and can fail.** It stops the server it can prove is this grid's
own — a recycled or corrupt `server_pid` is reported and never signalled — and then asks the grid's
own port whether that worked. Exit is non-zero, naming a remedy, when a server outlived SIGKILL, when
the grid is still answering on its port, or when it could neither verify the pid nor reach the port;
the recorded pid is kept in all three so a retry still has a handle. It is therefore no longer
instant: a wedged server can cost the shared 25s stop grace and, if its process group outlives it,
another 25s. Healthy stops return as soon as the process exits, and a grid that is already down
succeeds quietly. See [ADR 0026](./adr/0026-the-grid-servers-pid-is-a-claim-too.md).

In `remote` mode these same verbs act on hosted **remote grids**: `grid up <name>` create-or-starts
one — `--type` is `permissioned-public` (default) or `permissioned-providers`, set on create, and
creating needs an explicit name (no auto-`home`). `grid down` stops it (the grid persists),
`grid ls` lists the grids your sign-in fetched (local — no network call, `* ` marking the active
one, columns name/id/type), and `grid info` prints `grid`, `type`, `status` and `grid_url` — the
same four keys under `--json`. `status` comes from the creator-only live status, so a member who
did not create the grid sees it blank. `grid info --env` prints your access token plus
`OPENAI_BASE_URL` as the grid's relay base + `/relay/v1`, so any OpenAI SDK can call it.
See [ADR 0003](./adr/0003-remote-grid-lifecycle.md).

## Engines

```
grid join [grid]                                      # auto-detect local engines
grid join [grid] --all                                # join every detected engine
grid join [grid] --at <url> -m <model>... [--name <id>]
grid join [grid] --serve <model> [--name <id>]
grid join [grid] --media [--bundle <bundle>]... [--name <id>]
grid join [grid] --api <kind> [-m <model>...]         # join a third-party API engine (openai, codex, doggi)
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
an engine label (the engine kind, e.g. `openai` or `codex`), a served model, or a URL fragment such
as `:8000`. Each step must resolve to exactly one engine, or it errors listing the candidates.

### `grid join` in remote mode

In remote mode the same verb serves your models on a remote grid: it brings the engine up the same
way, then runs a detached loop that registers the engine's capabilities with the hosted relay,
long-polls it for work, forwards each claimed job to the local engine, and heartbeats — `grid
leave` stops and unregisters it. You must be signed in and the grid must be up (`grid up`) — with one
deliberate exception: `grid leave <grid-id>` also works **signed out**, so a serve child left running by
an earlier sign-out can still be reaped. It stops the process but cannot tell the grid (there is no
token), so the models drop at the node TTL (~120s) and it says so. `grid
join --all` serves several detected engines under **one** identity: it advertises the union of their
models and routes each job to the engine that serves the requested model (first-detected wins when
two engines share a model name).

Re-running a join that changes nothing is a **no-op** — but only when the engine is actually serving.
The serve loop records when it registered with the relay and touches a heartbeat file beside its run
record on every successful beat, so `grid join` can tell a working engine from a live-but-stuck one
without any network call. When it can't vouch for the engine it says so instead — naming the pid, how
long the process has been up, the last registration error and the log path — rather than the old
reassuring "Already serving …; nothing to append." An engine still inside its bring-up window is
reported as *starting*, with no suggestion to restart it (a large model can take minutes to load).
**`grid join --respawn`** is the way out: it stops the engine already serving this grid and starts a
fresh one, never no-ops and never hot-reloads. On its own (`grid join --respawn`, no other flags) it
restarts whatever this box is already serving; with nothing running it is an ordinary join. See
[ADR 0021](./adr/0021-service-truth-at-the-join-gate.md).

`grid join --media [--bundle <bundle>]...` serves this box's built-in media (ComfyUI) engine to the
relay — media-only, or alongside a text engine (`--serve`/`--at` + `--media`). The serve loop brings
up ComfyUI + the media server, registers the `comfyui:*` workflows the host's VRAM gates in, and the
relay forwards `media/*` jobs to the media server on loopback; the SSE (progress + base64 result
files) streams back exactly as in local mode.

`grid join --api <kind> [-m <model>...]` joins an **API engine** — a third-party LLM API service
served through your own vendor credential ([ADR 0012](./adr/0012-api-engines.md)). Three kinds exist:
`openai` (a metered API key) and `codex` (a ChatGPT/Codex subscription seat — see below), both text
engines, and `doggi` (a media gateway: pass its URL with `--at`, key from `DOGGI_API_KEY`). The key is
resolved in order: the `--api-key` flag (accepted, with a warning that a key on the command line
lands in your shell history), else the `OPENAI_API_KEY` env var, else the machine-local key store,
else a hidden interactive prompt; non-interactive with no key anywhere is a clear error. It is validated at join time against the vendor's model listing — an invalid key
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
the advertised `openai:<name>` to the vendor's `<name>`; SSE streams pass through unchanged. An API engine serves exactly its
kind's endpoints — `openai` serves `chat/completions` and `responses`, `codex` serves `responses`
only. Anything else, including a legacy `completions` job, gets a structured "not served" error
naming what the engine does serve, and is never forwarded. A vendor error (401/429/5xx) surfaces as that job's error
with the upstream status, never touching your grid sign-in and never unregistering the engine; an
auth/quota failure (401/403/429) additionally warns in the engine's log so a revoked key or
exhausted quota is visible to you, not just to consumers. **Requests to `openai:*` models leave the
grid for the vendor**, under your key and your own OpenAI account's terms. `--api` is mutually
exclusive with `--serve`/`--advertise-as`/`--media`/`--bundle` in one invocation — join other engines
with a separate (additive) `grid join`. `--at` is *not* excluded: it overrides the vendor's base URL,
and a kind that ships none (the `doggi` media gateway) requires it.

**A subscription as an API engine (`--api codex`).** `grid join --api codex` joins a ChatGPT/Codex
**subscription seat** instead of a metered key: the CLI runs the vendor's OAuth sign-in itself
(browser + localhost callback by default; `--no-browser` prints the URL and takes the pasted
redirect on a headless box) and stores the rotating token bundle in the same `api_keys.toml`
(`grid logout` leaves it intact; `~/.codex/auth.json` is never touched). The join then runs **one
free probe** — the vendor's own model listing — proving in a single round-trip that the seat is
live, that this machine's egress IP isn't blocked (Cloudflare typically challenges datacenter/VPS
addresses; such a join is refused naming the cause), and which models the seat actually has. The
advertised set is the seat's **live probe set** — the probe is the source of truth for both the
models and their capabilities, and the plan name is a display label only. A re-join that changes nothing performs
**zero vendor calls**; a fresh sign-in restarts a live engine (credential rotation, like a rotated
key); a stored seat the vendor now rejects gets one inline fresh sign-in on an interactive run.
`codex:*` models serve the vendor's **`responses` endpoint only** — point an external Codex app at
your grid; `grid chat` refuses them client-side with exactly that guidance. **Jobs spend the
seat's own monthly Codex allowance.** See
[ADR 0015](./adr/0015-codex-subscription-engine.md), or the step-by-step
[codex quickstart](./codex-quickstart.md) (join → sign-in, headless included → serve → point the
Codex CLI at the grid).

### Pointing a Codex app at your grid (using `codex:*` models)

A `codex:*` model is **used from an external Codex-compatible app** (Codex CLI, Codex Desktop),
never from `grid chat` — the grid's own use verbs speak `chat/completions`, codex engines serve
only the `responses` endpoint, and traffic is never translated between the two. The app needs the
same two values every OpenAI SDK needs, printed by **`grid info --env`** (the grid must be up, and
your sign-in must be a member): the relay base URL and your per-grid access token.

For the Codex CLI, add a provider to `~/.codex/config.toml` and select a `codex:*` model
(keys verified against `codex-cli 0.144.2`):

```toml
model = "codex:gpt-5.4-mini"            # a codex:* name from `grid models`
model_provider = "grid"
model_context_window = 272000           # pin it — an unknown slug gets fallback metadata otherwise

[model_providers.grid]
name = "Autonomous Grid"
base_url = "https://<your relay>/relay/v1"   # OPENAI_BASE_URL from `grid info --env`
env_key = "GRID_API_KEY"                     # the env var the app reads your api key from
wire_api = "responses"                       # mandatory — `wire_api = "chat"` is rejected by the app
supports_websockets = false                  # the grid relay streams HTTP SSE, not WebSocket
```

```bash
export GRID_API_KEY="<your access token>"    # OPENAI_API_KEY from `grid info --env`
codex "explain this repo"
```

(The same keys can hang off a `[profiles.<name>]` block instead of the config root if you don't
want to change the app's default provider.)

What the passthrough contract means for the app, in plain language:

- **Requests leave the grid for the vendor** (OpenAI), forwarded by the member whose seat serves
  them — and **spend that member's own monthly Codex allowance**.
- **The vendor is forced stateless**: the relay refuses `store: true`, `previous_response_id`, and
  `conversation` up front (in the vendor's own error shape), so every turn resends the full
  history — exactly how the Codex CLI already behaves. A long session therefore grows toward the
  relay's request body-size cap, which is the practical session bound.
- **The relay still retains stream chunks for its task TTL** — like every other grid endpoint —
  even though the vendor is forced stateless. Statelessness upstream is not zero retention on the
  relay. And the vendor itself **caches prompts for ~24h even under `store: false`** (the response
  advertises `prompt_cache_retention: "24h"`) — "stateless" bounds conversation state, not every
  vendor-side trace.
- **There is no output-token ceiling on this path.** The vendor's codex backend accepts no cap
  parameter under any name (`max_output_tokens`, `max_tokens`, `max_completion_tokens` are all
  refused, as is `temperature` — it runs an allowlist and rejects chat-era knobs). The relay
  refuses the cap spellings up front so you learn there is no ceiling instead of being silently
  billed for an uncapped response; anything else it passes through verbatim, so an unlisted
  parameter surfaces as the vendor's own 400.
- **The app's `/model` picker writes bare vendor names — the relay absorbs that.** Picking a
  model inside the Codex app rewrites its `config.toml` to the bare slug (`gpt-5.6-terra`),
  dropping the `codex:` prefix; the app never reads the grid's model list, so it cannot know the
  grid's names. On `/responses` (only), a bare name that matches nothing is aliased onto the one
  `codex:<name>` engine that serves it — responses replies still name the `codex:*` model. If two
  kinds ever serve the same bare name, the relay refuses and names both rather than picking.

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
  `--heartbeat-interval` (seconds between heartbeats, default 15), `--api-key <key>` (overrides the
  env var and the key store, and warns that it is visible in shell history), and the media flags
  `--media` / `--bundle <bundle>` / `--comfyui-port` / `--media-port`.
- **local-only:** `--advertise-host` (a remote engine polls the relay outbound — there is no inbound
  endpoint to advertise).
- **Remote-only:** `--api <kind>` for a **text** kind (`openai`, `codex`) — a text API engine is
  served by the relay's poll loop, which local mode has no equivalent of; the **media** kind
  (`doggi`) also joins in `local` mode, where it bridges to the vendor gateway exactly where ComfyUI
  would sit. `-m` optionally narrows the whitelist (omitted = every whitelisted model the key can
  see), `--no-browser` (the codex OAuth
  sign-in's paste flow for headless boxes; inert elsewhere, with a note), and `--max-concurrency`
  (how many requests this engine serves at once; the provider runs one poll worker per slot —
  default 1, or 8 when the identity serves only API engines, pinned back to **1** when any of
  them is a `codex` seat: a flat-rate subscription is never hammered eight-wide by default).
  Match it to the engine's own batch width — llama.cpp `--parallel`, vLLM `max_num_seqs` — or the
  extra slots queue behind a batch that was never widened to take them. Finally, `--respawn` (stop
  the engine already serving this grid and start a fresh one — see below).
- **Deprecated:** `--engine-label` — the grid page now derives the engine kind automatically, so it is
  accepted but inert (still matched by `grid leave --engine <label>`); `--pricing-input` /
  `--pricing-output` — kept so old invocations don't hard-error, but they no longer advertise a price.
  Set your authoritative per-model price with `grid price set` (see [Price](#price)) instead.

A flag used in the wrong mode fails with a clear message. (`--advertise-as` is single-engine only: a
join whose merged union holds more than one engine — or an append onto an identity already serving —
is rejected, and the aliases must be re-passed in one command after `grid leave`. Locally, an alias
count that does not match the `-m` count fails the join.) See [ADR 0004](./adr/0004-remote-provider-serve.md),
[ADR 0007](./adr/0007-remote-multi-engine-routing.md), and
[ADR 0008](./adr/0008-remote-media-serve.md).

## Models

```
grid models [grid] [--verbose] [--json] # live models the grid can run now
grid catalog [--json]                   # models Grid can pull
grid catalog --api <kind> [--json]      # API-engine whitelist for a service kind (openai, codex, doggi)
grid pull <model>                       # pull a model for the default text engine
grid rm <model> [--yes]                 # remove a pulled model
grid ctx <model> [--json]               # a model's max context length, read from its GGUF metadata
grid device-info [--json]               # this machine's chip, cores, memory, disk and GPU
```

`grid ctx` takes a filename under `~/.grid/models/` or a path to a `.gguf`; `grid device-info`
prints one flat inventory of the machine you are on. Both are mode-agnostic and need no grid.

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
relay endpoint (no per-grid token needed, so they work even before `grid sync`). `--verbose` prints
`MODEL ENGINE NODE` — the **node** serving each model instead of a local `WHERE` URL, since remote
engines sit behind the relay, not at an address you call directly. A grid with auto-routing enabled
also lists the reserved model `auto` (see [Router](#router)).

`grid catalog --api <kind>` answers the discovery question for **API engines**: which models
would a `grid join --api <kind>` serve? It prints a curated, static whitelist with each model's
capabilities and context window — no key needed and no network call, the same posture as the
"Grid can pull" catalog. (`--api codex` is the one exception: with a seat signed in it makes one
free model listing to show that seat's real entitlement — see below.) The table carries the date it was last verified against the vendor's
documentation, and an unknown kind is a clear error listing the supported kinds. Models are
advertised under namespaced names (`openai:gpt-5.5`), so it is visible in every model list that
requests to them leave the grid for the vendor. `--json` emits the same table machine-readable.

```text
Models a `grid join --api openai` would serve (verified 2026-07-08):
  openai:gpt-5.5           1,050,000 ctx   tools, vision, json, structured, responses
  openai:gpt-5.4           1,050,000 ctx   tools, vision, json, structured, responses
  openai:gpt-5.4-mini        400,000 ctx   tools, vision, json, structured, responses
  openai:gpt-5.4-nano        400,000 ctx   tools, vision, json, structured, responses

No key needed to view. Requests to openai:* models leave the grid for the vendor.
```

For the `codex` kind a seat's model set depends on its plan, so `grid catalog --api codex` prints
two blocks: **your current plan** — with a seat signed in it makes one free model listing to read
that seat's real entitlement — and the per-tier table for reference. Codex rows claim no
chat-dialect capabilities (no json/structured column): these models serve the vendor's `responses`
endpoint, for external Codex apps, never `grid chat`. `--json` for codex speaks `current_plan` and
`tiers` (alongside `kind`, `source_url`, `last_verified`, `endpoints` and `warning`) instead of the
flat `models` list.

See [ADR 0012](./adr/0012-api-engines.md) for the decisions behind the CLI-shipped whitelist,
the `openai:*` namespacing, and the key-store lifecycle.

## Use

```
grid chat  -m <model> "<message>" [--json] [--grid <g>] [--timeout <s>]
grid image -m <model> "<prompt>" [-o <dir>] [--width 720] [--height 720] [--steps 4]
grid edit  -m <model> "<prompt>" -i <img>... [-o <dir>] [--steps 4]
grid video -m <model> "<prompt>" -i <img> [-o <dir>] [--duration 5s|8s] [--aspect-ratio <r>]

# all four also take: [--grid <g>] [--timeout <s>] [--target-provider <id>] [--allow-self-provider]
```

`-m/--model` is required on all four. `image` sizes with `--width`/`--height` (720×720) and
`--steps` (4); `edit` takes `--steps` and up to three `-i/--image` values; `video` takes
`--duration` (`5s` or `8s`) and `--aspect-ratio`. `--grid` runs against a grid other than the
active one, and `--timeout` is in seconds — 600 for `chat`, 1800 for the three media verbs, which
wait on a streamed result.

These are smoke tests and useful daily commands. The same verbs work in both modes: in `local`
they go through the local grid proxy, in `remote` through the grid's relay with your access token.
`--target-provider` (pin the request to a specific engine) and `--allow-self-provider` (let your
own engine serve it) are **remote-only** — in `local` mode they exit 1 before any network call,
naming the flag and the way out. An error from these verbs should name the missing model, the
selected grid, and the next diagnostic command (`grid models`, then `grid engines`).

**`-m auto`** lets the grid pick the model, when its owner has enabled auto-routing (`grid router`).
`grid chat -m auto "…"` sends the reserved name `auto`; the reply comes back from whichever capable
model the grid ranked and had free, and the `X-Grid-Routed-Model` response header (and the reply's
`model` field) name it. On a grid without routing enabled, `auto` is a clear "not enabled" error. See
[Router](#router).

**`codex:*` models are not chat models.** They serve the vendor's `responses` endpoint for external
Codex apps; `grid chat -m codex:…` is refused client-side, before any network call, with the
guidance to point a Codex-compatible app at the grid instead
([Pointing a Codex app at your grid](#pointing-a-codex-app-at-your-grid-using-codex-models)).

## Launch

```
grid launch                                    # list the apps that can be launched
grid launch <target> [grid]                    # start one, already pointed at the grid
grid launch <target> [grid] --print-env        # print the environment instead; start nothing
grid launch <target> [grid] -- <app args…>     # forward everything after `--` to the app
```

`grid launch` starts a **third-party app already pointed at your grid**. The endpoint, the credential
and the model names go into that app's own process environment and nowhere else — never exported to
your shell, never written to a config file. Closing the app is the entire cleanup, which is why there
is no `--restore`. One **launch target** ships today: `claude` (Claude Code).

Bare `grid launch` lists the targets and exits 0 — it is discovery, not an error:

```text
Launch targets:
  claude	Claude Code

Start one with `grid launch <target>`.
```

**Remote-only, and the dialect is the reason.** Claude Code speaks the Anthropic Messages dialect and
nothing else. A local grid serves `chat/completions`, `completions`, `models` and media — there is no
`/v1/messages` on it at all — so this is a design boundary, not a missing flag, and `local` mode says
which one:

```text
`grid launch` is a remote-mode command. Run `grid mode remote` (or pass --remote) to launch an app
that speaks the Anthropic Messages dialect, which a local grid does not serve.
```

A remote grid's relay translates a Messages request to `chat/completions` before it routes, which is
why any chat model on a remote grid can back the app. Teaching the local server the same dialect
would mean hand-carrying that translation across a seam with no code dependency — rejected in
[ADR 0028](./adr/0028-launch-hands-an-app-the-grid.md).

**`[grid]`** matches `info`/`models`/`engines` verbatim: a grid name or `ag-…` id, omitted for the
active grid — so `grid use` stays the one place you choose where work runs.

**`--`** hands everything after the **first** separator to the app, in order and unread:

```bash
grid launch claude -- --continue
grid launch claude -- -p 'summarise this repo'
grid launch claude research -- --continue      # …on a grid other than the active one
```

Two words are the exception. `--local` and `--remote` are this CLI's one-shot mode override and are
stripped from **anywhere** on the line, separator included, so neither can reach the app; a warning
says so when one is taken. `-- --local` additionally flips the run to local mode, where the command
refuses — reported as a mode error rather than as a missing argument.

**`--print-env`** prints exactly what a launch would inject, as shell exports, and starts nothing:

```bash
grid launch claude --print-env
```

```bash
export ANTHROPIC_BASE_URL='https://relay.example/relay'
export ANTHROPIC_AUTH_TOKEN='<your access token>'
```

Two keys — an endpoint and a credential, and nothing else. **Only the bearer variable**: setting `ANTHROPIC_API_KEY` beside it makes Claude Code warn that auth may not work, and buys nothing — the relay prefers `Authorization: Bearer` whenever both arrive.

The base carries the relay prefix and **no** `/v1`: Claude Code appends `/v1/messages` itself, so the
`/v1` that `grid info --env` prints for OpenAI clients would 404 every request here. This is the
**second** command that prints your access token — `grid info --env` is the first — and carries the
same justification: an explicit, user-requested disclosure of your own token to your own shell. Every
warning goes to stderr, so `eval "$(grid launch claude --print-env)"` evaluates only exports.
`--print-env` and `--` are mutually exclusive: with nothing started, the arguments after `--` have
nothing to reach, so the combination is refused rather than silently dropped.

**No model is chosen for you.** `grid launch` sets no `ANTHROPIC_MODEL` and none of its siblings, so
Claude Code resolves a model the way it does everywhere else — its own defaults, your
`settings.json`, and `/model` inside the app. Which model a session runs on is your choice, and the
grid answers for whatever it is asked. Use `grid models` to see what the grid serves, and point the
app at one of those names.

The trade is worth naming: a model the grid does not serve now fails at your **first prompt** rather
than at launch. Grid cannot check it in advance, because it no longer knows what the app is going to
ask for.

Preflight is therefore down to the one model fact that holds whatever the app asks: a grid serving
nothing can serve nothing. It always runs, has no off switch, and never asks a question:

```text
Grid team serves no models yet, so Claude Code has nothing to talk to.
Run `grid join` on a machine with an engine, then `grid launch claude` again.
```

It reads the grid's **public overview**, the same read `grid models` and `grid engines` render, so
the two can never disagree about what is live.

**The credential is checked first, and repaired if it can be.** The order is deliberate: a dead token
invalidates the "your grid is empty" advice above, while an empty grid says nothing about the token. `grid launch`
reads the token's own expiry offline, and asks the grid — `GET /relay/v1/models`, one round-trip —
whether it will actually accept it. A token that has expired, is within a day of expiring, or is
refused by the grid is **refreshed in place** and the launch continues, with one line on stderr:

```text
Refreshed grid team's access token (it expired 3 days ago).
```

That covers the whole family a grid rejects a token for — a passed expiry, a `member_epoch` or
`network_epoch` bumped by a membership change, a rotated key. What it cannot repair, it refuses
before the app starts, saying which: a dead refresh credential (`grid login`), a membership that no
longer permits inference (`grid login` will not help — the `consumer` and `both` roles grant it), or a
control plane that could not mint a token (nothing is wrong with your sign-in; try again).

A check that could not run never costs you a launch that would have worked: a throttled, failing or
unreachable relay warns and launches anyway, and so does a renewal that fails while the current token
is still valid. This is the one thing `grid launch` writes — a refreshed token goes back into
`~/.grid/credentials.toml`, the same store `grid login` and `grid sync` maintain. Nothing is written
for the *app*. See [ADR 0029](./adr/0029-the-credential-is-checked-before-it-is-handed-over.md).

Everything else is left alone. The app runs on your real configuration — MCP servers, skills,
permissions, history — which also means Claude Code's own `settings.json` `env` block **outranks**
what is injected; the command reads that file and warns in one line, and never edits it. A missing
binary on an interactive run offers the **vendor's own installer**; with no terminal it prints the
install command and exits non-zero rather than prompting where no one can answer. One line on stderr
names the grid and the model before the app takes the screen, and the app's exit code becomes the
command's.

Step by step: [Claude Code quickstart](./claude-code-quickstart.md) ·
[ADR 0028](./adr/0028-launch-hands-an-app-the-grid.md) ·
[ADR 0029](./adr/0029-the-credential-is-checked-before-it-is-handed-over.md).

## Training

```
grid train doctor [--config <path>] [--json]      # what this computer can do right now
grid train where                                  # which grids training can use (LAN and hosted)
grid train init [--pack <name>] [--dest <dir>] [--config <path>] [--force]
grid train packs [--json]                         # bundled task packs for business data

grid train sft [--backend auto|mlx|torch] [--iters <n>] [--run-dir <dir>] [--config <path>]
grid train run [--config <path>]                  # the feedback loop (GRPO via TRL)
grid train eval --run <dir> --candidate <name> [--adapter <dir>] [--base <name>] [--config <path>]
grid train deploy --adapter <dir> [--gate] [--run <dir>] [--node <url>]... [--name <n>] [--config <path>]

grid train collect [--on | --off] [--teacher <model>]... [--sample <f>] [--retain-days <n>]
                   [--no-redact] [--prune] [--days <n>] [--json]
grid train autopilot [--stage auto|sft|rl] [--min-examples <n>] [--days <n>]
                     [--no-deploy] [--ignore-host] [--history] [--config <path>]
grid train nightly [--no-deploy] [--ignore-host] [--history] [--config <path>]
grid train outcomes zendesk --subdomain <s> --email <e> [--days 7] [--dry-run]
grid train schedule [status|on|off] [--at HH:MM] [--name <label>] [--config <path>]

grid train serve [--model <id>] [--adapter-path <dir>] [--host <h>] [--port <p>]
grid train convert-adapter <source> <dest> [--to mlx|peft]
grid train pull zendesk|hubspot [--out <file>] [--subdomain <s>] [--email <e>]
                                [--max-rows <n>] [--status <s>]
grid train ui [--port 8321]                       # read-only dashboard of runs and curves
grid train web [--port 8322] [--host 127.0.0.1]   # the point-and-click interface
```

Training is the other half of owning your machines: the grid already runs inference on them, and
these verbs make it **train small models on your own data, on the same machines, and serve the
result only if it is better**. Nothing is uploaded anywhere — training is local in every topology
([topologies](./topologies.md) covers why inference can be hybrid and training is not).

**Start with `grid train doctor`.** It answers for two rungs separately, because they need
different things:

```text
What this computer can do now
  ok learn from answers your team already wrote (`grid train sft`)  ·  MLX on this Mac's own chip
  NO learn from feedback (`grid train run`)  ·  needs an engine that serves the training contract
                                                — `grid train serve` on a Mac, or vLLM
Ready for stage one. `grid train sft` works on this computer right now.
```

(The first line reads `torch on this machine` off Apple Silicon.) It exits 0 when either rung is
ready, so a machine that can do stage one is not reported as a failure.

- **`grid train sft`** is imitation: it learns from the answers your team already wrote and needs
  nothing but the machine in front of you. `--backend auto` picks MLX on Apple Silicon and torch
  elsewhere.
- **`grid train run`** is the feedback loop (GRPO through TRL). It needs an engine that returns the
  **token ids it sampled and their logprobs** — the *rollout contract*. vLLM returns them natively;
  `grid train serve` makes an Apple-Silicon Mac serve the same contract, which is why an all-Mac
  office needs no CUDA and no vLLM. A chat-only API yields zero trainable samples, and `doctor`
  says so before a night is wasted.

### The gate

`grid train eval` and `grid train deploy --gate` are the same check: score the model you serve
today and the trained candidate over held-out work with the same graders training used, greedy
both sides, per grader. A candidate that gains less than **0.01** overall, or loses more than
**0.02** on any single grader, is refused — "tidier but less correct" does not ship.

The candidate is judged as **weights**, not as a name: the incumbent is scored first, then the
adapter is loaded under a checking name (`<name>-candidate`), then scored. Asking an engine for the
candidate's serving name would score whatever weights it already holds. Afterwards the check puts
back what the node was serving — always, whatever the verdict — so a check is an observation, and
the winner is deployed once, afterwards. Each run writes `eval-card.{json,html}`: the per-grader
table plus the same work answered by both models.

### The loop that runs itself

```bash
grid train collect --on            # keep the work the grid does — local files, redacted, pruned
grid train autopilot               # one unattended cycle over what has accumulated
grid train schedule on --at 23:00  # and have this computer run it every night
```

`collect` is **off until you turn it on**. What it keeps is stored under `~/.grid/capture`, scrubbed
of emails, phone numbers and card numbers unless you pass `--no-redact`, sampled with `--sample`,
and pruned on `--retain-days`. `grid train collect` with no flags prints what has accumulated.

An example earns its place from a signal, never from the model's own confidence: a human
**correction** (weight 1.0) outranks a **stronger model's** answer (`--teacher <model>`, 0.8), which
outranks **sent as-is** (0.6); a **rejected** answer is kept for the record and never imitated; and
**a model's own unjudged output is never trained on**, which is the rule that stops a model
agreeing with itself into drift. Apps report the human's verdict by quoting back the
`X-Grid-Request-Id` header they got with the answer to `POST /v1/feedback`.

`grid train outcomes` closes the loop without asking anyone to click anything. The helpdesk
already holds the reply that was actually sent and whether the ticket stayed solved, so this reads
it back and writes the same three verdicts a person would: sent as we wrote it (weaker), rewritten
by a person (their text becomes the truth), or never sent — never imitated. It joins an answer to a
record only when it can prove they are the same piece of work: an app can send
`X-Grid-Ref: zendesk:12345` with the request, and failing that the reference is read out of the
prompt — but only when exactly one candidate id is there, because a wrong join teaches the model
somebody else's answer. **A person's verdict is never overwritten by this.**

`autopilot` is one cycle: check the machine is free, build tonight's dataset from captured work,
refuse below `--min-examples` (120 by default — waiting is the correct outcome, not a failure),
train, prove it, and serve it only if it won. `--history` prints the past cycles instead. `nightly`
is the same shape for a dataset someone prepared rather than one captured.

`schedule on` installs a real per-user job — a **LaunchAgent** on macOS, a **`systemd --user`
timer** on Linux — after smoke-testing that the command can start at all; `off` deletes what it
wrote. It refuses to take over a job another folder owns (use `--name` to run several models on one
machine), and where there is no per-user scheduler it prints the cron line instead of pretending it
installed one. Output goes to `autopilot.log` beside the model.

**Training waits for the machine to be free** — mains power and an idle keyboard, checked in code.
`--ignore-host` overrides it for a run you are watching.

### For people who do not use a terminal

`grid train web` is the same engine with none of the vocabulary: pick the job, upload an export,
tick what a good answer looks like in plain language (it generates real Python graders), pick
machines, watch the curve, and see a before/after card whose "start using this" button only exists
if the model won. It writes an ordinary `grid-train.toml` and launches an ordinary `grid train`
subprocess, so the two surfaces cannot drift into separate products.

It binds to loopback by default. `--host 0.0.0.0` shares it with colleagues and prints a link with
a code in it — that page shows real customer data and can start jobs on the machine, so on a shared
network only that link works (`GRID_TRAIN_WEB_TOKEN` pins the code across restarts).

`grid train ui` is the read-only dashboard: every run, its reward and held-out-eval curves.

### Data to start from

`grid train packs` lists bundled starting points for business data — `support-replies` and
`sales-triage` — and `grid train init --pack support-replies` installs one (config, a prepare
script, graders, and sample rows) into `./<pack>/`. `grid train init` on its own writes a starter
`grid-train.toml`. The browser flow offers four jobs, including sorting work into your own
categories and anything else your team answers in writing.

`grid train pull zendesk|hubspot` fetches the examples instead of asking someone to export them.
The token comes from `ZENDESK_API_TOKEN` / `HUBSPOT_ACCESS_TOKEN`, is never written to disk and
never appears in a URL or an error; what lands on disk is raw rows, which go through exactly the
same preparation and the same refusals as an uploaded file.

### Where it runs

`grid train where` prints the grids training can use in **both modes** — a LAN grid found on this
network, and a hosted one you are signed in to — with the honest asymmetry: through the relay the
trainer **cannot push weights back** to NAT'd nodes, so relay training keeps its serving machines
reachable or accepts slower off-policy learning. `grid train convert-adapter` moves a LoRA adapter
between the torch/peft and MLX layouts, so a trainer on one backend can feed nodes on the other.

Runs land in `~/.grid/artifacts/train/`; browser workspaces in `~/.grid/train-workspaces/<slug>/`,
each holding the upload verbatim, the generated `rewards.py`, the `grid-train.toml`, and the run.

See [ADR 0019](./adr/0019-rl-training-plane.md) for the decisions and the honest limits,
[start-on-a-mac](./start-on-a-mac.md) for the twenty-minute path from nothing to a trained model,
[two-node-training](./two-node-training.md) for the smallest real fleet, and
[`train/README.md`](../train/README.md) for what reinforcement learning is doing here at all.

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
them). `--grid` follows the usual selection (active grid when omitted) and the call uses the grid's
per-grid access token.

`set` can also record optional model **metadata** on the same relay endpoint — `--name` (display
name), `--maker` (vendor), `--status` (e.g. `available`), and `--context-length` (max tokens). Each is
sent only when given, so a rates-only `set` stays minimal and doesn't clobber metadata set earlier.

`set` requires the engine to be **joined and serving the model** — the relay rejects a price for a
model you aren't currently serving (`grid join` first). `rm` does not (you can clean up a price after
`grid leave`). `show` lists the grid's models and prices. In `local` mode the command exits with
guidance to switch.

## Task

```
grid task create --prompt <text> [--project <name>] [--grid <grid>] [--json]
grid task get    <task-id> [--grid <grid>] [--json]
grid task follow <task-id> [--after-seq <n>] [--grid <grid>] [--json]
```

**Remote-only.** Hand the grid a coding task and read the result back later. Unlike a chat request,
a task **outlives the command that created it**: `create` returns immediately with an id, a provider
claims the task from the grid's durable queue and runs it, and `get` reports where it got to. You can
close your laptop in between.

`follow` watches the task's progress as it happens, rather than polling `get`. The provider publishes
events while it works and the relay keeps them in one **append-only log per task**, so `follow` is
resumable: every event carries a sequence number, and a stream that drops is reattached at the last
one seen — nothing is lost and nothing is repeated. `--after-seq <n>` attaches at a cursor by hand
(the default, `-1`, means from the very start); `--json` emits one `{"seq": …, "event": {…}}` object
per line for a script to consume.

`follow` exits with the task's own outcome — `0` for `completed`, non-zero for `failed` /
`timed_out`, or if the stream ended without ever saying how the task finished.

The log is **one sequence for the task's whole life**, including across a retry: a reattached client
never finds that its cursor has come to mean something else.

`--project` groups tasks that share a workspace (default: `default`). **One task runs per project at
a time** — creating a second one while the first is still `preparing`, `queued` or `running` is
refused, so a project's tasks are strictly sequential and each starts from the last one's result.
Use different `--project` names to run tasks in parallel.

A task's `state` is `queued` (waiting for a provider), `running` (claimed), or one of the terminal
`completed` / `failed` / `timed_out`. `get` prints the result on success and the error on failure.

`follow` refuses a task that is past its deadline but never finished (`410`), rather than holding a
stream open on work nothing will advance — distinct from a task that does not exist (`404`) or
belongs to someone else (`403`).

Serving tasks is **opt-in on the provider side** and off by default: an engine claims tasks only when
started with `GRID_TASKS=1` in its environment (`GRID_TASKS=1 grid join …`). A provider without it
serves inference exactly as before, and the two loops are independent — neither can stop the other.

### What a provider actually runs

A claimed task spawns **Claude Code** in print mode against the project's workspace, and its
`stream-json` output is republished as the task's events while it runs: `task.session` (the
conversation id), `task.tool_use` (a tool name and the file it targets), `task.output` (what the
agent says), `task.stderr`, and `task.result`. Anything credential-shaped is stripped before an
event leaves the provider.

The agent authenticates with **the provider's own Claude subscription** — not with the grid token,
and not with the requesting user's. Nothing about the grid is put into its environment.

The task runs with `--permission-mode bypassPermissions` by default: print mode cannot answer a
permission prompt, so any narrower mode silently denies the tools a coding task needs. This assumes
an **internally operated fleet** ([ADR 0032](./adr/0032-a-task-is-not-an-inference-transaction.md)) —
a provider runs a prompt written by someone else, with its own credentials, on its own machine.

The environment variables that tune a provider, all optional:

| variable | default | what it does |
|---|---|---|
| `GRID_TASKS` | off | `1` to claim tasks at all |
| `GRID_TASK_ROOT` | `/var/grid` | root of the workspace tree. **Every provider in a grid must agree** — Claude Code derives a session's transcript directory from the working directory, so a provider using a different root cannot resume a session another one started |
| `GRID_TASK_TIMEOUT_SECONDS` | `3600` | how long one agent run may take before the provider gives up on it |
| `GRID_TASK_PERMISSION_MODE` | `bypassPermissions` | any mode `claude --permission-mode` accepts |
| `GRID_TASK_CLAUDE_CONFIG_DIR` | unset | a fixed `CLAUDE_CONFIG_DIR` for every task this provider runs. Fixed **per provider**, never per user — a fresh config directory has no credential of its own and the agent will refuse to start |

The default root `/var/grid` needs privileges an ordinary account does not have. **Do not reach for
`sudo` first** — point `GRID_TASK_ROOT` at a directory the provider's own account can write, or
create `/var/grid` once and chown it. Claude Code declines to bypass permission checks when it
believes it is running as root, so a provider started with `sudo` can fail every task it claims; the
agent's own message comes back as the task's error, so check `grid task get <id>` if tasks fail
immediately. Whatever root is chosen, **every provider in the grid must use the same one**.

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
`auto` works on `chat/completions` and, for codex API engines, the Responses endpoint (see
[ADR 0016](./adr/0016-auto-routing-responses-dialect.md)); the legacy `completions` endpoint and an
`X-Target-Provider` header each reject it, and media models are never candidates.

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
grid engine install llama.cpp [--from-source] [--target-sm <sm_XX>]   # default text engine
grid engine install comfyui                    # default media engine
grid engine pull <bundle>                      # ComfyUI media bundle: image_generation, image_editing, i2v
grid engine status [--port 8188]               # ComfyUI: installed, its venv, output dir, bundles, running?
grid engine start [--port 8188] [--detach]     # start ComfyUI (blocks unless --detach)
grid engine stop                               # stop it
grid engine ls [grid] [--json]                 # live engines joined to a grid (same view as grid engines)
```

Grid has no inference engine of its own. These commands install open-source default
engines so a bare machine can join a grid without Ollama, LM Studio, or vLLM.
`--from-source` builds llama.cpp locally (Metal on macOS, CUDA on Linux NVIDIA) instead of
downloading a release, and `--target-sm` pins the CUDA architecture for that build. The
`status`/`start`/`stop` trio operates the built-in ComfyUI media engine.

## Agent Setup

```
grid agent install hermes | codex [--force]   # install an agent CLI (no Homebrew, no admin rights)
grid agent status                             # whether each is installed, and where
```

Mode-agnostic and grid-independent: these install the agent CLIs a machine may want alongside a
grid, into Grid's own prefix, and report where they landed.

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

And to teach a model of your own on the same machines:

```bash
grid train doctor                     # what this computer can do now
grid train web                        # or: grid train init --pack support-replies
```
