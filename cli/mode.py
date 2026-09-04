"""`grid mode` and `grid use`: read/switch the mode and the per-mode active grid.

Both are mode-agnostic (they run in either mode and are never gated). `cmd_mode`
reports/sets the *persisted* mode and deliberately ignores the `--local`/`--remote`
override; `cmd_use` acts on the *resolved* mode that dispatch stamps on ``args.mode``.
"""
from __future__ import annotations

import argparse
import json
import shlex

from local import config
from shared import state

from .next_steps import print_next_steps


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
        print("Remote mode: `grid login` to sign in, then `grid start` to bring a remote grid online, "
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
    # Selecting a grid is a middle step, never the goal: say what the grid is now for. Suppressed
    # under --json, which on this path already prints the plain line above rather than a document.
    if not getattr(args, "json", False):
        _print_use_next_steps(mode, name)
    return 0


def _print_use_next_steps(mode: str, name: str) -> None:
    """What to do with the grid you just selected. `grid join` is the one line that differs by mode:
    remote joins by grid *name* (the relay knows where it is), local needs the grid's URL."""
    target = shlex.quote(name) if mode == "remote" else "<grid-url>"
    print_next_steps([
        ("grid models", "see what this grid serves"),
        ('grid chat -m <model> "hello"', "talk to a model"),
        # The exports that point any OpenAI-dialect client (opencode, codex, Cursor) at this grid.
        ("grid info --env", "point coding agents at it (opencode, codex, …)"),
        (f"grid join {target} --serve <model>", "optional: serve a model to it"),
    ])
    # Set apart from the list above because it is not a fifth thing to choose between: it is the
    # one line to paste when the answer is "a coding agent". `print_next_steps` already ended with a
    # blank line, so this stands on its own.
    print(f'To point a coding agent at this grid, run:  eval "$(grid info --env)"')
    print("")


def _require_local_grid(name: str) -> None:
    for cfg in config.iter_grid_configs():
        if cfg.get("name") == name or cfg.get("grid_id") == name:
            return
    raise SystemExit(
        f"Grid not found: {name!r}. Run `grid start {shlex.quote(name)}` on this device, or "
        "`grid ls` to see your grids."
    )
