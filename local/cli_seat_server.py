"""OpenAI and Anthropic APIs for a CLI seat: grid ──/chat/completions or /messages──▶ here
──subprocess──▶ `<cli> -p …`

Loopback-only — this process can spend the operator's subscription. CLI-agnostic: the driver
arrives as a `SeatSpec`, so a second CLI reuses this server unchanged. Which wire a request came
in on rides through as `wire` (`"openai"` | `"anthropic"`) so the answer goes back in the same one.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from shared.agent import cli_seat
from shared.agent.cli_seat import SeatOptions, SeatSpec  # noqa: F401


def create_app(
    *, spec: SeatSpec, binary: str, options: SeatOptions
) -> FastAPI:
    app = FastAPI(
        title=f"Grid CLI Seat ({spec.label})",
        description=f"Engine-local OpenAI API backed by the {spec.label} CLI.",
        version="0.1.0",
    )
    app.state.spec = spec
    app.state.binary = binary
    app.state.options = options
    # One CLI process per in-flight request; default 1 so a single subscription is not raced.
    app.state.slots = asyncio.Semaphore(max(1, options.concurrency))
    # Single-flight: without it, N callers missing an expired cache each spawn their own probe.
    app.state.quota_lock = asyncio.Lock()
    app.state.quota_cache: tuple[float, object] | None = None

    @app.get("/health")
    async def health():
        return {"ok": True, "engine": "cli-seat", "kind": spec.kind,
                "models": cli_seat.advertised_models(spec.kind)}

    @app.get("/quota")
    async def quota(fresh: bool = False):
        """Quota without sending a job.

        Cached by default because the poll loop reads this every heartbeat to report upstream;
        probing on that timer would spend seconds per interval for a number that barely moves.
        `?fresh=1` forces a live read for an operator who is asking right now.
        """
        snapshot = await _quota(app, fresh=fresh)
        options: SeatOptions = app.state.options
        return {
            "kind": spec.kind,
            "known": snapshot is not None,
            "session_pct": snapshot.session_pct if snapshot else None,
            "session_reset": snapshot.session_reset if snapshot else None,
            "week_pct": snapshot.week_pct if snapshot else None,
            "week_reset": snapshot.week_reset if snapshot else None,
            "session_limit": options.session_limit,
            "week_limit": options.week_limit,
            "serving": cli_seat.quota_refusal(snapshot, options) is None,
            # 0-100, how much of this seat's allowance is left. What the relay sorts on, so it is
            # computed HERE — the seat is the only place that knows both the usage and the
            # operator's ceilings.
            "headroom_pct": cli_seat.quota_headroom_pct(snapshot, options),
        }

    @app.get("/models")
    @app.get("/v1/models")
    async def models():
        return {
            "object": "list",
            "data": [
                {"id": name, "object": "model", "owned_by": spec.kind}
                for name in cli_seat.advertised_models(spec.kind)
            ],
        }

    @app.post("/chat/completions")
    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        return await _handle(app, request, wire="openai")

    @app.post("/responses")
    @app.post("/v1/responses")
    async def responses(request: Request):
        return await _handle(app, request, wire="responses")

    @app.post("/messages")
    @app.post("/v1/messages")
    async def messages(request: Request):
        return await _handle(app, request, wire="anthropic")

    return app


async def _handle(app: FastAPI, request: Request, *, wire: str = "openai"):
    spec: SeatSpec = app.state.spec
    options: SeatOptions = app.state.options
    raw = await request.body()
    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return _error(400, "Request body is not valid JSON.")
    if not isinstance(body, dict):
        return _error(400, "Request body must be a JSON object.")

    try:
        prepared = await run_in_threadpool(
            cli_seat.prepare, body, spec.kind, spec.tool_protocol, wire,
        )
    except cli_seat.SeatBadRequest as exc:
        return _error(400, str(exc))

    # Before the semaphore: the probe spends no allowance, so it must not hold the spend slot.
    if options.session_limit is not None or options.week_limit is not None:
        snapshot = await _quota(app)
        refusal = cli_seat.quota_refusal(snapshot, options)
        if refusal is not None:
            return _error(429, refusal, error_type="quota_exhausted")


    if body.get("stream") and cli_seat.can_stream(spec):
        return StreamingResponse(_stream(app, prepared, wire=wire), media_type="text/event-stream")

    if wire == "responses":
        answer_fn = cli_seat.answer_response
    elif wire == "anthropic":
        answer_fn = cli_seat.answer_anthropic
    else:
        answer_fn = cli_seat.answer
    async with app.state.slots:
        try:
            # A CLI run is a blocking subprocess of minutes. Off the event loop, or one request
            # would freeze health checks and every other caller.
            completion = await run_in_threadpool(
                answer_fn, spec, prepared, app.state.binary, options.timeout,
            )
        except cli_seat.SeatBadRequest as exc:
            return _error(400, str(exc))
        except cli_seat.SeatError as exc:
            return _error(502, str(exc))

    if body.get("stream"):
        if wire == "responses":
            sse = _as_responses_sse(completion)
        elif wire == "anthropic":
            sse = _as_anthropic_sse(completion)
        else:
            sse = _as_sse(completion)
        return StreamingResponse(sse, media_type="text/event-stream")
    return JSONResponse(completion)


async def _stream(app: FastAPI, prepared, *, wire: str = "openai"):
    """SSE straight from the CLI's own output, chunk by chunk as it arrives.

    A failure mid-stream cannot re-status the response — the headers are long gone — so it is
    emitted as a terminal SSE error frame, which is what an OpenAI-shaped client understands.
    """
    if wire == "anthropic":
        async for frame in _stream_anthropic(app, prepared):
            yield frame
        return
    if wire == "responses":
        async for frame in _stream_responses(app, prepared):
            yield frame
        return
    spec, options = app.state.spec, app.state.options
    stream = cli_seat.answer_stream(spec, prepared, app.state.binary, options.timeout)
    base = {"id": f"chatcmpl-{uuid.uuid4().hex}", "object": "chat.completion.chunk",
            "created": int(time.time()), "model": prepared.model}
    first = True
    # Text before the first `{` cannot be part of a call, so it streams; the rest waits for the
    # parse. See `_SafeText` — this used to hold the WHOLE answer whenever tools were present.
    safe = _SafeText(bool(prepared.tool_protocol))
    async with app.state.slots:
        try:
            while True:
                # next() blocks on the child's stdout, so it runs off the event loop like the
                # non-streaming call does.
                kind, payload = await run_in_threadpool(_next_or_none, stream)
                if kind is None:
                    break
                if kind == "delta":
                    payload = safe.feed(payload)
                    if not payload:
                        continue
                    delta = {"content": payload}
                    if first:
                        delta["role"], first = "assistant", False
                    yield _frame({**base, "choices": [
                        {"index": 0, "delta": delta, "finish_reason": None}]})
                else:
                    payload, quota = payload
                    if quota is not None:
                        # A free reading the answer carried; refresh the cache so the next
                        # request's ceiling check costs nothing.
                        app.state.quota_cache = (time.monotonic(), quota)
                    choice = (payload.get("choices") or [{}])[0]
                    message = choice.get("message") or {}
                    # Only what the stream did not already send — the prose the parse left behind,
                    # minus the span emitted before the first `{`.
                    text = safe.remainder(message.get("content") or "")
                    if text:
                        delta = {"content": text}
                        if first:
                            delta["role"], first = "assistant", False
                        yield _frame({**base, "choices": [
                            {"index": 0, "delta": delta, "finish_reason": None}]})
                    # Tool calls only exist once the whole answer is parsed, so they ride the
                    # final chunk rather than being invented mid-stream.
                    tail = {"tool_calls": [{**c, "index": i} for i, c in
                                           enumerate(message["tool_calls"])]} if message.get("tool_calls") else {}
                    yield _frame({**base, "choices": [
                        {"index": 0, "delta": tail,
                         "finish_reason": choice.get("finish_reason") or "stop"}],
                        "usage": payload.get("usage"), "grid_cli_seat": payload.get("grid_cli_seat")})
        except Exception as exc:  # noqa: BLE001
            # Every exception, not just SeatError: one that escapes here kills the connection
            # mid-body, and the consumer sees a torn stream instead of a reason.
            yield _frame({"error": {"message": str(exc) or type(exc).__name__,
                                    "type": "cli_seat_error"}})
    yield "data: [DONE]\n\n"


def _next_or_none(stream):
    try:
        return next(stream)
    except StopIteration:
        return None, None


async def _stream_anthropic(app: FastAPI, prepared):
    """Anthropic SSE: `message_start`, one block per text/tool_use in the answer, `message_delta`,
    `message_stop`.

    Same streaming rule as the OpenAI stream above, via the same `_SafeText`: text before the first
    `{` goes out as it arrives, the rest waits for the parse — because only the parse knows which
    bytes were the call, and emitting those early is what let a client print raw tool JSON as prose
    AND run the tool.
    """
    spec, options = app.state.spec, app.state.options
    stream = cli_seat.answer_stream_anthropic(spec, prepared, app.state.binary, options.timeout)
    safe = _SafeText(bool(prepared.tool_protocol))
    msg_id = f"msg_{uuid.uuid4().hex}"

    yield _anthropic_frame({"type": "message_start", "message": {
        "id": msg_id, "type": "message", "role": "assistant", "model": prepared.model,
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0}}})

    index, text_open = 0, False
    stop_reason, usage = "end_turn", {"output_tokens": 0}
    async with app.state.slots:
        try:
            while True:
                # next() blocks on the child's stdout, so it runs off the event loop like the
                # non-streaming call does.
                kind, payload = await run_in_threadpool(_next_or_none, stream)
                if kind is None:
                    break
                if kind == "delta":
                    payload = safe.feed(payload)
                    if not payload:
                        continue
                    if not text_open:
                        yield _anthropic_frame({"type": "content_block_start", "index": index,
                                     "content_block": {"type": "text", "text": ""}})
                        text_open = True
                    yield _anthropic_frame({"type": "content_block_delta", "index": index,
                                 "delta": {"type": "text_delta", "text": payload}})
                else:
                    message, quota = payload
                    if quota is not None:
                        # A free reading the answer carried; refresh the cache so the next
                        # request's ceiling check costs nothing.
                        app.state.quota_cache = (time.monotonic(), quota)
                    usage = message.get("usage") or usage
                    stop_reason = message.get("stop_reason") or "end_turn"
                    # One path, whatever was streamed. Finish the text the stream started (nothing,
                    # a prefix, or all of it), close it, then render the tool_use blocks — which
                    # only exist now that the parse has run on the complete answer. With nothing
                    # streamed this emits exactly the frames the fully-buffered path used to.
                    content = message.get("content") or []
                    tail = safe.remainder("".join(
                        b.get("text") or "" for b in content if b.get("type") == "text"))
                    if tail:
                        if not text_open:
                            yield _anthropic_frame({"type": "content_block_start", "index": index,
                                         "content_block": {"type": "text", "text": ""}})
                            text_open = True
                        yield _anthropic_frame({"type": "content_block_delta", "index": index,
                                     "delta": {"type": "text_delta", "text": tail}})
                    if text_open:
                        yield _anthropic_frame({"type": "content_block_stop", "index": index})
                        index, text_open = index + 1, False
                    frames, index = _anthropic_block_frames(
                        [b for b in content if b.get("type") == "tool_use"], index)
                    for frame in frames:
                        yield frame
        except Exception as exc:  # noqa: BLE001 — mirrors the OpenAI stream's terminal-error contract
            yield _anthropic_frame({"type": "error", "error": {
                "type": "cli_seat_error", "message": str(exc) or type(exc).__name__}})
            return
    yield _anthropic_frame({"type": "message_delta",
                  "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": usage})
    yield _anthropic_frame({"type": "message_stop"})


class _SafeText:
    """Answer text provably outside any tool call, so it can go out as it arrives.

    Both protocols ask the model to reply with a JSON object "and nothing else", and the parse
    (`cli_seat._json_objects`) finds a call ANYWHERE in the text — so from the first `{` on, nothing
    can be called prose until the answer ends and the parse has run. Everything before it can: no
    byte there can end up inside a call. That is the whole rule, and it is what lets a turn stream
    while still never printing raw tool JSON as prose.

    What this replaced keyed on the mere PRESENCE of tools, so a client that always sends a tool
    roster — Claude Code does — never saw a token until the turn was over. Measured on the prod
    grid: the gap to the second chunk equalled the whole request, 344s on an opus turn.

    Prose that legitimately contains `{` (a code block) stops streaming there and finishes in one
    burst. That is the old behaviour for that turn, never worse — safety before coverage.

    One class, both wires, because the rule must not drift between them (the same reason
    `_anthropic_block_frames` is shared).
    """

    def __init__(self, guarded: bool) -> None:
        self._guarded = guarded
        self._sent = ""
        self._stopped = False

    def feed(self, delta: str) -> str:
        """The part of `delta` safe to emit now — `""` once the rest must be held."""
        if self._stopped:
            return ""
        safe = delta
        if self._guarded:
            brace = delta.find("{")
            if brace >= 0:
                safe, self._stopped = delta[:brace], True
        self._sent += safe
        return safe

    def remainder(self, text: str) -> str:
        """What still needs emitting once the parse has run.

        The `startswith` guard is what keeps the client's total byte-for-byte equal to the final
        answer: it holds whenever the parse returned the text unchanged (the no-call case, which
        `parse_openai_tool_calls` returns UNSTRIPPED), and fails only on the whitespace `.strip()`
        removes when a call WAS found — where what already went out covers the prose and re-sending
        a recomputed span would double it.
        """
        return text[len(self._sent):] if text.startswith(self._sent) else ""


async def _stream_responses(app, prepared):
    """OpenAI Responses SSE: ``response.created``, ``output_text`` delta(s), ``response.completed``.

    Same streaming rule as the OpenAI/Anthropic streams, via the same ``_SafeText``: text before the
    first ``{`` goes out as it arrives, the rest waits for the parse — because only the parse knows
    which bytes were the call, and emitting those early is what let a client print raw tool JSON as
    prose AND run the tool. Tool-call items ride the terminal ``response.completed`` event's
    ``output[]``, never a delta (they cannot be known until the answer is whole).

    Reuses ``_anthropic_frame`` — the SSE framing (``event: <type>\\ndata: <json>``) is identical
    across the Anthropic and Responses dialects; only the event names differ.
    """
    spec, options = app.state.spec, app.state.options
    stream = cli_seat.answer_stream_response(spec, prepared, app.state.binary, options.timeout)
    safe = _SafeText(bool(prepared.tool_protocol))
    resp_id = f"resp_{uuid.uuid4().hex}"
    msg_id = f"msg_{uuid.uuid4().hex}"

    yield _anthropic_frame({"type": "response.created", "response": {
        "id": resp_id, "object": "response", "status": "in_progress",
        "model": prepared.model, "output": []}})

    def _open_text():
        return (
            _anthropic_frame({"type": "response.output_item.added", "output_index": 0,
                "item": {"id": msg_id, "type": "message", "status": "in_progress",
                         "role": "assistant", "content": []}}),
            _anthropic_frame({"type": "response.content_part.added",
                "output_index": 0, "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []}}),
        )

    text_open = False
    full_text = ""
    response_obj = None
    async with app.state.slots:
        try:
            while True:
                # next() blocks on the child's stdout, so it runs off the event loop like the
                # non-streaming call does.
                kind, payload = await run_in_threadpool(_next_or_none, stream)
                if kind is None:
                    break
                if kind == "delta":
                    payload = safe.feed(payload)
                    if not payload:
                        continue
                    if not text_open:
                        for frame in _open_text():
                            yield frame
                        text_open = True
                    full_text += payload
                    yield _anthropic_frame({"type": "response.output_text.delta",
                        "output_index": 0, "content_index": 0, "delta": payload})
                else:
                    response_obj, quota = payload
                    if quota is not None:
                        # A free reading the answer carried; refresh the cache so the next
                        # request's ceiling check costs nothing.
                        app.state.quota_cache = (time.monotonic(), quota)
                    # One path, whatever was streamed: emit only what the stream did not already
                    # send — nothing when it streamed the lot, the whole prose when it streamed
                    # none (a call-only answer leaves this empty and the text item never opens).
                    tail = safe.remainder(_responses_output_text(response_obj))
                    if tail:
                        if not text_open:
                            for frame in _open_text():
                                yield frame
                            text_open = True
                        full_text += tail
                        yield _anthropic_frame({"type": "response.output_text.delta",
                            "output_index": 0, "content_index": 0, "delta": tail})
        except Exception as exc:  # noqa: BLE001 — mirrors the OpenAI stream's terminal-error contract
            yield _anthropic_frame({"type": "response.failed", "response": {
                "id": resp_id, "object": "response", "status": "failed",
                "model": prepared.model, "output": [],
                "error": {"message": str(exc) or type(exc).__name__,
                          "code": "cli_seat_error"}}})
            return

    if text_open:
        yield _anthropic_frame({"type": "response.output_text.done",
            "output_index": 0, "content_index": 0, "text": full_text})
        yield _anthropic_frame({"type": "response.content_part.done",
            "output_index": 0, "content_index": 0,
            "part": {"type": "output_text", "text": full_text, "annotations": []}})
        yield _anthropic_frame({"type": "response.output_item.done",
            "output_index": 0,
            "item": {"id": msg_id, "type": "message", "status": "completed",
                     "role": "assistant",
                     "content": [{"type": "output_text", "text": full_text, "annotations": []}]}})

    completed = response_obj or {"id": resp_id, "object": "response", "status": "completed",
                                  "model": prepared.model, "output": [], "usage": {}}
    yield _anthropic_frame({"type": "response.completed", "response": completed})


def _responses_output_text(response_obj):
    """The assistant's text from a ``to_response`` object — the first ``output_text`` part on the
    message item, or "" when there is none (a tool-call-only answer)."""
    for item in (response_obj.get("output") or []) if isinstance(response_obj, dict) else []:
        if isinstance(item, dict) and item.get("type") == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "output_text":
                    return part.get("text") or ""
    return ""


def _anthropic_block_frames(blocks, start_index):
    """`content_block_start`/`delta`/`stop` for each text or tool_use block in `blocks`, indexed
    from `start_index`. Shared by the live-stream path above — which passes only the `tool_use`
    blocks, having already streamed the text — and the no-stream fallback below, which passes the
    whole answer, so a block is never rendered two different ways."""
    frames, index = [], start_index
    for block in blocks:
        kind = block.get("type")
        if kind == "text" and block.get("text"):
            frames.append(_anthropic_frame({"type": "content_block_start", "index": index,
                                  "content_block": {"type": "text", "text": ""}}))
            frames.append(_anthropic_frame({"type": "content_block_delta", "index": index,
                                  "delta": {"type": "text_delta", "text": block["text"]}}))
        elif kind == "tool_use":
            frames.append(_anthropic_frame({"type": "content_block_start", "index": index, "content_block": {
                "type": "tool_use", "id": block.get("id"), "name": block.get("name"), "input": {}}}))
            frames.append(_anthropic_frame({"type": "content_block_delta", "index": index, "delta": {
                "type": "input_json_delta",
                "partial_json": json.dumps(block.get("input") or {}, ensure_ascii=False)}}))
        else:
            continue
        frames.append(_anthropic_frame({"type": "content_block_stop", "index": index}))
        index += 1
    return frames, index


async def _quota(app: FastAPI, *, fresh: bool = False):
    """Quota, cached for `quota_ttl`, single-flight. Caches None too — a failed probe will fail
    again a second later, and re-paying seconds to rediscover that punishes the broken case."""
    options: SeatOptions = app.state.options
    ttl = max(0.0, options.quota_ttl)

    def _hit():
        cached = app.state.quota_cache
        if fresh or ttl <= 0 or cached is None:
            return None
        return cached if (time.monotonic() - cached[0]) < ttl else None

    if (cached := _hit()) is not None:
        return cached[1]
    async with app.state.quota_lock:
        # Re-check inside the lock: whoever held it may have just refreshed for us.
        if (cached := _hit()) is not None:
            return cached[1]
        snapshot = await run_in_threadpool(
            # A stalled probe must not freeze the seat for the minutes a real answer may take.
            cli_seat.probe_quota, app.state.spec, app.state.binary, min(30.0, options.timeout),
        )
        app.state.quota_cache = (time.monotonic(), snapshot)
        return snapshot


def _as_sse(completion: dict):
    """SSE-wrap a finished completion. NOT token-by-token — a CLI seat returns one whole answer,
    so time-to-first-token measured here is really time-to-last-token."""
    choice = (completion.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    base = {
        "id": completion.get("id") or f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion.chunk",
        "created": completion.get("created") or int(time.time()),
        "model": completion.get("model"),
    }
    delta: dict = {"role": "assistant"}
    if message.get("content"):
        delta["content"] = message["content"]
    if message.get("tool_calls"):
        delta["tool_calls"] = [
            {**call, "index": index} for index, call in enumerate(message["tool_calls"])
        ]
    yield _frame({**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
    yield _frame({
        **base,
        "choices": [{"index": 0, "delta": {}, "finish_reason": choice.get("finish_reason") or "stop"}],
        "usage": completion.get("usage"),
    })
    yield "data: [DONE]\n\n"


def _as_anthropic_sse(message: dict):
    """SSE-wrap a finished Anthropic message, for a seat that cannot stream incrementally.
    Reuses `_anthropic_block_frames` so this renders a block identically to `_stream_anthropic`'s
    buffered path."""
    msg_id = message.get("id") or f"msg_{uuid.uuid4().hex}"
    yield _anthropic_frame({"type": "message_start", "message": {
        "id": msg_id, "type": "message", "role": "assistant", "model": message.get("model"),
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": (message.get("usage") or {}).get("input_tokens", 0),
                  "output_tokens": 0}}})
    frames, _ = _anthropic_block_frames(message.get("content") or [], 0)
    for frame in frames:
        yield frame
    yield _anthropic_frame({"type": "message_delta",
                   "delta": {"stop_reason": message.get("stop_reason"), "stop_sequence": None},
                   "usage": message.get("usage")})
    yield _anthropic_frame({"type": "message_stop"})


def _as_responses_sse(response_obj):
    """SSE-wrap a finished Responses object, for a seat that cannot stream incrementally.
    Sends ``response.created`` then ``response.completed`` — the minimum a Responses SSE consumer
    needs, the complete output (text and any function-call items) riding the terminal event."""
    resp_id = response_obj.get("id") or f"resp_{uuid.uuid4().hex}"
    yield _anthropic_frame({"type": "response.created", "response": {
        "id": resp_id, "object": "response", "status": "in_progress",
        "model": response_obj.get("model"), "output": []}})
    yield _anthropic_frame({"type": "response.completed", "response": response_obj})


def _frame(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _anthropic_frame(payload: dict) -> str:
    """Anthropic SSE pairs a named `event:` line with its `data:` line, terminated by a blank
    line — the framing a switch-on-event-name consumer and any block-regrouping code are both
    written against. `<type>` is the event's own `type` field, so one function keeps every
    Anthropic frame (including the terminal error frame) honest about its own name."""
    return f"event: {payload['type']}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"




def _error(status: int, message: str, *, error_type: str = "cli_seat_error") -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": error_type}}, status_code=status)
