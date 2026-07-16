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

from typing import Any

import httpx

from . import codex_oauth

# One request on a human-watched path: a hung socket must surface as an error, not hang the join.
# Matches `_VENDOR_LIST_TIMEOUT` (the openai join call) and `_EXCHANGE_TIMEOUT` (the sign-in).
_PROBE_TIMEOUT = 15.0


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
) -> tuple[str, ...]:
    """The seat's visible model slugs, or why this machine cannot serve it.

    One GET, no retry. Raises ``SeatRejected`` for the auth class and ``SystemExit`` (a terminal
    operator message ending "Nothing was joined.") for every other failure. The slugs come back
    deduped in vendor order; ``visibility: "hide"`` rows are dropped — a model the vendor hides
    from its own client must never be advertised to a grid (facts.md #5, ``codex-auto-review``).
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
    return _visible_slugs(resp)


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


def _visible_slugs(resp: httpx.Response) -> tuple[str, ...]:
    """The visible model slugs of a 200 listing, defensively.

    The body is vendor JSON with ~42 fields per model; only ``slug`` and ``visibility`` are read,
    and nothing is indexed without a shape check — a vendor reshape must surface as the
    "unreadable listing" contract-drift error, never as a KeyError escaping the taxonomy.
    """
    try:
        doc: Any = resp.json()
    except ValueError:
        raise _unreadable_listing() from None
    models = doc.get("models") if isinstance(doc, dict) else None
    if not isinstance(models, list):
        raise _unreadable_listing()

    # Drift vs empty is decided by READABILITY, not by what survives the hide-filter: an
    # all-hidden listing parsed perfectly and is a legitimate (if unusual) seat state — sending
    # its operator to "check for a newer grid release" would be a lie. Only a non-empty listing
    # in which NO row anywhere carries a readable slug is shape drift (silent-failure review #1).
    readable = [
        model for model in models
        if isinstance(model, dict) and isinstance(model.get("slug"), str) and model["slug"]
    ]
    if models and not readable:
        raise _unreadable_listing()

    slugs: dict[str, None] = {}  # insertion-ordered dedupe, vendor order preserved
    for model in readable:
        # The hide-filter is defence in depth, NOT the wall: if the vendor renames `visibility`
        # this check fails open, and what actually stops a hidden model being advertised is the
        # verified tier row the join intersects with (pinned by test). A hidden model that IS in
        # the tier row would then be advertised and fail per-job — visible damage, not silent.
        if model.get("visibility") != "hide":
            slugs.setdefault(model["slug"])
    return tuple(slugs)


def _unreadable_listing() -> SystemExit:
    return SystemExit(
        "The codex backend returned a model listing this version of grid can't read. Nothing was "
        "joined. This usually means the vendor changed its API contract — check for a newer grid "
        "release."
    )
