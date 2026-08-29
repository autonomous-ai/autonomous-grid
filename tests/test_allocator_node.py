from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from local import server as server_module
from local.allocator_node import AllocatorNodeAgent, _allocator_resources
from local.server import create_app
from shared import jsonio
from shared.allocator.auth import engine_node_id, mint_node_token
from shared.allocator.local import HostPolicy, LocalHostProtectionLoop
from shared.allocator.models import ActionKind, MutationAction, ResidencyState
from shared.allocator.runtime import (
    ManagedModelRuntime,
    ManagedResidency,
    RuntimeHandle,
    shutdown_request_path,
)
from shared.system.hostsignals import HostSignals


class FakeBackend:
    def __init__(self) -> None:
        self.endpoint_scheme = "https"
        self.cached = {"qwen.gguf"}
        self.live: dict[int, tuple[str, int]] = {}
        self.next_pid = 20_000
        self.starts = 0
        self.stops = 0
        self.active_count = 0

    def cached_models(self) -> tuple[str, ...]:
        return tuple(sorted(self.cached))

    def start(self, model_id: str, port: int) -> RuntimeHandle:
        self.next_pid += 1
        self.live[self.next_pid] = (model_id, port)
        self.starts += 1
        return RuntimeHandle(self.next_pid, port)

    def owns(self, handle: RuntimeHandle, model_id: str) -> bool:
        return self.live.get(handle.pid) == (model_id, handle.port)

    def ready(self, handle: RuntimeHandle, model_id: str) -> bool:
        return self.owns(handle, model_id)

    def active_requests(self, handle: RuntimeHandle, model_id: str) -> int | None:
        return self.active_count if self.owns(handle, model_id) else None

    def stop(self, handle: RuntimeHandle, model_id: str) -> None:
        if not self.owns(handle, model_id):
            raise RuntimeError("not owned")
        self.live.pop(handle.pid)
        self.stops += 1


class StaticCollector:
    def __init__(self) -> None:
        self.signals = safe_signals()

    def collect(self) -> HostSignals:
        return self.signals


def safe_signals() -> HostSignals:
    return HostSignals(
        timestamp=time.time(),
        battery_percent=100,
        on_battery=False,
        battery_charging=True,
        idle_seconds=1_000,
        user_active=False,
        temperature_celsius=45,
        cpu_utilization_percent=10,
        load_per_cpu=0.1,
        memory_percent=20,
        network_available=True,
    )


def resources() -> dict:
    return {
        "usable_bytes": 16 * 1024**3,
        "backend": "metal",
        "machine": {"platform": "macos-arm64"},
    }


def model_profile(*, min_replicas: int = 1) -> dict:
    return {
        "memory_mb": 8_000,
        "runtimes": ["llama.cpp"],
        "backends": ["metal"],
        "min_replicas": min_replicas,
        "max_replicas": 1,
        "min_residency_seconds": 0,
        "scale_down_cooldown_seconds": 0,
    }


def make_agent(
    tmp_path,
    client,
    *,
    collector=None,
    resource_info_collector=resources,
    advertise_host="10.0.0.5",
    **agent_kwargs,
):
    backend = FakeBackend()
    collector = collector or StaticCollector()
    managed = ManagedModelRuntime(
        tmp_path / "node.json",
        host_id="host-a",
        backend=backend,
        signal_collector=collector,
        protection_loop=LocalHostProtectionLoop(
            HostPolicy(
                pause_for_user_activity=False,
                recovery_cooldown_seconds=0,
                activity_recovery_seconds=0,
                thermal_recovery_seconds=0,
            )
        ),
        port_available=lambda _port: True,
    )
    agent = AllocatorNodeAgent(
        grid_url="http://testserver",
        control_token=mint_node_token("secret", "host-a"),
        runtime=managed,
        advertise_host=advertise_host,
        client=client,
        resource_collector=resource_info_collector,
        heartbeat_interval=1,
        shutdown_drain_timeout=1,
        shutdown_poll_interval=0.01,
        allow_insecure_http=True,
        **agent_kwargs,
    )
    return agent, managed, backend, collector


def enable_automatic(client: TestClient) -> None:
    if client.app.state.allocator.state_path is None:
        temporary_state = tempfile.TemporaryDirectory()
        client.app.state.allocator_test_state = temporary_state
        client.app.state.allocator.state_path = (
            Path(temporary_state.name) / "allocator.json"
        )
    headers = {"X-Grid-Allocator-Token": "secret"}
    assert client.put(
        "/allocator/models/qwen.gguf",
        headers=headers,
        json=model_profile(),
    ).status_code == 200
    assert client.put(
        "/allocator/mode",
        headers=headers,
        json={"mode": "automatic"},
    ).status_code == 200


def test_node_loop_warms_and_advertises_only_ready_model(tmp_path):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    with TestClient(app) as client:
        enable_automatic(client)
        agent, managed, backend, _ = make_agent(tmp_path, client)
        agent.heartbeat_once()

        assert managed.wait_idle(1)
        assert backend.starts == 1
        assert managed.residencies[0].state == ResidencyState.READY
        assert [item["id"] for item in client.get("/v1/models").json()["data"]] == [
            "qwen.gguf"
        ]
        engine = client.get("/nodes/discover").json()["engines"][0]
        assert engine["endpoint_url"].startswith("https://10.0.0.5:")


def test_ready_engine_put_commit_then_timeout_retains_tombstone_and_fences_route(
    tmp_path,
    monkeypatch,
):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    with TestClient(app) as client:
        enable_automatic(client)
        agent, managed, backend, _ = make_agent(tmp_path, client)
        original_put = client.put
        committed = False

        def commit_engine_then_lose_response(*args, **kwargs):
            nonlocal committed
            body = kwargs.get("json") or {}
            if body.get("role") == "engine" and not committed:
                committed = True
                response = original_put(*args, **kwargs)
                assert response.status_code == 200
                raise httpx.ReadTimeout(
                    "engine PUT response lost after commit",
                    request=response.request,
                )
            return original_put(*args, **kwargs)

        monkeypatch.setattr(client, "put", commit_engine_then_lose_response)
        with pytest.raises(httpx.ReadTimeout, match="after commit"):
            agent.heartbeat_once()
        assert managed.wait_idle(1)

        engine_id = engine_node_id(managed.host_id, "qwen.gguf")
        assert committed
        assert agent._registered_engines["qwen.gguf"] == -1
        assert app.state.nodes[engine_id].allocator["state"] == "accepting"
        # The ambiguous child PUT is safety-critical, so its exception path independently fences
        # the host even though the server may already have committed the READY child.
        assert app.state.nodes[agent.node_id].allocator["state"] == "draining"
        assert client.get("/v1/models").json()["data"] == []

        monkeypatch.setattr(client, "put", original_put)
        assert client.put(
            "/allocator/mode",
            headers={"X-Grid-Allocator-Token": "secret"},
            json={"mode": "observe"},
        ).status_code == 200
        backend.live.clear()
        deleted: list[str] = []
        original_delete = client.delete

        def record_delete(*args, **kwargs):
            if str(args[0]).endswith(f"/nodes/{engine_id}"):
                deleted.append(engine_id)
            return original_delete(*args, **kwargs)

        monkeypatch.setattr(client, "delete", record_delete)
        agent.heartbeat_once()

        assert deleted == [engine_id]
        assert engine_id not in app.state.nodes
        assert "qwen.gguf" not in agent._registered_engines
        assert client.get("/v1/models").json()["data"] == []


@pytest.mark.parametrize(
    "advertise_host",
    ("fe80::1234%en0", "[fe80::1234%25en0]"),
)
def test_scoped_ipv6_managed_endpoint_is_encoded_exactly_once(
    tmp_path,
    advertise_host,
):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    with TestClient(app) as client:
        enable_automatic(client)
        agent, managed, _, _ = make_agent(
            tmp_path,
            client,
            advertise_host=advertise_host,
        )
        agent.heartbeat_once()
        assert managed.wait_idle(1)
        agent.heartbeat_once()

        engine = client.get("/nodes/discover").json()["engines"][0]
        assert engine["endpoint_url"] == "https://[fe80::1234%25en0]:18081/v1"


def test_node_heartbeats_runtime_slot_activity_for_direct_requests(tmp_path):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    with TestClient(app) as client:
        enable_automatic(client)
        agent, managed, _, _ = make_agent(tmp_path, client)
        managed.active_requests = lambda _model_id: 2  # type: ignore[method-assign]

        agent.heartbeat_once()
        assert managed.wait_idle(1)
        agent.heartbeat_once()

        engine = client.get("/nodes/discover").json()["engines"][0]
        assert engine["load"]["active_tasks"] == 2
        managed_engine = app.state.nodes[engine["node_id"]]
        assert managed_engine.engine_api_key == managed.engine_api_key
        assert "engine_api_key" not in engine
        residency = client.get("/allocator/status").json()["nodes"][0]["residencies"][0]
        assert residency["active_requests"] == 2


def test_many_slow_child_activity_probes_are_bounded_parallel_after_host_lease(
    tmp_path,
    monkeypatch,
):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    with TestClient(app) as client:
        agent, managed, backend, _ = make_agent(
            tmp_path,
            client,
            heartbeat_cycle_timeout=15,
        )
        residencies: dict[str, ManagedResidency] = {}
        for index in range(100):
            model_id = f"model-{index:02d}.gguf"
            handle = RuntimeHandle(30_000 + index, 21_000 + index)
            backend.live[handle.pid] = (model_id, handle.port)
            residencies[model_id] = ManagedResidency(
                model_id,
                1,
                ResidencyState.READY,
                handle=handle,
            )
            agent._registered_engines[model_id] = handle.port
        with managed._lock:
            managed._residencies = residencies

        lease_published = threading.Event()
        original_lease = agent._heartbeat_control_lease

        def publish_lease(*, deadline=None) -> None:
            original_lease(deadline=deadline)
            lease_published.set()

        active = 0
        peak_active = 0
        activity_lock = threading.Lock()
        health_active = 0
        peak_health = 0

        def slow_ready(handle: RuntimeHandle, model_id: str) -> bool:
            nonlocal health_active, peak_health
            assert lease_published.is_set()
            with activity_lock:
                health_active += 1
                peak_health = max(peak_health, health_active)
            time.sleep(0.02)
            with activity_lock:
                health_active -= 1
            return backend.owns(handle, model_id)

        def slow_activity(_model_id: str) -> int:
            nonlocal active, peak_active
            assert lease_published.is_set()
            with activity_lock:
                active += 1
                peak_active = max(peak_active, active)
            time.sleep(1.0)
            with activity_lock:
                active -= 1
            return 0

        monkeypatch.setattr(agent, "_heartbeat_control_lease", publish_lease)
        monkeypatch.setattr(backend, "ready", slow_ready)
        monkeypatch.setattr(managed, "active_requests", slow_activity)

        started = time.monotonic()
        agent.heartbeat_once()
        elapsed = time.monotonic() - started

        # At the runtime's actual maximum of 100 models, serial one-second /slots timeouts exceed
        # the 60-second registry lease. The bounded worker pool finishes that exact worst case with
        # ample room for every child update inside one absolute cycle deadline.
        assert peak_health == 16
        assert peak_active == 32
        assert 3.0 <= elapsed < 6.0
        assert agent.last_error == ""
        assert agent.node_id in app.state.nodes
        assert len(agent._registered_engines) == 100
        engines = {
            node_id: node.last_heartbeat
            for node_id, node in app.state.nodes.items()
            if node.role == "engine"
        }
        assert len(engines) == 100

        # Repeat the worst-case cardinality and verify that deterministic bounded work renews every
        # child rather than repeatedly servicing only a low-id prefix.
        agent.heartbeat_once()
        assert all(
            app.state.nodes[node_id].last_heartbeat > prior
            for node_id, prior in engines.items()
        )


def test_normal_registry_requests_keep_full_cycle_budget_but_shutdown_is_capped(
    tmp_path,
    monkeypatch,
):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    with TestClient(app) as client:
        agent, _, _, _ = make_agent(tmp_path, client)
        now = [100.0]
        agent._monotonic = lambda: now[0]

        # A normal control heartbeat can legitimately wait for the server's ten-second allocator
        # tick. Preserve the whole cycle remainder rather than truncating every request to one
        # second.
        assert agent._timeout_kwargs(111.0) == {"timeout": 11.0}

        shutdown_deadlines: list[float] = []

        def record_shutdown_fence(*, deadline=None) -> None:
            assert deadline is not None
            shutdown_deadlines.append(deadline)

        monkeypatch.setattr(agent, "_heartbeat_shutdown_control", record_shutdown_fence)
        agent.shutdown(drain_timeout=15)

        # Shutdown deliberately gives each control-plane operation only its independent bounded
        # request slice, leaving time for local cleanup inside the overall drain deadline.
        assert shutdown_deadlines == pytest.approx([101.0])


def test_authenticated_server_ttl_can_only_raise_local_expiry_bound(tmp_path):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    with TestClient(app) as client:
        agent, _, _, _ = make_agent(tmp_path, client)

        agent._observe_registry_ttl({"node": {"ttl_seconds": 90}})
        assert agent.registry_ttl_seconds == 90
        agent._observe_registry_ttl({"ttl_seconds": 30})
        assert agent.registry_ttl_seconds == 90


@pytest.mark.parametrize("target", ("control", "ready-engine"))
@pytest.mark.parametrize("lose_response", (False, True))
def test_routable_write_completion_anchors_full_registry_lease(
    tmp_path,
    monkeypatch,
    target,
    lose_response,
):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    with TestClient(app) as client:
        agent, managed, backend, _ = make_agent(tmp_path, client)
        now = [0.0]
        agent._monotonic = lambda: now[0]
        original_put = client.put

        def complete_at_thirty_seconds(*args, **kwargs):
            body = kwargs.get("json") or {}
            if body.get("role") == ("allocator" if target == "control" else "engine"):
                response = original_put(*args, **kwargs)
                assert response.status_code == 200
                now[0] = 30.0
                if lose_response:
                    raise httpx.ReadTimeout(
                        "routable response lost after late commit",
                        request=response.request,
                    )
                return response
            return original_put(*args, **kwargs)

        monkeypatch.setattr(client, "put", complete_at_thirty_seconds)
        if target == "control":
            operation = agent._register_control
        else:
            handle = backend.start("qwen.gguf", 18_081)
            residency = ManagedResidency(
                "qwen.gguf",
                8_000,
                ResidencyState.READY,
                handle=handle,
            )
            with managed._lock:
                managed._residencies[residency.model_id] = residency

            def operation():
                return agent._register_engine(residency)

        if lose_response:
            with pytest.raises(httpx.ReadTimeout, match="late commit"):
                operation()
        else:
            operation()

        # The request began at t=0 but could have committed immediately before its completion at
        # t=30. The child must remain serving through t=90, not merely start+TTL at t=60.
        assert agent._last_routable_registry_attempt_at == pytest.approx(30.0)
        assert agent._routable_registry_expiry_deadline() == pytest.approx(90.0)


def test_physical_capacity_is_not_multiplied_by_child_engine_records(tmp_path):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    with TestClient(app) as client:
        enable_automatic(client)
        agent, _, _, _ = make_agent(tmp_path, client)
        agent.heartbeat_once()
        status = client.get("/allocator/status").json()
        assert len(status["nodes"]) == 1
        assert status["nodes"][0]["node_id"] == "host-a"
        assert status["nodes"][0]["capacity_mb"] == 16 * 1024


def test_local_unhealthy_state_immediately_removes_child_from_routing(tmp_path):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    with TestClient(app) as client:
        enable_automatic(client)
        collector = StaticCollector()
        agent, managed, backend, _ = make_agent(tmp_path, client, collector=collector)
        agent.heartbeat_once()
        assert client.get("/v1/models").json()["data"]

        collector.signals = HostSignals(timestamp=time.time() + 1, network_available=False)
        agent.heartbeat_once()
        assert client.get("/v1/models").json()["data"] == []
        assert managed.residencies[0].state == ResidencyState.READY
        assert backend.live


@pytest.mark.parametrize("failure_mode", ("dead", "owned_unready"))
def test_desired_failed_replica_recovers_without_old_success_cooldown(
    tmp_path,
    monkeypatch,
    failure_mode,
):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    host_busy = [False]

    def full_host_resources() -> dict:
        return {
            "usable_bytes": 1,
            "backend": "metal",
            "machine": {"platform": "macos-arm64"},
            "memory": {
                "total_gb": 16,
                "available_gb": 0 if host_busy[0] else 16,
            },
        }

    with TestClient(app) as client:
        enable_automatic(client)
        agent, managed, backend, _ = make_agent(
            tmp_path,
            client,
            resource_info_collector=(
                full_host_resources if failure_mode == "owned_unready" else resources
            ),
        )
        agent.heartbeat_once()
        assert managed.wait_idle(1)
        agent.heartbeat_once()
        first = managed.residencies[0].handle
        assert first is not None
        assert client.get("/v1/models").json()["data"]

        if failure_mode == "dead":
            backend.live.pop(first.pid)
        else:
            original_ready = backend.ready

            def ready_unless_old(handle, model_id):
                return handle.pid != first.pid and original_ready(handle, model_id)

            monkeypatch.setattr(backend, "ready", ready_unless_old)
            backend.active_count = 0
            host_busy[0] = True

        # The prior successful WARM normally owns a success-observation guard. Explicit FAILED
        # health contradicts that observation, so this same cycle must issue a fresh WARM while
        # still retaining any real failure backoff. The runtime replaces only a proven-idle owned
        # child; a confirmed-dead handle is simply restarted.
        agent.heartbeat_once()
        assert managed.wait_idle(1)
        agent.heartbeat_once()

        recovered = managed.residencies[0]
        assert recovered.state == ResidencyState.READY
        assert recovered.handle is not None
        assert recovered.handle != first
        assert backend.starts == 2
        assert backend.stops == int(failure_mode == "owned_unready")
        assert [item["id"] for item in client.get("/v1/models").json()["data"]] == [
            "qwen.gguf"
        ]


def test_scale_down_drains_before_stopping_process(tmp_path):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    headers = {"X-Grid-Allocator-Token": "secret"}
    with TestClient(app) as client:
        enable_automatic(client)
        agent, managed, backend, _ = make_agent(tmp_path, client)
        agent.heartbeat_once()
        assert backend.live

        response = client.put(
            "/allocator/models/qwen.gguf",
            headers=headers,
            json=model_profile(min_replicas=0),
        )
        assert response.status_code == 200
        for _ in range(4):
            agent.heartbeat_once()
            if managed.residencies[0].state == ResidencyState.CACHED:
                break
        assert managed.residencies[0].state == ResidencyState.CACHED
        assert backend.stops == 1
        assert client.get("/v1/models").json()["data"] == []


@pytest.mark.parametrize("commit_before_timeout", (False, True))
def test_draining_child_heartbeat_timeout_independently_fences_host(
    tmp_path,
    monkeypatch,
    commit_before_timeout,
):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    with TestClient(app) as client:
        enable_automatic(client)
        agent, managed, _, _ = make_agent(tmp_path, client)
        agent.heartbeat_once()
        assert managed.wait_idle(1)
        agent.heartbeat_once()
        assert client.put(
            "/allocator/mode",
            headers={"X-Grid-Allocator-Token": "secret"},
            json={"mode": "observe"},
        ).status_code == 200

        ready = managed.residencies[0]
        with managed._lock:
            managed._residencies[ready.model_id] = ManagedResidency(
                model_id=ready.model_id,
                memory_mb=ready.memory_mb,
                state=ResidencyState.DRAINING,
                loaded_at=ready.loaded_at,
                last_used_at=ready.last_used_at,
                load_failures=ready.load_failures,
                pinned=ready.pinned,
                handle=ready.handle,
            )

        engine_id = engine_node_id(managed.host_id, ready.model_id)
        original_post = client.post
        timed_out = False

        def lose_draining_child_response(*args, **kwargs):
            nonlocal timed_out
            body = kwargs.get("json") or {}
            is_draining_child = (
                body.get("node_id") == engine_id
                and (body.get("allocator") or {}).get("state") == "draining"
            )
            if is_draining_child and not timed_out:
                timed_out = True
                if commit_before_timeout:
                    response = original_post(*args, **kwargs)
                    assert response.status_code == 200
                    request = response.request
                else:
                    request = httpx.Request("POST", str(args[0]))
                raise httpx.ReadTimeout("draining heartbeat timed out", request=request)
            return original_post(*args, **kwargs)

        monkeypatch.setattr(client, "post", lose_draining_child_response)
        with pytest.raises(httpx.ReadTimeout, match="draining heartbeat"):
            agent.heartbeat_once()

        assert timed_out
        expected_child_state = "draining" if commit_before_timeout else "accepting"
        assert app.state.nodes[engine_id].allocator["state"] == expected_child_state
        assert app.state.nodes[agent.node_id].allocator["state"] == "draining"
        assert agent._registry_cleanup_fenced
        assert client.get("/v1/models").json()["data"] == []

        monkeypatch.setattr(client, "post", original_post)
        agent.heartbeat_once()
        assert app.state.nodes[engine_id].allocator["state"] == "draining"
        assert app.state.nodes[agent.node_id].allocator["state"] == "accepting"
        assert not agent._registry_cleanup_fenced


def test_post_command_child_sync_deadline_publishes_independent_host_fence(
    tmp_path,
    monkeypatch,
):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    headers = {"X-Grid-Allocator-Token": "secret"}
    with TestClient(app) as client:
        enable_automatic(client)
        agent, managed, backend, _ = make_agent(tmp_path, client)
        agent.heartbeat_once()
        assert managed.wait_idle(1)
        agent.heartbeat_once()
        assert client.get("/v1/models").json()["data"]
        assert client.put(
            "/allocator/mode",
            headers=headers,
            json={"mode": "observe"},
        ).status_code == 200

        epoch, sequence, _digest = managed._latest_plan_generation.split(":")
        drain = MutationAction(
            action_id="post-command-drain",
            kind=ActionKind.DRAIN,
            node_id=managed.host_id,
            model_id="qwen.gguf",
            memory_mb=8_000,
            reason="test post-command route fence",
            plan_generation=f"{epoch}:{int(sequence) + 1:020d}:{'f' * 12}",
            created_at=time.time(),
            executable=True,
        )
        original_post_control = agent._post_control
        command_delivered = False

        def deliver_drain(*, deadline=None):
            nonlocal command_delivered
            assert deadline is not None
            if not command_delivered:
                command_delivered = True
                return {"allocator": {"commands": [drain.to_dict()]}}, []
            return original_post_control(deadline=deadline)

        original_sync = agent._sync_engine_nodes
        sync_calls = 0

        def expire_post_command_sync(*, deadline=None) -> tuple[str, ...]:
            nonlocal sync_calls
            sync_calls += 1
            if sync_calls == 2:
                assert managed.residencies[0].state == ResidencyState.DRAINING
                return ("qwen.gguf",)
            return original_sync(deadline=deadline)

        monkeypatch.setattr(agent, "_post_control", deliver_drain)
        monkeypatch.setattr(agent, "_sync_engine_nodes", expire_post_command_sync)
        agent.heartbeat_once()

        engine_id = engine_node_id(managed.host_id, "qwen.gguf")
        assert command_delivered
        assert backend.live
        assert managed.residencies[0].state == ResidencyState.DRAINING
        assert app.state.nodes[engine_id].allocator["state"] == "accepting"
        assert app.state.nodes[agent.node_id].allocator["state"] == "draining"
        assert client.get("/v1/models").json()["data"] == []
        assert agent._registry_cleanup_fenced

        # A later complete child sync publishes the DRAINING child first and only then reopens the
        # host record. The emergency fence is therefore sticky for exactly as long as required.
        monkeypatch.setattr(agent, "_post_control", original_post_control)
        monkeypatch.setattr(agent, "_sync_engine_nodes", original_sync)
        agent.heartbeat_once()
        assert not agent._registry_cleanup_fenced
        assert app.state.nodes[engine_id].allocator["state"] == "draining"
        assert app.state.nodes[agent.node_id].allocator["state"] == "accepting"


def test_deferred_child_sync_lease_does_not_mark_discarded_command_delivered(
    tmp_path,
    monkeypatch,
):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    headers = {"X-Grid-Allocator-Token": "secret"}
    with TestClient(app) as client:
        enable_automatic(client)
        agent, managed, _, _ = make_agent(tmp_path, client)
        agent.heartbeat_once()
        assert managed.wait_idle(1)
        agent.heartbeat_once()

        assert client.put(
            "/allocator/models/qwen.gguf",
            headers=headers,
            json=model_profile(min_replicas=0),
        ).status_code == 200
        assert client.post("/allocator/tick", headers=headers).status_code == 200
        pending = client.get("/allocator/status").json()["pending_commands"]
        drain = next(item for item in pending if item["kind"] == "drain")

        # The early control lease returns before a deliberately deferred child sync. Its response
        # is discarded, so it must not cross the controller's command-delivery boundary.
        monkeypatch.setattr(
            agent,
            "_sync_engine_nodes",
            lambda **_kwargs: ("qwen.gguf",),
        )
        agent.heartbeat_once()
        assert drain["action_id"] not in app.state.allocator._delivered_command_ids

        # Demand rebounds before any real command poll. An undelivered destructive command can be
        # cancelled cleanly instead of becoming permanent withdrawn-delivery uncertainty.
        assert client.put(
            "/allocator/models/qwen.gguf",
            headers=headers,
            json=model_profile(min_replicas=1),
        ).status_code == 200
        assert client.post("/allocator/tick", headers=headers).status_code == 200
        status = client.get("/allocator/status").json()
        assert status["pending_commands"] == []
        assert status["withdrawn_destructive"] == []


def test_demand_rebound_readmits_draining_process_on_a_full_host(tmp_path):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    headers = {"X-Grid-Allocator-Token": "secret"}

    host_busy = [False]

    def exactly_full_resources() -> dict:
        return {
            "usable_bytes": 1,
            "backend": "metal",
            "machine": {"platform": "macos-arm64"},
            "memory": {
                "total_gb": 16,
                "available_gb": 0 if host_busy[0] else 16,
            },
        }

    with TestClient(app) as client:
        enable_automatic(client)
        agent, managed, backend, _ = make_agent(
            tmp_path,
            client,
            resource_info_collector=exactly_full_resources,
        )
        agent.heartbeat_once()
        assert managed.wait_idle(1)
        assert managed.residencies[0].state == ResidencyState.READY
        assert backend.starts == 1
        host_busy[0] = True

        assert client.put(
            "/allocator/models/qwen.gguf",
            headers=headers,
            json=model_profile(min_replicas=0),
        ).status_code == 200
        agent.heartbeat_once()
        assert managed.wait_idle(1)
        assert managed.residencies[0].state == ResidencyState.DRAINING
        assert agent.resources["available_mb"] == 0
        assert backend.live

        # Demand rebounds before the dependent UNLOAD is delivered. WARM is a zero-allocation
        # state transition here: the exact same child becomes routable again on the full host.
        assert client.put(
            "/allocator/models/qwen.gguf",
            headers=headers,
            json=model_profile(min_replicas=1),
        ).status_code == 200
        agent.heartbeat_once()
        assert managed.wait_idle(1)

        assert managed.residencies[0].state == ResidencyState.READY
        assert backend.starts == 1
        assert backend.stops == 0
        assert len(backend.live) == 1
        assert client.get("/v1/models").json()["data"]


def test_control_record_reregisters_after_server_forgets_it(tmp_path):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    with TestClient(app) as client:
        agent, _, _, _ = make_agent(tmp_path, client)
        agent.heartbeat_once()
        assert client.delete(
            f"/nodes/{agent.node_id}",
            headers={"X-Grid-Allocator-Node-Token": agent.control_token},
        ).status_code == 200
        agent.heartbeat_once()
        node_ids = {row["node_id"] for row in client.get("/allocator/status").json()["nodes"]}
        assert "host-a" in node_ids


def test_restart_fences_host_before_deleting_stale_deterministic_engine_record(
    tmp_path,
    monkeypatch,
):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    headers = {"X-Grid-Allocator-Token": "secret"}
    with TestClient(app) as client:
        enable_automatic(client)
        first, managed, backend, collector = make_agent(tmp_path, client)
        first.heartbeat_once()
        assert managed.wait_idle(1)
        first.heartbeat_once()
        stale_id = engine_node_id(managed.host_id, "qwen.gguf")
        assert stale_id in app.state.nodes

        # Freeze planning, stop the child without letting the old agent clean its registry record,
        # then construct the replacement agent from the persisted CACHED residency.
        assert client.put(
            "/allocator/mode", headers=headers, json={"mode": "observe"}
        ).status_code == 200
        managed.stop_all(wait_timeout=0)
        assert stale_id in app.state.nodes
        restarted_runtime = ManagedModelRuntime(
            managed.state_path,
            host_id="host-a",
            backend=backend,
            signal_collector=collector,
            protection_loop=LocalHostProtectionLoop(
                HostPolicy(
                    pause_for_user_activity=False,
                    recovery_cooldown_seconds=0,
                    activity_recovery_seconds=0,
                    thermal_recovery_seconds=0,
                )
            ),
            port_available=lambda _port: True,
        )
        restarted = AllocatorNodeAgent(
            grid_url="http://testserver",
            control_token=mint_node_token("secret", "host-a"),
            runtime=restarted_runtime,
            advertise_host="10.0.0.5",
            client=client,
            resource_collector=resources,
            allow_insecure_http=True,
        )

        assert restarted._registry_cleanup_fenced
        events: list[str] = []
        original_shutdown_control = restarted._heartbeat_shutdown_control
        original_delete = client.delete

        def publish_startup_fence(*, deadline=None) -> None:
            events.append("host-fenced")
            original_shutdown_control(deadline=deadline)

        def delete_after_startup_fence(*args, **kwargs):
            if str(args[0]).endswith(f"/nodes/{stale_id}"):
                events.append("stale-child-delete")
                assert app.state.nodes[restarted.node_id].allocator["state"] == "draining"
                assert client.get("/v1/models").json()["data"] == []
            return original_delete(*args, **kwargs)

        monkeypatch.setattr(restarted, "_heartbeat_shutdown_control", publish_startup_fence)
        monkeypatch.setattr(client, "delete", delete_after_startup_fence)
        restarted.heartbeat_once()

        assert events[:2] == ["host-fenced", "stale-child-delete"]
        assert stale_id not in app.state.nodes
        assert not restarted._registry_cleanup_fenced
        assert app.state.nodes[restarted.node_id].allocator["state"] == "accepting"


def test_failed_engine_delete_remains_a_tombstone_and_retries(tmp_path, monkeypatch):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    with TestClient(app) as client:
        agent, _, _, _ = make_agent(tmp_path, client)
        agent._registered_engines["retired.gguf"] = -1
        original_delete = client.delete
        attempts = 0

        def flaky_delete(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                request = httpx.Request("DELETE", str(args[0]))
                raise httpx.ConnectError("temporary outage", request=request)
            return original_delete(*args, **kwargs)

        monkeypatch.setattr(client, "delete", flaky_delete)
        agent._sync_engine_nodes()
        # A failed DELETE is immediately followed by a fail-closed heartbeat fence. A 404 on that
        # authenticated child record proves it is already absent, so no tombstone remains.
        assert agent._registered_engines == {}

        agent._sync_engine_nodes()
        assert agent._registered_engines == {}
        assert attempts == 1


def test_failed_engine_delete_fences_stale_child_route_before_acknowledging(
    tmp_path,
    monkeypatch,
):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    with TestClient(app) as client:
        enable_automatic(client)
        agent, managed, backend, _ = make_agent(tmp_path, client)
        agent.heartbeat_once()
        assert managed.wait_idle(1)
        agent.heartbeat_once()
        engine_id = engine_node_id(managed.host_id, "qwen.gguf")
        assert [item["id"] for item in client.get("/v1/models").json()["data"]] == [
            "qwen.gguf"
        ]
        # Freeze desired-state actuation so this test isolates retirement fencing rather than the
        # separate immediate self-healing path, which can safely overwrite the deterministic child
        # record with a newly started replacement in the same heartbeat.
        assert client.put(
            "/allocator/mode",
            headers={"X-Grid-Allocator-Token": "secret"},
            json={"mode": "observe"},
        ).status_code == 200

        # Health reconciliation proves the exact owned child died. Simulate a transient failure
        # deleting its old READY registry record: the agent must turn that record DRAINING before
        # it can release the FAILED receipt to the global controller.
        backend.live.clear()
        original_delete = client.delete

        def failed_engine_delete(*args, **kwargs):
            if str(args[0]).endswith(f"/nodes/{engine_id}"):
                return httpx.Response(
                    503,
                    request=httpx.Request("DELETE", str(args[0])),
                )
            return original_delete(*args, **kwargs)

        monkeypatch.setattr(client, "delete", failed_engine_delete)
        agent.heartbeat_once()

        assert client.get("/v1/models").json()["data"] == []
        assert app.state.nodes[engine_id].allocator["state"] == "draining"
        assert "qwen.gguf" in agent._registered_engines

        monkeypatch.setattr(client, "delete", original_delete)
        agent.heartbeat_once()
        assert engine_id not in app.state.nodes
        assert "qwen.gguf" not in agent._registered_engines


def test_unfenceable_stale_child_keeps_host_draining_and_withholds_receipts(
    tmp_path,
    monkeypatch,
):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    with TestClient(app) as client:
        enable_automatic(client)
        agent, managed, backend, _ = make_agent(tmp_path, client)
        agent.heartbeat_once()
        assert managed.wait_idle(1)
        agent.heartbeat_once()
        engine_id = engine_node_id(managed.host_id, "qwen.gguf")
        backend.live.clear()

        original_delete = client.delete
        original_post = client.post

        def failed_engine_delete(*args, **kwargs):
            if str(args[0]).endswith(f"/nodes/{engine_id}"):
                return httpx.Response(
                    503,
                    request=httpx.Request("DELETE", str(args[0])),
                )
            return original_delete(*args, **kwargs)

        def failed_child_fence(*args, **kwargs):
            body = kwargs.get("json") or {}
            if body.get("node_id") == engine_id:
                return httpx.Response(
                    503,
                    request=httpx.Request("POST", str(args[0])),
                )
            return original_post(*args, **kwargs)

        monkeypatch.setattr(client, "delete", failed_engine_delete)
        monkeypatch.setattr(client, "post", failed_child_fence)
        agent.heartbeat_once()

        assert agent._registry_cleanup_fenced
        assert "qwen.gguf" in agent._registered_engines
        assert app.state.nodes[agent.node_id].allocator["state"] == "draining"
        # The stale child remains READY, but the authoritative physical-host fence removes it from
        # discovery and the early lease keeps that fence alive on subsequent failed cycles.
        assert app.state.nodes[engine_id].allocator["state"] == "accepting"
        assert client.get("/v1/models").json()["data"] == []
        history_before = tuple(app.state.allocator.history)
        assert original_delete(
            f"/nodes/{agent.node_id}",
            headers={"X-Grid-Allocator-Node-Token": agent.control_token},
        ).status_code == 200
        agent.heartbeat_once()
        assert app.state.nodes[agent.node_id].allocator["state"] == "draining"
        assert tuple(app.state.allocator.history) == history_before

        monkeypatch.setattr(client, "delete", original_delete)
        monkeypatch.setattr(client, "post", original_post)
        agent.heartbeat_once()
        assert not agent._registry_cleanup_fenced
        assert managed.wait_idle(1)
        # Once stale deletion succeeds, the withheld FAILED observation reaches the controller and
        # its immediate recovery WARM may reuse the deterministic engine id. The surviving record
        # must describe the new READY child, never the stale dead endpoint.
        assert engine_id in app.state.nodes
        assert app.state.nodes[engine_id].allocator["state"] == "accepting"
        assert backend.starts == 2
        assert client.get("/v1/models").json()["data"]


def test_shutdown_stops_children_and_unregisters_all_records(tmp_path):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    with TestClient(app) as client:
        enable_automatic(client)
        agent, _, backend, _ = make_agent(tmp_path, client)
        agent.heartbeat_once()
        original_stop = backend.stop

        def stop_after_confirmed_fence(handle, model_id) -> None:
            control = app.state.nodes[agent.node_id]
            assert control.allocator["state"] == ResidencyState.DRAINING.value
            assert control.allocator["decision"] is None
            assert client.get("/v1/models").json()["data"] == []
            original_stop(handle, model_id)

        backend.stop = stop_after_confirmed_fence
        agent.shutdown()
        assert not backend.live
        assert client.get("/nodes/discover").json()["engines"] == []
        assert client.get("/allocator/status").json()["nodes"] == []


def test_shutdown_keeps_host_fence_when_a_child_record_cannot_be_deleted(
    tmp_path,
    monkeypatch,
):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    with TestClient(app) as client:
        enable_automatic(client)
        agent, managed, backend, _ = make_agent(tmp_path, client)
        agent.heartbeat_once()
        assert managed.wait_idle(1)
        agent.heartbeat_once()
        engine_id = engine_node_id(managed.host_id, "qwen.gguf")
        original_delete = client.delete

        def failed_child_delete(*args, **kwargs):
            if str(args[0]).endswith(f"/nodes/{engine_id}"):
                return httpx.Response(
                    503,
                    request=httpx.Request("DELETE", str(args[0])),
                )
            return original_delete(*args, **kwargs)

        monkeypatch.setattr(client, "delete", failed_child_delete)
        agent.shutdown()

        assert not backend.live
        assert engine_id in app.state.nodes
        assert agent.node_id in app.state.nodes
        assert app.state.nodes[agent.node_id].allocator["state"] == "draining"
        assert client.get("/v1/models").json()["data"] == []
        assert not agent._shutdown_complete

        monkeypatch.setattr(client, "delete", original_delete)
        agent.shutdown()
        assert agent._shutdown_complete
        assert app.state.nodes == {}


def test_shutdown_fences_host_before_a_racing_warm_can_finish(tmp_path, monkeypatch):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    with TestClient(app) as client:
        enable_automatic(client)
        agent, managed, backend, _ = make_agent(tmp_path, client)
        entered_start = threading.Event()
        release_start = threading.Event()
        original_start = backend.start
        original_stop = backend.stop

        def blocking_start(model_id: str, port: int) -> RuntimeHandle:
            entered_start.set()
            assert release_start.wait(1)
            return original_start(model_id, port)

        def fenced_stop(handle: RuntimeHandle, model_id: str) -> None:
            control = app.state.nodes[agent.node_id]
            assert control.allocator["state"] == "draining"
            assert control.allocator["decision"] is None
            original_stop(handle, model_id)

        monkeypatch.setattr(backend, "start", blocking_start)
        monkeypatch.setattr(backend, "stop", fenced_stop)
        agent.heartbeat_once()
        assert entered_start.wait(1)
        assert managed.busy

        finished = threading.Event()

        def shutdown() -> None:
            agent.shutdown(drain_timeout=1)
            finished.set()

        thread = threading.Thread(target=shutdown)
        thread.start()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            control = app.state.nodes.get(agent.node_id)
            if control is not None and control.allocator.get("state") == "draining":
                break
            time.sleep(0.005)
        else:
            pytest.fail("shutdown did not publish its routing fence")

        release_start.set()
        thread.join(1)
        assert finished.is_set()
        assert not backend.live
        assert managed.residencies[0].handle is None
        assert app.state.nodes == {}


def test_shutdown_gates_routing_then_waits_for_server_owned_active_tasks(tmp_path):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    with TestClient(app) as client:
        enable_automatic(client)
        agent, managed, backend, _ = make_agent(tmp_path, client)
        agent.heartbeat_once()
        assert managed.wait_idle(1)
        engine_id = next(
            node_id for node_id, node in app.state.nodes.items() if node.role == "engine"
        )
        server_module._change_active_tasks(app.state.nodes[engine_id], 1)

        stopped = threading.Event()

        def shutdown() -> None:
            agent.shutdown(drain_timeout=1)
            stopped.set()

        thread = threading.Thread(target=shutdown)
        thread.start()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            control = app.state.nodes.get(agent.node_id)
            child = app.state.nodes.get(engine_id)
            if (
                control is not None
                and control.allocator.get("state") == "draining"
                and child is not None
                and child.allocator.get("state") == "draining"
            ):
                break
            time.sleep(0.005)

        assert client.get("/v1/models").json()["data"] == []
        assert backend.live
        assert not stopped.is_set()

        # This is the transition the proxy performs in its stream-finally block. Runtime slot
        # samples cannot clear the exact server-owned counter while the drain is in progress.
        server_module._change_active_tasks(app.state.nodes[engine_id], -1)
        thread.join(1)
        assert stopped.is_set()
        assert not backend.live
        assert app.state.nodes == {}


def test_shutdown_deadline_fails_safe_without_waiting_forever(tmp_path):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    with TestClient(app) as client:
        enable_automatic(client)
        agent, managed, backend, _ = make_agent(tmp_path, client)
        agent.heartbeat_once()
        assert managed.wait_idle(1)
        engine = next(node for node in app.state.nodes.values() if node.role == "engine")
        server_module._change_active_tasks(engine, 1)
        backend.active_count = 1

        started = time.monotonic()
        with pytest.raises(RuntimeError, match="cleanup was incomplete"):
            agent.shutdown(drain_timeout=0.03)
        assert time.monotonic() - started < 0.5
        assert backend.live

        agent.shutdown(force=True)
        assert not backend.live


def test_force_shutdown_skips_active_task_drain(tmp_path):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    with TestClient(app) as client:
        enable_automatic(client)
        agent, managed, backend, _ = make_agent(tmp_path, client)
        agent.heartbeat_once()
        assert managed.wait_idle(1)
        engine = next(node for node in app.state.nodes.values() if node.role == "engine")
        server_module._change_active_tasks(engine, 1)
        backend.active_count = 1

        started = time.monotonic()
        agent.shutdown(force=True)
        assert time.monotonic() - started < 0.5
        assert not backend.live


def test_shutdown_fence_failure_keeps_children_live_and_is_retryable(
    tmp_path,
    monkeypatch,
):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    with TestClient(app) as client:
        enable_automatic(client)
        agent, managed, backend, _ = make_agent(tmp_path, client)
        agent.heartbeat_once()
        assert managed.wait_idle(1)
        assert backend.live

        original_fence = agent._heartbeat_shutdown_control
        attempts = 0

        def flaky_fence(*, deadline=None) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                request = httpx.Request("POST", "http://testserver/nodes/heartbeat")
                raise httpx.ConnectError("temporary fence failure", request=request)
            original_fence(deadline=deadline)

        monkeypatch.setattr(agent, "_heartbeat_shutdown_control", flaky_fence)
        with pytest.raises(httpx.ConnectError, match="temporary fence failure"):
            agent.shutdown(drain_timeout=0.2)

        # The old accepting route still exists, so its target must remain alive. Failed fencing
        # also leaves the runtime open to a later retry instead of irreversibly entering shutdown.
        assert backend.live
        assert client.get("/v1/models").json()["data"]
        assert not managed.shutting_down
        assert not agent._shutdown_complete

        agent.shutdown(drain_timeout=1)
        assert attempts == 2
        assert not backend.live
        assert agent._shutdown_complete
        assert app.state.nodes == {}


def test_run_forever_retries_a_failed_shutdown_fence_before_stopping_children(
    tmp_path,
    monkeypatch,
):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    with TestClient(app) as client:
        enable_automatic(client)
        agent, managed, backend, _ = make_agent(tmp_path, client)
        agent.heartbeat_once()
        assert managed.wait_idle(1)
        agent.heartbeat_once()
        assert backend.live

        original_fence = agent._heartbeat_shutdown_control
        attempts = 0

        def flaky_fence(*, deadline=None) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                assert backend.live
                assert client.get("/v1/models").json()["data"]
                request = httpx.Request("POST", "http://testserver/nodes/heartbeat")
                raise httpx.ConnectError("temporary fence failure", request=request)
            original_fence(deadline=deadline)

        monkeypatch.setattr(agent, "_heartbeat_shutdown_control", flaky_fence)
        agent.request_shutdown()

        assert agent.run_forever() == 0
        assert attempts == 2
        assert not backend.live
        assert agent._shutdown_complete
        assert app.state.nodes == {}


def test_run_forever_keeps_children_live_until_last_possible_route_lease_expires(
    tmp_path,
    monkeypatch,
):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    with TestClient(app) as client:
        enable_automatic(client)
        agent, managed, backend, _ = make_agent(tmp_path, client)
        agent.heartbeat_once()
        assert managed.wait_idle(1)
        agent.heartbeat_once()
        assert backend.live

        now = [100.0]
        sleeps: list[float] = []
        stop_times: list[float] = []
        agent.registry_ttl_seconds = 0.05
        agent._last_routable_registry_attempt_at = now[0]
        agent._monotonic = lambda: now[0]

        def advance(seconds: float) -> None:
            assert backend.live
            assert client.get("/v1/models").json()["data"]
            sleeps.append(seconds)
            now[0] += seconds

        agent._sleep = advance
        original_stop = backend.stop

        def stop_after_expiry(handle, model_id) -> None:
            stop_times.append(now[0])
            assert now[0] >= 100.05
            original_stop(handle, model_id)

        def unreachable_fence(*, deadline=None) -> None:
            assert deadline is not None
            assert backend.live
            request = httpx.Request("POST", "http://testserver/nodes/heartbeat")
            raise httpx.ConnectError("controller unavailable", request=request)

        monkeypatch.setattr(backend, "stop", stop_after_expiry)
        monkeypatch.setattr(agent, "_heartbeat_shutdown_control", unreachable_fence)
        agent.request_shutdown()

        assert agent.run_forever() == 0
        assert sum(sleeps) == pytest.approx(0.05)
        assert stop_times == pytest.approx([100.05])
        assert not backend.live


def test_heartbeat_reconciles_runtime_health_before_advertising(tmp_path, monkeypatch):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    with TestClient(app) as client:
        agent, managed, _, _ = make_agent(tmp_path, client)
        events: list[str] = []
        monkeypatch.setattr(
            managed,
            "reconcile_process_health",
            lambda **_kwargs: events.append("health"),
            raising=False,
        )
        original_sync = agent._sync_engine_nodes

        def sync(*, deadline=None) -> tuple[str, ...]:
            events.append("advertise")
            return original_sync(deadline=deadline)

        monkeypatch.setattr(agent, "_sync_engine_nodes", sync)
        agent.heartbeat_once()
        assert events[:2] == ["health", "advertise"]


def test_health_persistence_failure_still_fences_a_proven_dead_child(
    tmp_path,
    monkeypatch,
):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    with TestClient(app) as client:
        enable_automatic(client)
        agent, managed, backend, _ = make_agent(tmp_path, client)
        agent.heartbeat_once()
        assert managed.wait_idle(1)
        agent.heartbeat_once()
        assert client.get("/v1/models").json()["data"]

        backend.live.clear()
        original_save = managed._save_locked

        def disk_full() -> None:
            raise OSError("disk full")

        monkeypatch.setattr(managed, "_save_locked", disk_full)
        agent.heartbeat_once()

        assert managed.residencies[0].state == ResidencyState.FAILED
        assert client.get("/v1/models").json()["data"] == []
        assert "could not persist managed-process health: disk full" in agent.last_error

        monkeypatch.setattr(managed, "_save_locked", original_save)


def test_health_probe_deadline_uses_independent_host_fence_and_later_reopens(
    tmp_path,
    monkeypatch,
):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    with TestClient(app) as client:
        enable_automatic(client)
        agent, managed, backend, _ = make_agent(tmp_path, client)
        agent.heartbeat_once()
        assert managed.wait_idle(1)
        agent.heartbeat_once()
        assert client.put(
            "/allocator/mode",
            headers={"X-Grid-Allocator-Token": "secret"},
            json={"mode": "observe"},
        ).status_code == 200
        assert client.get("/v1/models").json()["data"]

        entered = threading.Event()
        release = threading.Event()
        blocked_once = False

        def one_slow_health_probe(handle, model_id) -> bool:
            nonlocal blocked_once
            if not blocked_once:
                blocked_once = True
                entered.set()
                release.wait(1)
            return backend.owns(handle, model_id)

        monkeypatch.setattr(backend, "ready", one_slow_health_probe)
        agent.heartbeat_cycle_timeout = 0.2
        try:
            agent.heartbeat_once()
        finally:
            release.set()

        engine_id = engine_node_id(managed.host_id, "qwen.gguf")
        assert entered.is_set()
        assert managed.residencies[0].state == ResidencyState.FAILED
        # The health probe consumed the normal cycle budget after its early ACCEPTING renewal. The
        # old child record is untouched, but the independent host-wide DRAINING write hides it.
        assert app.state.nodes[engine_id].allocator["state"] == "accepting"
        assert app.state.nodes[agent.node_id].allocator["state"] == "draining"
        assert client.get("/v1/models").json()["data"] == []
        assert agent._registry_cleanup_fenced

        agent.heartbeat_cycle_timeout = 1
        agent.heartbeat_once()

        assert managed.residencies[0].state == ResidencyState.READY
        assert not agent._registry_cleanup_fenced
        assert app.state.nodes[engine_id].allocator["state"] == "accepting"
        assert app.state.nodes[agent.node_id].allocator["state"] == "accepting"
        assert client.get("/v1/models").json()["data"]


def test_deferred_recovered_child_on_new_port_fences_stale_ready_route(
    tmp_path,
    monkeypatch,
):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    with TestClient(app) as client:
        enable_automatic(client)
        agent, managed, backend, _ = make_agent(tmp_path, client)
        agent.heartbeat_once()
        assert managed.wait_idle(1)
        agent.heartbeat_once()
        assert client.put(
            "/allocator/mode",
            headers={"X-Grid-Allocator-Token": "secret"},
            json={"mode": "observe"},
        ).status_code == 200

        prior = managed.residencies[0]
        assert prior.handle is not None
        old_port = prior.handle.port
        replacement = RuntimeHandle(prior.handle.pid + 1, old_port + 1)
        backend.live.clear()
        backend.live[replacement.pid] = (prior.model_id, replacement.port)
        with managed._lock:
            managed._residencies[prior.model_id] = ManagedResidency(
                model_id=prior.model_id,
                memory_mb=prior.memory_mb,
                state=ResidencyState.READY,
                loaded_at=prior.loaded_at,
                last_used_at=prior.last_used_at,
                load_failures=prior.load_failures,
                pinned=prior.pinned,
                handle=replacement,
            )

        now = [100.0]
        consume_deadline = True

        def health_recovery_then_deadline(*, deadline=None, **_kwargs) -> bool:
            nonlocal consume_deadline
            assert deadline is not None
            if consume_deadline:
                consume_deadline = False
                now[0] = deadline
            return False

        agent._monotonic = lambda: now[0]
        monkeypatch.setattr(managed, "reconcile_process_health", health_recovery_then_deadline)
        agent.heartbeat_once()

        engine_id = engine_node_id(managed.host_id, prior.model_id)
        stale_engine = app.state.nodes[engine_id]
        assert stale_engine.endpoint_url.endswith(f":{old_port}/v1")
        assert stale_engine.allocator["state"] == "accepting"
        assert app.state.nodes[agent.node_id].allocator["state"] == "draining"
        assert client.get("/v1/models").json()["data"] == []
        assert agent._registry_cleanup_fenced

        now[0] += 0.1
        agent.heartbeat_once()

        current_engine = app.state.nodes[engine_id]
        assert current_engine.endpoint_url.endswith(f":{replacement.port}/v1")
        assert current_engine.allocator["state"] == "accepting"
        assert app.state.nodes[agent.node_id].allocator["state"] == "accepting"
        assert not agent._registry_cleanup_fenced
        assert client.get("/v1/models").json()["data"]


def test_non_loopback_plain_http_requires_explicit_opt_in(tmp_path):
    managed = ManagedModelRuntime(
        tmp_path / "transport.json",
        host_id="host-a",
        backend=FakeBackend(),
        port_available=lambda _port: True,
    )
    token = mint_node_token("secret", "host-a")
    with pytest.raises(ValueError, match="non-loopback HTTP"):
        AllocatorNodeAgent(
            grid_url="http://10.0.0.9:8080",
            control_token=token,
            runtime=managed,
        )
    agent = AllocatorNodeAgent(
        grid_url="http://10.0.0.9:8080",
        control_token=token,
        runtime=managed,
        advertise_host="10.0.0.5",
        allow_insecure_http=True,
    )
    assert agent.grid_url == "http://10.0.0.9:8080"


def test_heartbeat_timing_must_fit_lease_and_runs_on_fixed_start_period(
    tmp_path,
    monkeypatch,
):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    with TestClient(app) as client:
        managed = ManagedModelRuntime(
            tmp_path / "timing-validation.json",
            host_id="host-a",
            backend=FakeBackend(),
            port_available=lambda _port: True,
        )
        common = {
            "grid_url": "http://testserver",
            "control_token": mint_node_token("secret", "host-a"),
            "runtime": managed,
            "advertise_host": "10.0.0.5",
            "client": client,
            "allow_insecure_http": True,
        }
        for unsafe_ttl in (0, 59.999):
            with pytest.raises(ValueError, match="between 60 and 300"):
                AllocatorNodeAgent(
                    **common,
                    registry_ttl_seconds=unsafe_ttl,
                )
        with pytest.raises(ValueError, match="shorter than the registry TTL"):
            AllocatorNodeAgent(
                **common,
                heartbeat_interval=60,
                registry_ttl_seconds=60,
            )
        with pytest.raises(ValueError, match="shorter than the registry TTL"):
            AllocatorNodeAgent(
                **common,
                heartbeat_interval=1,
                heartbeat_cycle_timeout=60,
                registry_ttl_seconds=60,
            )

        agent = AllocatorNodeAgent(
            **common,
            heartbeat_interval=1,
            heartbeat_cycle_timeout=0.9,
            registry_ttl_seconds=60,
        )
        now = [0.0]
        starts: list[float] = []
        sleeps: list[float] = []

        def heartbeat() -> None:
            starts.append(now[0])
            now[0] += 0.8
            if len(starts) == 2:
                agent.request_shutdown()

        def advance(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += seconds

        agent._monotonic = lambda: now[0]
        agent._sleep = advance
        monkeypatch.setattr(agent, "heartbeat_once", heartbeat)
        monkeypatch.setattr(agent, "shutdown", lambda **_kwargs: None)

        assert agent.run_forever() == 0
        assert starts == pytest.approx([0.0, 1.0])
        assert sum(sleeps) == pytest.approx(0.2)


def test_permanent_node_credential_rejection_waits_for_registry_ttl_before_stopping(
    tmp_path,
    monkeypatch,
):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    with TestClient(app) as client:
        enable_automatic(client)
        agent, managed, backend, _ = make_agent(tmp_path, client)
        agent.heartbeat_once()
        assert managed.wait_idle(1)
        assert backend.live
        sleeps: list[float] = []
        monotonic = [0.0]

        def registry_wait(seconds: float) -> None:
            # Old records remain routable during a credential rotation, so their target must still
            # be serving until the registry's bounded expiry window has elapsed.
            assert backend.live
            assert client.get("/v1/models").json()["data"]
            sleeps.append(seconds)
            monotonic[0] += seconds

        agent._sleep = registry_wait
        agent._monotonic = lambda: monotonic[0]
        response = httpx.Response(
            403,
            request=httpx.Request("POST", "http://testserver/nodes/heartbeat"),
        )

        def rejected() -> None:
            raise httpx.HTTPStatusError("expired credential", request=response.request, response=response)

        monkeypatch.setattr(agent, "heartbeat_once", rejected)
        assert agent.run_forever() == 1
        assert sum(sleeps) == pytest.approx(60.0)
        assert max(sleeps) <= 0.25
        assert not backend.live
        # Authentication was rejected, so cleanup cannot delete these records. The server's normal
        # TTL eviction removes them; importantly, they never pointed at a dead child before expiry.
        assert app.state.nodes


def test_credential_expiry_drains_local_engine_slots_after_registry_ttl(tmp_path):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    with TestClient(app) as client:
        enable_automatic(client)
        agent, managed, backend, _ = make_agent(tmp_path, client)
        agent.heartbeat_once()
        assert managed.wait_idle(1)
        backend.active_count = 1
        monotonic = [0.0]
        stopped_busy = [False]

        def advance(seconds: float) -> None:
            monotonic[0] += seconds
            if monotonic[0] >= 7.05 and backend.active_count:
                assert backend.live
                stopped_busy[0] = True
                backend.active_count = 0

        agent.registry_ttl_seconds = 7.0
        agent._monotonic = lambda: monotonic[0]
        agent._sleep = advance
        # A repeated local stop request cannot truncate the registry expiry safety wait.
        agent.request_shutdown()
        agent.shutdown(wait_for_registry_expiry=True)

        assert stopped_busy[0]
        assert monotonic[0] >= 7.05
        assert not backend.live


def test_agent_clears_stale_local_shutdown_request_on_startup(tmp_path):
    state_path = tmp_path / "stale.json"
    request_path = shutdown_request_path(state_path)
    request_path.write_text("shutdown\n", encoding="utf-8")
    managed = ManagedModelRuntime(
        state_path,
        host_id="host-a",
        backend=FakeBackend(),
        port_available=lambda _port: True,
    )

    AllocatorNodeAgent(
        grid_url="https://grid.example",
        control_token=mint_node_token("secret", "host-a"),
        runtime=managed,
        advertise_host="10.0.0.5",
    )

    assert not request_path.exists()


def test_startup_marker_is_written_refreshed_and_removed_for_own_instance(tmp_path):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    marker_path = tmp_path / "allocator.ready.json"
    with TestClient(app) as client:
        agent, _, _, _ = make_agent(
            tmp_path,
            client,
            instance_id="instance-a",
            startup_path=marker_path,
        )
        agent.heartbeat_once()
        first = jsonio.load_json(marker_path)
        assert first["instance_id"] == "instance-a"
        assert first["pid"] > 0
        assert first["host_id"] == "host-a"
        assert first["last_seen_at"] >= first["registered_at"]

        agent.heartbeat_once()
        refreshed = jsonio.load_json(marker_path)
        assert refreshed["registered_at"] == first["registered_at"]
        assert refreshed["last_seen_at"] >= first["last_seen_at"]

        agent.shutdown(force=True)
        assert not marker_path.exists()


def test_shutdown_does_not_remove_replacement_instance_marker(tmp_path):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    marker_path = tmp_path / "allocator.ready.json"
    with TestClient(app) as client:
        agent, _, _, _ = make_agent(
            tmp_path,
            client,
            instance_id="old-instance",
            startup_path=marker_path,
        )
        agent.heartbeat_once()
        jsonio.atomic_write_json(marker_path, {"instance_id": "replacement"})

        agent.shutdown(force=True)
        assert jsonio.load_json(marker_path)["instance_id"] == "replacement"


def test_shutdown_request_must_match_running_instance(tmp_path):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    with TestClient(app) as client:
        agent, _, _, _ = make_agent(
            tmp_path,
            client,
            instance_id="current-instance",
            startup_path=tmp_path / "ready.json",
        )
        jsonio.atomic_write_json(
            agent.shutdown_request_file,
            {"instance_id": "stale-instance", "requested_at": time.time()},
        )
        assert not agent._consume_shutdown_request()
        assert not agent.shutdown_request_file.exists()

        jsonio.atomic_write_json(
            agent.shutdown_request_file,
            {"instance_id": "current-instance", "requested_at": time.time()},
        )
        assert agent._consume_shutdown_request()
        assert not agent.shutdown_request_file.exists()


def test_local_shutdown_request_interrupts_long_heartbeat_sleep(tmp_path, monkeypatch):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    with TestClient(app) as client:
        agent, managed, _, _ = make_agent(tmp_path, client)
        agent.heartbeat_interval = 60
        requested_at = 0.0

        def heartbeat() -> None:
            nonlocal requested_at
            requested_at = time.monotonic()
            agent.shutdown_request_file.write_text("shutdown\n", encoding="utf-8")

        shutdown_started = 0.0
        real_shutdown = agent.shutdown

        def shutdown(**kwargs) -> None:
            nonlocal shutdown_started
            shutdown_started = time.monotonic()
            real_shutdown(**kwargs)

        monkeypatch.setattr(agent, "heartbeat_once", heartbeat)
        monkeypatch.setattr(agent, "shutdown", shutdown)

        assert agent.run_forever() == 0
        assert 0 <= shutdown_started - requested_at <= 0.25
        assert not agent.shutdown_request_file.exists()
        assert managed.shutting_down


def test_cuda_capacity_is_stable_and_excludes_managed_usage_from_external_reserve():
    info = {
        "usable_bytes": 6_000 * 1024**2,
        "backend": "cuda",
        "machine": {"platform": "linux"},
        "gpus": [{"memory_total_mb": 16_000, "memory_used_mb": 10_000}],
    }
    residency = ManagedResidency(
        "qwen.gguf",
        8_000,
        ResidencyState.READY,
        handle=RuntimeHandle(123, 20_000),
    )

    initial = _allocator_resources(info, (residency,))
    busier = _allocator_resources(
        {**info, "gpus": [{"memory_total_mb": 16_000, "memory_used_mb": 12_000}]},
        (residency,),
    )

    assert initial["capacity_mb"] == 16_000
    assert initial["reserved_mb"] == 2_000
    assert initial["available_mb"] == 6_000
    assert initial["gpu_count"] == 1
    assert initial["gpu_memory_mb"] == [16_000]
    assert busier["capacity_mb"] == 16_000
    assert busier["reserved_mb"] == 4_000
    assert busier["available_mb"] == 4_000


def test_cpu_capacity_uses_stable_physical_total_and_dynamic_external_reserve():
    info = {
        "usable_bytes": 1,
        "backend": "cpu",
        "machine": {"platform": "linux"},
        "memory": {"total_gb": 64, "available_gb": 40},
    }
    residency = ManagedResidency(
        "qwen.gguf",
        8_000,
        ResidencyState.READY,
        handle=RuntimeHandle(123, 20_000),
    )

    advertised = _allocator_resources(info, (residency,))

    assert advertised["capacity_mb"] == 55_705
    assert advertised["reserved_mb"] == 6_745
    assert advertised["available_mb"] == 40 * 1024
    assert (
        advertised["capacity_mb"]
        - advertised["reserved_mb"]
        - residency.memory_mb
    ) == advertised["available_mb"]


def test_resource_collector_can_partition_one_machine_into_failure_domains():
    advertised = _allocator_resources(
        {
            "usable_bytes": 8 * 1024**3,
            "backend": "metal",
            "machine": {"platform": "macos-arm64"},
            "memory": {"total_gb": 8, "available_gb": 7},
            "failure_domain": "logical-node-3",
            "mem_bandwidth_gbps": 400,
            "compute_gflops": 27_132,
            "cost_per_hour": 0.25,
            "host_priority": 2,
        }
    )

    assert advertised["failure_domain"] == "logical-node-3"
    assert advertised["memory_bandwidth_gbps"] == 400
    assert advertised["compute_gflops"] == 27_132
    assert advertised["cost_per_hour"] == 0.25
    assert advertised["host_priority"] == 2
    assert advertised["gpu_count"] == 1
    assert advertised["gpu_memory_mb"] == [advertised["capacity_mb"]]


def test_node_rejects_warm_when_local_capacity_drops_after_global_plan(
    tmp_path,
    monkeypatch,
):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )

    with TestClient(app) as client:
        enable_automatic(client)
        agent, managed, backend, _ = make_agent(tmp_path, client)
        original_post = agent._post_control

        def post_then_observe_capacity_drop(*, deadline=None):
            payload, sent = original_post(deadline=deadline)
            if ((payload.get("allocator") or {}).get("commands") or ()):
                assert agent._resources is not None
                agent._resources["available_mb"] = 4_000
            return payload, sent

        monkeypatch.setattr(agent, "_post_control", post_then_observe_capacity_drop)
        agent.heartbeat_once()

        assert managed.wait_idle(1)
        assert backend.starts == 0
        assert managed.residencies == ()
        history = client.get("/allocator/status").json()["history"]
        assert any(
            item["status"] == "failed"
            and "local capacity changed before warm" in item["message"]
            for item in history
        )


def test_proxy_last_used_is_returned_to_and_persisted_by_node_runtime(tmp_path):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    with TestClient(app) as client:
        enable_automatic(client)
        agent, managed, _, _ = make_agent(tmp_path, client)
        agent.heartbeat_once()
        assert managed.wait_idle(1)
        agent.heartbeat_once()
        engine_id = engine_node_id("host-a", "qwen.gguf")
        used_at = time.time() + 5
        app.state.nodes[engine_id].model_last_used_at["qwen.gguf"] = used_at

        agent.heartbeat_once()

        assert managed.residencies[0].last_used_at == used_at
        persisted = jsonio.load_json(managed.state_path)
        assert persisted["residencies"][0]["last_used_at"] == used_at


def test_local_safety_fence_is_published_even_when_its_state_write_fails(
    tmp_path,
    monkeypatch,
):
    app = create_app(
        grid_id="grid",
        grid_name="test",
        allocator_control_token="secret",
        allocator_interval_seconds=3_600,
    )
    with TestClient(app) as client:
        collector = StaticCollector()
        collector.signals = HostSignals(
            timestamp=time.time(),
            network_available=False,
        )
        agent, managed, _, _ = make_agent(
            tmp_path,
            client,
            collector=collector,
        )
        monkeypatch.setattr(
            managed,
            "_save_locked",
            lambda: (_ for _ in ()).throw(OSError("disk full")),
        )

        agent.heartbeat_once()

        control = app.state.nodes[agent.node_id]
        assert control.allocator["decision"]["state"] == "unhealthy"
        assert "disk full" in agent.last_error
