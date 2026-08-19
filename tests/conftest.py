"""Suite-wide safety nets.

Everything here exists to stop a test reaching something real. Nothing here sets up a feature.
"""
import shutil
import sys
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def short_task_root():
    """A `GRID_TASK_ROOT` short enough to be the shape a real provider has.

    Not a safety net and the one thing in this file that is not — it is here because more than one
    suite needs it and two copies would drift.

    `tmp_path` is about **96** characters (`/private/var/folders/…/pytest-of-<user>/pytest-N/
    <test-name>0`). ADR 0034 D-c's workspace adds **126** more, and the whole path is then flattened
    into ONE directory name by `task_agent.transcript_dir_name`. Claude Code stops using such a name
    verbatim past ~200 — MEASURED, it keeps a prefix and appends a hash nothing here can reproduce —
    so `link_transcript` refuses, and every test that spawns an agent would fail on a limit no real
    deployment meets. `/var/grid` flattens to **135**.

    Resolved, so the path is already its own realpath: on macOS `/tmp` is a symlink to
    `/private/tmp`, and a root that resolves elsewhere is the issue-06 trap this suite has met once.

    ⚠️ **`/tmp` then `.resolve()`, never `/private/tmp` directly.** `/private` exists only on macOS,
    so naming it made every agent-spawning test a `FileNotFoundError` on Linux — 125 errors, on a
    suite that is green on the developer's own machine. `resolve()` gives the identical
    `/private/tmp/gt-…` on macOS and leaves `/tmp/gt-…` on Linux, so both are short and both are
    their own realpath.
    """
    root = Path(tempfile.mkdtemp(prefix="gt-", dir="/tmp")).resolve()
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


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
def _no_test_reads_a_real_process_environment(monkeypatch):
    """No test may decide anything from a **real** process's environment.

    `orphan_sweep` spares a match it can prove runs from a different `GRID_HOME`, reading
    `/proc/<pid>/environ` through `shared.process_home.home_of`. Every sweep test feeds a fabricated
    process table, so its pids are made-up numbers — and on Linux a made-up number often names a
    real, live, unrelated process whose home is not the `tmp_path` the test set. That match would be
    spared and the test would fail on a coincidence of the machine it ran on, which is the flake
    class this file exists to prevent.

    Answering "unknown" is also the *truthful* answer for a pid that names nothing, so this narrows
    nothing the suite is entitled to see. A test that means to exercise the check patches `home_of`
    itself; `monkeypatch.setattr` in the test runs after this fixture, so its value wins. The real
    reader keeps its own positive control in `tests/test_process_home.py`, which calls the
    underlying `_read_environ` and is therefore untouched by this.
    """
    from shared import process_home

    monkeypatch.setattr(process_home, "home_of", lambda pid: None)


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
