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
        api_catalog.advertised_name("claude", e) for e in api_catalog.entries_for("claude")
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
    # The turn travels as verbatim JSON, not flattened prose: a `tool_use` stays a tool_use, so the
    # model can see the shape it is expected to answer in.
    assert '"hi"' in cli_seat.prepare(body, "claude").prompt
    with_tools = cli_seat.prepare({**body, "tools": tools}, "claude")
    assert with_tools.system_prompt == "be brief", "the tool block must not reach the system prompt"
    assert "tool_calls" in with_tools.prompt
    assert with_tools.prompt.index("hi") < with_tools.prompt.index("tool_calls")


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
    prepared = cli_seat.PreparedRequest("claude:sonnet", "sonnet", "hi", "SYSTEM",
                                        messages=({"role": "user", "content": "hi"},))
    argv, stdin = claude.invoke("/bin/claude", prepared, tmp_path)
    # NOT `--input-format stream-json`: it reads each `user` line as a live turn, so a replayed
    # transcript re-answers every past question and claude's own answers are spliced into the
    # history between them. Measured — three lines in, two `result` envelopes out, the first
    # re-answering the opening message. One prompt, one turn.
    assert "--input-format" not in argv
    assert stdin == prepared.prompt
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


def test_adaptive_thinking_gets_a_middle_effort_not_silence():
    """Regression: captured from a live, unmodified Claude Code run — no env overrides at all —
    the DEFAULT request shape is `{"type": "adaptive", "display": "omitted"}`, not `enabled` or
    `disabled`. `_effort_for_thinking` knew only those two and treated `adaptive` exactly like
    `disabled`: no `--effort` flag. That silently downgraded every ordinary Claude Code turn —
    thinking on, turn billed, `claude` never told to think — with no error and no warning."""
    body = {"model": "claude:sonnet", "messages": [{"role": "user", "content": "hi"}],
            "thinking": {"type": "adaptive", "display": "omitted"}}
    assert cli_seat.prepare(body, "claude", wire="anthropic").effort == "medium"


def test_an_unrecognised_thinking_type_also_gets_a_middle_effort():
    """The same class of gap `adaptive` was: a `type` value none of us has seen yet must not fall
    through to "no effort" the way `adaptive` used to. The client sent a non-empty `type` that is
    not the explicit `"disabled"` off-switch, so it asked for something — silence is the failure
    mode this whole function exists to close."""
    body = {"model": "claude:sonnet", "messages": [{"role": "user", "content": "hi"}],
            "thinking": {"type": "some_future_mode"}}
    assert cli_seat.prepare(body, "claude", wire="anthropic").effort == "medium"


def test_enabled_without_a_budget_also_gets_a_middle_effort():
    """`{"type": "enabled"}` with no `budget_tokens` is still an enabled state, just one carrying
    no number to grade — the same situation `adaptive` is in. It must resolve the same way
    `adaptive` does rather than being the one enabled shape left silent."""
    body = {"model": "claude:sonnet", "messages": [{"role": "user", "content": "hi"}],
            "thinking": {"type": "enabled"}}
    assert cli_seat.prepare(body, "claude", wire="anthropic").effort == "medium"


def test_enabled_with_a_budget_still_grades_by_size():
    """The one case that DOES carry a number must keep climbing the ladder with it, not collapse
    to the same flat "medium" the budget-less shapes above get."""
    body = {"model": "claude:sonnet", "messages": [{"role": "user", "content": "hi"}]}
    small = cli_seat.prepare(
        {**body, "thinking": {"type": "enabled", "budget_tokens": 2000}}, "claude",
        wire="anthropic")
    large = cli_seat.prepare(
        {**body, "thinking": {"type": "enabled", "budget_tokens": 50000}}, "claude",
        wire="anthropic")
    assert small.effort == "low"
    assert large.effort == "xhigh"


def test_disabled_thinking_still_passes_no_effort():
    """The one explicit off-switch must stay off: `disabled` is the caller actively saying "do not
    think", not "gave no number" — it must not be swept into the new middle-effort default."""
    body = {"model": "claude:sonnet", "messages": [{"role": "user", "content": "hi"}],
            "thinking": {"type": "disabled"}}
    assert cli_seat.prepare(body, "claude", wire="anthropic").effort == ""


def test_absent_thinking_still_passes_no_effort():
    """A request naming no `thinking` field at all must remain today's baseline: no signal was
    sent, so none should be invented."""
    body = {"model": "claude:sonnet", "messages": [{"role": "user", "content": "hi"}]}
    assert cli_seat.prepare(body, "claude", wire="anthropic").effort == ""


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


# `--output-format json` is not one shape. Claude Code 2.1.220 prints a JSON ARRAY of events on a
# single line, where older builds printed the bare result object. The array parses fine, so the
# JSONL fallback below never runs and the answer — sitting right there in the array — was dropped:
# every non-streaming request 502'd with "no result envelope".
def _array_stdout(*events):
    return json.dumps(list(events))


def test_claude_decode_reads_the_result_out_of_a_json_array():
    raw = _array_stdout(
        {"type": "system", "subtype": "init", "cwd": "/tmp/grid-claude-seat-x"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}},
        {"type": "rate_limit_event", "rate_limit": {"status": "allowed"}},
        {"type": "result", "result": "ok", "is_error": False, "total_cost_usd": 0.5,
         "duration_ms": 7, "num_turns": 1, "session_id": "s",
         "usage": {"input_tokens": 10, "cache_read_input_tokens": 5,
                   "cache_creation_input_tokens": 2, "output_tokens": 3}},
    )
    result = claude.decode(SimpleNamespace(stdout=raw, stderr="", returncode=0), Path("/tmp"))
    assert result.text == "ok"
    assert result.input_tokens == 17 and result.output_tokens == 3
    assert result.cost_usd == 0.5 and result.session_id == "s"


def test_claude_decode_ignores_non_result_elements_of_an_array():
    """`system`/`assistant`/`rate_limit_event` carry no answer — only the `result` element does, and
    picking any other one would return a fragment as if it were the reply."""
    raw = _array_stdout(
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "partial"}]}},
        {"type": "result", "result": "final", "is_error": False},
    )
    result = claude.decode(SimpleNamespace(stdout=raw, stderr="", returncode=0), Path("/tmp"))
    assert result.text == "final"


def test_claude_decode_surfaces_an_error_carried_by_an_array_result():
    """The array shape must not become a hole the `is_error` check falls through: a failed turn has
    to raise here, or the seat would answer 200 with the error text as the model's reply."""
    raw = _array_stdout(
        {"type": "system", "subtype": "init"},
        {"type": "result", "is_error": True, "result": "Not logged in"},
    )
    with pytest.raises(cli_seat.SeatError) as caught:
        claude.decode(SimpleNamespace(stdout=raw, stderr="", returncode=0), Path("/tmp"))
    # Not just "it raised": the undecoded path ALSO raises, and its message quotes the raw stdout —
    # which contains "Not logged in" verbatim. Pin the reason, or this passes without the fix.
    assert "Not logged in" == str(caught.value)


def test_claude_decode_still_refuses_an_array_with_no_result():
    """An array that never carries a `result` is a genuine failure — it must keep raising rather
    than silently decoding to an empty answer."""
    raw = _array_stdout({"type": "system", "subtype": "init"})
    with pytest.raises(cli_seat.SeatError, match="no result envelope"):
        claude.decode(SimpleNamespace(stdout=raw, stderr="", returncode=0), Path("/tmp"))


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
    assert '"hi"' in prepared.prompt


def test_an_anthropic_tool_result_block_is_replayed_as_text():
    body = {"model": "claude:sonnet", "messages": [
        {"role": "user", "content": "read it"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu1", "name": "Read", "input": {"file_path": "a.txt"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu1", "content": "SECRET"}]},
    ]}
    prompt = cli_seat.prepare(body, "claude", wire="anthropic").prompt
    # Both keep their own shape — flattening turned them into the words "Tool result:" and the
    # model had to guess the format back.
    assert '"type": "tool_use"' in prompt
    assert '"type": "tool_result"' in prompt
    assert "SECRET" in prompt


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


# the seat serves /responses (OpenAI Responses API dialect)

def test_the_codex_cli_catalog_row_declares_responses():
    assert "responses" in api_catalog.WHITELISTS["codex-cli"].endpoints
    assert "chat/completions" in api_catalog.WHITELISTS["codex-cli"].endpoints


def test_a_responses_request_reads_string_input_as_a_user_message():
    body = {"model": "codex-cli:gpt-5.5", "input": "hello"}
    prepared = cli_seat.prepare(body, "codex-cli", wire="responses")
    assert json.loads(prepared.prompt.split("\n\n", 1)[1]) == [
        {"role": "user", "content": "hello"}]


def test_a_responses_request_reads_message_array_input():
    body = {"model": "codex-cli:gpt-5.5", "input": [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "third"},
    ]}
    # Verbatim JSON, not "User: …" prose — roles stay roles, so nothing has to be parsed back out.
    prepared = cli_seat.prepare(body, "codex-cli", wire="responses")
    assert json.loads(prepared.prompt.split("\n\n", 1)[1]) == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "third"}]


def test_a_responses_request_reads_instructions_as_the_system_prompt():
    body = {"model": "codex-cli:gpt-5.5", "instructions": "Be brief.",
            "input": [{"role": "user", "content": "hi"}]}
    prepared = cli_seat.prepare(body, "codex-cli", wire="responses")
    assert prepared.system_prompt == "Be brief."


def test_a_responses_request_without_instructions_falls_back_to_system_role_items():
    body = {"model": "codex-cli:gpt-5.5", "input": [
        {"role": "system", "content": "You are a helper."},
        {"role": "user", "content": "hi"},
    ]}
    prepared = cli_seat.prepare(body, "codex-cli", wire="responses")
    assert prepared.system_prompt == "You are a helper."


def test_responses_input_text_content_parts_are_normalised():
    """The Responses dialect spells text parts ``input_text``/``output_text``; the shared flattener
    reads ``text``. The normaliser rewrites just those type tags so no dialect branch leaks in."""
    body = {"model": "codex-cli:gpt-5.5", "input": [
        {"role": "user", "content": [{"type": "input_text", "text": "a plain question"}]},
    ]}
    prepared = cli_seat.prepare(body, "codex-cli", wire="responses")
    turns = json.loads(prepared.prompt.split("\n\n", 1)[1])
    assert turns == [{"role": "user", "content": [{"type": "text", "text": "a plain question"}]}]


def test_a_responses_function_call_output_item_is_replayed_as_a_tool_result():
    body = {"model": "codex-cli:gpt-5.5", "input": [
        {"role": "user", "content": "what is the weather?"},
        {"type": "function_call", "call_id": "call_1", "name": "get_weather",
         "arguments": '{"city": "Hanoi"}'},
        {"type": "function_call_output", "call_id": "call_1", "output": "Sunny, 32C"},
        {"role": "user", "content": "thanks"},
    ]}
    prompt = cli_seat.prepare(body, "codex-cli", wire="responses").prompt
    # The call and its result keep their own shapes; flattening turned both into sentences.
    assert '"name": "get_weather"' in prompt
    assert '"role": "tool"' in prompt and "Sunny, 32C" in prompt
    assert '"thanks"' in prompt


def test_to_response_builds_a_responses_object():
    result = cli_seat.SeatResult(text="hello world", input_tokens=3, output_tokens=2)
    resp = cli_seat.to_response(result, "codex-cli", "codex-cli:gpt-5.5")
    assert resp["object"] == "response"
    assert resp["status"] == "completed"
    assert resp["model"] == "codex-cli:gpt-5.5"
    msg = resp["output"][0]
    assert msg["type"] == "message" and msg["role"] == "assistant"
    assert msg["content"][0]["type"] == "output_text"
    assert msg["content"][0]["text"] == "hello world"
    assert resp["usage"] == {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}


def test_to_response_maps_tool_calls_to_function_call_items():
    text = ('{"tool_calls": [{"type": "function", "function": '
            '{"name": "get_weather", "arguments": {"city": "Hanoi"}}}]}')
    result = cli_seat.SeatResult(text=text, input_tokens=1, output_tokens=1)
    resp = cli_seat.to_response(result, "codex-cli", "codex-cli:gpt-5.5", cli_seat.OPENAI)
    items = resp["output"]
    # No message item: the answer WAS the call. One used to be emitted with `content: []`, so a
    # client reading `output[]` in order met an assistant message saying nothing before the call.
    assert [i["type"] for i in items] == ["function_call"]
    fc = items[0]
    assert fc["type"] == "function_call"
    assert fc["name"] == "get_weather"
    assert json.loads(fc["arguments"]) == {"city": "Hanoi"}
    assert fc["call_id"].startswith("call_")


def test_the_responses_stream_uses_event_data_pairs():
    """The Responses SSE pairs ``event:`` + ``data:`` (like Anthropic, unlike OpenAI chat's
    bare ``data:``) and emits the canonical lifecycle: created → delta(s) → completed."""
    from fastapi.testclient import TestClient

    from local.cli_seat_server import create_app
    from shared.agent.seats import seat_for

    def run(binary, prepared, timeout, on_delta):
        on_delta("hi there")
        return cli_seat.SeatResult(text="hi there", output_tokens=2)

    base = seat_for("codex-cli")
    spec = base.__class__(**{**base.__dict__, "run": run})
    app = create_app(spec=spec, binary="/fake/bin", options=cli_seat.SeatOptions())
    client = TestClient(app)

    resp = client.post("/responses", json={
        "model": "codex-cli:gpt-5.6-terra", "stream": True, "input": "hi"})
    body = resp.text

    assert 'event: response.created\n' in body
    assert 'event: response.output_text.delta\n' in body
    assert 'event: response.completed\n' in body
    # every frame is an event: line immediately followed by its data: line
    for pair in body.split("\n\n"):
        if not pair.strip():
            continue
        lines = pair.split("\n")
        assert lines[0].startswith("event: "), f"frame missing event: line: {pair!r}"
        assert lines[1].startswith("data: "), f"event not paired with data: {pair!r}"


def test_the_responses_non_stream_endpoint_returns_a_json_object():
    from fastapi.testclient import TestClient

    from local.cli_seat_server import create_app
    from shared.agent.seats import seat_for

    def run(binary, prepared, timeout, on_delta):
        return cli_seat.SeatResult(text="plain answer", input_tokens=1, output_tokens=2)

    base = seat_for("codex-cli")
    spec = base.__class__(**{**base.__dict__, "run": run})
    app = create_app(spec=spec, binary="/fake/bin", options=cli_seat.SeatOptions())
    client = TestClient(app)

    resp = client.post("/responses", json={
        "model": "codex-cli:gpt-5.6-terra", "input": "hello"})
    obj = resp.json()
    assert obj["object"] == "response"
    assert obj["output"][0]["content"][0]["text"] == "plain answer"


def test_responses_reasoning_effort_is_read_from_the_nested_field():
    """The Responses API carries effort under ``reasoning.effort``, not the flat
    ``reasoning_effort`` the chat wire uses."""
    body = {"model": "codex-cli:gpt-5.5", "input": "hi",
            "reasoning": {"effort": "high"}}
    prepared = cli_seat.prepare(body, "codex-cli", wire="responses")
    assert prepared.effort == "high"




# Streaming with tools attached.
#
# `buffered = bool(prepared.tool_protocol)` dropped EVERY delta whenever the request carried tools,
# so the whole answer landed in one burst at the end. Claude Code always sends its tool roster, so
# every one of its turns was silent — measured on the prod grid as a gap-to-second-chunk equal to
# the entire request (344s of nothing on an opus turn). The guard itself is real: the parse decides
# which bytes were the call, so a delta emitted early once let a client print raw tool JSON as prose
# AND run the tool. The rule that satisfies both: stream up to the first `{`, hold from there.

_ANTHROPIC_TOOL = {"name": "get_weather", "description": "Get weather",
                   "input_schema": {"type": "object", "properties": {"loc": {"type": "string"}}}}
_OPENAI_TOOL = {"type": "function", "function": {"name": "get_weather", "description": "Get weather",
                "parameters": {"type": "object", "properties": {"loc": {"type": "string"}}}}}
_TOOL_JSON = '{"type": "tool_use", "name": "get_weather", "input": {"loc": "Hanoi"}}'
_OPENAI_CALL_JSON = ('{"tool_calls": [{"type": "function", "function": '
                     '{"name": "get_weather", "arguments": {"loc": "Hanoi"}}}]}')


def _seat_app(pieces):
    """An app whose seat emits `pieces` as separate deltas, then returns their concatenation."""
    from local.cli_seat_server import create_app
    from shared.agent.seats import seat_for

    def run(binary, prepared, timeout, on_delta):
        # `run_seat` passes on_delta=None on the non-streaming path — the same spec has to serve both.
        for piece in pieces:
            if on_delta is not None:
                on_delta(piece)
        return cli_seat.SeatResult(text="".join(pieces), output_tokens=len(pieces))

    base = seat_for("claude")
    spec = base.__class__(**{**base.__dict__, "run": run})
    return create_app(spec=spec, binary="/fake/bin", options=cli_seat.SeatOptions())


def _sse_frames(body):
    """Every `data:` payload in an SSE body, decoded."""
    out = []
    for line in body.splitlines():
        if line.startswith("data: ") and line[6:].strip() != "[DONE]":
            try:
                out.append(json.loads(line[6:]))
            except ValueError:
                pass
    return out


def _streamed_text(body):
    return "".join(f["delta"]["text"] for f in _sse_frames(body)
                   if f.get("type") == "content_block_delta"
                   and (f.get("delta") or {}).get("type") == "text_delta")


def _post_messages(app, body):
    from fastapi.testclient import TestClient
    return TestClient(app).post("/messages", json=body).text


def test_a_prose_answer_streams_even_when_the_request_carries_tools():
    """The regression this whole change is about: a turn that calls no tool must not be held back
    just because tools were offered."""
    pieces = ["The sea ", "is wide ", "and deep."]
    body = _post_messages(_seat_app(pieces), {
        "model": "claude:sonnet", "stream": True, "tools": [_ANTHROPIC_TOOL],
        "messages": [{"role": "user", "content": "describe the sea"}]})

    deltas = [f for f in _sse_frames(body) if f.get("type") == "content_block_delta"]
    assert len(deltas) > 1, "the answer arrived in one burst — still buffered"
    assert _streamed_text(body) == "".join(pieces)


def test_a_tool_call_is_never_streamed_as_prose():
    """The property the buffering existed to protect. The model was told to reply with the JSON
    object and nothing else, so this streams no text at all — and the raw call must never reach the
    client as text, or it prints the JSON and runs the tool."""
    body = _post_messages(_seat_app([_TOOL_JSON]), {
        "model": "claude:sonnet", "stream": True, "tools": [_ANTHROPIC_TOOL],
        "messages": [{"role": "user", "content": "weather in Hanoi?"}]})

    assert _streamed_text(body) == ""
    assert "get_weather" not in _streamed_text(body)
    kinds = [(f.get("content_block") or {}).get("type") for f in _sse_frames(body)
             if f.get("type") == "content_block_start"]
    assert "tool_use" in kinds, "the call must still be delivered as a tool_use block"


def test_prose_before_a_call_streams_but_the_call_does_not():
    pieces = ["Let me check. ", _TOOL_JSON]
    body = _post_messages(_seat_app(pieces), {
        "model": "claude:sonnet", "stream": True, "tools": [_ANTHROPIC_TOOL],
        "messages": [{"role": "user", "content": "weather in Hanoi?"}]})

    streamed = _streamed_text(body)
    assert "Let me check." in streamed
    assert "tool_use" not in streamed and "get_weather" not in streamed
    kinds = [(f.get("content_block") or {}).get("type") for f in _sse_frames(body)
             if f.get("type") == "content_block_start"]
    assert "tool_use" in kinds


def test_the_streamed_text_equals_what_the_non_streaming_answer_returns():
    """The client must end up with exactly the same bytes either way — the streamed prefix plus
    whatever the final frames add, never a doubled or a missing span."""
    from fastapi.testclient import TestClient

    pieces = ["The sea ", "is wide."]
    request = {"model": "claude:sonnet", "tools": [_ANTHROPIC_TOOL],
               "messages": [{"role": "user", "content": "describe the sea"}]}

    streamed = _streamed_text(_post_messages(_seat_app(pieces), {**request, "stream": True}))
    whole = TestClient(_seat_app(pieces)).post("/messages", json=request).json()
    assert streamed == "".join(b.get("text", "") for b in whole["content"] if b.get("type") == "text")


def test_the_openai_stream_shares_the_rule():
    """One rule, both wires — `/chat/completions` buffered on the same flag and must not drift."""
    from fastapi.testclient import TestClient
    from local.cli_seat_server import create_app
    from shared.agent.seats import seat_for

    def app_for(pieces):
        def run(binary, prepared, timeout, on_delta):
            for piece in pieces:
                on_delta(piece)
            return cli_seat.SeatResult(text="".join(pieces), output_tokens=1)
        base = seat_for("codex-cli")
        spec = base.__class__(**{**base.__dict__, "run": run})
        return create_app(spec=spec, binary="/fake/bin", options=cli_seat.SeatOptions())

    def post(pieces):
        return TestClient(app_for(pieces)).post("/chat/completions", json={
            "model": "codex-cli:gpt-5.6-terra", "stream": True, "tools": [_OPENAI_TOOL],
            "messages": [{"role": "user", "content": "hi"}]}).text

    def content(body):
        return "".join((f.get("choices") or [{}])[0].get("delta", {}).get("content") or ""
                       for f in _sse_frames(body))

    prose = post(["The sea ", "is wide."])
    prose_deltas = [f for f in _sse_frames(prose)
                    if ((f.get("choices") or [{}])[0].get("delta") or {}).get("content")]
    assert len(prose_deltas) > 1, "prose still arrives in one burst on the OpenAI wire"
    assert content(prose) == "The sea is wide."

    call = post([_OPENAI_CALL_JSON])
    assert content(call) == "", "the raw call leaked into the stream as prose"
    assert any(((f.get("choices") or [{}])[0].get("delta") or {}).get("tool_calls") for f in _sse_frames(call))


def test_the_responses_stream_shares_the_rule():
    """The third wire, added while this fix was in flight, carried the same
    `buffered = bool(prepared.tool_protocol)` — so it must land under the same rule or it silently
    keeps the frozen stream for every codex-cli turn that offers tools."""
    from fastapi.testclient import TestClient
    from local.cli_seat_server import create_app
    from shared.agent.seats import seat_for

    def app_for(pieces):
        def run(binary, prepared, timeout, on_delta):
            for piece in pieces:
                if on_delta is not None:
                    on_delta(piece)
            return cli_seat.SeatResult(text="".join(pieces), output_tokens=len(pieces))
        base = seat_for("codex-cli")
        spec = base.__class__(**{**base.__dict__, "run": run})
        return create_app(spec=spec, binary="/fake/bin", options=cli_seat.SeatOptions())

    def deltas(pieces):
        body = TestClient(app_for(pieces)).post("/responses", json={
            "model": "codex-cli:gpt-5.6-terra", "stream": True, "tools": [_OPENAI_TOOL],
            "input": "hi"}).text
        return [f["delta"] for f in _sse_frames(body)
                if f.get("type") == "response.output_text.delta"]

    prose = deltas(["The sea ", "is wide."])
    assert len(prose) > 1, "prose still arrives in one burst on the responses wire"
    assert "".join(prose) == "The sea is wide."

    call = deltas([_OPENAI_CALL_JSON])
    assert "".join(call) == "", "the raw call leaked into the stream as prose"


def test_flatten_content_skips_tool_use_not_omitted():
    content = [
        {"type": "text", "text": "let me read that"},
        {"type": "tool_use", "id": "tu1", "name": "read_file", "input": {"path": "foo.txt"}},
    ]
    result = cli_seat._flatten_content(content)
    assert "omitted" not in result
    assert "let me read that" in result

def test_flatten_content_skips_tool_result_not_omitted():
    content = [{"type": "tool_result", "tool_use_id": "tu1", "content": "file contents"}]
    result = cli_seat._flatten_content(content)
    assert "omitted" not in result

def test_prepare_carries_raw_messages_and_tool_text():
    body = {
        "model": "claude:sonnet",
        "messages": [{"role": "user", "content": "hello"}],
        "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {}}}],
    }
    prepared = cli_seat.prepare(body, "claude")
    assert len(prepared.messages) == 1
    assert prepared.messages[0]["content"] == "hello"
    assert prepared.tool_text

def test_prepare_tool_text_empty_without_tools():
    body = {"model": "claude:sonnet",
            "messages": [{"role": "user", "content": "hello"}]}
    prepared = cli_seat.prepare(body, "claude")
    assert prepared.tool_text == ""

def test_to_stream_json_converts_openai_messages():
    from shared.agent.seats.claude import to_stream_json
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path": "foo.txt"}'}}
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "file contents"},
        {"role": "user", "content": "thanks"},
    ]
    objs = [json.loads(line) for line in to_stream_json(messages).splitlines()]
    assert len(objs) == 4
    assert objs[0]["message"]["content"] == "hello"
    tu = next(b for b in objs[1]["message"]["content"] if b["type"] == "tool_use")
    assert tu["name"] == "read_file" and tu["input"] == {"path": "foo.txt"}
    assert any(b["type"] == "tool_result" for b in objs[2]["message"]["content"])

def test_to_stream_json_appends_tool_instructions():
    from shared.agent.seats.claude import to_stream_json
    lines = to_stream_json([{"role": "user", "content": "hi"}], tools_text="TOOLS HERE")
    content = json.loads(lines.splitlines()[-1])["message"]["content"]
    assert isinstance(content, list)
    assert any("TOOLS HERE" in b.get("text", "") for b in content)

def test_claude_invoke_sends_one_prompt_not_stream_json(tmp_path):
    prepared = cli_seat.PreparedRequest(
        model="claude:sonnet", model_alias="sonnet", prompt="unused",
        system_prompt="You are helpful.",
        messages=({"role": "user", "content": "hello"},))
    from shared.agent.seats import claude
    argv, stdin = claude.invoke("/fake/claude", prepared, tmp_path)
    # One prompt, one turn. `--input-format stream-json` treats every `user` line as a live turn,
    # so a replayed transcript re-answers the whole history and claude's own replies land between
    # the messages being replayed.
    assert "--input-format" not in argv
    assert stdin == prepared.prompt

def test_the_tool_block_is_the_last_thing_in_the_prompt():
    """Assembled by `prepare`, not by the seat's `invoke` — one place builds the turn, so every
    seat gets the block in the same position. Last, because that is what made the model emit a call
    instead of answering "I don't have access to a Read tool"."""
    body = {"model": "claude:sonnet",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"name": "Read", "input_schema": {"type": "object"}}]}
    prepared = cli_seat.prepare(body, "claude", wire="anthropic")
    assert prepared.prompt.rstrip().endswith(prepared.tool_text.rstrip())
    assert '"hello"' in prepared.prompt


# edge cases for to_stream_json (bugs found in code review)

def test_to_stream_json_merges_consecutive_tool_results():
    """B2: parallel tool calls produce consecutive role:tool messages. Claude requires
    alternating user/assistant turns, so they must merge into ONE user message."""
    from shared.agent.seats.claude import to_stream_json
    messages = [
        {"role": "user", "content": "check both files"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "read", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "file A"},
        {"role": "tool", "tool_call_id": "c2", "content": "file B"},
        {"role": "user", "content": "thanks"},
    ]
    objs = [json.loads(line) for line in to_stream_json(messages).splitlines()]
    types = [o["type"] for o in objs]
    assert types == ["user", "assistant", "user", "user"]
    merged = objs[2]["message"]["content"]
    assert len(merged) == 2
    assert all(b["type"] == "tool_result" for b in merged)
    assert merged[0]["content"] == "file A"
    assert merged[1]["content"] == "file B"


def test_to_stream_json_empty_assistant_gets_placeholder():
    """R4: an assistant message with no text and no tool_calls must not produce content: []
    — Claude requires at least one block per message."""
    from shared.agent.seats.claude import to_stream_json
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None},
        {"role": "user", "content": "hello?"},
    ]
    objs = [json.loads(line) for line in to_stream_json(messages).splitlines()]
    assistant_blocks = objs[1]["message"]["content"]
    assert len(assistant_blocks) >= 1


def test_to_stream_json_image_in_user_content_is_omitted():
    """R3: OpenAI image_url blocks in user content must not be passed raw to Claude —
    flatten to text (images become 'omitted')."""
    from shared.agent.seats.claude import to_stream_json
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "describe this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
    ]}]
    obj = json.loads(to_stream_json(messages).splitlines()[0])
    content = obj["message"]["content"]
    assert isinstance(content, list)
    assert any(b["type"] == "text" and "describe this" in b["text"] for b in content)
    assert not any("image_url" in str(b) for b in content)


def test_to_stream_json_tools_text_with_no_user_messages():
    """R5: if there are no user messages, tools_text creates one rather than being dropped."""
    from shared.agent.seats.claude import to_stream_json
    messages = [{"role": "assistant", "content": "hi"}]
    lines = to_stream_json(messages, tools_text="TOOLS")
    last = json.loads(lines.splitlines()[-1])
    assert last["type"] == "user"
    assert any("TOOLS" in b.get("text", "") for b in last["message"]["content"])
