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
        "`grid relay info` reads remote Grid records. Pass `--remote` or switch modes."
    )


def _pairing_bundle(**updates):
    import base64
    import time

    def enc(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    claims = {
        "relay_id": "relay-self", "relay_url": "https://relay.example",
        "server_id": "server-self", "network_id": "grid-self", "network_name": "Self",
        "network_type": "permissioned", "node_id": "node-self",
        "exp": int(time.time()) + 3600,
    }
    token = ".".join((enc(b'{"alg":"RS256"}'), enc(json.dumps(claims).encode()), enc(b"sig")))
    value = {
        "version": 1, "relay_id": "relay-self", "server_id": "server-self",
        "relay_url": "https://relay.example",
        "network_id": "grid-self", "network_name": "Self", "network_type": "permissioned",
        "access_token": token, "node_id": "node-self", "email": "a@example.com",
        "roles": ["both"], "scopes": ["inference:create"], "expires_at": claims["exp"],
    }
    value.update(updates)
    return enc(json.dumps(value).encode())


def test_connect_saves_self_hosted_grid_and_switches_to_remote(monkeypatch, tmp_path, capsys):
    from cli import remote_relay

    monkeypatch.setenv("GRID_HOME", str(tmp_path))

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"server_id": "server-self"}

    monkeypatch.setattr(remote_relay.httpx, "get", lambda *a, **k: Response())
    assert cli.main(["relay", "connect", "--bundle", _pairing_bundle()]) == 0
    data = credentials.load_credentials()
    assert data["networks"][0]["self_hosted"] is True
    assert data["networks"][0]["relay_id"] == "relay-self"
    assert state.get_mode() == "remote"
    assert state.get_active("remote") == "grid-self"
    assert "Connected to self-hosted Grid Self" in capsys.readouterr().out


def test_host_command_delegates_to_separate_runtime(monkeypatch, tmp_path):
    from cli import remote_relay

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    monkeypatch.setenv("GRID_RELAY_BIN", "/opt/grid/bin/grid-relay")
    seen = {}

    class Result:
        returncode = 7

    def run(argv, check):
        seen["argv"] = argv
        seen["check"] = check
        return Result()

    monkeypatch.setattr(remote_relay.subprocess, "run", run)
    assert cli.main(["relay", "status", "forge", "--json"]) == 7
    assert seen == {
        "argv": ["/opt/grid/bin/grid-relay", "status", "forge", "--json"],
        "check": False,
    }


@pytest.mark.parametrize("argv", [
    pytest.param(["relay", "list", "--json"], id="after-list"),
    pytest.param(["--json", "relay", "list"], id="global"),
    pytest.param(["relay", "status", "--json"], id="status-with-default-selector"),
])
def test_host_json_without_selector_reaches_separate_runtime(monkeypatch, tmp_path, argv):
    from cli import remote_relay

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    monkeypatch.setenv("GRID_RELAY_BIN", "/opt/grid/bin/grid-relay")
    seen = {}

    class Result:
        returncode = 0

    def run(command, check):
        seen["argv"] = command
        seen["check"] = check
        return Result()

    monkeypatch.setattr(remote_relay.subprocess, "run", run)
    assert cli.main(argv) == 0
    command = "status" if "status" in argv else "list"
    assert seen == {
        "argv": ["/opt/grid/bin/grid-relay", command, "--json"],
        "check": False,
    }


def test_disconnect_removes_only_selected_self_hosted_grid(monkeypatch, tmp_path):
    _remote_home(monkeypatch, tmp_path, [
        {"network_id": "hosted", "name": "Hosted", "signaling_url": "https://hosted.example"},
        {"network_id": "self", "name": "Self", "relay_id": "relay-self",
         "signaling_url": "https://self.example", "self_hosted": True},
    ], active="self")
    assert cli.main(["relay", "disconnect", "relay-self"]) == 0
    assert [item["network_id"] for item in credentials.load_credentials()["networks"]] == ["hosted"]
    assert state.get_active("remote") is None


def test_sync_without_hosted_account_keeps_self_hosted_grid(monkeypatch, tmp_path, capsys):
    _remote_home(monkeypatch, tmp_path, [
        {"network_id": "self", "name": "Self", "relay_id": "relay-self",
         "signaling_url": "https://self.example", "self_hosted": True},
    ])
    data = credentials.load_credentials()
    credentials.save_credentials({**data, "session_token": "self-hosted:relay-self"})
    assert cli.main(["sync"]) == 0
    assert [item["network_id"] for item in credentials.load_credentials()["networks"]] == ["self"]
    assert capsys.readouterr().out == "Synced 1 grid(s): Self.\n"


def test_hosted_sync_merges_self_hosted_grid(monkeypatch, tmp_path, capsys):
    from remote import control_plane

    _remote_home(monkeypatch, tmp_path, [
        {"network_id": "old", "name": "Old", "signaling_url": "https://hosted.example"},
        {"network_id": "self", "name": "Self", "relay_id": "relay-self",
         "signaling_url": "https://self.example", "self_hosted": True},
    ])
    monkeypatch.setattr(control_plane, "fetch_tokens", lambda *args: [
        {"network_id": "new", "name": "New", "signaling_url": "https://new.example"},
    ])
    assert cli.main(["sync"]) == 0
    assert {item["network_id"] for item in credentials.load_credentials()["networks"]} == {
        "new", "self",
    }
    assert capsys.readouterr().out == "Synced 2 grid(s): New, Self.\n"
