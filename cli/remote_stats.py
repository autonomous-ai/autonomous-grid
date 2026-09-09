"""Remote-mode `grid stats` / `grid usage`: the live rollup a hosted grid's relay reports.

The terminal equivalent of the desktop app's grid panels. Both read the *same* two relay
endpoints, through the same rules, so the two surfaces cannot disagree about one grid:

* ``GET /relay/v1/grid/overview`` (public) — the grid's state and uptime, its advertised
  models, and every live engine with its hardware, telemetry and answered-token rollup. The
  app parses it in ``lib/infrastructure/api/models/grid_overview.dart`` and folds it in
  ``grid_power_provider.dart`` / ``node_groups.dart`` / ``model_usage.dart``.
* ``GET /relay/v1/grid/members/usage`` (per-grid token) — what each person on the grid ran in
  the same window. The app's ``member_usage_provider.dart``.

``grid usage --by member`` reads **only** the second of those. It used to also merge the
control-plane roster so somebody who had *not* used the grid still got an unmeasured row; that
call is refused to anyone who is not the grid's owner or an active member, and on a grid whose
membership is not a stored row it has nothing to contribute even when it answers. So the command
now reports what was measured and nothing else — who is merely a member is ``grid members list``.

Three rules are ported deliberately and are the reason this is not a thinner wrapper:

* **Absent is not zero.** A relay too old to compute a rollup sends no ``answered`` object at
  all, and a ``0`` printed there would report a busy fleet as dead. Unmeasured prints ``—``.
* **Cached input is *part of* input, never additional to it.** Settlement bills
  ``(in − cached)·input + cached·cache + out·output``, so the ``input`` this prints is the
  *fresh* leg — the three legs then add up to what actually passed through. ``--json`` carries
  the relay's raw ``tokens_in`` alongside it, so nothing is redefined on the wire.
* **A subscription seat brings a plan, not memory.** A node with a ``plan_type`` relays to a
  hosted model, so its host's RAM never runs anything for the grid and is left out of the pool.

Import rule mirrors `cli/remote_overview.py`: `remote.*` and the remote-specific `cli` siblings
are imported lazily inside the helpers, because `cli.dispatch` imports this module while the
`cli` package is still initialising. `cli._format` is a leaf (stdlib only) and safe at top.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import httpx

from . import _format


# Both reads are small; neither parser carries a `--timeout`, and the relay client requires the
# kwarg — so bound them with a constant, like `remote_overview._OVERVIEW_TIMEOUT`.
_TIMEOUT = 30.0

# Printed in place of a figure the grid did not report. An em dash, not `0`, `N/A` or a blank:
# it reads as "nothing here" without looking like a measurement or like a bug.
UNMEASURED = "—"

USAGE_DIMENSIONS = ("model", "member", "engine")

# How many machines `grid stats --verbose` prints a card for, however many the relay returns.
#
# A card is eleven lines, so a large grid would otherwise scroll its own summary off the screen —
# the rollup at the top is the part every run is read for. The list is sorted strongest-first
# (see [engine_cards]), so the cap keeps the machines actually carrying the grid and drops the
# tail. Nothing is hidden: the `nodes=` line above states the real count, and `--json` is
# uncapped, so a script never sees a truncated fleet.
MAX_NODE_CARDS = 20


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------

def _resolve(args: argparse.Namespace) -> tuple[str, dict[str, Any], str, str, str, str]:
    """``(session, record, network_id, label, relay base, token)`` for the grid these commands act on.

    Same gates, in the same order, as every other remote read: signed in → a grid resolves → it
    is up. The token is passed along for the ride and ignored by the public overview route, which
    is what lets `grid stats` work on a grid whose token `grid sync` has not stored yet;
    `_require_token` adds the real gate for the one dimension that names people.
    """
    from remote import credentials

    from . import remote_grid

    session = credentials.require_session()
    rec = remote_grid._select(getattr(args, "grid", None))
    network_id = remote_grid._network_id(rec)
    label = str(rec.get("name") or network_id)
    base, _status = remote_grid.resolve_relay_base(session, rec, network_id, label)
    return session, rec, network_id, label, base, str(rec.get("access_token") or "")


def _require_token(rec: dict[str, Any], label: str) -> str:
    from . import remote_grid

    return remote_grid.require_access_token(rec, label)


def fetch_member_usage(base: str, token: str, label: str) -> dict[str, Any] | None:
    """``/relay/v1/grid/members/usage`` for the grid at ``base``, or ``None`` when it can't say.

    ``None`` covers every "this grid has no answer for us" case and is rendered as such, never as
    zeros: a master that predates the endpoint (404), a caller who may not ask (401/403), and a
    relay whose ``members`` is explicitly null — no rollup has landed yet. A grid that *did*
    measure and found nobody sends an empty list, which is a different and true statement.

    Anything else — a transport failure, a 500, a non-JSON body — is a clean ``SystemExit``: those
    are not facts about how much anyone used, and silently reading them as "no data" would put a
    broken relay on screen as an idle grid.
    """
    from remote import relay

    try:
        with relay.open_consumer_client(base, token, timeout=_TIMEOUT) as client:
            resp = client.get("/relay/v1/grid/members/usage")
    except httpx.RequestError as exc:
        raise SystemExit(f"Could not reach grid {label}: {exc}") from exc
    if resp.status_code in (401, 403, 404):
        return None
    if resp.status_code >= 400:
        raise SystemExit(f"Grid {label} member usage failed ({resp.status_code}): {resp.text[:200]}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise SystemExit(f"Grid {label} returned a non-JSON member usage: {resp.text[:200]}") from exc
    if not isinstance(data, dict) or data.get("members") is None:
        return None
    if not isinstance(data.get("members"), list):
        raise SystemExit(f"Grid {label} returned an unexpected member usage shape.")
    return data


# ---------------------------------------------------------------------------
# defended readers — the relay body crosses a trust boundary
# ---------------------------------------------------------------------------

def _number(value: Any) -> float | None:
    """A JSON number as a float, or ``None`` for anything else.

    ``bool`` is an ``int`` subclass, so it is excluded explicitly: ``vram_gb: true`` must read as
    "this node didn't say", not as one gigabyte.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _count(value: Any) -> int:
    """A JSON number as a non-negative int; ``0`` for anything else. For figures *inside* an
    object the relay did send, where a missing key is genuinely zero."""
    number = _number(value)
    return int(number) if number is not None and number > 0 else 0


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _gb(value: float | None) -> float | None:
    """A GB figure rounded to the tenth these commands print.

    The relay reports memory in megabytes, so every GB figure here is a division and carries the
    float noise of one (``105.24374999999998``). Rounding at the projection — not at the render —
    is what keeps ``--json`` and the human line quoting the same number rather than one being the
    exact remainder of the other.
    """
    return None if value is None else round(value, 1)


def read_answered(raw: Any) -> dict[str, int] | None:
    """One ``answered`` object as four honest figures, or ``None`` when the relay sent none.

    ``tokens_cached`` is clamped to the ``tokens_in`` it is a share of — the way the relay stores
    it. An old row or a provider bug would otherwise hand the caller a negative fresh-input leg.
    """
    if not isinstance(raw, dict):
        return None
    tokens_in = _count(raw.get("tokens_in"))
    return {
        "window_seconds": _count(raw.get("window_seconds")),
        "tokens_in": tokens_in,
        "tokens_cached": min(_count(raw.get("tokens_cached")), tokens_in),
        "tokens_out": _count(raw.get("tokens_out")),
        "requests": _count(raw.get("requests")),
    }


def _fresh(answered: dict[str, int]) -> int:
    """Input that was not served from a prompt cache — the leg that, with cache and output, adds
    up to what passed through."""
    return answered["tokens_in"] - answered["tokens_cached"]


def _online(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The engines currently serving. ``online`` is read strictly (``is True``), the same way the
    app reads it: a machine that is asleep contributes no memory and no throughput, and counting
    one would overstate what the grid can do right now."""
    return [node for node in nodes if node.get("online") is True]


def node_memory_gb(node: dict[str, Any]) -> float | None:
    """GPU memory this engine contributes to the grid pool, in GB, or ``None`` when it brings none.

    Prefers the relay's ``vram_gb``, falling back to ``vram_total_mb / 1024``. A subscription seat
    (a non-empty ``plan_type``) returns ``None`` however much its host reports: it relays to a
    hosted model, so that memory never runs a model for the grid, and pooling it would advertise
    capacity nobody on the grid can use.
    """
    if _text(node.get("plan_type")):
        return None
    gb = _number(node.get("vram_gb"))
    if gb is None:
        total_mb = _number(node.get("vram_total_mb"))
        gb = None if total_mb is None else total_mb / 1024
    return gb if gb is not None and gb > 0 else None


def node_memory_used_gb(node: dict[str, Any]) -> float | None:
    """Memory this engine currently has in use, in GB, or ``None``. Gated on [node_memory_gb] so
    the used share is summed over exactly the machines the total is: a ratio counted over two
    different sets is quietly wrong rather than obviously missing."""
    if node_memory_gb(node) is None:
        return None
    used_mb = _number(node.get("vram_used_mb"))
    return used_mb / 1024 if used_mb is not None and used_mb > 0 else None


def node_memory_kind(node: dict[str, Any]) -> str:
    """What this engine's memory should be *called*: ``RAM`` on Apple Silicon, which shares one
    unified pool, and ``VRAM`` on a discrete GPU that has its own."""
    return "RAM" if _text(node.get("platform")).lower().startswith("macos-arm") else "VRAM"


def _platform_label(platform: Any) -> str:
    """``macos-arm64`` → ``macOS``. The arch half is dropped: ``x86_64`` beside ``M3 Ultra`` tells
    nobody anything they can act on, and the hardware line already names the chip."""
    value = _text(platform).lower()
    for prefix, label in (("macos", "macOS"), ("linux", "Linux"), ("windows", "Windows")):
        if value.startswith(prefix):
            return label
    return ""


def _model_key(model: Any) -> str:
    """One model id in the form everything here compares by.

    Ids arrive from two directions that disagree on case — the grid's catalog entry keeps its own
    (``DeepSeek-V4-Flash-0731``) while a node advertises the relay's normalised, lowercased id —
    and a raw ``==`` misses every model on the grid *silently*, as "this model reports nothing".
    """
    return str(model).strip().lower()


# ---------------------------------------------------------------------------
# rollups — pure, so every rule below is testable without a relay
# ---------------------------------------------------------------------------

def grid_rollup(overview: dict[str, Any]) -> dict[str, Any]:
    """What the grid brings and what it has answered, from an already-fetched overview.

    Each hardware total stays ``None`` unless at least one online engine reported that field, so a
    grid where nobody advertises memory shows none rather than claiming "0 GB".

    **There is deliberately no grid-level throughput.** Each engine's ``throughput_tok_s`` is its
    own decode estimate, measured whenever it last answered something, so adding them up produces a
    rate no single request ever sees and no two engines were ever measured at the same moment. Speed
    is a property of the machine that answers you; it is reported per engine (``--verbose``) and
    nowhere else.

    ``parallel`` prefers the relay's own ``stats.concurrent_capacity``: the relay is the authority
    on how much work it will actually dispatch, and may cap or oversubscribe what the engines
    individually advertise. Summing ``max_concurrency`` is the fallback for a relay that is silent.

    ``answered`` prefers the grid-wide rollup over summing the engines, and the difference is not
    cosmetic: an engine is listed only while its heartbeat is live, so a machine that served all
    morning and then went offline takes its tokens out of a summed total — and the relay's
    per-node rollup separately drops rows it cannot attribute to a machine. The sum is only what a
    relay too old to send a grid total leaves us.
    """
    from . import remote_overview

    nodes = remote_overview._nodes_from(overview)
    online = _online(nodes)
    stats = overview.get("stats") if isinstance(overview.get("stats"), dict) else {}
    models = overview.get("models")

    memory = memory_used = None
    summed_parallel: int | None = None
    node_total: dict[str, int] | None = None
    for node in online:
        gb = node_memory_gb(node)
        if gb is not None:
            memory = (memory or 0.0) + gb
            used = node_memory_used_gb(node)
            if used is not None:
                memory_used = (memory_used or 0.0) + used

        parallel = _number(node.get("max_concurrency"))
        if parallel is not None and parallel > 0:
            summed_parallel = (summed_parallel or 0) + int(parallel)

        answered = read_answered(node.get("answered"))
        if answered is not None:
            node_total = _add_answered(node_total, answered)

    capacity = _number(stats.get("concurrent_capacity"))
    pool = _gb(memory)
    # Clamped to the pool it is a share of (see below), then rounded like every other GB figure —
    # so the percentage and the two numbers it sits between are computed from the same values.
    used_pool = _gb(None if memory is None or memory_used is None else min(memory_used, memory))
    return {
        "status": _state(overview),
        "uptime_pct": _number(stats.get("uptime_pct")),
        "engines_online": len(online),
        "engines_total": len(nodes),
        "models": len(models) if isinstance(models, list) and models else _count(stats.get("models")),
        "memory_gb": pool,
        # Never above the pool it is a share of: an engine reporting more used than it advertises
        # as total (a driver rounding, a seat counted twice) would otherwise show over 100%.
        "memory_used_gb": used_pool,
        "memory_used_pct": None if not pool or used_pool is None else round(used_pool / pool * 100, 1),
        "parallel": int(capacity) if capacity is not None and capacity > 0 else summed_parallel,
        "answered": read_answered(overview.get("answered")) or node_total,
    }


def _add_answered(running: dict[str, int] | None, extra: dict[str, int]) -> dict[str, int]:
    """Fold one rollup into a running total. The window is one setting on one relay, so every row
    agrees; the first one seen is the one reported."""
    if running is None:
        return dict(extra)
    return {
        "window_seconds": running["window_seconds"] or extra["window_seconds"],
        "tokens_in": running["tokens_in"] + extra["tokens_in"],
        "tokens_cached": running["tokens_cached"] + extra["tokens_cached"],
        "tokens_out": running["tokens_out"] + extra["tokens_out"],
        "requests": running["requests"] + extra["requests"],
    }


def _state(overview: dict[str, Any]) -> str:
    grid = overview.get("grid")
    return _text(grid.get("state")) if isinstance(grid, dict) else ""


def engine_cards(overview: dict[str, Any]) -> list[dict[str, Any]]:
    """One projected card per engine, strongest first — the shape `-v` prints and `--json` emits.

    A derived view, not the raw passthrough `grid engines --json` already gives: a new field on a
    node object shows up there, not here. Ordered by memory rather than left in relay order,
    because the machine carrying most of the grid is the one worth seeing first and relay order
    buried a 382 GB box under a 32 GB laptop. Engines bringing no memory sort last, by name, so
    the order stays stable between refreshes instead of shuffling.
    """
    from . import remote_overview

    cards = [_engine_card(node, overview) for node in remote_overview._nodes_from(overview)]
    cards.sort(key=lambda card: (
        card["memory_gb"] is None, -(card["memory_gb"] or 0.0), card["engine"]
    ))
    return cards


def _engine_card(node: dict[str, Any], overview: dict[str, Any]) -> dict[str, Any]:
    from . import remote_overview

    total = node_memory_gb(node)
    used = node_memory_used_gb(node)
    capabilities = node.get("model_capabilities")
    capabilities = capabilities if isinstance(capabilities, dict) else {}
    # Two readings of one list, in the same order. A node advertises the relay's *lowercased* id
    # and that is what its `model_capabilities` is keyed by — but the lowercased form is never what
    # should render, so the displayed name comes from the overview-corrected reading, which restores
    # the catalog's true case (`deepseek-v4-flash-0731` → `DeepSeek-V4-Flash-0731`). Both lists are
    # built from the same `node["models"]`, so zipping them cannot misalign.
    keys = remote_overview._node_models(node)
    shown = remote_overview._node_models(node, overview)
    models = []
    for key, name in zip(keys, shown):
        entry = capabilities.get(key)
        window = _number(entry.get("context_length")) if isinstance(entry, dict) else None
        models.append({"model": name, "context_length": int(window) if window else None})
    return {
        "engine": _text(node.get("name")),
        "online": node.get("online") is True,
        "owner": _text(node.get("provider_email")),
        "hardware": _text(node.get("chip")) or _text(node.get("device")),
        "platform": _platform_label(node.get("platform")),
        "kind": _text(node.get("engine")),
        "plan_type": _text(node.get("plan_type")) or None,
        "models": models,
        "parallel": int(_number(node.get("max_concurrency")) or 0) or None,
        "throughput_tok_s": _number(node.get("throughput_tok_s")),
        "memory_kind": node_memory_kind(node),
        "memory_gb": _gb(total),
        "memory_used_gb": _gb(used),
        # Only when the node reported BOTH halves: subtracting an absent figure from a present one
        # would invent headroom nobody measured.
        "memory_free_gb": _gb(None if total is None or used is None else total - used),
        "gpu_temp_c": _number(node.get("gpu_temp_c")),
        "gpu_util_pct": _number(node.get("gpu_util_pct")),
        "gpu_power_w": _number(node.get("gpu_power_w")),
        "gpu_power_limit_w": _number(node.get("gpu_power_limit_w")),
        "disk_total_gb": _number(node.get("disk_total_gb")),
        "disk_used_gb": _number(node.get("disk_used_gb")),
        "answered": read_answered(node.get("answered")),
    }


def model_rows(overview: dict[str, Any], grid_total: dict[str, int] | None) -> list[dict[str, Any]]:
    """What every model on the grid answered, busiest first.

    A model is usually served by more than one engine and the relay reports the rollup per engine,
    so the grid-level figure exists nowhere in the payload and is added up here (keyed by
    [_model_key], since the catalog entry and the node's advertisement disagree on case).

    Rows come from the grid's advertised model list *and* from anything the rollup measured that
    the list doesn't carry: a model the catalog has since dropped still did the work it did, and
    silently omitting it would leave the column adding up to less than its own header.

    A model with no rows of its own is ambiguous, and [grid_total] settles it — non-null exactly
    when at least one engine reported a rollup. On a measured grid such a model gets a real zero,
    because a model nobody used today is a fact worth seeing and a blank row reads as one the CLI
    forgot to ask about; on a grid nothing measured it stays unmeasured, because printing zeros
    against every model there would report a busy fleet as dead.

    Ordered by output, then requests, then id — so the position itself carries information, and a
    grid whose models have all answered nothing keeps a stable order across polls.
    """
    from . import remote_overview

    nodes = remote_overview._nodes_from(overview)
    online = _online(nodes)
    measured, serving = _measured_by_model(nodes), _serving_counts(online)
    idle = None if grid_total is None else {
        "window_seconds": grid_total["window_seconds"],
        "tokens_in": 0, "tokens_cached": 0, "tokens_out": 0, "requests": 0,
    }

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in _catalog_models(overview):
        key = _model_key(entry)
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append({
            # Membership, not truthiness: a measured row is a dict and never empty today, but an
            # `or` here would silently fall through to the zero row if that ever changed — and the
            # difference between "measured" and "assumed idle" is exactly what this line decides.
            "model": entry,
            "answered": measured[key] if key in measured else idle,
            "engines": serving.get(key, 0),
        })
    for key, answered in measured.items():
        if key not in seen:
            rows.append({"model": key, "answered": answered, "engines": serving.get(key, 0)})
    rows.sort(key=lambda row: (
        -(row["answered"] or {}).get("tokens_out", 0),
        -(row["answered"] or {}).get("requests", 0),
        _model_key(row["model"]),
    ))
    return rows


def _catalog_models(overview: dict[str, Any]) -> list[str]:
    """The ids in the overview's ``models`` list, defended against a non-list field and entries
    that aren't objects — a malformed catalog costs its own rows, never the whole table."""
    models = overview.get("models")
    if not isinstance(models, list):
        return []
    return [_text(m.get("id")) for m in models if isinstance(m, dict) and _text(m.get("id"))]


def _measured_by_model(nodes: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Every engine's ``answered.by_model`` folded into one total per model. A model no engine
    reported on is **absent**, never a zero entry: the caller has to be able to tell "answered
    nothing" from "nothing measured it", and only the missing key carries the second meaning."""
    totals: dict[str, dict[str, int]] = {}
    for node in nodes:
        answered = node.get("answered")
        by_model = answered.get("by_model") if isinstance(answered, dict) else None
        if not isinstance(by_model, list):
            continue
        window = _count(answered.get("window_seconds"))
        for entry in by_model:
            if not isinstance(entry, dict):
                continue
            key = _model_key(entry.get("model", ""))
            row = read_answered(entry)
            if not key or row is None:
                continue
            row["window_seconds"] = row["window_seconds"] or window
            totals[key] = _add_answered(totals.get(key), row)
    return totals


def _serving_counts(online: list[dict[str, Any]]) -> dict[str, int]:
    """How many online engines advertise each model. A node's primary ``model`` counts too — an
    older provider fills that and leaves ``models`` empty, and reading only the list would report
    such a machine as serving nothing."""
    from . import remote_overview

    counts: dict[str, int] = {}
    for node in online:
        # Uncorrected on purpose: every id here goes through [_model_key], which lowercases, so
        # restoring the catalog's case would be work undone on the next line.
        advertised = {_model_key(m) for m in remote_overview._node_models(node)}
        primary = _model_key(node.get("model") or "")
        if primary:
            advertised.add(primary)
        for key in advertised:
            if key:
                counts[key] = counts.get(key, 0) + 1
    return counts


def engine_usage_rows(overview: dict[str, Any]) -> list[dict[str, Any]]:
    """What each engine answered in the window, busiest first. Engines the relay measured and
    found idle keep their row (a real zero); one it never measured carries ``None``."""
    from . import remote_overview

    rows = [
        {"engine": _text(node.get("name")), "answered": read_answered(node.get("answered"))}
        for node in remote_overview._nodes_from(overview)
    ]
    rows.sort(key=lambda row: (-(row["answered"] or {}).get("tokens_out", 0), row["engine"]))
    return rows


def member_rows(members: list[Any] | None) -> list[dict[str, Any]]:
    """Who used the grid, biggest reader first.

    **Everyone here was measured.** The control-plane roster used to add an unmeasured row for a
    member who had never sent a request; `_usage_rows` explains why it no longer does. What survives
    of that merge is the one case it was also protecting: a consumer the relay counted but could not
    name keeps its row rather than being dropped — usage nobody can name is still usage.
    """
    rows: list[dict[str, Any]] = []
    for raw in members or []:
        if not isinstance(raw, dict):
            continue
        answered = read_answered(raw)
        if answered is None:
            continue
        rows.append({"email": _text(raw.get("email")), "answered": answered})
    rows.sort(key=lambda row: (-_fresh(row["answered"]), row["email"].lower()))
    return rows


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def _token_pairs(
    answered: dict[str, int] | None, *, include_window: bool = True
) -> list[tuple[str, str]]:
    """The window and the three legs plus requests, as name/value pairs.

    Every name is present whether or not the grid reported one — a stable set is what lets two runs
    be compared line for line — with an empty value where nothing measured it. What that emptiness
    is *rendered* as belongs to the caller: a dash where the values sit in a column, nothing after
    the `=` where they do not.

    ``include_window`` drops the span for a caller that has already stated it. `grid stats` names it
    in its heading, where it qualifies the whole readout; repeating it as a row would be the same
    fact twice on one screen.
    """
    window = [("window", "" if answered is None else _format.window(answered["window_seconds"]))]
    if answered is None:
        legs = [(name, "") for name in ("input", "cached", "output", "requests")]
    else:
        legs = [
            ("input", _format.count(_fresh(answered))),
            ("cached", _format.count(answered["tokens_cached"])),
            ("output", _format.count(answered["tokens_out"])),
            ("requests", _format.count(answered["requests"])),
        ]
    return (window if include_window else []) + legs


def _overview_block(rows: list[tuple[str, str]], window: str, *, title: str) -> list[str]:
    """A heading naming the span, then one aligned column of name/value pairs.

    The shape both commands open with, from one place: `grid stats` fills it with the machines and
    the day, `grid usage` with the day alone above its breakdown. Two renderings of the same block
    would drift, and these two are read minutes apart against the same grid.

    ``title`` is each caller's own, and required rather than defaulted so neither can inherit the
    other's noun by accident. In `grid stats` this block *is* the command's answer and names the
    thing it describes; in `grid usage` it is a header over a breakdown, and a heavier title there
    would out-weigh the table it introduces.

    **The span is the relay's, never the literal "24h".** The window is an operator knob
    (``node_answered_window_seconds``), so a hardcoded label would go quietly wrong the moment
    someone retuned it — and since no row repeats it, this heading is the only thing saying what the
    token figures below cover. A grid that reported no rollup names no span and gets the bare noun.

    Anything unreported reads [UNMEASURED]: in a column, a name with nothing after it looks like a
    rendering failure rather than a fact about the grid.
    """
    width = max(len(name) for name, _ in rows)
    return [
        f"{window} {title}:" if window else f"{title}:",
        "",
        *(f"{name:<{width}}  {value or UNMEASURED}" for name, value in rows),
    ]


def _window_label(answered: dict[str, int] | None) -> str:
    return _format.window(answered["window_seconds"]) if answered else ""


def render_usage_totals(answered: dict[str, int] | None) -> list[str]:
    """`grid usage`'s header: what the whole grid answered, above whichever split follows."""
    return _overview_block(
        _token_pairs(answered, include_window=False), _window_label(answered), title="overview"
    )


def _memory_value(rollup: dict[str, Any]) -> str:
    """The pool as one figure — ``0.9/1.4 TB (68%)`` — or empty when no engine advertised memory.

    The total alone is still worth printing when nothing reported its occupancy: it is the pool a
    model has to fit into, so only the missing half degrades.
    """
    total, used = rollup["memory_gb"], rollup["memory_used_gb"]
    if total is None:
        return ""
    if used is None:
        return _format.memory(total)
    return f"{_format.memory_share(used, total)} ({_format.share(used / total)})"


def render_stats(label: str, rollup: dict[str, Any]) -> list[str]:
    """The grid's own readout: a heading naming the span, then one column of figures under it.

    **The span in the heading is the relay's, never the string "24h".** The window is an operator
    knob (``node_answered_window_seconds``), so a hardcoded label would go quietly wrong the moment
    someone retuned it — and it qualifies every token figure below, which is exactly why it belongs
    in the heading and not in a row of its own. A grid that reported no rollup names no span, and
    the heading degrades to the bare noun rather than to an empty one.

    Anything the grid did not report reads [UNMEASURED] — the same dash the engine cards use,
    because in a column a name with nothing after it looks like a rendering failure rather than a
    fact about the grid.

    No speed among them: see [grid_rollup] for why a summed tok/s is not a rate the grid can
    deliver. Each machine's own is on its card.
    """
    uptime = rollup["uptime_pct"]
    answered = rollup["answered"]
    capacity = [
        ("grid", label),
        ("status", rollup["status"]),
        ("uptime", f"{_format.metric_number(uptime)}%" if uptime is not None else ""),
        # `nodes`, not `engines`, and deliberately at odds with the Output Contract's vocabulary
        # (docs/cli.md): this readout is the terminal form of the app's NODES panel, and a machine
        # here is a *node* while `engine` is the software running on it — a distinction the card
        # below makes on its own `engine` line ("external · 16 parallel"). One word could not carry
        # both. The `--json` keys stay `engines_online` / `engines_total` / `engines`, which is a
        # split worth knowing about; renaming those is a contract change, not a display one.
        ("nodes", str(rollup["engines_online"])),
        ("models", str(rollup["models"])),
        ("memory", _memory_value(rollup)),
        ("parallel", "" if rollup["parallel"] is None else str(rollup["parallel"])),
    ]
    return _overview_block(
        capacity + _token_pairs(answered, include_window=False), _window_label(answered),
        title="Grid Overview",
    )


def _card_lines(card: dict[str, Any]) -> list[str]:
    """One engine as a block of labelled readings.

    **Every reading gets its line, whether or not the machine reported it** — rows that come and
    go make two machines impossible to compare, because the eye has to re-read the labels on each
    before it can read the numbers. A reading the engine never sent prints [UNMEASURED], which is
    what keeps "measured at zero" (an online, idle GPU) apart from "never measured" (a Mac, which
    reports no temperature at all because macOS exposes it only to a root `powermetrics`).
    """
    hardware = " · ".join(part for part in (card["hardware"], card["platform"]) if part)
    kind = " · ".join(part for part in (
        card["kind"], f"{card['parallel']} parallel" if card["parallel"] else "",
        f"{card['plan_type']} plan" if card["plan_type"] else "",
    ) if part)
    models = ", ".join(
        entry["model"] + (f" ({_format.count(entry['context_length'])} ctx)" if entry["context_length"] else "")
        for entry in card["models"]
    )
    rate = card["throughput_tok_s"]
    answered = card["answered"]
    rows = [
        ("owner", card["owner"]),
        ("hardware", hardware),
        ("engine", kind),
        ("models", models),
        (card["memory_kind"], _pair_gb(card["memory_used_gb"], card["memory_gb"], card["memory_free_gb"])),
        ("temperature", f"{_format.metric_number(card['gpu_temp_c'])}°C" if card["gpu_temp_c"] is not None else UNMEASURED),
        ("usage", f"{_format.round_half_up(card['gpu_util_pct'])}%" if card["gpu_util_pct"] is not None else UNMEASURED),
        ("power", _pair_w(card["gpu_power_w"], card["gpu_power_limit_w"])),
        ("storage", _pair_gb(card["disk_used_gb"], card["disk_total_gb"], None)),
        ("throughput", f"~{_format.round_half_up(rate)} tok/s" if rate else UNMEASURED),
        (
            f"tokens {_format.window(answered['window_seconds']) or 'total'}" if answered else "tokens",
            _answered_phrase(answered),
        ),
    ]
    header = card["engine"] + ("" if card["online"] else "  (offline)")
    width = max(len(name) for name, _ in rows)
    return [header, *(f"  {name:<{width}}  {value or UNMEASURED}" for name, value in rows)]


def _pair_gb(used: float | None, total: float | None, free: float | None) -> str:
    """Two GB figures against each other — ``277.2/382.4 GB (72%)``.

    One decimal on both halves, which is how the app's own node dashboard writes them, and not the
    compact rule the grid's pooled `memory=` line uses: a card has the width, and 382.4 is what the
    machine actually reported. Both rules are the app's, for the two widths it has; keeping them on
    the same surfaces is what stops one figure reading two ways.

    The total is a real fact worth printing even when the occupancy is missing — it is the pool a
    model has to fit into — so only the absent half degrades.
    """
    if total is None:
        return UNMEASURED
    if used is None:
        return f"{UNMEASURED}/{_format.metric_number(total)} GB"
    pair = f"{_format.metric_number(used)}/{_format.metric_number(total)} GB"
    share = f"{pair} ({_format.share(used / total)})" if total > 0 else pair
    return share if free is None else f"{share} · {_format.metric_number(free)} GB free"


def _pair_w(draw: float | None, limit: float | None) -> str:
    if draw is None:
        return UNMEASURED
    drawn = f"{_format.metric_number(draw)}W"
    return f"{drawn}/{UNMEASURED}" if limit is None or limit <= 0 else f"{drawn}/{_format.metric_number(limit)}W"


def _answered_phrase(answered: dict[str, int] | None) -> str:
    """A rollup in one line — "31.3M input · 526.2M cached · 6.2M output · 8K requests"."""
    if answered is None:
        return UNMEASURED
    return (
        f"{_format.count(_fresh(answered))} input · {_format.count(answered['tokens_cached'])} cached · "
        f"{_format.count(answered['tokens_out'])} output · {_format.count(answered['requests'])} requests"
    )


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    """A left-aligned table whose last column is not padded (trailing spaces are noise a pipe
    keeps). Empty when there are no rows — the caller prints its own empty state."""
    if not rows:
        return []
    widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))]
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    for row in rows:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return lines


def _cells(answered: dict[str, int] | None) -> list[str]:
    """The four token columns for one row — all [UNMEASURED] when nothing measured it."""
    if answered is None:
        return [UNMEASURED] * 4
    return [
        _format.count(_fresh(answered)),
        _format.count(answered["tokens_cached"]),
        _format.count(answered["tokens_out"]),
        _format.count(answered["requests"]),
    ]


_TOKEN_HEADERS = ["INPUT", "CACHED", "OUTPUT", "REQUESTS"]


def render_model_table(rows: list[dict[str, Any]], grid_out: int) -> list[str]:
    return _table(
        ["MODEL", *_TOKEN_HEADERS, "SHARE", "ENGINES"],
        [
            [
                str(row["model"]),
                *_cells(row["answered"]),
                _format.share((row["answered"] or {}).get("tokens_out", 0) / grid_out) if grid_out > 0 else UNMEASURED,
                str(row["engines"]),
            ]
            for row in rows
        ],
    )


def render_member_table(rows: list[dict[str, Any]]) -> list[str]:
    """Who spent the tokens, and nothing else. What each person is *allowed* to do is a different
    question with its own command (`grid members list`), and a permission column beside four token
    figures invited the two to be read as one fact about a person."""
    return _table(
        ["MEMBER", *_TOKEN_HEADERS],
        [[row["email"] or "(unnamed)", *_cells(row["answered"])] for row in rows],
    )


def render_engine_table(rows: list[dict[str, Any]]) -> list[str]:
    return _table(
        ["ENGINE", *_TOKEN_HEADERS],
        [[row["engine"], *_cells(row["answered"])] for row in rows],
    )


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_remote_stats(args: argparse.Namespace) -> int:
    """`grid stats [grid] [-v] [--json]` — what the grid brings and what it has answered."""
    from . import remote_overview

    _session, _rec, _network_id, label, base, token = _resolve(args)
    overview = remote_overview.fetch_overview(base, token, label)
    rollup = grid_rollup(overview)
    cards = engine_cards(overview)

    if getattr(args, "json", False):
        # A derived view, not a passthrough: `grid engines --json` stays the verbatim node list, so
        # a new relay field surfaces there rather than silently changing this shape. `--verbose`
        # deliberately does not move it — a flag that changed the machine-readable output would
        # make every script depend on how it was invoked.
        print(json.dumps({
            "grid": label,
            **rollup,
            "answered": _json_answered(rollup["answered"], window=True),
            "engines": [
                {**card, "answered": _json_answered(card["answered"], window=True)} for card in cards
            ],
        }, indent=2))
        return 0

    for line in render_stats(label, rollup):
        print(line)
    if not getattr(args, "verbose", False):
        return 0
    if not cards:
        print("\n(no nodes — `grid join` one first)")
        return 0
    # Fixed wording, so the list always announces the rule it follows rather than only saying so on
    # the grids large enough to hit it. How many actually exist is the `nodes=` line above.
    print(f"\nTop {MAX_NODE_CARDS} nodes:")
    for card in cards[:MAX_NODE_CARDS]:
        print()
        for line in _card_lines(card):
            print(line)
    return 0


def cmd_remote_usage(args: argparse.Namespace) -> int:
    """`grid usage [grid] [--by model|member|engine] [--json]` — who and what spent the tokens.

    The header totals are the **grid's own** rollup in every dimension, not the sum of the rows
    below them, so this command and `grid stats` can never print two different figures for one
    grid and window. The rows can therefore add up to slightly less: a member the relay could not
    name, or work it could not attribute to a machine, is counted once at the top and nowhere else.
    """
    from . import remote_overview

    dimension = getattr(args, "by", "model") or "model"
    _session, rec, _network_id, label, base, token = _resolve(args)
    overview = remote_overview.fetch_overview(base, token, label)
    rollup = grid_rollup(overview)
    answered = rollup["answered"]

    rows, note = _usage_rows(dimension, overview, answered, rec, label, base)

    if getattr(args, "json", False):
        print(json.dumps({
            "grid": label,
            "by": dimension,
            "window_seconds": answered["window_seconds"] if answered else None,
            "totals": _json_answered(answered),
            "rows": [_json_row(dimension, row) for row in rows],
        }, indent=2))
    else:
        for line in render_usage_totals(answered):
            print(line)
        table = {
            "model": lambda: render_model_table(rows, (answered or {}).get("tokens_out", 0)),
            "member": lambda: render_member_table(rows),
            "engine": lambda: render_engine_table(rows),
        }[dimension]()
        print()
        for line in table or [f"(no {dimension} usage — this grid reported none)"]:
            print(line)
    if note:
        print(note, file=sys.stderr, flush=True)
    return 0


def _usage_rows(
    dimension: str,
    overview: dict[str, Any],
    grid_total: dict[str, int] | None,
    rec: dict[str, Any],
    label: str,
    base: str,
) -> tuple[list[dict[str, Any]], str | None]:
    """The rows for one dimension, and a note for stderr when something had to be left out.

    Only `--by member` leaves the overview: the model and engine splits are already in it, while
    who-ran-what names *people* and so rides an authenticated endpoint.

    ⚠️ **It asks the GRID and nothing else — the control-plane roster is deliberately not consulted.**
    It used to be, so that a member who had never sent a request still kept an unmeasured row. Two
    things killed that: the roster is refused to anyone who is not the grid's owner or an active
    member, which put a caveat on stderr far more often than it added a row; and on a grid whose
    membership is not a stored row at all there is no roster to add — the read is guaranteed to
    refuse and guaranteed to have nothing to contribute even if it did not.

    ⚠️ **Not branched on the grid's type, and that is the same decision `cli/grid_credential` makes
    a few files over.** The stored record does carry a `network_type`, but it is a snapshot from the
    last login/sync that nothing refreshes on a token exchange, and keying on it would put a copy of
    a network-type literal in this repository, which by decision holds none. Such a branch degrades
    in **silence**: renamed at the far end it simply stops firing, with nothing red.

    So this command now answers exactly one question — *who spent what* — and answers it from the
    one place that measured it. Who may merely be a member is `grid members list`, which is the
    command that owns that question and reports its own refusal in its own words.
    """
    if dimension == "model":
        return model_rows(overview, grid_total), None
    if dimension == "engine":
        return engine_usage_rows(overview), None

    usage = fetch_member_usage(base, _require_token(rec, label), label)
    rows = member_rows((usage or {}).get("members"))
    if usage is None:
        return rows, (
            f"Note: grid {label} reports no member usage (its relay may predate the endpoint, or "
            f"no rollup has landed yet)."
        )
    return rows, None


def _json_answered(answered: dict[str, int] | None, *, window: bool = False) -> dict[str, int] | None:
    """A rollup as JSON, in one shape wherever one appears — the grid's, an engine's, a row's.

    Carries the relay's raw ``tokens_in`` **and** the fresh leg the human tables print, so nothing
    on the wire is redefined and no script has to know that cached input is a share of input
    rather than a fourth kind. ``window`` adds the span, for the places that are not already
    printed under one.
    """
    if answered is None:
        return None
    return {
        **({"window_seconds": answered["window_seconds"]} if window else {}),
        "tokens_in": answered["tokens_in"],
        "tokens_in_fresh": _fresh(answered),
        "tokens_cached": answered["tokens_cached"],
        "tokens_out": answered["tokens_out"],
        "requests": answered["requests"],
    }


def _json_row(dimension: str, row: dict[str, Any]) -> dict[str, Any]:
    key = {"model": "model", "member": "email", "engine": "engine"}[dimension]
    entry: dict[str, Any] = {key: row[key], **(_json_answered(row["answered"]) or {})}
    if dimension == "model":
        entry["engines"] = row["engines"]
    entry["measured"] = row["answered"] is not None
    return entry
