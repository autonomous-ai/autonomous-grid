"""What `grid project refresh` says about a clone, rendered for a person (ADR 0033 D-h).

Extracted from `cli/remote_project.py` for the reason `cli/project_providers.py` was: adding this
command took that file past this project's 800-line ceiling. One concern lives here — turning a
`remote.project_clone.Refreshed` into either a machine-readable payload or a few sentences a member
can act on — and it holds no state and reaches no network.

Three rules run through all of it, and each has already been broken once:

* **Never claim a cause the command cannot verify.** "The grid does not have this branch" has two
  opposite histories (a WIP branch not created yet, a task branch the relay collected) and nothing
  here can tell them apart without reading the relay's ref-naming rule into this side of the wire.
* **Never print advice that would fail if followed.** On a diverged branch `git merge --ff-only`
  errors and `git pull` makes a merge commit a member can never push, so neither is offered.
* **Say nothing when there is nothing to say.** A standing "0 other refs updated, 0 pruned" on the
  commonest run of all is what stops the line being read on the run that matters.
"""
from __future__ import annotations

# How wide a commit id is printed. FIXED, not git's own abbreviation: `--short` grows with a
# repository's object count, so the same commit would print at different widths for two members of
# one project, and somebody comparing what refresh said with what `grid task get` said would be
# comparing two different-length prefixes. The full oid is always in `--json`.
_OID_WIDTH = 12

# What the two tips mean, per state, and what to do about it. Dicts rather than a chain of `if`s for
# `_INTEGRATED`'s reason: an unknown state must be a lookup that MISSES — loudly, in our own code,
# where a wrong state is a bug — never an `else` that prints a plausible sentence for a case nobody
# thought about. Advice is absent, not empty, when there is nothing to do.
_DIFFERENCE = {
    "up_to_date": "(up to date)",
    "behind": "({behind} commit{s} you do not have)",
    "ahead": "(you have {ahead} commit{a} the grid does not)",
    "diverged": "({behind} commit{s} you do not have, and you have {ahead} it does not)",
}

_ADVICE = {
    "behind": "Update your files with:\n  git merge --ff-only {upstream}",
    # Never `grid project clone` here, even though re-cloning is the documented way to update: it
    # updates by resetting the branch, so on this state it is the one command that destroys the
    # very work being reported. Same wording as `project_clone`'s own refusal.
    "ahead": "Land them with:\n  grid project commit {project_id} -m '<message>' --file <path>",
    # Deliberately NO ff line and no `git pull`: the first fails here, and the second "works" by
    # making a merge commit a member can never push. Only one relay write can put the grid's copy
    # somewhere your history does not reach — every other one produces a descendant — so the cause
    # is nameable rather than a shrug.
    "diverged": ("The grid's copy of this branch is not an ancestor of yours. Either somebody ran "
                 "`grid project wip reset`,\nor you rewrote your own history here.\n"
                 "  See both sides:   git -C {path} log --left-right --oneline {branch}...{upstream}"
                 "\n  Keep yours aside: git -C {path} branch my-work {branch}"),
}

# States with no second tip to compare against, so there is no two-column block to print — one
# sentence each instead. Kept apart from `_DIFFERENCE` rather than folded in with a blank oid:
# printing `on grid` beside nothing says the grid holds an empty branch, which is a different claim
# from the grid not holding one.
_UNCOMPARABLE = {
    # ⚠️ States the fact and claims NO cause, deliberately. An earlier wording said "it appears when
    # your first task lands", which is simply false for somebody standing on a finished `task/<id>`
    # — and that is a place `grid task fetch`'s own refusal sends people.
    "no_remote_branch": ("The grid does not have {branch}, so there is nothing to compare it with.\n"
                         "A branch is on the grid once work has landed on it, and is collected "
                         "again once a task is over."),
    "detached": ("HEAD is detached, so there is no branch here to compare with the grid.\n"
                 "What the grid holds was still fetched. Check a branch out to see where it sits."),
    # Not the same fact as `no_remote_branch`: there the upstream is configured and the ref is not
    # made yet, here nothing is configured at all, so there is nothing the grid could be asked.
    "no_upstream": ("{branch} tracks nothing on the grid, so there is nothing to compare it with.\n"
                    "What the grid holds was still fetched."),
}


def _short(oid: str | None) -> str:
    return (oid or "")[:_OID_WIDTH]


def payload(found) -> dict:
    """Everything the printed report can say, as data.

    Every field, so `--json` is not a lossy version of the same command that leaves an application
    scraping stdout for the rest. `state` is carried explicitly rather than left to be derived from
    `ahead`/`behind` — three of the states cannot be expressed as a pair of counts at all, and those
    two are `None`, never `0`, when no comparison was possible.
    """
    return {"project_id": found.project_id, "path": str(found.path), "state": found.state,
            "branch": found.branch, "upstream": found.upstream,
            "local_commit": found.local_commit, "remote_commit": found.remote_commit,
            "ahead": found.ahead, "behind": found.behind,
            "refs_updated": found.refs_updated, "refs_pruned": found.refs_pruned,
            "upstream_remote": found.upstream_remote}


def render(found) -> None:
    """The human half.

    The branch is printed as a BARE heading, never called "your branch": `grid task fetch`'s own
    refusal tells members to `git checkout task/<id>` inside a clone, so the branch they are
    standing on is frequently not theirs.
    """
    from remote.project_clone import GRID_REMOTE

    print(f"project {found.project_id} in {found.path}")
    uncomparable = _UNCOMPARABLE.get(found.state)
    if uncomparable:
        print("\n" + uncomparable.format(branch=found.branch, upstream=found.upstream))
    else:
        # "on grid" is a CLAIM, and it is only true of the remote this command fetched. A branch
        # tracking anything else is still compared — the comparison is true of what is on disk —
        # but it is labelled with the remote it really came from, and said out loud below.
        elsewhere = found.upstream_remote != GRID_REMOTE
        label = f"on {found.upstream_remote}" if elsewhere else "on grid"
        counts = {"behind": found.behind, "ahead": found.ahead,
                  "s": "" if found.behind == 1 else "s", "a": "" if found.ahead == 1 else "s"}
        print(f"\n{found.branch}")
        print(f"  {'here':<9}{_short(found.local_commit)}")
        print(f"  {label:<9}{_short(found.remote_commit)}   "
              + _DIFFERENCE[found.state].format(**counts))
        if elsewhere:
            print(f"\n{found.branch} tracks {found.upstream_remote}, which this command does not "
                  f"fetch — only {GRID_REMOTE} was updated,\nso those figures are only as fresh as "
                  f"your last `git fetch {found.upstream_remote}`.")
    if found.refs_updated or found.refs_pruned:
        print(f"\n{found.refs_updated} other ref{'' if found.refs_updated == 1 else 's'} updated, "
              f"{found.refs_pruned} pruned")
    # No early return for the uncomparable states: they simply have no advice registered, and a
    # second guard saying so would silently swallow one the day somebody adds it.
    advice = _ADVICE.get(found.state)
    if advice:
        print("\n" + advice.format(upstream=found.upstream, project_id=found.project_id,
                                   branch=found.branch, path=found.path))
