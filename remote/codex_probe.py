"""The codex join probe — one free ``GET {base}/models`` proving what a sign-in alone cannot
(ADR 0015 D-f as amended; issue 05).

OAuth success proves the vendor honoured a code; it says nothing about whether THIS machine can
serve the seat. The probe settles the three join-time questions in one round-trip, for free
(facts.md B1 — the endpoint is free, where a ``POST /responses`` probe could not even cap its own
spend, facts.md #1):

* **egress reachability** — Cloudflare fronts the vendor host, and a datacenter/VPS egress IP can
  draw a challenge; a challenged machine can never serve jobs (PRD user stories 8/9);
* **seat liveness** — the token's ``exp`` says nothing about server-side revocation;
* **the entitled set** — the seat's REAL visible model list, which no tier guess can beat.

The tier is deliberately NOT read here: it lives in the access token's claim, decoded offline at
sign-in (``remote/codex_auth.decode_seat``), and ``GET /models`` carries no ``x-codex-*`` headers
at all (facts.md B6), so this response has nothing to cross-check against.

Every failure is the operator's taxonomy (issue 05), one distinct terminal message per class —
except the AUTH class, which is the typed, catchable ``SeatRejected``: the join's dead-seat
re-sign-in (the PRD's sign-in inline "when the stored one is dead") must catch exactly that class
and nothing else, and catching a ``SystemExit`` to string-match it would be the bug this type
exists to prevent. Classification rules paid for in spike evidence:

* Cloudflare detection keys on **403 + ``Cf-Mitigated``**, never on ``CF-RAY`` — CF-RAY rides
  every response including 200s, so keying on it would classify every success as a block
  (facts.md B4). The CF-403 branch itself is live-UNVERIFIED (no challenge was drawable from a
  residential IP); its message states what is known.
* A vendor 400 NEVER means "tier mismatch" — the vendor's own out-of-set refusal names the auth
  mode, not the tier (facts.md #5) — so the 400 message here claims contract drift and advises a
  newer release, nothing about tiers.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from typing import Any

import httpx

from . import codex_oauth

# One request on a human-watched path: a hung socket must surface as an error, not hang the join.
# Matches `_VENDOR_LIST_TIMEOUT` (the openai join call) and `_EXCHANGE_TIMEOUT` (the sign-in).
_PROBE_TIMEOUT = 15.0

# A model slug rides into the advertised name and onto the operator's terminal (the join print,
# the `-m` refusal). Vendor text at best, so it is printable-checked and length-bounded at the ONE
# point of origin — the same `_safe`/`_bounded_detail` posture — so an unbounded/ANSI-laden slug from
# a hostile or MITM'd backend can never forge lines or inject escapes downstream (CWE-150). Real
# slugs are short (`gpt-5.6-terra`); 128 is generous headroom.
_MAX_SLUG_LEN = 128


@dataclass(frozen=True)
class CodexModel:
    """One entitled model from the seat's live ``GET /models`` — slug plus the caps the codex
    capability envelope needs (issue 10a). The probe is the SOLE source of truth for the served set
    and its caps now that the static tier table no longer gates serving; the CLI persists these into
    the run record at join so the serving side can build the envelope with no static lookup.

    ``context_window`` uses ``0`` as the "unknown" sentinel (the existing media/doggi convention), so
    an absent/malformed probe value is OMITTED from the envelope downstream, never a fabricated
    number. Caps fail closed per field (absent → the conservative claim); the model itself is still
    served (DESIGN §8 — never fail closed on a model)."""

    slug: str
    context_window: int  # 0 = unknown (omitted from the envelope), never fabricated
    supports_vision: bool
    supports_tools: bool


class SeatRejected(Exception):
    """The vendor refused the seat's credential (401, or 403 without a Cloudflare marker).

    The ONE probe failure the join may catch — a stored seat that died invites one inline
    re-sign-in (issue 05); every other class is terminal by design and raises ``SystemExit``
    directly. Carries only the status code: an auth-error body on this host is vendor text of
    unbounded shape, and the operator message is the join's to compose.
    """

    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def probe_seat(
    bundle: codex_oauth.CodexBundle, *, base_url: str, client_version: str
) -> tuple[CodexModel, ...]:
    """The seat's servable models (slug + derived caps), or why this machine cannot serve it.

    One GET, no retry. Raises ``SeatRejected`` for the auth class and ``SystemExit`` (a terminal
    operator message ending "Nothing was joined.") for every other failure. The records come back
    deduped in vendor order; membership is ``visibility != "hide"`` AND ``supported_in_api`` is not
    explicitly ``False`` (issue 10a / DESIGN §6): a model the vendor hides from its own client
    (``codex-auto-review``) or explicitly marks API-unsupported is never advertised, but an ABSENT
    ``supported_in_api`` is served (Option A — never fail closed on a model). Caps derive from the
    probe (``context_window``; vision from ``input_modalities``; tools from the tool-call flags),
    fail-closed per field.
    """
    url = f"{base_url}/models"
    headers = {
        # The five headers the real client sends (spike probe.py `headers_for`, verified on the
        # wire 2026-07-15). No Content-Type: this is a GET with no body.
        "Authorization": f"Bearer {bundle.access_token}",
        "Chatgpt-Account-Id": bundle.account_id,
        "Originator": codex_oauth.ORIGINATOR,
        "User-Agent": codex_oauth.ORIGINATOR,
        "Accept": "application/json",
    }
    try:
        with httpx.Client(timeout=_PROBE_TIMEOUT) as client:
            resp = client.get(url, params={"client_version": client_version}, headers=headers)
    except httpx.HTTPError as exc:
        raise SystemExit(
            f"Could not reach the codex backend at {url}: {exc}. Nothing was joined."
        ) from None

    if resp.status_code != 200:
        _raise_probe_failure(resp)
    return _visible_models(resp)


def _raise_probe_failure(resp: httpx.Response) -> None:
    """One distinct terminal message per failure class (issue 05's taxonomy) — the operator's
    next move differs per class, so the classes must not share wording.

    Order matters: the Cloudflare check must precede the auth check, because a CF challenge IS a
    403 — read auth-first, every challenged VPS would be told to sign in again, which cannot fix
    an IP.
    """
    status = resp.status_code

    # CF-challenge: keyed on the `Cf-Mitigated` marker, NEVER on CF-RAY (facts.md B4 — CF-RAY
    # rides every response, including 200s; keying on it would call every success a block).
    # This branch is live-UNVERIFIED (no challenge was drawable from a residential IP), so the
    # message states the mechanism observed, not more.
    if status == 403 and resp.headers.get("cf-mitigated") is not None:
        raise SystemExit(
            "The codex backend's edge (Cloudflare) challenged this machine's egress IP "
            "(HTTP 403 + Cf-Mitigated), so this seat cannot be served from here — "
            "datacenter/VPS addresses are typically blocked. Nothing was joined. "
            "Serve the seat from a residential connection, or change this machine's egress IP."
        )

    if status in (401, 403):
        raise SeatRejected(status)

    if status == 429:
        raise SystemExit(
            "The codex backend says this seat is currently rate-limited (HTTP 429). Nothing was "
            "joined. Wait for the seat's limit window to pass, then re-run `grid join --api codex`."
        )

    if status >= 500:
        raise SystemExit(
            f"The codex backend is unavailable (HTTP {status}) — a vendor outage, not a problem "
            "on this machine or with your seat. Nothing was joined. Try again later."
        )

    # 400 and anything else: the vendor refused the probe request itself. NEVER worded as a tier
    # problem — the vendor's own refusals name the auth mode, not the tier (facts.md #5) — and a
    # 400 on this free GET means the pinned contract (the `client_version` query) drifted.
    raise SystemExit(
        f"The codex backend refused the join probe (HTTP {status}{_bounded_detail(resp)}). "
        "Nothing was joined. This usually means the vendor changed its API contract — check for "
        "a newer grid release."
    )


def _bounded_detail(resp: httpx.Response) -> str:
    """The vendor's ``detail`` string, bounded for a terminal — or nothing.

    Vendor text at best, so it is length-capped and printable-checked before it may ride an
    operator message (the `_safe` posture from remote/codex_oauth): an unbounded echo could carry
    ANSI escapes or newlines that forge lines around our own output.
    """
    try:
        doc = resp.json()
    except ValueError:
        return ""
    detail = doc.get("detail") if isinstance(doc, dict) else None
    if isinstance(detail, str) and detail and detail.isprintable() and len(detail) <= 200:
        return f": {detail}"
    return ""


def _readable_slug(model: object) -> bool:
    """Whether a row carries a usable model slug: a non-empty, PRINTABLE, length-bounded string. A
    row that fails this is treated exactly like one with no slug — dropped, and (if NO row anywhere
    is readable) surfaced as contract drift. Bounding + printable-checking at the ONE origin means a
    hostile/ANSI slug from a compromised or MITM'd backend can never ride the advertised name onto
    the operator's terminal (the join print / `-m` refusal) and forge lines or inject escapes
    (CWE-150) — the same posture `_bounded_detail` already takes for vendor `detail` text."""
    if not isinstance(model, dict):
        return False
    slug = model.get("slug")
    return isinstance(slug, str) and bool(slug) and slug.isprintable() and len(slug) <= _MAX_SLUG_LEN


def _visible_models(resp: httpx.Response) -> tuple[CodexModel, ...]:
    """The servable models of a 200 listing (slug + derived caps), defensively.

    The body is vendor JSON with ~42 fields per model; only the caps-critical fields are read, and
    nothing is indexed without a shape check — a vendor reshape must surface as the "unreadable
    listing" contract-drift error or a conservative caps claim, never as an exception escaping the
    failure taxonomy.
    """
    try:
        doc: Any = resp.json()
    except ValueError:
        raise _unreadable_listing() from None
    models = doc.get("models") if isinstance(doc, dict) else None
    if not isinstance(models, list):
        raise _unreadable_listing()

    # Drift vs empty is decided by READABILITY, not by what survives the membership filters: an
    # all-hidden (or all-unsupported) listing parsed perfectly and is a legitimate (if unusual) seat
    # state — sending its operator to "check for a newer grid release" would be a lie. Only a
    # non-empty listing in which NO row anywhere carries a readable slug is shape drift.
    readable = [model for model in models if _readable_slug(model)]
    if models and not readable:
        raise _unreadable_listing()

    picked: dict[str, CodexModel] = {}  # insertion-ordered dedupe, vendor order, first occurrence wins
    drifted: set[str] = set()
    for model in readable:
        # Membership (issue 10a / DESIGN §6): the hide-filter drops a model the vendor hides from its
        # own picker (codex-auto-review); the supported_in_api filter drops one the vendor EXPLICITLY
        # marks API-unsupported (US5). These two filters are now the SOLE structural guards — the
        # static tier intersection that used to backstop them is gone. If the vendor renames
        # `visibility` the hide-filter fails OPEN and the hidden model is served: visible damage (it
        # 400s per job), never silent, and never a model failed closed (DESIGN §8). An ABSENT
        # supported_in_api is served (Option A); only an explicit `False` excludes.
        if model.get("visibility") == "hide":
            continue
        if model.get("supported_in_api") is False:
            continue
        slug = model["slug"]
        if slug not in picked:  # first occurrence wins; a later dup is never re-parsed (nor re-checked)
            picked[slug] = _model_caps(model)
            drifted.update(_malformed_caps_fields(model))
    _warn_caps_drift(drifted)
    return tuple(picked.values())


def _warn_caps_drift(fields: set[str]) -> None:
    """One operator breadcrumb when a caps-critical field arrived PRESENT-but-unreadable across the
    listing (a vendor reshape). Per-field fail-closed is otherwise SILENT — a whole-listing field
    rename would strip vision/tools/context from every model with a "success" join and no signal
    (the membership set at least surfaces in the join's `models=` print; caps never do). Fires ONLY
    on genuine shape drift, never on real data or a legitimately absent (sparse) field."""
    if fields:
        print(
            "Note: the codex model listing carried capability field(s) "
            f"{', '.join(sorted(fields))} in a shape this grid can't read; those capabilities were "
            "treated conservatively for the affected models. A vendor API change may be "
            "under-reporting your models — check for a newer grid release.",
            file=sys.stderr,
        )


def _is_finite_number(value: Any) -> bool:
    """A real, finite number — the shape a numeric probe field must have before coercion. Excludes
    ``bool`` (``True``/``False`` are ``int`` in Python) and non-finite floats (Python's ``json``
    accepts ``Infinity``/``NaN``, and ``int(inf)`` raises ``OverflowError`` — which would escape the
    probe's failure taxonomy as a raw traceback). A huge arbitrary-precision ``int`` (e.g. a 309-digit
    JSON literal in a hostile vendor body or a corrupted `codex_models_cache.json`) ALSO overflows the
    float conversion inside ``math.isfinite`` — caught here so it reads as "not a usable number" (→ the
    ``0`` unknown sentinel), never a raw traceback that would break the probe's / the cache's contract."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _positive_int(value: Any) -> int:
    """A probe field coerced to a positive int, or the ``0`` unknown sentinel (→ omitted from the
    envelope downstream, never fabricated) when absent, non-numeric, non-finite, or non-positive."""
    return int(value) if _is_finite_number(value) and value > 0 else 0


def _flag(value: Any) -> bool:
    """A probe boolean, fail-closed: ``True`` ONLY for the JSON literal ``true``. Any other shape —
    a vendor reshaping the field to a truthy string (``bool("false")`` is ``True``!), a number, or
    absence — is the conservative ``False``, the same fail-closed direction as the numeric guard."""
    return value is True


def _malformed_caps_fields(model: dict[str, Any]) -> tuple[str, ...]:
    """Caps-critical fields PRESENT in a row but in an unreadable SHAPE — the vendor-reshape signal
    for ``_warn_caps_drift``. Distinct from a legitimately ABSENT field (sparse — no signal, no
    warn): a key that isn't there is normal; a key that IS there but the wrong type is drift."""
    bad: list[str] = []
    ctx = model.get("context_window")
    if ctx is not None and not _is_finite_number(ctx):
        bad.append("context_window")
    modalities = model.get("input_modalities")
    if modalities is not None and not isinstance(modalities, list):
        bad.append("input_modalities")
    for field in ("supports_parallel_tool_calls", "supports_search_tool"):
        value = model.get(field)
        if value is not None and not isinstance(value, bool):
            bad.append(field)
    return tuple(bad)


def _model_caps(model: dict[str, Any]) -> CodexModel:
    """Derive one model's caps from its probe row, fail-closed per field (never raises).

    ``context_window`` → a positive int, else ``0`` (unknown sentinel); vision → ``"image" in
    input_modalities``; tools → ``supports_parallel_tool_calls or supports_search_tool``, each
    accepted ONLY as a real JSON ``true`` (`_flag`). A field the vendor stops sending — or reshapes —
    yields the conservative claim; the model is still served (DESIGN §8). A present-but-unreadable
    caps field is additionally surfaced by `_malformed_caps_fields` → `_warn_caps_drift`."""
    modalities = model.get("input_modalities")
    return CodexModel(
        slug=model["slug"],
        context_window=_positive_int(model.get("context_window")),
        supports_vision=isinstance(modalities, list) and "image" in modalities,
        supports_tools=_flag(model.get("supports_parallel_tool_calls"))
        or _flag(model.get("supports_search_tool")),
    )


def _unreadable_listing() -> SystemExit:
    return SystemExit(
        "The codex backend returned a model listing this version of grid can't read. Nothing was "
        "joined. This usually means the vendor changed its API contract — check for a newer grid "
        "release."
    )
