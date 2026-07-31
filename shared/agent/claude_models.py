"""Which concrete Claude models the installed `claude` binary serves.

Two local sources, neither of which calls a vendor API:

  `/model`  a local slash command (measured: 7ms, 0 tokens, $0, `num_turns: 0`) whose output
            names the tier aliases this CLI accepts.
  the executable  carries every concrete model id as a plain string in its bundle string pool.

The first says which tiers exist, the second turns a tier into a dated id. Neither list is written
down here, so a new Claude Code release brings its new models along without an edit.

Design and the measurements behind each rule:
docs/superpowers/specs/2026-07-31-claude-seat-dynamic-models-design.md
"""
from __future__ import annotations

import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from shared.paths import grid_home

# Same shape as the seat's QUOTA_ARGV: a local slash command, no model turn, no cost. Shared by
# both probes here so the two can never drift into asking under different conditions.
_PROBE_ARGV = ("--output-format", "json", "--safe-mode", "--tools", "",
               "--no-session-persistence")

MODELS_ARGV = ("-p", "/model", *_PROBE_ARGV)

_AVAILABLE = re.compile(r"Available:\s*([^\n]+)")
_BRACKET = re.compile(r"\[[^\]]*\]$")   # opus[1m] -> opus


def available_tiers(text):
    """The alias names `/model` lists, in the order printed.

    `[1m]`-style suffixes are stripped and NOT recorded: `--model opus[1m]` is known to work, but
    the full id carrying the suffix has never been measured, so claiming a 1M window while
    invoking the bare id could advertise a window the request does not get.

    The trailing "or a full model ID" is prose, not an alias — it is dropped by the space test,
    which is also why no alias containing a space can ever be served.
    """
    found = _AVAILABLE.search(text or "")
    if found is None:
        return []
    tiers = []
    for raw in found.group(1).split(","):
        name = _BRACKET.sub("", raw.strip().rstrip("."))
        if name and " " not in name and name not in tiers:
            tiers.append(name)
    return tiers


def _version(raw):
    """`4-1-20250805` -> `(4, 1)`.

    A trailing segment of exactly eight digits is a release date, not a version component. Left
    in, `claude-opus-4-1-20250805` would read `(4, 1, 20250805)` and outrank `claude-opus-5`.
    """
    parts = [int(part) for part in raw.split("-")]
    if len(parts) > 1 and len(str(parts[-1])) == 8:
        parts = parts[:-1]
    return tuple(parts)


def ranked_ids(blob, tiers):
    """`{tier: [ids, newest first]}` for whichever of `tiers` the blob carries.

    One pass over the whole blob, with the tiers alternated inside a single pattern. Scanning
    per-tier instead costs a full 266MB pass each — measured 6.5s a pass, 47s for four.

    Both anchors are load-bearing. The leading `[^a-z0-9.-]` rejects the Bedrock and Vertex
    spellings (`us.anthropic.claude-opus-5`, `anthropic.claude-opus-5`); the trailing
    `[^a-z0-9-]` rejects the `-fast` and `-v1` builds. A tier with no id at all — `best`,
    `opusplan`, `default` — simply never matches and is absent from the result, which is why none
    of those three is named anywhere in this file.

    Ties go to the undated spelling: `claude-haiku-4-5` and `claude-haiku-4-5-20251001` both read
    `(4, 5)`, and the undated one is the alias that follows its tier forward.

    A whole ranked list rather than one winner, because the newest id a build carries is not
    always one the account may call — see `accepts` and `discover`.
    """
    if not tiers:
        return {}
    alternation = b"|".join(re.escape(tier.encode()) for tier in tiers)
    pattern = re.compile(
        rb"[^a-z0-9.-](claude-(" + alternation + rb")-(\d+(?:-\d+)*))[^a-z0-9-]"
    )
    found = {}
    for match in pattern.finditer(blob):
        tier = match.group(2).decode()
        found.setdefault(tier, {})[match.group(1).decode()] = _version(match.group(3).decode())
    return {
        tier: sorted(ids, key=lambda ident: (ids[ident], -len(ident)), reverse=True)
        for tier, ids in found.items()
    }


# `/model <name>` answers one of three ways, and only the first means "you may call this":
#     Set model to Opus 5 for this session only
#     Model 'claude-opus-6' not found                            -> no such model
#     Mythos 5 isn't available for your account yet. Run /model  -> real, but not for this account
# The third is the one no amount of reading the binary could have told us: entitlement is an
# account fact, and the binary is a proven superset of what an account may call.
_ACCEPTED = re.compile(r"^Set model to ", re.M)


def accepts(binary, model_id):
    """True iff this CLI will actually serve `model_id` for the signed-in account.

    Costs one local slash command, measured at ~11ms and $0 — no model turn. `--no-session-persistence`
    is what makes "for this session only" a no-op: nothing is written back.

    Unreadable output reads as False. That direction matters: a seat that advertises a model it
    cannot serve fails every request into that tier, while one that skips a model it could have
    served merely offers less.
    """
    try:
        proc = subprocess.run([binary, "-p", f"/model {model_id}", *_PROBE_ARGV],
                              capture_output=True, text=True, timeout=60)
        envelope = json.loads(proc.stdout or "")
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    if not isinstance(envelope, dict):
        return False
    return bool(_ACCEPTED.search(str(envelope.get("result") or "")))


def executable(binary):
    """The file to scan, or None.

    `binary` is whatever resolved on PATH, which is not necessarily the executable: a
    cmux-managed entry is a twenty-line bash shim and scanning it finds nothing. Ask the CLI which
    version is running and index the native install by it — deliberately the RUNNING version and
    not the newest installed one, since several sit side by side under `versions/` and scanning
    the wrong one would advertise models the serving binary may not have.
    """
    try:
        proc = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    version = (proc.stdout or "").strip().split(" ")[0]
    if not version:
        return None
    path = Path.home() / ".local" / "share" / "claude" / "versions" / version
    return path if path.is_file() else None


def _cache_file():
    return grid_home() / "claude-models.json"


def _cache_key(path):
    stat = path.stat()
    return f"{path}:{stat.st_size}:{int(stat.st_mtime)}"


def _read_cache(key):
    """The cached ids for this executable, or None to force a rescan.

    An EMPTY stored list reads as a miss, not as "this binary serves nothing". Nothing writes one
    any more (see `discover`), but machines that ran the version which did are still carrying one,
    and a stored `[]` was a permanent cache HIT — discovery never re-probed, so the seat advertised
    the static fallback for the life of that executable. Treating it as a miss heals those boxes on
    their next join rather than requiring the file be deleted by hand.
    """
    try:
        stored = json.loads(_cache_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(stored, dict) or stored.get("key") != key:
        return None
    models = stored.get("models")
    return models if isinstance(models, list) and models else None


def _write_cache(key, models):
    cache = _cache_file()
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"key": key, "models": models}), encoding="utf-8")
    except OSError:
        pass   # an unwritable home costs a rescan, never an answer


def _ask_tiers(binary):
    try:
        proc = subprocess.run([binary, *MODELS_ARGV], capture_output=True, text=True, timeout=60)
        envelope = json.loads(proc.stdout or "")
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    if not isinstance(envelope, dict):
        return []
    return available_tiers(str(envelope.get("result") or ""))


def discover(binary):
    """The concrete model ids this `claude` serves, newest per tier, in `/model` order.

    Returns `[]` when anything is unavailable — no binary, no native install, unparseable output.
    A caller that gets `[]` keeps its static list, so this can never block a join.

    The cache is keyed on the executable's identity, and the `/model` probe sits INSIDE the miss
    path: a cache hit costs the one `claude --version` needed to find the executable, and no
    second spawn. Each spawn of a 266MB binary is ~1s, so which side of the cache they fall on is
    the difference between a join that waits and one that does not.
    """
    path = executable(binary)
    if path is None:
        return []
    try:
        key = _cache_key(path)
    except OSError:
        return []
    cached = _read_cache(key)
    if cached is not None:
        return cached
    tiers = _ask_tiers(binary)
    if not tiers:
        return []
    try:
        ranked = ranked_ids(path.read_bytes(), tiers)
    except OSError:
        return []
    def first_callable(tier):
        """Walk DOWN the tier until one id is actually callable. Normally the first try wins and
        this is a single probe; during a rollout that ships an id before enabling it, the walk
        lands on the newest the account may really use instead of failing the whole tier."""
        for model_id in ranked.get(tier, ()):
            if accepts(binary, model_id):
                return model_id
        return None

    # One thread per tier. Each probe is a separate `claude` process and spawning that binary
    # costs ~1s of wall clock whatever the command does, so four sequential walks turned an 8.6s
    # cold discovery into 23s. They share nothing, so running them together costs one spawn.
    with ThreadPoolExecutor(max_workers=max(1, len(tiers))) as pool:
        models = [found for found in pool.map(first_callable, tiers) if found is not None]
    # Only a NON-EMPTY result is worth remembering. Every way this list comes back empty is
    # transient — `accepts` reads an unparseable envelope as False, so one expired login, one rate
    # limit, one timeout on a 266MB spawn empties it — while the cache key is the executable's
    # identity, which does not change when the transient condition clears. Caching `[]` therefore
    # pinned the seat to the static fallback until the binary was next upgraded. Paying for a
    # rescan on each join is the cheaper side of that trade by a wide margin.
    if models:
        _write_cache(key, models)
    return models
