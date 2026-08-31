"""The harness: a real relay from grid-src, real provider processes from this repo, a fake agent.

Nothing in this directory is collected by an ordinary `pytest tests/` run — the modules are named
`e2e_*.py` rather than `test_*.py`, the same convention `tests/e2e_doggi.py` and `tests/e2e_train.py`
already use. Pass a path explicitly to run them. See `README.md`.
"""
from __future__ import annotations

import json
import os
import shutil
import string
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _harness as H

sys.path.insert(0, str(H.GRID_REPO))

_HERE = Path(__file__).resolve().parent
_BASE_GOAL_MODELS = ("fake-grid-model", "fake-grid-child-model")


def _advertise_goal_models(relay: str, token: str, node_id: str,
                           models: tuple[str, ...] | list[str]) -> None:
    """Give the real relay endpoint-aware fake routes used by native Goal protocol tests."""
    from remote import relay as relay_client

    capabilities = {
        "schema_version": 1,
        "models": {
            model: {
                "endpoints": ["chat/completions", "messages", "responses"],
                "input_modalities": ["text"], "output_modalities": ["text"],
                "features": {"tools": True},
            }
            for model in models
        },
    }
    relay_client.register_node(
        relay, token, node_id, models=list(models), capabilities=capabilities, role="provider")


class _FakeInferenceProvider:
    """One real provider poll/settle loop for a single deterministic E2E model."""

    def __init__(self, relay: str, token: str, node_id: str, model: str):
        self.relay = relay
        self.token = token
        self.node_id = node_id
        self.model = model
        self.records: list[dict] = []
        self.errors: list[str] = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        import httpx
        from remote import relay as relay_client

        while not self.stop.is_set():
            try:
                # The relay holds an empty provider poll for 30 seconds. Keep the client's five-
                # second safety margin: timing out first can race a newly assigned job, leaving the
                # row `streaming` after its HTTP response was discarded with the socket.
                job = relay_client.poll(self.relay, self.token)
                if job is None:
                    continue
                body = job.get("body") or {}
                if body.get("model") != self.model:
                    raise RuntimeError(
                        f"provider {self.node_id} received model {body.get('model')!r}, "
                        f"expected {self.model!r}")
                dialect = job.get("wire_format")
                if dialect == "responses":
                    answer = {
                        "id": f"resp-{job['transaction_id']}", "object": "response",
                        "created_at": int(time.time()), "status": "completed",
                        "model": self.model,
                        "output": [{
                            "id": f"msg-{job['transaction_id']}", "type": "message",
                            "status": "completed", "role": "assistant",
                            "content": [{"type": "output_text", "text": "grid inference ok",
                                         "annotations": [], "logprobs": []}],
                        }],
                        "usage": {"input_tokens": 7, "output_tokens": 3,
                                  "total_tokens": 10},
                    }
                elif dialect == "anthropic":
                    answer = {
                        "id": f"msg-{job['transaction_id']}", "type": "message",
                        "role": "assistant", "model": self.model,
                        "content": [{"type": "text", "text": "grid inference ok"}],
                        "stop_reason": "end_turn", "stop_sequence": None,
                        "usage": {"input_tokens": 7, "output_tokens": 3},
                    }
                else:
                    answer = {
                        "id": f"chatcmpl-{job['transaction_id']}",
                        "object": "chat.completion", "model": self.model,
                        "choices": [{"index": 0, "message": {
                            "role": "assistant", "content": "grid inference ok"},
                            "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 7, "completion_tokens": 3,
                                  "total_tokens": 10},
                    }
                headers = {
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                }
                attempt_token = job.get("provider_attempt_token")
                if attempt_token:
                    headers["X-Grid-Provider-Attempt"] = attempt_token
                response = httpx.post(
                    f"{self.relay}/relay/v1/response/{job['transaction_id']}",
                    content=json.dumps(answer, separators=(",", ":")).encode(),
                    headers=headers, timeout=10)
                response.raise_for_status()
                self.records.append({**job, "response": answer})
            except relay_client.RelayError as exc:
                if not self.stop.is_set():
                    self.errors.append(f"{type(exc).__name__}: {exc}")
                    self.stop.set()
            except Exception as exc:  # noqa: BLE001 - preserve background failure for the test
                if not self.stop.is_set():
                    self.errors.append(f"{type(exc).__name__}: {exc}")
                    self.stop.set()

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=36)


@pytest.fixture(scope="session")
def relay_home(tmp_path_factory):
    """Where the relay keeps its database and its project repositories.

    Split out of `relay` so a test can reach the relay's own `users` table (ADR 0033 issue 21).
    That is not a shortcut around the API: there IS no API for it. A member row is written by
    `grid_auth._upsert_identity` when a grid token is verified, and this harness runs the relay with
    `GRID_MODE=false` — the plain-JWT branch, which never touches `users`. Without seeding, an
    author test here would exercise the anonymous fallback and agree with a relay that resolved
    nothing at all.
    """
    return tmp_path_factory.mktemp("relay")


@pytest.fixture(scope="session")
def relay_db(relay_home):
    """The relay's SQLite file, addressable with the stdlib. This repo has no SQLAlchemy."""
    return relay_home / "e2e.db"


@pytest.fixture(scope="session")
def relay(relay_home):
    """grid-src's `server:app` under a real `uvicorn`, in a subprocess, run by ITS OWN interpreter.

    A subprocess and not a thread, and grid-src's interpreter and not this one: the two repositories
    are separate installs in the field, and a single process importing both would be a topology that
    exists nowhere. It also means the relay's own reaper is really ticking, which is what a reclaim
    needs — a test that poked the row would prove the reclaim function works, not that anything calls
    it.
    """
    proc, base = H.start_relay(relay_home)
    try:
        yield base
    finally:
        H.stop_relay(proc)


@pytest.fixture(scope="session")
def relay_short_budgets(tmp_path_factory):
    """A SECOND relay, whose queue and run budgets are seconds rather than hours.

    Its own process because grid-src reads both budgets from the environment at import time, and its
    own database because a task reaped after three seconds must not appear in the queue every other
    module's assertions read. The ordinary `relay` keeps the production defaults deliberately: a
    three-second run budget there would reap the long-running agents those tests spawn on purpose,
    and the failure would look like a provider bug.
    """
    proc, base = H.start_relay(tmp_path_factory.mktemp("relay-budgets"), extra_env={
        "TASK_QUEUE_DEADLINE_SECONDS": str(H.QUEUE_BUDGET_SECONDS),
        "TASK_DEADLINE_SECONDS": str(H.RUN_BUDGET_SECONDS),
    })
    try:
        yield base
    finally:
        H.stop_relay(proc)


@pytest.fixture(scope="session")
def owner_token():
    return H.token("alice", "client-node")


@pytest.fixture(scope="session")
def provider_nodes(relay, owner_token):
    """Four REGISTERED provider nodes, each with a token carrying its own id.

    Registration is not decoration: `_require_provider` gates the claim on the node registry's role,
    so an unregistered member cannot claim at all. Two real identities rather than one used twice,
    because the push fence is keyed on `provider_id` — one identity could not tell "the lease moved"
    from "the lease is still mine".
    """
    import httpx

    nodes = {}
    with httpx.Client(base_url=relay, timeout=30.0) as client:
        for label in ("A", "B", "C", "D"):
            created = client.post(
                "/nodes", json={"role": "both"},
                headers={"Authorization": f"Bearer {owner_token}"})
            assert created.status_code == 200, created.text
            nodes[label] = (created.json()["node_id"], H.token(f"provider-{label}", created.json()["node_id"]))
    return nodes


@pytest.fixture
def advertise_goal_models(relay, provider_nodes):
    """Advertise inference on a node without starting its task/agent process."""
    def advertise(label: str, *models: str, quota_serving: bool | None = None) -> str:
        from remote import relay as relay_client

        node_id, node_token = provider_nodes[label]
        combined = tuple(dict.fromkeys((*_BASE_GOAL_MODELS, *models)))
        _advertise_goal_models(relay, node_token, node_id, combined)
        if quota_serving is not None:
            # Registration owns static routes; the heartbeat owns live allowance. Keeping these as
            # two real requests is the field topology and catches a scheduler that mistakes an
            # advertised-but-withdrawn subscription seat for usable inference.
            assert relay_client.heartbeat(
                relay, node_token,
                load={"quota": {
                    "serving": quota_serving,
                    "headroom_pct": 100 if quota_serving else 0,
                }},
            ) == "ok"
        return node_id

    return advertise


@pytest.fixture
def spawn_inference_provider(relay, owner_token, provider_nodes):
    """Serve deterministic model bytes through the relay's real inference queue."""
    providers: list[_FakeInferenceProvider] = []

    # Session-scoped node identities retain the previous test's registrations. This scenario needs
    # exclusive routes so its provider attribution proves something stronger than "a request ran".
    for node_id, node_token in provider_nodes.values():
        _advertise_goal_models(relay, node_token, node_id, [])

    def spawn(label: str, model: str) -> _FakeInferenceProvider:
        import httpx

        # In production an inference engine and an agent worker may coexist, but they remain
        # separately registered services. Use a dedicated node identity here so attribution can
        # never collapse "the agent ran" into "that same process served its model request".
        created = httpx.post(
            f"{relay}/nodes", json={"role": "provider"},
            headers={"Authorization": f"Bearer {owner_token}"}, timeout=10)
        created.raise_for_status()
        node_id = created.json()["node_id"]
        node_token = H.token(f"inference-{label}", node_id)
        _advertise_goal_models(relay, node_token, node_id, [model])
        provider = _FakeInferenceProvider(relay, node_token, node_id, model)
        providers.append(provider)
        return provider

    yield spawn
    for provider in providers:
        provider.close()


@pytest.fixture(scope="session")
def fake_agent_bin(tmp_path_factory):
    """`fake_claude.py` installed as a `claude` on PATH — which is how `claude_install.resolve()`
    finds it, PATH first."""
    bindir = tmp_path_factory.mktemp("bin")
    target = bindir / "claude"
    target.write_text(
        f"#!{sys.executable}\n" + (_HERE / "fake_claude.py").read_text(encoding="utf-8"),
        encoding="utf-8")
    target.chmod(0o755)
    return bindir


@pytest.fixture(scope="session")
def fake_codex_bin(tmp_path_factory):
    """The deterministic app-server used only by the distributed Goal E2E."""
    bindir = tmp_path_factory.mktemp("codex-bin")
    target = bindir / "codex"
    target.write_text(
        f"#!{sys.executable}\n" + (_HERE / "fake_codex.py").read_text(encoding="utf-8"),
        encoding="utf-8")
    target.chmod(0o755)
    return bindir


@pytest.fixture(scope="session")
def workspace_root():
    """The providers' `GRID_TASK_ROOT`, and a SHORT one.

    ⚠️ **Not `tmp_path_factory`, for the reason `e2e_live_agent.live_workspace_root` gives at
    length.** Its roots are ~96 characters before grid adds the 126 that ADR 0034 D-c's workspace
    path costs, and the whole thing is flattened into ONE transcript directory name. Claude Code
    stops using such a name verbatim past ~200 — measured — so `task_agent.link_transcript` refuses,
    and every task in this directory would fail on a limit no real deployment meets (`/var/grid`
    flattens to 135). `fake_claude.py` derives its own transcript path the same way the real binary
    does, so this is not an artefact of the fake: it is the production shape.

    `/private/tmp` rather than `/tmp` so the path is already its own realpath — on macOS `/tmp` is a
    symlink, and a root that resolves elsewhere is issue 06's trap.
    """
    root = Path(tempfile.mkdtemp(prefix="ge2e-", dir="/private/tmp"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="session")
def goal_workspace_root():
    """Three disjoint provider disks under a genuinely short macOS-safe task root.

    The task runner warns above 31 characters because macOS sandbox profiles flatten the path into
    process arguments.  A conventional ``mkdtemp`` root already consumed that entire budget before
    this suite appended its human-readable per-provider disk label.  Reserve one otherwise-unused
    single-character directory atomically instead: the longest current root is then 30 characters,
    while the labels that make cross-machine assertions readable remain unchanged.
    """
    root = None
    for suffix in string.ascii_letters + string.digits:
        candidate = Path("/private/tmp") / suffix
        try:
            candidate.mkdir(mode=0o700)
        except FileExistsError:
            continue
        root = candidate
        break
    if root is None:
        raise RuntimeError("no short /private/tmp task root is available for Goal E2E")
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def business_api():
    """A process-external API with an idempotent write, as a local business service would expose."""
    state = {
        "reads": [], "verifications": [], "write_requests": [], "writes_by_key": {},
        "side_effects": [],
        # The result-window E2E pauses the FIRST committed mutation before its HTTP response. That
        # gives the driver a deterministic place to kill the provider after the side effect but
        # before ToolExecutor can append goal.act.result. Ordinary scenarios leave this disabled.
        "pause_after_commit": False,
        "commit_reached": threading.Event(),
        "release_response": threading.Event(),
    }
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def send_json(self, status: int, value: dict) -> None:
            encoded = json.dumps(value, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                # Expected when the result-window test kills the client process while this handler
                # deliberately holds the response after committing the mutation.
                pass

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler protocol name
            parsed = urlparse(self.path)
            if parsed.path not in ("/tickets/read", "/tickets/check"):
                self.send_json(404, {"error": "not found"})
                return
            ticket_id = (parse_qs(parsed.query).get("ticket_id") or [""])[0]
            with lock:
                if parsed.path == "/tickets/check":
                    state["verifications"].append(ticket_id)
                    side_effects = len(state["side_effects"])
                else:
                    state["reads"].append(ticket_id)
                    side_effects = None
            if parsed.path == "/tickets/check":
                self.send_json(200, {
                    "ticket_id": ticket_id,
                    "status": "resolved" if side_effects == 1 else "open",
                    "side_effects": side_effects,
                })
            else:
                self.send_json(200, {
                    "ticket_id": ticket_id, "text": "Customer cannot reset their password",
                })

        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler protocol name
            if urlparse(self.path).path != "/tickets/reply":
                self.send_json(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
                body = json.loads(self.rfile.read(length))
            except (TypeError, ValueError):
                self.send_json(400, {"error": "invalid JSON"})
                return
            key = self.headers.get("Idempotency-Key")
            if not key:
                self.send_json(400, {"error": "missing idempotency key"})
                return
            with lock:
                state["write_requests"].append({"key": key, "body": body})
                previous = state["writes_by_key"].get(key)
                if previous is not None and previous != body:
                    self.send_json(409, {"error": "idempotency conflict"})
                    return
                replayed = previous is not None
                if not replayed:
                    state["writes_by_key"][key] = body
                    state["side_effects"].append(body)
                pause_after_commit = state["pause_after_commit"] and not replayed
            if pause_after_commit:
                state["commit_reached"].set()
                state["release_response"].wait(timeout=30)
            self.send_json(200, {
                "reply_id": "R-1", "applied": not replayed, "replayed": replayed,
            })

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["origin"] = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        state["release_response"].set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def spawn_goal_provider(relay, provider_nodes, fake_codex_bin, fake_agent_bin, goal_workspace_root,
                        tmp_path_factory):
    """A real task-loop process advertising Codex and using a node-private task root."""
    started: list[H.Provider] = []
    handles: list = []

    def _spawn(label: str, *, agent_kinds: str = "codex", scenario: str = "codex",
               disk_label: str | None = None, codex_capabilities: str = "",
               claude_capabilities: str = "", tool_origins: str = "",
               one_task: bool = False, task_workers: int = 1,
               advertise_models: bool = True):
        node_id, node_token = provider_nodes[label]
        task_root = goal_workspace_root / (disk_label or label)
        assert len(str(task_root)) <= 31, (
            f"Goal E2E task root exceeds the measured macOS safety budget: {task_root}")
        # This process emulates only the task/agent plane.  The separate registration emulates a
        # compatible Grid inference route and refreshes its heartbeat before each scenario; the
        # model-readiness scheduler must not infer model capacity merely from a task poller.
        if advertise_models:
            _advertise_goal_models(relay, node_token, node_id, _BASE_GOAL_MODELS)
        env = {
            **os.environ,
            "PATH": (f"{fake_codex_bin}{os.pathsep}{fake_agent_bin}{os.pathsep}"
                     f"{os.environ.get('PATH', '')}"),
            "GRID_REPO": str(H.GRID_REPO),
            "GRID_SIGNALING_URL": relay,
            "GRID_NODE_ID": node_id,
            "GRID_TOKEN": node_token,
            "GRID_RENEW_SECONDS": str(H.RENEW_SECONDS),
            "GRID_TASK_ROOT": str(task_root),
            "GRID_TASK_TIMEOUT_SECONDS": "120",
            "GRID_E2E_GOAL_NODE": label,
            "GRID_E2E_GOAL_SCENARIO": scenario,
            "GRID_E2E_ONE_TASK": "1" if one_task else "0",
            "GRID_E2E_TASK_WORKERS": str(task_workers),
            "GRID_TASK_AGENT_KINDS": agent_kinds,
            "GRID_CODEX_GOAL_CAPABILITIES": codex_capabilities,
            "GRID_CLAUDE_TASK_CAPABILITIES": claude_capabilities,
            "GRID_GOAL_TOOL_ORIGINS": tool_origins,
            # The task runner intentionally allowlists child env. This test-only selector is
            # explicit operator passthrough, not an accidental inheritance of provider secrets.
            "GRID_TASK_ENV_PASSTHROUGH": "GRID_E2E_GOAL_NODE,GRID_E2E_GOAL_SCENARIO",
        }
        log_path = tmp_path_factory.mktemp(f"goal-provider-{label}") / "provider.log"
        claim_marker = log_path.parent / "claim-poll-entered"
        env["GRID_E2E_CLAIM_MARKER"] = str(claim_marker)
        env["GRID_E2E_STALE_POLLS_REQUIRED"] = str(max(1, task_workers - 1))
        env["GRID_TASK_ENV_PASSTHROUGH"] += (
            ",GRID_E2E_CLAIM_MARKER,GRID_E2E_STALE_POLLS_REQUIRED")
        handle = open(log_path, "w", buffering=1)
        handles.append(handle)
        proc = subprocess.Popen(
            [sys.executable, str(_HERE / "provider_process.py")], env=env,
            stdout=handle, stderr=subprocess.STDOUT, text=True)
        provider = H.Provider(proc, node_id, log_path=log_path)
        started.append(provider)
        return provider

    yield _spawn
    for provider in started:
        provider.stop()
    for handle in handles:
        handle.close()


@pytest.fixture
def spawn_provider(relay, provider_nodes, fake_agent_bin, workspace_root, tmp_path_factory):
    """A provider process running this repo's real `task_loop` against the fake agent."""
    started: list[H.Provider] = []
    logs: list = []

    def _spawn(label="A", workers=1, extra_env=None):
        """`workers` is how many turns this provider runs AT ONCE (ADR 0034 D-b, issue 40).

        **Default 1, and every existing test depends on it.** `test_09` proves a cancel really
        stopped an agent by watching a second task become claimable, which is evidence only while
        this provider has one worker — so concurrency is opt-in per spawn rather than a new default.

        `extra_env` is for the provider-side knobs a test needs to drive from outside the process —
        `GRID_TASK_MAX_WORKSPACES` (ADR 0034 D-c, issue 50) is the first. Applied LAST so a test's
        value wins over the defaults below, and per spawn rather than through `monkeypatch.setenv`,
        which would reach every provider in a two-provider test.
        """
        node_id, node_token = provider_nodes[label]
        env = {
            **os.environ,
            "GRID_E2E_TASK_WORKERS": str(workers),
            "PATH": f"{fake_agent_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "GRID_REPO": str(H.GRID_REPO),
            "GRID_SIGNALING_URL": relay,
            "GRID_NODE_ID": node_id,
            "GRID_TOKEN": node_token,
            "GRID_RENEW_SECONDS": str(H.RENEW_SECONDS),
            "GRID_TASK_ROOT": str(workspace_root / label),
            # NEVER the developer's own `~/.claude`: `link_transcript` plants a symlink under it.
            # `e2e_live_agent.py` is the one place that cannot do this, and it cleans up after itself.
            "GRID_TASK_CLAUDE_CONFIG_DIR": str(tmp_path_factory.mktemp(f"claude-config-{label}")),
            "GRID_TASK_TIMEOUT_SECONDS": "120",
            **(extra_env or {}),
        }
        # ⚠️ **A FILE, not an undrained `subprocess.PIPE`.** This used to be a pipe nobody read, and
        # that is not merely untidy: a pipe's buffer is 64 KB, and a provider that fills it blocks
        # in `write` — mid-task, holding a lease, with no error anywhere. It changed what this
        # harness measured rather than merely hiding output. Found by mutation while adding the
        # cancel test (ADR 0033 issue 19b): with the pipe, deleting a letter from the lockstep
        # refusal code still PASSED; with the log on disk the same mutant fails, because the
        # provider then really does stay busy with the agent it was supposed to stop.
        #
        # It also gives a test something to read while the provider is still running, which
        # `H.Provider.output()` exposes.
        log_path = tmp_path_factory.mktemp(f"provider-log-{label}") / "provider.log"
        handle = open(log_path, "w", buffering=1)
        logs.append(handle)
        proc = subprocess.Popen(
            [sys.executable, str(_HERE / "provider_process.py")],
            env=env, stdout=handle, stderr=subprocess.STDOUT, text=True)
        provider = H.Provider(proc, node_id, log_path=log_path)
        started.append(provider)
        return provider

    yield _spawn
    for provider in started:
        provider.stop()
    for handle in logs:
        handle.close()


@pytest.fixture(scope="session")
def relay_private_domain(tmp_path_factory):
    """A THIRD relay, on ADR 0034 D-k's network type but **NOT in Grid mode** — so it serves the
    rule not at all, and that is the point of it.

    Its own process for `relay_short_budgets`' reason: grid-src reads `GRID_NETWORK_TYPE` from the
    environment at import time, so a relay with a different topology is a different process. Its own
    database too, so its projects do not appear in the listings every other module asserts on.

    ⚠️ **This fixture cannot exercise D-k's positive path, and nothing in this suite can.**
    `grid_access_enabled()` requires `config.grid_mode` AND `private-domain`, and
    `H.start_relay` sets `GRID_MODE=false` for every relay it starts — because the harness mints its
    own JWTs and Grid mode refuses everything but a control-plane-signed token (see this file's
    `relay_home` docstring for the same constraint from the other side). So the pair this fixture
    creates is exactly the misconfiguration the gate exists to refuse, and `test_15` asserts the
    refusal.

    ⚠️ **Read the suite's green count accordingly.** A passing run does NOT mean a colleague
    reaching a project nobody invited them to has been exercised end to end — it means the rule is
    correctly OFF here. The positive path is covered only at unit level, in grid-src's
    `test_project_visibility.py`, against a relay whose config that suite patches. That is a real
    gap, named rather than left for somebody to infer: closing it means teaching `_harness.py` to
    sign against a local JWKS (`config.grid_token_jwks_path`) and giving this fixture a grid-mode
    sibling. Deliberately not done in issue 36; the alternative was leaving the gate open.
    """
    proc, base = H.start_relay(tmp_path_factory.mktemp("relay-private-domain"), extra_env={
        "GRID_NETWORK_TYPE": "private-domain",
    })
    try:
        yield base
    finally:
        H.stop_relay(proc)
