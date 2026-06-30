"""`grid mode` and `grid use`: read/switch the mode and the per-mode active grid.

Both are mode-agnostic (they run in either mode and are never gated). `cmd_mode`
reports/sets the *persisted* mode and deliberately ignores the `--local`/`--remote`
override; `cmd_use` acts on the *resolved* mode that dispatch stamps on ``args.mode``.
"""
from __future__ import annotations

import argparse
import json

from local import config
from shared import state


def cmd_mode(args: argparse.Namespace) -> int:
    target = getattr(args, "target", None)
    if target is not None:
        state.set_mode(target)
    mode = state.get_mode()
    if getattr(args, "json", False):
        print(json.dumps({"mode": mode}))
        return 0
    print(mode)
    if target == "remote":
        print("Remote mode: `grid login` to sign in, then `grid up` to bring a remote grid online, "
              "`grid join` to serve models to it, and `grid chat -m <model> \"…\"` to use them.")
    return 0


def cmd_use(args: argparse.Namespace) -> int:
    mode = getattr(args, "mode", None) or state.get_mode()
    name = getattr(args, "name", None)

    if getattr(args, "none", False):
        if name is not None:
            raise SystemExit("Pass either a grid name or --none, not both.")
        state.set_active(mode, None)
        print(f"active grid cleared for {mode} mode")
        return 0

    if name is None:
        active = state.get_active(mode)
        if getattr(args, "json", False):
            print(json.dumps({"mode": mode, "active": active}))
        elif active:
            print(active)
        else:
            print("(no active grid — set one with `grid use <name>`)")
        return 0

    if mode == "local":
        _require_local_grid(name)
    state.set_active(mode, name)
    print(f"active grid for {mode} mode: {name}")
    return 0


def _require_local_grid(name: str) -> None:
    for cfg in config.iter_grid_configs():
        if cfg.get("name") == name or cfg.get("grid_id") == name:
            return
    raise SystemExit(
        f"Grid not found: {name!r}. Run `grid up {name}` on this device, or `grid ls` "
        "to see your grids."
    )
