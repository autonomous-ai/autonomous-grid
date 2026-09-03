from __future__ import annotations

import json

import pytest

import cli
from remote import credentials
from shared import state


def _remote_home(monkeypatch, tmp_path, networks, *, active=None):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    state.set_mode("remote")
    credentials.save_credentials({
        "session_token": "session",
        "networks": networks,
    })
    if active is not None:
        state.set_active("remote", active)


def test_relay_info_from_active_grid_lists_every_grid_using_it(monkeypatch, tmp_path, capsys):
    _remote_home(monkeypatch, tmp_path, [
        {"network_id": "g-2", "name": "Support", "relay_id": "relay-c",
         "signaling_url": "https://relay.example/"},
        {"network_id": "g-1", "name": "Forge", "relay_id": "relay-c",
         "lan_signaling_url": "https://relay.example"},
        {"network_id": "g-3", "name": "Research", "relay_id": "relay-d",
         "signaling_url": "https://other.example"},
    ], active="Forge")

    assert cli.main(["relay", "info"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "relay_id=relay-c",
        "relay_url=https://relay.example",
        "grids=2",
        "  Forge\tg-1",
        "  Support\tg-2",
    ]


def test_relay_info_json_has_relay_and_grid_ownership(monkeypatch, tmp_path, capsys):
    _remote_home(monkeypatch, tmp_path, [
        {"network_id": "g-1", "name": "Forge", "relay_id": "relay-c",
         "signaling_url": "https://relay.example"},
        {"network_id": "g-2", "name": "Support", "relay_id": "relay-c",
         "signaling_url": "https://relay.example"},
    ])

    assert cli.main(["relay", "info", "relay-c", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "relay_id": "relay-c",
        "relay_url": "https://relay.example",
        "grids": [
            {"grid": "Forge", "id": "g-1"},
            {"grid": "Support", "id": "g-2"},
        ],
    }


@pytest.mark.parametrize("selector", ["Forge", "g-1", "https://relay.example/"])
def test_relay_info_accepts_grid_or_url_selector(
    monkeypatch, tmp_path, capsys, selector
):
    _remote_home(monkeypatch, tmp_path, [
        {"network_id": "g-1", "name": "Forge",
         "lan_signaling_url": "https://relay.example"},
    ])

    assert cli.main(["relay", "info", selector]) == 0
    out = capsys.readouterr().out
    assert "relay_url=https://relay.example" in out
    assert "  Forge\tg-1" in out


def test_relay_info_bridges_old_url_only_grid_during_relay_id_rollout(
    monkeypatch, tmp_path, capsys
):
    _remote_home(monkeypatch, tmp_path, [
        {"network_id": "new", "name": "New", "relay_id": "relay-c",
         "signaling_url": "https://relay.example"},
        {"network_id": "old", "name": "Old",
         "lan_signaling_url": "https://relay.example/"},
    ])

    assert cli.main(["relay", "info", "relay-c", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert [grid["id"] for grid in result["grids"]] == ["new", "old"]


def test_active_old_record_adopts_id_and_transitively_follows_relay_move(
    monkeypatch, tmp_path, capsys
):
    _remote_home(monkeypatch, tmp_path, [
        {"network_id": "old", "name": "Old", "signaling_url": "https://old.example"},
        {"network_id": "bridge", "name": "Bridge", "relay_id": "relay-c",
         "signaling_url": "https://old.example"},
        {"network_id": "moved", "name": "Moved", "relay_id": "relay-c",
         "signaling_url": "https://new.example"},
    ], active="Old")

    assert cli.main(["relay", "info", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["relay_id"] == "relay-c"
    assert {grid["id"] for grid in result["grids"]} == {"old", "bridge", "moved"}


def test_relay_info_without_selection_refuses_multiple_relays(monkeypatch, tmp_path):
    _remote_home(monkeypatch, tmp_path, [
        {"network_id": "g-1", "name": "One", "signaling_url": "https://one.example"},
        {"network_id": "g-2", "name": "Two", "signaling_url": "https://two.example"},
    ])

    with pytest.raises(SystemExit) as exc:
        cli.main(["relay", "info"])
    assert "More than one relay" in str(exc.value)


def test_relay_info_does_not_print_grid_credentials(monkeypatch, tmp_path, capsys):
    _remote_home(monkeypatch, tmp_path, [
        {"network_id": "g-1", "name": "Forge", "signaling_url": "https://relay.example",
         "access_token": "ACCESS-SECRET", "refresh_token": "REFRESH-SECRET"},
    ])

    assert cli.main(["relay", "info"]) == 0
    output = capsys.readouterr().out
    assert "ACCESS-SECRET" not in output
    assert "REFRESH-SECRET" not in output


def test_relay_info_requires_remote_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))

    with pytest.raises(SystemExit) as exc:
        cli.main(["relay", "info"])
    assert str(exc.value) == (
        "`grid relay` is a remote-mode command. "
        "Run `grid mode remote` (or pass --remote) to sign in."
    )
