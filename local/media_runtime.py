from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

from shared import logging_setup, paths


def start_media_server(*, port: int, comfyui_url: str) -> subprocess.Popen:
    if _tcp_port_in_use("127.0.0.1", port):
        raise SystemExit(f"Port {port} is already in use; cannot start provider media server.")
    paths.ensure_all()
    log_path = paths.logs_dir() / f"media_provider_{port}.log"
    log = logging_setup.cap_and_open_append(log_path, logging_setup.engine_log_max_bytes())
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
    wait_for_media_server(proc, port, log_path)
    return proc


def start_api_media_server(*, port: int, api_kind: str, base_url: str, api_key: str) -> subprocess.Popen:
    """Start the vendor-gateway media bridge (`local/api_media_server.py`) on loopback.

    Same contract as ``start_media_server`` — a healthy `/health` before returning — so the caller
    treats a ComfyUI media engine and an API media engine identically. The key is passed through the
    child's ENVIRONMENT, never argv: argv is world-readable via `ps`.
    """
    if _tcp_port_in_use("127.0.0.1", port):
        raise SystemExit(f"Port {port} is already in use; cannot start the {api_kind} media bridge.")
    paths.ensure_all()
    log_path = paths.logs_dir() / f"media_api_{api_kind}_{port}.log"
    log = logging_setup.cap_and_open_append(log_path, logging_setup.engine_log_max_bytes())
    proc = subprocess.Popen(
        _cli_subprocess_command() + [
            "__api-media-server",
            "--port", str(port),
            "--api-kind", api_kind,
            "--base-url", base_url.rstrip("/"),
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env={**os.environ, "GRID_API_MEDIA_KEY": api_key, "PYTHONUNBUFFERED": "1"},
    )
    wait_for_media_server(proc, port, log_path)
    return proc


def wait_for_media_server(proc: subprocess.Popen, port: int, log_path: Path, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SystemExit(
                f"Provider media server exited (code {proc.returncode}) before becoming healthy. See {log_path}"
            )
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

