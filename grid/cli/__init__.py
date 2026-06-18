"""Grid command-line interface.

The CLI is split by command group. This package re-exports the public surface —
``main``, ``build_parser`` and every ``cmd_*`` handler — so ``grid.cli.<name>``
resolves from one place.
"""
from __future__ import annotations

# Imported so tests can monkeypatch ``grid.cli.httpx`` / ``grid.cli.time`` and
# have the patch apply to the per-group modules (they share the singletons).
import time  # noqa: F401
import httpx  # noqa: F401

from ._main import cmd_internal_media_server, cmd_internal_server, main
from .engine import (
    cmd_engine_install,
    cmd_engine_pull,
    cmd_engine_start,
    cmd_engine_status,
    cmd_engine_stop,
)
from .grid import (
    cmd_down,
    cmd_info,
    cmd_ls,
    cmd_overview,
    cmd_up,
    cmd_version,
)
from .models import cmd_catalog, cmd_pull, cmd_rm
from .parser import build_parser
from .provider import cmd_engines, cmd_join, cmd_leave, cmd_models
from .request import cmd_chat, cmd_edit, cmd_image, cmd_video

__all__ = [
    "main",
    "build_parser",
    "cmd_internal_server",
    "cmd_internal_media_server",
    "cmd_overview",
    "cmd_version",
    "cmd_up",
    "cmd_down",
    "cmd_ls",
    "cmd_info",
    "cmd_join",
    "cmd_leave",
    "cmd_models",
    "cmd_engines",
    "cmd_catalog",
    "cmd_pull",
    "cmd_rm",
    "cmd_chat",
    "cmd_image",
    "cmd_edit",
    "cmd_video",
    "cmd_engine_install",
    "cmd_engine_pull",
    "cmd_engine_status",
    "cmd_engine_start",
    "cmd_engine_stop",
]
