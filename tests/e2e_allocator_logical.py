"""Manual real-engine exercise for several logical allocator hosts on one machine.

Run from the repository root, for example::

    uv run python tests/e2e_allocator_logical.py --nodes 2
    uv run python tests/e2e_allocator_logical.py --nodes 4

The harness uses one in-process signaling server but gives every logical host a durable state file,
stable host id, failure domain, engine credential, and non-overlapping llama.cpp port range. Model
processes are real; only host-idle signals and the physical-capacity partition are simulated.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi.testclient import TestClient

from local import server as server_module
from local.allocator_node import AllocatorNodeAgent
from local.server import create_app
from shared.allocator.auth import engine_node_id, mint_node_token
from shared.allocator.local import HostPolicy, LocalHostProtectionLoop
from shared.allocator.models import ResidencyState
from shared.allocator.runtime import LlamaCppBackend, ManagedModelRuntime
from shared.system.device_info import collect_device_info
from shared.system.hostsignals import HostSignals

DEFAULT_MODEL = "SmolLM2-135M-Instruct-Q3_K_M.gguf"
CONTROL_TOKEN = "logical-fleet-control-token"


@dataclass
class LogicalHost:
    agent: AllocatorNodeAgent
    runtime: ManagedModelRuntime
    signals: "IdleSignals"


class IdleSignals:
    """Make each logical partition behave like an unattended machine."""

    def __init__(self) -> None:
        self.user_active = False

    def collect(self) -> HostSignals:
        return HostSignals(
            timestamp=time.time(),
            battery_percent=100,
            on_battery=False,
            battery_charging=True,
            idle_seconds=0 if self.user_active else 3_600,
            user_active=self.user_active,
            temperature_celsius=42,
            cpu_utilization_percent=5,
            load_per_cpu=0.05,
            memory_percent=20,
            network_available=True,
        )


def _logical_resources(
    physical: dict[str, Any],
    *,
    node_index: int,
    node_count: int,
) -> Callable[[], dict[str, Any]]:
    machine = dict(physical.get("machine") or {})
    memory = dict(physical.get("memory") or {})
    total_gb = max(4.0, float(memory.get("total_gb") or 0) / node_count)
    available_gb = min(
        total_gb,
        max(0.0, float(memory.get("available_gb") or 0) / node_count),
    )
    usable_bytes = max(0, int(physical.get("usable_bytes") or 0) // node_count)

    def collect() -> dict[str, Any]:
        return {
            "usable_bytes": usable_bytes,
            "backend": str(physical.get("backend") or "cpu"),
            "machine": machine,
            "memory": {"total_gb": total_gb, "available_gb": available_gb},
            # Partition performance proxies with capacity. Otherwise N logical hosts would make
            # this one Mac appear N times faster to the placement scorer.
            "mem_bandwidth_gbps": max(
                0.0,
                float(physical.get("mem_bandwidth_gbps") or 0) / node_count,
            ),
            "compute_gflops": max(
                0.0,
                float(physical.get("compute_gflops") or 0) / node_count,
            ),
            "failure_domain": f"logical-machine-{node_index + 1}",
        }

    return collect


def _profile(
    model: str,
    replicas: int,
    cooldown_seconds: float,
    *,
    memory_mb: int = 256,
    artifact_sha256: str = "",
    max_colocated_models: int = 0,
    priority: int = 100,
) -> dict[str, Any]:
    return {
        # Includes ample runtime/KV overhead above this model's ~94 MB weights.
        "memory_mb": memory_mb,
        "artifact_sha256": artifact_sha256,
        "max_colocated_models": max_colocated_models,
        "runtimes": ["llama.cpp"],
        # Empty means CPU and Metal logical hosts can share this physical-Mac trial profile.
        "backends": [],
        "min_replicas": 0,
        "max_replicas": replicas,
        "target_utilization": 0.70,
        "expected_service_seconds": 5,
        "latency_slo_ms": 10_000,
        "priority": priority,
        "min_residency_seconds": 0,
        "scale_down_cooldown_seconds": cooldown_seconds,
        "min_failure_domains": replicas,
    }


def _ready_hosts(hosts: list[LogicalHost], model: str) -> list[LogicalHost]:
    return [
        host
        for host in hosts
        if any(
            item.model_id == model
            and item.state == ResidencyState.READY
            and item.handle is not None
            for item in host.runtime.residencies
        )
    ]


def _unloaded(hosts: list[LogicalHost], model: str) -> bool:
    return all(
        all(
            item.model_id != model
            or (item.state == ResidencyState.CACHED and item.handle is None)
            for item in host.runtime.residencies
        )
        for host in hosts
    )


def _tick(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/allocator/tick",
        headers={"X-Grid-Allocator-Token": CONTROL_TOKEN},
    )
    response.raise_for_status()
    return response.json()


def _drive_until(
    client: TestClient,
    hosts: list[LogicalHost],
    predicate: Callable[[], bool],
    *,
    timeout: float,
    label: str,
) -> int:
    deadline = time.monotonic() + timeout
    cycles = 0
    while time.monotonic() < deadline:
        _tick(client)
        for host in hosts:
            host.agent.heartbeat_once()
        cycles += 1
        if predicate():
            # One final heartbeat publishes terminal receipts and exact route state.
            for host in hosts:
                host.runtime.wait_idle(0.2)
                host.agent.heartbeat_once()
            _tick(client)
            return cycles
        time.sleep(0.1)
    status = client.get("/allocator/status").json()
    raise RuntimeError(
        f"timed out waiting for {label}: "
        f"{json.dumps({'status': status, 'nodes': _host_summary(hosts)}, default=str)}"
    )


def _host_summary(hosts: list[LogicalHost]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for host in hosts:
        rows.append(
            {
                "host_id": host.runtime.host_id,
                "port_range": [host.runtime.port_start, host.runtime.port_end],
                "residencies": [
                    {
                        "model": item.model_id,
                        "state": item.state.value,
                        "pid": item.handle.pid if item.handle else None,
                        "port": item.handle.port if item.handle else None,
                        "artifact_sha256": item.artifact_sha256,
                    }
                    for item in host.runtime.residencies
                ],
                "last_error": host.agent.last_error,
            }
        )
    return rows


def _performance_summary(client: TestClient) -> dict[str, dict[str, float]]:
    return {
        row["node_id"]: {
            "latency_ms": float(row.get("latency_ms") or 0),
            "tokens_per_second": float(row.get("tokens_per_second") or 0),
        }
        for row in client.get("/allocator/status").json().get("nodes") or []
        if row.get("latency_ms") or row.get("tokens_per_second")
    }


def _require_measured_performance(client: TestClient) -> dict[str, dict[str, float]]:
    measured = _performance_summary(client)
    if not any(
        row["latency_ms"] > 0 and row["tokens_per_second"] > 0
        for row in measured.values()
    ):
        raise RuntimeError(f"real inference produced no allocator performance signal: {measured}")
    return measured


def _require_learned_warm_times(
    client: TestClient,
    *,
    model: str,
    replicas: int,
) -> list[dict[str, Any]]:
    learned = [
        row
        for row in client.get("/allocator/status").json().get(
            "learned_warm_seconds"
        )
        or []
        if row.get("model_id") == model
    ]
    if len(learned) != replicas or any(
        float(row.get("seconds") or 0) <= 0 for row in learned
    ):
        raise RuntimeError(f"real warm actions produced invalid timing signals: {learned}")
    return learned


def _affinity_key_for_host(app, *, model: str, host_id: str) -> str:
    target = engine_node_id(host_id, model)
    for index in range(10_000):
        key = f"logical-failover-{index}"
        selected = server_module._choose_engine(
            app,
            model,
            affinity_digest=server_module._affinity_digest(key),
        )
        if selected is not None and selected.node_id == target:
            return key
    raise RuntimeError(f"could not map an affinity key to logical host {host_id}")


def _stream_real_completion(client: TestClient, model: str) -> dict[str, Any]:
    """Exercise real fragmented SSE and require the runtime's final usage record."""

    completion: dict[str, Any] = {}
    completion_tokens = 0
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_tokens": 8,
            "temperature": 0,
            "stream": True,
            "stream_options": {"include_usage": True},
        },
    ) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            chunk = json.loads(payload)
            if not isinstance(chunk, dict):
                continue
            for field_name in ("id", "model"):
                if chunk.get(field_name):
                    completion[field_name] = chunk[field_name]
            usage = chunk.get("usage")
            value = usage.get("completion_tokens") if isinstance(usage, dict) else 0
            if isinstance(value, int) and not isinstance(value, bool):
                completion_tokens = max(completion_tokens, value)
    if completion_tokens <= 0:
        raise RuntimeError("real streamed inference returned no completion-token usage")
    completion["completion_tokens"] = completion_tokens
    return completion


def run(
    nodes: int,
    model: str,
    port_base: int,
    timeout: float,
    *,
    scenario: str = "lifecycle",
    second_model: str | None = None,
) -> dict[str, Any]:
    if scenario == "activity" and nodes < 2:
        raise ValueError("activity evacuation requires at least two logical nodes")
    if scenario == "contention" and (nodes < 4 or not second_model):
        raise ValueError("contention requires four nodes and --second-model")
    if scenario == "preemption" and not second_model:
        raise ValueError("preemption requires --second-model")
    desired_replicas = (
        nodes - 1
        if scenario == "activity"
        else 2
        if scenario == "contention"
        else nodes
    )
    claimed_memory_mb = (
        8_000 if scenario in ("contention", "preemption") else 256
    )
    physical = collect_device_info()
    artifact_sha256 = LlamaCppBackend().artifact_sha256(model)
    second_artifact_sha256 = (
        LlamaCppBackend().artifact_sha256(second_model)
        if second_model is not None
        else ""
    )
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix=f"grid-logical-{nodes}-") as temporary:
        root = Path(temporary)
        app = create_app(
            grid_id=f"logical-{nodes}",
            grid_name=f"logical-{nodes}",
            allocator_state_path=root / "controller.json",
            allocator_control_token=CONTROL_TOKEN,
            allocator_interval_seconds=3_600,
        )
        hosts: list[LogicalHost] = []
        with TestClient(app) as client:
            headers = {"X-Grid-Allocator-Token": CONTROL_TOKEN}
            profile_response = client.put(
                f"/allocator/models/{model}",
                headers=headers,
                # Hold the synthetic burst throughout concurrent engine startup. Scale-down is
                # enabled explicitly after all replicas are proven ready.
                json=_profile(
                    model,
                    desired_replicas,
                    cooldown_seconds=max(60.0, timeout),
                    memory_mb=claimed_memory_mb,
                    artifact_sha256=artifact_sha256,
                    max_colocated_models=(
                        1 if scenario in ("contention", "preemption") else 0
                    ),
                    priority=10 if scenario == "preemption" else 100,
                ),
            )
            profile_response.raise_for_status()
            if scenario in ("contention", "preemption"):
                second_profile = client.put(
                    f"/allocator/models/{second_model}",
                    headers=headers,
                    json=_profile(
                        second_model,
                        nodes,
                        cooldown_seconds=max(60.0, timeout),
                        memory_mb=claimed_memory_mb,
                        artifact_sha256=second_artifact_sha256,
                        max_colocated_models=1,
                        priority=1_000 if scenario == "preemption" else 100,
                    ),
                )
                second_profile.raise_for_status()
            mode_response = client.put(
                "/allocator/mode",
                headers=headers,
                json={"mode": "automatic"},
            )
            mode_response.raise_for_status()

            for index in range(nodes):
                host_id = f"logical-host-{index + 1}"
                node_port_start = port_base + index * 10
                backend = LlamaCppBackend(
                    readiness_timeout=min(120.0, timeout),
                    bind_host="127.0.0.1",
                    endpoint_host="127.0.0.1",
                )
                runtime = ManagedModelRuntime(
                    root / f"node-{index + 1}.json",
                    host_id=host_id,
                    backend=backend,
                    signal_collector=(signals := IdleSignals()),
                    protection_loop=LocalHostProtectionLoop(
                        HostPolicy(
                            pause_for_user_activity=True,
                            activity_debounce_seconds=0,
                            drain_grace_seconds=0,
                            recovery_cooldown_seconds=0,
                            activity_recovery_seconds=0,
                            thermal_recovery_seconds=0,
                        )
                    ),
                    port_start=node_port_start,
                    port_end=node_port_start + 3,
                )
                agent = AllocatorNodeAgent(
                    grid_url="http://testserver",
                    control_token=mint_node_token(CONTROL_TOKEN, host_id),
                    runtime=runtime,
                    advertise_host="127.0.0.1",
                    client=client,
                    resource_collector=_logical_resources(
                        physical,
                        node_index=index,
                        node_count=nodes,
                    ),
                    heartbeat_interval=0.25,
                    allow_insecure_http=True,
                )
                hosts.append(LogicalHost(agent, runtime, signals))

            try:
                for host in hosts:
                    host.agent.heartbeat_once()

                # A deterministic burst: one minute's worth of expensive requests in one EWMA
                # bucket. This drives the target to the configured fleet size without mocking the
                # planner or actuator.
                for _ in range(60):
                    assert app.state.allocator.observe(
                        model,
                        service_seconds=5.0,
                        latency_ms=5_000,
                    )

                warm_cycles = _drive_until(
                    client,
                    hosts,
                    lambda: len(_ready_hosts(hosts, model)) == desired_replicas,
                    timeout=timeout,
                    label=f"{desired_replicas} ready replicas",
                )
                learned_warm_seconds = _require_learned_warm_times(
                    client,
                    model=model,
                    replicas=desired_replicas,
                )

                if scenario == "preemption":
                    assert second_model is not None
                    for _ in range(60):
                        assert app.state.allocator.observe(
                            second_model,
                            service_seconds=5.0,
                            latency_ms=5_000,
                        )
                    preemption_cycles = _drive_until(
                        client,
                        hosts,
                        lambda: _unloaded(hosts, model)
                        and len(_ready_hosts(hosts, second_model)) == nodes,
                        timeout=timeout,
                        label="critical model preempts every batch logical residency",
                    )
                    learned_warm_seconds = [
                        *learned_warm_seconds,
                        *_require_learned_warm_times(
                            client,
                            model=second_model,
                            replicas=nodes,
                        ),
                    ]
                    completion = _stream_real_completion(client, second_model)
                    critical_performance = _require_measured_performance(client)

                    retire_second = client.delete(
                        f"/allocator/models/{second_model}",
                        headers=headers,
                    )
                    retire_second.raise_for_status()
                    restoration_cycles = _drive_until(
                        client,
                        hosts,
                        lambda: _unloaded(hosts, second_model)
                        and len(_ready_hosts(hosts, model)) == nodes,
                        timeout=timeout,
                        label="displaced batch service restored after critical burst",
                    )
                    restored_warm_seconds = _require_learned_warm_times(
                        client,
                        model=model,
                        replicas=nodes,
                    )
                    if any(int(row.get("samples") or 0) < 2 for row in restored_warm_seconds):
                        raise RuntimeError(
                            "restored lifecycle did not update learned warm estimates: "
                            f"{restored_warm_seconds}"
                        )
                    restored_completion = _stream_real_completion(client, model)
                    restored_performance = _require_measured_performance(client)

                    retire_first = client.delete(
                        f"/allocator/models/{model}",
                        headers=headers,
                    )
                    retire_first.raise_for_status()
                    unload_cycles = _drive_until(
                        client,
                        hosts,
                        lambda: _unloaded(hosts, model)
                        and _unloaded(hosts, second_model),
                        timeout=timeout,
                        label="preemption trial models fully offloaded",
                    )
                    status = client.get("/allocator/status").json()
                    actions = [row["kind"] for row in status.get("history") or []]
                    if not {"warm", "drain", "unload"}.issubset(actions):
                        raise RuntimeError(
                            f"incomplete preemption lifecycle history: {actions}"
                        )
                    return {
                        "nodes": nodes,
                        "scenario": scenario,
                        "batch_model": model,
                        "critical_model": second_model,
                        "preemption_cycles": preemption_cycles,
                        "restoration_cycles": restoration_cycles,
                        "unload_cycles": unload_cycles,
                        "actions": actions,
                        "completion_id": completion.get("id"),
                        "completion_model": completion.get("model"),
                        "stream_completion_tokens": completion.get(
                            "completion_tokens"
                        ),
                        "restored_completion_id": restored_completion.get("id"),
                        "restored_completion_model": restored_completion.get("model"),
                        "restored_stream_completion_tokens": restored_completion.get(
                            "completion_tokens"
                        ),
                        "learned_warm_seconds": learned_warm_seconds,
                        "restored_warm_seconds": restored_warm_seconds,
                        "critical_performance": critical_performance,
                        "restored_performance": restored_performance,
                        "hosts": _host_summary(hosts),
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                    }

                restart_adoption: dict[str, Any] | None = None
                if scenario == "restart":
                    original_pids = {
                        host.runtime.host_id: next(
                            item.handle.pid
                            for item in host.runtime.residencies
                            if item.model_id == model and item.handle is not None
                        )
                        for host in hosts
                    }
                    restarted_hosts: list[LogicalHost] = []
                    for index, old_host in enumerate(hosts):
                        backend = LlamaCppBackend(
                            readiness_timeout=min(120.0, timeout),
                            bind_host="127.0.0.1",
                            endpoint_host="127.0.0.1",
                        )
                        runtime = ManagedModelRuntime(
                            old_host.runtime.state_path,
                            host_id=old_host.runtime.host_id,
                            backend=backend,
                            signal_collector=old_host.signals,
                            protection_loop=LocalHostProtectionLoop(
                                HostPolicy(
                                    pause_for_user_activity=True,
                                    activity_debounce_seconds=0,
                                    drain_grace_seconds=0,
                                    recovery_cooldown_seconds=0,
                                    activity_recovery_seconds=0,
                                    thermal_recovery_seconds=0,
                                )
                            ),
                            port_start=old_host.runtime.port_start,
                            port_end=old_host.runtime.port_end,
                        )
                        agent = AllocatorNodeAgent(
                            grid_url="http://testserver",
                            control_token=mint_node_token(
                                CONTROL_TOKEN,
                                runtime.host_id,
                            ),
                            runtime=runtime,
                            advertise_host="127.0.0.1",
                            client=client,
                            resource_collector=_logical_resources(
                                physical,
                                node_index=index,
                                node_count=nodes,
                            ),
                            heartbeat_interval=0.25,
                            allow_insecure_http=True,
                        )
                        restarted_hosts.append(
                            LogicalHost(agent, runtime, old_host.signals)
                        )
                    hosts[:] = restarted_hosts
                    adoption_cycles = _drive_until(
                        client,
                        hosts,
                        lambda: len(_ready_hosts(hosts, model)) == desired_replicas,
                        timeout=timeout,
                        label="restarted node agents adopt live owned processes",
                    )
                    adopted_pids = {
                        host.runtime.host_id: next(
                            item.handle.pid
                            for item in host.runtime.residencies
                            if item.model_id == model and item.handle is not None
                        )
                        for host in hosts
                    }
                    if adopted_pids != original_pids:
                        raise RuntimeError(
                            "node restart respawned instead of adopting live children: "
                            f"before={original_pids}, after={adopted_pids}"
                        )
                    restart_adoption = {
                        "cycles": adoption_cycles,
                        "pids": adopted_pids,
                    }

                if scenario == "contention":
                    assert second_model is not None
                    for _ in range(60):
                        assert app.state.allocator.observe(
                            second_model,
                            service_seconds=5.0,
                            latency_ms=5_000,
                        )
                    initial_second_cycles = _drive_until(
                        client,
                        hosts,
                        lambda: len(_ready_hosts(hosts, second_model))
                        == nodes - desired_replicas,
                        timeout=timeout,
                        label="second model fills remaining logical hosts",
                    )
                    live_counts = [
                        sum(
                            item.state
                            not in (ResidencyState.CACHED, ResidencyState.FAILED)
                            for item in host.runtime.residencies
                        )
                        for host in hosts
                    ]
                    if any(count > 1 for count in live_counts):
                        raise RuntimeError(
                            f"claimed capacity overcommitted a logical host: {live_counts}"
                        )

                    retire_first = client.delete(
                        f"/allocator/models/{model}",
                        headers=headers,
                    )
                    retire_first.raise_for_status()
                    expansion_cycles = _drive_until(
                        client,
                        hosts,
                        lambda: _unloaded(hosts, model)
                        and len(_ready_hosts(hosts, second_model)) == nodes,
                        timeout=timeout,
                        label="first model offloaded before second model expands",
                    )
                    learned_warm_seconds = [
                        *learned_warm_seconds,
                        *_require_learned_warm_times(
                            client,
                            model=second_model,
                            replicas=nodes,
                        ),
                    ]

                    completion = _stream_real_completion(client, second_model)
                    measured_performance = _require_measured_performance(client)

                    retire_second = client.delete(
                        f"/allocator/models/{second_model}",
                        headers=headers,
                    )
                    retire_second.raise_for_status()
                    unload_cycles = _drive_until(
                        client,
                        hosts,
                        lambda: _unloaded(hosts, model)
                        and _unloaded(hosts, second_model),
                        timeout=timeout,
                        label="both model generations fully offloaded",
                    )
                    status = client.get("/allocator/status").json()
                    actions = [row["kind"] for row in status.get("history") or []]
                    return {
                        "nodes": nodes,
                        "scenario": scenario,
                        "models": [model, second_model],
                        "warm_cycles": warm_cycles,
                        "initial_second_cycles": initial_second_cycles,
                        "expansion_cycles": expansion_cycles,
                        "unload_cycles": unload_cycles,
                        "actions": actions,
                        "completion_id": completion.get("id"),
                        "completion_model": completion.get("model"),
                        "stream_completion_tokens": completion.get(
                            "completion_tokens"
                        ),
                        "learned_warm_seconds": learned_warm_seconds,
                        "measured_performance": measured_performance,
                        "hosts": _host_summary(hosts),
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                    }

                failure: dict[str, Any] | None = None
                route_failover: dict[str, Any] | None = None
                activity_evacuation: dict[str, Any] | None = None
                if scenario == "activity":
                    initially_ready = _ready_hosts(hosts, model)
                    victim = initially_ready[0]
                    initial_ready_ids = {
                        host.runtime.host_id for host in initially_ready
                    }
                    victim_residency = next(
                        item
                        for item in victim.runtime.residencies
                        if item.model_id == model and item.handle is not None
                    )
                    victim_pid = victim_residency.handle.pid
                    victim.signals.user_active = True

                    def evacuated() -> bool:
                        ready = _ready_hosts(hosts, model)
                        ready_ids = {host.runtime.host_id for host in ready}
                        victim_offloaded = all(
                            item.model_id != model
                            or (
                                item.state == ResidencyState.CACHED
                                and item.handle is None
                            )
                            for item in victim.runtime.residencies
                        )
                        return (
                            len(ready) == desired_replicas
                            and victim.runtime.host_id not in ready_ids
                            and bool(ready_ids - initial_ready_ids)
                            and victim_offloaded
                            and victim.runtime.decision is not None
                            and not victim.runtime.decision.accept
                        )

                    evacuation_cycles = _drive_until(
                        client,
                        hosts,
                        evacuated,
                        timeout=timeout,
                        label="active employee host evacuated to spare logical node",
                    )
                    replacement_ids = sorted(
                        {
                            host.runtime.host_id
                            for host in _ready_hosts(hosts, model)
                        }
                        - initial_ready_ids
                    )
                    activity_evacuation = {
                        "host_id": victim.runtime.host_id,
                        "displaced_pid": victim_pid,
                        "replacement_host_ids": replacement_ids,
                        "cycles": evacuation_cycles,
                        "local_state": victim.runtime.decision.state.value,
                    }
                elif nodes >= 4:
                    victim = hosts[-1]
                    old_residency = next(
                        item
                        for item in victim.runtime.residencies
                        if item.model_id == model and item.handle is not None
                    )
                    old_pid = old_residency.handle.pid
                    affinity_key = _affinity_key_for_host(
                        app,
                        model=model,
                        host_id=victim.runtime.host_id,
                    )
                    os.kill(old_pid, signal.SIGKILL)
                    time.sleep(0.05)
                    failover_responses = [
                        client.post(
                            "/v1/chat/completions",
                            headers={"X-Grid-Affinity-Key": affinity_key},
                            json={
                                "model": model,
                                "messages": [
                                    {"role": "user", "content": "Reply with OK."}
                                ],
                                "max_tokens": 1,
                            },
                        )
                        for _ in range(3)
                    ]
                    failover_statuses = [
                        response.status_code for response in failover_responses
                    ]
                    if failover_statuses != [502, 502, 200]:
                        raise RuntimeError(
                            "dead route did not fail over after its bounded threshold: "
                            f"{failover_statuses}"
                        )
                    route_failover = {
                        "host_id": victim.runtime.host_id,
                        "statuses": failover_statuses,
                    }

                    def victim_recovered() -> bool:
                        ready = _ready_hosts(hosts, model)
                        replacement = next(
                            (
                                item
                                for item in victim.runtime.residencies
                                if item.model_id == model
                                and item.state == ResidencyState.READY
                                and item.handle is not None
                                and item.handle.pid != old_pid
                            ),
                            None,
                        )
                        return len(ready) == nodes and replacement is not None

                    recovery_cycles = _drive_until(
                        client,
                        hosts,
                        victim_recovered,
                        timeout=timeout,
                        label="killed replica fenced and replaced",
                    )
                    new_residency = next(
                        item
                        for item in victim.runtime.residencies
                        if item.model_id == model
                        and item.state == ResidencyState.READY
                        and item.handle is not None
                    )
                    failure = {
                        "host_id": victim.runtime.host_id,
                        "killed_pid": old_pid,
                        "replacement_pid": new_residency.handle.pid,
                        "recovery_cycles": recovery_cycles,
                    }

                completion = _stream_real_completion(client, model)
                measured_performance = _require_measured_performance(client)

                # Demand and per-residency cooldowns are both one second. Let them expire, then
                # exercise route fencing, drain, direct-slot idleness, and real process teardown.
                scale_down_profile = client.put(
                    f"/allocator/models/{model}",
                    headers=headers,
                    json=_profile(
                        model,
                        desired_replicas,
                        cooldown_seconds=1.0,
                        memory_mb=claimed_memory_mb,
                        artifact_sha256=artifact_sha256,
                        max_colocated_models=(
                            1 if scenario in ("contention", "preemption") else 0
                        ),
                    ),
                )
                scale_down_profile.raise_for_status()
                time.sleep(1.25)
                unload_cycles = _drive_until(
                    client,
                    hosts,
                    lambda: _unloaded(hosts, model),
                    timeout=timeout,
                    label="all replicas drained and unloaded",
                )

                status = client.get("/allocator/status").json()
                actions = [row["kind"] for row in status.get("history") or []]
                if "warm" not in actions or "drain" not in actions or "unload" not in actions:
                    raise RuntimeError(f"incomplete lifecycle history: {actions}")
                if client.get("/v1/models").json().get("data"):
                    raise RuntimeError("model route remained published after unload")
                if any(host.agent.last_error for host in hosts):
                    raise RuntimeError(f"node heartbeat errors: {_host_summary(hosts)}")

                return {
                    "nodes": nodes,
                    "model": model,
                    "artifact_sha256": artifact_sha256,
                    "physical": {
                        "backend": physical.get("backend"),
                        "machine": physical.get("machine"),
                        "memory": physical.get("memory"),
                    },
                    "warm_cycles": warm_cycles,
                    "failure_recovery": failure,
                    "route_failover": route_failover,
                    "activity_evacuation": activity_evacuation,
                    "restart_adoption": restart_adoption,
                    "unload_cycles": unload_cycles,
                    "actions": actions,
                    "completion_id": completion.get("id"),
                    "completion_model": completion.get("model"),
                    "stream_completion_tokens": completion.get("completion_tokens"),
                    "learned_warm_seconds": learned_warm_seconds,
                    "measured_performance": measured_performance,
                    "hosts": _host_summary(hosts),
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
            finally:
                for host in hosts:
                    try:
                        host.runtime.begin_shutdown()
                        host.runtime.stop_all(wait_timeout=5, force=True)
                    except Exception as exc:  # noqa: BLE001 - cleanup must reach every host
                        print(
                            json.dumps(
                                {"cleanup_error": host.runtime.host_id, "detail": str(exc)}
                            )
                        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=int, choices=(2, 4), required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--port-base", type=int, default=18_100)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--scenario",
        choices=("lifecycle", "activity", "contention", "preemption", "restart"),
        default="lifecycle",
    )
    parser.add_argument("--second-model")
    args = parser.parse_args()
    print(
        json.dumps(
            run(
                args.nodes,
                args.model,
                args.port_base,
                args.timeout,
                scenario=args.scenario,
                second_model=args.second_model,
            ),
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
