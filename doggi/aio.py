"""Asyncio (``await``-based) client — the async twin of the sync API.

Mirrors :class:`doggi.client.DoggiClient` and :class:`doggi.receipt.Receipt`
on top of :class:`httpx.AsyncClient`, so you can run many generations
concurrently with ``asyncio.gather`` without threads.

Example::

    import asyncio
    from doggi.aio import AsyncDoggiClient

    async def main():
        async with AsyncDoggiClient() as doggi:
            # sync-style: await the whole thing
            task = await doggi.t2i.run(prompt="a red bicycle")
            print(task["result_files"][0]["file_url"])

            # receipt-style: submit, poll later
            r = await doggi.i2v.submit(image_url=url, prompt="slow zoom")
            while not await r.ready():
                await asyncio.sleep(2)
            files = await r.files()

            # many at once
            tasks = await asyncio.gather(*[
                doggi.t2i.run(prompt=p) for p in prompts
            ])

    asyncio.run(main())

Requires ``httpx`` (``pip install doggi[async]``).
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, List, Optional, Sequence, Literal

from . import payloads
from .client import MEDIA_GEN_BASE_URL, _read_env_file
from .errors import (
    AuthError,
    GatewayError,
    GenerationFailedError,
    GenerationTimeoutError,
    ResultNotReadyError,
)
from .types import (
    TERMINAL_STATUSES, 
    NormalizedFile,
    normalize_files,
    ImageSize,
    ImageAspectRatio,
    VideoResolution,
    VideoAspectRatio
)

try:
    import httpx
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "The async client needs httpx. Install it with: pip install doggi[async]"
    ) from exc


class AsyncReceipt:
    """Async handle to a submitted generation (twin of :class:`doggi.Receipt`)."""

    def __init__(
        self,
        client: "AsyncDoggiClient",
        request_id: str,
        *,
        model: str = "",
        status: Optional[str] = None,
        task: Optional[Dict[str, Any]] = None,
    ):
        self._client = client
        self.request_id = request_id
        self.model = model
        self.status: Optional[str] = status
        self.task: Dict[str, Any] = task or {}

    # -- cached, no I/O --------------------------------------------------

    @property
    def progress(self) -> Optional[float]:
        return self.task.get("progress")

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def succeeded(self) -> bool:
        return self.status == "completed"

    # -- polling (await) -------------------------------------------------

    async def refresh(self) -> "AsyncReceipt":
        """Re-fetch the task object and update cached status in place."""
        task = await self._client.get_generation(self.request_id)
        self.task = task
        self.status = task.get("status", self.status)
        return self

    async def ready(self) -> bool:
        """Poll once; return True if the generation reached a terminal status."""
        if self.is_terminal():
            return True
        await self.refresh()
        return self.is_terminal()

    async def result(self, *, refresh: bool = True) -> Dict[str, Any]:
        """Return the finished task object (raising if not done / failed)."""
        if refresh and not self.is_terminal():
            await self.refresh()
        if not self.is_terminal():
            raise ResultNotReadyError(
                f"Generation {self.request_id} is not finished "
                f"(status: {self.status})"
            )
        if self.status != "completed":
            raise GenerationFailedError(
                self.request_id, self.status or "UNKNOWN", task=self.task
            )
        return self.task

    async def files(self, *, refresh: bool = False) -> List[NormalizedFile]:
        """Return the finished generation's output files, normalized."""
        task = await self.result(refresh=refresh)
        return normalize_files(task)

    async def wait(
        self,
        *,
        timeout: float = 600.0,
        poll_interval: float = 2.0,
    ) -> Dict[str, Any]:
        """Await until the generation finishes, then return the task object."""

        deadline = time.monotonic() + timeout

        while True:
            await self.refresh()

            if self.is_terminal():
                if self.status != "completed":
                    raise GenerationFailedError(
                        self.request_id, self.status or "UNKNOWN", task=self.task
                    )

                return self.task

            remaining = deadline - time.monotonic()

            if remaining <= 0:
                raise GenerationTimeoutError(
                    self.request_id, self.status or "UNKNOWN", timeout
                )

            await asyncio.sleep(min(poll_interval, remaining))

    # -- serialization ---------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "model": self.model,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, client: "AsyncDoggiClient", data: Dict[str, Any]) -> "AsyncReceipt":
        return cls(
            client,
            request_id=data["request_id"],
            model=data.get("model", ""),
            status=data.get("status"),
        )

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"AsyncReceipt(request_id={self.request_id!r}, model={self.model!r}, "
            f"status={self.status!r})"
        )


class AsyncDoggiClient:
    """Asyncio client for the doggi gateway (twin of :class:`doggi.DoggiClient`).

    Use as an async context manager (recommended) so the underlying
    :class:`httpx.AsyncClient` is closed cleanly::

        async with AsyncDoggiClient() as doggi:
            ...

    Or construct directly and call :meth:`aclose` when done.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        request_timeout: float = 60.0,
        client: Optional["httpx.AsyncClient"] = None,
        env_file: Optional[str] = None,
    ):
        env = _read_env_file(env_file) if env_file else {}
        self.api_key = (
            api_key
            or os.environ.get("DOGGI_API_KEY")
            or os.environ.get("MEDIA_GATEWAY_API_KEY")
            or env.get("MEDIA_GATEWAY_API_KEY")
            or _read_env_file().get("MEDIA_GATEWAY_API_KEY")
        )
        if not self.api_key:
            raise AuthError(
                "No API key. Pass api_key=..., set $DOGGI_API_KEY / "
                "$MEDIA_GATEWAY_API_KEY, or provide a .env with "
                "MEDIA_GATEWAY_API_KEY=..."
            )
        self.base_url = (
            base_url
            or os.environ.get("MEDIA_GATEWAY_URL")
            or env.get("MEDIA_GATEWAY_URL")
            or MEDIA_GEN_BASE_URL
        ).rstrip("/")
        self.request_timeout = request_timeout
        self._owns_client = client is None
        self._http = client or httpx.AsyncClient(timeout=request_timeout)

    # -- lifecycle -------------------------------------------------------

    async def aclose(self) -> None:
        """Close the underlying httpx client (only if this client created it)."""
        if self._owns_client:
            await self._http.aclose()

    async def __aenter__(self) -> "AsyncDoggiClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # -- layer 1 — generic ----------------------------------------------

    async def submit(self, payload: Dict[str, Any]) -> AsyncReceipt:
        """Submit a generation; return immediately with an :class:`AsyncReceipt`."""
        body = dict(payload)
        resp = await self._request("POST", "/media/generations", json_body=body)
        request_id = resp.get("request_id") or resp.get("id")

        if not request_id:
            raise GatewayError(200, resp)

        return AsyncReceipt(
            self,
            request_id=request_id,
            model=str(body.get("model", "")),
            status=resp.get("status"),
            task=resp,
        )

    async def run(
        self,
        payload: Dict[str, Any],
        *,
        timeout: float = 600.0,
        poll_interval: float = 2.0,
    ) -> Dict[str, Any]:
        """Submit and await completion (sync-style). Returns the finished task."""
        receipt = await self.submit(payload)
        return await receipt.wait(timeout=timeout, poll_interval=poll_interval)

    async def get_generation(self, request_id: str) -> Dict[str, Any]:
        """Fetch the current task object for ``request_id``."""
        return await self._request("GET", f"/media/generations/{request_id}")

    async def receipt(self, request_id: str, *, model: str = "") -> AsyncReceipt:
        """Rebuild and refresh an :class:`AsyncReceipt` for an existing id."""
        return await AsyncReceipt(self, request_id=request_id, model=model).refresh()

    # -- layer 2 — typed helpers ----------------------------------------

    @property
    def t2i(self) -> "AsyncTextToImage":
        return AsyncTextToImage(self)

    @property
    def i2i(self) -> "AsyncImageToImage":
        return AsyncImageToImage(self)

    @property
    def i2v(self) -> "AsyncImageToVideo":
        return AsyncImageToVideo(self)

    @property
    def model3d(self) -> "AsyncImageToThreeD":
        return AsyncImageToThreeD(self)

    @property
    def bg(self) -> "AsyncBackgroundRemoval":
        return AsyncBackgroundRemoval(self)

    # -- HTTP ------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        resp = await self._http.request(method, url, json=json_body, headers=headers)
        try:
            body: Any = resp.json()
        except ValueError:
            body = resp.text
        if resp.is_error:
            if resp.status_code in (401, 403):
                raise AuthError(
                    f"Gateway rejected the API key (HTTP {resp.status_code})."
                )
            raise GatewayError(resp.status_code, body)
        return body


# ---------------------------------------------------------------------------
# Async typed helpers (twins of doggi.actors)
# ---------------------------------------------------------------------------

class _AsyncTask:
    def __init__(self, client: AsyncDoggiClient):
        self._client = client

    async def submit(self, *args: Any, **kwargs: Any) -> AsyncReceipt:  # pragma: no cover
        raise NotImplementedError

    async def run(
        self,
        *args: Any,
        timeout: float = 600.0,
        poll_interval: float = 2.0,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Same arguments as :meth:`submit`, plus ``timeout`` / ``poll_interval``;
        awaits completion and returns the finished task.
        """
        receipt = await self.submit(*args, **kwargs)
        return await receipt.wait(timeout=timeout, poll_interval=poll_interval)


class AsyncTextToImage(_AsyncTask):
    """Generate images from a text prompt (FLUX.2 Klein)."""

    async def submit(
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
    ) -> AsyncReceipt:
        return await self._client.submit(
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


class AsyncImageToImage(_AsyncTask):
    """Edit a source image with a text prompt (FLUX.2 Klein)."""

    async def submit(
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
    ) -> AsyncReceipt:
        return await self._client.submit(
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


class AsyncImageToVideo(_AsyncTask):
    """Animate a still image into a video (Wan 2.2 Lightning)."""

    async def submit(
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
    ) -> AsyncReceipt:
        return await self._client.submit(
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


class AsyncImageToThreeD(_AsyncTask):
    """Turn a single product photo into a 3D GLB model (Pixal3D)."""

    async def submit(self, model: str, image_url: str, **extra: Any) -> AsyncReceipt:
        return await self._client.submit(
            payloads.image_to_3d(model, image_url=image_url, extra=extra)
        )


class AsyncBackgroundRemoval(_AsyncTask):
    """Remove the background from an image (BiRefNet)."""

    async def submit(
        self,
        model: str,
        image_url: str,
        *,
        output_format: str = "png",
        output_mask: bool = False,
        refine_foreground: bool = True,
        **extra: Any,
    ) -> AsyncReceipt:
        return await self._client.submit(
            payloads.background_removal(
                model,
                image_url=image_url,
                output_format=output_format,
                output_mask=output_mask,
                refine_foreground=refine_foreground,
                extra=extra,
            )
        )
