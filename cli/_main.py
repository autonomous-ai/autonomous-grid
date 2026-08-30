"""CLI entry point: dispatch internal subcommands, otherwise parse and run."""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

from local import config
from local import runtime
from shared import logging_setup, paths, process_home, run_records
from . import json_error
from .dispatch import dispatch, resolve_override, split_forwarded
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


def cmd_internal_cli_seat_server(args) -> int:
    """The CLI seat's loopback server. Binds LOOPBACK like `__api-media-server`: this process can
    spend the operator's subscription, so only the grid proxy on this box should reach it."""
    import uvicorn

    from local.cli_seat_server import create_app
    from shared.agent.cli_seat import SeatOptions
    from shared.agent.seats import seat_for

    spec = seat_for(args.kind)
    options = SeatOptions(
        port=args.port, timeout=args.timeout, concurrency=args.concurrency,
        session_limit=args.session_limit, week_limit=args.week_limit, quota_ttl=args.quota_ttl,
    )
    app = create_app(spec=spec, binary=args.binary, options=options)
    uvicorn.run(app, host="127.0.0.1", port=options.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    internal = _maybe_internal(raw_argv)
    if internal is not None:
        return internal
    override, cleaned = resolve_override(raw_argv)
    # After the override strip, and before the parser sees anything: the forwarded vector must never
    # reach argparse, which would bind the app's own flags to this CLI's positionals.
    cleaned, forwarded = split_forwarded(cleaned)
    parser = build_parser()
    # ADR 0034 D-m (issue 46): a refusal an application can branch on. Written here rather than in
    # each handler because every command an application drives has to carry it, and a new one would
    # otherwise have to remember — the failure being one nobody sees until a client is already
    # parsing prose.
    #
    # ⚠️ **`parse_args` is INSIDE the guard, and that was found in review.** argparse raises its own
    # `SystemExit(2)` for an argv that does not parse, before `dispatch` is ever reached — so a
    # usage error was the one failure that reached an application as prose. Worse than prose: `2` is
    # this CLI's code for *not finished yet, ask again* (issue 32), so a client polling with one
    # flag this build does not know would loop for ever on it. Version skew between the app and the
    # binary is exactly the case a spawned-subprocess client cannot rule out.
    #
    # ⚠️ **Re-raised, never returned.** `SystemExit` out of `main()` is this plane's error contract;
    # see `cli/json_error.py` for the callers and tests that depend on it.
    args = None
    try:
        args = parser.parse_args(cleaned)
        if forwarded:
            # Only when there is something to forward, so every other command's namespace is
            # untouched. The parser declares `forward=()` on `launch`, so its handler always finds
            # the attribute — `grid launch claude --` with nothing after it is therefore identical
            # to no `--` at all.
            args.forward = forwarded
        return dispatch(args, override)
    except SystemExit as exc:
        # `args` is `None` when argparse itself refused, so the flag is read out of the raw argv —
        # see `json_error.asked_for_json`.
        json_error.refuse_as_json(args, exc, argv=cleaned)
        raise


def _maybe_internal(argv: list[str]) -> int | None:
    if not argv:
        return None
    # Detached engine children carry their GRID_HOME identity before the dispatch marker so a
    # process-table sweep can separate two installations serving the same grid on one machine.
    # Validate it in the child too: a copied/stale argv under a different environment must fail
    # closed instead of starting a process its installation can no longer identify safely.
    engine_markers = {run_records.LOCAL_ENGINE_MARKER, run_records.REMOTE_ENGINE_MARKER}
    if len(argv) >= 2 and argv[1] in engine_markers and argv[0].startswith(
        process_home.HOME_TAG_ARG_PREFIX
    ):
        supplied = process_home.tag_from_arg(argv[0])
        expected = process_home.own_tag()
        if supplied is None or expected is None or supplied != expected:
            raise SystemExit("Detached Grid engine GRID_HOME identity does not match this process.")
        argv = argv[1:]
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
    if argv[0] == "__cli-seat-server":
        parser = argparse.ArgumentParser(prog="grid __cli-seat-server")
        parser.add_argument("--kind", required=True)
        parser.add_argument("--binary", required=True)
        parser.add_argument("--port", type=int, required=True)
        parser.add_argument("--timeout", type=float, required=True)
        parser.add_argument("--concurrency", type=int, default=1)
        parser.add_argument("--session-limit", type=int, default=None)
        parser.add_argument("--week-limit", type=int, default=None)
        parser.add_argument("--quota-ttl", type=float, default=60.0)
        return cmd_internal_cli_seat_server(parser.parse_args(argv[1:]))
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

