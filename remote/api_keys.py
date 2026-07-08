"""Machine-local API-engine key store: one vendor key per service kind (ADR 0012).

Persists to ``~/.grid/api_keys.toml`` (TOML, ``0o600``) through the same hardened writer as the
credential store, but is deliberately NOT part of it: ``grid logout`` deletes credentials.toml and
must leave the vendor key intact (the key belongs to the provider's vendor account, not to the
autonomous sign-in). One ``[<kind>]`` table per service kind with the key under ``key``, so later
per-kind metadata (e.g. an OpenAI-compatible ``base_url``) is a field addition, not a format change.

``store_key``'s read-merge-write is serialized under its own ``file_lock`` (the ADR 0010 pattern):
without it, two overlapping ``join --api`` runs for DIFFERENT kinds could each read before the
other's write landed and the loser's key would silently vanish from the whole-file rewrite. Reads
stay lock-free — the write is an atomic rename, so a read never sees a torn file. The
vendor-validation network call happens before the run-record lock is taken, so this store never
nests inside that lock (no ordering hazard).
"""
from __future__ import annotations

import contextlib

from shared import paths
from shared.filelock import file_lock

from . import credentials


def load_key(kind: str) -> str | None:
    """The stored key for one service kind, or None (no file / no entry)."""
    entry = credentials.load_toml(paths.api_keys_file()).get(kind)
    key = entry.get("key") if isinstance(entry, dict) else None
    return str(key) if key else None


def store_key(kind: str, key: str) -> None:
    """Persist one kind's key (0o600), preserving every other kind and future per-kind fields.

    Immutable update: a fresh dict is written, never the loaded one mutated in place.
    """
    with file_lock(paths.api_keys_file()):  # serialize the read-merge-write (see module docstring)
        data = credentials.load_toml(paths.api_keys_file())
        existing = data.get(kind)
        merged_kind = {**(existing if isinstance(existing, dict) else {}), "key": key}
        credentials.atomic_write_toml(paths.api_keys_file(), {**data, kind: merged_kind})
    # Best-effort: keep the home dir owner-only (mirrors credentials.save_credentials), so a local
    # user can't even list that a key file exists — even when THIS write is what created ~/.grid.
    with contextlib.suppress(OSError):
        paths.grid_home().chmod(0o700)
