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
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from remote import control_plane, credentials, probe, relay
from shared import run_records

# One engine's probe result: (normalized llm_url, advertised models, upstream models, caps envelope).
_EngineResults = list[tuple[str, list[str], list[str], dict[str, Any]]]


# Engine read budget when the relay doesn't advertise one (older relay); matches its default.
_DEFAULT_INFERENCE_TIMEOUT = 600.0

# How many consecutive post-swap re-register failures a reload retries before giving up loudly (so a
# permanent relay/validation error doesn't PUT every 2s forever; the next join/leave re-triggers it).
_MAX_RELOAD_REGISTER_RETRIES = 5

# The relay-supplied endpoint is interpolated into a local engine URL, so only forward known
# endpoints — this stops a buggy or compromised relay from probing other local paths via `../`.
# Text goes to the LLM engine; media goes to this box's media server, each with its own fixed
# allowlist (the media paths are NOT under `_ALLOWED_ENDPOINTS` — they route to a different URL).
_ALLOWED_ENDPOINTS = frozenset({"chat/completions", "completions"})
_MEDIA_ENDPOINTS = frozenset({"media/image/generate", "media/image/edit", "media/video/i2v"})

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
    succeeded, so a silent stale union would be invisible (ADR 0009 D4 F6)."""
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
    # so SIGHUP only ever lands on the main thread (ADR 0009 C4).
    if hasattr(signal, "SIGHUP"):
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGHUP})

    launched: list[Any] = []
    launcher = None
    media_proc = None
    comfyui_started = False
    state = None
    rc = 0
    try:
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
            max_concurrency=int(record.get("max_concurrency") or 1),
            routes=routes,
            upstream=upstream,
            media_url=media_url,
        )
        register_once(state)
        print(f"Engine {state.node_id} serving {union_models} via the relay at {signaling_url}")
        print("Send SIGTERM (grid leave) to unregister.")
        heartbeat = threading.Thread(target=_heartbeat_loop, args=(state,), daemon=True)
        heartbeat.start()
        # Start the SIGHUP-driven reload watcher, then unblock SIGHUP on the main thread LAST — the worker
        # threads above inherited the block, so a `grid join`/`leave` SIGHUP now always lands on main: its
        # long-poll takes EINTR (PEP 475 retries it) and the handler sets the reload event, which the
        # watcher (waiting on the event, not the signal) services within ≤1s (ADR 0009 C4).
        _start_reload_watcher(state, engine_id, engine_results, media_models, record)
        if hasattr(signal, "SIGHUP"):
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGHUP})
        _poll_loop(state)  # blocks until the loop stops or SIGTERM
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
    with ``--alias``), so a job's advertised model can be rewritten to it before forwarding. Each engine
    is probed by its upstream name (Ollama/vLLM only know that) but the caps envelope is keyed by the
    advertised name (what consumers ask for). ``launched`` collects the built-in llama-servers to stop
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
            caps = probe.capabilities(
                llm_url, upstream[0], advertise_as=advertised[0], context_window=record.get("ctx_size"),
            ) if upstream else {}
            results.append((llm_url, advertised, upstream, caps))
    except BaseException:  # a later spec failed — don't orphan a server an earlier spec already launched
        if launcher_mod is not None:
            for proc in launched:
                launcher_mod.stop(proc)
        raise
    return results, launched, launcher_mod


def _flat_spec(record: dict[str, Any]) -> dict[str, Any]:
    """A record written before multi-engine (no ``engines``) → one spec from its flat fields."""
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
    if len(set(cleaned)) != len(cleaned):
        raise SystemExit("--advertise-as values must be unique.")
    return cleaned


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
    engine_results: _EngineResults, media_models: list[str], record: dict[str, Any], engine_id: str
) -> _Snapshot:
    """Build one routing snapshot from probe results + this box's media models. Used by BOTH startup and
    a hot-reload so their routing/caps/meta never drift (ADR 0009 D4). Surfaces shadowed-model warnings
    to stderr exactly as startup does."""
    routes, upstream, union_models, capabilities, warnings = _build_routing(engine_results)
    for line in warnings:
        print(line, file=sys.stderr)
    union_models, capabilities = _merge_media(union_models, capabilities, media_models)
    return _Snapshot.build(
        routes=routes, upstream=upstream, models=union_models, capabilities=capabilities,
        meta=_meta(record, engine_id), pricing=_pricing(record),
        max_concurrency=int(record.get("max_concurrency") or 1),
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
    respawn choice and this reload guard can't desync (ADR 0009 D4 F6 / C3)."""
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
    bind it once never see a torn half-update (ADR 0009 D4 F4). ``build`` is the sole normalization
    site: it copies every dict/list, so a published snapshot is never mutated in place.
    """
    routes: dict[str, str]        # advertised model -> local engine URL (normalized, no trailing /)
    upstream: dict[str, str]      # advertised model -> the name the engine itself answers to
    models: list[str]             # advertised union, first-seen order (text engines, then media)
    capabilities: dict[str, Any]  # {"schema_version": 1, "models": {...}} envelope, or {}
    meta: dict[str, Any]          # grid-page {name, engine}
    pricing: dict[str, float]     # always {} today (advertised pricing is deprecated)
    max_concurrency: int

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
    ) -> "_Snapshot":
        return _Snapshot(
            routes={model: url.rstrip("/") for model, url in routes.items()},
            upstream=dict(upstream or {}),
            models=list(models),
            capabilities=dict(capabilities or {}),
            meta=dict(meta or {}),
            pricing=dict(pricing or {}),
            max_concurrency=max(1, int(max_concurrency)),
        )


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
    ) -> None:
        self.signaling_url = signaling_url
        self.node_id = node_id
        self.network_id = network_id
        self.llm_url = llm_url.rstrip("/")
        # This box's media server base (`http://127.0.0.1:<media_port>`) when the identity serves
        # media, else None. `media/*` jobs forward here instead of an LLM engine; all media models
        # share the one server, so a single URL (not a per-model route) is enough.
        self.media_url = media_url.rstrip("/") if media_url else None
        # The reload-swappable routing (routes/upstream/models/caps/meta/pricing/concurrency) lives in
        # one immutable snapshot so a hot-reload can swap it atomically (ADR 0009 D4). Several engines
        # may serve under one identity (DECISIONS D9); for the single-engine case the route map is
        # derived so every advertised model points at the one engine.
        route_map = routes if routes is not None else {model: self.llm_url for model in models}
        self._snapshot = _Snapshot.build(
            routes=route_map, upstream=upstream or {}, models=models, capabilities=capabilities,
            meta=meta, pricing=pricing, max_concurrency=max_concurrency,
        )
        # The probe results the live snapshot was built from, kept so a reload probes only newly-added
        # engines (ADR 0009 D4 F6). Reload-owned: set at startup, thereafter only the reload loop writes.
        self._engine_results: _EngineResults = []
        # This box's media (comfyui:*) model names + a fingerprint of the media config the process
        # brought up. A hot-reload can't launch/teardown media or swap bundles, so a reload whose re-read
        # record differs here is refused — the CLI respawns instead (ADR 0009 D4 F6 / C3). Set at startup.
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

    def snapshot(self) -> _Snapshot:
        """The current routing snapshot — one atomic reference load (no lock). Bind it ONCE per
        operation, then read fields off the result, so a concurrent reload swap is never seen
        half-applied (ADR 0009 D4 F4)."""
        return self._snapshot

    def apply(self, snapshot: _Snapshot, engine_results: _EngineResults) -> None:
        """Swap in a freshly-built routing snapshot and the probe results it was built from. Rebinds
        under ``_lock`` then RELEASES before the caller re-registers, so the ``_register_lock → _lock``
        order stays acyclic — never register while holding ``_lock`` (ADR 0009 D4 F5)."""
        with self._lock:
            self._snapshot = snapshot
            self._engine_results = engine_results

    def engine_results(self) -> _EngineResults:
        """The probe results the live snapshot was built from — a reload reuses them so it re-probes
        only newly-added engines (ADR 0009 D4 F6)."""
        return self._engine_results

    def route(self, model: str | None) -> str | None:
        """The local engine URL serving ``model``.

        Exact match wins. Otherwise, when every model points at the **same single engine** (one
        distinct URL — even if that one engine serves several models), fall back to it: a job with a
        missing/unknown ``model`` still forwards as it did before multi-engine (the proxy forwarded the
        body unchanged, letting the engine answer). With several distinct engines and no match, return
        ``None`` so the caller reports "no engine serves" instead of guessing.
        """
        snap = self._snapshot  # bind once — a concurrent reload swap is never seen half-applied
        if model and model in snap.routes:
            return snap.routes[model]
        distinct = set(snap.routes.values())
        if len(distinct) == 1:
            return next(iter(distinct))
        return None

    def upstream_model(self, model: str | None) -> str | None:
        """The name the local engine answers to for an advertised ``model`` (``--advertise-as`` maps the
        consumer-facing alias back to the engine's real model name). ``None`` when unmapped — the caller
        then forwards the body's model unchanged (single-engine fallback / built-in, where they match).
        """
        snap = self._snapshot
        if model and model in snap.upstream:
            return snap.upstream[model]
        return None

    def token(self) -> str:
        with self._lock:
            return self._access_token

    def load(self) -> dict[str, Any]:
        with self._lock:
            return {"active_tasks": self._inflight}

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
    two PUTs, and a slow racer can't land a stale snapshot last (ADR 0009 D4 F4/F5). On a 401 it refreshes
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


def _reload_once(state: _ServeState, engine_id: str) -> None:
    """Rebuild routing from the (re-read) record and re-advertise the union in place — the body of the
    SIGHUP hot-reload (ADR 0009 D3). External-only and probe-only: reuse retained caps for engines
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
        advertised = _advertised_models(models, aliases)
        prev = retained.get(url)
        if prev is not None and prev[1][:1] == advertised[:1]:
            # Already serving this engine: reuse ONLY its probed caps. advertised/upstream come from the
            # record, so a model appended to this engine is still picked up, not dropped (ADR 0009 C2).
            reassembled.append((url, advertised, list(models), prev[3]))
        else:  # a genuinely new engine (or a changed first model) → probe it once
            caps = probe.capabilities(
                url, models[0], advertise_as=advertised[0], context_window=record.get("ctx_size"),
            ) if models else {}
            reassembled.append((url, advertised, list(models), caps))

    snapshot = _assemble_snapshot(reassembled, state.media_models, record, engine_id)
    state.apply(snapshot, reassembled)  # atomic swap; in-flight requests were unaffected until here
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
        # (ADR 0009 C5). Reuse means the retry never re-probes.
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

    if endpoint in _MEDIA_ENDPOINTS:  # media → this box's media server; always SSE, so always stream
        if not state.media_url:
            _try_submit_error(state, txn, f"this engine does not serve media (endpoint {endpoint!r})")
            return
        state.enter_inference()
        try:
            _forward_stream(state, txn, endpoint, body, read_timeout, state.media_url)
        except Exception as exc:  # one bad media job must not kill the loop
            print(f"\nMedia job {txn} failed: {exc!r}", file=sys.stderr)
            _try_submit_error(state, txn, str(exc))
        finally:
            state.exit_inference()
        return
    if endpoint.startswith("media/"):  # a media path we don't serve — never blind-forward it
        _try_submit_error(state, txn, f"unsupported media endpoint: {endpoint!r}")
        return
    if endpoint not in _ALLOWED_ENDPOINTS:  # don't forward an unknown path to the local engine
        _try_submit_error(state, txn, f"unsupported endpoint: {endpoint!r}")
        return

    model = body.get("model")  # body is already `job.get("body") or {}`, so it is a dict
    target = state.route(model)  # which local engine serves this model (DECISIONS D9)
    if target is None:
        _try_submit_error(state, txn, f"no engine serves model {model!r}")
        return

    # Consumers address the model by its advertised name; an external engine behind ``--advertise-as``
    # only knows its real name, so rewrite the body's model before forwarding (a new dict — never
    # mutate the job). No mapping / already-equal → forward unchanged (built-in + single-engine paths).
    upstream_model = state.upstream_model(model)
    forward_body = {**body, "model": upstream_model} if upstream_model and upstream_model != model else body

    state.enter_inference()
    try:
        if is_stream:
            _forward_stream(state, txn, endpoint, forward_body, read_timeout, target)
        else:
            _forward_whole(state, txn, endpoint, forward_body, read_timeout, target)
    except Exception as exc:  # one bad job must not kill the loop
        print(f"\nJob {txn} failed: {exc!r}", file=sys.stderr)
        _try_submit_error(state, txn, str(exc))
    finally:
        state.exit_inference()


def _try_submit_error(state: _ServeState, txn: str, message: str) -> None:
    """Report a job failure to the relay, best-effort — a failed report is logged, never raised."""
    try:
        relay.submit_error(state.signaling_url, state.token(), txn, message=message)
    except (relay.RelayError, relay.RelayUnauthorized) as exc:
        print(f"\nCouldn't report job {txn} failure to the relay: {exc}", file=sys.stderr)


def _forward_whole(
    state: _ServeState, txn: str, endpoint: str, body: dict[str, Any], read_timeout: float, target_url: str
) -> None:
    import httpx

    timeout = httpx.Timeout(connect=10, read=read_timeout, write=30, pool=10)
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(
            f"{target_url}/{endpoint}", json=body, headers={"Content-Type": "application/json"}
        )
    if resp.status_code != 200:
        _try_submit_error(state, txn, f"engine error: {resp.status_code}")
        return
    relay.submit_response(state.signaling_url, state.token(), txn, content=resp.content, stream=False)


def _forward_stream(
    state: _ServeState, txn: str, endpoint: str, body: dict[str, Any], read_timeout: float, target_url: str
) -> None:
    import httpx

    timeout = httpx.Timeout(connect=10, read=read_timeout, write=None, pool=10)
    with httpx.Client(timeout=timeout) as client:
        with client.stream(
            "POST", f"{target_url}/{endpoint}", json=body, headers={"Content-Type": "application/json"}
        ) as engine_resp:
            if engine_resp.status_code != 200:
                engine_resp.read()
                _try_submit_error(state, txn, f"engine error: {engine_resp.status_code}")
                return
            # Pass the engine's SSE bytes straight through while its stream is open.
            relay.submit_response(
                state.signaling_url, state.token(), txn,
                content=_traced_stream(txn, engine_resp.iter_bytes()), stream=True,
            )
            _debug(f"stream txn={txn} submit_response returned (relay accepted the full stream) t={time.time():.3f}")


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
        state.stop.wait(relay.HEARTBEAT_INTERVAL)


def _reload_loop(state: _ServeState, engine_id: str) -> None:
    """Wait for a SIGHUP-set reload request and hot-reload the routing in place (ADR 0009 D3).

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
                # thread, silently disabling hot-reload for the process's life (ADR 0009 D4 F6). Mirrors
                # run_remote_engine_from_record's own (Exception, SystemExit) handler.
                _warn(f"reload failed (keeping current routing): {exc!r}")


def _start_reload_watcher(
    state: _ServeState, engine_id: str, engine_results: _EngineResults, media_models: list[str],
    record: dict[str, Any],
) -> threading.Thread:
    """Make the running engine reload-ready and start the SIGHUP-driven reload daemon (ADR 0009 D3/C4).

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
