"""Mode-aware command dispatch.

`resolve_override` pulls a one-shot `--local`/`--remote` out of argv (any position).
`dispatch` resolves the effective mode, stamps it on ``args.mode`` (so mode-agnostic
handlers like the overview and `grid use` can see it), and routes: local handlers wired
by the parser run as-is; in remote mode, mode-gated commands route to a clear stub until later
slices implement real remote handlers; and remote-only commands (sign-in) are gated with
guidance when run in local mode — the mirror image of the remote stub.

`AGNOSTIC`, `REMOTE_HANDLERS`, and `REMOTE_ONLY` must together classify *every* registered
command — a test asserts this so a future command can never silently run local code in remote
mode (nor remote code in local mode). `REMOTE_ONLY` maps each of its commands to the *reason*
its local-mode gate gives, because "sign in" is not why every remote-only command needs remote
mode; `None` takes the sign-in default.
"""
from __future__ import annotations

import argparse
import sys
from typing import NoReturn

from shared import state

from . import remote_grid, remote_overview, remote_provider, remote_request

# Commands that behave identically in both modes: local engine/model setup, plus the
# mode/selection commands and the bare overview (which branch on the mode internally).
# ``None`` is the bare `grid` invocation (no subcommand).
AGNOSTIC = frozenset({
    None,
    "version",
    "device-info",
    "catalog",
    "pull",
    "rm",
    "remove",
    "ctx",
    "engine",
    "agent",
    "mode",
    "use",
    # `train` talks to whatever rollout endpoint its config names (a local proxy or a hosted
    # relay URL work identically), so it is deliberately mode-blind like `engine`/`agent`.
    "train",
    # git's credential helper (ADR 0033 D-h). AGNOSTIC rather than REMOTE_ONLY even though only a
    # remote grid has a relay to clone from: git runs this on every operation inside a clone, in
    # whatever mode `grid mode` happens to be, so a local-mode refusal would break `git pull` in a
    # directory that has nothing to do with the member's current mode. It reads `credentials.toml`
    # and nothing else, which is not a mode-specific file.
    "credential",
    # `stt` hits the account-level control plane — there's no "this grid" for it to route
    # through, so it is mode-blind too.
    "stt",
})

# Mode-gated commands: real local behaviour today; a clear stub in remote mode until later slices
# ship the remote handlers. NOTE: gated ``engines`` (live, networked) is distinct from the
# agnostic ``engine`` (local setup) — one keystroke apart.
GATED = (
    "up",
    "down",
    # `start`/`stop` are `up`/`down` under a name that doesn't clash with `grid join`/`leave`
    # (cli/parser.py `_build_up_parser`) — same handler, so they need the same remote classification
    # or they hit the "not classified for remote dispatch" internal-error guard below.
    "start",
    "stop",
    # No remote handler on purpose: `cmd_delete` reads LOCAL grid config only (`local.config`), never
    # the remote grid records `remote.credentials` keeps — deleting one of those is a real
    # server-side action against the control plane and deserves its own design, not a same-named
    # local operation repurposed. The stub is the honest answer until that exists.
    "delete",
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
    "allocator",
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
    "start": remote_grid.cmd_remote_up,
    "stop": remote_grid.cmd_remote_down,
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


# The purpose clause completing "Run `grid mode remote` (or pass --remote) …" for a command that
# registers no reason of its own — every command below today, all gated because they need a
# signed-in account.
_DEFAULT_LOCAL_GATE_REASON = "to sign in."

# Remote-only commands: they run their real handlers in remote mode and are gated with
# guidance in local mode — the mirror image of ``remote_stub`` for the GATED commands.
#
# command -> why switching to remote mode is what this user wants, or ``None`` for the default
# above. Sign-in is not every command's reason: a command gated for some other reason (a local grid
# not serving the dialect an app speaks, say) registers its own. A value completes the sentence in
# ``local_stub``, so it reads as a purpose clause and carries its own final period; the
# `grid mode remote` signpost stays in the fixed part, where no later command can drop it.
REMOTE_ONLY: dict[str, str | None] = {
    "login": None,
    "logout": None,
    "members": None,
    "sync": None,
    "price": None,
    "router": None,
    # Tasks live in the relay's durable queue and are claimed by whichever provider is free
    # (ADR 0032). A local grid has neither, so this is sign-in-gated like the rest.
    "task": None,
    # A project and its members are rows in the RELAY's own database (ADR 0033 D-a) — deliberately
    # not the control plane's — and the repository they name is served by the relay's git plane. A
    # local grid has none of it.
    "project": None,
    # The one command whose reason is not sign-in (ADR 0028): a local grid serves chat/completions,
    # completions, models and media — never Anthropic Messages, which is the only dialect Claude Code
    # speaks. Naming the dialect is what stops this being filed as a bug.
    "launch": (
        "to launch an app that speaks the Anthropic Messages dialect, which a local grid "
        "does not serve."
    ),
}


def local_stub(command: str | None) -> NoReturn:
    # ``or``, not ``is None``: a registered reason that is empty is a coding error, and today's
    # sentence is a better thing to print than a sentence that stops mid-air.
    reason = REMOTE_ONLY.get(command) or _DEFAULT_LOCAL_GATE_REASON
    raise SystemExit(
        f"`grid {command}` is a remote-mode command. Run `grid mode remote` (or pass --remote) "
        f"{reason}"
    )


def resolve_override(argv: list[str]) -> tuple[str | None, list[str]]:
    """Strip a one-shot ``--local``/``--remote`` from argv (any position).

    Returns ``(override, cleaned_argv)``. Passing both ``--local`` and ``--remote`` is an
    error. The flag is matched as a bare token anywhere — acceptable on this surface.
    """
    override: str | None = None
    cleaned: list[str] = []
    past_separator = False
    for token in argv:
        if token in ("--local", "--remote"):
            if past_separator:
                # The one substitution on this surface a user cannot see happening. After `--` they
                # were addressing another program (`grid launch <target> -- …`), and this took the
                # token anyway. `--remote` then simply vanishes; `--local` flips the run's mode, so a
                # remote-only command refuses and reports a *mode* problem — pointing at a fix that
                # has nothing to do with what they actually did. Said here, where the token is taken,
                # so it covers both directions and every command rather than one gate's message.
                print(
                    f"Warning: `{token}` after `--` is grid's own one-shot mode override, not the "
                    f"launched app's — it has been removed from the command line and this run uses "
                    f"{token[2:]} mode.",
                    file=sys.stderr,
                    flush=True,
                )
            flag = token[2:]
            if override is not None and override != flag:
                raise SystemExit("Pass only one of --local / --remote.")
            override = flag
        else:
            # Only the first `--` opens the forwarded region, but re-marking on a later one is
            # harmless: everything from the first onwards is already past it.
            past_separator = past_separator or token == "--"
            cleaned.append(token)
    return override, cleaned


# The one command whose ``--`` means "everything after this belongs to the app it launches", rather
# than argparse's own "stop looking for options". Deliberately a single name and not a growing list:
# a second passthrough command is a decision, not a config value.
_PASSTHROUGH_COMMAND = "launch"


def split_forwarded(argv: list[str]) -> tuple[list[str], tuple[str, ...]]:
    """Split ``grid launch … -- <the app's own arguments>`` at the first bare ``--``.

    Returns ``(argv for the parser, the forwarded vector)``.

    Done **here, before ``parse_args``**, because argparse cannot do it. ``launch`` has two
    ``nargs="?"`` positionals (target, grid), so a third ``nargs="*"`` positional binds the first
    forwarded word to *grid* — ``launch claude -- -p hi`` yields ``grid="-p"`` — and
    ``nargs=REMAINDER`` swallows the launcher's own ``--print-env`` into the forwarded vector while
    leaving the flag False. Both verified against argparse, not assumed.

    Scoped to one command on purpose. Every other command keeps argparse's own ``--`` handling, so
    what ``grid chat -- hello`` has always meant cannot change as a side effect of this feature.

    Runs **after** ``resolve_override``, which has already pulled ``--local``/``--remote`` out of any
    position — *including* from after this separator. Those two exact tokens therefore cannot be
    forwarded to a launched app (ADR 0028), which is why `grid launch --help` says so.
    """
    try:
        # The FIRST separator only: a later ``--`` is one of the app's own arguments and is forwarded
        # verbatim, exactly as it would be if the user had typed it into the app's own command line.
        separator = argv.index("--")
    except ValueError:
        return argv, ()
    # The command word, found the way argparse finds it: the first token that is not an option. This
    # tolerates the global flags that may precede a subcommand (`grid --json launch …`).
    command = next((token for token in argv[:separator] if not token.startswith("-")), None)
    if command != _PASSTHROUGH_COMMAND:
        return argv, ()
    return argv[:separator], tuple(argv[separator + 1:])


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
