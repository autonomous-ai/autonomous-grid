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
