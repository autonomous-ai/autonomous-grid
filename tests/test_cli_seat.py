"""CLI seat: the shared pipeline, and each adapter's own translation."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from shared.agent import cli_seat
from shared.agent.seats import SEATS, claude, codex, seat_for
from shared.models import api_catalog


# registry / catalog agreement

def test_every_seat_has_a_catalog_row_declaring_it_local():
    """The registry and the catalog are two halves of one fact. A seat missing its row would join
    (the driver exists) and then advertise nothing, or vice versa."""
    for kind, spec in SEATS.items():
        assert api_catalog.local_seat_port(kind) is not None, f"{kind} has no local_seat_port"
        assert api_catalog.kind_credential(kind) == "none", f"{kind} must hold no credential"
        assert spec.kind == kind


def test_seat_kinds_do_not_collide_with_the_oauth_codex_seat():
    """`codex` is the OAuth HTTP seat (ADR 0015); the CLI seat must be a different kind so a grid
    can serve both at once."""
    assert "codex" not in SEATS
    assert "codex-cli" in SEATS
    assert api_catalog.local_seat_port("codex") is None
    assert api_catalog.kind_credential("codex") == "oauth"


def test_seats_bind_different_default_ports():
    ports = [api_catalog.local_seat_port(kind) for kind in SEATS]
    assert len(set(ports)) == len(ports), "two seats would fight over one loopback port"


def test_seat_for_names_what_is_available():
    with pytest.raises(cli_seat.SeatError) as exc:
        seat_for("nope")
    assert "claude" in str(exc.value)


# model resolution

def test_advertised_models_come_from_the_catalog():
    assert cli_seat.advertised_models("claude") == [
        api_catalog.advertised_name("claude", e) for e in api_catalog.WHITELISTS["claude"].entries
    ]


def test_alias_accepts_namespaced_and_bare_names():
    """The relay rewrites an advertised name to its upstream spelling before forwarding, so the
    seat legitimately receives both forms."""
    assert cli_seat.alias_for("claude", "claude:sonnet") == "sonnet"
    assert cli_seat.alias_for("claude", "sonnet") == "sonnet"
    assert cli_seat.alias_for("claude", "gpt-4") is None


# prompt building

def test_single_user_turn_passes_through_verbatim():
    assert cli_seat.build_prompt([{"role": "user", "content": "hi"}]) == "hi"


def test_a_replayed_turn_uses_the_shape_the_instruction_asks_for():
    """The history and the instruction must spell a call the same way. They did not: calls were
    replayed as Hermes `<tool_call>` tags while the instruction asked for this JSON, so from the
    second turn on the model saw one convention and was told another."""
    prompt = cli_seat.build_prompt([
        {"role": "user", "content": "weather?"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"function": {"name": "get_weather", "arguments": '{"loc":"Hanoi"}'}}]},
        {"role": "tool", "content": '{"temp":31}'},
    ])
    assert '"tool_calls"' in prompt and '"name": "get_weather"' in prompt
    # The encoded string is decoded on the way in, so the model reads an object, as it must write one.
    assert '"arguments": {"loc": "Hanoi"}' in prompt
    assert "Tool result:" in prompt
    assert "<tool_call>" not in prompt


def test_the_tool_block_goes_in_the_turn_not_the_system_prompt():
    """Measured against Claude Code's real 27 KB system prompt: appended to it, the model answered
    "I don't have access to a Read tool"; moved to the end of the turn — the last thing it reads —
    it emitted the call. So the system prompt stays the caller's, verbatim."""
    messages = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}]
    tools = [{"type": "function", "function": {"name": "f"}}]
    assert cli_seat.build_system_prompt(messages) == "be brief"

    body = {"model": "claude:sonnet", "messages": messages}
    assert cli_seat.prepare(body, "claude").prompt == "hi"
    with_tools = cli_seat.prepare({**body, "tools": tools}, "claude")
    assert with_tools.system_prompt == "be brief", "the tool block must not reach the system prompt"
    assert with_tools.prompt.startswith("hi")
    assert "tool_calls" in with_tools.prompt


def test_images_are_named_not_silently_dropped():
    prompt = cli_seat.build_prompt([{"role": "user", "content": [
        {"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "x"}},
    ]}])
    assert "image omitted" in prompt


def test_a_thinking_block_folds_its_reasoning_into_the_prompt():
    """Regression: `_flatten_content` handled `text`, `image_url` and `input_image` only, so a
    replayed `thinking` block — the model's own prior reasoning — vanished with no trace. From
    turn two the model lost its own chain of thought and nobody was told."""
    prompt = cli_seat.build_prompt([{"role": "user", "content": [
        {"type": "thinking", "thinking": "first I considered X, then Y", "signature": "sig-abc"},
        {"type": "text", "text": "so the answer is 4"},
    ]}])
    assert "first I considered X, then Y" in prompt
    assert "so the answer is 4" in prompt
    # the reasoning appears before the text that followed it — client block order preserved
    assert prompt.index("first I considered X") < prompt.index("so the answer is 4")
    # signature is opaque and only meaningful on the way back out; this seat never produces one
    assert "sig-abc" not in prompt


def test_an_unrecognised_block_leaves_a_visible_marker():
    """An unknown block type used to be dropped with no trace at all — worse than an image, which
    at least leaves `[image omitted ...]`. The next unhandled shape must be discoverable, not
    invisible."""
    prompt = cli_seat.build_prompt([{"role": "user", "content": [
        {"type": "text", "text": "look"}, {"type": "some_future_block", "data": "x"},
    ]}])
    assert "look" in prompt
    assert "some_future_block" in prompt and "omitted" in prompt


def test_prepare_rejects_an_unserved_model():
    with pytest.raises(cli_seat.SeatBadRequest):
        cli_seat.prepare({"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}, "claude")


# the answer is text, not parsed

def test_the_answer_is_returned_verbatim():
    """A CLI has no native tool channel, so any tool convention is the caller's own — the seat
    hands back the model's text untouched and the caller parses it."""
    raw = 'Checking.\n<tool_call>\n{"name": "f", "arguments": {}}\n</tool_call>'
    done = cli_seat.to_chat_completion(cli_seat.SeatResult(text=raw), "claude", "claude:sonnet")
    assert done["choices"][0]["message"]["content"] == raw
    assert done["choices"][0]["finish_reason"] == "stop"
    assert "tool_calls" not in done["choices"][0]["message"]
    assert done["grid_cli_seat"]["kind"] == "claude"


def test_the_protocol_asks_for_the_shape_the_model_already_knows():
    """The model is shown the OpenAI tool_calls shape rather than a bespoke one, because that is
    what it has seen on the wire — and it is what the relay translates without a second mapping."""
    assert cli_seat.OPENAI.name == "openai"
    rendered = cli_seat.OPENAI.render([{"type": "function", "function": {"name": "f"}}])
    assert '"tool_calls"' in rendered and '"arguments"' in rendered


# quota gate

def test_unknown_quota_never_refuses():
    """Fail-open: this parses human-formatted prose, so a wording change must not take a healthy
    seat off the grid."""
    options = cli_seat.SeatOptions(session_limit=1, week_limit=1)
    assert cli_seat.quota_refusal(None, options) is None


def test_ceilings_refuse_and_weekly_wins():
    snapshot = cli_seat.QuotaSnapshot(session_pct=50, week_pct=90, week_reset="Aug 4")
    assert cli_seat.quota_refusal(snapshot, cli_seat.SeatOptions()) is None
    assert "session" in cli_seat.quota_refusal(snapshot, cli_seat.SeatOptions(session_limit=10))
    both = cli_seat.quota_refusal(snapshot, cli_seat.SeatOptions(session_limit=10, week_limit=10))
    assert "weekly" in both and "Aug 4" in both


def test_a_seat_without_a_usage_screen_reports_unknown():
    assert cli_seat.probe_quota(SEATS["codex-cli"], "/nonexistent") is None


# options round-trip

def test_options_survive_flags_then_record_then_child_argv():
    args = SimpleNamespace(seat_port=None, seat_timeout=30.0, seat_concurrency=None,
                           seat_session_limit=80, seat_week_limit=None, seat_quota_ttl=None)
    options = cli_seat.options_from_args(args, default_port=9999)
    assert options.port == 9999 and options.timeout == 30.0 and options.session_limit == 80

    restored = cli_seat.options_from_spec({"seat": cli_seat.options_to_dict(options)})
    assert restored == options

    argv = cli_seat.options_child_argv(restored, "claude")
    assert "--session-limit" in argv and "80" in argv
    assert "--week-limit" not in argv  # an unset ceiling is absent, not None-as-a-string


# adapters

def test_claude_invoke_disables_tools_and_replaces_the_system_prompt(tmp_path: Path):
    prepared = cli_seat.PreparedRequest("claude:sonnet", "sonnet", "hi", "SYSTEM")
    argv, stdin = claude.invoke("/bin/claude", prepared, tmp_path)
    assert stdin == "hi"
    assert "--tools" in argv and argv[argv.index("--tools") + 1] == ""
    assert "--safe-mode" in argv and "--no-session-persistence" in argv
    # REPLACES the vendor prompt: keeping it leaked the provider's cwd, OS and git branch to a
    # bare `pwd?`, with no tool involved.
    assert "--system-prompt-file" in argv and "--append-system-prompt-file" not in argv
    written = Path(argv[argv.index("--system-prompt-file") + 1])
    assert written.read_text(encoding="utf-8") == "SYSTEM"


def test_prepare_maps_the_thinking_budget_to_an_effort_level():
    """Regression: the request's `thinking` field was read nowhere (grep confirmed zero
    occurrences), so a caller who enabled extended thinking got an ordinary answer and was
    charged for the turn with no sign anything was ignored."""
    body = {"model": "claude:sonnet", "messages": [{"role": "user", "content": "hi"}]}

    # absent -> no --effort at all, so today's behaviour is unchanged
    assert cli_seat.prepare(body, "claude", wire="anthropic").effort == ""

    # {"type": "disabled"} -> same as absent
    disabled = {**body, "thinking": {"type": "disabled"}}
    assert cli_seat.prepare(disabled, "claude", wire="anthropic").effort == ""

    # two different budgets must produce two different levels, rising with the budget
    small = {**body, "thinking": {"type": "enabled", "budget_tokens": 2000}}
    large = {**body, "thinking": {"type": "enabled", "budget_tokens": 50000}}
    small_effort = cli_seat.prepare(small, "claude", wire="anthropic").effort
    large_effort = cli_seat.prepare(large, "claude", wire="anthropic").effort
    assert small_effort and large_effort
    assert small_effort != large_effort


def test_claude_invoke_passes_effort_only_when_the_request_asked_for_thinking(tmp_path: Path):
    """The plumbing must reach `claude.py`'s `invoke`, which builds the argv — and an OpenAI
    `/chat/completions` request (no `thinking` field) must produce byte-identical argv to today."""
    body = {"model": "claude:sonnet", "messages": [{"role": "user", "content": "hi"}]}

    no_thinking = cli_seat.prepare(body, "claude")  # OpenAI wire — never carries `thinking`
    argv, _ = claude.invoke("/bin/claude", no_thinking, tmp_path)
    assert "--effort" not in argv

    enabled = {**body, "thinking": {"type": "enabled", "budget_tokens": 50000}}
    prepared = cli_seat.prepare(enabled, "claude", wire="anthropic")
    assert prepared.effort
    argv, _ = claude.invoke("/bin/claude", prepared, tmp_path)
    assert "--effort" in argv and argv[argv.index("--effort") + 1] == prepared.effort


def test_prepare_reads_reasoning_effort_when_thinking_is_absent():
    """Most traffic never reaches the seat in Anthropic's own shape — an engine that does not
    speak Anthropic translates the request to the OpenAI wire first, and a plain OpenAI client
    sends `reasoning_effort`, never `thinking`. Regression: grep confirmed zero occurrences of
    `reasoning_effort` under shared/agent/, so this path discarded the caller's choice silently."""
    body = {"model": "claude:sonnet", "messages": [{"role": "user", "content": "hi"}]}

    # absent -> no effort at all, so a body carrying neither field is unaffected
    assert cli_seat.prepare(body, "claude").effort == ""

    # the OpenAI standard levels pass straight through 1:1 — not climbed to a higher rung either
    # CLI also accepts, since "high" names OpenAI's own vocabulary, not a budget to interpret
    for level in ("low", "medium", "high"):
        prepared = cli_seat.prepare({**body, "reasoning_effort": level}, "claude")
        assert prepared.effort == level

    # an unrecognised value reads as no effort, same as an absent field
    assert cli_seat.prepare({**body, "reasoning_effort": "extreme"}, "claude").effort == ""


def test_thinking_wins_over_reasoning_effort_when_both_are_present():
    """`thinking` carries an actual token budget; `reasoning_effort` carries one of three words.
    The richer signal must win whenever it resolves to something, and only a `thinking` that
    resolves to nothing (absent or disabled) should let `reasoning_effort` be read at all."""
    body = {"model": "claude:sonnet", "messages": [{"role": "user", "content": "hi"}]}

    both = {**body, "thinking": {"type": "enabled", "budget_tokens": 50000},
            "reasoning_effort": "low"}
    prepared = cli_seat.prepare(both, "claude", wire="anthropic")
    assert prepared.effort != "low"
    assert prepared.effort == cli_seat.prepare(
        {**body, "thinking": both["thinking"]}, "claude", wire="anthropic").effort

    # thinking present but disabled -> falls through to reasoning_effort
    disabled_both = {**body, "thinking": {"type": "disabled"}, "reasoning_effort": "high"}
    assert cli_seat.prepare(disabled_both, "claude", wire="anthropic").effort == "high"


def test_claude_decode_reads_usage_and_folds_in_cache_tokens():
    proc = SimpleNamespace(stdout=json.dumps({
        "result": "ok", "is_error": False, "total_cost_usd": 0.5, "duration_ms": 7, "num_turns": 1,
        "session_id": "s", "usage": {"input_tokens": 10, "cache_read_input_tokens": 5,
                                     "cache_creation_input_tokens": 2, "output_tokens": 3},
    }), stderr="", returncode=0)
    result = claude.decode(proc, Path("/tmp"))
    assert result.text == "ok" and result.input_tokens == 17 and result.output_tokens == 3
    assert result.cost_usd == 0.5


def test_claude_decode_surfaces_the_error_message():
    proc = SimpleNamespace(stdout=json.dumps({"is_error": True, "result": "Not logged in"}),
                           stderr="", returncode=1)
    with pytest.raises(cli_seat.SeatError, match="Not logged in"):
        claude.decode(proc, Path("/tmp"))


def test_claude_parses_the_usage_screen():
    snapshot = claude.parse_usage(
        "Current session: 7% used · resets Jul 29 at 4pm (Asia/Saigon)\n"
        "Current week (all models): 16% used · resets Aug 4 at 5am (Asia/Saigon)\n"
    )
    assert snapshot.session_pct == 7 and snapshot.week_pct == 16
    assert snapshot.session_reset == "Jul 29 at 4pm"
    assert claude.parse_usage("some other output") is None


def test_codex_invoke_locks_down_and_replaces_the_system_prompt(tmp_path: Path):
    """`model_instructions_file` replaces codex's own base prompt; the prompt itself goes on stdin."""
    prepared = cli_seat.PreparedRequest("codex-cli:gpt-5.5", "gpt-5.5", "hi", "SYSTEM")
    argv, stdin = codex.invoke("/bin/codex", prepared, tmp_path)
    assert stdin == "hi"
    assert any(a.startswith("model_instructions_file=") for a in argv)
    assert (tmp_path / "instructions.md").read_text() == "SYSTEM"
    assert argv[1] == "exec" and argv[-1] == "-"
    assert "--ephemeral" in argv                       # no saving, like claude
    assert argv[argv.index("-s") + 1] == "read-only"   # cannot write
    # `-a`/`--ask-for-approval` is documented as a global flag but `codex exec` rejects it.
    assert "-a" not in argv and "--ask-for-approval" not in argv
    assert 'approval_policy = "untrusted"' in codex.CONFIG_TOML


def test_codex_decode_reads_the_output_file_and_turn_usage(tmp_path: Path):
    (tmp_path / "last.txt").write_text("done", encoding="utf-8")
    proc = SimpleNamespace(stdout="\n".join([
        json.dumps({"type": "thread.started", "thread_id": "t1"}),
        "not json at all",
        json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 4, "cached_input_tokens": 1, "cache_write_input_tokens": 1,
            "output_tokens": 2, "reasoning_output_tokens": 3}}),
    ]), stderr="", returncode=0)
    result = codex.decode(proc, tmp_path)
    assert result.text == "done" and result.session_id == "t1"
    assert result.input_tokens == 6 and result.output_tokens == 5
    assert result.cost_usd == 0.0  # codex reports tokens but no dollar figure


def test_codex_decode_explains_an_empty_run(tmp_path: Path):
    proc = SimpleNamespace(
        stdout=json.dumps({"type": "turn.failed", "message": "boom"}), stderr="", returncode=1)
    with pytest.raises(cli_seat.SeatError, match="boom"):
        codex.decode(proc, tmp_path)


# the server actually boots

def test_every_seat_boots_its_server_and_reports_itself():
    """Boots the real app for each registered seat. The unit tests above never called
    `create_app`, which let a renamed SeatSpec field through to runtime."""
    from fastapi.testclient import TestClient

    from local.cli_seat_server import create_app

    for kind, spec in SEATS.items():
        app = create_app(spec=spec, binary="/fake/bin", options=cli_seat.SeatOptions())
        client = TestClient(app)

        health = client.get("/health").json()
        assert health["ok"] and health["kind"] == kind

        listed = [m["id"] for m in client.get("/v1/models").json()["data"]]
        assert listed == cli_seat.advertised_models(kind)

        # a model this seat does not serve is the CALLER's error, not a broken seat
        bad = client.post("/chat/completions", json={
            "model": "nope", "messages": [{"role": "user", "content": "hi"}]})
        assert bad.status_code == 400


# streaming

def test_claude_streams_text_deltas_and_ignores_other_events():
    delta = {"type": "stream_event", "event": {
        "type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}}}
    assert claude.parse_event(delta) == "hi"
    assert claude.parse_event({"type": "stream_event", "event": {"type": "message_start"}}) is None
    assert claude.parse_event({"type": "system"}) is None


def test_claude_turns_a_blocked_rate_limit_into_a_full_quota_reading():
    """The event rides along with every answer for free, so a blocked account is noticed without
    paying for a separate /usage probe."""
    allowed = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}}
    assert claude.parse_event(allowed) is None

    blocked = {"type": "rate_limit_event", "rate_limit_info": {
        "status": "rejected", "resetsAt": 1785315600}}
    snapshot = claude.parse_event(blocked)
    assert isinstance(snapshot, cli_seat.QuotaSnapshot)
    assert snapshot.session_pct == 100 and snapshot.session_reset  # a ceiling of any size refuses
    assert cli_seat.quota_refusal(snapshot, cli_seat.SeatOptions(session_limit=99)) is not None


def test_codex_streams_the_whole_message_once():
    """Codex emits no deltas, so a streaming consumer gets one chunk."""
    done = {"type": "item.completed", "item": {"type": "agent_message", "text": "all of it"}}
    assert codex.parse_event(done) == "all of it"
    assert codex.parse_event({"type": "turn.started"}) is None


def test_claude_decode_reads_the_result_line_out_of_a_jsonl_stream():
    """The streaming path reuses `decode` on the collected output, so both modes must build the
    same answer from the same function."""
    stream = "\n".join([
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({"type": "stream_event", "event": {"type": "message_start"}}),
        json.dumps({"type": "result", "result": "done", "is_error": False,
                    "total_cost_usd": 0.25, "usage": {"input_tokens": 3, "output_tokens": 1}}),
    ])
    result = claude.decode(SimpleNamespace(stdout=stream, stderr="", returncode=0), Path("/tmp"))
    assert result.text == "done" and result.cost_usd == 0.25 and result.input_tokens == 3


def test_both_seats_declare_streaming():
    for kind, spec in SEATS.items():
        assert cli_seat.can_stream(spec), f"{kind} should stream"


# isolated home

def test_a_seat_with_its_own_home_writes_its_config_there(tmp_path, monkeypatch):
    """The config IS the lockdown, so it is rewritten every start — a stale or hand-edited file
    would silently loosen it."""
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    spec = seat_for("codex-cli")

    env = cli_seat.ensure_home(spec)
    home = tmp_path / "seats" / "codex-cli"
    assert env["CODEX_HOME"] == str(home)

    config = (home / "config.toml").read_text()
    assert 'approval_policy = "untrusted"' in config   # cannot execute
    assert 'sandbox_mode = "read-only"' in config      # cannot write
    assert "[mcp_servers]" in config                   # no external tools
    assert "enabled = false" in config                 # no bundled skills

    (home / "config.toml").write_text("approval_policy = \"never\"\n")
    cli_seat.ensure_home(spec)
    assert 'approval_policy = "untrusted"' in (home / "config.toml").read_text()


def test_the_seat_home_is_not_the_operators_home(tmp_path, monkeypatch):
    """The operator's own hooks, skills and project trust must never load in a seat serving
    strangers — and their sign-in is never copied."""
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    env = cli_seat.ensure_home(seat_for("codex-cli"))
    assert env["CODEX_HOME"] != os.path.expanduser("~/.codex")


def test_the_codex_seat_runs_in_its_own_home(tmp_path, monkeypatch):
    """The operator's own config, skills, hooks and chat history must stay out of reach of a seat
    serving strangers."""
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    spec = seat_for("codex-cli")
    assert spec.home_env == "CODEX_HOME"
    assert cli_seat.ensure_home(spec)["CODEX_HOME"] == str(tmp_path / "seats" / "codex-cli")


def test_the_claude_seat_shares_the_operators_home_for_now():
    """A KNOWN GAP, not a design. CLAUDE_CONFIG_DIR is honoured and the isolated home works, but it
    needs its own interactive sign-in and driving that is the app's job. Flip `home_env` back to
    "CLAUDE_CONFIG_DIR" once the app can log a seat in; this test then becomes the codex one.

    What it costs meanwhile: `--safe-mode` still drops CLAUDE.md, skills, plugins, hooks and MCP,
    and `--tools ""` means the model cannot reach any of it — one layer thinner than codex, not
    open."""
    spec = seat_for("claude")
    assert spec.home_env == ""
    assert cli_seat.seat_home(spec) is None


def test_codex_no_longer_passes_ignore_user_config(tmp_path: Path):
    """Once the home is ours, ignoring config.toml would ignore OUR lockdown."""
    prepared = cli_seat.PreparedRequest("codex-cli:gpt-5.5", "gpt-5.5", "hi", "SYS")
    argv, _ = codex.invoke("/bin/codex", prepared, tmp_path)
    assert "--ignore-user-config" not in argv
    assert "--ignore-rules" in argv        # project .rules live outside CODEX_HOME
    assert argv[argv.index("-s") + 1] == "read-only"


# join lifecycle

def test_a_seat_never_hot_reloads():
    """A reload re-advertises models but starts no process. Hot-reloading a seat would advertise
    its models against a port with nothing listening — seen live when a second seat was joined."""
    from cli import remote_provider

    live = [{"engine_id": "remote", "reload_signal": "sighup", "engines": []}]
    record = {"engines": []}
    external = [{"endpoint_url": "http://127.0.0.1:9001", "models": ["x"]}]
    assert remote_provider._hot_reloadable(live, external, record) is True

    with_seat = external + [{"endpoint_url": "http://127.0.0.1:8098",
                             "api_kind": "codex-cli", "models": ["codex-cli:gpt-5.5"]}]
    assert remote_provider._hot_reloadable(live, with_seat, record) is False


def test_an_empty_system_prompt_falls_back_to_a_neutral_one():
    """The seats REPLACE the vendor prompt, so an empty replacement is not an option: codex
    refuses an empty instructions file, and any fallback to the vendor prompt re-opens the path
    leak. A caller sending no system message still gets a non-empty, non-leaking prompt."""
    assert cli_seat.build_system_prompt([{"role": "user", "content": "hi"}], None) == \
        cli_seat.DEFAULT_SYSTEM_PROMPT
    assert cli_seat.build_system_prompt([{"role": "system", "content": "be brief"}], None) == "be brief"


# quota reported upstream

def test_headroom_is_measured_against_the_operators_own_ceiling():
    """A seat capped at 50% is empty at 50% used, not at 100% — the ceiling is the point it stops
    serving, so that is what "how much is left" has to mean."""
    snapshot = cli_seat.QuotaSnapshot(session_pct=26, week_pct=19)
    assert cli_seat.quota_headroom_pct(snapshot, cli_seat.SeatOptions()) == 74.0
    assert cli_seat.quota_headroom_pct(snapshot, cli_seat.SeatOptions(session_limit=50)) == 48.0
    spent = cli_seat.QuotaSnapshot(session_pct=50, week_pct=19)
    assert cli_seat.quota_headroom_pct(spent, cli_seat.SeatOptions(session_limit=50)) == 0.0


def test_unknown_quota_reports_full_headroom():
    """Routing must not push a seat down for failing to measure itself."""
    assert cli_seat.quota_headroom_pct(None, cli_seat.SeatOptions()) == 100.0


def test_the_tighter_window_decides_headroom():
    """Whichever window runs out first is the one that stops the seat."""
    snapshot = cli_seat.QuotaSnapshot(session_pct=10, week_pct=90)
    assert cli_seat.quota_headroom_pct(snapshot, cli_seat.SeatOptions()) == 10.0


def test_only_cli_seat_specs_are_asked_for_quota():
    from remote import serve

    record = {"engines": [
        {"api_kind": "codex-cli", "seat": {"port": 8098}},
        {"api_kind": "openai", "endpoint_url": "https://api.openai.com/v1"},
        {"endpoint_url": "http://127.0.0.1:8081"},
    ]}
    assert serve._cli_seat_urls(record) == ["http://127.0.0.1:8098"]
    assert serve._cli_seat_urls({"engines": []}) == []


# streaming: both execution models must present ONE shape to the server

def _prepared(model="codex-cli:gpt-5.6-terra"):
    return cli_seat.prepare({"model": model, "messages": [{"role": "user", "content": "hi"}]},
                            model.split(":")[0])


def test_a_run_spec_streams_deltas_then_one_done_pair():
    """Regression: `_stream_via_run` returned a bare SeatResult while the invoke/decode path
    returned `(result, quota)`, so `answer_stream`'s unpack raised TypeError on every codex stream.
    The server caught only SeatError, so the answer arrived and then the connection was torn —
    the consumer saw `incomplete chunked read`, never a reason.
    """
    def run(binary, prepared, timeout, on_delta):
        on_delta("OK")
        return cli_seat.SeatResult(text="OK", output_tokens=1)

    spec = seat_for("codex-cli").__class__(**{**seat_for("codex-cli").__dict__, "run": run})
    events = list(cli_seat.answer_stream(spec, _prepared(), "codex", 30))
    assert events[0] == ("delta", "OK")
    kind, (completion, quota) = events[-1]
    assert kind == "done"
    assert completion["choices"][0]["message"]["content"] == "OK"
    assert quota is None


def test_both_execution_models_return_the_same_pair():
    """The contract lives in one place: whichever path ran, the caller unpacks `(result, quota)`."""
    def run(binary, prepared, timeout, on_delta):
        return cli_seat.SeatResult(text="done")

    spec = seat_for("codex-cli").__class__(**{**seat_for("codex-cli").__dict__, "run": run})
    stream = cli_seat.stream_seat(spec, _prepared(), "codex", 30)
    with pytest.raises(StopIteration) as stop:
        while True:
            next(stream)
    result, quota = stop.value.value
    assert isinstance(result, cli_seat.SeatResult) and quota is None


# tool calls: text back into tool_calls[]

def test_a_tool_call_becomes_an_openai_tool_call():
    """The shape the instruction asks for, read back into the shape the relay translates."""
    text = ('{"tool_calls": [{"type": "function", "function": '
            '{"name": "get_weather", "arguments": {"city": "Hanoi"}}}]}')
    prose, calls = cli_seat.parse_openai_tool_calls(text)
    assert prose == ""
    assert calls[0]["function"]["name"] == "get_weather"
    # A STRING, because the relay calls json.loads on it to build `tool_use.input`.
    assert json.loads(calls[0]["function"]["arguments"]) == {"city": "Hanoi"}
    assert calls[0]["type"] == "function" and calls[0]["id"].startswith("call_")


def test_arguments_are_accepted_as_an_object_or_an_encoded_string():
    """A model writes the object; the OpenAI wire carries it encoded. Both mean the same call."""
    for arguments in ('{"city": "Hanoi"}', '"{\\"city\\": \\"Hanoi\\"}"'):
        text = ('{"tool_calls": [{"function": {"name": "get_weather", "arguments": '
                + arguments + "}}]}")
        _, calls = cli_seat.parse_openai_tool_calls(text)
        assert json.loads(calls[0]["function"]["arguments"]) == {"city": "Hanoi"}


def test_a_nested_object_argument_is_not_truncated():
    """Why the parse scans with the JSON decoder: a brace counter or a `\\{.*?\\}` pattern cuts this
    at the first `}` and yields invalid JSON for every object-valued argument — the common case."""
    text = ('{"tool_calls": [{"function": {"name": "write", "arguments": '
            '{"file": {"path": "a.txt", "mode": 644}}}}]}')
    _, calls = cli_seat.parse_openai_tool_calls(text)
    assert json.loads(calls[0]["function"]["arguments"]) == {"file": {"path": "a.txt", "mode": 644}}


def test_prose_around_a_call_is_kept_and_several_calls_are_read():
    text = ('Let me check both.\n'
            '{"tool_calls": [{"function": {"name": "a", "arguments": {}}},'
            ' {"function": {"name": "b", "arguments": {"x": 1}}}]}\n'
            'done')
    prose, calls = cli_seat.parse_openai_tool_calls(text)
    assert [c["function"]["name"] for c in calls] == ["a", "b"]
    assert "Let me check both." in prose and "done" in prose
    assert "tool_calls" not in prose


def test_a_malformed_block_stays_in_the_prose():
    """Never silently dropped: a half-written call is a result worth seeing, and swallowing it
    turns a visible malformation into a model that mysteriously did nothing."""
    for broken in ('{"tool_calls": [{"function": {"name": "a", oops}}]}',
                   '{"tool_calls": [{"function": {"arguments": {}}}]}',
                   '{"tool_calls": "not a list"}'):
        prose, calls = cli_seat.parse_openai_tool_calls(broken)
        assert calls == []
        assert prose == broken


def test_an_answer_with_no_call_is_returned_byte_for_byte():
    text = "  Just talking. Braces { } and a < sign.  "
    assert cli_seat.parse_openai_tool_calls(text) == (text, [])


def test_tools_sent_means_parsed_and_no_tools_means_verbatim():
    """The one switch: `tools[]` in the request is what turns parsing on, so a client that brought
    its own convention in a system message still gets its text back untouched."""
    raw = ('{"tool_calls": [{"function": '
           '{"name": "get_weather", "arguments": {"city": "Hanoi"}}}]}')
    result = cli_seat.SeatResult(text=raw)
    body = {"model": "claude:sonnet", "messages": [{"role": "user", "content": "weather?"}]}
    tools = [{"type": "function", "function": {"name": "get_weather", "parameters": {}}}]

    with_tools = cli_seat.prepare({**body, "tools": tools}, "claude")
    assert with_tools.tool_protocol is cli_seat.OPENAI
    done = cli_seat.to_chat_completion(result, "claude", body["model"], with_tools.tool_protocol)
    assert done["choices"][0]["finish_reason"] == "tool_calls"
    assert done["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "get_weather"

    without = cli_seat.prepare(body, "claude")
    assert without.tool_protocol is None
    plain = cli_seat.to_chat_completion(result, "claude", body["model"], without.tool_protocol)
    assert plain["choices"][0]["message"]["content"] == raw
    assert "tool_calls" not in plain["choices"][0]["message"]
    assert plain["choices"][0]["finish_reason"] == "stop"


# tool calls: Anthropic's own shape, spoken beside OpenAI's

def test_an_anthropic_tool_use_is_read_back():
    """Same scan as the OpenAI parse — `_json_objects` is reused — differing only in which keys
    carry the call."""
    text = '{"type": "tool_use", "name": "Read", "input": {"file_path": "data.txt"}}'
    prose, calls = cli_seat.parse_anthropic_tool_uses(text)
    assert prose == ""
    assert calls[0]["name"] == "Read"
    assert calls[0]["input"] == {"file_path": "data.txt"}
    assert calls[0]["id"].startswith("toolu_")


def test_anthropic_input_is_accepted_as_an_object_or_an_encoded_string():
    """A model transcribing the template has no schema enforcement and may double-encode `input`
    the way OpenAI's `arguments` convention taught it to. Dropping that string to `{}` would call
    the tool with its arguments silently gone; decoding it mirrors `_one_call` on the OpenAI side."""
    for input_value in ('{"file_path": "a.txt"}', '"{\\"file_path\\": \\"a.txt\\"}"'):
        text = '{"type": "tool_use", "name": "Read", "input": ' + input_value + "}"
        _, calls = cli_seat.parse_anthropic_tool_uses(text)
        assert calls[0]["input"] == {"file_path": "a.txt"}


def test_an_anthropic_answer_with_no_call_is_returned_byte_for_byte():
    text = "  Just talking. Braces { } and a < sign.  "
    assert cli_seat.parse_anthropic_tool_uses(text) == (text, [])


def test_a_malformed_anthropic_block_stays_in_the_prose():
    broken = '{"type": "tool_use", "input": {}}'
    prose, calls = cli_seat.parse_anthropic_tool_uses(broken)
    assert calls == []
    assert prose == broken


def test_to_anthropic_message_sets_tool_use_and_stop_reason():
    raw = '{"type": "tool_use", "name": "Read", "input": {"file_path": "data.txt"}}'
    result = cli_seat.SeatResult(text=raw, input_tokens=10, output_tokens=4)
    message = cli_seat.to_anthropic_message(result, "claude", "claude:sonnet", cli_seat.ANTHROPIC)
    assert message["type"] == "message"
    assert message["stop_reason"] == "tool_use"
    assert [b["type"] for b in message["content"]] == ["tool_use"]
    assert message["usage"] == {"input_tokens": 10, "output_tokens": 4}


# the seat reads Anthropic requests and serves /messages (Task 11)

def test_an_anthropic_request_puts_the_top_level_system_in_the_system_prompt():
    """Anthropic sends `system` as a top-level field, not a message with role=system. Read as a
    message list it vanishes, and the model loses the caller's whole instruction set."""
    body = {"model": "claude:sonnet",
            "system": [{"type": "text", "text": "You are Claude Code."}],
            "messages": [{"role": "user", "content": "hi"}]}
    prepared = cli_seat.prepare(body, "claude", wire="anthropic")
    assert prepared.system_prompt == "You are Claude Code."
    assert prepared.prompt == "hi"


def test_an_anthropic_tool_result_block_is_replayed_as_text():
    body = {"model": "claude:sonnet", "messages": [
        {"role": "user", "content": "read it"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu1", "name": "Read", "input": {"file_path": "a.txt"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu1", "content": "SECRET"}]},
    ]}
    prompt = cli_seat.prepare(body, "claude", wire="anthropic").prompt
    assert '"type": "tool_use"' in prompt
    assert "Tool result: SECRET" in prompt


def test_the_claude_catalog_row_declares_messages():
    assert "messages" in api_catalog.WHITELISTS["claude"].endpoints


# the seat refuses an Anthropic server tool (Task 12)

def test_an_anthropic_server_tool_is_refused_rather_than_passed_through():
    """`web_search`, `bash` and `code_execution` are run by Anthropic, not by the caller. The
    seat must refuse them with 400 — letting one through produces a tool call nobody executes,
    which reaches the user as a hang."""
    body = {"model": "claude:sonnet",
            "messages": [{"role": "user", "content": "search"}],
            "tools": [{"type": "web_search_20250305", "name": "web_search"}]}
    with pytest.raises(cli_seat.SeatBadRequest) as exc:
        cli_seat.prepare(body, "claude", wire="anthropic")
    assert "web_search" in str(exc.value)


def test_an_ordinary_function_tool_is_accepted():
    body = {"model": "claude:sonnet",
            "messages": [{"role": "user", "content": "read"}],
            "tools": [{"name": "Read", "input_schema": {"type": "object"}}]}
    assert cli_seat.prepare(body, "claude", wire="anthropic").tool_protocol is cli_seat.ANTHROPIC


# SSE framing: event: + data: pairs (found live, not by a test)

def test_the_anthropic_stream_pairs_a_named_event_with_each_data_line():
    """Found by running the real seat: it emitted bare `data:` frames with no `event:` line, so
    every frame dispatched under the SSE default event name `message` and a client switching on
    the event name could not tell `content_block_delta` from `message_stop` apart. Anthropic's own
    wire pairs a named `event:` line with its `data:` line before every blank-line boundary."""
    from fastapi.testclient import TestClient

    from local.cli_seat_server import create_app
    from shared.agent.seats import seat_for

    def run(binary, prepared, timeout, on_delta):
        on_delta("hi")
        return cli_seat.SeatResult(text="hi", output_tokens=1)

    base = seat_for("claude")
    spec = base.__class__(**{**base.__dict__, "run": run})
    app = create_app(spec=spec, binary="/fake/bin", options=cli_seat.SeatOptions())
    client = TestClient(app)

    resp = client.post("/messages", json={
        "model": "claude:sonnet", "stream": True,
        "messages": [{"role": "user", "content": "hi"}]})
    body = resp.text

    assert 'event: message_start\ndata: {"type": "message_start"' in body
    assert 'event: content_block_delta\ndata: {"type": "content_block_delta"' in body
    assert 'event: message_stop\ndata: {"type": "message_stop"' in body
    # every frame is an event: line immediately followed by its data: line, never a bare data: line
    pairs = [ln for ln in body.split("\n\n") if ln.strip()]
    for pair in pairs:
        lines = pair.split("\n")
        assert lines[0].startswith("event: "), f"frame missing event: line: {pair!r}"
        assert lines[1].startswith("data: "), f"event: not paired with data: {pair!r}"


def test_the_openai_stream_stays_data_only():
    """The OpenAI `/chat/completions` stream is `data:`-only by its own convention and must stay
    byte-identical — this pins that the Anthropic `event:` fix did not leak across wires."""
    from fastapi.testclient import TestClient

    from local.cli_seat_server import create_app
    from shared.agent.seats import seat_for

    def run(binary, prepared, timeout, on_delta):
        on_delta("hi")
        return cli_seat.SeatResult(text="hi", output_tokens=1)

    base = seat_for("codex-cli")
    spec = base.__class__(**{**base.__dict__, "run": run})
    app = create_app(spec=spec, binary="/fake/bin", options=cli_seat.SeatOptions())
    client = TestClient(app)

    resp = client.post("/chat/completions", json={
        "model": "codex-cli:gpt-5.6-terra", "stream": True,
        "messages": [{"role": "user", "content": "hi"}]})
    body = resp.text

    assert "event:" not in body
    assert "data: [DONE]" in body
