"""Remote-mode grid lifecycle: `grid start` / `stop` / `ls` / `info` against the hosted
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
import shlex
from typing import Any

from shared import shell, state

from .next_steps import print_env_hint


# Default network type for `grid start` on create (DECISIONS D11; the other choice is
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
    The single home of the active>sole precedence, shared by ``start`` (no name) and ``_select``.
    """
    nets = _networks()
    active = state.get_active("remote")
    if active:
        for net in nets:
            if net.get("network_id") == active or net.get("name") == active:
                return net
    return nets[0] if len(nets) == 1 else None


def _select(name: str | None) -> dict[str, Any]:
    """The grid a name-taking command (``stop``/``info``) acts on. An explicit name must exist;
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


def require_access_token(rec: dict[str, Any], label: str) -> str:
    """The grid's per-grid access token, or the familiar "run `grid login`" guidance.

    One source for the sentence, shared by `grid info --env` and `grid launch` — a launch that
    proceeded with an empty bearer would surface inside the launched app as an auth error that names
    neither the grid nor the fix. (Three older copies of this sentence live in `remote_request.py`,
    `remote_price.py` and `remote_provider.py`; folding those in is out of scope here.)
    """
    token = rec.get("access_token")
    if not token:
        raise SystemExit(
            f"Grid {label} has no access token locally. Run `grid login` to refresh your grids."
        )
    return str(token)


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
    # `shlex.quote`: a grid name is freeform and can carry a space ("Hydrate Grid"), and an
    # unquoted hint isn't actually copy-pasteable (grid-leave issue: `cli/grid.py`/`cli/provider.py`
    # had the identical bug in their own `Next:` hints).
    quoted_label = shlex.quote(label)
    if not base:
        raise SystemExit(f"Grid {label} isn't up; run `grid start {quoted_label}` first.")
    if status.get("state") and status.get("state") != "running":
        raise SystemExit(f"Grid {label} isn't up; run `grid start {quoted_label}` first.")
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
        raise SystemExit("Name a grid to create: grid start <name> (or grid use <name> to pick one).")
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
        # next `grid start <name>` that would create a duplicate.
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
    print(f"Grid {label} is down (grid start {shlex.quote(label)} brings it back).")
    return 0


def _is_owner(rec: dict[str, Any]) -> bool:
    """Whether this account created the grid, read from its own per-grid token.

    The control plane is the authority and refuses a member's delete on its own; this reads the
    ``admin`` role the token already carries so the refusal can name the reason before a round trip,
    the way ``grid stop`` names a stopped grid rather than posting and translating a 404. A stale or
    unreadable token answers "not owner", which costs a member nothing (they may not delete anyway)
    and costs an owner one `grid login`.
    """
    from remote import credentials

    roles = credentials.claims_from_token(rec.get("access_token")).get("roles")
    return isinstance(roles, list) and "admin" in roles


def _member_count(session: str, network_id: str) -> int | None:
    """How many members the grid has, or ``None`` when that cannot be read. Best-effort like
    ``_try_status``: it decorates a warning, so it must never be the thing that fails a delete."""
    from remote import control_plane

    try:
        members = control_plane.list_members(session, network_id)
    except SystemExit:
        return None
    if isinstance(members, dict):
        members = members.get("members")
    return len(members) if isinstance(members, list) else None


def cmd_remote_delete(args: argparse.Namespace) -> int:
    """`grid delete <name>` — remove a remote grid from the account for good.

    The one irreversible verb in the lifecycle, so it is deliberately harder to reach than the three
    reversible ones around it. Three gates, in the order that fails cheapest first:

    1. **The name is required.** Every other remote command falls back to the active grid; this one
       will not, because "the active grid" is not something you can misread — you have to type what
       you are destroying.
    2. **Owner only**, and the grid must already be **stopped**. Stopping is the loud, reversible
       step where service actually ends and everyone on the grid sees it; delete is then only the
       paperwork on a grid that is already dark. Splitting them means the irreversible half can
       never happen as a surprise consequence of the reversible one.
    3. **Confirmation is the name, not a keystroke.** `y` is what a reader types while skimming, and
       `grid stop` and `grid delete` sit one word apart in the help. Retyping the grid name is the
       one confirmation that cannot be given by accident.

    Providers are *evicted*, not consulted: an owner cannot be made to wait on other people's
    machines running `grid leave`, so joined engines stop being routed to rather than blocking this.
    """
    from remote import control_plane, credentials

    session = credentials.require_session()
    if not args.name:
        raise SystemExit(
            "Name the grid to delete: `grid delete <name>`. Unlike every other command, this one "
            "will not act on the active grid — deleting cannot be undone, so it should never be "
            "possible to do to a grid you did not name."
        )
    rec = _select(args.name)
    network_id = _network_id(rec)
    label = rec.get("name") or network_id

    # Before asking whether the token says "owner", insist there IS a token: an absent one reads as
    # "not owner" through `claims_from_token`, and answering an owner with "you are only a member"
    # sends them to fix the wrong thing. `require_access_token` already owns that sentence.
    require_access_token(rec, label)
    if not _is_owner(rec):
        raise SystemExit(
            f"Only the owner of {label} can delete it — this account is a member of it. "
            "`grid leave` removes your machines from a grid you did not create."
        )

    status = _try_status(session, network_id)
    if status.get("state") == "running":
        raise SystemExit(
            f"{label} is running. Stop it first, so its members see service end before it "
            f"disappears:\n  grid stop {shlex.quote(label)}"
        )

    if not args.yes:
        members = _member_count(session, network_id)
        print(f"Delete grid {label} ({network_id})?")
        print()
        print("  `grid stop` pauses a grid and `grid start` brings it back with everything intact.")
        print("  This is the other one: the grid leaves the account for good, its members lose")
        print("  access, and any machine still serving it stops being routed to. There is no undo.")
        if members is not None:
            print()
            print(f"  members: {members}")
        print()
        try:
            typed = input(f"Type the grid name to confirm ({label}): ").strip()
        except EOFError:
            typed = ""
        if typed != label:
            print("Aborted — the name did not match.")
            return 1

    try:
        control_plane.delete_managed_network(session, network_id)
    except SystemExit as exc:
        # The local owner check reads a token that was minted once and can be out of date — the grid
        # may have changed hands since. Only 403 is re-worded: 401 keeps its exact rendering because
        # `cli/auth._SESSION_EXPIRED_RE` matches on that string to offer a re-login.
        if getattr(exc, "status", None) == 403:
            raise SystemExit(
                f"The control plane refused to delete {label}: this account is not its owner. "
                "The grid may have changed hands since you last signed in — `grid login` refreshes "
                "what your machine believes."
            ) from None
        raise
    credentials.remove_network(network_id)
    # The active pointer outlives the grid it names, and every later command resolves through it —
    # so a delete that left it set would aim `grid join`, `grid chat` and the rest at a grid that is
    # gone. Cleared by *either* spelling, because `grid use` accepts both.
    if state.get_active("remote") in (label, network_id):
        state.set_active("remote", None)
    print(f"Deleted grid {label}.")
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
        print("(no grids — run `grid start <name>` to bring one online)")
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
        token = require_access_token(rec, label)
        # The relay base comes from live status for the creator, or the login bundle for a member —
        # resolve_relay_base handles both (the bundle carries the token but not the address).
        base, _status = resolve_relay_base(session, rec, _network_id(rec), label)
        # The one deliberate exception to "never print a token" (ADR 0003 §6): an explicit,
        # user-requested disclosure of the caller's own token to their own shell — like
        # `gh auth token`. Every other path (ls, info without --env, all --json) stays token-free.
        base_url = base.rstrip("/") + "/relay/v1"
        # `shell.quote`, not an f-string in double quotes: this block exists to be evaluated, and the
        # token is an opaque credential this repo neither mints nor validates while the base arrives
        # from the control plane. A value holding `"`, `$`, a backtick or a backslash would break out
        # of a double-quoted context and be *executed* by the eval this command invites.
        print(f"export OPENAI_BASE_URL={shell.quote(base_url)}")
        print(f"export OPENAI_API_KEY={shell.quote(token)}")
        print_env_hint(_env_command(args.grid))
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


def _env_command(grid: str | None) -> str:
    """The `info --env` command as this caller typed it, re-quoted so the hint can be pasted back."""
    return "grid info --env" + (f" {shlex.quote(grid)}" if grid else "")
