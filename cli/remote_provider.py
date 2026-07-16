"""Remote-mode `grid join` / `grid leave` — serve one engine to the active remote grid's relay.

Mirrors the local handlers (`cli/provider.py`) but resolves the grid + relay address from the remote
credential store and spawns the remote serve loop (`__remote-engine` → `remote/serve.py`) instead of
the local heartbeat loop. The engine record + teardown are the shared ones (`shared/run_records.py`),
so `grid leave` works the same in both modes. `grid join --all` serves several local engines under
one identity (the union of their models, model→engine routing — DECISIONS D9 / ADR 0007); local spawns
one identity per engine instead.

Import rule mirrors `cli/remote_grid.py`: only stdlib + `shared.*` at module top; `remote.*` and the
local runtime are imported lazily inside each handler, because `cli.dispatch` imports this module
while the `cli` package is still initialising.
"""
from __future__ import annotations

import argparse
import getpass
import os
import signal
import socket
import subprocess
import sys
import time
import uuid

from shared import logging_setup, paths, run_records
from shared.filelock import file_lock
from shared.models import api_catalog

# Remote has exactly ONE identity per grid: the relay node_id is pinned to the per-grid access token
# (remote/serve._node_id_from_token), so two `grid join`s on a grid would register the same node and
# clobber each other. The run record is therefore a singleton keyed by this constant — one file
# engines_dir(<network_id>)/remote.json — and repeated joins are additive (ADR 0010). `--name` no longer
# keys the record (it can't mint a second identity); it is the grid-page display name (record["meta_name"]).
_REMOTE_IDENTITY = "remote"

# One-shot vendor model-listing call at `join --api` (key validation + whitelist intersection).
_VENDOR_LIST_TIMEOUT = 15.0


def _reject_local_only_flags(args: argparse.Namespace) -> None:
    """local-only `grid join` flags have no meaning in remote mode (DECISIONS D8): a remote engine
    polls the relay outbound, so there is no inbound endpoint to advertise. (`--media` IS supported —
    a remote media engine's server is reached by the serve loop on loopback, not advertised.)"""
    if getattr(args, "advertise_host", None) is not None:
        raise SystemExit(
            "--advertise-host is local-only. A remote engine polls the relay outbound, so there is "
            "no inbound endpoint to advertise."
        )


def _warn_deprecated(triggered: bool, message: str) -> None:
    """Print a one-line deprecation note to stderr when a deprecated flag was used."""
    if triggered:
        print(message, file=sys.stderr)


def _reject_api_conflicts(args: argparse.Namespace) -> None:
    """Grammar for ``grid join --api <kind>`` (ADR 0012): one API engine per invocation. The
    hardware/media selectors don't combine with it (additive joins cover serving both), and
    aliasing never applies — the namespaced whitelist names ARE the advertised names. ``-m`` is
    optional: omitted, the join serves the whole whitelist the key can see (zero-config default)."""
    kind = args.api
    if kind not in api_catalog.WHITELISTS:
        supported = ", ".join(api_catalog.supported_kinds())
        raise SystemExit(f"Unknown API kind {kind!r}. Supported: {supported}")
    conflicts = (
        ("at", "--at"),
        ("serve", "--serve"),
        ("advertise_as", "--advertise-as"),
        ("media", "--media"),
        ("bundles", "--bundle"),
    )
    used = [flag for attr, flag in conflicts if getattr(args, attr, None)]
    if used:
        raise SystemExit(
            f"--api joins one API engine and can't combine with {', '.join(used)} in the same "
            "invocation. Join other engines with a separate `grid join`."
        )
    models = list(getattr(args, "models", []) or [])
    if any("=" in model for model in models):
        raise SystemExit(
            "API-engine models are advertised under their whitelist names — inline `-m real=alias` "
            "aliasing doesn't apply with --api."
        )


def cmd_remote_join(args: argparse.Namespace) -> int:
    from remote import credentials

    from . import provider, remote_grid

    _reject_local_only_flags(args)
    if getattr(args, "api", None) is not None:  # `--api ""` must error, not fall through to hardware
        _reject_api_conflicts(args)
    if args.serve and args.models:
        raise SystemExit("--serve serves one built-in model; drop -m/--model (alias a built-in with --advertise-as).")
    provider._apply_inline_aliases(args)
    _warn_deprecated(
        getattr(args, "pricing_input", None) is not None or getattr(args, "pricing_output", None) is not None,
        "Note: --pricing-input/--pricing-output are deprecated and no longer advertise a price. "
        "Set your model price with `grid price set` after joining.",
    )
    _warn_deprecated(
        getattr(args, "engine_label", None) is not None,
        "Note: --engine-label is deprecated and no longer changes the grid page — the engine's kind "
        "is derived automatically. (It still matches `grid leave --engine <label>`.)",
    )
    if args.at and args.serve:
        raise SystemExit("Use either --at (point at an existing engine) or --serve, not both.")

    session = credentials.require_session()
    rec = remote_grid._select(getattr(args, "grid", None))
    network_id = remote_grid._network_id(rec)
    label = rec.get("name") or network_id
    if not rec.get("access_token"):
        raise SystemExit(
            f"Grid {label} has no access token locally. Run `grid login` to refresh your grids."
        )

    # Resolve the relay address (works for a member, not just the creator; a stopped grid that a
    # member can't pre-check fails later at register). See remote_grid.resolve_relay_base.
    signaling_url, _status = remote_grid.resolve_relay_base(session, rec, network_id, label)

    key_rotated = False  # a `join --api` that stored a NEW key must reach a live identity via respawn
    if getattr(args, "api", None) is not None:
        specs, key_rotated = _resolve_api_targets(args)
        media_detected = False
    else:
        specs, media_detected = _resolve_serve_targets(args)
    media = bool(getattr(args, "media", False)) or media_detected
    if not specs and not media:  # engines detected and the operator declined, or nothing to serve
        print("Nothing joined.")
        return 0
    engine_id = _REMOTE_IDENTITY
    meta_name = getattr(args, "name", None) or socket.gethostname()

    # Remote has ONE identity per grid (the token pins the relay node_id), so `grid join` is additive:
    # merge this join's engines into whatever is already serving, then respawn the single detached engine.
    # The read-merge-write is serialized so two concurrent joins can't lost-update the union (ADR 0010).
    with file_lock(run_records.record_path(network_id, engine_id)):
        live = _live_records(network_id)  # normally just the singleton; also legacy `engine-<uuid>` on upgrade
        merged_specs, changed = _merge_engines(_engine_union(live), specs)
        # A rotated key only matters when this kind's API spec is already LIVE. A reload WOULD re-read the
        # key store and swap the bearer in place (issue 05), but rotation deliberately RESPAWNS so the
        # operator has certainty the new key is live — never a no-op, never SIGHUP. Kept at the call sites
        # (not inside `_hot_reloadable`) because leave-shrink shares that gate and rotation never applies there.
        rotated_live = key_rotated and any(
            spec.get("api_kind") == args.api for spec in _engine_union(live)
        )
        base_media = any(bool(rec.get("media")) for rec in live)
        media = base_media or media
        base_bundles = list(dict.fromkeys(b for rec in live for b in (rec.get("media_bundles") or [])))
        bundles = list(dict.fromkeys(base_bundles + list(getattr(args, "bundles", []) or [])))
        # An idempotent re-join (no new engine/model, and no display-name/media/bundle change) is a no-op,
        # so it doesn't needlessly restart a live identity. A change in ANY of those does respawn to apply
        # it — there is no other way to rename or add a bundle in Slice 1.
        if (
            live and not changed and media == base_media and bundles == base_bundles
            and meta_name == _identity_field(live, "meta_name") and not rotated_live
        ):
            print(f"Already serving on {label}; nothing to append.")
            # The serve process records a hot-reload that failed AFTER the CLI reported success (the
            # SIGHUP is fire-and-forget) — surface it here, or the no-op compounds the false success.
            stale = _identity_field(live, "last_reload_error")
            if stale:
                print(
                    f"Warning: the engine's last hot-reload failed and it kept its previous engines: "
                    f"{stale}\n(see log={paths.engines_dir(network_id) / f'{engine_id}.log'})",
                    file=sys.stderr,
                )
            return 0
        _reject_unserveable_union(merged_specs, args, live)
        _warn_shadowed_models(merged_specs)  # the serve loop logs this too; show it on the operator's terminal

        record = _build_record(
            args, network_id, engine_id, signaling_url, merged_specs,
            media=media, meta_name=meta_name, bundles=bundles,
        )
        # Preserve the live identity's --max-concurrency across an additive join, like media/bundles/meta
        # above. It sizes the running N-worker poll pool (remote/serve._serve_loop), so a re-join that
        # doesn't re-pass --max-concurrency must NOT reset it to the default 1 — that would silently
        # collapse an 8-worker engine to one on the next respawn (it's harmless to over-carry: the value is
        # advertised, and the reload pins the advertised capacity to the actual live pool anyway).
        if getattr(args, "max_concurrency", None) is None and live:
            record["max_concurrency"] = _identity_field(live, "max_concurrency")
        # Zero-drop when we can: SIGHUP the live singleton to hot-reload the union in place — an appended
        # API engine reloads too now that its bearer is re-read from the key store (issue 05). Fall back to
        # stop-respawn for a first join, a legacy/pre-handler process, a launch, a media change, a
        # concurrency-default flip, or a rotated key (respawned by policy so the operator knows it's live).
        if rotated_live:
            print(f"Rotated the stored {args.api} key — restarting the engine to apply it.")
        reloaded = (not rotated_live) and _hot_reloadable(live, merged_specs, record)
        if reloaded:
            reloaded = _hot_reload_identity(network_id, record, live)  # False if it fell back to a respawn
        else:
            _respawn_identity(network_id, record, live)  # stops prior process(es) then respawns; aborts on failure

    appended = bool(live)
    verb = "Appended to" if appended else "Joining"
    print(f"{verb} {label} (pid={record['pid']}) — {'re-serving' if appended else 'serving'} the union via the relay.")
    if len(record["engines"]) > 1:
        print(f"engines={len(record['engines'])} (serving the union under one identity)")
    elif record["endpoint_url"]:
        print(f"endpoint_url={record['endpoint_url']}")
    if record["models"]:
        print(f"models={','.join(record['models'])}")
    if media:  # the comfyui:* models are resolved from bundle gating at serve time, not here
        print("media=on (serving comfyui:* workflows via the relay)")
    print(f"log={paths.engines_dir(network_id) / f'{engine_id}.log'}")
    if reloaded:  # the live process re-advertised in place — nothing restarted, nothing dropped
        print(f"(hot-reloaded — no in-flight requests dropped; stop with `grid leave {label}`)")
    else:
        # The relay isn't locally pollable, so we can't confirm "registered" here — report starting.
        print(f"(starting — stop with `grid leave {label}`)")
    return 0


def _resolve_api_targets(args: argparse.Namespace) -> tuple[list[dict[str, object]], bool]:
    """The single API-engine spec for ``join --api <kind>``, plus whether the stored key rotated:
    ``-m`` validated against the static whitelist first (no key, no network), then the key resolved
    (env var, else key store, else hidden prompt), then the vendor's model listing — the ONLY place
    the CLI itself calls the vendor (ADR 0012) — which doubles as key validation and as the
    whitelist ∩ visible-models filter. The spec is kind-generic and never carries the key; the
    vendor model names are derived from the advertised names at serve time (a stored map would go
    stale on an additive re-join, which unions models only)."""
    kind = args.api
    whitelist = api_catalog.WHITELISTS[kind]  # kind already validated by _reject_api_conflicts
    if getattr(args, "advertise_as", None):
        # Defence in depth: inline `-m real=alias` desugars into advertise_as after the early guard.
        raise SystemExit("--advertise-as aliasing doesn't apply with --api.")
    valid = {api_catalog.advertised_name(kind, entry): entry for entry in whitelist.entries}
    # No -m = the whole whitelist (zero-config default); `valid` preserves whitelist order.
    chosen = list(dict.fromkeys(args.models or [])) or list(valid)  # dedupe so errors don't repeat
    unknown = [model for model in chosen if model not in valid]
    if unknown:
        raise SystemExit(
            f"Not in the {kind} whitelist: {', '.join(unknown)}. "
            f"Valid models: {', '.join(valid)}."
        )
    from remote import api_keys  # lazy: only stdlib + shared.* at module top (see module docstring)

    from . import provider

    # Key precedence (ADR 0012): env var, else the machine-local key store, else a hidden
    # interactive prompt. Never a flag — a key on the command line leaks into shell history.
    # The env value is stripped like the prompt's, so accidental whitespace can't make an
    # identical key look rotated on the `key != stored` check below.
    stored = api_keys.load_key(kind)
    key = (os.environ.get(whitelist.env_var) or "").strip() or stored
    if not key and provider._interactive():
        key = _prompt_api_key(kind, whitelist.env_var)
        if not key:
            raise SystemExit(f"No {kind} API key entered.")
    if not key:
        raise SystemExit(
            f"--api {kind} needs your API key in {whitelist.env_var} "
            f"(export {whitelist.env_var}=... and re-run), or run interactively to be prompted."
        )
    visible = _list_vendor_models(kind, whitelist.base_url, key)
    # The listing call above proved the key valid — only now persist it to the machine-local key
    # store, so a mistyped/revoked key is never stored for later joins (and the detached serve
    # process) to reuse silently. A reused stored key skips the no-op rewrite; any NEW key (env or
    # prompted) counts as a rotation the caller must deliver to a live identity via respawn.
    key_rotated = key != stored
    if key_rotated:
        api_keys.store_key(kind, key)
    served = [model for model in chosen if valid[model].vendor_name in visible]
    skipped = [model for model in chosen if model not in served]
    if not served:
        # Wording must fit both the -m subset and the no--m default (nothing was "requested" then),
        # and must keep the model names — they are the actionable part of the diagnostic.
        raise SystemExit(
            f"None of these {kind} whitelist models are available to this key: {', '.join(skipped)}."
        )
    if skipped:
        print(f"Skipping (not available to this {kind} key): {', '.join(skipped)}", file=sys.stderr)
    return [{
        "endpoint_url": whitelist.base_url,
        "models": served,
        "engine_label": kind,
        "api_kind": kind,
    }], key_rotated


def _prompt_api_key(kind: str, env_var: str) -> str:
    """Hidden interactive prompt for one kind's API key — input is never echoed (getpass). Split
    out so the CLI-seam tests can monkeypatch it (getpass reads the controlling tty)."""
    return getpass.getpass(f"Enter your {kind} API key (input hidden; or export {env_var}): ").strip()


def _list_vendor_models(kind: str, base_url: str, key: str) -> set[str]:
    """The vendor model ids this key can see (``GET {base_url}/models``). A rejected key or an
    unreachable/malformed vendor is a terminal error — nothing is spawned. Never echoes the key."""
    import httpx  # lazy: only stdlib + shared.* at module top (see module docstring)

    try:
        with httpx.Client(timeout=_VENDOR_LIST_TIMEOUT) as client:
            resp = client.get(f"{base_url}/models", headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as exc:
        raise SystemExit(f"Could not reach {kind} at {base_url}: {exc}") from None
    if resp.status_code in (401, 403):
        raise SystemExit(
            f"{kind} rejected the API key (HTTP {resp.status_code}): {resp.text[:200]}"
        )
    if resp.status_code != 200:  # an outage/redirect is not a key problem — don't blame the key
        raise SystemExit(f"{kind} returned HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        data = resp.json()
    except ValueError:
        raise SystemExit(f"{kind} returned a malformed model listing (not JSON).") from None
    # A 200 that isn't the documented {"data": [...]} shape must be its own diagnostic error —
    # returning an empty set here would masquerade as "your key can't see these models".
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise SystemExit(f"{kind} returned an unexpected model listing shape: {resp.text[:200]}")
    return {
        str(item["id"])
        for item in items
        if isinstance(item, dict) and item.get("id")
    }


def _live_records(network_id: str) -> list[dict[str, object]]:
    """Every remote run record for this grid whose detached process is still alive. Normally that's just
    the singleton ``remote.json``; on upgrade it also catches legacy ``engine-<uuid>`` records so the join
    can adopt their engines and stop their processes (they share the token node_id)."""
    return [
        rec for rec in run_records.read_records(network_id).values()
        if run_records.pid_alive(int(rec.get("pid") or 0))
    ]


def _flat_spec(record: dict[str, object]) -> dict[str, object]:
    """One engine spec synthesised from a record written before the multi-engine ``engines`` field
    (mirrors ``remote/serve._flat_spec``) so an old-format live record is still adopted, not dropped.
    Never carries ``api_kind``: api specs postdate the ``engines`` array, so a flat record can't
    hold one — if that invariant ever breaks, the spec would silently degrade to a hardware engine."""
    return {
        "endpoint_url": record.get("endpoint_url"),
        "models": list(record.get("models") or []),
        "engine_label": record.get("engine_label"),
    }


def _spec_key(spec: dict[str, object]) -> object:
    """Identity of an engine for dedup/merge: its endpoint URL, or — for the built-in ``--serve`` engine,
    which has no URL — a marker plus its model set, so re-joining the same built-in is recognised."""
    url = spec.get("endpoint_url")
    return url if url else ("__builtin__", tuple(spec.get("models") or []))


def _merge_engines(
    base: list[dict[str, object]], incoming: list[dict[str, object]]
) -> tuple[list[dict[str, object]], bool]:
    """Merge ``incoming`` specs into a fresh copy of ``base``. The same engine (by ``_spec_key``) unions its
    models; a new engine is appended. Returns ``(merged, changed)`` where ``changed`` is True only when a
    model or engine was actually added — so an idempotent re-join (including adding a model to an engine
    already in the union) stays a no-op instead of silently dropping the request."""
    merged = [dict(spec) for spec in base]
    index = {_spec_key(spec): spec for spec in merged}
    changed = False
    for spec in incoming:
        existing = index.get(_spec_key(spec))
        if existing is None:
            copy = dict(spec)
            copy["models"] = list(copy.get("models") or [])
            merged.append(copy)
            index[_spec_key(copy)] = copy
            changed = True
            continue
        added = [m for m in (spec.get("models") or []) if m not in (existing.get("models") or [])]
        if added:
            existing["models"] = list(existing.get("models") or []) + added
            changed = True
    return merged, changed


def _engine_union(records: list[dict[str, object]]) -> list[dict[str, object]]:
    """The merged union of every engine across ``records`` (same engine → models unioned). A record with no
    ``engines`` array falls back to a flat spec so a pre-multi-engine live record isn't silently lost."""
    union: list[dict[str, object]] = []
    for record in records:
        specs = record.get("engines")
        if not specs and (record.get("endpoint_url") or record.get("models")):
            specs = [_flat_spec(record)]
        union, _ = _merge_engines(union, specs or [])
    return union


def _identity_field(live: list[dict[str, object]], key: str) -> object:
    """One field of the live identity — the singleton's if present, else the first live record's."""
    for record in live:
        if record.get("engine_id") == _REMOTE_IDENTITY:
            return record.get(key)
    return live[0].get(key) if live else None


def _reject_unserveable_union(
    merged_specs: list[dict[str, object]], args: argparse.Namespace, live: list[dict[str, object]]
) -> None:
    """Guard the merged union: the built-in engine can't join a multi-engine identity (external-only,
    ADR 0007 D4), and ``--advertise-as`` aliases only a single engine (so appending onto an already-aliased
    identity is rejected rather than silently dropping the alias)."""
    if len(merged_specs) > 1 and any(not spec.get("endpoint_url") for spec in merged_specs):
        raise SystemExit(
            "The built-in engine (`--serve`) serves a single model and can't join a multi-engine "
            "identity. Run `grid leave`, then re-join every engine as external `--at <url> -m <model>`."
        )
    # --advertise-as aliases don't merge across joins (the record's `advertise_as` is a flat, positionally
    # keyed list), so appending onto — or with — an alias would drop an alias or mismatch the alias/model
    # counts (which crashes the reload's _advertised_models). Reject any changing append touching aliases;
    # the no-op case already returned earlier, so `live` here means a real change (ADR 0010).
    aliased = bool(getattr(args, "advertise_as", []) or []) or any(rec.get("advertise_as") for rec in live)
    if aliased and (len(merged_specs) > 1 or live):
        raise SystemExit(
            "--advertise-as aliases are single-engine and don't merge across joins. Run `grid leave`, "
            "then re-join every engine in one command with its -m/--advertise-as pairs."
        )


def _media_key(record: dict[str, object]) -> tuple[bool, tuple[str, ...], int, int]:
    """See ``shared.run_records.media_signature`` — one shared definition so this hot-reload-vs-respawn
    decision and the serve loop's reload guard can't desync (ADR 0010 C3)."""
    return run_records.media_signature(record)


def _hot_reloadable(
    live: list[dict[str, object]], merged_specs: list[dict[str, object]], record: dict[str, object]
) -> bool:
    """Whether this update can be SIGHUP-hot-reloaded into the live singleton (zero-drop) instead of a
    stop-respawn. True only when the SOLE live process is the singleton, it was started by a build that
    installs the SIGHUP reload handler (``reload_signal``), the merged union is external-only, the
    media config is unchanged, and the effective poll-worker count doesn't flip. Everything else — a
    first join, a legacy/pre-handler sibling, a built-in ``--serve`` launch, any media/bundle change,
    or a concurrency-default flip — still respawns (ADR 0010 D3 / C1 / C3).
    """
    if len(live) != 1:
        return False
    singleton = live[0]
    if singleton.get("engine_id") != _REMOTE_IDENTITY:
        return False
    if singleton.get("reload_signal") != "sighup":  # a pre-Slice-2 process has no SIGHUP handler (C1)
        return False
    if any(not spec.get("endpoint_url") for spec in merged_specs):  # a built-in --serve needs a launch
        return False
    # The poll-worker pool is sized once at spawn and a reload can't resize it (remote/serve
    # `_assemble_snapshot` pins the advertised capacity to the live pool). When this update flips
    # the EFFECTIVE concurrency — the api-only default 8 vs the hardware default 1, with no
    # explicit --max-concurrency pinning both sides — only a respawn applies the new size.
    if run_records.effective_max_concurrency(record) != run_records.effective_max_concurrency(singleton):
        return False
    # A NEW API engine is hot-reloadable now that the reload re-reads the key store and swaps the vendor
    # bearer atomically with routing (issue 05 — remote/serve._assemble_snapshot → _api_bearers), so it
    # is NOT gated here. A ROTATED key for an already-live api spec is still a respawn, forced by the
    # caller (`rotated_live` in cmd_remote_join) for operator certainty — a policy choice, not a limit.
    return _media_key(record) == _media_key(singleton)  # a media/bundle change needs a bring-up (C3)


def _signal_reload(pid: int) -> None:
    """SIGHUP the live singleton so it hot-reloads the merged record in place — no restart, no dropped
    in-flight requests (ADR 0010 D3)."""
    os.kill(pid, signal.SIGHUP)


def _hot_reload_identity(
    network_id: str, record: dict[str, object], live: list[dict[str, object]]
) -> bool:
    """Write the merged record then SIGHUP the live singleton so it re-advertises the union in place. The
    process keeps its pid; write BEFORE signalling so the reload reads the new record (ADR 0010 D3).

    Returns ``True`` if it hot-reloaded in place, ``False`` if it had to fall back to a respawn — so the
    caller reports honestly instead of claiming zero-drop. The fallback fires when the singleton vanished
    between the liveness check and the signal (``os.kill`` raises ``ProcessLookupError``); the residual
    PID-reuse TOCTOU (same window, pid recycled to an unrelated process) is shared with
    ``run_records.terminate_pid`` and not fully fixable without pidfd.
    """
    pid = int(live[0].get("pid") or 0)
    if pid <= 0:  # defensive: a live singleton always has a real pid, and os.kill(0) hits our own group
        _respawn_identity(network_id, record, live)
        return False
    record["pid"] = pid
    run_records.write_record(network_id, _REMOTE_IDENTITY, record)
    try:
        _signal_reload(pid)
        return True
    except ProcessLookupError:  # the process died between the liveness check and the signal — respawn
        _respawn_identity(network_id, record, [])
        return False


def _respawn_identity(
    network_id: str, record: dict[str, object], priors: list[dict[str, object]]
) -> None:
    """Stop the prior process(es), then write ``record`` and (re)spawn the one detached engine, setting
    ``record["pid"]``. Shared by join-append and leave-shrink (respawn is Slice 1's update mechanism).

    Aborts (SystemExit) BEFORE spawning if any prior can't be confirmed stopped — a second live child on
    the same token-pinned node_id would clobber it (the original bug). Raises if the fresh process dies
    during start-up: the grid is left not serving either way, so the operator must know.
    """
    engine_id = _REMOTE_IDENTITY
    undead: list[str] = []
    for prior in priors:
        if not run_records.terminate_pid(int(prior.get("pid") or 0)):
            undead.append(str(prior.get("pid")))
            continue
        prior_id = str(prior.get("engine_id") or "")
        if prior_id and prior_id != engine_id:  # drop a legacy record's file so only the singleton remains
            run_records.record_path(network_id, prior_id).unlink(missing_ok=True)
    if undead:
        raise SystemExit(
            f"Could not stop the engine(s) already serving this grid (pid(s) {', '.join(undead)}); they may "
            "still be registered on the relay. Investigate before re-joining — starting another would clobber them."
        )

    record["pid"] = 0
    run_records.write_record(network_id, engine_id, record)
    proc = _spawn_remote_engine(network_id, engine_id)
    record["pid"] = proc.pid
    run_records.write_record(network_id, engine_id, record)

    log_path = paths.engines_dir(network_id) / f"{engine_id}.log"
    if _await_remote_engine_start(proc) == "died":
        run_records.record_path(network_id, engine_id).unlink(missing_ok=True)
        from . import provider

        raise SystemExit(
            f"Engine exited before it started — the grid is not serving now. See {log_path}:\n"
            f"{provider._log_tail(log_path)}"
        )


def _resolve_serve_targets(args: argparse.Namespace) -> tuple[list[dict[str, object]], bool]:
    """What to serve: `(text_engine_specs, media_detected)`.

    Text specs are `{endpoint_url, models, engine_label}`. `media_detected` is True when auto-detect
    finds a media (ComfyUI) engine, so the caller brings the media engine up alongside the text ones.
    External `--at` and built-in `--serve` each resolve to one text spec (media comes only from an
    explicit `--media`). An explicit `--media` with no text engine is media-only: return `([], False)`
    and let `args.media` carry it. A bare `grid join` auto-detects: text engines join under one
    identity (DECISIONS D9) — `--all` (or an interactive confirm) accepts several, otherwise it asks —
    and any detected media engine flips `media_detected`. Returns `([], False)` when the operator
    declines the "join all" prompt. Mirrors local `cli/provider.cmd_join` (remote → ONE identity).
    """
    from . import provider

    if args.at:
        if not args.models:
            raise SystemExit("--at requires at least one -m/--model naming what that engine serves.")
        return [{"endpoint_url": args.at, "models": list(args.models), "engine_label": None}], False
    if args.serve:
        return [{"endpoint_url": None, "models": [args.serve], "engine_label": None}], False
    if args.models:
        raise SystemExit("-m/--model names models for an engine; pair it with --at <url>, or use --serve <model>.")
    if getattr(args, "media", False):
        # Explicit `--media` with no text engine → media-only. Skip detection; the serve loop brings up
        # the media engine from the bundle gating.
        return [], False

    detected = provider._detect(None)  # advertise_host is local-only; remote always probes loopback
    if not detected:
        raise SystemExit(
            "No running engine detected on this box. Point at one with "
            "`grid join --at <url> -m <model>`, or start the built-in engine with `grid join --serve <model>`."
        )
    if args.kind:
        detected = [engine for engine in detected if engine.label == args.kind]
        if not detected:
            raise SystemExit(f"No detected engine of kind {args.kind!r}. Run `grid join` to list them.")

    media_detected = any(engine.media for engine in detected)
    text = [engine for engine in detected if not engine.media]
    # Gate on ALL detected engines (incl. a media/ComfyUI one) and show them in the plan, so a
    # detected media engine is never silently joined without confirmation, nor silently dropped on
    # decline (mirrors local `cli/provider.cmd_join`, which counts + prints the full detected list).
    if len(detected) > 1 and not args.all:
        provider._print_plan(detected)
        if provider._interactive():
            if not provider._confirm("Join all detected engines?"):
                return [], False
        else:
            raise SystemExit("Multiple engines detected; pass --all, --kind <kind>, or --at <url>.")
    return [
        {"endpoint_url": engine.endpoint_url, "models": list(engine.models), "engine_label": engine.label}
        for engine in text
    ], media_detected


def _warn_shadowed_models(specs: list[dict[str, object]]) -> None:
    """Warn when two engines advertise the same model — the first detected wins (ADR 0007 / D9)."""
    owner: dict[str, str] = {}
    for spec in specs:
        label = str(spec.get("engine_label") or spec.get("endpoint_url") or "an engine")
        for model in spec["models"]:
            if model in owner:
                print(
                    f"Note: model {model!r} is served by more than one engine; routing it to "
                    f"{owner[model]!r} (first detected wins).",
                    file=sys.stderr,
                )
            else:
                owner[model] = label


def _build_record(
    args: argparse.Namespace,
    network_id: str,
    engine_id: str,
    signaling_url: str,
    specs: list[dict[str, object]],
    media: bool = False,
    meta_name: str | None = None,
    bundles: list[str] | None = None,
) -> dict[str, object]:
    """The remote engine's run record — non-secret routing only; the token stays in credentials.toml.

    Several engines can serve under one identity (DECISIONS D9): `engines` carries each local engine
    so the serve loop can build the model→engine table. Top-level `models` is their union and
    `endpoint_url` is the single engine's URL (None when several) — kept for display + back-compat.
    `media` (+ bundles/ports) mirror the local record fields so the serve loop brings up ComfyUI + the
    media server; a media-only join has empty `specs`/`models` and derives `comfyui:*` at serve time.
    """
    from local import runtime

    union = list(dict.fromkeys(model for spec in specs for model in spec["models"]))
    single_endpoint = specs[0]["endpoint_url"] if len(specs) == 1 else None

    return {
        "engine_id": engine_id,
        # Written by a build whose serve loop installs the SIGHUP reload handler, so a later `grid
        # join`/`leave` can hot-reload this identity in place instead of stop-respawning it (ADR 0010 C1).
        "reload_signal": "sighup",
        "node_id": f"node-{uuid.uuid4().hex[:12]}",
        "grid_id": network_id,  # the remote network_id doubles as the run record's grid_id
        "meta_name": meta_name,  # grid-page display name (--name, or hostname); NOT the record key
        "pid": 0,
        "signaling_url": signaling_url,
        "endpoint_url": single_endpoint,
        "models": union,
        "engines": specs,
        "media": bool(media),
        "media_bundles": list(bundles if bundles is not None else (getattr(args, "bundles", []) or [])),
        "comfyui_port": getattr(args, "comfyui_port", 8188),
        "media_port": getattr(args, "media_port", 8190),
        "advertise_as": list(getattr(args, "advertise_as", []) or []),
        "engine_label": getattr(args, "engine_label", None),
        "pricing_input": getattr(args, "pricing_input", None),
        "pricing_output": getattr(args, "pricing_output", None),
        "max_concurrency": getattr(args, "max_concurrency", None),
        "endpoint_port": getattr(args, "endpoint_port", 8081),
        "ctx_size": getattr(args, "ctx_size", None),
        "n_predict": getattr(args, "n_predict", None),
        "parallel": getattr(args, "parallel", None),
        "flash_attn": getattr(args, "flash_attn", None),
        "temp": getattr(args, "temp", None),
        "reasoning_budget": getattr(args, "reasoning_budget", None),
        "started_at": runtime.utc_now(),
    }


def _spawn_remote_engine(network_id: str, engine_id: str) -> subprocess.Popen:
    from local import runtime

    log_path = paths.engines_dir(network_id) / f"{engine_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = logging_setup.cap_and_open_append(log_path, logging_setup.engine_log_max_bytes())
    return subprocess.Popen(
        runtime.cli_command() + ["__remote-engine", network_id, engine_id],
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )


def _await_remote_engine_start(proc: subprocess.Popen, grace: float = 3.0) -> str:
    """Block briefly to tell a freshly-spawned remote engine "died" from "starting".

    Unlike local there is no local registry to poll (the relay isn't locally reachable), so this
    only checks the process stayed alive — registration shows up on the grid page, not here.
    """
    deadline = time.time() + grace
    while time.time() < deadline:
        if proc.poll() is not None:
            return "died"
        time.sleep(0.2)
    return "starting" if proc.poll() is None else "died"


def cmd_remote_leave(args: argparse.Namespace) -> int:
    from remote import credentials

    from . import remote_grid

    credentials.require_session()
    rec = remote_grid._select(getattr(args, "grid", None))
    network_id = remote_grid._network_id(rec)
    label = rec.get("name") or network_id

    with file_lock(run_records.record_path(network_id, _REMOTE_IDENTITY)):
        records = run_records.read_records(network_id)
        if not records:
            print(f"No engines joined to {label}.")
            return 0
        # `--engine <endpoint_url|label>` drops one engine from the union; anything else (bare / `--all`)
        # tears down the whole identity — remote has one identity per grid, so both mean the same here.
        if args.engine and not args.all:
            return _leave_one_engine(args, network_id, label, records)
        from . import provider  # shared teardown: stops the engine + reaps a media engine's ComfyUI

        for engine_id, record in records.items():
            provider._stop_engine(network_id, engine_id, record)
        print(f"Left {label}.")
        return 0


def _leave_one_engine(
    args: argparse.Namespace, network_id: str, label: str, records: dict[str, dict[str, object]]
) -> int:
    """Drop the engine matching ``--engine`` (endpoint URL, or a unique label) from the identity's union.

    Removing the last text engine (with no media) tears the whole identity down; otherwise the singleton
    is respawned serving the reduced union. Operates on the live record(s), adopting any legacy sibling.
    """
    survivors = [rec for rec in records.values() if run_records.pid_alive(int(rec.get("pid") or 0))]
    survivors = survivors or list(records.values())
    union = _engine_union(survivors)
    to_drop = _drop_spec(union, args.engine, label)
    if not to_drop:
        raise SystemExit(
            f"No engine {args.engine!r} on {label} (match by endpoint URL, a served model, or a URL "
            f"fragment). Engines: {_engines_summary(union)}."
        )
    drop_ids = {id(spec) for spec in to_drop}  # filter by identity — value-equal specs must not both drop
    remaining = [spec for spec in union if id(spec) not in drop_ids]
    media = any(bool(rec.get("media")) for rec in survivors)

    if not remaining and not media:  # nothing left to serve → tear the whole identity down
        from . import provider  # shared teardown: stops the engine + reaps a media engine's ComfyUI

        for engine_id, record in records.items():
            provider._stop_engine(network_id, engine_id, record)
        print(f"Left {label} (removed the last engine).")
        return 0

    # Rebuild the singleton from the identity's own record, minus the dropped engine, and respawn it.
    # (When one engine remains, `remote/serve._ServeState.route()` falls back to it for an unknown model —
    # a job for the just-dropped model now forwards to the survivor instead of erroring; existing semantics.)
    record = dict(next(iter(survivors)))
    record["engine_id"] = _REMOTE_IDENTITY
    record["reload_signal"] = "sighup"  # stamp it so a pre-handler identity self-heals on leave (like join)
    record["engines"] = remaining
    record["models"] = list(dict.fromkeys(m for spec in remaining for m in spec.get("models") or []))
    record["endpoint_url"] = remaining[0]["endpoint_url"] if len(remaining) == 1 else None
    record["media"] = media  # recompute from the survivors, don't inherit the arbitrary template's flag
    record["media_bundles"] = list(dict.fromkeys(b for rec in survivors for b in (rec.get("media_bundles") or [])))
    record.pop("last_reload_error", None)  # a fresh lifecycle attempt shouldn't inherit a stale failure
    if _hot_reloadable(survivors, remaining, record):
        reloaded = _hot_reload_identity(network_id, record, survivors)  # SIGHUP the survivor — zero-drop shrink
    else:
        _respawn_identity(network_id, record, survivors)  # aborts on a stuck prior / raises on a dead respawn
        reloaded = False
    # Report honestly, like cmd_remote_join: _hot_reload_identity returns False when it fell back to a
    # respawn (the pid vanished in the TOCTOU window), and a respawn is not a zero-drop shrink.
    how = "hot-reloaded — no in-flight requests dropped" if reloaded else "restarted the engine to apply it"
    print(f"Dropped {args.engine!r} from {label}; re-serving {len(remaining)} engine(s) ({how}).")
    return 0


def _engines_summary(union: list[dict[str, object]]) -> str:
    """A short human list of an identity's engines for a leave error / ambiguity message."""
    parts = []
    for spec in union:
        url = spec.get("endpoint_url") or "(built-in)"
        models = ",".join(spec.get("models") or [])
        parts.append(f"{url} [{models}]" if models else str(url))
    return "; ".join(parts)


def _drop_spec(
    union: list[dict[str, object]], selector: str, label: str
) -> list[dict[str, object]]:
    """The spec(s) to remove for ``selector`` — exact endpoint_url → engine_label → served model → URL
    substring — via the shared matcher (`shared.run_records.match_engine`). Remote engines are keyed by
    URL/label, so no exact-id short-circuit here (that's the local caller's job)."""
    return run_records.match_engine(union, selector, label=label, summary=_engines_summary(union))
