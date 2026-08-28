"""Memory-based gating for which media bundles a provider is willing to advertise.

The desktop's logic (`additional_services_manager.py:386-391`) keys off total
*system* RAM because Apple Silicon has unified memory. On a dGPU NVIDIA host
the right thing to check is per-GPU **VRAM**, since ComfyUI loads model
weights onto a single CUDA device.

Thresholds match common card sizes, assuming the VRAM-aware ComfyUI launch
in `engine/comfyui.py` (`--lowvram --reserve-vram 1` when max card < 32 GB):
    image_generation  - Krea 2 Turbo. The bf16 UNet is ~26 GB, so on a 24 GB
                        card it runs partitioned to RAM under --lowvram, the
                        same way image_editing and i2v already do at this
                        threshold. Heavier than the Z-Image Turbo it replaced
                        (~12 GB UNet); if generation starts OOMing on 24 GB
                        hosts, raise this gate rather than shrinking the model.
    z_image           - Z-Image Turbo, the lighter alternative on the same
                        route (~12 GB UNet). A host can serve either, or both.
    image_editing     - Qwen-Image-Edit at Q4_1 + Lightning lora fits a
                        24 GB card under --lowvram (UNet partitioned to RAM).
    i2v               - Wan2.2 14B high+low noise sequential, also fits a
                        24 GB card under --lowvram. Video activations push
                        peak VRAM hard; long clips with a coresident LLM
                        may OOM; pin ComfyUI to a free GPU if available.

If multiple GPUs are present we take the **largest** card's VRAM; ComfyUI
picks one device per workflow run.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass


# (capability label, advertised model name, min VRAM GB to enable)
@dataclass(frozen=True)
class MediaGate:
    bundle: str
    advertise_as: str
    min_vram_gb: float


# `image_generation` is advertised twice on purpose. `comfyui:image_generation` names the *task* and
# is what `ENDPOINT_MODELS` resolves a request to when it names no model — every client written
# before there was a choice keeps working. `comfyui:krea2` names the *model*, which is what a caller
# picking between models actually wants, and reads correctly beside `comfyui:z_image`. Same bundle,
# same files, same workflow: two names for one thing, not two things.
GATES: tuple[MediaGate, ...] = (
    MediaGate(bundle="image_generation", advertise_as="comfyui:image_generation", min_vram_gb=22.0),
    MediaGate(bundle="image_generation", advertise_as="comfyui:krea2",            min_vram_gb=22.0),
    MediaGate(bundle="z_image",          advertise_as="comfyui:z_image",          min_vram_gb=22.0),
    MediaGate(bundle="image_editing",    advertise_as="comfyui:image_editing",    min_vram_gb=22.0),
    MediaGate(bundle="i2v",              advertise_as="comfyui:i2v",              min_vram_gb=22.0),
)


# Media route -> the built-in (ComfyUI) model that serves it. Media has no body `model` field to
# route on the way text does; the ROUTE names the task, so this is the map from one to the other.
# ONE definition, read by every layer that needs it — the local proxy's route table, the local
# engine's own model check, and the remote serve loop's — so a fourth media task cannot be added
# to some of them and forgotten in the others.
#
# `comfyui:*` names are the built-ins only. A request may legitimately name a different model for
# the same route (an API media engine such as `doggi:*`, remote-only); use `is_builtin_model` to
# ask "is this OUR model", never `model != endpoint_model(...)` to mean "invalid".
ENDPOINT_MODELS: dict[str, str] = {
    "media/image/generate": "comfyui:image_generation",
    "media/image/edit": "comfyui:image_editing",
    "media/video/i2v": "comfyui:i2v",
}

# The other built-in models a route accepts. `ENDPOINT_MODELS` above says what a request gets when
# it names nothing; this says what else it may name. Generation has two models behind one route, so
# "is this model allowed here" stopped being "does it equal the route's default" — a question two
# separate guards were asking, and both rejected `comfyui:z_image` on the route that serves it.
ENDPOINT_ALTERNATES: dict[str, frozenset[str]] = {
    "media/image/generate": frozenset({"comfyui:krea2", "comfyui:z_image"}),
}

BUILTIN_MODELS: frozenset[str] = frozenset(ENDPOINT_MODELS.values()) | frozenset(
    model for models in ENDPOINT_ALTERNATES.values() for model in models
)


def endpoint_model(endpoint_path: str) -> str | None:
    """The built-in model serving ``endpoint_path`` (``None`` for a non-media route)."""
    return ENDPOINT_MODELS.get(endpoint_path.strip("/"))


def is_builtin_model(model: str) -> bool:
    """Whether ``model`` is one of the built-in ComfyUI media models."""
    return model in BUILTIN_MODELS


def endpoint_for_model(model: str) -> str | None:
    """The media route ``model`` is served on (``None`` if it is not a built-in media model)."""
    for endpoint, name in ENDPOINT_MODELS.items():
        if name == model or model in ENDPOINT_ALTERNATES.get(endpoint, frozenset()):
            return endpoint
    return None


def serves_endpoint(model: str, endpoint_path: str) -> bool:
    """Whether ``model`` may be asked of ``endpoint_path``.

    A built-in media model belongs to exactly one route and must be asked of that one: naming
    ``comfyui:i2v`` on the image route would otherwise be served as whatever the route means,
    because the engine-side handler dispatches on the route alone. Anything we do not recognise —
    an API media model such as ``doggi:*`` — is not ours to judge; the engine either serves it or
    the request 503s.
    """
    served_on = endpoint_for_model(model)
    return served_on is None or served_on == endpoint_path.strip("/")


def is_apple_silicon() -> bool:
    return platform.system() == "Darwin" and platform.machine() in ("arm64", "aarch64")


def select_bundles(memory_mb_per_device: list[float], requested: list[str] | None = None) -> list[MediaGate]:
    """Pick the bundles a host has enough VRAM to serve.

    Args:
        memory_mb_per_device: per-GPU VRAM totals in MB on NVIDIA hosts, or
            unified memory totals in MB on Apple Silicon.
        requested: if provided, only consider bundles listed here (i.e. a
            subset of `["image_generation", "image_editing", "i2v"]`).

    Returns:
        The list of MediaGate entries that pass the memory threshold.
    """
    if not memory_mb_per_device:
        return []
    max_gb = max(memory_mb_per_device) / 1024.0
    out: list[MediaGate] = []
    for gate in GATES:
        if requested is not None and gate.bundle not in requested:
            continue
        if max_gb + 0.5 >= gate.min_vram_gb:  # +0.5 GB tolerance for VRAM rounding
            out.append(gate)
    return out


def capability_entry() -> dict:
    """Capability features for a `comfyui:*` model: minimal stub matching
    what the desktop emitted (`endpoints: ["media"]`)."""
    return {
        "endpoints": ["media"],
        "input_modalities": [],
        "output_modalities": [],
        "features": {},
    }
