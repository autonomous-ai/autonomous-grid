"""A three-node Grid Goal handoff through the real relay task and Git planes.

Run with the matching relay worktree:

    GRID_SRC_REPO=/path/to/autonomous-grid-cli uv run pytest \
      tests/e2e_cross_repo/e2e_goal.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _harness as H

sys.path.insert(0, str(H.GRID_REPO))

GAME_EVALS = [
    {"type": "file", "name": "game page", "path": "index.html", "min_bytes": 10},
    {"type": "file", "name": "game logic", "path": "game.js", "min_bytes": 10},
    {"type": "file", "name": "game styles", "path": "style.css", "min_bytes": 10},
    {"type": "file", "name": "game instructions", "path": "README.md", "min_bytes": 10},
]


def _tasks(relay: str, token: str, project_id: str, conversation_id: str) -> list[dict]:
    from remote import relay as relay_client

    page = relay_client.list_tasks(relay, token, project_id, mine=True, limit=50)
    return [row for row in page.get("tasks") or ()
            if row.get("conversation_id") == conversation_id]


def _partial(root: Path, name: str):
    return next(iter(root.rglob(name)), None) if root.exists() else None


def _completed_goal(relay: str, token: str, conversation_id: str) -> dict | None:
    from remote import relay as relay_client

    value = relay_client.get_goal(relay, token, conversation_id)
    return value if value.get("status") == "complete" else None


def test_three_nodes_reclaim_goal_turns_and_finish_one_game(
        relay, owner_token, spawn_goal_provider, goal_workspace_root, tmp_path):
    from remote import relay as relay_client
    from remote import task_repo

    project_id = relay_client.create_project(
        relay, owner_token, name="p-distributed-goal")["id"]
    H.seed_trunk(relay, owner_token, project_id)

    node_a = spawn_goal_provider("A")
    goal = relay_client.create_goal(
        relay, owner_token, project_id=project_id,
        objective="Build a small playable browser click game with four features",
        done_when="index.html, game.js, style.css and README.md exist and the Goal is complete",
        model="fake-grid-model", token_budget=10_000, tools=[], evals=GAME_EVALS)
    conversation_id = goal["id"]

    # A finishes feature 1 (turn 1), receives a new relay row, then disappears during feature 2.
    assert H.wait_for(
        lambda: _partial(goal_workspace_root / "A", "partial-feature-2.tmp"), timeout=20), (
        f"A never reached feature 2; rows={_tasks(relay, owner_token, project_id, conversation_id)!r}; "
        f"goal={relay_client.get_goal(relay, owner_token, conversation_id)!r}; "
        f"provider output:\n{node_a.output()}")
    rows = _tasks(relay, owner_token, project_id, conversation_id)
    assert len(rows) == 2, rows
    assert rows[0]["state"] == "completed" and rows[0]["attempt"] == 1
    assert rows[0]["provider_id"] == node_a.node_id
    second_turn = rows[1]["id"]
    assert rows[1]["state"] == "running" and rows[1]["attempt"] == 1
    assert rows[1]["provider_id"] == node_a.node_id
    node_a.die()

    # B owns a different disk. Reaper reclaims the SAME turn row; B reconstructs feature 1 and the
    # Codex home from the relay's branch + refs/grid/agent side-ref, completes feature 2, then dies
    # while working on the next row.
    node_b = spawn_goal_provider("B")
    assert H.wait_for(
        lambda: _partial(goal_workspace_root / "B", "partial-feature-34.tmp"), timeout=75), (
        f"B never reclaimed and advanced the Goal; provider output:\n{node_b.output()}")
    assert not _partial(goal_workspace_root / "B", "partial-feature-2.tmp"), (
        "A's uncommitted workspace leaked into B instead of B reconstructing from relay Git")
    rows = _tasks(relay, owner_token, project_id, conversation_id)
    assert len(rows) == 3, rows
    assert rows[1]["id"] == second_turn
    assert rows[1]["state"] == "completed" and rows[1]["attempt"] == 2
    assert rows[1]["provider_id"] == node_b.node_id
    third_turn = rows[2]["id"]
    assert rows[2]["state"] == "running" and rows[2]["attempt"] == 1
    assert rows[2]["provider_id"] == node_b.node_id
    node_b.die()

    # C again owns a new disk. Its fake app-server refuses to proceed unless the distributed Codex
    # history contains exactly A's completed turn followed by B's completed turn.
    node_c = spawn_goal_provider("C")
    complete = H.wait_for(
        lambda: _completed_goal(relay, owner_token, conversation_id), timeout=75)
    assert complete, f"C did not complete the Goal; provider output:\n{node_c.output()}"
    rows = _tasks(relay, owner_token, project_id, conversation_id)
    assert len(rows) == 3, rows
    assert rows[2]["id"] == third_turn
    assert rows[2]["state"] == "completed" and rows[2]["attempt"] == 2
    assert rows[2]["provider_id"] == node_c.node_id
    assert complete["turns_completed"] == 3

    final = rows[2]
    destination = tmp_path / "game"
    destination.mkdir()
    task_repo.checkout_result(
        destination, url=relay_client.git_remote_url(relay, project_id), token=owner_token,
        branch=final["branch"], commit=final["result_commit"], project_id=project_id)
    assert {"index.html", "game.js", "style.css", "README.md"} <= {
        path.name for path in destination.iterdir()}
    assert "addEventListener('click'" in (destination / "game.js").read_text()
    assert not (destination / "partial-feature-2.tmp").exists()
    assert not (destination / "partial-feature-34.tmp").exists()

    # This is the checkpoint C actually resumed, copied from B's side-ref by task materialization.
    histories = list((goal_workspace_root / "C").rglob("fake-history.json"))
    assert len(histories) == 1, histories
    assert json.loads(histories[0].read_text()) == [
        {"node": "A", "feature": 1},
        {"node": "B", "feature": 2},
        {"node": "C", "features": [3, 4]},
    ]


def test_three_nodes_reclaim_one_goal_codex_then_claude_then_codex(
        relay, owner_token, spawn_goal_provider, goal_workspace_root, tmp_path):
    """The production topology in miniature: no shared disk and two opaque native histories."""
    from remote import relay as relay_client
    from remote import task_repo

    project_id = relay_client.create_project(
        relay, owner_token, name="p-mixed-distributed-goal")["id"]
    H.seed_trunk(relay, owner_token, project_id)

    node_a = spawn_goal_provider(
        "A", agent_kinds="codex", scenario="mixed", disk_label="mixed-A")
    goal = relay_client.create_goal(
        relay, owner_token, project_id=project_id,
        objective="Build a small playable browser click game with four features",
        done_when="index.html, game.js, style.css and README.md exist and the Goal is complete",
        model="fake-grid-model", token_budget=10_000, tools=[],
        agents=["codex", "claude"], evals=GAME_EVALS)
    conversation_id = goal["id"]

    assert H.wait_for(
        lambda: _partial(goal_workspace_root / "mixed-A", "partial-feature-2.tmp"), timeout=20), (
        f"Codex A never reached feature 2; output:\n{node_a.output()}")
    rows = _tasks(relay, owner_token, project_id, conversation_id)
    assert [row["agent_kind"] for row in rows] == ["codex", "codex"]
    second_turn = rows[1]["id"]
    node_a.die()

    # B advertises Claude only. It must reclaim A's exact second turn, use native `/goal`, publish
    # feature 2 and its Claude transcript, then resume that same Claude session for turn 3.
    node_b = spawn_goal_provider(
        "B", agent_kinds="claude", scenario="mixed", disk_label="mixed-B")
    assert H.wait_for(
        lambda: _partial(goal_workspace_root / "mixed-B", "partial-feature-34.tmp"), timeout=75), (
        f"Claude B never reclaimed, checkpointed, and resumed; output:\n{node_b.output()}")
    assert not _partial(goal_workspace_root / "mixed-B", "partial-feature-2.tmp")
    rows = _tasks(relay, owner_token, project_id, conversation_id)
    assert len(rows) == 3, rows
    assert rows[1]["id"] == second_turn and rows[1]["attempt"] == 2
    assert rows[1]["provider_id"] == node_b.node_id and rows[1]["agent_kind"] == "claude"
    third_turn = rows[2]["id"]
    assert rows[2]["agent_kind"] == "claude" and rows[2]["state"] == "running"
    node_b.die()

    # C advertises Codex only. It resumes Codex's own A-era app-server state while receiving the
    # files and Claude transcript B published through the shared side ref.
    node_c = spawn_goal_provider(
        "C", agent_kinds="codex", scenario="mixed", disk_label="mixed-C")
    complete = H.wait_for(lambda: _completed_goal(
        relay, owner_token, conversation_id), timeout=75)
    assert complete, f"Codex C did not complete the mixed Goal; output:\n{node_c.output()}"
    rows = _tasks(relay, owner_token, project_id, conversation_id)
    assert rows[2]["id"] == third_turn and rows[2]["attempt"] == 2
    assert rows[2]["provider_id"] == node_c.node_id and rows[2]["agent_kind"] == "codex"
    assert complete["turns_completed"] == 3

    destination = tmp_path / "mixed-game"
    destination.mkdir()
    task_repo.checkout_result(
        destination, url=relay_client.git_remote_url(relay, project_id), token=owner_token,
        branch=rows[2]["branch"], commit=rows[2]["result_commit"], project_id=project_id)
    assert {"index.html", "game.js", "style.css", "README.md"} <= {
        path.name for path in destination.iterdir()}
    assert not (destination / "partial-feature-2.tmp").exists()
    assert not (destination / "partial-feature-34.tmp").exists()

    codex_histories = list((goal_workspace_root / "mixed-C").rglob("fake-history.json"))
    assert len(codex_histories) == 1
    assert json.loads(codex_histories[0].read_text()) == [
        {"node": "A", "feature": 1},
        {"node": "C", "features": [3, 4], "after": "claude-B"},
    ]
    claude_transcripts = list((goal_workspace_root / "mixed-C").rglob("*.jsonl"))
    assert claude_transcripts, "C did not fetch Claude B's opaque transcript side-ref"


def test_parent_codex_spawns_claude_child_then_codex_fans_it_in(
        relay, owner_token, spawn_goal_provider, tmp_path):
    """A real dynamic tool call creates a second distributed Goal and Git-fans it into parent."""
    from remote import relay as relay_client
    from remote import task_repo

    project_id = relay_client.create_project(
        relay, owner_token, name="p-distributed-subgoal")["id"]
    H.seed_trunk(relay, owner_token, project_id)
    node_a = spawn_goal_provider(
        "A", agent_kinds="codex", scenario="subgoal", disk_label="subgoal-A")
    parent = relay_client.create_goal(
        relay, owner_token, project_id=project_id,
        objective="Create instructions through a child Goal and verify them in the parent",
        done_when="README.md and FINAL.md exist after child fan-in",
        model="fake-grid-model", token_budget=10_000, tools=[], agents=["codex"],
        allow_subgoals=True,
        evals=[
            {"type": "file", "name": "child instructions", "path": "README.md"},
            {"type": "file", "name": "parent finish", "path": "FINAL.md"},
        ])

    waiting = H.wait_for(lambda: (lambda goal: goal if goal.get("status") == "waiting_children"
                                  else None)(relay_client.get_goal(
                                      relay, owner_token, parent["id"])), timeout=30)
    assert waiting, (
        f"parent never waited for its spawned child; "
        f"goal={relay_client.get_goal(relay, owner_token, parent['id'])!r}; "
        f"turns={_tasks(relay, owner_token, project_id, parent['id'])!r}; "
        f"A output:\n{node_a.output()}")
    assert len(waiting["children"]) == 1
    child_id = waiting["children"][0]["id"]
    parent_turns = _tasks(relay, owner_token, project_id, parent["id"])
    assert len(parent_turns) == 1
    assert parent_turns[0]["provider_id"] == node_a.node_id
    assert parent_turns[0]["agent_kind"] == "codex"
    node_a.die()

    node_b = spawn_goal_provider(
        "B", agent_kinds="claude", scenario="subgoal", disk_label="subgoal-B")
    child_done = H.wait_for(lambda: (lambda goal: goal if goal.get("status") == "complete"
                                    else None)(relay_client.get_goal(
                                        relay, owner_token, child_id)), timeout=75)
    assert child_done, f"Claude child did not complete; B output:\n{node_b.output()}"
    child_turns = _tasks(relay, owner_token, project_id, child_id)
    assert len(child_turns) == 1
    assert child_turns[0]["provider_id"] == node_b.node_id
    assert child_turns[0]["agent_kind"] == "claude"
    node_b.die()

    node_c = spawn_goal_provider(
        "C", agent_kinds="codex", scenario="subgoal", disk_label="subgoal-C")
    complete = H.wait_for(lambda: _completed_goal(
        relay, owner_token, parent["id"]), timeout=75)
    assert complete, f"Codex parent did not finish after fan-in; C output:\n{node_c.output()}"
    parent_turns = _tasks(relay, owner_token, project_id, parent["id"])
    assert len(parent_turns) == 2
    assert parent_turns[1]["provider_id"] == node_c.node_id
    assert parent_turns[1]["agent_kind"] == "codex"

    destination = tmp_path / "subgoal-result"
    destination.mkdir()
    task_repo.checkout_result(
        destination, url=relay_client.git_remote_url(relay, project_id), token=owner_token,
        branch=parent_turns[-1]["branch"], commit=parent_turns[-1]["result_commit"],
        project_id=project_id)
    assert (destination / "README.md").is_file()
    assert (destination / "FINAL.md").is_file()
    evidence = relay_client.get_goal_evidence(relay, owner_token, parent["id"])
    assert evidence["relationships"]["children"][0]["id"] == child_id
    assert all(run["passed"] for run in evidence["eval_runs"])
