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

import pytest

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
    if not root.exists():
        return None
    try:
        # Workspaces are disposable and replaced between attempts. A recursive scan can observe a
        # directory and then lose it before scandir reaches it; that means "not visible yet", not a
        # failed Goal. The wait loop retries against the next stable snapshot.
        return next(iter(root.rglob(name)), None)
    except FileNotFoundError:
        return None


@pytest.fixture(autouse=True)
def _cancel_goals_created_by_each_scenario(relay, owner_token):
    """A failed scenario must not feed its queued Goal to the next scenario's providers."""
    from remote import relay as relay_client

    before = {
        goal["id"] for goal in relay_client.list_goals(relay, owner_token, all=True)
    }
    yield
    for goal in relay_client.list_goals(relay, owner_token, all=False):
        if goal.get("id") not in before:
            relay_client.control_goal(relay, owner_token, goal["id"], "cancel")


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


def test_relay_timer_recovers_a_goal_abandoned_while_preparing_continuation(
        relay, relay_db, owner_token):
    """The durable post-response crash state recovers without another client request.

    Fault injection writes the shape left after a continuation row commits but before its Git
    preparation reaches ``queued``. From that point on this test uses only the real relay process:
    its periodic sweep must atomically terminate the abandoned row, wake its SSE stream, and let
    Goal reconciliation create exactly one replacement. Calling a create/commit endpoint here
    would exercise the older request-triggered cleanup and leave the actual crash backstop unproven.
    """
    import sqlite3

    from remote import relay as relay_client

    project_id = relay_client.create_project(
        relay, owner_token, name="p-goal-abandoned-continuation")['id']
    H.seed_trunk(relay, owner_token, project_id)
    goal = relay_client.create_goal(
        relay, owner_token, project_id=project_id,
        objective="Recover work after the relay dies while preparing the next Goal turn",
        done_when="RECOVERED.md exists", model="fake-grid-model", token_budget=10_000,
        evals=[{"type": "file", "name": "recovered", "path": "RECOVERED.md"}])

    try:
        # The real endpoint already proved it can insert and prepare this row. Rewind only the
        # durable fields that distinguish the crash window, age it beyond the private relay's
        # configured prepare ceiling, and make no further mutation request.
        with sqlite3.connect(relay_db, timeout=10) as db:
            updated = db.execute(
                "UPDATE turns SET state='preparing', created_at=datetime('now', '-10 minutes'), "
                "base_commit=NULL, input_commit=NULL WHERE id=? AND state='queued'",
                (goal["turn_id"],),
            ).rowcount
        assert updated == 1

        def recovered():
            rows = _tasks(relay, owner_token, project_id, goal["id"])
            return rows if (len(rows) == 2
                            and rows[0]["id"] == goal["turn_id"]
                            and rows[0]["state"] == "failed"
                            and rows[0]["error"] == "prepare_abandoned"
                            and rows[1]["state"] == "queued"
                            and rows[1]["attempt"] == 0) else None

        rows = H.wait_for(recovered, timeout=15)
        assert rows, _tasks(relay, owner_token, project_id, goal["id"])
        events = list(relay_client.stream_task_events(
            relay, owner_token, goal["turn_id"]))
        terminal = [payload for _seq, payload in events
                    if payload.get("type") == "task.terminal"]
        assert terminal == [{
            "type": "task.terminal", "state": "failed", "error": "prepare_abandoned",
        }]
        assert len(_tasks(relay, owner_token, project_id, goal["id"])) == 2
    finally:
        relay_client.control_goal(relay, owner_token, goal["id"], "cancel")


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
        # eligible native profile. This is an eventual-dispatch assertion, not a latency SLO. The
        # full cross-repo matrix has measured just over 15 seconds while the exact isolated test
        # passes, so keep enough room for two complete long-poll cycles without weakening any state
        # or attempt-count assertion.
        timeout=30,
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
    # Terminal settlement is acknowledged before post-commit continuation preparation so a slow
    # Git prepare cannot make the worker time out after its result already landed. The durable row
    # may therefore be briefly `preparing`; require eventual queue publication, not an impossible
    # same-response ordering, while still proving no provider spent the repair attempt.
    def repair_turn_is_queued():
        current = _tasks(relay, owner_token, project_id, conversation_id)
        return bool(len(current) == 4
                    and current[3]["state"] == "queued"
                    and current[3]["attempt"] == 0)

    assert H.wait_for(repair_turn_is_queued, timeout=30), _tasks(
        relay, owner_token, project_id, conversation_id)
    rows = _tasks(relay, owner_token, project_id, conversation_id)
    assert rows[2]["state"] == "completed" and rows[2]["provider_id"] == node_c.node_id

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
    repair_prompts = [record["prompt"] for record in transcript_records
                      if "required literal content is absent" in record["prompt"]]
    assert repair_prompts
    # A prose failure is not actionable enough: the physical two-machine run proved a local model
    # will guess plausible filenames and tests when the immutable contract is omitted.  Claude must
    # receive the same relay-owned path and exact literals as Codex, even after an intervening Codex
    # nomination and restoration of Claude's opaque native session on a fourth machine.
    assert any('"path": "game.js"' in prompt for prompt in repair_prompts)
    assert any("addEventListener('click'" in prompt for prompt in repair_prompts)
    assert any("textContent" in prompt for prompt in repair_prompts)

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
    # There are two correct real-network orderings. A parked pre-quarantine poll can receive the
    # requeued row and decline it, or its short E2E long-poll can expire during relay-owned Git
    # settlement and the next local poll observes that the harness is quarantined. Neither ordering
    # may start attempt 2. The decline wire path and its generation fence have dedicated public and
    # private tests; this scenario proves the end-to-end quarantine and cross-harness recovery.
    assert H.wait_for(
        lambda: ("declined stale Goal claim" in node_a.output()
                 or "task claims suspended" in node_a.output()),
        timeout=15), node_a.output()
    after_quarantine = _tasks(relay, owner_token, project_id, goal["id"])
    assert len(after_quarantine) == 1
    assert after_quarantine[0]["state"] == "queued" and after_quarantine[0]["attempt"] == 1

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
    # Three sibling long-polls began with Codex's pre-quarantine profile. A poll that is still open
    # after relay-owned Git settlement receives and declines the stale delivery. With the deliberately
    # short E2E long-poll, every old poll may instead expire first; subsequent loops then suspend
    # locally because Codex's exact executable revision is quarantined. Both are safe only if the row
    # remains queued at attempt 1. The decline endpoint and client revalidation path are independently
    # covered with exact claim-generation assertions.
    assert H.wait_for(
        lambda: ("declined stale Goal claim" in node_a.output()
                 or "task claims suspended" in node_a.output()),
        timeout=15), node_a.output()
    after_quarantine = _tasks(relay, owner_token, project_id, goal["id"])
    assert len(after_quarantine) == 1
    assert after_quarantine[0]["state"] == "queued" and after_quarantine[0]["attempt"] == 1

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


def test_child_goal_reclaims_same_turn_codex_to_claude_then_fans_in_to_codex(
        relay, owner_token, spawn_goal_provider, tmp_path):
    """Compose hierarchy, same-turn checkpoint recovery, mixed harnesses and fan-in."""
    from remote import relay as relay_client
    from remote import task_repo

    project_id = relay_client.create_project(
        relay, owner_token, name="p-subgoal-mixed-retry")['id']
    H.seed_trunk(relay, owner_token, project_id)
    node_a = spawn_goal_provider(
        "A", agent_kinds="codex", scenario="subgoal_mixed_retry",
        disk_label="child-mix-A", one_task=True)
    parent = relay_client.create_goal(
        relay, owner_token, project_id=project_id,
        objective="Delegate a child that survives a Codex-to-Claude machine handoff",
        done_when="The recovered child is evaluated, merged, and FINAL.md records the chain",
        model="fake-grid-model", token_budget=10_000, tools=[], agents=["codex"],
        allow_subgoals=True,
        evals=[
            {"type": "file", "name": "Codex child checkpoint", "path": "CHILD_PARTIAL.md",
             "max_bytes": 2_000, "contains": ["Codex B checkpoint"]},
            {"type": "file", "name": "Claude child completion", "path": "CHILD_DONE.md",
             "max_bytes": 2_000, "contains": ["Claude C", "Codex B checkpoint"]},
            {"type": "file", "name": "parent fan-in", "path": "FINAL.md",
             "max_bytes": 2_000, "contains": ["Codex D", "independently evaluated fan-in"]},
        ])

    waiting = H.wait_for(lambda: (lambda goal: goal if goal.get("status") == "waiting_children"
                                  else None)(relay_client.get_goal(
                                      relay, owner_token, parent["id"])), timeout=30)
    assert waiting, f"parent never yielded to its child; A output:\n{node_a.output()}"
    assert len(waiting["children"]) == 1
    child_id = waiting["children"][0]["id"]
    assert H.wait_for(lambda: node_a.proc.poll() is not None, timeout=15), node_a.output()

    # B starts the child on Codex and fails after writing both its native and worktree checkpoint.
    node_b = spawn_goal_provider(
        "B", agent_kinds="codex", scenario="subgoal_mixed_retry",
        disk_label="child-mix-B", one_task=True)
    assert H.wait_for(lambda: node_b.proc.poll() is not None, timeout=30), node_b.output()
    after_b = _tasks(relay, owner_token, project_id, child_id)
    assert len(after_b) == 1 and after_b[0]["state"] == "queued", after_b
    child_turn_id = after_b[0]["id"]
    assert after_b[0]["attempt"] == 1 and after_b[0]["checkpoint_commit"], after_b
    assert after_b[0]["agent_kind"] == "codex"

    # C has a separate disk and only Claude. It must reclaim B's exact row at attempt 2 and start a
    # native Claude /goal over the accepted Codex checkpoint, not create a fresh child turn.
    node_c = spawn_goal_provider(
        "C", agent_kinds="claude", scenario="subgoal_mixed_retry",
        disk_label="child-mix-C", one_task=True)
    child_done = H.wait_for(lambda: (lambda goal: goal if goal.get("status") == "complete"
                                    else None)(relay_client.get_goal(
                                        relay, owner_token, child_id)), timeout=60)
    assert child_done, f"Claude did not recover the Codex child:\n{node_c.output()}"
    child_turns = _tasks(relay, owner_token, project_id, child_id)
    assert len(child_turns) == 1 and child_turns[0]["id"] == child_turn_id, child_turns
    assert child_turns[0]["attempt"] == 2
    assert child_turns[0]["provider_id"] == node_c.node_id
    assert child_turns[0]["agent_kind"] == "claude"
    child_evidence = relay_client.get_goal_evidence(relay, owner_token, child_id)
    retry = [item for item in child_evidence["attempt_events"]
             if item["event"].get("type") == "task.retry"]
    assert len(retry) == 1
    assert retry[0]["event"]["previous_provider_id"] == node_b.node_id
    assert retry[0]["event"]["previous_agent_kind"] == "codex"
    from cli.goal import _verify_evidence
    assert _verify_evidence(child_evidence, min_execution_nodes=2) == []

    # D can run only the Codex parent. Its independent disk receives the evaluated child branch
    # through relay-owned fan-in, then it completes the second parent turn.
    node_d = spawn_goal_provider(
        "D", agent_kinds="codex", scenario="subgoal_mixed_retry",
        disk_label="child-mix-D", one_task=True)
    complete = H.wait_for(lambda: _completed_goal(
        relay, owner_token, parent["id"]), timeout=60)
    assert complete, f"Codex D did not complete the fanned-in parent:\n{node_d.output()}"
    assert complete["children"][0]["id"] == child_id
    assert complete["children"][0]["merge_state"] == "merged"
    parent_turns = _tasks(relay, owner_token, project_id, parent["id"])
    assert [row["provider_id"] for row in parent_turns] == [node_a.node_id, node_d.node_id]
    assert [row["agent_kind"] for row in parent_turns] == ["codex", "codex"]

    destination = tmp_path / "subgoal-mixed-retry-result"
    destination.mkdir()
    task_repo.checkout_result(
        destination, url=relay_client.git_remote_url(relay, project_id), token=owner_token,
        branch=parent_turns[-1]["branch"], commit=parent_turns[-1]["result_commit"],
        project_id=project_id)
    assert {"CHILD_PARTIAL.md", "CHILD_DONE.md", "FINAL.md"} <= {
        path.name for path in destination.iterdir()}
    parent_evidence = relay_client.get_goal_evidence(relay, owner_token, parent["id"])
    _assert_transcript_chain(parent_evidence, 2, min_nodes=2)
    assert all(run["passed"] and run["accepted"] for run in parent_evidence["eval_runs"])


def test_parent_fans_out_parallel_codex_and_claude_children_then_merges_both(
        relay, owner_token, spawn_goal_provider, spawn_inference_provider, tmp_path):
    """Parallel children use distinct harnesses, disks, capabilities, and Grid model providers."""
    from remote import relay as relay_client
    from remote import task_repo

    project_id = relay_client.create_project(
        relay, owner_token, name="p-subgoal-parallel-fanout")['id']
    H.seed_trunk(relay, owner_token, project_id)
    child_model_provider = spawn_inference_provider("B", "fake-grid-child-model")
    main_model_provider = spawn_inference_provider("C", "fake-grid-model")
    node_a = spawn_goal_provider(
        "A", agent_kinds="codex", scenario="subgoal_fanout",
        disk_label="fanout-A", one_task=True, advertise_models=False)
    parent = relay_client.create_goal(
        relay, owner_token, project_id=project_id,
        objective="Build two release halves concurrently with specialized child Goals",
        done_when="Both evaluated child branches are merged before FINAL.md is written",
        model="fake-grid-model", token_budget=12_000, tools=[], agents=["codex"],
        allow_subgoals=True,
        evals=[
            {"type": "file", "name": "Codex half", "path": "CODEX_CHILD.md",
             "max_bytes": 2_000, "contains": ["Codex B", "parallel child"]},
            {"type": "file", "name": "Claude half", "path": "CLAUDE_CHILD.md",
             "max_bytes": 2_000, "contains": ["Claude C", "parallel child"]},
            {"type": "file", "name": "combined release", "path": "FINAL.md",
             "max_bytes": 2_000, "contains": ["Codex D", "Codex B", "Claude C"]},
        ])

    waiting = H.wait_for(lambda: (lambda goal: goal if (
        goal.get("status") == "waiting_children" and len(goal.get("children") or []) == 2)
        else None)(relay_client.get_goal(relay, owner_token, parent["id"])), timeout=30)
    assert waiting, f"parent did not publish two children; A output:\n{node_a.output()}"
    assert H.wait_for(lambda: node_a.proc.poll() is not None, timeout=15), node_a.output()
    child_relations = {child["objective"]: child for child in waiting["children"]}
    codex_child = relay_client.get_goal(
        relay, owner_token,
        child_relations["Build the Codex half of the parallel release"]["id"])
    claude_child = relay_client.get_goal(
        relay, owner_token,
        child_relations["Build the Claude half of the parallel release"]["id"])
    assert codex_child["agents"] == ["codex"]
    assert claude_child["agents"] == ["claude"]
    assert codex_child["model"] == "fake-grid-child-model"
    assert claude_child["model"] == "fake-grid-model"

    node_b = spawn_goal_provider(
        "B", agent_kinds="codex", scenario="subgoal_fanout", disk_label="fanout-B",
        codex_capabilities="fanout_codex", one_task=True, advertise_models=False)
    node_c = spawn_goal_provider(
        "C", agent_kinds="claude", scenario="subgoal_fanout", disk_label="fanout-C",
        claude_capabilities="fanout_claude", one_task=True, advertise_models=False)

    def both_children_running():
        codex_rows = _tasks(relay, owner_token, project_id, codex_child["id"])
        claude_rows = _tasks(relay, owner_token, project_id, claude_child["id"])
        return bool(codex_rows and claude_rows
                    and codex_rows[0]["state"] == claude_rows[0]["state"] == "running")

    assert H.wait_for(both_children_running, timeout=15), (
        f"children never overlapped; B output:\n{node_b.output()}\nC output:\n{node_c.output()}")
    def completed_or_provider_failed():
        if child_model_provider.errors or main_model_provider.errors:
            return "provider_failed"
        children = tuple(relay_client.get_goal(
            relay, owner_token, child["id"]) for child in (codex_child, claude_child))
        return children if all(child["status"] == "complete" for child in children) else None

    completed_children = H.wait_for(completed_or_provider_failed, timeout=60)
    assert completed_children != "provider_failed", (
        f"inference provider failed: child={child_model_provider.errors}, "
        f"main={main_model_provider.errors}")
    assert completed_children, (
        f"parallel children did not finish; B output:\n{node_b.output()}\nC output:\n{node_c.output()}")

    codex_rows = _tasks(relay, owner_token, project_id, codex_child["id"])
    claude_rows = _tasks(relay, owner_token, project_id, claude_child["id"])
    assert len(codex_rows) == len(claude_rows) == 1
    assert codex_rows[0]["provider_id"] == node_b.node_id
    assert codex_rows[0]["agent_kind"] == "codex"
    assert claude_rows[0]["provider_id"] == node_c.node_id
    assert claude_rows[0]["agent_kind"] == "claude"
    for child in (codex_child, claude_child):
        child_evidence = relay_client.get_goal_evidence(relay, owner_token, child["id"])
        _assert_transcript_chain(child_evidence, 1)
        assert all(run["passed"] and run["accepted"]
                   for run in child_evidence["eval_runs"])

    codex_evidence = relay_client.get_goal_evidence(relay, owner_token, codex_child["id"])
    claude_evidence = relay_client.get_goal_evidence(relay, owner_token, claude_child["id"])
    assert codex_evidence["inference"] == [{
        "turn_id": codex_rows[0]["id"], "model": "fake-grid-child-model",
        "provider_node_id": child_model_provider.node_id, "state": "completed",
        "goal_attempt": 1, "goal_executor_node_id": node_b.node_id,
        "goal_agent_kind": "codex", "requests": 1, "tokens_in": 7, "tokens_out": 3,
    }]
    assert claude_evidence["inference"] == [{
        "turn_id": claude_rows[0]["id"], "model": "fake-grid-model",
        "provider_node_id": main_model_provider.node_id, "state": "completed",
        "goal_attempt": 1, "goal_executor_node_id": node_c.node_id,
        # Anthropic settlement preserves the relay's anti-underreporting input estimate (9)
        # while accepting the provider's measured output usage (3).
        "goal_agent_kind": "claude", "requests": 1, "tokens_in": 9, "tokens_out": 3,
    }]
    assert not child_model_provider.errors and not main_model_provider.errors
    observed_model_calls = H.wait_for(lambda: (
        [record for record in child_model_provider.records
         if (record.get("body") or {}).get("input")
         == "prove Codex child inference routing"],
        [record for record in main_model_provider.records
         if ((record.get("body") or {}).get("messages") or [{}])[0].get("content")
         == "prove Claude child inference routing"],
    ) if (any((record.get("body") or {}).get("input")
              == "prove Codex child inference routing"
              for record in child_model_provider.records)
          and any(((record.get("body") or {}).get("messages") or [{}])[0].get("content")
                  == "prove Claude child inference routing"
                  for record in main_model_provider.records)) else None, timeout=5)
    assert observed_model_calls, "provider poll records did not catch up with settled evidence"
    codex_model_calls, claude_model_calls = observed_model_calls
    # The provider poll wire deliberately omits Goal ids; the relay alone owns that attribution.
    # Match the unique probe body here, while the signed evidence assertions above fence it to the
    # exact turn/attempt/executor. A registration warmup cannot satisfy both sides of that proof.
    assert [record["wire_format"] for record in codex_model_calls] == ["responses"]
    assert [record["wire_format"] for record in claude_model_calls] == ["anthropic"]

    node_d = spawn_goal_provider(
        "D", agent_kinds="codex", scenario="subgoal_fanout",
        disk_label="fanout-D", one_task=True, advertise_models=False)
    complete = H.wait_for(lambda: _completed_goal(
        relay, owner_token, parent["id"]), timeout=60)
    assert complete, f"parent did not complete after parallel fan-in; D output:\n{node_d.output()}"
    assert len(complete["children"]) == 2
    assert all(child["merge_state"] == "merged" for child in complete["children"])
    assert complete["child_tokens_reserved"] == 0
    assert complete["descendant_tokens_used"] == sum(
        child["tokens_used"] for child in completed_children)

    parent_turns = _tasks(relay, owner_token, project_id, parent["id"])
    assert [row["provider_id"] for row in parent_turns] == [node_a.node_id, node_d.node_id]
    destination = tmp_path / "parallel-fanout-result"
    destination.mkdir()
    task_repo.checkout_result(
        destination, url=relay_client.git_remote_url(relay, project_id), token=owner_token,
        branch=parent_turns[-1]["branch"], commit=parent_turns[-1]["result_commit"],
        project_id=project_id)
    assert {"CODEX_CHILD.md", "CLAUDE_CHILD.md", "FINAL.md"} <= {
        path.name for path in destination.iterdir()}

    parent_evidence = relay_client.get_goal_evidence(relay, owner_token, parent["id"])
    _assert_transcript_chain(parent_evidence, 2, min_nodes=2)
    spawn_requests = [item["event"] for item in parent_evidence["attempt_events"]
                      if item["event"].get("type") == "goal.act.request"]
    spawn_results = [item["event"] for item in parent_evidence["attempt_events"]
                     if item["event"].get("type") == "goal.act.result"]
    assert len(spawn_requests) == len(spawn_results) == 2
    assert len({item["call_id"] for item in spawn_requests}) == 2
    assert len({item["idempotency_key"] for item in spawn_requests}) == 2
    assert {item["result"]["body"]["id"] for item in spawn_results} == {
        codex_child["id"], claude_child["id"]}
    assert all(run["passed"] and run["accepted"] for run in parent_evidence["eval_runs"])


def test_failed_required_child_blocks_parent_and_cancels_running_claude_sibling(
        relay, owner_token, spawn_goal_provider):
    """A failed required branch cannot orphan another agent on work no parent can consume."""
    from remote import relay as relay_client

    project_id = relay_client.create_project(
        relay, owner_token, name="p-subgoal-required-failure")['id']
    H.seed_trunk(relay, owner_token, project_id)
    node_a = spawn_goal_provider(
        "A", agent_kinds="codex", scenario="subgoal_required_failure",
        disk_label="required-fail-A", one_task=True)
    parent = relay_client.create_goal(
        relay, owner_token, project_id=project_id,
        objective="Build two required release halves without orphaning failed work",
        done_when="Both required child branches pass before release",
        model="fake-grid-model", token_budget=10_000, tools=[], agents=["codex"],
        allow_subgoals=True)

    waiting = H.wait_for(lambda: (lambda goal: goal if (
        goal.get("status") == "waiting_children" and len(goal.get("children") or []) == 2)
        else None)(relay_client.get_goal(relay, owner_token, parent["id"])), timeout=30)
    assert waiting, f"parent did not publish required children; A output:\n{node_a.output()}"
    relations = {child["objective"]: child for child in waiting["children"]}
    failed_child = relay_client.get_goal(
        relay, owner_token, relations["Build the required Codex release half"]["id"])
    slow_child = relay_client.get_goal(
        relay, owner_token, relations["Build the required Claude release half"]["id"])

    node_b = spawn_goal_provider(
        "B", agent_kinds="codex", scenario="subgoal_required_failure",
        disk_label="required-fail-B", codex_capabilities="failure_codex", one_task=True)
    node_c = spawn_goal_provider(
        "C", agent_kinds="claude", scenario="subgoal_required_failure",
        disk_label="required-fail-C", claude_capabilities="slow_claude", one_task=True)

    def both_children_running():
        failed_rows = _tasks(relay, owner_token, project_id, failed_child["id"])
        slow_rows = _tasks(relay, owner_token, project_id, slow_child["id"])
        return bool(failed_rows and slow_rows
                    and failed_rows[0]["state"] == slow_rows[0]["state"] == "running")

    assert H.wait_for(both_children_running, timeout=15), (
        f"required children never overlapped; B output:\n{node_b.output()}\nC output:\n{node_c.output()}")
    def blocked_and_settled():
        goal = relay_client.get_goal(relay, owner_token, parent["id"])
        children = {child["id"]: child for child in goal.get("children") or []}
        return goal if (
            goal.get("status") == "blocked"
            and goal.get("child_tokens_reserved") == 0
            and children.get(slow_child["id"], {}).get("status") == "cancelled"
        ) else None

    # Terminal task acknowledgement intentionally precedes post-result tree convergence. Wait for
    # both the dependency stop and budget settlement, not merely the first visible blocked status.
    blocked = H.wait_for(blocked_and_settled, timeout=30)
    assert blocked, f"parent did not block after required failure; B output:\n{node_b.output()}"
    failed_final = relay_client.get_goal(relay, owner_token, failed_child["id"])
    slow_final = relay_client.get_goal(relay, owner_token, slow_child["id"])
    assert failed_final["status"] == "failed"
    assert slow_final["status"] == "cancelled"
    assert blocked["blocked_reason"] == (
        f"Required child Goal did not complete: {failed_child['id']}=failed")
    assert blocked["child_tokens_reserved"] == 0
    assert blocked["descendant_tokens_used"] == failed_final["tokens_used"] == 40
    with pytest.raises(relay_client.TaskRefusal) as refused_resume:
        relay_client.control_goal(relay, owner_token, parent["id"], "resume")
    assert refused_resume.value.status == 409
    assert refused_resume.value.refusal_code == "goal_required_child_failed"
    assert "cannot be resumed" in str(refused_resume.value)

    failed_rows = _tasks(relay, owner_token, project_id, failed_child["id"])
    slow_rows = _tasks(relay, owner_token, project_id, slow_child["id"])
    assert len(failed_rows) == len(slow_rows) == 1
    assert failed_rows[0]["provider_id"] == node_b.node_id
    assert failed_rows[0]["state"] == "completed"
    assert slow_rows[0]["provider_id"] == node_c.node_id
    assert slow_rows[0]["state"] == "failed" and slow_rows[0]["error"] == "cancelled"
    assert H.wait_for(lambda: node_c.proc.poll() is not None, timeout=15), node_c.output()

    slow_evidence = relay_client.get_goal_evidence(relay, owner_token, slow_child["id"])
    cancelled_events = [item["event"] for item in slow_evidence["attempt_events"]
                        if item["event"].get("type") == "task.cancelled"]
    terminal_events = [item["event"] for item in slow_evidence["attempt_events"]
                       if item["event"].get("type") == "task.terminal"]
    assert len(cancelled_events) == 1
    assert terminal_events[-1] == {
        "type": "task.terminal", "state": "failed", "error": "cancelled"}
    parent_evidence = relay_client.get_goal_evidence(relay, owner_token, parent["id"])
    assert len(parent_evidence["turns"]) == 1
    assert parent_evidence["turns"][0]["transcript_commit"] is None
    assert parent_evidence["turns"][0]["transcript_result_commit"]
    from cli.goal import _verify_evidence
    assert _verify_evidence(parent_evidence) == [
        "Goal status is 'blocked', not 'complete'"]
    assert parent_evidence["relationships"]["children"] == blocked["children"]


def test_replacement_parent_reconstructs_one_child_after_spawn_failure(
        relay, owner_token, spawn_goal_provider, tmp_path):
    """A different Codex session may restate child policy, but it must replay one child identity."""
    from remote import relay as relay_client
    from remote import task_repo

    project_id = relay_client.create_project(
        relay, owner_token, name="p-subgoal-retry-dedupe")["id"]
    H.seed_trunk(relay, owner_token, project_id)
    node_a = spawn_goal_provider(
        "A", agent_kinds="codex", scenario="subgoal_retry",
        disk_label="subgoal-retry-A", one_task=True)
    parent = relay_client.create_goal(
        relay, owner_token, project_id=project_id,
        objective="Delegate one crash-safe child and merge its instructions exactly once",
        done_when="README.md and FINAL.md exist after one child fan-in",
        model="fake-grid-model", token_budget=10_000, tools=[], agents=["codex"],
        allow_subgoals=True,
        evals=[
            {"type": "file", "name": "child instructions", "path": "README.md"},
            {"type": "file", "name": "parent finish", "path": "FINAL.md"},
        ])

    assert H.wait_for(lambda: node_a.proc.poll() is not None, timeout=30), node_a.output()
    first = relay_client.get_goal(relay, owner_token, parent["id"])
    assert len(first["children"]) == 1, first
    child_id = first["children"][0]["id"]

    node_b = spawn_goal_provider(
        "B", agent_kinds="codex", scenario="subgoal_retry",
        disk_label="subgoal-retry-B", one_task=True)
    waiting = H.wait_for(lambda: (lambda goal: goal if goal.get("status") == "waiting_children"
                                  else None)(relay_client.get_goal(
                                      relay, owner_token, parent["id"])), timeout=45)
    assert waiting, f"replacement parent never yielded; B output:\n{node_b.output()}"
    assert len(waiting["children"]) == 1
    assert waiting["children"][0]["id"] == child_id
    parent_turns = _tasks(relay, owner_token, project_id, parent["id"])
    assert len(parent_turns) == 1 and parent_turns[0]["attempt"] == 2
    assert parent_turns[0]["provider_id"] == node_b.node_id

    node_c = spawn_goal_provider(
        "C", agent_kinds="claude", scenario="subgoal_retry",
        disk_label="subgoal-retry-C", one_task=True)
    child_done = H.wait_for(lambda: (lambda goal: goal if goal.get("status") == "complete"
                                    else None)(relay_client.get_goal(
                                        relay, owner_token, child_id)), timeout=75)
    assert child_done, f"Claude child did not complete; C output:\n{node_c.output()}"

    node_d = spawn_goal_provider(
        "D", agent_kinds="codex", scenario="subgoal_retry",
        disk_label="subgoal-retry-D", one_task=True)
    complete = H.wait_for(lambda: _completed_goal(
        relay, owner_token, parent["id"]), timeout=75)
    assert complete, f"parent did not finish after child fan-in; D output:\n{node_d.output()}"
    assert len(complete["children"]) == 1
    assert complete["children"][0]["merge_state"] == "merged"

    destination = tmp_path / "subgoal-retry-result"
    destination.mkdir()
    parent_turns = _tasks(relay, owner_token, project_id, parent["id"])
    task_repo.checkout_result(
        destination, url=relay_client.git_remote_url(relay, project_id), token=owner_token,
        branch=parent_turns[-1]["branch"], commit=parent_turns[-1]["result_commit"],
        project_id=project_id)
    assert (destination / "README.md").is_file()
    assert "exactly once" in (destination / "FINAL.md").read_text()

    evidence = relay_client.get_goal_evidence(relay, owner_token, parent["id"])
    requests = [item["event"] for item in evidence["attempt_events"]
                if item["event"].get("type") == "goal.act.request"]
    results = [item["event"] for item in evidence["attempt_events"]
               if item["event"].get("type") == "goal.act.result"]
    assert len(requests) == len(results) == 2
    assert {item["idempotency_key"] for item in requests + results} == {
        requests[0]["idempotency_key"]}
    assert {item["result"]["body"]["id"] for item in results} == {child_id}
    assert {item["attempt"] for item in requests + results} == {1, 2}
    assert all(run["passed"] for run in evidence["eval_runs"])


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
