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
    refuses on upload for exactly this reason): issue 06's transcript lives there, and a clean that
    deleted it would destroy the project's conversation on every task.
  * **`.grid/agent/` is the one part of it that is COMMITTED** (issue 06). The transcript and the
    agent's `memory/` are how a project's conversation reaches its next task and its next provider,
    and the ordinary result commit is the only path they travel. The rest of `.grid/` stays the
    provider's own. The relay still refuses to accept an upload anywhere under `.grid/`, so the
    conversation is written by the provider and by nobody else.
"""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

# The one directory `git clean` must not touch. LOCKSTEP with the relay's upload validator, which
# refuses any path inside it — the two halves of one rule: nothing may be uploaded here, and nothing
# here may be deleted.
RESERVED_DIR = ".grid"
# The one directory INSIDE `.grid/` that is committed: the Claude Code transcript and the agent's
# `memory/`, which is how a project's conversation reaches the next task and the next provider
# (ADR 0032, issue 06). Everything else under `.grid/` stays the provider's own business. Named here
# rather than in `task_agent` because this module owns the repository's layout — it is what writes
# the exclude that carves this one subdirectory back in.
TRANSCRIPT_DIR = "agent"
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
#
# LOCKSTEP with the relay's `task_repo.GIT_RPC_TIMEOUT_SECONDS` (600s, grid-src), and it must stay
# ABOVE it: a fetch the relay is still willing to serve is one this provider would otherwise have
# already abandoned — the relay does the packing anyway and the failure surfaces here, blaming a
# timeout the relay never saw. It must also stay well BELOW the relay's `task_deadline_seconds`
# (3600s), or one checkout can eat the task's whole budget and the reaper ends it before the agent
# runs. `tests/test_task_lease.py` pins both inequalities against grid-src's own source.
#
# Raised from 300s for ADR 0033 issue 16a: a real 581 MiB / 29,133-commit repository takes ~11s to
# fetch on a fast local disk, and the relay is allowed ten minutes for the case that is not fast.
_GIT_NETWORK_TIMEOUT_SECONDS = 900
# Wall-clock ceiling for the workspace listing (ADR 0032 issue 08). Much SHORTER than the local one,
# and that is the whole reason it exists rather than reusing it: this call runs on the lease
# renewer's beat, and `_GIT_TIMEOUT_SECONDS` is 120s — the relay's entire lease TTL. A listing
# allowed to run that long would push the next renewal past the point where the task is reclaimed,
# so the observability feature would cost the task. See `tests/test_task_lease.py` for the
# arithmetic this figure has to satisfy: it is ADDED to the renewal interval in the worst case, and
# the sum has to stay three beats inside the relay's lease TTL. Generous for what it bounds — reading
# the index and walking the working tree — and a workspace pathological enough to exceed it loses a
# snapshot, which is the correct trade against risking the lease.
LS_FILES_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class GitRemote:
    """A project's repository on the relay, and the credential for it.

    Carried as one value rather than two loose arguments so a call site cannot pair a URL with the
    wrong token — and so the token stops being an ambient thing that gets logged by accident.
    """

    url: str
    token: str


@dataclass(frozen=True)
class GitIdentity:
    """Who a commit names. One value rather than two loose strings, so a call site cannot pair a
    name with somebody else's address."""

    name: str
    email: str


# What a commit is authored by when the claim named no member, and what every commit here is
# COMMITTED by. `commit` refuses to run without an identity, so this is the floor, not a nicety.
# HAND-DUPLICATED with the relay's `task_repo.DEFAULT_IDENTITY` — the two halves of one task's
# history have to agree on what anonymous looks like.
DEFAULT_IDENTITY = GitIdentity("grid", "grid@invalid")
# Ceiling on either half of an identity. git accepts a 5000-character name and writes every byte of
# it into the commit object, permanently. This one arrives off the wire, so the bound is input
# validation rather than tidiness.
MAX_IDENT_CHARS = 200
# `<` and `>` frame the address in git's ident line. git strips them itself, along with `\n`; it
# keeps `\r` and `\t`. Measured on git 2.54.0.
#
# A NUL is the sharpest of them and `_clean` below is the ONLY thing standing in front of it: git
# never even gets a chance to be lenient, because Python refuses to build an environment containing
# one, and that `ValueError` is not an `OSError` — so it would escape `_run`'s handler, become a
# `PushError`, and fail the push. Unreachable through `identity_or_default` by construction; stated
# because a future caller that hand-builds a `GitIdentity` has no such guard.
_IDENT_STRIP = "<>"


def _clean(value: str | None) -> str:
    """One half of an identity, reduced to something git will accept and store verbatim.

    Never raises and never refuses: the caller is on its way to a commit, and there is no
    attribution worth failing a task for. See `commit_and_push` for why failing here is far worse
    than losing a name.
    """
    # Wrong TYPE, not merely a wrong value: this arrives as JSON off the wire, and nothing upstream
    # promises a string. Treated as absent rather than coerced with `str()`, which would author a
    # commit `12345` or `{'name': 'Alice'}` — the address's local part still names the member.
    if not isinstance(value, str) or not value:
        return ""
    # `isprintable()` is the whole control-character rule: it is False for every Unicode "Other"
    # and "Separator" except the ASCII space — so NUL, `\n`, `\r`, `\t`, a zero-width space and a
    # non-breaking space all go, and an ordinary space in a real name stays.
    kept = "".join(c for c in value if c.isprintable() and c not in _IDENT_STRIP)
    # Trimmed AGAIN after the cut: truncating mid-name can leave a trailing space, and git refuses a
    # name that consists only of characters it disallows.
    return kept.strip()[:MAX_IDENT_CHARS].strip()


def identity_or_default(name: str | None, email: str | None) -> GitIdentity:
    """The claim payload's author fields as an identity git can commit with.

    **This is a system boundary.** Both fields arrive over the wire, and the relay is authenticated
    rather than trusted — so the same two fallbacks the relay applies are applied again here, and
    for a reason that is this side's own (measured on git 2.54.0):

      * **No usable address ⇒ the whole `DEFAULT_IDENTITY`.** An old relay sends neither field, and
        that has to look exactly like today's behaviour.
      * **No usable name ⇒ the address's local part, then `grid`.** `commit` REFUSES an empty or
        whitespace-only author name outright, and a refusal here is not a lost name — it is a
        `PushError`, a task left `running`, a lapsed lease, and a retry that fails identically on
        every provider in the fleet, forever.
    """
    clean_email = _clean(email)
    if not clean_email:
        return DEFAULT_IDENTITY
    clean_name = _clean(name) or clean_email.split("@")[0] or DEFAULT_IDENTITY.name
    return GitIdentity(clean_name, clean_email)


class CheckoutError(RuntimeError):
    """The workspace could not be brought to the task's input commit."""


class InputFetchError(CheckoutError):
    """The input could not be FETCHED from the relay — the one checkout failure worth retrying.

    A subclass rather than a sibling, deliberately: every `except CheckoutError` already written
    stays correct, so adding this cannot quietly change how an existing call site behaves. Only the
    one place that wants the distinction has to ask for it.

    **What separates it from its parent is where the failure happened, not what it looked like.**
    A timeout, a relay at its git concurrency limit answering 503, a connection dropped mid-pack: all
    facts about THIS attempt against THIS relay at THIS moment, and another provider may well
    succeed. So the supervisor reports nothing at all, the lease lapses, and the relay's reclaim
    hands the task on — the same mechanism `PushError` uses, for the same reason.

    An ordinary `CheckoutError` — no branch on the claim, no input commit, no remote wired, a `clean`
    that cannot remove a leftover — is not that. Retrying those spends every attempt to arrive at
    `retries_exhausted`, which does not even carry the real reason (ADR 0033 issue 16a, criterion 4).
    """


class PushError(RuntimeError):
    """The result could not be committed or pushed.

    Deliberately NOT a `CheckoutError`, because the two demand opposite handling. A checkout that
    fails must fail the task — an agent run against input that never arrived produces a confidently
    wrong answer. A push that fails must **not** report anything at all: the task stays `running`,
    its lease lapses, and the relay's reaper hands it to another provider. Reporting `failed` would
    be terminal, and a terminal task is one nothing will ever retry.
    """


def _env(token: str | None = None, author: GitIdentity | None = None) -> dict[str, str]:
    """The environment every git child gets.

    **Author and committer are different people, and that is the point** (ADR 0033 D-m). The author
    is the project member whose task this is, carried on the claim payload; the committer is always
    the grid. `author=None` gives the pre-0033 behaviour exactly: `grid` on both.

    Through the environment rather than `git commit --author=`, matching the relay — whose
    `commit-tree` has no such flag — and for this side's own reason too: argv is world-readable
    through `/proc/<pid>/cmdline`, which is the same reason the token below is not passed there.

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
    author = author or DEFAULT_IDENTITY
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "HOME": "/nonexistent",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": author.name,
        "GIT_AUTHOR_EMAIL": author.email,
        "GIT_COMMITTER_NAME": DEFAULT_IDENTITY.name,
        "GIT_COMMITTER_EMAIL": DEFAULT_IDENTITY.email,
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
         timeout: float = _GIT_TIMEOUT_SECONDS,
         author: GitIdentity | None = None) -> str:
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
            capture_output=True, text=True, env=_env(token, author), timeout=timeout)
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
    """Make `workspace` a git working copy, and keep `.grid/` out of every commit made from it —
    except `.grid/agent/`, which is the project's conversation and has to travel.

    The exclude is written on every call rather than only at init, because it is what keeps the
    provider's own state out of the requesting user's repository under `git add -A`. A file that
    only got written at init would go missing the first time a workspace was restored from
    anywhere else.

    The whole of `.grid/` is excluded here, and the issue-06 carve-out is made by `commit_and_push`
    **force-adding** `.grid/agent/` instead of by a `!` negation in this file. That is not a style
    choice: a *tracked* `.gitignore` outranks `$GIT_DIR/info/exclude`, so a project whose repository
    ignores (say) `*.jsonl` or `memory/` would silently defeat a negation written here — the task
    would still report `completed`, the transcript would simply never be committed, and the loss
    would only surface when a different provider found no conversation to resume. A forced add
    overrides every ignore source, so the rule cannot be shadowed by the repository's own contents.
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
    try:
        _run(workspace, "fetch", "--quiet", url, f"+refs/heads/{branch}:refs/heads/{branch}",
             token=token, timeout=_GIT_NETWORK_TIMEOUT_SECONDS)
    except CheckoutError as exc:
        # The ONE line in this function that talks to the relay, and so the only one whose failure
        # another provider might not repeat. Re-raised as the retryable subclass — see
        # `InputFetchError`. Everything above is validation and everything below is local, and both
        # stay terminal on purpose.
        raise InputFetchError(str(exc)) from None

    # `symbolic-ref` rather than `checkout -B`: the workspace may hold a previous task's modified
    # files, and `checkout` REFUSES to move a branch when that would overwrite local changes. Here
    # discarding them is the whole intent, so point HEAD first and let `reset --hard` do the work.
    _run(workspace, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
    _run(workspace, "reset", "--quiet", "--hard", input_commit)
    # `-x` ignores `.gitignore` (a leftover build artefact is still a leftover), `-ff` reaches into
    # a nested repository a previous agent may have cloned, and `-e` spares the one directory that
    # is ours rather than the task's.
    _run(workspace, "clean", "--quiet", "-ffdx", "-e", RESERVED_DIR)


def list_files(workspace: Path, *,
               timeout: float = LS_FILES_TIMEOUT_SECONDS) -> list[str]:
    """Every file in `workspace` the project's own ignore rules keep, sorted (ADR 0032 issue 08).

    Asked of git rather than walked, because "the project's ignore rules" is precisely what
    `--exclude-standard` means: the repository's tracked `.gitignore` files plus the
    `$GIT_DIR/info/exclude` `_ensure_repo` writes — so `.grid/` is excluded here for free, by the
    same one rule rather than by a second copy of it. Hand-rolling the match would be a second,
    worse implementation of gitignore semantics, and its errors would be silent: a file wrongly
    hidden from the client looks exactly like a file the agent never created.

    Reusing `_run` matters as much as reusing the exclude. The environment it builds is what keeps a
    listing from being a code-execution seam — `core.hooksPath` pointed at nothing, `HOME` at a
    directory that does not exist, and `GIT_CONFIG_GLOBAL` at `/dev/null`. That last one also has a
    visible consequence worth stating: the provider *operator's* personal global gitignore cannot
    hide files from the user watching their own task.
    """
    # ONE budget across both invocations, not one each. This runs on the lease renewer's thread, and
    # what has to stay bounded is the whole listing — a per-call ceiling would quietly make the real
    # worst case twice the figure the lease arithmetic in `tests/test_task_lease.py` checks against.
    deadline = time.monotonic() + timeout
    tracked_and_new = _split(_run(workspace, "ls-files", "-z", "--cached", "--others",
                                  "--exclude-standard", timeout=timeout))
    # Subtracted, because `--cached` is the INDEX and the index outlives the file. An agent that
    # deletes a file leaves it staged until the terminal commit, so without this the snapshot would
    # keep showing a file that is no longer there — for the rest of the run, in the one view whose
    # entire job is to say what is there now.
    deleted = set(_split(_run(workspace, "ls-files", "-z", "--deleted",
                              timeout=max(0.0, deadline - time.monotonic()))))
    return sorted(path for path in tracked_and_new if path not in deleted)


def _split(output: str) -> list[str]:
    """git's `-z` output as paths. NUL-separated, so a path with a newline in it stays one path."""
    return [path for path in output.split("\0") if path]


def commit_and_push(workspace: Path, *, url: str, token: str, branch: str,
                    message: str, author: GitIdentity | None = None) -> str:
    """Commit whatever the agent left and push `branch`. Returns the result commit.

    `author` is the project member the claim named (ADR 0033 D-m); the committer is `grid` whatever
    it says. `None` gives the pre-0033 identity on both — what an older relay's payload produces,
    and the reason this key is free to roll out in either direction.

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
        # The project's conversation, added explicitly and by force (ADR 0032, issue 06). `-f`
        # because EVERY ignore source has to be overridden, not just the one we wrote: `.grid/` is
        # excluded wholesale by `_ensure_repo`, and a tracked `.gitignore` in the user's own
        # repository outranks that file anyway. `-A` within the pathspec so a memory file the agent
        # deleted is staged as a deletion rather than lingering forever.
        #
        # Guarded on existence because `git add` treats a pathspec matching nothing as an error, and
        # a task whose agent never started has no transcript directory to add — that outcome must
        # still commit and push, since a failed attempt is pushed too.
        if (workspace / RESERVED_DIR / TRANSCRIPT_DIR).is_dir():
            _run(workspace, "add", "-f", "-A", "--", f"{RESERVED_DIR}/{TRANSCRIPT_DIR}")
        # The ONE invocation here that writes an identity, so the author travels no further than it.
        _run(workspace, "commit", "--quiet", "--allow-empty", "-m", message, author=author)
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
