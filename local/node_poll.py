"""One worker poll-and-serve cycle: claim, run the engine, report back."""

from __future__ import annotations

import json
from typing import Iterable

import httpx

# Must outlast the grid's own poll wait window (30.0s) or every idle cycle
# reads as a transport error instead of "no work yet".
POLL_TIMEOUT_SECONDS = 35.0

# Matches the push path this replaces (server.ENGINE_TIMEOUT_SECONDS). `read=None` is the load
# bearing half: tokens arrive whenever the model produces them, so any read deadline cancels a
# working generation rather than catching a broken one. Without it httpx's 5s default applied.
ENGINE_TIMEOUT_SECONDS = 600
ENGINE_TIMEOUT = httpx.Timeout(ENGINE_TIMEOUT_SECONDS, read=None)

# Submitting a stream reads from the engine for as long as the engine keeps writing, so the
# submit cannot carry a read deadline either. Same shape remote/serve.py uses for the same job.
STREAM_SUBMIT_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=None, pool=10.0)


def run_one_cycle(
    grid: httpx.Client,
    engine: httpx.Client,
    *,
    host_id: str,
    models: Iterable[str],
    token: str,
) -> bool:
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
    body = json.dumps(work["body"]).encode()
    try:
        if work.get("stream"):
            _serve_streamed(grid, engine, txn_id, body, headers)
        else:
            answered = engine.post(
                "/v1/chat/completions",
                content=body,
                headers={"content-type": "application/json"},
                timeout=ENGINE_TIMEOUT,
            )
            grid.post(f"/grid/v1/result/{txn_id}", content=answered.content, headers=headers)
    except httpx.HTTPError as exc:
        grid.post(f"/grid/v1/error/{txn_id}", content=str(exc).encode()[:500], headers=headers)
    return True


def _serve_streamed(
    grid: httpx.Client,
    engine: httpx.Client,
    txn_id: str,
    body: bytes,
    headers: dict[str, str],
) -> None:
    """Pipe the engine's SSE into the submit's request body, verbatim.

    One request, not one per chunk: handing the byte iterator to `content` lets httpx stream the
    body, so the consumer is served while the engine is still writing. Mirrors
    remote/serve.py::_forward_stream, which has carried streamed jobs in remote mode all along.
    The grid ends the consumer's stream when this request body ends, so no separate /done is sent.
    """

    with engine.stream(
        "POST",
        "/v1/chat/completions",
        content=body,
        headers={"content-type": "application/json"},
        timeout=ENGINE_TIMEOUT,
    ) as answered:
        if answered.status_code != 200:
            answered.read()
            grid.post(
                f"/grid/v1/error/{txn_id}",
                content=f"engine error {answered.status_code}: {answered.text[:200]}".encode(),
                headers=headers,
            )
            return
        grid.post(
            f"/grid/v1/result/{txn_id}",
            content=answered.iter_bytes(),
            headers={**headers, "content-type": "text/event-stream"},
            timeout=STREAM_SUBMIT_TIMEOUT,
        )
