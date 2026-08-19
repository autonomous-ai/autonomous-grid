"""`grid project archive | unarchive | delete` (ADR 0033 D-p, issue 33).

Extracted from `cli/remote_project.py` rather than added to it, for the reason
`cli/project_providers.py` records: that file is already past this project's 800-line ceiling, and
three more handlers would take it further. One concern lives here — taking a project out of the way,
and getting rid of one — and the two halves are deliberately not symmetrical.

**Archive is reversible and can destroy nothing.** It is the operation a member should reach for,
and it asks nothing: the repository is kept, every read still answers, and `unarchive` puts it back.

**Delete is irreversible**, and the relay refuses it for any project that has a trunk or has ever
had a task — so the guarantee that nothing is destroyed is the RELAY's. The confirmation this module
asks for is about the caller's *intent*, never about safety: it exists because a mistyped id in an
irreversible command is a different kind of accident from a mistyped id in a reversible one, and
because the reply names the project so a person can see which one they are about to remove.
"""
from __future__ import annotations

import argparse


def project_archive(args: argparse.Namespace) -> int:
    """Archive a project — it accepts no new work and leaves `grid project list`."""
    return _set_archived(args, archived=True)


def project_unarchive(args: argparse.Namespace) -> int:
    """Put an archived project back."""
    return _set_archived(args, archived=False)


def _set_archived(args: argparse.Namespace, *, archived: bool) -> int:
    """Both verbs, because the reply shape and the guard are one thing (mirroring the relay)."""
    from remote import relay

    from . import remote_project

    base, token, label = remote_project._resolve(args)
    call = relay.archive_project if archived else relay.unarchive_project
    answer = call(base, token, args.project_id)

    # ⚠️ **Emitted, then validated — never returned on** (ADR 0034 D-m, issue 46). The guard below
    # was unreachable under `--json`, which is the one mode an application drives: a body that never
    # said so exited 0 there, and the member walks away believing their project takes no new work.
    emitted = remote_project._emit(args, answer)
    if not isinstance(answer, dict) or answer.get("archived") is not archived:
        # The house guard: a reply this command cannot read is not a state change it may report.
        # ⚠️ `is not archived`, never a truthiness test. `archived` is a boolean on this wire, and
        # a relay that answered `"true"` or omitted the key would otherwise be reported as having
        # done what was asked — after which the member walks away believing their project takes no
        # new work, and their next `task create` succeeds.
        raise SystemExit(
            f"The relay's answer for project {args.project_id} did not say the project was "
            f"{'archived' if archived else 'unarchived'}, so this cannot be reported as one. "
            f"`grid project {'archive' if archived else 'unarchive'} {args.project_id} --json` "
            f"shows what it sent.")
    if emitted:
        return 0

    verb = "archived" if archived else "unarchived"
    # `changed is False` — explicit, not falsy — says a previous request had already done it. A
    # missing key means a relay that does not report the distinction, and "already" is the one thing
    # that must not be guessed: it is the difference between "somebody beat you to it" and "you did
    # this".
    already = " already" if answer.get("changed") is False else ""
    print(f"{args.project_id} is{already} {verb} on {label}")
    print()
    if archived:
        print("Its repository is untouched: cloning, status and fetching still work, and any task "
              "already running will finish normally.")
        # Every printed command goes at the END of its line — `test_task_lease.py` retypes each one
        # through the real parser and reads to end of line, so a command with prose after it is
        # reported as advice the CLI cannot run.
        print(f"Undo with: grid project unarchive {args.project_id}")
    else:
        print(f"Next: grid task create --project {args.project_id} --prompt '<what to do>'")
    return 0


def project_delete(args: argparse.Namespace) -> int:
    """Delete a project that has nothing in it. Irreversible, and refused for anything else."""
    from remote import relay
    from shared.launch import system

    from . import remote_project

    if not args.yes:
        if remote_project._emit_quietly(args):
            # ⚠️ **`--json` means there is nobody to ask** (ADR 0034 D-m, issue 46). Found in
            # review, and it was the worst shape in the plane: an application spawning this binary
            # has no terminal, so `input()` read EOF, `confirm` answered False, and the command
            # printed an interactive prompt to stdout and exited **0** — which a client reading the
            # status takes as *the project was deleted*. "Declined" and "done" were the same answer
            # on the one irreversible verb here.
            #
            # Refused rather than confirmed: a flag that means "I meant it" cannot be inferred from
            # the absence of a terminal. The exit code is non-zero and `cli/json_error.py` puts the
            # sentence where a program can read it.
            raise SystemExit(
                f"Deleting project {args.project_id} cannot be confirmed with --json, because there "
                f"is nobody to ask. Pass --yes to say you meant it: "
                f"grid project delete {args.project_id} --yes --json")
        if not system.confirm(
                f"Permanently delete project {args.project_id} and its repository?"):
            # A clean refusal, not an error: the caller declined. `system.confirm` answers False for
            # EOF and for Ctrl-C too, so a person who hits Ctrl-D is refused rather than being taken
            # to have agreed — which is the direction that matters for an irreversible command.
            print("Nothing was deleted.")
            return 0

    base, token, label = remote_project._resolve(args)
    answer = relay.delete_project(base, token, args.project_id)

    # Emitted, then validated — see `_set_archived` above (ADR 0034 D-m, issue 46).
    emitted = remote_project._emit(args, answer)
    if not isinstance(answer, dict) or answer.get("deleted") is not True:
        raise SystemExit(
            f"The relay's answer for project {args.project_id} did not say the project was "
            f"deleted, so this cannot be reported as one. "
            f"`grid project delete {args.project_id} --yes --json` shows what it sent.")
    if emitted:
        return 0

    print(f"{args.project_id} is deleted on {label}")
    # ⚠️ `is False`, never falsiness. *Absent ⇒ nothing to report*, which is a relay that does not
    # send the key; an explicit `False` is the relay saying the rows went but the directory did not,
    # and staying silent about that would leave an orphaned repository on a disk nobody is watching.
    # The rows are gone either way, so this is a note for an operator, not a failure for the caller.
    if answer.get("repository_removed") is False:
        print("\nIts repository could not be removed and is still on the relay's disk. "
              "The project itself is gone — tell whoever runs this grid, so they can reclaim it.")
    return 0
