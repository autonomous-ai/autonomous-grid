"""Native Claude `/goal` slicing and Grid inference credential confinement."""
from __future__ import annotations

import json
import os
import sys
import time

from remote import task_agent, task_codex, task_stream, tasks


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
        "member_key": "member-1", "agent_kind": "claude", "prompt": "continue",
        "goal": {"objective": "Build four features", "done_when": "all checks pass",
                 "model": "grid-model", "turns_completed": 2, "tokens_used": 100,
                 "time_used_seconds": 20, "token_budget": 115},
    }, inference=task_codex.GridInference("https://grid.example/relay/v1", "GRID-SECRET"))

    assert captured["prompt"].startswith("/goal Build four features")
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
