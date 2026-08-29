from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import shared.allocator.runtime as runtime_module
from shared.allocator.local import HostPolicy, LocalHostProtectionLoop, LocalOverride
from shared.allocator.models import (
    ActionKind,
    MutationAction,
    NodeState,
    ResidencyState,
)
from shared.allocator.reconcile import MutationStatus
from shared.allocator.runtime import (
    LlamaCppBackend,
    ManagedModelRuntime,
    ManagedResidency,
    RuntimeHandle,
    clear_local_override,
    engine_api_key_path,
    local_override_path,
    write_local_override,
)
from shared.system.hostsignals import HostSignals


class FakeBackend:
    def __init__(self, cached: tuple[str, ...] = ("qwen.gguf",)) -> None:
        self.cached = set(cached)
        self.artifacts = {model_id: "a" * 64 for model_id in cached}
        self.live: dict[int, tuple[str, int]] = {}
        self.starts: list[tuple[str, int]] = []
        self.stops: list[tuple[int, str]] = []
        self.next_pid = 10_000
        self.start_gate: threading.Event | None = None
        self.spawned = threading.Event()
        self.unready: set[int] = set()
        self.unowned: set[int] = set()
        self.raise_after_spawn = False
        self.cancelled = False

    def cached_models(self) -> tuple[str, ...]:
        return tuple(sorted(self.cached))

    def artifact_sha256(self, model_id: str) -> str:
        return self.artifacts[model_id]

    def start(self, model_id: str, port: int) -> RuntimeHandle:
        if self.start_gate is not None:
            self.start_gate.wait(2)
        if self.cancelled:
            raise RuntimeError("cancelled")
        self.next_pid += 1
        handle = RuntimeHandle(self.next_pid, port)
        self.live[handle.pid] = (model_id, port)
        self.starts.append((model_id, port))
        return handle

    def start_with_callback(self, model_id, port, on_spawn):
        self.next_pid += 1
        handle = RuntimeHandle(self.next_pid, port, f"fake:{self.next_pid}")
        self.live[handle.pid] = (model_id, port)
        self.starts.append((model_id, port))
        on_spawn(handle)
        self.spawned.set()
        if self.raise_after_spawn:
            raise RuntimeError("simulated daemon crash after spawn")
        if self.start_gate is not None:
            self.unready.add(handle.pid)
            self.start_gate.wait(2)
            self.unready.discard(handle.pid)
        if self.cancelled:
            self.live.pop(handle.pid, None)
            raise RuntimeError("cancelled")
        return handle

    def alive(self, handle: RuntimeHandle) -> bool:
        return handle.pid in self.live

    def owns(self, handle: RuntimeHandle, model_id: str) -> bool:
        return handle.pid not in self.unowned and self.live.get(handle.pid) == (
            model_id,
            handle.port,
        )

    def ready(self, handle: RuntimeHandle, model_id: str) -> bool:
        return handle.pid not in self.unready and self.owns(handle, model_id)

    def stop(self, handle: RuntimeHandle, model_id: str) -> None:
        if not self.owns(handle, model_id):
            raise RuntimeError("not owned")
        self.stops.append((handle.pid, model_id))
        self.live.pop(handle.pid)

    def cancel_pending(self) -> None:
        self.cancelled = True
        if self.start_gate is not None:
            self.start_gate.set()


class StaticCollector:
    def __init__(self, signals: HostSignals) -> None:
        self.signals = signals

    def collect(self) -> HostSignals:
        return self.signals


class Clock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def action(
    kind: ActionKind,
    *,
    action_id: str | None = None,
    model_id: str = "qwen.gguf",
    host_id: str = "host-a",
    generation: str = "0000000000100-plan",
    dependencies: tuple[str, ...] = (),
    artifact_sha256: str = "",
    controller_term: int = 0,
    controller_id: str = "",
    controller_lease_expires_at: float = 0.0,
) -> MutationAction:
    return MutationAction(
        action_id=action_id or f"{kind.value}-{generation}",
        kind=kind,
        node_id=host_id,
        model_id=model_id,
        memory_mb=8_000,
        reason="test",
        plan_generation=generation,
        created_at=100,
        dependencies=dependencies,
        executable=True,
        artifact_sha256=artifact_sha256,
        controller_term=controller_term,
        controller_id=controller_id,
        controller_lease_expires_at=controller_lease_expires_at,
    )


def runtime(tmp_path, backend=None, clock=None, **kwargs) -> ManagedModelRuntime:
    return ManagedModelRuntime(
        tmp_path / "runtime.json",
        host_id="host-a",
        backend=backend or FakeBackend(),
        clock=clock or Clock(),
        port_available=lambda _port: True,
        **kwargs,
    )


def wait(runtime: ManagedModelRuntime) -> None:
    assert runtime.wait_idle(2)


def receipt_status(runtime: ManagedModelRuntime, action_id: str) -> MutationStatus:
    row = next(
        item for item in runtime.acknowledgements() if item["action_id"] == action_id
    )
    return MutationStatus(row["status"])


def test_mutation_action_wire_round_trip_and_schema_validation():
    original = action(
        ActionKind.WARM,
        dependencies=("load",),
        artifact_sha256="A" * 64,
    )
    assert original.artifact_sha256 == "a" * 64
    assert MutationAction.from_dict(original.to_dict()) == original
    broken = {**original.to_dict(), "schema_version": 99}
    try:
        MutationAction.from_dict(broken)
    except ValueError as exc:
        assert "schema" in str(exc)
    else:  # pragma: no cover - assertion clarity
        raise AssertionError("unknown schema was accepted")


def test_adopted_process_exit_as_zombie_completes_without_identity_false_alarm(
    monkeypatch,
):
    stopped = iter((False, True))
    identities = iter((True, False))
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(runtime_module, "stopped_running", lambda _pid: next(stopped))
    monkeypatch.setattr(runtime_module, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        runtime_module.os,
        "kill",
        lambda pid, sent_signal: signals.append((pid, sent_signal)),
    )

    runtime_module._terminate_owned_pid(
        12_345,
        identity_check=lambda: next(identities),
    )

    assert signals == [(12_345, runtime_module.signal.SIGTERM)]


def test_adopted_zombie_is_dead_for_health_recovery(monkeypatch):
    backend = LlamaCppBackend()
    handle = RuntimeHandle(
        12_345,
        18_081,
        "birth:original",
        executable_path="/opt/grid/llama-server",
        model_path="/models/qwen.gguf",
    )
    monkeypatch.setattr(runtime_module, "stopped_running", lambda _pid: True)

    assert backend.alive(handle) is False


def test_adopted_live_process_with_changed_identity_still_fails_closed(monkeypatch):
    monkeypatch.setattr(runtime_module, "stopped_running", lambda _pid: False)

    with pytest.raises(RuntimeError, match="ownership changed"):
        runtime_module._terminate_owned_pid(
            12_345,
            identity_check=lambda: False,
        )


def test_runtime_persists_stable_host_identity(tmp_path):
    backend = FakeBackend()
    first = ManagedModelRuntime(tmp_path / "state.json", backend=backend)
    second = ManagedModelRuntime(tmp_path / "state.json", backend=backend)
    assert second.host_id == first.host_id
    assert second.host_id.startswith("host-")
    assert second.engine_api_key == first.engine_api_key
    assert len(first.engine_api_key) >= 32
    if runtime_module.os.name != "nt":
        assert oct((tmp_path / "state.json").stat().st_mode & 0o777) == "0o600"
        assert oct(engine_api_key_path(tmp_path / "state.json").stat().st_mode & 0o777) == "0o600"
    assert engine_api_key_path(tmp_path / "state.json").read_text().strip() == first.engine_api_key
    with pytest.raises(AttributeError):
        second.engine_api_key = "replacement"


def test_load_verifies_cache_and_reports_success_once(tmp_path):
    managed = runtime(tmp_path)
    command = action(ActionKind.LOAD)
    running = managed.begin(command)
    assert running and running.status == MutationStatus.RUNNING
    wait(managed)
    assert receipt_status(managed, command.action_id) == MutationStatus.SUCCEEDED
    assert managed.residencies[0].state == ResidencyState.CACHED

    acknowledgement = managed.acknowledgements()
    managed.mark_acknowledged(acknowledgement)
    assert managed.acknowledgements() == []
    assert managed.begin(command).status == MutationStatus.SUCCEEDED
    assert managed.acknowledgements() == acknowledgement


def test_checksum_protected_load_and_warm_publish_proven_artifact(tmp_path):
    managed = runtime(tmp_path)
    load = action(ActionKind.LOAD, artifact_sha256="a" * 64)
    managed.begin(load)
    wait(managed)

    assert receipt_status(managed, load.action_id) == MutationStatus.SUCCEEDED
    assert managed.residencies[0].artifact_sha256 == "a" * 64

    warm = action(
        ActionKind.WARM,
        action_id="warm-proven-artifact",
        artifact_sha256="a" * 64,
    )
    managed.begin(warm)
    wait(managed)

    assert receipt_status(managed, warm.action_id) == MutationStatus.SUCCEEDED
    assert managed.residencies[0].state == ResidencyState.READY
    assert managed.residencies[0].artifact_sha256 == "a" * 64
    assert managed.allocator_envelope()["residencies"][0]["artifact_sha256"] == "a" * 64


def test_checksum_mismatch_fails_before_starting_model(tmp_path):
    backend = FakeBackend()
    managed = runtime(tmp_path, backend=backend)
    command = action(ActionKind.WARM, artifact_sha256="b" * 64)

    managed.begin(command)
    wait(managed)

    assert receipt_status(managed, command.action_id) == MutationStatus.FAILED
    assert backend.starts == []
    assert "SHA-256 mismatch" in managed.acknowledgements()[0]["message"]


@pytest.mark.parametrize("kind", [ActionKind.DRAIN, ActionKind.UNLOAD])
def test_stale_destructive_command_cannot_touch_newer_artifact(tmp_path, kind):
    managed = runtime(tmp_path)
    warm = action(ActionKind.WARM, artifact_sha256="a" * 64)
    managed.begin(warm)
    wait(managed)
    assert managed.residencies[0].state == ResidencyState.READY

    stale = action(
        kind,
        action_id=f"stale-{kind.value}",
        artifact_sha256="b" * 64,
    )
    managed.begin(stale)
    wait(managed)

    assert receipt_status(managed, stale.action_id) == MutationStatus.FAILED
    assert managed.residencies[0].state == ResidencyState.READY
    assert managed.residencies[0].artifact_sha256 == "a" * 64
    assert "stale" in managed.acknowledgements()[-1]["message"]


def test_warm_receipt_reports_monotonic_duration_and_replays_it(tmp_path):
    backend = FakeBackend()
    backend.start_gate = threading.Event()
    monotonic = Clock(10)
    managed = runtime(
        tmp_path,
        backend=backend,
        monotonic_clock=monotonic,
    )
    command = action(ActionKind.WARM)

    managed.begin(command)
    assert backend.spawned.wait(1)
    monotonic.value = 17.5
    backend.start_gate.set()
    wait(managed)
    acknowledgement = managed.acknowledgements()[0]

    assert acknowledgement["status"] == "succeeded"
    assert acknowledgement["duration_seconds"] == 7.5
    managed.mark_acknowledged([acknowledgement])
    assert managed.begin(command).duration_seconds == 7.5
    assert managed.acknowledgements()[0]["duration_seconds"] == 7.5


def test_uncached_load_fails_without_downloading_and_tracks_failure(tmp_path):
    managed = runtime(tmp_path, backend=FakeBackend(cached=()))
    command = action(ActionKind.LOAD)
    managed.begin(command)
    wait(managed)
    assert receipt_status(managed, command.action_id) == MutationStatus.FAILED
    residency = managed.residencies[0]
    assert residency.state == ResidencyState.FAILED
    assert residency.load_failures == 1
    assert "not cached" in managed.acknowledgements()[0]["message"]


def test_reject_persists_an_idempotent_failed_receipt_without_launching(tmp_path):
    backend = FakeBackend()
    managed = runtime(tmp_path, backend=backend)
    command = action(ActionKind.WARM, action_id="capacity-reject")

    rejected = managed.reject(command, "insufficient refreshed host memory")

    assert rejected.status == MutationStatus.FAILED
    assert rejected.message == "insufficient refreshed host memory"
    assert backend.starts == []
    managed.mark_acknowledged(managed.acknowledgements())
    assert managed.acknowledgements() == []
    assert managed.reject(command, "a later message is ignored") == rejected
    assert managed.acknowledgements()[0]["status"] == "failed"

    restored = ManagedModelRuntime(
        managed.state_path,
        host_id="host-a",
        backend=backend,
        clock=Clock(200),
        port_available=lambda _port: True,
    )
    assert restored.reject(command, "still ignored").message == rejected.message
    with pytest.raises(ValueError, match="target"):
        restored.reject(
            action(ActionKind.WARM, action_id="wrong-host", host_id="host-b"),
            "no capacity",
        )


def test_begin_rolls_back_running_state_when_initial_persistence_fails(
    monkeypatch,
    tmp_path,
):
    backend = FakeBackend()
    managed = runtime(tmp_path, backend=backend)
    original_save = managed._save_locked

    monkeypatch.setattr(
        managed,
        "_save_locked",
        lambda: (_ for _ in ()).throw(OSError("state disk full")),
    )
    command = action(ActionKind.WARM, action_id="retry-after-save")
    with pytest.raises(OSError, match="state disk full"):
        managed.begin(command)

    assert not managed.busy
    assert managed.acknowledgements() == []
    assert backend.starts == []

    monkeypatch.setattr(managed, "_save_locked", original_save)
    assert managed.begin(command).status == MutationStatus.RUNNING
    wait(managed)
    assert receipt_status(managed, command.action_id) == MutationStatus.SUCCEEDED


def test_begin_never_launches_after_a_post_replace_durability_failure(
    monkeypatch,
    tmp_path,
):
    backend = FakeBackend()
    managed = runtime(tmp_path, backend=backend)
    original_write = runtime_module.jsonio.atomic_write_json
    writes = 0

    def commit_then_fail_directory_fsync(path, payload, mode=0o600):
        nonlocal writes
        writes += 1
        original_write(path, payload, mode=mode)
        if writes == 1:
            raise runtime_module.jsonio.AtomicWriteCommittedError(
                Path(path),
                OSError("directory fsync failed"),
            )

    monkeypatch.setattr(
        runtime_module.jsonio,
        "atomic_write_json",
        commit_then_fail_directory_fsync,
    )
    command = action(ActionKind.WARM, action_id="durability-fence")

    receipt = managed.begin(command)

    assert receipt is not None
    assert receipt.status == MutationStatus.FAILED
    assert "durability barrier failed" in receipt.message
    assert not managed.busy
    assert backend.starts == []
    persisted = json.loads(managed.state_path.read_text())
    assert persisted["receipts"][-1]["status"] == "failed"

    restored = ManagedModelRuntime(
        managed.state_path,
        host_id="host-a",
        backend=backend,
        clock=Clock(200),
        port_available=lambda _port: True,
    )
    assert restored.acknowledgements()[0]["status"] == "failed"
    assert backend.starts == []


def test_begin_terminalizes_receipt_when_worker_thread_cannot_start(
    monkeypatch,
    tmp_path,
):
    managed = runtime(tmp_path)

    class BrokenThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("thread quota exhausted")

    monkeypatch.setattr(runtime_module.threading, "Thread", BrokenThread)
    command = action(ActionKind.WARM, action_id="thread-start-failure")
    with pytest.raises(RuntimeError, match="thread quota exhausted"):
        managed.begin(command)

    assert not managed.busy
    acknowledgement = managed.acknowledgements()
    assert acknowledgement[0]["action_id"] == command.action_id
    assert acknowledgement[0]["status"] == "failed"
    assert "could not start" in acknowledgement[0]["message"]


def test_receipt_eviction_uses_durable_sequence_and_never_drops_unreported(
    tmp_path,
):
    clock = Clock(10_000)
    managed = runtime(tmp_path, clock=clock)
    generation = "0000000000100-plan"
    for index in range(runtime_module.MAX_RECEIPTS):
        receipt = managed.begin(
            action(
                ActionKind.WARM,
                action_id=f"old-{index}",
                host_id="another-host",
                generation=generation,
            )
        )
        assert receipt is not None
        managed.mark_acknowledged(managed.acknowledgements())

    clock.value = 1
    newest = managed.begin(
        action(
            ActionKind.WARM,
            action_id="clock-rollback-new",
            host_id="another-host",
            generation=generation,
        )
    )
    assert newest is not None
    assert [row["action_id"] for row in managed.acknowledgements()] == [
        "clock-rollback-new"
    ]
    persisted = json.loads((tmp_path / "runtime.json").read_text())
    assert len(persisted["receipts"]) == runtime_module.MAX_RECEIPTS
    assert persisted["receipts"][-1]["action_id"] == "clock-rollback-new"
    assert persisted["receipts"][-1]["sequence"] > persisted["receipts"][0]["sequence"]

    restored = ManagedModelRuntime(
        tmp_path / "runtime.json",
        host_id="host-a",
        backend=FakeBackend(),
        clock=Clock(0),
    )
    assert restored.acknowledgements()[0]["action_id"] == "clock-rollback-new"


def test_unreported_terminal_receipts_may_temporarily_exceed_cache_bound(tmp_path):
    managed = runtime(tmp_path)
    for index in range(runtime_module.MAX_RECEIPTS + 1):
        managed.begin(
            action(
                ActionKind.WARM,
                action_id=f"unreported-{index}",
                host_id="another-host",
            )
        )
    assert len(managed.acknowledgements()) == runtime_module.MAX_RECEIPTS + 1


def test_warm_waits_for_readiness_then_advertises_ready_endpoint(tmp_path):
    backend = FakeBackend()
    managed = runtime(tmp_path, backend=backend)
    command = action(ActionKind.WARM)
    managed.begin(command)
    wait(managed)
    residency = managed.residencies[0]
    assert residency.state == ResidencyState.READY
    assert residency.handle is not None
    assert managed.endpoint_for("qwen.gguf", host="10.0.0.5") == (
        f"http://10.0.0.5:{residency.handle.port}/v1"
    )
    assert managed.endpoint_for(
        "qwen.gguf",
        host="[fe80::1234%25en0]",
    ) == f"http://[fe80::1234%25en0]:{residency.handle.port}/v1"
    assert managed.allocator_envelope()["residencies"][0]["state"] == "ready"
    assert backend.starts == [("qwen.gguf", residency.handle.port)]
    assert managed.active_requests("qwen.gguf") is None


def test_record_model_used_persists_a_monotonic_cooldown_watermark(tmp_path):
    backend = FakeBackend()
    managed = runtime(tmp_path, backend=backend)
    managed.begin(action(ActionKind.WARM))
    wait(managed)

    assert managed.record_model_used("qwen.gguf", 175.0)
    assert not managed.record_model_used("qwen.gguf", 150.0)
    assert not managed.record_model_used("unknown.gguf", 200.0)
    assert managed.residencies[0].last_used_at == 175.0

    restored = ManagedModelRuntime(
        managed.state_path,
        host_id="host-a",
        backend=backend,
        clock=Clock(200),
        port_available=lambda _port: True,
    )
    assert restored.residencies[0].last_used_at == 175.0
    for invalid in (-1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="finite and non-negative"):
            restored.record_model_used("qwen.gguf", invalid)


def test_allocator_envelope_reports_model_local_ages_from_the_same_clock(tmp_path):
    backend = FakeBackend()
    clock = Clock(100)
    managed = runtime(tmp_path, backend=backend, clock=clock)
    managed.begin(action(ActionKind.WARM))
    wait(managed)
    assert managed.record_model_used("qwen.gguf", 120)
    clock.value = 130

    row = managed.allocator_envelope()["residencies"][0]
    assert row["loaded_age_seconds"] == 30
    assert row["last_used_age_seconds"] == 10


def test_warm_persists_spawned_handle_before_readiness_wait(tmp_path):
    backend = FakeBackend()
    backend.start_gate = threading.Event()
    managed = runtime(tmp_path, backend=backend)

    managed.begin(action(ActionKind.WARM))
    assert backend.spawned.wait(1)

    payload = json.loads(managed.state_path.read_text(encoding="utf-8"))
    row = payload["residencies"][0]
    assert row["state"] == "warming"
    assert row["handle"]["pid"] in backend.live
    assert row["handle"]["process_birth_marker"].startswith("fake:")
    assert managed.reconcile_process_health() is False
    assert managed.residencies[0].state == ResidencyState.WARMING
    assert managed.endpoint_for("qwen.gguf") is None

    backend.start_gate.set()
    wait(managed)


def test_spawn_failure_retains_live_handle_for_restart_adoption(tmp_path):
    backend = FakeBackend()
    backend.raise_after_spawn = True
    managed = runtime(tmp_path, backend=backend)

    managed.begin(action(ActionKind.WARM))
    wait(managed)

    failed = managed.residencies[0]
    assert failed.state == ResidencyState.FAILED
    assert failed.handle is not None
    assert failed.handle.pid in backend.live

    backend.raise_after_spawn = False
    restored = ManagedModelRuntime(
        managed.state_path,
        host_id="host-a",
        backend=backend,
        clock=Clock(200),
        port_available=lambda _port: True,
    )
    assert restored.residencies[0].state == ResidencyState.READY
    assert restored.residencies[0].handle == failed.handle


def test_health_reconciliation_fences_then_recovers_transiently_unready_child(tmp_path):
    backend = FakeBackend()
    managed = runtime(tmp_path, backend=backend)
    managed.begin(action(ActionKind.WARM, action_id="warm"))
    wait(managed)
    handle = managed.residencies[0].handle
    assert handle is not None

    backend.unready.add(handle.pid)
    assert managed.reconcile_process_health()
    assert managed.residencies[0].state == ResidencyState.FAILED
    assert managed.residencies[0].handle == handle
    assert managed.endpoint_for("qwen.gguf") is None

    backend.unready.clear()
    assert managed.reconcile_process_health()
    assert managed.residencies[0].state == ResidencyState.READY
    assert managed.residencies[0].handle == handle


def test_warm_replaces_proven_owned_idle_failed_process(tmp_path):
    observed_fences: list[tuple[str, int]] = []

    class IdleObservableBackend(FakeBackend):
        def active_requests(self, handle, model_id):
            row = json.loads((tmp_path / "runtime.json").read_text())["residencies"][0]
            observed_fences.append((row["state"], row["handle"]["pid"]))
            return 0 if self.owns(handle, model_id) else None

    backend = IdleObservableBackend()
    managed = runtime(tmp_path, backend=backend)
    managed.begin(action(ActionKind.WARM, action_id="initial"))
    wait(managed)
    first = managed.residencies[0].handle
    assert first is not None

    backend.unready.add(first.pid)
    assert managed.reconcile_process_health()
    assert managed.residencies[0].state == ResidencyState.FAILED
    assert managed.residencies[0].handle == first

    managed.begin(
        action(
            ActionKind.WARM,
            action_id="recover",
            generation="0000000000200-plan",
        )
    )
    wait(managed)

    replacement = managed.residencies[0]
    assert receipt_status(managed, "recover") == MutationStatus.SUCCEEDED
    assert replacement.state == ResidencyState.READY
    assert replacement.handle is not None
    assert replacement.handle != first
    assert observed_fences == [("warming", first.pid)]
    assert backend.stops == [(first.pid, "qwen.gguf")]
    assert len(backend.starts) == 2
    assert set(backend.live) == {replacement.handle.pid}


@pytest.mark.parametrize("activity", [None, 1])
def test_warm_keeps_failed_process_when_direct_activity_is_not_proven_idle(
    activity,
    tmp_path,
):
    class ActivityBackend(FakeBackend):
        def active_requests(self, _handle, _model_id):
            return activity

    backend = ActivityBackend()
    managed = runtime(tmp_path, backend=backend)
    managed.begin(action(ActionKind.WARM, action_id="initial"))
    wait(managed)
    first = managed.residencies[0].handle
    assert first is not None

    backend.unready.add(first.pid)
    assert managed.reconcile_process_health()
    managed.begin(
        action(
            ActionKind.WARM,
            action_id="blocked-recovery",
            generation="0000000000200-plan",
        )
    )
    wait(managed)

    assert receipt_status(managed, "blocked-recovery") == MutationStatus.FAILED
    assert managed.residencies[0].state == ResidencyState.FAILED
    assert managed.residencies[0].handle == first
    assert backend.stops == []
    assert len(backend.starts) == 1
    assert first.pid in backend.live


def test_warm_keeps_failed_process_when_direct_activity_cannot_be_observed(tmp_path):
    backend = FakeBackend()
    managed = runtime(tmp_path, backend=backend)
    managed.begin(action(ActionKind.WARM, action_id="initial"))
    wait(managed)
    first = managed.residencies[0].handle
    assert first is not None

    backend.unready.add(first.pid)
    assert managed.reconcile_process_health()
    managed.begin(
        action(
            ActionKind.WARM,
            action_id="unobservable-recovery",
            generation="0000000000200-plan",
        )
    )
    wait(managed)

    assert receipt_status(managed, "unobservable-recovery") == MutationStatus.FAILED
    assert managed.residencies[0].state == ResidencyState.FAILED
    assert managed.residencies[0].handle == first
    assert backend.stops == []
    assert len(backend.starts) == 1


def test_health_reconciliation_releases_only_confirmed_dead_handle(tmp_path):
    backend = FakeBackend()
    managed = runtime(tmp_path, backend=backend)
    managed.begin(action(ActionKind.WARM, action_id="warm"))
    wait(managed)
    first = managed.residencies[0].handle
    assert first is not None

    backend.live.pop(first.pid)
    assert managed.reconcile_process_health()
    assert managed.residencies[0].state == ResidencyState.FAILED
    assert managed.residencies[0].handle is None
    assert managed.endpoint_for("qwen.gguf") is None

    managed.begin(
        action(
            ActionKind.WARM, action_id="replacement", generation="0000000000200-plan"
        )
    )
    wait(managed)
    assert managed.residencies[0].state == ResidencyState.READY
    assert managed.residencies[0].handle != first
    assert len(backend.starts) == 2


def test_health_reconciliation_probes_in_parallel_outside_lock_and_honors_deadline(
    tmp_path,
):
    class BlockingBackend(FakeBackend):
        def __init__(self):
            super().__init__(tuple(f"m-{index}" for index in range(4)))
            self.entered = 0
            self.maximum = 0
            self.release = threading.Event()
            self.guard = threading.Lock()

        def ready(self, handle, model_id):
            with self.guard:
                self.entered += 1
                self.maximum = max(self.maximum, self.entered)
            self.release.wait(2)
            with self.guard:
                self.entered -= 1
            return super().ready(handle, model_id)

    backend = BlockingBackend()
    managed = runtime(tmp_path, backend=backend)
    with managed._lock:
        for index in range(4):
            model_id = f"m-{index}"
            handle = RuntimeHandle(20_000 + index, 18_081 + index)
            backend.live[handle.pid] = (model_id, handle.port)
            managed._residencies[model_id] = ManagedResidency(
                model_id,
                1_000,
                ResidencyState.READY,
                handle=handle,
            )

    result: dict[str, bool] = {}
    worker = threading.Thread(
        target=lambda: result.setdefault(
            "changed",
            managed.reconcile_process_health(
                deadline=time.monotonic() + 0.2,
                max_workers=4,
            ),
        )
    )
    worker.start()
    deadline = time.monotonic() + 1
    while backend.maximum < 4 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert backend.maximum == 4

    lock_probe_started = time.monotonic()
    assert len(managed.residencies) == 4
    assert time.monotonic() - lock_probe_started < 0.05

    worker.join(1)
    assert not worker.is_alive()
    assert result == {"changed": True}
    assert {item.state for item in managed.residencies} == {ResidencyState.FAILED}
    backend.release.set()


def test_restart_health_recovery_parallelizes_many_children(tmp_path):
    class BlockingRecoveryBackend(FakeBackend):
        def __init__(self):
            super().__init__(tuple(f"m-{index}" for index in range(4)))
            self.arrived = threading.Barrier(5)

        def ready(self, handle, model_id):
            self.arrived.wait(timeout=1)
            return super().ready(handle, model_id)

    backend = BlockingRecoveryBackend()
    rows = []
    for index in range(4):
        model_id = f"m-{index}"
        handle = RuntimeHandle(30_000 + index, 18_081 + index)
        backend.live[handle.pid] = (model_id, handle.port)
        rows.append(
            ManagedResidency(
                model_id,
                1_000,
                ResidencyState.READY,
                handle=handle,
            ).to_dict()
        )
    jsonio = runtime_module.jsonio
    jsonio.atomic_write_json(
        tmp_path / "runtime.json",
        {
            "schema_version": 1,
            "host_id": "host-a",
            "residencies": rows,
            "receipts": [],
        },
    )

    restored: list[ManagedModelRuntime] = []
    worker = threading.Thread(
        target=lambda: restored.append(
            ManagedModelRuntime(
                tmp_path / "runtime.json",
                host_id="host-a",
                backend=backend,
                port_available=lambda _port: True,
            )
        )
    )
    worker.start()
    backend.arrived.wait(timeout=1)
    worker.join(2)
    assert not worker.is_alive()
    assert {item.state for item in restored[0].residencies} == {ResidencyState.READY}


def test_health_deadline_fences_running_probe_but_preserves_never_started_queue(
    tmp_path,
):
    class OneBlockingBackend(FakeBackend):
        def __init__(self):
            super().__init__(("first", "queued"))
            self.entered = threading.Event()
            self.release = threading.Event()

        def ready(self, handle, model_id):
            self.entered.set()
            self.release.wait(2)
            return super().ready(handle, model_id)

    backend = OneBlockingBackend()
    managed = runtime(tmp_path, backend=backend)
    with managed._lock:
        for index, model_id in enumerate(("first", "queued")):
            handle = RuntimeHandle(40_000 + index, 18_081 + index)
            backend.live[handle.pid] = (model_id, handle.port)
            managed._residencies[model_id] = ManagedResidency(
                model_id,
                1_000,
                ResidencyState.READY,
                handle=handle,
            )

    assert managed.reconcile_process_health(
        deadline=time.monotonic() + 0.1,
        max_workers=1,
    )
    states = {item.model_id: item.state for item in managed.residencies}
    assert states == {
        "first": ResidencyState.FAILED,
        "queued": ResidencyState.READY,
    }
    backend.release.set()


def test_ambiguous_live_handle_is_retained_and_blocks_duplicate(tmp_path):
    backend = FakeBackend()
    managed = runtime(tmp_path, backend=backend)
    managed.begin(action(ActionKind.WARM, action_id="warm"))
    wait(managed)
    handle = managed.residencies[0].handle
    assert handle is not None

    backend.unowned.add(handle.pid)
    managed.reconcile_process_health()
    assert managed.residencies[0].state == ResidencyState.FAILED
    assert managed.residencies[0].handle == handle

    managed.begin(
        action(ActionKind.WARM, action_id="blocked", generation="0000000000200-plan")
    )
    wait(managed)
    assert receipt_status(managed, "blocked") == MutationStatus.FAILED
    assert len(backend.starts) == 1


def test_duplicate_warm_is_idempotent_across_redelivery(tmp_path):
    backend = FakeBackend()
    managed = runtime(tmp_path, backend=backend)
    command = action(ActionKind.WARM)
    managed.begin(command)
    wait(managed)
    managed.begin(command)
    wait(managed)
    assert len(backend.starts) == 1


def test_dependencies_and_single_mutation_gate_parallel_commands(tmp_path):
    backend = FakeBackend()
    gate = threading.Event()
    backend.start_gate = gate
    managed = runtime(tmp_path, backend=backend)
    warm = action(ActionKind.WARM, action_id="warm")
    assert managed.begin(warm).status == MutationStatus.RUNNING
    assert managed.busy
    assert (
        managed.begin(action(ActionKind.LOAD, action_id="other", model_id="other.gguf"))
        is None
    )
    dependent = action(ActionKind.DRAIN, action_id="dependent", dependencies=("warm",))
    assert managed.begin(dependent) is None
    gate.set()
    wait(managed)
    assert managed.begin(dependent).status == MutationStatus.RUNNING
    wait(managed)
    assert managed.residencies[0].state == ResidencyState.DRAINING


def test_drain_removes_admission_then_unload_stops_owned_process(tmp_path):
    backend = FakeBackend()
    managed = runtime(tmp_path, backend=backend)
    warm = action(ActionKind.WARM, action_id="warm")
    managed.begin(warm)
    wait(managed)
    handle = managed.residencies[0].handle
    assert handle is not None

    drain = action(ActionKind.DRAIN, action_id="drain", generation="0000000000200-plan")
    managed.begin(drain)
    wait(managed)
    assert managed.residencies[0].state == ResidencyState.DRAINING

    unload = action(
        ActionKind.UNLOAD, action_id="unload", generation="0000000000300-plan"
    )
    managed.begin(unload)
    wait(managed)
    assert managed.residencies[0].state == ResidencyState.CACHED
    assert managed.residencies[0].handle is None
    assert backend.stops == [(handle.pid, "qwen.gguf")]


def test_demand_rebound_readmits_draining_process_without_starting_duplicate(tmp_path):
    backend = FakeBackend()
    managed = runtime(tmp_path, backend=backend)
    managed.begin(action(ActionKind.WARM, action_id="warm"))
    wait(managed)
    handle = managed.residencies[0].handle
    managed.begin(
        action(ActionKind.DRAIN, action_id="drain", generation="0000000000200-plan")
    )
    wait(managed)
    assert managed.residencies[0].state == ResidencyState.DRAINING

    managed.begin(
        action(ActionKind.WARM, action_id="rewarm", generation="0000000000300-plan")
    )
    wait(managed)
    assert managed.residencies[0].state == ResidencyState.READY
    assert managed.residencies[0].handle == handle
    assert len(backend.starts) == 1


def test_unload_refuses_to_skip_drain(tmp_path):
    backend = FakeBackend()
    managed = runtime(tmp_path, backend=backend)
    managed.begin(action(ActionKind.WARM, action_id="warm"))
    wait(managed)
    unload = action(
        ActionKind.UNLOAD, action_id="unload", generation="0000000000200-plan"
    )
    managed.begin(unload)
    wait(managed)
    assert receipt_status(managed, "unload") == MutationStatus.FAILED
    assert managed.residencies[0].state == ResidencyState.READY
    assert backend.stops == []


def test_older_generation_is_cancelled_without_side_effect(tmp_path):
    backend = FakeBackend()
    managed = runtime(tmp_path, backend=backend)
    newest = action(ActionKind.LOAD, action_id="new", generation="0000000000300-new")
    managed.begin(newest)
    wait(managed)
    stale = action(ActionKind.WARM, action_id="stale", generation="0000000000200-old")
    receipt = managed.begin(stale)
    assert receipt and receipt.status == MutationStatus.CANCELLED
    assert backend.starts == []


def test_controller_epoch_fences_lower_sequences_and_superseded_epochs(tmp_path):
    backend = FakeBackend()
    managed = runtime(tmp_path, backend=backend)
    epoch_a = "a" * 32
    epoch_b = "b" * 32
    generation_a2 = f"{epoch_a}:00000000000000000002:{'1' * 12}"
    generation_a1 = f"{epoch_a}:00000000000000000001:{'2' * 12}"
    generation_b1 = f"{epoch_b}:00000000000000000001:{'3' * 12}"

    managed.begin(action(ActionKind.LOAD, action_id="a2", generation=generation_a2))
    wait(managed)
    stale = managed.begin(
        action(ActionKind.WARM, action_id="a1", generation=generation_a1)
    )
    assert stale and stale.status == MutationStatus.CANCELLED

    switched = managed.begin(
        action(ActionKind.LOAD, action_id="b1", generation=generation_b1)
    )
    assert switched and switched.status == MutationStatus.RUNNING
    wait(managed)

    restored = ManagedModelRuntime(
        tmp_path / "runtime.json",
        host_id="host-a",
        backend=backend,
        clock=Clock(200),
        port_available=lambda _port: True,
    )
    delayed_old_epoch = restored.begin(
        action(
            ActionKind.WARM,
            action_id="a3-delayed",
            generation=f"{epoch_a}:00000000000000000003:{'4' * 12}",
        )
    )
    assert delayed_old_epoch and delayed_old_epoch.status == MutationStatus.CANCELLED
    assert backend.starts == []


def test_controller_terms_fence_conflicts_takeover_and_reordered_delivery(tmp_path):
    backend = FakeBackend()
    managed = runtime(tmp_path, backend=backend)
    leader_a = action(
        ActionKind.LOAD,
        action_id="leader-a",
        controller_term=7,
        controller_id="leader-a",
    )
    managed.begin(leader_a)
    wait(managed)

    conflict = managed.begin(
        action(
            ActionKind.WARM,
            action_id="conflict",
            controller_term=7,
            controller_id="leader-b",
        )
    )
    assert conflict and conflict.status == MutationStatus.CANCELLED

    takeover = managed.begin(
        action(
            ActionKind.LOAD,
            action_id="takeover",
            generation=f"{'b' * 32}:00000000000000000001:{'1' * 12}",
            controller_term=8,
            controller_id="leader-b",
        )
    )
    assert takeover and takeover.status == MutationStatus.RUNNING
    wait(managed)

    restored = ManagedModelRuntime(
        tmp_path / "runtime.json",
        host_id="host-a",
        backend=backend,
        clock=Clock(200),
        port_available=lambda _port: True,
    )
    delayed = restored.begin(
        action(
            ActionKind.WARM,
            action_id="delayed-a",
            controller_term=7,
            controller_id="leader-a",
        )
    )
    assert delayed and delayed.status == MutationStatus.CANCELLED
    assert delayed.message == "stale allocator controller term"


def test_expired_controller_lease_is_cancelled_before_side_effect(tmp_path):
    backend = FakeBackend()
    managed = runtime(tmp_path, backend=backend, clock=Clock(100))
    receipt = managed.begin(
        action(
            ActionKind.LOAD,
            action_id="expired",
            controller_term=2,
            controller_id="leader-a",
            controller_lease_expires_at=100,
        )
    )
    assert receipt and receipt.status == MutationStatus.CANCELLED
    assert receipt.message == "allocator controller lease expired"
    assert managed.residencies == ()
    assert backend.starts == []


def test_delayed_old_term_cannot_overwrite_refenced_command_receipt(tmp_path):
    managed = runtime(tmp_path)
    original = action(
        ActionKind.LOAD,
        action_id="stable-command",
        controller_term=3,
        controller_id="leader-a",
    )
    managed.begin(original)
    wait(managed)
    succeeded = next(
        item for item in managed.acknowledgements() if item["action_id"] == original.action_id
    )
    assert succeeded["status"] == "succeeded"

    refenced = replace(original, controller_term=4, controller_id="leader-b")
    assert managed.begin(refenced).status == MutationStatus.SUCCEEDED
    delayed = managed.begin(original)
    assert delayed.status == MutationStatus.SUCCEEDED


def test_wrong_host_and_non_executable_recommendation_are_cancelled(tmp_path):
    managed = runtime(tmp_path)
    wrong = action(ActionKind.LOAD, action_id="wrong", host_id="host-b")
    assert managed.begin(wrong).status == MutationStatus.CANCELLED
    recommendation = replace(
        action(ActionKind.LOAD, action_id="recommend"), executable=False
    )
    assert managed.begin(recommendation).status == MutationStatus.CANCELLED
    assert managed.residencies == ()


def test_local_protection_outranks_global_warm_command(tmp_path):
    signals = HostSignals(timestamp=100, network_available=False)
    collector = StaticCollector(signals)
    loop = LocalHostProtectionLoop(
        HostPolicy(
            require_network=True,
            pause_for_user_activity=False,
            recovery_cooldown_seconds=0,
        )
    )
    backend = FakeBackend()
    managed = runtime(
        tmp_path,
        backend=backend,
        signal_collector=collector,
        protection_loop=loop,
    )
    decision = managed.evaluate_host()
    assert decision.state.value == "unhealthy"
    receipt = managed.begin(action(ActionKind.WARM))
    assert receipt and receipt.status == MutationStatus.CANCELLED
    assert backend.starts == []
    assert managed.allocator_envelope()["state"] == "unhealthy"


def test_runtime_reloads_durable_local_override_and_absence_resumes(tmp_path):
    collector = StaticCollector(HostSignals(timestamp=100, network_available=True))
    loop = LocalHostProtectionLoop(
        HostPolicy(
            pause_for_user_activity=False,
            recovery_cooldown_seconds=0,
            activity_recovery_seconds=0,
            thermal_recovery_seconds=0,
        )
    )
    managed = runtime(
        tmp_path,
        signal_collector=collector,
        protection_loop=loop,
    )
    assert managed.override_path == tmp_path / "runtime.override.json"

    path = write_local_override(managed.state_path, LocalOverride.pause("bench work"))
    assert path == local_override_path(managed.state_path)
    assert managed.evaluate_host().state == NodeState.PAUSED

    collector.signals = HostSignals(timestamp=101, network_available=True)
    write_local_override(managed.state_path, LocalOverride.drain("deploy"))
    draining = managed.evaluate_host()
    assert draining.state == NodeState.DRAINING
    assert "local_override:draining:deploy" in draining.reasons

    clear_local_override(managed.state_path)
    collector.signals = HostSignals(timestamp=102, network_available=True)
    assert managed.evaluate_host().state == NodeState.ACCEPTING


def test_malformed_local_override_file_fails_closed_to_quarantine(tmp_path):
    collector = StaticCollector(HostSignals(timestamp=100, network_available=True))
    managed = runtime(tmp_path, signal_collector=collector)
    managed.override_path.write_text("{not-json", encoding="utf-8")

    decision = managed.evaluate_host()

    assert decision.state == NodeState.QUARANTINED
    assert decision.accept is False
    assert "local_override:quarantined:invalid_local_override_file" in decision.reasons
    assert managed.override_error


def test_restart_adopts_only_backend_owned_ready_process(tmp_path):
    backend = FakeBackend()
    managed = runtime(tmp_path, backend=backend)
    managed.begin(action(ActionKind.WARM))
    wait(managed)
    handle = managed.residencies[0].handle
    assert handle is not None

    restored = ManagedModelRuntime(
        tmp_path / "runtime.json",
        host_id="host-a",
        backend=backend,
        clock=Clock(200),
    )
    assert restored.residencies[0].state == ResidencyState.READY
    assert restored.residencies[0].handle == handle

    backend.live.clear()
    recovered_dead = ManagedModelRuntime(
        tmp_path / "runtime.json",
        host_id="host-a",
        backend=backend,
        clock=Clock(300),
    )
    assert recovered_dead.residencies[0].state == ResidencyState.CACHED
    assert recovered_dead.residencies[0].handle is None


def test_restart_turns_interrupted_action_into_failed_receipt(tmp_path):
    state_path = tmp_path / "runtime.json"
    payload = {
        "schema_version": 1,
        "host_id": "host-a",
        "latest_plan_generation": "0000000000100-plan",
        "local_state": {"schema_version": 1},
        "residencies": [],
        "receipts": [
            {
                "action_id": "interrupted",
                "status": "running",
                "message": "started",
                "plan_generation": "0000000000100-plan",
                "updated_at": 100,
                "reported_status": "running",
            }
        ],
    }
    state_path.write_text(json.dumps(payload))
    managed = ManagedModelRuntime(
        state_path,
        host_id="host-a",
        backend=FakeBackend(),
        clock=Clock(200),
    )
    acknowledgement = managed.acknowledgements()[0]
    assert acknowledgement["action_id"] == "interrupted"
    assert acknowledgement["status"] == "failed"
    assert "restarted" in acknowledgement["message"]


def test_stop_all_releases_processes_and_preserves_cached_weights(tmp_path):
    backend = FakeBackend()
    managed = runtime(tmp_path, backend=backend)
    managed.begin(action(ActionKind.WARM))
    wait(managed)
    managed.stop_all()
    assert managed.residencies[0].state == ResidencyState.CACHED
    assert not backend.live


def test_stop_all_attempts_every_owned_child_before_reporting_persistence_errors(
    monkeypatch,
    tmp_path,
):
    backend = FakeBackend(cached=("qwen.gguf", "other.gguf"))
    managed = runtime(tmp_path, backend=backend)
    managed.begin(action(ActionKind.WARM, action_id="warm-qwen"))
    wait(managed)
    managed.begin(
        action(
            ActionKind.WARM,
            action_id="warm-other",
            model_id="other.gguf",
            generation="0000000000200-plan",
        )
    )
    wait(managed)
    original_save = managed._save_locked

    def fail_after_a_child_stops():
        if any(
            item.state == ResidencyState.CACHED and item.handle is None
            for item in managed.residencies
        ):
            raise OSError("state disk full")
        original_save()

    monkeypatch.setattr(managed, "_save_locked", fail_after_a_child_stops)

    with pytest.raises(
        RuntimeError, match="processes were cleaned up.*state disk full"
    ):
        managed.stop_all()

    assert not backend.live
    assert {model_id for _pid, model_id in backend.stops} == {
        "qwen.gguf",
        "other.gguf",
    }
    assert all(item.state == ResidencyState.CACHED for item in managed.residencies)


def test_begin_shutdown_marks_live_residencies_draining_before_process_stop(tmp_path):
    backend = FakeBackend()
    managed = runtime(tmp_path, backend=backend)
    managed.begin(action(ActionKind.WARM))
    wait(managed)
    managed.begin_shutdown()
    assert managed.shutting_down
    assert managed.residencies[0].state == ResidencyState.DRAINING
    assert backend.live
    envelope = managed.allocator_envelope()
    assert envelope["state"] == "draining"
    assert envelope["decision"] is None
    managed.stop_all()
    assert not backend.live


def test_stop_all_cancels_inflight_warm_without_orphaning_process(tmp_path):
    backend = FakeBackend()
    backend.start_gate = threading.Event()
    managed = runtime(tmp_path, backend=backend)
    managed.begin(action(ActionKind.WARM))
    managed.stop_all()
    wait(managed)
    assert not backend.live
    assert managed.residencies[0].state == ResidencyState.FAILED


def test_stop_all_waits_for_direct_requests_and_force_can_bypass_unknown(tmp_path):
    class ActivityBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.activity: list[int | None] = [1, 0]
            self.activity_checks = 0

        def active_requests(self, _handle, _model_id):
            self.activity_checks += 1
            if len(self.activity) > 1:
                return self.activity.pop(0)
            return self.activity[0]

        def stop_with_timeout(self, handle, model_id, *, timeout):
            assert timeout >= 0
            self.stop(handle, model_id)

    backend = ActivityBackend()
    managed = runtime(tmp_path, backend=backend)
    managed.begin(action(ActionKind.WARM))
    wait(managed)

    managed.stop_all(wait_timeout=1.0)

    assert backend.activity_checks >= 2
    assert not backend.live

    unknown_backend = ActivityBackend()
    unknown_backend.activity = [None]
    unknown = ManagedModelRuntime(
        tmp_path / "unknown.json",
        host_id="host-a",
        backend=unknown_backend,
        clock=Clock(),
        port_available=lambda _port: True,
    )
    unknown.begin(action(ActionKind.WARM))
    wait(unknown)
    with pytest.raises(RuntimeError, match="activity is unknown"):
        unknown.stop_all(wait_timeout=0.1)
    assert unknown_backend.live

    unknown.stop_all(wait_timeout=0, force=True)
    assert not unknown_backend.live


def test_stop_all_reports_arbitrary_and_missing_worker_outcomes(monkeypatch, tmp_path):
    class WorkerFailureBackend(FakeBackend):
        def __init__(self):
            super().__init__(cached=("qwen.gguf", "other.gguf"))
            self.attempted: set[str] = set()

        def stop_with_timeout(self, _handle, model_id, *, timeout):
            assert timeout >= 0
            self.attempted.add(model_id)
            if model_id == "qwen.gguf":
                raise ValueError("unexpected backend failure")
            raise SystemExit("worker aborted before recording an outcome")

    # SystemExit intentionally crosses the worker's Exception boundary to exercise the missing-
    # outcome guard without emitting an irrelevant unhandled-thread warning in the test runner.
    monkeypatch.setattr(threading, "excepthook", lambda _args: None)
    backend = WorkerFailureBackend()
    managed = runtime(tmp_path, backend=backend)
    managed.begin(action(ActionKind.WARM, action_id="warm-qwen"))
    wait(managed)
    managed.begin(
        action(
            ActionKind.WARM,
            action_id="warm-other",
            model_id="other.gguf",
            generation="0000000000200-plan",
        )
    )
    wait(managed)

    with pytest.raises(RuntimeError) as raised:
        managed.stop_all(wait_timeout=0.5)

    message = str(raised.value)
    assert "ValueError: unexpected backend failure" in message
    assert "process stop worker ended without an outcome" in message
    assert backend.attempted == {"qwen.gguf", "other.gguf"}


@pytest.mark.parametrize("activity", [None, 1])
def test_unload_refuses_unknown_or_active_direct_engine_requests(
    activity,
    tmp_path,
):
    class ActivityBackend(FakeBackend):
        def active_requests(self, _handle, _model_id):
            return activity

    backend = ActivityBackend()
    managed = runtime(tmp_path, backend=backend)
    managed.begin(action(ActionKind.WARM, action_id="warm"))
    wait(managed)
    managed.begin(
        action(ActionKind.DRAIN, action_id="drain", generation="0000000000200-plan")
    )
    wait(managed)
    managed.begin(
        action(ActionKind.UNLOAD, action_id="unload", generation="0000000000300-plan")
    )
    wait(managed)

    assert receipt_status(managed, "unload") == MutationStatus.FAILED
    assert managed.residencies[0].state == ResidencyState.DRAINING
    assert backend.stops == []


def test_llama_backend_cancels_process_spawned_during_shutdown_race(monkeypatch, tmp_path):
    from shared.engine import launcher

    entered_spawn = threading.Event()
    release_spawn = threading.Event()
    stopped = threading.Event()

    class Process:
        pid = 44_321

        def __init__(self) -> None:
            self.dead = False

        def poll(self):
            return 0 if self.dead else None

    process = Process()
    launched = SimpleNamespace(proc=process, port=18_081)

    def start_llm(*_args, **kwargs):
        kwargs["on_spawn"](launched)
        entered_spawn.set()
        assert release_spawn.wait(1)
        return launched

    def stop(_launched):
        process.dead = True
        stopped.set()

    monkeypatch.setattr(launcher, "is_port_in_use", lambda _port, **_kwargs: False)
    monkeypatch.setattr(launcher, "assert_supported_build", lambda: None)
    monkeypatch.setattr(launcher, "start_llm", start_llm)
    monkeypatch.setattr(launcher, "wait_for_models", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(launcher, "stop", stop)
    backend = LlamaCppBackend()
    key_file = tmp_path / "engine.key"
    key_file.write_text("engine-secret\n")
    key_file.chmod(0o600)
    backend.configure_api_key_file(key_file)
    monkeypatch.setattr(backend, "cached_models", lambda: ("qwen.gguf",))
    errors: list[BaseException] = []

    def start() -> None:
        try:
            backend.start("qwen.gguf", 18_081)
        except BaseException as exc:  # noqa: BLE001 - SystemExit is part of the backend boundary
            errors.append(exc)

    thread = threading.Thread(target=start)
    thread.start()
    assert entered_spawn.wait(1)
    backend.cancel_pending()
    release_spawn.set()
    thread.join(1)

    assert not thread.is_alive()
    assert stopped.is_set()
    assert len(errors) == 1
    assert "shut down during model startup" in str(errors[0])


def test_llama_backend_publishes_birth_marker_before_readiness(monkeypatch, tmp_path):
    from shared.engine import launcher

    events: list[tuple[str, RuntimeHandle | None]] = []
    start_options: dict[str, object] = {}
    readiness_options: dict[str, object] = {}
    port_probe: dict[str, object] = {}

    class Process:
        pid = 51_234

        def poll(self):
            return None

    launched = SimpleNamespace(proc=Process(), port=18_081)

    def is_port_in_use(port, **kwargs):
        port_probe.update(port=port, **kwargs)
        return False

    monkeypatch.setattr(launcher, "is_port_in_use", is_port_in_use)
    monkeypatch.setattr(launcher, "assert_supported_build", lambda: None)

    def start_llm(*_args, **kwargs):
        start_options.update(kwargs)
        kwargs["on_spawn"](launched)
        return launched

    monkeypatch.setattr(launcher, "start_llm", start_llm)

    def wait_for_models(*_args, **kwargs):
        readiness_options.update(kwargs)
        events.append(("wait", None))

    monkeypatch.setattr(launcher, "wait_for_models", wait_for_models)
    monkeypatch.setattr(
        runtime_module, "_process_birth_marker", lambda _pid: "birth:51"
    )
    backend = LlamaCppBackend(bind_host="::", api_key="engine-secret")
    key_file = tmp_path / "engine.key"
    key_file.write_text("engine-secret\n")
    key_file.chmod(0o600)
    backend.configure_api_key_file(key_file)
    monkeypatch.setattr(backend, "cached_models", lambda: ("qwen.gguf",))

    handle = backend.start_with_callback(
        "qwen.gguf",
        18_081,
        lambda spawned: events.append(("spawn", spawned)),
    )

    assert [event for event, _value in events] == ["spawn", "spawn", "wait"]
    provisional = events[0][1]
    assert provisional is not None
    assert provisional.pid == handle.pid
    assert provisional.port == handle.port
    assert provisional.process_birth_marker == ""
    assert events[1] == ("spawn", handle)
    assert handle.process_birth_marker == "birth:51"
    assert start_options["host"] == "::"
    assert port_probe == {"port": 18_081, "host": "::1"}
    assert start_options["api_key_file"] == str(key_file.resolve())
    assert "api_key" not in start_options
    assert readiness_options["api_key"] == "engine-secret"
    assert LlamaCppBackend().bind_host == "0.0.0.0"
    with pytest.raises(ValueError, match="IPv4 or IPv6"):
        LlamaCppBackend(bind_host="https://not-a-bind-address")


def test_llama_backend_and_launcher_reject_exposed_tls_private_key(
    monkeypatch,
    tmp_path,
):
    if runtime_module.os.name == "nt":
        pytest.skip("POSIX mode-bit assertion")
    from shared.engine import launcher

    cert = tmp_path / "engine.pem"
    key = tmp_path / "engine.key"
    api_key = tmp_path / "api.key"
    cert.write_text("certificate")
    key.write_text("private key")
    api_key.write_text("engine-secret\n")
    key.chmod(0o644)
    api_key.chmod(0o600)

    with pytest.raises(ValueError, match="owner-only"):
        LlamaCppBackend(tls_cert_file=cert, tls_key_file=key)

    monkeypatch.setenv("GRID_HOME", str(tmp_path / "grid-home"))
    with pytest.raises(ValueError, match="owner-only"):
        launcher.start_llm(
            "qwen.gguf",
            port=18_081,
            api_key_file=api_key,
            tls_cert_file=cert,
            tls_key_file=key,
        )


def test_launcher_stops_child_when_immediate_publication_fails(monkeypatch, tmp_path):
    from shared import paths
    from shared.engine import launcher

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    model_path = paths.models_dir() / "qwen.gguf"
    model_path.parent.mkdir(parents=True)
    model_path.write_bytes(b"model")
    events: list[object] = []

    class Process:
        pid = 52_345

        def __init__(self) -> None:
            self.dead = False

        def poll(self):
            return 0 if self.dead else None

        def terminate(self):
            events.append("terminate")
            self.dead = True

        def wait(self, timeout):
            events.append(("wait", timeout))
            return 0

    process = Process()
    monkeypatch.setattr(launcher, "llama_server_path", lambda: "/opt/grid/llama-server")
    commands: list[list[str]] = []

    def popen(command, **_kwargs):
        commands.append(command)
        return process

    monkeypatch.setattr(launcher.subprocess, "Popen", popen)

    def fail_publication(_launched):
        events.append("publish")
        raise OSError("state disk full")

    key_file = tmp_path / "engine.key"
    key_file.write_text("engine-secret\n")
    key_file.chmod(0o600)
    with pytest.raises(OSError, match="state disk full"):
        launcher.start_llm(
            "qwen.gguf",
            port=18_081,
            alias="qwen.gguf",
            api_key_file=key_file,
            mmproj=None,
            on_spawn=fail_publication,
        )

    assert events == ["publish", "terminate", ("wait", 10.0)]
    assert "--slots" in commands[0]
    assert commands[0][commands[0].index("--api-key-file") + 1] == str(key_file.resolve())
    assert "engine-secret" not in commands[0]


def test_llama_backend_hashes_exact_cached_file_and_invalidates_changed_identity(
    monkeypatch,
    tmp_path,
):
    model_path = tmp_path / "qwen.gguf"
    model_path.write_bytes(b"first immutable model")
    monkeypatch.setattr(
        runtime_module.model_store,
        "list_all",
        lambda: (SimpleNamespace(name="qwen.gguf", path=model_path),),
    )
    backend = LlamaCppBackend()

    first = backend.artifact_sha256("qwen.gguf")
    assert first == runtime_module.hashlib.sha256(b"first immutable model").hexdigest()
    assert backend.artifact_sha256("qwen.gguf") == first

    model_path.write_bytes(b"second immutable model")
    second = backend.artifact_sha256("qwen.gguf")
    assert second == runtime_module.hashlib.sha256(b"second immutable model").hexdigest()
    assert second != first


def test_llama_backend_rejects_pid_reuse_and_requires_exact_argv(monkeypatch, tmp_path):
    model_path = tmp_path / "qwen.gguf"
    model_path.write_bytes(b"model")
    monkeypatch.setattr(
        runtime_module.model_store,
        "list_all",
        lambda: (SimpleNamespace(name="qwen.gguf", path=model_path),),
    )
    backend = LlamaCppBackend()
    handle = RuntimeHandle(
        61_234,
        18_081,
        "birth:original",
        executable_path="/opt/grid/llama-server",
        model_path=str(model_path),
    )
    exact = (
        "/opt/grid/llama-server",
        "-m",
        str(model_path),
        "--alias",
        "qwen.gguf",
        "--port",
        "18081",
    )
    marker = {"value": "birth:replacement"}
    argv = {"value": exact}
    monkeypatch.setattr(
        runtime_module,
        "_process_birth_marker",
        lambda _pid: marker["value"],
    )
    monkeypatch.setattr(runtime_module, "_process_argv", lambda _pid: argv["value"])

    assert backend.owns(handle, "qwen.gguf") is False

    marker["value"] = "birth:original"
    assert backend.owns(handle, "qwen.gguf") is True
    assert RuntimeHandle.from_dict(asdict(handle)) == handle
    old_handle = replace(handle, model_path="")
    assert backend.owns(old_handle, "qwen.gguf") is True

    # Persisted argv identity outlives the model-store entry, so cleanup can still prove and stop
    # the process after the GGUF itself has been deleted.
    model_path.unlink()
    monkeypatch.setattr(runtime_module.model_store, "list_all", lambda: ())
    assert backend.owns(handle, "qwen.gguf") is True

    # State written by older allocator versions has no model_path and retains its original live-
    # store fallback behavior.
    assert backend.owns(old_handle, "qwen.gguf") is False

    argv["value"] = (*exact, "--port", "18082")
    assert backend.owns(handle, "qwen.gguf") is False
    argv["value"] = (
        "/opt/grid/llama-server",
        "-m",
        str(tmp_path / "prefix-qwen.gguf"),
        "--alias",
        "qwen.gguf-other",
        "--port",
        "18081",
    )
    assert backend.owns(handle, "qwen.gguf") is False


def test_llama_backend_counts_direct_slots_without_environment_proxies(monkeypatch):
    backend = LlamaCppBackend(api_key="engine-secret")
    handle = RuntimeHandle(71_234, 18_081, "birth", "/opt/llama", "/models/qwen.gguf")
    monkeypatch.setattr(backend, "alive", lambda _handle: True)
    monkeypatch.setattr(backend, "owns", lambda _handle, _model_id: True)
    monkeypatch.setattr(backend, "_secure_launch_configuration", lambda _handle: True)
    observed: dict[str, object] = {}
    requests: list[tuple[str, dict[str, str] | None]] = []
    clients: list[tuple[float, bool]] = []

    class Response:
        status_code = 200

        def json(self):
            return [
                {"id": 0, "is_processing": True},
                {"id": 1, "is_processing": False},
                {"id": 2, "is_processing": True},
            ]

    class Client:
        def __init__(self, *, timeout, trust_env, verify):
            observed.update(timeout=timeout, trust_env=trust_env, verify=verify)
            clients.append((timeout, trust_env))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, url, *, headers=None):
            observed["url"] = url
            requests.append((url, headers))
            return Response()

    monkeypatch.setattr(runtime_module.httpx, "Client", Client)

    assert backend.active_requests(handle, "qwen.gguf") == 2
    assert backend.ready(handle, "qwen.gguf") is True
    assert observed == {
        "timeout": 2.0,
        "trust_env": False,
        "verify": True,
        "url": "http://127.0.0.1:18081/v1/models",
    }
    assert clients == [(1.0, False), (2.0, False)]
    assert requests == [
        (
            "http://127.0.0.1:18081/slots",
            {"Authorization": "Bearer engine-secret"},
        ),
        (
            "http://127.0.0.1:18081/v1/models",
            {"Authorization": "Bearer engine-secret"},
        ),
    ]


def test_launcher_readiness_probe_authenticates_without_environment_proxies(
    monkeypatch,
):
    from shared.engine import launcher

    observed: dict[str, object] = {}

    class Process:
        def poll(self):
            return None

    class Response:
        status_code = 200

    class Client:
        def __init__(self, *, timeout, trust_env, verify):
            observed.update(timeout=timeout, trust_env=trust_env, verify=verify)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, url, *, headers=None):
            observed.update(url=url, headers=headers)
            return Response()

    monkeypatch.setattr(launcher.httpx, "Client", Client)
    launched = launcher.LlamaProcess(
        proc=Process(),
        port=18_081,
        log=Path("unused.log"),
        host="::",
    )

    launcher.wait_for_models(launched, timeout=1.0, api_key="engine-secret")

    assert observed == {
        "timeout": 5.0,
        "trust_env": False,
        "verify": True,
        "url": "http://[::1]:18081/v1/models",
        "headers": {"Authorization": "Bearer engine-secret"},
    }


def test_allocator_port_probes_use_the_bind_address_family(monkeypatch, tmp_path):
    from shared.engine import launcher

    calls: list[tuple[str, int, tuple[str, int]]] = []

    class Socket:
        def __init__(self, family, socktype):
            assert socktype == runtime_module.socket.SOCK_STREAM
            self.family = family

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def settimeout(self, _timeout):
            return None

        def connect_ex(self, address):
            calls.append(("connect", self.family, address))
            return 0

        def bind(self, address):
            calls.append(("bind", self.family, address))

    monkeypatch.setattr(runtime_module.socket, "socket", Socket)

    assert launcher.is_port_in_use(18_081, host="::") is True
    assert runtime_module._port_available(18_082, bind_host="::") is True
    assert calls == [
        ("connect", runtime_module.socket.AF_INET6, ("::1", 18_081)),
        ("bind", runtime_module.socket.AF_INET6, ("::", 18_082)),
    ]

    selected: dict[str, object] = {}

    def available(port, *, bind_host):
        selected.update(port=port, bind_host=bind_host)
        return True

    monkeypatch.setattr(runtime_module, "_port_available", available)
    backend = FakeBackend()
    backend.bind_host = "::"
    managed = ManagedModelRuntime(
        tmp_path / "ipv6.json",
        backend=backend,
    )

    assert managed._port_available(18_083) is True
    assert selected == {"port": 18_083, "bind_host": "::"}
