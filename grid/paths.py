from __future__ import annotations

import os
from pathlib import Path


def grid_home() -> Path:
    return Path(os.getenv("GRID_HOME", "~/.grid")).expanduser()


def home() -> Path:
    return grid_home()


def networks_dir() -> Path:
    return grid_home() / "networks"


def network_dir(network_id: str) -> Path:
    return networks_dir() / network_id


def ensure_base() -> None:
    networks_dir().mkdir(parents=True, exist_ok=True)


def bin_dir() -> Path:
    return grid_home() / "bin"


def llama_server_bin() -> Path:
    return bin_dir() / "llama-server"


def models_dir() -> Path:
    return grid_home() / "models"


def logs_dir() -> Path:
    return grid_home() / "logs"


def run_dir() -> Path:
    return grid_home() / "run"


def pid_file() -> Path:
    return run_dir() / "provider.pid"


def llama_log(port: int) -> Path:
    return logs_dir() / f"llama_llm_{port}.log"


def ensure_all() -> None:
    for directory in (grid_home(), networks_dir(), bin_dir(), models_dir(), logs_dir(), run_dir()):
        directory.mkdir(parents=True, exist_ok=True)
