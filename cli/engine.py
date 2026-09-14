"""`grid engine` commands: set up and run the built-in engines (llama.cpp, ComfyUI)."""
from __future__ import annotations

import argparse

from ._constants import VALID_MEDIA_BUNDLES


def cmd_engine_install(args: argparse.Namespace) -> int:
    version = getattr(args, "engine_version", None)
    if version and args.name != "mlx-omarchy":
        raise SystemExit("--version applies to `grid engine install mlx-omarchy` only; the others are pinned in Grid.")
    if args.name == "llama.cpp":
        return _install_llama_cpp(args)
    if args.name == "comfyui":
        from shared.engine import comfyui

        comfyui.install()
        print("Done. Now download the model files for what you want to make:")
        print("  grid engine pull image_generation     # also: z_image, image_editing, i2v")
        return 0
    if args.name == "mlx-omarchy":
        from shared.engine import mlx_omarchy

        device = mlx_omarchy.install(version)
        print(f"\n✓ {mlx_omarchy.ENGINE} {mlx_omarchy.installed_version() or ''} installed — "
              f"it uses this Mac's GPU ({device}).")
        print("\nNext:  grid join <grid> --serve mlx-community/Qwen2.5-7B-Instruct-4bit --engine mlx-omarchy")
        return 0
    raise SystemExit(
        f"Unknown engine {args.name!r}. Choose 'llama.cpp' (text), 'comfyui' (media), "
        "or 'mlx-omarchy' (text, Apple Silicon Mac running Linux)."
    )


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
        installer.install_macos_prebuilt()
        print("\n✓ Engine installed — it uses this Mac's GPU.")
        print("\nNext:  grid catalog")
        return 0

    from shared.system import apple_linux, gpu

    gpus = gpu.enumerate_gpus()
    sm_required = (args.target_sm,) if args.target_sm else tuple(item.compute_cap_sm for item in gpus)
    if sm_required:
        print(f"Detected GPUs: {', '.join(sm_required)}")

    if args.from_source:
        if not sm_required:
            raise SystemExit(
                "--from-source builds the CUDA engine, but no NVIDIA GPU was detected "
                "(nvidia-smi missing or returned nothing). Pass --target-sm <sm_XX> to override."
            )
        path = installer.install_from_source(sm_required[0])
        print(f"Installed llama-server from source (CUDA {sm_required[0]}) -> {path}")
        return 0

    # llama.cpp publishes CUDA binaries for Windows only — every Linux asset in a release is CPU,
    # Vulkan, ROCm, SYCL or OpenVINO. So an NVIDIA box gets Vulkan, which runs on the same cards
    # with no toolchain, and is told plainly how to get CUDA instead. This used to be two pinned
    # entries with PLACEHOLDER urls that could never be filled, so the command simply dead-ended.
    kind = "vulkan" if gpus else "cpu"

    # An M-series Mac booted into Linux (Omarchy M) has no nvidia-smi and so read as "no GPU" —
    # and got the CPU build on a machine whose GPU runs the same Vulkan engine through Asahi's
    # driver. Checked only once NVIDIA came back empty: no Mac carries an NVIDIA card, and the
    # ordering keeps every other Linux box on the branch it was on.
    if not gpus and apple_linux.is_apple_silicon_linux():
        return _install_llama_cpp_on_apple_linux()

    installer.install_linux_prebuilt(kind)
    if not gpus:
        print("\n✓ Engine installed — no GPU detected, so it runs on the CPU.")
        print("\nNext:  grid catalog")
        return 0

    # Answer "can I have CUDA?" here, so nobody has to go and probe their own toolchain to find
    # out. The same prober gates `--from-source`, so this can never advise a build that would
    # then refuse to start.
    ready, _ = installer.cuda_build_readiness(sm_required[0].removeprefix("sm_"))
    faster = "  (a faster CUDA build is possible: add --from-source)" if ready else ""
    print(f"\n✓ Engine installed — it uses your GPUs via Vulkan.{faster}")
    print("\nNext:  grid catalog")
    return 0


def _install_llama_cpp_on_apple_linux() -> int:
    """`grid engine install llama.cpp` on an M-series Mac running Linux (Omarchy M).

    The GPU is driven through Vulkan — Asahi's Honeykrisp driver, which Omarchy Mac installs as
    `vulkan-asahi` — so the machine gets the same `linux-vulkan-arm64` build an NVIDIA box gets,
    and is named the way a Mac is: by chip. With no driver in place the CPU build is installed
    instead and the person is told the one command that changes that, rather than the bare "no
    GPU detected" a box with no GPU gets — there IS one, and it is one package away.
    """
    from shared.engine import installer
    from shared.system import apple_linux

    _, chip = apple_linux.describe_chip()
    what = chip or "Apple Silicon"
    if apple_linux.vulkan_ready():
        installer.install_linux_prebuilt("vulkan")
        print(f"\n✓ Engine installed — it uses this Mac's GPU ({what}) via Vulkan.")
    else:
        installer.install_linux_prebuilt("cpu")
        print(f"\n✓ Engine installed — {what}, but no Vulkan driver was found, so it runs on the CPU.")
        print("  To use the GPU:  sudo pacman -S vulkan-asahi vulkan-icd-loader  and re-run this command.")
    print("\nNext:  grid catalog")
    return 0
