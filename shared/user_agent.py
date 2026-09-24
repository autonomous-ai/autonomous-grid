"""The User-Agent this CLI sends to a grid's relay and to the control plane.

The control plane's proxy journal names the client behind every wake and every refusal
(grid-reads-without-waking issue 01), and the operator counts its lines by this header: which wakes a
person's `grid` caused, and whether any came from a read that carried no credential. So it says which
build is asking, and whether the request carries a credential — a request that does not is one the proxy
never wakes a sleeping grid for (grid-apis `wake_routes`), which is what `--no-wake` relies on.

⚠️ **Read by people counting journal lines, not by any program.** Nothing on the far side branches on it,
so a change here breaks no seam — only the operator's saved queries, which is why it is a register row.
"""
from __future__ import annotations

from shared._version import __version__

#: Appended when no credential is sent. Spelled in the public repo's lockstep register.
NO_WAKE_SUFFIX = " (no-wake)"


def user_agent(*, credential: bool) -> str:
    """``grid-cli/<version>``, plus :data:`NO_WAKE_SUFFIX` when the request carries no credential."""
    base = f"grid-cli/{__version__}"
    return base if credential else base + NO_WAKE_SUFFIX
