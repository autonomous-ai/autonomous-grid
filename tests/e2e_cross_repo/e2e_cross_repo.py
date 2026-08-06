"""This repo's real provider and client against grid-src's real relay (ADR 0032).

Neither repository's unit suite can prove any of this, because each MOCKS the other side. grid-src's
own `*_live` tests run a real relay but hand-roll the provider's HTTP; this repo's tests run the real
provider against a fake relay. The seam between them — the one that only exists at runtime, and the
one every lockstep value is about — was tested by nothing. Both defects that reached this branch's
history were invisible to both suites and would both have surfaced here: a relay module missing from
the loader tuple (the unit suites import it directly, the live master does not), and the provider
planting its transcript symlink at an unresolved path while the agent wrote at the resolved one.

The agent here is `fake_claude.py`, which is honest about the wire and cannot be honest about the
vendor; `e2e_live_agent.py` is the other half, and it costs money.

Run:  .venv/bin/python -m pytest tests/e2e_cross_repo/e2e_cross_repo.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _harness as H  # noqa: E402

sys.path.insert(0, str(H.GRID_REPO))


def test_01_a_task_with_a_file_reaches_the_agent_and_its_result_comes_back(
        relay, owner_token, spawn_provider):
    """The whole round trip, through both repositories' real code.

    The prompt makes the agent READ the uploaded file and WRITE a new one, so what is asserted is not
    "a task completed" but "the agent saw exactly the bytes the client uploaded, and what it produced
    came back" — which is what issues 04 and 05 exist for.
    """
    spawn_provider("A")
    created = H.create(
        relay, owner_token, "READ input.txt; WRITE answer.txt PONG-4417",
        project="p-roundtrip", files=[{"path": "input.txt", "content_b64": H.b64_file("PING-4417")}])
    assert created["state"] in ("queued", "preparing"), created

    done = H.await_state(relay, owner_token, created["id"], {"completed"})

    assert "PING-4417" in (done.get("result_text") or ""), (
        f"the agent did not read the uploaded file back: {done!r}")
    assert done.get("result_commit"), "no result commit was recorded"
    assert done.get("claude_session_id"), "the session id never reached the task row"


def test_02_the_client_can_disconnect_mid_stream_and_reattach_at_its_cursor(
        relay, owner_token, spawn_provider):
    """Issue 02's headline: no gap, no duplicate, one unbroken sequence across a real disconnect."""
    from remote import relay as relay_client

    spawn_provider("A")
    task_id = H.create(
        relay, owner_token, "SAY one; SLEEP 1; SAY two; SLEEP 1; SAY three",
        project="p-reattach")["id"]

    first: list[tuple[int, dict]] = []
    for seq, payload in relay_client.stream_task_events(relay, owner_token, task_id, after_seq=-1):
        first.append((seq, payload))
        if len(first) >= 3:
            break                                  # disconnect mid-stream
    assert first, "nothing arrived on the first attachment"

    rest = list(relay_client.stream_task_events(
        relay, owner_token, task_id, after_seq=first[-1][0]))

    seqs = [seq for seq, _ in first + rest]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), (
        f"the log is not contiguous across a reattach: {seqs}")
    assert len(seqs) == len(set(seqs)), f"an event was replayed across the reattach: {seqs}"
    assert any(p.get("type") == "task.terminal" for _, p in rest), (
        "the reattached reader never saw the relay's own terminal event")


def test_03_the_workspace_tree_is_published_while_the_agent_is_still_working(
        relay, owner_token, spawn_provider):
    """Issue 08 across the wire: the file the agent creates shows up in a `task.tree` event.

    Also the only check here that would notice the beat dying quietly — a tree rides the heartbeat,
    so no tree means either the renewal loop is not beating or the snapshot is not reaching the relay,
    and both are silent everywhere else.
    """
    from remote import relay as relay_client

    spawn_provider("A")
    task_id = H.create(
        relay, owner_token, "WRITE watched.txt hello; SLEEP 4", project="p-tree")["id"]

    trees = []
    for _seq, payload in relay_client.stream_task_events(relay, owner_token, task_id, after_seq=-1):
        if payload.get("type") == "task.tree":
            trees.append(payload)
        if payload.get("type") == "task.terminal":
            break

    assert trees, "no tree snapshot ever arrived"
    assert any("watched.txt" in (t.get("paths") or []) for t in trees), (
        f"the agent's new file never appeared in the tree: {trees!r}")


def test_04_the_client_fetches_exactly_what_the_agent_produced_and_main_moved(
        relay, owner_token, spawn_provider, tmp_path):
    """Issue 05: fetch over the relay's git front with the grid token, no SSH key anywhere."""
    from remote import relay as relay_client, task_repo

    spawn_provider("A")
    task_id = H.create(
        relay, owner_token, "WRITE out/result.txt ZEBRA-4417", project="p-fetch")["id"]
    done = H.await_state(relay, owner_token, task_id, {"completed"})

    # The destination exists before `checkout_result` sees it: `git init` runs INSIDE it, and
    # `grid task fetch` does the `mkdir` itself after its own overwrite guards.
    dest = tmp_path / "fetched"
    dest.mkdir(parents=True, exist_ok=True)
    url = relay_client.git_remote_url(relay, done["project_id"])
    task_repo.checkout_result(
        dest, url=url, token=owner_token, branch=done["branch"],
        commit=done["result_commit"], project_id=done["project_id"])

    assert (dest / "out" / "result.txt").read_text() == "ZEBRA-4417"

    # `main` advanced to the result, which is what makes it the base the project's next task is cut
    # from — the property D-e exists to provide.
    assert done["result_commit"] in H.git_ls_remote(url, "refs/heads/main", bearer=owner_token)


def test_05_a_failed_task_pushes_its_branch_but_never_moves_main(
        relay, owner_token, spawn_provider):
    """D-e's asymmetry: the user can still see what the agent did, and the trunk stays known-good."""
    from remote import relay as relay_client

    spawn_provider("A")
    good = H.create(relay, owner_token, "WRITE kept.txt first", project="p-failure")["id"]
    main_before = H.await_state(relay, owner_token, good, {"completed"})["result_commit"]

    bad = H.create(
        relay, owner_token, "WRITE broken.txt half; FAIL the agent gave up",
        project="p-failure")["id"]
    failed = H.await_state(relay, owner_token, bad, {"failed"})

    assert failed.get("result_commit"), "a failed attempt must still push its branch"
    assert failed["result_commit"] != main_before

    url = relay_client.git_remote_url(relay, failed["project_id"])
    assert main_before in H.git_ls_remote(url, "refs/heads/main", bearer=owner_token), (
        "main moved on a FAILED task — it is no longer a known-good base")


def test_06_a_provider_killed_mid_task_loses_it_and_another_one_finishes_the_work(
        relay, owner_token, spawn_provider):
    """Issue 07 end to end, with a provider that really dies.

    `SIGKILL` on the provider PROCESS, not a renewer politely stopping: nothing tells the relay
    anything, the lease simply stops being renewed, and the relay's own sweep is what notices. This
    is the reason the providers here are processes rather than threads.
    """
    from remote import relay as relay_client

    doomed = spawn_provider("A")
    task_id = H.create(
        relay, owner_token, "SLEEP 90; WRITE never.txt x", project="p-reclaim")["id"]

    running = H.await_state(relay, owner_token, task_id, {"running"}, timeout=60)
    assert running["provider_id"] == doomed.node_id

    doomed.die()

    rescuer = spawn_provider("B")
    taken_over = H.wait_for(
        lambda: (lambda t: t if t.get("provider_id") == rescuer.node_id else None)(
            H.get(relay, owner_token, task_id)),
        timeout=90, interval=0.5)
    assert taken_over is not None, (
        f"the task was never handed to the second provider: "
        f"{H.get(relay, owner_token, task_id)!r}")

    events = list(relay_client.stream_task_events(relay, owner_token, task_id, after_seq=-1))
    seqs = [seq for seq, _ in events]
    assert seqs == list(range(len(seqs))), (
        f"the sequence restarted or skipped across the retry: {seqs}")
    retries = [p for _, p in events if p.get("type") == "task.retry"]
    assert retries, "the client was never told the attempt was lost and restarted"
    assert retries[0].get("reason") == "lease_expired", retries[0]


def test_07_a_member_who_is_not_a_provider_cannot_claim_a_task(relay, owner_token):
    """Issue 01's authorization, at the one place it can be checked honestly.

    Claiming discloses the prompt and grants the lease that writes the result, so it is gated on the
    node registry rather than on being any member of the grid — and the gate runs before the
    long-poll, so a refusal is immediate rather than a three-second wait.
    """
    import httpx

    H.create(relay, owner_token, "SAY nothing", project="p-authz")

    consumer = H.token("mallory", "unregistered-node")
    with httpx.Client(base_url=relay, timeout=30.0) as client:
        resp = client.post(
            "/relay/v1/tasks/claim", headers={"Authorization": f"Bearer {consumer}"})

    assert resp.status_code == 403, (
        f"an unregistered member claimed another user's task: {resp.status_code} {resp.text}")


def test_08_both_commits_of_a_task_name_the_member_who_asked_for_it(
        relay, relay_db, owner_token, spawn_provider, tmp_path):
    """ADR 0033 D-m / issue 21, at the ONE place both halves of the rule exist together.

    The relay authors the input commit; this repo's provider authors the result commit. Each
    repository's own suite proves its own half and would stay perfectly green if the two disagreed
    about the claim payload's key names — which is the whole failure this file exists to catch.

    The member row is seeded directly, because there is no API that writes one: `users` is filled by
    `grid_auth._upsert_identity` when a GRID token is verified, and this harness runs the relay with
    `GRID_MODE=false`. Seeding is what makes the assertion mean something; without it the test would
    pass against a relay that resolved nothing and committed `grid`.
    """
    import sqlite3
    import subprocess

    from remote import relay as relay_client, task_repo

    with sqlite3.connect(relay_db) as db:
        db.execute(
            "INSERT OR REPLACE INTO users (user_id, email, name, google_sub, created_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            ("alice", "alice@example.com", "Alice Nguyen", "sub-alice"))

    spawn_provider("A")
    created = H.create(relay, owner_token, "WRITE authored.txt YES-4417", project="p-author")
    done = H.await_state(relay, owner_token, created["id"], {"completed"})

    dest = tmp_path / "authored"
    dest.mkdir(parents=True, exist_ok=True)
    url = relay_client.git_remote_url(relay, done["project_id"])
    task_repo.checkout_result(
        dest, url=url, token=owner_token, branch=done["branch"],
        commit=done["result_commit"], project_id=done["project_id"])

    def idents(rev):
        """Read by git itself, in a clone the client fetched — not through either repo's code."""
        return subprocess.run(
            ["git", "log", "-1", "--format=%an|%ae|%cn|%ce", rev],
            cwd=str(dest), capture_output=True, text=True, check=True).stdout.strip()

    # The relay's half and the provider's half, asserted with one expectation — because the point
    # is that they AGREE. A committer of anything but `grid` would mean the split was lost.
    assert idents(created["input_commit"]) == "Alice Nguyen|alice@example.com|grid|grid@invalid"
    assert idents(done["result_commit"]) == "Alice Nguyen|alice@example.com|grid|grid@invalid"

    # And the thing a person actually asks: who wrote this line the agent produced.
    blamed = subprocess.run(
        ["git", "blame", "--line-porcelain", "authored.txt"],
        cwd=str(dest), capture_output=True, text=True, check=True).stdout
    assert "author Alice Nguyen" in blamed, blamed
