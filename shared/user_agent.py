"""The User-Agent this CLI sends to a grid's relay and to the control plane.

The control plane's proxy journal names the client behind every wake and every refusal
(grid-reads-without-waking issue 01), and the operator counts its lines by this header: which wakes a
person's `grid` caused, and whether any came from a read that carried no credential. So a relay request
says which build is asking, and whether it carries a credential — one that does not is a request the proxy
never wakes a sleeping grid for (grid-apis `wake_routes`), which is what `--no-wake` relies on.

⚠️ **Read by people counting journal lines, not by any program.** Nothing on the far side branches on it,
so a change here breaks no seam — only the operator's saved queries, which is why it is a register row.
"""
from __future__ import annotations

from shared._version import __version__

#: What every request from this build says it is — the whole header for the control plane, which is
#: never behind the proxy and so has nothing a `(no-wake)` would tell anybody.
CLI_USER_AGENT = f"grid-cli/{__version__}"

#: Appended to a RELAY request that carries no credential. Spelled in the public repo's lockstep register.
NO_WAKE_SUFFIX = " (no-wake)"


def relay_user_agent(*, has_credential: bool) -> str:
    """:data:`CLI_USER_AGENT`, plus :data:`NO_WAKE_SUFFIX` when the relay request carries no credential."""
    return CLI_USER_AGENT if has_credential else CLI_USER_AGENT + NO_WAKE_SUFFIX
