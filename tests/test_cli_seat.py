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


def test_multi_turn_renders_tool_calls_in_hermes_spelling():
    prompt = cli_seat.build_prompt([
        {"role": "user", "content": "weather?"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"function": {"name": "get_weather", "arguments": '{"loc":"Hanoi"}'}}]},
        {"role": "tool", "content": '{"temp":31}'},
    ])
    assert "<tool_call>" in prompt and '"name": "get_weather"' in prompt
    assert "<tool_response>" in prompt


def test_hermes_block_injected_only_for_native_tools():
    messages = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}]
    assert cli_seat.build_system_prompt(messages, None) == "be brief"
    with_tools = cli_seat.build_system_prompt(messages, [{"type": "function"}])
    assert with_tools.startswith("be brief")
    assert "<tool_call>" in with_tools


def test_images_are_named_not_silently_dropped():
    prompt = cli_seat.build_prompt([{"role": "user", "content": [
        {"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "x"}},
    ]}])
    assert "image omitted" in prompt


def test_prepare_rejects_an_unserved_model():
    with pytest.raises(cli_seat.SeatBadRequest):
        cli_seat.prepare({"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}, "claude")


# tool-call parsing

def test_tool_calls_are_lifted_out_of_the_prose():
    text = 'Checking.\n<tool_call>\n{"arguments": {"a": 1}, "name": "f"}\n</tool_call>'
    content, calls = cli_seat.parse_tool_calls(text)
    assert content == "Checking."
    assert calls[0]["function"]["name"] == "f"
    assert json.loads(calls[0]["function"]["arguments"]) == {"a": 1}


def test_a_malformed_call_stays_in_the_prose():
    """On a tool-calling benchmark a broken call is a result worth seeing; deleting it would
    report the run as cleaner than it was."""
    text = "<tool_call>\nnot json\n</tool_call>"
    content, calls = cli_seat.parse_tool_calls(text)
    assert calls == []
    assert "not json" in content


def test_finish_reason_tracks_whether_a_call_was_emitted():
    with_call = cli_seat.to_chat_completion(
        cli_seat.SeatResult(text='<tool_call>{"name":"f","arguments":{}}</tool_call>'),
        "claude", "claude:sonnet")
    assert with_call["choices"][0]["finish_reason"] == "tool_calls"
    plain = cli_seat.to_chat_completion(cli_seat.SeatResult(text="hello"), "claude", "claude:sonnet")
    assert plain["choices"][0]["finish_reason"] == "stop"
    assert plain["grid_cli_seat"]["kind"] == "claude"


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


def test_every_seat_gets_its_own_home(tmp_path, monkeypatch):
    """Both CLIs support one, and both must use it: the operator's own config, skills, hooks and
    chat history stay out of reach of a seat serving strangers."""
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    for kind, env_var in (("claude", "CLAUDE_CONFIG_DIR"), ("codex-cli", "CODEX_HOME")):
        spec = seat_for(kind)
        assert spec.home_env == env_var
        env = cli_seat.ensure_home(spec)
        assert env[env_var] == str(tmp_path / "seats" / kind)


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
