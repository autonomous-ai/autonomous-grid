# Dynamic resource allocator

Grid's allocator decides which configured models should be resident on which computers, then
converges the fleet toward that placement without taking a healthy last replica away. It is built
for an AI intranet where capacity is heterogeneous and partly opportunistic: an always-on GPU
server, an employee laptop, and an external inference endpoint do not have the same ownership or
safety rules.

The allocator is experimental. It defaults to **recommend** mode and does not change a process
until an operator explicitly selects **automatic** mode.

## Two cooperating loops

The global loop optimizes the grid. The local loop protects one computer. They exchange desired and
actual state, but the local decision is authoritative when they disagree.

```text
request counts, latency, queues          host telemetry and local override
                 |                                      |
                 v                                      v
        bounded demand forecast                 local protection loop
                 |                                      |
                 v                                      |
     capacity-aware placement plan                      |
                 |                                      |
                 v                                      |
        safe staged reconciliation <--------------------+
                 |
                 v
       LOAD -> WARM -> READY -> DRAIN -> UNLOAD
                 ^
                 |
       heartbeat actual state + acknowledgements
```

### Global placement loop

The global loop consumes:

- model profiles: memory, runtime/backend compatibility, replica bounds, priority, data tier,
  placement tags, failure-domain goal, pins, and cooldowns;
- host snapshots: usable memory, reserve, runtime/backend, lifecycle state, policy tags, cached and
  resident models, concurrency, queue, measured throughput and latency, memory bandwidth, compute,
  heartbeat age, and actuator ownership;
- short-horizon demand: request rate, offered concurrency, queue depth, p95 latency, errors, and
  trend.

Demand history is bounded by time buckets and contains aggregate timings and counts, not prompts or
responses. Bursts are folded into their bucket rather than truncated at a raw-request limit. A
bucketed EWMA and positive trend term make the near-term forecast. The target replica count adds
demand headroom and reacts to queue, latency, and error pressure while respecting per-model minimum
and maximum bounds. Rising demand is also projected across each model's declared load-plus-warm
time, confidence-weighted and capped to a five-minute/2× horizon, so slow cold starts begin before
the queue arrives without letting one noisy slope cause a fleet-wide load spike. Negative trends
never accelerate scale-down. The tracker also learns mature groups of models that repeatedly become
active in the same time buckets. Current demand for one group member can prewarm a quiet peer using
a confidence-weighted historical rate ratio. This requires at least three co-active buckets and
strong symmetric cosine association, caps inferred demand at twice the peer's observed peak, takes
the maximum rather than sum across at most 32 current sources, and never propagates inferred demand
transitively. Old peer-local queue, latency, and error evidence is not refreshed by a correlation.
Only configured, non-retiring model IDs create demand series, so the permissionless inference
endpoint cannot grow controller state with arbitrary names.

Placement is deterministic. Higher-priority and larger models place first; candidates then prefer
an existing ready residency, local cached weights, another failure domain, measured throughput, and
best-fit memory. Before measured throughput exists, bounded memory-bandwidth and compute estimates
break otherwise-cold ties; ready and cached bonuses remain much larger, so hardware estimates do
not cause gratuitous migration. Cost, latency, host priority, cold-start time, and throttling lower
a candidate's score. Persisted failed warm/load attempts apply a bounded per-model penalty, allowing
a healthy peer to be tried after backoff instead of selecting the same broken cache forever; the
failed node remains a fallback when it is the only feasible target. Explicit pins, per-host model
limits, compatibility policies, and a feasible minimum failure-
domain count are hard constraints. A failure-domain shortfall or capacity shortage is reported
rather than hidden by overcommit. A throttled host exposes only its configured fraction of capacity
for new placement. When greedy placement fragments capacity, a deterministic bounded backtracking
repair can evacuate and re-place several equal-priority replicas; unrelated or ineligible inventory
does not change that search budget or its result.

Request routing uses the same heterogeneous capacity evidence after placement. Among engines in the
same host-protection class that already serve the requested model, Grid compares active requests as
a fraction of each engine's effective concurrency limit rather than comparing raw request counts.
This keeps a wide-batching vLLM server from appearing busier than a narrow llama.cpp engine merely
because it safely carries more simultaneous work. Missing capacity remains conservative raw load;
zero capacity remains closed. Throttled-host priority and hard admission limits still take
precedence over this load balance.

The proxy attributes each successful response to both the engine and requested model, keeping
bounded EWMAs of end-to-end latency and completion-token throughput. Those server-owned measurements
override self-reported estimates in placement snapshots, so actual service performance eventually
supersedes the cold hardware prior. A multi-model vLLM engine is scored only with measurements for
the model being placed; its fast model cannot lend an unrelated slow model an inflated score.
Per-model evidence ramps to full placement authority over eight successful samples and then decays
with age, preventing one fast outlier or an almost-expired benchmark from dominating a mature
measurement. Current engine-reported node telemetry remains fully weighted.
Streaming responses still contribute latency even when their wire format does not expose token
usage. The measurements are private: discovery does not expose them, managed heartbeats cannot
overwrite them, and no prompt or response content is retained for allocator telemetry. A bounded
freshness window prevents an old benchmark from surviving an engine reload or long idle period;
expired measurements fall back to current hardware priors until new requests refresh them.

Recent ready replicas and a recently persisted demand watermark remain desired during the model's
scale-down cooldown. This is the global hysteresis that prevents a quiet minute—or a signaling-
server restart—from unloading a model that was just used.

### Local host-protection loop

Each participating computer evaluates its own telemetry independently of the controller:

- user activity and idle time;
- battery level and whether the machine is charging;
- thermal state and temperature when available;
- CPU, system memory, GPU memory, and load pressure;
- network availability;
- an explicit local drain, pause, or quarantine override.

The result is one of six lifecycle states:

| State | New work | Meaning |
| --- | --- | --- |
| `accepting` | yes | Normal capacity and priority. |
| `throttled` | yes, reduced | The host remains useful but yields capacity. |
| `draining` | no | Existing work may finish before a pause or unload. |
| `paused` | no | The employee or battery has reclaimed the machine. |
| `unhealthy` | no | A confirmed safety or connectivity failure requires recovery. |
| `quarantined` | no | An operator has fenced the host until explicitly released. |

Debounce avoids reacting to a one-sample spike. Drain grace protects requests already running.
Separate recovery thresholds and a recovery cooldown prevent rapid state flapping. Missing sensors
are represented as unknown, not zero; policy may ignore unknowns or conservatively throttle.

The request router enforces the local decision too: fully accepting engines are preferred over
throttled ones, and the advertised concurrency multiplier limits new admissions. Proxy-owned active
request counters survive managed heartbeats. The server returns its per-model `last_used_at`
watermark to the authenticated node, which persists the monotonic value; drain and scale-down
decisions therefore reflect work actually routed by the server and survive a server restart.
Grid-owned llama.cpp children also use a durable per-host engine key. The key is stored owner-only,
sent only in an authenticated managed registration, removed from every discovery/status response,
and added by Grid on the private upstream hop. LAN clients therefore cannot bypass the routing
fence and begin new inference directly on a child port during drain. Authenticated `/slots` probes
account for llama work at both heartbeat and final unload boundaries.

A local override outranks global desired state. Confirmed local safety can still make an override
more restrictive—for example, an operator cannot turn a critically hot machine back into an
accepting one.

## Safety invariants

The planner and reconciler keep these rules even when demand, membership, or clocks change:

1. **Never overcommit declared memory.** Reserved memory and policy headroom are removed before
   placement. Unmanaged resident workloads consume capacity first.
2. **Never place on an ineligible host.** Paused, draining, unhealthy, quarantined, stale, missing-
   heartbeat, or implausibly future-heartbeat hosts receive no new placement. Runtime, backend,
   data tier, tags, allow/deny lists, pins, and per-host model limits are enforced.
3. **Make capacity available before removing it.** Missing desired replicas are loaded and warmed
   before obsolete replicas are considered for drain.
4. **Route only admitted ready models.** Cached, loading, warming, and failed residencies never enter
   the model identity list. A draining child may retain its identity while existing requests finish,
   but the host/model admission gates remove it from active routing immediately. A managed control
   envelope claiming `ready` is inventory, not replacement proof: Grid reports it as `warming`
   until a live, admitted child-engine record for the same host/model corroborates the route.
   When a corroborated child remains live but its host is intentionally draining, paused,
   unhealthy, or quarantined, its process state remains `ready` so reconciliation can drain and
   unload it; the host fence still excludes it from routing, placement, replacement, and failure-
   domain evidence.
5. **Protect the last required replica.** An old replica is not drained until all required desired
   replacements report ready.
6. **Drain before unload.** A draining model is not unloaded while that model residency reports
   requests in flight; unrelated work on the same host does not block retirement forever. Managed
   llama ports require Grid's private engine key, so no unauthenticated direct admission can race
   the final idle check. If activity is unknown or exceeds the graceful deadline, non-force cleanup
   fails safe and leaves the proven process alive; only an explicit force stop may cut it.
7. **Respect ownership.** Pinned, manually managed, and externally managed engines may satisfy
   demand but are never actuated by Grid. An unauthenticated external record also cannot authorize
   draining the last managed baseline replica; only authenticated managed inventory can do that.
8. **Bound change.** Automatic mode has global and per-host concurrent-mutation limits. Minimum
   residency, mutation cooldown, observation timeout, and exponential failure backoff suppress
   churn and retry storms.
9. **Make retries idempotent.** Actions have stable IDs, pending equivalents are suppressed, and
   duplicate acknowledgements are harmless. Command delivery is durably marked before the response
   is returned to a node. A late success or failure may complete an action that the controller had
   cancelled; conflicting later acknowledgements are ignored.
10. **Fail honestly.** Unmet replicas and policy shortfalls remain visible in the plan; the
    allocator does not invent capacity or silently relax a hard constraint.

## Reconciliation and modes

Reconciliation separates a desired plan from side effects. Its transitions are deliberately small:

- `load`: acquire or verify the model artifact on the selected host;
- `warm`: start it and wait for a successful readiness probe;
- `drain`: stop routing new requests to an obsolete residency;
- `unload`: release memory after the residency is drained and idle.

A `warm` depends on a preceding `load` when the artifact is not cached; if that load is deferred,
the warm is deferred too. Availability actions have priority over destructive actions. Failure
backoff is tracked per action kind, host, and model, so a broken artifact does not create a tight
fleet-wide retry loop and does not block healthy targets elsewhere.

A delivered `drain` or `unload` may already be running even while its last controller record still
says `pending`. If fresh placement or host evidence makes any destructive action unsafe, the
controller withdraws the entire destructive batch for that model. Every delivered member remains
listed in `withdrawn_destructive`, and further destructive work for that model is blocked until an
authenticated terminal receipt arrives or an authoritative heartbeat proves the action's durable
postcondition (`draining`/`cached`/absent). Availability work and destructive work for other models
continue. This guard intentionally has no wall-clock timeout: if the host never returns and never
reports a terminal receipt, destructive convergence for that model remains blocked rather than
risking a late command taking the fleet below its replacement or diversity floor.

After controller restart, restored commands receive a bounded membership-recovery grace beginning
at the first reconciliation tick. If the wall clock moves backward during that grace, its in-memory
anchor is rebased to the corrected time so an absent command cannot consume the mutation budget
until the old future timestamp is reached.

| Mode | Plan and forecast | Proposed actions | Executable commands |
| --- | --- | --- | --- |
| `observe` | yes | no; drift is recorded as deferred | no |
| `recommend` | yes | yes | no |
| `automatic` | yes | yes | yes, within safety governors |

Changing away from `automatic` cancels pending commands. Removing a model profile creates a durable
retirement tombstone with a target of zero replicas. It cancels pending availability work, keeps
enough state to drain managed copies that reappear after an offline host returns, and clears the
model's demand history. Because local membership is not a durable inventory of every machine that
may later return, the tombstone remains visible until an operator creates that model profile again.

### Managed host actuator

The initial managed backend is one Grid-owned llama.cpp child process per model. The host runtime
persists its stable `host_id`, local-protection state, residencies, process handle and port, latest
plan generation, and bounded action receipts. It runs only one side effect at a time while the
heartbeat remains responsive. A restart marks an interrupted action failed, proves ownership and
readiness before adopting a surviving child, and otherwise fences it. The runtime persists a child
PID and port in the immediate post-spawn callback, then enriches that record with its executable and
process-birth proof before waiting for readiness. If either durable publication fails, the child is
stopped. A confirmed-dead child releases its
handle; a live child whose exact executable, model path, alias, port, and process-birth marker cannot
be proved remains retained in `failed` state. That fail-closed state prevents both an unsafe signal
and a duplicate process after a transient probe failure or PID reuse.

The runtime also persists a randomly generated engine API key in the owner-only state file and
writes llama.cpp's one-key-per-line input beside it with mode `0600`. It launches with
`--api-key-file` and `--slots`; the durable key never appears in process argv or the child's
environment. Readiness and activity probes authenticate with that key. Health checks snapshot
under the runtime lock, probe children in a bounded parallel pool, and commit only if the handle is
still current. Listener and port probes follow the advertised address family (`0.0.0.0` for IPv4
or `::` for IPv6), including IPv6-only hostnames and scoped IPv6 advertise URLs.

Commands for another host, non-executable recommendations, and older plan generations are rejected.
Dependencies must have succeeded before a command begins. A local decision that rejects admission
cancels new `load` or `warm` work, even when the global controller requested it. Unique ports are
allocated from the managed range, and the backend refuses to stop a PID it cannot prove belongs to
that model runtime. Every heartbeat refreshes physical memory and current external use. Managed
residencies are subtracted exactly once, and the node rejects a `warm` before process launch if its
local free-memory observation no longer satisfies the command.

For now, `load` is deliberately verification-only: the requested model ID must already name a GGUF
in Grid's model store. Run `grid pull <model>` before enabling automatic placement. The actuator
does not infer a mutable download source from a display name.

## Local wire contract

Allocator additions are namespaced so old Grid nodes can continue to register. The existing
top-level `models` field still means exactly "ready and routable now." A stable `host_id` represents
the physical machine even when several engine records run on it; the local controller merges those
records without multiplying the machine's memory capacity.

An allocator-capable node registers or updates with `PUT /nodes/{node_id}`:

```json
{
  "role": "allocator",
  "models": [],
  "host_id": "host-01HX...",
  "resources": {
    "capacity_mb": 65536,
    "reserved_mb": 8192,
    "runtimes": ["llama.cpp"],
    "backends": ["metal"],
    "memory_bandwidth_gbps": 400,
    "compute_gflops": 27132,
    "failure_domain": "floor-2",
    "tags": ["employee"]
  },
  "allocator": {
    "schema_version": 1,
    "state": "accepting",
    "cached_models": ["qwen3-coder"],
    "residencies": [
      {
        "model_id": "qwen3-coder",
        "memory_mb": 24576,
        "state": "ready",
        "loaded_at": 1785300000,
        "last_used_at": 1785300100,
        "managed": true,
        "active_requests": 0
      }
    ],
    "actuator_capabilities": ["load", "warm", "drain", "unload"]
  }
}
```

Heartbeat requests may update `load`, `resources`, and `allocator`, and may acknowledge commands:

```json
{
  "node_id": "node-record-id",
  "load": {"active_tasks": 2, "queue_depth": 1},
  "allocator": {"schema_version": 1, "state": "accepting", "residencies": []},
  "request_commands": true,
  "acknowledgements": [
    {"action_id": "8ccf...", "status": "succeeded", "message": "ready"}
  ]
}
```

`request_commands` defaults to `true` for compatibility. The node sets it to `false` on its early
lease and fail-closed fence heartbeats: those requests update registry truth and mark placement
dirty, but return immediately without waiting for reconciliation and without durably marking any
command delivered. Only the final control heartbeat uses `true` and consumes returned commands.

Every allocator input advances a causal dirty revision, while only a semantic change to destructive
safety advances the separate safety revision. Repeated identical lease or relative-age telemetry can
therefore keep planning dirty without starving a command poll: availability commands may use any
successful tick at or beyond the poll's causal revision. A `drain` or `unload` is stricter. The
successful tick's safety revision must equal the current safety revision exactly, and immediately
after durably preparing the delivery marker the controller revalidates the complete destructive batch
against a fresh raw-registry snapshot under the command-selection lock. This ordering leaves no
blocking state write between the final proof and the response. Replacement control and child routes
must each have strictly more than 30 seconds left on their 60-second lease at that final check,
covering the revision wait, response delivery, and the managed node's immediate durable action-start
boundary. If a replacement becomes non-routable, falls inside that margin, or expires while the tick
or marker write is running, destructive commands remain pending and the controller durably removes
markers prepared by that poll before suppressing the response. A failed compensating write retains
the marker conservatively and exposes the uncertainty in allocator status; availability work can
still proceed.

An authenticated heartbeat response carries commands for that `host_id`:

```json
{
  "ttl_seconds": 60,
  "model_last_used_at": {"qwen3-coder": 1785300100},
  "allocator": {
    "mode": "automatic",
    "commands": [
      {
        "action_id": "8ccf...",
        "kind": "warm",
        "node_id": "host-01HX...",
        "model_id": "qwen3-coder",
        "memory_mb": 24576,
        "plan_generation": "c91d...:00000000000000000042:4fc1...",
        "dependencies": [],
        "executable": true
      }
    ]
  }
}
```

The generation is a persistent controller epoch plus a monotonically increasing sequence and an
input digest. It remains ordered across wall-clock changes, while a new controller epoch fences
commands from a superseded process. The complete action also includes its reason, creation time,
and `not_before` time. Wire objects use `schema_version: 1`. Unknown or malformed residency rows are
excluded and make the host snapshot `unhealthy`; an invalid host lifecycle likewise fails closed
rather than becoming eligible. Managed registry IDs are derived from the authenticated host and
model identities, so a host-scoped credential cannot squat another host's control or engine record.
Managed engine registration additionally carries a private `engine_api_key`; the signaling server
stores it only in memory for upstream forwarding and never serializes it into public node output.
An HTTPS engine may also carry a bounded CA chain inside the authenticated allocator envelope.
Grid removes that PEM before storing public metadata and builds a private hostname-verifying SSL
context. Residencies carry model-local loaded/last-used ages; Grid reconstructs timestamps from its
receipt time so node clock skew cannot bypass minimum-residency or scale-down cooldown.

## Operations

### CLI workflow

Start the local grid, cache the exact GGUF filename the managed runtime will serve, and join each
computer that should offer managed capacity:

```bash
grid up
grid pull <hugging-face-repo>:<model.gguf>
grid allocator node start
grid allocator node status
```

Create a placement profile from a machine that can control the grid. Memory is the resident runtime
budget for one replica, not the file's compressed size:

```bash
grid allocator model set <model.gguf> \
  --memory-mb 12000 \
  --min-replicas 1 \
  --max-replicas 3 \
  --min-failure-domains 2
```

The profile command also accepts repeated `--runtime`, `--backend`, `--required-tag`,
`--forbidden-tag`, and `--pin` constraints; data tier, target utilization, expected service time,
latency SLO, priority, load/warm estimates, residency and scale-down cooldowns are explicit flags.
`--replica-concurrency` declares a conservative service-slot estimate for a newly managed replica.
Once a single-model engine is ready, its live `max_concurrency` may prove a higher batch width; a
multi-model engine's shared node-wide limit is never credited independently to every model. Queue,
latency, or error pressure still requests at least one replica beyond the current ready set.
If `--runtime` is omitted, it defaults to `llama.cpp`. Once the flag is present, only the listed
runtimes are eligible.

Use the three modes as a rollout sequence:

```bash
grid allocator mode observe
grid allocator tick
grid allocator status

grid allocator mode recommend
grid allocator tick
grid allocator status --json

grid allocator mode automatic
```

`recommend` is the default. Before selecting `automatic`, pre-pull the exact profiled GGUF on every
eligible managed node; an uncached `load` fails safely and enters backoff rather than downloading
unapproved weights.

`status` shows host lifecycle and capacity, model count, pending mutations, withdrawn destructive
commands, unmet constraints, the dirty/processed/success/safety revisions, and any persistent-state
warning; `--json` includes the full snapshots, demand forecasts, plan, reconciliation result,
`pending_commands`, `delivered_pending_action_ids`, `withdrawn_destructive`, the latest bounded
delivery-safety error, and bounded history. `tick` is useful after a profile or host change. The
server also runs a periodic pass and coalesces registration, heartbeat, acknowledgement, and demand
events into prompt background passes without blocking request serving.

To retire a profile or this machine's managed node:

```bash
grid allocator model remove <model.gguf>   # `rm` is an alias
grid allocator node stop
```

`node stop` first requests a graceful local drain, waits for active requests, and then stops owned
model processes. Startup failure and a stuck runtime use bounded escalation against the verified
detached process group or Windows process tree. The
daemon advertises only children that pass a steady health check, retries failed registry deletion,
and uses an instance-scoped readiness lease so stale state cannot make `node start` report success.
Before any signal, the CLI verifies the daemon's unique command-line instance and process-birth
marker; an ambiguous, legacy, or reused PID is never killed automatically and requires manual
inspection. If a node credential expires, its children keep
serving until the server-side routing lease has expired, avoiding a stale route to a dead port.
Local operators can fence a machine independently of the global controller; these overrides are
durable across node restarts and may expire automatically:

```bash
grid allocator node drain --reason "taking laptop home" --for-seconds 3600
grid allocator node pause --reason "battery use"
grid allocator node quarantine --reason "investigating thermal fault"
grid allocator node resume
```

Every command accepts `--grid <name|id|local-url>` at its own subcommand level. Administrative
commands use the operator capability from the local grid config, `GRID_ALLOCATOR_CONTROL_TOKEN`, or
their `--token-file`. A managed node uses a separate host-scoped credential from
`GRID_ALLOCATOR_NODE_TOKEN` or `node start --token-file`. Never copy the operator capability to a
worker. The allocator CLI is local-mode only in this release.

To provision another computer without putting the capability in terminal output or shell history,
write it to an owner-only file on the controller and transfer that file over your existing secure
administration channel:

```bash
grid allocator token write ./grid-node-token --host-id host-mac-studio
# securely copy the file to the other computer, then:
grid allocator node start --grid https://grid.company.internal \
  --token-file ./grid-node-token \
  --advertise-host worker-01.company.internal \
  --engine-tls-cert ./worker-01-chain.pem \
  --engine-tls-key ./worker-01-key.pem \
  --engine-tls-ca ./company-inference-ca.pem
```

`token write` signs an expiring credential authorized only for the selected stable host ID; if the
ID is omitted, it prints the generated ID once for provisioning. The file is created with mode
`0600` on POSIX and an owner-only ACL on Windows. The secret is never stored in the node process
record, command line, public node metadata, model-child environment, or CLI output. The TLS private
key must be owner-only. The certificate SAN must cover the exact advertised hostname or IP, and
`--engine-tls-ca` supplies a private intranet CA to the node and Grid upstream verifier. Grid
refuses to send node or engine credentials over non-loopback plain HTTP; `--allow-insecure-http`
is accepted for CLI compatibility but does not override that boundary for managed nodes. A node
started against a Grid owned by the same machine advertises the Grid's literal loopback control
address by default. Remote workers must use HTTPS for Grid control and TLS for their advertised
engine address. `grid down` stops a managed
allocator node before it stops the local signaling server, allowing owned model processes to
unregister and exit cleanly.

### HTTP control surface

The local signaling server exposes:

- `GET /allocator/status` — current mode, host snapshots, profiles, forecasts, latest plan,
  reconciliation result, pending commands, withdrawn destructive commands, and bounded action
  history;
- `PUT /allocator/models/{model_id}` — create or replace a model profile;
- `DELETE /allocator/models/{model_id}` — retire a model profile and safely converge to zero;
- `PUT /allocator/mode` — select `observe`, `recommend`, or `automatic`;
- `POST /allocator/tick` — request an immediate reconciliation pass.

Administrative routes require the durable operator capability in `X-Grid-Allocator-Token` or a
Bearer header. Node registration, heartbeat, command delivery, and acknowledgements require the
host-scoped credential in `X-Grid-Allocator-Node-Token`; the server rejects use against another
host ID. Neither credential belongs in engine metadata or logs. Read-only status follows the local
server's existing LAN visibility.

Operational rollout should follow this order:

1. Register hosts and verify stable physical `host_id` values, capacity, compatibility, policy
   tags, actual residencies, and heartbeat freshness.
2. Add model profiles with explicit memory and replica bounds.
3. Run in `observe`, then `recommend`, and inspect unsatisfied constraints and proposed mutations.
4. Verify local protection transitions on employee machines and confirm external engines appear as
   manually managed.
5. Select `automatic` only after the proposed placements and mutation limits match the fleet's
   failure tolerance. Returning to `recommend` is the kill switch for new automatic work.

Controller state is written atomically. If that file is corrupt at startup, Grid quarantines it,
starts a clean controller in `recommend` mode, and keeps a visible warning in allocator status.
If no durable state path exists, or the requested path cannot be quarantined or written,
`automatic` mode is refused rather than running mutations with non-durable intent.
After a valid state restore, fresh membership must re-register before destructive work resumes;
the restart grace period prevents a temporarily incomplete fleet view from causing unloads.

### Single-machine logical fleet test

Before a physical multi-machine rollout, the development harness can partition one Mac into
isolated logical hosts with separate host IDs, failure domains, durable state files, credentials,
port ranges, capacity shares, and real llama.cpp children:

```bash
uv run python tests/e2e_allocator_logical.py --nodes 2
uv run python tests/e2e_allocator_logical.py --nodes 4
uv run python tests/e2e_allocator_logical.py --nodes 4 --scenario activity
uv run python tests/e2e_allocator_logical.py --nodes 2 --scenario restart
```

The lifecycle runs cover demand-driven warm, real OpenAI-compatible inference, route fencing,
drain, unload, and abrupt child recovery. The activity scenario runs three replicas with a fourth
logical spare, marks a loaded partition user-active, and requires make-before-break evacuation.
The restart scenario rebuilds each logical node agent from durable state and requires it to adopt
the exact live llama.cpp PID before proving that the adopted process can still drain and unload.
`--scenario contention --second-model <cached-alias.gguf>` exercises two model identities under a
one-model-per-logical-host claimed capacity budget. Logical performance and memory telemetry are
partitioned so the harness never reports N times the physical Mac's capacity.

## Current limits

- The controller and status/control routes in this repository are the local Grid implementation.
  The hosted relay/control-plane service is separate, so remote fleet allocation needs a matching
  versioned persistence, authentication, lease, and command-delivery implementation there.
- The first managed process boundary is Grid-owned model runtimes. External Ollama, vLLM, LM Studio,
  API, and manually started engines are inventory and routing sources, not processes the allocator
  may stop.
- The current autonomous.ai NVIDIA engines are vLLM/CUDA even though live discovery labels their
  ownership class `external`. Framework identity and lifecycle ownership are independent: those
  engines participate in routing and placement evidence, but discovery alone does not grant Grid
  permission to start, drain, or stop them. Local auto-discovery publishes the detected runtime;
  when pointing at an engine explicitly, use `grid join --at <url> -m <model> --kind vllm` (or the
  corresponding kind) so runtime-constrained profiles can use the inventory.
- The llama.cpp `load` action verifies an already cached GGUF; it does not download one. Artifact
  distribution remains an explicit `grid pull` operation.
- Capacity is refreshed by the node as stable physical capacity plus dynamic non-Grid reserve.
  Device count and per-device VRAM are preserved, and profiles may fail closed with
  `min_gpu_count` and `min_gpu_memory_mb` constraints. This covers basic tensor-parallel
  feasibility but does not yet model GPU interconnect bandwidth, heterogeneous sharding, NUMA
  boundaries, disk budgets, or transfer-bandwidth bottlenecks.
- Model profiles accept a portable `memory_mb` fallback plus runtime-specific
  `runtime_memory_mb` estimates, so llama.cpp/Metal and vLLM/CUDA placements account for their
  distinct footprints. If a node advertises several matching runtimes, the planner conservatively
  uses the largest matching estimate. Model identity is still currently a model ID plus these
  memory requirements. Production artifact rollout
  should additionally fence on an immutable source revision and checksum.
- The planner is a transparent deterministic heuristic, not an optimal mixed-integer solver. It
  prioritizes predictable safety and understandable decisions over a mathematically minimal cost.
- In-memory LAN node membership is rebuilt by registration after a local signaling-server restart;
  durable controller state does not make a stale node eligible without a fresh heartbeat.
