"""HTTP client for a cloud grid's hosted relay — the provider (engine) side of the serve loop.

The relay base is the grid's ``signaling_url``; a joined engine authenticates every call with its
per-grid ``access_token`` (Bearer). It registers its capabilities (``PUT /nodes/{node_id}``),
long-polls for work (``GET /relay/v1/poll``), posts each result back
(``POST /relay/v1/{response,error}/{txn}``), and heartbeats (``POST /nodes/heartbeat``).

Ported and trimmed from ``grid-src/grid_cli/provider_runtime/provider/{register,poll_worker,
heartbeat}.py``, repointed onto the in-repo ``signaling_url`` base (DECISIONS D11/ADR 0003). Unlike
``control_plane`` — which raises a ``SystemExit`` on any ``>=400`` — the relay layer maps status
codes so the *long-running* serve loop can refresh on 401, re-register on 404, and back off on a
transient error instead of dying. The serve loop (`cloud/serve.py`) owns that orchestration; this
module is the stateless wire boundary.
"""
from __future__ import annotations

import sys
from typing import Any, Iterable

import httpx


# Long-poll window and heartbeat cadence (grid-src parity: well within the relay's 120s node TTL).
POLL_TIMEOUT = 35.0
HEARTBEAT_INTERVAL = 30
# How long to wait posting a result back. Streaming submits read indefinitely (write=None).
_SUBMIT_TIMEOUT = 30.0
_REGISTER_TIMEOUT = 15.0


class RelayUnauthorized(Exception):
    """The relay rejected the access token (401) — the caller should refresh and retry."""


class RelayError(Exception):
    """An unexpected relay status or transport failure — the caller logs and backs off."""


def _client(signaling_url: str, access_token: str, *, timeout: float | httpx.Timeout) -> httpx.Client:
    return httpx.Client(
        base_url=signaling_url.rstrip("/"),
        headers={"User-Agent": "grid-cli", "Authorization": f"Bearer {access_token}"},
        timeout=timeout,
    )


def _guard(resp: httpx.Response, what: str) -> None:
    """Map a relay response to the shared error policy: 401 → refresh, other ≥400 → back off."""
    if resp.status_code == 401:
        raise RelayUnauthorized()
    if resp.status_code >= 400:
        raise RelayError(f"{what} failed ({resp.status_code}): {resp.text[:200]}")


def register_node(
    signaling_url: str,
    access_token: str,
    node_id: str,
    *,
    models: list[str],
    capabilities: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
    pricing: dict[str, float] | None = None,
    max_concurrency: int | None = None,
    role: str = "provider",
) -> None:
    """Advertise this engine's capabilities to the relay (``PUT /nodes/{node_id}``).

    The capabilities map must use the ``{"schema_version": 1, "models": {...}}`` envelope or the
    relay silently drops it (grid-src register.py). Optional fields are omitted when empty.
    """
    body: dict[str, Any] = {"role": role, "models": models, "pricing": pricing or {}}
    if capabilities:
        body["capabilities"] = capabilities
    if meta:
        body["meta"] = meta
    if max_concurrency is not None:
        body["max_concurrency"] = max_concurrency
    try:
        with _client(signaling_url, access_token, timeout=_REGISTER_TIMEOUT) as client:
            resp = client.put(f"/nodes/{node_id}", json=body)
    except httpx.HTTPError as exc:
        raise RelayError(f"register transport error: {exc}") from None
    _guard(resp, "register")


def unregister_node(signaling_url: str, access_token: str, node_id: str) -> None:
    """Flip the node back to ``consumer`` so the relay drains queued work and stops sending more.

    Best-effort on shutdown: a failed drain never raises (the relay's TTL prune evicts us anyway).
    """
    body = {"role": "consumer", "models": [], "pricing": {}}
    try:
        with _client(signaling_url, access_token, timeout=_REGISTER_TIMEOUT) as client:
            resp = client.put(f"/nodes/{node_id}", json=body)
    except httpx.HTTPError as exc:
        print(f"unregister failed (best-effort, ignoring): {exc}", file=sys.stderr)
        return
    if resp.status_code >= 400:
        print(f"unregister returned {resp.status_code} (best-effort, ignoring).", file=sys.stderr)


def heartbeat(signaling_url: str, access_token: str, *, load: dict[str, Any]) -> str:
    """Keep the node live (``POST /nodes/heartbeat``). Returns ``"ok"`` or ``"missing"`` (404 →
    the node was pruned, so the caller re-registers). 401 raises ``RelayUnauthorized``.

    The body carries only ``load`` — the relay identifies the node from the bearer token, not a
    body field (grid-src parity).
    """
    try:
        with _client(signaling_url, access_token, timeout=10.0) as client:
            resp = client.post("/nodes/heartbeat", json={"load": load})
    except httpx.HTTPError as exc:
        raise RelayError(f"heartbeat transport error: {exc}") from None
    if resp.status_code == 404:
        return "missing"
    _guard(resp, "heartbeat")
    return "ok"


def poll(signaling_url: str, access_token: str, *, timeout: float = POLL_TIMEOUT) -> dict[str, Any] | None:
    """Long-poll for one unit of work (``GET /relay/v1/poll``).

    Returns the job dict on 200 (``{transaction_id, endpoint_path, body, is_stream,
    inference_timeout_seconds}``), ``None`` on 204 (no work). 401 → ``RelayUnauthorized``; any
    other status / transport error → ``RelayError`` so the caller backs off without dying.
    """
    try:
        with _client(signaling_url, access_token, timeout=timeout) as client:
            resp = client.get("/relay/v1/poll")
    except httpx.HTTPError as exc:
        raise RelayError(f"poll transport error: {exc}") from None
    if resp.status_code == 204:
        return None
    if resp.status_code == 200:
        return resp.json()
    _guard(resp, "poll")
    raise RelayError(f"poll returned unexpected {resp.status_code}")


def submit_response(
    signaling_url: str,
    access_token: str,
    txn_id: str,
    *,
    content: bytes | Iterable[bytes],
    stream: bool,
) -> None:
    """Post the engine's result back to the relay (``POST /relay/v1/response/{txn}``).

    ``content`` is the raw engine body: bytes for a whole response (``application/json``) or an
    iterator of SSE byte-chunks for a streamed one (``text/event-stream``).
    """
    content_type = "text/event-stream" if stream else "application/json"
    # A streamed submit reads from the engine indefinitely; a whole one is bounded.
    timeout = httpx.Timeout(connect=10, read=None, write=None, pool=10) if stream else _SUBMIT_TIMEOUT
    try:
        with _client(signaling_url, access_token, timeout=timeout) as client:
            resp = client.post(
                f"/relay/v1/response/{txn_id}",
                content=content,
                headers={"Content-Type": content_type},
            )
    except httpx.HTTPError as exc:
        raise RelayError(f"submit_response transport error: {exc}") from None
    _guard(resp, "submit_response")


def submit_error(
    signaling_url: str,
    access_token: str,
    txn_id: str,
    *,
    message: str,
    tokens_delivered: int = 0,
) -> None:
    """Tell the relay this job failed (``POST /relay/v1/error/{txn}``)."""
    try:
        with _client(signaling_url, access_token, timeout=10.0) as client:
            resp = client.post(
                f"/relay/v1/error/{txn_id}",
                json={"error": message, "tokens_delivered": tokens_delivered},
            )
    except httpx.HTTPError as exc:
        raise RelayError(f"submit_error transport error: {exc}") from None
    # 404 = the txn is already terminal server-side; not an error worth raising on.
    if resp.status_code == 404:
        return
    _guard(resp, "submit_error")


# ---------------------------------------------------------------------------
# Consumer (app) side: send a request through the relay and read the result.
# The orchestration (resolve grid, build payload, consume the SSE) lives in
# cli/cloud_request.py; this module owns only the wire boundary (base URL,
# Bearer, the optional routing headers) so it stays the one relay contract.
# ---------------------------------------------------------------------------


def open_consumer_client(
    signaling_url: str, access_token: str, *, timeout: float | httpx.Timeout
) -> httpx.Client:
    """A relay client for the *consumer* side: the same ``signaling_url`` base + Bearer as the
    provider client. Returned (not used internally) so the caller can ``.post()`` chat and
    ``.stream()`` media against ``/relay/v1/...`` and close the client itself — the response
    context manager closes the response, not the client.
    """
    return _client(signaling_url, access_token, timeout=timeout)


def consumer_headers(
    *, target_provider: str | None = None, allow_self_provider: bool = False
) -> dict[str, str]:
    """The optional routing headers for a consumer request (the cloud-only ``--target-provider`` /
    ``--allow-self-provider``, DECISIONS D16). Each is omitted unless set, so a plain request carries
    neither; the relay reads ``X-Allow-Self-Provider`` as the string ``"true"``.
    """
    headers: dict[str, str] = {}
    if target_provider:
        headers["X-Target-Provider"] = target_provider
    if allow_self_provider:
        headers["X-Allow-Self-Provider"] = "true"
    return headers
