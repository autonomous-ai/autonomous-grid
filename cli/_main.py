"""CLI entry point: dispatch internal subcommands, otherwise parse and run."""
from __future__ import annotations

import argparse
import sys

from lan import config
from lan import runtime
from .dispatch import dispatch, resolve_override
from .parser import build_parser


def cmd_internal_server(grid_id: str) -> int:
    import uvicorn

    cfg = config.load_grid_config(grid_id)
    if not cfg:
        raise SystemExit(f"Grid config not found: {grid_id}")
    from lan.server import create_app

    app = create_app(grid_id=cfg["grid_id"], grid_name=cfg["name"])
    uvicorn.run(app, host=cfg.get("host") or runtime.DEFAULT_HOST, port=int(cfg["port"]))
    return 0


def cmd_internal_media_server(port: int, comfyui_url: str) -> int:
    import uvicorn

    from lan.media_server import create_app

    app = create_app(comfyui_url=comfyui_url)
    uvicorn.run(app, host="0.0.0.0", port=int(port))
    return 0


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    internal = _maybe_internal(raw_argv)
    if internal is not None:
        return internal
    override, cleaned = resolve_override(raw_argv)
    parser = build_parser()
    args = parser.parse_args(cleaned)
    return dispatch(args, override)


def _maybe_internal(argv: list[str]) -> int | None:
    if not argv:
        return None
    if argv[0] == "__server":
        parser = argparse.ArgumentParser(prog="grid __server")
        parser.add_argument("grid_id")
        args = parser.parse_args(argv[1:])
        return cmd_internal_server(args.grid_id)
    if argv[0] == "__media-server":
        parser = argparse.ArgumentParser(prog="grid __media-server")
        parser.add_argument("--port", type=int, required=True)
        parser.add_argument("--comfyui-url", required=True)
        args = parser.parse_args(argv[1:])
        return cmd_internal_media_server(args.port, args.comfyui_url)
    if argv[0] == "__engine":
        from .provider import run_engine_from_record

        parser = argparse.ArgumentParser(prog="grid __engine")
        parser.add_argument("grid_id")
        parser.add_argument("engine_id")
        args = parser.parse_args(argv[1:])
        return run_engine_from_record(args.grid_id, args.engine_id)
    if argv[0] == "__internet-engine":
        from internet.serve import run_internet_engine_from_record

        parser = argparse.ArgumentParser(prog="grid __internet-engine")
        parser.add_argument("grid_id")
        parser.add_argument("engine_id")
        args = parser.parse_args(argv[1:])
        return run_internet_engine_from_record(args.grid_id, args.engine_id)
    return None


