"""Native Claude `/goal` slicing and Grid inference credential confinement."""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

from remote import task_agent, task_codex, task_stream, tasks

_REAL_REQUIRE_DISTRIBUTED_GOAL = task_agent.require_distributed_goal


@pytest.fixture(autouse=True)
def _measured_native_goal_binary(monkeypatch):
    """Most runner tests use /fake/claude; isolate the admission gate to its own tests."""
    monkeypatch.setattr(task_agent, "require_distributed_goal", lambda binary: binary)


def _attachment(*, met: bool, reason: str, **counters):
    return json.dumps({
        "type": "attachment",
        "attachment": {"type": "goal_status", "met": met, "reason": reason, **counters},
    }) + "\n"


def test_goal_stream_distinguishes_set_check_and_completion():
    translated = task_stream.GoalStreamTranslator()
    assert translated.feed(json.dumps({
        "type": "attachment", "attachment": {
            "type": "goal_status", "sentinel": True, "met": False, "condition": "tests pass"},
    }))[0][0] == "goal.claude.set"
    assert not translated.goal_evaluated

    events = translated.feed(_attachment(met=False, reason="two tests still fail"))
    assert events[0][0] == "goal.claude.evaluated"
    assert translated.goal_evaluated and not translated.goal_met
    assert translated.goal_reason == "two tests still fail"

    completed = task_stream.GoalStreamTranslator()
    completed.feed(_attachment(
        met=True, reason="all tests passed", iterations=3, durationMs=1200, tokens=456))
    assert completed.goal_met
    assert (completed.goal_iterations, completed.goal_duration_ms, completed.goal_tokens) == (
        3, 1200, 456)


def test_goal_stream_rejects_a_contradictory_terminal_attachment():
    translated = task_stream.GoalStreamTranslator()
    translated.feed(json.dumps({
        "type": "attachment", "attachment": {
            "type": "goal_status", "met": True, "impossible": True,
            "reason": "contradictory future protocol",
        },
    }) + "\n")

    assert translated.goal_evaluated
    assert "both met and impossible" in str(translated.goal_protocol_error)


def test_child_is_interrupted_only_after_an_unmet_native_evaluation():
    translator = task_stream.GoalStreamTranslator()
    program = "\n".join((
        "import json, signal, sys, time",
        "signal.signal(signal.SIGINT, lambda *_: sys.exit(130))",
        "print(json.dumps({'type':'attachment','attachment':{'type':'goal_status',"
        "'met':False,'reason':'continue'}}), flush=True)",
        "time.sleep(30)",
    ))
    started = time.monotonic()
    returncode, _ = tasks._run_child(
        [sys.executable, "-c", program], timeout=10, publish=lambda *_a, **_k: None,
        translator=translator,
        stop_when=lambda: translator.goal_evaluated and not translator.goal_met)
    assert returncode != 0  # SIGINT is intentional and therefore not raised as `_ChildFailed`.
    assert time.monotonic() - started < 5


def test_claude_goal_uses_native_command_and_loopback_grid_model(tmp_path, monkeypatch):
    monkeypatch.setenv("GRID_TASK_ROOT", str(tmp_path / "tasks"))
    monkeypatch.setattr(task_agent, "resolve_binary", lambda: "/fake/claude")
    monkeypatch.setattr(task_agent, "preflight", lambda: None)
    monkeypatch.setattr(task_agent, "link_transcript", lambda *_args: tmp_path / "transcript")
    monkeypatch.setattr(task_agent, "resumable_session", lambda *_args: task_agent.ResumeDecision())
    monkeypatch.setattr(task_agent, "child_env", lambda **_kwargs: {"PATH": os.environ["PATH"]})
    captured = {}

    def argv(binary, prompt, *, workspace, resume=None):
        captured["prompt"] = prompt
        captured["resume"] = resume
        return [binary, "-p", prompt]

    def run_child(_argv, **kwargs):
        captured["env"] = kwargs["env"]
        translator = kwargs["translator"]
        translator.feed(json.dumps({
            "type": "system", "subtype": "init", "session_id": "claude-session-1"}) + "\n")
        translator.feed(json.dumps({
            "type": "assistant", "message": {"usage": {"input_tokens": 10, "output_tokens": 5},
                "content": [{"type": "text", "text": "implemented feature one"}]}}) + "\n")
        translator.feed(_attachment(met=False, reason="three features remain"))
        assert kwargs["stop_when"]()
        return 130, ""

    monkeypatch.setattr(task_agent, "agent_argv", argv)
    monkeypatch.setattr(tasks, "_run_child", run_child)
    monkeypatch.setattr(tasks.task_codex_proxy.InferenceProxy, "start", lambda self: None)
    monkeypatch.setattr(tasks.task_codex_proxy.InferenceProxy, "stop", lambda self: None)

    outcome = tasks.run_task({
        "task_id": "turn-1", "conversation_id": "goal-1", "project_id": "project-1",
        "member_key": "member-1", "agent_kind": "claude",
        "prompt": "Codex finished feature one. Continue with the failed collision check.",
        "goal": {"objective": "Build four features", "done_when": "all checks pass",
                 "model": "grid-model", "turns_completed": 2, "tokens_used": 100,
                 "time_used_seconds": 20, "token_budget": 115},
    }, inference=task_codex.GridInference("https://grid.example/relay/v1", "GRID-SECRET"))

    assert captured["prompt"].startswith("/goal Build four features")
    assert "Grid handoff for this distributed turn:" in captured["prompt"]
    assert "Codex finished feature one" in captured["prompt"]
    assert outcome.state == "completed" and outcome.goal_status == "budget_limited"
    assert outcome.session_id == "claude-session-1"
    assert outcome.goal_turns_completed == 3 and outcome.goal_tokens_used == 115
    assert outcome.output == "implemented feature one"
    assert captured["env"]["ANTHROPIC_MODEL"] == "grid-model"
    assert captured["env"]["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")
    assert captured["env"]["ANTHROPIC_AUTH_TOKEN"] != "GRID-SECRET"
    assert "GRID-SECRET" not in repr(captured)


def test_claude_goal_resume_does_not_reset_native_goal(tmp_path, monkeypatch):
    monkeypatch.setenv("GRID_TASK_ROOT", str(tmp_path / "tasks"))
    monkeypatch.setattr(task_agent, "resolve_binary", lambda: "/fake/claude")
    monkeypatch.setattr(task_agent, "preflight", lambda: None)
    monkeypatch.setattr(task_agent, "link_transcript", lambda *_args: tmp_path / "transcript")
    monkeypatch.setattr(task_agent, "resumable_session", lambda *_args: task_agent.ResumeDecision(
        session_id="claude-session-1"))
    monkeypatch.setattr(task_agent, "child_env", lambda **_kwargs: {"PATH": os.environ["PATH"]})
    seen = {}

    def argv(_binary, prompt, *, workspace, resume=None):
        seen.update(prompt=prompt, resume=resume)
        return ["/fake/claude"]

    def run_child(_argv, **kwargs):
        translator = kwargs["translator"]
        translator.session_id = "claude-session-1"
        translator.feed(_attachment(met=True, reason="done", iterations=1, tokens=7))
        return 0, ""

    monkeypatch.setattr(task_agent, "agent_argv", argv)
    monkeypatch.setattr(tasks, "_run_child", run_child)
    monkeypatch.setattr(tasks.task_codex_proxy.InferenceProxy, "start", lambda self: None)
    monkeypatch.setattr(tasks.task_codex_proxy.InferenceProxy, "stop", lambda self: None)
    outcome = tasks.run_task({
        "conversation_id": "goal-1", "project_id": "project-1", "member_key": "member-1",
        "agent_kind": "claude", "prompt": "Continue from the Grid checkpoint",
        "resume_session_id": "claude-session-1",
        "goal": {"objective": "Build it", "done_when": "tests pass", "model": "grid-model"},
    }, inference=task_codex.GridInference("https://grid.example/relay/v1", "secret"))
    assert seen == {"prompt": "Continue from the Grid checkpoint", "resume": "claude-session-1"}
    assert outcome.goal_status == "complete"


@pytest.mark.parametrize("failure", [
    pytest.param("timeout", id="timeout"),
    pytest.param("exit", id="nonzero-exit"),
    pytest.param("no-checkpoint", id="missing-native-checkpoint"),
])
def test_post_spawn_claude_harness_failures_are_retryable(tmp_path, monkeypatch, failure):
    """These failures describe one native process, not whether the distributed Goal is possible."""
    import subprocess

    monkeypatch.setenv("GRID_TASK_ROOT", str(tmp_path / "tasks"))
    monkeypatch.setattr(task_agent, "resolve_binary", lambda: "/fake/claude")
    monkeypatch.setattr(task_agent, "preflight", lambda: None)
    monkeypatch.setattr(task_agent, "link_transcript", lambda *_args: tmp_path / "transcript")
    monkeypatch.setattr(task_agent, "resumable_session", lambda *_args: task_agent.ResumeDecision())
    monkeypatch.setattr(task_agent, "child_env", lambda **_kwargs: {"PATH": os.environ["PATH"]})
    monkeypatch.setattr(task_agent, "agent_argv", lambda *_args, **_kwargs: ["/fake/claude"])
    monkeypatch.setattr(tasks.task_codex_proxy.InferenceProxy, "start", lambda self: None)
    monkeypatch.setattr(tasks.task_codex_proxy.InferenceProxy, "stop", lambda self: None)

    def run_child(_argv, **_kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(cmd="claude", timeout=1)
        if failure == "exit":
            raise tasks._ChildFailed(17, "native process crashed")
        return 0, ""  # no Goal attachment/evaluator checkpoint

    monkeypatch.setattr(tasks, "_run_child", run_child)
    outcome = tasks.run_task({
        "task_id": "turn-1", "conversation_id": "goal-1", "project_id": "project-1",
        "member_key": "member-1", "agent_kind": "claude", "prompt": "continue",
        "goal": {"objective": "Build it", "done_when": "tests pass", "model": "grid-model"},
    }, inference=task_codex.GridInference("https://grid.example/relay/v1", "secret"))

    assert outcome.state == "failed" and outcome.retryable is True


def test_missing_claude_goal_attachment_quarantines_only_that_binary_revision(
        tmp_path, monkeypatch):
    binary = tmp_path / "claude"
    binary.write_text("future claude revision\n")
    monkeypatch.setenv("GRID_TASK_ROOT", str(tmp_path / "tasks"))
    monkeypatch.setenv("GRID_TASK_AGENT_KINDS", "claude")
    monkeypatch.setattr(task_agent, "_DISTRIBUTED_GOAL_FAILURES", {})
    monkeypatch.setattr(task_agent, "_binary_version", lambda _binary: (999, 0, 0))
    monkeypatch.setattr(task_agent, "require_distributed_goal", _REAL_REQUIRE_DISTRIBUTED_GOAL)
    monkeypatch.setattr(task_agent, "resolve_binary", lambda: str(binary))
    monkeypatch.setattr(task_agent, "preflight", lambda: None)
    monkeypatch.setattr(task_agent, "link_transcript", lambda *_args: tmp_path / "transcript")
    monkeypatch.setattr(task_agent, "resumable_session", lambda *_args: task_agent.ResumeDecision())
    monkeypatch.setattr(task_agent, "child_env", lambda **_kwargs: {"PATH": os.environ["PATH"]})
    monkeypatch.setattr(task_agent, "agent_argv", lambda *_args, **_kwargs: [str(binary)])
    monkeypatch.setattr(tasks, "_run_child", lambda *_args, **_kwargs: (0, ""))
    monkeypatch.setattr(tasks.task_codex_proxy.InferenceProxy, "start", lambda self: None)
    monkeypatch.setattr(tasks.task_codex_proxy.InferenceProxy, "stop", lambda self: None)

    outcome = tasks.run_task({
        "task_id": "turn-1", "conversation_id": "goal-1", "project_id": "project-1",
        "member_key": "member-1", "agent_kind": "claude", "prompt": "continue",
        "goal": {"objective": "Build it", "done_when": "tests pass", "model": "grid-model"},
    }, inference=task_codex.GridInference("https://grid.example/relay/v1", "secret"))

    assert outcome.retryable is True
    assert "without a native evaluator checkpoint" in str(outcome.error)
    assert task_agent.distributed_goal_available() is False
    # Claude remains an ordinary task harness, but its scheduler profile loses native_goal.
    assert tasks._agent_profiles() == ({"kind": "claude", "capabilities": []},)

    binary.chmod(0o755)
    assert task_agent.distributed_goal_available() is True


def test_native_claude_impossible_verdict_remains_terminal(tmp_path, monkeypatch):
    """The native Goal's explicit impossible verdict is semantic; another node must not loop it."""
    monkeypatch.setenv("GRID_TASK_ROOT", str(tmp_path / "tasks"))
    monkeypatch.setattr(task_agent, "resolve_binary", lambda: "/fake/claude")
    monkeypatch.setattr(task_agent, "preflight", lambda: None)
    monkeypatch.setattr(task_agent, "link_transcript", lambda *_args: tmp_path / "transcript")
    monkeypatch.setattr(task_agent, "resumable_session", lambda *_args: task_agent.ResumeDecision())
    monkeypatch.setattr(task_agent, "child_env", lambda **_kwargs: {"PATH": os.environ["PATH"]})
    monkeypatch.setattr(task_agent, "agent_argv", lambda *_args, **_kwargs: ["/fake/claude"])
    monkeypatch.setattr(tasks.task_codex_proxy.InferenceProxy, "start", lambda self: None)
    monkeypatch.setattr(tasks.task_codex_proxy.InferenceProxy, "stop", lambda self: None)

    def run_child(_argv, **kwargs):
        translator = kwargs["translator"]
        translator.feed(json.dumps({
            "type": "attachment", "attachment": {"type": "goal_status", "met": False,
                "impossible": True, "reason": "required system does not exist"},
        }) + "\n")
        return 0, ""

    monkeypatch.setattr(tasks, "_run_child", run_child)
    outcome = tasks.run_task({
        "task_id": "turn-1", "conversation_id": "goal-1", "project_id": "project-1",
        "member_key": "member-1", "agent_kind": "claude", "prompt": "continue",
        "goal": {"objective": "Build it", "done_when": "tests pass", "model": "grid-model"},
    }, inference=task_codex.GridInference("https://grid.example/relay/v1", "secret"))

    assert outcome.state == "completed" and outcome.goal_status == "failed"
    assert outcome.retryable is False


def test_internal_subgoal_tool_carries_grid_auth_lease_fence_and_idempotency(monkeypatch):
    captured = []

    class Response:
        status_code = 201

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def iter_bytes():
            yield b'{"id":"child-goal"}'

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, method, url, **kwargs):
            captured.append({"method": method, "url": url, **kwargs})
            return Response()

    monkeypatch.setattr(task_codex.httpx, "Client", Client)
    executor = task_codex.ToolExecutor([{
        "name": "grid_spawn_subgoal", "grid_internal": True, "mode": "act",
        "http": {
            "method": "POST", "url": "/relay/v1/goals/parent/children", "auth": "grid",
            "headers": {"X-Grid-Goal-Turn": "turn-1", "X-Not-Allowed": "secret"},
        },
    }], publish=lambda *_a, **_k: None,
        inference=task_codex.GridInference("https://grid.example", "grid-token"),
        scope="parent", turn_scope="parent:1")
    result = executor.call(
        "grid_spawn_subgoal", {"objective": "child"}, "call-1")
    assert result["success"] is True
    # A replacement machine may replay the same logical action with a new transient Codex call id.
    # The receiving business API must see the same key for that retry, but a later Grid Goal turn
    # must get a new key so an intentional repeated action is not suppressed forever.
    assert executor.call(
        "grid_spawn_subgoal", {"objective": "child"}, "replacement-call")["success"] is True
    later = task_codex.ToolExecutor(executor.tools.values(), publish=lambda *_a, **_k: None,
        inference=task_codex.GridInference("https://grid.example", "grid-token"),
        scope="parent", turn_scope="parent:2")
    assert later.call(
        "grid_spawn_subgoal", {"objective": "child"}, "call-1")["success"] is True

    assert captured[0]["url"] == "https://grid.example/relay/v1/goals/parent/children"
    assert captured[0]["headers"]["Authorization"] == "Bearer grid-token"
    assert captured[0]["headers"]["X-Grid-Goal-Turn"] == "turn-1"
    assert "X-Not-Allowed" not in captured[0]["headers"]
    keys = [request["headers"]["Idempotency-Key"] for request in captured]
    assert keys[0].startswith("grid-goal-")
    assert keys[0] == keys[1]
    assert keys[2] != keys[0]


def test_internal_subgoal_tool_refreshes_expired_node_token_without_repeating_action(monkeypatch):
    calls = []
    live = {"token": "expired-node-token"}

    class Response:
        def __init__(self, status_code):
            self.status_code = status_code

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def iter_bytes(self):
            yield b'{"id":"child-goal"}'

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, _method, _url, **kwargs):
            calls.append(kwargs["headers"].copy())
            return Response(401 if len(calls) == 1 else 201)

    refreshes = []

    def refresh(stale):
        refreshes.append(stale)
        live["token"] = "fresh-node-token"
        return True

    monkeypatch.setattr(task_codex.httpx, "Client", Client)
    executor = task_codex.ToolExecutor([{
        "name": "grid_spawn_subgoal", "grid_internal": True, "mode": "act",
        "http": {
            "method": "POST", "url": "/relay/v1/goals/parent/children", "auth": "grid",
            "headers": {"X-Grid-Goal-Turn": "turn-1"},
        },
    }], publish=lambda *_a, **_k: None,
        inference=task_codex.GridInference(
            "https://grid.example", lambda: live["token"], refresh),
        scope="parent", turn_scope="parent:1")

    result = executor.call("grid_spawn_subgoal", {"objective": "child"}, "call-1")

    assert result["success"] is True
    assert refreshes == ["expired-node-token"]
    assert [call["Authorization"] for call in calls] == [
        "Bearer expired-node-token", "Bearer fresh-node-token"]
    # The same idempotency key makes the one authentication retry safe even for an act tool.
    assert calls[0]["Idempotency-Key"] == calls[1]["Idempotency-Key"]


def test_claude_profile_cannot_claim_grid_runner_capabilities_it_does_not_wire(monkeypatch):
    monkeypatch.setenv("GRID_TASK_AGENT_KINDS", "claude")
    monkeypatch.setenv(
        "GRID_CLAUDE_TASK_CAPABILITIES",
        "native_goal dynamic_tools subgoals image_generation")
    monkeypatch.setattr(task_agent, "distributed_goal_available", lambda: True)

    assert tasks._agent_profiles() == ({
        "kind": "claude", "capabilities": ["native_goal"],
    },)


def test_codex_profile_advertises_only_operator_approved_tool_origins(monkeypatch):
    monkeypatch.setenv("GRID_TASK_AGENT_KINDS", "codex")
    monkeypatch.setenv(
        "GRID_GOAL_TOOL_ORIGINS",
        "https://SUPPORT.example:443/,http://finance.internal:8080,https://bad.example/path")
    monkeypatch.setattr(task_codex, "available", lambda: True)
    capabilities = set(tasks._agent_profiles()[0]["capabilities"])
    assert {"native_goal", "dynamic_tools", "subgoals"} <= capabilities
    assert task_codex.goal_tool_origin_capabilities() <= capabilities
    assert len([item for item in capabilities if item.startswith("tool_origin.")]) == 2


def test_full_tool_recording_captures_training_payloads_but_redacts_secrets(monkeypatch):
    events = []
    monkeypatch.setenv("GRID_GOAL_TOOL_ORIGINS", "https://support.example")

    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def iter_bytes():
            yield json.dumps({
                "ticket": {"text": "customer needs help", "access_token": "response-secret"},
            }).encode()

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(task_codex.httpx, "Client", Client)
    executor = task_codex.ToolExecutor([{
        "name": "read_ticket", "mode": "observe", "record": "full",
        "http": {"method": "GET", "url": "https://support.example/ticket"},
    }], publish=lambda event_type, **payload: events.append({"type": event_type, **payload}),
        inference=task_codex.GridInference("https://grid.example", "grid-secret"), scope="goal:1")
    result = executor.call(
        "read_ticket", {"ticket_id": "T-1", "api_key": "argument-secret"}, "call-1")

    assert result["success"] is True
    assert events[0]["arguments"] == {"ticket_id": "T-1", "api_key": "[REDACTED]"}
    assert events[1]["result"]["body"]["ticket"] == {
        "text": "customer needs help", "access_token": "[REDACTED]"}
    assert "argument-secret" not in repr(events)
    assert "response-secret" not in repr(events)


def test_business_tools_fail_closed_without_an_exact_operator_approved_origin(monkeypatch):
    calls = []

    class Client:
        def __init__(self, **_kwargs):
            calls.append("constructed")

    monkeypatch.setattr(task_codex.httpx, "Client", Client)
    monkeypatch.setenv("GRID_GOAL_TOOL_ORIGINS", "https://support.example")
    tools = [{
        "name": "read_ticket", "mode": "observe",
        "http": {"method": "GET", "url": "https://evil.support.example/ticket"},
    }]
    executor = task_codex.ToolExecutor(
        tools, publish=lambda *_a, **_k: None,
        inference=task_codex.GridInference("https://grid.example", "grid-secret"), scope="goal:1")
    result = executor.call("read_ticket", {"ticket_id": "T-1"}, "call-1")
    assert result["success"] is False
    assert "not approved" in result["contentItems"][0]["text"]
    assert calls == []


def test_user_tool_cannot_borrow_grid_auth_or_a_relative_relay_url(monkeypatch):
    calls = []
    monkeypatch.setattr(
        task_codex.httpx, "Client", lambda **_kwargs: calls.append("constructed"))
    executor = task_codex.ToolExecutor([{
        "name": "steal", "mode": "observe",
        "http": {"method": "GET", "url": "/relay/v1/goals", "auth": "grid"},
    }], publish=lambda *_a, **_k: None,
        inference=task_codex.GridInference("https://grid.example", "grid-secret"), scope="goal:1")
    result = executor.call("steal", {}, "call-1")
    assert result["success"] is False
    assert calls == []
    assert "grid-secret" not in repr(result)


def test_duplicate_tool_names_are_not_exposed_to_codex():
    executor = task_codex.ToolExecutor([
        {"name": "same", "mode": "observe", "http": {
            "method": "GET", "url": "https://one.example/read"}},
        {"name": "same", "mode": "act", "http": {
            "method": "POST", "url": "https://two.example/write"}},
    ], publish=lambda *_a, **_k: None,
        inference=task_codex.GridInference("https://grid.example", "grid-secret"), scope="goal:1")
    assert executor.specs() == []
    assert executor.call("same", {}, "call-1")["success"] is False


def test_tool_arguments_and_response_bodies_are_hard_bounded(monkeypatch):
    monkeypatch.setenv("GRID_GOAL_TOOL_ORIGINS", "https://support.example")
    calls = []

    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def iter_bytes():
            yield b"x" * task_codex._MAX_TOOL_HTTP_BYTES
            yield b"overflow"

    class Client:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(task_codex.httpx, "Client", Client)
    executor = task_codex.ToolExecutor([{
        "name": "read_ticket", "mode": "observe",
        "http": {"method": "GET", "url": "https://support.example/ticket"},
    }], publish=lambda *_a, **_k: None,
        inference=task_codex.GridInference("https://grid.example", "grid-secret"), scope="goal:1")
    too_large = executor.call(
        "read_ticket", {"value": "x" * task_codex._MAX_TOOL_ARGUMENT_BYTES}, "call-1")
    assert too_large["success"] is False and calls == []
    oversized_response = executor.call("read_ticket", {"ticket_id": "T-1"}, "call-2")
    assert oversized_response["success"] is False
    assert "response exceeds" in oversized_response["contentItems"][0]["text"]
    assert calls[0]["trust_env"] is False
    assert calls[0]["follow_redirects"] is False


def test_tool_does_not_act_until_its_request_audit_is_durable(monkeypatch):
    monkeypatch.setenv("GRID_GOAL_TOOL_ORIGINS", "https://support.example")
    calls = []
    monkeypatch.setattr(
        task_codex.httpx, "Client", lambda **_kwargs: calls.append("constructed"))
    executor = task_codex.ToolExecutor([{
        "name": "send_reply", "mode": "act",
        "http": {"method": "POST", "url": "https://support.example/reply"},
    }], publish=lambda *_a, **_k: False,
        inference=task_codex.GridInference("https://grid.example", "secret"), scope="goal:1")
    result = executor.call("send_reply", {"reply": "hello"}, "call-1")
    assert result["success"] is False
    assert "durably record tool request" in result["contentItems"][0]["text"]
    assert calls == []


def test_tool_fails_the_turn_if_its_committed_result_cannot_be_recorded(monkeypatch):
    monkeypatch.setenv("GRID_GOAL_TOOL_ORIGINS", "https://support.example")
    publishes = []

    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def iter_bytes():
            yield b'{"reply_id":"R-1"}'

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, *_args, **_kwargs):
            return Response()

    def publish(event_type, **_fields):
        publishes.append(event_type)
        return len(publishes) == 1

    monkeypatch.setattr(task_codex.httpx, "Client", Client)
    executor = task_codex.ToolExecutor([{
        "name": "send_reply", "mode": "act",
        "http": {"method": "POST", "url": "https://support.example/reply"},
    }], publish=publish,
        inference=task_codex.GridInference("https://grid.example", "secret"), scope="goal:1")
    with pytest.raises(task_codex.CodexGoalError, match="durably record tool result"):
        executor.call("send_reply", {"reply": "hello"}, "call-1")
    assert publishes == ["goal.act.request", "goal.act.result"]


def test_business_action_key_survives_a_later_eval_repair_turn(monkeypatch):
    monkeypatch.setenv("GRID_GOAL_TOOL_ORIGINS", "https://support.example")
    requests = []

    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def iter_bytes():
            yield b'{"reply_id":"R-1"}'

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, _method, _url, **kwargs):
            requests.append(kwargs)
            return Response()

    monkeypatch.setattr(task_codex.httpx, "Client", Client)
    tool = {
        "name": "send_reply", "mode": "act",
        "http": {"method": "POST", "url": "https://support.example/reply"},
    }
    for turn in (1, 2):
        executor = task_codex.ToolExecutor(
            [tool], publish=lambda *_a, **_k: True,
            inference=task_codex.GridInference("https://grid.example", "secret"),
            scope="goal-1", turn_scope=f"goal-1:{turn}")
        assert executor.call("send_reply", {"reply": "same"}, f"call-{turn}")["success"]
    assert requests[0]["headers"]["Idempotency-Key"] == requests[1]["headers"]["Idempotency-Key"]
