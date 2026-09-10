"""In-flight inference transaction table: the handoff between a consumer's
request and a worker's poll loop. Pure and in-memory; nothing here persists."""

from __future__ import annotations

import asyncio
import enum
import time
import uuid
from dataclasses import dataclass, field
from typing import AsyncGenerator, Iterable


class TransactionState(enum.Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    STREAMING = "streaming"
    DONE = "done"
    FAILED = "failed"


_TERMINAL = (TransactionState.DONE, TransactionState.FAILED)


@dataclass
class Transaction:
    id: str
    model: str
    body: bytes
    is_stream: bool
    created_at: float
    state: TransactionState = TransactionState.PENDING
    node_id: str | None = None
    claimed_at: float | None = None
    error: str | None = None
    result: bytes | None = None
    _queue: asyncio.Queue = field(default_factory=asyncio.Queue, repr=False)


class InflightTable:
    def __init__(
        self,
        *,
        clock=time.monotonic,
        claim_deadline: float = 30.0,
        result_deadline: float = 600.0,
    ) -> None:
        self._clock = clock
        self._claim_deadline = claim_deadline
        self._result_deadline = result_deadline
        self._transactions: dict[str, Transaction] = {}
        self._work_available = asyncio.Event()

    def create(self, *, model: str, body: bytes, is_stream: bool) -> Transaction:
        txn = Transaction(
            id=uuid.uuid4().hex,
            model=model,
            body=body,
            is_stream=is_stream,
            created_at=self._clock(),
        )
        self._transactions[txn.id] = txn
        self._work_available.set()
        return txn

    def get(self, txn_id: str) -> Transaction | None:
        return self._transactions.get(txn_id)

    def claim(self, *, node_id: str, models: Iterable[str]) -> Transaction | None:
        wanted = set(models)
        # A single synchronous pass with no `await` inside it is what makes
        # this atomic under asyncio's cooperative scheduling.
        candidates = [
            txn
            for txn in self._transactions.values()
            if txn.state is TransactionState.PENDING and txn.model in wanted
        ]
        if not candidates:
            return None
        txn = min(candidates, key=lambda t: t.created_at)
        txn.state = TransactionState.CLAIMED
        txn.node_id = node_id
        txn.claimed_at = self._clock()
        return txn

    def publish(self, txn_id: str, chunk: bytes) -> bool:
        txn = self._transactions.get(txn_id)
        if txn is None or txn.state in _TERMINAL:
            return False
        txn.state = TransactionState.STREAMING
        txn._queue.put_nowait(chunk)
        return True

    def finish(self, txn_id: str, result: bytes | None) -> bool:
        txn = self._transactions.get(txn_id)
        if txn is None or txn.state in _TERMINAL:
            return False
        txn.state = TransactionState.DONE
        txn.result = result
        txn._queue.put_nowait(None)
        return True

    def cancel(self, txn_id: str, reason: str) -> None:
        # Always pops, even for an already-terminal txn: this is the cleanup path
        # every caller is expected to run once it's done with a transaction.
        txn = self._transactions.pop(txn_id, None)
        if txn is None or txn.state in _TERMINAL:
            return
        txn.state = TransactionState.FAILED
        txn.error = reason
        txn._queue.put_nowait(None)

    def sweep(self) -> list[Transaction]:
        now = self._clock()
        expired = []
        for txn in list(self._transactions.values()):
            if txn.state is TransactionState.PENDING:
                if now - txn.created_at > self._claim_deadline:
                    expired.append(txn)
            elif txn.state in (TransactionState.CLAIMED, TransactionState.STREAMING):
                assert txn.claimed_at is not None
                if now - txn.claimed_at > self._result_deadline:
                    expired.append(txn)
        for txn in expired:
            self.cancel(txn.id, "expired")
        return expired

    async def stream(self, txn_id: str) -> AsyncGenerator[bytes, None]:
        txn = self._transactions.get(txn_id)
        if txn is None:
            return
        while True:
            chunk = await txn._queue.get()
            if chunk is None:
                return
            yield chunk

    async def wait_for_work(self, timeout: float) -> None:
        self._work_available.clear()
        try:
            await asyncio.wait_for(self._work_available.wait(), timeout)
        except asyncio.TimeoutError:
            pass
