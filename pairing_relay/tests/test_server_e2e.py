"""The whole thing, over real sockets.

`test_cell.py` proves the rules with fakes, which is where they belong. This
file exists for the other half: that the adapter in `server.py` actually wires
those rules to a socket, in the order the wire says. A cell that is perfect and
an adapter that forgets to call it look identical from the inside.
"""
from __future__ import annotations

import base64
import json
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from pairing_relay.cell import Cell
from pairing_relay.host_proof import answer_challenge
from pairing_relay.host_proof import HostChallenge
from pairing_relay.protocol import PROTOCOL_VERSION, CloseCode
from pairing_relay.server import create_app
from pairing_relay.tests.support import RELAY_ORIGIN, FakeHost


@pytest.fixture
def client():
    with TestClient(create_app(Cell(relay_origin=RELAY_ORIGIN))) as test_client:
        yield test_client


def _authenticate(socket, host: FakeHost) -> dict:
    """Drive the desktop's side of the proof and return the hello-ack."""
    socket.send_text(json.dumps(host.hello()))
    challenge = HostChallenge.from_json(json.loads(socket.receive_text()))
    proof = answer_challenge(
        challenge,
        host_private_key=host.private_key,
        context=host.context(),
        now_ms=int(time.time() * 1000),
    )
    socket.send_text(
        json.dumps(
            {
                "type": "host-challenge-ack",
                "challengeId": challenge.challenge_id,
                "proofB64": proof,
            }
        )
    )
    return json.loads(socket.receive_text())


def _invite(socket) -> str:
    socket.send_text(
        json.dumps({"type": "invite-create", "reqId": "r1", "relayDeviceId": "phone-1"})
    )
    created = json.loads(socket.receive_text())
    assert created["type"] == "invite-created"
    return created["inviteToken"]


def test_a_phone_and_its_desktop_exchange_bytes_neither_the_relay_reads(client):
    host = FakeHost()
    with client.websocket_connect("/v1/host/control") as control:
        ack = _authenticate(control, host)
        assert ack["relayHostId"] == host.relay_host_id
        token = _invite(control)

        with client.websocket_connect(f"/v1/connect/{host.relay_host_id}") as phone:
            phone.send_text(
                json.dumps(
                    {"type": "relay-auth", "v": 1, "mode": "connect", "credential": token}
                )
            )
            opened = json.loads(control.receive_text())
            assert opened["type"] == "conn-open"

            with client.websocket_connect(f"/v1/host/data/{opened['connId']}") as data:
                data.send_text(
                    json.dumps(
                        {
                            "type": "host-data-auth",
                            "v": PROTOCOL_VERSION,
                            "connTicket": opened["connTicket"],
                            "generation": ack["generation"],
                        }
                    )
                )
                hello = json.loads(phone.receive_text())
                assert hello["ok"] is True

                # Sealed bytes as far as the relay is concerned. They happen to
                # spell one of its own control messages, and it forwards them
                # without a glance.
                sealed = json.dumps({"type": "conn-open", "connId": "spoof"}).encode()
                phone.send_bytes(sealed)
                assert data.receive_bytes() == sealed

                data.send_bytes(b"\x00\xff\xfe reply")
                assert phone.receive_bytes() == b"\x00\xff\xfe reply"


def test_a_phone_arriving_before_its_desktop_is_told_the_desktop_is_offline(client):
    host = FakeHost()
    with client.websocket_connect(f"/v1/connect/{host.relay_host_id}") as phone:
        phone.send_text(
            json.dumps({"type": "relay-auth", "v": 1, "mode": "connect", "credential": "x"})
        )
        # The code arrives as a message first, so the app has something to show,
        # and only then as a close.
        refusal = json.loads(phone.receive_text())
        assert refusal == {"type": "relay-hello", "ok": False, "code": CloseCode.HOST_OFFLINE}
        with pytest.raises(WebSocketDisconnect) as disconnect:
            phone.receive_text()
        assert disconnect.value.code == CloseCode.HOST_OFFLINE


def test_a_desktop_that_fails_the_proof_never_registers(client):
    host, impostor = FakeHost(), FakeHost()
    with client.websocket_connect("/v1/host/control") as control:
        # Claim the real host's id, answer with the wrong key.
        control.send_text(json.dumps(host.hello()))
        challenge = HostChallenge.from_json(json.loads(control.receive_text()))
        control.send_text(
            json.dumps(
                {
                    "type": "host-challenge-ack",
                    "challengeId": challenge.challenge_id,
                    "proofB64": base64.b64encode(b"n" * 32).decode(),
                }
            )
        )
        with pytest.raises(WebSocketDisconnect) as disconnect:
            control.receive_text()
        assert disconnect.value.code == CloseCode.BAD_CREDENTIAL

    # And the id is still free, which is the point: a failed claim leaves nothing.
    with client.websocket_connect("/v1/connect/" + host.relay_host_id) as phone:
        phone.send_text(
            json.dumps({"type": "relay-auth", "v": 1, "mode": "connect", "credential": "x"})
        )
        assert json.loads(phone.receive_text())["code"] == CloseCode.HOST_OFFLINE
    assert impostor.relay_host_id != host.relay_host_id


def test_the_desktop_hanging_up_takes_its_phone_with_it(client):
    """A phone cannot tell a quiet desktop from a dead splice, so the cell has
    to close the socket rather than leave it looking alive."""
    host = FakeHost()
    with client.websocket_connect("/v1/host/control") as control:
        ack = _authenticate(control, host)
        token = _invite(control)
        with client.websocket_connect(f"/v1/connect/{host.relay_host_id}") as phone:
            phone.send_text(
                json.dumps(
                    {"type": "relay-auth", "v": 1, "mode": "connect", "credential": token}
                )
            )
            opened = json.loads(control.receive_text())
            with client.websocket_connect(f"/v1/host/data/{opened['connId']}") as data:
                data.send_text(
                    json.dumps(
                        {
                            "type": "host-data-auth",
                            "v": PROTOCOL_VERSION,
                            "connTicket": opened["connTicket"],
                            "generation": ack["generation"],
                        }
                    )
                )
                assert json.loads(phone.receive_text())["ok"] is True

            # The desktop's data socket has gone.
            with pytest.raises(WebSocketDisconnect):
                phone.receive_bytes()


def test_a_socket_that_connects_and_says_nothing_does_not_hold_a_slot(client):
    with pytest.raises(WebSocketDisconnect) as disconnect:
        with client.websocket_connect("/v1/host/control") as control:
            control.receive_text()
    assert disconnect.value.code == CloseCode.BAD_CREDENTIAL
