"""Bridge an existing Grid's inference into the disposable physical Goal lab.

Production Goals and inference normally share one Grid. The physical acceptance lab runs a
feature-branch relay before that branch is deployed, so it temporarily has a separate control
plane. This loopback-only bridge lets its relay-host node forward model work to the existing
company Grid without copying that Grid's bearer to the other test machine.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar, NamedTuple

import httpx


_MAX_BODY = 32 * 1024 * 1024
_FORWARDED = frozenset({
    "/responses", "/chat/completions", "/completions", "/models",
})


class SourceGrid(NamedTuple):
    base_url: str
    access_token: str


def _source_grid(name: str) -> SourceGrid:
    """Resolve the source before GRID_HOME is switched to the disposable lab identity."""
    from cli import remote_grid
    from remote import credentials

    session = credentials.require_session()
    record = remote_grid._select(name)
    label = str(record.get("name") or record.get("network_id") or name)
    token = remote_grid.require_access_token(record, label)
    base, _status = remote_grid.resolve_relay_base(
        session, record, remote_grid._network_id(record), label)
    return SourceGrid(base.rstrip("/") + "/relay/v1", token)


def _model_ids(payload: Any) -> list[str]:
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ValueError("source Grid returned no model list")
    return [str(item["id"]) for item in items
            if isinstance(item, dict) and isinstance(item.get("id"), str)]


def require_exact_model(source: SourceGrid, requested: str) -> str:
    """Fail before join when a stale or case-mismatched name would strand every Goal."""
    try:
        response = httpx.get(
            source.base_url + "/models",
            headers={"Authorization": f"Bearer {source.access_token}"}, timeout=15.0)
        response.raise_for_status()
        models = _model_ids(response.json())
    except (httpx.HTTPError, ValueError, RecursionError) as exc:
        raise SystemExit(f"Could not list models on the source Grid: {exc}") from None
    if requested in models:
        return requested
    same_casefold = [model for model in models if model.casefold() == requested.casefold()]
    if same_casefold:
        raise SystemExit(
            f"Source Grid model names are case-sensitive. Use {same_casefold[0]!r}, "
            f"not {requested!r}.")
    available = ", ".join(models) if models else "(none)"
    raise SystemExit(f"Source Grid does not serve {requested!r}. Available: {available}")


class Bridge:
    """A loopback forwarding boundary that never hands the source bearer to Grid or an agent."""

    def __init__(self, source: SourceGrid, port: int = 0):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"
            bridge: ClassVar[Bridge] = owner

            def do_GET(self) -> None:
                self.bridge.forward(self)

            def do_POST(self) -> None:
                self.bridge.forward(self)

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        self.source = source
        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True, name="grid-goal-inference-bridge")

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def forward(self, handler: BaseHTTPRequestHandler) -> None:
        path = handler.path.split("?", 1)[0]
        if path.startswith("/v1/"):
            path = path[3:]
        if path not in _FORWARDED:
            self._error(handler, 404, "Grid bridge route not found")
            return
        try:
            length = int(handler.headers.get("Content-Length", "0"))
        except ValueError:
            self._error(handler, 400, "invalid Content-Length")
            return
        if length < 0 or length > _MAX_BODY:
            self._error(handler, 413, "request body is too large")
            return
        body = handler.rfile.read(length) if length else None
        headers = {
            "Authorization": f"Bearer {self.source.access_token}",
            "Accept": handler.headers.get("Accept", "application/json"),
        }
        if body is not None:
            headers["Content-Type"] = handler.headers.get("Content-Type", "application/json")
        started = False
        try:
            with httpx.Client(timeout=httpx.Timeout(30.0, read=None), trust_env=False) as client:
                with client.stream(
                        handler.command, self.source.base_url + path,
                        content=body, headers=headers) as response:
                    handler.send_response(response.status_code)
                    for name in ("content-type", "cache-control", "openai-processing-ms"):
                        if response.headers.get(name):
                            handler.send_header(name, response.headers[name])
                    handler.send_header("Connection", "close")
                    handler.end_headers()
                    started = True
                    for chunk in response.iter_raw():
                        handler.wfile.write(chunk)
                        handler.wfile.flush()
        except (httpx.HTTPError, OSError):
            if not started:
                with contextlib.suppress(OSError):
                    self._error(handler, 502, "source Grid inference failed")
        finally:
            handler.close_connection = True

    @staticmethod
    def _error(handler: BaseHTTPRequestHandler, status: int, message: str) -> None:
        payload = json.dumps({"error": {"message": message}}).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(payload)


@contextlib.contextmanager
def _target_home(path: Path):
    previous = os.environ.get("GRID_HOME")
    os.environ["GRID_HOME"] = str(path)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("GRID_HOME", None)
        else:
            os.environ["GRID_HOME"] = previous


def run(args: argparse.Namespace) -> int:
    if not 0 <= args.port <= 65535:
        raise SystemExit("--port must be between 0 and 65535")
    if args.max_tasks < 1:
        raise SystemExit("--max-tasks must be at least 1")
    source = _source_grid(args.source_grid)
    model = require_exact_model(source, args.model)
    target_home = Path(args.target_home).expanduser().resolve()
    if not (target_home / "credentials.toml").is_file():
        raise SystemExit(f"Target lab GRID_HOME is not configured: {target_home}")
    tasks_root = Path(args.tasks_root).expanduser().resolve()
    from cli import main as grid_main

    # A bridge endpoint exists only for this process's lifetime. A prior Ctrl-C or crash can leave
    # the detached provider advertising that now-dead loopback URL. `join --respawn` merges engine
    # definitions, so it would otherwise keep routing the model to the stale first endpoint. Own
    # this disposable target identity exclusively: unregister any prior provider before joining.
    with _target_home(target_home):
        if grid_main(["leave", args.target_grid]) != 0:
            raise SystemExit(f"Could not clear the previous {args.target_grid} bridge provider")

    bridge = Bridge(source, args.port)
    bridge.start()
    joined = False
    try:
        with _target_home(target_home):
            result = grid_main([
                "join", args.target_grid, "--at", bridge.base_url, "-m", model,
                "--tasks", "--respawn", "--name", args.name,
                "--max-tasks", str(args.max_tasks), "--tasks-root", str(tasks_root),
            ])
        if result != 0:
            return int(result)
        joined = True
        print("\nGrid inference bridge is ready:", flush=True)
        print(f"  source: {args.source_grid} / {model}", flush=True)
        print(f"  target: {args.target_grid} / {args.name}", flush=True)
        print(f"  local:  {bridge.base_url}", flush=True)
        print("  bearer: retained inside this process", flush=True)
        print("Keep this terminal open. Ctrl-C stops the bridge and unregisters this lab node.",
              flush=True)
        threading.Event().wait()
    except KeyboardInterrupt:
        return 130
    finally:
        if joined:
            with _target_home(target_home):
                # Best effort during shutdown: the important invariant is that a normal Ctrl-C
                # never leaves a detached provider pointing at a dead loopback bridge.
                with contextlib.suppress(Exception):
                    grid_main(["leave", args.target_grid])
        bridge.stop()
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Bridge existing Grid inference into Goal lab")
    result.add_argument("--source-grid", default="autonomous.ai")
    result.add_argument("--target-grid", default="goal-physical")
    result.add_argument("--target-home", required=True)
    result.add_argument("--model", required=True,
                        help="Exact, case-sensitive model id advertised by the source Grid")
    result.add_argument("--name", default="grid-goal-relay-host")
    result.add_argument("--tasks-root", default="/private/tmp/grid-goal-physical/work-relay-host")
    result.add_argument("--max-tasks", type=int, default=1)
    result.add_argument("--port", type=int, default=0,
                        help="Loopback bridge port (default: choose a free port)")
    return result


if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
