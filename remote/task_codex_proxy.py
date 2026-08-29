"""Short-lived credential boundary for a distributed Codex Goal worker."""
from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import httpx


class InferenceProxy:
    """Expose native agent inference dialects on loopback; keep Grid's bearer out of children."""

    def __init__(self, upstream_base: str, upstream_token: str, *,
                 turn_id: str | None = None, conversation_id: str | None = None):
        self.upstream_base = upstream_base.rstrip("/")
        self.upstream_token = upstream_token
        self.turn_id = turn_id
        self.conversation_id = conversation_id
        self.child_token = secrets.token_urlsafe(32)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"
            proxy: ClassVar[InferenceProxy] = owner

            def do_POST(self):
                self.proxy._forward(self)

            def log_message(self, _format, *_args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    @property
    def anthropic_base_url(self) -> str:
        # Claude Code appends `/v1/messages` itself.
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _forward(self, handler: BaseHTTPRequestHandler) -> None:
        supplied = handler.headers.get("Authorization", "").removeprefix("Bearer ")
        if not secrets.compare_digest(supplied, self.child_token):
            self._error(handler, 401, "invalid Goal inference token")
            return
        path = handler.path.split("?", 1)[0]
        destinations = {
            "/responses": "responses",
            "/v1/responses": "responses",
            "/v1/messages": "messages",
        }
        destination = destinations.get(path)
        if destination is None:
            self._error(handler, 404, "Goal proxy only serves /responses and /v1/messages")
            return
        try:
            length = int(handler.headers.get("Content-Length", "0"))
        except ValueError:
            self._error(handler, 400, "invalid Content-Length")
            return
        if not 1 <= length <= 32 * 1024 * 1024:
            self._error(handler, 413, "request body must be between 1 byte and 32 MiB")
            return

        body = handler.rfile.read(length)
        headers = self._upstream_headers(handler)
        started = False
        try:
            with httpx.Client(timeout=httpx.Timeout(30.0, read=None)) as client, client.stream(
                "POST", f"{self.upstream_base}/{destination}", content=body, headers=headers
            ) as response:
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
        except (httpx.HTTPError, OSError) as exc:
            if not started:
                try:
                    self._error(handler, 502, f"Grid inference proxy failed: {exc}")
                except OSError:
                    pass
        finally:
            handler.close_connection = True

    def _upstream_headers(self, handler: BaseHTTPRequestHandler) -> dict[str, str]:
        """Bind every native-model request to the durable Grid Goal turn that caused it."""
        headers = {
            "Authorization": f"Bearer {self.upstream_token}",
            "Content-Type": handler.headers.get("Content-Type", "application/json"),
            "Accept": handler.headers.get("Accept", "text/event-stream"),
        }
        if self.turn_id:
            headers["X-Request-Id"] = self.turn_id
        if self.conversation_id:
            headers["X-Grid-Conversation"] = self.conversation_id
        return headers

    @staticmethod
    def _error(handler: BaseHTTPRequestHandler, status: int, message: str) -> None:
        payload = json.dumps({"error": {"message": message}}).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(payload)
