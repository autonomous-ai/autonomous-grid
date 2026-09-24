"""A provider that meets a SLEEPING grid parks, and is told why (`idle-sleep` issue 02).

The control plane's proxy answers every request that does not wake a sleeping grid with a 503 whose
body carries ``code: grid_asleep`` (grid-apis `grid_proxy.GRID_ASLEEP_CODE`). Before this, a provider
read that 503 as transient: bring-up retried it forever at its 60s cap, and each poll worker re-polled
every two seconds — 6,491 refusals in one hour from one provider, measured on prod — while the proxy
told it "it is waking now" each time.

**Parked, not exited** (decided 2026-09-22, over the issue's first draft): the hosted starter engine is
itself a `grid join` child, and a provider that exited on a sleeping grid would leave that grid with no
engine once somebody woke it — for good, since nothing starts a starter engine twice (ADR 0038 D-f). So
the provider stops asking the relay for work, keeps its ordinary heartbeat as the one probe, says why
once, and serves again by itself when the grid is awake. It never wakes the grid (decision B).
"""
from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from remote import bringup, relay, service_truth

#: The wire value, written out rather than imported: comparing the module's constant to itself would
#: pin nothing. `tests/test_grid_sleep_lockstep.py` compares both repositories to the same literal.
ASLEEP_CODE = "grid_asleep"

#: What grid-apis' proxy answers a request that did not wake a sleeping grid, byte-for-byte in shape.
ASLEEP_BODY = {
    "detail": "grid is asleep and this request does not wake it — a person's request wakes it: "
              "inference, or a signed-in read, action or `grid join`",
    "code": ASLEEP_CODE,
}


@contextmanager
def _relay_answering(status: int, body: dict):
    """A throwaway relay on loopback that answers every request with ``status`` and ``body``."""

    class Handler(BaseHTTPRequestHandler):
        def _reply(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_PUT = _reply

        def log_message(self, *_args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


# --- the relay client reads the code off the proxy's answer ------------------------------------------


@pytest.mark.parametrize("call", ["register", "heartbeat", "poll"])
def test_each_provider_call_reports_a_sleeping_grid(call):
    """The three calls a provider makes, each against the proxy's real answer shape."""
    with _relay_answering(503, ASLEEP_BODY) as url, pytest.raises(relay.RelayError) as caught:
        if call == "register":
            relay.register_node(url, "AT", "node-1", models=["m"])
        elif call == "heartbeat":
            relay.heartbeat(url, "AT", load={})
        else:
            relay.poll(url, "AT", timeout=5.0)

    assert relay.is_grid_asleep(caught.value)
    assert caught.value.status == 503


@pytest.mark.parametrize(
    "body",
    [
        {"detail": "grid is sleeping; it is waking now — retry shortly"},  # today's proxy: no code
        {"detail": "grid is asleep", "code": "grid_sleeping"},  # a reworded code this CLI never heard
        {"detail": {"code": ASLEEP_CODE, "message": "nested"}},  # the task plane's shape, not the proxy's
        {"detail": "asleep", "code": True},  # not a string — compared for equality, never truthiness
    ],
)
def test_anything_but_the_exact_code_is_not_a_sleeping_grid(body):
    """Both skew directions degrade to today's behaviour: an old proxy sends no code and a reworded one
    an unknown code, and either must read as transient — never as asleep."""
    with _relay_answering(503, body) as url, pytest.raises(relay.RelayError) as caught:
        relay.heartbeat(url, "AT", load={})

    assert not relay.is_grid_asleep(caught.value)


# --- bring-up parks instead of spinning --------------------------------------------------------------


class _RecordingStop(threading.Event):
    """A stop event that records each wait and never sleeps (the shape `test_local_cli` uses)."""

    _RUNAWAY = 50

    def __init__(self) -> None:
        super().__init__()
        self.waits: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:  # type: ignore[override]
        self.waits.append(timeout)
        if len(self.waits) > self._RUNAWAY:
            raise AssertionError(f"bring-up waited {len(self.waits)} times without stopping")
        return False


def _asleep() -> relay.RelayError:
    return relay.RelayError(f"register failed (503): {json.dumps(ASLEEP_BODY)}",
                            status=503, code=ASLEEP_CODE)


def _register_failing(errors: list[Exception]):
    """A `register` that raises each of ``errors`` in turn, then lands."""
    remaining = list(errors)

    def register() -> None:
        if remaining:
            raise remaining.pop(0)

    return register


def test_bringup_parks_on_a_sleeping_grid_and_registers_once_it_wakes():
    stop = _RecordingStop()
    noted: list[str | None] = []

    bringup.register_with_backoff(
        _register_failing([_asleep(), _asleep(), _asleep()]),
        stop=stop, log=lambda _m: None, note_error=noted.append,
    )

    assert stop.waits == [30.0, 60.0, 60.0], "a sleeping grid is checked on its own, slower clock"
    assert noted == [service_truth.ASLEEP_REASON] * 3, "one stable sentence, so the record is written once"


def test_the_asleep_sentence_names_what_wakes_the_grid_and_that_the_engine_will_register():
    reason = service_truth.ASLEEP_REASON
    assert "asleep" in reason
    assert "inference request" in reason and "owner" in reason, "it must name what DOES wake it"
    assert "by itself" in reason, "and that nothing is needed from the operator: it rejoins on its own"


def test_bringup_says_the_sentence_once_per_sleep_not_once_per_check():
    lines: list[str] = []

    bringup.register_with_backoff(
        _register_failing([_asleep(), _asleep(), _asleep()]),
        stop=_RecordingStop(), log=lines.append, note_error=lambda _m: None,
    )

    assert sum(service_truth.ASLEEP_REASON in line for line in lines) == 1, lines
    assert all("asleep" in line for line in lines), lines


def test_bringup_does_not_reannounce_a_sleep_across_a_dropped_connection():
    """A codeless failure (nothing answered) says nothing about the grid, so it does not start a NEW
    sleep for the log — the rule the serve loop's park follows too — and its own line names the failure,
    never "still asleep"."""
    lines: list[str] = []
    dropped = relay.RelayError("register transport error: connection reset")

    bringup.register_with_backoff(
        # Two asleep answers first: the drop has to land where a "still asleep" line is otherwise due.
        _register_failing([_asleep(), _asleep(), dropped, _asleep()]),
        stop=_RecordingStop(), log=lines.append, note_error=lambda _m: None,
    )

    assert sum(service_truth.ASLEEP_REASON in line for line in lines) == 1, lines
    assert "connection reset" in lines[2] and "still asleep" not in lines[2], lines
    assert "still asleep" in lines[3], "the sleep it interrupted is the same sleep"


def test_bringup_against_a_proxy_without_the_code_keeps_todays_retry():
    """A new CLI against an old proxy: a codeless 503 is transient, on the ordinary schedule."""
    stop = _RecordingStop()
    old = relay.RelayError("register failed (503): grid is sleeping", status=503)

    bringup.register_with_backoff(
        _register_failing([old, old, old]), stop=stop, log=lambda _m: None, note_error=lambda _m: None,
    )

    assert stop.waits == [1.0, 2.0, 4.0]


def test_bringup_does_not_die_of_a_sleeping_grid():
    """Parked means parked: the code is never terminal, so bring-up can only end by landing or stopping."""
    assert not bringup.is_terminal(_asleep())


# --- a serving engine whose grid is put to sleep under it --------------------------------------------


def _serve_state(monkeypatch, tmp_path, **overrides):
    """The same construction `test_local_cli._serve_state` uses, kept local to this file."""
    from remote import serve
    from shared.system import host

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    monkeypatch.setattr(host, "platform_kind", lambda: "linux")
    monkeypatch.setattr(host, "disk_gb", lambda path: None)
    kwargs = {
        "signaling_url": "https://relay.example", "node_id": "node-1", "network_id": "n1",
        "llm_url": "http://127.0.0.1:8081/v1", "access_token": "AT", "refresh_token": "RT",
        "models": ["m"], "capabilities": {"schema_version": 1, "models": {}},
        "meta": {"name": "e1", "engine": "llama.cpp"}, "pricing": {}, "max_concurrency": 1,
    }
    return serve._ServeState(**{**kwargs, **overrides})


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _run(loop, state) -> threading.Thread:
    thread = threading.Thread(target=loop, args=(state,), daemon=True)
    thread.start()
    return thread


def test_a_poll_worker_does_not_ask_a_grid_it_knows_is_asleep(monkeypatch, tmp_path):
    """Once the grid is known to be asleep, a poll worker makes NO request at all — before, every worker
    re-polled every two seconds for as long as the grid slept — and it resumes when the grid wakes."""
    from remote import serve

    state = _serve_state(monkeypatch, tmp_path)
    polls: list[int] = []

    def poll(url, tok, *, timeout=None):
        polls.append(1)  # and answers None: a 204, no work

    monkeypatch.setattr(relay, "poll", poll)
    state.parked.set()
    worker = _run(serve._poll_loop, state)
    try:
        time.sleep(0.3)
        assert polls == [], "a poll worker asked a grid the engine already knew was asleep"
        state.parked.clear()
        assert _wait_until(lambda: polls), "the worker did not resume once the grid woke"
    finally:
        state.stop.set()
        worker.join(timeout=5)


def test_a_poll_worker_that_meets_the_code_parks_every_worker(monkeypatch, tmp_path):
    from remote import serve

    state = _serve_state(monkeypatch, tmp_path)
    polls: list[int] = []

    def poll(url, tok, *, timeout=None):
        polls.append(1)
        raise relay.RelayError("poll failed (503)", status=503, code=ASLEEP_CODE)

    monkeypatch.setattr(relay, "poll", poll)
    worker = _run(serve._poll_loop, state)
    try:
        assert _wait_until(state.parked.is_set), "the asleep answer did not park the engine"
        time.sleep(2.3)  # past the ordinary 2s poll retry
        assert polls == [1], f"the worker kept polling a sleeping grid: {len(polls)} requests"
    finally:
        state.stop.set()
        worker.join(timeout=5)


def _one_beat(monkeypatch, state, answer):
    """Run exactly one heartbeat tick, answering with ``answer`` (an exception is raised)."""
    from remote import serve

    monkeypatch.setattr(serve, "_maybe_refresh_codex", lambda s: None)
    monkeypatch.setattr(serve, "_maybe_probe_engines", lambda s: None)

    def beat(url, tok, *, load, meta=None):
        state.stop.set()  # one tick per `_heartbeat_loop` call
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(relay, "heartbeat", beat)
    state.stop.clear()
    serve._heartbeat_loop(state)


def test_the_heartbeat_marks_a_sleeping_grid_and_records_why(monkeypatch, tmp_path, capsys):
    from shared import run_records

    state = _serve_state(monkeypatch, tmp_path)
    run_records.write_record("n1", "remote", {"engine_id": "remote", "grid_id": "n1", "pid": 0})
    asleep = relay.RelayError("heartbeat failed (503)", status=503, code=ASLEEP_CODE)

    for _ in range(3):
        _one_beat(monkeypatch, state, asleep)

    assert state.parked.is_set()
    assert run_records.read_record("n1", "remote")["last_register_error"] == service_truth.ASLEEP_REASON
    assert capsys.readouterr().err.count(service_truth.ASLEEP_REASON) == 1, "said once per sleep, not per beat"


def test_the_heartbeat_unparks_the_engine_when_the_grid_answers_again(monkeypatch, tmp_path, capsys):
    from shared import run_records

    state = _serve_state(monkeypatch, tmp_path)
    state._registration_recorded = True
    run_records.write_record("n1", "remote", {"engine_id": "remote", "grid_id": "n1", "pid": 0})
    _one_beat(monkeypatch, state, relay.RelayError("heartbeat failed (503)", status=503, code=ASLEEP_CODE))
    capsys.readouterr()

    _one_beat(monkeypatch, state, "ok")
    _one_beat(monkeypatch, state, "ok")

    assert not state.parked.is_set()
    assert "last_register_error" not in run_records.read_record("n1", "remote")
    assert capsys.readouterr().err.count("Resumed") == 1, "the log closes the pause it opened, once"


def test_a_different_word_from_the_relay_unparks_the_poll_workers(monkeypatch, tmp_path):
    """Parked means "the relay's last word was asleep". A different coded answer — the proxy's master
    down while it should be running — is a new word, and the workers go back to their ordinary retry
    rather than sitting out a grid that is coming straight back."""
    state = _serve_state(monkeypatch, tmp_path)
    state.parked.set()

    _one_beat(monkeypatch, state, relay.RelayError("heartbeat failed (503)", status=503,
                                                   code="grid_master_down"))

    assert not state.parked.is_set()


def test_a_heartbeat_that_reaches_nobody_keeps_the_park_and_says_nothing_new(monkeypatch, tmp_path, capsys):
    """A codeless failure — a dropped connection, something in front of the relay — says nothing about
    the grid. Ending the park on it would send every poll worker back to a grid that is still asleep,
    and announce the same sleep again on the next beat (found in review)."""
    state = _serve_state(monkeypatch, tmp_path)
    asleep = relay.RelayError("heartbeat failed (503)", status=503, code=ASLEEP_CODE)

    _one_beat(monkeypatch, state, asleep)
    _one_beat(monkeypatch, state, relay.RelayError("heartbeat transport error: connection reset"))
    assert state.parked.is_set()
    _one_beat(monkeypatch, state, asleep)

    assert capsys.readouterr().err.count(service_truth.ASLEEP_REASON) == 1


# --- a re-run `grid join` reports a parked engine truthfully -----------------------------------------


def _parked_record() -> dict:
    """A live identity that has been up past the bring-up window, parked on a sleeping grid."""
    from datetime import UTC, datetime, timedelta

    started = datetime.now(UTC) - timedelta(minutes=20)
    return {"pid": 4242, "started_at": started.isoformat(), "last_register_error": service_truth.ASLEEP_REASON}


def test_rejoining_a_parked_engine_does_not_advise_a_respawn(monkeypatch, tmp_path):
    """The join gate's advice for a stuck engine is `--respawn`. For one parked on a sleeping grid that
    advice is worse than useless — the fresh child meets the same sleeping grid and parks too — while
    the sentence above it already says nothing needs doing."""
    from remote import service_truth
    from shared import run_records

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    run_records.heartbeat_path("n1", "remote").parent.mkdir(parents=True, exist_ok=True)
    run_records.heartbeat_path("n1", "remote").touch()  # a build that reports service truth

    verdict = service_truth.not_serving(_parked_record(), "n1", "remote", log_path=tmp_path / "e.log")

    assert verdict is not None
    assert service_truth.ASLEEP_REASON in verdict.detail
    assert "--respawn" not in verdict.detail


def test_any_other_stuck_engine_is_still_advised_to_respawn(monkeypatch, tmp_path):
    from remote import service_truth
    from shared import run_records

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    run_records.heartbeat_path("n1", "remote").parent.mkdir(parents=True, exist_ok=True)
    run_records.heartbeat_path("n1", "remote").touch()
    record = {**_parked_record(), "last_register_error": "register failed (503): upstream unavailable"}

    verdict = service_truth.not_serving(record, "n1", "remote", log_path=tmp_path / "e.log")

    assert "--respawn" in verdict.detail


def test_the_asleep_sentence_survives_the_records_bound_intact():
    """The join gate recognises a parked engine by comparing the RECORDED reason to the sentence, and
    the record keeps only `REGISTER_ERROR_MAX_CHARS` of it — a longer sentence would be stored cut,
    never match, and bring the `--respawn` advice back for every sleeping grid."""
    assert len(service_truth.ASLEEP_REASON) <= service_truth.REGISTER_ERROR_MAX_CHARS


@pytest.mark.parametrize(
    "waker",
    ["inference", "signed-in read", "`grid join`", "owner starting it"],
)
def test_the_asleep_sentence_names_everything_that_wakes_the_grid(waker):
    """`idle-sleep` issue 04 (decision 1) made a signed-in read or action and a `grid join` wake a
    sleeping grid, beside inference and the owner's start — and the sentence a parked provider logs and
    records still named only the first and the last (issue 05, part H). A person reading it to find out
    how to get their grid back must be told every way."""
    assert waker in service_truth.ASLEEP_REASON


def test_the_asleep_sentence_does_not_say_this_engine_wakes_it():
    """A `grid join` wakes a grid; THIS engine — itself a running `grid join` — does not, by polling. The
    sentence must not read as if the process logging it were the thing that wakes the grid."""
    assert "new `grid join`" in service_truth.ASLEEP_REASON
    assert "does not wake it" in service_truth.ASLEEP_REASON


def test_a_parked_poll_worker_waits_between_looks_instead_of_spinning(monkeypatch, tmp_path):
    """Parked must mean ASLEEP, not busy. A park loop that stopped waiting would make no request — every
    other test here would stay green — while burning a core per poll worker for as long as the grid
    slept, on every provider of every sleeping grid."""
    from remote import serve

    class CountingEvent(threading.Event):
        def __init__(self) -> None:
            super().__init__()
            self.looks = 0

        def is_set(self) -> bool:
            self.looks += 1
            return super().is_set()

    monkeypatch.setattr(serve, "_PARK_TICK_SECONDS", 0.05)
    monkeypatch.setattr(relay, "poll", lambda *a, **k: None)
    state = _serve_state(monkeypatch, tmp_path)
    state.parked = CountingEvent()
    state.parked.set()
    worker = _run(serve._poll_loop, state)
    try:
        time.sleep(0.5)
    finally:
        state.stop.set()
        worker.join(timeout=5)

    assert state.parked.looks < 100, f"{state.parked.looks} looks in 0.5s — the worker spun"
