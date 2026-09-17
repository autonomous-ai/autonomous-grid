"""The order a phone's connection is allowed to happen in.

A splice is built out of two sockets that arrive separately, from two peers
that cannot see each other. Almost every way it goes wrong is an ordering
mistake — acknowledging the phone before the desktop attached, attaching a
second data socket to a connection already spliced, reviving a torn-down one —
and each of those looks like a working connection that silently carries
nothing.

So the order is written down once, here, as data. The alternative is a set of
booleans spread over the session object, where "is it too late for this?" is a
different expression at every call site and one of them is wrong.
"""
from __future__ import annotations

from enum import Enum
from typing import Final


class SpliceState(Enum):
    """Where one phone connection has got to."""

    #: The socket passed the cheap checks and holds a slot. Nothing is reserved.
    PRE_AUTH_ADMITTED = "pre-auth-admitted"

    #: Its credential was spent. From here a failure must hand the credential
    #: back, or a phone that retries after a blip finds its invite already used.
    CREDENTIAL_RESERVED = "credential-reserved"

    #: `conn-open` is on the desktop's control channel.
    HOST_NOTIFIED = "host-notified"

    #: The attach deadline is armed and the cell is waiting for a data socket.
    ATTACH_PENDING = "attach-pending"

    #: The desktop's data socket arrived with the right ticket.
    HOST_ATTACHED = "host-attached"

    #: The phone has been told it is through. Only legal once forwarding is
    #: actually wired — see [may_acknowledge_client].
    CLIENT_ACKNOWLEDGED = "client-acknowledged"

    #: Bytes are moving.
    SPLICED = "spliced"

    #: Over. Terminal, and reachable from everywhere.
    TEARDOWN = "teardown"


#: Orca's own machine has a ninth state between SPLICED and TEARDOWN,
#: `e2ee-confirmable`, where a phone dialling in on a renewed resume token
#: proves the new credential works before the old one is retired. This cell
#: issues one-shot invites and never renews, so that state would be a value
#: nothing can reach — left out rather than carried as decoration.
_FORWARD: Final[dict[SpliceState, frozenset[SpliceState]]] = {
    SpliceState.PRE_AUTH_ADMITTED: frozenset(
        {SpliceState.CREDENTIAL_RESERVED, SpliceState.TEARDOWN}
    ),
    SpliceState.CREDENTIAL_RESERVED: frozenset(
        {SpliceState.HOST_NOTIFIED, SpliceState.TEARDOWN}
    ),
    SpliceState.HOST_NOTIFIED: frozenset(
        {SpliceState.ATTACH_PENDING, SpliceState.TEARDOWN}
    ),
    SpliceState.ATTACH_PENDING: frozenset(
        {SpliceState.HOST_ATTACHED, SpliceState.TEARDOWN}
    ),
    SpliceState.HOST_ATTACHED: frozenset(
        {SpliceState.CLIENT_ACKNOWLEDGED, SpliceState.TEARDOWN}
    ),
    SpliceState.CLIENT_ACKNOWLEDGED: frozenset(
        {SpliceState.SPLICED, SpliceState.TEARDOWN}
    ),
    SpliceState.SPLICED: frozenset({SpliceState.TEARDOWN}),
    SpliceState.TEARDOWN: frozenset(),
}


def can_advance(current: SpliceState, target: SpliceState) -> bool:
    """Whether [target] is the next step after [current], or a teardown."""
    return target in _FORWARD[current]


def may_acknowledge_client(state: SpliceState, forwarding_installed: bool) -> bool:
    """Whether the phone may be told it is through.

    Both halves matter. Telling the phone it is connected while the state is
    only `host-attached`, or while the forwarding path is not wired yet, hands
    it a socket that looks alive and drops everything written to it — and a
    phone has no way to tell that apart from a desktop that is simply quiet.
    """
    return state is SpliceState.HOST_ATTACHED and forwarding_installed


class SpliceStateError(RuntimeError):
    """A transition the machine does not allow, raised rather than ignored."""


def advance(current: SpliceState, target: SpliceState) -> SpliceState:
    """[target], or [SpliceStateError] when that is not a legal step."""
    if not can_advance(current, target):
        raise SpliceStateError(f"{current.value} cannot advance to {target.value}")
    return target
