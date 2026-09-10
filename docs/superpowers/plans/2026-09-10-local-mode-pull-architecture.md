# Local Mode Pull Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make local mode serve inference by pull instead of push, so the worker's engine binds loopback and the engine certificate, the trust-on-first-use CA fetch and the process-wide `SSL_CERT_FILE` can be deleted.

**Architecture:** The grid keeps an in-memory transaction table. A consumer request registers a transaction and awaits it; the worker long-polls for work, runs inference against `http://127.0.0.1:<port>/v1`, and POSTs results back. The grid never dials a worker. No database — the client connection is the durability boundary.

**Tech Stack:** Python 3.11+, FastAPI, asyncio, httpx, pytest. `uv` for running everything. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-10-local-mode-pull-architecture-design.md`

---

## File Structure

| File | Responsibility | Depends on |
|---|---|---|
| `local/inflight.py` *(new)* | The transaction table. Pure: no I/O, no FastAPI, injected clock. | asyncio only |
| `local/poll.py` *(new)* | The three worker-facing routes. | `inflight`, existing node auth |
| `local/node_poll.py` *(new)* | The worker's poll loop, one per concurrency slot. | httpx |
| `local/server.py` *(modify)* | Mount the router; `_proxy_openai` registers a transaction instead of dialling. | `inflight`, `poll` |
| `local/allocator_node.py` *(modify)* | Start/stop the poll loops alongside the heartbeat. | `node_poll` |
| `cli/allocator.py` *(modify)* | Delete engine TLS minting and `_fetch_grid_ca`. | — |
| `local/runtime.py` *(modify)* | Delete the `SSL_CERT_FILE` helpers. | — |
| `local/config.py` *(modify)* | Delete the `apply_server_tls_client_env` call. | — |

`inflight.py` is deliberately free of FastAPI so its concurrency rules can be tested without a server, the way `auto_router.py` is kept I/O-free in the sibling repo.

---

## Stage 1 — the transaction table and the routes, mounted but unused

### Task 1: `local/inflight.py` — the transaction table

**Files:**
- Create: `local/inflight.py`
- Test: `tests/test_inflight.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_inflight.py`:

```python
import asyncio

import pytest

from local.inflight import InflightTable, TransactionState


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_claim_is_exclusive_and_only_matches_advertised_models():
    clock = FakeClock()
    table = InflightTable(clock=clock)
    txn = table.create(model="m1", body=b"{}", is_stream=False)

    assert table.claim(node_id="n1", models=("other",)) is None
    first = table.claim(node_id="n1", models=("m1",))
    assert first is not None and first.id == txn.id
    assert first.state is TransactionState.CLAIMED
    assert table.claim(node_id="n2", models=("m1",)) is None


def test_publish_after_finish_is_ignored_not_an_error():
    table = InflightTable(clock=FakeClock())
    txn = table.create(model="m1", body=b"{}", is_stream=True)
    table.claim(node_id="n1", models=("m1",))
    table.finish(txn.id, b"done")
    assert table.publish(txn.id, b"late chunk") is False


def test_sweep_expires_a_claim_that_never_reported():
    clock = FakeClock()
    table = InflightTable(clock=clock, result_deadline=60.0)
    txn = table.create(model="m1", body=b"{}", is_stream=False)
    table.claim(node_id="n1", models=("m1",))
    clock.now += 61.0
    expired = table.sweep()
    assert [item.id for item in expired] == [txn.id]
    assert table.get(txn.id) is None


@pytest.mark.asyncio
async def test_consumer_receives_chunks_in_order_then_the_terminal_result():
    table = InflightTable(clock=FakeClock())
    txn = table.create(model="m1", body=b"{}", is_stream=True)
    table.claim(node_id="n1", models=("m1",))
    table.publish(txn.id, b"a")
    table.publish(txn.id, b"b")
    table.finish(txn.id, None)
    received = [chunk async for chunk in table.stream(txn.id)]
    assert received == [b"a", b"b"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --with pytest-asyncio pytest tests/test_inflight.py -v --asyncio-mode=auto`
Expected: FAIL with `ModuleNotFoundError: No module named 'local.inflight'`

- [ ] **Step 3: Write the implementation**

Create `local/inflight.py`:

```python
"""The in-flight inference transactions a local grid is holding for its consumers.

Deliberately free of FastAPI, httpx and disk: everything here is memory and asyncio, so the
concurrency rules below can be tested without standing up a server. The clock is injected for the
same reason — a deadline test must not sleep.

There is no persistence and that is a decision, not an omission: a transaction exists only while a
consumer holds its HTTP connection, so a grid restart kills the connection and the transaction
together. See docs/superpowers/specs/2026-09-10-local-mode-pull-architecture-design.md.
"""

from __future__ import annotations

import asyncio
import enum
import secrets
import time
from dataclasses import dataclass, field
from typing import AsyncGenerator, Callable, Iterable

# A worker's long-poll must OUTLAST this window, or the worker gives up first and every idle cycle
# looks like a transport error rather than "no work yet". The sibling relay fixes the same pair at
# 30/35 and says so at remote/relay.py:32,42-44. The two numbers are one decision; do not narrow
# this one without widening the worker's.
POLL_WINDOW_SECONDS = 30.0
# How long a request may wait for any worker to take it before the consumer is told nothing did.
CLAIM_DEADLINE_SECONDS = 30.0
# How long a claimed transaction may go without a chunk before it is failed. NOT requeued: the
# engine may be mid-generation, and a second worker would duplicate the work and the cost.
RESULT_DEADLINE_SECONDS = 600.0


class TransactionState(enum.Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    STREAMING = "streaming"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Transaction:
    id: str
    model: str
    body: bytes
    is_stream: bool
    created_at: float
    state: TransactionState = TransactionState.PENDING
    node_id: str = ""
    claimed_at: float = 0.0
    error: str = ""
    result: bytes | None = None
    chunks: asyncio.Queue = field(default_factory=asyncio.Queue)


_SENTINEL = object()


class InflightTable:
    """Registry of transactions awaiting, or being served by, a worker."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        claim_deadline: float = CLAIM_DEADLINE_SECONDS,
        result_deadline: float = RESULT_DEADLINE_SECONDS,
    ) -> None:
        self._clock = clock
        self._claim_deadline = claim_deadline
        self._result_deadline = result_deadline
        self._items: dict[str, Transaction] = {}
        self._lock = asyncio.Lock()
        self._arrivals = asyncio.Event()

    def create(self, *, model: str, body: bytes, is_stream: bool) -> Transaction:
        txn = Transaction(
            id=secrets.token_urlsafe(16),
            model=model,
            body=body,
            is_stream=is_stream,
            created_at=self._clock(),
        )
        self._items[txn.id] = txn
        self._arrivals.set()
        return txn

    def get(self, txn_id: str) -> Transaction | None:
        return self._items.get(txn_id)

    def claim(self, *, node_id: str, models: Iterable[str]) -> Transaction | None:
        """The oldest pending transaction this node can serve, or None.

        Claiming is a single synchronous pass over a dict with no await inside it, which is what
        makes it atomic under asyncio: no other coroutine can interleave between the read and the
        write, so a transaction can never be handed to two workers.
        """
        servable = set(models)
        for txn in sorted(self._items.values(), key=lambda item: item.created_at):
            if txn.state is TransactionState.PENDING and txn.model in servable:
                txn.state = TransactionState.CLAIMED
                txn.node_id = node_id
                txn.claimed_at = self._clock()
                return txn
        return None

    def publish(self, txn_id: str, chunk: bytes) -> bool:
        """Record one streamed chunk. False means the consumer is gone — stop generating.

        Publishing to a finished or cancelled transaction is not an error: a worker's last write
        legitimately races the consumer's disconnect, and raising there would turn a normal race
        into a reported failure.
        """
        txn = self._items.get(txn_id)
        if txn is None or txn.state in (TransactionState.DONE, TransactionState.FAILED):
            return False
        txn.state = TransactionState.STREAMING
        txn.chunks.put_nowait(chunk)
        return True

    def finish(self, txn_id: str, result: bytes | None) -> bool:
        txn = self._items.get(txn_id)
        if txn is None or txn.state in (TransactionState.DONE, TransactionState.FAILED):
            return False
        txn.result = result
        txn.state = TransactionState.DONE
        txn.chunks.put_nowait(_SENTINEL)
        return True

    def cancel(self, txn_id: str, reason: str) -> None:
        txn = self._items.pop(txn_id, None)
        if txn is None:
            return
        txn.state = TransactionState.FAILED
        txn.error = reason
        txn.chunks.put_nowait(_SENTINEL)

    def sweep(self) -> list[Transaction]:
        """Expire transactions past their deadline. The caller decides what to report."""
        now = self._clock()
        expired: list[Transaction] = []
        for txn in list(self._items.values()):
            if txn.state is TransactionState.PENDING:
                overdue = now - txn.created_at > self._claim_deadline
            elif txn.state in (TransactionState.CLAIMED, TransactionState.STREAMING):
                overdue = now - txn.claimed_at > self._result_deadline
            else:
                overdue = False
            if overdue:
                expired.append(txn)
                self.cancel(txn.id, "deadline exceeded")
        return expired

    async def stream(self, txn_id: str) -> AsyncGenerator[bytes, None]:
        txn = self._items.get(txn_id)
        if txn is None:
            return
        while True:
            chunk = await txn.chunks.get()
            if chunk is _SENTINEL:
                return
            yield chunk

    async def wait_for_work(self, timeout: float) -> None:
        """Sleep until a transaction arrives or the poll window closes."""
        self._arrivals.clear()
        try:
            await asyncio.wait_for(self._arrivals.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --with pytest-asyncio pytest tests/test_inflight.py -v --asyncio-mode=auto`
Expected: PASS, 4 passed

- [ ] **Step 5: Commit**

```bash
git add local/inflight.py tests/test_inflight.py
git commit -m "feat(local): add the in-flight inference transaction table"
```

---

### Task 2: `local/poll.py` — the three worker-facing routes

**Files:**
- Create: `local/poll.py`
- Test: `tests/test_local_poll.py`

The routes authenticate with the credential the node already holds, verified exactly as
`local/server.py:2479-2492` does it: the `X-Grid-Allocator-Node-Token` header against
`app.state.allocator_control_token` and the caller's `host_id`. No new credential is introduced.

- [ ] **Step 1: Write the failing test**

Create `tests/test_local_poll.py`:

```python
from fastapi import FastAPI
from fastapi.testclient import TestClient

from local.inflight import InflightTable
from local.poll import poll_router
from shared.allocator.auth import mint_node_token


def _app() -> tuple[FastAPI, TestClient, str]:
    app = FastAPI()
    app.state.allocator_control_token = "control-secret"
    app.state.inflight = InflightTable()
    app.include_router(poll_router)
    token = mint_node_token("control-secret", "host-1", ttl_seconds=3600)
    return app, TestClient(app), token


def test_poll_without_a_node_token_is_refused():
    _app_obj, client, _token = _app()
    response = client.get("/grid/v1/poll", params={"host_id": "host-1", "models": "m1"})
    assert response.status_code == 401


def test_poll_returns_a_transaction_the_node_can_serve():
    app, client, token = _app()
    txn = app.state.inflight.create(model="m1", body=b'{"model":"m1"}', is_stream=False)
    response = client.get(
        "/grid/v1/poll",
        params={"host_id": "host-1", "models": "m1"},
        headers={"x-grid-allocator-node-token": token},
    )
    assert response.status_code == 200
    assert response.json()["transaction_id"] == txn.id


def test_result_for_a_cancelled_transaction_tells_the_worker_to_stop():
    app, client, token = _app()
    txn = app.state.inflight.create(model="m1", body=b"{}", is_stream=True)
    app.state.inflight.claim(node_id="host-1", models=("m1",))
    app.state.inflight.cancel(txn.id, "consumer went away")
    response = client.post(
        f"/grid/v1/result/{txn.id}",
        content=b"chunk",
        headers={"x-grid-allocator-node-token": token, "x-grid-host-id": "host-1"},
    )
    assert response.status_code == 200
    assert response.json()["cancelled"] is True
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_local_poll.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'local.poll'`

- [ ] **Step 3: Write the implementation**

Create `local/poll.py`:

```python
"""The worker-facing half of the pull path: claim work, report chunks, report failure.

Every route here is called BY a worker, outbound. Nothing in this module ever dials a worker, which
is the property that lets a worker's engine bind loopback and drop its certificate entirely.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

from .inflight import POLL_WINDOW_SECONDS, InflightTable

poll_router = APIRouter()


def _table(request: Request) -> InflightTable:
    return request.app.state.inflight


def _require_node(request: Request, host_id: str) -> None:
    from .server import _allocator_node_control_valid

    if not host_id or not _allocator_node_control_valid(request.app, request, host_id):
        # A rogue node registration is unauthenticated by the permissionless registry design, so
        # this check is what keeps such a node from TAKING work as well as advertising itself.
        raise HTTPException(status_code=401, detail="node credential required")


@poll_router.get("/grid/v1/poll")
async def poll(request: Request, host_id: str = "", models: str = "") -> Response:
    _require_node(request, host_id)
    table = _table(request)
    servable = tuple(item for item in models.split(",") if item)
    txn = table.claim(node_id=host_id, models=servable)
    if txn is None:
        await table.wait_for_work(POLL_WINDOW_SECONDS)
        txn = table.claim(node_id=host_id, models=servable)
    if txn is None:
        return Response(status_code=204)
    return Response(
        content=(
            '{"transaction_id": "%s", "model": "%s", "stream": %s, "body": %s}'
            % (txn.id, txn.model, "true" if txn.is_stream else "false", txn.body.decode())
        ),
        media_type="application/json",
    )


@poll_router.post("/grid/v1/result/{txn_id}")
async def result(request: Request, txn_id: str) -> dict[str, bool]:
    _require_node(request, request.headers.get("x-grid-host-id", ""))
    table = _table(request)
    payload = await request.body()
    txn = table.get(txn_id)
    if txn is None:
        return {"cancelled": True}
    if txn.is_stream:
        accepted = table.publish(txn_id, payload)
        return {"cancelled": not accepted}
    accepted = table.finish(txn_id, payload)
    return {"cancelled": not accepted}


@poll_router.post("/grid/v1/result/{txn_id}/done")
async def done(request: Request, txn_id: str) -> dict[str, bool]:
    _require_node(request, request.headers.get("x-grid-host-id", ""))
    accepted = _table(request).finish(txn_id, None)
    return {"cancelled": not accepted}


@poll_router.post("/grid/v1/error/{txn_id}")
async def error(request: Request, txn_id: str) -> dict[str, bool]:
    _require_node(request, request.headers.get("x-grid-host-id", ""))
    table = _table(request)
    message = (await request.body()).decode("utf-8", "replace")[:500]
    table.cancel(txn_id, message or "worker reported a failure")
    return {"cancelled": True}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_local_poll.py -v`
Expected: PASS, 3 passed

- [ ] **Step 5: Commit**

```bash
git add local/poll.py tests/test_local_poll.py
git commit -m "feat(local): add the worker poll, result and error routes"
```

---

### Task 3: Mount the router and the table, still unused by consumers

**Files:**
- Modify: `local/server.py` (the `create_app` body, near `app.state.allocator = allocator` at line 318)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_local_poll.py`:

```python
def test_the_real_app_mounts_the_poll_routes():
    from local.server import create_app

    app = create_app()
    paths = {route.path for route in app.routes}
    assert "/grid/v1/poll" in paths
    assert "/grid/v1/result/{txn_id}" in paths
    assert isinstance(app.state.inflight, InflightTable)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_local_poll.py::test_the_real_app_mounts_the_poll_routes -v`
Expected: FAIL with `AttributeError: 'State' object has no attribute 'inflight'`

- [ ] **Step 3: Write the implementation**

In `local/server.py`, add the import beside the other local imports:

```python
from .inflight import InflightTable
from .poll import poll_router
```

and immediately after `app.state.allocator = allocator`:

```python
    # The pull path's transaction table. Mounted from the start but unused until _proxy_openai
    # switches over, so this lands with no behaviour change.
    app.state.inflight = InflightTable()
    app.include_router(poll_router)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_local_poll.py -v`
Expected: PASS, 4 passed

- [ ] **Step 5: Verify nothing else changed**

Run: `uv run pytest tests/test_allocator_server.py tests/test_local_cli.py -q -p no:randomly`
Expected: the same failing set as before this task — 27 failures, all `test_project_refresh_*` / `test_project_clone_*`, which are environmental (this machine's git does not report `authtype`). Any other id is a regression you introduced.

- [ ] **Step 6: Commit**

```bash
git add local/server.py tests/test_local_poll.py
git commit -m "feat(local): mount the pull path alongside the existing proxy"
```

---

## Stage 2 — the worker polls, behind a switch

### Task 4: Node selection

**Files:**
- Modify: `local/server.py` (add beside `_choose_engine` at line 1762)
- Test: `tests/test_local_poll.py`

`_choose_engine` answers "which URL do I dial". The pull path needs "which node can serve this", from
the same registry data — so this is a sibling, not a replacement, until Stage 4 deletes the original.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_local_poll.py`:

```python
def test_choose_node_prefers_the_least_loaded_node_serving_the_model():
    from local.server import Node, _choose_node, create_app

    app = create_app()
    app.state.nodes = {
        "busy": Node(node_id="busy", role="engine", models=["m1"], host_id="h-busy",
                     load={"active_tasks": 5}, last_heartbeat=1e12),
        "idle": Node(node_id="idle", role="engine", models=["m1"], host_id="h-idle",
                     load={"active_tasks": 0}, last_heartbeat=1e12),
        "other": Node(node_id="other", role="engine", models=["m2"], host_id="h-other",
                      load={"active_tasks": 0}, last_heartbeat=1e12),
    }
    assert _choose_node(app, "m1") == "h-idle"
    assert _choose_node(app, "absent") is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_local_poll.py::test_choose_node_prefers_the_least_loaded_node_serving_the_model -v`
Expected: FAIL with `ImportError: cannot import name '_choose_node'`

- [ ] **Step 3: Write the implementation**

In `local/server.py`, directly above `def _choose_engine(`:

```python
def _choose_node(app: FastAPI, model: str) -> str | None:
    """The host_id of the least-loaded live node advertising ``model``, or None.

    Dispatch is not placement. The allocator decides which model should live on which host, on a
    timescale of minutes and using forecasts; this answers who takes THIS request, right now, from
    the load already carried in the last heartbeat. Routing dispatch through the allocator would
    couple a per-request path to a forecasting loop — see the design spec.
    """
    candidates = [
        node
        for node in _active_engines(app, model)
        if node.host_id
    ]
    if not candidates:
        return None
    best = min(candidates, key=lambda node: int(node.load.get("active_tasks") or 0))
    return best.host_id
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_local_poll.py -v`
Expected: PASS, 5 passed

- [ ] **Step 5: Commit**

```bash
git add local/server.py tests/test_local_poll.py
git commit -m "feat(local): choose a node for dispatch, not an engine URL"
```

---

### Task 5: `_proxy_openai` gains a pull branch behind `GRID_LOCAL_PULL`

**Files:**
- Modify: `local/server.py:1002-1027` (`_proxy_openai`, after the model validation block)
- Test: `tests/test_local_poll.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_local_poll.py`:

```python
def test_pull_path_answers_503_when_no_node_advertises_the_model(monkeypatch):
    from local.server import create_app

    monkeypatch.setenv("GRID_LOCAL_PULL", "1")
    app = create_app()
    client = TestClient(app)
    response = client.post("/v1/chat/completions", json={"model": "absent"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "engine_unavailable"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_local_poll.py::test_pull_path_answers_503_when_no_node_advertises_the_model -v`
Expected: FAIL — the push path is still taken and the assertion on `code` may pass for the wrong reason, or the request hangs. Read the failure before continuing; if it passes accidentally, add `assert app.state.inflight.get(...) is None` to force the distinction.

- [ ] **Step 3: Write the implementation**

In `local/server.py`, immediately after the `features = classify_request(...)` block in `_proxy_openai`:

```python
    if os.getenv("GRID_LOCAL_PULL") == "1":
        return await _serve_by_pull(app, endpoint_path, request, body, raw_body, model)
```

and add the function beside `_proxy_openai`:

```python
async def _serve_by_pull(
    app: FastAPI,
    endpoint_path: str,
    request: Request,
    body: dict[str, Any],
    raw_body: bytes,
    model: str,
) -> Response:
    """Register the request and wait for a worker to take it.

    The grid does not dial anyone here. Chunks are relayed VERBATIM — no reframing, no injected
    [DONE] — because a pipe that rewrites its contents will eventually disagree with some client.
    """
    table: InflightTable = app.state.inflight
    if _choose_node(app, model) is None:
        return _openai_error(
            503, f"No active local engine for model {model!r}", "engine_unavailable"
        )
    txn = table.create(model=model, body=raw_body, is_stream=bool(body.get("stream")))
    try:
        if body.get("stream"):
            async def relay() -> AsyncGenerator[bytes, None]:
                async for chunk in table.stream(txn.id):
                    yield chunk

            return StreamingResponse(relay(), media_type="text/event-stream")
        async for chunk in table.stream(txn.id):
            del chunk  # a non-streaming transaction carries its whole body in `result`
        settled = table.get(txn.id)
        if settled is None or settled.result is None:
            return _openai_error(504, "No worker returned a result", "engine_timeout")
        return Response(content=settled.result, media_type="application/json")
    finally:
        table.cancel(txn.id, "consumer finished")
```

Add `AsyncGenerator` to the `typing` import and `StreamingResponse` to the `fastapi.responses`
import if they are not already present.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_local_poll.py -v`
Expected: PASS, 6 passed

- [ ] **Step 5: Verify the push path is untouched**

Run: `uv run pytest tests/test_allocator_server.py -q -p no:randomly`
Expected: same result as before this task. `GRID_LOCAL_PULL` is unset there, so every existing test takes the old branch.

- [ ] **Step 6: Commit**

```bash
git add local/server.py tests/test_local_poll.py
git commit -m "feat(local): serve inference by pull behind GRID_LOCAL_PULL"
```

---

### Task 6: The worker's poll loop

**Files:**
- Create: `local/node_poll.py`
- Modify: `local/allocator_node.py` (start the loops where the heartbeat thread starts)
- Test: `tests/test_node_poll.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_node_poll.py`:

```python
import httpx

from local.node_poll import run_one_cycle


def test_a_cycle_runs_the_engine_and_posts_the_result_back():
    posted: list[tuple[str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/grid/v1/poll":
            return httpx.Response(
                200,
                json={"transaction_id": "t1", "model": "m1", "stream": False, "body": {"model": "m1"}},
            )
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(200, json={"ok": True})
        posted.append((request.url.path, request.content))
        return httpx.Response(200, json={"cancelled": False})

    transport = httpx.MockTransport(handler)
    grid = httpx.Client(base_url="http://grid.invalid", transport=transport)
    engine = httpx.Client(base_url="http://127.0.0.1:18082", transport=transport)

    assert run_one_cycle(grid, engine, host_id="h1", models=("m1",), token="tok") is True
    assert posted and posted[0][0] == "/grid/v1/result/t1"


def test_a_cycle_with_no_work_reports_no_work_without_touching_the_engine():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/grid/v1/poll"
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    grid = httpx.Client(base_url="http://grid.invalid", transport=transport)
    engine = httpx.Client(base_url="http://127.0.0.1:18082", transport=transport)

    assert run_one_cycle(grid, engine, host_id="h1", models=("m1",), token="tok") is False
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_node_poll.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'local.node_poll'`

- [ ] **Step 3: Write the implementation**

Create `local/node_poll.py`:

```python
"""The worker's side of the pull path: take one unit of work, run it, report it.

One loop per concurrency slot, exactly as remote mode does — "each slot is a real OS thread holding
a long-poll" (remote/serve.py:61). That is what bounds a worker's parallelism, so the grid needs no
server-side slot accounting.

`run_one_cycle` is a plain function taking two clients so it can be tested against
httpx.MockTransport without a grid, a node, or a model.
"""

from __future__ import annotations

import json
from typing import Iterable

import httpx

# Must OUTLAST the grid's POLL_WINDOW_SECONDS (30.0) or every idle cycle reads as a transport error
# instead of "no work yet". See local/inflight.py for the other half of this pair.
POLL_TIMEOUT_SECONDS = 35.0


def run_one_cycle(
    grid: httpx.Client,
    engine: httpx.Client,
    *,
    host_id: str,
    models: Iterable[str],
    token: str,
) -> bool:
    """Claim at most one transaction and serve it. True if work was done."""

    headers = {"x-grid-allocator-node-token": token, "x-grid-host-id": host_id}
    claimed = grid.get(
        "/grid/v1/poll",
        params={"host_id": host_id, "models": ",".join(models)},
        headers=headers,
        timeout=POLL_TIMEOUT_SECONDS,
    )
    if claimed.status_code == 204:
        return False
    claimed.raise_for_status()
    work = claimed.json()
    txn_id = work["transaction_id"]
    try:
        answered = engine.post(
            "/v1/chat/completions",
            content=json.dumps(work["body"]).encode(),
            headers={"content-type": "application/json"},
        )
        grid.post(
            f"/grid/v1/result/{txn_id}", content=answered.content, headers=headers
        )
    except httpx.HTTPError as exc:
        grid.post(
            f"/grid/v1/error/{txn_id}", content=str(exc).encode()[:500], headers=headers
        )
    return True
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_node_poll.py -v`
Expected: PASS, 2 passed

- [ ] **Step 5: Wire the loops into the node**

In `local/allocator_node.py`, where the heartbeat thread is started, start `max_concurrency` daemon
threads each running `run_one_cycle` in a `while not self._shutdown_requested.is_set()` loop, with
the engine client pointed at `http://127.0.0.1:<residency port>/v1`. Guard the whole block on
`os.getenv("GRID_LOCAL_PULL") == "1"` so this task, like Task 5, lands with no behaviour change.

- [ ] **Step 6: Run the node suite**

Run: `uv run pytest tests/test_allocator_node.py -q -p no:randomly`
Expected: PASS, unchanged count

- [ ] **Step 7: Commit**

```bash
git add local/node_poll.py local/allocator_node.py tests/test_node_poll.py
git commit -m "feat(local): worker poll loop, one per concurrency slot"
```

---

## Stage 3 — flip the default, verify on hardware

### Task 7: Make pull the default and prove it on the two Macs

**Files:**
- Modify: `local/server.py` (the `GRID_LOCAL_PULL` check), `local/allocator_node.py` (same)

- [ ] **Step 1: Invert the switch**

Replace both `os.getenv("GRID_LOCAL_PULL") == "1"` guards with
`os.getenv("GRID_LOCAL_PUSH") != "1"`, so pull is the default and the old path is the escape hatch.

- [ ] **Step 2: Run the full suite**

Run: `uv run pytest tests/ -q -p no:randomly -rf`
Expected: the failing SET matches the recorded baseline (27 `project_refresh`/`project_clone`
failures). Diff the ids, not the count.

- [ ] **Step 3: Deploy to both machines**

```bash
scp local/inflight.py local/poll.py local/node_poll.py local/server.py local/allocator_node.py \
  mac-studio-water:/Users/mac6/ag-e2e/local/
```

Restart the grid on machine A and the node on the Studio.

- [ ] **Step 4: Prove one request end to end**

Run: `grid --local chat --grid lan2m --model gemma-4-31B-it-IQ4_NL.gguf "Reply with exactly: GRID OK"`
Expected: `GRID OK`

- [ ] **Step 5: Prove it is stable, not lucky**

Run one request a minute for ten minutes while watching the node's `load_failures` counter. Expected:
ten successes and a counter that does not move. A single successful request proves nothing here — the
defect class this replaces oscillated on a ~35-second cycle.

- [ ] **Step 6: Commit**

```bash
git add local/server.py local/allocator_node.py
git commit -m "feat(local): serve inference by pull by default"
```

---

## Stage 4 — delete the machinery. This is what pays for the work.

### Task 8: Delete the push path

**Files:**
- Modify: `local/server.py` — remove `_choose_engine`, `_engine_tls_verify`, the dialling half of `_proxy_openai`, and the `GRID_LOCAL_PUSH` escape hatch.

- [ ] **Step 1: Delete and run the suite**

Run: `uv run pytest tests/ -q -p no:randomly -rf`
Expected: baseline failing set. Tests that asserted proxy behaviour will fail — retarget them to the
pull path rather than deleting them, and say in the commit which requirement each still encodes.

- [ ] **Step 2: Commit**

```bash
git add local/server.py tests/
git commit -m "refactor(local): delete the push proxy now that pull is the only path"
```

### Task 9: Delete the engine certificate

**Files:**
- Modify: `cli/allocator.py` (the `ensure_server_cert` call at line 971 and the `--engine-tls-*` arguments), `local/allocator_node.py`, `cli/parser.py`

- [ ] **Step 1: Remove the engine TLS material and its plumbing, then run**

Run: `uv run pytest tests/test_allocator_node.py tests/test_allocator_cli.py -q -p no:randomly`
Expected: PASS

- [ ] **Step 2: Commit**

```bash
git add cli/allocator.py local/allocator_node.py cli/parser.py
git commit -m "refactor(allocator): a loopback engine needs no certificate"
```

### Task 10: Delete the CA-learning machinery

**Files:**
- Modify: `cli/allocator.py:322-357` (`_fetch_grid_ca` and its call site), `local/runtime.py:208-258`, `local/config.py:38-52`

- [ ] **Step 1: Write the failing regression test**

Create `tests/test_no_ambient_trust.py`:

```python
import pathlib


def test_no_module_reinstates_the_removed_trust_machinery():
    """These helpers replaced the public trust store process-wide and re-armed TOFU on every
    verification failure, disclosing the operator token. They are gone; keep them gone."""
    banned = ("apply_server_tls_client_env", "server_tls_client_env", "_fetch_grid_ca", "SSL_CERT_FILE")
    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = [
        f"{path}: {name}"
        for path in list((root / "cli").rglob("*.py")) + list((root / "local").rglob("*.py"))
        for name in banned
        if name in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_no_ambient_trust.py -v`
Expected: FAIL, listing the current call sites

- [ ] **Step 3: Delete the helpers and their call sites**

- [ ] **Step 4: Run it to verify it passes**

Run: `uv run pytest tests/test_no_ambient_trust.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite and re-verify on hardware**

Run: `uv run pytest tests/ -q -p no:randomly -rf`, then repeat Task 7 Steps 4–5 on the two Macs.
Expected: baseline failing set; `GRID OK`; ten stable minutes.

- [ ] **Step 6: Commit**

```bash
git add cli/allocator.py local/runtime.py local/config.py tests/test_no_ambient_trust.py
git commit -m "refactor: delete the learned-CA fetch and the process-wide SSL_CERT_FILE"
```

---

## Self-review against the spec

| Spec requirement | Task |
|---|---|
| In-memory transaction table, pure, injected clock | 1 |
| Claim exclusivity, publish-after-finish is a no-op, deadline sweeps | 1 |
| Three worker routes, authenticated by the existing node credential | 2 |
| Poll window 30 s grid-side / 35 s worker-side, stated as one decision | 1, 6 |
| Dispatch is least-loaded and does not consult the allocator | 4 |
| Chunks relayed verbatim, no reframing | 5 |
| 503 when no node serves the model; 504 when a claim reports nothing | 5 |
| One poll loop per concurrency slot | 6 |
| Four-stage rollout, each verified on hardware | 3, 5, 7, 10 |
| Delete push path, engine certificate, TOFU fetch, `SSL_CERT_FILE` | 8, 9, 10 |
| Regression test that the deleted machinery cannot return | 10 |
| No database, no new dependency | every task — nothing here adds one |

**Not covered here, by design:** the two open defects the spec names under Security posture (LAN-wide
CORS with unauthenticated node registration, and `ensure_server_cert` reusing an unverified or expired
leaf). Both are real and filed; neither is caused or fixed by this change, and folding them in would
mix an architecture change with unrelated hardening.
