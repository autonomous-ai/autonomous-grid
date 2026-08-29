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

    evidence = build_parser().parse_args([
        "goal", "evidence", "goal-1", "--verify", "--min-execution-nodes", "3",
        "--require-inference",
    ])
    assert evidence.min_execution_nodes == 3
    assert evidence.require_inference is True


def test_goal_run_loads_tools_and_posts_the_resolved_project(monkeypatch, tmp_path, capsys):
    from cli import goal, remote_task
    from remote import relay

    manifest = tmp_path / "tools.json"
    manifest.write_text(json.dumps({"version": 1, "tools": [{
        "name": "tickets", "mode": "observe", "http": {"url": "/tickets"},
    }]}))
    eval_manifest = tmp_path / "evals.json"
    eval_manifest.write_text(json.dumps({"version": 1, "evals": [{
        "type": "file", "name": "README exists", "path": "README.md",
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
        tools=str(manifest), evals=str(eval_manifest), name="support", grid=None, json=True,
    )
    assert goal.cmd_goal(args) == 0
    assert captured == {
        "project_id": "project-id", "objective": "Resolve tickets",
        "done_when": "Backlog is zero", "model": "grid-model", "token_budget": 1234,
        "tools": [{"name": "tickets", "mode": "observe", "http": {"url": "/tickets"}}],
        "name": "support", "agents": ["codex"], "required_capabilities": [],
        "evals": [{"type": "file", "name": "README exists", "path": "README.md"}],
        "allow_subgoals": False,
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


def test_goal_evidence_prints_machine_readable_audit_record(monkeypatch, capsys):
    from cli import goal
    from remote import relay

    monkeypatch.setattr(goal, "_resolve", lambda _args: ("http://relay", "token", "grid"))
    monkeypatch.setattr(relay, "get_goal_evidence", lambda *_args: {
        "goal": {"id": "goal-1"}, "turns": [{"provider_node_id": "node-A"}],
        "inference": [{"provider_node_id": "gpu-B", "model": "model-1"}],
        "eval_runs": [],
    })
    args = SimpleNamespace(goal_action="evidence", goal_id="goal-1", grid=None, verify=False)
    assert goal.cmd_goal(args) == 0
    assert json.loads(capsys.readouterr().out)["turns"][0]["provider_node_id"] == "node-A"


def test_goal_evidence_verify_accepts_an_exact_distributed_chain(monkeypatch, capsys):
    from cli import goal
    from remote import relay

    record = {
        "schema_version": 1,
        "goal": {"id": "goal-1", "status": "complete", "evals": [{
            "definition_id": "eval-1", "definition_hash": "hash-1", "name": "artifact",
        }]},
        "trajectory": {"transcript_pruned": False, "pruned_turn_branches": [],
                       "worktree_chain": [{
                           "from_turn_id": "turn-1", "to_turn_id": "turn-2",
                           "result_commit": "2" * 40, "input_commit": "2" * 40,
                           "ancestor": True, "error": None,
                       }]},
        "turns": [
            {"id": "turn-1", "state": "completed", "agent_kind": "codex",
             "provider_node_id": "node-A",
             "input_commit": "1" * 40, "result_commit": "2" * 40,
             "transcript_commit": None, "transcript_result_commit": "a" * 40},
            {"id": "turn-2", "state": "completed", "agent_kind": "claude",
             "provider_node_id": "node-B",
             "input_commit": "2" * 40, "result_commit": "3" * 40,
             "transcript_commit": "a" * 40, "transcript_result_commit": "b" * 40},
        ],
        "eval_runs": [{
            "turn_id": "turn-2", "definition_id": "eval-1", "definition_hash": "hash-1",
            "result_commit": "3" * 40, "evaluator_node_id": "relay", "state": "passed",
            "score": 1.0, "accepted": True, "accepted_at": "2026-08-29T12:00:00+00:00",
            "passed": True,
        }],
    }
    monkeypatch.setattr(goal, "_resolve", lambda _args: ("http://relay", "token", "grid"))
    monkeypatch.setattr(relay, "get_goal_evidence", lambda *_args: record)

    args = SimpleNamespace(goal_action="evidence", goal_id="goal-1", grid=None, verify=True)
    assert goal.cmd_goal(args) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == record
    assert "Goal evidence verified" in captured.err

    # The same commit can legitimately recur on a later no-op turn. An older turn's accepted score
    # is not evidence that the final completion nomination was independently evaluated.
    record["eval_runs"][0]["turn_id"] = "turn-1"
    failures = goal._verify_evidence(record)
    assert any("from the final turn" in failure for failure in failures)


def test_goal_evidence_verify_refuses_a_broken_handoff(monkeypatch, capsys):
    from cli import goal
    from remote import relay

    record = {
        "schema_version": 1,
        "goal": {"id": "goal-1", "status": "complete", "evals": []},
        "trajectory": {"transcript_pruned": False, "pruned_turn_branches": [],
                       "worktree_chain": [{
                           "from_turn_id": "turn-1", "to_turn_id": "turn-2",
                           "result_commit": "2" * 40, "input_commit": "2" * 40,
                           "ancestor": True, "error": None,
                       }]},
        "turns": [
            {"id": "turn-1", "state": "completed", "agent_kind": "codex",
             "provider_node_id": "node-A",
             "input_commit": "1" * 40, "result_commit": "2" * 40,
             "transcript_commit": None, "transcript_result_commit": "a" * 40},
            {"id": "turn-2", "state": "completed", "agent_kind": "codex",
             "provider_node_id": "node-C",
             "input_commit": "2" * 40, "result_commit": "3" * 40,
             "transcript_commit": "wrong", "transcript_result_commit": "b" * 40},
        ],
        "eval_runs": [],
    }
    monkeypatch.setattr(goal, "_resolve", lambda _args: ("http://relay", "token", "grid"))
    monkeypatch.setattr(relay, "get_goal_evidence", lambda *_args: record)

    args = SimpleNamespace(goal_action="evidence", goal_id="goal-1", grid=None, verify=True)
    with pytest.raises(SystemExit, match="turn 2 transcript input"):
        goal.cmd_goal(args)
    assert json.loads(capsys.readouterr().out) == record


def test_goal_evidence_verify_refuses_pruned_training_objects():
    from cli.goal import _verify_evidence

    record = {
        "schema_version": 1,
        "goal": {"status": "complete", "evals": []},
        "trajectory": {"transcript_pruned": True, "pruned_turn_branches": ["turn-1"]},
        "turns": [{
            "id": "turn-1", "state": "completed", "agent_kind": "codex",
            "provider_node_id": "node-A", "input_commit": "1" * 40,
            "result_commit": "2" * 40, "transcript_commit": None,
            "transcript_result_commit": "a" * 40, "branch_pruned": True,
        }],
        "eval_runs": [],
    }

    failures = _verify_evidence(record)
    assert "the Goal transcript ref has been pruned" in failures
    assert any("turn branches have been pruned" in item for item in failures)


def test_goal_evidence_verify_refuses_an_unrelated_worktree_handoff():
    from cli.goal import _verify_evidence

    record = {
        "schema_version": 1,
        "goal": {"status": "complete", "evals": []},
        "trajectory": {"transcript_pruned": False, "pruned_turn_branches": [],
                       "worktree_chain": [{
                           "from_turn_id": "turn-1", "to_turn_id": "turn-2",
                           "result_commit": "2" * 40, "input_commit": "9" * 40,
                           "ancestor": False, "error": None,
                       }]},
        "turns": [
            {"id": "turn-1", "state": "completed", "agent_kind": "codex",
             "provider_node_id": "node-A", "input_commit": "1" * 40,
             "result_commit": "2" * 40, "transcript_commit": None,
             "transcript_result_commit": "a" * 40},
            {"id": "turn-2", "state": "completed", "agent_kind": "codex",
             "provider_node_id": "node-B", "input_commit": "9" * 40,
             "result_commit": "3" * 40, "transcript_commit": "a" * 40,
             "transcript_result_commit": "b" * 40},
        ],
        "attempt_events": [], "eval_runs": [],
    }
    failures = _verify_evidence(record)
    assert any("input does not contain turn 1 result" in item for item in failures)


def test_goal_evidence_verify_requires_relay_retry_proof_for_reclaimed_turn():
    from cli.goal import _verify_evidence

    record = {
        "schema_version": 1,
        "goal": {"status": "complete", "evals": []},
        "trajectory": {"transcript_pruned": False, "pruned_turn_branches": []},
        "turns": [{
            "id": "turn-1", "attempt": 2, "state": "completed", "agent_kind": "codex",
            "provider_node_id": "node-B", "input_commit": "1" * 40,
            "result_commit": "2" * 40, "transcript_commit": None,
            "transcript_result_commit": "a" * 40,
        }],
        "attempt_events": [],
        "eval_runs": [],
    }
    assert any("no authoritative retry event" in item for item in _verify_evidence(record))

    record["attempt_events"].append({
        "turn_id": "turn-1",
        "event": {"type": "task.retry", "attempt": 1,
                  "previous_provider_id": "node-A", "reason": "lease_expired"},
    })
    assert _verify_evidence(record) == []


def test_goal_evidence_strict_physical_gates_require_nodes_and_grid_inference():
    from cli.goal import _verify_evidence

    record = {
        "schema_version": 1,
        "goal": {"status": "complete", "evals": []},
        "trajectory": {"transcript_pruned": False, "pruned_turn_branches": []},
        "turns": [{
            "id": "turn-1", "attempt": 1, "state": "completed", "agent_kind": "codex",
            "provider_node_id": "node-A", "input_commit": "1" * 40,
            "result_commit": "2" * 40, "transcript_commit": None,
            "transcript_result_commit": "a" * 40,
        }],
        "attempt_events": [], "inference": [], "eval_runs": [],
    }
    failures = _verify_evidence(record, min_execution_nodes=2, require_inference=True)
    assert any("fewer than required 2" in item for item in failures)
    assert any("no model requests attributed" in item for item in failures)

    record["inference"].append({
        "turn_id": "turn-1", "model": "grid-model", "provider_node_id": "gpu-A",
        "requests": 1,
    })
    assert _verify_evidence(record, require_inference=True) == []


def test_goal_status_shows_budget_blocker_and_distributed_children(capsys):
    from cli.goal import _show

    _show({
        "id": "parent", "status": "blocked", "objective": "Ship it",
        "done_when": "checks pass", "turns_completed": 2, "tokens_used": 1_250,
        "token_budget": 10_000, "child_tokens_reserved": 4_000,
        "agents": ["codex", "claude"], "blocked_reason": "child conflict in app.py",
        "children": [{
            "id": "child-1", "status": "complete", "required": True,
            "objective": "Build the API",
        }],
    }, False)
    output = capsys.readouterr().out
    assert "1,250 / 10,000 tokens · 4,000 reserved for children" in output
    assert "agents     codex, claude" in output
    assert "blocked    child conflict in app.py" in output
    assert "child-1 [complete] (required) Build the API" in output
