"""Suite-wide safety nets.

Everything here exists to stop a test reaching something real. Nothing here sets up a feature.
"""
import sys

import pytest


@pytest.fixture(autouse=True)
def _claude_config_dir_is_never_the_real_one(monkeypatch, tmp_path_factory, request):
    """No test may plant its transcript symlink in the developer's own `~/.claude`.

    `task_agent.claude_config_dir()` falls back to `~/.claude` when the operator has fixed no
    directory — correct in production, and catastrophic in a suite: `run_task` links
    `<config>/projects/<encoded cwd>` for every task it runs, so an unguarded suite leaves one
    dangling symlink per test in the developer's real Claude configuration. That is not
    hypothetical; it is what this fixture was written in response to, after a full run deposited
    486 of them.

    Autouse and directory-wide, like grid-src's `task_repo_root` guard, because the failure is
    silent: the tests still pass, and nothing in their output mentions the directory they wrote to.
    A test that sets the variable itself — the cross-provider one switches between two providers'
    config directories — keeps its own value, since `monkeypatch.setenv` runs after this.
    """
    monkeypatch.setenv(
        "GRID_TASK_CLAUDE_CONFIG_DIR",
        str(tmp_path_factory.mktemp(f"claude-config-{request.node.name[:40]}")))


@pytest.fixture(autouse=True)
def _no_test_leaves_a_project_workspace_reserved():
    """`tasks._WORKSPACES_IN_USE` is process-global, and a leak from one test breaks OTHERS.

    A reservation held past a test makes `_reserve_workspace` refuse that project for the rest of the
    session — and refusing is deliberately quiet on the wire (no terminal report, the lease lapses),
    so the symptom is unrelated tests failing on a missing report with nothing pointing back here.
    Almost every task test uses the same default `project_id`, which is exactly the collision that
    would spread.

    So the check fails in the test that leaked, and clears the set so one failure does not become
    fifty. Keyed off `sys.modules` rather than importing: a suite that never touched `remote.tasks`
    pays nothing.
    """
    yield
    tasks = sys.modules.get("remote.tasks")
    if tasks is None:
        return
    leaked = set(tasks._WORKSPACES_IN_USE)
    tasks._WORKSPACES_IN_USE.clear()
    assert not leaked, (
        f"this test left {sorted(leaked)} reserved in tasks._WORKSPACES_IN_USE — a supervisor "
        f"thread it started is still running, or a release was skipped")
