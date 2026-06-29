# ADR 0002 — Cloud sign-in (`grid login` / `grid logout`)

Status: accepted (2026-06-27); amended 2026-06-29 — added Decision 11 (`grid sync`, issue 10)

## Context

ADR 0001 set up modes, `state.json`, and mode-aware dispatch with cloud as a stub. This slice
fills the first real cloud surface: **sign-in**. `grid login` runs the Google device-code flow
against autonomous's hosted control plane, fetches the caller's grid tokens, and persists them;
`grid logout` clears them. It introduces the credential store and the "you're not signed in"
gate that every later cloud command (request/info/members) will reuse.

The API contract is ported verbatim from the reference client `grid-src/grid_cli/`
(`control_plane.py`, `config.py`, `cli.py:cmd_auth_login/_browser_login`); only the thin client
(device flow + token fetch + TOML credential store) is taken — the proprietary backend
(relay, Postgres, billing) stays out.

Hard invariant: LAN mode stays LAN-only, unauthenticated, stateless — unchanged. Cloud mode is
*allowed* to reach the internet to the control plane; that is the feature.

## Decisions

1. **Cloud-only commands gated symmetrically (`cli/dispatch.py`).** A new `CLOUD_ONLY =
   {"login", "logout"}` set joins `AGNOSTIC` and `CLOUD_HANDLERS`; the three now partition every
   registered command (the classification test asserts coverage *and* disjointness). In cloud mode
   `CLOUD_ONLY` falls through to its real handler; in LAN mode it hits `lan_stub` — the mirror of
   the existing `cloud_stub`. The LAN gate is an **`elif`** after the `if mode == "cloud"` block:
   `dispatch` has no `else`, so a bare `if` would fire in cloud too and break login/logout there.

2. **Device-code flow, ported and trimmed (`cloud/control_plane.py`).** Three functions only —
   `start_device_login` → `POST /v1/grid/auth/device/start`, `poll_device_login` →
   `POST /v1/grid/auth/device/poll`, `fetch_tokens` → `GET /v1/grid/tokens?device_id=…`. `cmd_login`
   prints the sign-in URL + code, opens a browser (unless `--no-browser`), polls until `approved`
   (the approved poll carries `session_token` + `user.email`, so no separate `/me` call), then
   fetches tokens. Terminal poll states (`expired`/`consumed`/`denied`) and the `expires_in`
   deadline raise clear `SystemExit`s. The google-token / session-token / refresh / members / jwks
   surface is left out of this slice.

3. **Trust-boundary validation.** Fetched bundles are validated before persistence: any network
   missing `network_id` or `name` aborts with a clear error and writes nothing (ports grid-src's
   fail-loud guard). Login prints names and later `grid use` matches by name, so a nameless bundle
   must never reach disk.

4. **Credential store: `~/.grid/credentials.toml` (TOML, `0o600`).** Schema: `session_token`,
   `api_url`, `[user] email`, `[[networks]]` (the verbatim token bundle). There is **no
   `active_network` key** — the active selection lives only in `state.json` (single source of
   truth). A stable per-machine id lives in a separate `~/.grid/device.toml` so it **survives
   logout** (re-login keeps the same device identity).

5. **One hardened atomic-write primitive (`shared/jsonio.atomic_write_bytes`).** Rather than add a
   second TOML writer, both the JSON state file and the TOML credential store go through one
   primitive that creates the temp file with `0o600` and `fchmod`s it before any bytes land — no
   world-readable window, and umask-proof (`O_CREAT`'s mode is masked by umask; the explicit
   `fchmod` is not). `atomic_write_json` was refactored onto it with byte-identical output, so this
   also hardens `state.json` for free.

6. **Login never auto-selects an active grid (supersedes D10).** D10 had `grid login` write the
   active cloud grid; we deliberately don't. Login lists the available grids and instructs
   `grid use <name>`; selection is always explicit. This keeps `state.json` the lone owner of the
   active pointer and avoids guessing when the caller belongs to several grids.

7. **Re-login refreshes by re-auth; no silent refresh.** Re-running `grid login` performs a fresh
   device flow and overwrites the store (User Story 20). The per-network `refresh_token` is
   persisted for the later provider runtime, but no automatic token refresh ships in this slice
   (YAGNI). `grid logout` deletes `credentials.toml` and clears `active.cloud`; it keeps
   `device.toml` and does not change the mode; it is idempotent.

8. **Config: keep grid-src's env vars + defaults, evaluated at call time.** Only two values are
   env-read — `GRID_CONTROL_PLANE_URL` (default `https://api-grid.autonomous.ai`) and
   `GRID_WEBSITE_URL` (default `https://staging.autonomousdev.xyz`); `GRID_LOGIN_PATH` is a
   hardcoded constant. The sign-in URL is built from `GRID_WEBSITE_URL`; setting it empty falls
   back to the server's `verification_uri_complete`. Reading env in functions (not at import) keeps
   it monkeypatchable per test.

9. **Dependencies: `tomli-w` only, in base.** Writing TOML needs `tomli-w` (added to base
   `dependencies`, not an extra, so LAN-only use is unaffected); reads use stdlib `tomllib`.
   `psutil` is **not** pulled in — it is provider-runtime host metrics (D14), not sign-in.

10. **The auth gate: `credentials.require_session()`.** Returns the stored session token or raises
    `SystemExit("You're not signed in. Run `grid login` to sign in.")`. No auth-requiring cloud
    command ships this slice, so it is unit-tested directly; later slices call it. **Secrets are
    never printed or logged** — not on the human path, not in `--json` (which emits grid
    names/types only), asserted by tests on both paths.

11. **`grid sync` — refresh the grid list without re-login (amendment 2026-06-29, issue 10).**
    `grid login` populates the grid list once; `grid ls` then reads only the local store, so a grid
    created on the website (or one the account is newly added to as a member) surfaces locally only
    after another full browser login. `grid sync` closes that gap: it reuses the stored **session**
    token to re-fetch `GET /v1/grid/tokens` (no browser) and authoritatively overwrites the stored
    grid list and per-grid tokens — the no-re-auth complement to Decision 7's re-login. It **never
    touches the active pointer** (continues Decision 6): only `credentials.toml` is rewritten, never
    `state.json`, so a vanished active grid is left as a tolerated stale pointer (selection falls
    through to the sole grid / none) and sync never auto-selects. It reuses Decision 3's `_validated`
    (a bundle missing `network_id`/`name` aborts, writing nothing). Three boundary guards: a
    **session** rejected by the control plane (401/403) is rewritten to an actionable "run `grid
    login`" — distinct from the *relay access-token* 401 on the consume path (ADR 0005), so it is a
    new message, not a reuse; a previously non-empty list returned empty is cleared but **warns on
    stderr** first (a transient backend hiccup must not silently wipe every credential); any other
    control-plane error propagates unchanged. Surface: `sync` joins `CLOUD_ONLY` (now `{"login",
    "logout", "sync"}`), gated with guidance in LAN; `--json` carries grid names/types only, never a
    token. Code: a standalone `cmd_sync` in `cli/auth.py` (no `cmd_login` refactor, no `--api-url`
    flag — `api_url` resolves from stored credentials). (This ADR-local "Decision 11" is
    unrelated to `DECISIONS.md`'s D11, the managed-networks repoint cited in `cloud/control_plane.py`.)

## Consequences

- LAN mode is untouched: login/logout are gated with guidance, and no new code runs for LAN users.
- `state.json` remains the single owner of the active selection; `credentials.toml` holds tokens
  only. Later slices resolve the active grid name against the persisted bundles.
- The hardened `atomic_write_bytes` is now the one place secret/state files are written; future
  stores should reuse it rather than re-rolling temp-file logic.
- A future cloud-only command added without placing it in `CLOUD_ONLY` fails the classification
  test rather than silently running LAN code (or erroring opaquely) in the wrong mode.
- The control plane is reachable only in cloud mode and only via `cloud/`; `shared/`/`lan/` gain no
  off-LAN calls.
- `grid sync` (amendment) makes `CLOUD_ONLY` `{"login", "logout", "sync"}`; the classification test
  covers it. Sync is the first command to find a **session** token expired long after login, so it
  introduces the session-expiry → re-login message; the other account-level commands
  (`up`/`down`/`ls`/`info`/`members`) still surface raw control-plane errors today, and unifying that
  is intentionally out of scope here.
