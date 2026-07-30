from __future__ import annotations

import ipaddress
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlparse, urlsplit

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


def advertised_address_works(url: str, timeout: float = 3.0) -> bool:
    """Can anything actually open a connection to the address we are about to hand out?

    ``detect_local_ip`` asks the routing table which interface reaches the internet, which is the
    right guess on an ordinary network and the wrong one on a machine holding a VPN: the interface
    that reaches the internet is not the interface other computers reach *you* on. The symptom is
    brutal for a newcomer — the grid is running perfectly, every command fails with "Server
    disconnected without sending a response", and nothing points at the address as the culprit.

    A real HTTP request, not a bare TCP connect: measured on a VPN'd machine, the TCP handshake
    SUCCEEDS and the connection is then dropped without a reply — which is the whole reason the
    error a user sees is "Server disconnected without sending a response". A connect-only probe
    reports that address as healthy. Any HTTP status at all counts as reachable; we care that
    something answered, not what it said.
    """
    parsed = urlparse(url if "//" in url else f"http://{url}")
    if not parsed.hostname or not parsed.port:
        return True  # nothing to check — do not invent a warning
    try:
        httpx.get(f"{parsed.scheme}://{parsed.hostname}:{parsed.port}/", timeout=timeout)
        return True
    except httpx.HTTPError:
        return False


def normalize_url(value: str) -> str:
    url = value.strip().rstrip("/")
    if not url:
        raise SystemExit("URL must not be empty.")
    if not url.startswith(("http://", "https://")):
        url = f"http://{url}"
    return url.rstrip("/")


# The grid config carries the same identity a run record does — `pid` plus the `(start_time, pgid)`
# that make it verifiable — under `server_`-prefixed keys. Prefixed rather than nested under one
# `server_identity` key because the config already names the pid as `server_pid`, and a nested copy
# would put two pids on disk for a hand-edit or a partial write to disagree about.
_IDENTITY_PREFIX = "server_"


def _server_identity(cfg: dict[str, Any]) -> dict[str, Any]:
    """The grid server's identity in the shape ``shared.run_records`` speaks.

    The key list comes from ``run_records.IDENTITY_FIELDS`` because this is the *reader*: it is
    building the very dict a writer would derive those names from. A ``startswith(_IDENTITY_PREFIX)``
    sweep instead would silently fold any future unrelated ``server_*`` config key into a record the
    teardown then signals on.
    """
    return {field: cfg.get(f"{_IDENTITY_PREFIX}{field}") for field in run_records.IDENTITY_FIELDS}


def _stamp_server(pid: int) -> dict[str, Any]:
    """``run_records.identity_stamp`` under this config's key prefix — the writer's half of
    ``_server_identity``. Merge it, never write one field without the others: a verified pid is what
    vouches for the ``pgid`` beside it, and they are only trustworthy together because they land in
    the same atomic config write. Derived from ``identity_stamp``'s own dict rather than spelled out,
    so a fourth identity field reaches the grid config for free.
    """
    return {
        f"{_IDENTITY_PREFIX}{field}": value
        for field, value in run_records.identity_stamp(pid).items()
    }


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
        # surface. It authorizes model-placement mutations on the local control plane.
        "allocator_control_token": secrets.token_urlsafe(32),
        # `_stamp_server(0)` rather than a bare `server_pid: 0`: pid 0 names no process, so the two
        # fields beside it are `None` — and writing the identity as a unit here is what keeps "never
        # one without the others" true of the config's very first write too.
        **_stamp_server(0),
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

    # Persist the upgrade before an early return for an already-running pre-allocator grid.
    ensure_allocator_control_token(cfg)

    # `record_alive`, not a bare pid check: it refuses the shapes a hand-edited or corrupt config can
    # hold (`int("abc")` used to raise here, and an out-of-range value reached `os.kill` and raised
    # OverflowError), and it answers "not alive" for a zombie or a recycled pid — both of which used
    # to read as a live server and cost a 3s health wait before falling through anyway.
    identity = _server_identity(cfg)
    _note_unusable_pid(cfg, identity)  # `grid up` overwrites it below; say so before the evidence goes
    if run_records.record_alive(identity):
        try:
            wait_for_health(cfg, timeout=3)
            return run_records.recorded_pid(identity) or 0
        except SystemExit:
            # Main permits a tokenless legacy record so upgrades can still stop it. For start,
            # however, a live pid that cannot be tied to this grid and does not answer as this grid
            # must not be silently replaced: keep the only evidence and ask for manual inspection.
            if run_records.record_verdict(identity) is run_records.RecordVerdict.LIVE_UNVERIFIED:
                raise SystemExit(
                    f"Refusing to start {cfg['name']}: its live recorded server PID cannot be "
                    "proven to belong to this Grid instance. Stop it manually, then retry."
                ) from None

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
    # Persist both main's general process identity and the allocator branch's command nonce. The
    # former remains the authority for teardown; the latter strengthens diagnostics and compatibility
    # with allocator-era run records.
    try:
        cfg.update(_stamp_server(proc.pid))
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


def _grid_label(cfg: dict[str, Any]) -> str:
    return str(cfg.get("name") or cfg.get("grid_id"))


def _note_unusable_pid(cfg: dict[str, Any], identity: dict[str, Any]) -> None:
    """Say when the config's ``server_pid`` is not a process id at all.

    Unusable shapes (a string, a negative, out of range, a list) signal nothing, which is correct and
    was completely silent — so `grid down` reported success having done nothing, and `grid up`
    silently overwrote the damaged value with a fresh stamp, destroying the only evidence. ``0``/absent
    is NOT this case: that is the ordinary never-started or already-stopped config, and it must stay
    quiet on both commands.
    """
    if run_records.recorded_pid(identity) is None:
        print(
            f"Note: the recorded server_pid for {_grid_label(cfg)} ({cfg.get('server_pid')!r}) is "
            "not a process id, so nothing was signalled.",
            file=sys.stderr,
        )


def _terminate_server(cfg: dict[str, Any], identity: dict[str, Any]) -> run_records.Teardown:
    """Stop the recorded server, treating a process we may not signal as *not ours to stop*.

    ``pid_alive`` reports EPERM as **alive** (ADR 0024: calling another user's process dead would let
    a teardown claim it reaped something still running), so a drifted ``server_pid`` can reach
    ``terminate_pid`` and raise ``PermissionError`` — which, uncaught, is a raw traceback out of
    `grid down`. Caught here and deliberately **not** inside ``terminate_pid``:
    ``orphan_sweep.terminate`` depends on that exception escaping to classify a swept match as
    ``foreign`` rather than reaped, so swallowing it there would blind both modes' `grid leave` to
    another user's serve child.

    Nothing is concluded from it — ``verified=False`` leaves the verdict to the port probe, which is
    the right authority either way: if that pid really is this grid's server under another account
    the port still answers and the command fails loud, and if the config had merely drifted onto a
    stranger the port is silent and the grid is genuinely down.
    """
    # Allocator-era servers carry a second, argv-bound nonce identity. If either half of that
    # identity remains on disk, require the complete identity to match before the general mainline
    # teardown gets any opportunity to signal the pid.
    if cfg.get("server_instance_id") or cfg.get("server_start_marker"):
        pid = run_records.recorded_pid(identity) or 0
        if pid and _server_process_state(cfg) != "owned":
            raise SystemExit(
                f"Refusing to signal PID {pid}: its Grid server ownership cannot be proven."
            )
    try:
        return run_records.terminate_recorded(identity)
    except PermissionError:
        print(
            f"Note: the recorded server pid for {_grid_label(cfg)} "
            f"({run_records.recorded_pid(identity)}) belongs to another user; left it alone.",
            file=sys.stderr,
        )
        return run_records.Teardown(verified=False)


class StopOutcome(NamedTuple):
    """What one `grid down` established — and they are two different questions.

    ``teardown`` says what happened to the process the config *named*; ``serving`` says whether the
    grid is still answering on its own port. Neither alone is the answer: a teardown can be
    unverifiable (a recycled pid, a config that never named anything) while the port proves the grid
    is gone, and a teardown can look clean while something is still serving.
    """

    teardown: run_records.Teardown
    serving: bool | None
    """``True`` = this grid still answers on its port; ``False`` = nothing is listening there;
    ``None`` = we could not tell. Deliberately tri-state: a refused connection is *proof* the grid is
    down, and a probe that could not run must never be laundered into that proof."""

    def stopped(self) -> bool:
        """Whether this box is no longer serving the grid — the ONE rule, read by both the config
        write and the message, so they can never disagree about what happened."""
        if self.teardown.survivor:
            return False                   # something of ours outlived even SIGKILL
        if self.serving is not None:
            return not self.serving        # the grid's own port answered the question outright
        return self.teardown.verified      # nothing to probe with — fall back to what we proved


def stop_grid(cfg: dict[str, Any]) -> StopOutcome:
    """Stop this grid's server, then ask the grid's own port whether that worked.

    The identity decides what may be **signalled** (a recycled `server_pid` names a stranger's
    process group); the port probe decides whether the command **succeeded**, and is also what lets an
    unprovable pid converge — without it, a config that can never be verified would fail forever.
    The identity is cleared only on success, so a retry always keeps whatever handle exists.
    """
    identity = _server_identity(cfg)
    _note_unusable_pid(cfg, identity)
    outcome = StopOutcome(_terminate_server(cfg, identity), _still_serving(cfg))
    if outcome.stopped():
        # As a unit, like every other identity write: a `server_pid` of 0 beside a live grid's stale
        # start-time token would be a record half-cleared, and `recorded_pgid` would still hand the
        # next teardown a group id to signal.
        cfg.update(_stamp_server(0))
        _clear_server_identity(cfg)
        cfg["updated_at"] = utc_now()
        config.save_grid_config(cfg["grid_id"], cfg)
    return outcome


# A grid server binds `cfg["host"]`, and a wildcard bind is reachable on loopback. Anything else has
# to be addressed as itself: `grid up --host 10.0.0.5` really does bind only that address, so probing
# 127.0.0.1 would report a perfectly healthy grid as unreachable — a 30s timeout in `wait_for_health`,
# and in `stop_grid` something worse, since "nothing answers" is promoted there to *proof* the grid
# stopped.
_LOOPBACK = "127.0.0.1"
_WILDCARD_HOSTS = frozenset({"", "0.0.0.0", "::", "*"})
_PROBE_TIMEOUT_SECONDS = 1

# A bind address is a hostname or an IP literal, and nothing else. A value carrying URL syntax is not
# one — and every such value silently *retargets the request*: `evil.com/x` becomes host `evil.com`
# with the configured port parsed as part of the path (so the probe reaches port 80), and
# `a@evil.com` hides the real host behind userinfo. Refusing them costs nothing, because no address a
# server can bind contains one, and it gives a mangled `host` the same honest "we never asked"
# treatment an unusable `port` already gets instead of a confidently wrong answer about a machine we
# never meant to contact.
_HOST_FORBIDDEN = frozenset("/@?#\\'\" \t\r\n")

# How much of a foreign grid's self-reported name may reach the terminal. It comes off the wire from
# whatever holds our port, so it is neither bounded nor trusted; `orphan_sweep._first_line` truncates
# another tool's output for the same reason.
_FOREIGN_NAME_CHARS = 80


def _probe_target(cfg: dict[str, Any]) -> tuple[str, int] | None:
    """``(host, port)`` this grid's server answers on, or ``None`` when the config cannot say.

    Tolerant of the config it reads, because both callers are commands that must not die of a
    hand-edited file — and `stop_grid` in particular promises it no longer tracebacks on one, a
    promise a bare ``int(cfg["port"])`` here would have quietly broken.
    """
    try:
        port = int(cfg["port"])
    except (KeyError, TypeError, ValueError):
        return None
    if not 0 < port < 65536:
        return None
    host = cfg.get("host")
    if host is not None and not isinstance(host, str):
        return None  # a list/int/dict is not a bind address; `str()` would coerce it into a hostname
    # Brackets come off before anything else looks at the value: `[::1]` is the same address as
    # `::1`, and re-bracketing an already-bracketed literal produced `http://[[::1]]:8090`, which is
    # unparseable — so a legitimately bracketed config silently never probed at all.
    host = (host or "").strip().strip("[]")
    if host in _WILDCARD_HOSTS:
        host = _LOOPBACK  # a wildcard bind is reachable on loopback
    if not host or _HOST_FORBIDDEN & set(host):
        return None
    return host, port


def probe_url(cfg: dict[str, Any]) -> str | None:
    """Where this grid's server answers, as a URL, or ``None`` when the config cannot say."""
    target = _probe_target(cfg)
    if target is None:
        return None
    host, port = target
    return f"http://{f'[{host}]' if ':' in host else host}:{port}"


def _nothing_is_listening(host: str, port: int) -> bool:
    """Whether a TCP connect to ``host:port`` is **refused** — the only outcome that proves nothing is
    serving there.

    Asked at the socket rather than read off ``httpx.ConnectError``, which is not the question it
    looks like. ``socket.create_connection`` resolves DNS *and* connects in one call, and
    ``socket.gaierror``, ``ENETUNREACH`` and ``EHOSTUNREACH`` are all plain ``OSError``s — so httpcore
    maps every one of them to the same ``ConnectError`` a genuine refusal produces. Treating that as
    proof is the laundering this whole probe exists to prevent, and it is not hypothetical: a laptop
    that roams networks between `grid up --host <lan-ip>` and `grid down` would have its live server
    reported as stopped and its recorded pid — the only handle a retry has — thrown away.

    ``ConnectionRefusedError`` is the precise builtin (PEP 3151 gives every errno its own subclass),
    so this needs no knowledge of httpx's or httpcore's exception wrapping, which is where the
    distinction was lost.
    """
    try:
        with socket.create_connection((host, port), timeout=_PROBE_TIMEOUT_SECONDS):
            return False  # something accepted — there is a server there, whoever it belongs to
    except ConnectionRefusedError:
        return True
    except OSError:
        return False  # DNS failure, no route, timeout: we asked and did not find out


def _still_serving(cfg: dict[str, Any]) -> bool | None:
    """Whether this grid is still answering on its own port: ``True`` yes, ``False`` nothing of ours
    is there, ``None`` we could not tell.

    Two probes, because they answer different halves. The socket says whether *anything* is listening
    — and only a refusal is proof of absence. The HTTP call then says whether what is listening is
    **this grid**. A timeout, a torn-up config, or a reply that is not a `/grid/info` all mean we did
    not find out, and none may be laundered into "the grid stopped": that verdict clears the recorded
    identity and exits 0.
    """
    target = _probe_target(cfg)
    if target is None:
        return None
    if _nothing_is_listening(*target):
        return False
    url = probe_url(cfg)
    try:
        resp = httpx.get(f"{url}/grid/info", timeout=_PROBE_TIMEOUT_SECONDS)
        body = resp.json() if resp.status_code == 200 else None
    except Exception as exc:
        # Something is listening but would not tell us what it is. Said out loud rather than degraded
        # in silence: it is the difference between the two failing branches of `grid down`, and the
        # operator is about to be told this command established nothing.
        # `!r` on the url as well as the exception: it is built from the config's `host`, so it is
        # config-controlled text reaching the terminal, and a raw control/ANSI sequence in it would be
        # interpreted rather than shown. Pre-diff this field only ever reached a `bind()`.
        print(f"Note: could not ask {url!r} which grid it is ({exc!r}).", file=sys.stderr)
        return None
    if not isinstance(body, dict) or not body.get("grid_id"):
        return None  # answering, but not as a grid — not evidence either way
    if body["grid_id"] == cfg.get("grid_id"):
        return True
    # `!r` and a length cap: this name is whatever the process holding our port chose to send, so it
    # is untrusted terminal output — raw escape sequences would be interpreted by the operator's
    # terminal, and there is no bound on the wire.
    print(
        f"Note: {url!r} is answering for a different grid "
        f"({str(body['grid_id'])[:_FOREIGN_NAME_CHARS]!r}); "
        f"{_grid_label(cfg)} is not serving there.",
        file=sys.stderr,
    )
    return False


def wait_for_health(cfg: dict[str, Any], timeout: int = 30) -> None:
    deadline = time.time() + timeout
    probe = probe_url(cfg)
    if probe is None:
        # Never say "did not become healthy" about a server we never asked: the config's own address
        # is unusable, which is a different problem with a different fix.
        raise SystemExit(
            f"Cannot reach the local signaling server: {cfg['name']}'s configured address "
            f"(host {cfg.get('host')!r}, port {cfg.get('port')!r}) is not one this can probe."
        )
    url = f"{probe}/grid/info"
    while time.time() < deadline:
        try:
            resp = httpx.get(url, timeout=_PROBE_TIMEOUT_SECONDS)
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
        raise RuntimeError(f"signaling server process tree {proc.pid} survived startup cleanup")


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


def port_in_use(port: int) -> bool:
    """Whether something is already listening on [port] locally.

    Delegates rather than reimplementing: `launcher.is_port_in_use` was already the engine's own
    check, and a second copy here meant the two could answer differently — and meant a test that
    stubbed one still reached the real socket through the other, making it depend on whatever
    happened to be listening on the machine running it. One definition, one stub point.

    Imported inside the call so the attribute is looked up per call: a test that patches
    `launcher.is_port_in_use` must reach this path too.
    """
    from shared.engine import launcher

    return launcher.is_port_in_use(int(port))


def free_port_from(start: int, attempts: int = 40) -> int | None:
    """The first free port at or after [start], so a busy default never dead-ends the first run."""
    for candidate in range(int(start), int(start) + attempts):
        if not port_in_use(candidate):
            return candidate
    return None


def lan_ip_candidates() -> list[str]:
    """Private IPv4 addresses on this machine, best guess first.

    Needed because the one address `detect_local_ip` finds can be a VPN's, and telling someone to
    substitute "<this machine's LAN IP>" is asking them to go and find a thing we are already
    standing on. `getaddrinfo` is not enough — on a VPN'd Mac it returns only the VPN address — so
    read the interfaces. Best-effort: an unparsable or missing tool yields an empty list and the
    caller falls back to describing the value instead of naming it.
    """
    for cmd in (["ip", "-4", "-o", "addr"], ["ifconfig"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        found = []
        for match in re.finditer(r"inet\s+(?:addr:)?(\d+\.\d+\.\d+\.\d+)", out.stdout or ""):
            address = match.group(1)
            try:
                parsed = ipaddress.ip_address(address)
            except ValueError:
                continue
            # `.0` hosts are network addresses that some VPN clients park on an interface; they are
            # never dialable, so offering one would send the reader straight back here.
            if parsed.is_private and not parsed.is_loopback and not address.endswith(".0"):
                found.append(address)
        if found:
            # 192.168.* before 10.* / 172.16-31.*: home routers hand these out, so on the machines
            # this message is written for it is the one a second computer can actually reach.
            return sorted(set(found), key=lambda a: (not a.startswith("192.168."), a))
    return []


def port_holder(port: int, timeout: float = 3.0) -> str | None:
    """Who is holding [port], as ``'name (pid 4711)'`` — or ``None`` when we cannot tell.

    "Port 8090 is already in use" leaves the reader to discover `lsof` before they can act. Naming
    the process turns it into something they can decide about, and very often the answer is a grid
    from an earlier run that they simply need to stop. Best-effort everywhere: no `lsof`, a refusal,
    or an unparsable answer all mean we say less rather than guess.
    """
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{int(port)}", "-sTCP:LISTEN", "-F", "cn"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    pid, name = None, None
    for line in (out.stdout or "").splitlines():
        if line.startswith("p"):
            pid = line[1:].strip()
        elif line.startswith("c") and name is None:
            name = line[1:].strip()
    if not name:
        return None
    return f"{name} (pid {pid})" if pid else name


def cli_command() -> list[str]:
    """The argv prefix that re-invokes this CLI (for detached subprocesses)."""
    return _cli_subprocess_command()


# What `CreateProcess` can actually launch on Windows. NOT `PATHEXT`, which routinely carries
# `.PY`/`.VBS`/`.JS` — those run through a shell association, so CreateProcess refuses them with
# WinError 193 and only the four below are safe to hand to `subprocess`.
_WINDOWS_EXEC_SUFFIXES = (".bat", ".cmd", ".com", ".exe")


def _is_executable(path: Path) -> bool:
    """Whether `path` is something a subprocess can be spawned from.

    Windows has no execute bit, so `os.access(path, os.X_OK)` there is only an existence check and
    answers True for **any** file — including the `cli/__main__.py` that `[sys.executable, "-m",
    "cli"]` leaves in a child's `sys.argv[0]`. A child re-deriving this command handed that `.py`
    straight to `CreateProcess` and died with `[WinError 193] %1 is not a valid Win32 application`,
    which is what took every `grid join --api claude` seat down on Windows: the seat server never
    started, `remote/serve.py` reported the OSError, and the run record was reaped — so the join
    printed "starting", exited 0, and the engine was gone a second later.

    POSIX keeps the execute-bit test unchanged; the suffix rule is Windows-only.
    """
    if not path.is_file():
        return False
    if os.name == "nt":
        return path.suffix.lower() in _WINDOWS_EXEC_SUFFIXES
    return os.access(path, os.X_OK)


def _cli_subprocess_command() -> list[str]:
    argv0 = sys.argv[0] if sys.argv else ""
    candidates: list[Path] = []
    if argv0:
        base = Path(argv0).expanduser()
        candidates.append(base)
        if os.name == "nt":
            # A console script's argv[0] reaches us WITHOUT its extension (`…\bin\grid`, measured on
            # the uv-installed launcher), and `shutil.which` adds no PATHEXT to a name that already
            # carries a directory component before 3.12 — so both lookups miss the real `grid.exe`
            # and the interpreter fallback below was taken even where a launcher exists.
            candidates += [base.with_name(base.name + ext) for ext in _WINDOWS_EXEC_SUFFIXES]
        resolved = shutil.which(argv0)
        if resolved:
            candidates.append(Path(resolved))
    for candidate in candidates:
        if _is_executable(candidate):
            return [str(candidate.resolve())]
    return [sys.executable, "-m", "cli"]
