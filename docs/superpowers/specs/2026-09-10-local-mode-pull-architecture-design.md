# Local mode serves inference by pull, not push

**Status:** draft for review · **Date:** 2026-09-10
**Repo:** `autonomous-grid` (the public CLI end users install)
**Becomes:** an ADR under `docs/adr/` once accepted.

## Summary

Local mode reaches a worker's inference engine by **dialling into it** across the LAN. That inbound
HTTPS endpoint on a private IP is the sole reason this codebase mints its own certificate authority,
and that CA machinery produced a day of production failures and two serious trust defects.

Remote mode has never needed any of it, because a worker there **pulls** work and its engine listens
only on loopback.

This spec makes local mode pull too. The engine certificate, the private CA on the worker, the
trust-on-first-use CA fetch and the process-wide `SSL_CERT_FILE` all stop existing — not because they
are fixed, but because nothing needs them.

## Why now — the evidence

Measured on two Macs (`172.168.20.114` running the grid, `172.168.20.246` running a node with a 31B
model) during a full end-to-end session on 2026-09-09. Seven distinct defects had to be fixed before
one inference request completed, and five were the certificate machinery or something masking it:

| Defect | Where |
|---|---|
| Leaf certificate inherited the CA's CN under LibreSSL (stock macOS `openssl` ignores `-subj`) | `shared/tls.py` |
| Leaf carried no `authorityKeyIdentifier`; OpenSSL 3.x peers refuse it outright | `shared/tls.py` |
| Engine trust context mixed an ambient store with the engine CA; two CAs share `CN=autonomous-grid local CA`, so OpenSSL matched the wrong issuer by name | `shared/allocator/runtime.py::_tls_verify` |
| `grid start` never re-ran `ensure_server_cert`, so an existing grid could never self-heal | `local/runtime.py` |
| Health-probe budget exhaustion flipped a healthy residency to FAILED | `shared/allocator/runtime.py::_probe_process_health` |

A security review of the same paths then found two more, both in the CA plumbing rather than in the
certificates themselves:

- **`cli/allocator.py:322-357` re-arms trust-on-first-use on every verification failure.** The guard
  is `if cfg.get("server_tls_ca_file")`, but a URL-addressed grid synthesises a config without that
  key (`local/config.py:63-75`) and the fetch never writes it. So each failure re-fetches the CA over
  `verify=False`, overwrites the learned one with no comparison, prints "Trusting this grid's
  certificate for the first time" (false on a re-learn), and **retries the request carrying
  `X-Grid-Allocator-Token`** — the operator token from which every node credential in the grid is
  derived.
- **`local/config.py:38-52` sets `SSL_CERT_FILE` on every grid-addressing command**, and httpx uses it
  as `cafile=`, which *replaces* the public roots rather than adding to them. The grid's CA carries no
  `nameConstraints` (`shared/tls.py:78-82`), so it becomes the process's sole and universal trust
  anchor. Two consequences: model downloads from `huggingface.co` inside the allocator node daemon
  cannot chain (`shared/models/download.py:66,116,178`), and whoever holds `ca.key` can mint a leaf
  for any name the process later dials.

Both live entirely inside the machinery this change deletes.

## Goals

1. No certificate, CA, or trust-learning on the **engine** hop in local mode.
2. A worker exposes **no inbound port** that another machine must reach.
3. No new runtime dependency: no database, no container engine, no second package.
4. Every step verifiable on the existing two-Mac setup as it lands.

## Non-goals

- **Unifying local and remote into one code path.** This makes local mode the same *shape* as remote.
  Actual code sharing is separate, later work.
- **Adopting the self-hosted relay** (`autonomous-grid#70` / `autonomous-grid-cli#20`). Evaluated and
  rejected for now — see Alternatives.
- **Encrypting the control-plane hop.** Decided separately; see Security posture.
- **Task, project, or Git planes.** Inference only.

## Architecture

```
BEFORE — push
  client ──> grid (machine A) ──dials──> engine (worker) 0.0.0.0:18082, HTTPS
                                         ↑ leaf + CA + learned CA + SSL_CERT_FILE

AFTER — pull
  client ──> grid (machine A) ── in-memory queue
                   ↑ long-poll, outbound from the worker
             node (worker) ──> engine 127.0.0.1:18082, plain HTTP
```

The worker's only network act is outbound. The grid never initiates a connection to a worker.

### Why there is no database

The internal relay uses PostgreSQL because it carries work that must outlive a process: tasks with
leases that survive a provider's death, projects, Git objects, billing, conversation logs.

An inference request has none of that shape. It exists only while a consumer holds an HTTP connection
open. If the grid restarts, that connection breaks and the request is dead whether or not it was
persisted. **The client connection is the durability boundary.** Persisting it would add a dependency
to protect state that cannot survive anyway.

This matches what local mode already does: the node registry is an in-memory dict (`app.state.nodes`,
`local/server.py:1642`) rebuilt from heartbeats within one interval, and only allocator policy is
written to disk, as JSON.

### This is ephemeral state, not statelessness — the grid is single-process

The distinction matters, and stating it wrongly would mislead whoever reads this next. The grid is
**not** stateless: the transaction table lives in the memory of the exact process holding the
consumer's connection. Three consequences follow, and the first is a hard limit:

1. **The grid cannot be run as two processes behind a load balancer.** A worker's poll landing on
   instance B cannot claim a transaction registered on instance A. Horizontal scaling would require
   shared state — and that is precisely the point at which a database returns and this design's main
   advantage over the internal relay disappears. Anyone proposing HA for a local grid is proposing a
   different architecture, not a deployment change.
2. **A grid restart kills every in-flight request.** Acceptable, and not a regression: the consumer's
   connection breaks in the same instant, so nothing recoverable is lost.
3. **The worker holds nothing the grid needs.** Its engine process and residency record are its own
   business. If it dies, the transaction expires at its deadline; there is no distributed cleanup.

The single-process assumption is therefore load-bearing. It is true of local mode today as well — the
registry is already in-process memory — so this change inherits the constraint rather than
introducing it, but it makes the constraint carry real per-request weight for the first time.

## Components

Three units in `local/`, each with one job and a stated dependency.

### `local/inflight.py` — the transaction table

Pure in-memory, no I/O, no FastAPI import. Testable standalone, in the spirit of `auto_router.py`.

```
create(model, body, features) -> Transaction        # registers, returns handle
claim(node_id, models) -> Transaction | None        # oldest unclaimed this node can serve
publish(txn_id, chunk)                              # provider result, streaming or whole
finish(txn_id, outcome)                             # terminal; wakes the consumer
cancel(txn_id, reason)                              # consumer vanished, or deadline passed
sweep(now) -> list[Transaction]                     # expired; caller decides what to report
```

A `Transaction` holds: id, model, request body, state (`pending | claimed | streaming | done |
failed`), claiming node, created/claimed timestamps, deadline, an `asyncio.Queue` for chunks, and a
`Future` the consumer awaits.

Invariants:
- A transaction is claimed by **at most one** node. Claiming is atomic under a single lock.
- `publish` after `finish` or `cancel` is a no-op, never an error — a provider's last write can race
  the consumer's disconnect.
- Nothing in this module blocks on I/O, so a slow worker can never stall the table.

### `local/poll.py` — the provider-facing routes

Depends on `inflight` and the existing node authentication.

| Route | Direction | Purpose |
|---|---|---|
| `GET /grid/v1/poll` | worker → grid | long-poll; returns one claimed transaction or 204 after the wait window |
| `POST /grid/v1/result/{txn}` | worker → grid | one chunk, or the whole response |
| `POST /grid/v1/error/{txn}` | worker → grid | terminal failure with a reportable message |

Authentication is the existing node credential (`shared/allocator/auth.py`), whose HMAC path the
security review checked and found sound. A node may only claim work for models it has advertised as
ready in its heartbeat.

One poll loop per concurrency slot, matching remote mode. That is what bounds a worker's parallelism;
there is no separate scheduler.

### `_proxy_openai`, reduced

It stops dialling an engine. It picks a **node** rather than an endpoint, registers a transaction,
and awaits the result. `_choose_engine` (`local/server.py:1762`) is replaced by node selection over
the same registry data it already reads — least-loaded among nodes whose heartbeat advertises the
model as ready.

## Data flow

**Non-streaming**

1. Consumer `POST /v1/chat/completions`.
2. `_proxy_openai` validates, selects a candidate node set, calls `inflight.create`.
3. Worker's poll returns the transaction; `state = claimed`.
4. Worker runs inference against `http://127.0.0.1:<port>/v1`, `POST`s the whole body to
   `/grid/v1/result/{txn}`.
5. `inflight.finish` resolves the consumer's future; the grid returns the body.

**Streaming**

Steps 1–3 identical. The worker then POSTs each SSE block to `/grid/v1/result/{txn}` as it arrives;
`inflight.publish` puts it on the consumer's queue and the grid re-emits it. The terminal block calls
`finish`.

Blocks are relayed **verbatim** — the grid does not parse, re-frame, or inject a `[DONE]`. It is a
pipe, and a pipe that rewrites its contents is a pipe that will disagree with some client.

## Error handling

| Case | Behaviour |
|---|---|
| No node advertises the model | 503 immediately, before creating a transaction. Same code the caller sees today. |
| Nodes exist but none claims within `claim_deadline` | 503, transaction cancelled. Distinct message from the above — "no worker took the request" is not "nothing serves this model". |
| Claimed, but no chunk within `result_deadline` | 504, transaction failed. **Not requeued**: the engine may be mid-generation, and a second worker would duplicate the work and the cost. |
| Worker `POST`s to a finished/cancelled transaction | 200 with a `cancelled: true` body, so the worker stops generating instead of streaming into a void. |
| Consumer disconnects mid-stream | `cancel`; the next worker write learns it and aborts. |
| Worker dies mid-transaction | `sweep` expires it at `result_deadline`; consumer gets 504. Nothing to clean up on the worker — its process died with the request. |
| Grid restarts | Every in-flight transaction dies with its client connection. Workers' polls fail and retry; registry rebuilds from heartbeats. No recovery path needed, because there is nothing to recover. |

Every deadline is a named constant with a comment, not a literal.

## What gets deleted

- `_choose_engine` and the engine-dialling half of `_proxy_openai` (`local/server.py`).
- Engine certificate generation in the node (`cli/allocator.py:971` and the `--engine-tls-*` plumbing).
- The trust-on-first-use CA fetch, `_fetch_grid_ca` (`cli/allocator.py:322-357`) — **removes the
  operator-token disclosure described above**.
- `SSL_CERT_FILE` injection: `apply_server_tls_client_env`, `server_tls_client_env`,
  `server_tls_ca_bundle`, `learned_ca_path`, `grid_dir_ca_candidate` (`local/runtime.py:208-258`) and
  the call at `local/config.py:38-52` — **restores the public trust store and unbreaks HuggingFace
  downloads**.
- `endpoint_url` on managed node registry records, and the engine-TLS fields that travel with it.

`shared/tls.py` remains, and is still needed: the grid's own listener uses it. That file's own defects
are fixed separately and are not in scope here.

## Security posture

This change is a net reduction in attack surface, but it does not encrypt the LAN.

**Removed:** the engine certificate; the private CA on the worker; the re-arming TOFU fetch and its
operator-token disclosure; the process-wide trust-store replacement; the worker's inbound port.

**Unchanged and still open** — filed separately, not addressed here:
- `local/server.py:353-359` sets `CORSMiddleware(allow_origins=["*"], allow_methods=["*"])` on a
  listener bound to the LAN address, and unmanaged `PUT /nodes/{id}` is unauthenticated. Any web page
  a browser on that LAN opens can read the registry and register a rogue node. Under this design the
  rogue node can no longer receive a dialled request, but it can still take work from the queue —
  so **the poll route must reject nodes that did not authenticate**, and this spec requires that.
- `shared/tls.py:173-178` reuses a leaf without checking that the CA on disk signed it, and never
  checks expiry (`CERT_DAYS = 398`).

**Accepted, deliberately:** the consumer→grid and worker→grid hops carry bearer tokens over the LAN.
Whether those run over TLS is an operator decision, unchanged by this spec. A reverse proxy in front
of the grid remains the supported way to add it, exactly as the self-hosted relay documents.

## Testing

- **`inflight` is pure**, so its table is unit-tested directly: claim exclusivity under concurrency,
  publish-after-finish, cancel-then-publish, deadline sweeps, and the ordering of streamed chunks. No
  grid, no network, no sleep-based timing — the clock is injected, as `ManagedModelRuntime` already
  does.
- **`poll.py`** is tested against a FastAPI test client: authentication, model filtering, the 204 wait
  window, and the cancelled-transaction response.
- **End-to-end on the two Macs**, the same protocol that validated the current fixes: a request
  returns a known marker; then a request every minute for ten minutes with the worker's failure
  counter watched, because the defect class this replaces oscillated on a ~35-second cycle and a
  single successful request proves nothing.
- **A regression test for the deletion**: assert that no module under `cli/` or `local/` references
  the removed TLS helpers, so the machinery cannot quietly return.

## Rollout

Each step leaves the tree working and is verified on hardware before the next.

1. `inflight` + `poll` land **alongside** the existing push path, unused. Nothing changes for anyone.
2. The node gains a poll loop, still also serving its HTTPS endpoint. Both paths work; a switch
   selects which one `_proxy_openai` uses.
3. Flip the default to pull. Verify end to end and over ten minutes.
4. Delete the push path, the engine certificate, and the CA-learning machinery. Verify again.

Step 4 is what pays for the work, and it must not be skipped or deferred — the defects live in the
code that step deletes.

## Alternatives considered

**Adopt the self-hosted relay (`autonomous-grid#70` + `autonomous-grid-cli#20`).** Rejected for now.
It is a real, working pull architecture and it would eventually be the right convergence — but for
this goal it costs more than it saves:

- The allocator has **never been integrated with it**: `git merge-base --is-ancestor origin/grid-relay
  origin/codex/dynamic-resource-allocator-v2` is false, and neither branch contains the other's files.
  That integration is unwritten work.
- The relay proxies `/allocator/*` to a loopback **sidecar that exists in neither repository**.
- It requires **Docker or Podman** for PostgreSQL, and **git ≥ 2.40** on the relay host, enforced
  fatally at startup — stock macOS ships 2.39.5, so the master refuses to boot.
- Its answer to "no CA" is **no TLS**: it serves plain HTTP and delegates TLS to a reverse proxy.

**Patch the CA machinery instead.** Rejected as the primary plan, though the TLS fixes already made
are worth shipping on their own: the grid's listener still needs a certificate, so `shared/tls.py`
stays either way. Patching leaves two certificates where the goal is zero on the engine hop.

## Decisions that would otherwise be left open

**Dispatch is not placement, and the allocator does not decide it.** The allocator answers "which
model should live on which host", on a timescale of minutes, using forecasts. The queue answers "who
takes *this* request", on a timescale of milliseconds, using the load already in the last heartbeat.
Routing dispatch through the allocator would couple a per-request path to a forecasting loop and give
the allocator a second, contradictory job. Node selection is therefore least-loaded among nodes whose
heartbeat advertises the model ready — the same data `_choose_engine` reads today, asked about a node
instead of a URL.

**The worker's poll timeout must outlast the grid's wait window.** Remote mode already fixes this
relationship and states why: `POLL_TIMEOUT = 35.0` on the client against a 30 s server window
(`remote/relay.py:32,42-44`) — "the client gives up first and every idle cycle looks like a transport
error". Local mode adopts the same pair, 30 s grid-side and 35 s worker-side, and the constants carry
a comment naming the relationship so a future edit cannot narrow it by accident. The two numbers are
one decision, not two.

**A claim is per node, bounded by the node's own concurrency.** The worker runs one poll loop per
slot, exactly as remote mode does — "each slot is a real OS thread holding a long-poll"
(`remote/serve.py:61`). That is what limits parallelism, so the grid needs no server-side slot
accounting and `poll.py` filters on model readiness alone. How many replicas of a model a node runs
is the node's business; it surfaces only as the concurrency it advertises.

## Deliberately deferred

- **Sharing code with remote mode.** This makes local the same shape as remote; merging the two
  implementations is a later, separate decision that should be taken with both working.
- **The two open defects named under Security posture** — LAN-wide CORS with unauthenticated node
  registration, and `ensure_server_cert` reusing an unverified or expired leaf. Both are real, both
  are filed, neither is caused or fixed by this change.
