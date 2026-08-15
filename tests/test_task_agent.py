"""What child a task spawns, where it runs, and how its stream becomes events (ADR 0032, issue 03).

Split out of `test_local_cli.py` rather than appended to it: these cover the two new modules
(`remote/task_agent.py`, `remote/task_stream.py`) whose subject is the agent child itself, while the
claim/run/report loop's own tests stay beside the rest of the task-loop suite.
"""
import json
import subprocess
from pathlib import Path

import pytest

# A `member_key` shaped like the relay's — 32 hex characters, `sha256(user_id)` truncated (grid-src
# `project_members.MEMBER_KEY_CHARS`). Since ADR 0033 D-g a workspace belongs to a (project, member)
# pair, so almost every test here needs one. Two of them, because several of the interesting
# properties are about one member NOT reaching another's.
_MEMBER = "9f2b" * 8
_OTHER_MEMBER = "4c7e" * 8

# The CONVERSATION a workspace belongs to since ADR 0034 D-c — a `uuid4()` the relay minted, sent on
# the claim payload as `conversation_id`. Two of them for `_MEMBER`'s reason, and here the properties
# are about one member's two conversations not reaching each other, which is the whole of issue 38.
_CONVERSATION = "2f0b9b1e-7a4c-4d5e-9c31-0a1b2c3d4e5f"
_OTHER_CONVERSATION = "8d1a4c60-3b2e-4f7a-95d8-6e0f1a2b3c4d"


def test_workspace_is_the_shared_path_every_provider_must_agree_on(monkeypatch, tmp_path):
    """`<root>/projects/<project_id>/<member_key>/<conversation_id>/workspace` — a LOCKSTEP value.

    Claude Code derives a session's transcript directory from the working directory, so a provider
    using a different prefix cannot `--resume` a session another one started (ADR 0032). The root is
    overridable only so tests and dev boxes need not write to `/var`.

    The member level arrived with ADR 0033 D-g: two members' tasks landing on one provider would
    otherwise share a directory that `materialize` opens with `reset --hard` and `clean -ffdx`.
    The conversation level is ADR 0034 D-c, and it is the same argument one level down — a member's
    two conversations are two Claude Code sessions, and one directory can only be one session.
    """
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_ROOT", str(tmp_path))

    assert (task_agent.workspace_for("proj-1", "9f2b" * 8, _CONVERSATION)
            == tmp_path / "projects" / "proj-1" / ("9f2b" * 8) / _CONVERSATION / "workspace")


def test_two_conversations_of_one_member_are_two_directories(monkeypatch, tmp_path):
    """Issue 38's demo, at the level the provider decides it.

    Stated separately from the path above because the path could be right and this still wrong —
    a segment built from something constant, or from the member key twice, satisfies the shape and
    gives both conversations one directory. That directory is one transcript directory, so the
    second conversation resumes the first's session while every signal reads healthy.
    """
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_ROOT", str(tmp_path))

    first = task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION)
    second = task_agent.workspace_for("proj-1", _MEMBER, _OTHER_CONVERSATION)

    assert first != second, (
        "a member's two conversations share one workspace, so they share one Claude Code session")
    # And they are SIBLINGS under the member, not two unrelated trees: issue 50's shared object
    # store depends on the conversations of one member sitting side by side.
    assert first.parent.parent == second.parent.parent


def test_a_member_key_is_accepted_as_a_path_segment(monkeypatch, tmp_path):
    """The relay's `member_key` must survive this repo's allowlist (ADR 0033 D-a, issue 10).

    `auth.user_id` is `grid:<network>:<sub>` — it contains **colons**, which `_SAFE_PROJECT_ID`
    rejects and git forbids in a ref name. So the relay derives `member_key = sha256(user_id)`
    truncated and sends THAT; issues 11 and 12 build `<project>/<member_key>/workspace` and
    `wip/<member_key>` from it.

    This asserts the two halves agree BEFORE anything depends on it. The key is spelled out rather
    than imported, because the relay is a different repository — a test that computed it here with
    this repo's own rule would agree with itself no matter what the relay actually sends.
    """
    import hashlib

    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_ROOT", str(tmp_path))
    user_id = "grid:2f0b9b1e-7a4c-4d5e-9c31-0a1b2c3d4e5f:106174299838271639492"
    member_key = hashlib.sha256(user_id.encode()).hexdigest()[:32]

    assert task_agent._SAFE_PROJECT_ID.match(member_key), (
        f"the relay's member_key {member_key!r} is not a legal path segment here — every task "
        "would fail on the workspace path from issue 11 onward")
    assert len(member_key) <= task_agent._MAX_PROJECT_ID_CHARS
    # And it survives the function that actually builds a path, not only the regex.
    assert (task_agent.workspace_for("proj-1", member_key, _CONVERSATION)
            .parent.parent.name == member_key)

    # The other half, and the reason the key exists at all: the RAW id must stay refused. Asserted
    # rather than left implied so that nobody later "fixes" the validator to accept colons — which
    # would compile, pass every other test, and produce a directory name git cannot carry as a ref
    # (issue 12's `wip/<member_key>`) on the very first task of every real user.
    assert not task_agent._SAFE_PROJECT_ID.match(user_id), (
        "the validator accepts a raw grid:<network>:<sub> id — a colon is illegal in a git ref "
        "name, so `member_key` would have no reason to exist and issue 12 would break")
    with pytest.raises(ValueError, match="member key"):
        task_agent.workspace_for("proj-1", user_id, _CONVERSATION)


_HOSTILE_SEGMENTS = [
    "../../etc",           # climbs out of the root entirely
    "a/b",                 # a separator invents a level nobody agreed on
    "a\\b",                # the same on Windows, where `\` is the separator
    "/etc",                # absolute: `Path(root) / "/etc"` IS `/etc`, silently
    "",                    # empty: the path collapses to the level above
    ".",
    "..",
    "x" * 4096,            # longer than any filesystem accepts, so `mkdir` fails obscurely
]


@pytest.mark.parametrize("hostile", _HOSTILE_SEGMENTS)
@pytest.mark.parametrize("position", ["project id", "member key", "conversation id"])
def test_a_hostile_path_segment_is_refused_before_anything_is_created(
        monkeypatch, tmp_path, hostile, position):
    """All three segments arrive off the wire, so all three are attacker-controlled (ADR 0032 D-b).

    `Path(root) / "../../etc"` is not a theoretical escape — it resolves, and the provider would then
    create a directory and run an agent with write access outside the tree entirely. Refused where the
    path is BUILT, so no caller can forget to check.

    Parametrized over the POSITION as well as the value, and that is the point of the rewrite: issue
    11 added a second segment and issue 38 a third, and validating only the ones that came before
    would leave a hole that every existing case in this table walks straight through.
    """
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_ROOT", str(tmp_path))
    args = {
        "project id": (hostile, _MEMBER, _CONVERSATION),
        "member key": ("proj-1", hostile, _CONVERSATION),
        "conversation id": ("proj-1", _MEMBER, hostile),
    }[position]

    with pytest.raises(ValueError) as excinfo:
        task_agent.workspace_for(*args)

    assert position in str(excinfo.value), (
        f"the refusal does not say which segment was wrong: {excinfo.value}")
    assert list(tmp_path.iterdir()) == []


def test_the_workspace_and_every_level_above_it_are_never_shared_writable(monkeypatch, tmp_path):
    """ADR 0027's rule, applied to the one tree this feature creates outside `GRID_HOME`.

    `shared.paths.ensure_dir` cannot be reused — it refuses paths outside `GRID_HOME` — so the mode
    discipline is restated here rather than inherited. Every level the provider creates is checked,
    not just the leaf: a group-writable `projects/` lets a second account swap a whole project's
    workspace for a symlink before the agent ever starts.
    """
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_ROOT", str(tmp_path / "root"))
    path = task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION)

    task_agent.ensure_workspace(path)

    assert path.is_dir()
    walked = path
    while walked != tmp_path:
        assert not walked.stat().st_mode & 0o022, f"{walked} is group- or other-writable"
        walked = walked.parent


def test_ensure_workspace_is_idempotent(monkeypatch, tmp_path):
    """A project's second task reuses the first one's workspace — that is the point of the path."""
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_ROOT", str(tmp_path))
    path = task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION)
    task_agent.ensure_workspace(path)
    (path / "kept.txt").write_text("from the first task", encoding="utf-8")

    task_agent.ensure_workspace(path)

    assert (path / "kept.txt").read_text(encoding="utf-8") == "from the first task"


def test_a_workspace_that_cannot_be_created_says_where_and_why(monkeypatch, tmp_path):
    """`/var/grid` needs privileges the provider may not have, and that must read as a clear task
    failure rather than a bare `PermissionError` from somewhere deep in the loop."""
    from remote import task_agent

    blocked = tmp_path / "blocked"
    blocked.mkdir(mode=0o500)
    monkeypatch.setenv("GRID_TASK_ROOT", str(blocked / "root"))

    with pytest.raises(OSError) as excinfo:
        task_agent.ensure_workspace(task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION))

    assert str(blocked) in str(excinfo.value)


def test_the_agent_is_spawned_in_print_mode_with_a_machine_readable_stream(monkeypatch):
    """The flags this whole slice rests on, pinned as a wire contract with the binary.

    Verified against Claude Code 2.1.221: `--output-format stream-json` needs `--print`, and this
    repo's existing seat pairs it with `--verbose` (`shared/agent/seats/claude.py`). The prompt is an
    argv ELEMENT — nothing here reaches a shell, so a prompt containing `; rm -rf /` is just text.

    The last two are issue 22's, and they are a security boundary rather than a preference: `-p`
    SKIPS the workspace-trust dialog, so without them a `.claude/settings.json` in a workspace that
    arrived over the wire runs a shell command before the model has said anything. The proof that
    they WORK is a side effect, not this list — `tests/e2e_agent_settings.py` runs the real binary.
    """
    from remote import task_agent

    monkeypatch.delenv("GRID_TASK_PERMISSION_MODE", raising=False)

    argv = task_agent.agent_argv(
        "/usr/local/bin/claude", "fix the flaky test", workspace=Path("/var/grid/p/workspace"))

    # `--settings` carries a whole JSON policy, so it is checked by name and parsed rather than
    # compared as a literal — `tests/test_task_sandbox.py` owns what is inside it, and
    # `tests/e2e_agent_sandbox.py` owns whether it does anything.
    settings = argv[argv.index("--settings") + 1]
    assert json.loads(settings)["sandbox"]["enabled"] is True
    assert argv[:argv.index("--settings")] == [
        "/usr/local/bin/claude",
        "-p", "fix the flaky test",
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "acceptEdits",
        "--setting-sources", "user",
        "--strict-mcp-config",
    ]
    assert len(argv) == argv.index("--settings") + 2


def test_the_repositorys_settings_are_dropped_but_the_operators_own_are_kept(monkeypatch):
    """`user`, and never `none` — which half of this flag is load-bearing is easy to lose.

    The directory the agent runs in arrived over the wire; the operator's `CLAUDE_CONFIG_DIR` did
    not. Dropping every source would also drop the provider operator's own settings, which is not
    what this defends against and would silently change how every task on that provider behaves.
    Measured on 2.1.223: with `--setting-sources user`, a `SessionStart` hook in the config
    directory's own `settings.json` still runs while the workspace's does not.
    """
    from remote import task_agent

    argv = task_agent.agent_argv("claude", "x", workspace=Path("/var/grid/p/workspace"))

    assert argv[argv.index("--setting-sources") + 1] == "user"


def test_the_permission_mode_is_accept_edits_by_default_and_overridable_per_provider(monkeypatch):
    """`acceptEdits`, not `bypassPermissions` — and that is a measured requirement, not a posture.

    Print mode cannot answer a permission prompt, so the default has to be one that lets a task do
    work; `acceptEdits` plus the sandbox's `autoAllowBashIfSandboxed` is that combination, and a
    real build-and-test task was measured completing under it.

    The mode it replaces is not merely broader. Measured on 2.1.223: with `bypassPermissions` and
    the entire sandbox policy in force, the agent read a file outside the workspace without
    difficulty — the `Read` tool runs inside the Claude Code process, which the sandbox does not
    confine. See `test_bypass_permissions_is_refused_while_the_agent_is_confined`.
    """
    from remote import task_agent

    monkeypatch.delenv("GRID_TASK_PERMISSION_MODE", raising=False)
    assert task_agent.permission_mode() == "acceptEdits"

    monkeypatch.setenv("GRID_TASK_PERMISSION_MODE", "plan")

    # Read by NAME rather than off the end of the list. This assertion used to be `[-1]`, which was
    # true only for as long as `--permission-mode` happened to be the last flag — it stopped being
    # so the moment issue 22 appended two more, and a positional assertion that breaks on an
    # unrelated change is one somebody eventually "fixes" by re-pinning the index.
    argv = task_agent.agent_argv("claude", "x", workspace=Path("/var/grid/p/workspace"))
    assert argv[argv.index("--permission-mode") + 1] == "plan"


def test_bypass_permissions_is_refused_while_the_agent_is_confined(monkeypatch):
    """The one mode that makes the whole policy decoration, refused where it can be explained.

    Measured (2.1.223, macOS): `--permission-mode bypassPermissions` with `sandbox.enabled`,
    `denyRead` and `credentials.files` all set, asked for a file outside the workspace — and got it.
    The sandbox confines the commands the MODEL runs; the `Read` tool is run by the Claude Code
    process itself, and `bypassPermissions` is exactly the setting that stops the permission layer
    from refusing it.

    So this combination cannot be accepted quietly: it would leave a provider looking configured and
    confining nothing, which is the same failure as a deny rule with one slash. The operator either
    drops the mode or turns confinement off deliberately — and the message says both.
    """
    from remote import task_agent, task_sandbox

    monkeypatch.delenv(task_sandbox.SANDBOX_ENV, raising=False)
    monkeypatch.setenv("GRID_TASK_PERMISSION_MODE", "bypassPermissions")

    with pytest.raises(ValueError) as excinfo:
        task_agent.permission_mode()

    message = str(excinfo.value)
    assert "bypassPermissions" in message
    assert task_sandbox.SANDBOX_ENV in message, "the operator is not told how to proceed either way"


def test_bypass_permissions_is_still_available_to_a_provider_that_is_not_confined(monkeypatch):
    """Turning confinement off is a decision an operator is allowed to make, and then the old mode
    is the right one again — print mode still cannot answer a prompt."""
    from remote import task_agent, task_sandbox

    monkeypatch.setenv(task_sandbox.SANDBOX_ENV, "0")
    monkeypatch.setenv("GRID_TASK_PERMISSION_MODE", "bypassPermissions")

    assert task_agent.permission_mode() == "bypassPermissions"


def test_an_unknown_permission_mode_is_refused_rather_than_handed_to_the_binary(monkeypatch):
    """A typo'd mode is rejected HERE, where it can be explained.

    Handed through, the binary refuses it — and the provider reads that as "the agent failed",
    once per task, forever. The accepted set is the binary's own (`--permission-mode`, 2.1.221).
    """
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_PERMISSION_MODE", "bypassPermissons")  # missing an `i`

    with pytest.raises(ValueError) as excinfo:
        task_agent.agent_argv("claude", "x", workspace=Path("/var/grid/p/workspace"))

    assert "bypassPermissons" in str(excinfo.value)


def test_the_binary_is_found_through_the_shared_resolver(monkeypatch, tmp_path):
    """One answer to "where is Claude Code" per machine, not two.

    `shared/launch/claude_install` already searches PATH and both conventional install locations and
    reports what it could not check; a second search here would drift from it.

    A real (fake) executable rather than a made-up path, since issue 23: `resolve_binary` now also
    asks the binary its version, so a name that cannot be executed is no longer a resolution this
    function can complete.
    """
    from remote import task_agent

    binary, _ = _claude_reporting(tmp_path, "2.1.223 (Claude Code)", name="resolver-claude")
    _resolving_to(monkeypatch, binary)

    assert task_agent.resolve_binary() == binary


def test_a_provider_without_claude_code_says_how_to_install_it(monkeypatch):
    """A task that fails because the app is missing must say so, not "exited 127".

    The refusal names the vendor's own installer — the same line `grid launch claude` prints — so an
    operator reading one failed task knows the whole fix.
    """
    from shared.launch import claude_install

    from remote import task_agent

    monkeypatch.setattr(
        claude_install, "resolve",
        lambda: claude_install.Resolution(binary=None, unchecked=("~/.local/bin (unreadable)",)))

    with pytest.raises(RuntimeError) as excinfo:
        task_agent.resolve_binary()

    message = str(excinfo.value)
    assert claude_install.install_instruction() in message
    # An incomplete search must never read as a completed one — the same rule `_unchecked_note` keeps
    # on the launch path. Otherwise an operator whose `~/.local/bin` is unreadable is told the app is
    # not installed, and goes to install an app that is already there.
    assert "~/.local/bin (unreadable)" in message


def _claude_reporting(tmp_path, version_output, *, name="fake-claude"):
    """A stand-in binary whose `--version` says `version_output`, counting how often it is asked."""
    script = tmp_path / name
    calls = tmp_path / f"{name}-version-calls"
    script.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "--version" ]; then echo x >> {calls}; printf "%s\\n" \'{version_output}\'; '
        "exit 0; fi\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    return str(script), calls


def _resolving_to(monkeypatch, binary):
    from shared.launch import claude_install

    monkeypatch.setattr(
        claude_install, "resolve", lambda: claude_install.Resolution(binary=binary, unchecked=()))


def test_a_claude_too_old_for_the_sandbox_is_refused_instead_of_running_unconfined(
        monkeypatch, tmp_path):
    """`--settings` is the one flag in this argv that does NOT fail closed, so a version is checked.

    Issue 22's flags are safe to add unconditionally because a binary that does not know an option
    refuses the whole invocation — the provider fails loudly and nothing runs unprotected. That
    property does not extend here: `--settings` is a flag every version knows, carrying settings
    KEYS, and unknown keys are dropped in silence. A binary too old for `sandbox.*` would therefore
    accept the policy, ignore it, and report `completed` on every task with the agent unconfined and
    no signal anywhere saying so.

    So the version is the gate, and it names both numbers — an operator reading one failed task
    should not have to find out what "too old" meant.
    """
    from remote import task_agent

    binary, _ = _claude_reporting(tmp_path, "2.1.99 (Claude Code)")
    _resolving_to(monkeypatch, binary)

    with pytest.raises(RuntimeError) as excinfo:
        task_agent.resolve_binary()

    message = str(excinfo.value)
    assert "2.1.99" in message
    assert "2.1.221" in message


def test_a_claude_new_enough_is_used_and_asked_for_its_version_only_once(monkeypatch, tmp_path):
    """The gate runs on the task path, so it must not shell out once per task forever."""
    from remote import task_agent

    binary, calls = _claude_reporting(tmp_path, "2.1.223 (Claude Code)")
    _resolving_to(monkeypatch, binary)

    assert task_agent.resolve_binary() == binary
    assert task_agent.resolve_binary() == binary

    assert calls.read_text(encoding="utf-8").count("x") == 1


def test_the_cached_version_does_not_survive_the_binary_changing_underneath_it(
        monkeypatch, tmp_path):
    """A provider runs for weeks; Claude Code updates itself in place while it does.

    The cache above is what keeps the gate off the per-task hot path, and a cache keyed on the path
    alone would answer for a binary that is no longer there. The direction that matters is the
    dangerous one: a *downgrade* remembered as new enough would run unconfined for the life of the
    process, silently — which is the failure this whole gate exists to prevent.
    """
    from remote import task_agent

    binary, _ = _claude_reporting(tmp_path, "2.1.223 (Claude Code)", name="rolling-claude")
    _resolving_to(monkeypatch, binary)
    assert task_agent.resolve_binary() == binary

    _claude_reporting(tmp_path, "2.0.9 (Claude Code)", name="rolling-claude")

    with pytest.raises(RuntimeError) as excinfo:
        task_agent.resolve_binary()
    assert "2.0.9" in str(excinfo.value)


def test_a_version_that_cannot_be_read_is_refused_rather_than_assumed_good(monkeypatch, tmp_path):
    """A third-party wrapper on `PATH` is a supported install (`claude_install.resolve` says so
    deliberately), and one that does not answer `--version` in the vendor's shape cannot be assumed
    to honour the policy. Failing closed here is loud and fixable; failing open is neither.
    """
    from remote import task_agent

    binary, _ = _claude_reporting(tmp_path, "a wrapper, not a version")
    _resolving_to(monkeypatch, binary)

    with pytest.raises(RuntimeError) as excinfo:
        task_agent.resolve_binary()

    assert "a wrapper, not a version" in str(excinfo.value)
    assert binary in str(excinfo.value)


def test_the_version_gate_only_applies_while_the_agent_is_confined(monkeypatch, tmp_path):
    """The gate exists to protect a control that fails open. Turn the control off deliberately and
    the gate has nothing to protect — an old binary is then the operator's own decision, and the
    provider goes back to behaving exactly as it did before issue 23."""
    from remote import task_agent, task_sandbox

    binary, _ = _claude_reporting(tmp_path, "2.0.1 (Claude Code)")
    _resolving_to(monkeypatch, binary)
    monkeypatch.setenv(task_sandbox.SANDBOX_ENV, "0")

    assert task_agent.resolve_binary() == binary


def test_the_child_environment_is_an_allowlist_not_the_providers_whole_environment(monkeypatch):
    """Issue 23 layer 1. The agent gets what it needs to run, not what this process happens to hold.

    This test used to assert the OPPOSITE — that an arbitrary marker variable reached the child —
    because `child_env` was `dict(os.environ)`. That is the rule being overturned: the process this
    copies from also serves inference and holds the grid access token, and a task is arbitrary code
    execution by another person. `env` in a task prompt should not be a credential dump.

    An allowlist rather than a deny list, for the reason `_SAFE_PROJECT_ID` gives: a deny list has
    to anticipate every name worth hiding, and the next provider integration invents one.
    """
    from remote import task_agent

    monkeypatch.delenv("GRID_TASK_CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI")
    monkeypatch.setenv("GRID_TOKEN", "eyJhbGciOiJSUzI1NiJ9")
    monkeypatch.setenv("A_MARKER_THE_PROVIDER_SET", "dropped")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    env = task_agent.child_env()

    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "GRID_TOKEN" not in env, "the grid's own bearer token reached the task agent"
    assert "A_MARKER_THE_PROVIDER_SET" not in env
    # What a program needs to run at all still arrives, or nothing works and the allowlist is a
    # different bug wearing the same clothes.
    assert env["PATH"] == "/usr/bin:/bin"
    assert "HOME" in env


def test_the_operator_can_declare_the_extra_variables_a_task_needs(monkeypatch):
    """An allowlist that cannot be extended is an allowlist an operator works around.

    A provider serving a team with a private package registry needs its token in the build; the
    supported answer is to name it, not to give up and go back to handing the agent everything. The
    variable holding the list is itself withheld — the child has no use for grid's own configuration,
    and this is the one name we know for certain reveals what a provider considers sensitive.
    """
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_ENV_PASSTHROUGH", "MY_REGISTRY_TOKEN, ABSENT_ON_THIS_BOX")
    monkeypatch.setenv("MY_REGISTRY_TOKEN", "npm-tok")

    env = task_agent.child_env()

    assert env["MY_REGISTRY_TOKEN"] == "npm-tok"
    # A name the operator listed but never set is an ordinary state, not an error: the same
    # provider configuration is deployed to boxes that do not all have the same tooling.
    assert "ABSENT_ON_THIS_BOX" not in env
    assert "GRID_TASK_ENV_PASSTHROUGH" not in env


def test_a_passthrough_entry_that_is_not_a_variable_name_is_refused(monkeypatch):
    """`FOO=bar` is the mistake this list invites, and it must be explained once, not forever.

    Accepted silently it matches nothing, so the variable the operator meant to pass never arrives —
    and the symptom is a build failing inside a task on every provider, with the actual cause sitting
    in a provider's service file. The same rule `permission_mode` applies to a typo'd mode.
    """
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_ENV_PASSTHROUGH", "GOOD_ONE, FOO=bar")

    with pytest.raises(ValueError) as excinfo:
        task_agent.child_env()

    assert "FOO=bar" in str(excinfo.value)
    assert task_agent.ENV_PASSTHROUGH_ENV in str(excinfo.value)


def test_an_ambient_claude_config_dir_never_reaches_the_child(monkeypatch):
    """The one variable that must come from `configured_claude_config_dir()` or not at all.

    An operator with `CLAUDE_CONFIG_DIR` exported in their own shell would otherwise send the child
    somewhere `claude_config_dir()` knows nothing about — so `link_transcript` plants the symlink in
    one directory while the agent writes its transcript in another. Nothing fails: the task
    completes, the transcript is simply not in the repository, and every following task on the
    project starts a fresh conversation while every other signal looks healthy. Exactly the shape of
    issue 06's bug, reached from the provider's own environment.
    """
    from remote import task_agent

    monkeypatch.delenv("GRID_TASK_CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/home/operator/.claude-personal")
    # Listed by the operator, which is the route that actually reaches the guard: the passthrough is
    # applied AFTER the allowlist and does not consult it, so an operator naming this variable —
    # reasonably, having read that it configures the agent — would silently break every conversation
    # on the project. Without this line the test passes on the allowlist alone and proves nothing.
    monkeypatch.setenv("GRID_TASK_ENV_PASSTHROUGH", "CLAUDE_CONFIG_DIR")

    env = task_agent.child_env()

    assert "CLAUDE_CONFIG_DIR" not in env
    # And when the operator DID fix one, that is the value — the existing contract, restated here so
    # the exclusion above can never be "simplified" into dropping it in both cases.
    monkeypatch.setenv("GRID_TASK_CLAUDE_CONFIG_DIR", "/etc/grid/claude")
    assert task_agent.child_env()["CLAUDE_CONFIG_DIR"] == "/etc/grid/claude"


def test_the_operators_own_git_configuration_does_not_reach_a_tree_from_the_wire(monkeypatch):
    """ADR 0033 D-f, named there and not closed until here.

    The provider's own git calls are hardened per invocation (`task_repo._env`:
    `-c core.symlinks=false -c core.hooksPath=`). The AGENT's are not — it runs `git` itself, in a
    checkout that arrived over the wire, with the operator's real `HOME`. So a `core.hooksPath` in
    the operator's `~/.gitconfig` applies to that tree, and `git commit` there runs whatever it
    points at. `HOME` cannot simply be withheld (Claude Code's credential lives under it), so the
    floor is set on git's own variables instead.

    Measured: `git` tolerates an unreadable global config — warns, exits 0 — so this is about which
    configuration applies, not about making git work. It does **not** make an agent's commit fail
    for want of `user.name`: re-measured on git 2.54.0, git auto-detects `<user>@<hostname>` and
    exits 0, which is why `_git_identity` forces the identity separately (issue 21).
    """
    import os as _os

    from remote import task_agent

    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/home/operator/.gitconfig")

    env = task_agent.child_env()

    assert env["GIT_CONFIG_GLOBAL"] == _os.devnull
    assert env["GIT_CONFIG_SYSTEM"] == _os.devnull


def test_the_git_safety_floor_outranks_an_operators_passthrough_list(monkeypatch):
    """The floor is applied after the passthrough, and a miscount is the failure that hides.

    `GIT_CONFIG_COUNT` is not a setting, it is how many settings follow — so an inherited or
    passed-through `GIT_CONFIG_COUNT=0` leaves `GIT_CONFIG_KEY_0`/`VALUE_0` sitting in the
    environment being read by nobody. Git does not complain; the workspace simply goes back to
    materializing symlinks, on one provider, with every other signal healthy. That is why the count
    is derived from the tuple and why the floor is written last.
    """
    from remote import task_agent, task_repo

    monkeypatch.setenv("GRID_TASK_ENV_PASSTHROUGH",
                       "GIT_CONFIG_COUNT, GIT_CONFIG_KEY_0, GIT_CONFIG_VALUE_0")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "0")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.symlinks")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")

    env = task_agent.child_env()

    assert env["GIT_CONFIG_COUNT"] == str(len(task_repo.GIT_SAFETY_CONFIG))
    settings = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
                for i in range(int(env["GIT_CONFIG_COUNT"]))}
    assert settings == dict(task_repo.GIT_SAFETY_CONFIG)


def _bare_repo_holding_a_symlink(tmp_path, branch):
    """A real bare repo whose `branch` carries `docs/README -> ../README.md`. `(url, commit)`.

    An INSIDE link on purpose: import refuses the escaping kind outright, so the object that
    actually reaches a workspace is this one, and this is the object the provider has to keep from
    becoming a link.
    """
    import os
    import subprocess

    def git(cwd, *args):
        return subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
            env={"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_NOSYSTEM": "1",
                 "GIT_CONFIG_GLOBAL": os.devnull, "HOME": "/nonexistent",
                 "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@invalid",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@invalid"})

    seed = tmp_path / "seed-symlink"
    (seed / "docs").mkdir(parents=True)
    git(tmp_path, "init", "-q", "-b", branch, str(seed))
    (seed / "README.md").write_text("hello\n")
    os.symlink("../README.md", seed / "docs" / "README")
    git(seed, "add", "-A")
    assert "120000" in git(seed, "ls-files", "-s").stdout, (
        "the fixture did not record a symlink, so this test would pass on an empty tree")
    git(seed, "commit", "-q", "-m", "imported")
    bare = tmp_path / "symlink-origin.git"
    git(tmp_path, "clone", "--bare", "-q", str(seed), str(bare))
    return str(bare), git(bare, "rev-parse", branch).stdout.strip()


def test_an_imported_symlink_does_not_materialize_under_the_agents_own_git(tmp_path):
    """ADR 0033 D-f, the provider half of import (issue 16b) — the layer that was missing.

    `task_repo._run` hardens the PROVIDER's git with `-c core.symlinks=false` on every invocation.
    The agent's own git calls got none of it, and since issue 15 a merge task's prompt *requires*
    the agent to run git in a checkout that arrived over the wire. So a `120000` object — one the
    import validator deliberately ALLOWS, because refusing every symlink rejects ordinary
    repositories — became a real link the moment the agent touched git, and the next `git add -A`
    followed it.

    Run against real git with `child_env()` and nothing else, because that is the whole of what the
    agent gets. The control below runs the identical checkout with the floor stripped out: without
    it this asserts nothing more than "git wrote a regular file", which it might do for any reason.

    ⚠️ **What this does NOT claim.** Measured on git 2.54.0: `-c core.symlinks=true` on the agent's
    own command line beats both the environment floor and the repository's config, so neither layer
    stops an agent that has decided to make a link. They close the ACCIDENTAL path — an ordinary
    `git merge` or `git checkout` materializing what the packfile carried — which is the path import
    actually creates. Against a deliberate agent the validator is the only layer, exactly as D-f
    says.
    """
    import subprocess

    from remote import task_agent, task_repo

    url, commit = _bare_repo_holding_a_symlink(tmp_path, "task/T1")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task_repo.materialize(workspace, url=url, token="", branch="task/T1", input_commit=commit)

    link = workspace / "docs" / "README"

    def checkout_as_the_agent(env):
        link.unlink()
        subprocess.run(["git", "checkout", "--", "."], cwd=str(workspace),
                       capture_output=True, text=True, check=True, env=env)

    checkout_as_the_agent(task_agent.child_env())
    assert not link.is_symlink(), (
        "the agent's own git re-created an imported 120000 object as a real symlink")
    assert link.read_text() == "../README.md", "the link's target should be the file's content"

    # The control. Same tree, same command, the floor removed — and now it IS a link, which is what
    # makes the assertion above evidence rather than a coincidence.
    unfloored = {k: v for k, v in task_agent.child_env().items()
                 if not k.startswith("GIT_CONFIG_")}
    subprocess.run(["git", "config", "--local", "--unset-all", "core.symlinks"],
                   cwd=str(workspace), capture_output=True, check=False)
    checkout_as_the_agent(unfloored)
    assert link.is_symlink(), (
        "without the floor git did not create a symlink either, so this test proves nothing")


def test_the_workspaces_own_config_holds_for_a_git_that_gets_none_of_grids_environment(tmp_path):
    """The second half of D-f's floor, and what it is actually worth.

    ADR 0033 asks for `core.symlinks=false` in the workspace's own config *as well as* on each
    invocation. The reason given — "env can be overridden by an agent that decides to" — turns out
    not to be the reason, measured: an agent's `-c core.symlinks=true` beats the repository's config
    just as easily as it beats the environment, and rewriting the repository's config does not beat
    the environment at all.

    What this layer does buy is the case the environment cannot reach: **a git that was not started
    from `child_env`.** An operator shelling into a workspace to look at a failed task, a tool the
    agent installed that re-execs without the environment, a future refactor that reorders the
    floor. The config travels with the directory, so it holds for all of them.

    Written on EVERY call, beside `info/exclude` and for the same stated reason: a persistent
    workspace's config is something a previous run could have changed.
    """
    import subprocess

    from remote import task_repo

    url, commit = _bare_repo_holding_a_symlink(tmp_path, "task/T1")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task_repo.materialize(workspace, url=url, token="", branch="task/T1", input_commit=commit)

    link = workspace / "docs" / "README"
    link.unlink()
    # No grid environment whatsoever — not the floor, not the identity, nothing. Only `PATH`, or git
    # cannot be found at all.
    import os
    bare_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path / "home")}
    subprocess.run(["git", "checkout", "--", "."], cwd=str(workspace),
                   capture_output=True, text=True, check=True, env=bare_env)

    assert not link.is_symlink(), (
        "a git run without grid's environment re-created the imported 120000 object as a link")

    for key, value in task_repo.GIT_SAFETY_CONFIG:
        got = subprocess.run(["git", "config", "--local", "--get", key], cwd=str(workspace),
                             capture_output=True, text=True)
        assert got.stdout.strip() == value, f"{key} is not in the workspace's own config"


def test_the_cli_process_environment_is_untouched(monkeypatch):
    """ADR 0028's rule: whatever we set is set on the CHILD, never exported anywhere."""
    from remote import task_agent

    monkeypatch.delenv("GRID_TASK_CLAUDE_CONFIG_DIR", raising=False)
    before = dict(__import__("os").environ)

    env = task_agent.child_env()

    assert env is not __import__("os").environ
    assert dict(__import__("os").environ) == before


def test_a_fixed_config_dir_reaches_the_child_only(monkeypatch):
    """`CLAUDE_CONFIG_DIR` is fixed PER PROVIDER, never per user (ADR 0032).

    The spike measured that a per-user config directory yields `Not logged in` even on macOS, where
    the token lives in the Keychain — it demands its own credential material, which a per-user
    directory would then carry into the repo it is synced through.
    """
    import os

    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_CLAUDE_CONFIG_DIR", "/etc/grid/claude")

    env = task_agent.child_env()

    assert env["CLAUDE_CONFIG_DIR"] == "/etc/grid/claude"
    assert "CLAUDE_CONFIG_DIR" not in os.environ


# --------------------------------------------------------------------------------------------
# remote/task_stream.py — stream-json records become task events
# --------------------------------------------------------------------------------------------


def _line(record):
    import json

    return json.dumps(record)


def test_the_session_id_is_captured_from_the_opening_record(monkeypatch):
    """`system`/`init` carries the session id, and it is the state issue 06 resumes from.

    Captured at the START rather than only from the terminal `result`: a child that is killed
    mid-run never writes a `result`, and the session it opened is exactly what a retry wants.
    """
    from remote import task_stream

    translator = task_stream.StreamTranslator()

    events = translator.feed(_line({
        "type": "system", "subtype": "init", "session_id": "012c9e09-abcd", "cwd": "/var/grid/x"}))

    assert events == [("task.session", {"session_id": "012c9e09-abcd"})]
    assert translator.session_id == "012c9e09-abcd"


def test_a_tool_call_becomes_its_name_and_its_target_path():
    """Issue 03's headline acceptance criterion: the client sees WHAT the agent is touching.

    Translated, not forwarded. The record's `input` is the tool's whole argument object — an `Edit`
    carries the full old and new text — and forwarding it would blow the relay's 64 KiB per-event
    cap on the very events a user most wants. Name and path are what render.
    """
    from remote import task_stream

    translator = task_stream.StreamTranslator()

    events = translator.feed(_line({
        "type": "assistant",
        "message": {"content": [{
            "type": "tool_use", "id": "toolu_1", "name": "Edit",
            "input": {"file_path": "/var/grid/projects/p/workspace/app.py",
                      "old_string": "x" * 90_000, "new_string": "y" * 90_000},
        }]},
    }))

    assert events == [("task.tool_use", {
        "tool": "Edit", "path": "/var/grid/projects/p/workspace/app.py", "id": "toolu_1"})]


def test_a_tool_with_no_path_still_reports_its_name():
    """Not every tool targets a file. `Bash` and `WebSearch` still say that something is happening,
    and a stream that went quiet during a ten-minute test run would read as a hang."""
    from remote import task_stream

    events = task_stream.StreamTranslator().feed(_line({
        "type": "assistant",
        "message": {"content": [{
            "type": "tool_use", "id": "toolu_2", "name": "Bash",
            "input": {"command": "pytest -q"}}]},
    }))

    assert events == [("task.tool_use", {"tool": "Bash", "path": None, "id": "toolu_2"})]


def test_assistant_text_arrives_as_output():
    """What the agent SAYS is the other half of watching it work — and `task.output` is the type the
    client already renders as a bare line (`cli/remote_task._render`)."""
    from remote import task_stream

    events = task_stream.StreamTranslator().feed(_line({
        "type": "assistant",
        "message": {"content": [
            {"type": "text", "text": "Looking at the failing test first."},
            {"type": "tool_use", "id": "t", "name": "Read", "input": {"file_path": "/w/t.py"}},
        ]},
    }))

    assert events == [
        ("task.output", {"text": "Looking at the failing test first."}),
        ("task.tool_use", {"tool": "Read", "path": "/w/t.py", "id": "t"}),
    ]


def test_a_plain_text_line_is_shown_rather_than_treated_as_a_fault():
    """The CLI prints plain-text notices alongside its events (`shared/agent/seats/claude._json_lines`
    records the same). Dropping them would hide the one line explaining why a task did nothing."""
    from remote import task_stream

    events = task_stream.StreamTranslator().feed("Warning: your subscription resets in 2 minutes\n")

    assert events == [("task.output", {"text": "Warning: your subscription resets in 2 minutes"})]


def test_a_blank_line_produces_nothing():
    from remote import task_stream

    assert task_stream.StreamTranslator().feed("\n") == []
    assert task_stream.StreamTranslator().feed("   ") == []


def test_a_line_that_blows_the_recursion_limit_is_handled_like_any_other_junk():
    """`json.loads` raises `RecursionError` on deep nesting — which is NOT a `ValueError`.

    A guard naming only `ValueError` has a hole no malformed-input parametrize finds, and the fault
    would escape into the thread running the child and end the task.
    """
    from remote import task_stream

    deep = "[" * 200_000 + "]" * 200_000

    events = task_stream.StreamTranslator().feed(deep)

    assert len(events) == 1
    assert events[0][0] == "task.output"


@pytest.mark.parametrize("payload", ['123', '"a string"', '[1, 2, 3]', 'null', 'true'])
def test_valid_json_that_is_not_a_record_is_treated_as_text(payload):
    """`json.loads` succeeds on all of these and returns something with no `.get`. Calling one would
    raise `AttributeError` inside the translator, which is the same lost task as a parse failure."""
    from remote import task_stream

    events = task_stream.StreamTranslator().feed(payload)

    assert events == [("task.output", {"text": payload})]


def test_one_enormous_line_is_bounded_to_fit_inside_an_event():
    """The relay refuses any event over `MAX_EVENT_BYTES`, and a refused batch takes the events
    around it with it. Bounded HERE, because a publisher that keeps resending an event the relay can
    never accept has silently stopped narrating."""
    import json

    from remote import task_events, task_stream

    events = task_stream.StreamTranslator().feed("z" * 500_000)

    (_type, fields), = events
    assert len(json.dumps({"type": "task.output", **fields}).encode()) <= task_events.MAX_EVENT_BYTES
    assert fields["text"].endswith("… [truncated]")


@pytest.mark.parametrize("secret", [
    "sk-ant-oat01-AbCdEf0123456789-_xyz",   # the OAuth token a subscription provider holds
    "sk-ant-api03-AbCdEf0123456789",        # a metered API key
])
def test_a_credential_in_the_stream_never_reaches_a_published_event(secret):
    """Issue 03's security criterion, made testable rather than hoped for.

    The provider's own credential is what runs the agent, and the agent is executing a prompt a
    stranger wrote. `cat ~/.claude/.credentials.json` is a legal thing for a task to ask, and its
    output comes straight back down this stream — into an event log the requesting user reads.
    """
    from remote import task_stream

    events = task_stream.StreamTranslator().feed(_line({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": f"the token is {secret} apparently"}]},
    }))

    (_type, fields), = events
    assert secret not in fields["text"]
    assert "sk-ant-***" in fields["text"]


def test_a_bearer_header_echoed_by_the_agent_is_redacted_too():
    """The other shape a credential takes on its way through a terminal — an `Authorization` header
    in a `curl` the agent ran, or in an error the vendor's API returned."""
    from remote import task_stream

    events = task_stream.StreamTranslator().feed(
        "curl -H 'Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.aaaaaaaa.bbbbbbbb' https://x")

    (_type, fields), = events
    assert "eyJhbGciOiJIUzI1NiJ9" not in fields["text"]
    assert "Bearer ***" in fields["text"]


def test_redaction_reaches_every_string_in_every_event_type():
    """Applied to what LEAVES the translator, not at each construction site.

    A per-site rule is one a future event type forgets, and forgetting is silent: the event
    publishes, the client renders it, and the credential is in a durable log on the relay.
    """
    from remote import task_stream

    events = task_stream.StreamTranslator().feed(_line({
        "type": "assistant",
        "message": {"content": [{
            "type": "tool_use", "id": "sk-ant-api03-0123456789abcdef", "name": "Read",
            "input": {"file_path": "/w/sk-ant-oat01-0123456789abcdef.txt"}}]},
    }))

    (_type, fields), = events
    assert "0123456789abcdef" not in repr(fields)


def test_the_result_record_carries_the_answer_and_how_the_run_ended():
    """The task's `output` is the agent's final message, not the raw stream.

    Dumping every record into `result_text` would hand a user a megabyte of JSON to answer "what
    happened"; the `result` record already holds the summary the agent wrote.

    No `task.terminal` is emitted here — that event is the RELAY's, written inside the same
    transaction as the terminal state change (issue 02), precisely so it cannot be lost.
    """
    from remote import task_stream

    translator = task_stream.StreamTranslator()

    events = translator.feed(_line({
        "type": "result", "subtype": "success", "is_error": False,
        "duration_ms": 42_000, "num_turns": 7, "session_id": "sess-9",
        "result": "Fixed the flaky test by seeding the RNG.",
        "total_cost_usd": 0.31,
    }))

    assert events == [("task.result", {
        "subtype": "success", "is_error": False, "num_turns": 7, "duration_ms": 42_000})]
    assert translator.result_text == "Fixed the flaky test by seeding the RNG."
    assert translator.is_error is False
    assert translator.subtype == "success"
    # Also the session id: a run whose `system/init` was lost to a torn line still has one to store.
    assert translator.session_id == "sess-9"


def test_an_agent_that_reports_its_own_failure_is_believed():
    """`is_error` is the agent saying it did not finish — `error_max_turns`, `error_during_execution`.

    Trusting THAT is not the same as trusting a success claim: the exit status stays the authority
    for "it worked", and this only ever makes a run fail that would otherwise have passed.
    """
    from remote import task_stream

    translator = task_stream.StreamTranslator()

    translator.feed(_line({
        "type": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": 40}))

    assert translator.is_error is True
    assert translator.subtype == "error_max_turns"


def test_the_final_answer_is_redacted_too():
    """`result_text` becomes the task's stored `output` on the relay — a durable record the
    requesting user reads. It leaves this module by a different door than the events, so it needs
    the scrub applied on its own."""
    from remote import task_stream

    translator = task_stream.StreamTranslator()

    translator.feed(_line({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "I found sk-ant-oat01-AbCdEf0123456789 in the config."}))

    assert "sk-ant-oat01" not in translator.result_text
    assert "sk-ant-***" in translator.result_text


def test_a_tool_finishing_is_reported_without_its_payload():
    """A `user` record is a tool RESULT, and its content is whatever the tool produced — a whole file
    for a `Read`. Only the fact and the outcome travel."""
    from remote import task_stream

    events = task_stream.StreamTranslator().feed(_line({
        "type": "user",
        "message": {"content": [{
            "type": "tool_result", "tool_use_id": "toolu_1", "is_error": False,
            "content": "x" * 200_000}]},
    }))

    assert events == [("task.tool_result", {"id": "toolu_1", "is_error": False})]


@pytest.mark.parametrize("record", [
    {"type": "stream_event", "event": {"type": "content_block_delta"}},
    {"type": "something_a_future_version_added"},
    {"no_type_at_all": 1},
])
def test_a_record_this_build_does_not_know_is_ignored_rather_than_guessed_at(record):
    """Ignoring is what makes a new record type free to arrive: an old provider narrates a little
    less, and nothing breaks."""
    from remote import task_stream

    assert task_stream.StreamTranslator().feed(_line(record)) == []


# --- issue 09: the provider reads its own subscription's pressure -------------------------------


_RATE_LIMIT = {
    "type": "rate_limit_event",
    "rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour",
                        "resetsAt": 1785832800, "overageStatus": "rejected",
                        "isUsingOverage": False},
}


def test_the_subscriptions_own_pressure_reaches_the_provider_and_the_client():
    """The signal the agent emits for free is the one the provider throttles on, so it goes two
    places at once: to whatever is deciding whether to claim another task, and into the task's log —
    a user whose task sat queued is owed the reason."""
    from remote import task_stream

    seen = []
    translator = task_stream.StreamTranslator(on_rate_limit=seen.append)

    events = translator.feed(_line(_RATE_LIMIT))

    assert seen == [_RATE_LIMIT["rate_limit_info"]]
    assert events == [("task.rate_limit", {"status": "rejected", "limit_type": "five_hour",
                                           "resets_at": 1785832800})]


def test_a_rate_limit_record_still_narrates_when_nobody_is_listening():
    """The hook is optional — `run_task` is callable without a gate wired, and was before this
    slice existed."""
    from remote import task_stream

    assert task_stream.StreamTranslator().feed(_line(_RATE_LIMIT)) == [
        ("task.rate_limit", {"status": "rejected", "limit_type": "five_hour",
                             "resets_at": 1785832800})]


def test_a_gate_that_raises_never_costs_the_task_its_run(capsys):
    """`feed` is documented never to raise and is called from the loop running the child. A capacity
    gate is an observer of that run; a fault in one may not end it."""
    from remote import task_stream

    def boom(_info):
        raise RuntimeError("gate exploded")

    translator = task_stream.StreamTranslator(on_rate_limit=boom)

    events = translator.feed(_line(_RATE_LIMIT))       # must not raise

    assert events[0][0] == "task.rate_limit"           # the client is narrated regardless
    assert "gate exploded" in capsys.readouterr().err


def test_a_broken_gate_is_reported_once_per_run_not_once_per_record(capsys):
    """`rate_limit_event` arrives with every turn of the agent's output, so a warning per record
    would bury the one line that matters under its own repetition — the lesson `_Reporter._complain`
    already records for the identical "documented never to raise" situation."""
    from remote import task_stream

    def boom(_info):
        raise RuntimeError("gate exploded")

    translator = task_stream.StreamTranslator(on_rate_limit=boom)

    for _ in range(6):
        translator.feed(_line(_RATE_LIMIT))

    assert capsys.readouterr().err.count("gate exploded") == 1


@pytest.mark.parametrize("info", [None, [], "rejected", 7, {}])
def test_a_rate_limit_record_with_no_readable_body_is_not_guessed_at(info):
    """The gate itself decides what an unreadable payload means (it keeps serving). What must not
    happen here is this translator inventing a field, or raising on a shape it did not expect."""
    from remote import task_stream

    seen = []
    translator = task_stream.StreamTranslator(on_rate_limit=seen.append)

    events = translator.feed(_line({"type": "rate_limit_event", "rate_limit_info": info}))

    assert seen == [info]                              # handed on verbatim; reading it is not our job
    assert events == [("task.rate_limit", {"status": None, "limit_type": None, "resets_at": None})]


# --------------------------------------------------------------------------------------------
# remote/tasks.run_task — the agent child, end to end
# --------------------------------------------------------------------------------------------


def _fake_claude(tmp_path, body):
    """A stand-in for the binary that emits real stream-json.

    A fake rather than the real `claude`: this exercises the argv, the pump, the deadline and the
    translator against an actual child process, without spending a subscription's quota on every
    test run.
    """
    script = tmp_path / "fake-claude"
    script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    script.chmod(0o755)
    return str(script)


@pytest.fixture
def agent(monkeypatch, tmp_path, short_task_root):
    """Point `run_task` at a fake binary and a writable workspace root.

    ⚠️ **The root is `short_task_root`, not `tmp_path`, and that is a correctness requirement rather
    than tidiness.** `tmp_path` is ~96 characters before grid adds the 126 of its own that ADR 0034
    D-c's path costs, and Claude Code stops using a transcript directory name verbatim past ~200
    (measured — see `task_agent.TRANSCRIPT_NAME_MAX_CHARS`), which `link_transcript` now refuses.
    Under `tmp_path` every test that spawns an agent would fail on a limit no real deployment meets:
    `/var/grid` flattens to 135. A short root is the production shape.
    """
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_ROOT", str(short_task_root))
    monkeypatch.delenv("GRID_TASK_PERMISSION_MODE", raising=False)
    # The real default is an hour. A test that wedges must fail in seconds, not hold the suite for
    # one — the deadline's own behaviour is pinned separately, by a test that sets it deliberately.
    monkeypatch.setenv("GRID_TASK_TIMEOUT_SECONDS", "20")

    def _install(body):
        binary = _fake_claude(tmp_path, body)
        monkeypatch.setattr(task_agent, "resolve_binary", lambda: binary)
        return binary

    return _install


def _job(**overrides):
    job = {"task_id": "T1", "project_id": "proj-1", "member_key": _MEMBER,
           # The CONVERSATION this turn continues (ADR 0034 D-c). `task_id` above is the TURN; two
           # keys for two objects, and `run_task` refuses a claim without this one.
           "conversation_id": _CONVERSATION,
           "prompt": "fix the flaky test", "attempt": 1}
    job.update(overrides)
    return job


def test_a_task_runs_the_agent_in_its_projects_workspace(agent, tmp_path):
    """The whole point of the fixed path: the agent's cwd is the project's workspace, created for it.

    Claude Code derives its transcript directory from the cwd, so this is also what makes issue 06's
    cross-provider `--resume` possible at all.
    """
    from remote import task_agent, tasks

    agent(
        "printf '{\"type\":\"system\",\"subtype\":\"init\",\"session_id\":\"sess-1\"}\\n'\n"
        "printf '{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,"
        "\"result\":\"%s\"}\\n' \"$(pwd)\"\n"
    )

    outcome = tasks.run_task(_job())

    assert outcome.state == "completed"
    assert outcome.error is None
    assert outcome.output == str(task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION))
    assert outcome.session_id == "sess-1"


def test_the_prompt_and_the_streaming_flags_reach_the_binary(agent, tmp_path):
    """Pinned through a real spawn, not by inspecting a list: a flag the binary never receives is
    the failure this catches, and only the child can report what it was actually given.

    argv goes to a FILE rather than out through the result record it used to use. Since issue 23 it
    carries `--settings` with a JSON policy, and echoing that back inside a JSON string produced an
    unparseable line — the test then failed on a `None` output, which says nothing about flags.
    """
    from remote import tasks

    seen = tmp_path / "argv.txt"
    agent(f'printf "%s" "$*" > {seen}\n'
          "printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")

    outcome = tasks.run_task(_job(prompt="fix the flaky test"))

    assert outcome.state == "completed", outcome.error
    argv = seen.read_text(encoding="utf-8")
    assert argv.startswith(
        "-p fix the flaky test --output-format stream-json --verbose "
        "--permission-mode acceptEdits --setting-sources user --strict-mcp-config")
    # And the confinement policy went with it — through the real spawn, so this covers the wiring
    # that a list-inspecting test cannot: `--settings` present, and carrying a sandbox that is on.
    assert '--settings {"sandbox":{"enabled":true' in argv


@pytest.mark.parametrize("variable, value, must_name", [
    # Each of `preflight()`'s three checks, through the door an operator actually misconfigures.
    ("GRID_TASK_PERMISSION_MODE", "bypassPermissions", "GRID_TASK_SANDBOX"),
    ("GRID_TASK_ENV_PASSTHROUGH", "FOO=bar", "GRID_TASK_ENV_PASSTHROUGH"),
    ("HOME", ".", "absolute"),
])
def test_a_misconfigured_provider_fails_before_it_fetches_anything(
        agent, monkeypatch, tmp_path, variable, value, must_name):
    """The property `preflight()` exists for, asserted on the fetch rather than on the message.

    All three of these are checked anyway by the functions that need them — but those run from
    `agent_argv` and `child_env`, AFTER the checkout and outside `run_task`'s guards. So without the
    pre-flight the operator's reward for a typo is a repository fetched into a workspace and then a
    raise about the task runner, once per task, forever.

    **The spy is the point.** An earlier version of this test asserted only the outcome and the
    message, and passed a job with no `input_commit` — so no checkout would have happened either
    way and the "before it fetches anything" in its name was decoration. Watching `materialize` is
    what makes it evidence: drop `task_agent.preflight()` from `run_task` and these go red, which is
    the regression that would otherwise reintroduce a silently mistargeted sandbox (`HOME=.`) with
    every unit test still green.
    """
    from remote import task_repo, tasks

    agent("printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")
    fetched: list[str] = []
    monkeypatch.setattr(task_repo, "materialize",
                        lambda *args, **kwargs: fetched.append("fetched"))
    monkeypatch.setenv(variable, value)

    outcome = tasks.run_task(
        _job(input_commit="0" * 40, branch="task/T1"),
        remote=task_repo.GitRemote(url="file:///nowhere", token="tok"))

    assert outcome.state == "failed"
    assert "could not start the agent" in outcome.error, (
        f"the refusal arrived from somewhere that cannot name the fix: {outcome.error!r}")
    assert must_name in outcome.error, (
        f"the failure does not say what to change: {outcome.error!r}")
    assert not fetched, (
        "the provider fetched the repository before noticing its own configuration was broken")


def test_the_agent_child_cannot_read_the_providers_own_stdin(agent, tmp_path):
    """Whatever is on the provider's stdin is not part of the task (ADR 0033 D-n).

    `claude -p` reads a non-TTY stdin as ADDITIONAL PROMPT INPUT — measured the hard way while
    taking this issue's other measurements: a heredoc feeding the harness turned up inside the
    agent's answer, and the agent acted on it. So a provider started by a supervisor that leaves a
    pipe on fd 0 hands its contents to every task it runs, mixed into a prompt written by somebody
    else, with no record anywhere that it happened.

    Tested by putting a real pipe on this process's own fd 0, because that is what a child inherits
    — `sys.stdin` is a Python object and a subprocess never sees it.
    """
    import os as _os

    from remote import tasks

    seen = tmp_path / "stdin.txt"
    read_fd, write_fd = _os.pipe()
    _os.write(write_fd, b"SECRET-ON-THE-PROVIDERS-STDIN\n")
    _os.close(write_fd)
    saved = _os.dup(0)
    try:
        _os.dup2(read_fd, 0)
        _os.close(read_fd)
        agent(f'cat > {seen}\n'
              "printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")
        outcome = tasks.run_task(_job())
    finally:
        _os.dup2(saved, 0)
        _os.close(saved)

    assert outcome.state == "completed", outcome.error
    assert seen.read_text(encoding="utf-8") == "", (
        "the agent read the provider's stdin, which `claude -p` treats as more prompt")


def test_a_non_zero_exit_is_a_failure_however_cheerful_the_stream_was(agent):
    """The exit status is the authority, not anything the agent said about itself (ADR 0032 D-e).

    The stream here ends with a textbook `result/success`; the process then exits 3. Believing the
    stream would report `completed` for a run that died on its way out.
    """
    from remote import tasks

    agent(
        "printf '{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,"
        "\"result\":\"all good\",\"session_id\":\"sess-2\"}\\n'\n"
        "echo 'the wheels came off' >&2\n"
        "exit 3\n"
    )

    outcome = tasks.run_task(_job())

    assert outcome.state == "failed"
    assert "exited 3" in outcome.error
    assert "the wheels came off" in outcome.error
    # The session id survives the failure: the conversation was opened, and issue 06 resumes the
    # PROJECT's session — losing it here would strand every later task on that project.
    assert outcome.session_id == "sess-2"


def test_a_credential_on_stderr_never_reaches_the_tasks_error(agent):
    """The failure message is a SECOND path out of the provider, and it bypassed the translator.

    `_ChildFailed` carries the child's raw stderr, and `run_task` puts it in `TaskOutcome.error` —
    which is POSTed to the relay, stored on the task row, AND written verbatim into the durable
    `task.terminal` event the requesting user reads back with `grid task follow`. The streamed
    `task.stderr` events were redacted; this copy was not, so a crash trace or an auth failure
    echoing the provider's own token leaked to the person who submitted the prompt.
    """
    from remote import tasks

    agent(
        "echo 'auth failed for sk-ant-oat01-AbCdEf0123456789' >&2\n"
        "echo 'sent Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.aaaaaaaa.bbbbbbbb' >&2\n"
        "exit 1\n"
    )

    outcome = tasks.run_task(_job())

    assert outcome.state == "failed"
    assert "sk-ant-oat01-AbCdEf0123456789" not in outcome.error
    assert "eyJhbGciOiJIUzI1NiJ9" not in outcome.error
    assert "sk-ant-***" in outcome.error and "Bearer ***" in outcome.error
    assert "auth failed" in outcome.error, "redacting must not swallow what the child said"


def test_every_failure_message_is_scrubbed_not_just_the_stderr_one(agent, monkeypatch):
    """One choke point, so a failure message added by a later issue is covered without anyone
    remembering to scrub it. An exception's own text is as unknown as the child's output."""
    from remote import tasks

    agent("printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"x\"}\\n'\n")
    monkeypatch.setattr(
        tasks, "_run_child",
        _raise_value(RuntimeError("boom while reading sk-ant-api03-0123456789abcdef")))

    outcome = tasks.run_task(_job())

    assert outcome.state == "failed"
    assert "0123456789abcdef" not in outcome.error


def _raise_value(exc):
    def _boom(*_args, **_kwargs):
        raise exc
    return _boom


def test_the_retained_stderr_buffer_itself_never_holds_a_credential():
    """Scrubbed at the SOURCE as well as at the outbound message.

    `_ChildFailed.stderr` is raw child output held in the provider's memory, and issues 05 and 07 add
    consumers of the failure path. Cleaning it where it is retained means a new reader inherits the
    guarantee instead of having to be told about it.
    """
    from remote import tasks

    with pytest.raises(tasks._ChildFailed) as excinfo:
        tasks._run_child(
            ["/bin/sh", "-c", "echo 'token sk-ant-oat01-AbCdEf0123456789' >&2; exit 4"],
            timeout=10.0, publish=lambda *_a, **_k: None)

    assert "AbCdEf0123456789" not in excinfo.value.stderr
    assert "sk-ant-***" in excinfo.value.stderr


def test_a_killed_child_fails_rather_than_completing(agent):
    """The criterion in the issue, verbatim: "a killed child yields a failure, not a success".

    A `Stop` hook does not run when a process is killed, which is precisely why the supervisor and
    not the agent owns this decision. `kill -9` on itself is the closest a test gets to the operator,
    the OOM killer, or issue 07's reclaim.
    """
    from remote import tasks

    agent(
        "printf '{\"type\":\"system\",\"subtype\":\"init\",\"session_id\":\"sess-3\"}\\n'\n"
        "kill -9 $$\n"
    )

    outcome = tasks.run_task(_job())

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.session_id == "sess-3"


def test_an_agent_that_disclaims_its_own_success_fails_the_task(agent):
    """Exit 0 and `is_error: true` — `error_max_turns`, `error_during_execution`.

    Reporting that as `completed` would hand a user a green task and an unfinished job. Trusting the
    agent in this direction only ever fails a run; it can never pass one the exit status failed.
    """
    from remote import tasks

    agent(
        "printf '{\"type\":\"result\",\"subtype\":\"error_max_turns\",\"is_error\":true,"
        "\"result\":\"I ran out of turns\"}\\n'\n"
        "exit 0\n"
    )

    outcome = tasks.run_task(_job())

    assert outcome.state == "failed"
    assert "error_max_turns" in outcome.error
    assert outcome.output == "I ran out of turns"


def test_a_wedged_child_is_killed_at_the_deadline_and_reported_failed(agent, monkeypatch):
    """A child that prints nothing must still hit the wall — `for line in stdout` would block on it
    forever, silently deleting the deadline."""
    from remote import tasks

    monkeypatch.setenv("GRID_TASK_TIMEOUT_SECONDS", "0.4")
    agent("sleep 30\n")

    outcome = tasks.run_task(_job())

    assert outcome.state == "failed"
    assert "timed out" in outcome.error


def test_a_misconfigured_deadline_falls_back_instead_of_retiring_task_serving(monkeypatch, capsys):
    """`GRID_TASK_TIMEOUT_SECONDS=1h` is a typo, not a reason to stop serving tasks for the life of
    the process. It falls back to the default and says so."""
    from remote import tasks

    monkeypatch.setenv("GRID_TASK_TIMEOUT_SECONDS", "1h")

    assert tasks.task_timeout() == tasks.DEFAULT_TASK_TIMEOUT_SECONDS
    assert "1h" in capsys.readouterr().err


def test_tool_activity_is_published_while_the_agent_is_still_running(agent):
    """Issue 03's first acceptance criterion — "while it is still running, not after".

    Proven by ordering against the child's own clock: the tool call is emitted, then the child sleeps
    before writing anything else. A publisher that buffered to the end would see the tool event only
    after that sleep, so asserting the event arrived BEFORE the child exited is the whole test.

    The sleep and the bound are deliberately a factor of THREE apart. A threshold sitting exactly on
    the child's own sleep has no margin in either direction: it fails on any scheduling hiccup, and
    the temptation is then to widen it until it no longer distinguishes a live publisher from a
    buffered one. Live is ~0.05s and buffered is ~3s, so 1.5s separates them with room on both sides.
    """
    import time

    from remote import tasks

    seen = []
    agent(
        "printf '{\"type\":\"assistant\",\"message\":{\"content\":[{\"type\":\"tool_use\","
        "\"id\":\"t1\",\"name\":\"Edit\",\"input\":{\"file_path\":\"/w/app.py\"}}]}}\\n'\n"
        "sleep 3\n"
        "printf '{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,"
        "\"result\":\"done\"}\\n'\n"
    )

    started = time.monotonic()
    outcome = tasks.run_task(
        _job(), publish=lambda kind, **f: seen.append((kind, f, time.monotonic() - started)))
    whole_run = time.monotonic() - started

    assert outcome.state == "completed"
    tool_events = [e for e in seen if e[0] == "task.tool_use"]
    assert tool_events, f"no tool activity was published at all: {seen}"
    kind, fields, at = tool_events[0]
    assert (fields["tool"], fields["path"]) == ("Edit", "/w/app.py")
    assert at < 1.5, f"the tool call surfaced only after the child's 3s sleep ({at:.2f}s)"
    # The child really did outlive the event — without this the bound above would also be satisfied
    # by a child that exited immediately, which proves nothing about publishing DURING a run.
    assert whole_run > 2.5, f"the child did not actually sleep ({whole_run:.2f}s)"


def test_a_large_burst_neither_stalls_nor_drops_events(agent):
    """A child that fills a pipe buffer while nobody reads it blocks on write and looks like a hang.

    Both pipes are read for the whole run and the deadline is enforced on the QUEUE, so a burst is
    absorbed. Every line still becomes an event: dropping under load is the failure that would only
    ever show up on a real task.
    """
    from remote import tasks

    published = []
    agent(
        "i=0\n"
        "while [ $i -lt 2000 ]; do\n"
        "  printf '{\"type\":\"assistant\",\"message\":{\"content\":[{\"type\":\"text\","
        "\"text\":\"line %s\"}]}}\\n' $i\n"
        "  i=$((i+1))\n"
        "done\n"
        "printf '{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,"
        "\"result\":\"done\"}\\n'\n"
    )

    outcome = tasks.run_task(_job(), publish=lambda kind, **f: published.append((kind, f)))

    assert outcome.state == "completed"
    texts = [f["text"] for kind, f in published if kind == "task.output"]
    assert texts == [f"line {i}" for i in range(2000)]


def test_stderr_and_plain_text_do_not_corrupt_the_event_log(agent):
    """Junk on both pipes, mixed with real records. Nothing is lost and nothing is malformed.

    The `bufsize=1` pump decodes with `errors="replace"`, so the non-UTF-8 byte here cannot raise
    inside a reader thread — where the broad guard would swallow it, end the thread, and report a
    task `completed` having lost its output.
    """
    import json

    from remote import task_events, tasks

    published = []
    agent(
        "echo 'this is not json at all'\n"
        "printf 'a partial line with a bad byte: \\xff\\n'\n"
        "echo 'a warning from the binary' >&2\n"
        "printf '{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,"
        "\"result\":\"finished anyway\"}\\n'\n"
    )

    outcome = tasks.run_task(_job(), publish=lambda kind, **f: published.append((kind, f)))

    assert outcome.state == "completed"
    assert outcome.output == "finished anyway"
    texts = [f.get("text") for _kind, f in published]
    assert "this is not json at all" in texts
    assert any(kind == "task.stderr" and "a warning from the binary" in f["text"]
               for kind, f in published)
    for kind, fields in published:
        assert len(json.dumps({"type": kind, **fields}).encode()) <= task_events.MAX_EVENT_BYTES


def test_a_flood_of_stderr_is_capped_and_says_so(agent):
    """stderr is not the task's output — it is the channel a child stuck in a retry loop floods.

    Capped rather than dropped silently: a log that just stops is indistinguishable from one that had
    nothing more to say.
    """
    from remote import tasks

    published = []
    agent(
        "i=0\n"
        "while [ $i -lt 400 ]; do echo \"noise $i\" >&2; i=$((i+1)); done\n"
        "printf '{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,"
        "\"result\":\"done\"}\\n'\n"
    )

    tasks.run_task(_job(), publish=lambda kind, **f: published.append((kind, f)))

    stderr_events = [f for kind, f in published if kind == "task.stderr"]
    assert len(stderr_events) == tasks._MAX_PUBLISHED_STDERR_LINES + 1
    assert "suppressed" in stderr_events[-1]["text"]


def test_a_chatty_child_cannot_grow_the_providers_memory_without_bound(agent, monkeypatch):
    """A task runs for up to an hour and `stream-json` is verbose — every line was being retained.

    Nothing reads the raw stdout any more (`run_task` reports the translator's `result_text`), and
    stderr is only ever read back 500 characters at a time, so retaining either in full buys
    nothing and costs a provider its memory on exactly the long runs this feature exists for.
    """
    from remote import tasks

    # Shrunk rather than generating megabytes: the property under test is that the bound EXISTS and
    # is applied to both streams, not what its value happens to be.
    monkeypatch.setattr(tasks, "_MAX_COLLECTED_CHARS", 5_000)
    chatty = (
        "i=0\n"
        "while [ $i -lt 3000 ]; do\n"
        "  printf 'out %s %s\\n' $i "
        "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'\n"
        "  printf 'err %s %s\\n' $i "
        "'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' >&2\n"
        "  i=$((i+1))\n"
        "done\n"
        "exit 7\n"
    )

    with pytest.raises(tasks._ChildFailed) as excinfo:
        tasks._run_child(["/bin/sh", "-c", chatty], timeout=30.0,
                         publish=lambda *_a, **_k: None)

    # Both streams together produce ~400 KB; each is held to its own budget.
    assert len(excinfo.value.stderr) <= tasks._MAX_COLLECTED_CHARS + len("… [truncated]\n")
    assert excinfo.value.stderr.startswith("… [truncated]"), "a dropped HEAD must be MARKED"
    # stderr keeps its END — the line a child writes last is the one that says why it died.
    assert "err 2999" in excinfo.value.stderr
    assert "err 0 " not in excinfo.value.stderr


def test_a_chatty_childs_failure_message_shows_its_LAST_words_not_its_first(agent, monkeypatch):
    """Retention and reading must want the same end of the stream.

    The failure message is built from `stderr[-500:]` — deliberately the tail, because the line that
    says *why* a child exited non-zero is the last one it wrote. A head-retained buffer throws that
    line away before the slice ever runs, and the slice then returns whatever happened to sit on the
    cap boundary: plausible-looking, irrelevant, and indistinguishable from the real thing.

    This is exactly the "child stuck in a retry loop" case the cap exists for, so the two policies
    meeting in the middle is not a corner.
    """
    from remote import tasks

    monkeypatch.setattr(tasks, "_MAX_COLLECTED_CHARS", 2_000)
    agent(
        "i=0\n"
        "while [ $i -lt 200 ]; do echo \"noise $i filler filler filler filler\" >&2; i=$((i+1)); "
        "done\n"
        "echo 'FATAL: connection refused to internal service' >&2\n"
        "exit 9\n"
    )

    outcome = tasks.run_task(_job())

    assert outcome.state == "failed"
    assert "FATAL: connection refused to internal service" in outcome.error, (
        f"the child's real reason was dropped; the message says: {outcome.error!r}")


def test_stdout_keeps_its_head_and_stderr_keeps_its_tail(monkeypatch):
    """The two streams are read from opposite ends, so they are retained from opposite ends.

    stdout is a transcript — its beginning is where a run explains itself. stderr is consumed as a
    failure message, and a failure explains itself at the end.
    """
    from remote import tasks

    monkeypatch.setattr(tasks, "_MAX_COLLECTED_CHARS", 25)

    head = tasks._Collected()
    tail = tasks._Tail()
    for filler in ("x", "y", "z", "w"):
        head.add(filler * 10)
        tail.add(filler * 10)

    assert head.text().startswith("x" * 10), "stdout keeps what came first"
    assert "w" not in head.text()

    assert tail.text().endswith("w" * 10), "stderr keeps what came last"
    assert "x" not in tail.text()
    assert tail.text().startswith("… [truncated]"), "the dropped HEAD is marked at the front"


def test_the_retained_stream_is_bounded_and_says_so(monkeypatch):
    """The bound itself, without spawning anything: keep the head, mark the tail, never grow."""
    from remote import tasks

    monkeypatch.setattr(tasks, "_MAX_COLLECTED_CHARS", 25)
    collected = tasks._Collected()

    for filler in ("x", "y", "z", "w"):
        collected.add(filler * 10)

    joined = collected.text()
    assert joined.startswith("x" * 10 + "y" * 10)
    assert "z" not in joined and "w" not in joined, "past the budget nothing more is retained"
    assert joined.endswith("… [truncated]")
    assert joined.count("… [truncated]") == 1, "the marker is appended once, not per dropped line"


def test_the_supervisor_is_handed_the_childs_own_process_handle(agent):
    """Issue 07's lease renewal proves liveness with `poll() is None` on THIS object.

    A pid read back from a record and signalled later is the hazard ADRs 0020 and 0026 removed from
    the run-record seams; the handle is passed out directly so it is never reintroduced.
    """
    import subprocess

    from remote import tasks

    handles = []
    agent("printf '{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,"
          "\"result\":\"done\"}\\n'\n")

    tasks.run_task(_job(), on_spawn=handles.append)

    assert len(handles) == 1
    assert isinstance(handles[0], subprocess.Popen)
    assert handles[0].poll() is not None  # the run is over, so the handle reports it


def test_a_publisher_that_raises_never_costs_the_task_its_result(agent, capsys):
    """Progress is an observer. The publisher is documented never to raise; if it ever does, the run
    still finishes and still reports — and the bug is named once, not once per line."""
    from remote import tasks

    def _broken(*_args, **_kwargs):
        raise RuntimeError("publisher bug")

    agent(
        "printf '{\"type\":\"assistant\",\"message\":{\"content\":[{\"type\":\"text\","
        "\"text\":\"a\"}]}}\\n'\n"
        "printf '{\"type\":\"assistant\",\"message\":{\"content\":[{\"type\":\"text\","
        "\"text\":\"b\"}]}}\\n'\n"
        "printf '{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,"
        "\"result\":\"done\"}\\n'\n"
    )

    outcome = tasks.run_task(_job(), publish=_broken)

    assert outcome.state == "completed"
    assert outcome.output == "done"
    assert capsys.readouterr().err.count("publisher bug") == 1


# --- issue 04: the task's input is already in git ------------------------------------------------

def _git(cwd, *args, check=True):
    import os
    import subprocess
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=check,
        env={"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_NOSYSTEM": "1",
             "HOME": "/nonexistent", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@invalid",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@invalid"})


def _remote_for(tmp_path, branch, files, name="origin.git"):
    """A real bare repo standing in for the relay's, and the branch tip. `(GitRemote, commit)`.

    A local path is a perfectly good git "URL", so the whole fetch/reset/clean/commit/push path runs
    against real git with no HTTP server in the way. What HTTP adds — the credential — is proved
    separately by looking at the child's environment, which is where it has to be either way.
    """
    from remote.task_repo import GitRemote

    seed = tmp_path / f"seed-{name}"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main", ".")
    for path, content in files.items():
        target = seed / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "input")
    _git(seed, "branch", "-f", branch)
    bare = tmp_path / name
    _git(tmp_path, "clone", "--bare", "-q", str(seed), str(bare))
    return GitRemote(url=str(bare), token="tok"), _git(bare, "rev-parse", branch).stdout.strip()


def test_a_second_task_on_a_project_fetches_only_the_delta(agent, tmp_path):
    """The workspace persists, so the second task must not re-download the project (issue 16a).

    The relay's side of this is proved in grid-src against a real 581 MiB history; this is the half
    that lives here, and it is the half that can regress silently. `materialize` fetches into a
    workspace `task_agent.workspace_for` derives from the project id — change it to clone into a
    fresh directory and every functional test in this file still passes, while every task on a real
    repository costs a fresh full clone of it.

    Asserted on OBJECTS rather than on wall clock: a timing assertion on a repository small enough
    to build in a fixture would be noise, and the object store is the thing that actually moved.
    """
    from remote import task_agent, task_repo, tasks

    remote, first_commit = _remote_for(tmp_path, "task/T1", {"a.txt": "one\n"})
    agent("printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")
    workspace = task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION)

    assert tasks.run_task(
        _job(input_commit=first_commit, branch="task/T1"), remote=remote).state == "completed"
    objects_after_first = {p.name for p in (workspace / ".git" / "objects").rglob("*")
                           if p.is_file()}
    assert objects_after_first, "the first task fetched nothing, so this proves nothing"

    # A second task branch on the SAME repository, sharing the first's history.
    _git(remote.url, "branch", "task/T2", "task/T1")
    second_commit = _git(remote.url, "rev-parse", "task/T2").stdout.strip()

    assert tasks.run_task(
        _job(input_commit=second_commit, branch="task/T2"), remote=remote).state == "completed"

    objects_after_second = {p.name for p in (workspace / ".git" / "objects").rglob("*")
                            if p.is_file()}
    assert objects_after_first <= objects_after_second, (
        "the workspace's object store was rebuilt rather than added to — the project is being "
        "re-cloned per task")
    # And it really is the same directory, which is what makes the comparison above meaningful at
    # all: two clones into two paths would each look like a clean superset of nothing.
    assert task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION) == workspace
    assert task_repo.fetched_project(workspace) is None, (
        "the provider's workspace was turned into a `grid task fetch` destination")


def test_the_agent_reads_the_exact_file_the_client_uploaded(agent, tmp_path, monkeypatch):
    """The issue's demo, end to end: upload a file, the agent reads THAT file.

    Driven through `run_task` rather than the checkout helper, because the guarantee is about the
    agent's cwd at spawn time — a checkout that happened after the child started would satisfy a
    unit test of the helper and still hand the agent an empty directory.
    """
    from remote import tasks

    remote, commit = _remote_for(tmp_path, "task/T1", {"src/a.txt": "ZEBRA-4417\n"})
    agent("printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"%s\"}\\n' \"$(cat src/a.txt)\"\n")

    outcome = tasks.run_task(
        _job(input_commit=commit, branch="task/T1"), remote=remote)

    assert outcome.state == "completed", outcome.error
    assert outcome.output == "ZEBRA-4417"


def test_a_previous_tasks_leftovers_are_gone_but_the_reserved_directory_survives(
        agent, tmp_path, monkeypatch):
    """The workspace is per-PROJECT and persists across tasks — the transcript path depends on it.

    So the reset has to be exact: a previous task's file left lying about is indistinguishable from
    this task's input. `.grid/` is the one exception, and deliberately so — it holds the project's
    conversation (issue 06), and a `git clean` that deleted it would destroy that conversation on
    every task. `-e .grid` covers the whole reserved directory, so the provider's own untracked
    state under it survives too.
    """
    from remote import task_agent, tasks

    workspace = task_agent.ensure_workspace(task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION))
    (workspace / "stale.txt").write_text("from the last task\n")
    transcript = task_agent.transcript_dir(workspace, _MEMBER)
    transcript.mkdir(parents=True)
    (transcript / "sess-1.jsonl").write_text("the project's conversation\n")
    (workspace / ".grid" / "scratch.txt").write_text("the provider's own\n")

    remote, commit = _remote_for(tmp_path, "task/T1", {"fresh.txt": "new\n"})
    agent("printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")

    outcome = tasks.run_task(
        _job(input_commit=commit, branch="task/T1"), remote=remote)

    assert outcome.state == "completed", outcome.error
    assert not (workspace / "stale.txt").exists(), "a previous task's file survived the reset"
    assert (workspace / "fresh.txt").read_text() == "new\n"
    assert (transcript / "sess-1.jsonl").read_text() == "the project's conversation\n"
    assert (workspace / ".grid" / "scratch.txt").read_text() == "the provider's own\n"


def test_the_workspace_is_left_on_the_task_branch_for_the_push(agent, tmp_path):
    """Issue 05 pushes `task/<id>` from here, so HEAD must be that branch and not a detached head —
    a detached checkout commits to nothing and the push has no ref to name."""
    from remote import task_agent, tasks

    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    agent("printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")

    tasks.run_task(_job(input_commit=commit, branch="task/T1"), remote=remote)

    workspace = task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION)
    assert _git(workspace, "symbolic-ref", "HEAD").stdout.strip() == "refs/heads/task/T1"
    assert _git(workspace, "rev-parse", "HEAD").stdout.strip() == commit


def test_input_that_cannot_be_fetched_stops_before_the_spawn_and_stays_retryable(
        agent, tmp_path):
    """An agent that ran against missing input produces a confidently wrong result, which is the
    exact failure ADR 0032 D-b exists to prevent. Stopping before the spawn is the only safe answer.

    That half is unchanged and is still the important one. What ADR 0033 issue 16a changed is what
    happens NEXT: this used to return a terminal `failed` outcome, and terminal is the one state
    nothing retries — so a relay that could not serve the fetch failed every task in the project
    permanently. It now raises `InputFetchError`, which `_supervise_one_task` turns into silence and
    the relay's reclaim turns into another provider's attempt.

    Both assertions are kept in one test on purpose: "did not spawn" and "did not report terminal"
    are the two halves of one answer, and a version of this that checked only the first would go
    green on a change that reintroduced the permanent failure.
    """
    from remote import task_repo, tasks

    agent("printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n"
          "touch \"$GRID_TEST_RAN\"\n")
    ran = tmp_path / "the-agent-ran"
    import os
    os.environ["GRID_TEST_RAN"] = str(ran)
    try:
        with pytest.raises(task_repo.InputFetchError):
            tasks.run_task(
                _job(input_commit="0" * 40, branch="task/T1"),
                remote=task_repo.GitRemote(
                    url=str(tmp_path / "nothing-here.git"), token="tok"))
    finally:
        os.environ.pop("GRID_TEST_RAN", None)

    assert not ran.exists(), "the agent was spawned against input that never arrived"


def test_a_relay_with_no_git_plane_runs_the_task_as_before(agent, tmp_path):
    """Old-relay compatibility, and the direction it fails in.

    A claim payload with no `input_commit` comes from a relay predating this slice. The provider
    runs the task in an empty workspace exactly as it did then — degrading to the PREVIOUS
    behaviour, never to a new failure.
    """
    from remote import tasks

    agent("printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")

    outcome = tasks.run_task(_job(), remote=None)

    assert outcome.state == "completed", outcome.error


def test_a_network_git_call_is_bounded_in_time_not_left_to_the_tasks_whole_deadline(
        tmp_path, monkeypatch):
    """What replaced the bundle's byte cap, and why it is a CLOCK now rather than a size.

    The provider used to read the whole input into memory, so it needed a byte ceiling of its own.
    git streams to disk instead, so the exposure changed shape: the size is bounded at the relay
    (`task_git_max_bytes`, where the body is actually buffered), and what the provider still owes
    itself is a bound on TIME — a stalled relay must fail the fetch rather than silently consume the
    task's entire deadline before the agent has started.

    The local ceiling must stay separate and smaller: reusing it for the network would turn an
    ordinary slow push into a lost result.
    """
    from remote import task_repo

    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    workspace = tmp_path / "ws"
    workspace.mkdir()
    seen = _spy_on_git(monkeypatch)

    task_repo.materialize(workspace, url=remote.url, token=remote.token,
                          branch="task/T1", input_commit=commit)

    # `-C <workspace>` always immediately precedes the subcommand.
    by_subcommand = {argv[argv.index(str(workspace)) + 1]: kw.get("timeout")
                     for argv, kw in seen}
    assert by_subcommand["fetch"] == task_repo._GIT_NETWORK_TIMEOUT_SECONDS
    assert by_subcommand["reset"] == task_repo._GIT_TIMEOUT_SECONDS
    assert task_repo._GIT_NETWORK_TIMEOUT_SECONDS > task_repo._GIT_TIMEOUT_SECONDS


def test_the_git_remote_is_built_for_this_tasks_own_project(monkeypatch):
    """The loop must hand `run_task` the remote for THIS task's project.

    The wrong project id would fetch someone else's repository — which the relay's fence turns into
    a 404 rather than a leak, but the task then fails for a reason no operator could read. The token
    has to be the live one too: `state.token()` at call time, not a value captured at start-up.
    """
    from remote import tasks

    captured = {}

    def fake_run(job, publish=None, on_spawn=None, remote=None, capacity=None):
        # Spelled out rather than `**kwargs` on purpose: absorbing whatever it is handed would
        # re-open the hole this test's comment below describes — a mismatch that raises inside
        # `_run_and_report`'s guard and leaves the test green while exercising nothing.
        captured["remote"] = remote
        return tasks.TaskOutcome("completed", "ok", None)

    monkeypatch.setattr(tasks, "run_task", fake_run)
    # `*a` rather than a fixed parameter list. The previous spelling took four positional arguments
    # while `_run_and_report` passes five (`spawned` was added by issue 05), so every call raised a
    # TypeError that `_run_and_report`'s own `except (Exception, SystemExit)` swallowed — and the
    # test still passed, because its assertion reads what `fake_run` recorded BEFORE the raise. It
    # was green while exercising none of the path past `run_task`.
    monkeypatch.setattr(tasks, "_push_result", lambda *a: (a[1], True))
    reported = {}
    monkeypatch.setattr(tasks, "report_once", lambda _s, tid, **kw: reported.update(kw))
    monkeypatch.setattr(tasks, "_publisher_for", lambda *a, **k: _NullPublisher())

    tasks._run_and_report(_FakeState(), {"task_id": "T-42", "prompt": "p",
                                         "project_id": "proj-1", "member_key": _MEMBER,
                                          "input_commit": "c" * 40})

    assert captured["remote"].url == "http://relay/relay/v1/git/proj-1"
    assert captured["remote"].token == "tok"
    # Proves the swallowed TypeError is really gone: a report only happens if the whole path ran.
    assert reported["state"] == "completed", reported


class _NullPublisher:
    def publish(self, *_a, **_k):
        return True

    def close(self):
        pass


class _FakeState:
    signaling_url = "http://relay"

    def token(self):
        return "tok"

    def refresh(self, stale_token=None):
        return False


def test_a_runner_that_raises_does_not_publish_the_exceptions_words_verbatim(monkeypatch):
    """The fourth door, and it was open.

    Every failure `run_task` RETURNS goes through `_failed`, which scrubs. This message is built
    outside it, from an exception nobody here controls — and it travels the same way: to the relay,
    onto the task row, and into the durable `task.terminal` event the requesting user reads back.
    A credential in an exception's repr would be in a permanent log with no way to unsay it.
    """
    from remote import tasks

    reported = {}

    def explode(*_a, **_k):
        raise RuntimeError("relay refused: Authorization: Bearer sk-ant-oat01-DEADBEEFCAFE")

    monkeypatch.setattr(tasks, "run_task", explode)
    monkeypatch.setattr(tasks, "_publisher_for", lambda *a, **k: _NullPublisher())
    # `**_rest` rather than every keyword spelled out: this test is about the SCRUB, and a double
    # that pinned the report's exact signature would fail every time a field was added to the wire
    # — which is a different test's job, and one nobody would look for here.
    monkeypatch.setattr(
        tasks, "report_once",
        lambda _s, _t, *, error, **_rest: reported.update(error=error))

    tasks._run_and_report(_FakeState(), {"task_id": "T1", "prompt": "p", "project_id": "proj-1",
                                         "member_key": _MEMBER,
                                         "conversation_id": _CONVERSATION})

    assert "DEADBEEFCAFE" not in reported["error"]
    # The MARKER is expected to survive — it is deliberately recognizable, so a reader can tell
    # "a credential was here and was removed" from "there was nothing here".
    assert "sk-ant-***" in reported["error"]
    # Still says something useful — a scrub that erased the whole message would trade one silent
    # failure for another.
    assert "task runner raised" in reported["error"]


def test_a_task_that_needs_input_with_no_way_to_fetch_it_fails_rather_than_running_empty(agent):
    """`input_commit` present but no git remote wired is a CALLER bug, and it must not look like the
    old-relay degrade.

    That degrade is correctly gated on `input_commit` being ABSENT. If a truthy commit met a missing
    remote, the old code skipped the checkout silently and spawned the agent against whatever was
    already in the per-project workspace — stale from a prior task, or empty. Issues 05 and 07 both
    assemble job dicts for this path, so the guard belongs in the function rather than in the
    convention that today's only caller happens to follow.
    """
    from remote import tasks

    agent("printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")

    outcome = tasks.run_task(_job(input_commit="abc123", branch="task/T1"), remote=None)

    assert outcome.state == "failed"
    assert "input" in (outcome.error or "").lower()


# --- issue 05: the result comes back over git-over-HTTP ------------------------------------------

def _spy_on_git(monkeypatch):
    """Capture every git child's argv and call kwargs, while still really running it."""
    from remote import task_repo

    seen = []
    real = task_repo.subprocess.run

    def spy(argv, **kwargs):
        seen.append((list(argv), dict(kwargs)))
        return real(argv, **kwargs)

    monkeypatch.setattr(task_repo.subprocess, "run", spy)
    return seen


def _envs(seen):
    return [dict(kw.get("env") or {}) for _argv, kw in seen]


def test_the_grid_token_reaches_git_through_the_environment_not_the_command_line(
        tmp_path, monkeypatch):
    """Argv is world-readable on Linux (`/proc/<pid>/cmdline`); the environment is not
    (`/proc/<pid>/environ` is 0600). A grid access token lives a year, so a provider serving
    inference beside a task would otherwise leak it to every local `ps`.

    Asserted BOTH ways round on purpose: that the token is in the environment, and that it is in no
    argv. Checking only the first would pass just as happily with the token in both places.
    """
    from remote import task_repo

    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    workspace = tmp_path / "ws"
    workspace.mkdir()
    seen = _spy_on_git(monkeypatch)

    task_repo.materialize(workspace, url=remote.url, token="SEKRIT",
                          branch="task/T1", input_commit=commit)

    assert (workspace / "a.txt").read_text() == "x\n"
    assert seen, "no git child ran at all"
    assert not any("SEKRIT" in part for argv, _kw in seen for part in argv)
    # The KEY as well as the value. Asserting only the value passes just as happily when the token
    # is bound to a setting git ignores — a mutant that renamed this to `http.unusedHeader` survived
    # the first version of this test, and every request would have gone out unauthenticated.
    assert any(env.get("GIT_CONFIG_KEY_0") == "http.extraHeader"
               and env.get("GIT_CONFIG_VALUE_0") == "Authorization: Bearer SEKRIT"
               for env in _envs(seen))


def test_the_credential_is_not_replayed_to_a_redirect_target(tmp_path, monkeypatch):
    """`http.extraHeader` is sent to whatever host git ends up talking to. A relay that answered a
    redirect — or a proxy that inserted one — would otherwise hand the grid token to a third party,
    so redirects are refused rather than followed."""
    from remote import task_repo

    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    workspace = tmp_path / "ws"
    workspace.mkdir()
    seen = _spy_on_git(monkeypatch)

    task_repo.materialize(workspace, url=remote.url, token="SEKRIT",
                          branch="task/T1", input_commit=commit)

    settings = {env.get(f"GIT_CONFIG_KEY_{n}"): env.get(f"GIT_CONFIG_VALUE_{n}")
                for env in _envs(seen) for n in range(int(env.get("GIT_CONFIG_COUNT") or 0))}
    assert settings.get("http.followRedirects") == "false"


def _publish_merge_target(remote, task_id, revision="main"):
    """Put a `refs/integrate/<id>` in the relay's repo, as tier-3 integration does (ADR 0033 D-e).

    The relay resolves `main` ONCE and pins it under this ref; the provider fetches the ref because
    a bare oid is unfetchable — `uploadpack.allowAnySHA1InWant` is off. Written here with real git so
    the fetch under test is a real fetch of a real ref.
    """
    ref = f"refs/integrate/{task_id}"
    _git(remote.url, "update-ref", ref, _git(remote.url, "rev-parse", revision).stdout.strip())
    return ref


def test_a_merge_task_fetches_the_ref_it_must_merge_onto_the_same_name(tmp_path, monkeypatch):
    """`merge_ref` on the claim (ADR 0033 D-e, issue 15), fetched BEFORE the agent is spawned.

    The agent cannot fetch it itself and must never be able to: `child_env` hands it no grid
    credential at all, and that is the property the whole confinement design rests on.

    **Onto the IDENTICAL local name.** The refspec is `+<ref>:<ref>`, which is what keeps the local
    ref name out of the cross-repo lockstep list — the ref the relay names in the merge prompt is the
    ref that exists in the workspace, so there is no second literal for the two repos to disagree
    about.
    """
    from remote import task_repo

    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    ref = _publish_merge_target(remote, "T1")
    pinned = _git(remote.url, "rev-parse", ref).stdout.strip()
    workspace = tmp_path / "ws"
    workspace.mkdir()

    task_repo.materialize(workspace, url=remote.url, token="tok", branch="task/T1",
                          input_commit=commit, merge_ref=ref)

    assert _git(workspace, "rev-parse", ref).stdout.strip() == pinned, (
        "the agent was told to merge a ref that is not in its workspace")


def test_a_claim_with_no_merge_ref_fetches_nothing_extra(tmp_path, monkeypatch):
    """*Absent ⇒ the provider merges nothing*, which is exactly the pre-integration behaviour — an
    old relay's claim runs as an ordinary task rather than failing in a new way."""
    from remote import task_repo

    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    workspace = tmp_path / "ws"
    workspace.mkdir()
    seen = _spy_on_git(monkeypatch)

    task_repo.materialize(workspace, url=remote.url, token="tok", branch="task/T1",
                          input_commit=commit)

    fetches = [argv for argv, _kw in seen if "fetch" in argv]
    assert len(fetches) == 1, fetches


def test_a_merge_ref_that_is_not_one_is_refused_before_any_git_runs(tmp_path, monkeypatch):
    """The value comes off the wire and ends up in a git argv, so it is validated at the boundary.

    `_run` never goes through a shell, so the exposure is option confusion rather than command
    injection — a leading `-` would be read by `git fetch` as a flag. That is exactly the class this
    refuses cheaply, and it is the same reasoning `resolve_commit` records on the relay side.

    TERMINAL rather than retryable: no provider can fix a malformed claim, and retrying it spends
    every attempt to arrive at `retries_exhausted`, which does not even carry the real reason.
    """
    from remote import task_repo

    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    workspace = tmp_path / "ws"
    workspace.mkdir()

    for bad in ("--upload-pack=touch /tmp/pwned", "refs/heads/main", "-x", "refs/integrate/a b",
                "refs/integrate/../heads/main", "refs/integrate/"):
        seen = _spy_on_git(monkeypatch)
        with pytest.raises(task_repo.CheckoutError) as raised:
            task_repo.materialize(workspace, url=remote.url, token="tok", branch="task/T1",
                                  input_commit=commit, merge_ref=bad)
        assert not isinstance(raised.value, task_repo.InputFetchError), (
            f"{bad!r} was treated as a retryable fetch failure")
        assert not seen, f"{bad!r} reached git before it was refused: {seen}"


def test_a_merge_ref_that_cannot_be_fetched_is_retryable_like_the_input(tmp_path):
    """The same split `materialize` already makes at the network (ADR 0033 issue 16a).

    A relay at its git concurrency limit, a dropped connection, a ref the retention sweep collected
    early: all facts about THIS attempt. Reporting `failed` would be terminal, and terminal is
    precisely the state nothing retries.
    """
    from remote import task_repo

    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    workspace = tmp_path / "ws"
    workspace.mkdir()

    with pytest.raises(task_repo.InputFetchError):
        task_repo.materialize(workspace, url=remote.url, token="tok", branch="task/T1",
                              input_commit=commit, merge_ref="refs/integrate/never-published")


def test_the_loop_hands_the_merge_ref_from_the_claim_to_the_checkout(tmp_path, monkeypatch, agent):
    """The wiring, asserted rather than assumed. A `merge_ref` the claim carried and `run_task`
    dropped would leave the agent merging nothing — and the relay would then refuse its result as a
    failed integration, blaming a run that did exactly what it was told."""
    from remote import task_repo, tasks

    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    ref = _publish_merge_target(remote, "T1")
    agent("printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")
    passed = {}
    real = task_repo.materialize

    def spy(workspace, **kwargs):
        passed.update(kwargs)
        return real(workspace, **kwargs)

    monkeypatch.setattr(task_repo, "materialize", spy)

    tasks.run_task({**_job_with_input(commit), "merge_ref": ref}, remote=remote)

    assert passed.get("merge_ref") == ref, passed


_BASE_LINES = "".join(f"line{n}\n" for n in range(1, 10))
_OURS = _BASE_LINES.replace("line5\n", "OURS-5\n")
_THEIRS = _BASE_LINES.replace("line5\n", "THEIRS-5\n")


def _conflicted_remote(tmp_path, task_id="T1"):
    """A relay repo where `task/<id>` and `refs/integrate/<id>` changed the SAME line.

    The tier-3 state, built with real git: neither side can be taken automatically, so a merge in the
    workspace stops with conflicts and leaves `MERGE_HEAD` — which is what the provider's own commit
    then turns into a two-parent commit. Returns `(remote, input_commit, merge_ref, pinned)`.
    """
    from remote.task_repo import GitRemote

    seed = tmp_path / f"seed-{task_id}"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main", ".")
    (seed / "shared.txt").write_text(_BASE_LINES)
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "base")
    _git(seed, "checkout", "-q", "-b", f"task/{task_id}")
    (seed / "shared.txt").write_text(_OURS)
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "ours")
    _git(seed, "checkout", "-q", "main")
    (seed / "shared.txt").write_text(_THEIRS)
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "theirs")

    bare = tmp_path / f"origin-{task_id}.git"
    _git(tmp_path, "clone", "--bare", "-q", str(seed), str(bare))
    ref = f"refs/integrate/{task_id}"
    pinned = _git(bare, "rev-parse", "main").stdout.strip()
    _git(bare, "update-ref", ref, pinned)
    return (GitRemote(url=str(bare), token="tok"),
            _git(bare, "rev-parse", f"task/{task_id}").stdout.strip(), ref, pinned)


def _merge_in_progress(tmp_path, workspace, remote, input_commit, merge_ref):
    """Materialize a merge task and run the merge the agent is told to run, leaving it conflicted."""
    from remote import task_repo

    task_repo.materialize(workspace, url=remote.url, token=remote.token, branch="task/T1",
                          input_commit=input_commit, merge_ref=merge_ref)
    # Exits 1 — that is the case. `_git` would raise on it, so this one is run directly.
    subprocess.run(["git", "-C", str(workspace), "merge", "--no-edit", merge_ref],
                   capture_output=True)


def test_a_resolved_merge_is_committed_with_both_parents(tmp_path):
    """The measurement the relay's whole ancestry check rests on (git 2.54.0).

    `commit_and_push` runs `git add -A` and `git commit`, and `git commit` CONSUMES `MERGE_HEAD` —
    so the result has two parents and reaches the ref that was merged, whether or not the agent
    committed the merge itself. That is what makes the relay's check structural rather than a
    question of the agent having phrased a git command correctly.

    Measured rather than assumed, and landed as a test rather than a note: if this ever stops being
    true, every merge task in the fleet starts being refused as a failed integration.
    """
    from remote import task_repo

    remote, input_commit, merge_ref, pinned = _conflicted_remote(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _merge_in_progress(tmp_path, workspace, remote, input_commit, merge_ref)
    # What the agent is told to do: resolve, `git add` to record that it decided, and leave the
    # commit to the grid.
    (workspace / "shared.txt").write_text(_OURS.replace("OURS-5\n", "OURS-5\nTHEIRS-5\n"))
    _git(workspace, "add", "shared.txt")

    pushed = task_repo.commit_and_push(
        workspace, url=remote.url, token=remote.token, branch="task/T1",
        message="task T1 (completed)")

    parents = _git(remote.url, "rev-list", "--parents", "-n", "1",
                   pushed.commit).stdout.split()[1:]
    assert parents == [input_commit, pinned], parents
    assert not pushed.unresolved, pushed.unresolved
    # The relay's own question, asked here of real git against the real pushed result.
    assert subprocess.run(["git", "--git-dir", remote.url, "merge-base", "--is-ancestor",
                           pinned, pushed.commit], capture_output=True).returncode == 0


def test_conflict_markers_the_agent_never_resolved_are_reported(tmp_path):
    """The hole `git add -A` opens, and the only place it can be closed.

    Measured: during a conflicted merge `git ls-files --unmerged` lists three stage entries, and
    `git add -A` clears them to zero — staging the conflict markers as if they were a resolution.
    `git commit` then succeeds where git itself would have refused, and the result is structurally a
    perfectly good merge commit. The relay cannot see any of this: the index is never pushed.

    So it is read HERE, before `add -A`, and reported. The work is still committed and pushed —
    ADR 0032 D-e — because a user must be able to see what the agent did.
    """
    from remote import task_repo

    remote, input_commit, merge_ref, _pinned = _conflicted_remote(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _merge_in_progress(tmp_path, workspace, remote, input_commit, merge_ref)

    pushed = task_repo.commit_and_push(
        workspace, url=remote.url, token=remote.token, branch="task/T1",
        message="task T1 (completed)")

    assert pushed.unresolved == ("shared.txt",), pushed.unresolved
    # Pushed anyway: the branch is the only copy of what the agent did.
    assert _git(remote.url, "rev-parse", "refs/heads/task/T1").stdout.strip() == pushed.commit


def test_a_resolution_the_agent_left_unstaged_is_still_unresolved(tmp_path):
    """**This assertion was the opposite way round, and the rule it encoded was wrong.**

    The first version of this check read markers out of the worktree and treated an unmerged index
    as merely "not `git add`ed", so that a resolution left unstaged would not be failed. That is
    unsound, and a `modify/delete` conflict is the proof: git leaves NO markers for one (measured on
    2.54.0 — the surviving side's content is written verbatim), so a marker test cannot see it at
    all. The whole class of non-textual conflicts was invisible.

    So the INDEX is the authority now, which is also git's own rule: `git commit` refuses while
    paths are unmerged, and `commit_and_push`'s `git add -A` is what overrides that refusal. The
    prompt tells the agent to `git add`/`git rm` every conflicted path, so anything still unmerged
    is a path the agent did not resolve.

    The cost is accepted deliberately: an agent that edits a file and stages nothing has its task
    failed, one run is wasted, and the work is still pushed for the member to read. The alternative
    cost is somebody's deletion silently discarded, permanently, with every signal reading healthy.
    """
    from remote import task_repo

    remote, input_commit, merge_ref, _pinned = _conflicted_remote(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _merge_in_progress(tmp_path, workspace, remote, input_commit, merge_ref)
    assert "<<<<<<< " in (workspace / "shared.txt").read_text(), "the fixture never conflicted"
    # Resolved in the worktree, deliberately NOT staged and NOT committed.
    (workspace / "shared.txt").write_text(_OURS.replace("OURS-5\n", "OURS-5\nTHEIRS-5\n"))

    pushed = task_repo.commit_and_push(
        workspace, url=remote.url, token=remote.token, branch="task/T1", message="task T1")

    assert pushed.unresolved == ("shared.txt",), pushed.unresolved


def test_a_conflict_the_agent_staged_is_resolved_however_it_chose_to_resolve_it(tmp_path):
    """Taking one side wholesale — including deleting the file — is a judgement call, not this
    check's business. What the check asks is only whether the agent DECIDED, and `git add`/`git rm`
    is how git records that a person decided."""
    from remote import task_repo

    remote, input_commit, merge_ref, _pinned = _conflicted_remote(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _merge_in_progress(tmp_path, workspace, remote, input_commit, merge_ref)
    _git(workspace, "rm", "-q", "-f", "shared.txt")

    pushed = task_repo.commit_and_push(
        workspace, url=remote.url, token=remote.token, branch="task/T1", message="task T1")

    assert pushed.unresolved == (), pushed.unresolved


def _modify_delete_remote(tmp_path, task_id="T1"):
    """A conflict git resolves with NO conflict markers at all: one side deletes, the other edits.

    Measured on git 2.54.0: `git merge` reports `CONFLICT (modify/delete)`, leaves the surviving
    side's content in the tree verbatim, and lists the path in `ls-files --unmerged` with two stage
    entries. There is nothing textual to find — which is why the index, not the file content, has to
    be what decides whether the agent resolved anything.
    """
    from remote.task_repo import GitRemote

    seed = tmp_path / f"seed-md-{task_id}"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main", ".")
    (seed / "shared.txt").write_text(_BASE_LINES)
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "base")
    # The trunk edits the file.
    (seed / "shared.txt").write_text(_THEIRS)
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "theirs edits")
    _git(seed, "checkout", "-q", "-b", f"task/{task_id}", "HEAD~1")
    # The member's branch deletes it.
    _git(seed, "rm", "-q", "shared.txt")
    _git(seed, "commit", "-q", "-m", "ours deletes")

    bare = tmp_path / f"origin-md-{task_id}.git"
    _git(tmp_path, "clone", "--bare", "-q", str(seed), str(bare))
    ref = f"refs/integrate/{task_id}"
    _git(bare, "update-ref", ref, _git(bare, "rev-parse", "main").stdout.strip())
    return (GitRemote(url=str(bare), token="tok"),
            _git(bare, "rev-parse", f"task/{task_id}").stdout.strip(), ref)


def test_a_conflict_with_no_markers_at_all_is_still_caught(tmp_path):
    """The regression for the hole a marker-based check could not see.

    One side deletes a file, the other edits it. git leaves the survivor's content in the tree with
    no markers, so an agent that does nothing produces a structurally perfect two-parent merge commit
    that PASSES the relay's ancestry check — and the deletion somebody intended is discarded in
    silence, which is exactly the failure ADR 0033 D-e exists to prevent, reached through a conflict
    type the first version of this check was blind to.
    """
    from remote import task_repo

    remote, input_commit, merge_ref = _modify_delete_remote(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _merge_in_progress(tmp_path, workspace, remote, input_commit, merge_ref)
    # The evidence that a marker test is not enough: there is nothing to find.
    assert "<<<<<<< " not in (workspace / "shared.txt").read_text()

    pushed = task_repo.commit_and_push(
        workspace, url=remote.url, token=remote.token, branch="task/T1", message="task T1")

    assert pushed.unresolved == ("shared.txt",), (
        "a modify/delete conflict the agent never resolved was reported as a clean merge")


def test_an_ordinary_task_reports_nothing_unresolved(tmp_path):
    """No merge, no unmerged paths — the check must not invent one for every ordinary task."""
    from remote import task_repo

    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    workspace = tmp_path / "ws"
    workspace.mkdir()
    task_repo.materialize(workspace, url=remote.url, token=remote.token, branch="task/T1",
                          input_commit=commit)
    (workspace / "a.txt").write_text("edited\n")

    pushed = task_repo.commit_and_push(
        workspace, url=remote.url, token=remote.token, branch="task/T1", message="task T1")

    assert pushed.unresolved == (), pushed.unresolved


def test_an_unresolved_merge_is_not_reported_as_a_completed_task(tmp_path, monkeypatch, agent):
    """The provider's half of "proving it merged" (ADR 0033 D-e, issue 15).

    An agent that exits 0 having left the conflict markers in place produces a structurally valid
    merge commit, so the relay's ancestry check PASSES and the member's WIP branch fast-forwards onto
    a tree full of `<<<<<<<`. Only the provider ever sees the index that says so.

    The same direction as `translator.is_error`: this can fail a run the agent called a success, and
    never pass one it did not.
    """
    from remote import tasks

    remote, input_commit, merge_ref, _pinned = _conflicted_remote(tmp_path)
    _relay_git_url(monkeypatch, remote.url)
    # The agent runs the merge, says it is done, and resolves nothing.
    agent(f'git merge --no-edit {merge_ref} >/dev/null 2>&1\n'
          "printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"done\"}\\n'\n")
    reported = []
    monkeypatch.setattr(tasks, "report_once",
                        lambda _s, tid, **kw: reported.append((tid, kw)))

    tasks._run_and_report(
        _FakeState(), {**_job_with_input(input_commit), "merge_ref": merge_ref})

    assert reported, "nothing was reported at all"
    _task_id, fields = reported[0]
    assert fields["state"] == "failed", fields
    assert "shared.txt" in (fields.get("error") or ""), fields
    # The work still reached the relay, so the user can see it and finish it by hand.
    assert fields["result_commit"], fields
    assert _git(remote.url, "rev-parse", "refs/heads/task/T1").stdout.strip() \
        == fields["result_commit"]


def test_a_commit_the_agent_makes_is_authored_by_the_member_not_the_provider(
        tmp_path, monkeypatch, agent):
    """ADR 0033 D-m, reached from the direction issue 15 opened — asserted on the REAL child.

    Until tier 3 nothing ever asked an agent to commit; the provider made every commit itself with
    `GIT_AUTHOR_*` from the claim. A merge task's prompt tells the agent to commit the merge, so its
    own `git commit` now writes history.

    `_GIT_CONFIG_FLOOR` was believed to make that fail loudly for want of a `user.name`. Measured on
    git 2.54.0 with exactly the child's environment: it does NOT fail — git auto-detects
    `<user>@<hostname>` and exits 0, authoring the requesting team's history as the provider's own
    machine. That is the one thing ADR 0033 records as not retroactively fixable.

    Asserted by making a real commit from inside the agent and reading it back with git, rather than
    by inspecting the dict `child_env` returns: the question is what the CHILD sees.
    """
    from remote import tasks

    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    _relay_git_url(monkeypatch, remote.url)
    agent("echo edited > a.txt\n"
          "git add -A\n"
          "git commit -q -m 'the agent commits for itself'\n"
          "printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")

    outcome = tasks.run_task(
        {**_job_with_input(commit),
         "author_name": "Alice Nguyen", "author_email": "alice@example.com"},
        remote=remote)

    assert outcome.state == "completed", outcome.error
    workspace = _workspace_for("proj-1", _MEMBER, _CONVERSATION)
    idents = _git(workspace, "log", "-1", "--format=%an|%ae|%cn|%ce", "HEAD").stdout.strip()
    assert idents == f"Alice Nguyen|alice@example.com|{task_repo_default_name()}|" \
                     f"{task_repo_default_email()}", idents


def task_repo_default_name():
    from remote import task_repo
    return task_repo.DEFAULT_IDENTITY.name


def task_repo_default_email():
    from remote import task_repo
    return task_repo.DEFAULT_IDENTITY.email


def _workspace_for(project_id, member_key, conversation_id=_CONVERSATION):
    from remote import task_agent
    return task_agent.workspace_for(project_id, member_key, conversation_id)


def test_an_ordinary_task_may_leave_a_merge_in_progress_if_that_is_what_was_asked(
        tmp_path, monkeypatch, agent):
    """The grid only enforces what the GRID asked for.

    A merge task's prompt is written by the relay and says "resolve the conflict", so an agent that
    reports success having resolved nothing is contradicting its instructions. An ordinary task's
    prompt is written by the USER, and "start merging X into Y and leave it for me to look at" is a
    perfectly ordinary thing to ask for — failing that would be the grid overruling the person whose
    repository it is.

    So the check is gated on `merge_ref`: it is the provider's half of proving a MERGE task merged,
    not a general opinion about what a workspace may contain.
    """
    from remote import tasks

    remote, input_commit, _merge_ref, _pinned = _conflicted_remote(tmp_path)
    _relay_git_url(monkeypatch, remote.url)
    # A conflict the agent makes ENTIRELY on its own, out of refs it created — so the workspace ends
    # in the same state a merge task's would, with nothing on the claim saying this is a merge task.
    agent(
        "git checkout -q -b sidebranch\n"
        "printf 'line1\\nline2\\nline3\\nline4\\nSIDE-5\\n' > shared.txt\n"
        "git add -A\n"
        "git -c user.name=t -c user.email=t@invalid commit -q -m side\n"
        "git checkout -q task/T1\n"
        # task/T1 has to move too, or merging its own descendant is a fast-forward and there is no
        # conflict to leave behind — which is how the first version of this test passed vacuously.
        "printf 'line1\\nline2\\nline3\\nline4\\nMINE-5\\n' > shared.txt\n"
        "git add -A\n"
        "git -c user.name=t -c user.email=t@invalid commit -q -m mine\n"
        "git merge --no-edit sidebranch >/dev/null 2>&1\n"
        "printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"left it for you\"}\\n'\n")
    reported = []
    monkeypatch.setattr(tasks, "report_once",
                        lambda _s, tid, **kw: reported.append((tid, kw)))

    tasks._run_and_report(_FakeState(), _job_with_input(input_commit))

    assert reported, "nothing was reported at all"
    _task_id, fields = reported[0]
    assert fields["state"] == "completed", fields


def _relay_git_url(monkeypatch, url):
    """Point the loop's remote-URL builder at a local bare repo, so the real commit/push path runs."""
    from remote import relay
    monkeypatch.setattr(relay, "git_remote_url", lambda _signaling, _project_id: url)


class _RecordingPublisher:
    def __init__(self):
        self.published = []

    def publish(self, kind, *, blocking=True, **fields):
        # `True` is part of the real publisher's shape: it answers whether it ACCEPTED the event, and
        # `task_tree.WorkspaceTree` only stops re-sending a snapshot once it hears yes.
        self.published.append((kind, fields))
        return True

    def close(self):
        pass


def _job_with_input(commit, branch="task/T1", member_key=_MEMBER,
                    conversation_id=_CONVERSATION, **overrides):
    job = {"task_id": "T1", "prompt": "p", "project_id": "proj-1", "member_key": member_key,
           # ⚠️ `task_id` is the TURN and `conversation_id` is the CONVERSATION — two keys for two
           # objects (ADR 0034 D-a). `run_task` REFUSES a claim without the second, so every job
           # dict here needs it even when the test is about something else.
           "conversation_id": conversation_id,
           "branch": branch, "input_commit": commit}
    # `**overrides` rather than a named `transcript_commit=` parameter: this dict IS the claim
    # payload, and a test about a key the relay may or may not send should be able to add or omit it
    # the way the relay does, without this helper growing a keyword per wire value.
    job.update(overrides)
    return job


def test_the_agents_work_is_pushed_and_the_conversation_no_longer_travels_with_it(
        agent, tmp_path, monkeypatch):
    """The agent's work is in the commit the relay is told about; the conversation is NOT.

    ⚠️ **This is a DELIBERATE FLIP of what this test asserted before ADR 0034 D-j (issue 39), and
    the flip is the point rather than a side effect.** It used to require
    `.grid/agent/<member_key>/` in the result commit and — in its last two lines — to assert the
    cross-member leak as a known limit, with a message saying that if a later slice closed it, this
    assertion was the one to flip. This is that slice, and this is that flip.

    What changed underneath it: issue 06 put the transcript in the result commit so it would reach
    the next provider, which was right while there was one session per MEMBER. Issue 38 gave every
    CONVERSATION its own session, so the same arrangement writes 105 KB-2 MB of `.jsonl` into the
    shared trunk on every turn and hands every member every other member's. The transcript now
    travels on `refs/grid/agent/<conversation_id>` instead, pushed by the lease holder.

    ⚠️ **The fixture seeds a SECOND MEMBER'S TRANSCRIPT AS A TRACKED FILE, and that is the whole
    test rather than scenery.** `$GIT_DIR/info/exclude` has no say over files git already tracks, so
    simply removing the force-add leaves `git add -A` staging every modification to them forever.
    "The trunk holds no transcripts" then passes on a fresh project and fails on every project that
    has ever run a task — which is every real one. Only `git rm --cached` closes it, and only a
    fixture with a tracked transcript can tell the two implementations apart.

    Issue 05 excluded all of `.grid/`; issue 06 carved `.grid/agent/` back in; this restores the
    exclusion for a different reason than 05 had. The relay's refusal to accept an UPLOAD anywhere
    under `.grid/` is unchanged throughout.
    """
    from remote import tasks

    remote, commit = _remote_for(
        tmp_path, "task/T1",
        {"a.txt": "x\n", f".grid/agent/{_OTHER_MEMBER}/sess-9.jsonl": "someone else's\n"})
    _relay_git_url(monkeypatch, remote.url)
    agent(f"mkdir -p .grid/agent/{_MEMBER}/memory\n"
          f"echo transcript > .grid/agent/{_MEMBER}/sess-1.jsonl\n"
          f"echo remembered > .grid/agent/{_MEMBER}/memory/note.md\n"
          f"echo tampered > .grid/agent/{_OTHER_MEMBER}/sess-9.jsonl\n"
          "echo provider-only > .grid/scratch.txt\n"
          "echo fixed > fix.py\n"
          "printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")
    reported = {}
    monkeypatch.setattr(tasks, "report_once", lambda _s, tid, **kw: reported.update(kw))
    monkeypatch.setattr(tasks, "_publisher_for", lambda *a, **k: _NullPublisher())

    tasks._run_and_report(_FakeState(), _job_with_input(commit))

    assert reported["state"] == "completed"
    pushed = reported["result_commit"]
    assert pushed and pushed != commit, "nothing new was pushed"
    listing = _git(remote.url, "ls-tree", "-r", "--name-only", pushed).stdout.split()
    assert "fix.py" in listing, listing
    assert ".grid/scratch.txt" not in listing, listing
    # The flip. Not "this member's transcript is absent" but "NO transcript is", including the one
    # that was already tracked when the task started — which is the half a fresh-project test cannot
    # see and the half `git rm --cached` exists for.
    assert not [path for path in listing if path.startswith(".grid/")], (
        f"the result commit still carries the reserved directory, so every turn adds a transcript "
        f"to the shared trunk and every member's clone holds every other member's: {listing}")


def test_the_projects_own_gitignore_cannot_suppress_the_conversation(
        agent, tmp_path, monkeypatch):
    """A **tracked** `.gitignore` outranks `$GIT_DIR/info/exclude`, so the conversation is
    force-added onto its side ref rather than merely un-excluded.

    ⚠️ **The rule survived ADR 0034 D-j; only the ref it applies to moved.** Before issue 39 the
    force-add was in `commit_and_push` and put the transcript in the result commit. That add is gone,
    but `push_transcript` still stages with `-f`, and for exactly the reason issue 06 established —
    which is why this test was re-aimed rather than deleted.

    Entirely plausible input: the requesting user's repository is an AI-agent project that ignores
    `*.jsonl` or `memory/`, or the agent — running under `bypassPermissions` — writes a `.gitignore`
    itself. The failure it produces is the silent kind this feature is most exposed to: the task
    still reports `completed` with a session id, the transcript simply never reaches the ref, and
    nothing surfaces until a *different* provider finds no conversation to resume and reports the
    misleading "no transcript for session X in this workspace".

    The same provider masks it for a while, too — its local `.grid/agent/` survives
    `clean -ffdx -e .grid` whether or not it was ever pushed.
    """
    from remote import task_repo, tasks

    remote, commit = _remote_for(
        tmp_path, "task/T1",
        {"a.txt": "x\n", ".gitignore": ".grid/agent/\n*.jsonl\nmemory/\n"})
    _relay_git_url(monkeypatch, remote.url)
    agent(f"mkdir -p .grid/agent/{_MEMBER}/memory\n"
          f"echo transcript > .grid/agent/{_MEMBER}/sess-1.jsonl\n"
          f"echo remembered > .grid/agent/{_MEMBER}/memory/note.md\n"
          "printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")
    reported = {}
    monkeypatch.setattr(tasks, "report_once", lambda _s, tid, **kw: reported.update(kw))
    monkeypatch.setattr(tasks, "_publisher_for", lambda *a, **k: _NullPublisher())

    tasks._run_and_report(_FakeState(), _job_with_input(commit))

    assert reported["state"] == "completed"
    ref = task_repo.transcript_ref(_CONVERSATION)
    listing = _git(remote.url, "ls-tree", "-r", "--name-only", ref).stdout.split()
    assert f".grid/agent/{_MEMBER}/sess-1.jsonl" in listing, listing
    assert f".grid/agent/{_MEMBER}/memory/note.md" in listing, listing
    # And still not in the project's own history, which is the other half of the same rule.
    assert ".grid" not in _git(
        remote.url, "ls-tree", "-r", "--name-only", reported["result_commit"]).stdout


def test_two_members_tasks_on_one_provider_do_not_reset_each_others_workspace(
        agent, tmp_path, monkeypatch):
    """Issue 11's first acceptance criterion, run through the real checkout rather than the lock.

    `_reserve_workspace` keeps two supervisors out of ONE directory; this is the other half — that
    the two members do not share a directory in the first place. It matters because `materialize`
    opens with `reset --hard` and `clean -ffdx`, so a shared workspace does not merely confuse the
    second task, it deletes the first member's work and their conversation on the way in.

    Sequential rather than concurrent on purpose: `clean -ffdx` is what has to be shown to miss, and
    a race would prove the lock instead of the path.
    """
    from remote import task_agent, tasks

    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    _relay_git_url(monkeypatch, remote.url)
    monkeypatch.setattr(tasks, "report_once", lambda _s, tid, **kw: None)
    monkeypatch.setattr(tasks, "_publisher_for", lambda *a, **k: _NullPublisher())

    agent(f"mkdir -p .grid/agent/{_MEMBER}\n"
          f"echo alices-conversation > .grid/agent/{_MEMBER}/sess-a.jsonl\n"
          "echo alices-work > mine.txt\n"
          "printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")
    first = tasks.run_task(_job_with_input(commit, member_key=_MEMBER), remote=remote)
    assert first.state == "completed", first.error

    # The second member's task, in the SAME project, on this same provider — an untracked file and
    # a full `reset --hard` + `clean -ffdx` of its own.
    agent("echo bobs-work > mine.txt\n"
          "printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")
    second = tasks.run_task(
        _job_with_input(commit, member_key=_OTHER_MEMBER), remote=remote)
    assert second.state == "completed", second.error

    mine = task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION)
    theirs = task_agent.workspace_for("proj-1", _OTHER_MEMBER, _CONVERSATION)
    assert mine != theirs
    assert (mine / "mine.txt").read_text(encoding="utf-8") == "alices-work\n", (
        "the second member's checkout reset the first member's workspace")
    assert (theirs / "mine.txt").read_text(encoding="utf-8") == "bobs-work\n"
    # And the conversation with it — `clean -ffdx -e .grid` spares `.grid`, but a SHARED workspace
    # would have had the second task's `link_transcript` and commit acting on the same directory.
    assert (task_agent.transcript_dir(mine, _MEMBER) / "sess-a.jsonl").is_file()
    assert not (task_agent.transcript_dir(theirs, _OTHER_MEMBER) / "sess-a.jsonl").exists()


def test_a_claim_with_no_member_key_is_refused_instead_of_falling_back_to_the_project(
        agent, tmp_path, monkeypatch):
    """Issue 11's fourth acceptance criterion. The one wire field on this path that is not optional.

    A fallback to `<root>/projects/<project_id>/workspace` would look like it worked: the agent
    runs, the result pushes, the task reports `completed`. What it actually does is change
    `transcript_dir_name(cwd)`, so Claude Code writes the conversation somewhere the next task never
    looks — that member's history is then permanently unresumable, with every other signal healthy.

    So it is a refusal, and a TERMINAL one: no provider can fix it and every retry finds the same
    answer, so the user gets the reason now rather than `retries_exhausted` in three lease TTLs.
    """
    from remote import task_agent, tasks

    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    agent("echo should-never-run > ran.txt\n"
          "printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")
    keyless = _job_with_input(commit)
    del keyless["member_key"]

    outcome = tasks.run_task(keyless, remote=remote)

    assert outcome.state == "failed"
    assert "member_key" in (outcome.error or ""), outcome.error
    # No fallback path was created, let alone run in. Asserted on the filesystem rather than on the
    # message, because the message is what a future edit would keep while quietly restoring the
    # fallback underneath it.
    root = task_agent.workspace_root()
    assert not (root / "projects" / "proj-1" / "workspace").exists(), (
        "a project-level workspace was created — the fallback this refusal exists to prevent")
    assert not list((root / "projects").glob("proj-1/*/workspace/ran.txt")), "the agent ran"


def test_a_claim_with_no_conversation_id_is_refused_instead_of_falling_back_to_the_member(
        agent, tmp_path, monkeypatch):
    """ADR 0034 D-c, and exactly `member_key`'s class one level down (issue 38).

    A fallback to `<root>/projects/<project_id>/<member_key>/workspace` would look like it worked:
    the agent runs, the result pushes, the turn reports `completed`. What it actually does is change
    `transcript_dir_name(cwd)`, so Claude Code writes the conversation somewhere the next turn of it
    never looks — that conversation is then permanently unresumable, with every other signal healthy.

    TERMINAL for `member_key`'s reason: no provider can fix a relay that does not send the key, and
    every retry finds the same answer, so the user gets the reason now rather than
    `retries_exhausted` in three lease TTLs. It is also what makes a version skew loud — hence: roll
    the relay out BEFORE the provider fleet.
    """
    from remote import task_agent, tasks

    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    agent("echo should-never-run > ran.txt\n"
          "printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")
    keyless = _job_with_input(commit)
    del keyless["conversation_id"]

    outcome = tasks.run_task(keyless, remote=remote)

    assert outcome.state == "failed"
    assert "conversation_id" in (outcome.error or ""), outcome.error
    # ⚠️ **A DELIBERATE refusal, not a crash that happens to mention the key.** Without this arm the
    # test passes with no refusal written at all: `workspace_for` raises `TypeError: missing 1
    # required positional argument: 'conversation_id'`, `run_task`'s guarded block catches it, and
    # the generic "could not start the agent: …" wrapper carries the key's name for free. That
    # reads as green while an operator gets a Python signature in place of an instruction, and the
    # filesystem assertions below hold for a crash exactly as they do for a refusal.
    assert "Upgrade the relay" in (outcome.error or ""), (
        f"the failure does not tell the operator what to do, so it is a crash rather than the "
        f"refusal ADR 0034 D-c asks for: {outcome.error!r}")
    assert "could not start the agent" not in (outcome.error or ""), (
        f"the refusal arrived through the generic startup handler, which means nothing refused "
        f"this claim on purpose: {outcome.error!r}")
    # On the FILESYSTEM, not on the message, for the reason the member_key test gives: the message
    # is what a future edit keeps while quietly restoring the fallback underneath it.
    root = task_agent.workspace_root()
    assert not (root / "projects" / "proj-1" / _MEMBER / "workspace").exists(), (
        "a member-level workspace was created — the fallback this refusal exists to prevent")
    assert not list((root / "projects").glob("proj-1/*/*/workspace/ran.txt")), "the agent ran"


def test_the_two_missing_key_refusals_do_not_read_the_same(agent, tmp_path, monkeypatch):
    """ADR 0034 D-c asks for a message DISTINCT from the missing-`member_key` one, and this is why.

    Both mean "upgrade the relay", but they arrive at different releases and name different keys.
    Fused into one sentence, an operator whose relay is missing `conversation_id` reads advice about
    ADR 0033 issue 11 — a slice they already deployed — and goes looking for a problem that is not
    there. Pinned rather than left to review, because merging two similar strings is exactly the
    kind of tidying that looks like an improvement.
    """
    from remote import tasks

    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    agent("printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")

    no_member = _job_with_input(commit)
    del no_member["member_key"]
    no_conversation = _job_with_input(commit)
    del no_conversation["conversation_id"]

    first = tasks.run_task(no_member, remote=remote).error
    second = tasks.run_task(no_conversation, remote=remote).error

    assert first != second, "the two refusals are the same sentence"
    assert "conversation_id" not in (first or ""), (
        "the missing-member_key refusal names conversation_id too, so neither message identifies "
        "which key is actually absent")
    assert "member_key" not in (second or ""), (
        "the missing-conversation_id refusal names member_key too, and sends an operator to check "
        "a slice they already deployed")


def test_a_task_whose_agent_left_no_transcript_still_commits_and_pushes(
        agent, tmp_path, monkeypatch):
    """The forced add names a path that need not exist — `git add` calls an unmatched pathspec an
    error, and a run that died before the agent wrote anything must still push (ADR 0032 D-e: a
    failed attempt is committed and pushed too, so the user can see what happened)."""
    from remote import task_agent, tasks

    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    _relay_git_url(monkeypatch, remote.url)
    agent("echo fixed > fix.py\n"
          "printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")
    # Remove the directory `link_transcript` created, so the pathspec genuinely matches nothing.
    monkeypatch.setattr(tasks, "report_once", lambda _s, tid, **kw: None)
    monkeypatch.setattr(tasks, "_publisher_for", lambda *a, **k: _NullPublisher())
    outcome = tasks.run_task(_job(input_commit=commit, branch="task/T1"), remote=remote)
    assert outcome.state == "completed", outcome.error
    workspace = task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION)
    import shutil
    shutil.rmtree(task_agent.transcript_dir(workspace, _MEMBER))

    from remote import task_repo
    pushed = task_repo.commit_and_push(
        workspace, url=remote.url, token=remote.token, branch="task/T1", message="task T1")

    listing = _git(remote.url, "ls-tree", "-r", "--name-only", pushed.commit).stdout.split()
    assert "fix.py" in listing, listing


def test_a_failed_task_still_commits_and_pushes_its_branch(agent, tmp_path, monkeypatch):
    """ADR 0032 D-e: a failed attempt still commits and still pushes, so the user can see what the
    agent did before it broke and cherry-pick what was right. Only `main` is withheld, and that is
    the relay's decision, not this one."""
    from remote import tasks

    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    _relay_git_url(monkeypatch, remote.url)
    agent("echo half-done > progress.txt\n"
          "printf '{\"type\":\"result\",\"is_error\":true,\"subtype\":\"error_max_turns\"}\\n'\n")
    reported = {}
    monkeypatch.setattr(tasks, "report_once", lambda _s, tid, **kw: reported.update(kw))
    monkeypatch.setattr(tasks, "_publisher_for", lambda *a, **k: _NullPublisher())

    tasks._run_and_report(_FakeState(), _job_with_input(commit))

    assert reported["state"] == "failed"
    pushed = reported["result_commit"]
    assert pushed and pushed != commit
    assert "progress.txt" in _git(remote.url, "ls-tree", "-r", "--name-only", pushed).stdout


def test_an_agent_that_changed_nothing_still_produces_a_result_commit(
        agent, tmp_path, monkeypatch):
    """One code path, not two. An agent that changed nothing is an ordinary outcome, and an empty
    commit says so truthfully — while branching on `status --porcelain` would leave `result_commit`
    meaning "what the agent produced" sometimes and "the input" other times."""
    from remote import tasks

    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    _relay_git_url(monkeypatch, remote.url)
    agent("printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"nothing to do\"}\\n'\n")
    reported = {}
    monkeypatch.setattr(tasks, "report_once", lambda _s, tid, **kw: reported.update(kw))
    monkeypatch.setattr(tasks, "_publisher_for", lambda *a, **k: _NullPublisher())

    tasks._run_and_report(_FakeState(), _job_with_input(commit))

    assert reported["state"] == "completed"
    assert reported["result_commit"] not in (None, "", commit)
    assert _git(remote.url, "rev-parse", "refs/heads/task/T1").stdout.strip() \
        == reported["result_commit"]


def test_a_push_that_fails_reports_no_terminal_state_at_all(agent, tmp_path, monkeypatch):
    """THE criterion: a failed push leaves the task in a state issue 07 can RETRY, not one that
    looks complete.

    Reporting `failed` would be terminal, and terminal is exactly what nothing retries — the result
    would be lost with a tidy-looking record saying so. Staying silent lets the lease lapse, which
    is the one path that can still produce what the user asked for.

    Both halves are asserted, because either alone passes for the wrong reason: that NOTHING was
    reported, and that the reason still reached the user's event log. A silent abandon would satisfy
    the first and be exactly the failure this test exists to prevent.
    """
    from remote import task_repo, tasks

    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    _relay_git_url(monkeypatch, remote.url)
    agent("echo fixed > fix.py\n"
          "printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")

    def refuse(*_a, **_k):
        raise task_repo.PushError("could not push task/T1: the relay refused")

    monkeypatch.setattr(task_repo, "commit_and_push", refuse)
    reports = []
    monkeypatch.setattr(tasks, "report_once", lambda *a, **k: reports.append(k))
    publisher = _RecordingPublisher()
    monkeypatch.setattr(tasks, "_publisher_for", lambda *a, **k: publisher)

    tasks._run_and_report(_FakeState(), _job_with_input(commit))

    assert reports == [], "a terminal state was reported for a result that is not in the repository"
    assert any(kind == "task.stderr" and "the relay refused" in fields.get("text", "")
               for kind, fields in publisher.published), publisher.published


def test_a_transcript_push_that_fails_reports_no_terminal_state_either(
        agent, tmp_path, monkeypatch):
    """ADR 0034 D-j: *"a failed push of the side ref fails the turn too — best-effort here means
    conversations evaporate in silence."*

    "Fails the turn" is spelled here exactly as a failed RESULT push already is, and that is a
    decision rather than an accident: report nothing terminal, let the lease lapse, let the reaper
    reclaim. A terminal `failed` would be louder and worse — it would spend the whole turn on a
    transient network fault, and no retry could ever recover a conversation the relay never
    received. What D-j forbids is the third option, carrying on as though the push had worked.

    Both halves are asserted, for the reason the result-push test beside this one gives: that
    NOTHING was reported, and that the reason still reached the user's event log. A silent abandon
    satisfies the first and is precisely the failure being prevented.
    """
    from remote import task_repo, tasks

    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    _relay_git_url(monkeypatch, remote.url)
    agent(f"mkdir -p .grid/agent/{_MEMBER}\n"
          f"echo transcript > .grid/agent/{_MEMBER}/sess-1.jsonl\n"
          "echo fixed > fix.py\n"
          "printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")

    def refuse(*_a, **_k):
        raise task_repo.PushError(
            "could not push the conversation's transcript to refs/grid/agent/c1: the relay refused")

    monkeypatch.setattr(task_repo, "push_transcript", refuse)
    reports = []
    monkeypatch.setattr(tasks, "report_once", lambda *a, **k: reports.append(k))
    publisher = _RecordingPublisher()
    monkeypatch.setattr(tasks, "_publisher_for", lambda *a, **k: publisher)

    tasks._run_and_report(_FakeState(), _job_with_input(commit))

    assert reports == [], (
        "a terminal state was reported for a turn whose conversation never reached the relay — "
        "the turn looks finished and the conversation is gone")
    assert any(kind == "task.stderr" and "the relay refused" in fields.get("text", "")
               for kind, fields in publisher.published), publisher.published


def test_a_transcript_the_relay_pinned_that_cannot_be_fetched_does_not_start_a_fresh_session(
        agent, tmp_path, monkeypatch):
    """ADR 0034 D-j: *"missing transcript" stops being one fact.*

    Before this slice, no transcript on disk meant the predecessor legitimately produced none, and
    starting fresh was right. Now it can also mean *the relay has one and this provider failed to
    fetch it* — and the provider cannot tell those apart by looking at the workspace. The pin is what
    distinguishes them: a turn the relay pinned has a transcript waiting for it, so a fetch that
    fails must NOT fall through to `resumable_session` and quietly start over.

    The fetch failure is an `InputFetchError`, which reports nothing terminal and lets the reaper
    retry — the same treatment the input fetch already gets, and for the same reason: another
    provider may well succeed.

    ⚠️ The alternative this forbids is the silent one. A fresh session here completes the turn,
    returns a session id, pushes a result and reads healthy everywhere, while the person watching
    finds the agent has forgotten everything they told it.
    """
    from remote import tasks

    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    _relay_git_url(monkeypatch, remote.url)
    agent("printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")
    reports = []
    monkeypatch.setattr(tasks, "report_once", lambda *a, **k: reports.append(k))
    monkeypatch.setattr(tasks, "_publisher_for", lambda *a, **k: _NullPublisher())

    # A pin naming a commit this project's repository has never held: the shape of a provider that
    # cannot reach the ref, or a relay that collected it early.
    tasks._run_and_report(
        _FakeState(),
        _job_with_input(commit, transcript_commit="b" * 40))

    assert reports == [], (
        "a turn whose pinned transcript could not be fetched reported a terminal state instead of "
        "being left for the reaper — so it either started a fresh session in silence or failed "
        "permanently on something another provider could have done")


def test_a_claim_with_no_pinned_transcript_fetches_nothing_and_starts_fresh(
        agent, tmp_path, monkeypatch):
    """The old-relay degrade, and the one direction that must never be a failure (ADR 0034 D-j).

    A relay predating issue 39 sends no `transcript_commit` at all, and a conversation's FIRST turn
    on a current relay sends `None`. Both mean the same thing — there is nothing to resume — and both
    must run exactly as they did before this slice: no fetch, no refusal, a fresh session.

    That asymmetry is why the rollout order is the relay before the fleet: an un-upgraded provider
    ignores the key, and an un-upgraded relay simply never sends it.
    """
    from remote import tasks

    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    _relay_git_url(monkeypatch, remote.url)
    agent("printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")
    reported = {}
    monkeypatch.setattr(tasks, "report_once", lambda _s, tid, **kw: reported.update(kw))
    monkeypatch.setattr(tasks, "_publisher_for", lambda *a, **k: _NullPublisher())

    tasks._run_and_report(_FakeState(), _job_with_input(commit))

    assert reported["state"] == "completed", reported


def test_the_reserved_directory_exclude_is_one_uniform_line_and_this_slice_did_not_touch_it(
        tmp_path):
    """ADR 0034 D-j's first correction, asserted rather than assumed.

    The first draft of issue 39 made "collapse `$GIT_DIR/info/exclude` to one uniform line" an
    acceptance criterion — which passes today with no code written, because `_ensure_repo` has
    always written exactly `/.grid/`, unconditionally and with no per-member shape. Nothing in this
    repository asserted that, so the criterion could neither be verified nor falsified.

    It matters beyond bookkeeping: this file is what keeps the provider's own state out of the
    requesting user's repository under `git add -A`, and since issue 39 it is also what keeps the
    transcript out. A `!` negation added here would be the silent failure issue 06 documented — a
    *tracked* `.gitignore` outranks this file, so the carve-out would work on most projects and
    vanish on the ones that ignore `*.jsonl`.
    """
    from remote import task_repo

    workspace = tmp_path / "ws"
    workspace.mkdir()
    task_repo._ensure_repo(workspace)

    exclude = (workspace / ".git" / "info" / "exclude").read_text()

    assert exclude.splitlines() == ["# written by grid — see ADR 0032", "/.grid/"], (
        f"the reserved-directory exclude is no longer one uniform line: {exclude!r}")


def test_a_task_with_no_git_plane_reports_normally_and_pushes_nothing(agent, monkeypatch):
    """Old-relay degrade, on the push side. A claim with no `input_commit` has no branch to push,
    so the loop must report exactly as it did before the git plane existed — degrading to the
    PREVIOUS behaviour, never to a new failure."""
    from remote import tasks

    agent("printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")
    reported = {}
    monkeypatch.setattr(tasks, "report_once", lambda _s, tid, **kw: reported.update(kw))
    monkeypatch.setattr(tasks, "_publisher_for", lambda *a, **k: _NullPublisher())

    tasks._run_and_report(_FakeState(), {"task_id": "T1", "prompt": "p", "project_id": "proj-1",
                                         "member_key": _MEMBER,
                                         "conversation_id": _CONVERSATION})

    assert reported["state"] == "completed"
    assert reported["result_commit"] is None


# --- issue 08: the live workspace tree -----------------------------------------------------------

def _fast_heartbeat(monkeypatch, interval=0.05):
    """Beat the lease at test speed, with the wire stubbed out.

    The interval is forced at CONSTRUCTION rather than by patching `RENEW_INTERVAL_SECONDS`: that
    constant is a default argument, bound when the module was imported, so patching it afterwards
    changes nothing and the test would pass a 30-second wait it never actually took.
    """
    from remote import task_lease

    real = task_lease.LeaseRenewer
    monkeypatch.setattr(task_lease.relay, "renew_task_lease", lambda *_a, **_k: None)
    monkeypatch.setattr(
        task_lease, "LeaseRenewer",
        lambda state, task_id, **kw: real(state, task_id, **{**kw, "interval": interval}))


def test_the_client_sees_the_workspace_change_while_the_task_runs(agent, tmp_path, monkeypatch):
    """The issue's demo, end to end through the real loop: a file the agent creates mid-run reaches
    the event log BEFORE the task ends.

    That "before" is the whole feature. The provider commits only at terminal boundaries (ADR 0032
    D-e), so between claim and terminal the repository holds nothing new and this stream is the only
    live view there is.
    """
    from remote import tasks

    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    _relay_git_url(monkeypatch, remote.url)
    _fast_heartbeat(monkeypatch)
    agent("echo made > made.py\n"
          "sleep 1\n"
          "printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")
    publisher = _RecordingPublisher()
    monkeypatch.setattr(tasks, "_publisher_for", lambda *_a, **_k: publisher)
    monkeypatch.setattr(tasks, "report_once", lambda *_a, **_k: None)

    tasks._run_and_report(_FakeState(), _job_with_input(commit))

    trees = [fields for kind, fields in publisher.published if kind == "task.tree"]
    assert trees, f"no tree snapshot was published at all: {publisher.published}"
    assert "made.py" in trees[-1]["paths"]
    assert "a.txt" in trees[-1]["paths"], "the task's input should be in the view too"


def test_the_tree_beat_and_the_push_follow_the_conversation_not_just_the_member(
        agent, tmp_path, monkeypatch):
    """ADR 0034 D-c's third warning: the workspace path is built in THREE places in this file.

    `run_task` builds it, `_tree_beat` builds it AGAIN, and `_push_result` builds it a third time —
    and the last two re-read `member_key` off the job rather than taking `run_task`'s local, so a
    change threaded through `run_task` alone leaves them one level up. Neither says so:

      * the tree beat degrades to `None` on any fault and `_complain` suppresses a repeat, so a
        wrong path is ONE stderr line for a whole run and a live file view that is simply absent;
      * the push would commit from a worktree that is not the one the agent ran in.

    So both are driven here, across two conversations of ONE member, and the assertion is that the
    second reports the second's work and NOT the first's. A member-keyed path would give both turns
    one directory, which is the state that makes each of them unresumable.
    """
    from remote import tasks

    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    _git(remote.url, "branch", "task/T2", commit)
    _relay_git_url(monkeypatch, remote.url)
    _fast_heartbeat(monkeypatch)
    monkeypatch.setattr(tasks, "report_once", lambda _s, tid, **kw: reported.setdefault(tid, kw))
    reported: dict = {}

    def _run(task_id, branch, conversation_id, filename):
        publisher = _RecordingPublisher()
        monkeypatch.setattr(tasks, "_publisher_for", lambda *_a, **_k: publisher)
        agent(f"echo made > {filename}\n"
              "sleep 1\n"
              "printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")
        job = _job_with_input(commit, branch=branch, conversation_id=conversation_id)
        job["task_id"] = task_id
        tasks._run_and_report(_FakeState(), job)
        return [f for kind, f in publisher.published if kind == "task.tree"]

    first_trees = _run("T1", "task/T1", _CONVERSATION, "first-only.py")
    second_trees = _run("T2", "task/T2", _OTHER_CONVERSATION, "second-only.py")

    # The live tree beat, for the SECOND conversation.
    assert second_trees, (
        "the second conversation published no tree at all — `_tree_beat` built a path that is not "
        "the workspace the agent ran in, and its only complaint is one line on stderr")
    assert "second-only.py" in second_trees[-1]["paths"]
    assert "first-only.py" not in second_trees[-1]["paths"], (
        "the second conversation's live view shows the first conversation's work, so the two are "
        "sharing one workspace")
    assert first_trees and "first-only.py" in first_trees[-1]["paths"]

    # The result push, same two conversations, same question.
    assert reported["T2"]["state"] == "completed", reported["T2"]
    listing = _git(
        remote.url, "ls-tree", "-r", "--name-only", reported["T2"]["result_commit"]).stdout.split()
    assert "second-only.py" in listing, listing
    assert "first-only.py" not in listing, (
        f"the second conversation pushed the first one's file, so `_push_result` committed from "
        f"the wrong worktree: {listing}")


def test_a_tree_that_cannot_be_read_at_all_still_lets_the_task_finish(agent, tmp_path, monkeypatch):
    """The isolation criterion at the level that matters: the RESULT.

    The unit tests prove `WorkspaceTree.beat` swallows its failures. This proves the wiring does too
    — a tree that cannot even be constructed must cost a view of a directory and nothing else.
    """
    from remote import task_tree, tasks

    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    _relay_git_url(monkeypatch, remote.url)
    _fast_heartbeat(monkeypatch)

    def _refuse(*_a, **_k):
        raise RuntimeError("no tree for you")

    monkeypatch.setattr(task_tree, "WorkspaceTree", _refuse)
    agent("printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")
    reported = {}
    monkeypatch.setattr(tasks, "report_once", lambda _s, tid, **kw: reported.update(kw))
    monkeypatch.setattr(tasks, "_publisher_for", lambda *_a, **_k: _NullPublisher())

    tasks._run_and_report(_FakeState(), _job_with_input(commit))

    assert reported["state"] == "completed"
    assert reported["result_commit"] not in (None, "", commit)


# --- issue 05: `grid task fetch` ------------------------------------------------------------------

def test_the_client_fetches_exactly_what_the_agent_produced(tmp_path, monkeypatch, capsys):
    """The issue's closing criterion, from the client's end: the file the agent wrote, byte for byte.

    Driven through `checkout_result` against a real repository rather than through mocks — what is
    being claimed is that git puts the right bytes on disk, and a mock cannot be wrong about that.
    """
    from remote import task_repo

    remote, commit = _remote_for(tmp_path, "task/T1", {"fix.py": "print(1)\n", "notes.md": "why\n"})
    dest = tmp_path / "result"
    dest.mkdir()

    task_repo.checkout_result(dest, url=remote.url, token="tok",
                              branch="task/T1", commit=commit)

    assert (dest / "fix.py").read_text() == "print(1)\n"
    assert (dest / "notes.md").read_text() == "why\n"


def test_fetching_never_destroys_work_already_in_the_destination(tmp_path):
    """`checkout_result` is deliberately NOT `materialize`. The provider's workspace is the
    provider's, so resetting it hard is right; a directory a user named is theirs, and a fetch that
    silently deleted their unrelated file would be unforgivable."""
    from remote import task_repo

    remote, commit = _remote_for(tmp_path, "task/T1", {"fix.py": "print(1)\n"})
    dest = tmp_path / "result"
    dest.mkdir()
    (dest / "my-notes.txt").write_text("mine\n")

    task_repo.checkout_result(dest, url=remote.url, token="tok",
                              branch="task/T1", commit=commit)

    assert (dest / "my-notes.txt").read_text() == "mine\n"
    assert (dest / "fix.py").read_text() == "print(1)\n"


def test_the_clients_token_is_never_written_into_the_clones_config(tmp_path, monkeypatch):
    """A grid access token lives a year, and a result directory is a thing users hand around, zip
    up and commit. Persisting the credential into `.git/config` would turn every fetched result
    into a copy of it, so the token is supplied per invocation and left nowhere."""
    from remote import task_repo

    remote, commit = _remote_for(tmp_path, "task/T1", {"fix.py": "print(1)\n"})
    dest = tmp_path / "result"
    dest.mkdir()

    task_repo.checkout_result(dest, url=remote.url, token="SEKRIT",
                              branch="task/T1", commit=commit)

    on_disk = "\n".join(
        path.read_text(errors="replace")
        for path in (dest / ".git").rglob("*") if path.is_file())
    assert "SEKRIT" not in on_disk


def test_a_refused_push_raises_push_error_and_not_checkout_error(tmp_path):
    """The two exceptions mean opposite things to a caller, so the constructor must not leak the
    wrong one: a `CheckoutError` says "fail the task", a `PushError` says "report nothing and let
    the lease lapse". `commit_and_push` runs `_run`, which raises `CheckoutError` for every git
    failure it sees — so without the translation the push path would inherit the checkout path's
    meaning, and a lost result would be recorded as a finished, failed task nothing ever retries.
    """
    from remote import task_repo

    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    workspace = tmp_path / "ws"
    workspace.mkdir()
    task_repo.materialize(workspace, url=remote.url, token="tok",
                          branch="task/T1", input_commit=commit)
    (workspace / "fix.py").write_text("print(1)\n")

    with pytest.raises(task_repo.PushError) as caught:
        task_repo.commit_and_push(
            workspace, url=str(tmp_path / "no-such-repo.git"), token="tok",
            branch="task/T1", message="task T1 (completed)")

    assert not isinstance(caught.value, task_repo.CheckoutError)
    # Still carries git's own words, which are the only thing that says WHY.
    assert "task/T1" in str(caught.value)


def test_a_push_that_is_refused_leaves_the_result_committed_locally(tmp_path):
    """The commit happens before the push, so a failed push does not also lose the work.

    That matters for the retry path: the same provider reclaiming the task, or an operator looking
    at the workspace, still finds what the agent produced rather than an uncommitted tree that the
    next attempt's `reset --hard` would erase.
    """
    from remote import task_repo

    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    workspace = tmp_path / "ws"
    workspace.mkdir()
    task_repo.materialize(workspace, url=remote.url, token="tok",
                          branch="task/T1", input_commit=commit)
    (workspace / "fix.py").write_text("print(1)\n")

    with pytest.raises(task_repo.PushError):
        task_repo.commit_and_push(workspace, url=str(tmp_path / "gone.git"), token="tok",
                                  branch="task/T1", message="task T1 (completed)")

    assert _git(workspace, "rev-parse", "HEAD").stdout.strip() != commit
    assert "fix.py" in _git(workspace, "show", "--name-only", "--format=", "HEAD").stdout


def test_a_workspace_that_could_not_be_prepared_pushes_nothing(agent, tmp_path, monkeypatch):
    """A `materialize` that fails PART WAY must not publish a result, and "is the workspace a git
    repo" cannot tell that apart from success.

    `_ensure_repo` is the first thing `materialize` does, and the workspace persists per project —
    so after any task has ever run, `.git` is there for good. Here the fetch and `symbolic-ref`
    succeed (leaving HEAD legitimately on the task branch) and `clean` fails, exactly as it would
    with an un-removable leftover from a previous attempt. Without a check on the SPAWN, the commit
    and push then succeed and the relay is handed a `result_commit` full of another task's files,
    attributed to an agent that never started — the confidently-wrong answer from the other end.
    """
    from remote import task_repo, tasks

    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    _relay_git_url(monkeypatch, remote.url)
    agent("printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")
    real_run = task_repo._run

    def fail_on_clean(workspace, *args, **kwargs):
        if args and args[0] == "clean":
            raise task_repo.CheckoutError("git clean failed (1): Permission denied")
        return real_run(workspace, *args, **kwargs)

    monkeypatch.setattr(task_repo, "_run", fail_on_clean)
    reported = {}
    monkeypatch.setattr(tasks, "report_once", lambda _s, tid, **kw: reported.update(kw))
    monkeypatch.setattr(tasks, "_publisher_for", lambda *a, **k: _NullPublisher())

    tasks._run_and_report(_FakeState(), _job_with_input(commit))

    assert reported["state"] == "failed"
    assert reported["result_commit"] is None, "a result was published for an agent that never ran"
    assert _git(remote.url, "rev-parse", "refs/heads/task/T1").stdout.strip() == commit


def test_a_fetch_that_fails_is_a_different_error_from_a_task_that_cannot_be_checked_out(
        tmp_path):
    """The input FETCH is retryable; a claim that never named an input is not (ADR 0033 issue 16a).

    Both are `CheckoutError` — the subclass is what lets the supervisor tell them apart without
    every existing `except CheckoutError` in this module having to be found and widened.

    The split is at the network, and it falls exactly where `materialize` already validates: a
    timeout, a 503 from a relay at its concurrency limit, or a connection dropped mid-pack is a fact
    about THIS attempt, and another provider may well succeed. A claim with no branch on it is a
    fact about the task, and retrying it burns three attempts to arrive at `retries_exhausted` —
    which does not even carry the real reason.
    """
    from remote import task_repo

    workspace = tmp_path / "ws"
    workspace.mkdir()

    with pytest.raises(task_repo.InputFetchError):
        task_repo.materialize(workspace, url=f"file://{tmp_path / 'no-such-repo.git'}",
                              token="tok", branch="task/T1", input_commit="0" * 40)

    for missing in ({"branch": ""}, {"input_commit": ""}, {"url": ""}):
        kwargs = {"url": "file:///somewhere", "token": "t",
                  "branch": "task/T1", "input_commit": "0" * 40, **missing}
        with pytest.raises(task_repo.CheckoutError) as raised:
            task_repo.materialize(workspace, **kwargs)
        assert not isinstance(raised.value, task_repo.InputFetchError), (
            f"{missing} was treated as a retryable fetch failure; it is a permanent bad claim and "
            f"retrying it spends every attempt to reach `retries_exhausted`")


def test_a_fetch_failure_leaves_the_task_retryable_instead_of_terminally_failed(
        agent, tmp_path, monkeypatch, capsys):
    """Acceptance criterion 4. A fetch that fails must NOT report a terminal state.

    Before this, a `materialize` failure became `failed` — and terminal is precisely the state
    nothing retries. So an imported history the relay could not pack in time made **every task in
    that project fail immediately and never retry**. It did not degrade; it stopped.

    The mechanism is the one the push path already uses and argues for: report nothing, let the
    lease lapse, and let the relay's reclaim hand the task to another provider up to its retry cap.
    Reporting `failed` here would be a lie of the worst kind — confident, terminal, and about
    somebody else's repository.

    The reason still has to reach the user, which is why the event is asserted as well as the
    silence: a retry with no explanation is a task that appears to sit still.
    """
    from remote import task_repo, tasks

    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    _relay_git_url(monkeypatch, remote.url)
    agent("printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")

    def timed_out(*_args, **_kwargs):
        raise task_repo.InputFetchError(
            "git fetch timed out after 900s")

    monkeypatch.setattr(task_repo, "materialize", timed_out)
    reported = []
    monkeypatch.setattr(tasks, "report_once",
                        lambda _s, tid, **kw: reported.append((tid, kw)))
    publisher = _RecordingPublisher()
    monkeypatch.setattr(tasks, "_publisher_for", lambda *a, **k: publisher)

    tasks._run_and_report(_FakeState(), _job_with_input(commit))

    assert not reported, (
        f"a fetch failure was reported as terminal, so nothing will ever retry it: {reported}")

    # BOTH channels, not either. `TaskEventPublisher` latches off permanently on a 403/404 and then
    # drops everything in silence — and a lost lease is both a common cause of a failed fetch and
    # exactly what silences it, so the run where the reason matters most is the run most likely to
    # lose it. An `or` here would go green with the unconditional half deleted.
    published = " ".join(str(fields) for _kind, fields in publisher.published)
    assert "900s" in published, (
        f"the user gets no event saying why their task stalled: {publisher.published}")
    assert "900s" in capsys.readouterr().err, (
        "nothing reached the provider's own log — if the event publisher had latched off, this "
        "failure would be completely invisible")


def test_a_push_failure_is_reported_locally_even_when_the_event_channel_is_dead(
        agent, tmp_path, monkeypatch, capsys):
    """The push most often fails BECAUSE the lease was lost — and the event channel is fenced on the
    same lease, so it is silenced by the same cause.

    `TaskEventPublisher` never raises (it buffers, and `flush` drops a refused batch with a generic
    message), so a `try/except` around the publish could not cover this and the reason would exist
    nowhere. It is written to stderr unconditionally instead, BEFORE the event is attempted.
    """
    from remote import task_repo, tasks

    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    _relay_git_url(monkeypatch, remote.url)
    agent("echo fixed > fix.py\n"
          "printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")

    def refuse(*_a, **_k):
        raise task_repo.PushError("could not push task/T1: the relay refused (403)")

    monkeypatch.setattr(task_repo, "commit_and_push", refuse)
    monkeypatch.setattr(tasks, "report_once",
                        lambda *a, **k: pytest.fail("a terminal state was reported"))

    class _DeadChannel:
        """What the real publisher does once the relay has refused it: accepts and discards."""

        def publish(self, *_a, **_k):
            pass

        def close(self):
            pass

    monkeypatch.setattr(tasks, "_publisher_for", lambda *a, **k: _DeadChannel())

    tasks._run_and_report(_FakeState(), _job_with_input(commit))

    err = capsys.readouterr().err
    assert "the relay refused (403)" in err, err


# --- issue 06: the project's conversation travels with the repository -----------------------------

# MEASURED against a real Claude Code (2.1.222, macOS, 2026-08-05), not derived from documentation:
# a session was run in `/private/tmp/grid_enc/a_b.c-d` and the transcript landed in
# `~/.claude/projects/-private-tmp-grid-enc-a-b-c-d`. The underscore is what makes this table worth
# keeping — it is the ONLY character that distinguishes "replace the separators `/` and `.`" from
# "replace everything that is not alphanumeric", and the two rules disagree on any operator whose
# `GRID_TASK_ROOT` contains one. The other rows were read off the same machine's existing
# `~/.claude/projects/` entries.
#
# Every row is a path that is its own realpath, so this table tests the ENCODING alone — resolution
# is a separate rule with its own test below. A `/var/...` row would silently exercise both here and
# fail on macOS, where `/var` is a symlink to `/private/var`.
_MEASURED_TRANSCRIPT_DIR_NAMES = [
    ("/private/tmp/grid_enc/a_b.c-d", "-private-tmp-grid-enc-a-b-c-d"),
    ("/Users/macbookpro/.grid", "-Users-macbookpro--grid"),
    ("/private/tmp/one/workspaces/abc.com", "-private-tmp-one-workspaces-abc-com"),
    # The shape this provider actually produces since ADR 0034 D-c: a real project uuid, a real
    # 32-hex `member_key` and a real conversation uuid — two levels deeper than issue 06's. The
    # characters are all in the classes already covered above, which is exactly why the E2E in
    # `tests/e2e_cross_repo/e2e_live_agent.py` is what settles this rather than the table — "every
    # character is already covered" is the reasoning that agreed with issue 06's bug.
    ("/private/var/grid/projects/2f0b9b1e-7a4c-4d5e-9c31-0a1b2c3d4e5f/"
     "9f2b9f2b9f2b9f2b9f2b9f2b9f2b9f2b/8d1a4c60-3b2e-4f7a-95d8-6e0f1a2b3c4d/workspace",
     "-private-var-grid-projects-2f0b9b1e-7a4c-4d5e-9c31-0a1b2c3d4e5f-"
     "9f2b9f2b9f2b9f2b9f2b9f2b9f2b9f2b-8d1a4c60-3b2e-4f7a-95d8-6e0f1a2b3c4d-workspace"),
]


def test_two_conversations_of_one_member_get_two_transcript_directory_names():
    """Why the path change IS the feature (ADR 0034 D-c), stated where the naming rule lives.

    Claude Code keys a session's transcript directory on the flattened cwd, so two workspaces that
    differ by one segment are two sessions and one workspace is one session — there is no third
    option. Asserted beside the encoding table because the table would go on passing if both
    conversations were handed the same directory: every row in it would still be right.
    """
    from remote import task_agent

    base = "/private/var/grid/projects/2f0b9b1e/9f2b9f2b"
    first = task_agent.transcript_dir_name(Path(f"{base}/{_CONVERSATION}/workspace"))
    second = task_agent.transcript_dir_name(Path(f"{base}/{_OTHER_CONVERSATION}/workspace"))

    assert first != second, (
        "two conversations of one member share a transcript directory, so the second resumes the "
        "first's session — which is the whole of what issue 38 removes")


@pytest.mark.parametrize("cwd,expected", _MEASURED_TRANSCRIPT_DIR_NAMES)
def test_the_transcript_directory_name_is_the_agent_cwd_with_every_separator_flattened(
        cwd, expected):
    """Claude Code derives a session's transcript directory from the ABSOLUTE working directory.

    This function has to reproduce the agent's own naming byte-for-byte: get it wrong and the
    provider symlinks a directory the agent never writes to, the transcript never reaches the
    repository, and every follow-up task silently starts a fresh conversation while looking healthy.
    Hence a table of measurements rather than a rule someone reasoned out.
    """
    from pathlib import Path

    from remote import task_agent

    assert task_agent.transcript_dir_name(Path(cwd)) == expected


def test_a_workspace_reached_through_a_symlink_is_named_by_its_real_path(tmp_path):
    """The name comes from the cwd the CHILD reports, and `getcwd` has already followed every
    symlink on the way in.

    Found by a live two-task run, not by a test: on macOS `/var` is a symlink to `/private/var`, so
    a workspace under `/var/folders/...` is `-private-var-folders-...` to the agent while a caller
    that trusted the string computed `-var-folders-...`, planted its symlink there, and watched
    Claude Code write the transcript somewhere else entirely. The task still succeeded and the
    session id still came back — nothing failed, the conversation was simply never captured.

    Every earlier unit test compared our own computation against itself and agreed, which is why
    this one is written against a symlink rather than against a string.
    """
    from remote import task_agent

    real = tmp_path / "real-root" / "workspace"
    real.mkdir(parents=True)
    (tmp_path / "via-link").symlink_to(tmp_path / "real-root", target_is_directory=True)

    through_link = task_agent.transcript_dir_name(tmp_path / "via-link" / "workspace")

    assert through_link == task_agent.transcript_dir_name(real)
    assert "via-link" not in through_link


def test_a_symlinked_task_root_still_names_a_conversations_workspace_by_its_real_path(
        monkeypatch, tmp_path):
    """The same rule against the path this provider actually builds (ADR 0034 D-c).

    The test above resolves a hand-written path; this one resolves `workspace_for`'s own, through a
    symlinked `GRID_TASK_ROOT` — which is the arrangement the issue-06 bug was found in, and is
    exactly what an operator does when they relocate storage. Two more segments is two more places
    a `resolve()` that was dropped or applied to the wrong end goes unnoticed, and the symptom is
    silent: the transcript is written outside the worktree and the conversation is never captured.
    """
    from remote import task_agent

    real_root = tmp_path / "real-root"
    real_root.mkdir()
    (tmp_path / "via-link").symlink_to(real_root, target_is_directory=True)

    monkeypatch.setenv("GRID_TASK_ROOT", str(tmp_path / "via-link"))
    through_link = task_agent.transcript_dir_name(
        task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION))
    monkeypatch.setenv("GRID_TASK_ROOT", str(real_root))
    direct = task_agent.transcript_dir_name(
        task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION))

    assert through_link == direct
    assert "via-link" not in through_link
    # The uuid survives verbatim: its only non-alphanumeric characters are hyphens, and the encoding
    # rule replaces every non-alphanumeric character with a hyphen.
    assert _CONVERSATION in through_link, (
        "the conversation segment is missing from the transcript directory name, so both of a "
        "member's conversations would resolve to one session")


def test_a_workspace_whose_transcript_name_the_binary_would_truncate_is_refused(
        monkeypatch, tmp_path):
    """MEASURED against Claude Code 2.1.232 on 2026-08-15, and found by the live E2E — not reasoned.

    The binary does not just flatten the cwd: past a limit it keeps a PREFIX and appends a short
    hash. Observed, in one operator's `~/.claude/projects/`: a 186-character name kept whole, and
    every over-long one written as exactly **207** characters — a 200-character prefix, a hyphen,
    and a 6-character suffix that is not derivable from the path. So beyond the limit this provider
    cannot compute where the binary will write, the symlink is planted somewhere nothing writes
    through, and the transcript never reaches the worktree — while the task completes, the session
    id comes back, and the push lands. Issue 06's failure exactly, re-armed by ADR 0034 D-c's extra
    segment, which adds 37 characters to every workspace path.

    A REFUSAL rather than a warning, and `run_task` makes it terminal: the alternative is running a
    conversation that is unresumable from its very first turn with nothing anywhere saying so.
    Refused at 200 rather than 207 because the two are indistinguishable in the data — every
    truncated name is 207 long, which is what BOTH "cap at 207" and "cap at 200, then append 7"
    produce — and the direction to be wrong in is refusing a provider that would have worked, never
    accepting one that silently loses every conversation. The stock layout is 135 characters, so
    this cannot fire on a sane deployment.
    """
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_ROOT", str(tmp_path / ("d" * 200)))
    workspace = task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION)
    workspace.mkdir(parents=True)
    monkeypatch.setenv("GRID_TASK_CLAUDE_CONFIG_DIR", str(tmp_path / "config"))

    with pytest.raises(OSError) as excinfo:
        task_agent.link_transcript(workspace, _MEMBER)

    assert task_agent.WORKSPACE_ROOT_ENV in str(excinfo.value), (
        f"the refusal does not name the variable an operator would change: {excinfo.value}")
    assert str(task_agent.TRANSCRIPT_NAME_MAX_CHARS) in str(excinfo.value), excinfo.value


def test_the_stock_workspace_layout_is_nowhere_near_the_transcript_name_limit():
    """The positive control, and the number that says the guard above cannot fire in production.

    `/var/grid/projects/<uuid-36>/<member_key-32>/<conversation_id-36>/workspace` flattens to 135
    characters against a limit of 200. Asserted rather than stated in a comment because it is the
    whole argument for the guard being a refusal instead of a warning — if the stock layout were
    close to the limit, refusing would be the wrong trade.
    """
    from remote import task_agent

    stock = Path(task_agent.DEFAULT_WORKSPACE_ROOT) / "projects" / (
        "2f0b9b1e-7a4c-4d5e-9c31-0a1b2c3d4e5f") / ("9f2b" * 8) / (
        "8d1a4c60-3b2e-4f7a-95d8-6e0f1a2b3c4d") / "workspace"

    assert len(task_agent.transcript_dir_name(stock)) < task_agent.TRANSCRIPT_NAME_MAX_CHARS


def test_what_the_agent_writes_to_its_transcript_directory_lands_in_the_workspace(
        monkeypatch, tmp_path, short_task_root):
    """The whole mechanic of issue 06, in one assertion.

    Claude Code writes through a symlink (measured by the issue-01 spike), so pointing its
    per-cwd transcript folder at a directory inside the git worktree captures the transcript *and*
    the agent's `memory/` with no copying step — the repository that already carries the project's
    files carries its conversation too.
    """
    from remote import task_agent

    config = tmp_path / "provider-config"
    monkeypatch.setenv("GRID_TASK_CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("GRID_TASK_ROOT", str(short_task_root))
    workspace = task_agent.ensure_workspace(task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION))

    target = task_agent.link_transcript(workspace, _MEMBER)

    link = config / "projects" / task_agent.transcript_dir_name(workspace)
    assert link.is_symlink(), f"{link} is not a symlink"
    # Writing THROUGH the link is exactly what the agent does; asserting on the link's own target
    # would prove the call, not the behaviour that makes the feature work.
    (link / "sess-1.jsonl").write_text('{"type":"system"}\n', encoding="utf-8")
    (link / "memory").mkdir()
    (link / "memory" / "note.md").write_text("remembered", encoding="utf-8")

    assert target == workspace / ".grid" / "agent" / _MEMBER
    assert (target / "sess-1.jsonl").read_text(encoding="utf-8") == '{"type":"system"}\n'
    assert (target / "memory" / "note.md").read_text(encoding="utf-8") == "remembered"


def test_a_real_directory_where_the_symlink_belongs_is_refused_and_nothing_is_deleted(
        monkeypatch, tmp_path, short_task_root):
    """Not hypothetical: every provider that ran tasks before this slice has one.

    Issues 03-05 spawned the agent with no symlink in place, so Claude Code created that directory
    itself and filled it with the project's conversation. Replacing it blind would delete exactly
    the history this feature exists to preserve, so the provider refuses and names the path — an
    operator moves it once. A symlink is different and IS replaced: it holds no data.
    """
    from remote import task_agent

    config = tmp_path / "provider-config"
    monkeypatch.setenv("GRID_TASK_CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("GRID_TASK_ROOT", str(short_task_root))
    workspace = task_agent.ensure_workspace(task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION))
    squatter = config / "projects" / task_agent.transcript_dir_name(workspace)
    squatter.mkdir(parents=True)
    (squatter / "older-session.jsonl").write_text("a conversation from before 06\n", encoding="utf-8")

    with pytest.raises(OSError) as excinfo:
        task_agent.link_transcript(workspace, _MEMBER)

    # A bare `FileExistsError` from `symlink_to` would also carry the path and would also be an
    # OSError — and it would tell an operator nothing about what to do. The refusal has to be the
    # provider's own, and it has to say how to clear it, or every task on this project fails with a
    # message that reads like a bug in grid.
    message = str(excinfo.value)
    assert str(squatter) in message, message
    assert "move or remove" in message.lower(), message
    assert (squatter / "older-session.jsonl").exists(), "an operator's transcript was destroyed"


def test_a_config_directory_inside_the_workspace_is_refused(monkeypatch, tmp_path, short_task_root):
    """ADR 0032 D-b's hazard, reached from the provider's own side rather than a client's upload.

    The config directory holds the provider's Claude subscription credential. Inside the workspace
    it is inside the git worktree, and the result push commits it into the requesting user's
    repository — the one leak this whole design is arranged to prevent.
    """
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_ROOT", str(short_task_root))
    workspace = task_agent.ensure_workspace(task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION))
    monkeypatch.setenv("GRID_TASK_CLAUDE_CONFIG_DIR", str(workspace / "provider-config"))

    with pytest.raises(OSError) as excinfo:
        task_agent.link_transcript(workspace, _MEMBER)

    assert "credential" in str(excinfo.value).lower(), excinfo.value


def test_an_operator_who_configures_nothing_gets_claude_codes_own_default(
        monkeypatch, tmp_path, short_task_root):
    """The commonest production configuration, and the one the suite's safety net hides.

    `tests/conftest.py` points `GRID_TASK_CLAUDE_CONFIG_DIR` at a temp directory for every test, so
    that nothing plants symlinks in the developer's real `~/.claude` — which means no other test
    exercises the fallback that almost every real provider actually uses. Proved here against a
    stubbed home rather than the real one, so the coverage costs nothing.
    """
    from pathlib import Path

    from remote import task_agent

    monkeypatch.delenv("GRID_TASK_CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("GRID_TASK_ROOT", str(short_task_root))
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))

    assert task_agent.configured_claude_config_dir() is None
    assert task_agent.claude_config_dir() == fake_home / ".claude"

    workspace = task_agent.ensure_workspace(task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION))
    task_agent.link_transcript(workspace, _MEMBER)

    link = fake_home / ".claude" / "projects" / task_agent.transcript_dir_name(workspace)
    assert link.is_symlink(), f"{link} was not created under the default config directory"


def test_a_relative_config_directory_is_refused(monkeypatch, tmp_path, short_task_root):
    """`child_env` hands the value to the child verbatim, and the child resolves it against ITS
    working directory — which is the workspace. So a relative config directory is the previous test's
    leak wearing a different spelling, and it is refused where the value is read."""
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_ROOT", str(short_task_root))
    monkeypatch.setenv("GRID_TASK_CLAUDE_CONFIG_DIR", "provider-config")

    with pytest.raises(ValueError) as excinfo:
        task_agent.claude_config_dir()

    assert "absolute" in str(excinfo.value).lower(), excinfo.value


def test_a_follow_up_task_asks_the_agent_to_continue_the_projects_conversation(monkeypatch):
    """`--resume <id>`, appended to the argv issue 03 pinned — nothing else about the child changes.

    A resume appends to the same transcript and keeps the same session id (measured), which is what
    makes one stored id per project reusable forever rather than a chain to walk.
    """
    from remote import task_agent

    monkeypatch.delenv("GRID_TASK_PERMISSION_MODE", raising=False)

    argv = task_agent.agent_argv(
        "/usr/local/bin/claude", "and now write the tests",
        workspace=Path("/var/grid/projects/p/workspace"), resume="012c9e09-abcd")

    # Everything up to the confinement policy, which `tests/test_task_sandbox.py` owns. `--resume`
    # keeps its place: it is appended before `--settings`, so a change to either is visible here.
    assert argv[:argv.index("--settings")] == [
        "/usr/local/bin/claude",
        "-p", "and now write the tests",
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "acceptEdits",
        "--setting-sources", "user",
        "--strict-mcp-config",
        "--resume", "012c9e09-abcd",
    ]


def test_a_members_transcript_lives_under_their_own_key_inside_the_worktree(
        monkeypatch, tmp_path, short_task_root):
    """`.grid/agent/<member_key>/`, not `.grid/agent/` (ADR 0033 D-g).

    One directory per project would have two members appending to the same JSONL transcript, which
    conflicts on every single integration — and a merge conflict inside a conversation is the last
    thing anyone wants an agent resolving. It is also the same fact as the workspace path: Claude
    Code derives the transcript directory from the cwd, so these cannot be keyed differently
    without one of them being wrong.
    """
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_ROOT", str(short_task_root))
    workspace = task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION)

    assert (task_agent.transcript_dir(workspace, _MEMBER)
            == workspace / ".grid" / "agent" / _MEMBER)
    # Both members' transcripts can coexist in one checkout, which is exactly what makes the
    # committed conversation travel without conflicting.
    assert (task_agent.transcript_dir(workspace, _OTHER_MEMBER)
            != task_agent.transcript_dir(workspace, _MEMBER))
    # And the key still arrives off the wire here, so it is still validated here.
    with pytest.raises(ValueError, match="member key"):
        task_agent.transcript_dir(workspace, "../../../etc")


def _transcript(workspace, session_id, body='{"type":"summary"}\n'):
    """Put a transcript for `session_id` where a checkout would have left one."""
    from remote import task_agent

    directory = task_agent.transcript_dir(workspace, _MEMBER)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    path.write_text(body, encoding="utf-8")
    return path


def test_a_session_whose_transcript_arrived_with_the_checkout_is_resumable(monkeypatch, tmp_path, short_task_root):
    """The relay says WHICH session; the workspace says WHETHER it is here. Both, or no resume.

    The relay's answer alone is not enough: it names the project's last conversation, but the
    transcript only reaches this provider if the commit carrying it was fast-forwarded onto `main`.
    """
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_ROOT", str(short_task_root))
    workspace = task_agent.ensure_workspace(task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION))
    _transcript(workspace, "012c9e09-abcd")

    decision = task_agent.resumable_session(workspace, "012c9e09-abcd", _MEMBER)

    assert decision.session_id == "012c9e09-abcd"
    assert decision.reason is None


def test_a_project_with_no_conversation_yet_starts_fresh_and_says_nothing(monkeypatch, tmp_path, short_task_root):
    """The project's FIRST task. Not a degraded outcome, so there is nothing to report."""
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_ROOT", str(short_task_root))
    workspace = task_agent.ensure_workspace(task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION))

    decision = task_agent.resumable_session(workspace, None, _MEMBER)

    assert decision.session_id is None
    assert decision.reason is None


@pytest.mark.parametrize("hostile", [
    "../../../etc/passwd",   # climbs out of the transcript directory
    "a/b",                   # a separator invents a level
    "/etc/passwd",           # absolute: `dir / "/etc/passwd"` IS `/etc/passwd`, silently
    "..",
    ".hidden",               # a leading dot, refused the way `workspace_for` refuses `.` and `..`
    "-rf",                   # would reach `--resume` as a flag rather than as its value
    "x" * 300,               # past the relay's own bound
])
def test_a_session_id_that_is_not_a_safe_filename_never_reaches_the_filesystem(
        monkeypatch, tmp_path, hostile, short_task_root):
    """The id is used to BUILD A PATH, so it gets `workspace_for`'s allowlist rule.

    It arrives from the relay rather than directly from a user, which makes it one hop further away
    — not trustworthy. A provider that followed `../../../` here would read, and then commit into
    the requesting user's repository, a file from outside the workspace entirely.
    """
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_ROOT", str(short_task_root))
    workspace = task_agent.ensure_workspace(task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION))

    decision = task_agent.resumable_session(workspace, hostile, _MEMBER)

    assert decision.session_id is None
    # The REASON matters as much as the verdict: without the allowlist these ids also return None,
    # simply because nothing happens to exist where they point. Asserting only "no resume" passes
    # just as happily with the guard deleted, which is how a path-escape check rots into decoration.
    assert "not a safe id" in (decision.reason or ""), decision.reason


@pytest.mark.parametrize("wrong_type", [7, True, ["s"], {"a": 1}, 3.5])
def test_a_session_id_of_the_wrong_type_degrades_instead_of_killing_the_task(
        monkeypatch, tmp_path, wrong_type, short_task_root):
    """`claim_task` returns `resp.json()` verbatim, so this field can be any JSON type.

    `re.match` raises `TypeError` on a non-string, and this call sits outside `run_task`'s
    try/except blocks — so a relay that serialized the field wrong would not merely lose the
    resume, it would lose the whole attempt: the raise unwinds past `_push_result`, so nothing is
    committed or pushed, and the user gets "task runner raised: TypeError(...)" instead of the
    agent's work. Every other wire-sourced value in `run_task` is already `str()`-cast or
    `isinstance`-checked; this one gets the same rule.
    """
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_ROOT", str(short_task_root))
    workspace = task_agent.ensure_workspace(task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION))

    decision = task_agent.resumable_session(workspace, wrong_type, _MEMBER)

    assert decision.session_id is None
    assert "not a safe id" in (decision.reason or ""), decision.reason


def test_a_session_id_cannot_reach_a_file_outside_the_transcript_directory(monkeypatch, tmp_path, short_task_root):
    """The escape the allowlist actually exists to stop, with a real file at the far end.

    `.grid/` holds provider-local state that is deliberately NOT committed. A relay — or anything
    that reached one — naming `../something` would have the provider read that file as a transcript
    and hand it to `--resume`. Planting a readable file there is what makes this test fail when the
    guard is removed, rather than passing on the accident that the path was empty.
    """
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_ROOT", str(short_task_root))
    workspace = task_agent.ensure_workspace(task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION))
    task_agent.transcript_dir(workspace, _MEMBER).mkdir(parents=True, exist_ok=True)
    outside = workspace / task_agent.task_repo.RESERVED_DIR / "sneaky.jsonl"
    outside.write_text('{"type": "summary"}\n', encoding="utf-8")

    decision = task_agent.resumable_session(workspace, "../sneaky", _MEMBER)

    assert decision.session_id is None, "a session id climbed out of the transcript directory"
    assert "not a safe id" in (decision.reason or ""), decision.reason


def test_a_follow_up_task_spawns_the_agent_with_the_projects_session(agent, tmp_path, monkeypatch):
    """End to end through `run_task`: the claim names a session, the checkout carries its
    transcript, and the child is asked to continue it.

    Asserted through what the CHILD actually received, not through the argv builder — the point of
    this slice is the wiring, and a test that re-read `agent_argv` would pass with the two never
    connected.
    """
    from remote import task_agent, tasks

    monkeypatch.setenv("GRID_TASK_CLAUDE_CONFIG_DIR", str(tmp_path / "provider-config"))
    workspace = task_agent.ensure_workspace(task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION))
    _transcript(workspace, "012c9e09-abcd")

    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    seen = tmp_path / "argv.txt"
    agent(f'printf "%s" "$*" > {seen}\n'
          "printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")
    publisher = _RecordingPublisher()

    outcome = tasks.run_task(
        _job(input_commit=commit, branch="task/T1", resume_session_id="012c9e09-abcd"),
        publish=publisher.publish, remote=remote)

    assert outcome.state == "completed", outcome.error
    assert "--resume 012c9e09-abcd" in seen.read_text(encoding="utf-8")
    # Said out loud, too: "resumed" and "started fresh" are the two outcomes a user needs to tell
    # apart, and only one of them is visible in the answer the agent gives.
    resumed = [f for kind, f in publisher.published if kind == "task.session_resumed"]
    assert resumed == [{"session_id": "012c9e09-abcd"}], publisher.published


def test_a_session_the_workspace_cannot_supply_starts_fresh_and_says_so_in_the_log(
        agent, tmp_path, monkeypatch):
    """The acceptance criterion: missing or corrupt starts fresh WITH A SIGNAL, never fails.

    Reachable the moment a task fails — a failed attempt's transcript is never fast-forwarded onto
    `main`, so the next task's checkout does not carry it while the relay still remembers the id
    that attempt reported. Passing `--resume` at that point would fail the task outright; starting
    over is the only useful answer, and the user has to be told, because an agent that has silently
    forgotten the project is indistinguishable from one that ignored them.
    """
    from remote import task_agent, tasks

    monkeypatch.setenv("GRID_TASK_CLAUDE_CONFIG_DIR", str(tmp_path / "provider-config"))
    task_agent.ensure_workspace(task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION))

    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    seen = tmp_path / "argv.txt"
    agent(f'printf "%s" "$*" > {seen}\n'
          "printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")
    publisher = _RecordingPublisher()

    outcome = tasks.run_task(
        _job(input_commit=commit, branch="task/T1", resume_session_id="012c9e09-abcd"),
        publish=publisher.publish, remote=remote)

    assert outcome.state == "completed", outcome.error
    assert "--resume" not in seen.read_text(encoding="utf-8")
    reset = [fields for kind, fields in publisher.published if kind == "task.session_reset"]
    assert len(reset) == 1, publisher.published
    assert reset[0]["requested_session_id"] == "012c9e09-abcd"
    assert "no transcript" in reset[0]["reason"]


def test_a_publisher_that_raises_on_the_session_event_does_not_fail_the_task(
        agent, tmp_path, monkeypatch):
    """A progress event must never be able to fail the task — `_Reporter._emit`'s rule, applied to
    the two publishes that happen BEFORE the spawn.

    That position is what makes it matter more here than elsewhere: a raise from these two would
    unwind past the agent and past the push, and `_run_and_report` would report "task runner
    raised: ..." for a task that never ran — a message indistinguishable, to the user, from the
    agent itself failing. `TaskEventPublisher` is documented never to raise; reaching this guard
    means it has a bug, and a bug there must cost one event, not the work.
    """
    from remote import task_agent, tasks

    task_agent.ensure_workspace(task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION))
    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    agent("echo done > out.txt\n"
          "printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")

    def exploding(kind, **fields):
        raise RuntimeError(f"the publisher is broken ({kind})")

    outcome = tasks.run_task(
        _job(input_commit=commit, branch="task/T1", resume_session_id="012c9e09-abcd"),
        publish=exploding, remote=remote)

    assert outcome.state == "completed", outcome.error
    assert (task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION) / "out.txt").exists(), "the agent never ran"


def test_a_relay_that_names_no_session_runs_fresh_without_reporting_anything(
        agent, tmp_path, monkeypatch):
    """A relay predating issue 06 sends no `resume_session_id`, and so does the FIRST task on every
    project. Neither is a degraded outcome, so neither says anything — exactly how a missing
    `input_commit` degrades to the pre-git-plane behaviour rather than to a new failure."""
    from remote import task_agent, tasks

    monkeypatch.setenv("GRID_TASK_CLAUDE_CONFIG_DIR", str(tmp_path / "provider-config"))
    task_agent.ensure_workspace(task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION))

    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    seen = tmp_path / "argv.txt"
    agent(f'printf "%s" "$*" > {seen}\n'
          "printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")
    publisher = _RecordingPublisher()

    outcome = tasks.run_task(
        _job(input_commit=commit, branch="task/T1"), publish=publisher.publish, remote=remote)

    assert outcome.state == "completed", outcome.error
    assert "--resume" not in seen.read_text(encoding="utf-8")
    assert not [kind for kind, _ in publisher.published if kind.startswith("task.session_")], \
        publisher.published


def test_a_second_provider_resumes_the_conversation_having_only_cloned_the_repository(
        agent, tmp_path, monkeypatch):
    """The acceptance criterion, and the reason the transcript is published at all.

    A single-host rehearsal of the issue-01 spike: provider A runs a task and pushes; its workspace
    is then **deleted entirely** and a **different config directory** takes over, which is what
    provider B has — no workspace, no transcript, nothing but the repository. The workspace PATH
    stays identical, because that is the lockstep value Claude Code derives the transcript
    directory from.

    ⚠️ **Since ADR 0034 D-j (issue 39) the conversation arrives on its SIDE REF, and this test is
    what says "only".** Provider B's input commit carries no `.grid/agent` path at all — the trunk
    holds none any more — so if the side-ref fetch did nothing, `sess-1.jsonl` would simply be
    absent and the agent would start fresh. The final assertion is therefore no longer incidental
    detail: it is the whole difference between this feature working and reporting that it did.

    What this does NOT prove is a genuinely different machine, OS, or Claude Code version. Providers
    should stay version-pinned until a two-host run says otherwise (PRD, "still unproven").
    """
    import shutil

    from remote import task_agent, tasks

    # --- provider A -------------------------------------------------------------------------
    monkeypatch.setenv("GRID_TASK_CLAUDE_CONFIG_DIR", str(tmp_path / "provider-a-config"))
    workspace = task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION)
    link = (task_agent.claude_config_dir() / "projects"
            / task_agent.transcript_dir_name(workspace))
    remote, first_input = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    # Writes THROUGH the config-directory path, exactly as the real agent does. Writing to
    # `$PWD/.grid/agent` instead would land in the right place while proving nothing about the link.
    #
    # Interpolated into the script rather than handed over as an environment variable, the way the
    # `seen` path below already is: since issue 23 `child_env()` is an allowlist, so an invented
    # variable no longer reaches the child. Baking the path in keeps this test about the transcript
    # link instead of quietly making it a second test of the environment.
    agent(f'mkdir -p "{link}/memory"\n'
          f'printf \'{{"type":"summary"}}\\n\' > "{link}/sess-1.jsonl"\n'
          f'echo remembered > "{link}/memory/note.md"\n'
          "printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")

    first = tasks.run_task(_job(input_commit=first_input, branch="task/T1"), remote=remote)
    assert first.state == "completed", first.error
    pushed = task_repo_commit_and_push(workspace, remote, "task/T1")
    # What the relay pins on the next turn of this conversation, and the only thing carrying the
    # transcript now: the result commit above holds none.
    pinned = task_repo_push_transcript(workspace, remote)
    assert pinned, "provider A published no transcript, so there is nothing for B to resume"
    assert ".grid/agent" not in _git(
        remote.url, "ls-tree", "-r", "--name-only", pushed).stdout, (
        "the result commit still carries the conversation, so this test would pass without the "
        "side ref ever being fetched")

    # --- everything provider A had, gone ----------------------------------------------------
    shutil.rmtree(workspace)
    shutil.rmtree(tmp_path / "provider-a-config")
    monkeypatch.setenv("GRID_TASK_CLAUDE_CONFIG_DIR", str(tmp_path / "provider-b-config"))

    # --- provider B: the project's next task, cut from what A pushed ------------------------
    _git(remote.url, "branch", "task/T2", pushed)
    seen = tmp_path / "argv-b.txt"
    agent(f'printf "%s" "$*" > {seen}\n'
          "printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")

    second = tasks.run_task(
        _job(task_id="T2", input_commit=pushed, branch="task/T2",
             resume_session_id="sess-1", transcript_commit=pinned),
        remote=remote)

    assert second.state == "completed", second.error
    assert "--resume sess-1" in seen.read_text(encoding="utf-8"), \
        "provider B started a fresh session instead of continuing the conversation"
    # The conversation itself arrived, not merely the id — including the agent's own memory, and
    # over the side ref alone.
    rebuilt = task_agent.transcript_dir(task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION), _MEMBER)
    assert (rebuilt / "sess-1.jsonl").exists()
    assert (rebuilt / "memory" / "note.md").read_text() == "remembered\n"
    assert (rebuilt / "memory" / "note.md").read_text(encoding="utf-8") == "remembered\n"


def test_publishing_a_transcript_twice_from_one_workspace_fast_forwards(agent, tmp_path):
    """A turn with NO pin can publish more than once, which is the retry path for a FIRST turn.

    ⚠️ **This was a real CRITICAL, reproduced with git 2.54.0 before it was fixed.** `push_transcript`
    used to take its parent from the LOCAL `refs/grid/agent/<id>`, which only ever existed as a side
    effect of `materialize`'s fetch — and that fetch is gated on the pin. A conversation's first turn
    has no pin by definition, so:

      1. attempt 1 publishes a ROOT commit and the push lands. **`git push <oid>:<ref>` creates no
         local ref**, so nothing in this workspace records that it happened;
      2. attempt 1's RESULT push then fails for any transient reason — no terminal report, which is
         the designed behaviour, and the reaper reclaims the turn;
      3. attempt 2 runs in the same workspace (`clean -ffdx -e .grid` spares the transcript), still
         has no pin, still fetches nothing, so `parent` is empty again and it builds a SECOND root
         commit. `receive.denyNonFastForwards` rejects it — and will reject every later attempt
         identically, because nothing ever fetches that ref for this row.

    The turn then burns every attempt and lands on `retries_exhausted` with the agent's work and its
    transcript both perfectly fine. It affects the first turn of every conversation and every merge
    task, which is the commonest case in the system.

    The fix is that `push_transcript` establishes its own parent from the RELAY rather than trusting
    a local ref somebody else's code path may or may not have populated.
    """
    from remote import task_agent, task_repo

    remote, base = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    workspace = task_agent_workspace()
    # `run_task` creates the directory before `materialize` sees it; `git init` refuses otherwise.
    task_agent.ensure_workspace(workspace)
    task_repo.materialize(workspace, url=remote.url, token=remote.token,
                          branch="task/T1", input_commit=base)
    transcript = task_agent_transcript_dir(workspace)
    transcript.mkdir(parents=True, exist_ok=True)
    ref = task_repo.transcript_ref(_CONVERSATION)

    (transcript / "sess-1.jsonl").write_text("A\n")
    first = task_repo.push_transcript(workspace, url=remote.url, token=remote.token, ref=ref)
    assert first, "the first publish produced nothing"

    # No pin, no materialize in between — exactly what a reclaimed first turn looks like.
    (transcript / "sess-1.jsonl").write_text("A\nB\n")
    second = task_repo.push_transcript(workspace, url=remote.url, token=remote.token, ref=ref)

    assert second and second != first
    assert _git(remote.url, "merge-base", "--is-ancestor", first, second).returncode == 0, (
        "the second publish is not a descendant of the first, so the relay refuses it as a "
        "non-fast-forward and every later attempt is refused identically")
    assert _transcript_body(remote, second) == "A\nB\n"


def test_a_turn_whose_agent_wrote_no_transcript_publishes_nothing_at_all(agent, tmp_path):
    """An EMPTY transcript directory is "nothing to publish", exactly like a missing one.

    ⚠️ **This is reachable on the most ordinary path there is, and it poisons the conversation
    permanently.** `link_transcript` creates `.grid/agent/<member_key>/` BEFORE the agent starts, so
    a turn whose agent never opened a session — it failed early, it was killed, it simply wrote
    nothing — leaves that directory existing and empty. Keyed on the DIRECTORY, `push_transcript`
    then stages nothing, and `git write-tree` over an index nothing was staged into returns the
    EMPTY TREE (measured, git 2.54.0) — so an empty commit is published as the conversation's first
    state.

    The next turn of that conversation pins that commit, and `git restore --source=<it> --worktree
    -- .grid/agent` fails with `pathspec '.grid/agent' did not match any file(s) known to git`. That
    call is outside `materialize`'s retryable arm, so the turn fails TERMINALLY — and so does every
    turn after it, because the pin never changes for a conversation whose ref never moves. The
    conversation is permanently unusable and the message names a pathspec nobody wrote.

    So the decision is made from the TREE, never from the directory: an empty tree is nothing to
    publish whether the directory is absent or merely empty.
    """
    from remote import task_agent, task_repo

    remote, base = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    workspace = task_agent_workspace()
    task_agent.ensure_workspace(workspace)
    task_repo.materialize(workspace, url=remote.url, token=remote.token,
                          branch="task/T1", input_commit=base)
    # Exactly what `link_transcript` leaves behind for an agent that then writes nothing.
    task_agent_transcript_dir(workspace).mkdir(parents=True, exist_ok=True)
    ref = task_repo.transcript_ref(_CONVERSATION)

    published = task_repo.push_transcript(
        workspace, url=remote.url, token=remote.token, ref=ref)

    assert published is None, (
        "an empty conversation was published as a commit; the next turn will pin it and fail "
        "terminally on `pathspec .grid/agent did not match`, and so will every turn after it")
    assert not _git(remote.url, "rev-parse", "--verify", "--quiet", ref,
                    check=False).stdout.strip(), "an empty transcript ref was created"


def test_a_conversation_with_a_history_is_never_published_as_an_empty_tree(agent, tmp_path):
    """If the transcript directory has vanished but the ref has history, REFUSE — never publish.

    ⚠️ Measured on git 2.54.0: `git write-tree` against an index nothing was staged into returns the
    EMPTY TREE and exits 0, and `commit-tree <empty> -p <parent>` is a perfectly valid fast-forward
    child. So a workspace whose `.grid/agent` disappeared between materialize and settle — an agent
    running `rm -rf`, a half-cleaned directory — would push a commit that silently ERASES the whole
    conversation, land cleanly, and report success.

    "Nothing to record" and "everything that was recorded is gone" are not the same observation, and
    only the first is an ordinary outcome. A conversation that HAS a history and now has no files is
    the second, so it fails the turn rather than publishing.
    """
    import shutil

    from remote import task_agent, task_repo

    remote, base = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    workspace = task_agent_workspace()
    # `run_task` creates the directory before `materialize` sees it; `git init` refuses otherwise.
    task_agent.ensure_workspace(workspace)
    task_repo.materialize(workspace, url=remote.url, token=remote.token,
                          branch="task/T1", input_commit=base)
    transcript = task_agent_transcript_dir(workspace)
    transcript.mkdir(parents=True, exist_ok=True)
    ref = task_repo.transcript_ref(_CONVERSATION)
    (transcript / "sess-1.jsonl").write_text("A\n")
    published = task_repo.push_transcript(workspace, url=remote.url, token=remote.token, ref=ref)
    assert published

    shutil.rmtree(workspace / ".grid" / "agent")

    with pytest.raises(task_repo.PushError, match="transcript"):
        task_repo.push_transcript(workspace, url=remote.url, token=remote.token, ref=ref)

    assert _git(remote.url, "rev-parse", ref).stdout.strip() == published, (
        "the conversation's history was replaced by an empty tree")


def task_agent_transcript_dir(workspace, member_key=_MEMBER):
    from remote import task_agent

    return task_agent.transcript_dir(workspace, member_key)


def _transcript_body(remote, oid, member_key=_MEMBER):
    """What `sess-1.jsonl` says at one point in a conversation's history, read from the bare repo."""
    return _git(remote.url, "show",
                f"{oid}:.grid/agent/{member_key}/sess-1.jsonl").stdout


def test_a_retry_resumes_the_pinned_transcript_while_a_follow_up_takes_the_tip(
        agent, tmp_path, monkeypatch):
    """ADR 0034 D-j's latch, with its three arms asserted separately.

    Issue 06 recorded *"a failed task's conversation does not carry forward"* as a deliberate
    consequence, but nothing ever enforced it: the transcript rode a commit that only reached `main`
    on success. Off the merge path it has to be rebuilt, and the three arms are:

      1. a **failed** turn still PUBLISHES its transcript — so the next thing a person types
         continues from what the agent actually did, however badly it went;
      2. the next **user** turn resumes that, because the relay pins the ref's tip when it creates
         the turn;
      3. an **automatic retry** resumes the oid pinned when the turn was created — the conversation
         as it stood BEFORE the attempt that failed — because the retry re-claims the same row and
         the relay never re-pins it.

    ⚠️ **Arm 3 is the only test in this suite that can tell the PIN from the TIP**, and that gap was
    found by mutation: restoring from `transcript_ref` instead of `transcript_commit` passes the
    cross-provider resume test, because on a follow-up they are the same commit. They differ only
    here, which is why the fixture goes to the trouble of building a history with two distinct
    states.

    The provider's interface is the claim payload, so these are hand-built job dicts. That is not a
    shortcut around the relay: `run_task` takes exactly this dict off the wire, and the relay's half
    of the pin has its own tests in grid-src.
    """
    from remote import tasks

    remote, base = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    workspace = task_agent_workspace()

    # --- turn 1: the conversation as it stood before anything went wrong --------------------
    agent(f"mkdir -p .grid/agent/{_MEMBER}\n"
          f"echo A > .grid/agent/{_MEMBER}/sess-1.jsonl\n"
          "printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")
    assert tasks.run_task(_job(input_commit=base, branch="task/T1"),
                          remote=remote).state == "completed"
    task_repo_commit_and_push(workspace, remote, "task/T1")
    before = task_repo_push_transcript(workspace, remote)

    # --- turn 2: pinned at `before`, and it FAILS after appending to the conversation --------
    agent(f"echo B >> .grid/agent/{_MEMBER}/sess-1.jsonl\n"
          "printf '{\"type\":\"result\",\"is_error\":true,\"result\":\"broke\"}\\n'\n")
    _git(remote.url, "branch", "task/T2", base)
    failed = tasks.run_task(
        _job(task_id="T2", input_commit=base, branch="task/T2", transcript_commit=before),
        remote=remote)
    assert failed.state == "failed"
    after = task_repo_push_transcript(workspace, remote)

    # ARM 1 — the failed turn published, and it published something new.
    assert after and after != before, "a failed turn did not publish its conversation"
    assert _transcript_body(remote, after) == "A\nB\n", (
        "the failed attempt's own words are not on the ref, so nothing a person types next can "
        "continue from what the agent actually did")

    # ARM 2 — the next USER turn is pinned at the tip and sees the failed attempt's work.
    seen = tmp_path / "follow-up.txt"
    agent(f"cat .grid/agent/{_MEMBER}/sess-1.jsonl > {seen}\n"
          "printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")
    _git(remote.url, "branch", "task/T3", base)
    assert tasks.run_task(
        _job(task_id="T3", input_commit=base, branch="task/T3", transcript_commit=after),
        remote=remote).state == "completed"
    assert seen.read_text() == "A\nB\n", (
        "a follow-up turn did not see the conversation the failed turn left behind")

    # ARM 3 — the automatic RETRY carries turn 2's original pin and must NOT see `B`.
    seen_retry = tmp_path / "retry.txt"
    agent(f"cat .grid/agent/{_MEMBER}/sess-1.jsonl > {seen_retry}\n"
          "printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")
    assert tasks.run_task(
        _job(task_id="T2", attempt=2, input_commit=base, branch="task/T2",
             transcript_commit=before),
        remote=remote).state == "completed"
    assert seen_retry.read_text() == "A\n", (
        "the retry inherited the conversation of the attempt it is retrying — including whatever "
        "confused the agent into failing — instead of resetting to the pinned state. Every retry "
        "would re-read the same broken conversation and the turn would end at retries_exhausted")


def task_agent_workspace():
    """This suite's one conversation workspace, spelled once."""
    from remote import task_agent

    return task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION)


def task_repo_commit_and_push(workspace, remote, branch, member_key=_MEMBER):
    """Commit and push what the agent left, the way `_push_result` does. Returns the commit.

    `.commit` rather than the whole `Pushed`: every caller here wants a revision to hand to git, and
    the other half — what the agent left unresolved — has its own tests.
    """
    from remote import task_repo

    return task_repo.commit_and_push(
        workspace, url=remote.url, token=remote.token, branch=branch, message="task T1 (completed)").commit


def task_repo_push_transcript(workspace, remote, conversation_id=_CONVERSATION):
    """Publish the conversation the way `_push_result` does, and answer the oid the relay would pin.

    A second helper rather than a flag on the one above, because since ADR 0034 D-j they are two
    pushes to two refs with two failure meanings — and because the value this one returns is the
    thing a later turn's claim carries, which the result commit is not.
    """
    from remote import task_repo

    return task_repo.push_transcript(
        workspace, url=remote.url, token=remote.token,
        ref=task_repo.transcript_ref(conversation_id))


@pytest.mark.parametrize("body,expected_in_reason", [
    ("", "empty"),
    ("   \n", "empty"),
    ("this is not json\n", "JSON"),
    ("[1, 2, 3]\n", "JSON"),          # valid JSON, but no record Claude Code ever wrote
    ('{"type": "summary"' + "\n", "JSON"),   # truncated mid-object: a push that died halfway
])
def test_a_transcript_that_cannot_be_read_starts_a_fresh_session_with_a_reason(
        monkeypatch, tmp_path, body, expected_in_reason, short_task_root):
    """Corrupt is a fresh start, never a failed task — and never a silent one.

    `--resume` against a broken transcript fails the whole task, so the check happens here where the
    answer can still be "start over and tell the user", which is what the acceptance criterion asks
    for.
    """
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_ROOT", str(short_task_root))
    workspace = task_agent.ensure_workspace(task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION))
    _transcript(workspace, "sess-1", body=body)

    decision = task_agent.resumable_session(workspace, "sess-1", _MEMBER)

    assert decision.session_id is None
    assert expected_in_reason in decision.reason, decision.reason


def test_a_transcript_that_never_arrived_starts_a_fresh_session_with_a_reason(
        monkeypatch, tmp_path, short_task_root):
    """The reachable case: the project's previous task FAILED, so its transcript was never
    fast-forwarded onto `main` and this checkout legitimately does not carry it — while the relay
    still remembers the session id that attempt reported."""
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_ROOT", str(short_task_root))
    workspace = task_agent.ensure_workspace(task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION))

    decision = task_agent.resumable_session(workspace, "012c9e09-abcd", _MEMBER)

    assert decision.session_id is None
    assert "no transcript" in decision.reason, decision.reason


def test_a_transcript_that_is_a_symlink_is_never_followed(monkeypatch, tmp_path, short_task_root):
    """Nothing legitimate plants one — the agent writes through OUR symlink, and a checkout cannot
    create one (`core.symlinks=false`). Following it would read, and then commit, its target."""
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_ROOT", str(short_task_root))
    workspace = task_agent.ensure_workspace(task_agent.workspace_for("proj-1", _MEMBER, _CONVERSATION))
    secret = tmp_path / "credentials.json"
    secret.write_text('{"access_token": "the provider\'s own"}\n', encoding="utf-8")
    directory = task_agent.transcript_dir(workspace, _MEMBER)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "sess-1.jsonl").symlink_to(secret)

    decision = task_agent.resumable_session(workspace, "sess-1", _MEMBER)

    assert decision.session_id is None
    assert "symlink" in decision.reason, decision.reason


# --- issue 07: the lease the supervisor keeps alive -----------------------------------------------

def test_the_supervisor_renews_the_lease_around_the_whole_task(agent, monkeypatch):
    """A claimed task keeps its lease for as long as the supervisor is working on it, and stops
    keeping it the moment that call returns.

    The renewer is created and closed by `_run_and_report`, so its life IS that call's — which is
    what makes "renewal proves the child" true even in the window BEFORE the child exists, where
    there is no handle to poll and a real `git fetch` can outrun the lease TTL.
    """
    from remote import task_lease, tasks

    agent("printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")
    monkeypatch.setattr(tasks, "report_once", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "_publisher_for", lambda *a, **k: _NullPublisher())
    events = []
    monkeypatch.setattr(task_lease.LeaseRenewer, "start",
                        lambda self: events.append(("start", self._task_id)))
    monkeypatch.setattr(task_lease.LeaseRenewer, "close",
                        lambda self: events.append(("close", self._task_id)))

    tasks._run_and_report(_FakeState(), _job())

    assert events == [("start", "T1"), ("close", "T1")]


def test_the_renewer_is_given_the_very_child_the_task_spawned(agent, monkeypatch):
    """The handle, not a pid — and the REAL one, taken at spawn.

    `on_spawn` already carried the "did anything run" flag; this proves the same callback hands the
    renewer the live `Popen`, which is the only thing it is allowed to treat as evidence (D-c).
    """
    from remote import task_lease, tasks

    agent("printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")
    monkeypatch.setattr(tasks, "report_once", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "_publisher_for", lambda *a, **k: _NullPublisher())
    attached = []
    monkeypatch.setattr(task_lease.LeaseRenewer, "start", lambda self: None)
    monkeypatch.setattr(task_lease.LeaseRenewer, "attach", lambda self, proc: attached.append(proc))

    tasks._run_and_report(_FakeState(), _job())

    assert len(attached) == 1
    assert hasattr(attached[0], "poll"), "the renewer was handed something that is not a child"
    assert attached[0].returncode == 0, "it was handed a different process than the one that ran"


def test_renewal_has_stopped_before_the_terminal_result_is_reported(agent, monkeypatch):
    """Ordering, and it is not cosmetic: a renewal in flight while the state changes underneath it
    is refused with a 404 that reads as a fault rather than as the race the caller just created."""
    from remote import task_lease, tasks

    agent("printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")
    order = []
    monkeypatch.setattr(tasks, "report_once", lambda *a, **k: order.append("report"))
    monkeypatch.setattr(tasks, "_publisher_for", lambda *a, **k: _NullPublisher())
    monkeypatch.setattr(task_lease.LeaseRenewer, "start", lambda self: None)
    monkeypatch.setattr(task_lease.LeaseRenewer, "close", lambda self: order.append("close"))

    tasks._run_and_report(_FakeState(), _job())

    assert order == ["close", "report"]


def test_a_renewer_that_blows_up_never_costs_the_task_its_result(agent, monkeypatch):
    """The renewer is an observer of the task, exactly as the event publisher is. A fault in it must
    not unwind past the agent, past the push, and turn a finished run into a reported failure."""
    from remote import task_lease, tasks

    agent("printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")
    reported = {}
    monkeypatch.setattr(tasks, "report_once", lambda _s, tid, **kw: reported.update(kw))
    monkeypatch.setattr(tasks, "_publisher_for", lambda *a, **k: _NullPublisher())

    def explode(self, *_a, **_k):
        raise RuntimeError("the renewer is broken")

    monkeypatch.setattr(task_lease.LeaseRenewer, "start", explode)
    monkeypatch.setattr(task_lease.LeaseRenewer, "close", explode)

    tasks._run_and_report(_FakeState(), _job())

    assert reported["state"] == "completed"


def test_the_reason_a_fresh_session_was_started_travels_on_the_terminal_report(
        agent, monkeypatch, tmp_path):
    """Issue 06's accepted HIGH, closed from this end.

    The reason was published as a progress event and NOWHERE else — and `TaskEventPublisher` latches
    off permanently on a 403/404, after which it drops everything. Carrying it on the terminal report
    means the relay records it, so the owner can read it after the fact whatever the publisher did.
    """
    from remote import tasks

    agent("printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")
    reported = {}
    monkeypatch.setattr(tasks, "report_once", lambda _s, tid, **kw: reported.update(kw))
    monkeypatch.setattr(tasks, "_publisher_for", lambda *a, **k: _NullPublisher())

    # The relay named a session this workspace has never seen, so the agent starts fresh.
    tasks._run_and_report(_FakeState(), _job(resume_session_id="sess-nobody-has"))

    assert reported["state"] == "completed"
    assert reported["session_reset_reason"], "the owner is never told why the conversation restarted"
    assert "sess-nobody-has" in reported["session_reset_reason"]


def test_a_task_that_resumed_cleanly_reports_no_reset_reason(agent, monkeypatch):
    """Absent means "there was no reset". Sending an empty reason on every ordinary task would make
    the field meaningless — and would overwrite a real reason recorded by an earlier attempt."""
    from remote import tasks

    agent("printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")
    reported = {}
    monkeypatch.setattr(tasks, "report_once", lambda _s, tid, **kw: reported.update(kw))
    monkeypatch.setattr(tasks, "_publisher_for", lambda *a, **k: _NullPublisher())

    tasks._run_and_report(_FakeState(), _job())  # no `resume_session_id` at all

    assert reported["session_reset_reason"] is None


# --- issue 09: how many tasks a provider can actually take ---------------------------------------


# A run that hits the subscription's wall PART WAY THROUGH and carries on working — which is what
# actually happens: `rate_limit_event` rides along with the agent's ordinary output.
_SPENT_MID_RUN = """\
printf '{"type":"system","subtype":"init","session_id":"sess-1"}\\n'
printf '{"type":"rate_limit_event","rate_limit_info":{"status":"rejected","rateLimitType":"five_hour","resetsAt":%s}}\\n' "$(( $(date +%s) + 3600 ))"
printf '{"type":"assistant","message":{"content":[{"type":"text","text":"still working"}]}}\\n'
printf '{"type":"result","subtype":"success","is_error":false,"result":"done"}\\n'
"""


def test_a_task_already_running_is_not_interrupted_by_the_limit_being_reached(agent):
    """The gate is consulted before a CLAIM and nowhere else, so hitting the wall mid-run costs
    nothing that was already in flight. Killing the child instead would throw away minutes of work —
    and the relay would hand the same task to another provider to redo."""
    from remote import task_capacity, tasks

    agent(_SPENT_MID_RUN)
    capacity = task_capacity.TaskCapacity()

    outcome = tasks.run_task(_job(), capacity=capacity)

    assert outcome.state == "completed"          # the run finished, wall or no wall
    assert outcome.output == "done"
    assert capacity.pause_seconds() > 0.0        # and the NEXT claim is the one that waits


def test_the_limit_a_run_discovers_is_what_stops_the_next_claim(agent):
    """End to end through a real child: the provider's ceiling comes out of the agent's own stream,
    not out of a number anyone benchmarked. Nothing in this path knows how many tasks a subscription
    is worth — it only knows what the subscription just said."""
    from remote import task_capacity, tasks

    agent(_SPENT_MID_RUN)
    capacity = task_capacity.TaskCapacity()

    tasks.run_task(_job(), capacity=capacity)

    pause = capacity.pause_seconds()
    assert 3500.0 < pause <= 3600.0              # the vendor's own window, to the second


def test_an_ordinary_run_never_pauses_the_provider(agent):
    """The common case has to stay free. A run that says nothing about rate limits leaves the
    provider claiming exactly as it did before this slice."""
    from remote import task_capacity, tasks

    agent("printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")
    capacity = task_capacity.TaskCapacity()

    tasks.run_task(_job(), capacity=capacity)

    assert capacity.pause_seconds() == 0.0


def test_a_run_with_no_gate_wired_behaves_exactly_as_it_did_before(agent):
    """`capacity` is optional all the way down — the child is the point and the gate observes it."""
    from remote import tasks

    agent(_SPENT_MID_RUN)

    assert tasks.run_task(_job()).state == "completed"


def _concurrent_supervisors(monkeypatch):
    """Two `_run_and_report` calls that overlap: the first parks inside `run_task` until released.

    Returns `(start_first, reported)` — call `start_first(job)` to launch the parked supervisor and
    get back a `join()` for it, then call `_run_and_report` on the second job directly.
    """
    import threading

    from remote import tasks

    inside = threading.Event()
    finish = threading.Event()
    reported = []

    def run(job, *_a, **_k):
        inside.set()
        finish.wait(5)
        return tasks.TaskOutcome("completed", "", None)

    monkeypatch.setattr(tasks, "run_task", run)
    monkeypatch.setattr(tasks, "report_once", lambda _s, tid, **kw: reported.append(tid))
    monkeypatch.setattr(tasks, "_publisher_for", lambda *a, **k: _NullPublisher())
    monkeypatch.setattr(tasks, "_lease_renewer", lambda *a, **k: tasks._NoRenewal())

    def start_first(job):
        thread = threading.Thread(
            target=tasks._run_and_report, args=(_FakeState(), job), daemon=True)
        thread.start()
        assert inside.wait(5), "the first task never started"

        def release():
            finish.set()
            thread.join(5)
            # Checked, not assumed. The reservation is process-global and is released in the
            # background thread's `finally`; a join that timed out would leave the pair held for
            # the rest of the session, and every later test using it would fail instead of this one.
            assert not thread.is_alive(), "the first task's supervisor never finished"

        return release

    return start_first, reported


def test_two_members_of_one_project_run_side_by_side_on_one_provider(monkeypatch, capsys):
    """The point of the re-key (ADR 0033 D-g), and the failure it removes.

    A workspace belongs to a (project, member) pair, so two members' tasks in ONE project are two
    directories and must both run. Keyed on the project alone — as this was — the second is refused,
    and refused with NO terminal report by design: it sits `running` for a lease TTL, is reclaimed,
    and can be refused again, reaching `retries_exhausted` on a provider that had capacity the whole
    time. `GRID_MAX_TASKS` makes one provider claiming two tasks in one project ordinary.
    """
    from remote import tasks

    start_first, reported = _concurrent_supervisors(monkeypatch)
    release = start_first(_job(task_id="T1", member_key=_MEMBER))

    # Same project, a DIFFERENT member, while T1 is still running.
    tasks._run_and_report(_FakeState(), _job(task_id="T2", member_key=_OTHER_MEMBER))

    release()

    assert sorted(reported) == ["T1", "T2"], (
        "the second member's task was refused — the reservation is still keyed on the project")
    assert "refusing task" not in capsys.readouterr().err


def test_two_conversations_of_one_member_run_on_one_provider(monkeypatch, capsys):
    """Issue 38's seventh criterion, and the same argument as the test above one level down.

    One provider process, one member, two conversations. A workspace belongs to a conversation
    since ADR 0034 D-c, so these are two directories and both must run. Keyed on the pair — as this
    was — the second is refused, and refused with NO terminal report by design: it sits `running`
    for a lease TTL, is reclaimed, and can be refused again, reaching `retries_exhausted` on a
    provider that had capacity the whole time.

    ⚠️ Nothing in the relay makes this reachable YET — its index still allows one running turn per
    member, and issue 40 is what lifts that. The reservation is re-keyed in the same slice as the
    path because they are one fact: leaving it on the pair would make a member's second conversation
    refusable the moment concurrency is switched on, which is precisely the shape of the bug ADR
    0033 D-g records this site having already had once.
    """
    from remote import tasks

    start_first, reported = _concurrent_supervisors(monkeypatch)
    release = start_first(_job(task_id="T1", conversation_id=_CONVERSATION))

    # Same project, same MEMBER, a different conversation, while T1 is still running.
    tasks._run_and_report(
        _FakeState(), _job(task_id="T2", conversation_id=_OTHER_CONVERSATION))

    release()

    assert sorted(reported) == ["T1", "T2"], (
        "a member's second conversation was refused — the reservation is still keyed on the "
        "(project, member) pair")
    assert "refusing task" not in capsys.readouterr().err


@pytest.mark.parametrize("missing", ["project_id", "member_key", "conversation_id"])
def test_a_keyless_reservation_takes_nothing_so_the_readable_refusal_is_what_the_user_gets(missing):
    """`_reserve_workspace` answers `True` for an empty segment, and that is load-bearing.

    There is no workspace to protect — `run_task` refuses such a claim with a message naming the
    key — so reserving under a blank would make a SECOND keyless turn collide with the first and
    take the silent no-report path, replacing a refusal the user can read with one they cannot.

    Parametrized over all three because issue 38 added the third, and an empty `conversation_id`
    that reserved `(project, member, "")` would be a lock on a directory nothing uses while the two
    conversations sharing the real one are unguarded — the exact hole ADR 0034 D-c warns about.
    """
    from remote import tasks

    args = {"project_id": "proj-1", "member_key": _MEMBER, "conversation_id": _CONVERSATION}
    args[missing] = ""

    assert tasks._reserve_workspace(**args) is True
    assert tasks._reserve_workspace(**args) is True, (
        "a second keyless turn collided with the first, so it takes the silent no-report path "
        "instead of the refusal that names the missing key")
    assert not tasks._WORKSPACES_IN_USE, (
        f"a reservation was taken under a blank {missing} — it guards a directory nothing uses "
        f"and leaks past this test")


def test_two_workers_never_run_two_tasks_in_one_members_workspace(monkeypatch, capsys):
    """A workspace persists between that member's tasks, and preparing one runs `reset --hard` and
    `clean` over it. Two supervisors inside one workspace is therefore not a race that produces a
    confusing log — it is one agent's work being deleted underneath it while it runs.

    The relay's active-task index means this cannot happen. This is here because a provider may now
    run several tasks at once, and the cost of the invariant being wrong ONCE is destroyed work
    rather than a retry.
    """
    from remote import tasks

    start_first, reported = _concurrent_supervisors(monkeypatch)
    release = start_first(_job(task_id="T1"))

    tasks._run_and_report(_FakeState(), _job(task_id="T2"))   # same project AND same member

    release()

    # No terminal report for T2 — reporting one would mark it finished with nothing done. Left
    # `running`, its lease lapses and the relay hands it to a provider that can actually run it.
    assert reported == ["T1"]
    assert "already running" in capsys.readouterr().err


def test_a_projects_workspace_is_free_again_once_its_task_ends(monkeypatch):
    """The reservation is the LIFE of one call, not a latch. A project stuck reserved after its task
    ended would refuse every follow-up task on it for the life of the process."""
    from remote import tasks

    reported = []
    monkeypatch.setattr(tasks, "run_task", lambda job, *_a, **_k: tasks.TaskOutcome("completed", "", None))
    monkeypatch.setattr(tasks, "report_once", lambda _s, tid, **kw: reported.append(tid))
    monkeypatch.setattr(tasks, "_publisher_for", lambda *a, **k: _NullPublisher())
    monkeypatch.setattr(tasks, "_lease_renewer", lambda *a, **k: tasks._NoRenewal())

    tasks._run_and_report(_FakeState(), _job(task_id="T1"))
    tasks._run_and_report(_FakeState(), _job(task_id="T2"))   # same project, one after the other

    assert reported == ["T1", "T2"]


def test_a_project_is_freed_even_when_the_task_blows_up(monkeypatch):
    """Released in a `finally`: a supervisor that raised on its way out must not take the project's
    workspace with it, or one bad task locks that project out until the provider restarts."""
    from remote import tasks

    reported = []
    monkeypatch.setattr(tasks, "report_once", lambda _s, tid, **kw: reported.append(tid))
    monkeypatch.setattr(tasks, "_publisher_for", lambda *a, **k: _NullPublisher())
    monkeypatch.setattr(tasks, "_lease_renewer", lambda *a, **k: tasks._NoRenewal())

    monkeypatch.setattr(tasks, "run_task", _raising(RuntimeError("supervisor exploded")))
    tasks._run_and_report(_FakeState(), _job(task_id="T1"))
    monkeypatch.setattr(tasks, "run_task", lambda job, *_a, **_k: tasks.TaskOutcome("completed", "", None))
    tasks._run_and_report(_FakeState(), _job(task_id="T2"))

    assert [tid for tid in reported] == ["T1", "T2"]


def _raising(exc):
    def _run(*_a, **_k):
        raise exc

    return _run


# --- The package-manager cache environment (F-02, measured on the dev VM 2026-08-10) --------------


def test_the_cache_variables_point_inside_the_writable_tree(tmp_path):
    """Every manager is redirected to the one directory beside the workspace the sandbox grants.

    Asserted against `task_sandbox.cache_dir` rather than against a literal path, because the whole
    point is that the policy and the environment cannot disagree: a second spelling would grant one
    directory and use another, and the symptom is the `EROFS` this fixes with the policy looking
    correct.
    """
    from remote import task_agent, task_sandbox

    workspace = tmp_path / "projects" / "p" / "m" / "workspace"
    cache = task_sandbox.cache_dir(workspace)

    env = task_agent.child_env(workspace=workspace)

    for name in ("npm_config_cache", "YARN_CACHE_FOLDER", "PIP_CACHE_DIR", "XDG_CACHE_HOME"):
        assert name in env, f"{name} unset — that manager falls back to the read-only shared cache"
        assert str(cache) in env[name], f"{name} points outside the tree the sandbox made writable"


def test_TMPDIR_is_never_pointed_into_the_cache_tree(tmp_path):
    """The redirect that broke every task on the dev VM, pinned so it cannot come back.

    Claude Code's sandbox binds Unix domain sockets under `TMPDIR`, and `sun_path` is **108 bytes**
    — a kernel limit nothing in the error text mentions. MEASURED against the real provider layout:
    `/var/grid-provider/projects/<uuid-36>/<member_key-32>/cache/tmp` is already 107 bytes, so a
    socket inside it is 128 and `bind()` answers `AF_UNIX path too long`. The agent then reports
    `Sandbox is required but failed to initialize: Failed to create bridge sockets after 5
    attempts`, every Bash call fails, and the task still ends `completed`.

    Adding `TMPDIR` here looks like symmetry with the cache variables. It is not: a cache path is
    only ever opened as a file, and a temp path gets a socket bound in it.
    """
    from remote import task_agent, task_sandbox

    workspace = tmp_path / "projects" / "p" / "m" / "workspace"
    cache = str(task_sandbox.cache_dir(workspace))

    env = task_agent.child_env(workspace=workspace)

    assert cache not in env.get("TMPDIR", ""), (
        "TMPDIR under the cache tree exceeds sun_path (108) and kills the sandbox outright")


def test_the_real_provider_layout_leaves_no_room_for_a_socket_under_the_cache(tmp_path):
    """The measurement itself, so the reason above is a number rather than a story.

    Uses the real shapes — a 36-character project uuid and a 32-character member key — under the
    documented default root. If a future layout makes this comfortably short, the constraint above
    can be revisited on evidence instead of being deleted on taste.
    """
    from remote import task_sandbox

    workspace = Path("/var/grid-provider/projects") / ("p" * 36) / ("m" * 32) / "workspace"
    tmp_under_cache = task_sandbox.cache_dir(workspace) / "tmp"

    # 108 is `sizeof(sockaddr_un.sun_path)`; a socket FILE still has to fit inside the directory.
    assert len(str(tmp_under_cache)) > 108 - len("/claude-0/bridge.sock")


def test_cargo_home_is_deliberately_not_redirected(tmp_path):
    """`CARGO_HOME` holds credentials and installed binaries, not only a cache.

    Moving it per task would silently drop an operator's toolchain, so it is left alone — and this
    says so, because "add the rest of the managers" is the obvious next edit.
    """
    from remote import task_agent

    env = task_agent.child_env(workspace=tmp_path / "projects" / "p" / "m" / "workspace")

    assert "CARGO_HOME" not in env


def test_a_call_with_no_workspace_sets_no_cache_variables(tmp_path):
    """The parameter is optional, so the callers that only want the identity floor keep working.

    Pinned because the failure is silent in the other direction too: a cache variable pointing at a
    path no policy granted is worse than none at all.

    `TMPDIR` is deliberately NOT in this list. It is on the passthrough allowlist (`:114`), so the
    provider's own value reaches the child whether or not a workspace was given — which is the
    pre-existing behaviour, and the one `task_sandbox.policy` already agrees with because it reads
    the same variable. What must not appear is a value this function invented.
    """
    import os

    from remote import task_agent

    env = task_agent.child_env()

    for name in ("npm_config_cache", "YARN_CACHE_FOLDER", "PIP_CACHE_DIR", "XDG_CACHE_HOME"):
        assert name not in env
    assert env.get("TMPDIR", "") == os.environ.get("TMPDIR", ""), (
        "TMPDIR must pass through untouched when no workspace was given")


def test_ensure_cache_creates_the_tree_before_any_agent_runs(tmp_path):
    """The sandbox grants `<member>/workspace` and `<member>/cache`, never `<member>/` itself.

    So a package manager handed `…/cache/npm` can make its own subdirectories and could never make
    `cache`. Left to the agent, the bug returns wearing a different errno.
    """
    from remote import task_agent, task_sandbox

    workspace = tmp_path / "projects" / "p" / "m" / "workspace"
    task_agent.ensure_workspace(workspace)

    cache = task_agent.ensure_cache(workspace)

    assert cache == task_sandbox.cache_dir(workspace)
    assert cache.is_dir()
    # The SAME mode discipline as the workspace beside it, asserted by comparison rather than
    # against a literal: `_DIR_MODE` is 0o755 and the umask keeps its say, so a hard-coded 0o700
    # would encode this developer's umask rather than the rule. What must hold either way is that
    # nothing outside the owner can WRITE (ADR 0027).
    assert cache.stat().st_mode == workspace.stat().st_mode
    assert (cache.stat().st_mode & 0o022) == 0


def test_ensure_cache_is_idempotent(tmp_path):
    """A second task for the same (project, member) finds the tree warm, and must not fail on it."""
    from remote import task_agent

    workspace = tmp_path / "projects" / "p" / "m" / "workspace"
    task_agent.ensure_workspace(workspace)

    first = task_agent.ensure_cache(workspace)
    (first / "npm" ).mkdir()
    second = task_agent.ensure_cache(workspace)

    assert first == second
    assert (second / "npm").is_dir(), "an existing cache must survive — otherwise it is never warm"


def test_the_unresolved_message_names_the_index_and_never_claims_markers(
        tmp_path, monkeypatch, agent):
    """What the failure SAYS, on the conflict class where the old wording was false.

    The message used to assert "the conflict markers are still in the tree". For a modify/delete
    conflict there are none — git writes the surviving side verbatim and reports the conflict only
    through the index (`_modify_delete_remote` measures that). So in precisely the case this guard
    exists to catch, the explanation sent the reader hunting for a string that is not there, and the
    natural conclusion from not finding it is that the grid is wrong.

    It also pointed at the wrong tool. `git status` and `git ls-files --unmerged` answer this
    question; grep does not.

    Asserted on the message rather than on `pushed.unresolved` because the tuple was always right —
    it is the sentence built from it that was not, and nothing pinned the sentence at all.
    """
    from remote import tasks

    remote, input_commit, merge_ref = _modify_delete_remote(tmp_path)
    _relay_git_url(monkeypatch, remote.url)
    # Runs the merge, claims success, resolves nothing — and leaves no markers behind to find.
    agent(f'git merge --no-edit {merge_ref} >/dev/null 2>&1\n'
          "printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"done\"}\\n'\n")
    reported = []
    monkeypatch.setattr(tasks, "report_once",
                        lambda _s, tid, **kw: reported.append((tid, kw)))

    tasks._run_and_report(
        _FakeState(), {**_job_with_input(input_commit), "merge_ref": merge_ref})

    assert reported, "nothing was reported at all"
    error = reported[0][1].get("error") or ""
    assert reported[0][1]["state"] == "failed", reported[0][1]
    assert "shared.txt" in error, error
    assert "index" in error, f"the message must name what actually decided: {error!r}"
    assert "marker" not in error.lower(), (
        f"a modify/delete conflict leaves no markers; claiming otherwise sends the reader "
        f"looking for a string that is not there: {error!r}")


def test_checkout_result_resets_to_the_PINNED_commit_not_the_branch_tip(tmp_path):
    """`result_commit` is what THAT task produced; the branch can have moved since.

    A retry pushes the same ref, so the tip and the commit a task reported are different facts. The
    tip is the fallback for a task that reported NO commit — never a shortcut for the pinned case.

    Driven against a real repository, because a mock of `checkout_result` can only check what the
    caller PASSES. The client-side tests do exactly that and are blind here: replacing `commit` with
    the branch tip inside this function left every one of them green.
    """
    from remote import task_repo

    remote, first = _remote_for(tmp_path, "task/T1", {"a.txt": "first\n"})
    # The branch moves on, exactly as a retry's push would move it. Done through the seed clone so
    # the bare repo is advanced the way a real push advances it.
    seed = tmp_path / "seed-origin.git"
    _git(seed, "checkout", "-q", "task/T1")
    (seed / "a.txt").write_text("second\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "retry")
    _git(seed, "push", "-q", str(remote.url), "task/T1")
    assert _git(remote.url, "rev-parse", "task/T1").stdout.strip() != first

    dest = tmp_path / "pinned"
    dest.mkdir()

    task_repo.checkout_result(dest, url=remote.url, token="tok", branch="task/T1", commit=first)

    assert (dest / "a.txt").read_text() == "first\n", "fetched the branch tip, not the pinned commit"


def test_checkout_result_takes_the_branch_tip_when_no_commit_was_recorded(tmp_path):
    """The cancelled-task arm (ADR 0033 D-l, issue 19b).

    An agent killed before `commit_and_push` leaves a branch holding the task's input and no
    `result_commit` at all. `grid task cancel` promises that branch, so this has to be able to serve
    it — the client says which of the two arrived.
    """
    from remote import task_repo

    remote, _commit = _remote_for(tmp_path, "task/T1", {"a.txt": "input\n"})
    dest = tmp_path / "tip"
    dest.mkdir()

    task_repo.checkout_result(dest, url=remote.url, token="tok", branch="task/T1")

    assert (dest / "a.txt").read_text() == "input\n"


def test_checkout_result_still_refuses_when_there_is_no_branch(tmp_path):
    """No branch is nowhere to look, and that stays a refusal rather than a git error."""
    from remote import task_repo

    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})

    with pytest.raises(task_repo.CheckoutError):
        task_repo.checkout_result(tmp_path / "d", url=remote.url, token="tok",
                                  branch="", commit=commit)
