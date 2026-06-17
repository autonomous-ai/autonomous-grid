"""`grid network` commands: create and manage LAN signaling servers."""
from __future__ import annotations

import argparse
from typing import Any

import httpx

from .. import config, runtime


def cmd_network_create(args: argparse.Namespace) -> int:
    existing = _network_by_name(args.name)
    cfg = existing or runtime.init_network_config(
        name=args.name,
        port=args.port,
        host=args.host,
        network_id=args.network_id,
        advertise_host=args.advertise_host,
    )
    pid = runtime.start_server(cfg)
    print(f"Started LAN network {cfg['name']} ({cfg['network_id']})")
    print(f"network_type={cfg['network_type']}")
    print(f"signaling_url={runtime.network_url(cfg)}")
    print(f"server_pid={pid}")
    return 0


def cmd_network_start(args: argparse.Namespace) -> int:
    cfg = config.select_network(args.network)
    pid = runtime.start_server(cfg)
    print(f"Started {cfg['name']} at {runtime.network_url(cfg)} pid={pid}")
    return 0


def cmd_network_stop(args: argparse.Namespace) -> int:
    cfg = config.select_network(args.network)
    if not cfg.get("managed_server", True):
        print(f"{cfg['name']} is hosted by another LAN device; nothing to stop locally.")
        return 0
    runtime.stop_server(cfg)
    print(f"Stopped {cfg['name']}.")
    return 0


def cmd_network_status(args: argparse.Namespace) -> int:
    cfg = config.select_network(args.network)
    print(f"name={cfg['name']}")
    print(f"network_id={cfg['network_id']}")
    print(f"network_type={cfg['network_type']}")
    print(f"signaling_url={runtime.network_url(cfg)}")
    print(f"managed_server={str(bool(cfg.get('managed_server', True))).lower()}")
    print(f"server_pid={int(cfg.get('server_pid') or 0)}")
    try:
        info = httpx.get(f"{runtime.network_url(cfg)}/server/info", timeout=2).json()
    except Exception as exc:
        print(f"server_status=unreachable ({exc})")
    else:
        print("server_status=reachable")
        print(f"providers_online={info.get('providers_online', 0)}")
    return 0


def cmd_network_list(args: argparse.Namespace) -> int:
    networks = config.iter_network_configs()
    if not networks:
        print("(no saved networks)")
        return 0
    for cfg in networks:
        managed = "local" if cfg.get("managed_server", True) else "remote"
        print(f"{cfg['name']}\t{cfg['network_id']}\t{managed}\t{runtime.network_url(cfg)}")
    return 0


def _network_by_name(name_or_id: str) -> dict[str, Any] | None:
    for cfg in config.iter_network_configs():
        if cfg.get("name") == name_or_id or cfg.get("network_id") == name_or_id:
            return cfg
    return None

