"""Doubles and shortcuts the cell's tests share.

Fakes, not mocks: a [FakeSocket] records what the cell actually sent, so a test
asserts on the bytes a real peer would have seen rather than on which method
was called.
"""
from __future__ import annotations

import asyncio
import base64
import functools
import time

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from pairing_relay.cell import Cell, HostSession
from pairing_relay.host_proof import (
    HostProofContext,
    answer_challenge,
    derive_relay_host_id,
)
from pairing_relay.protocol import PROTOCOL_VERSION

RELAY_ORIGIN = "ws://relay.test:8787"


def asyncio_test(fn):
    """Run an async test under plain pytest.

    Deliberately not pytest-asyncio: it is not in this repo's dev dependencies,
    and one three-line decorator is cheaper than making every contributor's
    environment grow a plugin to run four files.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


class FakeSocket:
    """A peer that remembers, and that can be made to have gone away."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.frames: list[bytes] = []
        self.closed: tuple[int, str] | None = None
        self.stall_s: float = 0.0
        self.fail_next_send = False

    async def send_text(self, data: str) -> None:
        await self._before_send()
        self.texts.append(data)

    async def send_bytes(self, data: bytes) -> None:
        await self._before_send()
        self.frames.append(data)

    async def close(self, code: int, reason: str = "") -> None:
        self.closed = (code, reason)

    async def _before_send(self) -> None:
        if self.fail_next_send:
            self.fail_next_send = False
            raise ConnectionResetError("peer gone")
        if self.stall_s:
            await asyncio.sleep(self.stall_s)


class FakeHost:
    """A desktop's keys and the id they own."""

    def __init__(self) -> None:
        key = X25519PrivateKey.generate()
        self.private_key = key.private_bytes_raw()
        self.public_key = key.public_key().public_bytes_raw()
        self.relay_host_id = derive_relay_host_id(self.public_key)

    def hello(self) -> dict[str, object]:
        return {
            "type": "host-hello",
            "v": PROTOCOL_VERSION,
            "relayHostId": self.relay_host_id,
            "hostPublicKeyB64": base64.b64encode(self.public_key).decode(),
        }

    def context(self, relay_origin: str = RELAY_ORIGIN) -> HostProofContext:
        return HostProofContext(
            relay_origin=relay_origin,
            relay_host_id=self.relay_host_id,
            host_public_key=self.public_key,
        )


async def connect_host(
    cell: Cell, host: FakeHost | None = None
) -> tuple[HostSession, FakeSocket, FakeHost]:
    """A desktop through the whole proof, ready to be asked for invites."""
    host = host or FakeHost()
    waiting = cell.begin_host_hello(host.hello())
    proof = answer_challenge(
        waiting.challenge,
        host_private_key=host.private_key,
        context=host.context(cell.relay_origin),
        now_ms=int(time.time() * 1000),
    )
    control = FakeSocket()
    session = await cell.complete_host_hello(
        waiting,
        {
            "type": "host-challenge-ack",
            "challengeId": waiting.challenge.challenge_id,
            "proofB64": proof,
        },
        control,
    )
    return session, control, host


def last_json(socket: FakeSocket) -> dict:
    """The most recent message a peer was sent, decoded."""
    import json

    assert socket.texts, "nothing was sent to this peer"
    return json.loads(socket.texts[-1])
