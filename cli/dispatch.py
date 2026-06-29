"""Mode-aware command dispatch.

`resolve_override` pulls a one-shot `--lan`/`--internet` out of argv (any position).
`dispatch` resolves the effective mode, stamps it on ``args.mode`` (so mode-agnostic
handlers like the overview and `grid use` can see it), and routes: LAN handlers wired
by the parser run as-is; in internet mode, mode-gated commands route to a clear stub until later
slices implement real internet handlers; and internet-only commands (sign-in) are gated with
guidance when run in LAN mode — the mirror image of the internet stub.

`AGNOSTIC`, `INTERNET_HANDLERS`, and `INTERNET_ONLY` must together classify *every* registered
command — a test asserts this so a future command can never silently run LAN code in internet
mode (nor internet code in LAN mode).
"""
from __future__ import annotations

import argparse
from typing import NoReturn

from shared import state

from . import internet_grid, internet_provider, internet_request


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

# Mode-gated commands: real LAN behaviour today; a clear stub in internet mode until later slices
# ship the internet handlers. NOTE: gated ``engines`` (live, networked) is distinct from the
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


def internet_stub(command: str | None) -> NoReturn:
    raise SystemExit(
        f"`grid {command}` isn't available in internet mode yet. Run `grid mode lan` (or pass "
        "--lan) to use it on your LAN grid."
    )


# command -> internet handler. Gated commands without a real handler yet map to the stub; the
# lifecycle verbs override it with their internet_grid handlers (issue 04). `list` is the `ls` alias
# (GATED includes it) and MUST track `ls`, or `grid list` in internet mode would still report "unavailable".
# Built in one immutable expression (the stubs first, then the real handlers win on key collision).
_INTERNET_STUBS = {command: (lambda args, _c=command: internet_stub(_c)) for command in GATED}
INTERNET_HANDLERS = {
    **_INTERNET_STUBS,
    "up": internet_grid.cmd_internet_up,
    "down": internet_grid.cmd_internet_down,
    "ls": internet_grid.cmd_internet_ls,
    "list": internet_grid.cmd_internet_ls,
    "info": internet_grid.cmd_internet_info,
    "join": internet_provider.cmd_internet_join,
    "leave": internet_provider.cmd_internet_leave,
    "chat": internet_request.cmd_internet_chat,
    "image": internet_request.cmd_internet_image,
    "edit": internet_request.cmd_internet_edit,
    "video": internet_request.cmd_internet_video,
}


# Internet-only commands: they run their real handlers in internet mode and are gated with
# guidance in LAN mode — the mirror image of ``internet_stub`` for the GATED commands.
INTERNET_ONLY = frozenset({"login", "logout", "members", "sync"})


def lan_stub(command: str | None) -> NoReturn:
    raise SystemExit(
        f"`grid {command}` is an internet-mode command. Run `grid mode internet` (or pass --internet) "
        "to sign in."
    )


def resolve_override(argv: list[str]) -> tuple[str | None, list[str]]:
    """Strip a one-shot ``--lan``/``--internet`` from argv (any position).

    Returns ``(override, cleaned_argv)``. Passing both ``--lan`` and ``--internet`` is an
    error. The flag is matched as a bare token anywhere — acceptable on this surface.
    """
    override: str | None = None
    cleaned: list[str] = []
    for token in argv:
        if token in ("--lan", "--internet"):
            flag = token[2:]
            if override is not None and override != flag:
                raise SystemExit("Pass only one of --lan / --internet.")
            override = flag
        else:
            cleaned.append(token)
    return override, cleaned


def dispatch(args: argparse.Namespace, override: str | None) -> int:
    mode = state.resolve_mode(override)
    args.mode = mode
    command = getattr(args, "command", None)
    if mode == "internet":
        if command in INTERNET_HANDLERS:
            return INTERNET_HANDLERS[command](args) or 0
        if command not in AGNOSTIC and command not in INTERNET_ONLY:
            # Defence in depth: the classification test should already catch this, but a
            # runtime guard means an unclassified command can never silently run LAN code.
            raise SystemExit(
                f"Internal error: command {command!r} is not classified for internet dispatch. "
                "Please file a bug."
            )
        # AGNOSTIC and INTERNET_ONLY both fall through to their real handler below.
    elif command in INTERNET_ONLY:
        # LAN mode: an internet-only command can't run here. Must be ``elif`` — ``dispatch`` has
        # no ``else`` after the internet block, so a bare ``if`` would fire in internet mode too and
        # break login/logout there.
        lan_stub(command)
    return args.handler(args) or 0
