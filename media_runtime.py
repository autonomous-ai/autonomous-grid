from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

import paths


def start_media_server(*, port: int, comfyui_url: str) -> subprocess.Popen:
    if _tcp_port_in_use("127.0.0.1", port):
        raise SystemExit(f"Port {port} is already in use; cannot start provider media server.")
    paths.ensure_all()
    log_path = paths.logs_dir() / f"media_provider_{port}.log"
    log = log_path.open("ab")
    proc = subprocess.Popen(
        _cli_subprocess_command() + [
            "__media-server",
            "--port",
            str(port),
            "--comfyui-url",
            comfyui_url.rstrip("/"),
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    wait_for_media_server(port, log_path)
    return proc


def wait_for_media_server(port: int, log_path: Path, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _proc_dead_port_unbound(port):
            pass
        try:
            resp = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
            if resp.status_code == 200:
                return
        except httpx.RequestError:
            pass
        time.sleep(0.25)
    raise SystemExit(f"Provider media server did not become healthy. See {log_path}")


def stop_media_server(proc: subprocess.Popen, *, timeout: float = 10.0) -> None:
    if proc.poll() is not None:
        return
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, 15)
        else:
            proc.terminate()
        proc.wait(timeout=timeout)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _tcp_port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) == 0


def _proc_dead_port_unbound(port: int) -> bool:
    return not _tcp_port_in_use("127.0.0.1", port)


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

