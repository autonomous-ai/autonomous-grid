"""The provider's on-disk layout: one object store per (project, member), one worktree per
conversation (ADR 0034 D-c, issue 50).

Split out of `test_task_agent.py` for the reason that file was split out of `test_local_cli.py`:
this is a whole subsystem with its own real-git fixtures, and it is the one place the shape of the
directory tree is asserted rather than assumed.

⚠️ **Everything here runs against REAL git.** The layout question — does a linked worktree read
`info/exclude` from the common directory, do N of them fetch into one store without contention — is
a question about git's behaviour, and a fake would answer it with this repository's own beliefs.
Issue 35 measured those answers against git 2.54.0; these tests are what stops the code drifting
away from what was measured.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_MEMBER = "9f2b" * 8
_CONVERSATION = "6a1d0f5c-3b27-4e18-9c40-2f7ab5d1e900"
_OTHER_CONVERSATION = "b4e91c72-8d05-4a63-bf19-70c3e4a2d581"


def _git(cwd, *args, check=True):
    """Real git, with the provider's own hermetic environment."""
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=check,
        env={"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_NOSYSTEM": "1",
             "GIT_CONFIG_GLOBAL": os.devnull, "HOME": "/nonexistent",
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@invalid",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@invalid"})


def _deep_remote(tmp_path, branch, *, commits=25, blob_bytes=120_000):
    """A bare repo whose HISTORY is much larger than any one checkout. `(GitRemote, commit)`.

    The size relationship is the whole point, and it is the real one: issue 35 measured a 792 MiB
    history against a 305.8 MiB checkout. A repository with one small commit cannot tell a shared
    object store from a second copy of it — on a tiny repo the checkout outweighs the store, which
    is exactly why `tests/test_measure_non_dev_design.py` refuses to assert which side is cheaper.
    So the seed rewrites one incompressible blob `commits` times: the store carries every version,
    the working tree carries one.
    """
    from remote.task_repo import GitRemote

    seed = tmp_path / f"seed-{branch.replace('/', '-')}"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main", ".")
    for revision in range(commits):
        # `os.urandom`, so nothing here compresses into a pack a hundredth the size and quietly
        # turns the measurement below into noise.
        (seed / "big.bin").write_bytes(os.urandom(blob_bytes))
        _git(seed, "add", "-A")
        _git(seed, "commit", "-q", "-m", f"revision {revision}")
    _git(seed, "branch", "-f", branch)
    bare = tmp_path / "origin.git"
    _git(tmp_path, "clone", "--bare", "-q", str(seed), str(bare))
    return GitRemote(url=str(bare), token="tok"), _git(bare, "rev-parse", branch).stdout.strip()


def _turn_branch(remote, branch, commit):
    """A second turn's branch on the same remote, at the same commit.

    Every turn has its own `task/<turn_id>`, so two conversations of one member never want the same
    branch — which is what keeps two linked worktrees of one store off a single ref. Spelled out
    here rather than reusing one name for both, because a test that shared a branch would be
    asserting against a shape production cannot produce.
    """
    _git(remote.url, "branch", "-f", branch, commit)
    return branch


def _weigh(path: Path) -> int:
    """Apparent bytes under `path`.

    `os.walk`, never `Path.rglob` — rglob yields an unreadable directory and then silently omits
    everything beneath it, so a per-file `except OSError` reports a small number for a large tree.
    The same reasoning `tests/measure_non_dev/gitrun.tree_bytes` carries, and the same reason `du`
    is not used: it reports disk blocks, not apparent size.
    """
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            entry = Path(root) / name
            if entry.is_symlink():
                continue
            try:
                total += entry.stat().st_size
            except OSError:
                continue
    return total


def _materialize(workspace, remote, commit, branch):
    from remote import task_agent, task_repo

    task_agent.ensure_workspace(workspace)
    task_repo.materialize(
        workspace, url=remote.url, token=remote.token, branch=branch, input_commit=commit)


def test_a_second_conversation_of_one_member_adds_a_working_tree_not_a_second_object_store(
        tmp_path, short_task_root, monkeypatch):
    """ADR 0034 D-c's layout, asserted by weighing the directory rather than by reading the code.

    Before issue 50 `task_repo._ensure_repo` ran `git init` inside each workspace, so a member's
    second conversation fetched the project's whole history for itself — the +1,043.4 MiB per
    conversation issue 35 measured, against a worktree's +305.8 MiB.

    Two assertions, and neither alone is enough. The STRUCTURAL one (one store, `.git` is a file)
    could be satisfied by a layout that still copied the objects; the BYTES one could pass by
    accident on a repository whose history is small. Together they say what the criterion asks:
    the second conversation costs a working tree.
    """
    from remote import task_agent, task_worktree

    monkeypatch.setenv(task_agent.WORKSPACE_ROOT_ENV, str(short_task_root))
    remote, commit = _deep_remote(tmp_path, "task/T1")

    member_dir = short_task_root / "projects" / "proj-1" / _MEMBER
    first = task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION)
    _materialize(first, remote, commit, "task/T1")
    after_one = _weigh(member_dir)

    second = task_agent.workspace_for("proj-1", _MEMBER, _OTHER_CONVERSATION)
    _materialize(second, remote, commit, _turn_branch(remote, "task/T2", commit))
    added_by_the_second = _weigh(member_dir) - after_one

    # 1. ONE object store, and it is where `store_for` says it is.
    stores = sorted(p for p in member_dir.iterdir() if (p / "objects").is_dir())
    assert stores == [task_worktree.store_for(first)], (
        f"expected exactly one object store under {member_dir}, found {stores!r}")
    assert task_worktree.store_for(first) == task_worktree.store_for(second)

    # 2. Each workspace is a LINKED worktree — `.git` is a file holding `gitdir:`, not a directory.
    for workspace in (first, second):
        dot_git = workspace / ".git"
        assert dot_git.is_file(), (
            f"{dot_git} is not a linked worktree's gitdir pointer; this workspace has an object "
            f"store of its own")
        assert dot_git.read_text().startswith("gitdir:")

    # 3. And it cost a working tree. The store is the bulk of `after_one`; the second conversation
    #    must not have paid for it again.
    store_bytes = _weigh(task_worktree.store_for(first))
    assert added_by_the_second < store_bytes // 2, (
        f"the second conversation added {added_by_the_second} bytes against an object store of "
        f"{store_bytes}; it is paying for the project's history a second time")


def test_a_standalone_workspace_from_before_this_layout_is_converted_and_keeps_its_conversation(
        tmp_path, short_task_root, monkeypatch):
    """THE MIGRATION. Every conversation that has ever run a turn arrives in this shape.

    Before issue 50 `task_repo._ensure_repo` ran `git init` inside the workspace, so `.git` is a
    DIRECTORY holding a full copy of the project. There is no in-place conversion that is not a worse
    `worktree add`, so the directory is emptied and re-cut — and the one thing that must survive is
    `.grid/`, which ADR 0032's `clean -ffdx -e .grid` spares on every ordinary turn and which holds
    the conversation's transcript when its last publish failed.

    ⚠️ Written because a MUTATION found it missing: making `is_worktree_of` answer `True`
    unconditionally left the whole suite green. Every other test here starts from an empty directory,
    where the probe fails on "not a repository" and the create path runs anyway — so nothing was
    exercising the branch that tells a foreign repository from one of ours, which is the only branch
    the migration takes.
    """
    from remote import task_agent, task_repo, task_worktree

    monkeypatch.setenv(task_agent.WORKSPACE_ROOT_ENV, str(short_task_root))
    remote, commit = _deep_remote(tmp_path, "task/T1", commits=1, blob_bytes=64)
    workspace = task_agent.ensure_workspace(
        task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION))

    # The pre-issue-50 shape, built the way `_ensure_repo` built it.
    _git(workspace, "init", "--quiet", "--initial-branch=main", ".")
    (workspace / "leftover.txt").write_text("from the old layout\n")
    transcript = workspace / task_repo.RESERVED_DIR / task_repo.TRANSCRIPT_DIR / _MEMBER
    transcript.mkdir(parents=True)
    (transcript / "sess-1.jsonl").write_text("the conversation\n")
    assert (workspace / ".git").is_dir(), "this test did not build the shape it is about"

    _materialize(workspace, remote, commit, "task/T1")

    store = task_worktree.store_for(workspace)
    assert (workspace / ".git").is_file(), (
        "the standalone repository was not converted — this conversation still carries its own copy "
        "of the project's history, so the layout buys nothing for every project that predates it")
    assert task_worktree.is_worktree_of(store, workspace)
    assert (transcript / "sess-1.jsonl").read_text() == "the conversation\n", (
        "the migration destroyed the conversation's transcript; a turn whose publish had failed "
        "would lose it with nothing said")
    assert not (workspace / "leftover.txt").exists(), (
        "the old layout's working files survived into the new checkout")
    assert (workspace / "big.bin").is_file()


def test_two_conversations_can_be_cut_at_one_ref_and_the_store_gains_no_branch_of_its_own(
        tmp_path, short_task_root, monkeypatch):
    """What `--detach` actually buys, measured rather than assumed.

    ⚠️ The obvious reason for the flag is WRONG, and the first version of this test asserted it.
    Handed a full object id — which is what `materialize` passes — `worktree add` detaches on its
    own and invents nothing, so dropping `--detach` left every test green. The basename-derived
    branch people remember is what happens when no commit-ish is given at all.

    What the flag really guards is `commit` arriving as a **branch name**: without it git checks that
    branch out and then REFUSES a second worktree wanting the same one, so a caller passing `branch`
    where `input_commit` belongs would work for a member's first conversation and fail for every one
    after it — on this provider only, and looking like a corrupt store.

    So the property is asserted at `ensure_worktree`'s own door, with the input that provokes it.
    """
    from remote import task_agent, task_worktree

    monkeypatch.setenv(task_agent.WORKSPACE_ROOT_ENV, str(short_task_root))
    remote, commit = _deep_remote(tmp_path, "task/T1", commits=1, blob_bytes=64)
    first = task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION)
    _materialize(first, remote, commit, "task/T1")
    store = task_worktree.store_for(first)

    # The first workspace is ON `task/T1` — `materialize` puts it there with `symbolic-ref`, which
    # is what makes git's bookkeeping consider that branch checked out and makes this test bite.
    assert _git(first, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "task/T1"

    # A SECOND conversation cut at the ref the first one is sitting on, by NAME. This must succeed:
    # without `--detach` git refuses it outright, and only ever on a member who already has one.
    second = task_agent.ensure_workspace(
        task_agent.workspace_for("proj-1", _MEMBER, _OTHER_CONVERSATION))
    task_worktree.ensure_worktree(store, second, "task/T1")

    assert _git(second, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "HEAD", (
        "the new worktree took the branch rather than detaching, so the next conversation cut at "
        "the same ref is refused")
    assert (second / "big.bin").is_file()

    branches = sorted(
        line.strip() for line in
        _git(store, "branch", "--list", "--format=%(refname:short)").stdout.splitlines()
        if line.strip())
    assert branches == ["task/T1"], (
        f"the store holds a ref nobody fetched: {branches}")


def test_a_local_git_failure_setting_up_the_store_is_retryable_and_not_a_terminal_failure(
        tmp_path, short_task_root, monkeypatch):
    """This provider's disk is not the task's problem — the same rule the FETCH already gets.

    `materialize`'s own comment states it: "the fetch is the one step whose failure is about this
    attempt rather than about the task", and treating it like the rest "meant an imported history
    the relay could not pack in time failed every task in that project instantly, with nothing to
    retry it". `ensure_store` and `ensure_worktree` are new steps in the identical position —
    provider-local git housekeeping, before the agent runs, on a disk another provider does not
    share — and a plain `CheckoutError` from either would be reported by `tasks.run_task` as a
    TERMINAL failure, burning the turn on one machine's ENOSPC.

    A `worktree add` is also a full checkout of the whole repository, so it is far more exposed to a
    disk filling up than the `git init` it replaced. Found in review.
    """
    import pytest

    from remote import task_agent, task_repo, task_worktree

    monkeypatch.setenv(task_agent.WORKSPACE_ROOT_ENV, str(short_task_root))
    remote, commit = _deep_remote(tmp_path, "task/T1", commits=1, blob_bytes=64)
    workspace = task_agent.ensure_workspace(
        task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION))

    def wedged(*args, **kwargs):
        raise task_repo.CheckoutError("fatal: could not write config file: No space left on device")

    monkeypatch.setattr(task_worktree, "ensure_store", wedged)
    with pytest.raises(task_repo.InputFetchError):
        task_repo.materialize(workspace, url=remote.url, token=remote.token,
                              branch="task/T1", input_commit=commit)

    monkeypatch.undo()
    monkeypatch.setenv(task_agent.WORKSPACE_ROOT_ENV, str(short_task_root))
    monkeypatch.setattr(task_worktree, "ensure_worktree", wedged)
    with pytest.raises(task_repo.InputFetchError):
        task_repo.materialize(workspace, url=remote.url, token=remote.token,
                              branch="task/T1", input_commit=commit)


def _is_ignored(worktree: Path, name: str) -> bool:
    """Whether git ignores `name` inside `worktree`, told apart from a probe that could not run.

    ⚠️ **Exit 1 means "not ignored" and exit 128 means "this command did not run at all"**, and a
    truthiness check on the return code cannot tell them apart. MEASURED (issue 35): with
    `GIT_LITERAL_PATHSPECS=1` set, `git check-ignore` exits **128** with `pathspec magic not
    supported by this command: 'literal'` — with and without `--no-index`. `task_repo._env` does not
    set that variable, but the relay's does, and a copied helper that swallowed a 128 would report
    "not ignored" for both control rows below — which is exactly the reading the answer this test
    asserts is built from. So the probe runs git directly, and anything but 0 or 1 raises.
    """
    probe = subprocess.run(
        ["git", "check-ignore", "-v", "--no-index", name], cwd=str(worktree),
        capture_output=True, text=True,
        env={"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_NOSYSTEM": "1",
             "GIT_CONFIG_GLOBAL": os.devnull, "HOME": "/nonexistent"})
    if probe.returncode not in (0, 1):
        raise AssertionError(
            f"git check-ignore on {name} exited {probe.returncode}, so this probe measured "
            f"nothing: {(probe.stdout or probe.stderr).strip()}")
    return probe.returncode == 0


def test_the_exclude_that_keeps_grids_own_directory_out_is_read_from_the_common_directory(
        tmp_path, short_task_root, monkeypatch):
    """Issue 35 measurement 1a, asserted here rather than assumed by the code that depends on it.

    `ensure_store` writes ONE `info/exclude` for a member's whole store instead of one per
    workspace, and that is only correct because a linked worktree reads the file from the **common**
    directory. If a future git changed that, `.grid/` would stop being excluded, `commit_and_push`'s
    `git add -A` would commit the provider's own state into the team's repository, and every test in
    this suite would still pass.

    Both controls, for issue 35's reason: a probe that cannot run reports "not ignored" for both
    rows, and "not ignored in the per-worktree location" is precisely the reading the answer
    `common` is built from. So the positive control has to fire before the negative one means
    anything.
    """
    from remote import task_agent, task_repo, task_worktree

    monkeypatch.setenv(task_agent.WORKSPACE_ROOT_ENV, str(short_task_root))
    remote, commit = _deep_remote(tmp_path, "task/T1", commits=1, blob_bytes=32)
    workspace = task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION)
    _materialize(workspace, remote, commit, "task/T1")

    store = task_worktree.store_for(workspace)
    worktree_git_dir = Path(_git(
        workspace, "rev-parse", "--path-format=absolute", "--git-dir").stdout.strip())
    assert worktree_git_dir.resolve() != store.resolve(), (
        "this workspace is not a LINKED worktree, so there is no per-worktree location for the "
        "negative control and this test would prove nothing")

    # Two DIFFERENT patterns, so neither control row can be satisfied by the other's file.
    (worktree_git_dir / "info").mkdir(parents=True, exist_ok=True)
    (worktree_git_dir / "info" / "exclude").write_text("per-worktree-control.txt\n")
    (workspace / "per-worktree-control.txt").write_text("x\n")
    (workspace / task_repo.RESERVED_DIR).mkdir(exist_ok=True)
    (workspace / task_repo.RESERVED_DIR / "scratch.txt").write_text("x\n")

    # The POSITIVE control: what `ensure_store` wrote, in the common directory, takes effect.
    assert _is_ignored(workspace, f"{task_repo.RESERVED_DIR}/scratch.txt"), (
        f"the {task_repo.RESERVED_DIR}/ line `ensure_store` writes to the common directory is not "
        f"honoured inside a linked worktree — the provider's own state would be committed into the "
        f"requesting team's repository by `git add -A`")

    # The NEGATIVE control: a per-worktree exclude is NOT honoured, which is why there is one file.
    assert not _is_ignored(workspace, "per-worktree-control.txt"), (
        "a per-worktree info/exclude took effect, which contradicts what issue 35 measured; if git "
        "now honours both, the single common-directory file is no longer the whole rule")


def test_conversations_of_one_member_materialize_at_the_same_time_into_one_store(
        tmp_path, short_task_root, monkeypatch):
    """Issue 35 measurement 1c, exercised against the code rather than against a scratch repo.

    `GRID_MAX_TASKS` above 1 runs several turns at once, and since ADR 0034 D-b a member's
    conversations run side by side — so several `materialize` calls hit one object store
    concurrently. Issue 35 measured the FETCH half clean (4/4, zero lock collisions), which is why
    fetching is deliberately not serialized. What it did not measure is the rest of what
    `ensure_store` does, and `git config --local` takes `config.lock`: two of them at once is
    `could not lock config file`, a `CheckoutError`, and a turn failed by disk hygiene.

    Four conversations rather than two, because a two-thread race passes by luck often enough to
    look green. Each fetches its own branch onto its own ref, so the only thing they share is the
    store — which is the thing under test.
    """
    from remote import task_agent, task_worktree
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setenv(task_agent.WORKSPACE_ROOT_ENV, str(short_task_root))
    remote, commit = _deep_remote(tmp_path, "task/T0", commits=2, blob_bytes=2048)
    conversations = [f"{_CONVERSATION[:-1]}{digit}" for digit in "1234"]
    for index, _ in enumerate(conversations):
        _turn_branch(remote, f"task/T{index}", commit)

    def materialize(index):
        workspace = task_agent.workspace_for("proj-1", _MEMBER, conversations[index])
        _materialize(workspace, remote, commit, f"task/T{index}")
        return workspace

    with ThreadPoolExecutor(max_workers=len(conversations)) as pool:
        workspaces = list(pool.map(materialize, range(len(conversations))))

    # Every one of them ran, and every one of them is a worktree of the ONE store.
    store = task_worktree.store_for(workspaces[0])
    for workspace in workspaces:
        assert (workspace / "big.bin").is_file(), (
            f"{workspace} has no checkout, so its materialize lost a race rather than failing")
        assert task_worktree.is_worktree_of(store, workspace)
    registered = _git(store, "worktree", "list", "--porcelain").stdout
    for workspace in workspaces:
        assert str(workspace) in registered, (
            f"{workspace} is not registered in {store}'s worktree list — git and the filesystem "
            f"disagree, which is the state `worktree prune` exists to clean up")


def test_a_worktree_whose_directory_vanished_is_collected_and_the_conversation_runs_again(
        tmp_path, short_task_root, monkeypatch):
    """A provider killed mid-conversation leaves an admin entry with no directory (criterion 8).

    The half-written state is `<store>/worktrees/<id>/` naming a path that is not there — a machine
    that lost power between `worktree add`'s admin write and its checkout, or an operator who
    deleted a workspace by hand to reclaim disk. git will not `worktree add` over a registered
    entry, so without a prune that conversation fails on this provider for as long as the store
    survives: every turn, terminally, on a fault the person who typed the message cannot see.

    Nothing runs at start-up to collect these, deliberately. The next turn that touches this
    member's store is the moment the disk is about to be spent, and it is the only moment the
    collection is worth anything — a provider that never claims again is spending nothing.
    """
    from remote import task_agent, task_worktree

    monkeypatch.setenv(task_agent.WORKSPACE_ROOT_ENV, str(short_task_root))
    remote, commit = _deep_remote(tmp_path, "task/T1", commits=1, blob_bytes=64)
    workspace = task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION)
    _materialize(workspace, remote, commit, "task/T1")
    store = task_worktree.store_for(workspace)

    # The crash: the directory goes, the registration stays.
    shutil.rmtree(workspace)
    assert str(workspace) in _git(store, "worktree", "list", "--porcelain").stdout, (
        "git forgot this worktree on its own, so the state under test never existed")

    # The conversation's next turn.
    _materialize(workspace, remote, commit, _turn_branch(remote, "task/T2", commit))

    assert (workspace / "big.bin").is_file()
    assert task_worktree.is_worktree_of(store, workspace)
    prunable = [line for line in _git(store, "worktree", "list", "--porcelain").stdout.splitlines()
                if line.startswith("prunable")]
    assert not prunable, (
        f"{store} still holds a worktree nothing will collect: {prunable}")


@pytest.mark.parametrize("caller_creates_the_workspace", [True, False])
def test_a_conversation_set_aside_mid_migration_is_recovered_even_if_its_directory_went_too(
        tmp_path, short_task_root, monkeypatch, caller_creates_the_workspace):
    """The worst crash window inside the migration: the sideline holds the conversation, and the
    workspace directory it belongs to is gone.

    `ensure_worktree` moves `.grid/` to a sideline, empties the workspace, re-cuts it as a worktree
    and moves the reserved directory back. A provider killed between the emptying and the
    re-creation leaves a conversation's only local transcript sitting in a directory nothing else
    reads — and the next turn's `_set_reserved_aside` clears a stale sideline on its way past, so
    "not restored" and "destroyed" are the same outcome here.

    ⚠️ **Parametrized over whether the CALLER made the workspace directory, and that is the point
    of the test rather than a detail of it.** `_restore_reserved` used to answer this question by
    asking `workspace.is_dir()` and declining when it was False — so recovery worked only because
    `task_agent.ensure_workspace` happens to run before `materialize` on every path today. That is an
    implicit dependency between two modules with nothing asserting it: an edit that reordered them
    would lose one conversation per crash and break nothing else, on a path a suite that always
    creates the directory first can never reach.

    So `False` is the row that matters — it drives `task_repo.materialize` with no directory at all,
    which `ensure_worktree` is perfectly able to handle (it creates one itself before cutting the
    worktree) and which used to be exactly where the transcript went missing. `True` keeps the
    production shape covered, because the two take different branches.
    """
    from remote import task_agent, task_repo, task_worktree

    monkeypatch.setenv(task_agent.WORKSPACE_ROOT_ENV, str(short_task_root))
    remote, commit = _deep_remote(tmp_path, "task/T1", commits=1, blob_bytes=64)
    workspace = task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION)
    _materialize(workspace, remote, commit, "task/T1")

    # Hand-build the half-migrated state: the transcript on the sideline, no workspace at all.
    sideline = workspace.parent / f"{task_repo.RESERVED_DIR}.migrating"
    (sideline / task_repo.TRANSCRIPT_DIR / _MEMBER).mkdir(parents=True)
    (sideline / task_repo.TRANSCRIPT_DIR / _MEMBER / "sess-1.jsonl").write_text("remembered\n")
    task_worktree.remove_worktree(task_worktree.store_for(workspace), workspace)
    shutil.rmtree(workspace, ignore_errors=True)
    assert not workspace.exists() and sideline.is_dir()

    branch = _turn_branch(remote, "task/T2", commit)
    if caller_creates_the_workspace:
        task_agent.ensure_workspace(workspace)
    task_repo.materialize(workspace, url=remote.url, token=remote.token,
                          branch=branch, input_commit=commit)

    restored = workspace / task_repo.RESERVED_DIR / task_repo.TRANSCRIPT_DIR / _MEMBER
    assert (restored / "sess-1.jsonl").read_text() == "remembered\n", (
        "the conversation set aside mid-migration was not put back — a provider that died in that "
        "window destroys the transcript on the next turn instead of recovering it")
    assert not sideline.exists(), "the sideline survived, so the next turn would restore it again"
    assert (workspace / "big.bin").is_file(), "the turn itself did not get its checkout"


def test_another_conversations_orphaned_worktree_is_collected_by_the_next_turn_of_any_of_them(
        tmp_path, short_task_root, monkeypatch):
    """The half of criterion 8 that `ensure_worktree`'s own retry cannot reach.

    The retry prunes only because the worktree it is about to add is in the way, so it collects
    exactly one orphan — this conversation's. An orphan belonging to a DIFFERENT conversation of the
    same member (evicted, or deleted by an operator reclaiming disk) is nothing's problem: it is not
    in anybody's way, so no retry ever fires for it, and it sits in `<store>/worktrees/` for the life
    of the store while `git worktree list` reports a checkout that is not there.

    ⚠️ Written because a MUTATION found the gap: deleting `ensure_store`'s `worktree prune` left the
    whole suite green, since the sibling test above passes through the retry instead. A prune no test
    reaches is a prune that can be removed by accident.
    """
    from remote import task_agent, task_worktree

    monkeypatch.setenv(task_agent.WORKSPACE_ROOT_ENV, str(short_task_root))
    remote, commit = _deep_remote(tmp_path, "task/T1", commits=1, blob_bytes=64)
    abandoned = task_agent.workspace_for("proj-1", _MEMBER, _OTHER_CONVERSATION)
    _materialize(abandoned, remote, commit, "task/T1")
    store = task_worktree.store_for(abandoned)

    shutil.rmtree(abandoned)
    assert str(abandoned) in _git(store, "worktree", "list", "--porcelain").stdout

    # A turn of a DIFFERENT conversation, which never touches the abandoned one's path.
    live = task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION)
    _materialize(live, remote, commit, _turn_branch(remote, "task/T2", commit))

    assert str(abandoned) not in _git(store, "worktree", "list", "--porcelain").stdout, (
        f"{store} still registers {abandoned}, whose directory is gone — nothing else will ever "
        f"collect it, so it accumulates for the life of the store")
