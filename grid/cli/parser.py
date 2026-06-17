"""Builds the full `grid` argument parser.

The command tree lives here; each command's handler lives in the per-group
module this imports from.
"""
from __future__ import annotations

import argparse

from .. import __version__, runtime
from ._constants import (
    VALID_I2V_ASPECT_RATIOS,
    VALID_I2V_DURATIONS,
    VALID_MEDIA_BUNDLES,
)
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="grid",
        description="LAN-only, unauthenticated Grid CLI.",
    )
    parser.add_argument("--version", action="version", version=f"grid {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    network = sub.add_parser("network", help="Create and manage LAN networks")
    net_sub = network.add_subparsers(dest="subcommand", required=True)

    create = net_sub.add_parser("create", help="Create and start a LAN signaling server")
    create.add_argument("name")
    create.add_argument("--port", type=int, default=runtime.DEFAULT_PORT)
    create.add_argument("--host", default=runtime.DEFAULT_HOST)
    create.add_argument("--advertise-host", default=None)
    create.add_argument("--network-id", default=None)
    create.set_defaults(handler=cmd_network_create)

    start = net_sub.add_parser("start", help="Start a local managed signaling server")
    start.add_argument("network")
    start.set_defaults(handler=cmd_network_start)

    stop = net_sub.add_parser("stop", help="Stop a local managed signaling server")
    stop.add_argument("network")
    stop.set_defaults(handler=cmd_network_stop)

    status = net_sub.add_parser("status", help="Show network status")
    status.add_argument("network")
    status.set_defaults(handler=cmd_network_status)

    list_cmd = net_sub.add_parser("list", help="List saved networks")
    list_cmd.set_defaults(handler=cmd_network_list)

    provider = sub.add_parser("provider", help="Provider lifecycle")
    provider_sub = provider.add_subparsers(dest="subcommand", required=True)

    provider_start = provider_sub.add_parser(
        "start",
        help="Advertise an OpenAI-compatible provider endpoint and keep heartbeating",
    )
    provider_start.add_argument("--network", required=True)
    provider_start.add_argument("--model", action="append", dest="models", default=[])
    provider_start.add_argument(
        "--advertise-as",
        action="append",
        dest="advertise_as",
        default=[],
        help="Model name advertised to the signaling server. Repeat once per --model.",
    )
    provider_start.add_argument("--endpoint-url", default=None)
    provider_start.add_argument("--endpoint-port", type=int, default=8081)
    provider_start.add_argument("--llama-port", dest="endpoint_port", type=int, default=argparse.SUPPRESS)
    provider_start.add_argument("--advertise-host", default=None)
    provider_start.add_argument("--node-id", default=None)
    provider_start.add_argument("--name", default=None)
    provider_start.add_argument("--heartbeat-interval", type=float, default=15.0)
    provider_start.add_argument("--ctx-size", type=int, default=None)
    provider_start.add_argument("--n-predict", type=int, default=None)
    provider_start.add_argument("--parallel", type=int, default=None)
    provider_start.add_argument("--flash-attn", default=None)
    provider_start.add_argument("--temp", type=float, default=None)
    provider_start.add_argument("--reasoning-budget", type=int, default=None)
    provider_start.add_argument("--enable-media", action="store_true")
    provider_start.add_argument(
        "--media-bundle",
        action="append",
        dest="media_bundles",
        choices=VALID_MEDIA_BUNDLES,
        default=[],
        help="Media bundle to advertise; repeat for multiple bundles.",
    )
    provider_start.add_argument("--comfyui-port", type=int, default=8188)
    provider_start.add_argument("--media-port", type=int, default=8190)
    provider_start.set_defaults(handler=cmd_provider_start)

    provider_list = provider_sub.add_parser("list", help="List active providers on a network")
    provider_list.add_argument("--network", required=True)
    provider_list.add_argument("--model", default=None)
    provider_list.set_defaults(handler=cmd_provider_list)

    llama_cpp = sub.add_parser("llama.cpp", help="Manage the local llama.cpp engine")
    llama_cpp_sub = llama_cpp.add_subparsers(dest="subcommand", required=True)
    llama_cpp_install = llama_cpp_sub.add_parser("install", help="Install or upgrade llama-server")
    llama_cpp_install.add_argument(
        "--from-source",
        action="store_true",
        help=(
            "On Apple Silicon, build llama.cpp with Metal from source instead of using Homebrew; "
            "on Linux NVIDIA, build from source instead of using a pinned tarball."
        ),
    )
    llama_cpp_install.add_argument(
        "--target-sm",
        default=None,
        help="Linux NVIDIA only: override the detected compute capability, for example sm_86.",
    )
    llama_cpp_install.set_defaults(handler=cmd_llama_cpp_install)

    models = sub.add_parser("models", help="Manage local GGUF model files")
    models_sub = models.add_subparsers(dest="subcommand", required=True)
    models_list = models_sub.add_parser("list", help="List local models")
    models_list.add_argument("--catalog", action="store_true", help="Also print the curated model catalog.")
    models_list.set_defaults(handler=cmd_models_list)
    models_pull = models_sub.add_parser("pull", help="Download a GGUF model from Hugging Face")
    models_pull.add_argument(
        "spec",
        help="Either '<hf-repo>:<filename>' or a catalog label from `grid models list --catalog`.",
    )
    models_pull.set_defaults(handler=cmd_models_pull)
    models_rm = models_sub.add_parser("rm", help="Delete a local model file")
    models_rm.add_argument("name", help="Filename under ~/.grid/models/")
    models_rm.add_argument("--yes", action="store_true", help="Skip confirmation.")
    models_rm.set_defaults(handler=cmd_models_rm)

    media = sub.add_parser("media", help="Manage the local ComfyUI media runtime")
    media_sub = media.add_subparsers(dest="subcommand", required=True)
    media_install = media_sub.add_parser("install", help="Install ComfyUI and media runtime dependencies")
    media_install.set_defaults(handler=cmd_media_install)
    media_pull = media_sub.add_parser("pull", help="Download a media model bundle")
    media_pull.add_argument("bundle", choices=VALID_MEDIA_BUNDLES)
    media_pull.set_defaults(handler=cmd_media_pull)
    media_status = media_sub.add_parser("status", help="Show ComfyUI install and runtime status")
    media_status.add_argument("--port", type=int, default=8188)
    media_status.set_defaults(handler=cmd_media_status)
    media_start = media_sub.add_parser("start", help="Start ComfyUI")
    media_start.add_argument("--port", type=int, default=8188)
    media_start.add_argument(
        "--detach",
        action="store_true",
        help="Return after ComfyUI is ready instead of blocking on its lifetime.",
    )
    media_start.set_defaults(handler=cmd_media_start)
    media_stop = media_sub.add_parser("stop", help="Stop ComfyUI")
    media_stop.set_defaults(handler=cmd_media_stop)

    consumer = sub.add_parser("consumer", help="Consumer helpers")
    consumer_sub = consumer.add_subparsers(dest="subcommand", required=True)
    env = consumer_sub.add_parser("env", help="Print OpenAI-compatible environment variables")
    env.add_argument("--network", required=True)
    env.set_defaults(handler=cmd_consumer_env)

    request = sub.add_parser("request", help="Smoke-test requests through the LAN server")
    req_sub = request.add_subparsers(dest="subcommand", required=True)
    chat = req_sub.add_parser("chat", help="Send one chat completion request")
    chat.add_argument("--network", required=True)
    chat.add_argument("--model", required=True)
    chat.add_argument("--message", required=True)
    chat.add_argument("--timeout", type=float, default=600.0)
    chat.set_defaults(handler=cmd_request_chat)

    media_req = req_sub.add_parser("media", help="Send media requests through a LAN network")
    media_req_sub = media_req.add_subparsers(dest="media_command", required=True)
    image_gen = media_req_sub.add_parser("image-generate", help="Generate an image")
    _add_media_request_common_args(image_gen)
    image_gen.add_argument("--prompt", required=True)
    image_gen.add_argument("--width", type=int, default=720)
    image_gen.add_argument("--height", type=int, default=720)
    image_gen.add_argument("--steps", type=int, default=4)
    image_gen.set_defaults(handler=cmd_request_media_image_generate)
    image_edit = media_req_sub.add_parser("image-edit", help="Edit one to three images")
    _add_media_request_common_args(image_edit)
    image_edit.add_argument("--prompt", required=True)
    image_edit.add_argument(
        "--image",
        action="append",
        dest="input_images",
        required=True,
        help="Input image path. Repeat up to three times.",
    )
    image_edit.add_argument("--steps", type=int, default=4)
    image_edit.set_defaults(handler=cmd_request_media_image_edit)
    i2v = media_req_sub.add_parser("i2v", help="Generate a short video from an image")
    _add_media_request_common_args(i2v)
    i2v.add_argument("--prompt", required=True)
    i2v.add_argument("--image", required=True, help="Input image path.")
    i2v.add_argument("--duration", choices=VALID_I2V_DURATIONS, default="5s")
    i2v.add_argument("--aspect-ratio", choices=VALID_I2V_ASPECT_RATIOS, default="2:3")
    i2v.set_defaults(handler=cmd_request_media_i2v)

    return parser


def _add_media_request_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--network", required=True)
    parser.add_argument("--timeout", type=int, default=1800, help="Seconds to wait for the streamed result.")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory for returned media files. Defaults to ~/.grid/outputs.",
    )


