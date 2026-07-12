"""Thin HTTP client for autonomous's hosted control plane (remote mode).

Ported and trimmed from ``grid-src/grid_cli/control_plane.py``: the device-code sign-in
surface (start/poll), the post-login token fetch, and the remote-grid lifecycle
(``*_managed_network`` — create/start/stop/status, repointed to ``/v1/grid/managed-networks``
per DECISIONS D11, authenticated with the account session token). The proprietary backend
(relay, Postgres, billing) is not here; this is a synchronous ``httpx`` client to the public
API. Remote mode is *allowed* to reach the remote — that is the feature — but nothing here
is reached in local mode (dispatch gates the remote commands to remote mode).
"""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import httpx

from . import credentials


# Control-plane HTTP timeout (seconds). The default suits every fast call; managed-network *create* is
# synchronous and can exceed it while the backend boots the master, so it is overridable via
# ``GRID_CONTROL_PLANE_TIMEOUT`` (a too-short timeout aborts the client mid-create, leaving the backend
# to finish on its own — a half-registered network).
_TIMEOUT = float(os.getenv("GRID_CONTROL_PLANE_TIMEOUT", "30"))


def _client(api_url: str | None = None, token: str | None = None) -> httpx.Client:
    headers = {"User-Agent": "grid-cli"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(base_url=credentials.api_url(api_url), headers=headers, timeout=_TIMEOUT)


def start_device_login(api_url: str | None = None) -> dict[str, Any]:
    with _client(api_url) as client:
        return _send(client, "POST", "/v1/grid/auth/device/start").json()


def poll_device_login(device_code: str, api_url: str | None = None) -> dict[str, Any]:
    with _client(api_url) as client:
        return _send(client, "POST", "/v1/grid/auth/device/poll", json={"device_code": device_code}).json()


def fetch_tokens(session_token: str, device_id: str, api_url: str | None = None) -> list[dict[str, Any]]:
    with _client(api_url, session_token) as client:
        resp = _send(client, "GET", "/v1/grid/tokens", params={"device_id": device_id})
        # `or []` coerces both a missing key and an explicit null to an empty list.
        return list(resp.json().get("networks") or [])


def refresh_network_token(
    *, network_id: str, refresh_token: str, api_url: str | None = None
) -> dict[str, Any]:
    """Exchange a per-grid refresh token for a fresh access token (relay auth, remote serve loop).

    Unauthenticated by design — the ``refresh_token`` in the body *is* the credential, so no
    session/access Bearer is attached (matches the reference client). A failed refresh surfaces as
    a clean ``SystemExit`` via ``_send``; the caller treats that as end-of-run.
    """
    with _client(api_url) as client:
        return _send(
            client, "POST", f"/v1/grid/tokens/{network_id}",
            json={"refresh_token": refresh_token},
        ).json()


def create_managed_network(
    session_token: str, name: str, network_type: str, api_url: str | None = None
) -> dict[str, Any]:
    with _client(api_url, session_token) as client:
        return _send(
            client, "POST", "/v1/grid/managed-networks",
            json={"name": name, "network_type": network_type},
        ).json()


def start_managed_network(session_token: str, network_id: str, api_url: str | None = None) -> dict[str, Any]:
    with _client(api_url, session_token) as client:
        return _send(client, "POST", f"/v1/grid/managed-networks/{network_id}/start").json()


def stop_managed_network(session_token: str, network_id: str, api_url: str | None = None) -> dict[str, Any]:
    with _client(api_url, session_token) as client:
        return _send(client, "POST", f"/v1/grid/managed-networks/{network_id}/stop").json()


def get_managed_network_status(session_token: str, network_id: str, api_url: str | None = None) -> dict[str, Any]:
    with _client(api_url, session_token) as client:
        return _send(client, "GET", f"/v1/grid/managed-networks/{network_id}/status").json()


def add_member(
    session_token: str, network_id: str, email: str, roles: list[str], api_url: str | None = None
) -> dict[str, Any]:
    """Add (or update) a member of a remote grid with the given role(s). Account-level — the session
    token authorises it. ``roles`` is sent as-is: ``both`` is a first-class role, not an expansion."""
    with _client(api_url, session_token) as client:
        return _json_or_empty(_send(
            client, "POST", f"/v1/grid/managed-networks/{network_id}/members",
            json={"email": email, "roles": roles},
        ))


def remove_member(
    session_token: str, network_id: str, email: str, api_url: str | None = None
) -> dict[str, Any]:
    """Remove a member from a remote grid. ``email`` is percent-encoded into a single path segment so a
    stray ``/`` (or other path char) cannot re-target the request — the boundary ``network_id``'s
    regex guards in ``cli/remote_grid.py``. A successful DELETE may answer ``204 No Content``."""
    with _client(api_url, session_token) as client:
        return _json_or_empty(_send(
            client, "DELETE",
            f"/v1/grid/managed-networks/{network_id}/members/{quote(email, safe='')}",
        ))


def list_members(
    session_token: str, network_id: str, api_url: str | None = None
) -> list[dict[str, Any]]:
    """The members of a remote grid. Unwrap defensively: accept both the ``{"members": [...]}``
    envelope (like ``fetch_tokens``) and a bare array; ``or []`` coerces a missing key / null."""
    with _client(api_url, session_token) as client:
        data = _json_or_empty(_send(client, "GET", f"/v1/grid/managed-networks/{network_id}/members"))
    members = data.get("members") if isinstance(data, dict) else data
    return list(members or [])


# --- Auto-router owner config (ADR 0013, revised) -------------------------------------------------
# Account-level, session-token authorised, owner/admin-checked on the control plane. NOTE the
# ``/networks/`` path prefix (NOT ``/managed-networks/`` like the calls above): the per-grid router
# routes are registered only under ``/networks/{id}/router`` in grid-apis (the catalog GET is
# account-level under ``/router/catalog`` — no network). Reads/writes return the *masked* config
# (``{"enabled", "advisors": [{"provider", "model"}]}``, never a key or URL); mutations add
# ``"synced": bool``. Advisors are picked BY NAME from the platform catalog — the owner supplies
# neither a base URL nor a key (the platform carries both), so nothing secret rides these requests.


def get_router_config(session_token: str, network_id: str, api_url: str | None = None) -> dict[str, Any]:
    """The grid's masked router config: ``{"enabled", "advisors": [{"provider", "model"}]}`` in priority
    order. Never a key or base URL — the control plane masks both on every read."""
    with _client(api_url, session_token) as client:
        return _json_or_empty(_send(client, "GET", f"/v1/grid/networks/{network_id}/router"))


def enable_router(session_token: str, network_id: str, api_url: str | None = None) -> dict[str, Any]:
    """Turn auto-routing on. The control plane rejects enabling with zero advisors (clear 400)."""
    with _client(api_url, session_token) as client:
        return _json_or_empty(_send(client, "POST", f"/v1/grid/networks/{network_id}/router/enable"))


def disable_router(session_token: str, network_id: str, api_url: str | None = None) -> dict[str, Any]:
    """Turn auto-routing off."""
    with _client(api_url, session_token) as client:
        return _json_or_empty(_send(client, "POST", f"/v1/grid/networks/{network_id}/router/disable"))


def set_advisors(
    session_token: str, network_id: str, advisors: list[tuple[str, str | None]],
    api_url: str | None = None,
) -> dict[str, Any]:
    """Replace the whole advisor chain (1-3 ``{provider, model}`` pairs, order = priority). Each item is a
    ``(provider, model | None)`` tuple; a bare provider (``model is None``) is sent provider-only so the
    control plane resolves the catalog default. Replace-all — the posted list IS the chain. No key and no
    URL ride this request (the platform carries both). Server 400s (unknown provider, off-whitelist model
    listing the valid names, duplicate pair, >3) surface as a clean ``SystemExit`` via ``_send``."""
    body = {"advisors": [
        ({"provider": provider, "model": model} if model else {"provider": provider})
        for provider, model in advisors
    ]}
    with _client(api_url, session_token) as client:
        return _json_or_empty(_send(
            client, "PUT", f"/v1/grid/networks/{network_id}/router/advisors", json=body))


def remove_advisor(
    session_token: str, network_id: str, provider: str, model: str | None = None,
    api_url: str | None = None,
) -> dict[str, Any]:
    """Remove advisors by name: an exact ``provider`` + ``model`` removes one entry; a bare ``provider``
    (``model is None``) removes all of that provider's entries. Name(s) ride the query string (httpx
    percent-encodes them; there is no path segment to inject). A remove that matches nothing is a clear
    400 from the control plane."""
    params: dict[str, str] = {"provider": provider}
    if model:
        params["model"] = model
    with _client(api_url, session_token) as client:
        return _json_or_empty(_send(
            client, "DELETE", f"/v1/grid/networks/{network_id}/router/advisors", params=params))


def get_router_catalog(session_token: str, api_url: str | None = None) -> dict[str, Any]:
    """The advisor catalog backing ``grid router models`` — each provider, its whitelisted models, and the
    default. Account-level (any session token); no network, no admin, no grid running. Never a base URL or
    key. Shape: ``{"providers": [{"provider", "models": [...], "default_model"}]}``."""
    with _client(api_url, session_token) as client:
        return _json_or_empty(_send(client, "GET", "/v1/grid/router/catalog"))


def _send(client: httpx.Client, method: str, url: str, **kwargs: Any) -> httpx.Response:
    """One request with both failure modes surfaced as a clean SystemExit (never a traceback):
    a transport/connection error before a response, and a >=400 status after one."""
    try:
        resp = client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise SystemExit(f"Cannot reach the control plane ({method} {url}): {exc}") from None
    _raise(resp)
    return resp


def _raise(resp: httpx.Response) -> None:
    if resp.status_code >= 400:
        raise SystemExit(
            f"{resp.request.method} {resp.request.url} failed ({resp.status_code}): {resp.text[:400]}"
        )


def _json_or_empty(resp: httpx.Response) -> Any:
    """``resp.json()`` for a normal body, or ``{}`` for an empty one. A 2xx with no content — e.g. a
    ``204 No Content`` on a successful DELETE — must not crash ``.json()`` with a decode error."""
    return resp.json() if resp.content else {}
