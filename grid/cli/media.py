"""`grid media` commands: install, fetch, and run the local ComfyUI media runtime."""
from __future__ import annotations

import argparse

from ._constants import VALID_MEDIA_BUNDLES


def cmd_media_install(args: argparse.Namespace) -> int:
    from ..engine import comfyui

    comfyui.install()
    print("Done. Run `grid media pull image_generation` or another bundle to download model files.")
    return 0


def cmd_media_pull(args: argparse.Namespace) -> int:
    from ..models import download, media_bundles

    paths_written = media_bundles.pull_bundle(args.bundle, on_progress=download.stderr_progress)
    print(f"Downloaded {len(paths_written)} file(s) into the ComfyUI models tree.")
    return 0


def cmd_media_status(args: argparse.Namespace) -> int:
    from ..engine import comfyui
    from ..models import media_bundles

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
    from ..engine import comfyui

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
    from ..engine import comfyui

    return comfyui.stop_running()


