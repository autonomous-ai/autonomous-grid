"""Grid lifecycle + overview: `grid`, `grid version`, `grid up/down/ls/info`."""
from __future__ import annotations

import argparse
import json
import re
from typing import Any

import httpx

from local import config
from local import runtime
from shared import state
from shared._version import __version__


def cmd_version(args: argparse.Namespace) -> int:
    print(f"grid {__version__}")
    return 0


def cmd_up(args: argparse.Namespace) -> int:
    name = args.name or "home"
    cfg = _grid_by_name(name)
    if cfg is None:
        _reject_foreign_grid(name)  # a known remote grid or an id-shaped arg → don't auto-create junk
        cfg = runtime.init_grid_config(
            name=name,
            port=args.port,
            host=args.host,
            advertise_host=args.advertise_host,
        )
    runtime.start_grid(cfg)
    print(f"grid={cfg['name']}")
    print(f"grid_url={runtime.grid_url(cfg)}")
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    cfg = config.select_grid(args.name)
    if not cfg.get("managed_server", True):
        print(f"{cfg['name']} is hosted by another box; nothing to stop here.")
        return 0
    runtime.stop_grid(cfg)
    print(f"Grid {cfg['name']} is down (config kept; `grid up {cfg['name']}` brings it back).")
    return 0


def cmd_ls(args: argparse.Namespace) -> int:
    grids = config.iter_grid_configs()
    if getattr(args, "json", False):
        print(json.dumps([
            {
                "grid": cfg["name"],
                "id": cfg["grid_id"],
                "grid_url": runtime.grid_url(cfg),
                "local": bool(cfg.get("managed_server", True)),
            }
            for cfg in grids
        ], indent=2))
        return 0
    if not grids:
        print("(no grids — run `grid up` to bring one online)")
        return 0
    for cfg in grids:
        where = "local" if cfg.get("managed_server", True) else "remote"
        print(f"{cfg['name']}\t{cfg['grid_id']}\t{where}\t{runtime.grid_url(cfg)}")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    cfg = config.select_grid(args.grid)
    grid_url = runtime.grid_url(cfg)

    if args.env:
        print(f'export OPENAI_BASE_URL="{grid_url}/v1"')
        print('export OPENAI_API_KEY="local-grid"')
        return 0

    engines, reachable = _live_engines(grid_url)
    models = _unique_models(engines)

    if args.json:
        print(json.dumps({
            "grid": cfg["name"],
            "grid_url": grid_url,
            "engines": [_engine_entry(engine) for engine in engines],
            "models": models,
        }, indent=2))
        return 0

    print(f"grid={cfg['name']}")
    print(f"grid_url={grid_url}")
    if not reachable:
        print("status=unreachable")
        return 0
    print(f"engines={len(engines)}")
    print(f"models={','.join(models) if models else '(none)'}")
    return 0


# ---------------------------------------------------------------------------
# overview (`grid` with no subcommand)
# ---------------------------------------------------------------------------

def cmd_overview(args: argparse.Namespace) -> int:
    # Mode is stamped by dispatch; fall back to the persisted mode for direct calls.
    mode = getattr(args, "mode", None) or state.get_mode()
    as_json = getattr(args, "json", False)
    if mode == "remote":
        return _overview_remote(as_json)
    return _overview_local(as_json)


def _overview_remote(as_json: bool) -> int:
    active = state.get_active("remote")
    if as_json:
        print(json.dumps({"mode": "remote", "grid": active}, indent=2))
        return 0
    print("mode: remote")
    print(f"active grid: {active}" if active else "active grid: (none)")
    print("\nSign in with `grid login`, then manage your remote grids with `grid up`/`ls`/`info`, "
          "serve models with `grid join`, and use them with `grid chat -m <model> \"…\"`.")
    return 0


def _overview_local(as_json: bool) -> int:
    grids = config.iter_grid_configs()
    if not grids:
        if as_json:
            print(json.dumps(
                {"mode": "local", "grid": None, "grid_url": None, "engines": [], "models": []},
                indent=2,
            ))
            return 0
        print("mode: local\n")
        print("No grid yet.\n")
        print("Start one:\n  grid up\n")
        print("Then join an engine:\n  grid join")
        return 0

    default = config.select_grid(None) if _has_default(grids) else grids[0]
    grid_url = runtime.grid_url(default)
    engines, reachable = _live_engines(grid_url)
    models = _unique_models(engines)

    if as_json:
        print(json.dumps({
            "mode": "local",
            "grid": default["name"],
            "grid_url": grid_url,
            "engines": [_engine_entry(engine) for engine in engines],
            "models": models,
        }, indent=2))
        return 0

    print("mode: local")
    print(f"Grid: {default['name']}")
    print(f"grid_url: {grid_url}")
    if not reachable:
        print("status: unreachable — start it with `grid up`")
    else:
        print(f"engines: {len(engines)} live")
        print(f"models: {', '.join(models) if models else '(none)'}")
    print("\nNext:")
    print("  grid join")
    if models:
        print(f'  grid chat -m {models[0]} "hello"')
    print("  grid info --env")
    return 0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

# Local grid ids are minted as ``ag-<slug>-<hex8>`` (local/runtime.init_grid_config). `grid up` uses this
# to refuse auto-creating a junk grid when the arg is an unsynced id, not a new name (ADR 0011 D-f).
# fullmatch (anchored) so a real name like ``ag-team`` still creates.
_GRID_ID_RE = re.compile(r"ag-.+-[0-9a-f]{8}")


def _looks_like_grid_id(name: str) -> bool:
    return bool(_GRID_ID_RE.fullmatch(name))


def _reject_foreign_grid(name: str) -> None:
    """Refuse to auto-create when `name` is really an existing grid the user hasn't synced here — one of
    their known remote grids (exact name/id match, zero false-positive), or a string shaped like a minted
    local grid id — instead of silently making a junk local grid named after it (ADR 0011 D-f). Runs
    before any create/start, so nothing is written or spawned."""
    from . import remote_grid

    if remote_grid._by_name(name) is not None:  # a grid from `grid login`, pasted in local mode
        raise SystemExit(
            f"{name!r} is one of your remote grids, not a new local grid. Switch to it with "
            f"`grid mode remote` (or `grid --remote up {name}`)."
        )
    if _looks_like_grid_id(name):
        raise SystemExit(
            f"No local grid with id {name!r}. That looks like a grid id, not a new grid's name — run "
            f"`grid ls` to see your grids (or `grid mode remote` + `grid sync` for a remote one)."
        )


def _grid_by_name(name: str) -> dict[str, Any] | None:
    for cfg in config.iter_grid_configs():
        if cfg.get("name") == name or cfg.get("grid_id") == name:
            return cfg
    return None


def _has_default(grids: list[dict[str, Any]]) -> bool:
    active = state.get_active("local")
    if active and any(cfg.get("grid_id") == active or cfg.get("name") == active for cfg in grids):
        return True
    return len(grids) == 1 or any(cfg.get("name") == "home" for cfg in grids)


def _live_engines(grid_url: str) -> tuple[list[dict[str, Any]], bool]:
    try:
        resp = httpx.get(f"{grid_url}/nodes/discover", timeout=3)
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError):
        return [], False
    if not isinstance(body, dict):
        return [], False
    return body.get("engines", []), True


def _engine_entry(engine: dict[str, Any]) -> dict[str, Any]:
    return {
        "engine": engine.get("name") or engine.get("node_id", "?"),
        "where": engine.get("endpoint_url") or engine.get("media_url") or "",
        "models": engine.get("models") or [],
    }


def _unique_models(engines: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for engine in engines:
        for model in engine.get("models") or []:
            if model not in seen:
                seen.append(model)
    return seen
