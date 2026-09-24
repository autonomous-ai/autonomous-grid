"""A provider meets a grid its OWNER stopped, or one that was DELETED (`idle-sleep` issue 04, part F).

grid-apis' proxy (issue 04) tells the two apart from a sleep, each with its own code beside ``detail``:

* ``503 grid_stopped`` — the owner stopped the grid, and nothing a caller sends wakes it. A provider
  PARKS, as it does on a sleeping grid (issue 02), and checks back once a minute: the owner's
  `grid start` is the only way out, and it is not one a provider can hurry.
* ``410 grid_deleted`` — the grid is gone. The engine STOPS, with one sentence, instead of sitting on a
  grid that no longer exists for as long as its machine is up (issue 02's review finding).

It also changes how often a provider's heartbeat asks (issue 04, decision 6): every 10s while the grid is
asleep or after a beat that failed, so a provider is back on a woken or restarted grid within seconds —
the master holds the first request for it — and every 60s while its owner has it stopped.
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from remote import bringup, relay, service_truth
from tests.test_grid_asleep import (
    _asleep,
    _RecordingStop,
    _register_failing,
    _relay_answering,
    _run,
    _serve_state,
    _wait_until,
)

#: The wire values, written out rather than imported (`tests/test_grid_sleep_lockstep.py` pins both
#: repositories to them).
STOPPED_CODE = "grid_stopped"
DELETED_CODE = "grid_deleted"

#: What grid-apis' proxy answers, in shape.
STOPPED_BODY = {
    "detail": "grid was stopped by its owner, and nothing wakes it until they start it again "
              "(`grid start`, or Start in the app)",
    "code": STOPPED_CODE,
}
DELETED_BODY = {"detail": "grid was deleted", "code": DELETED_CODE}


def _stopped() -> relay.RelayError:
    return relay.RelayError(f"failed (503): {json.dumps(STOPPED_BODY)}", status=503, code=STOPPED_CODE)


def _deleted() -> relay.RelayError:
    return relay.RelayError(f"failed (410): {json.dumps(DELETED_BODY)}", status=410, code=DELETED_CODE)


# --- the relay client reads both codes off the proxy's answer ----------------------------------------


@pytest.mark.parametrize("call", ["register", "heartbeat", "poll"])
@pytest.mark.parametrize(
    ("status", "body", "check"),
    [(503, STOPPED_BODY, "is_grid_stopped"), (410, DELETED_BODY, "is_grid_deleted")],
)
def test_each_provider_call_reports_what_the_proxy_said(call, status, body, check):
    with _relay_answering(status, body) as url, pytest.raises(relay.RelayError) as caught:
        if call == "register":
            relay.register_node(url, "AT", "node-1", models=["m"])
        elif call == "heartbeat":
            relay.heartbeat(url, "AT", load={})
        else:
            relay.poll(url, "AT", timeout=5.0)

    assert getattr(relay, check)(caught.value)
    assert caught.value.status == status


@pytest.mark.parametrize(
    "body",
    [
        {"detail": "grid was stopped"},  # an older proxy: no code
        {"detail": "stopped", "code": "grid_stop"},  # a reworded code this CLI never heard
        {"detail": {"code": STOPPED_CODE}},  # the task plane's nested shape, not the proxy's
        {"detail": "stopped", "code": True},
    ],
)
def test_anything_but_the_exact_code_is_neither(body):
    """Compared for EQUALITY: an unknown code must stay today's behaviour — a transient failure."""
    with _relay_answering(503, body) as url, pytest.raises(relay.RelayError) as caught:
        relay.heartbeat(url, "AT", load={})

    assert not relay.is_grid_stopped(caught.value)
    assert not relay.is_grid_deleted(caught.value)


# --- bring-up ------------------------------------------------------------------------------------------


def test_bringup_parks_on_a_grid_its_owner_stopped_and_checks_once_a_minute():
    stop = _RecordingStop()
    noted: list[str | None] = []
    lines: list[str] = []

    bringup.register_with_backoff(
        _register_failing([_stopped(), _stopped(), _stopped()]),
        stop=stop, log=lines.append, note_error=noted.append,
    )

    assert stop.waits == [60.0, 60.0, 60.0]
    assert noted == [service_truth.STOPPED_REASON] * 3
    assert sum(service_truth.STOPPED_REASON in line for line in lines) == 1, lines


def test_bringup_announces_a_change_of_pause_in_full():
    """Asleep, then stopped by its owner: two pauses with two different remedies, so the second is said
    in full too — not the short "still …" line that follows a pause already announced (found by a
    surviving mutant)."""
    lines: list[str] = []

    bringup.register_with_backoff(
        _register_failing([_asleep(), _asleep(), _stopped()]),
        stop=_RecordingStop(), log=lines.append, note_error=lambda _m: None,
    )

    assert "still asleep" in lines[1]
    assert service_truth.STOPPED_REASON in lines[2], lines


def test_bringup_ends_on_a_deleted_grid_with_one_sentence():
    """Terminal: a grid that is gone does not come back, and the child that kept retrying it was a
    process and a growing log per deleted grid for as long as the machine ran."""
    attempts: list[int] = []

    def register() -> None:
        attempts.append(1)
        raise _deleted()

    with pytest.raises(bringup.GridDeleted) as caught:
        bringup.register_with_backoff(
            register, stop=_RecordingStop(), log=lambda _m: None, note_error=lambda _m: None,
        )

    assert attempts == [1]
    assert str(caught.value) == service_truth.DELETED_REASON


def test_neither_code_is_mistaken_for_the_other_or_for_a_sleep():
    assert not relay.is_grid_asleep(_stopped()) and not relay.is_grid_deleted(_stopped())
    assert not relay.is_grid_asleep(_deleted()) and not relay.is_grid_stopped(_deleted())


# --- a serving engine ------------------------------------------------------------------------------------


def test_a_poll_worker_that_meets_an_owners_stop_parks_every_worker(monkeypatch, tmp_path):
    from remote import serve

    state = _serve_state(monkeypatch, tmp_path)
    polls: list[int] = []

    def poll(url, tok, *, timeout=None):
        polls.append(1)
        raise _stopped()

    monkeypatch.setattr(relay, "poll", poll)
    worker = _run(serve._poll_loop, state)
    try:
        assert _wait_until(state.parked.is_set), "the owner's stop did not park the engine"
        assert state.park_reason == service_truth.STOPPED_REASON
        time.sleep(2.3)  # past the ordinary 2s poll retry
        assert polls == [1], f"the worker kept polling a stopped grid: {len(polls)} requests"
    finally:
        state.stop.set()
        worker.join(timeout=5)


def test_a_poll_worker_that_meets_a_deleted_grid_ends_the_engine(monkeypatch, tmp_path, capsys):
    from remote import serve

    state = _serve_state(monkeypatch, tmp_path)
    monkeypatch.setattr(relay, "poll", lambda *a, **k: (_ for _ in ()).throw(_deleted()))
    worker = _run(serve._poll_loop, state)
    try:
        assert _wait_until(state.stop.is_set), "a deleted grid did not end the engine"
    finally:
        state.stop.set()
        worker.join(timeout=5)

    assert capsys.readouterr().err.count(service_truth.DELETED_REASON) == 1


class _OneWait(threading.Event):
    """A stop event for exactly one heartbeat tick: its first wait records the interval and ends the loop."""

    def __init__(self) -> None:
        super().__init__()
        self.waits: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:  # type: ignore[override]
        self.waits.append(timeout)
        self.set()
        return True


def _beat(monkeypatch, state, answer) -> float | None:
    """One heartbeat tick answered with ``answer`` (raised when an exception); returns its wait."""
    from remote import serve

    monkeypatch.setattr(serve, "_maybe_refresh_codex", lambda s: None)
    monkeypatch.setattr(serve, "_maybe_probe_engines", lambda s: None)

    def beat(url, tok, *, load, meta=None):
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(relay, "heartbeat", beat)
    stop = _OneWait()
    state.stop = stop
    serve._heartbeat_loop(state)
    return stop.waits[-1] if stop.waits else None


def test_the_heartbeat_parks_on_an_owners_stop_and_records_why(monkeypatch, tmp_path, capsys):
    from shared import run_records

    state = _serve_state(monkeypatch, tmp_path)
    run_records.write_record("n1", "remote", {"engine_id": "remote", "grid_id": "n1", "pid": 0})

    for _ in range(3):
        _beat(monkeypatch, state, _stopped())

    assert state.parked.is_set()
    assert run_records.read_record("n1", "remote")["last_register_error"] == service_truth.STOPPED_REASON
    assert capsys.readouterr().err.count(service_truth.STOPPED_REASON) == 1


def test_a_grid_that_goes_from_asleep_to_stopped_is_announced_again(monkeypatch, tmp_path, capsys):
    """Two different pauses, two different remedies — the operator is told when it changes."""
    state = _serve_state(monkeypatch, tmp_path)

    _beat(monkeypatch, state, relay.RelayError("503", status=503, code="grid_asleep"))
    _beat(monkeypatch, state, _stopped())

    err = capsys.readouterr().err
    assert service_truth.ASLEEP_REASON in err and service_truth.STOPPED_REASON in err
    assert state.park_reason == service_truth.STOPPED_REASON


def test_the_heartbeat_ends_the_engine_on_a_deleted_grid(monkeypatch, tmp_path, capsys):
    from shared import run_records

    state = _serve_state(monkeypatch, tmp_path)
    run_records.write_record("n1", "remote", {"engine_id": "remote", "grid_id": "n1", "pid": 0})

    _beat(monkeypatch, state, _deleted())

    assert state.stop.is_set()
    assert run_records.read_record("n1", "remote")["last_register_error"] == service_truth.DELETED_REASON
    assert capsys.readouterr().err.count(service_truth.DELETED_REASON) == 1


@pytest.mark.parametrize(
    ("answer", "wait"),
    [
        ("ok", 30.0),
        # Parked on a sleep: a woken grid's master holds its first request for a provider, so the
        # provider must be back within seconds (decision 6).
        (relay.RelayError("503", status=503, code="grid_asleep"), 10.0),
        # Parked on an owner's stop: nothing the provider does hurries a `grid start`.
        (relay.RelayError("503", status=503, code=STOPPED_CODE), 60.0),
        # A beat that failed: a master restarted by the platform must see this provider again fast.
        (relay.RelayError("503", status=503, code="grid_master_down"), 10.0),
        (relay.RelayError("heartbeat transport error: connection reset"), 10.0),
    ],
)
def test_the_heartbeat_asks_as_often_as_the_answer_calls_for(monkeypatch, tmp_path, answer, wait):
    state = _serve_state(monkeypatch, tmp_path)
    state._registration_recorded = True

    assert _beat(monkeypatch, state, answer) == wait


def test_a_parked_engine_that_hears_ok_is_back_on_the_ordinary_interval(monkeypatch, tmp_path):
    state = _serve_state(monkeypatch, tmp_path)
    state._registration_recorded = True
    _beat(monkeypatch, state, _stopped())

    assert _beat(monkeypatch, state, "ok") == 30.0
    assert not state.parked.is_set()


# --- the join gate --------------------------------------------------------------------------------------


def test_rejoining_an_engine_parked_on_an_owners_stop_does_not_advise_a_respawn(monkeypatch, tmp_path):
    """A fresh child meets the same stopped grid and parks too."""
    from datetime import UTC, datetime, timedelta

    from shared import run_records

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    run_records.heartbeat_path("n1", "remote").parent.mkdir(parents=True, exist_ok=True)
    run_records.heartbeat_path("n1", "remote").touch()
    record = {
        "pid": 4242,
        "started_at": (datetime.now(UTC) - timedelta(minutes=20)).isoformat(),
        "last_register_error": service_truth.STOPPED_REASON,
    }

    verdict = service_truth.not_serving(record, "n1", "remote", log_path=tmp_path / "e.log")

    assert verdict is not None
    assert service_truth.STOPPED_REASON in verdict.detail
    assert "--respawn" not in verdict.detail


@pytest.mark.parametrize("reason", ["STOPPED_REASON", "DELETED_REASON"])
def test_each_sentence_survives_the_records_bound_intact(reason):
    """The gate recognises them by comparing the RECORDED reason to the sentence."""
    assert len(getattr(service_truth, reason)) <= service_truth.REGISTER_ERROR_MAX_CHARS


def test_the_stopped_sentence_names_who_can_start_it_and_that_the_engine_will_rejoin():
    reason = service_truth.STOPPED_REASON
    assert "owner" in reason and "start" in reason
    assert "by itself" in reason


def test_the_owner_stopped_and_deleted_sentences_are_exactly_what_they_were():
    """`idle-sleep` issue 05, part H reworded the ASLEEP sentence only. These two are recognised by
    EQUALITY in the records every running engine has already written, so a reword here would bring the
    `--respawn` advice back for every engine parked or stopped under the old words — pinned as literals,
    because a test comparing the constant to itself pins nothing."""
    assert service_truth.STOPPED_REASON == (
        "the grid's owner has stopped it, and nothing wakes it until they start it again (`grid start`); "
        "this engine keeps checking and rejoins by itself once it is started"
    )
    assert service_truth.DELETED_REASON == "the grid was deleted, so this engine has stopped serving it"
