"""What a launch target is, and what it is told about the grid it is being pointed at."""
from __future__ import annotations

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

    ``run`` also resolves the app itself and refuses cleanly when it is absent — one call, and no
    window in which the app can leave ``PATH`` between a separate installed-check and its use. Issue 04
    adds the installed-check as its own member, when the install offer that must ask *before* running
    arrives to call it.
    """

    name: str
    label: str

    def run(self, session: GridSession) -> int:
        """Start the app pointed at ``session``, and return its exit code."""
        ...
