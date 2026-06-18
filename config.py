from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import paths


CONFIG_FILE = "config.json"


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise SystemExit(f"Invalid config file: {path}")
    return data


def atomic_write_json(path: Path, data: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def network_config_path(network_id: str) -> Path:
    return paths.network_dir(network_id) / CONFIG_FILE


def load_network_config(network_id: str) -> dict[str, Any]:
    return load_json(network_config_path(network_id))


def save_network_config(network_id: str, data: dict[str, Any]) -> None:
    atomic_write_json(network_config_path(network_id), data)


def iter_network_configs() -> list[dict[str, Any]]:
    root = paths.networks_dir()
    if not root.exists():
        return []
    configs: list[dict[str, Any]] = []
    for path in sorted(root.glob(f"*/{CONFIG_FILE}")):
        data = load_json(path)
        if data:
            configs.append(data)
    return configs


def select_grid(name_or_id: str | None) -> dict[str, Any]:
    """Resolve the grid to act on.

    Honors the CLI convention: when a name is given, look it up; when omitted,
    default to the only grid, else the one named ``home``, else ask the caller
    to name one.
    """
    if name_or_id:
        return select_network(name_or_id)
    grids = iter_network_configs()
    if not grids:
        raise SystemExit("No grids yet. Run `grid up` to bring one online.")
    if len(grids) == 1:
        return grids[0]
    for cfg in grids:
        if cfg.get("name") == "home":
            return cfg
    names = ", ".join(sorted(cfg.get("name", cfg["network_id"]) for cfg in grids))
    raise SystemExit(f"Several grids exist ({names}); name one, e.g. `grid info <grid>`.")


def select_network(name_or_id: str) -> dict[str, Any]:
    if _looks_like_signaling_url(name_or_id):
        url = _normalize_signaling_url(name_or_id)
        return {
            "network_id": url,
            "name": url,
            "network_type": "lan-permissionless",
            "managed_server": False,
            "host": "",
            "port": 0,
            "lan_signaling_url": url,
            "server_pid": 0,
        }
    matches = [
        cfg for cfg in iter_network_configs()
        if cfg.get("network_id") == name_or_id or cfg.get("name") == name_or_id
    ]
    if not matches:
        raise SystemExit(
            f"Grid not found: {name_or_id!r}. Run `grid up {name_or_id}` "
            "on this device or pass a grid URL."
        )
    return matches[-1]


def _looks_like_signaling_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def _normalize_signaling_url(value: str) -> str:
    url = value.strip().rstrip("/")
    if not url:
        raise SystemExit("Network URL must not be empty.")
    return url
