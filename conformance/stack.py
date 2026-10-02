"""The server side of a conformance run: a real master behind the real proxy (grid-platform ticket 13).

* **The master** is grid-src's `private_server`, started as a process in GRID MODE (the hosted fleet's mode). It verifies
  RS256 grid tokens against a JWKS this run writes, exactly as a hosted master verifies the control plane's.
* **The proxy** is grid-apis' `grid_proxy` app (`proxy_launcher.py`): every routing, wake and refusal decision is the
  real code; only its reads of the control plane come from a state file this run flips.
* **The control plane** a CLI calls directly (a grid's status) is a stub here, and so is the engine a provider serves.
  Neither is under test: they are what the clients need around the two things that are.

Both siblings are located by environment (`GRID_SRC_REPO`, `GRID_APIS_REPO`) or beside this checkout, and run with
their own `.venv/bin/python`.
"""
from __future__ import annotations

import contextlib
import json
import os
import secrets
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

ROOT = Path(__file__).resolve().parents[1]
ISSUER = "https://conformance.invalid"
KID = "conformance-1"
ENGINE_MODEL = "conformance-echo"
ENGINE_ANSWER = "conformance-ok"
INFERENCE_SCOPES = ["inference:create", "inference:models", "inference:resume"]
PROVIDER_SCOPES = ["provider:heartbeat", "provider:update", "provider:poll", "provider:submit", "provider:error"]
_FAR_FUTURE = 4_102_444_800  # 2100-01-01


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass(frozen=True)
class Siblings:
    grid_src: Path
    grid_apis: Path

    @property
    def grid_src_python(self) -> Path:
        return self.grid_src / ".venv" / "bin" / "python"

    @property
    def grid_apis_python(self) -> Path:
        return self.grid_apis / ".venv" / "bin" / "python"


def locate_siblings() -> Siblings | None:
    """grid-src and grid-apis: named by environment in CI, beside this checkout on a laptop. None when absent."""
    def find(env: str, names: tuple[str, ...]) -> Path | None:
        if os.environ.get(env):
            return Path(os.environ[env]).resolve()
        for name in names:
            for candidate in (ROOT.parent / name, ROOT.parents[1] / name):
                if (candidate / ".venv" / "bin" / "python").exists():
                    return candidate
        return None

    grid_src = find("GRID_SRC_REPO", ("grid-src",))
    grid_apis = find("GRID_APIS_REPO", ("grid-apis",))
    return Siblings(grid_src, grid_apis) if grid_src and grid_apis else None


# ── Keys and tokens: what the control plane signs ───────────────────────────────────────────────


@dataclass
class Signer:
    key: rsa.RSAPrivateKey = field(default_factory=lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048))

    def jwks(self) -> dict:
        jwk = json.loads(RSAAlgorithm.to_jwk(self.key.public_key()))
        return {"keys": [{**jwk, "kid": KID, "alg": "RS256", "use": "sig"}]}

    def token(self, network_id: str, *, email: str, node_id: str, scopes: list[str], roles: list[str],
              network_type: str = "permissionless") -> str:
        now = int(time.time())
        claims = {
            "iss": ISSUER, "aud": f"grid:{network_id}", "sub": email, "iat": now, "exp": _FAR_FUTURE,
            "network_id": network_id, "network_type": network_type, "node_id": node_id, "email": email,
            "scopes": scopes, "roles": roles, "member_epoch": 1, "network_epoch": 1,
        }
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": KID})


# ── The two stubs ───────────────────────────────────────────────────────────────────────────────


class _Quiet(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def _json(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@contextlib.contextmanager
def _serve(handler: type[BaseHTTPRequestHandler]) -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def _engine_handler() -> type[BaseHTTPRequestHandler]:
    """An OpenAI-compatible engine that answers every chat with one known word: what a provider serves."""

    class Engine(_Quiet):
        def do_GET(self) -> None:
            if self.path.rstrip("/").endswith("/models"):
                self._json(200, {"object": "list", "data": [{"id": ENGINE_MODEL, "object": "model", "owned_by": "conformance"}]})
            else:
                self._json(200, {"ok": True})

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if not self.path.rstrip("/").endswith("/chat/completions"):
                self._json(404, {"error": "conformance engine serves chat only"})
                return
            created = int(time.time())
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for delta in ({"role": "assistant"}, {"content": ENGINE_ANSWER}):
                    chunk = {"id": "c1", "object": "chat.completion.chunk", "created": created, "model": ENGINE_MODEL,
                             "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                last = {"id": "c1", "object": "chat.completion.chunk", "created": created, "model": ENGINE_MODEL,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}}
                self.wfile.write(f"data: {json.dumps(last)}\n\ndata: [DONE]\n\n".encode())
                self.wfile.flush()
                return
            self._json(200, {
                "id": "c1", "object": "chat.completion", "created": created, "model": ENGINE_MODEL,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": ENGINE_ANSWER}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
            })

    return Engine


def _control_plane_handler(state_path: Path, calls: list[str]) -> type[BaseHTTPRequestHandler]:
    """The control-plane routes a released CLI calls directly. Every call is recorded, so a run says what it needed."""

    class ControlPlane(_Quiet):
        def do_GET(self) -> None:
            calls.append(f"GET {self.path}")
            parts = self.path.split("?")[0].strip("/").split("/")
            if parts[:3] == ["v1", "grid", "managed-networks"] and len(parts) == 5 and parts[4] == "status":
                grid = json.loads(state_path.read_text())["grids"].get(parts[3])
                if grid is None or grid["state"] == "deleted":
                    self._json(404, {"detail": "Grid not found"})
                else:
                    self._json(200, {"network_id": parts[3], "status": grid["state"]})
                return
            self._json(404, {"detail": f"the conformance control plane does not serve {self.path}"})

        def do_POST(self) -> None:
            calls.append(f"POST {self.path}")
            self._json(404, {"detail": f"the conformance control plane does not serve {self.path}"})

    return ControlPlane


# ── The stack ───────────────────────────────────────────────────────────────────────────────────


@dataclass
class Stack:
    """One grid, its master, the proxy in front of it, and the stubs around them. `with Stack.up(...) as stack:`.

    Sleep is what it is on the fleet: the grid keeps its port, and its master process goes. A supervisor keeps the master
    in step with the state file, so a wake the proxy asks for brings it back as the control plane's wake would."""

    siblings: Siblings
    workdir: Path
    network_id: str
    signer: Signer
    master_port: int
    proxy_port: int
    control_plane_port: int
    engine_port: int
    control_plane_calls: list[str]
    _master_proc: subprocess.Popen | None = None
    _master_lock: threading.Lock = field(default_factory=threading.Lock)
    _paused: bool = False
    _master_env: dict[str, str] = field(default_factory=dict)
    supervisor_errors: list[str] = field(default_factory=list)

    @property
    def state_path(self) -> Path:
        return self.workdir / "state.json"

    @property
    def proxy_url(self) -> str:
        return f"http://127.0.0.1:{self.proxy_port}"

    @property
    def grid_url(self) -> str:
        """What a CLI is told the grid's address is: the proxy, by grid id (the hosted fleet's signaling URL)."""
        return f"{self.proxy_url}/{self.network_id}"

    @property
    def control_plane_url(self) -> str:
        return f"http://127.0.0.1:{self.control_plane_port}"

    @property
    def engine_url(self) -> str:
        return f"http://127.0.0.1:{self.engine_port}/v1"

    def state(self) -> dict:
        return json.loads(self.state_path.read_text())["grids"][self.network_id]

    def set_state(self, state: str, *, last_known: dict | None = None) -> None:
        """Record the grid's state as the control plane would, and bring the master in step at once.

        `last_known` is the sleep RECORD (`{nodes, ids}`); the proxy stamps its age."""
        record = {"captured_at": time.time(), **last_known} if last_known is not None else None
        _write_state(self.state_path, {self.network_id: {"port": self.master_port, "state": state, "last_known": record}})
        self._sync_master()

    @contextlib.contextmanager
    def master_held_down(self) -> Iterator[None]:
        """The grid is meant to be up and its master is not: a crash the platform has not restarted yet."""
        with self._master_lock:
            self._paused = True
            if self._master_proc is not None:
                _stop(self._master_proc)
                self._master_proc = None
        try:
            yield
        finally:
            with self._master_lock:
                self._paused = False
            self._sync_master()

    def consumer_token(self, email: str = "consumer@conformance.invalid") -> str:
        return self.signer.token(self.network_id, email=email, node_id=f"node-{secrets.token_hex(4)}",
                                 scopes=INFERENCE_SCOPES, roles=["consumer"])

    def provider_token(self, email: str = "provider@conformance.invalid") -> str:
        return self.signer.token(self.network_id, email=email, node_id=f"node-{secrets.token_hex(4)}",
                                 scopes=INFERENCE_SCOPES + PROVIDER_SCOPES, roles=["consumer", "provider"])

    @classmethod
    @contextlib.contextmanager
    def up(cls, siblings: Siblings, workdir: Path) -> Iterator[Stack]:
        workdir.mkdir(parents=True, exist_ok=True)
        network_id = f"grid-{secrets.token_hex(8)}"
        signer = Signer()
        (workdir / "jwks.json").write_text(json.dumps(signer.jwks()))
        calls: list[str] = []
        master_port, proxy_port = free_port(), free_port()
        state_path = workdir / "state.json"
        _write_state(state_path, {network_id: {"port": master_port, "state": "running", "last_known": None}})
        with _serve(_engine_handler()) as engine_port, _serve(_control_plane_handler(state_path, calls)) as cp_port:
            stack = cls(siblings, workdir, network_id, signer, master_port, proxy_port, cp_port, engine_port, calls)
            with stack._master(), stack._proxy():
                yield stack

    # ── the master and its supervisor ──

    @contextlib.contextmanager
    def _master(self) -> Iterator[None]:
        self._master_env = self._build_master_env()
        stop = threading.Event()
        self._sync_master()
        supervisor = threading.Thread(target=self._supervise, args=(stop,), daemon=True)
        supervisor.start()
        try:
            yield
        finally:
            stop.set()
            supervisor.join(timeout=5)
            with self._master_lock:
                if self._master_proc is not None:
                    _stop(self._master_proc)
                    self._master_proc = None

    def _supervise(self, stop: threading.Event) -> None:
        while not stop.wait(0.2):
            try:
                self._sync_master()
            except Exception as exc:  # noqa: BLE001 — a supervisor that died would freeze the grid's state
                self.supervisor_errors.append(repr(exc))
                with (self.workdir / "supervisor.log").open("a") as log:
                    log.write(f"{time.time()} {exc!r}\n")

    def _sync_master(self) -> None:
        with self._master_lock:
            if self._paused:
                return
            want_up = self.state()["state"] == "running"  # read under the lock: a set_state cannot slip between
            alive = self._master_proc is not None and self._master_proc.poll() is None
            if want_up and not alive:
                self._master_proc = self._start_master()
            elif not want_up and alive:
                _stop(self._master_proc)
                self._master_proc = None

    def _start_master(self) -> subprocess.Popen:
        private_server = self.siblings.grid_src / "grid_cli" / "private_server"
        log_path = self.workdir / "master.log"
        with log_path.open("ab") as log:
            proc = subprocess.Popen(
                [str(self.siblings.grid_src_python), "-m", "uvicorn", "server:app", "--host", "127.0.0.1",
                 "--port", str(self.master_port), "--log-level", "warning"],
                cwd=private_server, env=self._master_env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            )
        try:
            _wait_until(lambda: _answers(f"http://127.0.0.1:{self.master_port}/server/info"), proc, "the master",
                        log_path)
        except BaseException:
            _stop(proc)  # a master that never answered is still a process: never leave one behind
            raise
        return proc

    def _build_master_env(self) -> dict[str, str]:
        """What `network_runtime.private_server_env` hands a hosted master, less what only the fleet has."""
        return {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(self.siblings.grid_src / "grid_cli" / "private_server"),
            "GRID_MODE": "true",
            "GRID_NETWORK_ID": self.network_id,
            "GRID_NETWORK_TYPE": "permissionless",
            "GRID_TOKEN_AUDIENCE": f"grid:{self.network_id}",
            "GRID_TOKEN_ISSUER": ISSUER,
            "GRID_TOKEN_JWKS_URL": "http://127.0.0.1:1/jwks.json",  # never fetched: the file holds the key
            "GRID_TOKEN_JWKS_PATH": str(self.workdir / "jwks.json"),
            "GRID_BILLING_MODE": "local_free",
            "GRID_ADMIN_SECRET": secrets.token_urlsafe(32),
            "SERVER_PORT": str(self.master_port),
            "SERVER_HOST": "127.0.0.1",
            "DATABASE_URL": f"sqlite+aiosqlite:///{self.workdir / 'master.db'}",
            "JWT_SECRET": secrets.token_urlsafe(32),
            "API_KEY_PEPPER": secrets.token_urlsafe(32),
            "API_KEYS_ENABLED": "false",
            "FAILED_AUTH_THROTTLE_ENABLED": "false",
            "KEYS_PATH": str(self.workdir / "keys"),
            "GRID_TASK_PLANE_ENABLED": "false",
            "GRID_MASTER_BOOT_ID": secrets.token_hex(16),
            "HOME": str(self.workdir / "master-home"),
            "GRID_HOME": str(self.workdir / "master-home" / ".grid"),
        }

    # ── the proxy ──

    @contextlib.contextmanager
    def _proxy(self) -> Iterator[None]:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(self.siblings.grid_apis),
            "GRID_CONFORMANCE_STATE": str(self.state_path),
            "GRID_ENV": "dev",
            "GRID_PROXY_ACTIVITY_DIR": str(self.workdir / "proxy_activity"),  # the default is /var/lib/grid-apis
        }
        log = (self.workdir / "proxy.log").open("ab")
        proc = subprocess.Popen(
            [str(self.siblings.grid_apis_python), str(Path(__file__).with_name("proxy_launcher.py")), str(self.proxy_port)],
            cwd=self.siblings.grid_apis, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        try:
            _wait_until(lambda: _answers(f"{self.proxy_url}/healthz"), proc, "the proxy", self.workdir / "proxy.log")
            yield
        finally:
            _stop(proc)
            log.close()


def _write_state(path: Path, grids: dict) -> None:
    with state_lock(path):
        _replace(path, {"grids": grids})


@contextlib.contextmanager
def state_lock(path: Path) -> Iterator[None]:
    """One writer of the state file at a time, across processes: the proxy's wake and the test both write it."""
    import fcntl

    with open(path.with_suffix(".lock"), "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _replace(path: Path, document: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(json.dumps(document))
    tmp.replace(path)


def _answers(url: str) -> bool:
    try:
        return httpx.get(url, timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def _wait_until(ready: Callable[[], bool], proc: subprocess.Popen, what: str, log: Path, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"{what} exited ({proc.returncode}) before it answered:\n{log.read_text()[-3000:]}")
        if ready():
            return
        time.sleep(0.25)
    raise RuntimeError(f"{what} did not answer within {timeout:.0f}s:\n{log.read_text()[-3000:]}")


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, 15)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, 9)
        proc.wait(timeout=5)
