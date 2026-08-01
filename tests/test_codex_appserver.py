"""Tests for the codex app-server client's server-request handling."""
from unittest.mock import MagicMock

from shared.agent.seats.codex_appserver import AppServer, UNSUPPORTED_REQUEST


def _make_server():
    """An AppServer with a mocked _send, bypassing __init__."""
    server = AppServer.__new__(AppServer)
    server._send = MagicMock()
    return server


def test_respond_declines_command_execution_approval():
    server = _make_server()
    server._respond_to_server(1, "item/commandExecution/requestApproval")
    sent = server._send.call_args[0][0]
    assert sent["id"] == 1
    assert sent["result"]["decision"] == "decline"


def test_respond_denies_all_permissions():
    server = _make_server()
    server._respond_to_server(2, "item/permissions/requestApproval")
    sent = server._send.call_args[0][0]
    assert sent["result"]["permissions"] == {}


def test_respond_redirects_tool_call():
    server = _make_server()
    server._respond_to_server(3, "item/tool/call")
    sent = server._send.call_args[0][0]
    assert sent["result"]["success"] is False
    assert len(sent["result"]["contentItems"]) == 1
    assert "not available" in sent["result"]["contentItems"][0]["text"]


def test_respond_generic_error_for_unknown_method():
    server = _make_server()
    server._respond_to_server(4, "some/unknown/method")
    sent = server._send.call_args[0][0]
    assert "error" in sent
    assert sent["error"]["code"] == UNSUPPORTED_REQUEST


def test_respond_handles_none_params():
    """params=None must not crash — some server requests may omit it."""
    server = _make_server()
    server._respond_to_server(5, "item/tool/call", params=None)
    sent = server._send.call_args[0][0]
    assert sent["result"]["success"] is False
