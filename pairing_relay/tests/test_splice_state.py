"""The order a connection is allowed to happen in.

Every assertion here is a shape of bug that looks like a working connection.
"""
from __future__ import annotations

import pytest

from pairing_relay.splice_state import (
    SpliceState,
    SpliceStateError,
    advance,
    can_advance,
    may_acknowledge_client,
)


def test_the_happy_path_is_reachable_one_step_at_a_time():
    order = [
        SpliceState.PRE_AUTH_ADMITTED,
        SpliceState.CREDENTIAL_RESERVED,
        SpliceState.HOST_NOTIFIED,
        SpliceState.ATTACH_PENDING,
        SpliceState.HOST_ATTACHED,
        SpliceState.CLIENT_ACKNOWLEDGED,
        SpliceState.SPLICED,
    ]
    for current, target in zip(order, order[1:]):
        assert can_advance(current, target), f"{current} -> {target}"


def test_a_connection_can_never_go_backwards_or_skip_a_step():
    # Skipping is how "acknowledged but never attached" happens.
    assert not can_advance(SpliceState.ATTACH_PENDING, SpliceState.SPLICED)
    assert not can_advance(SpliceState.CREDENTIAL_RESERVED, SpliceState.HOST_ATTACHED)
    # Going back is how a second data socket attaches to a live splice.
    assert not can_advance(SpliceState.SPLICED, SpliceState.HOST_ATTACHED)
    assert not can_advance(SpliceState.HOST_ATTACHED, SpliceState.ATTACH_PENDING)


def test_teardown_is_reachable_from_everywhere_and_is_the_end():
    for state in SpliceState:
        if state is SpliceState.TEARDOWN:
            continue
        assert can_advance(state, SpliceState.TEARDOWN), state
    for state in SpliceState:
        assert not can_advance(SpliceState.TEARDOWN, state), state


def test_advance_raises_rather_than_returning_the_old_state():
    # A silent no-op here would leave the caller believing it moved on.
    assert advance(SpliceState.ATTACH_PENDING, SpliceState.HOST_ATTACHED) is (
        SpliceState.HOST_ATTACHED
    )
    with pytest.raises(SpliceStateError):
        advance(SpliceState.SPLICED, SpliceState.HOST_ATTACHED)


def test_the_phone_is_only_acknowledged_once_forwarding_actually_exists():
    # Both halves. The state alone is what Orca's own comment warns about:
    # success before both handlers are installed strands a client on a splice
    # that looks alive and carries nothing.
    assert may_acknowledge_client(SpliceState.HOST_ATTACHED, True)
    assert not may_acknowledge_client(SpliceState.HOST_ATTACHED, False)
    assert not may_acknowledge_client(SpliceState.ATTACH_PENDING, True)
    assert not may_acknowledge_client(SpliceState.SPLICED, True)
