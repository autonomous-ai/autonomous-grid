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
import json
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from shared import state


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

    credentials.require_session()
    if args.subcommand != "info":
        raise SystemExit(f"Unknown relay subcommand: {args.subcommand!r}")

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
