"""Remote-mode `grid engines` / `grid models`: list the live engines and models of the active
remote grid from the public ``GET /relay/v1/grid/overview`` read model.

Mirrors the local handlers (`cli/provider.cmd_engines` / `cmd_models`) — same verbs, same shape of
output — but reads the hosted relay's overview instead of the local grid's ``/nodes/discover``.

The overview route is **public** (no auth), so this resolves the relay base from a signed-in
session + ``network_id`` only (`remote_grid.resolve_relay_base`) and does **not** require a per-grid
access token: listing works even before ``grid sync`` stores one after ``grid start``. The token is
sent as Bearer when present and ignored by the public route. A stopped grid raises the same
"isn't up; run `grid start`" error as every other relay command.

⚠️ **The Bearer is not ignored by the control plane's PROXY**: a signed-in read of a SLEEPING grid wakes
it (grid-apis `wake_routes`, `idle-sleep` issue 04). ``--no-wake`` (:data:`NO_WAKE_FLAG`,
grid-reads-without-waking issue 01) sends none, so a sleeping grid answers ``503 grid_asleep`` at once
and is not started — and when the owner status already says ``asleep``, nothing is sent at all. The
harness's Model Manager viewer passes it on every automatic read; `grid stats` takes it too
(`cli/remote_stats`). Either way the asleep answer is a refusal whose ``--json`` code is ``grid_asleep``.

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
from dataclasses import dataclass
from typing import Any

import httpx


# The overview is a small read; the engines/models parsers have no `--timeout`, and
# `open_consumer_client(..., *, timeout=...)` requires the kwarg — so bound it with a constant.
_OVERVIEW_TIMEOUT = 30.0

#: The flag that reads a grid without waking it. ⚠️ **A cross-repo literal**: the harness's bundled Model
#: Manager viewer passes it on every automatic `engines`/`models`/`stats` call, and its daemon on a
#: fallback read — renamed here, argparse refuses it with exit 2 and the viewer shows a stale reading.
#: Loud, never a wake. See the lockstep register.
NO_WAKE_FLAG = "--no-wake"

#: The master's two public reads, under a grid's relay base. ⚠️ Cross-repo literals (grid-src's routes,
#: grid-apis' route rules and its sleep record); pinned by `tests/test_grid_reads_lockstep.py`.
OVERVIEW_PATH = "/relay/v1/grid/overview"
DISCOVER_PATH = "/nodes/discover"


@dataclass(frozen=True)
class ReadTarget:
    """The grid a read acts on, and the credential it goes out with."""

    session: str
    record: dict[str, Any]
    network_id: str
    label: str
    base: str
    #: The per-grid token, or ``""`` under ``--no-wake`` — then no ``Authorization`` is sent at all.
    token: str


def read_target(args: argparse.Namespace) -> ReadTarget:
    """Resolve the grid ``grid models | engines | stats`` read, or a clean ``SystemExit``.

    Lighter than the consumer ``remote_request._resolve``: the overview is public, so this needs only
    a signed-in session and a resolvable relay base (no access-token gate).

    Under ``--no-wake`` the read carries no credential, and an owner status that already says
    ``asleep`` is answered here with no request at all (grid-reads-without-waking issue 01). A member
    cannot read that status (`remote_grid.resolve_relay_base` answers ``{}``), so for a member the
    proxy is the one that says it.
    """
    from remote import credentials

    from . import remote_grid

    session = credentials.require_session()
    rec = remote_grid._select(getattr(args, "grid", None))
    network_id = remote_grid._network_id(rec)
    label = str(rec.get("name") or network_id)
    base, status = remote_grid.resolve_relay_base(session, rec, network_id, label)
    if not getattr(args, "no_wake", False):
        token = str(rec.get("access_token") or "")  # public route — token optional
        return ReadTarget(session, rec, network_id, label, base, token)
    if status.get("state") == remote_grid.ASLEEP_STATE:
        raise _asleep_refusal(label)
    return ReadTarget(session, rec, network_id, label, base, "")


def _asleep_refusal(label: str, *, status: int | None = None, detail: Any = None) -> SystemExit:
    """The refusal for a grid that is asleep: ``grid_asleep`` in the ``--json`` envelope.

    A ``TaskRefusal`` because that is this CLI's one ``SystemExit`` that carries a code to the envelope
    (`cli/json_error`); ``status`` is the proxy's when the proxy said it, ``None`` when the owner status
    did and nothing was asked.
    """
    from remote import relay

    if isinstance(detail, str) and detail:
        sentence = f"Grid {label} is asleep: {detail}"
    else:
        sentence = (
            f"Grid {label} is asleep, and {NO_WAKE_FLAG} does not wake it. Run the same command without "
            f"{NO_WAKE_FLAG}, or send the grid a request, to start it."
        )
    return relay.TaskRefusal(sentence, code=relay.GRID_ASLEEP_CODE, status=status)


def _fetch_overview(args: argparse.Namespace) -> dict[str, Any]:
    """The active remote grid's ``/relay/v1/grid/overview`` payload, or a clean ``SystemExit``."""
    target = read_target(args)
    return fetch_overview(target.base, target.token, target.label)


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
            resp = client.get(OVERVIEW_PATH)
    except httpx.RequestError as exc:
        raise SystemExit(f"Could not reach grid {label}: {exc}") from exc
    if resp.status_code >= 400:
        # Only the asleep code is read (for equality); every other refusal keeps today's sentence and
        # a null code — a codeless 503 and `grid_master_down` included.
        if relay.answers_grid_asleep(resp):
            raise _asleep_refusal(label, status=resp.status_code, detail=_detail(resp))
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
    case_map = _model_case_map(overview)
    return tuple(dict.fromkeys(
        model for node in _nodes_from(overview) for model in _node_models(node, case_map)
    ))


def _detail(resp: httpx.Response) -> Any:
    """The ``detail`` beside an error answer's code, or ``None``. Never raises (a hostile body)."""
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 — see `relay._answer_code`
        return None
    return body.get("detail") if isinstance(body, dict) else None


def _model_case_map(overview: dict[str, Any]) -> dict[str, str]:
    """``lower(id) -> id``, read from the overview's own top-level ``models`` list.

    That list is the ONE place the overview reports a model's id in its true case; every node's own
    ``models`` array is lowercased for display (grid-leave issue: reproduced live serving
    `Qwen3.5-2B-Q4_K_M`, listed under a node as `qwen3.5-2b-q4_k_m`, and rejected verbatim when
    copied back into `grid chat -m`). Correcting it here, once, fixes it everywhere this renders —
    `grid engines`, `grid models`, and `grid launch`'s preflight (`live_model_names`) all read
    through this same function now, so none of them can show a name the grid won't answer to.
    `grid models` goes further (`_exact_case_map`): the list holds only CURATED models."""
    return _case_map_of(_curated_ids(overview))


def _curated_ids(overview: dict[str, Any]) -> list[str]:
    entries = overview.get("models")
    if not isinstance(entries, list):
        return []
    return [entry["id"] for entry in entries if isinstance(entry, dict) and isinstance(entry.get("id"), str)]


def _case_map_of(*sources: list[str]) -> dict[str, str]:
    """``lower(id) -> id`` over ``sources`` in order. The first spelling of a name wins, except that an
    id carrying an upper-case letter always replaces an all-lower-case one — the lower-case form is what
    the overview already showed, so it is never the better answer (grid-reads-without-waking issue 01).
    """
    case: dict[str, str] = {}
    for source in sources:
        for exact in source:
            key = exact.strip().lower()
            if not key:
                continue
            known = case.get(key)
            if known is None or (known == known.lower() and exact != exact.lower()):
                case[key] = exact.strip()
    return case


#: What the master's display rule strips from the end of a raw model id, compared without regard to case.
_GGUF_SUFFIX = ".gguf"


def _display_name(raw: str) -> str:
    """grid-src's ``model_ids.display_model_name``: a trailing ``.gguf`` removed, case kept — the rule
    the master applies before it lower-cases an id for the overview. ⚠️ Cross-repo; see the register."""
    return raw[: -len(_GGUF_SUFFIX)] if raw.lower().endswith(_GGUF_SUFFIX) else raw


def _run_record_ids(network_id: str) -> list[str]:
    """What THIS computer advertises on the grid, in the case it advertised it: each run record's
    ``advertise_as`` first (that is what was registered), then its ``models``. Anything unreadable is
    nothing — the list is only ever a spelling, never a reason to fail the command."""
    from shared import run_records

    try:
        records = run_records.read_records(network_id)
    except (OSError, SystemExit):  # `jsonio.load_json` refuses a damaged record with a SystemExit
        return []
    ids: list[str] = []
    for record in records.values():
        for key in ("advertise_as", "models"):
            values = record.get(key)
            if isinstance(values, list):
                ids.extend(_display_name(value) for value in values if isinstance(value, str))
    return ids


def _discovered_ids(base: str, token: str) -> list[str]:
    """Every served route's ``raw_model_id`` from the grid's public provider discovery, under the
    master's display rule. One read, with the same credential the overview went out with (none under
    ``--no-wake``); any failure is an empty list, which keeps today's ids."""
    from remote import relay

    try:
        with relay.open_consumer_client(base, token, timeout=_OVERVIEW_TIMEOUT) as client:
            resp = client.get(DISCOVER_PATH)
        body = resp.json() if resp.status_code == 200 else None
    except Exception:  # noqa: BLE001 — a transport fault or a hostile body: no spellings, never a failure
        return []
    providers = body.get("providers") if isinstance(body, dict) else None
    if not isinstance(providers, list):
        return []
    ids: list[str] = []
    for provider in providers:
        routes = provider.get("models") if isinstance(provider, dict) else None
        capabilities = provider.get("capabilities") if isinstance(provider, dict) else None
        entries = capabilities.get("models") if isinstance(capabilities, dict) else None
        if not isinstance(routes, list) or not isinstance(entries, dict):
            continue
        for route in routes:
            entry = entries.get(route) if isinstance(route, str) else None
            raw = entry.get("raw_model_id") if isinstance(entry, dict) else None
            if isinstance(raw, str):
                ids.append(_display_name(raw))
    return ids


def _exact_case_map(overview: dict[str, Any], target: ReadTarget) -> dict[str, str]:
    """``lower(id) -> id`` for `grid models`: the curated list (today's), then this computer's run
    records, then — only while some served id is still all lower-case — the grid's provider discovery
    (grid-reads-without-waking issue 01). Its ``--json`` then stops lower-casing ids."""
    case = _case_map_of(_curated_ids(overview), _run_record_ids(target.network_id))
    served = [str(model).strip().lower() for node in _nodes_from(overview) for model in _raw_models(node)]
    if all(_has_upper_case(case.get(model, model)) for model in served):
        return case
    return _case_map_of(list(case.values()), _discovered_ids(target.base, target.token))


def _has_upper_case(model_id: str) -> bool:
    """Whether an id carries an upper-case letter — i.e. is not merely the lower-cased form the overview
    already shows, so no other source could spell it better."""
    return model_id != model_id.lower()


def _raw_models(node: dict[str, Any]) -> list[Any]:
    models = node.get("models")
    return models if isinstance(models, list) else []


def _node_models(node: dict[str, Any], case_map: dict[str, str]) -> list[str]:
    """A node's served model ids as strings (defends against a non-list ``models`` or non-string
    items — otherwise ``",".join`` would split a bare string into characters or raise ``TypeError``).

    Corrected against ``case_map`` (`_model_case_map`, or `grid models`' `_exact_case_map`) — the raw,
    lowercased id is never what should render."""
    return [case_map.get(str(model).strip().lower(), str(model)) for model in _raw_models(node)]


def _node_responses_models(node: dict[str, Any]) -> set[str]:
    """The subset of a node's served models that serve the Responses dialect (issue 10), read from
    the overview's ``responses_models`` and str-coerced to match ``_node_models``' ids. Defends
    against a non-list field, non-string items, and an older master that omits it entirely — each
    yields an empty set, so a capability is never falsely shown (graceful degradation, fail-closed)."""
    models = node.get("responses_models")
    if not isinstance(models, list):
        return set()
    # Lower-cased, because it is matched against ids whose case `_node_models` may have restored.
    return {str(model).lower() for model in models}


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

    case_map = _model_case_map(overview)
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
        models = ",".join(_node_models(node, case_map)) or "(none)"
        print(f"{name:<{nwidth}}  {engine:<{ewidth}}  {device:<{dwidth}}  {tok}")
        print(f"{'':<{nwidth}}  models: {models}")
    return 0


def cmd_remote_models(args: argparse.Namespace) -> int:
    """`grid models` (remote): the models served across the active grid's live engines, plus the
    reserved ``auto`` model when the grid has auto-routing enabled AND serves at least one engine
    model (mirrors ``GET /relay/v1/models``, except the zero-engine case — see the gate below).

    Each engine row carries whether it serves the model via the Responses dialect (issue 10), read
    per-engine from the overview's ``responses_models``; shown in ``-v`` and ``--json`` (an older
    master omits the field → nothing shown). The plain listing stays bare model ids for scripting.

    Ids are in exact case (`_exact_case_map`), and ``--no-wake`` reads a sleeping grid without waking
    it (see the module docstring)."""
    target = read_target(args)
    overview = fetch_overview(target.base, target.token, target.label)
    case_map = _exact_case_map(overview, target)
    nodes = _nodes_from(overview)
    rows: list[tuple[str, str, str, bool]] = []
    for node in nodes:
        engine = str(node.get("engine") or "")
        name = str(node.get("name") or "")
        capable = _node_responses_models(node)  # resolved once per node, not per served model
        for model in _node_models(node, case_map):
            rows.append((model, engine, name, model.lower() in capable))
    # When auto routing is enabled AND the grid actually serves something, advertise the reserved
    # router family FIRST — mirroring the relay's /relay/v1/models endpoint (owner `grid-router`).
    # The relay lists the three effort modes under their display names
    # (`effort_router.EFFORT_DISPLAY_NAMES`: "Auto", "Brute Force", "Feedback Loop"); the standard
    # one is the bare `auto` row the grid has always shown (both spellings parse the same), and
    # these two extra names are accepted request ids too — without them the CLI hid two working
    # models behind the one listing (an older master whose overview lacks router_enabled reports
    # falsy → no router rows at all: graceful degradation).
    # Gated on `rows` (an intentional divergence from the relay): the router ranks candidates from
    # the models the grid currently serves (ADR 0013), so with zero engine models it has nothing to
    # pick — every request to the family fails. A zero-engine grid must not list three aliases the
    # caller cannot run; it gets the plain "no live models" line instead.
    # `responses` is False for `auto`: dialect-reachability is a per-request routing outcome, not a
    # static property of the reserved model (no AC covers it) — a real model's badge is its engine's.
    if overview.get("router_enabled") and rows:
        rows.insert(0, ("auto", "grid-router", "", False))
        rows[1:1] = [(name, "grid-router", "", False) for name in ("Brute Force", "Feedback Loop")]

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
    # Prefer a real model over the reserved router family here: the router rows are always inserted
    # first when routing is on, and a newcomer who just joined an engine wants to see THAT model
    # chat-tested, not a router alias. Mirrors the local `cmd_models`' closing-the-loop hint (issue:
    # `grid join`'s own "still loading" message can't yet promise a working model — this can).
    target = next((m for m in seen if m not in ("auto", "Brute Force", "Feedback Loop")), seen[0])

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
