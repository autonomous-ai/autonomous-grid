"""Machine-local API-engine credential store: one vendor credential per service kind (ADR 0012).

Persists to ``~/.grid/api_keys.toml`` (TOML, ``0o600``) through the same hardened writer as the
credential store, but is deliberately NOT part of it: ``grid logout`` deletes credentials.toml and
must leave the vendor credential intact (it belongs to the provider's vendor account, not to the
autonomous sign-in). One ``[<kind>]`` table per service kind, so per-kind metadata is a field
addition, not a format change.

**A kind's credential shape is its own** (ADR 0015 D-c). ``openai`` is one metered key under ``key``;
``codex`` is an OAuth bundle — access/refresh token, account id, tier, last-refresh — because a
subscription seat has no ``sk-...``. Callers that only need "the Bearer for this kind" ask
``require_bearer`` and never learn which shape they got; callers that manage the seat itself (the
sign-in, and ADR 0015 D-d's rotation) use the codex bundle pair.

``store_key``'s read-merge-write is serialized under its own ``file_lock`` (the ADR 0010 pattern):
without it, two overlapping ``join --api`` runs for DIFFERENT kinds could each read before the
other's write landed and the loser's key would silently vanish from the whole-file rewrite. Reads
stay lock-free — the write is an atomic rename, so a read never sees a torn file. The
vendor-validation network call happens before the run-record lock is taken, so this store never
nests inside that lock (no ordering hazard).
"""
from __future__ import annotations

import contextlib
import os
import threading
import time

from shared import paths
from shared.filelock import file_lock
from shared.models import api_catalog

from . import codex_oauth, credentials


def load_key(kind: str) -> str | None:
    """The stored key for one service kind, or None (no file / no entry).

    Key-shaped kinds only. A codex seat has no ``key`` field and reads back None here — ask
    ``require_bearer`` or ``load_codex_bundle`` instead.
    """
    entry = credentials.load_toml(paths.api_keys_file()).get(kind)
    key = entry.get("key") if isinstance(entry, dict) else None
    return str(key) if key else None


def require_bearer(kind: str) -> str:
    """The Bearer the forward path sends for one service kind, or a terminal error naming the fix.

    **The one place that knows a kind's credential shape.** Callers get a string whichever kind they
    hold, so the serve loop neither branches on kind nor consults the whitelist's env var — which is
    what keeps ADR 0015 D-c's "no env-var input path for codex" true by construction rather than by
    everyone remembering. The credential never appears in any message raised here.
    """
    if kind == CODEX_KIND:
        return require_codex_bundle().access_token

    whitelist = api_catalog.WHITELISTS.get(kind)
    # No synthesised `{KIND}_API_KEY` fallback for a kind the catalog doesn't name: guessing an env
    # var name is how a stray environment variable becomes a credential for a kind we know nothing
    # about. A kind with no whitelist row resolves from the store or not at all.
    env_var = whitelist.env_var if whitelist else None
    key = load_key(kind) or (os.environ.get(env_var) if env_var else None)
    if key:
        return key
    if env_var:
        raise SystemExit(
            f"This engine serves --api {kind} models but no key is stored and {env_var} is "
            f"not set. Re-run `grid join --api {kind}` to store a key (or export {env_var})."
        )
    raise SystemExit(
        f"This engine serves --api {kind} models but no key is stored. "
        f"Re-run `grid join --api {kind}` to store a key."
    )


def store_key(kind: str, key: str) -> None:
    """Persist one kind's key (0o600), preserving every other kind and future per-kind fields.

    Immutable update: a fresh dict is written, never the loaded one mutated in place.
    """
    _merge_kind(kind, {"key": key})


# Re-exported from the catalog (issue 05): shared/run_records' concurrency rule needs the kind
# key and shared/ must not import remote/, so the single definition lives with the whitelist.
CODEX_KIND = api_catalog.CODEX_KIND


def require_codex_bundle() -> codex_oauth.CodexBundle:
    """The stored codex seat, or the terminal error naming the fix (die-before-advertise).

    Deliberately names no environment variable: for an OAuth seat there is none, and offering one
    would invite exactly the spoof ADR 0015 D-c exists to prevent.
    """
    bundle = load_codex_bundle()
    if bundle is None:
        raise SystemExit(
            "This engine serves --api codex models but this machine is not signed in to a "
            "codex subscription. Re-run `grid join --api codex` to sign in."
        )
    return bundle


def load_codex_bundle() -> codex_oauth.CodexBundle | None:
    """The stored codex seat, or None when this box has never signed in.

    None also covers a *half-written* entry: every field but ``plan_type`` is required, so a bundle
    missing one is treated as absent rather than reconstructed with blanks. A blank access token
    would 401 every job; a blank refresh token would brick ADR 0015 D-d's rotation with no way back.
    "Not signed in" is a state the join can fix by signing in — a malformed bundle is not.
    """
    entry = credentials.load_toml(paths.api_keys_file()).get(CODEX_KIND)
    if not isinstance(entry, dict):
        return None
    access_token, refresh_token = entry.get("access_token"), entry.get("refresh_token")
    account_id, last_refresh = entry.get("account_id"), entry.get("last_refresh")
    if not all(isinstance(v, str) and v for v in (access_token, refresh_token, account_id)):
        return None
    # The account id is spent as an HTTP header value on every vendor call, and httpx will send a
    # CRLF-bearing header verbatim (facts.md B5b). `decode_seat` guards the SIGN-IN path; this
    # guards the LOAD path — the one every re-join takes — so header-safety travels with the
    # store rather than living in one caller three hops upstream (issue 05 security review).
    # An unusable id reads as "not signed in": the join then mints a clean bundle.
    if not str(account_id).strip() or not str(account_id).isprintable():
        return None
    plan_type = entry.get("plan_type")
    return codex_oauth.CodexBundle(
        access_token=str(access_token),
        refresh_token=str(refresh_token),
        account_id=str(account_id),
        # Absent means "the token never said" — ADR 0015 D-f's minimal whitelist. TOML has no null,
        # so absence is the ONLY way None survives a round-trip (see `store_codex_bundle`).
        plan_type=plan_type if isinstance(plan_type, str) else None,
        # `isinstance(True, int)` is True in Python, so an unguarded read would turn `last_refresh =
        # true` into 1 — epoch 1970 — and refresh eagerly forever (the guard `codex_auth` uses on `exp`).
        last_refresh=last_refresh if isinstance(last_refresh, int) and not isinstance(last_refresh, bool) else 0,
    )


def store_codex_bundle(bundle: codex_oauth.CodexBundle) -> None:
    """Persist the codex seat (0o600), preserving every other kind.

    The whole bundle is replaced as one unit: the access and refresh tokens are minted together and a
    mix of the two rotations is not a credential the vendor would honour. The wholesale replace is
    also what clears a leftover ``refresh_pending_since`` journal — a fresh sign-in resolves any
    in-doubt rotation by definition.
    """
    # Replaced wholesale rather than merged into the previous codex entry: a stale field left behind
    # by an older grid version must not survive a re-sign-in inside a bundle it doesn't belong to.
    _merge_kind(CODEX_KIND, _codex_entry(bundle), replace=True)


def _codex_entry(bundle: codex_oauth.CodexBundle) -> dict[str, object]:
    """The codex seat as a TOML table — shared by ``store_codex_bundle`` and the rotation's persist
    so the two writers cannot drift."""
    entry: dict[str, object] = {
        "access_token": bundle.access_token,
        "refresh_token": bundle.refresh_token,
        "account_id": bundle.account_id,
        "last_refresh": bundle.last_refresh,
    }
    # TOML has no null. Writing `plan_type` only when the token stated one keeps absence meaning
    # "unknown tier" on the way back in; `tomli_w` would raise on a None anyway, so a stringified
    # "None" is the shape this omission exists to prevent.
    if bundle.plan_type is not None:
        entry["plan_type"] = bundle.plan_type
    return entry


# The rotation journal (ADR 0015 D-d): present in the codex entry while an exchange is in flight
# and never afterwards — every persisted bundle is a wholesale replace, so a leftover key can only
# mean an exchange died between the vendor call and the write. PRESENCE is the signal; the value
# (POSIX seconds) is forensic only. `load_codex_bundle` reads fields by name, so the key never
# affects bundle validity.
_CODEX_JOURNAL_KEY = "refresh_pending_since"


class CodexNotSignedIn(Exception):
    """No usable codex seat in the store — signed out, never signed in, or half-written."""


class RotationAbandoned(Exception):
    """Shutdown won the race to the store lock: nothing was journaled and nothing was spent."""


class CodexRotationRefused(Exception):
    """The vendor refused to rotate the stored seat — only a fresh sign-in can revive it.

    ``interrupted`` is True when a journal from a PREVIOUS attempt was already present when this
    one started: an earlier exchange died between the vendor call and the persist, so the stored
    refresh token was likely spent — the diagnosis ADR 0015 D-d's journal exists to make possible.
    Carries no token and no vendor body; the operator wording is the caller's to compose.
    """

    def __init__(self, status_code: int, reason: str, *, interrupted: bool) -> None:
        super().__init__(f"HTTP {status_code} ({reason})")
        self.status_code = status_code
        self.reason = reason
        self.interrupted = interrupted


def rotate_codex_bundle(
    stale_access_token: str,
    *,
    exchange_in_flight: threading.Event | None = None,
    abandon: threading.Event | None = None,
) -> codex_oauth.CodexBundle:
    """One cross-process compare-and-swap rotation of the codex seat (ADR 0015 D-d).

    The vendor call happens UNDER the store's file lock — deliberately: N serve processes share
    ONE single-use refresh token, and the lock is what makes "exactly one exchange, the losers
    adopt" true across processes (the losers block on the flock, re-read, and see the winner's
    fresher token). The named cost: any other api_keys write on this box waits out the exchange
    (bounded by codex_oauth._REFRESH_TIMEOUT).

    ``exchange_in_flight`` is published BEFORE the first side-effect and cleared in ``finally`` —
    the shutdown drain waits on it, so "flag unset" must mean "nothing was spent and nothing will
    be" (a worker that passed the ``abandon`` re-check below has already published the flag).
    ``abandon`` (the serve loop's stop event) is re-checked under the lock so no journal is ever
    written after shutdown began.

    Returns the fresh bundle (already persisted). Raises ``CodexNotSignedIn``,
    ``RotationAbandoned``, ``CodexRotationRefused`` (definitive vendor no — carries the
    ``interrupted`` diagnosis), or re-raises ``codex_oauth.RefreshUnavailable`` (transient; the
    journal stays unless the request provably never left this machine).

    NEVER call the locked writers (``store_codex_bundle``/``_merge_kind``) from inside: the store
    lock is flock-based and NOT reentrant — a nested acquire self-deadlocks. Everything in here
    writes through ``_write_kind_unlocked``.
    """
    with file_lock(paths.api_keys_file()):
        stored = load_codex_bundle()
        if stored is None:
            raise CodexNotSignedIn("the stored codex seat is gone")
        if stored.access_token != stale_access_token:
            return stored  # someone else already rotated — adopt, spend nothing (the CAS)
        if exchange_in_flight is not None:
            exchange_in_flight.set()  # before ANY side-effect, so the drain never sees a gap
        try:
            if abandon is not None and abandon.is_set():
                raise RotationAbandoned()
            raw = credentials.load_toml(paths.api_keys_file()).get(CODEX_KIND)
            journal_was_present = isinstance(raw, dict) and _CODEX_JOURNAL_KEY in raw

            def _withdraw_own_journal() -> None:
                # Scoped to OUR OWN journal: when a journal predated this attempt, it is an
                # EARLIER crash's unresolved doubt and only a persisted bundle may clear it —
                # erasing it here would downgrade the next refusal's diagnosis from "a rotation
                # was lost" to a plain revocation. When the journal is ours alone, `raw` IS the
                # pre-journal entry (read under this same lock hold), so writing it back verbatim
                # withdraws exactly what we added — no re-read, no filter.
                if not journal_was_present and isinstance(raw, dict):
                    _write_kind_unlocked(CODEX_KIND, dict(raw), replace=True)

            _write_kind_unlocked(CODEX_KIND, {_CODEX_JOURNAL_KEY: int(time.time())})
            try:
                fresh = codex_oauth.refresh_bundle(stored)
            except codex_oauth.RefreshRefused as exc:
                # A definitive refusal is a COMPLETED attempt — nothing died mid-flight — so it
                # withdraws its own journal; left behind, attempt 1's pre-call write would become
                # attempt 2's "a prior exchange died" evidence and a stably-dead seat would be
                # misdiagnosed as a lost rotation from the second refusal on. A journal that
                # PREDATED this attempt stays (the earlier crash's doubt keeps resurfacing until
                # a bundle persists — issue 05's posture), and is what `interrupted` reports.
                _withdraw_own_journal()
                raise CodexRotationRefused(
                    exc.status_code, exc.reason, interrupted=journal_was_present
                ) from None
            except codex_oauth.RefreshUnavailable as exc:
                if not exc.request_sent:
                    # The exchange provably never left this machine — nothing was spent, so OUR
                    # journal is withdrawn: left behind, it would sharpen a WRONG "rotation was
                    # lost" diagnosis out of an ordinary offline blip. An ambiguous failure
                    # (request_sent unknown/True) keeps it, truthfully — the grant may have
                    # reached the vendor and died on the way back.
                    _withdraw_own_journal()
                raise
            _write_kind_unlocked(CODEX_KIND, _codex_entry(fresh), replace=True)
            return fresh
        finally:
            if exchange_in_flight is not None:
                exchange_in_flight.clear()


def _merge_kind(kind: str, entry: dict[str, object], *, replace: bool = False) -> None:
    """Write one kind's entry, leaving every other kind's untouched.

    Immutable update: a fresh dict is written, never the loaded one mutated in place.
    """
    with file_lock(paths.api_keys_file()):  # serialize the read-merge-write (see module docstring)
        _write_kind_unlocked(kind, entry, replace=replace)


def _write_kind_unlocked(kind: str, entry: dict[str, object], *, replace: bool = False) -> None:
    """The read-merge-atomic-write of one kind's entry. **The caller must hold the store's file
    lock.** Split from ``_merge_kind`` because the lock is flock-based and NOT reentrant (each
    acquisition opens a fresh fd, and flock treats same-process fds as independent holders) — the
    rotation CAS holds the lock across its whole read→journal→exchange→persist sequence and must
    write through this layer, never through the locked one."""
    data = credentials.load_toml(paths.api_keys_file())
    existing = data.get(kind)
    base = {} if replace or not isinstance(existing, dict) else existing
    credentials.atomic_write_toml(paths.api_keys_file(), {**data, kind: {**base, **entry}})
    # Best-effort: keep the home dir owner-only (mirrors credentials.save_credentials), so a local
    # user can't even list that a credential file exists — even when THIS write created ~/.grid.
    with contextlib.suppress(OSError):
        paths.grid_home().chmod(0o700)
