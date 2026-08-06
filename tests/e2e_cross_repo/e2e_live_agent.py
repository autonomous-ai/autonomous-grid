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
def live_workspace_root(tmp_path_factory):
    """ONE root shared by every provider in this module.

    Not a root each, unlike the fake-agent harness: the workspace path is a lockstep value precisely
    because the transcript directory's name is derived from it, so two providers that disagree about
    it cannot resume each other's conversation. A root each would quietly test the one arrangement
    the design forbids.
    """
    return Path(str(tmp_path_factory.mktemp("live-workspaces")))


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


def test_a_second_task_recalls_what_the_first_one_was_told(
        relay, owner_token, spawn_live_provider, live_workspace_root):
    """Issue 06's live check, scripted: the conversation travels through the repository.

    The workspace is DELETED between the two tasks and the second one runs under a different provider
    identity, so nothing left on disk can explain a pass — the token is never written to a file, only
    said. What carries it is the transcript, committed by the ordinary result commit, fast-forwarded
    onto `main`, and checked out again by whoever claims next.
    """
    project = "live-resume"
    first_provider = spawn_live_provider("A")

    planted = H.create(
        relay, owner_token,
        "Remember this token for later: ZEBRA-4417. Reply with just the word OK. "
        "Create or modify no file.",
        project=project)
    first = H.await_state(
        relay, owner_token, planted["id"], {"completed", "failed"}, timeout=_SETTLE_TIMEOUT)
    assert first["state"] == "completed", first
    session = first.get("claude_session_id")
    assert session, "no session id was captured from the first task"

    first_provider.stop()

    workspace = live_workspace_root / "projects" / first["project_id"] / "workspace"
    assert sorted((workspace / ".grid" / "agent").glob("*.jsonl")), (
        f"the agent's transcript never landed in {workspace / '.grid' / 'agent'} — this is issue "
        f"06's failure mode, and it is silent through every other signal")
    shutil.rmtree(workspace)

    spawn_live_provider("B")
    asked = H.create(
        relay, owner_token,
        "What was the token I asked you to remember? Reply with just the token and nothing else. "
        "Create or modify no file.",
        project=project)
    second = H.await_state(
        relay, owner_token, asked["id"], {"completed", "failed"}, timeout=_SETTLE_TIMEOUT)

    assert second["state"] == "completed", second
    assert "ZEBRA-4417" in (second.get("result_text") or ""), (
        f"the second task did not recall the token — the conversation did not travel: {second!r}")
    assert second.get("claude_session_id") == session, (
        f"the session id changed across a resume ({session} -> {second.get('claude_session_id')}) — "
        f"a resume appends to the same transcript and keeps its id")
    assert not second.get("session_reset_reason"), (
        f"the second task started a FRESH session instead of resuming: "
        f"{second.get('session_reset_reason')!r}")
