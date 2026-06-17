from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from . import __version__, config, paths, runtime

VALID_MEDIA_BUNDLES = ("image_generation", "image_editing", "i2v")
VALID_I2V_DURATIONS = ("5s", "8s")
VALID_I2V_ASPECT_RATIOS = ("2:3", "3:2", "1:1")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="grid",
        description="LAN-only, unauthenticated Grid CLI.",
    )
    parser.add_argument("--version", action="version", version=f"grid {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    network = sub.add_parser("network", help="Create and manage LAN networks")
    net_sub = network.add_subparsers(dest="subcommand", required=True)

    create = net_sub.add_parser("create", help="Create and start a LAN signaling server")
    create.add_argument("name")
    create.add_argument("--port", type=int, default=runtime.DEFAULT_PORT)
    create.add_argument("--host", default=runtime.DEFAULT_HOST)
    create.add_argument("--advertise-host", default=None)
    create.add_argument("--network-id", default=None)
    create.set_defaults(handler=cmd_network_create)

    start = net_sub.add_parser("start", help="Start a local managed signaling server")
    start.add_argument("network")
    start.set_defaults(handler=cmd_network_start)

    stop = net_sub.add_parser("stop", help="Stop a local managed signaling server")
    stop.add_argument("network")
    stop.set_defaults(handler=cmd_network_stop)

    status = net_sub.add_parser("status", help="Show network status")
    status.add_argument("network")
    status.set_defaults(handler=cmd_network_status)

    list_cmd = net_sub.add_parser("list", help="List saved networks")
    list_cmd.set_defaults(handler=cmd_network_list)

    provider = sub.add_parser("provider", help="Provider lifecycle")
    provider_sub = provider.add_subparsers(dest="subcommand", required=True)

    provider_start = provider_sub.add_parser(
        "start",
        help="Advertise an OpenAI-compatible provider endpoint and keep heartbeating",
    )
    provider_start.add_argument("--network", required=True)
    provider_start.add_argument("--model", action="append", dest="models", default=[])
    provider_start.add_argument(
        "--advertise-as",
        action="append",
        dest="advertise_as",
        default=[],
        help="Model name advertised to the signaling server. Repeat once per --model.",
    )
    provider_start.add_argument("--endpoint-url", default=None)
    provider_start.add_argument("--endpoint-port", type=int, default=8081)
    provider_start.add_argument("--llama-port", dest="endpoint_port", type=int, default=argparse.SUPPRESS)
    provider_start.add_argument("--advertise-host", default=None)
    provider_start.add_argument("--node-id", default=None)
    provider_start.add_argument("--name", default=None)
    provider_start.add_argument("--heartbeat-interval", type=float, default=15.0)
    provider_start.add_argument("--ctx-size", type=int, default=None)
    provider_start.add_argument("--n-predict", type=int, default=None)
    provider_start.add_argument("--parallel", type=int, default=None)
    provider_start.add_argument("--flash-attn", default=None)
    provider_start.add_argument("--temp", type=float, default=None)
    provider_start.add_argument("--reasoning-budget", type=int, default=None)
    provider_start.add_argument("--enable-media", action="store_true")
    provider_start.add_argument(
        "--media-bundle",
        action="append",
        dest="media_bundles",
        choices=VALID_MEDIA_BUNDLES,
        default=[],
        help="Media bundle to advertise; repeat for multiple bundles.",
    )
    provider_start.add_argument("--comfyui-port", type=int, default=8188)
    provider_start.add_argument("--media-port", type=int, default=8190)
    provider_start.set_defaults(handler=cmd_provider_start)

    provider_list = provider_sub.add_parser("list", help="List active providers on a network")
    provider_list.add_argument("--network", required=True)
    provider_list.add_argument("--model", default=None)
    provider_list.set_defaults(handler=cmd_provider_list)

    llama_cpp = sub.add_parser("llama.cpp", help="Manage the local llama.cpp engine")
    llama_cpp_sub = llama_cpp.add_subparsers(dest="subcommand", required=True)
    llama_cpp_install = llama_cpp_sub.add_parser("install", help="Install or upgrade llama-server")
    llama_cpp_install.add_argument(
        "--from-source",
        action="store_true",
        help=(
            "On Apple Silicon, build llama.cpp with Metal from source instead of using Homebrew; "
            "on Linux NVIDIA, build from source instead of using a pinned tarball."
        ),
    )
    llama_cpp_install.add_argument(
        "--target-sm",
        default=None,
        help="Linux NVIDIA only: override the detected compute capability, for example sm_86.",
    )
    llama_cpp_install.set_defaults(handler=cmd_llama_cpp_install)

    models = sub.add_parser("models", help="Manage local GGUF model files")
    models_sub = models.add_subparsers(dest="subcommand", required=True)
    models_list = models_sub.add_parser("list", help="List local models")
    models_list.add_argument("--catalog", action="store_true", help="Also print the curated model catalog.")
    models_list.set_defaults(handler=cmd_models_list)
    models_pull = models_sub.add_parser("pull", help="Download a GGUF model from Hugging Face")
    models_pull.add_argument(
        "spec",
        help="Either '<hf-repo>:<filename>' or a catalog label from `grid models list --catalog`.",
    )
    models_pull.set_defaults(handler=cmd_models_pull)
    models_rm = models_sub.add_parser("rm", help="Delete a local model file")
    models_rm.add_argument("name", help="Filename under ~/.grid/models/")
    models_rm.add_argument("--yes", action="store_true", help="Skip confirmation.")
    models_rm.set_defaults(handler=cmd_models_rm)

    media = sub.add_parser("media", help="Manage the local ComfyUI media runtime")
    media_sub = media.add_subparsers(dest="subcommand", required=True)
    media_install = media_sub.add_parser("install", help="Install ComfyUI and media runtime dependencies")
    media_install.set_defaults(handler=cmd_media_install)
    media_pull = media_sub.add_parser("pull", help="Download a media model bundle")
    media_pull.add_argument("bundle", choices=VALID_MEDIA_BUNDLES)
    media_pull.set_defaults(handler=cmd_media_pull)
    media_status = media_sub.add_parser("status", help="Show ComfyUI install and runtime status")
    media_status.add_argument("--port", type=int, default=8188)
    media_status.set_defaults(handler=cmd_media_status)
    media_start = media_sub.add_parser("start", help="Start ComfyUI")
    media_start.add_argument("--port", type=int, default=8188)
    media_start.add_argument(
        "--detach",
        action="store_true",
        help="Return after ComfyUI is ready instead of blocking on its lifetime.",
    )
    media_start.set_defaults(handler=cmd_media_start)
    media_stop = media_sub.add_parser("stop", help="Stop ComfyUI")
    media_stop.set_defaults(handler=cmd_media_stop)

    consumer = sub.add_parser("consumer", help="Consumer helpers")
    consumer_sub = consumer.add_subparsers(dest="subcommand", required=True)
    env = consumer_sub.add_parser("env", help="Print OpenAI-compatible environment variables")
    env.add_argument("--network", required=True)
    env.set_defaults(handler=cmd_consumer_env)

    request = sub.add_parser("request", help="Smoke-test requests through the LAN server")
    req_sub = request.add_subparsers(dest="subcommand", required=True)
    chat = req_sub.add_parser("chat", help="Send one chat completion request")
    chat.add_argument("--network", required=True)
    chat.add_argument("--model", required=True)
    chat.add_argument("--message", required=True)
    chat.add_argument("--timeout", type=float, default=600.0)
    chat.set_defaults(handler=cmd_request_chat)

    media_req = req_sub.add_parser("media", help="Send media requests through a LAN network")
    media_req_sub = media_req.add_subparsers(dest="media_command", required=True)
    image_gen = media_req_sub.add_parser("image-generate", help="Generate an image")
    _add_media_request_common_args(image_gen)
    image_gen.add_argument("--prompt", required=True)
    image_gen.add_argument("--width", type=int, default=720)
    image_gen.add_argument("--height", type=int, default=720)
    image_gen.add_argument("--steps", type=int, default=4)
    image_gen.set_defaults(handler=cmd_request_media_image_generate)
    image_edit = media_req_sub.add_parser("image-edit", help="Edit one to three images")
    _add_media_request_common_args(image_edit)
    image_edit.add_argument("--prompt", required=True)
    image_edit.add_argument(
        "--image",
        action="append",
        dest="input_images",
        required=True,
        help="Input image path. Repeat up to three times.",
    )
    image_edit.add_argument("--steps", type=int, default=4)
    image_edit.set_defaults(handler=cmd_request_media_image_edit)
    i2v = media_req_sub.add_parser("i2v", help="Generate a short video from an image")
    _add_media_request_common_args(i2v)
    i2v.add_argument("--prompt", required=True)
    i2v.add_argument("--image", required=True, help="Input image path.")
    i2v.add_argument("--duration", choices=VALID_I2V_DURATIONS, default="5s")
    i2v.add_argument("--aspect-ratio", choices=VALID_I2V_ASPECT_RATIOS, default="2:3")
    i2v.set_defaults(handler=cmd_request_media_i2v)

    return parser


def _add_media_request_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--network", required=True)
    parser.add_argument("--timeout", type=int, default=1800, help="Seconds to wait for the streamed result.")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory for returned media files. Defaults to ~/.grid/outputs.",
    )


def cmd_network_create(args: argparse.Namespace) -> int:
    existing = _network_by_name(args.name)
    cfg = existing or runtime.init_network_config(
        name=args.name,
        port=args.port,
        host=args.host,
        network_id=args.network_id,
        advertise_host=args.advertise_host,
    )
    pid = runtime.start_server(cfg)
    print(f"Started LAN network {cfg['name']} ({cfg['network_id']})")
    print(f"network_type={cfg['network_type']}")
    print(f"signaling_url={runtime.network_url(cfg)}")
    print(f"server_pid={pid}")
    return 0


def cmd_network_start(args: argparse.Namespace) -> int:
    cfg = config.select_network(args.network)
    pid = runtime.start_server(cfg)
    print(f"Started {cfg['name']} at {runtime.network_url(cfg)} pid={pid}")
    return 0


def cmd_network_stop(args: argparse.Namespace) -> int:
    cfg = config.select_network(args.network)
    if not cfg.get("managed_server", True):
        print(f"{cfg['name']} is hosted by another LAN device; nothing to stop locally.")
        return 0
    runtime.stop_server(cfg)
    print(f"Stopped {cfg['name']}.")
    return 0


def cmd_network_status(args: argparse.Namespace) -> int:
    cfg = config.select_network(args.network)
    print(f"name={cfg['name']}")
    print(f"network_id={cfg['network_id']}")
    print(f"network_type={cfg['network_type']}")
    print(f"signaling_url={runtime.network_url(cfg)}")
    print(f"managed_server={str(bool(cfg.get('managed_server', True))).lower()}")
    print(f"server_pid={int(cfg.get('server_pid') or 0)}")
    try:
        info = httpx.get(f"{runtime.network_url(cfg)}/server/info", timeout=2).json()
    except Exception as exc:
        print(f"server_status=unreachable ({exc})")
    else:
        print("server_status=reachable")
        print(f"providers_online={info.get('providers_online', 0)}")
    return 0


def cmd_network_list(args: argparse.Namespace) -> int:
    networks = config.iter_network_configs()
    if not networks:
        print("(no saved networks)")
        return 0
    for cfg in networks:
        managed = "local" if cfg.get("managed_server", True) else "remote"
        print(f"{cfg['name']}\t{cfg['network_id']}\t{managed}\t{runtime.network_url(cfg)}")
    return 0


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
            from .engine import launcher as launcher_mod

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
            from . import media_runtime

            media_runtime.stop_media_server(media_proc)
            print(f"Stopped provider media server on :{args.media_port}")
        if comfyui_started:
            from .engine import comfyui

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
    from . import media_runtime
    from .engine import comfyui
    from .models import media_bundles
    from .provider import media_gating
    from .system import gpu as gpu_probe
    from .system import host as host_probe

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


def cmd_llama_cpp_install(args: argparse.Namespace) -> int:
    from .engine import installer

    if installer.is_apple_silicon():
        if args.target_sm:
            raise SystemExit("Apple Silicon installs do not use --target-sm; omit it for Homebrew or Metal builds.")
        if args.from_source:
            path = installer.install_metal_from_source()
            print(f"Installed llama-server with Metal -> {path}")
            return 0
        path = installer.install_macos_homebrew()
        print(f"Installed Homebrew llama-server -> {path}")
        return 0

    from .system import gpu

    gpus = gpu.enumerate_gpus()
    if not gpus and not args.target_sm:
        raise SystemExit(
            "No NVIDIA GPUs detected (nvidia-smi missing or returned nothing). "
            "Pass --target-sm <sm_XX> to override."
        )

    sm_required = (args.target_sm,) if args.target_sm else tuple(item.compute_cap_sm for item in gpus)
    print(f"Detected GPUs: {', '.join(sm_required) if sm_required else '(none)'}")

    if args.from_source:
        target_sm = sm_required[0]
        path = installer.install_from_source(target_sm)
        print(f"Installed llama-server from source -> {path}")
        return 0

    tarball = installer.pick_tarball(gpus) if not args.target_sm else None
    if not tarball and args.target_sm:
        for candidate in installer.TARBALLS:
            if args.target_sm in candidate.supports_sm:
                tarball = candidate
                break
    if not tarball:
        raise SystemExit(
            "No pinned tarball covers this GPU set. Re-run with --from-source to build llama.cpp locally."
        )
    path = installer.install_pinned(tarball)
    print(f"Installed llama-server ({tarball.label}) -> {path}")
    return 0


def cmd_models_list(args: argparse.Namespace) -> int:
    from .models import catalog, store

    stored = store.list_all()
    if not stored:
        print("(no local models - try `grid models pull <hf-repo>:<file>`)")
    else:
        for model in stored:
            print(f"{model.name:<60} {model.size_bytes / 1e9:>7.2f} GB")
    if args.catalog:
        print()
        print("Recommended catalog:")
        for entry in catalog.recommended_entries():
            print(catalog.format_catalog_entry(entry))
    return 0


def cmd_models_pull(args: argparse.Namespace) -> int:
    from .models import catalog, download

    entry = catalog.find(args.spec)
    if entry:
        repo, filename = entry.hf_repo, entry.quantized_file
        print(f"Resolved catalog label {entry.label!r} -> {repo}/{filename}")
    else:
        repo, filename = download.parse_spec(args.spec)
    print(f"Downloading {repo}/{filename} ...")
    target = download.download(repo, filename, on_progress=download.stderr_progress)
    print(f"Saved {target}")
    return 0


def cmd_models_rm(args: argparse.Namespace) -> int:
    from .models import store

    model = store.find(args.name)
    if not model:
        raise SystemExit(f"No such model: {args.name}")
    if not args.yes:
        response = input(f"Delete {model.path} ({model.size_bytes / 1e9:.2f} GB)? [y/N] ").strip().lower()
        if response != "y":
            print("Aborted.")
            return 1
    store.remove(args.name)
    print(f"Removed {model.path}")
    return 0


def cmd_media_install(args: argparse.Namespace) -> int:
    from .engine import comfyui

    comfyui.install()
    print("Done. Run `grid media pull image_generation` or another bundle to download model files.")
    return 0


def cmd_media_pull(args: argparse.Namespace) -> int:
    from .models import download, media_bundles

    paths_written = media_bundles.pull_bundle(args.bundle, on_progress=download.stderr_progress)
    print(f"Downloaded {len(paths_written)} file(s) into the ComfyUI models tree.")
    return 0


def cmd_media_status(args: argparse.Namespace) -> int:
    from .engine import comfyui
    from .models import media_bundles

    installed = comfyui.comfyui_dir().exists()
    print(f"Installed       : {'yes' if installed else 'no'} ({comfyui.comfyui_dir()})")
    print(f"Python (venv)   : {comfyui.comfyui_python()}")
    print(f"Output dir      : {comfyui.output_dir()}")
    if installed:
        for name in VALID_MEDIA_BUNDLES:
            files = media_bundles.BUNDLES[name]
            present = sum(1 for file_spec in files if media_bundles.target_path(file_spec).exists())
            print(f"Bundle {name:<18} {present}/{len(files)} files present")
    print(f"Running         : {'yes' if comfyui.is_running(args.port) else 'no'} (port {args.port})")
    return 0


def cmd_media_start(args: argparse.Namespace) -> int:
    from .engine import comfyui

    cp = comfyui.start(args.port)
    print(f"Spawned ComfyUI pid={cp.proc.pid}, log={cp.log}")
    comfyui.wait_for_ready(args.port)
    print(f"ComfyUI ready on http://localhost:{args.port}")
    if args.detach:
        return 0
    try:
        cp.proc.wait()
    except KeyboardInterrupt:
        comfyui.stop()
    return 0


def cmd_media_stop(args: argparse.Namespace) -> int:
    from .engine import comfyui

    return comfyui.stop_running()


def cmd_consumer_env(args: argparse.Namespace) -> int:
    cfg = config.select_network(args.network)
    print(f'export OPENAI_BASE_URL="{runtime.network_url(cfg)}/v1"')
    print('export OPENAI_API_KEY="local-lan"')
    return 0


def cmd_request_chat(args: argparse.Namespace) -> int:
    cfg = config.select_network(args.network)
    try:
        resp = httpx.post(
            f"{runtime.network_url(cfg)}/v1/chat/completions",
            json={"model": args.model, "messages": [{"role": "user", "content": args.message}]},
            timeout=args.timeout,
        )
    except httpx.RequestError as exc:
        raise SystemExit(f"Request failed: {exc}") from exc
    print(resp.text)
    return 0 if resp.status_code < 400 else 1


def cmd_request_media_image_generate(args: argparse.Namespace) -> int:
    return _post_media_request(
        args,
        "media/image/generate",
        {
            "prompt": args.prompt,
            "width": args.width,
            "height": args.height,
            "steps": args.steps,
        },
    )


def cmd_request_media_image_edit(args: argparse.Namespace) -> int:
    if len(args.input_images) > 3:
        raise SystemExit("Image editing supports at most three --image values.")
    return _post_media_request(
        args,
        "media/image/edit",
        {
            "prompt": args.prompt,
            "steps": args.steps,
            "input_images": [_load_media_file(path) for path in args.input_images],
        },
    )


def cmd_request_media_i2v(args: argparse.Namespace) -> int:
    payload = {
        "prompt": args.prompt,
        "duration": args.duration,
        "aspect_ratio": args.aspect_ratio,
        "input_image": _load_media_file(args.image),
    }
    return _post_media_request(args, "media/video/i2v", payload)


def _post_media_request(args: argparse.Namespace, endpoint_path: str, payload: dict[str, Any]) -> int:
    cfg = config.select_network(args.network)
    timeout = httpx.Timeout(float(args.timeout), read=float(args.timeout))
    url = f"{runtime.network_url(cfg)}/v1/{endpoint_path}"
    output_dir = Path(args.output_dir).expanduser() if args.output_dir else paths.grid_home() / "outputs"
    try:
        with httpx.stream("POST", url, json=payload, timeout=timeout) as resp:
            if resp.status_code >= 400:
                print(resp.read().decode("utf-8", errors="replace"))
                return 1
            return _consume_media_sse(resp, output_dir)
    except httpx.RequestError as exc:
        print(f"Media request failed: {exc}", file=sys.stderr)
        return 1


def _consume_media_sse(resp: httpx.Response, output_dir: Path) -> int:
    exit_code = 0
    saw_result = False
    for line in resp.iter_lines():
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            print(line)
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            print(data)
            continue
        if "error" in event:
            print(f"Error: {event['error']}", file=sys.stderr)
            exit_code = 1
            continue
        if event.get("type") == "progress":
            progress = event.get("progress")
            status = event.get("status", "running")
            print(f"progress={progress}% status={status}", file=sys.stderr)
            continue
        if event.get("type") == "result":
            saw_result = True
            written = _write_media_outputs(event.get("output_files") or [], output_dir)
            for path in written:
                print(path)
            continue
        print(json.dumps(event, sort_keys=True))
    if not saw_result and exit_code == 0:
        print("No media result returned.", file=sys.stderr)
        return 1
    return exit_code


def _load_media_file(path_value: str) -> dict[str, str]:
    path = Path(path_value).expanduser()
    if not path.is_file():
        raise SystemExit(f"Input image not found: {path}")
    return {
        "filename": path.name,
        "content_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
    }


def _write_media_outputs(output_files: list[dict[str, Any]], output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for index, item in enumerate(output_files, start=1):
        filename = Path(str(item.get("filename") or f"media_output_{index}")).name
        content_base64 = item.get("content_base64")
        if not content_base64:
            continue
        out_path = _unused_path(output_dir / filename)
        out_path.write_bytes(base64.b64decode(content_base64))
        written.append(out_path)
    return written


def _unused_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(1, 10_000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise SystemExit(f"Could not find an unused output path for {path}")


def cmd_internal_server(network_id: str) -> int:
    import uvicorn

    cfg = config.load_network_config(network_id)
    if not cfg:
        raise SystemExit(f"Network config not found: {network_id}")
    from .server import create_app

    app = create_app(network_id=cfg["network_id"], network_name=cfg["name"])
    uvicorn.run(app, host=cfg.get("host") or runtime.DEFAULT_HOST, port=int(cfg["port"]))
    return 0


def cmd_internal_media_server(port: int, comfyui_url: str) -> int:
    import uvicorn

    from .provider.media_server import create_app

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
    return None


def _network_by_name(name_or_id: str) -> dict[str, Any] | None:
    for cfg in config.iter_network_configs():
        if cfg.get("name") == name_or_id or cfg.get("network_id") == name_or_id:
            return cfg
    return None


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


if __name__ == "__main__":
    raise SystemExit(main())
