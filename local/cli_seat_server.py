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

    answer_fn = cli_seat.answer_anthropic if wire == "anthropic" else cli_seat.answer
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
        sse = _as_anthropic_sse(completion) if wire == "anthropic" else _as_sse(completion)
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
    spec, options = app.state.spec, app.state.options
    stream = cli_seat.answer_stream(spec, prepared, app.state.binary, options.timeout)
    base = {"id": f"chatcmpl-{uuid.uuid4().hex}", "object": "chat.completion.chunk",
            "created": int(time.time()), "model": prepared.model}
    first = True
    # With a tool protocol in play, nothing is streamed as it arrives: the raw `{"tool_calls": …}`
    # would go out as prose AND come back as a parsed call, so the client printed the JSON and ran
    # the tool. Only the parse knows which bytes were the call, and it cannot run until the answer
    # ends — so the whole thing waits, and `content` is sent once, below.
    buffered = bool(prepared.tool_protocol)
    async with app.state.slots:
        try:
            while True:
                # next() blocks on the child's stdout, so it runs off the event loop like the
                # non-streaming call does.
                kind, payload = await run_in_threadpool(_next_or_none, stream)
                if kind is None:
                    break
                if kind == "delta":
                    if buffered:
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
                    text = message.get("content") or ""
                    if buffered and text:
                        # The prose the parse left behind — the whole answer minus any call.
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

    Same buffering rule as the OpenAI stream above: with a tool protocol in play nothing goes out
    until the CLI stops writing, because only the parse knows which bytes were the call — emitting
    text deltas early is what let a client print the raw tool JSON as prose AND run the tool.
    """
    spec, options = app.state.spec, app.state.options
    stream = cli_seat.answer_stream_anthropic(spec, prepared, app.state.binary, options.timeout)
    buffered = bool(prepared.tool_protocol)
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
                    if buffered:
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
                    if buffered:
                        # Nothing went out above — the whole answer, text and any tool_use block
                        # alike, is only known now that the parse has run on the complete text.
                        frames, index = _anthropic_block_frames(message.get("content") or [], index)
                        for frame in frames:
                            yield frame
                    elif text_open:
                        yield _anthropic_frame({"type": "content_block_stop", "index": index})
        except Exception as exc:  # noqa: BLE001 — mirrors the OpenAI stream's terminal-error contract
            yield _anthropic_frame({"type": "error", "error": {
                "type": "cli_seat_error", "message": str(exc) or type(exc).__name__}})
            return
    yield _anthropic_frame({"type": "message_delta",
                  "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": usage})
    yield _anthropic_frame({"type": "message_stop"})


def _anthropic_block_frames(blocks, start_index):
    """`content_block_start`/`delta`/`stop` for each text or tool_use block in `blocks`, indexed
    from `start_index`. Shared by the buffered live-stream path above and the no-stream fallback
    below, so a block is never rendered two different ways."""
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
