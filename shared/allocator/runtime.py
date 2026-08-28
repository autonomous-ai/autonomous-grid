"""Managed model lifecycle for an allocator-controlled Grid host.

The global controller decides *where* a model should run.  This module is the deliberately small
actuator that makes one host converge on those commands.  It persists every transition before and
after side effects, executes at most one mutation at a time, and treats a local host-protection
decision as a higher-priority admission gate.

The first runtime backend is llama.cpp.  ``load`` currently means "verify that the GGUF is already
in Grid's model store"; downloading is intentionally not inferred from a model name.  That keeps
automatic mode from fetching mutable or unverified artifacts behind an administrator's back.
"""

from __future__ import annotations

import ipaddress
import math
import os
import secrets
import shlex
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from shared import jsonio, paths
from shared.allocator.local import (
    AdmissionDecision,
    LocalAllocatorState,
    LocalHostProtectionLoop,
    LocalOverride,
)
from shared.allocator.models import (
    SCHEMA_VERSION,
    ActionKind,
    ModelResidency,
    MutationAction,
    NodeState,
    ResidencyState,
)
from shared.allocator.reconcile import MutationStatus
from shared.models import store as model_store
from shared.run_records import pid_alive, stopped_running
from shared.system.hostsignals import HostSignalCollector

RUNTIME_SCHEMA_VERSION = 1
DEFAULT_PORT_START = 18_081
DEFAULT_PORT_END = 18_180
MAX_RECEIPTS = 256
MAX_HEALTH_PROBE_WORKERS = 16
DEFAULT_HEALTH_PROBE_WORKERS = 8
RECOVERY_HEALTH_DEADLINE_SECONDS = 30.0
MAX_MODEL_AGE_SECONDS = 10 * 365 * 24 * 60 * 60
LOCAL_OVERRIDE_SUFFIX = ".override.json"
SHUTDOWN_REQUEST_SUFFIX = ".shutdown"
ENGINE_API_KEY_SUFFIX = ".engine-api-key"


def local_override_path(state_path: Path) -> Path:
    """Return the durable local-operator override path paired with a runtime state file."""

    path = Path(state_path)
    if path.suffix:
        return path.with_suffix(LOCAL_OVERRIDE_SUFFIX)
    return path.with_name(f"{path.name}{LOCAL_OVERRIDE_SUFFIX}")


def shutdown_request_path(state_path: Path) -> Path:
    """Return the cross-platform local graceful-shutdown request path for a runtime."""

    path = Path(state_path)
    if path.suffix:
        return path.with_suffix(SHUTDOWN_REQUEST_SUFFIX)
    return path.with_name(f"{path.name}{SHUTDOWN_REQUEST_SUFFIX}")


def engine_api_key_path(state_path: Path) -> Path:
    """Return the owner-only llama.cpp key file paired with a runtime state file."""

    path = Path(state_path)
    if path.suffix:
        return path.with_suffix(ENGINE_API_KEY_SUFFIX)
    return path.with_name(f"{path.name}{ENGINE_API_KEY_SUFFIX}")


def write_local_override(state_path: Path, override: LocalOverride) -> Path:
    """Atomically install a validated local override and return its concrete path."""

    if not isinstance(override, LocalOverride):
        raise TypeError("override must be a LocalOverride")
    path = local_override_path(state_path)
    jsonio.atomic_write_json(path, override.to_dict(), mode=0o600)
    return path


def clear_local_override(state_path: Path) -> Path:
    """Remove the local override (resume normal policy); absence is intentionally idempotent."""

    path = local_override_path(state_path)
    path.unlink(missing_ok=True)
    return path


@dataclass(frozen=True, slots=True)
class RuntimeHandle:
    pid: int
    port: int
    process_birth_marker: str = ""
    executable_path: str = ""
    model_path: str = ""

    def __post_init__(self) -> None:
        if self.pid <= 0 or not 0 < self.port < 65_536:
            raise ValueError("runtime handle requires a positive pid and valid port")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RuntimeHandle:
        return cls(
            pid=int(value["pid"]),
            port=int(value["port"]),
            process_birth_marker=str(value.get("process_birth_marker") or ""),
            executable_path=str(value.get("executable_path") or ""),
            model_path=str(value.get("model_path") or ""),
        )


@dataclass(frozen=True, slots=True)
class _ProcessHealth:
    alive: bool | None
    owned: bool
    ready: bool
    started: bool = True


@dataclass(frozen=True, slots=True)
class ManagedResidency:
    model_id: str
    memory_mb: int
    state: ResidencyState
    loaded_at: float = 0.0
    last_used_at: float = 0.0
    load_failures: int = 0
    pinned: bool = False
    handle: RuntimeHandle | None = None

    def __post_init__(self) -> None:
        if not self.model_id or self.memory_mb <= 0:
            raise ValueError(
                "managed residency requires a model id and positive memory"
            )
        if not isinstance(self.state, ResidencyState):
            object.__setattr__(self, "state", ResidencyState(self.state))
        if self.loaded_at < 0 or self.last_used_at < 0 or self.load_failures < 0:
            raise ValueError(
                "managed residency counters and timestamps must be non-negative"
            )

    def to_model_residency(self) -> ModelResidency:
        return ModelResidency(
            model_id=self.model_id,
            memory_mb=self.memory_mb,
            state=self.state,
            loaded_at=self.loaded_at,
            last_used_at=self.last_used_at,
            load_failures=self.load_failures,
            pinned=self.pinned,
            managed=True,
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["state"] = self.state.value
        return data

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ManagedResidency:
        raw_handle = value.get("handle")
        return cls(
            model_id=str(value["model_id"]),
            memory_mb=int(value["memory_mb"]),
            state=ResidencyState(value["state"]),
            loaded_at=float(value.get("loaded_at") or 0.0),
            last_used_at=float(value.get("last_used_at") or 0.0),
            load_failures=int(value.get("load_failures") or 0),
            pinned=bool(value.get("pinned", False)),
            handle=(
                RuntimeHandle.from_dict(raw_handle)
                if isinstance(raw_handle, Mapping)
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class ActionReceipt:
    action_id: str
    status: MutationStatus
    message: str
    plan_generation: str
    updated_at: float
    reported_status: MutationStatus | None = None
    sequence: int = 0

    def __post_init__(self) -> None:
        if not self.action_id or not self.plan_generation:
            raise ValueError("action receipt identity and generation are required")
        if not isinstance(self.status, MutationStatus):
            object.__setattr__(self, "status", MutationStatus(self.status))
        if self.reported_status is not None and not isinstance(
            self.reported_status, MutationStatus
        ):
            object.__setattr__(
                self, "reported_status", MutationStatus(self.reported_status)
            )
        if self.updated_at < 0:
            raise ValueError("receipt time must be non-negative")
        if self.sequence < 0:
            raise ValueError("receipt sequence must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "status": self.status.value,
            "message": self.message,
            "plan_generation": self.plan_generation,
            "updated_at": self.updated_at,
            "reported_status": self.reported_status.value
            if self.reported_status
            else None,
            "sequence": self.sequence,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ActionReceipt:
        reported = value.get("reported_status")
        return cls(
            action_id=str(value["action_id"]),
            status=MutationStatus(value["status"]),
            message=str(value.get("message") or ""),
            plan_generation=str(value["plan_generation"]),
            updated_at=float(value.get("updated_at") or 0.0),
            reported_status=MutationStatus(reported) if reported else None,
            sequence=int(value.get("sequence") or 0),
        )


class ModelRuntimeBackend(Protocol):
    """Side-effect boundary used by the node state machine and its tests."""

    def cached_models(self) -> tuple[str, ...]: ...

    def start(self, model_id: str, port: int) -> RuntimeHandle: ...

    def start_with_callback(
        self,
        model_id: str,
        port: int,
        on_spawn: Callable[[RuntimeHandle], None],
    ) -> RuntimeHandle: ...

    def alive(self, handle: RuntimeHandle) -> bool: ...

    def owns(self, handle: RuntimeHandle, model_id: str) -> bool: ...

    def ready(self, handle: RuntimeHandle, model_id: str) -> bool: ...

    def stop(self, handle: RuntimeHandle, model_id: str) -> None: ...

    def active_requests(self, handle: RuntimeHandle, model_id: str) -> int | None: ...


class LlamaCppBackend:
    """llama.cpp lifecycle backed by Grid's existing launcher and model store."""

    def __init__(
        self,
        *,
        readiness_timeout: float = 120.0,
        bind_host: str = "0.0.0.0",
        api_key: str | None = None,
        endpoint_host: str | None = None,
        tls_cert_file: str | os.PathLike[str] | None = None,
        tls_key_file: str | os.PathLike[str] | None = None,
        tls_ca_file: str | os.PathLike[str] | None = None,
        tls_ca_pem: str | None = None,
        allow_missing_transport_files: bool = False,
    ) -> None:
        if readiness_timeout <= 0:
            raise ValueError("readiness_timeout must be positive")
        try:
            normalized_bind_host = str(ipaddress.ip_address(str(bind_host).strip()))
        except ValueError as exc:
            raise ValueError("bind_host must be a valid IPv4 or IPv6 address") from exc
        self.readiness_timeout = readiness_timeout
        self.bind_host = normalized_bind_host
        ipv6_bind = ipaddress.ip_address(normalized_bind_host).version == 6
        self.loopback_address = "::1" if ipv6_bind else "127.0.0.1"
        self.loopback_host = "[::1]" if ipv6_bind else "127.0.0.1"
        self.endpoint_host = _validated_endpoint_host(
            endpoint_host or self.loopback_address
        )
        self.tls_cert_file = _validated_runtime_file(
            tls_cert_file,
            "TLS certificate",
            allow_missing=allow_missing_transport_files,
        )
        self.tls_key_file = _validated_runtime_file(
            tls_key_file,
            "TLS private key",
            owner_only=True,
            allow_missing=allow_missing_transport_files,
        )
        self.tls_ca_file = _validated_runtime_file(
            tls_ca_file,
            "TLS CA file",
            allow_missing=allow_missing_transport_files,
        )
        if bool(self.tls_cert_file) != bool(self.tls_key_file):
            raise ValueError("TLS certificate and private key must be configured together")
        if (self.tls_ca_file or tls_ca_pem) and not self.tls_cert_file:
            raise ValueError("TLS CA file requires TLS certificate and private key")
        self.tls_ca_pem = str(tls_ca_pem or "")
        if self.tls_ca_pem:
            _validate_ca_pem(self.tls_ca_pem)
        elif self.tls_ca_file and Path(self.tls_ca_file).is_file():
            try:
                pem = Path(self.tls_ca_file).read_text(encoding="ascii")
            except (OSError, UnicodeError) as exc:
                raise ValueError(f"could not read TLS CA file: {exc}") from exc
            _validate_ca_pem(pem)
            self.tls_ca_pem = pem
        self.endpoint_scheme = "https" if self.tls_cert_file else "http"
        self._api_key = _validated_api_key(api_key or secrets.token_urlsafe(32))
        self._api_key_file = ""
        self._spawned: dict[int, tuple[Any, str, RuntimeHandle]] = {}
        self._starting: set[int] = set()
        self._cancelled = False
        self._lock = threading.Lock()

    def configure_api_key(self, api_key: str) -> None:
        """Install the runtime's durable key before adopting or starting any process."""

        validated = _validated_api_key(api_key)
        with self._lock:
            if self._spawned or self._starting:
                raise RuntimeError(
                    "cannot change the engine API key while a process is managed"
                )
            self._api_key = validated

    def configure_api_key_file(self, path: str | os.PathLike[str]) -> None:
        """Point llama.cpp at the protected key file without putting the key in argv."""

        validated = _validated_runtime_file(
            path,
            "engine API key file",
            owner_only=True,
        )
        with self._lock:
            if self._spawned or self._starting:
                raise RuntimeError(
                    "cannot change the engine API key file while a process is managed"
                )
            self._api_key_file = validated

    def cached_models(self) -> tuple[str, ...]:
        return tuple(
            item.name
            for item in model_store.list_all()
            if item.path.suffix.lower() == ".gguf"
        )

    def start(self, model_id: str, port: int) -> RuntimeHandle:
        return self._start(model_id, port, on_spawn=None)

    def start_with_callback(
        self,
        model_id: str,
        port: int,
        on_spawn: Callable[[RuntimeHandle], None],
    ) -> RuntimeHandle:
        """Spawn and durably publish provisional, then OS-enriched, ownership."""

        return self._start(model_id, port, on_spawn=on_spawn)

    def _start(
        self,
        model_id: str,
        port: int,
        *,
        on_spawn: Callable[[RuntimeHandle], None] | None,
    ) -> RuntimeHandle:
        if model_id not in self.cached_models():
            raise RuntimeError(
                f"model weights are not cached: {model_id!r}; run `grid pull {model_id}` first"
            )
        from shared.engine import launcher

        with self._lock:
            if self._cancelled:
                raise RuntimeError("allocator runtime is shutting down")
        if launcher.is_port_in_use(port, host=self.loopback_address):
            raise RuntimeError(f"allocator runtime port {port} is already in use")
        launcher.assert_supported_build()
        if not self._api_key_file:
            raise RuntimeError("allocator engine API key file is not configured")

        def publish_provisional(launched: Any) -> None:
            """Record the Popen identity before launcher.start_llm can return to this frame."""

            provisional = RuntimeHandle(launched.proc.pid, port)
            with self._lock:
                self._spawned[launched.proc.pid] = (launched, model_id, provisional)
                self._starting.add(launched.proc.pid)
                cancelled = self._cancelled
            if cancelled:
                with self._lock:
                    self._starting.discard(launched.proc.pid)
                    self._spawned.pop(launched.proc.pid, None)
                raise RuntimeError("allocator runtime shut down during model startup")
            try:
                if on_spawn is not None:
                    # The managed runtime atomically writes this weak PID/port handle. A restart
                    # cannot prove ownership from it and therefore fails closed, but it can never
                    # lose all knowledge that a child was created.
                    on_spawn(provisional)
            except BaseException:
                with self._lock:
                    self._starting.discard(launched.proc.pid)
                    self._spawned.pop(launched.proc.pid, None)
                raise

        launched = launcher.start_llm(
            model_id,
            port=port,
            host=self.bind_host,
            alias=model_id,
            api_key_file=self._api_key_file,
            tls_cert_file=self.tls_cert_file or None,
            tls_key_file=self.tls_key_file or None,
            tls_ca_file=self.tls_ca_file or None,
            probe_host=self.endpoint_host,
            on_spawn=publish_provisional,
        )
        handle = RuntimeHandle(
            launched.proc.pid,
            port,
            process_birth_marker=_process_birth_marker(launched.proc.pid),
            executable_path=_popen_executable(launched.proc),
            # Match launcher.start_llm's canonicalization exactly. Persisting this argv operand is
            # what lets a restarted allocator prove ownership even after the GGUF is deleted.
            model_path=str(paths.models_dir() / Path(model_id).name),
        )
        with self._lock:
            self._spawned[launched.proc.pid] = (launched, model_id, handle)
            cancelled = self._cancelled
        if cancelled:
            launcher.stop(launched)
            with self._lock:
                self._starting.discard(launched.proc.pid)
                self._spawned.pop(launched.proc.pid, None)
            raise RuntimeError("allocator runtime shut down during model startup")
        try:
            if on_spawn is not None:
                # Replace the provisional record with the PID-reuse and executable identity
                # needed to adopt or stop the child safely after a daemon restart.
                on_spawn(handle)
            launcher.wait_for_models(
                launched,
                timeout=self.readiness_timeout,
                api_key=self._api_key,
            )
        except (Exception, SystemExit):
            launcher.stop(launched)
            with self._lock:
                self._starting.discard(launched.proc.pid)
                self._spawned.pop(launched.proc.pid, None)
            raise
        with self._lock:
            self._starting.discard(launched.proc.pid)
        return handle

    def cancel_pending(self) -> None:
        """Fence new starts and terminate children still inside their readiness probe."""

        from shared.engine import launcher

        with self._lock:
            self._cancelled = True
            starting = [
                self._spawned[pid][0] for pid in self._starting if pid in self._spawned
            ]
        for launched in starting:
            launcher.stop(launched)

    def alive(self, handle: RuntimeHandle) -> bool:
        with self._lock:
            spawned = self._spawned.get(handle.pid)
        if spawned is not None:
            return spawned[0].proc.poll() is None
        return _pid_alive(handle.pid)

    def owns(self, handle: RuntimeHandle, model_id: str) -> bool:
        with self._lock:
            spawned = self._spawned.get(handle.pid)
        if spawned is not None:
            launched, spawned_model, spawned_handle = spawned
            return (
                launched.proc.poll() is None
                and launched.port == handle.port
                and spawned_model == model_id
                and (
                    spawned_handle == handle
                    # While the enriched marker is being durably written, another heartbeat may
                    # still hold the provisional handle. The live Popen object is authoritative
                    # inside this process; after restart, the weak handle still fails closed.
                    or (
                        spawned_handle.pid == handle.pid
                        and spawned_handle.port == handle.port
                        and not handle.process_birth_marker
                    )
                )
            )
        if not handle.process_birth_marker:
            return False
        if _process_birth_marker(handle.pid) != handle.process_birth_marker:
            return False
        argv = _process_argv(handle.pid)
        if not argv:
            return False
        return self._argv_matches(
            argv,
            model_id,
            handle.port,
            executable_path=handle.executable_path,
            model_path=handle.model_path,
        )

    def _argv_matches(
        self,
        argv: tuple[str, ...],
        model_id: str,
        port: int,
        *,
        executable_path: str,
        model_path: str = "",
    ) -> bool:
        if not argv or not executable_path:
            return False
        if Path(argv[0]).resolve() != Path(executable_path).resolve():
            return False
        # New handles persist the exact path passed to llama-server. That ownership proof remains
        # usable after an operator deletes or moves the GGUF, which is precisely when cleanup must
        # still be able to stop the old process. Older state files fall back to the live model
        # store lookup for backwards compatibility.
        expected_model = (
            Path(model_path) if model_path else _cached_model_path(model_id)
        )
        if expected_model is None:
            return False
        model_values = _option_values(argv, "-m")
        alias_values = _option_values(argv, "--alias")
        port_values = _option_values(argv, "--port")
        return (
            len(model_values) == 1
            and Path(model_values[0]).resolve() == expected_model.resolve()
            and alias_values == (model_id,)
            and port_values == (str(port),)
        )

    def ready(self, handle: RuntimeHandle, model_id: str) -> bool:
        if not self.owns(handle, model_id):
            return False
        if not self._secure_launch_configuration(handle):
            return False
        try:
            with httpx.Client(
                timeout=2.0,
                trust_env=False,
                verify=self._tls_verify(),
            ) as client:
                response = client.get(
                    f"{self.endpoint_scheme}://{_url_host(self.endpoint_host)}:"
                    f"{handle.port}/v1/models",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
        except httpx.RequestError:
            return False
        return response.status_code == 200

    def active_requests(self, handle: RuntimeHandle, model_id: str) -> int | None:
        """Return llama.cpp's direct in-flight slot count, or ``None`` when unknowable."""

        if not self.alive(handle):
            return 0
        if not self.owns(handle, model_id):
            return None
        if not self._secure_launch_configuration(handle):
            return None
        try:
            with httpx.Client(
                timeout=1.0,
                trust_env=False,
                verify=self._tls_verify(),
            ) as client:
                response = client.get(
                    f"{self.endpoint_scheme}://{_url_host(self.endpoint_host)}:"
                    f"{handle.port}/slots",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
            if response.status_code != 200:
                return None
            payload = response.json()
        except (httpx.HTTPError, OSError, TypeError, ValueError):
            return None
        if not isinstance(payload, list):
            return None
        active = 0
        for slot in payload:
            if not isinstance(slot, Mapping) or not isinstance(
                slot.get("is_processing"), bool
            ):
                return None
            active += int(slot["is_processing"] is True)
        return active

    def _tls_verify(self) -> ssl.SSLContext | bool:
        if not self.tls_ca_pem:
            return True
        context = ssl.create_default_context()
        context.load_verify_locations(cadata=self.tls_ca_pem)
        return context

    def _secure_launch_configuration(self, handle: RuntimeHandle) -> bool:
        """Reject adoption of pre-hardening children that exposed or omitted the key."""

        with self._lock:
            if handle.pid in self._spawned:
                return True
        argv = _process_argv(handle.pid)
        if not argv or not self._api_key_file:
            return False
        key_files = _option_values(argv, "--api-key-file")
        if len(key_files) != 1 or Path(key_files[0]).resolve() != Path(
            self._api_key_file
        ).resolve():
            return False
        if _option_values(argv, "--api-key"):
            return False
        cert_files = _option_values(argv, "--ssl-cert-file")
        key_files_tls = _option_values(argv, "--ssl-key-file")
        if self.endpoint_scheme == "https":
            return (
                len(cert_files) == 1
                and len(key_files_tls) == 1
                and Path(cert_files[0]).resolve()
                == Path(self.tls_cert_file).resolve()
                and Path(key_files_tls[0]).resolve()
                == Path(self.tls_key_file).resolve()
            )
        return not cert_files and not key_files_tls

    def stop(self, handle: RuntimeHandle, model_id: str) -> None:
        self.stop_with_timeout(handle, model_id, timeout=15.0)

    def stop_with_timeout(
        self,
        handle: RuntimeHandle,
        model_id: str,
        *,
        timeout: float,
    ) -> None:
        if not self.owns(handle, model_id):
            if _pid_alive(handle.pid):
                raise RuntimeError(
                    "refusing to stop a process the allocator cannot prove it owns"
                )
            return
        with self._lock:
            spawned = self._spawned.pop(handle.pid, None)
        if spawned is not None:
            from shared.engine import launcher

            graceful = max(0.0, timeout * 2.0 / 3.0)
            launcher.stop(
                spawned[0],
                timeout=graceful,
                kill_timeout=max(0.0, timeout - graceful),
            )
            return
        _terminate_owned_pid(
            handle.pid,
            timeout=max(0.0, timeout),
            identity_check=lambda: self.owns(handle, model_id),
        )


class ManagedModelRuntime:
    """Persistent, idempotent actuator for one physical host.

    ``begin`` is non-blocking.  Slow model start/stop work runs in one daemon thread, while the
    heartbeat loop can continue reporting RUNNING and host telemetry.  Terminal receipts remain on
    disk until the control plane acknowledges them.
    """

    def __init__(
        self,
        state_path: Path,
        *,
        host_id: str | None = None,
        backend: ModelRuntimeBackend | None = None,
        clock: Callable[[], float] = time.time,
        signal_collector: HostSignalCollector | None = None,
        protection_loop: LocalHostProtectionLoop | None = None,
        override_path: Path | None = None,
        port_start: int = DEFAULT_PORT_START,
        port_end: int = DEFAULT_PORT_END,
        port_available: Callable[[int], bool] | None = None,
    ) -> None:
        if not 0 < port_start <= port_end < 65_536:
            raise ValueError("allocator runtime port range is invalid")
        self.state_path = state_path
        self.backend = backend or LlamaCppBackend()
        self._engine_api_key = secrets.token_urlsafe(32)
        self.clock = clock
        self.signal_collector = signal_collector or HostSignalCollector(clock=clock)
        self.protection_loop = protection_loop or LocalHostProtectionLoop()
        self.override_path = override_path or local_override_path(state_path)
        self.override_error = ""
        self.port_start = port_start
        self.port_end = port_end
        if port_available is not None:
            self._port_available = port_available
        else:
            bind_host = str(getattr(self.backend, "bind_host", "127.0.0.1"))
            self._port_available = lambda port: _port_available(
                port,
                bind_host=bind_host,
            )
        self._lock = threading.RLock()
        self._residencies: dict[str, ManagedResidency] = {}
        self._receipts: dict[str, ActionReceipt] = {}
        self._next_receipt_sequence = 1
        self._active_action_id: str | None = None
        self._shutting_down = False
        self._latest_plan_generation = ""
        self._superseded_plan_epochs: set[str] = set()
        self._decision: AdmissionDecision | None = None
        self._persisted_backend_config: dict[str, str] = {}
        self.host_id = host_id or f"host-{uuid.uuid4().hex[:16]}"
        self._restore()
        if host_id and self.host_id != host_id:
            raise ValueError(
                "persisted allocator host id does not match the requested host id"
            )
        self._validate_backend_configuration()
        key_file = engine_api_key_path(self.state_path)
        jsonio.atomic_write_bytes(
            key_file,
            f"{self._engine_api_key}\n".encode(),
            mode=0o600,
        )
        configure_api_key = getattr(self.backend, "configure_api_key", None)
        if callable(configure_api_key):
            configure_api_key(self._engine_api_key)
        configure_api_key_file = getattr(self.backend, "configure_api_key_file", None)
        if callable(configure_api_key_file):
            configure_api_key_file(key_file)
        self._recover()
        self._save()

    @property
    def residencies(self) -> tuple[ManagedResidency, ...]:
        with self._lock:
            return tuple(self._residencies[key] for key in sorted(self._residencies))

    @property
    def decision(self) -> AdmissionDecision | None:
        with self._lock:
            return self._decision

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._active_action_id is not None

    @property
    def shutting_down(self) -> bool:
        with self._lock:
            return self._shutting_down

    @property
    def engine_api_key(self) -> str:
        """Stable bearer credential for Grid's private path to managed model engines."""

        return self._engine_api_key

    def evaluate_host(self) -> AdmissionDecision:
        signals = self.signal_collector.collect()
        override = self._read_local_override()
        with self._lock:
            decision = self.protection_loop.evaluate(signals, override=override)
            self._decision = decision
            self._save_locked()
            return decision

    def begin(
        self, raw_action: MutationAction | Mapping[str, Any]
    ) -> ActionReceipt | None:
        action = (
            raw_action
            if isinstance(raw_action, MutationAction)
            else MutationAction.from_dict(raw_action)
        )
        now = max(0.0, float(self.clock()))
        with self._lock:
            prior_receipts = dict(self._receipts)
            prior_active_action_id = self._active_action_id
            prior_latest_generation = self._latest_plan_generation
            prior_superseded_epochs = set(self._superseded_plan_epochs)
            prior_next_receipt_sequence = self._next_receipt_sequence
            existing = self._receipts.get(action.action_id)
            if existing is not None:
                # The controller may have acknowledged this receipt and then restarted from a
                # durable snapshot that still contains the command. Redelivery is idempotent, but
                # it must re-arm the cached receipt or the controller can wait forever for an ACK
                # that this node believes it already sent.
                if existing.reported_status == existing.status:
                    existing = ActionReceipt(
                        existing.action_id,
                        existing.status,
                        existing.message,
                        existing.plan_generation,
                        existing.updated_at,
                        sequence=existing.sequence,
                    )
                    self._receipts[action.action_id] = existing
                    self._save_locked()
                return existing
            if action.node_id != self.host_id:
                return self._terminal_locked(
                    action,
                    MutationStatus.CANCELLED,
                    "command target does not match this host",
                    now,
                )
            if self._shutting_down:
                return self._terminal_locked(
                    action,
                    MutationStatus.CANCELLED,
                    "allocator node is shutting down",
                    now,
                )
            if not action.executable:
                return self._terminal_locked(
                    action,
                    MutationStatus.CANCELLED,
                    "recommendation is not an executable command",
                    now,
                )
            if self._generation_is_stale_locked(action.plan_generation):
                return self._terminal_locked(
                    action,
                    MutationStatus.CANCELLED,
                    "stale allocator plan generation",
                    now,
                )
            self._accept_generation_locked(action.plan_generation)
            if any(
                self._receipts.get(dependency) is None
                or self._receipts[dependency].status != MutationStatus.SUCCEEDED
                for dependency in action.dependencies
            ):
                # Do not acknowledge an action whose prerequisite is still in flight.  The server
                # redelivers it, and the stable action id makes the later begin idempotent.
                self._save_locked()
                return None
            if self._active_action_id is not None:
                return None
            if (
                action.kind in (ActionKind.LOAD, ActionKind.WARM)
                and self._decision is not None
                and not self._decision.accept
            ):
                return self._terminal_locked(
                    action,
                    MutationStatus.CANCELLED,
                    f"local host protection is {self._decision.state.value}",
                    now,
                )
            receipt = ActionReceipt(
                action.action_id,
                MutationStatus.RUNNING,
                "allocator action started",
                action.plan_generation,
                now,
                sequence=self._allocate_receipt_sequence_locked(),
            )
            self._receipts[action.action_id] = receipt
            self._active_action_id = action.action_id
            self._trim_receipts_locked()
            try:
                self._save_locked()
            except jsonio.AtomicWriteCommittedError as exc:
                # The RUNNING record is already the visible target, but its directory barrier was
                # not confirmed. Never launch a process from uncertain crash-durable intent and
                # never roll memory behind that visible file. Persist a compensating terminal
                # receipt; if this second write also fails, restart recovery converts the visible
                # RUNNING record to FAILED without any side effect having occurred.
                failed = ActionReceipt(
                    action.action_id,
                    MutationStatus.FAILED,
                    f"allocator command durability barrier failed: {exc}"[:500],
                    action.plan_generation,
                    now,
                    sequence=receipt.sequence,
                )
                self._receipts[action.action_id] = failed
                self._active_action_id = prior_active_action_id
                self._save_locked()
                return failed
            except BaseException:
                self._receipts = prior_receipts
                self._active_action_id = prior_active_action_id
                self._latest_plan_generation = prior_latest_generation
                self._superseded_plan_epochs = prior_superseded_epochs
                self._next_receipt_sequence = prior_next_receipt_sequence
                raise
        thread = threading.Thread(
            target=self._execute,
            args=(action,),
            name=f"grid-allocator-{action.kind.value}-{action.model_id}",
            daemon=True,
        )
        try:
            thread.start()
        except BaseException as exc:
            with self._lock:
                current = self._receipts.get(action.action_id)
                if current is not None and current.status == MutationStatus.RUNNING:
                    self._receipts[action.action_id] = ActionReceipt(
                        current.action_id,
                        MutationStatus.FAILED,
                        f"could not start allocator action worker: {exc}"[:500],
                        current.plan_generation,
                        max(0.0, float(self.clock())),
                        sequence=current.sequence,
                    )
                if self._active_action_id == action.action_id:
                    self._active_action_id = None
                try:
                    self._save_locked()
                except Exception as persistence_error:  # noqa: BLE001
                    # Preserve the worker-start exception. The in-memory terminal receipt prevents
                    # duplicate work; a restart converts the already-durable RUNNING receipt.
                    persistence_error.add_note(
                        "while persisting allocator worker-start failure"
                    )
            raise
        return receipt

    def reject(
        self,
        raw_action: MutationAction | Mapping[str, Any],
        message: str,
    ) -> ActionReceipt:
        """Persist a valid delivered command as terminally failed without a side effect.

        The node agent uses this admission boundary after refreshing host capacity but before a
        WARM reaches the process launcher. Re-delivery returns (and, if needed, re-arms) the exact
        prior receipt instead of changing its outcome.
        """

        action = (
            raw_action
            if isinstance(raw_action, MutationAction)
            else MutationAction.from_dict(raw_action)
        )
        reason = str(message).strip()
        if not reason:
            raise ValueError("rejected allocator action requires a message")
        now = max(0.0, float(self.clock()))
        with self._lock:
            existing = self._receipts.get(action.action_id)
            if existing is not None:
                if existing.reported_status == existing.status:
                    existing = ActionReceipt(
                        existing.action_id,
                        existing.status,
                        existing.message,
                        existing.plan_generation,
                        existing.updated_at,
                        sequence=existing.sequence,
                    )
                    self._receipts[action.action_id] = existing
                    self._save_locked()
                return existing
            if action.node_id != self.host_id:
                raise ValueError("command target does not match this host")
            if not action.executable:
                raise ValueError("recommendation is not an executable command")
            if self._generation_is_stale_locked(action.plan_generation):
                raise ValueError("stale allocator plan generation")
            if any(
                self._receipts.get(dependency) is None
                or self._receipts[dependency].status != MutationStatus.SUCCEEDED
                for dependency in action.dependencies
            ):
                raise ValueError("allocator action dependencies have not succeeded")
            self._accept_generation_locked(action.plan_generation)
            return self._terminal_locked(
                action,
                MutationStatus.FAILED,
                reason[:500],
                now,
            )

    def acknowledgements(self) -> list[dict[str, str]]:
        with self._lock:
            return [
                {
                    "action_id": receipt.action_id,
                    "status": receipt.status.value,
                    "message": receipt.message,
                }
                for receipt in sorted(
                    self._receipts.values(), key=lambda item: item.sequence
                )
                if receipt.reported_status != receipt.status
            ]

    def mark_acknowledged(self, acknowledgements: list[Mapping[str, Any]]) -> None:
        with self._lock:
            changed = False
            for acknowledgement in acknowledgements:
                action_id = str(acknowledgement.get("action_id") or "")
                receipt = self._receipts.get(action_id)
                if receipt is None:
                    continue
                try:
                    status = MutationStatus(acknowledgement["status"])
                except (KeyError, ValueError):
                    continue
                if status != receipt.status:
                    continue
                self._receipts[action_id] = ActionReceipt(
                    receipt.action_id,
                    receipt.status,
                    receipt.message,
                    receipt.plan_generation,
                    receipt.updated_at,
                    reported_status=receipt.status,
                    sequence=receipt.sequence,
                )
                changed = True
            if changed:
                self._trim_receipts_locked()
                self._save_locked()

    def allocator_envelope(self) -> dict[str, Any]:
        with self._lock:
            decision = self._decision
            node_now = max(0.0, float(self.clock()))
            residencies = [
                _model_residency_dict(item.to_model_residency(), now=node_now)
                for item in self.residencies
            ]
            shutting_down = self._shutting_down
            state = (
                NodeState.DRAINING
                if shutting_down
                else decision.state
                if decision
                else NodeState.ACCEPTING
            )
            cached = set(self.backend.cached_models())
            cached.update(
                item.model_id
                for item in self._residencies.values()
                if item.state == ResidencyState.CACHED
            )
            envelope: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "managed": True,
                "host_id": self.host_id,
                "state": state.value,
                # A local admission decision normally has precedence over top-level state in the
                # registry.  Clear it during shutdown so an earlier ACCEPTING decision cannot
                # accidentally override the DRAINING fence.
                "decision": decision.to_dict()
                if decision and not shutting_down
                else None,
                "residencies": residencies,
                "cached_models": sorted(cached),
                "actuator_capabilities": [item.value for item in ActionKind],
                "max_models": self.port_end - self.port_start + 1,
                "latest_plan_generation": self._latest_plan_generation,
                "manually_managed": False,
            }
            ca_pem = str(getattr(self.backend, "tls_ca_pem", "") or "")
            if ca_pem:
                # The authenticated registration transports this to Grid so its private upstream
                # client can validate an intranet/self-signed CA. The server strips it from every
                # public/status allocator envelope before storing the node.
                envelope["engine_tls_ca_pem"] = ca_pem
            return envelope

    def endpoint_for(self, model_id: str, *, host: str = "127.0.0.1") -> str | None:
        with self._lock:
            residency = self._residencies.get(model_id)
            if (
                residency is None
                or residency.handle is None
                or residency.state
                not in (ResidencyState.READY, ResidencyState.DRAINING)
            ):
                return None
            scheme = str(getattr(self.backend, "endpoint_scheme", "http"))
            return f"{scheme}://{_url_host(host)}:{residency.handle.port}/v1"

    def _validate_backend_configuration(self) -> None:
        if not self._persisted_backend_config:
            return
        current = _backend_config(self.backend)
        if current != self._persisted_backend_config:
            raise ValueError(
                "persisted allocator engine transport does not match the requested backend "
                "configuration"
            )

    def record_model_used(self, model_id: str, timestamp: float) -> bool:
        """Durably advance one residency's last-use watermark.

        Proxy observations can arrive concurrently with heartbeats and lifecycle mutations. The
        monotonic max prevents a delayed observation from moving the scale-down cooldown backward.
        Unknown models are harmless because a request may finish just after its residency unloads.
        """

        try:
            observed_at = float(timestamp)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                "model usage timestamp must be finite and non-negative"
            ) from exc
        if not math.isfinite(observed_at) or observed_at < 0:
            raise ValueError("model usage timestamp must be finite and non-negative")
        with self._lock:
            current = self._residencies.get(model_id)
            if current is None or observed_at <= current.last_used_at:
                return False
            self._residencies[model_id] = ManagedResidency(
                model_id=current.model_id,
                memory_mb=current.memory_mb,
                state=current.state,
                loaded_at=current.loaded_at,
                last_used_at=observed_at,
                load_failures=current.load_failures,
                pinned=current.pinned,
                handle=current.handle,
            )
            self._save_locked()
            return True

    def active_requests(self, model_id: str) -> int | None:
        """Return direct engine activity when supported, without breaking legacy backends."""

        with self._lock:
            residency = self._residencies.get(model_id)
            handle = residency.handle if residency is not None else None
        if handle is None:
            return 0
        query = getattr(self.backend, "active_requests", None)
        if not callable(query):
            return None
        try:
            count = query(handle, model_id)
        except Exception:  # noqa: BLE001 - backend observation failure means unknown activity
            return None
        if count is None:
            return None
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            return None
        return count

    def reconcile_process_health(
        self,
        *,
        deadline: float | None = None,
        max_workers: int = DEFAULT_HEALTH_PROBE_WORKERS,
    ) -> bool:
        """Fence dead, unready, or ambiguously owned children before they are advertised.

        A definitely dead handle is safe to forget. A live handle whose ownership cannot be
        proven is retained in FAILED state: this deliberately blocks a duplicate start and avoids
        ever signalling a PID-reused process. A proven child can recover to READY after a transient
        readiness failure without being restarted.
        """

        if not 1 <= int(max_workers) <= MAX_HEALTH_PROBE_WORKERS:
            raise ValueError(
                f"max_workers must be between 1 and {MAX_HEALTH_PROBE_WORKERS}"
            )
        with self._lock:
            snapshot = tuple(
                (model_id, residency)
                for model_id, residency in self._residencies.items()
                if residency.handle is not None
            )
        observations = self._probe_process_health(
            snapshot,
            deadline=deadline,
            max_workers=int(max_workers),
        )
        changed = False
        with self._lock:
            for model_id, residency in snapshot:
                if self._residencies.get(model_id) != residency:
                    continue
                observation = observations.get(model_id)
                next_residency = self._residency_after_health_probe(
                    residency,
                    observation,
                    recovering=False,
                )
                if next_residency != residency:
                    self._residencies[model_id] = next_residency
                    changed = True
            if changed:
                self._save_locked()
        return changed

    def _probe_process_health(
        self,
        snapshot: tuple[tuple[str, ManagedResidency], ...],
        *,
        deadline: float | None,
        max_workers: int,
    ) -> dict[str, _ProcessHealth]:
        if not snapshot:
            return {}

        def probe(model_id: str, residency: ManagedResidency) -> _ProcessHealth:
            assert residency.handle is not None
            try:
                alive = self._backend_alive(residency.handle, model_id)
                if not alive:
                    return _ProcessHealth(False, False, False)
                owned = bool(self.backend.owns(residency.handle, model_id))
                ready = owned and bool(self.backend.ready(residency.handle, model_id))
                return _ProcessHealth(True, owned, ready)
            except (Exception, SystemExit):  # noqa: BLE001
                return _ProcessHealth(None, False, False)

        executor = ThreadPoolExecutor(
            max_workers=min(max_workers, len(snapshot)),
            thread_name_prefix="grid-runtime-health",
        )
        futures: dict[Future[_ProcessHealth], str] = {
            executor.submit(probe, model_id, residency): model_id
            for model_id, residency in snapshot
        }
        timeout = None
        if deadline is not None:
            timeout = max(0.0, float(deadline) - time.monotonic())
        completed, pending = wait(futures, timeout=timeout)
        observations: dict[str, _ProcessHealth] = {}
        for future in completed:
            try:
                observations[futures[future]] = future.result()
            except CancelledError:  # pragma: no cover - completed set excludes cancellation
                observations[futures[future]] = _ProcessHealth(None, False, False)
        for future in pending:
            if future.done():
                try:
                    observations[futures[future]] = future.result()
                except CancelledError:  # pragma: no cover - done-after-wait cancellation race
                    observations[futures[future]] = _ProcessHealth(None, False, False)
                continue
            if future.cancel():
                observations[futures[future]] = _ProcessHealth(
                    None,
                    False,
                    False,
                    started=False,
                )
            else:
                observations[futures[future]] = _ProcessHealth(None, False, False)
        executor.shutdown(wait=False, cancel_futures=True)
        return observations

    def _residency_after_health_probe(
        self,
        residency: ManagedResidency,
        observation: _ProcessHealth | None,
        *,
        recovering: bool,
        cached: bool = False,
    ) -> ManagedResidency:
        handle = residency.handle
        assert handle is not None
        if observation is not None and not observation.started and not recovering:
            # A bounded cycle can leave work queued behind the worker cap. No probe began, so this
            # is absence of evidence rather than negative health; leave the prior state untouched.
            return residency
        if observation is not None and observation.ready:
            state = (
                ResidencyState.DRAINING
                if residency.state == ResidencyState.DRAINING or self._shutting_down
                else ResidencyState.READY
            )
            return ManagedResidency(
                residency.model_id,
                residency.memory_mb,
                state,
                residency.loaded_at,
                residency.last_used_at,
                residency.load_failures,
                residency.pinned,
                handle,
            )
        if (
            not recovering
            and observation is not None
            and observation.alive
            and observation.owned
            and residency.state == ResidencyState.WARMING
            and self._active_action_id is not None
        ):
            return residency
        alive = observation.alive if observation is not None else None
        state = (
            ResidencyState.CACHED
            if recovering and alive is False and cached
            else ResidencyState.FAILED
        )
        return ManagedResidency(
            residency.model_id,
            residency.memory_mb,
            state,
            residency.loaded_at,
            residency.last_used_at,
            residency.load_failures
            + int(
                state == ResidencyState.FAILED
                and (recovering or residency.state != ResidencyState.FAILED)
            ),
            residency.pinned,
            handle if alive is not False else None,
        )

    def begin_shutdown(self) -> None:
        """Fence new work and mark every live residency as draining without stopping it.

        The node agent publishes this state before waiting for the proxy-owned in-flight counters.
        Startup cancellation happens at the same fence: a model process that is still inside its
        readiness probe must never outlive the allocator node as an untracked orphan.
        """

        with self._lock:
            self._shutting_down = True
            changed = False
            for model_id, residency in list(self._residencies.items()):
                if (
                    residency.handle is None
                    or residency.state == ResidencyState.DRAINING
                ):
                    continue
                self._residencies[model_id] = ManagedResidency(
                    model_id=residency.model_id,
                    memory_mb=residency.memory_mb,
                    state=ResidencyState.DRAINING,
                    loaded_at=residency.loaded_at,
                    last_used_at=residency.last_used_at,
                    load_failures=residency.load_failures,
                    pinned=residency.pinned,
                    handle=residency.handle,
                )
                changed = True
            if changed:
                self._save_locked()
        cancel_pending = getattr(self.backend, "cancel_pending", None)
        if callable(cancel_pending):
            cancel_pending()

    def stop_all(self, *, wait_timeout: float = 5.0, force: bool = False) -> None:
        """Stop every allocator-owned process after fencing starts, leaving weights cached.

        A full disk must not turn one state-file update into an orphan factory. Persistence errors
        are retained and raised only after every handle in the ownership snapshot has had its stop
        attempted.
        """

        if wait_timeout < 0:
            raise ValueError("wait_timeout must be non-negative")
        persistence_errors: list[tuple[str, Exception]] = []
        stop_errors: list[tuple[str, Exception]] = []
        try:
            self.begin_shutdown()
        except (OSError, RuntimeError) as exc:
            persistence_errors.append(("shutdown fence", exc))
            # begin_shutdown normally reaches this cancellation after its state write. If that
            # write failed, explicitly wake any child still blocked in readiness probing.
            cancel_pending = getattr(self.backend, "cancel_pending", None)
            if callable(cancel_pending):
                try:
                    cancel_pending()
                except (OSError, RuntimeError) as cancel_exc:
                    persistence_errors.append(("startup cancellation", cancel_exc))
        # A cancelling backend wakes the action worker out of readiness probing.  Joining through
        # the state-machine gate makes shutdown deterministic and prevents the daemon thread from
        # disappearing while it still owns a just-spawned child.
        deadline = time.monotonic() + wait_timeout
        self.wait_idle(max(0.0, deadline - time.monotonic()))
        targets = [item for item in self.residencies if item.handle is not None]
        stopped: list[ManagedResidency] = []
        bounded_stop = getattr(self.backend, "stop_with_timeout", None)
        if callable(bounded_stop) and targets:
            # Every llama child receives the same absolute budget concurrently. Sequential 15s
            # stops made shutdown O(number of models), which could exceed the node's whole graceful
            # deadline by minutes. Keep a tiny force-confirmation allowance even when draining used
            # the nominal budget; the threads share it, so it is constant rather than per child.
            stop_deadline = max(deadline, time.monotonic() + 0.25)
            outcomes: dict[str, Exception | None] = {}
            outcome_lock = threading.Lock()

            def stop_one(residency: ManagedResidency) -> None:
                assert residency.handle is not None
                error: Exception | None = None
                try:
                    self._wait_for_direct_activity(
                        residency,
                        deadline=deadline,
                        force=force,
                    )
                    bounded_stop(
                        residency.handle,
                        residency.model_id,
                        timeout=max(0.0, stop_deadline - time.monotonic() - 0.01),
                    )
                except Exception as exc:  # noqa: BLE001 - every backend failure is a stop failure
                    error = exc
                with outcome_lock:
                    outcomes[residency.model_id] = error

            threads = [
                threading.Thread(target=stop_one, args=(residency,), daemon=True)
                for residency in targets
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(max(0.0, stop_deadline - time.monotonic()))
            for residency, thread in zip(targets, threads, strict=True):
                error = outcomes.get(
                    residency.model_id,
                    RuntimeError("process stop worker ended without an outcome"),
                )
                if thread.is_alive():
                    error = RuntimeError(
                        "process stop exceeded the shared shutdown deadline"
                    )
                if error is None:
                    stopped.append(residency)
                else:
                    stop_errors.append((residency.model_id, error))
        else:
            for residency in targets:
                assert residency.handle is not None
                try:
                    self._wait_for_direct_activity(
                        residency,
                        deadline=deadline,
                        force=force,
                    )
                    self.backend.stop(residency.handle, residency.model_id)
                except Exception as exc:  # noqa: BLE001 - every backend failure is a stop failure
                    stop_errors.append((residency.model_id, exc))
                else:
                    stopped.append(residency)

        with self._lock:
            changed = False
            for residency in stopped:
                handle = residency.handle
                assert handle is not None
                current = self._residencies.get(residency.model_id)
                if current and current.handle == handle:
                    self._residencies[residency.model_id] = ManagedResidency(
                        model_id=current.model_id,
                        memory_mb=current.memory_mb,
                        state=ResidencyState.CACHED,
                        loaded_at=current.loaded_at,
                        last_used_at=current.last_used_at,
                        load_failures=current.load_failures,
                        pinned=current.pinned,
                    )
                    changed = True
            if changed:
                try:
                    self._save_locked()
                except OSError as exc:
                    persistence_errors.append(("runtime state", exc))
        cleanup_errors = [*stop_errors, *persistence_errors]
        if cleanup_errors:
            details = "; ".join(
                f"{model_id}: {type(error).__name__}: {error}"
                for model_id, error in cleanup_errors
            )
            summary = (
                "allocator-owned process cleanup was incomplete"
                if stop_errors
                else "allocator-owned processes were cleaned up, but runtime state persistence failed"
            )
            raise RuntimeError(f"{summary} ({details})") from cleanup_errors[0][1]

    def _wait_for_direct_activity(
        self,
        residency: ManagedResidency,
        *,
        deadline: float,
        force: bool,
    ) -> None:
        """Drain direct-to-engine requests inside the caller's one shared deadline."""

        if force:
            return
        query = getattr(self.backend, "active_requests", None)
        if not callable(query):
            # Older/testing backends have no direct network endpoint. Preserve compatibility;
            # production llama.cpp implements this observation boundary.
            return
        handle = residency.handle
        assert handle is not None
        while True:
            try:
                count = query(handle, residency.model_id)
            except Exception:  # noqa: BLE001 - an observation failure is deliberately fail-safe
                count = None
            if count is not None and (
                isinstance(count, bool) or not isinstance(count, int) or count < 0
            ):
                count = None
            if count == 0:
                return
            if count is None:
                alive = self._backend_alive(handle, residency.model_id)
                owned = alive and self.backend.owns(handle, residency.model_id)
                if alive and owned:
                    raise RuntimeError(
                        "direct engine activity is unknown; refusing non-force shutdown"
                    )
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f"{count} direct engine request(s) exceeded the shared shutdown deadline"
                )
            time.sleep(min(0.05, remaining))

    def _assert_direct_idle(
        self,
        handle: RuntimeHandle,
        model_id: str,
        *,
        require_observation: bool = False,
    ) -> None:
        """Fail safe before UNLOAD or failed-process replacement when activity is unknowable."""

        query = getattr(self.backend, "active_requests", None)
        if not callable(query):
            if require_observation:
                raise RuntimeError(
                    "direct engine activity cannot be observed; refusing to replace the model"
                )
            return
        try:
            count = query(handle, model_id)
        except Exception:  # noqa: BLE001 - observation failure is treated as unknown, not idle
            count = None
        if count is not None and (
            isinstance(count, bool) or not isinstance(count, int) or count < 0
        ):
            count = None
        if count is None:
            raise RuntimeError(
                "direct engine activity is unknown; refusing to unload the model"
            )
        if count:
            raise RuntimeError(
                f"refusing to unload model with {count} direct engine request(s) active"
            )

    def wait_idle(self, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.busy:
                return True
            time.sleep(0.01)
        return not self.busy

    def _execute(self, action: MutationAction) -> None:
        try:
            if action.kind == ActionKind.LOAD:
                self._load(action)
            elif action.kind == ActionKind.WARM:
                self._warm(action)
            elif action.kind == ActionKind.DRAIN:
                self._drain(action)
            elif action.kind == ActionKind.UNLOAD:
                self._unload(action)
            else:  # pragma: no cover - enum exhaustiveness guard
                raise RuntimeError(f"unsupported allocator action: {action.kind}")
        except (Exception, SystemExit) as exc:  # noqa: BLE001
            # ``launcher`` uses SystemExit for operator-facing startup errors; in the detached
            # actuator that is an action failure, not a reason to terminate the node loop.
            self._fail(action, exc)
        else:
            self._finish(
                action, MutationStatus.SUCCEEDED, f"{action.kind.value} complete"
            )

    def _load(self, action: MutationAction) -> None:
        if action.model_id not in self.backend.cached_models():
            raise RuntimeError(
                f"model weights are not cached: {action.model_id!r}; pull an immutable GGUF first"
            )
        with self._lock:
            current = self._residencies.get(action.model_id)
            if (
                current
                and current.handle
                and self._backend_alive(current.handle, current.model_id)
            ):
                # LOAD only verifies weights. Never discard a live runtime handle, including an
                # ambiguous FAILED one, because that could permit a later duplicate WARM.
                return
            if current and current.state in (
                ResidencyState.READY,
                ResidencyState.WARMING,
                ResidencyState.DRAINING,
            ):
                return
            self._residencies[action.model_id] = ManagedResidency(
                model_id=action.model_id,
                memory_mb=action.memory_mb,
                state=ResidencyState.CACHED,
                loaded_at=current.loaded_at if current else 0.0,
                last_used_at=current.last_used_at if current else 0.0,
                load_failures=current.load_failures if current else 0,
                pinned=current.pinned if current else False,
            )
            self._save_locked()

    def _warm(self, action: MutationAction) -> None:
        if action.model_id not in self.backend.cached_models():
            raise RuntimeError(f"cannot warm uncached model {action.model_id!r}")
        recovery_handle: RuntimeHandle | None = None
        with self._lock:
            if self._shutting_down:
                raise RuntimeError("allocator node is shutting down")
            current = self._residencies.get(action.model_id)
            if current and current.handle:
                alive = self._backend_alive(current.handle, current.model_id)
                owned = alive and self.backend.owns(current.handle, current.model_id)
                ready = owned and self.backend.ready(current.handle, current.model_id)
                if ready:
                    if current.state != ResidencyState.READY:
                        # Demand can rebound after DRAIN but before UNLOAD. Re-admit the proven-live
                        # process instead of starting a duplicate copy on a second port.
                        self._residencies[action.model_id] = ManagedResidency(
                            model_id=current.model_id,
                            memory_mb=current.memory_mb,
                            state=ResidencyState.READY,
                            loaded_at=current.loaded_at,
                            last_used_at=max(current.last_used_at, float(self.clock())),
                            load_failures=current.load_failures,
                            pinned=current.pinned,
                            handle=current.handle,
                        )
                        self._save_locked()
                    return
                if alive:
                    if current.state != ResidencyState.FAILED or not owned:
                        raise RuntimeError(
                            "existing model process is alive but is not ready or cannot be proven "
                            "owned"
                        )
                    # Health reconciliation has already removed FAILED from routing. Persist a
                    # non-routable recovery marker before touching the proven-owned child, then make
                    # the direct-engine slot table prove it is idle outside this lock. A crash at any
                    # later point retains the exact handle for restart adoption/cleanup.
                    recovery_handle = current.handle
                    current = ManagedResidency(
                        current.model_id,
                        current.memory_mb,
                        ResidencyState.WARMING,
                        current.loaded_at,
                        current.last_used_at,
                        current.load_failures,
                        current.pinned,
                        current.handle,
                    )
                    self._residencies[action.model_id] = current
                    self._save_locked()
                else:
                    # A definitely dead handle cannot be adopted and is safe to replace.
                    current = ManagedResidency(
                        current.model_id,
                        current.memory_mb,
                        ResidencyState.FAILED,
                        current.loaded_at,
                        current.last_used_at,
                        current.load_failures,
                        current.pinned,
                    )
                    self._residencies[action.model_id] = current
                    self._save_locked()

        if recovery_handle is not None:
            # A FAILED child may still be serving a direct caller that bypassed Grid's registry. Only
            # an explicit zero is safe evidence for replacement; missing/invalid telemetry and any
            # active slot leave the exact process intact and fail the action.
            self._assert_direct_idle(
                recovery_handle,
                action.model_id,
                require_observation=True,
            )
            with self._lock:
                if self._shutting_down:
                    raise RuntimeError("allocator node is shutting down")
                current = self._residencies.get(action.model_id)
                if current is None:
                    raise RuntimeError(
                        "model residency disappeared during failed-process recovery"
                    )
                if current.handle != recovery_handle:
                    if (
                        current.state == ResidencyState.READY
                        and current.handle is not None
                    ):
                        return
                    if current.handle is not None:
                        raise RuntimeError(
                            "model process identity changed during failed-process recovery"
                        )
                elif current.state == ResidencyState.READY:
                    # A concurrent health probe proved the original child healthy while activity was
                    # sampled. Keep it instead of terminating a recovered process.
                    return
                elif current.state != ResidencyState.WARMING:
                    raise RuntimeError(
                        "model residency changed during failed-process recovery"
                    )
                else:
                    alive = self._backend_alive(recovery_handle, action.model_id)
                    owned = alive and self.backend.owns(
                        recovery_handle, action.model_id
                    )
                    if alive and not owned:
                        raise RuntimeError(
                            "existing model process can no longer be proven owned"
                        )
                    if owned and self.backend.ready(recovery_handle, action.model_id):
                        self._residencies[action.model_id] = ManagedResidency(
                            model_id=current.model_id,
                            memory_mb=current.memory_mb,
                            state=ResidencyState.READY,
                            loaded_at=current.loaded_at,
                            last_used_at=max(current.last_used_at, float(self.clock())),
                            load_failures=current.load_failures,
                            pinned=current.pinned,
                            handle=recovery_handle,
                        )
                        self._save_locked()
                        return
                    if owned:
                        # Hold the runtime state lock across this bounded exact-ownership stop so
                        # shutdown or health reconciliation cannot adopt/relabel the same handle
                        # between the final proof and termination.
                        self.backend.stop(recovery_handle, action.model_id)
                    current = ManagedResidency(
                        current.model_id,
                        current.memory_mb,
                        ResidencyState.FAILED,
                        current.loaded_at,
                        current.last_used_at,
                        current.load_failures,
                        current.pinned,
                    )
                    self._residencies[action.model_id] = current
                    self._save_locked()

        with self._lock:
            if self._shutting_down:
                raise RuntimeError("allocator node is shutting down")
            current = self._residencies.get(action.model_id)
            if current and current.handle:
                # Another lifecycle observer recovered or replaced the child while this action was
                # proving the old process safe to stop. Never create a second copy.
                raise RuntimeError(
                    "model process changed while preparing a replacement"
                )
            port = self._free_port_locked()
            failures = current.load_failures if current else 0
            self._residencies[action.model_id] = ManagedResidency(
                model_id=action.model_id,
                memory_mb=action.memory_mb,
                state=ResidencyState.WARMING,
                loaded_at=current.loaded_at if current else 0.0,
                last_used_at=current.last_used_at if current else 0.0,
                load_failures=failures,
                pinned=current.pinned if current else False,
            )
            self._save_locked()
        with self._lock:
            if self._shutting_down:
                raise RuntimeError("allocator node is shutting down")

        def persist_spawn(handle: RuntimeHandle) -> None:
            with self._lock:
                self._residencies[action.model_id] = ManagedResidency(
                    model_id=action.model_id,
                    memory_mb=action.memory_mb,
                    state=ResidencyState.WARMING,
                    loaded_at=current.loaded_at if current else 0.0,
                    last_used_at=current.last_used_at if current else 0.0,
                    load_failures=failures,
                    pinned=current.pinned if current else False,
                    handle=handle,
                )
                self._save_locked()

        start_with_callback = getattr(self.backend, "start_with_callback", None)
        if callable(start_with_callback):
            handle = start_with_callback(action.model_id, port, persist_spawn)
        else:
            # Compatibility for third-party/testing backends. Production llama.cpp uses the
            # callback path above, which closes the spawn-to-persist crash window.
            handle = self.backend.start(action.model_id, port)
            persist_spawn(handle)
        with self._lock:
            shutting_down = self._shutting_down
        if shutting_down:
            self.backend.stop(handle, action.model_id)
            raise RuntimeError("allocator node shut down while the model was warming")
        now = max(0.0, float(self.clock()))
        with self._lock:
            self._residencies[action.model_id] = ManagedResidency(
                model_id=action.model_id,
                memory_mb=action.memory_mb,
                state=ResidencyState.READY,
                loaded_at=now,
                last_used_at=now,
                load_failures=failures,
                pinned=current.pinned if current else False,
                handle=handle,
            )
            self._save_locked()

    def _drain(self, action: MutationAction) -> None:
        with self._lock:
            current = self._residencies.get(action.model_id)
            if current is None or current.state == ResidencyState.CACHED:
                return
            self._residencies[action.model_id] = ManagedResidency(
                model_id=current.model_id,
                memory_mb=current.memory_mb,
                state=ResidencyState.DRAINING,
                loaded_at=current.loaded_at,
                last_used_at=current.last_used_at,
                load_failures=current.load_failures,
                pinned=current.pinned,
                handle=current.handle,
            )
            self._save_locked()

    def _unload(self, action: MutationAction) -> None:
        with self._lock:
            current = self._residencies.get(action.model_id)
            if current is None or (
                current.state == ResidencyState.CACHED and current.handle is None
            ):
                return
            if current.state not in (ResidencyState.DRAINING, ResidencyState.FAILED):
                raise RuntimeError("model must be drained before unload")
            handle = current.handle
        if handle is not None:
            self._assert_direct_idle(handle, action.model_id)
            self.backend.stop(handle, action.model_id)
        with self._lock:
            current = self._residencies.get(action.model_id)
            if current is None:
                return
            self._residencies[action.model_id] = ManagedResidency(
                model_id=current.model_id,
                memory_mb=current.memory_mb,
                state=ResidencyState.CACHED,
                loaded_at=current.loaded_at,
                last_used_at=current.last_used_at,
                load_failures=current.load_failures,
                pinned=current.pinned,
            )
            self._save_locked()

    def _fail(self, action: MutationAction, exc: BaseException) -> None:
        message = str(exc).strip() or type(exc).__name__
        with self._lock:
            current = self._residencies.get(action.model_id)
            if action.kind in (ActionKind.LOAD, ActionKind.WARM):
                handle = current.handle if current else None
                if handle is not None and not self._backend_alive(
                    handle, action.model_id
                ):
                    handle = None
                failure_state = (
                    ResidencyState.DRAINING
                    if self._shutting_down and handle is not None
                    else ResidencyState.FAILED
                )
                self._residencies[action.model_id] = ManagedResidency(
                    model_id=action.model_id,
                    memory_mb=action.memory_mb,
                    state=failure_state,
                    loaded_at=current.loaded_at if current else 0.0,
                    last_used_at=current.last_used_at if current else 0.0,
                    load_failures=(current.load_failures if current else 0) + 1,
                    pinned=current.pinned if current else False,
                    handle=handle,
                )
            self._finish_locked(action, MutationStatus.FAILED, message)

    def _finish(
        self, action: MutationAction, status: MutationStatus, message: str
    ) -> None:
        with self._lock:
            self._finish_locked(action, status, message)

    def _finish_locked(
        self,
        action: MutationAction,
        status: MutationStatus,
        message: str,
    ) -> None:
        existing = self._receipts.get(action.action_id)
        self._receipts[action.action_id] = ActionReceipt(
            action.action_id,
            status,
            message[:500],
            action.plan_generation,
            max(0.0, float(self.clock())),
            sequence=(
                existing.sequence
                if existing is not None
                else self._allocate_receipt_sequence_locked()
            ),
        )
        if self._active_action_id == action.action_id:
            self._active_action_id = None
        self._trim_receipts_locked()
        self._save_locked()

    def _terminal_locked(
        self,
        action: MutationAction,
        status: MutationStatus,
        message: str,
        now: float,
    ) -> ActionReceipt:
        receipt = ActionReceipt(
            action.action_id,
            status,
            message,
            action.plan_generation,
            now,
            sequence=self._allocate_receipt_sequence_locked(),
        )
        self._receipts[action.action_id] = receipt
        self._trim_receipts_locked()
        self._save_locked()
        return receipt

    def _free_port_locked(self) -> int:
        allocated = {
            item.handle.port
            for item in self._residencies.values()
            if item.handle is not None
        }
        for port in range(self.port_start, self.port_end + 1):
            if port in allocated or not self._port_available(port):
                continue
            return port
        raise RuntimeError("no free allocator runtime ports remain")

    def _recover(self) -> None:
        cached = set(self.backend.cached_models())
        with self._lock:
            for action_id, receipt in list(self._receipts.items()):
                if receipt.status == MutationStatus.RUNNING:
                    self._receipts[action_id] = ActionReceipt(
                        receipt.action_id,
                        MutationStatus.FAILED,
                        "node restarted during allocator action",
                        receipt.plan_generation,
                        max(0.0, float(self.clock())),
                        sequence=receipt.sequence,
                    )
            for model_id, residency in list(self._residencies.items()):
                if residency.state == ResidencyState.LOADING:
                    state = (
                        ResidencyState.CACHED
                        if model_id in cached
                        else ResidencyState.FAILED
                    )
                    self._residencies[model_id] = ManagedResidency(
                        model_id,
                        residency.memory_mb,
                        state,
                        residency.loaded_at,
                        residency.last_used_at,
                        residency.load_failures + int(state == ResidencyState.FAILED),
                        residency.pinned,
                    )
                    continue
                if residency.handle is not None:
                    continue
                if residency.state not in (
                    ResidencyState.READY,
                    ResidencyState.WARMING,
                    ResidencyState.DRAINING,
                ):
                    continue
                state = (
                    ResidencyState.CACHED
                    if model_id in cached
                    else ResidencyState.FAILED
                )
                self._residencies[model_id] = ManagedResidency(
                    model_id,
                    residency.memory_mb,
                    state,
                    residency.loaded_at,
                    residency.last_used_at,
                    residency.load_failures + int(state == ResidencyState.FAILED),
                    residency.pinned,
                )
            snapshot = tuple(
                (model_id, residency)
                for model_id, residency in self._residencies.items()
                if residency.handle is not None
            )
            self._active_action_id = None
        observations = self._probe_process_health(
            snapshot,
            deadline=time.monotonic() + RECOVERY_HEALTH_DEADLINE_SECONDS,
            max_workers=DEFAULT_HEALTH_PROBE_WORKERS,
        )
        with self._lock:
            for model_id, residency in snapshot:
                if self._residencies.get(model_id) != residency:
                    continue
                self._residencies[model_id] = self._residency_after_health_probe(
                    residency,
                    observations.get(model_id),
                    recovering=True,
                    cached=model_id in cached,
                )

    def _backend_alive(self, handle: RuntimeHandle, model_id: str) -> bool:
        alive = getattr(self.backend, "alive", None)
        if callable(alive):
            return bool(alive(handle))
        # Legacy backends have no independent liveness boundary; their in-memory ownership proof
        # is the only safe signal available. Production backends implement ``alive``.
        return bool(self.backend.owns(handle, model_id))

    def _trim_receipts_locked(self) -> None:
        if len(self._receipts) <= MAX_RECEIPTS:
            return
        ordered = sorted(self._receipts.values(), key=lambda item: item.sequence)
        removable = [
            item
            for item in ordered
            if item.status != MutationStatus.RUNNING
            and item.reported_status == item.status
        ]
        for receipt in removable[: len(self._receipts) - MAX_RECEIPTS]:
            self._receipts.pop(receipt.action_id, None)

    def _allocate_receipt_sequence_locked(self) -> int:
        sequence = self._next_receipt_sequence
        self._next_receipt_sequence += 1
        return sequence

    def _read_local_override(self) -> LocalOverride | None:
        if not os.path.lexists(self.override_path):
            self.override_error = ""
            return None
        try:
            value = jsonio.load_json(self.override_path)
            override = LocalOverride.from_dict(value)
        except (
            KeyError,
            OSError,
            OverflowError,
            SystemExit,
            TypeError,
            ValueError,
        ) as exc:
            self.override_error = str(exc).strip() or "invalid local override file"
            # An operator-created control file is an admission boundary.  Corruption, a partial
            # manual edit, or an unknown future schema must never silently turn a fenced host back
            # into an accepting one.
            return LocalOverride.quarantine("invalid_local_override_file")
        self.override_error = ""
        return override

    def _generation_is_stale_locked(self, candidate: str) -> bool:
        candidate_epoch = _epoch_generation(candidate)
        latest_epoch = _epoch_generation(self._latest_plan_generation)
        if candidate_epoch is not None:
            epoch, sequence = candidate_epoch
            if epoch in self._superseded_plan_epochs:
                return True
            if latest_epoch is None:
                return False
            latest_name, latest_sequence = latest_epoch
            return epoch == latest_name and sequence < latest_sequence
        if latest_epoch is not None:
            # Once this runtime has joined an epoch-fenced controller, an old unstructured command
            # cannot be allowed to roll the host back to the legacy timestamp namespace.
            return True
        return _generation_is_older(candidate, self._latest_plan_generation)

    def _accept_generation_locked(self, candidate: str) -> None:
        candidate_epoch = _epoch_generation(candidate)
        latest_epoch = _epoch_generation(self._latest_plan_generation)
        if candidate_epoch is None:
            self._latest_plan_generation = _newer_generation(
                self._latest_plan_generation, candidate
            )
            return
        epoch, sequence = candidate_epoch
        if latest_epoch is not None:
            latest_name, latest_sequence = latest_epoch
            if epoch != latest_name:
                self._superseded_plan_epochs.add(latest_name)
                self._latest_plan_generation = candidate
                return
            if sequence < latest_sequence:
                return
        self._latest_plan_generation = candidate

    def _restore(self) -> None:
        if not self.state_path.exists():
            return
        value = jsonio.load_json(self.state_path)
        if (
            int(value.get("schema_version", RUNTIME_SCHEMA_VERSION))
            != RUNTIME_SCHEMA_VERSION
        ):
            raise ValueError("unsupported managed-runtime schema")
        persisted_host = str(value.get("host_id") or "")
        if persisted_host:
            self.host_id = persisted_host
        if "engine_api_key" in value:
            self._engine_api_key = _validated_api_key(value["engine_api_key"])
        raw_backend = value.get("backend_config")
        if isinstance(raw_backend, Mapping):
            self._persisted_backend_config = {
                str(key): str(raw_backend.get(key) or "")
                for key in (
                    "bind_host",
                    "endpoint_host",
                    "endpoint_scheme",
                    "tls_cert_file",
                    "tls_key_file",
                    "tls_ca_file",
                    "tls_ca_pem",
                )
            }
        self._latest_plan_generation = str(value.get("latest_plan_generation") or "")
        self._superseded_plan_epochs = {
            str(item)
            for item in value.get("superseded_plan_epochs") or ()
            if isinstance(item, str) and item
        }
        self._residencies = {
            item.model_id: item
            for row in value.get("residencies") or ()
            if isinstance(row, Mapping)
            for item in (ManagedResidency.from_dict(row),)
        }
        self._receipts = {}
        for row in value.get("receipts") or ():
            if not isinstance(row, Mapping):
                continue
            item = ActionReceipt.from_dict(row)
            if item.sequence == 0:
                item = ActionReceipt(
                    item.action_id,
                    item.status,
                    item.message,
                    item.plan_generation,
                    item.updated_at,
                    reported_status=item.reported_status,
                    sequence=self._next_receipt_sequence,
                )
            self._receipts[item.action_id] = item
            self._next_receipt_sequence = max(
                self._next_receipt_sequence,
                item.sequence + 1,
            )
        try:
            persisted_next_sequence = int(value.get("next_receipt_sequence") or 1)
        except (TypeError, ValueError, OverflowError):
            persisted_next_sequence = 1
        self._next_receipt_sequence = max(
            self._next_receipt_sequence,
            persisted_next_sequence,
        )
        raw_local = value.get("local_state")
        if isinstance(raw_local, Mapping):
            self.protection_loop.reset(LocalAllocatorState.from_dict(raw_local))

    def _save(self) -> None:
        with self._lock:
            self._save_locked()

    def _save_locked(self) -> None:
        payload = {
            "schema_version": RUNTIME_SCHEMA_VERSION,
            "host_id": self.host_id,
            "engine_api_key": self._engine_api_key,
            "backend_config": _backend_config(self.backend),
            "next_receipt_sequence": self._next_receipt_sequence,
            "latest_plan_generation": self._latest_plan_generation,
            "superseded_plan_epochs": sorted(self._superseded_plan_epochs),
            "local_state": self.protection_loop.state.to_dict(),
            "residencies": [item.to_dict() for item in self.residencies],
            "receipts": [
                item.to_dict()
                for item in sorted(
                    self._receipts.values(), key=lambda value: value.sequence
                )
            ],
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        jsonio.atomic_write_json(self.state_path, payload, mode=0o600)


def _model_residency_dict(
    residency: ModelResidency,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    payload = {
        "model_id": residency.model_id,
        "memory_mb": residency.memory_mb,
        "state": residency.state.value,
        "loaded_at": residency.loaded_at,
        "last_used_at": residency.last_used_at,
        "load_failures": residency.load_failures,
        "pinned": residency.pinned,
        "managed": residency.managed,
    }
    if now is not None:
        payload["loaded_age_seconds"] = min(
            MAX_MODEL_AGE_SECONDS,
            max(0.0, now - residency.loaded_at) if residency.loaded_at > 0 else 0.0,
        )
        payload["last_used_age_seconds"] = min(
            MAX_MODEL_AGE_SECONDS,
            max(0.0, now - residency.last_used_at)
            if residency.last_used_at > 0
            else 0.0,
        )
    return payload


def _cached_model_path(model_id: str) -> Path | None:
    return next(
        (Path(item.path) for item in model_store.list_all() if item.name == model_id),
        None,
    )


def _validated_api_key(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(character in "\r\n\0" for character in value)
    ):
        raise ValueError("engine API key must be a non-empty single-line string")
    return value


def _validated_runtime_file(
    value: str | os.PathLike[str] | None,
    label: str,
    *,
    owner_only: bool = False,
    allow_missing: bool = False,
) -> str:
    if value is None:
        return ""
    raw = os.fspath(value)
    if not raw or any(character in "\r\n\0" for character in raw):
        raise ValueError(f"{label} path must be non-empty and single-line")
    path = Path(raw).expanduser().resolve()
    if allow_missing and not path.exists():
        return str(path)
    if not path.is_file() or not os.access(path, os.R_OK):
        raise ValueError(f"{label} is not a readable regular file: {path}")
    if owner_only and os.name != "nt":
        metadata = path.stat()
        if metadata.st_mode & 0o077:
            raise ValueError(f"{label} must be owner-only (chmod 600): {path}")
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            raise ValueError(f"{label} must be owned by the current user: {path}")
    return str(path)


def _validate_ca_pem(value: str) -> None:
    try:
        size = len(value.encode("ascii"))
    except UnicodeEncodeError as exc:
        raise ValueError("TLS CA must be ASCII PEM") from exc
    if size > 65_536 or "-----BEGIN CERTIFICATE-----" not in value:
        raise ValueError("TLS CA must contain at most 64 KiB of PEM certificates")
    try:
        context = ssl.create_default_context()
        context.load_verify_locations(cadata=value)
    except (OSError, ssl.SSLError) as exc:
        raise ValueError("TLS CA PEM is invalid") from exc


def _validated_endpoint_host(value: str) -> str:
    host = str(value).strip()
    if not host or any(character in "\r\n/" for character in host):
        raise ValueError("endpoint host must be a hostname or IP address")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host


def _url_host(value: str) -> str:
    host = _validated_endpoint_host(value)
    decoded_host = host.replace("%25", "%")
    candidate = decoded_host.split("%", 1)[0]
    try:
        if ipaddress.ip_address(candidate).version == 6:
            return f"[{decoded_host.replace('%', '%25')}]"
    except ValueError:
        pass
    return host


def _backend_config(backend: ModelRuntimeBackend) -> dict[str, str]:
    return {
        "bind_host": str(getattr(backend, "bind_host", "")),
        "endpoint_host": str(getattr(backend, "endpoint_host", "")),
        "endpoint_scheme": str(getattr(backend, "endpoint_scheme", "http")),
        "tls_cert_file": str(getattr(backend, "tls_cert_file", "")),
        "tls_key_file": str(getattr(backend, "tls_key_file", "")),
        "tls_ca_file": str(getattr(backend, "tls_ca_file", "")),
        "tls_ca_pem": str(getattr(backend, "tls_ca_pem", "")),
    }


def _epoch_generation(value: str) -> tuple[str, int] | None:
    parts = value.split(":")
    if len(parts) != 3:
        return None
    epoch, sequence, digest = parts
    if (
        len(epoch) != 32
        or epoch != epoch.lower()
        or any(character not in "0123456789abcdef" for character in epoch)
        or len(sequence) != 20
        or not sequence.isdigit()
        or len(digest) != 12
        or digest != digest.lower()
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        return None
    return epoch, int(sequence)


def _generation_key(value: str) -> tuple[int, str]:
    prefix, separator, suffix = value.partition("-")
    if separator and prefix.isdigit():
        return int(prefix), suffix
    return 0, value


def _generation_is_older(candidate: str, latest: str) -> bool:
    if not latest:
        return False
    candidate_time, _ = _generation_key(candidate)
    latest_time, _ = _generation_key(latest)
    # Two distinct plans can legitimately be produced inside one millisecond.  Their digest
    # suffixes are identities, not an ordering relation, so equal timestamps are both current.
    return candidate_time < latest_time


def _newer_generation(left: str, right: str) -> str:
    if not left:
        return right
    left_time, _ = _generation_key(left)
    right_time, _ = _generation_key(right)
    return right if right_time >= left_time else left


def _port_available(port: int, *, bind_host: str = "127.0.0.1") -> bool:
    value = str(bind_host).strip().strip("[]")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((value, port))
        except OSError:
            return False
    return True


def _pid_alive(pid: int) -> bool:
    """Cross-platform liveness with an explicit negative-PID guard."""

    return pid > 0 and pid_alive(pid)


def _process_argv(pid: int) -> tuple[str, ...]:
    if not _pid_alive(pid):
        return ()
    proc_cmdline = Path(f"/proc/{pid}/cmdline")
    try:
        raw = proc_cmdline.read_bytes()
        return tuple(part.decode(errors="replace") for part in raw.split(b"\0") if part)
    except OSError:
        pass
    if sys.platform == "win32":
        return _windows_process_argv(pid)
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    if result.returncode != 0 or not result.stdout.strip():
        return ()
    try:
        return tuple(shlex.split(result.stdout.strip()))
    except ValueError:
        return ()


def _option_values(argv: tuple[str, ...], option: str) -> tuple[str, ...]:
    values: list[str] = []
    for index, argument in enumerate(argv):
        if argument != option or index + 1 >= len(argv):
            continue
        values.append(argv[index + 1])
    return tuple(values)


def _popen_executable(process: Any) -> str:
    args = getattr(process, "args", ())
    executable = args[0] if isinstance(args, (list, tuple)) and args else args
    return str(executable) if isinstance(executable, (str, os.PathLike)) else ""


def _process_birth_marker(pid: int) -> str:
    """Return a durable process-incarnation marker, or empty when proof is unavailable."""

    if not _pid_alive(pid):
        return ""
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            # The parenthesized command name may itself contain spaces. Fields following the last
            # close-paren start at Linux proc field 3; process start time is field 22.
            start_ticks = stat.rsplit(") ", 1)[1].split()[19]
            boot_id = (
                Path("/proc/sys/kernel/random/boot_id")
                .read_text(encoding="ascii")
                .strip()
            )
        except (IndexError, OSError):
            return ""
        return f"linux:{boot_id}:{start_ticks}"
    if sys.platform == "win32":
        return _windows_process_birth_marker(pid)
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    started = " ".join(result.stdout.split()) if result.returncode == 0 else ""
    return f"ps:{started}" if started else ""


def _windows_process_birth_marker(pid: int) -> str:
    try:
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        kernel32 = ctypes.windll.kernel32
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        get_process_times = kernel32.GetProcessTimes
        get_process_times.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        get_process_times.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        handle = open_process(process_query_limited_information, False, pid)
        if not handle:
            return ""
        try:
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            if not get_process_times(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return ""
            ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            return f"windows:{ticks}"
        finally:
            close_handle(handle)
    except (AttributeError, ImportError, OSError):
        return ""


def _windows_process_argv(pid: int) -> tuple[str, ...]:
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}').CommandLine",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    command = result.stdout.strip() if result.returncode == 0 else ""
    if not command:
        return ()
    try:
        import ctypes
        from ctypes import wintypes

        count = ctypes.c_int()
        command_line_to_argv = ctypes.windll.shell32.CommandLineToArgvW
        command_line_to_argv.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        command_line_to_argv.restype = ctypes.POINTER(wintypes.LPWSTR)
        pointer = command_line_to_argv(command, ctypes.byref(count))
        if not pointer:
            return ()
        try:
            return tuple(pointer[index] for index in range(count.value))
        finally:
            ctypes.windll.kernel32.LocalFree(pointer)
    except (AttributeError, ImportError, OSError, ValueError):
        return ()


def _terminate_owned_pid(
    pid: int,
    timeout: float = 10.0,
    *,
    identity_check: Callable[[], bool] | None = None,
) -> None:
    if stopped_running(pid):
        return
    if identity_check is not None and not identity_check():
        if stopped_running(pid):
            return
        raise RuntimeError("refusing to stop a process whose ownership changed")
    if sys.platform == "win32":
        result = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            check=False,
            capture_output=True,
        )
        if result.returncode != 0 or _pid_alive(pid):
            raise RuntimeError(f"allocator-owned process {pid} survived taskkill")
        return
    os.kill(pid, signal.SIGTERM)
    started = time.monotonic()
    deadline = started + timeout
    confirmation_budget = min(1.0, max(0.0, timeout / 3.0))
    graceful_deadline = deadline - confirmation_budget
    while _pid_alive(pid) and time.monotonic() < graceful_deadline:
        if identity_check is not None and not identity_check():
            if stopped_running(pid):
                return
            raise RuntimeError("process ownership changed during allocator shutdown")
        time.sleep(0.05)
    if _pid_alive(pid):
        if stopped_running(pid):
            return
        if identity_check is not None and not identity_check():
            if stopped_running(pid):
                return
            raise RuntimeError("process ownership changed before allocator escalation")
        os.kill(pid, signal.SIGKILL)
        next_zombie_probe = time.monotonic()
        while _pid_alive(pid) and time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_zombie_probe:
                if stopped_running(pid):
                    return
                next_zombie_probe = now + 0.25
            time.sleep(0.01)
        if _pid_alive(pid):
            if stopped_running(pid):
                return
            raise RuntimeError(f"allocator-owned process {pid} survived SIGKILL")
