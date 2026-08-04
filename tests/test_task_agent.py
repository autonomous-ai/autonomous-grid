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
