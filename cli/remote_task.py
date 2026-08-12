"""Remote-mode `grid task create|get|follow|fetch` — hand the grid a coding task, read it back.

A task (ADR 0032) is work that outlives the request that created it: the client posts a prompt, a
provider claims it and runs an agent against it, and the client comes back later for the result.
That is why this is two commands and not one streaming call — there is nothing to hold open.

Each call resolves the grid + relay base + per-grid access token exactly as `price`/`router` do, and
talks to the relay's `/relay/v1/tasks`. Remote-only — `cli.dispatch` gates it (in `REMOTE_ONLY`);
local mode exits with guidance. Import rule mirrors the other remote handlers: stdlib only at module
top; `remote.*` / sibling cli modules imported lazily.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import json
import sys
from pathlib import Path
from typing import Any

# Bounded reattach: a relay that is genuinely gone must end the command rather than spin. The budget
# is per OUTAGE, not per task — any event received resets it, so a long task that blips repeatedly
# is not slowly starved of retries.
_RECONNECT_ATTEMPTS = 5
_RECONNECT_BACKOFF_SECONDS = 2.0

# Local upload bounds, refused HERE rather than after a multi-megabyte POST the relay then rejects.
# LOCKSTEP with grid-src `task_files.MAX_FILE_BYTES` / `MAX_TOTAL_BYTES` / `MAX_FILES`: the relay is
# the authority and refuses anything over its own limits regardless, so a client that drifts LOW
# merely refuses early with a clear message, and one that drifts HIGH pays for the upload before
# being told no. Neither corrupts anything, which is why these are duplicated rather than negotiated.
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_FILES = 200

# The states in which a task has stopped and its branch is final. LOCKSTEP with grid-src's
# `tasks.TERMINAL_STATES`, and this half is deliberately CONSERVATIVE: a state this build has never
# heard of is treated as not-yet-finished, so a newer relay's extra state refuses a fetch rather
# than handing back the input files as though they were the result.
_TERMINAL_STATES = frozenset({"completed", "failed", "timed_out"})

# The terminal reason meaning "no provider ever claimed this task" (ADR 0033 D-k, issue 18).
# LOCKSTEP with grid-src's `task_reaper.QUEUE_EXPIRED`, kept in step by editing both repos;
# `tests/test_task_lease.py` parses that module rather than restating the string.
#
# ⚠️ The ONE terminal `error` this client branches on rather than printing verbatim, and the reason
# is that `queue_expired` and `deadline_exceeded` are one word apart and call for OPPOSITE actions —
# add providers, or fix the task. The slug is still printed either way; what the branch adds is the
# sentence saying which of the two a reader is looking at.
#
# *Absent ⇒ an old relay sends `deadline_exceeded` as it always did*, so nothing here changes; a
# newer relay against an older CLI prints the new slug with no sentence. No rollout order.
QUEUE_EXPIRED = "queue_expired"
# Phrased about the BUDGET, not about the attempt count, because `queue_expired` does not mean
# `attempt == 0`. A task whose provider died is put back on the queue clock, so one that nobody
# picks up again ends this way having already run once — and "no provider ever claimed it" would be
# flatly false for exactly the row a team stares at when a fleet is flapping.
_QUEUE_EXPIRED_NOTE = ("it ran out of time WAITING for a provider rather than while running — the "
                       "grid is short of task capacity, not the task at fault. "
                       "`grid project status` says who is online and paused.")


def _resolve(args: argparse.Namespace) -> tuple[str, str, str]:
    """(relay_base, access_token, label) for the selected grid. Clean SystemExit if signed-out."""
    from remote import credentials

    from . import remote_grid

    session = credentials.require_session()
    rec = remote_grid._select(getattr(args, "grid", None))
    network_id = remote_grid._network_id(rec)
    label = rec.get("name") or network_id
    token = rec.get("access_token")
    if not token:
        raise SystemExit(
            f"Grid {label} has no access token locally. Run `grid login` to refresh your grids.")
    base, _status = remote_grid.resolve_relay_base(session, rec, network_id, label)
    return base, token, label


def cmd_remote_task(args: argparse.Namespace) -> int:
    if args.subcommand == "create":
        return _task_create(args)
    if args.subcommand == "follow":
        return _task_follow(args)
    if args.subcommand == "fetch":
        return _task_fetch(args)
    if args.subcommand == "list":
        return _task_list(args)
    if args.subcommand == "cancel":
        return _task_cancel(args)
    # argparse (required=True + choices) guarantees the rest is `get`; guard explicitly anyway, so
    # direct misuse of the handler fails loudly rather than falling through to the wrong verb.
    if args.subcommand != "get":
        raise SystemExit(f"Unknown task subcommand: {args.subcommand!r}")
    return _task_get(args)


def _collect_files(specs: list[str] | None, *, mark_executable: bool = False) -> list[dict]:
    """Read each `--file LOCAL[:DEST]` into the wire shape, or exit with a sentence naming the file.

    Every refusal here is local and happens BEFORE the relay is contacted. Two of them cannot be
    delegated to it at all:

      * **A symlink.** The wire carries `{path, content}`, so a symlink is not representable and the
        relay has nothing to detect. Following it and uploading the TARGET would be worse than
        refusing — it silently uploads a file the user never named, and the classic target of a
        planted link is a private key. (The relay's half of this rule is structural: it writes mode
        `100644` and nothing else, so a caller that is not this CLI still cannot create one.)
      * **A directory or an unreadable file.** The relay never sees these; they are facts about this
        machine.

    Path RULES — `..`, `.git/`, absolute — are deliberately NOT re-implemented here. The relay is
    the sole authority on them, and a second copy in another repo drifts silently: each side keeps
    working, just not identically, and the gap is a path one accepts and the other does not.

    `mark_executable` sends the `executable` boolean (ADR 0033 D-j), and it is **set-only**: `True`
    for a local file with an exec bit, and the key OMITTED otherwise. Never `False`, because absent
    means "inherit the mode already in the project" and `False` means "make this a regular file" —
    so a local copy that lost its bit (a zip, a Windows share, a `curl -O`) would silently strip the
    bit on the server, which is the exact defect this field exists to fix. Clearing one is therefore
    not expressible from this CLI; the relay's API takes `false` if it is ever wanted.

    **Off by default, and that is a rollout constraint rather than a taste.** `grid task create`
    calls this with no flag, so its wire shape is byte-identical to before — and it has to be:
    `task_files.parse_files` refuses unknown keys, so a relay predating this release answers 422
    rather than dropping the field, and sending it from task create would break a command that works
    today. Task uploads still stop stripping executable bits, because that half is the relay reading
    the base tree and needs nothing from here.
    """
    if not specs:
        # No key at all rather than an empty list: a relay predating the git plane must not receive
        # a field it does not understand for a task that has no files.
        return []
    if len(specs) > MAX_FILES:
        raise SystemExit(f"Too many files: {len(specs)} (the limit is {MAX_FILES}).")

    files: list[dict] = []
    total = 0
    for spec in specs:
        local, dest = _split_spec(spec)
        source = Path(local)

        # `is_symlink` BEFORE any other probe: `exists()` and `is_file()` both follow the link, so
        # checking them first would report a planted symlink as a perfectly ordinary file.
        if source.is_symlink():
            raise SystemExit(
                f"Refusing to upload {local}: it is a symlink, and uploading what it points at "
                f"would send a file you did not name. Pass the target directly if you meant it.")
        if not source.exists():
            raise SystemExit(f"Cannot upload {local}: no such file.")
        if source.is_dir():
            raise SystemExit(
                f"Cannot upload {local}: it is a directory, and --file takes one file at a time.")
        if not source.is_file():
            raise SystemExit(f"Cannot upload {local}: it is not a regular file.")

        try:
            content = source.read_bytes()
        except OSError as exc:
            raise SystemExit(f"Cannot read {local}: {exc}")

        if len(content) > MAX_FILE_BYTES:
            raise SystemExit(
                f"Cannot upload {local}: it is {len(content)} bytes, over the "
                f"{MAX_FILE_BYTES}-byte per-file limit.")
        total += len(content)
        if total > MAX_TOTAL_BYTES:
            raise SystemExit(
                f"The upload exceeds the {MAX_TOTAL_BYTES}-byte total limit.")

        entry = {"path": dest, "content_b64": base64.b64encode(content).decode()}
        if mark_executable and _is_executable(source):
            entry["executable"] = True
        files.append(entry)
    return files


def _is_executable(source: Path) -> bool:
    """Whether this local file carries an execute bit.

    `os.access(X_OK)` is deliberately NOT used: it answers "could *this process* execute it", which
    folds in ownership, ACLs and the mount's `noexec` — none of which say anything about the mode
    that belongs in the project's history. The question here is what git would record, which is the
    stat mode.

    ANY of the three bits, matching git's own rule: git stores one executable mode, so a file that is
    executable for its owner and nobody else is still `100755` once committed.
    """
    import stat

    try:
        return bool(source.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
    except OSError:
        # Unreadable metadata on a file whose CONTENT was just read successfully. Not worth failing
        # the command over: the mode is then whatever the project already has, which is the same
        # answer a caller who says nothing gets.
        return False


def _split_spec(spec: str) -> tuple[str, str]:
    """`LOCAL[:DEST]` → `(local, dest)`, defaulting the destination to the file's basename.

    Split on the LAST colon, not the first: a colon is a legal character in a filename on every
    platform this runs on, so `--file ./od:2026-08-04.csv:notes/od.csv` must keep the colon in the
    LOCAL name and take only the final field as the destination. Splitting on the first would look
    for a file called `./od` and report it missing.

    Only when what follows is non-empty — `--file ./a.txt:` is a typo, not a request to upload to
    "". A colon-bearing filename with NO destination is still ambiguous by construction and resolves
    to "no such file", which names the path it looked for.
    """
    if ":" in spec:
        local, _, dest = spec.rpartition(":")
        if local and dest:
            return local, dest
    return spec, Path(spec).name


# The project a task lands in when `--project` is not given. A NAME, resolved here against the
# caller's own projects and never sent to the relay as one — `POST /tasks` takes an id (ADR 0033
# D-a). LOCKSTEP in spirit with grid-src's `DEFAULT_PROJECT_NAME`, but not in effect: the relay no
# longer resolves it for anybody, so the two drifting apart would give this CLI a differently-named
# personal project, not a broken one.
DEFAULT_PROJECT_NAME = "default"

# The `role` a project listing gives the person who owns it, mirroring grid-src's
# `project_members.OWNER_ROLE`. Required rather than cosmetic: `GET /relay/v1/projects` lists every
# project the caller is a MEMBER of, so without this filter a `default` belonging to a COLLEAGUE
# would be substituted for the one the caller meant — which is precisely the ADR 0033 D-a bug this
# feature exists to remove, reintroduced on the path that has no `--project` to check against.
#
# *A role this build does not recognise simply does not match*, so the fail direction is the
# "which project?" refusal, which creates nothing and names the two commands that fix it.
# `tests/test_task_lease.py` parses grid-src for the spelling rather than restating it.
_OWNER_ROLE = "owner"

# Case A (ADR 0033 D-o, issue 26). Framed on the actionable fact — a task needs a project and none
# was named — rather than on `default`, which is an internal convention the user never chose and
# cannot act on.
_NO_PROJECT = (
    "No project given. A task runs on a project's code, so it needs to know which one.\n"
    "\n"
    "  grid project list                                  # your projects\n"
    "  grid task create --project <id> --prompt '...'\n"
    "\n"
    "No projects yet?  grid project create --name my-app")


def _resolve_project(base: str, token: str, project: str | None) -> str:
    """The project id to post a task to.

    `--project` is an **id**, used verbatim: a name is unique per owner (`idx_projects_owner_name`),
    so it was never an address a second project member could use, and resolving one here would put
    back the bug ADR 0033 D-a closes — posting a name someone else owns used to hand you a new,
    empty project of your own, silently.

    With no `--project`, the caller's own project called `default` is looked UP and used if it is
    there. It is never created: minting one as a side effect of a forgotten flag left people owning
    projects they had not asked for, named after a convention, discoverable only through the error
    line that followed (ADR 0033 D-o, issue 26).
    """
    if project is not None:
        # `is not None`, then reject blank — NOT a bare truthiness test. `--project ""` naming the
        # default project is the same substitution this whole slice removes: the caller named
        # something and something else was used. The relay would refuse an empty `project_id`, but
        # it never sees one, because an empty string never reaches the wire.
        if not project.strip():
            raise SystemExit(
                "--project needs a project id. Find yours with `grid project list`, "
                "or leave --project off to use your own 'default' project.")
        return project
    return _own_default_project(base, token)


def _own_default_project(base: str, token: str) -> str:
    """The id of the caller's OWN project called `default`, or the "which project?" refusal.

    One round trip, exactly as the create-or-get it replaces — the convention is kept so that
    anybody who already has a `default` project keeps their one-liner working, and only the minting
    goes away.
    """
    from remote import relay

    answer = relay.list_projects(base, token)
    # `isinstance` before `.get`: `_task_oneshot` returns `resp.json()` verbatim, so a 200 carrying a
    # JSON array, string or number — a proxy, a captive portal — reaches this line and an
    # `AttributeError` escapes `main()` as a traceback, breaking this plane's "any failure is a clean
    # SystemExit" contract. The hole is `_task_oneshot`'s and is older than this command; it is
    # guarded HERE because a projectless `grid task create` is the most-run path in the CLI.
    projects = answer.get("projects") if isinstance(answer, dict) else None
    if not isinstance(projects, list):
        # A reply this command cannot read is NOT an empty account — the rule `grid task list`,
        # `project status`, `promote`, `integrate` and `init` all follow. Reporting one as "you have
        # no projects" sends somebody who has used their `default` for months to create a second
        # one, leaving their work in the project the message did not mention.
        raise SystemExit(
            "The relay's answer did not contain a project list, so this cannot tell whether you "
            "have a project called 'default'. `grid project list --json` shows what it sent; "
            "`grid task create --project <id>` names one explicitly.")
    mine = [project for project in projects
            if isinstance(project, dict)
            and project.get("role") == _OWNER_ROLE
            and project.get("name") == DEFAULT_PROJECT_NAME]
    if not mine:
        raise SystemExit(_NO_PROJECT)
    project_id = mine[0].get("id")
    if not isinstance(project_id, str) or not project_id.strip():
        # Listed, but with nothing to address it by. Nothing downstream can recover — `create_task`
        # would post `project_id: None` and be refused with a message about a key the user never
        # typed — so say what actually happened.
        raise SystemExit(
            f"The relay listed your {DEFAULT_PROJECT_NAME!r} project but gave it no id. "
            "Pass --project <id> explicitly, or list your projects with `grid project list`.")
    return project_id


def _ensure_trunk(base: str, token: str, project_id: str, *, quiet: bool) -> None:
    """`--init-project`: give the project a trunk if it has none, then let the task go ahead.

    Init-then-create rather than a pre-check, for `create_task`'s reason: issue 25's route already
    decides this, and asking first would add a round trip to learn something the call itself
    reports.

    **A project that ALREADY has a trunk is not an error here.** The relay's 409 says the state the
    caller asked for is the state that holds, and refusing on it would make the very command Case B
    suggests single-use — a task create that then failed for any other reason (a busy slot, a
    rejected path) would leave the user re-running a line that now complains about a trunk instead of
    running their task. It is the same reading `project_init` itself applies to the swap it loses to
    an identical commit: *an error for a state that is already correct sends somebody to fix a thing
    that is not broken*.

    Every OTHER refusal stops the command, including a relay too old to have the route — that one
    arrives as `_OLD_RELAY`'s sentence from `init_project`, so no task is created against a relay
    that could not have initialized anything.

    `quiet` moves the line to **stderr** rather than suppressing it, and the distinction matters
    because this is the one IRREVERSIBLE thing `task create` can do: a project given an empty trunk
    can never `grid project import`, and nothing undoes it. `--json`'s contract is that stdout is a
    single parseable document — that is satisfied by writing elsewhere, not by saying nothing. It
    used to say nothing, which left the caller most likely to run this unattended with no record
    that the door had been opened at all: `create_task` can still fail AFTER the init lands (a busy
    slot, a path the relay rejects), and the JSON consumer would see only that error.
    """
    from remote import relay

    # `--json` sends it to stderr; a human sees it on stdout with the rest of the command's progress.
    where = sys.stderr if quiet else sys.stdout

    try:
        answer = relay.init_project(base, token, project_id)
    except relay.TaskRefusal as exc:
        if exc.refusal_code != _TRUNK_EXISTS:
            raise
        print(f"{project_id} already has a trunk — nothing to initialize.", file=where)
        return
    # `.get()` and a fallback: the reply is the relay's, and the task is going ahead either way.
    # `grid project init` is the command that REPORTS an initialization and it guards its reply
    # properly; here the trunk is a precondition being met, not the thing being reported — and if it
    # was not really met, `create_task` refuses `project_has_no_trunk` one round trip later.
    print(f"{answer.get('trunk') or 'main'} initialized at "
          f"{answer.get('commit') or '(no commit reported)'} in project {project_id}", file=where)


def _no_trunk_message(args: argparse.Namespace, project_id: str) -> str:
    """Case B: the two ways forward, as commands, carrying what the user already typed.

    The suggestion reproduces the prompt and every `--file` spec **verbatim**, because the
    alternative — "fix it and run the task again" — is a retype of a command that may hold a long
    prompt and several uploads. `--grid` rides along when it was given: without it the suggested
    line would target a different grid from the one that just refused, which is a worse failure than
    the one being explained.

    `shlex.quote` on every value, so a prompt carrying a space or an apostrophe pastes back as ONE
    argument. `--project` always names the resolved id even when the caller did not type one — the
    id is the thing they need and the only unambiguous way to say it.

    Deliberately NOT the relay's own sentence with a line appended: the relay's ends by naming
    import as the only fix, and since ADR 0033 D-o there are two.
    """
    import shlex

    head = (f"Project {project_id} has no main yet, so there is nothing to cut a task from.")
    if getattr(args, "init_project", False):
        # The caller ALREADY asked for a trunk and the relay still says there is none, so offering
        # `--init-project` would hand back the command that just failed — the "advice that is
        # guaranteed not to work" this whole issue exists to remove, reintroduced at the one place
        # left that could still print it.
        #
        # Reachable through the conflation named in `_task_create`: grid-src's `resolve_branch`
        # answers `None` both for "no trunk" and for "this repository could not be read", so a
        # transient relay-side read failure looks exactly like a missing trunk. That points at the
        # relay, not at the user, and re-running would change nothing.
        return (
            f"{head} The --init-project you passed reported success, so either the relay could not "
            f"read this project's repository or its trunk went missing between the two calls — "
            f"neither is something re-running fixes.\n"
            f"\n"
            f"  Check what the relay thinks it has:\n"
            f"    grid project status {project_id}")

    grid = getattr(args, "grid", None)
    argv = ["grid", "task", "create", "--project", project_id, "--init-project"]
    if grid:
        argv += ["--grid", grid]
    argv += ["--prompt", args.prompt]
    for spec in getattr(args, "file", None) or []:
        argv += ["--file", spec]
    suggestion = " ".join(shlex.quote(part) for part in argv)
    return (
        f"{head} Two ways forward:\n"
        f"\n"
        f"  Start empty (no existing code):\n"
        f"    {suggestion}\n"
        f"\n"
        f"  Or bring an existing repository in:\n"
        f"    grid project import . {project_id}")


# The relay's refusal code for "this project has no `main`" — grid-src `tasks._wip_base_ref`, a 422.
# One of the TWO parsed `detail` codes this CLI reads (the other is `_TRUNK_EXISTS` below); the
# provider still reads exactly one, `remote/task_lease.CANCELLED_CODE`.
#
# *Absent ⇒ the branch does not fire and the relay's own sentence is shown*, which is the behaviour
# every release before this one had. `tests/test_task_lease.py` parses grid-src for the spelling.
_NO_TRUNK = "project_has_no_trunk"

# The relay's refusal code for "this project already has a `main`" — grid-src
# `project_trunk.refuse_if_trunk_exists`, a 409 shared by both bootstraps.
_TRUNK_EXISTS = "project_already_has_trunk"


def _task_create(args: argparse.Namespace) -> int:
    from remote import relay

    # Read the files BEFORE resolving the grid: a typo in a filename should not first cost a
    # credential lookup and a control-plane round trip to discover.
    files = _collect_files(getattr(args, "file", None))

    base, token, label = _resolve(args)
    project_id = _resolve_project(base, token, getattr(args, "project", None))
    if getattr(args, "init_project", False):
        _ensure_trunk(base, token, project_id, quiet=bool(getattr(args, "json", False)))
    # `files` and not `files or None`: `create_task` already decides whether the key goes on the
    # wire, and a second guard here reads as though it mattered while doing nothing.
    try:
        task = relay.create_task(
            base, token, prompt=args.prompt, project_id=project_id, files=files)
    except relay.TaskRefusal as exc:
        # Deliberately NOT pre-checked with a `project_status` call. That would add a round trip to
        # every task creation to serve the first one, and it cannot even answer the question
        # reliably: `main_commit` is `None` both for "no trunk" and for "the relay cannot read this
        # repository", so a pre-flight would tell a user to initialize a project when the fault is
        # the relay's disk. Nothing is wasted by letting the post happen — grid-src refuses before
        # the task row exists and before `commit_input`, so no state is written and no slot spent.
        if exc.refusal_code != _NO_TRUNK:
            raise
        raise SystemExit(_no_trunk_message(args, project_id)) from None

    if getattr(args, "json", False):
        print(json.dumps(task, indent=2))
        return 0

    # `.get()` throughout: the reply shape is the relay's, and an older one may not send every key.
    print(f"task {task.get('id') or '(no id)'} created on {label}")
    print(f"state={task.get('state') or 'unknown'}")
    print(f"project={task.get('project_id') or 'unknown'}")
    print(f"\nWatch it with: grid task get {task.get('id') or '<id>'}")
    return 0


def _task_follow(args: argparse.Namespace) -> int:
    """Watch a task's event log live, reattaching at the cursor if the connection drops.

    The cursor is the whole point. Every event carries its `seq`, so a stream that dies is resumed
    with `after_seq=<last seq seen>` and the relay replays exactly what follows — no gap, no repeat.
    Without that, a reconnect would either re-render everything or silently skip whatever arrived
    while the socket was down.

    Reconnects are BOUNDED. A relay that is genuinely gone must end the command, not spin forever;
    and an answered refusal (403 not yours, 404 no such task, 410 expired) is never retried at all,
    because reconnecting cannot change a verdict.

    Exit code is the task's own outcome: 0 for `completed`, 1 for `failed`/`timed_out`, so a script
    can branch on it. A stream that ends without a terminal event exits non-zero too — the task's
    fate is unknown, and reporting unknown as success is the failure this guards.
    """
    import time

    from remote import relay

    base, token, _label = _resolve(args)
    as_json = bool(getattr(args, "json", False))
    cursor = int(getattr(args, "after_seq", -1) or -1)
    terminal: dict | None = None
    attempts_left = _RECONNECT_ATTEMPTS
    # Outlives the reconnect loop deliberately: a reattach resumes at the cursor, so the tree the
    # follower already showed is still the tree the user is looking at.
    tree = _TreeView()

    while terminal is None:
        made_progress = False
        try:
            for seq, event in relay.stream_task_events(
                    base, token, args.task_id, after_seq=cursor):
                cursor = seq
                made_progress = True
                _render(seq, event, as_json=as_json, tree=tree)
                if event.get("type") == "task.terminal":
                    terminal = event
                    break
        except SystemExit:
            raise
        except relay.RelayError as exc:
            if _is_answer_not_blip(getattr(exc, "status", None)):
                print(f"Cannot follow task {args.task_id}: {exc}", file=sys.stderr)
                return 1
            if made_progress:
                attempts_left = _RECONNECT_ATTEMPTS
            elif attempts_left <= 0:
                print(f"Lost the connection following task {args.task_id} and could not "
                      f"reattach: {exc}", file=sys.stderr)
                return 1
            else:
                attempts_left -= 1
            print(f"connection lost ({exc}); reattaching at seq {cursor}", file=sys.stderr)
            time.sleep(_RECONNECT_BACKOFF_SECONDS)
            continue

        if terminal is not None:
            break

        # The stream ended CLEANLY without a terminal event, and the client cannot tell why: the
        # relay may have finished (its row GC'd), or something between us — a proxy's SSE read
        # timeout — may have closed a perfectly live stream. Those are byte-identical at this end,
        # so reattach rather than guess. Progress refills the budget, so a proxy that severs a busy
        # stream every few minutes is followed indefinitely; a task that is genuinely over yields
        # nothing new and costs a few empty reattaches before this gives up.
        if made_progress:
            attempts_left = _RECONNECT_ATTEMPTS
        elif attempts_left <= 0:
            print(f"The stream for task {args.task_id} ended without a terminal event.",
                  file=sys.stderr)
            return 1
        else:
            attempts_left -= 1
        time.sleep(_RECONNECT_BACKOFF_SECONDS)

    return 0 if terminal.get("state") == "completed" else 1


def _is_answer_not_blip(status: int | None) -> bool:
    """Whether the relay ANSWERED, so reattaching cannot change the outcome.

    The same rule the provider's report path uses, applied to the read side: a 4xx is a verdict
    (403 not yours, 404 no such task, 410 expired); a 5xx or a bare transport failure means nobody
    decided anything yet and the connection is worth remaking.
    """
    return status is not None and 400 <= status < 500


# How many path lines one tree snapshot may print. The provider's own cap protects the WIRE; this
# one protects the terminal, where the tree shares a screen with the tool-call lines a user is
# actually reading. A capped snapshot is still five hundred paths.
_TREE_LINES = 40
# What every path line is indented by, so a tree is visibly subordinate to the `[seq]` line above it.
_TREE_INDENT = "      "


class _TreeView:
    """The last workspace snapshot this follower saw, so later ones can be shown as changes.

    The provider re-sends the WHOLE tree every time — it has to, because a client can attach at any
    `after_seq` and a requeue can move the task to a provider that never saw the earlier events
    (ADR 0032 D-d). Snapshots are the right thing on the wire and the wrong thing on a screen:
    reprinting two hundred unchanged paths every thirty seconds buries everything else. So the
    difference is computed here, at the only end that has somewhere to keep it.

    **A delta is only computed between two COMPLETE snapshots**, and "complete" is judged rather than
    assumed. A truncated snapshot shows a prefix; so does one whose `paths` was not a list, or held
    an entry that was not a string, or whose `total` disagrees with what arrived. In every one of
    those cases a path missing from this snapshot may simply be a path we were not given — and
    reporting it as `- src/main.py` would be a confident, wrong statement that the agent deleted the
    user's file. Listing the snapshot again is noisier and true, which is the correct way round.
    """

    def __init__(self) -> None:
        # The last COMPLETE snapshot, or `None` for "no baseline worth diffing against" — which is
        # both the initial state and what a partial snapshot leaves behind.
        self._paths: frozenset[str] | None = None

    def lines(self, seq: int, event: dict) -> list[str]:
        """The lines one `task.tree` event prints. Never raises — the event came off the wire."""
        paths, well_formed = _tree_paths(event)
        total = event.get("total")
        # `bool` is an `int` in Python, and `True` is not a file count.
        counted = isinstance(total, int) and not isinstance(total, bool) and total >= len(paths)
        truncated = bool(event.get("truncated"))
        # Complete means: everything the tree holds is in this event, and we can prove it. A `total`
        # that disagrees with the paths delivered is as partial as a flag saying so.
        complete = well_formed and counted and not truncated and total == len(paths)

        if counted:
            head = f"[{seq}] workspace: {total} files"
            if truncated:
                head += f" (truncated, showing {len(paths)} of {total})"
        else:
            # No usable count arrived, so none is claimed: "showing 2 of 2" under a `truncated` flag
            # is a line that contradicts itself, and it is built entirely out of a recovered number.
            head = f"[{seq}] workspace: {len(paths)} files listed"
            if truncated:
                head += " (truncated; the total was not reported)"

        previous = self._paths
        self._paths = frozenset(paths) if complete else None
        if previous is None or not complete:
            # Nothing honest to compare against: either the user attached mid-run and has no idea
            # what is in there, or one of the two snapshots is part of a tree we never saw whole.
            return [head, *_bounded([f"{_TREE_INDENT}{path}" for path in paths])]

        added = sorted(frozenset(paths) - previous)
        removed = sorted(previous - frozenset(paths))
        if not added and not removed:
            return [head]
        counts = ([f"+{len(added)}"] if added else []) + ([f"-{len(removed)}"] if removed else [])
        head += f" ({', '.join(counts)})"
        return [head, *_bounded(
            [f"{_TREE_INDENT}+ {path}" for path in added]
            + [f"{_TREE_INDENT}- {path}" for path in removed])]


def _tree_paths(event: dict) -> tuple[list[str], bool]:
    """The event's `paths`, and whether they arrived intact.

    Defended at the boundary — a relay that is newer, older, or simply wrong must not be able to turn
    `grid task follow` into a traceback. The second half of the answer is what stops the defence from
    becoming its own bug: silently dropping an entry we could not read leaves a SHORTER list that
    looks complete, and `_TreeView` would then report the missing entry as a deleted file.
    """
    raw = event.get("paths")
    if not isinstance(raw, list):
        return [], False
    usable = [path for path in raw if isinstance(path, str)]
    return usable, len(usable) == len(raw)


def _bounded(lines: list[str]) -> list[str]:
    """At most `_TREE_LINES` of them, and a line saying so when there were more.

    Saying so is the part that matters: a listing silently cut at forty reads as a workspace that
    holds forty files, which is exactly the wrong thing to believe about a dependency install.
    """
    if len(lines) <= _TREE_LINES:
        return lines
    return [*lines[:_TREE_LINES], f"{_TREE_INDENT}… and {len(lines) - _TREE_LINES} more"]


def _resets_note(resets_at: Any) -> str:
    """" — resets <local time>", or nothing at all when the stamp is not one.

    A raw epoch is not a time anyone can read, and this line exists to tell a user when they can
    submit their next task. Every field on the event is optional — it is built from a subprocess's
    stdout — so an unusable stamp is simply left out rather than shown as `None` or raised on.
    """
    if isinstance(resets_at, bool) or not isinstance(resets_at, (int, float)):
        return ""
    try:
        return f" — resets {datetime.datetime.fromtimestamp(resets_at):%b %-d at %H:%M}"
    except (OverflowError, OSError, ValueError):
        # A stamp far enough out of range that the platform cannot render it. The rest of the line
        # is still worth printing; a follower may never die on a diagnostic.
        return ""


def _result_note(event: dict) -> str:
    """": success (12 turns, 34.5s)" — with each part dropped unless it is usable.

    Same contract as `_resets_note`: the fields come off a subprocess's stdout, nothing bounds them,
    and a follower may never die on a diagnostic. `num_turns` excludes `bool` because `True` is an
    `int` in Python and "1 turns" from a boolean is a confident lie about the run.
    """
    parts = []
    turns = event.get("num_turns")
    if isinstance(turns, int) and not isinstance(turns, bool):
        parts.append(f"{turns} turns")
    duration = event.get("duration_ms")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
        try:
            parts.append(f"{duration / 1000:.1f}s")
        except (OverflowError, ValueError):
            # A JSON number is an unbounded int; dividing one long enough overflows the float.
            pass
    subtype = event.get("subtype")
    head = f": {subtype}" if isinstance(subtype, str) and subtype else ""
    if event.get("is_error"):
        head += " (the agent reports it went wrong)"
    return head + (f" ({', '.join(parts)})" if parts else "")


def _render(seq: int, event: dict, *, as_json: bool, tree: "_TreeView | None" = None) -> None:
    """One event, printed. Unknown types are shown rather than dropped.

    Event types grow, and a follower that rendered only the types it knew would show a user nothing
    while the relay faithfully streamed them what they asked for.
    """
    if as_json:
        print(json.dumps({"seq": seq, "event": event}))
        return

    kind = event.get("type") or "event"
    if kind == "task.tree":
        # The only live view of a running task's working directory: the provider commits at terminal
        # boundaries only (ADR 0032 D-e), so between claim and terminal the repository holds nothing
        # new and there is nothing else to look at.
        for line in (tree or _TreeView()).lines(seq, event):
            print(line)
    elif kind == "task.output":
        print(event.get("text", ""))
    elif kind == "task.tool_use":
        # The line a user reads most of — a task is minutes of tool calls and a sentence of prose.
        # The path is optional: `Bash` and `WebSearch` target nothing, and a stream that went silent
        # during a ten-minute test run would read as a hang.
        path = event.get("path")
        print(f"[{seq}] {event.get('tool') or 'tool'}" + (f" {path}" if path else ""))
    elif kind == "task.tool_result":
        # One of these arrives for EVERY tool call, and a real task makes hundreds. The call's
        # identity was already printed by its `task.tool_use` line, so the only news here is a
        # failure — narrating the successes would double the length of a follow with a line whose
        # sole content is an id the user cannot act on. Silence is the right render for "it worked".
        if event.get("is_error"):
            print(f"[{seq}] tool call failed ({event.get('id') or 'unknown call'})", file=sys.stderr)
    elif kind == "task.stderr":
        # The agent's own diagnostics, on OUR stderr — so `grid task follow > out.txt` keeps the
        # task's output separable from the noise around it.
        print(f"[{seq}] {event.get('text', '')}", file=sys.stderr)
    elif kind == "task.session":
        print(f"[{seq}] session {event.get('session_id')}")
    elif kind == "task.session_resumed":
        print(f"[{seq}] resuming session {event.get('session_id')}")
    elif kind == "task.session_reset":
        # On stderr with the other diagnostics: the agent is about to answer without any of the
        # project's history, and a user who does not know that reads the result as the agent
        # ignoring everything they established in earlier tasks.
        reason = event.get("reason") or "the transcript could not be used"
        print(f"[{seq}] starting a fresh session ({reason})", file=sys.stderr)
    elif kind == "task.rate_limit":
        # The provider's own Claude subscription, reporting itself (ADR 0032 issue 09). On stderr
        # with the other diagnostics: it says nothing about this task's result, and it is the only
        # copy of the reason that reaches the person who submitted the task — the provider's own
        # warning goes to the provider's log, which they cannot read. So when their NEXT task sits
        # queued, this is what explains it.
        #
        # "provider capacity", not "rate limit", and `status=` rather than a verb. The provider now
        # drops the boring reading (`task_capacity.QUIET_STATUS`), so anything arriving here is worth
        # a line — but the OLD phrasing made even a healthy one read as a fault: "the provider's
        # five_hour window is allowed" opens with the words `rate limit`, puts `allowed` where a
        # reader expects a verdict, and prints on stderr beside real diagnostics. The status stays
        # VERBATIM (the `task.retry` rule): the decision about what a status means belongs to
        # `task_capacity`, on the provider, and a second reading here is a second thing to disagree.
        print(f"[{seq}] provider capacity: "
              f"{event.get('limit_type') or 'subscription'} window, "
              f"status={event.get('status') or 'unknown'}"
              + _resets_note(event.get("resets_at")), file=sys.stderr)
    elif kind == "task.attempt_started":
        provider = event.get("provider_id")
        where = f" on {provider}" if provider else ""
        print(f"[{seq}] attempt {event.get('attempt')} started{where}")
    elif kind == "task.retry":
        # On stderr with the other disclosures, and worded so the asymmetry is unmissable: the relay
        # resets the TASK's branch to its input commit before the next attempt, and a user who is
        # not told that reads a repeated run as the agent looping (ADR 0032 D-d).
        #
        # It used to say "Changes the lost attempt made in git are undone", and ADR 0033 D-c made
        # that FALSE. The reset covers `task/<id>` and nothing else — the reaper never touches a WIP
        # branch, promote writes only `main`, and there is no revert. So an interrupted settle (the
        # git write landed, the terminal transaction did not) leaves the lost attempt's work on
        # `wip/<member_key>`, which is exactly where this member's NEXT task is cut from.
        #
        # Naming the recovery is the other half. "Something may be left behind" with no way to act
        # on it is a worse message than the wrong one it replaces.
        attempt = event.get("attempt")
        of = event.get("max_attempts")
        reason = event.get("reason") or "its lease lapsed"
        print(f"[{seq}] attempt {attempt}"
              + (f" of {of}" if of else "")
              + f" was lost ({reason}); retrying from the task's input. The task's own branch is "
                f"reset; anything it did outside git is not, and work an interrupted settle "
                f"already merged into your wip/ branch stays there "
                f"(`grid project wip reset` moves it back).", file=sys.stderr)
    elif kind == "task.cancelled":
        # Somebody stopped this run (ADR 0033 D-l, issue 19b). On stderr with the other
        # disclosures: it says nothing about the result and everything about why there is not one,
        # and on a shared project the person reading this is often not the person who did it.
        #
        # `by` is a member key, which is what `grid project member list` and `grid project promote`
        # both speak. Absent is ordinary — an older relay, or a canceller who has since left the
        # project — and is worth saying rather than printing `None`.
        by = event.get("by")
        print(f"[{seq}] cancelled by " + (f"project member {by}" if by else "a project member")
              + ". The task's branch is left where the agent got to.", file=sys.stderr)
    elif kind == "task.result":
        # The agent's own account of the run, one event at the end. `is_error` here is the AGENT's
        # claim about itself; the task's real outcome is `task.terminal`'s, which prints after it.
        # Every field is optional and none is bounded (it is built from a subprocess's JSON), so
        # each part is dropped unless it is usable rather than printed as `None`.
        print(f"[{seq}] agent finished{_result_note(event)}")
    elif kind == "task.terminal":
        error = event.get("error")
        print(f"[{seq}] {event.get('state')}" + (f": {error}" if error else ""))
        if error == QUEUE_EXPIRED:
            # The relay's word stays on the line above, verbatim like every other reason. This says
            # what it means, because `timed_out` reads as "the task hung" and this one did not run
            # at all (ADR 0033 D-k).
            #
            # On stdout with the line it explains, unlike the `task.retry` and `task.rate_limit`
            # diagnostics: this is part of the terminal report, and a reader piping stdout to a log
            # must not end up with the outcome and not the reason for it.
            print(f"[{seq}] {_QUEUE_EXPIRED_NOTE}")
    else:
        # Verbatim, minus the type we already printed — enough to be useful without pretending to
        # understand a shape this build has never seen.
        rest = {k: v for k, v in event.items() if k != "type"}
        print(f"[{seq}] {kind} {json.dumps(rest, sort_keys=True)}" if rest else f"[{seq}] {kind}")


def _task_fetch(args: argparse.Namespace) -> int:
    """Put a finished task's result on disk, over the relay's git front (ADR 0032 issue 05).

    No SSH key is provisioned for anyone: the same grid token this CLI already holds authenticates
    the fetch, handed to git through the environment so it never reaches argv and is never written
    into the clone's `.git/config`. A result directory is a thing people zip up and pass around, and
    a grid access token lives a year.
    """
    from remote import project_clone, relay, task_repo

    base, token, _label = _resolve(args)
    task = relay.get_task(base, token, args.task_id)

    state = task.get("state")
    if state not in _TERMINAL_STATES:
        # Refused rather than served: until the provider pushes, the branch still holds only the
        # INPUT, so a fetch would hand back the uploaded files as though they were the result.
        raise SystemExit(
            f"Task {args.task_id} is {state or 'unknown'}, so it has no result yet. "
            f"Watch it with `grid task follow {args.task_id}`.")
    commit, branch, project_id = (
        task.get("result_commit"), task.get("branch"), task.get("project_id"))
    if not (branch and project_id):
        # Nothing ADDRESSABLE — no branch to fetch from, so there is genuinely nowhere to look.
        raise SystemExit(
            f"Task {args.task_id} finished as {state} but recorded no result to fetch. "
            f"`grid task get {args.task_id}` shows what it did report.")
    # A missing `result_commit` used to be refused here too, and that made `grid task cancel` a
    # liar: it prints "Its branch is left where the agent got to: grid task fetch <id>", and this
    # command then answered "recorded no result to fetch" — while the branch was on the relay all
    # along, holding at least the task's input.
    #
    # The promise cannot be made conditional at its own end: cancel returns immediately and the
    # agent does not die until the next lease beat, so at the moment the sentence is printed nobody
    # knows whether a result will land. So the fix belongs here — serve what exists, and SAY which
    # of the two it is. Refusing was one way to keep "input" from reading as "result"; saying so is
    # the other, and it is the one that keeps the promise.
    if not commit:
        print(f"Task {args.task_id} recorded no result, so this is the branch as the grid last "
              f"saw it — it may hold only the task's input.")
    if task.get("branch_pruned"):
        # Refused HERE rather than letting git answer, because everything else about an old task
        # still looks fetchable — the commit and the branch name are both on the row. git's own
        # `couldn't find remote ref` reads as corruption or as a permissions problem, and sends the
        # user hunting for a break that is a retention policy working exactly as intended.
        #
        # `.get` with a falsy default is the degrade: a relay predating ADR 0033 issue 16a sends no
        # such key AND collects no branches, so absent must mean "still there". Reading absence the
        # other way would refuse every fetch against every relay that has not redeployed.
        raise SystemExit(
            f"Task {args.task_id}'s branch is no longer kept on the relay — its files were "
            f"collected after the project's retention window. "
            f"`grid task get {args.task_id}` still shows what it did and why.")

    dest = Path(args.into) if getattr(args, "into", None) else Path(args.task_id)
    if dest.exists() and not dest.is_dir():
        raise SystemExit(f"Cannot fetch into {dest}: it exists and is not a directory.")
    if dest.is_dir() and any(dest.iterdir()):
        # A directory of somebody's own work is not a place to check a branch out into: `git
        # checkout -- .` overwrites a file of the same name without complaining. Only a directory
        # THIS command created is safe, and only for the project it was created for — updating one
        # in place is how a project's successive tasks are followed.
        #
        # The presence of `.git` is NOT that proof, and using it as such was a real defect: the
        # user's own repository has one too, so `--into ~/my-project` (or running the command inside
        # it) sailed past the guard and destroyed uncommitted work.
        # A `grid project clone` is checked FIRST and refused with its own sentence (ADR 0033 D-h,
        # issue 17). The guard is not loosened for it — a clone is full of work in progress, and
        # `git checkout -- .` overwrites a same-named file without complaining — but "pass --into
        # with a new directory" is the wrong fix there: the clone already has the task branch, and
        # two git commands reach it without this command being involved at all.
        cloned = project_clone.cloned_project(dest)
        if cloned is not None:
            raise SystemExit(
                f"Cannot fetch into {dest}: it is a `grid project clone`, which already has "
                f"task {args.task_id}'s branch. Get the result there with "
                f"`git fetch origin task/{args.task_id}` then `git checkout task/{args.task_id}`.")
        held = task_repo.fetched_project(dest)
        if held is None:
            raise SystemExit(
                f"Cannot fetch into {dest}: it already has files in it and was not created by "
                f"`grid task fetch`. Pass --into with a new directory.")
        if held != project_id:
            raise SystemExit(
                f"Cannot fetch into {dest}: it holds project {held}, and task {args.task_id} "
                f"belongs to project {project_id}. Pass --into with a different directory.")
    dest.mkdir(parents=True, exist_ok=True)

    try:
        task_repo.checkout_result(
            dest, url=relay.git_remote_url(base, project_id), token=token,
            branch=branch, commit=commit, project_id=project_id)
    except Exception as exc:
        raise SystemExit(f"Could not fetch task {args.task_id}: {exc}")

    if getattr(args, "json", False):
        print(json.dumps(
            {"task_id": args.task_id, "state": state, "result_commit": commit,
             "branch": branch, "path": str(dest)}, indent=2))
        return 0

    print(f"task {args.task_id} ({state}) at {commit or branch}")
    print(f"fetched into {dest}")
    # Named so the user can see WHAT arrived rather than being told that something did — on a
    # `failed` task especially, the file list is the point of fetching at all.
    for path in sorted(p for p in dest.rglob("*") if p.is_file() and ".git" not in p.parts):
        print(f"  {path.relative_to(dest)}")
    return 0


# How much of a prompt a listed row shows. The prompt is up to 100 KB, and a table is unreadable
# the moment one row wraps — `grid task get <id>` is where the whole thing lives.
_PROMPT_COLUMN = 48


def _task_list(args: argparse.Namespace) -> int:
    """The tasks in a project (ADR 0033 D-l, issue 19a).

    Nothing listed tasks before this. `grid task get` answers one id at a time and the id came from
    `grid task create`, so somebody who closed their terminal had to clone the project and read
    `task/*` refs by hand.

    `--all` widens it from the caller's own runs to every member's, which is the point on a shared
    project: a team wants to see what the team ran, and the relay fences the answer on membership.
    """
    from remote import relay

    base, token, _label = _resolve(args)
    answer = relay.list_tasks(base, token, args.project, mine=not args.all,
                              states=list(args.state or ()), limit=args.limit,
                              after=args.after)
    if getattr(args, "json", False):
        print(json.dumps(answer, indent=2))
        return 0

    tasks = answer.get("tasks")
    if not isinstance(tasks, list):
        # A reply this command cannot read is NOT an empty project — the same rule
        # `grid project status`, `check`, `promote`, `integrate`, `commit` and `import` all follow.
        # The two collapsed here until issue 19a's review: a body a proxy had stripped came back as
        # `{}`, and somebody polling a shared project was told nobody was working. The check is on
        # the PRESENCE of `tasks`, not its truthiness — an empty list is a real answer.
        raise SystemExit(
            f"The relay's answer for project {args.project} did not contain a task list, so this "
            f"cannot be reported as one. "
            f"`grid task list --project {args.project} --json` shows what it sent.")

    if not tasks:
        # Said rather than printed as an empty table, which reads as a display bug.
        print(f"No tasks in project {args.project}.")
        print(f"\nCreate one with: grid task create --project {args.project} --prompt '<what to do>'")
        return 0

    print(f"{'TASK':38}  {'STATE':10}  {'MEMBER':34}  PROMPT")
    for task in tasks:
        prompt = " ".join(str(task.get("prompt") or "").split())
        if len(prompt) > _PROMPT_COLUMN:
            prompt = prompt[:_PROMPT_COLUMN - 1] + "…"
        print(f"{str(task.get('id') or '?'):38}  "
              f"{str(task.get('state') or '?'):10}  "
              # `member_key` and not `owner_id`: the key is what `grid project promote` and
              # `grid project member list` both speak, and `grid:<network>:<sub>` is unreadable in
              # a column. Absent when the author has since left the project.
              f"{str(task.get('member_key') or '(not a member)'):34}  {prompt}")
    if answer.get("next_after"):
        # There is more. Said out loud, because a page that silently stops at the limit reads as the
        # whole history — and this is the flag that continues it.
        print(f"\nMore: grid task list --project {args.project} --after {answer['next_after']}")
    return 0


def _task_cancel(args: argparse.Namespace) -> int:
    """Stop a queued or running task and give its member their slot back (ADR 0033 D-l, issue 19b).

    **No confirmation, on purpose.** Every other consequential verb here — promote, integrate,
    commit — does the thing it is asked to do, and an application driving this CLI would have to
    pass a `--yes` on every call. Knowing whose task it is would also cost a `GET` first. The
    accountability is the relay's event log, which records the member who cancelled.

    A cancelled task's branch is left where the agent got to, so `grid task fetch` still works on it.
    """
    from remote import relay

    base, token, _label = _resolve(args)
    answer = relay.cancel_task(base, token, args.task_id)

    if getattr(args, "json", False):
        print(json.dumps(answer, indent=2))
        return 0

    state = answer.get("state")
    if not state:
        # A reply this command cannot read is NOT a cancellation — the rule every sibling in this
        # plane follows. Reporting one would tell somebody their colleague's hour-long merge task
        # had been stopped when nothing had happened, and the slot is what they would act on next.
        raise SystemExit(
            f"The relay's answer for task {args.task_id} did not say what state the task is now in, "
            f"so this cannot be reported as a cancellation. "
            f"`grid task cancel {args.task_id} --json` shows what it sent.")

    # The state AND the reason. `failed` alone reads as "the agent broke", which is the one thing
    # that did not happen — and `error` is what tells a later `grid task get` the two apart.
    reason = answer.get("error")
    print(f"task {args.task_id} cancelled — it is now {state}"
          + (f" ({reason})" if reason else ""))
    # The branch is deliberately not rewound, so whatever the agent had done is still fetchable.
    # Said out loud, because "cancelled" reads as "undone" and here it is not.
    print(f"Its branch is left where the agent got to: grid task fetch {args.task_id}")
    return 0


def _task_get(args: argparse.Namespace) -> int:
    from remote import relay

    base, token, _label = _resolve(args)
    task = relay.get_task(base, token, args.task_id)

    if getattr(args, "json", False):
        print(json.dumps(task, indent=2))
        return 0

    print(f"task {task.get('id') or args.task_id}")
    print(f"state={task.get('state') or 'unknown'}")
    if task.get("provider_id"):
        print(f"provider={task['provider_id']}")
    if task.get("claude_session_id"):
        print(f"session={task['claude_session_id']}")
    if task.get("error"):
        print(f"error={task['error']}")
        if task["error"] == QUEUE_EXPIRED:
            print(_QUEUE_EXPIRED_NOTE)
    result = task.get("result_text")
    if result:
        print("\n--- result ---")
        print(result.rstrip("\n"))
    return 0
