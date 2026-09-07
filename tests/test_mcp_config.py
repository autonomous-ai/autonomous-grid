"""`grid mcp config` / `grid mcp token` — the harness-facing half of ADR 0041.

The command reads only local state and prints. What is worth pinning is therefore not "does it run"
but the three things that are wrong in a way nobody would notice:

- **the URL keeps its trailing slash.** Without it the control-plane mount answers 307 (measured).
- **the base is the signed-in control plane**, not a hardcoded production host — a home logged in
  against dev must print a dev URL, or the config silently points at the wrong fleet.
- **Codex gets a config BLOCK and not a command.** Measured on codex-cli 0.144.6: `codex mcp add`
  has no `--header` flag, so a printed `codex mcp add … --header …` would be an invalid-argument
  error in the user's terminal. This is the assertion that would catch somebody "tidying" the two
  harnesses into one shape.
"""

from __future__ import annotations

import argparse
import json

import pytest

from cli import mcp_config

API_URL = "https://api-grid.example.invalid"
TOKEN = "eyJ-a-per-grid-access-token"


@pytest.fixture
def signed_in(monkeypatch, tmp_path):
    """One grid in the local store, signed in against a non-default control plane."""
    from remote import credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    credentials.save_credentials(
        {
            "session_token": "session",
            "api_url": API_URL,
            "networks": [
                {
                    "network_id": "ag-0000000000000001",
                    "name": "team",
                    "access_token": TOKEN,
                    "lan_signaling_url": "https://relay.example.invalid",
                }
            ],
        }
    )
    return argparse.Namespace(grid=None, json=False)


def test_token_prints_the_access_token_and_nothing_else(signed_in, capsys):
    assert mcp_config.cmd_mcp_token(signed_in) == 0
    assert capsys.readouterr().out.strip() == TOKEN


def test_config_url_is_the_signed_in_control_plane_with_a_trailing_slash(signed_in, capsys):
    assert mcp_config.cmd_mcp_config(signed_in) == 0
    out = capsys.readouterr().out
    expected = f"{API_URL}{mcp_config.MOUNT_PATH}/"
    assert expected in out
    # The default production host must not appear: a home signed in against dev prints dev.
    assert "api-grid.autonomous.ai" not in out


def test_config_covers_all_three_harnesses_with_a_literal_header(signed_in, capsys):
    assert mcp_config.cmd_mcp_config(signed_in) == 0
    out = capsys.readouterr().out

    assert "claude mcp add --transport http" in out
    assert f"--header 'Authorization: Bearer {TOKEN}'" in out

    # ⚠️ Codex takes a BLOCK, never a command — it has no --header flag (measured, 0.144.6).
    assert "http_headers = { Authorization = " in out
    assert "codex mcp add" not in out

    assert '"type": "remote"' in out  # opencode
    assert out.count(TOKEN) == 3  # one credential, three spellings


def test_config_says_the_output_carries_a_credential(signed_in, capsys):
    """The command prints a 365-day token to a terminal; somebody pasting it into an issue should
    have been told what it is."""
    mcp_config.cmd_mcp_config(signed_in)
    assert "access token" in capsys.readouterr().out


def test_config_json_is_a_machine_readable_triple(signed_in, capsys):
    signed_in.json = True
    assert mcp_config.cmd_mcp_config(signed_in) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "server": mcp_config.SERVER_NAME,
        "url": f"{API_URL}{mcp_config.MOUNT_PATH}/",
        "authorization": f"Bearer {TOKEN}",
    }


def test_a_grid_with_no_token_says_to_sign_in(monkeypatch, tmp_path):
    """The familiar sentence, not an empty bearer pasted into a config that then 401s forever."""
    from remote import credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    credentials.save_credentials(
        {
            "session_token": "session",
            "api_url": API_URL,
            "networks": [{"network_id": "ag-0000000000000002", "name": "tokenless"}],
        }
    )
    with pytest.raises(SystemExit) as refused:
        mcp_config.cmd_mcp_config(argparse.Namespace(grid=None, json=False))
    assert "grid login" in str(refused.value)


def test_an_unknown_grid_name_is_refused(signed_in):
    signed_in.grid = "no-such-grid"
    with pytest.raises(SystemExit) as refused:
        mcp_config.cmd_mcp_token(signed_in)
    assert "no-such-grid" in str(refused.value)
