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


def _list_targets_or_refuse(print_env: bool, forward: tuple[str, ...]) -> int:
    """Bare `grid launch` — the discovery listing, unless the user asked for something it cannot do.

    Both refusals exist because the listing exits **0**. Without them a user who named no app gets a
    helpful-looking list and a success code, with nothing to tell them that the flag or the arguments
    they typed went nowhere — the kind of failure found only by eventually wondering why.
    """
    choices = ", ".join(registry.names())
    if print_env:
        raise SystemExit(
            "`--print-env` needs a launch target: `grid launch <target> --print-env`. "
            f"Choose from: {choices}."
        )
    if forward:
        raise SystemExit(
            f"Nothing to pass {' '.join(forward)} to: `grid launch` on its own lists the apps it can "
            f"start. Name one — `grid launch <target> -- …`. Choose from: {choices}."
        )
    return _print_targets()


def cmd_launch(args: argparse.Namespace) -> int:
    # Both arrive from outside the parser: `forward` from `cli.dispatch.split_forwarded` (argparse
    # cannot hold it — see there), `print_env` from the parser itself.
    forward = tuple(getattr(args, "forward", ()) or ())
    print_env = bool(getattr(args, "print_env", False))
    if print_env and forward:
        # Before everything else, because it needs nothing else to be true: the two contradict each
        # other whatever grid, target or account is behind them. Refused rather than ignored —
        # printing a block that drops the arguments the user typed, and exiting 0 while doing it, is
        # a silent failure dressed as a success.
        raise SystemExit(
            "`--print-env` prints the environment instead of starting the app, so the arguments "
            "after `--` have nothing to reach. Pass one or the other."
        )
    target_name = getattr(args, "target", None)
    if not target_name:
        return _list_targets_or_refuse(print_env, forward)
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

    from . import grid_credential, remote_grid, remote_overview

    session_token = credentials.require_session()
    rec = remote_grid._select(getattr(args, "grid", None))
    label = str(rec.get("name") or rec.get("network_id") or "?")
    # Before the overview read on purpose: a token missing *locally* is the familiar `grid login`
    # failure, and a network round-trip cannot improve on it.
    token = remote_grid.require_access_token(rec, label)
    # The relay base comes from live status for the creator, or the login bundle for a member — the
    # same helper (and the same "isn't up; run `grid start`" error) `grid info --env` uses.
    base, _status = remote_grid.resolve_relay_base(
        session_token, rec, remote_grid._network_id(rec), label
    )
    # Credential before models, and the order is not cosmetic (ADR 0029). A dead credential invalidates
    # the model advice — the target's refusal sends the user off to `grid join` an engine, which cannot
    # help while the token is dead — whereas a missing model says nothing about the credential. `rec` is
    # a disk snapshot, so the possibly-refreshed token is what comes back; `rec`'s would be stale.
    token = grid_credential.ensure_usable(rec, label, token, base)
    # Preflight's half of the read: which models the grid serves. The public overview, the same one
    # `grid models` / `grid engines` render, so preflight and those commands can never disagree about
    # what is live. Whether those models are *enough* is the target's call — it owns its model names.
    live_models = remote_overview.live_model_names(
        remote_overview.fetch_overview(base, token, label)
    )
    session = GridSession(
        label=label, relay_base=base, access_token=token, live_models=live_models
    )
    if print_env:
        return target.print_env(session)
    # Whatever the user typed after `--`, already taken out of argv by `cli.dispatch.split_forwarded`.
    return target.run(session, forward)
