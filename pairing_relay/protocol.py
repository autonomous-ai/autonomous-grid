"""What the phone, the desktop and this cell agree on before any of them speak.

Every number here is a refusal the relay owes somebody. They live in one place
because the interesting failures are the ones where two sides disagree about a
limit: the desktop thinks it has 30 seconds to attach, the cell gives it 10,
and the phone sees a host that is plainly running report itself as offline.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final

#: Bumped when a message shape changes in a way an older peer cannot read.
PROTOCOL_VERSION: Final = 1

#: Handshake nonce, public key and derived-key length, shared with the app's
#: `lib/infrastructure/pairing/` — 32 bytes everywhere.
KEY_BYTES: Final = 32


class CloseCode:
    """Why the cell hung up.

    Private-use range (4000-4999), so none of these can be confused with a
    WebSocket protocol close.

    **Exactly two of them name a cause, and both are ones a phone's owner can
    act on.** [HOST_OFFLINE] means no desktop is connected at all — "open Grid
    on your computer". [ATTACH_TIMEOUT] means one *is* connected but did not
    bring up the data socket in time — "Grid is running but not answering", a
    different sentence and a different fix. Orca spends one code on both cases
    and its own comment admits the second is the first wearing a borrowed name;
    a phone that cannot tell them apart has to guess which advice to give.

    The rest stay vague on purpose. Telling an attacker which half of a
    credential was wrong is free help.
    """

    BAD_CREDENTIAL: Final = 4401
    HOST_OFFLINE: Final = 4404
    ATTACH_TIMEOUT: Final = 4408
    LIMIT_EXCEEDED: Final = 4409
    TOO_MANY_REQUESTS: Final = 4429
    DRAINING: Final = 4503


@dataclass(frozen=True)
class Limits:
    """Bounds the cell enforces. Every one of them ends a connection, not a frame."""

    #: A socket that connects and says nothing is a socket holding a slot.
    first_frame_deadline_s: float = 2.0

    #: The splice is opaque, so the cell cannot ask an oversized frame to be
    #: smaller — it can only refuse the session. 8 MiB is chosen the way Orca
    #: chose it: a desktop's worktree catalogue already passes 1 MiB and grows.
    max_frame_bytes: int = 8 * 1024 * 1024

    #: Phones per desktop. A cap, not a quota: it bounds one host's share of
    #: the cell, and eight is more devices than a person pairs.
    max_connections_per_host: int = 8

    #: How long the desktop has to bring up its data socket after being told a
    #: phone is waiting. Past this the phone is answered HOST_OFFLINE, which is
    #: a lie about *why* but true about the outcome.
    host_attach_deadline_s: float = 10.0

    #: A pairing code on a screen nobody scanned must stop being a credential.
    invite_ttl_s: float = 600.0

    #: Tries per invite, so a leaked code cannot be brute-forced for ten minutes.
    invite_max_attempts: int = 5

    #: How long a *resume* token lasts, and why it is not the ten minutes above.
    #:
    #: An invite is a pairing code on a screen: nobody has proved anything yet,
    #: so it has to stop being a credential quickly. A resume token is the
    #: opposite — it is handed to a device that has already completed the
    #: handshake and proved its per-device token *inside* the sealed channel,
    #: and it is delivered through that channel rather than shown to a room.
    #:
    #: Ten minutes for this was the bug: a phone closed for eleven minutes
    #: could not get back in, and the person had to copy a code from the
    #: computer every single time.
    resume_ttl_s: float = 30 * 24 * 3600.0

    #: Tries per resume token. Higher than an invite's because this one is meant
    #: to be used again and again — but still bounded, so a leaked token is not
    #: an unlimited door.
    resume_max_attempts: int = 2000

    #: A single forward that blocks this long means the peer is gone or wedged.
    #: See README: this cell has no send queue, so the timeout *is* the memory
    #: bound — one frame in flight per direction, never a backlog.
    splice_send_timeout_s: float = 20.0

    #: Control channel: ping this often, hang up after this much silence.
    control_ping_interval_s: float = 15.0
    control_silence_timeout_s: float = 75.0

    #: A challenge older than this is refused even if its signature is perfect.
    host_challenge_ttl_s: float = 10.0

    #: Tolerated clock difference between this cell and a desktop. Without it a
    #: machine a second ahead fails every proof it ever makes.
    host_challenge_skew_s: float = 30.0


DEFAULT_LIMITS: Final = Limits()


class RelayRefusal(Exception):
    """A connection the cell will not serve, carrying the code it closes with."""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(f"{code} {reason}")
        self.code = code
        self.reason = reason
