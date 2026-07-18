# Codex quickstart — serve your ChatGPT subscription, use it from the Codex CLI

This walks the whole path end to end: join your ChatGPT/Codex **subscription seat** to a grid
(including on a headless box), verify it is serving, then point the external **Codex CLI** at the
grid and run it. Reference detail lives in [cli.md](./cli.md) ("A subscription as an API engine")
and [ADR 0015](./adr/0015-codex-subscription-engine.md).

Two roles appear below. The **provider** owns the ChatGPT/Codex plan and serves it. The
**consumer** uses the served models from a Codex app. They can be the same person on the same
machine — the steps don't change.

## What you need

- The `grid` CLI installed, in **remote** mode, signed in, with an active grid:

  ```bash
  grid login                 # device-code sign-in to the hosted relay
  grid ls                    # grids you can reach
  grid use <name>            # pick the active grid
  ```

- A ChatGPT/Codex subscription (the provider's own account — grid never bills it, jobs spend its
  monthly Codex allowance).
- Codex engines are **remote-only**: there is nothing to join in `local` mode.

## 1. Join — sign in to your subscription and serve it

One command does the OAuth sign-in, the seat probe, and starts serving:

```bash
grid join --api codex
```

What happens, in order:

1. **Sign-in (first time only).** Your browser opens the vendor's authorize page; approve it and
   the CLI catches the redirect on `localhost:1455`. The URL is also printed in case the browser
   didn't open. You have ~10 minutes before the sign-in code expires.
2. **One free probe.** The CLI lists the seat's live models — proving the seat works and this
   machine's egress IP isn't blocked (Cloudflare tends to challenge datacenter/VPS addresses; a
   blocked join is refused naming the cause).
3. **Serve.** The engine registers the seat's tier-verified models (∩ its live set) and starts
   forwarding in the background — the command returns and you can close the terminal.

**Already signed in before?** The stored seat is reused silently — no browser, no prompt. A
re-join that changes nothing performs **zero** vendor calls. If the vendor has since rejected the
stored seat, an interactive run falls straight into one fresh sign-in.

Notes:

- "Signed in" means signed in **through grid**. Grid never reads or writes the Codex CLI's own
  `~/.codex/auth.json` — being logged in to the Codex app does not carry over, and grid's seat
  (stored in `~/.grid/api_keys.toml`) survives `grid logout`.
- `-m codex:<name>` narrows the served set; omitted, the join serves the seat's whole verified
  tier. `grid catalog --api codex` prints the per-tier table.
- If port 1455 is busy (usually the real Codex CLI or Codex Desktop signing in), grid falls back
  to the paste flow below.

### Headless box (no browser): `--no-browser`

```bash
grid join --api codex --no-browser
```

The CLI prints the authorize URL instead of opening anything:

1. Open the printed URL on **any** machine with a browser and approve the sign-in there.
2. The browser lands on a `http://localhost:1455/...` URL that **fails to load — that is
   expected**. Copy the full URL from the address bar.
3. Paste it into the waiting prompt on the headless box.

Same ~10-minute deadline; a timed-out or abandoned sign-in saves nothing — just re-run the
command for a fresh URL.

`--no-browser` still needs an **interactive terminal** to paste the redirect URL into — a box with
no TTY at all (systemd, cron, CI) can't sign in this way. Sign in from a machine that has a terminal.

## 2. Verify it is serving

```bash
grid                       # overview: engines + models on the active grid
grid models                # the codex:* names consumers will use
grid engine ls             # live engines, including kind "codex"
```

You should see `codex:*` models (e.g. `codex:gpt-5.4-mini`). To stop serving later:

```bash
grid leave --engine codex
```

This stops the engine but keeps the stored seat, so the next `grid join --api codex` is one
command with no sign-in.

## 3. Get the env values

`codex:*` models serve the vendor's **`responses` endpoint only** and are used from an external
Codex-compatible app — `grid chat` refuses them by design. The app needs the same two values every
OpenAI SDK needs; one command prints both (the grid must be up, and your sign-in must be a member
of it):

```bash
grid info --env
```

```bash
export OPENAI_BASE_URL="https://<your relay>/relay/v1"
export OPENAI_API_KEY="<your access token>"
```

This is the one command that prints your token (like `gh auth token`). A consumer on a different
account runs `grid login` + `grid use <name>` themselves and captures **their own** values — the
token is per-member, per-grid.

## 4. Point the Codex CLI at the grid and run it

Add a provider to `~/.codex/config.toml` (keys verified against `codex-cli 0.144.2`):

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

Then export the key under the name the config declares, and run:

```bash
export GRID_API_KEY="<your access token>"    # OPENAI_API_KEY from `grid info --env`
codex "explain this repo"
```

(The same keys can hang off a `[profiles.<name>]` block instead of the config root if you don't
want to change the app's default provider.)

## What to expect on this path

- **Requests leave the grid for the vendor** (OpenAI), forwarded by the member whose seat serves
  them — and **spend that member's monthly Codex allowance**.
- **The vendor is forced stateless**: `store: true`, `previous_response_id`, and `conversation`
  are refused up front, so every turn resends the full history — exactly how the Codex CLI
  already behaves. Long sessions are bounded by the relay's request body-size cap.
- **No output-token ceiling**: the vendor's codex backend accepts no cap parameter (and no
  `temperature`); the relay refuses the cap spellings up front so you learn that instead of being
  silently billed for an uncapped response.
- **Switching models inside the app's `/model` picker writes bare vendor names** (`gpt-5.4-mini`,
  no `codex:` prefix) into its config — the relay aliases a bare name onto the `codex:*` engine
  that serves it, so a picker switch keeps working **for the models this grid actually serves**.
  The picker also lists models the seat isn't entitled to (a free seat, for one, doesn't get
  `gpt-5.6-sol`); picking one the seat can't serve returns `No providers available for this model`
  — `grid models` is the authoritative set. Editing `config.toml` (or `-m`) is the reliable switch.
- A vendor error (401/429/5xx) surfaces as that job's error with the upstream status — it never
  touches your grid sign-in and never unregisters the engine.

## Let the grid pick the model — `model = "auto"`

If the grid **owner** has enabled auto-routing (`grid router enable`, with at least one advisor
set), a consumer can hand the model choice to the grid instead of naming a slug. Use the **same**
provider block as above with one line changed — `model = "auto"`:

```toml
model = "auto"                          # the grid picks among the codex models your seat serves
model_provider = "grid"
model_context_window = 272000           # pin it — the served model can differ from any one slug,
                                        # and the Codex CLI falls back to this on an unknown slug

[model_providers.grid]
name = "Autonomous Grid"
base_url = "https://<your relay>/relay/v1"   # OPENAI_BASE_URL from `grid info --env`
env_key = "GRID_API_KEY"                     # the env var the app reads your api key from
wire_api = "responses"                       # mandatory — codex engines speak the responses endpoint
supports_websockets = false                  # the grid relay streams HTTP SSE, not WebSocket
```

What the grid does with `auto`:

- It picks among the codex models your seat actually serves — a trivial prompt routes to a small
  model, a demanding one to the flagship, and an image request only considers vision-capable models.
- The response's `model` field names the model that **actually served** (never `auto`), and two
  response headers report the pick: `X-Grid-Routed-Model: <real model>` and `X-Grid-Router: ranked`
  (an advisor ordered the candidates) or `fallback` (a deterministic local pick, because no advisor
  was reachable). Billing records the chosen model, at that model's rates.
- Because the served model can differ from any single slug, **pin `model_context_window`** — the
  Codex CLI keeps working across picks by using it as fallback metadata for a slug it doesn't know.
- Send bare `auto`, not `codex:auto` (the latter returns a 400 that tells you the name is `auto`).
  If the owner hasn't enabled routing, an `auto` request returns a clear *"auto routing is not
  enabled on this grid"* error — name a `codex:*` model instead, or ask the owner to enable it.

Auto-routing is the grid owner's switch, the same one that governs the chat path; see
[ADR 0016](./adr/0016-auto-routing-responses-dialect.md) and the `Router` section of
[cli.md](./cli.md#router).
