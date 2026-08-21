"""The bound on what a provider's workspaces cost it (ADR 0034 D-c, issue 50).

Nothing in `remote/` deleted a directory before this: `GRID_MAX_TASKS` bounds how many turns run at
once and bounds no disk at all, so a provider accumulated one workspace per conversation forever.

Two properties run through every test here and neither is negotiable:

  * **Eviction never refuses work.** A provider over its bound evicts and carries on; it does not
    decline a turn, because a turn declined for disk is a person's message left unanswered by a
    fault they cannot see and the relay cannot report.
  * **Eviction never touches a workspace a worker holds.** It takes the same reservation
    `tasks._reserve_workspace` takes, so "in use" is one fact with one owner rather than two
    registries that can disagree.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

_MEMBER = "9f2b" * 8
_OTHER_MEMBER = "4c7e" * 8
_CONVERSATIONS = [f"c0nv-{index}" for index in range(6)]


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        env={"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_NOSYSTEM": "1",
             "GIT_CONFIG_GLOBAL": os.devnull, "HOME": "/nonexistent",
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@invalid",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@invalid"})


def _remote(tmp_path):
    from remote.task_repo import GitRemote

    seed = tmp_path / "seed"
    seed.mkdir(parents=True)
    _git(seed, "init", "-q", "-b", "main", ".")
    (seed / "a.txt").write_text("one\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "input")
    bare = tmp_path / "origin.git"
    _git(tmp_path, "clone", "--bare", "-q", str(seed), str(bare))
    return GitRemote(url=str(bare), token="tok"), _git(bare, "rev-parse", "main").stdout.strip()


def _real_workspace(tmp_path, root, conversation, *, member=_MEMBER, project="proj-1"):
    """A workspace materialized the way a turn materializes one, so eviction has real state to eat."""
    from remote import task_agent, task_evict, task_repo

    remote, commit = _remote(tmp_path / conversation)
    branch = f"task/{conversation}"
    _git(remote.url, "branch", "-f", branch, commit)
    workspace = task_agent.ensure_workspace(
        task_agent.workspace_for(project, member, conversation))
    task_repo.materialize(workspace, url=remote.url, token=remote.token,
                          branch=branch, input_commit=commit)
    task_agent.ensure_cache(workspace)
    task_evict.touch(workspace)
    return workspace


def _never_reserved(_triple):
    """A reservation that always succeeds — the ordinary case, where no worker holds anything."""
    return True


def _released(_triple):
    return None


def _conversation_dirs(root, *, member=_MEMBER, project="proj-1"):
    from remote import task_worktree

    member_dir = root / "projects" / project / member
    if not member_dir.is_dir():
        return []
    return sorted(p.name for p in member_dir.iterdir()
                  if p.is_dir() and p.name != task_worktree.STORE_DIR_NAME)


def test_a_provider_over_its_workspace_cap_evicts_the_least_recently_used_and_keeps_serving(
        tmp_path, short_task_root, monkeypatch):
    """Criterion 2: the bound is enforced by eviction, never by refusing a turn.

    Three conversations against a cap of one leaves one, and the one left is the one used most
    recently — the other two are what a person is least likely to come back to, and re-materializing
    either costs a fetch the relay serves from a history it already has.

    `sweep` returning normally is half the assertion. A bound that raised, or that answered "no", is
    a turn declined for a reason the relay cannot report and the person who typed the message cannot
    see.
    """
    from remote import task_evict

    monkeypatch.setenv(task_agent_root_env(), str(short_task_root))
    monkeypatch.setenv(task_evict.MAX_WORKSPACES_ENV, "1")
    for conversation in _CONVERSATIONS[:3]:
        _real_workspace(tmp_path, short_task_root, conversation)

    task_evict.sweep(short_task_root, keep=None,
                     reserve=_never_reserved, release=_released)

    assert _conversation_dirs(short_task_root) == [_CONVERSATIONS[2]], (
        "the cap was not enforced, or it evicted the conversation that was used most recently")
    # And the store survives: it is what makes the next materialize a delta rather than a clone.
    from remote import task_agent, task_worktree
    store = task_worktree.store_for(
        task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATIONS[2]))
    assert (store / "objects").is_dir(), (
        "the member's object store went with the workspaces, so the next turn re-clones the whole "
        "project — the cost this layout exists to remove")


def test_a_stray_file_beside_the_projects_does_not_warn_that_the_bound_has_stopped(
        tmp_path, short_task_root, monkeypatch, capsys):
    """ND-18. A `.DS_Store` under `projects/` made every sweep on every macOS provider say the
    provider's workspace bound "is not being fully enforced".

    The warning was TRUE of the entry it named and false of everything a reader takes it to mean.
    `_conversations` handed each child of `projects/` to `_subdirectories`, which listed it, got
    `NotADirectoryError` for a file, and reported the one thing it is right to report when a real
    listing fails. But a file there is not a failure — it is Finder, on every Mac, without anybody
    choosing it — and the sweep went on to enforce the cap correctly over every genuine project.
    So the impact was nil and the sentence said otherwise, once per sweep, forever.

    The fix is to stop asking: a non-directory is not a project and never was a candidate, so it
    is filtered where the children are listed. The warning then keeps its meaning for the case it
    was written for — which the sibling test below still holds it to.

    Both halves are asserted, and the eviction half is the positive control: a test that only
    checked stderr would pass just as well against a sweep that had silently stopped working.
    """
    from remote import task_evict

    monkeypatch.setenv(task_agent_root_env(), str(short_task_root))
    monkeypatch.setenv(task_evict.MAX_WORKSPACES_ENV, "1")
    for conversation in _CONVERSATIONS[:2]:
        _real_workspace(tmp_path, short_task_root, conversation)
    # Exactly what Finder leaves behind the moment somebody opens the folder.
    (short_task_root / "projects" / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
    capsys.readouterr()

    task_evict.sweep(short_task_root, keep=None,
                     reserve=_never_reserved, release=_released)

    err = capsys.readouterr().err
    assert _conversation_dirs(short_task_root) == [_CONVERSATIONS[1]], (
        "the cap stopped being enforced, so the stderr assertion below proves nothing")
    assert ".DS_Store" not in err, (
        f"a stray file is still reported as a directory the sweep could not read:\n{err}")
    assert "not being fully enforced" not in err, (
        f"the sweep told the operator its workspace bound had stopped working, while enforcing "
        f"it correctly in the same call (ND-18):\n{err}")


def test_eviction_skips_a_workspace_a_worker_is_holding_rather_than_waiting_for_it(
        tmp_path, short_task_root, monkeypatch):
    """Criterion 3, and the mechanism matters as much as the outcome.

    A workspace is "in use" when a worker holds its reservation, and eviction asks for that same
    reservation rather than consulting a second registry. Anything else is two facts that can
    disagree — and the disagreement is an agent whose working tree is deleted underneath it
    mid-turn, which reads as a broken model rather than as a broken provider.

    Skipped, never waited on: a sweep that blocked on a reservation would hold up the turn that
    triggered it for as long as somebody else's agent runs.
    """
    from remote import task_evict

    monkeypatch.setenv(task_agent_root_env(), str(short_task_root))
    monkeypatch.setenv(task_evict.MAX_WORKSPACES_ENV, "1")
    for conversation in _CONVERSATIONS[:3]:
        _real_workspace(tmp_path, short_task_root, conversation)

    busy = ("proj-1", _MEMBER, _CONVERSATIONS[0])
    taken = []

    def reserve(triple):
        if triple == busy:
            return False
        taken.append(triple)
        return True

    task_evict.sweep(short_task_root, keep=None, reserve=reserve, release=_released)

    remaining = _conversation_dirs(short_task_root)
    assert remaining == [_CONVERSATIONS[0]], (
        f"a workspace a worker was holding was evicted — its agent's working tree disappeared "
        f"mid-turn: {remaining}")
    assert busy not in taken, "eviction took the reservation of a workspace it could not have"


def test_a_held_workspace_counts_against_the_cap_rather_than_costing_a_colder_one_its_place(
        tmp_path, short_task_root, monkeypatch):
    """The stop rule, which is easy to get backwards in the expensive direction.

    A workspace a worker holds is still a workspace and still disk, so it counts. What must NOT
    happen is the sweep going on past its bound to make up for it: with a cap of two, one held and
    two cold, exactly ONE cold workspace goes. Treating the held one as free would evict both and
    charge the next turn of the survivor a fetch to hold an average nobody asked for.

    The bound is therefore best-effort *upwards* — three held workspaces against a cap of one leaves
    three, because there is nothing else to do — and exact downwards.
    """
    from remote import task_evict

    monkeypatch.setenv(task_agent_root_env(), str(short_task_root))
    monkeypatch.setenv(task_evict.MAX_WORKSPACES_ENV, "2")
    for conversation in _CONVERSATIONS[:3]:
        _real_workspace(tmp_path, short_task_root, conversation)

    busy = ("proj-1", _MEMBER, _CONVERSATIONS[0])
    task_evict.sweep(short_task_root, keep=None,
                     reserve=lambda triple: triple != busy, release=_released)

    remaining = _conversation_dirs(short_task_root)
    assert remaining == [_CONVERSATIONS[0], _CONVERSATIONS[2]], (
        f"expected the held one and the newest to survive a cap of two, got {remaining}")


def test_the_conversation_a_turn_is_about_to_run_in_is_never_evicted(
        tmp_path, short_task_root, monkeypatch):
    """`keep`, which covers what the reservation cannot.

    The sweep runs at the start of a turn, and by then `_run_and_report` already holds this
    conversation's reservation — so in production `reserve` refuses it and it is safe. `keep` is for
    every other caller: a direct `run_task`, a test, a future path that sweeps before reserving.
    Evicting the workspace a turn is about to use is not a correctness bug — `materialize` would
    rebuild it — but it throws away the warm checkout for nothing, which is what the cap exists to
    ration.
    """
    from remote import task_evict

    monkeypatch.setenv(task_agent_root_env(), str(short_task_root))
    monkeypatch.setenv(task_evict.MAX_WORKSPACES_ENV, "1")
    for conversation in _CONVERSATIONS[:2]:
        _real_workspace(tmp_path, short_task_root, conversation)

    task_evict.sweep(short_task_root, keep=("proj-1", _MEMBER, _CONVERSATIONS[0]),
                     reserve=_never_reserved, release=_released)

    assert _CONVERSATIONS[0] in _conversation_dirs(short_task_root)


def test_the_last_conversation_of_a_member_takes_the_object_store_with_it(
        tmp_path, short_task_root, monkeypatch):
    """The half that actually bounds the disk, and the one a cap alone does not reach.

    Evicting worktrees leaves the member's object store — ~1,043 MiB of it on the repository issue
    35 measured — for conversations that no longer exist. It is kept while ANY of that member's
    conversations survive, because that is what makes their next turn a delta rather than a clone;
    it goes with the last one, because at that point it is a copy of a history nothing on this
    provider is working in.

    The project directory follows its last member for the same reason, one level up.
    """
    from remote import task_evict, task_worktree

    monkeypatch.setenv(task_agent_root_env(), str(short_task_root))
    monkeypatch.setenv(task_evict.MAX_WORKSPACES_ENV, "1")
    workspace = _real_workspace(tmp_path, short_task_root, _CONVERSATIONS[0])
    store = task_worktree.store_for(workspace)
    _real_workspace(tmp_path, short_task_root, _CONVERSATIONS[1], member=_OTHER_MEMBER)
    assert (store / "objects").is_dir(), "this test never had a store to collect"

    task_evict.sweep(short_task_root, keep=("proj-1", _OTHER_MEMBER, _CONVERSATIONS[1]),
                     reserve=_never_reserved, release=_released)

    assert not store.exists(), (
        f"{store} survived its last conversation — the member's whole git history is still on disk "
        f"for a member with no workspace left, which is the bulk of what a bound is for")
    assert not store.parent.exists(), "the member directory is an empty shell"
    # The OTHER member is untouched: the collection is per member, not a whole-tree sweep.
    assert (short_task_root / "projects" / "proj-1" / _OTHER_MEMBER).is_dir()


def test_a_directory_that_cannot_be_listed_is_assumed_OCCUPIED_and_nothing_is_collected(
        tmp_path, short_task_root, monkeypatch, capsys):
    """A listing that FAILED must never be read as a listing that came back EMPTY.

    `_subdirectories` answering `[]` on `OSError` is the safe direction where it builds the
    candidate list — the sweep under-counts and the bound is under-enforced. `_collect_empty_parents`
    asks the opposite question: it reads "nothing here but the store" as permission to delete the
    member's entire tree. Fed the same `[]`, one transient `OSError` — EMFILE on a provider running
    many concurrent git subprocesses is the realistic one, and this sweep is running beside exactly
    that — deletes every conversation of that member, live ones included, and the shared history
    with them. One level up it deletes every member of the project.

    Nothing raises, so `sweep`'s own handler never fires: the function succeeds at deleting the wrong
    thing, in silence, past the reservation that is supposed to be the gate. That is the failure the
    whole module is written to avoid, arriving through the one call site that reads an absence as a
    fact rather than as an absence.

    Found by review, and the fix is that "could not tell" is now its own answer.
    """
    from remote import task_evict

    monkeypatch.setenv(task_agent_root_env(), str(short_task_root))
    monkeypatch.setenv(task_evict.MAX_WORKSPACES_ENV, "1")
    doomed = _real_workspace(tmp_path, short_task_root, _CONVERSATIONS[0])
    live = _real_workspace(tmp_path, short_task_root, _CONVERSATIONS[1])
    member_dir = live.parent.parent
    project_dir = member_dir.parent

    real_iterdir = Path.iterdir
    seen: dict = {}

    def iterdir(self):
        # TRANSIENT, not a broken filesystem: the first listing of each of these two directories
        # succeeds, so the sweep builds its candidate list and performs the eviction it was asked
        # for exactly as normal. The failure lands on the SECOND listing — which is the one
        # `_collect_empty_parents` makes, after the eviction, to decide whether anything is left.
        if self in (member_dir, project_dir):
            seen[self] = seen.get(self, 0) + 1
            if seen[self] > 1:
                raise OSError("simulated EMFILE")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)

    task_evict.sweep(short_task_root, keep=None, reserve=_never_reserved, release=_released)

    assert live.is_dir(), (
        "a sibling conversation this sweep never touched was deleted because one directory listing "
        "failed — if its agent were running, its working tree just vanished underneath it")
    assert member_dir.is_dir() and project_dir.is_dir()
    assert not doomed.parent.exists(), (
        "the eviction the sweep was actually asked for did not happen, so this test proves nothing "
        "about the guard")
    assert str(member_dir) in capsys.readouterr().err, (
        "the listing failed and nothing said so — an operator sees a bound that is quietly not "
        "being enforced for this member, with no way to find out why")


def test_an_evicted_conversation_leaves_no_dangling_transcript_link_behind(
        tmp_path, short_task_root, monkeypatch):
    """One symlink per evicted conversation, in the operator's own Claude configuration.

    `link_transcript` plants `<config>/projects/<flattened cwd>` pointing into the workspace, and
    eviction removes what it points at. Individually harmless; in aggregate it is the 486 dangling
    links a full suite once left in a developer's real `~/.claude`, which is what
    `conftest._claude_config_dir_is_never_the_real_one` exists for.

    Named with the same function that made it rather than by scanning for a target: one spelling,
    so the link cannot be orphaned by a rule that drifted.
    """
    from remote import task_agent, task_evict

    monkeypatch.setenv(task_agent_root_env(), str(short_task_root))
    monkeypatch.setenv(task_evict.MAX_WORKSPACES_ENV, "1")
    doomed = _real_workspace(tmp_path, short_task_root, _CONVERSATIONS[0])
    task_agent.link_transcript(doomed, _MEMBER)
    link = task_agent.claude_config_dir() / "projects" / task_agent.transcript_dir_name(doomed)
    assert link.is_symlink(), "this test never had a link to collect"
    _real_workspace(tmp_path, short_task_root, _CONVERSATIONS[1])

    task_evict.sweep(short_task_root, keep=None, reserve=_never_reserved, release=_released)

    assert not link.is_symlink(), (
        f"{link} still points into a workspace that no longer exists")


def test_a_free_space_floor_evicts_even_when_the_count_is_under_the_cap(
        tmp_path, short_task_root, monkeypatch):
    """The second bound, and the one that answers the question an operator actually asks.

    A count cap bounds directories; whether that is bytes depends on the repository. The floor is
    the promise: this provider will not drive the disk below a line. Both are O(1) —
    `shutil.disk_usage` is a `statvfs`, not a walk — which is why there are two cheap bounds rather
    than one expensive one.

    Off by default, so this test has to turn it on: a floor is a promise about a disk the provider
    does not own alone, and a machine already below it would otherwise evict everything on every
    turn and re-fetch it.
    """
    import shutil as shutil_module

    from remote import task_evict

    monkeypatch.setenv(task_agent_root_env(), str(short_task_root))
    monkeypatch.setenv(task_evict.MAX_WORKSPACES_ENV, "100")   # the COUNT is not what bites here
    monkeypatch.setenv(task_evict.MIN_FREE_ENV, "10")
    for conversation in _CONVERSATIONS[:3]:
        _real_workspace(tmp_path, short_task_root, conversation)

    # A disk under the floor for the FIRST check only, so the loop's exit condition is exercised
    # rather than its "evict everything" degenerate case: exactly one eviction, then room.
    freed = {"calls": 0}
    real_usage = shutil_module.disk_usage

    def usage(path):
        freed["calls"] += 1
        total = real_usage(path)
        free = 1 * 1024 ** 3 if freed["calls"] <= 1 else 100 * 1024 ** 3
        return type(total)(total.total, total.used, free)

    monkeypatch.setattr(task_evict.shutil, "disk_usage", usage)

    task_evict.sweep(short_task_root, keep=None, reserve=_never_reserved, release=_released)

    remaining = _conversation_dirs(short_task_root)
    assert remaining == [_CONVERSATIONS[1], _CONVERSATIONS[2]], (
        f"the free-space floor did not evict, or did not stop once the disk was above it: "
        f"{remaining}")


def test_a_floor_that_cannot_be_measured_deletes_nothing(short_task_root, monkeypatch):
    """Fail OPEN, and this is the one place in the module where that direction is not obvious.

    Everywhere else "could not tell" leans towards evicting — a stamp that cannot be read sorts
    oldest, which costs one fetch. Here it would mean deleting every workspace on the provider on
    every turn, forever, because the floor can never be satisfied. So an unreadable filesystem
    reports plenty of room.
    """
    from remote import task_evict

    monkeypatch.setenv(task_agent_root_env(), str(short_task_root))
    monkeypatch.setattr(
        task_evict.shutil, "disk_usage",
        lambda path: (_ for _ in ()).throw(OSError("no statvfs here")))

    assert task_evict._free_bytes(short_task_root) > 1024 ** 5


def test_a_misconfigured_bound_warns_and_falls_back_rather_than_stopping_task_serving(
        monkeypatch, capsys):
    """The tunable convention (`GRID_MAX_TASKS`, `GRID_TASK_TIMEOUT_SECONDS`), not the refusing one.

    Refusing to start over a mistyped *cap* takes task serving down for the life of the process,
    which is a far larger fault than the one it reports — and the value is not a security boundary,
    unlike `GRID_TASK_PERMISSION_MODE`, which is refused outright and should stay that way.

    Zero and negative are refused rather than honoured: a cap of zero evicts every workspace on
    every turn, which is a provider that re-clones the project per turn and never says why.
    """
    from remote import task_evict

    for bad in ("nonsense", "0", "-3", "2.5"):
        monkeypatch.setenv(task_evict.MAX_WORKSPACES_ENV, bad)
        assert task_evict.max_workspaces() == task_evict.DEFAULT_MAX_WORKSPACES, bad
        assert task_evict.MAX_WORKSPACES_ENV in capsys.readouterr().err

    # ⚠️ `inf` and `nan` PARSE. `float("inf")` raises no `ValueError`, and `inf < 0` and `nan < 0`
    #    are both False, so both used to sail past this function's own validation and blow up one
    #    line later in `int(...)` — `OverflowError` and `ValueError` respectively, neither caught
    #    here. `sweep`'s blanket handler then swallowed it, which disables the ENTIRE bound (the
    #    count cap included) on every turn for the life of the process, behind a generic warning
    #    that never names the variable at fault. A config typo that turns disk bounding off and does
    #    not say so. Found in review; the fuzz-the-field-carrying-the-verdict class.
    for bad in ("not-a-number", "inf", "-inf", "nan", "1e400"):
        monkeypatch.setenv(task_evict.MIN_FREE_ENV, bad)
        assert task_evict.min_free_bytes() == 0, bad
        assert task_evict.MIN_FREE_ENV in capsys.readouterr().err, bad

    monkeypatch.delenv(task_evict.MAX_WORKSPACES_ENV)
    monkeypatch.delenv(task_evict.MIN_FREE_ENV)
    assert task_evict.max_workspaces() == task_evict.DEFAULT_MAX_WORKSPACES
    assert task_evict.min_free_bytes() == 0, "the free-space floor is off unless asked for"
    assert capsys.readouterr().err == "", "an unset knob is not a misconfiguration"


def test_one_workspace_that_cannot_be_evicted_does_not_disable_the_bound_for_the_others(
        tmp_path, short_task_root, monkeypatch, capsys):
    """A single bad directory must cost one workspace's worth of disk, not all of it.

    `_evict` is written not to raise — `rmtree` is `ignore_errors`, `remove_worktree` swallows — but
    "written not to raise" is what every unbounded blast radius is made of. Without a per-item guard
    the first surprise propagates out of the loop, `sweep` catches it at the top, and the remaining
    candidates are never even considered: the bound silently stops being enforced, on every turn,
    for the life of the directory. That is the same shape as the fault this module exists to prevent,
    one level up.

    The module's own contract already says the right thing — "a workspace that cannot be evicted is
    skipped rather than waited on" — and this is what makes it true of a workspace that cannot be
    evicted for a reason nobody predicted, rather than only of one a worker holds.
    """
    from remote import task_evict

    monkeypatch.setenv(task_agent_root_env(), str(short_task_root))
    # A cap of TWO, not one: with a cap of one the loop would have to evict everything it can either
    # way, so an aborted sweep and a resilient one differ by two directories rather than by the one
    # this test is about. At two, a sweep that stops at the wedged workspace leaves all three.
    monkeypatch.setenv(task_evict.MAX_WORKSPACES_ENV, "2")
    for conversation in _CONVERSATIONS[:3]:
        _real_workspace(tmp_path, short_task_root, conversation)

    doomed = short_task_root / "projects" / "proj-1" / _MEMBER / _CONVERSATIONS[0]
    real_evict = task_evict._evict

    def evict(conversation_dir):
        if conversation_dir == doomed:
            raise OSError("this one is wedged")
        return real_evict(conversation_dir)

    monkeypatch.setattr(task_evict, "_evict", evict)

    task_evict.sweep(short_task_root, keep=None, reserve=_never_reserved, release=_released)

    remaining = _conversation_dirs(short_task_root)
    assert remaining == [_CONVERSATIONS[0], _CONVERSATIONS[2]], (
        f"one wedged workspace stopped the sweep reaching the others, so the bound is not enforced "
        f"at all while it exists: {remaining}")
    assert "wedged" in capsys.readouterr().err, (
        "the workspace that could not be evicted was skipped in silence")


def test_a_sweep_that_blows_up_does_not_take_the_turn_with_it(short_task_root, monkeypatch, capsys):
    """Disk hygiene is not correctness, and it runs on the path a turn starts on.

    Every fault in here — an unreadable directory, a store that will not answer, a filesystem that
    has gone away — must arrive as a warning and a turn that runs normally. The alternative is a
    provider that fails somebody's message because it could not tidy up.
    """
    from remote import task_evict

    monkeypatch.setattr(
        task_evict, "_sweep",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("the disk went away")))

    task_evict.sweep(short_task_root, keep=None, reserve=_never_reserved, release=_released)

    assert "the disk went away" in capsys.readouterr().err


def task_agent_root_env():
    from remote import task_agent

    return task_agent.WORKSPACE_ROOT_ENV
