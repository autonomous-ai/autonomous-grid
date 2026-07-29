"""Start/stop a CLI seat's loopback server — the text twin of `start_api_media_server`.

The health wait, teardown and executable resolution are reused from `media_runtime`: a second copy
of `killpg` is how a stopped engine leaves an orphan holding the port.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from shared import logging_setup, paths
from shared.agent import cli_seat

from .media_runtime import (
    _cli_subprocess_command,
    _tcp_port_in_use,
    stop_media_server as stop_seat_server,
    wait_for_child_server,
)

__all__ = ["start_seat_server", "stop_seat_server", "transcript_path"]


def transcript_path(kind: str, port: int) -> Path:
    """The JSONL turn log, one file per (kind, port). Appended across restarts."""
    return paths.logs_dir() / f"cli_seat_{kind}_{port}.jsonl"


def start_seat_server(*, kind: str, options, binary: str) -> subprocess.Popen:
    """Start the seat server and return once it answers /health. `binary` is passed down, not
    re-resolved: the caller ran the sign-in check seconds ago."""
    if options.transcript:
        print(
            f"Warning: --seat-transcript is ON. Every message consumers send will be written "
            f"verbatim to {transcript_path(kind, options.port)} on this machine.",
            file=sys.stderr,
        )
    if _tcp_port_in_use("127.0.0.1", options.port):
        raise SystemExit(f"Port {options.port} is already in use; cannot start the {kind} seat.")
    paths.ensure_all()
    log_path = paths.logs_dir() / f"cli_seat_{kind}_{options.port}.log"
    log = logging_setup.cap_and_open_append(log_path, logging_setup.engine_log_max_bytes())
    proc = subprocess.Popen(
        _cli_subprocess_command()
        + ["__cli-seat-server", "--binary", binary,
           "--transcript-path", str(transcript_path(kind, options.port))]
        + cli_seat.options_child_argv(options, kind),
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    wait_for_child_server(proc, options.port, log_path, label=f"{kind} seat")
    return proc
