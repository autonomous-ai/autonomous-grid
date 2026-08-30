"""Unit checks for the no-SSH physical Goal lab bootstrap.

The live acceptance remains physical and cross-repository.  These checks protect the part most
likely to turn a real run into a misleading one: two machines accidentally sharing one node id,
an expired/copied credential, or a config that silently resolves back to the hosted Grid.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import stat
import time
from pathlib import Path

import pytest


_SOURCE = Path(__file__).parent / "e2e_cross_repo" / "physical_goal_lab.py"
_SPEC = importlib.util.spec_from_file_location("physical_goal_lab", _SOURCE)
assert _SPEC and _SPEC.loader
lab = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lab)


def _pair(*, node="goal-a", expires=None):
    expiry = expires if expires is not None else int(time.time()) + 3600
    token = lab.issue_token(
        "secret-padded-past-thirty-two-bytes", user_id="one-owner",
        node_id=node, expires_at=expiry)
    return lab.decode_pair(lab.encode_pair(
        url="http://192.0.2.44:8090", token=token, node_id=node, expires_at=expiry))


def test_pairing_round_trip_pins_relay_node_owner_and_expiry():
    pair = _pair()
    claims = lab.token_claims(pair["access_token"])

    assert pair["relay_url"] == "http://192.0.2.44:8090"
    assert pair["network_id"] == lab.NETWORK_ID
    assert pair["node_id"] == claims["node_id"] == "goal-a"
    assert claims["user_id"] == "one-owner"
    assert set(lab.SCOPES).issubset(claims["scopes"])


def test_pairing_refuses_expiry_identity_mismatch_and_oversize():
    expired = int(time.time()) - 1
    token = lab.issue_token(
        "secret-padded-past-thirty-two-bytes", user_id="owner",
        node_id="goal-a", expires_at=expired)
    bundle = lab.encode_pair(
        url="http://192.0.2.44:8090", token=token, node_id="goal-a", expires_at=expired)
    with pytest.raises(ValueError, match="expired"):
        lab.decode_pair(bundle)

    live = int(time.time()) + 3600
    token = lab.issue_token(
        "secret-padded-past-thirty-two-bytes", user_id="owner",
        node_id="goal-a", expires_at=live)
    mismatched = lab.encode_pair(
        url="http://192.0.2.44:8090", token=token, node_id="goal-b", expires_at=live)
    with pytest.raises(ValueError, match="does not match"):
        lab.decode_pair(mismatched)
    with pytest.raises(ValueError, match="unexpectedly large"):
        lab.decode_pair("x" * 16_385)


@pytest.mark.parametrize("url", [
    "file:///tmp/relay", "http://user:secret@example.test:8090", "http://host/x",
    "http://host:99999", "http://host:8090?token=x",
])
def test_pairing_refuses_non_root_or_credential_bearing_relay_urls(url):
    with pytest.raises(ValueError):
        lab.validate_relay_url(url)


def test_configure_home_writes_an_isolated_remote_grid(tmp_path, monkeypatch):
    home = tmp_path / "machine-a"
    original = os.environ.get("GRID_HOME")
    lab.configure_home(home, _pair())

    # The helper restores its caller's environment; pairing one test node must not redirect the
    # terminal from which the helper itself was invoked.
    assert os.environ.get("GRID_HOME") == original
    monkeypatch.setenv("GRID_HOME", str(home))
    from remote import credentials
    from shared import state

    saved = credentials.load_credentials()
    assert saved["session_token"] == "physical-goal-lab"
    assert saved["networks"][0]["network_id"] == lab.NETWORK_ID
    assert saved["networks"][0]["signaling_url"] == "http://192.0.2.44:8090"
    assert state.get_mode() == "remote"
    assert state.get_active("remote") == lab.NETWORK_ID
    assert stat.S_IMODE((home / "credentials.toml").stat().st_mode) == 0o600


def test_prepare_relay_mints_distinct_machine_identities_and_private_files(
        tmp_path, monkeypatch):
    relay_repo = tmp_path / "private-relay"
    (relay_repo / "grid_cli" / "private_server").mkdir(parents=True)
    relay_python = relay_repo / ".venv" / "bin" / "python"
    relay_python.parent.mkdir(parents=True)
    relay_python.write_text("", encoding="utf-8")
    root = tmp_path / "physical-state"
    monkeypatch.setattr(lab, "discover_lan_host", lambda: "192.0.2.88")
    args = argparse.Namespace(
        root=str(root), relay_repo=str(relay_repo), reuse=False, advertise_host=None,
        port=8090, token_hours=48, lease_seconds=120, reaper_seconds=5,
        claim_timeout_seconds=30)

    prepared_root, env, raw = lab.prepare_relay(args)
    metadata = lab.json.loads(raw)
    a = lab.decode_pair(metadata["pair_a"])
    monkeypatch.setenv("GRID_HOME", metadata["home_b"])
    from remote import credentials
    b_token = credentials.load_credentials()["networks"][0]["access_token"]
    b_claims = lab.token_claims(b_token)
    a_claims = lab.token_claims(a["access_token"])

    assert prepared_root == root
    assert metadata["url"] == "http://192.0.2.88:8090"
    assert a_claims["user_id"] == b_claims["user_id"]
    assert a_claims["node_id"] != b_claims["node_id"]
    assert env["TASK_REPO_ROOT"] == str(root / "projects")
    assert env["GRID_MODE"] == "false"
    assert stat.S_IMODE((root / "jwt-secret").stat().st_mode) == 0o600
    assert stat.S_IMODE((root / "machine-a-pairing.txt").stat().st_mode) == 0o600
