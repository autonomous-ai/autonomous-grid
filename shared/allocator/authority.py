"""Durable single-writer authority for the local allocator control loop.

The lease prevents two signaling-server processes sharing one controller state directory from
legitimately issuing mutations at once.  Its monotonically increasing term is also carried to
managed nodes, where it acts as a fencing token against delayed commands from an older leader.
"""
from __future__ import annotations

import math
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from shared import jsonio
from shared.allocator.models import MAX_COUNTER, MAX_ID_LENGTH
from shared.filelock import file_lock

AUTHORITY_SCHEMA_VERSION = 1


class AuthorityUnavailable(RuntimeError):
    """Another live controller owns the allocator mutation lease."""


@dataclass(frozen=True, slots=True)
class AuthorityGrant:
    term: int
    leader_id: str
    expires_at: float


class ControllerAuthorityLease:
    """Acquire and renew a short durable lease using a locked compare-and-swap file."""

    def __init__(
        self,
        controller_state_path: Path,
        *,
        ttl_seconds: float = 45.0,
        leader_id: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("allocator authority ttl must be finite and positive")
        self.path = controller_state_path.with_suffix(
            controller_state_path.suffix + ".authority"
        )
        self.ttl_seconds = float(ttl_seconds)
        self.leader_id = leader_id or uuid.uuid4().hex
        self.clock = clock
        self._grant: AuthorityGrant | None = None
        self._lock = threading.RLock()

    @property
    def grant(self) -> AuthorityGrant | None:
        with self._lock:
            return self._grant

    def ensure(self) -> AuthorityGrant:
        """Return a live grant, renewing it before the final third of its lifetime."""

        now = self._now()
        with self._lock:
            if (
                self._grant is not None
                and now < self._grant.expires_at - self.ttl_seconds / 3.0
            ):
                return self._grant
            with file_lock(self.path):
                value = self._read()
                current = self._decode(value)
                if (
                    current is not None
                    and current.expires_at > now
                    and current.leader_id != self.leader_id
                ):
                    self._grant = None
                    raise AuthorityUnavailable(
                        "allocator automatic mode is owned by another live controller "
                        f"(term {current.term})"
                    )
                if (
                    current is not None
                    and current.expires_at > now
                    and current.leader_id == self.leader_id
                ):
                    term = current.term
                else:
                    previous_term = current.term if current is not None else 0
                    if previous_term >= MAX_COUNTER:
                        raise OverflowError("allocator controller term is exhausted")
                    term = previous_term + 1
                grant = AuthorityGrant(term, self.leader_id, now + self.ttl_seconds)
                self._write(grant)
                self._grant = grant
                return grant

    def release(self) -> None:
        """Expire this process's grant early; a successor still receives a higher term."""

        with self._lock:
            grant = self._grant
            self._grant = None
            if grant is None:
                return
            now = self._now()
            with file_lock(self.path):
                current = self._decode(self._read())
                if (
                    current is None
                    or current.term != grant.term
                    or current.leader_id != self.leader_id
                ):
                    return
                self._write(AuthorityGrant(current.term, self.leader_id, now))

    def status(self) -> dict[str, object]:
        with self._lock:
            grant = self._grant
            now = self._now()
            return {
                "leader_id": self.leader_id,
                "term": grant.term if grant is not None else 0,
                "expires_at": grant.expires_at if grant is not None else 0.0,
                "held": bool(grant is not None and now < grant.expires_at),
            }

    def _now(self) -> float:
        now = float(self.clock())
        if not math.isfinite(now) or now < 0:
            raise ValueError("allocator authority clock must be finite and non-negative")
        return now

    def _read(self) -> dict[str, object]:
        try:
            return jsonio.load_json(self.path)
        except SystemExit as exc:
            raise ValueError(f"invalid allocator authority file: {exc}") from exc

    def _decode(self, value: dict[str, object]) -> AuthorityGrant | None:
        if not value:
            return None
        if int(value.get("schema_version") or 0) != AUTHORITY_SCHEMA_VERSION:
            raise ValueError("unsupported allocator authority schema")
        term = int(value.get("term") or 0)
        leader_id = str(value.get("leader_id") or "")
        expires_at = float(value.get("expires_at") or 0.0)
        if (
            not 0 < term <= MAX_COUNTER
            or not leader_id
            or len(leader_id) > MAX_ID_LENGTH
            or not math.isfinite(expires_at)
            or expires_at < 0
        ):
            raise ValueError("invalid persisted allocator authority")
        return AuthorityGrant(term, leader_id, expires_at)

    def _write(self, grant: AuthorityGrant) -> None:
        jsonio.atomic_write_json(
            self.path,
            {
                "schema_version": AUTHORITY_SCHEMA_VERSION,
                "term": grant.term,
                "leader_id": grant.leader_id,
                "expires_at": grant.expires_at,
            },
            mode=0o600,
        )
