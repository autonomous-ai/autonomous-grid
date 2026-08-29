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
        "name": "tickets", "mode": "observe",
        "http": {"method": "GET", "url": "https://support.example/tickets"},
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
        "tools": [{"name": "tickets", "mode": "observe",
                   "http": {"method": "GET", "url": "https://support.example/tickets"}}],
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
    # Use the real canonical definition identity exported by the relay.
    definition = record["goal"]["evals"][0]
    definition.update({
        "type": "file", "version": 1, "path": "artifact.txt", "exists": True,
    })
    definition_body = {
        key: value for key, value in definition.items()
        if key not in {"definition_id", "definition_hash"}
    }
    definition_hash = __import__("hashlib").sha256(json.dumps(
        definition_body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")).hexdigest()
    definition["definition_hash"] = definition_hash
    record["eval_runs"][0]["definition_hash"] = definition_hash
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


def test_goal_evidence_verify_recomputes_metric_identity_and_requires_relay_evaluator():
    from cli import goal

    spec = {
        "type": "file", "version": 1, "name": "artifact",
        "path": "artifact.txt", "exists": True,
    }
    definition_hash = __import__("hashlib").sha256(json.dumps(
        spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")).hexdigest()
    record = {
        "schema_version": 1,
        "goal": {"id": "goal-1", "status": "complete", "evals": [{
            **spec, "definition_id": "eval-1", "definition_hash": definition_hash,
        }]},
        "trajectory": {"transcript_pruned": False, "pruned_turn_branches": [],
                       "worktree_chain": []},
        "turns": [{
            "id": "turn-1", "state": "completed", "agent_kind": "codex",
            "provider_node_id": "node-A", "input_commit": "1" * 40,
            "result_commit": "2" * 40, "transcript_commit": None,
            "transcript_result_commit": "a" * 40,
        }],
        "attempt_events": [], "inference": [],
        "eval_runs": [{
            "turn_id": "turn-1", "definition_id": "eval-1",
            "definition_hash": definition_hash, "result_commit": "2" * 40,
            "evaluator_node_id": "relay", "state": "passed", "score": 1.0,
            "accepted": True, "accepted_at": "2026-08-29T12:00:00+00:00",
            "passed": True,
        }],
    }
    assert goal._verify_evidence(record) == []

    record["goal"]["evals"][0]["path"] = "silently-changed.txt"
    assert any("does not match" in failure for failure in goal._verify_evidence(record))
    record["goal"]["evals"][0]["path"] = "artifact.txt"
    record["eval_runs"][0]["evaluator_node_id"] = "node-A"
    assert any("no accepted passing run" in failure for failure in goal._verify_evidence(record))


def test_goal_evidence_verify_proves_native_retry_checkpoint_ancestry():
    from cli import goal

    checkpoint, result = "2" * 40, "3" * 40
    transcript_checkpoint, transcript_result = "a" * 40, "b" * 40
    record = {
        "schema_version": 1,
        "goal": {"id": "goal-1", "status": "complete", "evals": []},
        "trajectory": {
            "transcript_pruned": False, "pruned_turn_branches": [],
            "worktree_chain": [],
            "retry_checkpoint_chain": [{
                "turn_id": "turn-1", "event_seq": 1, "attempt": 1,
                "checkpoint_commit": checkpoint, "result_commit": result,
                "worktree_ancestor": True, "worktree_error": None,
                "transcript_checkpoint_commit": transcript_checkpoint,
                "transcript_result_commit": transcript_result,
                "transcript_ancestor": True, "transcript_error": None,
            }],
        },
        "turns": [{
            "id": "turn-1", "state": "completed", "attempt": 2,
            "agent_kind": "codex", "provider_node_id": "node-B",
            "input_commit": "1" * 40, "checkpoint_commit": checkpoint,
            "result_commit": result, "transcript_commit": None,
            "transcript_checkpoint_commit": transcript_checkpoint,
            "transcript_result_commit": transcript_result,
        }],
        "attempt_events": [{
            "turn_id": "turn-1", "seq": 1, "event": {
                "type": "task.retry", "reason": "native_harness_failure", "attempt": 1,
                "previous_provider_id": "node-A", "previous_agent_kind": "codex",
                "checkpoint_commit": checkpoint,
                "transcript_checkpoint_commit": transcript_checkpoint,
            },
        }],
        "inference": [], "eval_runs": [],
    }
    assert goal._verify_evidence(record, min_execution_nodes=2) == []

    record["trajectory"]["retry_checkpoint_chain"][0]["worktree_ancestor"] = False
    assert any("final worktree does not contain" in failure
               for failure in goal._verify_evidence(record, min_execution_nodes=2))
    record["trajectory"]["retry_checkpoint_chain"][0]["worktree_ancestor"] = True
    record["turns"][0]["transcript_checkpoint_commit"] = "c" * 40
    assert any("stored transcript checkpoint" in failure
               for failure in goal._verify_evidence(record, min_execution_nodes=2))


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
                  "previous_provider_id": "node-A", "previous_agent_kind": "codex",
                  "reason": "lease_expired"},
    })
    assert _verify_evidence(record) == []
    assert _verify_evidence(record, min_execution_nodes=2) == []

    record["attempt_events"][0]["event"]["previous_agent_kind"] = None
    assert any("no relay-authored previous harness identity" in item
               for item in _verify_evidence(record))
    record["attempt_events"][0]["event"]["previous_agent_kind"] = "codex"

    # A provider-authored start marker or a retry naming the eventual winner cannot fabricate a
    # second physical worker for the strict gate.
    record["attempt_events"][0]["event"]["previous_provider_id"] = "node-B"
    assert any("fewer than required 2" in item
               for item in _verify_evidence(record, min_execution_nodes=2))


def test_goal_evidence_verifies_tool_attempts_and_idempotent_reconciliation():
    from cli.goal import _verify_evidence

    key = "grid-goal-" + "a" * 64
    record = {
        "schema_version": 1,
        "goal": {"status": "complete", "evals": []},
        "trajectory": {"transcript_pruned": False, "pruned_turn_branches": []},
        "turns": [{
            "id": "turn-1", "attempt": 2, "state": "completed", "agent_kind": "codex",
            "provider_node_id": "node-C", "input_commit": "1" * 40,
            "result_commit": "2" * 40, "transcript_commit": None,
            "transcript_result_commit": "a" * 40,
        }],
        "attempt_events": [
            {"turn_id": "turn-1", "event": {
                "type": "task.retry", "attempt": 1, "previous_provider_id": "node-B",
                "previous_agent_kind": "codex",
            }},
            {"turn_id": "turn-1", "event": {
                "type": "goal.observe.request", "provider_node_id": "node-B", "attempt": 1,
                "tool": "read_ticket", "call_id": "read-1",
            }},
            {"turn_id": "turn-1", "event": {
                "type": "goal.observe.result", "provider_node_id": "node-B", "attempt": 1,
                "tool": "read_ticket", "call_id": "read-1",
            }},
            # B disappears after its durable request. C safely reconciles the same logical action
            # under a different native call id and the Goal-wide idempotency key.
            {"turn_id": "turn-1", "event": {
                "type": "goal.act.request", "provider_node_id": "node-B", "attempt": 1,
                "tool": "send_reply", "call_id": "act-B", "idempotency_key": key,
            }},
            {"turn_id": "turn-1", "event": {
                "type": "goal.act.request", "provider_node_id": "node-C", "attempt": 2,
                "tool": "send_reply", "call_id": "act-C", "idempotency_key": key,
            }},
            {"turn_id": "turn-1", "event": {
                "type": "goal.act.result", "provider_node_id": "node-C", "attempt": 2,
                "tool": "send_reply", "call_id": "act-C", "idempotency_key": key,
            }},
            # A killed read is safe to repeat, so an unmatched observe/verify request is valid.
            {"turn_id": "turn-1", "event": {
                "type": "goal.verify.request", "provider_node_id": "node-C", "attempt": 2,
                "tool": "check_ticket", "call_id": "verify-1",
            }},
        ],
        "eval_runs": [],
    }
    assert _verify_evidence(record, min_execution_nodes=2) == []


def test_goal_evidence_refuses_ambiguous_or_unattributed_tool_events():
    from cli.goal import _verify_evidence

    key = "grid-goal-" + "b" * 64
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
        "attempt_events": [
            {"turn_id": "turn-1", "event": {
                "type": "goal.act.request", "tool": "missing-attribution", "call_id": "bad",
                "idempotency_key": "spoofed",
            }},
            {"turn_id": "turn-1", "event": {
                "type": "goal.act.request", "provider_node_id": "node-A", "attempt": 1,
                "tool": "unresolved", "call_id": "act-1", "idempotency_key": key,
            }},
            {"turn_id": "turn-1", "event": {
                "type": "goal.act.result", "provider_node_id": "node-A", "attempt": 1,
                "tool": "orphan", "call_id": "act-2",
                "idempotency_key": "grid-goal-" + "c" * 64,
            }},
            {"turn_id": "turn-1", "event": {
                "type": "goal.observe.result", "provider_node_id": "node-A", "attempt": 1,
                "tool": "read_ticket", "call_id": "read-1",
            }},
        ],
        "eval_runs": [],
    }
    failures = _verify_evidence(record)
    assert any("has no valid provider_node_id" in item for item in failures)
    assert any("has no valid attempt" in item for item in failures)
    assert any("has no valid idempotency_key" in item for item in failures)
    assert any("orphan/act-2 has no matching request" in item for item in failures)
    assert any("unresolved/act-1 has no durable result" in item for item in failures)
    assert any("read_ticket/read-1 has no matching request" in item for item in failures)


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
            "id": "child-1", "status": "complete", "required": False,
            "objective": "Explore the API", "merge_state": "skipped",
            "merge_error": "child child-1 conflicts with the parent in app.py",
        }],
    }, False)
    output = capsys.readouterr().out
    assert "1,250 / 10,000 tokens · 4,000 reserved for children" in output
    assert "agents     codex, claude" in output
    assert "blocked    child conflict in app.py" in output
    assert "child-1 [complete] (optional) Explore the API · fan-in skipped" in output
    assert "child child-1 conflicts with the parent in app.py" in output
