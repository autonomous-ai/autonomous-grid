"""The checks no test replaces: REAL Claude Code, driven by the real provider against a real relay.

`e2e_cross_repo.py` runs `fake_claude.py`, which is honest about the wire and cannot be honest about
the vendor. Issue 06's bug is the standing proof that the difference matters: the provider planted
its transcript symlink at the UNRESOLVED workspace path while the binary — whose cwd comes from
`getcwd`, already resolved — wrote somewhere else. The task completed, the session id came back, the
terminal report was correct, and the conversation was simply not captured. Every unit test agreed
with the bug, because each compared our own computation against itself.

Two things are deliberate and neither is a preference:

* **The DEFAULT config directory.** Issue 01's spike measured a fresh `CLAUDE_CONFIG_DIR` yielding
  `Not logged in` even on macOS, where the token is in the Keychain, and seeding a minimal account
  file did not help. So these tests write into the operator's own `~/.claude` — exactly what the unit
  suite's autouse guard exists to prevent — and remove what they planted afterwards.
* **A cheap model.** These runs spend a real subscription, and the seam is what is under test, not
  the model's reasoning. `GRID_E2E_MODEL` overrides it.

Requires a logged-in Claude Code, and it costs money. Run:

    .venv/bin/python -m pytest tests/e2e_cross_repo/e2e_live_agent.py -q
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _harness as H  # noqa: E402

sys.path.insert(0, str(H.GRID_REPO))

_HERE = Path(__file__).resolve().parent
_MODEL = os.environ.get("GRID_E2E_MODEL", "claude-haiku-4-5-20251001")
_TASK_TIMEOUT_SECONDS = "300"
_SETTLE_TIMEOUT = 300.0


@pytest.fixture(scope="module")
def live_workspace_root():
    """ONE root shared by every provider in this module, and a SHORT one.

    Not a root each, unlike the fake-agent harness: the workspace path is a lockstep value precisely
    because the transcript directory's name is derived from it, so two providers that disagree about
    it cannot resume each other's conversation. A root each would quietly test the one arrangement
    the design forbids.

    ⚠️ **Not `tmp_path_factory`, and that is a finding rather than a preference.** Its roots look
    like `/private/var/folders/…/pytest-of-<user>/pytest-2204/live-workspaces0` — 96 characters
    before grid adds any of its own. Under ADR 0034 D-c the whole path then flattens to a
    232-character transcript directory name, and Claude Code stops using such a name verbatim past
    ~200: MEASURED, it keeps a 200-character prefix and appends a hash this repository cannot
    reproduce, so the provider's symlink is planted where nothing writes and the transcript never
    reaches the worktree. `task_agent.TRANSCRIPT_NAME_MAX_CHARS` carries the measurement and
    `link_transcript` now refuses outright — so with `tmp_path_factory` this module would fail every
    task on a real limitation that a real deployment (`/var/grid`, 135 characters flattened) never
    meets. A short root under `/private/tmp` is the production shape, which is what this module is
    for.
    """
    root = Path(tempfile.mkdtemp(prefix="grid-live-", dir="/private/tmp"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def spawn_live_provider(relay, provider_nodes, live_workspace_root, monkeypatch):
    """A provider running the REAL binary, with the operator's own config directory.

    Teardown sweeps that directory through `_harness.sweep_transcript_links` — see it for why the
    sweep reads each symlink's TARGET rather than deriving the names it expects to find.
    """
    if not shutil.which("claude"):
        pytest.skip("Claude Code is not on PATH; the live agent checks need the real binary")

    # `tests/conftest.py` sets `GRID_TASK_CLAUDE_CONFIG_DIR` for EVERY test, and this module is the
    # one place that must opt out — a custom config directory does not authenticate. Removing it here
    # rather than only from the child's environment is what keeps this process and the provider it
    # spawns agreed about which directory is in play: with the variable still set, the teardown below
    # sweeps a temp directory while the child plants its symlink in `~/.claude`, and the sweep reports
    # success having looked at the wrong place. That is not hypothetical — it is what the first
    # version of this fixture did, and it left links behind on every run.
    monkeypatch.delenv("GRID_TASK_CLAUDE_CONFIG_DIR", raising=False)

    started: list[H.Provider] = []

    def _spawn(label="A"):
        node_id, node_token = provider_nodes[label]
        env = {
            **os.environ,
            "GRID_REPO": str(H.GRID_REPO),
            "GRID_SIGNALING_URL": relay,
            "GRID_NODE_ID": node_id,
            "GRID_TOKEN": node_token,
            "GRID_RENEW_SECONDS": str(H.RENEW_SECONDS),
            "GRID_TASK_ROOT": str(live_workspace_root),
            "GRID_TASK_TIMEOUT_SECONDS": _TASK_TIMEOUT_SECONDS,
            # The cheap model, reaching the CHILD only: `child_env()` copies this process's
            # environment, and ADR 0028's rule is that nothing is exported to the provider's shell.
            "ANTHROPIC_MODEL": _MODEL,
        }
        # The child inherits no `GRID_TASK_CLAUDE_CONFIG_DIR` because the fixture removed it above —
        # a custom one does not authenticate (issue 01's spike), so the agent uses `~/.claude`.
        assert "GRID_TASK_CLAUDE_CONFIG_DIR" not in env
        proc = subprocess.Popen(
            [sys.executable, str(_HERE / "provider_process.py")],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        provider = H.Provider(proc, node_id)
        started.append(provider)
        return provider

    yield _spawn

    for provider in started:
        provider.stop()

    H.sweep_transcript_links(live_workspace_root)


def test_a_real_agent_run_reports_tool_activity_while_it_is_still_working(
        relay, owner_token, spawn_live_provider):
    """Issue 03's criteria against the real binary: live tool activity that names its target, a
    captured session id, and a terminal state derived from the child's exit."""
    from remote import relay as relay_client

    spawn_live_provider("A")
    task_id = H.create(
        relay, owner_token,
        "Create a file called greeting.txt whose entire contents are the single word HELLO. "
        "Create no other file and explain nothing.",
        project="live-tools")["id"]

    seen: list[dict] = []
    for _seq, payload in relay_client.stream_task_events(relay, owner_token, task_id, after_seq=-1):
        seen.append(payload)
        if payload.get("type") == "task.terminal":
            break

    done = H.await_state(relay, owner_token, task_id, {"completed", "failed"}, timeout=_SETTLE_TIMEOUT)
    assert done["state"] == "completed", f"{done!r}\nevents: {seen!r}"

    tool_uses = [p for p in seen if p.get("type") == "task.tool_use"]
    assert tool_uses, f"no tool activity arrived before the task ended: {seen!r}"
    assert any(p.get("path") for p in tool_uses), (
        f"a tool call arrived but never named the file it targeted: {tool_uses!r}")
    # These were read off a LIVE stream that stopped at the terminal event, so their presence here
    # ahead of it is their arrival while the task was still running — not a batch delivered at the end.
    assert seen.index(tool_uses[0]) < len(seen) - 1
    assert done.get("claude_session_id"), "the real binary's session id never reached the task row"


def test_a_second_conversation_does_not_recall_the_first(
        relay, owner_token, spawn_live_provider, live_workspace_root):
    """ADR 0034 D-c / issue 38, against the REAL binary — the only seam that can see this.

    ⚠️ **OVERTURNS `test_a_second_task_recalls_what_the_first_one_was_told`.** That test asserted
    the opposite and was right for its release: before D-c a member had one workspace per project,
    so their second task resumed their first task's session by construction. Since D-c the workspace
    is keyed by the conversation and every `POST /tasks` mints one, so two tasks are two
    conversations and the second starts fresh. That is the feature, stated as a person sees it: they
    open a second conversation and it does not remember the first.

    Only a live run can check it. The transcript directory's name is a contract with the Claude Code
    binary — issue 06's bug was the provider planting its symlink at an unresolved path while the
    binary wrote elsewhere, and every unit test agreed with it because each compared our own
    computation against itself. Two conversations that quietly shared one directory would look
    exactly like this test's setup and pass every assertion the fake agent can make.

    The token is only ever SAID, never written to a file, so nothing in the shared git history can
    carry it between the two — which is what makes the negative meaningful. The turns of one project
    DO share its files; they must not share its session.

    ⚠️ **What is deliberately not here: a second TURN of one conversation resuming its own session
    after its workspace is deleted and a different provider claims it.** Issue 38's acceptance
    criteria ask for it, and the route that posts a turn into an existing conversation is issue 47 —
    until it ships there is no way to reach a conversation twice, so the check has nothing to run
    against. It belongs with 47 and is recorded there rather than approximated here.
    """
    project = "live-two-conversations"
    spawn_live_provider("A")

    planted = H.create(
        relay, owner_token,
        "Remember this token for later: ZEBRA-4417. Reply with just the word OK. "
        "Create or modify no file.",
        project=project)
    first = H.await_state(
        relay, owner_token, planted["id"], {"completed", "failed"}, timeout=_SETTLE_TIMEOUT)
    assert first["state"] == "completed", first
    session = first.get("claude_session_id")
    assert session, "no session id was captured from the first conversation"

    asked = H.create(
        relay, owner_token,
        "What was the token I asked you to remember? If you were not told one in this conversation, "
        "reply with exactly NOTHING-WAS-SAID. Create or modify no file.",
        project=project)
    second = H.await_state(
        relay, owner_token, asked["id"], {"completed", "failed"}, timeout=_SETTLE_TIMEOUT)
    assert second["state"] == "completed", second

    # 1. It did not remember. The whole demo.
    assert "ZEBRA-4417" not in (second.get("result_text") or ""), (
        f"the second conversation recalled the first one's token, so the two are sharing a Claude "
        f"Code session — which means one workspace directory: {second!r}")

    # 2. And it was a fresh session rather than a failed resume. Without this, a provider that asked
    #    to resume a transcript it could not find would satisfy the assertion above while reporting
    #    a reset — the agent "forgetting" for the wrong reason, which is a bug wearing the feature's
    #    clothes.
    assert second.get("claude_session_id") != session, second
    assert not second.get("session_reset_reason"), (
        f"the second conversation was TOLD to resume something and could not — it should have been "
        f"asked to resume nothing at all: {second.get('session_reset_reason')!r}")

    # 3. Two workspaces on disk, each with its own transcript. DISCOVERED, not derived: the member
    #    key is `sha256(user_id)` truncated by the RELAY and the conversation id is the relay's
    #    uuid, so recomputing either here would make this test agree with a rule this repository
    #    does not own — the same reason `_harness.sweep_transcript_links` reads symlink targets
    #    instead of predicting their names.
    members = sorted((live_workspace_root / "projects" / first["project_id"]).iterdir())
    assert len(members) == 1, f"expected one member's directory, found {members!r}"
    conversations = sorted(p for p in members[0].iterdir() if (p / "workspace").is_dir())
    assert len(conversations) == 2, (
        f"expected one workspace per conversation under {members[0]}, found {conversations!r}")

    # 4. Issue 06's property, kept and now checked per conversation: the real binary wrote its
    #    transcript THROUGH our symlink and into the worktree. This is the assertion that fails if
    #    `transcript_dir_name` and the binary disagree about the deeper path, and it is silent
    #    through every other signal — the task completes, the session id comes back, the push lands.
    landed = {
        conversation.name: sorted(
            p.name for p in
            (conversation / "workspace" / ".grid" / "agent" / members[0].name).glob("*.jsonl"))
        for conversation in conversations
    }
    assert all(landed.values()), (
        f"a conversation's transcript never reached its worktree — issue 06's failure mode at the "
        f"depth ADR 0034 D-c added, and it is silent through every other signal. Found {landed!r}; "
        f"the first conversation's session was {session}, the second's "
        f"{second.get('claude_session_id')}. The provider's own Claude config directory holds: "
        + repr(sorted(p.name for p in (Path.home() / ".claude" / "projects").glob("*")
                      if members[0].name in p.name)))


def test_a_real_conversation_survives_losing_its_workspace_and_travels_on_its_side_ref(
        relay, owner_token, spawn_live_provider, live_workspace_root, tmp_path):
    """ADR 0034 D-j / issue 39 against the REAL binary, and the one check that costs money.

    Everything else about this slice is checked against `fake_claude.py`, which writes a one-line
    `.jsonl` it invented. What only the real binary can answer is whether a REAL transcript — tens of
    kilobytes of structured session state, possibly compacted — survives being packed onto a ref,
    losing its workspace entirely, and being restored at the same absolute path by a process that has
    never seen it. Issue 35 measured that the mechanism works in isolation (`measure_non_dev_design`:
    a compacted transcript resumed after a real push/fetch round trip). This is the same question
    asked of THIS code path.

    ⚠️ **Driven at `run_task` rather than through the relay, deliberately.** Every `POST /tasks`
    mints a fresh conversation and issue 47 owns the route that posts a second turn into an existing
    one — so a second turn of one conversation cannot be created over the wire at all yet. The claim
    payload IS `run_task`'s interface, so a hand-built job here is the real thing rather than a
    stand-in; what is faked is only the relay's decision about which conversation this turn belongs
    to, which is exactly the part issue 47 will supply. The relay's own half of the pin has its own
    tests in grid-src.

    The token is only ever SAID, never written to a file, so nothing in the git history can carry it
    across — and the negative control at the end proves a conversation never told it cannot produce
    it either.

    ⚠️ **MEASURED while mutation-testing this test, and it bounds what "a different provider" means
    here.** Deleting the workspace does NOT delete the operator's `~/.claude`, which this module
    deliberately uses because a custom config directory does not authenticate. With the `restore`
    removed from `materialize`, the agent reported `session_reset_reason` correctly — no transcript
    — and STILL answered with the token, because the workspace PATH is unchanged and the vendor keys
    its own storage on that path. So on this machine the token can reach a rebuilt workspace by a
    route that is not the side ref, and the load-bearing assertion is `session_reset_reason` rather
    than the token. A genuinely different machine has neither, which is what a two-host run would add
    and this one cannot.
    """
    from remote import task_agent, task_repo, tasks

    if not shutil.which("claude"):
        pytest.skip("Claude Code is not on PATH; the live agent checks need the real binary")
    # The fixture's own guard, needed here too: this test never spawns a provider process, so it
    # would otherwise run with `tests/conftest.py`'s non-authenticating config directory.
    spawn_live_provider  # noqa: B018 — requested for its `GRID_TASK_CLAUDE_CONFIG_DIR` removal

    conversation = "live-sideref-0001"
    monkey_root = live_workspace_root
    os.environ["GRID_TASK_ROOT"] = str(monkey_root)
    os.environ["GRID_TASK_TIMEOUT_SECONDS"] = _TASK_TIMEOUT_SECONDS
    os.environ["ANTHROPIC_MODEL"] = _MODEL

    member = "9f2b" * 8
    remote, base = _bare_repo_with_a_trunk(tmp_path)
    workspace = task_agent.workspace_for("live-proj", member, conversation)

    def job(task_id, branch, **extra):
        return {"task_id": task_id, "project_id": "live-proj", "member_key": member,
                "conversation_id": conversation, "branch": branch, "input_commit": base, **extra}

    # --- turn 1: plant a token in the conversation and nowhere else ------------------------
    first = tasks.run_task(
        job("L1", "task/L1",
            prompt="Remember this token for later: OKAPI-9931. Reply with just the word OK. "
                   "Create or modify no file."),
        remote=remote)
    assert first.state == "completed", first.error
    session = first.session_id
    assert session, "the real binary reported no session id"
    task_repo.commit_and_push(workspace, url=remote.url, token=remote.token,
                              branch="task/L1", message="task L1 (completed)")
    pinned = task_repo.push_transcript(
        workspace, url=remote.url, token=remote.token,
        ref=task_repo.transcript_ref(conversation))
    assert pinned, "the real transcript was never published to its side ref"

    # --- the workspace, gone. Only the ref carries the conversation now --------------------
    shutil.rmtree(workspace.parent)
    assert not workspace.exists()

    # --- turn 2: the same conversation, on a workspace that has to be rebuilt --------------
    _git_bare(remote.url, "branch", "task/L2", base)
    second = tasks.run_task(
        job("L2", "task/L2", prompt="What token did I ask you to remember? Reply with just it.",
            resume_session_id=session, transcript_commit=pinned),
        remote=remote)

    assert second.state == "completed", second.error
    assert not second.session_reset_reason, (
        f"the agent was told to resume a session it could not use, so the conversation was lost "
        f"rather than carried: {second.session_reset_reason}")
    assert "OKAPI-9931" in (second.output or ""), (
        f"a REAL transcript did not survive the round trip through {task_repo.transcript_ref(conversation)} "
        f"— the token was only ever spoken, so the side ref is the only way it could have "
        f"travelled. The agent said: {second.output!r}")

    # --- the negative control, and it is not optional -------------------------------------
    #
    # ⚠️ **The token assertion above is NOT known to be discriminating on its own, and this is what
    # says so.** Mutation-testing this test (deleting the `restore` from `materialize`) left the
    # agent still answering `OKAPI-9931` while `session_reset_reason` correctly reported no
    # transcript — so something other than the side ref can supply that answer, and a positive-only
    # test would have been reporting on the wrong thing. A third turn in a FRESH conversation, with
    # no pin and no session, is the row that tells the two apart: if it also knows the token, then
    # the check above proves nothing and the only load-bearing assertion here is the reset reason.
    control_conversation = "live-sideref-control"
    _git_bare(remote.url, "branch", "task/L3", base)
    control = tasks.run_task(
        {"task_id": "L3", "project_id": "live-proj", "member_key": member,
         "conversation_id": control_conversation, "branch": "task/L3", "input_commit": base,
         "prompt": "What token did I ask you to remember? Reply with just it."},
        remote=remote)

    assert control.state == "completed", control.error
    assert "OKAPI-9931" not in (control.output or ""), (
        f"a conversation that was never told the token still produced it, so the assertion above "
        f"is not evidence that the transcript travelled — it is evidence of nothing. The control "
        f"said: {control.output!r}")


def _bare_repo_with_a_trunk(tmp_path):
    """A bare repo standing in for the relay's, with one commit on `main`. Returns (remote, commit).

    Real git, and deliberately not through `task_repo`: a test that built its remote with the code
    under test would agree with itself just as happily if both were wrong.
    """
    from remote.task_repo import GitRemote

    seed = tmp_path / "seed"
    seed.mkdir()
    _git_bare(seed, "init", "--quiet", "-b", "main", ".")
    (seed / "a.txt").write_text("x\n")
    _git_bare(seed, "add", "-A")
    _git_bare(seed, "commit", "--quiet", "-m", "seed")
    commit = _git_bare(seed, "rev-parse", "HEAD").strip()
    bare = tmp_path / "origin.git"
    _git_bare(tmp_path, "clone", "--bare", "-q", str(seed), str(bare))
    _git_bare(bare, "branch", "task/L1", commit)
    return GitRemote(url=str(bare), token="tok"), commit


def _git_bare(cwd, *args):
    """git in a hermetic environment, the way every other real-git helper in this suite runs it."""
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "HOME": "/nonexistent", "GIT_AUTHOR_NAME": "grid", "GIT_AUTHOR_EMAIL": "grid@invalid",
           "GIT_COMMITTER_NAME": "grid", "GIT_COMMITTER_EMAIL": "grid@invalid"}
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          check=True, env=env).stdout
