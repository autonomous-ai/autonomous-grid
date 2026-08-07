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

## Project

```
grid project create --name <name> [--grid <grid>] [--json]
grid project list [--grid <grid>] [--json]
grid project member list   <project-id> [--grid <grid>] [--json]
grid project member add    <project-id> --email <address> [--grid <grid>] [--json]
grid project member remove <project-id> <member-key> [--grid <grid>] [--json]
grid project wip reset     <project-id> <member-key> --commit <oid> [--grid <grid>] [--json]
```

**Remote-only.** A project is the long-lived workspace a task runs in, and the git repository that
holds it. It has **members**, and it is addressed by **id** — `create` prints the id, `list` shows
every project you are a member of, and that id is what `grid task create --project` takes.

A name is unique **per owner**, so it is not an address someone else can use: two people can both
have a project called `acme` and they are different projects. That is why the wire carries an id and
`--project` takes one.

`member add` takes an **email**, and the person must already be a member of the grid — someone who
has never signed in to it cannot be admitted to a project on it. `member remove` takes a **member
key**, which `member list` prints: a member's underlying id contains colons, which are illegal in
both a git ref name and a path segment, so the key is the form that can be said everywhere.

Only the project's **owner** can add or remove members. Removal takes effect on that person's very
next request — they stop being able to clone the repository or create tasks in it immediately.

Someone who is not a member of a project gets **404** from every one of its endpoints, including its
git plane — not 403, because the id is the only thing standing between one team's source and
another's, and a 403 would confirm the project exists.

### Each member has a WIP branch, and `main` is the release branch

A task is cut from — and, on success, fast-forwards — **`wip/<member-key>`**, the asking member's own
branch. `main` is not touched by a task at all. That is what lets several people work in one project
without their results racing for one ref.

A project's `main` therefore has to **exist before its first task**: a project that has just been
created has no commits, and creating a task in one is refused with a message saying so. Give it a
first commit (push `main` while the project is idle) and tasks work from then on.

`wip reset` moves a member's WIP branch back to a commit you name — the way out of the one state
nothing else can undo. If a task's result is written into git but its completion is then interrupted,
that member's branch is left ahead of the task branch, every retry is refused as a non-fast-forward,
and their *next* task would be cut from the lost attempt's work. `grid task get <id>` prints the
`base_commit` a task was cut from, which is usually the commit to reset to.

Any member may reset any member's branch — someone who leaves the team otherwise strands everything
they never merged, since nothing can adopt or transfer their branch. It is refused while that member
has a task running, because moving the base out from under an attempt in flight would fail its
result for a reason nobody caused.

## Task

```
grid task create --prompt <text> [--file <local>[:<dest>]]… [--project <id>] [--grid <grid>] [--json]
grid task get    <task-id> [--grid <grid>] [--json]
grid task follow <task-id> [--after-seq <n>] [--grid <grid>] [--json]
grid task fetch  <task-id> [--into <dir>] [--grid <grid>] [--json]
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

`--project` takes a project **id** from `grid project list`. With no `--project`, the task runs in
your own project called `default`, created on first use — the name is resolved here, by the CLI,
against projects you own, and only an id ever reaches the relay.

**One task runs per person per project at a time** — creating a second one while your first is still
`preparing`, `queued` or `running` is refused, so *your* tasks in a project are strictly sequential
and each starts from the last one's result. Other members are unaffected: a project with five people
in it runs five tasks at once, one each. Use different projects to run your own tasks in parallel.

### Sending files with a task

`--file` uploads a file with the task; repeat it for more. `--file ./bug.py` lands as `bug.py`;
`--file ./local/conf.toml:config/conf.toml` places it at a path you choose.

The files travel in the **same request** as the prompt, and that ordering is the guarantee, not a
convenience: each project is a git repository the relay owns, and creating a task cuts `task/<id>`
from your `wip/<member-key>` branch, commits the input, and only *then* makes the task claimable.
"The task exists" and "its input is in git" are one event, so a provider can never claim a task and
check out before the files arrive — which would run the agent against missing input with nothing to
say why the answer is wrong. `get --json` reports the `input_commit` the files landed on.

**Filenames are validated at the relay and never repaired.** A path that is absolute, contains
`..`, or names anything inside `.git/` or `.grid/` is refused and *nothing* is committed — a file
under `.git/hooks/` would execute on the provider the moment it checked the branch out. The check is
case- and Unicode-insensitive, so `.GIT` and a zero-width-space spelling are refused too. Symlinks
are refused by this CLI before upload, because uploading what one points at would send a file you
never named; the relay commits every file as a regular file, so a client that is not this CLI cannot
create one either.

Limits, refused with the number stated rather than truncated: **200 files**, **5 MB per file**,
**20 MB in total**.

### Getting the result back

`fetch` puts a finished task's files on disk: `grid task fetch <task-id>` lands them in `./<task-id>`,
or wherever `--into` names. It needs `git` on the machine you run it from.

The project is a git repository the relay serves over HTTP, and **your grid token is the whole
credential** — no SSH key is provisioned for anyone, at either end. The token is handed to `git`
through its environment, so it never appears in a process listing and is never written into the
fetched clone's `.git/config`; a result directory is something you can hand around without handing
over a year-long credential with it. If you'd rather drive git yourself, the remote is
`<relay>/relay/v1/git/<project-id>` (`get --json` reports the `project_id`) with
`http.extraHeader` carrying `Authorization: Bearer <your grid token>`.

`fetch` refuses a task that has not finished: until the provider pushes, the branch still holds only
the files you uploaded, and handing those back as "the result" would be a wrong answer delivered
confidently. It also refuses `--into` a directory that already has files in it and was not made by
`grid task fetch` for this same project, rather than checking out over your own work — the check is
a marker the command writes itself, not the presence of a `.git`, because your own repositories have
one of those too.

**On success the project's `main` moves to the result; on failure it does not.** That is what makes
`main` a known-good state and the base the project's next task is cut from. A failed attempt still
commits and still pushes its own `task/<id>` branch, so you can fetch it, read what the agent did
before it broke, and cherry-pick what was right — `get --json` reports the `result_commit` for both
outcomes.

**What a provider may see and write is decided by the lease, not by trust.** A provider running one
of your tasks is shown that task's branch and the project's `main` — not the branches of your other
tasks, including failed ones whose work never reached `main`. It may push only
while it currently holds that task's lease, and only that task's own branch; the moment the lease
ends, the relay refuses it — without the provider needing to be told it lost. You cannot push to a
project while a task is active on it, and neither you nor a provider can move `main` by hand: only
the relay advances it, and only for a task it saw succeed.

A task's `state` is `queued` (waiting for a provider), `running` (claimed), or one of the terminal
`completed` / `failed` / `timed_out`. `get` prints the result on success and the error on failure.

`follow` refuses a task that is past its deadline but never finished (`410`), rather than holding a
stream open on work nothing will advance — distinct from a task that does not exist (`404`) or
belongs to someone else (`403`).

Serving tasks is **opt-in on the provider side** and off by default: an engine claims tasks only when
started with `GRID_TASKS=1` in its environment (`GRID_TASKS=1 grid join …`). A provider without it
serves inference exactly as before, and the two loops are independent — neither can stop the other.

### What a provider actually runs

A claimed task first brings the workspace to the task's input commit: it fetches the task
branch from the relay over the same git-over-HTTP front the client uses, and resets the workspace to
it exactly, so a previous task's leftovers can never be mistaken for this task's input. **`git` must
be installed on the provider.** The one directory spared is `.grid/`, which is the provider's own
state — it is why nothing may be uploaded there, and it is excluded from the result commit too, with
one exception: `.grid/agent/<member>/` holds that member's conversation and *is* committed (see
[Continuing a conversation](#continuing-a-conversation) below). If the input cannot be checked out
the task fails **without spawning the agent** — an agent run against input that never arrived
produces a confidently wrong answer.

**A workspace belongs to a project *and a member*, not to a project alone** —
`<root>/projects/<project_id>/<member>/workspace`. A project can have several people in it, and
bringing a workspace to a task's input starts with `reset --hard` and `clean -ffdx`: sharing one
directory between two members would mean each one's task wiped the other's, and it is what keeps
their conversations apart, since Claude Code derives a session's transcript directory from the
working directory. The relay tells the provider which member a task belongs to on the claim; a
provider that is not told **refuses to run the task** rather than guessing, because a guess would
put the conversation somewhere the next task never looks.

When the agent exits, the provider commits the workspace and pushes `task/<id>` — for a failed run
as well as a successful one — and reports the commit it pushed. The relay checks that against the
branch it actually holds, so a push that silently failed cannot be recorded as a finished task. If
the push cannot be made at all, the provider deliberately reports **nothing**: a terminal state is
one nothing retries, so the task is left `running` for its lease to lapse and another provider to
pick it up, and the git error is published to the task's event stream instead.

### When a provider disappears

**A task survives the provider running it.** While a provider is serving a task it keeps renewing
that task's lease, and it renews only while the agent process it started is still alive — not while
the machine is merely reachable, which would let a task whose agent had died hold its project
forever. A task that produces no output for ten minutes is not assumed to be stuck: silence is
normal while a build or a test suite runs, so nothing infers a hang from quiet.

If the renewals stop — the provider was killed, lost power, or its agent died and it could not
report — the lease lapses and the relay **reclaims** the task: it goes back to the queue, its branch
is reset to exactly the input you uploaded, and the next provider to claim it starts from there. You
see this in `grid task follow` as a retry line, and it says what the reset does and does not cover:
**changes the lost attempt made in git are undone; anything it did outside git is not.** The event
log keeps one sequence across the whole task, so a client attached across a retry loses nothing and
never sees its cursor come to mean something else.

Retries are capped (3 attempts by default). A task that exhausts them fails with
`retries_exhausted`, and its project unlocks so you can create the next task on it. Separately, any
task that outlives its deadline — including one sitting `queued` on a grid with no provider at all —
is ended as `timed_out`, so a project can never be locked indefinitely by work nothing is doing.

One case worth knowing: if the agent clones another repository into the workspace, git records it as
a submodule reference whose objects the relay does not have, and the push fails with git's own
(fairly opaque) message. It routes to the retry path above rather than losing anything.

Once the workspace is ready the task spawns **Claude Code** in print mode against it, and its
`stream-json` output is republished as the task's events while it runs: `task.session` (the
conversation id), `task.tool_use` (a tool name and the file it targets), `task.tool_result` (how
that call ended), `task.output` (what the agent says), `task.stderr`, and `task.result` (the agent's
own account of the run — turns and duration). `follow` prints a `task.tool_result` **only when the
call failed**: one arrives for every tool call, and a task makes hundreds, so narrating the
successful ones would bury the output under an id nobody can act on. Two more bracket the start of a
follow-up task:
`task.session_resumed` (this task is continuing the project's conversation) and
`task.session_reset` (it could not, and why). Anything credential-shaped is stripped before an event
leaves the provider.

The agent authenticates with **the provider's own Claude subscription** — not with the grid token,
and not with the requesting user's. Nothing about the grid is put into its environment.

### How much a provider takes on

Two things bound it, and whichever runs out first is the one that stops a provider claiming.

`GRID_MAX_TASKS` is how many tasks that provider runs at once — **1 by default**, so turning task
serving on does not also change how much of the operator's subscription it spends. There is no upper
limit: the number is the operator's to choose, and a machine that runs out of threads says so and
keeps the workers it did start.

> ⚠️ **Anything above 1 is unverified.** No value greater than 1 has been run against a real relay,
> and the specific unknown is **two Claude Code children sharing one `CLAUDE_CONFIG_DIR`** — the
> issue-01 spike listed it as an open question and never measured it. The parts that can be reasoned
> about hold: a workspace belongs to a (project, member) pair, and the relay allows only one active
> task per project, so two concurrent tasks are always in different directories. What has not been
> observed is the agent itself under concurrency. Raise this only on a provider you can watch, and
> expect to be the first to find out.

The other bound is the subscription itself, and nobody configures it. Every agent child a provider
spawns draws on the same Claude subscription, so that subscription's rate limit — not memory, not
CPU, and not the inference `max_concurrency` — is the real ceiling. Claude Code reports it in the
same stream the provider is already reading, so the provider learns its own pressure from work it was
doing anyway. When a run says the window is spent, the provider **stops claiming new tasks and
starts again when the vendor's window resets**; you see it as a `task.rate_limit` line in
`grid task follow`, which is what explains a next task that sits queued.

Three properties of that are worth knowing:

- **Tasks already running are never interrupted.** The limit is consulted before a claim and nowhere
  else, so hitting the wall mid-run costs nothing that was already in flight.
- **A provider that cannot read the signal keeps serving.** An unreadable payload, a status this
  build has never seen, a reset stamp that is missing or absurd — none of them stop a provider, and
  an unrecognised status is named in the provider's log so the gap can be closed. A fleet that
  quietly withdrew because the vendor added a status string is a far worse failure than one that
  claims a task and reports the refusal.
- **Task capacity and inference capacity are independent.** A provider with no task headroom left is
  still a perfectly good provider of inference, and nothing about this reaches the heartbeat or the
  relay's routing. A provider saturated with inference can still take a task.

### Watching the workspace change

`follow` also shows the **shape of the working directory** as the agent builds it, on a `task.tree`
event the provider folds into the heartbeat it already sends. This is the only live view there is:
the result is committed at the end of the task, so between claim and terminal the repository holds
nothing new and files cannot be downloaded mid-run.

```
[12] workspace: 3 files
      README.md
      src/main.py
      tests/test_main.py
[31] workspace: 4 files (+1)
      + src/util.py
```

The first snapshot is listed in full and later ones show only what changed. What you see respects
**your project's own ignore rules** — a `.gitignore`d `node_modules/` never appears — and is capped,
so a very large workspace arrives truncated and says so (`workspace: 12431 files (truncated, showing
500 of 12431)`); a truncated snapshot is listed rather than diffed, because a path missing from a
prefix has not necessarily been deleted. A snapshot is only sent when the tree actually changed, so
an idle task produces no tree lines at all, and `--json` carries every path rather than the summary.

### Continuing a conversation

A project's second and later tasks **continue the first one's Claude Code session** instead of
starting cold, and they do so even when a different provider serves them.

The mechanism is the repository. Claude Code keeps a session's transcript — and the agent's own
`memory/` — in a folder named after its working directory, and it writes through a symlink, so the
provider points that folder at `.grid/agent/<member>/` inside that member's workspace. The
transcript is then carried by the ordinary result commit, with no separate synchronization step: a
provider that has done nothing but clone the repository has the conversation the moment it checks
the task out. This is why every provider must agree on `GRID_TASK_ROOT` — the folder's name is
derived from the *absolute* path, so a different root is a different conversation.

A conversation belongs to one member, so a follow-up task continues **your own** and never a
colleague's. It is not private, though: the transcript travels in the ordinary commit, so once a
branch has been promoted and merged, everyone working in the project has a copy of everyone's.

Two consequences worth knowing:

- **A failed task's conversation does not carry forward.** `main` only advances on success, and the
  next task is cut from `main`, so a project resumes from its last *successful* state rather than
  from a broken one.
- **If the transcript is missing or unreadable, the task starts a fresh session rather than
  failing** — and says so, in three places, deliberately: as a `starting a fresh session (…)` line
  in `grid task follow`, in the task's durable event log, and on the task itself as
  `session_reset_reason`, which `grid task get --json` reports. The last of those is the one that
  survives everything: progress events stop if the provider loses the task's lease mid-run, so the
  reason is also carried on the final report and recorded by the relay. The commonest cause is the
  previous task having failed.

The conversation is written only by the provider: the relay refuses any uploaded file under
`.grid/`. The provider's credential never goes near it — the config directory is required to be an
absolute path outside the workspace, and a provider refuses to run rather than place it inside the
repository it pushes.

If `GRID_TASK_CLAUDE_CONFIG_DIR` names a directory that already holds a real (non-symlink)
transcript folder for a project — which is the case for any provider that served tasks before this
existed — the task fails with a message naming the exact path. Move or remove it once; grid will not
delete a conversation it did not create.

The task runs with `--permission-mode bypassPermissions` by default: print mode cannot answer a
permission prompt, so any narrower mode silently denies the tools a coding task needs. This assumes
an **internally operated fleet** ([ADR 0032](./adr/0032-a-task-is-not-an-inference-transaction.md)) —
a provider runs a prompt written by someone else, with its own credentials, on its own machine.

**A task's files cannot configure the agent that reads them.** Print mode skips Claude Code's
workspace-trust dialog, so a `.claude/settings.json` arriving with a task would otherwise run its
hooks on the provider before the model had said anything. Two rules close that
([ADR 0033](./adr/0033-a-project-has-its-own-members-so-main-stops-being-the-base.md) D-f):

- the agent is spawned with `--setting-sources user --strict-mcp-config`, so only the *provider's
  own* settings load — never the workspace's, and never its `.mcp.json`;
- the relay refuses an uploaded path under `.claude/`, or named `.mcp.json`, the way it already
  refuses `.git/` and `.grid/`. `--file ./x.json:.claude/settings.json` comes back as a 422 naming
  the directory.

**Instructions are not configuration and are not blocked.** A `CLAUDE.md` in the workspace, and
`.claude/agents/` and `.claude/skills/`, arrive with the repository and stay readable — an agent that
looks finds them. The line is narrower than "the agent ignores the repository": no *shell command*
runs before the model has said anything. Put per-task guidance in `CLAUDE.md`, not in a settings file.

⚠️ **A workspace `CLAUDE.md` is not loaded automatically.** Measured on Claude Code 2.1.223:
`--setting-sources user` also turns off project-memory discovery, so the repository's `CLAUDE.md`
reaches the model only if the agent opens it. Ask for it in the task prompt when it matters
("follow the repository's CLAUDE.md"). Recovering automatic discovery would mean loading the
repository's settings again, which is the hole this closes, so it is deliberately not done here.

This needs a Claude Code new enough to know `--setting-sources` — 2.1.221 is the oldest version
measured to know and honour it. An older one refuses the whole invocation (`error: unknown option`,
before it runs anything), so a provider on a stale binary fails every task with that message in
`grid task get <id>` rather than quietly running unprotected. **Upgrade the fleet's Claude Code
before upgrading grid.**

### What a task can reach on the provider

A task is arbitrary code execution as the provider's user — that is the product, not a flaw. What
the provider controls is the blast radius, in three layers
([ADR 0033](./adr/0033-a-project-has-its-own-members-so-main-stops-being-the-base.md) D-n).

**The agent's environment is an allowlist**, not a copy of the provider's. It gets `PATH`, `HOME`,
`SHELL`, `TMPDIR`, the locale, the TLS and proxy settings, and anything named `ANTHROPIC_*` or
`LC_*`. Everything else is dropped — the grid's own access token, cloud keys, CI tokens. Add what a
build genuinely needs with `GRID_TASK_ENV_PASSTHROUGH`. The operator's git configuration is dropped
too (`GIT_CONFIG_GLOBAL` and `GIT_CONFIG_SYSTEM` point at `/dev/null`), so a `core.hooksPath` in
`~/.gitconfig` cannot run against a repository that arrived over the wire — and an agent asked to
`git commit` fails for want of an identity rather than silently authoring as the operator.

**The agent runs sandboxed** — seatbelt on macOS, bubblewrap on Linux — with the policy delivered per
invocation, so a repository cannot weaken it. `$HOME`, `~/.grid` and the Claude configuration
directory are unreadable to the task; package-manager caches are re-allowed by name; the workspace
is the only writable place. Network egress is an allowlist of the usual package registries, and a
task that needs another host says so with a clear `curl` error — extend it with
`GRID_TASK_ALLOWED_DOMAINS`.

⚠️ **On macOS, a task cannot install dependencies.** The sandbox blocks the system service that
verifies TLS certificates, so anything using the system trust store fails — measured, `pip install`
returns `SSLCertVerificationError (OSStatus -26276)` even with `pypi.org` on the egress allowlist,
while `curl` to the same host succeeds. Providers are expected to run Linux, where the setting that
governs this does not apply; a macOS box used for development can set `GRID_TASK_SANDBOX=0` to run
tasks unconfined. Whether the Linux backend has an equivalent limitation is unmeasured.

**Run the provider as a dedicated unprivileged user.** This is the layer that actually bounds the
damage, and it is operational rather than code: the sandbox confines the commands the model runs, but
it does not confine Claude Code itself, which must be able to read the subscription credential. A
task can still take *that*. Give the provider its own account with its own `CLAUDE_CONFIG_DIR`, or a
container per task — the design suits it, since the workspace is a fixed path and all state travels
in git.

Stated plainly, because it follows from the relay's own authorization and not from anything above:
**any member of a grid can execute code on any provider in it.** A provider is authorized as a
provider, not for a particular project. That is a decision an internally operated fleet can make; it
should be made rather than inherited.

**Rollout order, and it is not optional.** On every provider, *before* upgrading grid:

```bash
apt install bubblewrap socat        # both — the sandbox needs socat as well as bwrap
curl -fsSL https://claude.ai/install.sh | bash   # Claude Code 2.1.221 or newer
```

Both halves fail closed, which is why the order matters rather than merely being tidy. The sandbox
is configured to refuse to start instead of running unconfined, so a provider missing either package
fails every task with `sandbox required but unavailable: … bubblewrap (bwrap) not installed, socat
not installed` — measured on Ubuntu 24.04. And a Claude Code too old for the sandbox settings would
accept them, ignore them, and report success, which is why the provider checks the version and
refuses to run at all below 2.1.221.

Nothing else is needed on Ubuntu 24.04. It restricts unprivileged user namespaces by default
(`kernel.apparmor_restrict_unprivileged_userns=1`), which stops the sandbox nesting the namespace it
uses to run each command — the provider handles that in the policy it sends, and it was measured on
a 24.04 host that a task can then read its workspace, install a dependency from PyPI, and still not
read a file outside the workspace. **Do not** turn that sysctl off to "fix" a task that fails to run
commands; check `bubblewrap` and `socat` first.

The environment variables that tune a provider, all optional:

| variable | default | what it does |
|---|---|---|
| `GRID_TASKS` | off | `1` to claim tasks at all |
| `GRID_MAX_TASKS` | `1` | how many tasks this provider runs at once. **Anything above 1 is unverified** — two Claude Code children sharing one config directory has never been measured (see [How much a provider takes on](#how-much-a-provider-takes-on)). No upper limit is imposed: the ceiling that actually binds is the provider's own Claude subscription, which is read at runtime rather than guessed at. A value that is not a positive whole number falls back to `1` and says so |
| `GRID_TASK_ROOT` | `/var/grid` | root of the workspace tree (`<root>/projects/<project_id>/<member>/workspace`). **Every provider in a grid must agree** — Claude Code derives a session's transcript directory from the working directory, so a provider using a different root cannot resume a session another one started |
| `GRID_TASK_TIMEOUT_SECONDS` | `3600` | how long one agent run may take before the provider gives up on it |
| `GRID_TASK_PERMISSION_MODE` | `acceptEdits` | any mode `claude --permission-mode` accepts, **except `bypassPermissions` while the sandbox is on** — the provider refuses that combination. Measured: with that mode the agent reads files outside its workspace even with the whole policy in force, because the `Read` tool runs inside Claude Code, which the sandbox does not confine |
| `GRID_TASK_SANDBOX` | on | `0` runs agents **unconfined** — no filesystem or network confinement, and the Claude Code version check is skipped with it |
| `GRID_TASK_ENV_PASSTHROUGH` | empty | extra environment variable names the agent may inherit, comma- or space-separated (e.g. a private registry token) |
| `GRID_TASK_ALLOWED_DOMAINS` | the common package registries | replaces the egress allowlist wholesale, comma- or space-separated. An empty value denies all network access to the task's own commands |
| `GRID_TASK_CLAUDE_CONFIG_DIR` | unset | a fixed `CLAUDE_CONFIG_DIR` for every task this provider runs. Must be an **absolute** path outside the workspace, or the provider refuses to run — the agent resolves a relative one against its working directory, which would commit the provider's credential into the user's repository. Fixed **per provider**, never per user: a fresh config directory has no credential of its own and the agent will refuse to start |

**Keep the root short.** On macOS the sandbox profile is built into a single command-line argument
that grows with the workspace path; past roughly 120 characters it exceeds the exec limit and every
command a task runs fails with `E2BIG`. The provider warns on stderr when it sees one that long.
`/var/grid` and anything like it is far inside the limit.

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
