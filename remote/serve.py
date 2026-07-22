"""The remote provider serve loop — what the detached ``__remote-engine`` subprocess runs.

It mirrors the local engine loop (`cli/provider.py:_run_engine`) but, instead of being forwarded
inbound requests by a grid proxy, it **polls** the hosted relay for work: bring the engine up
through the shared engine layer, probe its capabilities, register them with the relay, then loop
``poll → forward to the local engine → submit result`` while a heartbeat thread keeps the node
live. The per-grid ``access_token`` authenticates every relay call and is refreshed on a 401.

Ported from ``grid-src/grid_cli/provider_runtime/provider/poll_worker.py`` (the threading reworked
into a small ``_ServeState`` + testable units). Engine bring-up + the run record + teardown are
shared with local; only this loop differs (DECISIONS D17). Secrets stay in ``credentials.toml`` — the
run record never carries a token.
"""
from __future__ import annotations

import base64
import json
import os
import signal
import sys
import threading
import time
import traceback
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any, Callable

from remote import api_keys, control_plane, credentials, probe, relay, codex_auth, codex_oauth
from shared.handlers import HANDLERS
from shared import run_records
from shared.filelock import file_lock
from shared.media import media_gating  # stdlib-only module; safe to import eagerly
from shared.models import api_catalog

# One engine's probe result: (normalized llm_url, advertised models, upstream models, caps envelope).
_EngineResults = list[tuple[str, list[str], list[str], dict[str, Any]]]


# Engine read budget when the relay doesn't advertise one (older relay); matches its default.
_DEFAULT_INFERENCE_TIMEOUT = 600.0

# Bounded drain: total budget (shared across workers) for in-flight jobs to finish + submit on
# shutdown before we unregister. A worker parked in a long-poll can't be woken by state.stop, so this
# caps teardown regardless of how many workers are parked.
_DRAIN_TIMEOUT = 5.0

# How long the teardown waits out a codex token exchange caught mid-flight (ADR 0015 D-d): the
# exchange's own vendor timeout (codex_oauth._REFRESH_TIMEOUT) is the true bound — the persist
# after it is milliseconds. Separate from _DRAIN_TIMEOUT: workers parked in long-polls are
# abandonable, a journaled exchange is not (dying mid-exchange loses a rotation the journal can
# then only diagnose). run_records._STOP_GRACE_SECONDS accommodates drain + this wait, or the
# parent's SIGKILL would cut the wait short and make it fiction.
_CODEX_EXCHANGE_DRAIN = 15.0

# Sanity ceiling on max_concurrency: each slot is a real OS thread holding a long-poll, so an absurd
# value (a typo like 200000) would exhaust threads/sockets and crash the process. 256 is at/above any
# realistic single-node batch width (e.g. vLLM max_num_seqs) while keeping a mistyped flag survivable.
_MAX_CONCURRENCY = 256

# How many consecutive post-swap re-register failures a reload retries before giving up loudly (so a
# permanent relay/validation error doesn't PUT every 2s forever; the next join/leave re-triggers it).
_MAX_RELOAD_REGISTER_RETRIES = 5

# The relay-supplied endpoint is interpolated into a local engine URL, so only forward known
# endpoints — this stops a buggy or compromised relay from probing other local paths via `../`.
# Text goes to the LLM engine; media goes to this box's media server, each with its own fixed
# allowlist (the media paths are NOT under `_ALLOWED_ENDPOINTS` — they route to a different URL).
_ALLOWED_ENDPOINTS = frozenset({"chat/completions", "completions"})
_MEDIA_ENDPOINTS = frozenset({"media/image/generate", "media/image/edit", "media/video/i2v"})

# The proactive rotation's due-conditions (ADR 0015 D-d), evaluated on the heartbeat tick so an
# idle grid still rotates. BOTH are conservative picks — the vendor's real rotation window is
# UNVERIFIED (facts.md #6) and untestable without risking a live seat:
# * margin 10 min: ≫ the 30s tick (≈20 attempts survive even a transient-failure gate before real
#   expiry), small enough not to raise rotation frequency — every rotation is one more crash
#   window, so we do NOT rotate earlier than needed;
# * window 24h: err short. Too short costs one cheap exchange per day per box; too long risks an
#   unknown server-side idle-expiry bricking a quiet seat (the "quiet fortnight", PRD story 12).
#   facts.md B6's 43200-minute figure is the QUOTA window, not evidence of token TTL.
_CODEX_EXPIRY_MARGIN = 600.0
_CODEX_ROTATION_WINDOW = 86_400.0

# Failure gates on the codex seat's rotation (ADR 0015 D-d): a definitively refused seat gets one
# polite vendor 4xx per window per process — never one per 30s heartbeat tick plus one per 401ing
# job — while a transient failure retries quickly (jobs are erroring for exactly that long). The
# lock-free store peek in `_CodexSeatHolder.refresh` keeps a cross-process re-sign-in adoptable
# instantly even while gated.
_CODEX_REFUSED_COOLDOWN = 600.0
_CODEX_UNAVAILABLE_COOLDOWN = 60.0

# Opt-in poll/heartbeat tracing. Off by default so a healthy engine's log stays quiet — only errors
# and job failures are recorded (a successful long-poll and a served job are otherwise silent).
# Set GRID_ENGINE_DEBUG=1 before `grid join` to trace every poll cycle when debugging the relay loop.
_DEBUG = bool(os.getenv("GRID_ENGINE_DEBUG"))


def _debug(msg: str) -> None:
    """Emit a poll/heartbeat trace line, but only when GRID_ENGINE_DEBUG is set."""
    if _DEBUG:
        print(f"[engine] {msg}", file=sys.stderr, flush=True)


def _warn(msg: str) -> None:
    """Always log a reload problem to stderr (unlike ``_debug``, which is opt-in tracing). A refused or
    failed hot-reload must leave a trace — the CLI has already told the operator the join/leave
    succeeded, so a silent stale union would be invisible (ADR 0010 D4 F6)."""
    print(f"[engine] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Detached entry
# ---------------------------------------------------------------------------

def run_remote_engine_from_record(grid_id: str, engine_id: str) -> int:
    """Detached ``__remote-engine`` entry: serve one engine to the grid's relay until SIGTERM."""
    record = run_records.read_record(grid_id, engine_id)
    if not record:
        raise SystemExit(f"No engine record for {engine_id} on {grid_id}.")
    network_id = record["grid_id"]  # the run record's grid_id IS the remote network_id
    signaling_url = (record.get("signaling_url") or "").rstrip("/")
    if not signaling_url:
        raise SystemExit("This grid has no relay address; run `grid up` then re-join.")
    access_token, refresh_token = _load_tokens(network_id)
    if not access_token:
        raise SystemExit("Run `grid login` to refresh your grid tokens, then re-join.")
    # The relay binds the node to the token: it authorizes PUT /nodes/{node_id} only for the token's
    # own node (else 403 "Cannot access another node"). So node_id is read from the JWT, never invented.
    node_id = _node_id_from_token(access_token)
    if not node_id:
        raise SystemExit(
            "This grid's access token carries no node identity; run `grid login` to refresh your "
            "tokens, then re-join."
        )
    def _on_term(_signum, _frame):  # noqa: ANN001
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _on_term)
    # Block SIGHUP for the whole startup window: its default disposition is *terminate*, and a concurrent
    # re-join may `os.kill(pid, SIGHUP)` while we're still in engine bring-up/probe — before the handler
    # exists. Blocked, that signal is queued (not fatal) and delivered once we unblock, after the reload
    # watcher is up, so the first reload folds it in. The worker threads spawned below inherit this mask,
    # so SIGHUP only ever lands on the main thread (ADR 0010 C4).
    if hasattr(signal, "SIGHUP"):
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGHUP})

    launched: list[Any] = []
    launcher = None
    media_proc = None
    comfyui_started = False
    state = None
    registered = False
    rc = 0
    try:
        # API-engine keys come from the machine-local key store (env var as fallback) — resolve
        # them up front so a keyless respawn dies naming the fix instead of advertising models
        # whose every job would 401 upstream. Never read from the record; the record never carries
        # a key. Inside the try so this death reaps the record like any died-before-registering
        # engine (the `finally` below).
        bearer_by_url = _api_bearers(record)
        # Build vendor handlers for every API engine in the record. These handle
        # media endpoints (and could handle text endpoints in the future) without
        # going through ComfyUI or a local engine.
        handlers = {}
        for url, kind in _api_kinds_by_url(record).items():
            key = bearer_by_url.get(url)
            if key and kind in HANDLERS:
                handlers[kind] = HANDLERS[kind](base_url=url, api_key=key)
        # Bring up text engines only when the record names some — a media-only join (`grid join
        # --media`) has no text spec, and `_bring_up_engines` would otherwise error on the empty spec.
        has_text = bool(record.get("engines")) or bool(record.get("models")) or bool(record.get("endpoint_url"))
        if has_text:
            engine_results, launched, launcher = _bring_up_engines(record)
        else:
            engine_results = []
        routes, upstream, union_models, capabilities, warnings = _build_routing(engine_results)
        for line in warnings:  # surface shadowed-duplicate models so routing isn't a silent surprise
            print(line, file=sys.stderr)

        # Media engine (ComfyUI + the provider media server) — brought up on this same box and
        # reached by the poll loop on loopback (the relay forwards `media/*` jobs to us like text).
        # Its `comfyui:*` models + caps merge into the one identity we register (DECISIONS D9).
        media_url = None
        media_models: list[str] = []
        if record.get("media"):
            from local import media_engine

            media_port = int(record.get("media_port") or 8190)
            prepared = media_engine.prepare_media_engine(
                media_bundles=list(record.get("media_bundles") or []) or None,
                comfyui_port=int(record.get("comfyui_port") or 8188),
                media_port=media_port,
                advertise_host=None,  # loopback forward — no LAN-facing URL needed in remote mode
            )
            media_proc = prepared["proc"]
            comfyui_started = bool(prepared["comfyui_started"])
            if comfyui_started:
                # Persist ownership so `grid leave` reaps only a ComfyUI THIS engine started.
                run_records.update_record(grid_id, engine_id, comfyui_started=True)
            media_url = f"http://127.0.0.1:{media_port}"
            media_models = list(prepared["models"])
            union_models, capabilities = _merge_media(union_models, capabilities, media_models)

        state = _ServeState(
            signaling_url=signaling_url,
            node_id=node_id,
            network_id=network_id,
            llm_url=engine_results[0][0] if engine_results else "",
            access_token=access_token,
            refresh_token=refresh_token,
            models=union_models,
            capabilities=capabilities,
            meta=_meta(record, engine_id),
            pricing=_pricing(record),
            # Explicit --max-concurrency wins; else 8 for an API-only union, 1 otherwise — one
            # shared rule with the CLI's hot-reload gate (run_records.effective_max_concurrency).
            max_concurrency=run_records.effective_max_concurrency(record),
            routes=routes,
            upstream=upstream,
            media_url=media_url,
            bearer_by_url=bearer_by_url,
            api_kind_by_url=_api_kinds_by_url(record),
            handlers=handlers,
        )
        # The codex fail-fast, after the state exists and before anything advertises: a codex spec
        # with no stored seat dies HERE naming the fix (still inside the try, so the record is
        # reaped like any died-before-registering engine), never as N per-job 401s.
        _prime_codex_seat(state, record)
        register_once(state)
        registered = True
        print(f"Engine {state.node_id} serving {union_models} via the relay at {signaling_url}")
        print("Send SIGTERM (grid leave) to unregister.")
        # Make the engine reload-ready (install the SIGHUP handler + start the reload daemon) while SIGHUP
        # is still blocked, so that daemon inherits the block too. `_serve_loop` then spawns the heartbeat +
        # N poll workers (also inheriting the block) and unblocks SIGHUP on THIS main thread LAST, so a
        # `grid join`/`leave` signal can only land here — its park takes EINTR (PEP 475), the handler sets
        # the reload event, and the reload daemon services it; a worker is never interrupted (ADR 0010 C4).
        reload_thread = _start_reload_watcher(state, engine_id, engine_results, media_models, record)
        _serve_loop(state, reload_thread)  # heartbeat + max_concurrency poll workers; blocks until stop/SIGTERM
    except KeyboardInterrupt:
        print("\nEngine unregistered.")
    except (Exception, SystemExit) as exc:  # detached top level: report, tear down, exit non-zero
        print(f"Remote engine stopped: {exc}", file=sys.stderr)
        rc = 1
    finally:
        if state is not None:
            state.stop.set()
            try:
                relay.unregister_node(state.signaling_url, state.token(), state.node_id)
            except Exception as exc:  # best-effort drain; never mask the real exit
                print(f"Unregister failed (ignoring): {exc}", file=sys.stderr)
        if launcher is not None:  # stop only the built-in servers we launched (external engines stay up)
            for proc in launched:
                try:
                    launcher.stop(proc)
                    print(f"Stopped llama-server (pid={proc.proc.pid}).")
                except Exception as exc:  # best-effort teardown; never mask the real exit
                    print(f"Stopping llama-server failed (ignoring): {exc}", file=sys.stderr)
        if media_proc is not None:  # stop the media server we launched
            from local import media_runtime

            try:
                media_runtime.stop_media_server(media_proc)
                print("Stopped engine media server.")
            except Exception as exc:  # best-effort teardown; never mask the real exit
                print(f"Stopping media server failed (ignoring): {exc}", file=sys.stderr)
        if comfyui_started:  # only stop ComfyUI if WE started it (not one the operator was running)
            from shared.engine import comfyui

            try:
                comfyui.stop()
                print("Stopped ComfyUI.")
            except Exception as exc:  # best-effort teardown; never mask the real exit
                print(f"Stopping ComfyUI failed (ignoring): {exc}", file=sys.stderr)
        if not registered:
            # Reap the on-disk record for an engine that died before registering (e.g. a media engine
            # whose ComfyUI never became ready), so it doesn't linger and force a `grid leave --all`.
            try:
                run_records.record_path(grid_id, engine_id).unlink(missing_ok=True)
            except OSError as exc:  # best-effort teardown; never mask the real exit
                print(f"Reaping stale record failed (ignoring): {exc}", file=sys.stderr)
    return rc


# ---------------------------------------------------------------------------
# Engine bring-up (shared layer, mirrors cli/provider._run_engine)
# ---------------------------------------------------------------------------

def _bring_up_engines(
    record: dict[str, Any],
) -> tuple[list[tuple[str, list[str], list[str], dict[str, Any]]], list[Any], Any]:
    """Bring up every engine the record lists and probe each (mirrors cli/provider._run_engine).

    Returns ``(engine_results, launched, launcher_module)`` where ``engine_results`` is
    ``[(llm_url, advertised_models, upstream_models, caps_envelope), ...]`` in record order — fed to
    ``_build_routing``. ``upstream_models`` is what the *local engine answers to* (the real model name
    for an external ``--at`` engine; the ``--advertise-as`` alias for a built-in llama-server launched
    with ``--alias``), so a job's advertised model can be rewritten to it before forwarding. Every model
    a spec serves is probed by its upstream name (Ollama/vLLM only know that) but the caps envelope is
    keyed by the advertised name (what consumers ask for), so a spec serving several models advertises
    caps for all of them, not just the first. ``launched`` collects the built-in llama-servers to stop
    on teardown (empty when every engine is external). Only a built-in ``--serve`` launches, and only as
    the **sole** engine: ``grid join --all`` gathers already-running engines, so a multi-engine record
    is all external URLs.
    """
    specs = record.get("engines") or [_flat_spec(record)]
    aliases = list(record.get("advertise_as") or [])
    if len(specs) > 1 and any(not spec.get("endpoint_url") for spec in specs):
        raise SystemExit("Serving several engines needs external endpoints; the built-in engine serves one model.")

    results: list[tuple[str, list[str], list[str], dict[str, Any]]] = []
    launched: list[Any] = []
    launcher_mod = None
    try:
        for spec in specs:
            llm_url, proc, mod, advertised, upstream = _bring_up_one(spec, record, aliases)
            if proc is not None:
                launched.append(proc)
                launcher_mod = mod
            # Probe EVERY model this spec serves (not just the first), so a multi-model `--at` advertises
            # caps for all of them — shared with the hot-reload path (`_reload_once`) so the two can't drift.
            caps = _probe_spec_caps(
                llm_url, advertised, upstream, record.get("ctx_size"), api_kind=spec.get("api_kind"),
                plan_type=spec.get("plan_type"),
            )
            results.append((llm_url, advertised, upstream, caps))
    except BaseException:  # a later spec failed — don't orphan a server an earlier spec already launched
        if launcher_mod is not None:
            for proc in launched:
                launcher_mod.stop(proc)
        raise
    return results, launched, launcher_mod


def _flat_spec(record: dict[str, Any]) -> dict[str, Any]:
    """A record written before multi-engine (no ``engines``) → one spec from its flat fields.
    Never carries ``api_kind``: api specs postdate the ``engines`` array, so a flat record can't
    hold one — if that invariant ever breaks, the spec would silently degrade to a hardware engine."""
    return {
        "endpoint_url": record.get("endpoint_url"),
        "models": list(record.get("models") or []),
        "engine_label": record.get("engine_label"),
    }


def _bring_up_one(
    spec: dict[str, Any], record: dict[str, Any], aliases: list[str]
) -> tuple[str, Any, Any, list[str], list[str]]:
    """Resolve one engine's URL, launching the built-in llama-server for ``--serve``.

    Returns ``(llm_url, launched, launcher_module, advertised_models, upstream_models)``. ``upstream``
    is what the engine itself answers to: the **real** model names for an external ``--at`` engine
    (Ollama/vLLM don't know the ``--advertise-as`` alias), but the **alias** for a built-in llama-server
    — it is launched with ``--alias advertised``, so that alias *is* its model name. For an external
    engine nothing is launched (``launched``/``launcher`` are ``None``). Launch tuning (port, ctx, …)
    comes from the record's top-level fields — only the single built-in path consumes them.
    """
    models = list(spec.get("models") or [])
    api_kind = spec.get("api_kind")
    if api_kind:
        # API engine: the advertised names ARE the namespaced whitelist names (aliases never touch
        # them) and the upstream names are the vendor names they embed; nothing is launched.
        upstream = [_api_upstream_name(api_kind, model) for model in models]
        return (spec.get("endpoint_url") or "").rstrip("/"), None, None, list(models), upstream
    advertised = _advertised_models(models, aliases)
    endpoint_url = spec.get("endpoint_url")
    if endpoint_url:  # external engine: forward to it (by its real model name), launch nothing
        return endpoint_url.rstrip("/"), None, None, advertised, list(models)
    if not models:
        raise SystemExit("Provide a model to serve (--serve <model>) or point at one (--at <url> -m <model>).")
    if len(models) != 1:
        raise SystemExit("Built-in engine launch supports exactly one model. Use --at for custom engines.")

    from shared.engine import launcher as launcher_mod

    port = int(record.get("endpoint_port") or 8081)
    if launcher_mod.is_port_in_use(port):
        raise SystemExit(f"Port {port} already in use; aborting.")
    launcher_mod.assert_supported_build()
    launched = launcher_mod.start_llm(
        models[0],
        port=port,
        ctx_size=record.get("ctx_size"),
        n_predict=record.get("n_predict"),
        parallel=record.get("parallel"),
        flash_attn=record.get("flash_attn"),
        temp=record.get("temp"),
        reasoning_budget=record.get("reasoning_budget"),
        alias=advertised[0],
    )
    print(f"Spawned llama-server pid={launched.proc.pid}, log={launched.log}")
    try:
        launcher_mod.wait_for_models(launched)
    except BaseException:
        # Don't orphan the llama-server if it never became ready (load failure / timeout / SIGTERM).
        launcher_mod.stop(launched)
        raise
    # The relay forwards to the engine on *this* box, so the loop reaches it on loopback. The built-in
    # llama-server is launched with ``--alias advertised[0]``, so it answers to the alias: upstream == advertised.
    return f"http://127.0.0.1:{port}/v1", launched, launcher_mod, advertised, list(advertised)


def _advertised_models(models: list[str], aliases: list[str]) -> list[str]:
    if not aliases:
        return list(models)
    if len(aliases) != len(models):
        raise SystemExit("--advertise-as must be provided once for each model.")
    cleaned = [alias.strip() for alias in aliases]
    if any(not alias for alias in cleaned):
        raise SystemExit("--advertise-as values cannot be empty.")
    if any(alias.startswith("comfyui:") for alias in cleaned):
        # `comfyui:*` is the reserved media namespace; aliasing a text model into it would clobber a
        # media capability entry at register time (matches the guard in cli/provider._advertised_text_models).
        raise SystemExit("--advertise-as is only for text models; media models use fixed comfyui:* names.")
    if len(set(cleaned)) != len(cleaned):
        raise SystemExit("--advertise-as values must be unique.")
    return cleaned


def _api_bearers(record: dict[str, Any]) -> dict[str, str]:
    """{vendor base URL: Bearer} for every API spec in the record.

    A kind's credential SHAPE lives in `api_keys.require_bearer` — one metered key for openai, an
    OAuth bundle's access token for codex — so this stays shape-blind and never consults the
    whitelist's env var (ADR 0015 D-c: an OAuth kind has no env-var input path, and a name guessed
    here would hand a stray `CODEX_API_KEY` the seat's job).

    A credential missing everywhere is terminal: better to die naming the fix than to serve models
    whose every job errors upstream. The credential never appears in the message.
    """
    bearers: dict[str, str] = {}
    for spec in record.get("engines") or []:
        kind = spec.get("api_kind")
        if not kind:
            continue
        if str(kind) == api_catalog.CODEX_KIND:
            # ADR 0015 D-d: the codex seat lives in `_CodexSeatHolder`, resolved at forward time —
            # a copy in the snapshot would go stale at the first rotation. The die-before-advertise
            # gate moves with it (`_prime_codex_seat`), so a not-signed-in respawn still dies here
            # at startup, not per job.
            continue
        bearers[(spec.get("endpoint_url") or "").rstrip("/")] = api_keys.require_bearer(str(kind))
    return bearers


def _prime_codex_seat(state: _ServeState, record: dict[str, Any]) -> None:
    """Prime the seat holder when ``record`` serves a codex engine — the die-before-advertise gate
    that used to live in ``_api_bearers`` for every kind, moved with the credential (ADR 0015 D-d).

    ONE derivation for startup and the hot-reload so the two can't drift: at startup a missing
    seat is terminal before anything advertises; at reload the same ``SystemExit`` is absorbed by
    ``_reload_loop``'s catch, refusing the reload with ``last_reload_error`` set and the old
    routing intact. A record with no codex spec never touches the store at all — an unprimed
    holder is inert, and a hardware-only engine must not go near a seat another grid may own.
    """
    if any(spec.get("api_kind") == api_catalog.CODEX_KIND for spec in record.get("engines") or []):
        state.codex_seat.prime_from_store()


def _api_kinds_by_url(record: dict[str, Any]) -> dict[str, str]:
    """{vendor base URL: service kind} for every API spec in the record — the endpoint-gating map.

    Derived from the record (not the probe results) at startup AND on every reload, so it follows
    `grid leave --engine <kind>` hot-reloads. Unlike the bearers it lives on the reload-swappable
    snapshot: it must never mark a URL an engine no longer serves.
    """
    return {
        (spec.get("endpoint_url") or "").rstrip("/"): str(spec.get("api_kind"))
        for spec in record.get("engines") or []
        if spec.get("api_kind")
    }


def _api_upstream_name(api_kind: str, advertised: str) -> str:
    """The vendor model name behind an advertised ``<kind>:<vendor>`` name. Whitelist-first (the
    single source of truth); prefix-strip as the fallback so a catalog edit between join and respawn
    degrades to a sane rewrite instead of forwarding the namespaced name verbatim."""
    entry = api_catalog.find_advertised(api_kind, advertised)
    if entry is not None:
        return entry.vendor_name
    return advertised.partition(":")[2] or advertised


def _api_unsupported_params(api_kind: str, body: dict[str, Any]) -> list[str]:
    """Params in ``body`` the vendor is known to reject (catalog fact), null values excluded —
    the vendors accept an explicit null, so only a real value earns a refusal."""
    whitelist = api_catalog.WHITELISTS.get(api_kind)
    if whitelist is None:
        return []
    return [p for p in whitelist.unsupported_params if body.get(p) is not None]


def _refuse_unsupported_api_params(state: _ServeState, txn: str, api_kind: str, params: list[str]) -> None:
    """Refuse the job wearing the vendor's own error shape — byte-for-byte what forwarding would
    have earned ("engine error 400: {openai-style json}"), so consumers and the relay's
    terminal-error mapper see no difference, minus the vendor round-trip."""
    param = params[0]
    payload = json.dumps({"error": {
        "message": f"Unsupported parameter: '{param}' is not supported with this model.",
        "type": "invalid_request_error",
        "param": param,
        "code": "unsupported_parameter",
    }})
    _try_submit_error(state, txn, f"engine error 400: {payload}")


def _adapt_output_token_param(body: dict[str, Any], api_kind: str | None) -> dict[str, Any]:
    """Rename the output-token cap to the vendor's parameter when forwarding to an API engine.

    The relay's contract layer normalises every request to ``max_tokens`` — the only name hardware
    engines understand — including rewriting a consumer's ``max_completion_tokens`` into it. A vendor
    that renamed the parameter (OpenAI's GPT-5.x) then 400s on every job, so translate on the way
    out. ``max_tokens`` holds the value the relay validated against its cap, so it wins over any
    ``max_completion_tokens`` left beside it by that rewrite; only one name may go upstream.

    Returns ``body`` unchanged for hardware engines, for vendors that still take ``max_tokens``, and
    for vendors that take no output cap at all.
    """
    if not api_kind:
        return body
    whitelist = api_catalog.WHITELISTS.get(api_kind)
    param = whitelist.max_output_param if whitelist else "max_tokens"
    # `param is None` means the vendor has no output-cap parameter under ANY name (codex — facts.md
    # #1), so there is nothing to rename to and the body is left alone; `unsupported_params` refuses
    # a real value before the round-trip. It must be tested FIRST and on its own: the `"max_tokens"
    # not in body` disjunct below is a key-PRESENCE check, so `max_tokens: null` — which
    # `_api_unsupported_params` deliberately lets through — would otherwise reach `adapted[param]`
    # and write `adapted[None] = None`, i.e. a literal `{"null": null}` on the wire.
    if param is None or param == "max_tokens" or "max_tokens" not in body:
        return body
    adapted = {k: v for k, v in body.items() if k not in ("max_tokens", param)}
    adapted[param] = body["max_tokens"]
    return adapted


def _static_api_caps(api_kind: str, advertised: list[str], plan_type: str | None = None) -> dict[str, Any]:
    """An API engine's caps envelope from the static whitelist — API engines are never live-probed
    or benchmarked (ADR 0012); the vendor sees no traffic until a real job forwards. A model missing
    from the whitelist (catalog edited between join and respawn) degrades like a failed probe: an
    all-False entry, never a crash.

    ``plan_type`` (codex only) is the seat's stored subscription tier — the row ``vendor_rank`` is
    read from (issue 03). ``None`` degrades to the minimal row via ``codex_vendor_rank``; it is
    ignored for every other kind."""
    no_features = dict.fromkeys(
        ("vision", "tools", "parallel_tool_calls", "json_object", "json_schema"), False
    )
    caps_models: dict[str, Any] = {}
    for advertised_model in advertised:
        entry = api_catalog.find_advertised(api_kind, advertised_model)
        if entry is None:
            # A local data-integrity condition, not a transient probe failure — leave a trace, or
            # tool/vision consumers break with no diagnostic trail (matches the reload _warn style).
            _warn(
                f"{advertised_model!r} is no longer in the {api_kind} whitelist "
                "(catalog changed since join) — advertising it with no capabilities"
            )
        if api_kind == api_catalog.CODEX_KIND:
            # The honest responses-only entry (issue 05): endpoints ["responses"], no chat-dialect
            # flags, no output cap — a codex envelope must never look like a chat model's. plus the
            # join-time vendor_rank (issue 03): the model's position in the seat's tier row, or None
            # when it isn't in that row (guard so the entry=None degrade never dereferences it).
            rank = api_catalog.codex_vendor_rank(plan_type, entry.vendor_name) if entry else None
            if entry is not None and rank is None:
                # Resolves in the flat whitelist union but has no slot in THIS seat's tier row: the
                # tier table was edited between join and respawn (drift), the same class as the
                # entry-is-None warn above. Advertise it, just without a rank — but leave a trail, or
                # an absent rank looks identical to "this seat simply doesn't rank this model".
                _warn(
                    f"{advertised_model!r} has no rank in this codex seat's tier row "
                    "(catalog changed since join) — advertising it without a capability rank"
                )
            caps_models[advertised_model] = probe.codex_capability_entry(entry, vendor_rank=rank)
            continue
        probed = api_catalog.probed_features(entry) if entry else no_features
        ctx = entry.context_window if entry else None
        # Media API engines (e.g. Doggi) advertise media endpoints; text API engines advertise
        # chat/completions only (the gate in handle_job refuses legacy completions for text APIs).
        endpoints = ["media"] if api_kind in HANDLERS else ["chat/completions"]
        env = probe.envelope(advertised_model, probed, ctx, endpoints=endpoints)
        caps_models.update((env or {}).get("models") or {})
    return {"schema_version": 1, "models": caps_models} if caps_models else {}


def _probe_spec_caps(
    llm_url: str, advertised: list[str], upstream: list[str], ctx_size: Any,
    api_kind: str | None = None, plan_type: str | None = None,
) -> dict[str, Any]:
    """Probe EVERY model a spec serves into one caps envelope — keyed by the advertised name, probed by
    the upstream name (Ollama/vLLM only know that). Shared by startup (`_bring_up_engines`) and the
    hot-reload path (`_reload_once`) so a multi-model `--at` engine advertises caps for ALL its models on
    BOTH paths, not just the first (main's 5078c8c fix, kept in ONE place so the two can't drift). N
    sequential probes at join/reload (one-time; N is small). A failed probe returns an all-False entry for
    that one model (`probe.capabilities` never raises), so one bad model can't sink the node; ``{}`` only
    when the spec serves no models. An API spec (``api_kind``) takes its caps from the static whitelist
    instead — no probe ever targets the vendor — via the same seam so startup and reload can't drift.
    """
    if api_kind:
        return _static_api_caps(api_kind, advertised, plan_type=plan_type)
    caps_models: dict[str, Any] = {}
    for advertised_model, upstream_model in zip(advertised, upstream, strict=True):
        env = probe.capabilities(
            llm_url, upstream_model, advertise_as=advertised_model, context_window=ctx_size,
        )
        caps_models.update((env or {}).get("models") or {})
    return {"schema_version": 1, "models": caps_models} if caps_models else {}


def _build_routing(
    engine_results: list[tuple[str, list[str], list[str], dict[str, Any]]],
) -> tuple[dict[str, str], dict[str, str], list[str], dict[str, Any], list[str]]:
    """Merge several local engines into one remote identity's routing state (DECISIONS D9).

    ``engine_results`` is ``[(llm_url, advertised, upstream, caps_envelope), ...]`` in detect order —
    ``advertised`` and ``upstream`` are parallel per-engine lists (the advertised name and the name the
    engine itself answers to). Returns ``(routes, upstream_routes, union_models, merged_caps, warnings)``:

    - ``routes`` — ``{advertised_model: llm_url}``; the **first** engine to advertise a model wins.
    - ``upstream_routes`` — ``{advertised_model: upstream_model}``; how a forwarded job's model is
      rewritten to what the local engine expects (identity unless ``--advertise-as`` aliased it).
    - ``union_models`` — every advertised model once, in first-seen order (what the identity registers).
    - ``merged_caps`` — one ``{"schema_version": 1, "models": {...}}`` envelope, first-wins per model;
      ``{}`` when nothing probed (registers text-only, like the single-engine path).
    - ``warnings`` — one human line per shadowed duplicate, so the operator sees why a second engine's
      copy of a model is ignored.

    A failed probe degrades to ``{}`` upstream (``probe.capabilities``), so the caps merge reads
    ``env.get("models") or {}`` and never KeyErrors the whole table on one bad engine.
    """
    routes: dict[str, str] = {}
    upstream_routes: dict[str, str] = {}
    union_models: list[str] = []
    merged_models: dict[str, Any] = {}
    warnings: list[str] = []
    for llm_url, advertised, upstream, caps in engine_results:
        caps_models = (caps or {}).get("models") or {}
        for model, upstream_model in zip(advertised, upstream):
            if model in routes:
                warnings.append(
                    f"Two engines serve model {model!r}; routing it to the first ({routes[model]!r}) "
                    f"and ignoring {llm_url!r}."
                )
                continue
            routes[model] = llm_url
            upstream_routes[model] = upstream_model
            union_models.append(model)
            if model in caps_models:
                merged_models[model] = caps_models[model]
    merged_caps = {"schema_version": 1, "models": merged_models} if merged_models else {}
    return routes, upstream_routes, union_models, merged_caps, warnings


def _merge_media(
    union_models: list[str], capabilities: dict[str, Any], media_models: list[str]
) -> tuple[list[str], dict[str, Any]]:
    """Merge this box's media (``comfyui:*``) models + caps into the text routing union (DECISIONS D9).

    Media models come after the text ones (first-seen order), each with the static media capability
    stub — so both startup and a hot-reload produce the same union/caps for the same media bundles.
    A no-media identity passes ``media_models=[]`` and the union is returned unchanged.
    """
    if not media_models:
        return union_models, capabilities
    from shared.media import media_gating

    models = list(union_models)
    caps_models = dict((capabilities or {}).get("models") or {})
    for model in media_models:
        if model not in models:
            models.append(model)
        caps_models[model] = media_gating.capability_entry()
    return models, {"schema_version": 1, "models": caps_models}


def _assemble_snapshot(
    engine_results: _EngineResults, media_models: list[str], record: dict[str, Any], engine_id: str,
    max_concurrency: int,
) -> _Snapshot:
    """Build one reload routing snapshot from probe results + this box's media models (drives the hot-
    reload; startup builds ``_ServeState`` directly). Reuses `_build_routing`/`_merge_media`/`_meta` so a
    reload's routing/caps/meta never drift from startup (ADR 0010 D4), and surfaces shadowed-model
    warnings to stderr exactly as startup does. ``max_concurrency`` is the LIVE pool size passed by the
    caller, NOT re-read from the record: `_serve_loop` sizes the N-worker pool once at startup and a
    reload can't resize it, so the advertised capacity must stay pinned to the real pool — changing
    `--max-concurrency` needs a respawn to take effect (`_hot_reloadable` keeps a live value unchanged)."""
    routes, upstream, union_models, capabilities, warnings = _build_routing(engine_results)
    for line in warnings:
        print(line, file=sys.stderr)
    union_models, capabilities = _merge_media(union_models, capabilities, media_models)
    return _Snapshot.build(
        routes=routes, upstream=upstream, models=union_models, capabilities=capabilities,
        meta=_meta(record, engine_id), pricing=_pricing(record),
        max_concurrency=max_concurrency,
        api_kind_by_url=_api_kinds_by_url(record),
        bearer_by_url=_api_bearers(record),  # re-read the key store so an appended api engine forwards with auth
    )


def _meta(record: dict[str, Any], engine_id: str) -> dict[str, Any]:
    """How the node appears on the grid page: name + engine kind label.

    The display name comes from the record's ``meta_name`` (the ``--name`` a remote operator gave, or
    the box's hostname when omitted), falling back to ``engine_id`` for a record written before the
    singleton change. A multi-engine identity shows the kinds it gathered (e.g. ``ollama+vllm``) when no
    explicit ``--engine-label`` was given, so the page reflects what is actually serving.
    """
    label = record.get("engine_label")
    if not label:
        kinds = [e.get("engine_label") for e in (record.get("engines") or []) if e.get("engine_label")]
        if kinds:
            label = "+".join(dict.fromkeys(kinds))
    if not label:
        # An all-external union is "external"; only a built-in `--serve` spec (no endpoint_url) launches
        # llama.cpp. Derive from the specs so a multi-engine external union isn't mislabelled llama.cpp.
        specs = record.get("engines") or (
            [_flat_spec(record)] if (record.get("endpoint_url") or record.get("models")) else []
        )
        if specs:
            label = "llama.cpp" if any(not s.get("endpoint_url") for s in specs) else "external"
        elif record.get("media"):  # a media-only identity has no text engine to name
            label = "comfyui"
        else:
            label = "llama.cpp"
    return {"name": record.get("meta_name") or engine_id, "engine": label}


def _pricing(record: dict[str, Any]) -> dict[str, float]:
    # Deprecated: the engine no longer advertises a price at register time. Pricing is authoritative,
    # per-provider, and set explicitly with `grid price set` (relay `grid_chat_pricing`). Always {} so a
    # stale `--pricing-input/output` in an old run record can't reintroduce an advertised price.
    return {}


def _media_signature(record: dict[str, Any]) -> tuple[bool, tuple[str, ...], int, int]:
    """See ``shared.run_records.media_signature`` — one shared definition so the CLI's hot-reload-vs-
    respawn choice and this reload guard can't desync (ADR 0010 D4 F6 / C3)."""
    return run_records.media_signature(record)


def _load_tokens(network_id: str) -> tuple[str | None, str | None]:
    for net in credentials.load_credentials().get("networks") or []:
        if net.get("network_id") == network_id:
            return net.get("access_token"), net.get("refresh_token")
    return None, None


def _node_id_from_token(access_token: str) -> str:
    """The provider node_id, read from the per-grid access token's JWT ``node_id`` claim.

    The relay authorizes ``PUT /nodes/{node_id}`` only for the node the token belongs to — any other
    id is rejected with 403 "Cannot access another node". So node_id is NOT ours to invent (a random
    ``node-<uuid>`` is exactly what the relay refuses); it must come from the token. Decode the JWT
    payload best-effort and read the claim — no signature check (the relay verifies server-side; we
    only need the claim to address our own node). Returns "" when the token isn't a decodable JWT
    carrying a node_id, so the caller can surface a clean re-login error.
    """
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # restore the base64 padding a JWT strips
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, json.JSONDecodeError):
        return ""
    node_id = claims.get("node_id") if isinstance(claims, dict) else None
    return str(node_id) if node_id else ""


# ---------------------------------------------------------------------------
# Serve state (thread-safe token + load shared by the poll loop and heartbeat)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Snapshot:
    """The identity's reload-swappable routing, as one immutable object. ``_reload_once`` builds a new
    snapshot and ``_ServeState.apply`` rebinds it in a single atomic reference store, so readers that
    bind it once never see a torn half-update (ADR 0010 D4 F4). ``build`` is the sole normalization
    site: it copies every dict/list, so a published snapshot is never mutated in place.
    """
    routes: dict[str, str]        # advertised model -> local engine URL (normalized, no trailing /)
    upstream: dict[str, str]      # advertised model -> the name the engine itself answers to
    models: list[str]             # advertised union, first-seen order (text engines, then media)
    capabilities: dict[str, Any]  # {"schema_version": 1, "models": {...}} envelope, or {}
    meta: dict[str, Any]          # grid-page {name, engine}
    pricing: dict[str, float]     # always {} today (advertised pricing is deprecated)
    max_concurrency: int
    api_kind_by_url: dict[str, str]  # vendor base URL -> service kind (endpoint gating); {} = none
    bearer_by_url: dict[str, str]    # vendor base URL -> API key (forward auth); {} = none

    @staticmethod
    def build(
        *,
        routes: dict[str, str],
        upstream: dict[str, str],
        models: list[str],
        capabilities: dict[str, Any],
        meta: dict[str, Any],
        pricing: dict[str, float],
        max_concurrency: int,
        api_kind_by_url: dict[str, str] | None = None,
        bearer_by_url: dict[str, str] | None = None,
    ) -> "_Snapshot":
        return _Snapshot(
            routes={model: url.rstrip("/") for model, url in routes.items()},
            upstream=dict(upstream or {}),
            models=list(models),
            capabilities=dict(capabilities or {}),
            meta=dict(meta or {}),
            pricing=dict(pricing or {}),
            api_kind_by_url={url.rstrip("/"): kind for url, kind in (api_kind_by_url or {}).items()},
            bearer_by_url={url.rstrip("/"): key for url, key in (bearer_by_url or {}).items()},
            # Clamp to [1, _MAX_CONCURRENCY]: each slot becomes a real poll-worker thread in `_serve_loop`,
            # so an absurd `--max-concurrency` can't exhaust threads/sockets. This is the sole clamp site,
            # so a reload that changes max_concurrency is bounded the same way as startup.
            max_concurrency=min(_MAX_CONCURRENCY, max(1, int(max_concurrency))),
        )


class _CodexSeatHolder:
    """The codex seat's live credential, OUTSIDE the routing snapshot (ADR 0015 D-d).

    A rotation must not rebuild routing or race a hot-reload swap, so unlike the openai bearer —
    which rides `_Snapshot.bearer_by_url` and is happily immutable — the codex bundle lives here
    and is resolved at forward time. One holder per serve state, created unconditionally (no None
    state to branch on): an identity that serves no codex engine simply never primes it, and every
    due-check no-ops on the unprimed holder.

    Thread model mirrors `_ServeState`: `_lock` is a leaf guarding the bundle swap; `_refresh_lock`
    serializes refreshers within this process (N poll workers + the heartbeat can 401 together)
    so they collapse to one store visit; the cross-PROCESS serialization is the store's file lock,
    inside `api_keys.rotate_codex_bundle`. Lock order is `_refresh_lock → file_lock`, `_lock`
    strictly leaf — acyclic.
    """

    def __init__(self, *, stop: threading.Event) -> None:
        self._stop = stop
        self._lock = threading.Lock()  # guards bundle/expires_at/not_before (leaf — no calls out)
        self._refresh_lock = threading.Lock()  # serializes refreshers WITHOUT blocking bundle() readers
        # Set for exactly the CAS's adopt-check→journal→exchange→persist window (the CAS itself
        # publishes/clears it) — the shutdown drain waits on this, never on a thread.
        self._exchange = threading.Event()
        self._bundle: codex_oauth.CodexBundle | None = None
        self._expires_at: int | None = None  # decoded ONCE per rotation, never per job
        self._not_before = 0.0  # monotonic gate after a failed rotation (no hammering a dead seat)

    def prime_from_store(self) -> None:
        """Load the stored seat, or die naming the fix (the die-before-advertise startup gate —
        better than advertising models whose every job would 401 upstream)."""
        self._adopt(api_keys.require_codex_bundle())

    def bundle(self) -> codex_oauth.CodexBundle:
        """The live bundle for one forward attempt — bind once per attempt, like a snapshot.

        Unprimed (a reload raced us, or wiring missed a path) it self-heals from the store rather
        than erroring a servable job; only a store with no seat at all refuses, typed, for the
        forward path to turn into a job error.
        """
        with self._lock:
            if self._bundle is not None:
                return self._bundle
        stored = api_keys.load_codex_bundle()
        if stored is None:
            raise api_keys.CodexNotSignedIn("this machine is not signed in to a codex subscription")
        self._adopt(stored)
        return stored

    def exchange_in_flight(self) -> bool:
        """Whether a rotation is inside its journal→exchange→persist window (the drain's signal)."""
        return self._exchange.is_set()

    def refresh(self, stale_access_token: str) -> bool:
        """Rotate the seat past ``stale_access_token`` — the ONE entry for the reactive 401 path
        and the proactive heartbeat tick. True means ``bundle()`` now yields a token that advanced
        past the stale one (own exchange, a sibling thread's, or another process's, adopted).

        Layered like ``_ServeState.refresh``, cheapest first: an in-memory compare collapses N
        401ing workers to one store visit; the failure cooldown stops a dead seat being hammered
        every tick+job (with a lock-free store peek first, so a re-sign-in from ANOTHER process
        heals instantly instead of waiting the gate out); the stop check never STARTS spending
        mid-shutdown; and only then the cross-process CAS, which may run the vendor exchange.
        Failures warn — with the journal-aware diagnosis for a refused seat — and gate; they never
        raise, so a refresh can never kill a poll worker.
        """
        with self._refresh_lock:
            with self._lock:
                live, not_before = self._bundle, self._not_before
            if live is not None and live.access_token != stale_access_token:
                return True  # a sibling thread already rotated; the live bundle is good
            if self._stop.is_set():
                return False  # shutting down — nothing new may be spent (D8's drain invariant)
            if time.monotonic() < not_before:
                stored = api_keys.load_codex_bundle()  # lock-free peek: reads never take the lock
                if stored is not None and stored.access_token != stale_access_token:
                    self._adopt(stored)  # another process rotated or re-signed-in — free heal
                    return True
                return False
            try:
                fresh = api_keys.rotate_codex_bundle(
                    stale_access_token, exchange_in_flight=self._exchange, abandon=self._stop,
                )
            except api_keys.RotationAbandoned:
                return False  # shutdown won the race to the lock; the drain reports what matters
            except api_keys.CodexNotSignedIn:
                _warn("codex token refresh failed: this machine is no longer signed in to a codex "
                      "subscription — re-run `grid join --api codex`. Jobs will keep erroring; "
                      "the engine stays registered.")
                return False
            except api_keys.CodexRotationRefused as exc:
                if exc.interrupted:
                    # AC 6: the journal left by a killed exchange turns "the vendor said no" into
                    # the real diagnosis — the rotation was lost, not merely rejected.
                    _warn(f"a previous codex token rotation was interrupted before it could be "
                          f"saved, and the vendor now refuses the stored refresh token "
                          f"({exc}) — the rotation was lost. Jobs will keep erroring; re-run "
                          f"`grid join --api codex` to sign in again (the engine stays registered).")
                else:
                    _warn(f"the vendor refused the codex seat's refresh token ({exc}) — revoked, "
                          f"or signed out elsewhere? Jobs will keep erroring; re-run "
                          f"`grid join --api codex` to sign in again (the engine stays registered).")
                with self._lock:
                    self._not_before = time.monotonic() + _CODEX_REFUSED_COOLDOWN
                return False
            except codex_oauth.RefreshUnavailable as exc:
                _warn(f"codex token refresh could not be concluded ({exc}); will retry.")
                with self._lock:
                    self._not_before = time.monotonic() + _CODEX_UNAVAILABLE_COOLDOWN
                return False
            except (Exception, SystemExit) as exc:
                # The "never raise" contract, made mechanical (python + silent-failure reviews):
                # the store peek and the CAS both read api_keys.toml, whose loader raises
                # SystemExit for a corrupt file — which skips every `except Exception` between a
                # poll worker and `_supervise`, turning one kind's store hiccup into a WHOLE
                # engine stop with no terminal signal to the consumer. Unlike credentials.toml
                # (fatal by documented design — the engine cannot outlive its relay tokens), this
                # store only feeds the codex forward, so: warn, gate like a transient, fail the
                # one job. The same hazard on the proactive path is guarded in
                # `_maybe_refresh_codex`.
                _warn(f"codex token refresh failed unexpectedly ({exc!r}); will retry.")
                with self._lock:
                    self._not_before = time.monotonic() + _CODEX_UNAVAILABLE_COOLDOWN
                return False
            self._adopt(fresh)
            return True

    def maybe_refresh(self, now: int) -> None:
        """The proactive trigger (heartbeat tick — D-d): rotate when the token's own expiry is
        inside `_CODEX_EXPIRY_MARGIN` (including already past), or when the last rotation is older
        than `_CODEX_ROTATION_WINDOW` — so an idle grid still rotates. An UNPRIMED holder never
        fires: this identity serves no codex engine, and the seat in the store may belong to
        another grid on this box. A `last_refresh` of 0 (a legacy bundle that never recorded one)
        is beyond any window → one immediate rotation establishes a real baseline. Failure
        handling, gating, and cross-process adoption all live in `refresh`."""
        with self._lock:
            bundle, expires_at = self._bundle, self._expires_at
        if bundle is None:
            return
        if (expires_at is not None and expires_at - now <= _CODEX_EXPIRY_MARGIN) or (
            now - bundle.last_refresh >= _CODEX_ROTATION_WINDOW
        ):
            self.refresh(bundle.access_token)

    def _adopt(self, bundle: codex_oauth.CodexBundle) -> None:
        """Swap in a bundle + its decoded expiry, and clear the failure gate — an adopted rotation
        is fresh evidence the seat works. `CodexTokenError` → no expiry (the rotation window rules
        the proactive refresh instead); never raises past that, so adopting can't kill a worker."""
        try:
            expires_at = codex_auth.decode_seat(bundle.access_token).expires_at
        except codex_auth.CodexTokenError:
            expires_at = None
        with self._lock:
            self._bundle = bundle
            self._expires_at = expires_at
            self._not_before = 0.0


class _ServeState:
    def __init__(
        self,
        *,
        signaling_url: str,
        node_id: str,
        network_id: str,
        llm_url: str,
        access_token: str,
        refresh_token: str | None,
        models: list[str],
        capabilities: dict[str, Any],
        meta: dict[str, Any],
        pricing: dict[str, float],
        max_concurrency: int,
        routes: dict[str, str] | None = None,
        upstream: dict[str, str] | None = None,
        media_url: str | None = None,
        bearer_by_url: dict[str, str] | None = None,
        api_kind_by_url: dict[str, str] | None = None,
        handlers: dict[str, Any] | None = None,
    ) -> None:
        self.signaling_url = signaling_url
        self.node_id = node_id
        self.network_id = network_id
        self.llm_url = llm_url.rstrip("/")
        # `bearer_by_url` and `api_kind_by_url` are resolved from the key store / record and live on the
        # reload-swappable snapshot built below (NOT as fixed attributes — see the `bearer_by_url`
        # property), so a SIGHUP hot-reload re-reads the key store and swaps them atomically WITH routing
        # (issue 05). A rotated key still respawns, by CLI policy, not because the mechanism can't swap it.
        # This box's media server base (`http://127.0.0.1:<media_port>`) when the identity serves
        # media, else None. `media/*` jobs forward here instead of an LLM engine; all media models
        # share the one server, so a single URL (not a per-model route) is enough.
        self.media_url = media_url.rstrip("/") if media_url else None
        # Vendor handlers for API engines (e.g. Doggi) that serve media endpoints directly without
        # ComfyUI. Built at startup from the record's api_kind entries; not reload-swappable because
        # a handler's config (base_url, api_key) can't change without a respawn.
        self.handlers: dict[str, Any] = handlers or {}
        # The reload-swappable routing (routes/upstream/models/caps/meta/pricing/concurrency) lives in
        # one immutable snapshot so a hot-reload can swap it atomically (ADR 0010 D4). Several engines
        # may serve under one identity (DECISIONS D9); for the single-engine case the route map is
        # derived so every advertised model points at the one engine.
        route_map = routes if routes is not None else {model: self.llm_url for model in models}
        self._snapshot = _Snapshot.build(
            routes=route_map, upstream=upstream or {}, models=models, capabilities=capabilities,
            meta=meta, pricing=pricing, max_concurrency=max_concurrency,
            api_kind_by_url=api_kind_by_url, bearer_by_url=bearer_by_url,
        )
        # The probe results the live snapshot was built from, kept so a reload probes only newly-added
        # engines (ADR 0010 D4 F6). Reload-owned: set at startup, thereafter only the reload loop writes.
        self._engine_results: _EngineResults = []
        # This box's media (comfyui:*) model names + a fingerprint of the media config the process
        # brought up. A hot-reload can't launch/teardown media or swap bundles, so a reload whose re-read
        # record differs here is refused — the CLI respawns instead (ADR 0010 D4 F6 / C3). Set at startup.
        self.media_models: list[str] = []
        self.media_signature: tuple[bool, tuple[str, ...], int, int] = _media_signature({})
        self._reload_register_fails = 0  # consecutive post-swap re-register failures (bounded retry, C5)
        self.stop = threading.Event()
        self._lock = threading.Lock()  # guards the snapshot swap + token + inflight (short sections)
        self._register_lock = threading.Lock()  # serializes reload-register vs heartbeat-404 re-register
        self.reload_requested = threading.Event()  # SIGHUP sets this; the reload loop waits on it
        self._refresh_lock = threading.Lock()  # serializes refreshes WITHOUT blocking token() readers
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._inflight = 0
        # The codex seat's credential holder — OUTSIDE the snapshot (ADR 0015 D-d): a token
        # rotation must not rebuild routing. Unconditional; primed only when the record has a
        # codex spec (startup + reload), unprimed otherwise and inert.
        self.codex_seat = _CodexSeatHolder(stop=self.stop)

    @property
    def models(self) -> list[str]:
        return self._snapshot.models

    @property
    def capabilities(self) -> dict[str, Any]:
        return self._snapshot.capabilities

    @property
    def meta(self) -> dict[str, Any]:
        return self._snapshot.meta

    @property
    def pricing(self) -> dict[str, float]:
        return self._snapshot.pricing

    @property
    def max_concurrency(self) -> int:
        return self._snapshot.max_concurrency

    @property
    def bearer_by_url(self) -> dict[str, str]:
        """The vendor bearers for the live snapshot — a hot-reload swaps these WITH routing (issue 05),
        so a forward binding one snapshot never pairs a route with another union's bearer (D4 F4)."""
        return self._snapshot.bearer_by_url

    def snapshot(self) -> _Snapshot:
        """The current routing snapshot — one atomic reference load (no lock). Bind it ONCE per
        operation, then read fields off the result, so a concurrent reload swap is never seen
        half-applied (ADR 0010 D4 F4)."""
        return self._snapshot

    def apply(self, snapshot: _Snapshot, engine_results: _EngineResults) -> None:
        """Swap in a freshly-built routing snapshot and the probe results it was built from. Rebinds
        under ``_lock`` then RELEASES before the caller re-registers, so the ``_register_lock → _lock``
        order stays acyclic — never register while holding ``_lock`` (ADR 0010 D4 F5)."""
        with self._lock:
            self._snapshot = snapshot
            self._engine_results = engine_results

    def engine_results(self) -> _EngineResults:
        """The probe results the live snapshot was built from — a reload reuses them so it re-probes
        only newly-added engines (ADR 0010 D4 F6)."""
        return self._engine_results

    def route_and_kind(self, model: str | None, snap: _Snapshot | None = None) -> tuple[str | None, str | None]:
        """The local engine URL serving ``model``, plus its API service kind (None = hardware/media).

        Exact match wins. Otherwise, when every model points at the **same single engine** (one
        distinct URL — even if that one engine serves several models), fall back to it: a job with a
        missing/unknown ``model`` still forwards as it did before multi-engine (the proxy forwarded the
        body unchanged, letting the engine answer). With several distinct engines and no match, return
        ``None`` so the caller reports "no engine serves" instead of guessing.

        One method for both lookups so they read the SAME snapshot: a separate kind read could bind
        a torn pair across a concurrent reload swap (ADR 0010 D4 F4 — bind once).
        """
        snap = snap if snap is not None else self._snapshot  # bind once — a concurrent reload swap is never seen half-applied
        target = None
        if model and model in snap.routes:
            target = snap.routes[model]
        else:
            distinct = set(snap.routes.values())
            if len(distinct) == 1:
                target = next(iter(distinct))
        if target is None:
            return None, None
        return target, snap.api_kind_by_url.get(target)  # both maps are URL-normalized by `build`

    def route(self, model: str | None) -> str | None:
        """The local engine URL serving ``model`` (see ``route_and_kind``)."""
        return self.route_and_kind(model)[0]

    def upstream_model(self, model: str | None, snap: _Snapshot | None = None) -> str | None:
        """The name the local engine answers to for an advertised ``model`` (``--advertise-as`` maps the
        consumer-facing alias back to the engine's real model name). ``None`` when unmapped — the caller
        then forwards the body's model unchanged (single-engine fallback / built-in, where they match).
        ``snap`` lets one job bind a single union for route + upstream + bearer (D4 F4 — bind once).
        """
        snap = snap if snap is not None else self._snapshot
        if model and model in snap.upstream:
            return snap.upstream[model]
        return None

    def token(self) -> str:
        with self._lock:
            return self._access_token

    def load(self) -> dict[str, Any]:
        with self._lock:
            load = {"active_tasks": self._inflight}
        # VRAM/GPU load for the grid page (per-provider VRAM roll-up). Probed OUTSIDE the lock — it
        # shells out to nvidia-smi / system_profiler (up to a few seconds); absent a GPU it returns {}.
        from shared.system import gpu, host

        load.update(gpu.load_snapshot())
        # OS/arch so the grid knows what a node runs: linux / macos-arm64 / macos-x86_64 / windows / other.
        load["platform"] = host.platform_kind()
        return load

    def enter_inference(self) -> None:
        with self._lock:
            self._inflight += 1

    def exit_inference(self) -> None:
        with self._lock:
            self._inflight = max(0, self._inflight - 1)

    def refresh(self, stale_token: str | None = None) -> bool:
        """Get a fresh access token after a 401: adopt one another worker already stored, else
        exchange the refresh token and persist it. Returns whether the token advanced.

        ``_refresh_lock`` serializes concurrent refreshes (the poll + heartbeat threads both 401 at
        token expiry) so only one network exchange runs; the loser adopts the winner's token. The
        network call happens with ``_lock`` released, so ``token()``/``load()`` readers never block
        on it. ``stale_token`` is the token that just failed: if the live token already advanced past
        it, another worker refreshed first and we adopt with no work.
        """
        with self._refresh_lock:
            with self._lock:
                if stale_token is not None and self._access_token != stale_token:
                    return True  # a concurrent worker already refreshed; the live token is good
                for net in credentials.load_credentials().get("networks") or []:
                    if net.get("network_id") == self.network_id:
                        stored = net.get("access_token")
                        if stored and stored != self._access_token:  # another *process* refreshed
                            self._access_token = stored
                            self._refresh_token = net.get("refresh_token") or self._refresh_token
                            return True
                        break
                if not self._refresh_token:
                    print("Token expired and no refresh token is available — re-run `grid login`.", file=sys.stderr)
                    return False
                refresh_token = self._refresh_token

            try:  # network call with _lock released (readers unblocked); _refresh_lock serializes us
                bundle = control_plane.refresh_network_token(
                    network_id=self.network_id, refresh_token=refresh_token
                )
            except SystemExit as exc:  # control_plane signals HTTP errors as SystemExit; don't die mid-loop
                print(f"Token refresh failed ({exc}).", file=sys.stderr)
                return False
            new_access = bundle.get("access_token")
            if not new_access:
                print("Token refresh returned no access token.", file=sys.stderr)
                return False
            new_refresh = bundle.get("refresh_token") or refresh_token
            credentials.update_network_tokens(
                self.network_id, access_token=new_access, refresh_token=new_refresh
            )
            with self._lock:
                self._access_token = new_access
                self._refresh_token = new_refresh
            return True


# ---------------------------------------------------------------------------
# Loop units (each independently testable against a mocked relay/engine)
# ---------------------------------------------------------------------------

def register_once(state: _ServeState, *, _allow_refresh: bool = True) -> None:
    """Advertise the identity's current snapshot to the relay (``PUT /nodes/{node_id}``).

    Binds the snapshot + token INSIDE ``_register_lock`` so whichever racing register actually PUTs
    sends the freshest union — the reload's register and the heartbeat's 404 re-register can't interleave
    two PUTs, and a slow racer can't land a stale snapshot last (ADR 0010 D4 F4/F5). On a 401 it refreshes
    and retries once — like ``poll_once``/``heartbeat_once`` — so a reload landing at token expiry still
    re-advertises instead of silently leaving the old union live.
    """
    token = None
    try:
        with state._register_lock:
            token = state.token()      # bound inside the lock: whoever PUTs sends the current token and
            snap = state.snapshot()    # the current snapshot, so a descheduled racer can't PUT a stale one
            relay.register_node(
                state.signaling_url,
                token,
                state.node_id,
                models=snap.models,
                capabilities=snap.capabilities or None,
                meta=snap.meta or None,
                pricing=snap.pricing or None,
                max_concurrency=snap.max_concurrency,
            )
    except relay.RelayUnauthorized:
        if _allow_refresh and token is not None and state.refresh(stale_token=token):
            return register_once(state, _allow_refresh=False)
        raise


def poll_once(state: _ServeState, *, _allow_refresh: bool = True) -> dict[str, Any] | None:
    """One relay poll; on 401 refresh the token and retry exactly once."""
    token = state.token()
    try:
        return relay.poll(state.signaling_url, token)
    except relay.RelayUnauthorized:
        if _allow_refresh and state.refresh(stale_token=token):
            return poll_once(state, _allow_refresh=False)
        raise


def heartbeat_once(state: _ServeState, *, _allow_refresh: bool = True) -> str:
    """One heartbeat; 404 → re-register (node pruned), 401 → refresh + retry once."""
    token = state.token()
    try:
        result = relay.heartbeat(state.signaling_url, token, load=state.load())
    except relay.RelayUnauthorized:
        if _allow_refresh and state.refresh(stale_token=token):
            return heartbeat_once(state, _allow_refresh=False)
        raise
    if result == "missing":
        register_once(state)
    return result


def _set_last_reload_error(state: _ServeState, engine_id: str, message: str | None) -> None:
    """Persist (``str``) or clear (``None``) a reload failure on the run record so the NEXT CLI command
    can surface it: the CLI prints success as soon as the SIGHUP is delivered, so a failure inside this
    process would otherwise be visible only in this log. Locked read-modify-write — the CLI merges
    joins under the same lock, so an unlocked write here could lose a concurrent join's union (ADR
    0010 F3). Best-effort bookkeeping that never raises: a raise inside ``_reload_loop``'s except
    would kill the watcher, and one after a successful swap would mislabel the reload as failed."""
    try:
        with file_lock(run_records.record_path(state.network_id, engine_id)):
            record = run_records.read_record(state.network_id, engine_id)
            if not record or (message is None and "last_reload_error" not in record):
                return
            updated = {k: v for k, v in record.items() if k != "last_reload_error"}
            if message is not None:
                updated["last_reload_error"] = message[:300]  # a trace, not a transcript (never the key)
            run_records.write_record(state.network_id, engine_id, updated)
    except (Exception, SystemExit) as exc:
        _warn(f"could not update last_reload_error on the run record (ignoring): {exc!r}")


def _reload_once(state: _ServeState, engine_id: str) -> None:
    """Rebuild routing from the (re-read) record and re-advertise the union in place — the body of the
    SIGHUP hot-reload (ADR 0010 D3). External-only and probe-only: reuse retained caps for engines
    already serving, probe only newly-added ``--at`` endpoints, and build the whole new snapshot before
    one atomic swap — so in-flight requests keep flowing on the old snapshot until the swap (D4 F6).
    Anything needing a launch (a built-in ``--serve``) or a media bring-up/bundle change is refused
    here; the CLI respawns those instead, so the reload thread never blocks on a heavy start.
    """
    record = run_records.read_record(state.network_id, engine_id)
    if not record:  # a concurrent full `grid leave` removed the record — SIGTERM will tear us down
        _debug("reload: record gone; keeping current routing")
        return
    specs = record.get("engines") or (
        [_flat_spec(record)] if (record.get("endpoint_url") or record.get("models")) else []
    )
    aliases = list(record.get("advertise_as") or [])
    # These refusals mean the CLI signalled something it should have respawned (or a manual SIGHUP) —
    # surface them, don't hide behind GRID_ENGINE_DEBUG (the CLI already reported the join/leave).
    if any(not spec.get("endpoint_url") for spec in specs):
        _warn("reload: record needs a built-in launch; refusing (respawn required)")
        return
    if len(specs) > 1 and aliases:
        _warn("reload: multi-engine identity with --advertise-as; refusing (respawn required)")
        return
    if _media_signature(record) != state.media_signature:
        _warn("reload: media config changed; refusing (respawn required)")
        return

    retained = {r[0]: r for r in state.engine_results()}  # keyed by the normalized llm_url
    reassembled: _EngineResults = []
    for spec in specs:
        url = (spec.get("endpoint_url") or "").rstrip("/")
        models = list(spec.get("models") or [])
        api_kind = spec.get("api_kind")
        # An api spec's advertised names ARE its record models; its upstream names are the vendor
        # names they embed — on BOTH branches below, or a reload would forward `openai:*` verbatim.
        advertised = list(models) if api_kind else _advertised_models(models, aliases)
        upstream = [_api_upstream_name(api_kind, m) for m in models] if api_kind else list(models)
        prev = retained.get(url)
        if prev is not None and prev[1][:1] == advertised[:1]:
            # Already serving this engine: reuse ONLY its probed caps. advertised/upstream come from the
            # record, so a model appended to this engine is still picked up, not dropped (ADR 0010 C2).
            reassembled.append((url, advertised, upstream, prev[3]))
        else:  # a genuinely new engine (or a changed first model) → probe EVERY model it serves, so a
            # newly-appended multi-model `--at` advertises caps for all of them, exactly like startup
            # (`_probe_spec_caps` is the shared site, so startup and reload can't drift — ADR 0009 C2).
            caps = (
                _probe_spec_caps(url, advertised, upstream, record.get("ctx_size"), api_kind=api_kind,
                                 plan_type=spec.get("plan_type"))
                if models else {}
            )
            reassembled.append((url, advertised, upstream, caps))

    snapshot = _assemble_snapshot(reassembled, state.media_models, record, engine_id, state.max_concurrency)
    # Prime the codex seat BEFORE the swap (one derivation with startup — `_prime_codex_seat`): a
    # hot-appended codex engine must never be routable while the holder has no seat, and a box
    # with no stored seat refuses the whole reload here (the raise lands in `_reload_loop`'s
    # catch → warn + last_reload_error), old routing intact. Re-reading the store also adopts a
    # rotation another process performed while we served.
    _prime_codex_seat(state, record)
    state.apply(snapshot, reassembled)  # atomic swap; in-flight requests were unaffected until here
    if record.get("last_reload_error"):
        _set_last_reload_error(state, engine_id, None)  # the union applied — a previous failure healed
    if state.stop.is_set():
        # A concurrent teardown (grid leave / SIGTERM) already set stop and the outer `finally` will
        # `unregister_node`; re-advertising now would resurrect a node that is exiting (a zombie the relay
        # only evicts on its heartbeat-TTL prune). The drain in `_serve_loop` also joins this thread, so a
        # register already in flight completes before that unregister — this just avoids starting a new one.
        _warn("reload: engine is stopping; skipping re-register")
        return
    try:
        register_once(state)  # swap THEN register — a new model is routable before the relay sends it
        state._reload_register_fails = 0  # a clean re-advertise resets the retry budget
    except relay.RelayUnauthorized:
        # Auth is exhausted (refresh failed too). The heartbeat loop stops the process on the same
        # condition, so don't spin re-registering — surface it (the new union stays unadvertised until re-auth).
        _warn("reload: re-register rejected the token and refresh is unavailable — new union not advertised")
    except Exception as exc:
        # Post-swap transient failure: the new snapshot serves locally but the relay still has the old
        # union (a healthy node is never heartbeat-404'd). Re-arm so a later tick retries — but BOUNDED, so
        # a permanent failure doesn't PUT every 2s forever; give up loudly and let the next join re-trigger
        # (ADR 0010 C5). Reuse means the retry never re-probes.
        state._reload_register_fails += 1
        if state._reload_register_fails <= _MAX_RELOAD_REGISTER_RETRIES:
            _warn(f"reload: re-register failed post-swap ({exc!r}); retry "
                  f"{state._reload_register_fails}/{_MAX_RELOAD_REGISTER_RETRIES}")
            state.stop.wait(2)
            state.reload_requested.set()
        else:
            _warn(f"reload: re-register still failing after {_MAX_RELOAD_REGISTER_RETRIES} tries "
                  f"({exc!r}); giving up until the next join/leave")
            state._reload_register_fails = 0


def _served_endpoints(api_kind: str | None) -> tuple[str, ...] | frozenset[str]:
    """The relay endpoints the routed engine's KIND serves — ADR 0015 D-b's matrix, one row per
    kind: hardware (``None``) keeps the legacy chat pair, an API kind serves exactly its whitelist
    row's endpoints (openai ⇒ chat/completions, codex ⇒ responses). A kind no longer in the
    catalog (edited between join and respawn) degrades to the ``ApiWhitelist`` default — chat-only,
    never the hardware pair — the same posture as ``_static_api_caps``."""
    if not api_kind:
        return _ALLOWED_ENDPOINTS
    whitelist = api_catalog.WHITELISTS.get(api_kind)
    return whitelist.endpoints if whitelist else ("chat/completions",)


def handle_job(state: _ServeState, job: dict[str, Any]) -> None:
    """Forward one claimed job to the local engine and submit its result back to the relay.

    A malformed or failing job must never kill the loop: bad input is dropped with a log line, a
    forward error is reported to the relay (best-effort), and reporting failures are swallowed.
    """
    txn = job.get("transaction_id")
    if not txn:
        print(f"\nDiscarding a relay job with no transaction_id: {job!r}", file=sys.stderr)
        return
    endpoint = job.get("endpoint_path") or ""
    body = job.get("body") or {}
    is_stream = bool(job.get("is_stream", False))
    read_timeout = float(job.get("inference_timeout_seconds") or _DEFAULT_INFERENCE_TIMEOUT)

    if endpoint in _MEDIA_ENDPOINTS:
        # Try a media API handler first (e.g. Doggi). These models are routed by
        # the standard model→URL map, so route_and_kind works the same as for text.
        snap = state.snapshot()
        model = body.get("model")
        target, api_kind = state.route_and_kind(model, snap)
        handler = state.handlers.get(api_kind) if api_kind else None
        if handler is not None:
            state.enter_inference()
            try:
                # ONE submit per transaction, like every other forward path (`_forward_stream` /
                # `_forward_whole`): the relay's mailbox for a txn is written once, so submitting
                # per SSE line would drop everything after the first. The events are drained here
                # rather than handed over as a lazy iterator so a mid-generation failure (gateway
                # error, no result files) is still reportable via `_try_submit_error` — nothing has
                # been POSTed yet. Media results are fully buffered anyway (base64 in one event),
                # so nothing is lost by materialising them.
                sse_bytes = b"".join(
                    line.encode() if isinstance(line, str) else line
                    for line in handler.forward(body, endpoint)
                )
                _submit_response(state, txn, content=sse_bytes, stream=True)
            except Exception as exc:  # one bad media job must not kill the loop
                print(f"\nMedia API job {txn} failed: {exc!r}", file=sys.stderr)
                _try_submit_error(state, txn, str(exc))
            finally:
                state.exit_inference()
            return
        # No API handler — fall through to the local ComfyUI media server.
        if not state.media_url:
            _try_submit_error(state, txn, f"this engine does not serve media (endpoint {endpoint!r})")
            return
        # Refuse a media model this engine does not serve, BEFORE forwarding. The engine-side
        # handler dispatches on the route alone (`shared/media/media_handler.handle_request`), so a
        # model naming a different task — or one whose bundle this host's VRAM gated out — would
        # otherwise be silently served as whatever the route means. Mirrors the per-kind endpoint
        # matrix below: routing decides WHERE, this decides WHETHER we serve it at all.
        if model is not None and not isinstance(model, str):
            _try_submit_error(state, txn, f"model must be a string, got {type(model).__name__}")
            return
        if model and model not in state.media_models:
            _try_submit_error(
                state, txn,
                f"this engine does not serve media model {model!r} "
                f"(serving: {', '.join(sorted(state.media_models)) or 'none'})",
            )
            return
        expected = media_gating.endpoint_model(endpoint)
        if model and expected and model != expected:
            _try_submit_error(
                state, txn,
                f"media model {model!r} does not serve endpoint {endpoint!r} (that is {expected!r})",
            )
            return
        state.enter_inference()
        try:
            _forward_stream(state, txn, endpoint, body, read_timeout, state.media_url,
                            headers=_forward_headers(state, state.media_url))
        except Exception as exc:  # one bad media job must not kill the loop
            print(f"\nMedia job {txn} failed: {exc!r}", file=sys.stderr)
            _try_submit_error(state, txn, str(exc))
        finally:
            state.exit_inference()
        return
    if endpoint.startswith("media/"):  # a media path we don't serve — never blind-forward it
        _try_submit_error(state, txn, f"unsupported media endpoint: {endpoint!r}")
        return
    # Bind ONE routing snapshot for this whole job: route + kind + upstream + bearer all come from the
    # same union, so a concurrent leave/append hot-reload swap is all-or-nothing per job — never a route
    # from one union paired with a bearer from another (ADR 0010 D4 F4 — bind once; issue 05).
    snap = state.snapshot()
    model = body.get("model")  # body is already `job.get("body") or {}`, so it is a dict
    target, api_kind = state.route_and_kind(model, snap)  # which local engine serves this model (DECISIONS D9)
    if target is None:
        _try_submit_error(state, txn, f"no engine serves model {model!r}")
        return
    # Per-kind endpoint matrix (ADR 0015 D-b): which endpoint an engine serves is a property of its
    # KIND, so the gate runs AFTER routing, where the kind is known — codex ⇒ responses only,
    # openai ⇒ chat/completions only, hardware ⇒ the chat pair. A mismatch — including a job that
    # arrived via the single-URL fallback above — is refused with a structured error, never
    # translated and never blind-forwarded. The global allow-list is deliberately NOT widened:
    # `responses` never enters `_ALLOWED_ENDPOINTS`, so the anti-traversal property is unchanged —
    # only a literal that matched this matrix ever reaches `f"{target_url}/{endpoint}"`.
    served = _served_endpoints(api_kind)
    if endpoint not in served:
        if api_kind:
            _try_submit_error(
                state, txn,
                f"API engine {api_kind!r} serves {', '.join(served)} only (endpoint {endpoint!r} not served)",
            )
        else:  # don't forward an unknown path to the local engine (the pre-matrix behavior, kept verbatim)
            _try_submit_error(state, txn, f"unsupported endpoint: {endpoint!r}")
        return
    # Params the vendor is known to reject (e.g. GPT-5.x and `stop`): refuse now, with the vendor's
    # own error shape, instead of forwarding to learn a static catalog fact. A CHAT-dialect gate —
    # the fabricated refusal wears the chat `{"error": ...}` envelope — so it runs only on the chat
    # forward: provably inert for a responses job, whose contract violations are the vendor's to
    # answer in its own `{"detail": ...}` shape (facts.md #7; the codex whitelist row's
    # `unsupported_params` is thereby advisory catalog data, not an executable gate).
    if api_kind and endpoint == "chat/completions":
        unsupported = _api_unsupported_params(api_kind, body)
        if unsupported:
            _refuse_unsupported_api_params(state, txn, api_kind, unsupported)
            return

    # Consumers address the model by its advertised name; an external engine behind ``--advertise-as``
    # only knows its real name, so rewrite the body's model before forwarding (a new dict — never
    # mutate the job). No mapping / already-equal → forward unchanged (built-in + single-engine paths).
    upstream_model = state.upstream_model(model, snap)
    forward_body = {**body, "model": upstream_model} if upstream_model and upstream_model != model else body
    # ... and an API vendor may spell the output-token cap differently from the grid's internal name.
    forward_body = _adapt_output_token_param(forward_body, api_kind)

    state.enter_inference()
    try:
        if api_kind == api_catalog.CODEX_KIND:
            # The seat speaks SSE only, so the job's stream flag is ignored — always the
            # streaming forward, submitting whole event blocks (ADR 0015 D-e); headers come from
            # the live seat holder, never the snapshot (D-d).
            _forward_codex(state, txn, endpoint, forward_body, read_timeout, target)
        elif is_stream:
            _forward_stream(state, txn, endpoint, forward_body, read_timeout, target,
                            headers=_forward_headers(state, target, snap), api_kind=api_kind)
        else:
            _forward_whole(state, txn, endpoint, forward_body, read_timeout, target,
                           headers=_forward_headers(state, target, snap), api_kind=api_kind)
    except Exception as exc:  # one bad job must not kill the loop
        print(f"\nJob {txn} failed: {exc!r}", file=sys.stderr)
        _try_submit_error(state, txn, str(exc))
    finally:
        state.exit_inference()


def _try_submit_error(state: _ServeState, txn: str, message: str) -> None:
    """Report a job failure to the relay, best-effort. Refresh the token once on a 401 — otherwise a
    job whose token expired mid-run gets NO terminal signal and the consumer hangs. A still-failed
    report is logged, never raised (one bad job must not kill the loop)."""
    for attempt in (1, 2):
        token = state.token()
        try:
            relay.submit_error(state.signaling_url, token, txn, message=message)
            return
        except relay.RelayUnauthorized:
            if attempt == 2 or not state.refresh(stale_token=token):
                print(f"\nCouldn't report job {txn} failure: relay rejected the token.", file=sys.stderr)
                return
            # refreshed — loop retries once with the new token
        except relay.RelayError as exc:
            print(f"\nCouldn't report job {txn} failure to the relay: {exc}", file=sys.stderr)
            return


def _submit_response(state: _ServeState, txn: str, *, content: Any, stream: bool) -> None:
    """Post a result to the relay, refreshing the token once on a 401 (mirrors poll_once/heartbeat_once
    — without this, a completed job whose token expired mid-run is silently discarded). A streamed body
    is a single-use iterator that can't be replayed, so a 401 there re-raises; `handle_job` then reports
    it via `_try_submit_error` (which also refreshes), so the consumer gets a terminal signal."""
    token = state.token()
    try:
        relay.submit_response(state.signaling_url, token, txn, content=content, stream=stream)
    except relay.RelayUnauthorized:
        if stream or not state.refresh(stale_token=token):
            raise
        relay.submit_response(state.signaling_url, state.token(), txn, content=content, stream=stream)


# Upstream statuses that point at the provider's key or quota (401/403 auth, 429 rate/quota) —
# these earn a stderr warn on top of the per-job error; a 5xx says nothing about the key.
_API_AUTH_QUOTA_STATUSES = frozenset({401, 403, 429})


def _warn_api_auth_failure(api_kind: str | None, status: int) -> None:
    """Warn the engine's stderr log when an API engine's upstream rejects for auth/quota reasons.

    The per-job error reaches only the consumer; without this line the operator whose key was
    revoked or quota exhausted has no signal in the engine log. Never includes the key. The loop
    stays alive and the engine stays registered — each job errors, nothing auto-ejects (ADR 0012).
    """
    if api_kind and status in _API_AUTH_QUOTA_STATUSES:
        _warn(f"{api_kind} upstream returned {status} — check your API key / quota "
              f"(jobs will keep erroring until it is fixed; the engine stays registered)")


def _forward_headers(state: _ServeState, target_url: str, snap: _Snapshot | None = None) -> dict[str, str]:
    """Forward headers for one target: the API key rides ONLY on an API engine's own vendor URL —
    hardware-engine (and media) forwards stay bearer-free, and an upstream 401 is a job error in a
    different auth domain from the relay token (it can never trigger the relay-token refresh). ``snap``
    binds the SAME union the route came from, so a hot-reload swap can't leave an in-flight vendor job
    bearer-less mid-forward (issue 05 / D4 F4)."""
    headers = {"Content-Type": "application/json"}
    bearers = (snap if snap is not None else state.snapshot()).bearer_by_url
    key = bearers.get(target_url.rstrip("/"))
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _codex_headers(bundle: codex_oauth.CodexBundle) -> dict[str, str]:
    """The real Codex client's request header set, built fresh per attempt from the live bundle
    (spike probe.py `headers_for`, verified on the wire 2026-07-15) — bearer, the account-id
    header derived from the token's own claim, the fixed originator/user-agent pair, SSE accept,
    JSON content-type. Deliberately NO `OpenAI-Beta`: this is not the platform API. `account_id`
    is CRLF-safe by the STORE's shape guard (facts.md B5b) — httpx would send an injected header
    verbatim, so that property must hold before a bundle ever reaches here."""
    return {
        "Authorization": f"Bearer {bundle.access_token}",
        "Chatgpt-Account-Id": bundle.account_id,
        "Originator": codex_oauth.ORIGINATOR,
        "User-Agent": codex_oauth.ORIGINATOR,
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
    }


def _warn_codex_upstream(status: int, headers: Any) -> None:
    """The operator's serve-time taxonomy for a codex upstream failure (ADR 0015 D-f): the two
    403s demand OPPOSITE actions, so they must not share wording — Cloudflare-challenge means
    "move the egress IP" (re-signing in cannot fix an IP), auth means "sign in again". Detection
    keys on 403 + `Cf-Mitigated`, NEVER on CF-RAY, which rides every response including 200s
    (facts.md B4). 429 keeps the existing quota warning; 5xx stays silent, as for every kind
    (it says nothing about the seat). The loop stays alive and the engine stays registered —
    jobs error, nothing auto-ejects."""
    if status == 403 and headers.get("cf-mitigated") is not None:
        _warn(
            "codex upstream returned 403 with a Cloudflare challenge — this machine's egress IP "
            "is blocked (datacenter/VPS addresses typically are). Serve the seat from a "
            "residential connection or change the egress IP; signing in again will not help. "
            "Jobs will keep erroring; the engine stays registered."
        )
    elif status in (401, 403):
        _warn(
            f"codex upstream returned {status} — check your seat: re-run `grid join --api codex` "
            "to sign in again. Jobs will keep erroring; the engine stays registered."
        )
    else:
        _warn_api_auth_failure(api_catalog.CODEX_KIND, status)


def _forward_codex(
    state: _ServeState, txn: str, endpoint: str, body: dict[str, Any], read_timeout: float,
    target_url: str,
) -> None:
    """Forward one responses job to the seat and stream the reply back as whole event blocks.

    Always the streaming path, whatever the job's stream flag says — the upstream only speaks SSE
    (ADR 0015 D-e). The bearer is resolved from the seat holder PER ATTEMPT (D-d: outside the
    routing snapshot, so a rotation needs no reload), and an upstream 401 refreshes and retries
    exactly once, codex-scoped — openai keeps ADR 0012's job-error-only. The refresh runs OUTSIDE
    the response context: the non-200 is drained and (status, headers, text) bound first, so no
    vendor connection is held through a ≤15s token exchange, and the warning path sees the
    response headers (CF-403 vs auth-403), not just the status int.
    """
    import httpx

    timeout = httpx.Timeout(connect=10, read=read_timeout, write=None, pool=10)
    for attempt in (1, 2):
        try:
            bundle = state.codex_seat.bundle()  # bind once per attempt, like a snapshot
        except api_keys.CodexNotSignedIn:
            _try_submit_error(
                state, txn,
                "this engine's codex seat is not signed in — re-run `grid join --api codex` to "
                "sign in again",
            )
            return
        except SystemExit as exc:
            # The unprimed holder's self-heal reads api_keys.toml, whose loader raises SystemExit
            # for a corrupt file — that must stay ONE job's error (see the matching guard in
            # `_CodexSeatHolder.refresh`), never sail past handle_job's `except Exception` into a
            # whole-engine stop.
            _warn(f"codex seat store unreadable ({exc}); failing this job only")
            _try_submit_error(
                state, txn,
                "this engine's codex seat store is unreadable — re-run `grid join --api codex` "
                "to re-create it",
            )
            return
        with httpx.Client(timeout=timeout) as client:
            with client.stream(
                "POST", f"{target_url}/{endpoint}", json=body, headers=_codex_headers(bundle),
            ) as resp:
                if resp.status_code == 200:
                    # Whole event blocks per submitted chunk (D-e); a streamed 401 from the RELAY
                    # re-raises out of _submit_response and handle_job's guard reports it — same
                    # terminal-signal guarantees as _forward_stream.
                    _submit_response(
                        state, txn, stream=True,
                        content=_traced_stream(txn, _iter_event_blocks(resp.iter_bytes())),
                    )
                    return
                resp.read()  # drain inside the context so .text is readable after it closes
                status, resp_headers, text = resp.status_code, resp.headers, resp.text
        if status == 401 and attempt == 1 and state.codex_seat.refresh(bundle.access_token):
            continue  # rotated — retry once with the fresh bearer (reactive D-d)
        _warn_codex_upstream(status, resp_headers)
        _try_submit_error(state, txn, f"engine error {status}: {text[:200]}")
        return


def _forward_whole(
    state: _ServeState, txn: str, endpoint: str, body: dict[str, Any], read_timeout: float, target_url: str,
    headers: dict[str, str], api_kind: str | None = None,
) -> None:
    import httpx

    timeout = httpx.Timeout(connect=10, read=read_timeout, write=30, pool=10)
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(f"{target_url}/{endpoint}", json=body, headers=headers)
    if resp.status_code != 200:
        _warn_api_auth_failure(api_kind, resp.status_code)
        _try_submit_error(state, txn, f"engine error {resp.status_code}: {resp.text[:200]}")
        return
    _submit_response(state, txn, content=resp.content, stream=False)


def _forward_stream(
    state: _ServeState, txn: str, endpoint: str, body: dict[str, Any], read_timeout: float, target_url: str,
    headers: dict[str, str], api_kind: str | None = None,
) -> None:
    import httpx

    timeout = httpx.Timeout(connect=10, read=read_timeout, write=None, pool=10)
    with httpx.Client(timeout=timeout) as client:
        with client.stream(
            "POST", f"{target_url}/{endpoint}", json=body, headers=headers,
        ) as engine_resp:
            if engine_resp.status_code != 200:
                engine_resp.read()
                _warn_api_auth_failure(api_kind, engine_resp.status_code)
                _try_submit_error(state, txn, f"engine error {engine_resp.status_code}: {engine_resp.text[:200]}")
                return
            # Pass the engine's SSE bytes straight through while its stream is open. A streamed 401 can't
            # replay the iterator, so `_submit_response` re-raises it; `handle_job` then reports via
            # `_try_submit_error` so the consumer still gets a terminal signal.
            _submit_response(state, txn, content=_traced_stream(txn, engine_resp.iter_bytes()), stream=True)
            _debug(f"stream txn={txn} submit_response returned (relay accepted the full stream) t={time.time():.3f}")


# Defensive bound on one buffered SSE event block: the real stream's largest block is ~1.4 KB of
# 47 (the shared fixture), so 8 MiB is absurd headroom — but a vendor that stopped sending blank
# lines must not buffer unboundedly. Past the cap the grouper degrades to passthrough: block
# ALIGNMENT is lost for that stretch, bytes never are (the relay re-splits on `\n` itself, so
# alignment is a fidelity nicety there, not a parsing requirement).
_MAX_EVENT_BLOCK = 8 * 1024 * 1024


def _iter_event_blocks(chunks: Iterable[bytes]) -> Iterator[bytes]:
    """Regroup vendor SSE bytes into whole event blocks — the streaming unit for a responses job
    (ADR 0015 D-e): one yielded chunk = one `event:`+`data:` block INCLUDING its terminating blank
    line, so each HTTP chunk submitted to the relay is a whole event and a provider death
    mid-stream strands only complete events there, never a torn half-block (the buffered partial
    is deliberately dropped when the source raises — do not "rescue" it in a finally).

    Bytes are passed through verbatim: no strip, no decode, no injected `[DONE]`, and no CR
    repair — the relay refuses bare-CR smuggling (`bare_cr_in_sse_line`), and a provider that
    re-framed CR would mask exactly what that sanitiser exists to catch. Invariant, whatever the
    input chunking: ``b"".join(output) == b"".join(input)``. A final block with no trailing blank
    line is flushed verbatim (the relay flushes its own last block the same way — losing it here
    would eat the `response.completed` that carries the usage).
    """
    buf = b""
    for chunk in chunks:
        buf += chunk
        while (i := buf.find(b"\n\n")) != -1:
            yield buf[: i + 2]
            buf = buf[i + 2:]
        if len(buf) > _MAX_EVENT_BLOCK:
            yield buf  # degrade to passthrough — never buffer unboundedly, never die mid-stream
            buf = b""
    if buf:
        yield buf


def _traced_stream(txn: str, chunks: Iterable[bytes]) -> Iterator[bytes]:
    """Pass engine SSE chunks straight through; when GRID_ENGINE_DEBUG is set, trace chunk progress with a
    wall-clock timestamp so a mid-stream event (e.g. a node re-register) can be correlated with whether
    bytes keep flowing to the relay. No-op overhead beyond a counter when debug is off."""
    n = 0
    for chunk in chunks:
        n += 1
        if _DEBUG and (n == 1 or n % 40 == 0):
            _debug(f"stream txn={txn} chunk#{n} t={time.time():.3f}")
        yield chunk
    _debug(f"stream txn={txn} engine finished after {n} chunks t={time.time():.3f}")


# ---------------------------------------------------------------------------
# Loops
# ---------------------------------------------------------------------------

def _poll_loop(state: _ServeState) -> None:
    while not state.stop.is_set():
        try:
            job = poll_once(state)
        except relay.RelayUnauthorized:
            print("\nRelay rejected the token and refresh is unavailable — stopping.", file=sys.stderr)
            state.stop.set()
            break
        except relay.RelayError as exc:
            print(f"\nPoll error ({exc}); retrying...", file=sys.stderr)
            state.stop.wait(2)
            continue
        if job is None:  # 204 — no work waiting; poll again
            _debug("poll: no job (204), re-polling")
            continue
        started = time.monotonic()
        try:
            handle_job(state, job)
        except Exception as exc:  # defence in depth: handle_job already guards, but never die here
            print(f"\nUnexpected error handling a job: {exc!r}", file=sys.stderr)
        else:
            if _DEBUG:
                txn = job.get("transaction_id")
                model = (job.get("body") or {}).get("model")
                _debug(f"poll: job txn={txn} model={model!r} handled in {time.monotonic() - started:.2f}s")


def _maybe_refresh_codex(state: _ServeState) -> None:
    """The heartbeat's proactive-rotation hook (ADR 0015 D-d). NEVER raises: `_heartbeat_loop`
    runs under `_supervise`, which stops the WHOLE engine on any escaping exception — a refresh
    bug must degrade to a warn, not an engine stop. `SystemExit` included: the store's TOML loader
    raises it for a corrupt file (the house daemon-thread hazard, same as `_reload_loop`)."""
    try:
        state.codex_seat.maybe_refresh(int(time.time()))
    except (Exception, SystemExit) as exc:
        _warn(f"codex proactive refresh failed unexpectedly (engine unaffected): {exc!r}")


def _heartbeat_loop(state: _ServeState) -> None:
    while not state.stop.is_set():
        try:
            result = heartbeat_once(state)
        except relay.RelayUnauthorized:
            # Auth is exhausted (refresh failed too) — stop now rather than spin re-failing until
            # the poll loop happens to notice, which can be up to a full long-poll away.
            print("\nHeartbeat token rejected and refresh is unavailable — stopping.", file=sys.stderr)
            state.stop.set()
            break
        except relay.RelayError as exc:
            print(f"\nHeartbeat error: {exc}", file=sys.stderr)
        else:
            _debug(f"heartbeat: ok ({result})")
        # On EVERY surviving tick — including a failed relay call (the relay being unreachable
        # says nothing about the vendor), so an idle grid behind a flaky relay still rotates.
        _maybe_refresh_codex(state)
        state.stop.wait(relay.HEARTBEAT_INTERVAL)


def _supervise(loop: Callable[[_ServeState], None], state: _ServeState) -> None:
    """Run one serve-loop thread (``_poll_loop``/``_heartbeat_loop``); if it dies from an unexpected
    fault, stop the whole engine loudly instead of letting the thread vanish.

    A dead *job* must not kill the loop (``handle_job`` guards that), but a dead *loop* is different: a
    background worker that silently exits would strand the node advertising capacity it no longer serves
    (and if all die, a heartbeating zombie at zero capacity). So catch everything — including the
    ``SystemExit`` a corrupt ``credentials.toml`` raises on refresh — log it with the thread name, and
    set ``state.stop`` so the main waiter tears the engine down deterministically, as the pre-fix single
    main-thread loop did when it raised. The reload watcher is deliberately NOT supervised: a failed
    *reload* leaves the old routing serving, so ``_reload_loop`` self-guards and keeps its daemon alive.
    """
    try:
        loop(state)
    except BaseException as exc:  # a loop-level fault must fail loud, not vanish (a job fault can't reach here)
        print(f"\n{threading.current_thread().name} stopped unexpectedly: {exc!r}", file=sys.stderr)
        traceback.print_exc()
        state.stop.set()


def _serve_loop(state: _ServeState, reload_thread: threading.Thread | None = None) -> None:
    """Heartbeat + one poll worker per concurrency slot, until stop / SIGTERM.

    ``max_concurrency`` independent daemon workers each long-poll the relay and forward one job, so up
    to N are in flight while the local engine batches them (a single loop capped real throughput at 1
    regardless of the advertised capacity). The main thread only parks on ``state.stop``: SIGTERM's
    KeyboardInterrupt unwinds *here*, never inside a worker's ``handle_job``, so no in-flight job is
    killed by the signal. Each loop runs under ``_supervise`` so a worker/heartbeat that dies from an
    unexpected fault stops the engine instead of vanishing. On stop, workers are joined against one
    shared deadline, so total teardown is bounded by ``_DRAIN_TIMEOUT`` even when every worker is parked
    in a long-poll (``state.stop`` can't wake a blocking ``relay.poll``); a job that finishes within the
    budget submits, and any worker still in flight when the budget expires is logged, not dropped silently.

    SIGHUP hot-reload coexists with the pool. The reload daemon (started by ``_start_reload_watcher``
    before this call) and every thread spawned here inherit the SIGHUP block the caller set at startup;
    this then unblocks SIGHUP on the main thread LAST — after all daemons exist — so a `grid join`/`leave`
    signal can only land here. The park's wait takes EINTR (PEP 475 retries it), the handler sets the
    reload event, and the reload daemon services it; a poll worker is never interrupted mid-forward
    (ADR 0010 C4). The reload daemon is not joined on drain — it holds no in-flight consumer job and
    exits within ≤1s of ``state.stop`` as a daemon.
    """
    heartbeat = threading.Thread(
        target=_supervise, args=(_heartbeat_loop, state), daemon=True, name="heartbeat"
    )
    heartbeat.start()
    workers = [
        threading.Thread(
            target=_supervise, args=(_poll_loop, state), daemon=True, name=f"poll-worker-{i + 1}"
        )
        for i in range(max(1, state.max_concurrency))  # the snapshot already clamps to [1, _MAX_CONCURRENCY]
    ]
    for worker in workers:
        worker.start()
    # Unblock SIGHUP on THIS (main) thread last: the reload daemon + heartbeat + N workers all inherited
    # the block, so a join/leave SIGHUP now lands here and EINTRs the park below — never on a poll worker
    # mid-forward (ADR 0010 C4).
    if hasattr(signal, "SIGHUP"):
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGHUP})
    try:
        while not state.stop.is_set():
            state.stop.wait(60)  # park; a worker/heartbeat may set stop, or SIGTERM unwinds here
    finally:
        state.stop.set()
        deadline = time.monotonic() + _DRAIN_TIMEOUT
        for worker in workers:
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        heartbeat.join(timeout=max(0.0, deadline - time.monotonic()))
        # Join the reload daemon too, against the SAME deadline — like the heartbeat, it can `register_once`
        # (re-advertise), so it must finish before the caller's `unregister_node`, or a reload's PUT could
        # land after the unregister and resurrect a node we're tearing down (ADR 0010 C5).
        if reload_thread is not None:
            reload_thread.join(timeout=max(0.0, deadline - time.monotonic()))
        # A codex rotation caught mid-exchange must not be abandoned with the workers (ADR 0015
        # D-d): its journal is on disk and the vendor may already be rotating — daemon-death now
        # loses a rotation the journal can then only DIAGNOSE ("sign in again"). Wait on the FLAG
        # (published before the journal, cleared after the persist), never on a thread: flag-unset
        # means nothing was spent AND nothing will be (the holder refuses to start once stop is
        # set, and the CAS re-checks under the store lock), and once it clears, whatever remains
        # of that worker is only relay submit work — abandonable like any straggler.
        if state.codex_seat.exchange_in_flight():
            _warn("waiting for an in-flight codex token exchange to persist — killing it now "
                  "could lose the seat's rotation")
            exchange_deadline = time.monotonic() + _CODEX_EXCHANGE_DRAIN
            while state.codex_seat.exchange_in_flight() and time.monotonic() < exchange_deadline:
                time.sleep(0.1)
            if state.codex_seat.exchange_in_flight():
                _warn("codex token exchange still unfinished at exit — if the next refresh "
                      "fails, re-run `grid join --api codex` to sign in again")
        stragglers = [worker.name for worker in workers if worker.is_alive()]
        if stragglers:
            print(
                f"\n{len(stragglers)} poll worker(s) still in flight after {_DRAIN_TIMEOUT}s drain — "
                f"abandoning ({', '.join(stragglers)}); their consumers may see no terminal response.",
                file=sys.stderr,
            )
        if reload_thread is not None and reload_thread.is_alive():
            # It didn't finish within the drain budget, so its re-register (if any) may still land after the
            # caller's unregister; the relay then TTL-prunes the resurrected node. Surface it, don't hide it.
            print(f"\nReload thread still running after {_DRAIN_TIMEOUT}s drain — a late re-register may "
                  f"briefly resurrect the node until the relay prunes it.", file=sys.stderr)


def _reload_loop(state: _ServeState, engine_id: str) -> None:
    """Wait for a SIGHUP-set reload request and hot-reload the routing in place (ADR 0010 D3).

    Clearing the event BEFORE the reload reads the record means a write+signal that lands during a
    reload re-sets the event (one extra, harmless reload) instead of being lost — the CLI's contract is
    write-record-then-signal. A failed reload logs and keeps the thread alive with the old routing
    intact (D4 F6); the 1s wait bounds how often ``stop`` is checked.
    """
    while not state.stop.is_set():
        if state.reload_requested.wait(timeout=1.0):
            state.reload_requested.clear()
            try:
                _reload_once(state, engine_id)
            except (Exception, SystemExit) as exc:  # SystemExit too — jsonio.load_json (a corrupt record)
                # and _advertised_models raise it; catching only Exception would let it kill the watcher
                # thread, silently disabling hot-reload for the process's life (ADR 0010 D4 F6). Mirrors
                # run_remote_engine_from_record's own (Exception, SystemExit) handler.
                _warn(f"reload failed (keeping current routing): {exc!r}")
                # Leave a trace the CLI can surface: it printed success on SIGHUP delivery, so this log
                # line alone would leave the operator believing the new union is advertised (issue 05).
                _set_last_reload_error(state, engine_id, str(exc) or repr(exc))


def _start_reload_watcher(
    state: _ServeState, engine_id: str, engine_results: _EngineResults, media_models: list[str],
    record: dict[str, Any],
) -> threading.Thread:
    """Make the running engine reload-ready and start the SIGHUP-driven reload daemon (ADR 0010 D3/C4).

    Retains the probe results + media fingerprint the reload reuses, installs the SIGHUP handler (which
    only sets ``reload_requested`` — it must never raise, so PEP 475 retries the interrupted long-poll),
    and starts ``_reload_loop``. The caller keeps SIGHUP blocked while calling this — so the daemon
    inherits the block and the signal lands on the main thread — then unblocks it on main afterwards.
    """
    state._engine_results = engine_results
    state.media_models = list(media_models)
    state.media_signature = _media_signature(record)
    if hasattr(signal, "SIGHUP"):
        def _on_sighup(_signum, _frame):  # noqa: ANN001 — only sets the event; never raises, so PEP 475
            state.reload_requested.set()   # retries the interrupted long-poll
        signal.signal(signal.SIGHUP, _on_sighup)
    thread = threading.Thread(target=_reload_loop, args=(state, engine_id), daemon=True)
    thread.start()
    return thread
