"""Decode-throughput measurement — the `tok_s` the grid page shows per node.

Pure logic, no engine and no network: every case here is a byte string shaped like something a real
engine sends, so the dialect differences these tests pin are the ones that actually reach a provider.
"""

from __future__ import annotations

import time

from remote import throughput


def _drain(meter: throughput.StreamMeter, chunks: list[bytes], *, pause: float = 0.002) -> bytes:
    """Run `chunks` through the meter, spacing them so the elapsed interval is measurable."""
    out = b""
    for chunk in meter.measure(chunks):
        out += chunk
        time.sleep(pause)
    meter.flush()
    return out


def _chat_stream(tokens: int) -> bytes:
    """An OpenAI chat stream ending in the usage-only chunk `stream_options.include_usage` appends —
    which the master injects on every job, so a provider always receives it."""
    return (
        b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
        + b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n' * tokens
        + b'data: {"choices":[],"usage":{"prompt_tokens":9,"completion_tokens":%d}}\n\n' % tokens
        + b"data: [DONE]\n\n"
    )


def test_reads_the_generated_token_count_of_every_dialect_the_provider_forwards():
    """One extractor for all four request shapes, so no forward path has to know its own wire."""
    assert throughput.completion_tokens_from({"usage": {"completion_tokens": 42}}) == 42   # chat
    assert throughput.completion_tokens_from({"usage": {"output_tokens": 42}}) == 42       # anthropic
    assert throughput.completion_tokens_from({"response": {"usage": {"output_tokens": 42}}}) == 42


def test_a_payload_carrying_no_generated_count_yields_nothing():
    """Absent is the answer that makes the caller decline to report, rather than invent, a rate."""
    assert throughput.completion_tokens_from({"usage": {"prompt_tokens": 10}}) is None
    assert throughput.completion_tokens_from({"usage": {"output_tokens": True}}) is None
    assert throughput.completion_tokens_from({"usage": {"completion_tokens": 0}}) is None
    assert throughput.completion_tokens_from("not an object") is None


def test_the_meter_never_alters_the_stream_it_measures():
    """The relay re-splits these bytes and refuses smuggled bare-CR, so a meter that normalised
    anything would mask the very thing that sanitiser exists to catch. Chunks are cut at 7 bytes so
    every SSE line arrives torn across boundaries, as a real socket delivers them."""
    stream = _chat_stream(30)
    meter = throughput.StreamMeter()

    assert _drain(meter, [stream[i:i + 7] for i in range(0, len(stream), 7)]) == stream
    assert meter.tok_s is not None


def test_throughput_is_measured_over_decode_alone():
    """Two clocks, not one: the interval starts at the FIRST token, so a long prompt's processing
    time never makes a node look slower than it decodes."""
    meter = throughput.StreamMeter()
    chunks = [b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'] * 20
    chunks.append(b'data: {"choices":[],"usage":{"completion_tokens":20}}\n\n')

    _drain(meter, chunks, pause=0.005)

    # 21 chunks spaced 5ms apart => ~0.1s of decode for 20 tokens. A meter that had started its clock
    # at the request (or divided by wall time including a prompt) would land far below this floor.
    assert meter.tok_s is not None
    assert 80 < meter.tok_s < 400


def test_an_anthropic_stream_reports_the_final_count_not_the_opening_one():
    """`message_start` announces `output_tokens: 1` before anything is generated and `message_delta`
    carries the real total. Reading the first would report ~1 tok/s for every Anthropic request."""
    meter = throughput.StreamMeter()
    stream = [
        b'event: message_start\ndata: {"message":{"usage":{"input_tokens":9,"output_tokens":1}}}\n\n',
        b'event: message_delta\ndata: {"usage":{"output_tokens":40}}\n\n',
    ]

    _drain(meter, stream)

    assert meter.tok_s is not None
    assert meter.tok_s > 40  # 40 tokens over the ~4ms above, not 1 token over the same span


def test_a_responses_stream_is_read_from_its_terminal_event():
    meter = throughput.StreamMeter()
    stream = [
        b'event: response.output_text.delta\ndata: {"delta":"x"}\n\n',
        b'event: response.completed\ndata: {"response":{"usage":{"output_tokens":64}}}\n\n',
    ]

    _drain(meter, stream)

    assert meter.tok_s is not None


def test_a_reply_too_short_to_time_is_not_reported():
    """A handful of tokens decode in milliseconds, where ordinary jitter swings the quotient by an
    order of magnitude. Such a sample leaves the previous real figure standing."""
    meter = throughput.StreamMeter()

    _drain(meter, [b'data: {"usage":{"completion_tokens":3}}\n\n'])

    assert meter.tok_s is None


def test_a_stream_carrying_no_usage_is_not_reported():
    """The media path is never metered, but a text engine that somehow answers without usage must
    still degrade to silence rather than to a rate divided by nothing."""
    meter = throughput.StreamMeter()

    _drain(meter, [b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'])

    assert meter.tok_s is None


def test_an_enormous_unbroken_line_stops_the_scan_without_stopping_the_stream():
    """The buffer is bounded: a stream that never sends a newline degrades to "not measured" instead
    of accumulating in memory — and its bytes still reach the consumer untouched."""
    blob = b'data: {"image":"' + b"A" * (2 * 1024 * 1024) + b'"}\n\n'
    meter = throughput.StreamMeter()

    assert _drain(meter, [blob], pause=0) == blob
    assert meter.tok_s is None


def test_a_whole_body_reply_prefers_the_engines_own_decode_timing():
    """llama.cpp measured decode with prompt processing already excluded — the exact distinction a
    single returned object otherwise hides. 500 tokens over 10s of wall time is 50/s; the engine's
    own 137.4 is the honest number and must win."""
    body = b'{"timings":{"predicted_per_second":137.4},"usage":{"completion_tokens":500}}'

    assert throughput.whole_body_tok_s(body, 10.0) == 137.4


def test_a_whole_body_reply_without_engine_timings_falls_back_to_wall_clock():
    """Every dialect that reaches the whole-body forward is covered, in its own spelling."""
    assert throughput.whole_body_tok_s(b'{"usage":{"completion_tokens":100}}', 2.0) == 50.0
    assert throughput.whole_body_tok_s(b'{"usage":{"output_tokens":100}}', 2.0) == 50.0


def test_an_unreadable_or_untimeable_whole_body_reply_reports_nothing():
    assert throughput.whole_body_tok_s(b"not json", 1.0) is None
    assert throughput.whole_body_tok_s(b'{"usage":{}}', 1.0) is None
    assert throughput.whole_body_tok_s(b'{"usage":{"completion_tokens":100}}', 0.0) is None
    assert throughput.whole_body_tok_s(b'{"usage":{"completion_tokens":3}}', 1.0) is None
