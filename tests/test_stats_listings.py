"""`grid stats --json` hands out `grid engines --json` and `grid models --json` from its one overview read.

The harness's Model Manager viewer polls a grid every few seconds and draws all three views. Asked as
three commands, each resolved the grid and fetched the overview for itself: three reads of one payload,
and three `grid` processes, every poll. ``listings`` carries the other two views out of the read `stats`
already made, spelled exactly as their own commands print them, so the viewer can stop asking.
"""
from __future__ import annotations

import json

import httpx

import cli
from tests._remote_seed import seed_remote_grid

_OVERVIEW = {
    "grid": {"state": "running"},
    "router_enabled": True,
    "stats": {"models": 2, "nodes": 2, "concurrent_capacity": 3, "uptime_pct": 99.9},
    # The curated list is where a node's lower-cased id gets its real spelling back.
    "models": [{"id": "GLM-5.2"}, {"id": "Qwen-3"}],
    "nodes": [
        {"name": "mac-studio", "device": "Mac Studio", "engine": "MLX", "memory_gb": 192,
         "models": ["glm-5.2", "qwen-3"], "responses_models": ["glm-5.2"],
         "throughput_tok_s": 58.0, "max_concurrency": 2, "online": True},
        {"name": "ollama-box", "device": "RTX 4090", "engine": "ollama", "vram_gb": 24,
         "models": ["qwen-3"], "throughput_tok_s": 90.0, "max_concurrency": 1, "online": True},
    ],
}


def _relay(monkeypatch, _real=httpx.Client) -> list[str]:
    """Serve the overview through a MockTransport; returns the path of every request it received."""
    from remote import relay

    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/relay/v1/grid/overview":
            return httpx.Response(200, json=_OVERVIEW)
        return httpx.Response(404, json={"detail": "Not Found"})

    monkeypatch.setattr(
        relay.httpx, "Client",
        lambda *a, **k: _real(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )
    return paths


def _json(capsys, argv: list[str]):
    assert cli.main(argv) == 0
    return json.loads(capsys.readouterr().out)


def test_stats_lists_the_engines_and_models_exactly_as_their_own_commands_print_them(
    monkeypatch, tmp_path, capsys
):
    seed_remote_grid(monkeypatch, tmp_path)
    _relay(monkeypatch)

    engines = _json(capsys, ["engines", "--no-wake", "--json"])
    models = _json(capsys, ["models", "--no-wake", "--json"])
    stats = _json(capsys, ["stats", "--no-wake", "--json"])

    assert stats["listings"] == {"engines": engines, "models": models}
    # Not vacuous: the model listing restored the curated spelling and put the router family first.
    assert [row["model"] for row in models][:1] == ["auto"] and "GLM-5.2" in {r["model"] for r in models}


def test_stats_reads_the_overview_once_for_all_three_views(monkeypatch, tmp_path, capsys):
    seed_remote_grid(monkeypatch, tmp_path)
    paths = _relay(monkeypatch)

    stats = _json(capsys, ["stats", "--no-wake", "--json"])

    assert paths.count("/relay/v1/grid/overview") == 1
    assert stats["listings"]["engines"] and stats["listings"]["models"]


def test_the_rollup_keeps_its_own_keys_beside_the_listings(monkeypatch, tmp_path, capsys):
    """`models` stays the rollup's COUNT and `engines` its cards: a script reading either sees no change."""
    seed_remote_grid(monkeypatch, tmp_path)
    _relay(monkeypatch)

    stats = _json(capsys, ["stats", "--json"])

    assert stats["models"] == 2
    assert {card["engine"] for card in stats["engines"]} == {"mac-studio", "ollama-box"}


def test_the_text_stats_do_not_print_the_listings(monkeypatch, tmp_path, capsys):
    seed_remote_grid(monkeypatch, tmp_path)
    _relay(monkeypatch)

    assert cli.main(["stats"]) == 0

    assert "listings" not in capsys.readouterr().out
