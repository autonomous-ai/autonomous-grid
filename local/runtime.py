from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

import httpx

from local import config
from shared import logging_setup, paths, run_records


GRID_TYPE = "lan-permissionless"
DEFAULT_PORT = 8090
DEFAULT_HOST = "0.0.0.0"


def slug_name(name: str) -> str:
    clean = "".join(ch.lower() if ch.isalnum() else "-" for ch in name).strip("-")
    return clean or f"grid-{uuid.uuid4().hex[:8]}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def detect_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"


def make_local_url(port: int, advertise_host: str | None = None) -> str:
    host = (advertise_host or detect_local_ip()).strip()
    return f"http://{host}:{int(port)}"


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
        # `_stamp_server(0)` rather than a bare `server_pid: 0`: pid 0 names no process, so the two
        # fields beside it are `None` — and writing the identity as a unit here is what keeps "never
        # one without the others" true of the config's very first write too.
        **_stamp_server(0),
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    config.save_grid_config(grid_id, data)
    return data


def start_grid(cfg: dict[str, Any]) -> int:
    if not cfg.get("managed_server", True):
        raise SystemExit(f"{cfg['name']} is a remote signaling URL; there is no local server to start.")

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
            pass

    port = int(cfg["port"])
    if _tcp_port_in_use("127.0.0.1", port):
        raise SystemExit(f"Port {port} is already in use. Choose a different --port.")

    # The rotating handler inside the __server child owns server.log; this raw redirect captures
    # only bootstrap/crash output (stays tiny — the server has no print()), capped on each start.
    log_path = paths.grid_dir(cfg["grid_id"]) / "server.err"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = logging_setup.cap_and_open_append(log_path, logging_setup.ERR_LOG_MAX_BYTES)
    proc = subprocess.Popen(
        _cli_subprocess_command() + ["__server", cfg["grid_id"]],
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    cfg.update(_stamp_server(proc.pid))
    cfg["updated_at"] = utc_now()
    config.save_grid_config(cfg["grid_id"], cfg)
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
                return
        except Exception:
            pass
        time.sleep(0.25)
    grid_dir = paths.grid_dir(cfg["grid_id"])
    raise SystemExit(
        "local signaling server did not become healthy. "
        f"See {grid_dir / 'server.log'} (and {grid_dir / 'server.err'} for bootstrap/crash output)"
    )


def grid_url(cfg: dict[str, Any]) -> str:
    return str(cfg["lan_signaling_url"]).rstrip("/")


def engine_endpoint_url(endpoint_url: str | None, port: int, advertise_host: str | None = None) -> str:
    if endpoint_url:
        return normalize_url(endpoint_url)
    return f"{make_local_url(port, advertise_host)}/v1"


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
