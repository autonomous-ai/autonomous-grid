"""FastAPI in front of the cell. Three sockets, and as little judgement as possible.

Everything that decides anything lives in `cell.py`. This file exists to turn a
Starlette WebSocket into the three-method [Socket] the cell talks to, to read
first frames under a deadline, and to make sure a disconnect on either side
reaches the cell instead of leaving a connection half-alive.

Route             | who dials it | first frame
------------------|--------------|----------------------------------------
/v1/host/control  | desktop      | host-hello, then answers a challenge
/v1/host/data/:id | desktop      | host-data-auth, then raw splice
/v1/connect/:host | phone        | relay-auth, then raw splice

The phone never learns the desktop's address and the desktop never learns the
phone's: both dial *out* to here, which is the whole reason this service exists.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from pairing_relay.cell import Cell, Connection, HostSession
from pairing_relay.protocol import PROTOCOL_VERSION, CloseCode, RelayRefusal

DEFAULT_ORIGIN = os.environ.get("GRID_PAIRING_RELAY_ORIGIN", "ws://127.0.0.1:8787")


class WebSocketPeer:
    """One real socket, wearing the cell's three-method interface."""

    def __init__(self, socket: WebSocket) -> None:
        self._socket = socket

    async def send_text(self, data: str) -> None:
        await self._socket.send_text(data)

    async def send_bytes(self, data: bytes) -> None:
        await self._socket.send_bytes(data)

    async def close(self, code: int, reason: str = "") -> None:
        try:
            await self._socket.close(code=code, reason=reason)
        except Exception:
            # Already gone. The cell's state is what matters and it is updated.
            pass


async def _first_frame(socket: WebSocket, deadline_s: float) -> dict[str, Any]:
    """The opening JSON, or a refusal. A socket that connects and says nothing
    is a socket holding a slot, so this never waits indefinitely."""
    try:
        raw = await asyncio.wait_for(socket.receive_text(), deadline_s)
    except (TimeoutError, WebSocketDisconnect, KeyError, RuntimeError) as error:
        raise RelayRefusal(CloseCode.BAD_CREDENTIAL, "no first frame") from error
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RelayRefusal(CloseCode.BAD_CREDENTIAL, "first frame is not JSON") from error
    if not isinstance(value, dict):
        raise RelayRefusal(CloseCode.BAD_CREDENTIAL, "first frame is not an object")
    return value


async def _next_payload(socket: WebSocket) -> str | bytes | None:
    """The next frame either way round, or None once the peer has gone."""
    try:
        message = await socket.receive()
    except (WebSocketDisconnect, RuntimeError):
        return None
    if message.get("type") == "websocket.disconnect":
        return None
    text = message.get("text")
    if text is not None:
        return text
    payload = message.get("bytes")
    return payload if payload is not None else None


def create_app(cell: Cell | None = None) -> FastAPI:
    """The relay, over [cell] — a fresh in-memory one when none is given."""
    relay = cell or Cell(relay_origin=DEFAULT_ORIGIN)
    app = FastAPI(title="Grid pairing relay (minimal)")
    app.state.cell = relay

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {"ok": True, "origin": relay.relay_origin}

    @app.websocket("/v1/host/control")
    async def host_control(socket: WebSocket) -> None:
        await socket.accept()
        peer = WebSocketPeer(socket)
        session: HostSession | None = None
        try:
            hello = await _first_frame(socket, relay.limits.first_frame_deadline_s)
            waiting = relay.begin_host_hello(hello)
            await socket.send_text(json.dumps(waiting.challenge.to_json()))
            ack = await _first_frame(socket, relay.limits.host_challenge_ttl_s)
            session = await relay.complete_host_hello(waiting, ack, peer)
            await socket.send_text(
                json.dumps(
                    {
                        "type": "host-hello-ack",
                        "v": PROTOCOL_VERSION,
                        "generation": session.generation,
                        "relayHostId": session.relay_host_id,
                    }
                )
            )
            await _control_loop(relay, socket, session)
        except RelayRefusal as refusal:
            await peer.close(refusal.code, refusal.reason)
        except WebSocketDisconnect:
            pass
        finally:
            if session is not None:
                await relay.retire_session(session)

    @app.websocket("/v1/host/data/{conn_id}")
    async def host_data(socket: WebSocket, conn_id: str) -> None:
        await socket.accept()
        peer = WebSocketPeer(socket)
        pair: tuple[HostSession, Connection] | None = None
        try:
            auth = await _first_frame(socket, relay.limits.first_frame_deadline_s)
            if auth.get("type") != "host-data-auth" or auth.get("v") != PROTOCOL_VERSION:
                raise RelayRefusal(CloseCode.BAD_CREDENTIAL, "bad host-data-auth")
            pair = await relay.attach_host_data(
                peer,
                conn_id,
                str(auth.get("connTicket", "")),
                int(auth.get("generation", -1)),
            )
            await _pump(relay, socket, pair, from_host=True)
        except RelayRefusal as refusal:
            await peer.close(refusal.code, refusal.reason)
        except WebSocketDisconnect:
            pass
        finally:
            if pair is not None:
                await relay.close_connection(*pair, CloseCode.HOST_OFFLINE, "host data closed")

    @app.websocket("/v1/connect/{relay_host_id}")
    async def connect(socket: WebSocket, relay_host_id: str) -> None:
        await socket.accept()
        peer = WebSocketPeer(socket)
        pair: tuple[HostSession, Connection] | None = None
        try:
            auth = await _first_frame(socket, relay.limits.first_frame_deadline_s)
            if auth.get("type") != "relay-auth" or auth.get("mode") != "connect":
                raise RelayRefusal(CloseCode.BAD_CREDENTIAL, "bad relay-auth")
            pair = await relay.open_client(
                peer, relay_host_id, str(auth.get("credential", ""))
            )
            await _pump(relay, socket, pair, from_host=False)
        except RelayRefusal as refusal:
            # The phone is told the code before the socket dies, so the app can
            # show a sentence instead of "connection closed".
            try:
                await socket.send_text(
                    json.dumps({"type": "relay-hello", "ok": False, "code": refusal.code})
                )
            except Exception:
                pass
            await peer.close(refusal.code, refusal.reason)
        except WebSocketDisconnect:
            pass
        finally:
            if pair is not None:
                await relay.close_connection(*pair, CloseCode.HOST_OFFLINE, "client closed")

    return app


async def _control_loop(relay: Cell, socket: WebSocket, session: HostSession) -> None:
    """Serve the desktop's requests, and notice when it stops answering."""
    last_inbound = time.monotonic()

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(relay.limits.control_ping_interval_s)
            if time.monotonic() - last_inbound > relay.limits.control_silence_timeout_s:
                # A half-open control socket holds a host id hostage: the phone
                # is told its desktop is online and then never attached to.
                await socket.close(code=CloseCode.DRAINING, reason="control silent")
                return
            await socket.send_text(json.dumps({"type": "ping", "t": int(time.time() * 1000)}))

    pinger = asyncio.create_task(heartbeat())
    try:
        while True:
            payload = await _next_payload(socket)
            if payload is None:
                return
            last_inbound = time.monotonic()
            if not isinstance(payload, str):
                raise RelayRefusal(CloseCode.BAD_CREDENTIAL, "binary control frame")
            await _handle_control(relay, socket, session, json.loads(payload))
    finally:
        pinger.cancel()


async def _handle_control(
    relay: Cell, socket: WebSocket, session: HostSession, message: dict[str, Any]
) -> None:
    kind = message.get("type")
    if kind == "pong":
        return
    if kind == "invite-create":
        invite = relay.create_invite(session, str(message.get("relayDeviceId", "")))
        await socket.send_text(
            json.dumps(
                {
                    "type": "invite-created",
                    "reqId": message.get("reqId"),
                    "inviteToken": invite.token,
                    "expiresAt": invite.expires_at_ms,
                    "maxAttempts": relay.limits.invite_max_attempts,
                }
            )
        )
        return
    if kind == "resume-create":
        resume = relay.create_resume(
            session.relay_host_id, str(message.get("relayDeviceId", ""))
        )
        await socket.send_text(
            json.dumps(
                {
                    "type": "resume-created",
                    "reqId": message.get("reqId"),
                    "inviteToken": resume.token,
                    "expiresAt": resume.expires_at_ms,
                    "maxAttempts": relay.limits.resume_max_attempts,
                }
            )
        )
        return
    # Drop an unrecognised control frame rather than closing on it. Orca learned
    # this the expensive way: self-closing on one stray frame orphaned the relay
    # session and answered the phone HOST_OFFLINE for minutes afterwards.
    print(f"[pairing-relay] ignoring control frame type={kind!r}")


async def _pump(
    relay: Cell,
    socket: WebSocket,
    pair: tuple[HostSession, Connection],
    *,
    from_host: bool,
) -> None:
    """Read one side of a splice and hand every frame to the other."""
    session, connection = pair
    forward = relay.forward_from_host if from_host else relay.forward_from_client
    while True:
        payload = await _next_payload(socket)
        if payload is None:
            return
        await forward(session, connection, payload)
