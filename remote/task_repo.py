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
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import task_worktree

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
# Where the relay pins what a MERGE TASK has to merge (ADR 0033 D-e, issue 15). Duplicated from
# grid-src's `task_repo.INTEGRATE_PREFIX` — the ref name is a wire contract, and this side both
# fetches it and refuses anything that is not one.
#
# The refspec is `+<ref>:<ref>`, an IDENTITY mapping, so the local name is not a second lockstep
# value: the relay writes this ref name into the prompt the agent reads, and that string has to be
# the one in the workspace.
INTEGRATE_PREFIX = "refs/integrate/"
# What a merge ref may contain. The relay builds these from a uuid4, so this is deliberately wider
# than that and still narrow enough to keep a leading `-`, a space, a `..` traversal and an empty id
# out of a git argv.
_INTEGRATE_REF = re.compile(re.escape(INTEGRATE_PREFIX) + r"[A-Za-z0-9][A-Za-z0-9._-]*$")
# A full object id, as the relay stores and sends it. Narrow on purpose: this value reaches a git
# argv, and unlike a ref name there is no legitimate reason for it to hold anything but hex.
_OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
# Where THIS CONVERSATION's transcript lives (ADR 0034 D-j, issue 39). Duplicated from grid-src's
# `task_repo.TRANSCRIPT_PREFIX`, and unlike `INTEGRATE_PREFIX` the name is **not** on the wire at
# all: this side builds it from `conversation_id`, and the relay builds the same string to grant it
# in `push_refs` and to un-hide it in `transfer.hideRefs`. The prefix is therefore the whole of what
# the two repositories have to agree about, and drift is silent on both sides — the fence hides a
# namespace this provider never asks about and refuses the one it does, so every task is reclaimed
# to `retries_exhausted` with both test suites green. `tests/test_task_lease.py` parses grid-src's
# assignment rather than restating it.
#
# Why the transcript stopped riding the result commit: issue 06 put it there so it would travel to
# whichever provider served next, which was right while there was one session per MEMBER. One
# session per CONVERSATION (issue 38) makes that 105 KB-2 MB of `.jsonl` per turn in the shared
# trunk, every conversation's merge carrying every other's, inside a directory a non-developer reads
# as "my project's files".
TRANSCRIPT_PREFIX = "refs/grid/agent/"
# What a transcript ref may contain. Same shape and same reason as `_INTEGRATE_REF`: the relay builds
# conversation ids from a uuid4, so this is deliberately wider than that and still narrow enough to
# keep a leading `-`, a space, a `..` traversal and an empty id out of a git argv.
_TRANSCRIPT_REF = re.compile(re.escape(TRANSCRIPT_PREFIX) + r"[A-Za-z0-9][A-Za-z0-9._-]*$")
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
# That second inequality got STRICTER in meaning without changing in value (ADR 0033 D-k, issue 18).
# `task_deadline_seconds` is now the RUN budget, starting at the claim, so these 900s are measured
# against a full fresh hour rather than against whatever an hour-long single budget had left after
# the task sat in a queue — which is the case that used to make a large first fetch fail for reasons
# nothing here could see.
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


def push_import(source: Path, *, url: str, token: str, local_ref: str, remote_ref: str) -> str:
    """Push a member's own repository to the relay's import staging ref. Returns the commit.

    The one git call in this module that runs against a repository grid did NOT create — the
    member's working clone, on the member's own machine, from `grid project import`. It goes through
    `_run` anyway, and that is worth being deliberate about rather than reaching for `subprocess`:
    `_run` is where the bearer token is put in the ENVIRONMENT instead of on the command line
    (`/proc/<pid>/cmdline` is world-readable and `/proc/<pid>/environ` is not), where
    `http.followRedirects=false` stops that token being handed to whatever a redirect names, and
    where the network ceiling that outwaits the relay's lives.

    ⚠️ It also means the member's own `~/.gitconfig` does not apply to this push, and their
    `core.hooksPath` therefore does not run. That is a side effect of reusing `_run` rather than its
    purpose, and it is the right one: a `pre-push` hook firing because somebody typed `grid project
    import` would be surprising, and the alternative — a second git invocation path with its own
    idea of how to carry a credential — is the thing this repo has one `_run` to avoid.

    The refspec is explicit and one-way. No `--force`: the relay's `receive.denyNonFastForwards` is
    on, and the staging ref was deleted when the import was opened, so a fast-forward from nothing
    is exactly what should be possible and nothing else should be.
    """
    if not local_ref or local_ref.startswith("-"):
        raise CheckoutError(f"{local_ref!r} is not a ref this command can push")
    if not remote_ref.startswith("refs/"):
        # The relay hands this back; a bare name would let git resolve it as something else
        # entirely. Defence in depth against a mangled reply, not against the relay.
        raise CheckoutError(f"the relay named {remote_ref!r}, which is not a full ref name")
    commit = _run(source, "rev-parse", "--verify", f"{local_ref}^{{commit}}").strip()
    _run(source, "push", "--quiet", url, f"{commit}:{remote_ref}",
         token=token, timeout=_GIT_NETWORK_TIMEOUT_SECONDS)
    return commit


@dataclass(frozen=True)
class Pushed:
    """What `commit_and_push` landed, and what the agent left behind unresolved.

    `unresolved` is a record rather than a separate query the caller makes for a reason this repo has
    already paid for once: a fact that has to be read at ONE moment — before `git add -A`, which
    destroys it — and a caller that forgot to ask would silently lose the guard. The same rule
    `transcript` follows on `commit_and_push`, which is required rather than defaulted for exactly
    that reason.

    Empty on every ordinary task, and on a merge the agent really did resolve.

    **`unchecked` is not the same fact as an empty `unresolved`, and collapsing the two would reopen
    the hole.** The check can fail on its own — a git blip, or `ls-files` exceeding its 5s budget on
    a large repository — and a caller handed `()` for that could not tell "this merge is clean" from
    "nobody looked". It carries git's own words when that happens, so the caller can disclose it
    rather than certify a merge nothing examined.
    """

    commit: str
    unresolved: tuple[str, ...] = ()
    unchecked: str = ""


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


# The two settings that must hold on a tree that arrived over the wire (ADR 0033 D-f), named ONCE
# because three places carry them and a hooks path spelled twice is a hooks path that drifts:
# `_run`'s `-c` flags, `_ensure_repo`'s write into the workspace's own config, and
# `task_agent._GIT_CONFIG_FLOOR`, which is what the AGENT's git gets.
#
#   * `core.symlinks=false` makes git materialize a `120000` object as a plain file holding the
#     target as text, so an object import allowed through cannot become a link into the provider's
#     config directory. Import is what makes such an object reachable at all: before it, the relay
#     wrote mode `100644` literally and had no other code path.
#   * `core.hooksPath` pointed at nothing means no hook can run, whatever a repository's own
#     `.git/config` says — and a persistent workspace's config is something a previous run could
#     have written.
#
# ⚠️ **Measured on git 2.54.0, and it bounds what any of this may claim: `-c core.symlinks=true` on
# the command line beats BOTH the environment floor and the repository's config.** So this stops the
# accidental path — an ordinary `git merge` or `git checkout` materializing what a packfile carried
# — and not an agent that has decided to make a link. Against a deliberate agent the relay's import
# validator is the only layer, which is exactly what ADR 0033 D-f says it is.
GIT_SAFETY_CONFIG: tuple[tuple[str, str], ...] = (
    ("core.symlinks", "false"),
    ("core.hooksPath", "/nonexistent/hooks"),
)


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
         author: GitIdentity | None = None,
         index: Path | None = None) -> str:
    """One git invocation inside `workspace`, or `CheckoutError` carrying git's own words.

    `GIT_SAFETY_CONFIG` is applied as `-c` on EVERY call rather than once at clone time, because a
    config written into `.git/config` is itself something a previous run could have changed. See
    that constant for what the two settings buy and — measured — what they do not.

    `index` points this one call at a THROWAWAY index (ADR 0034 D-j, issue 39), which is how the
    transcript is staged without ever entering the project's own. A `GIT_INDEX_FILE` that leaked into
    other calls would be far worse than the problem it solves, so it is a per-call argument rather
    than something `_env` knows about: the result commit's index is the one thing in this module that
    must not acquire a `.grid/` entry, since that is the whole of what this slice removes.
    """
    safety: list[str] = []
    for key, value in GIT_SAFETY_CONFIG:
        safety += ["-c", f"{key}={value}"]
    env = _env(token, author)
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    try:
        proc = subprocess.run(
            ["git", *safety, "-C", str(workspace), *args],
            capture_output=True, text=True, env=env, timeout=timeout)
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

    The exclude is written on every call rather than only at init, because it is what keeps the
    provider's own state out of the requesting user's repository under `git add -A`. A file that
    only got written at init would go missing the first time a workspace was restored from
    anywhere else.

    **The whole of `.grid/` is excluded, without exception, since ADR 0034 D-j (issue 39).** It used
    to have one: issue 06 carved `.grid/agent/` back in so the conversation would travel in the
    result commit, and that carve-out was a force-add in `commit_and_push` rather than a `!` negation
    here. The transcript now travels on `refs/grid/agent/<conversation_id>` instead, so the force-add
    is gone and this file's rule is uniform again.

    ⚠️ **The reason the carve-out was never written here still applies to `push_transcript`, which
    inherited it.** A *tracked* `.gitignore` outranks `$GIT_DIR/info/exclude`, so a project whose
    repository ignores (say) `*.jsonl` or `memory/` would silently defeat a negation written in this
    file — the task would still report `completed` and the transcript would simply never be
    published. That is why the side-ref publish stages with `add -f`, and why
    `test_the_projects_own_gitignore_cannot_suppress_the_conversation` was re-aimed rather than
    deleted.

    ⚠️ **This file has no say over paths git already TRACKS**, which is what `commit_and_push`'s
    `git rm --cached -r .grid/agent` is for: every project that ran a task under ADR 0033 has
    transcripts tracked in its history, and an exclude does nothing about those.

    `GIT_SAFETY_CONFIG` is written into the workspace's own config on every call too, and for a
    narrower reason than ADR 0033 D-f gives. D-f says the environment "can be overridden by an agent
    that decides to" — measured on git 2.54.0, that is true of the repository's config in exactly
    the same way and to exactly the same degree (`-c` beats both), so this is not a second lock on
    the same door. What it covers is a git that never saw `child_env` at all: an operator shelling
    into a workspace to look at a failed task, a tool that re-execs without the environment. The
    config travels with the directory.
    """
    if not (workspace / ".git").exists():
        _run(workspace, "init", "--quiet", f"--initial-branch={DEFAULT_BRANCH}", ".")
    exclude = workspace / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text(f"# written by grid — see ADR 0032\n/{RESERVED_DIR}/\n")
    for key, value in GIT_SAFETY_CONFIG:
        _run(workspace, "config", "--local", key, value)


def materialize(workspace: Path, *, url: str, token: str, branch: str,
                input_commit: str, merge_ref: str = "",
                transcript_ref: str = "", transcript_commit: str = "",
                reset_agent_state: bool = False) -> None:
    """Bring `workspace` to exactly `input_commit` on `branch`. Raises `CheckoutError` on any failure.

    Raising rather than returning a status is deliberate: the ONLY safe response to input that did
    not arrive is to not start the agent. An agent run against missing input produces a confidently
    wrong result with nothing anywhere indicating why — the precise failure ADR 0032 D-b exists to
    prevent.

    Fetched from the relay's smart-HTTP front rather than from a bundle (issue 05): the same
    repository the result is pushed back to, so the provider negotiates what it is missing instead
    of downloading the project's whole history per task.

    `merge_ref` is a MERGE TASK's second ref (ADR 0033 D-e, issue 15): the trunk, pinned by the relay
    at integrate and published as `refs/integrate/<task_id>`. Fetched here, before the agent is
    spawned, because `child_env` hands the agent no grid credential at all and that must stay true —
    the agent merges refs that are already local.

    It is fetched onto the **identical local ref name**, which is the whole reason the local name is
    not a cross-repo lockstep value: the relay names this ref in the prompt it writes, so an identity
    refspec means the two repositories cannot disagree about it.

    *Absent ⇒ nothing extra is fetched*, which is exactly the pre-integration behaviour: an old
    relay's claim runs as an ordinary task rather than failing in a new way.
    """
    if not branch or not input_commit:
        raise CheckoutError("the claim gave no branch or input commit to check out")
    if not url:
        raise CheckoutError("the claim gave no git remote to fetch the input from")
    # BEFORE `_ensure_repo`, so a malformed value costs nothing and touches nothing. Validated at
    # all because it arrives off the wire and ends up in a git argv: `_run` never goes through a
    # shell, so the exposure is option confusion — `--upload-pack=…` read as a flag — rather than
    # command injection, which is exactly the class this refuses cheaply.
    #
    # TERMINAL (a plain `CheckoutError`), never the retryable subclass: no provider can fix a
    # malformed claim, and retrying it spends every attempt to reach `retries_exhausted`.
    if merge_ref and not _INTEGRATE_REF.fullmatch(merge_ref):
        raise CheckoutError(
            f"the claim's merge_ref {merge_ref!r} is not a {INTEGRATE_PREFIX}<id> ref, so this "
            f"provider will not hand it to git")
    # The same rule for the transcript ref, and for the same reason: it reaches a git argv. This one
    # is built HERE from `conversation_id` rather than sent by the relay, so a malformed value means
    # a malformed conversation id — still terminal, because no provider can fix it and retrying
    # spends every attempt reaching `retries_exhausted`.
    if transcript_ref and not _TRANSCRIPT_REF.fullmatch(transcript_ref):
        raise CheckoutError(
            f"the transcript ref {transcript_ref!r} is not a {TRANSCRIPT_PREFIX}<id> ref, so this "
            f"provider will not hand it to git")
    # A pin with no ref to find it in is a claim this provider cannot act on, and it is the shape a
    # caller bug takes rather than an old relay's: an old relay sends neither.
    if transcript_commit and not _OID.fullmatch(transcript_commit):
        raise CheckoutError(
            f"the claim's transcript_commit {transcript_commit!r} is not an object id")

    # THE OBJECT STORE, and the fetch goes into it rather than into the workspace (ADR 0034 D-c,
    # issue 50). The order is forced: a worktree cannot be cut at a commit the store does not hold
    # yet, so the store is created, then filled, and only then is this conversation's working tree
    # cut from it. Everything below `ensure_worktree` runs in the workspace exactly as before.
    store = task_worktree.store_for(workspace)
    try:
        # INSIDE the retryable block, beside the fetch, and that placement is a decision. This is
        # provider-LOCAL git housekeeping — `init --bare`, `config`, `worktree prune` — so its
        # failures are about this provider's disk and not about the task: an ENOSPC here is exactly
        # the "about this attempt rather than about the task" case the fetch's own comment below
        # describes, and reported terminally it burns the turn on one machine's full disk with
        # nothing to retry it. Found in review.
        task_worktree.ensure_store(store)
        # A retry on the same provider already has this branch checked out in its linked worktree.
        # Fetch normally refuses to update such a ref even when the oid is unchanged. We are about
        # to hard-reset that worktree to the relay-pinned input, so this is exactly the plumbing use
        # ``--update-head-ok`` exists for; without it cross-machine recovery works while same-node
        # recovery fails before the agent starts.
        _run(store, "fetch", "--quiet", "--update-head-ok", url,
             f"+refs/heads/{branch}:refs/heads/{branch}",
             token=token, timeout=_GIT_NETWORK_TIMEOUT_SECONDS)
        if merge_ref:
            # Its own invocation rather than a second refspec on the one above: a relay that has
            # collected this ref early answers with a failure naming the ref, and combining them
            # would lose the input fetch's success to the merge ref's failure.
            _run(store, "fetch", "--quiet", url, f"+{merge_ref}:{merge_ref}",
                 token=token, timeout=_GIT_NETWORK_TIMEOUT_SECONDS)
        if transcript_commit:
            # THE CONVERSATION (ADR 0034 D-j, issue 39), and only when the relay pinned one. An
            # unpinned turn fetches nothing: either the conversation has no transcript yet, or the
            # relay predates this key — both mean "start fresh", which is the safe degrade.
            #
            # Gated on the PIN rather than on the ref, because the pin is what says a transcript
            # exists. `transcript_ref` is derived from the conversation id and is always non-empty.
            #
            # ⚠️ Fetched BY NAME even though the pin is what gets checked out. A bare oid is
            # unfetchable — `uploadpack.allowAnySHA1InWant` is off — and the pin is reachable inside
            # what the name brings back only because this ref is fast-forward only. That is the
            # dependency that makes the fast-forward rule load-bearing rather than tidy.
            #
            # Its own invocation, for the merge ref's reason: this failure has to be
            # distinguishable, because "the relay says a transcript exists and I could not get it"
            # must NOT become a silent fresh session.
            _run(store, "fetch", "--quiet", url, f"+{transcript_ref}:{transcript_ref}",
                 token=token, timeout=_GIT_NETWORK_TIMEOUT_SECONDS)

        # This conversation's working tree, cut from the store the fetch just filled. Cheap on every
        # turn after the first — the worktree is already there and this is a probe.
        #
        # Retryable for `ensure_store`'s reason, and more so: `worktree add` is a full checkout of
        # the whole repository, so it is far more exposed to a disk filling up than the `git init`
        # it replaced.
        task_worktree.ensure_worktree(store, workspace, input_commit)
    except CheckoutError as exc:
        # The lines that talk to the relay, and — since issue 50 — the provider-local git
        # housekeeping either side of them: everything whose failure is about THIS ATTEMPT rather
        # than about the task, and which another provider might therefore not repeat. Re-raised as
        # the retryable subclass; see `InputFetchError`. The VALIDATION above stays terminal, because
        # a malformed claim finds the same answer on every provider.
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

    # A pinned task is allowed to see exactly the pinned agent state, not a failed attempt's newer
    # local files. Codex Goals need the same reset even on their first turn (whose pin is empty),
    # because an automatic retry of that first turn can land back on the same provider. Keep the
    # explicit flag narrow: an old Claude relay sends no pin and historically relies on `.grid/`
    # surviving materialization, so changing the default would destroy that conversation.
    if transcript_commit or reset_agent_state:
        agent_state = workspace / RESERVED_DIR / TRANSCRIPT_DIR
        try:
            if agent_state.is_symlink() or (agent_state.exists() and not agent_state.is_dir()):
                agent_state.unlink()
            elif agent_state.is_dir():
                shutil.rmtree(agent_state)
        except OSError as exc:
            raise InputFetchError(f"could not reset the task's pinned agent state: {exc}") from None

    if transcript_commit:
        # The conversation, put where Claude Code will look for it (ADR 0034 D-j, issue 39). AFTER
        # the reset and the clean, both of which spare `.grid/` but neither of which would put a
        # transcript there.
        #
        # ⚠️ **`restore --worktree`, never `checkout <oid> -- <path>`.** Measured on git 2.54.0:
        # `checkout` writes the INDEX as well, so those paths would become staged entries in the
        # project's index and `commit_and_push` would commit them straight back into the trunk —
        # reintroducing, by a different route, the exact thing this slice removes. `restore` with
        # `--worktree` alone touches no index at all.
        #
        # The PINNED oid, not the ref's tip. On a retry the tip is what the failed attempt pushed and
        # the pin is what preceded it, and resuming the failed attempt's own confusion on every retry
        # is what ADR 0034 D-j's latch exists to prevent.
        _run(workspace, "restore", "--source", transcript_commit, "--worktree", "--",
             f"{RESERVED_DIR}/{TRANSCRIPT_DIR}")


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
                    message: str, author: GitIdentity | None = None) -> Pushed:
    """Commit whatever the agent left and push `branch`. Returns the result commit and what the
    agent left unresolved.

    ⚠️ **The conversation is NOT in what this pushes** (ADR 0034 D-j, issue 39). It used to be, and
    the `transcript` parameter that carried it is gone rather than defaulted — a defaulted one would
    be a caller that silently kept the old behaviour. The transcript goes to
    `refs/grid/agent/<conversation_id>` via `push_transcript`, and the one thing this function still
    does about it is `git rm --cached` the copies a pre-0034 project already has TRACKED, without
    which they would be re-staged by `add -A` on every turn forever.

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

    # No transcript containment check here any more, and it is gone rather than relaxed (ADR 0034
    # D-j, issue 39). It existed because a caller-supplied directory became a `git add` PATHSPEC, so
    # a path outside the worktree had to be refused before it reached argv. Nothing here takes that
    # directory now: the only `.grid/agent` pathspec below is a module constant, and where the
    # transcript actually goes is `push_transcript`'s business. A check with nothing left to check
    # would read as protection that is no longer protecting anything.

    # BEFORE `add -A`, which is the only moment this fact exists (ADR 0033 D-e, issue 15).
    #
    # Measured on git 2.54.0: during a conflicted merge `git ls-files --unmerged` lists three stage
    # entries per file, and `git add -A` clears them to ZERO — staging the conflict markers as if
    # they were a resolution. `git commit` then succeeds where git itself would have refused, and
    # what comes out is structurally a perfectly good merge commit. So the relay's ancestry check
    # passes, the member's WIP branch fast-forwards, and a tree full of `<<<<<<<` becomes the base
    # of their next task.
    #
    # The relay cannot make this check — the index is never pushed — so the provider is the only
    # party that can, and only here.
    #
    # A check that could not RUN is carried as its own fact rather than as an empty result: the
    # caller discloses it and lets the task complete, which is right — turning a git availability
    # blip into a lost push would be worse than not checking — but it must not be able to read as
    # "this merge is clean".
    unresolved: tuple[str, ...] = ()
    unchecked = ""
    try:
        unresolved = _unresolved_paths(workspace)
    except _CouldNotCheck as exc:
        unchecked = str(exc)

    try:
        # ⚠️ **BEFORE `add -A`, and this line is the whole of the migration** (ADR 0034 D-j, issue
        # 39). The transcript now travels on `refs/grid/agent/<conversation_id>` and the exclude
        # `_ensure_repo` writes keeps `.grid/` out of every commit — but `$GIT_DIR/info/exclude` has
        # **no say over files git already TRACKS**, and every project that has run a task under
        # ADR 0033 has `.grid/agent/**` tracked in `main`. Without this, `add -A` below goes on
        # staging every modification to them forever: the trunk quietly stays fat, every member
        # keeps receiving every other member's conversation, and *every test on a fresh project
        # passes*. That asymmetry is why the flip test seeds a tracked transcript.
        #
        # `--cached`, so the files stay on disk: they are this conversation's live transcript, which
        # the agent is still appending to and which `push_transcript` publishes moments later.
        # `--ignore-unmatch`, so a project with nothing tracked there is a no-op rather than an
        # error — `git rm` treats an unmatched pathspec as a failure, exactly as `git add` does, and
        # the overwhelmingly common case after the first turn is that there is nothing to remove.
        # Measured on git 2.54.0: exit 0 with no output when nothing matches, and when the directory
        # does not exist at all.
        _run(workspace, "rm", "--cached", "-r", "--quiet", "--ignore-unmatch", "--",
             f"{RESERVED_DIR}/{TRANSCRIPT_DIR}")
        _run(workspace, "add", "-A")
        # No force-add of the transcript any more, and its absence is the feature. Issue 06 added
        # one so the conversation would ride the result commit to the next provider; ADR 0034 D-j
        # moves it to a side ref, so a force-add here would put back exactly what the slice removes.
        # `add -A` above does not re-stage it: `.grid/` is excluded and, after the `rm --cached`,
        # untracked.
        #
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
    return Pushed(commit=commit, unresolved=unresolved, unchecked=unchecked)


def transcript_ref(conversation_id: str) -> str:
    """Where this conversation's transcript lives (ADR 0034 D-j, issue 39).

    Built here rather than taken off the claim, which is why `TRANSCRIPT_PREFIX` is the lockstep
    value and the ref name is not: a name a provider derives from a duplicated constant is one fewer
    field a proxy can mangle, and the relay derives the identical string to grant it in `push_refs`.

    An empty id answers an empty string rather than raising, and the difference from grid-src's
    `transcript_ref` — which refuses one — is deliberate. On the relay an empty id would silently
    widen a fence grant to a namespace shared by everybody. Here it can only mean "this claim named
    no conversation", which `run_task` has already refused terminally before any of this runs; an
    exception thrown from a path that cannot be reached would be a second, worse answer to a question
    already answered.
    """
    return f"{TRANSCRIPT_PREFIX}{conversation_id}" if conversation_id else ""


def push_transcript(workspace: Path, *, url: str, token: str, ref: str) -> str | None:
    """Publish this conversation's transcript to its side ref (ADR 0034 D-j, issue 39).

    Answers the commit pushed, or `None` when the agent produced no transcript at all — a turn whose
    agent never started, which still has to settle. Raises `PushError` on any failure, exactly like
    `commit_and_push`: a conversation that evaporates in silence is the outcome D-j names as
    unacceptable, so "best effort" is not on offer here.

    **Built through a THROWAWAY INDEX, and every part of that is load-bearing:**

      * `GIT_INDEX_FILE` at a temp path means these paths never enter the project's own index, so the
        result commit cannot acquire a `.grid/` entry — which is precisely what this slice removes.
        Measured on git 2.54.0: after this sequence `git status --porcelain` in the workspace is
        empty.
      * `add -f` because EVERY ignore source has to be overridden. `.grid/` is excluded wholesale by
        `_ensure_repo`, and a *tracked* `.gitignore` in the user's own repository outranks that file
        anyway — a project that ignores `*.jsonl` or `memory/` would otherwise publish an empty
        transcript while reporting `completed`. That is issue 06's silent-loss failure, reached
        through the new path, and `test_the_projects_own_gitignore_cannot_suppress_the_conversation`
        is the test that would catch it.
      * `commit-tree` with the FETCHED TIP as parent, never with the pinned oid. The pin decides what
        content was materialized; the parent decides that the push fast-forwards. On a retry those
        differ — the content is older than the tip — and using the pin as parent would build a
        sibling that `receive.denyNonFastForwards` refuses, so a retry could never publish anything.

    The tree covers `.grid/agent` as a whole rather than one member's subdirectory. The workspace is
    already per conversation (ADR 0034 D-c), so there is only one conversation's transcript in it,
    and narrowing further would be a second copy of the member-key rule for no gain.
    """
    if not ref.startswith("refs/"):
        # A full ref name, for `push_import`'s reason: the relay authorizes an exact ref, and a
        # short name would be resolved by git into something else entirely.
        raise PushError(f"a transcript ref must be a full ref name, got {ref!r}")
    if not url:
        raise PushError("no git remote to push the transcript to")

    transcript = workspace / RESERVED_DIR / TRANSCRIPT_DIR

    # ⚠️ **The parent comes from the RELAY, fetched HERE, and this function must not borrow it from
    # anywhere else.** It used to read whatever `materialize` had left on the local ref — and that
    # fetch is gated on the PIN, so a conversation's FIRST turn (no pin, by definition) fetched
    # nothing. `git push <oid>:<ref>` creates no local ref either, so after a successful publish
    # this workspace held no record of it. A first turn that needed a second attempt — its result
    # push failed, the reaper reclaimed it, the workspace survived `clean -ffdx -e .grid` — then
    # built a SECOND root commit and was refused as a non-fast-forward, identically, on every
    # remaining attempt, until `retries_exhausted`. The agent's work and its transcript were both
    # fine throughout. Reproduced on git 2.54.0; `test_publishing_a_transcript_twice_from_one_
    # workspace_fast_forwards` is the regression test.
    #
    # Best-effort, and the swallow is bounded rather than lazy: a ref that does not exist yet is a
    # conversation's first publish, which is the ordinary case. A fetch that fails for a REAL reason
    # (the network, the relay) leaves `parent` empty, so the push below builds a root commit and is
    # refused LOUDLY as a non-fast-forward — a `PushError`, no terminal report, and the reaper
    # retries the whole turn. There is no arrangement here that loses a conversation quietly.
    if url:
        try:
            _run(workspace, "fetch", "--quiet", url, f"+{ref}:{ref}",
                 token=token, timeout=_GIT_NETWORK_TIMEOUT_SECONDS)
        except CheckoutError:
            pass

    parent = ""
    try:
        parent = _run(workspace, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip()
    except CheckoutError:
        # `rev-parse --verify --quiet` exits non-zero for a ref that is not there, which is not an
        # error here: it is a first turn.
        parent = ""

    try:
        with tempfile.TemporaryDirectory(prefix="grid-transcript-") as scratch:
            index = Path(scratch) / "index"
            # Guarded on the directory only because `git add` calls an unmatched pathspec an ERROR.
            # Whether there is anything to publish is decided from the TREE below, not from here.
            if transcript.is_dir():
                _run(workspace, "add", "-f", "-A", "--", f"{RESERVED_DIR}/{TRANSCRIPT_DIR}",
                     index=index)
            tree = _run(workspace, "write-tree", index=index).strip()
            # ⚠️ **"Is there anything to publish" is a question about the TREE, and asking the
            # DIRECTORY instead was a real bug with a permanent consequence.** `link_transcript`
            # creates `.grid/agent/<member_key>/` BEFORE the agent starts, so a turn whose agent
            # never opened a session leaves that directory existing and EMPTY — `is_dir()` is true,
            # nothing stages, and `write-tree` answers the empty tree (measured, git 2.54.0). An
            # empty commit was then published as the conversation's first state, the next turn
            # pinned it, and `git restore --source=<it> -- .grid/agent` failed with
            # `pathspec '.grid/agent' did not match any file(s) known to git`. That call is outside
            # `materialize`'s retryable arm, so the turn failed TERMINALLY — and so did every turn
            # after it, because the pin of a conversation whose ref never moves never changes. The
            # conversation was permanently unusable, and the message named a pathspec nobody wrote.
            #
            # Read with `ls-tree` rather than compared against a hardcoded empty-tree oid: that oid
            # differs between SHA-1 and SHA-256 repositories, and this has no business knowing which
            # it is in.
            empty = not _run(workspace, "ls-tree", "--name-only", tree).strip()
            if empty and not parent:
                # Nothing to publish and nothing published before. An ordinary outcome — the turn
                # still settles — and an empty commit would spend a round trip to say so while
                # poisoning every later turn of this conversation.
                return None
            if empty:
                # ⚠️ **A history that exists plus nothing to publish is a REFUSAL.**
                # `commit-tree <empty> -p <parent>` is a valid FAST-FORWARD child, so this would
                # land cleanly and report success while replacing the whole conversation with an
                # empty commit. "Nothing to record" and "everything recorded is gone" are different
                # observations and only the first is ordinary.
                raise PushError(
                    f"this conversation has a published transcript at {ref} but there is nothing "
                    f"under {transcript} to publish now; refusing, because the commit that would be "
                    f"pushed replaces the conversation with an empty tree")
            args = ["commit-tree", tree, "-m", f"conversation transcript for {ref}"]
            if parent:
                args += ["-p", parent]
            commit = _run(workspace, *args).strip()
    except CheckoutError as exc:
        raise PushError(f"could not build the transcript commit: {exc}") from None

    if commit == parent:
        # The agent changed nothing the transcript records. Nothing to push, and saying so beats a
        # no-op round trip; the ref already names this commit.
        return commit

    try:
        _run(workspace, "push", "--quiet", url, f"{commit}:{ref}",
             token=token, timeout=_GIT_NETWORK_TIMEOUT_SECONDS)
    except CheckoutError as exc:
        raise PushError(f"could not push the conversation's transcript to {ref}: {exc}") from None
    return commit


def _unresolved_paths(workspace: Path) -> tuple[str, ...]:
    """Paths git still holds unmerged — conflicts the agent never resolved.

    **The INDEX decides, not the file content, and an earlier version of this got that wrong.** It
    looked for git's `<<<<<<<` markers in the worktree and treated an unmerged index as merely "not
    `git add`ed". A `modify/delete` conflict destroys that reasoning: one side deletes a file, the
    other edits it, and git leaves **no markers at all** — measured on 2.54.0, it writes the
    surviving side's content verbatim and reports the conflict only through the index and its exit
    status. An agent that did nothing then produced a structurally perfect two-parent merge commit
    that passed the relay's ancestry check, and somebody's deletion was discarded in silence: the
    exact failure ADR 0033 D-e exists to prevent, reached through a conflict type the check could not
    see. Every non-textual conflict class was invisible the same way.

    So the rule is git's own: `git commit` refuses while paths are unmerged, and this module's
    `git add -A` is precisely what overrides that refusal. The merge prompt tells the agent to
    `git add` or `git rm` every conflicted path, so a path still unmerged when it stops is a path
    it did not decide about — whatever the bytes in the file happen to look like.

    The accepted cost, stated so it is not rediscovered as a bug: an agent that edits a conflicted
    file and stages nothing has its task failed, one run is wasted, and its work is still pushed for
    the member to read. That is the cheap side of the trade against a deletion silently discarded,
    permanently, with every other signal reading healthy.

    `ls-files --unmerged` reports one line per STAGE (base, ours, theirs — and a modify/delete has
    only two), so a path appears more than once; they are collapsed here.

    Never raises. This runs on a path whose job is to preserve the agent's work, and a result that
    could not be committed because a diagnostic failed would be strictly worse than one reported
    unchallenged. The caller is told the check did not run rather than being handed a clean answer.
    """
    try:
        listed = _run(workspace, "ls-files", "--unmerged", "-z",
                      timeout=LS_FILES_TIMEOUT_SECONDS)
    except CheckoutError as exc:
        # NOT `()`, which would be indistinguishable from "checked, found nothing" — the caller has
        # to be able to tell a clean merge from a check that never ran, or a git blip silently
        # reopens the hole this function exists to close.
        raise _CouldNotCheck(str(exc)) from None
    # `<mode> <sha> <stage>\t<path>` — the path is everything after the first tab, so a path
    # containing a space survives, and `-z` keeps one with a newline in it whole.
    return tuple(sorted({entry.split("\t", 1)[1] for entry in listed.split("\0") if "\t" in entry}))


class _CouldNotCheck(Exception):
    """`ls-files` could not answer, so this result is neither clean nor known-bad."""


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


def checkout_result(dest: Path, *, url: str, token: str, branch: str, commit: str = "",
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
    if not branch:
        raise CheckoutError("this task has no branch to fetch")

    _ensure_repo(dest)
    # Written before the checkout, so a fetch interrupted halfway still leaves a directory the next
    # run recognizes as its own instead of one it refuses to touch.
    (dest / ".git" / FETCH_MARKER).write_text(f"{project_id}\n", encoding="utf-8")
    _run(dest, "fetch", "--quiet", url, f"+refs/heads/{branch}:refs/heads/{branch}",
         token=token, timeout=_GIT_NETWORK_TIMEOUT_SECONDS)
    _run(dest, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
    # The PINNED commit whenever the task reported one, and the branch tip only when it did not.
    # Never the tip as a shortcut for the pinned case: the row's `result_commit` is what that task
    # actually produced, and a branch can have moved since — a retry pushes the same ref.
    #
    # The tip arm exists for a task that ended with no result at all: a cancelled one, whose agent
    # was killed before `commit_and_push` ran (ADR 0033 D-l, issue 19b). Its branch still holds the
    # task's INPUT, and `grid task cancel` promises exactly that branch — a promise it has to make
    # before the outcome is known, because the agent does not die until the next lease beat. The
    # caller is responsible for saying which of the two arrived; see `cli/remote_task._task_fetch`.
    _run(dest, "reset", "--quiet", commit or f"refs/heads/{branch}")
    _run(dest, "checkout", "--quiet", "--", ".")
