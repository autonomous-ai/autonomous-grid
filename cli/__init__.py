"""Grid command-line interface.

The CLI is split by command group. This package re-exports the public surface —
``main``, ``build_parser`` and every ``cmd_*`` handler — so ``cli.<name>``
resolves from one place.
"""
from __future__ import annotations

# Imported so tests can monkeypatch ``cli.httpx`` / ``cli.time`` and
# have the patch apply to the per-group modules (they share the singletons).
import time  # noqa: F401
import httpx  # noqa: F401

from ._main import cmd_internal_media_server, cmd_internal_server, main
from .auth import cmd_login, cmd_logout, cmd_sync
from .credential import cmd_credential
from .remote_grid import (
    cmd_remote_down,
    cmd_remote_info,
    cmd_remote_ls,
    cmd_remote_members,
    cmd_remote_up,
)
from .remote_price import cmd_remote_price
from .remote_project import cmd_remote_project
from .remote_task import cmd_remote_task
from .remote_request import (
    cmd_remote_chat,
    cmd_remote_edit,
    cmd_remote_image,
    cmd_remote_video,
)
from .remote_router import cmd_remote_router
from .remote_relay import cmd_remote_relay
from .agent import (
    cmd_agent_install,
    cmd_agent_status,
)
from .engine import (
    cmd_engine_install,
    cmd_engine_list,
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
from .device import cmd_device_info
from .launch import cmd_launch
from .goal import cmd_goal
from .mode import cmd_mode, cmd_use
from .models import cmd_catalog, cmd_pull, cmd_rm
from .parser import build_parser
from .provider import cmd_engines, cmd_join, cmd_leave, cmd_models
from .request import cmd_chat, cmd_edit, cmd_image, cmd_video
from .stt import cmd_stt_transcribe

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
    "cmd_device_info",
    "cmd_pull",
    "cmd_rm",
    "cmd_mode",
    "cmd_use",
    "cmd_login",
    "cmd_logout",
    "cmd_sync",
    "cmd_credential",
    "cmd_remote_up",
    "cmd_remote_down",
    "cmd_remote_ls",
    "cmd_remote_info",
    "cmd_remote_members",
    "cmd_remote_price",
    "cmd_remote_project",
    "cmd_remote_task",
    "cmd_remote_router",
    "cmd_remote_relay",
    "cmd_remote_chat",
    "cmd_remote_image",
    "cmd_remote_edit",
    "cmd_remote_video",
    "cmd_chat",
    "cmd_image",
    "cmd_edit",
    "cmd_video",
    "cmd_launch",
    "cmd_goal",
    "cmd_agent_install",
    "cmd_agent_status",
    "cmd_engine_install",
    "cmd_engine_list",
    "cmd_engine_pull",
    "cmd_engine_status",
    "cmd_engine_start",
    "cmd_engine_stop",
    "cmd_stt_transcribe",
]
