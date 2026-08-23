"""What a provider's workspaces are allowed to cost it (ADR 0034 D-c, issue 50).

`GRID_MAX_TASKS` bounds how many turns run at once. It bounds no directories at all, and before this
module nothing in `remote/` ever removed one — a provider accumulated a workspace per conversation
for the life of the machine. MEASURED (issue 35, git 2.54.0, on a 792 MiB / 34,159-commit
repository): a worktree costs **+305.8 MiB** and its member's object store **+1,043.4 MiB**, so a
busy team's provider fills a disk on arithmetic nobody was doing.

**The bound is enforced by eviction and never by refusing work**, which is the whole shape of this
module. A turn declined for disk is a person's message left unanswered by a fault the relay cannot
report and they cannot see; a turn that runs after evicting somebody's cold checkout costs a fetch
the relay serves from a history it already has. So every failure in here warns and carries on, and
a bound that cannot be met is simply not met.

Two knobs, both TUNABLES — a bad value warns on stderr and falls back, the convention
`task_opt_in.worker_count` and `tasks.task_timeout` follow, rather than the refuse-outright one
`GRID_TASK_PERMISSION_MODE` follows. Refusing to start over a misconfigured *cap* would take task
serving down for the life of the process, which is a far larger fault than the one it reports.
"""
from __future__ import annotations

import math
import os
import shutil
import sys
from pathlib import Path

from . import task_worktree

# How many conversation workspaces one provider keeps. Past this, the least recently used are
# evicted until the count fits.
#
# The default is a compromise between two costs that are both real: too low and a team's ordinary
# conversations re-fetch their checkout every time somebody switches between them; too high and the
# bound is decorative. Eight worktrees of the repository issue 35 measured is ~2.4 GiB beside a
# ~1 GiB store — a figure an operator can check against their own disk, which is why the measured
# per-worktree cost is quoted in `docs/cli.md` beside this knob rather than left to be rediscovered.
MAX_WORKSPACES_ENV = "GRID_TASK_MAX_WORKSPACES"
DEFAULT_MAX_WORKSPACES = 8

# A floor under free space on the filesystem holding the task root, in whole GiB. **Off by default**,
# and that is deliberate: a floor is a promise about a disk this provider does not own alone, so a
# machine already below it for reasons of its own would evict everything on every turn and re-fetch
# it, spending bandwidth to fix somebody else's log file. An operator who wants the promise asks for
# it. Nothing else is off by default here, so the asymmetry is stated rather than left to be read
# off the code.
MIN_FREE_ENV = "GRID_TASK_MIN_FREE_GB"
DEFAULT_MIN_FREE_GB = 0

_BYTES_PER_GB = 1024 ** 3

# The LRU stamp, a sibling of `workspace` and `cache` inside the conversation directory.
#
# Outside the worktree on purpose, twice over: `git clean -ffdx` would remove it from inside, and
# the sandbox grants the agent neither this file nor the directory holding it — so a workspace
# cannot make itself look recently used and outlive a colleague's.
LAST_USED_NAME = "last-used"


def _warn(message: str) -> None:
    print(f"\n[tasks] {message}", file=sys.stderr)


def max_workspaces() -> int:
    """How many conversation workspaces this provider keeps, or the default with a warning."""
    raw = (os.getenv(MAX_WORKSPACES_ENV) or "").strip()
    if not raw:
        return DEFAULT_MAX_WORKSPACES
    try:
        count = int(raw)
    except ValueError:
        count = 0
    if count < 1:
        _warn(f"{MAX_WORKSPACES_ENV}={raw!r} is not a positive whole number of workspaces; "
              f"using {DEFAULT_MAX_WORKSPACES}")
        return DEFAULT_MAX_WORKSPACES
    return count


def min_free_bytes() -> int:
    """The free-space floor in bytes, or 0 when the operator has not asked for one.

    ⚠️ **`inf` and `nan` PARSE, and that is the whole reason this reads the way it does.**
    `float("inf")` raises no `ValueError`, and `inf < 0` and `nan < 0` are both False — so a
    range check alone lets both through to `int(...)`, which raises `OverflowError` and `ValueError`
    respectively. Neither is caught here, so this function's own message — the one naming the
    variable at fault — never fires; `sweep`'s blanket handler catches it instead and disables the
    ENTIRE bound, count cap included, on every turn for the life of the process, behind a warning
    that says only that something went wrong. A typo that silently turns disk bounding off.

    `math.isfinite` is the check, not `< 0`: the question is whether this is a number of gigabytes,
    and `inf` is not one however it compares. Found in review.
    """
    raw = (os.getenv(MIN_FREE_ENV) or "").strip()
    if not raw:
        return DEFAULT_MIN_FREE_GB * _BYTES_PER_GB
    try:
        gigabytes = float(raw)
    except ValueError:
        gigabytes = -1.0
    if not math.isfinite(gigabytes) or gigabytes < 0:
        _warn(f"{MIN_FREE_ENV}={raw!r} is not a number of gigabytes; using "
              f"{DEFAULT_MIN_FREE_GB}")
        return DEFAULT_MIN_FREE_GB * _BYTES_PER_GB
    return int(gigabytes * _BYTES_PER_GB)


def touch(workspace: Path) -> None:
    """Record that this conversation was used now, for the LRU order.

    Best-effort by design: a stamp that could not be written makes a workspace look old, which
    means it is evicted sooner than it deserved — a cost measured in one fetch. Raising here would
    fail a turn over bookkeeping.
    """
    try:
        (workspace.parent / LAST_USED_NAME).write_text("")
    except OSError as exc:
        _warn(f"could not stamp {workspace.parent} as recently used ({exc}); it will look older "
              f"than it is to the workspace bound")


def _last_used(conversation_dir: Path) -> float:
    """When this conversation was last run, oldest-first ordering key.

    Falls back to the directory's own mtime for a workspace that predates the stamp, or one whose
    stamp could not be written. Never raises: a conversation directory that cannot be read at all
    sorts oldest, which puts it first in line to be removed — the right direction, since it is
    exactly the state a half-deleted eviction leaves behind.
    """
    for candidate in (conversation_dir / LAST_USED_NAME, conversation_dir):
        try:
            return candidate.stat().st_mtime
        except OSError:
            continue
    return 0.0


def _conversations(root: Path) -> list[tuple[tuple[str, str, str], Path]]:
    """Every conversation directory under `root`, as `((project, member, conversation), path)`.

    Read off the DISK rather than from anything this process remembers, which is what makes a
    restart harmless: a workspace left by a provider that died — or by a previous version of it — is
    an ordinary candidate here, so nothing accumulates that only a human would ever collect.
    """
    found: list[tuple[tuple[str, str, str], Path]] = []
    projects = root / "projects"
    if not projects.is_dir():
        return found
    try:
        # ⚠️ **Filtered to DIRECTORIES here, not left for `_subdirectories` to trip over** (ND-18).
        # A non-directory under `projects/` is not a project and can never hold a candidate, but
        # handing one down produced `NotADirectoryError` — a real listing failure as far as that
        # function can tell — and with it a warning that this provider's workspace bound "is not
        # being fully enforced". On macOS the file is `.DS_Store`, Finder writes it the moment
        # anybody opens the folder, and the sweep went on enforcing the cap perfectly over every
        # genuine project while saying once per sweep that it had stopped. The impact was nil and
        # the sentence was alarming, which is the pair that teaches an operator to ignore it.
        #
        # Same predicate as `_subdirectories`, symlinks included: a symlinked project directory
        # would let the sweep walk — and delete — through a link out of the tree entirely.
        project_dirs = sorted(entry for entry in projects.iterdir()
                              if entry.is_dir() and not entry.is_symlink())
    except OSError as exc:
        # A failure to list `projects/` ITSELF is still a real one, and still says so.
        _warn(f"could not list {projects} to bound the provider's workspaces ({exc})")
        return found
    for project in project_dirs:
        # `or []` — a directory that could not be listed contributes no CANDIDATES, which is the
        # safe direction here: the bound is under-enforced rather than over-enforced, and
        # `_subdirectories` has already said so on stderr.
        for member in _subdirectories(project) or []:
            for conversation in _subdirectories(member) or []:
                if conversation.name == task_worktree.STORE_DIR_NAME:
                    continue
                found.append(
                    ((project.name, member.name, conversation.name), conversation))
    return found


def _subdirectories(path: Path) -> list[Path] | None:
    """The directories under `path`, or **`None` when that could not be determined**.

    ⚠️ **`None` and `[]` are different answers and conflating them deletes people's work.** Found in
    review, reproduced, and it was live: `_subdirectories` used to answer `[]` for a listing that
    FAILED, which is the safe direction for `_conversations` (fewer candidates, the bound
    under-enforced) and the catastrophic one for `_collect_empty_parents`, which reads "nothing here
    but the store" as permission to delete a member's entire tree. One transient `OSError` — EMFILE
    on a provider running many concurrent git subprocesses, which is exactly what is running beside
    this sweep — therefore deleted every conversation of that member, live ones included, and the
    shared history with them; one level up, every member of the project. Nothing raised, so nothing
    warned, and the reservation that is supposed to be the gate was never consulted for any of them.

    So the ambiguity is removed at the source rather than at each call site: this answers what it
    knows, callers that can safely proceed on nothing say so with `or []`, and the one caller that
    is about to delete something must handle `None`.
    """
    try:
        return sorted(entry for entry in path.iterdir()
                      if entry.is_dir() and not entry.is_symlink())
    except OSError as exc:
        _warn(f"could not list {path} ({exc}); this provider's workspace bound is not being fully "
              f"enforced while that lasts, and nothing under it will be collected")
        return None


def sweep(root: Path, *, keep, reserve, release) -> None:
    """Bring the provider back under its workspace bound. Never raises, never refuses.

    `reserve`/`release` are `tasks._reserve_workspace` and `tasks._release_workspace`: eviction takes
    the SAME reservation a worker takes, rather than reading a second registry that could disagree
    with it. A registry that said "free" a moment before a worker took it would delete an agent's
    working tree mid-turn, and the symptom is a model that appears to have lost its mind. Passed in
    rather than imported because `tasks` imports this module.

    `keep` is the conversation the caller is about to run in, or `None`. In production it is already
    reserved by the time this runs, so it is protected twice — but the two cover different callers:
    the reservation covers OTHER workers, `keep` covers this one, which need not hold a reservation
    at all.

    Anything that cannot be evicted is SKIPPED rather than waited on. A sweep that blocked on a
    reservation would hold up the turn that triggered it for as long as somebody else's agent runs,
    and the bound is not worth that.
    """
    try:
        _sweep(root, keep=keep, reserve=reserve, release=release)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — hygiene must never take a turn down
        # `SystemExit` too, and it is not padding: this package uses it as a clean-error idiom, so it
        # is what a CLI-style validator raises — and `SystemExit` is not an `Exception`. This call
        # sits OUTSIDE `run_task`'s guarded block, so one escaping here would propagate all the way
        # to `_supervise_one_task`'s own `(Exception, SystemExit)` and be reported to the person as a
        # FAILED TURN, which is the exact opposite of this function's contract. Nothing on the paths
        # below raises one today; the convention is what stops that being a permanent fact nobody
        # rechecks. Same clause as every other guard in `tasks.py`.
        _warn(f"could not bound this provider's workspaces ({exc!r}); the turn runs normally, but "
              f"disk is not being reclaimed")


def _sweep(root: Path, *, keep, reserve, release) -> None:
    cap = max_workspaces()
    floor = min_free_bytes()
    candidates = _conversations(root)
    # Oldest first, which is the order they are offered for eviction in.
    candidates.sort(key=lambda item: _last_used(item[1]))
    remaining = len(candidates)
    for triple, conversation_dir in candidates:
        if remaining <= cap and _free_bytes(root) >= floor:
            return
        if triple == keep:
            continue
        if not reserve(triple):
            # A worker has it. Skipped — and `remaining` is deliberately NOT decremented, because a
            # held workspace is still a workspace and still disk. Decrementing it would make the
            # sweep go on past its bound to make up the number, evicting a colder workspace to pay
            # for one it could not touch and charging that conversation's next turn a fetch.
            #
            # So the bound is best-effort UPWARDS (three held against a cap of one leaves three,
            # because there is nothing else to do) and exact downwards.
            continue
        try:
            _evict(conversation_dir)
            remaining -= 1
        except (Exception, SystemExit) as exc:  # noqa: BLE001 — see below
            # PER ITEM, not per sweep, and the difference is the whole bound. `_evict` is written not
            # to raise — `rmtree` is `ignore_errors`, `remove_worktree` swallows — but that is what
            # every unbounded blast radius is made of. Caught at the top instead, the first surprise
            # ends the loop and the candidates behind it are never considered: the bound stops being
            # enforced on every turn for as long as the bad directory exists, which is the fault this
            # module exists to prevent, one level up.
            #
            # `remaining` is deliberately not decremented — the disk is still there — so this reads
            # exactly like a workspace a worker holds.
            _warn(f"could not evict {conversation_dir} ({exc!r}); skipping it and carrying on")
        finally:
            release(triple)


def _free_bytes(root: Path) -> int:
    """Free space on the filesystem holding `root`, or "plenty" if it cannot be read.

    `shutil.disk_usage` is a `statvfs` — O(1), no walk. Failing OPEN is the right direction: a floor
    that cannot be measured must not become a licence to delete everything.
    """
    try:
        return shutil.disk_usage(root).free
    except OSError:
        return sys.maxsize


def _evict(conversation_dir: Path) -> None:
    """Remove one conversation's workspace, cache and stamp, leaving the member's store.

    The store stays because it is what makes the next turn of any of this member's conversations a
    delta rather than a full clone — it is removed only when the last of them goes, below.

    ⚠️ `"workspace"` is spelled here as well as in `task_agent.workspace_for`, deliberately rather
    than by calling it: that function VALIDATES its three segments, and a junk directory under the
    task root would raise instead of being collected — so the one thing the bound most needs to
    remove would be the one thing it could not. Safe to duplicate because a wrong name degrades
    rather than misfires: `remove_worktree` finds nothing, `rmtree` takes the directory anyway, and
    the store's stale worktree entry is collected by the next `ensure_store`.
    """
    workspace = conversation_dir / "workspace"
    store = task_worktree.store_for(workspace)
    if (store / "objects").is_dir():
        task_worktree.remove_worktree(store, workspace)
    _unlink_transcript_link(workspace)
    shutil.rmtree(conversation_dir, ignore_errors=True)
    _collect_empty_parents(conversation_dir.parent)


def _unlink_transcript_link(workspace: Path) -> None:
    """Drop the Claude Code symlink that pointed into this workspace.

    Named with the SAME function that made it (`task_agent.transcript_dir_name`), never by scanning
    for a target — one spelling, so a link cannot be left behind by a rule that drifted. Left
    behind, it is a dangling symlink in the operator's config directory per evicted conversation:
    harmless individually, and the thing that put 486 of them in a developer's `~/.claude` once
    already.
    """
    from . import task_agent

    try:
        link = (task_agent.claude_config_dir() / "projects"
                / task_agent.transcript_dir_name(workspace))
        if link.is_symlink():
            link.unlink()
    except OSError as exc:
        _warn(f"could not remove the transcript link for {workspace} ({exc})")


def _collect_empty_parents(member_dir: Path) -> None:
    """Take the object store when its last conversation goes, and the project when its last member does.

    This is the half that actually bounds the disk: a member directory holding only a store is
    ~1 GiB of history for conversations that no longer exist. Removed only when the last
    conversation is gone, so a member with any live workspace keeps the store that makes their next
    turn a delta.

    ⚠️ **The only place in this module that reads an EMPTY listing as permission to delete**, which
    is why it is the only one that must distinguish `[]` from `None`. `_subdirectories`' docstring
    carries what happened when it could not.

    ⚠️ Under the store's lock, because removing a store IS a structural mutation of it — the most
    structural there is — and `task_worktree` serializes the other three. Without it a brand-new
    conversation of the same member, whose `ensure_workspace` lands between the listing and the
    `rmtree`, is deleted mid-creation: its directory was not in this sweep's candidate snapshot, so
    nothing reserved it and nothing would have said so. Narrow, and the shape this whole function
    got wrong once already.
    """
    entries = _subdirectories(member_dir)
    if entries is None:
        return  # `_subdirectories` already said why; assume occupied and touch nothing.
    if any(entry.name != task_worktree.STORE_DIR_NAME for entry in entries):
        return

    with task_worktree.store_lock(member_dir / task_worktree.STORE_DIR_NAME):
        # Asked AGAIN under the lock: the answer above was taken before anything was held, and the
        # whole point is the conversation that appeared in between.
        entries = _subdirectories(member_dir)
        if entries is None or any(
                entry.name != task_worktree.STORE_DIR_NAME for entry in entries):
            return
        shutil.rmtree(member_dir, ignore_errors=True)

    project_dir = member_dir.parent
    siblings = _subdirectories(project_dir)
    if siblings is not None and not siblings:
        shutil.rmtree(project_dir, ignore_errors=True)
