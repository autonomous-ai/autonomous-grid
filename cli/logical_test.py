"""Development CLI for a persistent, single-machine logical Grid."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx

from shared import jsonio, paths, run_records
from shared.allocator.runtime import LlamaCppBackend

DEFAULT_MODEL = "SmolLM2-135M-Instruct-Q3_K_M.gguf"
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


def _root() -> Path:
    return paths.run_dir() / "logical-test"


def _record_path() -> Path:
    return _root() / "supervisor.json"


def _load_record() -> dict[str, Any]:
    return jsonio.load_json(_record_path())


def _running_record() -> dict[str, Any]:
    record = _load_record()
    return record if record and run_records.record_alive(record) else {}


def _assert_ports_available(port: int, engine_port_base: int, machines: int) -> None:
    ports = [port]
    ports.extend(
        engine_port_base + index * 10 + offset
        for index in range(machines)
        for offset in range(4)
    )
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
        print(f"  {node.get('node_id')}  {node.get('state')}  {placement}")
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
        ):
            raise SystemExit(
                "A logical test Grid is already running with different settings; "
                "run `grid test stop` first."
            )
        _print_status(_status_payload(existing), as_json=args.json)
        return 0

    try:
        artifact_sha256 = LlamaCppBackend().artifact_sha256(args.model)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    _assert_ports_available(args.port, args.engine_port_base, args.machines)

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
            "model": args.model,
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
        "model": args.model,
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
        if int(ready.get("ready_replicas") or 0) == args.machines:
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
