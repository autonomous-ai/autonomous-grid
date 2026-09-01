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

from ._main import (
    cmd_internal_allocator_node,
    cmd_internal_media_server,
    cmd_internal_server,
    main,
)
from .agent import (
    cmd_agent_install,
    cmd_agent_status,
)
from .allocator import (
    cmd_allocator_join,
    cmd_allocator_mode,
    cmd_allocator_model_remove,
    cmd_allocator_model_set,
    cmd_allocator_node_override,
    cmd_allocator_node_resume,
    cmd_allocator_node_start,
    cmd_allocator_node_status,
    cmd_allocator_node_stop,
    cmd_allocator_status,
    cmd_allocator_tick,
    cmd_allocator_token_write,
)
from .allocator_scenario import cmd_test_graduate, cmd_test_scenario
from .auth import cmd_login, cmd_logout, cmd_sync
from .device import cmd_device_info
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
from .launch import cmd_launch
from .logical_test import (
    cmd_test_compete,
    cmd_test_demo,
    cmd_test_start,
    cmd_test_status,
    cmd_test_stop,
    cmd_test_watch,
)
from .mode import cmd_mode, cmd_use
from .models import cmd_catalog, cmd_pull, cmd_rm
from .parser import build_parser
from .provider import cmd_engines, cmd_join, cmd_leave, cmd_models
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
    "cmd_test_compete",
    "cmd_device_info",
    "cmd_pull",
    "cmd_rm",
    "cmd_mode",
    "cmd_use",
    "cmd_test_start",
    "cmd_test_demo",
    "cmd_test_scenario",
    "cmd_test_graduate",
    "cmd_test_status",
    "cmd_test_stop",
    "cmd_test_watch",
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
    "cmd_remote_chat",
    "cmd_remote_image",
    "cmd_remote_edit",
    "cmd_remote_video",
    "cmd_chat",
    "cmd_image",
    "cmd_edit",
    "cmd_video",
    "cmd_launch",
    "cmd_internal_allocator_node",
    "cmd_agent_install",
    "cmd_agent_status",
    "cmd_allocator_mode",
    "cmd_allocator_join",
    "cmd_allocator_model_remove",
    "cmd_allocator_model_set",
    "cmd_allocator_node_override",
    "cmd_allocator_node_resume",
    "cmd_allocator_node_start",
    "cmd_allocator_node_status",
    "cmd_allocator_node_stop",
    "cmd_allocator_status",
    "cmd_allocator_tick",
    "cmd_allocator_token_write",
    "cmd_catalog",
    "cmd_chat",
    "cmd_device_info",
    "cmd_down",
    "cmd_edit",
    "cmd_engine_install",
    "cmd_engine_list",
    "cmd_engine_pull",
    "cmd_engine_start",
    "cmd_engine_status",
    "cmd_engine_stop",
    "cmd_stt_transcribe",
]
