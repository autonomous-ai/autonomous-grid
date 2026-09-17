"""The cell: everything that happens between a phone arriving and bytes moving.

Written against an abstract [Socket] rather than a WebSocket on purpose. The
interesting failures here are ordering and lifetime — a data socket attaching
to the wrong connection, a phone told it is through before forwarding exists,
an attach deadline that fires after the attach — and every one of them is
reproducible with two objects in a list. Put a real socket in the middle and
those tests become slow, flaky, and about the network instead.

The adapter that owns real sockets is `server.py`, and it is deliberately thin.

**No send queue, anywhere.** A forward is `await peer.send(...)`, so a slow
phone slows the desktop's reader and nothing accumulates: memory is one frame
per direction, full stop. Orca's cell carries a queue and a byte budget because
Node's `ws.send` buffers without awaiting, so backpressure there has to be
built; asyncio gives it for free. What has to be built here instead is the
other half — a peer that never drains would otherwise block its reader forever
— and that is [Limits.splice_send_timeout_s].
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Protocol

from pairing_relay.host_proof import (
    HostChallenge,
    PendingChallenge,
    derive_relay_host_id,
    issue_challenge,
    verify_proof,
)
from pairing_relay.protocol import (
    DEFAULT_LIMITS,
    PROTOCOL_VERSION,
    CloseCode,
    Limits,
    RelayRefusal,
)
from pairing_relay.splice_state import SpliceState, advance, may_acknowledge_client


class Socket(Protocol):
    """The three things the cell ever does to a peer."""

    async def send_text(self, data: str) -> None: ...

    async def send_bytes(self, data: bytes) -> None: ...

    async def close(self, code: int, reason: str = "") -> None: ...


@dataclass
class Invite:
    """A pairing code's server half. One connection's worth of authority."""

    token: str
    relay_device_id: str
    expires_at_ms: int
    attempts: int = 0
    consumed: bool = False


@dataclass
class Resume:
    """A phone's way back in after its invite is long gone.

    Kept on the **host id**, not the host session. That is the whole point: a
    session dies every time somebody quits Grid on the computer, and with it
    died every invite hanging off it — so a phone could not reconnect after a
    desktop restart no matter how long its credential said it was good for.
    A host id is derived from the desktop's public key, so it is the same one
    tomorrow.

    Reusable, unlike an invite. It is not a code on a screen; it is the
    credential a phone that has already authenticated was handed, in private,
    through the channel it authenticated on.
    """

    token: str
    relay_device_id: str
    expires_at_ms: int
    attempts: int = 0


@dataclass
class Connection:
    """One phone's socket and the desktop socket it is being joined to."""

    conn_id: str
    conn_ticket: str
    relay_device_id: str
    invite_token: str
    client: Socket
    state: SpliceState = SpliceState.PRE_AUTH_ADMITTED
    host: Socket | None = None
    forwarding_installed: bool = False
    attach_timer: asyncio.Task[None] | None = None

    def advance_to(self, target: SpliceState) -> None:
        self.state = advance(self.state, target)


@dataclass
class HostSession:
    """A desktop's live control channel and everything hanging off it."""

    relay_host_id: str
    host_public_key: bytes
    control: Socket
    generation: int
    invites: dict[str, Invite] = field(default_factory=dict)
    connections: dict[str, Connection] = field(default_factory=dict)


@dataclass(frozen=True)
class PendingHostHello:
    """A control socket mid-proof. Held by the caller, so nothing leaks here
    when a desktop hangs up between the challenge and its answer."""

    relay_host_id: str
    host_public_key: bytes
    challenge: HostChallenge
    pending: PendingChallenge


def _now_ms() -> int:
    return int(time.time() * 1000)


class Cell:
    """One relay cell. In-memory, single process, no director, no database."""

    def __init__(
        self,
        *,
        relay_origin: str,
        limits: Limits = DEFAULT_LIMITS,
        now_ms=_now_ms,
        new_token=lambda: secrets.token_urlsafe(32),
    ) -> None:
        self._relay_origin = relay_origin
        self._limits = limits
        self._now_ms = now_ms
        self._new_token = new_token
        self._sessions: dict[str, HostSession] = {}
        # Keyed by relay host id and deliberately outside `_sessions`: these
        # have to outlive a desktop being quit and reopened. See `Resume`.
        self._resumes: dict[str, dict[str, Resume]] = {}
        self._generation = 0

    @property
    def limits(self) -> Limits:
        return self._limits

    @property
    def relay_origin(self) -> str:
        """What a host proof is bound to. Both sides must spell it identically."""
        return self._relay_origin

    def session(self, relay_host_id: str) -> HostSession | None:
        return self._sessions.get(relay_host_id)

    # --- the desktop's control channel ---------------------------------------

    def begin_host_hello(self, hello: dict) -> PendingHostHello:
        """Answer a `host-hello` with a challenge only its real owner can pass."""
        if hello.get("type") != "host-hello" or hello.get("v") != PROTOCOL_VERSION:
            raise RelayRefusal(CloseCode.BAD_CREDENTIAL, "bad host-hello")
        try:
            host_public_key = base64.b64decode(str(hello["hostPublicKeyB64"]), validate=True)
        except Exception as error:
            raise RelayRefusal(CloseCode.BAD_CREDENTIAL, "bad host key") from error
        if len(host_public_key) != 32:
            raise RelayRefusal(CloseCode.BAD_CREDENTIAL, "bad host key")
        relay_host_id = derive_relay_host_id(host_public_key)
        # The id is derived, never taken on the desktop's word. A hello that
        # claims a different one is either a bug or someone reaching for an id
        # that is not theirs; both end here rather than later.
        if hello.get("relayHostId") != relay_host_id:
            raise RelayRefusal(CloseCode.BAD_CREDENTIAL, "host id does not match key")
        challenge, pending = issue_challenge(
            relay_origin=self._relay_origin,
            relay_host_id=relay_host_id,
            host_public_key=host_public_key,
            now_ms=self._now_ms(),
            ttl_ms=int(self._limits.host_challenge_ttl_s * 1000),
        )
        return PendingHostHello(relay_host_id, host_public_key, challenge, pending)

    async def complete_host_hello(
        self, waiting: PendingHostHello, ack: dict, control: Socket
    ) -> HostSession:
        """Register the desktop, once its answer checks out."""
        if (
            ack.get("type") != "host-challenge-ack"
            or ack.get("challengeId") != waiting.challenge.challenge_id
            or not isinstance(ack.get("proofB64"), str)
            or not verify_proof(waiting.pending, ack["proofB64"], now_ms=self._now_ms())
        ):
            raise RelayRefusal(CloseCode.BAD_CREDENTIAL, "host proof failed")
        # A desktop that reconnects supersedes its own older session rather than
        # sitting beside it: two live controls for one id would race every
        # conn-open, and the phone would see whichever won.
        previous = self._sessions.pop(waiting.relay_host_id, None)
        if previous is not None:
            await self._retire(previous)
        self._generation += 1
        session = HostSession(
            relay_host_id=waiting.relay_host_id,
            host_public_key=waiting.host_public_key,
            control=control,
            generation=self._generation,
        )
        self._sessions[session.relay_host_id] = session
        return session

    def create_invite(self, session: HostSession, relay_device_id: str) -> Invite:
        """Mint the server half of a pairing code."""
        invite = Invite(
            token=self._new_token(),
            relay_device_id=relay_device_id,
            expires_at_ms=self._now_ms() + int(self._limits.invite_ttl_s * 1000),
        )
        session.invites[invite.token] = invite
        return invite

    def create_resume(self, relay_host_id: str, relay_device_id: str) -> Resume:
        """Mint a phone's way back in, good across desktop restarts."""
        self._drop_expired_resumes(relay_host_id)
        resume = Resume(
            token=self._new_token(),
            relay_device_id=relay_device_id,
            expires_at_ms=self._now_ms() + int(self._limits.resume_ttl_s * 1000),
        )
        self._resumes.setdefault(relay_host_id, {})[resume.token] = resume
        return resume

    def _drop_expired_resumes(self, relay_host_id: str) -> None:
        now = self._now_ms()
        held = self._resumes.get(relay_host_id)
        if held is None:
            return
        for token, resume in list(held.items()):
            if now > resume.expires_at_ms:
                del held[token]

    def _authorize(
        self, session: HostSession, relay_host_id: str, credential: str
    ) -> tuple[str, str, str]:
        """Which credential this is, whose device it names, and its token.

        Refuses with one message for every kind of bad credential: an unknown
        token, a spent one and an expired one are the same answer to whoever is
        holding it, and the difference only helps someone who should not be here.
        """
        now = self._now_ms()
        invite = session.invites.get(credential)
        if invite is not None:
            if invite.consumed or now > invite.expires_at_ms:
                raise RelayRefusal(CloseCode.BAD_CREDENTIAL, "invalid credential")
            if invite.attempts >= self._limits.invite_max_attempts:
                raise RelayRefusal(CloseCode.TOO_MANY_REQUESTS, "invite exhausted")
            # Counted before the attempt, not after: a try that dies halfway
            # still spent a guess, and only counting the completed ones makes
            # the cap free to walk past.
            invite.attempts += 1
            return "invite", invite.relay_device_id, invite.token

        resume = self._resumes.get(relay_host_id, {}).get(credential)
        if resume is None or now > resume.expires_at_ms:
            raise RelayRefusal(CloseCode.BAD_CREDENTIAL, "invalid credential")
        if resume.attempts >= self._limits.resume_max_attempts:
            raise RelayRefusal(CloseCode.TOO_MANY_REQUESTS, "resume exhausted")
        resume.attempts += 1
        # Not consumed: this is the credential that has to still work tomorrow.
        return "resume", resume.relay_device_id, resume.token

    async def retire_session(self, session: HostSession) -> None:
        """Drop a desktop and every phone hanging off it."""
        if self._sessions.get(session.relay_host_id) is session:
            del self._sessions[session.relay_host_id]
        await self._retire(session)

    async def _retire(self, session: HostSession) -> None:
        for connection in list(session.connections.values()):
            await self.close_connection(session, connection, CloseCode.HOST_OFFLINE, "host left")

    # --- a phone arriving ----------------------------------------------------

    async def open_client(
        self, client: Socket, relay_host_id: str, credential: str
    ) -> tuple[HostSession, Connection]:
        """Reserve the phone's credential and ask its desktop to attach."""
        session = self._sessions.get(relay_host_id)
        if session is None:
            # The one refusal that names its cause, because it is the one the
            # phone's owner can do something about: open Grid on the computer.
            raise RelayRefusal(CloseCode.HOST_OFFLINE, "host offline")
        # An invite first, then a resume token. Two credentials, one door: the
        # phone presents whichever it holds and does not have to know which kind
        # the relay will recognise.
        kind, relay_device_id, token = self._authorize(session, relay_host_id, credential)
        if len(session.connections) >= self._limits.max_connections_per_host:
            raise RelayRefusal(CloseCode.LIMIT_EXCEEDED, "too many connections")

        connection = Connection(
            conn_id=self._new_token(),
            conn_ticket=self._new_token(),
            relay_device_id=relay_device_id,
            invite_token=token,
            client=client,
        )
        connection.advance_to(SpliceState.CREDENTIAL_RESERVED)
        session.connections[connection.conn_id] = connection
        try:
            await session.control.send_text(
                json.dumps(
                    {
                        "type": "conn-open",
                        "connId": connection.conn_id,
                        "connTicket": connection.conn_ticket,
                        "kind": kind,
                        "relayDeviceId": connection.relay_device_id,
                        "attachDeadlineMs": int(self._limits.host_attach_deadline_s * 1000),
                    }
                )
            )
        except Exception as error:
            # The control socket died between the lookup and the write. The
            # desktop is gone; say so rather than leaving the phone on a
            # connection nothing will ever attach to.
            session.connections.pop(connection.conn_id, None)
            connection.advance_to(SpliceState.TEARDOWN)
            raise RelayRefusal(CloseCode.HOST_OFFLINE, "host offline") from error
        connection.advance_to(SpliceState.HOST_NOTIFIED)
        connection.advance_to(SpliceState.ATTACH_PENDING)
        connection.attach_timer = asyncio.create_task(self._expire_attach(session, connection))
        return session, connection

    async def _expire_attach(self, session: HostSession, connection: Connection) -> None:
        try:
            await asyncio.sleep(self._limits.host_attach_deadline_s)
        except asyncio.CancelledError:
            return
        await self.close_connection(
            session, connection, CloseCode.ATTACH_TIMEOUT, "host did not attach"
        )

    # --- the desktop answering it --------------------------------------------

    async def attach_host_data(
        self, host: Socket, conn_id: str, ticket: str, generation: int
    ) -> tuple[HostSession, Connection]:
        """Join the desktop's data socket to the phone waiting on [conn_id]."""
        for session in self._sessions.values():
            connection = session.connections.get(conn_id)
            if connection is None:
                continue
            # A data socket from a control session that has since been replaced
            # must not attach: its desktop no longer owns this connection.
            if session.generation != generation:
                raise RelayRefusal(CloseCode.BAD_CREDENTIAL, "stale generation")
            if not hmac.compare_digest(connection.conn_ticket, ticket):
                raise RelayRefusal(CloseCode.BAD_CREDENTIAL, "bad ticket")
            if connection.state is not SpliceState.ATTACH_PENDING:
                raise RelayRefusal(CloseCode.BAD_CREDENTIAL, "not awaiting attach")
            return session, await self._splice(session, connection, host)
        raise RelayRefusal(CloseCode.BAD_CREDENTIAL, "unknown connection")

    async def _splice(
        self, session: HostSession, connection: Connection, host: Socket
    ) -> Connection:
        if connection.attach_timer is not None:
            connection.attach_timer.cancel()
            connection.attach_timer = None
        connection.host = host
        connection.advance_to(SpliceState.HOST_ATTACHED)
        # Wire first, acknowledge second. The other order hands the phone a
        # socket that looks connected and silently drops what it writes, and a
        # phone cannot tell that apart from a desktop that is merely quiet.
        connection.forwarding_installed = True
        if not may_acknowledge_client(connection.state, connection.forwarding_installed):
            await self.close_connection(
                session, connection, CloseCode.LIMIT_EXCEEDED, "splice not ready"
            )
            raise RelayRefusal(CloseCode.LIMIT_EXCEEDED, "splice not ready")
        invite = session.invites.get(connection.invite_token)
        await connection.client.send_text(
            json.dumps(
                {
                    "type": "relay-hello",
                    "ok": True,
                    "credentialKind": "invite",
                    "leaseExpiresAt": invite.expires_at_ms if invite else self._now_ms(),
                }
            )
        )
        connection.advance_to(SpliceState.CLIENT_ACKNOWLEDGED)
        connection.advance_to(SpliceState.SPLICED)
        if invite is not None:
            # One connection's worth of authority, spent. A phone that drops
            # needs a fresh code — this cell issues no resume credential, which
            # is the single biggest thing it leaves out. See README.
            invite.consumed = True
        return connection

    # --- bytes ---------------------------------------------------------------

    async def forward_from_client(
        self, session: HostSession, connection: Connection, data: str | bytes
    ) -> None:
        await self._forward(session, connection, connection.host, data)

    async def forward_from_host(
        self, session: HostSession, connection: Connection, data: str | bytes
    ) -> None:
        await self._forward(session, connection, connection.client, data)

    async def _forward(
        self,
        session: HostSession,
        connection: Connection,
        peer: Socket | None,
        data: str | bytes,
    ) -> None:
        if connection.state is not SpliceState.SPLICED or peer is None:
            raise RelayRefusal(CloseCode.LIMIT_EXCEEDED, "not spliced")
        if len(data) > self._limits.max_frame_bytes:
            # The stream is opaque, so there is no "drop this one and carry on":
            # the peers are counting frames, and a hole is worse than a close.
            await self.close_connection(
                session, connection, CloseCode.LIMIT_EXCEEDED, "frame too large"
            )
            raise RelayRefusal(CloseCode.LIMIT_EXCEEDED, "frame too large")
        send = peer.send_bytes if isinstance(data, (bytes, bytearray)) else peer.send_text
        try:
            await asyncio.wait_for(send(data), self._limits.splice_send_timeout_s)
        except TimeoutError as error:
            await self.close_connection(
                session, connection, CloseCode.LIMIT_EXCEEDED, "peer not draining"
            )
            raise RelayRefusal(CloseCode.LIMIT_EXCEEDED, "peer not draining") from error
        except Exception as error:
            # Broad on purpose: the peer is a transport this cell does not own,
            # and every way its send can fail means the same thing — it is gone.
            await self.close_connection(session, connection, CloseCode.HOST_OFFLINE, "peer gone")
            raise RelayRefusal(CloseCode.HOST_OFFLINE, "peer gone") from error

    async def close_connection(
        self, session: HostSession, connection: Connection, code: int, reason: str
    ) -> None:
        """End one connection and both its sockets. Safe to call twice."""
        if connection.state is SpliceState.TEARDOWN:
            return
        connection.advance_to(SpliceState.TEARDOWN)
        connection.forwarding_installed = False
        if connection.attach_timer is not None:
            connection.attach_timer.cancel()
            connection.attach_timer = None
        session.connections.pop(connection.conn_id, None)
        for socket in (connection.client, connection.host):
            if socket is None:
                continue
            try:
                await socket.close(code, reason)
            except Exception:
                # A peer that is already gone is the normal way this ends.
                pass
