# ADR 0006 — Remote membership (`grid members add` / `remove` / `list`)

Status: accepted (2026-06-28)

## Context

ADR 0001 set up modes + dispatch; 0002 sign-in + the account `session_token`; 0003 the remote grid
lifecycle (`grid up/down/ls/info`) authenticated with that session token; 0004 the provider serve
loop; 0005 the consume path. This slice fills the last remote surface in scope: **membership
administration** for a remote grid you own. `grid members add [grid] <email> [--role …]`,
`grid members remove [grid] <email>`, and `grid members list [grid]` manage who may use or serve a
grid, through the hosted **managed-networks members API**.

The reference client `grid-src/grid_cli/` (`control_plane.py` `add_member`/`remove_member`/
`list_members`, `cli.py` `allowlist` handlers) is the port source for the wire contract; the
proprietary backend stays out. This is the D13 rename: `grid network allowlist …` →
`grid members …`, with denylist / set-type / plan / cancel / transactions / sync all **out of
scope** (they live on the website).

Hard invariant: local mode stays local-only, unauthenticated, stateless — unchanged. The whole existing
suite stays green; remote reaches the control plane only through `remote/`; tokens are never printed.

`members` / `role` / `consumer` / `provider` / `both` are the **sanctioned vocabulary** for this
command; `network` / `signaling` / `node` stay off the surface.

## Decisions

1. **`members` is an `REMOTE_ONLY` command with nested subcommands.** `"members"` joins
   `dispatch.REMOTE_ONLY` (with `login`/`logout`). In remote mode dispatch falls through to
   `args.handler`; in local mode the `elif command in REMOTE_ONLY → local_stub` gate fires *before* the
   handler (`local_stub` is `NoReturn`), exiting with "run `grid mode remote`". The classification and
   subset/disjointness partition tests stay green because only the top-level `members` is a
   `sub.choices` key — the nested `add`/`remove`/`list` are invisible to them, exactly like the
   AGNOSTIC `engine` command's own subcommands.

2. **One handler, `cli/remote_grid.py:cmd_remote_members`, branching on `args.subcommand`.** It mirrors
   `_add_engine_setup`'s nested-subparser idiom (`dest="subcommand"`, `required=True`, each
   subparser `set_defaults(handler=cmd_remote_members)`). The parser points directly at the real remote
   handler — the `login`/`logout` pattern for REMOTE_ONLY commands (there is no local handler to reroute
   from). The `[grid]` positional is declared **before** the required `email` so argparse binds a
   lone positional to `email` and leaves `grid` defaulting to the active grid.

3. **Account-level auth = the session token; no status/relay call.** Membership is an account-level
   operation (D13, like lifecycle in ADR 0003 §2), so the handler carries
   `credentials.require_session()` and needs only the grid's `network_id`, resolved locally with the
   reused `_select(args.grid)` + `_network_id(rec)`. It does **not** require the grid to be running
   and makes **no** `get_managed_network_status` / relay call — the leanest mirror is `cmd_remote_down`.
   The per-grid `access_token` (relay consumption) is untouched here.

4. **Endpoint repointed to `/v1/grid/managed-networks/{id}/members`** (D11/D13), not the port
   source's legacy `/v1/grid/networks/...` — the same divergence ADR 0003 made for lifecycle. Auth is
   `Authorization: Bearer {session_token}`; the body and method are ported from the reference:
   `add` → `POST` body `{"email", "roles":[…]}`, `remove` → `DELETE …/members/{email}`, `list` →
   `GET …/members`. The functions live beside the other `*_managed_network` clients in
   `remote/control_plane.py` and reuse `_client`/`_send`/`_raise`, so a transport error or any `≥400`
   (e.g. 403 not-owner, 404 unknown grid) surfaces as a clean `SystemExit`, never a traceback.

5. **`--role` is a single choice `consumer|provider|both` (default `consumer`), sent as
   `roles=[role]` with no client-side expansion.** `both` is a first-class wire role —
   D13 defines it as a role, and the port source's `VALID_MEMBER_ROLES`
   includes `"both"` and sends `--role` values verbatim (no `both → ["consumer","provider"]` magic).
   So `--role both` → `roles=["both"]`. The reference's `admin` role is dropped — it is outside the
   D13-locked surface.

6. **Human output is built from known inputs, never by indexing the reply; `--json` echoes the raw
   reply.** The reference `add_member`/`remove_member` return raw `resp.json()` with **no guaranteed
   `member`/`status`/`member_epoch` keys**, so `add`/`remove` print from the `email`/`role` we
   already hold (`Added <email> (roles: <role>)` / `Removed <email>`) and `list` reads each member
   with `.get()` (email + roles only — `status` is not in the guaranteed Member shape). `--json`
   prints the raw control-plane return for all three (the unwrapped member *list* for `list`, no
   re-wrapping). Every state-reading command supports `--json` (repo convention); no token is ever
   printed (none is loaded beyond the session token, which is never echoed).

7. **`list_members` unwraps defensively, and `remove` percent-encodes the email.** `list_members`
   accepts both the `{"members":[…]}` envelope (the `fetch_tokens` idiom) and a bare array, coercing
   a missing key / null to `[]`. `remove_member` interpolates `quote(email, safe="")` so a stray `/`
   (or other path char) in the user-supplied email cannot re-target the request — the same path
   boundary `network_id`'s regex already guards. This is a deliberate hardening over the port
   source's plain interpolation; a standard path-param decode on the server makes it transparent.

## Consequences

- local is untouched: `members` is rejected with guidance in local mode and no new code runs for local
  users; the existing suite stays green.
- The `list` `{"members":[…]}` envelope is the one response shape not confirmed against the
  managed-networks path (it is inferred from the lifecycle/token contracts). The defensive unwrap
  covers a bare array, so a shape mismatch degrades to an empty/echoed list rather than a crash; it
  should be confirmed against the live API when available.
- The members wire contract joins the lifecycle clients in `remote/control_plane.py` (mocked in tests
  via `httpx.MockTransport`), adjustable if the hosted API diverges.
- Membership reads/writes use only the session token; the per-grid token store is untouched, and no
  token reaches stdout. A future denylist / role-set enhancement extends the same handler + client
  without touching the dispatch or vocabulary boundary.
