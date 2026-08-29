"""Persistent single-machine Grid made of isolated logical allocator hosts.

This is the worker behind ``grid test start``.  It intentionally uses the same real
``ManagedModelRuntime`` and ``AllocatorNodeAgent`` classes as a physical deployment; only host
identity, idle signals, failure domains, and the physical-capacity share are simulated.
"""

from __future__ import annotations

import argparse
import math
import secrets
import signal
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

import httpx
import uvicorn
from fastapi import HTTPException, Request

from local.allocator_node import AllocatorNodeAgent
from local import server as server_module
from local.server import create_app
from shared import jsonio
from shared.allocator.auth import mint_node_token
from shared.allocator.intelligence import classify_request
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


def _profile(
    model: str,
    machines: int,
    artifact_sha256: str,
    *,
    min_replicas: int | None = None,
    workload_scores: tuple[tuple[str, float], ...] = (),
) -> ModelProfile:
    minimum = machines if min_replicas is None else min_replicas
    return ModelProfile(
        model_id=model,
        # Includes runtime/KV overhead above the default tiny model's ~94 MB weights.
        memory_mb=256,
        runtimes=("llama.cpp",),
        min_replicas=minimum,
        max_replicas=machines,
        target_utilization=0.70,
        expected_service_seconds=5,
        latency_slo_ms=10_000,
        min_residency_seconds=0,
        # The demo clears demand explicitly. Keep observed pressure alive long enough for a real
        # staged drain/unload/load/warm replacement to complete.
        scale_down_cooldown_seconds=300,
        min_failure_domains=max(1, minimum),
        artifact_sha256=artifact_sha256,
        workload_scores=workload_scores,
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


def install_demand_simulation_routes(app, *, model: str, control_token: str) -> None:
    """Add authenticated synthetic telemetry controls only to the development fixture."""

    def require_control(request: Request) -> None:
        supplied = request.headers.get("X-Grid-Allocator-Token", "")
        if not supplied or not secrets.compare_digest(supplied, control_token):
            raise HTTPException(status_code=403, detail="allocator control token is required")

    @app.post("/test/demand")
    async def inject_demand(request: Request):
        require_control(request)
        try:
            body = await request.json()
            requested_model = str(body.get("model") or model)
            requests = body.get("requests", 60)
            service_seconds = float(body.get("service_seconds", 5.0))
            latency_ms = float(body.get("latency_ms", service_seconds * 1_000.0))
            queue_depth = body.get("queue_depth", 0)
        except (AttributeError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="demand body is invalid") from exc
        if requested_model != model:
            raise HTTPException(status_code=400, detail=f"test Grid model is {model!r}")
        if (
            isinstance(requests, bool)
            or not isinstance(requests, int)
            or not 1 <= requests <= 10_000
            or isinstance(queue_depth, bool)
            or not isinstance(queue_depth, int)
            or not 0 <= queue_depth <= 1_000_000
            or not math.isfinite(service_seconds)
            or service_seconds < 0
            or not math.isfinite(latency_ms)
            or latency_ms < 0
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "requests must be 1–10000, queue_depth must be non-negative, and timing "
                    "values must be finite and non-negative"
                ),
            )
        accepted = 0
        for _ in range(requests):
            accepted += int(
                app.state.allocator.observe(
                    model,
                    service_seconds=service_seconds,
                    latency_ms=latency_ms,
                    queue_depth=queue_depth,
                )
            )
        if accepted != requests:
            raise HTTPException(status_code=409, detail="model profile is not accepting demand")
        server_module._mark_allocator_dirty(app)
        forecast = app.state.allocator.demand.forecast(model)
        return {"accepted": accepted, "forecast": asdict(forecast)}

    @app.delete("/test/demand")
    async def clear_demand(request: Request):
        require_control(request)
        with app.state.allocator._demand_lock:
            app.state.allocator.demand.clear(model)
            app.state.allocator.intelligence.clear()
        server_module._mark_allocator_dirty(app)
        return {"cleared": model}

    @app.post("/test/exchanges")
    async def inject_exchanges(request: Request):
        """Replay request/response lifecycles; the allocator must infer the workload itself."""

        require_control(request)
        try:
            body = await request.json()
            requests = body.get("requests", 3)
            request_body = body.get("request") or {}
            endpoint_path = str(body.get("endpoint") or "chat/completions")
            service_seconds = float(body.get("service_seconds", 5.0))
            output_units = body.get("output_units", 128)
            error = bool(body.get("error", False))
        except (AttributeError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="exchange body is invalid") from exc
        if (
            isinstance(requests, bool)
            or not isinstance(requests, int)
            or not 1 <= requests <= 10_000
            or not isinstance(request_body, dict)
            or isinstance(output_units, bool)
            or not isinstance(output_units, int)
            or not 0 <= output_units <= 1_000_000_000
            or not math.isfinite(service_seconds)
            or service_seconds < 0
        ):
            raise HTTPException(status_code=400, detail="exchange values are outside test bounds")
        features = classify_request(endpoint_path, request_body)
        for _ in range(requests):
            app.state.allocator.observe_lifecycle(
                features,
                service_seconds=service_seconds,
                error=error,
                output_units=output_units,
            )
        server_module._mark_allocator_dirty(app)
        now = time.time()
        return {
            "accepted": requests,
            "features": asdict(features),
            "workloads": [
                {**asdict(item), "workload": item.model_id}
                for item in app.state.allocator.intelligence.workload_forecasts(now=now)
            ],
            "portfolio": list(
                app.state.allocator.intelligence.projections(
                    app.state.allocator.profiles,
                    now=now,
                )
            ),
        }


def run_worker(config_path: Path) -> int:
    cfg = jsonio.load_json(config_path)
    run_dir = config_path.parent
    machines = int(cfg["machines"])
    model = str(cfg["model"])
    portfolio_model = str(cfg.get("portfolio_model") or "")
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
    install_demand_simulation_routes(app, model=model, control_token=control_token)
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
        portfolio_sha256 = (
            LlamaCppBackend().artifact_sha256(portfolio_model) if portfolio_model else ""
        )
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
                # One model slot per logical machine makes portfolio trade-offs visible: the
                # allocator must choose which model occupies each simulated host.
                port_end=node_port_start,
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
            if portfolio_model:
                response = client.put(
                    f"/allocator/models/{portfolio_model}",
                    headers=headers,
                    json=_profile(
                        portfolio_model,
                        machines,
                        portfolio_sha256,
                        min_replicas=0,
                        workload_scores=(("coding", 1.0), ("research", 0.8)),
                    ).to_dict(),
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
