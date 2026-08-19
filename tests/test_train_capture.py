"""Capture tests: the store, redaction, the feedback join, and the rule that keeps unattended
training honest — a model's own unjudged output is never treated as truth.

Also covers the serving-path hook, because the contract there is absolute: capture may cost an
example, never an answer.
"""
from __future__ import annotations

import json

import pytest

from train import capture


@pytest.fixture(autouse=True)
def grid_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    return tmp_path


def _on(**kwargs) -> capture.Policy:
    policy = capture.Policy(enabled=True, **kwargs)
    capture.save_policy(policy)
    return policy


@pytest.fixture(autouse=True)
def _flush_writes():
    """Appends go to a background writer, so anything reading straight after writing waits."""
    yield
    capture.flush()


# --- policy: off unless someone opts in ----------------------------------------------------

def test_capture_is_off_until_turned_on():
    assert capture.load_policy().enabled is False
    assert capture.record("a ticket", "an answer", model="m") is None
    assert not capture.capture_dir().glob("traffic-*.jsonl") or True
    assert capture.summarize().requests == 0


def test_policy_round_trips():
    capture.save_policy(capture.Policy(enabled=True, sample=0.5, retain_days=7,
                                      teachers=("openai:gpt-5.6",)))
    policy = capture.load_policy()
    assert (policy.enabled, policy.sample, policy.retain_days) == (True, 0.5, 7)
    assert policy.teachers == ("openai:gpt-5.6",)


def test_a_corrupt_policy_file_means_off_not_crash():
    capture.policy_file().parent.mkdir(parents=True, exist_ok=True)
    capture.policy_file().write_text("{not json", encoding="utf-8")
    assert capture.load_policy().enabled is False


# --- redaction ------------------------------------------------------------------------------

def test_redaction_scrubs_the_obvious_identifiers():
    text = ("Contact me at dee@autonomous.ai or 555 867 5309. "
            "Card 4111 1111 1111 1111. Key sk-abcdefghijklmnop.")
    clean = capture.redact(text)
    assert "dee@autonomous.ai" not in clean and "[email]" in clean
    assert "4111 1111 1111 1111" not in clean and "[card]" in clean
    assert "867 5309" not in clean and "[phone]" in clean
    assert "sk-abcdefghijklmnop" not in clean and "[secret]" in clean


def test_captured_rows_are_redacted_by_default():
    _on()
    capture.record("ticket from bob@example.com", "we emailed bob@example.com", model="m")
    capture.flush()
    row = json.loads(next(capture.capture_dir().glob("traffic-*.jsonl")).read_text().splitlines()[0])
    assert "bob@example.com" not in row["prompt"] + row["completion"]


def test_redaction_can_be_turned_off_deliberately():
    _on(redact=False)
    capture.record("ticket from bob@example.com", "ok", model="m")
    capture.flush()
    row = json.loads(next(capture.capture_dir().glob("traffic-*.jsonl")).read_text().splitlines()[0])
    assert "bob@example.com" in row["prompt"]


# --- the anti-collapse rule -----------------------------------------------------------------

def test_an_unjudged_answer_is_stored_but_never_trained_on():
    """The whole ethic: imitating your own guesses is how a model drifts."""
    _on()
    capture.record("a ticket", "the model's guess", model="local-model")
    assert capture.summarize().requests == 1
    assert capture.build_examples() == []          # nothing to learn from yet


def test_an_edited_answer_becomes_ground_truth():
    _on()
    request_id = capture.record("a ticket", "the model's draft", model="local-model")
    capture.record_feedback(request_id, "edited", final_text="what the agent actually sent")
    examples = capture.build_examples()
    assert len(examples) == 1
    assert examples[0].answer == "what the agent actually sent"   # the human's version wins
    assert (examples[0].source, examples[0].weight) == ("edited", 1.0)


def test_an_accepted_answer_is_worth_less_than_a_corrected_one():
    _on()
    accepted = capture.record("ticket A", "draft A", model="local-model")
    capture.record_feedback(accepted, "accepted")
    edited = capture.record("ticket B", "draft B", model="local-model")
    capture.record_feedback(edited, "edited", final_text="better B")
    by_source = {e.source: e for e in capture.build_examples()}
    assert by_source["accepted"].weight < by_source["edited"].weight


def test_a_rejected_answer_is_never_imitated():
    _on()
    request_id = capture.record("a ticket", "a bad draft", model="local-model")
    capture.record_feedback(request_id, "rejected")
    assert capture.build_examples() == []


def test_a_stronger_models_answer_is_a_target_without_any_human():
    """The hybrid flywheel: what the frontier answered becomes what the local model learns."""
    _on(teachers=("openai:gpt-5.6",))
    capture.record("a hard ticket", "the frontier answer", model="openai:gpt-5.6")
    capture.record("an easy ticket", "the local guess", model="local-model")
    examples = capture.build_examples()
    assert [(e.source, e.answer) for e in examples] == [("teacher", "the frontier answer")]


def test_feedback_rejects_a_verdict_we_do_not_understand():
    _on()
    assert capture.record_feedback("abc", "loved-it") is False


# --- summary: numbers a person can act on ---------------------------------------------------

def test_summary_speaks_plainly_at_each_stage():
    off = capture.summarize()
    assert "Not collecting" in off.headline and "Turn it on" in off.advice

    _on()
    empty = capture.summarize()
    assert "nothing has come through" in empty.headline

    capture.record("t1", "a1", model="m")
    unjudged = capture.summarize()
    assert unjudged.requests == 1 and unjudged.trainable == 0
    assert "none can be learned from yet" in unjudged.headline
    assert "reports what the person did" in unjudged.advice

    for i in range(3):
        request_id = capture.record(f"t{i + 2}", f"a{i + 2}", model="m")
        capture.record_feedback(request_id, "edited", final_text=f"fixed {i}")
    ready = capture.summarize()
    assert ready.trainable == 3 and ready.edited == 3
    assert "3 examples ready to learn from" in ready.headline
    assert "few hundred makes a real difference" in ready.advice


def test_summary_counts_models_and_dates():
    _on()
    capture.record("t", "a", model="qwen-local")
    capture.record("t2", "a2", model="qwen-local")
    capture.record("t3", "a3", model="other")
    summary = capture.summarize()
    assert summary.models == {"qwen-local": 2, "other": 1}
    assert summary.first_seen and summary.last_seen


# --- turning traffic into the same files every other path uses -----------------------------

def test_written_files_match_what_the_packs_and_browser_write(tmp_path):
    _on()
    for i in range(5):
        request_id = capture.record(f"ticket {i}", "draft", model="m")
        capture.record_feedback(request_id, "edited", final_text=f"the real reply {i}")
    examples = capture.build_examples()
    result = capture.write_training_files(examples, tmp_path / "built",
                                         system_prompt="You are the support agent.")
    assert result["examples"] == 5
    built = tmp_path / "built"
    # The RL path reads prompts.jsonl; the graders read refs.jsonl; stage one reads sft.jsonl.
    assert len((built / "prompts.jsonl").read_text().splitlines()) == 5
    refs = [json.loads(line) for line in (built / "refs.jsonl").read_text().splitlines()]
    assert refs[0]["reference"].startswith("the real reply")

    from train.sft import load_examples

    sft = load_examples(built / "sft.jsonl", minimum=5)
    assert [m["role"] for m in sft[0]["messages"]] == ["system", "user", "assistant"]
    sources = json.loads((built / "sources.json").read_text())
    assert sources["by_source"]["edited"] == 5


def test_prune_drops_old_days_only():
    from datetime import date, timedelta

    _on(retain_days=3)
    stale_day = (date.today() - timedelta(days=10)).isoformat()  # noqa: DTZ011 — local day, as the store uses
    old = capture.capture_dir() / f"traffic-{stale_day}.jsonl"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text('{"id":"x"}\n', encoding="utf-8")
    capture.record("today", "answer", model="m")
    # ⚠️ Appends go to a BACKGROUND writer (`capture._append`), so today's file does not exist the
    # instant `record` returns — the autouse `_flush_writes` fixture only runs at teardown, which is
    # after every assertion below. Every other test in this file that reads back what it wrote
    # flushes by hand; this one did not, and failed on CI (3.12) while passing on a developer's Mac.
    #
    # It also made the last assertion VACUOUS in the other direction: with nothing on disk, `prune`
    # was never actually asked to spare today's file, which is the whole thing this test is for.
    capture.flush()
    assert capture.prune() == 1
    assert not old.exists()
    assert list(capture.capture_dir().glob("traffic-*.jsonl"))   # today's file survived


# --- the serving path: capture may cost an example, never an answer -----------------------

def test_serving_helpers_read_both_dialects():
    from local.server import _answer_text, _prompt_text

    assert _prompt_text({"prompt": "raw text"}) == "raw text"
    assert _prompt_text({"messages": [{"role": "system", "content": "s"},
                                      {"role": "user", "content": "the work"}]}) == "the work"
    assert _prompt_text({"messages": [{"role": "assistant", "content": "a"}]}) == ""
    assert _prompt_text({}) == ""
    assert _answer_text({"choices": [{"text": "completion form"}]}) == "completion form"
    assert _answer_text({"choices": [{"message": {"content": "chat form"}}]}) == "chat form"
    assert _answer_text({"choices": []}) == ""
    assert _answer_text("not a dict") == ""


def test_capture_failure_cannot_break_serving(monkeypatch):
    """A capture problem must be invisible to the customer's request."""
    from local import server

    _on()

    def explode(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr("train.capture.record", explode)

    class FakeResponse:
        status_code = 200
        content = b'{"choices": [{"text": "an answer"}]}'

        def json(self):
            return {"choices": [{"text": "an answer"}]}

    assert server._capture_exchange({"prompt": "p", "model": "m"}, FakeResponse()) is None


def test_capture_hook_returns_an_id_apps_can_quote_back():
    from local import server

    _on()

    class FakeResponse:
        status_code = 200
        content = b'{"choices": [{"message": {"content": "the answer"}}]}'

        def json(self):
            return {"choices": [{"message": {"content": "the answer"}}]}

    request_id = server._capture_exchange(
        {"messages": [{"role": "user", "content": "the work"}], "model": "m"}, FakeResponse()
    )
    assert request_id and len(request_id) == 16
    assert capture.summarize().requests == 1


def test_an_enormous_answer_is_not_stored_at_all():
    """A five-megabyte file dump is not a training example, and parsing it twice on a customer's
    request would be work done for nothing."""
    from local import server

    _on()
    big = json.dumps({"choices": [{"text": "x" * 400_000}]}).encode()

    class HugeResponse:
        status_code = 200
        content = big

        def json(self):
            return json.loads(big)

    assert server._capture_exchange({"prompt": "p", "model": "m"}, HugeResponse()) is None
    assert capture.summarize().requests == 0


def test_a_long_answer_is_clipped_rather_than_stored_whole():
    _on()
    capture.record("a ticket", "y" * 50_000, model="m")
    capture.flush()
    row = json.loads(next(capture.capture_dir().glob("traffic-*.jsonl")).read_text().splitlines()[0])
    assert len(row["completion"]) < capture.MAX_TEXT + 40
    assert row["completion"].endswith("…[trimmed]")


def test_redaction_cannot_be_used_to_stall_the_grid():
    """An unauthenticated LAN request used to buy ~34 seconds of blocked event loop per 100 KB."""
    import time

    _on()
    started = time.time()
    capture.record_feedback("rid", "edited", final_text="A" * 200_000)
    capture.flush()
    assert time.time() - started < 1.0


def test_sampling_actually_drops_requests():
    """`time.time_ns() % 1000` is always 0 on macOS, so the old sampling kept everything."""
    _on(sample=0.0)
    for i in range(40):
        capture.record(f"t{i}", "a", model="m")
    assert capture.summarize().requests == 0


# --- the feedback endpoint, through the real server ---------------------------------------

def _server():
    from fastapi.testclient import TestClient

    from local.server import create_app

    return TestClient(create_app(grid_id="ag-test", grid_name="test"))


def test_feedback_endpoint_stores_what_the_human_did():
    _on()
    request_id = capture.record("a ticket", "a draft", model="m")
    response = _server().post("/v1/feedback", json={
        "request_id": request_id, "verdict": "edited", "final_text": "what we really sent",
    })
    assert response.status_code == 200 and response.json() == {"stored": True}
    examples = capture.build_examples()
    assert examples[0].answer == "what we really sent"


def test_feedback_is_a_no_op_when_collecting_is_off():
    """An instrumented app must not break because the owner hasn't opted in."""
    response = _server().post("/v1/feedback", json={"request_id": "abc", "verdict": "accepted"})
    assert response.status_code == 200
    assert response.json() == {"stored": False, "reason": "collecting is off on this grid"}


def test_feedback_validates_its_input():
    _on()
    client = _server()
    assert client.post("/v1/feedback", json={"verdict": "accepted"}).status_code == 400
    assert client.post("/v1/feedback", json={"request_id": "a", "verdict": "great"}).status_code == 400
    assert client.post("/v1/feedback", content=b"{not json").status_code == 400
    bad = client.post("/v1/feedback", json={"request_id": "a", "verdict": "great"}).json()
    assert "accepted" in bad["error"]["message"]   # says what the allowed verdicts are


# --- streamed answers: how every chat interface actually talks --------------------------------

def test_a_streamed_answer_is_captured_too():
    """Every chat UI streams, so skipping this branch meant missing the traffic that matters."""
    from local.server import _StreamCollector

    _on()
    collector = _StreamCollector({"messages": [{"role": "user", "content": "the ticket"}],
                                  "model": "qwen-local"})
    for piece in ("Sorry ", "about ", "the E07"):
        chunk = b'data: ' + json.dumps({"choices": [{"delta": {"content": piece}}]}).encode() + b"\n\n"
        collector.feed(chunk)
    collector.feed(b"data: [DONE]\n\n")
    collector.store()
    capture.flush()

    rows = [json.loads(line) for line in
            next(capture.capture_dir().glob("traffic-*.jsonl")).read_text().splitlines()]
    assert rows[0]["completion"] == "Sorry about the E07"
    assert rows[0]["id"] == collector.request_id       # the id the client was handed mid-stream


def test_a_streamed_answer_in_completions_dialect_is_captured():
    from local.server import _StreamCollector

    _on()
    collector = _StreamCollector({"prompt": "the ticket", "model": "m"})
    collector.feed(b'data: ' + json.dumps({"choices": [{"text": "hello"}]}).encode() + b"\n\n")
    collector.store()
    capture.flush()
    assert capture.summarize().requests == 1


def test_a_very_long_stream_stops_collecting_rather_than_growing():
    from local.server import _StreamCollector

    _on()
    collector = _StreamCollector({"prompt": "p", "model": "m"}, limit=100)
    for _ in range(50):
        collector.feed(b'data: ' + json.dumps({"choices": [{"delta": {"content": "x" * 20}}]}).encode() + b"\n\n")
    collector.store()
    capture.flush()
    row = json.loads(next(capture.capture_dir().glob("traffic-*.jsonl")).read_text().splitlines()[0])
    assert len(row["completion"]) < 200      # bounded, not the full 1000 characters


def test_junk_in_a_stream_is_ignored_not_fatal():
    from local.server import _StreamCollector

    _on()
    collector = _StreamCollector({"prompt": "p", "model": "m"})
    collector.feed(b"data: {not json\n\n")
    collector.feed(b": keepalive\n\n")
    collector.feed(b'data: ' + json.dumps({"choices": []}).encode() + b"\n\n")
    collector.feed(b'data: ' + json.dumps({"choices": [{"delta": {"content": "ok"}}]}).encode() + b"\n\n")
    collector.store()
    capture.flush()
    row = json.loads(next(capture.capture_dir().glob("traffic-*.jsonl")).read_text().splitlines()[0])
    assert row["completion"] == "ok"


def test_a_model_filter_keeps_the_teacher_rows(tmp_path, monkeypatch):
    """Teacher rows are stored under the TEACHER's name — that is what answered.

    Filtering captured traffic to "this model's own work" therefore dropped exactly the examples
    distillation depends on: the hard requests a stronger model handled.
    """
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    from train import capture

    capture.save_policy(capture.Policy(enabled=True, teachers=("gpt-5",)))
    capture.record(prompt="easy one", completion="fine", model="support-v1", request_id="a")
    capture.record(prompt="hard one", completion="a much better answer", model="gpt-5",
                   request_id="b")          # a teacher by policy, not by flag
    capture.record(prompt="someone else's", completion="x", model="routing-v1", request_id="c")
    capture.flush()
    capture.record_feedback("a", verdict="accepted")
    capture.flush()

    ours = capture.build_examples(models=("support-v1",))
    kept = {e.prompt: e.source for e in ours}
    assert kept.get("hard one") == "teacher"          # the whole point of a teacher
    assert "easy one" in kept
    assert "someone else's" not in kept               # another model's work still excluded


def test_teacher_rows_can_be_kept_out_when_several_models_share_a_grid(tmp_path, monkeypatch):
    """A teacher row is stored under the teacher's name — the model the request asked for — so
    nothing in the store says which of your models it belongs to. Sharing them is right on a grid
    with one model and wrong when two models do different jobs, so it is a choice, not a default
    that pretends to be knowledge."""
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    from train import capture

    capture.save_policy(capture.Policy(enabled=True, teachers=("gpt-5",)))
    capture.record(prompt="ours", completion="fine", model="support-v1", request_id="a")
    capture.record(prompt="a hard one someone sent to the frontier", completion="a good answer",
                   model="gpt-5", request_id="b")
    capture.flush()
    capture.record_feedback("a", verdict="accepted")
    capture.flush()

    shared = {e.prompt for e in capture.build_examples(models=("support-v1",))}
    assert "a hard one someone sent to the frontier" in shared

    alone = {e.prompt for e in capture.build_examples(models=("support-v1",),
                                                      include_teachers=False)}
    assert alone == {"ours"}
