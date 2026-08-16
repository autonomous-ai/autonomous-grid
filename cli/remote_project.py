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

from . import project_arg, project_providers, project_visibility


def cmd_remote_project(args: argparse.Namespace) -> int:
    # A project id has two spellings and one meaning (ADR 0033 D-a, issue 28). Settled once, here,
    # so every handler below reads the `args.project_id` it always read.
    args = project_arg.resolve(args)
    if args.subcommand == "create":
        return _project_create(args)
    if args.subcommand == "init":
        return _project_init(args)
    if args.subcommand == "list":
        return _project_list(args)
    if args.subcommand in ("archive", "unarchive", "delete"):
        # ADR 0033 D-p (issue 33). Their handlers live in `cli/project_archive.py` — this file is
        # already past the 800-line ceiling, the same reason `project_providers.py` was split out.
        from . import project_archive

        return {
            "archive": project_archive.project_archive,
            "unarchive": project_archive.project_unarchive,
            "delete": project_archive.project_delete,
        }[args.subcommand](args)
    if args.subcommand in ("share", "private"):
        # ADR 0034 D-k (issue 36). Their handlers live in `cli/project_visibility.py`, for the same
        # reason the three above live in `cli/project_archive.py` — imported at module scope with
        # the other two `cli.` siblings, because this file already reads its constants at :196 and
        # :702 and a second local import would be two spellings of one dependency.
        return {
            "share": project_visibility.project_share,
            "private": project_visibility.project_private,
        }[args.subcommand](args)
    if args.subcommand == "wip":
        if args.wip_action != "reset":
            raise SystemExit(f"Unknown project wip action: {args.wip_action!r}")
        return _wip_reset(args)
    if args.subcommand == "promote":
        return _project_promote(args)
    if args.subcommand == "integrate":
        return _project_integrate(args)
    if args.subcommand == "status":
        return _project_status(args)
    if args.subcommand == "check":
        return _project_check(args)
    if args.subcommand == "commit":
        return _project_commit(args)
    if args.subcommand == "import":
        return _project_import(args)
    if args.subcommand == "clone":
        return _project_clone(args)
    if args.subcommand == "refresh":
        return _project_refresh(args)
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

    Create-or-get rather than refuse-if-exists, so running it twice with one name answers with the
    project already there instead of failing. The reason used to be stronger — this same call
    resolved `grid task create`'s default project — and since ADR 0033 D-o / issue 26 it no longer
    does: a projectless task create looks its `default` UP and creates nothing. This is now the only
    thing that creates a project, which is the point of the slice.
    """
    from remote import relay

    base, token, label = _resolve(args)
    empty = bool(getattr(args, "empty", False))
    project = relay.create_project(
        base, token, name=args.name, bootstrap=relay.BOOTSTRAP_EMPTY if empty else None)
    trunk = _bootstrapped_trunk(project) if empty else None

    emitted = _emit(args, project)
    # `isinstance` before any `.get()`: `_task_oneshot` returns `resp.json()` verbatim, so a 2xx
    # carrying a JSON array, string or number — a proxy, a captive portal — reaches this line, and
    # an `AttributeError` would escape `main()` as a traceback instead of this plane's clean
    # `SystemExit`. Checked HERE rather than only inside `_bootstrapped_trunk`, because the id is
    # read on every path including the ones that never look at a trunk.
    #
    # ⚠️ Under `--json` the document has ALREADY been written, so this is a refusal after a
    # successful print — which is correct and is the same ordering the bootstrap guard below uses:
    # stdout stays one parseable document, the explanation goes to stderr, the exit code carries
    # the verdict.
    if not isinstance(project, dict):
        raise SystemExit(
            f"The relay answered {args.name!r} with something that is not a project object, so "
            f"this cannot tell you whether one was created. "
            f"`grid project create --name {args.name} --json` shows what it sent.")

    if not emitted:
        # `.get()` throughout: the reply shape is the relay's, and an older one may not send
        # every key.
        print(f"project {project.get('id') or '(no id)'} on {label}")
        print(f"name={project.get('name') or 'unknown'}")
    project_id = project.get("id") or "<id>"

    if empty:
        if trunk is None:
            # The project EXISTS — it is the trunk that did not happen — so the id is printed above
            # (or is in the JSON) before this fires, and the sentence says so. Raised AFTER `_emit`
            # so `--json`'s contract holds: stdout is still one parseable document, the explanation
            # goes to stderr, and the exit code is what an application branches on. Reporting 0
            # here would be a flag silently ignored, which is the one outcome this whole slice
            # exists to remove.
            raise SystemExit(_no_bootstrap_message(args, project_id))
        if not emitted:
            print(f"{trunk.get('trunk') or 'main'} is now at {trunk.get('commit')} on {label}")
            print()
            print(f"Next: grid task create --project {project_id} --prompt '<what to do>'")
        return 0

    if emitted:
        return 0
    # NOT `grid task create` (ADR 0033 D-o, issue 25). A project has no trunk when it is created, so
    # that advice was guaranteed to fail — the first thing a new user was told to do was the one
    # thing that could not work. The next step is a trunk, and there are two ways to get one.
    print("\nGive it a trunk — a task is cut from `main` and it has none yet:")
    print(f"  grid project init {project_id}                 # start empty")
    print(f"  grid project import <path> {project_id}        # bring an existing repository")
    return 0


def _bootstrapped_trunk(project) -> dict | None:
    """The trunk block a `--empty` create really produced, or `None` if it did not.

    `_project_init`'s house guard, applied to the nested reply: a `dict` under `bootstrap`, the
    status that route sends, and a commit. Anything else is `None` — including the case this check
    exists for, a relay that predates issue 48 and DROPPED the key while answering 201.

    `isinstance` before `.get`: `_task_oneshot` returns `resp.json()` verbatim, so a 200 carrying a
    list or a string — a proxy, a captive portal — reaches here, and an `AttributeError` would
    escape `main()` as a traceback instead of this plane's clean `SystemExit`.
    """
    if not isinstance(project, dict):
        return None
    trunk = project.get("bootstrap")
    if not isinstance(trunk, dict):
        return None
    if trunk.get("status") != "initialized" or not trunk.get("commit"):
        return None
    return trunk


def _no_bootstrap_message(args: argparse.Namespace, project_id: str) -> str:
    """What to say when `--empty` was asked for and no trunk came back.

    Deliberately NOT `relay._OLD_RELAY`, which says the relay has no projects at all: it plainly
    does — it just answered — and sending somebody to check a feature that works is the mistake
    `_OLD_RELAY_NO_VISIBILITY` was added to avoid.

    The line offered is `grid project init`, which is the same operation the relay would have run,
    and it is re-runnable: it is the trunk that is missing, not the project.
    """
    grid = getattr(args, "grid", None)
    suffix = f" --grid {grid}" if grid else ""
    return (
        f"Project {project_id} was created, but this relay ignored --empty and gave it no trunk — "
        f"it predates the bootstrap key, which it drops rather than refusing. Nothing else is "
        f"wrong, and the project is fine.\n"
        f"\n"
        f"  Give it one with the command the relay would have run:\n"
        f"    grid project init {project_id}{suffix}\n"
        f"\n"
        f"  Or bring an existing repository in instead — still possible, because nothing has "
        f"claimed the trunk yet:\n"
        f"    grid project import <path> {project_id}{suffix}")


def _project_init(args: argparse.Namespace) -> int:
    """Give an empty project a trunk — one empty root commit, as `main` (ADR 0033 D-o).

    The counterpart to `import`, and the one a new piece of work needs: before this, a project
    created from nothing could never run a task, and the only fix offered required a git repository
    the user did not have.

    Deliberately its own command rather than something `task create` does on your behalf. Import
    refuses a project that already has a trunk, so an init nobody asked for would permanently close
    the import path for that project — and nothing undoes it.
    """
    from remote import relay

    base, token, label = _resolve(args)
    answer = relay.init_project(base, token, args.project_id)

    if _emit(args, answer):
        return 0
    trunk, commit = answer.get("trunk"), answer.get("commit")
    if answer.get("status") != "initialized" or not commit:
        # The house guard: a reply this command cannot read is not an initialization it may report.
        # A trunk is created once and cannot be created again, so "probably fine" is the one thing
        # this must never print — the next command a member runs is a task create against it.
        raise SystemExit(
            f"The relay's answer for project {args.project_id} did not say the project was "
            f"initialized, so this cannot be reported as one. "
            f"`grid project init {args.project_id} --json` shows what it sent.")
    print(f"{trunk or 'main'} is now at {commit} on {label}")
    print()
    print(f"Next: grid task create --project {args.project_id} --prompt '<what to do>'")
    return 0


def _project_list(args: argparse.Namespace) -> int:
    """Every project the caller is a MEMBER of — not only the ones they own.

    Archived projects are hidden unless `--all` (ADR 0033 D-p, issue 33), because this is the
    listing somebody reads to find an id and one they archived is one they have said they are not
    working in.
    """
    from remote import relay

    base, token, label = _resolve(args)
    answer = relay.list_projects(base, token, include_archived=bool(getattr(args, "all", False)))
    projects = answer.get("projects") or []

    if _emit(args, answer):
        return 0
    if not projects:
        print(f"No projects on {label}.")
        if not getattr(args, "all", False):
            # It may not be empty — it may be entirely archived, and saying "create one" to
            # somebody who has five would be advice that makes a sixth.
            print("Archived ones are hidden; see them with: grid project list --all")
        print("\nCreate one with: grid project create --name <name>")
        return 0
    print(f"{'ID':38}  {'ROLE':8}  NAME")
    for project in projects:
        # ⚠️ `is True`, never a truthiness test. *Absent ⇒ not archived* is what a relay predating
        # this slice says, and that reading has to stay available — the same rule `serves_you`
        # follows in `project_providers.print_unserved`. A truthy test would also mark a row whose
        # `archived` a proxy had stringified to `"false"`.
        marker = "  (archived)" if project.get("archived") is True else ""
        # ⚠️ `== VISIBILITY_PRIVATE`, never falsiness (ADR 0034 D-k, issue 36). *Absent ⇒ grid* is
        # what every relay predating this slice says, and that reading has to stay available — a
        # truthy test would mark every project on an old relay as private, and the `or '?'` idiom
        # used for `role` above would be worse still, printing a guess where a fact belongs.
        if project.get("visibility") == project_visibility.VISIBILITY_PRIVATE:
            marker += "  (private)"
        print(f"{str(project.get('id') or '?'):38}  "
              f"{str(project.get('role') or '?'):8}  {project.get('name') or '?'}{marker}")
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


def _project_clone(args: argparse.Namespace) -> int:
    """A member's own working clone of a project (ADR 0033 D-h, issue 17).

    `grid task fetch` gets a task's RESULT onto disk and gets away with never writing the token into
    `.git/config` by handing it to each git child through the environment. A real clone is used by
    the member's own git — from an IDE, on a schedule, with nothing of ours in the call path — so the
    credential has to be something git can come back and ask for. It configures a helper (`grid
    credential`) in the clone's own config, scoped to that grid's relay, which also means a refreshed
    token is picked up instead of one written once expiring in place.
    """
    from pathlib import Path

    from remote import project_clone, relay

    from . import remote_grid

    # The grid's id, for pinning the clone's helper. Read from the same record `_resolve` selects,
    # so a `--grid` naming one grid can never write another's id into the config.
    rec = remote_grid._select(getattr(args, "grid", None))
    network_id = remote_grid._network_id(rec)
    base, token, label = _resolve(args)

    # Before anything is written: a git that cannot carry a Bearer credential would leave a
    # directory whose every `git pull` fails, blaming the relay.
    try:
        project_clone.require_credential_capability()
    except project_clone.CloneError as exc:
        raise SystemExit(f"Cannot clone project {args.project_id}: {exc}")

    status = relay.project_status(base, token, args.project_id)
    branch = status.get("branch")
    if not branch:
        # A reply this command cannot read is not a project — the same rule `_project_status`,
        # promote, integrate, commit and import all follow. Guessing `wip/<key>` here would put the
        # relay's ref-naming rule in a second place, free to disagree with it.
        raise SystemExit(
            f"The relay's answer for project {args.project_id} did not say which branch is yours, "
            f"so there is nothing to clone. "
            f"`grid project status {args.project_id} --json` shows what it sent.")
    trunk = status.get("trunk") or "main"

    dest = Path(args.directory) if getattr(args, "directory", None) else Path(args.project_id)
    _refuse_unusable_destination(dest, args.project_id)

    try:
        cloned = project_clone.clone_project(
            dest, url=relay.git_remote_url(base, args.project_id), project_id=args.project_id,
            branch=branch, trunk=trunk, relay_base=base, network_id=network_id)
    except project_clone.CloneError as exc:
        raise SystemExit(f"Could not clone project {args.project_id}: {exc}")

    if _emit(args, {"project_id": args.project_id, "path": str(cloned.path),
                    "branch": cloned.branch, "trunk": cloned.trunk, "grid": label,
                    "started_from_trunk": cloned.started_from_trunk}):
        return 0

    print(f"project {args.project_id} cloned into {cloned.path}")
    print(f"on {cloned.branch} (your branch); {cloned.trunk} is here too")
    if cloned.started_from_trunk:
        # Said out loud because the clone otherwise looks like somebody else's repository: the
        # relay creates a member's WIP branch when their first task settles, so until then `git log`
        # here shows only the trunk.
        print(f"Your branch has nothing on it yet, so it starts at {cloned.trunk}. It appears on "
              f"the grid when your first task lands.")
    print("\nNo credential was written: git asks grid for one each time, so a refreshed token is "
          "used automatically.")
    # Said here because it is the obvious next action inside a real clone, and because the relay's
    # refusal on its own would read as a permissions bug (ADR 0033 D-h).
    print("\n`git push` is refused. Your branch is written by the grid alone, so that a task "
          "running right now cannot have the ground moved under it. To land work from this clone:")
    print(f"  grid project commit {args.project_id} -m '<message>' --file <path>   # no agent")
    print(f"  grid project integrate {args.project_id}              # bring {cloned.trunk} in")
    print(f"  grid task create --project {args.project_id} --prompt '…'")
    return 0


def _project_refresh(args: argparse.Namespace) -> int:
    """Bring a clone's view of the grid up to date and report the difference. Touches nothing else.

    The read-only counterpart to `clone`. Re-cloning in place is the other way to update, but it
    updates by RESETTING the branch to the fetched tip, so it must refuse whenever the member has a
    local commit — which is how anybody checkpoints work between `grid project commit` calls. This
    has no such refusal because it has nothing to lose: no checkout, no merge, no reset.

    **No relay API call**, which is the reason it takes no `--grid`: the grid is already pinned
    inside the clone's own credential helper, so a flag naming a different one could not change
    anything. It is not "no network" — the fetch is an HTTP round trip to the relay's git front, and
    that front has to be reachable. What it does not need is the CONTROL PLANE, because the
    credential helper reads only the local store.

    It reports the clone against the grid's copy of the branch the member is standing on; how far
    that branch is from `main` is a different axis and belongs to `grid project status`, which asks
    the relay for it. Rendering lives in `cli/project_refresh.py`.
    """
    from pathlib import Path

    from remote import project_clone

    from . import project_refresh

    dest = Path(args.directory) if getattr(args, "directory", None) else Path.cwd()
    try:
        found = project_clone.refresh_clone(dest, project_id=args.project_id)
    except project_clone.CloneError as exc:
        raise SystemExit(f"Cannot refresh project {args.project_id}: {exc}")

    if _emit(args, project_refresh.payload(found)):
        return 0
    project_refresh.render(found)
    return 0


def _refuse_unusable_destination(dest, project_id: str) -> None:
    """A directory this command may write into, or a clean refusal naming why not.

    `git checkout -B` resets a branch to the fetched tip, which over somebody else's repository is
    silent data loss — the same hazard `grid task fetch`'s guard exists for, and it has already been
    a real defect there once. A directory a previous clone of THIS project made is the one
    exception, because re-cloning in place is how a member updates.
    """
    from remote import project_clone, task_repo

    if dest.exists() and not dest.is_dir():
        raise SystemExit(f"Cannot clone into {dest}: it exists and is not a directory.")
    if not (dest.is_dir() and any(dest.iterdir())):
        return
    held = project_clone.cloned_project(dest)
    if held is None:
        fetched = task_repo.fetched_project(dest)
        extra = (" It holds a `grid task fetch` result; clone somewhere else and use the clone "
                 "from now on." if fetched else "")
        raise SystemExit(
            f"Cannot clone into {dest}: it already has files in it and was not created by "
            f"`grid project clone`. Name a new directory.{extra}")
    if held != project_id:
        raise SystemExit(
            f"Cannot clone into {dest}: it holds project {held}, not {project_id}. "
            f"Name a different directory.")


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
    import subprocess

    from remote import relay, task_repo

    source = os.path.abspath(args.path)
    # Checked here rather than left to git, whose own message for this ("not a git repository")
    # arrives AFTER the relay has already opened an import and deleted whatever was staged. That
    # reason is why the check exists; it is not a licence to answer the question a different way.
    #
    # ⚠️ It used to be `isdir(source/".git")`, and "has a `.git` DIRECTORY" is not "is a git
    # repository". A **worktree** keeps a `.git` FILE holding a gitdir pointer, and a **bare** repo
    # has no `.git` at all — both are ordinary repositories, and both were refused. Worktrees are
    # not an exotic case here: this product's own tri-repo development runs in them, so the first
    # import anyone tried from a feature checkout failed with "there is nothing to import" while
    # pointing at 459 commits.
    #
    # So git is asked the question git owns. Local, network-free, and no more expensive than the
    # `stat` it replaces.
    probe = subprocess.run(["git", "-C", source, "rev-parse", "--git-dir"],
                           capture_output=True, text=True)
    if probe.returncode != 0:
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
    print(f"Next: grid task create --project {args.project_id} --prompt '<what to do>'")
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


def _project_status(args: argparse.Namespace) -> int:
    """Where the project is, from your side (ADR 0033 D-l, issue 19a).

    Two of these were answerable before only by performing a write. "How far behind am I" meant
    attempting a promote and reading the refusal — a call that either releases work or refuses it,
    used as a query. "What holds my slot" meant attempting a create and reading the 409.

    It is also the change signal: `main_commit` moves on a promote or an import, and every member's
    tip moves when their work settles, when a tier-1/2 integration lands, or when they commit — so
    an application watches this instead of polling `git fetch`. A CONFLICTING integration moves no
    ref at all and shows up as a held slot instead, which is why both are printed.
    """
    from remote import relay

    base, token, _label = _resolve(args)
    answer = relay.project_status(base, token, args.project_id)

    if _emit(args, answer):
        return 0
    # `.get()` throughout: the reply shape is the relay's, and an older one may not send every key.
    branch = answer.get("branch")
    if not branch:
        # A reply this command cannot read is NOT a status — the same rule promote, integrate,
        # commit and import all follow. Printing the template with blanks in it would read as "you
        # have no work", which is the one answer nobody should get from a body a proxy mangled.
        raise SystemExit(
            f"The relay's answer for project {args.project_id} did not say which branch is yours, "
            f"so this cannot be reported as a status. "
            f"`grid project status {args.project_id} --json` shows what it sent.")

    trunk = answer.get("trunk") or "main"
    print(f"project {args.project_id}")
    # ⚠️ `is True`, never truthiness (ADR 0033 D-p, issue 33). *Absent ⇒ not archived*, which is
    # every relay predating this slice — the rule `serves_you` follows. Printed FIRST because it
    # changes what every line below is worth: a member reading a healthy-looking status has no other
    # way to learn why their next `task create` will be refused.
    if answer.get("archived") is True:
        print("ARCHIVED — accepts no new work.")
        # The command goes at the END of its line, and that is a house rule rather than a style
        # choice: `test_task_lease.py` retypes every printed `grid …` hint through the real parser,
        # and it reads to end of line.
        print(f"Put it back with: grid project unarchive {args.project_id}")
    # ⚠️ `== VISIBILITY_PRIVATE`, never falsiness (ADR 0034 D-k, issue 36). *Absent ⇒ grid*, which
    # is every relay predating this slice. Only the RESTRICTED state is announced: on a relay that
    # serves D-k, grid-visible is the default and saying so on every status would be noise, while
    # "private" is a deliberate act somebody needs reminding of before they wonder why a colleague
    # cannot see their work.
    if answer.get("visibility") == project_visibility.VISIBILITY_PRIVATE:
        print("PRIVATE — only its members can reach it.")
        print(f"Share it with: grid project share {args.project_id}")
    print(f"{trunk}={answer.get('main_commit') or '(none yet)'}")
    print(f"{branch}={answer.get('wip_commit') or '(nothing yet)'}")

    ahead, behind = answer.get("ahead"), answer.get("behind")
    if ahead is None or behind is None:
        # Absent, not zero. `0/0` would read as "up to date with main", and the next thing somebody
        # does on that belief is promote a branch that does not exist.
        print(f"\nNothing to compare yet — run a task, or "
              f"`grid project commit {args.project_id} -m '<message>' --file <path>`.")
    else:
        can_promote = answer.get("can_promote")
        if can_promote is not True and can_promote is not False:
            # TRI-STATE, exactly as `_project_promote` treats `advanced` and for the same measured
            # reason: a silently-defaulted "no" is how promote once reported a release for a body a
            # proxy had stripped. Here it would print a self-contradiction — the relay defines
            # `can_promote` as exactly `behind == 0`, so a reply keeping the distances and dropping
            # it says "ahead=3 behind=0" and then "Behind main, so a promote would be refused".
            #
            # Deliberately NOT derived from `behind == 0` on this side: that would put the relay's
            # rule in a second place, and the two would then be free to disagree about a fact only
            # one of them can see.
            raise SystemExit(
                f"The relay's answer for project {args.project_id} did not say whether "
                f"{branch} can be promoted, so this cannot be reported as a status. "
                f"`grid project status {args.project_id} --json` shows what it sent.")
        print(f"\nahead={ahead}  behind={behind}")
        if can_promote:
            print(f"Ready to release: grid project promote {args.project_id} "
                  f"{answer.get('member_key') or '<member-key>'}")
        else:
            print(f"Behind {trunk}, so a promote would be refused. "
                  f"Fix it with: grid project integrate {args.project_id}")

    # `active_turns`, a LIST, since ADR 0034 D-b (issue 40): a member holds one turn per
    # conversation, so the singular `active_task` this used to read could only ever show one of
    # them. *Absent ⇒ nothing shown*, which is what a relay too old to send it produces — the key is
    # not falsily-tested for a reason (the `serves_you` rule), it is simply iterated, and an empty
    # or missing list prints nothing either way. **Roll the relay out before the CLI.**
    active = answer.get("active_turns") or []
    running = [turn for turn in active if isinstance(turn, dict) and turn.get("id")]
    if running:
        # The whole point of "what am I waiting on, and since when". Without the id there is nothing
        # to wait on, read or follow.
        print(f"\nYou have {len(running)} turn(s) in flight:")
        for turn in running:
            since = turn.get("created_at")
            print(f"  {turn['id']} ({turn.get('state') or 'unknown'}"
                  + (f", since {since}" if since else "") + ")")
        # One command, naming the OLDEST — the turn a person watching for a result is waiting on.
        # Printing one per turn would bury the queue block below it on a busy project.
        print(f"Watch the oldest with: grid task follow {running[0]['id']}")

    queue = answer.get("queue") or {}
    providers = answer.get("providers")
    if queue.get("queued") or queue.get("running"):
        # The relay's own view of why nothing is moving, in two halves: how much work is waiting
        # (the project's) and who could take it (the fleet's).
        print(f"\nproject queue: {queue.get('queued', 0)} queued, "
              f"{queue.get('running', 0)} running")
        if queue.get("oldest_queued_at"):
            print(f"oldest wait started {queue['oldest_queued_at']}")
        project_providers.print_providers(providers)
    else:
        # Nothing is waiting, so there is no fleet report to give — but "this grid does not serve
        # your domain" is not a fleet report, and an empty queue is exactly the state a member is in
        # when they follow `queue_expired`'s advice to run this command (G-01). Same helper as the
        # branch above, so the two can never answer differently.
        project_providers.print_unserved(providers)
    return 0


# What the relay says integrating WOULD do, and how to say that to a person. A dict rather than a
# chain of `if`s for `_INTEGRATED`'s reason: the one thing this must never do is print a line for a
# `status` it does not know, so an unknown one is a lookup that misses rather than an `else` that
# guesses. Every line is in the CONDITIONAL, because nothing has happened.
_WOULD_INTEGRATE = {
    "up_to_date": "{branch} already has everything on main; integrating would do nothing",
    "fast_forward": "{branch} would move straight onto main — no merge commit, nothing to review",
    "merged": "main would be merged into {branch} cleanly, leaving a merge commit",
    "merge_task": "main and {branch} both changed the same lines, so integrating would queue a "
                  "merge task for an agent to resolve",
}


def _project_check(args: argparse.Namespace) -> int:
    """Would integrating conflict? (ADR 0033 D-l, issue 19a.)

    Integration **is** the conflict check without this: asking costs your one task slot and, when
    the answer is "they conflict", queues a paid agent run to resolve them. This spends neither, and
    for the same reason it answers while you already have a task in flight — which is exactly when
    `grid project integrate` refuses you.

    Nothing here moves a ref or creates a task, so every line is written in the conditional.
    """
    from remote import relay

    base, token, _label = _resolve(args)
    answer = relay.preview_integration(base, token, args.project_id)

    if _emit(args, answer):
        return 0
    # `.get()` throughout: the reply shape is the relay's, and an older one may not send every key.
    status = answer.get("status")
    branch = answer.get("branch") or "your WIP branch"
    line = _WOULD_INTEGRATE.get(status)
    if line is None:
        # A reply this command cannot read is not a check that ran. The same rule promote and
        # integrate follow, and it matters more here: somebody acts on this by deciding NOT to
        # integrate, and a silent default would make that decision for them.
        raise SystemExit(
            f"The relay's answer for project {args.project_id} did not say what integrating would "
            f"do, so this cannot be reported as a check. "
            f"`grid project check {args.project_id} --json` shows what it sent.")

    print(line.format(branch=branch))
    ahead, behind = answer.get("ahead"), answer.get("behind")
    if ahead is not None and behind is not None:
        print(f"ahead={ahead}  behind={behind}")
    files = answer.get("files") or []
    if files:
        # The relay sends them as DATA precisely so this does not have to be dug out of a sentence.
        print(f"conflicts: {', '.join(str(path) for path in files)}")
    print(f"\nNothing has changed. Do it with: grid project integrate {args.project_id}")
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
    files = remote_task._collect_files(getattr(args, "file", None),
                                       dirs=getattr(args, "dir", None), mark_executable=True)
    deletes = list(getattr(args, "delete", None) or ())
    if not files and not deletes:
        # Refused HERE as well as by the relay, because this one is answerable without a round trip
        # and the message can name the flags rather than the wire fields.
        raise SystemExit(
            "Nothing to commit. Pass --file to write a file, --dir to write a folder, --delete to "
            "remove one, or any combination.")

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
