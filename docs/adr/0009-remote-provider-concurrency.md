# ADR 0009 — Remote provider concurrency enforcement (`--max-concurrency` = N poll workers)

Status: accepted (2026-07-03)

## Context

Since ADR 0004/0007 a remote engine advertises `max_concurrency` to the master (`PUT /nodes`), and the
master's capacity-aware router (grid-src `private_server`, already merged) will send it up to that many
concurrent jobs. But the provider ran a **single synchronous `_poll_loop`** (`remote/serve.py`): claim
one job → forward to completion → poll again. So `_inflight` never exceeded 1, the advertised capacity
was inert, and surplus jobs queued on the master. ADR 0007 §3 flagged this. This slice makes the
provider actually serve `max_concurrency` jobs at once, closing the gap between what it advertises and
what it does. Parent: branch `fix/max-concurrency`.

Provider-side only. The flag (`cli/parser.py`), the run record (`cli/remote_provider.py`), `_ServeState`
(clamp `≥1`, `enter/exit_inference`, `load`), the register/heartbeat wire (`remote/relay.py`), and the
master's routing all already exist and are unchanged — only the orchestration around the loop changes.

**Port-source note.** grid-src `provider_runtime/poll_worker.py:run` spawns one poll loop per slot
(N−1 daemon threads + the main thread) and joins them on drain. This ports that shape, adapted to the
in-repo `_ServeState` + testable loop units (`_poll_loop`/`_heartbeat_loop`), with the main thread made
a **pure waiter** (Decision 3).

Hard invariant: local stays untouched (it already rejects `--max-concurrency` as remote-only via
`_REMOTE_ONLY_JOIN_FLAGS`); the existing suite stays green; the loop units
(`register`/`poll_once`/`heartbeat_once`/`handle_job`) are unchanged. Vocabulary: `engine`/`grid`/`model`
on the surface; `node_id` internal.

## Decisions

1. **One poll worker per concurrency slot.** `_serve_loop` spawns `max(1, max_concurrency)` daemon
   threads, each running the existing `_poll_loop`; each independently long-polls the relay
   (`POLL_TIMEOUT=35`, its own `httpx.Client`) and forwards one job, so up to N are in flight while the
   local engine batches them. `_ServeState` was already thread-safe for this — lock-guarded
   `enter/exit_inference`/`load`, and a double-locked `refresh()` that serializes 401 refreshes across
   "the poll + heartbeat threads". N=1 reproduces the pre-fix single-worker behavior exactly.

2. **Default 1, explicit-only — no `--parallel` derivation.** `max_concurrency` stays 1 unless
   `--max-concurrency` is passed; `--parallel` (llama.cpp batch width) and `--max-concurrency` (jobs
   pulled) remain independent knobs. Deriving one from the other (grid-src does) was rejected to keep the
   two flags orthogonal and the default conservative — an operator opts into concurrency explicitly.

3. **Main thread is a pure waiter; drain is bounded by one shared deadline.** SIGTERM raises
   `KeyboardInterrupt` in the main thread only, so the main thread runs no worker — it parks on
   `state.stop` — and the signal can never kill an in-flight `handle_job`. All N workers are daemon
   threads; on stop they are joined against a single `time.monotonic() + _DRAIN_TIMEOUT` deadline, so
   total teardown is bounded by `_DRAIN_TIMEOUT` (5s) even when every worker is parked in a blocking
   long-poll that `state.stop` cannot wake. Jobs that finish within the budget submit their response
   (drain) while the node is still registered; the outer `finally` then unregisters. A per-worker
   timeout was rejected: it serializes to `N × _DRAIN_TIMEOUT` on an idle `grid leave`. Any worker still
   in flight when the deadline expires is logged (by name), not dropped silently.

4. **A dead loop fails the engine loudly; concurrency is capped.** Moving `_poll_loop` off the main
   thread would let an *unexpected* fault (e.g. `relay.poll` decoding a malformed 200 body, or the
   `SystemExit` a corrupt `credentials.toml` raises on refresh) kill a daemon worker silently, stranding
   the node advertising capacity it no longer serves. So each loop runs under `_supervise`, which catches
   any escaped fault, logs it with the thread name, and sets `state.stop` — restoring the pre-fix property
   that a loop's death stops the engine (a dead *loop* is not a dead *job*; `handle_job` still guards the
   latter). `relay.poll` additionally maps a malformed body to a retryable `RelayError` so a transient
   relay hiccup backs off instead of tripping the supervisor. And `max_concurrency` is clamped to
   `[1, _MAX_CONCURRENCY]` (256) — each slot is a real OS thread, so an absurd `--max-concurrency` can't
   exhaust threads/sockets.

## Consequences

- `--max-concurrency N` now genuinely serves N concurrent jobs; heartbeat `active_tasks` ranges 0..N,
  which the master's load score reads relative to capacity. N=1 (the default) is byte-for-byte the old
  behavior.
- Net production change is small: `remote/serve.py` gains `import time`, `_DRAIN_TIMEOUT`, and
  `_serve_loop`; the single `_poll_loop(state)` call site becomes `_serve_loop(state)`. No change to
  `relay.py`, the record layer, or master routing.
- Per-engine (vs one aggregate) concurrency tracking stays out of scope — ADR 0007 §3's standing
  limitation, unchanged.
- New tests cover one-worker-per-slot, the single-worker default, one-heartbeat, bounded-deadline
  teardown, the now-concurrently-hit in-flight counter, a dead worker stopping the engine, drain letting
  an in-flight job finish, and the malformed-poll-body guard.
- `grid engines <grid>` (and `--json`) surfaces each engine's advertised `max_concurrency`, so an
  operator can confirm what a join is serving at (remote-only; the field is `null` when unadvertised).
  The authoritative local value is also readable in the run record `~/.grid/run/engines/<grid_id>/<engine_id>.json`.
