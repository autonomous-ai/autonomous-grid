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
import tempfile
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
_MAX_TOOL_ARGUMENT_BYTES = 64 * 1024
_MAX_TOOL_HTTP_BYTES = 64 * 1024
_MAX_TOOL_JSON_DEPTH = 64
_MAX_ALLOWED_TOOL_ORIGINS = 32
_MAX_RECORDED_TOOL_VALUE = 24 * 1024
_SECRET_FIELD_PARTS = ("authorization", "api_key", "apikey", "password", "secret", "token")
# Oldest binary measured against the stable thread/goal/{set,get,clear} and thread/resume contract.
# A binary's presence alone is not a capability proof: advertising an older Codex strands the Goal
# after it has already been leased to that node.
MIN_DISTRIBUTED_GOAL_VERSION = (0, 150, 1)
_VERSION_PATTERN = re.compile(r"\Acodex-cli (\d+)\.(\d+)\.(\d+)")
_VERSION_TIMEOUT_SECONDS = 10
BinaryRevision = tuple[str, int, int, int, int, int, int]
_VERSION_CACHE: dict[BinaryRevision, tuple[int, int, int]] = {}
_PROTOCOL_TIMEOUT_SECONDS = 15
_PROTOCOL_SCHEMA_MAX_BYTES = 2 * 1024 * 1024
# These are the exact request/notification methods used below.  Codex app-server is explicitly
# experimental, so a semver floor by itself cannot prove that a newer binary still implements the
# protocol Grid is about to lease a durable Goal to.
_REQUIRED_PROTOCOL_METHODS = frozenset({
    "initialize", "initialized", "thread/start", "thread/resume", "thread/goal/set",
    "thread/goal/get", "thread/tokenUsage/updated", "turn/started", "turn/completed",
    "item/tool/call",
})
_PROTOCOL_CACHE: dict[BinaryRevision, tuple[bool, str]] = {}
_INTERNAL_GOAL_TOOLS = frozenset({"grid_spawn_subgoal"})


def _tool_json_depth_is_safe(value: Any) -> bool:
    """Bound recursive JSON before serializers/redaction can exhaust the worker's Python stack."""
    # Root container is level one. This makes the constant match how people count JSON nesting and
    # avoids an off-by-one where 65 nested arrays were admitted under a 64-level ceiling.
    stack = [(value, 1)]
    seen: set[int] = set()
    while stack:
        item, depth = stack.pop()
        if isinstance(item, (dict, list, tuple)):
            if depth > _MAX_TOOL_JSON_DEPTH or id(item) in seen:
                # Aliases and cycles cannot arrive through JSON-RPC. Rejecting both keeps this
                # boundary total for direct callers too, without maintaining an active-path DFS.
                return False
            seen.add(id(item))
            children = item.values() if isinstance(item, dict) else item
            stack.extend((child, depth + 1) for child in children)
    return True


def _http_origin(value: str) -> str | None:
    """Return a normalized exact HTTP origin, excluding ambient URL credentials."""
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if (scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None):
        return None
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    if ":" in host:
        host = f"[{host}]"
    default_port = 80 if scheme == "http" else 443
    suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{scheme}://{host}{suffix}"


def _allowed_goal_tool_origins() -> frozenset[str]:
    """Parse operator-approved business API origins; invalid/path entries fail closed."""
    answer: set[str] = set()
    for value in os.getenv("GRID_GOAL_TOOL_ORIGINS", "").split(","):
        candidate = value.strip()
        if not candidate:
            continue
        parsed = urlparse(candidate)
        origin = _http_origin(candidate)
        if (origin is not None and parsed.path in ("", "/")
                and not parsed.params and not parsed.query and not parsed.fragment):
            answer.add(origin)
            if len(answer) >= _MAX_ALLOWED_TOOL_ORIGINS:
                break
    return frozenset(answer)


def goal_tool_origin_capabilities() -> set[str]:
    """Opaque claim capabilities bind scheduling to this node's exact API allowlist."""
    return {
        "tool_origin." + hashlib.sha256(origin.encode("utf-8")).hexdigest()[:32]
        for origin in _allowed_goal_tool_origins()
    }


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


class CodexProtocolError(CodexGoalError):
    """The installed app-server cannot safely execute Grid's native Goal contract."""


class MissingRollout(CodexGoalError):
    """A promised native thread has no portable rollout in the copied checkpoint."""


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
    token: str | Callable[[], str]
    refresh: Callable[[str], bool] | None = None
    claim_id: str | None = None

    def current_token(self) -> str:
        return str(self.token() if callable(self.token) else self.token)

    def refresh_token(self, stale_token: str) -> bool:
        if self.refresh is None:
            return False
        try:
            return bool(self.refresh(stale_token))
        except (Exception, SystemExit):
            return False


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


def _version_cache_key(binary: str) -> BinaryRevision | None:
    try:
        info = os.stat(binary)
    except OSError:
        return None
    # Include identity, permissions and ctime as well as content-shaped fields.  An operator fixing
    # execute permissions must clear a cached probe failure even though chmod preserves mtime/size;
    # an atomic package-manager replacement must do the same even if it preserves timestamps.
    return (binary, info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


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


def _protocol_capability(binary: str) -> tuple[bool, str]:
    """Probe the exact installed app-server schema once per executable revision.

    The probe is intentionally local and side-effect-free: it generates the experimental schema
    under a temporary CODEX_HOME, never starts a thread, and never contacts a model.  False results
    are cached too, so an incompatible binary cannot turn the provider claim loop into a process
    storm.
    """
    key = _version_cache_key(binary)
    cached = _PROTOCOL_CACHE.get(key) if key is not None else None
    if cached is not None:
        return cached
    try:
        with tempfile.TemporaryDirectory(prefix="grid-codex-protocol-") as temporary:
            root = Path(temporary)
            output = root / "schema"
            output.mkdir()
            codex_home = root / "home"
            codex_home.mkdir()
            env = os.environ.copy()
            env["CODEX_HOME"] = str(codex_home)
            proc = subprocess.run(
                [binary, "app-server", "generate-json-schema", "--experimental",
                 "--out", str(output)],
                capture_output=True, text=True, errors="replace", env=env,
                timeout=_PROTOCOL_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL, check=False)
            if proc.returncode != 0:
                result = (False, "Codex could not generate its experimental app-server schema "
                          f"(exit {proc.returncode})")
            else:
                schema_path = output / "codex_app_server_protocol.schemas.json"
                try:
                    size = schema_path.stat().st_size
                    if size > _PROTOCOL_SCHEMA_MAX_BYTES:
                        raise CodexProtocolError("Codex app-server schema is unexpectedly large")
                    schema = json.loads(schema_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, RecursionError) as exc:
                    raise CodexProtocolError(
                        f"Codex returned no usable app-server schema: {exc}") from exc
                remaining = set(_REQUIRED_PROTOCOL_METHODS)
                pending = [schema]
                while pending and remaining:
                    value = pending.pop()
                    if isinstance(value, str):
                        remaining.discard(value)
                    elif isinstance(value, dict):
                        pending.extend(value.values())
                    elif isinstance(value, list):
                        pending.extend(value)
                if remaining:
                    result = (False, "Codex app-server schema lacks Grid Goal methods: "
                              + ", ".join(sorted(remaining)))
                else:
                    result = (True, "")
    except (OSError, subprocess.SubprocessError, CodexProtocolError) as exc:
        result = (False, str(exc))
    if key is not None:
        _PROTOCOL_CACHE[key] = result
    return result


def _remember_protocol_failure(binary: str, reason: str) -> None:
    """Quarantine deterministic runtime drift until this executable changes on disk."""
    key = _version_cache_key(binary)
    if key is not None:
        _PROTOCOL_CACHE[key] = (False, reason)


def _require_distributed_goal_capability(binary: str) -> str:
    _require_distributed_goal_version(binary)
    supported, reason = _protocol_capability(binary)
    if not supported:
        raise CodexProtocolError(
            f"Codex cannot provide distributed native Goals: {reason or 'incompatible protocol'}")
    return binary


def supports_distributed_goals(binary: str) -> bool:
    """Whether this exact executable is safe to advertise to the Goal scheduler."""
    try:
        _require_distributed_goal_capability(binary)
    except CodexGoalError:
        return False
    return True


def resolve_binary() -> str:
    binary = shutil.which("codex")
    if not binary:
        raise CodexGoalError("Codex is not installed on this provider")
    return _require_distributed_goal_capability(binary)


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


def _relative_rollout(codex_home: Path, path: Any, *, require_exists: bool = True) -> str:
    """Turn Codex's machine-local rollout path into a checkpoint-safe relative path.

    Codex 0.150.1 returns the future JSONL path from ``thread/start`` before activation creates the
    file.  Grid must persist that portable pointer before the turn runs (so a mid-turn kill can be
    handed off), but must still require the promised file before accepting a completed slice.
    Containment is checked both times, including after any parent symlinks have appeared.
    """
    if not isinstance(path, str) or not path:
        raise CodexGoalError("Codex returned no rollout path for its distributed Goal thread")
    root = codex_home.resolve()
    supplied = Path(path)
    if not supplied.is_absolute():
        raise CodexGoalError("Codex returned a non-absolute distributed Goal rollout path")
    candidate = supplied.resolve()
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        raise CodexGoalError("Codex stored its Goal rollout outside the portable Codex home") from None
    if require_exists and not candidate.is_file():
        raise CodexGoalError("Codex's distributed Goal rollout does not exist")
    return relative.as_posix()


def _resume_rollout(codex_home: Path, state: dict[str, Any], thread_id: str) -> Path:
    """Resolve a copied rollout without trusting A's absolute SQLite path on worker B."""
    relative = state.get("rollout_relpath")
    if relative is not None:
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise CodexGoalError("the distributed Codex checkpoint has an invalid rollout path")
        root = codex_home.resolve()
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            raise CodexGoalError(
                "the distributed Codex checkpoint rollout escapes its Codex home") from None
        if not candidate.is_file():
            raise MissingRollout("the distributed Codex checkpoint rollout is missing")
        return candidate

    # Compatibility for checkpoints written before rollout_relpath existed. The copied transcript
    # still contains the rollout; recover it by exact thread-id suffix and write the relative path
    # back after this slice. Never fall back to resume-by-id: Codex's SQLite row contains worker A's
    # absolute path and can silently work only while A's disk happens to remain mounted.
    sessions = codex_home / "sessions"
    matches = [path for path in sessions.rglob("*.jsonl")
               if path.is_file() and path.name.endswith(f"-{thread_id}.jsonl")]
    if len(matches) != 1:
        raise CodexGoalError(
            "the distributed Codex checkpoint has no unique portable rollout for its thread")
    return matches[0].resolve()


class ToolExecutor:
    """Execute the Goal's explicit observe/act HTTP capabilities and publish an audit event."""

    def __init__(self, tools: list[dict[str, Any]], *, publish: Callable[..., Any],
                 inference: GridInference, scope: str, turn_scope: str | None = None):
        self.tools: dict[str, dict[str, Any]] = {}
        invalid_names: set[str] = set()
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            name = str(tool.get("name") or "")
            if not name or name in invalid_names:
                continue
            if name in self.tools:
                self.tools.pop(name)
                invalid_names.add(name)
                continue
            self.tools[name] = tool
        self.publish = publish
        self.inference = inference
        self.scope = scope
        self.turn_scope = turn_scope or scope
        self.allowed_origins = _allowed_goal_tool_origins()

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
        if not _tool_json_depth_is_safe(arguments):
            return self._result(False, {
                "error": f"tool arguments exceed {_MAX_TOOL_JSON_DEPTH} levels",
            })
        try:
            encoded_arguments = json.dumps(
                arguments, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            return self._result(False, {"error": "tool arguments are not valid JSON"})
        if len(encoded_arguments) > _MAX_TOOL_ARGUMENT_BYTES:
            return self._result(False, {
                "error": f"tool arguments exceed {_MAX_TOOL_ARGUMENT_BYTES} bytes",
            })
        mode = str(tool.get("mode") or "")
        http = tool.get("http") if isinstance(tool.get("http"), dict) else {}
        url = str(http.get("url") or "")
        method = str(http.get("method") or "POST").upper()
        if (mode not in ("observe", "act", "verify") or not url
                or method not in ("GET", "POST", "PUT", "PATCH", "DELETE")):
            return self._result(False, {"error": "tool HTTP configuration is invalid"})
        if ((mode in ("observe", "verify") and method != "GET")
                or (mode == "act" and method == "GET")):
            return self._result(False, {"error": "tool mode and HTTP method are inconsistent"})

        internal = tool.get("grid_internal") is True
        parsed = urlparse(url)
        if internal:
            if (name not in _INTERNAL_GOAL_TOOLS or http.get("auth") != "grid"
                    or not url.startswith("/") or url.startswith("//")
                    or parsed.query or parsed.fragment):
                return self._result(False, {"error": "internal Grid tool configuration is invalid"})
            url = urljoin(self.inference.base_url.rstrip("/") + "/", url.lstrip("/"))
            if _http_origin(url) != _http_origin(self.inference.base_url):
                return self._result(False, {"error": "internal Grid tool escaped the selected relay"})
        else:
            if http.get("auth") is not None or http.get("headers") is not None:
                return self._result(False, {"error": "business tools cannot use Grid credentials"})
            origin = _http_origin(url)
            if (origin is None or parsed.params or parsed.query or parsed.fragment
                    or (parsed.path and not parsed.path.startswith("/"))):
                return self._result(False, {"error": "business tool URL is invalid"})
            if origin not in self.allowed_origins:
                return self._result(False, {
                    "error": "business tool origin is not approved by this Grid node",
                })

        headers = {"Accept": "application/json"}
        # A Grid credential may only be sent to a relay-authored internal action on the exact relay
        # origin. Business API credentials are never smuggled through the Goal manifest or Git.
        if internal:
            headers["Authorization"] = f"Bearer {self.inference.current_token()}"
            if self.inference.claim_id:
                # Internal actions mutate Grid state and need the same exact attempt generation as
                # Git, inference, events and settlement. It stays in the supervisor-side HTTP
                # client and is never exposed to the native agent or accepted from a user manifest.
                headers["X-Grid-Task-Claim"] = self.inference.claim_id
            configured_headers = http.get("headers")
            if isinstance(configured_headers, dict):
                # The relay injects this lease fence for its built-in subgoal action. No arbitrary
                # header forwarding: user manifests must not be able to smuggle credentials or
                # override content negotiation through the provider.
                turn_id = configured_headers.get("X-Grid-Goal-Turn")
                if isinstance(turn_id, str) and turn_id:
                    headers["X-Grid-Goal-Turn"] = turn_id
        if mode == "act":
            # External mutations are content-idempotent across the whole Goal, including a later
            # turn created after an eval failure. Relay-internal actions remain turn-scoped because
            # their lease fence and child-spawn ownership are explicitly tied to one turn.
            action_scope = self.turn_scope if internal else self.scope
            canonical = json.dumps([action_scope, name, arguments], sort_keys=True,
                                   separators=(",", ":"), ensure_ascii=False,
                                   allow_nan=False).encode()
            headers["Idempotency-Key"] = "grid-goal-" + hashlib.sha256(canonical).hexdigest()
        raw_timeout = http.get("timeout_seconds", 30)
        if (isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float))
                or not 0.1 <= raw_timeout <= 300):
            return self._result(False, {"error": "tool HTTP timeout is invalid"})
        record_full = tool.get("record") == "full"
        recorded = self.publish(
            f"goal.{mode}.request", tool=name, call_id=call_id,
            _flush=True,
            **({"idempotency_key": headers["Idempotency-Key"]} if mode == "act" else {}),
            **({"arguments": _recorded_tool_value(arguments)} if record_full else {}))
        if recorded is False:
            # An unrecorded action is not safe to perform. A later call/attempt can retry before
            # any business side effect exists.
            return self._result(False, {"error": "could not durably record tool request"})
        try:
            with httpx.Client(
                    timeout=float(raw_timeout), trust_env=False, follow_redirects=False) as client:
                for attempt in range(2):
                    stale_token = self.inference.current_token() if internal else ""
                    if internal:
                        headers["Authorization"] = f"Bearer {stale_token}"
                    with client.stream(
                            method, url, params=arguments if method == "GET" else None,
                            json=None if method == "GET" else arguments,
                            headers=headers) as response:
                        status_code = response.status_code
                        if (internal and status_code == 401 and attempt == 0
                                and self.inference.refresh_token(stale_token)):
                            continue
                        chunks: list[bytes] = []
                        size = 0
                        oversized = False
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > _MAX_TOOL_HTTP_BYTES:
                                oversized = True
                                break
                            chunks.append(chunk)
                        break
            if oversized:
                success, result = False, {
                    "status_code": status_code,
                    "error": f"tool response exceeds {_MAX_TOOL_HTTP_BYTES} bytes",
                }
            else:
                raw_body = b"".join(chunks)
                try:
                    body: Any = json.loads(raw_body) if raw_body else None
                    if not _tool_json_depth_is_safe(body):
                        raise ValueError("tool response JSON is nested too deeply")
                except (UnicodeDecodeError, ValueError, RecursionError):
                    body = raw_body.decode("utf-8", "replace")
                result = {"status_code": status_code, "body": body}
                success = 200 <= status_code < 300
        except (httpx.HTTPError, ValueError):
            # HTTP client exception strings commonly contain the fully expanded GET URL. Do not
            # leak argument values into an unstructured error that evades field-name redaction.
            success, result = False, {"error": "tool HTTP request failed"}
        recorded = self.publish(
            f"goal.{mode}.result", tool=name, call_id=call_id, success=success,
            _flush=True,
            **({"idempotency_key": headers["Idempotency-Key"]} if mode == "act" else {}),
            **({"result": _recorded_tool_value(result)} if record_full else {}))
        if recorded is False:
            # The API may already have committed an action. Fail the turn so the same leased-turn
            # scope and deterministic idempotency key are retried; never let Codex build more work
            # on an outcome Grid cannot prove happened.
            raise CodexGoalError("could not durably record tool result")
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
            message = str(error.get("message", error) if isinstance(error, dict) else error)
            if isinstance(error, dict) and error.get("code") == -32601:
                raise CodexProtocolError(f"Codex app-server method is unavailable: {message}")
            raise CodexGoalError(message)
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
    conversation_id = job.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        raise CodexGoalError("the relay supplied no Goal conversation id")

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
                         scope=conversation_id,
                         turn_scope=f"{conversation_id}:{turns_before + 1}")

    claim = ({"claim_id": inference.claim_id} if inference.claim_id else {})
    proxy = InferenceProxy(
        inference.base_url.rstrip("/") + "/relay/v1", inference.current_token,
        refresh_token=inference.refresh_token,
        turn_id=str(job.get("task_id") or "") or None,
        conversation_id=str(job.get("conversation_id") or "") or None,
        **claim)
    proxy.start()
    process: ProcessLike | None = None
    started = time.monotonic()
    try:
        env = task_agent.goal_child_env(workspace=workspace)
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
            try:
                resume["path"] = str(_resume_rollout(codex_home, state, thread_id))
            except MissingRollout:
                # A process can die after thread/start promises its future path but before Codex
                # activates the first turn and creates it. No native turn completed and therefore
                # no model history exists to lose; restart the native thread over the Git worktree
                # checkpoint instead of burning every distributed retry on the same absent file.
                if turns_before:
                    raise
                thread_id = ""
            if thread_id:
                thread_result = rpc.wait(
                    rpc.send("thread/resume", {"threadId": thread_id, **resume})) or {}
        if not thread_id:
            thread_result = rpc.wait(rpc.send("thread/start", thread_params)) or {}
            thread_id = str((thread_result.get("thread") or {}).get("id") or "")
            if not thread_id:
                raise CodexProtocolError("Codex thread/start returned no thread id")
        rollout_path = (thread_result.get("thread") or {}).get("path")
        rollout_relpath = _relative_rollout(
            codex_home, rollout_path, require_exists=False)
        # Persist the portable thread pointer BEFORE activating/running this slice. If the native
        # app-server dies halfway through the turn, `_push_result` can now publish a checkpoint that
        # another Codex machine resumes from the copied rollout instead of starting a new thread.
        # Counters deliberately remain at the last completed checkpoint: replay is the same logical
        # turn, which also keeps external-action idempotency keys stable.
        _write_state(workspace, {
            "version": 2, "thread_id": thread_id,
            "rollout_relpath": rollout_relpath, "status": "active",
            "turns_completed": turns_before, "tokens_used": tokens_before,
            "time_used_seconds": time_before,
        })
        # Grid may reduce the parent's remaining native budget after terminal children replace
        # their allocations with actual cumulative usage. Refresh the cap on every resumed slice;
        # setting it only at thread creation would let the old native cap overspend the hierarchy.
        set_goal: dict[str, Any] = {
            "threadId": thread_id, "status": "active",
            "tokenBudget": goal.get("token_budget"),
        }
        if turns_before == 0:
            set_goal.update({
                "objective": f"{goal['objective'].strip()}\n\nDone when: {goal['done_when'].strip()}",
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
        # A successful native turn without its rollout cannot be resumed on another machine. The
        # early check deliberately accepted Codex's promised future path; this is where that promise
        # becomes mandatory, before Grid publishes a completed checkpoint.
        rollout_relpath = _relative_rollout(codex_home, rollout_path, require_exists=True)
        result = GoalSlice(status, thread_id, turns_before + 1, measured_tokens, measured_time,
                           output=str(turn.get("output") or "") or None)
        _write_state(workspace, {
            "version": 2, "thread_id": result.thread_id,
            "rollout_relpath": rollout_relpath, "status": result.status,
            "turns_completed": result.turns_completed, "tokens_used": result.tokens_used,
            "time_used_seconds": result.time_used_seconds,
        })
        publish("goal.slice.completed", status=result.status,
                turns_completed=result.turns_completed, tokens_used=result.tokens_used)
        return result
    except CodexProtocolError as exc:
        # The current turn is retried from its Git checkpoint, but this process immediately stops
        # advertising the incompatible binary.  That prevents one bad node from consuming every
        # bounded attempt before a compatible Grid machine can claim the Goal.
        _remember_protocol_failure(executable, str(exc))
        raise
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
    mapped = {"active": "active", "paused": "paused", "blocked": "blocked",
              "usageLimited": "usage_limited", "budgetLimited": "budget_limited",
              "complete": "complete", "failed": "failed"}.get(native)
    if mapped is None:
        # Version/protocol drift is a harness fault, not proof that the Goal is impossible. The
        # caller turns this into a bounded distributed retry so another compatible node can claim
        # the turn instead of storing an invented terminal failure.
        raise CodexProtocolError(f"Codex returned unsupported native Goal status {native!r}")
    return mapped


def _counter(source: dict[str, Any], field: str, label: str) -> int:
    value = source.get(field, 0)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CodexGoalError(f"the {label} has an invalid {field}")
    return value
