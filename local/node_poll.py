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
    try:
        answered = engine.post(
            "/v1/chat/completions",
            content=json.dumps(work["body"]).encode(),
            headers={"content-type": "application/json"},
            timeout=ENGINE_TIMEOUT,
        )
        grid.post(f"/grid/v1/result/{txn_id}", content=answered.content, headers=headers)
    except httpx.HTTPError as exc:
        grid.post(f"/grid/v1/error/{txn_id}", content=str(exc).encode()[:500], headers=headers)
    return True
