"""What the cell owes a phone, a desktop, and everyone else.

Driven with fakes rather than sockets: every property here is about ordering,
lifetime and authority, and none of them is about the network.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json

import pytest

from pairing_relay.cell import Cell
from pairing_relay.protocol import DEFAULT_LIMITS, CloseCode, RelayRefusal
from pairing_relay.splice_state import SpliceState
from pairing_relay.tests.support import (
    RELAY_ORIGIN,
    FakeHost,
    FakeSocket,
    asyncio_test,
    connect_host,
    last_json,
)


def make_cell(**limit_overrides) -> Cell:
    return Cell(
        relay_origin=RELAY_ORIGIN,
        limits=dataclasses.replace(DEFAULT_LIMITS, **limit_overrides),
    )


async def spliced(cell: Cell):
    """A desktop, a phone, and a live splice between them."""
    session, control, host = await connect_host(cell)
    invite = cell.create_invite(session, "device-1")
    client = FakeSocket()
    _, connection = await cell.open_client(client, session.relay_host_id, invite.token)
    opened = last_json(control)
    data = FakeSocket()
    await cell.attach_host_data(
        data, opened["connId"], opened["connTicket"], session.generation
    )
    return session, control, client, data, connection, host


# --- who gets in --------------------------------------------------------------


@asyncio_test
async def test_a_desktop_cannot_claim_an_id_its_key_does_not_own():
    cell, host = make_cell(), FakeHost()
    hello = host.hello() | {"relayHostId": FakeHost().relay_host_id}
    with pytest.raises(RelayRefusal) as refusal:
        cell.begin_host_hello(hello)
    assert refusal.value.code == CloseCode.BAD_CREDENTIAL


@asyncio_test
async def test_a_phone_whose_desktop_is_not_connected_is_told_exactly_that():
    # The one refusal a phone's owner can act on, so it must not be vague.
    cell = make_cell()
    with pytest.raises(RelayRefusal) as refusal:
        await cell.open_client(FakeSocket(), "AbCdEf0123_-xyZ9", "any-token")
    assert refusal.value.code == CloseCode.HOST_OFFLINE


@asyncio_test
async def test_a_wrong_credential_is_refused_without_saying_which_part_was_wrong():
    cell = make_cell()
    session, _, _ = await connect_host(cell)
    cell.create_invite(session, "device-1")
    with pytest.raises(RelayRefusal) as refusal:
        await cell.open_client(FakeSocket(), session.relay_host_id, "not-the-token")
    assert refusal.value.code == CloseCode.BAD_CREDENTIAL


# --- the splice ---------------------------------------------------------------


@asyncio_test
async def test_the_desktop_is_asked_to_attach_and_the_phone_waits_in_silence():
    cell = make_cell()
    session, control, _ = await connect_host(cell)
    invite = cell.create_invite(session, "device-1")
    client = FakeSocket()
    _, connection = await cell.open_client(client, session.relay_host_id, invite.token)

    opened = last_json(control)
    assert opened["type"] == "conn-open"
    assert opened["relayDeviceId"] == "device-1"
    assert connection.state is SpliceState.ATTACH_PENDING
    # Nothing has been promised to the phone yet, because nothing can carry it.
    assert client.texts == []


@asyncio_test
async def test_the_phone_is_acknowledged_only_after_the_desktop_has_attached():
    cell = make_cell()
    _, _, client, _, connection, _ = await spliced(cell)

    assert connection.state is SpliceState.SPLICED
    hello = last_json(client)
    assert hello == {
        "type": "relay-hello",
        "ok": True,
        "credentialKind": "invite",
        "leaseExpiresAt": hello["leaseExpiresAt"],
    }


@asyncio_test
async def test_a_ticket_does_not_open_a_different_connection():
    cell = make_cell()
    session, control, _ = await connect_host(cell)
    first = cell.create_invite(session, "device-1")
    second = cell.create_invite(session, "device-2")
    await cell.open_client(FakeSocket(), session.relay_host_id, first.token)
    first_open = last_json(control)
    await cell.open_client(FakeSocket(), session.relay_host_id, second.token)
    second_open = last_json(control)

    with pytest.raises(RelayRefusal) as refusal:
        await cell.attach_host_data(
            FakeSocket(), second_open["connId"], first_open["connTicket"], session.generation
        )
    assert refusal.value.code == CloseCode.BAD_CREDENTIAL


@asyncio_test
async def test_a_data_socket_from_a_replaced_control_session_cannot_attach():
    # The desktop reconnected; sockets belonging to the old control channel are
    # no longer speaking for it.
    cell = make_cell()
    session, control, _ = await connect_host(cell)
    invite = cell.create_invite(session, "device-1")
    await cell.open_client(FakeSocket(), session.relay_host_id, invite.token)
    opened = last_json(control)

    with pytest.raises(RelayRefusal) as refusal:
        await cell.attach_host_data(
            FakeSocket(), opened["connId"], opened["connTicket"], session.generation - 1
        )
    assert refusal.value.code == CloseCode.BAD_CREDENTIAL


@asyncio_test
async def test_a_desktop_that_never_attaches_stops_the_phone_waiting_forever():
    cell = make_cell(host_attach_deadline_s=0.05)
    session, _, _ = await connect_host(cell)
    invite = cell.create_invite(session, "device-1")
    client = FakeSocket()
    _, connection = await cell.open_client(client, session.relay_host_id, invite.token)

    await asyncio.sleep(0.12)
    assert connection.state is SpliceState.TEARDOWN
    assert client.closed is not None and client.closed[0] == CloseCode.ATTACH_TIMEOUT


# --- bytes --------------------------------------------------------------------


@asyncio_test
async def test_frames_cross_in_both_directions_exactly_as_they_arrived():
    cell = make_cell()
    session, _, client, data, connection, _ = await spliced(cell)

    # Bytes that look like the cell's own control messages, to prove nothing
    # here reads the payload: above this layer it is sealed and unreadable.
    disguised = json.dumps({"type": "conn-open", "connId": "spoofed"}).encode()
    await cell.forward_from_client(session, connection, disguised)
    await cell.forward_from_host(session, connection, b"\x00\xff binary")

    assert data.frames == [disguised]
    assert client.frames == [b"\x00\xff binary"]


@asyncio_test
async def test_a_frame_larger_than_the_ceiling_ends_the_session_rather_than_the_frame():
    # The stream is opaque and the peers count frames: a dropped one is a hole
    # they cannot see, which is worse than a close they can.
    cell = make_cell(max_frame_bytes=64)
    session, _, client, data, connection, _ = await spliced(cell)

    with pytest.raises(RelayRefusal) as refusal:
        await cell.forward_from_client(session, connection, b"x" * 65)
    assert refusal.value.code == CloseCode.LIMIT_EXCEEDED
    assert connection.state is SpliceState.TEARDOWN
    assert client.closed is not None and data.closed is not None


@asyncio_test
async def test_a_peer_that_never_drains_is_cut_loose_instead_of_wedging_the_other():
    cell = make_cell(splice_send_timeout_s=0.05)
    session, _, client, data, connection, _ = await spliced(cell)
    data.stall_s = 5.0

    with pytest.raises(RelayRefusal) as refusal:
        await cell.forward_from_client(session, connection, b"hello")
    assert refusal.value.code == CloseCode.LIMIT_EXCEEDED
    assert connection.state is SpliceState.TEARDOWN


@asyncio_test
async def test_nothing_is_forwarded_before_the_splice_is_complete():
    cell = make_cell()
    session, control, _ = await connect_host(cell)
    invite = cell.create_invite(session, "device-1")
    client = FakeSocket()
    _, connection = await cell.open_client(client, session.relay_host_id, invite.token)

    with pytest.raises(RelayRefusal):
        await cell.forward_from_client(session, connection, b"too early")


# --- authority and limits -----------------------------------------------------


@asyncio_test
async def test_a_pairing_code_opens_one_connection_and_then_is_spent():
    cell = make_cell()
    session, control, _ = await connect_host(cell)
    invite = cell.create_invite(session, "device-1")
    _, connection = await cell.open_client(
        FakeSocket(), session.relay_host_id, invite.token
    )
    opened = last_json(control)
    await cell.attach_host_data(
        FakeSocket(), opened["connId"], opened["connTicket"], session.generation
    )

    with pytest.raises(RelayRefusal) as refusal:
        await cell.open_client(FakeSocket(), session.relay_host_id, invite.token)
    assert refusal.value.code == CloseCode.BAD_CREDENTIAL


@asyncio_test
async def test_a_leaked_code_cannot_be_guessed_at_for_its_whole_lifetime():
    cell = make_cell(invite_max_attempts=3)
    session, _, _ = await connect_host(cell)
    invite = cell.create_invite(session, "device-1")

    for _ in range(3):
        _, connection = await cell.open_client(
            FakeSocket(), session.relay_host_id, invite.token
        )
        await cell.close_connection(session, connection, CloseCode.HOST_OFFLINE, "gave up")

    with pytest.raises(RelayRefusal) as refusal:
        await cell.open_client(FakeSocket(), session.relay_host_id, invite.token)
    assert refusal.value.code == CloseCode.TOO_MANY_REQUESTS


@asyncio_test
async def test_one_desktop_cannot_hold_more_of_the_cell_than_its_share():
    cell = make_cell(max_connections_per_host=2)
    session, _, _ = await connect_host(cell)
    for index in range(2):
        invite = cell.create_invite(session, f"device-{index}")
        await cell.open_client(FakeSocket(), session.relay_host_id, invite.token)

    spare = cell.create_invite(session, "device-spare")
    with pytest.raises(RelayRefusal) as refusal:
        await cell.open_client(FakeSocket(), session.relay_host_id, spare.token)
    assert refusal.value.code == CloseCode.LIMIT_EXCEEDED


@asyncio_test
async def test_closing_one_side_closes_the_other_and_frees_the_slot():
    cell = make_cell()
    session, _, client, data, connection, _ = await spliced(cell)

    await cell.close_connection(session, connection, CloseCode.HOST_OFFLINE, "host left")
    assert client.closed == (CloseCode.HOST_OFFLINE, "host left")
    assert data.closed == (CloseCode.HOST_OFFLINE, "host left")
    assert session.connections == {}
    # Idempotent: the adapter's `finally` runs on both sockets.
    await cell.close_connection(session, connection, CloseCode.HOST_OFFLINE, "again")


@asyncio_test
async def test_a_desktop_reconnecting_replaces_itself_rather_than_racing_itself():
    # Two live controls for one id would race every conn-open and the phone
    # would reach whichever won.
    cell = make_cell()
    session, _, host = await connect_host(cell)
    invite = cell.create_invite(session, "device-1")
    client = FakeSocket()
    _, connection = await cell.open_client(client, session.relay_host_id, invite.token)

    second, _, _ = await connect_host(cell, host)
    assert cell.session(host.relay_host_id) is second
    assert second.generation > session.generation
    assert connection.state is SpliceState.TEARDOWN
    assert client.closed is not None and client.closed[0] == CloseCode.HOST_OFFLINE


@asyncio_test
async def test_a_control_socket_that_died_reads_as_the_host_being_gone():
    cell = make_cell()
    session, control, _ = await connect_host(cell)
    invite = cell.create_invite(session, "device-1")
    control.fail_next_send = True

    with pytest.raises(RelayRefusal) as refusal:
        await cell.open_client(FakeSocket(), session.relay_host_id, invite.token)
    assert refusal.value.code == CloseCode.HOST_OFFLINE
    assert session.connections == {}


# --- getting back in ----------------------------------------------------------


@asyncio_test
async def test_a_resume_token_survives_the_desktop_being_quit_and_reopened():
    """The reason resume tokens exist at all.

    Invites hang off a host *session*, which dies whenever somebody quits Grid
    on the computer. A phone that had been paired for weeks therefore had to be
    handed a fresh code by hand every time the desktop restarted — which is
    exactly what it looked like from the phone: a blank "paste a code" screen.
    """
    cell = make_cell()
    host = FakeHost()
    session, _, _ = await connect_host(cell, host)
    resume = cell.create_resume(session.relay_host_id, "device-1")

    # The desktop goes away, taking its session and every invite with it.
    await cell.retire_session(session)
    reopened, control, _ = await connect_host(cell, host)

    _, connection = await cell.open_client(
        FakeSocket(), reopened.relay_host_id, resume.token
    )

    assert connection.relay_device_id == "device-1"
    assert last_json(control)["kind"] == "resume"


@asyncio_test
async def test_a_resume_token_can_be_used_more_than_once_unlike_an_invite():
    cell = make_cell()
    session, _, _ = await connect_host(cell)
    resume = cell.create_resume(session.relay_host_id, "device-1")

    for _ in range(3):
        _, connection = await cell.open_client(
            FakeSocket(), session.relay_host_id, resume.token
        )
        await cell.close_connection(
            session, connection, CloseCode.HOST_OFFLINE, "done"
        )


@asyncio_test
async def test_an_expired_resume_token_is_refused_like_any_other_bad_one():
    cell = make_cell()
    session, _, _ = await connect_host(cell)
    resume = cell.create_resume(session.relay_host_id, "device-1")

    # Aged by hand rather than by moving the cell's clock: the host proof this
    # session was built on is time-bound too, and winding the clock forward
    # expires *that* first — the test then passes for the wrong reason.
    resume.expires_at_ms = 0

    with pytest.raises(RelayRefusal) as refusal:
        await cell.open_client(FakeSocket(), session.relay_host_id, resume.token)
    assert refusal.value.code == CloseCode.BAD_CREDENTIAL


@asyncio_test
async def test_a_resume_token_does_not_open_a_different_computer():
    """It is kept per host id, so it is not a key to whatever is on this relay."""
    cell = make_cell()
    mine, _, _ = await connect_host(cell)
    resume = cell.create_resume(mine.relay_host_id, "device-1")
    theirs, _, _ = await connect_host(cell, FakeHost())

    with pytest.raises(RelayRefusal) as refusal:
        await cell.open_client(FakeSocket(), theirs.relay_host_id, resume.token)
    assert refusal.value.code == CloseCode.BAD_CREDENTIAL


@asyncio_test
async def test_a_resume_token_is_still_bounded_however_long_it_lives():
    cell = make_cell()
    session, _, _ = await connect_host(cell)
    resume = cell.create_resume(session.relay_host_id, "device-1")
    resume.attempts = cell.limits.resume_max_attempts

    with pytest.raises(RelayRefusal) as refusal:
        await cell.open_client(FakeSocket(), session.relay_host_id, resume.token)
    assert refusal.value.code == CloseCode.TOO_MANY_REQUESTS
