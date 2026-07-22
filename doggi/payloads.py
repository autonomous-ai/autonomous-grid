"""Pure gateway-payload builders shared by the sync and async typed helpers.

Each function maps task parameters to the exact JSON body the gateway expects
(``model`` + optional ``type`` + snake_case fields), applying the per-model
quirks (the ``image_urls`` list rule, ``duration`` as a string, the models that
take no ``type``). No I/O — both :mod:`doggi.actors` (sync) and :mod:`doggi.aio`
(async) call these so the wire format lives in exactly one place.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Sequence

from doggi.types import VideoAspectRatio, VideoResolution, ImageSize, ImageAspectRatio


def _compact(**kwargs: Any) -> Dict[str, Any]:
    """Build a dict, dropping any keys whose value is ``None``."""
    return {k: v for k, v in kwargs.items() if v is not None}


def _merge(
    model: str,
    type_: Optional[str],
    fields: Dict[str, Any],
    extra: Mapping[str, Any],
) -> Dict[str, Any]:
    body: Dict[str, Any] = {"model": model}
    if type_ is not None:
        body["type"] = type_
    body.update(fields)
    body.update(extra)  # caller-supplied passthrough wins
    return body


def text_to_image(
    model: str,
    *,
    prompt: str,
    image_size: ImageSize,
    negative_prompt: Optional[str],
    num_images: int,
    num_inference_steps: int,
    guidance_scale: float,
    acceleration: str,
    output_format: str,
    seed: Optional[int],
    extra: Mapping[str, Any],
) -> Dict[str, Any]:
    return _merge(
        model,
        "text-to-image",
        _compact(
            prompt=prompt,
            image_size=image_size,
            negative_prompt=negative_prompt,
            num_images=num_images,
            num_inference_steps=num_inference_steps,
            guidance_scale=guidance_scale,
            acceleration=acceleration,
            output_format=output_format,
            seed=seed,
        ),
        extra,
    )


def image_to_image(
    model: str,
    *,
    prompt: str,
    image_url: str,
    image_urls: Optional[Sequence[str]],
    aspect_ratio: ImageAspectRatio,
    strength: Optional[float],
    num_images: int,
    num_inference_steps: int,
    guidance_scale: float,
    acceleration: str,
    output_format: str,
    seed: Optional[int],
    extra: Mapping[str, Any],
) -> Dict[str, Any]:
    all_urls = [image_url, *(image_urls or [])]
    return _merge(
        model,
        "image-to-image",
        _compact(
            prompt=prompt,
            image_url=image_url,
            image_urls=all_urls if len(all_urls) > 1 else None,
            aspect_ratio=aspect_ratio,
            strength=strength,
            num_images=num_images,
            num_inference_steps=num_inference_steps,
            guidance_scale=guidance_scale,
            acceleration=acceleration,
            output_format=output_format,
            seed=seed,
        ),
        extra,
    )


def image_to_video(
    model: str,
    *,
    image_url: str,
    prompt: str,
    resolution: VideoResolution,
    aspect_ratio: VideoAspectRatio,
    duration: int | str,
    negative_prompt: str,
    end_image_url: Optional[str],
    cfg_scale: float,
    extra: Mapping[str, Any],
) -> Dict[str, Any]:
    return _merge(
        model,
        "image-to-video",
        _compact(
            prompt=prompt,
            image_url=image_url,
            end_image_url=end_image_url,
            duration=str(int(duration)),  # gateway expects an integer of str representation
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            negative_prompt=negative_prompt,
            cfg_scale=cfg_scale,
        ),
        extra,
    )


def image_to_3d(
    model: str,
    *, 
    image_url: str, 
    extra: Mapping[str, Any]
) -> Dict[str, Any]:
    return _merge(
        model,
        "image-to-3d", 
        _compact(image_url=image_url), 
        extra
    )


def background_removal(
    model: str,
    *,
    image_url: str,
    output_format: str,
    output_mask: bool,
    refine_foreground: bool,
    extra: Mapping[str, Any],
) -> Dict[str, Any]:
    return _merge(
       model,
        "background-removal",
        _compact(
            image_url=image_url,
            output_format=output_format,
            output_mask=output_mask,
            refine_foreground=refine_foreground,
        ),
        extra,
    )
