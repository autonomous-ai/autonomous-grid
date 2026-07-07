"""Remote-mode grid lifecycle: `grid up` / `down` / `ls` / `info` against the hosted
managed-networks API.

Remote-only — `cli.dispatch` routes these here in remote mode, so the handlers assume remote and
gate on sign-in via `credentials.require_session()`. Lifecycle is an *account-level* operation:
it authenticates with the session token, not a per-grid token (the per-grid token and
`info --env` are the remote use-path, a later slice). `ls` reads the locally stored grids
(`credentials.toml`), never the network. Tokens are never printed. See ADR 0003.

Import rule: only stdlib + `shared.state` at module top; `remote.*` is imported lazily inside
each handler (mirrors `cli/auth.py`) because `cli.dispatch` imports this module while the `cli`
package is still initialising — a top-level `from cli import …` here would be a partial-init cycle.
"""
from __future__ import annotations

import argparse
import json
import re
from typing import Any

from shared import state


# Default network type for `grid up` on create (DECISIONS D11; the other choice is
# permissioned-providers). `--type` parses with default None so a value passed on a *start* can
# be told apart from this create default.
DEFAULT_NETWORK_TYPE = "permissioned-public"

# A grid's network_id is interpolated straight into the control-plane request path, so it must be
# an opaque token with no path/query characters — reject anything else (and a missing id) before it
# can re-target a request (e.g. `n1/../admin`) or crash a later call with a bare KeyError.
_NETWORK_ID_RE = re.compile(r"[A-Za-z0-9_-]+")


def _networks() -> list[dict[str, Any]]:
    from remote import credentials

    return list(credentials.load_credentials().get("networks") or [])


def _by_name(name: str) -> dict[str, Any] | None:
    """The locally stored grid matching ``name`` (by name or network_id), or ``None``."""
    for net in _networks():
        if net.get("name") == name or net.get("network_id") == name:
            return net
    return None


def _resolve_default() -> dict[str, Any] | None:
    """The grid to act on when none is named: the active selection, else the sole grid, else
    ``None``. No ``home`` fallback — remote never auto-creates one. Mirrors the default branch of
    ``local/config.select_grid``; a stale active (its grid was removed) falls through to the sole grid.
    The single home of the active>sole precedence, shared by ``up`` (no name) and ``_select``.
    """
    nets = _networks()
    active = state.get_active("remote")
    if active:
        for net in nets:
            if net.get("network_id") == active or net.get("name") == active:
                return net
    return nets[0] if len(nets) == 1 else None


def _select(name: str | None) -> dict[str, Any]:
    """The grid a name-taking command (``down``/``info``) acts on. An explicit name must exist;
    otherwise fall back to ``_resolve_default`` (active>sole). Clear ``SystemExit`` either way."""
    if name:
        rec = _by_name(name)
        if rec is None:
            raise SystemExit(f"Grid not found: {name!r}. Run `grid ls` to see your grids.")
        return rec
    rec = _resolve_default()
    if rec is None:
        raise SystemExit("Name a grid (run `grid ls` to see your grids).")
    return rec


def _valid_network_id(nid: Any) -> bool:
    return isinstance(nid, str) and _NETWORK_ID_RE.fullmatch(nid) is not None


def _network_id(rec: dict[str, Any]) -> str:
    """The grid's validated network_id. Guards the boundary where an id from the local store (a
    create reply or a login-fetched bundle) is about to be interpolated into a request path."""
    nid = rec.get("network_id")
    if not _valid_network_id(nid):
        raise SystemExit(
            f"Grid {rec.get('name') or '?'!r} has no usable id locally. "
            "Run `grid login` to refresh your grids."
        )
    return str(nid)


def _grid_url(live: dict[str, Any], rec: dict[str, Any]) -> str:
    """The grid's relay address. Prefer the live response; fall back to the stored bundle — a
    login-fetched bundle carries it as ``lan_signaling_url`` (a create reply as ``signaling_url``)."""
    return live.get("signaling_url") or rec.get("signaling_url") or rec.get("lan_signaling_url") or ""


def _try_status(session: str, network_id: str) -> dict[str, Any]:
    """Live managed-network status, or ``{}`` when the caller may not read it (a non-creator member
    gets 403 from the creator-only endpoint). For display paths that should degrade, never fail."""
    from remote import control_plane

    try:
        return control_plane.get_managed_network_status(session, network_id)
    except SystemExit:
        return {}


def resolve_relay_base(
    session: str, rec: dict[str, Any], network_id: str, label: str
) -> tuple[str, dict[str, Any]]:
    """The grid's relay base URL for a use/serve command — works for a member, not just the creator.

    The creator-only live status is authoritative and confirms the grid is running; a member
    (provider/consumer who didn't create the grid) gets 403 there, so fall back to the URL the login
    bundle already carries (``lan_signaling_url``). Returns ``(base, status)`` where ``status`` is
    ``{}`` for a member (no run-state visible), so a member skips the up-front running check (a stopped
    grid then fails later at the relay). Raises a clean ``SystemExit`` when no URL is available
    anywhere, or when the creator-visible status says the grid is stopped.
    """
    from remote import control_plane

    bundle_url = rec.get("lan_signaling_url") or rec.get("signaling_url")
    try:
        status = control_plane.get_managed_network_status(session, network_id)
    except SystemExit:
        if not bundle_url:
            raise  # not the creator and no stored relay URL — surface the original error
        status = {}
    base = _grid_url(status, rec)
    if not base:
        raise SystemExit(f"Grid {label} isn't up; run `grid up {label}` first.")
    if status.get("state") and status.get("state") != "running":
        raise SystemExit(f"Grid {label} isn't up; run `grid up {label}` first.")
    return base, status


def _record(resp: dict[str, Any], name: str) -> dict[str, Any]:
    """The fixed projection persisted locally on create — never the whole response, so a token in
    the create reply cannot leak into credentials.toml. ``None`` fields are dropped: the create
    reply may omit ``status``/``signaling_url``, and TOML cannot serialise ``None``."""
    fields = {
        "network_id": resp.get("network_id"),
        "name": resp.get("name") or name,
        "network_type": resp.get("network_type"),
        "signaling_url": resp.get("signaling_url"),
        "status": resp.get("status"),
    }
    return {key: value for key, value in fields.items() if value is not None}


def _print_up(name: str, url: str) -> int:
    print(f"grid={name}")
    print(f"grid_url={url}")
    return 0


def cmd_remote_up(args: argparse.Namespace) -> int:
    from remote import control_plane, credentials

    session = credentials.require_session()
    name = args.name
    rec = _by_name(name) if name else _resolve_default()
    if rec is not None:  # known / active / sole grid → start (idempotent if already running)
        if args.type is not None:  # --type only applies on create; say so rather than silently drop it
            print(f"Note: --type applies only when creating; ignoring it for the existing grid "
                  f"{rec.get('name') or name}.")
        network_id = _network_id(rec)
        control_plane.start_managed_network(session, network_id)
        # The start reply carries only {network_id, status} — no signaling_url — so read the grid's
        # address from the status endpoint (authoritative), falling back to the stored record.
        status = control_plane.get_managed_network_status(session, network_id)
        return _print_up(rec.get("name") or name, _grid_url(status, rec))
    if name is None:  # nothing to start, and no name to create under
        raise SystemExit("Name a grid to create: grid up <name> (or grid use <name> to pick one).")
    resp = control_plane.create_managed_network(session, name, args.type or DEFAULT_NETWORK_TYPE)
    if not _valid_network_id(resp.get("network_id")):
        # A 200 with no usable id would otherwise persist a record that can't be acted on and
        # crash the next call with a bare KeyError — surface it as a clean error instead.
        raise SystemExit("The control plane returned no usable id for the grid; it may not have "
                         "been created. Run `grid ls` (after `grid login`) to check.")
    record = _record(resp, name)
    try:
        credentials.add_network(record)
    except OSError as exc:
        # The grid exists server-side now; tell the user rather than leaving a bare traceback and a
        # next `grid up <name>` that would create a duplicate.
        raise SystemExit(
            f"Grid {name!r} was created in remote mode but couldn't be saved locally ({exc}). "
            "Run `grid login` to re-sync your grids before retrying."
        ) from None
    return _print_up(resp.get("name") or name, _grid_url(resp, record))


def cmd_remote_down(args: argparse.Namespace) -> int:
    from remote import control_plane, credentials

    session = credentials.require_session()
    rec = _select(args.name)
    network_id = _network_id(rec)
    label = rec.get("name") or network_id
    control_plane.stop_managed_network(session, network_id)
    print(f"Grid {label} is down (grid up {label} brings it back).")
    return 0


def cmd_remote_ls(args: argparse.Namespace) -> int:
    from remote import credentials

    credentials.require_session()
    nets = _networks()  # local only — `grid login` already fetched these; no network call
    active = state.get_active("remote")
    if args.json:
        print(json.dumps(
            [{"grid": n.get("name"), "type": n.get("network_type"), "id": n.get("network_id")} for n in nets],
            indent=2,
        ))
        return 0
    if not nets:
        print("(no grids — run `grid up <name>` to bring one online)")
        return 0
    for net in nets:
        is_active = active and (net.get("network_id") == active or net.get("name") == active)
        marker = "* " if is_active else "  "
        print(f"{marker}{net.get('name') or ''}\t{net.get('network_id') or ''}\t{net.get('network_type') or ''}")
    return 0


def cmd_remote_info(args: argparse.Namespace) -> int:
    from remote import credentials

    session = credentials.require_session()
    if args.env:
        rec = _select(args.grid)
        label = rec.get("name") or rec.get("network_id")
        token = rec.get("access_token")
        if not token:
            raise SystemExit(
                f"Grid {label} has no access token locally. Run `grid login` to refresh your grids."
            )
        # The relay base comes from live status for the creator, or the login bundle for a member —
        # resolve_relay_base handles both (the bundle carries the token but not the address).
        base, _status = resolve_relay_base(session, rec, _network_id(rec), label)
        # The one deliberate exception to "never print a token" (ADR 0003 §6): an explicit,
        # user-requested disclosure of the caller's own token to their own shell — like
        # `gh auth token`. Every other path (ls, info without --env, all --json) stays token-free.
        base_url = base.rstrip("/") + "/relay/v1"
        print(f'export OPENAI_BASE_URL="{base_url}"')
        print(f'export OPENAI_API_KEY="{token}"')
        return 0
    rec = _select(args.grid)
    # Status is creator-only; a member sees `{}` here and just gets a blank run-state (never an error).
    status = _try_status(session, _network_id(rec))
    # Project the status reply onto a fixed grid-vocabulary shape: the live API names the run state
    # `state`; we drop the proprietary server internals (server_pid / sync_pid / postgres / base_url /
    # plan / seats) and never carry a token.
    view = {
        "grid": rec.get("name") or rec.get("network_id"),
        "type": rec.get("network_type") or status.get("network_type"),
        "status": status.get("state"),
        "grid_url": _grid_url(status, rec),
    }
    if args.json:
        print(json.dumps(view, indent=2))
        return 0
    for key in ("grid", "type", "status", "grid_url"):
        print(f"{key}={view[key] if view[key] is not None else ''}")
    return 0


def cmd_remote_members(args: argparse.Namespace) -> int:
    """`grid members add|remove|list [grid] <email>` — manage who may use or serve a remote grid.

    Account-level (session token, like lifecycle): it resolves the grid locally and never needs it
    running, so there is no status/relay call. Human output is built from the inputs we already hold
    and ``.get()`` on each member — the control-plane reply shape is never indexed into; ``--json``
    echoes the raw reply. No token is printed."""
    from remote import control_plane, credentials

    session = credentials.require_session()
    network_id = _network_id(_select(args.grid))

    if args.subcommand == "add":
        role = args.role  # parser default is "both"; choices constrain it to the three roles
        result = control_plane.add_member(session, network_id, args.email, [role])
        if args.json:
            print(json.dumps(result, indent=2))
            return 0
        print(f"Added {args.email} (roles: {role})")
        return 0

    if args.subcommand == "remove":
        result = control_plane.remove_member(session, network_id, args.email)
        if args.json:
            print(json.dumps(result, indent=2))
            return 0
        print(f"Removed {args.email}")
        return 0

    # add/remove returned above; argparse (required=True + choices) guarantees the rest is `list`,
    # but guard explicitly so a future subcommand can't silently fall through to a list.
    if args.subcommand != "list":
        raise SystemExit(f"Unknown members subcommand: {args.subcommand!r}")
    members = control_plane.list_members(session, network_id)
    if args.json:
        print(json.dumps(members, indent=2))
        return 0
    if not members:
        print("(no members)")
        return 0
    for member in members:
        email = member.get("email") or ""
        roles = ",".join(member.get("roles") or [])
        print(f"{email}\t{roles}")
    return 0
