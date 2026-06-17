"""Grid command-line interface.

The CLI is split by command group (network, provider, models, media, …). This
package re-exports the public surface — ``main``, ``build_parser`` and every
``cmd_*`` handler — so ``grid.cli.<name>`` keeps resolving as before.
"""
from __future__ import annotations

# Imported so tests can monkeypatch ``grid.cli.httpx`` / ``grid.cli.time`` and
# have the patch apply to the per-group modules (they share the singletons).
import time  # noqa: F401
import httpx  # noqa: F401

from ._main import cmd_internal_media_server, cmd_internal_server, main
from .parser import build_parser
from .consumer import cmd_consumer_env
from .llama_cpp import cmd_llama_cpp_install
from .media import (
    cmd_media_install,
    cmd_media_pull,
    cmd_media_start,
    cmd_media_status,
    cmd_media_stop,
)
from .models import cmd_models_list, cmd_models_pull, cmd_models_rm
from .network import (
    cmd_network_create,
    cmd_network_list,
    cmd_network_start,
    cmd_network_status,
    cmd_network_stop,
)
from .provider import cmd_provider_list, cmd_provider_start
from .request import (
    cmd_request_chat,
    cmd_request_media_image_edit,
    cmd_request_media_image_generate,
    cmd_request_media_i2v,
)

__all__ = [
    "main",
    "build_parser",
    "cmd_internal_server",
    "cmd_internal_media_server",
    "cmd_network_create",
    "cmd_network_start",
    "cmd_network_stop",
    "cmd_network_status",
    "cmd_network_list",
    "cmd_provider_start",
    "cmd_provider_list",
    "cmd_llama_cpp_install",
    "cmd_models_list",
    "cmd_models_pull",
    "cmd_models_rm",
    "cmd_media_install",
    "cmd_media_pull",
    "cmd_media_status",
    "cmd_media_start",
    "cmd_media_stop",
    "cmd_consumer_env",
    "cmd_request_chat",
    "cmd_request_media_image_generate",
    "cmd_request_media_image_edit",
    "cmd_request_media_i2v",
]
