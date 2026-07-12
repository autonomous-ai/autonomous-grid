"""Remote-mode `grid router status|enable|disable|set-advisors|remove-advisor|models` — the network
creator's surface for auto-routing (model `auto`, ADR 0013, revised).

Account-level, like `grid members`: it authenticates with the session token, resolves the grid locally via
``remote_grid._select``/``_network_id`` and calls the control-plane owner API — no relay, no running grid
needed. An **Advisor** is a ``provider[:model]`` pair picked BY NAME from the platform catalog; the platform
carries the base URL and the key, so there is NO key input anywhere in this group — no env var, no prompt,
no flag. ``grid router models`` prints the catalog and needs no grid at all.

Remote-only — ``cli.dispatch`` gates it (``router`` is in ``REMOTE_ONLY``); local mode exits with guidance.
Import rule mirrors the other remote handlers: stdlib only at module top; ``remote.*`` and sibling ``cli``
modules are imported lazily inside handlers (``cli.dispatch`` imports this module while the ``cli`` package
is still initialising).
"""
from __future__ import annotations

import argparse
import json
from typing import Any

# Max advisors in a chain — a client-side fast-fail cap. The control plane's ``_ROUTER_MAX_ADVISORS`` is the
# real backstop (a stricter server just surfaces its own 400); this only spares an obviously-too-long request.
MAX_ADVISORS = 3

# Reply fields that must never be echoed. The control plane already masks any key AND base URL on every read
# (the per-grid proxy key + advisor-proxy URL ride only the owner's snapshot channel, never a CLI reply);
# this is a client-side belt-and-suspenders scrub so a future server-side masking regression can't turn
# `--json`/`status` into a key OR URL leak — ADR 0013: "`status` returns neither key nor URL". The URL keys
# (`base_url`/`endpoint_url`) match the vocabulary used elsewhere in the client (`cli/remote_provider.py`);
# no v2 reply legitimately carries any of these fields, so nothing real is ever dropped.
_SECRET_KEYS = frozenset(
    {"api_key", "apikey", "key", "secret", "token", "base_url", "url", "endpoint_url"})


def parse_advisor_token(token: str) -> tuple[str, str | None]:
    """Parse one ``provider[:model]`` advisor token into ``(provider, model | None)``. A bare provider →
    ``model is None`` (the control plane resolves the catalog default).

    SHAPE validation only — the CLI never hardcodes the provider/model whitelist (that is the control plane's
    catalog); an unknown provider or off-whitelist model is a server 400 listing the valid names. Raises
    ``argparse.ArgumentTypeError`` (→ a clean parser error, never a traceback) on an empty token, empty
    provider (``:model``), empty model (``provider:``), or a stray extra colon (``a:b:c``). The single ``:``
    delimiter assumes catalog model names are colon-free — true for the v1 openai catalog (``gpt-5-mini`` &c.).
    """
    raw = token.strip()
    if not raw:
        raise argparse.ArgumentTypeError("empty advisor token")
    if ":" not in raw:
        return raw, None  # bare provider — the server resolves the catalog default model
    parts = raw.split(":")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(
            f"malformed advisor {token!r}: use 'provider' or 'provider:model'")
    provider, model = parts[0].strip(), parts[1].strip()
    if not provider:
        raise argparse.ArgumentTypeError(f"malformed advisor {token!r}: empty provider")
    if not model:
        raise argparse.ArgumentTypeError(f"malformed advisor {token!r}: empty model after ':'")
    return provider, model


class AdvisorsAction(argparse.Action):
    """Cap ``set-advisors`` at ``MAX_ADVISORS`` tokens at PARSE time, so a 4th token is a clean parser error
    (``argparse`` exits 2) rather than a late runtime failure. ``nargs="+"`` already guarantees ≥1; this only
    rejects the upper bound, then stores the parsed list exactly like the default ``store`` action (the
    ``setattr`` is not optional — without it ``args.advisors`` would be unset)."""

    def __call__(self, parser, namespace, values, option_string=None):  # noqa: ANN001
        if len(values) > MAX_ADVISORS:
            parser.error(f"set-advisors takes at most {MAX_ADVISORS} advisors (got {len(values)})")
        setattr(namespace, self.dest, values)


def cmd_remote_router(args: argparse.Namespace) -> int:
    """`grid router status|enable|disable|set-advisors|remove-advisor|models` — configure auto-routing on a
    grid you own.

    Account-level (session token, like `grid members`): resolves the grid locally and never needs it running,
    so there is no relay call. ``models`` is answered BEFORE grid resolution — it reads the account-level
    catalog and needs no grid. Every control-plane reply is masked (``{provider, model}`` pairs, never a
    key or URL) and echoed as-is for ``--json`` (after a defensive scrub); human output is built with
    ``.get()`` and never indexes the reply shape. There is NO key input anywhere in this group."""
    from remote import control_plane, credentials

    from . import remote_grid

    session = credentials.require_session()

    # `models` is account-level (catalog only) — resolve NO grid, so it works with none selected/running.
    if args.subcommand == "models":
        catalog = control_plane.get_router_catalog(session)
        return _print_catalog(catalog, args.json)

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

    if args.subcommand == "set-advisors":
        result = control_plane.set_advisors(session, network_id, args.advisors)
        chain = " ".join(_fmt_token(provider, model) for provider, model in args.advisors)
        return _print_mutation(result, args.json, f"Set advisors: {chain}.")

    if args.subcommand == "remove-advisor":
        provider, model = args.advisor
        result = control_plane.remove_advisor(session, network_id, provider, model)
        return _print_mutation(result, args.json, f"Removed advisor {_fmt_token(provider, model)}.")

    raise SystemExit(f"Unknown router subcommand: {args.subcommand!r}")


def _fmt_token(provider: str, model: str | None) -> str:
    """Render an advisor as its ``provider[:model]`` token — bare provider when the model is unset/blank."""
    return f"{provider}:{model}" if model else provider


def _safe_reply(reply: Any) -> dict[str, Any]:
    """Guard + scrub a control-plane reply before it is formatted or echoed. A non-dict 2xx body is
    off-contract — fail with a clean ``SystemExit`` rather than an ``AttributeError`` traceback (the same
    "never a traceback" idiom as ``_send``/``_raise`` and ``cli/remote_provider.py``'s shape check). Then
    strip any key-material field at any depth (defence in depth over the control plane's own masking)."""
    if not isinstance(reply, dict):
        raise SystemExit(f"The control plane returned an unexpected router reply: {str(reply)[:200]}")
    return _strip_secrets(reply)


def _strip_secrets(value: Any) -> Any:
    """Recursively drop any ``_SECRET_KEYS`` field from a reply so key or URL material can never be echoed."""
    if isinstance(value, dict):
        return {k: _strip_secrets(v) for k, v in value.items() if k.lower() not in _SECRET_KEYS}
    if isinstance(value, list):
        return [_strip_secrets(item) for item in value]
    return value


def _print_status(config: dict[str, Any], as_json: bool) -> int:
    """Show routing state + the advisor chain as ordered ``provider:model`` tokens. ``--json`` echoes the
    (scrubbed, already-masked) reply; human output uses ``.get()`` throughout and never indexes the shape."""
    config = _safe_reply(config)
    if as_json:
        print(json.dumps(config, indent=2))
        return 0
    print(f"auto-routing: {'enabled' if config.get('enabled') else 'disabled'}")
    advisors = config.get("advisors") or []
    if not advisors:
        print("(no advisors configured)")
        return 0
    for advisor in advisors:
        print(_fmt_token(advisor.get("provider") or "", advisor.get("model")))
    return 0


def _print_catalog(catalog: dict[str, Any], as_json: bool) -> int:
    """Show the advisor catalog for ``grid router models`` — every provider's whitelisted models, with the
    default marked. ``--json`` echoes the (scrubbed) reply; human output uses ``.get()`` throughout."""
    catalog = _safe_reply(catalog)
    if as_json:
        print(json.dumps(catalog, indent=2))
        return 0
    providers = catalog.get("providers") or []
    if not providers:
        print("(no advisor providers available)")
        return 0
    for provider in providers:
        name = provider.get("provider") or ""
        default = provider.get("default_model")
        for model in provider.get("models") or []:
            suffix = "  (default)" if model == default else ""
            print(f"{_fmt_token(name, model)}{suffix}")
    return 0


def _print_mutation(result: dict[str, Any], as_json: bool, human: str) -> int:
    """Confirm a router mutation. ``--json`` echoes the raw (scrubbed, masked) reply verbatim. On the human
    path the confirmation is a fixed string; when the control plane reports ``synced: false`` (saved, but the
    best-effort push to the running master didn't land) a caveat is appended so the creator isn't told it's
    live when the periodic snapshot hasn't applied it yet."""
    result = _safe_reply(result)
    if as_json:
        print(json.dumps(result, indent=2))
        return 0
    # Only an explicit ``synced: false`` appends the caveat. An absent key (older server) OR an empty ``{}``
    # body (a 204-equivalent DELETE coerced by ``_json_or_empty``) reads as "no not-synced signal" → no
    # caveat, so on that defensive empty-reply path an actually-unknown sync state prints as a plain
    # confirmation. Accepted: the real mutation endpoints always return a full ``{…, synced}`` body.
    if result.get("synced") is False:
        human += " (saved; the running grid will apply it shortly)"
    print(human)
    return 0
