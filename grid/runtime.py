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

from . import config, paths


NETWORK_TYPE = "lan-permissionless"
DEFAULT_PORT = 8090
DEFAULT_HOST = "0.0.0.0"


def slug_name(name: str) -> str:
    clean = "".join(ch.lower() if ch.isalnum() else "-" for ch in name).strip("-")
    return clean or f"network-{uuid.uuid4().hex[:8]}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def detect_lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"


def make_lan_url(port: int, advertise_host: str | None = None) -> str:
    host = (advertise_host or detect_lan_ip()).strip()
    return f"http://{host}:{int(port)}"


def normalize_url(value: str) -> str:
    url = value.strip().rstrip("/")
    if not url:
        raise SystemExit("URL must not be empty.")
    if not url.startswith(("http://", "https://")):
        url = f"http://{url}"
    return url.rstrip("/")


def init_network_config(
    *,
    name: str,
    port: int = DEFAULT_PORT,
    host: str = DEFAULT_HOST,
    network_id: str | None = None,
    advertise_host: str | None = None,
) -> dict[str, Any]:
    network_id = network_id or f"ag-{slug_name(name)}-{uuid.uuid4().hex[:8]}"
    data = {
        "network_id": network_id,
        "name": name,
        "network_type": NETWORK_TYPE,
        "managed_server": True,
        "host": host,
        "port": int(port),
        "lan_signaling_url": make_lan_url(port, advertise_host),
        "server_pid": 0,
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    config.save_network_config(network_id, data)
    return data


def start_server(cfg: dict[str, Any]) -> int:
    if not cfg.get("managed_server", True):
        raise SystemExit(f"{cfg['name']} is a remote LAN signaling URL; there is no local server to start.")

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

    log_path = paths.network_dir(cfg["network_id"]) / "server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("ab")
    proc = subprocess.Popen(
        _cli_subprocess_command() + ["__server", cfg["network_id"]],
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    cfg["server_pid"] = proc.pid
    cfg["updated_at"] = utc_now()
    config.save_network_config(cfg["network_id"], cfg)
    wait_for_health(cfg)
    return proc.pid


def stop_server(cfg: dict[str, Any]) -> None:
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
    config.save_network_config(cfg["network_id"], cfg)


def wait_for_health(cfg: dict[str, Any], timeout: int = 30) -> None:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{int(cfg['port'])}/server/info"
    while time.time() < deadline:
        try:
            resp = httpx.get(url, timeout=1)
            if resp.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.25)
    log = paths.network_dir(cfg["network_id"]) / "server.log"
    raise SystemExit(f"LAN signaling server did not become healthy. See {log}")


def network_url(cfg: dict[str, Any]) -> str:
    return str(cfg["lan_signaling_url"]).rstrip("/")


def provider_endpoint_url(endpoint_url: str | None, port: int, advertise_host: str | None = None) -> str:
    if endpoint_url:
        return normalize_url(endpoint_url)
    return f"{make_lan_url(port, advertise_host)}/v1"


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
    return [sys.executable, "-m", "grid.cli"]
