import threading

import httpx

from local.node_poll import run_one_cycle


def test_a_cycle_runs_the_engine_and_posts_the_result_back():
    posted: list[tuple[str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/grid/v1/poll":
            return httpx.Response(
                200,
                json={"transaction_id": "t1", "model": "m1", "stream": False, "body": {"model": "m1"}},
            )
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(200, json={"ok": True})
        posted.append((request.url.path, request.content))
        return httpx.Response(200, json={"cancelled": False})

    transport = httpx.MockTransport(handler)
    grid = httpx.Client(base_url="http://grid.invalid", transport=transport)
    engine = httpx.Client(base_url="http://127.0.0.1:18082", transport=transport)

    assert run_one_cycle(grid, engine, host_id="h1", models=("m1",), token="tok") is True
    assert posted and posted[0][0] == "/grid/v1/result/t1"


def test_a_cycle_with_no_work_reports_no_work_without_touching_the_engine():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/grid/v1/poll"
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    grid = httpx.Client(base_url="http://grid.invalid", transport=transport)
    engine = httpx.Client(base_url="http://127.0.0.1:18082", transport=transport)

    assert run_one_cycle(grid, engine, host_id="h1", models=("m1",), token="tok") is False


def test_the_engine_call_has_no_read_deadline():
    """Generation has no upper bound; a default deadline cuts real answers off mid-sentence.

    MEASURED on a live two-node grid: a 300-token answer from a 2B model at 22.6 tok/s was
    cancelled by httpx's five-second default, and llama.cpp logged `cancel task` after
    n_decoded=101. Every verification until then had asked for "GRID OK" -- six tokens, under a
    second -- so the whole class was invisible. The push path this replaces already used
    `httpx.Timeout(600, read=None)`; the pull path must not be stricter than the path it retires.
    """

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/grid/v1/poll":
            return httpx.Response(
                200,
                json={"transaction_id": "t1", "model": "m1", "stream": False, "body": {"model": "m1"}},
            )
        if request.url.path == "/v1/chat/completions":
            seen.update(request.extensions.get("timeout") or {})
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"cancelled": False})

    transport = httpx.MockTransport(handler)
    grid = httpx.Client(base_url="http://grid.invalid", transport=transport)
    engine = httpx.Client(base_url="http://127.0.0.1:18082", transport=transport)

    assert run_one_cycle(grid, engine, host_id="h1", models=("m1",), token="tok") is True
    assert seen.get("read") is None, f"engine read deadline was {seen.get('read')!r}"
    assert seen.get("connect") == 600


def test_a_streamed_job_pipes_the_engine_bytes_through_as_one_request():
    """Streaming is how every real client asks; `grid chat` not using it is what hid this.

    MEASURED before the fix: the same prompt returned {"content": "GRID OK"} without `stream`
    and 0 bytes with it, twice. node_poll had no streaming branch at all, so the SSE body was
    posted as one blob to /result and the stream was never ended.

    Mirrors remote/serve.py::_forward_stream, the shape already proven in remote mode: read the
    engine with `client.stream` and hand its byte iterator straight to the submit as the request
    BODY -- one request, not one per chunk -- so bytes reach the consumer while the engine is
    still writing.
    """

    engine_sse = [b'data: {"choices":[{"delta":{"content":"GRID"}}]}\n\n',
                  b'data: {"choices":[{"delta":{"content":" OK"}}]}\n\n',
                  b"data: [DONE]\n\n"]
    posts: list[tuple[str, bytes, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/grid/v1/poll":
            return httpx.Response(
                200,
                json={"transaction_id": "t1", "model": "m1", "stream": True,
                      "body": {"model": "m1", "stream": True}},
            )
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(200, stream=httpx.ByteStream(b"".join(engine_sse)),
                                  headers={"content-type": "text/event-stream"})
        posts.append((request.url.path, request.read(), request.headers.get("content-type", "")))
        return httpx.Response(200, json={"cancelled": False})

    transport = httpx.MockTransport(handler)
    grid = httpx.Client(base_url="http://grid.invalid", transport=transport)
    engine = httpx.Client(base_url="http://127.0.0.1:18082", transport=transport)

    assert run_one_cycle(grid, engine, host_id="h1", models=("m1",), token="tok") is True
    # ONE submit carrying the whole SSE body, not one request per chunk.
    assert [path for path, _body, _ct in posts] == ["/grid/v1/result/t1"]
    assert posts[0][1] == b"".join(engine_sse)
    assert posts[0][2] == "text/event-stream"


def test_the_poll_loop_survives_a_transport_error_and_keeps_polling():
    """A dead poll loop is invisible: heartbeats continue and the residency still reads ready.

    MEASURED on the live grid: restarting the grid cut the worker's in-flight long-poll, the
    thread raised httpx.RemoteProtocolError out of an unguarded `while` and died, and that node
    then served nothing for the rest of its life while every health signal stayed green -- 70
    seconds with no poll at all, and it would have been forever.

    remote/serve.py::_poll_loop has always caught this ("retrying...", a 2s backoff, continue);
    this is the same contract for the local worker.
    """

    from local.allocator_node import _poll_until_stopped

    calls: list[int] = []
    stop = threading.Event()

    def cycle() -> bool:
        calls.append(len(calls))
        if len(calls) == 1:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        if len(calls) >= 3:
            stop.set()
        return False

    _poll_until_stopped(cycle, stop, backoff_seconds=0.0)

    assert len(calls) >= 3, "the loop stopped at the first transport error"


def test_a_joined_engine_polls_by_node_id_with_no_token():
    """`grid join` has no allocator token, because registering never required one."""

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/grid/v1/poll":
            seen["params"] = dict(request.url.params)
            seen["node_header"] = request.headers.get("x-grid-node-id")
            seen["token_header"] = request.headers.get("x-grid-allocator-node-token")
            return httpx.Response(204)
        raise AssertionError(f"unexpected {request.url.path}")

    transport = httpx.MockTransport(handler)
    grid = httpx.Client(base_url="http://grid.invalid", transport=transport)
    engine = httpx.Client(base_url="http://192.168.1.9:11434", transport=transport)

    assert run_one_cycle(grid, engine, node_id="joined-1", models=("m1",), token="") is False
    assert seen["params"] == {"node_id": "joined-1", "models": "m1"}
    assert seen["node_header"] == "joined-1"
    # No empty allocator token: the grid reads a blank one as a failed proof, not as absence.
    assert seen["token_header"] is None
