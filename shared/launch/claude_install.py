"""Where Claude Code is on this machine, and how to get it if it is not.

Separate from ``claude.py`` — that module answers "what is the app handed", this one answers "is there
an app at all". They fail in different ways and change for different reasons.

ADR 0028 rejected the shape ``shared/agent/codex_installer`` uses (a pinned, SHA-256-verified binary
under the Grid home). That fits Codex because Codex is a static release asset; Claude Code manages its
own versions, so pinning would make this repo the owner of a number it does not control and would ship
users a stale agent. So nothing here names a version or a checksum: the offer is to run the **vendor's
own installer**, which does its own verification.
"""
from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from . import system

BINARY = "claude"

# Where a user without the app is sent. The vendor's own page, never a repackaged copy — and no pinned
# version anywhere, because Claude Code manages its own (ADR 0028).
INSTALL_URL = "https://claude.com/claude-code"

# The vendor's own official installer, their published one-liner unchanged. A module constant with no
# interpolation of anything: the one value a shell string could ever take here is the app's own name,
# which is also a constant, so there is no site where user input could reach `bash -c`.
_POSIX_INSTALL = "curl -fsSL https://claude.ai/install.sh | bash"

# What is actually run adds two guards to that line. Neither changes which script executes; both only
# narrow how it can go wrong, which is why the *printed* instruction stays the vendor's own.
#
# `--proto '=https'`: `-L` follows redirects, and without this a redirect could walk the download down
# to plain HTTP. This repo's own `install.sh` pins it on every network `curl` it makes, including the
# structurally identical `curl … astral.sh/uv/install.sh | sh` — so this is the house rule, not
# belt-and-braces.
#
# `set -o pipefail`: without it the pipeline reports `bash`'s status, and a failed `curl` feeds `bash`
# an empty script that exits 0 — measured, not assumed. The install then "succeeds" having done
# nothing, and the user is told to open a new shell when the real problem was the network.
_POSIX_INSTALL_ARGV = (
    "/bin/bash",
    "-c",
    "set -o pipefail; curl -fsSL --proto '=https' https://claude.ai/install.sh | bash",
)

# That script refuses Windows itself ("Windows is not supported by this script"), and Windows has a
# separate PowerShell installer. Running *that* is not built here: a Windows user gets the printed
# command instead, through the same branch a machine with no terminal already takes. Kept as text so
# the instruction a Windows user reads is still the right one.
_WINDOWS_INSTALL = "irm https://claude.ai/install.ps1 | iex"


def _is_windows() -> bool:
    return platform.system() == "Windows"


def install_command() -> tuple[str, ...] | None:
    """The vendor's installer as an argument vector, or ``None`` where we do not run it ourselves."""
    return None if _is_windows() else _POSIX_INSTALL_ARGV


def install_instruction() -> str:
    """The installer as a line a user can paste — what we print wherever we will not run it."""
    return _WINDOWS_INSTALL if _is_windows() else _POSIX_INSTALL


def install_locations() -> tuple[tuple[Path, ...], str | None]:
    """The two conventional places Claude Code installs itself in the order they are tried, and why
    they could not be worked out at all.

    Both were read off the app rather than guessed: ``~/.local/bin/claude`` is the launcher the native
    installer manages (a symlink into ``~/.local/share/claude/versions/``), and ``~/.claude/local/claude``
    is the older local install. Neither is on every user's ``PATH``, which is the whole reason they are
    searched.

    Two homes are refused, and **reported rather than silently dropped** — a caller that quietly
    searched nothing would go on to say it found nothing "in either place", which is a claim about a
    check that never ran:

    - **No home at all.** ``Path.home()`` raises ``RuntimeError`` on a container running as a UID with
      no passwd entry and no ``HOME``, the same failure ``claude._settings_paths`` guards against.
    - **A relative home.** ``Path.home()`` is only as good as ``HOME``, and ``HOME=.`` yields ``.`` —
      so the candidates would resolve against the *current directory*. A repository that ships
      ``.local/bin/claude`` would then have its own binary run with the grid's token in its
      environment, which is the ``_settings_env`` threat model with execution on the end of it.
    """
    try:
        home = Path.home()
    except RuntimeError as exc:
        return (), f"your home directory could not be resolved ({exc})"
    if not home.is_absolute():
        return (), f"your home directory is not an absolute path ({str(home)!r})"
    name = f"{BINARY}.exe" if _is_windows() else BINARY
    return (home / ".local" / "bin" / name, home / ".claude" / "local" / name), None


@dataclass(frozen=True)
class Resolution:
    """Where Claude Code is, and what stood in the way of finding out."""

    #: The runnable command, or ``None`` when nothing was found *and* nothing obstructed the search.
    binary: str | None
    #: One line per place that could not be looked at, and why. Empty on an ordinary miss — which is
    #: what makes a non-empty value mean "this answer is incomplete" rather than "this answer is no".
    unchecked: tuple[str, ...]


def resolve() -> Resolution:
    """Claude Code on this machine: ``PATH`` first, then the conventional locations, first hit wins.

    ``PATH`` going first means it can resolve to a third-party wrapper rather than the vendor's own
    install. That is accepted deliberately — it is what the user's own ``PATH`` says they want, and it
    is what ollama's equivalent does — so nothing here tries to detect or second-guess one.

    An obstructed candidate never ends the search: the whole point of a second location is that the
    first one may be unusable, so what cannot be checked is collected and carried out alongside the
    answer instead of raising over it.
    """
    found = system.find_executable(BINARY)
    if found is not None:
        return Resolution(binary=found, unchecked=())
    locations, unresolved = install_locations()
    unchecked = [unresolved] if unresolved else []
    for candidate in locations:
        found, reason = system.executable_at(candidate)
        if found is not None:
            return Resolution(binary=found, unchecked=tuple(unchecked))
        if reason is not None:
            unchecked.append(f"{candidate} ({reason})")
    return Resolution(binary=None, unchecked=tuple(unchecked))


def _warn_unchecked(unchecked: tuple[str, ...]) -> None:
    """Say what could not be looked at, on a run that is otherwise going ahead.

    stderr, and never fatal: an app was found, so this is a caveat on a working launch rather than a
    problem with it. It still has to be said — the one thing worse than an incomplete search is an
    incomplete search that looked complete, because that is what sends a user hunting for the wrong
    fault when the app they get is not the one they expected.
    """
    if unchecked:
        print(
            f"Warning: couldn't check {'; '.join(unchecked)} for {BINARY}.",
            file=sys.stderr,
            flush=True,
        )


def _unchecked_note(unchecked: tuple[str, ...]) -> str:
    """The line that keeps "isn't installed" honest when the search was incomplete.

    Without it every refusal claims a completed search. A user whose `~/.local/bin` is unreadable, or
    whose home cannot be resolved, would be told the app is not installed when what actually happened
    is that we could not look — and would then be handed an installer that fails the same way.
    """
    if not unchecked:
        return ""
    return "\nCouldn't check: " + "; ".join(unchecked) + "."


def _refuse_uninstalled(lead: str, unchecked: tuple[str, ...] = ()) -> NoReturn:
    """Stop, telling the user exactly what to run — the one exit from every no-install outcome.

    ``lead`` is the only part that differs between them ("isn't installed" vs "not installing"), and
    the rest is shared on purpose: whichever way a user arrives here, the next thing they need is the
    command, and it should not read differently depending on how they got here.
    """
    raise SystemExit(
        f"{lead}{_unchecked_note(unchecked)}\n"
        f"  {install_instruction()}\n"
        f"Then run `grid launch {BINARY}` again, or see {INSTALL_URL}."
    )


def resolve_or_install(label: str) -> str:
    """Claude Code on this machine, installing it first if the user asks for that.

    The offer is the point of this function: a machine without the app is the *first-run* state, and a
    dead end there is a user who never sees the feature work at all.

    It asks only where an answer can be given. A prompt on a machine with no terminal is worse than no
    prompt: it blocks until something kills it, and the operator reads a hang instead of the one line
    that fixes their problem. So a non-interactive run — and a platform whose installer we do not run
    ourselves — prints the command and exits non-zero, which is what automation can act on.
    """
    resolution = resolve()
    if resolution.binary is not None:
        # Found, but perhaps not everywhere we meant to look. The launch proceeds — this is a working
        # app — and the incomplete search is a warning rather than a refusal, on stderr because stdout
        # belongs to the app that is about to start.
        _warn_unchecked(resolution.unchecked)
        return resolution.binary
    command = install_command()
    if command is None or not system.interactive():
        _refuse_uninstalled(f"{label} isn't installed.", resolution.unchecked)
    # stdout, unlike the launch banner: this is one half of a conversation whose other half is the
    # prompt below, and `input` writes that to stdout. Reached only when `interactive()` already said
    # stdout is a terminal, so it can never land in a pipe the app's own output was meant to fill.
    _warn_unchecked(resolution.unchecked)
    print(f"{label} isn't installed. `grid launch` can install it with the official installer:",
          flush=True)
    print(f"  {install_instruction()}", flush=True)
    if not system.confirm(f"Install {label} now?"):
        _refuse_uninstalled(f"Not installing {label}.", resolution.unchecked)
    # `os.environ` verbatim, and never the environment the app is about to be handed: the installer
    # talks to the vendor, not to the grid, so giving it the grid's bearer token would be an exposure
    # bought for nothing. Through `spawn` because the installer is a foreground child that owns the
    # terminal exactly like the app does — it runs the vendor's own TUI, and a Ctrl-C during it must
    # reach it rather than us.
    try:
        code = system.spawn(command, os.environ)
    except SystemExit as exc:
        # `spawn`'s own message names `argv[0]` — here `/bin/bash`, which says nothing about Claude
        # Code, nothing about an installer, and nothing about what to do. The *worst* failure (the
        # installer could not even start) would otherwise get the least actionable message of any
        # branch in this function.
        raise SystemExit(
            f"Couldn't run the {label} installer: {exc}\n"
            f"  {install_instruction()}\n"
            f"Run that yourself, or see {INSTALL_URL}."
        ) from exc
    if code != 0:
        raise SystemExit(
            f"The {label} installer exited {code}; nothing was launched. "
            f"Install it yourself with `{install_instruction()}` or from {INSTALL_URL}, "
            f"then run `grid launch {BINARY}` again."
        )
    # The whole resolution again, not just `PATH`. The installer writes its launcher into
    # `~/.local/bin` and appends a `PATH` line to a shell rc file — neither of which reaches a process
    # that is already running. So `PATH` is still as stale as it was a moment ago, and asking it alone
    # would tell a user who just watched the install succeed that the app is not installed.
    resolution = resolve()
    if resolution.binary is None:
        raise SystemExit(
            f"The {label} installer finished, but no `{BINARY}` turned up on your PATH or in either "
            f"place it installs to.{_unchecked_note(resolution.unchecked)}\n"
            f"Open a new shell and run `grid launch {BINARY}` again; if that still fails, the install "
            f"did not land — see {INSTALL_URL}."
        )
    _warn_unchecked(resolution.unchecked)
    return resolution.binary
