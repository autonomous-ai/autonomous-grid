"""Bring up the local media (ComfyUI) engine + the provider media server.

Shared by both modes so neither owns the ComfyUI bring-up: local (`cli/provider._run_engine`)
advertises the media server to the LAN grid proxy; remote (`remote/serve.py`) forwards relay media
jobs to it on loopback. It lives in `local/` (not `cli/`) precisely so `remote/serve.py` can reuse it
without a `remote → cli` back-dependency (ADR 0004 §2) — the serve loop already imports `local.runtime`.

Memory gating (`shared.media.media_gating`) picks which `comfyui:*` bundles this host has enough
VRAM / unified memory to advertise; a missing ComfyUI install or un-pulled bundle is a clean
`SystemExit` at bring-up, never a half-started engine.
"""
from __future__ import annotations

from typing import Any

from local import media_runtime, runtime


def prepare_media_engine(
    *,
    media_bundles: list[str] | None,
    comfyui_port: int,
    media_port: int,
    advertise_host: str | None,
) -> dict[str, Any]:
    """Start ComfyUI (if needed) + the provider media server, and report what it advertises.

    Returns ``{"models": [comfyui:* ...], "proc": <media server Popen>, "media_url": <advertised
    base>, "comfyui_started": bool}``. ``media_url`` is the URL the media server is *advertised* at
    (LAN-facing via ``advertise_host`` for local); the remote serve loop ignores it and forwards on
    loopback. ``comfyui_started`` tells the caller whether IT launched ComfyUI (so teardown only stops
    a ComfyUI it started, never one the operator was already running).
    """
    from shared.engine import comfyui
    from shared.media import media_gating
    from shared.models import media_bundles as bundles_mod
    from shared.system import gpu as gpu_probe
    from shared.system import host as host_probe

    if not comfyui.comfyui_dir().exists():
        raise SystemExit(
            "ComfyUI is not installed. Run `grid engine install comfyui` first, then "
            "`grid engine pull <bundle>` for each bundle you want to serve."
        )

    requested = list(media_bundles) if media_bundles else None
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
        for spec in bundles_mod.BUNDLES[gate.bundle]:
            if not bundles_mod.target_path(spec).exists():
                missing.append(f"{gate.bundle}/{spec.hf_path}")
    if missing:
        raise SystemExit(
            "ComfyUI bundles are missing files. Run `grid engine pull <bundle>` "
            "for each enabled bundle. Missing:\n  " + "\n  ".join(missing[:10])
        )

    comfyui_started = False
    if not comfyui.is_running(comfyui_port):
        cp = comfyui.start(comfyui_port)
        comfyui_started = True
        print(f"Spawned ComfyUI pid={cp.proc.pid}, log={cp.log}")
        comfyui.wait_for_ready(comfyui_port)
        print(f"ComfyUI ready on http://localhost:{comfyui_port}")

    comfyui_url = f"http://localhost:{comfyui_port}/api"
    proc = media_runtime.start_media_server(port=media_port, comfyui_url=comfyui_url)
    media_url = runtime.engine_endpoint_url(None, media_port, advertise_host).removesuffix("/v1")
    print(f"Spawned engine media server pid={proc.pid}, url={media_url}")
    advertised = [gate.advertise_as for gate in gates]
    print(f"Media enabled: advertising {advertised}")
    return {
        "models": advertised,
        "proc": proc,
        "media_url": media_url,
        "comfyui_started": comfyui_started,
    }
