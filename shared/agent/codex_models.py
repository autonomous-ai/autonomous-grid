"""Which models the signed-in `codex` account can actually use.

Asked, not hardcoded. The app-server answers `model/list` from the account itself, so a model added
by the vendor arrives without an edit here — the static list had already fallen behind by two
(`gpt-5.6-sol`, the one Codex CLI picks by DEFAULT, and `gpt-5.4`).

Cached per account: the answer is the same for every seat on this machine and asking costs an
app-server spawn. `hidden` models are dropped, which is what the static list was approximating by
hand when it excluded `codex-auto-review`.
"""
from __future__ import annotations

import json
import time

from shared.paths import grid_home

CACHE_TTL = 24 * 3600


def _cache_file():
    return grid_home() / "codex-models.json"


def _read_cache():
    try:
        stored = json.loads(_cache_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    models = stored.get("models")
    if not isinstance(models, list) or not models:
        return None   # never treat an empty result as a hit; it would pin the seat at zero models
    if time.time() - float(stored.get("fetched_at") or 0) > CACHE_TTL:
        return None
    return models


def _write_cache(models):
    try:
        path = _cache_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"fetched_at": time.time(), "models": models}),
                        encoding="utf-8")
    except OSError:
        pass   # a cache that cannot be written is a slow seat, not a broken one


def usable(entries):
    """`model/list` data -> the ids worth serving, in the order the vendor gave them."""
    out = []
    for entry in entries or []:
        if not isinstance(entry, dict) or entry.get("hidden"):
            continue
        model_id = entry.get("id") or entry.get("model")
        if isinstance(model_id, str) and model_id and model_id not in out:
            out.append(model_id)
    return out


def discover(binary=None):
    """The account's model ids, or `[]` when the CLI cannot be asked.

    `[]` is the caller's cue to fall back to the static list — never a reason to serve nothing.
    """
    cached = _read_cache()
    if cached is not None:
        return cached

    server = None
    try:
        from shared.agent.seats import codex_appserver

        server = codex_appserver.start_app_server(binary=binary, timeout=60.0)
        models = usable((server.call("model/list", {}) or {}).get("data"))
    except Exception:  # noqa: BLE001 — discovery must never take the seat down with it
        return []
    finally:
        if server is not None:
            server.stop()

    if models:
        _write_cache(models)
    return models
