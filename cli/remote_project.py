"""Remote-mode `grid project create|list` and `grid project member list|add|remove` (ADR 0033 D-a).

A project has **members** now, and is addressed by **id**. Both facts need a command surface that
did not exist while a project was created as a side effect of posting a task and belonged to whoever
did:

  * creating one is an explicit act, so there is something to be a member OF;
  * listing is how anyone learns an id at all, now that `grid task create --project` takes one;
  * admitting and removing people is the membership itself.

Remote-only — `cli.dispatch` gates it (in `REMOTE_ONLY`); local mode exits with guidance, because a
local grid has neither the relay's tables nor its git plane. Import rule mirrors the other remote
handlers: stdlib only at module top; `remote.*` / sibling cli modules imported lazily.
"""
from __future__ import annotations

import argparse
import json


def cmd_remote_project(args: argparse.Namespace) -> int:
    if args.subcommand == "create":
        return _project_create(args)
    if args.subcommand == "list":
        return _project_list(args)
    if args.subcommand == "wip":
        if args.wip_action != "reset":
            raise SystemExit(f"Unknown project wip action: {args.wip_action!r}")
        return _wip_reset(args)
    if args.subcommand == "promote":
        return _project_promote(args)
    # argparse (required=True + choices) guarantees the rest is `member`; guard explicitly anyway,
    # so direct misuse of the handler fails loudly rather than falling through to the wrong verb.
    if args.subcommand != "member":
        raise SystemExit(f"Unknown project subcommand: {args.subcommand!r}")
    if args.member_action == "list":
        return _member_list(args)
    if args.member_action == "add":
        return _member_add(args)
    if args.member_action != "remove":
        raise SystemExit(f"Unknown project member action: {args.member_action!r}")
    return _member_remove(args)


def _resolve(args: argparse.Namespace) -> tuple[str, str, str]:
    """(relay_base, access_token, label) for the selected grid. Clean SystemExit if signed-out."""
    from . import remote_task

    return remote_task._resolve(args)


def _emit(args: argparse.Namespace, payload) -> bool:
    """Print `payload` as JSON when `--json` was asked for. True if it did."""
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
        return True
    return False


def _project_create(args: argparse.Namespace) -> int:
    """Create-or-get the caller's project by name, and print the **id**.

    Create-or-get rather than refuse-if-exists: the same call resolves `grid task create`'s default
    project, so being punished for running it twice would make that flow impossible.
    """
    from remote import relay

    base, token, label = _resolve(args)
    project = relay.create_project(base, token, name=args.name)

    if _emit(args, project):
        return 0
    # `.get()` throughout: the reply shape is the relay's, and an older one may not send every key.
    print(f"project {project.get('id') or '(no id)'} on {label}")
    print(f"name={project.get('name') or 'unknown'}")
    print(f"\nRun a task in it with: grid task create --project {project.get('id') or '<id>'} "
          "--prompt '…'")
    return 0


def _project_list(args: argparse.Namespace) -> int:
    """Every project the caller is a MEMBER of — not only the ones they own."""
    from remote import relay

    base, token, label = _resolve(args)
    answer = relay.list_projects(base, token)
    projects = answer.get("projects") or []

    if _emit(args, answer):
        return 0
    if not projects:
        print(f"No projects on {label}.")
        print("\nCreate one with: grid project create --name <name>")
        return 0
    print(f"{'ID':38}  {'ROLE':8}  NAME")
    for project in projects:
        print(f"{str(project.get('id') or '?'):38}  "
              f"{str(project.get('role') or '?'):8}  {project.get('name') or '?'}")
    return 0


def _member_list(args: argparse.Namespace) -> int:
    """Who is in a project, including the **member key** each one is removed by.

    The key is printed rather than the user id because it is what `member remove` takes, and it is
    what it takes because `grid:<network>:<sub>` is not a path segment (ADR 0033 D-a).
    """
    from remote import relay

    base, token, _label = _resolve(args)
    answer = relay.list_project_members(base, token, args.project_id)
    members = answer.get("members") or []

    if _emit(args, answer):
        return 0
    if not members:
        # Not reachable through this CLI — a project always has its owner — so say what it means
        # rather than printing an empty table that reads like a display bug.
        print(f"Project {args.project_id} lists no members.")
        return 0
    print(f"{'MEMBER KEY':34}  {'ROLE':8}  EMAIL")
    for member in members:
        print(f"{str(member.get('member_key') or '?'):34}  "
              f"{str(member.get('role') or '?'):8}  {member.get('email') or '(unknown)'}")
    return 0


def _member_add(args: argparse.Namespace) -> int:
    """Admit someone by email. Only the project's owner may."""
    from remote import relay

    base, token, _label = _resolve(args)
    member = relay.add_project_member(base, token, args.project_id, email=args.email)

    if _emit(args, member):
        return 0
    print(f"{member.get('email') or args.email} is in project {args.project_id}")
    print(f"member_key={member.get('member_key') or 'unknown'}")
    return 0


def _wip_reset(args: argparse.Namespace) -> int:
    """Move a member's WIP branch back to a named commit (ADR 0033 D-c).

    The recovery path for a settle that was interrupted between its git write and its terminal
    transaction: the WIP branch is then ahead of a task branch the relay has reset, every later
    attempt is a non-fast-forward, and — worse — that member's next task is cut from the lost
    attempt's work.

    Any project member may reset any member's branch, matching promote: the moment somebody leaves
    the team nobody else could move `wip/<departed>`, and there is no adopt or transfer operation.

    Refused by the relay while that member has an active task, because a reset landing mid-task
    would move the base out from under an attempt in flight.
    """
    from remote import relay

    base, token, _label = _resolve(args)
    answer = relay.reset_project_wip(base, token, args.project_id,
                                     member_key=args.member_key, commit=args.commit)

    if _emit(args, answer):
        return 0
    # `.get()` throughout: the reply shape is the relay's, and an older one may not send every key.
    print(f"{answer.get('branch') or 'the WIP branch'} is now at "
          f"{answer.get('commit') or args.commit}")
    previous = answer.get("previous_commit")
    if previous:
        print(f"was={previous}")
    return 0


def _project_promote(args: argparse.Namespace) -> int:
    """Fast-forward the project's `main` from a member's WIP branch (ADR 0033 D-b).

    `main` is the release branch: no task touches it, and this is the one thing that moves it. It
    goes through the relay rather than a push because the relay being `main`'s only writer is what
    makes a provider unable to announce its own success.

    The source is NAMED. Any member may promote any member's branch — including a departed one's,
    which is the whole reason it is not "your own": nothing else can ever move `wip/<departed>`.

    Fast-forward only, so a branch that is behind is refused and integration is the fix. The
    refusal carries how far behind, and the relay's own sentence is what is shown.
    """
    from remote import relay

    base, token, _label = _resolve(args)
    answer = relay.promote_project(base, token, args.project_id, member_key=args.member_key)

    if _emit(args, answer):
        return 0
    # `.get()` throughout: the reply shape is the relay's, and an older one may not send every key.
    branch = answer.get("branch") or "main"
    commit = answer.get("commit") or ""
    advanced = answer.get("advanced")
    if advanced is False:
        # Said plainly rather than printed as a move. A team reading "main is now at <oid>" after a
        # no-op has been told something shipped when nothing did.
        print(f"{branch} is already at {commit}; nothing to promote")
        return 0
    if advanced is not True or not commit:
        # Anything that is not one of the two answers this command knows how to report is a reply it
        # cannot read, NOT a release. Defaulting to the success line here printed `main is now at `
        # — with no commit — and exited 0 for a body a proxy had stripped, telling a human and a
        # script alike that work had shipped. There is no relay old enough to be a reason to be
        # lenient: promote is a new route, so every relay that has it sends both keys.
        raise SystemExit(
            f"The relay's answer to promoting {args.member_key} in project {args.project_id} did "
            f"not say whether {branch} moved, so this cannot be reported as a release. "
            f"`grid project promote {args.project_id} {args.member_key} --json` shows what it sent.")
    print(f"{branch} is now at {commit}")
    previous = answer.get("previous_commit")
    if previous:
        # A fast-forward leaves no merge commit, so this line is the only place the release it
        # replaced is named — and there is no revert command, so somebody undoing this needs it.
        print(f"was={previous}")
    if answer.get("promotion_id") is None:
        # The trunk moved and the row naming who released it did not get written — the relay says so
        # here and nowhere else a person will look. Left unsaid, it is discoverable only by grepping
        # the relay's log, or by somebody later finding a hole in the project's release history.
        # Not an error: the release happened, and calling it a failure would invite a second promote
        # that records nothing either.
        print("warning: the relay could not record who promoted this; "
              "it will be missing from the project's release history")
    return 0


def _member_remove(args: argparse.Namespace) -> int:
    """Remove someone by member key. Takes effect on their very next request."""
    from remote import relay

    base, token, _label = _resolve(args)
    answer = relay.remove_project_member(base, token, args.project_id,
                                         member_key=args.member_key)

    if _emit(args, answer):
        return 0
    print(f"removed {args.member_key} from project {args.project_id}")
    return 0
