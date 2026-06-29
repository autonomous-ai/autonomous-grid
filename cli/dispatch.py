"""Mode-aware command dispatch.

`resolve_override` pulls a one-shot `--lan`/`--cloud` out of argv (any position).
`dispatch` resolves the effective mode, stamps it on ``args.mode`` (so mode-agnostic
handlers like the overview and `grid use` can see it), and routes: LAN handlers wired
by the parser run as-is; in cloud, mode-gated commands route to a clear stub until later
slices implement real cloud handlers; and cloud-only commands (sign-in) are gated with
guidance when run in LAN mode — the mirror image of the cloud stub.

`AGNOSTIC`, `CLOUD_HANDLERS`, and `CLOUD_ONLY` must together classify *every* registered
command — a test asserts this so a future command can never silently run LAN code in cloud
mode (nor cloud code in LAN mode).
"""
from __future__ import annotations

import argparse
from typing import NoReturn

from shared import state

from . import cloud_grid, cloud_provider, cloud_request


# Commands that behave identically in both modes: local engine/model setup, plus the
# mode/selection commands and the bare overview (which branch on the mode internally).
# ``None`` is the bare `grid` invocation (no subcommand).
AGNOSTIC = frozenset({
    None,
    "version",
    "catalog",
    "pull",
    "rm",
    "remove",
    "engine",
    "mode",
    "use",
})

# Mode-gated commands: real LAN behaviour today; a clear stub in cloud until later slices
# ship the cloud handlers. NOTE: gated ``engines`` (live, networked) is distinct from the
# agnostic ``engine`` (local setup) — one keystroke apart.
GATED = (
    "up",
    "down",
    "ls",
    "list",
    "info",
    "join",
    "leave",
    "models",
    "engines",
    "chat",
    "image",
    "edit",
    "video",
)


def cloud_stub(command: str | None) -> NoReturn:
    raise SystemExit(
        f"`grid {command}` isn't available in cloud mode yet. Run `grid mode lan` (or pass "
        "--lan) to use it on your LAN grid."
    )


# command -> cloud handler. Gated commands without a real handler yet map to the stub; the
# lifecycle verbs override it with their cloud_grid handlers (issue 04). `list` is the `ls` alias
# (GATED includes it) and MUST track `ls`, or `grid list` in cloud would still report "unavailable".
# Built in one immutable expression (the stubs first, then the real handlers win on key collision).
_CLOUD_STUBS = {command: (lambda args, _c=command: cloud_stub(_c)) for command in GATED}
CLOUD_HANDLERS = {
    **_CLOUD_STUBS,
    "up": cloud_grid.cmd_cloud_up,
    "down": cloud_grid.cmd_cloud_down,
    "ls": cloud_grid.cmd_cloud_ls,
    "list": cloud_grid.cmd_cloud_ls,
    "info": cloud_grid.cmd_cloud_info,
    "join": cloud_provider.cmd_cloud_join,
    "leave": cloud_provider.cmd_cloud_leave,
    "chat": cloud_request.cmd_cloud_chat,
    "image": cloud_request.cmd_cloud_image,
    "edit": cloud_request.cmd_cloud_edit,
    "video": cloud_request.cmd_cloud_video,
}


# Cloud-only commands: they run their real handlers in cloud mode and are gated with
# guidance in LAN mode — the mirror image of ``cloud_stub`` for the GATED commands.
CLOUD_ONLY = frozenset({"login", "logout", "members", "sync"})


def lan_stub(command: str | None) -> NoReturn:
    raise SystemExit(
        f"`grid {command}` is a cloud-mode command. Run `grid mode cloud` (or pass --cloud) "
        "to sign in."
    )


def resolve_override(argv: list[str]) -> tuple[str | None, list[str]]:
    """Strip a one-shot ``--lan``/``--cloud`` from argv (any position).

    Returns ``(override, cleaned_argv)``. Passing both ``--lan`` and ``--cloud`` is an
    error. The flag is matched as a bare token anywhere — acceptable on this surface.
    """
    override: str | None = None
    cleaned: list[str] = []
    for token in argv:
        if token in ("--lan", "--cloud"):
            flag = token[2:]
            if override is not None and override != flag:
                raise SystemExit("Pass only one of --lan / --cloud.")
            override = flag
        else:
            cleaned.append(token)
    return override, cleaned


def dispatch(args: argparse.Namespace, override: str | None) -> int:
    mode = state.resolve_mode(override)
    args.mode = mode
    command = getattr(args, "command", None)
    if mode == "cloud":
        if command in CLOUD_HANDLERS:
            return CLOUD_HANDLERS[command](args) or 0
        if command not in AGNOSTIC and command not in CLOUD_ONLY:
            # Defence in depth: the classification test should already catch this, but a
            # runtime guard means an unclassified command can never silently run LAN code.
            raise SystemExit(
                f"Internal error: command {command!r} is not classified for cloud dispatch. "
                "Please file a bug."
            )
        # AGNOSTIC and CLOUD_ONLY both fall through to their real handler below.
    elif command in CLOUD_ONLY:
        # LAN mode: a cloud-only command can't run here. Must be ``elif`` — ``dispatch`` has
        # no ``else`` after the cloud block, so a bare ``if`` would fire in cloud too and
        # break login/logout there.
        lan_stub(command)
    return args.handler(args) or 0
