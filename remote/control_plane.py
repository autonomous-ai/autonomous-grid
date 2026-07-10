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

from typing import Any
from urllib.parse import quote

import httpx

from . import credentials


def _client(api_url: str | None = None, token: str | None = None) -> httpx.Client:
    headers = {"User-Agent": "grid-cli"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(base_url=credentials.api_url(api_url), headers=headers, timeout=30.0)


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


# --- Auto-router owner config (ADR 0013) ----------------------------------------------------------
# Account-level, session-token authorised, owner/admin-checked on the control plane. NOTE the
# ``/networks/`` path prefix (NOT ``/managed-networks/`` like the calls above): the router routes are
# registered only under ``/networks/{id}/router`` in grid-apis. Reads/writes return the *masked*
# config (``{"enabled", "rankers": [{"position", "base_url", "model"}]}``, never a key); mutations add
# ``"synced": bool``. Ranker keys ride only this owner channel, in the ``set_ranker`` request body.


def get_router_config(session_token: str, network_id: str, api_url: str | None = None) -> dict[str, Any]:
    """The grid's masked router config (enabled state + each ranker's position/base_url/model). Never
    carries key material — the control plane strips it on every read."""
    with _client(api_url, session_token) as client:
        return _json_or_empty(_send(client, "GET", f"/v1/grid/networks/{network_id}/router"))


def enable_router(session_token: str, network_id: str, api_url: str | None = None) -> dict[str, Any]:
    """Turn auto-routing on. The control plane rejects enabling with zero rankers (clear 400)."""
    with _client(api_url, session_token) as client:
        return _json_or_empty(_send(client, "POST", f"/v1/grid/networks/{network_id}/router/enable"))


def disable_router(session_token: str, network_id: str, api_url: str | None = None) -> dict[str, Any]:
    """Turn auto-routing off."""
    with _client(api_url, session_token) as client:
        return _json_or_empty(_send(client, "POST", f"/v1/grid/networks/{network_id}/router/disable"))


def set_ranker(
    session_token: str, network_id: str, position: int, base_url: str, model: str, api_key: str,
    api_url: str | None = None,
) -> dict[str, Any]:
    """Set the ranker at ``position`` (1-3). ``api_key`` rides the body (sourced by the CLI from the
    ``GRID_RANKER_API_KEY`` env var or a hidden prompt — never a flag). The control plane validates
    scheme / non-empty model / key presence / contiguity and returns clear 400s."""
    with _client(api_url, session_token) as client:
        return _json_or_empty(_send(
            client, "PUT", f"/v1/grid/networks/{network_id}/router/rankers/{position}",
            json={"base_url": base_url, "model": model, "api_key": api_key},
        ))


def remove_ranker(
    session_token: str, network_id: str, position: int, api_url: str | None = None
) -> dict[str, Any]:
    """Remove the ranker at ``position`` (1-3). Removing the last ranker while enabled is a clear 400."""
    with _client(api_url, session_token) as client:
        return _json_or_empty(_send(
            client, "DELETE", f"/v1/grid/networks/{network_id}/router/rankers/{position}"))


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
