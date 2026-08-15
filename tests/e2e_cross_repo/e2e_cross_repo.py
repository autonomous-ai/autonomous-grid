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

import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _harness as H  # noqa: E402

sys.path.insert(0, str(H.GRID_REPO))

from cli import remote_task  # noqa: E402


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


def test_04_the_client_fetches_what_the_agent_produced_and_the_wip_branch_moved(
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

    # The AUTHOR'S WIP BRANCH advanced to the result, which is what makes it the base their next
    # task is cut from — the property D-e exists to provide.
    #
    # ⚠️ This assertion used to name `refs/heads/main`, and ADR 0033 D-c moved it: a settle
    # fast-forwards `wip/<member_key>` and `main` is written by promote and import alone. It had
    # been failing since issue 12 and nothing noticed, because this whole directory has been red
    # since 16b took away the member-push bootstrap `H.create` was relying on.
    branch = relay_client.project_status(relay, owner_token, done["project_id"])["branch"]
    assert done["result_commit"] in H.git_ls_remote(url, f"refs/heads/{branch}",
                                                    bearer=owner_token)
    # ...and `main` did NOT move. It is still the seeded import, because nothing has promoted.
    assert done["result_commit"] not in H.git_ls_remote(url, "refs/heads/main", bearer=owner_token)


def test_05_a_failed_task_pushes_its_branch_but_never_moves_the_wip_branch(
        relay, owner_token, spawn_provider):
    """D-e's asymmetry: the user can still see what the agent did, and the base stays known-good.

    The base a task is cut from is `wip/<member_key>` since ADR 0033 D-c, so that — not `main` — is
    the ref a failure must leave alone. `main` is left alone too, by construction: nothing but a
    promote or an import writes it at all.
    """
    from remote import relay as relay_client

    spawn_provider("A")
    good = H.create(relay, owner_token, "WRITE kept.txt first", project="p-failure")["id"]
    wip_before = H.await_state(relay, owner_token, good, {"completed"})["result_commit"]

    bad = H.create(
        relay, owner_token, "WRITE broken.txt half; FAIL the agent gave up",
        project="p-failure")["id"]
    failed = H.await_state(relay, owner_token, bad, {"failed"})

    assert failed.get("result_commit"), "a failed attempt must still push its branch"
    assert failed["result_commit"] != wip_before

    url = relay_client.git_remote_url(relay, failed["project_id"])
    branch = relay_client.project_status(relay, owner_token, failed["project_id"])["branch"]
    assert wip_before in H.git_ls_remote(url, f"refs/heads/{branch}", bearer=owner_token), (
        "the WIP branch moved on a FAILED task — the next task is no longer cut from a good base")


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


def test_09_cancelling_a_task_frees_the_slot_at_once_and_really_stops_the_agent(
        relay, owner_token, spawn_provider, workspace_root):
    """ADR 0033 D-l / issue 19b, at the ONE place both halves of it exist together.

    The relay's half and the provider's half are proven separately by each repository's own suite,
    and both would stay green if they disagreed about the single string that joins them. That string
    is the refusal code `task_cancelled`, carried on a **404** — and 404 is the one answer the
    renewer deliberately refuses to kill on, because a relay too old to have the lease route sends
    an indistinguishable one. So a typo in either copy of the code does not fail anything: it just
    leaves the agent running on the operator's own subscription, for as long as the task would have
    taken, with nobody waiting for it. This is the only test that can see that.

    Both halves are asserted through behaviour rather than through the wire:

      * the SLOT, by creating a second task in the same project — which the
        `tasks_one_active_per_member` index would refuse outright if the first still held it;
      * the AGENT, by that second task actually finishing. The provider here runs ONE worker, and
        the cancelled task's agent was told to sleep for 90 seconds. If it were still running, there
        would be nothing free to claim the second task and this would time out.
    """
    from remote import relay as relay_client

    provider = spawn_provider("A")
    task_id = H.create(
        relay, owner_token, "WRITE alive-4417.txt yes; SLEEP 90; WRITE never.txt x",
        project="p-cancel")["id"]
    running = H.await_state(relay, owner_token, task_id, {"running"}, timeout=60)
    assert running["provider_id"] == provider.node_id

    # ⚠️ Waiting for `running` is NOT waiting for the agent, and getting this wrong made an earlier
    # version of this test pass against a provider that killed nothing. The relay writes `running`
    # at CLAIM; the provider then checks the repository out and only then spawns. Cancelling in that
    # window refuses the CHECKOUT instead — the task ends, the worker frees, and every relay-side
    # assertion below is satisfied without the kill ever being reached.
    #
    # The proof of life is a file the agent writes, watched on disk. Waiting for one of its EVENTS
    # was tried and is not equivalent: an event reaches the relay only when the publisher's batch
    # flushes, and when that lagged the wait returned after the agent had already finished — so the
    # cancel landed on a task with no child left, and the test failed for a reason that had nothing
    # to do with what it was testing. The file appears the moment the agent runs.
    assert H.wait_for(lambda: next(workspace_root.rglob("alive-4417.txt"), None),
                      timeout=60, interval=0.2), (
        "the agent never started, so there was nothing to stop")

    answer = relay_client.cancel_task(relay, owner_token, task_id)

    assert (answer.get("state"), answer.get("error")) == ("failed", "cancelled"), answer

    # The slot, immediately. This create is refused with `member_has_active_task` if the cancelled
    # row is still active — so it is the index itself answering, not a field we chose to read.
    second = H.create(relay, owner_token, "SAY freed", project="p-cancel")
    assert second["id"] != task_id

    # The AGENT, from the provider's own account of what it did with the refusal it was given.
    #
    # A timing observable was tried first — "one worker, so the next task cannot start until the
    # sleeping agent is gone" — and it is too loose to trust: the provider claims, checks out and
    # reports around the same window, so the same wall clock covers both outcomes often enough to
    # be flaky in BOTH directions. The provider says which branch it took, in one line, and that is
    # the branch this whole slice is about.
    assert H.wait_for(lambda: "was cancelled by a project member" in provider.output() or None,
                      timeout=30, interval=0.2), (
        "the provider did not stop the agent on a cancelled lease refusal. Its own log says which "
        f"branch it took instead:\n{provider.output()[-2000:]}")
    # ...and it must NOT have taken the ambiguous-404 path, which leaves the agent running. Both
    # sentences mention the task, so the wait above alone would pass on a provider that logged both.
    assert "The agent is left running" not in provider.output(), provider.output()[-2000:]

    events = list(relay_client.stream_task_events(relay, owner_token, task_id, after_seq=-1))
    cancelled = [payload for _, payload in events if payload.get("type") == "task.cancelled"]
    assert cancelled, f"the log does not say the task was cancelled: {events!r}"
    assert cancelled[0].get("by"), "the log does not say WHO cancelled it"
    terminal = [payload for _, payload in events if payload.get("type") == "task.terminal"]
    assert terminal and terminal[-1].get("error") == "cancelled", terminal


def test_10_a_cancelled_tasks_branch_is_still_there_to_fetch(
        relay, owner_token, spawn_provider):
    """Cancel ends the task; it does not rewind the repository (ADR 0033 issue 19b).

    A member who stops a run still wants to see how far it got, and `grid task fetch` resolves the
    branch by name. Asserted over the real git front rather than against the relay's own answer,
    because the ref is what a fetch actually needs and the fence in front of it is what decides
    whether a member can reach it.
    """
    from remote import relay as relay_client

    spawn_provider("A")
    created = H.create(
        relay, owner_token, "SLEEP 90; WRITE never.txt x", project="p-cancel-branch")
    task_id = created["id"]
    # The URL names the project by ID, never by the name `H.create` resolved — a name is not an
    # address in this design (ADR 0033 D-a), and the git front does not accept one.
    url = relay_client.git_remote_url(relay, created["project_id"])
    H.await_state(relay, owner_token, task_id, {"running"}, timeout=60)
    before = H.git_ls_remote(url, f"refs/heads/task/{task_id}", bearer=owner_token)
    assert before, "the task branch should exist while it is running"

    relay_client.cancel_task(relay, owner_token, task_id)

    after = H.git_ls_remote(url, f"refs/heads/task/{task_id}", bearer=owner_token)
    assert after == before, "cancelling rewound the task's branch"


def test_11_a_task_nobody_serves_waits_on_its_own_clock_and_says_so(
        relay_short_budgets, owner_token):
    """The queue is not the run, over the wire (ADR 0033 D-k, issue 18).

    **No provider is spawned** — that is the condition being tested, and it makes this the cheapest
    test in the directory: no agent, no git checkout, no subscription.

    Three things neither unit suite can see, because each mocks the other side:

      * the task OUTLIVES the run budget while queued. Both halves of that live in grid-src, but a
        client is what suffers when it is wrong, and the client is here.
      * following it does not answer 410. This repo's unit test has to call the endpoint as a
        FUNCTION — measured: httpx's `ASGITransport` never returns for a stream that has no end, so
        the status of a live queued task's stream is unobservable in-process. Over a real socket the
        headers arrive immediately, which makes this the only place the fix is checked as HTTP.
      * the reason on the wire is the string this repo branches on. `test_task_lease.py` proves the
        two CONSTANTS agree by parsing grid-src; only this can prove the relay actually SENDS it,
        which is the failure mode a lockstep test structurally cannot reach (see `test_09`).
    """
    import httpx

    created = H.create(relay_short_budgets, owner_token, "WRITE never.txt x", project="p-queue")
    task_id = created["id"]

    # ⚠️ FIRST, prove the clock this test's timings assume is the clock the relay is on. Every
    # sleep below is meaningless otherwise: with the production budgets a task is `queued` at five
    # seconds too, so an ignored `TASK_QUEUE_DEADLINE_SECONDS` would leave every assertion here
    # passing for the wrong reason. The window is readable on the wire — `deadline_at` at create is
    # `created_at + queue budget` — so the test can check it instead of trusting the fixture.
    window = (datetime.fromisoformat(created["deadline_at"])
              - datetime.fromisoformat(created["created_at"]))
    assert window == timedelta(seconds=H.QUEUE_BUDGET_SECONDS), (
        f"this relay is queueing on a {window} window, not the {H.QUEUE_BUDGET_SECONDS}s one the "
        f"fixture asked for — the scaled budgets are not reaching the process")

    # Past the RUN budget, and nothing has claimed it. Before this slice the task would already be
    # `timed_out` here, with `attempt = 0`, having never run.
    time.sleep(H.RUN_BUDGET_SECONDS + 2)
    waiting = H.get(relay_short_budgets, owner_token, task_id)
    assert waiting["state"] == "queued", waiting
    assert waiting["claimed_at"] is None, waiting

    # Opened and abandoned deliberately: a live queued task's stream has no end, and having one is
    # the point. `stream=True` means httpx hands back the headers without draining the body.
    with httpx.Client(timeout=10.0) as client:
        with client.stream("GET", f"{relay_short_budgets}/relay/v1/tasks/{task_id}/events",
                           headers={"Authorization": f"Bearer {owner_token}"}) as following:
            assert following.status_code == 200, (
                "a member following their own queued task was told the stream had expired while "
                f"the task record still said `queued` ({following.status_code})")

    ended = H.await_state(relay_short_budgets, owner_token, task_id, {"timed_out"}, timeout=60)
    assert ended["error"] == remote_task.QUEUE_EXPIRED, (
        f"the relay ended an unclaimed task with {ended['error']!r}, which is not the reason this "
        f"client explains as a capacity shortfall")
    assert ended["attempt"] == 0, ended


def test_12_an_empty_project_gets_a_trunk_and_a_task_runs_in_it(
        relay, owner_token, spawn_provider):
    """ADR 0033 D-o / issue 25, at the one place the WIRE can be checked rather than the constants.

    Both unit suites read a reply this repo or that one wrote down: the CLI test asserts against a
    `httpx.MockTransport` answer authored here, and the relay test asserts against its own handler.
    A relay that shaped the reply differently — a `status` of `"created"`, the oid under `oid`
    instead of `commit`, no `trunk` key — leaves both of them green and breaks `grid project init`
    for every user. That is the same class of gap this file was made for.

    It also proves the claim the whole slice rests on: a project initialized this way is not a
    special one. A real task, cut from that root by the ordinary `ensure_wip_branch`, runs and
    settles — which is the failure ADR 0033 D-c records if init had produced anything other than one
    real trunk.
    """
    from remote import relay as relay_client

    project_id = relay_client.create_project(relay, owner_token, name="p-init")["id"]
    answer = relay_client.init_project(relay, owner_token, project_id)

    # The reply keys `cli/remote_project._project_init` branches on, from the real relay.
    assert answer.get("status") == "initialized", answer
    assert answer.get("trunk") == "main", answer
    assert len(answer.get("commit") or "") == 40, answer
    # This request really wrote the ref. `False` here would mean it lost the swap to an identical
    # commit — impossible with one caller, and the one reading that says which of the two happened.
    assert answer.get("created") is True, answer

    # `/status` is a second, independent reader of the same ref — so this is the relay agreeing with
    # itself about what init did, not this test agreeing with the reply it was just handed.
    status = relay_client.project_status(relay, owner_token, project_id)
    assert status.get("main_commit") == answer["commit"], status

    # Running it twice is refused, and by the code both bootstraps share.
    import httpx
    with httpx.Client(base_url=relay, timeout=30.0) as client:
        again = client.post(f"/relay/v1/projects/{project_id}/init",
                            headers={"Authorization": f"Bearer {owner_token}"})
    assert again.status_code == 409, again.text
    assert again.json()["detail"]["code"] == "project_already_has_trunk", again.text

    # A body is refused rather than dropped — asserted on the wire because the middleware, the
    # framework and this route all get a say in what reaches `request.body()`.
    with httpx.Client(base_url=relay, timeout=30.0) as client:
        with_body = client.post(f"/relay/v1/projects/{project_id}/init",
                                json={"files": []},
                                headers={"Authorization": f"Bearer {owner_token}"})
    assert with_body.status_code == 422, with_body.text
    assert with_body.json()["detail"]["field"] == "body", with_body.text

    # And the point of all of it: the project works.
    spawn_provider()
    created = relay_client.create_task(
        relay, owner_token, prompt="SAY hello from an empty trunk", project_id=project_id)
    ended = H.await_state(relay, owner_token, created["id"], {"completed", "failed"}, timeout=120)
    assert ended["state"] == "completed", ended


def test_13_a_trunkless_project_refuses_a_task_with_a_code_and_init_project_fixes_it(
        relay, owner_token, spawn_provider):
    """ADR 0033 D-o / issue 26, at the one place the WIRE can be checked.

    `grid task create` now BRANCHES on a parsed refusal code — the second thing in this repository
    to do so, after the lease renewer — and every unit test on this side asserts against a
    `httpx.MockTransport` reply authored in this repository. A relay that nested its refusal
    differently, sent the code under another key, or stopped sending one, leaves both suites green
    and silently returns every trunkless project to the old message: the one that names import as
    the only way forward, which has been wrong since init existed.

    The second half is the flag's own claim, and it is a claim about GIT rather than about a reply:
    the files a member uploads with `--init-project` go on their WIP branch, and the trunk it
    created stays empty. `main` moving here would mean a third way to put work on `main` without a
    promote, which is what D-b forbids.
    """
    import httpx

    from cli import remote_task
    from remote import relay as relay_client

    project_id = relay_client.create_project(relay, owner_token, name="p-init-task")["id"]

    # 1. The wire fact the client's branch is keyed on, read off a real relay's real refusal.
    with httpx.Client(base_url=relay, timeout=30.0) as client:
        refused = client.post("/relay/v1/tasks",
                              json={"prompt": "x", "project_id": project_id},
                              headers={"Authorization": f"Bearer {owner_token}"})
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == remote_task._NO_TRUNK, refused.text

    # 2. ...and that this client really parses THAT body into the code it branches on. `refusal_code`
    #    answers `None` for anything it does not recognise, so a shape change would show up here as a
    #    silent degrade rather than an error — which is precisely how it would reach a user.
    try:
        relay_client.create_task(relay, owner_token, prompt="x", project_id=project_id)
    except relay_client.TaskRefusal as exc:
        assert exc.refusal_code == remote_task._NO_TRUNK, (
            f"the relay's code did not survive the parse: {exc.refusal_code!r}")
    else:
        raise AssertionError("a task was created in a project with no trunk")

    # 3. `--init-project`'s own step, through the function the CLI calls.
    remote_task._ensure_trunk(relay, owner_token, project_id, quiet=True)
    root = relay_client.project_status(relay, owner_token, project_id)["main_commit"]
    assert len(root or "") == 40, root

    # 4. Running it again is the case the flag has to tolerate, against the relay's REAL 409 — the
    #    reading that keeps Case B's suggested command re-runnable.
    remote_task._ensure_trunk(relay, owner_token, project_id, quiet=True)

    # 5. The task runs, and the uploaded file reaches the agent.
    spawn_provider()
    created = relay_client.create_task(
        relay, owner_token, prompt="READ input.txt; WRITE answer.txt PONG-2626",
        project_id=project_id,
        files=[{"path": "input.txt", "content_b64": H.b64_file("PING-2626")}])
    done = H.await_state(relay, owner_token, created["id"], {"completed", "failed"}, timeout=120)
    assert done["state"] == "completed", done
    assert "PING-2626" in (done.get("result_text") or ""), (
        f"the file uploaded alongside --init-project never reached the agent: {done!r}")

    # 6. And it landed on the member's branch, not on the trunk the flag just made.
    status = relay_client.project_status(relay, owner_token, project_id)
    assert status["main_commit"] == root, (
        f"the trunk moved without a promote: {status['main_commit']} != {root}")
    assert status["ahead"] >= 1, status
    assert status["behind"] == 0, status


def test_14_a_project_is_archived_and_unarchived_and_an_empty_one_is_deleted(
        relay, owner_token, spawn_provider):
    """ADR 0033 D-p / issue 33, at the one place the WIRE can be checked rather than the constants.

    Both unit suites read a reply the repository they live in wrote down: the CLI's asserts against
    an `httpx.MockTransport` answer authored here, and the relay's against its own handler. A relay
    that named the listing parameter differently, dropped `archived` from the view, or answered
    `changed` under another key would leave both of them green while `grid project list --all`
    silently showed nothing and `grid project archive` reported an archive that had not happened.

    It also proves the decision this slice is most likely to be "fixed" into a bug later: a task
    that is already running when the project is archived **runs to completion and settles**. That
    is a claim about the claim SELECT, the lease fence and the git front all at once, and no unit
    test on either side can make it — the CLI's has no provider and the relay's has no real agent.
    """
    import httpx

    from remote import relay as relay_client

    project_id = relay_client.create_project(relay, owner_token, name="p-archive")["id"]
    relay_client.init_project(relay, owner_token, project_id)

    # 1. A task is started and CLAIMED before the archive, so what follows is about work in flight.
    spawn_provider()
    running = relay_client.create_task(
        relay, owner_token, prompt="WRITE kept.txt SURVIVED-3333", project_id=project_id)

    # 2. Archive it out from under that task. The reply keys `cli/project_archive` branches on, from
    #    the real relay.
    archived = relay_client.archive_project(relay, owner_token, project_id)
    assert archived.get("archived") is True, archived
    assert archived.get("changed") is True, archived
    # A double-submit is a 200 saying it changed nothing, never a 409.
    assert relay_client.archive_project(relay, owner_token, project_id).get("changed") is False

    # 3. The listing hides it, and `--all` brings it back MARKED. Two calls rather than one, because
    #    a relay that ignored the parameter entirely would pass a test that only ever asked for all.
    hidden = relay_client.list_projects(relay, owner_token)["projects"]
    assert project_id not in [p["id"] for p in hidden], hidden
    shown = relay_client.list_projects(relay, owner_token, include_archived=True)["projects"]
    mine = [p for p in shown if p["id"] == project_id]
    assert mine and mine[0].get("archived") is True, shown

    # 4. A NEW task is refused, with the code and a message naming the way back.
    try:
        relay_client.create_task(relay, owner_token, prompt="nope", project_id=project_id)
    except relay_client.TaskRefusal as exc:
        assert exc.refusal_code == "project_archived", exc.refusal_code
        assert "unarchive" in str(exc), exc
    else:
        raise AssertionError("a task was created in an archived project")

    # 5. ⚠️ THE DECISION. The task claimed in step 1 finishes and settles anyway — the claim query,
    #    the lease and the leased-branch push are all untouched by archiving.
    done = H.await_state(relay, owner_token, running["id"], {"completed", "failed"}, timeout=120)
    assert done["state"] == "completed", (
        f"archiving killed a task that was already running: {done!r}")

    # 6. Unarchiving restores it fully — a task create succeeds, which is the only proof that means
    #    anything here.
    back = relay_client.unarchive_project(relay, owner_token, project_id)
    assert back.get("archived") is False, back
    after = relay_client.create_task(relay, owner_token, prompt="WRITE again.txt OK",
                                     project_id=project_id)
    assert after["id"], after

    # 7. Delete is REFUSED for this project, because it has both a trunk and tasks.
    with httpx.Client(base_url=relay, timeout=30.0) as client:
        refused = client.delete(f"/relay/v1/projects/{project_id}",
                                headers={"Authorization": f"Bearer {owner_token}"})
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "project_not_empty", refused.text
    assert "archive" in refused.json()["detail"]["message"], refused.text

    # 8. And an empty one really goes — row, membership and repository — so the id stops resolving.
    junk = relay_client.create_project(relay, owner_token, name="p-typo")["id"]
    removed = relay_client.delete_project(relay, owner_token, junk)
    assert removed.get("deleted") is True, removed
    assert removed.get("repository_removed") is True, removed
    assert junk not in [
        p["id"] for p in relay_client.list_projects(
            relay, owner_token, include_archived=True)["projects"]]
    with httpx.Client(base_url=relay, timeout=30.0) as client:
        gone = client.get(f"/relay/v1/projects/{junk}/status",
                          headers={"Authorization": f"Bearer {owner_token}"})
    assert gone.status_code == 404, gone.text
    assert gone.json()["detail"]["code"] == "no_such_project", gone.text


def test_15_the_grid_access_rule_is_not_served_outside_grid_mode(
        relay_private_domain, tmp_path):
    """ADR 0034 D-k / issue 36 — the **fail-closed** half, which is the half this harness can prove.

    D-k's premise is *authenticated on this grid ⇒ a colleague*, and only Grid mode enforces it: the
    relay takes a control-plane-signed token there and refuses public API keys outright. Outside it
    `_extract_auth` accepts any `lga_sk_` key and any locally-signed JWT, neither carrying a domain
    claim — so `GRID_MODE=false GRID_NETWORK_TYPE=private-domain` would hand every key holder every
    non-private project on the relay.

    That combination shipped in the first draft of this slice and was found in review. **It is
    exactly this harness's own configuration**, which is why the test belongs here rather than in a
    unit suite: `relay_private_domain` sets the network type, and `_harness.start_relay` sets
    `GRID_MODE=false` for every relay it starts. Nothing else in either repository puts those two
    facts in one process.

    ⚠️ **This asserts the rule is OFF, and that is a deliberate downgrade from what this test used
    to do.** It drove the positive case through real git — a colleague cloning a project nobody
    invited them to, seeing `main` and no WIP branches — and that case is now unreachable here,
    because a relay in Grid mode will not accept the tokens `_harness.token` mints. Restoring it
    means teaching the harness to sign against a local JWKS (`config.grid_token_jwks_path`), which
    is follow-up work. The positive case is covered at unit level by
    `grid-src/tests/test_project_visibility.py`, which drives `task_git._access` directly.
    """
    import subprocess

    from remote import relay as relay_client

    alice = H.token("alice", "alice-node")
    bob = H.token("bob", "bob-node")

    project_id = relay_client.create_project(relay_private_domain, alice, name="p-shared")["id"]
    relay_client.init_project(relay_private_domain, alice, project_id)

    # 1. The relay says so on the wire, which is what stops the CLI claiming a widening it did not
    #    perform. `grid_access` is the whole reason that key exists.
    listed = relay_client.list_projects(relay_private_domain, alice)["projects"]
    mine = [p for p in listed if p["id"] == project_id]
    assert mine, listed
    assert mine[0]["grid_access"] is False, (
        "a relay outside Grid mode reported that it serves projects grid-wide")
    assert mine[0]["visibility"] == "grid", (
        "the STORED setting must still read back as stored — only its effect is withheld")

    # 2. And it really is not served: bob is on this grid and sees nothing of alice's.
    assert relay_client.list_projects(relay_private_domain, bob)["projects"] == [], (
        "a colleague reached a project on a relay whose premise for letting them is not enforced")
    refused = _refusal(
        lambda: relay_client.project_status(relay_private_domain, bob, project_id))
    bogus = _refusal(lambda: relay_client.project_status(
        relay_private_domain, bob, "11111111-2222-3333-4444-555555555555"))
    assert refused == bogus, (refused, bogus)

    # 3. Through REAL GIT, over the same fence: no ref of alice's is advertised to bob at all.
    url = relay_client.git_remote_url(relay_private_domain, project_id)
    listed_refs = subprocess.run(
        ["git", "ls-remote", url, "refs/heads/*"], capture_output=True, text=True, timeout=60,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "1",
             "GIT_CONFIG_KEY_0": "http.extraHeader",
             "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {bob}"})
    assert listed_refs.returncode != 0, (
        f"a colleague cloned a project the relay does not actually serve them: "
        f"{listed_refs.stdout!r}")

    # 4. The owner is untouched by all of it — the fail-closed direction must not cost alice her own
    #    project, or "off" would be a degrade rather than the pre-36 behaviour.
    assert relay_client.project_status(
        relay_private_domain, alice, project_id)["project_id"] == project_id
    assert "refs/heads/main" in H.git_ls_remote(url, "refs/heads/*", bearer=alice)


def test_16_a_project_created_empty_runs_its_first_task_with_no_second_command(
        relay, owner_token, spawn_provider):
    """ADR 0034 D-o / issue 48, at the one place the WIRE can be checked rather than the constants.

    Both unit suites read a reply the same repository wrote down — the CLI test asserts against an
    `httpx.MockTransport` answer authored here, the relay test against its own handler — so a relay
    that nested the trunk block under another key, sent `status: "created"`, or put the oid under
    `oid`, leaves BOTH green while `grid project create --empty` refuses every real create with the
    sentence about an old relay. That failure mode is worse than the one it exists to catch, because
    the advice it prints is wrong: it would tell a member to run `grid project init` against a relay
    that had just initialized their trunk.

    ⚠️ **The negative half is the load-bearing one and cannot be checked anywhere else.** grid-src's
    `create_project` has no unknown-key refusal, so "the relay ignored `bootstrap`" and "the relay
    honoured it" are told apart ONLY by what comes back in the body. A unit test cannot see that
    distinction — it authors the body — so the assertion that a plain create carries no trunk block
    is a real check here and a tautology anywhere else.
    """
    from cli import remote_task
    from remote import relay as relay_client

    # 1. The reply keys `cli/remote_project._bootstrapped_trunk` branches on, from the real relay.
    created = relay_client.create_project(
        relay, owner_token, name="p-empty", bootstrap=relay_client.BOOTSTRAP_EMPTY)
    project_id = created["id"]
    boot = created.get("bootstrap")
    assert isinstance(boot, dict), created
    assert boot.get("status") == "initialized", boot
    assert boot.get("trunk") == "main", boot
    assert len(boot.get("commit") or "") == 40, boot
    assert boot.get("created") is True, boot

    # 2. `/status` is a second, independent reader of the same ref — the relay agreeing with itself
    #    about what it did, rather than this test agreeing with the reply it was handed.
    status = relay_client.project_status(relay, owner_token, project_id)
    assert status.get("main_commit") == boot["commit"], status

    # 3. Asking again is the postcondition holding, not a collision — on the wire, where the 409
    #    `init` sends for the same state would be indistinguishable at unit level from a 200 the
    #    client happened to be handed.
    again = relay_client.create_project(
        relay, owner_token, name="p-empty", bootstrap=relay_client.BOOTSTRAP_EMPTY)
    assert again["id"] == project_id, again
    assert again["bootstrap"]["created"] is False, again
    assert again["bootstrap"]["commit"] == boot["commit"], again

    # 4. The negative control. A plain create says nothing about a trunk and has none — which is
    #    what makes the client's postcondition check able to tell an old relay from a new one.
    plain = relay_client.create_project(relay, owner_token, name="p-plain")
    assert "bootstrap" not in plain, plain
    # Keyed on the CODE rather than on the sentence: the words are the relay's and may be reworded,
    # and "no" appears in most English. `_NO_TRUNK` is what `grid task create` itself branches on.
    try:
        relay_client.create_task(relay, owner_token, prompt="x", project_id=plain["id"])
    except relay_client.TaskRefusal as exc:
        assert exc.refusal_code == remote_task._NO_TRUNK, exc.refusal_code
    else:
        raise AssertionError("a task was created in a project that was never bootstrapped")

    # 5. And the point of all of it: the project works, first time, with nothing run in between.
    spawn_provider()
    task = relay_client.create_task(
        relay, owner_token, prompt="SAY hello from a project that was born ready",
        project_id=project_id)
    ended = H.await_state(relay, owner_token, task["id"], {"completed", "failed"}, timeout=120)
    assert ended["state"] == "completed", ended


def test_17_a_second_conversation_of_one_member_starts_fresh_on_one_provider(
        relay, owner_token, spawn_provider, workspace_root):
    """ADR 0034 D-c / issue 38, at the one seam where both repositories are on the wire together.

    `conversation_id` is a lockstep value with the fail-CLOSED direction: `remote/tasks.run_task`
    refuses a claim without it, terminally. Every unit test on the provider side reads a job dict
    this repository wrote down, and every unit test on the relay side reads a payload grid-src wrote
    down — so a relay that renamed the key, nested it, or stopped sending it leaves BOTH suites
    green while every task on every provider fails. Only a real claim over the wire can tell.

    The second half is the behaviour a person sees: two conversations in one project, on ONE
    provider process and for ONE member, and the second is not continuing the first's session.
    `fake_claude.py` refuses a workspace that is not conversation-keyed, so a dropped segment breaks
    this test at the agent as well as at the assertions.

    ⚠️ **Separate CONVERSATION, not separate repository** — and the distinction is asserted in both
    directions here because the first draft of this test got it backwards. The turns of one project
    share its git history: the second turn is cut from the member's WIP branch, which the first turn
    fast-forwarded, so it legitimately sees the first conversation's files. What must not be shared
    is the Claude Code session, and that is what the workspace path decides.

    ⚠️ What is NOT here, and is not an omission: a SECOND TURN of one conversation resuming its own
    session. Every `POST /tasks` mints a conversation and the route that posts a turn into an
    existing one is issue 47, so until it ships nothing can resume anything. The paid
    `e2e_live_agent.py` carries the half that needs the real binary.
    """
    from remote import relay as relay_client

    spawn_provider("A")
    project = relay_client.create_project(
        relay, owner_token, name="p-two-conversations",
        bootstrap=relay_client.BOOTSTRAP_EMPTY)["id"]

    first = relay_client.create_task(
        relay, owner_token, project_id=project,
        prompt="WRITE only-in-the-first.txt ZEBRA-4417; SAY remembered")
    first_done = H.await_state(
        relay, owner_token, first["id"], {"completed", "failed"}, timeout=120)
    assert first_done["state"] == "completed", first_done

    second = relay_client.create_task(
        relay, owner_token, project_id=project,
        prompt="READ only-in-the-first.txt; SAY done")
    second_done = H.await_state(
        relay, owner_token, second["id"], {"completed", "failed"}, timeout=120)

    # 1. Both ran. A relay that stopped sending `conversation_id` fails here first, and loudly —
    #    which is the whole reason the provider refuses rather than falling back.
    assert second_done["state"] == "completed", (
        f"the second conversation did not complete — if its error names conversation_id, the "
        f"relay is not sending the key this provider refuses to run without: {second_done}")

    # 2. The FILES are shared and that is correct — the second turn is cut from the member's WIP
    #    branch, which the first turn fast-forwarded, so its workspace is materialized from a commit
    #    holding the first conversation's work. Pinned rather than assumed, because "a second
    #    conversation starts fresh" is easy to read as "it starts from an empty project", and an
    #    edit that made that true would break every follow-up in a real team's repository.
    assert "ZEBRA-4417" in (second_done.get("result_text") or ""), (
        f"the second conversation could not see the project's own files — a conversation is a "
        f"separate SESSION, not a separate repository: {second_done}")

    # 3. The SESSION is not shared, which is the whole slice. `fake_claude.py` echoes back the id it
    #    was told to `--resume`, so two conversations handed one session id would report one here —
    #    which is exactly what the deleted "most recent turn of this (project, owner)" lookup did.
    assert first_done.get("claude_session_id") and second_done.get("claude_session_id")
    assert first_done["claude_session_id"] != second_done["claude_session_id"], (
        "both conversations reported one session id — the relay is still resolving the resume "
        "target per (project, owner) rather than per conversation, so the second conversation is "
        "continuing the first one's Claude Code session")
    assert not second_done.get("session_reset_reason"), (
        f"the second conversation was told to resume a session it could not use — a conversation "
        f"with no predecessor should be asked to resume nothing at all: {second_done}")

    # 4. On DISK, keyed by the conversation. Discovered rather than derived: the ids are the relay's
    #    and recomputing them here would make this test agree with a rule this repository does not
    #    own (`_harness.sweep_transcript_links` reads symlink targets for the same reason).
    members = sorted((workspace_root / "A" / "projects" / project).iterdir())
    assert len(members) == 1, f"expected one member's directory, found {members!r}"
    conversations = sorted(p for p in members[0].iterdir() if (p / "workspace").is_dir())
    assert len(conversations) == 2, (
        f"expected one workspace per conversation under {members[0]}, found {conversations!r}")


def test_18_a_conversations_transcript_leaves_the_projects_history(
        relay, relay_home, owner_token, spawn_provider, workspace_root):
    """ADR 0034 D-j / issue 39, at the one seam where both repositories are on the wire together.

    Three things can only be checked here, and each of them leaves BOTH unit suites green when it
    breaks:

      * **the fence grants the ref**. The provider builds `refs/grid/agent/<conversation_id>` from
        its own copy of `TRANSCRIPT_PREFIX` and the relay builds the same string to put in
        `push_refs`. No wire value carries the name, so the AST lockstep test catches the two
        constants DISAGREEING — it cannot catch them agreeing while the fence computes the ref from
        the wrong column, or narrows it to the wrong lease. Here the push either lands or 403s.
      * **the trunk really is clean.** A provider-side test can only assert what its own
        `commit_and_push` staged; this asserts what is in `main` on the relay after a real settle
        and a real auto-apply.
      * **`transcript_commit` survives the round trip.** Both suites read a payload their own
        repository wrote down. Only a real claim proves the relay sends the key and the provider
        reads the same one.

    ⚠️ What is NOT here, and is not an omission: a second TURN of one conversation fetching the ref
    back. Every `POST /tasks` mints a conversation and issue 47 owns the route that posts into an
    existing one, so no turn on this wire has a pin to fetch yet. The fetch-and-resume half is
    covered on the provider side, where a job dict IS the claim payload, and by the paid
    `e2e_live_agent.py` against the real binary.
    """
    import subprocess

    from remote import relay as relay_client

    spawn_provider("A")
    project = relay_client.create_project(
        relay, owner_token, name="p-transcript-side-ref",
        bootstrap=relay_client.BOOTSTRAP_EMPTY)["id"]

    created = relay_client.create_task(
        relay, owner_token, project_id=project,
        prompt="WRITE kept.txt HELLO-8812; SAY done")
    done = H.await_state(relay, owner_token, created["id"], {"completed", "failed"}, timeout=120)
    assert done["state"] == "completed", done

    url = relay_client.git_remote_url(relay, project)
    conversation = _one_conversation_dir(workspace_root, project)

    # 1. The ref reached the relay's REPOSITORY, read off disk rather than over the wire.
    #
    #    ⚠️ It cannot be checked with `ls-remote` as the member, and finding that out is what this
    #    test is for: the fence un-hides this namespace only to the caller holding that
    #    conversation's LEASE, and by now the turn is over, so `alice` is correctly shown nothing.
    #    The first draft asserted `ls-remote` was non-empty and failed against a working
    #    implementation. On disk is the only place "the push landed" and "this caller may see it"
    #    are separate questions.
    repo = relay_home / "projects" / f"{project}.git"
    tip = subprocess.run(
        ["git", "--git-dir", str(repo), "rev-parse", "--verify", "--quiet",
         f"refs/grid/agent/{conversation}^{{commit}}"],
        capture_output=True, text=True).stdout.strip()
    assert tip, (
        f"the conversation's transcript never reached the relay — the git fence refused the push, "
        f"or the two repositories disagree about the ref prefix. Conversation: {conversation}")
    published = subprocess.run(
        ["git", "--git-dir", str(repo), "ls-tree", "-r", "--name-only", tip],
        capture_output=True, text=True).stdout
    assert ".jsonl" in published, (
        f"the transcript ref exists but carries no session file: {published!r}")

    # 2. And the MEMBER is not offered it over the wire, which is the privacy half ADR 0033 listed
    #    as permanently out of scope. Asserted through the real fence, with the ref provably there.
    assert not H.git_ls_remote(url, f"refs/grid/agent/{conversation}", bearer=owner_token), (
        "a member's clone is offered a conversation's transcript ref")

    # 3. The member's own branch carries the work and NOT the conversation.
    #
    #    ⚠️ `wip/<member_key>`, not `main`. A settle fast-forwards the member's WIP branch and `main`
    #    is written by promote and import alone (ADR 0033 D-c) — the auto-apply that moves the trunk
    #    on every successful turn is a LATER slice of ADR 0034. The first draft asserted against
    #    `main` and read an empty tree. The WIP branch is the right target anyway: it is what the
    #    member's next turn is cut from, and it is exactly where the transcript used to accumulate.
    branch = relay_client.project_status(relay, owner_token, project)["branch"]
    listing = subprocess.run(
        ["git", "--git-dir", str(repo), "ls-tree", "-r", "--name-only", f"refs/heads/{branch}"],
        capture_output=True, text=True).stdout
    assert "kept.txt" in listing, f"the agent's work is not on the member's branch: {listing!r}"
    assert ".grid" not in listing, (
        f"the member's branch still carries the reserved directory, so every turn adds a transcript "
        f"to what the next one is cut from: {listing!r}")


def _one_conversation_dir(workspace_root, project):
    """The single conversation id under this project on provider A, DISCOVERED not derived.

    Recomputing it here would make this test agree with a rule this repository does not own — the
    ids are the relay's. Same reasoning as `_harness.sweep_transcript_links`.
    """
    members = sorted((workspace_root / "A" / "projects" / project).iterdir())
    assert len(members) == 1, f"expected one member's directory, found {members!r}"
    conversations = sorted(p.name for p in members[0].iterdir() if (p / "workspace").is_dir())
    assert len(conversations) == 1, f"expected one conversation, found {conversations!r}"
    return conversations[0]


def _refusal(call):
    """What a refused call SAYS, as a comparable value. Anything else is a test failure."""
    try:
        call()
    except SystemExit as exc:
        return str(exc)
    raise AssertionError("the call was expected to be refused and was not")
