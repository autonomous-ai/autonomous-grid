"""Builds the full `grid` argument parser.

The command tree lives here; each command's handler lives in the per-group
module this imports from. The surface mirrors docs/cli.md.
"""
from __future__ import annotations

import argparse

from local import runtime
from shared._version import __version__
from ._constants import (
    VALID_I2V_ASPECT_RATIOS,
    VALID_I2V_DURATIONS,
    VALID_MEDIA_BUNDLES,
)
from .agent import cmd_agent_install, cmd_agent_status
from .auth import cmd_login, cmd_logout, cmd_sync
from .remote_grid import cmd_remote_members
from .remote_price import cmd_remote_price
from .remote_router import AdvisorsAction, MAX_ADVISORS, cmd_remote_router, parse_advisor_token
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
from .mode import cmd_mode, cmd_use
from .models import cmd_catalog, cmd_ctx, cmd_pull, cmd_rm
from .provider import cmd_engines, cmd_join, cmd_leave, cmd_models
from .request import cmd_chat, cmd_edit, cmd_image, cmd_video


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="grid",
        description=(
            "Grid: one private OpenAI endpoint for the engines you already run. "
            "Use --local/--remote before any command to override the active mode for that one run."
        ),
    )
    parser.add_argument("--version", action="version", version=f"grid {__version__}")
    parser.add_argument(
        "--json",
        action="store_true",
        help="With no command, print the overview as JSON. (For subcommands, pass --json after the command.)",
    )
    parser.set_defaults(handler=cmd_overview)
    sub = parser.add_subparsers(dest="command", metavar="<command>", required=False)

    version = sub.add_parser("version", help="Print the grid version")
    version.set_defaults(handler=cmd_version)

    _add_grid_lifecycle(sub)
    _add_engines(sub)
    _add_models(sub)
    _add_use(sub)
    _add_state(sub)
    _add_auth(sub)
    _add_members(sub)
    _add_price(sub)
    _add_router(sub)
    _add_engine_setup(sub)

    return parser


def _add_grid_lifecycle(sub) -> None:
    up = sub.add_parser("up", help="Bring a grid online (creates it on first run; default: home)")
    up.add_argument("name", nargs="?", default=None,
                    help="Grid name or id (ag-…). Omit for 'home'.")
    up.add_argument("--port", type=int, default=runtime.DEFAULT_PORT)
    up.add_argument("--host", default=runtime.DEFAULT_HOST)
    up.add_argument("--advertise-host", default=None)
    # Remote-only (local cmd_up ignores it): the network type set when `grid up` creates a remote grid.
    # default=None lets the remote handler tell an explicit value on a *start* from this create default.
    up.add_argument(
        "--type",
        choices=("permissioned-public", "permissioned-providers"),
        default=None,
        help="Remote grid network type, set on create (default permissioned-public).",
    )
    up.set_defaults(handler=cmd_up)

    down = sub.add_parser("down", help="Take a grid offline (config persists)")
    down.add_argument("name", nargs="?", default=None,
                      help="Grid name or id (ag-…). Omit for the active grid.")
    down.set_defaults(handler=cmd_down)

    ls = sub.add_parser("ls", help="List your grids")
    ls.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    ls.set_defaults(handler=cmd_ls)

    list_alias = sub.add_parser("list", help="Alias for `grid ls`")
    list_alias.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    list_alias.set_defaults(handler=cmd_ls)

    info = sub.add_parser("info", help="Endpoint, key, and live models for a grid")
    info.add_argument("grid", nargs="?", default=None,
                      help="Grid name or id (ag-…). Omit for the active grid.")
    info.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    info.add_argument("--env", action="store_true", help="Print OPENAI_* shell exports.")
    info.set_defaults(handler=cmd_info)


def _add_engines(sub) -> None:
    join = sub.add_parser("join", help="Join an engine to a grid")
    join.add_argument("grid", nargs="?", default=None,
                      help="Grid name or id (ag-…). Omit for the active grid.")

    choose = join.add_argument_group("Choose an engine")
    choose.add_argument("-m", "--model", action="append", dest="models", default=[],
                        help="A model an engine serves; pair with --at, or use --serve for the built-in.")
    choose.add_argument("--at", default=None, help="URL of an existing OpenAI-compatible engine.")
    choose.add_argument("--serve", default=None, help="Start the built-in engine for this model, then join.")
    choose.add_argument("--media", action="store_true", help="Join this box as a media (ComfyUI) engine.")
    choose.add_argument(
        "--bundle",
        action="append",
        dest="bundles",
        choices=VALID_MEDIA_BUNDLES,
        default=[],
        help="Media bundle to advertise; repeat for multiple bundles.",
    )
    choose.add_argument("--all", action="store_true", help="Join every detected engine.")
    choose.add_argument("--kind", "--engine", dest="kind", default=None,
                        help="Join only the detected engine of this kind (e.g. ollama, vllm).")
    choose.add_argument(
        "--api",
        metavar="KIND",
        default=None,
        help="Join a third-party API engine of this service kind (e.g. openai, codex). "
             "Remote only; -m optionally narrows the whitelist (see `grid catalog --api`), "
             "omitted = every whitelisted model the credential can serve.",
    )
    choose.add_argument(
        "--no-browser",
        action="store_true",
        help="For `--api codex` on a headless machine: print the sign-in URL instead of opening a "
             "browser, and take the redirect URL back by paste.",
    )

    naming = join.add_argument_group("Name & display")
    naming.add_argument("--name", default=None,
                        help="Local: engine id. Remote: display name shown on the grid page.")
    naming.add_argument(
        "--advertise-as",
        action="append",
        dest="advertise_as",
        default=[],
        help="Model name advertised to the grid. Repeat once per -m/--model.",
    )

    tuning = join.add_argument_group("Built-in tuning (--serve)")
    tuning.add_argument("--endpoint-port", "--llama-port", type=int, default=8081)
    tuning.add_argument("--heartbeat-interval", type=float, default=15.0)
    tuning.add_argument("--ctx-size", type=int, default=None)
    tuning.add_argument("--n-predict", type=int, default=None)
    tuning.add_argument("--parallel", type=int, default=None)
    tuning.add_argument("--flash-attn", default=None)
    tuning.add_argument("--temp", type=float, default=None)
    tuning.add_argument("--reasoning-budget", type=int, default=None)

    media = join.add_argument_group("Media")
    media.add_argument("--comfyui-port", type=int, default=8188)
    media.add_argument("--media-port", type=int, default=8190)

    local_only = join.add_argument_group("Local only")
    # A remote engine polls the relay outbound, so it has no inbound endpoint to advertise.
    local_only.add_argument("--advertise-host", default=None,
                            help="Host/IP to advertise this engine at (local only).")

    remote_only = join.add_argument_group("Remote only")
    # Remote-only: billing + pull-based capacity + grid-page display (rejected in local). default=None
    # so a wrong-mode use is detectable.
    remote_only.add_argument("--engine-label", default=None,
                             help="Deprecated — the grid page derives the engine kind automatically; "
                                  "no longer changes display (remote only).")
    # Deprecated: pricing now lives in the authoritative per-provider table — set it with
    # `grid price set` instead. Kept so old invocations don't hard-error; they no longer advertise a price.
    remote_only.add_argument("--pricing-input", type=float, default=None,
                             help="Deprecated — use `grid price set`. (No longer advertises a price.)")
    remote_only.add_argument("--pricing-output", type=float, default=None,
                             help="Deprecated — use `grid price set`. (No longer advertises a price.)")
    remote_only.add_argument("--max-concurrency", type=int, default=None,
                             help="How many requests this engine serves at once (remote only).")
    join.set_defaults(handler=cmd_join)

    leave = sub.add_parser("leave", help="Stop and unregister engines from a grid")
    leave.add_argument("grid", nargs="?", default=None,
                       help="Grid name or id (ag-…). Omit for the active grid.")
    leave.add_argument("--engine", default=None,
                       help="Engine to leave. Matches, in order: exact engine id, endpoint URL, a "
                            "served model, or a URL fragment (e.g. :8000).")
    leave.add_argument("--all", action="store_true",
                       help="Leave every engine on this grid. Without --engine: a one-engine grid "
                            "leaves that one; a multi-engine grid requires --all.")
    leave.set_defaults(handler=cmd_leave)

    models = sub.add_parser("models", help="Live models the grid can run now")
    models.add_argument("grid", nargs="?", default=None,
                        help="Grid name or id (ag-…). Omit for the active grid.")
    models.add_argument("--verbose", action="store_true", help="Show the engine serving each model.")
    models.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    models.set_defaults(handler=cmd_models)

    engines = sub.add_parser("engines", help="Live engines joined to a grid")
    engines.add_argument("grid", nargs="?", default=None,
                         help="Grid name or id (ag-…). Omit for the active grid.")
    engines.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    engines.set_defaults(handler=cmd_engines)


def _add_models(sub) -> None:
    catalog = sub.add_parser("catalog", help="Models Grid can pull")
    catalog.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    catalog.add_argument(
        "--api",
        metavar="KIND",
        help="Show the API-engine whitelist for a service kind (e.g. openai, codex).",
    )
    catalog.set_defaults(handler=cmd_catalog)

    pull = sub.add_parser("pull", help="Download a model (catalog label or '<hf-repo>:<file>')")
    pull.add_argument("model")
    pull.set_defaults(handler=cmd_pull)

    for verb, help_text in (("rm", "Delete a local model file"), ("remove", "Alias for `grid rm`")):
        rm = sub.add_parser(verb, help=help_text)
        rm.add_argument("model", help="Filename under ~/.grid/models/")
        rm.add_argument("--yes", action="store_true", help="Skip confirmation.")
        rm.set_defaults(handler=cmd_rm)

    ctx = sub.add_parser("ctx", help="Show a model's max context length (from GGUF metadata)")
    ctx.add_argument("model", help="Filename under ~/.grid/models/ or a path to a .gguf")
    ctx.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    ctx.set_defaults(handler=cmd_ctx)


def _add_use(sub) -> None:
    chat = sub.add_parser("chat", help="Send one chat message")
    chat.add_argument("-m", "--model", required=True)
    chat.add_argument("message")
    chat.add_argument("--grid", default=None)
    chat.add_argument("--json", action="store_true", help="Print the full JSON response.")
    chat.add_argument("--timeout", type=float, default=600.0)
    _add_remote_use_flags(chat)
    chat.set_defaults(handler=cmd_chat)

    image = sub.add_parser("image", help="Generate an image")
    _add_media_common(image)
    image.add_argument("prompt")
    image.add_argument("--width", type=int, default=720)
    image.add_argument("--height", type=int, default=720)
    image.add_argument("--steps", type=int, default=4)
    _add_remote_use_flags(image)
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
    _add_remote_use_flags(edit)
    edit.set_defaults(handler=cmd_edit)

    video = sub.add_parser("video", help="Generate a short video from an image")
    _add_media_common(video)
    video.add_argument("prompt")
    video.add_argument("-i", "--image", required=True, help="Input image path.")
    video.add_argument("--duration", choices=VALID_I2V_DURATIONS, default="5s")
    video.add_argument("--aspect-ratio", choices=VALID_I2V_ASPECT_RATIOS, default="2:3")
    _add_remote_use_flags(video)
    video.set_defaults(handler=cmd_video)


def _add_state(sub) -> None:
    mode = sub.add_parser("mode", help="Show or switch the active mode (local/remote)")
    mode.add_argument(
        "target",
        nargs="?",
        choices=("local", "remote"),
        default=None,
        help="Switch to this mode and persist it; omit to print the current mode.",
    )
    mode.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    mode.set_defaults(handler=cmd_mode)

    use = sub.add_parser("use", help="Set the active grid for the current mode")
    use.add_argument(
        "name",
        nargs="?",
        default=None,
        help="Grid to make active; omit to print the current active grid.",
    )
    use.add_argument("--none", action="store_true", help="Clear the active grid for the current mode.")
    use.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    use.set_defaults(handler=cmd_use)


def _add_auth(sub) -> None:
    login = sub.add_parser("login", help="Sign in to remote mode")
    login.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the sign-in URL and code instead of opening a browser (for headless machines).",
    )
    login.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    login.set_defaults(handler=cmd_login)

    logout = sub.add_parser("logout", help="Sign out of remote mode")
    logout.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    logout.set_defaults(handler=cmd_logout)

    sync = sub.add_parser("sync", help="Refresh your remote grids without signing in again")
    sync.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    sync.set_defaults(handler=cmd_sync)


def _add_members(sub) -> None:
    """Remote-only membership admin (DECISIONS D13): `grid members add|remove [grid] <email>` and
    `grid members list [grid]`. Gated in local mode by dispatch (`members` is in `REMOTE_ONLY`). On
    add/remove the `[grid]` positional is declared first so argparse binds a lone positional to the
    required `email`; omitting it falls back to the active grid."""
    members = sub.add_parser("members", help="Manage who may use or serve a remote grid")
    members_sub = members.add_subparsers(dest="subcommand", required=True)

    add = members_sub.add_parser("add", help="Add a member to a grid")
    add.add_argument("grid", nargs="?", default=None)
    add.add_argument("email")
    add.add_argument(
        "--role",
        choices=("consumer", "provider", "both"),
        default="both",
        help="Member role (default: both).",
    )
    add.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    add.set_defaults(handler=cmd_remote_members)

    remove = members_sub.add_parser("remove", help="Remove a member from a grid")
    remove.add_argument("grid", nargs="?", default=None)
    remove.add_argument("email")
    remove.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    remove.set_defaults(handler=cmd_remote_members)

    listing = members_sub.add_parser("list", help="List a grid's members and roles")
    listing.add_argument("grid", nargs="?", default=None)
    listing.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    listing.set_defaults(handler=cmd_remote_members)


def _add_price(sub) -> None:
    """Remote-only `grid price set|rm|show` — this engine's authoritative model price (grid_chat_pricing).
    Gated in local mode by dispatch (`price` is in `REMOTE_ONLY`). `--type` defaults to chat; image/video are
    not priced yet (the handler rejects them). `--grid` selects the grid (active grid when omitted)."""
    price = sub.add_parser("price", help="Set or remove this engine's model price (remote)")
    price_sub = price.add_subparsers(dest="subcommand", required=True)

    pset = price_sub.add_parser("set", help="Set the price for a model this engine serves")
    pset.add_argument("-m", "--model", required=True, help="Model id (as advertised to the grid).")
    pset.add_argument(
        "--type",
        choices=("chat", "image", "video"),
        default="chat",
        help="Model type (default chat). image/video pricing isn't supported yet.",
    )
    pset.add_argument("--input", type=float, required=True, help="USD per 1M input tokens.")
    pset.add_argument("--output", type=float, required=True, help="USD per 1M output tokens.")
    pset.add_argument("--cache", type=float, default=0.0, help="USD per 1M cached input tokens (default 0).")
    # Optional model metadata recorded on the same relay endpoint; each is sent only when given.
    pset.add_argument("--name", default=None, help="Display name shown on the grid page (e.g. 'Ornith 1.0 397B').")
    pset.add_argument("--maker", default=None, help="Model maker/vendor (e.g. 'DeepReinforce AI').")
    pset.add_argument("--status", default=None, help="Model status on the grid (e.g. 'available').")
    pset.add_argument("--context-length", type=int, default=None, help="Max context length in tokens.")
    pset.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    pset.set_defaults(handler=cmd_remote_price)

    for verb, help_text in (("rm", "Remove your price for a model"), ("delete", "Alias for `grid price rm`")):
        prm = price_sub.add_parser(verb, help=help_text)
        prm.add_argument("-m", "--model", required=True, help="Model id whose price to remove.")
        prm.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
        prm.set_defaults(handler=cmd_remote_price)

    show = price_sub.add_parser("show", help="Show the grid's model prices")
    show.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    show.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    show.set_defaults(handler=cmd_remote_price)


def _add_router(sub) -> None:
    """Remote-only auto-routing config for a grid you own (model `auto`, ADR 0013, revised):
    `grid router status|enable|disable [--grid <grid>]`, `grid router models`, `grid router set-advisors
    <provider[:model]> …`, and `grid router remove-advisor <provider[:model]>`. Gated in local mode by
    dispatch (`router` is in `REMOTE_ONLY`).

    An Advisor is picked BY NAME from the platform catalog — there is NO key or URL input anywhere in this
    group. Grid selection is a uniform `--grid` FLAG on every subcommand that acts on a grid (omit for the
    active grid), matching `grid price`. The flag (not a positional `[grid]`) is forced by `set-advisors`,
    whose `nargs="+"` advisor tokens make a leading positional `[grid]` ambiguous — is the first token a
    grid or an advisor?; the other subcommands adopt it too so the whole group reads one way and a
    positional-grid habit can't silently become an advisor token. `models` takes no grid at all (it reads
    the account-level catalog)."""
    router = sub.add_parser("router", help="Configure auto-routing (model `auto`) for a grid you own")
    router_sub = router.add_subparsers(dest="subcommand", required=True)

    # status / enable / disable share the same shape (`--grid` + `--json`); build them in a loop, mirroring
    # `_add_price`'s rm/delete idiom. `--grid` (not a positional) keeps the whole group's selection uniform.
    for name, help_text in (
        ("status", "Show routing state and the advisor chain (no keys)"),
        ("enable", "Enable auto-routing on the grid"),
        ("disable", "Disable auto-routing on the grid"),
    ):
        simple = router_sub.add_parser(name, help=help_text)
        simple.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
        simple.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
        simple.set_defaults(handler=cmd_remote_router)

    # `models` lists the platform advisor catalog — account-level, needs no grid (and no grid running).
    models = router_sub.add_parser(
        "models", help="List the advisor catalog (providers + whitelisted models; no grid needed)")
    models.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    models.set_defaults(handler=cmd_remote_router)

    set_advisors = router_sub.add_parser(
        "set-advisors",
        help=f"Replace the advisor chain (up to {MAX_ADVISORS} `provider[:model]`, order = priority)",
        description=(
            f"Replace the whole advisor chain with 1-{MAX_ADVISORS} `provider[:model]` tokens, in priority "
            "order. A bare `provider` uses the catalog's default model. Advisors are picked by name from the "
            "platform catalog (`grid router models`) — there is no URL or key to supply."
        ),
    )
    set_advisors.add_argument(
        "advisors", nargs="+", metavar="provider[:model]", type=parse_advisor_token, action=AdvisorsAction,
        help=f"1-{MAX_ADVISORS} advisors in priority order, e.g. `openai:gpt-5-mini openai:gpt-4o-mini`.")
    set_advisors.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    set_advisors.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    set_advisors.set_defaults(handler=cmd_remote_router)

    remove_advisor = router_sub.add_parser(
        "remove-advisor",
        help="Remove an advisor by name (exact `provider:model`, or bare `provider` for all its entries)")
    remove_advisor.add_argument(
        "advisor", metavar="provider[:model]", type=parse_advisor_token,
        help="Exact `provider:model` removes one entry; bare `provider` removes all of its entries.")
    remove_advisor.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    remove_advisor.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    remove_advisor.set_defaults(handler=cmd_remote_router)


def _add_engine_setup(sub) -> None:
    engine = sub.add_parser("engine", help="Set up built-in engines and list live ones")
    engine_sub = engine.add_subparsers(dest="subcommand", required=True)

    install = engine_sub.add_parser("install", help="Install an engine: llama.cpp (text) or comfyui (media)")
    install.add_argument("name", choices=("llama.cpp", "comfyui"))
    install.add_argument(
        "--from-source",
        action="store_true",
        help=(
            "llama.cpp only: build from source instead of downloading a pinned release "
            "(Metal on macOS, CUDA on Linux NVIDIA)."
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

    # `grid engine ls`/`list`: live engines joined to the grid (mode-aware, like `grid engines`).
    for verb, help_text in (("ls", "List live engines (like `grid engines`)"),
                            ("list", "Alias for `grid engine ls`")):
        engine_list = engine_sub.add_parser(verb, help=help_text)
        engine_list.add_argument("grid", nargs="?", default=None,
                                 help="Grid name or id (ag-…). Omit for the active grid.")
        engine_list.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
        engine_list.set_defaults(handler=cmd_engine_list)

    agent = sub.add_parser("agent", help="Set up the agents that run tools in chat (hermes, codex)")
    agent_sub = agent.add_subparsers(dest="subcommand", required=True)

    agent_install = agent_sub.add_parser("install", help="Install an agent (no Homebrew, no admin rights)")
    agent_install.add_argument("name", choices=("hermes", "codex"))
    agent_install.add_argument(
        "--force",
        action="store_true",
        help="Reinstall (or upgrade) even when the agent is already present.",
    )
    agent_install.set_defaults(handler=cmd_agent_install)

    agent_status = agent_sub.add_parser("status", help="Show whether the agent is installed")
    agent_status.set_defaults(handler=cmd_agent_status)


def _add_media_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--grid", default=None)
    parser.add_argument("--timeout", type=int, default=1800, help="Seconds to wait for the streamed result.")
    parser.add_argument(
        "-o",
        "--output-dir",
        default=None,
        help="Directory for returned media files. Defaults to ~/.grid/outputs.",
    )


def _add_remote_use_flags(parser: argparse.ArgumentParser) -> None:
    """Remote-only request-routing flags shared by chat/image/edit/video (DECISIONS D16). Declared on
    the unified parser; the local handlers reject them (cli/request.py) since the concept is remote-only.
    ``--target-provider`` defaults to ``None`` and ``--allow-self-provider`` to ``False`` so a wrong-mode
    use is detectable."""
    parser.add_argument(
        "--target-provider",
        default=None,
        help="Remote only: pin this request to a specific engine by id.",
    )
    parser.add_argument(
        "--allow-self-provider",
        action="store_true",
        help="Remote only: let your own engine serve this request.",
    )
