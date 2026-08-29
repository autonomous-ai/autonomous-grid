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
        model="fake-grid-model", token_budget=10_000, tools=[])
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
