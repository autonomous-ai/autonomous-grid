"""The internet provider serve loop — what the detached ``__internet-engine`` subprocess runs.

It mirrors the LAN engine loop (`cli/provider.py:_run_engine`) but, instead of being forwarded
inbound requests by a grid proxy, it **polls** the hosted relay for work: bring the engine up
through the shared engine layer, probe its capabilities, register them with the relay, then loop
``poll → forward to the local engine → submit result`` while a heartbeat thread keeps the node
live. The per-grid ``access_token`` authenticates every relay call and is refreshed on a 401.

Ported from ``grid-src/grid_cli/provider_runtime/provider/poll_worker.py`` (the threading reworked
into a small ``_ServeState`` + testable units). Engine bring-up + the run record + teardown are
shared with LAN; only this loop differs (DECISIONS D17). Secrets stay in ``credentials.toml`` — the
run record never carries a token.
"""
from __future__ import annotations

import base64
import json
import signal
import sys
import threading
from typing import Any

from internet import control_plane, credentials, probe, relay
from shared import run_records


# Engine read budget when the relay doesn't advertise one (older relay); matches its default.
_DEFAULT_INFERENCE_TIMEOUT = 600.0

# The relay-supplied endpoint is interpolated into the local engine URL, so only forward known text
# endpoints — this stops a buggy or compromised relay from probing other local paths via `../`.
# (Media is a later slice and is rejected with its own message.)
_ALLOWED_ENDPOINTS = frozenset({"chat/completions", "completions"})


# ---------------------------------------------------------------------------
# Detached entry
# ---------------------------------------------------------------------------

def run_internet_engine_from_record(grid_id: str, engine_id: str) -> int:
    """Detached ``__internet-engine`` entry: serve one engine to the grid's relay until SIGTERM."""
    record = run_records.read_record(grid_id, engine_id)
    if not record:
        raise SystemExit(f"No engine record for {engine_id} on {grid_id}.")
    network_id = record["grid_id"]  # the run record's grid_id IS the internet network_id
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

    launched: list[Any] = []
    launcher = None
    state = None
    rc = 0
    try:
        engine_results, launched, launcher = _bring_up_engines(record)
        routes, union_models, capabilities, warnings = _build_routing(engine_results)
        for line in warnings:  # surface shadowed-duplicate models so routing isn't a silent surprise
            print(line, file=sys.stderr)
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
        )
        register(state)
        print(f"Engine {state.node_id} serving {union_models} via the relay at {signaling_url}")
        print("Send SIGTERM (grid leave) to unregister.")
        heartbeat = threading.Thread(target=_heartbeat_loop, args=(state,), daemon=True)
        heartbeat.start()
        _poll_loop(state)  # blocks until the loop stops or SIGTERM
    except KeyboardInterrupt:
        print("\nEngine unregistered.")
    except (Exception, SystemExit) as exc:  # detached top level: report, tear down, exit non-zero
        print(f"Internet engine stopped: {exc}", file=sys.stderr)
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
    return rc


# ---------------------------------------------------------------------------
# Engine bring-up (shared layer, mirrors cli/provider._run_engine)
# ---------------------------------------------------------------------------

def _bring_up_engines(
    record: dict[str, Any],
) -> tuple[list[tuple[str, list[str], dict[str, Any]]], list[Any], Any]:
    """Bring up every engine the record lists and probe each (mirrors cli/provider._run_engine).

    Returns ``(engine_results, launched, launcher_module)`` where ``engine_results`` is
    ``[(llm_url, advertised_models, caps_envelope), ...]`` in record order — fed to ``_build_routing``.
    ``launched`` collects the built-in llama-servers to stop on teardown (empty when every engine is
    external). Only a built-in ``--serve`` launches, and only as the **sole** engine: ``grid join
    --all`` gathers already-running engines, so a multi-engine record is all external URLs.
    """
    specs = record.get("engines") or [_flat_spec(record)]
    aliases = list(record.get("advertise_as") or [])
    if len(specs) > 1 and any(not spec.get("endpoint_url") for spec in specs):
        raise SystemExit("Serving several engines needs external endpoints; the built-in engine serves one model.")

    results: list[tuple[str, list[str], dict[str, Any]]] = []
    launched: list[Any] = []
    launcher_mod = None
    try:
        for spec in specs:
            llm_url, proc, mod, advertised = _bring_up_one(spec, record, aliases)
            if proc is not None:
                launched.append(proc)
                launcher_mod = mod
            caps = probe.capabilities(llm_url, advertised[0]) if advertised else {}
            results.append((llm_url, advertised, caps))
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
) -> tuple[str, Any, Any, list[str]]:
    """Resolve one engine's URL, launching the built-in llama-server for ``--serve``.

    Returns ``(llm_url, launched, launcher_module, advertised_models)``. For an external engine
    nothing is launched (``launched``/``launcher`` are ``None``). Launch tuning (port, ctx, …) comes
    from the record's top-level fields — only the single built-in path consumes them.
    """
    models = list(spec.get("models") or [])
    advertised = _advertised_models(models, aliases)
    endpoint_url = spec.get("endpoint_url")
    if endpoint_url:  # external engine: forward to it, launch nothing
        return endpoint_url.rstrip("/"), None, None, advertised
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
    # The relay forwards to the engine on *this* box, so the loop reaches it on loopback.
    return f"http://127.0.0.1:{port}/v1", launched, launcher_mod, advertised


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
    engine_results: list[tuple[str, list[str], dict[str, Any]]],
) -> tuple[dict[str, str], list[str], dict[str, Any], list[str]]:
    """Merge several local engines into one internet identity's routing state (DECISIONS D9).

    ``engine_results`` is ``[(llm_url, models, caps_envelope), ...]`` in detect order. Returns
    ``(routes, union_models, merged_caps, warnings)``:

    - ``routes`` — ``{model: llm_url}``; the **first** engine to advertise a model wins (deterministic).
    - ``union_models`` — every advertised model once, in first-seen order (what the identity registers).
    - ``merged_caps`` — one ``{"schema_version": 1, "models": {...}}`` envelope, first-wins per model;
      ``{}`` when nothing probed (registers text-only, like the single-engine path).
    - ``warnings`` — one human line per shadowed duplicate, so the operator sees why a second engine's
      copy of a model is ignored.

    A failed probe degrades to ``{}`` upstream (``probe.capabilities``), so the caps merge reads
    ``env.get("models") or {}`` and never KeyErrors the whole table on one bad engine.
    """
    routes: dict[str, str] = {}
    union_models: list[str] = []
    merged_models: dict[str, Any] = {}
    warnings: list[str] = []
    for llm_url, models, caps in engine_results:
        caps_models = (caps or {}).get("models") or {}
        for model in models:
            if model in routes:
                warnings.append(
                    f"Two engines serve model {model!r}; routing it to the first ({routes[model]!r}) "
                    f"and ignoring {llm_url!r}."
                )
                continue
            routes[model] = llm_url
            union_models.append(model)
            if model in caps_models:
                merged_models[model] = caps_models[model]
    merged_caps = {"schema_version": 1, "models": merged_models} if merged_models else {}
    return routes, union_models, merged_caps, warnings


def _meta(record: dict[str, Any], engine_id: str) -> dict[str, Any]:
    """How the node appears on the grid page: name (from --name/engine_id) + engine kind label.

    A multi-engine identity shows the kinds it gathered (e.g. ``ollama+vllm``) when no explicit
    ``--engine-label`` was given, so the page reflects what is actually serving.
    """
    label = record.get("engine_label")
    if not label:
        kinds = [e.get("engine_label") for e in (record.get("engines") or []) if e.get("engine_label")]
        if kinds:
            label = "+".join(dict.fromkeys(kinds))
    return {"name": engine_id, "engine": label or ("external" if record.get("endpoint_url") else "llama.cpp")}


def _pricing(record: dict[str, Any]) -> dict[str, float]:
    pricing: dict[str, float] = {}
    if record.get("pricing_input") is not None:
        pricing["input_per_1k_tokens"] = float(record["pricing_input"])
    if record.get("pricing_output") is not None:
        pricing["output_per_1k_tokens"] = float(record["pricing_output"])
    return pricing


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
    ) -> None:
        self.signaling_url = signaling_url
        self.node_id = node_id
        self.network_id = network_id
        self.llm_url = llm_url.rstrip("/")
        self.models = list(models)
        # model → local engine URL. Several engines may serve under one identity (DECISIONS D9); for
        # the single-engine case the map is derived so every advertised model points at the one engine.
        if routes is not None:
            self._routes = {model: url.rstrip("/") for model, url in routes.items()}
        else:
            self._routes = {model: self.llm_url for model in self.models}
        self.capabilities = dict(capabilities or {})
        self.meta = dict(meta or {})
        self.pricing = dict(pricing or {})
        self.max_concurrency = max(1, int(max_concurrency))
        self.stop = threading.Event()
        self._lock = threading.Lock()  # guards the token + inflight count (short critical sections)
        self._refresh_lock = threading.Lock()  # serializes refreshes WITHOUT blocking token() readers
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._inflight = 0

    def route(self, model: str | None) -> str | None:
        """The local engine URL serving ``model``.

        Exact match wins. Otherwise, when every model points at the **same single engine** (one
        distinct URL — even if that one engine serves several models), fall back to it: a job with a
        missing/unknown ``model`` still forwards as it did before multi-engine (the proxy forwarded the
        body unchanged, letting the engine answer). With several distinct engines and no match, return
        ``None`` so the caller reports "no engine serves" instead of guessing.
        """
        if model and model in self._routes:
            return self._routes[model]
        distinct = set(self._routes.values())
        if len(distinct) == 1:
            return next(iter(distinct))
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

def register(state: _ServeState) -> None:
    relay.register_node(
        state.signaling_url,
        state.token(),
        state.node_id,
        models=state.models,
        capabilities=state.capabilities or None,
        meta=state.meta or None,
        pricing=state.pricing or None,
        max_concurrency=state.max_concurrency,
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
        result = relay.heartbeat(state.signaling_url, token, load=state.load())
    except relay.RelayUnauthorized:
        if _allow_refresh and state.refresh(stale_token=token):
            return heartbeat_once(state, _allow_refresh=False)
        raise
    if result == "missing":
        register(state)
    return result


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

    if endpoint.startswith("media/"):  # internet media serving is a later slice
        _try_submit_error(state, txn, "media serving isn't available in internet mode yet")
        return
    if endpoint not in _ALLOWED_ENDPOINTS:  # don't forward an unknown path to the local engine
        _try_submit_error(state, txn, f"unsupported endpoint: {endpoint!r}")
        return

    model = body.get("model")  # body is already `job.get("body") or {}`, so it is a dict
    target = state.route(model)  # which local engine serves this model (DECISIONS D9)
    if target is None:
        _try_submit_error(state, txn, f"no engine serves model {model!r}")
        return

    state.enter_inference()
    try:
        if is_stream:
            _forward_stream(state, txn, endpoint, body, read_timeout, target)
        else:
            _forward_whole(state, txn, endpoint, body, read_timeout, target)
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
                state.signaling_url, state.token(), txn, content=engine_resp.iter_bytes(), stream=True
            )


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
            continue
        try:
            handle_job(state, job)
        except Exception as exc:  # defence in depth: handle_job already guards, but never die here
            print(f"\nUnexpected error handling a job: {exc!r}", file=sys.stderr)


def _heartbeat_loop(state: _ServeState) -> None:
    while not state.stop.is_set():
        try:
            heartbeat_once(state)
        except relay.RelayUnauthorized:
            # Auth is exhausted (refresh failed too) — stop now rather than spin re-failing until
            # the poll loop happens to notice, which can be up to a full long-poll away.
            print("\nHeartbeat token rejected and refresh is unavailable — stopping.", file=sys.stderr)
            state.stop.set()
            break
        except relay.RelayError as exc:
            print(f"\nHeartbeat error: {exc}", file=sys.stderr)
        state.stop.wait(relay.HEARTBEAT_INTERVAL)
