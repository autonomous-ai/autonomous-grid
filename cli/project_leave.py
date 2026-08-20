"""`grid project leave` — taking yourself off a project (ADR 0035 D-b … D-g, issue 56).

Extracted rather than added to `cli/remote_project.py`, for the reason `cli/project_rename.py` and
`cli/project_archive.py` both record: that file is already past this project's 800-line ceiling.

**The command names only the project, and that is the feature.** Before this route the way off a
project was to ask its owner, and the do-it-yourself version was `grid project member list`, find
your own email in the table, copy a 32-hex key, `grid project member remove` — four steps and a
lookup, for a thing a person should be able to do in one. Identity comes from the token, so there is
nothing here to look up and no body to send.

## `--yes` is a flag and not a prompt, and the two are not interchangeable

`grid project delete` asks, and falls back to `--yes` when there is nobody to ask. This one does not
ask at all (ADR 0035 D-g). A confirmation somebody **declines** exits **0**, and 0 on this plane
means *done* — so a script driving this CLI would read a refused departure as a completed one, and
the person is still a member of a project their tooling believes they left.

⚠️ The refusal you get without `--yes` **is** the confirmation text. It is the only moment this
command has to tell somebody what they are agreeing to, so it says the part that is not obvious:
leaving takes your own history in that project with you.

## What leaving does not do

Nothing of the leaver's is stopped or thrown away — no task is cancelled and no work is collected
(ADR 0035 D-c). The whole of the effect is that the next request is refused. That is worth printing
rather than assuming, because "leave" is a word people expect to be destructive, and somebody who
believes it cancelled their running task will not go and stop it.
"""
from __future__ import annotations

import argparse


def project_leave(args: argparse.Namespace) -> int:
    """Take yourself off a project, and say what that did and did not do."""
    from remote import relay

    from . import remote_project

    if not args.yes:
        # ⚠️ **Before `_resolve`, so nothing is sent and nothing is even looked up.** Refused rather
        # than confirmed, in every mode: there is no prompt to fall back to here, deliberately (see
        # the module docstring). The exit code is non-zero, and `cli/json_error.py` puts the
        # sentence where a program can read it — which is the half `grid project delete` had to
        # learn in review, where declining and succeeding were the same answer.
        #
        # This sentence is the whole of the confirmation, so it carries the consequence a person
        # cannot see coming rather than only naming the flag.
        raise SystemExit(
            f"Leaving project {args.project_id} needs --yes, to say you meant it. Your tasks in it "
            f"keep running and nothing of yours is thrown away, but the project stops answering to "
            f"you and your tasks in it leave `grid task list`. Only its owner can add you back. "
            f"To go ahead: grid project leave {args.project_id} --yes")

    base, token, label = remote_project._resolve(args)
    answer = relay.leave_project(base, token, args.project_id)

    # ⚠️ **Emitted, then validated — never returned on** (ADR 0034 D-m, issue 46). The guard below
    # was unreachable under `--json` in the commands this one is modelled on, which is the one mode
    # an application drives: a body that never said so exited 0 there, and the caller walks away
    # believing they had left.
    emitted = remote_project._emit(args, answer)
    if not isinstance(answer, dict) or answer.get("left") is not True:
        # The house guard: a reply this command cannot read is not a state change it may report.
        # ⚠️ `is not True`, never a truthiness test — `left` is a boolean on this wire, and a relay
        # that answered `"true"` or omitted the key would otherwise be reported as a departure.
        # After which somebody stops watching a project they are still in, and their colleagues go
        # on sending them work.
        raise SystemExit(
            f"The relay's answer for project {args.project_id} did not say you had left it, so "
            f"this cannot be reported as one. You may still be a member — check with: "
            f"grid project list")
    if emitted:
        return 0

    print(f"You have left project {args.project_id} on {label}")
    print()
    print("Nothing of yours was stopped or thrown away — any task you already asked for runs and "
          "finishes normally. What changes is that the project stops answering to you, and your "
          "own tasks in it are no longer shown by: grid task list")
    print()
    # Every printed command goes at the END of its line — `test_task_lease.py` retypes each one
    # through the real parser and reads to end of line, so a command with prose after it is
    # reported as advice the CLI cannot run.
    #
    # The OWNER's command, named rather than offered: the person reading this has just lost the
    # ability to run it, and cannot even see the project any more. Printing the id is what makes it
    # useful — it is the one thing they can still hand to somebody else, and after leaving they have
    # no way to look it up again.
    print("Only the project's owner can put you back, with: "
          f"grid project member add {args.project_id} --email <your address>")
    return 0
