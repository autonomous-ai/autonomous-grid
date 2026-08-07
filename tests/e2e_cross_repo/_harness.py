"""Constants and helpers shared by the two cross-repo E2E modules.

Separate from `conftest.py` so the test modules can import them without importing the conftest —
pytest owns that file's module identity, and reaching into it from a test is a way to end up with
two copies of one module.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

import pytest

# This repository, derived rather than written down: `tests/e2e_cross_repo/_harness.py`.
GRID_REPO = Path(__file__).resolve().parents[2]

# grid-src's matching worktree. Hardcoded with an env override and a skip, exactly as
# `tests/test_task_lease.py` does for the lockstep constants it parses: the two repositories share no
# code and are not installed together, so there is no import path to discover this by.
RELAY_REPO = Path(os.environ.get(
    "GRID_SRC_REPO", "/Users/macbookpro/Projects/grid-src-feats/distributed-tasks"))
RELAY_SERVER_DIR = RELAY_REPO / "grid_cli" / "private_server"
RELAY_PYTHON = RELAY_REPO / ".venv" / "bin" / "python"

SECRET = "cross-repo-e2e-secret-padded-past-32-bytes"
BOOT_TIMEOUT_SECONDS = 90.0
# Seconds rather than the production 120s/30s, and the RATIO is what is kept (ADR 0032 D-c: a TTL
# several beats wider than the renewal interval). 6s/0.5s is the same 4x with the same slack, so what
# is under test is the mechanism and not the operator's patience.
LEASE_SECONDS = 6
REAPER_SECONDS = 1
RENEW_SECONDS = 0.5
SCOPES = [
    "inference:create", "inference:models", "inference:resume",
    "provider:heartbeat", "provider:update", "provider:poll", "provider:submit", "provider:error",
]


def require_relay_repo() -> None:
    """Skip rather than fail when grid-src is not beside this worktree — same rule as the lockstep
    tests, because a machine without the other repository cannot check a cross-repo property."""
    if not RELAY_SERVER_DIR.is_dir():
        pytest.skip(f"grid-src worktree not found at {RELAY_REPO}; set GRID_SRC_REPO")
    if not RELAY_PYTHON.exists():
        pytest.skip(f"grid-src has no virtualenv at {RELAY_PYTHON}; the relay needs its own install")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def token(user_id: str, node_id: str = "node-1") -> str:
    """A real HS256 token, minted by hand.

    PyJWT is not a dependency of this repository and adding one for a test would be the wrong trade:
    a provider never mints a token, it is handed one. Twenty lines of `hmac` keep the two repos'
    dependency lists as different here as they are in the field — and a real token is the honest
    test anyway, since nothing in this process can reach into the relay's to patch a verifier.
    """
    now = int(time.time())
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64(json.dumps({
        "user_id": user_id, "node_id": node_id, "email": f"{user_id}@invalid", "role": "both",
        "scopes": SCOPES, "iat": now, "exp": now + 3600}, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode()
    return f"{header}.{payload}.{_b64(hmac.new(SECRET.encode(), signing_input, hashlib.sha256).digest())}"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_for(predicate, timeout=25.0, interval=0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def git_ls_remote(url: str, ref: str, *, bearer: str) -> str:
    """`git ls-remote` against the relay's git front, authenticated the way the CLI does it."""
    listed = subprocess.run(
        ["git", "ls-remote", url, ref], capture_output=True, text=True, timeout=60,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "1",
             "GIT_CONFIG_KEY_0": "http.extraHeader",
             "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {bearer}"})
    assert listed.returncode == 0, listed.stderr
    return listed.stdout


class Provider:
    """One provider process, and the two different ways it can stop."""

    def __init__(self, proc, node_id):
        self.proc = proc
        self.node_id = node_id

    def die(self):
        """`SIGKILL` — no cleanup, no final report, no goodbye to the relay.

        A provider whose host went away, which is the only thing that is supposed to make a lease
        lapse. `stop()` is a provider tidying up, which is the opposite case.
        """
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
        self.proc.wait(timeout=15)

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# ------------------------------------------------------------------------- the client, for real

def create(relay_base, bearer, prompt, *, project, files=None):
    """One task in the project called `project`, creating that project if it is not there yet.

    Two calls, and the first one is the point rather than setup noise: a task is posted to a project
    **id** since ADR 0033 issue 10, and a name is resolved only by `POST /relay/v1/projects`, under
    the caller's own ownership. This is exactly the sequence `grid task create` runs, so the E2E
    exercises the real client path and every call site above stays as it was.
    """
    from remote import relay as relay_client

    project_id = relay_client.create_project(relay_base, bearer, name=project)["id"]
    return relay_client.create_task(
        relay_base, bearer, prompt=prompt, project_id=project_id, files=files)


def get(relay_base, bearer, task_id):
    from remote import relay as relay_client

    return relay_client.get_task(relay_base, bearer, task_id)


def await_state(relay_base, bearer, task_id, states, timeout=60.0):
    got = wait_for(
        lambda: (lambda t: t if t.get("state") in states else None)(get(relay_base, bearer, task_id)),
        timeout=timeout, interval=0.3)
    if got is None:
        raise AssertionError(
            f"task {task_id} never reached {states}; last seen {get(relay_base, bearer, task_id)!r}")
    return got


def b64_file(content: str) -> str:
    return base64.b64encode(content.encode()).decode()


# --------------------------------------------------------- the operator's own config directory

def sweep_transcript_links(workspace_root) -> None:
    """Remove the transcript symlinks a live run planted in the operator's real `~/.claude`.

    Every module that drives the REAL binary has to write there — issue 01's spike measured a custom
    `CLAUDE_CONFIG_DIR` yielding `Not logged in` even on macOS, where the token is in the Keychain —
    so every one of them has to clean up, and they must clean up the same way.

    By TARGET, never by deriving the expected link names. Deriving them quietly half-works: a test
    that deletes its workspace on purpose leaves nothing to derive a name from, so that run's link
    survives. Reading targets also needs no opinion about how the vendor encodes a path into a
    directory name — and being wrong about that encoding is the bug this whole E2E layer exists to
    catch, so a cleanup that depended on it would fail in exactly the case worth knowing about.

    Safe because of the target test: everything under a `tmp_path_factory` root belongs to the run
    that asked, so nothing of the operator's own can match.
    """
    from remote import task_agent

    projects = task_agent.claude_config_dir() / "projects"
    if not projects.is_dir():
        return
    ours = {str(workspace_root), os.path.realpath(workspace_root)}
    for entry in projects.iterdir():
        # Only ever a symlink; a real directory there is somebody's data.
        if not entry.is_symlink():
            continue
        target = os.path.realpath(os.readlink(entry))
        if any(target.startswith(root + os.sep) for root in ours):
            entry.unlink()
