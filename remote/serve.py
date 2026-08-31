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

import json
import os
import signal
import sys
import threading
import time
import traceback
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Callable

from remote import (
    api_keys, bringup, control_plane, credentials, engine_health, probe, relay, service_truth,
    codex_auth, codex_oauth, task_capacity, task_opt_in, throughput,
)
from shared.handlers import HANDLERS
from shared import run_records
from shared.system import node_hardware
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
# The Responses dialect's endpoint literal, authored HERE — never taken from the wire. Kept OUT of the
# closed `_ALLOWED_ENDPOINTS` frozenset and composed onto a hardware engine's served set only when its
# join-time probe found the route (issue 08 / ADR 0018 decision 3): the probe decides WHETHER this
# authored literal is included, never WHAT it is, so nothing discovered over the network reaches URL
# construction. Must match grid-src's `endpoint_path` byte-for-byte (CLAUDE.md lockstep).
_RESPONSES_ENDPOINT = "responses"
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
    succeeded, so a silent stale union would be invisible (ADR 0010 D4 F6).

    Delegates to ``bringup.log`` so the child has exactly ONE narration voice: the bring-up trail and
    every later warning land in the same file, in the same shape, and a test that captures one
    captures the other (ADR 0022)."""
    bringup.log(msg)


def _bookkeep(what: str, write: Callable[..., Any], *args: Any) -> bool:
    """Run one piece of best-effort run-record bookkeeping that must never take a serving engine down.

    Every one of these runs *inside* something more important than itself — a registration, a
    heartbeat tick, a reload's except arm. A raise there would either kill a watcher thread or
    replace the operator's real failure reason with a disk error (Python demotes the original to
    ``__context__``, which nothing prints). ``SystemExit`` is caught alongside ``Exception`` because
    ``jsonio`` raises it for a corrupt record, and this is a thread, where a bare ``SystemExit`` dies
    silently. The failure is reported, never swallowed: the child's log is where a future orphan
    investigation starts.

    Returns whether the write **landed**. Callers whose fact must not be lost (the registration
    stamp, the error heal) key their retry off this instead of assuming one attempt is enough — a
    swallowed failure is invisible to the operator by design, so it has to be visible to us. A writer
    that returns nothing has simply succeeded; only an explicit ``False`` means "not settled"."""
    try:
        outcome = write(*args)
    except (Exception, SystemExit) as exc:
        _warn(f"could not update {what} on the run record (ignoring): {exc!r}")
        return False
    return outcome is not False


# ---------------------------------------------------------------------------
# Detached entry
# ---------------------------------------------------------------------------

def _stamp_own_pid(grid_id: str, engine_id: str) -> None:
    """Heal pid drift at the source: under the record lock, write our real ``os.getpid()`` into our
    own run record, so the recorded pid is always this live serve process's — regardless of what the
    spawner wrote (``proc.pid`` of a launcher whose interpreter is a group-child, the ``0`` caught in
    the join write-race window, or a copied/older record).

    Holds the SAME ``file_lock`` the CLI join-append and leave teardown take (``cli.remote_provider``),
    so a concurrent write can't be lost and "record gone" is observed atomically. Read-checks first
    (``update_record`` no-ops when the record is gone), so a record a concurrent full leave just
    deleted is never resurrected. This heals drift for a leave that reads the record AFTER our stamp;
    a leave that wins the lock BEFORE it still acts on the spawner's pid, and the true child is then
    reaped by issue-02's argv orphan sweep (``shared.orphan_sweep``), not by this stamp. Best-effort:
    a failed stamp warns and serves on — the engine must serve even if the disk write hiccups (the
    reload bookkeeping's never-raise contract)."""
    try:
        with file_lock(run_records.record_path(grid_id, engine_id)):
            run_records.update_record(grid_id, engine_id, **run_records.identity_stamp(os.getpid()))
            # Declare, before any socket is opened, that this build reports service truth (issue 10).
            # The sidecar's ABSENCE is what keeps the join gate quiet about an older build's child, so
            # this must land here rather than at first registration: the incident regime is a child
            # wedged in bring-up, which never reaches its first heartbeat. Its own wrapper, so a
            # sidecar failure can neither mask nor be mistaken for the pid stamp's.
            _bookkeep("heartbeat sidecar", service_truth.touch_heartbeat, grid_id, engine_id)
    except (Exception, SystemExit) as exc:  # never let a disk hiccup stop the engine serving
        _warn(f"could not stamp live pid into the run record for {engine_id}@{grid_id}: {exc}")


def run_remote_engine_from_record(grid_id: str, engine_id: str) -> int:
    """Detached ``__remote-engine`` entry: serve one engine to the grid's relay until SIGTERM."""
    # Name ourselves before anything else can fail — including the record read below, which raises
    # `SystemExit` on a corrupt *sibling* record (`read_record` globs the whole grid directory) from
    # outside the try, before any handler exists. From here on this child's log cannot be empty
    # (ADR 0022): the log file is created by the spawner and the child is unbuffered, so a 0-byte log
    # used to be positive evidence that the first line simply came too late — it came after
    # registration, which is precisely the step a wedged child never reaches.
    bringup.log(f"starting engine {engine_id}@{grid_id} (pid {os.getpid()})")
    record = run_records.read_record(grid_id, engine_id)
    if not record:
        raise SystemExit(f"No engine record for {engine_id} on {grid_id}.")
    network_id = record["grid_id"]  # the run record's grid_id IS the remote network_id
    signaling_url = (record.get("signaling_url") or "").rstrip("/")
    if not signaling_url:
        raise SystemExit("This grid has no relay address; run `grid start` then re-join.")
    access_token, refresh_token = _load_tokens(network_id)
    if not access_token:
        raise SystemExit("Run `grid login` to refresh your grid tokens, then re-join.")
    # The relay binds the node to the token: it authorizes PUT /nodes/{node_id} only for the token's
    # own node (else 403 "Cannot access another node"). So node_id is read from the JWT, never invented.
    node_id = credentials.node_id_from_token(access_token)
    if not node_id:
        raise SystemExit(
            "This grid's access token carries no node identity; run `grid login` to refresh your "
            "tokens, then re-join."
        )
    # The intent line: what this child will serve and where it will register, from the record alone,
    # so it lands before the first socket rather than after the last one.
    bringup.log(bringup.describe_intent(record, signaling_url))

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
    seat_procs: list[Any] = []
    comfyui_started = False
    state = None
    registered = False
    rc = 0
    try:
        # Heal pid drift at the source before the (possibly slow) engine bring-up: once we stamp, the
        # record's pid is THIS live process, so a later `grid leave` kills the right child instead of
        # whatever the spawner recorded (proc.pid of a launcher, or the transient 0). A leave that wins
        # the record lock before we stamp falls back to issue-02's argv sweep. Best-effort +
        # read-checked (see `_stamp_own_pid`).
        _stamp_own_pid(grid_id, engine_id)
        # API-engine keys come from the machine-local key store (env var as fallback) — resolve
        # them up front so a keyless respawn dies naming the fix instead of advertising models
        # whose every job would 401 upstream. Never read from the record; the record never carries
        # a key. Inside the try so this death reaps the record like any died-before-registering
        # engine (the `finally` below).
        # CLI seats: bring each one's loopback server up before `_bring_up_engines`, which probes
        # every spec's endpoint_url.
        seat_procs = _start_cli_seats(record)
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
            engine_id=engine_id,  # which record this process writes its service truth onto
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
        state._seat_urls = _cli_seat_urls(record)
        _prime_codex_seat(state, record)
        # Restore the last persisted quota so /grid/overview shows it immediately on respawn — and so a
        # hand-edited remote.json can inject a simulated snapshot for debugging. A response/seed refreshes it.
        # Ahead of the register loop below (which may block for several backoffs): the restore reads the
        # record this process already holds, so it must not wait on the relay to become reachable.
        if record.get("codex_rate_limits"):
            state.set_codex_quota(record["codex_rate_limits"])

        def _note_register_error(message: str | None) -> None:
            # Bounded and body-only: `RelayError` carries the response text, never the bearer.
            #
            # `_bookkeep`'s "did the write land" answer is deliberately NOT consumed here, unlike
            # `mark_registered`'s. Three reasons, and the last one is why keying a retry off it would
            # be a bug: this field fails OPEN (absent reads as "no error", never as an accusation —
            # `registered_at` is what says "not registered"); a transient failure self-heals within
            # one backoff *structurally*, because `mutate_record` re-reads the record under lock and
            # compares against DISK — a write that never landed leaves the record still differing, so
            # the next attempt writes it, with nothing to remember between them; and `mutate_record`
            # returns False both when the record is gone AND when nothing changed
            # — and "nothing changed" is the *normal* case here, since a relay down for an hour keeps
            # producing the identical reason. A caller that retried on that would spin on the healthy
            # path. `mark_registered` has to compensate for the same conflation explicitly.
            _bookkeep("last_register_error", service_truth.note_register_error,
                      network_id, engine_id, message)

        try:
            # Retry a relay that is merely temporarily wrong, rather than dying of it (ADR 0022).
            # This is where the field incident lands: a master mid-respawn answers 503 (or 404), and
            # before this loop that single answer ended the child. `state.stop` is the wait, so a
            # SIGTERM during a backoff still unwinds inside the parent's 25s kill grace.
            bringup.register_with_backoff(
                lambda: register_once(state),
                stop=state.stop,
                log=bringup.log,
                note_error=_note_register_error,
            )
        except (Exception, SystemExit) as exc:
            # Only a TERMINAL failure reaches here now (the relay understood and refused, or
            # something unexpected raised). Leave the reason on the record before this child dies of
            # it: the record usually goes with it (the `finally` reaps a never-registered child), but
            # not when ownership can't be proven — and a still-trying child's reason is written by
            # the loop above, which is what `grid join` reports while it keeps trying.
            _note_register_error(str(exc) or repr(exc))
            raise
        registered = True
        _seed_codex_quota(state, record)  # best-effort: populate quota for /grid/overview before jobs
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
        for kind, proc in seat_procs:
            from local.cli_seat_runtime import stop_seat_server

            try:
                stop_seat_server(proc)
                print(f"Stopped {kind} seat.")
            except Exception as exc:  # best-effort teardown; never mask the real exit
                print(f"Stopping the {kind} seat failed (ignoring): {exc}", file=sys.stderr)
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
            # whose ComfyUI never became ready), so it doesn't linger and force a `grid leave --all` —
            # but ONLY while that record still points at us. A record a newer serve child now owns is
            # left alone: unlinking it would strand that live child untracked (issue 05's audit).
            # No guard needed here — like every other step in this `finally`, it is best-effort and
            # reports its own failures to stderr rather than raising over the real exit error.
            run_records.discard_own_record(grid_id, engine_id)
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
    # ONE clock for the whole fan-out — every spec, every model — because the wedge this bounds is the
    # sum, not any single probe (ADR 0022). Opened here rather than inside `_probe_spec_caps` so a
    # second engine cannot buy itself a fresh budget after the first spent it.
    deadline = bringup.probe_deadline()
    unprobed: list[str] = []
    try:
        for spec in specs:
            llm_url, proc, mod, advertised, upstream = _bring_up_one(spec, record, aliases)
            if proc is not None:
                launched.append(proc)
                launcher_mod = mod
            # Probe EVERY model this spec serves (not just the first), so a multi-model `--at` advertises
            # caps for all of them — shared with the hot-reload path (`_reload_once`) so the two can't drift.
            try:
                caps = _probe_spec_caps(
                    llm_url, advertised, upstream, record.get("ctx_size"), api_kind=spec.get("api_kind"),
                    model_caps=spec.get("model_caps"), deadline=deadline,
                )
            except bringup.ProbeBudgetExceeded as exc:
                # Startup's policy: keep whatever was probed and give the rest the same fail-closed
                # entry a failed probe already produces. Correct HERE — and only here — because there
                # is no previous verdict to keep: an unprobed model degrades to the chat-only posture
                # rather than lying about what it can do.
                caps = _degraded_caps(exc, record.get("ctx_size"))
                unprobed.extend(exc.skipped)
            results.append((llm_url, advertised, upstream, caps))
    except BaseException:  # a later spec failed — don't orphan a server an earlier spec already launched
        if launcher_mod is not None:
            for proc in launched:
                launcher_mod.stop(proc)
        raise
    if unprobed:
        # Never silent: a model losing its capabilities is the failure this bound exists to prevent,
        # not an acceptable way to achieve it.
        bringup.log(
            f"capability probing hit its {bringup.PROBE_BUDGET:.0f}s budget — advertising "
            f"{', '.join(unprobed)} without probed capabilities (the engine was not answering); "
            "re-join once it is healthy to advertise them fully"
        )
    return results, launched, launcher_mod


def _degraded_caps(exc: bringup.ProbeBudgetExceeded, ctx_size: Any) -> dict[str, Any]:
    """The envelope to advertise when the probe budget bit part-way through a spec.

    Every model the spec serves gets an entry — the probed ones theirs, the skipped ones the
    fail-closed all-False entry a failed probe already produces. The **key must be present** even
    though nothing is claimed: the relay validates that ``capabilities.models``' keys equal the
    advertised model list exactly and answers 400 otherwise, and 400 is terminal to bring-up's retry
    loop — so omitting the skipped models would make this degrade *kill* the slow engine it exists to
    keep serving, on every join. That mismatch was unreachable before the budget existed, because
    ``probe.capabilities`` never omits a model, only empties it.
    """
    models = dict((exc.probed or {}).get("models") or {})
    for name in exc.skipped:
        models[name] = probe.unprobed_entry(ctx_size)
    return {"schema_version": 1, "models": models} if models else {}


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
    from local import runtime

    port = int(record.get("endpoint_port") or 8081)
    if runtime.port_in_use(port):
        # The exact bug already fixed once for the LOCAL `--serve` join (`cli/provider.py`) —
        # this is `remote/serve.py`'s own separate copy of the same check, missed at the time
        # because the two paths don't share the function. A remote join has no interactive
        # terminal to retype a flag into: the detached child just dies, and the only trace was
        # "Remote engine stopped: Port 8081 already in use; aborting." in this engine's own log —
        # `grid join` itself reports "starting" and success regardless, because (per the comment
        # below) the relay isn't locally pollable and this process can't confirm registration.
        holder = runtime.port_holder(port)
        replacement = runtime.free_port_from(port + 1)
        if replacement is None:
            raise SystemExit(
                f"Port {port} is already in use{f' by {holder}' if holder else ''}, "
                "and no free port was found near it."
            )
        print(f"Port {port} is in use{f' by {holder}' if holder else ''} — starting on {replacement} instead.")
        port = replacement
    launcher_mod.assert_supported_build()
    launched = launcher_mod.start_llm(
        models[0],
        port=port,
        ctx_size=record.get("ctx_size"),
        n_predict=record.get("n_predict"),
        parallel=record.get("parallel"),
        flash_attn=record.get("flash_attn"),
        mmproj=record.get("mmproj"),
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


def _cli_seat_urls(record: dict[str, Any]) -> list[str]:
    """Loopback URL of every CLI seat this record serves — what the heartbeat asks for quota."""
    from shared.agent import cli_seat

    return [
        f"http://127.0.0.1:{cli_seat.options_from_spec(spec).port}"
        for spec in (record.get("engines") or [])
        if api_catalog.local_seat_port(str(spec.get("api_kind") or "")) is not None
    ]


def _start_cli_seats(record: dict[str, Any]) -> list[tuple[str, Any]]:
    """Start a loopback server for every CLI-seat spec in ``record``. Returns (kind, proc) pairs."""
    from local.cli_seat_runtime import start_seat_server
    from shared.agent import cli_seat
    from shared.agent.seats import seat_for

    started: list[tuple[str, Any]] = []
    for spec in record.get("engines") or []:
        kind = str(spec.get("api_kind") or "")
        if api_catalog.local_seat_port(kind) is None:
            continue
        seat = seat_for(kind)
        options = cli_seat.options_from_spec(spec)
        proc = start_seat_server(
            kind=kind, options=options, binary=cli_seat.assert_available(seat)
        )
        print(f"{seat.label} seat up on http://127.0.0.1:{options.port} (pid={proc.pid})")
        started.append((kind, proc))
    return started


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
        # Only a "key" kind has a credential the grid holds. An OAuth seat resolves its own at
        # forward time (ADR 0015 D-d) and a CLI seat has none at all — both read from the catalog
        # rather than from a branch here, so a new kind never needs a new exemption.
        if api_catalog.kind_credential(str(kind)) != "key":
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


def _seed_codex_quota(state: _ServeState, record: dict[str, Any]) -> None:
    """Seed the seat's quota right after registration, so /grid/overview shows it BEFORE any consumer
    traffic (the "one minimal request after join" idea). One tiny streamed request whose x-codex-*
    headers carry the quota; the stream is closed the moment the headers are read, so it spends ~no
    output. Best-effort: a non-codex record is a no-op, and any failure (offline, CF-403, dead seat)
    is swallowed — a telemetry seed must never break serve start; the first real job seeds it anyway."""
    import httpx

    spec = next(
        (s for s in record.get("engines") or [] if s.get("api_kind") == api_catalog.CODEX_KIND), None
    )
    if spec is None:
        return
    base_url = (spec.get("endpoint_url") or "").rstrip("/")
    model = next(
        (_api_upstream_name(api_catalog.CODEX_KIND, m) for m in spec.get("models") or []), None
    )
    if not base_url or not model:
        return
    try:
        bundle = state.codex_seat.bundle()
    except Exception:  # noqa: BLE001 — unsigned/unreadable seat: nothing to seed, not an error here
        return
    body = {
        "model": model,
        "instructions": "ping",
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        "stream": True,
        "store": False,
    }
    try:
        timeout = httpx.Timeout(connect=10, read=15, write=30, pool=10)
        with httpx.Client(timeout=timeout) as client:
            with client.stream(
                "POST", f"{base_url}/responses", json=body, headers=_codex_headers(bundle),
            ) as resp:
                # Headers are here as soon as the response starts; harvest and let the `with` close the
                # stream without draining the body — the seed stays ~free.
                _capture_codex_quota(state, resp.headers)
    except Exception as exc:  # noqa: BLE001 — seed is optional telemetry, never fatal to serve start
        _warn(f"codex quota seed skipped ({exc})")


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
    """Refuse the job with an openai-style `engine error 400: {"error": {...}}`. For the openai/chat
    case this is byte-for-byte what forwarding would have earned, so consumers and the relay's
    terminal-error mapper see no difference, minus the vendor round-trip. For a kind whose real 400
    wears a different shape — the codex seat answers `{"detail": ...}` (facts.md #7) — it is NOT
    byte-identical, but the relay's `_terminal_error` keys on `["error"]["message"]`, so this shape
    yields a CLEANER extracted message than the raw seat body would, and the relay re-renders it per
    dialect (chat json / responses `response.failed`) either way.

    Only the first unsupported param is named, mirroring the relay's own pre-queue denylist (which
    raises on the first `param in body` match): a multi-violation body is corrected one param per
    round-trip, consistently across both layers."""
    param = params[0]
    payload = json.dumps({"error": {
        "message": f"Unsupported parameter: '{param}' is not supported with this model.",
        "type": "invalid_request_error",
        "param": param,
        "code": "unsupported_parameter",
    }})
    _try_submit_error(state, txn, f"engine error 400: {payload}")


def _refuse_stream_only_seat(state: _ServeState, txn: str) -> None:
    """Refuse a non-stream responses job for the stream-only subscription seat (issue 05, AC7).

    The seat's backend speaks SSE only. The relay lifted its global stream-mandatory rule so that every
    other engine may serve a non-stream responses request; this per-kind gate — the layer that knows
    the kind — re-homes the seat's refusal, wearing the same `engine error 400: {…}` string the other
    per-kind gates use. The relay's `_terminal_error` extracts (400, "Stream must be set to true") and
    renders this dialect's `{"detail": "Stream must be set to true"}` + 400 — byte-identical to the old
    pre-queue refusal, so the seat's observable behaviour is unchanged (user story 16)."""
    payload = json.dumps({"error": {
        "message": "Stream must be set to true",
        "type": "invalid_request_error",
        "param": "stream",
        "code": "unsupported_parameter",
    }})
    _try_submit_error(state, txn, f"engine error 400: {payload}")


def _adapt_output_token_param(body: dict[str, Any], api_kind: str | None, endpoint: str) -> dict[str, Any]:
    """Rename the output-token cap to the vendor's CHAT parameter when forwarding a chat job to an
    API engine.

    The relay's chat normaliser rewrites every request to ``max_tokens`` — the only name hardware
    engines understand — including a consumer's ``max_completion_tokens``. A vendor that renamed the
    parameter (OpenAI's GPT-5.x) then 400s on every job, so translate on the way out. ``max_tokens``
    holds the value the relay validated against its cap, so it wins over any ``max_completion_tokens``
    left beside it by that rewrite; only one name may go upstream.

    Scoped to ``chat/completions`` (issue 04). The relay's responses contract is a PASSTHROUGH — it
    does not normalise to ``max_tokens``; it lets the dialect's own cap ``max_output_tokens`` through
    byte-for-byte, already the name the vendor's responses endpoint wants. So on responses there is
    nothing to translate, and applying the chat rename would MIS-name a stray ``max_tokens`` as the
    chat spelling that endpoint rejects. (The relay also refuses ``max_tokens`` on responses, so a real
    body never carries it there — but this guard does not lean on that cross-repo rule.)

    Returns ``body`` unchanged for the responses dialect, for hardware engines, for vendors that still
    take ``max_tokens``, and for vendors that take no output cap at all.
    """
    if not api_kind or endpoint != "chat/completions":
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


def _codex_entry_from_caps(advertised_model: str, caps: dict[str, Any]) -> api_catalog.ApiModelEntry:
    """Rebuild the ``ApiModelEntry`` ``codex_capability_entry`` consumes from the caps the join
    persisted in the run record (issue 10a). ``vendor_name`` is the real slug (unread on the codex
    envelope path, but faithful rather than a placeholder); json/structured are always False — a
    Responses passthrough can't claim chat-dialect features. ``context_window`` falls back to the
    ``0`` unknown sentinel (→ omitted from the envelope) when absent/non-numeric, never fabricated."""
    ctx = caps.get("context_window")
    return api_catalog.ApiModelEntry(
        vendor_name=advertised_model.partition(":")[2] or advertised_model,
        context_window=ctx if isinstance(ctx, int) and not isinstance(ctx, bool) else 0,
        supports_tools=bool(caps.get("tools")),
        supports_vision=bool(caps.get("vision")),
        supports_json_mode=False,
        supports_structured_outputs=False,
    )


def _codex_caps_entry(advertised_model: str, caps: dict[str, Any] | None) -> dict[str, Any]:
    """One codex model's capability envelope, from the caps the join PERSISTED in the run record
    (issue 10a). ``caps`` absent — a record written before the live-probe catalog, or a caps map that
    dropped this model — degrades to the responses-only fail-closed entry (no capability claims), with
    a warn: the same posture as a model gone from a static whitelist, so an absent entry is never a
    silent no-caps advertisement. ``vendor_rank`` rides only when it is a real int."""
    if not caps:
        _warn(
            f"{advertised_model!r} has no probe-derived caps in the run record "
            "(it predates the live-probe catalog, or its caps were dropped) — advertising it "
            "responses-only with no capability claims; re-run `grid join --api codex` to refresh"
        )
        return probe.codex_capability_entry(None)
    rank = caps.get("vendor_rank")
    return probe.codex_capability_entry(
        _codex_entry_from_caps(advertised_model, caps),
        vendor_rank=rank if isinstance(rank, int) and not isinstance(rank, bool) else None,
    )


def _static_api_caps(
    api_kind: str, advertised: list[str], model_caps: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    """An API engine's caps envelope. Most kinds take it from the static whitelist — API engines are
    never live-probed or benchmarked (ADR 0012); the vendor sees no traffic until a real job forwards
    — and a model missing from the whitelist (catalog edited between join and respawn) degrades like a
    failed probe: an all-False entry, never a crash.

    The codex kind is the exception (issue 10a): its served set + per-model caps come from the seat's
    live probe, PERSISTED at join in ``model_caps`` (advertised name → context_window/vision/tools/
    vendor_rank), because no static table exists to look them up from. A codex model advertised but
    absent from ``model_caps`` — a record written before those caps existed, or a caps map that lost a
    model — degrades to the responses-only fail-closed entry, with a warn. ``model_caps`` is ignored
    for every other kind."""
    no_features = dict.fromkeys(
        ("vision", "tools", "parallel_tool_calls", "json_object", "json_schema"), False
    )
    caps_models: dict[str, Any] = {}
    caps_by_model = model_caps or {}  # codex only: the per-model caps the join persisted in the record
    # Which relay endpoint(s) this kind serves comes from its catalog row (issue 03) — NOT a hardcoded
    # chat-only list. This is the wire contract grid-src's per-model `provider_supports` filter reads,
    # so a kind that serves `responses` (openai) must advertise it here or nothing routes. A kind gone
    # from the catalog between join and respawn degrades to chat-only, matching `_served_endpoints`.
    whitelist = api_catalog.WHITELISTS.get(api_kind)
    kind_endpoints = list(whitelist.endpoints) if whitelist else ["chat/completions"]
    # Loop-invariant per-kind fact, hoisted beside kind_endpoints (issue 06b): whether this kind honours
    # a Responses output cap. Sourced from the SAME catalog fact the engine gate refuses on
    # (`unsupported_params`) via `kind_honours_output_cap`, so the auto-router filter (layer 2) and the
    # per-kind engine gate (layer 3, issue 04) can never disagree — openai True, any future can't-cap kind
    # False for free. Gated per-MODEL on `entry is not None` at the call below (a stale model fails closed).
    kind_can_cap = api_catalog.kind_honours_output_cap(api_kind)
    for advertised_model in advertised:
        if api_kind == api_catalog.CODEX_KIND:
            # Issue 10a: the codex served set + caps come from the seat's live probe, PERSISTED per
            # model in the run record (`model_caps`) — there is no static table to look them up from,
            # so this reads the record, not `find_advertised`. The honest responses-only envelope
            # (endpoints ["responses"], no chat-dialect flags, no output cap) is built from those caps;
            # a model absent from them fail-closes with a warn (the stale-catalog posture).
            caps_models[advertised_model] = _codex_caps_entry(
                advertised_model, caps_by_model.get(advertised_model)
            )
            continue
        entry = api_catalog.find_advertised(api_kind, advertised_model)
        if entry is None:
            # A local data-integrity condition, not a transient probe failure — leave a trace, or
            # tool/vision consumers break with no diagnostic trail (matches the reload _warn style).
            _warn(
                f"{advertised_model!r} is no longer in the {api_kind} whitelist "
                "(catalog changed since join) — advertising it with no capabilities"
            )
        probed = api_catalog.probed_features(entry) if entry else no_features
        ctx = entry.context_window if entry else None
        # Media API engines (e.g. Doggi) advertise media endpoints — their jobs run through
        # HANDLERS, not the text relay dialects, so the per-kind text contract below does not
        # apply to them. Text API engines advertise the kind's catalog row (above), never legacy
        # `completions` — an API engine never serves it, and the handle_job gate refuses it anyway
        # (honest advertisement, ADR 0012).
        # `honours_output_cap` is gated on `entry is not None`: a model gone from the whitelist degrades
        # to an all-False FEATURES dict (like a failed probe), so its `output_cap` is False too — a capped
        # `auto` request then excludes the anomaly rather than routing to a likely-broken model, the
        # fail-closed posture the whole filter relies on. `endpoints` stays per-KIND (outside features)
        # because it is what makes the gate refuse a wrong dialect at all.
        if api_kind in HANDLERS:
            env = probe.envelope(advertised_model, probed, ctx, endpoints=["media"])
        else:
            env = probe.envelope(
                advertised_model, probed, ctx, endpoints=kind_endpoints,
                honours_output_cap=entry is not None and kind_can_cap,
            )
        caps_models.update((env or {}).get("models") or {})
    return {"schema_version": 1, "models": caps_models} if caps_models else {}


def _probe_spec_caps(
    llm_url: str, advertised: list[str], upstream: list[str], ctx_size: Any,
    api_kind: str | None = None, model_caps: dict[str, dict[str, Any]] | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Probe EVERY model a spec serves into one caps envelope — keyed by the advertised name, probed by
    the upstream name (Ollama/vLLM only know that). Shared by startup (`_bring_up_engines`) and the
    hot-reload path (`_reload_once`) so a multi-model `--at` engine advertises caps for ALL its models on
    BOTH paths, not just the first (main's 5078c8c fix, kept in ONE place so the two can't drift). N
    sequential probes at join/reload (one-time; N is small). A failed probe returns an all-False entry for
    that one model (`probe.capabilities` never raises), so one bad model can't sink the node; ``{}`` only
    when the spec serves no models. An API spec (``api_kind``) takes its caps from the static whitelist
    instead — no probe ever targets the vendor — via the same seam so startup and reload can't drift;
    the codex kind additionally passes ``model_caps`` (its live-probe caps persisted in the record).
    """
    if api_kind:
        return _static_api_caps(api_kind, advertised, model_caps=model_caps)
    if not advertised:
        return {}  # no models → nothing to advertise; skip the route probe's network round-trip (loop was a no-op)
    # `deadline` bounds this whole fan-out, not each call in it (ADR 0022) — the API branch above is
    # static data and is never gated. It RAISES rather than degrading, because the two callers want
    # opposite things: startup has no previous verdict and degrades fail-closed, while a reload holds
    # a live registration it must not poison, so it refuses instead. Policy belongs to the caller.
    if deadline is not None and time.monotonic() >= deadline:
        raise bringup.ProbeBudgetExceeded({}, advertised)
    # Discover the Responses dialect ONCE per engine (issue 08 / ADR 0018): the route is a property of
    # the server, not of a model, so probe here — OUTSIDE the per-model loop — and stamp the one answer
    # onto every model. A capable engine advertises `responses` in its endpoints and (PRD §3 — every
    # non-seat engine accepts an output cap) the `output_cap` routing FEATURE too, so a capped `auto`
    # request can land on it; a failed/absent probe leaves the pre-Phase-2 chat pair untouched (fail
    # closed). The API-kind branch returned above is never probed — seat/openai caps are static data.
    serves_responses = probe.probe_responses_endpoint(llm_url)
    # Opt-in breadcrumb (GRID_ENGINE_DEBUG): the one lever an operator has to answer "why didn't my
    # engine pick up Responses" — the probe verdict is otherwise silent, and a False sticks until re-join.
    _debug(f"responses probe {llm_url!r}: {'serves' if serves_responses else 'does not serve'} /responses")
    endpoints = ["chat/completions", "completions"] + (["responses"] if serves_responses else [])
    caps_models: dict[str, Any] = {}
    for index, (advertised_model, upstream_model) in enumerate(zip(advertised, upstream, strict=True)):
        if deadline is not None and time.monotonic() >= deadline:
            raise bringup.ProbeBudgetExceeded(
                {"schema_version": 1, "models": caps_models} if caps_models else {}, advertised[index:]
            )
        env = probe.capabilities(
            llm_url, upstream_model, advertise_as=advertised_model, context_window=ctx_size,
            endpoints=endpoints, honours_output_cap=serves_responses,
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
    """How the node appears on the grid page: name, engine kind label, and the machine itself.

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
    meta = {"name": record.get("meta_name") or engine_id, "engine": label}
    # What the machine IS — chip on Apple Silicon, card elsewhere. Without it the grid page can only
    # say the hostname and the OS ("Grid-Relay · macOS"), which is the same sentence for a laptop
    # and for a 192 GB Mac Studio. Empty fields are dropped rather than sent blank: the relay merges
    # meta over what it holds, so a probe that failed this run must not erase a name that worked on
    # the last one. Cached in `node_hardware`, so the heartbeat path pays nothing.
    meta.update(node_hardware.meta_fields())
    # The seat tier of an API engine (codex: free/plus/pro/…), surfaced on the grid page next to the
    # engine kind. A public label, never a secret — distinct from the token/account_id we never emit.
    # Union identities can gather several engines; take the first spec that carries one (only API
    # engines like codex do — hardware specs never set plan_type), so a codex+hardware union still
    # shows the codex tier. None (no such engine, or vendor said nothing) omits the key entirely.
    plan_type = next(
        (e.get("plan_type") for e in (record.get("engines") or []) if e.get("plan_type")), None
    )
    if plan_type:
        meta["plan_type"] = plan_type
    return meta


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
        # Which run record this process owns. Remote keeps one identity per grid, so it is always
        # `run_records.REMOTE_IDENTITY` in production — but the record-writing bookkeeping below (the
        # reload error, issue 10's service truth, and the codex quota snapshot) needs it by name, and
        # only the entrypoint knows it.
        engine_id: str = run_records.REMOTE_IDENTITY,
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
        # The record this process writes onto (`network_id` is the record dir's grid_id): both the
        # service-truth bookkeeping (issue 10) and the codex quota snapshot persist through it.
        self.engine_id = engine_id
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
        # Whether THIS process has written a `last_register_error` that still needs healing. In
        # memory so the healthy path never reads the record to discover there is nothing to clear —
        # the heartbeat runs every 30s for the life of the engine (issue 10). Both flags track what
        # actually LANDED on disk, not what was attempted: `_bookkeep` swallows write failures by
        # design, so trusting the attempt is how a lost write becomes permanent.
        self._register_error_noted = False
        self._registration_recorded = False
        # Local-engine reachability, folded into the heartbeat's `load` (ADR 0019). Keyed by engine
        # URL, not model, so a hot-reload that re-points a model can't carry a stale verdict onto a
        # different engine. Empty = nothing withheld, which is also what every failure path leaves.
        self._sweep = engine_health.SweepResult(health=engine_health.EngineHealth())
        self.stop = threading.Event()
        # Retires the distributed-tasks loop ALONE (ADR 0032). A second event, not a flag on
        # `stop`, because the two planes must be independently stoppable: a relay with no tasks
        # plane, or a task credential gone bad, retires task serving while inference keeps running,
        # and nothing on the task side may ever set `stop`. Teardown sets both.
        self.tasks_stop = threading.Event()
        self._lock = threading.Lock()  # guards the snapshot swap + token + inflight (short sections)
        self._register_lock = threading.Lock()  # serializes reload-register vs heartbeat-404 re-register
        self.reload_requested = threading.Event()  # SIGHUP sets this; the reload loop waits on it
        self._refresh_lock = threading.Lock()  # serializes refreshes WITHOUT blocking token() readers
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._inflight = 0
        self._jobs_held = 0   # claimed-but-unfinished jobs; the drain's signal (see `jobs_held`)
        # The codex seat's credential holder — OUTSIDE the snapshot (ADR 0015 D-d): a token
        # rotation must not rebuild routing. Unconditional; primed only when the record has a
        # codex spec (startup + reload), unprimed otherwise and inert.
        self.codex_seat = _CodexSeatHolder(stop=self.stop)
        # Latest codex seat rate-limit snapshot (parsed from a served response's x-codex-* headers, or
        # the start-up seed), guarded by _lock. Rides the heartbeat load so /grid/overview shows it
        # per-node. None until the first codex response/seed; a non-codex engine leaves it None.
        self._codex_quota: dict[str, Any] | None = None
        # Loopback URLs of this identity's CLI seats, read once at startup — the heartbeat asks
        # each for its allowance. Empty for an identity that serves no seat.
        self._seat_urls: list[str] = []
        # Decode rate (tokens/sec) of the most recently served request that could be timed, guarded
        # by _lock. None until this node has actually served one — the grid page then shows nothing,
        # which is the truth: a node that has answered nothing has no measured throughput. Deliberately
        # the LATEST sample rather than an average, so the figure tracks what the node is doing now.
        self._tok_s: float | None = None

    def set_codex_quota(self, quota: dict[str, Any]) -> None:
        """Record the seat's latest rate-limit snapshot; the next heartbeat carries it to the relay.
        Short section under _lock (no calls out), then persist OUTSIDE the lock."""
        with self._lock:
            self._codex_quota = quota
        # Persist to the engine's run record (remote.json) so the snapshot survives a respawn and can
        # be inspected — or hand-edited to simulate a tier/usage — for debugging. Best-effort and off
        # the lock: a telemetry write must never fail a served job. `network_id` IS the record's
        # grid_id, and `update_record` is a documented no-op once the record is gone — so a child
        # whose record was already reaped needs no guard of its own here.
        try:
            run_records.update_record(self.network_id, self.engine_id, codex_rate_limits=quota)
        except Exception as exc:  # noqa: BLE001 — persistence is optional, never fatal
            _warn(f"could not persist codex quota to the run record ({exc})")

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

    def last_sweep(self) -> engine_health.SweepResult:
        """The last probe round (ADR 0019) — its verdict, what it could not reach, and why.

        One value rather than three fields: the next round needs all of it (the verdict to fold
        into, the unchecked set to owe a probe to, and the errors already reported so a permanent
        one is not re-logged every 30s), and binding it once keeps those three consistent."""
        with self._lock:
            return self._sweep

    def health(self) -> engine_health.EngineHealth:
        """This box's current local-engine reachability verdict."""
        return self.last_sweep().health

    def apply_sweep(self, result: engine_health.SweepResult) -> None:
        """Swap in a freshly-probed round. One atomic rebind, like ``apply`` — the probing itself
        happens with the lock RELEASED, so a slow engine never blocks ``load()`` or ``token()``."""
        with self._lock:
            self._sweep = result

    def set_throughput(self, tok_s: float | None) -> None:
        """Record the decode rate of the request that just finished. Called from every poll worker,
        so it takes the lock; ``None`` (an unmeasurable job — media, a reply too short to time) is
        ignored rather than clearing, so one un-timeable request does not blank the gauge."""
        if tok_s is None or tok_s <= 0:
            return
        with self._lock:
            self._tok_s = float(tok_s)

    def throughput(self) -> float | None:
        with self._lock:
            return self._tok_s

    @staticmethod
    def _task_pause_stamp() -> str | None:
        """When this provider claims tasks again, as an ISO-8601 UTC string — or `None` while it is
        claiming now (ADR 0033 D-l, issue 19b).

        ISO 8601 and not a float, because the relay's half is a fail-open reader and a bare number
        would make it guess at units; every other timestamp on this wire is already a string.

        **Best-effort, exactly like the codex quota and the seat allowance beside it.** `load()` is
        what the heartbeat is built from, and a heartbeat that raises is a provider that TTLs out of
        the grid entirely — it would stop serving inference over a telemetry value about tasks. So
        the failure of a reading is the absence of a key, which is already the wire's "nothing to
        report".
        """
        try:
            paused_until = task_capacity.shared().paused_until()
            if paused_until is None:
                return None
            from datetime import datetime, timezone

            return datetime.fromtimestamp(paused_until, tz=timezone.utc).isoformat()
        except Exception as exc:  # noqa: BLE001 — telemetry must never fail a heartbeat
            _warn(f"could not read this provider's task-capacity pause ({exc}); the heartbeat is "
                  f"being sent without it")
            return None

    def load(self) -> dict[str, Any]:
        with self._lock:
            load = {"active_tasks": self._inflight}
            # Bound to the SAME snapshot the health verdict is keyed against, inside one lock hold, so
            # a concurrent reload swap can't pair one union's routes with another's verdict.
            withheld = engine_health.unhealthy_models(self._snapshot.routes, self._sweep.health)
            codex_quota = self._codex_quota
        # Emitted only when non-empty: absent is the wire's "nothing withheld", so a healthy box's
        # payload stays byte-identical to a pre-ADR-0019 build (ADR 0019, the polarity corollary).
        if withheld:
            load[engine_health.UNHEALTHY_LOAD_KEY] = withheld
        # VRAM/GPU load for the grid page (per-provider VRAM roll-up). Probed OUTSIDE the lock — it
        # shells out to nvidia-smi / system_profiler (up to a few seconds); absent a GPU it returns {}.
        from shared.system import gpu, host

        load.update(gpu.load_snapshot())
        # OS/arch so the grid knows what a node runs: linux / macos-arm64 / macos-x86_64 / windows / other.
        load["platform"] = host.platform_kind()
        load.update(_disk_load())
        # Decode throughput measured on this node's OWN traffic (never a synthetic benchmark), absent
        # until a real request has been served — see `throughput.py`.
        tok_s = self.throughput()
        if tok_s is not None:
            load["tok_s"] = round(tok_s, 1)
        # Codex seat quota (when this engine serves a seat) — surfaced per-node on /grid/overview. The
        # value only changes on a served response / the join seed; the heartbeat just re-ships the last.
        if codex_quota:
            load["codex_rate_limits"] = codex_quota
        # CLI-seat allowance, for the relay's routing (`_quota_exhausted` / `_quota_headroom`).
        # Read from the seat's own cached view, so the heartbeat costs one loopback GET rather
        # than a probe. Best-effort: a seat that cannot answer simply reports nothing, and the
        # relay treats absent as unknown, which routes exactly as it did before.
        seat_quota = self._seat_quota()
        if seat_quota:
            load["quota"] = seat_quota
        # When this provider's own Claude subscription lets it claim TASKS again (ADR 0033 D-l,
        # issue 19b). OUTSIDE `_lock`, with the other cross-module reads: `task_capacity.shared()`
        # carries its own lock, and taking ours around it would order two locks for no reason.
        #
        # Nothing the relay ROUTES on: a provider out of task headroom serves inference perfectly
        # well, which is the property `test_a_task_capacity_block_changes_nothing_the_relay_ROUTES_ON`
        # still pins. This is published so the members waiting on a queue can see why it is not
        # moving, and for nothing else.
        paused_until = self._task_pause_stamp()
        if paused_until:
            try:
                from . import tasks

                fully_paused = not tasks.has_non_claude_claim_capacity()
            except (Exception, SystemExit) as exc:
                # Telemetry fails open just like the timestamp parser: a profile-probe fault is not
                # evidence that every harness is withdrawn. The task loop still enforces Claude's
                # actual capacity gate locally.
                _warn(f"could not determine whether Codex task capacity remains available ({exc}); "
                      "the heartbeat is being sent without a full-provider pause")
                fully_paused = False
            if fully_paused:
                load[task_capacity.PAUSED_LOAD_KEY] = paused_until
        return load

    def _seat_quota(self) -> dict[str, Any] | None:
        """The tightest allowance across this identity's CLI seats, or None when it serves none.

        Tightest, not averaged: the identity stops serving as soon as ANY of its seats is spent,
        so the relay must see the seat that runs out first.
        """
        import httpx

        tightest = None
        for url in self._seat_urls:
            try:
                answer = httpx.get(f"{url}/quota", timeout=3.0).json()
            except Exception:  # noqa: BLE001 — a seat that cannot answer reports nothing
                continue
            if not answer.get("known"):
                continue
            entry = {"serving": bool(answer.get("serving", True)),
                     "headroom_pct": float(answer.get("headroom_pct", 100.0)),
                     "kind": answer.get("kind")}
            if tightest is None or entry["headroom_pct"] < tightest["headroom_pct"]:
                tightest = entry
            if not entry["serving"]:
                tightest = entry
                break
        return tightest

    def enter_inference(self) -> None:
        with self._lock:
            self._inflight += 1

    def exit_inference(self) -> None:
        with self._lock:
            self._inflight = max(0, self._inflight - 1)

    def enter_job(self) -> None:
        with self._lock:
            self._jobs_held += 1

    def exit_job(self) -> None:
        with self._lock:
            self._jobs_held = max(0, self._jobs_held - 1)

    def jobs_held(self) -> int:
        """Claimed jobs a worker has not finished with — what the teardown drain waits on.

        Deliberately NOT ``_inflight``. That one brackets the forward only and is published as
        ``active_tasks`` for the relay's busy accounting, so widening it would change routing. This
        brackets the whole of ``handle_job``, which is the real answer-owing window: routing, the
        endpoint gate and every ``_try_submit_error`` run BEFORE the forward, and each of those
        still owes the consumer a terminal response.

        And not the worker threads either: one parked in a long-poll holds no job and cannot be
        woken by ``stop``, so joining it only burns the shared budget."""
        with self._lock:
            return self._jobs_held

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
    # We are on the grid. Say so on the record, where the CLI can read it offline: "the process is
    # running" was never evidence of that, and the join gate believed it for a year (issue 10). The
    # outcome is remembered because this write must not be lost — see `_heartbeat_loop`'s retry.
    state._registration_recorded = _bookkeep(
        "registered_at", service_truth.mark_registered, state.network_id, state.engine_id
    )


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
        # The hardware fields ride along on every beat — only those, not the whole meta: name and
        # engine are already right from registration, while `chip`/`device` are the ones a node that
        # joined before they existed can never correct any other way. Cached, so this costs nothing.
        result = relay.heartbeat(
            state.signaling_url, token, load=state.load(), meta=node_hardware.meta_fields(),
        )
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
    process would otherwise be visible only in this log.

    ``run_records.mutate_record`` supplies the three properties this used to hand-roll — the record
    lock the CLI's join merge also takes (so an unlocked write here cannot lose a concurrent join's
    union, ADR 0010 F3), a no-op when a leave has already deleted the record, and no rewrite when
    nothing changed. Best-effort and never raises: a raise inside ``_reload_loop``'s except would kill
    the watcher, and one after a successful swap would mislabel the reload as failed."""

    def _set(record: dict[str, Any]) -> None:
        if message is None:
            record.pop("last_reload_error", None)
        else:
            record["last_reload_error"] = message[:300]  # a trace, not a transcript (never the key)

    _bookkeep("last_reload_error", run_records.mutate_record, state.network_id, engine_id, _set)


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
    # One budget for this reload's whole fan-out, and — unlike startup — NOT caught: exhausting it
    # refuses the reload (the exception reaches `_reload_loop`, which keeps the old routing and
    # records why). What makes that the right policy here is the state, not which spec: this node is
    # ALREADY registered and serving, so an all-False envelope would poison a live registration the
    # CLI has already reported as hot-reloaded, and it would stick until the next reload — the
    # opposite of `engine_health`'s rule that what a bounded sweep could not reach keeps its previous
    # verdict. (A spec only reaches the probe below when it is not already retained under that URL
    # with the same first advertised model — a newly added engine, or one whose model list changed at
    # the front; see the branch comment below, which this must not contradict.)
    deadline = bringup.probe_deadline()
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
                                 model_caps=spec.get("model_caps"), deadline=deadline)
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


def _hardware_serves_responses(snap: _Snapshot, model: str | None, target: str) -> bool:
    """Whether the hardware engine at ``target`` serves the Responses dialect — read from the per-model
    ``endpoints`` list the probe wrote into the caps envelope (issue 08), so the engine-side gate and the
    relay's candidate filter can never disagree about who serves the dialect.

    The capability is a property of the ENGINE, not the model (ADR 0018 — probed once per engine, stamped
    on every model it advertises). Normally ``model`` matches an advertised name and its entry answers
    directly. But ``route_and_kind``'s single-engine fallback resolves an unmatched/missing ``model`` onto
    the sole engine (as chat does), and that model has no caps entry — so on a miss, resolve the answer
    from ANY model routed to the same ``target`` (all carry the identical per-engine answer). Without this
    a single-engine grid would refuse a responses job with "unsupported endpoint" even though its one
    engine serves the dialect — the misleading-error class ADR 0017/0018 exist to prevent.

    Defensive: a non-dict entry or a non-list ``endpoints`` reads as "no" (fail closed), never raises."""
    models = snap.capabilities.get("models") or {}
    entry = models.get(model)
    if not isinstance(entry, dict):
        # The routed model has no caps entry (single-engine fallback on an unmatched/missing model):
        # consult the ENGINE via any model routed to the same target — the probe stamped one answer on all.
        for advertised_model, url in snap.routes.items():
            if url == target and isinstance(models.get(advertised_model), dict):
                entry = models[advertised_model]
                break
    endpoints = entry.get("endpoints") if isinstance(entry, dict) else None
    return isinstance(endpoints, list) and _RESPONSES_ENDPOINT in endpoints


def _served_endpoints(
    api_kind: str | None, *, hardware_serves_responses: bool = False,
) -> tuple[str, ...] | frozenset[str]:
    """The relay endpoints the routed engine serves. An API kind keeps ADR 0015 D-b's per-KIND matrix:
    it serves exactly its whitelist row's endpoints (openai ⇒ chat/completions, codex ⇒ responses),
    degrading to the ``ApiWhitelist`` default — chat-only, never the hardware pair — for a kind edited
    out of the catalog between join and respawn (the same posture as ``_static_api_caps``).

    Hardware (``api_kind`` is None) is now per-ENGINE, not a fixed property of being hardware (ADR 0018
    decision 1): the closed ``_ALLOWED_ENDPOINTS`` chat pair, plus the authored ``responses`` literal
    composed on ONLY when this engine's join-time probe found the route (``hardware_serves_responses``).
    ``_ALLOWED_ENDPOINTS`` itself is never widened — the served set stays a closed set of fixed literals
    checked before any URL is built (decision 3)."""
    if not api_kind:
        if hardware_serves_responses:
            return _ALLOWED_ENDPOINTS | {_RESPONSES_ENDPOINT}  # frozenset | set → frozenset; the closed set is unchanged
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
    # A caller that spoke Anthropic all the way in gets answered in Anthropic all the way out: the
    # poll payload carries which wire the request came in on, and a seat whose catalog row
    # declares `messages` (the claude seat) is posted to THAT route instead of `chat/completions`.
    # Any other kind never sees this flip — nothing here changes for openai/codex.
    wire_format = job.get("wire_format") or "openai"

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
    # Endpoint gate, AFTER routing where the engine is known. API kinds keep ADR 0015 D-b's per-KIND
    # matrix (codex ⇒ responses only, openai ⇒ chat/completions); HARDWARE is now per-ENGINE (ADR 0018
    # decision 1) — the chat pair plus `responses` iff this engine's join-time probe found the route,
    # read from the caps the probe stamped. A mismatch — including a job that arrived via the single-URL
    # fallback above — is refused with a structured error, never translated and never blind-forwarded.
    #
    # ADR 0018 decision 3 deliberately NARROWS the old anti-traversal statement (which read "`responses`
    # never enters `_ALLOWED_ENDPOINTS`, so the property is unchanged"): the literal now DOES enter the
    # set the gate consults for a capable hardware engine. This is not a loosening. The safety was never
    # that `responses` was absent — it is that the endpoint is checked against a CLOSED set of fixed
    # literals before it is used to build `f"{target_url}/{endpoint}"`. `_ALLOWED_ENDPOINTS` stays a
    # frozenset untouched; the `responses` composed onto the hardware set is a literal authored in this
    # repo, and the probe decides only WHETHER it is included, never WHAT it is — so nothing from the
    # relay or the engine's wire answer reaches URL construction.
    served = _served_endpoints(
        api_kind,
        hardware_serves_responses=api_kind is None and _hardware_serves_responses(snap, model, target),
    )
    if endpoint not in served:
        if api_kind:
            _try_submit_error(
                state, txn,
                f"API engine {api_kind!r} serves {', '.join(served)} only (endpoint {endpoint!r} not served)",
            )
        else:  # don't forward an unknown path to the local engine (the pre-matrix behavior, kept verbatim)
            _try_submit_error(state, txn, f"unsupported endpoint: {endpoint!r}")
        return
    # Params the vendor is known to reject (GPT-5.x rejects `stop`; the codex seat rejects every
    # output-cap spelling and `temperature` — facts.md #7): refuse now, with the vendor's own error
    # shape, instead of forwarding to learn a static catalog fact. Runs on EVERY dialect an API kind
    # serves (issue 04), so the codex row's `unsupported_params` is an executable per-kind gate, not
    # advisory data — this is the layer that knows the seat's kind, which the relay (normalising
    # before it selects an engine) cannot. The refusal is the same `engine error 400: {"error": ...}`
    # string on both dialects; the relay renders it PER-DIALECT — chat's `{"error"/"detail": ...}`, or
    # a `response.failed` block for responses (`relay._responses_failure_block`, which lifts the
    # message that names the param) — so an app's error handling never forks on which layer caught it.
    # The relay still refuses the chat-dialect cap spellings on `responses` pre-queue (they are the
    # wrong dialect for every engine), so what this gate newly answers there is the seat's
    # `max_output_tokens` and `temperature`.
    if api_kind:
        unsupported = _api_unsupported_params(api_kind, body)
        if unsupported:
            _refuse_unsupported_api_params(state, txn, api_kind, unsupported)
            return
        # A stream-only seat is SSE-only, so a non-stream responses job is refused here (issue 05 AC7)
        # — the relay lifted its global stream rule precisely so this kind-aware gate answers it. Gated
        # on `kind_is_stream_only` (issue 06c), the SAME predicate the advertised `stream_only` trait is
        # sourced from, so the layer that refuses and the auto-router that routes around it can't
        # disagree, and a future stream-only kind inherits this refusal for free. Every other kind
        # serves a non-stream responses request (the forward block's whole-body arm).
        if api_catalog.kind_is_stream_only(api_kind) and endpoint == "responses" and not is_stream:
            _refuse_stream_only_seat(state, txn)
            return

    # Consumers address the model by its advertised name; an external engine behind ``--advertise-as``
    # only knows its real name, so rewrite the body's model before forwarding (a new dict — never
    # mutate the job). No mapping / already-equal → forward unchanged (built-in + single-engine paths).
    upstream_model = state.upstream_model(model, snap)
    forward_body = {**body, "model": upstream_model} if upstream_model and upstream_model != model else body
    # ... and an API vendor may spell the output-token cap differently from the grid's internal name
    # on the CHAT dialect (the responses dialect passes its own cap through — see the function).
    forward_body = _adapt_output_token_param(forward_body, api_kind, endpoint)

    state.enter_inference()
    try:
        if api_kind == api_catalog.CODEX_KIND:
            # The seat speaks SSE only, so the job's stream flag is ignored — always the
            # streaming forward, submitting whole event blocks (ADR 0015 D-e); headers come from
            # the live seat holder, never the snapshot (D-d).
            _forward_codex(state, txn, endpoint, forward_body, read_timeout, target)
        elif endpoint == "responses" and is_stream:
            # A STREAMING responses job served by a non-seat engine (openai, Phase 1). The shared
            # block-aligned forward RETURNS a non-200 rather than reporting it; unlike the seat's D-d
            # refresh an openai engine does NOT retry (ADR 0012, job-error-only) — so answer a failure
            # here with a terminal signal, or the consumer hangs. Kept off the chat forwards below,
            # which lack block alignment (the terminal event carrying usage would tear).
            failure = _forward_responses_stream(
                state, txn, endpoint, forward_body, read_timeout, target,
                headers=_forward_headers(state, target, snap),
            )
            if failure is not None:
                _warn_api_auth_failure(api_kind, failure.status)
                _try_submit_error(state, txn, f"engine error {failure.status}: {failure.text[:200]}")
        elif endpoint == "responses":
            # A NON-streaming responses job (issue 05). The vendor returns ONE whole response object,
            # so the dialect-agnostic whole-body forward serves it — block alignment is a streaming
            # concern and there is no stream here. `_forward_whole` already answers a non-200 with a
            # terminal signal, so the same caller obligation is met. The codex seat never reaches this
            # arm: it is stream-only and refuses a non-stream job at the per-kind gate above.
            _forward_whole(state, txn, endpoint, forward_body, read_timeout, target,
                           headers=_forward_headers(state, target, snap), api_kind=api_kind)
        elif is_stream:
            _forward_stream(state, txn, "messages" if wire_format == "anthropic" else endpoint,
                            forward_body, read_timeout, target,
                            headers=_forward_headers(state, target, snap), api_kind=api_kind,
                            measure=True)
        else:
            _forward_whole(state, txn, "messages" if wire_format == "anthropic" else endpoint,
                           forward_body, read_timeout, target,
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


def _codex_window(group: dict[str, str], face: str) -> dict[str, Any] | None:
    """One rate-limit face (``primary``/``secondary``) parsed from a flat ``suffix -> value`` map of
    a codex header group. Returns None when the face carries no numeric field (a free seat's
    ``secondary`` is all-empty), so the shape never advertises a window the vendor didn't report."""
    def _num(suffix: str) -> int | None:
        try:
            return int(group[f"{face}-{suffix}"])
        except (KeyError, ValueError, TypeError):
            return None
    used = _num("used-percent")
    win = _num("window-minutes")
    reset_at = _num("reset-at")
    reset_after = _num("reset-after-seconds")
    if used is None and win is None and reset_at is None and reset_after is None:
        return None
    return {
        "used_percent": used,
        "remaining_percent": (100 - used) if used is not None else None,
        "window_minutes": win,  # 43200=30d (free), 10080=7d (weekly, paid) — read it, never assume
        "reset_at": reset_at,
        "reset_after_seconds": reset_after,
    }


def _parse_codex_quota(headers: Any) -> dict[str, Any] | None:
    """The seat's rate-limit snapshot, parsed from a vendor response's ``x-codex-*`` headers.

    The subscription backend reports quota ONLY on response headers (never the body); the official
    Codex client reads this same set. Free seats carry a single ``primary`` window + ``credits``;
    paid tiers add per-model sub-limits under a dynamic prefix (``x-codex-bengalfox-*`` etc.), so we
    group every non-main header by its leading name rather than cherry-picking a fixed list. Returns
    None when no ``x-codex-*`` header is present, so a non-codex or header-less response never clobbers
    a good snapshot. The raw headers ride along under ``headers`` so nothing is lost to the parse."""
    raw = {k.lower(): v for k, v in headers.items() if k.lower().startswith("x-codex-")}
    if not raw:
        return None
    fields = {k[len("x-codex-"):]: v for k, v in raw.items()}
    _MAIN = ("primary-", "secondary-", "plan-type", "active-limit", "credits-")
    main: dict[str, str] = {}
    subs: dict[str, dict[str, str]] = {}
    for key, val in fields.items():
        if key.startswith(_MAIN):
            main[key] = val
        else:  # a named sub-limit, e.g. "bengalfox-primary-used-percent" -> name "bengalfox"
            name, _, suffix = key.partition("-")
            subs.setdefault(name, {})[suffix] = val

    def _truthy(val: str | None) -> bool | None:
        return None if val is None else str(val).strip().lower() in ("true", "1", "yes")

    return {
        "plan_type": main.get("plan-type"),
        "active_limit": main.get("active-limit"),
        "primary": _codex_window(main, "primary"),
        "secondary": _codex_window(main, "secondary"),
        "credits": {
            "balance": main.get("credits-balance") or None,
            "has_credits": _truthy(main.get("credits-has-credits")),
            "unlimited": _truthy(main.get("credits-unlimited")),
        },
        "sublimits": {
            name: {
                "limit_name": grp.get("limit-name"),
                "primary": _codex_window(grp, "primary"),
                "secondary": _codex_window(grp, "secondary"),
            }
            for name, grp in subs.items()
        },
        "headers": raw,
    }


def _capture_codex_quota(state: "_ServeState", headers: Any) -> None:
    """Stash the seat's quota snapshot from a response's headers onto the serve state, so the next
    heartbeat carries it to the relay (surfaced per-node on /grid/overview). Best-effort: a telemetry
    read must never fail a served job, and a header-less response leaves the last snapshot intact."""
    try:
        quota = _parse_codex_quota(headers)
    except Exception:  # noqa: BLE001 — telemetry must not break the forward path
        return
    if quota:
        state.set_codex_quota(quota)


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


@dataclass(frozen=True)
class _UpstreamFailure:
    """One engine's non-200 on the responses path, drained and bound so it outlives the response
    context. `headers` is carried alongside `status` because the operator taxonomy needs it — a
    Cloudflare-challenge 403 and an auth 403 demand OPPOSITE actions and cannot be told apart from
    the status int alone (ADR 0015 D-f).

    `headers` is typed structurally rather than as `httpx.Headers`: a lookup is all any reader does,
    and this record is meant to stay kind-agnostic, so it does not take a dependency on the client
    library. It holds a live mapping and so is NOT hashable — never put one in a set or dict key."""

    status: int
    headers: Mapping[str, str]
    text: str


def _forward_responses_stream(
    state: _ServeState, txn: str, endpoint: str, body: dict[str, Any], read_timeout: float,
    target_url: str, headers: dict[str, str],
    on_headers: Callable[[Any], None] | None = None,
) -> _UpstreamFailure | None:
    """Forward one responses job and stream the reply back as whole event blocks.

    The dialect's streaming submission, with NO engine-kind knowledge — any kind that serves
    `responses` can call it (PRD §5 / ADR 0018). Block alignment is the point, and it is a property
    of the dialect rather than of any one backend (ADR 0015 D-e, kept verbatim): one submitted chunk
    is one whole `event:`+`data:` block, so the terminal event that carries the usage is never torn
    across two submissions, and an engine dying mid-stream strands only complete events at the relay.

    Credentials are the CALLER's business — whatever `headers` says is what goes on the wire, so a
    seat resolving a rotating bearer per attempt and an API engine carrying a static key are the
    same code here. A streamed 401 from the RELAY re-raises out of `_submit_response` and
    `handle_job`'s guard reports it — the same terminal-signal guarantee `_forward_stream` has.

    Returns None once a 200 has been submitted. A non-200 is drained inside the response context and
    returned, with BOTH contexts closed first: how to answer it differs by kind (the seat refreshes
    and retries once, D-d; an API engine does not, ADR 0012), and deciding out here means no vendor
    connection is held open through a ≤15s token exchange.

    **The caller MUST answer a non-None return with a terminal signal** — `_try_submit_error`, or a
    retry that reaches one. Nothing has been submitted for the job at that point, and a dropped
    return is invisible to `handle_job`'s `except Exception` guard, so the consumer would hang until
    it timed out: exactly the outcome `_submit_response` and `_try_submit_error` exist to prevent.
    """
    import httpx

    timeout = httpx.Timeout(connect=10, read=read_timeout, write=None, pool=10)
    with httpx.Client(timeout=timeout) as client:
        with client.stream(
            "POST", f"{target_url}/{endpoint}", json=body, headers=headers,
        ) as resp:
            # Quota/rate-limit headers ride EVERY response and arrive before the body — read them here
            # (engine-agnostic hook; the codex path uses it to harvest x-codex-*), whatever the status.
            if on_headers is not None:
                on_headers(resp.headers)
            if resp.status_code == 200:
                # Metered like the chat stream, and reachable by every kind that serves the dialect —
                # an openai engine, a local engine whose `/responses` probe succeeded, and the codex
                # seat, which reaches this function through `_forward_codex`. The blocks are already
                # whole events here, but the meter re-splits on newlines regardless, so it needs no
                # knowledge of which forward it is wrapping.
                meter = throughput.StreamMeter()
                _submit_response(
                    state, txn, stream=True,
                    content=_traced_stream(txn, _iter_event_blocks(meter.measure(resp.iter_bytes()))),
                )
                meter.flush()
                state.set_throughput(meter.tok_s)
                return None
            resp.read()  # drain inside the context so .text is readable after it closes
            return _UpstreamFailure(resp.status_code, resp.headers, resp.text)


def _forward_codex(
    state: _ServeState, txn: str, endpoint: str, body: dict[str, Any], read_timeout: float,
    target_url: str,
) -> None:
    """Forward one responses job to the seat — the seat's own credential behaviour, wrapped around
    the shared responses forward.

    Always the streaming path, whatever the job's stream flag says — the upstream only speaks SSE
    (ADR 0015 D-e). What is genuinely the SEAT's and stays here: the bearer is resolved from the
    seat holder PER ATTEMPT (D-d: outside the routing snapshot, so a rotation needs no reload), an
    upstream 401 refreshes and retries exactly once, codex-scoped — openai keeps ADR 0012's
    job-error-only — and the CF-403-vs-auth-403 operator taxonomy (D-f). Block alignment is NOT a
    seat concern and lives in `_forward_responses_stream`, where any kind can reach it.

    The refresh still runs OUTSIDE the response context — now by construction rather than by care:
    the shared forward returns a failure only after draining it and closing both contexts, so no
    vendor connection is held through a ≤15s token exchange, and the warning path still sees the
    response headers (CF-403 vs auth-403), not just the status int.
    """
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
        failure = _forward_responses_stream(
            state, txn, endpoint, body, read_timeout, target_url,
            headers=_codex_headers(bundle),
            # Harvest the seat's quota off this response's x-codex-* headers, so the next heartbeat
            # carries it to /grid/overview. Best-effort inside — never fails the served job.
            on_headers=lambda h: _capture_codex_quota(state, h),
        )
        if failure is None:
            return  # submitted — the shared path already gave the job its terminal signal
        if failure.status == 401 and attempt == 1 and state.codex_seat.refresh(bundle.access_token):
            continue  # rotated — retry once with the fresh bearer (reactive D-d)
        _warn_codex_upstream(failure.status, failure.headers)
        _try_submit_error(state, txn, f"engine error {failure.status}: {failure.text[:200]}")
        return


def _disk_load() -> dict[str, float]:
    """``disk_total_gb`` / ``disk_used_gb`` for the volume holding this node's model store.

    Measured at ``paths.models_dir()``, not at ``~``: a box with its models on a separate NVMe would
    otherwise report the home volume, which is not the space the next `grid pull` consumes. Walks up
    to the first directory that exists, since the store is created lazily and a node can heartbeat
    before it has pulled anything.

    The measurement itself is `host.disk_gb`, which carries this repo's psutil→``statvfs`` ladder —
    psutil is optional here, so calling it directly would report nothing on every box without it.
    One ``statvfs`` per heartbeat, which is why nothing is memoized: unlike the GPU probe there is no
    process to spawn, and caching the total would still leave usage needing the same call.

    Returns ``{}`` on any failure — a node that cannot stat its own volume reports no disk rather
    than zeros, which the grid page would render as a full drive.
    """
    try:
        from shared import paths
        from shared.system import host

        target = paths.models_dir()
        while not target.exists() and target != target.parent:
            target = target.parent
        measured = host.disk_gb(str(target))
    except Exception:  # noqa: BLE001 — telemetry must never fail a heartbeat
        return {}
    if measured is None:
        return {}
    total_gb, used_gb = measured
    return {"disk_total_gb": total_gb, "disk_used_gb": used_gb}


def _forward_whole(
    state: _ServeState, txn: str, endpoint: str, body: dict[str, Any], read_timeout: float, target_url: str,
    headers: dict[str, str], api_kind: str | None = None,
) -> None:
    import httpx

    timeout = httpx.Timeout(connect=10, read=read_timeout, write=30, pool=10)
    started = time.monotonic()
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(f"{target_url}/{endpoint}", json=body, headers=headers)
    if resp.status_code != 200:
        _warn_api_auth_failure(api_kind, resp.status_code)
        _try_submit_error(state, txn, f"engine error {resp.status_code}: {resp.text[:200]}")
        return
    # Every caller of this forward is a text dialect (chat, anthropic /messages, non-stream
    # responses) — media never reaches here — so the reply always has a usage object to read, in one
    # spelling or another. Measured before the submit so a relay-side failure doesn't cost the sample.
    state.set_throughput(throughput.whole_body_tok_s(resp.content, time.monotonic() - started))
    _submit_response(state, txn, content=resp.content, stream=False)


def _forward_stream(
    state: _ServeState, txn: str, endpoint: str, body: dict[str, Any], read_timeout: float, target_url: str,
    headers: dict[str, str], api_kind: str | None = None, measure: bool = False,
) -> None:
    """Forward one streamed job, passing the engine's SSE bytes through verbatim.

    ``measure`` times the decode rate for the node's `tok_s`. It is opt-in per call site rather than
    always-on because THIS function also carries media: a ComfyUI reply is a multi-megabyte base64
    image in a single unbroken SSE line, and a meter hunting that line for a newline would buffer the
    whole image. Media has no tokens to count anyway, so the media call site simply leaves it off and
    the meter never sees those bytes.
    """
    import httpx

    timeout = httpx.Timeout(connect=10, read=read_timeout, write=None, pool=10)
    meter = throughput.StreamMeter() if measure else None
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
            chunks = engine_resp.iter_bytes()
            if meter is not None:
                chunks = meter.measure(chunks)
            _submit_response(state, txn, content=_traced_stream(txn, chunks), stream=True)
            _debug(f"stream txn={txn} submit_response returned (relay accepted the full stream) t={time.time():.3f}")
    # After the submit returned, so a stream that died mid-flight (which re-raises out of
    # `_submit_response`) records nothing rather than a rate computed from a truncated reply.
    if meter is not None:
        meter.flush()
        state.set_throughput(meter.tok_s)


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
        # Mark the job held for the WHOLE of handle_job, not just its forward: everything before the
        # forward — routing, the endpoint gate, each `_try_submit_error` — still owes this consumer a
        # terminal response, and the teardown drain waits on this counter (see `_ServeState.jobs_held`).
        state.enter_job()
        try:
            handle_job(state, job)
        except Exception as exc:  # defence in depth: handle_job already guards, but never die here
            print(f"\nUnexpected error handling a job: {exc!r}", file=sys.stderr)
        else:
            if _DEBUG:
                txn = job.get("transaction_id")
                model = (job.get("body") or {}).get("model")
                _debug(f"poll: job txn={txn} model={model!r} handled in {time.monotonic() - started:.2f}s")
        finally:
            # `finally`, not the `else`: a job that raised is finished with too, and leaking the
            # count would make every later drain spend its whole budget waiting on a ghost.
            state.exit_job()


def _maybe_refresh_codex(state: _ServeState) -> None:
    """The heartbeat's proactive-rotation hook (ADR 0015 D-d). NEVER raises: `_heartbeat_loop`
    runs under `_supervise`, which stops the WHOLE engine on any escaping exception — a refresh
    bug must degrade to a warn, not an engine stop. `SystemExit` included: the store's TOML loader
    raises it for a corrupt file (the house daemon-thread hazard, same as `_reload_loop`)."""
    try:
        state.codex_seat.maybe_refresh(int(time.time()))
    except (Exception, SystemExit) as exc:
        _warn(f"codex proactive refresh failed unexpectedly (engine unaffected): {exc!r}")


def _maybe_probe_engines(state: _ServeState) -> None:
    """One local-engine reachability round, folded into the next heartbeat's load (ADR 0019).

    Runs AFTER the heartbeat rather than before it, so a slow engine delays the *verdict* by one
    tick instead of delaying the heartbeat itself: a late heartbeat costs the node its 120s TTL and
    unlists everything it serves, a late verdict costs one more tick of a stale advertisement.

    NEVER raises, for the same reason as `_maybe_refresh_codex`: `_heartbeat_loop` runs under
    `_supervise`, which stops the WHOLE engine on any escaping exception — so a bug in an advisory
    health probe must not take a working provider offline. `SystemExit` included (the house
    clean-error idiom / daemon-thread hazard). On any fault the previous verdict stands, and the
    safe direction of a missing verdict is "withhold nothing".
    """
    try:
        snap = state.snapshot()  # bind once — route + kind must come from the SAME union
        urls = engine_health.probe_urls(snap.routes, snap.api_kind_by_url)
        previous = state.last_sweep()
        # Whatever last round could not check goes first, so a fixed route order can never starve
        # the same engine every tick and freeze its verdict (an unrestorable withdrawal).
        ordered = engine_health.prioritize(urls, previous.unchecked)
        result = engine_health.sweep(ordered, previous.health, should_stop=state.stop.is_set)
        state.apply_sweep(result)
        for line in engine_health.transitions(previous.health, result.health, snap.routes):
            _warn(line)
        if result.skipped:  # a partial sweep must never read as a clean one
            _warn(f"engine health probe ran out of time; not checked: {', '.join(result.skipped)}")
        # Errors on the CROSSING only, like `transitions`: a malformed URL never fixes itself and is
        # re-probed first every round, so a per-tick line would bury the withdrawals that matter.
        already = {url for url, _exc in previous.errors}
        for url, exc in result.errors:
            if url not in already:
                _warn(f"engine health probe could not check {url}: {exc}")
    except (Exception, SystemExit) as exc:
        _warn(f"engine health probe failed unexpectedly (engine unaffected): {exc!r}")


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
            # That line is inside a detached child's log. Put the reason where the CLI can read it
            # too: the operator's next move is a `grid join`, which must not answer "Already serving"
            # while this box is off the grid (issue 10).
            # `or` keeps the flag set when THIS write is the one that failed: an accusation an
            # earlier beat did land still needs healing.
            state._register_error_noted = _bookkeep(
                "last_register_error", service_truth.note_register_error,
                state.network_id, state.engine_id, str(exc) or repr(exc),
            ) or state._register_error_noted
        else:
            _debug(f"heartbeat: ok ({result})")
            # The relay heard from us — the one fact the join gate's freshness check is about. Touched
            # only here, on success: freshening it after a failed beat would launder an unreachable
            # engine into a healthy-looking one.
            _bookkeep("heartbeat sidecar", service_truth.touch_heartbeat,
                      state.network_id, state.engine_id)
            # Retry the registration stamp until it is durable. It is the one fact here that fails
            # CLOSED — absent reads as a confident "has not registered" — so a single swallowed write
            # would leave this engine accused for as long as it runs, and the operator's natural
            # response (re-running the same join) is the read-only no-op path that can never heal it.
            if not state._registration_recorded:
                state._registration_recorded = _bookkeep(
                    "registered_at", service_truth.mark_registered,
                    state.network_id, state.engine_id,
                )
            # Heal, without a disk read on every healthy tick — and only clear the flag once the heal
            # actually landed, or a failed heal would strand a stale, undatable accusation forever.
            if state._register_error_noted and _bookkeep(
                "last_register_error", service_truth.note_register_error,
                state.network_id, state.engine_id, None,
            ):
                state._register_error_noted = False
        # On EVERY surviving tick — including a failed relay call (the relay being unreachable
        # says nothing about the vendor), so an idle grid behind a flaky relay still rotates.
        _maybe_refresh_codex(state)
        # Likewise every tick, and likewise unconditional: this loop IS the health probe's rate
        # limit (one round per engine per HEARTBEAT_INTERVAL), so there is no second interval to
        # keep in sync — and an idle grid, where no job failure will ever expose a dead engine, is
        # exactly the case this exists for (ADR 0019).
        _maybe_probe_engines(state)
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


def _start_task_worker(state: _ServeState) -> list[threading.Thread]:
    """Start the distributed-tasks claim loops, if enabled. Returns the threads that started.

    Deliberately NOT wrapped in ``_supervise`` — the same exemption ``_reload_loop`` has, for the
    same reason. ``_supervise`` sets ``state.stop`` on any fault, which is right for a poll worker
    (a dead one strands advertised inference capacity) and wrong here: a fault in task serving must
    leave inference running. ``task_loop`` self-guards and owns its own retirement signal.

    Not joined on drain either. A task runs for minutes; the drain budget is 5s and belongs to
    in-flight inference jobs. The thread is a daemon, so it cannot outlive the process.

    Guarded end to end, because this call runs on the MAIN thread and BEFORE `_serve_loop`'s own
    try/finally: an unguarded fault here — an import error in `remote/tasks.py`, `RuntimeError:
    can't start new thread` under exhaustion — would escape to the top-level handler, unregister the
    node and kill the process. Every other fault in this plane is isolated; a startup fault must be
    too, or the isolation is only true once the thread already exists.

    Each worker is guarded on its OWN, so a machine that runs out of threads part way through keeps
    the ones it managed to start. Task serving is retired only when NONE started, because that is the
    only case where nothing is claiming and saying so is the honest record.

    Nothing is passed to `task_loop`: it finds the process-wide capacity gate itself, which is what
    makes every worker throttle on ONE reading of the one subscription they all spend.
    """
    try:
        if not task_opt_in.serving_enabled():
            return []
        from . import tasks

        count = task_opt_in.worker_count()
    except (Exception, SystemExit) as exc:
        print(f"\nCould not start task serving ({exc!r}); inference is unaffected.", file=sys.stderr)
        state.tasks_stop.set()
        return []

    threads: list[threading.Thread] = []
    for index in range(count):
        try:
            thread = threading.Thread(
                target=_supervise_tasks, args=(tasks.task_loop, state), daemon=True,
                name=f"task-worker-{index + 1}")
            thread.start()
            threads.append(thread)
        except (Exception, SystemExit) as exc:
            print(f"\nCould not start task worker {index + 1} of {count} ({exc!r}); "
                  f"inference is unaffected.", file=sys.stderr)
    if not threads:
        state.tasks_stop.set()
    return threads


def _supervise_tasks(loop: Callable[[_ServeState], None], state: _ServeState) -> None:
    """`_supervise` for the task loop: just as loud, but it retires task serving instead of the engine.

    ``task_loop`` guards its own body, so arriving here means a fault outside that guard. Letting it
    escape would drop the thread into the default excepthook and leave task serving silently dead
    for the life of the process — the failure ``_supervise`` was written to prevent. The difference
    is only which stop event is set: ``tasks_stop``, never ``state.stop``, so a task-plane fault
    cannot take inference down with it.
    """
    try:
        loop(state)
    except BaseException as exc:  # noqa: BLE001 — a loop-level fault must fail loud, not vanish
        from . import tasks  # imported here for the same reason the caller does: keep it off startup

        print(f"\n{threading.current_thread().name} stopped unexpectedly: {exc!r}. "
              f"{tasks.RESUME_HINT}", file=sys.stderr)
        traceback.print_exc()
        state.tasks_stop.set()


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
    _start_task_worker(state)
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
        state.tasks_stop.set()  # retire task serving with the engine, not only on its own signal
        deadline = time.monotonic() + _DRAIN_TIMEOUT
        # Wait on in-flight JOBS, never on the worker threads. A worker parked in a long-poll cannot
        # be woken by `state.stop` and does not return for up to `relay.POLL_TIMEOUT` (35s) — far past
        # this whole budget — so joining workers in list order let the first parked one (workers are
        # idle almost always) spend the entire deadline, leaving `join(timeout=0.0)` for a worker
        # genuinely mid-`handle_job` and for the heartbeat/reload joins below. The counter is the
        # honest signal: it covers exactly the window from claim to submit.
        while state.jobs_held() and time.monotonic() < deadline:
            time.sleep(0.02)
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
        # Two different facts, deliberately two different lines. An abandoned PARKED poller costs
        # nobody an answer; only a job still unsubmitted does. Reporting every live thread as the
        # latter (what this used to do) reads as fleet-wide data loss on every ordinary restart and
        # sends an operator hunting a bug that isn't there.
        unfinished = state.jobs_held()
        if unfinished:
            print(
                f"\n{unfinished} job(s) still in flight after {_DRAIN_TIMEOUT}s drain — "
                f"abandoning; their consumers may see no terminal response.",
                file=sys.stderr,
            )
        # Subtract the job holders: they are alive too, and calling them "parked, holding no job"
        # would be the same overstatement in miniature. Counted, not named — pollers are
        # interchangeable, so twenty thread names are a wall of text with no diagnostic value
        # (unlike the heartbeat/reload lines below, where the name IS the finding).
        parked = max(0, sum(1 for worker in workers if worker.is_alive()) - unfinished)
        if parked:
            print(
                f"\n{parked} poll worker(s) abandoned while parked in a long-poll — they hold no job.",
                file=sys.stderr,
            )
        if heartbeat.is_alive():
            # The heartbeat is joined BEFORE the reload thread against the same deadline, so a
            # heartbeat that overruns is also what ate the reload thread's turn below. Name it, or
            # an operator reading the next line has no way to know what consumed the budget.
            print(f"\nHeartbeat thread still running after {_DRAIN_TIMEOUT}s drain — it held the "
                  f"shared teardown budget; anything joined after it got less of one.", file=sys.stderr)
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
