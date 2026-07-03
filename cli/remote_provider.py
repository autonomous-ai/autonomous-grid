"""Remote-mode `grid join` / `grid leave` — serve one engine to the active remote grid's relay.

Mirrors the local handlers (`cli/provider.py`) but resolves the grid + relay address from the remote
credential store and spawns the remote serve loop (`__remote-engine` → `remote/serve.py`) instead of
the local heartbeat loop. The engine record + teardown are the shared ones (`shared/run_records.py`),
so `grid leave` works the same in both modes. `grid join --all` serves several local engines under
one identity (the union of their models, model→engine routing — DECISIONS D9 / ADR 0007); local spawns
one identity per engine instead.

Import rule mirrors `cli/remote_grid.py`: only stdlib + `shared.*` at module top; `remote.*` and the
local runtime are imported lazily inside each handler, because `cli.dispatch` imports this module
while the `cli` package is still initialising.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import uuid

from shared import paths, run_records


def _reject_local_only_flags(args: argparse.Namespace) -> None:
    """local-only `grid join` flags have no meaning in remote mode (DECISIONS D8): a remote engine
    polls the relay outbound, so there is no inbound endpoint to advertise. (`--media` IS supported —
    a remote media engine's server is reached by the serve loop on loopback, not advertised.)"""
    if getattr(args, "advertise_host", None) is not None:
        raise SystemExit(
            "--advertise-host is local-only. A remote engine polls the relay outbound, so there is "
            "no inbound endpoint to advertise."
        )


def cmd_remote_join(args: argparse.Namespace) -> int:
    from remote import credentials

    from . import remote_grid

    _reject_local_only_flags(args)
    if getattr(args, "pricing_input", None) is not None or getattr(args, "pricing_output", None) is not None:
        print(
            "Note: --pricing-input/--pricing-output are deprecated and no longer advertise a price. "
            "Set your model price with `grid price set` after joining.",
            file=sys.stderr,
        )
    if args.at and args.serve:
        raise SystemExit("Use either --at (point at an existing engine) or --serve, not both.")

    session = credentials.require_session()
    rec = remote_grid._select(getattr(args, "grid", None))
    network_id = remote_grid._network_id(rec)
    label = rec.get("name") or network_id
    if not rec.get("access_token"):
        raise SystemExit(
            f"Grid {label} has no access token locally. Run `grid login` to refresh your grids."
        )

    # Resolve the relay address (works for a member, not just the creator; a stopped grid that a
    # member can't pre-check fails later at register). See remote_grid.resolve_relay_base.
    signaling_url, _status = remote_grid.resolve_relay_base(session, rec, network_id, label)

    specs, media_detected = _resolve_serve_targets(args)
    media = bool(getattr(args, "media", False)) or media_detected
    if not specs and not media:  # engines detected and the operator declined, or nothing to serve
        print("Nothing joined.")
        return 0
    if len(specs) > 1 and (getattr(args, "advertise_as", []) or []):
        raise SystemExit("--advertise-as serves a single engine; it can't alias the union of --all.")
    _warn_shadowed_models(specs)  # the detached loop logs this too, but show it on the operator's terminal

    engine_id = getattr(args, "name", None) or f"engine-{uuid.uuid4().hex[:8]}"
    existing = run_records.read_record(network_id, engine_id)
    if existing and run_records.pid_alive(int(existing.get("pid") or 0)):
        raise SystemExit(f"Engine {engine_id!r} is already joined to {label}. Use a different --name.")

    record = _build_record(args, network_id, engine_id, signaling_url, specs, media=media)
    run_records.write_record(network_id, engine_id, record)

    proc = _spawn_remote_engine(network_id, engine_id)
    record["pid"] = proc.pid
    run_records.write_record(network_id, engine_id, record)

    log_path = paths.engines_dir(network_id) / f"{engine_id}.log"
    if _await_remote_engine_start(proc) == "died":
        run_records.record_path(network_id, engine_id).unlink(missing_ok=True)
        from . import provider

        raise SystemExit(
            f"Engine {engine_id} exited before it started. See {log_path}:\n{provider._log_tail(log_path)}"
        )

    print(f"Joining engine {engine_id} to {label} (pid={proc.pid}) — serving via the relay.")
    if len(specs) > 1:
        print(f"engines={len(specs)} (serving the union under one identity)")
    elif record["endpoint_url"]:
        print(f"endpoint_url={record['endpoint_url']}")
    if record["models"]:
        print(f"models={','.join(record['models'])}")
    if media:  # the comfyui:* models are resolved from bundle gating at serve time, not here
        print("media=on (serving comfyui:* workflows via the relay)")
    print(f"log={log_path}")
    # The relay isn't locally pollable, so we can't confirm "registered" here — report starting.
    print(f"(starting — stop with `grid leave {label} --engine {engine_id}`)")
    return 0


def _resolve_serve_targets(args: argparse.Namespace) -> tuple[list[dict[str, object]], bool]:
    """What to serve: `(text_engine_specs, media_detected)`.

    Text specs are `{endpoint_url, models, engine_label}`. `media_detected` is True when auto-detect
    finds a media (ComfyUI) engine, so the caller brings the media engine up alongside the text ones.
    External `--at` and built-in `--serve` each resolve to one text spec (media comes only from an
    explicit `--media`). An explicit `--media` with no text engine is media-only: return `([], False)`
    and let `args.media` carry it. A bare `grid join` auto-detects: text engines join under one
    identity (DECISIONS D9) — `--all` (or an interactive confirm) accepts several, otherwise it asks —
    and any detected media engine flips `media_detected`. Returns `([], False)` when the operator
    declines the "join all" prompt. Mirrors local `cli/provider.cmd_join` (remote → ONE identity).
    """
    from . import provider

    if args.at:
        if not args.models:
            raise SystemExit("--at requires at least one -m/--model naming what that engine serves.")
        return [{"endpoint_url": args.at, "models": list(args.models), "engine_label": None}], False
    if args.serve:
        return [{"endpoint_url": None, "models": [args.serve], "engine_label": None}], False
    if args.models:
        raise SystemExit("-m/--model names models for an engine; pair it with --at <url>, or use --serve <model>.")
    if getattr(args, "media", False):
        # Explicit `--media` with no text engine → media-only. Skip detection; the serve loop brings up
        # the media engine from the bundle gating.
        return [], False

    detected = provider._detect(None)  # advertise_host is local-only; remote always probes loopback
    if not detected:
        raise SystemExit(
            "No running engine detected on this box. Point at one with "
            "`grid join --at <url> -m <model>`, or start the built-in engine with `grid join --serve <model>`."
        )
    if args.engine:
        detected = [engine for engine in detected if engine.label == args.engine]
        if not detected:
            raise SystemExit(f"No detected engine named {args.engine!r}. Run `grid join` to list them.")

    media_detected = any(engine.media for engine in detected)
    text = [engine for engine in detected if not engine.media]
    # Gate on ALL detected engines (incl. a media/ComfyUI one) and show them in the plan, so a
    # detected media engine is never silently joined without confirmation, nor silently dropped on
    # decline (mirrors local `cli/provider.cmd_join`, which counts + prints the full detected list).
    if len(detected) > 1 and not args.all:
        provider._print_plan(detected)
        if provider._interactive():
            if not provider._confirm("Join all detected engines?"):
                return [], False
        else:
            raise SystemExit("Multiple engines detected; pass --all, --engine <kind>, or --at <url>.")
    return [
        {"endpoint_url": engine.endpoint_url, "models": list(engine.models), "engine_label": engine.label}
        for engine in text
    ], media_detected


def _warn_shadowed_models(specs: list[dict[str, object]]) -> None:
    """Warn when two engines advertise the same model — the first detected wins (ADR 0007 / D9)."""
    owner: dict[str, str] = {}
    for spec in specs:
        label = str(spec.get("engine_label") or spec.get("endpoint_url") or "an engine")
        for model in spec["models"]:
            if model in owner:
                print(
                    f"Note: model {model!r} is served by more than one engine; routing it to "
                    f"{owner[model]!r} (first detected wins).",
                    file=sys.stderr,
                )
            else:
                owner[model] = label


def _build_record(
    args: argparse.Namespace,
    network_id: str,
    engine_id: str,
    signaling_url: str,
    specs: list[dict[str, object]],
    media: bool = False,
) -> dict[str, object]:
    """The remote engine's run record — non-secret routing only; the token stays in credentials.toml.

    Several engines can serve under one identity (DECISIONS D9): `engines` carries each local engine
    so the serve loop can build the model→engine table. Top-level `models` is their union and
    `endpoint_url` is the single engine's URL (None when several) — kept for display + back-compat.
    `media` (+ bundles/ports) mirror the local record fields so the serve loop brings up ComfyUI + the
    media server; a media-only join has empty `specs`/`models` and derives `comfyui:*` at serve time.
    """
    from local import runtime

    union = list(dict.fromkeys(model for spec in specs for model in spec["models"]))
    single_endpoint = specs[0]["endpoint_url"] if len(specs) == 1 else None

    return {
        "engine_id": engine_id,
        "node_id": f"node-{uuid.uuid4().hex[:12]}",
        "grid_id": network_id,  # the remote network_id doubles as the run record's grid_id
        "pid": 0,
        "signaling_url": signaling_url,
        "endpoint_url": single_endpoint,
        "models": union,
        "engines": specs,
        "media": bool(media),
        "media_bundles": list(getattr(args, "bundles", []) or []),
        "comfyui_port": getattr(args, "comfyui_port", 8188),
        "media_port": getattr(args, "media_port", 8190),
        "advertise_as": list(getattr(args, "advertise_as", []) or []),
        "engine_label": getattr(args, "engine_label", None),
        "pricing_input": getattr(args, "pricing_input", None),
        "pricing_output": getattr(args, "pricing_output", None),
        "max_concurrency": getattr(args, "max_concurrency", None),
        "endpoint_port": getattr(args, "endpoint_port", 8081),
        "ctx_size": getattr(args, "ctx_size", None),
        "n_predict": getattr(args, "n_predict", None),
        "parallel": getattr(args, "parallel", None),
        "flash_attn": getattr(args, "flash_attn", None),
        "temp": getattr(args, "temp", None),
        "reasoning_budget": getattr(args, "reasoning_budget", None),
        "started_at": runtime.utc_now(),
    }


def _spawn_remote_engine(network_id: str, engine_id: str) -> subprocess.Popen:
    from local import runtime

    log_path = paths.engines_dir(network_id) / f"{engine_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("ab")
    return subprocess.Popen(
        runtime.cli_command() + ["__remote-engine", network_id, engine_id],
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )


def _await_remote_engine_start(proc: subprocess.Popen, grace: float = 3.0) -> str:
    """Block briefly to tell a freshly-spawned remote engine "died" from "starting".

    Unlike local there is no local registry to poll (the relay isn't locally reachable), so this
    only checks the process stayed alive — registration shows up on the grid page, not here.
    """
    deadline = time.time() + grace
    while time.time() < deadline:
        if proc.poll() is not None:
            return "died"
        time.sleep(0.2)
    return "starting" if proc.poll() is None else "died"


def cmd_remote_leave(args: argparse.Namespace) -> int:
    from remote import credentials

    from . import remote_grid

    credentials.require_session()
    rec = remote_grid._select(getattr(args, "grid", None))
    network_id = remote_grid._network_id(rec)
    label = rec.get("name") or network_id
    records = run_records.read_records(network_id)

    if args.all:
        targets = list(records)
    elif args.engine:
        if args.engine not in records:
            raise SystemExit(f"No engine {args.engine!r} joined to {label}.")
        targets = [args.engine]
    elif len(records) == 1:
        targets = list(records)
    elif not records:
        print(f"No engines joined to {label}.")
        return 0
    else:
        names = ", ".join(sorted(records))
        raise SystemExit(f"Several engines joined ({names}); pass --engine <id> or --all.")

    from . import provider  # shared teardown: stops the engine + reaps a media engine's ComfyUI

    for engine_id in targets:
        provider._stop_engine(network_id, engine_id, records[engine_id])
        print(f"Left engine {engine_id} on {label}.")
    return 0
