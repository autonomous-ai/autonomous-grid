"""OpenAI API for a CLI seat: grid ──/chat/completions──▶ here ──subprocess──▶ `<cli> -p …`

Loopback-only — this process can spend the operator's subscription. CLI-agnostic: the driver
arrives as a `SeatSpec`, so a second CLI reuses this server unchanged.
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
    *, spec: SeatSpec, binary: str, options: SeatOptions, transcript: str | None = None
) -> FastAPI:
    app = FastAPI(
        title=f"Grid CLI Seat ({spec.label})",
        description=f"Engine-local OpenAI API backed by the {spec.label} CLI.",
        version="0.1.0",
    )
    app.state.spec = spec
    app.state.binary = binary
    app.state.options = options
    app.state.transcript = transcript
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
        return await _handle(app, request)

    return app


async def _handle(app: FastAPI, request: Request):
    spec: SeatSpec = app.state.spec
    options: SeatOptions = app.state.options
    raw = await request.body()
    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return _error(400, "Request body is not valid JSON.")
    if not isinstance(body, dict):
        return _error(400, "Request body must be a JSON object.")

    # Resolve once — the transcript logs the same prompt the subprocess gets.
    try:
        prepared = await run_in_threadpool(cli_seat.prepare, body, spec.kind)
    except cli_seat.SeatBadRequest as exc:
        return _error(400, str(exc))

    # Before the semaphore: the probe spends no allowance, so it must not hold the spend slot.
    if options.session_limit is not None or options.week_limit is not None:
        snapshot = await _quota(app)
        refusal = cli_seat.quota_refusal(snapshot, options)
        await _log_turn(app, "quota", {
            "known": snapshot is not None,
            "session_pct": snapshot.session_pct if snapshot else None,
            "week_pct": snapshot.week_pct if snapshot else None,
            "refused": refusal is not None,
        })
        if refusal is not None:
            return _error(429, refusal, error_type="quota_exhausted")

    await _log_turn(app, "request", {
        "model": prepared.model,
        "stream": bool(body.get("stream")),
        "messages": body.get("messages"),
        "tools": body.get("tools"),
        # System prompt by LENGTH, not verbatim — with native tools[] it repeats the schema
        # already logged above, three copies per request on an append-only file.
        "prompt": prepared.prompt,
        "system_prompt_chars": len(prepared.system_prompt),
        "system_prompt_has_hermes_block": "<tool_call>" in prepared.system_prompt,
    })

    if body.get("stream") and cli_seat.can_stream(spec):
        return StreamingResponse(_stream(app, prepared), media_type="text/event-stream")

    async with app.state.slots:
        try:
            # A CLI run is a blocking subprocess of minutes. Off the event loop, or one request
            # would freeze health checks and every other caller.
            completion = await run_in_threadpool(
                cli_seat.answer, spec, prepared, app.state.binary, options.timeout,
            )
        except cli_seat.SeatBadRequest as exc:
            await _log_turn(app, "error", {"status": 400, "message": str(exc)})
            return _error(400, str(exc))
        except cli_seat.SeatError as exc:
            await _log_turn(app, "error", {"status": 502, "message": str(exc)})
            return _error(502, str(exc))

    # Outside the slot: logging and serialising an answer costs the next caller nothing.
    await _log_turn(app, "response", completion)
    if body.get("stream"):
        return StreamingResponse(_as_sse(completion), media_type="text/event-stream")
    return JSONResponse(completion)


async def _stream(app: FastAPI, prepared):
    """SSE straight from the CLI's own output, chunk by chunk as it arrives.

    A failure mid-stream cannot re-status the response — the headers are long gone — so it is
    emitted as a terminal SSE error frame, which is what an OpenAI-shaped client understands.
    """
    spec, options = app.state.spec, app.state.options
    stream = cli_seat.answer_stream(spec, prepared, app.state.binary, options.timeout)
    base = {"id": f"chatcmpl-{uuid.uuid4().hex}", "object": "chat.completion.chunk",
            "created": int(time.time()), "model": prepared.model}
    first = True
    async with app.state.slots:
        try:
            while True:
                # next() blocks on the child's stdout, so it runs off the event loop like the
                # non-streaming call does.
                kind, payload = await run_in_threadpool(_next_or_none, stream)
                if kind is None:
                    break
                if kind == "delta":
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
                    # Tool calls only exist once the whole answer is parsed, so they ride the
                    # final chunk rather than being invented mid-stream.
                    tail = {"tool_calls": [{**c, "index": i} for i, c in
                                           enumerate(message["tool_calls"])]} if message.get("tool_calls") else {}
                    yield _frame({**base, "choices": [
                        {"index": 0, "delta": tail,
                         "finish_reason": choice.get("finish_reason") or "stop"}],
                        "usage": payload.get("usage"), "grid_cli_seat": payload.get("grid_cli_seat")})
                    await _log_turn(app, "response", payload)
        except cli_seat.SeatError as exc:
            await _log_turn(app, "error", {"status": 502, "message": str(exc), "streamed": True})
            yield _frame({"error": {"message": str(exc), "type": "cli_seat_error"}})
    yield "data: [DONE]\n\n"


def _next_or_none(stream):
    try:
        return next(stream)
    except StopIteration:
        return None, None


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


def _frame(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _log_turn(app: FastAPI, kind: str, payload: dict) -> None:
    """Append one JSONL record off the event loop. Never raises — a full disk must not cost the
    caller their answer."""
    path = getattr(app.state, "transcript", None)
    if not path:
        return
    record = {"ts": time.time(), "kind": kind, **payload}
    await run_in_threadpool(_append_jsonl, path, record)


def _append_jsonl(path: str, record: dict) -> None:
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:  # noqa: BLE001 — logging must never break serving
        pass


def _error(status: int, message: str, *, error_type: str = "cli_seat_error") -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": error_type}}, status_code=status)
