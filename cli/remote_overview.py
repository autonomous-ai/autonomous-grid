"""Remote-mode `grid engines` / `grid models`: list the live engines and models of the active
remote grid from the public ``GET /relay/v1/grid/overview`` read model.

Mirrors the local handlers (`cli/provider.cmd_engines` / `cmd_models`) — same verbs, same shape of
output — but reads the hosted relay's overview instead of the local grid's ``/nodes/discover``.

The overview route is **public** (no auth), so this resolves the relay base from a signed-in
session + ``network_id`` only (`remote_grid.resolve_relay_base`) and does **not** require a per-grid
access token: listing works even before ``grid sync`` stores one after ``grid up``. The token is
sent as Bearer when present and ignored by the public route. A stopped grid raises the same
"isn't up; run `grid up`" error as every other relay command.

The renderers defend against a malformed/partial payload (the body crosses a trust boundary): a
non-JSON 2xx, a non-dict envelope, or a node whose ``nodes``/``models`` aren't the expected lists
degrade to a clean message or empty output rather than a traceback.

Import rule mirrors `cli/remote_request.py`: `remote.*` and the remote-specific `cli` siblings are
imported lazily inside the fetch helper, because `cli.dispatch` imports this module while the `cli`
package is still initialising.
"""
from __future__ import annotations

import argparse
import json
from typing import Any

import httpx


# The overview is a small read; the engines/models parsers have no `--timeout`, and
# `open_consumer_client(..., *, timeout=...)` requires the kwarg — so bound it with a constant.
_OVERVIEW_TIMEOUT = 30.0


def _fetch_overview(args: argparse.Namespace) -> dict[str, Any]:
    """The active remote grid's ``/relay/v1/grid/overview`` payload, or a clean ``SystemExit``.

    Lighter than the consumer ``remote_request._resolve``: the overview is public, so this needs only
    a signed-in session and a resolvable relay base (no access-token gate).
    """
    from remote import credentials, relay

    from . import remote_grid

    session = credentials.require_session()
    rec = remote_grid._select(getattr(args, "grid", None))
    network_id = remote_grid._network_id(rec)
    label = rec.get("name") or network_id
    base, _status = remote_grid.resolve_relay_base(session, rec, network_id, label)
    token = str(rec.get("access_token") or "")  # public route — token optional
    try:
        with relay.open_consumer_client(base, token, timeout=_OVERVIEW_TIMEOUT) as client:
            resp = client.get("/relay/v1/grid/overview")
    except httpx.RequestError as exc:
        raise SystemExit(f"Could not reach grid {label}: {exc}") from exc
    if resp.status_code >= 400:
        raise SystemExit(f"Grid {label} overview failed ({resp.status_code}): {resp.text[:200]}")
    try:
        data = resp.json()
    except ValueError as exc:  # a non-JSON 2xx body (e.g. a proxy error / maintenance page)
        raise SystemExit(f"Grid {label} returned a non-JSON overview: {resp.text[:200]}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"Grid {label} returned an unexpected overview shape.")
    return data


def _nodes_from(overview: dict[str, Any]) -> list[dict[str, Any]]:
    """The live engine nodes in an already-fetched overview — only well-formed object entries, so a
    malformed ``nodes`` field (non-list, or a list with scalar junk) renders as empty, never crashes."""
    nodes = overview.get("nodes")
    if not isinstance(nodes, list):
        return []
    return [node for node in nodes if isinstance(node, dict)]


def _nodes(args: argparse.Namespace) -> list[dict[str, Any]]:
    """The active remote grid's live engine nodes (fetches the overview)."""
    return _nodes_from(_fetch_overview(args))


def _node_models(node: dict[str, Any]) -> list[str]:
    """A node's served model ids as strings (defends against a non-list ``models`` or non-string
    items — otherwise ``",".join`` would split a bare string into characters or raise ``TypeError``)."""
    models = node.get("models")
    if not isinstance(models, list):
        return []
    return [str(model) for model in models]


def cmd_remote_engines(args: argparse.Namespace) -> int:
    """`grid engines` (remote): the live engines (nodes) joined to the active grid."""
    nodes = _nodes(args)

    if getattr(args, "json", False):
        print(json.dumps(nodes, indent=2))  # passthrough of each node object — forward-compatible
        return 0

    if not nodes:
        print("(no engines — `grid join` one first)")
        return 0

    names = [str(n.get("name") or "") for n in nodes]
    engines = [str(n.get("engine") or "") for n in nodes]
    devices = [str(n.get("device") or "") for n in nodes]
    nwidth = max(len("NODE"), *(len(x) for x in names))
    ewidth = max(len("ENGINE"), *(len(x) for x in engines))
    dwidth = max(len("DEVICE"), *(len(x) for x in devices))
    print(f"{'NODE':<{nwidth}}  {'ENGINE':<{ewidth}}  {'DEVICE':<{dwidth}}  TOK/S")
    for node in nodes:
        name = str(node.get("name") or "")
        engine = str(node.get("engine") or "")
        device = str(node.get("device") or "")
        tok_s = node.get("throughput_tok_s")
        # bool is an int subclass — exclude it so `throughput_tok_s: true` shows "-", not "1".
        tok = f"{tok_s:g}" if isinstance(tok_s, (int, float)) and not isinstance(tok_s, bool) else "-"
        models = ",".join(_node_models(node)) or "(none)"
        print(f"{name:<{nwidth}}  {engine:<{ewidth}}  {device:<{dwidth}}  {tok}")
        print(f"{'':<{nwidth}}  models: {models}")
    return 0


def cmd_remote_models(args: argparse.Namespace) -> int:
    """`grid models` (remote): the models served across the active grid's live engines, plus the
    reserved ``auto`` model when the grid has auto-routing enabled (mirrors ``GET /relay/v1/models``)."""
    overview = _fetch_overview(args)
    nodes = _nodes_from(overview)
    rows = [
        (model, str(node.get("engine") or ""), str(node.get("name") or ""))
        for node in nodes
        for model in _node_models(node)
    ]
    # When auto routing is enabled, advertise the reserved `auto` model FIRST — same as the relay's
    # /relay/v1/models endpoint (owner `grid-router`), so it shows even when zero engines are joined.
    # An older master whose overview lacks the field reports falsy → no auto row (graceful degradation).
    if overview.get("router_enabled"):
        rows.insert(0, ("auto", "grid-router", ""))

    if getattr(args, "json", False):
        # Derived view (not a raw passthrough like engines): new API fields on a model entry
        # won't surface here.
        print(json.dumps(
            [{"model": model, "engine": engine, "node": node} for model, engine, node in rows],
            indent=2,
        ))
        return 0

    if not rows:
        print("(no live models — `grid join` an engine first)")
        return 0

    if getattr(args, "verbose", False):
        mwidth = max(len("MODEL"), *(len(model) for model, _, _ in rows))
        ewidth = max(len("ENGINE"), *(len(engine) for _, engine, _ in rows))
        print(f"{'MODEL':<{mwidth}}  {'ENGINE':<{ewidth}}  NODE")
        for model, engine, node in rows:
            print(f"{model:<{mwidth}}  {engine:<{ewidth}}  {node}")
        return 0

    for model in dict.fromkeys(model for model, _, _ in rows):  # order-preserving dedup
        print(model)
    return 0
