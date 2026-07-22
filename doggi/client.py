"""Layer 1 — the generic client.

:class:`DoggiClient` speaks the raw doggi media-generation gateway: give it a
JSON payload (which names a ``model`` and a ``type``) and it will submit the
generation and hand you the result, either as a
:class:`~doggi.receipt.Receipt` (async flow) or by polling to completion for
you (sync flow).

Layer 2 (the typed ``t2i`` / ``i2i`` / ``i2v`` / ``model3d`` / ``bg`` helpers,
see :mod:`doggi.actors`) is built entirely on top of these two methods.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional

import requests

from .errors import AuthError, GatewayError
from .receipt import Receipt

if TYPE_CHECKING:
    from .actors import (
        BackgroundRemoval,
        ImageToImage,
        ImageToThreeD,
        ImageToVideo,
        TextToImage,
    )

MEDIA_GEN_BASE_URL = os.getenv("MEDIA_GEN_BASE_URL", "http://127.0.0.1")


class DoggiClient:
    """Client for the doggi media-generation gateway.

    Args:
        api_key: Gateway API key. If omitted, resolved from
            ``$DOGGI_API_KEY``, ``$MEDIA_GATEWAY_API_KEY``, then a ``.env`` file
            in the current directory (or ``env_file``).
        base_url: Gateway base URL. Defaults to ``$MEDIA_GATEWAY_URL`` or
            ``http://127.0.0.1``.
        request_timeout: Per-HTTP-request timeout in seconds (not the overall
            generation timeout — that's ``timeout`` on :meth:`run`/`Receipt.wait`).
        session: Optional pre-configured :class:`requests.Session`.
        env_file: Optional path to a ``.env`` file to read the key/URL from.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        request_timeout: float = 60.0,
        session: Optional[requests.Session] = None,
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
        self._session = session or requests.Session()

    # ------------------------------------------------------------------
    # Layer 1 — generic generation API
    # ------------------------------------------------------------------

    def submit(self, payload: Mapping[str, Any]) -> Receipt:
        """Submit a generation and return immediately with a :class:`Receipt`.

        This is the core primitive: every task is "POST this JSON payload to the
        gateway." The payload must include a ``model`` (and usually a ``type``)
        plus the model's parameters. The caller keeps the receipt and checks
        back later with :meth:`Receipt.ready` / :meth:`Receipt.result`, or
        blocks with :meth:`Receipt.wait`.

        Example::

            r = client.submit({"model": "flux-2-klein-t2i",
                               "type": "text-to-image", "prompt": "a cat"})
        """
        body = dict(payload)
        resp = self._request("POST", "/media/generations", json_body=body)
        request_id = resp.get("request_id") or resp.get("id")

        if not request_id:
            raise GatewayError(200, resp)

        return Receipt(
            self,
            request_id=request_id,
            model=str(body.get("model", "")),
            status=resp.get("status"),
            task=resp,
        )

    def run(
        self,
        payload: Mapping[str, Any],
        *,
        timeout: float = 600.0,
        poll_interval: float = 2.0,
    ) -> Dict[str, Any]:
        """Submit a generation and block until it finishes (sync flow).

        Equivalent to ``submit(payload).wait(...)``. Returns the finished task
        object. Raises :class:`~doggi.errors.GenerationFailedError` on failure
        and :class:`~doggi.errors.GenerationTimeoutError` if ``timeout`` elapses.
        """
        return self.submit(payload).wait(timeout=timeout, poll_interval=poll_interval)

    def get_generation(self, request_id: str) -> Dict[str, Any]:
        """Fetch the current task object for ``request_id``."""
        return self._request("GET", f"/media/generations/{request_id}")

    def receipt(self, request_id: str, *, model: str = "") -> Receipt:
        """Rebuild a :class:`Receipt` for an existing request id and refresh it.

        Handy for picking up a generation started elsewhere (e.g. a stored
        :meth:`Receipt.to_dict`).
        """
        return Receipt(self, request_id=request_id, model=model).refresh()

    # ------------------------------------------------------------------
    # Layer 2 — typed task helpers (lazy, built on layer 1)
    # ------------------------------------------------------------------

    @property
    def t2i(self) -> "TextToImage":
        """Text-to-image (FLUX.2 Klein)."""
        from .actors import TextToImage

        return TextToImage(self)

    @property
    def i2i(self) -> "ImageToImage":
        """Image-to-image editing (FLUX.2 Klein)."""
        from .actors import ImageToImage

        return ImageToImage(self)

    @property
    def i2v(self) -> "ImageToVideo":
        """Image-to-video (Wan 2.2 Lightning)."""
        from .actors import ImageToVideo

        return ImageToVideo(self)

    @property
    def model3d(self) -> "ImageToThreeD":
        """Image-to-3D model generation (Pixal3D)."""
        from .actors import ImageToThreeD

        return ImageToThreeD(self)

    @property
    def bg(self) -> "BackgroundRemoval":
        """Background removal (BiRefNet)."""
        from .actors import BackgroundRemoval

        return BackgroundRemoval(self)

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        resp = self._session.request(
            method,
            url,
            data=json.dumps(json_body) if json_body is not None else None,
            headers=headers,
            timeout=self.request_timeout,
        )
        body = _decode(resp)
        if not resp.ok:
            if resp.status_code in (401, 403):
                raise AuthError(
                    f"Gateway rejected the API key (HTTP {resp.status_code})."
                )
            raise GatewayError(resp.status_code, body)
        return body


def _decode(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.text


def _read_env_file(path: Optional[str] = None) -> Dict[str, str]:
    """Minimal ``.env`` reader (``KEY=VALUE`` lines). Returns {} if absent."""
    env_path = Path(path) if path else Path.cwd() / ".env"
    out: Dict[str, str] = {}
    try:
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            out[key.strip()] = val.strip().strip('"').strip("'")
    except OSError:
        pass
    return out
