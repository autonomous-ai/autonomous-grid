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
import sys
from pathlib import Path

from shared import paths

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

# Where package managers keep their caches, re-allowed inside the denied home. Without these a task
# that installs anything re-downloads it at best and fails at worst, and acceptance rests on a normal
# build-and-test task still passing. `allowRead` is documented to take precedence over `denyRead`;
# it does **not** override a `credentials.files` deny entry, which was measured rather than assumed.
_BUILD_CACHE_DIRS = (
    ".cache", ".npm", ".yarn", ".pnpm-store", ".cargo", ".rustup", ".gradle", ".m2", ".bun",
    ".local/share/uv", ".local/share/pnpm", ".nvm", ".pyenv", ".rbenv",
)

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
# `/var/grid/projects/<uuid>/workspace` is about 65 characters and multi-membership (issue 11) adds
# roughly 16 more, so nothing this design produces is near it; this is for the operator who moved the
# root. Not applied off macOS: bubblewrap does not pass a profile this way, and D-n already requires
# the Linux backend to be re-measured on the fleet rather than assumed to behave like seatbelt.
WORKSPACE_PATH_WARNING_CHARS = 120


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


def _warn_if_the_path_is_long_enough_to_break_exec(workspace_path: str) -> None:
    """Say it on stderr, once per path, and carry on. See `WORKSPACE_PATH_WARNING_CHARS`."""
    if sys.platform != "darwin" or len(workspace_path) <= WORKSPACE_PATH_WARNING_CHARS:
        return
    if workspace_path in _WARNED_ABOUT:
        return
    _WARNED_ABOUT.add(workspace_path)
    print(
        f"Warning: the task workspace path is {len(workspace_path)} characters. macOS builds the "
        f"sandbox profile into a single exec argument, and past roughly "
        f"{WORKSPACE_PATH_WARNING_CHARS} it can exceed the limit — every command the agent runs "
        f"then fails with E2BIG. Point {WORKSPACE_ROOT_HINT} at a shorter directory if tasks report "
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
    tmp = os.getenv("TMPDIR")
    if tmp:
        allowed.append(_resolved(Path(tmp)))
        writable.append(_resolved(Path(tmp)))
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
