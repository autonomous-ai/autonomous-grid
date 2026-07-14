"""`grid engine` commands: set up and run the built-in engines (llama.cpp, ComfyUI)."""
from __future__ import annotations

import argparse

from ._constants import VALID_MEDIA_BUNDLES


def cmd_engine_install(args: argparse.Namespace) -> int:
    if args.name == "llama.cpp":
        return _install_llama_cpp(args)
    if args.name == "comfyui":
        from shared.engine import comfyui

        comfyui.install()
        print("Done. Run `grid engine pull <bundle>` to download media model files.")
        return 0
    raise SystemExit(f"Unknown engine {args.name!r}. Choose 'llama.cpp' (text) or 'comfyui' (media).")


def cmd_engine_pull(args: argparse.Namespace) -> int:
    from shared.models import download, media_bundles

    paths_written = media_bundles.pull_bundle(args.bundle, on_progress=download.stderr_progress)
    print(f"Downloaded {len(paths_written)} file(s) into the ComfyUI models tree.")
    return 0


def cmd_engine_status(args: argparse.Namespace) -> int:
    from shared.engine import comfyui
    from shared.models import media_bundles

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


def cmd_engine_start(args: argparse.Namespace) -> int:
    from shared.engine import comfyui

    cp = comfyui.start(args.port)
    print(f"Spawned ComfyUI pid={cp.proc.pid}, log={cp.log}")
    comfyui.wait_for_ready(args.port, proc=cp.proc)
    print(f"ComfyUI ready on http://localhost:{args.port}")
    if args.detach:
        return 0
    try:
        cp.proc.wait()
    except KeyboardInterrupt:
        comfyui.stop()
    return 0


def cmd_engine_stop(args: argparse.Namespace) -> int:
    from shared.engine import comfyui

    return comfyui.stop_running()


def cmd_engine_list(args: argparse.Namespace) -> int:
    """`grid engine ls` — live engines joined to the grid (mode-aware, the same view as `grid engines`).

    `engine` is dispatch-AGNOSTIC, so this leaf runs its handler in both modes; branch on the mode
    dispatch stamped on ``args`` (falling back to the persisted mode for a direct call), mirroring
    ``cli.grid.cmd_overview``."""
    from shared import state

    mode = getattr(args, "mode", None) or state.get_mode()
    if mode == "remote":
        from . import remote_overview

        return remote_overview.cmd_remote_engines(args)
    from . import provider

    return provider.cmd_engines(args)


def _install_llama_cpp(args: argparse.Namespace) -> int:
    from shared.engine import installer

    if installer.is_macos():
        if args.target_sm:
            raise SystemExit("macOS installs do not use --target-sm; omit it for prebuilt or Metal builds.")
        if args.from_source:
            path = installer.install_metal_from_source()
            print(f"Installed llama-server with Metal -> {path}")
            return 0
        path = installer.install_macos_prebuilt()
        print(f"Installed llama-server -> {path}")
        return 0

    from shared.system import gpu

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
