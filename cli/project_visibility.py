"""`grid project share | private` — who on the grid may reach a project (ADR 0034 D-k, issue 36).

Extracted rather than added to `cli/remote_project.py`, for the reason `cli/project_archive.py` and
`cli/project_providers.py` both record: that file is already past this project's 800-line ceiling.

**Two verbs for one setting**, mirroring `archive`/`unarchive`. A person does not think "set
visibility to private"; they think "make this private". The relay carries the value as an enum on a
single route because ADR 0034 D-k leaves the set open — so a third state costs a third verb here and
no wire change at all, which is the right way round.

Both are **idempotent**, and `changed` is what separates "you did this" from "somebody had already
done it". Neither can destroy anything, so neither asks for confirmation — the asymmetry
`project_archive` draws between archive and delete does not arise here.

⚠️ **`private` is the safe direction and `share` is the one that widens.** A relay too old to have
the route answers a bare framework 404, and `_OLD_RELAY_NO_VISIBILITY` says what that silence means:
on such a relay every project is already members-only, so somebody reaching for `private` has what
they asked for and somebody reaching for `share` does not. The sentence covers both.
"""
from __future__ import annotations

import argparse

# The two values `projects.visibility` may hold, hand-duplicated with grid-src's
# `project_access.VISIBILITY_GRID` / `VISIBILITY_PRIVATE` and kept in lockstep by editing both.
#
# *Absent ⇒ grid-visible* on the reading side: every relay before this slice sends no `visibility`
# at all, so `remote_project` keys on an explicit `== VISIBILITY_PRIVATE` and never on falsiness —
# the rule `serves_you` and `archived` already follow. A rename would make `grid project list` stop
# marking private projects, and a member would find out by discovering a colleague could read one.
# `tests/test_task_lease.py` parses grid-src for both rather than restating them.
VISIBILITY_GRID = "grid"
VISIBILITY_PRIVATE = "private"


def project_share(args: argparse.Namespace) -> int:
    """Let anyone on the grid work in this project."""
    return _set_visibility(args, visibility=VISIBILITY_GRID)


def project_private(args: argparse.Namespace) -> int:
    """Restrict this project to its members."""
    return _set_visibility(args, visibility=VISIBILITY_PRIVATE)


def _set_visibility(args: argparse.Namespace, *, visibility: str) -> int:
    """Both verbs, because the reply shape and the guard are one thing (mirroring the relay)."""
    from remote import relay

    from . import remote_project

    base, token, label = remote_project._resolve(args)
    answer = relay.set_project_visibility(base, token, args.project_id, visibility=visibility)

    # ⚠️ **Emitted, then validated — never returned on** (ADR 0034 D-m, issue 46). The guard below
    # was unreachable under `--json`, which is the one mode an application drives: somebody walks
    # away believing their project is private and their colleagues can still read it, which is the
    # one failure this command exists to prevent.
    emitted = remote_project._emit(args, answer)
    if not isinstance(answer, dict) or answer.get("visibility") != visibility:
        # The house guard: a reply this command cannot read is not a state change it may report.
        # ⚠️ Compared against the value we ASKED for, never a truthiness test — a relay that omitted
        # the key, or answered `True`, would otherwise be reported as having done what was asked.
        # After which somebody walks away believing their project is private and their colleagues
        # can still read it, which is the one failure this command exists to prevent.
        raise SystemExit(
            f"The relay's answer for project {args.project_id} did not say the project was "
            f"{visibility}-visible, so this cannot be reported as one. "
            f"`grid project {'share' if visibility == VISIBILITY_GRID else 'private'} "
            f"{args.project_id} --json` shows what it sent.")
    if emitted:
        return 0

    # `changed is False` — explicit, not falsy — says a previous request had already done it. A
    # missing key means a relay that does not report the distinction, and "already" is the one thing
    # that must not be guessed: it is the difference between "somebody beat you to it" and "you did
    # this".
    already = " already" if answer.get("changed") is False else ""
    if visibility == VISIBILITY_PRIVATE:
        print(f"{args.project_id} is{already} private on {label}")
        print()
        print("Only its members can reach it. Anyone who has already worked in it stays a member.")
        # Every printed command goes at the END of its line — `test_task_lease.py` retypes each one
        # through the real parser and reads to end of line, so a command with prose after it is
        # reported as advice the CLI cannot run.
        print(f"Undo with: grid project share {args.project_id}")
    else:
        print(f"{args.project_id} is{already} shared with everyone on {label}")
        print()
        # ⚠️ `is False`, never falsiness. *Absent ⇒ served* is the only available reading: a relay
        # that answers this route at all has this slice, and one that does not answers a 404 that
        # became `_OLD_RELAY_NO_VISIBILITY` above.
        #
        # This branch exists because the setting and its EFFECT are two facts, and only one of them
        # is the column (ADR 0034 D-k). On any grid but a per-email-domain one, `visibility` is
        # written and honoured by nothing — so the sentence below used to be printed
        # unconditionally and claimed a widening that had not happened. Found in review, and it is
        # the direction that matters: `private` is accurate on such a relay, `share` was not.
        if answer.get("grid_access") is False:
            print("It is recorded, but this grid does not share projects grid-wide — only its "
                  "members can reach it, exactly as before. The setting will take effect if the "
                  "grid is ever reconfigured to share them.")
        else:
            print("Anyone signed in to this grid can now work in it without being added.")
        print(f"Restrict it again with: grid project private {args.project_id}")
    return 0
