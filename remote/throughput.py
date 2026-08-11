"""Decode throughput (tokens/sec) measured on this node's own served traffic.

The figure the grid page shows per node. It is measured, never benchmarked: a synthetic warm-up
request describes a machine that was idle at start-up, while what an operator wants to know is how
fast the node is answering *now*, under whatever load it actually has.

**Two clocks, not one.** A request's wall time is prompt processing plus decode; dividing tokens by
all of it reports a slower node whenever the prompt is long, even though nothing about the GPU
changed. So the streaming meter starts its clock at the FIRST token, not at the request — the
interval it divides by is decode alone. The whole-body path cannot see that boundary (the engine
returns one object), so it prefers llama.cpp's own `timings.predicted_per_second`, which the engine
measured with the same distinction, and only falls back to wall-clock when the engine says nothing.

**Every dialect, one extractor.** The provider forwards four request shapes and each spells the
generated-token count differently:

    usage.completion_tokens         OpenAI chat/completions (stream and whole)
    usage.output_tokens             Anthropic /messages, OpenAI /responses (whole)
    response.usage.output_tokens    OpenAI /responses (stream, `response.completed`)

`completion_tokens_from` reads all three, so a caller never has to know which wire it is on.

**The LAST usage wins.** Anthropic streams `usage` twice — `message_start` carries
`output_tokens: 1` before anything is generated, `message_delta` carries the final count. Taking the
first would report ~1 token/sec for every Anthropic request, so the meter keeps overwriting.

Nothing here can change what the consumer receives: the meter is a pass-through generator that
yields its input byte for byte. Measurement that alters the stream it measures is not measurement.
"""

from __future__ import annotations

import json
import time
from typing import Any, Iterable, Iterator

# Below this many generated tokens a sample is noise, not a measurement: the interval between the
# first token and the last is then a handful of milliseconds, and normal jitter in it swings the
# quotient by an order of magnitude. Such a request leaves the previous (real) figure standing rather
# than replacing it with a number that would swing the gauge on every one-word reply.
MIN_TOKENS = 16

# Largest partial line the stream meter will buffer while hunting for a newline. Text SSE lines are
# tiny (one delta each), so this is never approached in practice — it exists so that a stream which
# somehow contains an enormous unbroken line degrades to "not measured" instead of accumulating it in
# memory. The media path, whose events really are megabytes of base64, is never metered at all.
_MAX_LINE = 1024 * 1024


def _positive_int(value: Any) -> int | None:
    """A token count, or None. `bool` is rejected despite subclassing `int` — a payload carrying
    `output_tokens: true` means nothing, and 1 would look like a real (and absurd) measurement."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def completion_tokens_from(payload: Any) -> int | None:
    """Generated-token count out of any of the dialects above, or None when the payload carries none.

    Checked in order, first hit wins; the `response` nesting is the Responses stream's terminal
    event. Returning None is the normal outcome for most events in a stream — only the terminal one
    carries usage — and for whole dialects that report nothing, which is exactly when the caller must
    decline to report a throughput rather than invent one.
    """
    if not isinstance(payload, dict):
        return None
    for holder in (payload, payload.get("response")):
        if not isinstance(holder, dict):
            continue
        usage = holder.get("usage")
        if not isinstance(usage, dict):
            continue
        for key in ("completion_tokens", "output_tokens"):
            tokens = _positive_int(usage.get(key))
            if tokens is not None:
                return tokens
    return None


def _tokens_from_line(line: bytes) -> int | None:
    """Token count out of one SSE line, or None. Accepts a bare JSON object too, so the same scanner
    reads the block-aligned Responses stream and the raw chat stream."""
    line = line.strip()
    if line.startswith(b"event:"):  # a Responses block's event-name line carries no payload
        return None
    if line.startswith(b"data:"):
        line = line[len(b"data:"):].strip()
    if not line or line == b"[DONE]" or not line.startswith(b"{"):
        return None
    try:
        payload = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    return completion_tokens_from(payload)


class StreamMeter:
    """Times one streamed response while passing its bytes through untouched.

    Wrap the engine's chunk iterator with `measure`, then read `tok_s` once it is exhausted. The
    result is None whenever the stream carried no usage, was too short to time (`MIN_TOKENS`), or
    produced its tokens in no measurable interval.
    """

    def __init__(self) -> None:
        self._first: float | None = None
        self._last: float | None = None
        self._tokens: int | None = None
        self._buf = b""
        self._overflowed = False

    def measure(self, chunks: Iterable[bytes]) -> Iterator[bytes]:
        """Yield `chunks` verbatim, timing them on the way past. The invariant this must never break
        is ``b"".join(measure(x)) == b"".join(x)`` — the relay re-splits these bytes itself and
        refuses smuggled bare-CR, so a meter that "helpfully" normalised anything would mask the very
        thing that sanitiser exists to catch."""
        for chunk in chunks:
            now = time.monotonic()
            if self._first is None:
                self._first = now
            self._last = now
            self._scan(chunk)
            yield chunk

    def _scan(self, chunk: bytes) -> None:
        """Accumulate and read complete lines. Chunk boundaries are arbitrary — `iter_bytes` splits
        wherever the socket did, so a `data:` line routinely arrives in two pieces and scanning each
        chunk alone would miss most of them."""
        if self._overflowed:
            return
        self._buf += chunk
        while (index := self._buf.find(b"\n")) != -1:
            line, self._buf = self._buf[:index], self._buf[index + 1:]
            tokens = _tokens_from_line(line)
            if tokens is not None:
                self._tokens = tokens  # last usage wins — see the module docstring
        if len(self._buf) > _MAX_LINE:
            self._overflowed = True
            self._buf = b""

    @property
    def tok_s(self) -> float | None:
        if self._tokens is None or self._tokens < MIN_TOKENS:
            return None
        if self._first is None or self._last is None:
            return None
        elapsed = self._last - self._first
        if elapsed <= 0:
            return None
        return self._tokens / elapsed

    def flush(self) -> None:
        """Scan whatever is left in the buffer. A stream whose last line has no trailing newline
        holds its usage there, so call this once the iterator is exhausted."""
        if self._overflowed or not self._buf:
            return
        tokens = _tokens_from_line(self._buf)
        self._buf = b""
        if tokens is not None:
            self._tokens = tokens


def whole_body_tok_s(content: bytes, elapsed: float) -> float | None:
    """Decode rate for a non-streamed reply, or None when it cannot be measured.

    llama.cpp's `timings.predicted_per_second` is preferred and is not merely a shortcut: the engine
    measured decode with prompt processing already excluded, which is precisely the distinction this
    path otherwise cannot make. Every other engine falls back to tokens over wall-clock, which
    understates the rate on a long prompt — honest but approximate, and the best available when the
    engine reports one number and no timings.
    """
    try:
        payload = json.loads(content)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    timings = payload.get("timings")
    if isinstance(timings, dict):
        rate = timings.get("predicted_per_second")
        if not isinstance(rate, bool) and isinstance(rate, (int, float)) and rate > 0:
            return float(rate)
    tokens = completion_tokens_from(payload)
    if tokens is None or tokens < MIN_TOKENS or elapsed <= 0:
        return None
    return tokens / elapsed
