from __future__ import annotations

from pathlib import Path
from typing import Any

from shared import paths, state
from shared.jsonio import atomic_write_json, load_json


CONFIG_FILE = "config.json"


def grid_config_path(grid_id: str) -> Path:
    return paths.grid_dir(grid_id) / CONFIG_FILE


def load_grid_config(grid_id: str) -> dict[str, Any]:
    return load_json(grid_config_path(grid_id))


def save_grid_config(grid_id: str, data: dict[str, Any]) -> None:
    atomic_write_json(grid_config_path(grid_id), data)


def iter_grid_configs() -> list[dict[str, Any]]:
    root = paths.grids_dir()
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
    prefer the persisted active selection (``grid use``), else the only grid, else
    the one named ``home``, else ask the caller to name one. A stale active selection
    (its grid was removed) is ignored rather than raising.
    """
    if name_or_id:
        if _looks_like_signaling_url(name_or_id):
            url = _normalize_signaling_url(name_or_id)
            return {
                "grid_id": url,
                "name": url,
                "grid_type": "lan-permissionless",
                "managed_server": False,
                "host": "",
                "port": 0,
                "lan_signaling_url": url,
                "server_pid": 0,
            }
        matches = [
            cfg for cfg in iter_grid_configs()
            if cfg.get("grid_id") == name_or_id or cfg.get("name") == name_or_id
        ]
        if not matches:
            raise SystemExit(
                f"Grid not found: {name_or_id!r}. Run `grid up {name_or_id}` "
                "on this device or pass a grid URL."
            )
        return matches[-1]
    grids = iter_grid_configs()
    if not grids:
        raise SystemExit("No grids yet. Run `grid up` to bring one online.")
    active = state.get_active("lan")
    if active:
        for cfg in grids:
            if cfg.get("grid_id") == active or cfg.get("name") == active:
                return cfg
        # a stale active selection (its grid was removed) falls through to the default
    if len(grids) == 1:
        return grids[0]
    for cfg in grids:
        if cfg.get("name") == "home":
            return cfg
    names = ", ".join(sorted(cfg.get("name", cfg["grid_id"]) for cfg in grids))
    raise SystemExit(f"Several grids exist ({names}); name one, e.g. `grid info <grid>`.")


def _looks_like_signaling_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def _normalize_signaling_url(value: str) -> str:
    url = value.strip().rstrip("/")
    if not url:
        raise SystemExit("Grid URL must not be empty.")
    return url
