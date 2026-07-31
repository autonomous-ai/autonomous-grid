"""What a launch target is, and what it is told about the grid it is being pointed at."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class GridSession:
    """The grid a target is being pointed at — everything a target is allowed to know about it.

    Deliberately narrow: a label to name in the target's own output, the relay base, the grid's
    access token, and the models it currently serves. A target never sees the credentials file, the
    control-plane session token, or the stored grid record.
    """

    label: str
    relay_base: str
    access_token: str
    #: The model ids the grid serves right now, in the grid's own order — read from the public
    #: overview by `cli/launch.py`. A target compares this against whatever models it needs and
    #: decides for itself: the CLI cannot, because a target's model names are the target's own
    #: (ADR 0028). Empty means the grid serves nothing, which is a refusal, not a missing read.
    live_models: tuple[str, ...]


class LaunchTarget(Protocol):
    """An app `grid launch` can start.

    The boundary is deliberate: the obvious second target (Codex) configures itself through a **file**
    rather than environment variables, so only ``run`` knows how a target is configured. That
    difference stays inside the target instead of leaking into the command.

    ``run`` also resolves the app itself, and offers to install it when it is absent — one call, and no
    window in which the app can leave ``PATH`` between a separate installed-check and its use.

    There is deliberately **no** ``is_installed`` member, though the feature's PRD sketched one. The
    install offer has to happen where the binary is resolved, because only a target knows its own
    binary, its own install locations and its own installer — so a member for it would have no caller,
    and would reopen the check-then-run gap the single call closes.
    """

    name: str
    label: str

    def run(self, session: GridSession, argv: Sequence[str] = ()) -> int:
        """Start the app pointed at ``session`` with ``argv`` appended, and return its exit code.

        ``argv`` is whatever the user typed after ``--``. It is the app's own command line and is
        passed on unread: the launcher must never become a ceiling on the app it launches, and
        anything it interpreted here would be a flag the app could no longer receive.
        """
        ...

    def print_env(self, session: GridSession) -> int:
        """Describe the same configuration ``run`` would apply, without starting anything.

        On the target rather than in the command for two reasons. The preflight that decides whether
        a grid can run this app at all already lives here, so `--print-env` inherits it instead of
        re-implementing it — printing exports for a grid that cannot serve them would reproduce
        exactly the trap preflight exists to close. And a target that configures itself through a
        **file** rather than the environment (Codex, the reason this protocol exists) answers for its
        own mechanism, instead of the command assuming every target is env-shaped.
        """
        ...
