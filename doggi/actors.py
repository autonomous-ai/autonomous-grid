"""Layer 2 (sync) — typed, task-shaped helpers.

Each helper builds the gateway payload for one task (via :mod:`doggi.payloads`)
and exposes a friendly, fully-typed ``submit`` (async-flow, returns a
:class:`~doggi.receipt.Receipt`). The shared base adds a generic ``run``
(sync-flow, polls to completion and returns the finished task). Both are thin
wrappers over :class:`~doggi.client.DoggiClient` — layer 1 does the real work.

For ``asyncio`` (``await``-based) versions of all of this, see
:mod:`doggi.aio`.

Every helper also accepts ``**extra`` keyword arguments, forwarded verbatim
into the gateway payload, so new gateway parameters work without a library
update.

Access them off a client::

    doggi = DoggiClient()
    task = doggi.t2i.run(prompt="a red bicycle", num_images=2)   # sync
    receipt = doggi.i2v.submit(image_url=url, prompt="slow zoom")  # async
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional, Sequence

from . import payloads
from .receipt import Receipt

if TYPE_CHECKING:
    from .client import DoggiClient

from .types import (
    TERMINAL_STATUSES,
    NormalizedFile,
    normalize_files,
    ImageSize,
    ImageAspectRatio,
    VideoResolution,
    VideoAspectRatio
)


class _Task:
    """Base for the sync typed helpers."""

    def __init__(self, client: "DoggiClient"):
        self._client = client

    # Subclasses provide a typed submit(...) -> Receipt. run() mirrors it.
    def submit(self, *args: Any, **kwargs: Any) -> Receipt:  # pragma: no cover
        raise NotImplementedError

    def run(
        self,
        *args: Any,
        timeout: float = 600.0,
        poll_interval: float = 2.0,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Sync flow: same arguments as :meth:`submit`, plus ``timeout`` and
        ``poll_interval``. Blocks until the generation finishes and returns the
        task object (raising on failure/timeout).
        """
        return self.submit(*args, **kwargs).wait(
            timeout=timeout, poll_interval=poll_interval
        )


class TextToImage(_Task):
    """Generate images from a text prompt (FLUX.2 Klein)."""

    def submit(
        self,
        model: str,
        prompt: str,
        *,
        image_size: ImageSize = "landscape_4_3",
        negative_prompt: Optional[str] = None,
        num_images: int = 1,
        num_inference_steps: int = 4,
        guidance_scale: float = 1.0,
        acceleration: str = "none",
        output_format: str = "jpeg",
        seed: Optional[int] = None,
        **extra: Any,
    ) -> Receipt:
        return self._client.submit(
            payloads.text_to_image(
                model,
                prompt=prompt,
                image_size=image_size,
                negative_prompt=negative_prompt,
                num_images=num_images,
                num_inference_steps=num_inference_steps,
                guidance_scale=guidance_scale,
                acceleration=acceleration,
                output_format=output_format,
                seed=seed,
                extra=extra,
            )
        )


class ImageToImage(_Task):
    """Edit a source image with a text prompt (FLUX.2 Klein)."""

    def submit(
        self,
        model: str,
        prompt: str,
        image_url: str,
        *,
        image_urls: Optional[Sequence[str]] = None,
        aspect_ratio: ImageAspectRatio = "auto",
        strength: Optional[float] = None,
        num_images: int = 1,
        num_inference_steps: int = 4,
        guidance_scale: float = 1.0,
        acceleration: str = "none",
        output_format: str = "jpeg",
        seed: Optional[int] = None,
        **extra: Any,
    ) -> Receipt:
        return self._client.submit(
            payloads.image_to_image(
                model,
                prompt=prompt,
                image_url=image_url,
                image_urls=image_urls,
                aspect_ratio=aspect_ratio,
                strength=strength,
                num_images=num_images,
                num_inference_steps=num_inference_steps,
                guidance_scale=guidance_scale,
                acceleration=acceleration,
                output_format=output_format,
                seed=seed,
                extra=extra,
            )
        )


class ImageToVideo(_Task):
    """Animate a still image into a video (Wan 2.2 Lightning)."""

    def submit(
        self,
        model: str,
        image_url: str,
        prompt: str,
        *,
        resolution: VideoResolution = "480p",
        aspect_ratio: VideoAspectRatio = "auto",
        duration: int = 5,
        negative_prompt: str = "blur, distort, and low quality",
        end_image_url: Optional[str] = None,
        cfg_scale: float = 1.0,
        **extra: Any,
    ) -> Receipt:
        return self._client.submit(
            payloads.image_to_video(
                model,
                image_url=image_url,
                prompt=prompt,
                resolution=resolution,
                aspect_ratio=aspect_ratio,
                duration=duration,
                negative_prompt=negative_prompt,
                end_image_url=end_image_url,
                cfg_scale=cfg_scale,
                extra=extra,
            )
        )


class ImageToThreeD(_Task):
    """Turn a single product photo into a 3D GLB model (Pixal3D)."""

    def submit(self, model: str, image_url: str, **extra: Any) -> Receipt:
        return self._client.submit(payloads.image_to_3d(model, image_url=image_url, extra=extra))


class BackgroundRemoval(_Task):
    """Remove the background from an image (BiRefNet)."""

    def submit(
        self,
        model,
        image_url: str,
        *,
        output_format: str = "png",
        output_mask: bool = False,
        refine_foreground: bool = True,
        **extra: Any,
    ) -> Receipt:
        return self._client.submit(
            payloads.background_removal(
                model,
                image_url=image_url,
                output_format=output_format,
                output_mask=output_mask,
                refine_foreground=refine_foreground,
                extra=extra,
            )
        )
