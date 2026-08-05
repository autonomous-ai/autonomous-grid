"""Suite-wide safety nets.

Everything here exists to stop a test reaching something real. Nothing here sets up a feature.
"""
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
