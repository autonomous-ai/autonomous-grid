"""HTTP client for a remote grid's hosted relay — the provider (engine) side of the serve loop.

The relay base is the grid's ``signaling_url``; a joined engine authenticates every call with its
per-grid ``access_token`` (Bearer). It registers its capabilities (``PUT /nodes/{node_id}``),
long-polls for work (``GET /relay/v1/poll``), posts each result back
(``POST /relay/v1/{response,error}/{txn}``), and heartbeats (``POST /nodes/heartbeat``).

A provider that also runs distributed tasks (ADR 0032) claims them on a SECOND, separate long-poll
(``POST /relay/v1/tasks/claim``) and reports terminal results to
``POST /relay/v1/tasks/{id}/result``. That plane shares this module's transport and credential and
nothing else — no queue, no reaper, no escrow in common with the inference path above.

Ported and trimmed from ``grid-src/grid_cli/provider_runtime/provider/{register,poll_worker,
heartbeat}.py``, repointed onto the in-repo ``signaling_url`` base (DECISIONS D11/ADR 0003). Unlike
``control_plane`` — which raises a ``SystemExit`` on any ``>=400`` — the relay layer maps status
codes so the *long-running* serve loop can refresh on 401, re-register on 404, and back off on a
transient error instead of dying. The serve loop (`remote/serve.py`) owns that orchestration; this
module is the stateless wire boundary.
"""
from __future__ import annotations

import sys
from typing import Any, Iterable, Iterator
from urllib.parse import quote

import httpx


# Long-poll window and heartbeat cadence (grid-src parity: well within the relay's 120s node TTL).
POLL_TIMEOUT = 35.0
HEARTBEAT_INTERVAL = 30
# How long to wait posting a result back. Streaming submits read indefinitely (write=None).
# A CLI-seat answer can take minutes to generate and is submitted whole, so 30s was losing work
# that had already been paid for: the seat logged 200 OK, the submit timed out, and the consumer
# waited forever. The engine has already spent the allowance by this point — the only thing a
# short timeout buys is throwing the result away.
_SUBMIT_TIMEOUT = 180.0
_REGISTER_TIMEOUT = 15.0

# Distributed tasks (ADR 0032). The claim long-poll must OUTLAST the relay's own claim window
# (`task_claim_timeout_seconds`, 30s) or the client gives up first and every idle cycle looks like a
# transport error — the same 30-vs-35 relationship `POLL_TIMEOUT` has with `poll_timeout_seconds`.
TASK_CLAIM_TIMEOUT = 35.0
# Reporting a terminal result is small and bounded, but it is the LAST word on work that has already
# been done: losing it means the task is retried from scratch. Generous on purpose.
_TASK_RESULT_TIMEOUT = 60.0
# Publishing progress is small, frequent, and DISPOSABLE — a lost event costs a line of output, not
# work. Tight on purpose: the publisher runs on the thread driving the child, so a slow relay must
# cost the task a moment, never minutes.
_TASK_EVENT_TIMEOUT = 15.0
# Following a task's stream, from the CLIENT side. The read phase is unbounded because that is the
# whole point — a task legitimately says nothing for minutes while a build runs, and silence is not
# evidence of death (ADR 0032 D-c). Connect stays bounded so an unreachable relay still fails fast.
_TASK_FOLLOW_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=5.0)
# Downloading the task's input. Longer than a result POST because the body is the whole repository
# rather than a status, and short enough that a stalled relay fails the attempt instead of eating
# the task's deadline before the agent has started.
_TASK_INPUT_TIMEOUT = 120.0
# Ceiling on the input bundle this provider will hold in memory. LOCKSTEP-ish with the relay's own
# `task_repo.MAX_BUNDLE_BYTES` — matched deliberately, but this side is the one that must hold: a
# provider protects itself rather than trusting the far end to have been configured the same way.
MAX_INPUT_BUNDLE_BYTES = 64 * 1024 * 1024

# Bring-up's own register deadline, stated phase by phase (ADR 0022). The read phase is the one that
# matters and the one a bare float never names: a relay that ACCEPTS the connection and then never
# answers — a master mid-respawn behind a proxy — is not covered by a connect timeout at all, and a
# bare float silently applies itself to all four phases *independently*, so "15s" was really up to
# 15s connecting and then 15s reading.
#
# Deliberately separate from `_REGISTER_TIMEOUT`, which keeps its bare float for the three callers
# that are NOT on the bring-up path: `unregister_node`, `deregister_node` — the CLI backstop on
# `grid leave`'s critical path, i.e. the very seam this branch exists to protect — and
# `_price_oneshot`. Widening a deadline is not free there, so it is not widened.
_SERVE_REGISTER_TIMEOUT = httpx.Timeout(connect=10.0, read=15.0, write=15.0, pool=5.0)

# `check_credential`'s deadline, and the tightest in this module — because of what happens when it is
# hit. Every other call here either retries or fails the command; this one **falls open** and launches
# the app anyway (ADR 0029), so the deadline is time a user spends staring at nothing before a launch
# that was always going to happen. Stated per phase for the reason recorded above: a bare float
# applies to all four independently, and the phase that actually matters here is `read` — a relay that
# accepts the connection and then never answers is exactly the shape a connect timeout cannot see.
_CREDENTIAL_CHECK_TIMEOUT = httpx.Timeout(connect=5.0, read=8.0, write=5.0, pool=5.0)


class RelayUnauthorized(Exception):
    """The relay rejected the access token (401) — the caller should refresh and retry."""


class RelayError(Exception):
    """An unexpected relay status or transport failure — the caller logs and backs off.

    ``status`` is the relay's HTTP status when there was one, and ``None`` for a transport failure
    (no answer to classify). Bring-up's retry loop decides retryable-vs-terminal from it (ADR 0022);
    without it the only discriminator would be the message text, which is a response body.

    ``terminal`` is the third category ``status`` cannot express: the request could not be *built* at
    all, so nothing was ever asked and waiting cannot change the answer. A malformed relay address
    fails that way — it is as fatal as a 403, but it carries no status to say so.
    """

    def __init__(self, *args: Any, status: int | None = None, terminal: bool = False) -> None:
        super().__init__(*args)
        self.status = status
        self.terminal = terminal


def _client(signaling_url: str, access_token: str, *, timeout: float | httpx.Timeout) -> httpx.Client:
    return httpx.Client(
        base_url=signaling_url.rstrip("/"),
        headers={"User-Agent": "grid-cli", "Authorization": f"Bearer {access_token}"},
        timeout=timeout,
    )


# Only `register_node` and `deregister_node` below catch `httpx.InvalidURL`, and that scoping is
# deliberate rather than a half-finished sweep. Unlike `remote/probe.py` — where all five guards sit
# on ONE path with ONE degrade ("no capabilities"), so repairing a subset would be worse than
# repairing none — the calls in this module have different error contracts: the serve loop's
# `poll`/`heartbeat` and the teardown's `unregister_node` already run under callers that catch
# `Exception` broadly, so a stray `InvalidURL` cannot escape uncaught there and mapping it would only
# relabel it. These two are the ones whose CALLER classifies the exception type — bring-up's retry
# loop and `grid leave`'s best-effort backstop — which is what makes the mapping load-bearing.
def _guard(resp: httpx.Response, what: str) -> None:
    """Map a relay response to the shared error policy: 401 → refresh, other ≥400 → back off."""
    if resp.status_code == 401:
        raise RelayUnauthorized()
    if resp.status_code >= 400:
        raise RelayError(f"{what} failed ({resp.status_code}): {resp.text[:200]}", status=resp.status_code)


def check_credential(signaling_url: str, access_token: str) -> None:
    """Prove ``access_token`` end-to-end against this grid's relay, or raise saying how it failed.

    Filed with the ``_guard``-mapped calls above rather than the one-shot pricing helpers below, whose
    stated convention is that any failure is a clean ``SystemExit``. That convention is wrong for this
    one: its caller (``cli/grid_credential``) has *three* different answers — refresh and retry (401),
    refuse (403), warn and launch anyway (429 / 5xx / no answer at all) — and a ``SystemExit`` can
    carry none of them (ADR 0029).

    ``GET /relay/v1/models`` is chosen for what it *requires*, not for what it returns; the body is
    discarded. It demands ``inference:models``, and the control plane grants the inference scopes only
    as one bundle, so a token passes here exactly when it holds the ``inference:create`` that
    ``POST /messages`` needs — no refusal this raises is one the launched app would not have hit.
    Verification runs the full server-side gate: signature, ``exp``, network and network-type match,
    scope, the member allowlist snapshot, the denylist, and both epoch-staleness checks.
    """
    try:
        with _client(signaling_url, access_token, timeout=_CREDENTIAL_CHECK_TIMEOUT) as client:
            resp = client.get("/relay/v1/models")
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        # `InvalidURL` is not an `HTTPError` subclass (the trap `deregister_node` records). Both mean
        # nothing was learned about the token, so both must reach the caller as a status-less
        # `RelayError` — the value that makes it warn and launch rather than refuse.
        raise RelayError(f"credential check transport error: {exc}") from None
    _guard(resp, "credential check")


def register_node(
    signaling_url: str,
    access_token: str,
    node_id: str,
    *,
    models: list[str],
    capabilities: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
    pricing: dict[str, float] | None = None,
    max_concurrency: int | None = None,
    role: str = "provider",
) -> None:
    """Advertise this engine's capabilities to the relay (``PUT /nodes/{node_id}``).

    The capabilities map must use the ``{"schema_version": 1, "models": {...}}`` envelope or the
    relay silently drops it (grid-src register.py). Optional fields are omitted when empty.
    """
    body: dict[str, Any] = {"role": role, "models": models, "pricing": pricing or {}}
    if capabilities:
        body["capabilities"] = capabilities
    if meta:
        body["meta"] = meta
    if max_concurrency is not None:
        body["max_concurrency"] = max_concurrency
    try:
        with _client(signaling_url, access_token, timeout=_SERVE_REGISTER_TIMEOUT) as client:
            resp = client.put(f"/nodes/{node_id}", json=body)
    except httpx.InvalidURL as exc:
        # NOT an `HTTPError` subclass (the same trap as `deregister_node` below), and raised while
        # *building* the request — so no relay was ever asked and no amount of waiting will change
        # the answer. Typed, so it can never reach bring-up's loop as an unclassified exception, and
        # marked terminal, so that loop kills the child instead of retrying a broken address at its
        # 60s floor forever (ADR 0022).
        raise RelayError(
            f"this grid's relay address is not a usable URL ({signaling_url!r}): {exc}", terminal=True
        ) from None
    except httpx.HTTPError as exc:
        raise RelayError(f"register transport error: {exc}") from None
    _guard(resp, "register")


def _consumer_role_body() -> dict[str, Any]:
    """The ``PUT /nodes/{id}`` body that flips a node to ``consumer`` (empty models, no pricing).

    Shared by the serve loop's fire-and-forget ``unregister_node`` and the CLI's authoritative
    ``deregister_node`` so the two can't drift. A fresh dict each call — httpx serialises it, but a
    shared mutable module constant would invite an accidental in-place edit leaking across calls.
    """
    return {"role": "consumer", "models": [], "pricing": {}}


def unregister_node(signaling_url: str, access_token: str, node_id: str) -> None:
    """Flip the node back to ``consumer`` so the relay drains queued work and stops sending more.

    Best-effort on shutdown: a failed drain never raises (the relay's TTL prune evicts us anyway).
    """
    try:
        with _client(signaling_url, access_token, timeout=_REGISTER_TIMEOUT) as client:
            resp = client.put(f"/nodes/{node_id}", json=_consumer_role_body())
    except httpx.HTTPError as exc:
        print(f"unregister failed (best-effort, ignoring): {exc}", file=sys.stderr)
        return
    if resp.status_code >= 400:
        print(f"unregister returned {resp.status_code} (best-effort, ignoring).", file=sys.stderr)


def deregister_node(signaling_url: str, access_token: str, node_id: str) -> None:
    """The authoritative CLI backstop for ``grid leave``: flip this box's node to ``consumer``
    (``PUT /nodes/{node_id}``, empty models) so a departed provider's model drops from the grid
    immediately — even when the serve child's own unregister never ran (SIGKILL), was already dead, or
    was rejected.

    Same wire shape as ``unregister_node`` but the opposite error contract: this is a **one-shot CLI**
    call, so it RAISES ``RelayUnauthorized`` (401) / ``RelayError`` (any other failure) and lets
    ``grid leave`` degrade to a clear best-effort message (the node TTL is the fallback). Never a
    ``DELETE`` — removing the row lets a surviving child's heartbeat-404 self-heal re-register it as a
    provider (resurrection); the consumer flip is resurrection-proof (the row survives, heartbeats
    return "ok", and nothing re-registers unless the row vanishes).
    """
    try:
        with _client(signaling_url, access_token, timeout=_REGISTER_TIMEOUT) as client:
            resp = client.put(f"/nodes/{node_id}", json=_consumer_role_body())
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        # httpx.InvalidURL is NOT an HTTPError subclass, so catch it too: a malformed signaling_url or
        # node_id must degrade to the caller's best-effort caveat, never a raw traceback — this one-shot
        # CLI call has no outer catch (unlike the serve-loop callers, which run under remote/serve.py's
        # top-level `except (Exception, SystemExit)`). Scoped to this function on purpose.
        raise RelayError(f"deregister transport error: {exc}") from None
    _guard(resp, "deregister")


def heartbeat(signaling_url: str, access_token: str, *, load: dict[str, Any]) -> str:
    """Keep the node live (``POST /nodes/heartbeat``). Returns ``"ok"`` or ``"missing"`` (404 →
    the node was pruned, so the caller re-registers). 401 raises ``RelayUnauthorized``.

    The body carries only ``load`` — the relay identifies the node from the bearer token, not a
    body field (grid-src parity).
    """
    try:
        with _client(signaling_url, access_token, timeout=10.0) as client:
            resp = client.post("/nodes/heartbeat", json={"load": load})
    except httpx.HTTPError as exc:
        raise RelayError(f"heartbeat transport error: {exc}") from None
    if resp.status_code == 404:
        return "missing"
    _guard(resp, "heartbeat")
    return "ok"


def poll(signaling_url: str, access_token: str, *, timeout: float = POLL_TIMEOUT) -> dict[str, Any] | None:
    """Long-poll for one unit of work (``GET /relay/v1/poll``).

    Returns the job dict on 200 (``{transaction_id, endpoint_path, body, is_stream,
    inference_timeout_seconds}``), ``None`` on 204 (no work). 401 → ``RelayUnauthorized``; any
    other status / transport error → ``RelayError`` so the caller backs off without dying.
    """
    try:
        with _client(signaling_url, access_token, timeout=timeout) as client:
            resp = client.get("/relay/v1/poll")
    except httpx.HTTPError as exc:
        raise RelayError(f"poll transport error: {exc}") from None
    if resp.status_code == 204:
        return None
    if resp.status_code == 200:
        try:
            return resp.json()
        except ValueError as exc:  # a malformed 200 body is a transient relay fault — back off, don't die
            raise RelayError(f"poll returned a malformed body: {exc}") from None
    _guard(resp, "poll")
    raise RelayError(f"poll returned unexpected {resp.status_code}")


def claim_task(
    signaling_url: str,
    access_token: str,
    *,
    timeout: float = TASK_CLAIM_TIMEOUT,
) -> dict[str, Any] | None:
    """Long-poll for one task and claim it (``POST /relay/v1/tasks/claim``).

    A task is claimed on its own endpoint, never ``/poll``: tasks are a durable queue claimed at
    poll time, while ``/poll`` serves an in-memory queue routed at enqueue time (ADR 0032 D-a).
    Mixing them would put a durable mechanism inside a route built for an ephemeral one — and put
    the money path at risk for a feature that does not touch money.

    Returns the claimed task on 200 (``{task_id, project_id, prompt, branch, attempt,
    lease_expires_at}``), ``None`` on 204 (nothing queued). 401 → ``RelayUnauthorized``; anything
    else → ``RelayError`` carrying ``.status``, so the caller can tell a relay with no tasks plane
    (404) from a transient fault.
    """
    try:
        with _client(signaling_url, access_token, timeout=timeout) as client:
            resp = client.post("/relay/v1/tasks/claim")
    except httpx.HTTPError as exc:
        raise RelayError(f"claim_task transport error: {exc}") from None
    if resp.status_code == 204:
        return None
    if resp.status_code == 200:
        try:
            return resp.json()
        except ValueError as exc:  # malformed 200 — transient relay fault, back off rather than die
            raise RelayError(f"claim_task returned a malformed body: {exc}") from None
    _guard(resp, "claim_task")
    raise RelayError(f"claim_task returned unexpected {resp.status_code}", status=resp.status_code)


def report_task_result(
    signaling_url: str,
    access_token: str,
    task_id: str,
    *,
    state: str,
    output: str | None,
    error: str | None,
    session_id: str | None = None,
) -> None:
    """Report a task's terminal outcome (``POST /relay/v1/tasks/{id}/result``).

    The relay authorizes this against the lease, so a provider whose lease expired is refused (403)
    without ever having learned it lost — which is the point of fencing on the lease rather than on
    liveness (ADR 0032 D-c).

    ``session_id`` is the Claude Code conversation this attempt opened; the relay stores it on the
    task so the project's next task can ``--resume`` it (issue 06). Sent only when there is one, so
    a report from a run that never reached the agent cannot blank a session id the relay already
    holds — nothing else on this wire distinguishes "no session" from "do not change it".
    """
    body: dict[str, Any] = {"state": state, "output": output, "error": error}
    if session_id:
        body["session_id"] = session_id
    try:
        with _client(signaling_url, access_token, timeout=_TASK_RESULT_TIMEOUT) as client:
            resp = client.post(
                # The id came off the wire and is being interpolated into a path.
                f"/relay/v1/tasks/{quote(task_id, safe='')}/result",
                json=body,
            )
    except httpx.HTTPError as exc:
        raise RelayError(f"report_task_result transport error: {exc}") from None
    _guard(resp, "report_task_result")


def fetch_task_input(
    signaling_url: str,
    access_token: str,
    task_id: str,
) -> bytes:
    """Download a task's input as a git bundle (``GET /relay/v1/tasks/{id}/input``).

    Lease-fenced server-side exactly like ``/result`` and ``/events``: a provider that no longer
    holds the lease is refused, because a task's input is the requesting user's private source
    (ADR 0032 D-c).

    Read in bounded chunks rather than with ``resp.content``. The relay is authenticated but that is
    not a licence to stream an unbounded body into this process's memory — a provider serves
    inference while a task runs, so an OOM here would take the engine down with the task.
    """
    try:
        with _client(signaling_url, access_token, timeout=_TASK_INPUT_TIMEOUT) as client:
            with client.stream(
                "GET",
                # The id came off the wire and is being interpolated into a path.
                f"/relay/v1/tasks/{quote(task_id, safe='')}/input",
            ) as resp:
                if resp.status_code >= 400:
                    resp.read()  # a streamed response has no `.text` until it is drained
                    _guard(resp, "fetch_task_input")
                chunks: list[bytes] = []
                size = 0
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > MAX_INPUT_BUNDLE_BYTES:
                        # Raised mid-stream, so the oversized body is abandoned rather than
                        # finished and then measured — measuring afterwards has already paid the
                        # cost the limit exists to avoid.
                        raise RelayError(
                            f"the task's input exceeds the {MAX_INPUT_BUNDLE_BYTES}-byte limit")
                    chunks.append(chunk)
                return b"".join(chunks)
    except httpx.HTTPError as exc:
        raise RelayError(f"fetch_task_input transport error: {exc}") from None


def publish_task_events(
    signaling_url: str,
    access_token: str,
    task_id: str,
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Append a BATCH of progress events to a task's log (``POST /relay/v1/tasks/{id}/events``).

    A batch rather than one event per request because a real agent run produces events continuously
    (ADR 0032 D-f): one POST per line would mean one TCP+TLS handshake per line.

    The relay assigns each event's ``seq`` — this side never numbers them. After a requeue the next
    attempt runs on a machine that never saw the previous one's log, and the sequence must continue
    rather than restart (ADR 0032 D-d), which only the relay can arrange.

    Lease-fenced server-side, so ``.status`` matters to the caller: 403 means the lease moved on and
    404 means the task already ended — both verdicts that retrying cannot change, unlike a 5xx or a
    transport failure.
    """
    try:
        with _client(signaling_url, access_token, timeout=_TASK_EVENT_TIMEOUT) as client:
            resp = client.post(
                # The id came off the wire and is being interpolated into a path.
                f"/relay/v1/tasks/{quote(task_id, safe='')}/events",
                json={"events": events},
            )
    except httpx.HTTPError as exc:
        raise RelayError(f"publish_task_events transport error: {exc}") from None
    _guard(resp, "publish_task_events")
    try:
        return resp.json() if resp.content else {}
    except ValueError:
        # The append committed — the relay said 200. A body we cannot read costs us the seq range,
        # which nothing on this path uses, so it must not be reported as a failed publish.
        return {}


def stream_task_events(
    signaling_url: str,
    access_token: str,
    task_id: str,
    *,
    after_seq: int = -1,
) -> Iterator[tuple[int, dict[str, Any]]]:
    """Follow a task's event log as SSE (``GET /relay/v1/tasks/{id}/events?after_seq=N``).

    Yields ``(seq, payload)``. The ``seq`` comes from the block's ``id:`` line and IS the caller's
    resume cursor: reconnecting with ``after_seq=<last seq yielded>`` returns exactly what follows,
    with no gap and no repeat. ``after_seq=-1`` means "from the start".

    Two kinds of line are skipped rather than raised on, because both are ordinary: `: ping`
    comments, which ``EventSourceResponse`` interleaves on an idle stream, and a `data:` line that
    will not parse — one unreadable event must not end a stream the caller is still following.

    A non-200 raises ``RelayError`` carrying ``.status``, so 410 (expired) and 403 (not yours) reach
    the caller as facts rather than as an empty stream that reads like "nothing has happened yet".
    """
    import json as _json

    try:
        with _client(signaling_url, access_token, timeout=_TASK_FOLLOW_TIMEOUT) as client:
            with client.stream(
                "GET",
                f"/relay/v1/tasks/{quote(task_id, safe='')}/events",
                params={"after_seq": after_seq},
            ) as resp:
                if resp.status_code >= 400:
                    resp.read()  # a streamed response has no `.text` until it is drained
                    _guard(resp, "stream_task_events")
                seq: int | None = None
                for line in resp.iter_lines():
                    if line.startswith("id:"):
                        try:
                            seq = int(line[3:].strip())
                        except ValueError:
                            seq = None
                    elif line.startswith("data:"):
                        try:
                            payload = _json.loads(line[5:].strip())
                        except (ValueError, RecursionError):
                            # `RecursionError` is a `RuntimeError`, NOT a `ValueError`, so naming
                            # only `ValueError` leaves a hole no malformed-input case finds — the
                            # same trap `_task_error_message` below already records. Payloads are
                            # opaque and unbounded in depth by contract, and an escape here is a
                            # raw traceback out of `grid task follow`, which catches `RelayError`.
                            continue  # unreadable event — skip it, don't end the stream
                        if isinstance(payload, dict) and seq is not None:
                            yield seq, payload
    except httpx.HTTPError as exc:
        raise RelayError(f"stream_task_events transport error: {exc}") from None


def submit_response(
    signaling_url: str,
    access_token: str,
    txn_id: str,
    *,
    content: bytes | Iterable[bytes],
    stream: bool,
) -> None:
    """Post the engine's result back to the relay (``POST /relay/v1/response/{txn}``).

    ``content`` is the raw engine body: bytes for a whole response (``application/json``) or an
    iterator of SSE byte-chunks for a streamed one (``text/event-stream``).
    """
    content_type = "text/event-stream" if stream else "application/json"
    # A streamed submit reads from the engine indefinitely; a whole one is bounded.
    timeout = httpx.Timeout(connect=10, read=None, write=None, pool=10) if stream else _SUBMIT_TIMEOUT
    try:
        with _client(signaling_url, access_token, timeout=timeout) as client:
            resp = client.post(
                f"/relay/v1/response/{txn_id}",
                content=content,
                headers={"Content-Type": content_type},
            )
    except httpx.HTTPError as exc:
        raise RelayError(f"submit_response transport error: {exc}") from None
    _guard(resp, "submit_response")


def submit_error(
    signaling_url: str,
    access_token: str,
    txn_id: str,
    *,
    message: str,
    tokens_delivered: int = 0,
) -> None:
    """Tell the relay this job failed (``POST /relay/v1/error/{txn}``)."""
    try:
        with _client(signaling_url, access_token, timeout=10.0) as client:
            resp = client.post(
                f"/relay/v1/error/{txn_id}",
                json={"error": message, "tokens_delivered": tokens_delivered},
            )
    except httpx.HTTPError as exc:
        raise RelayError(f"submit_error transport error: {exc}") from None
    # 404 = the txn is already terminal server-side; not an error worth raising on.
    if resp.status_code == 404:
        return
    _guard(resp, "submit_error")


# ---------------------------------------------------------------------------
# Consumer (app) side: send a request through the relay and read the result.
# The orchestration (resolve grid, build payload, consume the SSE) lives in
# cli/remote_request.py; this module owns only the wire boundary (base URL,
# Bearer, the optional routing headers) so it stays the one relay contract.
# ---------------------------------------------------------------------------


def open_consumer_client(
    signaling_url: str, access_token: str, *, timeout: float | httpx.Timeout
) -> httpx.Client:
    """A relay client for the *consumer* side: the same ``signaling_url`` base + Bearer as the
    provider client. Returned (not used internally) so the caller can ``.post()`` chat and
    ``.stream()`` media against ``/relay/v1/...`` and close the client itself — the response
    context manager closes the response, not the client.
    """
    return _client(signaling_url, access_token, timeout=timeout)


# ---------------------------------------------------------------------------
# Provider model pricing (one-shot, not the serve loop): set / remove / show this
# engine's authoritative price for a model it serves, via the relay's `/relay/v1/grid/models`.
# Unlike the serve-loop calls above (which map 401→refresh / 404→re-register so the loop
# survives), these are one-shot CLI calls, so any failure is a clean SystemExit.
# ---------------------------------------------------------------------------


def _price_oneshot(signaling_url: str, access_token: str, method: str, path: str, **kwargs: Any) -> Any:
    """One request to the relay with both failure modes as a clean SystemExit (CLI semantics)."""
    try:
        with _client(signaling_url, access_token, timeout=_REGISTER_TIMEOUT) as client:
            resp = client.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        raise SystemExit(f"Cannot reach the relay ({method} {path}): {exc}") from None
    if resp.status_code >= 400:
        raise SystemExit(f"{method} {path} failed ({resp.status_code}): {resp.text[:400]}")
    return resp.json() if resp.content else {}


# ---------------------------------------------------------------------------
# Distributed tasks, consumer side (ADR 0032): create a task and read one back.
# One-shot CLI calls like the price block above, so any failure is a clean SystemExit — but with
# the relay's `detail` unwrapped, because these carry messages a user is meant to act on ("this
# project already has an active task"), not just a status a developer reads.
# ---------------------------------------------------------------------------


def _task_oneshot(signaling_url: str, access_token: str, method: str, path: str, **kwargs: Any) -> Any:
    try:
        with _client(signaling_url, access_token, timeout=_REGISTER_TIMEOUT) as client:
            resp = client.request(method, path, **kwargs)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        # `InvalidURL` is NOT an `HTTPError` subclass — the trap `deregister_node` records above, and
        # it is raised by the `httpx.Client(base_url=...)` construction inside this very `try`. These
        # are CLI one-shots whose CALLER classifies the exception (the contract is "any failure is a
        # clean SystemExit"), which is exactly the condition that makes the mapping load-bearing.
        raise SystemExit(f"Cannot reach the relay ({method} {path}): {exc}") from None
    if resp.status_code >= 400:
        raise SystemExit(_task_error_message(resp))
    return resp.json() if resp.content else {}


def _task_error_message(resp: httpx.Response) -> str:
    """The relay's `detail` if it sent one, else the raw body — never an empty or bare-status line."""
    try:
        detail = resp.json().get("detail")
    except Exception:
        # Broad on purpose. A hostile or truncated body can raise well outside `ValueError`:
        # `json` recurses, and `RecursionError` is a `RuntimeError`. This function only ever builds
        # an error STRING — there is nothing it could usefully re-raise, and letting anything escape
        # would turn a clean CLI exit into a traceback.
        detail = None
    if isinstance(detail, str) and detail.strip():
        return detail
    return f"Task request failed ({resp.status_code}): {resp.text[:400]}"


def create_task(
    signaling_url: str,
    access_token: str,
    *,
    prompt: str,
    project: str | None,
    files: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create a task (``POST /relay/v1/tasks``).

    The files ride in the SAME request as the prompt, and that is the point of the slice rather than
    a convenience: "the task exists" and "its input is in git" must be one event (ADR 0032 D-b).
    Creating the task first and uploading after lets a provider claim and check out before the files
    land, and the agent then runs against missing input with nothing indicating why.

    Omitted entirely when there are none, so a relay predating the git plane never sees a key it
    does not understand.
    """
    body: dict[str, Any] = {"prompt": prompt}
    if project:
        body["project"] = project
    if files:
        body["files"] = files
    return _task_oneshot(signaling_url, access_token, "POST", "/relay/v1/tasks", json=body)


def get_task(signaling_url: str, access_token: str, task_id: str) -> dict[str, Any]:
    """Read one task back (``GET /relay/v1/tasks/{id}``)."""
    return _task_oneshot(
        signaling_url, access_token, "GET",
        # The id is user input going into a path.
        f"/relay/v1/tasks/{quote(task_id, safe='')}",
    )


def set_model_price(
    signaling_url: str,
    access_token: str,
    *,
    model: str,
    modality: str,
    input_rate: float,
    output_rate: float,
    cache_rate: float,
    name: str | None = None,
    maker: str | None = None,
    status: str | None = None,
    context_length: int | None = None,
) -> dict[str, Any]:
    """Set this engine's authoritative price for ``model`` (``PUT /relay/v1/grid/models``). The relay
    authorizes it only for a provider whose live node serves the model (else 403) — so the engine must be
    joined. Rates are USD per 1,000,000 tokens.

    The same endpoint also records optional model *metadata* (``name``, ``maker``, ``status``,
    ``context_length``): each is sent only when provided, so a rates-only call stays a minimal body and
    doesn't clobber previously-set metadata."""
    body: dict[str, Any] = {
        "model": model,
        "modality": modality,
        "input_rate": input_rate,
        "output_rate": output_rate,
        "cache_rate": cache_rate,
    }
    for key, value in (
        ("name", name),
        ("maker", maker),
        ("status", status),
        ("context_length", context_length),
    ):
        if value is not None:
            body[key] = value
    return _price_oneshot(signaling_url, access_token, "PUT", "/relay/v1/grid/models", json=body)


def delete_model_price(signaling_url: str, access_token: str, model: str) -> dict[str, Any]:
    """Remove this engine's price for ``model`` (``DELETE /relay/v1/grid/models/{model}``). Does not
    require a live node — a provider can clean up its own price after leaving."""
    return _price_oneshot(
        signaling_url, access_token, "DELETE", f"/relay/v1/grid/models/{quote(model, safe='')}"
    )


def list_model_prices(signaling_url: str, access_token: str) -> dict[str, Any]:
    """List the grid's curated models + prices (``GET /relay/v1/grid/models``)."""
    return _price_oneshot(signaling_url, access_token, "GET", "/relay/v1/grid/models")


def consumer_headers(
    *, target_provider: str | None = None, allow_self_provider: bool = False
) -> dict[str, str]:
    """The optional routing headers for a consumer request (the remote-only ``--target-provider`` /
    ``--allow-self-provider``, DECISIONS D16). Each is omitted unless set, so a plain request carries
    neither; the relay reads ``X-Allow-Self-Provider`` as the string ``"true"``.
    """
    headers: dict[str, str] = {}
    if target_provider:
        headers["X-Target-Provider"] = target_provider
    if allow_self_provider:
        headers["X-Allow-Self-Provider"] = "true"
    return headers
