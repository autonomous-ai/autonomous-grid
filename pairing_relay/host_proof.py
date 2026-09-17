"""Proving a desktop is the one behind a `relayHostId`, before it is given one.

A host id is not handed out by this cell. It is **derived from the desktop's
own public key** — the same key the pairing code puts in the phone's hands —
so claiming somebody else's id means holding their private key. That removes a
whole class of problem a server-issued id has: there is no registry to race, no
id to squat, and nothing to reconcile when a desktop moves between cells.

What is left is proving possession, and the shape of that proof matters as much
as the fact of it. The cell seals a secret to the claimed key, so only the real
holder can read it; and it seals a *transcript* alongside, naming the origin,
the id, the key and the window — so the desktop can check it is not being asked
to sign something for a cell it never dialled. A challenge/response where only
the server picks what gets signed is a signing oracle.

⚠️ **Possession is not authorisation.** This proves *who*, never *whether*. A
production cell also has to answer "is this account allowed on this relay, and
how much of it may they have" — quota, abuse, revocation — which is what Orca's
`relayJwt` carries and this file deliberately has no opinion about.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from pairing_relay.protocol import KEY_BYTES

HOST_PROOF_DOMAIN = b"grid-relay-host-proof/v1"
CHALLENGE_DOMAIN = b"grid-relay-host-challenge/v1"
PROOF_TAG = HOST_PROOF_DOMAIN + b"\x00ack\x00"
AEAD_NONCE_BYTES = 12
HOST_ID_CHARS = 16


def derive_relay_host_id(public_key: bytes) -> str:
    """The id [public_key] owns.

    Truncated to 16 base64url characters — 96 bits, which is collision-proof at
    any number of desktops that will ever exist, and short enough to sit in a
    URL path and a QR code without complaint.
    """
    if len(public_key) != KEY_BYTES:
        raise ValueError(f"public key must be {KEY_BYTES} bytes")
    digest = hashlib.sha256(public_key).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")[:HOST_ID_CHARS]


def _field(name: str, value: bytes) -> bytes:
    encoded = name.encode("utf-8")
    return struct.pack(">I", len(encoded)) + encoded + struct.pack(">I", len(value)) + value


def _encode_transcript(
    *,
    relay_origin: str,
    relay_ephemeral_public_key: bytes,
    challenge_nonce: bytes,
    challenge_id: str,
    relay_host_id: str,
    host_public_key: bytes,
    issued_at_ms: int,
    expires_at_ms: int,
) -> bytes:
    """The bytes both sides authenticate, length-prefixed so no value can forge
    a field boundary. Same encoding as the app's handshake transcript."""
    return b"".join(
        [
            _field("protocol", HOST_PROOF_DOMAIN),
            _field("version", struct.pack(">I", 1)),
            _field("relayOrigin", relay_origin.encode("utf-8")),
            _field("relayEphemeralPublicKey", relay_ephemeral_public_key),
            _field("challengeNonce", challenge_nonce),
            _field("challengeId", challenge_id.encode("utf-8")),
            _field("relayHostId", relay_host_id.encode("utf-8")),
            _field("hostPublicKey", host_public_key),
            _field("issuedAt", struct.pack(">Q", issued_at_ms)),
            _field("expiresAt", struct.pack(">Q", expires_at_ms)),
        ]
    )


@dataclass(frozen=True)
class HostChallenge:
    """What goes on the wire. Carries no secret a wrong holder can use."""

    challenge_id: str
    relay_ephemeral_public_key: bytes
    nonce: bytes
    ciphertext: bytes
    expires_at_ms: int

    def to_json(self) -> dict[str, object]:
        return {
            "type": "host-challenge",
            "challengeId": self.challenge_id,
            "relayEphemeralPublicKeyB64": _b64(self.relay_ephemeral_public_key),
            "nonceB64": _b64(self.nonce),
            "ciphertextB64": _b64(self.ciphertext),
            "expiresAt": self.expires_at_ms,
        }

    @staticmethod
    def from_json(value: dict[str, object]) -> "HostChallenge":
        return HostChallenge(
            challenge_id=str(value["challengeId"]),
            relay_ephemeral_public_key=_unb64(value["relayEphemeralPublicKeyB64"]),
            nonce=_unb64(value["nonceB64"]),
            ciphertext=_unb64(value["ciphertextB64"]),
            expires_at_ms=int(value["expiresAt"]),  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class PendingChallenge:
    """What the cell keeps back. Never leaves this process."""

    challenge_id: str
    secret: bytes
    transcript: bytes
    expires_at_ms: int


def issue_challenge(
    *,
    relay_origin: str,
    relay_host_id: str,
    host_public_key: bytes,
    now_ms: int,
    ttl_ms: int,
) -> tuple[HostChallenge, PendingChallenge]:
    """A challenge only the holder of [host_public_key] can answer."""
    ephemeral = X25519PrivateKey.generate()
    ephemeral_public = ephemeral.public_key().public_bytes_raw()
    nonce = os.urandom(AEAD_NONCE_BYTES)
    challenge_id = secrets.token_urlsafe(16)
    expires_at_ms = now_ms + ttl_ms
    transcript = _encode_transcript(
        relay_origin=relay_origin,
        relay_ephemeral_public_key=ephemeral_public,
        challenge_nonce=nonce,
        challenge_id=challenge_id,
        relay_host_id=relay_host_id,
        host_public_key=host_public_key,
        issued_at_ms=now_ms,
        expires_at_ms=expires_at_ms,
    )
    secret = os.urandom(KEY_BYTES)
    shared = ephemeral.exchange(X25519PublicKey.from_public_bytes(host_public_key))
    plaintext = struct.pack(">I", len(transcript)) + transcript + secret
    ciphertext = ChaCha20Poly1305(_seal_key(shared, nonce)).encrypt(
        nonce, plaintext, CHALLENGE_DOMAIN
    )
    return (
        HostChallenge(
            challenge_id=challenge_id,
            relay_ephemeral_public_key=ephemeral_public,
            nonce=nonce,
            ciphertext=ciphertext,
            expires_at_ms=expires_at_ms,
        ),
        PendingChallenge(
            challenge_id=challenge_id,
            secret=secret,
            transcript=transcript,
            expires_at_ms=expires_at_ms,
        ),
    )


def verify_proof(pending: PendingChallenge, proof_b64: str, *, now_ms: int) -> bool:
    """Whether [proof_b64] answers [pending] and is still in time."""
    if now_ms > pending.expires_at_ms:
        return False
    try:
        offered = base64.b64decode(proof_b64, validate=True)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(offered, expected_proof(pending.secret, pending.transcript))


def expected_proof(secret: bytes, transcript: bytes) -> bytes:
    """The answer a holder of the secret produces. Shared by both sides so the
    relay and the desktop cannot disagree about what is being signed."""
    return hmac.new(secret, PROOF_TAG + transcript, hashlib.sha256).digest()


def _seal_key(shared_secret: bytes, nonce: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=KEY_BYTES, salt=nonce, info=CHALLENGE_DOMAIN
    ).derive(shared_secret)


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode()


def _unb64(value: object) -> bytes:
    return base64.b64decode(str(value), validate=True)


# --- the desktop's side -------------------------------------------------------
#
# The real host is the Flutter app (`lib/infrastructure/pairing/`). This is the
# reference the Dart must match and the double the relay's own tests drive, so
# the two halves of the protocol can be read next to each other instead of one
# of them being inferred from the other.


@dataclass(frozen=True)
class HostProofContext:
    """What the desktop believes, and will refuse to sign anything that differs."""

    relay_origin: str
    relay_host_id: str
    host_public_key: bytes


class HostProofRefused(Exception):
    """A challenge the desktop will not answer.

    Carries the failing check by **name only**. A reason that quoted the value
    it disagreed with would put the transcript into whatever log this lands in.
    """


def answer_challenge(
    challenge: HostChallenge,
    *,
    host_private_key: bytes,
    context: HostProofContext,
    now_ms: int,
    max_window_ms: int = 10_000,
    skew_ms: int = 30_000,
) -> str:
    """The proof for [challenge], or [HostProofRefused] naming why not."""
    private = X25519PrivateKey.from_private_bytes(host_private_key)
    shared = private.exchange(
        X25519PublicKey.from_public_bytes(challenge.relay_ephemeral_public_key)
    )
    try:
        plaintext = ChaCha20Poly1305(_seal_key(shared, challenge.nonce)).decrypt(
            challenge.nonce, challenge.ciphertext, CHALLENGE_DOMAIN
        )
    except Exception as error:  # InvalidTag, and anything the AEAD raises
        # The only honest reading: this challenge was not sealed to our key.
        raise HostProofRefused("challenge-undecryptable") from error

    if len(plaintext) < 4 + KEY_BYTES:
        raise HostProofRefused("plaintext-too-short")
    (length,) = struct.unpack(">I", plaintext[:4])
    if 4 + length + KEY_BYTES != len(plaintext):
        raise HostProofRefused("plaintext-length-mismatch")
    transcript = plaintext[4 : 4 + length]
    secret = plaintext[4 + length :]

    fields = _parse_transcript(transcript)
    if fields is None:
        raise HostProofRefused("transcript-structure")
    _check_transcript(
        fields,
        challenge=challenge,
        context=context,
        now_ms=now_ms,
        max_window_ms=max_window_ms,
        skew_ms=skew_ms,
    )
    return _b64(expected_proof(secret, transcript))


def _parse_transcript(transcript: bytes) -> dict[str, bytes] | None:
    fields: dict[str, bytes] = {}
    offset = 0
    while offset < len(transcript):
        if offset + 4 > len(transcript):
            return None
        (name_length,) = struct.unpack(">I", transcript[offset : offset + 4])
        offset += 4
        if offset + name_length + 4 > len(transcript):
            return None
        name = transcript[offset : offset + name_length].decode("utf-8", "replace")
        offset += name_length
        (value_length,) = struct.unpack(">I", transcript[offset : offset + 4])
        offset += 4
        if offset + value_length > len(transcript) or name in fields:
            return None
        fields[name] = transcript[offset : offset + value_length]
        offset += value_length
    return fields if offset == len(transcript) else None


def _check_transcript(
    fields: dict[str, bytes],
    *,
    challenge: HostChallenge,
    context: HostProofContext,
    now_ms: int,
    max_window_ms: int,
    skew_ms: int,
) -> None:
    if len(fields) != 10:
        raise HostProofRefused("transcript-field-count")
    issued_at = _read_u64(fields.get("issuedAt"))
    expires_at = _read_u64(fields.get("expiresAt"))
    if issued_at is None or expires_at is None:
        raise HostProofRefused("transcript-timestamps")

    checks: list[tuple[str, bool]] = [
        ("protocol", fields.get("protocol") == HOST_PROOF_DOMAIN),
        ("version", fields.get("version") == struct.pack(">I", 1)),
        ("relayOrigin", fields.get("relayOrigin") == context.relay_origin.encode()),
        (
            "relayEphemeralPublicKey",
            fields.get("relayEphemeralPublicKey") == challenge.relay_ephemeral_public_key,
        ),
        ("challengeNonce", fields.get("challengeNonce") == challenge.nonce),
        ("challengeId", fields.get("challengeId") == challenge.challenge_id.encode()),
        ("relayHostId", fields.get("relayHostId") == context.relay_host_id.encode()),
        ("hostPublicKey", fields.get("hostPublicKey") == context.host_public_key),
        # Our own id must be the one our key owns, or this desktop is misconfigured
        # and would be proving possession for an id it cannot really hold.
        (
            "relayHostId-derives-from-key",
            context.relay_host_id == derive_relay_host_id(context.host_public_key),
        ),
        ("expiry-consistent", expires_at == challenge.expires_at_ms),
        ("issued-before-expiry", issued_at <= expires_at),
        ("window", expires_at - issued_at <= max_window_ms),
        ("not-expired", now_ms - skew_ms <= expires_at),
        ("not-future", issued_at - skew_ms <= now_ms),
    ]
    failed = [name for name, ok in checks if not ok]
    if failed:
        raise HostProofRefused("transcript:" + "+".join(failed))


def _read_u64(value: bytes | None) -> int | None:
    if value is None or len(value) != 8:
        return None
    return struct.unpack(">Q", value)[0]
