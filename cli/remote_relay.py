"""Read-only relay inventory for remote Grids.

A Goal belongs to a Grid, and a Grid belongs to a relay.  This command deliberately
derives the last relationship from the locally stored Grid records: no Goal row grows a
second, drift-prone ``relay_id`` and no separate "fleet" registry is needed.

Newer control-plane bundles may carry ``relay_id``.  Older and disposable bundles carry
only ``signaling_url``/``lan_signaling_url``; the normalized URL is their stable fallback
identity.  Supporting both makes ``grid relay info`` useful during a rolling upgrade.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from shared import state


PAIRING_VERSION = 1
MAX_PAIRING_BYTES = 32 * 1024
_HOST_COMMANDS = frozenset({
    "list", "up", "down", "restart", "status", "invite", "revoke", "set-url", "backup",
    "restore", "destroy", "supervise", "service",
})


def _networks() -> list[dict[str, Any]]:
    from remote import credentials

    raw = credentials.load_credentials().get("networks") or []
    return [record for record in raw if isinstance(record, dict)]


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _relay_url(record: dict[str, Any]) -> str:
    return _text(record.get("signaling_url") or record.get("lan_signaling_url"))


def _normalized_url(value: object) -> str:
    """Comparable relay URL without changing the address printed to the operator."""
    raw = _text(value)
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        if not parsed.scheme or not parsed.netloc:
            return raw.rstrip("/")
        # Hosts and schemes are case-insensitive; paths are not.  A root trailing slash is
        # cosmetic, and fragments never identify a server.
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(),
                           parsed.path.rstrip("/"), parsed.query, ""))
    except ValueError:
        # A malformed stored value is still displayable/selectable.  Other commands own the
        # stronger validation at the point where they make a request to it.
        return raw.rstrip("/")


def _relay_id(record: dict[str, Any]) -> str:
    return _text(record.get("relay_id"))


def _grid_matches(record: dict[str, Any], selector: str) -> bool:
    return selector in {_text(record.get("network_id")), _text(record.get("name"))}


def _component(networks: list[dict[str, Any]], target: dict[str, Any]) -> list[dict[str, Any]]:
    """All Grid records connected to ``target`` by relay id or URL.

    The fixed point matters during a migration: URL-a can connect an old record to a new
    id-bearing record, while that id connects it to a record already advertising URL-b.
    """
    connected = [target]
    remaining = [record for record in networks if record is not target]
    changed = True
    while changed:
        changed = False
        ids = {_relay_id(record) for record in connected} - {""}
        urls = {_normalized_url(_relay_url(record)) for record in connected} - {""}
        for record in list(remaining):
            relay_id = _relay_id(record)
            relay_url = _normalized_url(_relay_url(record))
            if ((relay_id and relay_id in ids) or (relay_url and relay_url in urls)):
                remaining.remove(record)
                connected.append(record)
                changed = True
    return connected


def _target(networks: list[dict[str, Any]], selector: str | None) -> dict[str, Any]:
    if selector:
        # A Grid is the most convenient unambiguous route to its relay.
        grid = next((record for record in networks if _grid_matches(record, selector)), None)
        if grid is not None:
            return grid

        normalized = _normalized_url(selector)
        relay = next((record for record in networks
                      if selector == _relay_id(record)
                      or (normalized and normalized == _normalized_url(_relay_url(record)))), None)
        if relay is None:
            raise SystemExit(
                f"Relay not found: {selector!r}. Name a relay id, relay URL, or one of your Grids."
            )
        return relay

    active = state.get_active("remote")
    if active:
        grid = next((record for record in networks if _grid_matches(record, active)), None)
        if grid is not None:
            return grid

    # No active Grid: a sole relay is still unambiguous even when several Grids use it. Build
    # connected components because a rolling upgrade can bridge identities transitively:
    # old(URL-a) <-> new(id-1, URL-a) <-> moved(id-1, URL-b).
    remaining = [record for record in networks if _relay_id(record) or _relay_url(record)]
    components: list[list[dict[str, Any]]] = []
    while remaining:
        seed = remaining[0]
        component = _component(remaining, seed)
        member_ids = {id(record) for record in component}
        remaining = [record for record in remaining if id(record) not in member_ids]
        components.append(component)
    if len(components) == 1:
        # Prefer a record carrying the newer server-issued identity for the displayed target.
        return next((record for record in components[0] if _relay_id(record)), components[0][0])
    if not components:
        raise SystemExit("No relay information is stored for your Grids. Run `grid sync` and retry.")
    raise SystemExit(
        "More than one relay is configured. Name a relay id, relay URL, or Grid "
        "(`grid ls` shows your Grids)."
    )


def cmd_remote_relay(args: argparse.Namespace) -> int:
    from remote import credentials

    if args.subcommand == "connect":
        return _connect(args)
    if args.subcommand == "disconnect":
        return _disconnect(args)
    if args.subcommand in _HOST_COMMANDS:
        return _host(args)
    if args.subcommand != "info":
        raise SystemExit(f"Unknown relay subcommand: {args.subcommand!r}")
    if getattr(args, "mode", None) != "remote":
        raise SystemExit("`grid relay info` reads remote Grid records. Pass `--remote` or switch modes.")
    credentials.require_session()

    networks = _networks()
    target = _target(networks, args.relay)
    component = _component(networks, target)
    # Prefer what the selected record says. For an old URL-only record, adopt the server id from
    # a connected refreshed record; for an id-only record, adopt a connected advertised URL.
    relay_id = _relay_id(target) or next(
        (_relay_id(record) for record in component if _relay_id(record)), "")
    relay_url = _relay_url(target) or next(
        (_relay_url(record) for record in component if _relay_url(record)), "")
    normalized_url = _normalized_url(relay_url)
    if not relay_id and not normalized_url:
        label = _text(target.get("name")) or _text(target.get("network_id")) or "selected Grid"
        raise SystemExit(
            f"Grid {label!r} has no relay information locally. Run `grid sync` and retry."
        )

    grids = [
        {
            "grid": _text(record.get("name")) or _text(record.get("network_id")),
            "id": _text(record.get("network_id")),
        }
        for record in component
    ]
    grids.sort(key=lambda item: (item["grid"].casefold(), item["id"]))
    view = {
        "relay_id": relay_id or None,
        "relay_url": relay_url or None,
        "grids": grids,
    }
    if args.json:
        print(json.dumps(view, indent=2))
        return 0

    print(f"relay_id={relay_id}")
    print(f"relay_url={relay_url}")
    print(f"grids={len(grids)}")
    for grid in grids:
        print(f"  {grid['grid']}\t{grid['id']}")
    return 0


def _decode_bundle(raw: str) -> dict[str, Any]:
    compact = "".join(str(raw or "").split())
    if not compact or len(compact) > MAX_PAIRING_BYTES:
        raise SystemExit("Pairing bundle is missing or unexpectedly large.")
    try:
        decoded = base64.urlsafe_b64decode(compact + "=" * (-len(compact) % 4))
        value = json.loads(decoded)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit("Pairing bundle is not valid base64url JSON.") from exc
    if not isinstance(value, dict) or value.get("version") != PAIRING_VERSION:
        raise SystemExit(f"Pairing bundle must use version {PAIRING_VERSION}.")
    return value


def _root_url(value: object) -> str:
    raw = str(value or "").strip().rstrip("/")
    parsed = urlsplit(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise SystemExit("Pairing bundle has no valid HTTP relay URL.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise SystemExit("Pairing bundle relay URL must be a credential-free root URL.")
    try:
        parsed.port
    except ValueError as exc:
        raise SystemExit("Pairing bundle relay URL has an invalid port.") from exc
    return raw


def _bundle_value(args: argparse.Namespace) -> str:
    if args.bundle and args.bundle_file:
        raise SystemExit("Use either --bundle or --bundle-file, not both.")
    if args.bundle_file:
        path = Path(args.bundle_file).expanduser()
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SystemExit(f"Cannot read pairing bundle {path}: {exc}") from None
    if args.bundle:
        return args.bundle
    return getpass.getpass("Paste relay pairing bundle (input hidden): ")


def _connect(args: argparse.Namespace) -> int:
    from remote import credentials

    value = _decode_bundle(_bundle_value(args))
    relay_url = _root_url(value.get("relay_url"))
    required = (
        "relay_id", "server_id", "network_id", "network_name", "network_type", "access_token",
        "node_id",
    )
    missing = [key for key in required if not str(value.get(key) or "").strip()]
    if missing:
        raise SystemExit(f"Pairing bundle is missing: {', '.join(missing)}.")
    claims = credentials.claims_from_token(value["access_token"])
    signed_identity = {
        "relay_id", "relay_url", "server_id", "network_id", "network_name", "network_type",
        "node_id",
    }
    if any(claims.get(key) != value.get(key) for key in signed_identity):
        raise SystemExit("Pairing bundle identity does not match its signed credential.")
    expires_at = value.get("expires_at")
    if not isinstance(expires_at, int) or isinstance(expires_at, bool) or expires_at <= int(time.time()):
        raise SystemExit("Pairing bundle credential is expired or has no valid expiry.")
    try:
        response = httpx.get(f"{relay_url}/server/info", timeout=10)
        response.raise_for_status()
        server = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SystemExit(f"Relay is not reachable at {relay_url}: {exc}") from None
    if not isinstance(server, dict) or not server.get("server_id"):
        raise SystemExit(f"{relay_url} answered, but it is not a Grid relay.")
    if str(server["server_id"]) != str(value["server_id"]):
        raise SystemExit("Pairing bundle points at a different Grid relay; refusing to connect.")

    current = credentials.load_credentials()
    record = {
        "network_id": str(value["network_id"]),
        "name": str(value["network_name"]),
        "network_type": str(value["network_type"]),
        "relay_id": str(value["relay_id"]),
        "signaling_url": relay_url,
        "lan_signaling_url": relay_url,
        "access_token": str(value["access_token"]),
        "node_id": str(value["node_id"]),
        "email": str(value.get("email") or ""),
        "roles": list(value.get("roles") or []),
        "scopes": list(value.get("scopes") or []),
        "expires_at": expires_at,
        "self_hosted": True,
    }
    networks = [
        item for item in (current.get("networks") or [])
        if isinstance(item, dict) and item.get("network_id") != record["network_id"]
    ]
    networks.append(record)
    credentials.save_credentials({
        **current,
        "session_token": current.get("session_token") or f"self-hosted:{record['relay_id']}",
        "user": current.get("user") or {"email": record["email"]},
        "networks": networks,
    })
    state.set_mode("remote")
    state.set_active("remote", record["network_id"])
    print(f"Connected to self-hosted Grid {record['name']} through relay {record['relay_id']}.")
    print(f"relay_url={relay_url}")
    print(f"node={record['node_id']}")
    return 0


def _disconnect(args: argparse.Namespace) -> int:
    from remote import credentials

    data = credentials.load_credentials()
    selector = args.relay
    removed = [item for item in (data.get("networks") or []) if isinstance(item, dict) and (
        item.get("self_hosted") and selector in {
            item.get("network_id"), item.get("name"), item.get("relay_id"),
        }
    )]
    if not removed:
        raise SystemExit(f"Self-hosted relay/Grid not found locally: {selector!r}.")
    remove_ids = {item.get("network_id") for item in removed}
    networks = [
        item for item in (data.get("networks") or [])
        if not isinstance(item, dict) or item.get("network_id") not in remove_ids
    ]
    credentials.save_credentials({**data, "networks": networks})
    active = state.get_active("remote")
    if active in remove_ids:
        state.set_active("remote", None)
    print(f"Disconnected {len(removed)} self-hosted Grid(s). The relay was not stopped.")
    return 0


def _host(args: argparse.Namespace) -> int:
    executable = os.getenv("GRID_RELAY_BIN") or shutil.which("grid-relay")
    if not executable:
        raise SystemExit(
            "The relay host runtime is not installed. Install the `grid-relay` package on this "
            "machine, then retry. Client-only machines need only `grid relay connect`."
        )
    forwarded = [str(executable), args.subcommand, *list(args.host_args or [])]
    return subprocess.run(forwarded, check=False).returncode
