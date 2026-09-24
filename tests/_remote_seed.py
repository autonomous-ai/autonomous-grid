"""A signed-in remote user with one grid, for the no-wake suites (grid-reads-without-waking issue 01):
`tests/test_no_wake.py` and `tests/test_grid_reads_lockstep.py`."""
from __future__ import annotations

from shared import state


def seed_remote_grid(monkeypatch, tmp_path, *, state_word="running", access_token="AT"):
    """One grid, `team` (network id `n1`), whose owner status says ``state_word``."""
    from remote import control_plane, credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    state.set_mode("remote")
    net = {"network_id": "n1", "name": "team", "network_type": "permissioned-public"}
    if access_token is not None:
        net["access_token"], net["refresh_token"] = access_token, "RT"
    credentials.save_credentials({
        "session_token": "sess-tok", "api_url": "https://api.example",
        "user": {"email": "a@b.com"}, "networks": [net],
    })
    state.set_active("remote", "team")
    monkeypatch.setattr(
        control_plane, "get_managed_network_status",
        lambda session, network_id, api_url=None: {
            "state": state_word, "signaling_url": "https://relay.example",
        },
    )
