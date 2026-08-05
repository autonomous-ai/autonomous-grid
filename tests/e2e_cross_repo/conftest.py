"""The harness: a real relay from grid-src, real provider processes from this repo, a fake agent.

Nothing in this directory is collected by an ordinary `pytest tests/` run — the modules are named
`e2e_*.py` rather than `test_*.py`, the same convention `tests/e2e_doggi.py` and `tests/e2e_train.py`
already use. Pass a path explicitly to run them. See `README.md`.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _harness as H  # noqa: E402

sys.path.insert(0, str(H.GRID_REPO))

_HERE = Path(__file__).resolve().parent


@pytest.fixture(scope="session")
def relay(tmp_path_factory):
    """grid-src's `server:app` under a real `uvicorn`, in a subprocess, run by ITS OWN interpreter.

    A subprocess and not a thread, and grid-src's interpreter and not this one: the two repositories
    are separate installs in the field, and a single process importing both would be a topology that
    exists nowhere. It also means the relay's own reaper is really ticking, which is what a reclaim
    needs — a test that poked the row would prove the reclaim function works, not that anything calls
    it.
    """
    import httpx

    H.require_relay_repo()
    root = tmp_path_factory.mktemp("relay")
    port = H.free_port()
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{root / 'e2e.db'}",
        "KEYS_PATH": str(root / "keys"),
        "JWT_SECRET": H.SECRET,
        "PLATFORM_FEE_PERCENT": "10.0",
        "TASK_REPO_ROOT": str(root / "projects"),
        "TASK_LEASE_SECONDS": str(H.LEASE_SECONDS),
        "TASK_REAPER_INTERVAL_SECONDS": str(H.REAPER_SECONDS),
        "TASK_CLAIM_TIMEOUT_SECONDS": "3",
        "GRID_MODE": "false",
        "PYTHONPATH": str(H.RELAY_SERVER_DIR),
    }
    proc = subprocess.Popen(
        [str(H.RELAY_PYTHON), "-m", "uvicorn", "server:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(H.RELAY_SERVER_DIR), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + H.BOOT_TIMEOUT_SECONDS
    while True:
        if proc.poll() is not None:
            pytest.fail(f"the relay exited before serving:\n{proc.communicate()[0]}")
        try:
            if httpx.get(f"{base}/relay/v1/health", timeout=2.0).status_code < 500:
                break
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:
            proc.kill()
            pytest.fail(f"the relay did not come up within {H.BOOT_TIMEOUT_SECONDS:.0f}s")
        time.sleep(0.2)
    try:
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="session")
def owner_token():
    return H.token("alice", "client-node")


@pytest.fixture(scope="session")
def provider_nodes(relay, owner_token):
    """Two REGISTERED provider nodes, each with a token carrying its own id.

    Registration is not decoration: `_require_provider` gates the claim on the node registry's role,
    so an unregistered member cannot claim at all. Two real identities rather than one used twice,
    because the push fence is keyed on `provider_id` — one identity could not tell "the lease moved"
    from "the lease is still mine".
    """
    import httpx

    nodes = {}
    with httpx.Client(base_url=relay, timeout=30.0) as client:
        for label in ("A", "B"):
            created = client.post(
                "/nodes", json={"role": "both"},
                headers={"Authorization": f"Bearer {owner_token}"})
            assert created.status_code == 200, created.text
            nodes[label] = (created.json()["node_id"], H.token(f"provider-{label}", created.json()["node_id"]))
    return nodes


@pytest.fixture(scope="session")
def fake_agent_bin(tmp_path_factory):
    """`fake_claude.py` installed as a `claude` on PATH — which is how `claude_install.resolve()`
    finds it, PATH first."""
    bindir = tmp_path_factory.mktemp("bin")
    target = bindir / "claude"
    target.write_text(
        f"#!{sys.executable}\n" + (_HERE / "fake_claude.py").read_text(encoding="utf-8"),
        encoding="utf-8")
    target.chmod(0o755)
    return bindir


@pytest.fixture(scope="session")
def workspace_root(tmp_path_factory):
    return Path(str(tmp_path_factory.mktemp("provider-workspaces")))


@pytest.fixture
def spawn_provider(relay, provider_nodes, fake_agent_bin, workspace_root, tmp_path_factory):
    """A provider process running this repo's real `task_loop` against the fake agent."""
    started: list[H.Provider] = []

    def _spawn(label="A"):
        node_id, node_token = provider_nodes[label]
        env = {
            **os.environ,
            "PATH": f"{fake_agent_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "GRID_REPO": str(H.GRID_REPO),
            "GRID_SIGNALING_URL": relay,
            "GRID_NODE_ID": node_id,
            "GRID_TOKEN": node_token,
            "GRID_RENEW_SECONDS": str(H.RENEW_SECONDS),
            "GRID_TASK_ROOT": str(workspace_root / label),
            # NEVER the developer's own `~/.claude`: `link_transcript` plants a symlink under it.
            # `e2e_live_agent.py` is the one place that cannot do this, and it cleans up after itself.
            "GRID_TASK_CLAUDE_CONFIG_DIR": str(tmp_path_factory.mktemp(f"claude-config-{label}")),
            "GRID_TASK_TIMEOUT_SECONDS": "120",
        }
        proc = subprocess.Popen(
            [sys.executable, str(_HERE / "provider_process.py")],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        provider = H.Provider(proc, node_id)
        started.append(provider)
        return provider

    yield _spawn
    for provider in started:
        provider.stop()
