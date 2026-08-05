"""What child a task spawns, where it runs, and what environment it is handed (ADR 0032, issue 03).

Separate from `remote/tasks.py` — that module answers "claim, run, report", this one answers "run
*what*, *where*". They change for different reasons: the loop's shape is settled, while the agent's
argv, its workspace and its credential posture are the parts this feature keeps moving.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import task_repo

# LOCKSTEP (PRD `.scratch/distributed-tasks/PRD.md`): **every provider must use the identical
# absolute path**, because Claude Code derives a session's transcript directory from the working
# directory (`~/.claude/projects/<abs-cwd with / → ->/`). A provider using a different prefix cannot
# `--resume` a session another one started, which is the whole of issue 06.
DEFAULT_WORKSPACE_ROOT = "/var/grid"
# Overridable only so tests and dev boxes need not write under `/var`. An operator who changes this
# on one provider and not the others breaks cross-provider resume — the flag is not a preference.
WORKSPACE_ROOT_ENV = "GRID_TASK_ROOT"


# An ALLOWLIST, not a blacklist of separators. The relay generates project ids as `uuid.uuid4()`
# (grid-src `tasks.py:563`) and names its own bare repo `/var/grid/projects/<project_id>.git`, so
# nothing legitimate falls outside this. Blacklisting would have to anticipate `/`, `\`, a Windows
# drive prefix, NUL and newline separately; an allowlist refuses all of them by construction.
_SAFE_PROJECT_ID = re.compile(r"\A[A-Za-z0-9._-]+\Z")
# Long enough for any id the relay mints, short enough that no `mkdir` fails on NAME_MAX instead.
_MAX_PROJECT_ID_CHARS = 200

# The same allowlist discipline for the session id the relay hands back on a claim: it is used to
# build the transcript's filename, so a separator or an absolute path would read (and then commit)
# something outside the workspace. Claude Code's own ids are uuid4, so nothing legitimate falls
# outside this. Bounded to match the relay's own `_MAX_SESSION_ID_CHARS`.
#
# The FIRST character must be alphanumeric, which is doing two jobs beyond tidiness: it refuses `.`
# and `..` the way `workspace_for` does rather than leaving them to be defused by the `.jsonl`
# suffix, and it refuses an id like `-rf` that would reach `--resume` as something the binary reads
# as a flag instead of a value.
_SAFE_SESSION_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")

# `0o777` minus the shared-write bits, the same value `shared/paths.py` uses under `GRID_HOME`.
_DIR_MODE = 0o755


# Print mode cannot answer a permission prompt — it denies it — so the default is the mode that lets
# a task do work. ADR 0032 scopes untrusted providers out ("the current design assumes an internally
# operated fleet"); an operator who wants a narrower posture sets `GRID_TASK_PERMISSION_MODE`.
DEFAULT_PERMISSION_MODE = "bypassPermissions"
PERMISSION_MODE_ENV = "GRID_TASK_PERMISSION_MODE"
# The binary's own accepted set (`claude --permission-mode`, 2.1.221). Validated here so a typo is
# explained once, rather than becoming "the agent failed" on every task for the life of the process.
_PERMISSION_MODES = ("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan")

# The agent's own variable, and the knob an operator sets to point every task at one fixed config
# directory. Two names because they are two different things: ours is grid configuration, theirs is
# the child's environment, and conflating them would export the grid's namespace into the agent.
_CLAUDE_CONFIG_DIR = "CLAUDE_CONFIG_DIR"
CLAUDE_CONFIG_DIR_ENV = "GRID_TASK_CLAUDE_CONFIG_DIR"


def workspace_root() -> Path:
    return Path(os.getenv(WORKSPACE_ROOT_ENV) or DEFAULT_WORKSPACE_ROOT)


def workspace_for(project_id: str) -> Path:
    """The working directory a task for `project_id` runs in.

    The id arrives off the wire, so it is attacker-controlled and is validated HERE — where the path
    is built — rather than at each caller. `Path(root) / "../../etc"` is not a theoretical escape: it
    resolves, and the provider would then create that directory and run an agent with write access
    inside it. `Path(root) / "/etc"` is worse still: pathlib *discards* the left operand when the
    right one is absolute, so the root is silently gone (ADR 0032 D-b applies the same rule to the
    filenames a client uploads).
    """
    if not isinstance(project_id, str):
        raise ValueError(f"project id must be a string, got {type(project_id).__name__}")
    if not project_id or len(project_id) > _MAX_PROJECT_ID_CHARS:
        raise ValueError(
            f"project id must be 1-{_MAX_PROJECT_ID_CHARS} characters, got {len(project_id)}")
    if project_id in (".", "..") or not _SAFE_PROJECT_ID.match(project_id):
        raise ValueError(f"project id {project_id!r} is not a single safe path segment")
    return workspace_root() / "projects" / project_id / "workspace"


def ensure_workspace(path: Path) -> Path:
    """Create the workspace and any missing level above it, never shared-writable (ADR 0027).

    `shared.paths.ensure_dir` cannot be reused: it refuses anything outside `GRID_HOME`, and this
    tree deliberately lives outside it. So the rule is restated rather than inherited — with one
    deliberate difference. `ensure_dir` also *repairs* a directory it finds too open; this does not.
    The chain here starts at a path an operator chose (`/var/grid`, or whatever `GRID_TASK_ROOT`
    names), and a repair walk would happily `chmod` `/tmp` — a system directory that is 1777 on
    purpose — the first time someone points the root at one. Only the levels this function creates
    get their mode from us; anything that already existed is the operator's business.

    The mode goes to the syscall rather than a following `chmod`, so no window exists in which the
    directory is group-writable, and the umask keeps its say (`umask 077` lands `0o700`).
    """
    if path.is_symlink():
        # The one level a second account on a shared box could plant. A workspace symlinked at the
        # provider's Claude config directory would route the agent's writes — and then the
        # transcript, and then the credential in it — into the repo this task pushes (ADR 0032 D-b).
        # A higher level is left alone: relocating storage by symlinking `/var/grid` is legitimate.
        raise OSError(f"the task workspace {path} is a symlink; refusing to run an agent in it")

    missing: list[Path] = []
    probe = path
    while not probe.exists():
        missing.append(probe)
        if probe.parent == probe:  # reached the filesystem root without finding anything
            break
        probe = probe.parent

    for directory in reversed(missing):
        try:
            os.mkdir(directory, _DIR_MODE)
        except FileExistsError:
            continue  # another provider's task got there first; the path is what matters, not who won
        except OSError as exc:
            raise OSError(f"could not create the task workspace {path}: {exc}") from exc

    if not path.is_dir():
        raise OSError(f"the task workspace {path} exists but is not a directory")
    return path


# Every character Claude Code replaces when it turns a working directory into a transcript-directory
# name. MEASURED, not read out of documentation: a session run in `/private/tmp/grid_enc/a_b.c-d`
# landed in `~/.claude/projects/-private-tmp-grid-enc-a-b-c-d` (2.1.222, macOS, 2026-08-05). The
# underscore is the discriminating case — "replace the separators `/` and `.`" would have kept it,
# and every provider whose `GRID_TASK_ROOT` contains one would then symlink a directory the agent
# never writes to. See `_MEASURED_TRANSCRIPT_DIR_NAMES` in `tests/test_task_agent.py`.
_TRANSCRIPT_NAME_REPLACED = re.compile(r"[^A-Za-z0-9]")


def transcript_dir_name(cwd: Path) -> str:
    """The folder Claude Code keeps a session's transcript in, under `<config dir>/projects/`.

    This has to reproduce the agent's own naming exactly. A near-miss does not fail loudly — it
    produces a symlink nothing writes through, so the transcript never reaches the repository and
    every follow-up task starts a fresh conversation while every other signal looks healthy. That is
    why the rule is a table of measurements rather than something derived from the path grammar.

    **The path is resolved first**, because the name comes from the working directory the CHILD
    reports, and a process's `getcwd` has already followed every symlink on the way in. This is not
    theoretical tidiness: on macOS `/var` is a symlink to `/private/var`, so a workspace under
    `/var/folders/...` is `-private-var-folders-...` to the agent and `-var-folders-...` to a
    caller that trusted the string. A live two-task run is what found it — every unit test compared
    our own computation against itself and agreed.

    It also sharpens the lockstep rule: providers must agree on the **resolved** absolute workspace
    path. One provider reaching `/var/grid` through a symlink and another not is already two
    different conversations as far as Claude Code is concerned.
    """
    return _TRANSCRIPT_NAME_REPLACED.sub("-", str(cwd.resolve(strict=False)))


def configured_claude_config_dir() -> Path | None:
    """The operator's fixed config directory, validated, or `None` when they set none.

    The single read point for the variable, so `child_env` and `link_transcript` cannot disagree
    about what it means. **Absolute or nothing**: `child_env` hands the value to the child verbatim
    and the child resolves a relative one against ITS working directory — the workspace — which puts
    the provider's credential inside the git worktree the result is pushed from.
    """
    configured = (os.getenv(CLAUDE_CONFIG_DIR_ENV) or "").strip()
    if not configured:
        return None
    path = Path(configured)
    if not path.is_absolute():
        raise ValueError(
            f"{CLAUDE_CONFIG_DIR_ENV}={configured!r} must be an absolute path; the agent resolves a "
            "relative one against its workspace, which would put the provider's credential in the "
            "repository the result is pushed to")
    return path


def claude_config_dir() -> Path:
    """The directory the agent child keeps its own state in — the provider's, never the user's.

    Mirrors what `child_env` hands the child: the operator's fixed directory when there is one, and
    otherwise Claude Code's own default, which this process must agree on because it is where the
    transcript symlink has to be planted.
    """
    return configured_claude_config_dir() or (Path.home() / ".claude")


def _resolve_for_containment(path: Path) -> Path:
    """`path` with symlinks and `..` resolved, for an "is A inside B" comparison.

    `strict=False` because neither side is required to exist yet — the config directory is created
    by the agent on first use, and the containment question has an answer either way.
    """
    return path.resolve(strict=False)


def transcript_dir(workspace: Path) -> Path:
    """Where this project's transcript and `memory/` live inside the git worktree."""
    return workspace / task_repo.RESERVED_DIR / task_repo.TRANSCRIPT_DIR


def link_transcript(workspace: Path) -> Path:
    """Point Claude Code's per-cwd transcript folder at the workspace, and return the target.

    The agent writes THROUGH this symlink (measured by the issue-01 spike), so the transcript and
    the agent's `memory/` land inside the worktree and travel to the next task — and the next
    provider — in the ordinary result commit, with no second synchronization path.
    """
    config_dir = claude_config_dir()
    # Checked BEFORE anything is created. The config directory holds the provider's Claude
    # subscription credential; inside the workspace it is inside the git worktree, and the result
    # push would commit it into the requesting user's repository (ADR 0032 D-b names this hazard for
    # a client's upload — this is the same leak reached from the provider's own configuration).
    # Resolved on both sides so a symlinked `/var/grid` cannot hide the containment.
    resolved_config = _resolve_for_containment(config_dir)
    resolved_workspace = _resolve_for_containment(workspace)
    if resolved_config == resolved_workspace or resolved_workspace in resolved_config.parents:
        raise OSError(
            f"the Claude config directory {config_dir} is inside the task workspace {workspace}; "
            "refusing to run, because the provider's credential would be committed to the "
            "requesting user's repository")

    target = transcript_dir(workspace)
    target.mkdir(parents=True, exist_ok=True)

    link = config_dir / "projects" / transcript_dir_name(workspace)
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        # Ours from a previous task, or pointing somewhere stale. A symlink holds no data, so
        # replacing one destroys nothing.
        link.unlink()
    elif link.exists():
        # Anything else here is real content, and it is most likely a conversation: a provider that
        # served tasks before this slice had no symlink, so Claude Code created this directory
        # itself and filled it. Deleting it would destroy exactly the history this feature carries.
        # Refuse, and say what to do — `symlink_to` would raise `FileExistsError` here anyway, but
        # with a bare "[Errno 17] File exists" that reads like a bug in grid rather than a
        # one-time cleanup an operator can perform.
        raise OSError(
            f"the Claude transcript directory {link} is a real directory, not grid's symlink; "
            f"move or remove it so grid can link it into {target}")
    link.symlink_to(target, target_is_directory=True)
    return target


@dataclass(frozen=True)
class ResumeDecision:
    """Whether this task continues the project's conversation, and — when it does not — why not.

    One value rather than an `str | None`, because the caller needs both halves: the id to spawn
    with, and a reason to publish. A bare `None` would make "this project has no conversation yet"
    and "it has one and we could not use it" the same answer, and the second is the one a user
    watching a follow-up task start from nothing needs to see.
    """

    session_id: str | None = None
    reason: str | None = None


def resumable_session(workspace: Path, requested: str | None) -> ResumeDecision:
    """The session this task should resume, given what the relay asked for and what is on disk.

    Missing or unreadable is an ORDINARY outcome, not a failure: the project's first task has no
    conversation, and a task whose predecessor failed never had its transcript fast-forwarded onto
    `main`, so the checkout legitimately arrives without one. Failing here would strand the project;
    starting fresh and saying so is the behaviour ADR 0032 asks for.
    """
    if not requested:
        return ResumeDecision()
    if not isinstance(requested, str):
        # `claim_task` returns the relay's JSON verbatim, so this field can arrive as any type.
        # Without this, `re.match` raises `TypeError` — and this call sits outside `run_task`'s
        # try/except blocks, so the raise would unwind past the push and lose the agent's whole
        # attempt over a field whose only job is to be optional. Every other wire-sourced value
        # here is already cast or type-checked; this one gets the same rule.
        return ResumeDecision(
            reason=f"the relay named session {requested!r}, which is not a safe id")
    if not _SAFE_SESSION_ID.match(requested):
        # The id arrives off the wire and is used to BUILD A PATH below. The same allowlist rule
        # `workspace_for` applies to a project id, for the same reason: `..` and separators resolve.
        return ResumeDecision(reason=f"the relay named session {requested!r}, which is not a safe id")

    path = transcript_dir(workspace) / f"{requested}.jsonl"
    if path.is_symlink():
        # Nothing legitimate plants one: the transcript is written by the agent through our own
        # symlink, and a checkout cannot create one (`core.symlinks=false`). Following it would read
        # — and then commit — whatever it points at.
        return ResumeDecision(reason=f"the transcript for session {requested} is a symlink")
    if not path.is_file():
        return ResumeDecision(reason=f"no transcript for session {requested} in this workspace")
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            first = handle.readline()
    except OSError as exc:
        return ResumeDecision(reason=f"the transcript for session {requested} is unreadable ({exc})")
    if not first.strip():
        return ResumeDecision(reason=f"the transcript for session {requested} is empty")
    try:
        opening = json.loads(first)
    except (ValueError, RecursionError):
        # `RecursionError` is a `RuntimeError`, not a `ValueError` — naming only the latter leaves a
        # hole that turns a deeply nested line into a crash on the task path.
        return ResumeDecision(reason=f"the transcript for session {requested} is not readable JSON")
    if not isinstance(opening, dict):
        return ResumeDecision(reason=f"the transcript for session {requested} is not readable JSON")
    return ResumeDecision(session_id=requested)


def permission_mode() -> str:
    """The `--permission-mode` this provider runs its agents with."""
    mode = (os.getenv(PERMISSION_MODE_ENV) or "").strip() or DEFAULT_PERMISSION_MODE
    if mode not in _PERMISSION_MODES:
        raise ValueError(
            f"{PERMISSION_MODE_ENV}={mode!r} is not one of {', '.join(_PERMISSION_MODES)}")
    return mode


def agent_argv(binary: str, prompt: str, *, resume: str | None = None) -> list[str]:
    """The child that runs one task.

    `resume` is the project's existing Claude Code session, when there is one and its transcript is
    actually on disk (see `resumable_session`) — the follow-up task then continues that conversation
    instead of starting cold. Keyword-only and defaulted, so the first task on a project, and every
    caller that predates issue 06, builds exactly the argv issue 03 pinned.

    `--output-format stream-json` is what makes progress renderable without reading the transcript
    file, and it requires `--print`; `--verbose` is what this repo's existing Claude seat pairs with
    it (`shared/agent/seats/claude.py`).

    The prompt is an argv ELEMENT and never a shell word. A task's prompt is arbitrary text from a
    user on another machine, so the one thing that must be structurally impossible here is for it to
    be interpreted rather than read.
    """
    argv = [
        binary,
        "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", permission_mode(),
    ]
    if resume:
        argv += ["--resume", resume]
    return argv


def resolve_binary() -> str:
    """Claude Code on this machine, or a refusal that says how to get it.

    `claude_install.resolve()` rather than a second search of our own, and deliberately NOT
    `resolve_or_install()`: that one offers to run the vendor's installer and prompts for consent.
    This runs on a daemon thread with no terminal and no user, where a prompt is a hang.

    An incomplete search is reported alongside the refusal for the reason `_unchecked_note` records
    on the launch path — otherwise an operator whose `~/.local/bin` is unreadable is told the app is
    not installed, and goes and installs an app that was already there.
    """
    from shared.launch import claude_install

    resolution = claude_install.resolve()
    if resolution.binary is not None:
        return resolution.binary
    unchecked = ("; ".join(resolution.unchecked)) if resolution.unchecked else ""
    raise RuntimeError(
        "Claude Code isn't installed on this provider"
        + (f" (couldn't check: {unchecked})" if unchecked else "")
        + f"; install it with: {claude_install.install_instruction()}"
    )


def child_env() -> dict[str, str]:
    """The environment the agent child is handed.

    ADR 0028's rule, applied here: whatever we set is set **on the child process only** — never
    exported to the provider's shell, never written to a config file, never into this process's own
    `os.environ`. Hence a copy.

    Nothing about the grid goes in. Unlike `grid launch claude`, which points the app at the relay,
    a task's agent authenticates with the PROVIDER's own Claude subscription: the requesting user's
    token has no business in this process, and the relay is not the endpoint it should be talking to.

    `CLAUDE_CONFIG_DIR` is set only when the operator fixed one, and it is fixed **per provider**,
    never per user (ADR 0032). The spike measured that a fresh per-user config directory yields
    `Not logged in` even on macOS, where the token lives in the Keychain — a custom config dir
    demands its own credential material, which a per-user directory would then carry into the repo
    it is synced through.
    """
    env = dict(os.environ)
    # Through the same validated read `link_transcript` uses, so the directory this process plants
    # the symlink in and the one the child writes to can never be two different places.
    config_dir = configured_claude_config_dir()
    if config_dir is not None:
        env[_CLAUDE_CONFIG_DIR] = str(config_dir)
    return env
