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

    # The CONVERSATION'S branch advanced to the result, which is what makes it the base its next
    # turn is cut from — the property D-e exists to provide.
    #
    # ⚠️ This assertion has moved twice. It named `refs/heads/main` until ADR 0033 D-c made a settle
    # fast-forward `wip/<member_key>`; ADR 0034 D-e re-keys that to the CONVERSATION, because a
    # member's conversations run at once and one shared ref means the second one's settle is refused
    # for doing what it was asked.
    branch = f"wip/{done['conversation_id']}"
    assert done["result_commit"] in H.git_ls_remote(url, f"refs/heads/{branch}",
                                                    bearer=owner_token)
    # ...and the result reaches `main` BY ITSELF (ADR 0034 D-d, issue 41). Nobody promotes; the relay
    # applies it on its own sweep, which is the headline of the whole feature. Polled rather than
    # asserted outright because the apply is deliberately OUTSIDE the settle request — that is what
    # keeps it off the lease TTL — so "done" and "on main" are one tick apart, not one moment.
    H.wait_for(
        lambda: done["result_commit"] in H.git_ls_remote(
            url, "refs/heads/main", bearer=owner_token),
        timeout=30.0)


def test_05_a_failed_turn_keeps_its_work_without_reaching_the_project(
        relay, owner_token, spawn_provider):
    """D-e's asymmetry, and ADR 0034 D-e inverts half of it.

    The CONVERSATION follows its turns whether they succeed or fail — an agent that broke halfway
    still did the half it finished, and the next turn's files have to match what the session
    remembers doing. What a failure must not reach is `main`, which the relay advances only on
    success (ADR 0034 D-d).

    ⚠️ Under ADR 0033 a failed task moved NOTHING, because the transcript rode in a commit that only
    reached the trunk on success and the branch was the member's. Both halves of that changed.
    """
    from remote import relay as relay_client

    spawn_provider("A")
    good = H.create(relay, owner_token, "WRITE kept.txt first", project="p-failure")["id"]
    landed = H.await_state(relay, owner_token, good, {"completed"})
    url = relay_client.git_remote_url(relay, landed["project_id"])
    H.wait_for(
        lambda: landed["result_commit"] in H.git_ls_remote(
            url, "refs/heads/main", bearer=owner_token),
        timeout=30.0)
    trunk_before = H.git_ls_remote(url, "refs/heads/main", bearer=owner_token)

    bad = H.create(
        relay, owner_token, "WRITE broken.txt half; FAIL the agent gave up",
        project="p-failure")["id"]
    failed = H.await_state(relay, owner_token, bad, {"failed"})

    assert failed.get("result_commit"), "a failed attempt must still push its branch"
    assert failed["result_commit"] != landed["result_commit"]

    # The half it finished is on its own conversation's branch, so the next turn starts from it.
    branch = f"wip/{failed['conversation_id']}"
    assert failed["result_commit"] in H.git_ls_remote(
        url, f"refs/heads/{branch}", bearer=owner_token), (
        "a failed turn left nothing behind — its conversation cannot carry on from where it broke")
    # And the project did not take it. Given a moment in which it COULD have: the apply sweep runs
    # every couple of seconds, so an assertion made immediately would pass against a relay that was
    # about to apply it.
    time.sleep(5)
    assert H.git_ls_remote(url, "refs/heads/main", bearer=owner_token) == trunk_before, (
        "a failed turn reached the project's trunk")


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
    #
    # ⚠️ **A RANGE, not equality, since ADR 0034 D-f (issue 43).** `deadline_at` is no longer written
    # once at create: `turn_promote._stamp` re-anchors it to `now + queue budget` at the moment the
    # turn is PREPARED, milliseconds later, so this difference is the budget PLUS however long the
    # preparation took. Equality here has been failing on that ever since — measured at 12.14s and
    # 12.15s against a 12s budget — and nothing noticed, because this file is run by hand and the
    # last recorded green run predates that slice.
    #
    # The check the assertion exists for is unharmed and is what the bound keeps: with the
    # PRODUCTION budgets this window would be four hours, so anything near the fixture's figure
    # proves the scaled budgets reached the process. The upper bound stays tight enough to catch
    # that, and the lower bound is exact because no path shortens the window.
    window = (datetime.fromisoformat(created["deadline_at"])
              - datetime.fromisoformat(created["created_at"]))
    assert timedelta(seconds=H.QUEUE_BUDGET_SECONDS) <= window < timedelta(
            seconds=H.QUEUE_BUDGET_SECONDS + 5), (
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

    # 6. And it reached the trunk the flag just made — by itself (ADR 0034 D-d, issue 41). This
    #    asserted the OPPOSITE until that slice: the work landed on the member's branch and the
    #    trunk stayed at `root` until somebody promoted. `ahead`/`behind` went with the promote they
    #    described.
    from remote import relay as _relay_client
    url = _relay_client.git_remote_url(relay, project_id)
    H.wait_for(
        lambda: done["result_commit"] in H.git_ls_remote(url, "refs/heads/main",
                                                         bearer=owner_token),
        timeout=30.0)
    assert relay_client.project_status(relay, owner_token, project_id)["main_commit"] != root, (
        "the trunk never moved, so `--init-project` produced a project nothing can reach")


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

    ⚠️ What is NOT here: a SECOND TURN of one conversation resuming its own session. That is
    `test_21`'s, over the route ADR 0034 D-n (issue 47) added — this test predates it and is left
    addressing what it was written for, two conversations rather than two turns. The paid
    `e2e_live_agent.py` still carries the half that needs the real binary.
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

    # ⚠️ **Wait for the first conversation's work to reach the trunk before opening the second**,
    # and that wait IS the behaviour rather than a flake guard. A conversation's first turn is cut
    # from `main` (ADR 0034 D-e), and `main` holds the first conversation's work only once the relay
    # has applied it (D-d) — which happens on its own sweep, a moment after the turn reports done.
    # Under ADR 0033 the two conversations shared one member branch, so this question did not arise.
    url = relay_client.git_remote_url(relay, project)
    H.wait_for(
        lambda: first_done["result_commit"] in H.git_ls_remote(
            url, "refs/heads/main", bearer=owner_token),
        timeout=30.0)

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

    # 2. The FILES are shared and that is correct — the second conversation's first turn is cut from
    #    the TRUNK, which the relay has just applied the first conversation's work to, so its
    #    workspace is materialized from a commit holding it. Pinned rather than assumed, because "a
    #    second conversation starts fresh" is easy to read as "it starts from an empty project", and
    #    an edit that made that true would break every follow-up in a real team's repository.
    #
    #    ⚠️ The ROUTE by which they are shared changed at issue 41 and the assertion did not: it used
    #    to be the member's own branch, which every turn of theirs fast-forwarded. It is now the
    #    project's trunk, which is a stronger property — a COLLEAGUE's work is there too.
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

    # 5. And the two working trees share ONE object store (ADR 0034 D-c, issue 50). Measured on the
    #    directory rather than read off the code, and over the wire rather than in a unit fixture:
    #    what the provider builds here is what a real claim produced.
    stores = sorted(p for p in members[0].iterdir() if (p / "objects").is_dir())
    assert len(stores) == 1, (
        f"expected the member's two conversations to share one object store, found {stores!r} — a "
        f"second conversation is costing a second copy of the project's whole history")
    for conversation in conversations:
        assert (conversation / "workspace" / ".git").is_file(), (
            f"{conversation}'s workspace is a repository of its own, not a linked worktree")


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

    # 3. The conversation's own branch carries the work and NOT the conversation's transcript.
    #
    #    ⚠️ `wip/<conversation_id>`, not `main` and no longer `wip/<member_key>`. ADR 0034 D-e
    #    re-keyed it (issue 41), and this ref is the right target for the same reason it always was:
    #    it is what the conversation's next turn is cut from, and it is exactly where the transcript
    #    used to accumulate. `main` is asserted separately, by test 04 — the relay applies to it on
    #    its own sweep, so reading it here would be a race rather than a fact.
    branch = f"wip/{conversation}"
    listing = subprocess.run(
        ["git", "--git-dir", str(repo), "ls-tree", "-r", "--name-only", f"refs/heads/{branch}"],
        capture_output=True, text=True).stdout
    assert "kept.txt" in listing, f"the agent's work is not on the member's branch: {listing!r}"
    assert ".grid" not in listing, (
        f"the member's branch still carries the reserved directory, so every turn adds a transcript "
        f"to what the next one is cut from: {listing!r}")


def _both_running(relay, token, ids, timeout=90.0):
    """Wait until EVERY id is `running` at the same moment, and return their VIEWS from that moment.

    ⚠️ **The point is simultaneity, not completion.** Asserting that two turns both reach
    `completed` is satisfied by a grid that runs them one after the other — which is exactly what
    every relay before ADR 0034 D-b did. This polls until both are `running` in one observation, and
    `None` (a timeout) is the failure the criterion is about.

    ⚠️ **It returns the whole views, so a caller reads every fact from ONE observation.** Re-reading
    `provider_id` afterwards is a race and it was a real flake: the harness runs a 6s lease with a 1s
    reaper so reclaims can be observed, and a turn reclaimed between the two reads reports the OTHER
    provider — which made "two providers" fail intermittently while both had genuinely been running.
    Turn prompts here stay well inside `H.LEASE_SECONDS` for the same reason.
    """
    def observation():
        views = {task_id: H.get(relay, token, task_id) for task_id in ids}
        return views if all(v.get("state") == "running" for v in views.values()) else None

    return H.wait_for(observation, timeout=timeout, interval=0.3)


def test_19_two_conversations_of_one_member_run_at_the_same_time(
        relay, owner_token, spawn_provider):
    """ADR 0034 D-b / issue 40, at the one seam where both repositories are on the wire together.

    Before this slice the relay's index was `tasks_one_active_per_member`, so a member's second
    `POST /tasks` was refused `member_has_active_task` outright: somebody who wanted the contact
    form fixed *and* the logo changed had to finish one first. Now they are two conversations and
    they run together.

    Both halves of the criterion are here because they fail differently:

      * **on ONE provider** — the relay hands out both, and the provider's own workspace reservation
        (`remote/tasks._reserve_workspace`, keyed on the project/member/conversation triple since
        issue 38) has to let the second through. Keyed on the pair it would refuse it SILENTLY, with
        no terminal report, and the turn would sit `running` until its lease lapsed;
      * **across TWO providers** — nothing about one process's bookkeeping is involved, so this is
        the relay's claim query alone.

    ⚠️ **The provider is spawned with two workers on purpose.** `provider_process.py` runs one
    `task_loop` by default and `test_09` depends on that, so concurrency is asked for per spawn.
    With one worker this test would pass by running the two turns in sequence — the exact
    green-for-the-wrong-reason it exists to rule out — which is why `_both_running` insists on
    seeing them running in ONE observation rather than both completing.
    """
    from remote import relay as relay_client

    spawn_provider("A", workers=2)
    project = relay_client.create_project(
        relay, owner_token, name="p-concurrent-conversations",
        bootstrap=relay_client.BOOTSTRAP_EMPTY)["id"]

    # Both created BEFORE either can finish — a `SLEEP` long enough that a grid running them in
    # sequence cannot have them both `running` at once.
    first = relay_client.create_task(
        relay, owner_token, project_id=project,
        prompt="SLEEP 3; WRITE contact-form.txt FIXED; SAY one")
    second = relay_client.create_task(
        relay, owner_token, project_id=project,
        prompt="SLEEP 3; WRITE logo.txt CHANGED; SAY two")

    assert first["id"] != second["id"]
    assert first["state"] == "queued" and second["state"] == "queued", (
        f"a create was refused or held: {first} {second}")

    running = _both_running(relay, owner_token, [first["id"], second["id"]])
    assert running, (
        "the member's two conversations never ran at the same time — the relay is still handing "
        "out one turn per member, or the provider refused the second workspace")


def test_20_two_conversations_of_one_member_run_across_two_providers(
        relay, owner_token, spawn_provider):
    """The same criterion with the provider's own bookkeeping taken out of it.

    Two processes, two node identities, one worker each — so nothing here can be satisfied by one
    process's workspace reservation being permissive. What is under test is the relay's claim query:
    it must hand two turns of two conversations to two different providers.
    """
    from remote import relay as relay_client

    a = spawn_provider("A")
    b = spawn_provider("B")
    project = relay_client.create_project(
        relay, owner_token, name="p-two-providers",
        bootstrap=relay_client.BOOTSTRAP_EMPTY)["id"]

    first = relay_client.create_task(
        relay, owner_token, project_id=project, prompt="SLEEP 3; SAY one")
    second = relay_client.create_task(
        relay, owner_token, project_id=project, prompt="SLEEP 3; SAY two")

    running = _both_running(relay, owner_token, [first["id"], second["id"]])
    assert running, "two conversations of one member did not run at once across two providers"

    # From the SAME observation that saw them both running — see `_both_running`. Read again
    # afterwards this is a race against the harness's deliberately short lease, and it flaked.
    holders = {view.get("provider_id") for view in running.values()}
    assert holders == {a.node_id, b.node_id}, (
        f"the two turns did not land on the two providers this test started, so it says nothing "
        f"the previous test did not.\n  holders={holders!r}\n"
        f"  provider A={a.node_id!r} log:\n{a.output()}\n"
        f"  provider B={b.node_id!r} log:\n{b.output()}")


def test_21_a_follow_up_message_runs_in_the_conversation_it_was_sent_to(
        relay, owner_token, spawn_provider, workspace_root):
    """ADR 0034 D-n / issue 47 — the door, at the one seam where both repositories are on the wire.

    Until this route existed every `POST /tasks` minted a conversation, so nothing in either suite
    could reach one twice: the relay's write of `ConversationRow.claude_session_id` and its read
    back on the claim were each tested against a row the OTHER half never wrote. `test_17`'s
    docstring says so in as many words. This is what joins them, through a real settle and a real
    claim, with nothing doctored.

    ⚠️ **Sequential on purpose, and that is a limitation rather than a convenience.** A follow-up
    posted while its sibling is still RUNNING is cut from the branch as it stood before that
    sibling — `input_commit` is written once, at create — so it would not compose and its settle
    would be refused `wip_not_fast_forward`. Issue 43 owns moving that computation to eligibility
    and issue 41 owns `wip/<conversation_id>`; the relay-side pin is
    `test_follow_up_turn.TestWhatAFollowUpDoesNotYetCompose`, which flips when they land. What this
    slice delivers, and what this test covers, is the case a person actually hits: reply after the
    last answer came back.
    """
    from remote import relay as relay_client

    spawn_provider("A")
    project = relay_client.create_project(
        relay, owner_token, name="p-follow-up",
        bootstrap=relay_client.BOOTSTRAP_EMPTY)["id"]

    first = relay_client.create_task(
        relay, owner_token, project_id=project,
        prompt="WRITE note.txt MAGPIE-4417; SAY started")
    first_done = H.await_state(
        relay, owner_token, first["id"], {"completed", "failed"}, timeout=120)
    assert first_done["state"] == "completed", first_done

    # 1. The id a person is given is the id the route takes. `create_task`'s reply is the only
    #    surface that has ever carried it, so a relay that stopped sending it fails right here —
    #    which is the point of reading it off the reply rather than out of the database.
    conversation_id = first["conversation_id"]
    assert conversation_id and conversation_id != first["id"], (
        f"the create did not name a conversation distinct from its turn: {first!r}")

    second = relay_client.send_turn(
        relay, owner_token, conversation_id, prompt="READ note.txt; SAY continued")
    second_done = H.await_state(
        relay, owner_token, second["id"], {"completed", "failed"}, timeout=120)

    # 2. It ran, and it ran in the conversation it was addressed to.
    assert second_done["state"] == "completed", (
        f"the follow-up did not complete: {second_done}")
    assert second["conversation_id"] == conversation_id, (
        f"the follow-up opened a conversation of its own: {second!r}")

    # 3. It composed: the follow-up's workspace holds what the first turn wrote. Sequential, so
    #    this is the case that works today — see the docstring.
    assert "MAGPIE-4417" in (second_done.get("result_text") or ""), (
        f"the follow-up could not see the file its own conversation's first turn wrote: "
        f"{second_done}")

    # 4. The SAME Claude Code session, which is the whole reason a conversation exists.
    #    `fake_claude.py` echoes back whatever it was told to `--resume` and mints a fresh id
    #    otherwise, so two different ids here means the second turn started cold — the failure a
    #    person would experience as the agent having forgotten the conversation.
    assert first_done.get("claude_session_id"), first_done
    assert second_done["claude_session_id"] == first_done["claude_session_id"], (
        f"the follow-up did not resume its own conversation's session — it started a fresh one in "
        f"a workspace that remembers everything: {second_done}")
    assert not second_done.get("session_reset_reason"), second_done

    # 5. ONE workspace on disk, not two. The provider keys it by conversation, so a second directory
    #    would mean the follow-up ran somewhere the session's transcript is not — the exact failure
    #    `conversation_id`'s fail-closed refusal exists to prevent, arriving by a different door.
    assert _one_conversation_dir(workspace_root, project) == conversation_id


# The words a person driving this product must never be shown (ADR 0034 D-m). Not an exhaustive git
# glossary — a list nobody can satisfy is a list nobody keeps — but the terms this feature's own
# design deleted or hid: the ones that used to appear because a MEMBER had to move refs by hand.
_BRANCH_VOCABULARY = ("branch", "wip/", "fast-forward", "fast_forward", "rebase",
                      "promote", "integrate", "merge conflict", "refs/")


def test_22_two_conversations_run_a_whole_session_and_the_work_appears_by_itself(
        relay, relay_home, owner_token, spawn_provider):
    """Issue 41's last criterion: a whole session, and nothing anybody reads names a branch.

    Two conversations touching DIFFERENT files, both cut from the trunk, both finishing. The first
    fast-forwards it; the second is a clean three-way merge the relay makes on its own. Nobody runs a
    command in between, which is the entire point of ADR 0034 D-d — under D-b's predecessor this
    needed a `promote`, and once anyone had promoted, an `integrate` before the next one.

    ⚠️ **Two CONVERSATIONS rather than two people, and the limit is the harness's not the design's.**
    `_harness.start_relay` sets `GRID_MODE=false`, so the relay never writes a `users` row for the
    tokens minted here and `POST …/members` cannot look a colleague up by email — the same gap
    `test_15` records about its own downgrade. What is under test is unaffected: the trunk cannot
    tell whose conversation a result came from, and two owners racing for it is covered where a real
    interleaving is available, in grid-src's `test_trunk_apply_postgres.py`.
    """
    from remote import relay as relay_client

    spawn_provider("A")
    project = relay_client.create_project(
        relay, owner_token, name="p-together", bootstrap="empty")["id"]
    url = relay_client.git_remote_url(relay, project)
    trunk_before = H.git_ls_remote(url, "refs/heads/main", bearer=owner_token)

    first = relay_client.create_task(
        relay, owner_token, project_id=project, prompt="WRITE alice.txt from-alice; SAY done")
    second = relay_client.create_task(
        relay, owner_token, project_id=project, prompt="WRITE bob.txt from-bob; SAY done")

    finished = [H.await_state(relay, owner_token, turn["id"], {"completed", "failed"}, timeout=120)
                for turn in (first, second)]
    assert [turn["state"] for turn in finished] == ["completed", "completed"], finished

    # 1. Both results reach the project with no command run by anyone — one by fast-forward, the
    #    other by a merge the relay made. Polled because the apply is deliberately outside the settle
    #    request (ADR 0034 D-d): "done" and "in the project" are a tick apart, never the same moment.
    for turn in finished:
        H.wait_for(
            lambda commit=turn["result_commit"]: commit in H.git_ls_remote(
                url, "refs/heads/main", bearer=owner_token),
            timeout=45.0)
    assert H.git_ls_remote(url, "refs/heads/main", bearer=owner_token) != trunk_before

    # 2. And BOTH files are there. The trunk moving proves something landed; only the tree proves
    #    that the second apply merged rather than replacing the first one's work — which is the
    #    failure a lost update produces, and it looks perfectly healthy from the ref alone.
    #    Read off the relay's own repository with `ls-tree`, the way `test_18` reads a ref: fetching
    #    the trunk into a fresh clone is refused by git itself (`refusing to fetch into branch
    #    'refs/heads/main' checked out at …`), and building a second authenticated-git path in this
    #    test to work round that would be asserting through machinery nothing else uses.
    import subprocess

    repo = relay_home / "projects" / f"{project}.git"
    landed = sorted(subprocess.run(
        ["git", "--git-dir", str(repo), "ls-tree", "-r", "--name-only", "refs/heads/main"],
        capture_output=True, text=True, check=True).stdout.split())
    assert landed == ["alice.txt", "bob.txt"], (
        f"the project holds {landed} — one conversation's work was replaced rather than combined")

    # 3. Nothing a person reads names a branch. The task views and the whole event stream, which is
    #    everything `grid task follow` and `grid task get` render.
    said = []
    for turn in finished:
        said.append(str(turn.get("result_text") or ""))
        said.append(str(turn.get("error") or ""))
        said.extend(str(payload) for _seq, payload in relay_client.stream_task_events(
            relay, owner_token, turn["id"], after_seq=-1))
    surface = " ".join(said).lower()
    # The control. A denylist over an empty string passes forever, and this surface is assembled
    # from three optional fields and a stream — any one of which could quietly stop being read.
    assert len(surface) > 200, (
        f"only {len(surface)} characters of what a person reads were captured, so finding no "
        f"branch vocabulary in it proves nothing: {surface!r}")
    leaked = [word for word in _BRANCH_VOCABULARY if word in surface]
    assert not leaked, (
        f"a person following this session was shown {leaked} — ADR 0034 D-m's whole premise is that "
        f"the reader does not know what a branch is. Surface: {surface[:600]}")


def test_23_a_collision_is_resolved_by_the_conversation_that_caused_it(
        relay, relay_home, owner_token, spawn_provider, workspace_root):
    """ADR 0034 D-g / issue 42, at the one seam where both repositories are on the wire.

    Two conversations change the same line. The first reaches `main`; the second cannot, so the relay
    hands the collision back to the conversation that caused it — as a turn of that conversation, in
    the same Claude Code session, **ahead of the follow-up its owner had already typed**. Nobody runs
    a command, and nothing anybody reads names a branch.

    ⚠️ **The ordering is the criterion** (issue 42): *"a merge turn that runs last fails this"*. The
    claim orders by `created_at`, so the merge turn is the newest row in the queue and the follow-up
    is older — left alone, the follow-up runs first on work the grid has not combined, conflicts in
    its turn, and queues another merge turn per typed-ahead message.

    ⚠️ **The whole loop is real here and nowhere else.** The relay decides the tier and cuts the
    turn; the provider fetches `refs/integrate/<id>` and spawns; `fake_claude` runs a genuine
    `git merge` and stages every conflicted path; the provider's `ls-files --unmerged` guard reads
    the index BEFORE `git add -A`; settle checks the pinned oid; and the apply sweep puts the result
    on the trunk. A unit test can hold any one of those against a fixture, and none of them against
    each other.
    """
    import subprocess

    from remote import relay as relay_client

    spawn_provider("A")
    project = relay_client.create_project(
        relay, owner_token, name="p-collide", bootstrap=relay_client.BOOTSTRAP_EMPTY)["id"]
    repo = relay_home / "projects" / f"{project}.git"

    def _trunk_shared():
        """`shared.txt` as the project holds it, or `""` while the trunk has no such file yet.

        ⚠️ **Content, never `commit in ls-remote(main)` — the shape `test_22` uses.** The trunk does
        not stop at any one of these results: the scaffolding turn below applies too, cleanly, so
        `main` ends up a DESCENDANT of whichever commit is being waited for rather than equal to it.
        An equality check therefore reports a trunk that contains everything as work that never
        landed — and it does so INTERMITTENTLY, depending on which apply the sweep reached first,
        which is the worst possible way for a test to be wrong. Measured: this test passed alone and
        failed inside the full module.
        """
        shown = subprocess.run(
            ["git", "--git-dir", str(repo), "show", "refs/heads/main:shared.txt"],
            capture_output=True, text=True)
        return shown.stdout if shown.returncode == 0 else ""

    # Both cut from the trunk as it stands now, so whichever lands second cannot reach it. The same
    # timing `test_22` relies on, and the same one a real team produces without trying.
    #
    # ⚠️ **WHICH of them loses is not decidable from here, and assuming it made this test pass alone
    # and fail inside the full module.** A create returns once its turn is `preparing`; it becomes
    # claimable when its input commit lands, and the provider is polling throughout — so under load
    # the second create's git work can finish first and be claimed first. The loser is therefore
    # DISCOVERED below, from the merge turn the relay cut, which is also the more honest assertion:
    # the conflict goes to the conversation that caused it, whichever that turns out to be.
    alice = relay_client.create_task(
        relay, owner_token, project_id=project, prompt="WRITE shared.txt from-alice; SAY done")
    bob = relay_client.create_task(
        relay, owner_token, project_id=project, prompt="WRITE shared.txt from-bob; SAY done")
    # ⚠️ **This one is scaffolding and it is what makes the ordering assertion honest.** The provider
    # runs ONE turn at a time (`provider_process.py`'s worker default), so an unrelated turn created
    # HERE — after the collision and before the follow-up — occupies it across the apply tick that
    # discovers the conflict. Without it the follow-up becomes claimable the instant its sibling
    # settles and is picked up before the sweep has run, which is a race in the harness rather than
    # a property of the design, and would make this test flaky in the direction that reads as a pass.
    blocker = relay_client.create_task(
        relay, owner_token, project_id=project, prompt="SLEEP 10; SAY done")
    # One into EACH conversation, typed while their turns were still running so both are OLDER than
    # any merge turn. Both, because the loser is not known yet — and the one in the conversation that
    # wins is harmless: its own turn is cut from a trunk that has not moved and it writes nothing.
    typed_ahead = {
        turn["conversation_id"]: relay_client.send_turn(
            relay, owner_token, turn["conversation_id"], prompt="READ shared.txt; SAY continued")
        for turn in (alice, bob)}

    finished = {}
    for turn in (alice, bob):
        finished[turn["conversation_id"]] = H.await_state(
            relay, owner_token, turn["id"], {"completed", "failed"}, timeout=120)
    assert [done["state"] for done in finished.values()] == ["completed", "completed"], finished
    assert H.wait_for(lambda: "from-" in _trunk_shared(), timeout=45.0), (
        f"neither result reached the project, so there is no collision to hand back. "
        f"Applies owed: {_applies(relay_home)}")

    # 1. A merge turn appears in that conversation, marked as machinery rather than as a message.
    def _merge_turn():
        listed = relay_client.list_tasks(relay, owner_token, project, mine=False, limit=50)
        for task in listed.get("tasks") or ():
            if task.get("kind") == "merge":
                return task
        return None

    merge = H.wait_for(_merge_turn, timeout=45.0)
    assert merge, "the collision was never handed back to anybody"
    losing = merge["conversation_id"]
    assert losing in finished, (
        f"the collision was handed to a conversation that did not cause it: {merge!r}")
    follow_up = typed_ahead[losing]
    assert merge["id"] not in (follow_up["id"], blocker["id"])

    # 2. It runs BEFORE that conversation's typed-ahead message — **the criterion**. Compared on
    #    `claimed_at`, which is a record of what the provider was handed and when, rather than on a
    #    state read at a moment that races the very scheduling under test.
    merge_done = H.await_state(
        relay, owner_token, merge["id"], {"completed", "failed"}, timeout=120)
    assert merge_done["state"] == "completed", merge_done
    follow_up_now = relay_client.get_task(relay, owner_token, follow_up["id"])
    assert merge_done["claimed_at"], merge_done
    assert (follow_up_now["claimed_at"] is None
            or follow_up_now["claimed_at"] > merge_done["claimed_at"]), (
        f"the typed-ahead message ran first, so it worked from a tree the grid had not combined and "
        f"will collide all over again: follow-up {follow_up_now}, merge {merge_done}")

    # 3. The same session, which is the entire justification for asking this conversation rather
    #    than the grid. `fake_claude` echoes back whatever it is told to `--resume`.
    assert merge_done["claude_session_id"] == finished[losing]["claude_session_id"], (
        f"the merge turn started a fresh session, so the agent resolving the collision had none of "
        f"the intent that is the only reason to ask it: {merge_done}")

    # 4. And BOTH people's work is in the project, with nobody having run a command. Content is the
    #    criterion: a two-parent commit that discarded one side satisfies every structural check
    #    there is, which is the failure ADR 0033 issue 15 measured.
    assert H.wait_for(
        lambda: "from-alice" in _trunk_shared() and "from-bob" in _trunk_shared(),
        timeout=45.0), (
        f"the project holds {_trunk_shared()!r} — one side was discarded, or the merge never "
        f"reached the trunk at all. Applies owed: {_applies(relay_home)}")

    # 5. Nothing a person reads names a branch — including the relay's own merge prompt, which is
    #    why `kind` exists. The merge turn's `prompt` is deliberately NOT in this surface: it is the
    #    relay's text for an agent, and issue 42's answer is that a client renders the KIND instead.
    said = [str(merge_done.get("result_text") or ""), str(merge_done.get("error") or ""),
            str(finished[losing].get("result_text") or "")]
    for turn in (finished[losing], merge):
        said.extend(str(payload) for _seq, payload in relay_client.stream_task_events(
            relay, owner_token, turn["id"], after_seq=-1))
    surface = " ".join(said).lower()
    assert len(surface) > 200, (
        f"only {len(surface)} characters of what a person reads were captured, so finding no branch "
        f"vocabulary in it proves nothing: {surface!r}")
    leaked = [word for word in _BRANCH_VOCABULARY if word in surface]
    assert not leaked, (
        f"a person whose work collided was shown {leaked}. Surface: {surface[:600]}")


def _applies(relay_home):
    """The relay's apply queue, read straight out of its SQLite file.

    Only ever used to make a failure diagnosable: the trunk not moving is the symptom of every fault
    in that queue, and without the row — its state, its attempts, its error — the message says
    nothing about which. The relay's stdout is a pipe nothing drains, so this is the only place the
    reason is reachable from a test.
    """
    import sqlite3

    with sqlite3.connect(relay_home / "e2e.db") as db:
        return db.execute(
            "SELECT state, attempts, merge_turn_id, error, conflicts FROM trunk_applies "
            "ORDER BY created_at").fetchall()


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


def test_23_a_member_cannot_take_the_whole_fleet(
        relay, relay_db, owner_token, spawn_provider):
    """ADR 0034 D-i / issue 49, at the one seam where a real relay, a real fleet and two real people
    are on the wire together.

    The unit suites prove the predicate; what only this can prove is that a provider with FREE
    WORKERS is offered the colleague's turn and not the capped member's. A relay whose cap was
    applied to the claim's result instead of inside its SELECT passes every SQL-level assertion and
    fails here, because the candidate list is truncated before the colleague's row is reached.

    ⚠️ **A real colleague, unlike `test_22`'s two conversations**, and it costs a seeded `users` row:
    `_harness.start_relay` runs `GRID_MODE=false`, so nothing writes one and `POST …/members` cannot
    look anybody up by email. `test_04` established the seeding, and here it is load-bearing rather
    than incidental — the criterion is about two PEOPLE sharing a fleet, and one person's two
    conversations cannot express it.

    ⚠️ **The cap is READ from the status view rather than restated.** Hard-coding 3 would make this
    test disagree with the relay the day somebody changes the default, and disagree silently: it
    would create too few turns, never reach the cap, and pass while proving nothing.

    ⚠️ **Load-sensitive, like `test_19` and `test_20` and for their reason.** It needs the member's
    turns genuinely running at one moment, inside `H.LEASE_SECONDS`; a machine busy enough to stretch
    five concurrent fake agents past that will reclaim one and the observation never lands. Run it
    alone before believing a failure.
    """
    import sqlite3

    from remote import relay as relay_client

    colleague = H.token("bob", "bob-node")
    with sqlite3.connect(relay_db) as db:
        db.execute(
            "INSERT OR REPLACE INTO users (user_id, email, name, google_sub, created_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            ("bob", "bob@example.com", "Bob Tran", "sub-bob"))

    project = relay_client.create_project(
        relay, owner_token, name="p-fairness", bootstrap=relay_client.BOOTSTRAP_EMPTY)["id"]
    relay_client.add_project_member(relay, owner_token, project, email="bob@example.com")

    cap = relay_client.project_status(relay, owner_token, project)["member_running_cap"]
    assert isinstance(cap, int) and cap >= 1, cap

    mine = [relay_client.create_task(relay, owner_token, project_id=project,
                                     prompt=f"SLEEP 3; SAY mine-{i}")
            for i in range(cap)]
    # Created BEFORE the colleague's, so global-FIFO alone would hand this one out first. That
    # ordering is the whole test: what must be claimed ahead of it is the NEWER turn.
    surplus = relay_client.create_task(
        relay, owner_token, project_id=project, prompt="SLEEP 1; SAY surplus")
    theirs = relay_client.create_task(
        relay, colleague, project_id=project, prompt="SLEEP 1; SAY theirs")

    # ⚠️ **The fleet arrives AFTER the queue, and that is what makes this deterministic rather than
    # a race against a stopwatch.** Started first, the member's early turns run — and can FINISH —
    # while the later creates are still doing their git work, so the moment this test is about may
    # be over before there is anything to poll. Measured: the first draft failed exactly that way,
    # with every one of the five `completed` and the assertion unable to say whether the cap had
    # ever held. With the queue already full, claim order is `created_at` and nothing depends on how
    # fast a create returns.
    #
    # More workers than the cap, so an idle worker exists at the moment the capped turn is passed
    # over. With exactly `cap` workers the fleet would be full and this would prove nothing.
    spawn_provider("A", workers=5)

    # What the poll last saw, so a timeout says WHICH half never happened. A bare "it never
    # happened" here would leave the reader unable to tell a broken cap from a slow machine.
    last = {}

    def observation():
        """The first moment the colleague's turn has been PICKED UP, with the surplus read alongside.

        ⚠️ **Deliberately NOT "all of the member's turns are running at once".** That was the first
        draft and it is a race against a stopwatch rather than a test of the rule: the member's turns
        are `SLEEP 3`, so on a busy machine the poll's first read already finds them `completed` and
        the assertion cannot say whether the cap ever held. Measured twice, exactly that way.

        What the criterion asks is an ORDERING — the colleague's NEWER turn is served while the
        member's OLDER surplus is not — and an ordering is observable from one side. Everything is
        read in a single pass for `_both_running`'s reason: re-reading afterwards races the
        harness's deliberately short lease.
        """
        theirs_view = H.get(relay, colleague, theirs["id"])
        surplus_view = H.get(relay, owner_token, surplus["id"])
        mine_states = [H.get(relay, owner_token, turn["id"]).get("state") for turn in mine]

        def seen(view):
            """State AND error: a turn that FAILED is a different problem from one that is late,
            and a diagnostic that cannot tell them apart sends the reader to the wrong place."""
            return view.get("state") + (f"({view['error']})" if view.get("error") else "")

        last.update(mine=mine_states, theirs=seen(theirs_view), surplus=seen(surplus_view))
        if theirs_view.get("state") == "queued":
            return None
        return surplus_view, mine_states

    got = H.wait_for(observation, timeout=90.0, interval=0.2)
    assert got, (
        f"the colleague's turn was never picked up at all, so this says nothing about fairness. "
        f"last seen: {last}")
    surplus_view, mine_states = got

    # THE CRITERION. The colleague's turn is NEWER than the surplus, so global-FIFO alone would have
    # served the surplus first; the member being at their cap is the only thing that reorders them.
    assert surplus_view["state"] == "queued", (
        f"a member at their cap was handed a {cap + 1}th turn before a colleague's newer one: "
        f"surplus={surplus_view['state']}, mine={mine_states}")
    assert surplus_view.get("provider_id") is None and surplus_view.get("attempt", 0) == 0, (
        f"the capped turn was claimed and put back, spending an attempt on a refusal that has "
        f"nothing to do with it: {surplus_view}")
    # ...and the fleet was demonstrably not the constraint: the member's OWN turns were holding the
    # slots. Without this, the assertion above is also satisfied by a grid that served nobody.
    assert any(state == "running" for state in mine_states), (
        f"the surplus was queued while none of the member's own turns were running, so something "
        f"other than the cap was holding it: {mine_states}")


def test_24_a_conversation_whose_workspace_was_evicted_carries_on_where_it_left_off(
        relay, owner_token, spawn_provider, workspace_root):
    """ADR 0034 D-c / issue 50's fourth criterion, and the one no unit test can reach.

    A provider's workspaces are bounded, and the bound is enforced by deleting the least recently
    used — which deletes the only local copy of a conversation, its Claude Code session included.
    That is survivable for exactly one reason: since issue 39 the transcript lives on
    `refs/grid/agent/<conversation_id>`, and the relay pins the commit a turn should resume on the
    claim. So the proof has to run both halves against a real relay: the eviction is this
    repository's, the pin is grid-src's, and a provider that evicted while the relay stopped sending
    `transcript_commit` would leave both unit suites green and every conversation starting cold.

    The provider is spawned with a cap of ONE, so the colleague conversation's turn in the middle is
    what performs the eviction — the real path, driven by the bound, rather than an `rmtree` standing
    in for it.

    ⚠️ **`GRID_MAX_TASKS` stays at 1.** Two workers against a cap of one workspace would have each
    turn evicting the other's working tree mid-run, which is a fault this design accepts by keying
    eviction on the reservation — and pointing this test at it would make it about the reservation
    instead of about resuming.
    """
    from remote import relay as relay_client

    spawn_provider("A", extra_env={"GRID_TASK_MAX_WORKSPACES": "1"})
    project = relay_client.create_project(
        relay, owner_token, name="p-evicted",
        bootstrap=relay_client.BOOTSTRAP_EMPTY)["id"]

    first = relay_client.create_task(
        relay, owner_token, project_id=project,
        prompt="WRITE ledger.txt OSPREY-8823; SAY started")
    first_done = H.await_state(
        relay, owner_token, first["id"], {"completed", "failed"}, timeout=120)
    assert first_done["state"] == "completed", first_done
    conversation_id = first["conversation_id"]

    members = sorted((workspace_root / "A" / "projects" / project).iterdir())
    assert len(members) == 1, members
    evicted = members[0] / conversation_id
    assert (evicted / "workspace").is_dir(), (
        f"the first turn left no workspace at {evicted}, so there is nothing for the bound to evict")

    # A SECOND conversation, which is what puts this provider over its cap of one.
    second = relay_client.create_task(
        relay, owner_token, project_id=project, prompt="WRITE other.txt ignored; SAY done")
    second_done = H.await_state(
        relay, owner_token, second["id"], {"completed", "failed"}, timeout=120)
    assert second_done["state"] == "completed", second_done
    assert second["conversation_id"] != conversation_id

    # 1. The bound was enforced by EVICTION, and the second turn was not refused for disk.
    assert not evicted.exists(), (
        f"{evicted} survived a provider capped at one workspace — the bound is not being enforced, "
        f"so a provider accumulates a directory per conversation exactly as it did before")

    # 2. And now the first conversation is spoken to again. Its workspace, its checkout and its
    #    Claude Code session all have to come back from the relay alone.
    H.wait_for(
        lambda: first_done["result_commit"] in H.git_ls_remote(
            relay_client.git_remote_url(relay, project), "refs/heads/main", bearer=owner_token),
        timeout=30.0)
    third = relay_client.send_turn(
        relay, owner_token, conversation_id, prompt="READ ledger.txt; SAY continued")
    third_done = H.await_state(
        relay, owner_token, third["id"], {"completed", "failed"}, timeout=120)

    assert third_done["state"] == "completed", (
        f"the evicted conversation could not run again: {third_done}")
    # THE SAME SESSION. `fake_claude.py` echoes back whatever it was told to `--resume` and mints a
    # fresh id otherwise, so a different id here is a person whose agent forgot the conversation —
    # which is exactly what eviction would cost if the transcript did not travel on its own ref.
    assert third_done["claude_session_id"] == first_done["claude_session_id"], (
        f"the evicted conversation started a cold session rather than resuming: "
        f"first={first_done.get('claude_session_id')}, third={third_done.get('claude_session_id')}")
    assert not third_done.get("session_reset_reason"), third_done
    # And the same FILES: the checkout was rebuilt from the trunk the first turn's work reached.
    assert "OSPREY-8823" in (third_done.get("result_text") or ""), (
        f"the rebuilt workspace does not hold what this conversation wrote: {third_done}")


class _ConversationReader:
    """A conversation's stream, followed on a daemon thread until it says it is idle.

    A thread rather than the generator inline, because the stream's whole contract is that it HOLDS
    while work is running: `_TASK_FOLLOW_TIMEOUT` sets `read=None` deliberately (silence is not
    death), so a conversation that never goes idle would HANG this suite rather than fail it — and a
    hung suite reads as a passing one, since it prints no summary and therefore no FAILED lines.
    """

    def __init__(self, relay, token, conversation_id, *, after_seq=-1):
        import threading

        self.blocks: list = []
        self.error: list = []
        self._worker = threading.Thread(target=self._follow, daemon=True)
        self._args = (relay, token, conversation_id, after_seq)

    def _follow(self):
        from remote import relay as relay_client

        relay_url, token, conversation_id, after_seq = self._args
        try:
            for seq, task_id, event in relay_client.stream_conversation_events(
                    relay_url, token, conversation_id, after_seq=after_seq):
                self.blocks.append((seq, task_id, event))
                if event.get("type") == "conversation.idle":
                    return
        except Exception as exc:                       # noqa: BLE001 - reported, never swallowed
            self.error.append(exc)

    def start(self):
        self._worker.start()
        # Attached means "has received something", and the first block always arrives: the relay
        # backfills before it parks, and an empty conversation is answered with the idle block.
        H.wait_for(lambda: bool(self.blocks) or bool(self.error), timeout=60.0)
        assert not self.error, f"the conversation stream failed to attach: {self.error[0]!r}"
        return self

    def finish(self, timeout=180.0):
        self._worker.join(timeout)
        assert not self.error, f"the conversation stream failed: {self.error[0]!r}"
        assert not self._worker.is_alive(), (
            f"the stream never reached conversation.idle within {timeout}s; "
            f"got {len(self.blocks)} blocks")
        return self.blocks


def test_25_one_stream_carries_a_whole_conversation(
        relay, owner_token, spawn_provider):
    """ADR 0034 D-m / issue 51, at the seam where both repositories are on the wire.

    Every unit test on this side reads an SSE body this repository wrote down. What only a real
    relay can show is the two halves agreeing about the WIRE: the route, the `conv_seq` cursor, the
    `{"task_id", "event"}` envelope, and the ending block. A relay that shaped any of them
    differently leaves both suites green and every conversation unfollowable.

    Attached BEFORE the second message is sent, so the criterion under test is the one a client-side
    fan-out cannot meet: the stream follows into a turn that did not exist when the reader attached.
    """
    from remote import relay as relay_client

    spawn_provider("A")
    project = relay_client.create_project(
        relay, owner_token, name="p-one-stream",
        bootstrap=relay_client.BOOTSTRAP_EMPTY)["id"]

    first = relay_client.create_task(
        relay, owner_token, project_id=project,
        prompt="WRITE ledger.txt HERON-2291; SAY first turn")
    first_done = H.await_state(
        relay, owner_token, first["id"], {"completed", "failed"}, timeout=120)
    assert first_done["state"] == "completed", first_done
    conversation_id = first["conversation_id"]

    # Attached FIRST, then spoken to. The reader learns about the second turn from the stream.
    reader = _ConversationReader(relay, owner_token, conversation_id).start()
    second = relay_client.send_turn(
        relay, owner_token, conversation_id, prompt="READ ledger.txt; SAY second turn")
    seen = reader.finish()

    second_done = H.await_state(
        relay, owner_token, second["id"], {"completed", "failed"}, timeout=120)
    assert second_done["state"] == "completed", second_done

    # 1. ONE cursor over the whole conversation, in order and never repeated. The ending block
    #    re-states the last cursor deliberately, so it is excluded rather than counted as a repeat.
    seqs = [seq for seq, _t, _e in seen[:-1]]
    assert seqs == sorted(seqs), f"the conversation's events arrived out of order: {seqs}"
    assert len(seqs) == len(set(seqs)), f"an event was repeated: {seqs}"

    # 2. BOTH turns are on it — the second having been sent after the reader attached, which is the
    #    criterion a client fanning out per turn cannot meet.
    turns = {task_id for _s, task_id, _e in seen if task_id}
    assert {first["id"], second["id"]} <= turns, (
        f"the stream did not carry both turns of the conversation; it carried {turns}")

    # 3. It ended by saying so, that block belongs to no turn, and it moved no cursor.
    assert seen[-1][2] == {"type": "conversation.idle"}, seen[-1]
    assert seen[-1][1] is None, seen[-1]
    assert seen[-1][0] == seen[-2][0], (
        f"the ending block claimed a cursor of its own: {seen[-2:]}")


def _refusal(call):
    """What a refused call SAYS, as a comparable value. Anything else is a test failure."""
    try:
        call()
    except SystemExit as exc:
        return str(exc)
    raise AssertionError("the call was expected to be refused and was not")


def test_26_undoing_a_change_leaves_every_later_turn_alone(
        relay, relay_home, owner_token, spawn_provider):
    """ADR 0034 D-l (issue 44), end to end through both repositories' real code.

    Undo is the counterweight to issue 41: auto-apply removed the moment a person could decline, so
    the only way back has to be something they press afterwards. The criterion that makes it hard is
    the second half — *leaves every later turn's work intact* — because the obvious implementation,
    resetting the trunk, destroys exactly that and is why D-l names it as refused.

    ⚠️ **The only seam where this CLI's request and the relay's git meet.** grid-src's own suite
    proves the reversal and this repository's proves the path; neither can see the two DISAGREEING,
    because every unit test on this side answers from a body this repository wrote down.
    """
    import subprocess

    from remote import relay as relay_client

    spawn_provider("A")
    project = relay_client.create_project(
        relay, owner_token, name="p-undo", bootstrap="empty")["id"]
    url = relay_client.git_remote_url(relay, project)
    repo = relay_home / "projects" / f"{project}.git"

    def landed():
        return sorted(subprocess.run(
            ["git", "--git-dir", str(repo), "ls-tree", "-r", "--name-only", "refs/heads/main"],
            capture_output=True, text=True, check=True).stdout.split())

    def run(prompt):
        turn = relay_client.create_task(relay, owner_token, project_id=project, prompt=prompt)
        done = H.await_state(relay, owner_token, turn["id"], {"completed", "failed"}, timeout=120)
        assert done["state"] == "completed", done
        return turn

    regretted = run("WRITE regretted.txt oops; SAY done")
    H.wait_for(lambda: "regretted.txt" in landed(), timeout=45.0)
    # A SECOND turn, sent only once the first has LANDED, so it is genuinely later work built on a
    # trunk that already holds the change about to be taken out.
    run("WRITE kept.txt keep-me; SAY done")
    H.wait_for(lambda: "kept.txt" in landed(), timeout=45.0)

    def trunk_oid():
        """The trunk's commit, as an OID rather than as the `ls-remote` LINE.

        ⚠️ `H.git_ls_remote` answers `<oid>\\trefs/heads/main\\n`. Its other callers here only ask
        whether a commit they already have appears in it, so a substring test is all they need; this
        one hands the value to `merge-base`, where a whole line is `Not a valid object name` and
        fails as *"the trunk moved backwards"* — an assertion about the one thing that had not
        happened.
        """
        oid = H.git_ls_remote(url, "refs/heads/main", bearer=owner_token).split()[0]
        assert len(oid) == 40, f"expected an oid and got {oid!r}"
        return oid

    trunk_before = trunk_oid()

    answer = relay_client.undo_task(relay, owner_token, regretted["id"])

    # 1. The change is out, and the later turn's work is untouched.
    assert answer["undone"] is True, answer
    assert landed() == ["kept.txt"], (
        f"the project holds {landed()} — undo either missed its own change or took away a later "
        f"turn's work, which is the failure D-l refuses a trunk reset to avoid")

    # 2. And the trunk moved FORWARD to get there. A reset satisfies the assertion above while
    #    handing every clone on the team a commit the grid no longer has.
    trunk_after = trunk_oid()
    assert trunk_after != trunk_before
    assert subprocess.run(
        ["git", "--git-dir", str(repo), "merge-base", "--is-ancestor", trunk_before, trunk_after],
        capture_output=True).returncode == 0, "the trunk moved backwards"

    # 3. A second undo is refused rather than answered with another reversal — which against a trunk
    #    that no longer holds the change would be an empty commit reported as an undo.
    said = _refusal(lambda: relay_client.undo_task(relay, owner_token, regretted["id"]))
    assert "already been undone" in said, said
    assert landed() == ["kept.txt"], "a refused second undo still moved the project"

    # 4. Nothing a person reads names a branch (ADR 0034 D-m) — this is the one command in the plane
    #    whose entire subject is a git operation.
    surface = " ".join(str(value) for value in answer.values()).lower() + " " + said.lower()
    leaked = [word for word in _BRANCH_VOCABULARY if word in surface]
    assert not leaked, f"undo showed a person {leaked}: {surface}"
