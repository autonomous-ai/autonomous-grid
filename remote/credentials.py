"""Remote-mode credential store: the signed-in session + per-grid tokens.

Ported and trimmed from ``grid-src/grid_cli/config.py``. Persists to
``~/.grid/credentials.toml`` (TOML, ``0o600``) through the shared
``jsonio.atomic_write_bytes`` primitive — one hardened writer for both the JSON state
file and this secret-bearing store — and keeps a stable per-machine id in
``~/.grid/device.toml`` that survives logout. Reads use the stdlib ``tomllib``; writes use
``tomli_w``. Nothing here reaches the network; it is just disk + env resolution. The
active-grid *selection* lives in ``shared/state.py`` (single source of truth), so this file
deliberately has no ``active_network`` concept.
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import tomllib
import uuid
from pathlib import Path
from typing import Any

import tomli_w

from shared import jsonio, paths


# The web frontend path that completes the device hand-off. Hardcoded — unlike the two URLs
# below it is not env-configurable (matches grid-src config.py).
GRID_LOGIN_PATH = "/grid/device-login"


def default_api_url() -> str:
    """Control-plane base URL — ``GRID_CONTROL_PLANE_URL`` or the hosted default."""
    return os.getenv("GRID_CONTROL_PLANE_URL", "https://api-grid.autonomous.ai").rstrip("/")


def default_website_url() -> str:
    """Sign-in web frontend — ``GRID_WEBSITE_URL`` or the hosted default.

    An explicit empty ``GRID_WEBSITE_URL`` returns ``""`` on purpose: the caller then falls
    back to the server-provided ``verification_uri_complete`` instead of constructing a URL.
    """
    return os.environ.get("GRID_WEBSITE_URL", "https://autonomous.ai").rstrip("/")


def load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        # Mirror jsonio.load_json: a corrupt/unreadable store gives a clean message, not a
        # raw traceback on every `grid` invocation.
        raise SystemExit(f"Cannot read {path}: {exc}") from None


def atomic_write_toml(path: Path, data: dict[str, Any], mode: int = 0o600) -> None:
    jsonio.atomic_write_bytes(path, tomli_w.dumps(data).encode("utf-8"), mode)


def device_id() -> str:
    """A stable per-machine id, generated once and reused; independent of credentials."""
    existing = load_toml(paths.device_file()).get("device_id")
    if existing:
        return str(existing)
    value = str(uuid.uuid4())
    atomic_write_toml(paths.device_file(), {"device_id": value})
    return value


def load_credentials() -> dict[str, Any]:
    return load_toml(paths.credentials_file())


def save_credentials(data: dict[str, Any]) -> None:
    atomic_write_toml(paths.credentials_file(), data)
    # Best-effort: keep the home dir owner-only (like ~/.ssh) so a local user can't even list
    # that a credential file exists. The file itself is already 0o600.
    with contextlib.suppress(OSError):
        paths.grid_home().chmod(0o700)


def add_network(record: dict[str, Any]) -> None:
    """Register a remote grid in the local store (e.g. one just created by ``grid up``).

    Idempotent by ``network_id`` — re-adding the same grid drops the stale entry and re-appends
    the new one — and preserves the rest of the credential file (session token, api_url, user).
    Immutable update: a fresh dict is written, never the loaded one mutated in place.
    """
    data = load_credentials()
    others = [n for n in (data.get("networks") or []) if n.get("network_id") != record.get("network_id")]
    save_credentials({**data, "networks": [*others, record]})


def update_network_tokens(
    network_id: str, *, access_token: str, refresh_token: str | None = None
) -> None:
    """Persist refreshed per-grid tokens for one network, in place.

    Used by the remote serve loop after a relay 401 → token refresh. Immutable update: rebuilds the
    file with the matching bundle's ``access_token`` (and ``refresh_token`` when the server rotated
    it) replaced — every other bundle and the rest of the file untouched and in original order. A
    no-op if no bundle matches ``network_id`` (the caller resolved it before joining, so it exists).
    """
    data = load_credentials()
    networks = []
    found = False
    for net in data.get("networks") or []:
        if net.get("network_id") == network_id:
            found = True
            merged = {**net, "access_token": access_token}
            if refresh_token is not None:
                merged["refresh_token"] = refresh_token
            networks.append(merged)
        else:
            networks.append(net)
    if not found:
        # Nothing to update (e.g. a concurrent `grid logout`) — don't rewrite the file, which would
        # clobber whatever is there now and silently drop the refreshed token.
        return
    save_credentials({**data, "networks": networks})


def clear_credentials() -> bool:
    """Delete the credential store. Returns whether it existed (for an honest logout message)."""
    try:
        paths.credentials_file().unlink()
        return True
    except FileNotFoundError:
        return False  # already signed out — idempotent, no race window
    except OSError as exc:
        raise SystemExit(f"Could not remove {paths.credentials_file()}: {exc}") from None


def api_url(explicit: str | None = None) -> str:
    """Effective control-plane URL: explicit arg > stored (post-login) > env/default."""
    if explicit:
        return explicit.rstrip("/")
    stored = load_credentials().get("api_url")
    return str(stored).rstrip("/") if stored else default_api_url()


def require_session() -> str:
    """The stored session token, or a clear 'sign in first' error.

    The auth gate every remote command that needs identity calls before doing work.
    """
    token = load_credentials().get("session_token")
    if not token:
        raise SystemExit("You're not signed in. Run `grid login` to sign in.")
    return str(token)


def node_id_from_token(access_token: str) -> str:
    """The provider node_id, read from a per-grid access token's JWT ``node_id`` claim.

    The relay authorizes ``PUT /nodes/{node_id}`` only for the node the token belongs to — any other
    id is rejected with 403 "Cannot access another node". So node_id is NOT ours to invent (a random
    ``node-<uuid>`` is exactly what the relay refuses, and the run record's ``node_id`` field is a
    junk display value); it must come from the token. The serve loop (``remote/serve.py``) registers
    under it; ``grid leave`` addresses its backstop deregister to it. Decode the JWT payload
    best-effort and read the claim — no signature check (the relay verifies server-side; we only need
    the claim to address our own node). Returns "" when the token isn't a decodable JWT carrying a
    node_id, so the caller can surface a clean re-login error.
    """
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # restore the base64 padding a JWT strips
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, json.JSONDecodeError):
        return ""
    node_id = claims.get("node_id") if isinstance(claims, dict) else None
    return str(node_id) if node_id else ""
