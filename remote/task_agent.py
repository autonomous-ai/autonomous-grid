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

from . import task_repo, task_sandbox, task_worktree

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


# Print mode cannot answer a permission prompt — it denies it — so the default has to be a mode that
# lets a task do work. `acceptEdits` is that mode once the agent is confined: the sandbox's
# `autoAllowBashIfSandboxed` approves Bash *because* the command is confined, and edits are accepted,
# which a real build-and-test task was measured completing under.
#
# It was `bypassPermissions` until issue 23, and the change is a measured requirement rather than a
# tightening for its own sake — see `_BYPASS_MODE` below.
DEFAULT_PERMISSION_MODE = "acceptEdits"
PERMISSION_MODE_ENV = "GRID_TASK_PERMISSION_MODE"
# The binary's own accepted set (`claude --permission-mode`, 2.1.221). Validated here so a typo is
# explained once, rather than becoming "the agent failed" on every task for the life of the process.
_PERMISSION_MODES = ("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan")
# The mode that turns the confinement into decoration (ADR 0033 D-n, issue 23). MEASURED on 2.1.223:
# with `--permission-mode bypassPermissions` and the entire sandbox policy in force — `enabled`,
# `denyRead`, `credentials.files` — an agent asked for a file outside its workspace was handed it.
# The sandbox confines the commands the MODEL runs; the `Read` tool is run by the Claude Code process
# itself, and this mode is precisely what stops the permission layer from refusing it. So the two are
# refused together rather than silently coexisting.
_BYPASS_MODE = "bypassPermissions"

# The agent's own variable, and the knob an operator sets to point every task at one fixed config
# directory. Two names because they are two different things: ours is grid configuration, theirs is
# the child's environment, and conflating them would export the grid's namespace into the agent.
_CLAUDE_CONFIG_DIR = "CLAUDE_CONFIG_DIR"
CLAUDE_CONFIG_DIR_ENV = "GRID_TASK_CLAUDE_CONFIG_DIR"

# Which settings files the agent may load (ADR 0033 D-f, issue 22). `user` — the operator's own
# `CLAUDE_CONFIG_DIR`, on the operator's own machine — and deliberately NOT `project` or `local`,
# which are read out of the WORKSPACE: a tree that arrived over the wire.
#
# This is a security boundary, not a preference. `-p` **skips the workspace-trust dialog** ("Only use
# this in directories you trust" — `claude --help`, 2.1.223) and stdout is a pipe, so both halves of
# the binary's own protection are already off by the time the child starts. Measured on 2.1.223: a
# `.claude/settings.json` carrying a `SessionStart` hook RUNS — before the model has said anything,
# with no permission prompt, as the provider's own user with its real `HOME` — and this flag stops
# it while leaving the config directory's own settings loading.
#
# It is the *repository's settings* that are refused. Its instructions stay READABLE — `CLAUDE.md`,
# `.claude/agents/` and `.claude/skills/` are all still on disk in the workspace, and an agent that
# looks finds them. The line this draws is narrow and firm: **no shell command runs before the model
# has said anything.**
#
# **Corrected while building issue 23**, because the original claim here was stronger and wrong. It
# said those files still *load*. Measured on 2.1.223 with a prompt that forbids tools, so that only
# auto-loaded context can answer: with `--setting-sources user` the model answered `UNKNOWN`, and
# with no `--setting-sources` at all it answered from `CLAUDE.md`. So this flag DOES stop the
# workspace's `CLAUDE.md` being auto-discovered, and the earlier measurement was watching the model
# open the file with the `Read` tool. ADR 0033 D-f and `docs/cli.md` carry the same wrong sentence.
# The flag is not changed here: it is what closes the execution hole, and buying memory discovery
# back by reopening that is issue 22's decision to take, not a side effect of this one.
_SETTING_SOURCES = "user"
# `.mcp.json` is the same hole wearing a different name — a stdio server is a command line, and it is
# STARTED at session start (measured: the control run's server process ran; with this flag the init
# event's `mcp_servers` is empty). It also drops the operator's own MCP servers, which is right for a
# task agent: nothing about the provider's desktop belongs in a stranger's repository.
_STRICT_MCP_CONFIG = "--strict-mcp-config"


# What the agent child inherits from this process, by NAME (ADR 0033 D-n, issue 23 layer 1).
# Everything here is either "a program cannot run without it" or "the vendor's own configuration";
# an allowlist rather than a deny list for the reason `_SAFE_PROJECT_ID` gives — a deny list has to
# anticipate every name worth hiding, and the next tool an operator installs invents one.
_ENV_ALLOWLIST = frozenset({
    # Without these nothing runs, or runs somewhere unintended.
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TERM", "TZ", "LANG",
    # TLS trust and egress, which a provider behind a corporate proxy or a private CA needs.
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
})
# `LC_*` completes `LANG`. `ANTHROPIC_*` is the vendor's own configuration — the model an operator
# pinned, and the API key a provider that does not use a subscription authenticates with. It reaches
# the PROCESS deliberately and is withheld from the process's own sandboxed commands by
# `task_sandbox`'s `credentials.envVars`, which is the split issue 23 asks for: the allowlist governs
# what the agent gets, the sandbox governs what its children see.
_ENV_ALLOWED_PREFIXES = ("LC_", "ANTHROPIC_")

# The operator's own extension to the allowlist — comma- or space-separated names. It exists because
# an allowlist nobody can extend is one a provider works around by not upgrading: a team with a
# private package registry needs its token in a build, and "hand the agent the whole environment
# again" must not be the only way to get it.
ENV_PASSTHROUGH_ENV = "GRID_TASK_ENV_PASSTHROUGH"

# The operator's own git configuration must not apply to a tree that arrived over the wire
# (ADR 0033 D-f). `task_repo._env` hardens the PROVIDER's git calls with
# `-c core.symlinks=false -c core.hooksPath=`; the agent's own are unhardened, run in the checkout,
# and see the operator's real `HOME` — so a `core.hooksPath` in `~/.gitconfig` is a shell command
# waiting for the agent's first `git commit`. `HOME` cannot be withheld instead: Claude Code's
# credential lives under it.
#
# Git tolerates the missing config (measured: warns, exits 0).
#
# ⚠️ **It does NOT stop an agent committing, and an earlier version of this comment said it did.**
# Re-measured on git 2.54.0 with exactly this environment — a real `HOME`, both config paths at
# `/dev/null`: `git commit` does not fail for want of `user.name`. It AUTO-DETECTS one from the OS
# username and the hostname and exits 0, so the commit is authored `<user>@<hostname>` — the
# provider's own machine, written into the requesting team's history, which is the precise outcome
# issue 21 exists to prevent and the one thing ADR 0033 records as not retroactively fixable.
#
# Latent until ADR 0033 issue 15, because nothing had ever asked an agent to commit: the provider
# made every commit itself, with `GIT_AUTHOR_*` set from the claim. A merge task's prompt tells the
# agent to commit the merge. So the identity is now put on the child's environment below
# (`_git_identity`), and an agent commit and a grid commit come out identical.
#
# Since issue 16b the floor also carries `task_repo.GIT_SAFETY_CONFIG` — the `core.symlinks` and
# `core.hooksPath` settings the PROVIDER's own git has always had as `-c` flags, which the agent's
# git never got. Import is what makes that matter: before it the relay wrote mode `100644` literally
# and a `120000` object could not exist in a project at all.
#
# Carried as `GIT_CONFIG_COUNT`/`_KEY_n`/`_VALUE_n` rather than as files, because the two file paths
# above are already `/dev/null` — these are settings, and that mechanism is the only one that
# survives having no config file to write them in. Measured on git 2.54.0: it also outranks the
# repository's own `.git/config`, so an agent that rewrites the workspace's config does not get the
# link back. What it does NOT outrank is `-c` on the agent's own command line — see
# `GIT_SAFETY_CONFIG` for what that bounds this to.
_GIT_CONFIG_FLOOR = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


def _git_safety_env() -> dict[str, str]:
    """`GIT_SAFETY_CONFIG` as git's own environment-borne configuration.

    The count is derived from the tuple rather than written down, so adding a third setting cannot
    leave `GIT_CONFIG_COUNT` naming two and silently dropping the new one — a miscount fails open,
    which is the direction that must not be possible here.
    """
    from remote import task_repo

    env = {"GIT_CONFIG_COUNT": str(len(task_repo.GIT_SAFETY_CONFIG))}
    for index, (key, value) in enumerate(task_repo.GIT_SAFETY_CONFIG):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env
# POSIX's own shape for a variable name, so a value that could never have been a variable is caught
# where it can be explained rather than becoming a child that behaves oddly for the life of a fleet.
_ENV_NAME = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


def _passthrough_env_names() -> tuple[str, ...]:
    """The extra variables this operator declared, validated."""
    declared = (os.getenv(ENV_PASSTHROUGH_ENV) or "").replace(",", " ").split()
    for name in declared:
        if not _ENV_NAME.match(name):
            raise ValueError(
                f"{ENV_PASSTHROUGH_ENV} names {name!r}, which is not an environment variable name; "
                f"list names only, separated by commas or spaces")
    return tuple(declared)


def _is_allowed_env(name: str) -> bool:
    return name in _ENV_ALLOWLIST or name.startswith(_ENV_ALLOWED_PREFIXES)


def workspace_root() -> Path:
    return Path(os.getenv(WORKSPACE_ROOT_ENV) or DEFAULT_WORKSPACE_ROOT)


def _safe_segment(kind: str, value: str) -> str:
    """`value` if it is one safe path component, else a `ValueError` naming WHICH one it was.

    The values reaching this all arrive off the wire, so they are attacker-controlled and are
    validated HERE — where the path is built — rather than at each caller.
    `Path(root) / "../../etc"` is not a theoretical escape: it resolves, and the provider would then
    create that directory and run an agent with write access inside it. `Path(root) / "/etc"` is
    worse still: pathlib *discards* the left operand when the right one is absolute, so the root is
    silently gone (ADR 0032 D-b applies the same rule to the filenames a client uploads).

    `kind` is in the message because there are now THREE segments and they come from different
    places — a project id the relay minted, a `member_key` it derived, and a `conversation_id` it
    minted — so "not a safe path segment" on its own would leave an operator with three things to
    check and no way to tell which.

    ⚠️ **One NAME is refused as well as one shape** (ADR 0034 D-c, issue 50). The object store lives
    at `<member_key>/store.git`, and `_SAFE_PROJECT_ID` admits a dot — so a conversation id spelled
    exactly that is legal by every other rule here and would put a workspace on top of the member's
    entire history. Refused in all three positions rather than only the one where the collision is
    reachable: one rule is checkable, and "which levels does the store sit between" is the kind of
    thing a later layout change moves.
    """
    if not isinstance(value, str):
        raise ValueError(f"{kind} must be a string, got {type(value).__name__}")
    if not value or len(value) > _MAX_PROJECT_ID_CHARS:
        raise ValueError(f"{kind} must be 1-{_MAX_PROJECT_ID_CHARS} characters, got {len(value)}")
    if value in (".", "..") or not _SAFE_PROJECT_ID.match(value):
        raise ValueError(f"{kind} {value!r} is not a single safe path segment")
    if value == task_worktree.STORE_DIR_NAME:
        raise ValueError(
            f"{kind} {value!r} is the object store's own directory name, so a workspace built from "
            f"it would sit on top of this member's whole git history")
    return value


def workspace_for(project_id: str, member_key: str, conversation_id: str) -> Path:
    """The working directory one turn of `conversation_id` runs in (ADR 0033 D-g, ADR 0034 D-c).

    Keyed on the TRIPLE. Claude Code derives a session's transcript directory from the working
    directory, so the cwd *is* the conversation's identity — and the two levels below `projects`
    are the same argument made twice:

      * `member_key` (D-g) — two MEMBERS' tasks landing on one provider would otherwise share a
        directory that `materialize` opens with `reset --hard` and `clean -ffdx`, deleting one
        agent's work while it runs;
      * `conversation_id` (D-c) — one member's two CONVERSATIONS are two Claude Code sessions, and
        one directory can only ever be one session. Without this level a person opens a second
        conversation and it resumes the first, which is precisely what issue 38 exists to stop.

    **All three** segments are validated, because each is just as much off the wire as the first:
    `member_key` is the relay's own `sha256(user_id)` truncated and `conversation_id` its own
    `uuid4()`, but this provider is handed both by an authenticated party, and authenticated is not
    trusted. It is also why the key exists at all — the raw `user_id` is `grid:<network>:<sub>`, and
    a colon fails `_SAFE_PROJECT_ID` here for the same reason git refuses it in a ref name.

    `conversation_id` is a REQUIRED positional and deliberately has no default. A default would
    quietly rebuild the member-level path for any caller that forgot it — the exact directory this
    level exists to stop two conversations sharing — and nothing downstream would look wrong.
    """
    return (workspace_root() / "projects"
            / _safe_segment("project id", project_id)
            / _safe_segment("member key", member_key)
            / _safe_segment("conversation id", conversation_id)
            / "workspace")


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


def ensure_cache(workspace: Path) -> Path:
    """Create the writable cache tree beside `workspace`, before any agent runs.

    It has to exist up front: the sandbox grants exactly `<member_key>/workspace` and
    `<member_key>/cache`, and NOT the `<member_key>/` directory that holds them — so a package
    manager handed `…/cache/npm` can create its own subdirectories but could never create `cache`
    itself. Left to the agent, the very failure this fixes comes back wearing a different errno.

    Only the cache root: there is deliberately no `tmp` beneath it, because `TMPDIR` is not
    redirected here — see `_cache_env` for the 108-byte `sun_path` limit that decided it.

    Reuses `ensure_workspace` for the mode discipline instead of restating it — the same
    `_DIR_MODE`, the same refusal on a symlink, the same "only levels we create get their mode
    from us".
    """
    cache = Path(task_sandbox.cache_dir(workspace))
    ensure_workspace(cache)
    return cache


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


# How long that name may get before the binary stops using it verbatim.
#
# MEASURED against Claude Code 2.1.232 on 2026-08-15, and found by `tests/e2e_cross_repo/
# e2e_live_agent.py` rather than reasoned: past a limit the binary keeps a PREFIX of the flattened
# path and appends a short hash. In one operator's `~/.claude/projects/`, a 186-character name was
# kept whole while every over-long one was written as exactly **207** characters — a 200-character
# prefix, a hyphen, and a 6-character suffix (`xw9fqz`, `8zkx3g`, …) that is not derivable from the
# path. Our own computation for those runs was 232.
#
# So beyond the limit this provider CANNOT know where the binary will write. That is not a cosmetic
# mismatch: `link_transcript` plants its symlink at the name we compute, nothing writes through it,
# and the transcript never reaches the worktree — while the task completes, the session id comes
# back and the push lands. Issue 06's failure exactly, re-armed by ADR 0034 D-c, which adds 37
# characters to every workspace path.
#
# ⚠️ **200, not 207, and the data cannot tell them apart.** Every truncated name is 207 long, which
# is what BOTH "cap at 207" and "cap at 200, then append 7" produce; the only name observed kept
# whole was 186, which discriminates neither. The direction to be wrong in is refusing a provider
# that would have worked, never accepting one that silently loses every conversation it runs. The
# stock layout flattens to **135**, so this has 65 characters of headroom against a real deployment
# and cannot fire on one.
TRANSCRIPT_NAME_MAX_CHARS = 200


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


def transcript_dir(workspace: Path, member_key: str) -> Path:
    """Where THIS MEMBER's transcript and `memory/` live inside the git worktree (ADR 0033 D-g).

    Per member, not per project, and it is the same fact as `workspace_for`'s second level rather
    than a matching decision: Claude Code derives the transcript directory from the cwd, so keying
    these two differently would mean one of them is wrong. Sharing one directory would also have
    every member appending to the same JSONL, which conflicts on every integration — and a conflict
    inside a conversation is the last thing anyone wants an agent resolving.

    Validated here as well as in `workspace_for` for that function's own reason: this builds a path
    out of a wire value, and the rule belongs where the path is built rather than at each caller.

    Committed, and therefore **readable by every other member** once the branch has been promoted
    and integrated. That is a property of the design — travelling in the ordinary result commit is
    what makes cross-provider resume work at all — and per-member directories do not change it.
    """
    return (workspace / task_repo.RESERVED_DIR / task_repo.TRANSCRIPT_DIR
            / _safe_segment("member key", member_key))


def link_transcript(workspace: Path, member_key: str) -> Path:
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

    # BEFORE anything is created. Past `TRANSCRIPT_NAME_MAX_CHARS` the binary writes to a name we
    # cannot compute (see that constant), so the link below would be planted somewhere nothing
    # writes through and the conversation would be lost from the first turn with every other signal
    # healthy. `run_task` treats a failure here as terminal, which is the right trade: no provider
    # can fix this by retrying, and the operator is told exactly what to shorten.
    name = transcript_dir_name(workspace)
    if len(name) > TRANSCRIPT_NAME_MAX_CHARS:
        raise OSError(
            f"the workspace path flattens to a {len(name)}-character transcript directory name, "
            f"and Claude Code stops using such a name verbatim past {TRANSCRIPT_NAME_MAX_CHARS} — "
            f"it keeps a prefix and appends a hash this provider cannot reproduce, so the "
            f"conversation would be written outside the repository and lost. Point "
            f"{WORKSPACE_ROOT_ENV} at a shorter directory ({workspace})")

    target = transcript_dir(workspace, member_key)
    target.mkdir(parents=True, exist_ok=True)

    link = config_dir / "projects" / name
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


def resumable_session(workspace: Path, requested: str | None, member_key: str) -> ResumeDecision:
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

    path = transcript_dir(workspace, member_key) / f"{requested}.jsonl"
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
    """The `--permission-mode` this provider runs its agents with.

    Refuses one combination, and it is the whole reason this function is not a one-line `getenv`:
    `bypassPermissions` **with the sandbox on** is a provider that looks confined and is not.
    """
    mode = (os.getenv(PERMISSION_MODE_ENV) or "").strip() or DEFAULT_PERMISSION_MODE
    if mode not in _PERMISSION_MODES:
        raise ValueError(
            f"{PERMISSION_MODE_ENV}={mode!r} is not one of {', '.join(_PERMISSION_MODES)}")
    if mode == _BYPASS_MODE and task_sandbox.enabled():
        raise ValueError(
            f"{PERMISSION_MODE_ENV}={mode!r} cannot be combined with the agent sandbox: the Read "
            f"tool runs inside the Claude Code process, which the sandbox does not confine, and "
            f"this mode is what stops the permission layer refusing it — measured on 2.1.223, a "
            f"task read a file outside its workspace with the whole policy in force. Remove "
            f"{PERMISSION_MODE_ENV}, or set {task_sandbox.SANDBOX_ENV}=0 to run agents unconfined "
            f"deliberately.")
    return mode


def agent_argv(binary: str, prompt: str, *, workspace: Path,
               resume: str | None = None) -> list[str]:
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

    `--setting-sources` and `--strict-mcp-config` are unconditional — see the constants for what they
    defend against. That is only safe because the binary **refuses an argv it does not understand**
    rather than ignoring the flag and running on unprotected, which would report `completed` on every
    task with the hole open and no signal anywhere saying so. Measured, not assumed: an unknown option
    is `error: unknown option '…'` and exit 1, before the model or any hook runs
    (`tests/e2e_agent_settings.py` pins it, and pays nothing to — the binary exits during argv
    parsing). So a provider too old for these flags fails every task loudly, which is why the fleet's
    Claude Code is upgraded BEFORE this is deployed. 2.1.221 is the oldest version measured to know
    and honour both; feature-detecting instead would leave the hole open on exactly the least
    maintained provider, silently.

    `--settings` carries the confinement policy (`task_sandbox`, issue 23), and it does **not**
    inherit the property above. It is a flag every version knows, carrying settings KEYS — and
    unknown keys are dropped in silence, so a binary too old for `sandbox.*` accepts the policy,
    ignores it, and reports `completed` with the agent unconfined. That is why `resolve_binary`
    checks a minimum version, which the argv alone cannot enforce here.
    """
    argv = [
        binary,
        "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", permission_mode(),
        "--setting-sources", _SETTING_SOURCES,
        _STRICT_MCP_CONFIG,
    ]
    if resume:
        argv += ["--resume", resume]
    if task_sandbox.enabled():
        # Last, and only when confinement is on: an operator who turned it off gets the argv this
        # provider built before issue 23, rather than an empty policy that would read as configured.
        argv += ["--settings", task_sandbox.settings_argument(workspace, claude_config_dir())]
    return argv


# The oldest Claude Code measured to know and honour everything this provider's argv depends on:
# issue 22's `--setting-sources` and `--strict-mcp-config`, and issue 23's whole `sandbox.*` settings
# schema (checked against the 2.1.221, 2.1.222 and 2.1.223 bundles). Checked at runtime because
# `--settings` — unlike every other flag here — fails OPEN on a binary that does not understand its
# contents: unknown settings keys are dropped in silence, so an old agent would run unconfined and
# report success. The flags refuse themselves; the policy cannot.
MIN_CLAUDE_VERSION = (2, 1, 221)
_VERSION_TIMEOUT_SECONDS = 10
# `claude --version` answers `2.1.223 (Claude Code)`. Anchored at the start so a wrapper that prints
# its own banner first is treated as unreadable rather than having a number picked out of its prose.
_VERSION_PATTERN = re.compile(r"\A(\d+)\.(\d+)\.(\d+)")
# One subprocess per binary per process, not one per task: this sits on the task path, and a
# provider claiming tasks all day should not pay a process launch to re-learn a constant.
#
# Keyed on what the file IS, not only where it is. Claude Code updates itself in place and a provider
# runs for weeks, so a path-keyed cache would keep answering for a binary that has been replaced. The
# direction that decides this is the dangerous one: a downgrade remembered as new enough would run
# every task unconfined for the life of the process, which is precisely what the gate exists to stop.
# A `stat` costs microseconds; a process launch does not.
_VERSION_CACHE: dict[tuple[str, int, int], tuple[int, int, int]] = {}


def _version_cache_key(binary: str) -> tuple[str, int, int] | None:
    """`(path, mtime, size)`, or `None` when the file cannot be stat'd and nothing may be cached."""
    try:
        info = os.stat(binary)  # follows the installer's symlink to the version actually installed
    except OSError:
        return None
    return (binary, info.st_mtime_ns, info.st_size)


def _binary_version(binary: str) -> tuple[int, int, int]:
    """What `binary --version` reports, or a refusal naming what it said instead."""
    key = _version_cache_key(binary)
    cached = _VERSION_CACHE.get(key) if key is not None else None
    if cached is not None:
        return cached
    import subprocess

    try:
        proc = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, errors="replace",
            timeout=_VERSION_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"could not ask {binary} for its version: {exc}") from exc
    reported = (proc.stdout or proc.stderr or "").strip()
    match = _VERSION_PATTERN.match(reported)
    if match is None:
        raise RuntimeError(
            f"{binary} --version said {reported!r}, which is not a version this provider can check")
    version = (int(match[1]), int(match[2]), int(match[3]))
    if key is not None:
        _VERSION_CACHE[key] = version
    return version


def resolve_binary() -> str:
    """Claude Code on this machine, new enough to honour the confinement, or a refusal.

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
        _require_version_for_the_sandbox(resolution.binary)
        return resolution.binary
    unchecked = ("; ".join(resolution.unchecked)) if resolution.unchecked else ""
    raise RuntimeError(
        "Claude Code isn't installed on this provider"
        + (f" (couldn't check: {unchecked})" if unchecked else "")
        + f"; install it with: {claude_install.install_instruction()}"
    )


def preflight() -> None:
    """Everything about this provider's own configuration that can be checked before work starts.

    All of it is checked anyway, later, by the functions that need it — but `agent_argv` and
    `child_env` are called AFTER the task's checkout and outside `run_task`'s guards, so a provider
    misconfiguration would arrive as a raise out of the task runner having already fetched a
    repository. Called from the guarded pre-spawn block, each of these becomes an ordinary
    "could not start the agent: …" naming the variable to change, on a task that cost nothing.

    Every call here is pure and cheap, so doing it twice costs nothing worth measuring.
    """
    permission_mode()
    _passthrough_env_names()
    if task_sandbox.enabled():
        task_sandbox.preflight()


def _require_version_for_the_sandbox(binary: str) -> None:
    """Refuse a binary that would accept the confinement policy and ignore it.

    Only while the sandbox is on, and that is the whole shape of the rule rather than a convenience:
    the check exists to protect a control that fails open, so an operator who turns the control off
    deliberately gets the provider that existed before issue 23 — including its tolerance for an
    older agent.
    """
    if not task_sandbox.enabled():
        return
    from shared.launch import claude_install

    version = _binary_version(binary)
    if version < MIN_CLAUDE_VERSION:
        current = ".".join(str(part) for part in version)
        minimum = ".".join(str(part) for part in MIN_CLAUDE_VERSION)
        raise RuntimeError(
            f"{binary} is Claude Code {current}, and the task sandbox needs at least {minimum}: an "
            f"older one accepts `--settings` and silently ignores the `sandbox` keys in it, so "
            f"every task would run unconfined and still report success. Upgrade it with: "
            f"{claude_install.install_instruction()}, or set {task_sandbox.SANDBOX_ENV}=0 to run "
            f"agents unconfined deliberately.")


def _git_identity(author) -> dict[str, str]:
    """Who a commit the AGENT makes is authored by (ADR 0033 D-m).

    The same four variables `task_repo._env` puts on the provider's own git calls, and the same
    split: the author is the member whose task this is, the committer is always the grid. So a merge
    the agent commits itself and a merge the grid commits for it are indistinguishable in history.

    Forced rather than allowlisted, for the reason the config floor beside it is: the danger is not a
    variable being inherited, it is git having no answer and inventing one from the hostname.

    `None` gives the pre-0033 identity on both halves — what an older relay's claim, which carries no
    author keys, produces. Never the provider's machine.
    """
    from . import task_repo

    identity = author or task_repo.DEFAULT_IDENTITY
    return {
        "GIT_AUTHOR_NAME": identity.name,
        "GIT_AUTHOR_EMAIL": identity.email,
        "GIT_COMMITTER_NAME": task_repo.DEFAULT_IDENTITY.name,
        "GIT_COMMITTER_EMAIL": task_repo.DEFAULT_IDENTITY.email,
    }


def _cache_env(workspace: Path) -> dict[str, str]:
    """Point every package manager's cache and temp at the writable tree beside the workspace.

    The sandbox denies `$HOME`, and `task_sandbox` re-allows the provider's package caches
    READ-ONLY on purpose — they are shared by every member on the box. A read-only cache does not
    degrade, it fails: MEASURED on the dev VM, a plain `npm install` dies with
    `EROFS: read-only file system, open '/root/.npm/_cacache/tmp/…'`, and `node` then reports
    `MODULE_NOT_FOUND`. `pip` survived the same policy only because it degrades to no-cache.

    So each manager is redirected to `task_sandbox.cache_dir(workspace)` — writable, per
    (project, member), warm on the second task, and a SIBLING of the workspace so
    `commit_and_push`'s `git add -A` never sees it.

    The names are the managers' own documented variables, not a guess: npm reads
    `npm_config_cache`, yarn `YARN_CACHE_FOLDER`, pip `PIP_CACHE_DIR`, and `XDG_CACHE_HOME` catches
    the tools that follow the base-directory spec (uv among them). `CARGO_HOME` is deliberately
    absent — it holds credentials and installed binaries, not only a cache, and moving it per task
    would silently drop an operator's toolchain.

    ⚠️ **`TMPDIR` is NOT here, and it was — for one deploy, on the dev VM, where it broke every
    task.** Pointing it at `<cache>/tmp` looked symmetrical and is fatal: Claude Code's sandbox
    creates Unix domain sockets under `TMPDIR`, and `sun_path` is **108 bytes**, a kernel limit no
    error message mentions. MEASURED on the dev VM against the real layout — the cache tmp path is
    already **107** bytes (`/var/grid-provider/projects/<uuid-36>/<member_key-32>/cache/tmp`), so a
    socket inside it is 128 and `bind()` answers `AF_UNIX path too long`. What the agent sees is
    `Sandbox is required but failed to initialize: Failed to create bridge sockets after 5
    attempts`, EVERY Bash call fails, and — the part that makes it dangerous — the task still ends
    `completed`.

    So the redirection stops at caches, whose paths are only ever opened as files. `/tmp` stays
    unwritable and that is accepted: the bug this function exists for was npm's CACHE
    (`/root/.npm/_cacache`), and an agent that wants scratch space has a writable workspace.
    Anything added here later must be a path a package manager opens, never one a socket is bound
    in.
    """
    cache = Path(task_sandbox.cache_dir(workspace))
    return {
        "npm_config_cache": str(cache / "npm"),
        "YARN_CACHE_FOLDER": str(cache / "yarn"),
        "PIP_CACHE_DIR": str(cache / "pip"),
        "XDG_CACHE_HOME": str(cache / "xdg"),
    }


def child_env(author=None, workspace: Path | None = None) -> dict[str, str]:
    """The environment the agent child is handed — an ALLOWLIST (ADR 0033 D-n, issue 23 layer 1).

    `author` is the project member the claim named (ADR 0033 D-m), used for commits the AGENT makes
    — which only became possible at issue 15, when a merge task's prompt started asking for one.

    `workspace` is what the package-manager cache variables are derived from. Optional so the two
    callers that only want the identity floor keep working, but a task that spawns an agent must
    pass it — without it `npm install` fails outright (see `_cache_env`).

    ADR 0028's rule, applied here: whatever we set is set **on the child process only** — never
    exported to the provider's shell, never written to a config file, never into this process's own
    `os.environ`. Hence a fresh dict.

    It used to be `dict(os.environ)`. The process it copies from also serves inference and holds the
    grid access token, and a task is arbitrary code execution written by somebody else — so `env` in
    a task prompt was a credential dump: the provider's cloud keys, its CI tokens, `GRID_TOKEN`.
    None of that is anything an agent needs to edit a repository.

    Nothing about the grid goes in. Unlike `grid launch claude`, which points the app at the relay,
    a task's agent authenticates with the PROVIDER's own Claude subscription: the requesting user's
    token has no business in this process, and the relay is not the endpoint it should be talking to.

    `CLAUDE_CONFIG_DIR` is set only when the operator fixed one, and it is fixed **per provider**,
    never per user (ADR 0032). The spike measured that a fresh per-user config directory yields
    `Not logged in` even on macOS, where the token lives in the Keychain — a custom config dir
    demands its own credential material, which a per-user directory would then carry into the repo
    it is synced through.
    """
    # Read BEFORE the comprehension, so a malformed list refuses the whole call rather than being
    # applied to some names and not others.
    declared = _passthrough_env_names()
    env = {name: value for name, value in os.environ.items()
           if _is_allowed_env(name) or name in declared}
    # `CLAUDE_CONFIG_DIR` comes from `configured_claude_config_dir()` or from nowhere — dropped HERE,
    # after everything else has been collected, so no route can carry an ambient one in: not the
    # allowlist, not a prefix somebody widens later, and not the operator's own passthrough list.
    # An inherited value would send the child to a directory `claude_config_dir()` knows nothing
    # about, so `link_transcript` would plant its symlink in one place while the agent wrote its
    # transcript in another. Nothing fails — the task completes, the transcript simply never reaches
    # the repository, and every following task on the project starts a fresh conversation while
    # every other signal looks healthy. That is issue 06's bug, reached from the environment.
    env.pop(_CLAUDE_CONFIG_DIR, None)
    # The git floor (ADR 0033 D-f). Forced rather than allowlisted, because the danger here is not a
    # variable being inherited — it is `~/.gitconfig` being found, and a `120000` object an import
    # carried being written as a real link. See the constant.
    #
    # The two halves are applied together and after the passthrough for the same reason the identity
    # below is: an operator who lists `GIT_CONFIG_COUNT` — or whose environment simply holds one —
    # would otherwise leave the count naming a different number of settings than the keys beside it,
    # and git reads the count. That is a floor that disappears without saying so.
    env.update(_GIT_CONFIG_FLOOR)
    env.update(_git_safety_env())
    # And WHO a commit the agent makes is by (ADR 0033 D-m). After the floor, deliberately: the
    # floor is what stops git reading a config, and this is what stops it inventing an identity
    # instead. Forced too, so an operator's passthrough list cannot put a hostname in a team's
    # history.
    env.update(_git_identity(author))
    # Through the same validated read `link_transcript` uses, so the directory this process plants
    # the symlink in and the one the child writes to can never be two different places.
    config_dir = configured_claude_config_dir()
    if config_dir is not None:
        env[_CLAUDE_CONFIG_DIR] = str(config_dir)
    # LAST, and forced like the git floor above: an operator's passthrough list naming
    # `npm_config_cache` would otherwise point a package manager at a path the sandbox does not
    # grant, and the failure is the `EROFS` this exists to remove.
    if workspace is not None:
        env.update(_cache_env(workspace))
    return env
