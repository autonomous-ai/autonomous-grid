"""Media model bundles for ComfyUI workflows.

Mirrors the desktop's:
    /image-generation/v2/models/download  -> Krea 2 Turbo (image generation)
    /image-editing/models/download         -> Qwen-Image-Edit-2511
    /i2v/models/download                   -> Wan2.2 I2V

Each bundle is a list of (hf_repo, hf_path, comfy_subdir) tuples. We resolve
hf_path through HuggingFace's `/resolve/main/<path>` URL, same as the
desktop's wget commands, and store under ComfyUI/models/<subdir>/.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from shared import paths
from shared.engine import comfyui as comfyui_engine
from . import download


@dataclass(frozen=True)
class FileSpec:
    hf_repo: str
    hf_path: str           # path under repo (passed to .../resolve/main/<hf_path>)
    subdir: str            # "unet" / "loras" / "text_encoders" / "vae" / "diffusion_models"
    target_name: str | None = None  # override basename, mainly to flatten when source path has dirs


# Image generation (Krea 2 Turbo, 4-step distilled). Replaced Z-Image-Turbo in the 2026-08 bump.
#
# The VAE below is byte-identical to the one IMAGE_EDITING pulls, and both resolve to the same
# models/vae/qwen_image_vae.safetensors — so a host serving both bundles downloads it once.
#
# The text encoder is Qwen3-VL-4B, NOT the Qwen3-4B that Z-Image used and NOT the Qwen2.5-VL-7B
# that IMAGE_EDITING uses. All three are distinct: ComfyUI identifies the encoder from its tensor
# shapes (`detect_te_model`), and `CLIPLoader type="krea2"` only accepts QWEN3VL_4B. They are not
# interchangeable, so a host serving generation + editing legitimately carries two encoders.
IMAGE_GENERATION = (
    FileSpec(
        "Comfy-Org/Krea-2",
        "diffusion_models/krea2_turbo_bf16.safetensors",
        "diffusion_models",
    ),
    # 4-step turbo LoRA. Third-party: Krea ship no distilled LoRA of their own, and this is the
    # file the workflow's `lora_name` refers to.
    FileSpec(
        "lvladikov/Krea2-Turbo-Distill-4step-LoRA",
        "krea2_turbo_4step_rank_64_lora_latest_comfyui.safetensors",
        "loras",
    ),
    FileSpec(
        "Comfy-Org/Krea-2",
        "text_encoders/qwen3vl_4b_bf16.safetensors",
        "text_encoders",
    ),
    FileSpec(
        "Comfy-Org/Krea-2",
        "vae/qwen_image_vae.safetensors",
        "vae",
    ),
)


# Z-Image Turbo — the model IMAGE_GENERATION used before Krea 2, kept as a second choice on the
# same route rather than removed. A caller picks it with `"model": "comfyui:z_image"`; omitting
# `model` still gets Krea 2, so nothing that worked before changes.
#
# It is a separate bundle, not a variant of IMAGE_GENERATION, because a host may want one and not
# the other: together they are ~57 GB and share nothing — different UNet, different text encoder
# (Qwen3-4B here, Qwen3-VL-4B for Krea 2), different VAE.
IMAGE_GENERATION_ZIMAGE = (
    FileSpec(
        "Comfy-Org/z_image_turbo",
        "split_files/diffusion_models/z_image_turbo_bf16.safetensors",
        "diffusion_models",
    ),
    FileSpec(
        "Comfy-Org/z_image_turbo",
        "split_files/text_encoders/qwen_3_4b.safetensors",
        "text_encoders",
    ),
    FileSpec(
        "Comfy-Org/z_image_turbo",
        "split_files/vae/ae.safetensors",
        "vae",
        target_name="z_image_vae.safetensors",
    ),
)


# Image editing (Qwen-Image-Edit-2511): desktop /image-editing/models/download
IMAGE_EDITING = (
    FileSpec(
        "unsloth/Qwen-Image-Edit-2511-GGUF",
        "qwen-image-edit-2511-Q4_1.gguf",
        "unet",
    ),
    FileSpec(
        "lightx2v/Qwen-Image-Edit-2511-Lightning",
        "Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors",
        "loras",
    ),
    FileSpec(
        "Comfy-Org/Qwen-Image_ComfyUI",
        "split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors",
        "text_encoders",
    ),
    FileSpec(
        "Comfy-Org/Qwen-Image_ComfyUI",
        "split_files/vae/qwen_image_vae.safetensors",
        "vae",
    ),
)


# I2V (Wan 2.2 14B): desktop /i2v/models/download
I2V = (
    FileSpec(
        "QuantStack/Wan2.2-I2V-A14B-GGUF",
        "HighNoise/Wan2.2-I2V-A14B-HighNoise-Q5_K_M.gguf",
        "unet",
    ),
    FileSpec(
        "QuantStack/Wan2.2-I2V-A14B-GGUF",
        "LowNoise/Wan2.2-I2V-A14B-LowNoise-Q5_K_M.gguf",
        "unet",
    ),
    FileSpec(
        "Comfy-Org/Wan_2.2_ComfyUI_Repackaged",
        "split_files/loras/wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors",
        "loras",
    ),
    FileSpec(
        "Comfy-Org/Wan_2.2_ComfyUI_Repackaged",
        "split_files/loras/wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors",
        "loras",
    ),
    FileSpec(
        "Comfy-Org/Wan_2.2_ComfyUI_Repackaged",
        "split_files/vae/wan_2.1_vae.safetensors",
        "vae",
    ),
    FileSpec(
        "Comfy-Org/Wan_2.1_ComfyUI_repackaged",
        "split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors",
        "text_encoders",
    ),
)


BUNDLES = {
    "image_generation": IMAGE_GENERATION,
    "z_image": IMAGE_GENERATION_ZIMAGE,
    "image_editing": IMAGE_EDITING,
    "i2v": I2V,
}


def comfy_models_dir() -> Path:
    return comfyui_engine.comfyui_dir() / "models"


def target_path(spec: FileSpec) -> Path:
    base = comfy_models_dir() / spec.subdir
    name = spec.target_name or Path(spec.hf_path).name
    return base / name


def bundle_is_present(name: str) -> bool:
    """True when every file of the named bundle is already downloaded to disk."""
    if name not in BUNDLES:
        raise SystemExit(f"Unknown media bundle: {name!r}. Known: {sorted(BUNDLES)}")
    return all(target_path(spec).exists() for spec in BUNDLES[name])


def present_bundles() -> list[str]:
    """The bundles fully pulled to disk, in canonical (BUNDLES) order.

    Used to default `grid join --media` (no `--bundle`) to what's actually pullable, so the happy
    path (pull one bundle → join) works instead of hard-failing on the un-pulled bundles.
    """
    return [name for name in BUNDLES if bundle_is_present(name)]


def pull_bundle(name: str, *, on_progress=None) -> list[Path]:
    """Download every file in the named bundle into the ComfyUI/models tree."""
    if name not in BUNDLES:
        raise SystemExit(f"Unknown media bundle: {name!r}. Known: {sorted(BUNDLES)}")
    if not comfyui_engine.comfyui_dir().exists():
        raise SystemExit(
            "ComfyUI is not installed. Run `grid engine install comfyui` first so the "
            "models/ subdirectories exist."
        )
    paths.ensure_all()
    out: list[Path] = []
    for spec in BUNDLES[name]:
        dest = target_path(spec)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            print(f"[skip] {dest} already present")
            out.append(dest)
            continue
        print(f"Downloading {spec.hf_repo}/{spec.hf_path} -> {dest}")
        download.download(spec.hf_repo, spec.hf_path, out=dest, on_progress=on_progress)
        out.append(dest)
    return out


# Capability advertisement names: the strings the provider advertises in
# `models=[...]` for media. Consumers route to these via the relay's
# `discover_providers(model=...)` exact match.
CAPABILITY_NAME = {
    "image_generation": "comfyui:image_generation",
    "z_image": "comfyui:z_image",
    "image_editing": "comfyui:image_editing",
    "i2v": "comfyui:i2v",
}
