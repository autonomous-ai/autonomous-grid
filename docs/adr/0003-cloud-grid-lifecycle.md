# ADR 0003 — Cloud grid lifecycle (`grid up` / `down` / `ls` / `info`)

Status: accepted (2026-06-27)

## Context

ADR 0001 set up modes, `state.json`, and mode-aware dispatch (cloud lifecycle verbs routed to a
stub). ADR 0002 filled sign-in and introduced `credentials.toml` (the session token plus the
per-grid `[[networks]]` bundles). This slice fills the cloud grid **lifecycle**: `grid up` /
`down` / `ls` / `info` against autonomous's hosted **cloud grids**, mirroring the LAN verbs the
user already knows — there are no separate create/start commands.

The reference client `grid-src/grid_cli/` (`control_plane.py`, `cli.py:cmd_network_*`) is the port
source, but it implements the **legacy self-host model** (`POST /v1/grid/networks`,
`lan_signaling_url`, a local Postgres/relay). We do **not** port it verbatim: per D11 the
lifecycle is repointed to the hosted **`/v1/grid/managed-networks/…`** API (the proprietary
relay/Postgres/billing run on autonomous's side; the CLI is only a client).

Hard invariant: LAN mode stays LAN-only, unauthenticated, stateless — unchanged. Cloud lifecycle
stays a **thin client**; no relay/Postgres/billing ships in-repo; tokens are never printed.

## Decisions

1. **The local grid registry is `credentials.toml [[networks]]` — `ls` is local, not a live
   list.** `grid ls` reads the bundles `grid login` already fetched (name + `network_type`); it
   makes **no** network call. `up` / `down` / `info` / `use` resolve a `name → network_id`
   against the same file. This keeps `ls` consistent with `grid use` (ADR 0002 already resolves
   the active grid against these bundles) and mirrors LAN `ls` (which lists local config without
   probing a server). **This supersedes D12's `ls = GET /v1/grid/networks`** — there is
   deliberately no list endpoint call. Trade-off: a grid created on the website (or by another
   machine) appears only after a re-`grid login`; that re-auth is the established refresh path
   (ADR 0002 §7). Resolution precedence mirrors LAN `select_grid`: positional `[name]` > active
   (`state.get_active("cloud")`) > sole grid; unresolvable → a clean `SystemExit`.

2. **Lifecycle authenticates with the account `session_token`, not the per-grid `access_token`.**
   create/start/stop/status are account-level operations, so they carry the session token
   (`credentials.require_session()`) and hit the **managed-networks** endpoints (D11):
   - `up` (create) → `POST /v1/grid/managed-networks` (body `name`, `network_type`)
   - `up` (start) / `down` → `POST …/managed-networks/{id}/start` | `…/{id}/stop`
   - `info` → `GET …/managed-networks/{id}/status`

   The per-grid `access_token` is for **consuming through the relay** and is untouched here. This
   is the clean 04 (lifecycle) / 05 (use-path) seam.

3. **`grid up` is create-or-start; creating requires an explicit name (a deliberate divergence
   from LAN).** `grid up <name>`: in the registry → start; not in the registry → create, then
   append the returned record (`network_id`, `name`, `network_type`, `signaling_url`, `status`)
   to `credentials.toml` so `ls`/`use`/`info` see it immediately. Bare `grid up` only **starts**
   the active/sole grid; with nothing to resolve it errors (`need a name to create: grid up
   <name>`). Unlike LAN it never auto-creates a grid named `home` — a cloud grid is hosted
   (carries a `plan`), so creating one silently under a default name is the wrong default.
   `--type` (choices `permissioned-public` (default) | `permissioned-providers`, per D11) applies
   on **create only**; passed on a start it is ignored with a one-line note.

4. **`grid down` stops; it does not delete.** `POST …/{id}/stop` — the grid persists in the
   registry and `grid up <name>` brings it back (mirroring LAN's "config kept"). There is no
   delete verb in v1; deletion/cancellation lives on the website (PRD Out of Scope).

5. **`grid info` maps the status response to grid vocabulary and hides proprietary internals.**
   `…/status` returns server-side fields including `server_pid` / `sync_pid` / `postgres` (D11);
   these are **not** surfaced. `info` shows `grid` (name), `type`, `status`, and `grid_url` —
   where `grid_url` is read from the API's `signaling_url` field but displayed under the
   LAN-symmetric key `grid_url` (the word "signaling" stays off the product surface, per the
   vocabulary discipline). Human and `--json` forms; **no token**.

6. **`info --env` and the per-grid token path are deferred to issue 05, but the design and a
   token-printing carve-out are recorded now.** Issue 04's `info` shows status only. The cloud
   `info --env` form (`OPENAI_BASE_URL="{signaling_url}/relay/v1"` +
   `OPENAI_API_KEY="{access_token}"`) needs the per-grid `access_token` and the relay base, which
   belong to the use-path slice (PRD issue breakdown item 5). When it lands, `info --env` is the
   **one deliberate exception** to "never print tokens": an explicit, user-requested disclosure
   of the caller's own token to their own shell — exactly like `gh auth token` /
   `gcloud auth print-access-token`. Everywhere else (`ls`, `info` without `--env`, all `--json`)
   stays token-free, as ADR 0002 §10 requires.

7. **`network sync` is dropped — it is not a CLI command.** The hosted relay self-syncs its
   allowlist/JWKS server-side (D13, PRD Out of Scope), so there is nothing to trigger or no-op;
   the command simply does not exist on the surface.

8. **Seam.** Cloud lifecycle handlers live in a new cloud-only `cli/cloud_grid.py`
   (`cmd_cloud_up` / `_down` / `_ls` / `_info`), wired into `dispatch.CLOUD_HANDLERS` in place of
   the `up`/`down`/`ls`/`info` stubs (the remaining gated commands stay stubs). `cloud/
   control_plane.py` gains `create_managed_network` / `start_managed_network` /
   `stop_managed_network` / `get_managed_network_status` (session-token Bearer, managed-networks
   URLs). `cloud/credentials.py` gains a thin `add_network(record)` (append to `[[networks]]`);
   the selection precedence lives in `cli/cloud_grid.py` because it needs `shared.state`, which
   `credentials.py` deliberately does not import. `--type` is added to the shared `up` subparser
   (LAN `cmd_up` ignores it). Tests go in `tests/test_lan_cli.py` via the existing
   `_mock_control_plane` (httpx `MockTransport`) + a seeded `credentials.toml`, driving
   `cli.main` in cloud mode, covering create / start / stop / list / status,
   create-requires-name, and secrets-never-printed.

## Consequences

- `ls`, `use`, and `info` resolve from one local source, so they never disagree; the price is the
  documented freshness gap (re-`grid login` to pick up grids created elsewhere).
- Issue 04 never touches a per-grid `access_token`; issue 05 owns relay consumption and
  `info --env`. The auth split keeps this slice purely account-scoped.
- A future reader sees no list-endpoint call behind `grid ls` — that is by design, recorded here,
  not an omission.
- Lifecycle on a grid the caller does not own returns a control-plane 4xx, surfaced as a clean
  `SystemExit` by the existing `_raise` helper.
- `grid info` cannot leak `server_pid` / `postgres` / `sync_pid`: the handler projects the status
  response onto a fixed grid-vocabulary shape rather than dumping it.
- A future gated command promoted to a real cloud handler replaces its entry in
  `CLOUD_HANDLERS`; the classification test keeps the partition total.
