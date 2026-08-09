"""A member's own clone of a project, configured to ask this CLI for a credential (ADR 0033 D-h).

Deliberately **not** part of `remote/task_repo.py`, and the reason is the environment rather than
the line count. `task_repo._env` is provider hardening: `HOME=/nonexistent`,
`GIT_CONFIG_GLOBAL=/dev/null`, `core.symlinks=false`, `core.hooksPath` pointed at nothing. That is
right for a workspace whose contents arrived over the wire onto a machine serving inference, and
wrong twice over here:

  * the credential helper is `grid` itself, and it reads `~/.grid` — an unset or fake `HOME` breaks
    the very mechanism this module exists to install;
  * `core.symlinks=false` checks symlinks out as ordinary files holding their target path, so every
    later `git status` in the member's own clone would report them modified, forever.

What IS kept is `GIT_TERMINAL_PROMPT=0`: a clone that stops to ask for a password is a clone that
hangs, and the helper is supposed to be the only answer to that question.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from shared import jsonio

# Written inside a directory `grid project clone` created, holding the project it holds. Read by
# `grid task fetch`, which must recognise a clone and refuse it — a clone already has the task
# branch, so the answer there is two git commands rather than a new directory.
#
# A separate marker from `task_repo.FETCH_MARKER` because they mean opposite things: that one says
# "this directory is safe to check a result out over", and a clone is precisely not.
CLONE_MARKER = "grid-project-clone"

# Wall-clock ceiling for one git invocation here. Generous: the fetch is a whole repository over the
# relay's transport, which grid-src is allowed 600s to pack (`task_repo.GIT_RPC_TIMEOUT_SECONDS`),
# and this side must not give up while the relay is still working — the same direction, and for the
# same reason, as `task_repo._GIT_NETWORK_TIMEOUT_SECONDS`.
_GIT_TIMEOUT_SECONDS = 900


class CloneError(Exception):
    """Something went wrong that the member has to be told about, in their own words."""


@dataclass(frozen=True)
class Cloned:
    """Where the clone is and what is checked out in it.

    `started_from_trunk` says the member's branch did not exist on the relay yet, so the checkout
    was cut from the trunk. That is the ORDINARY state of a member who has not run a task — a WIP
    branch is created by the relay when one settles, not when a project is created — and it has to
    reach the member, because `git log` in that clone shows somebody else's history and nothing of
    their own.
    """

    path: Path
    branch: str
    trunk: str
    started_from_trunk: bool = False


def _env() -> dict[str, str]:
    """The member's own environment, plus the one thing a clone must not do.

    Inherited rather than replaced — see the module docstring. `GIT_TERMINAL_PROMPT=0` turns "no
    credential" into an immediate error instead of a prompt nobody is watching.
    """
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


# git exit codes that mean "there was nothing to remove", measured on git 2.54.0. Named rather than
# spelled at the call sites, and NARROW on purpose: `ok=(0, 5)` says which failure is expected, so
# every other one still raises. A bare `check=False` would swallow a config write that did not land.
_NO_SUCH_CONFIG_KEY = 5   # `git config --unset-all` on a key that is not set
_NO_SUCH_REMOTE = 2       # `git remote remove` on a remote that is not there

# What may be baked into a clone's `credential.<url>.helper` as `--grid <id>`. Deliberately NARROWER
# than `remote_grid._NETWORK_ID_RE` in the one way that matters: a leading dash is refused, because
# `shlex.quote` leaves it unquoted and argparse would then read it as a flag. Server-issued ids are
# `grid-<hex>`, so nothing legitimate is excluded.
_SAFE_GRID_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")


def _run(cwd: Path, *args: str, ok: tuple[int, ...] = (0,)) -> str:
    try:
        proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                              env=_env(), timeout=_GIT_TIMEOUT_SECONDS)
    except OSError as exc:
        raise CloneError(f"could not run git: {exc}") from None
    except subprocess.TimeoutExpired:
        raise CloneError(f"git {args[0]} took longer than {_GIT_TIMEOUT_SECONDS}s") from None
    if proc.returncode not in ok:
        # The token is never in argv and never in the URL, so this text cannot carry it.
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise CloneError(f"git {args[0]} failed: {detail[-1] if detail else 'no output'}")
    return proc.stdout


class GitUnavailable(CloneError):
    """git could not be RUN, which is a different fact from git being too old.

    Its own type because the two have different fixes and only one of them is "upgrade git". Told
    to upgrade a git that is not installed, a member is also being reassured that `grid task fetch`
    still works — and that shells out to git too.
    """


def credential_capabilities() -> frozenset[str]:
    """What this machine's git can do in the credential protocol, asked of git itself.

    A capability probe rather than a version floor, and that is worth the sentence: the alternative
    is a number duplicated here that has to be measured, maintained, and re-measured every time git
    moves. `git credential capability` answers the question directly.

    The reading rule is git's own (`git-credential(1)`): "If a non-zero exit status is received, or
    if the first line doesn't start with the word version and a space, callers should assume that no
    capabilities are supported." An old git has no such subcommand at all, which lands in the same
    place. Measured on git 2.54.0: `version 0` / `capability authtype` / `capability state`.

    An empty set therefore means exactly one thing — **this git does not support the capability** —
    and every other way the probe can fail raises `GitUnavailable` instead. Collapsing the two would
    make "git is missing" and "git is old" produce the same sentence, and only one of them is fixed
    by upgrading.
    """
    try:
        proc = subprocess.run(["git", "credential", "capability"], capture_output=True, text=True,
                              env=_env(), timeout=30)
    except OSError as exc:
        # git is not on PATH, or is not executable. NOT an empty capability set: that would be read
        # as "your git is too old" and answered with "please upgrade git", which is a confident
        # diagnosis of the wrong illness.
        raise GitUnavailable(f"git could not be run ({exc}). Is it installed and on your PATH?") \
            from None
    except subprocess.TimeoutExpired:
        raise GitUnavailable(
            "`git credential capability` did not answer within 30s, so there is no way to tell "
            "whether this git can carry the credential the grid relay needs.") from None
    lines = proc.stdout.splitlines()
    if proc.returncode != 0 or not lines or not lines[0].startswith("version "):
        return frozenset()
    return frozenset(
        line.split(" ", 1)[1].strip() for line in lines[1:] if line.startswith("capability "))


def require_credential_capability() -> None:
    """Refuse to build a clone this machine's git could never authenticate.

    Fail here, before anything is written, rather than leaving a directory whose every `git pull`
    reports `fatal: Authentication failed` — which names the relay and blames it. The relay's git
    front accepts only `Authorization: Bearer`, and `authtype` is the only way a helper can name a
    scheme; without it git would send Basic, which is refused (measured).
    """
    from . import git_credential

    if git_credential.AUTHTYPE_CAPABILITY not in credential_capabilities():
        raise CloneError(
            "this machine's git cannot carry a `Bearer` credential — `git credential capability` "
            "does not report `authtype`, so git would fall back to a scheme the grid relay refuses. "
            "Please upgrade git, then run this again. `grid task fetch` works meanwhile.")


def cloned_project(dest: Path) -> str | None:
    """The project a `grid project clone` left here, or `None` if this is not such a directory.

    Reports only. Whether a caller may write here is a question about the user's intent and belongs
    in the command — the same split `task_repo.fetched_project` makes.
    """
    try:
        return (dest / ".git" / CLONE_MARKER).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def grid_command() -> str:
    """This CLI, as an absolute path git can run from inside a clone years from now.

    An absolute path rather than the bare name `grid`: an IDE runs git with its own `PATH`, which is
    routinely not the shell's, and a helper git cannot find produces `fatal: could not read
    Username` — a message naming neither grid nor the real fault. `gh` writes an absolute path into
    `.git/config` for the same reason.
    """
    # `os.access(X_OK)` and not merely `is_file()`: run as `python -m …` or from a checkout,
    # `sys.argv[0]` is a `.py` path — a real file that git cannot execute, so writing it into the
    # config would produce a helper that fails on every operation. Falling through to `which` is
    # right there. `.resolve()` because argv[0] may be relative to a cwd this clone will outlive.
    argv0 = Path(sys.argv[0])
    if argv0.name and argv0.is_file() and os.access(argv0, os.X_OK):
        return str(argv0.resolve())
    from shutil import which

    found = which("grid")
    if found:
        return str(Path(found).resolve())
    raise CloneError(
        "could not work out where the `grid` command lives, so the clone's credential helper "
        "would not be runnable. Reinstall grid, or make sure `grid` is on your PATH.")


def helper_command(network_id: str) -> str:
    """The `credential.<url>.helper` value: this CLI, pinned to one grid.

    The `!` (shell snippet) form is not a stylistic choice. `gitcredentials(7)` runs an unquoted
    absolute path verbatim — which breaks the moment the path holds a space — and a QUOTED path no
    longer "begins with an absolute path", so git falls through to its third rule and tries to run
    `git credential-'/Users/...'`. The `!` form is the only one where quoting is both possible and
    honoured.

    The grid is pinned here so the helper does not have to guess which of several stored grids a
    host belongs to. It is not a secret, and the helper checks the host against it anyway.

    ⚠️ **`shlex.quote` is not enough on its own, and the gap is a leading `-`.** Its safe set is
    `[\\w@%+=:,./-]`, so `shlex.quote("-evil")` returns `-evil` UNQUOTED (measured). That would be
    written as `--grid -evil`, which argparse reads as a flag rather than as `--grid`'s value —
    `error: argument --grid: expected one argument`, exit 2, on every credential lookup for that
    clone until somebody hand-edits `.git/config`. `remote_grid._NETWORK_ID_RE` does not stop it
    either: `[A-Za-z0-9_-]+` allows a dash anywhere, including first.

    Server-issued ids are `grid-<hex>`, so reaching this needs a hand-edited credential store — the
    same threat class the token-shape guard already takes seriously. It is refused HERE, at the one
    moment the value is written to disk, rather than left to fail at every later use: a bad quote
    baked into a config outlives the process that wrote it.
    """
    if not _SAFE_GRID_ID.fullmatch(network_id or ""):
        raise CloneError(
            f"grid id {network_id!r} cannot be written into a clone's credential helper — it must "
            f"be letters, digits, dot, underscore or dash, and must not begin with a dash. "
            f"Run `grid login` to refresh your grids.")
    return f"!{shlex.quote(grid_command())} credential --grid {shlex.quote(network_id)}"


def credential_config(relay_base: str, network_id: str) -> list[tuple[str, str]]:
    """The `git config --local --add` pairs that make git ask this CLI, and only this CLI.

    Scoped to the relay's URL, in the CLONE's own config — never `~/.gitconfig`, and never global.

    The empty `helper` FIRST is load-bearing, not defensive. Measured: with a global
    `credential.helper` present and no reset, git consults it first, it answers with its own
    username/password, git sends *that* as Basic, the relay refuses it, and our helper is never
    asked for a credential at all — only for an `erase`. `gitcredentials(7)`: "If credential.helper
    is configured to the empty string, this resets the helper list to empty."

    `proactiveAuth` is what stops every operation opening with an unauthenticated request. Measured
    both ways: git DOES ask the helper after the relay's bare 401 (no `WWW-Authenticate`), so this
    is not required for correctness — but each of those probes is a failed authentication the relay
    counts (grid-src `auth_throttle.record_auth_failure`), and a team of auto-fetching IDEs would
    eventually be throttled off a grid they are entitled to use.

    `interactive` follows `git-config(1)` — "To avoid the possibility of user interactivity from
    Git, set credential.interactive=false" — so a clone whose helper cannot answer errors instead
    of waiting on a prompt. Not measurable here: a non-tty subprocess fails identically either way.
    """
    base = relay_base.rstrip("/")
    return [
        (f"credential.{base}.helper", ""),
        (f"credential.{base}.helper", helper_command(network_id)),
        (f"credential.{base}.interactive", "false"),
        (f"http.{base}.proactiveAuth", "auto"),
    ]


def clone_project(dest: Path, *, url: str, project_id: str, branch: str, trunk: str,
                  relay_base: str, network_id: str) -> Cloned:
    """Put a project's repository in `dest`, configured to ask this CLI for its credential.

    The fetch runs **through the helper**, with no token anywhere in this process's git environment.
    That is the point: the clone either proves the credential path works, here, where the failure
    can be explained — or it fails now instead of inside somebody's IDE next Tuesday.
    """
    dest.mkdir(parents=True, exist_ok=True)
    _run(dest, "init", "--quiet", "-b", trunk)
    # Written BEFORE the fetch, so a clone interrupted halfway still leaves a directory the next
    # command recognises as ours rather than one it has to guess about.
    #
    # Through the codebase's atomic writer, not `write_text`: a plain write can be TRUNCATED by a
    # kill, and `cloned_project` reads an empty marker as "not ours" — so the next attempt would
    # refuse a directory it made, with a message saying it did not, and no way out but deleting the
    # file by hand. An interruption between `git init` and this line still leaves a bare `.git` that
    # is genuinely unrecognisable; atomicity closes the truncation window, not that one.
    #
    # ⚠️ An unreadable marker is deliberately NOT treated as "ours, proceed". An empty one does not
    # say WHICH project, and proceeding on that would let a clone of one project be reset to
    # another's tip — trading a confusing refusal for silent data loss.
    jsonio.atomic_write_bytes(dest / ".git" / CLONE_MARKER, f"{project_id}\n".encode("utf-8"))

    # Re-cloning into a directory this command already made is how a member updates, so every write
    # below has to be idempotent. `--add` alone is not: a second run would leave TWO helper entries
    # and a second reset, and `remote add` would simply fail. Clear each key first, then add — the
    # ORDER of the two helper values is what makes the reset work, so `--replace-all` (which cannot
    # express two values) is not an option.
    for key in dict.fromkeys(key for key, _ in credential_config(relay_base, network_id)):
        _run(dest, "config", "--local", "--unset-all", key, ok=(0, _NO_SUCH_CONFIG_KEY))
    for key, value in credential_config(relay_base, network_id):
        _run(dest, "config", "--local", "--add", key, value)

    _run(dest, "remote", "remove", "origin", ok=(0, _NO_SUCH_REMOTE))
    _run(dest, "remote", "add", "origin", url)
    _run(dest, "fetch", "--quiet", "origin")

    # A member's WIP branch is created by the RELAY, when one of their tasks settles or they commit
    # — not when the project is created and not when they join it. So for everyone who has not run
    # a task yet the ref simply is not there, which is the ordinary case rather than an edge one,
    # and `--track origin/<branch>` fails outright: "not a commit and a branch cannot be created
    # from it". Found by the cross-repo E2E; the unit fixture had always seeded the branch, so no
    # unit test could have seen it.
    has_wip = _remote_has(dest, branch)
    if not has_wip and not _remote_has(dest, trunk):
        raise CloneError(
            f"this project has no {trunk} yet, so there is nothing to clone. "
            f"`grid project import <repository> <project>` is how a project gets one.")

    target = f"origin/{branch}" if has_wip else f"origin/{trunk}"
    # ⚠️ **`checkout -B` resets, and its only safety net is a DIRTY working tree.** Measured on git
    # 2.54.0: an uncommitted edit makes it refuse, a COMMITTED one is discarded in silence at exit 0.
    # A member cannot push, so a local commit is the only git-native way they checkpoint work — and
    # re-cloning is how they pick up somebody else's promote. Those two ordinary acts together are a
    # data-loss path, so it is refused here rather than resolved by guessing.
    #
    # Checked AFTER the fetch on purpose: `origin/*` is then up to date, so every command named in
    # the refusal actually works in the directory the member is standing in.
    discarding = _commits_only_here(dest, branch, target)
    if discarding:
        raise CloneError(
            f"{branch} here has {discarding} commit{'s' if discarding != 1 else ''} the grid does "
            f"not have, and updating this clone would discard them.\n"
            f"  See them:  git -C {dest} log {target}..{branch}\n"
            f"  Land them: grid project commit {project_id} -m '…' --file <path>\n"
            f"  Or keep them aside first: git -C {dest} branch my-work {branch}")

    # `-B` so re-cloning into the same directory is an update rather than a refusal.
    if has_wip:
        # `--track` gives `git pull` an upstream without the member configuring one.
        _run(dest, "checkout", "--quiet", "-B", branch, "--track", f"origin/{branch}")
    else:
        _run(dest, "checkout", "--quiet", "-B", branch, f"origin/{trunk}")
        # Written by hand because `--set-upstream-to` requires the remote ref to EXIST, and the
        # whole point here is that it does not yet. These two keys are what `--track` would have
        # written, so the member's first `git pull` after their first task just works.
        _run(dest, "config", "--local", "branch." + branch + ".remote", "origin")
        _run(dest, "config", "--local", "branch." + branch + ".merge", f"refs/heads/{branch}")
    return Cloned(path=dest, branch=branch, trunk=trunk, started_from_trunk=not has_wip)


def _commits_only_here(dest: Path, branch: str, target: str) -> int:
    """How many commits the local `branch` has that `target` does not — 0 on a first clone.

    Answers the only question that matters before a `checkout -B`: is there anything to lose. A
    branch that does not exist locally yet cannot lose anything, so the first clone never trips it.

    An unreadable count is treated as "there IS something here", not as zero. This guard stands
    between a member and their own unpushed work, and the failure directions are not symmetric: a
    false refusal costs one confusing message, a false zero costs the work.
    """
    if not _local_has(dest, branch):
        return 0
    counted = _run(dest, "rev-list", "--count", f"{target}..{branch}").strip()
    try:
        return int(counted)
    except ValueError:
        raise CloneError(
            f"could not tell whether {branch} here has commits the grid does not "
            f"(`git rev-list --count {target}..{branch}` answered {counted!r}), so this clone was "
            f"left alone rather than reset.") from None


def _local_has(dest: Path, branch: str) -> bool:
    """Whether this clone already has its own `refs/heads/<branch>` — i.e. this is a re-clone."""
    return bool(_run(
        dest, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", ok=(0, 1)).strip())


def _remote_has(dest: Path, branch: str) -> bool:
    """Whether `origin/<branch>` came back from the fetch.

    `rev-parse --verify --quiet` answers 1 for "no such ref", which is an ANSWER here rather than a
    failure — hence the explicit `ok`. Read off the remote-tracking ref rather than `ls-remote`, so
    it costs no second round trip and reports on exactly what this clone fetched.
    """
    proc = _run(dest, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}", ok=(0, 1))
    return bool(proc.strip())
