# ADR 0009 — Remote `grid join` is additive (one identity per grid)

Status: accepted (2026-07-02) — supersedes **ADR 0007 Decision 1 for remote mode**. Shipped as **Slice 1**
(singleton + additive append via **stop-respawn** + leave + migration). **Slice 2** (SIGHUP hot-reload for
zero-drop append, Decision 3) is a deferred fast-follow, gated on a live append-during-stream test — see
"On the zero-drop claim" below for why it is not settled in-repo.

## Context

ADR 0007 Decision 1 chose "one `grid join` = one identity; repeated joins stay *separate* (status quo)".
That holds in **local** mode — the node_id is the record's random `node-<uuid>` (`cli/provider.py:131`
written, `:355` read back into `_register_engine`), and the local server holds N nodes. It is **false in
remote** mode: a remote engine's relay node_id is derived from the per-grid access-token JWT
(`remote/serve.py` `_node_id_from_token`, used where `run_remote_engine_from_record` sets `node_id`), so
**every** `grid join` on one grid registers under the **same** node_id. `relay.register_node` is
`PUT /nodes/{node_id}` — a full replace (`remote/relay.py:82`), authorized only for the token's own node.

Maintainer-observed symptoms of running `grid join` twice on one remote grid:
- Two detached `__remote-engine` processes register the same node_id → **last-win clobber** of advertised
  models/caps.
- Both long-poll the same per-provider queue → a job for model A can be claimed by the process serving only
  B → *"no engine serves model A" / "No providers available"*.

The record's random `node_id` (`cli/remote_provider.py:202`) is **dead** in remote — the serve loop ignores
it. So remote has, and can only have, **one identity per grid**. Repeated `grid join` must be **additive**
(append engines to that one identity), never "separate".

**Cross-machine is out of scope but fine:** the per-grid tokens are fetched with a stable per-machine
`device_id` (`remote/credentials.py:61` → `control_plane.fetch_tokens(session_token, device_id)`), so the
node_id is per-*(device, grid)* — two boxes get distinct node_ids and serve as separate identities. The
singleton is per-box, which matches; it does not (and need not) coordinate across machines.

Secondary ask: `--all` misses a llama.cpp on a non-default port. Resolved by the now-appendable `--at`
(`grid join --all` then `grid join --at http://127.0.0.1:9000 -m foo`); `--all` keeps scanning only the
default ports. No `--scan-port` flag.

## Decisions

1. **Remote is one identity per grid; the run record is a singleton.** The remote record is keyed by a
   constant (`"remote"`) — one file `engines_dir(network_id)/remote.json` — replacing the random
   `engine-{uuid}` at `cli/remote_provider.py:74`. At most one `__remote-engine` process per grid. `--name`
   in remote becomes the grid-page **display name** only (a new `meta_name` record field read by
   `remote/serve.py:_meta`), not the record key and not a way to mint a second identity (impossible under
   one-token-one-node). **Local `--name` is unchanged.**

2. **`grid join` is additive (auto-merge).** When a live singleton record exists, merge the newly-resolved
   spec(s) into its `engines` union: dedup by `endpoint_url`, recompute top-level `models` (union) and
   `endpoint_url` (sole url or `None`), write the merged record. **External-only guard** (ADR 0007 D4): a
   union with >1 engine may not contain a built-in `--serve` spec (no `endpoint_url`); appending anything
   that needs a launch or a media/ComfyUI bring-up mid-serve is rejected with "leave + re-join" guidance.

3. **The live serve process hot-reloads in place; re-register is non-destructive.** `grid join`/`grid leave`
   rewrite the record and signal the running process (**SIGHUP**, not mtime — avoids the spurious
   pid-double-write reload, coarse-mtime misses, and ~2s latency). A reload thread (like `_heartbeat_loop`)
   rebuilds routing from the record (`_bring_up_engines` → `_build_routing`, probing **only newly-added**
   engines), atomically swaps an immutable snapshot into `_ServeState`, then re-`register()`s the union
   (swap **then** register). The poll loop and heartbeat never stop.

   **On the zero-drop claim — NOT settled in this repo.** The relay *server* that would guarantee re-register
   preserves in-flight work lives in a **separate** repo (`grid-src/grid_cli/private_server/`, the upstream
   this was ported from) — its `node_update` upserts on `role=provider` and only cleans up in-flight on
   `role=consumer`/`DELETE`/prune, which is encouraging, but it is **not** the hosted relay and not verifiable
   here. In-repo the evidence is *asymmetric*: `unregister_node` flips role→consumer specifically to make the
   relay drain (`remote/relay.py:88-91`), corroborating that a `role=provider` re-register does **not** drain
   *queued* work — but nothing in-repo corroborates in-flight **stream** survival across a re-register. So
   hot-reload's zero-drop is gated on a live 2-host append-during-stream test before Slice 2 ships.

4. **Concurrency correctness (from the design audit) — required, not optional.**
   - **Snapshot reads (F4):** `_ServeState.route()`/`upstream_model()` currently read `self._routes` several
     times unlocked (`serve.py:465-467`). Bind the map once per call, or have the reload swap a single
     immutable snapshot attribute readers load exactly once — else a mid-swap read KeyErrors and the job is
     dropped **without** a `submit_error` (consumer hangs to timeout).
   - **Lock discipline (F5):** `_lock` is a plain (non-reentrant) `Lock`; `register()`→`token()` takes it.
     `apply()` must swap under `_lock`, **release**, then register. A dedicated register lock serializes the
     reload's register with heartbeat's 404-reregister so neither PUTs a torn snapshot.
   - **Reload isolation (F6):** wrap the reload body so a failure (unreachable appended `--at`) logs and
     keeps the watcher thread alive; build fully then swap (never half-apply); probe only new engines.

5. **Migration + merge safety.**
   - **Migration (F2):** `read_records` keys by the record's `engine_id` field, so a pre-change
     `engine-<uuid>.json` is a different key from `remote.json`. Adopt/stop **any** live legacy remote
     record for the grid before creating/using the singleton — else the old process stays live and the two
     clobber on the same token node_id (the bug, resurrected on upgrade).
   - **Merge race (F3):** `atomic_write_json` makes each write atomic but does not serialize
     read-modify-write across two concurrent `grid join` processes. flock the record (or a sibling lockfile)
     around read-merge-write.

6. **`grid leave`.** Bare `grid leave` → SIGTERM teardown (unchanged). `grid leave --engine <endpoint_url|
   label>` removes the matching spec (key on `endpoint_url`; label only when set+unique) → same reload path
   shrinks the union; removing the last spec → full teardown (never reload-to-empty).

## Slice 1 as built (respawn)

Decisions 1/2/5/6 shipped; Decision 3 is deferred (Slice 2). The update mechanism is **stop-respawn**, not
hot-reload: `cli/remote_provider._respawn_identity` stops the prior process(es) then spawns one fresh
`__remote-engine` from the merged record. Consequences and the review-hardened behavior:

- **Respawn is not atomic.** The prior process is stopped before the replacement is confirmed alive, so each
  append interrupts in-flight requests and, if the fresh process dies at start-up, the grid is left not
  serving — surfaced as a clear `SystemExit` with the log tail (both join-append and leave-shrink go through
  the shared helper, so neither can silently claim success). Slice 2's hot-reload removes the interrupt.
- **Never spawn over a live prior.** `run_records.terminate_pid` now returns whether the process is confirmed
  gone (and `pid_alive` treats `EPERM` as alive); `_respawn_identity` aborts before spawning if a prior can't
  be stopped, rather than starting a second child that would clobber the shared node_id.
- **Additive merge is by engine, model-aware.** `_merge_engines` unions models into an engine already in the
  union (re-joining a known URL with a new `-m` model adds it, not drops it) and treats the built-in `--serve`
  engine as keyed by its model set, so a re-join with nothing new (or only a `--name`/`--bundle` change) is a
  true no-op / respawn-to-apply — never a silent drop. Appending onto a `--advertise-as` identity is rejected
  (aliases are single-engine, ADR 0007 D4).

## Considered options (reload mechanism)

- **Stop + respawn (Slice 1, shipped).** No new threads/locks; drops in-flight requests + a re-register gap
  (seconds, incl. re-probe) on each append. Ships the clobber fix immediately; hot-reload replaces it later
  with **no change to the singleton core** (Decisions 1/2/5/6 are shared).
- **SIGHUP hot-reload (Slice 2, deferred).** Zero-drop, but rests on the unverified-in-repo relay behavior
  (Decision 3) and adds the concurrency surface in Decision 4. Gated on the live test.
- **mtime-watch trigger.** Rejected in favor of SIGHUP (spurious pid-double-write reload, coarse-mtime
  misses, ~2s latency).

## Consequences

- local stays local-only / byte-identical; single-engine remote serve unchanged; remote reaches the relay
  only via `remote/*`; tokens never enter the record or logs.
- Tests: update `test_remote_join_at_serves_external_engine` (`test_local_cli.py:1249`) and
  `test_remote_join_died_cleans_up_record` (`:1425`); add the missing coverage — double-join/append, union
  dedup, external-only reject, `--name`→display-name, migration sweep, concurrent-merge, hot-reload swap +
  register, reload-failure isolation, leave-shrink, leave-last-teardown. (The suite currently has **no**
  double-join test — that gap let the bug through.)
- One live 2-host test (append `--at` during an in-flight stream; assert the stream completes) is the final
  confirmation for Decision 3 on the hosted relay.
