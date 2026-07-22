"""Mode-aware command dispatch.

`resolve_override` pulls a one-shot `--local`/`--remote` out of argv (any position).
`dispatch` resolves the effective mode, stamps it on ``args.mode`` (so mode-agnostic
handlers like the overview and `grid use` can see it), and routes: local handlers wired
by the parser run as-is; in remote mode, mode-gated commands route to a clear stub until later
slices implement real remote handlers; and remote-only commands (sign-in) are gated with
guidance when run in local mode — the mirror image of the remote stub.

`AGNOSTIC`, `REMOTE_HANDLERS`, and `REMOTE_ONLY` must together classify *every* registered
command — a test asserts this so a future command can never silently run local code in remote
mode (nor remote code in local mode).
"""
from __future__ import annotations

import argparse
from typing import NoReturn

from shared import state

from . import remote_grid, remote_overview, remote_provider, remote_request


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
    "ctx",
    "engine",
    "agent",
    "mode",
    "use",
})

# Mode-gated commands: real local behaviour today; a clear stub in remote mode until later slices
# ship the remote handlers. NOTE: gated ``engines`` (live, networked) is distinct from the
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


def remote_stub(command: str | None) -> NoReturn:
    raise SystemExit(
        f"`grid {command}` isn't available in remote mode yet. Run `grid mode local` (or pass "
        "--local) to use it on your local grid."
    )


# command -> remote handler. Gated commands without a real handler yet map to the stub; the
# lifecycle verbs override it with their remote_grid handlers (issue 04). `list` is the `ls` alias
# (GATED includes it) and MUST track `ls`, or `grid list` in remote mode would still report "unavailable".
# Built in one immutable expression (the stubs first, then the real handlers win on key collision).
_REMOTE_STUBS = {command: (lambda args, _c=command: remote_stub(_c)) for command in GATED}
REMOTE_HANDLERS = {
    **_REMOTE_STUBS,
    "up": remote_grid.cmd_remote_up,
    "down": remote_grid.cmd_remote_down,
    "ls": remote_grid.cmd_remote_ls,
    "list": remote_grid.cmd_remote_ls,
    "info": remote_grid.cmd_remote_info,
    "engines": remote_overview.cmd_remote_engines,
    "models": remote_overview.cmd_remote_models,
    "join": remote_provider.cmd_remote_join,
    "leave": remote_provider.cmd_remote_leave,
    "chat": remote_request.cmd_remote_chat,
    "image": remote_request.cmd_remote_image,
    "edit": remote_request.cmd_remote_edit,
    "video": remote_request.cmd_remote_video,
}


# Remote-only commands: they run their real handlers in remote mode and are gated with
# guidance in local mode — the mirror image of ``remote_stub`` for the GATED commands.
REMOTE_ONLY = frozenset({"login", "logout", "members", "sync", "price", "router"})


def local_stub(command: str | None) -> NoReturn:
    raise SystemExit(
        f"`grid {command}` is a remote-mode command. Run `grid mode remote` (or pass --remote) "
        "to sign in."
    )


def resolve_override(argv: list[str]) -> tuple[str | None, list[str]]:
    """Strip a one-shot ``--local``/``--remote`` from argv (any position).

    Returns ``(override, cleaned_argv)``. Passing both ``--local`` and ``--remote`` is an
    error. The flag is matched as a bare token anywhere — acceptable on this surface.
    """
    override: str | None = None
    cleaned: list[str] = []
    for token in argv:
        if token in ("--local", "--remote"):
            flag = token[2:]
            if override is not None and override != flag:
                raise SystemExit("Pass only one of --local / --remote.")
            override = flag
        else:
            cleaned.append(token)
    return override, cleaned


def dispatch(args: argparse.Namespace, override: str | None) -> int:
    mode = state.resolve_mode(override)
    args.mode = mode
    command = getattr(args, "command", None)
    if mode == "remote":
        if command in REMOTE_HANDLERS:
            return REMOTE_HANDLERS[command](args) or 0
        if command not in AGNOSTIC and command not in REMOTE_ONLY:
            # Defence in depth: the classification test should already catch this, but a
            # runtime guard means an unclassified command can never silently run local code.
            raise SystemExit(
                f"Internal error: command {command!r} is not classified for remote dispatch. "
                "Please file a bug."
            )
        # AGNOSTIC and REMOTE_ONLY both fall through to their real handler below.
    elif command in REMOTE_ONLY:
        # local mode: a remote-only command can't run here. Must be ``elif`` — ``dispatch`` has
        # no ``else`` after the remote block, so a bare ``if`` would fire in remote mode too and
        # break login/logout there.
        local_stub(command)
    return args.handler(args) or 0
