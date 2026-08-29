"""Development CLI for a persistent, single-machine logical Grid."""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
import json
import os
import re
import secrets
import socket
import statistics
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import quote
import uuid

import httpx

from shared import jsonio, paths, run_records
from shared.allocator.intelligence import KNOWN_WORKLOADS
from shared.allocator.runtime import LlamaCppBackend

DEFAULT_MODEL = "SmolLM2-135M-Instruct-Q3_K_M.gguf"
DEFAULT_PORTFOLIO_MODEL = "SmolLM2-135M-Instruct-Q3_K_S.gguf"
DEFAULT_PORT = 22_100
DEFAULT_ENGINE_PORT_BASE = 22_110
MAX_LOGICAL_MACHINES = 32


def logical_machine_count(raw: str) -> int:
    try:
        machines = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{raw!r} is not a whole number of machines") from None
    if not 1 <= machines <= MAX_LOGICAL_MACHINES:
        raise argparse.ArgumentTypeError(
            f"{raw!r} must be between 1 and {MAX_LOGICAL_MACHINES} machines"
        )
    return machines


def positive_seconds(raw: str) -> float:
    try:
        seconds = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{raw!r} is not a number of seconds") from None
    if seconds <= 0:
        raise argparse.ArgumentTypeError(f"{raw!r} must be greater than zero")
    return seconds


def real_user_count(raw: str) -> int:
    try:
        users = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{raw!r} is not a whole number of users") from None
    if not 1 <= users <= 64:
        raise argparse.ArgumentTypeError(f"{raw!r} must be between 1 and 64 users")
    return users


def real_request_count(raw: str) -> int:
    try:
        requests = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{raw!r} is not a whole number of requests") from None
    if not 1 <= requests <= 1_000:
        raise argparse.ArgumentTypeError(f"{raw!r} must be between 1 and 1000 requests")
    return requests


def positive_tokens(raw: str) -> int:
    try:
        tokens = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{raw!r} is not a whole number of tokens") from None
    if not 1 <= tokens <= 4_096:
        raise argparse.ArgumentTypeError(f"{raw!r} must be between 1 and 4096 tokens")
    return tokens


def positive_gib_csv(raw: str) -> tuple[float, ...]:
    try:
        values = tuple(float(item.strip()) for item in raw.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{raw!r} must be a comma-separated list of GiB values"
        ) from None
    if not values or any(not 0 < value <= 1_048_576 for value in values):
        raise argparse.ArgumentTypeError(
            f"{raw!r} must contain positive finite GiB values"
        )
    return values


def workload_model_binding(raw: str) -> tuple[str, str]:
    """Parse one real-demo capability binding such as ``coding=coder.gguf``."""

    workload, separator, model = raw.partition("=")
    workload = workload.strip().lower()
    model = model.strip()
    if not separator or workload not in KNOWN_WORKLOADS or not model:
        choices = ", ".join(sorted(KNOWN_WORKLOADS))
        raise argparse.ArgumentTypeError(
            f"{raw!r} must be WORKLOAD=GGUF, where WORKLOAD is one of: {choices}"
        )
    if len(model) > 1_024:
        raise argparse.ArgumentTypeError("model filename is too long")
    return workload, model


def _workload_model_map(
    bindings: list[tuple[str, str]] | tuple[tuple[str, str], ...],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for workload, model in bindings:
        prior = result.get(workload)
        if prior is not None and prior != model:
            raise SystemExit(
                f"Workload {workload!r} is bound to both {prior!r} and {model!r}."
            )
        result[workload] = model
    return result


def _root() -> Path:
    return paths.run_dir() / "logical-test"


def _record_path() -> Path:
    return _root() / "supervisor.json"


def _load_record() -> dict[str, Any]:
    return jsonio.load_json(_record_path())


def _running_record() -> dict[str, Any]:
    record = _load_record()
    return record if record and run_records.record_alive(record) else {}


def _assert_ports_available(
    port: int,
    engine_port_base: int,
    machines: int,
    *,
    extra_ports: tuple[int, ...] = (),
) -> None:
    ports = [port]
    ports.extend(
        engine_port_base + index * 10 + offset
        for index in range(machines)
        for offset in range(4)
    )
    ports.extend(extra_ports)
    if len(ports) != len(set(ports)):
        raise SystemExit("Logical Grid control and engine port ranges overlap.")
    invalid = next((candidate for candidate in ports if not 1 <= candidate <= 65_535), None)
    if invalid is not None:
        raise SystemExit(f"Logical Grid port is outside 1–65535: {invalid}")
    for candidate in ports:
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", candidate))
            except OSError as exc:
                raise SystemExit(f"Logical Grid port {candidate} is already in use: {exc}") from exc


def _status_payload(record: dict[str, Any] | None = None) -> dict[str, Any]:
    record = record or _load_record()
    running = bool(record and run_records.record_alive(record))
    payload: dict[str, Any] = {
        "running": running,
        "machines": int(record.get("machines") or 0) if record else 0,
        "model": str(record.get("model") or "") if record else "",
        "portfolio_model": str(record.get("portfolio_model") or "") if record else "",
        "portfolio_models": list(record.get("portfolio_models") or []) if record else [],
        "workload_models": dict(record.get("workload_models") or {}) if record else {},
        "text_machines": int(record.get("text_machines") or 0) if record else 0,
        "include_comfyui": bool(record.get("include_comfyui", False)) if record else False,
        "media_bundle": str(record.get("media_bundle") or "") if record else "",
        "text_capacities_gib": list(record.get("text_capacities_gib") or []) if record else [],
        "endpoint": str(record.get("endpoint") or "") if record else "",
        "run_dir": str(record.get("run_dir") or "") if record else "",
        "log": str(record.get("log") or "") if record else "",
        "token_file": str(record.get("token_file") or "") if record else "",
    }
    if not running:
        return payload
    try:
        with httpx.Client(timeout=2.0, trust_env=False) as client:
            allocator = client.get(f"{payload['endpoint']}/allocator/status")
            allocator.raise_for_status()
            models = client.get(f"{payload['endpoint']}/v1/models")
            models.raise_for_status()
        status = allocator.json()
        payload.update(
            mode=status.get("mode"),
            authority=status.get("authority"),
            node_authorities=status.get("node_authorities") or [],
            nodes=status.get("nodes") or [],
            models=status.get("models") or [],
            forecasts=status.get("forecasts") or [],
            workload_forecasts=status.get("workload_forecasts") or [],
            portfolio_projections=status.get("portfolio_projections") or [],
            portfolio_selection=status.get("portfolio_selection") or {},
            portfolio_admissions=status.get("portfolio_admissions") or [],
            portfolio_policy=status.get("portfolio_policy") or {},
            portfolio_placement_hints=status.get("portfolio_placement_hints") or [],
            model_workload_outcomes=status.get("model_workload_outcomes") or [],
            capacity_recommendations=status.get("capacity_recommendations") or [],
            pending_commands=status.get("pending_commands") or [],
            history=status.get("history") or [],
            plan=status.get("plan") or {},
            available_models=[row.get("id") for row in models.json().get("data") or []],
        )
    except (httpx.HTTPError, TypeError, ValueError) as exc:
        payload["status_error"] = str(exc)
    return payload


def _print_status(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2))
        return
    if not payload["running"]:
        print("Logical test Grid is not running.")
        return
    nodes = payload.get("nodes") or []
    ready = sum(
        item.get("state") == "ready"
        for node in nodes
        for item in node.get("residencies") or []
    )
    print(
        f"Logical test Grid running · {len(nodes)}/{payload['machines']} hosts · "
        f"{ready} ready · allocator {payload.get('mode') or 'starting'}"
    )
    print(f"  endpoint  {payload['endpoint']}/v1")
    print(f"  model     {payload['model']}")
    portfolio_models = payload.get("portfolio_models") or (
        [payload["portfolio_model"]] if payload.get("portfolio_model") else []
    )
    if portfolio_models:
        print(f"  portfolio {', '.join(portfolio_models)}")
    workload_models = payload.get("workload_models") or {}
    if workload_models:
        print(
            "  capabilities "
            + " · ".join(
                f"{workload}→{candidate}"
                for workload, candidate in sorted(workload_models.items())
            )
        )
    frameworks = ["llama.cpp"]
    if payload.get("include_comfyui"):
        frameworks.append(f"comfyui/{payload.get('media_bundle') or 'media'}")
    print(f"  frameworks {', '.join(frameworks)} (real local processes)")
    if payload.get("token_file"):
        print(f"  control   {payload['token_file']}")
    print(f"  log       {payload['log']}")
    if payload.get("status_error"):
        print(f"  status    starting ({payload['status_error']})")
        return
    authority = payload.get("authority") or {}
    if authority.get("held"):
        accepted_terms = {
            int(item.get("highest_controller_term") or 0)
            for item in payload.get("node_authorities") or []
        }
        converged = accepted_terms == {int(authority.get("term") or 0)}
        print(
            f"  authority term {authority.get('term')} · lease active · "
            f"nodes {'converged' if converged else 'converging'}"
        )
    for node in nodes:
        residencies = node.get("residencies") or []
        placement = ", ".join(
            f"{item.get('model_id')}={item.get('state')}"
            for item in residencies
        ) or "empty"
        runtimes = ",".join(node.get("runtimes") or []) or "unknown-runtime"
        backends = ",".join(node.get("backends") or []) or "unknown-backend"
        capacity_gib = float(node.get("capacity_mb") or 0) / 1024.0
        ownership = "managed" if node.get("actuator_capabilities") else "inventory"
        print(
            f"  {node.get('node_id')}  {node.get('state')}  "
            f"{runtimes}/{backends} · {capacity_gib:.1f} GiB · {ownership}  {placement}"
        )
    portfolio_policy = payload.get("portfolio_policy") or {}
    if portfolio_policy.get("joint"):
        print(
            f"  portfolio {int(portfolio_policy.get('workloads') or 0)} workloads jointly · "
            f"models {', '.join(portfolio_policy.get('selected_models') or []) or 'none'}"
        )
        if portfolio_policy.get("objective"):
            print(f"  objective {portfolio_policy['objective']}")
    for admission in payload.get("portfolio_admissions") or []:
        print(
            f"  workload {admission.get('workload') or 'unknown'} · "
            f"{admission.get('state') or 'unknown'} · "
            f"{admission.get('model_id') or 'no-model'} · "
            f"{int(admission.get('ready_replicas') or 0)}/"
            f"{int(admission.get('desired_replicas') or 0)} ready"
        )
        if admission.get("state") != "ready" and admission.get("reason"):
            print(f"    why {admission['reason']}")
    for recommendation in (payload.get("capacity_recommendations") or [])[:3]:
        shape = recommendation.get("minimum_shape") or {}
        runtimes = ",".join(shape.get("runtimes") or []) or "any runtime"
        backends = ",".join(shape.get("backends") or []) or "any backend"
        print(
            f"  capacity  {recommendation.get('model_id')} missing "
            f"{int(recommendation.get('missing_replicas') or 0)} · "
            f">={float(shape.get('memory_mb') or 0) / 1024:.1f} GiB · "
            f"{runtimes}/{backends} · {recommendation.get('reason') or 'more capacity needed'}"
        )
    pending = payload.get("pending_commands") or []
    if pending:
        print(f"  pending   {len(pending)} allocator action(s)")
        for action in pending[:8]:
            print(
                f"    {action.get('kind')} {action.get('model_id')} on "
                f"{action.get('node_id')} — {action.get('reason')}"
            )
    history = payload.get("history") or []
    if history:
        print("  recent actions")
        for action in history[-8:]:
            duration = float(action.get("duration_seconds") or 0)
            detail = f" — {action.get('message')}" if action.get("message") else ""
            print(
                f"    {action.get('kind')} {action.get('model_id')} on "
                f"{action.get('node_id')}: {action.get('status')} ({duration:.2f}s){detail}"
            )
    print("\nFollow changes:  grid test watch")
    print(
        f"Send a request:   grid --local chat --grid {payload['endpoint']} "
        f"-m {payload['model']} 'Reply with OK'"
    )


def cmd_test_start(args: argparse.Namespace) -> int:
    workload_models = _workload_model_map(args.workload_models or [])
    portfolio_models = tuple(
        dict.fromkeys(
            model
            for model in (
                args.portfolio_model,
                *(args.candidate_models or ()),
                *workload_models.values(),
            )
            if model and model != args.model
        )
    )
    existing = _running_record()
    if existing:
        if (
            int(existing.get("machines") or 0) != args.machines
            or str(existing.get("model") or "") != args.model
            or str(existing.get("portfolio_model") or "") != args.portfolio_model
            or tuple(existing.get("portfolio_models") or ()) != portfolio_models
            or dict(existing.get("workload_models") or {}) != workload_models
            or bool(existing.get("include_comfyui", False)) != args.include_comfyui
            or str(existing.get("media_bundle") or "")
            != (args.media_bundle if args.include_comfyui else "")
            or tuple(existing.get("text_capacities_gib") or ())
            != tuple(args.text_capacities_gib or ())
        ):
            raise SystemExit(
                "A logical test Grid is already running with different settings; "
                "run `grid test stop` first."
            )
        _print_status(_status_payload(existing), as_json=args.json)
        return 0

    try:
        artifact_sha256 = LlamaCppBackend().artifact_sha256(args.model)
        for candidate in portfolio_models:
            LlamaCppBackend().artifact_sha256(candidate)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    if args.include_comfyui:
        if args.machines < 3:
            raise SystemExit(
                "A mixed real Grid needs at least three logical machines: two text slots and one "
                "ComfyUI media node."
            )
        from shared.engine import comfyui
        from shared.models import media_bundles

        if not comfyui.comfyui_dir().exists():
            raise SystemExit(
                "ComfyUI is not installed. Run `grid engine install comfyui`, then "
                f"`grid engine pull {args.media_bundle}`."
            )
        if not media_bundles.bundle_is_present(args.media_bundle):
            raise SystemExit(
                f"ComfyUI bundle {args.media_bundle!r} is not present. Run "
                f"`grid engine pull {args.media_bundle}` first."
            )
    text_machines = args.machines - int(args.include_comfyui)
    if args.text_capacities_gib and len(args.text_capacities_gib) != text_machines:
        raise SystemExit(
            f"--text-capacities-gib needs {text_machines} values, one per text node."
        )
    extra_ports = (
        (args.comfyui_port, args.media_port) if args.include_comfyui else ()
    )
    _assert_ports_available(
        args.port,
        args.engine_port_base,
        text_machines,
        extra_ports=extra_ports,
    )

    run_id = f"run-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    run_dir = _root() / run_id
    paths.ensure_dir(run_dir)
    config_path = run_dir / "config.json"
    token_path = run_dir / "control-token"
    log_path = run_dir / "supervisor.log"
    endpoint = f"http://127.0.0.1:{args.port}"
    jsonio.atomic_write_bytes(
        token_path,
        f"{secrets.token_urlsafe(32)}\n".encode(),
    )
    jsonio.atomic_write_json(
        config_path,
        {
            "machines": args.machines,
            "text_machines": text_machines,
            "model": args.model,
            "portfolio_model": args.portfolio_model,
            "portfolio_models": list(portfolio_models),
            "workload_models": workload_models,
            "include_comfyui": args.include_comfyui,
            "media_bundle": args.media_bundle if args.include_comfyui else "",
            "comfyui_port": args.comfyui_port,
            "media_port": args.media_port,
            "text_capacities_gib": list(args.text_capacities_gib or ()),
            "artifact_sha256": artifact_sha256,
            "port": args.port,
            "engine_port_base": args.engine_port_base,
            "startup_timeout": args.timeout,
            "control_token_path": os.fspath(token_path),
        },
    )
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "local.logical_test_grid",
                "--config",
                os.fspath(config_path),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    record = {
        "kind": "logical-test-grid",
        "machines": args.machines,
        "text_machines": text_machines,
        "model": args.model,
        "portfolio_model": args.portfolio_model,
        "portfolio_models": list(portfolio_models),
        "workload_models": workload_models,
        "include_comfyui": args.include_comfyui,
        "media_bundle": args.media_bundle if args.include_comfyui else "",
        "text_capacities_gib": list(args.text_capacities_gib or ()),
        "endpoint": endpoint,
        "run_dir": os.fspath(run_dir),
        "log": os.fspath(log_path),
        "token_file": os.fspath(token_path),
        "started_at": time.time(),
        **run_records.identity_stamp(proc.pid),
    }
    jsonio.atomic_write_json(_record_path(), record)

    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        ready = jsonio.load_json(run_dir / "ready.json")
        if int(ready.get("ready_replicas") or 0) == text_machines:
            payload = _status_payload(record)
            # A llama.cpp readiness probe completes just before the node's next heartbeat carries
            # the terminal warm receipt. Do not announce a settled Grid in that small window.
            if not payload.get("status_error") and not payload.get("pending_commands"):
                payload["engine_ports"] = ready.get("engine_ports") or []
                _print_status(payload, as_json=args.json)
                return 0
        error = jsonio.load_json(run_dir / "error.json")
        if error:
            raise SystemExit(
                f"Logical test Grid failed to start: {error.get('error')}. See {log_path}"
            )
        if not run_records.record_alive(record):
            raise SystemExit(f"Logical test Grid exited during startup. See {log_path}")
        time.sleep(0.1)
    raise SystemExit(
        f"Logical test Grid is still starting after {args.timeout:g}s. "
        f"Run `grid test status`; log: {log_path}"
    )


def cmd_test_status(args: argparse.Namespace) -> int:
    _print_status(_status_payload(), as_json=args.json)
    return 0


def _residency_states(payload: dict[str, Any]) -> dict[tuple[str, str], str]:
    return {
        (str(node.get("node_id")), str(item.get("model_id"))): str(item.get("state"))
        for node in payload.get("nodes") or []
        for item in node.get("residencies") or []
    }


def _history_key(action: dict[str, Any]) -> tuple[str, str, float]:
    return (
        str(action.get("action_id") or ""),
        str(action.get("status") or ""),
        float(action.get("completed_at") or 0),
    )


def cmd_test_watch(args: argparse.Namespace) -> int:
    payload = _status_payload()
    if not payload["running"]:
        print("Logical test Grid is not running.")
        return 1
    if payload.get("status_error"):
        print(f"Logical test Grid is starting: {payload['status_error']}")
    else:
        print(
            f"Watching {payload['machines']} logical hosts at {payload['endpoint']} "
            "(Ctrl-C to stop watching; the Grid keeps running)."
        )
    prior_states = _residency_states(payload)
    seen_history = {_history_key(row) for row in payload.get("history") or []}
    for (node_id, model_id), state in sorted(prior_states.items()):
        print(f"{time.strftime('%H:%M:%S')}  {node_id}  {model_id}: {state}")
    try:
        while True:
            time.sleep(args.interval)
            current = _status_payload()
            if not current["running"]:
                print(f"{time.strftime('%H:%M:%S')}  logical test Grid stopped")
                return 0
            if current.get("status_error"):
                continue
            states = _residency_states(current)
            for key in sorted(set(prior_states) | set(states)):
                before = prior_states.get(key, "absent")
                after = states.get(key, "absent")
                if before != after:
                    print(
                        f"{time.strftime('%H:%M:%S')}  {key[0]}  {key[1]}: "
                        f"{before} -> {after}",
                        flush=True,
                    )
            prior_states = states
            for action in current.get("history") or []:
                key = _history_key(action)
                if key in seen_history:
                    continue
                seen_history.add(key)
                detail = f" — {action.get('message')}" if action.get("message") else ""
                print(
                    f"{time.strftime('%H:%M:%S')}  allocator {action.get('kind')} "
                    f"{action.get('model_id')} on {action.get('node_id')}: "
                    f"{action.get('status')}{detail}",
                    flush=True,
                )
    except KeyboardInterrupt:
        print("\nStopped watching; the logical test Grid is still running.")
        return 0


def _ready_replicas(payload: dict[str, Any], model: str) -> int:
    return sum(
        item.get("model_id") == model and item.get("state") == "ready"
        for node in payload.get("nodes") or []
        for item in node.get("residencies") or []
    )


def _live_replicas(payload: dict[str, Any], model: str) -> int:
    return sum(
        item.get("model_id") == model
        and item.get("state") in {"loading", "warming", "ready", "draining"}
        for node in payload.get("nodes") or []
        for item in node.get("residencies") or []
    )


def _print_new_events(
    prior_states: dict[tuple[str, str], str],
    seen_history: set[tuple[str, str, float]],
    current: dict[str, Any],
) -> dict[tuple[str, str], str]:
    states = _residency_states(current)
    for key in sorted(set(prior_states) | set(states)):
        before = prior_states.get(key, "absent")
        after = states.get(key, "absent")
        if before != after:
            print(f"    {key[0]} · {key[1]}: {before} -> {after}", flush=True)
    for action in current.get("history") or []:
        key = _history_key(action)
        if key in seen_history:
            continue
        seen_history.add(key)
        detail = f" — {action.get('message')}" if action.get("message") else ""
        print(
            f"    allocator {action.get('kind')} on {action.get('node_id')}: "
            f"{action.get('status')}{detail}",
            flush=True,
        )
    return states


def _wait_for_replicas(
    record: dict[str, Any],
    *,
    model: str,
    replicas: int,
    timeout: float,
    initial: dict[str, Any],
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    prior_states = _residency_states(initial)
    seen_history = {_history_key(row) for row in initial.get("history") or []}
    while time.monotonic() < deadline:
        current = _status_payload(record)
        if not current["running"]:
            raise SystemExit("Logical test Grid stopped during the demand demonstration.")
        if current.get("status_error"):
            time.sleep(0.1)
            continue
        prior_states = _print_new_events(prior_states, seen_history, current)
        if _ready_replicas(current, model) == replicas and not current.get("pending_commands"):
            return current
        time.sleep(0.1)
    raise SystemExit(f"Timed out waiting for {replicas} ready replicas.")


def _wait_for_placement(
    record: dict[str, Any],
    *,
    replicas: dict[str, int],
    timeout: float,
    initial: dict[str, Any],
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    prior_states = _residency_states(initial)
    seen_history = {_history_key(row) for row in initial.get("history") or []}
    while time.monotonic() < deadline:
        current = _status_payload(record)
        if not current["running"]:
            raise SystemExit("Logical test Grid stopped during the allocator demonstration.")
        if current.get("status_error"):
            time.sleep(0.1)
            continue
        prior_states = _print_new_events(prior_states, seen_history, current)
        if all(
            _ready_replicas(current, model) == count
            and _live_replicas(current, model) == count
            for model, count in replicas.items()
        ):
            if not current.get("pending_commands"):
                return current
        time.sleep(0.1)
    expected = ", ".join(f"{model}={count}" for model, count in replicas.items())
    raise SystemExit(f"Timed out waiting for placement: {expected}.")


def _desired_replicas(payload: dict[str, Any], model: str) -> int:
    desired = (payload.get("plan") or {}).get("desired_replicas") or {}
    return int(desired.get(model) or 0)


@dataclass(frozen=True, slots=True)
class _RealUser:
    user_id: str
    role: str
    model: str
    prompt: str


@dataclass(frozen=True, slots=True)
class _RealChatResult:
    user_id: str
    role: str
    model: str
    status_code: int
    elapsed_seconds: float
    response_id: str
    completion_tokens: int
    text: str
    error: str = ""
    attempts: int = 1


@dataclass(frozen=True, slots=True)
class _CodingBenchmarkTask:
    name: str
    prompt: str
    expected: str


_CODING_BENCHMARK = (
    _CodingBenchmarkTask(
        "python-list-comprehension",
        """What does this Python print?
x = [1, 2, 3, 4]
print([n * 2 for n in x if n % 2])
A. [2, 4]  B. [2, 6]  C. [4, 8]  D. [1, 3]
Reply with only A, B, C, or D.""",
        "B",
    ),
    _CodingBenchmarkTask(
        "python-mutable-default",
        """Which signature safely fixes a Python function that currently mutates items=[]?
A. def add(x, items=[])
B. def add(x, items={})
C. def add(x, items=None), then create [] when items is None
D. def add(x, items=list())
Reply with only A, B, C, or D.""",
        "C",
    ),
    _CodingBenchmarkTask(
        "binary-search-complexity",
        "Binary search on a sorted array has which worst-case time complexity? "
        "A. O(1)  B. O(log n)  C. O(n)  D. O(n log n). Reply with only A, B, C, or D.",
        "B",
    ),
    _CodingBenchmarkTask(
        "sql-parameterization",
        """Which Python database call best avoids SQL injection for an integer id?
A. execute(f'SELECT * FROM t WHERE id={id}')
B. execute('SELECT * FROM t WHERE id=' + str(id))
C. execute('SELECT * FROM t WHERE id=%s' % id)
D. execute('SELECT * FROM t WHERE id=?', (id,))
Reply with only A, B, C, or D.""",
        "D",
    ),
    _CodingBenchmarkTask(
        "python-finally-return",
        """What does this Python print?
def f():
    try: return 1
    finally: return 2
print(f())
A. 1  B. 2  C. None  D. It raises. Reply with only A, B, C, or D.""",
        "B",
    ),
    _CodingBenchmarkTask(
        "javascript-map-filter",
        "What is [1,2,3].map(x => x * 2).filter(x => x > 2)? "
        "A. [2,4]  B. [2,4,6]  C. [4,6]  D. [6]. Reply with only A, B, C, or D.",
        "C",
    ),
    _CodingBenchmarkTask(
        "async-gather",
        "Three independent one-second async operations run with gather. Ignoring overhead, "
        "how long do they take? A. about 1 second  B. about 2 seconds  C. about 3 seconds "
        "D. they cannot run concurrently. Reply with only A, B, C, or D.",
        "A",
    ),
    _CodingBenchmarkTask(
        "git-soft-reset",
        "Which command removes the latest local Git commit while keeping its changes staged? "
        "A. git reset --hard HEAD~1  B. git revert HEAD  C. git reset --soft HEAD~1 "
        "D. git clean -fd. Reply with only A, B, C, or D.",
        "C",
    ),
)


_REAL_USER_BLUEPRINTS = (
    (
        "software-engineer",
        "specialist",
        "Debug this Python function, explain the bug, and propose one unit test.",
    ),
    (
        "researcher",
        "specialist",
        "Compare two approaches to retrieval augmented generation in three concise points.",
    ),
    (
        "marketer",
        "baseline",
        "Write one concise launch headline for a privacy-first local AI product.",
    ),
    (
        "sales",
        "baseline",
        "Draft a two-sentence follow-up to a customer evaluating private AI infrastructure.",
    ),
    (
        "designer",
        "baseline",
        "Describe a simple dashboard layout for monitoring a four-node compute grid.",
    ),
    (
        "operations",
        "baseline",
        "Summarize the safest response to a model server becoming unavailable.",
    ),
)


def _real_users(count: int, *, baseline: str, specialist: str) -> tuple[_RealUser, ...]:
    users: list[_RealUser] = []
    for index in range(count):
        role, target, prompt = _REAL_USER_BLUEPRINTS[index % len(_REAL_USER_BLUEPRINTS)]
        users.append(
            _RealUser(
                user_id=f"user-{index + 1:03d}",
                role=role,
                model=specialist if target == "specialist" else baseline,
                prompt=prompt,
            )
        )
    return tuple(users)


def _fixture_user_ids(
    count: int,
    *,
    namespace: str = "unresolved-coding",
) -> tuple[str, ...]:
    """Create stable distinct personas for real multi-user demand tests."""

    return tuple(f"user-{namespace}-{index + 1}" for index in range(count))


_WORKLOAD_PROMPTS = {
    "coding": "Debug this Python API function and propose one regression unit test.",
    "research": "Research and compare two retrieval approaches using evidence from papers.",
    "marketing": "Write a marketing campaign headline for a privacy-first local AI product.",
    "sales": "Draft a sales follow-up for a customer evaluating private AI infrastructure.",
    "design": "Design a simple dashboard layout and explain its typography and hierarchy.",
    "general": "Explain one practical benefit of running AI models locally.",
    "embedding": "Explain how embedding vectors support semantic retrieval.",
    "image": "Create an image concept for a privacy-first local AI product.",
    "video": "Create a video concept for a privacy-first local AI product.",
}


def _real_chat_request(
    endpoint: str,
    user: _RealUser,
    *,
    max_tokens: int,
    timeout: float,
    retries: int = 0,
    request_headers: dict[str, str] | None = None,
) -> _RealChatResult:
    request_started = time.monotonic()
    result: _RealChatResult | None = None
    for attempt in range(retries + 1):
        result = _real_chat_request_once(
            endpoint,
            user,
            max_tokens=max_tokens,
            timeout=timeout,
            request_headers=request_headers,
        )
        result = replace(
            result,
            attempts=attempt + 1,
            # Report client-observed latency across every transient 429/503, including bounded
            # backoff—not merely the last successful engine attempt. Otherwise a saturated Grid
            # appears artificially faster precisely when users are waiting the longest.
            elapsed_seconds=time.monotonic() - request_started,
        )
        if result.status_code == 200 or result.status_code not in {429, 503}:
            return result
        if attempt < retries:
            time.sleep(min(1.0, 0.1 * (attempt + 1)))
    assert result is not None
    return result


def _real_chat_request_once(
    endpoint: str,
    user: _RealUser,
    *,
    max_tokens: int,
    timeout: float,
    request_headers: dict[str, str] | None = None,
) -> _RealChatResult:
    started = time.monotonic()
    try:
        with httpx.Client(base_url=endpoint, timeout=timeout, trust_env=False) as client:
            response = client.post(
                "/v1/chat/completions",
                headers={
                    "X-Grid-Affinity-Key": user.user_id,
                    **(request_headers or {}),
                },
                json={
                    "model": user.model,
                    "messages": [{"role": "user", "content": user.prompt}],
                    "max_tokens": max_tokens,
                    "temperature": 0,
                },
            )
        payload = response.json()
        choices = payload.get("choices") or [] if isinstance(payload, dict) else []
        message = choices[0].get("message") if choices and isinstance(choices[0], dict) else {}
        text = str((message or {}).get("content") or "")
        usage = payload.get("usage") or {} if isinstance(payload, dict) else {}
        error = ""
        if response.status_code != 200:
            detail = payload.get("error") if isinstance(payload, dict) else None
            error = json.dumps(detail, sort_keys=True) if detail else response.text[:300]
        elif not text:
            error = "successful response contained no assistant text"
        return _RealChatResult(
            user_id=user.user_id,
            role=user.role,
            model=user.model,
            status_code=response.status_code,
            elapsed_seconds=time.monotonic() - started,
            response_id=str(payload.get("id") or "") if isinstance(payload, dict) else "",
            completion_tokens=int(usage.get("completion_tokens") or 0),
            text=text,
            error=error,
        )
    except (httpx.HTTPError, TypeError, ValueError) as exc:
        return _RealChatResult(
            user_id=user.user_id,
            role=user.role,
            model=user.model,
            status_code=0,
            elapsed_seconds=time.monotonic() - started,
            response_id="",
            completion_tokens=0,
            text="",
            error=str(exc),
        )


def _run_real_chat_batch(
    endpoint: str,
    users: tuple[_RealUser, ...],
    *,
    requests: int,
    max_tokens: int,
    timeout: float,
) -> tuple[_RealChatResult, ...]:
    scheduled = tuple(users[index % len(users)] for index in range(requests))
    results: list[_RealChatResult] = []
    with ThreadPoolExecutor(max_workers=min(len(users), requests, 32)) as pool:
        futures = [
            pool.submit(
                _real_chat_request,
                endpoint,
                user,
                max_tokens=max_tokens,
                timeout=timeout,
                retries=12,
            )
            for user in scheduled
        ]
        for future in as_completed(futures):
            results.append(future.result())
    return tuple(results)


def _require_real_chat(results: tuple[_RealChatResult, ...], *, label: str) -> None:
    failures = [item for item in results if item.status_code != 200 or item.error]
    if failures:
        detail = "; ".join(
            f"{item.user_id}/{item.role}: HTTP {item.status_code} {item.error}"
            for item in failures[:5]
        )
        raise SystemExit(f"{label} real inference failed: {detail}")


def _performance_samples(payload: dict[str, Any]) -> dict[tuple[str, str], int]:
    return {
        (str(node.get("node_id")), str(row.get("model_id"))): int(
            row.get("sample_count") or 0
        )
        for node in payload.get("nodes") or []
        for row in node.get("model_performance") or []
    }


def _wait_for_demand_ready(
    record: dict[str, Any],
    *,
    model: str,
    timeout: float,
    initial: dict[str, Any],
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    prior_states = _residency_states(initial)
    seen_history = {_history_key(row) for row in initial.get("history") or []}
    while time.monotonic() < deadline:
        current = _status_payload(record)
        if not current["running"]:
            raise SystemExit("Logical test Grid stopped during real inference.")
        if current.get("status_error"):
            time.sleep(0.1)
            continue
        prior_states = _print_new_events(prior_states, seen_history, current)
        if _ready_replicas(current, model) >= 1 and not current.get("pending_commands"):
            return current
        time.sleep(0.1)
    raise SystemExit(f"Timed out waiting for real demand to make {model!r} routable.")


def _media_model_for_bundle(bundle: str) -> str:
    return {
        "image_generation": "comfyui:image_generation",
        "z_image": "comfyui:z_image",
    }.get(bundle, "")


def _run_real_image(
    record: dict[str, Any],
    *,
    timeout: float,
) -> dict[str, Any]:
    bundle = str(record.get("media_bundle") or "")
    model = _media_model_for_bundle(bundle)
    if not model:
        raise SystemExit(
            f"The real demo does not yet have an image-generation request for bundle {bundle!r}."
        )
    endpoint = str(record["endpoint"])
    started = time.monotonic()
    result: dict[str, Any] | None = None
    with httpx.Client(
        base_url=endpoint,
        timeout=httpx.Timeout(timeout, read=timeout),
        trust_env=False,
    ) as client:
        with client.stream(
            "POST",
            "/v1/media/image/generate",
            headers={"X-Grid-Affinity-Key": "user-image-001"},
            json={
                "model": model,
                "prompt": (
                    "A clean editorial illustration of four small computers sharing AI work, "
                    "dark navy background, cyan accents, no text"
                ),
                "width": 512,
                "height": 512,
            },
        ) as response:
            if response.status_code != 200:
                raise SystemExit(
                    f"Real ComfyUI request failed with HTTP {response.status_code}: "
                    f"{response.read().decode(errors='replace')[:500]}"
                )
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line.removeprefix("data:").strip()
                if not raw or raw == "[DONE]":
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "progress":
                    print(f"    ComfyUI progress {float(event.get('progress') or 0):.1f}%", flush=True)
                if event.get("error"):
                    raise SystemExit(f"Real ComfyUI workflow failed: {event['error']}")
                if event.get("type") == "result":
                    result = event
    files = (result or {}).get("output_files") or []
    if not files:
        raise SystemExit("Real ComfyUI workflow returned no output image.")
    first = files[0]
    try:
        image_bytes = base64.b64decode(str(first["content_base64"]), validate=True)
    except (KeyError, ValueError) as exc:
        raise SystemExit("Real ComfyUI workflow returned invalid image bytes.") from exc
    if not image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        raise SystemExit("Real ComfyUI workflow output is not a PNG image.")
    output = Path(str(record["run_dir"])) / "real-comfyui-output.png"
    output.write_bytes(image_bytes)
    return {
        "model": model,
        "path": str(output),
        "bytes": len(image_bytes),
        "elapsed_seconds": time.monotonic() - started,
    }


def _benchmark_choice(text: str) -> str:
    match = re.search(r"(?<![A-Z])([ABCD])(?![A-Z])", text.upper()[:160])
    return match.group(1) if match else ""


def _profile_body(row: dict[str, Any], **updates: Any) -> dict[str, Any]:
    return {**{key: value for key, value in row.items() if key != "retiring"}, **updates}


def _put_test_profile(
    client: httpx.Client,
    headers: dict[str, str],
    row: dict[str, Any],
    **updates: Any,
) -> None:
    model_id = str(row["model_id"])
    response = client.put(
        f"/allocator/models/{quote(model_id, safe='')}",
        headers=headers,
        json=_profile_body(row, **updates),
    )
    response.raise_for_status()


def _wait_for_competition_choice(
    record: dict[str, Any],
    *,
    candidates: tuple[str, ...],
    timeout: float,
    initial: dict[str, Any],
    expected: str = "",
) -> tuple[str, dict[str, Any]]:
    deadline = time.monotonic() + timeout
    prior = _residency_states(initial)
    seen_history = {_history_key(row) for row in initial.get("history") or []}
    while time.monotonic() < deadline:
        status = _status_payload(record)
        if status.get("status_error"):
            time.sleep(0.1)
            continue
        prior = _print_new_events(prior, seen_history, status)
        projection = next(
            (
                row
                for row in status.get("portfolio_projections") or []
                if row.get("workload") == "coding"
                and row.get("chosen_model") in candidates
            ),
            None,
        )
        chosen = str((projection or {}).get("chosen_model") or "")
        if (
            chosen
            and (not expected or chosen == expected)
            and _ready_replicas(status, chosen) >= 1
            and not status.get("pending_commands")
        ):
            return chosen, status
        time.sleep(0.1)
    raise SystemExit("Timed out waiting for measured coding evidence to select and warm a model.")


def cmd_test_compete(args: argparse.Namespace) -> int:
    """Benchmark distinct real models, record quality, then prove autonomous selection."""

    record = _running_record()
    if not record:
        raise SystemExit("Logical test Grid is not running. Start it with `grid test start`.")
    status = _status_payload(record)
    if status.get("status_error"):
        raise SystemExit(f"Cannot read logical Grid status: {status['status_error']}")
    candidates = tuple(
        dict.fromkeys(
            str(item)
            for item in (
                record.get("portfolio_models")
                or ([record.get("portfolio_model")] if record.get("portfolio_model") else [])
            )
            if str(item)
        )
    )
    if len(candidates) < 2:
        raise SystemExit(
            "Real competition needs at least two portfolio candidates. Restart with repeated "
            "`--candidate-model <cached-gguf>` arguments."
        )
    by_model = {
        str(row.get("model_id")): row for row in status.get("models") or []
    }
    missing = [model for model in candidates if model not in by_model]
    if missing:
        raise SystemExit(f"Allocator profiles are missing competition models: {missing}")
    baseline = str(record["model"])
    if baseline not in by_model:
        raise SystemExit(f"Allocator profile is missing baseline model {baseline!r}.")
    token = Path(str(record["token_file"])).read_text(encoding="utf-8").strip()
    headers = {"X-Grid-Allocator-Token": token}
    endpoint = str(record["endpoint"])
    benchmark_rows: list[dict[str, Any]] = []

    print("Real model competition · router-independent allocator evidence")
    print(f"Candidates: {', '.join(candidates)}")
    print(f"Benchmark:  {len(_CODING_BENCHMARK)} deterministic coding tasks per model")
    print("Every answer is generated by a real llama.cpp/Metal process; correctness is exact and local.\n")

    with httpx.Client(base_url=endpoint, timeout=10.0, trust_env=False) as client:
        _put_test_profile(
            client,
            headers,
            by_model[baseline],
            min_replicas=1,
            max_replicas=1,
            min_failure_domains=1,
            min_residency_seconds=0,
            scale_down_cooldown_seconds=1,
        )
        for row in by_model.values():
            if row.get("model_id") in candidates:
                _put_test_profile(
                    client,
                    headers,
                    row,
                    min_replicas=0,
                    max_replicas=1,
                    min_failure_domains=1,
                    min_residency_seconds=0,
                    scale_down_cooldown_seconds=1,
                    workload_scores=[["coding", 1.0]],
                )
        time.sleep(1.1)

        for candidate_index, candidate in enumerate(candidates, 1):
            print(f"[{candidate_index}/{len(candidates)}] Evaluating {candidate}")
            _put_test_profile(
                client,
                headers,
                by_model[candidate],
                min_replicas=1,
                max_replicas=1,
                min_failure_domains=1,
                min_residency_seconds=0,
                scale_down_cooldown_seconds=1,
                workload_scores=[["coding", 1.0]],
            )
            ready = _wait_for_demand_ready(
                record,
                model=candidate,
                timeout=args.timeout,
                initial=_status_payload(record),
            )
            node = next(
                node
                for node in ready.get("nodes") or []
                if any(
                    item.get("model_id") == candidate and item.get("state") == "ready"
                    for item in node.get("residencies") or []
                )
            )
            correct = 0
            latencies: list[float] = []
            for task_index, task in enumerate(_CODING_BENCHMARK, 1):
                result = _real_chat_request(
                    endpoint,
                    _RealUser(
                        user_id=f"benchmark-{candidate_index}-{task_index}",
                        role="software-engineer",
                        model=candidate,
                        prompt=task.prompt,
                    ),
                    max_tokens=args.max_tokens,
                    timeout=args.timeout,
                    retries=3,
                    request_headers={
                        "X-Grid-Allocator-Evaluation": "1",
                        **headers,
                    },
                )
                answer = _benchmark_choice(result.text)
                passed = result.status_code == 200 and answer == task.expected
                correct += int(passed)
                latencies.append(result.elapsed_seconds)
                evidence = client.post(
                    "/allocator/evaluations",
                    headers=headers,
                    json={
                        "model_id": candidate,
                        "workload": "coding",
                        "artifact_sha256": str(
                            by_model[candidate].get("artifact_sha256") or ""
                        ),
                        "quality": float(passed),
                        "error": result.status_code != 200,
                        "latency_ms": result.elapsed_seconds * 1_000.0,
                        "output_units": result.completion_tokens,
                    },
                )
                evidence.raise_for_status()
                print(
                    f"    {task.name:<28} expected {task.expected} · "
                    f"got {answer or '?'} · {'pass' if passed else 'fail'}"
                )
            quality = correct / len(_CODING_BENCHMARK)
            benchmark_rows.append(
                {
                    "model": candidate,
                    "quality": quality,
                    "median_latency": statistics.median(latencies),
                    "memory_mb": int(by_model[candidate].get("memory_mb") or 0),
                    "node": str(node.get("node_id") or ""),
                    "node_capacity_mb": int(node.get("capacity_mb") or 0),
                }
            )
            print(
                f"  score {correct}/{len(_CODING_BENCHMARK)} ({quality:.0%}) · median "
                f"{statistics.median(latencies):.3f}s · {node.get('node_id')} "
                f"({float(node.get('capacity_mb') or 0) / 1024:.1f} GiB)\n"
            )
            _put_test_profile(
                client,
                headers,
                by_model[candidate],
                min_replicas=0,
                max_replicas=1,
                min_failure_domains=1,
                min_residency_seconds=0,
                scale_down_cooldown_seconds=1,
                workload_scores=[["coding", 1.0]],
            )
            # Fully release the one model-at-a-time benchmark slot before admitting the next
            # candidate. Otherwise an intentionally tiny test node can still be draining while
            # the next candidate arrives, briefly forcing a cold start on a larger expensive node
            # and obscuring the placement decision this command is meant to explain.
            _wait_for_placement(
                record,
                replicas={candidate: 0},
                timeout=args.timeout,
                initial=ready,
            )

        settled = _wait_for_placement(
            record,
            replicas={baseline: 1, **{candidate: 0 for candidate in candidates}},
            timeout=args.timeout,
            initial=_status_payload(record),
        )
        for candidate in candidates:
            _put_test_profile(
                client,
                headers,
                by_model[candidate],
                min_replicas=0,
                max_replicas=1,
                min_failure_domains=1,
                min_residency_seconds=0,
                scale_down_cooldown_seconds=300,
                workload_scores=[["coding", 1.0]],
            )

    print("[selection] Sending unresolved coding demand after all candidates are offloaded")
    unresolved = tuple(
        _real_chat_request(
            endpoint,
            _RealUser(
                user_id=f"competition-unresolved-{index}",
                role="software-engineer",
                model="auto",
                prompt="Debug this Python function and add a regression unit test.",
            ),
            max_tokens=args.max_tokens,
            timeout=args.timeout,
        )
        for index in range(1, 4)
    )
    if any(item.status_code != 503 for item in unresolved):
        raise SystemExit("Expected unresolved router-free requests to return HTTP 503.")
    chosen, selected = _wait_for_competition_choice(
        record,
        candidates=candidates,
        timeout=args.timeout,
        initial=settled,
    )
    projection = next(
        row
        for row in selected.get("portfolio_projections") or []
        if row.get("workload") == "coding" and row.get("chosen_model") == chosen
    )
    chosen_node = next(
        node
        for node in selected.get("nodes") or []
        if any(
            item.get("model_id") == chosen and item.get("state") == "ready"
            for item in node.get("residencies") or []
        )
    )
    placement_hint = next(
        (
            row
            for row in selected.get("portfolio_placement_hints") or []
            if row.get("model_id") == chosen
        ),
        {},
    )
    expected_node = str(placement_hint.get("best_node_id") or "")
    if expected_node and chosen_node.get("node_id") != expected_node:
        raise SystemExit(
            f"Allocator placed {chosen!r} on {chosen_node.get('node_id')}, but its live "
            f"placement plan preferred {expected_node}."
        )
    proof = _real_chat_request(
        endpoint,
        _RealUser(
            user_id="competition-proof",
            role="software-engineer",
            model=chosen,
            prompt="What is the time complexity of binary search? Reply concisely.",
        ),
        max_tokens=args.max_tokens,
        timeout=args.timeout,
    )
    _require_real_chat((proof,), label="winning model")

    print(f"\n[constraint change] Making {chosen} ineligible on every logical node")
    forced_tag = "logical-test-forced-infeasible"
    original_tags = list(by_model[chosen].get("required_tags") or [])
    with httpx.Client(base_url=endpoint, timeout=10.0, trust_env=False) as client:
        _put_test_profile(
            client,
            headers,
            by_model[chosen],
            min_replicas=0,
            max_replicas=1,
            min_failure_domains=1,
            min_residency_seconds=0,
            scale_down_cooldown_seconds=300,
            workload_scores=[["coding", 1.0]],
            required_tags=[*original_tags, forced_tag],
        )
    fallback, fallback_status = _wait_for_competition_choice(
        record,
        candidates=candidates,
        timeout=args.timeout,
        initial=selected,
    )
    if fallback == chosen:
        raise SystemExit("Allocator retained a portfolio model after it became fleet-infeasible.")
    fallback_projection = next(
        row
        for row in fallback_status.get("portfolio_projections") or []
        if row.get("workload") == "coding" and row.get("chosen_model") == fallback
    )
    rejected = next(
        row
        for row in fallback_projection.get("candidates") or []
        if row.get("model_id") == chosen
    )
    rejected_reason = str((rejected.get("placement") or {}).get("reason") or "")
    fallback_proof = _real_chat_request(
        endpoint,
        _RealUser(
            user_id="competition-fallback-proof",
            role="software-engineer",
            model=fallback,
            prompt="What is the time complexity of binary search? Reply concisely.",
        ),
        max_tokens=args.max_tokens,
        timeout=args.timeout,
    )
    _require_real_chat((fallback_proof,), label="fleet-feasible fallback")
    print(f"  rejected {chosen}: {rejected_reason}")
    print(f"  loaded {fallback} instead and served a real response: {fallback_proof.text[:100]!r}")

    print(f"\n[recovery] Restoring eligibility for {chosen}")
    with httpx.Client(base_url=endpoint, timeout=10.0, trust_env=False) as client:
        _put_test_profile(
            client,
            headers,
            by_model[chosen],
            min_replicas=0,
            max_replicas=1,
            min_failure_domains=1,
            min_residency_seconds=0,
            scale_down_cooldown_seconds=300,
            workload_scores=[["coding", 1.0]],
            required_tags=original_tags,
        )
    recovered, selected = _wait_for_competition_choice(
        record,
        candidates=candidates,
        timeout=args.timeout,
        initial=fallback_status,
        expected=chosen,
    )
    if recovered != chosen:
        raise SystemExit(f"Allocator did not restore the measured winner {chosen!r}.")
    with httpx.Client(base_url=endpoint, timeout=10.0, trust_env=False) as client:
        _put_test_profile(
            client,
            headers,
            by_model[chosen],
            min_replicas=0,
            max_replicas=1,
            min_failure_domains=1,
            min_residency_seconds=0,
            scale_down_cooldown_seconds=300,
            workload_scores=[["coding", 1.0]],
            required_tags=original_tags,
        )
    print(f"  restored {chosen}; the measured winner is ready again")

    print("\nResult")
    for row in sorted(benchmark_rows, key=lambda item: (-item["quality"], item["median_latency"])):
        print(
            f"  {row['model']}: quality {row['quality']:.0%} · median "
            f"{row['median_latency']:.3f}s · profile {row['memory_mb']} MiB"
        )
    print(f"  allocator chose {chosen} · portfolio score {float(projection.get('score') or 0):.4f}")
    print(
        f"  placed on {chosen_node.get('node_id')} · "
        f"{float(chosen_node.get('capacity_mb') or 0) / 1024:.1f} GiB · "
        "planner-preferred capable node"
    )
    print(f"  real winning response: {proof.text[:160]!r}")
    print(
        f"  forced constraint fallback: {chosen} → {fallback} → {chosen} after recovery"
    )
    print(
        "The router supplied no provisioning requirement; measured evidence and live fleet "
        "constraints drove selection."
    )
    return 0


def _workload_scores_for_model(
    workload_models: dict[str, str], model_id: str
) -> list[list[Any]]:
    return [
        [workload, 1.0]
        for workload, candidate in sorted(workload_models.items())
        if candidate == model_id
    ]


def _send_real_unresolved_workload(
    endpoint: str,
    workload: str,
    *,
    token: str,
    max_tokens: int,
    timeout: float,
) -> tuple[_RealChatResult, ...]:
    prompt = _WORKLOAD_PROMPTS[workload]
    user_ids = _fixture_user_ids(3, namespace=f"unresolved-{workload}")
    results = tuple(
        _real_chat_request(
            endpoint,
            _RealUser(
                user_id=user_ids[index % len(user_ids)],
                role=workload,
                model="auto",
                prompt=prompt,
            ),
            max_tokens=max_tokens,
            timeout=timeout,
            tenant_attestation_secret=token,
        )
        for index in range(12)
    )
    unexpected = [item for item in results if item.status_code != 503]
    if unexpected:
        raise SystemExit(
            f"Expected router-free {workload} discovery requests to return HTTP 503; "
            f"got {unexpected[0].status_code}."
        )
    return results


def _selected_models_for_workloads(
    status: dict[str, Any], workloads: tuple[str, ...]
) -> dict[str, str]:
    projections = {
        str(row.get("workload")): str(row.get("chosen_model") or "")
        for row in status.get("portfolio_projections") or []
    }
    selected = {workload: projections.get(workload, "") for workload in workloads}
    missing = [workload for workload, model in selected.items() if not model]
    if missing:
        raise SystemExit(
            "Allocator did not select a fleet-feasible model for: " + ", ".join(missing)
        )
    return selected


def _cmd_test_adaptive_workday(
    args: argparse.Namespace,
    record: dict[str, Any],
    status: dict[str, Any],
) -> int:
    """Exercise real multi-workload portfolio changes on the persistent logical Grid."""

    baseline = str(record["model"])
    workload_models = {
        str(workload): str(model)
        for workload, model in dict(record.get("workload_models") or {}).items()
    }
    workloads = tuple(sorted(workload_models))
    candidate_models = tuple(sorted(set(workload_models.values())))
    text_machines = int(record.get("text_machines") or record["machines"])
    if len(candidate_models) > text_machines - 1:
        raise SystemExit(
            f"This workload map needs {len(candidate_models) + 1} simultaneous text slots "
            f"(including the baseline), but the Grid has {text_machines}."
        )
    by_model = {
        str(row.get("model_id")): row for row in status.get("models") or []
    }
    missing_profiles = [
        model for model in (baseline, *candidate_models) if model not in by_model
    ]
    if missing_profiles:
        raise SystemExit(f"Allocator profiles are missing real models: {missing_profiles}")

    endpoint = str(record["endpoint"])
    token = Path(str(record["token_file"])).read_text(encoding="utf-8").strip()
    headers = {"X-Grid-Allocator-Token": token}
    print(
        f"Real adaptive workday · {text_machines} llama.cpp logical machines on this Mac"
    )
    print(f"Baseline: {baseline}")
    for workload, model in sorted(workload_models.items()):
        print(f"Capability: {workload:<10} → {model}")
    if record.get("include_comfyui"):
        print(
            f"Media: {_media_model_for_bundle(str(record.get('media_bundle') or ''))} "
            "through real ComfyUI/PyTorch-MPS"
        )
    print(
        "All placement changes below come from observed HTTP requests and real engine "
        "load/warm/drain/unload actions.\n"
    )

    with httpx.Client(base_url=endpoint, timeout=10.0, trust_env=False) as client:
        print("[1/5] Idle fleet: keep one safe baseline and free every optional slot")
        response = client.put(
            "/allocator/mode", headers=headers, json={"mode": "automatic"}
        )
        response.raise_for_status()
        _put_test_profile(
            client,
            headers,
            by_model[baseline],
            min_replicas=1,
            max_replicas=text_machines,
            min_failure_domains=1,
            min_residency_seconds=0,
            scale_down_cooldown_seconds=1,
        )
        for candidate in candidate_models:
            _put_test_profile(
                client,
                headers,
                by_model[candidate],
                min_replicas=0,
                max_replicas=1,
                min_failure_domains=1,
                min_residency_seconds=0,
                scale_down_cooldown_seconds=1,
                workload_scores=_workload_scores_for_model(
                    workload_models, candidate
                ),
            )
        time.sleep(1.1)
        idle = _wait_for_placement(
            record,
            replicas={baseline: 1, **{model: 0 for model in candidate_models}},
            timeout=args.timeout,
            initial=status,
        )
        print(f"  Placement: {baseline}=1; {text_machines - 1} text slot(s) free.\n")

        # Freeze actuation before lengthening the optional models' active horizon. Forecast history
        # is deliberately durable, so doing this in automatic mode could resurrect an earlier demo
        # before the new real requests below establish the current workload mix.
        response = client.put(
            "/allocator/mode", headers=headers, json={"mode": "recommend"}
        )
        response.raise_for_status()
        for candidate in candidate_models:
            _put_test_profile(
                client,
                headers,
                by_model[candidate],
                min_replicas=0,
                max_replicas=1,
                min_failure_domains=1,
                min_residency_seconds=0,
                scale_down_cooldown_seconds=300,
                workload_scores=_workload_scores_for_model(
                    workload_models, candidate
                ),
            )

        print("[2/5] Morning mix: multiple users ask for different kinds of work")
        for workload in workloads:
            _send_real_unresolved_workload(
                endpoint,
                workload,
                token=token,
                max_tokens=args.max_tokens,
                timeout=args.timeout,
            )
            print(f"  observed 3 users × 4 {workload} requests")
        response = client.post("/allocator/tick", headers=headers)
        response.raise_for_status()
        observed = _status_payload(record)
        selected = _selected_models_for_workloads(observed, workloads)
        for workload, model in selected.items():
            expected = workload_models[workload]
            if model != expected:
                raise SystemExit(
                    f"Configured {workload} capability is {expected!r}, but allocator selected "
                    f"{model!r}."
                )
            print(f"  decision: {workload:<10} → {model}")
        response = client.put(
            "/allocator/mode", headers=headers, json={"mode": "automatic"}
        )
        response.raise_for_status()
        morning = _wait_for_placement(
            record,
            replicas={baseline: 1, **{model: 1 for model in candidate_models}},
            timeout=args.timeout,
            initial=observed,
        )

        proof_results = tuple(
            _real_chat_request(
                endpoint,
                _RealUser(
                    user_id=f"workday-{workload}",
                    role=workload,
                    model=model,
                    prompt=_WORKLOAD_PROMPTS[workload],
                ),
                max_tokens=args.max_tokens,
                timeout=args.timeout,
                retries=3,
                tenant_attestation_secret=token,
            )
            for workload, model in selected.items()
        )
        _require_real_chat(proof_results, label="multi-workload portfolio")
        print(
            f"  served {len(proof_results)}/{len(proof_results)} real specialist responses; "
            f"median {statistics.median(item.elapsed_seconds for item in proof_results):.3f}s.\n"
        )

        if record.get("include_comfyui"):
            image = _run_real_image(record, timeout=args.timeout)
            print(
                f"  media proof: real PNG {image['bytes'] / 1024:.1f} KiB in "
                f"{image['elapsed_seconds']:.1f}s → {image['path']}\n"
            )

        print("[3/5] General-demand surge: replace idle specialists with baseline replicas")
        for candidate in candidate_models:
            _put_test_profile(
                client,
                headers,
                by_model[candidate],
                min_replicas=0,
                max_replicas=1,
                min_failure_domains=1,
                min_residency_seconds=0,
                scale_down_cooldown_seconds=1,
                workload_scores=_workload_scores_for_model(
                    workload_models, candidate
                ),
            )
        # Establish a fresh successful baseline observation before extending its residency horizon;
        # this makes a repeated demo respond to this run rather than merely reviving retained EWMA.
        surge_seed = _real_chat_request(
            endpoint,
            _RealUser(
                user_id="surge-seed",
                role="general",
                model=baseline,
                prompt="Reply with exactly: surge ready",
            ),
            max_tokens=args.max_tokens,
            timeout=args.timeout,
            retries=3,
            tenant_attestation_secret=token,
        )
        _require_real_chat((surge_seed,), label="general-demand seed")
        _put_test_profile(
            client,
            headers,
            by_model[baseline],
            min_replicas=1,
            max_replicas=text_machines,
            min_failure_domains=1,
            min_residency_seconds=0,
            scale_down_cooldown_seconds=300,
        )
        surge_users = tuple(
            _RealUser(
                user_id=f"surge-user-{index + 1:03d}",
                role="general",
                model=baseline,
                prompt="Explain one practical benefit of private local AI in one sentence.",
            )
            for index in range(max(2, args.users))
        )
        surge_results = _run_real_chat_batch(
            endpoint,
            surge_users,
            requests=max(args.requests, text_machines * 4),
            max_tokens=args.max_tokens,
            timeout=args.timeout,
            tenant_attestation_secret=token,
        )
        _require_real_chat(surge_results, label="general-demand surge")
        scaled = _wait_for_placement(
            record,
            replicas={
                baseline: text_machines,
                **{model: 0 for model in candidate_models},
            },
            timeout=args.timeout,
            initial=morning,
        )
        retries = sum(item.attempts - 1 for item in surge_results)
        print(
            f"  Decision: baseline {1}→{text_machines} replicas; optional specialists offloaded."
        )
        print(
            f"  {len(surge_results)} real responses · {retries} cold/saturation retries · "
            f"median {statistics.median(item.elapsed_seconds for item in surge_results):.3f}s.\n"
        )

        print("[4/5] Afternoon shift: general surge ends; non-coding work remains")
        _put_test_profile(
            client,
            headers,
            by_model[baseline],
            min_replicas=1,
            max_replicas=text_machines,
            min_failure_domains=1,
            min_residency_seconds=0,
            scale_down_cooldown_seconds=1,
        )
        time.sleep(1.1)
        _wait_for_placement(
            record,
            replicas={baseline: 1, **{model: 0 for model in candidate_models}},
            timeout=args.timeout,
            initial=scaled,
        )
        shifted_workloads = tuple(
            workload for workload in workloads if workload != "coding"
        )[:2] or (workloads[-1],)
        expected_shift_models = {
            workload_models[workload] for workload in shifted_workloads
        }
        # Freeze actuation before extending the candidate horizon. Retained morning observations
        # are intentionally durable; changing the cooldown while automatic can briefly resurrect
        # them and start a cold load before the fresh afternoon workload mix is established.
        response = client.put(
            "/allocator/mode", headers=headers, json={"mode": "recommend"}
        )
        response.raise_for_status()
        # Active demand must outlive the real load+warm path. Keep unrelated candidates at the
        # one-second expiry used above so yesterday's workload mix cannot reappear merely because
        # one afternoon capability became active again.
        for candidate in expected_shift_models:
            _put_test_profile(
                client,
                headers,
                by_model[candidate],
                min_replicas=0,
                max_replicas=1,
                min_failure_domains=1,
                min_residency_seconds=0,
                scale_down_cooldown_seconds=300,
                workload_scores=_workload_scores_for_model(
                    workload_models, candidate
                ),
            )
        for workload in shifted_workloads:
            _send_real_unresolved_workload(
                endpoint,
                workload,
                token=token,
                max_tokens=args.max_tokens,
                timeout=args.timeout,
            )
        response = client.post("/allocator/tick", headers=headers)
        response.raise_for_status()
        shifted_observed = _status_payload(record)
        shifted = _selected_models_for_workloads(
            shifted_observed, shifted_workloads
        )
        shifted_models = set(shifted.values())
        shifted_plan = dict(shifted_observed.get("plan") or {})
        planned_pairs = [
            (str(item.get("model_id") or ""), str(item.get("node_id") or ""))
            for item in shifted_plan.get("assignments") or []
            if str(item.get("model_id") or "") in {baseline, *shifted_models}
        ]
        if planned_pairs:
            print(
                "  planned placement: "
                + " · ".join(f"{model} on {node}" for model, node in planned_pairs)
            )
        for preemption in shifted_plan.get("preemptions") or []:
            if str(preemption.get("for_model_id") or "") in shifted_models:
                print(
                    "  planned repack: move "
                    f"{preemption.get('model_id')} off {preemption.get('node_id')} for "
                    f"{preemption.get('for_model_id')}"
                )
        response = client.put(
            "/allocator/mode", headers=headers, json={"mode": "automatic"}
        )
        response.raise_for_status()
        afternoon = _wait_for_placement(
            record,
            replicas={
                baseline: 1,
                **{
                    model: int(model in shifted_models)
                    for model in candidate_models
                },
            },
            timeout=args.timeout,
            initial=shifted_observed,
        )
        for workload, model in shifted.items():
            print(f"  decision: {workload:<10} → {model}")
        afternoon_proofs = tuple(
            _real_chat_request(
                endpoint,
                _RealUser(
                    user_id=f"afternoon-{workload}",
                    role=workload,
                    model=model,
                    prompt=_WORKLOAD_PROMPTS[workload],
                ),
                max_tokens=args.max_tokens,
                timeout=args.timeout,
                retries=3,
                tenant_attestation_secret=token,
            )
            for workload, model in shifted.items()
        )
        _require_real_chat(afternoon_proofs, label="shifted afternoon portfolio")
        print(
            "  Coding capacity stayed off because no fresh coding demand remained; "
            "the shared non-coding model was loaded once and served real responses.\n"
        )

        print("[5/5] Demand cools: converge back to the one-replica idle floor")
        for candidate in expected_shift_models:
            _put_test_profile(
                client,
                headers,
                by_model[candidate],
                min_replicas=0,
                max_replicas=1,
                min_failure_domains=1,
                min_residency_seconds=0,
                scale_down_cooldown_seconds=1,
                workload_scores=_workload_scores_for_model(
                    workload_models, candidate
                ),
            )
        time.sleep(1.1)
        final = _wait_for_placement(
            record,
            replicas={baseline: 1, **{model: 0 for model in candidate_models}},
            timeout=args.timeout,
            initial=afternoon,
        )

    history_delta = [
        row
        for row in final.get("history") or []
        if float(row.get("attempted_at") or 0)
        >= float((idle.get("plan") or {}).get("created_at") or 0)
    ]
    succeeded = sum(row.get("status") == "succeeded" for row in history_delta)
    print("Result")
    print(
        "  real user requests served: "
        f"{len(proof_results) + len(surge_results) + len(afternoon_proofs) + 1}"
    )
    print(f"  autonomous lifecycle actions succeeded: {succeeded}")
    print(
        f"  placement sequence: idle baseline=1 → mixed models={1 + len(candidate_models)} "
        f"→ baseline replicas={text_machines} → shifted models={1 + len(shifted_models)} "
        "→ idle baseline=1"
    )
    print("  final state: one ready baseline; every optional text model offloaded")
    return 0


def cmd_test_demo(args: argparse.Namespace) -> int:
    record = _running_record()
    if not record:
        raise SystemExit("Logical test Grid is not running. Start it with `grid test start`.")
    status = _status_payload(record)
    if status.get("status_error"):
        raise SystemExit(f"Cannot read logical Grid status: {status['status_error']}")
    if record.get("workload_models"):
        return _cmd_test_adaptive_workday(args, record, status)
    model = str(record["model"])
    portfolio_model = str(record.get("portfolio_model") or "")
    machines = int(record["machines"])
    text_machines = int(record.get("text_machines") or machines)
    if not portfolio_model:
        raise SystemExit(
            "This Grid has no portfolio model. Restart it with `grid test start "
            "--portfolio-model <cached-gguf>`."
        )
    profiles = [row for row in status.get("models") or [] if row.get("model_id") == model]
    portfolio_profiles = [
        row
        for row in status.get("models") or []
        if row.get("model_id") == portfolio_model
    ]
    if not profiles:
        raise SystemExit(f"The running test Grid has no allocator profile for {model}.")
    if not portfolio_profiles:
        raise SystemExit(
            f"The running test Grid has no allocator profile for {portfolio_model}."
        )
    profile = {
        key: value for key, value in profiles[0].items() if key != "retiring"
    }
    profile.update(
        min_replicas=1,
        max_replicas=text_machines,
        min_failure_domains=1,
        min_residency_seconds=0,
        scale_down_cooldown_seconds=300,
    )
    portfolio_profile = {
        key: value
        for key, value in portfolio_profiles[0].items()
        if key != "retiring"
    }
    portfolio_profile.update(
        min_replicas=0,
        max_replicas=text_machines,
        min_failure_domains=1,
        min_residency_seconds=0,
        scale_down_cooldown_seconds=300,
    )
    token = Path(str(record["token_file"])).read_text(encoding="utf-8").strip()
    headers = {"X-Grid-Allocator-Token": token}
    endpoint = str(record["endpoint"])

    print(f"Real allocator workload · {machines} logical machines on this physical Mac")
    print(f"Baseline model:  {model}")
    print(f"Coding model:    {portfolio_model}")
    if record.get("include_comfyui"):
        print(f"Image model:     {_media_model_for_bundle(str(record.get('media_bundle') or ''))}")
    print(f"Users:           {args.users} real concurrent clients")
    print("Text calls go through /v1/chat/completions to independently managed llama.cpp children.")
    if record.get("include_comfyui"):
        print("The image call goes through /v1/media/image/generate to ComfyUI/PyTorch-MPS.")
    print("No synthetic demand, fabricated latency, or mocked inference response is used.\n")

    with httpx.Client(base_url=endpoint, timeout=10.0, trust_env=False) as client:
        phases = 5 if record.get("include_comfyui") else 4
        print(f"[1/{phases}] Establishing a clean idle placement")
        print("  Existing observations expire naturally; policy keeps one safe text replica.")
        response = client.put(
            f"/allocator/models/{quote(model, safe='')}",
            headers=headers,
            json={**profile, "scale_down_cooldown_seconds": 1},
        )
        response.raise_for_status()
        response = client.put(
            f"/allocator/models/{quote(portfolio_model, safe='')}",
            headers=headers,
            json={**portfolio_profile, "scale_down_cooldown_seconds": 1},
        )
        response.raise_for_status()
        time.sleep(1.1)
        _wait_for_placement(
            record,
            replicas={model: 1, portfolio_model: 0},
            timeout=args.timeout,
            initial=status,
        )
        print(
            f"  Decision: 1 baseline ready; {text_machines - 1} text slot(s) empty for changing "
            "demand.\n"
        )

        # Real replacements need a demand horizon longer than their staged lifecycle. The final
        # phase switches back to a short test-only cooldown before explicitly clearing demand.
        response = client.put(
            f"/allocator/models/{quote(model, safe='')}",
            headers=headers,
            json=profile,
        )
        response.raise_for_status()
        response = client.put(
            f"/allocator/models/{quote(portfolio_model, safe='')}",
            headers=headers,
            json=portfolio_profile,
        )
        response.raise_for_status()

        baseline_user = _RealUser(
            user_id="user-baseline-smoke",
            role="operations",
            model=model,
            prompt="Reply with exactly: baseline ready",
        )
        print(f"[2/{phases}] Sending a real baseline inference request")
        baseline_results = (_real_chat_request(
            endpoint,
            baseline_user,
            max_tokens=args.max_tokens,
            timeout=args.timeout,
        ),)
        _require_real_chat(baseline_results, label="baseline")
        print(
            f"  HTTP 200 · response id {baseline_results[0].response_id} · "
            f"{baseline_results[0].completion_tokens} generated tokens · "
            f"{baseline_results[0].elapsed_seconds:.3f}s"
        )
        print(f"  Actual response: {baseline_results[0].text[:160]!r}\n")

        print(f"[3/{phases}] Proactively allocating from unresolved coding requests")
        print(
            "  Three genuine requests target unresolved 'auto'. The router is not involved; "
            "the allocator must classify their bounded features and select a portfolio canary."
        )
        unresolved = tuple(
            _RealUser(
                user_id=f"user-unresolved-coding-{index}",
                role="software-engineer",
                model="auto",
                prompt=(
                    "Debug this Python API function, explain the bug, and propose one unit test."
                ),
            )
            for index in range(1, 4)
        )
        unresolved_results = tuple(
            _real_chat_request(
                endpoint,
                user,
                max_tokens=args.max_tokens,
                timeout=args.timeout,
            )
            for user in unresolved
        )
        unexpected = [item for item in unresolved_results if item.status_code != 503]
        if unexpected:
            codes = ", ".join(str(item.status_code) for item in unexpected)
            raise SystemExit(
                "Expected unresolved requests to receive genuine HTTP 503 responses with the "
                f"router uninvolved; got {codes}."
            )
        print("  Real unresolved responses: 3 × HTTP 503 (no fabricated inference result)")
        coding = _wait_for_demand_ready(
            record,
            model=portfolio_model,
            timeout=args.timeout,
            initial=_status_payload(record),
        )
        projection = next(
            (
                row
                for row in coding.get("portfolio_projections") or []
                if row.get("chosen_model") == portfolio_model
            ),
            {},
        )
        print(
            f"  Allocator classified {projection.get('workload') or 'coding'} work and made "
            f"{_ready_replicas(coding, portfolio_model)} specialist replica(s) routable."
        )

        specialist_probe = _RealUser(
            user_id="user-specialist-probe",
            role="software-engineer",
            model=portfolio_model,
            prompt="Debug this Python API and propose one unit test.",
        )
        specialist_result = _real_chat_request(
            endpoint,
            specialist_probe,
            max_tokens=args.max_tokens,
            timeout=args.timeout,
        )
        _require_real_chat((specialist_result,), label="proactively allocated specialist")
        print(
            f"  First named specialist call now returns real HTTP 200 · "
            f"{specialist_result.completion_tokens} generated tokens · "
            f"{specialist_result.elapsed_seconds:.3f}s\n"
        )

        print(f"[4/{phases}] Running real concurrent traffic from multiple users")
        users = _real_users(args.users, baseline=model, specialist=portfolio_model)
        before_samples = _performance_samples(coding)
        results = _run_real_chat_batch(
            endpoint,
            users,
            requests=args.requests,
            max_tokens=args.max_tokens,
            timeout=args.timeout,
        )
        _require_real_chat(results, label="multi-user")
        latencies = [item.elapsed_seconds for item in results]
        status_after_traffic = _status_payload(record)
        after_samples = _performance_samples(status_after_traffic)
        serving_nodes = sorted(
            node_id
            for (node_id, served_model), samples in after_samples.items()
            if samples > before_samples.get((node_id, served_model), 0)
        )
        print(
            f"  {len(results)}/{len(results)} requests returned real assistant text · "
            f"median {statistics.median(latencies):.3f}s · max {max(latencies):.3f}s"
        )
        retry_count = sum(item.attempts - 1 for item in results)
        print(f"  Bounded saturation/cold-start retries: {retry_count}")
        print(f"  Measured serving nodes: {', '.join(serving_nodes) or 'awaiting heartbeat samples'}")
        for item in sorted(results, key=lambda row: (row.role, row.user_id))[:3]:
            print(
                f"    {item.user_id}/{item.role} → {item.model}: "
                f"{item.text[:100]!r}"
            )
        print()

        image_result: dict[str, Any] | None = None
        if record.get("include_comfyui"):
            print(f"[5/{phases}] Generating a real image through ComfyUI/PyTorch-MPS")
            image_result = _run_real_image(record, timeout=args.timeout)
            print(
                f"  PNG received: {image_result['bytes'] / 1024:.1f} KiB in "
                f"{image_result['elapsed_seconds']:.1f}s"
            )
            print(f"  Saved actual output: {image_result['path']}\n")

        print("[cleanup] Letting only observed real work expire")
        print("  No demand is injected or cleared; the configured one-second cooldown expires naturally.")
        response = client.put(
            f"/allocator/models/{quote(model, safe='')}",
            headers=headers,
            json={**profile, "scale_down_cooldown_seconds": 1},
        )
        response.raise_for_status()
        response = client.put(
            f"/allocator/models/{quote(portfolio_model, safe='')}",
            headers=headers,
            json={**portfolio_profile, "scale_down_cooldown_seconds": 1},
        )
        response.raise_for_status()
        time.sleep(1.1)
        final = _wait_for_placement(
            record,
            replicas={model: 1, portfolio_model: 0},
            timeout=args.timeout,
            initial=_status_payload(record),
        )
        print("  Decision: safely returned to one baseline replica after real drain/unload guards.\n")

    print("Summary")
    print(f"  logical machines   {machines} total · {text_machines} llama.cpp" + (" · 1 ComfyUI" if record.get("include_comfyui") else ""))
    print(
        f"  real text requests {5 + len(results)} attempted · {2 + len(results)} successful · "
        "3 expected unresolved 503s"
    )
    print(f"  final placement    baseline={_ready_replicas(final, model)} · specialist={_ready_replicas(final, portfolio_model)}")
    if image_result is not None:
        print(f"  real image          {image_result['path']}")
    print("Every successful result above came from a real engine process on this Mac.")
    print("Run `grid test status` for the final placement or `grid test demo` to replay it.")
    return 0


def cmd_test_stop(args: argparse.Namespace) -> int:
    record = _load_record()
    if not record or not run_records.record_alive(record):
        _record_path().unlink(missing_ok=True)
        payload = {"running": False, "stopped": False}
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print("Logical test Grid is not running.")
        return 0
    outcome = run_records.terminate_recorded(record)
    if outcome.survivor:
        raise SystemExit(
            f"Logical test Grid did not stop; {run_records.describe_survivor(outcome)} survived."
        )
    _record_path().unlink(missing_ok=True)
    payload = {
        "running": False,
        "stopped": True,
        "run_dir": record.get("run_dir"),
        "log": record.get("log"),
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print("Logical test Grid stopped; all managed model processes were drained and unloaded.")
        print(f"  log  {record.get('log')}")
    return 0
