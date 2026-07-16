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
        bundle = load_codex_bundle()
        if bundle is None:
            # Deliberately names no environment variable: for an OAuth seat there is none, and
            # offering one would invite exactly the spoof D-c exists to prevent.
            raise SystemExit(
                "This engine serves --api codex models but this machine is not signed in to a "
                "codex subscription. Re-run `grid join --api codex` to sign in."
            )
        return bundle.access_token

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


CODEX_KIND = "codex"


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
    mix of the two rotations is not a credential the vendor would honour.
    """
    entry = {
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
    # Replaced wholesale rather than merged into the previous codex entry: a stale field left behind
    # by an older grid version must not survive a re-sign-in inside a bundle it doesn't belong to.
    _merge_kind(CODEX_KIND, entry, replace=True)


def _merge_kind(kind: str, entry: dict[str, object], *, replace: bool = False) -> None:
    """Write one kind's entry, leaving every other kind's untouched.

    Immutable update: a fresh dict is written, never the loaded one mutated in place.
    """
    with file_lock(paths.api_keys_file()):  # serialize the read-merge-write (see module docstring)
        data = credentials.load_toml(paths.api_keys_file())
        existing = data.get(kind)
        base = {} if replace or not isinstance(existing, dict) else existing
        credentials.atomic_write_toml(paths.api_keys_file(), {**data, kind: {**base, **entry}})
    # Best-effort: keep the home dir owner-only (mirrors credentials.save_credentials), so a local
    # user can't even list that a credential file exists — even when THIS write created ~/.grid.
    with contextlib.suppress(OSError):
        paths.grid_home().chmod(0o700)
