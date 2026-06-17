"""`grid llama.cpp` commands: install or upgrade the local llama-server engine."""
from __future__ import annotations

import argparse


def cmd_llama_cpp_install(args: argparse.Namespace) -> int:
    from ..engine import installer

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

    from ..system import gpu

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


