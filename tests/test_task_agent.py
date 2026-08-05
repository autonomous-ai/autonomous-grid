"""What child a task spawns, where it runs, and how its stream becomes events (ADR 0032, issue 03).

Split out of `test_local_cli.py` rather than appended to it: these cover the two new modules
(`remote/task_agent.py`, `remote/task_stream.py`) whose subject is the agent child itself, while the
claim/run/report loop's own tests stay beside the rest of the task-loop suite.
"""
import pytest


def test_workspace_is_the_shared_path_every_provider_must_agree_on(monkeypatch, tmp_path):
    """`<root>/projects/<project_id>/workspace` — a LOCKSTEP value, not a local preference.

    Claude Code derives a session's transcript directory from the working directory, so a provider
    using a different prefix cannot `--resume` a session another one started (ADR 0032). The root is
    overridable only so tests and dev boxes need not write to `/var`.
    """
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_ROOT", str(tmp_path))

    assert task_agent.workspace_for("proj-1") == tmp_path / "projects" / "proj-1" / "workspace"


@pytest.mark.parametrize("project_id", [
    "../../etc",           # climbs out of the root entirely
    "a/b",                 # a separator invents a level nobody agreed on
    "a\\b",                # the same on Windows, where `\` is the separator
    "/etc",                # absolute: `Path(root) / "/etc"` IS `/etc`, silently
    "",                    # empty: the path collapses to the projects directory itself
    ".",
    "..",
    "x" * 4096,            # longer than any filesystem accepts, so `mkdir` fails obscurely
])
def test_a_hostile_project_id_is_refused_before_anything_is_created(
        monkeypatch, tmp_path, project_id):
    """The project id arrives off the wire, so it is attacker-controlled (ADR 0032 D-b).

    `Path(root) / "../../etc"` is not a theoretical escape — it resolves, and the provider would then
    create a directory and run an agent with write access outside the tree entirely. Refused where the
    path is BUILT, so no caller can forget to check.
    """
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_ROOT", str(tmp_path))

    with pytest.raises(ValueError):
        task_agent.workspace_for(project_id)

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
    path = task_agent.workspace_for("proj-1")

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
    path = task_agent.workspace_for("proj-1")
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
        task_agent.ensure_workspace(task_agent.workspace_for("proj-1"))

    assert str(blocked) in str(excinfo.value)


def test_the_agent_is_spawned_in_print_mode_with_a_machine_readable_stream(monkeypatch):
    """The four flags this whole slice rests on, pinned as a wire contract with the binary.

    Verified against Claude Code 2.1.221: `--output-format stream-json` needs `--print`, and this
    repo's existing seat pairs it with `--verbose` (`shared/agent/seats/claude.py`). The prompt is an
    argv ELEMENT — nothing here reaches a shell, so a prompt containing `; rm -rf /` is just text.
    """
    from remote import task_agent

    monkeypatch.delenv("GRID_TASK_PERMISSION_MODE", raising=False)

    argv = task_agent.agent_argv("/usr/local/bin/claude", "fix the flaky test")

    assert argv == [
        "/usr/local/bin/claude",
        "-p", "fix the flaky test",
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "bypassPermissions",
    ]


def test_the_permission_mode_is_bypass_by_default_and_overridable_per_provider(monkeypatch):
    """`bypassPermissions` because print mode cannot answer a prompt — it denies it silently.

    ADR 0032 scopes untrusted providers out ("the current design assumes an internally operated
    fleet"), so the default is the one that lets a task actually do work. An operator who wants a
    narrower posture sets the variable; nothing else in the argv changes.
    """
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_PERMISSION_MODE", "acceptEdits")

    assert task_agent.agent_argv("claude", "x")[-1] == "acceptEdits"


def test_an_unknown_permission_mode_is_refused_rather_than_handed_to_the_binary(monkeypatch):
    """A typo'd mode is rejected HERE, where it can be explained.

    Handed through, the binary refuses it — and the provider reads that as "the agent failed",
    once per task, forever. The accepted set is the binary's own (`--permission-mode`, 2.1.221).
    """
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_PERMISSION_MODE", "bypassPermissons")  # missing an `i`

    with pytest.raises(ValueError) as excinfo:
        task_agent.agent_argv("claude", "x")

    assert "bypassPermissons" in str(excinfo.value)


def test_the_binary_is_found_through_the_shared_resolver(monkeypatch):
    """One answer to "where is Claude Code" per machine, not two.

    `shared/launch/claude_install` already searches PATH and both conventional install locations and
    reports what it could not check; a second search here would drift from it.
    """
    from shared.launch import claude_install

    from remote import task_agent

    monkeypatch.setattr(
        claude_install, "resolve",
        lambda: claude_install.Resolution(binary="/opt/claude", unchecked=()))

    assert task_agent.resolve_binary() == "/opt/claude"


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


def test_the_child_environment_is_the_providers_own_and_the_cli_process_is_untouched(monkeypatch):
    """ADR 0028's rule: the child's environment is set on the CHILD, never exported anywhere.

    The agent authenticates with the PROVIDER's own Claude subscription — nothing about the grid's
    relay or the requesting user's token belongs in here.
    """
    from remote import task_agent

    monkeypatch.delenv("GRID_TASK_CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("A_MARKER_THE_PROVIDER_SET", "kept")
    before = dict(__import__("os").environ)

    env = task_agent.child_env()

    assert env["A_MARKER_THE_PROVIDER_SET"] == "kept"
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
    {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
    {"type": "stream_event", "event": {"type": "content_block_delta"}},
    {"type": "something_a_future_version_added"},
    {"no_type_at_all": 1},
])
def test_a_record_this_build_does_not_know_is_ignored_rather_than_guessed_at(record):
    """Ignoring is what makes a new record type free to arrive — including `rate_limit_event`, which
    issue 09 consumes and which this slice's only obligation is not to choke on."""
    from remote import task_stream

    assert task_stream.StreamTranslator().feed(_line(record)) == []


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
def agent(monkeypatch, tmp_path):
    """Point `run_task` at a fake binary and a writable workspace root."""
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_ROOT", str(tmp_path / "root"))
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
    job = {"task_id": "T1", "project_id": "proj-1", "prompt": "fix the flaky test", "attempt": 1}
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
    assert outcome.output == str(task_agent.workspace_for("proj-1"))
    assert outcome.session_id == "sess-1"


def test_the_prompt_and_the_streaming_flags_reach_the_binary(agent):
    """Pinned through a real spawn, not by inspecting a list: a flag the binary never receives is
    the failure this catches, and only the child can report what it was actually given."""
    from remote import tasks

    agent("printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"%s\"}\\n' \"$*\"\n")

    outcome = tasks.run_task(_job(prompt="fix the flaky test"))

    assert outcome.output == (
        "-p fix the flaky test --output-format stream-json --verbose "
        "--permission-mode bypassPermissions")


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

    workspace = task_agent.ensure_workspace(task_agent.workspace_for("proj-1"))
    (workspace / "stale.txt").write_text("from the last task\n")
    transcript = task_agent.transcript_dir(workspace)
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

    workspace = task_agent.workspace_for("proj-1")
    assert _git(workspace, "symbolic-ref", "HEAD").stdout.strip() == "refs/heads/task/T1"
    assert _git(workspace, "rev-parse", "HEAD").stdout.strip() == commit


def test_input_that_cannot_be_checked_out_fails_the_task_without_spawning_the_agent(
        agent, tmp_path):
    """An agent that ran against missing input produces a confidently wrong result, which is the
    exact failure ADR 0032 D-b exists to prevent. Failing before the spawn is the only safe answer."""
    from remote import tasks

    agent("printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n"
          "touch \"$GRID_TEST_RAN\"\n")
    ran = tmp_path / "the-agent-ran"
    import os
    os.environ["GRID_TEST_RAN"] = str(ran)
    try:
        from remote.task_repo import GitRemote
        outcome = tasks.run_task(
            _job(input_commit="0" * 40, branch="task/T1"),
            remote=GitRemote(url=str(tmp_path / "nothing-here.git"), token="tok"))
    finally:
        os.environ.pop("GRID_TEST_RAN", None)

    assert outcome.state == "failed"
    assert not ran.exists(), "the agent was spawned against input that never arrived"
    assert "workspace" in (outcome.error or "").lower() or "input" in (outcome.error or "").lower()


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

    def fake_run(job, publish=None, on_spawn=None, remote=None):
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
                                         "project_id": "proj-1", "input_commit": "c" * 40})

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

    tasks._run_and_report(_FakeState(), {"task_id": "T1", "prompt": "p", "project_id": "proj-1"})

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


def _job_with_input(commit, branch="task/T1"):
    return {"task_id": "T1", "prompt": "p", "project_id": "proj-1",
            "branch": branch, "input_commit": commit}


def test_the_agents_work_is_pushed_with_its_conversation_but_not_the_rest_of_the_reserved_directory(
        agent, tmp_path, monkeypatch):
    """The issue's demo from the provider's end: the agent writes a file, and that file is in the
    commit the relay is told about — and so is the conversation it had.

    `.grid/agent/` rides along; the rest of `.grid/` does not. The transcript and the agent's
    `memory/` ARE the project's conversation, and the ordinary result commit is how they reach the
    project's next task and its next provider (issue 06). Anything else under `.grid/` is the
    provider's own state and belongs in nobody else's repository.

    **This reverses issue 05**, which excluded all of `.grid/` on the reasoning that the transcript
    was provider internals. That exclusion makes cross-provider resume impossible — the whole of
    issue 06 — and contradicts ADR 0032 D-b, which describes the transcript as "then committed back
    to the user's repo". The relay's refusal to accept an UPLOAD anywhere under `.grid/` is
    unchanged, so the conversation is still written only by the provider.
    """
    from remote import tasks

    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    _relay_git_url(monkeypatch, remote.url)
    agent("mkdir -p .grid/agent/memory\n"
          "echo transcript > .grid/agent/sess-1.jsonl\n"
          "echo remembered > .grid/agent/memory/note.md\n"
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
    assert "fix.py" in listing
    assert ".grid/agent/sess-1.jsonl" in listing, listing
    assert ".grid/agent/memory/note.md" in listing, listing
    assert ".grid/scratch.txt" not in listing, listing


def test_the_projects_own_gitignore_cannot_suppress_the_conversation(
        agent, tmp_path, monkeypatch):
    """A **tracked** `.gitignore` outranks `$GIT_DIR/info/exclude`, so the conversation is
    force-added rather than merely un-excluded.

    Entirely plausible input: the requesting user's repository is an AI-agent project that ignores
    `*.jsonl` or `memory/`, or the agent — running under `bypassPermissions` — writes a `.gitignore`
    itself. The failure it produces is the silent kind this feature is most exposed to: the task
    still reports `completed` with a session id, the transcript simply never enters the commit, and
    nothing surfaces until a *different* provider finds no conversation to resume and reports the
    misleading "no transcript for session X in this workspace".

    The same provider masks it for a while, too — its local `.grid/agent/` survives
    `clean -ffdx -e .grid` whether or not it was ever pushed.
    """
    from remote import tasks

    remote, commit = _remote_for(
        tmp_path, "task/T1",
        {"a.txt": "x\n", ".gitignore": ".grid/agent/\n*.jsonl\nmemory/\n"})
    _relay_git_url(monkeypatch, remote.url)
    agent("mkdir -p .grid/agent/memory\n"
          "echo transcript > .grid/agent/sess-1.jsonl\n"
          "echo remembered > .grid/agent/memory/note.md\n"
          "printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")
    reported = {}
    monkeypatch.setattr(tasks, "report_once", lambda _s, tid, **kw: reported.update(kw))
    monkeypatch.setattr(tasks, "_publisher_for", lambda *a, **k: _NullPublisher())

    tasks._run_and_report(_FakeState(), _job_with_input(commit))

    assert reported["state"] == "completed"
    listing = _git(remote.url, "ls-tree", "-r", "--name-only", reported["result_commit"]).stdout.split()
    assert ".grid/agent/sess-1.jsonl" in listing, listing
    assert ".grid/agent/memory/note.md" in listing, listing


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
    workspace = task_agent.workspace_for("proj-1")
    import shutil
    shutil.rmtree(task_agent.transcript_dir(workspace))

    from remote import task_repo
    pushed = task_repo.commit_and_push(
        workspace, url=remote.url, token=remote.token, branch="task/T1", message="task T1")

    listing = _git(remote.url, "ls-tree", "-r", "--name-only", pushed).stdout.split()
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


def test_a_task_with_no_git_plane_reports_normally_and_pushes_nothing(agent, monkeypatch):
    """Old-relay degrade, on the push side. A claim with no `input_commit` has no branch to push,
    so the loop must report exactly as it did before the git plane existed — degrading to the
    PREVIOUS behaviour, never to a new failure."""
    from remote import tasks

    agent("printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")
    reported = {}
    monkeypatch.setattr(tasks, "report_once", lambda _s, tid, **kw: reported.update(kw))
    monkeypatch.setattr(tasks, "_publisher_for", lambda *a, **k: _NullPublisher())

    tasks._run_and_report(_FakeState(), {"task_id": "T1", "prompt": "p", "project_id": "proj-1"})

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
]


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


def test_what_the_agent_writes_to_its_transcript_directory_lands_in_the_workspace(
        monkeypatch, tmp_path):
    """The whole mechanic of issue 06, in one assertion.

    Claude Code writes through a symlink (measured by the issue-01 spike), so pointing its
    per-cwd transcript folder at a directory inside the git worktree captures the transcript *and*
    the agent's `memory/` with no copying step — the repository that already carries the project's
    files carries its conversation too.
    """
    from remote import task_agent

    config = tmp_path / "provider-config"
    monkeypatch.setenv("GRID_TASK_CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("GRID_TASK_ROOT", str(tmp_path / "root"))
    workspace = task_agent.ensure_workspace(task_agent.workspace_for("proj-1"))

    target = task_agent.link_transcript(workspace)

    link = config / "projects" / task_agent.transcript_dir_name(workspace)
    assert link.is_symlink(), f"{link} is not a symlink"
    # Writing THROUGH the link is exactly what the agent does; asserting on the link's own target
    # would prove the call, not the behaviour that makes the feature work.
    (link / "sess-1.jsonl").write_text('{"type":"system"}\n', encoding="utf-8")
    (link / "memory").mkdir()
    (link / "memory" / "note.md").write_text("remembered", encoding="utf-8")

    assert target == workspace / ".grid" / "agent"
    assert (target / "sess-1.jsonl").read_text(encoding="utf-8") == '{"type":"system"}\n'
    assert (target / "memory" / "note.md").read_text(encoding="utf-8") == "remembered"


def test_a_real_directory_where_the_symlink_belongs_is_refused_and_nothing_is_deleted(
        monkeypatch, tmp_path):
    """Not hypothetical: every provider that ran tasks before this slice has one.

    Issues 03-05 spawned the agent with no symlink in place, so Claude Code created that directory
    itself and filled it with the project's conversation. Replacing it blind would delete exactly
    the history this feature exists to preserve, so the provider refuses and names the path — an
    operator moves it once. A symlink is different and IS replaced: it holds no data.
    """
    from remote import task_agent

    config = tmp_path / "provider-config"
    monkeypatch.setenv("GRID_TASK_CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("GRID_TASK_ROOT", str(tmp_path / "root"))
    workspace = task_agent.ensure_workspace(task_agent.workspace_for("proj-1"))
    squatter = config / "projects" / task_agent.transcript_dir_name(workspace)
    squatter.mkdir(parents=True)
    (squatter / "older-session.jsonl").write_text("a conversation from before 06\n", encoding="utf-8")

    with pytest.raises(OSError) as excinfo:
        task_agent.link_transcript(workspace)

    # A bare `FileExistsError` from `symlink_to` would also carry the path and would also be an
    # OSError — and it would tell an operator nothing about what to do. The refusal has to be the
    # provider's own, and it has to say how to clear it, or every task on this project fails with a
    # message that reads like a bug in grid.
    message = str(excinfo.value)
    assert str(squatter) in message, message
    assert "move or remove" in message.lower(), message
    assert (squatter / "older-session.jsonl").exists(), "an operator's transcript was destroyed"


def test_a_config_directory_inside_the_workspace_is_refused(monkeypatch, tmp_path):
    """ADR 0032 D-b's hazard, reached from the provider's own side rather than a client's upload.

    The config directory holds the provider's Claude subscription credential. Inside the workspace
    it is inside the git worktree, and the result push commits it into the requesting user's
    repository — the one leak this whole design is arranged to prevent.
    """
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_ROOT", str(tmp_path / "root"))
    workspace = task_agent.ensure_workspace(task_agent.workspace_for("proj-1"))
    monkeypatch.setenv("GRID_TASK_CLAUDE_CONFIG_DIR", str(workspace / "provider-config"))

    with pytest.raises(OSError) as excinfo:
        task_agent.link_transcript(workspace)

    assert "credential" in str(excinfo.value).lower(), excinfo.value


def test_an_operator_who_configures_nothing_gets_claude_codes_own_default(monkeypatch, tmp_path):
    """The commonest production configuration, and the one the suite's safety net hides.

    `tests/conftest.py` points `GRID_TASK_CLAUDE_CONFIG_DIR` at a temp directory for every test, so
    that nothing plants symlinks in the developer's real `~/.claude` — which means no other test
    exercises the fallback that almost every real provider actually uses. Proved here against a
    stubbed home rather than the real one, so the coverage costs nothing.
    """
    from pathlib import Path

    from remote import task_agent

    monkeypatch.delenv("GRID_TASK_CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("GRID_TASK_ROOT", str(tmp_path / "root"))
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))

    assert task_agent.configured_claude_config_dir() is None
    assert task_agent.claude_config_dir() == fake_home / ".claude"

    workspace = task_agent.ensure_workspace(task_agent.workspace_for("proj-1"))
    task_agent.link_transcript(workspace)

    link = fake_home / ".claude" / "projects" / task_agent.transcript_dir_name(workspace)
    assert link.is_symlink(), f"{link} was not created under the default config directory"


def test_a_relative_config_directory_is_refused(monkeypatch, tmp_path):
    """`child_env` hands the value to the child verbatim, and the child resolves it against ITS
    working directory — which is the workspace. So a relative config directory is the previous test's
    leak wearing a different spelling, and it is refused where the value is read."""
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_ROOT", str(tmp_path / "root"))
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
        "/usr/local/bin/claude", "and now write the tests", resume="012c9e09-abcd")

    assert argv == [
        "/usr/local/bin/claude",
        "-p", "and now write the tests",
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "bypassPermissions",
        "--resume", "012c9e09-abcd",
    ]


def _transcript(workspace, session_id, body='{"type":"summary"}\n'):
    """Put a transcript for `session_id` where a checkout would have left one."""
    from remote import task_agent

    directory = task_agent.transcript_dir(workspace)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    path.write_text(body, encoding="utf-8")
    return path


def test_a_session_whose_transcript_arrived_with_the_checkout_is_resumable(monkeypatch, tmp_path):
    """The relay says WHICH session; the workspace says WHETHER it is here. Both, or no resume.

    The relay's answer alone is not enough: it names the project's last conversation, but the
    transcript only reaches this provider if the commit carrying it was fast-forwarded onto `main`.
    """
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_ROOT", str(tmp_path / "root"))
    workspace = task_agent.ensure_workspace(task_agent.workspace_for("proj-1"))
    _transcript(workspace, "012c9e09-abcd")

    decision = task_agent.resumable_session(workspace, "012c9e09-abcd")

    assert decision.session_id == "012c9e09-abcd"
    assert decision.reason is None


def test_a_project_with_no_conversation_yet_starts_fresh_and_says_nothing(monkeypatch, tmp_path):
    """The project's FIRST task. Not a degraded outcome, so there is nothing to report."""
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_ROOT", str(tmp_path / "root"))
    workspace = task_agent.ensure_workspace(task_agent.workspace_for("proj-1"))

    decision = task_agent.resumable_session(workspace, None)

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
        monkeypatch, tmp_path, hostile):
    """The id is used to BUILD A PATH, so it gets `workspace_for`'s allowlist rule.

    It arrives from the relay rather than directly from a user, which makes it one hop further away
    — not trustworthy. A provider that followed `../../../` here would read, and then commit into
    the requesting user's repository, a file from outside the workspace entirely.
    """
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_ROOT", str(tmp_path / "root"))
    workspace = task_agent.ensure_workspace(task_agent.workspace_for("proj-1"))

    decision = task_agent.resumable_session(workspace, hostile)

    assert decision.session_id is None
    # The REASON matters as much as the verdict: without the allowlist these ids also return None,
    # simply because nothing happens to exist where they point. Asserting only "no resume" passes
    # just as happily with the guard deleted, which is how a path-escape check rots into decoration.
    assert "not a safe id" in (decision.reason or ""), decision.reason


@pytest.mark.parametrize("wrong_type", [7, True, ["s"], {"a": 1}, 3.5])
def test_a_session_id_of_the_wrong_type_degrades_instead_of_killing_the_task(
        monkeypatch, tmp_path, wrong_type):
    """`claim_task` returns `resp.json()` verbatim, so this field can be any JSON type.

    `re.match` raises `TypeError` on a non-string, and this call sits outside `run_task`'s
    try/except blocks — so a relay that serialized the field wrong would not merely lose the
    resume, it would lose the whole attempt: the raise unwinds past `_push_result`, so nothing is
    committed or pushed, and the user gets "task runner raised: TypeError(...)" instead of the
    agent's work. Every other wire-sourced value in `run_task` is already `str()`-cast or
    `isinstance`-checked; this one gets the same rule.
    """
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_ROOT", str(tmp_path / "root"))
    workspace = task_agent.ensure_workspace(task_agent.workspace_for("proj-1"))

    decision = task_agent.resumable_session(workspace, wrong_type)

    assert decision.session_id is None
    assert "not a safe id" in (decision.reason or ""), decision.reason


def test_a_session_id_cannot_reach_a_file_outside_the_transcript_directory(monkeypatch, tmp_path):
    """The escape the allowlist actually exists to stop, with a real file at the far end.

    `.grid/` holds provider-local state that is deliberately NOT committed. A relay — or anything
    that reached one — naming `../something` would have the provider read that file as a transcript
    and hand it to `--resume`. Planting a readable file there is what makes this test fail when the
    guard is removed, rather than passing on the accident that the path was empty.
    """
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_ROOT", str(tmp_path / "root"))
    workspace = task_agent.ensure_workspace(task_agent.workspace_for("proj-1"))
    task_agent.transcript_dir(workspace).mkdir(parents=True, exist_ok=True)
    outside = workspace / task_agent.task_repo.RESERVED_DIR / "sneaky.jsonl"
    outside.write_text('{"type": "summary"}\n', encoding="utf-8")

    decision = task_agent.resumable_session(workspace, "../sneaky")

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
    workspace = task_agent.ensure_workspace(task_agent.workspace_for("proj-1"))
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
    task_agent.ensure_workspace(task_agent.workspace_for("proj-1"))

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

    task_agent.ensure_workspace(task_agent.workspace_for("proj-1"))
    remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    agent("echo done > out.txt\n"
          "printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")

    def exploding(kind, **fields):
        raise RuntimeError(f"the publisher is broken ({kind})")

    outcome = tasks.run_task(
        _job(input_commit=commit, branch="task/T1", resume_session_id="012c9e09-abcd"),
        publish=exploding, remote=remote)

    assert outcome.state == "completed", outcome.error
    assert (task_agent.workspace_for("proj-1") / "out.txt").exists(), "the agent never ran"


def test_a_relay_that_names_no_session_runs_fresh_without_reporting_anything(
        agent, tmp_path, monkeypatch):
    """A relay predating issue 06 sends no `resume_session_id`, and so does the FIRST task on every
    project. Neither is a degraded outcome, so neither says anything — exactly how a missing
    `input_commit` degrades to the pre-git-plane behaviour rather than to a new failure."""
    from remote import task_agent, tasks

    monkeypatch.setenv("GRID_TASK_CLAUDE_CONFIG_DIR", str(tmp_path / "provider-config"))
    task_agent.ensure_workspace(task_agent.workspace_for("proj-1"))

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
    """The acceptance criterion, and the reason the transcript is committed at all.

    A single-host rehearsal of the issue-01 spike: provider A runs a task and pushes; its workspace
    is then **deleted entirely** and a **different config directory** takes over, which is what
    provider B has — no workspace, no transcript, nothing but the repository. The workspace PATH
    stays identical, because that is the lockstep value Claude Code derives the transcript
    directory from.

    What this does NOT prove is a genuinely different machine, OS, or Claude Code version. Providers
    should stay version-pinned until a two-host run says otherwise (PRD, "still unproven").
    """
    import shutil

    from remote import task_agent, tasks

    # --- provider A -------------------------------------------------------------------------
    monkeypatch.setenv("GRID_TASK_CLAUDE_CONFIG_DIR", str(tmp_path / "provider-a-config"))
    workspace = task_agent.workspace_for("proj-1")
    link = (task_agent.claude_config_dir() / "projects"
            / task_agent.transcript_dir_name(workspace))
    remote, first_input = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
    # Writes THROUGH the config-directory path, exactly as the real agent does. Writing to
    # `$PWD/.grid/agent` instead would land in the right place while proving nothing about the link.
    monkeypatch.setenv("GRID_TEST_LINK", str(link))
    agent('mkdir -p "$GRID_TEST_LINK/memory"\n'
          'printf \'{"type":"summary"}\\n\' > "$GRID_TEST_LINK/sess-1.jsonl"\n'
          'echo remembered > "$GRID_TEST_LINK/memory/note.md"\n'
          "printf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\"}\\n'\n")

    first = tasks.run_task(_job(input_commit=first_input, branch="task/T1"), remote=remote)
    assert first.state == "completed", first.error
    pushed = task_repo_commit_and_push(workspace, remote, "task/T1")

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
             resume_session_id="sess-1"),
        remote=remote)

    assert second.state == "completed", second.error
    assert "--resume sess-1" in seen.read_text(encoding="utf-8"), \
        "provider B started a fresh session instead of continuing the project's"
    # The conversation itself arrived, not merely the id — including the agent's own memory.
    rebuilt = task_agent.transcript_dir(task_agent.workspace_for("proj-1"))
    assert (rebuilt / "sess-1.jsonl").exists()
    assert (rebuilt / "memory" / "note.md").read_text(encoding="utf-8") == "remembered\n"


def task_repo_commit_and_push(workspace, remote, branch):
    """Commit and push what the agent left, the way `_push_result` does."""
    from remote import task_repo

    return task_repo.commit_and_push(
        workspace, url=remote.url, token=remote.token, branch=branch, message="task T1 (completed)")


@pytest.mark.parametrize("body,expected_in_reason", [
    ("", "empty"),
    ("   \n", "empty"),
    ("this is not json\n", "JSON"),
    ("[1, 2, 3]\n", "JSON"),          # valid JSON, but no record Claude Code ever wrote
    ('{"type": "summary"' + "\n", "JSON"),   # truncated mid-object: a push that died halfway
])
def test_a_transcript_that_cannot_be_read_starts_a_fresh_session_with_a_reason(
        monkeypatch, tmp_path, body, expected_in_reason):
    """Corrupt is a fresh start, never a failed task — and never a silent one.

    `--resume` against a broken transcript fails the whole task, so the check happens here where the
    answer can still be "start over and tell the user", which is what the acceptance criterion asks
    for.
    """
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_ROOT", str(tmp_path / "root"))
    workspace = task_agent.ensure_workspace(task_agent.workspace_for("proj-1"))
    _transcript(workspace, "sess-1", body=body)

    decision = task_agent.resumable_session(workspace, "sess-1")

    assert decision.session_id is None
    assert expected_in_reason in decision.reason, decision.reason


def test_a_transcript_that_never_arrived_starts_a_fresh_session_with_a_reason(
        monkeypatch, tmp_path):
    """The reachable case: the project's previous task FAILED, so its transcript was never
    fast-forwarded onto `main` and this checkout legitimately does not carry it — while the relay
    still remembers the session id that attempt reported."""
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_ROOT", str(tmp_path / "root"))
    workspace = task_agent.ensure_workspace(task_agent.workspace_for("proj-1"))

    decision = task_agent.resumable_session(workspace, "012c9e09-abcd")

    assert decision.session_id is None
    assert "no transcript" in decision.reason, decision.reason


def test_a_transcript_that_is_a_symlink_is_never_followed(monkeypatch, tmp_path):
    """Nothing legitimate plants one — the agent writes through OUR symlink, and a checkout cannot
    create one (`core.symlinks=false`). Following it would read, and then commit, its target."""
    from remote import task_agent

    monkeypatch.setenv("GRID_TASK_ROOT", str(tmp_path / "root"))
    workspace = task_agent.ensure_workspace(task_agent.workspace_for("proj-1"))
    secret = tmp_path / "credentials.json"
    secret.write_text('{"access_token": "the provider\'s own"}\n', encoding="utf-8")
    directory = task_agent.transcript_dir(workspace)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "sess-1.jsonl").symlink_to(secret)

    decision = task_agent.resumable_session(workspace, "sess-1")

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
