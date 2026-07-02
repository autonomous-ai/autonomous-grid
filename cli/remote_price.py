"""Remote-mode `grid price set|rm|show` — manage this engine's authoritative model price.

The price is the per-provider rate the relay uses to bill and to pick the cheapest engine (it replaces
the old advertise-only `--pricing-input/--pricing-output` join flags). Each call resolves the grid + relay
base + per-grid access token (like the use/serve path) and talks to the relay's `/relay/v1/grid/models`.

Remote-only — `cli.dispatch` gates it (in `REMOTE_ONLY`); local mode exits with guidance. Import rule
mirrors the other remote handlers: stdlib only at module top; `remote.*` / sibling cli modules imported
lazily.
"""
from __future__ import annotations

import argparse
import json


def _resolve(args: argparse.Namespace) -> tuple[str, str, str]:
    """(relay_base, access_token, label) for the selected grid. Clean SystemExit if signed-out / no token."""
    from remote import credentials

    from . import remote_grid

    session = credentials.require_session()
    rec = remote_grid._select(getattr(args, "grid", None))
    network_id = remote_grid._network_id(rec)
    label = rec.get("name") or network_id
    token = rec.get("access_token")
    if not token:
        raise SystemExit(f"Grid {label} has no access token locally. Run `grid login` to refresh your grids.")
    base, _status = remote_grid.resolve_relay_base(session, rec, network_id, label)
    return base, token, label


def cmd_remote_price(args: argparse.Namespace) -> int:
    if args.subcommand == "set":
        return _price_set(args)
    if args.subcommand == "show":
        return _price_show(args)
    # argparse (required=True + choices) guarantees the rest is rm/delete; guard explicitly anyway.
    if args.subcommand not in ("rm", "delete"):
        raise SystemExit(f"Unknown price subcommand: {args.subcommand!r}")
    return _price_rm(args)


def _price_set(args: argparse.Namespace) -> int:
    from remote import relay

    # Only chat is priced today; image/video need per-unit rate shapes + relay tables (later).
    if args.type != "chat":
        raise SystemExit(f"--type {args.type!r} isn't supported yet; only 'chat' has pricing for now.")
    base, token, label = _resolve(args)
    try:
        relay.set_model_price(
            base, token, model=args.model, modality="chat",
            input_rate=args.input, output_rate=args.output, cache_rate=args.cache,
            name=getattr(args, "name", None), maker=getattr(args, "maker", None),
            status=getattr(args, "status", None),
            context_length=getattr(args, "context_length", None),
        )
    except SystemExit as exc:
        # The relay rejects a price for a model this engine isn't actively serving (403). Point the
        # operator at `grid join` rather than leaving the raw status line.
        if "(403)" in str(exc):
            raise SystemExit(
                f"Can't set the price for {args.model!r}: this engine isn't serving it on {label}. "
                f"Join it first (`grid join`), then set the price."
            ) from None
        raise
    print(
        f"Set price for {args.model} on {label}: "
        f"input={args.input} output={args.output} cache={args.cache} (USD per 1M tokens)."
    )
    return 0


def _price_rm(args: argparse.Namespace) -> int:
    from remote import relay

    base, token, label = _resolve(args)
    relay.delete_model_price(base, token, args.model)  # 404 → clean SystemExit from the relay client
    print(f"Removed your price for {args.model} on {label}.")
    return 0


def _price_show(args: argparse.Namespace) -> int:
    from remote import relay

    base, token, label = _resolve(args)
    data = relay.list_model_prices(base, token)
    models = data.get("models") if isinstance(data, dict) else data
    models = list(models or [])
    if args.json:
        print(json.dumps(models, indent=2))
        return 0
    if not models:
        print(f"(no model prices on {label})")
        return 0
    for m in models:
        print(
            f"{m.get('model') or '':<24} input={m.get('input_rate', 0)} "
            f"output={m.get('output_rate', 0)} cache={m.get('cache_rate', 0)} "
            f"status={m.get('status') or ''}"
        )
    return 0
