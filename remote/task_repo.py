"""Getting the task's input into the workspace before the agent starts (ADR 0032, issue 04).

Separate from `task_agent.py` — that module answers "run *what*, *where*", this one answers "against
*what input*". They change for different reasons, and this one changes again at issue 05 when the
transport becomes smart-HTTP.

The workspace is a **real git working copy**, not an extracted archive. That is what lets issue 05
commit and push `task/<id>` from here without rebuilding the checkout, and what makes the reset
below exact rather than approximate.

Two properties are worth stating because they are easy to lose:

  * **The reset is exact.** The workspace is per-PROJECT and persists across tasks — Claude Code
    derives its transcript directory from the working directory, so the path cannot be recreated per
    task. A previous task's leftover file is therefore indistinguishable from this task's input
    unless the reset removes it.
  * **`.grid/` survives the clean.** It is the reserved internal directory (and the one the relay
    refuses on upload for exactly this reason): issue 06's symlinked transcript lives there, and a
    clean that deleted it would destroy the project's conversation on every task.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

# The one directory `git clean` must not touch. LOCKSTEP with the relay's upload validator, which
# refuses any path inside it — the two halves of one rule: nothing may be uploaded here, and nothing
# here may be deleted.
RESERVED_DIR = ".grid"
# Written inside a directory `grid task fetch` created, holding the project it holds. The CLI
# requires it before checking anything out over an existing directory: `.git` alone proves only
# that SOME repository is there, and the user's own is the one that must not be written into.
FETCH_MARKER = "grid-task-fetch"
# The project's trunk, matching the relay's. Only ever the initial branch of a fresh workspace repo;
# the task branch is what is actually checked out.
DEFAULT_BRANCH = "main"
# Wall-clock ceiling for one git invocation. A fetch from a local bundle is milliseconds; this stops
# a wedged git (a stale `index.lock` from a killed previous task) from consuming the task's whole
# deadline before the agent ever starts.
_GIT_TIMEOUT_SECONDS = 120
# Wall-clock ceiling for a git call that crosses the network — the input fetch and the result push.
# Separate from, and much larger than, the local one: those two are bounded by a relay round trip
# and a packfile, not by the milliseconds a local plumbing command takes, and reusing the local
# figure would turn an ordinary slow push into a lost result.
_GIT_NETWORK_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class GitRemote:
    """A project's repository on the relay, and the credential for it.

    Carried as one value rather than two loose arguments so a call site cannot pair a URL with the
    wrong token — and so the token stops being an ambient thing that gets logged by accident.
    """

    url: str
    token: str


class CheckoutError(RuntimeError):
    """The workspace could not be brought to the task's input commit."""


class PushError(RuntimeError):
    """The result could not be committed or pushed.

    Deliberately NOT a `CheckoutError`, because the two demand opposite handling. A checkout that
    fails must fail the task — an agent run against input that never arrived produces a confidently
    wrong answer. A push that fails must **not** report anything at all: the task stays `running`,
    its lease lapses, and issue 07 hands it to another provider. Reporting `failed` would be
    terminal, and a terminal task is one nothing will ever retry.
    """


def _env(token: str | None = None) -> dict[str, str]:
    """The environment every git child gets.

    `HOME` points at a directory that does not exist, and `GIT_CONFIG_NOSYSTEM` covers
    `/etc/gitconfig`: between them, no configuration file on the provider can reach these commands.
    That matters more here than on the relay — a `[core] hooksPath` in the provider operator's own
    `~/.gitconfig` would otherwise run on a repository whose contents came off the wire.

    When a `token` is given it is injected as git configuration **through the environment**
    (`GIT_CONFIG_COUNT`/`_KEY_n`/`_VALUE_n`), never as `-c` on the command line and never inside the
    URL. On Linux `/proc/<pid>/cmdline` is world-readable while `/proc/<pid>/environ` is owner-only,
    and a provider serves inference beside a running task — so argv would publish a year-long grid
    credential to every local `ps`. Neither `GIT_CONFIG_NOSYSTEM` nor `GIT_CONFIG_GLOBAL` blocks
    this mechanism; they cover files, and this is not one.

    `http.followRedirects=false` rides along for a reason of its own: `http.extraHeader` is sent to
    whatever host git ends up talking to, so a redirect would hand the grid token to a third party.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "HOME": "/nonexistent",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "grid",
        "GIT_AUTHOR_EMAIL": "grid@invalid",
        "GIT_COMMITTER_NAME": "grid",
        "GIT_COMMITTER_EMAIL": "grid@invalid",
    }
    if token:
        env.update({
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {token}",
            "GIT_CONFIG_KEY_1": "http.followRedirects",
            "GIT_CONFIG_VALUE_1": "false",
        })
    return env


def _run(workspace: Path, *args: str, token: str | None = None,
         timeout: float = _GIT_TIMEOUT_SECONDS) -> str:
    """One git invocation inside `workspace`, or `CheckoutError` carrying git's own words.

    `-c core.symlinks=false` and `-c core.hooksPath=` are set on EVERY call rather than once at
    clone time, because a config written into `.git/config` is itself something a previous run could
    have changed. They are the last two layers behind "the relay only ever writes mode 100644" and
    "git refuses to check out a path with a `.git` component":

      * `core.symlinks=false` makes git materialize a symlink object as a plain file, so even an
        object that should not exist cannot become a link into the provider's config directory.
      * `core.hooksPath` pointed at nothing means no hook in this repository can run, whatever a
        fetched object claims.
    """
    try:
        proc = subprocess.run(
            ["git", "-c", "core.symlinks=false", "-c", "core.hooksPath=/nonexistent/hooks",
             "-C", str(workspace), *args],
            capture_output=True, text=True, env=_env(token), timeout=timeout)
    except subprocess.TimeoutExpired:
        raise CheckoutError(f"git {args[0]} timed out after {timeout:.0f}s") from None
    except OSError as exc:
        # No git on this provider. Worth its own words: every task fails until an operator installs
        # it, and the bare OSError does not say which file was missing.
        raise CheckoutError(f"could not run git ({exc}); is git installed on this provider?") from None
    if proc.returncode != 0:
        raise CheckoutError(
            f"git {args[0]} failed ({proc.returncode}): {proc.stderr.strip()[-500:]}")
    return proc.stdout


def _ensure_repo(workspace: Path) -> None:
    """Make `workspace` a git working copy, and keep `.grid/` out of every commit made from it.

    The exclude is written on every call rather than only at init, because it is what stops issue
    06's transcript symlink — the provider's own state, living inside the workspace — from being
    committed into the requesting user's repository by `git add -A`. It is the third face of one
    rule whose other two are already in place: the relay refuses `.grid/` on upload, and the clean
    below spares it. A file that only got written at init would go missing the first time a
    workspace was restored from anywhere else.
    """
    if not (workspace / ".git").exists():
        _run(workspace, "init", "--quiet", f"--initial-branch={DEFAULT_BRANCH}", ".")
    exclude = workspace / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text(f"# written by grid — see ADR 0032\n/{RESERVED_DIR}/\n")


def materialize(workspace: Path, *, url: str, token: str, branch: str,
                input_commit: str) -> None:
    """Bring `workspace` to exactly `input_commit` on `branch`. Raises `CheckoutError` on any failure.

    Raising rather than returning a status is deliberate: the ONLY safe response to input that did
    not arrive is to not start the agent. An agent run against missing input produces a confidently
    wrong result with nothing anywhere indicating why — the precise failure ADR 0032 D-b exists to
    prevent.

    Fetched from the relay's smart-HTTP front rather than from a bundle (issue 05): the same
    repository the result is pushed back to, so the provider negotiates what it is missing instead
    of downloading the project's whole history per task.
    """
    if not branch or not input_commit:
        raise CheckoutError("the claim gave no branch or input commit to check out")
    if not url:
        raise CheckoutError("the claim gave no git remote to fetch the input from")

    _ensure_repo(workspace)
    _run(workspace, "fetch", "--quiet", url, f"+refs/heads/{branch}:refs/heads/{branch}",
         token=token, timeout=_GIT_NETWORK_TIMEOUT_SECONDS)

    # `symbolic-ref` rather than `checkout -B`: the workspace may hold a previous task's modified
    # files, and `checkout` REFUSES to move a branch when that would overwrite local changes. Here
    # discarding them is the whole intent, so point HEAD first and let `reset --hard` do the work.
    _run(workspace, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
    _run(workspace, "reset", "--quiet", "--hard", input_commit)
    # `-x` ignores `.gitignore` (a leftover build artefact is still a leftover), `-ff` reaches into
    # a nested repository a previous agent may have cloned, and `-e` spares the one directory that
    # is ours rather than the task's.
    _run(workspace, "clean", "--quiet", "-ffdx", "-e", RESERVED_DIR)


def commit_and_push(workspace: Path, *, url: str, token: str, branch: str,
                    message: str) -> str:
    """Commit whatever the agent left and push `branch`. Returns the result commit.

    Run for **every** terminal outcome, success and failure alike (ADR 0032 D-e): a failed attempt
    still commits and still pushes, so the user can see what the agent did before it broke and
    cherry-pick what was right. Only the relay decides whether `main` follows.

    `--allow-empty` keeps this to one code path. An agent that changed nothing is an ordinary
    outcome, not an error, and an empty commit says so truthfully — while the alternative, branching
    on `status --porcelain`, would leave `result_commit` meaning two different things.

    Raises `PushError`, never `CheckoutError`: see that class for why the distinction is
    load-bearing rather than tidy.
    """
    if not branch:
        raise PushError("no branch to push the result to")
    if not url:
        raise PushError("no git remote to push the result to")

    try:
        _run(workspace, "add", "-A")
        _run(workspace, "commit", "--quiet", "--allow-empty", "-m", message)
        commit = _run(workspace, "rev-parse", "HEAD").strip()
    except CheckoutError as exc:
        raise PushError(f"could not commit the result: {exc}") from None

    try:
        # The refspec is spelled out rather than pushed as `HEAD`: the relay authorizes the exact
        # ref name it is handed, and a detached or re-pointed HEAD would name something else.
        _run(workspace, "push", "--quiet", url, f"refs/heads/{branch}:refs/heads/{branch}",
             token=token, timeout=_GIT_NETWORK_TIMEOUT_SECONDS)
    except CheckoutError as exc:
        raise PushError(f"could not push {branch}: {exc}") from None
    return commit


def fetched_project(dest: Path) -> str | None:
    """The project a previous `grid task fetch` left here, or `None` if this is not such a directory.

    The CALLER decides what to do about it. This only reports, because "may I write here" is a
    question about the user's intent and belongs in the command, not in the git layer.
    """
    marker = dest / ".git" / FETCH_MARKER
    try:
        return marker.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def checkout_result(dest: Path, *, url: str, token: str, branch: str, commit: str,
                    project_id: str = "") -> None:
    """Put a task's finished branch into a directory the CLIENT chose.

    Deliberately not `materialize`: that one resets hard and cleans everything unrelated, which is
    right for a workspace the provider owns and catastrophic for a directory a user named.

    It is **not** safe to point at an arbitrary directory, and an earlier version of this docstring
    claimed otherwise. `git checkout -- .` overwrites a file of the same name without complaint —
    unlike `git checkout <branch>`, which refuses — so a colliding path in a destination the user
    picked is destroyed silently. Untracked files with no counterpart in the branch survive, which
    is what makes the marker below the whole guard rather than a nicety: the CALLER must establish
    that this directory is one of ours before calling.
    """
    if not branch or not commit:
        raise CheckoutError("this task has no result to fetch yet")

    _ensure_repo(dest)
    # Written before the checkout, so a fetch interrupted halfway still leaves a directory the next
    # run recognizes as its own instead of one it refuses to touch.
    (dest / ".git" / FETCH_MARKER).write_text(f"{project_id}\n", encoding="utf-8")
    _run(dest, "fetch", "--quiet", url, f"+refs/heads/{branch}:refs/heads/{branch}",
         token=token, timeout=_GIT_NETWORK_TIMEOUT_SECONDS)
    _run(dest, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
    _run(dest, "reset", "--quiet", commit)
    _run(dest, "checkout", "--quiet", "--", ".")
