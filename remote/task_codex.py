"""Run one native Codex Goal turn inside a Grid task worktree.

All durable state lives below ``.grid/agent/codex``.  ``task_repo.push_transcript`` publishes that
tree to the conversation side-ref, so another provider can resume without sharing a filesystem.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Protocol
from urllib.parse import urljoin, urlparse

import httpx

from . import task_agent, task_stream
from .task_codex_proxy import InferenceProxy

AGENT_DIR = Path(".grid") / "agent" / "codex"
STATE_FILE = "goal-state.json"
HOME_DIR = "home"
_MAX_TOOL_RESULT = 64 * 1024
_MAX_RECORDED_TOOL_VALUE = 24 * 1024
_SECRET_FIELD_PARTS = ("authorization", "api_key", "apikey", "password", "secret", "token")
# Oldest binary measured against the stable thread/goal/{set,get,clear} and thread/resume contract.
# A binary's presence alone is not a capability proof: advertising an older Codex strands the Goal
# after it has already been leased to that node.
MIN_DISTRIBUTED_GOAL_VERSION = (0, 150, 1)
_VERSION_PATTERN = re.compile(r"\Acodex-cli (\d+)\.(\d+)\.(\d+)")
_VERSION_TIMEOUT_SECONDS = 10
_VERSION_CACHE: dict[tuple[str, int, int], tuple[int, int, int]] = {}


def _recorded_tool_value(value: Any) -> Any:
    """Redact credential-shaped fields and bound one durable training/audit payload."""
    def redact(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                str(key): ("[REDACTED]" if any(part in str(key).lower()
                                               for part in _SECRET_FIELD_PARTS)
                           else redact(child))
                for key, child in item.items()
            }
        if isinstance(item, list):
            return [redact(child) for child in item]
        return item

    safe = redact(value)
    encoded = json.dumps(safe, ensure_ascii=False, sort_keys=True, default=str)
    raw = encoded.encode("utf-8")
    if len(raw) <= _MAX_RECORDED_TOOL_VALUE:
        return safe
    prefix = raw[:_MAX_RECORDED_TOOL_VALUE].decode("utf-8", "ignore")
    return {"truncated": True, "json_prefix": prefix + task_stream.TRUNCATION_MARKER}


class CodexGoalError(RuntimeError):
    pass


@dataclass(frozen=True)
class GoalSlice:
    status: str
    thread_id: str
    turns_completed: int
    tokens_used: int
    time_used_seconds: int
    output: str | None = None


@dataclass(frozen=True)
class GridInference:
    base_url: str
    token: str


class ProcessLike(Protocol):
    stdin: IO[str] | None
    stdout: IO[str] | None
    returncode: int | None

    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...
    def kill(self) -> None: ...


ProcessFactory = Callable[[list[str], dict[str, str], Path], ProcessLike]


def _spawn(argv: list[str], env: dict[str, str], cwd: Path) -> ProcessLike:
    return subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, cwd=cwd, env=env, start_new_session=True)


def _version_cache_key(binary: str) -> tuple[str, int, int] | None:
    try:
        info = os.stat(binary)
    except OSError:
        return None
    return (binary, info.st_mtime_ns, info.st_size)


def _binary_version(binary: str) -> tuple[int, int, int]:
    key = _version_cache_key(binary)
    cached = _VERSION_CACHE.get(key) if key is not None else None
    if cached is not None:
        return cached
    try:
        proc = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, errors="replace",
            timeout=_VERSION_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CodexGoalError(f"could not ask {binary} for its version: {exc}") from exc
    reported = (proc.stdout or proc.stderr or "").strip()
    match = _VERSION_PATTERN.match(reported)
    if match is None:
        raise CodexGoalError(
            f"{binary} --version said {reported!r}, which is not a Codex version Grid can check")
    version = (int(match[1]), int(match[2]), int(match[3]))
    if key is not None:
        _VERSION_CACHE[key] = version
    return version


def _require_distributed_goal_version(binary: str) -> str:
    version = _binary_version(binary)
    if version < MIN_DISTRIBUTED_GOAL_VERSION:
        required = ".".join(str(part) for part in MIN_DISTRIBUTED_GOAL_VERSION)
        found = ".".join(str(part) for part in version)
        raise CodexGoalError(
            f"Codex {found} cannot resume distributed native Goals; install {required} or newer")
    return binary


def supports_distributed_goals(binary: str) -> bool:
    """Whether this exact executable is safe to advertise to the Goal scheduler."""
    try:
        _require_distributed_goal_version(binary)
    except CodexGoalError:
        return False
    return True


def resolve_binary() -> str:
    binary = shutil.which("codex")
    if not binary:
        raise CodexGoalError("Codex is not installed on this provider")
    return _require_distributed_goal_version(binary)


def available() -> bool:
    binary = shutil.which("codex")
    return bool(binary and supports_distributed_goals(binary))


def state_dir(workspace: Path) -> Path:
    path = workspace / AGENT_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _load_state(workspace: Path) -> dict[str, Any]:
    path = state_dir(workspace) / STATE_FILE
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        raise CodexGoalError(f"cannot read the distributed Codex checkpoint: {exc}") from exc
    if not isinstance(value, dict):
        raise CodexGoalError("the distributed Codex checkpoint is not an object")
    return value


def _write_state(workspace: Path, value: dict[str, Any]) -> None:
    directory = state_dir(workspace)
    path = directory / STATE_FILE
    temporary = directory / f".{STATE_FILE}.{os.getpid()}.tmp"
    payload = json.dumps(value, separators=(",", ":"), ensure_ascii=False) + "\n"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class ToolExecutor:
    """Execute the Goal's explicit observe/act HTTP capabilities and publish an audit event."""

    def __init__(self, tools: list[dict[str, Any]], *, publish: Callable[..., None],
                 inference: GridInference, scope: str):
        self.tools = {str(tool.get("name")): tool for tool in tools if isinstance(tool, dict)}
        self.publish = publish
        self.inference = inference
        self.scope = scope

    def specs(self) -> list[dict[str, Any]]:
        answer = []
        for name, tool in self.tools.items():
            if not name or tool.get("mode") not in ("observe", "act", "verify"):
                continue
            schema = tool.get("input_schema")
            if not isinstance(schema, dict):
                schema = {"type": "object"}
            answer.append({
                "type": "function", "name": name,
                "description": f"[{str(tool.get('mode')).upper()}] {tool.get('description') or name}",
                "inputSchema": schema,
            })
        return answer

    def call(self, name: str, arguments: Any, call_id: str) -> dict[str, Any]:
        tool = self.tools.get(name)
        if tool is None or not isinstance(arguments, dict):
            return self._result(False, {"error": "tool is not allowed or arguments are not an object"})
        mode = str(tool.get("mode") or "")
        http = tool.get("http") if isinstance(tool.get("http"), dict) else {}
        url = str(http.get("url") or "")
        method = str(http.get("method") or "POST").upper()
        if not url or method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            return self._result(False, {"error": "tool HTTP configuration is invalid"})
        if url.startswith("/"):
            url = urljoin(self.inference.base_url.rstrip("/") + "/", url.lstrip("/"))
        headers = {"Accept": "application/json"}
        # A Grid credential may only be sent back to the same relay origin. Arbitrary internal HTTP
        # tools are supported without auth; external secret distribution is not smuggled through git.
        if http.get("auth") == "grid":
            if urlparse(url).netloc != urlparse(self.inference.base_url).netloc:
                return self._result(False, {"error": "grid auth is restricted to the selected relay"})
            headers["Authorization"] = f"Bearer {self.inference.token}"
            configured_headers = http.get("headers")
            if isinstance(configured_headers, dict):
                # The relay injects this lease fence for its built-in subgoal action. No arbitrary
                # header forwarding: user manifests must not be able to smuggle credentials or
                # override content negotiation through the provider.
                turn_id = configured_headers.get("X-Grid-Goal-Turn")
                if isinstance(turn_id, str) and turn_id:
                    headers["X-Grid-Goal-Turn"] = turn_id
        if mode == "act":
            canonical = json.dumps([self.scope, name, arguments], sort_keys=True,
                                   separators=(",", ":"), ensure_ascii=False).encode()
            headers["Idempotency-Key"] = "grid-goal-" + hashlib.sha256(canonical).hexdigest()
        record_full = tool.get("record") == "full"
        self.publish(
            f"goal.{mode}.request", tool=name, call_id=call_id,
            **({"arguments": _recorded_tool_value(arguments)} if record_full else {}))
        try:
            timeout = float(http.get("timeout_seconds") or 30)
            with httpx.Client(timeout=min(300.0, max(0.1, timeout))) as client:
                response = client.request(method, url,
                                          params=arguments if method == "GET" else None,
                                          json=None if method == "GET" else arguments,
                                          headers=headers)
            try:
                body: Any = response.json()
            except ValueError:
                body = response.text[:_MAX_TOOL_RESULT]
            result = {"status_code": response.status_code, "body": body}
            success = 200 <= response.status_code < 300
        except httpx.HTTPError as exc:
            success, result = False, {"error": str(exc)}
        self.publish(
            f"goal.{mode}.result", tool=name, call_id=call_id, success=success,
            **({"result": _recorded_tool_value(result)} if record_full else {}))
        return self._result(success, result)

    @staticmethod
    def _result(success: bool, value: Any) -> dict[str, Any]:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if len(text) > _MAX_TOOL_RESULT:
            text = text[:_MAX_TOOL_RESULT] + task_stream.TRUNCATION_MARKER
        return {"success": success,
                "contentItems": [{"type": "inputText", "text": text}]}


class Rpc:
    def __init__(self, process: ProcessLike, *, timeout: float, tools: ToolExecutor,
                 publish: Callable[..., None]):
        if process.stdin is None or process.stdout is None:
            raise CodexGoalError("Codex app-server did not expose stdio")
        self.process, self.stdin, self.stdout = process, process.stdin, process.stdout
        self.deadline = time.monotonic() + timeout
        self.tools, self.publish = tools, publish
        self.next_id = 1
        self.responses: dict[int, dict[str, Any]] = {}
        self.completed: dict[str, Any] | None = None
        self.pause_id: int | None = None
        self.tokens = 0
        self.lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        # Codex writes diagnostics on stderr. Leaving that PIPE undrained eventually blocks the
        # app-server in `write`, which looks exactly like a model turn that stopped making progress
        # while the node keeps renewing its lease. Drain it independently of the JSON-RPC stream.
        stderr = getattr(process, "stderr", None)
        if stderr is not None:
            threading.Thread(target=self._read_stderr, args=(stderr,), daemon=True).start()

    def _read(self) -> None:
        try:
            for line in self.stdout:
                self.lines.put(line)
        finally:
            self.lines.put(None)

    def _read_stderr(self, stderr: IO[str]) -> None:
        try:
            for line in stderr:
                text = task_stream.redact(line.rstrip())
                if text:
                    self.publish("task.stderr", text=text)
        except (OSError, ValueError):
            return

    def send(self, method: str, params: dict[str, Any]) -> int:
        request_id = self.next_id
        self.next_id += 1
        self._write({"id": request_id, "method": method, "params": params})
        return request_id

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        self._write(message)

    def _write(self, message: dict[str, Any]) -> None:
        try:
            self.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            self.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise CodexGoalError(f"Codex app-server closed its input: {exc}") from exc

    def wait(self, request_id: int) -> Any:
        while request_id not in self.responses:
            self._one()
        response = self.responses.pop(request_id)
        if "error" in response:
            error = response["error"]
            raise CodexGoalError(str(error.get("message", error) if isinstance(error, dict) else error))
        return response.get("result")

    def wait_completed(self) -> dict[str, Any]:
        while self.completed is None:
            self._one()
        return self.completed

    def _one(self) -> None:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise CodexGoalError("Codex Goal turn timed out")
        try:
            line = self.lines.get(timeout=remaining)
        except queue.Empty:
            raise CodexGoalError("Codex Goal turn timed out") from None
        if line is None:
            raise CodexGoalError(
                f"Codex app-server exited before turn completion (exit {self.process.poll()})")
        try:
            message = json.loads(line)
        except ValueError as exc:
            raise CodexGoalError(f"Codex app-server emitted invalid JSON: {exc}") from exc
        if not isinstance(message, dict):
            return
        if "id" in message and ("result" in message or "error" in message):
            self.responses[message["id"]] = message
            return
        if "id" in message and "method" in message:
            request_id = message["id"]
            if message["method"] != "item/tool/call":
                self._write({"id": request_id, "error": {"code": -32601, "message": "unsupported"}})
                return
            params = message.get("params") or {}
            result = self.tools.call(str(params.get("tool") or ""), params.get("arguments"),
                                     str(params.get("callId") or request_id))
            self._write({"id": request_id, "result": result})
            return
        method, params = message.get("method"), message.get("params") or {}
        if method == "thread/tokenUsage/updated":
            total = ((params.get("tokenUsage") or {}).get("total") or {}).get("totalTokens")
            if isinstance(total, int) and not isinstance(total, bool):
                self.tokens = max(self.tokens, total)
        elif method == "turn/started" and self.pause_id is None:
            thread_id = str(params.get("threadId") or "")
            if thread_id:
                # Grid owns continuation. Pause suppresses Codex's automatic next turn after this one.
                self.pause_id = self.send("thread/goal/set", {"threadId": thread_id,
                                                               "status": "paused"})
        elif method == "turn/completed":
            self.completed = params
        elif isinstance(method, str) and method.startswith(("turn/", "item/")):
            self.publish("goal.codex.event", method=method)


def run_slice(job: dict[str, Any], workspace: Path, *, inference: GridInference,
              executable: str, timeout: float, publish: Callable[..., None],
              on_spawn: Callable[[ProcessLike], None] | None = None,
              process_factory: ProcessFactory = _spawn) -> GoalSlice:
    goal = job.get("goal")
    if not isinstance(goal, dict):
        raise CodexGoalError("the relay supplied no Goal metadata")
    for field in ("objective", "done_when", "model"):
        if not isinstance(goal.get(field), str) or not goal[field].strip():
            raise CodexGoalError(f"the relay supplied no usable Goal {field}")

    state = _load_state(workspace)
    thread_id = str(state.get("thread_id") or "")
    turns_before = max(_counter(goal, "turns_completed", "Goal metadata"),
                       _counter(state, "turns_completed", "distributed checkpoint"))
    tokens_before = max(_counter(goal, "tokens_used", "Goal metadata"),
                       _counter(state, "tokens_used", "distributed checkpoint"))
    time_before = max(_counter(goal, "time_used_seconds", "Goal metadata"),
                     _counter(state, "time_used_seconds", "distributed checkpoint"))
    tools = ToolExecutor(goal.get("tools") if isinstance(goal.get("tools"), list) else [],
                         publish=publish, inference=inference,
                         scope=f"{job.get('conversation_id')}:{turns_before + 1}")

    proxy = InferenceProxy(
        inference.base_url.rstrip("/") + "/relay/v1", inference.token,
        turn_id=str(job.get("task_id") or "") or None,
        conversation_id=str(job.get("conversation_id") or "") or None)
    proxy.start()
    process: ProcessLike | None = None
    started = time.monotonic()
    try:
        env = task_agent.child_env(workspace=workspace)
        codex_home = state_dir(workspace) / HOME_DIR
        codex_home.mkdir(parents=True, exist_ok=True)
        env["CODEX_HOME"] = str(codex_home)
        env["GRID_GOAL_API_KEY"] = proxy.child_token
        process = process_factory([executable, "app-server", "--listen", "stdio://"], env, workspace)
        if on_spawn is not None:
            on_spawn(process)
        rpc = Rpc(process, timeout=timeout, tools=tools, publish=publish)
        rpc.wait(rpc.send("initialize", {
            "clientInfo": {"name": "grid-goal", "title": "Grid Goal", "version": "0.1"},
            "capabilities": {"experimentalApi": True},
        }))
        rpc.notify("initialized")
        thread_params = {
            "cwd": str(workspace), "model": goal["model"], "modelProvider": "grid",
            "approvalPolicy": "never", "sandbox": "workspace-write",
            "dynamicTools": tools.specs(),
            "config": {"model_providers": {"grid": {
                "name": "Grid", "base_url": proxy.base_url, "env_key": "GRID_GOAL_API_KEY",
                "wire_api": "responses",
            }}},
        }
        if thread_id:
            resume = dict(thread_params)
            resume.pop("dynamicTools", None)
            rpc.wait(rpc.send("thread/resume", {"threadId": thread_id, **resume}))
        else:
            result = rpc.wait(rpc.send("thread/start", thread_params)) or {}
            thread_id = str((result.get("thread") or {}).get("id") or "")
            if not thread_id:
                raise CodexGoalError("Codex thread/start returned no thread id")
        set_goal: dict[str, Any] = {"threadId": thread_id, "status": "active"}
        if turns_before == 0:
            set_goal.update({
                "objective": f"{goal['objective'].strip()}\n\nDone when: {goal['done_when'].strip()}",
                "tokenBudget": goal.get("token_budget"),
            })
        rpc.wait(rpc.send("thread/goal/set", set_goal))
        completed = rpc.wait_completed()
        if rpc.pause_id is not None:
            rpc.wait(rpc.pause_id)
        native = rpc.wait(rpc.send("thread/goal/get", {"threadId": thread_id})) or {}
        native_goal = native.get("goal") or {}
        turn = completed.get("turn") or {}
        if turn.get("status") == "failed":
            raise CodexGoalError(str(turn.get("error") or "Codex Goal turn failed"))
        status = _public_status(str(native_goal.get("status") or "paused"))
        if status == "paused":
            status = "active"
        measured_tokens = max(tokens_before, rpc.tokens,
                              _counter(native_goal, "tokensUsed", "native Codex Goal"))
        budget = goal.get("token_budget")
        if status == "active" and isinstance(budget, int) and measured_tokens >= budget:
            status = "budget_limited"
        elapsed = round(time.monotonic() - started)
        measured_time = max(time_before + elapsed,
                            _counter(native_goal, "timeUsedSeconds", "native Codex Goal"))
        result = GoalSlice(status, thread_id, turns_before + 1, measured_tokens, measured_time,
                           output=str(turn.get("output") or "") or None)
        _write_state(workspace, {
            "version": 1, "thread_id": result.thread_id, "status": result.status,
            "turns_completed": result.turns_completed, "tokens_used": result.tokens_used,
            "time_used_seconds": result.time_used_seconds,
        })
        publish("goal.slice.completed", status=result.status,
                turns_completed=result.turns_completed, tokens_used=result.tokens_used)
        return result
    finally:
        if process is not None:
            _stop(process)
        proxy.stop()


def _stop(process: ProcessLike) -> None:
    try:
        if process.stdin is not None:
            process.stdin.close()
    except (OSError, ValueError):
        pass
    if process.poll() is not None:
        return
    try:
        pid = getattr(process, "pid", None)
        if pid and hasattr(os, "killpg"):
            os.killpg(pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=5)
    except (ProcessLookupError, ChildProcessError):
        return
    except (subprocess.TimeoutExpired, TimeoutError):
        process.kill()
        try:
            process.wait(timeout=5)
        except (subprocess.TimeoutExpired, TimeoutError):
            pass


def _public_status(native: str) -> str:
    return {"active": "active", "paused": "paused", "blocked": "blocked",
            "usageLimited": "usage_limited", "budgetLimited": "budget_limited",
            "complete": "complete"}.get(native, "failed")


def _counter(source: dict[str, Any], field: str, label: str) -> int:
    value = source.get(field, 0)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CodexGoalError(f"the {label} has an invalid {field}")
    return value
