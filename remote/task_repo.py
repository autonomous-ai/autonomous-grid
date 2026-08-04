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
import tempfile
from pathlib import Path

# The one directory `git clean` must not touch. LOCKSTEP with the relay's upload validator, which
# refuses any path inside it — the two halves of one rule: nothing may be uploaded here, and nothing
# here may be deleted.
RESERVED_DIR = ".grid"
# The project's trunk, matching the relay's. Only ever the initial branch of a fresh workspace repo;
# the task branch is what is actually checked out.
DEFAULT_BRANCH = "main"
# Wall-clock ceiling for one git invocation. A fetch from a local bundle is milliseconds; this stops
# a wedged git (a stale `index.lock` from a killed previous task) from consuming the task's whole
# deadline before the agent ever starts.
_GIT_TIMEOUT_SECONDS = 120


class CheckoutError(RuntimeError):
    """The workspace could not be brought to the task's input commit."""


def _env() -> dict[str, str]:
    """The environment every git child gets.

    `HOME` points at a directory that does not exist, and `GIT_CONFIG_NOSYSTEM` covers
    `/etc/gitconfig`: between them, no configuration file on the provider can reach these commands.
    That matters more here than on the relay — a `[core] hooksPath` in the provider operator's own
    `~/.gitconfig` would otherwise run on a repository whose contents came off the wire.
    """
    return {
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


def _run(workspace: Path, *args: str) -> str:
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
            capture_output=True, text=True, env=_env(), timeout=_GIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise CheckoutError(f"git {args[0]} timed out after {_GIT_TIMEOUT_SECONDS}s") from None
    except OSError as exc:
        # No git on this provider. Worth its own words: every task fails until an operator installs
        # it, and the bare OSError does not say which file was missing.
        raise CheckoutError(f"could not run git ({exc}); is git installed on this provider?") from None
    if proc.returncode != 0:
        raise CheckoutError(
            f"git {args[0]} failed ({proc.returncode}): {proc.stderr.strip()[-500:]}")
    return proc.stdout


def materialize(workspace: Path, bundle: bytes, *, branch: str, input_commit: str) -> None:
    """Bring `workspace` to exactly `input_commit` on `branch`. Raises `CheckoutError` on any failure.

    Raising rather than returning a status is deliberate: the ONLY safe response to input that did
    not arrive is to not start the agent. An agent run against missing input produces a confidently
    wrong result with nothing anywhere indicating why — the precise failure ADR 0032 D-b exists to
    prevent.
    """
    if not branch or not input_commit:
        raise CheckoutError("the claim gave no branch or input commit to check out")

    if not (workspace / ".git").exists():
        _run(workspace, "init", "--quiet", f"--initial-branch={DEFAULT_BRANCH}", ".")

    # The bundle goes to a file: `git fetch` takes a repository path, and a temporary file keeps the
    # bytes out of the workspace, where the clean below would either delete them or (worse) the
    # agent would find them.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "input.bundle")
        with open(path, "wb") as handle:
            handle.write(bundle)
        _run(workspace, "fetch", "--quiet", path, f"+refs/heads/{branch}:refs/heads/{branch}")

    # `symbolic-ref` rather than `checkout -B`: the workspace may hold a previous task's modified
    # files, and `checkout` REFUSES to move a branch when that would overwrite local changes. Here
    # discarding them is the whole intent, so point HEAD first and let `reset --hard` do the work.
    _run(workspace, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
    _run(workspace, "reset", "--quiet", "--hard", input_commit)
    # `-x` ignores `.gitignore` (a leftover build artefact is still a leftover), `-ff` reaches into
    # a nested repository a previous agent may have cloned, and `-e` spares the one directory that
    # is ours rather than the task's.
    _run(workspace, "clean", "--quiet", "-ffdx", "-e", RESERVED_DIR)
