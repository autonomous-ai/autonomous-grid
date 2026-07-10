"""Remote-mode `grid router status|enable|disable|set-ranker|remove-ranker` — the network
creator's surface for auto-routing (model `auto`, ADR 0013).

Account-level, like `grid members`: it authenticates with the session token, resolves the grid
locally via ``remote_grid._select``/``_network_id`` and calls the control-plane owner API — no
relay, no running grid needed. The Ranker key for ``set-ranker`` is sourced from the
``GRID_RANKER_API_KEY`` env var or a hidden prompt (never a flag, never printed); ``status`` never
shows key material.

Remote-only — ``cli.dispatch`` gates it (``router`` is in ``REMOTE_ONLY``); local mode exits with
guidance. Import rule mirrors the other remote handlers: stdlib only at module top; ``remote.*`` and
sibling ``cli`` modules are imported lazily inside handlers (``cli.dispatch`` imports this module
while the ``cli`` package is still initialising).
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
from typing import Any

# The Ranker API key for `set-ranker` is read from this env var (else a hidden prompt) — never a
# flag. grid-apis documents the same name as the CLI's source (ADR 0013 / grid-apis handler).
RANKER_KEY_ENV = "GRID_RANKER_API_KEY"

# Reply fields that must never be echoed. The control plane already masks ranker keys on every read
# (they ride only the owner's snapshot channel); this is a client-side belt-and-suspenders scrub so
# a future server-side masking regression can't turn `--json`/`status` into a key leak.
_SECRET_KEYS = frozenset({"api_key", "apikey", "key", "secret", "token"})


def cmd_remote_router(args: argparse.Namespace) -> int:
    """`grid router status|enable|disable|set-ranker|remove-ranker` — configure auto-routing on a
    grid you own.

    Account-level (session token, like `grid members`): resolves the grid locally and never needs it
    running, so there is no relay call. The control-plane reply is masked (position/base_url/model,
    never a key) and echoed as-is for ``--json`` (after a defensive scrub); human output is built with
    ``.get()`` and never indexes the reply shape. The Ranker key for ``set-ranker`` never touches this
    function's output — it flows straight from env/prompt into the control-plane request body."""
    from remote import control_plane, credentials

    from . import remote_grid

    session = credentials.require_session()
    network_id = remote_grid._network_id(remote_grid._select(args.grid))

    if args.subcommand == "status":
        config = control_plane.get_router_config(session, network_id)
        return _print_status(config, args.json)

    if args.subcommand == "enable":
        result = control_plane.enable_router(session, network_id)
        return _print_mutation(result, args.json, "Auto-routing enabled.")

    if args.subcommand == "disable":
        result = control_plane.disable_router(session, network_id)
        return _print_mutation(result, args.json, "Auto-routing disabled.")

    if args.subcommand == "set-ranker":
        key = _resolve_ranker_key()
        result = control_plane.set_ranker(
            session, network_id, args.position, args.base_url, args.model, key)
        return _print_mutation(result, args.json, f"Set ranker {args.position} ({args.model}).")

    if args.subcommand == "remove-ranker":
        result = control_plane.remove_ranker(session, network_id, args.position)
        return _print_mutation(result, args.json, f"Removed ranker {args.position}.")

    raise SystemExit(f"Unknown router subcommand: {args.subcommand!r}")


def _safe_reply(reply: Any) -> dict[str, Any]:
    """Guard + scrub a control-plane reply before it is formatted or echoed. A non-dict 2xx body is
    off-contract — fail with a clean ``SystemExit`` rather than an ``AttributeError`` traceback (the
    same "never a traceback" idiom as ``_send``/``_raise`` and ``cli/remote_provider.py``'s shape
    check). Then strip any key-material field at any depth (defence in depth over the control plane's
    own masking)."""
    if not isinstance(reply, dict):
        raise SystemExit(f"The control plane returned an unexpected router reply: {str(reply)[:200]}")
    return _strip_secrets(reply)


def _strip_secrets(value: Any) -> Any:
    """Recursively drop any ``_SECRET_KEYS`` field from a reply so key material can never be echoed."""
    if isinstance(value, dict):
        return {k: _strip_secrets(v) for k, v in value.items() if k.lower() not in _SECRET_KEYS}
    if isinstance(value, list):
        return [_strip_secrets(item) for item in value]
    return value


def _print_status(config: dict[str, Any], as_json: bool) -> int:
    """Show routing state + each configured ranker. ``--json`` echoes the (scrubbed, already-masked)
    reply; human output uses ``.get()`` throughout and never indexes the reply shape."""
    config = _safe_reply(config)
    if as_json:
        print(json.dumps(config, indent=2))
        return 0
    print(f"auto-routing: {'enabled' if config.get('enabled') else 'disabled'}")
    rankers = config.get("rankers") or []
    if not rankers:
        print("(no rankers configured)")
        return 0
    for ranker in rankers:
        print(f"{ranker.get('position')}\t{ranker.get('base_url') or ''}\t{ranker.get('model') or ''}")
    return 0


def _print_mutation(result: dict[str, Any], as_json: bool, human: str) -> int:
    """Confirm a router mutation. ``--json`` echoes the raw (scrubbed, masked) reply verbatim. On the
    human path the confirmation is a fixed string; when the control plane reports ``synced: false``
    (saved, but the best-effort push to the running master didn't land) a caveat is appended so the
    creator isn't told it's live when the periodic snapshot hasn't applied it yet."""
    result = _safe_reply(result)
    if as_json:
        print(json.dumps(result, indent=2))
        return 0
    if result.get("synced") is False:
        human += " (saved; the running grid will apply it shortly)"
    print(human)
    return 0


def _resolve_ranker_key() -> str:
    """The Ranker API key for `set-ranker`: the ``GRID_RANKER_API_KEY`` env var, else a hidden prompt.
    Deliberately never a flag — a key on the command line leaks into shell history and process
    listings. An empty prompt, or non-interactive with no key, is a clear error naming the env var
    (mirrors the API-engine key precedent in ``cli/remote_provider.py``). The resolved key goes
    straight to the control plane (``set_ranker`` body); it is never stored locally, logged, or
    printed."""
    from . import provider  # lazy: for _interactive() (both stdin+stdout must be a tty)

    key = (os.environ.get(RANKER_KEY_ENV) or "").strip()
    if not key and provider._interactive():
        key = _prompt_ranker_key()
        if not key:
            raise SystemExit("No Ranker API key entered.")
    if not key:
        raise SystemExit(
            f"set-ranker needs your Ranker API key in {RANKER_KEY_ENV} "
            f"(export {RANKER_KEY_ENV}=... and re-run), or run interactively to be prompted."
        )
    return key


def _prompt_ranker_key() -> str:
    """Hidden interactive prompt for the Ranker key — input is never echoed (getpass). Split out so
    the CLI-seam tests can monkeypatch it (getpass reads the controlling tty)."""
    return getpass.getpass(
        f"Enter your Ranker API key (input hidden; or export {RANKER_KEY_ENV}): "
    ).strip()
