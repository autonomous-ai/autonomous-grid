"""`grid join` / `grid leave` / `grid models`: the engine lifecycle.

`grid join` registers an engine into a grid and keeps heartbeating it. It runs
the heartbeat loop in a *detached* process (the internal ``__engine`` entry)
and records the engine under ``~/.grid/run/engines/<grid>/`` so a later
`grid leave` can stop and unregister it. `grid models` lists the live models the
grid can serve right now.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
import uuid
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

import httpx

from local import config
from shared import logging_setup, paths, run_records
from shared.handlers import HANDLERS
from shared.models import api_catalog
from local import runtime


# ---------------------------------------------------------------------------
# grid join
# ---------------------------------------------------------------------------

# Remote-only `grid join` flags (DECISIONS D6/D8): rejected in local mode, where the concept
# doesn't exist. (attr on args, surface flag) — kept here next to the local handler that guards them.
_REMOTE_ONLY_JOIN_FLAGS = (
    ("engine_label", "--engine-label"),
    ("pricing_input", "--pricing-input"),
    ("pricing_output", "--pricing-output"),
    ("respawn", "--respawn"),
    ("relay_at", "--relay-at"),
    # Task serving (ADR 0032, issue 61). A task is claimed from the relay, and local mode has no
    # relay. ⚠️ All three default to `None`, `--tasks` included: the predicate below is `is not
    # None`, so a `store_true` flag
    # defaulting to False would refuse every LOCAL join.
    ("tasks", "--tasks"),
    ("max_tasks", "--max-tasks"),
    ("tasks_root", "--tasks-root"),
)


def _reject_remote_only_flags(args: argparse.Namespace) -> None:
    used = [flag for attr, flag in _REMOTE_ONLY_JOIN_FLAGS if getattr(args, attr, None) is not None]
    if used:
        raise SystemExit(
            f"{', '.join(used)} only applies in remote mode. "
            "Switch with `grid mode remote` (or pass --remote)."
        )
    # `--api` is no longer remote-only wholesale, but it is still remote-only for TEXT kinds served
    # over the network: such an engine is driven by the relay's poll loop, which local mode has no
    # equivalent of. Two kinds are local-capable for the same structural reason — they are served by
    # a process on THIS box behind a loopback URL, which is exactly the shape the local proxy already
    # forwards to. A MEDIA kind slots in where ComfyUI does (`local/api_media_server.py`); a
    # CLI seat slots in where llama-server does (`local/cli_seat_server.py`).
    kind = getattr(args, "api", None)
    local_kinds = set(api_catalog.local_seat_kinds()) | set(HANDLERS)
    if kind is not None and kind not in local_kinds:
        raise SystemExit(
            f"`--api {kind}` only applies in remote mode. Switch with `grid mode remote` "
            f"(or pass --remote). Locally, --api supports: {', '.join(sorted(local_kinds))}."
        )


def _apply_inline_aliases(args: argparse.Namespace) -> None:
    """Desugar inline ``-m real=alias`` into the flat ``models`` / ``advertise_as`` lists both join
    handlers already consume. Same contract as ``--advertise-as``: all-or-nothing across the -m/--model
    values, and mutually exclusive with ``--advertise-as``. A no-op when no ``-m`` contains ``=``. Runs
    before the record is built / ``args.models`` is read. Splits on the first ``=`` only — model names
    can't contain ``=`` (ollama's ``:`` and media's ``comfyui:*`` are untouched)."""
    serve = getattr(args, "serve", None)
    if serve and "=" in serve:
        raise SystemExit("`--serve` takes a bare model; alias it with `--advertise-as`, not `=`.")
    models = list(getattr(args, "models", []) or [])
    inline = [item for item in models if "=" in item]
    if not inline:
        return
    if getattr(args, "advertise_as", None):
        raise SystemExit("Use inline `-m real=alias` or `--advertise-as`, not both.")
    if len(inline) != len(models):
        raise SystemExit("Alias every -m/--model as `real=alias`, or none of them.")
    reals: list[str] = []
    aliases: list[str] = []
    for item in models:
        if item.count("=") != 1:
            raise SystemExit(f"Inline alias {item!r} must be exactly one `real=alias` pair.")
        real, _, alias = item.partition("=")
        real, alias = real.strip(), alias.strip()
        if not real or not alias:
            raise SystemExit(f"Inline alias {item!r} needs a non-empty model and alias (real=alias).")
        reals.append(real)
        aliases.append(alias)
    args.models = reals
    args.advertise_as = aliases


def cmd_join(args: argparse.Namespace) -> int:
    _reject_remote_only_flags(args)
    gpu_memory_mb = list(getattr(args, "gpu_memory_mb", []) or [])
    gpu_count = getattr(args, "gpu_count", None)
    if gpu_count is None and gpu_memory_mb:
        gpu_count = len(gpu_memory_mb)
    if gpu_count and len(gpu_memory_mb) == 1:
        gpu_memory_mb *= gpu_count
    if gpu_memory_mb and len(gpu_memory_mb) != gpu_count:
        raise SystemExit(
            "Repeat --gpu-memory-mb once per heterogeneous GPU, or provide one value "
            "for a homogeneous --gpu-count."
        )
    args.gpu_count = gpu_count
    args.gpu_memory_mb = gpu_memory_mb
    if args.serve and args.models:
        raise SystemExit("--serve serves one built-in model; drop -m/--model (alias a built-in with --advertise-as).")
    _apply_inline_aliases(args)
    advertise_host = getattr(args, "advertise_host", None)
    cfg = config.select_grid(getattr(args, "grid", None))
    grid_id = cfg["grid_id"]

    if args.at and args.serve:
        raise SystemExit("Use either --at (point at an existing engine) or --serve, not both.")

    # An API media engine is resolved before the --at/--serve branches: `--at` names the VENDOR
    # gateway here (not a local OpenAI-compatible engine), so it must not fall through to the
    # generic text-engine path below.
    if api_catalog.local_seat_port(getattr(args, "api", None) or "") is not None:
        return _spawn_cli_seat_engine(cfg, args)

    if getattr(args, "api", None):
        return _spawn_api_media_engine(cfg, args)

    if args.at:
        if not args.models:
            raise SystemExit("--at requires at least one -m/--model naming what that engine serves.")
        return _spawn_engine(
            cfg,
            args,
            endpoint_url=args.at,
            models=list(args.models),
            media=args.media,
            runtime_kind=args.kind,
        )

    if args.serve:
        return _spawn_engine(
            cfg,
            args,
            endpoint_url=None,
            models=[args.serve],
            media=args.media,
            runtime_kind="llama.cpp",
        )

    if args.media and not args.models:
        return _spawn_engine(
            cfg,
            args,
            endpoint_url=None,
            models=[],
            media=True,
            runtime_kind="comfyui",
        )

    if args.models:
        raise SystemExit(
            "-m/--model names what an engine serves, so it needs to know which engine.\n"
            "  Point at one you already run:  grid join --at http://localhost:11434/v1 -m llama3\n"
            "  Or start the built-in one:     grid join --serve my-model.gguf"
        )

    # No engine spec: detect what is already running on this box.
    detected = _detect(advertise_host)
    if not detected:
        raise SystemExit(
            "Nothing on this computer is running a model yet.\n"
            "  Install the built-in engine:   grid engine install llama.cpp\n"
            "  then download a model:         grid catalog\n"
            "  then serve it:                 grid join --serve <the file grid pull saved>\n"
            "  Already run Ollama or vLLM?    grid join --at http://localhost:11434/v1 -m llama3"
        )
    if args.kind:
        detected = [engine for engine in detected if engine.label == args.kind]
        if not detected:
            raise SystemExit(f"No detected engine of kind {args.kind!r}. Run `grid join` to list them.")
    elif len(detected) > 1 and not args.all:
        _print_plan(detected)
        if _interactive():
            if not _confirm("Join all detected engines?"):
                print("Nothing joined.")
                return 0
        else:
            # The plan above already printed both commands with real values in them; repeating
            # them here just made the reader read the same two lines twice.
            raise SystemExit("More than one engine is running here, so pick one of the two above.")

    used: set[str] = set()
    rc = 0
    for engine in detected:
        engine_id = _unique_engine_id(grid_id, engine.label, used)
        used.add(engine_id)
        try:
            _spawn_engine(
                cfg,
                args,
                endpoint_url=None if engine.media else engine.endpoint_url,
                models=engine.models,
                engine_id=engine_id,
                media=engine.media,
                runtime_kind=engine.label,
            )
        except SystemExit as exc:
            print(f"Skipped {engine.label}: {exc}", file=sys.stderr)
            rc = 1
    return rc


def _seat_options(args: argparse.Namespace):
    """The seat's options from the kind-agnostic `--seat-*` flags, defaulted per kind."""
    from shared.agent import cli_seat

    kind = getattr(args, "api", None) or ""
    return cli_seat.options_from_args(args, default_port=api_catalog.local_seat_port(kind))


def _narrow_advertised(kind: str, advertised: list[str], requested: list[str]) -> list[str]:
    """Restrict a kind's advertised models to the ones `-m` named, or all of them when `-m` is absent."""
    if not requested:
        return advertised
    unknown = [model for model in requested if model not in advertised]
    if unknown:
        raise SystemExit(
            f"Not {kind} models: {', '.join(unknown)}. Available: {', '.join(advertised)}"
        )
    return requested


def _spawn_cli_seat_engine(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    """`grid join --api <seat kind>` in local mode: serve a coding CLI installed on this box.

    Binary and sign-in are checked in the foreground so a missing or signed-out CLI is a one-line
    error at the prompt, not a registered engine that fails every request into a log. No credential
    is resolved or stored — the CLI authenticates itself.
    """
    from shared.agent import cli_seat
    from shared.agent.seats import seat_for

    kind = args.api
    spec = seat_for(kind)
    try:
        cli_seat.assert_available(spec)
    except cli_seat.SeatError as exc:
        raise SystemExit(str(exc))

    advertised = _narrow_advertised(
        kind, cli_seat.advertised_models(kind), list(getattr(args, "models", None) or [])
    )
    print(
        "Warning: the local grid is unauthenticated and LAN-reachable, so anyone who can reach "
        f"{runtime.grid_url(cfg)} can spend this {spec.label} subscription.",
        file=sys.stderr,
    )
    return _spawn_engine(
        cfg, args, endpoint_url=None, models=advertised, media=False, api_kind=kind,
    )


def _run_cli_seat_engine(args: SimpleNamespace, cfg: dict[str, Any], grid_url: str, node_id: str) -> int:
    """The detached loop for `grid join --api <seat kind>` (local mode).

    Brings the seat's loopback server up and advertises it as an ordinary TEXT engine — the grid
    proxy forwards `chat/completions` to `endpoint_url` and cannot tell it from llama-server.
    """
    from local.cli_seat_runtime import start_seat_server, stop_seat_server
    from shared.agent import cli_seat
    from shared.agent.seats import seat_for

    kind = args.api_kind
    spec = seat_for(kind)
    options = cli_seat.SeatOptions(**(getattr(args, "seat", None) or {}))
    proc = start_seat_server(
        kind=kind, options=options, binary=cli_seat.assert_available(spec)
    )
    endpoint_url = f"http://127.0.0.1:{options.port}"
    print(f"Spawned {spec.label} seat pid={proc.pid}, url={endpoint_url}")

    models = list(args.models)
    payload = {
        "role": "engine",
        "models": models,
        # Loopback on purpose: the grid proxy runs on this box and is the only thing that should
        # reach the process that can spend the subscription.
        "endpoint_url": endpoint_url,
        "media_url": None,
        "name": args.name,
        "pricing": {},
        "capabilities": {},
        "load": {
            "active_tasks": 0,
            "max_concurrency": max(1, int(getattr(args, "max_concurrency", None) or 1)),
        },
        "upstream": {model: model for model in models},
    }
    return _advertise_until_terminated(
        args, grid_url, node_id, payload,
        stop=lambda: stop_seat_server(proc),
        stopped_msg=f"Stopped {spec.label} seat.",
    )


def _advertise_until_terminated(
    args: SimpleNamespace, grid_url: str, node_id: str, payload: dict[str, Any],
    *, stop, stopped_msg: str,
) -> int:
    """Register `payload`, heartbeat until SIGTERM, then unregister and stop the child.

    Shared by every engine whose backend is a loopback child (media bridge, CLI seat): the
    register/heartbeat/unregister dance is identical, and a second copy is a second place to forget
    the ghost-record cleanup.
    """
    registered = False
    try:
        _register_engine(grid_url, node_id, payload)
        registered = True
        print(f"Engine {node_id} advertised on {grid_url}")
        if payload.get("models"):
            print(f"models={','.join(payload['models'])}")
        print("Send SIGTERM (grid leave) to unregister.")
        while True:
            time.sleep(max(1.0, float(args.heartbeat_interval)))
            try:
                _heartbeat(
                    grid_url,
                    node_id,
                    dict(payload.get("load") or {"active_tasks": 0}),
                    payload,
                )
            except httpx.RequestError as exc:
                print(f"Heartbeat failed: {exc}", file=sys.stderr)
    except KeyboardInterrupt:
        print("\nEngine unregistered.")
        return 0
    finally:
        if registered:
            try:
                httpx.delete(f"{grid_url}/nodes/{node_id}", timeout=5)
            except Exception as exc:
                print(f"Unregister failed (ignoring): {exc}", file=sys.stderr)
        stop()
        print(stopped_msg)
        if not registered:
            # An engine that died before registering must not leave a ghost record for
            # `grid leave --all`, nor unlink one a newer live child owns (issue 05's audit).
            # `discard_own_record`, never a bare unlink: the blind version deleted the record a
            # NEWER live child had just written, stranding it untracked — exactly the orphan the
            # ownership check exists to prevent.
            run_records.discard_own_record(args.grid, args.name)


def _spawn_api_media_engine(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    """`grid join --api <media kind>` in local mode: serve a vendor media gateway to this grid.

    Resolves the gateway URL and key here, in the foreground, so a bad value is a clean error at the
    prompt rather than a detached process that dies into a log. The key is NOT written to the run
    record (records are plain JSON under ~/.grid/run); it is handed to the detached engine through
    the environment, which is also how that engine passes it to the bridge.
    """
    from shared.models import api_catalog

    kind = args.api
    whitelist = api_catalog.WHITELISTS.get(kind)
    if whitelist is None:
        raise SystemExit(f"Unknown API kind {kind!r}.")

    base_url = (getattr(args, "at", None) or whitelist.base_url or "").rstrip("/")
    if not base_url:
        raise SystemExit(
            f"--api {kind} needs the gateway URL. Pass --at <url> "
            f"(e.g. --at https://your-{kind}-endpoint)."
        )

    key = _resolve_api_media_key(kind, whitelist, args)

    # With no -m, serve the whole whitelist for this kind — same default as the remote join.
    advertised = _narrow_advertised(
        kind,
        [api_catalog.advertised_name(kind, entry) for entry in api_catalog.entries_for(kind)],
        list(getattr(args, "models", None) or []),
    )

    print(
        f"Warning: the local grid is unauthenticated and LAN-reachable, so anyone who can reach "
        f"{runtime.grid_url(cfg)} can spend this {kind} key. Use remote mode for an authenticated grid.",
        file=sys.stderr,
    )
    return _spawn_engine(
        cfg, args, endpoint_url=None, models=advertised, media=False,
        api_kind=kind, api_base_url=base_url, api_key=key,
    )


def _resolve_api_media_key(kind: str, whitelist: Any, args: argparse.Namespace) -> str:
    """Key precedence for a local API media join: --api-key, else the env var, else a hidden prompt.

    Deliberately does NOT read or write the machine-local key store: that store lives under
    `remote/` and belongs to the signed-in remote flow, and `local/` must not depend on `remote/`
    (ARCHITECTURE.md layering). One less place a key is persisted is the right trade here.
    """
    import getpass

    env_var = whitelist.env_var
    flag_key = getattr(args, "api_key", None)
    if flag_key:
        print(
            "Warning: --api-key is visible in shell history."
            + (f" Consider exporting {env_var} instead." if env_var else ""),
            file=sys.stderr,
        )
    key = (flag_key or (os.environ.get(env_var) if env_var else None) or "").strip()
    if not key and _interactive():
        key = getpass.getpass(
            f"Enter your {kind} API key (input hidden"
            + (f"; or export {env_var}" if env_var else "")
            + "): "
        ).strip()
    if not key:
        hint = f"export {env_var}=..., " if env_var else ""
        raise SystemExit(
            f"--api {kind} needs an API key. Pass --api-key <key>, {hint}"
            "or run interactively to be prompted."
        )
    return key


def _spawn_engine(
    cfg: dict[str, Any],
    args: argparse.Namespace,
    *,
    endpoint_url: str | None,
    models: list[str],
    engine_id: str | None = None,
    media: bool = False,
    api_kind: str | None = None,
    api_base_url: str | None = None,
    api_key: str | None = None,
    runtime_kind: str | None = None,
) -> int:
    grid_id = cfg["grid_id"]
    engine_id = engine_id or getattr(args, "name", None) or f"engine-{uuid.uuid4().hex[:8]}"
    if _record_path(grid_id, engine_id).exists() and _record_alive(grid_id, engine_id):
        raise SystemExit(f"Engine {engine_id!r} is already joined to {cfg['name']}. Use a different --name.")

    record = {
        "engine_id": engine_id,
        "node_id": f"node-{uuid.uuid4().hex[:12]}",
        "grid_id": grid_id,
        "pid": 0,
        "endpoint_url": endpoint_url,
        "models": models,
        # Serving implementation is independent from ownership. In particular, an external vLLM
        # process remains manually managed even though its runtime is now available to placement.
        "runtime_kind": runtime_kind,
        "max_concurrency": getattr(args, "max_concurrency", None),
        "gpu_count": getattr(args, "gpu_count", None),
        "gpu_memory_mb": list(getattr(args, "gpu_memory_mb", []) or []),
        "advertise_as": list(getattr(args, "advertise_as", []) or []),
        "media": bool(media),
        "media_bundles": list(getattr(args, "bundles", []) or []),
        "endpoint_port": getattr(args, "endpoint_port", 8081),
        "advertise_host": getattr(args, "advertise_host", None),
        "comfyui_port": getattr(args, "comfyui_port", 8188),
        "media_port": getattr(args, "media_port", 8190),
        "heartbeat_interval": getattr(args, "heartbeat_interval", 15.0),
        "ctx_size": getattr(args, "ctx_size", None),
        "n_predict": getattr(args, "n_predict", None),
        "parallel": getattr(args, "parallel", None),
        "flash_attn": getattr(args, "flash_attn", None),
        "mmproj": getattr(args, "mmproj", None),
        "temp": getattr(args, "temp", None),
        "reasoning_budget": getattr(args, "reasoning_budget", None),
        # API media engine (`--api <kind>`): the vendor gateway this engine bridges to. The KEY is
        # deliberately absent — the record is plain JSON on disk; the key travels in the child's
        # environment instead (see below).
        "api_kind": api_kind,
        "api_base_url": api_base_url,
        "api_media_port": getattr(args, "media_port", 8190),
        # CLI seat (`--api <seat kind>`): one options object, not six loose keys.
        "seat": _seat_options(args).to_dict() if api_catalog.local_seat_port(api_kind or "") else None,
        "started_at": runtime.utc_now(),
    }
    _write_record(grid_id, engine_id, record)

    log_path = paths.engines_dir(grid_id) / f"{engine_id}.log"
    log = logging_setup.cap_and_open_append(log_path, logging_setup.engine_log_max_bytes())
    child_env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    if api_key:
        child_env["GRID_API_MEDIA_KEY"] = api_key
    proc = subprocess.Popen(
        runtime.cli_command() + ["__engine", grid_id, engine_id],
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=child_env,
    )
    # The identity, not just the pid (grid-leave issue 08): a local engine child is torn down by the
    # SAME `run_records` terminator as a remote one, so without the token its record can never be
    # proven to name our child rather than a recycled pid, and its process group can never back up a
    # kill the recorded pid missed. Local has no self-stamp, so this is the only writer.
    record.update(run_records.identity_stamp(proc.pid))
    _write_record(grid_id, engine_id, record)

    status = _await_engine_start(runtime.grid_url(cfg), record["node_id"], proc)
    if status == "died":
        _record_path(grid_id, engine_id).unlink(missing_ok=True)
        raise SystemExit(
            f"Engine {engine_id} exited before it registered. See {log_path}:\n{_log_tail(log_path)}"
        )

    _report_join(cfg, args, engine_id, models, endpoint_url, log_path, status)
    return 0


def serves_vision(args) -> bool:
    """Whether the model this join just started can read images.

    Only ever true for a built-in `--serve` engine: an `--at` join points at somebody else's
    server, whose modalities are its own to advertise, not ours to guess from a file we do not
    have. Read through the same `projector_beside` the launcher uses to decide `--mmproj`, so the
    hint and the engine can never disagree about whether vision is on.
    """
    serve = getattr(args, "serve", None)
    if not serve:
        return False
    if getattr(args, "mmproj", None):
        return True  # explicitly pointed at a projector, so vision is on by instruction
    from shared.models import gguf

    return gguf.projector_beside(paths.models_dir() / serve) is not None


def chat_hints(model: str, vision: bool) -> list[str]:
    """The `grid chat` line(s) to suggest for [model] — two when it can see, one when it cannot.

    Shared by both modes' join reports so a vision model is offered `--image` identically whether
    it joined a local grid or a remote one.
    """
    lines = [f'  grid chat -m {model} "hello"']
    if vision:
        # Only for a model that actually carries a projector: offering `--image` on a text-only
        # model would advertise something the engine will not answer.
        # A placeholder, not a plausible filename: `photo.jpg` reads as something to paste, and
        # pasting it fails on a file nobody has. Quoted so a path with a space survives the shell.
        lines.append(f'  grid chat -m {model} --image "<image-path>" "what is in this image?"')
    return lines


def _report_join(cfg, args, engine_id, models, endpoint_url, log_path, status) -> None:
    """Say whether the model is actually being served, in the words someone new would use.

    This used to print "Joined engine ... (pid=…)" and leave it there. "Joined" only ever meant
    "the process was launched" — the model could still be loading, or about to fail — and the four
    lines that say what really happened (model loaded, vision on, registered with the grid) go to a
    log file nobody thinks to open. So the reader was told a pid and a path and left to work out
    for themselves whether it had worked.

    The advertised name is what gets printed, because that is the name they will type next. The
    old line printed the FILENAME here while the log printed the advertised name — the same label
    over two different values, and the one on screen was the one you cannot use.
    """
    advertised = _advertised_text_models(list(models or []), list(getattr(args, "advertise_as", []) or []))
    served = advertised or list(models or [])

    names = ", ".join(f"'{name}'" for name in served) or f"'{engine_id}'"
    if status == "starting":
        print(f"\n… Loading {names} — large models take a while.")
        # Quoted: a grid name is freeform and can carry a space ("Hydrate Grid"), and this hint
        # printed bare isn't actually copy-pasteable — argparse splits it into two positionals and
        # rejects the second (grid-leave issue: exactly this line is what a reader hit).
        print(f"\nNext:  grid models {shlex.quote(cfg['name'])}")
        return

    print(f"\n✓ Serving {names} on grid '{cfg['name']}'")
    if served:
        print("\nNext:")
        for line in chat_hints(served[0], serves_vision(args)):
            print(line)


def _await_engine_start(grid_url: str, node_id: str, proc, grace: float = 3.0) -> str:
    """Block briefly to tell whether a freshly-spawned engine actually came up.

    Returns "registered" once the grid sees it, "died" if the process exited,
    or "starting" if it is still alive but not yet registered (e.g. a `--serve`
    engine still loading its model). Uses ``proc.poll()`` rather than a bare
    pid signal so an exited-but-unreaped child (a zombie) is detected.
    """
    deadline = time.time() + grace
    while time.time() < deadline:
        if proc.poll() is not None:
            return "died"
        if _is_registered(grid_url, node_id):
            return "registered"
        time.sleep(0.2)
    return "starting" if proc.poll() is None else "died"


def _is_registered(grid_url: str, node_id: str) -> bool:
    try:
        resp = httpx.get(f"{grid_url}/nodes/discover", timeout=2)
        resp.raise_for_status()
    except httpx.HTTPError:
        return False
    return any(p.get("node_id") == node_id for p in resp.json().get("engines", []))


def _log_tail(path, lines: int = 12) -> str:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return ""
    return "\n".join(text.strip().splitlines()[-lines:])


# ---------------------------------------------------------------------------
# grid leave
# ---------------------------------------------------------------------------

def cmd_leave(args: argparse.Namespace) -> int:
    """Resolve which engines this leave means, then hand them to the teardown (`cli/local_leave`).

    Two shapes, and the difference is not cosmetic — it decides how widely the argv sweep may reap.
    `--engine <x>` names ONE of this grid's children (local runs one per engine), so the sweep pins
    its engine id too. Everything else — `--all`, a bare leave, and a bare leave with no records at
    all — means the whole grid, so the sweep pins only the grid id and can therefore also reap a
    record-less orphan, whose engine id nobody knows. A bare leave with no records is a first-class
    **repair** verb, not the dead end it used to be (grid-leave issue 17).
    """
    from . import local_leave

    cfg = config.select_grid(getattr(args, "grid", None))
    grid_id = cfg["grid_id"]
    records = _read_records(grid_id)

    if args.engine and not args.all:
        engine_id = _resolve_leave_target(records, args.engine, cfg["name"])
        return local_leave.tear_down(
            grid_id, cfg["name"], {engine_id: records[engine_id]}, engine_id=engine_id
        )
    if not args.all and len(records) > 1:
        names = ", ".join(sorted(records))
        raise SystemExit(f"Several engines joined ({names}); pass --engine <id> or --all.")
    return local_leave.tear_down(grid_id, cfg["name"], records)


def _resolve_leave_target(records: dict[str, dict[str, Any]], selector: str, grid_name: str) -> str:
    """The engine id to leave for ``--engine <selector>``: an exact engine id wins; otherwise match by
    endpoint URL, a served model, or a URL fragment — the same order as remote (ADR 0011 D-d)."""
    if selector in records:
        return selector
    specs = [
        {"id": engine_id, "endpoint_url": record.get("endpoint_url"),
         "models": record.get("models") or [], "engine_label": None}  # local records carry no label
        for engine_id, record in records.items()
    ]
    matched = run_records.match_engine(
        specs, selector, label=grid_name, summary=_leave_summary(records),
        hint="pass the exact engine id instead",
    )
    if not matched:
        raise SystemExit(
            f"No engine {selector!r} joined to {grid_name} (match by id, endpoint URL, a served "
            f"model, or a URL fragment). Engines: {_leave_summary(records)}."
        )
    return str(matched[0]["id"])


def _leave_summary(records: dict[str, dict[str, Any]]) -> str:
    """A short human list of joined engines for a leave error / ambiguity message."""
    parts = []
    for engine_id, record in records.items():
        models = ",".join(record.get("models") or [])
        parts.append(f"{engine_id} [{models}]" if models else engine_id)
    return "; ".join(parts)


def _stop_engine(grid_id: str, engine_id: str, record: dict[str, Any]) -> run_records.Teardown:
    """Stop one engine child and reap only a ComfyUI it itself started. Returns the honest teardown
    outcome — ``survivor`` (0 when confirmed gone) so the caller can keep the record + fail loudly
    instead of printing "Left …" over a live process, and ``verified`` so a leave that could prove
    nothing does not read as one that proved everything."""
    outcome = run_records.stop_engine(grid_id, engine_id, record)
    # Reap ONLY a ComfyUI this engine itself started (`comfyui_started` persisted at bring-up) — never one
    # shared with another media engine or started by the operator — and target its specific port so a
    # co-resident engine's ComfyUI is untouched. Covers the case where the engine's own teardown was
    # SIGKILLed mid-stop or the media server restarted ComfyUI in a separate session.
    if record.get("media") and record.get("comfyui_started"):
        from shared.engine import comfyui

        port = record.get("comfyui_port", comfyui.COMFYUI_PORT_DEFAULT)
        try:
            comfyui.stop_running(port)
        except OSError as exc:  # best-effort: one media engine's reap must not abort a `leave --all`
            print(f"Reaping ComfyUI on :{port} failed (ignoring): {exc}", file=sys.stderr)
    return outcome


# ---------------------------------------------------------------------------
# grid models
# ---------------------------------------------------------------------------

def print_models_hint(model: str) -> None:
    """Suggest the chat command for [model] — to **stderr**, and only on a real terminal.

    `grid models` is a data command: its stdout is a list somebody pipes into a loop, and a hint
    printed there arrives as a phantom model name (measured: `grid models | while read m` handed a
    script the blank line and `Next:  grid chat -m ... "hello"` as two extra "models"). stderr
    keeps the list clean while a person still sees the hint; the tty check keeps it out of a
    script's error log too, where it would be noise nobody reads.
    """
    if not sys.stdout.isatty():
        return
    print(f'\nNext:  grid chat -m {model} "hello"', file=sys.stderr)


def cmd_models(args: argparse.Namespace) -> int:
    cfg = config.select_grid(getattr(args, "grid", None))
    engines = _discover(cfg)
    rows = [
        (model, _engine_label(engine), engine.get("endpoint_url") or engine.get("media_url") or "")
        for engine in engines
        for model in engine.get("models") or []
    ]

    if getattr(args, "json", False):
        print(json.dumps(
            [{"model": model, "engine": label, "where": where} for model, label, where in rows],
            indent=2,
        ))
        return 0

    if not rows:
        print("(no live models — `grid join` an engine first)")
        return 0

    seen: list[str] = []
    for model, _, _ in rows:
        if model not in seen:
            seen.append(model)

    if args.verbose:
        width = max(len("MODEL"), *(len(model) for model, _, _ in rows))
        ewidth = max(len("ENGINE"), *(len(label) for _, label, _ in rows))
        print(f"{'MODEL':<{width}}  {'ENGINE':<{ewidth}}  WHERE")
        for model, label, where in rows:
            print(f"{model:<{width}}  {label:<{ewidth}}  {where}")
    else:
        for model in seen:
            print(model)

    # A model showing up here is exactly the moment `grid join`'s own "still loading" message
    # (`_report_join`) could not yet promise — it only ever pointed back to this command. Close
    # that loop here, with the real name that just appeared, not the one someone typed minutes ago.
    print_models_hint(seen[0])
    return 0


def cmd_engines(args: argparse.Namespace) -> int:
    cfg = config.select_grid(getattr(args, "grid", None))
    engines = _discover(cfg)

    if getattr(args, "json", False):
        print(json.dumps(
            [
                {
                    "engine": _engine_label(engine),
                    "where": engine.get("endpoint_url") or engine.get("media_url") or "",
                    "models": engine.get("models") or [],
                    "max_concurrency": engine.get("max_concurrency"),
                }
                for engine in engines
            ],
            indent=2,
        ))
        return 0

    if not engines:
        print("(no engines — `grid join` one first)")
        return 0

    labels = [_engine_label(engine) for engine in engines]
    ewidth = max(len("ENGINE"), *(len(label) for label in labels))
    print(f"{'ENGINE':<{ewidth}}  WHERE")
    for engine, label in zip(engines, labels):
        where = engine.get("endpoint_url") or engine.get("media_url") or ""
        models = ",".join(engine.get("models") or []) or "(none)"
        # max_concurrency is a remote-only advertised field — show it only when the engine reports it.
        concurrency = engine.get("max_concurrency")
        detail = f"models: {models}"
        if concurrency is not None:
            detail += f"   concurrency: {concurrency}"
        print(f"{label:<{ewidth}}  {where}")
        print(f"{'':<{ewidth}}  {detail}")
    return 0


def _discover(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    grid_url = runtime.grid_url(cfg)
    try:
        resp = httpx.get(f"{grid_url}/nodes/discover", timeout=10)
        resp.raise_for_status()
    except httpx.RequestError as exc:
        raise SystemExit(f"Could not reach grid {cfg['name']} at {grid_url}: {exc}") from exc
    except httpx.HTTPStatusError as exc:
        raise SystemExit(f"Discovery failed: {exc.response.status_code} {exc.response.text}") from exc
    return resp.json().get("engines", [])


def _engine_label(engine: dict[str, Any]) -> str:
    return engine.get("name") or engine.get("node_id", "?")


# ---------------------------------------------------------------------------
# detached provider loop (internal `__engine` entry)
# ---------------------------------------------------------------------------

def run_engine_from_record(grid_id: str, engine_id: str) -> int:
    record = _read_records(grid_id).get(engine_id)
    if not record:
        raise SystemExit(f"No engine record for {engine_id} on {grid_id}.")
    args = SimpleNamespace(
        grid=record["grid_id"],
        node_id=record["node_id"],
        name=engine_id,
        models=list(record.get("models") or []),
        advertise_as=list(record.get("advertise_as") or []),
        endpoint_url=record.get("endpoint_url"),
        endpoint_port=record.get("endpoint_port", 8081),
        advertise_host=record.get("advertise_host"),
        enable_media=bool(record.get("media")),
        media_bundles=list(record.get("media_bundles") or []),
        comfyui_port=record.get("comfyui_port", 8188),
        media_port=record.get("media_port", 8190),
        heartbeat_interval=record.get("heartbeat_interval", 15.0),
        ctx_size=record.get("ctx_size"),
        n_predict=record.get("n_predict"),
        parallel=record.get("parallel"),
        flash_attn=record.get("flash_attn"),
        mmproj=record.get("mmproj"),
        temp=record.get("temp"),
        reasoning_budget=record.get("reasoning_budget"),
        runtime_kind=record.get("runtime_kind"),
        max_concurrency=record.get("max_concurrency"),
        gpu_count=record.get("gpu_count"),
        gpu_memory_mb=list(record.get("gpu_memory_mb") or []),
        api_kind=record.get("api_kind"),
        api_base_url=record.get("api_base_url"),
        api_media_port=record.get("api_media_port", 8190),
        seat=record.get("seat"),
    )

    def _on_term(_signum, _frame):  # noqa: ANN001
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _on_term)
    return _run_engine(args)


def _fixed_up_endpoint(endpoint_url: str, args: SimpleNamespace, grid_url: str) -> str:
    """Fall back to a reachable address for a built-in engine's own endpoint, the same way
    `cli/grid.py::_resolve_address` already does for the grid's address — one hop further
    downstream, and much harder to notice.

    `detect_local_ip` can name an interface (typically a VPN) nothing can dial back — including
    the grid on this same box. When it names the *grid's* address, `grid start` prints a warning
    at the moment it happens. When it names an *engine's* address instead, the join reports
    success (llama-server really is listening — just not where it just told the grid to look),
    and the failure only surfaces later as `grid chat` answering "Server disconnected without
    sending a response", nowhere near the command that caused it.

    Checked only once llama-server is confirmed listening (`wait_for_models` returned), so the
    reachability test means something instead of probing a port nothing has bound yet.
    """
    if args.advertise_host is not None or runtime.advertised_address_works(endpoint_url):
        return endpoint_url  # an explicit choice, or already proven reachable — leave it alone

    # The grid itself reached over loopback means everything is one machine, and llama-server
    # binds 0.0.0.0, so loopback always reaches it too.
    if urlparse(grid_url).hostname in ("127.0.0.1", "localhost"):
        fixed = runtime.engine_endpoint_url(None, args.endpoint_port, "127.0.0.1")
    else:
        candidates = runtime.lan_ip_candidates()
        fixed = runtime.engine_endpoint_url(None, args.endpoint_port, candidates[0]) if candidates else endpoint_url

    if fixed != endpoint_url:
        print(f"{endpoint_url} was not reachable — advertising {fixed} instead.")
        run_records.update_record(args.grid, args.name, endpoint_url=fixed)
    return fixed


def _run_engine(args: SimpleNamespace) -> int:
    cfg = config.select_grid(args.grid)
    grid_url = runtime.grid_url(cfg)
    node_id = args.node_id
    launched = None
    media_proc = None
    media_url = None
    comfyui_started = False
    registered = False
    launcher = None
    try:
        # A Claude seat short-circuits the llama-server bring-up: its "engine" is the operator's
        # `claude` CLI behind a loopback server this loop owns.
        if api_catalog.local_seat_port(getattr(args, "api_kind", None) or "") is not None:
            return _run_cli_seat_engine(args, cfg, grid_url, node_id)
        # An API media engine short-circuits the text/ComfyUI bring-up entirely: its models are
        # served by the vendor bridge on loopback, and it has no local endpoint_url at all.
        if getattr(args, "api_kind", None):
            return _run_api_media_engine(args, cfg, grid_url, node_id)
        if not args.models and not args.enable_media:
            raise SystemExit("Provide a model for a text engine or --media for a media-only engine.")
        text_advertised_models = _advertised_text_models(args.models, args.advertise_as)
        endpoint_url = None
        if args.endpoint_url:
            endpoint_url = runtime.engine_endpoint_url(args.endpoint_url, args.endpoint_port, args.advertise_host)
        elif args.models:
            endpoint_url = runtime.engine_endpoint_url(None, args.endpoint_port, args.advertise_host)
            if len(args.models) != 1:
                raise SystemExit("Built-in engine launch supports exactly one model. Use --at for custom engines.")
            from shared.engine import launcher as launcher_mod

            launcher = launcher_mod
            if runtime.port_in_use(args.endpoint_port):
                # Same fix as a busy `grid start` port: this runs in the detached child
                # (`__engine`), so a hard abort here used to surface as "Engine ... exited
                # before it registered. Port 8081 already in use; aborting." — a dead join with
                # no way forward short of typing `--endpoint-port` and guessing a free number.
                holder = runtime.port_holder(args.endpoint_port)
                replacement = runtime.free_port_from(args.endpoint_port + 1)
                if replacement is None:
                    raise SystemExit(
                        f"Port {args.endpoint_port} is already in use"
                        f"{f' by {holder}' if holder else ''}, and no free port was found near it."
                    )
                print(
                    f"Port {args.endpoint_port} is in use"
                    f"{f' by {holder}' if holder else ''} — starting on {replacement} instead."
                )
                args.endpoint_port = replacement
                endpoint_url = runtime.engine_endpoint_url(
                    args.endpoint_url, args.endpoint_port, args.advertise_host
                )
                # The record on disk still names the old port — every later `grid engines` /
                # `grid leave` read has to see the one actually running, not the one asked for.
                run_records.update_record(
                    args.grid, args.name, endpoint_port=replacement, endpoint_url=endpoint_url
                )
            launcher.assert_supported_build()
            launched = launcher.start_llm(
                args.models[0],
                port=args.endpoint_port,
                ctx_size=args.ctx_size,
                n_predict=args.n_predict,
                parallel=args.parallel,
                flash_attn=args.flash_attn,
                mmproj=getattr(args, "mmproj", None),
                temp=args.temp,
                reasoning_budget=args.reasoning_budget,
                alias=text_advertised_models[0],
            )
            print(f"Spawned llama-server pid={launched.proc.pid}, log={launched.log}")
            launcher.wait_for_models(launched)
            print(f"llama-server is ready on :{args.endpoint_port}")
            endpoint_url = _fixed_up_endpoint(endpoint_url, args, grid_url)

        advertised_models = list(text_advertised_models)
        # Map each advertised (possibly `--advertise-as` alias) text model to the name the engine
        # answers to, so the local proxy can rewrite the model before forwarding: the real model for
        # an external `--at` engine, the alias itself for a built-in (llama-server is launched with
        # `--alias`, so it *is* the alias). Media models keep their fixed `comfyui:*` names — no rewrite.
        if args.endpoint_url:
            upstream = dict(zip(text_advertised_models, args.models))
        else:
            upstream = {name: name for name in text_advertised_models}
        if args.enable_media:
            prepared = _prepare_media_engine(args)
            advertised_models.extend(prepared["models"])
            media_proc = prepared["proc"]
            media_url = prepared["media_url"]
            comfyui_started = bool(prepared["comfyui_started"])
            if comfyui_started:
                # Persist ownership so `grid leave` reaps only a ComfyUI THIS engine started — never one
                # shared with another media engine or already running when we joined.
                run_records.update_record(args.grid, args.name, comfyui_started=True)

        max_concurrency = max(1, int(getattr(args, "max_concurrency", None) or 1))
        reported_load = {
            "active_tasks": 0,
            "max_concurrency": max_concurrency,
        }
        payload = {
            "role": "engine",
            "models": advertised_models,
            "endpoint_url": endpoint_url,
            "media_url": media_url,
            "name": args.name,
            "pricing": {},
            "capabilities": _merge_capabilities(
                # `--at` names an engine somewhere on the network; a built-in one was just spawned
                # here, so probe it over loopback rather than the address we advertise outward.
                _text_capabilities(
                    text_advertised_models, upstream,
                    args.endpoint_url or f"http://127.0.0.1:{args.endpoint_port}/v1",
                ) if text_advertised_models else {},
                _media_capabilities(advertised_models) if args.enable_media else {},
            ),
            "load": reported_load,
            "upstream": upstream,
        }
        # Old built-in records predate ``runtime_kind``; their missing endpoint is still definitive
        # evidence that this loop launched llama.cpp. External records stay unknown unless the
        # operator supplied --kind or auto-discovery recorded the detected label.
        runtime_kind = getattr(args, "runtime_kind", None)
        if not runtime_kind and not args.endpoint_url and args.models:
            runtime_kind = "llama.cpp"
        runtimes = list(
            dict.fromkeys(
                runtime
                for runtime in (
                    runtime_kind,
                    "comfyui" if args.enable_media else None,
                )
                if runtime
            )
        )
        gpu_memory_mb = list(getattr(args, "gpu_memory_mb", []) or [])
        gpu_count = int(getattr(args, "gpu_count", None) or len(gpu_memory_mb))
        if runtimes or gpu_count or gpu_memory_mb:
            payload["resources"] = {
                **({"runtimes": runtimes} if runtimes else {}),
                **({"gpu_count": gpu_count} if gpu_count else {}),
                **({"gpu_memory_mb": gpu_memory_mb} if gpu_memory_mb else {}),
            }
        _register_engine(grid_url, node_id, payload)
        registered = True
        print(f"Engine {node_id} advertised on {grid_url}")
        print(f"models={','.join(advertised_models)}")
        if endpoint_url:
            print(f"endpoint_url={endpoint_url}")
        if media_url:
            print(f"media_url={media_url}")
        print("Send SIGTERM (grid leave) to unregister.")
        while True:
            time.sleep(max(1.0, float(args.heartbeat_interval)))
            try:
                _heartbeat(grid_url, node_id, reported_load, payload)
            except httpx.RequestError as exc:
                print(f"Heartbeat failed: {exc}", file=sys.stderr)
    except KeyboardInterrupt:
        print("\nEngine unregistered.")
        return 0
    finally:
        if registered:
            try:
                httpx.delete(f"{grid_url}/nodes/{node_id}", timeout=5)
            except Exception as exc:
                print(f"Unregister failed (ignoring): {exc}", file=sys.stderr)
        if launched is not None and launcher is not None:
            launcher.stop(launched)
            print(f"Stopped llama-server on :{args.endpoint_port}")
        if media_proc is not None:
            from local import media_runtime

            media_runtime.stop_media_server(media_proc)
            print(f"Stopped engine media server on :{args.media_port}")
        if comfyui_started:
            from shared.engine import comfyui

            comfyui.stop()
            print(f"Stopped ComfyUI on :{args.comfyui_port}")
        if not registered:
            # An engine that exited before registering (e.g. a media engine whose ComfyUI never became
            # ready — it slips past the 3s spawn grace, so nothing else reaps it) must not leave a stale
            # record, or `grid leave --all` is needed just to clear the ghost. Ownership-checked: a
            # record a newer live engine child owns is kept, never unlinked (issue 05's audit).
            run_records.discard_own_record(args.grid, args.name)


# ---------------------------------------------------------------------------
# detection helpers
# ---------------------------------------------------------------------------

def _detect(advertise_host: str | None) -> list[Any]:
    from shared.system import detect

    return detect.detect_engines(advertise_host=advertise_host)


def _print_plan(detected: list[Any]) -> None:
    print("Detected engines on this machine:\n")
    for engine in detected:
        models = ",".join(engine.models) or ("comfyui" if engine.media else "(no models listed)")
        print(f"  {engine.label:<12} {engine.endpoint_url:<34} {models}")
    # Name a kind that is actually on this screen. `--kind <kind>` asked the reader to invent a
    # value the command had just finished discovering for them.
    example = sorted({engine.label for engine in detected})[0]
    print("\nJoin them:")
    print("  grid join --all")
    print(f"  grid join --kind {example}      # or any other kind listed above")


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _confirm(prompt: str) -> bool:
    return input(f"{prompt} [y/N] ").strip().lower() == "y"


def _unique_engine_id(grid_id: str, base: str, used: set[str]) -> str:
    candidate = base
    index = 2
    existing = set(_read_records(grid_id))
    while candidate in used or candidate in existing:
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def _advertised_text_models(models: list[str], aliases: list[str]) -> list[str]:
    if not aliases:
        return list(models)
    if not models:
        raise SystemExit("--advertise-as requires at least one model.")
    if len(aliases) != len(models):
        raise SystemExit("--advertise-as must be provided once for each model.")
    cleaned = [alias.strip() for alias in aliases]
    if any(not alias for alias in cleaned):
        raise SystemExit("--advertise-as values cannot be empty.")
    if any(alias.startswith("comfyui:") for alias in cleaned):
        raise SystemExit("--advertise-as is only for text models; media models use fixed comfyui:* names.")
    if len(set(cleaned)) != len(cleaned):
        raise SystemExit("--advertise-as values must be unique.")
    return cleaned


def _run_api_media_engine(args: SimpleNamespace, cfg: dict[str, Any], grid_url: str, node_id: str) -> int:
    """The detached loop for `grid join --api <media kind>` (local mode).

    Brings up the vendor bridge on loopback, advertises its models with `endpoints: ["media"]`
    (which is what tells the registry these are media models and not text ones), then heartbeats.
    Structurally the media half of `_run_engine`, minus every ComfyUI/llama-server concern.
    """
    from local import media_runtime

    api_key = os.environ.get("GRID_API_MEDIA_KEY", "").strip()
    if not api_key:
        raise SystemExit(
            "GRID_API_MEDIA_KEY is not set for this engine. The key is passed from `grid join` "
            "through the environment and is not stored, so re-run `grid join --api "
            f"{args.api_kind} …` to restart it."
        )
    port = int(getattr(args, "api_media_port", 8190) or 8190)
    proc = media_runtime.start_api_media_server(
        port=port, api_kind=args.api_kind, base_url=args.api_base_url, api_key=api_key,
    )
    print(f"Spawned {args.api_kind} media bridge pid={proc.pid}, url=http://127.0.0.1:{port}")

    models = list(args.models)
    payload = {
        "role": "engine",
        "models": models,
        "endpoint_url": None,
        # Loopback on purpose: the grid proxy runs on this box and is the only thing that should
        # reach the process holding the vendor credential.
        "media_url": f"http://127.0.0.1:{port}",
        "name": args.name,
        "pricing": {},
        "capabilities": _api_media_capabilities(models),
        "load": {
            "active_tasks": 0,
            "max_concurrency": max(1, int(getattr(args, "max_concurrency", None) or 1)),
        },
        "upstream": {},
    }
    print(f"media_url={payload['media_url']} ({args.api_kind} -> {args.api_base_url})")
    return _advertise_until_terminated(
        args, grid_url, node_id, payload,
        stop=lambda: media_runtime.stop_media_server(proc),
        stopped_msg=f"Stopped {args.api_kind} media bridge.",
    )


def _api_media_capabilities(models: list[str]) -> dict[str, Any]:
    """Advertise every model as media-only, the same envelope the remote path builds."""
    from shared.media import media_gating

    return {
        "schema_version": 1,
        "models": {model: media_gating.capability_entry() for model in models},
    }


def _prepare_media_engine(args: SimpleNamespace) -> dict[str, Any]:
    # The bring-up itself lives in `local/media_engine.py` so the remote serve loop can reuse it
    # without a `remote → cli` back-dependency (ADR 0004 §2). This wrapper adapts the local `args`.
    from local import media_engine

    return media_engine.prepare_media_engine(
        media_bundles=list(args.media_bundles) if args.media_bundles else None,
        comfyui_port=args.comfyui_port,
        media_port=args.media_port,
        advertise_host=args.advertise_host,
    )


def _text_capabilities(advertised: list[str], upstream: dict[str, str], llm_url: str) -> dict[str, Any]:
    """Probe this box's text models and describe what they can actually do.

    Local mode used to advertise ``{}`` for every text engine, so a local grid knew a model's NAME
    and nothing else — serve a vision model and the grid still called it text-only. The probe is
    the same one the remote path runs; nothing here is remote-specific, it is just HTTP against the
    engine that was launched a moment ago.

    Probed by the name the engine answers to and keyed by the advertised one, matching the remote
    path: with ``--advertise-as`` those differ, and asking an engine about a name it does not know
    returns nothing. Best-effort — a probe failure costs the description, never the join.
    """
    from remote import probe

    models: dict[str, Any] = {}
    for name in advertised:
        try:
            env = probe.capabilities(llm_url, upstream.get(name, name), advertise_as=name)
        except httpx.HTTPError:
            continue
        models.update((env or {}).get("models") or {})
    return {"schema_version": 1, "models": models} if models else {}


def _merge_capabilities(*envelopes: dict[str, Any]) -> dict[str, Any]:
    """Fold several ``{schema_version, models}`` envelopes into one, or ``{}`` if all are empty.

    An engine can serve text and media at once, and the relay wants a single envelope whose model
    keys match the advertised list exactly — a missing key costs the whole registration.
    """
    models: dict[str, Any] = {}
    for envelope in envelopes:
        models.update((envelope or {}).get("models") or {})
    return {"schema_version": 1, "models": models} if models else {}


def _media_capabilities(models: list[str]) -> dict[str, Any]:
    media_models = {
        model: {
            "endpoints": ["media"],
            "input_modalities": [],
            "output_modalities": [],
            "features": {},
        }
        for model in models
        if model.startswith("comfyui:")
    }
    if not media_models:
        return {}
    return {"schema_version": 1, "models": media_models}


# ---------------------------------------------------------------------------
# registration / state
# ---------------------------------------------------------------------------

def _register_engine(grid_url: str, node_id: str, payload: dict[str, Any]) -> None:
    try:
        resp = httpx.put(f"{grid_url}/nodes/{node_id}", json=payload, timeout=10)
    except httpx.RequestError as exc:
        raise SystemExit(f"Could not reach grid at {grid_url}: {exc}") from exc
    if resp.status_code >= 400:
        raise SystemExit(f"Engine registration failed ({resp.status_code}): {resp.text}")


def _heartbeat(
    grid_url: str,
    node_id: str,
    load: dict[str, Any],
    registration_payload: dict[str, Any],
) -> None:
    resp = httpx.post(f"{grid_url}/nodes/heartbeat", json={"node_id": node_id, "load": load}, timeout=10)
    if resp.status_code == 404:
        _register_engine(grid_url, node_id, registration_payload)
        return
    if resp.status_code >= 400:
        raise SystemExit(f"Engine heartbeat failed ({resp.status_code}): {resp.text}")


# Engine-record I/O + teardown live in `shared.run_records` (shared by both modes, DECISIONS
# D17). These thin wrappers keep the existing `cli.provider._*` call/monkeypatch surface.
def _record_path(grid_id: str, engine_id: str):
    return run_records.record_path(grid_id, engine_id)


def _write_record(grid_id: str, engine_id: str, record: dict[str, Any]) -> None:
    run_records.write_record(grid_id, engine_id, record)


def _read_records(grid_id: str) -> dict[str, dict[str, Any]]:
    return run_records.read_records(grid_id)


def _record_alive(grid_id: str, engine_id: str) -> bool:
    """Whether this engine id is already joined and actually running. `record_alive` rather than a
    bare `pid_alive` (grid-leave issue 08): a zombie pid — the norm in a container with no init
    reaper — made this refuse a re-join of an engine that had already died."""
    record = run_records.read_records(grid_id).get(engine_id)
    return bool(record and run_records.record_alive(record))
