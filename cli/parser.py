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
from .mode import cmd_mode, cmd_use
from .models import cmd_catalog, cmd_ctx, cmd_pull, cmd_rm
from .provider import cmd_engines, cmd_join, cmd_leave, cmd_models
from .remote_grid import cmd_remote_members
from .remote_price import cmd_remote_price
from .remote_project import cmd_remote_project
from .remote_task import cmd_remote_task
from .remote_router import (
    MAX_ADVISORS,
    AdvisorsAction,
    cmd_remote_router,
    parse_advisor_token,
)
from .request import cmd_chat, cmd_edit, cmd_image, cmd_video


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="grid",
        description=(
            "Grid: one private OpenAI endpoint for the engines you already run. "
            "Use --local/--remote before any command to override the active mode for that one run."
        ),
    )
    # ⚠ Every global flag here must be **valueless** (`store_true`/`version`). `cli.dispatch.
    # split_forwarded` finds the subcommand as the first non-option token, so a global flag that took
    # a separate value would make that value look like the command word — `grid launch claude -- -p`
    # would stop being recognised as a passthrough and argparse would bind `-p` to the `grid`
    # positional again, which is the exact bug that function exists to prevent. A global flag that
    # needs a value must be spelled `--flag=value`, or `split_forwarded` must learn about it.
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
    _add_project(sub)
    _add_task(sub)
    _add_router(sub)
    _add_engine_setup(sub)
    _add_launch(sub)
    _add_train(sub)

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
             "omitted = every whitelisted model the credential can serve. "
             "A CLI-seat kind (e.g. claude) serves a coding CLI installed on this box "
             "and works in local mode too.",
    )
    choose.add_argument(
        "--no-browser",
        action="store_true",
        help="For `--api codex` on a headless machine: print the sign-in URL instead of opening a "
             "browser, and take the redirect URL back by paste.",
    )
    choose.add_argument(
        "--api-key",
        default=None,
        help="API key for the --api engine. Overrides env var and key store. "
             "Warning: visible in shell history; prefer exporting the env var.",
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

    seat = join.add_argument_group("CLI seat (--api claude, …)")
    seat.add_argument("--seat-port", type=int, default=None,
                      help="Loopback port for the seat server (default: the kind's own).")
    seat.add_argument("--seat-timeout", type=float, default=None,
                      help="Seconds one CLI run may take before it is abandoned (default 600).")
    seat.add_argument("--seat-concurrency", type=int, default=None,
                      help="Concurrent CLI processes (default 1 — each request is a whole process "
                           "racing one subscription's rate limit).")
    seat.add_argument("--seat-session-limit", type=int, default=None, metavar="PCT",
                      help="Stop serving once the short-window usage reaches this percent.")
    seat.add_argument("--seat-week-limit", type=int, default=None, metavar="PCT",
                      help="Stop serving once WEEKLY usage reaches this percent. Worth setting "
                           "lower than the session limit: a spent week costs days.")
    seat.add_argument("--seat-quota-ttl", type=float, default=None, metavar="SECONDS",
                      help="How long a quota reading is reused before re-probing (default 60). "
                           "0 = probe every request, which adds seconds to each one.")

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
    # `default=None`, not the `store_true` default of False: `provider._reject_remote_only_flags`
    # decides "was this flag used" with `is not None`, so a False default would reject every LOCAL
    # `grid join`. Every flag in this group defaults to None for that reason.
    remote_only.add_argument("--respawn", action="store_true", default=None,
                             help="Stop the engine already serving this grid and start a fresh one, "
                                  "instead of no-opping an identical re-join (remote only).")
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
    device_info = sub.add_parser(
        "device-info",
        help="This machine's hardware profile (CPU, memory, disk, GPU)",
    )
    device_info.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    device_info.set_defaults(handler=cmd_device_info)

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
    image.add_argument(
        "-m", "--model",
        required=True,
        help="Model to use (e.g. doggi:hunyuan-image-3-t2i).",
    )
    image.add_argument("--width", type=int, default=720)
    image.add_argument("--height", type=int, default=720)
    image.add_argument("--steps", type=int, default=4)
    _add_remote_use_flags(image)
    image.set_defaults(handler=cmd_image)

    edit = sub.add_parser("edit", help="Edit one to three images")
    _add_media_common(edit)
    edit.add_argument("prompt")
    edit.add_argument(
        "-m", "--model",
        required=True,
        help="Model to use (e.g. doggi:hunyuan-image-3-i2i).",
    )
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
    video.add_argument(
        "-m", "--model",
        required=True,
        help="Model to use (e.g. doggi:Wan-AI/Wan2.2-I2V-A14B-Lightning).",
    )
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
    logout.add_argument(
        "--force",
        action="store_true",
        help="Sign out even if a serve child on this box could not be stopped (it is still stopped "
             "first; `grid leave <grid-id>` reaps a survivor afterwards).",
    )
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


def _add_project(sub) -> None:
    """Remote-only `grid project create|list` and `grid project member …` (ADR 0033 D-a).

    Gated in local mode by dispatch (`project` is in `REMOTE_ONLY`). A project is addressed by
    **id** everywhere downstream, so `create` printing one and `list` showing them is not a
    convenience — without it, `grid task create --project <id>` has no id to be given."""
    project = sub.add_parser("project", help="Create projects and manage who is in them (remote)")
    project_sub = project.add_subparsers(dest="subcommand", required=True)

    create = project_sub.add_parser("create", help="Create (or get) one of your projects by name")
    create.add_argument("--name", required=True, help="What to call it. Unique among your own.")
    create.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    create.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    create.set_defaults(handler=cmd_remote_project)

    listing = project_sub.add_parser("list", help="List the projects you are a member of")
    listing.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    listing.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    listing.set_defaults(handler=cmd_remote_project)

    member = project_sub.add_parser("member", help="List, add and remove project members")
    member_sub = member.add_subparsers(dest="member_action", required=True)

    member_list = member_sub.add_parser("list", help="Show a project's members and their keys")
    member_list.add_argument("project_id", help="Project id from `grid project list`.")
    member_list.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    member_list.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    member_list.set_defaults(handler=cmd_remote_project)

    member_add = member_sub.add_parser("add", help="Admit a grid member to this project")
    member_add.add_argument("project_id", help="Project id from `grid project list`.")
    member_add.add_argument(
        "--email", required=True,
        help="Their address on this grid. They must have signed in to it at least once.")
    member_add.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    member_add.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    member_add.set_defaults(handler=cmd_remote_project)

    # By member key and not by email: the key is a path segment by construction, and a member's
    # `user_id` (`grid:<network>:<sub>`) is not. `grid project member list` prints it.
    member_remove = member_sub.add_parser("remove", help="Remove someone from this project")
    member_remove.add_argument("project_id", help="Project id from `grid project list`.")
    member_remove.add_argument("member_key", help="Member key from `grid project member list`.")
    member_remove.add_argument("--grid", default=None,
                               help="Grid to act on (default: active grid).")
    member_remove.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    member_remove.set_defaults(handler=cmd_remote_project)

    # `wip reset` — the one way out of a WIP branch left ahead of the task branch it settled from
    # (ADR 0033 D-c). Nothing else moves one backwards: members never push it, promote writes only
    # `main`, and there is no revert — so without this the member's NEXT task is silently cut from
    # a lost attempt's work.
    wip = project_sub.add_parser(
        "wip", help="Work on a member's WIP branch — the ref their tasks are cut from")
    wip_sub = wip.add_subparsers(dest="wip_action", required=True)

    wip_reset = wip_sub.add_parser(
        "reset", help="Move a member's WIP branch back to a commit (recovers a lost attempt)")
    wip_reset.add_argument("project_id", help="Project id from `grid project list`.")
    wip_reset.add_argument("member_key", help="Member key from `grid project member list`.")
    wip_reset.add_argument(
        "--commit", required=True,
        help="Where to put the branch. `grid task get <id>` prints the `base_commit` a task was "
             "cut from, which is usually the commit you want.")
    wip_reset.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    wip_reset.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    wip_reset.set_defaults(handler=cmd_remote_project)

    # `promote` — the only thing that moves `main` (ADR 0033 D-b). Its two consequences are in the
    # help text and not only in the ADR, because the person typing this is the last one who can
    # decide they are acceptable, and they can only decide it if they are told.
    promote = project_sub.add_parser(
        "promote",
        help="Advance the project's main to a member's WIP branch (fast-forward only)",
        description=(
            "Advance the project's `main` to a member's WIP branch, fast-forward only.\n\n"
            "`main` is the release branch: no task ever moves it, and this is the one thing that "
            "does. Any member may promote any member's branch, including someone who has left the "
            "team — nothing else can move their branch once they are gone.\n\n"
            "Two things to know before you run it. Code an agent wrote reaches `main` if you "
            "promote without reviewing it. And a promote cannot be undone by pushing, so there is "
            "no revert for it in this release — the commit it replaced is printed, and putting it "
            "back is an operation on the relay itself.\n\n"
            "A branch that is behind `main` is refused, saying how far behind: integrate `main` "
            "into it first, then promote."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    promote.add_argument("project_id", help="Project id from `grid project list`.")
    promote.add_argument(
        "member_key",
        help="Whose WIP branch to promote. Member key from `grid project member list`.")
    promote.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    promote.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    promote.set_defaults(handler=cmd_remote_project)

    # `integrate` — the counterpart to promote, and what makes promote survivable at all (ADR 0033
    # D-d/D-e). It takes NO member key: the relay holds the caller's own task slot while it works,
    # so the branch it moves has to be the caller's own.
    integrate = project_sub.add_parser(
        "integrate",
        help="Bring the project's main into your own WIP branch",
        description=(
            "Bring the project's `main` into your own WIP branch.\n\n"
            "`main` moves only when somebody promotes, so the first promote leaves everyone else "
            "unable to promote at all — their branch was cut from a trunk that is now history. "
            "Integrating is the way back, and it is what you run before promoting again.\n\n"
            "It is always YOUR branch, so there is no member key to give. The relay holds your one "
            "task slot while it works, which is what stops an integration moving the branch a task "
            "of yours is running on — so it is refused while you have a task in flight, and the "
            "refusal names that task.\n\n"
            "Four things can happen: your branch already has everything on `main`; it moves "
            "straight onto `main`; the two are merged and a merge commit is made on your branch; "
            "or — if you and somebody else changed the same lines, which git cannot merge on its "
            "own — the grid queues a MERGE TASK whose agent resolves the conflict.\n\n"
            "A merge task costs an agent run and holds your one task slot while it works, so "
            "nothing has moved when this command returns. Watch it with `grid task follow`, then "
            "promote.\n\n"
            "What the grid checks is that the merge HAPPENED — the result really contains `main`. "
            "It cannot check that the resolution is right, so read it before you promote."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    integrate.add_argument("project_id", help="Project id from `grid project list`.")
    integrate.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    integrate.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    integrate.set_defaults(handler=cmd_remote_project)

    # `status` and `check` — the two questions that used to need a WRITE to answer (ADR 0033 D-l,
    # issue 19a). Both are pure reads: neither moves a ref, neither takes the caller's task slot.
    status = project_sub.add_parser(
        "status",
        help="Where the project is: your branch, how far it is from main, what holds your slot",
        description=(
            "Where the project is, from your side.\n\n"
            "Two of these were answerable before only by attempting something: how far behind your "
            "branch is (attempt a promote and read the refusal) and what is holding your one task "
            "slot (attempt a create and read the refusal). Both are reads now, and neither costs "
            "anything.\n\n"
            "It is also how an application notices the project changed without running `git fetch`: "
            "`main` moves on a promote or an import, and each member's branch moves when their work "
            "settles, integrates, or is committed."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    status.add_argument("project_id", help="Project id from `grid project list`.")
    status.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    status.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    status.set_defaults(handler=cmd_remote_project)

    check = project_sub.add_parser(
        "check",
        help="Ask whether integrating would conflict, without integrating",
        description=(
            "Ask what `grid project integrate` would do, without doing it.\n\n"
            "Integration IS the conflict check without this command: asking costs your one task "
            "slot, and when the answer is that you and somebody else changed the same lines it "
            "queues a merge task — a paid agent run.\n\n"
            "This spends neither. It moves no ref, creates no task, and holds no slot — so it "
            "answers even while you already have a task in flight, which is exactly when "
            "`grid project integrate` refuses you.\n\n"
            "It reports one of four answers, the same four integrate itself reports: already up to "
            "date, a straight fast-forward, a clean merge, or a conflict that would need an agent."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    check.add_argument("project_id", help="Project id from `grid project list`.")
    check.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    check.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    check.set_defaults(handler=cmd_remote_project)

    # `commit` — a change goes in without an agent (ADR 0033 D-j, issue 20). Like integrate it takes
    # NO member key: the relay holds the caller's own task slot while it writes.
    committer = project_sub.add_parser(
        "commit",
        help="Put files into the project without running an agent",
        description=(
            "Commit files onto your own WIP branch, with no agent and no provider.\n\n"
            "This is the answer to 'the agent got it 90% right, let me fix the last line'. The "
            "alternative is `grid task create --file`, which spends your one task slot and then "
            "runs an agent that may change the very line you are fixing.\n\n"
            "It is always YOUR branch, so there is no member key to give. You still cannot push to "
            "the project — the write goes through the grid, lands on exactly one ref, and holds "
            "your one task slot while it does. So it is refused while you have a task in flight, "
            "and the refusal names that task.\n\n"
            "Executable bits look after themselves. Editing a file the project already has as "
            "executable keeps it executable, and a local file that is executable is committed that "
            "way. (Removing an executable bit is not expressible here.)\n\n"
            "--delete takes a path already in your branch. A path that is not there is REFUSED "
            "rather than quietly ignored, because git's own answer to deleting a file that does "
            "not exist is to report success and do nothing.\n\n"
            "Nothing reaches `main`: promote is still what releases work."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    committer.add_argument("project_id", help="Project id from `grid project list`.")
    committer.add_argument(
        "-m", "--message", required=True, metavar="MSG",
        help="What this commit did. Required, like git's own.")
    committer.add_argument(
        "--file", action="append", metavar="LOCAL[:DEST]", default=None,
        help="A file to write, repeatable. DEST defaults to the file's name. Same form as "
             "`grid task create --file`.")
    committer.add_argument(
        "--delete", action="append", metavar="PATH", default=None,
        help="A path in your branch to remove, repeatable. Refused if it is not there.")
    committer.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    committer.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    committer.set_defaults(handler=cmd_remote_project)

    # `import` — how a project that has no commits gets a trunk at all (ADR 0033 D-f, issue 16b).
    # Since this slice a member cannot push `main` themselves, so this is the only way in.
    importer = project_sub.add_parser(
        "import",
        help="Import an existing repository into an empty project",
        description=(
            "Import an existing repository, with its history, into a project that has no `main` "
            "yet.\n\n"
            "This is the only way a project gets a trunk. The relay is `main`'s sole writer — "
            "promote moves it afterwards, and nothing else does — so a first `git push` of `main` "
            "is refused.\n\n"
            "What happens: the repository is pushed to a staging ref only you can see, the relay "
            "reads EVERY tree its history reaches, and only then does it become `main`. The "
            "reading is the slow part and it is why this command waits — on a 29,000-commit "
            "repository it is about twenty seconds.\n\n"
            "It is refused if the repository contains a submodule (a task's provider has no "
            "credential to fetch one), a path under `.grid/`, or a symlink pointing outside the "
            "repository. Symlinks that stay inside are fine, and so is `.claude/`. A repository "
            "using Git LFS imports with a warning: an agent will see pointer files, not content.\n\n"
            "A refused import leaves the project with NO trunk, on purpose — half a trunk would be "
            "worse. Fix what it names and import again, or import into a fresh project.\n\n"
            "Import brings a repository into an EMPTY project. A project that already has a `main` "
            "is refused, because a second import would move the trunk out from under every "
            "member's branch and nothing could integrate back."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    importer.add_argument("path", help="Path to the local git repository to import.")
    importer.add_argument("project_id", help="Project id from `grid project list`.")
    importer.add_argument(
        "--branch", default="HEAD",
        help="Which local ref to import (default: HEAD, i.e. whatever is checked out).")
    importer.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    importer.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    importer.set_defaults(handler=cmd_remote_project)


def _add_task(sub) -> None:
    """Remote-only `grid task create|get` — hand the grid a coding task, read the result back (ADR 0032).

    Gated in local mode by dispatch (`task` is in `REMOTE_ONLY`). `--grid` is a FLAG, not a leading
    positional: the prompt that follows is free-form, and an optional positional in front of it is
    ambiguous — the same call `router set-advisors` made."""
    task = sub.add_parser("task", help="Create and read distributed tasks (remote)")
    task_sub = task.add_subparsers(dest="subcommand", required=True)

    create = task_sub.add_parser("create", help="Hand the grid a task and queue it for a provider")
    create.add_argument("--prompt", required=True, help="What the agent should do.")
    create.add_argument(
        "--project", default=None, metavar="ID",
        help="Project ID to run in, from `grid project list` (default: your own project named "
             "'default', created on first use). One task runs per project at a time.")
    create.add_argument(
        "--file", action="append", default=None, metavar="LOCAL[:DEST]",
        help="File to upload with the task; repeatable. Committed with the task before any "
             "provider can claim it, so the agent always finds it. Placed at the file's own name "
             "unless you give a destination (e.g. ./conf.toml:config/conf.toml).")
    create.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    create.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    create.set_defaults(handler=cmd_remote_task)

    get = task_sub.add_parser("get", help="Show a task's state and result")
    get.add_argument("task_id", help="Task id returned by `grid task create`.")
    get.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    get.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    get.set_defaults(handler=cmd_remote_task)

    # A separate verb rather than `get --follow`: `get` answers "where did it get to" in one shot,
    # `follow` holds a stream open and owns a cursor. One flag flipping between those two would
    # change the output shape wholesale.
    follow = task_sub.add_parser("follow", help="Watch a task's output as it runs")
    follow.add_argument("task_id", help="Task id returned by `grid task create`.")
    follow.add_argument(
        "--after-seq", type=int, default=-1, dest="after_seq",
        help="Resume after this event sequence number (default: -1, from the start).")
    follow.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    follow.add_argument("--json", action="store_true", help="Emit one JSON event per line.")
    follow.set_defaults(handler=cmd_remote_task)

    fetch = task_sub.add_parser("fetch", help="Fetch a finished task's result into a directory")
    fetch.add_argument("task_id", help="Task id returned by `grid task create`.")
    fetch.add_argument(
        "--into", default=None, metavar="DIR",
        help="Where to put the result (default: ./<task-id>). Created if missing; an existing "
             "directory is added to, never reset.")
    fetch.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    fetch.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    fetch.set_defaults(handler=cmd_remote_task)

    # `list` — nothing listed tasks at all before ADR 0033 issue 19a. `grid task get` answers one id
    # at a time, and the id came from `grid task create`, so somebody who closed their terminal had
    # to clone the project and read `task/*` refs by hand.
    listing = task_sub.add_parser("list", help="List the tasks in a project")
    listing.add_argument(
        "--project", required=True, metavar="ID",
        help="Project ID to list, from `grid project list`.")
    listing.add_argument(
        "--all", action="store_true",
        help="Every member's tasks, not only your own. A project is shared, so this is how a team "
             "sees what the team ran.")
    listing.add_argument(
        "--state", action="append", default=None, metavar="STATE",
        help="Only tasks in this state (queued, running, completed, failed, timed_out). "
             "Repeatable.")
    listing.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="How many to show (default 50, maximum 200).")
    listing.add_argument(
        "--after", default=None, metavar="TASK_ID",
        help="Continue from a previous page — the task id the last page printed.")
    listing.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    listing.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    listing.set_defaults(handler=cmd_remote_task)


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
def _add_launch(sub) -> None:
    launch = sub.add_parser(
        "launch",
        help="Start an app on your grid (claude)",
        # The separator is invisible in a usage line, and the one thing it cannot carry is invisible
        # everywhere — so both are spelled out here, which is the only place a user goes looking.
        epilog=(
            "Anything after `--` is passed to the app unchanged:\n"
            "  grid launch claude -- --continue\n"
            "  grid launch claude -- -p 'summarise this repo'\n"
            "\n"
            "Two words are the exception: `--local` and `--remote` are this CLI's one-shot mode\n"
            "override and are removed from anywhere on the command line, so they cannot be\n"
            "forwarded to the app — a warning says so when one is taken. `-- --local` also\n"
            "switches this command to local mode, where it refuses, so it is reported as a mode\n"
            "error rather than as a missing argument."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Two optional positionals, `target` first: argparse fills them left to right, so
    # `grid launch claude` is target=claude/grid=None and `grid launch claude team` names both.
    # `grid` matches info/models/engines verbatim — a name or id, omitted for the active grid.
    launch.add_argument("target", nargs="?", default=None,
                        help="Launch target. Omit to list what can be launched.")
    launch.add_argument("grid", nargs="?", default=None,
                        help="Grid name or id (ag-…). Omit for the active grid.")
    launch.add_argument(
        "--print-env",
        action="store_true",
        help="Print the environment as shell exports instead of starting the app. "
             "Prints your grid's access token.",
    )
    # `forward` is never parsed from here — `cli.dispatch.split_forwarded` takes everything after the
    # first `--` out of argv before this parser runs (argparse would bind the app's own flags to the
    # two positionals above). The default is what makes `grid launch claude --`, with nothing after
    # it, identical to no `--` at all.
    launch.set_defaults(handler=cmd_launch, forward=())


def _add_train(sub) -> None:
    from .train import (
        cmd_train_autopilot,
        cmd_train_collect,
        cmd_train_convert,
        cmd_train_deploy,
        cmd_train_doctor,
        cmd_train_eval,
        cmd_train_init,
        cmd_train_nightly,
        cmd_train_outcomes,
        cmd_train_packs,
        cmd_train_pull,
        cmd_train_run,
        cmd_train_schedule,
        cmd_train_serve,
        cmd_train_sft,
        cmd_train_ui,
        cmd_train_web,
        cmd_train_where,
    )

    train = sub.add_parser("train", help="RL fine-tuning served by your grid (ADR 0019)")
    train_sub = train.add_subparsers(dest="subcommand", required=True)

    init = train_sub.add_parser("init", help="Write a starter grid-train.toml (or install a pack)")
    init.add_argument("--config", default=None, help="Path to write (default: ./grid-train.toml)")
    init.add_argument("--force", action="store_true", help="Overwrite an existing file.")
    init.add_argument(
        "--pack",
        default=None,
        help="Install a task pack instead (see `grid train packs`), e.g. support-replies.",
    )
    init.add_argument("--dest", default=None, help="Directory for --pack (default: ./<pack>/)")
    init.set_defaults(handler=cmd_train_init)

    packs = train_sub.add_parser("packs", help="List bundled task packs for business data")
    packs.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    packs.set_defaults(handler=cmd_train_packs)

    ui = train_sub.add_parser("ui", help="Local dashboard: runs, reward/eval curves (read-only)")
    ui.add_argument("--port", type=int, default=8321)
    ui.set_defaults(handler=cmd_train_ui)

    web = train_sub.add_parser(
        "web", help="Open the point-and-click interface (for non-engineers)"
    )
    web.add_argument("--port", type=int, default=8322)
    web.add_argument("--host", default="127.0.0.1",
                     help="0.0.0.0 to let colleagues on your network use it — it then prints a "
                          "link with a code in it, and only that link works.")
    web.set_defaults(handler=cmd_train_web)

    serve = train_sub.add_parser(
        "serve", help="Run this Mac as a rollout node (MLX; serves the training contract)"
    )
    serve.add_argument("--model", default="mlx-community/SmolLM2-135M-Instruct")
    serve.add_argument("--adapter-path", default=None, help="LoRA adapter dir (mlx_lm format)")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8080)
    serve.set_defaults(handler=cmd_train_serve)

    convert = train_sub.add_parser(
        "convert-adapter", help="Convert a LoRA adapter between torch/peft and MLX formats"
    )
    convert.add_argument("source", help="Adapter directory to read")
    convert.add_argument("dest", help="Directory to write")
    convert.add_argument("--to", choices=("mlx", "peft"), default=None,
                         help="Target format (default: the one the source is not)")
    convert.set_defaults(handler=cmd_train_convert)

    doctor = train_sub.add_parser(
        "doctor", help="Readiness check: deps, rollout endpoint, data/rewards"
    )
    doctor.add_argument("--config", default=None, help="Run config (default: ./grid-train.toml)")
    doctor.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    doctor.set_defaults(handler=cmd_train_doctor)

    run = train_sub.add_parser("run", help="Run the training climb (GRPO)")
    run.add_argument("--config", default=None, help="Run config (default: ./grid-train.toml)")
    # Same flag the imitation stage has taken all along. Its absence here is what left Apple
    # Silicon able to do stage one and not stage two, with a working MLX GRPO loop in the tree.
    run.add_argument("--backend", choices=("auto", "mlx", "torch"), default="auto",
                     help="auto picks MLX on Apple Silicon, torch elsewhere.")
    run.add_argument("--steps", type=int, default=None, help="Override [trainer].steps.")
    run.set_defaults(handler=cmd_train_run)

    collect = train_sub.add_parser(
        "collect", help="Learn from the work the grid already does (off until you turn it on)"
    )
    collect.add_argument("--on", action="store_true", help="Start keeping served requests.")
    collect.add_argument("--off", action="store_true", help="Stop keeping them.")
    collect.add_argument("--teacher", action="append", default=[],
                         help="A model whose answers count as teaching examples (repeatable).")
    collect.add_argument("--retain-days", type=int, default=0, help="How long to keep them.")
    collect.add_argument("--sample", type=float, default=None,
                         help="Fraction of requests to keep (1.0 = all).")
    collect.add_argument("--no-redact", action="store_true",
                         help="Store text as-is instead of scrubbing emails/phones/cards.")
    collect.add_argument("--prune", action="store_true", help="Delete files past the window now.")
    collect.add_argument("--days", type=int, default=30, help="Window to summarise.")
    collect.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    collect.set_defaults(handler=cmd_train_collect)

    auto = train_sub.add_parser(
        "autopilot", help="Improve a model from captured work, unattended (see `schedule`)"
    )
    auto.add_argument("--config", default=None, help="Run config (default: ./grid-train.toml)")
    auto.add_argument("--days", type=int, default=30, help="How far back to draw examples from.")
    auto.add_argument("--min-examples", type=int, default=120,
                      help="Refuse to train on less than this.")
    auto.add_argument("--stage", choices=("auto", "sft", "rl"), default="auto",
                      help="auto = imitate the corrections (needs no rollout engine).")
    auto.add_argument("--no-deploy", action="store_true",
                      help="Prove it but don't serve it. (It is still loaded under a checking "
                           "name — that is the only way to score it.)")
    auto.add_argument("--ignore-host", action="store_true",
                      help="Run even on battery or while the machine is in use.")
    auto.add_argument("--history", action="store_true", help="Show recent cycles instead.")
    auto.set_defaults(handler=cmd_train_autopilot)

    sft = train_sub.add_parser(
        "sft", help="Stage one: learn from the replies your team already wrote (works on a Mac)"
    )
    sft.add_argument("--config", default=None, help="Run config (default: ./grid-train.toml)")
    sft.add_argument("--backend", choices=("auto", "mlx", "torch"), default="auto",
                     help="auto picks MLX on Apple Silicon, torch elsewhere.")
    sft.add_argument("--iters", type=int, default=None, help="Training iterations (MLX path).")
    sft.add_argument("--run-dir", default=None,
                     help="Where to write adapter/log/run.json (default: a timestamped folder).")
    sft.set_defaults(handler=cmd_train_sft)

    nightly = train_sub.add_parser(
        "nightly", help="One unattended cycle: train, prove it, ship it only if it won"
    )
    nightly.add_argument("--config", default=None, help="Run config (default: ./grid-train.toml)")
    nightly.add_argument("--no-deploy", action="store_true",
                         help="Train and prove it, but don't serve it even on a pass. (It is "
                              "still loaded under a checking name so it can be scored.)")
    nightly.add_argument("--ignore-host", action="store_true",
                         help="Train even on battery or while the machine is in use.")
    nightly.add_argument("--history", action="store_true", help="Show recent nights instead.")
    nightly.set_defaults(handler=cmd_train_nightly)

    pull = train_sub.add_parser(
        "pull", help="Pull examples straight from Zendesk or HubSpot into a local file"
    )
    pull.add_argument("source", choices=("zendesk", "hubspot"))
    pull.add_argument("--out", default=None, help="Where to write (default: <source>-export.jsonl)")
    pull.add_argument("--subdomain", default=None, help="Zendesk: the bit before .zendesk.com")
    pull.add_argument("--email", default=None, help="Zendesk: the account email for the API token")
    pull.add_argument("--max-rows", type=int, default=5000, help="Stop after this many rows.")
    pull.add_argument("--status", default="solved",
                      help="Zendesk: which tickets to take (default solved).")
    pull.set_defaults(handler=cmd_train_pull)

    outcomes = train_sub.add_parser(
        "outcomes", help="Ask the helpdesk what actually happened, and record it as feedback"
    )
    outcomes.add_argument("source", choices=("zendesk",))
    outcomes.add_argument("--subdomain", default=None, help="Zendesk: the bit before .zendesk.com")
    outcomes.add_argument("--email", default=None, help="Zendesk: the account email for the token")
    outcomes.add_argument("--days", type=int, default=7,
                          help="How far back to look for answers to judge.")
    outcomes.add_argument("--dry-run", action="store_true",
                          help="Say what it would record, and record nothing.")
    outcomes.set_defaults(handler=cmd_train_outcomes)

    schedule = train_sub.add_parser(
        "schedule", help="Run the nightly cycle automatically (launchd on macOS, systemd on Linux)"
    )
    schedule.add_argument("action", nargs="?", choices=("status", "on", "off"), default="status",
                          help="status (default), on = install it, off = remove it.")
    schedule.add_argument("--at", default="23:00", help="Time of day to run, HH:MM (default 23:00).")
    schedule.add_argument("--config", default=None, help="Run config (default: ./grid-train.toml)")
    schedule.add_argument("--name", default=None,
                          help="Label, if this machine schedules more than one model.")
    schedule.set_defaults(handler=cmd_train_schedule)

    where = train_sub.add_parser("where", help="Which grids training can use (LAN and hosted)")
    where.set_defaults(handler=cmd_train_where)

    ev = train_sub.add_parser(
        "eval", help="Score a trained model against the one you serve, on held-out work"
    )
    ev.add_argument("--run", required=True, help="Run directory under ~/.grid/artifacts/train/")
    ev.add_argument("--candidate", required=True,
                    help="The name customers use for this model (what the winner will serve as)")
    ev.add_argument("--adapter", default=None,
                    help="The trained adapter to check (default: <run>/adapter). It is loaded "
                         "under a checking name AFTER the incumbent is scored, so the comparison "
                         "is between two different models rather than one model twice.")
    ev.add_argument("--base", default=None, help="Incumbent model name (default: the config's model)")
    ev.add_argument("--config", default=None, help="Run config (default: ./grid-train.toml)")
    ev.set_defaults(handler=cmd_train_eval)

    deploy = train_sub.add_parser("deploy", help="Hot-load a trained adapter onto serving nodes")
    deploy.add_argument(
        "--gate",
        action="store_true",
        help="Refuse to deploy unless it beats the model you already serve on held-out work.",
    )
    deploy.add_argument("--run", default=None, help="Run directory for --gate (default: the adapter's parent)")
    deploy.add_argument("--adapter", required=True, help="Adapter directory (contains adapter_config.json)")
    deploy.add_argument("--node", action="append", help="Serving node /v1 root (repeatable).")
    deploy.add_argument("--name", default=None, help="Adapter name to serve under.")
    deploy.add_argument("--config", default=None, help="Fill nodes/name from a run config.")
    deploy.set_defaults(handler=cmd_train_deploy)
