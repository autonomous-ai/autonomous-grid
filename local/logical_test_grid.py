"""Persistent single-machine Grid made of isolated logical allocator hosts.

This is the worker behind ``grid test start``.  It intentionally uses the same real
``ManagedModelRuntime`` and ``AllocatorNodeAgent`` classes as a physical deployment; only host
identity, idle signals, failure domains, and the physical-capacity share are simulated.
"""

from __future__ import annotations

import argparse
import signal
import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx
import uvicorn

from local.allocator_node import AllocatorNodeAgent
from local.server import create_app
from shared import jsonio
from shared.allocator.auth import mint_node_token
from shared.allocator.local import HostPolicy, LocalHostProtectionLoop
from shared.allocator.models import ModelProfile, ResidencyState
from shared.allocator.runtime import LlamaCppBackend, ManagedModelRuntime
from shared.system.device_info import collect_device_info
from shared.system.hostsignals import HostSignals


class IdleLogicalSignals:
    """Make a logical partition behave like an unattended plugged-in machine."""

    def collect(self) -> HostSignals:
        return HostSignals(
            timestamp=time.time(),
            battery_percent=100,
            on_battery=False,
            battery_charging=True,
            idle_seconds=3_600,
            user_active=False,
            temperature_celsius=42,
            cpu_utilization_percent=5,
            load_per_cpu=0.05,
            memory_percent=20,
            network_available=True,
        )


def logical_resources(
    physical: dict[str, Any], *, machine_index: int, machine_count: int
) -> Callable[[], dict[str, Any]]:
    """Partition one machine's capacity instead of multiplying it by the logical host count."""

    machine = dict(physical.get("machine") or {})
    memory = dict(physical.get("memory") or {})
    total_gb = max(1.0, float(memory.get("total_gb") or 0) / machine_count)
    available_gb = min(
        total_gb,
        max(0.0, float(memory.get("available_gb") or 0) / machine_count),
    )
    usable_bytes = max(0, int(physical.get("usable_bytes") or 0) // machine_count)

    def collect() -> dict[str, Any]:
        return {
            "usable_bytes": usable_bytes,
            "backend": str(physical.get("backend") or "cpu"),
            "machine": machine,
            "memory": {"total_gb": total_gb, "available_gb": available_gb},
            "mem_bandwidth_gbps": max(
                0.0,
                float(physical.get("mem_bandwidth_gbps") or 0) / machine_count,
            ),
            "compute_gflops": max(
                0.0,
                float(physical.get("compute_gflops") or 0) / machine_count,
            ),
            "failure_domain": f"logical-machine-{machine_index + 1}",
        }

    return collect


def _profile(model: str, machines: int, artifact_sha256: str) -> ModelProfile:
    return ModelProfile(
        model_id=model,
        # Includes runtime/KV overhead above the default tiny model's ~94 MB weights.
        memory_mb=256,
        runtimes=("llama.cpp",),
        min_replicas=machines,
        max_replicas=machines,
        target_utilization=0.70,
        expected_service_seconds=5,
        latency_slo_ms=10_000,
        min_residency_seconds=0,
        scale_down_cooldown_seconds=1,
        min_failure_domains=machines,
        artifact_sha256=artifact_sha256,
    )


def _ready_count(runtimes: list[ManagedModelRuntime], model: str) -> int:
    return sum(
        1
        for runtime in runtimes
        if any(
            item.model_id == model
            and item.state == ResidencyState.READY
            and item.handle is not None
            for item in runtime.residencies
        )
    )


def _wait_for_server(server: uvicorn.Server, thread: threading.Thread, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.started:
            return
        if not thread.is_alive():
            raise RuntimeError("logical Grid HTTP server exited during startup")
        time.sleep(0.05)
    raise RuntimeError("timed out starting logical Grid HTTP server")


def run_worker(config_path: Path) -> int:
    cfg = jsonio.load_json(config_path)
    run_dir = config_path.parent
    machines = int(cfg["machines"])
    model = str(cfg["model"])
    control_token = Path(str(cfg["control_token_path"])).read_text(encoding="utf-8").strip()
    if not control_token:
        raise RuntimeError("logical Grid control token is empty")
    port = int(cfg["port"])
    port_base = int(cfg["engine_port_base"])
    startup_timeout = float(cfg["startup_timeout"])
    endpoint = f"http://127.0.0.1:{port}"
    stop_event = threading.Event()

    def request_stop(_signum=None, _frame=None) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    app = create_app(
        grid_id=f"logical-test-{machines}",
        grid_name=f"logical-test-{machines}",
        allocator_state_path=run_dir / "controller.json",
        allocator_control_token=control_token,
        allocator_interval_seconds=1.0,
        allocator_coalesce_seconds=0.05,
        allocator_min_tick_seconds=0.05,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
        )
    )
    server_thread = threading.Thread(
        target=server.run,
        name="logical-grid-http",
        daemon=True,
    )
    agents: list[AllocatorNodeAgent] = []
    runtimes: list[ManagedModelRuntime] = []
    agent_threads: list[threading.Thread] = []

    try:
        server_thread.start()
        _wait_for_server(server, server_thread, min(15.0, startup_timeout))

        physical = collect_device_info()
        artifact_sha256 = LlamaCppBackend().artifact_sha256(model)
        for index in range(machines):
            host_id = f"logical-host-{index + 1}"
            node_port_start = port_base + index * 10
            runtime = ManagedModelRuntime(
                run_dir / f"node-{index + 1}.json",
                host_id=host_id,
                backend=LlamaCppBackend(
                    readiness_timeout=min(120.0, startup_timeout),
                    bind_host="127.0.0.1",
                    endpoint_host="127.0.0.1",
                ),
                signal_collector=IdleLogicalSignals(),
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
                grid_url=endpoint,
                control_token=mint_node_token(control_token, host_id),
                runtime=runtime,
                advertise_host="127.0.0.1",
                resource_collector=logical_resources(
                    physical,
                    machine_index=index,
                    machine_count=machines,
                ),
                heartbeat_interval=0.25,
                shutdown_drain_timeout=5.0,
                allow_insecure_http=True,
            )
            runtimes.append(runtime)
            agents.append(agent)

        headers = {"X-Grid-Allocator-Token": control_token}
        with httpx.Client(base_url=endpoint, timeout=10.0, trust_env=False) as client:
            response = client.put(
                f"/allocator/models/{model}",
                headers=headers,
                json=_profile(model, machines, artifact_sha256).to_dict(),
            )
            response.raise_for_status()
            response = client.put(
                "/allocator/mode",
                headers=headers,
                json={"mode": "automatic"},
            )
            response.raise_for_status()

        for index, agent in enumerate(agents):
            thread = threading.Thread(
                target=agent.run_forever,
                name=f"logical-grid-node-{index + 1}",
                daemon=True,
            )
            agent_threads.append(thread)
            thread.start()

        deadline = time.monotonic() + startup_timeout
        ready_written = False
        while not stop_event.wait(0.1):
            alive = sum(thread.is_alive() for thread in agent_threads)
            if alive != machines:
                raise RuntimeError(
                    f"a logical node agent exited unexpectedly ({alive}/{machines} remain)"
                )
            ready = _ready_count(runtimes, model)
            if ready == machines and not ready_written:
                jsonio.atomic_write_json(
                    run_dir / "ready.json",
                    {
                        "endpoint": endpoint,
                        "machines": machines,
                        "model": model,
                        "ready_replicas": ready,
                        "engine_ports": [
                            next(
                                item.handle.port
                                for item in runtime.residencies
                                if item.model_id == model and item.handle is not None
                            )
                            for runtime in runtimes
                        ],
                        "started_at": time.time(),
                    },
                )
                ready_written = True
            if not ready_written and time.monotonic() >= deadline:
                raise RuntimeError(
                    f"timed out waiting for {machines} ready replicas; {_ready_count(runtimes, model)} ready"
                )
    except BaseException as exc:
        try:
            jsonio.atomic_write_json(
                run_dir / "error.json",
                {"error": f"{type(exc).__name__}: {exc}", "at": time.time()},
            )
        except OSError:
            pass
        raise
    finally:
        for agent in agents:
            agent.request_shutdown()
        for thread in agent_threads:
            thread.join(timeout=10.0)
        for thread, runtime in zip(agent_threads, runtimes, strict=True):
            if thread.is_alive():
                try:
                    runtime.begin_shutdown()
                    runtime.stop_all(wait_timeout=2.0, force=True)
                except Exception:
                    pass
        server.should_exit = True
        server_thread.join(timeout=5.0)
        (run_dir / "ready.json").unlink(missing_ok=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    return run_worker(args.config)


if __name__ == "__main__":
    raise SystemExit(main())
