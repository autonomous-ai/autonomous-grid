"""Short-lived credential boundary for a distributed Codex Goal worker."""
from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections.abc import Callable
from typing import ClassVar

import httpx


# The relay gives an inference transaction 600 seconds to settle.  A little extra room keeps the
# proxy from racing that authoritative deadline, while still bounding a half-open HTTP stream well
# below the worker's one-hour native-agent timeout.  ``read=None`` used to let a tunnel or relay
# disappear after sending response headers and pin the Goal attempt forever.
_UPSTREAM_CONNECT_TIMEOUT_SECONDS = 30.0
_UPSTREAM_READ_TIMEOUT_SECONDS = 660.0


class InferenceProxy:
    """Expose native agent inference dialects on loopback; keep Grid's bearer out of children."""

    def __init__(self, upstream_base: str, upstream_token: str | Callable[[], str], *,
                 refresh_token: Callable[[str], bool] | None = None,
                 turn_id: str | None = None, conversation_id: str | None = None,
                 claim_id: str | None = None, upstream_model: str | None = None):
        self.upstream_base = upstream_base.rstrip("/")
        self.upstream_token = upstream_token
        self.refresh_token = refresh_token
        self.turn_id = turn_id
        self.conversation_id = conversation_id
        self.claim_id = claim_id
        # Claude Code validates its local model selector against Anthropic's known model names
        # before sending a request. A Grid model may be Qwen, Gemma, or any company-local name, so
        # the child uses a native Claude alias and this credential boundary substitutes the exact
        # immutable Goal model only on the upstream Messages request.
        self.upstream_model = upstream_model
        self.child_token = secrets.token_urlsafe(32)
        # Claude Code collapses many gateway failures into "selected model may not exist". Keep a
        # secret-free server-side reason so the durable Goal retry event says whether the boundary
        # rejected auth, routing, or the upstream request. Never retain a response body here.
        self.last_failure: str | None = None
        self._failure_lock = threading.Lock()
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
        self._set_failure(None)
        supplied = handler.headers.get("Authorization", "").removeprefix("Bearer ")
        if not secrets.compare_digest(supplied, self.child_token):
            self._set_failure("the native harness did not present its loopback Goal token")
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
            self._set_failure(f"the native harness requested unsupported path {path!r}")
            self._error(handler, 404, "Goal proxy only serves /responses and /v1/messages")
            return
        try:
            length = int(handler.headers.get("Content-Length", "0"))
        except ValueError:
            self._set_failure("the native harness sent an invalid Content-Length")
            self._error(handler, 400, "invalid Content-Length")
            return
        if not 1 <= length <= 32 * 1024 * 1024:
            self._set_failure("the native harness request body was outside the 1-byte-to-32-MiB limit")
            self._error(handler, 413, "request body must be between 1 byte and 32 MiB")
            return

        body = handler.rfile.read(length)
        try:
            body = self._upstream_body(destination, body)
        except (TypeError, ValueError, UnicodeDecodeError):
            self._set_failure("the native harness sent an invalid JSON inference body")
            self._error(handler, 400, "invalid Goal inference request body")
            return
        started = False
        try:
            with httpx.Client(timeout=httpx.Timeout(
                    _UPSTREAM_CONNECT_TIMEOUT_SECONDS,
                    read=_UPSTREAM_READ_TIMEOUT_SECONDS)) as client:
                for attempt in range(2):
                    token = self._current_upstream_token()
                    headers = self._upstream_headers(handler, token=token)
                    with client.stream(
                        "POST", f"{self.upstream_base}/{destination}", content=body,
                        headers=headers,
                    ) as response:
                        # An authentication refusal happens before inference begins, so replaying
                        # the held request body once is safe. The provider state's refresh method
                        # is compare-and-swap aware: concurrent proxy, poll and heartbeat threads
                        # either perform one refresh or adopt the token that already won.
                        if (response.status_code == 401 and attempt == 0
                                and self._refresh_upstream_token(token)):
                            continue
                        if response.status_code >= 400:
                            self._set_failure(
                                f"Grid inference returned HTTP {response.status_code} "
                                f"for /{destination}")
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
                        break
        except (httpx.HTTPError, OSError) as exc:
            self._set_failure(
                f"Grid inference transport failed for /{destination}: {type(exc).__name__}")
            if not started:
                try:
                    self._error(handler, 502, f"Grid inference proxy failed: {exc}")
                except OSError:
                    pass
        finally:
            handler.close_connection = True

    def _set_failure(self, reason: str | None) -> None:
        with self._failure_lock:
            self.last_failure = reason

    def _current_upstream_token(self) -> str:
        token = self.upstream_token() if callable(self.upstream_token) else self.upstream_token
        return str(token)

    def _upstream_body(self, destination: str, body: bytes) -> bytes:
        """Map Claude's recognized local alias to the relay's exact Grid model."""
        if destination != "messages" or self.upstream_model is None:
            return body
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError("Messages request must be an object")
        value["model"] = self.upstream_model
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    def _refresh_upstream_token(self, stale_token: str) -> bool:
        if self.refresh_token is None:
            return False
        try:
            return bool(self.refresh_token(stale_token))
        except (Exception, SystemExit):
            return False

    def _upstream_headers(self, handler: BaseHTTPRequestHandler, *,
                          token: str | None = None) -> dict[str, str]:
        """Bind every native-model request to the durable Grid Goal turn that caused it."""
        headers = {
            "Authorization": f"Bearer {token if token is not None else self._current_upstream_token()}",
            "Content-Type": handler.headers.get("Content-Type", "application/json"),
            "Accept": handler.headers.get("Accept", "text/event-stream"),
        }
        if self.turn_id:
            headers["X-Request-Id"] = self.turn_id
        if self.conversation_id:
            headers["X-Grid-Conversation"] = self.conversation_id
        if self.claim_id:
            headers["X-Grid-Task-Claim"] = self.claim_id
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
