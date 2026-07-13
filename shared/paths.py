from __future__ import annotations

import os
from pathlib import Path


def grid_home() -> Path:
    return Path(os.getenv("GRID_HOME", "~/.grid")).expanduser()


def home() -> Path:
    return grid_home()


def credentials_file() -> Path:
    """Remote-mode credential store (TOML, 0o600). Absent ⇒ signed out."""
    return grid_home() / "credentials.toml"


def device_file() -> Path:
    """Stable per-machine device id (TOML). Survives logout."""
    return grid_home() / "device.toml"


def api_keys_file() -> Path:
    """Machine-local API-engine key store (TOML, 0o600), keyed by service kind. Survives logout —
    deliberately separate from the sign-in credential store (like device.toml)."""
    return grid_home() / "api_keys.toml"


def grids_dir() -> Path:
    return grid_home() / "grids"


def grid_dir(grid_id: str) -> Path:
    return grids_dir() / grid_id


def ensure_base() -> None:
    grids_dir().mkdir(parents=True, exist_ok=True)


def bin_dir() -> Path:
    return grid_home() / "bin"


def llama_server_bin() -> Path:
    return bin_dir() / "llama-server"


def llama_prefix_dir() -> Path:
    """Where a prebuilt llama.cpp is unpacked. The macOS binaries link their shared libraries
    through `@loader_path`, so `llama-server` only runs with its `.dylib`s beside it — they get a
    directory of their own, and `bin/llama-server` is a symlink into it."""
    return grid_home() / "engines" / "llama.cpp"


def tools_dir() -> Path:
    """Where `uv` keeps the agent tools it installs for Grid (one venv per tool)."""
    return grid_home() / "tools"


def python_dir() -> Path:
    """Where `uv` downloads the private CPython those tools run on. It lives under ~/.grid so Grid
    owns what Grid installed — and removing ~/.grid removes it too."""
    return grid_home() / "python"


def models_dir() -> Path:
    return grid_home() / "models"


def logs_dir() -> Path:
    return grid_home() / "logs"


def run_dir() -> Path:
    return grid_home() / "run"


def engines_dir(grid_id: str) -> Path:
    return run_dir() / "engines" / grid_id


def llama_log(port: int) -> Path:
    return logs_dir() / f"llama_llm_{port}.log"


def ensure_all() -> None:
    for directory in (grid_home(), grids_dir(), bin_dir(), models_dir(), logs_dir(), run_dir()):
        directory.mkdir(parents=True, exist_ok=True)
