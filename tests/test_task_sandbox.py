"""The confinement policy a provider hands its agent (ADR 0033 D-n, issue 23 layer 2).

Every number and every polarity here was **measured** against Claude Code 2.1.223 on macOS 26.6 on
2026-08-06, with the provider's own argv shape, before any of it was written. The measurements are
recorded once, here, because each one is a decision this module would otherwise look arbitrary for:

| # | run | result |
|---|---|---|
| 1 | `acceptEdits` + `filesystem.denyRead: [$HOME]`, agent asked to read a canary outside the workspace | **blocked** — `Operation not permitted (os error 1)`, an `EPERM` from the kernel |
| 2 | today's argv (`bypassPermissions`, no policy) | the canary **came back** — the control fires, so run 1 means something |
| 3 | `bypassPermissions` **with** the whole policy | the canary **came back** — the in-process `Read` tool is not sandboxed; only Bash is |
| 4 | the policy with **no** `network` section | **all egress denied** (`curl: (56) CONNECT tunnel failed, response 403`) |
| 5 | `allowedDomains: ["*"]` / a curated list / an off-list host under `strictAllowlist` | 200 / 200 / 403 |
| 6 | `filesystem.allowRead: [$HOME]` against a `credentials.files` deny entry | still **blocked** — the credential entry wins |
| 7 | `permissions.deny: ["Read(/abs)"]` vs `["Read(//abs)"]` | **leaked** vs blocked — one slash is the whole control |
| 8 | a normal build-and-test task under `acceptEdits` | completed; writes and Bash both worked |
| 9 | `network` section PRESENT with an **empty** `allowedDomains` | **all egress denied** (403) — an empty list is not "unset" |

Row 9 was measured because a reviewer noticed it was being *claimed* rather than measured: rows 4
and 9 are different configurations, and the docs promise `GRID_TASK_ALLOWED_DOMAINS=` means "no
egress at all". It does — but nothing had checked it, and an allowlist library treating empty as
"unrestricted" is a common enough default that the claim was not safe to make for free.

Run 3 is why `permission_mode` refuses `bypassPermissions` while this is on, and why the policy
carries `permissions.deny` rules **as well as** `filesystem.denyRead`: the sandbox covers what the
model's Bash commands do, and the permission rules cover what the Claude Code process does on the
model's behalf. Neither covers the other's half.

Run 7 is why no path here is ever built by string-joining a single slash, and it has a live test of
its own in `tests/e2e_agent_sandbox.py` — the trap is that a wrong rule **fails open** and looks
configured, so only a test that proves the *denial* is worth anything.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


@pytest.fixture
def config_dir(tmp_path):
    return tmp_path / "provider-config"


def test_the_policy_turns_the_sandbox_on_and_closes_its_escape_hatches(tmp_path, config_dir):
    """The four switches, each with a reason that is not "it seemed safer".

    `failIfUnavailable` is the one that would be easy to leave out and expensive to have left out:
    the vendor's own words are that when it is false — the DEFAULT — a sandbox that cannot start
    produces "a warning" and "commands run unsandboxed". Providers run Linux, where the backend is
    bubblewrap and `bwrap` has to be installed. Without this, the first provider missing it reports
    `completed` on every task with nothing confined and no signal anywhere saying so.
    """
    from remote import task_sandbox

    sandbox = task_sandbox.policy(tmp_path / "workspace", config_dir)["sandbox"]

    assert sandbox["enabled"] is True
    assert sandbox["failIfUnavailable"] is True, (
        "a provider whose sandbox cannot start would run every task unconfined and say nothing")
    assert sandbox["autoAllowBashIfSandboxed"] is True
    assert sandbox["allowUnsandboxedCommands"] is False, (
        "the `dangerouslyDisableSandbox` parameter is left available to the model")


def test_the_provider_and_the_operators_credentials_are_denied(tmp_path, config_dir, monkeypatch):
    """`~/.grid` first: it holds the grid access token and the vendor API keys this product issues.

    Denied twice on purpose — in `filesystem.denyRead`, which the sandbox enforces on the model's own
    commands, and in `credentials.files`, which measurement showed survives a broader `allowRead`
    that would otherwise re-open it.
    """
    from shared import paths

    from remote import task_sandbox

    monkeypatch.setenv("GRID_HOME", str(tmp_path / "grid-home"))

    sandbox = task_sandbox.policy(tmp_path / "workspace", config_dir)["sandbox"]

    grid_home = str(paths.grid_home().resolve())
    assert grid_home in sandbox["filesystem"]["denyRead"]
    assert str(Path.home().resolve()) in sandbox["filesystem"]["denyRead"]
    assert str(config_dir.resolve()) in sandbox["filesystem"]["denyRead"]

    protected = {entry["path"] for entry in sandbox["credentials"]["files"]}
    assert grid_home in protected
    assert str(config_dir.resolve()) in protected
    assert str((Path.home() / ".ssh").resolve()) in protected
    assert all(entry["mode"] == "deny" for entry in sandbox["credentials"]["files"])


def test_every_denied_path_also_has_a_permission_rule_with_two_slashes(tmp_path, config_dir):
    """The second layer, and the one-character trap that makes it worth its own test.

    The sandbox confines Bash. The `Read` tool runs inside the Claude Code process, which the sandbox
    never sees — measured: `bypassPermissions` plus this whole policy read a file outside the
    workspace without trouble. So the same paths are denied again as permission rules.

    And they are written `Read(//abs/**)`. With ONE slash the path is treated as project-relative,
    is silently prefixed, matches nothing, and the read succeeds — a control that looks configured
    and does nothing. `tests/e2e_agent_sandbox.py` proves the difference against the real binary;
    this pins the shape so it cannot be "simplified" back.
    """
    from remote import task_sandbox

    policy = task_sandbox.policy(tmp_path / "workspace", config_dir)

    denied = policy["sandbox"]["filesystem"]["denyRead"]
    rules = policy["permissions"]["deny"]
    assert len(rules) == len(denied)
    for path in denied:
        assert f"Read(//{path.lstrip('/')}/**)" in rules
    for rule in rules:
        assert rule.startswith("Read(//"), (
            f"{rule!r} has one leading slash, so it is read as project-relative and denies nothing")


def test_linux_gets_the_nested_sandbox_option_and_macos_does_not(monkeypatch, tmp_path, config_dir):
    """Without this, no task on the fleet can run a single command.

    MEASURED on the dev VM (Ubuntu 24.04, kernel 6.8): every Bash command fails with
    `apply-seccomp: write /proc/self/setgroups (nested userns is capability-restricted)`, because
    24.04 ships `kernel.apparmor_restrict_unprivileged_userns=1` and the sandbox nests a user
    namespace per command. Confirmed beneath Claude Code: one `bwrap` works, `bwrap` inside `bwrap`
    does not.

    It is platform-gated rather than unconditional because macOS was measured working without it,
    and an option whose own name says "weaker" does not get switched on where nothing needs it.
    """
    from remote import task_sandbox

    monkeypatch.setattr(task_sandbox.sys, "platform", "linux")
    linux = task_sandbox.policy(tmp_path / "workspace", config_dir)["sandbox"]
    assert linux["enableWeakerNestedSandbox"] is True

    monkeypatch.setattr(task_sandbox.sys, "platform", "darwin")
    macos = task_sandbox.policy(tmp_path / "workspace", config_dir)["sandbox"]
    assert "enableWeakerNestedSandbox" not in macos

    # And it is not a licence to drop anything else: the confinement it sits beside is unchanged,
    # which is what the live run on the VM verified (a denied path still comes back absent).
    assert linux["filesystem"]["denyRead"] == macos["filesystem"]["denyRead"]
    assert linux["allowUnsandboxedCommands"] is False


def test_a_relative_home_is_refused_rather_than_silently_protecting_the_wrong_tree(monkeypatch,
                                                                                   tmp_path,
                                                                                   config_dir):
    """`HOME=.` does not raise — it quietly makes the whole deny list point somewhere else.

    `Path.home()` returns `.`, and `_resolved` resolves that against the daemon's own working
    directory. The policy then comes back fully populated — sandbox enabled, `denyRead` and
    `credentials.files` both filled in — while the operator's real home, and the credentials in it,
    stay readable. Nothing raises and nothing warns.

    `_read_rule`'s absolute-path guard cannot catch this: a resolved relative path IS absolute. It
    is absolute to the wrong place, which is exactly why this needs its own check.
    `shared/launch/claude_install.install_locations` refuses the same input for the same reason.
    """
    from remote import task_sandbox

    monkeypatch.setenv("HOME", ".")

    with pytest.raises(ValueError) as excinfo:
        task_sandbox.policy(tmp_path / "workspace", config_dir)

    assert "absolute" in str(excinfo.value)


def test_a_deny_rule_cannot_be_built_from_a_relative_path(tmp_path, config_dir):
    """The one fail-open control in this module, checked rather than left to convention.

    Every caller passes `_resolved(...)`, so the leading `//` is guaranteed by construction today.
    That guarantee is one refactor thick, and the failure it protects against is invisible: a
    relative path produces `Read(/…)`, which the binary reads as project-relative, which matches
    nothing, which means the deny list is there and does nothing.
    """
    from remote import task_sandbox

    with pytest.raises(ValueError) as excinfo:
        task_sandbox._read_rule("relative/path")

    assert "absolute" in str(excinfo.value)


def test_the_temp_directory_is_writable_and_not_merely_readable(monkeypatch, tmp_path, config_dir):
    """Compilers, package managers and `python3 -c` all write there.

    Read-only would confine the agent into failing rather than into safety — and acceptance rests on
    an ordinary build-and-test task still passing. A temp directory is not where secrets live.
    """
    from remote import task_sandbox

    monkeypatch.setenv("TMPDIR", str(tmp_path / "scratch"))

    filesystem = task_sandbox.policy(tmp_path / "workspace", config_dir)["sandbox"]["filesystem"]

    expected = str((tmp_path / "scratch").resolve())
    assert expected in filesystem["allowRead"]
    assert expected in filesystem["allowWrite"]


def test_the_workspace_stays_readable_even_when_it_sits_inside_the_denied_home(monkeypatch,
                                                                               tmp_path):
    """A dev box points `GRID_TASK_ROOT` at a directory under `$HOME`, which is denied wholesale.

    `allowRead` is documented to take precedence over `denyRead`, so the workspace is re-allowed by
    name and the agent can still read the repository it was asked to work on. Without this the
    confinement would be perfect and every task would fail.
    """
    from remote import task_sandbox

    workspace = Path.home() / ".grid-test-workspace" / "projects" / "p" / "workspace"

    sandbox = task_sandbox.policy(workspace, tmp_path / "cfg")["sandbox"]

    assert str(Path.home().resolve()) in sandbox["filesystem"]["denyRead"]
    assert str(workspace.resolve()) in sandbox["filesystem"]["allowRead"]
    assert str(workspace.resolve()) in sandbox["filesystem"]["allowWrite"]


def test_build_caches_survive_the_denied_home(tmp_path, config_dir):
    """Acceptance rests on a normal build-and-test task still passing.

    `$HOME` is denied, and every package manager keeps its cache there. Re-allowed by name, which is
    a list that will need adding to — and the failure when something is missing is a task that
    cannot install a dependency, which is loud.
    """
    from remote import task_sandbox

    allowed = task_sandbox.policy(tmp_path / "workspace", config_dir)["sandbox"]["filesystem"][
        "allowRead"]

    for cache in (".cache", ".npm", ".cargo"):
        assert str((Path.home() / cache).resolve()) in allowed


def test_the_vendor_api_key_reaches_the_process_but_not_the_commands_the_model_runs(tmp_path,
                                                                                    config_dir):
    """The split issue 23 asks for, in one assertion each side.

    `task_agent.child_env` lets `ANTHROPIC_*` through so a provider authenticating with an API key
    still works; this stops the model's own commands from reading it out of their environment.
    """
    from remote import task_agent, task_sandbox

    denied = {entry["name"]: entry["mode"]
              for entry in task_sandbox.policy(tmp_path / "workspace",
                                               config_dir)["sandbox"]["credentials"]["envVars"]}

    assert denied["ANTHROPIC_API_KEY"] == "deny"
    assert task_agent._is_allowed_env("ANTHROPIC_API_KEY"), (
        "the process itself needs the key it authenticates with")


def test_egress_is_an_allowlist_the_operator_can_replace(tmp_path, config_dir, monkeypatch):
    """Measured: with **no** network section, every host is denied and `pip install` fails.

    So the section is mandatory and its content is a decision. The default is what an ordinary build
    reaches for; `GRID_TASK_ALLOWED_DOMAINS` replaces it wholesale, because a provider that needs an
    internal registry usually wants to say exactly what it permits rather than add to a list it
    cannot see.
    """
    from remote import task_sandbox

    network = task_sandbox.policy(tmp_path / "workspace", config_dir)["sandbox"]["network"]
    assert "pypi.org" in network["allowedDomains"]
    assert network["strictAllowlist"] is True, (
        "an off-list host would be prompted, and a prompt in print mode is a denial nobody can read")

    monkeypatch.setenv("GRID_TASK_ALLOWED_DOMAINS", "registry.internal, pypi.org")
    replaced = task_sandbox.policy(tmp_path / "workspace", config_dir)["sandbox"]["network"]
    assert replaced["allowedDomains"] == ["registry.internal", "pypi.org"]


def test_the_settings_argument_is_one_json_element_that_round_trips(tmp_path, config_dir):
    """It reaches the binary as a single argv element, so it must survive `json.loads` exactly.

    A file would have to be written, cleaned up, kept out of the git worktree — and would be editable
    by the agent it constrains, mid-session.
    """
    from remote import task_sandbox

    argument = task_sandbox.settings_argument(tmp_path / "workspace", config_dir)

    assert json.loads(argument) == task_sandbox.policy(tmp_path / "workspace", config_dir)
    assert "\n" not in argument


@pytest.mark.skipif(sys.platform != "darwin", reason="the seatbelt profile is a macOS argv element")
def test_a_workspace_path_long_enough_to_break_exec_says_so_on_stderr(config_dir, capsys):
    """Measured: past roughly 120 characters, every Bash command in the task dies with `E2BIG`.

    The seatbelt profile is handed to each sandboxed command as one argv element and grows with this
    path — at 160 and 186 characters it reached 1.5MB and 1MB, past what `exec` accepts. What the
    agent then reports is about exec argument limits, which nobody reads as "the workspace root is
    too deep".

    A warning rather than a refusal, and the reason is in the measurement: 160 produced a LARGER
    profile than 186, so the length is a symptom and not the cause. A hard limit built on a proxy
    that non-monotonic would refuse providers that work, on a rule that cannot be defended — so this
    prints the clue where an operator is already looking and lets the task run.
    """
    from remote import task_sandbox

    task_sandbox._WARNED_ABOUT.clear()
    long_path = Path("/private/tmp") / ("d" * 40) / ("e" * 40) / ("f" * 40) / "workspace"

    task_sandbox.policy(long_path, config_dir)

    warning = capsys.readouterr().err
    assert "E2BIG" in warning
    assert task_sandbox.WORKSPACE_ROOT_HINT in warning

    # Once per path, not once per task: a provider claiming all day would otherwise print this
    # thousands of times and bury whatever else its log had to say.
    task_sandbox.policy(long_path, config_dir)
    assert capsys.readouterr().err == ""


def test_confinement_is_on_unless_an_operator_turns_it_off(monkeypatch):
    """Never a fallback this module takes by itself — only a deliberate, visible act."""
    from remote import task_sandbox

    monkeypatch.delenv(task_sandbox.SANDBOX_ENV, raising=False)
    assert task_sandbox.enabled() is True

    monkeypatch.setenv(task_sandbox.SANDBOX_ENV, "0")
    assert task_sandbox.enabled() is False

    # Anything else is on, including a typo: the failure direction for a misspelt value must be
    # "confined" rather than "unconfined and nobody noticed".
    monkeypatch.setenv(task_sandbox.SANDBOX_ENV, "flase")
    assert task_sandbox.enabled() is True


# --- The writable cache tree (F-02, measured on the dev VM 2026-08-10) ----------------------------
#
# `_BUILD_CACHE_DIRS` grants the provider's package caches READ-ONLY, and a read-only cache does not
# degrade — it FAILS. Measured against Claude Code 2.1.226 in a real task: a plain `npm install`
# died with `EROFS: read-only file system, open '/root/.npm/_cacache/tmp/…'` and `node` then
# reported `MODULE_NOT_FOUND`. `pip` survived the identical policy only because it degrades to
# no-cache, which is why "pip install works" was both true and misleading.
#
# The fix is a writable tree BESIDE the workspace rather than widening the shared home caches. These
# tests pin all three properties that makes it depend on, because getting any one wrong reintroduces
# a different bug: it must be writable, it must NOT be inside the workspace, and the shared caches
# must stay read-only.


def test_the_cache_tree_is_writable_so_a_package_manager_can_use_it(tmp_path, config_dir):
    """The regression itself. Read-only here is the `EROFS` that stopped every npm install."""
    from remote import task_sandbox

    workspace = tmp_path / "projects" / "p" / "m" / "workspace"

    filesystem = task_sandbox.policy(workspace, config_dir)["sandbox"]["filesystem"]

    cache = str(task_sandbox.cache_dir(workspace).resolve())
    assert cache in filesystem["allowRead"]
    assert cache in filesystem["allowWrite"], "a read-only cache fails npm outright, not gracefully"


def test_the_cache_tree_is_a_SIBLING_of_the_workspace_never_inside_it(tmp_path):
    """`task_repo.commit_and_push` runs `git add -A`.

    A cache under the workspace would therefore be committed into the team's history — a
    `node_modules` at a time, permanently, on the first task that installed anything. This is the
    property that decides where the directory goes, so it is asserted on the path itself rather
    than inferred from the policy.
    """
    from remote import task_sandbox

    workspace = tmp_path / "projects" / "p" / "m" / "workspace"
    cache = task_sandbox.cache_dir(workspace)

    assert cache.parent == workspace.parent
    assert workspace not in cache.parents
    assert not str(cache).startswith(str(workspace) + os.sep)


def test_the_SHARED_home_caches_stay_read_only(tmp_path, config_dir):
    """The security half, and the reason the fix is not the one-line version.

    `~/.npm`, `~/.cargo` and the rest belong to the PROVIDER and are shared by every task and every
    member on the box. Writable, they are a cross-member contamination channel — one member's task
    plants a package another member's task then installs — which is exactly what ADR 0033 D-g
    exists to prevent. A future "just make the caches writable" fix fails here.
    """
    from remote import task_sandbox

    filesystem = task_sandbox.policy(tmp_path / "workspace", config_dir)["sandbox"]["filesystem"]

    for cache in task_sandbox._BUILD_CACHE_DIRS:
        resolved = str((Path.home() / cache).resolve())
        assert resolved in filesystem["allowRead"]
        assert resolved not in filesystem["allowWrite"], (
            f"{cache} is shared between members; writable makes it a contamination channel")


# --- preflight proves the sandbox can actually start (F-04) ---------------------------------------
#
# The vendor's `failIfUnavailable` covers MISSING PACKAGES — that exits 1 before any model call and
# the provider already reports it. What it does not cover is a sandbox that fails to INITIALIZE at
# runtime. MEASURED on the dev VM: every Bash call returned `Sandbox is required but failed to
# initialize: Failed to create bridge sockets after 5 attempts`, the agent ran its turn, reported
# honestly in prose, exited 0 — and the task was recorded `completed` with `error: null` having done
# nothing at all. An application polling `state` reads that as success.


def test_preflight_refuses_a_temp_base_with_no_room_for_a_socket(monkeypatch):
    """The RESERVE alone — and the window it has to be tested in is the whole subtlety.

    The base here exists, and a socket with THIS function's own short probe name would bind in it
    fine. What it has no room for is the vendor's longer path. That window is exactly why the
    reserve is a separate check from the bind: a bind that succeeds proves nothing about a path 20
    characters longer.

    Getting this wrong is not hypothetical. The first version padded "until at least the reserve is
    exceeded", which could overshoot far enough that the probe socket ALSO failed to bind — and then
    deleting the reserve check left the test green, because the bind was catching it. Mutation
    checked: with `if room < _SOCKET_HEADROOM` disabled, this test fails.

    The length being reproduced is real: `/var/grid-provider/projects/<uuid-36>/<member_key-32>/
    cache/tmp` is 107 characters, ONE under `sun_path`'s 108, so the directory was fine and every
    socket inside it was not.
    """
    import tempfile

    from remote import task_sandbox

    # Three characters INSIDE the reserve. The window is narrow and both edges are real: below it
    # the reserve does not fire, above it the PROBE socket stops binding and the bind check catches
    # the case instead — which is how the first version of this test let the reserve be deleted.
    target = task_sandbox._SOCKET_PATH_MAX - task_sandbox._SOCKET_HEADROOM + 3

    base = Path(tempfile.mkdtemp())
    assert len(str(base)) < target, "the system temp base is too long to build this case on"
    base = base / ("x" * (target - len(str(base)) - 1))
    base.mkdir()
    try:
        assert task_sandbox._SOCKET_PATH_MAX - len(str(base)) < task_sandbox._SOCKET_HEADROOM
        # ...and the probe socket WOULD have bound here, which is what isolates the reserve.
        assert len(str(base / f"grid-sandbox-{os.getpid()}.sock")) < task_sandbox._SOCKET_PATH_MAX

        monkeypatch.setenv("TMPDIR", str(base))

        with pytest.raises(OSError) as raised:
            task_sandbox.preflight()

        message = str(raised.value)
        assert "sockets" in message
        # Names the fix, not just the fault — the operator's next command is in the sentence.
        assert "TMPDIR" in message
    finally:
        base.rmdir()
        base.parent.rmdir()


def test_preflight_refuses_a_temp_base_it_cannot_bind_in(monkeypatch, tmp_path):
    """The bind check ALONE — a short path that simply is not there.

    A `stat` answers a different question than `bind` does, and this is the case a length check
    cannot see. Chosen over an unwritable directory deliberately: a suite running as root would walk
    straight through a permission bit and the test would certify nothing.
    """
    from remote import task_sandbox

    monkeypatch.setenv("TMPDIR", "/nonexistent-grid-sandbox-probe")

    with pytest.raises(OSError) as raised:
        task_sandbox.preflight()

    assert "sockets" in str(raised.value)


def test_preflight_passes_on_an_ordinary_provider_and_leaves_nothing_behind(monkeypatch):
    """The positive control, and the litter check beside it.

    Without a case that MUST pass, a preflight that refused everything would look like a working
    guard. The probe socket is unlinked because a provider claiming all day would otherwise fill its
    temp directory with them.

    Uses a directory under the SYSTEM temp base rather than pytest's `tmp_path`, and that is not a
    detail: pytest's is ~121 characters, which this check correctly refuses — the first version of
    this test failed for exactly that reason and was right to. A provider's real base is short (see
    the test below).
    """
    import tempfile

    from remote import task_sandbox

    base = Path(tempfile.mkdtemp())
    try:
        monkeypatch.setenv("TMPDIR", str(base))

        task_sandbox.preflight()

        assert list(base.iterdir()) == [], "the probe socket must not be left behind"
    finally:
        for leftover in base.iterdir():
            leftover.unlink()
        base.rmdir()


def test_the_headroom_leaves_a_real_provider_alone(monkeypatch):
    """The constant is a reserve, so it has to be checked against what providers actually have.

    MEASURED 2026-08-10: the dev VM (Ubuntu 24.04) reports `TMPDIR` unset and falls back to `/tmp`
    — 4 characters, 104 of headroom. A macOS provider gets `/var/folders/<2>/<30>/T/` — 49
    characters, 59 of headroom. Both clear the reserve several times over, which is what makes
    fail-closed affordable here: raising it costs nobody anything until a path is genuinely absurd.

    Pinned so that raising `_SOCKET_HEADROOM` on a hunch fails a test instead of a fleet.
    """
    from remote import task_sandbox

    for base, measured in (("/tmp", 4), ("/var/folders/92/v3xrh9kd62lglrnl9rmvsrg00000gn/T/", 49)):
        assert len(base) == measured
        monkeypatch.setenv("TMPDIR", base)
        assert task_sandbox._SOCKET_PATH_MAX - len(str(task_sandbox.temp_base())) >= (
            task_sandbox._SOCKET_HEADROOM), f"{base} is a real provider default and must not be refused"


def test_the_socket_ceiling_is_the_one_this_platform_actually_enforces():
    """`sun_path` is not one number, and assuming it was under-reserved on every macOS provider.

    MEASURED 2026-08-10 by binding at increasing lengths until the kernel refused: Linux
    (Ubuntu 24.04) bound 107 and refused 108; macOS 26.6 bound 103 and refused 104. This asserts the
    module agrees with the platform it is running on — and it does it by BINDING, so a future
    platform simply tells the truth rather than matching a table.
    """
    import socket
    import tempfile

    from remote import task_sandbox

    directory = Path(tempfile.mkdtemp())
    try:
        longest = None
        for length in range(1, 200):
            candidate = directory / ("s" * length)
            if len(str(candidate)) >= 200:
                break
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.bind(str(candidate))
                candidate.unlink()
                longest = len(str(candidate))
            except OSError:
                break

        assert longest is not None, "could not bind any socket here; the measurement means nothing"
        # The kernel refuses at exactly the ceiling, so the longest that binds is one less.
        assert longest + 1 == task_sandbox._SOCKET_PATH_MAX, (
            f"this platform refuses at {longest + 1}, the module reserves against "
            f"{task_sandbox._SOCKET_PATH_MAX}")
    finally:
        for leftover in directory.iterdir():
            leftover.unlink()
        directory.rmdir()


def test_temp_base_reads_what_the_child_will_actually_get(monkeypatch):
    """`child_env` does not set `TMPDIR`, so the child inherits the provider's — or falls back.

    Pinned in both directions: a check that read a different variable than the child uses would
    certify a directory nobody writes to.
    """
    from remote import task_sandbox

    monkeypatch.setenv("TMPDIR", "/somewhere")
    assert task_sandbox.temp_base() == Path("/somewhere")

    monkeypatch.delenv("TMPDIR", raising=False)
    assert task_sandbox.temp_base() == Path("/tmp")
