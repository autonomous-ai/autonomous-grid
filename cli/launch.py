"""`grid launch <target> [grid]` — start an app already pointed at a remote grid (ADR 0028).

Remote-only: ``cli.dispatch`` gates it in local mode with the dialect as the reason (a local grid
serves chat/completions, and Claude Code speaks only Anthropic Messages).

This module is the only place that knows *which* grid a launch uses — it resolves the grid, its
access token and its relay base with the same helpers ``grid info --env`` uses, then hands the target
a value object. ``shared/launch`` therefore never imports ``cli``.
"""
from __future__ import annotations

import argparse

from shared.launch import registry
from shared.launch.target import GridSession


def _print_targets() -> int:
    """Bare `grid launch`: what can be launched. Discovery, so it exits 0 and needs no account."""
    print("Launch targets:")
    for target in registry.TARGETS.values():
        print(f"  {target.name}\t{target.label}")
    print("\nStart one with `grid launch <target>`.")
    return 0


def cmd_launch(args: argparse.Namespace) -> int:
    target_name = getattr(args, "target", None)
    if not target_name:
        return _print_targets()
    target = registry.get(target_name)
    if target is None:
        # Before the session gate on purpose: a typo is not a sign-in problem, and reporting it as
        # one sends the user to `grid login` for a mistake `grid login` cannot fix.
        raise SystemExit(
            f"Unknown launch target {target_name!r}. Choose from: {', '.join(registry.names())}."
        )
    # `remote.*` and the `cli` siblings are imported here, not at module top: `cli/parser.py` imports
    # this module while the `cli` package is still initialising (same rule as `cli/remote_overview.py`).
    from remote import credentials

    from . import remote_grid, remote_overview

    session_token = credentials.require_session()
    rec = remote_grid._select(getattr(args, "grid", None))
    label = str(rec.get("name") or rec.get("network_id") or "?")
    # Before the overview read on purpose: a token missing *locally* is the familiar `grid login`
    # failure, and a network round-trip cannot improve on it.
    token = remote_grid.require_access_token(rec, label)
    # The relay base comes from live status for the creator, or the login bundle for a member — the
    # same helper (and the same "isn't up; run `grid up`" error) `grid info --env` uses.
    base, _status = remote_grid.resolve_relay_base(
        session_token, rec, remote_grid._network_id(rec), label
    )
    # Preflight's half of the read: which models the grid serves. The public overview, the same one
    # `grid models` / `grid engines` render, so preflight and those commands can never disagree about
    # what is live. Whether those models are *enough* is the target's call — it owns its model names.
    #
    # The route is public and ignores the bearer token, so this proves model presence, not credential
    # validity: an expired token passes here and fails inside the app. Known and accepted (ADR 0028);
    # the authenticated model-listing route is the recorded fix if it bites.
    live_models = remote_overview.live_model_names(
        remote_overview.fetch_overview(base, token, label)
    )
    return target.run(
        GridSession(label=label, relay_base=base, access_token=token, live_models=live_models)
    )
