"""The server side of a conformance run: what serves every grid (grid-platform ticket 13; M5 ticket 07).

* **The stack** is grid-platform's pool conformance target (`grid-src/conformance_pool/pool_target.py`), taken from the
  sibling grid-src checkout rather than copied here, so the two can never drift: two relay pools of one relay instance
  each, in GRID MODE, on a relay database of the run's own; the control plane's sync log and the REAL projector
  between them, so a grid is created, stopped, started and deleted by rows as the control plane does it; and
  grid-apis' REAL inference proxy in front, routing by placement. Each test asks its checks of a grid of its own,
  created by rows on one of those pools (:func:`pool_target_from`). Until M5 ticket 07 this was one single-grid master
  behind the proxy's legacy path, which no grid has had since 2026-10-22.
* **The control plane** a CLI calls directly (a grid's status) is a stub there, and so is the engine a provider serves
  (:func:`_engine_handler`, here, which the pool target extends). Neither is under test.
* **Keys and tokens** (:class:`Signer`): what the control plane signs, verified by the relay instances against a JWKS
  the run writes.

The siblings are located by environment (`GRID_SRC_REPO`, `GRID_APIS_REPO`) or beside this checkout, and run with their
own `.venv/bin/python`. The relay database needs a PostgreSQL SUPERUSER: `GRID_TEST_POSTGRES_URL`, else a throwaway
cluster this run starts with the server binaries it finds (:func:`throwaway_postgres`).
"""
from __future__ import annotations

import atexit
import contextlib
import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Protocol

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


# ── The engine stub ───────────────────────────────────────────────────────────────────────────────


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


# ── The stack ───────────────────────────────────────────────────────────────────────────────────


class Stack(Protocol):
    """What a check reads of the grid it asks: `pool_target.PoolGrid` in grid-src's `conformance_pool` is one. A grid on
    a relay pool never sleeps (R14-D10): its states are `running`, `stopped` (its owner's) and `deleted`."""

    network_id: str
    grid_url: str
    control_plane_url: str
    engine_url: str

    def state(self) -> dict: ...
    def set_state(self, state: str) -> None: ...
    def consumer_token(self, email: str = ...) -> str: ...
    def provider_token(self, email: str = ...) -> str: ...
    def delete_node(self, name: str) -> int: ...


#: Where a PostgreSQL's server binaries may be when `initdb` is not on PATH: Debian's (the CI runner's: PostgreSQL 16,
#: its service disabled) and Homebrew's.
_SERVER_BINARIES = ("/usr/lib/postgresql/*/bin", "/opt/homebrew/opt/postgresql@*/bin",
                    "/usr/local/opt/postgresql@*/bin")


def throwaway_postgres() -> str:
    """A superuser URL on a PostgreSQL cluster of this run's own, stopped and removed when the process exits: the relay
    database's roles need a superuser (grid-src `relay_pg`), and a laptop or a CI job with no database service still has
    the server binaries. RuntimeError when it finds none."""
    initdb = shutil.which("initdb")
    if initdb is None:
        candidates = sorted(path for pattern in _SERVER_BINARIES for path in glob.glob(f"{pattern}/initdb"))
        if not candidates:
            raise RuntimeError("no PostgreSQL server binaries (initdb) on PATH or in "
                               f"{', '.join(_SERVER_BINARIES)}; set GRID_TEST_POSTGRES_URL to a superuser URL")
        initdb = candidates[-1]
    pg_ctl = str(Path(initdb).with_name("pg_ctl"))
    data = Path(tempfile.mkdtemp(prefix="conformance-pg-"))
    subprocess.run([initdb, "-D", str(data / "data"), "-U", "conformance", "--auth=trust"], check=True,
                   capture_output=True)
    port = free_port()
    subprocess.run([pg_ctl, "-D", str(data / "data"), "-l", str(data / "server.log"), "-w", "-o",
                    f"-p {port} -k '' -c listen_addresses=127.0.0.1", "start"], check=True, capture_output=True)

    def stop() -> None:
        subprocess.run([pg_ctl, "-D", str(data / "data"), "-m", "fast", "stop"], capture_output=True, check=False)
        shutil.rmtree(data, ignore_errors=True)

    atexit.register(stop)
    return f"postgresql://conformance@127.0.0.1:{port}/postgres"


def pool_target_from(siblings: Siblings) -> ModuleType:
    """The sibling grid-src's pool conformance target (`conformance_pool/pool_target.py`), imported with what it reads
    at import: this checkout as its `CONFORMANCE_ROOT`, and the siblings as its relay instances' and proxy's
    checkouts. Its `pools` fixture is the session's stack (it needs :func:`use_a_relay_database` first) and its
    `PoolGrid` a grid on it."""
    os.environ.setdefault("CONFORMANCE_ROOT", str(ROOT))
    os.environ.setdefault("GRID_SRC_REPO", str(siblings.grid_src))
    os.environ.setdefault("GRID_APIS_REPO", str(siblings.grid_apis))
    sys.path.insert(0, str(siblings.grid_src / "conformance_pool"))
    import pool_target

    return pool_target


def use_a_relay_database() -> None:
    """The superuser URL the pool target's relay database is made on (grid-src `relay_pg.URL`, read when it bootstraps,
    not when it is imported): `GRID_TEST_POSTGRES_URL`, else :func:`throwaway_postgres` — started here, so a run that
    makes no stack (`--collect-only`, a skip) starts none."""
    import relay_pg  # on the path once the pool target is imported

    relay_pg.URL = os.environ.get("GRID_TEST_POSTGRES_URL") or throwaway_postgres()
