"""`grid join` / `grid leave` / `grid models`: the engine lifecycle.

`grid join` registers an engine into a grid and keeps heartbeating it. It runs
the heartbeat loop in a *detached* process (the internal ``__provider`` entry)
and records the engine under ``~/.grid/run/engines/<grid>/`` so a later
`grid leave` can stop and unregister it. `grid models` lists the live models the
grid can serve right now.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from types import SimpleNamespace
from typing import Any

import httpx

import config
import paths
import runtime


# ---------------------------------------------------------------------------
# grid join
# ---------------------------------------------------------------------------

def cmd_join(args: argparse.Namespace) -> int:
    advertise_host = getattr(args, "advertise_host", None)
    cfg = config.select_grid(getattr(args, "grid", None))
    grid_id = cfg["network_id"]

    if args.at and args.serve:
        raise SystemExit("Use either --at (point at an existing engine) or --serve, not both.")

    if args.at:
        if not args.models:
            raise SystemExit("--at requires at least one -m/--model naming what that engine serves.")
        return _spawn_engine(cfg, args, endpoint_url=args.at, models=list(args.models), media=args.media)

    if args.serve:
        return _spawn_engine(cfg, args, endpoint_url=None, models=[args.serve], media=args.media)

    if args.media and not args.models:
        return _spawn_engine(cfg, args, endpoint_url=None, models=[], media=True)

    if args.models:
        raise SystemExit("-m/--model names models for an engine; pair it with --at <url>, or use --serve <model>.")

    # No engine spec: detect what is already running on this box.
    detected = _detect(advertise_host)
    if not detected:
        raise SystemExit(
            "No running engine detected on this box. Point at one with "
            "`grid join --at <url> -m <model>`, or start the built-in engine with `grid join --serve <model>`."
        )
    if args.engine:
        detected = [engine for engine in detected if engine.label == args.engine]
        if not detected:
            raise SystemExit(f"No detected engine named {args.engine!r}. Run `grid join` to list them.")
    elif len(detected) > 1 and not args.all:
        _print_plan(detected)
        if _interactive():
            if not _confirm("Join all detected engines?"):
                print("Nothing joined.")
                return 0
        else:
            raise SystemExit("Multiple engines detected; pass --all, --engine <kind>, or --at <url>.")

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
            )
        except SystemExit as exc:
            print(f"Skipped {engine.label}: {exc}", file=sys.stderr)
            rc = 1
    return rc


def _spawn_engine(
    cfg: dict[str, Any],
    args: argparse.Namespace,
    *,
    endpoint_url: str | None,
    models: list[str],
    engine_id: str | None = None,
    media: bool = False,
) -> int:
    grid_id = cfg["network_id"]
    engine_id = engine_id or getattr(args, "name", None) or f"engine-{uuid.uuid4().hex[:8]}"
    if _record_path(grid_id, engine_id).exists() and _record_alive(grid_id, engine_id):
        raise SystemExit(f"Engine {engine_id!r} is already joined to {cfg['name']}. Use a different --name.")

    record = {
        "engine_id": engine_id,
        "node_id": f"node-{uuid.uuid4().hex[:12]}",
        "network_id": grid_id,
        "pid": 0,
        "endpoint_url": endpoint_url,
        "models": models,
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
        "temp": getattr(args, "temp", None),
        "reasoning_budget": getattr(args, "reasoning_budget", None),
        "started_at": runtime.utc_now(),
    }
    _write_record(grid_id, engine_id, record)

    log_path = paths.engines_dir(grid_id) / f"{engine_id}.log"
    log = log_path.open("ab")
    proc = subprocess.Popen(
        runtime.cli_command() + ["__provider", grid_id, engine_id],
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    record["pid"] = proc.pid
    _write_record(grid_id, engine_id, record)

    print(f"Joined engine {engine_id} to {cfg['name']} (pid={proc.pid})")
    if endpoint_url:
        print(f"endpoint_url={endpoint_url}")
    if models:
        print(f"models={','.join(models)}")
    print(f"log={log_path}")
    print(f"Check `grid models {cfg['name']}`; stop with `grid leave {cfg['name']} --engine {engine_id}`.")
    return 0


# ---------------------------------------------------------------------------
# grid leave
# ---------------------------------------------------------------------------

def cmd_leave(args: argparse.Namespace) -> int:
    cfg = config.select_grid(getattr(args, "grid", None))
    grid_id = cfg["network_id"]
    records = _read_records(grid_id)

    if args.all:
        targets = list(records)
    elif args.engine:
        if args.engine not in records:
            raise SystemExit(f"No engine {args.engine!r} joined to {cfg['name']}.")
        targets = [args.engine]
    elif len(records) == 1:
        targets = list(records)
    elif not records:
        print(f"No engines joined to {cfg['name']}.")
        return 0
    else:
        names = ", ".join(sorted(records))
        raise SystemExit(f"Several engines joined ({names}); pass --engine <id> or --all.")

    for engine_id in targets:
        _stop_engine(grid_id, engine_id, records[engine_id])
        print(f"Left engine {engine_id} on {cfg['name']}.")
    return 0


def _stop_engine(grid_id: str, engine_id: str, record: dict[str, Any]) -> None:
    pid = int(record.get("pid") or 0)
    if pid and _pid_alive(pid):
        # SIGTERM the detached provider so it unregisters and stops its engines.
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.time() + 8
        while time.time() < deadline and _pid_alive(pid):
            time.sleep(0.2)
        if _pid_alive(pid):
            _kill_group(pid)
    _record_path(grid_id, engine_id).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# grid models
# ---------------------------------------------------------------------------

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

    if args.verbose:
        width = max(len("MODEL"), *(len(model) for model, _, _ in rows))
        ewidth = max(len("ENGINE"), *(len(label) for _, label, _ in rows))
        print(f"{'MODEL':<{width}}  {'ENGINE':<{ewidth}}  WHERE")
        for model, label, where in rows:
            print(f"{model:<{width}}  {label:<{ewidth}}  {where}")
        return 0

    seen: list[str] = []
    for model, _, _ in rows:
        if model not in seen:
            seen.append(model)
    for model in seen:
        print(model)
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
        print(f"{label:<{ewidth}}  {where}")
        print(f"{'':<{ewidth}}  models: {models}")
    return 0


def _discover(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    grid_url = runtime.network_url(cfg)
    try:
        resp = httpx.get(f"{grid_url}/nodes/discover", timeout=10)
        resp.raise_for_status()
    except httpx.RequestError as exc:
        raise SystemExit(f"Could not reach grid {cfg['name']} at {grid_url}: {exc}") from exc
    except httpx.HTTPStatusError as exc:
        raise SystemExit(f"Discovery failed: {exc.response.status_code} {exc.response.text}") from exc
    return resp.json().get("providers", [])


def _engine_label(engine: dict[str, Any]) -> str:
    return engine.get("name") or engine.get("node_id", "?")


# ---------------------------------------------------------------------------
# detached provider loop (internal `__provider` entry)
# ---------------------------------------------------------------------------

def run_provider_from_record(grid_id: str, engine_id: str) -> int:
    record = _read_records(grid_id).get(engine_id)
    if not record:
        raise SystemExit(f"No engine record for {engine_id} on {grid_id}.")
    args = SimpleNamespace(
        network=record["network_id"],
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
        temp=record.get("temp"),
        reasoning_budget=record.get("reasoning_budget"),
    )

    def _on_term(_signum, _frame):  # noqa: ANN001
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _on_term)
    return _run_provider(args)


def _run_provider(args: SimpleNamespace) -> int:
    cfg = config.select_network(args.network)
    grid_url = runtime.network_url(cfg)
    node_id = args.node_id
    launched = None
    media_proc = None
    media_url = None
    comfyui_started = False
    registered = False
    launcher = None
    try:
        if not args.models and not args.enable_media:
            raise SystemExit("Provide a model for a text engine or --media for a media-only engine.")
        text_advertised_models = _advertised_text_models(args.models, args.advertise_as)
        endpoint_url = None
        if args.endpoint_url:
            endpoint_url = runtime.provider_endpoint_url(args.endpoint_url, args.endpoint_port, args.advertise_host)
        elif args.models:
            endpoint_url = runtime.provider_endpoint_url(None, args.endpoint_port, args.advertise_host)
            if len(args.models) != 1:
                raise SystemExit("Built-in engine launch supports exactly one model. Use --at for custom engines.")
            from engine import launcher as launcher_mod

            launcher = launcher_mod
            if launcher.is_port_in_use(args.endpoint_port):
                raise SystemExit(f"Port {args.endpoint_port} already in use; aborting.")
            launcher.assert_supported_build()
            launched = launcher.start_llm(
                args.models[0],
                port=args.endpoint_port,
                ctx_size=args.ctx_size,
                n_predict=args.n_predict,
                parallel=args.parallel,
                flash_attn=args.flash_attn,
                temp=args.temp,
                reasoning_budget=args.reasoning_budget,
                alias=text_advertised_models[0],
            )
            print(f"Spawned llama-server pid={launched.proc.pid}, log={launched.log}")
            launcher.wait_for_models(launched)
            print(f"llama-server is ready on :{args.endpoint_port}")

        advertised_models = list(text_advertised_models)
        if args.enable_media:
            prepared = _prepare_media_provider(args)
            advertised_models.extend(prepared["models"])
            media_proc = prepared["proc"]
            media_url = prepared["media_url"]
            comfyui_started = bool(prepared["comfyui_started"])

        payload = {
            "role": "provider",
            "models": advertised_models,
            "endpoint_url": endpoint_url,
            "media_url": media_url,
            "name": args.name,
            "pricing": {},
            "capabilities": _media_capabilities(advertised_models) if args.enable_media else {},
            "load": {"active_tasks": 0},
        }
        _register_provider(grid_url, node_id, payload)
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
                _heartbeat(grid_url, node_id, {"active_tasks": 0}, payload)
            except httpx.RequestError as exc:
                print(f"Heartbeat failed: {exc}", file=sys.stderr)
    except KeyboardInterrupt:
        print("\nEngine unregistered.")
        return 0
    finally:
        if registered:
            try:
                httpx.delete(f"{grid_url}/nodes/{node_id}", timeout=5)
            except Exception:
                pass
        if launched is not None and launcher is not None:
            launcher.stop(launched)
            print(f"Stopped llama-server on :{args.endpoint_port}")
        if media_proc is not None:
            import media_runtime

            media_runtime.stop_media_server(media_proc)
            print(f"Stopped engine media server on :{args.media_port}")
        if comfyui_started:
            from engine import comfyui

            comfyui.stop()
            print(f"Stopped ComfyUI on :{args.comfyui_port}")


# ---------------------------------------------------------------------------
# detection helpers
# ---------------------------------------------------------------------------

def _detect(advertise_host: str | None) -> list[Any]:
    from system import detect

    return detect.detect_engines(advertise_host=advertise_host)


def _print_plan(detected: list[Any]) -> None:
    print("Detected engines on this machine:\n")
    for engine in detected:
        models = ",".join(engine.models) or ("comfyui" if engine.media else "(no models listed)")
        print(f"  {engine.label:<12} {engine.endpoint_url:<34} {models}")
    print("\nJoin them:\n  grid join --all\n  grid join --engine <kind>")


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


def _prepare_media_provider(args: SimpleNamespace) -> dict[str, Any]:
    import media_runtime
    from engine import comfyui
    from models import media_bundles
    from provider import media_gating
    from system import gpu as gpu_probe
    from system import host as host_probe

    if not comfyui.comfyui_dir().exists():
        raise SystemExit(
            "ComfyUI is not installed. Run `grid engine install comfyui` first, then "
            "`grid engine pull <bundle>` for each bundle you want to serve."
        )

    requested = list(args.media_bundles) if args.media_bundles else None
    if media_gating.is_apple_silicon():
        host_info = host_probe.gather()
        memory_mb = [host_info.memory_total_gb * 1024]
        memory_label = f"unified memory = {host_info.memory_total_gb:.1f} GB"
    else:
        gpus = gpu_probe.enumerate_gpus()
        memory_mb = [item.memory_total_mb for item in gpus]
        memory_label = f"VRAM = {[round(value / 1024, 1) for value in memory_mb] or 'none'} GB"

    gates = media_gating.select_bundles(memory_mb, requested=requested)
    if not gates:
        raise SystemExit(
            "No media bundles meet the memory threshold for this host "
            f"(detected {memory_label}). Either skip --media or run on a larger GPU/system."
        )

    missing: list[str] = []
    for gate in gates:
        for spec in media_bundles.BUNDLES[gate.bundle]:
            if not media_bundles.target_path(spec).exists():
                missing.append(f"{gate.bundle}/{spec.hf_path}")
    if missing:
        raise SystemExit(
            "ComfyUI bundles are missing files. Run `grid engine pull <bundle>` "
            "for each enabled bundle. Missing:\n  " + "\n  ".join(missing[:10])
        )

    comfyui_started = False
    if not comfyui.is_running(args.comfyui_port):
        cp = comfyui.start(args.comfyui_port)
        comfyui_started = True
        print(f"Spawned ComfyUI pid={cp.proc.pid}, log={cp.log}")
        comfyui.wait_for_ready(args.comfyui_port)
        print(f"ComfyUI ready on http://localhost:{args.comfyui_port}")

    comfyui_url = f"http://localhost:{args.comfyui_port}/api"
    proc = media_runtime.start_media_server(port=args.media_port, comfyui_url=comfyui_url)
    media_url = runtime.provider_endpoint_url(
        None,
        args.media_port,
        args.advertise_host,
    ).removesuffix("/v1")
    print(f"Spawned engine media server pid={proc.pid}, url={media_url}")
    advertised = [gate.advertise_as for gate in gates]
    print(f"Media enabled: advertising {advertised}")
    return {
        "models": advertised,
        "proc": proc,
        "media_url": media_url,
        "comfyui_started": comfyui_started,
    }


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

def _register_provider(grid_url: str, node_id: str, payload: dict[str, Any]) -> None:
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
        _register_provider(grid_url, node_id, registration_payload)
        return
    if resp.status_code >= 400:
        raise SystemExit(f"Engine heartbeat failed ({resp.status_code}): {resp.text}")


def _record_path(grid_id: str, engine_id: str):
    return paths.engines_dir(grid_id) / f"{engine_id}.json"


def _write_record(grid_id: str, engine_id: str, record: dict[str, Any]) -> None:
    config.atomic_write_json(_record_path(grid_id, engine_id), record)


def _read_records(grid_id: str) -> dict[str, dict[str, Any]]:
    root = paths.engines_dir(grid_id)
    if not root.exists():
        return {}
    records: dict[str, dict[str, Any]] = {}
    for path in sorted(root.glob("*.json")):
        data = config.load_json(path)
        if data.get("engine_id"):
            records[data["engine_id"]] = data
    return records


def _record_alive(grid_id: str, engine_id: str) -> bool:
    record = _read_records(grid_id).get(engine_id)
    return bool(record and _pid_alive(int(record.get("pid") or 0)))


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _kill_group(pid: int) -> None:
    try:
        if hasattr(os, "killpg"):
            os.killpg(pid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
