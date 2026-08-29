# Dynamic resource allocator

Grid's allocator decides which configured models should be resident on which computers, then
converges the fleet toward that placement without taking a healthy last replica away. It is built
for an AI intranet where capacity is heterogeneous and partly opportunistic: an always-on GPU
server, an employee laptop, and an external inference endpoint do not have the same ownership or
safety rules.

The allocator is experimental. It defaults to **recommend** mode and does not change a process
until an operator explicitly selects **automatic** mode.

The living [allocator expert panel](allocator-expert-panel.md) records independent scores,
reproduced failures, accepted fixes, and the remaining gaps behind that experimental label.

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

### Proactive observation and portfolio planning

The router and allocator have separate jobs. The router selects one ready engine for one live
request. It never tells the allocator what to provision. The allocator independently observes the
ordinary request lifecycle at the Grid boundary and changes future supply across the fleet.

For every completed or failed request, Grid derives bounded features in memory: endpoint family,
requested and served model, text/image/video modality, approximate input and output units, one of a
small configured workload vocabulary, service time, queue pressure, and outcome. Raw prompts,
images, tool arguments, responses, API keys, and user identities are not retained. Observation is
best effort and cannot delay or fail inference.

The controller keeps direct named-model pressure separate from workload demand. Named-model traffic
scales that model normally. Unbound or automatic traffic contributes to a workload forecast such as
coding, research, design, image, video, embedding, or general language work. At planning time, the
allocator projects sustained workload demand onto configured models using each profile's workload
scores, compatibility and resource cost, cold-start time, and measured outcomes. This lets an
inactive specialist receive a bounded canary without creating a loaded-only feedback loop.

Measured model/workload outcomes use confidence that reaches full weight after twenty fresh
requests; separately labeled quality reaches full weight after eight fresh evaluations. Both decay
with independent seven-day half-lives: a fresh latency-only request cannot revive stale quality.
Evidence is keyed by model, workload, and immutable artifact SHA-256, so a replacement artifact
starts uncertain and a late response from the previous revision cannot update the new one. Legacy
shared-timestamp quality is discarded conservatively during restore. A six-point bounded optimism
bonus lets an equally suitable, currently feasible cold candidate earn a canary, then falls to zero
as fresh evidence accumulates. The bonus is smaller than meaningful configured-suitability
differences and a preemption-only candidate pays a larger penalty, so uncertainty can spend spare
capacity but cannot manufacture eviction authority. Status reports service and quality evidence
age/freshness, effective sample count, confidence, quality confidence, and the remaining exploration
bonus for every candidate.

Request latency is compacted into a bounded logarithmic histogram per time bucket. Cohort SLO
graduation therefore uses an approximate request-level p95: one slow outlier among ninety-nine fast
requests remains tail evidence, but it no longer makes the entire minute appear slow.

Portfolio selection first scans the current fleet with the same hard runtime, backend, GPU, tag,
data-tier, artifact, memory-headroom, model-slot, and colocation rules used by placement. An
attractive model that no live node can host is excluded instead of suppressing a usable fallback.
Among otherwise similar feasible candidates, a bounded cost term prefers the cheapest eligible
host without overwhelming measured quality or configured workload suitability. Allocator status
exposes every candidate's current-headroom feasibility, immutable compatibility, possible
planner-authorized preemption path, eligible-node count, best host, startup estimate, hourly cost,
transition penalty, and rejection reason. A candidate with an avoidable cold start must show a
meaningful score improvement over a resident peer; after a justified switch the penalty reverses,
providing state-dependent hysteresis without a stale controller-side lease. An ordinary canary may
use only current headroom. Trusted broad service pressure may consider a host after removable
managed speculation is drained, but the normal planner still proves victim priority, evidence,
ownership, pins, active work, and capacity before acting.

When two or more workload classes are active, Grid no longer picks each model independently. It
starts from the evidence-backed choices and runs a deterministic bounded coordinate search over
complete workload-to-model maps, evaluating each candidate portfolio with the authoritative fleet
planner. Configured baselines and direct demand are preserved first; then the search maximizes
demand-weighted workload coverage, minimizes missing replicas, and compares measured utility,
unknown-price exposure, and known hourly cost. A shared generalist can therefore beat two slightly
better specialists when only one model slot is affordable. Search considers at most four candidates
per workload and 64 distinct portfolios, so catalog size cannot create an unbounded planning pass.
Only one distinct model may differ from the exploitation-only portfolio because of uncertainty at a
time; this is an explicit fleet exploration budget, not one canary allowance per workload. Status
shows the joint mapping, selected model set, and the model currently consuming that exploration
slot. The controller snapshots bounded demand and outcome state under the telemetry mutex, releases
it, and performs planner-backed search on that immutable snapshot; request completion telemetry is
therefore not serialized behind portfolio optimization.

Counterfactual portfolio demand is deliberately weaker than direct evidence: it may fill spare
capacity but cannot evict a baseline or a directly demanded model. Workload-wide latency, queues,
and errors remain workload evidence and are never copied into a candidate model as if that model
had served the work. If direct demand later needs the hosts, the normal drain and unload guards
reclaim the canary first. See [ADR 0039](adr/0039-the-allocator-observes-work-it-does-not-ask-the-router.md)
for the control-loop boundary.

### Anonymous cohort fairness and SLO pressure

K-user evidence is aggregated without retaining K identities. The request boundary immediately
hashes an optional `X-Grid-Affinity-Key`, uses only four bits to assign it to one of 16 fixed
anonymous cohorts, and discards the key and full digest. Requests without an affinity key share one
anonymous class. The allocator stores five minutes of bucketed cohort counts, latency, and errors by
workload; raw keys, users, prompts, responses, signatures, and full digests never enter controller
state. Fixed cohort names bound cardinality even if a caller rotates affinity keys. Shipped Grid
clients already send stable affinity keys for routing and diagnostics, but a caller-selected key is
not proof of a distinct user.

Unattested cohorts, including the anonymous class, create only non-destructive portfolio-canary
pressure no matter how many keys a caller rotates. A trusted authentication ingress may attach a
short-lived `X-Grid-Tenant-Attestation` bound to the exact affinity digest and signed by the Grid
control secret. A workload graduates to ordinary service urgency only after at least three attested
cohorts each contribute four recent samples and SLO failure affects at least half the trusted
cohorts. This lets sustained authenticated multi-user failure reclaim capacity occupied only by
speculation, but it cannot displace configured baselines, pins, direct service, higher administrator
priorities, or incompatible workloads. Status separately reports observed, attested, and qualifying
cohorts, SLO-breach rate, and Jain fairness. This is fleet-level fairness: per-token queue ordering,
weighted tenant shares, admission control, and rate limits remain responsibilities of the router and
serving runtime, where the allocator has neither the timing nor authority to schedule individual
requests.

### Global placement loop

The global loop consumes:

- model profiles: memory, optional immutable artifact SHA-256, runtime/backend compatibility,
  replica bounds, priority, data tier, placement tags, failure-domain goal, co-location ceiling,
  pins, and cooldowns;
- host snapshots: usable memory, reserve, runtime/backend, lifecycle state, policy tags, cached and
  resident models, concurrency, queue, measured throughput and latency, memory bandwidth, compute,
  heartbeat age, and actuator ownership;
- short-horizon demand: request rate, offered concurrency, queue depth, p95 latency, errors, and
  trend.

Demand history is bounded by time buckets and contains aggregate timings and counts, not prompts or
responses. Bursts are folded into their bucket rather than truncated at a raw-request limit. A
bucketed EWMA and positive trend term make the near-term forecast. The target replica count adds
demand headroom and reacts to queue, latency, and error pressure while respecting per-model minimum
and maximum bounds. Rising demand is also projected across the fastest eligible next replica's
artifact-locality-aware load-plus-warm path, confidence-weighted and capped to a five-minute/2×
horizon, so slow cold starts begin before the queue arrives without letting one noisy slope cause a
fleet-wide load spike. Negative trends never accelerate scale-down. The tracker also learns mature
groups of models that repeatedly become
active in the same time buckets and directional pairs that repeatedly activate one bucket apart.
Current demand for one group member can prewarm a quiet peer; a current workflow stage can likewise
prewarm its historically next model. Both use a confidence-weighted historical rate ratio, require
at least three supporting buckets and a 0.70 association/transition threshold, and exclude incomplete
future buckets from transition failures. Inferred demand is capped at twice the target's observed
peak, takes the maximum rather than sum across at most 32 current sources, and never propagates
transitively. Old target-local queue, latency, and error evidence is not refreshed by an association.
Only configured, non-retiring model IDs create demand series, so the permissionless inference
endpoint cannot grow controller state with arbitrary names.

Correlation-derived demand retains a separate observed-rate field. Within the same administrator
priority class, required baseline replicas place first, then models with direct request/queue/SLO
evidence, then inferred-only prewarms. This makes prediction opportunistic: it can use otherwise
idle capacity, but an alphabetically earlier speculative model cannot evict the only slot available
to real traffic. Older forecast producers without correlation lineage remain direct evidence for
wire compatibility.

Placement is deterministic. Hard pins are reserved first and higher-priority classes fill before
lower-priority work. Within one priority class, constrained and larger models define each round's
order, but placement progresses one replica per model per round. Scarce capacity is therefore
max-min fair among equally important compatible models instead of being monopolized by the first
model ID before its peers receive a baseline. If lower-priority managed residencies already occupy
all compatible capacity, Grid emits an explicit staged preemption: it first drains and unloads the
lowest-priority sufficient victim set, continues reporting the important model as
unsatisfied, and places it only after a later heartbeat proves that memory is actually free. The
same mechanism converges a host whose model ceiling was lowered below its live inventory. Each plan
stages at most 64 individual evictions by default; larger changes converge over later heartbeats
instead of producing an unbounded operational wave. Within a single-domain unpinned wave,
independent node-local victim sets are proven in one fleet scan and then consumed in disruption-cost
order; pins and multi-domain placement retain fresh searches. External,
manual, pinned, and minimum-residency-protected work is never bypassed. Correlation-only predictive
demand may use spare capacity but cannot trigger a destructive preemption; a configured baseline,
pin, or direct request/queue/SLO/error signal is required. Among equally low-priority choices, the
allocator first prefers failed, already-draining, and idle victims so urgent capacity does not wait
behind avoidable live work. It then prefers the set with the lowest learned warm-back cost, reducing
the price of restoring displaced service after the burst. Required failure-domain diversity is reserved
before that cost comparison, so several cheap victims in one rack cannot strand a critical model
that needs capacity across racks. A missing hard pin targets its exact node before either domain or
cost selection; freeing a cheaper host that cannot satisfy the pin would be gratuitous disruption.
Candidates otherwise prefer an existing ready residency, local cached weights, another failure
domain, measured throughput, and best-fit memory. Before measured
throughput exists, bounded memory-bandwidth and compute estimates break otherwise-cold ties; ready
and cached bonuses remain much larger, so hardware estimates do not cause gratuitous migration.
Ready, loading, and warming incumbents on full one-model hosts are indexed and ranked in one pass
when their failure domains are independent. Treating an in-progress incumbent as occupied prevents
the empty-host fast path from starting a duplicate cold load while its heartbeat is still
converging. The same optimization applies to empty one-model hosts only when one model remains in
its priority class, preserving equal-priority sharing. Both cases preserve the general scorer's
exact result while avoiding a fleet-wide rescan for every replica on large networks.
When several equal-priority models share an otherwise uniform empty fleet, Grid caches each static
candidate order but still consumes it one replica per model per fairness round; any shared-host or
domain interaction falls back to complete rescoring and bounded repacking.
Cost, latency, host priority, cold-start time, and throttling lower a candidate's score. Managed
nodes report monotonic action duration in their authenticated acknowledgements. Successful warm
times are retained in bounded controller history and blended with the configured model estimate as
a cold-start prior; a bounded eight-sample EWMA becomes authoritative after four samples for that
node/model/artifact revision. A checksum change starts with the configured prior instead of
inheriting an optimistic timing from different weights. Placement favors a faster cached host,
and the predictive prewarm horizon follows the fastest eligible next host's learned warm time and
cache/lifecycle state; a replica already warming does not pay the artifact-load phase twice. An
unknown host keeps the conservative configured load-plus-warm fallback, while one known slow host
does not inflate the whole fleet's target when a faster path is available. Samples
expire after 30 days so a runtime or hardware upgrade can relearn. Invalid, non-finite, negative, or
over-one-hour reports are ignored rather than poisoning scheduling or receipt delivery. Persisted
failed warm/load attempts apply a bounded per-model penalty, allowing a healthy peer to be tried
after backoff instead of selecting the same broken cache forever; the failed node remains a fallback
when it is the only feasible target. Explicit pins, per-host model limits, compatibility policies,
and a feasible minimum failure-
domain count are hard constraints. A failure-domain shortfall or capacity shortage is reported
rather than hidden by overcommit. A throttled host exposes only its configured fraction of capacity
for new placement. Placement keeps two memory ledgers: current live processes consume incremental
make-before-break capacity including admission headroom, while the complete desired footprint must
fit the host's raw allocatable memory after reserve and thermal derating. An existing process may
therefore be re-admitted without requiring phantom free headroom, but reserve growth cannot leave an
unsafe collection of zero-incremental incumbents selected. The lowest-priority movable incumbent is
migrated first when a safe peer exists; without one, Grid reports the shortfall and preserves the
last replica instead of hiding either constraint. When greedy placement fragments capacity, a deterministic bounded backtracking
repair can evacuate and re-place several equal-priority replicas; unrelated or ineligible inventory
does not change that search budget or its result. On homogeneous empty-residency fleets—where moves
provably preserve resource use—aggregate free memory and model slots provide an admissible lower
bound, so an already saturated fleet fails fast. Runtime-specific memory and existing-residency
cases retain the full repair search because relocation can change their net footprint.

An operator may also set a durable hard fleet ceiling with
`grid allocator budget --max-hourly-cost USD`. Cost is charged once per selected physical host,
not once per colocated model. Zero disables the ceiling. Under a positive ceiling, missing price
metadata fails closed unless the operator explicitly supplies `--allow-unknown-cost`; both desired
and currently active unknown-cost hosts remain visible in status. Tightening the ceiling does not
merely prevent new placements: Grid stages drain/unload for unselected managed, unpinned
residencies so current state can converge to the affordable desired set. Manual, external, and
pinned processes remain untouched because a budget does not grant ownership. Their cost and the
resulting service shortfall stay explicit. Plan generations include the budget policy so a policy
change is fenced like every other desired-state transition.

Status turns the desired hourly run rate into explicit 1-hour, 24-hour, and 30-day projections.
These are run-rate forecasts, not invoices: they carry the demand-confidence used for the current
placement, a conservative confidence-adjusted known-cost band, the configured ceiling and remaining
headroom for each window, and a `complete` flag. Any selected host without an explicit price keeps
the projection incomplete instead of being silently valued at zero. Every missing replica also
produces capacity advice with its minimum runtime, backend, memory and GPU shape; a budget-blocked
replica includes the minimum additional known hourly allowance needed for the cheapest currently
eligible host.

Request routing uses the same heterogeneous capacity evidence after placement. Among engines in the
same host-protection class that already serve the requested model, Grid compares active requests as
a fraction of each engine's effective concurrency limit rather than comparing raw request counts.
Its expected-completion estimate also includes advertised queued work, so an otherwise fast batched
vLLM engine with a backlog yields to a clear peer while active concurrency remains the hard admission
boundary.
This keeps a wide-batching vLLM server from appearing busier than a narrow llama.cpp engine merely
because it safely carries more simultaneous work. Missing capacity remains conservative raw load;
zero capacity remains closed. Throttled-host priority and hard admission limits still take
precedence over this load balance. Equivalent engines prefer the freshest lease, but a timestamp
inside the allowed future-skew window is clamped to zero age and cannot gain extra priority.
When private proxy measurements exist for the requested model, routing minimizes estimated
completion time: the incoming request's service wave is multiplied by a confidence- and
freshness-weighted latency EWMA. Weak or missing measurements blend toward the current cohort
median, measurements for other models are ignored, and expired evidence falls back completely.
For text generation, a bounded `max_completion_tokens` or `max_tokens` hint adds a model-throughput
lower bound to that estimate, allowing short requests to favor low latency and long generations to
favor high token throughput. Grid never inspects or stores prompt content for this decision.

Clients with multi-turn or iterative workloads may send an opaque `X-Grid-Affinity-Key` header.
Grid immediately hashes a printable key of at most 256 UTF-8 bytes and uses rendezvous hashing to
keep that model's requests on the same near-equivalent engine, preserving runtime KV/prompt caches
without a centralized session map. The raw key is neither retained nor forwarded upstream. Host
protection and admission remain hard gates, and affinity considers only the best protection class
and routes whose estimated completion time is within 20% of the best available route. Adding or
removing an otherwise equivalent engine therefore remaps only the sessions assigned to the changed
engine; load, throttling, or failure can still move a session immediately.

Lease health is not the only routing signal. Grid keeps a private per-engine, per-model circuit
breaker for outcomes observed by the proxy. A 429 opens a one-second cooldown immediately; two
consecutive transport or 5xx failures open it, with exponential backoff capped at 30 seconds. An
expired circuit admits a half-open probe and any 2xx response resets the streak. Caller-caused 4xx
responses do not poison route health, and a broken model route on a multi-model vLLM server does not
hide its healthy models. Circuit state never changes discovery inventory, never grants lifecycle
authority, and is not persisted across a Grid-server restart. Grid also does not automatically
replay a failed POST: the breaker redirects only subsequent requests, avoiding duplicate inference
or tool side effects.

The proxy attributes each successful response to both the engine and requested model, keeping
bounded EWMAs of end-to-end latency and completion-token throughput. Those server-owned measurements
override self-reported estimates in placement snapshots, so actual service performance eventually
supersedes the cold hardware prior. A multi-model vLLM engine is scored only with measurements for
the model being placed; its fast model cannot lend an unrelated slow model an inflated score.
For a checksum-protected managed model, every measurement is also bound to the residency's exact
artifact revision. Routing ignores the previous revision immediately after a rollout, and the first
successful response resets that model's estimator instead of blending incompatible revisions.
Placement likewise falls back to hardware priors until revision-matching evidence exists. External
vLLM inventory without artifact checksums retains the backward-compatible model-scoped behavior.
Latency and throughput each ramp to full placement authority over eight relevant samples and decay
against their own update timestamp. A stream that exposes no token count can refresh latency without
making an old token rate look fresh. When an OpenAI-compatible stream includes final usage metadata,
a bounded fragmentation-safe SSE parser extracts only its completion-token count; malformed and
oversized events are ignored. Streaming responses without usage still contribute latency. The
measurements are private: discovery does not expose them, managed heartbeats cannot overwrite them,
and no prompt or response content is retained for allocator telemetry. Expired measurements fall
back to current hardware priors until relevant new requests refresh them.

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

1. **Never overcommit declared memory.** Reserved memory and thermal derating bound the complete
   desired footprint. Policy headroom additionally fences new or resized allocations, while a
   zero-allocation transition may retain an existing process in that margin. Unmanaged resident
   workloads consume capacity first.
2. **Never place on an ineligible host.** Paused, draining, unhealthy, quarantined, stale, missing-
   heartbeat, or implausibly future-heartbeat hosts receive no new placement. Runtime, backend,
   data tier, tags, allow/deny lists, pins, and per-host model limits are enforced.
3. **Make capacity available before removing it.** Missing desired replicas are loaded and warmed
   before obsolete replicas are considered for drain. The deliberate exception is an explicit
   higher-priority preemption on a saturated compatible host: the victim drains first, and the new
   model is not assigned until a later heartbeat proves the memory was released.
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
   churn and retry storms. Scarce execution slots go to higher-priority service even across
   lifecycle phases; an explicit preemption drain inherits the beneficiary's priority, while
   routine cleanup remains behind availability work. Within one administrator-priority class,
   required baseline and direct demand execute before correlation-only prewarming. Equal-priority,
   equal-urgency work uses its estimated remaining cold-start path, so a cached model that can serve
   soon is not stranded behind an unrelated artifact download. Within the same readiness class,
   mutation slots are filled one replica round per model, preventing one service's second replica
   from starting before a peer's first; capacity-release preemptions retain the same beneficiary
   round. Within one preemption wave, already-drained and idle capacity is released before a newly
   draining or busy victim. Among equally disruptive victim sets, the allocator releases a host
   that can start the beneficiary soonest, including cached weights and learned warm-start time;
   under a tight mutation budget it finishes the group with the fewest remaining lifecycle
   transitions instead of spending a slot on a partial release that cannot yet serve traffic.
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
fleet-wide retry loop and does not block healthy targets elsewhere. Reconciliation indexes plan
urgency, assignment memory, actual READY inventory, and mutation attempts once per tick;
safety-floor and retry construction scale with configured models, reported residencies, and retained
history rather than their cross products.

Planning likewise memoizes compatibility, capacity fit, and the exact dynamic score of a
node/model/remaining-capacity/domain state within one tick. Fair replica rounds may revisit shared
hosts, but repeated visits do not repeat performance, artifact-locality, hardware, or policy
evaluation; colocation-enabled plans retain complete fit evaluation as their peer set changes.

If a higher service class appears while the mutation governor is full, the controller may withdraw
a lower-class constructive command only when it has never been delivered to its node. A delivered
`pending` command is treated as potentially running and keeps its slot until the node acknowledges
it; equal-class work is not churned merely to change queue order. When one slot is enough, a leaf
mutation is withdrawn before its useful prerequisite so reprioritization does not discard extra
work. When a host permits several
queued mutations, delivery preserves the reconciler's service ordering instead of re-sorting by
opaque action identity. A higher service class queued on a later tick also precedes an older,
undelivered lower-class entry; FIFO remains the tie-breaker within the same class.

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

An authenticated `cached` residency is authoritative evidence that an earlier warm lifecycle has
finished and no process remains. If later demand restores that placement, Grid may issue a fresh
warm immediately instead of waiting for the old successful WARM receipt's observation timeout;
failed warm history still retains its normal backoff. The same causal rule permits a new DRAIN when
the runtime is authoritatively `ready` again and a new UNLOAD when it is `draining` again. This lets
models cycle out and back in without a prior successful receipt imposing a false 120-second delay;
failed destructive actions remain backoff-protected.

For now, `load` is deliberately verification-only: the requested model ID must already name a GGUF
in Grid's model store. Run `grid pull <model>` before enabling automatic placement. The actuator
does not infer a mutable download source from a display name. When a profile declares
`artifact_sha256`, `load` and `warm` hash the exact cached file before process launch. A residency
reports the digest it proved; a same-named residency with a missing or different digest does not
satisfy placement. Grid warms a matching replica elsewhere before draining the old version, and
refuses an unsafe in-place replacement when no peer can preserve availability.

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
    {
      "action_id": "8ccf...",
      "status": "succeeded",
      "message": "ready",
      "duration_seconds": 12.5
    }
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
        "controller_term": 7,
        "controller_id": "c57a...",
        "controller_lease_expires_at": 1788020000.0,
        "dependencies": [],
        "executable": true
      }
    ]
  }
}
```

The plan generation is a persistent epoch plus a monotonically increasing sequence and an input
digest, so plans remain ordered across wall-clock changes. Mutation authority is a separate durable
fence: automatic mode acquires a renewable single-writer lease beside the controller state file,
increments its term on every takeover, and stamps every command with `(controller_term,
controller_id, controller_lease_expires_at)`. A managed node durably remembers the highest term,
accepts at most one controller identity in that term, rejects expired leases, and rejects every
lower-term command even after restart. This makes command safety independent of network delivery
order; plan generations continue to order plans within the accepted authority.

The complete action also includes its reason, creation time, and `not_before` time. Wire objects use
`schema_version: 1`; the new authority fields are additive so older persisted actions still decode,
but once a node has observed a fenced command it will not return to the legacy term-zero namespace.
Unknown or malformed residency rows are
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
  --artifact-sha256 <64-hex-digest> \
  --workload-score coding=1 \
  --workload-score research=.8 \
  --max-colocated-models 1 \
  --colocation-exclude <interfering-model.gguf> \
  --min-replicas 1 \
  --max-replicas 3 \
  --min-failure-domains 2
```

The profile command also accepts repeated `--runtime`, `--backend`, `--required-tag`,
`--forbidden-tag`, `--pin`, and `--workload-score WORKLOAD=SCORE` values. Workload scores are
capability hints in `(0, 1]` for portfolio planning; they do not route an individual request. Data
tier, target utilization, expected service time, latency SLO, priority, load/warm estimates,
residency and scale-down cooldowns are explicit flags.
`--artifact-sha256` is optional but recommended for managed production GGUFs; it is canonicalized
to lowercase and becomes part of command, retry, and readiness identity.
`--max-colocated-models` is also optional (`0` means unlimited). The value counts the candidate
itself, so `1` requests exclusive serving for an interference-sensitive model. The constraint is
reciprocal: Grid will neither place that model beside another live/planned model nor later place a
different configured model beside it. Cached-only weights do not consume a serving slot. When a
managed host already violates a tightened ceiling, Grid deterministically elects the higher-priority
(then more constrained) survivor and stages safe drain/unload of removable peers before it admits
new work. Existing manually managed vLLM inventory that violates a profile remains visible but is
reported unsatisfied; the constraint never grants Grid authority to resize or stop the external
engine.
For a narrower policy, repeat `--colocation-exclude <model>` to name only measured bad pairings.
Exclusions are reciprocal even if declared by one profile: neither placement order can put the pair
together. Compatible peers may still share the host, and the same managed-only staged convergence
applies if a pair is already live when the policy is added.
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

grid allocator budget --max-hourly-cost 2.50
grid allocator mode automatic
```

`recommend` is the default. Before selecting `automatic`, pre-pull the exact profiled GGUF on every
eligible managed node; an uncached `load` fails safely and enters backoff rather than downloading
unapproved weights.

`status` shows host lifecycle and capacity, model count, desired and current hourly cost under a
configured ceiling, pending mutations, withdrawn destructive
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
  reconciliation result, last successful tick duration, pending commands, withdrawn destructive
  commands, and bounded action history;
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

### Single-machine scenario lab and logical fleet test

Use the deterministic scenario lab to explore a large heterogeneous fleet without starting model
processes or pretending the development Mac owns the modeled GPUs:

```bash
uv run grid test scenario \
  --machines 8 \
  --models 8 \
  --users 50 \
  --duration 30m \
  --seed 42
uv run grid test scenario --machines 16 --models 9 --users 500 --duration 2h --json
```

The lab creates logical Apple/Metal, ComfyUI/MPS, and NVIDIA/vLLM/ComfyUI configurations with
different memory, disk, cached artifacts, concurrency, performance, and cost. User personas produce
coding, research, marketing, sales, design, image, video, embedding, and general demand through the
real bounded request classifier; operations traffic also names the baseline model so direct demand
and autonomous portfolio demand compete in the same run. A seeded workday includes a coding surge,
creative campaign, thermal throttle, node outage, recovery, and cooldown. Every planning tick uses
the production workload intelligence and placement planner.

The report explains loads, unloads, node transitions, capacity shortfalls, demand served, workload
per-user and per-workload fairness/SLO attainment, portfolio suitability, memory use, cache
locality, persistent modeled disk consumption, cold starts, cost, capacity recommendations, and
safety invariants. It intentionally reports shortfalls instead of inventing capacity. `--timeline`
prints every changing tick; `--json` emits the complete stable report;
reusing `--seed` reproduces the same run. Artifact disk constraints are translated into each
one-model logical node's admission set, while the allocator's native runtime, backend, lifecycle,
memory, headroom, and model-slot rules remain authoritative.

The scenario lab is a planning-scale and decision-quality test only. It is not an inference test.
The persistent fixture below is the real-process proof: every successful text or image result comes
from an engine running on the development Mac.

For interactive development, start a persistent Grid with any number of logical machines. Each
machine gets a stable host id, failure domain, state file, credential, capacity share, and real
llama.cpp child while the Grid API remains available until explicitly stopped:

```bash
uv run grid test start --machines 4
uv run grid test status
uv run grid test watch
uv run grid test demo --users 6 --requests 12
uv run grid --local models http://127.0.0.1:22100
uv run grid --local chat --grid http://127.0.0.1:22100 \
  -m SmolLM2-135M-Instruct-Q3_K_M.gguf 'Reply with OK'
curl http://127.0.0.1:22100/v1/models
curl http://127.0.0.1:22100/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"SmolLM2-135M-Instruct-Q3_K_M.gguf","messages":[{"role":"user","content":"Reply with OK"}],"max_tokens":8}'
uv run grid test stop
```

For a real mixed-framework Grid, install the runtime and bundle once, then reserve one of the N
logical machines for ComfyUI. On Apple Silicon this is ComfyUI with PyTorch/MPS; the remaining
logical machines run independently managed llama.cpp/Metal children:

```bash
uv run grid engine install comfyui
uv run grid engine pull z_image
uv run grid test start --machines 4 --include-comfyui --media-bundle z_image
uv run grid test demo --users 6 --requests 12 --max-tokens 32
```

Here `--machines 4` means four total logical machines: three llama.cpp text nodes and one ComfyUI
media node. All processes still share one physical Mac, so the fixture partitions reported capacity
instead of multiplying it. It does not pretend that CUDA or vLLM exists on Apple hardware. A CUDA
host can install/register vLLM as external inventory during the physical-node phase.

`--machines N` accepts 1–32 logical machines; practical limits are the physical machine's memory
and process capacity. Use `--model`, `--port`, and `--engine-port-base` to run a different cached
GGUF or avoid local port conflicts. Starting is idempotent for matching settings, and status can be
emitted as JSON. `watch` follows residency transitions and allocator command outcomes without
stopping the Grid when you press Ctrl-C. The ordinary shipped CLI can address the test endpoint by
URL (`--local` makes that explicit even when remote mode is active), including `grid chat`,
`grid models`, and `grid allocator status --grid http://127.0.0.1:22100`. The start output names the
owner-only token file for allocator mutations. The fixture stays isolated from the active local or
remote Grid configuration.

To compare genuinely different models and heterogeneous node economics, add repeatable portfolio
candidates plus one capacity and hourly cost per text node, then run the real competition:

```bash
uv run grid test start --machines 4 --include-comfyui --media-bundle z_image \
  --candidate-model qwen2.5-coder-0.5b-instruct-q4_k_m.gguf \
  --candidate-model qwen2.5-0.5b-instruct-q4_k_m.gguf \
  --text-capacities-gib 3.5,5,25.5 \
  --text-costs-per-hour 0.05,0.20,0.80
uv run grid test compete
```

`grid test compete` loads one candidate at a time through the production lifecycle, runs eight
deterministic coding questions through real llama.cpp/Metal inference, and submits authenticated
correctness and latency evidence without manufacturing user demand. It then offloads every
candidate and sends unresolved coding traffic through the ordinary Grid request boundary. The
allocator—not the router—chooses the measured portfolio winner, reloads it, verifies a real answer,
and fails the test if it did not use the cheapest currently capable logical node. It then makes the
winner fleet-ineligible with a temporary impossible node tag, verifies that the allocator explains
the rejection, unloads it, loads a feasible runner-up, and serves another real answer. Removing the
constraint must restore the measured winner. Evaluation-marked
inference still updates per-engine performance, but only the owner capability may mark it and its
quality is recorded separately at `POST /allocator/evaluations`; ordinary callers cannot suppress
their demand signal. Evaluation submissions may include `artifact_sha256`; an explicit digest must
match the currently configured revision. The command leaves the selected winner ready for
interactive `grid chat` use.

Configured logical capacities must fit within this machine's real usable memory. The list lengths
must equal the number of text nodes (`--machines` minus the optional ComfyUI node), so a too-small
cheap node, a medium economical node, and a large expensive fallback can be tested explicitly.

`grid test demo` performs no synthetic inference or demand injection. It first converges the
baseline to one real replica and verifies a genuine OpenAI-compatible response. Twelve genuine
requests using distinct caller-selected affinity keys first target unresolved `auto`; the report
requires them to remain untrusted canary evidence at urgency one. Twelve more requests from three
operator-attested anonymous cohorts then target the same workload. All 24 receive honest HTTP 503
responses with no router involved, but only the attested `4 + 4 + 4` evidence may promote service
urgency.
The production request path classifies bounded features locally; after its minimum evidence
threshold, the allocator projects that workload onto the configured coding portfolio, proves broad
cohort SLO failure, and proactively loads and warms a real llama.cpp deployment before any request names it. The first named
specialist call must then return real output. Multiple client personas send concurrent requests to
both text models with stable affinity keys and bounded production-style retries. The report requires
non-empty assistant output, response IDs, usage, client-visible latency including retries, per-node performance samples,
and the complete process lifecycle before passing. Observed demand expires naturally; the command
never clears or fabricates it.

When the Grid was started with `--include-comfyui`, the same command also sends a genuine image
workflow through `/v1/media/image/generate`, validates returned PNG bytes, and writes the result into
the logical test run directory. ComfyUI is currently registered as immutable media inventory: the
fixture owns its process startup and teardown, while allocator mutations remain limited to
Grid-owned llama.cpp residencies. This ownership boundary is reported rather than hidden.

Before a physical multi-machine rollout, the development harness can partition one Mac into
isolated logical hosts with separate host IDs, failure domains, durable state files, credentials,
port ranges, capacity shares, and real llama.cpp children:

```bash
uv run python tests/e2e_allocator_logical.py --nodes 2
uv run python tests/e2e_allocator_logical.py --nodes 4
uv run python tests/e2e_allocator_logical.py --nodes 4 --scenario activity
uv run python tests/e2e_allocator_logical.py --nodes 2 --scenario restart
uv run python tests/e2e_allocator_logical.py --nodes 4 --scenario preemption \
  --second-model <cached-alias.gguf>
```

The lifecycle runs cover demand-driven warm, real OpenAI-compatible inference, route fencing,
drain, unload, and abrupt child recovery. The activity scenario runs three replicas with a fourth
logical spare, marks a loaded partition user-active, and requires make-before-break evacuation.
The restart scenario rebuilds each logical node agent from durable state and requires it to adopt
the exact live llama.cpp PID before proving that the adopted process can still drain and unload.
`--scenario contention --second-model <cached-alias.gguf>` exercises two model identities under a
one-model-per-logical-host claimed capacity budget. Logical performance and memory telemetry are
partitioned so the harness never reports N times the physical Mac's capacity.
`--scenario preemption --second-model <cached-alias.gguf>` keeps demand for a low-priority model
active on every logical host, injects a high-priority burst for the second model, and requires real
drain/unload of every incumbent before the second model is warmed and served. It then retires the
critical burst and requires the displaced batch service to warm back onto every logical host and
complete another real streamed request before final cleanup.

## Research basis

The design follows several primary systems results while preserving Grid's allocator/router split:

- [Scalable Joint Resource Allocation for SLO-Constrained LLM Inference in Heterogeneous GPU
  Clouds](https://arxiv.org/abs/2604.07472) motivates joint feasibility, model choice, provisioning,
  routing, quality, latency, memory, and budget constraints. Grid applies its fleet-feasibility and
  bounded cost insight in the allocator while leaving per-request routing independent.
- [Fairness in Serving Large Language Models](https://www.usenix.org/conference/osdi24/presentation/sheng)
  motivates work-conserving, token-aware user fairness. Grid currently measures fairness in the
  scenario lab and uses bounded cohort-wide failure as fleet-allocation evidence; enforceable token
  fairness belongs in the serving scheduler/router.
- [Ensuring Fair LLM Serving Amid Diverse Applications](https://arxiv.org/abs/2411.15997) motivates
  application-aware accounting and protection against noisy neighbors. Grid applies that insight
  through fixed anonymous cohorts rather than durable user IDs.
- [SLOs-Serve](https://arxiv.org/abs/2504.08784) motivates application-specific SLO accounting and
  continuous adaptation. Grid applies it at the slower model-residency timescale; token allocation
  remains inside each serving engine.
- [Online LLM Selection via Constrained Bandits with Time-Varying Demand](https://arxiv.org/abs/2606.17489)
  motivates confidence-aware portfolio exploration under hard cost and service constraints. Grid
  does not yet claim a bandit optimizer; this is the next model-selection direction after trusted
  demand and preemption admission are correct.
- [JITServe](https://www.usenix.org/conference/nsdi26/presentation/zhang-wei) motivates maximizing
  SLO goodput under imprecise request information. Grid applies conservative evidence and explicit
  uncertainty at fleet timescales while leaving token scheduling to the serving runtime.
- [HydraServe](https://www.usenix.org/conference/nsdi26/presentation/lou) motivates proactive
  artifact distribution and contention-aware cold starts; Grid currently models cache, load, warm,
  and measured startup time but not network transfer bandwidth.
- [Llumnix](https://www.usenix.org/conference/osdi24/presentation/sun-biao) and
  [Libra](https://www.usenix.org/conference/nsdi26/presentation/ruan-libra) motivate dynamic
  rescheduling, isolation, and SLO-aware adaptation under changing load. Grid's load/warm and
  drain/unload state machine applies those ideas at the slower fleet-allocation timescale.

## Current limits

- The controller and status/control routes in this repository are the local Grid implementation.
  The hosted relay/control-plane service is separate, so remote fleet allocation needs a matching
  versioned persistence, authentication, lease, and command-delivery implementation there.
- The first managed process boundary is Grid-owned llama.cpp model runtimes. ComfyUI, external
  Ollama/vLLM/LM Studio, API, and manually started engines are inventory and routing sources, not
  processes the allocator may stop. The mixed-framework logical fixture starts and stops its own
  ComfyUI process as test-fixture setup/cleanup; allocator actions do not masquerade as ComfyUI
  model lifecycle mutations.
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
  uses the largest matching estimate. Managed GGUF profiles can additionally require an immutable
  SHA-256; remote artifact distribution and source-revision resolution remain operator-managed.
- The planner is a transparent deterministic heuristic, not an optimal mixed-integer solver. It
  prioritizes predictable safety and understandable decisions over a mathematically minimal cost.
- Inter-model interference is controlled through the per-profile `max_colocated_models` ceiling and
  explicit reciprocal `colocation_excludes` pairs. Grid does not yet infer those pairs from
  production co-run experiments or partition GPU execution resources such as CUDA MPS/MIG.
- In-memory LAN node membership is rebuilt by registration after a local signaling-server restart;
  durable controller state does not make a stale node eligible without a fresh heartbeat.
