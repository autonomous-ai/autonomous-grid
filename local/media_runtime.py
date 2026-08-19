from __future__ import annotations

import os
import socket
import subprocess
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


def wait_for_child_server(
    proc: subprocess.Popen, port: int, log_path: Path, timeout: float = 30.0,
    *, label: str = "Provider media server",
) -> None:
    """Block until a loopback child answers ``/health``, or explain why it never will.

    Engine-agnostic on purpose: the media bridge and the CLI seats have the same contract (a child
    on loopback that must be healthy before anything advertises it), and a second copy of this loop
    is a second place for the exit-code check to be forgotten. ``label`` only names the child in the
    two failure messages.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SystemExit(
                f"{label} exited (code {proc.returncode}) before becoming healthy. See {log_path}"
            )
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0).status_code == 200:
                return
        except httpx.RequestError:
            pass
        time.sleep(0.25)
    raise SystemExit(f"{label} did not become healthy. See {log_path}")


# The pre-existing name, kept so the media call sites read unchanged.
wait_for_media_server = wait_for_child_server


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


# Re-exported, not re-implemented. `local/runtime.py` owns the one copy: this used to be a second,
# byte-identical body, and a fix landing in only one of them is how the media bridge and the CLI
# seats would start disagreeing about how to re-invoke this CLI — the same duplication this module's
# `stop_media_server`/`wait_for_child_server` are shared to avoid.
def _cli_subprocess_command() -> list[str]:
    from local.runtime import _cli_subprocess_command as resolve  # lazy: keeps the import one-way

    return resolve()

