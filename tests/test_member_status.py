"""A member never asks for the creator-only live status — it is refused, every time, with a 403.

`resolve_relay_base` asked it before every read and fell back to the address the login bundle stores.
For the grid's creator that status is the authority (the live address, and `asleep` or `stopped`
without a request); for everyone else it is a round trip that can only be refused. The harness's Model
Manager viewer polls a team's grid every few seconds with four `grid` commands, so one member's open
viewer sent the control plane some 24 refusals a minute.

The grid's own token already says which one this account is: the control plane grants the creator
``admin`` and keeps it (grid-apis `store.py`), so a readable token WITHOUT ``admin`` belongs to a member.
A token that cannot be read proves nothing, and the status is asked exactly as before.
"""
from __future__ import annotations

import base64
import json

import pytest

import cli
from cli import remote_grid
from remote import control_plane
from shared import state

_BUNDLE_URL = "https://relay.bundle.example"
_LIVE_URL = "https://relay.live.example"


def _token(roles: list[str]) -> str:
    """A per-grid token carrying ``roles``. Unsigned: the CLI reads claims only to decide its own
    behaviour and never verifies them (`credentials.claims_from_token`)."""
    def part(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part({'network_id': 'n1', 'roles': roles})}.sig"


def _seed(monkeypatch, tmp_path, *, access_token: str | None, bundle_url: str | None = _BUNDLE_URL,
          creator: bool = False) -> list[str]:
    """One grid, `team` (`n1`). Returns the network id of every status request, answered as the control
    plane answers it: the live status for the creator, 403 for anyone else."""
    from remote import credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    state.set_mode("remote")
    net = {"network_id": "n1", "name": "team", "network_type": "os-community", "refresh_token": "RT"}
    if access_token is not None:
        net["access_token"] = access_token
    if bundle_url is not None:
        net["lan_signaling_url"] = bundle_url
    credentials.save_credentials({
        "session_token": "sess-tok", "api_url": "https://api.example",
        "user": {"email": "a@b.com"}, "networks": [net],
    })
    state.set_active("remote", "team")
    asked: list[str] = []

    def status(session, network_id, api_url=None):
        asked.append(network_id)
        if not creator:
            raise control_plane.ControlPlaneError(
                f"GET /v1/grid/managed-networks/{network_id}/status failed (403): not the creator",
                status=403,
            )
        return {"state": "running", "signaling_url": _LIVE_URL}

    monkeypatch.setattr(control_plane, "get_managed_network_status", status)
    return asked


def _resolve() -> tuple[str, dict]:
    from remote import credentials

    rec = credentials.load_credentials()["networks"][0]
    return remote_grid.resolve_relay_base("sess-tok", rec, "n1", "team")


def test_a_member_takes_the_address_its_login_stored_without_asking_the_status(monkeypatch, tmp_path):
    asked = _seed(monkeypatch, tmp_path, access_token=_token(["both"]))

    assert _resolve() == (_BUNDLE_URL, {})
    assert asked == []


def test_the_creator_still_reads_its_live_status(monkeypatch, tmp_path):
    asked = _seed(monkeypatch, tmp_path, access_token=_token(["admin", "both"]), creator=True)

    base, status = _resolve()

    assert asked == ["n1"]
    assert base == _LIVE_URL and status["state"] == "running"


@pytest.mark.parametrize("access_token", [None, "AT", "a.b"], ids=["absent", "opaque", "malformed"])
def test_a_token_that_says_nothing_still_asks_and_falls_back_as_before(monkeypatch, tmp_path, access_token):
    asked = _seed(monkeypatch, tmp_path, access_token=access_token)

    assert _resolve() == (_BUNDLE_URL, {})
    assert asked == ["n1"]


def test_an_admin_who_did_not_create_the_grid_is_refused_and_falls_back_as_before(monkeypatch, tmp_path):
    """``admin`` can be granted to a member, so it does not prove "creator" — only its absence proves
    "not creator". Such an admin pays today's one refusal, never a wrong answer."""
    asked = _seed(monkeypatch, tmp_path, access_token=_token(["admin"]))

    assert _resolve() == (_BUNDLE_URL, {})
    assert asked == ["n1"]


def test_a_member_with_no_stored_address_still_asks_and_hears_the_refusal(monkeypatch, tmp_path):
    asked = _seed(monkeypatch, tmp_path, access_token=_token(["both"]), bundle_url=None)

    with pytest.raises(SystemExit, match="403"):
        _resolve()
    assert asked == ["n1"]


def test_grid_info_asks_no_status_for_a_member(monkeypatch, tmp_path, capsys):
    asked = _seed(monkeypatch, tmp_path, access_token=_token(["both"]))

    assert cli.main(["info", "--json"]) == 0

    view = json.loads(capsys.readouterr().out)
    assert view["status"] is None and view["grid_url"] == _BUNDLE_URL
    assert asked == []


def test_grid_info_still_reads_the_creators_status(monkeypatch, tmp_path, capsys):
    asked = _seed(monkeypatch, tmp_path, access_token=_token(["admin", "both"]), creator=True)

    assert cli.main(["info", "--json"]) == 0

    view = json.loads(capsys.readouterr().out)
    assert view["status"] == "running" and view["grid_url"] == _LIVE_URL
    assert asked == ["n1"]
