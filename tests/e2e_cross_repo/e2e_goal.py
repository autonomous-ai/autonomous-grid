"""A three-node Grid Goal handoff through the real relay task and Git planes.

Run with the matching relay worktree:

    GRID_SRC_REPO=/path/to/autonomous-grid-cli uv run pytest \
      tests/e2e_cross_repo/e2e_goal.py -q
"""
from __future__ import annotations

import json
import sys
import time
from itertools import pairwise
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _harness as H

sys.path.insert(0, str(H.GRID_REPO))

GAME_EVALS = [
    {"type": "file", "name": "interactive game page", "path": "index.html",
     "min_bytes": 10, "max_bytes": 20_000,
     "contains": ['id="target"', 'script src="game.js"']},
    {"type": "file", "name": "click updates score", "path": "game.js",
     "min_bytes": 10, "max_bytes": 20_000,
     "contains": ["addEventListener('click'", "textContent"]},
    {"type": "file", "name": "visible game styles", "path": "style.css",
     "min_bytes": 10, "max_bytes": 20_000, "contains": ["background:"]},
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


def _assert_transcript_chain(evidence: dict, expected_turns: int, *, min_nodes: int = 1) -> None:
    """Every new relay turn must consume the exact checkpoint its predecessor published."""
    turns = evidence["turns"]
    assert len(turns) == expected_turns, turns
    assert turns[0]["transcript_commit"] is None, turns
    assert all(turn["transcript_result_commit"] for turn in turns), turns
    for previous, current in pairwise(turns):
        assert current["transcript_commit"] == previous["transcript_result_commit"], turns
    from cli.goal import _verify_evidence
    assert _verify_evidence(evidence, min_execution_nodes=min_nodes) == []


def test_root_goal_create_replay_returns_one_durable_identity(relay, owner_token):
    """A lost create acknowledgement cannot launch two autonomous Goal trees."""
    from remote import relay as relay_client

    project_id = relay_client.create_project(
        relay, owner_token, name="p-idempotent-goal-create")["id"]
    H.seed_trunk(relay, owner_token, project_id)
    request = {
        "project_id": project_id,
        "objective": "Create exactly one durable Goal",
        "done_when": "one.txt exists",
        "model": "fake-grid-model",
        "token_budget": 1_000,
        "idempotency_key": "e2e-root-goal-once",
    }

    first = relay_client.create_goal(relay, owner_token, **request)
    try:
        replay = relay_client.create_goal(relay, owner_token, **request)
        assert replay["id"] == first["id"]
        assert replay["turn_id"] == first["turn_id"]
        rows = _tasks(relay, owner_token, project_id, first["id"])
        assert len(rows) == 1, rows
        assert rows[0]["id"] == first["turn_id"]

        try:
            relay_client.create_goal(
                relay, owner_token, **{**request, "objective": "Create a different Goal"})
        except relay_client.TaskRefusal as exc:
            assert exc.status == 409
            assert exc.refusal_code == "goal_idempotency_key_reused"
        else:
            raise AssertionError("a changed Goal request reused an existing create key")
    finally:
        # This module intentionally shares one relay queue across scenarios. A proof Goal with no
        # provider must not remain queued for the next scenario's first provider to claim.
        relay_client.control_goal(relay, owner_token, first["id"], "cancel")


def test_same_node_reclaim_rejects_the_old_process_on_every_goal_plane(
        relay, owner_token, provider_nodes, advertise_goal_models):
    """A→A is still a handoff: a reusable node identity is not a lease generation."""
    import httpx
    from remote import relay as relay_client

    project_id = relay_client.create_project(
        relay, owner_token, name="p-goal-same-node-aba-fence")["id"]
    H.seed_trunk(relay, owner_token, project_id)
    node_id, node_token = provider_nodes["A"]
    assert advertise_goal_models("A", "fake-grid-model") == node_id
    goal = relay_client.create_goal(
        relay, owner_token, project_id=project_id,
        objective="Prove a stale process cannot mutate its replacement's Goal attempt",
        done_when="SAFE.md exists", model="fake-grid-model", token_budget=10_000,
        allow_subgoals=True,
        evals=[{"type": "file", "name": "safe handoff", "path": "SAFE.md"}])
    profile = ({"kind": "codex", "capabilities": ["native_goal", "subgoals"]},)

    try:
        old = relay_client.claim_task(
            relay, node_token, agent_kinds=("codex",), agent_profiles=profile, timeout=5)
        assert old and old["task_id"] == goal["turn_id"], old
        declined = relay_client.decline_task_claim(
            relay, node_token, old["task_id"],
            attempt=old["attempt"], claim_id=old["claim_id"])
        assert declined["state"] == "queued"
        current = relay_client.claim_task(
            relay, node_token, agent_kinds=("codex",), agent_profiles=profile, timeout=5)
        assert current and current["task_id"] == old["task_id"], current
        assert current["attempt"] == old["attempt"]
        assert current["claim_id"] != old["claim_id"]

        auth = {"Authorization": f"Bearer {node_token}"}
        stale = {**auth, "X-Grid-Task-Claim": old["claim_id"]}
        with httpx.Client(base_url=relay, timeout=10) as client:
            missing_responses = [
                client.post(f"/relay/v1/tasks/{old['task_id']}/lease", headers=auth),
                client.post(
                    f"/relay/v1/tasks/{old['task_id']}/events", headers=auth,
                    json={"events": [{"type": "task.output", "text": "unfenced"}]}),
                client.post(
                    f"/relay/v1/tasks/{old['task_id']}/retry", headers=auth,
                    json={"reason": "unfenced worker"}),
                client.post(
                    f"/relay/v1/tasks/{old['task_id']}/result", headers=auth,
                    json={"state": "failed", "output": None, "error": "unfenced"}),
            ]
            assert [response.status_code for response in missing_responses] == [403] * 4
            assert all(response.json()["detail"]["code"] == "claim_required"
                       for response in missing_responses)

            stale_responses = [
                client.post(f"/relay/v1/tasks/{old['task_id']}/lease", headers=stale),
                client.post(
                    f"/relay/v1/tasks/{old['task_id']}/events", headers=stale,
                    json={"events": [{"type": "task.output", "text": "stale"}]}),
                client.post(
                    f"/relay/v1/tasks/{old['task_id']}/retry", headers=stale,
                    json={"reason": "stale worker"}),
                client.post(
                    f"/relay/v1/tasks/{old['task_id']}/result", headers=stale,
                    json={"state": "failed", "output": None, "error": "stale"}),
                client.post(
                    "/relay/v1/responses", headers={
                        **stale, "X-Request-Id": old["task_id"],
                        "X-Grid-Conversation": goal["id"],
                    }, json={"model": "fake-grid-model", "input": "stale inference"}),
            ]
            assert [response.status_code for response in stale_responses] == [403] * 5
            assert client.get(
                f"/relay/v1/git/{project_id}/info/refs",
                headers=stale, params={"service": "git-upload-pack"}).status_code == 404

            child_url = f"/relay/v1/goals/{goal['id']}/children"
            child_headers = {
                "X-Grid-Goal-Turn": old["task_id"],
                "Idempotency-Key": "same-node-child",
            }
            child_body = {
                "objective": "Build a child proof", "done_when": "CHILD.md exists",
                "required": False, "token_budget": 1_000,
            }
            missing_child = client.post(
                child_url, headers={**auth, **child_headers}, json=child_body)
            stale_child = client.post(
                child_url, headers={**stale, **child_headers}, json=child_body)
            assert missing_child.status_code == 403
            assert stale_child.status_code == 409
            live = {**auth, "X-Grid-Task-Claim": current["claim_id"]}
            child = client.post(
                child_url, headers={**live, **child_headers}, json=child_body)
            assert child.status_code == 201, child.text
            stale_replay = client.post(
                child_url, headers={**stale, **child_headers}, json=child_body)
            assert stale_replay.status_code == 409

        # The old process altered nothing and the replacement generation still owns the lease.
        relay_client.renew_task_lease(
            relay, node_token, current["task_id"], claim_id=current["claim_id"])
        rows = _tasks(relay, owner_token, project_id, goal["id"])
        assert len(rows) == 1 and rows[0]["state"] == "running", rows
        assert rows[0]["provider_id"] == node_id
        evidence = relay_client.get_goal_evidence(relay, owner_token, goal["id"])
        assert not [item for item in evidence["attempt_events"]
                    if item["event"].get("text") in ("unfenced", "stale")]
        assert evidence["eval_runs"] == []
        assert [row["id"] for row in relay_client.get_goal(
            relay, owner_token, goal["id"])["children"]] == [child.json()["id"]]
    finally:
        relay_client.control_goal(relay, owner_token, goal["id"], "cancel")


def test_goal_waits_at_attempt_zero_until_separate_inference_node_adds_model(
        relay, owner_token, spawn_goal_provider, advertise_goal_models):
    """A live agent machine is insufficient until the Grid can serve the Goal's model."""
    from remote import relay as relay_client

    project_id = relay_client.create_project(
        relay, owner_token, name="p-goal-model-readiness")["id"]
    H.seed_trunk(relay, owner_token, project_id)
    node_a = spawn_goal_provider(
        "A", scenario="model_readiness", disk_label="model-ready-A", one_task=True)
    assert H.wait_for(lambda: "provider" in node_a.output(), timeout=5), node_a.output()

    goal = relay_client.create_goal(
        relay, owner_token, project_id=project_id,
        objective="Prove a Goal waits for Grid inference without spending an attempt",
        done_when="READY.md exists",
        model="late-grid-model", token_budget=1_000, tools=[],
        evals=[{"type": "file", "name": "readiness proof", "path": "READY.md"}])
    time.sleep(1.5)  # several real claim polls and longer than the relay's readiness-cache TTL

    waiting = _tasks(relay, owner_token, project_id, goal["id"])
    assert len(waiting) == 1, waiting
    assert waiting[0]["id"] == goal["turn_id"]
    assert waiting[0]["state"] == "queued"
    assert waiting[0]["attempt"] == 0
    assert waiting[0]["provider_id"] is None
    before = relay_client.get_goal_evidence(relay, owner_token, goal["id"])
    assert not [item for item in before["attempt_events"]
                if item["event"].get("type") in ("task.attempt_started", "task.retry")]

    # C gains only the inference role in this scenario—no task process runs there. A must claim the
    # original row, proving scheduler readiness is grid-wide rather than executor-local.
    inference_node = advertise_goal_models("C", "late-grid-model")
    complete = H.wait_for(
        lambda: _completed_goal(relay, owner_token, goal["id"]), timeout=25)
    assert complete, f"Goal did not wake after model registration; A output:\n{node_a.output()}"
    rows = _tasks(relay, owner_token, project_id, goal["id"])
    assert len(rows) == 1, rows
    assert rows[0]["id"] == goal["turn_id"]
    assert rows[0]["state"] == "completed"
    assert rows[0]["attempt"] == 1
    assert rows[0]["provider_id"] == node_a.node_id
    assert inference_node != node_a.node_id

    evidence = relay_client.get_goal_evidence(relay, owner_token, goal["id"])
    starts = [item["event"] for item in evidence["attempt_events"]
              if item["event"].get("type") == "task.attempt_started"]
    assert len(starts) == 1, starts
    assert starts[0]["attempt"] == 1 and starts[0]["provider_id"] == node_a.node_id
    assert not [item for item in evidence["attempt_events"]
                if item["event"].get("type") == "task.retry"]
    assert "could not set up lease renewal" not in node_a.output()


def test_goal_waits_at_attempt_zero_while_advertised_inference_quota_is_exhausted(
        relay, owner_token, spawn_goal_provider, advertise_goal_models):
    """A route existing in the catalog is not readiness when its seat has withdrawn service."""
    from remote import relay as relay_client

    project_id = relay_client.create_project(
        relay, owner_token, name="p-goal-quota-readiness")["id"]
    H.seed_trunk(relay, owner_token, project_id)
    node_a = spawn_goal_provider(
        "A", scenario="model_readiness", disk_label="quota-ready-A", one_task=True)
    assert H.wait_for(lambda: "provider" in node_a.output(), timeout=5), node_a.output()

    model = "quota-recovery-model"
    inference_node = advertise_goal_models("C", model, quota_serving=False)
    goal = relay_client.create_goal(
        relay, owner_token, project_id=project_id,
        objective="Prove an exhausted Grid inference seat cannot spend a Goal attempt",
        done_when="READY.md exists",
        model=model, token_budget=1_000, tools=[],
        evals=[{"type": "file", "name": "quota recovery proof", "path": "READY.md"}])
    assert relay_client.get_goal(relay, owner_token, goal["id"])["model_readiness"] == {
        "state": "waiting", "agents": [],
    }
    time.sleep(1.5)  # several task polls and longer than the readiness snapshot TTL

    waiting = _tasks(relay, owner_token, project_id, goal["id"])
    assert len(waiting) == 1, waiting
    assert waiting[0]["id"] == goal["turn_id"]
    assert waiting[0]["state"] == "queued"
    assert waiting[0]["attempt"] == 0
    assert waiting[0]["provider_id"] is None
    before = relay_client.get_goal_evidence(relay, owner_token, goal["id"])
    assert not [item for item in before["attempt_events"]
                if item["event"].get("type") in ("task.attempt_started", "task.retry")]

    # C changes only its live heartbeat allowance. The route, task node, turn id, and agent process
    # are unchanged; A must wake on the same row once Grid can actually dispatch model requests.
    assert advertise_goal_models("C", model, quota_serving=True) == inference_node
    assert H.wait_for(
        lambda: relay_client.get_goal(relay, owner_token, goal["id"])
        .get("model_readiness") == {"state": "ready", "agents": ["codex"]},
        # A relay under concurrent regression load can spend several seconds draining the
        # invalidated readiness snapshot before the provider's next long poll republishes its
        # eligible native profile. This is an eventual-dispatch assertion, not a 5-second SLO.
        timeout=15,
    ), (relay_client.get_goal(relay, owner_token, goal["id"]), node_a.output())
    complete = H.wait_for(
        lambda: _completed_goal(relay, owner_token, goal["id"]), timeout=25)
    assert complete, f"Goal did not wake after quota recovery; A output:\n{node_a.output()}"

    rows = _tasks(relay, owner_token, project_id, goal["id"])
    assert len(rows) == 1, rows
    assert rows[0]["id"] == goal["turn_id"]
    assert rows[0]["state"] == "completed"
    assert rows[0]["attempt"] == 1
    assert rows[0]["provider_id"] == node_a.node_id
    assert inference_node != node_a.node_id
    evidence = relay_client.get_goal_evidence(relay, owner_token, goal["id"])
    starts = [item["event"] for item in evidence["attempt_events"]
              if item["event"].get("type") == "task.attempt_started"]
    assert len(starts) == 1, starts
    assert starts[0]["attempt"] == 1 and starts[0]["provider_id"] == node_a.node_id
    assert not [item for item in evidence["attempt_events"]
                if item["event"].get("type") == "task.retry"]


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
    evidence = relay_client.get_goal_evidence(relay, owner_token, conversation_id)
    _assert_transcript_chain(evidence, 3, min_nodes=3)
    retries = {
        item["turn_id"]: item["event"] for item in evidence["attempt_events"]
        if item["event"].get("type") == "task.retry"
    }
    assert retries[second_turn]["previous_provider_id"] == node_a.node_id
    assert retries[third_turn]["previous_provider_id"] == node_b.node_id
    assert retries[second_turn]["previous_agent_kind"] == "codex"
    assert retries[third_turn]["previous_agent_kind"] == "codex"
    assert all(event["reason"] == "lease_expired" for event in retries.values())
    starts = {}
    for item in evidence["attempt_events"]:
        if item["event"].get("type") == "task.attempt_started":
            starts.setdefault(item["turn_id"], []).append(
                (item["event"]["attempt"], item["event"]["provider_id"],
                 item["event"]["agent_kind"]))
    # The killed process can lose its best-effort provider-authored start event. The relay-authored
    # retry above is the authority for the lost provider; these events prove the replacements also
    # announced the attempts that eventually settled.
    assert (2, node_b.node_id, "codex") in starts[second_turn]
    assert (2, node_c.node_id, "codex") in starts[third_turn]


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
    evidence = relay_client.get_goal_evidence(relay, owner_token, conversation_id)
    retries = {
        item["turn_id"]: item["event"] for item in evidence["attempt_events"]
        if item["event"].get("type") == "task.retry"
    }
    assert retries[second_turn]["previous_agent_kind"] == "codex"
    assert retries[third_turn]["previous_agent_kind"] == "claude"
    _assert_transcript_chain(evidence, 3, min_nodes=3)


def test_four_nodes_cross_harness_eval_repair_resumes_claude_after_codex(
        relay, owner_token, spawn_goal_provider, goal_workspace_root, tmp_path):
    """A -> B -> C fails independent behavior eval; D resumes B's Claude Goal and repairs it."""
    from remote import relay as relay_client
    from remote import task_repo

    project_id = relay_client.create_project(
        relay, owner_token, name="p-mixed-eval-repair")['id']
    H.seed_trunk(relay, owner_token, project_id)
    node_a = spawn_goal_provider(
        "A", agent_kinds="codex", scenario="mixed_eval_repair",
        disk_label="repair-A")
    goal = relay_client.create_goal(
        relay, owner_token, project_id=project_id,
        objective="Build a playable game and repair any independently measured defect",
        done_when="The commit-pinned interaction and presentation checks all pass",
        model="fake-grid-model", token_budget=12_000, tools=[],
        agents=["codex", "claude"], evals=GAME_EVALS)
    conversation_id = goal["id"]

    assert H.wait_for(
        lambda: _partial(goal_workspace_root / "repair-A", "partial-feature-2.tmp"),
        timeout=20), node_a.output()
    node_a.die()

    node_b = spawn_goal_provider(
        "B", agent_kinds="claude", scenario="mixed_eval_repair",
        disk_label="repair-B")
    assert H.wait_for(
        lambda: _partial(goal_workspace_root / "repair-B", "partial-feature-34.tmp"),
        timeout=75), node_b.output()
    node_b.die()

    # C resumes Codex's A-era native state, receives B's project commit, and nominates a result
    # whose JavaScript looks plausible but has no click handler. One-task mode withdraws C before
    # it can claim the repair turn itself.
    node_c = spawn_goal_provider(
        "C", agent_kinds="codex", scenario="mixed_eval_repair",
        disk_label="repair-C", one_task=True)
    assert H.wait_for(lambda: node_c.proc.poll() is not None, timeout=75), node_c.output()
    status = relay_client.get_goal(relay, owner_token, conversation_id)
    assert status["status"] == "active", status
    failed = [item for item in status["last_eval"]["results"] if not item["passed"]]
    assert [item["name"] for item in failed] == ["click updates score"]
    assert "required literal content is absent" in failed[0]["evidence"]
    rows = _tasks(relay, owner_token, project_id, conversation_id)
    assert len(rows) == 4, rows
    assert rows[2]["state"] == "completed" and rows[2]["provider_id"] == node_c.node_id
    assert rows[3]["state"] == "queued" and rows[3]["attempt"] == 0

    # D is a fourth node with a fresh disk and only Claude. It must restore B's opaque Claude
    # session across intervening Codex work, consume Grid's failed-eval handoff, and repair the tree.
    node_d = spawn_goal_provider(
        "D", agent_kinds="claude", scenario="mixed_eval_repair",
        disk_label="repair-D", one_task=True)
    complete = H.wait_for(
        lambda: _completed_goal(relay, owner_token, conversation_id), timeout=60)
    assert complete, node_d.output()
    rows = _tasks(relay, owner_token, project_id, conversation_id)
    assert len(rows) == 4 and rows[3]["state"] == "completed", rows
    assert rows[3]["provider_id"] == node_d.node_id
    assert rows[3]["agent_kind"] == "claude" and rows[3]["attempt"] == 1

    destination = tmp_path / "mixed-eval-repaired-game"
    destination.mkdir()
    task_repo.checkout_result(
        destination, url=relay_client.git_remote_url(relay, project_id), token=owner_token,
        branch=rows[3]["branch"], commit=rows[3]["result_commit"], project_id=project_id)
    assert "addEventListener('click'" in (destination / "game.js").read_text()
    assert "textContent" in (destination / "game.js").read_text()

    transcripts = list((goal_workspace_root / "repair-D").rglob("*.jsonl"))
    resolved_transcripts = {path.resolve() for path in transcripts}
    # D legitimately carries both opaque histories: Claude's transcript and Codex's rollout. Pick
    # the Claude record by its vendor shape rather than pretending mixed harnesses share one JSONL.
    claude_histories = []
    for transcript_path in resolved_transcripts:
        records = [json.loads(line) for line in transcript_path.read_text().splitlines() if line]
        if records and all("sessionId" in record and "prompt" in record for record in records):
            claude_histories.append(records)
    assert len(claude_histories) == 1 and len(resolved_transcripts) >= 2, transcripts
    transcript_records = claude_histories[0]
    assert len({record["sessionId"] for record in transcript_records}) == 1
    assert any("required literal content is absent" in record["prompt"]
               for record in transcript_records)

    evidence = relay_client.get_goal_evidence(relay, owner_token, conversation_id)
    _assert_transcript_chain(evidence, 4, min_nodes=4)
    final_turn_id = rows[3]["id"]
    nomination_turn_id = rows[2]["id"]
    accepted = [run for run in evidence["eval_runs"] if run["accepted"]]
    assert any(run["turn_id"] == nomination_turn_id and not run["passed"] for run in accepted)
    assert all(run["passed"] for run in accepted if run["turn_id"] == final_turn_id)


def test_native_codex_crash_immediately_checkpoints_same_turn_to_another_machine(
        relay, owner_token, spawn_goal_provider, goal_workspace_root, tmp_path):
    """A graceful post-spawn harness failure preserves partial files *and* the native thread."""
    from remote import relay as relay_client
    from remote import task_repo

    project_id = relay_client.create_project(
        relay, owner_token, name="p-native-crash-checkpoint")['id']
    H.seed_trunk(relay, owner_token, project_id)
    node_a = spawn_goal_provider(
        "A", scenario="graceful_crash", disk_label="crash-A", one_task=True)
    goal = relay_client.create_goal(
        relay, owner_token, project_id=project_id,
        objective="Build a crash-safe browser game",
        done_when="The four game artifacts exist after distributed native Goal recovery",
        model="fake-grid-model", token_budget=5_000, tools=[], evals=GAME_EVALS)

    # A exits cleanly only after its real task supervisor pushed both refs and the relay accepted
    # the nonterminal retry. No lease-expiry sleep or test-side database mutation is involved.
    assert H.wait_for(lambda: node_a.proc.poll() is not None, timeout=30), node_a.output()
    rows = _tasks(relay, owner_token, project_id, goal["id"])
    assert len(rows) == 1 and rows[0]["state"] == "queued", rows
    assert rows[0]["attempt"] == 1 and rows[0]["checkpoint_commit"], rows

    node_b = spawn_goal_provider(
        "B", scenario="graceful_crash", disk_label="crash-B", one_task=True)
    complete = H.wait_for(
        lambda: _completed_goal(relay, owner_token, goal["id"]), timeout=45)
    assert complete, f"node B did not resume the native Goal:\n{node_b.output()}"
    rows = _tasks(relay, owner_token, project_id, goal["id"])
    assert len(rows) == 1 and rows[0]["state"] == "completed", rows
    assert rows[0]["attempt"] == 2 and rows[0]["provider_id"] == node_b.node_id

    destination = tmp_path / "crash-checkpoint-game"
    destination.mkdir()
    task_repo.checkout_result(
        destination, url=relay_client.git_remote_url(relay, project_id), token=owner_token,
        branch=rows[0]["branch"], commit=rows[0]["result_commit"], project_id=project_id)
    assert {"PARTIAL.md", "index.html", "game.js", "style.css", "README.md"} <= {
        path.name for path in destination.iterdir()}

    histories = list((goal_workspace_root / "crash-B").rglob("fake-history.json"))
    assert len(histories) == 1, histories
    assert json.loads(histories[0].read_text()) == [
        {"node": "A", "native_thread": "partial-feature-1"},
        {"node": "B", "resumed_native_thread": True},
    ]
    evidence = relay_client.get_goal_evidence(relay, owner_token, goal["id"])
    retries = [item for item in evidence["attempt_events"]
               if item["event"].get("type") == "task.retry"]
    assert len(retries) == 1 and retries[0]["event"]["reason"] == "native_harness_failure"
    assert retries[0]["event"]["previous_provider_id"] == node_a.node_id
    assert retries[0]["event"]["previous_agent_kind"] == "codex"
    turn = evidence["turns"][0]
    assert turn["checkpoint_commit"] and turn["transcript_checkpoint_commit"]
    assert turn["transcript_result_commit"]
    from cli.goal import _verify_evidence
    assert _verify_evidence(evidence, min_execution_nodes=2) == []


def test_claude_protocol_drift_quarantines_node_and_hands_same_turn_to_codex(
        relay, owner_token, spawn_goal_provider, goal_workspace_root, tmp_path):
    """A clean exit without Claude's evaluator attachment must not consume every attempt."""
    from remote import relay as relay_client
    from remote import task_repo

    project_id = relay_client.create_project(
        relay, owner_token, name="p-claude-protocol-drift")['id']
    H.seed_trunk(relay, owner_token, project_id)
    node_a = spawn_goal_provider(
        "A", agent_kinds="claude", scenario="claude_protocol_drift",
        disk_label="claude-drift-A", task_workers=2)
    assert H.wait_for(
        lambda: "claim poll entered 2" in node_a.output(), timeout=15), node_a.output()
    goal = relay_client.create_goal(
        relay, owner_token, project_id=project_id,
        objective="Recover a partial artifact when one native Goal protocol changes",
        done_when="PARTIAL.md and DONE.md prove a cross-harness same-turn recovery",
        model="fake-grid-model", token_budget=3_000, tools=[],
        agents=["claude", "codex"],
        evals=[
            {"type": "file", "name": "accepted Claude checkpoint", "path": "PARTIAL.md",
             "max_bytes": 2_000, "contains": ["Claude A"]},
            {"type": "file", "name": "Codex recovery", "path": "DONE.md",
             "max_bytes": 2_000, "contains": ["Codex B", "same Goal turn"]},
        ])

    # A remains alive. Its child exited zero, but without the native evaluator attachment; the
    # supervisor must publish a coherent checkpoint and immediately requeue the same row. Its next
    # claim profile no longer carries native_goal, so A cannot take its own retry.
    def queued_after_drift():
        rows = _tasks(relay, owner_token, project_id, goal["id"])
        return bool(rows and rows[0]["state"] == "queued" and rows[0]["attempt"] == 1)

    assert H.wait_for(queued_after_drift, timeout=30), node_a.output()
    assert node_a.proc.poll() is None, "Claude A exited instead of remaining an ordinary task node"
    queued = _tasks(relay, owner_token, project_id, goal["id"])
    assert len(queued) == 1 and queued[0]["id"] == goal["turn_id"], queued
    # The list surface exposes the worktree checkpoint; the transcript checkpoint is intentionally
    # an evidence-only field and is asserted against the relay-authored retry below.
    assert queued[0]["checkpoint_commit"]
    assert H.wait_for(
        lambda: "declined stale Goal claim" in node_a.output(), timeout=15), node_a.output()
    after_decline = _tasks(relay, owner_token, project_id, goal["id"])
    assert len(after_decline) == 1
    assert after_decline[0]["state"] == "queued" and after_decline[0]["attempt"] == 1

    node_b = spawn_goal_provider(
        "B", agent_kinds="codex", scenario="claude_protocol_drift",
        disk_label="claude-drift-B", one_task=True)
    complete = H.wait_for(
        lambda: _completed_goal(relay, owner_token, goal["id"]), timeout=45)
    assert complete, f"Codex B did not recover Claude protocol drift:\n{node_b.output()}"
    rows = _tasks(relay, owner_token, project_id, goal["id"])
    assert len(rows) == 1 and rows[0]["id"] == goal["turn_id"], rows
    assert rows[0]["attempt"] == 2 and rows[0]["provider_id"] == node_b.node_id
    assert rows[0]["agent_kind"] == "codex"

    destination = tmp_path / "claude-protocol-drift-result"
    destination.mkdir()
    task_repo.checkout_result(
        destination, url=relay_client.git_remote_url(relay, project_id), token=owner_token,
        branch=rows[0]["branch"], commit=rows[0]["result_commit"], project_id=project_id)
    assert {"PARTIAL.md", "DONE.md"} <= {path.name for path in destination.iterdir()}

    # B's empty machine fetched both opaque harness namespaces: Claude's transcript checkpoint and
    # the Codex rollout/history it created while finishing. No direct copy from A is available.
    files = list((goal_workspace_root / "claude-drift-B").rglob("*"))
    assert any(path.name == "fake-history.json" for path in files)
    claude_records = []
    for path in files:
        if path.suffix != ".jsonl" or not path.is_file():
            continue
        try:
            records = [json.loads(line) for line in path.read_text().splitlines() if line]
        except (UnicodeDecodeError, ValueError):
            continue
        if records and all("sessionId" in row for row in records):
            claude_records.extend(records)
    assert claude_records and any(record["sessionId"] for record in claude_records)

    evidence = relay_client.get_goal_evidence(relay, owner_token, goal["id"])
    retry = next(item["event"] for item in evidence["attempt_events"]
                 if item["event"].get("type") == "task.retry")
    assert retry["reason"] == "native_harness_failure"
    assert retry["previous_provider_id"] == node_a.node_id
    assert retry["previous_agent_kind"] == "claude"
    assert retry["checkpoint_commit"] and retry["transcript_checkpoint_commit"]
    _assert_transcript_chain(evidence, 1, min_nodes=2)


def test_codex_protocol_drift_quarantines_node_and_hands_same_turn_to_claude(
        relay, owner_token, spawn_goal_provider, goal_workspace_root, tmp_path):
    """A schema-admitted Codex whose runtime method disappears must yield to Claude."""
    from remote import relay as relay_client
    from remote import task_repo

    project_id = relay_client.create_project(
        relay, owner_token, name="p-codex-protocol-drift")['id']
    H.seed_trunk(relay, owner_token, project_id)
    node_a = spawn_goal_provider(
        "A", agent_kinds="codex", scenario="codex_protocol_drift",
        disk_label="codex-drift-A", task_workers=4)
    assert H.wait_for(
        lambda: "claim poll entered 4" in node_a.output(), timeout=15), node_a.output()
    goal = relay_client.create_goal(
        relay, owner_token, project_id=project_id,
        objective="Recover a partial artifact when the Codex app-server protocol changes",
        done_when="PARTIAL.md and DONE.md prove a cross-harness same-turn recovery",
        model="fake-grid-model", token_budget=3_000, tools=[],
        agents=["codex", "claude"],
        evals=[
            {"type": "file", "name": "accepted Codex checkpoint", "path": "PARTIAL.md",
             "max_bytes": 2_000, "contains": ["Codex A"]},
            {"type": "file", "name": "Claude recovery", "path": "DONE.md",
             "max_bytes": 2_000, "contains": ["Claude B", "same Goal turn"]},
        ])

    def queued_after_drift():
        rows = _tasks(relay, owner_token, project_id, goal["id"])
        return bool(rows and rows[0]["state"] == "queued" and rows[0]["attempt"] == 1)

    assert H.wait_for(queued_after_drift, timeout=30), node_a.output()
    assert node_a.proc.poll() is None, "Codex A exited instead of remaining a Grid inference node"
    queued = _tasks(relay, owner_token, project_id, goal["id"])
    assert len(queued) == 1 and queued[0]["id"] == goal["turn_id"], queued
    assert queued[0]["checkpoint_commit"]
    # Three sibling long-polls began with Codex's pre-quarantine profile. At least one must receive
    # and decline a stale delivery before B exists, proving the real delivery/revalidation path.
    # The others may legally observe the brief gap while that sibling owns the row and return
    # empty; requiring N outstanding PULLS to receive one row confuses concurrency with broadcast.
    # The attempt assertion immediately below is the herd-wide safety property: regardless of how
    # many stale polls saw the row, none was allowed to spend attempt 2.
    assert H.wait_for(
        lambda: node_a.output().count("declined stale Goal claim") >= 1,
        timeout=15), node_a.output()
    after_decline = _tasks(relay, owner_token, project_id, goal["id"])
    assert len(after_decline) == 1
    assert after_decline[0]["state"] == "queued" and after_decline[0]["attempt"] == 1

    node_b = spawn_goal_provider(
        "B", agent_kinds="claude", scenario="codex_protocol_drift",
        disk_label="codex-drift-B", one_task=True)
    complete = H.wait_for(
        lambda: _completed_goal(relay, owner_token, goal["id"]), timeout=45)
    assert complete, f"Claude B did not recover Codex protocol drift:\n{node_b.output()}"
    rows = _tasks(relay, owner_token, project_id, goal["id"])
    assert len(rows) == 1 and rows[0]["id"] == goal["turn_id"], rows
    assert rows[0]["attempt"] == 2 and rows[0]["provider_id"] == node_b.node_id
    assert rows[0]["agent_kind"] == "claude"

    destination = tmp_path / "codex-protocol-drift-result"
    destination.mkdir()
    task_repo.checkout_result(
        destination, url=relay_client.git_remote_url(relay, project_id), token=owner_token,
        branch=rows[0]["branch"], commit=rows[0]["result_commit"], project_id=project_id)
    assert {"PARTIAL.md", "DONE.md"} <= {path.name for path in destination.iterdir()}

    # Claude B's empty machine receives Codex's opaque rollout/history from the transcript side-ref,
    # even though Claude never interprets that namespace itself.
    histories = list((goal_workspace_root / "codex-drift-B").rglob("fake-history.json"))
    assert len(histories) == 1
    assert json.loads(histories[0].read_text()) == [
        {"node": "A", "protocol": "thread-goal-get-removed"}]

    evidence = relay_client.get_goal_evidence(relay, owner_token, goal["id"])
    retry = next(item["event"] for item in evidence["attempt_events"]
                 if item["event"].get("type") == "task.retry")
    assert retry["reason"] == "native_harness_failure"
    assert retry["previous_provider_id"] == node_a.node_id
    assert retry["previous_agent_kind"] == "codex"
    assert retry["checkpoint_commit"] and retry["transcript_checkpoint_commit"]
    _assert_transcript_chain(evidence, 1, min_nodes=2)


def test_committed_business_action_survives_immediate_native_checkpoint_handoff(
        relay, owner_token, spawn_goal_provider, goal_workspace_root, business_api, tmp_path):
    """A's API write commits, its harness crashes, and B replays one accepted checkpoint safely."""
    from remote import relay as relay_client
    from remote import task_repo

    project_id = relay_client.create_project(
        relay, owner_token, name="p-graceful-action-checkpoint")['id']
    H.seed_trunk(relay, owner_token, project_id)
    origin = business_api["origin"]
    node_a = spawn_goal_provider(
        "A", scenario="graceful_business_crash", disk_label="action-crash-A",
        tool_origins=origin, one_task=True)
    goal = relay_client.create_goal(
        relay, owner_token, project_id=project_id,
        objective="Send one support reply and survive a native harness crash",
        done_when="DONE.md proves the committed action was reconciled without duplication",
        model="fake-grid-model", token_budget=4_000,
        tools=[{
            "name": "send_reply", "mode": "act", "record": "full",
            "input_schema": {"type": "object", "properties": {
                "ticket_id": {"type": "string"}, "reply": {"type": "string"}},
                "required": ["ticket_id", "reply"]},
            "http": {"method": "POST", "url": f"{origin}/tickets/reply"},
        }],
        evals=[{
            "type": "file", "name": "reconciliation proof", "path": "DONE.md",
            "max_bytes": 2_000, "contains": ["replayed", "without a duplicate side effect"],
        }])

    assert H.wait_for(lambda: node_a.proc.poll() is not None, timeout=30), node_a.output()
    rows = _tasks(relay, owner_token, project_id, goal["id"])
    assert len(rows) == 1 and rows[0]["state"] == "queued", rows
    assert rows[0]["attempt"] == 1 and rows[0]["checkpoint_commit"], rows
    assert len(business_api["write_requests"]) == 1
    assert len(business_api["side_effects"]) == 1

    node_b = spawn_goal_provider(
        "B", scenario="graceful_business_crash", disk_label="action-crash-B",
        tool_origins=origin, one_task=True)
    complete = H.wait_for(lambda: _completed_goal(
        relay, owner_token, goal["id"]), timeout=45)
    assert complete, node_b.output()
    rows = _tasks(relay, owner_token, project_id, goal["id"])
    assert len(rows) == 1 and rows[0]["attempt"] == 2, rows
    assert rows[0]["provider_id"] == node_b.node_id
    assert len(business_api["write_requests"]) == 2
    assert len({item["key"] for item in business_api["write_requests"]}) == 1
    assert len(business_api["side_effects"]) == 1

    destination = tmp_path / "graceful-action-result"
    destination.mkdir()
    task_repo.checkout_result(
        destination, url=relay_client.git_remote_url(relay, project_id), token=owner_token,
        branch=rows[0]["branch"], commit=rows[0]["result_commit"], project_id=project_id)
    assert (destination / "ACTION.md").is_file()
    assert "without a duplicate side effect" in (destination / "DONE.md").read_text()

    evidence = relay_client.get_goal_evidence(relay, owner_token, goal["id"])
    action_requests = [item for item in evidence["attempt_events"]
                       if item["event"].get("type") == "goal.act.request"]
    action_results = [item for item in evidence["attempt_events"]
                      if item["event"].get("type") == "goal.act.result"]
    assert len(action_requests) == len(action_results) == 2
    assert {
        (item["event"]["provider_node_id"], item["event"]["attempt"])
        for item in action_requests + action_results
    } == {(node_a.node_id, 1), (node_b.node_id, 2)}
    assert {item["event"]["idempotency_key"]
            for item in action_requests + action_results} == {
                business_api["write_requests"][0]["key"]}
    retry = next(item["event"] for item in evidence["attempt_events"]
                 if item["event"].get("type") == "task.retry")
    assert retry["reason"] == "native_harness_failure"
    assert retry["previous_provider_id"] == node_a.node_id
    assert retry["previous_agent_kind"] == "codex"
    assert retry["checkpoint_commit"] and retry["transcript_checkpoint_commit"]
    from cli.goal import _verify_evidence
    assert _verify_evidence(evidence, min_execution_nodes=2) == []


def test_machine_dies_between_business_commit_and_durable_result_event(
        relay, owner_token, spawn_goal_provider, business_api, tmp_path):
    """B is SIGKILLed in the narrow result window; C reconciles one side effect and the audit."""
    from remote import relay as relay_client
    from remote import task_repo

    project_id = relay_client.create_project(
        relay, owner_token, name="p-business-result-window")['id']
    H.seed_trunk(relay, owner_token, project_id)
    origin = business_api["origin"]
    business_api["pause_after_commit"] = True
    node_b = spawn_goal_provider(
        "B", scenario="business_result_window", disk_label="result-window-B",
        tool_origins=origin)
    goal = relay_client.create_goal(
        relay, owner_token, project_id=project_id,
        objective="Send one support reply despite a machine dying in the result window",
        done_when="DONE.md proves another node reconciled the committed reply",
        model="fake-grid-model", token_budget=3_000,
        tools=[{
            "name": "send_reply", "mode": "act", "record": "full",
            "input_schema": {"type": "object", "properties": {
                "ticket_id": {"type": "string"}, "reply": {"type": "string"}},
                "required": ["ticket_id", "reply"]},
            "http": {"method": "POST", "url": f"{origin}/tickets/reply"},
        }],
        evals=[{
            "type": "file", "name": "result-window reconciliation", "path": "DONE.md",
            "max_bytes": 2_000, "contains": ["replayed the stable action key"],
        }])

    assert business_api["commit_reached"].wait(timeout=30), node_b.output()
    rows = _tasks(relay, owner_token, project_id, goal["id"])
    assert len(rows) == 1 and rows[0]["state"] == "running", rows
    assert rows[0]["attempt"] == 1 and rows[0]["provider_id"] == node_b.node_id
    assert len(business_api["side_effects"]) == 1
    node_b.die()
    business_api["release_response"].set()

    node_c = spawn_goal_provider(
        "C", scenario="business_result_window", disk_label="result-window-C",
        tool_origins=origin)
    complete = H.wait_for(
        lambda: _completed_goal(relay, owner_token, goal["id"]), timeout=75)
    assert complete, node_c.output()
    rows = _tasks(relay, owner_token, project_id, goal["id"])
    assert len(rows) == 1 and rows[0]["attempt"] == 2, rows
    assert rows[0]["provider_id"] == node_c.node_id
    assert len(business_api["write_requests"]) == 2
    assert len({item["key"] for item in business_api["write_requests"]}) == 1
    assert len(business_api["side_effects"]) == 1

    destination = tmp_path / "business-result-window"
    destination.mkdir()
    task_repo.checkout_result(
        destination, url=relay_client.git_remote_url(relay, project_id), token=owner_token,
        branch=rows[0]["branch"], commit=rows[0]["result_commit"], project_id=project_id)
    assert "replayed the stable action key" in (destination / "DONE.md").read_text()

    evidence = relay_client.get_goal_evidence(relay, owner_token, goal["id"])
    requests = [item for item in evidence["attempt_events"]
                if item["event"].get("type") == "goal.act.request"]
    results = [item for item in evidence["attempt_events"]
               if item["event"].get("type") == "goal.act.result"]
    assert {(item["event"]["provider_node_id"], item["event"]["attempt"])
            for item in requests} == {(node_b.node_id, 1), (node_c.node_id, 2)}
    assert {(item["event"]["provider_node_id"], item["event"]["attempt"])
            for item in results} == {(node_c.node_id, 2)}
    assert {item["event"]["idempotency_key"] for item in requests + results} == {
        business_api["write_requests"][0]["key"]}
    _assert_transcript_chain(evidence, 1, min_nodes=2)


def test_image_goal_waits_for_a_node_with_the_required_capability(
        relay, owner_token, spawn_goal_provider, goal_workspace_root, tmp_path):
    """An online native-Goal harness is not enough: the exact capability must match."""
    from remote import relay as relay_client
    from remote import task_repo

    project_id = relay_client.create_project(
        relay, owner_token, name="p-capability-image-goal")["id"]
    H.seed_trunk(relay, owner_token, project_id)

    # B can run native Claude Goals. Even a mistaken operator declaration cannot make this runner
    # advertise image generation, because Grid wires no image tool into Claude Code. Keeping it
    # online proves the row is capability-blocked rather than merely waiting for any provider.
    node_b = spawn_goal_provider(
        "B", agent_kinds="claude", scenario="image", disk_label="image-B",
        claude_capabilities="image_generation")
    assert H.wait_for(lambda: "provider" in node_b.output(), timeout=5), node_b.output()
    goal = relay_client.create_goal(
        relay, owner_token, project_id=project_id,
        objective="Generate a PNG poster artifact",
        done_when="poster.png exists and passes the independent file check",
        model="fake-grid-model", token_budget=2_000, tools=[],
        agents=["codex", "claude"], required_capabilities=["image_generation"],
        evals=[{"type": "file", "name": "PNG poster", "path": "poster.png",
                "min_bytes": 50}])
    conversation_id = goal["id"]
    assert H.wait_for(
        lambda: len(_tasks(relay, owner_token, project_id, conversation_id)) == 1, timeout=5)
    time.sleep(1.5)  # several real claim polls while the incapable provider remains online
    waiting = _tasks(relay, owner_token, project_id, conversation_id)
    assert waiting[0]["state"] == "queued" and waiting[0]["attempt"] == 0, waiting
    assert waiting[0]["provider_id"] is None
    assert not (goal_workspace_root / "image-B").exists(), (
        "Claude materialized a capability-constrained Goal it was not allowed to claim")

    node_a = spawn_goal_provider(
        "A", agent_kinds="codex", scenario="image", disk_label="image-A",
        codex_capabilities="image_generation")
    complete = H.wait_for(
        lambda: _completed_goal(relay, owner_token, conversation_id), timeout=30)
    assert complete, f"capable Codex node did not complete the image Goal:\n{node_a.output()}"
    rows = _tasks(relay, owner_token, project_id, conversation_id)
    assert len(rows) == 1 and rows[0]["provider_id"] == node_a.node_id, rows
    assert rows[0]["agent_kind"] == "codex" and rows[0]["attempt"] == 1

    destination = tmp_path / "image-result"
    destination.mkdir()
    task_repo.checkout_result(
        destination, url=relay_client.git_remote_url(relay, project_id), token=owner_token,
        branch=rows[0]["branch"], commit=rows[0]["result_commit"], project_id=project_id)
    assert (destination / "poster.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    _assert_transcript_chain(
        relay_client.get_goal_evidence(relay, owner_token, conversation_id), 1)


def test_business_goal_matches_api_origin_and_replays_action_across_nodes(
        relay, owner_token, spawn_goal_provider, goal_workspace_root, business_api, tmp_path):
    """Read on B, lose B after a committed write, then replay exactly once on C."""
    from remote import relay as relay_client
    from remote import task_repo

    project_id = relay_client.create_project(
        relay, owner_token, name="p-business-tool-failover")['id']
    H.seed_trunk(relay, owner_token, project_id)
    origin = business_api["origin"]

    # A is a perfectly healthy Codex Goal node, but its operator did not authorize this API.
    # It must not spend an attempt or even materialize the Goal workspace.
    node_a = spawn_goal_provider(
        "A", agent_kinds="codex", scenario="business_tools", disk_label="business-A")
    assert H.wait_for(lambda: "provider" in node_a.output(), timeout=5), node_a.output()
    goal = relay_client.create_goal(
        relay, owner_token, project_id=project_id,
        objective="Resolve support ticket T-42 using the approved business API",
        done_when="DONE.md confirms the reply after an idempotent API action",
        model="fake-grid-model", token_budget=5_000,
        tools=[
            {
                "name": "read_ticket", "mode": "observe", "record": "full",
                "input_schema": {"type": "object", "properties": {
                    "ticket_id": {"type": "string"}}, "required": ["ticket_id"]},
                "http": {"method": "GET", "url": f"{origin}/tickets/read"},
            },
            {
                "name": "send_reply", "mode": "act", "record": "full",
                "input_schema": {"type": "object", "properties": {
                    "ticket_id": {"type": "string"}, "reply": {"type": "string"}},
                    "required": ["ticket_id", "reply"]},
                "http": {"method": "POST", "url": f"{origin}/tickets/reply"},
            },
        ],
        evals=[
            {"type": "file", "name": "resolution proof", "path": "DONE.md",
             "min_bytes": 200},
            {"type": "json", "name": "structured support outcome", "path": "metrics.json",
             "max_bytes": 2_000, "checks": [
                 {"pointer": "/ticket_id", "op": "equals", "value": "T-42"},
                 {"pointer": "/side_effects", "op": "equals", "value": 1},
                 {"pointer": "/audit_complete", "op": "equals", "value": True},
             ]},
        ])
    conversation_id = goal["id"]
    origin_caps = [item for item in goal["required_capabilities"]
                   if item.startswith("tool_origin.")]
    assert len(origin_caps) == 1
    time.sleep(1.5)
    waiting = _tasks(relay, owner_token, project_id, conversation_id)
    assert len(waiting) == 1 and waiting[0]["state"] == "queued", waiting
    assert waiting[0]["attempt"] == 0 and waiting[0]["provider_id"] is None
    assert not (goal_workspace_root / "business-A").exists()

    # B is authorized for the exact origin. It publishes its observation on turn 1, calls the
    # mutation on turn 2, and is killed after the API commits but before Grid accepts the turn.
    node_b = spawn_goal_provider(
        "B", agent_kinds="codex", scenario="business_tools", disk_label="business-B",
        tool_origins=origin)
    assert H.wait_for(
        lambda: _partial(goal_workspace_root / "business-B", "partial-reply.tmp"), timeout=30), (
        f"B did not reach its post-action failure point; output:\n{node_b.output()}")
    assert len(business_api["side_effects"]) == 1
    rows = _tasks(relay, owner_token, project_id, conversation_id)
    assert len(rows) == 2 and rows[0]["state"] == "completed", rows
    assert rows[0]["provider_id"] == node_b.node_id
    second_turn = rows[1]["id"]
    assert rows[1]["state"] == "running" and rows[1]["attempt"] == 1
    node_b.die()

    # C has a separate disk but the same operator-approved origin. Its retry gets the same
    # idempotency key, so the API reports a replay and performs no second side effect.
    node_c = spawn_goal_provider(
        "C", agent_kinds="codex", scenario="business_tools", disk_label="business-C",
        tool_origins=origin)
    complete = H.wait_for(
        lambda: _completed_goal(relay, owner_token, conversation_id), timeout=75)
    assert complete, f"C did not complete the business Goal; output:\n{node_c.output()}"
    rows = _tasks(relay, owner_token, project_id, conversation_id)
    assert len(rows) == 3 and rows[1]["id"] == second_turn, rows
    assert rows[1]["attempt"] == 2 and rows[1]["provider_id"] == node_c.node_id
    assert rows[2]["attempt"] == 1 and rows[2]["provider_id"] == node_c.node_id
    assert complete["turns_completed"] == 3
    assert len(business_api["write_requests"]) == 3
    assert len({item["key"] for item in business_api["write_requests"]}) == 1
    assert business_api["write_requests"][0]["body"] == business_api["write_requests"][1]["body"]
    assert len(business_api["side_effects"]) == 1

    destination = tmp_path / "business-result"
    destination.mkdir()
    task_repo.checkout_result(
        destination, url=relay_client.git_remote_url(relay, project_id), token=owner_token,
        branch=rows[-1]["branch"], commit=rows[-1]["result_commit"], project_id=project_id)
    assert json.loads((destination / "ticket.json").read_text())["ticket_id"] == "T-42"
    assert json.loads((destination / "metrics.json").read_text()) == {
        "ticket_id": "T-42", "side_effects": 1, "audit_complete": True,
    }
    assert (destination / "DONE.md").is_file()
    assert not (destination / "partial-reply.tmp").exists()

    evidence = relay_client.get_goal_evidence(relay, owner_token, conversation_id)
    _assert_transcript_chain(evidence, 3, min_nodes=2)
    request_records = [item for item in evidence["attempt_events"]
                       if item["event"].get("type") == "goal.act.request"]
    result_records = [item for item in evidence["attempt_events"]
                      if item["event"].get("type") == "goal.act.result"]
    action_requests = [item["event"] for item in request_records]
    action_results = [item["event"] for item in result_records]
    assert len(action_requests) == 3 and len(action_results) == 3
    assert all(item["success"] for item in action_results)
    expected_attribution = {
        (second_turn, node_b.node_id, 1),
        (second_turn, node_c.node_id, 2),
        (rows[2]["id"], node_c.node_id, 1),
    }
    for records in (request_records, result_records):
        assert {
            (item["turn_id"], item["event"]["provider_node_id"], item["event"]["attempt"])
            for item in records
        } == expected_attribution
    evidence_keys = {item["idempotency_key"] for item in action_requests + action_results}
    assert evidence_keys == {business_api["write_requests"][0]["key"]}
    assert any(run["passed"] is False for run in evidence["eval_runs"])
    assert len(evidence["eval_runs"]) == 4
    assert sum(run["passed"] is True for run in evidence["eval_runs"]) == 2
    assert evidence["eval_runs"][-1]["passed"] is True


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
    assert child_done["model"] == "fake-grid-child-model"
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
    relation = evidence["relationships"]["children"][0]
    assert relation["id"] == child_id
    assert relation["tokens_charged"] == child_done["tokens_used"]
    assert complete["child_tokens_reserved"] == 0
    assert complete["descendant_tokens_used"] == child_done["tokens_used"]
    assert (complete["tokens_used"]
            == complete["own_tokens_used"] + complete["descendant_tokens_used"])
    assert all(run["passed"] for run in evidence["eval_runs"])
    request = next(item["event"] for item in evidence["attempt_events"]
                   if item["event"].get("type") == "goal.act.request")
    result = next(item["event"] for item in evidence["attempt_events"]
                  if item["event"].get("type") == "goal.act.result")
    assert request["tool"] == "grid_spawn_subgoal"
    assert request["arguments"]["objective"] == "Write the child instructions"
    assert result["success"] is True
    assert result["result"]["body"]["id"] == child_id


def test_failed_optional_child_does_not_block_parent_on_another_node(
        relay, owner_token, spawn_goal_provider):
    """A failed exploratory child stays in evidence while a third node finishes the parent."""
    from remote import relay as relay_client

    project_id = relay_client.create_project(
        relay, owner_token, name="p-optional-subgoal")["id"]
    H.seed_trunk(relay, owner_token, project_id)
    node_a = spawn_goal_provider(
        "A", agent_kinds="codex", scenario="optional_subgoal", disk_label="optional-A")
    parent = relay_client.create_goal(
        relay, owner_token, project_id=project_id,
        objective="Finish a release even if an optional experiment fails",
        done_when="FINAL.md exists and the parent Goal passes its independent eval",
        model="fake-grid-model", token_budget=10_000, tools=[], agents=["codex"],
        allow_subgoals=True,
        evals=[{"type": "file", "name": "parent finish", "path": "FINAL.md"}])

    waiting = H.wait_for(lambda: (lambda goal: goal if goal.get("status") == "waiting_children"
                                  else None)(relay_client.get_goal(
                                      relay, owner_token, parent["id"])), timeout=30)
    assert waiting, f"parent never waited for optional child; A output:\n{node_a.output()}"
    assert len(waiting["children"]) == 1
    assert waiting["children"][0]["required"] is False
    child_id = waiting["children"][0]["id"]
    node_a.die()

    # This node can run the optional child but cannot steal the subgoal-capable parent continuation.
    node_b = spawn_goal_provider(
        "B", agent_kinds="codex", scenario="optional_subgoal", disk_label="optional-B",
        codex_capabilities="native_goal optional_worker")
    child_failed = H.wait_for(lambda: (lambda goal: goal if goal.get("status") == "failed"
                                       else None)(relay_client.get_goal(
                                           relay, owner_token, child_id)), timeout=45)
    assert child_failed, f"optional child did not fail as intended; B output:\n{node_b.output()}"
    child_turns = _tasks(relay, owner_token, project_id, child_id)
    assert len(child_turns) == 1
    assert child_turns[0]["provider_id"] == node_b.node_id
    node_b.die()

    node_c = spawn_goal_provider(
        "C", agent_kinds="codex", scenario="optional_subgoal", disk_label="optional-C")
    complete = H.wait_for(lambda: _completed_goal(
        relay, owner_token, parent["id"]), timeout=75)
    assert complete, f"parent did not resume after optional failure; C output:\n{node_c.output()}"
    assert complete["blocked_reason"] is None
    assert complete["children"][0]["status"] == "failed"
    assert complete["children"][0]["required"] is False
    parent_turns = _tasks(relay, owner_token, project_id, parent["id"])
    assert [row["provider_id"] for row in parent_turns] == [node_a.node_id, node_c.node_id]
    _assert_transcript_chain(
        relay_client.get_goal_evidence(relay, owner_token, parent["id"]), 2)
