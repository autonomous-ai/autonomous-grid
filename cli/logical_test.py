"""Development CLI for a persistent, single-machine logical Grid."""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
import json
import os
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
        "text_machines": int(record.get("text_machines") or 0) if record else 0,
        "include_comfyui": bool(record.get("include_comfyui", False)) if record else False,
        "media_bundle": str(record.get("media_bundle") or "") if record else "",
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
            nodes=status.get("nodes") or [],
            models=status.get("models") or [],
            forecasts=status.get("forecasts") or [],
            workload_forecasts=status.get("workload_forecasts") or [],
            portfolio_projections=status.get("portfolio_projections") or [],
            model_workload_outcomes=status.get("model_workload_outcomes") or [],
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
    if payload.get("portfolio_model"):
        print(f"  portfolio {payload['portfolio_model']}")
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
    existing = _running_record()
    if existing:
        if (
            int(existing.get("machines") or 0) != args.machines
            or str(existing.get("model") or "") != args.model
            or str(existing.get("portfolio_model") or "") != args.portfolio_model
            or bool(existing.get("include_comfyui", False)) != args.include_comfyui
            or str(existing.get("media_bundle") or "")
            != (args.media_bundle if args.include_comfyui else "")
        ):
            raise SystemExit(
                "A logical test Grid is already running with different settings; "
                "run `grid test stop` first."
            )
        _print_status(_status_payload(existing), as_json=args.json)
        return 0

    try:
        artifact_sha256 = LlamaCppBackend().artifact_sha256(args.model)
        if args.portfolio_model:
            LlamaCppBackend().artifact_sha256(args.portfolio_model)
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
            "include_comfyui": args.include_comfyui,
            "media_bundle": args.media_bundle if args.include_comfyui else "",
            "comfyui_port": args.comfyui_port,
            "media_port": args.media_port,
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
        "include_comfyui": args.include_comfyui,
        "media_bundle": args.media_bundle if args.include_comfyui else "",
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


def _real_chat_request(
    endpoint: str,
    user: _RealUser,
    *,
    max_tokens: int,
    timeout: float,
    retries: int = 0,
) -> _RealChatResult:
    request_started = time.monotonic()
    result: _RealChatResult | None = None
    for attempt in range(retries + 1):
        result = _real_chat_request_once(
            endpoint,
            user,
            max_tokens=max_tokens,
            timeout=timeout,
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
) -> _RealChatResult:
    started = time.monotonic()
    try:
        with httpx.Client(base_url=endpoint, timeout=timeout, trust_env=False) as client:
            response = client.post(
                "/v1/chat/completions",
                headers={"X-Grid-Affinity-Key": user.user_id},
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


def cmd_test_demo(args: argparse.Namespace) -> int:
    record = _running_record()
    if not record:
        raise SystemExit("Logical test Grid is not running. Start it with `grid test start`.")
    status = _status_payload(record)
    if status.get("status_error"):
        raise SystemExit(f"Cannot read logical Grid status: {status['status_error']}")
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
