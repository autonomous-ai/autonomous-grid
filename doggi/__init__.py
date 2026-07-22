"""doggi — a convenient Python client for the doggi media-generation gateway

Two layers:

* **Layer 1 (generic):** :meth:`DoggiClient.submit` / :meth:`DoggiClient.run`
  take a JSON payload (naming a ``model`` and ``type``). ``submit`` returns a
  :class:`Receipt` (async flow); ``run`` polls to completion (sync flow).
* **Layer 2 (typed tasks):** ``client.t2i`` / ``i2i`` / ``i2v`` / ``model3d`` /
  ``bg`` give fully-typed helpers for each task, built on layer 1.

Quick start::

    from doggi import DoggiClient

    doggi = DoggiClient()  # key from $DOGGI_API_KEY / $MEDIA_GATEWAY_API_KEY / .env

    # sync — blocks, returns the finished task
    task = doggi.t2i.run(prompt="a red bicycle in the rain")
    print(task["result_files"][0]["file_url"])

    # async — returns a receipt you can check later
    receipt = doggi.i2v.submit(image_url=url, prompt="slow zoom in")
    ...
    if receipt.ready():
        for f in receipt.files():
            print(f["url"])

    # generic layer-1 escape hatch for any model/payload
    task = doggi.run({"model": "some-new-model", "type": "x", "prompt": "..."})
"""

from typing import TYPE_CHECKING

from .client import MEDIA_GEN_BASE_URL, DoggiClient
from .errors import (
    AuthError,
    DoggiError,
    GatewayError,
    GenerationFailedError,
    GenerationTimeoutError,
    ResultNotReadyError,
)
from .receipt import Receipt
from .types import normalize_files

if TYPE_CHECKING:  # for editors/type-checkers only
    from .aio import AsyncDoggiClient, AsyncReceipt

__version__ = "0.1.0"

__all__ = [
    "DoggiClient",
    "Receipt",
    "AsyncDoggiClient",
    "AsyncReceipt",
    "MEDIA_GEN_BASE_URL",
    "normalize_files",
    "DoggiError",
    "AuthError",
    "GatewayError",
    "GenerationFailedError",
    "GenerationTimeoutError",
    "ResultNotReadyError",
    "__version__",
]

# Lazily expose the async client so importing `doggi` never hard-requires httpx.
_LAZY = {"AsyncDoggiClient", "AsyncReceipt"}


def __getattr__(name: str):
    if name in _LAZY:
        from . import aio

        return getattr(aio, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
