"""What child a task spawns, where it runs, and what environment it is handed (ADR 0032, issue 03).

Separate from `remote/tasks.py` — that module answers "claim, run, report", this one answers "run
*what*, *where*". They change for different reasons: the loop's shape is settled, while the agent's
argv, its workspace and its credential posture are the parts this feature keeps moving.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

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


def permission_mode() -> str:
    """The `--permission-mode` this provider runs its agents with."""
    mode = (os.getenv(PERMISSION_MODE_ENV) or "").strip() or DEFAULT_PERMISSION_MODE
    if mode not in _PERMISSION_MODES:
        raise ValueError(
            f"{PERMISSION_MODE_ENV}={mode!r} is not one of {', '.join(_PERMISSION_MODES)}")
    return mode


def agent_argv(binary: str, prompt: str) -> list[str]:
    """The child that runs one task.

    `--output-format stream-json` is what makes progress renderable without reading the transcript
    file, and it requires `--print`; `--verbose` is what this repo's existing Claude seat pairs with
    it (`shared/agent/seats/claude.py`).

    The prompt is an argv ELEMENT and never a shell word. A task's prompt is arbitrary text from a
    user on another machine, so the one thing that must be structurally impossible here is for it to
    be interpreted rather than read.
    """
    return [
        binary,
        "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", permission_mode(),
    ]


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
    config_dir = (os.getenv(CLAUDE_CONFIG_DIR_ENV) or "").strip()
    if config_dir:
        env[_CLAUDE_CONFIG_DIR] = config_dir
    return env
