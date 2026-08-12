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

    ``code`` is the relay's machine-readable refusal code (ADR 0033 D-l) when the caller asked for
    one, and ``None`` otherwise — which is every caller but the lease renewer. It exists because one
    provider-side decision cannot be made from the status alone: a cancelled task and an old relay
    with no lease route both answer 404, and only one of them means "stop the agent". See
    ``renew_task_lease``. Defaulting to ``None`` is what keeps every other raiser unchanged.
    """

    def __init__(self, *args: Any, status: int | None = None, terminal: bool = False,
                 code: str | None = None) -> None:
        super().__init__(*args)
        self.status = status
        self.terminal = terminal
        self.code = code


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

    Returns the claimed task on 200 (``{task_id, project_id, member_key, prompt, branch, attempt,
    lease_expires_at}``), ``None`` on 204 (nothing queued). 401 → ``RelayUnauthorized``; anything
    else → ``RelayError`` carrying ``.status``, so the caller can tell a relay with no tasks plane
    (404) from a transient fault.

    ``member_key`` is the one key here that is **not** safe to omit (ADR 0033 D-g): it names whose
    workspace and whose conversation the task belongs to, and `run_task` refuses a claim without one
    rather than falling back to a project-level path. Every other key on this payload degrades to
    the behaviour that preceded it.
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
    result_commit: str | None = None,
    session_reset_reason: str | None = None,
) -> None:
    """Report a task's terminal outcome (``POST /relay/v1/tasks/{id}/result``).

    The relay authorizes this against the lease, so a provider whose lease expired is refused (403)
    without ever having learned it lost — which is the point of fencing on the lease rather than on
    liveness (ADR 0032 D-c).

    ``session_id`` is the Claude Code conversation this attempt opened; the relay stores it on the
    task so the project's next task can ``--resume`` it (issue 06). Sent only when there is one, so
    a report from a run that never reached the agent cannot blank a session id the relay already
    holds — nothing else on this wire distinguishes "no session" from "do not change it".

    ``result_commit`` is where the pushed task branch ended up. The relay checks it against the
    branch it actually holds and refuses the report (409) if they disagree, which is what stops a
    push that silently failed from being recorded as a finished task. Omitted, like ``session_id``,
    when there is none — a run that never got as far as pushing lets the relay read the tip itself.

    ``session_reset_reason`` says why this run started a fresh conversation rather than continuing
    the project's (issue 07). It rides the terminal report because the matching progress event
    cannot be relied on: ``TaskEventPublisher`` latches off permanently after a 403/404 and then
    drops everything, so the only copy that reaches the task's owner is the one the relay writes
    from here. Omitted when nothing reset, so a later clean attempt cannot blank an earlier reason.
    """
    body: dict[str, Any] = {"state": state, "output": output, "error": error}
    if session_id:
        body["session_id"] = session_id
    if result_commit:
        body["result_commit"] = result_commit
    if session_reset_reason:
        body["session_reset_reason"] = session_reset_reason
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


def git_remote_url(signaling_url: str, project_id: str) -> str:
    """The project's repository on the relay's smart-HTTP front (ADR 0032 issue 05).

    The single construction point for this URL, and LOCKSTEP with grid-src's route literal
    (`task_git.py`, `/relay/v1/git/{project_id}`). git appends `/info/refs`, `/git-upload-pack` and
    `/git-receive-pack` to it itself, so only the base is spelled here.

    Named a PROJECT rather than a task: the repository outlives every task in it, and one clone
    serves a project's whole sequence of tasks.
    """
    # The id came off the wire and is being interpolated into a URL.
    return f"{signaling_url.rstrip('/')}/relay/v1/git/{quote(project_id, safe='')}"


def renew_task_lease(signaling_url: str, access_token: str, task_id: str) -> None:
    """Push this task's lease out (``POST /relay/v1/tasks/{id}/lease``).

    The relay renews on the LEASE alone — `state='running'` and this node recorded as the holder —
    and asks no liveness question of its own. That is deliberate (ADR 0032 D-c): a relay that pinged
    back would be inferring the agent from the network, which is the substitution the whole design
    refuses. What a renewal actually proves lives on this side, in `remote/task_lease.py`, which
    renews only while it holds a live `Popen`.

    Lease-fenced like the other two provider writes, so ``.status`` matters: 403 means the lease
    moved to another provider and 404 means the task already ended — both verdicts no retry can
    change, unlike a 5xx or a bare transport failure.

    **The one place in this repository where a provider reads a parsed ``detail``** (ADR 0033 D-l,
    issue 19b). The status is no longer the whole answer for 404: a task a member CANCELLED and a
    relay too old to have this route both answer 404, and the renewer must stop the agent for the
    first and must not for the second. Only the refusal code separates them, so it is lifted onto
    the error here — for this call and no other. ``_guard`` is untouched, so every other caller in
    this module raises exactly the error it raised before.
    """
    try:
        with _client(signaling_url, access_token, timeout=_TASK_EVENT_TIMEOUT) as client:
            resp = client.post(
                # The id came off the wire and is being interpolated into a path.
                f"/relay/v1/tasks/{quote(task_id, safe='')}/lease")
    except httpx.HTTPError as exc:
        raise RelayError(f"renew_task_lease transport error: {exc}") from None
    # The body carries the new expiry, and nothing here reads it: this side already knows its own
    # renewal cadence, and treating the relay's clock as authoritative would make a clock skew look
    # like a lease that had already lapsed. The STATUS, plus the code below, is the whole answer.
    try:
        _guard(resp, "renew_task_lease")
    except RelayError as exc:
        # Rebuilt rather than mutated, so the error a caller sees is one object with one set of
        # fields — and so `_guard` keeps producing the same message for every other call site.
        raise RelayError(*exc.args, status=exc.status, code=refusal_code(resp)) from None


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
                    if resp.status_code == 401:
                        raise RelayUnauthorized()
                    # The relay's OWN sentence, not the object carrying it (ADR 0033 D-l). Every
                    # 4xx in this plane answers `detail={"code","message",…}` since issue 19a, and
                    # `_guard` builds its message from `resp.text` — so this is the one place a
                    # person still reads a refusal and the JSON reached them verbatim. Falls back
                    # to the truncated body for a reply with no `message`, which is what
                    # `_task_error_message` already does for every one-shot task route.
                    raise RelayError(_task_error_message(resp), status=resp.status_code)
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


# What FastAPI answers for a route it has never heard of. A relay too old for an endpoint sends
# exactly this, and it is byte-identical to nothing else this feature produces — every real 404 from
# these routes carries its own sentence. Compared rather than trusted, which is why the hint below
# is keyed on it and not on the status alone: a 404 ABOUT the thing you asked for must show the
# relay's own words. The same distinction `remote/task_lease.py` already has to make.
_BARE_FRAMEWORK_404 = "not found"


def _task_oneshot(signaling_url: str, access_token: str, method: str, path: str, *,
                  missing_route_hint: str | None = None, timeout: float = _REGISTER_TIMEOUT,
                  **kwargs: Any) -> Any:
    """One relay call. Any failure is a clean `SystemExit` carrying the relay's own words.

    `missing_route_hint` replaces the useless bare-404 body for an endpoint that a relay predating
    its feature simply does not have. Without it the user's entire reward for upgrading this CLI
    ahead of their relay is the words "Not Found", which reads as "the thing I asked for is gone".

    `timeout` defaults to the 15s every other one-shot uses, which is right for a call the relay
    answers out of its database. It is a parameter because ONE of these is not that: finishing an
    import makes the relay read a whole repository's object graph, measured at 18.17s on a real
    28,666-commit history (ADR 0033 issue 16b) — so on the default this CLI would give up on a
    request that was going to succeed, and report a timeout for an import that then lands with
    nobody watching.
    """
    try:
        with _client(signaling_url, access_token, timeout=timeout) as client:
            resp = client.request(method, path, **kwargs)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        # `InvalidURL` is NOT an `HTTPError` subclass — the trap `deregister_node` records above, and
        # it is raised by the `httpx.Client(base_url=...)` construction inside this very `try`. These
        # are CLI one-shots whose CALLER classifies the exception (the contract is "any failure is a
        # clean SystemExit"), which is exactly the condition that makes the mapping load-bearing.
        raise SystemExit(f"Cannot reach the relay ({method} {path}): {exc}") from None
    if resp.status_code >= 400:
        message = _task_error_message(resp)
        if (missing_route_hint
                and resp.status_code == 404
                and message.strip().lower() == _BARE_FRAMEWORK_404):
            raise SystemExit(missing_route_hint)
        raise SystemExit(message)
    return resp.json() if resp.content else {}


def refusal_code(resp: httpx.Response) -> str | None:
    """The relay's machine-readable refusal code, or `None` when the answer does not carry one.

    The other half of `_task_error_message`: that one pulls out the sentence a PERSON reads, this
    one the string a program branches on (ADR 0033 D-l). Split rather than one function returning a
    pair, because the two have different callers with nothing in common — every CLI command wants
    the sentence, and exactly one provider-side call wants the code.

    `None` is the ordinary answer and is never an error. A relay predating ADR 0033 sends a
    plain-string `detail`; a relay with no such route at all sends FastAPI's bare
    `{"detail": "Not Found"}`; a proxy may send something that is not JSON. All three mean "no code
    was stated", which every caller must already treat as the pre-19b behaviour — so this function
    cannot raise, and a shape it does not recognise is silently `None` rather than a guess.

    The `except` is deliberately broad, for `_task_error_message`'s reason: a hostile or truncated
    body can raise well outside `ValueError` — `json` recurses, and `RecursionError` is a
    `RuntimeError`, not a `ValueError`. This function only ever produces an OPTIONAL string, so
    there is nothing it could usefully re-raise, and letting anything escape would turn a lease
    refusal into a traceback on a renewal thread.
    """
    try:
        detail = resp.json().get("detail")
    except Exception:
        return None
    if not isinstance(detail, dict):
        # A plain-string `detail` (every relay before ADR 0033, and every endpoint outside this
        # plane), or a shape nobody here writes. Either way no code was stated.
        return None
    code = detail.get("code")
    # `isinstance`, never a bare truthiness test: the value is the relay's, and a client that
    # compared a non-string to its own constant would silently never match — which for the one
    # caller of this function means never stopping a cancelled agent.
    return code if isinstance(code, str) else None


def _task_error_message(resp: httpx.Response) -> str:
    """The relay's `detail` if it sent one, else the raw body — never an empty or bare-status line.

    `detail` is a **string or an object**. Since ADR 0033 D-l the task plane's refusals carry a
    machine-readable `code` alongside their sentence — `{"code": …, "message": …}` — because the
    client is an application and one that regex-matches English is a release away from mis-handling
    a reworded message. The sentence is what a PERSON reads, so it is pulled out here; a caller that
    wants the code reads the response itself.

    Both shapes stay supported, and that is not transitional politeness: only the routes ADR 0033
    touched send objects, every other endpoint on this relay still sends a plain string, and issue
    19 is where the rest follow.
    """
    try:
        detail = resp.json().get("detail")
    except Exception:
        # Broad on purpose. A hostile or truncated body can raise well outside `ValueError`:
        # `json` recurses, and `RecursionError` is a `RuntimeError`. This function only ever builds
        # an error STRING — there is nothing it could usefully re-raise, and letting anything escape
        # would turn a clean CLI exit into a traceback.
        detail = None
    if isinstance(detail, dict):
        # `.get`, never `["message"]`: the shape is the relay's, and an object without one is a
        # refusal we still owe the user words for rather than a `KeyError` in an error path.
        message = detail.get("message")
        detail = message if isinstance(message, str) else None
    if isinstance(detail, str) and detail.strip():
        return detail
    return f"Task request failed ({resp.status_code}): {resp.text[:400]}"


def create_task(
    signaling_url: str,
    access_token: str,
    *,
    prompt: str,
    project_id: str,
    files: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create a task (``POST /relay/v1/tasks``).

    The project is an **id** and is required (ADR 0033 D-a). It used to be a name, resolved by the
    relay against `(owner_id, name)` — an index that is unique per owner, so a name was never an
    address a second project member could use: posting one into someone else's project silently
    created a new, empty project of one's own. Names are resolved here instead, by
    :func:`create_project`, where they can only ever name something of the caller's.

    The files ride in the SAME request as the prompt, and that is the point of the slice rather than
    a convenience: "the task exists" and "its input is in git" must be one event (ADR 0032 D-b).
    Creating the task first and uploading after lets a provider claim and check out before the files
    land, and the agent then runs against missing input with nothing indicating why.

    Omitted entirely when there are none, so a relay predating the git plane never sees a key it
    does not understand.
    """
    body: dict[str, Any] = {"prompt": prompt, "project_id": project_id}
    if files:
        body["files"] = files
    task = _task_oneshot(signaling_url, access_token, "POST", "/relay/v1/tasks", json=body)

    # The ONE way this feature can fail silently, and it is the exact bug it exists to kill.
    # `/relay/v1/tasks` exists on a relay that predates project membership too, so it answers 201
    # rather than the bare 404 every other new route gives: its `_project_name` reads
    # `body.get("project")`, finds nothing because we now send `project_id`, and falls back to the
    # caller's OWN project called `default`. The task runs, reports success, and is simply in the
    # wrong project — which for a shared project means one person's work landing in their personal
    # one with nothing said. **Roll the relay out BEFORE the client.**
    #
    # It is already created by the time we see this, so this cannot prevent it. What it can do is
    # refuse to call it a success and name both projects, which is the whole difference between a
    # silent wrong answer and a loud one. A relay that sends no `project_id` at all cannot be
    # checked and is not second-guessed — every relay that has ever had this endpoint sends it, so
    # the absent case is a proxy mangling the body, not an old server.
    landed = task.get("project_id")
    if landed and landed != project_id:
        raise SystemExit(
            f"Task {task.get('id') or '(no id)'} was created in project {landed}, not the "
            f"{project_id} you asked for. This grid's relay predates project membership and "
            "ignored the project — ask its operator to update it before using shared projects."
        )
    return task


# Every project endpoint arrived together (ADR 0033 issue 10), so a relay missing one is missing
# all of them, and one sentence covers the lot.
_OLD_RELAY = (
    "This grid's relay does not have projects yet — it predates project membership. "
    "Ask its operator to update it, or use a grid that has."
)


# How long to wait for `import/finish`. The relay walks every tree the pushed ref reaches before it
# will set `main` — measured at **18.17s** on a real 28,666-commit repository, under its own 600s
# ceiling (`import_graph.WALK_TIMEOUT_SECONDS`). This sits ABOVE that ceiling, deliberately and in
# the same direction as the git-transport pair: a client that gives up first turns a refusal the
# relay was about to explain into "the connection died", and the import either lands unobserved or
# does not, with no way to tell from here.
_IMPORT_FINISH_TIMEOUT = 900.0


def init_project(signaling_url: str, access_token: str, project_id: str) -> dict[str, Any]:
    """Give an empty project a trunk (``POST /relay/v1/projects/{id}/init``), ADR 0033 D-o.

    The other bootstrap beside import, and the only one available to somebody starting a new piece
    of work: it creates a single empty root commit and sets it as `main`.

    **No request body**, and that is asserted on the relay's side rather than merely unread — the
    trunk it makes holds nothing, so a `files` list (which means something real on
    `POST …/{id}/commit`) is refused instead of silently dropped. Sending `{}` would be a wire
    detail with nothing behind it.

    The default timeout, not import's: this writes one empty commit, with no object graph to walk.
    """
    return _task_oneshot(
        signaling_url, access_token, "POST",
        f"/relay/v1/projects/{quote(project_id, safe='')}/init",
        missing_route_hint=_OLD_RELAY)


def open_project_import(signaling_url: str, access_token: str,
                        project_id: str) -> dict[str, Any]:
    """Open an import and learn where to push (``POST /relay/v1/projects/{id}/import``).

    The reply carries the staging ref. It is NOT derived here: the spelling of
    `refs/import/<member_key>` is the relay's alone (the PRD's lockstep table says so), and a client
    that built it would be a second place that has to agree about how a member key is shaped.
    """
    return _task_oneshot(
        signaling_url, access_token, "POST",
        f"/relay/v1/projects/{quote(project_id, safe='')}/import",
        missing_route_hint=_OLD_RELAY)


def finish_project_import(signaling_url: str, access_token: str,
                          project_id: str) -> dict[str, Any]:
    """Validate what was staged and set `main` (``POST …/{id}/import/finish``).

    Slow by nature — see `_IMPORT_FINISH_TIMEOUT`. A refusal comes back as a 422 whose `detail`
    carries `reason` and `path` beside its sentence, so a caller can say which file to remove
    without reading English.
    """
    return _task_oneshot(
        signaling_url, access_token, "POST",
        f"/relay/v1/projects/{quote(project_id, safe='')}/import/finish",
        missing_route_hint=_OLD_RELAY, timeout=_IMPORT_FINISH_TIMEOUT)


def create_project(signaling_url: str, access_token: str, *, name: str) -> dict[str, Any]:
    """Create-or-get the caller's project called ``name`` (``POST /relay/v1/projects``).

    Idempotent by design, so `grid task create` with no `--project` can call it every time to turn
    the default name into an id without accumulating empty projects.
    """
    return _task_oneshot(signaling_url, access_token, "POST", "/relay/v1/projects",
                         json={"name": name}, missing_route_hint=_OLD_RELAY)


def list_projects(signaling_url: str, access_token: str) -> dict[str, Any]:
    """Every project the caller is a MEMBER of (``GET /relay/v1/projects``).

    Membership, not ownership: since a project has members, being told an id out of band is no
    longer the only way someone admitted to one could find it.
    """
    return _task_oneshot(signaling_url, access_token, "GET", "/relay/v1/projects",
                         missing_route_hint=_OLD_RELAY)


def list_project_members(signaling_url: str, access_token: str, project_id: str) -> dict[str, Any]:
    """Who is in a project (``GET /relay/v1/projects/{id}/members``)."""
    return _task_oneshot(
        signaling_url, access_token, "GET",
        # The id is user input going into a path.
        f"/relay/v1/projects/{quote(project_id, safe='')}/members",
        missing_route_hint=_OLD_RELAY)


def add_project_member(signaling_url: str, access_token: str, project_id: str,
                       *, email: str) -> dict[str, Any]:
    """Admit someone to a project by email (``POST /relay/v1/projects/{id}/members``).

    By email because `user_id` is `grid:<network>:<sub>` — a string nobody types and nobody can look
    up. The relay resolves it against the grid's own members, so someone who has never signed in is
    refused rather than invented.
    """
    return _task_oneshot(
        signaling_url, access_token, "POST",
        f"/relay/v1/projects/{quote(project_id, safe='')}/members", json={"email": email},
        missing_route_hint=_OLD_RELAY)


def remove_project_member(signaling_url: str, access_token: str, project_id: str,
                          *, member_key: str) -> dict[str, Any]:
    """Remove someone from a project (``DELETE …/members/{member_key}``).

    Addressed by the **member key** rather than the email or the user id: the key is a path segment
    by construction, and `grid:<network>:<sub>` is not. `grid project member list` prints it.
    """
    return _task_oneshot(
        signaling_url, access_token, "DELETE",
        f"/relay/v1/projects/{quote(project_id, safe='')}"
        f"/members/{quote(member_key, safe='')}", missing_route_hint=_OLD_RELAY)


def reset_project_wip(signaling_url: str, access_token: str, project_id: str,
                      *, member_key: str, commit: str) -> dict[str, Any]:
    """Put a member's WIP branch back to a named commit (``POST …/wip/{key}/reset``).

    The one way out of a WIP branch left ahead of the task branch it settled from (ADR 0033 D-c).
    Nothing else moves one backwards: members do not push, promote writes only `main`, and there is
    no revert — so without this a member's next task is silently cut from a lost attempt's work.

    Addressed by the **member key**, like `remove_project_member` and for the same reason: the key
    is a path segment by construction and `grid:<network>:<sub>` is not.

    Refused by the relay while that member has an active task, which is the serialization that
    makes two writers of one branch impossible. That refusal arrives as a real 409 with its own
    words, not as the bare-404 hint below.
    """
    return _task_oneshot(
        signaling_url, access_token, "POST",
        f"/relay/v1/projects/{quote(project_id, safe='')}"
        f"/wip/{quote(member_key, safe='')}/reset",
        json={"commit": commit}, missing_route_hint=_OLD_RELAY)


def promote_project(signaling_url: str, access_token: str, project_id: str,
                    *, member_key: str) -> dict[str, Any]:
    """Fast-forward a project's `main` from a member's WIP branch (``POST …/{id}/promote``).

    `main` is a release branch since ADR 0033 D-b, so nothing a task does reaches it — this is the
    one thing that moves it, and it is an ENDPOINT rather than a push because keeping the relay
    `main`'s only writer is what lets a provider be unable to announce its own success.

    The source is named, not assumed to be the caller's own: the moment somebody leaves the team
    `wip/<departed>` holds everything they never promoted, and there is no adopt, transfer or rename
    anywhere in this feature. By **member key** for `reset_project_wip`'s reason.

    **Fast-forward only.** A source branch that is behind is refused with a real 409 — a D-l object
    carrying `main_commit` and `behind` as fields, so a client can serialize its own promotes rather
    than discover the collision rate empirically. That is not the bare-404 hint below; it is the
    relay's own answer, and the words belong to it.
    """
    return _task_oneshot(
        signaling_url, access_token, "POST",
        f"/relay/v1/projects/{quote(project_id, safe='')}/promote",
        json={"member_key": member_key}, missing_route_hint=_OLD_RELAY)


def integrate_project(signaling_url: str, access_token: str,
                      project_id: str) -> dict[str, Any]:
    """Bring a project's `main` into the CALLER's WIP branch (``POST …/{id}/integrate``).

    The counterpart to promote, and the thing that makes promote survivable at all: since `main`
    moves only on a promote, the first one locks every other member out — their branch was cut from
    a trunk that is now history, so `merge-base --is-ancestor main wip/<theirs>` can never succeed
    again. This is the only way back.

    **It takes no member key**, which is the one place it differs from `promote_project` and
    `reset_project_wip`, and it is forced rather than chosen. The relay holds the caller's one task
    slot by inserting a task row keyed on their own `owner_id` — that INSERT is what serializes an
    integration against a task already running on the branch it is about to move — so a request that
    named somebody else's branch would take the wrong person's slot and move a ref their running
    task was cut from. A departed member's branch is reached through promote and `wip reset`, both
    of which are safe to address by key.

    **No request body**, because the relay reads none: the branch is the caller's own and the trunk
    is the project's, so there is nothing to send. Sending `{}` would be a wire detail with nothing
    behind it.

    Four answers, and the client has to tell them apart without diffing oids: `status` is
    `up_to_date`, `fast_forward`, `merged` or — since ADR 0033 issue 15 — `merge_task`, which means
    git could not decide and the relay has QUEUED a task whose agent will. That last one carries a
    `task_id` and the conflicted paths as `files`, and reports `advanced: false`: nothing has moved,
    and the caller's one task slot is now held until that task ends.

    A conflict is therefore no longer a refusal. The **409** that remains is
    `integrate_not_fast_forward` — something moved the caller's branch under a request holding their
    slot — and it is not the bare-404 hint below, which is only for a relay that predates the route.
    """
    return _task_oneshot(
        signaling_url, access_token, "POST",
        f"/relay/v1/projects/{quote(project_id, safe='')}/integrate",
        missing_route_hint=_OLD_RELAY)


def commit_project(signaling_url: str, access_token: str, project_id: str, *,
                   message: str, files: list[dict[str, Any]] | None = None,
                   deletes: list[str] | None = None) -> dict[str, Any]:
    """Commit files onto the CALLER's WIP branch without running an agent (``POST …/{id}/commit``).

    ADR 0033 D-j, and the thing that makes the rest of this design usable day to day: without it the
    only route from "I edited a file on my machine" to "it is in the project" is `grid task create
    --file`, which spends the member's one task slot and then runs an agent that may change the very
    line being fixed.

    **Not a relaxation of the push ban.** The write still goes through the relay, still lands on
    exactly one ref, and the relay still holds the member's task slot while it does — so committing
    is refused while they have a task in flight, exactly as integrating is, and the refusal names it.

    **No member key**, for `integrate_project`'s reason: the slot the relay holds is keyed on the
    caller's own identity, so there is no coherent way to commit onto somebody else's branch.

    The keys are omitted when empty rather than sent as `[]`, matching `create_task`: a relay that
    predates this route answers a bare 404 either way, but a relay that predates only `deletes` must
    not see a key it would refuse.
    """
    body: dict[str, Any] = {"message": message}
    if files:
        body["files"] = files
    if deletes:
        body["deletes"] = deletes
    return _task_oneshot(
        signaling_url, access_token, "POST",
        f"/relay/v1/projects/{quote(project_id, safe='')}/commit",
        json=body, missing_route_hint=_OLD_RELAY)


# A relay that predates ADR 0033 issue 19b has no cancel route, and answers the same bare framework
# 404 an unknown TASK id would produce on a relay that does. Its own sentence rather than
# `_OLD_RELAY`: that one says the relay has no projects, which is both wrong here and would send
# somebody to check a feature that is working perfectly.
_OLD_RELAY_NO_CANCEL = (
    "This grid's relay cannot cancel a task — it predates the cancel route. Ask its operator to "
    "update it. The task will still end at its own deadline."
)


def cancel_task(signaling_url: str, access_token: str, task_id: str) -> dict[str, Any]:
    """End a queued or running task, freeing its member's slot (``POST /relay/v1/tasks/{id}/cancel``).

    No body. The route is addressed by the task id and fenced on the caller's own identity, so
    anything sent here would be a second way of saying who is cancelling what — the reasoning
    `integrate_project` records for the same shape.

    Fenced on project MEMBERSHIP relay-side, not ownership: on a shared project the colleague whose
    merge task has been stuck for an hour is precisely who needs to stop it.
    """
    return _task_oneshot(
        signaling_url, access_token, "POST",
        # The id is user input going into a path.
        f"/relay/v1/tasks/{quote(task_id, safe='')}/cancel",
        missing_route_hint=_OLD_RELAY_NO_CANCEL)


def get_task(signaling_url: str, access_token: str, task_id: str) -> dict[str, Any]:
    """Read one task back (``GET /relay/v1/tasks/{id}``)."""
    return _task_oneshot(
        signaling_url, access_token, "GET",
        # The id is user input going into a path.
        f"/relay/v1/tasks/{quote(task_id, safe='')}",
    )


def list_tasks(signaling_url: str, access_token: str, project_id: str, *,
               mine: bool = True, states: list[str] | None = None,
               limit: int | None = None, after: str | None = None) -> dict[str, Any]:
    """The tasks in one project (``GET /relay/v1/tasks``). ADR 0033 D-l, issue 19a.

    Nothing listed tasks before this: `get_task` answers one id at a time, and the id came from the
    create call — so an application that lost it, or a person asking "what has run today", had to
    clone the project and read `task/*` refs over the git front.

    `mine=False` widens it to every member's tasks in the project, which is issue 19a's decision on
    ADR 0033's own reading that fencing a shared project's reads on `owner_id` is the wrong default.

    `states` is repeatable and the relay does not check the VALUES — a state nobody writes simply
    matches nothing, so a new one needs no client release.
    """
    params: dict[str, Any] = {"project_id": project_id}
    # Sent as the strings the relay parses. Every query parameter on that route is declared as a
    # string there so its refusals carry a `code` like the rest of the plane, rather than falling to
    # FastAPI's list-shaped validation error.
    if not mine:
        params["mine"] = "false"
    if states:
        params["state"] = list(states)
    if limit is not None:
        params["limit"] = str(limit)
    if after:
        params["after"] = after
    return _task_oneshot(
        signaling_url, access_token, "GET", "/relay/v1/tasks",
        params=params, missing_route_hint=_OLD_RELAY)


def project_status(signaling_url: str, access_token: str,
                   project_id: str) -> dict[str, Any]:
    """Where a project is, from the caller's side (``GET /relay/v1/projects/{id}/status``).

    ADR 0033 D-l, issue 19a. Two things were answerable before this only by performing a write:
    *how far behind is my branch* (attempt a promote and read the refusal) and *what holds my slot*
    (attempt a create and read the 409). Both are reads now.

    It is also the project's **change signal**. `main_commit` moves on a promote or an import, and
    each member's tip moves when a task of theirs settles, when a tier-1/2 integration lands, or
    when they commit — so an application notices either by diffing an oid it already holds, instead
    of polling `git fetch` against the transport issue 16a exists to rescue.

    ⚠️ **A CONFLICTING integration moves no ref at all** — it queues a merge task — so the tips are
    not the whole signal. `active_task` and each member's `active_task_id` are what change there,
    and they are the ones that matter: that is the integration which can run for an hour.
    """
    return _task_oneshot(
        signaling_url, access_token, "GET",
        f"/relay/v1/projects/{quote(project_id, safe='')}/status",
        missing_route_hint=_OLD_RELAY)


def preview_integration(signaling_url: str, access_token: str,
                        project_id: str) -> dict[str, Any]:
    """What integrating WOULD do (``GET /relay/v1/projects/{id}/integrate/preview``).

    ADR 0033 D-l, issue 19a. Before it, integration *was* the conflict check: asking cost the
    member's one task slot and, when the answer was "they conflict", queued a paid agent run to
    resolve them.

    The `status` vocabulary is `integrate_project`'s own — `up_to_date`, `fast_forward`, `merged`,
    `merge_task` — deliberately, so a caller branches on one set of four words rather than mapping a
    preview enum onto the real one. It writes nothing and holds no slot, so it answers while the
    caller already has a task in flight, which is exactly when they want it.
    """
    return _task_oneshot(
        signaling_url, access_token, "GET",
        f"/relay/v1/projects/{quote(project_id, safe='')}/integrate/preview",
        missing_route_hint=_OLD_RELAY)


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
