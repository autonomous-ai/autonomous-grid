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
    if args.subcommand == "integrate":
        return _project_integrate(args)
    if args.subcommand == "commit":
        return _project_commit(args)
    if args.subcommand == "import":
        return _project_import(args)
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


def _project_import(args: argparse.Namespace) -> int:
    """Bring an existing repository into an empty project (ADR 0033 D-f, issue 16b).

    Three steps, and the middle one is a real `git push` rather than an HTTP call this CLI makes:

      1. ask the relay to open an import — it answers with the staging ref to push to;
      2. push the local repository there over the project's own smart-HTTP front;
      3. ask the relay to check it and set `main`.

    Step 3 is the slow one. The relay reads every tree the pushed history reaches before it will let
    it become the trunk — measured at 18.17s on a 28,666-commit repository — so the wait is expected
    and is said out loud rather than looking like a hang.

    **A refused import leaves the project with no trunk**, which is deliberate and is a state the
    rest of the system already explains: `grid task create` refuses it with a message naming import.
    Half a trunk would be far worse, because `main` is the one ref nothing in this design rewrites.
    """
    import os

    from remote import relay, task_repo

    source = os.path.abspath(args.path)
    if not os.path.isdir(os.path.join(source, ".git")):
        # Checked here rather than left to git, whose own message for this ("not a git repository")
        # arrives AFTER the relay has already opened an import and deleted whatever was staged.
        raise SystemExit(f"{source} is not a git repository, so there is nothing to import.")

    base, token, _label = _resolve(args)
    opened = relay.open_project_import(base, token, args.project_id)
    ref = opened.get("ref")
    if not ref:
        raise SystemExit(
            f"The relay did not say where to push the repository for project {args.project_id}, "
            f"so nothing was uploaded. "
            f"`grid project import {args.path} {args.project_id} --json` shows what it sent.")

    url = relay.git_remote_url(base, args.project_id)
    if not _emit_quietly(args):
        print(f"Pushing {source} ({args.branch}) to project {args.project_id}…")
    try:
        commit = task_repo.push_import(source, url=url, token=token,
                                       local_ref=args.branch, remote_ref=ref)
    except Exception as exc:
        # `push_import` raises `CheckoutError`, a bare `RuntimeError` — so without this the one step
        # of this command that is not an HTTP call is also the one that ends in a traceback instead
        # of a sentence. Steps 1 and 3 go through `relay._task_oneshot`, which turns every failure
        # into a clean `SystemExit`; this matches them, and matches `remote_task`'s own fetch
        # handler, which wraps its `checkout_result` call for exactly this reason.
        #
        # Broad, like that one: a push can fail as a `CheckoutError`, as an `OSError` on the source
        # directory, or as whatever git's absence produces, and the user wants the reason rather
        # than the class. Nothing sensitive travels in it — the token is in the environment, never
        # in argv or the URL, so `_run`'s error text cannot carry it.
        raise SystemExit(
            f"Could not push {source} to project {args.project_id}: {exc}\n"
            f"Nothing was imported; the project still has no {opened.get('trunk') or 'main'}.")
    if not _emit_quietly(args):
        print(f"Pushed {commit}. Checking it before it becomes "
              f"{opened.get('trunk') or 'main'} — this reads the whole history and can take a "
              f"while…")

    answer = relay.finish_project_import(base, token, args.project_id)

    if _emit(args, answer):
        return 0
    status = answer.get("status")
    imported = answer.get("commit")
    if status != "imported" or not imported:
        # The same rule promote and integrate follow: a reply this command cannot read is not an
        # import. Reporting one would tell a person their team's history is on the relay when the
        # only evidence is a body that did not say so.
        raise SystemExit(
            f"The relay's answer for project {args.project_id} did not say the repository was "
            f"imported, so this cannot be reported as one. "
            f"`grid project import {args.path} {args.project_id} --json` shows what it sent.")
    print(f"{answer.get('trunk') or 'main'} is now at {imported}")
    for warning in answer.get("warnings") or ():
        # Printed VERBATIM. These are the relay's words about a repository this CLI never read, and
        # the only one today (Git LFS) is a fact about what an agent will find rather than an error.
        print(f"warning: {warning}")
    print()
    print(f"Next: grid task create --project {args.project_id} \"<prompt>\"")
    return 0


def _emit_quietly(args: argparse.Namespace) -> bool:
    """Whether progress lines should be withheld — `--json` output must stay parseable."""
    return bool(getattr(args, "json", False))


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


# What the relay can say happened, and how to say it to a person. A dict rather than a chain of
# `if`s so the one thing this command must never do — print a line for a `status` it does not know —
# is a lookup that misses rather than an `else` that guesses.
_INTEGRATED = {
    "up_to_date": "{branch} already has everything on main; nothing to integrate",
    "fast_forward": "{branch} moved onto main at {commit}",
    "merged": "main was merged into {branch}; the merge commit is {commit}",
    # Tier 3 (ADR 0033 D-e, issue 15). Nothing has moved yet — an agent is about to.
    "merge_task": "main and {branch} changed the same lines, so task {task_id} will merge them",
}

# The statuses whose report needs a COMMIT to be meaningful, and the one that needs a task id
# instead. A reply missing the field its own status is about is a reply this command cannot read —
# not an integration that succeeded — and saying so is the difference between a person waiting for a
# task that exists and a person believing work landed that did not.
_NEEDS_COMMIT = ("fast_forward", "merged")


def _project_integrate(args: argparse.Namespace) -> int:
    """Bring the project's `main` into the caller's own WIP branch (ADR 0033 D-d/D-e).

    The counterpart to promote. Because `main` moves only on a promote, the first one leaves every
    other member unable to promote at all — their branch was cut from a trunk that is now history —
    and this is the only way back.

    **Your own branch, so there is no member key to name.** The relay holds your one task slot while
    it works, by inserting a task row: that INSERT is what stops an integration moving the branch a
    task of yours is running on. So integrating is refused while you have a task in flight, and the
    refusal names it.

    Four outcomes, and they are deliberately different sentences: already up to date, a
    fast-forward, a real merge commit, and — when you and somebody else changed the same lines — a
    **merge task** an agent runs to resolve it. That last one costs a slot and an agent run, and
    nothing has moved when this command returns.
    """
    from remote import relay

    base, token, _label = _resolve(args)
    answer = relay.integrate_project(base, token, args.project_id)

    if _emit(args, answer):
        return 0
    # `.get()` throughout: the reply shape is the relay's, and an older one may not send every key.
    status = answer.get("status")
    branch = answer.get("branch") or "your WIP branch"
    commit = answer.get("commit") or ""
    task_id = answer.get("task_id") or ""
    line = _INTEGRATED.get(status)
    if line is None or (status in _NEEDS_COMMIT and not commit) or (
            status == "merge_task" and not task_id):
        # A reply this command cannot read is NOT an integration that succeeded. Printing the
        # success line by default is how promote once reported work as landed for a body a proxy
        # had stripped — and the next thing somebody does on that belief is promote. There is no
        # relay old enough to be a reason for leniency: integrate is a new route, so every relay
        # that has it sends `status`.
        #
        # The per-status field check is what makes that guard mean anything for tier 3: a merge-task
        # reply's whole payload IS the id, so one without it leaves nothing to watch or wait on.
        raise SystemExit(
            f"The relay's answer to integrating project {args.project_id} did not say what it did, "
            f"so this cannot be reported as an integration. "
            f"`grid project integrate {args.project_id} --json` shows what it sent.")
    print(line.format(branch=branch, commit=commit, task_id=task_id))
    if status == "merge_task":
        files = answer.get("files") or []
        if files:
            # What the agent is about to change. The relay sends them as DATA precisely so this does
            # not have to be dug out of a sentence.
            print(f"conflicts: {', '.join(str(path) for path in files)}")
        print(f"\nNothing has moved yet. Watch it with: grid task follow {task_id}")
        print(f"When it finishes, promote with: grid project promote {args.project_id} "
              f"{answer.get('member_key') or '<member-key>'}")
        return 0
    previous = answer.get("previous_commit")
    if answer.get("advanced") and previous:
        # Where the branch was before. There is no revert, so somebody putting it back needs it —
        # and `grid project wip reset` is the command that takes it.
        print(f"was={previous}")
    return 0


def _project_commit(args: argparse.Namespace) -> int:
    """Put a change into the project without running an agent (ADR 0033 D-j).

    The answer to "the agent got it 90% right, let me fix the last line" — which at team scale is the
    most frequent action of a working day, and which the rest of this design otherwise answers with a
    whole agent run that may change the very line being fixed.

    **Your own branch, so there is no member key to name**, exactly like integrate. The relay holds
    your one task slot while it commits, which is what stops a commit landing under a task of yours
    that is already running — so this is refused while you have one in flight, and the refusal names
    it.

    An executable bit is **kept** without being asked for: a file already in the project as
    executable stays executable when you edit it, and a local file that is executable makes the
    committed one executable too.
    """
    from remote import relay

    from . import remote_task

    # Read the files BEFORE resolving the grid, for `_task_create`'s reason: a typo in a filename
    # should not first cost a credential lookup and a control-plane round trip to discover.
    files = remote_task._collect_files(getattr(args, "file", None), mark_executable=True)
    deletes = list(getattr(args, "delete", None) or ())
    if not files and not deletes:
        # Refused HERE as well as by the relay, because this one is answerable without a round trip
        # and the message can name the flags rather than the wire fields.
        raise SystemExit(
            "Nothing to commit. Pass --file to write a file, --delete to remove one, or both.")

    base, token, _label = _resolve(args)
    answer = relay.commit_project(base, token, args.project_id,
                                  message=args.message, files=files, deletes=deletes)

    if _emit(args, answer):
        return 0
    # `.get()` throughout: the reply shape is the relay's, and an older one may not send every key.
    commit = answer.get("commit")
    branch = answer.get("branch") or "your WIP branch"
    if not commit:
        # A reply this command cannot read is NOT a commit — the same rule promote, integrate and
        # import follow. Printing the success line by default is how promote once reported work as
        # landed for a body a proxy had stripped, and the next thing somebody does on that belief is
        # promote. There is no relay old enough to be a reason for leniency: commit is a new route,
        # so every relay that has it sends the commit.
        raise SystemExit(
            f"The relay's answer to committing in project {args.project_id} did not say what it "
            f"wrote, so this cannot be reported as a commit. "
            f"`grid project commit {args.project_id} … --json` shows what it sent.")
    print(f"{branch} is now at {commit}")
    previous = answer.get("previous_commit")
    if previous:
        # There is no revert, and `grid project wip reset` is the way back — which takes exactly
        # this. Left unsaid, undoing a mistyped commit means finding the old oid by hand.
        print(f"was={previous}")
    wrote = answer.get("files") or []
    removed = answer.get("deletes") or []
    if wrote:
        print(f"wrote: {', '.join(str(path) for path in wrote)}")
    if removed:
        print(f"deleted: {', '.join(str(path) for path in removed)}")
    print(f"\nRelease it with: grid project promote {args.project_id} "
          f"{answer.get('member_key') or '<member-key>'}")
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
