"""The deliberate real-subprocess tests in this suite: a **real** detached ``__remote-engine`` serve
child — torn down by the **real** ``grid leave`` handler, or dying on its own before it ever
registered — against a **fake in-process relay**.

Why this exists (`.scratch/grid-leave/` issue 04). `grid leave` used to kill the serve child by the
run record's pid *alone* and then delete the record unconditionally, so a stale or missing record
stranded a live child that heartbeated as a provider forever — five such orphans, 7–23 days old,
were found on a real operator's machine. Every unit test around that code mocks ``Popen``, which is
exactly where the bug hid: with the process faked, a mistargeted kill and a correct one look
identical. Diagnosis only saw the failure once a real child met a relay double, so that harness is
promoted here.

The fake relay implements the master's exact node-liveness rule (``role ∈ {provider, both}`` ∧ a
heartbeat inside the TTL ∧ a non-empty model list), so ``_RelayState.overview_live()`` answers the
same question ``grid models`` / ``grid engines`` do. Each regime asserts the symptom triple:

1. the real child process is dead,
2. the relay saw the ``role=consumer`` flip for this node,
3. the overview lists nothing.

Nothing in the leave path is mocked — real dispatch, real ``_stop_engine``, real POSIX argv sweep
(shelling out to the host's ``ps``), real ``PUT`` over real HTTP.

The second test (grid-leave issue 05) covers the other end of the same orphan class: the record
*deletion* a serve child performs from its own exit path when it dies before registering. Mocked,
that unlink and an ownership-checked one look identical — it takes a real child, really stamping its
real pid into a real record, to show which record it actually removes.

One subtlety worth stating plainly, because it decides what this file can prove. The child's own
exit-path unregister sends a ``PUT`` byte-identical to the CLI's backstop deregister, and leave runs
reap-then-backstop — the reap blocks until the pid is confirmed gone, so the child's unregister has
*already* flipped the role and emptied the overview before the backstop is even called. Assertions
on the flip alone would therefore survive deleting the backstop outright. ``_RelayState`` closes that
by stamping each ``PUT`` with the child's liveness on arrival: a consumer ``PUT`` that lands when the
child is already dead can only have come from the CLI. The kill→backstop *ordering* itself stays a
mocked seam by design (PRD §Testing Decisions) — covered by
``test_local_cli.py::test_remote_leave_backstop_lands_even_when_child_was_killed`` and its three
siblings, which assert exactly one consumer PUT with ``terminate_pid`` mocked. Don't delete those
believing this file subsumes them; it proves the backstop *fires*, not that it fires second.
"""
from __future__ import annotations

import base64
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, NamedTuple

import pytest

import cli
from local import runtime
from remote import credentials, orphan_sweep
from shared import paths, process_identity, run_records, state

# The sweep half of the old skip reason is gone — grid-leave issue 06 gave the argv sweep Windows
# parity, and `tests/test_windows_orphan_sweep.py` proves it against the real process table. What
# remains is a property of what THIS file asserts, not of the sweep: the teardown it measures is
# SIGTERM → a 25s grace loop → `killpg` of a `start_new_session` group, and Windows has none of those
# (`run_records.terminate_pid` short-circuits straight to `taskkill /F /T`, no grace, no group). The
# spawn differs too — `start_new_session=True` is silently ignored on Windows — so the detached child
# these regimes depend on would not be detached at all.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="asserts the POSIX teardown itself (SIGTERM → 25s grace → killpg of a start_new_session "
           "group), which has no Windows equivalent; the Windows sweep is covered by "
           "tests/test_windows_orphan_sweep.py",
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ENGINE_ID = "remote"  # cli.remote_provider._REMOTE_IDENTITY — remote keeps one identity per grid
_GRID_NAME = "team"
_MODEL = "itest-model"

# The master's node TTL, deliberately UNSCALED. The whole test lives well inside it and the child
# heartbeats immediately at loop start, so heartbeat freshness never lapses — an empty overview can
# only come from the consumer-role flip, never from an expiring TTL. That keeps the third assertion
# about the code under test instead of about a clock.
_NODE_TTL = 120.0
# The fake relay's long-poll park. `remote/serve._poll_loop` re-polls a 204 with NO backoff, so an
# instant 204 would spin the child at 100% CPU; parking also bounds the SIGTERM drain.
_POLL_PARK = 0.5
_WAIT_TIMEOUT = 15.0  # every wait here is bounded; nothing sleeps a fixed time and calls it an assert

# Leave prints this when it could NOT read the process table. Asserting its absence is what proves
# the real argv sweep ran against real `ps` output rather than silently degrading to "nothing found".
_SWEEP_UNSCANNED_NOTE = cli.remote_provider._SWEEP_UNSCANNED_NOTE


# ---------------------------------------------------------------------------
# The relay double (also the control plane — one loopback port serves both)
# ---------------------------------------------------------------------------

class _NodePut(NamedTuple):
    """One ``PUT /nodes/{id}`` the relay received, plus whether the serve child was still alive when
    it arrived — the only thing that distinguishes the child's own unregister from the CLI backstop."""

    path: str
    role: Any
    models: Any
    child_was_alive: bool


class _RelayState:
    """The one node row the fake relay keeps, plus every ``PUT /nodes/...`` it received.

    Mirrors the master's storage faithfully on the axis under test: a register/deregister ``PUT``
    sets the role and model list, a heartbeat refreshes freshness and **never touches the role** (so
    a surviving child can pin a node inside the TTL but can never promote itself back to provider).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._role = "consumer"
        self._models: list[str] = []
        self._last_heartbeat = 0.0
        self._puts: list[_NodePut] = []
        # Assigned right after the spawn returns, so the child's very first (register) PUT can race
        # it and be stamped `child_was_alive=False`. Harmless by construction: that PUT carries
        # role=provider, and only role=consumer PUTs are ever filtered on the stamp. Every consumer
        # PUT happens at leave time, long after the assignment. Plain int store — atomic.
        self.child_pid = 0

    def put_node(self, path: str, body: dict[str, Any]) -> None:
        # Stamp the child's liveness BEFORE answering, and outside the lock (it is an `os.kill(pid,0)`
        # syscall, not shared state): the child stays blocked on our response while it sends its own
        # unregister, so that PUT is always stamped alive, while the CLI's backstop — sent only after
        # `terminate_pid` confirmed the pid gone — is always stamped dead. That is the only thing
        # separating the two otherwise byte-identical consumer PUTs (see `backstop_put_paths`).
        child_was_alive = bool(self.child_pid) and run_records.pid_alive(self.child_pid)
        with self._lock:
            self._puts.append(_NodePut(path, body.get("role"), body.get("models"), child_was_alive))
            if body.get("role") is not None:
                self._role = str(body["role"])
            if "models" in body:
                self._models = list(body.get("models") or [])
            self._last_heartbeat = time.monotonic()  # registering counts as liveness, as on the master

    def heartbeat(self) -> None:
        with self._lock:
            self._last_heartbeat = time.monotonic()

    def overview_live(self) -> bool:
        """The master's exact query-time filter: a node is listed iff it is a provider with a fresh
        heartbeat and something to serve. ``False`` here == an empty ``grid models``/``grid engines``."""
        with self._lock:
            if not self._last_heartbeat:
                return False
            fresh = (time.monotonic() - self._last_heartbeat) <= _NODE_TTL
            return self._role in ("provider", "both") and fresh and bool(self._models)

    def role(self) -> str:
        with self._lock:
            return self._role

    def consumer_put_paths(self) -> list[str]:
        """Paths of the deregister ``PUT``s (``role=consumer``) — which node was flipped, and how often."""
        with self._lock:
            return [put.path for put in self._puts if put.role == "consumer"]

    def backstop_put_paths(self) -> list[str]:
        """Paths of the consumer ``PUT``s that can ONLY be the CLI's backstop deregister.

        Leave runs reap-then-backstop (``_full_leave_teardown``), and the reap blocks until
        ``terminate_pid`` confirms the pid is gone — so the child's own exit-path unregister has
        necessarily already landed, *while it was alive*, before the backstop is even called. A
        consumer PUT stamped with the child already dead therefore has exactly one possible sender.

        Without this split every assertion in this file would survive deleting issue 01's backstop
        outright: the child's own unregister alone flips the role and empties the overview.
        """
        with self._lock:
            return [put.path for put in self._puts if put.role == "consumer" and not put.child_was_alive]


class _Handler(BaseHTTPRequestHandler):
    """The relay + control-plane routes the serve child and `grid leave` actually call."""

    def log_message(self, *args: Any) -> None:  # silence the stdlib access log
        pass

    @property
    def _state(self) -> _RelayState:
        return self.server.relay_state  # type: ignore[attr-defined]

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8", "replace"))
        except ValueError:
            return {}

    def _send_json(self, code: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the child was signalled mid-request — that IS the teardown under test

    def do_PUT(self) -> None:  # register / the child's own unregister / the CLI backstop deregister
        body = self._read_body()
        gate = self.server.register_gate  # type: ignore[attr-defined]
        if gate is not None and body.get("role") == "provider":
            # Park the child INSIDE its registration so the test can act while it is demonstrably
            # past its startup pid self-stamp and demonstrably not yet registered, then fail it. No
            # sleep-and-hope: `register_seen` says "it's blocked here now".
            self.server.register_seen.set()  # type: ignore[attr-defined]
            gate.wait(timeout=_WAIT_TIMEOUT)
            self._state.put_node(self.path, body)
            self._send_json(500, {"error": "relay unavailable"})
            return
        self._state.put_node(self.path, body)
        self._send_json(200, {"status": "updated"})

    def do_POST(self) -> None:
        self._read_body()
        if self.path.endswith("/nodes/heartbeat"):
            self._state.heartbeat()
            self._send_json(200, {"ttl_seconds": int(_NODE_TTL)})
            return
        self._send_json(200, {})

    def do_GET(self) -> None:
        if self.path.startswith("/relay/v1/poll"):
            time.sleep(_POLL_PARK)  # long-poll shape; a 0-delay 204 hot-spins the child's poll loop
            try:
                self.send_response(204)
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if "/managed-networks/" in self.path and self.path.endswith("/status"):
            # The control-plane route a record-less leave resolves the relay through. Serving it
            # here is what keeps that regime hermetic: no monkeypatch inside the leave path, and
            # no packet leaves the box.
            self._send_json(200, {"state": "running", "signaling_url": self.server.base_url})  # type: ignore[attr-defined]
            return
        self._send_json(200, {})


class _FakeRelay(ThreadingHTTPServer):
    """The grid's relay and control plane on one ephemeral loopback port.

    With ``gate_register`` the register ``PUT`` is held open until the test releases it and then
    answered ``500`` — a relay that is up but unwell, which is what a master mid-respawn looks like
    to a joining child (the PRD's field confirmation #2) and the only way to fail a *real* child
    after its startup self-stamp without mocking anything inside it.
    """

    daemon_threads = True  # a parked poll handler must never hold up interpreter exit

    def __init__(self, *, gate_register: bool = False) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.relay_state = _RelayState()
        self.base_url = f"http://127.0.0.1:{self.server_address[1]}"
        self.register_gate = threading.Event() if gate_register else None
        self.register_seen = threading.Event()
        threading.Thread(target=self.serve_forever, daemon=True).start()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _jwt(claims: dict[str, Any]) -> str:
    """An unsigned JWT-shaped token carrying ``claims`` — enough for the best-effort claim decode in
    ``remote.credentials.node_id_from_token`` (nothing verifies a signature). The leave backstop
    addresses the node by this token's ``node_id`` claim, never by the run record's field."""
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{body}.sig"


def _closed_port() -> int:
    """A loopback port with nothing listening. Every capability probe against it fails on instant
    ECONNREFUSED (``remote/probe.py`` has no retry sleeps), so the child advertises chat-only and
    reaches "registered" in well under a second — no engine to install, launch or wait for."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _dead_pid() -> int:
    """A pid that is certainly dead **and reaped**: run a no-op child and wait for it. (Synthesising
    one by arithmetic risks exceeding ``pid_max`` — 99999 on macOS — or naming a live process.)"""
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


def _wait_until(predicate: Callable[[], bool], *, timeout: float = _WAIT_TIMEOUT) -> bool:
    """Poll ``predicate`` until it holds; return whether it ever did. The only waiting primitive in
    this module — callers assert on the return value, so a failure message carries real context."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _log_tail(network_id: str, limit: int = 2000) -> str:
    """The serve child's own log (``_spawn_remote_engine`` points its stdout+stderr there). Only read
    while an assertion is already failing, so a startup failure explains itself instead of timing out."""
    path = paths.engines_dir(network_id) / f"{_ENGINE_ID}.log"
    try:
        return path.read_text(errors="replace")[-limit:]
    except OSError as exc:
        return f"<no child log at {path}: {exc}>"


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Best-effort SIGKILL of the child's own process group, so a serve child that survived a failed
    assertion never escapes the test. Confined **because** ``_spawn_remote_engine`` spawns with
    ``start_new_session=True``: ``setsid()`` runs in the child before exec, making it session *and*
    group leader for life, so its pgid is its own pid and this cannot in practice reach the pytest
    process group. The residual is the pid-reuse TOCTOU between ``poll()`` and ``killpg`` that the
    production kill path already documents and accepts (``remote/orphan_sweep.py`` on
    ``terminate_pid``: "not fully fixable without pidfd"). The reaper thread does the reaping."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except OSError:  # ProcessLookupError/PermissionError included — it exited, or isn't ours
        pass


class _LiveEngine(NamedTuple):
    network_id: str
    node_id: str
    relay: _FakeRelay
    proc: subprocess.Popen
    record_path: Path


def _seed_grid_home(monkeypatch, tmp_path, *, network_id: str, node_id: str, relay_base: str) -> None:
    """Everything on disk a remote-mode ``grid leave`` reads: mode, credentials, and the engine run
    record — all under ``tmp_path``. ``shared/paths.grid_home()`` re-reads ``GRID_HOME`` on every
    call (no import-time caching), so the env var covers parent and child alike, and
    ``_spawn_remote_engine`` snapshots ``os.environ`` after this runs."""
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    # The child must import THIS checkout whatever directory pytest was invoked from.
    monkeypatch.setenv("PYTHONPATH", str(_REPO_ROOT))
    state.set_mode("remote")
    credentials.save_credentials({
        "session_token": "sess-itest",
        "api_url": relay_base,  # the same double answers the control-plane status GET
        "user": {"email": "itest@example.com"},
        "networks": [{
            "network_id": network_id,
            "name": _GRID_NAME,
            "access_token": _jwt({"node_id": node_id, "network_id": network_id}),
        }],
    })
    state.set_active("remote", _GRID_NAME)

    engine_url = f"http://127.0.0.1:{_closed_port()}/v1"
    run_records.write_record(network_id, _ENGINE_ID, {
        "engine_id": _ENGINE_ID,
        "grid_id": network_id,
        "pid": 0,  # what a join write-race leaves behind; the child self-stamps its real pid over it
        "reload_signal": "sighup",
        "signaling_url": relay_base,
        "meta_name": "itest-box",
        "engines": [{"endpoint_url": engine_url, "models": [_MODEL]}],
        "models": [_MODEL],
        "endpoint_url": engine_url,
        "media": False,
    })


class _Spawned:
    """Everything a spawning fixture must tear down, split by how it may be killed.

    ``children`` are detached serve children (``start_new_session=True`` → their own process group,
    so ``_kill_process_group`` is confined to them). ``helpers`` are ordinary ``Popen`` processes that
    share pytest's process group — killed by pid ONLY, never by group, or the run kills itself."""

    def __init__(self) -> None:
        self.relays: list[_FakeRelay] = []
        self.children: list[subprocess.Popen] = []
        self.helpers: list[subprocess.Popen] = []


@pytest.fixture
def spawned():
    box = _Spawned()
    yield box
    for proc in box.children:  # children first, so none is left polling a relay that's going away
        _kill_process_group(proc)
    for proc in box.helpers:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
    for relay in box.relays:
        relay.shutdown()
        relay.server_close()


# A **real** launcher shim: it re-execs the CLI as a child and waits for it, exactly like the Nuitka
# `--onefile` bootstrap on the Linux build and the uv/pip trampoline on Windows. `start_new_session`
# is applied to the shim, so the shim is the session/group leader and the real serve child is a
# *member* of its group — which is the whole reason `proc.pid` can name something that is not the
# engine, and why the run record stamps a `pgid` beside the pid.
_LAUNCHER_SHIM = (
    "import subprocess, sys; "
    "sys.exit(subprocess.run([sys.executable, '-m', 'cli'] + sys.argv[1:]).returncode)"
)


@pytest.fixture
def live_engine(monkeypatch, tmp_path, spawned):
    """Seed a remote-mode ``GRID_HOME``, start the relay double, spawn the **real** detached serve
    child, and block until the relay sees it as a live provider. Yields a factory (the per-regime
    network id has to be unique); ``spawned`` guarantees teardown of whatever it started.

    ``reap=False`` skips the reaper thread so a killed child becomes a **real zombie** — the opposite
    of what every other regime here wants, and the point of the zombie regime. ``shim=True`` spawns
    through a real launcher process, so the recorded pid is not the serve child's.
    """
    relays = spawned.relays
    procs = spawned.children

    def start(tag: str, *, reap: bool = True, shim: bool = False) -> _LiveEngine:
        # A unique network id per regime, because the argv sweep under test is the REAL one: it
        # enumerates the whole host process table. A unique id guarantees this test can never match
        # — and kill — a `grid join` child the developer is actually running, nor a leftover from a
        # sibling regime. Satisfies remote_grid._NETWORK_ID_RE ([A-Za-z0-9_-]+).
        network_id = f"itest-{os.getpid()}-{tag}"
        node_id = f"node-{tag}"
        relay = _FakeRelay()
        relays.append(relay)  # registered BEFORE anything below can raise, or its listener leaks

        _seed_grid_home(monkeypatch, tmp_path, network_id=network_id, node_id=node_id,
                        relay_base=relay.base_url)

        # Under pytest ``sys.argv[0]`` is `.../bin/pytest` — an executable file — so the real
        # ``runtime._cli_subprocess_command()`` would spawn `pytest __remote-engine ...`. Everything
        # else about the spawn stays real: the argv the sweep matches, the detached session, the log.
        command = [sys.executable, "-c", _LAUNCHER_SHIM] if shim else [sys.executable, "-m", "cli"]
        monkeypatch.setattr(runtime, "cli_command", lambda: command)
        proc = cli.remote_provider._spawn_remote_engine(network_id, _ENGINE_ID)
        procs.append(proc)  # ditto — before the waits below, which can fail
        relay.relay_state.child_pid = proc.pid  # lets each PUT record whether the child was still alive
        # Reap the instant it exits. We are the child's parent, so an unreaped child lingers as a
        # zombie, ``os.kill(pid, 0)`` keeps succeeding, and ``run_records.pid_alive`` would report a
        # dead child as alive — which would make leave's terminator burn its whole 25s grace and
        # then declare a phantom survivor. Measured during diagnosis.
        if reap:
            threading.Thread(target=proc.wait, daemon=True).start()

        _wait_until(lambda: relay.relay_state.overview_live() or proc.poll() is not None)
        assert relay.relay_state.overview_live(), (
            f"the serve child never became a live provider (exit={proc.poll()}, "
            f"role={relay.relay_state.role()!r}). Child log:\n{_log_tail(network_id)}"
        )
        return _LiveEngine(
            network_id=network_id, node_id=node_id, relay=relay, proc=proc,
            record_path=run_records.record_path(network_id, _ENGINE_ID),
        )

    return start


@pytest.fixture
def dying_engine(monkeypatch, tmp_path, spawned):
    """Same real spawn, but against a relay that holds the register ``PUT`` open and then fails it.
    Returns once the child is parked inside its registration — past its startup pid self-stamp,
    never registered — which is the only window where the exit-path record reap under test decides
    anything. The caller releases the gate (``engine.relay.register_gate.set()``)."""

    def start(tag: str) -> _LiveEngine:
        network_id = f"itest-{os.getpid()}-{tag}"  # unique per regime — see `live_engine`
        node_id = f"node-{tag}"
        relay = _FakeRelay(gate_register=True)
        spawned.relays.append(relay)

        _seed_grid_home(monkeypatch, tmp_path, network_id=network_id, node_id=node_id,
                        relay_base=relay.base_url)
        monkeypatch.setattr(runtime, "cli_command", lambda: [sys.executable, "-m", "cli"])
        proc = cli.remote_provider._spawn_remote_engine(network_id, _ENGINE_ID)
        spawned.children.append(proc)
        relay.relay_state.child_pid = proc.pid
        # Reap on exit, or the dead child lingers as a zombie and `pid_alive` keeps calling it alive
        # (the same trap `live_engine` documents — and the one this file's waits depend on).
        threading.Thread(target=proc.wait, daemon=True).start()

        assert relay.register_seen.wait(_WAIT_TIMEOUT), (
            f"the serve child never reached its register PUT (exit={proc.poll()}). "
            f"Child log:\n{_log_tail(network_id)}"
        )
        return _LiveEngine(
            network_id=network_id, node_id=node_id, relay=relay, proc=proc,
            record_path=run_records.record_path(network_id, _ENGINE_ID),
        )

    return start


# ---------------------------------------------------------------------------
# The regression tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("regime", ["healthy", "stale_pid", "orphan"])
def test_real_child_leave_kills_the_child_and_deregisters(live_engine, capsys, regime):
    """`grid leave` is authoritative in all three measured regimes: the real serve child ends up
    dead, the relay sees the ``role=consumer`` flip for this node, and the overview lists nothing —
    immediately, not after the 120s TTL.

    ``stale_pid`` is the reproduced root cause (record pid ≠ live child), where only the argv sweep
    can reach the process; ``orphan`` is the field case where the record directory was deleted out
    from under a live child, making it untracked and — before this fix — unkillable via the CLI.
    """
    engine = live_engine(regime)
    relay_state = engine.relay.relay_state
    child_pid = engine.proc.pid

    if regime == "healthy":
        # Issue 03's self-stamp: the child wrote its real pid over the spawner's ``0``, so the
        # record-pid kill targets the live process.
        assert int(run_records.read_record(engine.network_id, _ENGINE_ID)["pid"]) == child_pid
    elif regime == "stale_pid":
        # Drift the record. Written only NOW — a stale pid seeded before the child booted would have
        # been healed by its startup self-stamp, and the bug would not reproduce.
        stale = _dead_pid()
        assert not run_records.pid_alive(stale), f"pid {stale} was recycled; the regime is void"
        run_records.update_record(engine.network_id, _ENGINE_ID, pid=stale)
    else:  # orphan: record gone, child alive — four of the five orphans found in the field
        engine.record_path.unlink()

    capsys.readouterr()  # drop the bring-up noise; what follows reads leave's own output
    assert cli.main(["leave"]) == 0
    captured = capsys.readouterr()
    out = captured.out
    # socketserver prints a handler thread's traceback to the (capsys-replaced) stderr, so a bug in
    # the relay double itself would otherwise be captured and silently discarded.
    assert "Traceback" not in captured.err, f"the fake relay raised in a handler thread:\n{captured.err}"

    # ---- the symptom triple ----
    assert _wait_until(lambda: not run_records.pid_alive(child_pid)), (
        f"the serve child (pid {child_pid}) survived `grid leave` — it keeps heartbeating and pins "
        f"the node inside the TTL (relay role is {relay_state.role()!r}, listed="
        f"{relay_state.overview_live()}). Child log:\n{_log_tail(engine.network_id)}"
    )
    assert relay_state.consumer_put_paths().count(f"/nodes/{engine.node_id}") >= 1, (
        f"the relay never saw a role=consumer deregister for {engine.node_id}; role is still "
        f"{relay_state.role()!r}, so the model only drops after the ~120s TTL"
    )
    assert not relay_state.overview_live(), (
        f"the node is still listed (role={relay_state.role()!r}) — `grid models`/`grid engines` "
        "would still advertise a model this box no longer serves"
    )
    # ...and the flip was not merely the child's own parting courtesy: this PUT arrived after the
    # child was already gone, so only the CLI's unconditional backstop can have sent it (issue 01).
    # Exactly-one, not `>= 1`: both PUTs target the same path, so if the child's were ever mis-stamped
    # dead too, the list would be length 2 and fail here rather than passing. The one residual needs
    # BOTH a handler starved past `unregister_node`'s 15s client timeout (it does a dict update and a
    # JSON encode) AND the real backstop failing to land — and this file's other budgets
    # (_WAIT_TIMEOUT, _STOP_GRACE_SECONDS) would blow first. Deliberately NOT closed by also asserting
    # a live-stamped PUT: that would couple this test to the child's own unregister, which the leave
    # contract designates an optimization, not the mechanism of record.
    assert relay_state.backstop_put_paths() == [f"/nodes/{engine.node_id}"], (
        "no consumer PUT arrived after the child was dead, so the CLI backstop deregister never "
        f"fired — a SIGKILLed or already-dead child would leave the node registered. Consumer PUTs "
        f"seen: {relay_state.consumer_put_paths()}"
    )

    # ---- leave reported honestly, and the sweep really ran ----
    assert _SWEEP_UNSCANNED_NOTE not in out, "the argv sweep degraded instead of reading the process table"
    reaped_line = f"Reaped 1 orphaned serve child(ren) on {_GRID_NAME} (pid(s) {child_pid})."

    if regime == "healthy":
        # The recorded pid did the work, so the sweep had nothing left to find (it excludes the
        # recorded pids). Note this shows rather than proves non-double-termination: by sweep time the
        # child is gone from the process table anyway, so the exclusion logic isn't what's exercised
        # here — the sweep-matching regimes below carry that. Likewise nothing in THIS regime needs
        # the sweep to find anything, so the unscanned-note check above is corroborated by
        # `stale_pid`/`orphan` (which fail loudly on a vacuous `ps`), not independent of them.
        assert f"Left {_GRID_NAME}." in out
        assert "Reaped" not in out
        assert not engine.record_path.exists()
    elif regime == "stale_pid":
        # Only the sweep could reach the live child: the record's dead pid made the terminator
        # report "confirmed gone" truthfully, and the record was then unlinked.
        assert f"Left {_GRID_NAME}." in out
        assert reaped_line in out
        assert not engine.record_path.exists()
    else:  # orphan
        assert reaped_line in out
        assert f"No engines were tracked for {_GRID_NAME}; deregistered it from the relay" in out


def test_real_child_leave_converges_over_a_real_zombie(live_engine, capsys):
    """`grid leave` finishes over a **real** corpse (`.scratch/grid-leave/` issue 08, residual (c)).

    Reproduced in the field on a Docker host whose PID 1 is `tail -f /dev/null` and therefore never
    reaps: every serve-child death leaves a permanent `Z (zombie)` entry, and `os.kill(pid, 0)` calls
    one alive. Leave's terminator then SIGTERMed the corpse, waited out the full 25s stop grace,
    SIGKILLed it, still read it as alive, and reported a survivor — so on this branch the
    honest-teardown gate kept the record and failed **every retry, forever**. The argv sweep cannot
    rescue it either: a zombie's command line is empty, so it matches nothing.

    Built with a real zombie rather than a fake: the fixture's reaper thread is disabled, so this test
    process stays the child's unreaping parent — the same relationship a container PID 1 has to it.
    """
    engine = live_engine("zombie", reap=False)
    child_pid = engine.proc.pid

    os.kill(child_pid, signal.SIGKILL)
    assert _wait_until(lambda: process_identity.is_zombie(child_pid)), (
        f"pid {child_pid} never became a zombie (state via ps: "
        f"{process_identity.process_facts(child_pid)}), so this regime is void"
    )
    assert run_records.pid_alive(child_pid), "a zombie must still answer os.kill(pid, 0) — that IS the bug"

    capsys.readouterr()
    started = time.monotonic()
    assert cli.main(["leave"]) == 0, "leave failed over a corpse — the never-converging retry loop"
    elapsed = time.monotonic() - started

    assert f"Left {_GRID_NAME}." in capsys.readouterr().out
    assert not engine.record_path.exists(), "the record was kept, so every retry would fail the same way"
    assert elapsed < 15.0, (
        f"took {elapsed:.1f}s: the teardown burned its stop grace signalling a process that had "
        "already exited"
    )


def _serve_children_in_group(pgid: int, network_id: str) -> list[int]:
    """Pids in process group ``pgid`` whose argv is a serve child of ``network_id``, asked of the
    host's own ``ps``. This is the TEST's tool for naming the process it must assert about; the code
    under test is separately denied any process table at all.

    Matched by argv rather than by "everything in the group", because the group also holds short-lived
    helpers — the one-shot ``ps`` the identity stamp runs, for one — and a test that waited for those
    to die would be asserting about the wrong processes.
    """
    out = subprocess.run(
        ["ps", "-A", "-ww", "-o", "pid=,pgid=,command="],
        capture_output=True, text=True, errors="replace",
    ).stdout
    matched = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) != 3 or not parts[0].isdecimal() or not parts[1].isdecimal():
            continue
        tokens = parts[2].split()
        if int(parts[1]) != pgid or run_records.REMOTE_ENGINE_MARKER not in tokens:
            continue
        if tokens[tokens.index(run_records.REMOTE_ENGINE_MARKER) + 1] == network_id:
            matched.append(int(parts[0]))
    return matched


def test_real_child_group_backstop_reaps_the_engine_when_ps_is_unreadable(
    live_engine, monkeypatch, capsys
):
    """Criterion 6: a real serve child reaped with **no readable process table at all**.

    The regime is residual (b). A `grid leave` can win the record `file_lock` before a just-spawned
    child runs its pid self-stamp — POSIX `flock` hands the lock to whoever asks, in no order — so the
    record still holds what the *spawner* wrote. With a launcher shim in play (the Nuitka `--onefile`
    bootstrap, the uv trampoline) that pid is the **shim's**, and stopping it leaves the real engine
    running. Until now the only thing that could reach that engine was the argv sweep, so a leave whose
    `ps` was simultaneously unavailable printed a qualified success over a live orphan and no retry
    could ever find it.

    The stamped process group closes it: a group id is not recycled while the group has members, so it
    still names our own descendants after the shim is gone. Here the sweep is switched off entirely, so
    nothing but the group can do the work.
    """
    engine = live_engine("groupreap", shim=True)
    shim_pid = engine.proc.pid
    others = [
        pid for pid in _serve_children_in_group(shim_pid, engine.network_id) if pid != shim_pid
    ]
    assert len(others) == 1, (
        f"expected exactly one serve child under launcher shim {shim_pid}, got {others} — the regime "
        "depends on the recorded pid NOT being the engine"
    )
    child_pid = others[0]

    # Roll the record back to what the spawner wrote, i.e. the state a leave racing the join sees
    # before the child's self-stamp lands. The child has really started and really self-stamped; this
    # restores the earlier record rather than faking one that never existed.
    run_records.update_record(
        engine.network_id, _ENGINE_ID, **run_records.identity_stamp(shim_pid)
    )
    assert run_records.read_record(engine.network_id, _ENGINE_ID)["pid"] == shim_pid != child_pid
    # Deny the code under test any process table, so only the stamped process group can reach the
    # engine. Both the seam and its POSIX implementation, so a refactor that stops routing through the
    # seam fails here rather than quietly passing on the host's real `ps`.
    monkeypatch.setattr(orphan_sweep, "_process_table_output", lambda: None)
    monkeypatch.setattr(orphan_sweep, "_posix_ps_output", lambda: None)

    capsys.readouterr()
    assert cli.main(["leave"]) == 0

    # The success line is still qualified, and correctly so: the group proves nothing *this identity*
    # spawned is left, but a stray from some older lineage would have a different group and only the
    # sweep could have found it. What matters is that leave stopped the engine and exited 0.
    assert _SWEEP_UNSCANNED_NOTE in capsys.readouterr().out
    assert _wait_until(lambda: not run_records.pid_alive(child_pid)), (
        f"the real serve child (pid {child_pid}) survived `grid leave` — the recorded pid was its "
        f"launcher shim ({shim_pid}) and no process table was available to find it"
    )
    assert not engine.record_path.exists()


@pytest.mark.parametrize("owner", ["itself", "newer-child"])
def test_real_child_dying_before_registration_reaps_only_its_own_record(dying_engine, spawned, owner):
    """A serve child that dies before it ever registered removes its run record — unless that record
    has meanwhile been handed to a **live** newer child, in which case it must leave it alone.

    The other half of the audited orphan class (`.scratch/grid-leave/` issue 05). The unlink used to
    be unconditional and keyed by ``(grid, engine_id)`` rather than by process, so a child wedged in
    bring-up — minutes, in the field — would, whenever it finally died, delete the record of the
    child that had since replaced it. That leaves a live serve child with nothing on disk pointing at
    it: exactly the untracked orphan the argv sweep had to be invented to reap, manufactured here by
    the CLI itself rather than by an outside deleter.

    Real child, real self-stamp, real record files: with ``Popen`` mocked, an unlink that checks
    ownership and one that doesn't are indistinguishable.
    """
    engine = dying_engine(owner)
    stamped = int(run_records.read_record(engine.network_id, _ENGINE_ID)["pid"])
    assert stamped == engine.proc.pid, (
        "the child had not self-stamped its pid by the time it reached its register PUT, so this "
        "test would prove nothing about which record it reaps"
    )

    successor = None
    if owner == "newer-child":
        # Stand in for the serve child a re-join would have started while this one hung: any live
        # process that is neither the dying child nor its parent (the reap treats a launcher-shim
        # parent as its own — see `run_records.discard_own_record`).
        successor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        spawned.helpers.append(successor)
        run_records.update_record(engine.network_id, _ENGINE_ID, pid=successor.pid)

    engine.relay.register_gate.set()  # 500 → the child dies having never registered

    assert _wait_until(lambda: not run_records.pid_alive(engine.proc.pid)), (
        f"the serve child (pid {engine.proc.pid}) never exited after its registration failed. "
        f"Child log:\n{_log_tail(engine.network_id)}"
    )
    if owner == "itself":
        assert not engine.record_path.exists(), (
            "a died-before-registering child must still clear its own record, or every failed "
            "bring-up leaves a ghost that only `grid leave --all` can remove"
        )
    else:
        assert engine.record_path.exists(), (
            f"the dying child deleted the record now owned by live pid {successor.pid} — that child "
            "keeps serving with no record, untracked by `grid leave` and invisible to `grid join`"
        )
        assert int(run_records.read_record(engine.network_id, _ENGINE_ID)["pid"]) == successor.pid
