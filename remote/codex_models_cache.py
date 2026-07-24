"""The grid-side codex model cache (issue 10b) — ``~/.grid/codex_models_cache.json``.

``grid catalog --api codex`` writes the seat's live ``GET /models`` probe result here after every
successful probe (the join probe too — ``cli/remote_provider``), and reads it back when the seat is
offline / the probe fails, so the catalog can still show the seat's LAST-KNOWN real set rather than
only the illustrative static reference. It mirrors the real codex CLI's ``~/.codex/models_cache.json``
mechanism — a per-seat cache keyed on the pinned ``client_version`` — MINUS the ``etag`` (the grid
probes rarely, so it needs no conditional GET), and stores only the DERIVED caps ``probe_seat`` already
returns (``CodexModel``), not the raw vendor rows (DESIGN §7 as refined by 10b's grill).

Safety, both directions fail SOFT — the catalog must NEVER die on its own cache (issue 10b Q2):
a write failure never fails the join or the catalog (best-effort), and a corrupt / absent / stale
file reads back as ``None`` (the catalog then falls to the static reference). The file is the seat's
entitlement — mildly sensitive but NEVER a token: it carries no access token and no RAW account id
(only slugs + caps + a one-way account FINGERPRINT that scopes it to the seat), and is written
``0o600`` through the same hardened atomic writer as the credential store. A grid upgrade bumps
``client_version``, so a cache from an older grid is treated as stale.
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from dataclasses import dataclass

from shared import jsonio, paths

# The probe's fail-closed coercions are the canonical ones; the cache read applies exactly the same
# discipline (a hand-edited/corrupted file is untrusted input like a vendor body) so a cached row and
# a freshly-probed row can never be interpreted differently.
from .codex_probe import (
    CodexModel,
    _flag,
    _is_finite_number,
    _positive_int,
    _readable_slug,
)


@dataclass(frozen=True)
class CachedModels:
    """A past successful probe, read back for an offline ``grid catalog --api codex``."""

    fetched_at: float  # POSIX seconds the probe was written; shown to the operator as "cached <when>"
    models: tuple[CodexModel, ...]


def _account_fingerprint(account_id: str) -> str:
    """A one-way SHA-256 fingerprint of the seat's account id — NEVER the id itself. It scopes the
    cache to the seat that wrote it, so an offline `grid catalog --api codex` after switching seats on
    one box shows the illustrative reference rather than the OTHER seat's models under this seat's plan
    label (issue 10b). A hash, not the raw id, keeps the file free of any recoverable account identifier
    (the stated privacy goal); on the seat's own `0o600` file it is a scoping key, not a secret."""
    return hashlib.sha256(account_id.encode("utf-8")).hexdigest()


def write_cache(models: tuple[CodexModel, ...], *, client_version: str, account_id: str) -> None:
    """Persist a successful probe (``0o600``), best-effort, scoped to the signed-in seat.

    Called after every successful probe (join + catalog-online). A write failure — a read-only home, a
    full disk — must NEVER fail the caller (the join already succeeded; the catalog already has its
    answer), so an ``OSError`` is swallowed with one operator breadcrumb. Stores only slugs + derived
    caps + a one-way account FINGERPRINT — never a token and never the raw account id.
    """
    payload = {
        "fetched_at": time.time(),
        "client_version": client_version,
        "account": _account_fingerprint(account_id),
        "models": [
            {
                "slug": m.slug,
                "context_window": m.context_window,
                "vision": m.supports_vision,
                "tools": m.supports_tools,
            }
            for m in models
        ],
    }
    try:
        jsonio.atomic_write_json(paths.codex_models_cache_file(), payload)
    except OSError as exc:
        print(
            f"Note: could not write the codex model cache ({exc}); an offline "
            "`grid catalog --api codex` will fall back to the illustrative reference.",
            file=sys.stderr,
        )


def read_cache(*, client_version: str, account_id: str) -> CachedModels | None:
    """The last cached probe for THIS ``client_version`` AND this seat, or ``None``.

    Deliberately does its OWN defensive read rather than ``jsonio.load_json``: that helper raises
    ``SystemExit`` on a malformed/unreadable file and returns ``{}`` for an absent one, either of which
    would crash ``grid catalog --api codex`` — but the catalog must never die on its cache (issue 10b
    Q2). Every failure mode — absent, unreadable, non-dict, wrong ``client_version``, a fingerprint from
    a DIFFERENT seat, or a non-empty listing whose rows are ALL unreadable (corruption, mirroring the
    live probe's contract-drift guard) — collapses to ``None``, and the caller falls back to the static
    reference. Individual bad rows in an otherwise-readable listing are dropped, not fatal (bad slug →
    dropped; bad caps field → the conservative claim), exactly as the probe does.
    """
    path = paths.codex_models_cache_file()
    try:
        with path.open("r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None  # absent, unreadable, or not valid JSON
    if not isinstance(doc, dict):
        return None
    if doc.get("client_version") != client_version:
        return None  # a cache from a different (older) grid — stale, never trusted
    if doc.get("account") != _account_fingerprint(account_id):
        return None  # written by a DIFFERENT seat on this box — never show one seat's set for another
    rows = doc.get("models")
    if not isinstance(rows, list):
        return None
    readable = [row for row in rows if _readable_slug(row)]
    if rows and not readable:
        # A non-empty listing where NO row is readable = corruption, NOT a legitimately-empty seat —
        # mirrors codex_probe._visible_models' drift guard, so cached-garbage and live-garbage are
        # interpreted identically (fall back to the illustrative reference, not "0 models").
        return None
    models = tuple(
        CodexModel(
            slug=row["slug"],
            context_window=_positive_int(row.get("context_window")),
            supports_vision=_flag(row.get("vision")),
            supports_tools=_flag(row.get("tools")),
        )
        for row in readable
    )
    fetched_at = doc.get("fetched_at")
    return CachedModels(
        fetched_at=float(fetched_at) if _is_finite_number(fetched_at) else 0.0,
        models=models,
    )
