"""What a task's agent may reach on the provider, and how that travels (ADR 0033 D-n, issue 23).

Separate from `remote/task_agent.py` — that module answers "run *what*, *where*", this one answers
"and confined how". They change for different reasons: the argv and the workspace move when this
feature moves, while everything here moves when the **vendor's** sandbox does. Every key below is
Claude Code's own, and the measurements that chose them are recorded in `tests/test_task_sandbox.py`
rather than repeated at each site.

The policy is delivered per invocation, as `--settings <json>`, for a measured reason: `--settings`
outranks the repository's own `.claude/settings.json`, and several of these keys are documented by
the vendor as *"Only honored from user, managed/policy, or CLI (`--settings`) settings — project
settings are ignored"*. So a workspace that arrived over the wire cannot weaken it even before issue
22's `--setting-sources user` stops those settings loading at all.

**Two layers, because the agent has two ways to read a file.** The sandbox (seatbelt on macOS,
bubblewrap on Linux) confines the commands the *model* runs — Bash. It does **not** confine the
Claude Code process, which runs the `Read` tool itself; that is governed by the permission system.
Measured: with `bypassPermissions` and this entire policy in force, the agent read a file outside the
workspace without difficulty. So the deny list is written twice, once for each layer, and
`task_agent.permission_mode` refuses the mode that switches the second one off.

**Nothing here is a lockstep value.** The relay is not involved: a provider decides what its own
agent may touch, and no other repository has to agree.
"""
from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

from shared import paths

# For `store_for` alone — the layout, spelled in one place (ADR 0034 D-c, issue 50). Safe as a
# top-level import where `task_agent` is not: `task_worktree` reaches `task_repo` and `task_agent`
# lazily, from inside its functions, precisely so importing it closes no ring.
from . import task_worktree

# Named rather than imported from `task_agent`, which imports THIS module — the variable an operator
# actually sets when a workspace path needs to move.
WORKSPACE_ROOT_HINT = "GRID_TASK_ROOT"

# The knob that turns confinement off, for an operator who must (an unsupported platform, a
# provider that cannot install bubblewrap). Off is a deliberate, visible act — never a fallback this
# module takes by itself, because a control that quietly stops applying is worse than one that was
# never configured.
SANDBOX_ENV = "GRID_TASK_SANDBOX"

# Where a task's own commands may reach on the network. **Not optional**: measured, a policy with no
# `network` section denies every host, so `pip install` and `git fetch` fail inside a task. The
# default is the registries an ordinary build needs; an operator with a private registry adds it.
ALLOWED_DOMAINS_ENV = "GRID_TASK_ALLOWED_DOMAINS"
DEFAULT_ALLOWED_DOMAINS = (
    "registry.npmjs.org", "registry.yarnpkg.com",
    "pypi.org", "files.pythonhosted.org",
    "crates.io", "static.crates.io", "index.crates.io",
    "proxy.golang.org", "sum.golang.org",
    "github.com", "codeload.github.com", "objects.githubusercontent.com",
    "raw.githubusercontent.com", "api.github.com",
)

# Credential stores a coding task has no business reading. `~/.grid` is this product's own — it holds
# the provider's grid access token and its vendor API keys — and it is first because it is the one
# nobody outside this repository would think to name.
_CREDENTIAL_DIRS = (".grid", ".ssh", ".aws", ".gnupg", ".config/gcloud", ".kube", ".docker")
_CREDENTIAL_FILES = (".netrc", ".npmrc", ".pypirc", ".git-credentials")

# Where package managers keep their caches, re-allowed READ-ONLY inside the denied home, so an
# already-populated provider cache is reused instead of re-downloaded. `allowRead` is documented to
# take precedence over `denyRead`; it does **not** override a `credentials.files` deny entry, which
# was measured rather than assumed.
#
# ⚠️ **Read-only, and the WRITE side is deliberately somewhere else — see `cache_dir`.** These are
# the provider's own caches, shared by every task and every MEMBER on this box. Making them writable
# is a one-line change and it opens a cross-member contamination channel: one member's task could
# plant a package in `~/.npm` that another member's task then installs, on a provider neither of
# them chose. ADR 0033 D-g exists to keep members out of each other's state; a shared writable cache
# would put it back at a layer nobody looks at.
_BUILD_CACHE_DIRS = (
    ".cache", ".npm", ".yarn", ".pnpm-store", ".cargo", ".rustup", ".gradle", ".m2", ".bun",
    ".local/share/uv", ".local/share/pnpm", ".nvm", ".pyenv", ".rbenv",
)

# The one writable scratch tree a task gets outside its workspace, and the name is a sibling of
# `workspace` rather than a directory inside it — **`task_repo.commit_and_push` runs `git add -A`**,
# so a cache under the workspace would be committed into the team's history, hundreds of megabytes
# of `node_modules` at a time, on the first task that installed anything.
#
# It exists because read-only caches do not degrade, they FAIL. MEASURED on the dev VM against
# Claude Code 2.1.226: with only the read-only grant above, a plain `npm install` dies with
# `EROFS: read-only file system, open '/root/.npm/_cacache/tmp/…'` and `node` then reports
# `MODULE_NOT_FOUND` — npm treats an unwritable cache as fatal. `pip` survives the same policy only
# because it degrades to no-cache, which is why "pip install works" was true and misleading: it was
# true of the single ecosystem that happens to forgive this.
#
# Per (project, member), like the workspace it sits beside, so it is warm on the second task and
# still isolated between members.
CACHE_DIR_NAME = "cache"


def cache_dir(workspace: Path) -> Path:
    """The writable cache tree beside `workspace`, for the task's package managers.

    Derived in ONE place because two consumers must agree exactly: this module grants it in
    `allowWrite`, and `task_agent.child_env` points `npm_config_cache` and friends at it. Two
    spellings of the same path would grant one directory and use another — and the symptom would be
    the `EROFS` this function exists to remove, with the policy looking correct.
    """
    return workspace.parent / CACHE_DIR_NAME

# NOT set, and the omission is a decision rather than an oversight: `sandbox.enableWeakerNetworkIsolation`.
#
# On macOS the sandbox blocks access to `com.apple.trustd.agent`, which is what evaluates TLS
# certificate trust — so any client using the system trust store fails to verify a certificate.
# MEASURED: inside a task, `pip install six` fails with
# `SSLError(SSLCertVerificationError('OSStatus -26276'))` even with `pypi.org` on the egress
# allowlist, and succeeds the moment this flag is true. `curl` is unaffected, which is what makes it
# confusing to meet in the wild — the network is reachable, only certificate verification is not.
#
# Left off because the fleet runs **Linux**, where the flag does nothing at all, and the vendor
# describes it as reducing security: "opens a potential data exfiltration vector through the trustd
# service". Turning it on by default would weaken every macOS provider to fix a platform the design
# does not deploy to. The consequence is stated in `docs/cli.md` instead: a macOS provider cannot
# install dependencies inside a task. Whether Linux/bubblewrap has an equivalent problem is exactly
# what the still-open acceptance criterion (re-measure on the fleet) has to answer.

# Vendor credentials that reach the agent PROCESS through `task_agent`'s allowlist — an API-key
# provider authenticates with them — and must not reach the commands the model runs. `deny` unsets
# the variable for sandboxed commands; the process keeps it.
_DENIED_ENV_NAMES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


# macOS only, and MEASURED rather than reasoned. The seatbelt profile is handed to every sandboxed
# command as a single argv element, and it grows with the workspace path: a 120-character workspace
# ran `echo` fine, while 160 and 186 produced profiles of 1.5MB and 1MB and every Bash command died
# with `E2BIG` — "the command line plus environment exceed the OS exec argument limit". The task then
# fails from the inside, and what the agent reports is about exec limits rather than about the
# provider's configuration, so nobody reads it as "the workspace root is too deep".
#
# A WARNING and not a refusal, deliberately. The relationship is not a clean threshold — 160 produced
# a LARGER profile than 186 — so the length is a symptom rather than the cause, and a hard limit
# built on a proxy this shaky would refuse working providers on a rule that cannot be defended.
# What an operator needs here is the clue, printed where they are already looking when tasks fail.
#
# ⚠️ **The budget is the OPERATOR'S ROOT, not the whole path, and that is the ADR 0034 D-c change.**
# The measured figure has always been about the whole path — but the whole path is mostly ours, and
# telling an operator to shorten a root they did not choose is not advice. The arithmetic:
#
#     grid-owned suffix, D-g   /projects/<uuid-36>/<member_key-32>/workspace          =  89
#     grid-owned suffix, D-c   + /<conversation_id-36>                                = 126
#     old whole-path threshold                                                          120
#     the operator's budget, unchanged                             120 - 89           =  31
#
# So the default `/var/grid` (9 characters) sat 22 inside the threshold before and sits 22 inside it
# now, while the absolute path went 98 -> 135. Keeping the whole-path check would have fired on every
# macOS provider running a stock layout, on every task — and a warning that always fires is one
# nobody reads, which costs exactly the operator this exists for.
#
# ⚠️ **Disclosed rather than glossed: the residual margin against the real failure shrank by 37
# characters even though the operator's budget did not.** The measured `E2BIG` break was near 160 and
# the stock path is now 135. That is uncomfortable, and it is not fixed by moving a threshold —
# nothing here can make the profile smaller. The honest reading is that a macOS provider on a deep
# root is closer to the wall than it was; the fleet is Linux, where the profile is not passed this
# way at all, and re-measuring seatbelt against the new depth belongs with issue 50, which is what
# decides the layout underneath this path.
#
# A WARNING and not a refusal, deliberately. The relationship is not a clean threshold — 160 produced
# a LARGER profile than 186 — so the length is a symptom rather than the cause, and a hard limit
# built on a proxy this shaky would refuse working providers on a rule that cannot be defended.
# What an operator needs here is the clue, printed where they are already looking when tasks fail.
#
# Not applied off macOS: bubblewrap does not pass a profile this way, and D-n already requires the
# Linux backend to be re-measured on the fleet rather than assumed to behave like seatbelt.
WORKSPACE_ROOT_WARNING_CHARS = 31

# How many characters of the workspace path this repo owns rather than the operator — `/projects`,
# the three wire-supplied segments with their separators, and `/workspace`. Derived from the path in
# hand instead of restated as a number, because the number moved once already (issue 38) and a
# hand-carried copy is what would go stale next time a level is added.
_GRID_OWNED_LEVELS = 5


def enabled() -> bool:
    """Whether this provider confines its agents. On unless an operator turned it off."""
    return (os.getenv(SANDBOX_ENV) or "").strip().lower() not in ("0", "false", "no", "off")


def allowed_domains() -> tuple[str, ...]:
    """The hosts a task's own commands may reach.

    The operator's list REPLACES the default rather than extending it, so a provider that must be
    strict can be, and one that needs an internal registry names the whole set it wants.

    An empty value is a deliberate "no egress at all", and that is **measured** rather than assumed:
    an empty `allowedDomains` inside a PRESENT `network` section denied `curl` with the same
    `CONNECT tunnel failed, response 403` as omitting the section entirely. Worth measuring because
    the two are different configurations, and "an empty allowlist means unrestricted" is a common
    enough library default that the docs should not have promised the opposite for free.
    """
    configured = os.getenv(ALLOWED_DOMAINS_ENV)
    if configured is None:
        return DEFAULT_ALLOWED_DOMAINS
    return tuple(configured.replace(",", " ").split())


def home_directory() -> Path:
    """The operator's home directory, or a refusal — never a plausible-looking wrong answer.

    Everything this module protects is expressed relative to `$HOME`: the `denyRead` root, and every
    `credentials.files` entry under it. So the two ways `Path.home()` can misbehave both matter, and
    the second is the dangerous one.

    **Unresolvable** — no `HOME` and no passwd entry for the UID — raises `RuntimeError`, which is a
    real container failure mode (`shared/launch/claude_install.install_locations` guards the same
    case for the same reason). Loud either way; this only makes it legible.

    **Relative** — `HOME=.` — does NOT raise. `Path.home()` hands back `.`, and `_resolved` then
    resolves it against the *daemon's own working directory*, so the deny list would be built around
    the wrong tree while the policy still looks complete: `sandbox.enabled` true, `denyRead`
    populated, `credentials.files` populated, no error anywhere. The real home stays readable and
    nothing says so. `_read_rule`'s absolute-path guard cannot catch it either, because a resolved
    relative path IS absolute — it is simply absolute to the wrong place.
    """
    try:
        home = Path.home()
    except RuntimeError as exc:
        raise RuntimeError(
            f"the provider's home directory could not be resolved ({exc}), and the agent sandbox is "
            f"built around it; set HOME, or set {SANDBOX_ENV}=0 to run agents unconfined") from exc
    if not home.is_absolute():
        raise ValueError(
            f"HOME is {str(home)!r}, which is not an absolute path: the sandbox would deny the "
            f"directory that resolves against the provider's working directory instead of the "
            f"operator's real home, and would look correctly configured while doing it")
    return home


def preflight() -> None:
    """Check what `policy()` will need, early enough to be worth reporting.

    `policy()` runs from `agent_argv`, which is built AFTER the task's checkout — so without this a
    provider with a broken `HOME` fetches a repository, links a transcript, and only then fails with
    a message about the task runner. Called from `task_agent.preflight()`.
    """
    home_directory()
    _prove_the_sandbox_can_bind_its_sockets()


# `sizeof(struct sockaddr_un.sun_path)`: the hard ceiling on the FULL path of a Unix domain socket,
# enforced by the kernel and named in no error message the agent will ever show you.
#
# **Not one number — it differs by platform, and it was measured on both** (2026-08-10) by binding
# at increasing lengths until the kernel refused: Linux (Ubuntu 24.04) took 107 and refused 108;
# macOS 26.6 took 103 and refused 104. Writing 108 everywhere — which this constant did at first —
# under-reserves by four bytes on every macOS provider, and the reserve below is what would have
# quietly absorbed the error instead of anyone noticing.
_SOCKET_PATH_MAX = 104 if sys.platform == "darwin" else 108
# What the vendor puts BELOW the temp base before the socket itself. Observed on the dev VM: a
# `claude-<n>/` directory (9 characters at n < 10), and inside it a socket whose name was never
# read — the failure is reported as "bridge sockets", plural, with no path. So this is a RESERVE,
# not a model, and it is deliberately larger than what was seen: a reserve that is too small lets a
# provider through and then fails every task on it, while one that is too large costs a message an
# operator can act on in one command. Fail-closed is the cheap side.
_SOCKET_HEADROOM = 30


def temp_base() -> Path:
    """The directory Claude Code will put its sandbox sockets under, as the CHILD will see it.

    `task_agent.child_env` deliberately does not set `TMPDIR` (see `_cache_env` — redirecting it
    into the per-member cache tree is what broke every task on the dev VM), so the child inherits
    the provider's value through the passthrough allowlist, and falls back to `/tmp` when there is
    none. Reading `os.environ` here is therefore reading exactly what the child gets.

    ⚠️ If `TMPDIR` is ever redirected again, this must read the redirected value or the check will
    certify a directory the child never uses.
    """
    return Path(os.getenv("TMPDIR") or "/tmp")


def _prove_the_sandbox_can_bind_its_sockets() -> None:
    """Refuse the task if the sandbox will not be able to start, BEFORE anything is fetched.

    This exists because the vendor's own `failIfUnavailable` does not cover this case. Missing
    packages exit 1 before any model call — loud, and already handled. A sandbox that fails to
    INITIALIZE at runtime does not: MEASURED on the dev VM, every Bash call returned
    `Sandbox is required but failed to initialize: Failed to create bridge sockets after 5
    attempts`, the agent ran its turn, reported honestly in prose, exited **0** — and the task
    was recorded `completed` with `error: null`, having done nothing. An application polling
    `state` sees a success. That is the failure this function converts into a refusal.

    Two checks, because they catch different things and either alone would pass the other's bug:

      * **room** — `sun_path` is 108 bytes and nothing says so when it is exceeded. The instance
        that cost a deploy: a `TMPDIR` of 107 characters, one under the limit, so the directory
        itself was fine and every socket inside it was not.
      * **bindable** — the base may be missing, unwritable, or on a filesystem that has no sockets.
        Proven by binding one, because a `stat` answers a different question than `bind` does.

    Not checked here, deliberately: whether the AGENT later reports a sandbox failure in its text.
    That would mean comparing vendor prose, which this repo treats as display-only everywhere else
    (the `task.retry` rule), and it would go stale the first time the wording changed.
    """
    base = temp_base()

    room = _SOCKET_PATH_MAX - len(str(base))
    if room < _SOCKET_HEADROOM:
        raise OSError(
            f"the sandbox puts its sockets under {base}, which leaves {room} of the {_SOCKET_PATH_MAX} "
            f"bytes a Unix socket path may use — under the {_SOCKET_HEADROOM} it needs. Every command "
            f"in every task would fail with 'failed to create bridge sockets' while the task still "
            f"reported success. Point TMPDIR at a shorter directory.")

    # No `mkdir` here, and its absence is load-bearing twice over. A check that creates what it is
    # checking for cannot fail on "it is not there" — and a base that does not exist is precisely an
    # operator error worth reporting, not one to paper over. It also made both of this function's
    # first tests pass for the wrong reason: each was really failing on `mkdir`, so removing the
    # check they claimed to cover left them green (mutation-checked, twice).
    probe = base / f"grid-sandbox-{os.getpid()}.sock"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.bind(str(probe))
    except OSError as exc:
        raise OSError(
            f"the sandbox needs to create Unix sockets under {base} and this provider cannot: "
            f"{exc}. Every command in every task would fail while the task still reported success. "
            f"Point TMPDIR at a writable directory that exists.") from exc
    finally:
        # Best effort: a socket left behind is litter, not a fault, and raising from the cleanup
        # would replace a diagnosis with an unrelated error.
        try:
            probe.unlink()
        except OSError:
            pass


def _resolved(path: Path) -> str:
    """An absolute path with every symlink and `..` gone.

    Resolved because the sandbox compares real paths, and the provider's do not survive naively: on
    macOS `/var` is a symlink to `/private/var`, so a workspace under `/var/folders/...` is one path
    to us and another to the kernel. Issue 06 was lost to exactly this difference and every unit
    test agreed with the bug, because each compared our own computation against itself.
    """
    return str(Path(path).expanduser().resolve(strict=False))


def _read_rule(path: str) -> str:
    """A path as a permission rule the in-process tools honour.

    **The leading `//` is the entire control.** Measured: `Read(/abs/path)` is treated as
    project-relative, is silently prefixed with the working directory, matches nothing, and the read
    **succeeds** — a rule that looks configured and does nothing. `Read(//abs/path)` blocks. So this
    is built here, once, from an already-absolute path, and never by hand at a call site.

    The second slash comes from `path` being absolute, which was true by construction and by
    convention only — every caller passes `_resolved(...)`. Convention is too thin a thing to rest
    the one fail-open control in this module on, so it is checked: a relative path here would emit a
    single-slash rule that denies nothing, and the whole point is that nobody can tell by looking.
    """
    if not path.startswith("/"):
        raise ValueError(
            f"a deny rule can only be built from an absolute path, got {path!r}: a relative one "
            f"produces `Read(/…)`, which the binary reads as project-relative and which denies "
            f"nothing at all")
    return f"Read(/{path}/**)"


_WARNED_ABOUT: set[str] = set()


def _task_root_of(workspace_path: str) -> str:
    """The operator-chosen prefix of a workspace path — everything above `projects/`.

    Read off the path rather than from `task_agent.workspace_root()`, for two reasons that both
    bite: `task_agent` imports THIS module, so asking it would close a cycle; and the policy is
    built from whatever path the caller was handed, so a check against the environment could pass
    while the path in the profile is somebody else's.

    A path with fewer levels than `workspace_for` builds answers `""`, which never warns. That is
    the right direction: this is a hint about a configuration, and a path shaped unlike ours is not
    one it can say anything true about.
    """
    parents = Path(workspace_path).parents
    if len(parents) <= _GRID_OWNED_LEVELS:
        return ""
    return str(parents[_GRID_OWNED_LEVELS - 1])


def _warn_if_the_path_is_long_enough_to_break_exec(workspace_path: str) -> None:
    """Say it on stderr, once per path, and carry on. See `WORKSPACE_ROOT_WARNING_CHARS`."""
    root = _task_root_of(workspace_path)
    if sys.platform != "darwin" or len(root) <= WORKSPACE_ROOT_WARNING_CHARS:
        return
    if workspace_path in _WARNED_ABOUT:
        return
    _WARNED_ABOUT.add(workspace_path)
    print(
        f"Warning: {WORKSPACE_ROOT_HINT} is {len(root)} characters, and the whole task workspace "
        f"path is {len(workspace_path)}. macOS builds the sandbox profile into a single exec "
        f"argument, and past roughly 160 characters it can exceed the limit — every command the "
        f"agent runs then fails with E2BIG. Grid adds about "
        f"{len(workspace_path) - len(root)} characters of its own below the root, so keep "
        f"{WORKSPACE_ROOT_HINT} under {WORKSPACE_ROOT_WARNING_CHARS} characters if tasks report "
        f"that. ({workspace_path})",
        file=sys.stderr, flush=True)


def policy(workspace: Path, config_dir: Path) -> dict:
    """The whole `--settings` payload for one task.

    `config_dir` is passed in rather than read here: `task_agent.claude_config_dir()` is the single
    validated answer to where the provider's credential lives, and importing it would make these two
    modules circular for a value the caller already holds.
    """
    workspace_path = _resolved(workspace)
    _warn_if_the_path_is_long_enough_to_break_exec(workspace_path)
    home = home_directory()
    denied = [_resolved(home), _resolved(paths.grid_home()), _resolved(config_dir)]
    # The workspace is re-allowed explicitly, and it matters on a dev box: `GRID_TASK_ROOT` may sit
    # under the home directory that was just denied, and `allowRead` is what takes precedence.
    allowed = [workspace_path]
    # The temp directory is READ and WRITTEN by everything a build runs — compilers, package
    # managers, `python3 -c`. Read-only here would confine the agent into failing rather than into
    # safety, and a temp directory is not somewhere secrets are kept: it is granted on both axes.
    writable = [workspace_path]
    # The per-(project, member) cache tree, on both axes for exactly the reason stated above the
    # temp directory — and it is the same sentence that was NOT applied to `_BUILD_CACHE_DIRS`,
    # which is how `npm install` came to fail on every real repository. Granted here rather than by
    # widening the home caches, so nothing shared between members becomes writable.
    # `workspace_path` is already a resolved STRING, so the sibling is derived from it as a `Path`
    # and resolved again — the cache may not exist yet when the policy is built, and `resolve()` is
    # non-strict, so this is the same real path the child will be handed.
    cache = _resolved(cache_dir(Path(workspace_path)))
    allowed.append(cache)
    writable.append(cache)
    # THE OBJECT STORE this conversation's worktree hangs off (ADR 0034 D-c, issue 50), on both axes
    # for the cache tree's reason and then some.
    #
    # ⚠️ **Without this the layout does not work at all, rather than working slowly.** A linked
    # worktree's `.git` is a FILE pointing into `<store>/worktrees/<conversation>/`, so the git state
    # the agent's own commands write is no longer inside the workspace: `git status` refreshes the
    # index there, `git commit` writes loose objects into `<store>/objects/`, and ADR 0033 D-e's
    # merge turn asks the agent for `git add`/`git rm` by name. Read-only fails every one of them.
    #
    # ⚠️ **This is the one place the agent is given something shared between two of its owner's
    # conversations, and the bound on it is `member_key`.** The store is under the MEMBER directory,
    # so what widens is one person's own project — never a colleague's, which is the channel ADR 0033
    # D-g exists to close. The member directory ITSELF is deliberately not granted: it holds the
    # other conversations' working trees, and an agent that could write those would be editing a
    # sibling turn's files while it ran.
    #
    # ⚠️ **It is still wider than "that member's history", and the difference is live state.** A bare
    # common directory holds `refs/heads/task/<turn_id>` for all of that member's conversations and
    # `worktrees/<conversation_id>/{HEAD,index,…}` for the ones RUNNING — so an agent here can reach
    # a sibling turn's ref or index while it is in flight, which nothing could do before the store
    # was shared. Accepted, because closing it means not sharing; see `task_worktree`'s module
    # docstring for the full statement.
    store = _resolved(task_worktree.store_for(Path(workspace_path)))
    allowed.append(store)
    writable.append(store)
    # `temp_base()`, NOT `os.getenv("TMPDIR")`. The child's temp directory is the variable *or*
    # `/tmp` when it is unset — that is what `temp_base` answers, what `child_env` lets through, and
    # what `_prove_the_sandbox_can_bind_its_sockets` certifies. Unset is the ORDINARY case, not the
    # exotic one: a provider under systemd exports no TMPDIR at all. Reading the variable here
    # granted the directory only on the boxes that happened to set it, and left `/tmp` outside
    # `allowWrite` on the rest — the same build-fails-with-EROFS class `CACHE_DIR_NAME` exists to
    # remove, arriving through a second door and with the policy still looking complete. Two
    # spellings of "the temp directory" in one module was the bug; there is now one.
    tmp = _resolved(temp_base())
    allowed.append(tmp)
    writable.append(tmp)
    allowed += [_resolved(home / cache) for cache in _BUILD_CACHE_DIRS]

    credentials = [{"path": _resolved(paths.grid_home()), "mode": "deny"},
                   {"path": _resolved(config_dir), "mode": "deny"}]
    credentials += [{"path": _resolved(home / name), "mode": "deny"}
                    for name in (*_CREDENTIAL_DIRS, *_CREDENTIAL_FILES)]

    return {
        "sandbox": {
            "enabled": True,
            # The default is false, and the vendor's own words for it are that a sandbox which
            # cannot start yields "a warning" while "commands run unsandboxed". That is the
            # difference between a provider that fails loudly and one that lies.
            #
            # MEASURED on the dev VM (Ubuntu 24.04, 2.1.223): with the packages missing this exits 1
            # with `sandbox required but unavailable: … bubblewrap (bwrap) not installed, socat not
            # installed`, before any model call — so the fail-closed direction holds on Linux, not
            # just on macOS. The dependency is **two** packages: with `bwrap` alone it still refuses,
            # naming socat. A stock provider VM has neither, and no Claude Code either.
            "failIfUnavailable": True,
            # Approves Bash BECAUSE the command is confined, which is what replaces the old
            # `bypassPermissions` posture rather than merely narrowing it.
            "autoAllowBashIfSandboxed": True,
            # Default true. Left alone, the model can opt any command out of the sandbox.
            "allowUnsandboxedCommands": False,
            # Linux only, and **required there** — without it the product does not work at all on
            # the fleet's own operating system. MEASURED on the dev VM (Ubuntu 24.04, kernel 6.8):
            # every Bash command fails with `apply-seccomp: write /proc/self/setgroups (nested userns
            # is capability-restricted; caller must provide CAP_SYS_ADMIN)`, because Ubuntu 24.04
            # ships `kernel.apparmor_restrict_unprivileged_userns=1` and Claude Code's sandbox nests
            # a user namespace to run each command. Reproduced underneath Claude Code entirely: a
            # single `bwrap` succeeds, `bwrap` inside `bwrap` fails `setting up uid map: Read-only
            # file system`. Running as root does not help — the capability is dropped by the outer
            # namespace, not by the account.
            #
            # The name says "weaker" and the vendor gives it no description, so it was not taken on
            # trust: with it enabled, a task still cannot read a file outside its workspace — the
            # denied path comes back as `No such file or directory` from `cat`, i.e. masked by the
            # kernel — while a file inside the workspace reads normally, and `pip install` works.
            # So what it restores is the ability to nest, not the confinement itself.
            #
            # The alternative is asking every operator to set
            # `kernel.apparmor_restrict_unprivileged_userns=0`, which weakens the HOST's protection
            # against a known local-privilege-escalation class, on every provider, by hand. This
            # keeps the weakening inside the sandbox's own configuration instead, where it is
            # measured. Set only on Linux: macOS uses seatbelt, needs none of it, and was measured
            # working without it.
            **({"enableWeakerNestedSandbox": True} if sys.platform.startswith("linux") else {}),
            "filesystem": {"denyRead": denied, "allowRead": allowed, "allowWrite": writable},
            "credentials": {
                "files": credentials,
                "envVars": [{"name": name, "mode": "deny"} for name in _DENIED_ENV_NAMES],
            },
            "network": {"allowedDomains": list(allowed_domains()), "strictAllowlist": True},
        },
        # The second layer. These govern the in-process tools — what the Claude Code process reads on
        # the model's behalf — which the sandbox never sees. Measured: without them, a run that has
        # the whole sandbox above still reads a file outside the workspace.
        "permissions": {"deny": [_read_rule(path) for path in denied]},
    }


def settings_argument(workspace: Path, config_dir: Path) -> str:
    """The policy as the one argv element `--settings` takes.

    A JSON string rather than a file, which `claude --help` accepts equally (`<file-or-json>`): a
    file would have to be written somewhere, cleaned up, and kept out of the git worktree — and it
    would be editable by the very agent it constrains while the session is running.
    """
    return json.dumps(policy(workspace, config_dir), separators=(",", ":"))
