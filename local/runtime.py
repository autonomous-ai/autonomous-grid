from __future__ import annotations

import ipaddress
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from local import config
from shared import logging_setup, paths, run_records
from shared.filelock import file_lock

GRID_TYPE = "lan-permissionless"
DEFAULT_PORT = 8090
DEFAULT_HOST = "0.0.0.0"
ALLOCATOR_STATE_FILE = "allocator.json"


def slug_name(name: str) -> str:
    clean = "".join(ch.lower() if ch.isalnum() else "-" for ch in name).strip("-")
    return clean or f"grid-{uuid.uuid4().hex[:8]}"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def detect_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
    except OSError:
        # An isolated intranet may have no default route. Hostname resolution can still expose
        # an address assigned to a real interface without depending on public connectivity.
        try:
            addresses = socket.getaddrinfo(
                socket.gethostname(),
                None,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_DGRAM,
            )
        except socket.gaierror:
            addresses = []
        for _family, _socktype, _proto, _canonname, sockaddr in addresses:
            candidate = str(sockaddr[0]).strip()
            if candidate and not _is_loopback_address(candidate):
                return candidate
        return "127.0.0.1"


def detect_local_ip_for_url(controller_url: str) -> str:
    """Return the source address the OS would use to reach ``controller_url``.

    A UDP ``connect`` performs only a route lookup; it sends no packet.  Unlike the historical
    public-DNS probe, this works on an air-gapped intranet and picks the right interface on a
    multi-homed host.  Advertising loopback to a remote controller would publish a dead engine
    route, so that case fails closed and asks the operator for ``--advertise-host``.
    """

    parsed = urlsplit(normalize_url(controller_url))
    controller_host = parsed.hostname
    if not controller_host:
        raise SystemExit("Grid URL must include a host.")
    controller_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        destinations = socket.getaddrinfo(
            controller_host,
            controller_port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_DGRAM,
        )
    except socket.gaierror as exc:
        if controller_host.lower() == "localhost" or _is_loopback_address(controller_host):
            return "127.0.0.1"
        raise SystemExit(
            f"Cannot resolve a route to Grid controller {controller_host!r}; "
            "pass --advertise-host with this node's reachable intranet address."
        ) from exc

    for family, socktype, proto, _canonname, sockaddr in destinations:
        try:
            with socket.socket(family, socktype, proto) as sock:
                sock.connect(sockaddr)
                local_host = str(sock.getsockname()[0]).strip()
        except OSError:
            continue
        if not local_host:
            continue
        destination_host = str(sockaddr[0])
        if _is_loopback_address(local_host) and not _is_loopback_address(destination_host):
            continue
        return local_host

    if controller_host.lower() == "localhost" or _is_loopback_address(controller_host):
        return "127.0.0.1"
    raise SystemExit(
        f"Cannot determine a reachable local address for Grid controller {controller_host!r}; "
        "pass --advertise-host with this node's intranet address."
    )


def url_host(host: str) -> str:
    """Format a hostname or IP literal for interpolation into an HTTP authority."""

    value = str(host).strip()
    if not value:
        raise SystemExit("Advertise host must not be empty.")
    if value.startswith("[") and value.endswith("]"):
        return value
    address_part = value.split("%", 1)[0]
    try:
        address = ipaddress.ip_address(address_part)
    except ValueError:
        return value
    if address.version == 6:
        # RFC 6874 requires the zone delimiter to be percent-encoded inside a URL authority.
        return f"[{value.replace('%', '%25')}]"
    return value


def _is_loopback_address(host: str) -> bool:
    value = str(host).strip().strip("[]").split("%", 1)[0]
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return value.lower() == "localhost"


def make_local_url(port: int, advertise_host: str | None = None) -> str:
    host = url_host(advertise_host or detect_local_ip())
    return f"http://{host}:{int(port)}"


def normalize_url(value: str) -> str:
    url = value.strip().rstrip("/")
    if not url:
        raise SystemExit("URL must not be empty.")
    if not url.startswith(("http://", "https://")):
        url = f"http://{url}"
    return url.rstrip("/")


def init_grid_config(
    *,
    name: str,
    port: int = DEFAULT_PORT,
    host: str = DEFAULT_HOST,
    grid_id: str | None = None,
    advertise_host: str | None = None,
) -> dict[str, Any]:
    grid_id = grid_id or f"ag-{slug_name(name)}-{uuid.uuid4().hex[:8]}"
    data = {
        "grid_id": grid_id,
        "name": name,
        "grid_type": GRID_TYPE,
        "managed_server": True,
        "host": host,
        "port": int(port),
        "lan_signaling_url": make_local_url(port, advertise_host),
        # This capability is intentionally separate from the permissionless inference/discovery
        # surface.  It authorizes model placement mutations on the local control plane and must
        # never be returned by a public endpoint.
        "allocator_control_token": secrets.token_urlsafe(32),
        "server_pid": 0,
        "server_instance_id": "",
        "server_start_marker": "",
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    config.save_grid_config(grid_id, data)
    return data


def ensure_allocator_control_token(cfg: dict[str, Any]) -> str:
    """Return this grid's durable allocator capability, minting it for old configs.

    Configs created before the allocator existed have no token.  Upgrade them in place exactly
    when a managed server is started, rather than leaving its mutation API permanently disabled.
    """
    existing = cfg.get("allocator_control_token")
    if isinstance(existing, str) and existing:
        return existing
    token = secrets.token_urlsafe(32)
    cfg["allocator_control_token"] = token
    cfg["updated_at"] = utc_now()
    config.save_grid_config(str(cfg["grid_id"]), cfg)
    return token


def start_grid(cfg: dict[str, Any]) -> int:
    grid_id = str(cfg["grid_id"])
    with file_lock(paths.grid_dir(grid_id) / "server-lifecycle"):
        persisted = config.load_grid_config(grid_id)
        if persisted:
            cfg.clear()
            cfg.update(persisted)
        return _start_grid_locked(cfg)


def _start_grid_locked(cfg: dict[str, Any]) -> int:
    if not cfg.get("managed_server", True):
        raise SystemExit(f"{cfg['name']} is a remote signaling URL; there is no local server to start.")

    # Persist the upgrade before any early return for an already-running server.  A newly spawned
    # server then reads the same stable token; an existing server picks it up on its next restart.
    ensure_allocator_control_token(cfg)

    pid = int(cfg.get("server_pid") or 0)
    process_state = _server_process_state(cfg)
    if process_state == "owned":
        wait_for_health(cfg, timeout=3)
        return pid
    if process_state == "ambiguous":
        raise SystemExit(
            f"Refusing to start {cfg['name']}: live server PID {pid} cannot be proven to belong "
            "to this Grid instance. Stop it manually, then retry."
        )
    if pid:
        _clear_server_identity(cfg)
        config.save_grid_config(cfg["grid_id"], cfg)

    port = int(cfg["port"])
    if _tcp_port_in_use("127.0.0.1", port):
        raise SystemExit(f"Port {port} is already in use. Choose a different --port.")

    # The rotating handler inside the __server child owns server.log; this raw redirect captures
    # only bootstrap/crash output (stays tiny — the server has no print()), capped on each start.
    log_path = paths.grid_dir(cfg["grid_id"]) / "server.err"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = logging_setup.cap_and_open_append(log_path, logging_setup.ERR_LOG_MAX_BYTES)
    instance_id = uuid.uuid4().hex
    command = _cli_subprocess_command() + [
        "__server",
        cfg["grid_id"],
        "--instance-id",
        instance_id,
    ]
    try:
        proc = subprocess.Popen(
            command,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log.close()
    # Persist the direct Popen identity before doing any slower OS introspection. If this process
    # crashes now, a later invocation sees an ambiguous live PID and refuses to signal it.
    try:
        cfg["server_pid"] = proc.pid
        cfg["server_instance_id"] = instance_id
        cfg["server_start_marker"] = ""
        cfg["updated_at"] = utc_now()
        config.save_grid_config(cfg["grid_id"], cfg)
        marker = _capture_process_start_marker(proc.pid)
        if marker is None:
            raise RuntimeError("could not capture signaling server process birth marker")
        cfg["server_start_marker"] = marker
        cfg["updated_at"] = utc_now()
        config.save_grid_config(cfg["grid_id"], cfg)
    except BaseException:
        _terminate_spawned_server(proc)
        _clear_server_identity(cfg)
        cfg["updated_at"] = utc_now()
        try:
            config.save_grid_config(cfg["grid_id"], cfg)
        except (OSError, SystemExit):
            pass
        raise
    wait_for_health(cfg)
    return proc.pid


def stop_grid(cfg: dict[str, Any]) -> None:
    grid_id = str(cfg["grid_id"])
    with file_lock(paths.grid_dir(grid_id) / "server-lifecycle"):
        persisted = config.load_grid_config(grid_id)
        if persisted:
            cfg.clear()
            cfg.update(persisted)
        _stop_grid_locked(cfg)


def _stop_grid_locked(cfg: dict[str, Any]) -> None:
    pid = int(cfg.get("server_pid") or 0)
    if not pid:
        return
    process_state = _server_process_state(cfg)
    if process_state == "ambiguous":
        raise SystemExit(
            f"Refusing to signal PID {pid}: its Grid server ownership cannot be proven."
        )
    if process_state == "owned" and not run_records.terminate_pid(
        pid,
        identity_check=lambda: _server_process_state(cfg) == "owned",
    ):
        raise SystemExit(f"Grid signaling server PID {pid} did not stop.")
    _clear_server_identity(cfg)
    cfg["updated_at"] = utc_now()
    config.save_grid_config(cfg["grid_id"], cfg)


def _terminate_spawned_server(proc: subprocess.Popen[Any]) -> None:
    """Contain the exact detached server tree while its authoritative Popen is available."""

    if proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5.0)
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        run_records.kill_group(proc.pid)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.wait(timeout=5.0)
    except (OSError, subprocess.SubprocessError):
        pass
    if proc.poll() is None:
        raise RuntimeError(
            f"signaling server process tree {proc.pid} survived startup cleanup"
        )


def wait_for_health(cfg: dict[str, Any], timeout: int = 30) -> None:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{int(cfg['port'])}/grid/info"
    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=1, trust_env=False) as client:
                resp = client.get(url)
            if resp.status_code == 200:
                payload = resp.json()
                if isinstance(payload, dict) and payload.get("grid_id") == cfg["grid_id"]:
                    return
        except (httpx.HTTPError, OSError, ValueError):
            pass
        time.sleep(0.25)
    grid_dir = paths.grid_dir(cfg["grid_id"])
    raise SystemExit(
        "local signaling server did not become healthy. "
        f"See {grid_dir / 'server.log'} (and {grid_dir / 'server.err'} for bootstrap/crash output)"
    )


def grid_url(cfg: dict[str, Any]) -> str:
    return str(cfg["lan_signaling_url"]).rstrip("/")


def allocator_control_url(cfg: dict[str, Any]) -> str:
    """Use loopback for secrets when the signaling server is owned by this machine."""

    if cfg.get("managed_server", True):
        return f"http://127.0.0.1:{int(cfg['port'])}"
    return grid_url(cfg)


def engine_endpoint_url(endpoint_url: str | None, port: int, advertise_host: str | None = None) -> str:
    if endpoint_url:
        return normalize_url(endpoint_url)
    return f"{make_local_url(port, advertise_host)}/v1"


def _server_process_state(cfg: dict[str, Any]) -> str:
    """Return ``dead``, ``owned``, or ``ambiguous`` for the persisted signaling child."""

    pid = int(cfg.get("server_pid") or 0)
    if not run_records.pid_alive(pid):
        return "dead"
    instance_id = str(cfg.get("server_instance_id") or "").strip()
    start_marker = str(cfg.get("server_start_marker") or "").strip()
    grid_id = str(cfg.get("grid_id") or "").strip()
    if not (instance_id and start_marker and grid_id):
        return "ambiguous"
    if run_records.process_matches(
        pid,
        required_args=("__server", grid_id, "--instance-id", instance_id),
        start_marker=start_marker,
    ):
        return "owned"
    return "ambiguous"


def _capture_process_start_marker(pid: int, timeout: float = 3.0) -> str | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        marker = run_records.process_start_marker(pid)
        if marker:
            return marker
        if not run_records.pid_alive(pid):
            return None
        time.sleep(0.05)
    return None


def _clear_server_identity(cfg: dict[str, Any]) -> None:
    cfg["server_pid"] = 0
    cfg["server_instance_id"] = ""
    cfg["server_start_marker"] = ""


def _tcp_port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) == 0


def cli_command() -> list[str]:
    """The argv prefix that re-invokes this CLI (for detached subprocesses)."""
    return _cli_subprocess_command()


def _cli_subprocess_command() -> list[str]:
    argv0 = sys.argv[0] if sys.argv else ""
    candidates: list[Path] = []
    if argv0:
        candidates.append(Path(argv0).expanduser())
        resolved = shutil.which(argv0)
        if resolved:
            candidates.append(Path(resolved))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return [str(candidate.resolve())]
    return [sys.executable, "-m", "cli"]
