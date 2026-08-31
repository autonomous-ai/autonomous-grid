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
    from remote import credentials

    from . import remote_grid

    session = credentials.require_session()
    rec = remote_grid._select(getattr(args, "grid", None))
    network_id = remote_grid._network_id(rec)
    label = rec.get("name") or network_id
    base, _status = remote_grid.resolve_relay_base(session, rec, network_id, label)
    token = str(rec.get("access_token") or "")  # public route — token optional
    return fetch_overview(base, token, str(label))


def fetch_overview(base: str, token: str, label: str) -> dict[str, Any]:
    """The public overview at ``base``, or a clean ``SystemExit`` naming grid ``label``.

    Split out of ``_fetch_overview`` so a caller that has already resolved its grid — `grid launch`'s
    preflight (ADR 0028) — reads the grid through *this* code path rather than a second copy that
    could drift away from it. The guards below are the trust boundary: the body is whatever the relay
    returned, so a non-JSON 2xx or a non-dict envelope becomes a message, never a traceback.
    """
    from remote import relay

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


def live_model_names(overview: dict[str, Any]) -> tuple[str, ...]:
    """Every model id the grid currently serves, first-seen order, deduped across engines.

    The same reading ``cmd_remote_models`` renders, through the same defended readers — so a malformed
    ``nodes`` or a node whose ``models`` isn't a list degrades to empty here exactly as it does there.

    ``auto`` is not included even when the grid has routing enabled: it is the reserved name that
    never matches an engine-advertised model (CONTEXT-MAP.md), so it can never be what a caller is
    asking about when it asks which models exist.
    """
    return tuple(dict.fromkeys(
        model for node in _nodes_from(overview) for model in _node_models(node, overview)
    ))


def _model_case_map(overview: dict[str, Any]) -> dict[str, str]:
    """``casefold(alias) -> alias``, read from the overview's top-level model catalog.

    That list is the ONE place the relay reports a model's id in its true case; every node's own
    ``models`` array is lowercased for display (grid-leave issue: reproduced live serving
    `Qwen3.5-2B-Q4_K_M`, listed under a node as `qwen3.5-2b-q4_k_m`, and rejected verbatim when
    copied back into `grid chat -m`). Correcting it here, once, fixes it everywhere this renders —
    `grid engines`, `grid models`, and `grid launch`'s preflight (`live_model_names`) all read
    through this same function now, so none of them can show a name the grid won't answer to."""
    entries = overview.get("models")
    if not isinstance(entries, list):
        return {}
    candidates: dict[str, set[str]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        model_id = entry["id"]
        aliases = (model_id, model_id[:-5]) if model_id.casefold().endswith(".gguf") else (model_id,)
        for alias in aliases:
            candidates.setdefault(alias.casefold(), set()).add(alias)
    # A case-insensitive collision is not enough evidence to choose one case-sensitive model.
    # Leave that node spelling untouched instead of silently routing it to an arbitrary peer.
    return {
        folded: next(iter(aliases))
        for folded, aliases in candidates.items()
        if len(aliases) == 1
    }


def _node_models(node: dict[str, Any], overview: dict[str, Any] | None = None) -> list[str]:
    """A node's served model ids as strings (defends against a non-list ``models`` or non-string
    items — otherwise ``",".join`` would split a bare string into characters or raise ``TypeError``).

    Corrected against ``overview``'s true-case list when given; a caller that already has the whole
    overview in scope should always pass it — the raw, lowercased id is never what should render."""
    models = node.get("models")
    if not isinstance(models, list):
        return []
    case_map = _model_case_map(overview) if overview is not None else {}
    return [case_map.get(str(model).casefold(), str(model)) for model in models]


def _node_responses_models(
    node: dict[str, Any], overview: dict[str, Any] | None = None
) -> set[str]:
    """The subset of a node's served models that serve the Responses dialect (issue 10), read from
    the overview's ``responses_models`` and str-coerced to match ``_node_models``' ids. Defends
    against a non-list field, non-string items, and an older master that omits it entirely — each
    yields an empty set, so a capability is never falsely shown (graceful degradation, fail-closed)."""
    models = node.get("responses_models")
    if not isinstance(models, list):
        return set()
    case_map = _model_case_map(overview) if overview is not None else {}
    return {case_map.get(str(model).casefold(), str(model)) for model in models}


def cmd_remote_engines(args: argparse.Namespace) -> int:
    """`grid engines` (remote): the live engines (nodes) joined to the active grid."""
    overview = _fetch_overview(args)
    nodes = _nodes_from(overview)

    if getattr(args, "json", False):
        print(json.dumps(nodes, indent=2))  # passthrough of each node object — forward-compatible
        return 0

    if not nodes:
        print("(no engines — `grid join` one first)")
        return 0

    raw_names = [str(n.get("name") or "") for n in nodes]
    # `--name` at join is never enforced unique across DIFFERENT machines on the same grid (only
    # this machine's own `grid leave` collision check is — cli/provider.py:414), so two members can
    # genuinely show up with an identical NODE label. The overview carries `provider_email` per
    # node precisely to tell them apart, but until now it only ever surfaced via `--json` — the
    # plain table had nothing to distinguish two "this-computer" rows. Appended ONLY on an actual
    # collision, so the common (all-unique) case stays exactly as terse as before — a column that is
    # always full of everyone's email would be noise on every normal, non-colliding grid.
    dupes = {name for name in raw_names if raw_names.count(name) > 1 and name}
    names = [
        f"{name} ({n.get('provider_email')})" if name in dupes and n.get("provider_email") else name
        for name, n in zip(raw_names, nodes)
    ]
    engines = [str(n.get("engine") or "") for n in nodes]
    devices = [str(n.get("device") or "") for n in nodes]
    nwidth = max(len("NODE"), *(len(x) for x in names))
    ewidth = max(len("ENGINE"), *(len(x) for x in engines))
    dwidth = max(len("DEVICE"), *(len(x) for x in devices))
    print(f"{'NODE':<{nwidth}}  {'ENGINE':<{ewidth}}  {'DEVICE':<{dwidth}}  TOK/S")
    for name, node in zip(names, nodes):
        engine = str(node.get("engine") or "")
        device = str(node.get("device") or "")
        tok_s = node.get("throughput_tok_s")
        # bool is an int subclass — exclude it so `throughput_tok_s: true` shows "-", not "1".
        tok = f"{tok_s:g}" if isinstance(tok_s, (int, float)) and not isinstance(tok_s, bool) else "-"
        models = ",".join(_node_models(node, overview)) or "(none)"
        print(f"{name:<{nwidth}}  {engine:<{ewidth}}  {device:<{dwidth}}  {tok}")
        print(f"{'':<{nwidth}}  models: {models}")
    return 0


def cmd_remote_models(args: argparse.Namespace) -> int:
    """`grid models` (remote): the models served across the active grid's live engines, plus the
    reserved ``auto`` model when the grid has auto-routing enabled (mirrors ``GET /relay/v1/models``).

    Each engine row carries whether it serves the model via the Responses dialect (issue 10), read
    per-engine from the overview's ``responses_models``; shown in ``-v`` and ``--json`` (an older
    master omits the field → nothing shown). The plain listing stays bare model ids for scripting."""
    overview = _fetch_overview(args)
    nodes = _nodes_from(overview)
    rows: list[tuple[str, str, str, bool]] = []
    for node in nodes:
        engine = str(node.get("engine") or "")
        name = str(node.get("name") or "")
        capable = _node_responses_models(
            node, overview
        )  # resolved once per node, not per served model
        for model in _node_models(node, overview):
            rows.append((model, engine, name, model in capable))
    # When auto routing is enabled, advertise the reserved `auto` model FIRST — same as the relay's
    # /relay/v1/models endpoint (owner `grid-router`), so it shows even when zero engines are joined.
    # An older master whose overview lacks the field reports falsy → no auto row (graceful degradation).
    # `responses` is False for `auto`: dialect-reachability is a per-request routing outcome, not a
    # static property of the reserved model (no AC covers it) — a real model's badge is its engine's.
    if overview.get("router_enabled"):
        rows.insert(0, ("auto", "grid-router", "", False))

    if getattr(args, "json", False):
        # Derived view (not a raw passthrough like engines): new API fields on a model entry
        # won't surface here. `responses` is this engine's dialect capability (issue 10).
        print(json.dumps(
            [{"model": model, "engine": engine, "node": node, "responses": serves}
             for model, engine, node, serves in rows],
            indent=2,
        ))
        return 0

    if not rows:
        print("(no live models — `grid join` an engine first)")
        return 0

    seen = list(dict.fromkeys(model for model, *_ in rows))  # order-preserving dedup
    # Prefer a real model over the reserved `auto` here: `auto` is always inserted first when
    # routing is on, and a newcomer who just joined an engine wants to see THAT model chat-tested,
    # not the router alias. Mirrors the local `cmd_models`' closing-the-loop hint (issue: `grid
    # join`'s own "still loading" message can't yet promise a working model — this can).
    target = next((m for m in seen if m != "auto"), seen[0])

    if getattr(args, "verbose", False):
        mwidth = max(len("MODEL"), *(len(model) for model, _, _, _ in rows))
        ewidth = max(len("ENGINE"), *(len(engine) for _, engine, _, _ in rows))
        print(f"{'MODEL':<{mwidth}}  {'ENGINE':<{ewidth}}  NODE")
        for model, engine, node, serves in rows:
            # `responses` joins the line as a trailing capability field — issue 06's intent (annotate
            # the line, not add a column); shown only when this engine serves the dialect, else the
            # row ends at NODE.
            trailer = "  responses" if serves else ""
            print(f"{model:<{mwidth}}  {engine:<{ewidth}}  {node}{trailer}")
        from . import provider

        provider.print_models_hint(target)
        return 0

    for model in seen:
        print(model)
    from . import provider

    provider.print_models_hint(target)
    return 0
