"""JSON-RPC client for `codex app-server --stdio`.

One long-lived codex process, one JSON message per line on stdin/stdout. Not quite JSON-RPC 2.0:
codex's own schema requires only {id, method, params} and carries no "jsonrpc" field, so this
sends none either.

It exists for the one thing `codex exec` cannot do — read the account's rate limits. The codex
seat's spec has no quota argv (seats/codex.py), so without this its quota gate reads permanently
unknown and no ceiling can ever be enforced.
"""
from __future__ import annotations

import datetime
import json
import os
import signal
import subprocess
import threading
import time
from collections import deque

from shared._version import __version__
from shared.agent import cli_seat
from shared.agent.cli_seat import QuotaSnapshot
from shared.agent.seats import codex

CLIENT_NAME = "grid"

# Sent back when the SERVER asks the client for something. -32601 is JSON-RPC "method not found".
UNSUPPORTED_REQUEST = -32601


class AppServerError(RuntimeError):
    """The app-server could not be started, died, or answered with an error."""


class AppServer:
    """A running app-server. One reader thread splits replies from notifications; `call` blocks
    for its own id, so several threads may call at once."""

    def __init__(self, proc, timeout=60.0):
        self.proc = proc
        self.timeout = timeout
        self.notifications = deque(maxlen=1000)  # bounded: a live thread pushes thousands
        self._replies = {}
        self._state = threading.Condition()
        self._write_lock = threading.Lock()
        self._next_id = 0
        self._closed = False
        self._stderr_tail = deque(maxlen=50)
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def call(self, method, params=None):
        """Send a request and wait for its reply. Raises AppServerError on error, timeout or death."""
        with self._state:
            self._next_id += 1
            request_id = self._next_id
        self._send({"id": request_id, "method": method, "params": params or {}})

        deadline = time.monotonic() + self.timeout
        with self._state:
            while request_id not in self._replies:
                if self._closed:
                    raise AppServerError(f"{method}: app-server exited{self._why()}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AppServerError(f"{method}: no reply within {self.timeout:.0f}s")
                self._state.wait(remaining)
            message = self._replies.pop(request_id)
        if "error" in message:
            raise AppServerError(f"{method}: {message['error']}")
        return message.get("result")

    def notify(self, method, params=None):
        """A message with no id, which the server never answers."""
        self._send({"method": method, "params": params or {}})

    def read_rate_limits(self):
        """Current quota, or None when unreadable. Never raises — unknown means keep serving,
        the same contract as `cli_seat.probe_quota`."""
        try:
            return snapshot_from_rate_limits(self.call("account/rateLimits/read", {}))
        except (AppServerError, OSError, ValueError):
            return None

    def start_thread(self, **options):
        """A new thread's id. Takes config, sandbox, ephemeral, cwd, model, baseInstructions,
        developerInstructions, approvalPolicy — None values are dropped rather than sent."""
        result = self.call("thread/start", {k: v for k, v in options.items() if v is not None})
        thread_id = ((result or {}).get("thread") or {}).get("id")
        if not thread_id:
            raise AppServerError(f"thread/start returned no thread id: {result}")
        return thread_id

    def start_turn(self, thread_id, text, effort=None):
        """Send one user turn. The ANSWER does not come back here — it arrives as `turn/*` and
        `item/*` notifications, which `drain_notifications` hands back.

        `effort` is per-turn, the way codex's own app-server schema has it — not session config,
        so it travels with THIS request rather than every turn a long-lived thread might run.
        Omitted (falsy) leaves the params byte-identical to before this field existed."""
        params = {"threadId": thread_id, "input": [{"type": "text", "text": text}]}
        if effort:
            params["effort"] = effort
        return self.call("turn/start", params)

    def drain_notifications(self):
        """Everything the server pushed since the last drain."""
        with self._state:
            drained = list(self.notifications)
            self.notifications.clear()
        return drained

    def stop(self, timeout=10.0):
        """Kill the whole process group, not just the parent — codex spawns children, and a
        terminate on the parent alone leaves them running. Mirrors media_runtime.stop_media_server."""
        proc = self.proc
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass
        if proc.poll() is not None:
            return
        try:
            if hasattr(os, "killpg"):
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                proc.terminate()
            proc.wait(timeout=timeout)
        except ProcessLookupError:
            return
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    def _read_stdout(self):
        for line in self.proc.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue  # codex prints non-JSON lines too; they are not errors
            if not isinstance(message, dict):
                continue
            if "result" in message or "error" in message:
                with self._state:
                    self._replies[message.get("id")] = message
                    self._state.notify_all()
            elif "id" in message:
                self._refuse(message["id"], message.get("method"))
            else:
                with self._state:
                    self.notifications.append(message)
        with self._state:  # stdout closed: the process is gone, so wake every waiting caller
            self._closed = True
            self._state.notify_all()

    def _read_stderr(self):
        """Drained, not ignored: an unread stderr pipe fills and blocks the child. The tail is
        kept because it is the only explanation when the server dies."""
        for line in self.proc.stderr:
            self._stderr_tail.append(line)

    def _refuse(self, request_id, method):
        """The server asked US for something — an approval, a tool call. A headless seat has
        nobody to ask, and silence would hang the turn forever, so refuse out loud."""
        try:
            self._send({"id": request_id,
                        "error": {"code": UNSUPPORTED_REQUEST,
                                  "message": f"{method}: this client answers no requests"}})
        except AppServerError:
            pass

    def _send(self, message):
        line = json.dumps(message) + "\n"
        with self._write_lock:
            try:
                self.proc.stdin.write(line)
                self.proc.stdin.flush()
            except (OSError, ValueError) as exc:  # broken or closed pipe: the child is gone
                raise AppServerError(f"app-server is not accepting input: {exc}") from exc

    def _why(self):
        tail = "".join(self._stderr_tail).strip()
        return f": {tail[-400:]}" if tail else ""


def start_app_server(home=None, binary=None, timeout=60.0):
    """Launch the app-server in the seat's own CODEX_HOME and complete the handshake.

    `initialize` must be the first message; `initialized` follows it the way MCP's does — codex's
    ClientNotification schema declares exactly that one notification.
    """
    executable = binary or cli_seat.seat_bin(codex.SPEC)
    if executable is None:
        raise AppServerError("The `codex` CLI was not found on PATH.")
    env = {**os.environ, "CODEX_HOME": str(home)} if home else cli_seat.ensure_home(codex.SPEC)
    try:
        proc = subprocess.Popen(
            [executable, "app-server", "--stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=env,
            start_new_session=True,  # its own process group, so stop() can take the children too
        )
    except OSError as exc:
        raise AppServerError(f"Could not run `{executable} app-server`: {exc}") from exc

    server = AppServer(proc, timeout=timeout)
    try:
        server.call("initialize", {"clientInfo": {"name": CLIENT_NAME, "version": __version__}})
        server.notify("initialized")
    except AppServerError:
        server.stop()
        raise
    return server


def read_quota(home=None, binary=None, timeout=60.0):
    """One reading from a throwaway app-server, for callers that want a number and no process.

    None when anything at all goes wrong, like `cli_seat.probe_quota` — a seat that cannot read
    its quota keeps serving.
    """
    server = None
    try:
        server = start_app_server(home=home, binary=binary, timeout=timeout)
        return server.read_rate_limits()
    except (AppServerError, OSError, ValueError):
        return None
    finally:
        if server is not None:
            server.stop()


# Notification methods, exactly as the server spells them (from its own ServerNotification schema —
# these are compared literally, not guessed at by substring).
_DELTA = "item/agentMessage/delta"          # params: {delta, itemId, threadId, turnId}
_ITEM_DONE = "item/completed"               # params: {item, threadId, turnId, completedAtMs}
_TURN_DONE = "turn/completed"               # params: {turn, threadId} — NOT usage; see below
_TOKENS = "thread/tokenUsage/updated"       # params: {tokenUsage: {last, total}, threadId, turnId}


def _lockdown_config():
    """The per-thread config, parsed from the SAME config.toml the seat writes for `codex exec`.

    Derived rather than duplicated on purpose: the two execution paths must lock down identically,
    and a second hand-written copy is how one of them quietly ends up looser than the other.
    """
    import tomllib

    return tomllib.loads(codex.CONFIG_TOML)


LOCKDOWN_CONFIG = _lockdown_config()


def serve(binary, prepared, timeout, on_delta=None):
    """Run one request through a throwaway app-server and return a SeatResult.

    The caller's system prompt replaces codex's own base prompt — see the comment at the call.

    `on_delta` receives text as it arrives. Codex is the only reason this exists: `codex exec`
    emits no incremental text at all, so streaming was impossible before the app-server.
    """
    from shared.agent.cli_seat import SeatError, SeatResult

    server = None
    try:
        server = start_app_server(binary=binary, timeout=timeout)
        thread_id = server.start_thread(
            config=LOCKDOWN_CONFIG,
            sandbox="read-only",
            ephemeral=True,
            model=prepared.model_alias,
            # The caller's prompt REPLACES codex's own base prompt. That replacement is the part
            # that matters: measured, leaving the vendor prompt in place and putting the caller's
            # text in `developerInstructions` instead still had codex answer "the workspace denied
            # the file-edit request" — it kept believing it had an editor. Both fields work once
            # base is replaced, so this uses the one field rather than a stub plus a second.
            baseInstructions=prepared.system_prompt or None,
        )
        # `prepared.effort` is resolved once, by cli_seat.prepare, from either the request's
        # `thinking` field or its `reasoning_effort` field — "" when the caller asked for neither,
        # which keeps this call's params identical to before either was read.
        server.start_turn(thread_id, prepared.prompt, effort=prepared.effort)
        text, tokens, duration_ms, failure = _collect(server, timeout, on_delta)
        if failure:
            raise SeatError(f"`codex` failed: {failure[:400]}")
        if not text:
            raise SeatError("`codex` produced no final message.")
        # camelCase here, unlike `codex exec --json`'s snake_case — the app-server speaks a
        # different dialect of the same numbers.
        return SeatResult(
            text=text,
            input_tokens=_int(tokens.get("inputTokens")) + _int(tokens.get("cachedInputTokens"))
            + _int(tokens.get("cacheWriteInputTokens")),
            output_tokens=_int(tokens.get("outputTokens")) + _int(tokens.get("reasoningOutputTokens")),
            cost_usd=0.0,  # codex reports tokens but no dollar figure
            duration_ms=duration_ms,
            session_id=thread_id,
            num_turns=1,
        )
    except AppServerError as exc:
        raise SeatError(str(exc)) from None
    finally:
        if server is not None:
            server.stop()


def _collect(server, timeout, on_delta):
    """Drain notifications until the turn ends. Returns (text, tokens, duration_ms, failure).

    Token counts do NOT arrive with the turn — `turn/completed` carries a Turn (id, status, error,
    durationMs), and usage comes separately on `thread/tokenUsage/updated`. So both are tracked and
    the last token reading before completion is the one that counts.
    """
    text, tokens, duration_ms, failure = "", {}, 0, ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for note in server.drain_notifications():
            method = note.get("method")
            params = note.get("params") or {}
            if method == _DELTA:
                chunk = params.get("delta") or ""
                if chunk:
                    text += chunk
                    if on_delta:
                        on_delta(chunk)
            elif method == _ITEM_DONE:
                item = params.get("item") or {}
                # The completed item is authoritative — deltas may be partial, or absent entirely.
                if item.get("type") == "agentMessage" and item.get("text"):
                    text = item["text"]
            elif method == _TOKENS:
                # `last` is this turn; `total` is the whole thread, which for an ephemeral
                # single-turn thread is the same thing but would over-count if that ever changed.
                usage = (params.get("tokenUsage") or {}).get("last") or {}
                if usage:
                    tokens = usage
            elif method == _TURN_DONE:
                turn = params.get("turn") or {}
                duration_ms = _int(turn.get("durationMs"))
                error = turn.get("error")
                if error:
                    failure = str(error.get("message") or error)
                elif str(turn.get("status") or "").lower() not in ("", "completed", "success"):
                    failure = f"turn ended with status {turn.get('status')!r}"
                return text, tokens, duration_ms, failure
        time.sleep(0.05)
    return text, tokens, duration_ms, failure or f"no turn completion within {timeout:.0f}s"


def _int(value):
    return int(value) if isinstance(value, (int, float)) else 0


def snapshot_from_rate_limits(payload):
    """An `account/rateLimits/read` result -> QuotaSnapshot, or None when it names no window.

    `primary` is the short rolling window and `secondary` the long one, so they land on
    session/week the way the other seats report them. Codex sends whole-number percentages.
    """
    limits = payload.get("rateLimits") if isinstance(payload, dict) else None
    if not isinstance(limits, dict):
        return None
    primary = limits.get("primary") if isinstance(limits.get("primary"), dict) else {}
    secondary = limits.get("secondary") if isinstance(limits.get("secondary"), dict) else {}
    if not primary and not secondary:
        return None
    # The vendor says the account is blocked: report full windows so any ceiling refuses, exactly
    # as claude's blocked branch does. The percentages can still read low when spend control tripped.
    blocked = bool(limits.get("rateLimitReachedType")) or bool(limits.get("spendControlReached"))
    return QuotaSnapshot(
        session_pct=100 if blocked else _pct(primary.get("usedPercent")),
        week_pct=100 if blocked else _pct(secondary.get("usedPercent")),
        session_reset=_reset_text(primary),
        week_reset=_reset_text(secondary),
    )


def _pct(value):
    """A missing window reads as 0 used — `secondary` is null on plans with one window only."""
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return 0


def _reset_text(window):
    """`resetsAt` is a unix timestamp, rendered the way seats/claude.py `_reset_text` renders its
    own, so both seats hand a human the same kind of string."""
    stamp = window.get("resetsAt")
    if not stamp:
        return ""
    try:
        return datetime.datetime.fromtimestamp(int(stamp)).strftime("%b %-d at %-I:%M%p").lower()
    except (TypeError, ValueError, OSError):
        return ""
