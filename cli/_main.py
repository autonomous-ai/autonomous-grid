"""CLI entry point: dispatch internal subcommands, otherwise parse and run."""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

from local import config
from local import runtime
from shared import logging_setup, paths
from .dispatch import dispatch, resolve_override
from .parser import build_parser


def cmd_internal_server(grid_id: str) -> int:
    import uvicorn

    cfg = config.load_grid_config(grid_id)
    if not cfg:
        raise SystemExit(f"Grid config not found: {grid_id}")
    from local.server import create_app

    app = create_app(grid_id=cfg["grid_id"], grid_name=cfg["name"])
    host = cfg.get("host") or runtime.DEFAULT_HOST
    port = int(cfg["port"])
    level = os.getenv("UVICORN_LOG_LEVEL", "info").upper()
    if level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}:
        level = "INFO"  # a typo'd level would otherwise crash dictConfig at boot

    # The signaling server logs one line per HTTP request (heartbeats, health checks) — the fastest
    # unbounded grower on a long-running grid. Give uvicorn an in-process rotating handler so it owns
    # server.log; the raw stdout/stderr redirect in local.runtime is now crash-only (server.err).
    log_path = paths.grid_dir(grid_id) / "server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    max_bytes, backup_count = logging_setup.server_log_limits()
    old_size = logging_setup.truncate_if_oversized(log_path, max_bytes)
    if old_size is not None:
        _note_server_log_truncation(log_path, old_size, max_bytes)
    # Pass ONLY log_config (no log_level=/use_colors=) so our dictConfig is the single source of truth.
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_config=logging_setup.build_uvicorn_log_config(
            log_path, max_bytes=max_bytes, backup_count=backup_count, level=level
        ),
    )
    return 0


def _note_server_log_truncation(log_path, old_size: int, max_bytes: int) -> None:
    """Write the boot-time truncation warning as the first line of the fresh server.log (the file the
    user tails), since it must happen before uvicorn configures its own logging."""
    # Local time (no tz) to match uvicorn's %(asctime)s, which uses time.localtime.
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(
                f"{ts} WARNING server.log was {old_size} bytes (> {max_bytes}); "
                f"truncated on startup, tail preserved (best-effort) in "
                f"{os.path.basename(os.fspath(log_path))}.oversized\n"
            )
    except OSError as exc:
        sys.stderr.write(f"grid: could not write truncation notice to {os.fspath(log_path)}: {exc!r}\n")


def cmd_internal_media_server(port: int, comfyui_url: str) -> int:
    import uvicorn

    from local.media_server import create_app

    app = create_app(comfyui_url=comfyui_url)
    uvicorn.run(app, host="0.0.0.0", port=int(port))
    return 0


def cmd_internal_api_media_server(port: int, api_kind: str, base_url: str) -> int:
    """The API media bridge (`grid join --api <kind>` in local mode).

    Binds LOOPBACK, unlike `__media-server`: this process holds the vendor credential, so the only
    thing that should be able to reach it is the grid proxy on this same box. The key arrives in the
    environment (`GRID_API_MEDIA_KEY`) rather than argv, which `ps` exposes to every local user.
    """
    import uvicorn

    from local.api_media_server import create_app

    api_key = os.environ.get("GRID_API_MEDIA_KEY", "")
    if not api_key:
        raise SystemExit(
            "GRID_API_MEDIA_KEY is not set; the API media bridge has no credential to serve with."
        )
    app = create_app(api_kind=api_kind, base_url=base_url, api_key=api_key)
    uvicorn.run(app, host="127.0.0.1", port=int(port))
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
    if argv[0] == "__api-media-server":
        parser = argparse.ArgumentParser(prog="grid __api-media-server")
        parser.add_argument("--port", type=int, required=True)
        parser.add_argument("--api-kind", required=True)
        parser.add_argument("--base-url", required=True)
        args = parser.parse_args(argv[1:])
        return cmd_internal_api_media_server(args.port, args.api_kind, args.base_url)
    if argv[0] == "__engine":
        from .provider import run_engine_from_record

        parser = argparse.ArgumentParser(prog="grid __engine")
        parser.add_argument("grid_id")
        parser.add_argument("engine_id")
        args = parser.parse_args(argv[1:])
        return run_engine_from_record(args.grid_id, args.engine_id)
    if argv[0] == "__remote-engine":
        from remote.serve import run_remote_engine_from_record

        parser = argparse.ArgumentParser(prog="grid __remote-engine")
        parser.add_argument("grid_id")
        parser.add_argument("engine_id")
        args = parser.parse_args(argv[1:])
        return run_remote_engine_from_record(args.grid_id, args.engine_id)
    return None


