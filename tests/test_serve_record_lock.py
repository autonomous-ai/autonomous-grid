"""The serve child's run-record writes go through the record lock (ADR 0010 F3).

`grid join` merges its union and `grid leave` tears the identity down under
`file_lock(record_path(...))`. A serve-child write that skips that lock can lose a join's append, or
re-create a record a leave has just deleted. These tests hold the lock for real, from another thread
(``flock`` conflicts across open file descriptions even inside one process), so they see contention
that a mocked serve child cannot.
"""
from __future__ import annotations

import base64
import contextlib
import json
import threading

import pytest

from shared import jsonio, run_records
from shared.filelock import file_lock

QUOTA = {"plan_type": "pro", "primary": {"used_percent": 12.5}}


def _serve_state(monkeypatch, tmp_path):
    from remote import serve
    from shared.system import host

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    monkeypatch.setattr(host, "platform_kind", lambda: "linux")
    monkeypatch.setattr(host, "disk_gb", lambda path: None)
    return serve._ServeState(
        signaling_url="https://relay.example", node_id="node-1", network_id="n1",
        llm_url="https://chatgpt.example/backend-api/codex", access_token="AT", refresh_token="RT",
        models=["gpt-5"], capabilities={"schema_version": 1, "models": {}},
        meta={"name": "e1", "engine": "codex"}, pricing={}, max_concurrency=1,
    )


def _remote_record():
    return {"engine_id": "remote", "grid_id": "n1", "pid": 0}


def test_codex_quota_persist_never_waits_on_a_held_record_lock(monkeypatch, tmp_path):
    """The quota is captured on a served job's path (the upstream's headers), and the lock's other
    holder can be a `grid leave` that holds it across its whole teardown of this very process. A wait
    there would hold the job past the drain; an unlocked write is the lost update. So: skip."""
    state = _serve_state(monkeypatch, tmp_path)
    run_records.write_record("n1", "remote", _remote_record())
    path = run_records.record_path("n1", "remote")
    done = threading.Event()

    with file_lock(path):  # a `grid leave` mid-teardown
        job = threading.Thread(target=lambda: (state.set_codex_quota(QUOTA), done.set()), daemon=True)
        job.start()
        assert done.wait(2), "the served job waited on the record lock"
        assert "codex_rate_limits" not in jsonio.load_json(path), \
            "the quota was written while another process held the record lock"

    assert state._codex_quota == QUOTA  # skipped on disk only: the next heartbeat still carries it


def test_codex_quota_persists_when_the_lock_is_free(monkeypatch, tmp_path):
    """Positive control for the skip above: uncontended, the snapshot lands on the record."""
    state = _serve_state(monkeypatch, tmp_path)
    run_records.write_record("n1", "remote", _remote_record())

    state.set_codex_quota(QUOTA)

    assert run_records.read_record("n1", "remote")["codex_rate_limits"] == QUOTA


def test_codex_quota_never_recreates_a_reaped_record(monkeypatch, tmp_path):
    state = _serve_state(monkeypatch, tmp_path)
    path = run_records.record_path("n1", "remote")
    assert not path.exists()

    state.set_codex_quota(QUOTA)

    assert not path.exists()


def test_codex_quota_persist_survives_a_corrupt_record(monkeypatch, tmp_path, capsys):
    """`jsonio` raises `SystemExit` for a corrupt record, which `except Exception` does not catch —
    and this runs inside a served job, which must not die of a telemetry write."""
    state = _serve_state(monkeypatch, tmp_path)
    path = run_records.record_path("n1", "remote")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    state.set_codex_quota(QUOTA)

    assert state._codex_quota == QUOTA
    assert "codex_rate_limits" in capsys.readouterr().err


def _media_only_child(monkeypatch, tmp_path, prepare):
    """Run the detached serve child over a media-only record, with `prepare` standing in for the
    ComfyUI bring-up. Returns what the relay was told."""
    from local import media_engine, media_runtime
    from remote import relay, serve
    from shared.engine import comfyui

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    run_records.write_record("n1", "media1", {
        "engine_id": "media1", "node_id": "ignored", "grid_id": "n1", "pid": 0,
        "signaling_url": "https://relay.example", "endpoint_url": None, "models": [], "engines": [],
        "media": True, "media_bundles": ["image_generation"], "comfyui_port": 8188, "media_port": 8190,
    })
    claims = base64.urlsafe_b64encode(json.dumps({"node_id": "node-xyz"}).encode()).decode().rstrip("=")
    monkeypatch.setattr(serve, "_load_tokens", lambda net: (f"h.{claims}.s", "RT"))

    class _Proc:
        pid = 1234

    def _prepare(**kw):
        prepare()
        return {"models": ["comfyui:image_generation"], "proc": _Proc(), "media_url": "unused",
                "comfyui_started": True}

    monkeypatch.setattr(media_engine, "prepare_media_engine", _prepare)
    captured: dict = {}
    monkeypatch.setattr(relay, "register_node", lambda url, tok, node, **kw: captured.update(node=node))
    monkeypatch.setattr(serve, "_poll_loop", lambda state: state.stop.set())
    monkeypatch.setattr(relay, "heartbeat", lambda *a, **k: "ok")
    monkeypatch.setattr(relay, "unregister_node", lambda *a, **k: None)
    monkeypatch.setattr(media_runtime, "stop_media_server", lambda proc, **k: None)
    monkeypatch.setattr(comfyui, "stop", lambda: None)
    serve.run_remote_engine_from_record("n1", "media1")
    return captured


def test_comfyui_ownership_is_written_under_the_record_lock(monkeypatch, tmp_path):
    """`comfyui_started` lands at bring-up, the window in which a concurrent `grid leave` deletes the
    record. Written without the lock, it can re-create that record; under it, the write waits for the
    leave and then finds the record gone."""
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    path = run_records.record_path("n1", "media1")
    held = threading.Event()
    reached = threading.Event()  # the child asked for the lock, or wrote without it
    seen: dict = {}

    def leave():  # a full `grid leave`: under the lock, look, then delete
        with file_lock(path):
            held.set()
            assert reached.wait(5), "the serve child never reached the comfyui_started write"
            seen["written_while_held"] = "comfyui_started" in jsonio.load_json(path)
            path.unlink()

    leaver = threading.Thread(target=leave, daemon=True)
    real_lock, real_write = run_records.file_lock, run_records.write_record

    @contextlib.contextmanager
    def lock_then_tell(p):
        reached.set()  # before waiting: the lock is what the child is about to block on
        with real_lock(p):
            yield

    def write_then_tell(*args, **kwargs):
        real_write(*args, **kwargs)
        reached.set()  # after landing, so the leave sees the unlocked write

    def prepare():
        # Watched from here on only: the child's pid stamp, before this, writes the record too.
        monkeypatch.setattr(run_records, "file_lock", lock_then_tell)
        monkeypatch.setattr(run_records, "write_record", write_then_tell)
        leaver.start()
        assert held.wait(2)

    _media_only_child(monkeypatch, tmp_path, prepare)
    leaver.join(5)

    assert seen == {"written_while_held": False}, "comfyui_started was written past the record lock"
    assert not path.exists(), "the serve child re-created a record `grid leave` had deleted"


def test_comfyui_ownership_write_failure_does_not_end_the_bring_up(monkeypatch, tmp_path, capsys):
    """A lost flag costs only the reap after a SIGKILL (the child's own teardown stops the ComfyUI it
    started), so a corrupt record must not end the bring-up — and the log says what was lost."""
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    path = run_records.record_path("n1", "media1")

    def prepare():
        path.write_text("{not json", encoding="utf-8")

    captured = _media_only_child(monkeypatch, tmp_path, prepare)

    assert captured.get("node") == "node-xyz", "the engine never registered"
    assert "comfyui_started" in capsys.readouterr().err


@pytest.mark.parametrize("held", [False, True], ids=["free", "contended"])
def test_try_mutate_record_writes_only_when_the_lock_is_free(monkeypatch, tmp_path, held):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    run_records.write_record("n1", "remote", _remote_record())
    path = run_records.record_path("n1", "remote")

    def _set(record):
        record["k"] = 1

    result: list[bool] = []

    def call():
        result.append(run_records.try_mutate_record("n1", "remote", _set))

    def run():  # off the test's thread, so an implementation that waits fails instead of hanging
        caller = threading.Thread(target=call, daemon=True)
        caller.start()
        caller.join(2)

    if held:
        with file_lock(path):
            run()
    else:
        run()
    assert result == [not held]
    assert ("k" in jsonio.load_json(path)) is not held
