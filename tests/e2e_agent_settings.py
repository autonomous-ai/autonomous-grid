"""The workspace does not get to configure the agent that reads it (ADR 0033 D-f, issue 22).

REAL Claude Code, driven through the real `run_task`. No unit test replaces this and none can: the
rule under test belongs to the vendor's binary, so a fake that answered it would be answering a
guess. This is the standing lesson of issue 06 — every unit test agreed with that bug, because each
compared our own computation against itself.

**Every test here carries a negative control.** Assert only that a marker file is absent and the
test passes whether or not the guard exists — the hook might not have fired for any of a dozen
unrelated reasons. So each check runs twice: once with the argv the provider builds, once with the
two flags stripped. The control has to FIRE for the guarded run's silence to mean anything.

What was measured while writing this (Claude Code **2.1.223**, macOS, 2026-08-06):

| # | run | result |
|---|---|---|
| 1 | project `.claude/settings.json` `SessionStart` hook, today's argv | the hook **ran** |
| 2 | the same, plus `--setting-sources user --strict-mcp-config` | it did **not** |
| 3 | `.mcp.json` naming a stdio server, control → guarded | server **started** → not started, and the init event's `mcp_servers` went from 17 entries to `[]` |
| 4 | `CLAUDE.md` + `.claude/agents/` + `.claude/skills/`, guarded | the model answered from `CLAUDE.md` — the instruction class still loads |
| 5 | a `SessionStart` hook in the *user* scope (`CLAUDE_CONFIG_DIR/settings.json`), guarded | it **ran** — `user` keeps the operator's own settings |

Requires a logged-in Claude Code, and it costs money — a handful of turns on the cheapest model.
Not collected by `pytest tests/` (the module is `e2e_*`, like `tests/e2e_doggi.py`). Run:

    .venv/bin/python -m pytest tests/e2e_agent_settings.py -q
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "e2e_cross_repo"))

import _harness as H  # noqa: E402

# The cheap model. What is under test is the binary's configuration loading, not its reasoning.
_MODEL = os.environ.get("GRID_E2E_MODEL", "claude-haiku-4-5-20251001")
# Issue 22's two flags, named once — the control strips exactly these and nothing else, so the two
# runs of a pair differ by the guard and by nothing else.
_STRICT_MCP = "--strict-mcp-config"
# Every OTHER flag in the provider's argv that is followed by a value. Listed so the control can copy
# each pair without inspecting the value — see `_strip_the_guard`. A flag added to `agent_argv` later
# and forgotten here is safe in the direction that matters: the control keeps it, and the pair still
# differs only by the guard.
_TAKES_A_VALUE = frozenset({"-p", "--output-format", "--permission-mode", "--resume"})


@pytest.fixture
def live(tmp_path, monkeypatch):
    """The operator's real config directory, a scratch workspace root, and a cleanup that runs.

    `tests/conftest.py` points `GRID_TASK_CLAUDE_CONFIG_DIR` at a temp directory for EVERY test, and
    this module is one of the two that must opt out: a custom config directory does not
    authenticate (issue 01's spike). Removing the variable rather than only unsetting it in the
    child's environment is what keeps this process and the binary agreed about which directory is in
    play — otherwise the sweep below tidies a temp directory while the agent plants its symlink in
    `~/.claude`.
    """
    if shutil.which("claude") is None:
        pytest.skip("Claude Code is not on PATH; this check needs the real binary")

    monkeypatch.delenv("GRID_TASK_CLAUDE_CONFIG_DIR", raising=False)
    root = tmp_path / "root"
    monkeypatch.setenv("GRID_TASK_ROOT", str(root))
    monkeypatch.setenv("GRID_TASK_TIMEOUT_SECONDS", "300")
    monkeypatch.delenv("GRID_TASK_PERMISSION_MODE", raising=False)
    # Reaches the CHILD only — `child_env()` copies this process's environment, and ADR 0028's rule
    # is that nothing grid sets is exported to the provider's own shell.
    monkeypatch.setenv("ANTHROPIC_MODEL", _MODEL)

    yield root

    H.sweep_transcript_links(root)


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        env={"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_NOSYSTEM": "1",
             "HOME": "/nonexistent", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@invalid",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@invalid"})
    return proc.stdout


def _remote_for(tmp_path: Path, branch: str, files: dict[str, str]):
    """A bare repo standing in for the relay's, and the branch tip. `(GitRemote, commit)`.

    The same shape as `tests/test_task_agent.py:_remote_for`, and the files are **committed** rather
    than written into the workspace: `materialize` runs `git clean -ffdx -e .grid`, so a planted
    untracked file is deleted before the agent ever starts. Committing them is also the honest
    reproduction — the hole this closes is reached through the git plane, not through the filesystem.

    Built here rather than imported from the unit suite: importing a 6000-line test module to borrow
    fifteen lines drags its fixtures and its collection along with it.
    """
    from remote.task_repo import GitRemote

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main", ".")
    for path, content in files.items():
        target = seed / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "input")
    _git(seed, "branch", "-f", branch)
    bare = tmp_path / "origin.git"
    _git(tmp_path, "clone", "--bare", "-q", str(seed), str(bare))
    return GitRemote(url=str(bare), token="tok"), _git(bare, "rev-parse", branch).strip()


def _hook_settings(marker: Path) -> str:
    """A `.claude/settings.json` whose `SessionStart` hook touches `marker`.

    `SessionStart` and not a tool hook on purpose: it fires before the model has produced anything,
    which is the precise claim — a shell command running on the provider with no permission prompt,
    as its own user, whatever the model goes on to decide.
    """
    return json.dumps({"hooks": {"SessionStart": [
        {"hooks": [{"type": "command", "command": f"/usr/bin/touch {marker}"}]}]}})


def _mcp_config(marker: Path) -> str:
    """An `.mcp.json` whose stdio server touches `marker` when it is started.

    It sleeps rather than exiting so a started server stays started; it never speaks MCP, and does
    not need to — the question is whether the provider's machine ran the command line at all.
    """
    return json.dumps({"mcpServers": {"probe": {
        "command": "/bin/sh", "args": ["-c", f"/usr/bin/touch {marker}; sleep 30"]}}})


def _run(tmp_path: Path, files: dict[str, str], prompt: str, *, guarded: bool, project: str):
    """One task, through the real `run_task`, against a workspace carrying `files`."""
    from remote import task_agent, tasks

    remote, commit = _remote_for(tmp_path, "task/T1", files)
    if not guarded:
        _strip_the_guard(task_agent)
    job = {"task_id": "T1", "project_id": project, "prompt": prompt, "attempt": 1,
           "input_commit": commit, "branch": "task/T1"}
    return tasks.run_task(job, remote=remote)


def _strip_the_guard(task_agent) -> None:
    """The NEGATIVE CONTROL: today's argv minus issue 22's flags, so the hook can fire.

    Patched at `agent_argv` rather than by hand-building an argv, so the control runs the product's
    own spawn path — `run_task` resolves the binary, checks out, links the transcript and reads the
    stream exactly as it does for the guarded run. The only difference between the two runs is the
    two flags, which is what makes the pair evidence rather than two separate experiments.

    Put back by the autouse `_restore_agent_argv` fixture. A rebinding of a module attribute that
    outlived its test would silently disarm every later one in the session.
    """
    original = task_agent.agent_argv

    def unguarded(binary, prompt, *, resume=None):
        argv = original(binary, prompt, resume=resume)
        out: list[str] = []
        i = 0
        while i < len(argv):
            item = argv[i]
            if item == "--setting-sources":
                i += 2            # the flag AND its value
                continue
            if item == _STRICT_MCP:
                i += 1
                continue
            if item in _TAKES_A_VALUE:
                # Copied as a pair without ever looking at the value. A prompt is arbitrary text
                # from another machine, and a prompt that happened to read `--strict-mcp-config`
                # would otherwise be deleted from the control's argv — leaving the two runs
                # differing by more than the guard, which is the one thing this must not do.
                out.extend(argv[i:i + 2])
                i += 2
                continue
            out.append(item)
            i += 1
        return out

    task_agent.agent_argv = unguarded


@pytest.fixture(autouse=True)
def _restore_agent_argv():
    """Put `agent_argv` back, whatever a control run did to it."""
    from remote import task_agent

    original = task_agent.agent_argv
    yield
    task_agent.agent_argv = original


def test_a_settings_hook_in_the_workspace_does_not_run(live, tmp_path):
    """The whole issue, as a side effect: no marker file, so no shell command ran.

    Asserted on the hook's own effect and never on the argv — a test that read `agent_argv` back
    would pass with the flag reaching nothing, and "the flag is in the list" is not the property
    anybody cares about.
    """
    marker = tmp_path / "HOOK_RAN"

    outcome = _run(
        tmp_path, {".claude/settings.json": _hook_settings(marker), "a.txt": "x\n"},
        "Reply with exactly OK. Create no file.", guarded=True, project="guarded")

    assert not marker.exists(), (
        "a SessionStart hook committed to the task's own branch executed on the provider — this is "
        "arbitrary code execution before the model said anything (ADR 0033 D-f)")
    assert outcome.state == "completed", outcome.error


def test_the_control_proves_the_hook_would_otherwise_run(live, tmp_path):
    """Strip the two flags and the marker appears. Without this, the test above is decoration.

    If this one ever fails, the test above has stopped being evidence — the vendor changed something
    and the pair has to be re-measured, not deleted.
    """
    marker = tmp_path / "HOOK_RAN"

    _run(tmp_path, {".claude/settings.json": _hook_settings(marker), "a.txt": "x\n"},
         "Reply with exactly OK. Create no file.", guarded=False, project="control")

    assert marker.exists(), (
        "the control did not fire, so this pair proves nothing: either the vendor stopped running "
        "project hooks by itself, or the hook never had a chance to run for an unrelated reason")


def test_an_mcp_server_in_the_workspace_is_not_started(live, tmp_path):
    """An MCP stdio entry is a command line, and the server is started at session start."""
    marker = tmp_path / "MCP_STARTED"

    outcome = _run(
        tmp_path, {".mcp.json": _mcp_config(marker), "a.txt": "x\n"},
        "Reply with exactly OK. Create no file.", guarded=True, project="guarded-mcp")

    assert not marker.exists(), (
        "a server named by the workspace's own .mcp.json was started on the provider")
    assert outcome.state == "completed", outcome.error


def test_the_control_proves_the_mcp_server_would_otherwise_start(live, tmp_path):
    """The other half of the pair — measured to fire, so its silence above means something."""
    marker = tmp_path / "MCP_STARTED"

    _run(tmp_path, {".mcp.json": _mcp_config(marker), "a.txt": "x\n"},
         "Reply with exactly OK. Create no file.", guarded=False, project="control-mcp")

    assert marker.exists(), (
        "the control did not start the server, so the guarded run proves nothing here")


def test_the_instruction_class_still_reaches_the_model(live, tmp_path):
    """`CLAUDE.md`, `.claude/agents/` and `.claude/skills/` are NOT blocked, and must not become so.

    The fix is deliberately narrow — no shell command before the model has said anything — and the
    obvious over-correction is to stop loading the repository's instructions too. That would break
    every real repository that uses Claude Code, so it is pinned here: the workspace's `CLAUDE.md`
    reaches the model, proved by asking for something only it knows.

    The agent and skill files are present for the same reason: `--setting-sources` is a settings
    flag, and a future change that reached further would fail this test rather than ship quietly.
    """
    outcome = _run(
        tmp_path,
        {"CLAUDE.md": "# Project rules\n\nThe project codename is ZEBRA-9911. When asked for the "
                      "codename, reply with it and nothing else.\n",
         ".claude/agents/reviewer.md":
             "---\nname: reviewer\ndescription: Reviews code\n---\n\nYou review code.\n",
         ".claude/skills/greet/SKILL.md":
             "---\nname: greet\ndescription: Greets\n---\n\nSay hello.\n"},
        "What is the project codename? Reply with just the codename. Create no file.",
        guarded=True, project="instructions")

    assert outcome.state == "completed", outcome.error
    assert "ZEBRA-9911" in (outcome.output or ""), (
        f"the workspace's CLAUDE.md never reached the model — the instruction class has been "
        f"blocked along with the execution class: {outcome.output!r}")


def test_a_flag_the_binary_does_not_know_fails_the_task_instead_of_running_unprotected(
        live, tmp_path):
    """The rollout note's load-bearing assumption, measured instead of asserted.

    The flags are added unconditionally, which is only safe because a binary that does not know one
    **refuses** rather than ignoring it and running unprotected. Ignoring would be the worst outcome
    available: every task on that provider would report `completed` while the hole stayed open, and
    no signal anywhere would say so.

    An unknown flag stands in for an old binary, because 2.1.221, 2.1.222 and 2.1.223 — every
    version on this machine — all know both real flags, so none of them can play the part. What is
    actually under test is the argv parser's disposition, and it is the same parser either way.

    Free: the binary exits during argv parsing, before any model call. Measured on 2.1.223:
    `error: unknown option '--not-a-real-flag'`, exit 1, and the hook never ran.
    """
    from remote import task_agent

    marker = tmp_path / "HOOK_RAN"
    original = task_agent.agent_argv
    task_agent.agent_argv = lambda binary, prompt, *, resume=None: (
        original(binary, prompt, resume=resume) + ["--not-a-real-flag"])

    outcome = _run(tmp_path, {".claude/settings.json": _hook_settings(marker), "a.txt": "x\n"},
                   "Reply with exactly OK.", guarded=True, project="unknown-flag")

    assert outcome.state == "failed", (
        f"the binary ACCEPTED an argv it does not understand — an old Claude Code would then run "
        f"without issue 22's flags and report success: {outcome!r}")
    assert "unknown option" in (outcome.error or ""), (
        f"the failure did not name the flag, so an operator cannot act on it: {outcome.error!r}")
    assert not marker.exists(), "the binary ran the workspace's hook before rejecting the argv"


def test_the_providers_own_settings_still_apply(live, tmp_path, monkeypatch):
    """`user`, not `none`: the operator's own `CLAUDE_CONFIG_DIR` keeps configuring their agent.

    Run against a temp config directory, which does NOT authenticate (issue 01's spike) — so the
    task fails, and that is fine: the claim is about which settings files were loaded, and the hook
    fires at session start, before the credential is needed. Measured on 2.1.223: the marker appears
    and the run then exits 1.

    Without this, `--setting-sources none` would pass every other test in this module while silently
    changing how every task on that provider behaves.
    """
    config = tmp_path / "provider-config"
    config.mkdir()
    marker = tmp_path / "USER_HOOK_RAN"
    (config / "settings.json").write_text(_hook_settings(marker), encoding="utf-8")
    monkeypatch.setenv("GRID_TASK_CLAUDE_CONFIG_DIR", str(config))

    _run(tmp_path, {"a.txt": "x\n"}, "Reply with exactly OK. Create no file.",
         guarded=True, project="user-scope")

    assert marker.exists(), (
        "a SessionStart hook in the provider's OWN config directory did not run — the argv is "
        "dropping user-scope settings, which is not what issue 22 asked for")
