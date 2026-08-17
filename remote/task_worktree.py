"""One object store per (project, member), one `git worktree` per conversation (ADR 0034 D-c).

`task_agent` answers "run *what*, *where*" and `task_repo` answers "against *what input*". This one
answers **"on top of what"** — the shape of the directory tree a provider spends its disk on.

Until issue 50 `task_repo._ensure_repo` ran `git init` inside every workspace, so a member's second
conversation fetched the project's whole history for itself. MEASURED (issue 35, git 2.54.0, on a
792 MiB / 34,159-commit repository): the Nth clone costs **+1,043.4 MiB**, the Nth worktree
**+305.8 MiB** — three conversations are 3.28 GB against 1.62 GB, so sharing is **3.4× cheaper for
every conversation after the first**. The layout here is what collects that.

Three properties of it are load-bearing, and all three were measured rather than assumed:

  * **`info/exclude` is COMMON-directory.** A pattern written to `<store>/info/exclude` is honoured
    inside every linked worktree; the same pattern in `<store>/worktrees/<n>/info/exclude` is not.
    So the one uniform `/{RESERVED_DIR}/` line ADR 0034 D-j requires really is ONE file, written
    here rather than per workspace. `tests/test_task_worktree.py` carries both controls.
  * **Concurrent fetches into one store do not contend** — 4/4 succeeded, zero lock collisions,
    0.171–0.173 s. So a fetch is deliberately NOT serialized; only the structural mutations are.
  * **A worktree is safe to evict.** git tracks its own worktrees, so `worktree remove` and
    `worktree prune` are exact. This is the whole reason object *alternates* were rejected: git
    tracks no borrowers, so removing a store would silently corrupt every workspace pointing at it.

⚠️ **The agent's sandbox is widened to reach this store, and that is a decision rather than an
oversight.** A linked worktree's `.git` is a *file* pointing into `<store>/worktrees/<id>/`, so
`git status` refreshing the index, `git commit` writing loose objects, and issue 15's merge turn
running `git add`/`git rm` all write here — outside the workspace. `task_sandbox.policy` therefore
grants the store on both axes, beside the cache tree.

⚠️ **What that grant exposes is more than "one member's own history", and the narrower phrasing was
wrong.** The store is a bare common directory: it holds `refs/heads/task/<turn_id>` for every one of
that member's conversations AND `<store>/worktrees/<conversation_id>/{HEAD,index,…}` — the *live*
administrative state of the sibling conversations running right now, because a linked worktree keeps
those in the common dir rather than in the working tree. So one conversation's agent can reach
another's ref or index while that turn is in flight (`git rev-parse --git-common-dir` from its own
workspace hands it the path; no guessing). It cannot reach a colleague — a store is per member and
`tests/test_task_sandbox.py` pins that — and the member directory holding the sibling *working
trees* is deliberately not granted. But "same member, different conversation, currently running" is
inside the blast radius, and it was not before this slice, when each conversation had its own
repository. Named here rather than discovered later; closing it means not sharing, which is the
feature. Found in review.
"""
from __future__ import annotations

import shutil
import threading
from pathlib import Path

# The shared object store's directory name, a SIBLING of the conversation directories rather than a
# level of its own — `<root>/projects/<project_id>/<member_key>/store.git`.
#
# A sibling because the member directory is what the store belongs to: one project, one member, one
# history. A `.git` suffix because that is what the thing is, and because it reads as a repository
# to anybody who ships into the tree to look at a failed task.
#
# ⚠️ `task_agent._SAFE_PROJECT_ID` admits a dot, so a conversation id spelled exactly `store.git`
# would land on top of this. `task_agent._safe_segment` refuses that name for exactly this reason —
# the two are one rule and must not drift.
STORE_DIR_NAME = "store.git"


def store_for(workspace: Path) -> Path:
    """The object store `workspace`'s worktree is cut from.

    Derived from the workspace path in ONE place, the same discipline `task_sandbox.cache_dir` sets
    for the sibling cache tree: three consumers must agree exactly — this module cuts worktrees from
    it, `task_sandbox.policy` grants it to the agent, and `task_evict` removes it. Two spellings
    would grant one directory and use another, and the symptom would be an agent whose every `git`
    call fails on a policy that looks complete.

    `workspace` is `<…>/<member_key>/<conversation_id>/workspace`, so the member directory is two
    levels up.
    """
    return workspace.parent.parent / STORE_DIR_NAME


# One lock per object store, so the STRUCTURAL mutations of one member's store are serialized while
# a member's conversations run side by side (ADR 0034 D-b).
#
# ⚠️ **This is not belt-and-braces, and issue 35's measurement does not cover it.** What was measured
# is the FETCH — 4/4 concurrent fetches into one object store, zero lock collisions — which is why
# fetching is deliberately left outside this lock. Everything `ensure_store` does is a different
# question, and it was measured here: four concurrent `materialize` calls fail on git 2.54.0 with
# `fatal: cannot copy '…/templates/hooks/pre-commit.sample' to '…/store.git/hooks/…': File exists`
# from `git init` racing itself, and `git config --local` takes `config.lock` behind it. The symptom
# is a `CheckoutError` from disk housekeeping, on a turn that had nothing wrong with it.
#
# A dict of locks needs its own lock to grow safely — the same reasoning `tasks._WORKSPACES_LOCK`
# states for its set, arriving at a dict because the thing being serialized is per store rather than
# global: two MEMBERS' stores are different directories and must not wait on each other.
#
# **Within-process**, like `tasks._WORKSPACES_IN_USE` and the relay's own per-project locks. Two
# `grid join` processes sharing one `GRID_TASK_ROOT` do not see each other's — which is already true
# of the workspace reservation, and is the reason a provider is one process per root.
_STORE_LOCKS: dict[str, threading.Lock] = {}
_STORE_LOCKS_GUARD = threading.Lock()


def store_lock(store: Path) -> threading.Lock:
    """The lock for one store, created on first use.

    Keyed on the RESOLVED path string: two spellings of one directory that took two locks would
    serialize nothing, which is the failure mode this exists to remove.

    Public because `task_evict` needs it: DELETING a store is a structural mutation of it, and the
    three in this module would be serialized against each other while the one that removes the whole
    thing raced them. Found in review.
    """
    key = str(store.resolve(strict=False))
    with _STORE_LOCKS_GUARD:
        lock = _STORE_LOCKS.get(key)
        if lock is None:
            lock = _STORE_LOCKS[key] = threading.Lock()
        return lock


def _repo():
    """`task_repo`, imported lazily to break a cycle that would only surface at the loader.

    `task_repo.materialize` imports THIS module at module scope, so a top-level import back would
    close a ring — an ImportError on a live provider rather than in any test that imports one module
    first. The same shape `tasks._tree_beat` uses for `task_tree`, and the same reason
    `task_sandbox` names `GRID_TASK_ROOT` instead of importing `task_agent` for it.
    """
    from . import task_repo

    return task_repo


def _dirs():
    """`task_agent`, lazily, for `ensure_workspace` — see `_repo`."""
    from . import task_agent

    return task_agent


def ensure_store(store: Path) -> Path:
    """Create the object store if it is absent, and re-assert what must be true of it every time.

    The exclude and the safety config are rewritten on EVERY call rather than only at init, for the
    reason `_ensure_repo` gave when it owned them: they are what keeps the provider's own state out
    of the requesting user's repository, and a file only written at init goes missing the first time
    the tree is restored from anywhere else.

    `worktree prune` runs here too, which is what makes criterion 8 hold: a provider killed
    mid-conversation leaves a `<store>/worktrees/<id>` entry whose directory may be gone, and the
    next turn that touches this member's store collects it. Nothing has to run at start-up, and
    nothing accumulates that only a restart would clear.
    """
    task_repo, task_agent = _repo(), _dirs()
    task_agent.ensure_workspace(store)
    with store_lock(store):
        if not (store / "objects").is_dir():
            task_repo._run(store, "init", "--bare", "--quiet", ".")
        # THE COMMON DIRECTORY. Measured (issue 35): this is the file every linked worktree reads,
        # and a copy under `<store>/worktrees/<id>/info/exclude` is not honoured at all.
        exclude = store / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(
            f"# written by grid — see ADR 0032\n/{task_repo.RESERVED_DIR}/\n")
        for key, value in task_repo.GIT_SAFETY_CONFIG:
            task_repo._run(store, "config", "--local", key, value)
        task_repo._run(store, "worktree", "prune")
    return store


def is_worktree_of(store: Path, workspace: Path) -> bool:
    """Whether `workspace` is already a linked worktree of `store`.

    Asked of GIT rather than of the filesystem. `(workspace / ".git").is_file()` is the shape of the
    answer, not the answer: it is equally true of a worktree linked to a store this provider has
    since evicted, whose gitdir pointer now names a directory that is not there.

    ⚠️ **A `False` here can mean "git could not answer" rather than "not a worktree"** — a timeout, a
    missing binary, a corrupt store — and `ensure_worktree` responds to it by removing the directory
    and cutting it again. Traced, and the answer is that nothing is lost: `.grid/` is carried across,
    and the working tree is rebuilt at the same `input_commit` the caller was about to `reset --hard`
    to anyway. What a transient fault costs is a full checkout where a reset would have done, and
    then `_add_worktree` fails too and the turn ends with git's own words. Fail-toward-rebuilding is
    the right direction here precisely because a workspace is disposable; anywhere it is not, this
    function is the wrong thing to ask.
    """
    task_repo = _repo()
    try:
        common = task_repo._run(
            workspace, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    except task_repo.CheckoutError:
        return False
    if not common:
        return False
    return Path(common).resolve(strict=False) == store.resolve(strict=False)


def ensure_worktree(store: Path, workspace: Path, commit: str) -> Path:
    """Make `workspace` a linked worktree of `store`, checked out at `commit`.

    Idempotent, and cheap on the ordinary path: a conversation's second and later turns find their
    worktree already there and this returns without running git at all beyond the probe.

    ⚠️ **This is also the MIGRATION**, and it is the one destructive step in the module. Every
    conversation that ran a turn before issue 50 has a standalone repository at this path — `.git` a
    directory, with a full copy of the project's history in it. There is no in-place conversion that
    is not a worse version of `worktree add`, so the directory is emptied and re-cut. MEASURED on git
    2.54.0: `worktree add` over a path that already exists and holds anything at all is
    `fatal: '<path>' already exists` (exit 128); over an EMPTY existing directory it succeeds.

    ⚠️ **`.grid/` is carried across, not wiped**, which is why this is not a plain `rmtree`. That
    directory is the one thing in a workspace that is the provider's rather than the task's — ADR
    0032's `clean -ffdx -e .grid` spares it on every turn, and a migration that did not would destroy
    a conversation whose last publish failed on the one turn that had no chance to notice.

    The retry after a prune is not defensive padding: a provider killed between `worktree add`'s
    admin write and its checkout leaves an entry git refuses to add over, and without the prune that
    conversation would fail on this provider for as long as the directory survives.
    """
    task_repo = _repo()
    _restore_reserved(workspace)
    if is_worktree_of(store, workspace):
        return workspace
    # Under the store's lock from here: `worktree add` and `worktree prune` both write
    # `<store>/worktrees/`, and a prune that lands between a sibling's admin write and its checkout
    # collects the entry that sibling is still building.
    with store_lock(store):
        # Asked AGAIN inside the lock. Two turns of one conversation never run at once (ADR 0034
        # D-b), but a re-cut is not the only thing this races with, and re-checking is one probe
        # against re-running a whole checkout.
        if is_worktree_of(store, workspace):
            return workspace
        reserved = _set_reserved_aside(workspace)
        if workspace.exists():
            shutil.rmtree(workspace)
        _dirs().ensure_workspace(workspace)
        try:
            _add_worktree(store, workspace, commit)
        except task_repo.CheckoutError:
            task_repo._run(store, "worktree", "prune")
            _add_worktree(store, workspace, commit)
        if reserved.is_dir():
            reserved.rename(workspace / task_repo.RESERVED_DIR)
    return workspace


def _reserved_sideline(workspace: Path) -> Path:
    """Where `.grid/` waits while its workspace is re-cut as a worktree.

    A SIBLING of the workspace inside the same conversation directory, so the move is a rename on
    one filesystem rather than a copy — and so it is somewhere the next turn can find it. Named with
    the reserved directory's own name plus a suffix, because anything else would be a second name
    for the same thing.
    """
    return workspace.parent / f"{_repo().RESERVED_DIR}.migrating"


def _set_reserved_aside(workspace: Path) -> Path:
    """Move `<workspace>/.grid` out of the way, and answer where it went."""
    sideline = _reserved_sideline(workspace)
    reserved = workspace / _repo().RESERVED_DIR
    shutil.rmtree(sideline, ignore_errors=True)
    if reserved.is_dir() and not reserved.is_symlink():
        reserved.rename(sideline)
    return sideline


def _restore_reserved(workspace: Path) -> None:
    """Put a stranded `.grid/` back, if a provider died between the two halves of a migration.

    Runs BEFORE the worktree probe, so it also recovers a workspace that was successfully re-cut and
    then lost the machine before its reserved directory came home.

    A workspace that already has its own `.grid/` keeps it and the stranded copy is dropped: the
    two can only both exist because a later turn rebuilt one, so the workspace's is at least as new.
    Choosing deterministically matters more than choosing cleverly — a merge would leave a
    conversation half from each and nothing would say so.

    ⚠️ **A MISSING workspace directory is created rather than treated as a reason to decline**, and
    that line is the whole of a defect this function used to have. It asked `workspace.is_dir()` and
    returned when the answer was False — so the worst crash window in the migration, the one between
    `rmtree(workspace)` and its re-creation, left the sideline holding a conversation's only local
    transcript with no directory beside it, `_set_reserved_aside` cleared the stale sideline on its
    way past, and the conversation was gone. Recovery happened to work anyway, but only because
    `task_agent.ensure_workspace` runs before `materialize` on every path today — an implicit
    dependency between two modules that nothing enforced, where reordering the two would lose one
    conversation per crash and break nothing else. Found in review; this function now answers for
    itself. `tests/test_task_worktree.py` drives both, with and without a caller-made directory.
    """
    sideline = _reserved_sideline(workspace)
    if not sideline.is_dir():
        return
    reserved = workspace / _repo().RESERVED_DIR
    if reserved.exists():
        shutil.rmtree(sideline, ignore_errors=True)
        return
    _dirs().ensure_workspace(workspace)
    sideline.rename(reserved)


def _add_worktree(store: Path, workspace: Path, commit: str) -> None:
    """`git worktree add`, detached.

    ⚠️ **`--detach` is a guard on the ARGUMENT, not on git's default** — measured on git 2.54.0,
    because the obvious reason for it is wrong. Handed a full object id, `worktree add` detaches on
    its own and invents nothing; the basename-derived branch people remember is what happens when no
    commit-ish is given at all. What `--detach` actually buys is the case where `commit` is a
    **branch name**: without it git checks that branch out, and `worktree add` then REFUSES a second
    worktree wanting the same one (`fatal: 'main' is already used by worktree at …`) — so a caller
    passing `branch` where `input_commit` belongs would work for one conversation of a member and
    fail for every one after it.

    `materialize` points HEAD at `refs/heads/task/<turn_id>` immediately afterwards in any case, so
    nothing downstream depends on where this leaves it.
    """
    _repo()._run(store, "worktree", "add", "--quiet", "--detach", str(workspace), commit)


def remove_worktree(store: Path, workspace: Path) -> None:
    """Give a conversation's working tree back, leaving the store intact.

    Never raises: eviction is disk hygiene and a workspace that cannot be removed is skipped rather
    than allowed to take a turn down with it. The `rmtree` fallback exists because a half-added
    worktree is not one `worktree remove` will accept, and leaving the directory there would mean
    the bound could never be met.
    """
    task_repo = _repo()
    with store_lock(store):
        try:
            task_repo._run(store, "worktree", "remove", "--force", str(workspace))
        except task_repo.CheckoutError:
            shutil.rmtree(workspace, ignore_errors=True)
        try:
            task_repo._run(store, "worktree", "prune")
        except task_repo.CheckoutError:
            pass
