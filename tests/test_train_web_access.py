"""Who can open the training interface.

Loopback is "whoever is using this computer", which is correct for a local tool. The moment someone
shares it with `--host 0.0.0.0`, the pages are on the office network showing real tickets and able
to start jobs — so a shared link needs a code in it. These tests pin both halves, and the fact that
the code never stays in the address bar.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from train.web import access, build_app


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    monkeypatch.delenv("GRID_TRAIN_WEB_TOKEN", raising=False)


def test_a_local_run_needs_no_code():
    assert access.resolve_token("127.0.0.1") == ""
    assert access.resolve_token("localhost") == ""
    client = TestClient(build_app(""), follow_redirects=False)
    assert client.get("/").status_code == 200


def test_sharing_it_on_the_network_requires_the_code():
    token = access.resolve_token("0.0.0.0")
    assert len(token) >= 20
    client = TestClient(build_app(token), follow_redirects=False)

    denied = client.get("/")
    assert denied.status_code == 403
    assert "needs the code" in denied.text
    assert token not in denied.text                     # the page never leaks it

    wrong = client.get("/", params={"token": "x" * len(token)})
    assert wrong.status_code == 403


def test_the_code_is_swapped_for_a_cookie_and_leaves_the_address_bar():
    """A URL with a secret in it ends up in screenshots, chat logs and browser history."""
    token = access.resolve_token("0.0.0.0")
    client = TestClient(build_app(token), follow_redirects=False)

    first = client.get("/", params={"token": token})
    assert first.status_code == 303
    assert "token" not in first.headers["location"]
    assert client.cookies.get(access.COOKIE) == token

    assert client.get("/").status_code == 200           # the cookie carries it from here


def test_a_pinned_code_survives_a_restart(monkeypatch):
    monkeypatch.setenv("GRID_TRAIN_WEB_TOKEN", "the-office-code")
    assert access.resolve_token("0.0.0.0") == "the-office-code"
    assert access.resolve_token("127.0.0.1") == ""      # still not needed locally


def test_the_startup_lines_never_print_a_bare_shared_address():
    lines = access.share_lines("0.0.0.0", 8322, "abc123")
    assert any("?token=abc123" in line for line in lines)
    assert not any(line.rstrip().endswith(":8322") for line in lines)
    assert any("Anyone with it" in line for line in lines)

    local = access.share_lines("127.0.0.1", 8322, "")
    assert any("Only this computer" in line for line in local)


def test_the_health_check_stays_open():
    """Something has to be able to ask "is it up" without the secret."""
    token = access.resolve_token("0.0.0.0")
    client = TestClient(build_app(token), follow_redirects=False)
    response = client.get("/healthz")
    assert response.status_code in (200, 404)           # open either way: never a 403
