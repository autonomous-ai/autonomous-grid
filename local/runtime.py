from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from local import config
from shared import logging_setup, paths


GRID_TYPE = "lan-permissionless"
DEFAULT_PORT = 8090
DEFAULT_HOST = "0.0.0.0"


def slug_name(name: str) -> str:
    clean = "".join(ch.lower() if ch.isalnum() else "-" for ch in name).strip("-")
    return clean or f"grid-{uuid.uuid4().hex[:8]}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def detect_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"


def make_local_url(port: int, advertise_host: str | None = None) -> str:
    host = (advertise_host or detect_local_ip()).strip()
    return f"http://{host}:{int(port)}"


def normalize_url(value: str) -> str:
    url = value.strip().rstrip("/")
    if not url:
        raise SystemExit("URL must not be empty.")
    if not url.startswith(("http://", "https://")):
        url = f"http://{url}"
    return url.rstrip("/")


def init_grid_config(
    *,
    name: str,
    port: int = DEFAULT_PORT,
    host: str = DEFAULT_HOST,
    grid_id: str | None = None,
    advertise_host: str | None = None,
) -> dict[str, Any]:
    grid_id = grid_id or f"ag-{slug_name(name)}-{uuid.uuid4().hex[:8]}"
    data = {
        "grid_id": grid_id,
        "name": name,
        "grid_type": GRID_TYPE,
        "managed_server": True,
        "host": host,
        "port": int(port),
        "lan_signaling_url": make_local_url(port, advertise_host),
        "server_pid": 0,
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    config.save_grid_config(grid_id, data)
    return data


def start_grid(cfg: dict[str, Any]) -> int:
    if not cfg.get("managed_server", True):
        raise SystemExit(f"{cfg['name']} is a remote signaling URL; there is no local server to start.")

    pid = int(cfg.get("server_pid") or 0)
    if pid and _pid_alive(pid):
        try:
            wait_for_health(cfg, timeout=3)
            return pid
        except SystemExit:
            pass

    port = int(cfg["port"])
    if _tcp_port_in_use("127.0.0.1", port):
        raise SystemExit(f"Port {port} is already in use. Choose a different --port.")

    # The rotating handler inside the __server child owns server.log; this raw redirect captures
    # only bootstrap/crash output (stays tiny — the server has no print()), capped on each start.
    log_path = paths.grid_dir(cfg["grid_id"]) / "server.err"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = logging_setup.cap_and_open_append(log_path, logging_setup.ERR_LOG_MAX_BYTES)
    proc = subprocess.Popen(
        _cli_subprocess_command() + ["__server", cfg["grid_id"]],
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    cfg["server_pid"] = proc.pid
    cfg["updated_at"] = utc_now()
    config.save_grid_config(cfg["grid_id"], cfg)
    wait_for_health(cfg)
    return proc.pid


def stop_grid(cfg: dict[str, Any]) -> None:
    pid = int(cfg.get("server_pid") or 0)
    if not pid:
        return
    try:
        if hasattr(os, "killpg"):
            os.killpg(pid, 15)
        else:
            os.kill(pid, 15)
    except ProcessLookupError:
        pass
    cfg["server_pid"] = 0
    cfg["updated_at"] = utc_now()
    config.save_grid_config(cfg["grid_id"], cfg)


def wait_for_health(cfg: dict[str, Any], timeout: int = 30) -> None:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{int(cfg['port'])}/grid/info"
    while time.time() < deadline:
        try:
            resp = httpx.get(url, timeout=1)
            if resp.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.25)
    grid_dir = paths.grid_dir(cfg["grid_id"])
    raise SystemExit(
        "local signaling server did not become healthy. "
        f"See {grid_dir / 'server.log'} (and {grid_dir / 'server.err'} for bootstrap/crash output)"
    )


def grid_url(cfg: dict[str, Any]) -> str:
    return str(cfg["lan_signaling_url"]).rstrip("/")


def engine_endpoint_url(endpoint_url: str | None, port: int, advertise_host: str | None = None) -> str:
    if endpoint_url:
        return normalize_url(endpoint_url)
    return f"{make_local_url(port, advertise_host)}/v1"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _tcp_port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) == 0


def cli_command() -> list[str]:
    """The argv prefix that re-invokes this CLI (for detached subprocesses)."""
    return _cli_subprocess_command()


def _cli_subprocess_command() -> list[str]:
    argv0 = sys.argv[0] if sys.argv else ""
    candidates: list[Path] = []
    if argv0:
        candidates.append(Path(argv0).expanduser())
        resolved = shutil.which(argv0)
        if resolved:
            candidates.append(Path(resolved))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return [str(candidate.resolve())]
    return [sys.executable, "-m", "cli"]
