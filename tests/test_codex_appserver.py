"""The codex app-server client: payload -> QuotaSnapshot, and the never-raise contract.

No real binary here — a scripted stdin/stdout stands in for the process, so the reader thread,
the id demultiplexing and the refusal path are all exercised for real. The one test that needs
the actual `codex` is marked `canary`.
"""
from __future__ import annotations

import io
import json
import queue
import re
import time

import pytest

from shared.agent.cli_seat import QuotaSnapshot
from shared.agent.seats import claude, codex_appserver
from shared.agent.seats.codex_appserver import AppServer, UNSUPPORTED_REQUEST

# The exact result the real binary returns, captured from `account/rateLimits/read`.
REAL_PAYLOAD = {
    "rateLimits": {
        "limitId": "codex",
        "planType": "plus",
        "primary": {"usedPercent": 3, "windowDurationMins": 10080, "resetsAt": 1785913699},
        "secondary": None,
        "rateLimitReachedType": None,
        "spendControlReached": False,
    },
    "rateLimitsByLimitId": {},
    "rateLimitResetCredits": {},
}


class _Stdout:
    """A blocking line source; the reader thread iterates it exactly as it iterates a real pipe."""

    def __init__(self):
        self.lines = queue.Queue()

    def __iter__(self):
        return self

    def __next__(self):
        line = self.lines.get()
        if line is None:
            raise StopIteration
        return line


class FakeProc:
    """A scripted app-server: each request written gets whatever its method maps to."""

    def __init__(self, replies=None, alive=False):
        self.replies = replies or {}
        self.stdout = _Stdout()
        self.stderr = io.StringIO("")
        self.stdin = self
        self.written = []
        self.alive = alive
        self.pid = 424242
        self.stdin_closed = False

    def write(self, line):
        self.written.append(json.loads(line))
        message = json.loads(line)
        reply = self.replies.get(message.get("method"))
        if reply is not None and "id" in message:
            self.push({"id": message["id"], **reply})

    def push(self, message):
        self.stdout.lines.put(json.dumps(message) + "\n")

    def hang_up(self):
        self.stdout.lines.put(None)

    def flush(self):
        pass

    def close(self):
        self.stdin_closed = True

    def poll(self):
        return None if self.alive else 0

    def wait(self, timeout=None):
        self.alive = False
        return 0

    def kill(self):
        self.alive = False


def _server(proc, timeout=2.0):
    return codex_appserver.AppServer(proc, timeout=timeout)


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# payload -> QuotaSnapshot

def test_the_real_payload_maps_to_a_snapshot():
    snapshot = codex_appserver.snapshot_from_rate_limits(REAL_PAYLOAD)
    assert isinstance(snapshot, QuotaSnapshot)
    assert snapshot.session_pct == 3
    assert snapshot.week_pct == 0          # secondary is null on this plan
    assert snapshot.session_reset != ""
    assert snapshot.week_reset == ""


def test_secondary_becomes_the_week_window():
    payload = {"rateLimits": {"primary": {"usedPercent": 12, "resetsAt": 1785913699},
                              "secondary": {"usedPercent": 47, "resetsAt": 1785999999}}}
    snapshot = codex_appserver.snapshot_from_rate_limits(payload)
    assert (snapshot.session_pct, snapshot.week_pct) == (12, 47)
    assert snapshot.week_reset != ""


def test_a_blocked_account_reports_full_windows():
    """Mirrors claude's blocked branch: the percentages can read low while the account is barred."""
    payload = {"rateLimits": {"primary": {"usedPercent": 3}, "secondary": None,
                              "rateLimitReachedType": "rate_limit_reached"}}
    snapshot = codex_appserver.snapshot_from_rate_limits(payload)
    assert (snapshot.session_pct, snapshot.week_pct) == (100, 100)


def test_spend_control_also_counts_as_blocked():
    payload = {"rateLimits": {"primary": {"usedPercent": 1}, "spendControlReached": True}}
    assert codex_appserver.snapshot_from_rate_limits(payload).session_pct == 100


@pytest.mark.parametrize("payload", [
    None, "", [], {}, {"rateLimits": None}, {"rateLimits": {}},
    {"rateLimits": {"primary": None, "secondary": None}},
])
def test_a_payload_with_no_window_reads_as_none(payload):
    assert codex_appserver.snapshot_from_rate_limits(payload) is None


@pytest.mark.parametrize("value,expected", [(None, 0), ("", 0), ("nope", 0), (3, 3),
                                            (3.6, 4), (-5, 0), (250, 100)])
def test_percentages_are_clamped_whole_numbers(value, expected):
    payload = {"rateLimits": {"primary": {"usedPercent": value}}}
    assert codex_appserver.snapshot_from_rate_limits(payload).session_pct == expected


# reset formatting

def test_reset_text_matches_the_claude_seat_exactly():
    """Both seats feed the same quota display, so a drift here shows up as two formats in one UI."""
    window = {"resetsAt": 1785913699}
    assert codex_appserver._reset_text(window) == claude._reset_text(window)
    assert re.fullmatch(r"[a-z]{3} \d{1,2} at \d{1,2}:\d{2}(am|pm)",
                        codex_appserver._reset_text(window))


@pytest.mark.parametrize("stamp", [None, 0, "", "later", -(10 ** 18)])
def test_an_unusable_reset_stamp_reads_as_empty(stamp):
    assert codex_appserver._reset_text({"resetsAt": stamp}) == ""


# the transport

def test_call_returns_the_reply_for_its_own_id():
    proc = FakeProc({"account/rateLimits/read": {"result": REAL_PAYLOAD}})
    server = _server(proc)
    assert server.call("account/rateLimits/read", {}) == REAL_PAYLOAD
    assert proc.written[0]["id"] == 1


def test_read_rate_limits_maps_a_live_reply():
    server = _server(FakeProc({"account/rateLimits/read": {"result": REAL_PAYLOAD}}))
    assert server.read_rate_limits() == QuotaSnapshot(
        session_pct=3, week_pct=0,
        session_reset=codex_appserver._reset_text({"resetsAt": 1785913699}), week_reset="",
    )


def test_read_rate_limits_returns_none_on_a_server_error():
    server = _server(FakeProc({"account/rateLimits/read":
                               {"error": {"code": -32603, "message": "not signed in"}}}))
    assert server.read_rate_limits() is None


def test_read_rate_limits_returns_none_when_the_server_dies():
    proc = FakeProc()  # answers nothing
    server = _server(proc, timeout=5.0)
    proc.hang_up()
    assert server.read_rate_limits() is None


def test_read_rate_limits_returns_none_on_timeout():
    server = _server(FakeProc(), timeout=0.05)
    assert server.read_rate_limits() is None


def test_call_raises_where_the_quota_read_swallows():
    """The never-raise contract belongs to the quota read, not to the transport underneath it."""
    server = _server(FakeProc({"thread/start": {"error": {"code": -1, "message": "nope"}}}))
    with pytest.raises(codex_appserver.AppServerError):
        server.start_thread(cwd="/tmp")


def test_start_thread_returns_the_nested_id():
    proc = FakeProc({"thread/start": {"result": {"thread": {"id": "th_123"}}}})
    server = _server(proc)
    assert server.start_thread(cwd="/tmp", model=None, ephemeral=True) == "th_123"
    assert proc.written[0]["params"] == {"cwd": "/tmp", "ephemeral": True}  # None is dropped


def test_start_turn_sends_the_input_block():
    """No `effort` argument (today's call shape): the params carry no `effort` key at all, not
    even a null one — a request naming neither `thinking` nor `reasoning_effort` must produce
    byte-identical `turn/start` params to before this field existed."""
    proc = FakeProc({"turn/start": {"result": {}}})
    _server(proc).start_turn("th_123", "hello")
    assert proc.written[0]["params"] == {"threadId": "th_123",
                                         "input": [{"type": "text", "text": "hello"}]}


def test_start_turn_carries_effort_when_one_was_resolved():
    """Regression: the codex seat read reasoning token counts back out but never set effort going
    in (grep confirmed no caller passed `effort` to `turn/start`). It rides this per-turn request,
    not config.toml — a session setting would apply to every turn, not just this request."""
    proc = FakeProc({"turn/start": {"result": {}}})
    _server(proc).start_turn("th_123", "hello", effort="high")
    assert proc.written[0]["params"] == {"threadId": "th_123",
                                         "input": [{"type": "text", "text": "hello"}],
                                         "effort": "high"}


def test_start_turn_treats_a_falsy_effort_the_same_as_none():
    proc = FakeProc({"turn/start": {"result": {}}})
    _server(proc).start_turn("th_123", "hello", effort="")
    assert "effort" not in proc.written[0]["params"]


def test_notifications_are_collected_not_mistaken_for_replies():
    proc = FakeProc({"account/rateLimits/read": {"result": REAL_PAYLOAD}})
    server = _server(proc)
    proc.push({"method": "account/rateLimits/updated", "params": {"rateLimits": {}}})
    assert _wait_for(lambda: server.notifications)
    assert server.read_rate_limits() is not None      # the notification did not consume the reply
    assert [n["method"] for n in server.drain_notifications()] == ["account/rateLimits/updated"]
    assert server.drain_notifications() == []


def test_a_server_request_is_refused_rather_than_left_hanging():
    """The server can ask the client for an approval. A headless seat has nobody to ask, and
    silence would hang the turn — so it must answer, not ignore. Known approval methods get a
    proper decline; unknown methods get a generic error so the turn never hangs."""
    proc = FakeProc()
    _server(proc)
    proc.push({"id": 77, "method": "execCommandApproval", "params": {}})
    assert _wait_for(lambda: proc.written)
    assert proc.written[0]["id"] == 77
    # Old method name (execCommandApproval) is handled as a command approval → decline
    assert proc.written[0]["result"]["decision"] == "decline"


def test_junk_lines_do_not_kill_the_reader():
    proc = FakeProc({"account/rateLimits/read": {"result": REAL_PAYLOAD}})
    server = _server(proc)
    proc.stdout.lines.put("not json at all\n")
    proc.stdout.lines.put("[1, 2, 3]\n")
    assert server.read_rate_limits() is not None


# start / stop

def test_stop_kills_the_process_group(monkeypatch):
    """Process group, not process: codex spawns children that a bare terminate would orphan."""
    killed = []
    monkeypatch.setattr(codex_appserver.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    proc = FakeProc(alive=True)
    _server(proc).stop()
    assert killed == [(proc.pid, codex_appserver.signal.SIGTERM)]
    assert proc.stdin_closed


def test_stop_is_a_no_op_when_the_process_already_exited(monkeypatch):
    monkeypatch.setattr(codex_appserver.os, "killpg",
                        lambda pid, sig: pytest.fail("killed an already-dead pid"))
    _server(FakeProc(alive=False)).stop()


def _fake_popen(monkeypatch, proc, seen):
    monkeypatch.setattr(codex_appserver.cli_seat, "seat_bin", lambda spec: "/fake/codex")
    def popen(argv, **kwargs):
        seen.append((argv, kwargs))
        return proc
    monkeypatch.setattr(codex_appserver.subprocess, "Popen", popen)


def test_start_handshakes_before_anything_else(monkeypatch, tmp_path):
    proc = FakeProc({"initialize": {"result": {}}})
    seen = []
    _fake_popen(monkeypatch, proc, seen)

    server = codex_appserver.start_app_server(home=tmp_path)
    argv, kwargs = seen[0]
    assert argv == ["/fake/codex", "app-server", "--stdio"]
    assert kwargs["env"]["CODEX_HOME"] == str(tmp_path)
    assert kwargs["start_new_session"] is True
    assert proc.written[0]["method"] == "initialize"
    assert proc.written[0]["params"]["clientInfo"]["name"] == codex_appserver.CLIENT_NAME
    assert proc.written[1]["method"] == "initialized"
    assert "id" not in proc.written[1]              # a notification, never answered
    server.stop()


def test_a_failed_handshake_does_not_leave_a_process_behind(monkeypatch, tmp_path):
    proc = FakeProc({"initialize": {"error": {"code": -1, "message": "bad client"}}})
    _fake_popen(monkeypatch, proc, [])
    with pytest.raises(codex_appserver.AppServerError):
        codex_appserver.start_app_server(home=tmp_path)
    assert proc.stdin_closed


def test_read_quota_reads_once_and_shuts_down(monkeypatch, tmp_path):
    proc = FakeProc({"initialize": {"result": {}},
                     "account/rateLimits/read": {"result": REAL_PAYLOAD}})
    _fake_popen(monkeypatch, proc, [])
    assert codex_appserver.read_quota(home=tmp_path).session_pct == 3
    assert proc.stdin_closed


def test_read_quota_returns_none_when_the_binary_is_missing(monkeypatch):
    monkeypatch.setattr(codex_appserver.cli_seat, "seat_bin", lambda spec: None)
    assert codex_appserver.read_quota() is None


def test_read_quota_returns_none_when_the_process_will_not_start(monkeypatch, tmp_path):
    monkeypatch.setattr(codex_appserver.cli_seat, "seat_bin", lambda spec: "/fake/codex")
    monkeypatch.setattr(codex_appserver.subprocess, "Popen",
                        lambda argv, **kwargs: (_ for _ in ()).throw(OSError("no such file")))
    assert codex_appserver.read_quota(home=tmp_path) is None


@pytest.mark.canary
def test_the_real_app_server_reports_a_quota():
    """Runs the operator's own signed-in codex. Opt in with `-m canary`."""
    snapshot = codex_appserver.read_quota()
    assert snapshot is not None, "app-server returned no rate limits — is the seat signed in?"
    assert 0 <= snapshot.session_pct <= 100


# --- _respond_to_server: proper responses per method (Phase 3 Task 6) ---

def _mock_server():
    """An AppServer with a mocked _send, bypassing __init__."""
    from unittest.mock import MagicMock
    server = AppServer.__new__(AppServer)
    server._send = MagicMock()
    return server


def test_respond_declines_command_execution_approval():
    server = _mock_server()
    server._respond_to_server(1, "item/commandExecution/requestApproval")
    sent = server._send.call_args[0][0]
    assert sent["id"] == 1
    assert sent["result"]["decision"] == "decline"


def test_respond_denies_all_permissions():
    server = _mock_server()
    server._respond_to_server(2, "item/permissions/requestApproval")
    sent = server._send.call_args[0][0]
    assert sent["result"]["permissions"] == {}


def test_respond_redirects_tool_call():
    server = _mock_server()
    server._respond_to_server(3, "item/tool/call")
    sent = server._send.call_args[0][0]
    assert sent["result"]["success"] is False
    assert len(sent["result"]["contentItems"]) == 1
    assert "not available" in sent["result"]["contentItems"][0]["text"]


def test_respond_generic_error_for_unknown_method():
    server = _mock_server()
    server._respond_to_server(4, "some/unknown/method")
    sent = server._send.call_args[0][0]
    assert "error" in sent
    assert sent["error"]["code"] == UNSUPPORTED_REQUEST


def test_respond_handles_none_params():
    server = _mock_server()
    server._respond_to_server(5, "item/tool/call", params=None)
    sent = server._send.call_args[0][0]
    assert sent["result"]["success"] is False
