from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


def test_goal_run_parser_requires_a_measurable_condition_and_model():
    from cli.parser import build_parser

    args = build_parser().parse_args([
        "goal", "run", "project-1", "--objective", "Build it",
        "--done-when", "All checks pass", "--model", "grid-model",
    ])
    assert args.goal_action == "run"
    assert args.project_id == "project-1"
    assert args.objective == "Build it"
    assert args.done_when == "All checks pass"
    assert args.model == "grid-model"


def test_goal_run_loads_tools_and_posts_the_resolved_project(monkeypatch, tmp_path, capsys):
    from cli import goal, remote_task
    from remote import relay

    manifest = tmp_path / "tools.json"
    manifest.write_text(json.dumps({"version": 1, "tools": [{
        "name": "tickets", "mode": "observe", "http": {"url": "/tickets"},
    }]}))
    monkeypatch.setattr(goal, "_resolve", lambda _args: ("http://relay", "token", "grid"))
    monkeypatch.setattr(remote_task, "_resolve_project", lambda *_args: "project-id")
    captured = {}

    def create(*args, **kwargs):
        captured.update(kwargs)
        return {"id": "goal-1", "status": "active", "objective": kwargs["objective"],
                "done_when": kwargs["done_when"]}

    monkeypatch.setattr(relay, "create_goal", create)
    args = SimpleNamespace(
        goal_action="run", project="project-id", objective="Resolve tickets",
        done_when="Backlog is zero", model="grid-model", token_budget=1234,
        tools=str(manifest), name="support", grid=None, json=True,
    )
    assert goal.cmd_goal(args) == 0
    assert captured == {
        "project_id": "project-id", "objective": "Resolve tickets",
        "done_when": "Backlog is zero", "model": "grid-model", "token_budget": 1234,
        "tools": [{"name": "tickets", "mode": "observe", "http": {"url": "/tickets"}}],
        "name": "support",
    }
    assert json.loads(capsys.readouterr().out)["id"] == "goal-1"


@pytest.mark.parametrize("action", ["pause", "resume", "cancel"])
def test_goal_control_uses_the_named_goal_and_action(monkeypatch, action):
    from cli import goal
    from remote import relay

    monkeypatch.setattr(goal, "_resolve", lambda _args: ("http://relay", "token", "grid"))
    seen = []
    monkeypatch.setattr(relay, "control_goal", lambda *args: seen.append(args) or {
        "id": "goal-1", "status": action, "objective": "x", "done_when": "y"})
    args = SimpleNamespace(goal_action=action, goal_id="goal-1", grid=None, json=True)
    assert goal.cmd_goal(args) == 0
    assert seen == [("http://relay", "token", "goal-1", action)]


def test_goal_tools_rejects_a_non_array_manifest(tmp_path):
    from cli.goal import _tools

    manifest = tmp_path / "tools.json"
    manifest.write_text('{"version":1,"tools":"not a list"}')
    with pytest.raises(SystemExit, match="tools array"):
        _tools(str(manifest))
