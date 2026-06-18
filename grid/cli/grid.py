"""Grid lifecycle + overview: `grid`, `grid version`, `grid up/down/ls/info`."""
from __future__ import annotations

import argparse
import json
from typing import Any

import httpx

from .. import __version__, config, runtime


def cmd_version(args: argparse.Namespace) -> int:
    print(f"grid {__version__}")
    return 0


def cmd_up(args: argparse.Namespace) -> int:
    name = args.name or "home"
    cfg = _grid_by_name(name) or runtime.init_network_config(
        name=name,
        port=args.port,
        host=args.host,
        advertise_host=args.advertise_host,
    )
    runtime.start_server(cfg)
    print(f"grid={cfg['name']}")
    print(f"grid_url={runtime.network_url(cfg)}")
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    cfg = config.select_grid(args.name)
    if not cfg.get("managed_server", True):
        print(f"{cfg['name']} is hosted by another box; nothing to stop here.")
        return 0
    runtime.stop_server(cfg)
    print(f"Grid {cfg['name']} is down (config kept; `grid up {cfg['name']}` brings it back).")
    return 0


def cmd_ls(args: argparse.Namespace) -> int:
    grids = config.iter_network_configs()
    if getattr(args, "json", False):
        print(json.dumps([
            {
                "grid": cfg["name"],
                "grid_url": runtime.network_url(cfg),
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
        print(f"{cfg['name']}\t{where}\t{runtime.network_url(cfg)}")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    cfg = config.select_grid(args.grid)
    grid_url = runtime.network_url(cfg)

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
    grids = config.iter_network_configs()
    if not grids:
        print("No grid yet.\n")
        print("Start one:\n  grid up\n")
        print("Then join an engine:\n  grid join")
        return 0

    default = config.select_grid(None) if _has_default(grids) else grids[0]
    grid_url = runtime.network_url(default)
    engines, reachable = _live_engines(grid_url)
    models = _unique_models(engines)

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

def _grid_by_name(name: str) -> dict[str, Any] | None:
    for cfg in config.iter_network_configs():
        if cfg.get("name") == name or cfg.get("network_id") == name:
            return cfg
    return None


def _has_default(grids: list[dict[str, Any]]) -> bool:
    return len(grids) == 1 or any(cfg.get("name") == "home" for cfg in grids)


def _live_engines(grid_url: str) -> tuple[list[dict[str, Any]], bool]:
    try:
        resp = httpx.get(f"{grid_url}/nodes/discover", timeout=3)
        resp.raise_for_status()
    except httpx.HTTPError:
        return [], False
    return resp.json().get("providers", []), True


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
