"""Stop and reload requests for a detached engine child on Windows (`shared.control_signal`).

Windows delivers no SIGTERM to a console-less child and has no SIGHUP at all, so `grid leave` could only
force-kill (the engine never unregistered) and a remote re-join's hot reload raised `AttributeError`.
The request travels as a sidecar file beside the run record instead, addressed by (grid, engine) —
never by pid, which on Windows can be uv's launcher rather than the Python child.

The Windows branches run here on any OS by flipping the modules' platform flags.
"""
from __future__ import annotations

import threading
import time

import pytest

from shared import control_signal, run_records

GRID, ENGINE = "ag-test", "e1"


@pytest.fixture
def grid_home(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    run_records.record_path(GRID, ENGINE).parent.mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(control_signal, "_IS_WINDOWS", True)
    monkeypatch.setattr(run_records, "_IS_WINDOWS", True)


def _until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ------------------------------------------------------------------ the watch


def test_posix_start_returns_none_and_starts_no_thread(grid_home, monkeypatch):
    monkeypatch.setattr(control_signal, "_IS_WINDOWS", False)
    before = threading.active_count()
    assert control_signal.Watch.start(GRID, ENGINE) is None
    assert threading.active_count() == before


def test_request_then_watch_calls_the_handler_once_and_removes_the_file(grid_home, windows):
    calls: list[str] = []
    watch = control_signal.Watch.start(GRID, ENGINE, interval=0.01)
    try:
        watch.on("stop", lambda: calls.append("stop"))
        control_signal.request(GRID, ENGINE, "stop")
        assert _until(lambda: calls == ["stop"])
        assert not run_records.control_path(GRID, ENGINE, "stop").exists()
        time.sleep(0.05)
        assert calls == ["stop"], "one request, one delivery"
    finally:
        watch.close()


def test_a_stale_stop_file_is_cleared_not_acted_on(grid_home, windows):
    """A stop left over from a child that died before reading it must not kill its replacement."""
    run_records.control_path(GRID, ENGINE, "stop").touch()
    calls: list[str] = []
    watch = control_signal.Watch.start(GRID, ENGINE, interval=0.01)
    try:
        watch.on("stop", lambda: calls.append("stop"))
        time.sleep(0.05)
        assert calls == []
        assert not run_records.control_path(GRID, ENGINE, "stop").exists()
    finally:
        watch.close()


def test_a_reload_written_before_the_watch_starts_is_delivered_once_registered(grid_home, windows):
    """The queued SIGHUP of remote/serve.py: a reload sent during bring-up lands once the handler exists."""
    watch = control_signal.Watch.start(GRID, ENGINE, interval=0.01)
    try:
        control_signal.request(GRID, ENGINE, "reload")
        time.sleep(0.05)
        assert run_records.control_path(GRID, ENGINE, "reload").exists(), "no handler yet: stays queued"
        calls: list[str] = []
        watch.on("reload", lambda: calls.append("reload"))
        assert _until(lambda: calls == ["reload"])
    finally:
        watch.close()


def test_a_watch_keeps_the_directory_it_started_in(grid_home, windows, monkeypatch, tmp_path):
    """A watch resolves its request files once, at start. Re-reading GRID_HOME on every poll let a
    watch left running in one test pick up a stop request written under another test's GRID_HOME —
    it interrupted a pytest worker on real Windows."""
    calls: list[str] = []
    watch = control_signal.Watch.start(GRID, ENGINE, interval=0.01)
    try:
        watch.on("stop", lambda: calls.append("stop"))
        other_home = tmp_path / "other-home"
        monkeypatch.setenv("GRID_HOME", str(other_home))
        run_records.record_path(GRID, ENGINE).parent.mkdir(parents=True)
        control_signal.request(GRID, ENGINE, "stop")
        time.sleep(0.1)
        assert calls == [], "a request under a different GRID_HOME is not this watch's"
        assert run_records.control_path(GRID, ENGINE, "stop").exists()
    finally:
        watch.close()


def test_acknowledged_false_removes_the_unconsumed_file(grid_home, windows):
    control_signal.request(GRID, ENGINE, "stop")
    started = time.monotonic()
    assert control_signal.acknowledged(GRID, ENGINE, "stop", timeout=0.1) is False
    assert time.monotonic() - started < 0.5
    assert not run_records.control_path(GRID, ENGINE, "stop").exists()


def test_remove_record_also_removes_control_files(grid_home):
    run_records.record_path(GRID, ENGINE).write_text("{}")
    for kind in ("stop", "reload"):
        run_records.control_path(GRID, ENGINE, kind).touch()
    run_records.remove_record(GRID, ENGINE)
    assert not any(run_records.control_path(GRID, ENGINE, kind).exists() for kind in ("stop", "reload"))


# ------------------------------------------------------------------ grid leave


def _live_record(monkeypatch, pid: int) -> dict:
    monkeypatch.setattr(run_records, "record_verdict", lambda record: run_records.RecordVerdict.LIVE_OURS)
    monkeypatch.setattr(run_records, "recorded_pgid", lambda record: None)
    return {"grid_id": GRID, "engine_id": ENGINE, "pid": pid}


def test_windows_terminate_recorded_asks_the_child_to_stop_before_killing(grid_home, windows, monkeypatch):
    alive = {"value": True}
    monkeypatch.setattr(run_records, "pid_alive", lambda pid: alive["value"])
    killed: list[int] = []
    monkeypatch.setattr(run_records, "kill_group", killed.append)
    stop_file = run_records.control_path(GRID, ENGINE, "stop")

    def child():  # consumes the request, unregisters, exits
        assert _until(stop_file.exists)
        stop_file.unlink()
        time.sleep(0.1)
        alive["value"] = False

    threading.Thread(target=child, daemon=True).start()
    outcome = run_records.terminate_recorded(_live_record(monkeypatch, 4242))
    assert outcome.survivor == 0
    assert killed == [], "it exited on request: nothing to force"


def test_windows_unacknowledged_stop_kills_at_once(grid_home, windows, monkeypatch):
    alive = {"value": True}
    monkeypatch.setattr(run_records, "pid_alive", lambda pid: alive["value"])
    killed: list[int] = []
    monkeypatch.setattr(run_records, "kill_group", lambda pid: (killed.append(pid), alive.update(value=False)))
    started = time.monotonic()
    outcome = run_records.terminate_recorded(_live_record(monkeypatch, 4242))
    assert time.monotonic() - started < 1.5, "no consumer: one ack window, never the full grace"
    assert killed == [4242]
    assert outcome.survivor == 0
    assert not run_records.control_path(GRID, ENGINE, "stop").exists()


def test_posix_terminate_recorded_writes_no_control_file(grid_home, monkeypatch):
    monkeypatch.setattr(run_records, "_IS_WINDOWS", False)
    monkeypatch.setattr(run_records, "terminate_pid", lambda pid: True)
    requested: list[tuple] = []
    monkeypatch.setattr(control_signal, "request", lambda *a: requested.append(a))
    run_records.terminate_recorded(_live_record(monkeypatch, 4242))
    assert requested == []


# ------------------------------------------------------------------ remote hot reload


def test_windows_request_reload_leaves_a_reload_file_for_a_live_child(grid_home, monkeypatch):
    from cli import remote_provider

    monkeypatch.setattr(remote_provider.run_records, "record_alive", lambda record: True)
    remote_provider._request_reload(GRID, {"pid": 4242})
    assert run_records.control_path(GRID, run_records.REMOTE_IDENTITY, "reload").exists()


def test_windows_request_reload_of_a_dead_child_raises_process_lookup(grid_home, monkeypatch):
    from cli import remote_provider

    monkeypatch.setattr(remote_provider.run_records, "record_alive", lambda record: False)
    with pytest.raises(ProcessLookupError):
        remote_provider._request_reload(GRID, {"pid": 4242})
    assert not run_records.control_path(GRID, run_records.REMOTE_IDENTITY, "reload").exists()


def test_reload_signal_names_the_mechanism_this_platform_delivers(monkeypatch):
    """The stamp `_hot_reloadable` trusts: a child that cannot read the request must be respawned."""
    from cli import remote_provider

    monkeypatch.setattr(remote_provider, "_IS_WINDOWS", False)
    assert remote_provider._reload_signal() == "sighup"
    monkeypatch.setattr(remote_provider, "_IS_WINDOWS", True)
    assert remote_provider._reload_signal() == "request-file"


def test_windows_hot_reloadable_only_for_a_child_that_reads_request_files(monkeypatch):
    """A Windows child stamped `sighup` by an older build never reads the request: it respawns once."""
    from cli import remote_provider

    monkeypatch.setattr(remote_provider, "_IS_WINDOWS", True)
    record = {"engines": []}
    external = [{"endpoint_url": "http://127.0.0.1:9001", "models": ["x"]}]

    def live(reload_signal):
        return [{"engine_id": "remote", "reload_signal": reload_signal, "serves_systemone": True, "engines": []}]

    assert remote_provider._hot_reloadable(live("request-file"), external, record) is True
    assert remote_provider._hot_reloadable(live("sighup"), external, record) is False


def test_install_stop_turns_a_stop_request_into_the_interrupt(grid_home, windows, monkeypatch):
    interrupted: list[str] = []
    monkeypatch.setattr(control_signal, "interrupt_main", lambda: interrupted.append("stop"))
    watch = control_signal.install_stop(GRID, ENGINE, interval=0.01)
    try:
        control_signal.request(GRID, ENGINE, "stop")
        assert _until(lambda: interrupted == ["stop"])
    finally:
        watch.close()


def test_local_engine_child_installs_the_stop_watch(grid_home, monkeypatch):
    from cli import provider

    run_records.write_record(GRID, ENGINE, {"engine_id": ENGINE, "grid_id": GRID, "node_id": "n", "pid": 1})
    installed: list[tuple[str, str]] = []
    monkeypatch.setattr(control_signal, "install_stop", lambda g, e: installed.append((g, e)))
    monkeypatch.setattr(provider, "_run_engine", lambda args: 0)
    assert provider.run_engine_from_record(GRID, ENGINE) == 0
    assert installed == [(GRID, ENGINE)]


def test_windows_hot_reload_sends_a_request_not_a_signal(grid_home, monkeypatch):
    from cli import remote_provider

    monkeypatch.setattr(remote_provider, "_IS_WINDOWS", True)
    monkeypatch.setattr(remote_provider.run_records, "record_alive", lambda record: True)
    monkeypatch.setattr(remote_provider.run_records, "write_record", lambda *a, **k: None)

    def no_signal(pid):
        raise AssertionError("Windows has no SIGHUP")

    monkeypatch.setattr(remote_provider, "_signal_reload", no_signal)
    asked: list[str] = []
    monkeypatch.setattr(remote_provider, "_request_reload", lambda network_id, singleton: asked.append(network_id))
    assert remote_provider._hot_reload_identity(GRID, {}, [{"pid": 4242}]) is True
    assert asked == [GRID]
