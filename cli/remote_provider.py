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
import contextlib
import getpass
import os
import signal
import socket
import subprocess
import sys
import time
from typing import NamedTuple
import uuid
from typing import TYPE_CHECKING

from shared import logging_setup, orphan_sweep, paths, run_records
from shared.filelock import file_lock
from shared.models import api_catalog

if TYPE_CHECKING:  # runtime imports of remote.* stay lazy (see the module docstring)
    from remote.codex_oauth import CodexBundle
    from remote.codex_probe import CodexModel, SeatRejected

# Remote has exactly ONE identity per grid: the relay node_id is pinned to the per-grid access token
# (remote/credentials.node_id_from_token), so two `grid join`s on a grid would register the same node and
# clobber each other. The run record is therefore a singleton keyed by this constant — one file
# engines_dir(<network_id>)/remote.json — and repeated joins are additive (ADR 0010). `--name` no longer
# keys the record (it can't mint a second identity); it is the grid-page display name (record["meta_name"]).
_REMOTE_IDENTITY = run_records.REMOTE_IDENTITY

# One-shot vendor model-listing call at `join --api` (key validation + whitelist intersection).
_VENDOR_LIST_TIMEOUT = 15.0

# Appended to a full-leave success line when the argv sweep could not read the process table: the
# backstop still dropped the model, but we couldn't verify no stray child remains, so the success is
# qualified rather than an unconditional "Left". One definition, shared by both full-leave report
# paths (bare/`--all` and the `--engine <last>` teardown). Worded without naming `ps` because the
# sweep now reads a process table on Windows too, where the reason is a PowerShell/WMI failure.
_SWEEP_UNSCANNED_NOTE = (
    " Couldn't scan for stray serve processes (the process table was unreadable); any that remain "
    "drop after the node TTL (~120s)."
)

# Appended when the sweep found a live process carrying THIS grid's marker and could not stop it —
# another user's, so not ours to kill (grid-leave issue 15/C). Not a failure: killing another
# operator's legitimate node would be wrong, so leave still exits 0. But not a clean success either,
# and the case that makes it matter is mundane rather than adversarial — a `sudo grid join` followed
# by an unprivileged `grid leave` over a shared GRID_HOME is the SAME node_id.
#
# What survives is the PROCESS, not the advertisement. The backstop flips the node to consumer with
# no models, and nothing the child does afterwards undoes that: grid-src's `registry.heartbeat`
# writes only `last_heartbeat`/`load` and never touches `role`, and its pruned-row self-heal
# (`ensure_node_exists`) re-creates a row as `consumer` too — so the heartbeat that would have made
# the child re-register (`relay.heartbeat` -> "missing" on a 404) is now essentially unreachable. The
# child re-registers on exactly two triggers, a heartbeat answered "missing" and a hot-reload, and a
# foreign child gets neither. That is the "idempotent and resurrection-proof" flip ADR 0023 relies on,
# working as designed — and it is why this note promises no TTL: there is nothing to wait out.
#
# The residual is still real, which is why the note exists at all. The root-owned child keeps the
# engine, the port and the credentials it loaded at startup, so the box is doing work the operator
# believes it stopped — and if the backstop *degraded* (401, relay down) rather than landing, the
# node was never flipped, and this child's heartbeats hold it inside the 120s TTL as a provider
# indefinitely. Only stopping the process ends either one.
#
# Deliberately NOT sharing `_SWEEP_UNSCANNED_NOTE`'s wording, and not merely for readability:
# `tests/test_remote_leave_real_child.py` asserts that string's *absence* as proof the sweep really
# read the table, so reusing it here would turn that guard into a tripwire.
_SWEEP_FOREIGN_NOTE = (
    " Couldn't stop a serve process for this grid owned by another user (named above); it keeps this "
    "box serving until an elevated `grid leave` stops it, or its owner does."
)

# Appended when the sweep ran but was only shown part of the process table — Windows only, where WMI
# returns a null command line for every process the caller cannot open (grid-leave issue 15/B). The
# sweep succeeded; it simply could not have found a serve child owned by anyone else. Distinct from
# `_SWEEP_UNSCANNED_NOTE`, which means no rows were read at all: different evidence, different remedy.
_SWEEP_PARTIAL_NOTE = (
    " Couldn't see all of the process table (most command lines were hidden from this account), so a "
    "stray serve process may remain; an elevated `grid leave` can see them, and any that remain drop "
    "after the node TTL (~120s)."
)


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


class _TaskServing(NamedTuple):
    """What this join decided about task serving, and what the operator must be told (issue 58)."""

    #: The operator opted in. False means they did not, and nothing here applies.
    requested: bool
    #: Why this provider cannot run a task, in the words of whichever check found out.
    problem: str | None

    @property
    def allowed(self) -> bool:
        return self.requested and self.problem is None


def _task_env_from_flags(args: argparse.Namespace) -> dict[str, str]:
    """What `--tasks`/`--max-tasks`/`--tasks-root` change in the serve child's environment.

    The flags SET the environment the child is handed rather than moving the reading into the run
    record, and that is deliberate — `task_opt_in.serving_enabled`'s docstring records why the
    opt-in is read at serve time. This adds a second way to set it, not a second place to read it.

    **The flag wins over an exported variable**, which is the ordinary expectation and is said in
    `--help` so nobody has to discover it. `--tasks` is the exception that proves nothing: there is
    no `--no-tasks`, so it can only ever turn serving ON — and turning it on is the one thing that
    must be a person typing it, never something inferred.
    """
    from remote import task_agent, task_opt_in

    overrides: dict[str, str] = {}
    if getattr(args, "tasks", None):
        overrides[task_opt_in.SERVING_ENV] = "1"
    count = getattr(args, "max_tasks", None)
    if count is not None:
        overrides[task_opt_in.WORKERS_ENV] = str(count)
    root = getattr(args, "tasks_root", None)
    if root is not None:
        overrides[task_agent.WORKSPACE_ROOT_ENV] = str(root)
    return overrides


@contextlib.contextmanager
def _as_the_child_will_see_it(overrides: dict[str, str]):
    """Run a check under the environment the serve child is about to be handed.

    The checks read `os.environ` because that is what the CHILD reads, so asking them about a
    `--tasks-root` this process was never started with means putting the value where they look.
    A scoped mutation with a `finally`, rather than threading every variable through every check:
    the alternative is a second way to express the provider's configuration, and a second way to
    express it is a second way for the two to disagree.
    """
    saved = {name: os.environ.get(name) for name in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _decide_task_serving() -> _TaskServing:
    """Ask, in the parent, whether the child about to be spawned could actually run a task.

    Every one of these answers exists already — and until this ran, `run_task` was the only place
    that asked, which is *after a member is waiting*. So `grid join` printed "serving",
    `grid project status` said online, and the provider looked healthy right up until somebody
    else's task died on it. The fail-closed work of issues 22 and 23 made these failures loud; they
    were loud at the wrong moment and to the wrong person.

    ⚠️ **The refusal cannot be printed by the serve child.** Measured: `_spawn_remote_engine`
    detaches it with BOTH stdout and stderr redirected into the engine log, and this process then
    waits 3s only to tell "alive" from "died" — tailing that log only when it died. Task serving
    must not kill the child, so anything it printed would land in a file nobody has a reason to
    open. The parent is the only place the sentence reaches the person who typed the command.

    Costs nothing when the opt-in is off, which is the default: no `claude --version`, no probe of a
    root nobody configured.

    `(Exception, SystemExit)` because these checks use both — `task_sandbox` raises `SystemExit` as
    a clean-error idiom, and a *bug* in any of them must degrade to "task serving off, inference
    fine" rather than taking down a provider that was only asked to serve inference.
    """
    from remote import task_opt_in

    if not task_opt_in.serving_enabled():
        return _TaskServing(requested=False, problem=None)
    from remote import task_agent

    try:
        task_agent.preflight_before_serving()
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — inference must survive any of them
        return _TaskServing(requested=True, problem=str(exc) or exc.__class__.__name__)
    return _TaskServing(requested=True, problem=None)


def _task_serving_override(decision: _TaskServing) -> dict[str, str] | None:
    """What to change in the serve child's environment, or `None` to hand it over untouched.

    Only ever an OFF. Turning the opt-in *on* for somebody is the one thing this feature may not do
    — a task loop spends the operator's own agent subscription — so a join that decided task serving
    is fine passes the operator's own environment through rather than asserting it.
    """
    from remote import task_opt_in

    if decision.requested and not decision.allowed:
        return {task_opt_in.SERVING_ENV: "0"}
    return None


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
    # Not deprecated, just scoped — so this is spelled out rather than routed through
    # `_warn_deprecated`, whose name is its contract. `--no-browser` drives the codex OAuth sign-in
    # and nothing else. Said aloud rather than swallowed: a flag that silently does nothing reads as
    # a flag that worked. A note, not an error — it is inert, not contradictory, and failing an
    # otherwise-valid join over it would be the worse trade.
    if getattr(args, "no_browser", False) and getattr(args, "api", None) != "codex":
        print(
            "Note: --no-browser only applies to `grid join --api codex` (the subscription sign-in); "
            "ignoring it.",
            file=sys.stderr,
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

    respawn = bool(getattr(args, "respawn", False))  # never no-op, never SIGHUP — always stop-and-start
    key_rotated = False  # a `join --api` that stored a NEW key must reach a live identity via respawn
    deferred_target_error: SystemExit | None = None
    if getattr(args, "api", None) is not None:
        specs, key_rotated = _resolve_api_targets(args, network_id)
        media_detected = False
    else:
        specs, media_detected, deferred_target_error = _resolve_or_defer(args, respawn=respawn)
    media = bool(getattr(args, "media", False)) or media_detected
    if not specs and not media and deferred_target_error is None:
        # engines detected and the operator declined, or nothing to serve
        print("Nothing joined.")
        return 0
    engine_id = _REMOTE_IDENTITY
    meta_name = getattr(args, "name", None) or socket.gethostname()

    # ⚠️ **Before the lock, and before anything is stopped.** A provider that was serving inference
    # a second ago must still be serving it after a task check that failed — so this may not run
    # from inside the block that has already terminated the prior child on its way to finding out.
    # It is also the last point where nothing has been written: a pure read, then the mutation.
    # The flags first, so every check below asks about the configuration the child will actually
    # get rather than the one this shell happens to export (issue 61).
    task_flags = _task_env_from_flags(args)
    with _as_the_child_will_see_it(task_flags):
        task_serving = _decide_task_serving()
    if task_serving.problem:
        print(
            f"Task serving is off for this join: {task_serving.problem}\n"
            f"Inference is unaffected. Fix that and re-run `grid join --respawn` to claim tasks.",
            file=sys.stderr,
        )
    # Withheld from the CHILD, never from this process: the opt-in travels in the environment the
    # parent hands over, and the child reads it once at startup. The withholding is applied LAST so
    # it outranks `--tasks` — a provider that cannot run a task may not be told to claim one by a
    # flag, however explicitly it was typed.
    engine_env = {**task_flags, **(_task_serving_override(task_serving) or {})} or None

    # Remote has ONE identity per grid (the token pins the relay node_id), so `grid join` is additive:
    # merge this join's engines into whatever is already serving, then respawn the single detached engine.
    # The read-merge-write is serialized so two concurrent joins can't lost-update the union (ADR 0010).
    with file_lock(run_records.record_path(network_id, engine_id)):
        live = _live_records(network_id)  # normally just the singleton; also legacy `engine-<uuid>` on upgrade
        # What this join INHERITS, which is not the same question as what is running. A re-join is
        # additive, so it carries the identity's last known configuration forward even when nothing is
        # live — otherwise `grid join --at <second-engine>` onto a crashed (or zombie'd) identity would
        # silently re-serve only the second engine and drop the one already joined. `live` alone still
        # decides the no-op gate and hot-reload-vs-respawn below; `base` decides only what is carried.
        # Union, media, bundles and concurrency inherit TOGETHER, and the alias guard reads `base` too:
        # aliases are positionally keyed to a record's models and are NOT carried by the spec merge, so
        # inheriting an aliased union would drop its aliases. It is refused with the existing
        # leave-then-rejoin instruction instead. (`meta_name` is not inherited by either path — it comes
        # from `--name` or the hostname.) Same shape as `_leave_one_engine`'s
        # `survivors or list(records.values())` (ADR 0010).
        if deferred_target_error is not None and not live:
            # Nothing running to restart, so there is no union to inherit and `--respawn` has nothing
            # to act on: the operator gets the guidance auto-detect would have given them anyway.
            raise deferred_target_error
        base = live or list(run_records.read_records(network_id).values())
        merged_specs, changed = _merge_engines(_engine_union(base), specs)
        # A rotated key only matters when this kind's API spec is already LIVE. A reload WOULD re-read the
        # key store and swap the bearer in place (issue 05), but rotation deliberately RESPAWNS so the
        # operator has certainty the new key is live — never a no-op, never SIGHUP. Kept at the call sites
        # (not inside `_hot_reloadable`) because leave-shrink shares that gate and rotation never applies there.
        rotated_live = key_rotated and any(
            spec.get("api_kind") == args.api for spec in _engine_union(live)
        )
        base_media = any(bool(rec.get("media")) for rec in base)
        media = base_media or media
        base_bundles = list(dict.fromkeys(b for rec in base for b in (rec.get("media_bundles") or [])))
        bundles = list(dict.fromkeys(base_bundles + list(getattr(args, "bundles", []) or [])))
        # An idempotent re-join (no new engine/model, and no display-name/media/bundle change) is a no-op,
        # so it doesn't needlessly restart a live identity. A change in ANY of those does respawn to apply
        # it — there is no other way to rename or add a bundle in Slice 1.
        if (
            live and not changed and media == base_media and bundles == base_bundles
            and meta_name == _identity_field(live, "meta_name") and not rotated_live
            and not respawn
        ):
            # Lazy, per this module's import rule: `cli.dispatch` imports it while `cli` is still
            # initialising, and `remote.relay` pulls httpx in eagerly.
            from remote import service_truth

            log_path = paths.engines_dir(network_id) / f"{engine_id}.log"
            # Running is not serving (grid-leave issue 10). A child that never registered with the
            # relay, or stopped heartbeating, is alive and useless — and the reassuring line below is
            # the exact transcript the mid-respawn incident produced: join says joined, `grid models`
            # empty. Report the state instead. Exit stays 0 (declining to act is still correct) and
            # `--respawn` is the way out; the message is the fix.
            adrift = service_truth.not_serving(
                _identity_record(live) or {}, network_id, engine_id, log_path=log_path
            )
            if adrift is not None:
                print(f"Not serving on {label}: {adrift.state}; nothing was appended.")
                if adrift.detail:
                    print(adrift.detail, file=sys.stderr)
                return 0
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
        _reject_unserveable_union(merged_specs, args, base)
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
        if getattr(args, "max_concurrency", None) is None and base:
            record["max_concurrency"] = _identity_field(base, "max_concurrency")
        # Zero-drop when we can: SIGHUP the live singleton to hot-reload the union in place — an appended
        # API engine reloads too now that its bearer is re-read from the key store (issue 05). Fall back to
        # stop-respawn for a first join, a legacy/pre-handler process, a launch, a media change, a
        # concurrency-default flip, or a rotated key (respawned by policy so the operator knows it's live).
        if rotated_live:
            # "credential", not "key": for openai it IS a key; for codex it is a fresh sign-in's
            # OAuth bundle, which counts as a rotation for exactly the same reason.
            print(f"Rotated the stored {args.api} credential — restarting the engine to apply it.")
        # `--respawn` suppresses the SIGHUP for the same reason a rotated credential does, and more
        # sharply: a hot-reload re-reads the record, which does nothing for a child whose problem is
        # that it never registered — the state the honest gate above points the operator here from.
        reloaded = (not rotated_live) and (not respawn) and _hot_reloadable(live, merged_specs, record)
        if reloaded:
            reloaded = _hot_reload_identity(network_id, record, live)  # False if it fell back to a respawn
        else:
            # stops prior process(es) then respawns; aborts on failure
            _respawn_identity(network_id, record, live, env_overrides=engine_env)

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
    if task_serving.allowed:  # said only when it is true — the refusal has already gone to stderr
        print("tasks=on (claiming tasks for this grid)")
    print(f"log={paths.engines_dir(network_id) / f'{engine_id}.log'}")
    if reloaded:  # the live process re-advertised in place — nothing restarted, nothing dropped
        print(f"(hot-reloaded — no in-flight requests dropped; stop with `grid leave {label}`)")
    else:
        # The relay isn't locally pollable, so we can't confirm "registered" here — report starting.
        print(f"(starting — stop with `grid leave {label}`)")
    return 0


def _resolve_api_targets(
    args: argparse.Namespace, network_id: str
) -> tuple[list[dict[str, object]], bool]:
    """The single API-engine spec for ``join --api <kind>``, plus whether the stored credential rotated.

    ``-m`` is validated against the static whitelist first — no credential, no network — so a typo'd
    model name never costs the operator a key prompt or a whole browser sign-in. How the credential
    itself is then resolved is per-kind and splits below: openai has a metered key (ADR 0012 D-c's
    env → stored → prompt), codex has an OAuth seat (ADR 0015 D-c: sign-in, no env var, no flag).
    ``network_id`` exists for codex's probe-skip precheck (D-f: an unchanged re-join must cost zero
    vendor calls, so the resolver has to see the live record); the key path ignores it.
    """
    kind = args.api
    whitelist = api_catalog.WHITELISTS[kind]  # kind already validated by _reject_api_conflicts
    if getattr(args, "advertise_as", None):
        # Defence in depth: inline `-m real=alias` desugars into advertise_as after the early guard.
        raise SystemExit("--advertise-as aliasing doesn't apply with --api.")
    valid = {api_catalog.advertised_name(kind, entry): entry for entry in api_catalog.entries_for(kind)}
    # No -m = the whole whitelist (zero-config default); `valid` preserves whitelist order.
    requested = list(dict.fromkeys(args.models or []))  # dedupe so errors don't repeat
    chosen = requested or list(valid)
    unknown = [model for model in chosen if model not in valid]
    if unknown:
        raise SystemExit(
            f"Not in the {kind} whitelist: {', '.join(unknown)}. "
            f"Valid models: {', '.join(valid)}."
        )
    from remote import api_keys  # lazy: only stdlib + shared.* at module top (see module docstring)

    if api_catalog.local_seat_port(kind) is not None:
        return _resolve_seat_targets(args, kind, chosen)
    if kind == api_keys.CODEX_KIND:
        # `requested`, not `chosen`: codex's no--m default is the seat's LIVE PROBE set (issue 10a —
        # the resolver probes and computes it itself, no static intersection), so the `chosen =
        # list(valid)` union default is wrong here — it would name static-table models the live seat
        # may not actually serve.
        return _resolve_codex_targets(args, whitelist, requested, network_id)
    return _resolve_key_api_targets(args, kind, whitelist, valid, chosen)


def _resolve_seat_targets(
    args: argparse.Namespace, kind: str, chosen: list[str]
) -> tuple[list[dict[str, object]], bool]:
    """The CLI seat's engine spec. Its "vendor endpoint" is a loopback server on this box, so the
    URL is computed here and the port travels with the spec's own options.

    Binary and sign-in are checked at the prompt for the same reason the local join does it: a
    signed-out seat otherwise registers looking healthy and fails every job into a log file.
    """
    from shared.agent import cli_seat
    from shared.agent.seats import seat_for

    spec = seat_for(kind)
    try:
        cli_seat.assert_available(spec)
    except cli_seat.SeatError as exc:
        raise SystemExit(str(exc))

    options = cli_seat.options_from_args(args, default_port=api_catalog.local_seat_port(kind))
    engine = {
        "endpoint_url": f"http://127.0.0.1:{options.port}",
        "models": list(chosen),
        "engine_label": kind,
        "api_kind": kind,
        "seat": cli_seat.options_to_dict(options),
    }
    return [engine], False  # no stored credential exists, so it can never have rotated


def _resolve_codex_targets(
    args: argparse.Namespace,
    whitelist: api_catalog.ApiWhitelist,
    requested: list[str],
    network_id: str,
) -> tuple[list[dict[str, object]], bool]:
    """The codex seat's engine spec, plus whether the credential changed (a fresh sign-in ran —
    the caller respawns a live codex identity for it, the openai key-rotation policy).

    Deliberately shares nothing with the key path: there is no env var to read, no flag to accept
    and no prompt to hide (ADR 0015 D-c), and the validation call is D-f's free
    ``GET {base}/models`` probe — egress reachability + seat liveness + the seat's real entitled
    set in one round-trip — not the key path's ``GET /v1/models``.
    """
    from remote import api_keys, codex_models_cache

    from . import codex_signin

    bundle, fresh = codex_signin.resolve_seat(no_browser=bool(getattr(args, "no_browser", False)))

    # Probe-once (D-f): an identical re-join must cost ZERO vendor calls. Advisory and lock-free —
    # the authoritative no-op gate still runs under the caller's file_lock. One narrow race is
    # accepted and documented (ADR 0015 issue-05 note): a concurrent `grid leave` landing between
    # this read and the lock lets a just-serving spec re-join unprobed — its contents were live
    # moments ago, and a seat that died in that window surfaces as job errors at serve time.
    live_codex = next(
        (spec for spec in _engine_union(_live_records(network_id))
         if spec.get("api_kind") == api_keys.CODEX_KIND),
        None,
    )
    if live_codex is not None and (
        (live_codex.get("endpoint_url") or "").rstrip("/") != whitelist.base_url
    ):
        # This grid release moved the codex backend. Echoing the old spec would pin the identity
        # to a dead URL forever ("nothing to append"), and proceeding would UNION a second codex
        # engine beside it (`_spec_key` keys engines by URL) — refuse loudly instead
        # (silent-failure review #3a).
        raise SystemExit(
            "This grid release moved the codex backend "
            f"({live_codex.get('endpoint_url')} -> {whitelist.base_url}); a live codex engine "
            "can't be re-pointed in place. Run `grid leave --engine codex`, then re-run "
            "`grid join --api codex`. Nothing was changed."
        )
    if (
        live_codex is not None and not fresh and live_codex.get("model_caps")
        and (not requested or set(requested) <= set(live_codex.get("models") or []))
    ):
        # Same credential, no model beyond the live union, AND the live spec already carries
        # probe-derived caps — nothing a probe could inform. (A -m SUBSET is unchanged too:
        # narrowing is leave-then-rejoin by design, a join only ever adds.) The `model_caps` guard
        # forces a re-probe when the live record predates issue 10a (no caps recorded): skipping it
        # there would leave the serving side advertising every model fail-closed. The spec's models
        # list AND model_caps are copied so the returned spec can never alias the live record's own
        # mutable fields (the immutability rule its sibling `models` already follows).
        return [{
            **live_codex,
            "models": list(live_codex.get("models") or []),
            "model_caps": dict(live_codex.get("model_caps") or {}),
        }], False

    live, bundle, fresh = _probe_codex_seat_with_recovery(args, whitelist, bundle, fresh)
    # Cache the fresh probe (issue 10b), scoped to this seat's account fingerprint: an offline
    # `grid catalog --api codex` then shows the seat's real last-known set instead of only the static
    # reference. Best-effort inside the writer — a cache-write failure never fails the join. Written
    # HERE, after a real probe — the probe-skip early-return above never reaches this line, so an
    # unchanged re-join keeps the last real cache.
    codex_models_cache.write_cache(
        live, client_version=api_catalog.CODEX_CLIENT_VERSION, account_id=bundle.account_id,
    )
    served, model_caps = _select_codex_models(requested, live)
    return [{
        "endpoint_url": whitelist.base_url,
        "models": served,
        "engine_label": api_keys.CODEX_KIND,
        "api_kind": api_keys.CODEX_KIND,
        # The seat's tier — a display/message label only now (issue 10a: serving no longer gates on
        # it). A short plan string (the seat's raw claim), never a secret; `None` rides through.
        "plan_type": bundle.plan_type,
        # The probe-derived per-model caps the serving side reads to build the capability envelope,
        # now that no static table exists to look them up from (issue 10a). Non-secret routing data
        # — model names + context_window/vision/tools/vendor_rank, never a token or the account id.
        "model_caps": model_caps,
    }], fresh


def _probe_codex_seat_with_recovery(
    args: argparse.Namespace,
    whitelist: api_catalog.ApiWhitelist,
    bundle: CodexBundle,
    fresh: bool,
) -> tuple[tuple[CodexModel, ...], CodexBundle, bool]:
    """D-f's probe, with the ONE recovery issue 05 allows: a STORED seat the vendor rejects gets
    a single fresh sign-in and one re-probe, interactive runs only (the PRD's sign-in inline
    "when the stored one is dead" — without it, a dead stored bundle makes every re-join load the
    same corpse and fail forever, since no other re-sign-in verb exists). A fresh seat failing, a
    second failure, or a non-interactive run gets the terminal auth-class message. Returns
    ``(live_records, bundle, fresh)`` — the probe's rich per-model records, plus the bundle and
    freshness the join must proceed with, since a recovery re-mints both.
    """
    from remote import codex_probe

    from . import codex_signin, provider

    try:
        live = codex_probe.probe_seat(
            bundle, base_url=whitelist.base_url, client_version=api_catalog.CODEX_CLIENT_VERSION,
        )
        return live, bundle, fresh
    except codex_probe.SeatRejected as rejected:
        if fresh or not provider._interactive():
            raise SystemExit(_codex_seat_rejected(rejected)) from None
        print(
            f"The vendor rejected the stored codex seat (HTTP {rejected.status_code}) — "
            "starting a fresh sign-in.",
            file=sys.stderr,
        )

    # Outside the except block so a second refusal raises with no chained context to dig through.
    bundle = codex_signin.sign_in(no_browser=bool(getattr(args, "no_browser", False)))
    try:
        live = codex_probe.probe_seat(
            bundle, base_url=whitelist.base_url, client_version=api_catalog.CODEX_CLIENT_VERSION,
        )
    except codex_probe.SeatRejected as rejected:
        raise SystemExit(_codex_seat_rejected(rejected)) from None
    return live, bundle, True


def _codex_seat_rejected(rejected: SeatRejected) -> str:
    """The auth-class terminal message (issue 05's taxonomy): the seat, not the machine."""
    return (
        f"The vendor rejected this codex seat (HTTP {rejected.status_code}). Nothing was joined. "
        "Re-run `grid join --api codex` from an interactive shell to sign in again."
    )


def _select_codex_models(
    requested: list[str], live: tuple[CodexModel, ...]
) -> tuple[list[str], dict[str, dict[str, object]]]:
    """The advertised set + its persisted caps, straight from the seat's live probe (issue 10a — no
    static tier intersection; the probe IS the source of truth for both the set and its caps).

    Membership is the probe set. An explicit ``-m`` for a model the seat can't serve is REFUSED,
    never silently narrowed (the deliberate divergence from openai's skip), naming what the probe
    set CAN serve. Returns ``(served advertised names, model_caps)`` where ``model_caps[advertised]``
    is the caps the serving side reads from the run record — ``context_window``/``vision``/``tools``
    derived from the probe, and ``vendor_rank`` = the model's 1-based position in the probe's visible
    order (vendor/priority order, the curated ordering today's ranking reproduces).
    """
    kind = api_catalog.CODEX_KIND
    # Caps for the WHOLE probe set (not just the -m subset): an additive re-join unions this engine's
    # models, so the persisted caps must cover every entitled model or a later `-m` append would union
    # a model whose caps this write dropped. `vendor_rank` = 1-based probe order (vendor/priority).
    model_caps: dict[str, dict[str, object]] = {
        f"{kind}:{model.slug}": {
            "context_window": model.context_window,
            "vision": model.supports_vision,
            "tools": model.supports_tools,
            "vendor_rank": index + 1,
        }
        for index, model in enumerate(live)
    }
    outside = [model for model in requested if model not in model_caps]
    if outside:
        # A personal seat asked for a model it can't serve deserves a refusal, not a silent subset.
        # Name what the seat CAN serve (the probe set) so the operator isn't left guessing.
        raise SystemExit(
            f"Not available on this codex seat: {', '.join(outside)}. "
            f"This seat can serve: {', '.join(model_caps) or '(none)'}. Nothing was joined."
        )
    served = requested or list(model_caps)
    if not served:
        # The no--m default found nothing: the seat's live listing is empty (every model hidden or
        # API-unsupported). Nothing was "requested", so no name is blamed.
        raise SystemExit(
            "This codex seat currently serves no models — its live listing is empty. "
            "Nothing was joined."
        )
    return served, model_caps


def _resolve_key_api_targets(
    args: argparse.Namespace,
    kind: str,
    whitelist: api_catalog.ApiWhitelist,
    valid: dict[str, api_catalog.ApiModelEntry],
    chosen: list[str],
) -> tuple[list[dict[str, object]], bool]:
    """The spec for a metered-key API engine (``openai``), plus whether the stored key rotated.

    The key is resolved (env var, else key store, else hidden prompt), then the vendor's model
    listing — the ONLY place the CLI itself calls the vendor (ADR 0012) — doubles as key validation
    and as the whitelist ∩ visible-models filter. The spec is kind-generic and never carries the key;
    the vendor model names are derived from the advertised names at serve time (a stored map would go
    stale on an additive re-join, which unions models only).
    """
    from remote import api_keys

    from . import provider

    # A kind reaching the KEY path must name the env var its key is read from — that IS a step of
    # the precedence below. This guard stays FIRST: `os.environ.get(None)` is a TypeError, i.e. a
    # traceback rather than this repo's clean-SystemExit contract, and the messages below would tell
    # the operator to `export None=...`. Unreachable while codex is the only env-var-less kind (it
    # routes to `_resolve_codex_targets` above), so this is the landmine guard for the next one —
    # `api_keys.require_bearer` holds the same line on the serve side.
    env_var = whitelist.env_var
    if not env_var:
        raise SystemExit(
            f"--api {kind} has no API-key sign-in path in this version of grid. "
            f"This is a bug: {kind} needs its own credential resolution."
        )

    stored = api_keys.load_key(kind)

    flag_key = getattr(args, "api_key", None)
    if flag_key:
        print(
            f"Warning: --api-key is visible in shell history. "
            f"Consider exporting {env_var} instead.",
            file=sys.stderr,
        )

    # Key precedence: --api-key flag, else the env var, else the machine-local key store, else a
    # hidden interactive prompt. Values are stripped so accidental whitespace can't make an
    # identical key look rotated on the `key != stored` check below.
    key = (flag_key or os.environ.get(env_var) or "").strip() or stored

    if not key and provider._interactive():
        key = _prompt_api_key(kind, env_var)
        if not key:
            raise SystemExit(f"No {kind} API key entered.")

    if not key:
        raise SystemExit(
            f"--api {kind} needs your API key. Pass --api-key <key>, "
            f"export {env_var}=..., or run interactively to be prompted."
        )

    # Resolve the endpoint URL: --at overrides the whitelist default (required when whitelist has no base_url).
    endpoint_url = getattr(args, "at", None) or whitelist.base_url
    if not endpoint_url:
        raise SystemExit(
            f"--api {kind} needs an endpoint URL. Pass --at <url> (e.g. --at https://your-doggi-endpoint)."
        )
    # Validate the key: text APIs via /models, media APIs via a lightweight probe.
    if whitelist.supports_model_listing:
        visible = _list_vendor_models(kind, endpoint_url, key)
        served = [model for model in chosen if valid[model].vendor_name in visible]
    else:
        # Media APIs (e.g. Doggi) don't expose GET /models — probe the endpoint to validate the key.
        _probe_media_api(kind, endpoint_url, key)
        visible = {entry.vendor_name for entry in api_catalog.entries_for(kind)}
        served = list(chosen)
    # The validation call above proved the key valid — only now persist it to the machine-local key
    # store, so a mistyped/revoked key is never stored for later joins (and the detached serve
    # process) to reuse silently. A reused stored key skips the no-op rewrite; any NEW key (env or
    # prompted) counts as a rotation the caller must deliver to a live identity via respawn.
    key_rotated = key != stored
    if key_rotated:
        api_keys.store_key(kind, key)
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
        "endpoint_url": endpoint_url,
        "models": served,
        "engine_label": kind,
        "api_kind": kind,
    }], key_rotated


def _prompt_api_key(kind: str, env_var: str) -> str:
    """Hidden interactive prompt for one kind's API key — input is never echoed (getpass). Split
    out so the CLI-seam tests can monkeypatch it (getpass reads the controlling tty)."""
    return getpass.getpass(f"Enter your {kind} API key (input hidden; or export {env_var}): ").strip()


# A request id that cannot exist, used to make the probe below a pure auth check: the lookup is
# rejected before it is resolved when the key is bad, and 404s when the key is good.
_PROBE_REQUEST_ID = "grid-key-probe-does-not-exist"


def _probe_media_api(kind: str, base_url: str, key: str) -> None:
    """Free URL + key check for media APIs that lack `GET /models`. Terminal on either failure —
    nothing is spawned. Never echoes the key.

    Two unauthenticated-cheap GETs instead of one submit:

    1. ``GET /health`` proves ``--at`` really points at a gateway. This is what catches a typo'd
       URL: step 2 alone cannot, because a wrong path 404s exactly like a missing task does.
    2. ``GET /media/generations/<id that cannot exist>`` proves the key. The gateway authenticates
       before it resolves the id, so a bad key is 401/403 and a good one is 404.

    Deliberately NOT a submitted generation: that would bill a real run on **every** join (verified
    against a live gateway — an accepted probe body queues and runs the model for ~30s of GPU) and
    would hardcode a model name that silently breaks the probe the day it is retired.
    """
    import httpx  # lazy: only stdlib + shared.* at module top (see module docstring)

    def _get(path: str, *, auth: bool) -> httpx.Response:
        headers = {"Authorization": f"Bearer {key}"} if auth else {}
        try:
            with httpx.Client(timeout=_VENDOR_LIST_TIMEOUT) as client:
                return client.get(f"{base_url}{path}", headers=headers)
        except httpx.HTTPError as exc:
            raise SystemExit(f"Could not reach {kind} at {base_url}: {exc}") from None

    health = _get("/health", auth=False)
    if health.status_code != 200:
        raise SystemExit(
            f"{base_url} does not look like a {kind} gateway: GET /health returned "
            f"HTTP {health.status_code}. Check the --at URL."
        )

    resp = _get(f"/media/generations/{_PROBE_REQUEST_ID}", auth=True)
    if resp.status_code in (401, 403):
        raise SystemExit(
            f"{kind} rejected the API key (HTTP {resp.status_code}): {resp.text[:200]}"
        )
    # 404 is the expected success shape (authenticated, then no such task); 200 would mean the id
    # somehow exists — still proof the key works. Anything else means the endpoint answers /health
    # but not the media API, so refuse rather than advertise models we cannot serve.
    if resp.status_code not in (200, 404):
        raise SystemExit(
            f"{kind} at {base_url} did not answer the key check as expected "
            f"(HTTP {resp.status_code}): {resp.text[:200]}"
        )


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
    can adopt their engines and stop their processes (they share the token node_id).

    "Alive" is ``record_alive``, not a bare ``pid_alive`` (grid-leave issue 08): a **zombie** pid and a
    **recycled** pid both answered "alive" to the old check, and the idempotent re-join gate above
    turned that into "Already serving …; nothing to append." over an engine that had been dead for
    hours — reported at exit 0, with `grid models` empty. A record that names no running serve child of
    ours is dead, so the join respawns.
    """
    return [
        rec for rec in run_records.read_records(network_id).values()
        if run_records.record_alive(rec)
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
            if "model_caps" in copy:  # a fresh dict, like models above — never alias the incoming spec
                copy["model_caps"] = dict(copy["model_caps"])
            merged.append(copy)
            index[_spec_key(copy)] = copy
            changed = True
            continue
        added = [m for m in (spec.get("models") or []) if m not in (existing.get("models") or [])]
        if added:
            existing["models"] = list(existing.get("models") or []) + added
            changed = True
        # A re-join re-resolves the seat's scalar facts too: refresh plan_type (the seat's tier
        # label) and model_caps (the probe-derived per-model caps serve reads from the record —
        # issue 10a) from the incoming spec, or the engine would keep the caps/rank it FIRST joined
        # with forever. A same-key engine is the same seat, so the freshly-probed values are
        # authoritative. Key-guarded so a non-codex spec, which carries neither, stays byte-identical.
        if "plan_type" in spec:
            existing["plan_type"] = spec["plan_type"]
        if "model_caps" in spec:
            # UNION, not replace: `existing["models"]` only ever GROWS (above), so a model still in
            # the union but dropped by a shrunk fresh re-probe must keep its last-known caps rather
            # than silently degrade to the fail-closed serve entry. Incoming caps win for a re-probed
            # model; a model absent from the fresh probe keeps its prior entry. A fresh dict (never a
            # mutate of either side) so the write can't alias the live record or the incoming spec.
            existing["model_caps"] = {**(existing.get("model_caps") or {}), **spec["model_caps"]}
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


def _identity_record(live: list[dict[str, object]]) -> dict[str, object] | None:
    """The live identity's record — the singleton's if present, else the first live one's."""
    for record in live:
        if record.get("engine_id") == _REMOTE_IDENTITY:
            return record
    return live[0] if live else None


def _identity_field(live: list[dict[str, object]], key: str) -> object:
    """One field of the live identity — the singleton's if present, else the first live record's."""
    record = _identity_record(live)
    return record.get(key) if record is not None else None


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


def _needs_local_process(spec: dict[str, object]) -> bool:
    """True when serving this spec means running a process on this box (a CLI seat)."""
    return api_catalog.local_seat_port(str(spec.get("api_kind") or "")) is not None


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
    # A spec that needs a PROCESS started cannot be hot-reloaded — a reload re-advertises models
    # but launches nothing. That is a built-in `--serve`, and equally a CLI seat, whose loopback
    # server the serve loop starts at spawn. Reloading one would advertise its models against a
    # port with nothing listening.
    if any(not spec.get("endpoint_url") or _needs_local_process(spec) for spec in merged_specs):
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

    **The verdict check below is load-bearing — do not remove it as redundant** (grid-leave issue 08).
    It is not a second opinion on ``_live_records``: ``_leave_one_engine`` reaches here through
    ``survivors or list(records.values())``, which deliberately re-admits exactly the records that
    filter rejected, so a shrink over a dead identity arrives with a **zombie** in hand. Signals to a
    corpse *succeed* — they are discarded, not refused — so ``_signal_reload`` would return cleanly and
    the CLI would print "hot-reloaded — no in-flight requests dropped" over a process dead for hours.
    A recycled pid is worse still: SIGHUP's default disposition is **terminate**, so signalling one
    kills an unrelated process the operator owns. Only ``LIVE_OURS`` — and ``LIVE_UNVERIFIED``, which is
    every record written before tokens existed and must keep behaving exactly as it did — may be
    signalled.
    """
    singleton = live[0]
    pid = run_records.recorded_pid(singleton) or 0
    if pid <= 0 or not run_records.record_alive(singleton):
        _respawn_identity(network_id, record, live)
        return False
    # Carry the identity forward: it is the SAME process, so its token and process group are unchanged,
    # and dropping them here would leave the reloaded record unverifiable until the next respawn.
    record["pid"] = pid
    record["pid_start_time"] = singleton.get("pid_start_time")
    record["pgid"] = singleton.get("pgid")
    # …and its clock. `_build_record` stamps `started_at` = now for every join, which is right for
    # `_respawn_identity` (a genuinely new process) and wrong here: nothing restarted. Left uncarried,
    # any hot-reloadable append to a WEDGED engine resets its uptime, putting it back inside the
    # bring-up quiet window — so the gate reports "still starting (up 0s)" and withholds the
    # `--respawn` suggestion from an engine that has been stuck for ten minutes, and each further
    # append re-extends the grace (grid-leave issue 10 review).
    record["started_at"] = singleton.get("started_at") or record.get("started_at")
    # Its registration travels with it for the same reason and by the same hand (grid-leave issue 10):
    # this process is still the one that registered, so dropping the fact would make the next re-join
    # accuse a healthy engine of never having reached the relay.
    from remote import service_truth  # lazy, per this module's import rule

    service_truth.carry_service_truth(record, singleton)
    run_records.write_record(network_id, _REMOTE_IDENTITY, record)
    try:
        _signal_reload(pid)
        return True
    except ProcessLookupError:  # the process died between the liveness check and the signal — respawn
        _respawn_identity(network_id, record, [])
        return False


def _respawn_identity(
    network_id: str, record: dict[str, object], priors: list[dict[str, object]],
    *, env_overrides: dict[str, str] | None = None,
) -> None:
    """Stop the prior process(es), then write ``record`` and (re)spawn the one detached engine, setting
    ``record["pid"]``. Shared by join-append and leave-shrink (respawn is Slice 1's update mechanism).

    Aborts (SystemExit) BEFORE spawning if any prior can't be confirmed stopped — a second live child on
    the same token-pinned node_id would clobber it (the original bug). Raises if the fresh process dies
    during start-up: the grid is left not serving either way, so the operator must know.

    Each prior goes through ``terminate_recorded``, which checks identity, rather than ``terminate_pid``
    on the bare recorded pid (grid-leave issue 08). The check has to live here rather than in the
    callers: ``_leave_one_engine`` reaches this function through ``survivors or
    list(records.values())``, which re-admits records ``_live_records`` deliberately rejected. With a
    bare pid, a shrink over a stale record SIGTERMed a **recycled** pid and then SIGKILLed its whole
    process group — an unrelated process tree the operator owns — burned the full 25s grace on it, and
    then aborted with "Could not stop the engine(s) …" (measured). It also reaches the serve child a
    stopped launcher shim left behind, through the record's stamped process group.
    """
    engine_id = _REMOTE_IDENTITY
    undead: list[str] = []
    for prior in priors:
        outcome = run_records.terminate_recorded(prior)
        if outcome.survivor:
            undead.append(run_records.describe_survivor(outcome))
            continue
        prior_id = str(prior.get("engine_id") or "")
        if prior_id and prior_id != engine_id:  # drop a legacy record's file so only the singleton remains
            run_records.remove_record(network_id, prior_id)
    if undead:
        raise SystemExit(
            f"Could not stop the engine(s) already serving this grid ({', '.join(undead)}); they may "
            "still be registered on the relay. Investigate before re-joining — starting another would clobber them."
        )

    record.update(run_records.identity_stamp(0))  # clear pid AND its identity — never a stale token
    # …and never a stale registration (grid-leave issue 10). The child about to be spawned has told
    # the relay nothing yet. `started_at` is refreshed with it so "up 42s" measures THIS process:
    # `_leave_one_engine` rebuilds its record by copying a survivor's, which carries the old one's.
    from local import runtime  # lazy, per this module's import rule
    from remote import service_truth

    service_truth.clear_service_truth(record)
    record["started_at"] = runtime.utc_now()
    run_records.write_record(network_id, engine_id, record)
    proc = _spawn_remote_engine(network_id, engine_id, env_overrides=env_overrides)
    # Stamp the identity, not just the pid: this is the ONLY stamp a `grid leave` racing this join
    # can see, because the child's own self-stamp has not run yet (POSIX `flock` gives the lock to
    # whoever asks, in no order). With it, that leave can verify the pid it is about to signal and —
    # when `proc.pid` turns out to be a launcher shim that exits without its child — reach the real
    # serve process through the stamped process group (grid-leave issue 08, residual (b)).
    record.update(run_records.identity_stamp(proc.pid))
    run_records.write_record(network_id, engine_id, record)

    log_path = paths.engines_dir(network_id) / f"{engine_id}.log"
    if _await_remote_engine_start(proc) == "died":
        run_records.remove_record(network_id, engine_id)
        from . import provider

        raise SystemExit(
            f"Engine exited before it started — the grid is not serving now. See {log_path}:\n"
            f"{provider._log_tail(log_path)}"
        )


def _resolve_or_defer(
    args: argparse.Namespace, *, respawn: bool
) -> tuple[list[dict[str, object]], bool, SystemExit | None]:
    """``_resolve_serve_targets``, except that a bare ``grid join --respawn`` may postpone its refusal.

    `--respawn` is also a **restart** — the one-command form of the `grid leave` + `grid join` folklore
    it replaces, which an operator runs with no arguments. But auto-detect probes loopback only, so an
    identity serving `--at <otherhost>` or an API engine has nothing to find, and the refusal would
    land before the CLI ever looked at what is running. Deferring it lets the caller answer from the
    live identity's own union instead, and re-raise untouched when there is no live identity.

    Only a join that names **nothing** defers, which is what makes this safe to express as "catch
    ``SystemExit``". `_resolve_serve_targets` has five refusals; three of them (`--at` without `-m`,
    a bare `-m`, an unmatched `--kind`) are unreachable here because each is guarded by the very flag
    this branch excludes — swallowing one would hide a typo. The two that remain are both about
    **detection**: "no running engine detected" and "multiple engines detected, pass --all". Neither
    is a question `--respawn` needs answered when something is already live, because the union that
    identity is serving IS the answer; deferring the second is what keeps `grid join --respawn`
    working on the multi-engine boxes it is most useful on. `provider._detect` itself cannot raise
    (`shared/system/detect.py` has no `raise`), so nothing else can arrive here.
    """
    named_nothing = not any((
        args.at, args.serve, getattr(args, "models", None), getattr(args, "media", False),
        getattr(args, "kind", None),
    ))
    try:
        specs, media_detected = _resolve_serve_targets(args)
    except SystemExit as exc:
        if not (respawn and named_nothing):
            raise
        return [], False, exc
    return specs, media_detected, None


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


def _spawn_remote_engine(
    network_id: str, engine_id: str, *, env_overrides: dict[str, str] | None = None
) -> subprocess.Popen:
    """Start the detached serve child. ``env_overrides`` wins over this process's own environment.

    The child reads its opt-ins from the environment it inherits, so an override is how the parent
    withholds one it has just decided this provider cannot honour (issue 58). A new dict rather than
    a mutation of `os.environ`: this process may still have work to do, and a knob turned off for a
    child must not be turned off for its parent.
    """
    from local import runtime

    log_path = paths.engines_dir(network_id) / f"{engine_id}.log"
    # `_join_remote` writes the run record immediately before it spawns, so this is not normally the
    # first creator of the run directory — hardened anyway because it is a `mkdir` into that tree,
    # and reordering those two statements must not silently reopen issue 19's hole.
    paths.ensure_dir(log_path.parent)
    log = logging_setup.cap_and_open_append(log_path, logging_setup.engine_log_max_bytes())
    return subprocess.Popen(
        runtime.cli_command() + [run_records.REMOTE_ENGINE_MARKER, network_id, engine_id],
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1", **(env_overrides or {})},
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


def _select_for_leave(name: str | None) -> dict[str, object]:
    """The grid ``grid leave`` acts on — falling back to the run tree when the credentials are gone.

    A credential bundle **always wins**. That ordering is the safety property, not a preference: the
    rescue bundle below carries no ``access_token``, so resolving one for a grid we *are* signed into
    would silently skip the backstop deregister and leave the node registered as a provider.

    The fallback needs an **explicit id** and never a bare/active-grid resolution. ``~/.grid/run/engines``
    accumulates a directory per grid this box has ever joined — 20 of them on the machine that reported
    this bug — and picking one of those by "the sole grid" heuristic would reap whichever the operator
    happened to have fewest of. Naming it is the consent.

    Everything else keeps today's errors, with one addition: when nothing resolves and this box *is*
    still running serve children, the error names them, because the operator who most needs this path
    is the one who does not yet know it exists.
    """
    from remote import credentials

    from . import remote_grid

    if name:
        bundle = remote_grid._by_name(name)
        if bundle is not None:
            return bundle
        if remote_grid._valid_network_id(name) and paths.engines_dir(name).is_dir():
            # Known to the run tree, unknown to the credential store: signed out, or a grid an
            # authoritative `grid sync`/`grid login` overwrite dropped while its child kept serving.
            return {"network_id": name, "name": name}
    if not credentials.load_credentials().get("session_token"):
        raise SystemExit(_signed_out_leave_message())
    return remote_grid._select(name)


def _signed_out_leave_message() -> str:
    """"You're not signed in", plus the grids this box can still be made to stop serving.

    Free to compute — record liveness only, never a process-table read — because it runs on an error
    path that must stay cheap, and because a record-less orphan cannot be named as a suggestion
    anyway: the operator would have nothing to type it against but the directory listing.
    """
    from . import signout

    serving = [
        nid for nid in run_records.known_grid_ids()
        if signout._recorded_live_pids(run_records.read_records(nid))
    ]
    base = "You're not signed in. Run `grid login` to sign in."
    if not serving:
        return base
    listed = ", ".join(serving)
    return (
        f"{base} This box is still serving {listed} — `grid leave <grid-id>` stops one without "
        "signing in (the grid drops its models after the node TTL, ~120s)."
    )


def _records_for_leave(network_id: str, *, shrinking: bool) -> dict[str, dict[str, object]]:
    """This grid's run records — or, for a full leave, an empty set when they cannot be read.

    ``read_records`` globs the whole grid directory and ``jsonio.load_json`` raises ``SystemExit`` on a
    bad parse, so one truncated file used to abort ``cmd_remote_leave`` before the argv sweep and
    before the backstop — the two things that exist precisely for a record that cannot be trusted.
    Measured: exit 1, the child still serving, zero deregisters, and no `grid leave` able to converge
    until the file was removed by hand. A corrupt *sibling* did it too, since the glob is grid-wide.

    Degrading is the record-less repair path arriving at its own premise rather than a widening of
    what we act on: with no records the sweep matches by argv (marker + this exact network id + the
    spawn arity) and the backstop is unconditional, so the box still stops serving and the grid still
    hears about it. Nothing is signalled on the strength of a record we could not parse.

    A ``--engine`` shrink is the one caller that may **not** degrade. Its contract is that the
    survivors keep serving, and honouring that needs the union the broken file holds; an empty union
    would respawn the identity serving nothing while printing success. So it re-raises, which is also
    the one place the operator is told the file is broken.

    The file is **kept** either way. It is neither provably ours nor provably dead, and
    ``remote/CONTEXT.md``'s rule is that a ghost record is the safer failure — so the note names the
    path and leaves the decision to remove it with the operator.
    """
    try:
        return run_records.read_records(network_id)
    except (Exception, SystemExit) as exc:
        if shrinking:
            raise
        print(
            f"Note: a run record for this grid could not be read ({exc}); continuing with the "
            "argv sweep and the relay deregister, which need no record. Remove the file above once "
            "you have looked at it — nothing here will delete a record it cannot identify.",
            file=sys.stderr,
        )
        return {}


def cmd_remote_leave(args: argparse.Namespace) -> int:
    from remote import credentials

    from . import remote_grid

    rec = _select_for_leave(getattr(args, "grid", None))
    network_id = remote_grid._network_id(rec)
    label = rec.get("name") or network_id
    if not rec.get("access_token") and getattr(args, "engine", None) and not getattr(args, "all", False):
        # A `--engine` shrink keeps the identity serving: it respawns (or hot-reloads) the child with
        # the reduced union, and that child has to register with a token. The rescue path has none, so
        # the shrink would stop a working engine and start one that dies on its first relay call. The
        # whole-identity teardown is the only thing this path can honestly do, so say so rather than
        # half-doing the other.
        raise SystemExit(
            f"Can't drop a single engine from {label} while signed out: re-advertising the rest needs "
            f"this grid's token. Run `grid leave {network_id}` to stop the whole identity, or "
            "`grid login` first to shrink it."
        )
    # Soft, not `require_session()`: leave is the repair verb, and the state it most needs to repair
    # is the one where the credentials are gone (ADR 0023). The session is only ever used to resolve a
    # relay URL no record carried; without one that resolve fails and the backstop degrades, which is
    # exactly the right answer for a grid we hold no token for anyway.
    session = str(credentials.load_credentials().get("session_token") or "")

    with file_lock(run_records.record_path(network_id, _REMOTE_IDENTITY)):
        records = _records_for_leave(network_id, shrinking=bool(args.engine and not args.all))
        # `--engine <endpoint_url|label>` shrinks the union. When survivors remain the identity is still
        # a provider, so a shrink sends NO backstop. But its last-engine sub-case (dropping the final
        # engine) IS a full identity teardown, so `_leave_one_engine` runs the same reap (record kills +
        # argv sweep) + unconditional backstop as a bare / `--all` leave — a `--engine <last>` with a
        # stale-pid orphan can't strand a live child or leave the node registered (issue 02). With no
        # records there is no union to shrink, so keep today's dead-end for the targeted form; the
        # idempotent-repair sweep + backstop are only for the bare / whole-identity leave intent.
        if args.engine and not args.all:
            if not records:
                print(f"No engines joined to {label}.")
                return 0
            return _leave_one_engine(args, rec, session, network_id, label, records)

        # Full leave (bare / `--all`). Reap the recorded child(ren) + argv-sweep any orphan a stale/
        # missing record could never reach, THEN send an authoritative CLI backstop deregister
        # UNCONDITIONALLY, so the model drops from the grid immediately even when a child was SIGKILLed,
        # was already dead, or its own unregister was rejected. A bare leave with no records still sweeps
        # + backstops: an idempotent repair for a historical orphan whose record was deleted out from
        # under a live child.
        sent, reaped = _full_leave_teardown(rec, session, network_id, label, records)
        _full_leave_report(label, bool(records), sent, reaped)
        return 0


def _full_leave_survivor_exit(survivors: list[int], label: str, sent: bool) -> None:
    """Raise the honest non-zero ``grid leave`` failure when serve child(ren) survived the reap. The
    message stays honest on two axes: it never claims "record kept" (false for a record-less swept
    orphan), and it asserts the relay deregister only when it actually landed (``sent``) — else it
    would contradict the "didn't land" caveat ``_full_leave_backstop`` already printed to stderr.

    A **negative** entry names a process *group* rather than a pid — the ``kill(1)`` convention, and
    the form the teardown reports when the recorded pid was a launcher shim whose session group
    outlived it (grid-leave issue 08). Rendering one as a "pid" would hand the operator
    ``kill -9 <group leader>``, and the group leader is exactly the process that is already gone.
    Groups are POSIX-only (``process_identity.group_alive`` is ``False`` on Windows, where
    ``taskkill /T`` already takes the whole tree), so a negative entry can never reach the Windows
    remedy below.
    """
    named = ", ".join(run_records.describe_target(value) for value in survivors)
    outcome = (
        f"deregistered {label} from the relay, but could not stop serve child(ren): {named}"
        if sent
        else f"could not stop serve child(ren): {named} (and the relay deregister didn't land — "
        "see above)"
    )
    # The remedy has to be a command the operator can actually run. `kill -9` was safe while the argv
    # sweep was POSIX-only; the sweep reaches Windows now, so this line does too — and that is the last
    # place to print a command that doesn't exist on the machine reading it.
    first = survivors[0]
    remedy = (
        f"taskkill /F /PID {first}" if sys.platform == "win32"
        else f"kill -9 -{-first}" if first < 0
        else f"kill -9 {first}"
    )
    raise SystemExit(
        f"grid leave: {outcome}. A retried `grid leave` will target them again; investigate before "
        f"re-joining (e.g. `{remedy}`)."
    )


def _recorded_pids(records: dict[str, dict[str, object]]) -> set[int]:
    """The process ids the record path will act on — the sweep's exclusion set.

    Exactly ``run_records.recorded_pid``'s answer, and deliberately nothing of its own. The invariant
    is that this set must be **neither wider nor narrower** than what the record teardown actually
    kills, and the only way to keep that true through later edits is to ask the same function the
    teardown asks (``record_verdict`` → ``recorded_pid``). Two parallel readers is precisely how this
    drifted: this one used to coerce a decimal *string* to an int while ``recorded_pid`` answered
    ``None`` for it, so a record carrying ``"4242"`` was declined by the record path (nothing to prove,
    nothing signalled) **and** excluded from the sweep — both nets standing down over one live child,
    behind a ``Teardown(verified=True)`` and a ``Left <grid>.`` at exit 0.

    ``0`` (never stamped — the join write-race) and ``None`` (any shape we can prove nothing about)
    are both dropped: neither names a process to exclude, so the sweep stays free to find the child by
    argv, which is the whole point of having a second net.
    """
    pids: set[int] = set()
    for record in records.values():
        pid = run_records.recorded_pid(record)
        if pid:
            pids.add(pid)
    return pids


class _ReapResult(NamedTuple):
    """What one full-leave reap established. Named rather than a bare tuple because ``survivors`` and
    ``unverified`` are both ``list[int]`` and mean opposite things — one is "still running and we
    failed to stop it", the other "we could not tell" — so a transposition at the call site would be
    invisible to a type checker. Same lesson as ``run_records.Teardown`` one level down."""

    survivors: list[int]   # negative entries name a process GROUP (see `_full_leave_survivor_exit`)
    scanned: bool          # False ⇒ the argv sweep could not read the process table
    unverified: list[int]  # record pids neither stopped as ours nor proven clean
    foreign: tuple[int, ...] = ()  # live children of THIS grid we were not permitted to stop
    partial: bool = False  # the sweep ran but was shown only part of the process table (Windows)


def _full_leave_reap(
    network_id: str, label: str, records: dict[str, dict[str, object]]
) -> _ReapResult:
    """The reap half of a full leave: SIGTERM→SIGKILL the recorded child(ren), then argv-sweep the
    process table for any live serve child a stale/missing record could never reach (the reproduced
    orphan bug) plus a record-less orphan. Prints the reaped/foreign notes; the backstop + report stay
    in the caller (so ``cmd_remote_leave`` stays small).

    Every field of the ``_ReapResult`` is something the caller must not print a clean success over:

    * ``survivors`` — recorded children wedged past SIGKILL PLUS swept orphans that survived, disjoint
      by construction (the sweep excludes the recorded pids, so a wedged recorded child is never
      re-terminated). Fatal: leave exits non-zero naming them.
    * ``scanned`` — False when the sweep could not read the process table at all.
    * ``unverified`` — records the teardown could neither stop **as ours** nor prove clean: a pid
      recycled onto an unrelated process, or long reaped with no session group left to vouch for it.
      Alone that is routine and harmless, because the argv sweep is the other net; together with
      ``scanned=False`` it means this leave checked **nothing** (grid-leave issue 08).
    * ``foreign`` — live children of this grid we were not permitted to stop, and ``partial`` — the
      table was read but mostly hidden from this account (grid-leave issue 15). Neither is fatal;
      both qualify the success line via ``_sweep_caveats``.
    """
    from . import provider  # shared teardown: stops the engine + reaps a media engine's ComfyUI

    record_pids = _recorded_pids(records)
    # BEFORE the kills, not after: a recorded child's launcher shim carries the same argv it does, and
    # the only way to tell them apart is the parent link — which disappears from the process table the
    # moment the kill below succeeds. Asked afterwards this returns nothing, and a healthy leave would
    # then sweep its own launcher and announce a reaped orphan.
    # Guarded for the same reason the sweep below is, and more so: this runs EARLIER, so an exception
    # here would abort the leave before the record kills as well as before the deregister.
    try:
        shim_pids = orphan_sweep.launcher_ancestors(
            run_records.REMOTE_ENGINE_MARKER, network_id, pids=record_pids
        )
    except (Exception, SystemExit) as exc:
        print(f"Note: couldn't identify launcher processes ({exc}).", file=sys.stderr)
        shim_pids = frozenset()
    record_survivors: list[int] = []
    unverified: list[int] = []
    for engine_id, record in records.items():
        outcome = provider._stop_engine(network_id, engine_id, record)
        if outcome.survivor:  # survived even SIGKILL — its record was kept; the caller fails loud, never "Left"
            # Negated for a process group, so the failure message can name it as one and print a
            # remedy that works — see `_full_leave_survivor_exit`.
            record_survivors.append(-outcome.survivor if outcome.is_group else outcome.survivor)
        elif not outcome.verified:
            unverified.append(run_records.recorded_pid(record) or 0)
    # The sweep is a best-effort diagnostic; the backstop deregister that follows it is the mechanism
    # of record. Anything unforeseen here — `taskkill` missing from a stripped Windows image, a parse
    # the enumerators didn't anticipate — must degrade to an honest "couldn't check", never propagate
    # and take the deregister with it, which would leave the model advertised for the full ~120s TTL.
    # (`SystemExit` is this repo's clean-error idiom and is not an `Exception`, hence both.)
    try:
        swept = orphan_sweep.sweep_orphans(
            run_records.REMOTE_ENGINE_MARKER, network_id, exclude_pids=record_pids | shim_pids
        )
    except (Exception, SystemExit) as exc:
        print(f"Note: the scan for orphaned serve children failed ({exc}).", file=sys.stderr)
        swept = orphan_sweep.SweepResult((), (), (), scanned=False)
    if swept.reaped:
        print(
            f"Reaped {len(swept.reaped)} orphaned serve child(ren) on {label} "
            f"(pid(s) {', '.join(map(str, swept.reaped))})."
        )
    for pid in swept.foreign:
        print(
            f"Note: a remote-engine process for {label} (pid {pid}) is owned by another user; "
            "left it alone.",
            file=sys.stderr,
        )
    return _ReapResult(record_survivors + list(swept.survivors), swept.scanned, unverified,
                       foreign=swept.foreign, partial=swept.partial)


def _full_leave_execute(
    rec: dict[str, object], session: str, network_id: str, label: str,
    records: dict[str, dict[str, object]],
) -> tuple[_ReapResult, bool]:
    """Do the full-identity teardown and report what happened, raising nothing.

    Split out of ``_full_leave_teardown`` for the one caller that must not exit on the first bad
    grid: ``grid logout`` tears down every grid this box serves before it deletes the credentials
    that address them, so a survivor on grid A may not abort grid B's teardown — and the decision
    of what to do about it is the sign-out's, not leave's (ADR 0023). Leave keeps its own answer by
    wrapping this; the order (reap, THEN backstop) is the contract and lives here so both share it.
    """
    reaped = _full_leave_reap(network_id, label, records)
    sent = _full_leave_backstop(rec, records, session, network_id, label)
    return reaped, sent


def _full_leave_teardown(
    rec: dict[str, object], session: str, network_id: str, label: str,
    records: dict[str, dict[str, object]],
) -> tuple[bool, _ReapResult]:
    """The authoritative full-identity teardown shared by a bare / ``--all`` leave and a
    ``--engine <last>`` drop: reap (record kills + argv sweep) then the UNCONDITIONAL backstop
    deregister, raising loud (via ``_full_leave_survivor_exit``) if a child survived even SIGKILL.
    Returns ``(sent, reaped)`` so the caller prints its own success line — the wording differs
    between a bare leave and a last-engine drop, but every caveat on it comes from ``_sweep_caveats``
    over this same result, so the two can't drift."""
    reaped, sent = _full_leave_execute(rec, session, network_id, label, records)
    if reaped.survivors:  # a live child survived SIGKILL — its record was kept; fail loud, never "Left"
        _full_leave_survivor_exit(reaped.survivors, label, sent)
    if reaped.unverified and not reaped.scanned:  # both nets failed at once — this leave checked nothing
        _full_leave_unchecked_exit(reaped.unverified, label, sent)
    return sent, reaped


def _full_leave_unchecked_exit(unverified: list[int], label: str, sent: bool) -> None:
    """Fail a leave that could neither confirm its recorded child stopped nor scan for a stray one.

    Two independent nets normally catch a stranded serve child: the run record (stop it by identity)
    and the argv sweep (find it in the process table). Each failing alone is fine and silent — that is
    what the other is for. Both failing at once is the reported bug: leave printed ``Left <grid>.``
    with a footnote and exited **0** having stopped nothing and confirmed nothing, so the operator had
    no signal to act on while a live orphan kept heartbeating as a provider (grid-leave issue 08,
    residual (b)).

    The record is **not** kept here, unlike the survivor case. There is no live process it is a handle
    to — that is the whole point — and an unverifiable record stays unverifiable forever, so keeping it
    would recreate exactly the never-converging retry loop this issue exists to end. A retry runs the
    bare idempotent-repair path (sweep + backstop) instead, which succeeds as soon as the process table
    is readable again.
    """
    pids = ", ".join(str(pid) for pid in unverified if pid) or "the recorded child"
    deregistered = (
        f"deregistered {label} from the relay, but"
        if sent
        else f"could not deregister {label} from the relay (see above), and"
    )
    raise SystemExit(
        f"grid leave: {deregistered} could not confirm the serve child for record pid(s) {pids} was "
        "stopped, and could not read the process table to look for a stray one — so nothing about this "
        "box was verified. Retry `grid leave` once processes are listable "
        f"({'tasklist' if sys.platform == 'win32' else 'ps'} is the check)."
    )


# Remote's wording for the three things a sweep can fail to establish. The precedence between them
# lives in `orphan_sweep.caveats`, shared with local mode; only these strings are remote's own.
_SWEEP_NOTES = orphan_sweep.SweepNotes(
    unscanned=_SWEEP_UNSCANNED_NOTE, partial=_SWEEP_PARTIAL_NOTE, foreign=_SWEEP_FOREIGN_NOTE
)


def _sweep_caveats(reaped: _ReapResult) -> str:
    """Everything a full-leave success line must not leave unsaid, as one suffix. The success line
    itself is worded per caller (a bare leave vs a last-engine drop); what qualifies it never is."""
    return orphan_sweep.caveats(
        scanned=reaped.scanned, partial=reaped.partial, foreign=bool(reaped.foreign),
        notes=_SWEEP_NOTES,
    )


def _full_leave_report(label: str, had_records: bool, sent: bool, reaped: _ReapResult) -> None:
    """Print the honest outcome of a full leave with no surviving child, qualified by whatever the
    sweep could not establish — it did not read the process table, or it found a live child of this
    grid it was not permitted to stop. The backstop dropped the model in every one of those cases, so
    none of them is a failure; each is a reason the success line must not be unconditional
    (silent-failure review: "couldn't check" must not read as "verified clean")."""
    caveats = _sweep_caveats(reaped)
    if had_records:
        print(f"Left {label}.{caveats}")
    elif sent:
        print(f"No engines were tracked for {label}; deregistered it from the relay to be safe.{caveats}")
    else:
        # No local records AND the safety deregister didn't land. Do NOT print the old "No engines
        # joined" dead-end — it reads as "nothing to do" and contradicts the stderr caveat
        # `_full_leave_backstop` just printed; acknowledge the attempted repair honestly.
        #
        # This branch carries the caveats too, and it is the branch that needs them most: it has the
        # least assurance behind it, and it is the ONLY place a partial scan is ever surfaced — a
        # `foreign` match at least prints its own per-pid note from the reap whichever branch follows.
        print(
            f"No engines were tracked for {label}; the safety deregister didn't land "
            f"(see above).{caveats}"
        )


def _backstop_degrade(reason: str) -> bool:
    """Print one best-effort backstop caveat to stderr and report the deregister as not-landed.

    The shared ~120s-TTL fallback tail lives here so the leave-backstop failure branches can't drift
    (a new branch is one ``_backstop_degrade(...)`` call from the same wording). ``reason`` is the
    branch-specific clause and must never carry a token — only the grid label and a status/reason.
    """
    print(f"{reason} Any model this box served drops after the node TTL (~120s).", file=sys.stderr)
    return False


def _full_leave_backstop(
    rec: dict[str, object],
    records: dict[str, dict[str, object]],
    session: str,
    network_id: str,
    label: str,
) -> bool:
    """Send the authoritative CLI-side deregister after a full leave: a relay ``PUT`` role=``consumer``
    (empty models) addressed to the token's JWT ``node_id`` claim — never the run record's junk
    ``node_id`` field — so the model drops from the grid immediately even when the serve child's own
    unregister never ran.

    Best-effort: a missing node identity, a down/unreachable grid, or a relay rejection degrades to a
    clear stderr caveat (the ~120s node TTL is the fallback), never a traceback and never a token in
    the output. Returns whether the relay accepted the deregister. Token refresh on 401 is follow-up
    F4; here an expired token just degrades.
    """
    from remote import credentials, relay

    access_token = str(rec.get("access_token") or "")
    if not access_token:
        # No bundle for this grid at all — a signed-out rescue leave, or a grid an earlier
        # sync/login overwrite dropped. Deliberately NOT the "refresh your token" wording below:
        # there is no token to refresh, and telling a signed-out operator to `grid login` implies
        # the reap they just performed did not count. The reap is real; only the deregister is not.
        return _backstop_degrade(
            f"Couldn't deregister {label} from the relay: no stored credentials for this grid."
        )
    node_id = credentials.node_id_from_token(access_token)
    if not node_id:
        return _backstop_degrade(
            f"Couldn't deregister {label} from the relay: this grid's token carries no node identity — "
            "run `grid login` to refresh it."
        )

    signaling_url, resolve_error = _backstop_signaling_url(rec, records, session, network_id, label)
    if not signaling_url:
        # resolve_relay_base raises for a stopped grid AND for any control-plane failure (429/500/
        # timeout, or a session-token 401), so surface the real reason instead of a blanket "down".
        return _backstop_degrade(
            f"Couldn't reach the relay to deregister {label} ({resolve_error or 'the grid may be down'})."
        )

    try:
        relay.deregister_node(signaling_url, access_token, node_id)
        return True
    except relay.RelayUnauthorized:
        return _backstop_degrade(
            f"The relay rejected the deregister for {label} (token expired) — run `grid login`."
        )
    except relay.RelayError as exc:
        return _backstop_degrade(f"Couldn't deregister {label} from the relay ({exc}).")


def _backstop_signaling_url(
    rec: dict[str, object],
    records: dict[str, dict[str, object]],
    session: str,
    network_id: str,
    label: str,
) -> tuple[str, str]:
    """The relay base to send the backstop to, as ``(url, error_reason)``.

    Prefer the URL the (now-stopped) child was registered against — stored on its run record, so the
    common full-leave case needs no network round-trip; the singleton ``remote`` record wins over any
    legacy ``engine-<uuid>`` sibling that might carry a stale address. With no records (a bare
    idempotent-repair leave) resolve it live from the control plane. A failed live resolve returns
    ``("", str(exc))`` — the real control-plane status/reason, not a blanket "down" — so the caller can
    tell a transient 500/timeout or a session-token 401 apart from a genuinely stopped grid.
    """
    from . import remote_grid

    singleton = records.get(_REMOTE_IDENTITY)
    ordered = ([singleton] if singleton is not None else []) + [
        record for engine_id, record in records.items() if engine_id != _REMOTE_IDENTITY
    ]
    for record in ordered:
        url = str(record.get("signaling_url") or "").rstrip("/")
        if url:
            return url, ""
    try:
        base, _status = remote_grid.resolve_relay_base(session, rec, network_id, label)
        return base.rstrip("/"), ""
    except SystemExit as exc:
        return "", str(exc)


def _leave_one_engine(
    args: argparse.Namespace,
    rec: dict[str, object],
    session: str,
    network_id: str,
    label: str,
    records: dict[str, dict[str, object]],
) -> int:
    """Drop the engine matching ``--engine`` (endpoint URL, or a unique label) from the identity's union.

    Removing the last text engine (with no media) tears the whole identity down — an authoritative full
    leave (reap + argv sweep + unconditional backstop, via ``rec``/``session``), like a bare / ``--all``
    leave; otherwise the singleton is respawned serving the reduced union (no backstop — still a
    provider). Operates on the live record(s), adopting any legacy sibling.
    """
    # `record_alive`, not a bare `pid_alive`: a zombie or a recycled pid is not a serving child
    # (grid-leave issue 08). The `or list(records.values())` fallback then re-admits exactly those
    # records so the union can still be rebuilt — which is why `_hot_reload_identity` and
    # `_respawn_identity` check identity again themselves before signalling anything.
    survivors = [rec for rec in records.values() if run_records.record_alive(rec)]
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

    if not remaining and not media:
        # Dropping the last engine is a full identity teardown, so it is authoritative like a bare /
        # `--all` leave: reap (record kills + argv sweep) then the UNCONDITIONAL backstop, so a stale-pid
        # orphan can't survive here and the node can't stay registered as a provider (issue 02 / A).
        _sent, reaped = _full_leave_teardown(rec, session, network_id, label, records)
        print(f"Left {label} (removed the last engine).{_sweep_caveats(reaped)}")
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
