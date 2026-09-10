"""Host-scoped credentials for allocator node control traffic.

The grid operator token is an administrator capability.  It must never be copied to worker hosts:
doing so would let any worker change placement policy or impersonate every other host.  Instead the
operator uses it as a signing key to mint an expiring credential whose authority is limited to one
stable allocator host id.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import math
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

NODE_TOKEN_PREFIX = "grid-node-v1"
DEFAULT_NODE_TOKEN_TTL_SECONDS = 365 * 24 * 60 * 60
_SIGNING_CONTEXT = b"autonomous-grid allocator node credential v1\0"
_HOST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,127}\Z")


@dataclass(frozen=True, slots=True)
class NodeCredential:
    host_id: str
    token_id: str
    issued_at: int
    expires_at: int

    def __post_init__(self) -> None:
        _validate_host_id(self.host_id)
        if not self.token_id or len(self.token_id) > 128:
            raise ValueError("allocator node credential has an invalid token id")
        if self.issued_at < 0 or self.expires_at <= self.issued_at:
            raise ValueError("allocator node credential has an invalid lifetime")


def mint_node_token(
    operator_token: str,
    host_id: str,
    *,
    ttl_seconds: int = DEFAULT_NODE_TOKEN_TTL_SECONDS,
    now: float | None = None,
    token_id: str | None = None,
) -> str:
    """Mint an opaque bearer credential limited to ``host_id``.

    The signed payload is intentionally self-contained so the local signaling server does not need
    a second credential database.  Rotation is done by rotating the operator token or issuing a
    short-lived replacement.
    """

    secret = _operator_secret(operator_token)
    _validate_host_id(host_id)
    issued_at = _whole_time(time.time() if now is None else now)
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
        raise ValueError("allocator node credential TTL must be a positive integer")
    credential = NodeCredential(
        host_id=host_id,
        token_id=token_id or secrets.token_urlsafe(12),
        issued_at=issued_at,
        expires_at=issued_at + ttl_seconds,
    )
    payload = _encode_payload(
        {
            "expires_at": credential.expires_at,
            "host_id": credential.host_id,
            "issued_at": credential.issued_at,
            "token_id": credential.token_id,
        }
    )
    signature = hmac.new(secret, _SIGNING_CONTEXT + payload.encode(), hashlib.sha256).digest()
    return f"{NODE_TOKEN_PREFIX}.{payload}.{_base64url(signature)}"


def decode_node_token(token: str) -> NodeCredential:
    """Decode public claims without authenticating them.

    This is used only by the worker CLI to bind its persistent runtime state to the host id selected
    by the operator.  Servers must call :func:`verify_node_token` instead.
    """

    _, payload, _ = _token_parts(token)
    return _credential_from_payload(payload)


def verify_node_token(
    token: str,
    operator_token: str,
    host_id: str,
    *,
    now: float | None = None,
) -> NodeCredential:
    """Authenticate a node credential and prove that it belongs to ``host_id``."""

    secret = _operator_secret(operator_token)
    _, payload, supplied_signature = _token_parts(token)
    expected_signature = _base64url(
        hmac.new(secret, _SIGNING_CONTEXT + payload.encode(), hashlib.sha256).digest()
    )
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise ValueError("allocator node credential signature is invalid")
    credential = _credential_from_payload(payload)
    if not hmac.compare_digest(credential.host_id, host_id):
        raise ValueError("allocator node credential is not valid for this host")
    timestamp = _whole_time(time.time() if now is None else now)
    if timestamp >= credential.expires_at:
        raise ValueError("allocator node credential has expired")
    return credential


def secure_control_transport(url: str) -> bool:
    """Return whether bearer credentials can safely be sent to ``url`` by default."""

    try:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme.lower() == "https":
            return bool(hostname)
        if parsed.scheme.lower() != "http":
            return False
        if hostname == "localhost":
            return True
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def control_node_id(host_id: str) -> str:
    """Return the only registry ID a host may use for its allocator control record."""

    _validate_host_id(host_id)
    digest = hashlib.sha256(f"control\0{host_id}".encode()).hexdigest()[:20]
    return f"allocator-control-{digest}"


def engine_node_id(host_id: str, model_id: str) -> str:
    """Return the only registry ID a host may use for one managed model child."""

    _validate_host_id(host_id)
    if not isinstance(model_id, str) or not model_id or len(model_id) > 1_024:
        raise ValueError("allocator model id is required and must be at most 1024 characters")
    digest = hashlib.sha256(f"engine\0{host_id}\0{model_id}".encode()).hexdigest()[:20]
    return f"allocator-engine-{digest}"


def _credential_from_payload(payload: str) -> NodeCredential:
    try:
        decoded = base64.urlsafe_b64decode(_padding(payload)).decode("utf-8")
        value: Any = json.loads(decoded)
        if not isinstance(value, dict):
            raise TypeError
        return NodeCredential(
            host_id=str(value["host_id"]),
            token_id=str(value["token_id"]),
            issued_at=int(value["issued_at"]),
            expires_at=int(value["expires_at"]),
        )
    except (KeyError, TypeError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("allocator node credential payload is invalid") from exc


def _token_parts(token: str) -> tuple[str, str, str]:
    if not isinstance(token, str):
        raise TypeError("allocator node credential must be text")
    parts = token.strip().split(".")
    if len(parts) != 3 or parts[0] != NODE_TOKEN_PREFIX or not parts[1] or not parts[2]:
        raise ValueError("allocator node credential format is invalid")
    return parts[0], parts[1], parts[2]


def _encode_payload(value: dict[str, Any]) -> str:
    return _base64url(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _padding(value: str) -> str:
    return value + "=" * (-len(value) % 4)


def _operator_secret(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("allocator operator token is required")
    return value.encode("utf-8")


def _validate_host_id(value: str) -> None:
    if not isinstance(value, str) or _HOST_ID.fullmatch(value) is None:
        raise ValueError(
            "allocator host id must be 1-128 URL-safe ASCII letters, numbers, '.', '_', '~', or '-'"
        )


def _whole_time(value: float) -> int:
    timestamp = float(value)
    if not math.isfinite(timestamp) or timestamp < 0:
        raise ValueError("allocator credential time must be finite and non-negative")
    return int(timestamp)
