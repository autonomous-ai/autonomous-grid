"""CLI entry point: dispatch internal subcommands, otherwise parse and run."""
from __future__ import annotations

import argparse
import sys

from .. import config, runtime
from .parser import build_parser


def cmd_internal_server(network_id: str) -> int:
    import uvicorn

    cfg = config.load_network_config(network_id)
    if not cfg:
        raise SystemExit(f"Network config not found: {network_id}")
    from ..server import create_app

    app = create_app(network_id=cfg["network_id"], network_name=cfg["name"])
    uvicorn.run(app, host=cfg.get("host") or runtime.DEFAULT_HOST, port=int(cfg["port"]))
    return 0


def cmd_internal_media_server(port: int, comfyui_url: str) -> int:
    import uvicorn

    from ..provider.media_server import create_app

    app = create_app(comfyui_url=comfyui_url)
    uvicorn.run(app, host="0.0.0.0", port=int(port))
    return 0


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    internal = _maybe_internal(raw_argv)
    if internal is not None:
        return internal
    parser = build_parser()
    args = parser.parse_args(raw_argv)
    return args.handler(args) or 0


def _maybe_internal(argv: list[str]) -> int | None:
    if not argv:
        return None
    if argv[0] == "__server":
        parser = argparse.ArgumentParser(prog="grid __server")
        parser.add_argument("network_id")
        args = parser.parse_args(argv[1:])
        return cmd_internal_server(args.network_id)
    if argv[0] == "__media-server":
        parser = argparse.ArgumentParser(prog="grid __media-server")
        parser.add_argument("--port", type=int, required=True)
        parser.add_argument("--comfyui-url", required=True)
        args = parser.parse_args(argv[1:])
        return cmd_internal_media_server(args.port, args.comfyui_url)
    if argv[0] == "__provider":
        from .provider import run_provider_from_record

        parser = argparse.ArgumentParser(prog="grid __provider")
        parser.add_argument("grid_id")
        parser.add_argument("engine_id")
        args = parser.parse_args(argv[1:])
        return run_provider_from_record(args.grid_id, args.engine_id)
    return None


