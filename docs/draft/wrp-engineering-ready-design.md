# Design spec — WRP "engineering-ready" features, adapted to the `grid` CLI

Status: draft (proposed)
Date: 2026-07-06
Source: *The Workload–Router–Pool Architecture for LLM Inference Optimization* (vLLM Semantic
Router project, March 2026) — the six opportunities the paper tiers as **engineering-ready**
(Opp 2, 5, 8, 11, 15, 20), mapped onto what this repo can actually build.

---

## 1. Context and scope

The paper's engineering-ready opportunities all assume a *semantic router with fleet-wide
visibility*. Grid has two in-repo router surfaces and one out-of-repo one:

| Surface | Where | What it decides today |
| --- | --- | --- |
| **Local grid server** | `local/server.py` | model → engine, least-loaded (`_choose_engine`, `_load_score`) |
| **Provider serve loop** | `remote/serve.py` | relay job → local engine (`_ServeState.route`), N poll workers (`--max-concurrency`, ADR 0009) |
| **Hosted relay** | *not in this repo* | remote consumer → provider routing, membership, billing, queuing |

Everything relay-side is out of scope (ARCHITECTURE.md: "the hosted backend … stay out of this
repo"). This spec therefore lands each feature at the local server, the provider serve loop, or
the consumer CLI — and explicitly parks the parts that need relay changes.

### 1.1 Applicability map (the six features)

| # | Paper opportunity | Grid adaptation | Lands in | Dropped / parked |
| --- | --- | --- | --- | --- |
| F1 | Opp 2 — HaluGate model-reputation routing | **Outcome-aware engine routing**: per-(engine, model) success/latency reputation feeding engine choice | `local/server.py`; heartbeat enrichment in `remote/serve.py` | Hallucination/factuality verdicts (no classifier in a thin CLI); semantic domains |
| F2 | Opp 5 — Runtime token-budget enforcement | **Provider token budgets + per-job clamps**; consumer `--max-tokens` | `remote/serve.py`, `cli/parser.py`, `cli/remote_provider.py` | Tool-catalog shaping, compression, model downgrade (grid doesn't rewrite bodies or pick models for consumers) |
| F3 | Opp 11 — Follow-the-sun pool rebalancing | **Serve windows + adaptive concurrency**: the one pool-capacity knob grid owns, recomputed from local telemetry | `remote/serve.py` | Pool-boundary (B*) math — pools live relay-side |
| F4 | Opp 15 — KV-cache retention directives | **Session/prefix affinity routing**: keep a conversation on the engine whose KV cache is warm | `local/server.py` | RetentionDirective API (no engine exposes one); relay-side affinity needs a relay header |
| F5 | Opp 20 — Governance-as-code | **`grid policy`**: a versioned policy file + `check` (satisfiability-lite), enforced at join/serve/consume | new `shared/policy.py`, `cli/policy.py`, hooks in join/serve/request paths | A DSL/compiler — JSON keys instead |
| F6 | Opp 8 — Request-level RBAC | **Opt-in local grid keys (admin/app roles)** + provider-side job guards | `local/server.py`, `cli/grid.py`, `cli/provider.py` | Per-caller enforcement on providers — relay jobs carry no caller identity (see §9) |

### 1.2 Non-goals

- No semantic classification, no hallucination detection, no ML in the CLI.
- No change to the relay wire beyond **additive, optional** fields (feature-flagged; see §8 risks).
- Local mode stays **default-open, in-memory, LAN-only, stateless** (ARCHITECTURE.md design
  constraints). Every local-mode addition here is either in-memory or strictly opt-in.
- No new long-lived daemons; no IPC beyond the existing run-record file channel.

### 1.3 Invariants honored (from ADRs / ARCHITECTURE.md)

1. `local/` and `remote/` never import each other; shared logic goes to `shared/`.
2. Run records carry **non-secret routing only**; tokens stay in `credentials.toml` (0600).
3. Every new command is classified in `cli/dispatch.py` (`AGNOSTIC` / `REMOTE_HANDLERS` /
   `REMOTE_ONLY`) — the classification test must be extended, never bypassed.
4. Surface vocabulary: **grid / engine / model / app**; `node_id`, `provider`, `consumer` stay
   internal.
5. `--max-concurrency` semantics from ADR 0009 (one poll worker per slot, `_MAX_CONCURRENCY=256`
   cap, bounded drain, main thread a pure waiter) are preserved; F3 only varies N at runtime.
6. One bad job never kills the serve loop; one dead loop stops the engine loudly (`_supervise`).

---

## 2. Shared foundation

Three small pieces are used by several features. Build these first.

### 2.1 `shared/outcomes.py` — outcome records and rolling stats (pure module)

Used by F1 (routing scores), F2 (token accounting), F3 (controller inputs).

```python
@dataclass
class Outcome:
    ts: float
    model: str
    kind: str        # "ok" | "engine_error" | "timeout" | "transport_error" | "rejected"
    status: int | None
    ttft_ms: float | None      # streams: first chunk; whole: total duration
    duration_ms: float
    tokens_in: int | None      # exact from `usage` when present, else estimate
    tokens_out: int | None
    estimated: bool            # True when token counts are estimated
```

`OutcomeStats` (per `(engine, model)` key) maintains, in memory:

- `ema_err` — EMA (α = 0.2) of `1.0` for *hard* failures, `0.0` otherwise.
  Hard = transport error, timeout, HTTP 5xx, or 404-model-not-found (a misadvertisement).
  Other 4xx are **neutral** (the request's fault, not the engine's) — recorded, not penalized.
- `ema_ttft_ms` — EMA (α = 0.2) of TTFT, streams only.
- `err_streak` — consecutive hard failures; reset on any success.
- `last_hard_failure_ts` — for cooldown.
- `hour_buckets: dict[int, TokenCounts]` — rolling 24 × 1-hour token buckets (F2); pruning on
  write; `window_tokens()` sums the live buckets.
- counters: `requests`, `failures`, `p50/p95_duration_ms` (t-digest is overkill — keep a
  fixed 256-sample reservoir and compute on demand).

Concurrency: the module is lock-free; **owners lock**. The local server mutates from a single
asyncio loop (no lock needed); the serve loop guards with the existing `_ServeState._lock`
pattern.

Persistence: none in this module. Owners decide (local server: none, matches the stateless
registry; serve loop: budget buckets persisted into the run record, §4.3).

Token estimation helper: `estimate_tokens(byte_len: int) -> int` = `max(1, byte_len // 4)`,
documented as ±30 %. Exact `usage` always wins when present.

### 2.2 Run record as a live config channel

`shared/run_records.update_record` (`shared/run_records.py:55`) already merges fields
atomically, and the serve loop already owns its record. New convention:

- The serve loop re-reads its record every **control tick** (piggybacked on the heartbeat
  cadence, `relay.HEARTBEAT_INTERVAL = 30 s`) and applies a whitelisted set of *tunable* fields:
  `max_concurrency`, `daily_token_budget`, `max_tokens_per_job`, `serve_windows`.
- `grid tune` (§7.2) is just `update_record` + user feedback; the loop picks changes up within
  one tick. No sockets, no signals, no new IPC — the record file is the channel.
- Unknown/malformed tunable values are logged and ignored (a corrupt record must not kill the
  loop — same posture as `shared/state.read_state`).

### 2.3 `shared/policy.py` — policy load/merge/eval (pure module)

See F5 (§6) for the schema. The module exposes:

```python
def load() -> Policy                 # ~/.grid/policy.json, lenient like state.read_state
def effective(grid: str | None) -> dict   # global ∪ grid override (key-wise, grid wins)
def check(policy, context) -> list[Finding]   # errors + warnings, pure
```

Precedence everywhere: **CLI flag > per-grid policy > global policy > built-in default.**

---

## 3. F1 — Outcome-aware engine routing (paper Opp 2)

**Paper idea kept:** maintain a per-(model, domain) reputation table from response verdicts;
route to the best performer; sliding window so stale reputation ages out; "silent drift"
detection. **Translation:** grid can't judge factuality, but it *observes* every forward's
transport outcome, status, and latency at `_proxy_openai` — enough for a reputation table keyed
`(engine, model)` where "verdict" = did the engine actually serve the request well.

### 3.1 Behaviour

`local/server.py` today sorts candidates by `(active_tasks, last_heartbeat)`
(`_active_engines`, `local/server.py:322`) and takes the head (`_choose_engine`,
`local/server.py:326`). New selection:

```
score(engine) = active_tasks
              + W_ERR  * ema_err            # W_ERR  = 4.0
              + W_TTFT * min(ema_ttft_ms / 5000, 1.0)   # W_TTFT = 1.0, streams only
```

plus a **cooldown gate**: an engine with `err_streak >= 3` is skipped for
`COOLDOWN_SECONDS = 30` after its last hard failure — *unless* it is the only candidate for the
model (degraded service beats no service; the paper's "route around, don't block").

Weights are constants in v1 (no config surface). Rationale: `W_ERR = 4.0` means a fully
failing engine (ema_err → 1.0) scores as if it had 4 extra in-flight tasks — enough to lose to
any healthy peer, small enough that one blip (ema_err ≈ 0.2 after a single failure) only
tie-breaks.

### 3.2 Recording

`_proxy_openai` (`local/server.py:187`) and `_proxy_media` wrap the forward:

- t0 before send; streams: stamp TTFT at the first chunk of `aiter_raw()`; whole: TTFT = total.
- Outcome classification exactly as §2.1; recorded in `app.state.stats` keyed
  `(node_id, model)`.
- Token counts: parse `usage` from whole JSON responses; streams: count `data:` payload bytes
  in passing (the generator already touches every chunk) and estimate. Never buffer, never
  delay chunks.

In-memory only — a grid restart forgets stats, exactly like it forgets nodes. This is the
documented local-mode posture, not a limitation to fix.

### 3.3 Surfacing

- `/nodes/discover` gains per-engine `"stats": {"requests": n, "success_rate": s,
  "err_streak": k, "p50_ms": …, "p95_ms": …, "cooldown": bool}` (computed, not stored on
  `Node`).
- `grid engines` prints one extra detail per engine:
  `health: 98% ok · p50 412ms · 1.2k reqs` and marks `DEGRADED (cooldown)` when gated.
  `grid engines --json` carries the raw object. No new verb.

### 3.4 Remote slice (minimal)

The provider loop cannot re-route (the relay picked it), but it already assembles heartbeat
`load` in `_ServeState.load()` (`remote/serve.py:498`). Add, behind env flag
`GRID_HEARTBEAT_STATS=1` (default off until verified against the relay, §8):

```json
"outcomes": {"<model>": {"requests": n, "success_rate": s, "p50_ms": m}}
```

This is the paper's "feed per-model quality back into routing policy" — the routing policy
lives relay-side, so grid's job is only to *emit the signal*.

### 3.5 Acceptance

- Two engines serve `m`; engine A returns three consecutive 5xx → the next request routes to B
  even when A is less loaded; after 30 s + one success, A is eligible again.
- 4xx responses do not change routing order.
- Sole engine failing → still selected (with `DEGRADED` visible in `grid engines`).
- No measurable added latency on the proxy hot path (stats update is O(1), no I/O).

---

## 4. F2 — Token budgets and job guards (paper Opp 5)

**Paper idea kept:** the router is the natural budget-enforcement point because it sees actual
token flow; graduated response; P90-style headroom so legitimate long jobs survive.
**Translation:** the serve loop is where a provider's GPU gets spent (and, on priced grids,
billed via `grid price set`) — today it forwards without reading `usage` at all. The graduated
response compresses to what a proxy that must not rewrite results *can* do: **clamp → warn →
pause claiming** (never kill in-flight work).

### 4.1 CLI surface

New remote-only `grid join` flags (added to `cli/parser.py` `_add_engines`, mirrored into the
run record in `cli/remote_provider.py:_build_record`, and appended to
`_REMOTE_ONLY_JOIN_FLAGS` in `cli/provider.py:35` so local mode rejects them):

```
--max-tokens-per-job N    Clamp each job's max_tokens to N before forwarding (default: none)
--daily-token-budget N    Stop claiming new jobs after N tokens (in+out) in a rolling 24h (default: none)
```

Consumer side, both modes: `grid chat --max-tokens N` → body `max_tokens` (today
`cli/remote_request.py:75` builds `{model, messages}` only; local `cli/request.py` likewise).

### 4.2 Enforcement in `handle_job` (`remote/serve.py:608`)

Ordered, before `enter_inference`:

1. **Reject oversized bodies**: serialized `body` > `MAX_JOB_BODY_BYTES` (default 10 MB, text
   endpoints only — media already streams files) → `_try_submit_error(state, txn,
   "request too large for this engine")`. (Also part of F6's guard story.)
2. **Clamp**: if `max_tokens_per_job` is set → `forward_body["max_tokens"] =
   min(cap, body.get("max_tokens") or cap)`. The engine enforces; the consumer sees a standard
   `finish_reason: "length"`. This is the only body mutation, and it follows the existing
   precedent (the loop already rewrites `model` for `--advertise-as`, `remote/serve.py:653`).
3. **Budget gate** is *not* per-job — exhaustion pauses claiming (§4.4), so a claimed job is
   always served. No job is half-punished.

### 4.3 Accounting

- `_forward_whole` (`remote/serve.py:701`): parse the engine's JSON for
  `usage.prompt_tokens` / `usage.completion_tokens`; fall back to `estimate_tokens(len(bytes))`.
- `_forward_stream` (`remote/serve.py:717`): wrap `engine_resp.iter_bytes()` in a counting
  generator (bytes flow through untouched); estimate tokens from cumulative `data:` payload
  size; if a final SSE chunk carries `usage` (llama.cpp with `include_usage`, vLLM), prefer it.
  Counts are marked `estimated`.
- Buckets: 24 rolling hour buckets per engine identity (§2.1), aggregate across models
  (budgets protect the *box*, not a model).
- Persistence: buckets flushed into the run record (`update_record(..., budget_buckets={...},
  budget_updated_at=ts)`) every control tick and on clean shutdown; reloaded on start. A
  restart therefore cannot reset a budget — the paper's enforcement survives the trivial
  bypass.

### 4.4 Graduated response

| Budget state | Trigger | Action |
| --- | --- | --- |
| `ok` | < 90 % of window budget | — |
| `low` | ≥ 90 % | serve-loop log line; heartbeat `load["budget"] = {"state": "low", "used": u, "limit": l}` |
| `exhausted` | ≥ 100 % | **pause claiming** (§4.5); heartbeat `budget.state = "exhausted"`; resume automatically when the rolling window frees ≥ 5 % headroom |

### 4.5 Pause/resume mechanics (shared with F3)

Pausing must tell the relay to stop routing here, not just stop polling (else jobs queue
against a dead slot). Grid already has the primitive: `unregister_node`
(`remote/relay.py:88`) flips the node to `role: "consumer"`, which the relay treats as drain.

- `_ServeState` gains `desired_role: "provider" | "consumer"` and `pause(reason)` /
  `resume()`; `register(state)` (`remote/serve.py:570`) sends `role=state.desired_role`.
- Workers check a `state.paused` event between polls: paused workers park on
  `resume_event.wait(5)` instead of long-polling.
- `heartbeat_once` (`remote/serve.py:594`) keeps running while paused (keeps the node fresh
  and the budget state visible); its 404 → `register(state)` path re-registers **with the
  current desired role**, so a prune during a pause cannot resurrect a claiming provider.
- In-flight jobs always finish and submit (drain semantics of ADR 0009 untouched).

### 4.6 Acceptance

- Whole + streamed jobs both count; `grid engines --json` (remote) shows
  `budget: {used, limit, state}` read from the run record.
- Set a 1 000-token budget, run chat jobs until exhausted → relay-visible role flips to
  consumer, no new jobs claimed, in-flight job completes and submits; window rolls → provider
  role restored without operator action.
- `--max-tokens-per-job 64` → engine receives `max_tokens ≤ 64` even when the consumer sent
  4 096 or nothing.
- Kill -9 the serve loop mid-window, rejoin → budget usage carries over (record reload).

---

## 5. F3 — Serve windows and adaptive concurrency (paper Opp 11)

**Paper idea kept:** capacity should track the workload's clock — recompute periodically from a
sliding window of telemetry and reassign capacity *in software*; hysteresis against
oscillation (the paper cites Autopilot's). **Translation:** grid owns exactly one pool-capacity
knob — how many poll workers a provider runs (`max_concurrency`, ADR 0009) and whether it
serves at all. Provider boxes are often workstations: "follow the sun" here literally means
*serve hard at night, lightly at 2pm*.

### 5.1 CLI surface

```
--serve-window "22:00-08:00"     Serve only inside these local-time windows (repeatable).
--max-concurrency auto[:F-C]     Adapt worker count between floor F and ceiling C (defaults 1–8).
```

`--max-concurrency N` keeps its exact ADR 0009 meaning. `auto` ceiling is clamped by
`_MAX_CONCURRENCY` (`remote/serve.py:40`). Both are run-record tunables (§2.2), so
`grid tune --serve-window …` retunes a live engine within one tick.

### 5.2 Windows

- Parse `HH:MM-HH:MM`, local time, midnight-crossing allowed; several windows OR-ed.
- Evaluated each control tick: outside → `pause("window")`, inside → `resume()` — the same
  §4.5 machinery (role flip + park). Window edges therefore *drain, never kill*.

### 5.3 Adaptive controller (AIMD)

Runs on the control tick (30 s), only when mode is `auto`:

- **State:** `target` (current worker target), `best_p95` (min windowed p95 job duration seen,
  floor 1 s), last-tick outcome window from `OutcomeStats`.
- **Increase** `target += 1` (up to ceiling) iff the last tick had ≥ 1 completion, zero hard
  failures, and windowed p95 ≤ 1.5 × `best_p95`.
- **Decrease** `target = max(floor, ceil(target / 2))` on any timeout, any engine 5xx, or
  p95 > 2 × `best_p95`; then hold (no increase) for a 90 s cooldown.
- Rationale: the congestion signal is *job latency degradation at the engine* — GPU-utilization
  telemetry is confounded (our own jobs pin the GPU; that's healthy, not overload).

Worker scaling mechanics (extends `_serve_loop`, `remote/serve.py:793`):

- Workers get indices; each checks `self.index < state.target_workers` at the top of its poll
  iteration and exits cleanly when above target (shrink latency ≤ one 35 s poll cycle).
- Growth spawns fresh threads under `_supervise` (unchanged supervision semantics).
- On any target change: re-register (`PUT /nodes/{id}` is idempotent and already re-sent on
  heartbeat-404) so the relay's advertised `max_concurrency` tracks reality; heartbeat gains
  `load["capacity"] = {"target": t, "limit": c}`.

### 5.4 Acceptance

- `--serve-window "22:00-08:00"` at 14:00 → engine registers, immediately pauses (role
  consumer), logs `outside serve window — paused`; at 22:00 (simulated clock) resumes.
- `auto:1-8` against a healthy fast engine → target climbs one step per tick to 8; inject
  timeouts → halves and holds ≥ 90 s; N never exceeds the ceiling nor `_MAX_CONCURRENCY`.
- ADR 0009 suite stays green with `--max-concurrency N` (fixed mode byte-identical).

---

## 6. F4 — Session/prefix affinity routing (paper Opp 15)

**Paper idea kept:** the router should use zero-cost identity signals (system-prompt
fingerprint, session continuation) to protect KV-cache locality — the engine can't infer this
from the token stream, the router can. **Translation:** grid can't emit retention directives
(no engine API), but it *chooses the engine*, and today it re-rolls that choice every turn:
turn 2 of a conversation can land on a different engine than turn 1, cold-starting the entire
prefix. llama.cpp (`cache_prompt`, on by default) and vLLM (automatic prefix caching) both
reward stickiness with large TTFT wins — grid just has to stop defeating them.

### 6.1 Behaviour (`local/server.py` only, v1)

- Compute a **conversation fingerprint** in `_proxy_openai` after the body parse (no extra
  parse): `sha256(model + "\0" + system_message_content + "\0" +
  first_user_message_content).hexdigest()[:16]`. Multi-part content is serialized with
  `json.dumps(..., sort_keys=True)`; missing messages hash as empty. Turns 2..n of one
  conversation repeat their first messages verbatim, so the fingerprint is stable per
  conversation without any client cooperation — the paper's "system-prompt fingerprint,
  zero classification cost".
- `app.state.affinity`: LRU `{fingerprint: (node_id, ts)}`, cap 1 024, TTL 900 s.
- Selection (extends `_choose_engine`): among the F1-scored candidates, pick the affinity
  engine iff it (a) is a live candidate for the model, (b) is not in cooldown, and
  (c) `active_tasks ≤ min_active + SLACK` (SLACK = 2). Otherwise take the score head and
  update the map. The slack term is the paper's sunk-cost-vs-queueing trade, reduced to a
  constant: a warm cache is worth waiting behind ≤ 2 tasks, not behind a pile-up.
- The forwarded body stays byte-identical (affinity only *reads*), preserving the documented
  "raw body forwarded unchanged" contract.

### 6.2 Explicitly parked

Remote-mode affinity requires the relay to accept a session hint and the consumer response to
identify the serving engine; neither exists on the wire this repo sees. If/when the relay
exposes them, the consumer slice is small: persist `{session → provider}` client-side and set
the existing `X-Target-Provider` header (`remote/relay.py:287`) on follow-up turns. Parked in
§9, not designed further here.

### 6.3 Acceptance

- Two engines serve `m`, both idle: three consecutive requests sharing a system+first-user
  prefix land on the same engine; a request with a different prefix may land elsewhere.
- Affinity engine loaded with `min_active + 3` tasks → conversation moves (slack exceeded).
- Affinity engine in F1 cooldown → conversation moves; map updates to the new engine.
- TTL expiry (fake clock) → entry dropped, no stale node_id ever returned to a dead engine.

---

## 7. F5 — Governance-as-code: `grid policy` (paper Opp 20)

**Paper idea kept:** operator constraints as reviewable *code*, validated for satisfiability
before they bite at runtime, then enforced at dispatch. **Translation:** JSON policy keys
instead of a DSL — the repo already speaks versioned JSON everywhere (`state.json`, run
records); the enforcement points already exist as hardcoded guards (`_ALLOWED_ENDPOINTS`,
`_REMOTE_ONLY_JOIN_FLAGS`) that this feature generalizes without replacing.

### 7.1 Policy file

`~/.grid/policy.json` — atomic writes via `shared/jsonio`, 0600, lenient reads
(corrupt → `{}` + warning, self-heals on next `set`; same posture as `shared/state.py`).

```json
{
  "version": 1,
  "global": {
    "serve.models": ["qwen3*", "llama3*"],
    "serve.media": false,
    "serve.max_tokens_per_job": 4096,
    "serve.daily_token_budget": 5000000,
    "serve.windows": ["22:00-08:00"],
    "serve.require_price": true,
    "consume.models": ["*"],
    "consume.max_price_per_mtok": 2.0,
    "local.require_key": false
  },
  "grids": {
    "home":     {"serve.media": true},
    "<network_id-or-name>": {"serve.daily_token_budget": 20000000}
  }
}
```

Key semantics (v1 — the complete set; anything else is a `check` error):

| Key | Type | Enforced at |
| --- | --- | --- |
| `serve.models` / `serve.models_deny` | globs (`fnmatch`) | join-time: filters the advertised set (empty result = error); serve-time: `handle_job` re-checks (defense in depth, policy may have tightened since join) |
| `serve.media` | bool | join-time: rejects `--media` / detected media |
| `serve.max_tokens_per_job` | int | default for the F2 flag |
| `serve.daily_token_budget` | int | default for the F2 flag |
| `serve.windows` | list | default for the F3 flag |
| `serve.require_price` | bool | remote join-time: `relay.list_model_prices` must cover every advertised model, else refuse with the missing list ("don't serve for free by accident") |
| `consume.models` | globs | `grid chat/image/edit/video`: reject a disallowed `-m` before any network call |
| `consume.max_price_per_mtok` | float | remote consume: fetch the grid's price table (only when the key is set), refuse when the model's `input_rate` or `output_rate` exceeds the ceiling |
| `local.require_key` | bool | default for `grid up --require-key` (F6) |

### 7.2 CLI surface

```
grid policy show [--json]                 # effective global + per-grid table
grid policy set <key> <value> [--grid G]  # typed parse per key; unknown key = error
grid policy unset <key> [--grid G]
grid policy check [grid]                  # satisfiability pass; exit 1 on errors

grid tune [grid] --engine <id> [--max-concurrency N|auto[:F-C]]
          [--daily-token-budget N] [--max-tokens-per-job N] [--serve-window W]...
```

- `policy` is **AGNOSTIC** in `cli/dispatch.py` (file edits work in either mode; remote-only
  keys simply don't fire locally). `tune` is **REMOTE_ONLY** v1 (its knobs are all remote);
  it writes run-record tunables (§2.2) and prints what the live loop will pick up.
- Both added to the dispatch classification sets *and* the classification test.

### 7.3 `grid policy check` — satisfiability-lite

Pure evaluation in `shared/policy.check` over a context assembled by the CLI:

- **Errors:** unknown key; type mismatch; unparseable window; `serve.models` ∩ (advertised or
  detected models) = ∅; `consume.models` = ∅; `require_price` true with unpriced advertised
  models (remote, needs the grid reachable); per-job cap > daily budget.
- **Warnings:** windows cover 0 h or 24 h (likely a mistake); price ceiling below every live
  model's price (nothing consumable); deny-glob shadowing an allow-glob.
- Output mirrors the paper's compile-time gate: every finding names the key, the scope
  (global/grid), and the fix.

### 7.4 Enforcement wiring (exact hook points)

| Hook | File anchor | Check |
| --- | --- | --- |
| local join | `cli/provider.py:cmd_join` (52) | `serve.models` filter, `serve.media` |
| remote join | `cli/remote_provider.py:cmd_remote_join` (37) | same + `serve.require_price`; resolved defaults written into the record so the detached loop needs no policy read on the hot path |
| serve loop | `remote/serve.py:handle_job` (608) | model allow re-check; §4.2 guards |
| local consume | `cli/request.py` handlers | `consume.models` |
| remote consume | `cli/remote_request.py:_resolve` callers (71, 136) | `consume.models`, `consume.max_price_per_mtok` |
| server start | `local/runtime.py` → `create_app` | `local.require_key` (F6) |

---

## 8. F6 — Request-level auth for the local grid (paper Opp 8)

**Paper idea kept:** "the model is both decision-maker and enforcer" → put an independent
authorization check at the unique point that sees identity + intent *before execution*, in
block/rewrite duality, defaulting to the narrowest role. **Translation:** grid proxies chat
rather than executing tools, so the enforcement point is the **grid server's API surface** —
which today is fully open on the LAN: any peer can consume every model *and* silently
`DELETE /nodes/{id}` your engines or register a rogue engine that then receives everyone's
prompts. Remote mode already has membership roles relay-side; the local server has nothing.

### 8.1 Design (strictly opt-in)

`grid up --require-key` (local-only flag; remote `up` rejects it, mirroring the
`_reject_local_only_flags` pattern):

- On create, generate two keys (`secrets.token_urlsafe(32)`): `admin_key`, `app_key`; store in
  the grid's `config.json` (`local/config.py:13`; `jsonio` already writes 0600) under
  `"auth": {"required": true, "admin_key": …, "app_key": …}`.
- FastAPI middleware in `create_app` when auth is on — role matrix:

| Endpoint | open (auth off) | `app_key` | `admin_key` |
| --- | --- | --- | --- |
| `/`, `/grid/info` | ✓ | ✓ | ✓ (always open; `auth_required` is reported here so clients can self-diagnose) |
| `/v1/*`, `/nodes/discover` | ✓ | ✓ | ✓ |
| `POST/PUT/DELETE /nodes*`, `/nodes/heartbeat` | ✓ | ✗ 403 | ✓ |

- 401 (missing/garbled) vs 403 (wrong role) with OpenAI-shaped error bodies
  (`_openai_error` reused).
- Key distribution: same-box commands read `config.json` transparently; cross-box `grid join`
  / `grid chat --grid <url>` accept `--key` or `GRID_API_KEY` env. `grid info --env` prints
  `OPENAI_API_KEY=<app_key>` when auth is on — the dummy key becomes a real one, and existing
  OpenAI SDK apps work unchanged.
- Engine heartbeat loop (`cli/provider.py:_run_engine`) sends
  `Authorization: Bearer <admin_key>` from its record-resolved config; the run record itself
  stays secret-free (the key is read from `config.json` at loop start — same box; cross-box
  external engines pass `--key`, stored nowhere, exported into the child's env).

### 8.2 Provider-side guards (what RBAC means without caller identity)

Relay jobs carry `{transaction_id, endpoint_path, body, is_stream, inference_timeout_seconds}`
— **no caller identity**, so per-caller rules are impossible here (parked, §9). What ships:
the F2/F5 `handle_job` guards (endpoint allowlist — already present as `_ALLOWED_ENDPOINTS` —
plus model allowlist, body-size cap, `max_tokens` clamp). This is the paper's "rewrite mode":
strip/clamp before the engine ever sees it, fail the job cleanly otherwise.

### 8.3 Acceptance

- Default `grid up`: byte-identical behaviour, `auth_required: false` in `/grid/info`.
- `--require-key`: unauthenticated `DELETE /nodes/x` → 401; app-key → 403; admin-key → 200.
- `OPENAI_API_KEY` from `grid info --env` works against `/v1/chat/completions` with a stock
  OpenAI SDK.
- ARCHITECTURE.md "local is unauthenticated" paragraph updated to "unauthenticated by
  default; `--require-key` opts a grid into key auth".

---

## 9. Parked — relay-dependent (explicit, so nobody designs against a wall)

| Idea | Missing wire piece |
| --- | --- |
| Remote session affinity (F4) | relay support for a session-hint header + serving-engine identity in responses |
| Per-caller RBAC / budgets / reputation on providers (Opp 8/9 full form) | caller identity claim in the poll job payload |
| Relay-side reputation routing on F1 signals | relay reading `load.outcomes` (grid only emits, flagged) |
| KV retention directives proper (Opp 15 full form) | any engine exposing a retention API |

Each is additive to this design: the emit points (`heartbeat`, `consumer_headers`) and the
stats/policy stores are already where those extensions would plug in.

---

## 10. Delivery plan

**Phase 1 — foundations + pure-CLI wins (no wire changes)**
`shared/outcomes.py`, `shared/policy.py` · F1 local routing (score + cooldown + `grid engines`
health) · F4 affinity · `grid chat --max-tokens` · `grid policy show/set/unset/check` with
consume gates and join-time serve gates · F2 `--max-tokens-per-job` clamp.

**Phase 2 — provider guardrails**
F2 accounting + budgets + run-record persistence + pause/resume role flip · F3 serve windows ·
`grid tune` (record channel) · budget/window state in `grid engines`.

**Phase 3 — adaptive + auth**
F3 AIMD `auto` concurrency + capacity heartbeat field · F1 heartbeat outcome enrichment
(flagged) · F6 `--require-key` + middleware + key plumbing.

Suggested ADRs: `0010-outcome-aware-local-routing`, `0011-provider-budgets-and-pause`,
`0012-adaptive-serve-capacity`, `0013-policy-and-local-auth`.

## 11. Testing strategy

- **Unit (pure):** outcome EMA/streak/buckets math; token estimator; window parser
  (midnight-crossing, overlaps); AIMD controller against a scripted timeline (fake clock);
  policy merge/precedence/check findings; fingerprint stability across turns and content
  forms.
- **Local server (FastAPI TestClient, extends `tests/test_local_cli.py` patterns):** routing
  prefers healthy engine after 3 hard failures; cooldown expiry; 4xx neutrality; affinity
  stickiness / slack eviction / TTL; auth matrix (401/403/200 per role per endpoint);
  default-open regression.
- **Serve loop (mocked relay, extends the ADR 0009 suite):** clamp applied to
  `forward_body`; whole + streamed token counting (usage present / absent); budget exhaustion
  → role-flip PUT observed, workers park, in-flight job still submits; window pause/resume;
  record-tunable pickup within one tick; auto mode scale-up/down + re-register; corrupt
  record/policy never kills the loop.
- **Dispatch:** classification test extended for `policy` (AGNOSTIC) and `tune` (REMOTE_ONLY);
  wrong-mode flag rejections (`--require-key` in remote, new remote-only join flags in local).
- **e2e smoke:** extend `test_remote_mode_e2e.sh` with a budgeted join and a `grid tune`
  round-trip.

## 12. Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Relay rejects unknown register/heartbeat fields | all new wire fields behind `GRID_HEARTBEAT_STATS` / omitted-when-unset; verify against staging before defaulting on |
| Streamed token estimates off by ±30 % | exact `usage` preferred whenever present; counts flagged `estimated`; budgets are provider self-protection, not billing |
| Pause role-flip races heartbeat re-register | single `desired_role` source of truth in `_ServeState`; `register()` always reads it |
| AIMD oscillation | one step per 30 s tick, 90 s post-decrease hold, `best_p95` floor |
| Affinity herds a conversation onto a hot engine | slack bound (≤ min_active + 2) + cooldown gate always win over affinity |
| Policy foot-gun (allow-list matches nothing) | `grid policy check` + join-time error naming the key; lenient loads never brick unrelated commands |
| Local auth breaks existing LAN apps | strictly opt-in; `/grid/info` always open and reports `auth_required`; `grid info --env` hands out the working key |

## 13. Open questions

1. Should F2 budgets also count *input* tokens of failed jobs (engine 5xx after a long
   prefill)? Proposed: yes — the GPU did the work (prefill cost is real).
2. `grid tune` in local mode (heartbeat interval, future local knobs) — defer until a local
   tunable exists, or classify GATED now with a local stub?
3. Does the relay tolerate frequent `PUT /nodes/{id}` re-registers from the auto controller
   (worst case every 30 s)? If not: only re-register on target *changes*, which the design
   already does — confirm rate limits with the relay team.
4. `consume.max_price_per_mtok` adds one price-table fetch per consume when set — cache it
   (`~/.grid/cache/`, 10-min TTL) or accept the latency? Proposed: accept in v1 (opt-in key).
