"""`grid provider` commands: advertise an engine into a network and heartbeat it."""
from __future__ import annotations

import argparse
import sys
import time
import uuid
from typing import Any

import httpx

from .. import config, runtime


def cmd_provider_start(args: argparse.Namespace) -> int:
    cfg = config.select_network(args.network)
    signaling_url = runtime.network_url(cfg)
    node_id = args.node_id or f"node-{uuid.uuid4().hex[:12]}"
    launched = None
    media_proc = None
    media_url = None
    comfyui_started = False
    registered = False
    launcher = None
    try:
        if not args.models and not args.enable_media:
            raise SystemExit("Provide --model for a text provider or --enable-media for a media-only provider.")
        text_advertised_models = _advertised_text_models(args.models, getattr(args, "advertise_as", []))
        endpoint_url = None
        if args.endpoint_url:
            endpoint_url = runtime.provider_endpoint_url(args.endpoint_url, args.endpoint_port, args.advertise_host)
        elif args.models:
            endpoint_url = runtime.provider_endpoint_url(None, args.endpoint_port, args.advertise_host)
            if len(args.models) != 1:
                raise SystemExit("Local llama-server launch supports exactly one --model. Use --endpoint-url for custom providers.")
            from ..engine import launcher as launcher_mod

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
        _register_provider(signaling_url, node_id, payload)
        registered = True
        print(f"Provider {node_id} advertised on {signaling_url}")
        print(f"models={','.join(advertised_models)}")
        if endpoint_url:
            print(f"endpoint_url={endpoint_url}")
        if media_url:
            print(f"media_url={media_url}")
        print("Press Ctrl-C to unregister.")
        while True:
            time.sleep(max(1.0, float(args.heartbeat_interval)))
            try:
                _heartbeat(signaling_url, node_id, {"active_tasks": 0}, payload)
            except httpx.RequestError as exc:
                print(f"Heartbeat failed: {exc}", file=sys.stderr)
    except KeyboardInterrupt:
        print("\nProvider unregistered.")
        return 0
    finally:
        if registered:
            try:
                httpx.delete(f"{signaling_url}/nodes/{node_id}", timeout=5)
            except Exception:
                pass
        if launched is not None and launcher is not None:
            launcher.stop(launched)
            print(f"Stopped llama-server on :{args.endpoint_port}")
        if media_proc is not None:
            from .. import media_runtime

            media_runtime.stop_media_server(media_proc)
            print(f"Stopped provider media server on :{args.media_port}")
        if comfyui_started:
            from ..engine import comfyui

            comfyui.stop()
            print(f"Stopped ComfyUI on :{args.comfyui_port}")


def _advertised_text_models(models: list[str], aliases: list[str]) -> list[str]:
    if not aliases:
        return list(models)
    if not models:
        raise SystemExit("--advertise-as requires at least one --model.")
    if len(aliases) != len(models):
        raise SystemExit("--advertise-as must be provided once for each --model.")
    cleaned = [alias.strip() for alias in aliases]
    if any(not alias for alias in cleaned):
        raise SystemExit("--advertise-as values cannot be empty.")
    if any(alias.startswith("comfyui:") for alias in cleaned):
        raise SystemExit("--advertise-as is only for text models; media models use fixed comfyui:* names.")
    if len(set(cleaned)) != len(cleaned):
        raise SystemExit("--advertise-as values must be unique.")
    return cleaned


def _prepare_media_provider(args: argparse.Namespace) -> dict[str, Any]:
    from .. import media_runtime
    from ..engine import comfyui
    from ..models import media_bundles
    from ..provider import media_gating
    from ..system import gpu as gpu_probe
    from ..system import host as host_probe

    if not comfyui.comfyui_dir().exists():
        raise SystemExit(
            "ComfyUI is not installed. Run `grid media install` first, then "
            "`grid media pull <bundle>` for each bundle you want to serve."
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
            f"(detected {memory_label}). Either skip --enable-media or run on a larger GPU/system."
        )

    missing: list[str] = []
    for gate in gates:
        for spec in media_bundles.BUNDLES[gate.bundle]:
            if not media_bundles.target_path(spec).exists():
                missing.append(f"{gate.bundle}/{spec.hf_path}")
    if missing:
        raise SystemExit(
            "ComfyUI bundles are missing files. Run `grid media pull <bundle>` "
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
    print(f"Spawned provider media server pid={proc.pid}, url={media_url}")
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


def cmd_provider_list(args: argparse.Namespace) -> int:
    cfg = config.select_network(args.network)
    params = {"model": args.model} if args.model else None
    try:
        resp = httpx.get(f"{runtime.network_url(cfg)}/nodes/discover", params=params, timeout=10)
        resp.raise_for_status()
    except httpx.RequestError as exc:
        raise SystemExit(f"Could not reach LAN signaling server: {exc}") from exc
    except httpx.HTTPStatusError as exc:
        raise SystemExit(f"Provider discovery failed: {exc.response.status_code} {exc.response.text}") from exc
    providers = resp.json().get("providers", [])
    if not providers:
        print("(no active providers)")
        return 0
    for provider in providers:
        models = ",".join(provider.get("models") or [])
        print(f"{provider['node_id']}\t{models}\t{provider.get('endpoint_url', '')}")
    return 0


def _register_provider(signaling_url: str, node_id: str, payload: dict[str, Any]) -> None:
    try:
        resp = httpx.put(f"{signaling_url}/nodes/{node_id}", json=payload, timeout=10)
    except httpx.RequestError as exc:
        raise SystemExit(f"Could not reach LAN signaling server at {signaling_url}: {exc}") from exc
    if resp.status_code >= 400:
        raise SystemExit(f"Provider registration failed ({resp.status_code}): {resp.text}")


def _heartbeat(
    signaling_url: str,
    node_id: str,
    load: dict[str, Any],
    registration_payload: dict[str, Any],
) -> None:
    resp = httpx.post(f"{signaling_url}/nodes/heartbeat", json={"node_id": node_id, "load": load}, timeout=10)
    if resp.status_code == 404:
        _register_provider(signaling_url, node_id, registration_payload)
        return
    if resp.status_code >= 400:
        raise SystemExit(f"Provider heartbeat failed ({resp.status_code}): {resp.text}")

