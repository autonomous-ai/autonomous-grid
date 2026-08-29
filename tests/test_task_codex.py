"""Native Codex Goal slices and machine-independent Git checkpoint handoff."""
from __future__ import annotations

import io
import json
import subprocess
from email.message import Message
from pathlib import Path

from remote import task_codex, task_codex_proxy, task_repo


class _OpenStringIO(io.StringIO):
    def close(self):
        pass


class _FakeProcess:
    def __init__(self, messages):
        self.stdin = _OpenStringIO()
        self.stdout = io.StringIO("".join(json.dumps(row) + "\n" for row in messages))
        self.stderr = io.StringIO()
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def kill(self):
        self.returncode = -9


def _messages(status="complete"):
    return [
        {"id": 1, "result": {"userAgent": "codex"}},
        {"id": 2, "result": {"thread": {"id": "thread-portable"}}},
        {"method": "turn/started", "params": {"threadId": "thread-portable"}},
        {"id": 3, "result": {"goal": {"status": "active"}}},
        {"method": "thread/tokenUsage/updated", "params": {
            "tokenUsage": {"total": {"totalTokens": 450}}}},
        {"method": "turn/completed", "params": {
            "turn": {"id": "turn-1", "status": "completed", "output": "done"}}},
        {"id": 4, "result": {"goal": {"status": "paused"}}},
        {"id": 5, "result": {"goal": {
            "status": status, "tokensUsed": 321, "timeUsedSeconds": 9}}},
    ]


def _job(**changes):
    value = {
        "task_id": "turn-1", "conversation_id": "goal-1", "agent_kind": "codex",
        "goal": {
            "objective": "Build the game", "done_when": "all four features and tests pass",
            "model": "grid-coder", "tools": [], "token_budget": 10_000,
            "turns_completed": 0, "tokens_used": 0, "time_used_seconds": 0,
        },
    }
    value.update(changes)
    return value


def test_goal_inference_proxy_attributes_requests_to_durable_turn_and_conversation():
    class Handler:
        headers = Message()

    Handler.headers["Content-Type"] = "application/json"
    proxy = task_codex_proxy.InferenceProxy(
        "https://grid.test/relay/v1", "grid-secret",
        turn_id="turn-1", conversation_id="goal-1")
    try:
        headers = proxy._upstream_headers(Handler())
    finally:
        proxy.server.server_close()

    assert headers["Authorization"] == "Bearer grid-secret"
    assert headers["X-Request-Id"] == "turn-1"
    assert headers["X-Grid-Conversation"] == "goal-1"


def test_codex_goal_capability_requires_a_measured_native_goal_version(monkeypatch):
    monkeypatch.setattr(task_codex.shutil, "which", lambda _name: "/fake/codex")
    monkeypatch.setattr(task_codex, "_binary_version", lambda _binary: (0, 150, 0))

    assert task_codex.available() is False
    try:
        task_codex.resolve_binary()
    except task_codex.CodexGoalError as exc:
        assert "install 0.150.1 or newer" in str(exc)
    else:
        raise AssertionError("an old Codex binary was accepted as a native Goal provider")

    monkeypatch.setattr(task_codex, "_binary_version", lambda _binary: (0, 150, 1))
    assert task_codex.available() is True
    assert task_codex.resolve_binary() == "/fake/codex"


def test_one_native_turn_is_checkpointed_below_grid_agent(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(task_codex.InferenceProxy, "start", lambda self: None)
    monkeypatch.setattr(task_codex.InferenceProxy, "stop", lambda self: None)
    process = _FakeProcess(_messages())
    result = task_codex.run_slice(
        _job(), tmp_path, inference=task_codex.GridInference("https://grid.test", "secret"),
        executable="/fake/codex", timeout=30,
        publish=lambda event, **fields: events.append((event, fields)),
        process_factory=lambda argv, env, cwd: process)

    assert result.status == "complete"
    assert result.thread_id == "thread-portable"
    assert result.turns_completed == 1 and result.tokens_used == 450
    checkpoint = json.loads((tmp_path / ".grid/agent/codex/goal-state.json").read_text())
    assert checkpoint["thread_id"] == "thread-portable"
    sent = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
    assert sent[0]["method"] == "initialize"
    assert sent[1] == {"method": "initialized"}
    # Creating the thread is the only thread/start request. Activating a native Codex Goal starts
    # its turn automatically; Grid must not add a second, ordinary user turn of its own.
    starts = [row for row in sent if row.get("method") == "thread/start"]
    assert len(starts) == 1
    assert starts[0]["params"]["sandbox"] == "workspace-write"
    assert any(row.get("method") == "thread/goal/set"
               and row["params"].get("status") == "paused" for row in sent)
    assert "secret" not in process.stdin.getvalue()
    assert events[-1][0] == "goal.slice.completed"


def _git(directory: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=directory, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _remote(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q", "-b", "main")
    _git(source, "config", "user.name", "test")
    _git(source, "config", "user.email", "test@example.invalid")
    (source / "README.md").write_text("start\n")
    _git(source, "add", "README.md")
    _git(source, "commit", "-q", "-m", "start")
    commit = _git(source, "rev-parse", "HEAD")
    bare = tmp_path / "relay.git"
    _git(tmp_path, "clone", "-q", "--bare", str(source), str(bare))
    return bare, commit


def _workspace(root: Path) -> Path:
    return root / "project" / "member" / "goal-conversation" / "workspace"


def test_three_isolated_workers_reconstruct_goal_only_from_relay_git(tmp_path):
    bare, initial = _remote(tmp_path)
    url = str(bare)

    # Worker A has its own object store, worktree and Codex home.
    _git(bare, "branch", "task/A", initial)
    a = _workspace(tmp_path / "node-a")
    task_repo.materialize(a, url=url, token="", branch="task/A", input_commit=initial)
    (a / "feature-1.txt").write_text("worker A\n")
    state_a = task_codex.state_dir(a)
    (state_a / "home").mkdir()
    (state_a / "home/state.sqlite").write_bytes(b"codex-a")
    (state_a / task_codex.STATE_FILE).write_text(json.dumps({
        "thread_id": "portable-thread", "turns_completed": 1}))
    pin_a = task_repo.push_transcript(a, url=url, token="",
                                      ref=task_repo.transcript_ref("goal-conversation"))
    result_a = task_repo.commit_and_push(
        a, url=url, token="", branch="task/A", message="A").commit

    # Worker B starts with no files from A. Materialize must fetch both commits from relay Git.
    _git(bare, "branch", "task/B", result_a)
    b = _workspace(tmp_path / "node-b")
    task_repo.materialize(
        b, url=url, token="", branch="task/B", input_commit=result_a,
        transcript_ref=task_repo.transcript_ref("goal-conversation"), transcript_commit=pin_a)
    assert (b / "feature-1.txt").read_text() == "worker A\n"
    assert json.loads((task_codex.state_dir(b) / task_codex.STATE_FILE).read_text())[
        "thread_id"] == "portable-thread"
    assert (task_codex.state_dir(b) / "home/state.sqlite").read_bytes() == b"codex-a"
    (b / "feature-2.txt").write_text("worker B\n")
    (task_codex.state_dir(b) / "home/state.sqlite").write_bytes(b"codex-b")
    (task_codex.state_dir(b) / task_codex.STATE_FILE).write_text(json.dumps({
        "thread_id": "portable-thread", "turns_completed": 2}))
    pin_b = task_repo.push_transcript(b, url=url, token="",
                                      ref=task_repo.transcript_ref("goal-conversation"))
    result_b = task_repo.commit_and_push(
        b, url=url, token="", branch="task/B", message="B").commit

    # Worker C is isolated again and sees exactly the state B published.
    _git(bare, "branch", "task/C", result_b)
    c = _workspace(tmp_path / "node-c")
    task_repo.materialize(
        c, url=url, token="", branch="task/C", input_commit=result_b,
        transcript_ref=task_repo.transcript_ref("goal-conversation"), transcript_commit=pin_b)
    assert (c / "feature-1.txt").is_file() and (c / "feature-2.txt").is_file()
    assert (task_codex.state_dir(c) / "home/state.sqlite").read_bytes() == b"codex-b"
    assert json.loads((task_codex.state_dir(c) / task_codex.STATE_FILE).read_text())[
        "turns_completed"] == 2


def test_retry_discards_same_machine_state_newer_than_the_pinned_commit(tmp_path):
    bare, initial = _remote(tmp_path)
    url = str(bare)
    _git(bare, "branch", "task/retry", initial)
    workspace = _workspace(tmp_path / "node-a")
    task_repo.materialize(workspace, url=url, token="", branch="task/retry",
                          input_commit=initial)
    state = task_codex.state_dir(workspace)
    (state / task_codex.STATE_FILE).write_text('{"turns_completed":1}')
    pin = task_repo.push_transcript(workspace, url=url, token="",
                                    ref=task_repo.transcript_ref("goal-conversation"))
    # Failed attempt left newer local state and an extra SQLite WAL not present in the pin.
    (state / task_codex.STATE_FILE).write_text('{"turns_completed":99}')
    (state / "home").mkdir(exist_ok=True)
    (state / "home/state.sqlite-wal").write_bytes(b"failed-attempt")
    task_repo.materialize(
        workspace, url=url, token="", branch="task/retry", input_commit=initial,
        transcript_ref=task_repo.transcript_ref("goal-conversation"), transcript_commit=pin)
    assert json.loads((state / task_codex.STATE_FILE).read_text())["turns_completed"] == 1
    assert not (state / "home/state.sqlite-wal").exists()
