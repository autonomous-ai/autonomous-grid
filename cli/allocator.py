"""Operator commands for Grid's dynamic model allocator."""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from local import config, runtime
from shared import jsonio, paths, run_records
from shared.allocator.auth import (
    DEFAULT_NODE_TOKEN_TTL_SECONDS,
    decode_node_token,
    mint_node_token,
    secure_control_transport,
)
from shared.allocator.local import LocalOverride
from shared.allocator.models import ModelProfile, stable_digest
from shared.allocator.runtime import (
    LlamaCppBackend,
    ManagedModelRuntime,
    clear_local_override,
    local_override_path,
    shutdown_request_path,
    write_local_override,
)
from shared.filelock import file_lock

OPERATOR_TOKEN_ENV = "GRID_ALLOCATOR_CONTROL_TOKEN"
NODE_TOKEN_ENV = "GRID_ALLOCATOR_NODE_TOKEN"
# Compatibility name for callers that imported the original operator-token constant.
TOKEN_ENV = OPERATOR_TOKEN_ENV
NODE_STARTUP_TIMEOUT_SECONDS = 60.0
NODE_HEARTBEAT_CYCLE_SECONDS = 30.0
NODE_REGISTRY_TTL_FALLBACK_SECONDS = 60.0
NODE_SHUTDOWN_DRAIN_SECONDS = 15.0
NODE_SHUTDOWN_SCHEDULING_MARGIN_SECONDS = 5.0
# A stop request can arrive just after a heartbeat starts; that in-flight cycle may renew an
# ACCEPTING route near its 30-second deadline. The daemon can then need a full 60-second lease
# expiry plus its normal 15-second drain budget. Do not signal its process tree before that whole
# safety path and a small scheduling margin have elapsed. Startup failure uses the same bound: the
# daemon can publish a child route before a later startup-marker or run-record write fails, and the
# parent has no durable proof that an apparently incomplete startup was never routable.
NODE_STOP_COOPERATIVE_GRACE_SECONDS = (
    NODE_HEARTBEAT_CYCLE_SECONDS
    + NODE_REGISTRY_TTL_FALLBACK_SECONDS
    + NODE_SHUTDOWN_DRAIN_SECONDS
    + NODE_SHUTDOWN_SCHEDULING_MARGIN_SECONDS
)
NODE_STARTUP_CLEANUP_GRACE_SECONDS = NODE_STOP_COOPERATIVE_GRACE_SECONDS
REMOTE_ENROLLMENT_READY_TIMEOUT_SECONDS = 15.0
REMOTE_ENROLLMENT_RETRY_SECONDS = 0.25


def cmd_allocator_status(args: argparse.Namespace) -> int:
    cfg = config.select_grid(getattr(args, "grid", None))
    payload = _request(cfg, "GET", "/allocator/status")
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
        return 0
    models = payload.get("models") or []
    nodes = payload.get("nodes") or []
    pending = payload.get("pending_commands") or []
    plan = payload.get("plan") or {}
    unsatisfied = plan.get("unsatisfied") or []
    print(f"Allocator {payload.get('mode', 'unknown')} · {len(nodes)} hosts · {len(models)} models")
    print(f"  pending mutations  {len(pending)}")
    print(f"  unmet constraints  {len(unsatisfied)}")
    authority = payload.get("authority") or {}
    if authority:
        print(
            f"  controller authority term {authority.get('term', 0)} · "
            f"{'held' if authority.get('held') else 'not held'}"
        )
    if payload.get("last_tick_at"):
        print(f"  last tick          {payload['last_tick_at']:.3f}")
    portfolio_policy = payload.get("portfolio_policy") or {}
    if portfolio_policy.get("joint"):
        selected_models = portfolio_policy.get("selected_models") or []
        print(
            f"  joint portfolio    {int(portfolio_policy.get('workloads') or 0)} workloads"
            f" -> {len(selected_models)} models"
        )
        if portfolio_policy.get("objective"):
            print(f"  objective          {portfolio_policy['objective']}")
        if portfolio_policy.get("exploration_models"):
            print(
                "  exploration slot  "
                + ", ".join(
                    str(item) for item in portfolio_policy["exploration_models"]
                )
            )
    projection_by_workload = {
        str(row.get("workload") or ""): row
        for row in payload.get("portfolio_projections") or []
        if isinstance(row, dict)
    }
    for admission in payload.get("portfolio_admissions") or []:
        workload = str(admission.get("workload") or "unknown")
        model_id = admission.get("model_id") or "no-model"
        ready = int(admission.get("ready_replicas") or 0)
        desired = int(admission.get("desired_replicas") or 0)
        print(
            f"  workload {workload:<10} "
            f"{admission.get('state') or 'unknown'} via {model_id} · "
            f"{ready}/{desired} ready"
        )
        projection = projection_by_workload.get(workload) or {}
        sequence_sources = projection.get("demand_correlation_sources") or ()
        if sequence_sources:
            confidence = 100.0 * float(
                projection.get("demand_correlation_confidence") or 0.0
            )
            print(
                "    proactive         learned workflow "
                + ", ".join(str(item) for item in sequence_sources)
                + f" → {workload} · {confidence:.0f}% confidence"
            )
        if admission.get("state") != "ready" and admission.get("reason"):
            print(f"    why              {admission['reason']}")
    if payload.get("last_error"):
        print(f"  error              {payload['last_error']}")
    if payload.get("warning"):
        print(f"  warning            {payload['warning']}")
    for node in nodes:
        ready = [
            item["model_id"]
            for item in node.get("residencies") or []
            if item.get("state") == "ready"
        ]
        disk = node.get("disk_available_mb")
        disk_text = f" · {disk} MB disk free" if isinstance(disk, int) else ""
        print(
            f"  {node.get('node_id')}  {node.get('state')}  "
            f"{node.get('capacity_mb', 0)} MB{disk_text}  {','.join(ready) or '-'}"
        )
    return 0


def cmd_allocator_model_set(args: argparse.Namespace) -> int:
    cfg = config.select_grid(getattr(args, "grid", None))
    maximum = args.max_replicas if args.max_replicas is not None else max(1, args.min_replicas)
    runtimes = tuple(args.runtimes or ("llama.cpp",))
    try:
        runtime_memory_mb = tuple(
            (runtime.strip(), int(memory_mb))
            for value in args.runtime_memory_mb
            for runtime, separator, memory_mb in (value.partition("="),)
            if separator
        )
        if len(runtime_memory_mb) != len(args.runtime_memory_mb):
            raise ValueError("--runtime-memory-mb must use RUNTIME=MB")
        workload_scores = tuple(
            (workload.strip().lower(), float(score))
            for value in args.workload_score
            for workload, separator, score in (value.partition("="),)
            if separator
        )
        if len(workload_scores) != len(args.workload_score):
            raise ValueError("--workload-score must use WORKLOAD=SCORE")
        profile = ModelProfile(
            model_id=args.model,
            memory_mb=args.memory_mb,
            runtime_memory_mb=runtime_memory_mb,
            workload_scores=workload_scores,
            runtimes=runtimes,
            backends=tuple(args.backends),
            data_tier=args.data_tier,
            required_tags=tuple(args.required_tags),
            forbidden_tags=tuple(args.forbidden_tags),
            pinned_nodes=tuple(args.pinned_nodes),
            min_replicas=args.min_replicas,
            max_replicas=maximum,
            target_utilization=args.target_utilization,
            replica_concurrency=args.replica_concurrency,
            expected_service_seconds=args.service_seconds,
            latency_slo_ms=args.latency_slo_ms,
            priority=args.priority,
            load_seconds=args.load_seconds,
            warm_seconds=args.warm_seconds,
            min_residency_seconds=args.min_residency_seconds,
            scale_down_cooldown_seconds=args.scale_down_cooldown_seconds,
            min_failure_domains=args.min_failure_domains,
            min_gpu_count=args.min_gpu_count,
            min_gpu_memory_mb=args.min_gpu_memory_mb,
            artifact_sha256=args.artifact_sha256,
            artifact_source=args.artifact_source,
            artifact_size_mb=args.artifact_size_mb,
            max_colocated_models=args.max_colocated_models,
            colocation_excludes=tuple(args.colocation_excludes),
        )
    except ValueError as exc:
        raise SystemExit(f"Invalid allocator model profile: {exc}") from exc
    payload = _request(
        cfg,
        "PUT",
        f"/allocator/models/{quote(args.model, safe='')}",
        body=profile.to_dict(),
        token=_control_token(cfg, getattr(args, "token_file", None)),
        allow_insecure_http=getattr(args, "allow_insecure_http", False),
    )
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
    else:
        print(
            f"Allocator profile set: {args.model} "
            f"({profile.memory_mb} MB, replicas {profile.min_replicas}–{profile.max_replicas})"
        )
    return 0


def cmd_allocator_model_remove(args: argparse.Namespace) -> int:
    cfg = config.select_grid(getattr(args, "grid", None))
    payload = _request(
        cfg,
        "DELETE",
        f"/allocator/models/{quote(args.model, safe='')}",
        token=_control_token(cfg, getattr(args, "token_file", None)),
        allow_insecure_http=getattr(args, "allow_insecure_http", False),
    )
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
    else:
        print(f"Allocator profile retired: {args.model} (managed replicas will drain safely)")
    return 0


def cmd_allocator_mode(args: argparse.Namespace) -> int:
    cfg = config.select_grid(getattr(args, "grid", None))
    payload = _request(
        cfg,
        "PUT",
        "/allocator/mode",
        body={"mode": args.allocator_mode},
        token=_control_token(cfg, getattr(args, "token_file", None)),
        allow_insecure_http=getattr(args, "allow_insecure_http", False),
    )
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
    else:
        print(f"Allocator mode: {payload['mode']}")
    return 0


def cmd_allocator_tick(args: argparse.Namespace) -> int:
    cfg = config.select_grid(getattr(args, "grid", None))
    payload = _request(
        cfg,
        "POST",
        "/allocator/tick",
        token=_control_token(cfg, getattr(args, "token_file", None)),
        allow_insecure_http=getattr(args, "allow_insecure_http", False),
    )
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
    else:
        actions = payload.get("actions") or []
        deferred = payload.get("deferred") or []
        print(f"Allocator tick: {len(actions)} actions, {len(deferred)} deferred")
    return 0


def cmd_allocator_token_write(args: argparse.Namespace) -> int:
    """Mint a host-scoped node capability without copying the operator secret."""

    cfg = config.select_grid(getattr(args, "grid", None))
    operator_token = _control_token(cfg, getattr(args, "token_file", None))
    host_id = str(args.host_id or f"host-{uuid.uuid4().hex[:16]}")
    ttl_seconds = int(args.ttl_days) * 24 * 60 * 60
    try:
        token = mint_node_token(operator_token, host_id, ttl_seconds=ttl_seconds)
    except ValueError as exc:
        raise SystemExit(f"Cannot mint allocator node credential: {exc}") from exc
    target = Path(args.path).expanduser()
    if target.exists() and not args.force:
        raise SystemExit(f"Refusing to overwrite {target}; pass --force to replace it.")
    jsonio.atomic_write_bytes(target, f"{token}\n".encode(), mode=0o600)
    try:
        jsonio.restrict_owner_only(target)
    except OSError as exc:
        target.unlink(missing_ok=True)
        raise SystemExit(f"Cannot secure allocator node credential file: {exc}") from exc
    print(
        f"Allocator node credential written to {target} "
        f"(host={host_id}, expires in {args.ttl_days} days, owner-readable only)."
    )
    return 0


def cmd_allocator_node_start(args: argparse.Namespace) -> int:
    cfg = config.select_grid(getattr(args, "grid", None))
    grid_url = runtime.grid_url(cfg)
    grid_info = _request(cfg, "GET", "/grid/info")
    grid_id = _validated_grid_id(grid_info.get("grid_id"))
    scope = _scope(grid_id)
    record_path = _node_record_path(scope)
    with file_lock(record_path):
        return _start_allocator_node_locked(
            args,
            cfg=cfg,
            grid_url=grid_url,
            grid_id=grid_id,
            scope=scope,
            record_path=record_path,
        )


def cmd_allocator_join(args: argparse.Namespace) -> int:
    """Enroll this already-serving remote provider as allocator-managed capacity."""

    from remote import credentials

    from . import remote_grid

    session = credentials.require_session()
    rec = remote_grid._select(getattr(args, "grid", None))
    network_id = remote_grid._network_id(rec)
    label = str(rec.get("name") or network_id)
    access_token = remote_grid.require_access_token(rec, label)
    relay_url, _status = remote_grid.resolve_relay_base(
        session, rec, network_id, label
    )
    relay_url = relay_url.rstrip("/")
    if not secure_control_transport(relay_url):
        raise SystemExit(
            "Allocator enrollment requires an HTTPS relay (literal loopback HTTP is also allowed)."
        )

    # Enrollment is capacity opt-in, not provider admission.  Refuse before minting anything unless
    # this exact remote identity is already alive and supports allocator-owned route hot reload.
    _remote_provider_network_id(network_id)
    cfg = {
        "grid_id": network_id,
        "name": label,
        "managed_server": False,
        "lan_signaling_url": f"{relay_url}/allocator-control",
    }
    grid_info = _request(cfg, "GET", "/grid/info")
    grid_id = _validated_grid_id(grid_info.get("grid_id"))
    scope = _scope(grid_id)
    record_path = _node_record_path(scope)
    if getattr(args, "restart", False):
        existing = jsonio.load_json(record_path)
        if _node_process_state(existing) == "owned":
            print(f"Gracefully restarting allocator node (pid={_record_pid(existing)}) ...")
            # Do not retain the run-record lock while the child fences remote routes: its publisher
            # takes the same lock to perform the ordered drain, and holding it here would deadlock.
            if not stop_allocator_node_for_grid(cfg):
                raise SystemExit("Allocator node could not be stopped for restart.")
    with file_lock(record_path):
        record = jsonio.load_json(record_path)
        if _node_process_state(record) == "owned":
            print(f"Allocator node already running (pid={_record_pid(record)})")
            return 0
        # The orchestrator always advertises llama.cpp as a managed runtime. A fresh capacity node
        # must therefore have the executable before it enrolls; otherwise the controller can make
        # a valid placement that is guaranteed to fail at warm time. Enrollment is the explicit
        # lifecycle opt-in, and the installer is a version- and SHA-256-pinned Grid artifact.
        from .engine import ensure_allocator_llama_cpp

        ensure_allocator_llama_cpp()
        node_token = _request_remote_enrollment(relay_url, access_token, label)
        args.provider_grid = network_id
        args.token_file = None
        args.advertise_host = None
        args.engine_tls_cert = None
        args.engine_tls_key = None
        args.engine_tls_ca = None
        args.allow_insecure_http = False
        return _start_allocator_node_locked(
            args,
            cfg=cfg,
            grid_url=runtime.grid_url(cfg),
            grid_id=grid_id,
            scope=scope,
            record_path=record_path,
            supplied_node_token=node_token,
        )


def _start_allocator_node_locked(
    args: argparse.Namespace,
    *,
    cfg: dict[str, Any],
    grid_url: str,
    grid_id: str,
    scope: str,
    record_path: Path,
    supplied_node_token: str = "",
) -> int:
    record = jsonio.load_json(record_path)
    pid = _record_pid(record)
    process_state = _node_process_state(record)
    if process_state == "owned":
        print(f"Allocator node already running (pid={pid})")
        return 0
    if process_state == "ambiguous":
        raise SystemExit(
            f"Allocator node pid {pid} is alive but its identity cannot be verified; refusing "
            f"to start a duplicate. See {record.get('log_path') or 'the allocator log'}."
        )
    if process_state in ("dead", "foreign"):
        # Preserve proven children for the replacement runtime to adopt. Its first control record
        # starts DRAINING while it reconciles persisted child tombstones, so it can update/delete
        # registry routes before deciding whether any process must stop. Killing here would leave
        # the old READY route pointing at a dead port for the rest of its lease.
        record_path.unlink(missing_ok=True)

    state_path = _node_state_path(scope)
    token, host_id = _node_token(
        cfg,
        getattr(args, "token_file", None),
        state_path,
        supplied_token=supplied_node_token,
        allow_stopped_host_rebind=bool(getattr(args, "provider_grid", None)),
    )
    control_url = runtime.allocator_control_url(cfg)
    provider_network_id = _remote_provider_network_id(
        getattr(args, "provider_grid", None)
    )
    effective_advertise_host = (
        "127.0.0.1"
        if provider_network_id
        else (
            args.advertise_host
            or _literal_loopback_url_host(control_url)
            or runtime.detect_local_ip_for_url(grid_url)
        )
    )
    tls_cert, tls_key, tls_ca = _validated_engine_tls_files(
        getattr(args, "engine_tls_cert", None),
        getattr(args, "engine_tls_key", None),
        getattr(args, "engine_tls_ca", None),
    )
    if not secure_control_transport(control_url):
        raise SystemExit(
            "Allocator nodes carrying private engine credentials require an HTTPS Grid control "
            "URL (literal loopback HTTP is also allowed). --allow-insecure-http cannot expose "
            "managed engine keys on a LAN."
        )
    if (
        not provider_network_id
        and not _literal_loopback_host(effective_advertise_host)
        and not tls_cert
    ):
        raise SystemExit(
            "A non-loopback managed engine requires end-to-end TLS. Pass "
            "--engine-tls-cert, --engine-tls-key, and (for a private CA) --engine-tls-ca."
        )
    shutdown_request_path(state_path).unlink(missing_ok=True)
    startup_path = _node_startup_path(scope)
    startup_path.unlink(missing_ok=True)
    log_path = _node_log_path(scope)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("ab")
    selector = _allocator_node_selector(cfg)
    instance_id = uuid.uuid4().hex
    command = runtime.cli_command() + [
        "__allocator-node",
        selector,
        "--state-path",
        str(state_path),
        "--instance-id",
        instance_id,
        "--startup-path",
        str(startup_path),
        "--heartbeat-interval",
        str(args.heartbeat_interval),
    ]
    command.extend(["--advertise-host", effective_advertise_host])
    if provider_network_id:
        command.extend(["--provider-grid-id", provider_network_id])
    if getattr(args, "dedicated", False):
        command.append("--dedicated")
    if tls_cert:
        command.extend(["--engine-tls-cert", tls_cert, "--engine-tls-key", tls_key])
    if tls_ca:
        command.extend(["--engine-tls-ca", tls_ca])
    # The public compatibility spelling is deliberately not forwarded. Managed children never
    # receive permission to put their node credential on non-loopback HTTP.
    child_env = {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
        NODE_TOKEN_ENV: token,
    }
    # A shell-level operator credential must not leak into the long-running worker process.
    child_env.pop(OPERATOR_TOKEN_ENV, None)
    try:
        process = subprocess.Popen(
            command,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=child_env,
        )
    finally:
        log.close()
    try:
        record_data = {
            "pid": process.pid,
            "instance_id": instance_id,
            # The unique argv instance id is already a safe PID-reuse fence. Persist the run record
            # before doing the slower OS birth-marker lookup so a parent crash cannot orphan the
            # newly spawned daemon without any durable handle.
            "process_start_marker": "",
            "grid_id": grid_id,
            "grid_selector": selector,
            "grid_url": grid_url,
            "host_id": host_id,
            "provider_grid_id": provider_network_id,
            "dedicated": bool(getattr(args, "dedicated", False)),
            "state_path": str(state_path),
            "startup_path": str(startup_path),
            "graceful_shutdown": True,
            "shutdown_request_path": str(shutdown_request_path(state_path)),
            "log_path": str(log_path),
            "started_at": time.time(),
        }
        jsonio.atomic_write_json(record_path, record_data)
        birth_marker = _await_process_start_marker(process.pid)
        if birth_marker is None:
            raise RuntimeError("could not capture allocator node process birth marker")
        record_data["process_start_marker"] = birth_marker
        jsonio.atomic_write_json(record_path, record_data)
        _await_node_start(process, startup_path, instance_id, log_path)
    except BaseException:
        request_path = shutdown_request_path(state_path)
        try:
            # The instance nonce makes a stale request harmless to a later node. Give this exact
            # daemon the same graceful drain path used by `grid allocator node stop` before any
            # process-tree escalation.
            jsonio.atomic_write_json(
                request_path,
                {"instance_id": instance_id, "requested_at": time.time()},
                mode=0o600,
            )
        except OSError:
            # The startup failure may itself be a full/unwritable filesystem. Cleanup must still
            # contain the detached process tree even when its cooperative request cannot persist.
            pass
        _terminate_spawned_process(process)
        if process.poll() is not None:
            request_path.unlink(missing_ok=True)
            record_path.unlink(missing_ok=True)
            startup_path.unlink(missing_ok=True)
        raise
    started = jsonio.load_json(startup_path)
    print(f"Allocator node started (pid={process.pid}, host={started.get('host_id', host_id)})")
    print(f"log={log_path}")
    return 0


def _remote_provider_network_id(selector: str | None) -> str:
    """Resolve a remote Grid name/id only when provider-route integration was requested."""

    if not selector:
        return ""
    from remote import credentials

    matches = [
        item
        for item in (credentials.load_credentials().get("networks") or [])
        if isinstance(item, dict)
        and (item.get("network_id") == selector or item.get("name") == selector)
    ]
    if not matches:
        raise SystemExit(
            f"Remote provider Grid not found: {selector!r}. Run `grid sync` and join it first."
        )
    network_id = str(matches[-1].get("network_id") or "")
    record = run_records.read_record(network_id, run_records.REMOTE_IDENTITY)
    if not record or not run_records.record_alive(record):
        raise SystemExit(
            f"This machine is not actively serving remote Grid {selector!r}; run `grid join` first."
        )
    if record.get("reload_signal") != "sighup":
        raise SystemExit(
            "The running remote provider predates allocator hot reload; rejoin it once with "
            "`grid join --respawn`."
        )
    return network_id


def _allocator_node_selector(cfg: dict[str, Any]) -> str:
    """Return a selector the detached child can resolve in its fresh process.

    Locally managed grids are persisted by id.  A synthesized remote enrollment config exists only
    in the parent process, so its child must receive the allocator-control URL instead of the remote
    network id.
    """

    if cfg.get("managed_server", True):
        return str(cfg["grid_id"])
    return runtime.grid_url(cfg)


def cmd_allocator_node_stop(args: argparse.Namespace) -> int:
    cfg = config.select_grid(getattr(args, "grid", None))
    stopped = stop_allocator_node_for_grid(cfg)
    if not stopped:
        print("Allocator node is not running.")
        return 0
    print("Allocator node stopped.")
    return 0


def stop_allocator_node_for_grid(cfg: dict[str, Any]) -> bool:
    """Stop this machine's managed node before its signaling server goes away."""

    scope = _scope_for_grid(cfg, allow_offline=True)
    path = _node_record_path(scope)
    with file_lock(path):
        return _stop_allocator_node_locked(scope, path)


def _stop_allocator_node_locked(scope: str, path: Path) -> bool:
    record = jsonio.load_json(path)
    pid = _record_pid(record)
    state_path = Path(record.get("state_path") or _node_state_path(scope))
    process_state = _node_process_state(record)
    if process_state in ("dead", "foreign"):
        cleaned_children = _stop_persisted_allocator_children(state_path)
        path.unlink(missing_ok=True)
        _node_startup_path(scope).unlink(missing_ok=True)
        return cleaned_children
    request_path: Path | None = None
    if record.get("graceful_shutdown"):
        state_path = Path(record.get("state_path") or _node_state_path(scope))
        request_path = shutdown_request_path(state_path)
        request_written = False
        try:
            jsonio.atomic_write_json(
                request_path,
                {
                    "instance_id": str(record.get("instance_id") or ""),
                    "requested_at": time.time(),
                },
                mode=0o600,
            )
            request_written = True
        except OSError:
            # Disk exhaustion must not prevent the exact-PID signal fallback below.
            pass
        # The daemon polls this file every 250 ms. Its whole network-drain/process-stop sequence is
        # bounded; leave a little scheduling margin before considering a signal fallback.
        deadline = time.monotonic() + NODE_STOP_COOPERATIVE_GRACE_SECONDS
        while request_written and time.monotonic() < deadline and run_records.pid_alive(pid):
            time.sleep(0.1)
        if not run_records.pid_alive(pid):
            _stop_persisted_allocator_children(state_path)
            request_path.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            _node_startup_path(scope).unlink(missing_ok=True)
            return True
    # Re-prove identity immediately before any signal. The original daemon could have exited and
    # its PID been reused while we waited for cooperative shutdown.
    process_state = _node_process_state(record)
    if process_state == "dead":
        _stop_persisted_allocator_children(state_path)
        path.unlink(missing_ok=True)
        _node_startup_path(scope).unlink(missing_ok=True)
        return True
    if process_state == "foreign":
        _stop_persisted_allocator_children(state_path)
        path.unlink(missing_ok=True)
        _node_startup_path(scope).unlink(missing_ok=True)
        return True
    if process_state != "owned":
        raise SystemExit(
            f"Allocator node pid {pid} did not answer its cooperative stop request, and its "
            "identity cannot be verified. Refusing to signal a possibly unrelated process."
        )
    if not run_records.terminate_pid(
        pid,
        identity_check=lambda: _node_process_state(record) == "owned",
    ):
        raise SystemExit(f"Allocator node pid {pid} did not stop; see {record.get('log_path')}")
    _stop_persisted_allocator_children(state_path)
    if request_path is not None:
        request_path.unlink(missing_ok=True)
    path.unlink(missing_ok=True)
    _node_startup_path(scope).unlink(missing_ok=True)
    return True


def cmd_allocator_node_status(args: argparse.Namespace) -> int:
    cfg = config.select_grid(getattr(args, "grid", None))
    scope = _scope_for_grid(cfg, allow_offline=True)
    record = jsonio.load_json(_node_record_path(scope))
    pid = _record_pid(record)
    process_state = _node_process_state(record)
    running = process_state == "owned"
    state_path = Path(record.get("state_path") or _node_state_path(scope))
    state = jsonio.load_json(state_path)
    override = _read_local_override_status(state_path)
    payload = {
        "running": running,
        "pid": pid if running else 0,
        "process_state": process_state,
        "host_id": state.get("host_id"),
        "residencies": state.get("residencies") or [],
        "local_override": override,
        "log_path": record.get("log_path") or str(_node_log_path(scope)),
    }
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
    elif running:
        ready = [
            row["model_id"]
            for row in payload["residencies"]
            if row.get("state") == "ready"
        ]
        print(f"Allocator node running (pid={pid}, host={payload['host_id']})")
        print(f"  ready  {','.join(ready) or '-'}")
        if override:
            print(f"  local  {override['state']} ({override['reason']})")
        print(f"  log    {payload['log_path']}")
    else:
        print("Allocator node is not running.")
        if process_state == "ambiguous":
            print(f"  warning  pid {pid} is live but cannot be verified as this allocator node")
        if override:
            print(f"  local  {override['state']} ({override['reason']})")
    return 0


def cmd_allocator_node_override(args: argparse.Namespace) -> int:
    """Persist a local admission fence that outranks global placement."""

    cfg = config.select_grid(getattr(args, "grid", None))
    state_path = _node_state_path(_scope_for_grid(cfg, allow_offline=True))
    duration = getattr(args, "for_seconds", None)
    if duration is not None and (not math.isfinite(duration) or duration <= 0):
        raise SystemExit("--for-seconds must be positive")
    expires_at = time.time() + duration if duration is not None else None
    reason = str(getattr(args, "reason", "") or f"local {args.override_state}")[:500]
    constructors = {
        "drain": LocalOverride.drain,
        "pause": LocalOverride.pause,
        "quarantine": LocalOverride.quarantine,
    }
    override = constructors[args.override_state](reason)
    if expires_at is not None:
        override = LocalOverride(override.state, override.reason, expires_at)
    path = write_local_override(state_path, override)
    expiry = f" until {expires_at:.3f}" if expires_at is not None else ""
    print(
        f"Allocator node local override: {override.state.value}{expiry}. "
        f"It will apply by the next heartbeat ({path})."
    )
    return 0


def cmd_allocator_node_resume(args: argparse.Namespace) -> int:
    """Return the node to telemetry-driven local policy."""

    cfg = config.select_grid(getattr(args, "grid", None))
    state_path = _node_state_path(_scope_for_grid(cfg, allow_offline=True))
    clear_local_override(state_path)
    print("Allocator node local override cleared; normal policy resumes by the next heartbeat.")
    return 0


def _read_local_override_status(state_path: Path) -> dict[str, Any] | None:
    path = local_override_path(state_path)
    if not path.exists():
        return None
    try:
        override = LocalOverride.from_dict(jsonio.load_json(path))
    except (KeyError, OSError, OverflowError, SystemExit, TypeError, ValueError) as exc:
        return {
            "state": "quarantined",
            "reason": "invalid_local_override_file",
            "expires_at": None,
            "error": str(exc),
        }
    return {
        "state": override.state.value,
        "reason": override.reason,
        "expires_at": override.expires_at,
    }


def _request(
    cfg: dict[str, Any],
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    token: str = "",
    allow_insecure_http: bool = False,
) -> dict[str, Any]:
    headers = {"X-Grid-Allocator-Token": token} if token else {}
    control_url = runtime.allocator_control_url(cfg)
    if token and not allow_insecure_http and not secure_control_transport(control_url):
        raise SystemExit(
            "Refusing to send an allocator credential over non-loopback HTTP. "
            "Use HTTPS or pass --allow-insecure-http for a trusted LAN."
        )
    try:
        with httpx.Client(
            timeout=15.0,
            trust_env=control_url.lower().startswith("https://"),
        ) as client:
            response = client.request(
                method,
                f"{control_url}{path}",
                json=body,
                headers=headers,
            )
    except httpx.RequestError as exc:
        raise SystemExit(f"Could not reach allocator on {cfg['name']}: {exc}") from exc
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except (ValueError, AttributeError):
            detail = response.text
        raise SystemExit(f"Allocator request failed ({response.status_code}): {detail}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise SystemExit("Allocator returned a non-object response")
    return payload


def _request_remote_enrollment(relay_url: str, access_token: str, label: str) -> str:
    """Exchange provider membership for a host credential after registration becomes visible.

    ``grid join`` deliberately returns once its detached provider has started, while the provider
    registers with the relay asynchronously. A directly following ``grid allocator join`` can
    therefore observe a short, honest 409. Retry only that exact readiness race; authentication,
    policy, malformed responses, and every other error remain immediate failures.
    """

    deadline = time.monotonic() + REMOTE_ENROLLMENT_READY_TIMEOUT_SECONDS
    while True:
        try:
            with httpx.Client(timeout=15.0, trust_env=True) as client:
                response = client.post(
                    f"{relay_url}/allocator/enroll",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
        except httpx.RequestError as exc:
            raise SystemExit(f"Could not enroll this machine with allocator on {label}: {exc}") from exc
        try:
            detail = response.json().get("detail")
        except (ValueError, AttributeError):
            detail = "relay rejected allocator enrollment"
        provider_not_visible = (
            response.status_code == 409
            and isinstance(detail, str)
            and "must join the grid as a provider" in detail.lower()
        )
        remaining = deadline - time.monotonic()
        if provider_not_visible and remaining > 0:
            time.sleep(min(REMOTE_ENROLLMENT_RETRY_SECONDS, remaining))
            continue
        break
    if response.status_code >= 400:
        raise SystemExit(f"Allocator enrollment failed ({response.status_code}): {detail}")
    try:
        payload = response.json()
        token = str(payload["node_token"])
        host_id = str(payload["host_id"])
        credential = decode_node_token(token)
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemExit("Allocator enrollment returned an invalid node credential") from exc
    if credential.host_id != host_id:
        raise SystemExit("Allocator enrollment returned a mismatched node identity")
    return token


def _control_token(cfg: dict[str, Any], token_file: str | None) -> str:
    token = ""
    if token_file:
        try:
            token = Path(token_file).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SystemExit(f"Cannot read allocator token file: {exc}") from exc
    token = token or os.environ.get(OPERATOR_TOKEN_ENV, "").strip()
    if not token and cfg.get("managed_server", True):
        token = runtime.ensure_allocator_control_token(cfg)
    if not token:
        raise SystemExit(
            f"Allocator control token required. Set {TOKEN_ENV} or pass --token-file."
        )
    return token


def _node_token(
    cfg: dict[str, Any],
    token_file: str | None,
    state_path: Path,
    *,
    supplied_token: str = "",
    allow_stopped_host_rebind: bool = False,
) -> tuple[str, str]:
    """Resolve a host credential and bind it to this runtime's durable host id."""

    token = supplied_token.strip()
    if token_file:
        try:
            token = Path(token_file).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SystemExit(f"Cannot read allocator node credential file: {exc}") from exc
    token = token or os.environ.get(NODE_TOKEN_ENV, "").strip()
    persisted = jsonio.load_json(state_path)
    persisted_host = str(persisted.get("host_id") or "")
    if not token and cfg.get("managed_server", True):
        host_id = persisted_host or f"host-{uuid.uuid4().hex[:16]}"
        token = mint_node_token(
            runtime.ensure_allocator_control_token(cfg),
            host_id,
            ttl_seconds=DEFAULT_NODE_TOKEN_TTL_SECONDS,
        )
    if not token:
        raise SystemExit(
            f"Allocator node credential required. Set {NODE_TOKEN_ENV} or pass --token-file. "
            "Mint one with `grid allocator token write`."
        )
    try:
        credential = decode_node_token(token)
    except (TypeError, ValueError) as exc:
        raise SystemExit(
            "Invalid allocator node credential; mint a host-scoped credential with "
            "`grid allocator token write`."
        ) from exc
    raw_residencies = persisted.get("residencies") or []
    raw_receipts = persisted.get("receipts") or []
    rebind_state_inert = (
        isinstance(raw_residencies, list)
        and all(
            isinstance(row, dict) and row.get("handle") is None
            for row in raw_residencies
        )
        and isinstance(raw_receipts, list)
        and all(
            not isinstance(row, dict) or row.get("status") != "running"
            for row in raw_receipts
        )
    )
    if (
        persisted_host
        and persisted_host != credential.host_id
        and not (allow_stopped_host_rebind and rebind_state_inert)
    ):
        raise SystemExit(
            f"Allocator credential is for {credential.host_id}, but this node is {persisted_host}."
        )
    return token, credential.host_id


def _scope(grid_url: str) -> str:
    return stable_digest(grid_url.rstrip("/"))[:16]


def _validated_grid_id(value: Any) -> str:
    grid_id = str(value or "").strip()
    if not grid_id or len(grid_id) > 512:
        raise SystemExit("Grid returned an invalid grid_id; refusing to start an allocator node.")
    return grid_id


def _scope_for_grid(cfg: dict[str, Any], *, allow_offline: bool) -> str:
    """Canonical per-grid scope, stable across equivalent signaling URLs."""

    if cfg.get("managed_server", True):
        return _scope(_validated_grid_id(cfg.get("grid_id")))
    try:
        info = _request(cfg, "GET", "/grid/info")
    except SystemExit:
        if not allow_offline:
            raise
        found = _find_record_scope(cfg)
        if found is not None:
            return found
        # No daemon record exists to recover. This fallback preserves deterministic status and
        # override paths for a currently unreachable URL without claiming two URLs are equivalent.
        return _scope(_validated_grid_id(cfg.get("grid_id")))
    return _scope(_validated_grid_id(info.get("grid_id")))


def _find_record_scope(cfg: dict[str, Any]) -> str | None:
    root = paths.run_dir() / "allocator"
    if not root.exists():
        return None
    wanted_url = runtime.grid_url(cfg).rstrip("/")
    matches: list[str] = []
    for candidate in sorted(root.glob("*.json")):
        try:
            record = jsonio.load_json(candidate)
        except SystemExit:
            continue
        if str(record.get("grid_url") or "").rstrip("/") == wanted_url:
            matches.append(candidate.stem)
    return matches[0] if len(matches) == 1 else None


def _node_record_path(scope: str) -> Path:
    return paths.run_dir() / "allocator" / f"{scope}.json"


def _node_state_path(scope: str) -> Path:
    return paths.grid_home() / "allocator" / scope / "state.json"


def _node_log_path(scope: str) -> Path:
    return paths.logs_dir() / f"allocator_node_{scope}.log"


def _node_startup_path(scope: str) -> Path:
    return paths.run_dir() / "allocator" / f"{scope}.ready.json"


def _record_pid(record: dict[str, Any]) -> int:
    try:
        pid = int(record.get("pid") or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, pid)


def _stop_persisted_allocator_children(
    state_path: Path,
    *,
    registry_ttl_seconds: float = NODE_REGISTRY_TTL_FALLBACK_SECONDS,
) -> bool:
    """After route expiry, adopt and stop proven children left by a dead daemon."""

    if not state_path.exists():
        return False
    if not math.isfinite(registry_ttl_seconds) or registry_ttl_seconds < 0:
        raise ValueError("registry_ttl_seconds must be non-negative")
    persisted = jsonio.load_json(state_path)
    has_handles = any(
        isinstance(row, dict) and isinstance(row.get("handle"), dict)
        for row in persisted.get("residencies") or ()
    )
    if not has_handles:
        return False
    # No live owned daemon remains to publish DRAINING or delete its server records. Its final
    # successful heartbeat could have landed immediately before death, so keep every proven child
    # serving for one complete authoritative lease from this observation before adopting/stopping.
    expiry_deadline = time.monotonic() + registry_ttl_seconds
    while True:
        remaining = expiry_deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.25, remaining))
    backend = _backend_from_persisted_state(persisted)
    managed = ManagedModelRuntime(state_path, backend=backend)
    managed.stop_all(wait_timeout=NODE_SHUTDOWN_DRAIN_SECONDS)
    return True


def _backend_from_persisted_state(persisted: dict[str, Any]) -> LlamaCppBackend:
    raw = persisted.get("backend_config")
    if isinstance(raw, dict) and raw.get("bind_host"):
        return LlamaCppBackend(
            bind_host=str(raw["bind_host"]),
            endpoint_host=str(raw.get("endpoint_host") or "") or None,
            tls_cert_file=str(raw.get("tls_cert_file") or "") or None,
            tls_key_file=str(raw.get("tls_key_file") or "") or None,
            tls_ca_file=str(raw.get("tls_ca_file") or "") or None,
            tls_ca_pem=str(raw.get("tls_ca_pem") or "") or None,
            allow_missing_transport_files=True,
        )
    bind_host = _derive_persisted_bind_host(persisted)
    endpoint_host = "::1" if bind_host == "::" else "127.0.0.1"
    return LlamaCppBackend(bind_host=bind_host, endpoint_host=endpoint_host)


def _derive_persisted_bind_host(persisted: dict[str, Any]) -> str:
    """Recover an old state's listener family from its live, later-proven child argv."""

    found: set[str] = set()
    for row in persisted.get("residencies") or ():
        handle = row.get("handle") if isinstance(row, dict) else None
        if not isinstance(handle, dict):
            continue
        try:
            pid = int(handle.get("pid") or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        argv = run_records.process_command_args(pid)
        if not argv:
            continue
        values = tuple(
            argv[index + 1]
            for index, argument in enumerate(argv[:-1])
            if argument == "--host"
        )
        if len(values) != 1:
            continue
        try:
            version = ipaddress.ip_address(values[0]).version
        except ValueError:
            continue
        found.add("::" if version == 6 else "0.0.0.0")
    if len(found) > 1:
        raise RuntimeError("persisted allocator children disagree on listener family")
    return next(iter(found), "0.0.0.0")


def _validated_engine_tls_files(
    cert_value: str | None,
    key_value: str | None,
    ca_value: str | None,
) -> tuple[str, str, str]:
    if bool(cert_value) != bool(key_value):
        raise SystemExit("--engine-tls-cert and --engine-tls-key must be provided together.")
    if ca_value and not cert_value:
        raise SystemExit("--engine-tls-ca requires --engine-tls-cert and --engine-tls-key.")
    cert = _readable_path(cert_value, "engine TLS certificate")
    key = _readable_path(key_value, "engine TLS private key")
    ca = _readable_path(ca_value, "engine TLS CA")
    if key and os.name != "nt":
        stat_result = Path(key).stat()
        if stat_result.st_mode & 0o077:
            raise SystemExit(
                f"Engine TLS private key must be owner-only (chmod 600): {key}"
            )
        if hasattr(os, "geteuid") and stat_result.st_uid != os.geteuid():
            raise SystemExit(f"Engine TLS private key must be owned by the current user: {key}")
    return cert, key, ca


def _readable_path(value: str | None, label: str) -> str:
    if not value:
        return ""
    path = Path(value).expanduser().resolve()
    if not path.is_file() or not os.access(path, os.R_OK):
        raise SystemExit(f"{label} is not a readable regular file: {path}")
    return str(path)


def _literal_loopback_host(value: str) -> bool:
    host = str(value).strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    host = host.replace("%25", "%").split("%", 1)[0]
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _literal_loopback_url_host(value: str) -> str:
    """Return a URL's literal loopback host, or an empty string for every other host."""

    try:
        host = urlsplit(str(value)).hostname or ""
    except ValueError:
        return ""
    return host if _literal_loopback_host(host) else ""


def _node_process_state(record: dict[str, Any]) -> str:
    """Return dead, owned, foreign (PID reuse), or ambiguous (fail closed)."""

    pid = _record_pid(record)
    if not pid or not run_records.pid_alive(pid):
        return "dead"
    instance_id = str(record.get("instance_id") or "")
    if not instance_id:
        return "ambiguous"
    argv = run_records.process_command_args(pid)
    if argv is None:
        return "ambiguous"
    if "__allocator-node" not in argv or instance_id not in argv:
        return "foreign"
    recorded_marker = str(record.get("process_start_marker") or "")
    if not recorded_marker:
        return "ambiguous"
    current_marker = run_records.process_start_marker(pid)
    if current_marker is None:
        return "ambiguous"
    if current_marker != recorded_marker:
        return "foreign"
    return "owned"


def _await_process_start_marker(pid: int, timeout: float = 1.0) -> str | None:
    deadline = time.monotonic() + timeout
    while True:
        marker = run_records.process_start_marker(pid)
        if marker is not None:
            return marker
        if time.monotonic() >= deadline or not run_records.pid_alive(pid):
            return None
        time.sleep(0.02)


def _terminate_spawned_process(
    process: subprocess.Popen,
    *,
    cooperative_timeout: float = NODE_STARTUP_CLEANUP_GRACE_SECONDS,
) -> None:
    """Bound graceful cleanup, then contain the exact detached process group/tree.

    The caller has already written an instance-scoped shutdown request. The Popen object is the
    authoritative identity and was created with ``start_new_session=True``, so its positive PID is
    also the POSIX process-group id; on Windows ``kill_group`` uses ``taskkill /T``.
    """

    if cooperative_timeout < 0:
        raise ValueError("cooperative timeout must be non-negative")
    already_exited = process.poll() is not None
    if not already_exited:
        try:
            process.wait(timeout=cooperative_timeout)
        except (AttributeError, OSError, subprocess.SubprocessError):
            pass
    # Even a daemon that exits during the cooperative wait can die before its own child cleanup.
    # The detached process group is exact ownership evidence from this Popen, so contain it in all
    # exit states. kill_group tolerates an already-empty group.
    try:
        run_records.kill_group(process.pid)
    except (AttributeError, OSError, subprocess.SubprocessError):
        pass
    try:
        process.wait(timeout=5.0)
    except (AttributeError, OSError, subprocess.SubprocessError):
        pass
    if process.poll() is None:
        raise RuntimeError(
            f"allocator node process tree {process.pid} survived startup cleanup; "
            "preserving its run record for manual recovery"
        )


def _await_node_start(
    process: subprocess.Popen,
    startup_path: Path,
    instance_id: str,
    log_path: Path,
    timeout: float = NODE_STARTUP_TIMEOUT_SECONDS,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit(
                f"Allocator node exited during startup. See {log_path}:\n{_log_tail(log_path)}"
            )
        if startup_path.exists():
            marker = jsonio.load_json(startup_path)
            if (
                marker.get("instance_id") == instance_id
                and int(marker.get("pid") or 0) == process.pid
                and marker.get("host_id")
                and marker.get("registered_at")
            ):
                return
        time.sleep(0.1)
    if process.poll() is not None:
        raise SystemExit(f"Allocator node failed to start. See {log_path}")
    raise SystemExit(f"Allocator node did not initialize within {timeout:g}s. See {log_path}")


def _log_tail(path: Path, lines: int = 20) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(log unavailable)"
    return "\n".join(text.splitlines()[-lines:])
