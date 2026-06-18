"""Builds the full `grid` argument parser.

The command tree lives here; each command's handler lives in the per-group
module this imports from. The surface mirrors docs/cli.md.
"""
from __future__ import annotations

import argparse

import runtime
from _version import __version__
from ._constants import (
    VALID_I2V_ASPECT_RATIOS,
    VALID_I2V_DURATIONS,
    VALID_MEDIA_BUNDLES,
)
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
from .provider import cmd_engines, cmd_join, cmd_leave, cmd_models
from .request import cmd_chat, cmd_edit, cmd_image, cmd_video


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="grid",
        description="Grid: one private OpenAI endpoint for the engines you already run.",
    )
    parser.add_argument("--version", action="version", version=f"grid {__version__}")
    parser.set_defaults(handler=cmd_overview)
    sub = parser.add_subparsers(dest="command", metavar="<command>", required=False)

    version = sub.add_parser("version", help="Print the grid version")
    version.set_defaults(handler=cmd_version)

    _add_grid_lifecycle(sub)
    _add_engines(sub)
    _add_models(sub)
    _add_use(sub)
    _add_engine_setup(sub)

    return parser


def _add_grid_lifecycle(sub) -> None:
    up = sub.add_parser("up", help="Bring a grid online (creates it on first run; default: home)")
    up.add_argument("name", nargs="?", default=None)
    up.add_argument("--port", type=int, default=runtime.DEFAULT_PORT)
    up.add_argument("--host", default=runtime.DEFAULT_HOST)
    up.add_argument("--advertise-host", default=None)
    up.set_defaults(handler=cmd_up)

    down = sub.add_parser("down", help="Take a grid offline (config persists)")
    down.add_argument("name", nargs="?", default=None)
    down.set_defaults(handler=cmd_down)

    ls = sub.add_parser("ls", help="List your grids")
    ls.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    ls.set_defaults(handler=cmd_ls)

    list_alias = sub.add_parser("list", help="Alias for `grid ls`")
    list_alias.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    list_alias.set_defaults(handler=cmd_ls)

    info = sub.add_parser("info", help="Endpoint, key, and live models for a grid")
    info.add_argument("grid", nargs="?", default=None)
    info.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    info.add_argument("--env", action="store_true", help="Print OPENAI_* shell exports.")
    info.set_defaults(handler=cmd_info)


def _add_engines(sub) -> None:
    join = sub.add_parser("join", help="Join an engine to a grid")
    join.add_argument("grid", nargs="?", default=None)
    join.add_argument("--at", default=None, help="URL of an existing OpenAI-compatible engine.")
    join.add_argument("-m", "--model", action="append", dest="models", default=[])
    join.add_argument("--serve", default=None, help="Start the built-in engine for this model, then join.")
    join.add_argument("--media", action="store_true", help="Join this box as a media (ComfyUI) engine.")
    join.add_argument(
        "--bundle",
        action="append",
        dest="bundles",
        choices=VALID_MEDIA_BUNDLES,
        default=[],
        help="Media bundle to advertise; repeat for multiple bundles.",
    )
    join.add_argument("--name", default=None, help="Engine id (for `grid leave --engine <id>`).")
    join.add_argument("--all", action="store_true", help="Join every detected engine.")
    join.add_argument("--engine", default=None, help="Join only the detected engine of this kind.")
    join.add_argument(
        "--advertise-as",
        action="append",
        dest="advertise_as",
        default=[],
        help="Model name advertised to the grid. Repeat once per -m/--model.",
    )
    join.add_argument("--endpoint-port", type=int, default=8081)
    join.add_argument("--advertise-host", default=None)
    join.add_argument("--heartbeat-interval", type=float, default=15.0)
    join.add_argument("--ctx-size", type=int, default=None)
    join.add_argument("--n-predict", type=int, default=None)
    join.add_argument("--parallel", type=int, default=None)
    join.add_argument("--flash-attn", default=None)
    join.add_argument("--temp", type=float, default=None)
    join.add_argument("--reasoning-budget", type=int, default=None)
    join.add_argument("--comfyui-port", type=int, default=8188)
    join.add_argument("--media-port", type=int, default=8190)
    join.set_defaults(handler=cmd_join)

    leave = sub.add_parser("leave", help="Stop and unregister engines from a grid")
    leave.add_argument("grid", nargs="?", default=None)
    leave.add_argument("--engine", default=None, help="Engine id to leave.")
    leave.add_argument("--all", action="store_true", help="Leave every engine on this grid.")
    leave.set_defaults(handler=cmd_leave)

    models = sub.add_parser("models", help="Live models the grid can run now")
    models.add_argument("grid", nargs="?", default=None)
    models.add_argument("--verbose", action="store_true", help="Show the engine serving each model.")
    models.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    models.set_defaults(handler=cmd_models)

    engines = sub.add_parser("engines", help="Live engines joined to a grid")
    engines.add_argument("grid", nargs="?", default=None)
    engines.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    engines.set_defaults(handler=cmd_engines)


def _add_models(sub) -> None:
    catalog = sub.add_parser("catalog", help="Models Grid can pull")
    catalog.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    catalog.set_defaults(handler=cmd_catalog)

    pull = sub.add_parser("pull", help="Download a model (catalog label or '<hf-repo>:<file>')")
    pull.add_argument("model")
    pull.set_defaults(handler=cmd_pull)

    for verb, help_text in (("rm", "Delete a local model file"), ("remove", "Alias for `grid rm`")):
        rm = sub.add_parser(verb, help=help_text)
        rm.add_argument("model", help="Filename under ~/.grid/models/")
        rm.add_argument("--yes", action="store_true", help="Skip confirmation.")
        rm.set_defaults(handler=cmd_rm)


def _add_use(sub) -> None:
    chat = sub.add_parser("chat", help="Send one chat message")
    chat.add_argument("-m", "--model", required=True)
    chat.add_argument("message")
    chat.add_argument("--grid", default=None)
    chat.add_argument("--json", action="store_true", help="Print the full JSON response.")
    chat.add_argument("--timeout", type=float, default=600.0)
    chat.set_defaults(handler=cmd_chat)

    image = sub.add_parser("image", help="Generate an image")
    _add_media_common(image)
    image.add_argument("prompt")
    image.add_argument("--width", type=int, default=720)
    image.add_argument("--height", type=int, default=720)
    image.add_argument("--steps", type=int, default=4)
    image.set_defaults(handler=cmd_image)

    edit = sub.add_parser("edit", help="Edit one to three images")
    _add_media_common(edit)
    edit.add_argument("prompt")
    edit.add_argument(
        "-i",
        "--image",
        action="append",
        dest="input_images",
        required=True,
        help="Input image path. Repeat up to three times.",
    )
    edit.add_argument("--steps", type=int, default=4)
    edit.set_defaults(handler=cmd_edit)

    video = sub.add_parser("video", help="Generate a short video from an image")
    _add_media_common(video)
    video.add_argument("prompt")
    video.add_argument("-i", "--image", required=True, help="Input image path.")
    video.add_argument("--duration", choices=VALID_I2V_DURATIONS, default="5s")
    video.add_argument("--aspect-ratio", choices=VALID_I2V_ASPECT_RATIOS, default="2:3")
    video.set_defaults(handler=cmd_video)


def _add_engine_setup(sub) -> None:
    engine = sub.add_parser("engine", help="Set up the built-in engines")
    engine_sub = engine.add_subparsers(dest="subcommand", required=True)

    install = engine_sub.add_parser("install", help="Install an engine: llama.cpp (text) or comfyui (media)")
    install.add_argument("name", choices=("llama.cpp", "comfyui"))
    install.add_argument(
        "--from-source",
        action="store_true",
        help=(
            "llama.cpp only: on Apple Silicon build with Metal from source instead of Homebrew; "
            "on Linux NVIDIA build from source instead of a pinned tarball."
        ),
    )
    install.add_argument(
        "--target-sm",
        default=None,
        help="llama.cpp on Linux NVIDIA: override the detected compute capability, e.g. sm_86.",
    )
    install.set_defaults(handler=cmd_engine_install)

    pull = engine_sub.add_parser("pull", help="Download a media model bundle (comfyui)")
    pull.add_argument("bundle", choices=VALID_MEDIA_BUNDLES)
    pull.set_defaults(handler=cmd_engine_pull)

    status = engine_sub.add_parser("status", help="Show the built-in media engine (ComfyUI) status")
    status.add_argument("--port", type=int, default=8188)
    status.set_defaults(handler=cmd_engine_status)

    start = engine_sub.add_parser("start", help="Start the built-in media engine (ComfyUI)")
    start.add_argument("--port", type=int, default=8188)
    start.add_argument(
        "--detach",
        action="store_true",
        help="Return after ComfyUI is ready instead of blocking on its lifetime.",
    )
    start.set_defaults(handler=cmd_engine_start)

    stop = engine_sub.add_parser("stop", help="Stop the built-in media engine (ComfyUI)")
    stop.set_defaults(handler=cmd_engine_stop)


def _add_media_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--grid", default=None)
    parser.add_argument("--timeout", type=int, default=1800, help="Seconds to wait for the streamed result.")
    parser.add_argument(
        "-o",
        "--output-dir",
        default=None,
        help="Directory for returned media files. Defaults to ~/.grid/outputs.",
    )
