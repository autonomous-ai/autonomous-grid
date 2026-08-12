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
