"""Persistent single-machine Grid made of isolated logical allocator hosts.

This is the worker behind ``grid test start``.  It intentionally uses the same real
``ManagedModelRuntime`` and ``AllocatorNodeAgent`` classes as a physical deployment; only host
identity, idle signals, failure domains, and the physical-capacity share are simulated.
"""

from __future__ import annotations

import argparse
import math
import signal
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import httpx
import uvicorn

from local.allocator_node import AllocatorNodeAgent
from local.server import create_app
from shared import jsonio, paths
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
    physical: dict[str, Any],
    *,
    machine_index: int,
    machine_count: int,
    capacity_bytes: int | None = None,
    cost_per_hour: float = 0.0,
) -> Callable[[], dict[str, Any]]:
    """Partition one machine's capacity instead of multiplying it by the logical host count."""

    machine = dict(physical.get("machine") or {})
    memory = dict(physical.get("memory") or {})
    physical_usable = max(1, int(physical.get("usable_bytes") or 0))
    usable_bytes = (
        max(1, int(capacity_bytes))
        if capacity_bytes is not None
        else max(1, physical_usable // machine_count)
    )
    capacity_fraction = min(1.0, usable_bytes / physical_usable)
    total_gb = max(
        1.0,
        (
            usable_bytes / (1024**3)
            if capacity_bytes is not None
            else float(memory.get("total_gb") or 0) / machine_count
        ),
    )
    available_gb = min(
        total_gb,
        max(0.0, float(memory.get("available_gb") or 0) * capacity_fraction),
    )

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
            "cost_per_hour": cost_per_hour,
        }

    return collect


def _profile(
    model: str,
    machines: int,
    artifact_sha256: str,
    *,
    min_replicas: int | None = None,
    workload_scores: tuple[tuple[str, float], ...] = (),
    memory_mb: int = 256,
) -> ModelProfile:
    minimum = machines if min_replicas is None else min_replicas
    return ModelProfile(
        model_id=model,
        # Includes runtime/KV overhead above the default tiny model's ~94 MB weights.
        memory_mb=memory_mb,
        runtimes=("llama.cpp",),
        min_replicas=minimum,
        max_replicas=machines,
        target_utilization=0.70,
        expected_service_seconds=5,
        latency_slo_ms=10_000,
        min_residency_seconds=0,
        # Keep observed pressure alive long enough for a real staged drain/unload/load/warm
        # replacement to complete. The demo later shortens this policy and lets it expire; it
        # never injects or clears demand.
        scale_down_cooldown_seconds=300,
        min_failure_domains=max(1, minimum),
        artifact_sha256=artifact_sha256,
        workload_scores=workload_scores,
    )


def _estimated_model_memory_mb(model_id: str) -> int:
    """Conservative real-weight footprint for logical admission and competition tests."""

    model_path = paths.models_dir() / Path(model_id).name
    try:
        weight_mb = model_path.stat().st_size / (1024 * 1024)
    except OSError as exc:
        raise RuntimeError(f"cannot size cached model {model_id!r}: {exc}") from exc
    return max(256, math.ceil(weight_mb * 1.25 + 128))


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
    include_comfyui = bool(cfg.get("include_comfyui", False))
    media_bundle = str(cfg.get("media_bundle") or "")
    comfyui_port = int(cfg.get("comfyui_port") or 22_200)
    media_port = int(cfg.get("media_port") or 22_201)
    text_machines = machines - int(include_comfyui)
    text_capacities_gib = tuple(float(item) for item in cfg.get("text_capacities_gib") or ())
    text_costs_per_hour = tuple(float(item) for item in cfg.get("text_costs_per_hour") or ())
    if text_machines < 1:
        raise RuntimeError("a real logical Grid needs at least one text machine")
    model = str(cfg["model"])
    portfolio_model = str(cfg.get("portfolio_model") or "")
    portfolio_models = tuple(
        dict.fromkeys(
            str(item)
            for item in (
                cfg.get("portfolio_models")
                or ([portfolio_model] if portfolio_model else [])
            )
            if str(item)
        )
    )
    workload_models = {
        str(workload): str(candidate)
        for workload, candidate in dict(cfg.get("workload_models") or {}).items()
    }
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
    media_proc = None
    comfyui_started = False
    media_node_id = "logical-media-1"
    media_registration: dict[str, Any] | None = None

    try:
        server_thread.start()
        _wait_for_server(server, server_thread, min(15.0, startup_timeout))

        physical = collect_device_info()
        physical_usable_bytes = max(1, int(physical.get("usable_bytes") or 0))
        media_memory_mb = 20_480 if media_bundle == "z_image" else 32_768
        media_capacity_bytes = (
            media_memory_mb * 1024 * 1024 if include_comfyui else 0
        )
        minimum_text_capacity = text_machines * 256 * 1024 * 1024
        if (
            include_comfyui
            and media_capacity_bytes + minimum_text_capacity > physical_usable_bytes
        ):
            raise RuntimeError(
                f"physical usable memory cannot fit {media_bundle} plus {text_machines} text slots"
            )
        configured_text_capacity = tuple(
            int(value * 1024**3) for value in text_capacities_gib
        )
        if configured_text_capacity:
            if len(configured_text_capacity) != text_machines:
                raise RuntimeError("configured text capacity count does not match text nodes")
            if sum(configured_text_capacity) + media_capacity_bytes > physical_usable_bytes:
                raise RuntimeError("configured logical capacities exceed physical usable memory")
            text_capacity_bytes = configured_text_capacity
        else:
            balanced = (
                (physical_usable_bytes - media_capacity_bytes) // text_machines
                if include_comfyui
                else physical_usable_bytes // text_machines
            )
            text_capacity_bytes = (balanced,) * text_machines
        artifact_sha256 = LlamaCppBackend().artifact_sha256(model)
        portfolio_sha256 = {
            candidate: LlamaCppBackend().artifact_sha256(candidate)
            for candidate in portfolio_models
        }
        for index in range(text_machines):
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
                    capacity_bytes=text_capacity_bytes[index],
                    cost_per_hour=(
                        text_costs_per_hour[index] if text_costs_per_hour else 0.0
                    ),
                ),
                heartbeat_interval=0.25,
                shutdown_drain_timeout=5.0,
                allow_insecure_http=True,
            )
            runtimes.append(runtime)
            agents.append(agent)

        if include_comfyui:
            from local import media_engine

            prepared = media_engine.prepare_media_engine(
                media_bundles=[media_bundle],
                comfyui_port=comfyui_port,
                media_port=media_port,
                advertise_host="127.0.0.1",
            )
            media_proc = prepared["proc"]
            comfyui_started = bool(prepared["comfyui_started"])
            media_models = list(prepared["models"])
            # Krea's task name and model name are aliases for the same resident graph.  A logical
            # test node must account for that graph once, not report two separately allocated
            # models that each consume the full bundle memory.
            if media_bundle == "image_generation":
                media_models = ["comfyui:image_generation"]
            capacity_mb = media_memory_mb
            backend = str(physical.get("backend") or "mps")
            if backend == "metal":
                backend = "mps"
            media_registration = {
                "role": "engine",
                "models": media_models,
                "media_url": str(prepared["media_url"]),
                "name": media_node_id,
                "pricing": {},
                "capabilities": {
                    "schema_version": 1,
                    "models": {
                        model_id: {
                            "endpoints": ["media"],
                            "input_modalities": ["text"],
                            "output_modalities": ["image"],
                            "features": {},
                        }
                        for model_id in media_models
                    },
                },
                "load": {"active_tasks": 0, "max_concurrency": 1},
                "resources": {
                    "capacity_mb": capacity_mb,
                    "runtimes": ["comfyui"],
                    "backends": [backend],
                    "gpu_count": 1,
                    "gpu_memory_mb": [capacity_mb],
                    "failure_domain": "logical-machine-media-1",
                    "tags": ["logical-test", "comfyui", backend],
                },
            }

        headers = {"X-Grid-Allocator-Token": control_token}
        with httpx.Client(base_url=endpoint, timeout=10.0, trust_env=False) as client:
            # Logical test economics are operator fixtures, not worker claims. Register them via
            # the same authenticated control surface a real Grid operator uses.
            for index in range(text_machines):
                response = client.put(
                    f"/allocator/hosts/logical-host-{index + 1}/price",
                    headers=headers,
                    json={
                        "cost_per_hour": (
                            text_costs_per_hour[index] if text_costs_per_hour else 0.0
                        )
                    },
                )
                response.raise_for_status()
            response = client.put(
                f"/allocator/models/{model}",
                headers=headers,
                json=_profile(model, text_machines, artifact_sha256).to_dict(),
            )
            response.raise_for_status()
            for candidate in portfolio_models:
                workload_scores = tuple(
                    sorted(
                        (workload, 1.0)
                        for workload, configured_model in workload_models.items()
                        if configured_model == candidate
                    )
                ) or (("coding", 1.0),)
                response = client.put(
                    f"/allocator/models/{quote(candidate, safe='')}",
                    headers=headers,
                    json=_profile(
                        candidate,
                        text_machines,
                        portfolio_sha256[candidate],
                        min_replicas=0,
                        workload_scores=workload_scores,
                        memory_mb=_estimated_model_memory_mb(candidate),
                    ).to_dict(),
                )
                response.raise_for_status()
            if media_registration is not None:
                response = client.put(f"/nodes/{media_node_id}", json=media_registration)
                response.raise_for_status()
                for media_model in media_registration["models"]:
                    response = client.put(
                        f"/allocator/models/{quote(str(media_model), safe='')}",
                        headers=headers,
                        json=ModelProfile(
                            model_id=str(media_model),
                            memory_mb=media_memory_mb,
                            runtimes=("comfyui",),
                            backends=(str(media_registration["resources"]["backends"][0]),),
                            min_replicas=1,
                            max_replicas=1,
                            expected_service_seconds=60.0,
                            latency_slo_ms=300_000.0,
                            min_residency_seconds=0,
                            scale_down_cooldown_seconds=300,
                            min_failure_domains=1,
                            max_colocated_models=1,
                            workload_scores=(("image", 1.0), ("design", 0.5)),
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
        next_media_heartbeat = 0.0
        while not stop_event.wait(0.1):
            alive = sum(thread.is_alive() for thread in agent_threads)
            if alive != text_machines:
                raise RuntimeError(
                    f"a logical node agent exited unexpectedly ({alive}/{text_machines} remain)"
                )
            if media_proc is not None and media_proc.poll() is not None:
                raise RuntimeError(
                    "the logical ComfyUI media adapter exited unexpectedly "
                    f"(code {media_proc.returncode})"
                )
            if media_registration is not None and time.monotonic() >= next_media_heartbeat:
                with httpx.Client(base_url=endpoint, timeout=10.0, trust_env=False) as client:
                    response = client.post(
                        "/nodes/heartbeat",
                        json={"node_id": media_node_id, "load": media_registration["load"]},
                    )
                    if response.status_code == 404:
                        response = client.put(
                            f"/nodes/{media_node_id}", json=media_registration
                        )
                    response.raise_for_status()
                next_media_heartbeat = time.monotonic() + 5.0
            ready = _ready_count(runtimes, model)
            if ready == text_machines and not ready_written:
                jsonio.atomic_write_json(
                    run_dir / "ready.json",
                    {
                        "endpoint": endpoint,
                        "machines": machines,
                        "text_machines": text_machines,
                        "include_comfyui": include_comfyui,
                        "media_bundle": media_bundle,
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
                    f"timed out waiting for {text_machines} ready replicas; "
                    f"{_ready_count(runtimes, model)} ready"
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
        if media_registration is not None:
            try:
                httpx.delete(f"{endpoint}/nodes/{media_node_id}", timeout=2.0)
            except Exception:
                pass
        if media_proc is not None:
            from local import media_runtime

            media_runtime.stop_media_server(media_proc)
        if comfyui_started:
            from shared.engine import comfyui

            try:
                # The fixture started this child in this process, so use the owned handle and
                # wait for termination. `stop_running` is the cross-process CLI fallback and only
                # sends a signal; using it here could report fixture shutdown while MPS memory was
                # still being released.
                comfyui.stop()
            except OSError:
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
