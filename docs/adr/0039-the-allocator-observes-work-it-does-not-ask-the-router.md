---
status: accepted
---

# The allocator observes work; it does not ask the router what to provision

The router and allocator are independent control loops over one shared inference plane.

The **router** is a request-time dispatcher. Given one request and the deployments that are ready
now, it chooses a concrete model and engine, queues, falls back, or rejects. It never downloads,
loads, warms, drains, or unloads a model.

The **allocator** is a fleet-time controller. It observes the ordinary request lifecycle, response
outcomes, node telemetry, the complete model catalog, user policy, and historical demand. It
forecasts workloads and changes which deployments exist. It never chooses the destination of an
individual live request.

The allocator must not depend on a router-specific provisioning message. Named-model requests,
media endpoints, direct engine traffic visible to a node, and requests that never reach the router
are all legitimate demand. Routing choices are useful observations, not commands.

## Allocator-owned observation

The Grid request boundary emits a bounded lifecycle observation after work completes (or fails):

- a generated request ID, endpoint family, requested model, locally classified workload and input
  modalities;
- bounded size estimates such as input tokens, requested output tokens, images, and media duration;
- outcome, status class, queue pressure, service time, and measured output units/throughput;
- optional explicit user feedback joined later by request ID;
- tenant policy class when the deployment has one, never an API key or raw identity.

Raw prompts, tool arguments, images, and responses do not enter allocator history. Classification
runs locally over the in-memory request and retains only a small known vocabulary. Unknown fields
remain unknown rather than being copied into telemetry. An installation may disable semantic
classification and still retain endpoint, size, and outcome signals.

The observation path is best effort. Allocation intelligence must never add a network call, an LLM
call, or a new failure mode to inference. A malformed or failed observation costs telemetry, never
the user's response.

## Autonomous planning

The allocator maintains three related views:

1. **Direct model pressure** scales a requested or actually serving model from arrival, queue,
   latency, and error evidence.
2. **Workload demand** forecasts coding, research, design, image, video, embedding, and general
   language work independently of the currently loaded portfolio.
3. **Portfolio evidence** combines that workload forecast with configured model capabilities,
   resource cost, compatibility, cold-start time, measured outcomes, and offline quality evidence.

At a planning tick—not in the request path—the portfolio planner may create speculative demand for
an inactive compatible model. Speculation is bounded, confidence-gated, can use only capacity left
after baseline and direct traffic, and is evaluated after readiness. Measured success, latency, and
optional quality outcomes influence later portfolio scores; failure or no demand returns the model
through the normal drain/unload safety path.

On a saturated fleet, a new speculative model may replace stale speculation or one excess replica
to obtain its first canary. It cannot evict baseline/direct work or another hypothesis's only
canary; further speculative scale-out still waits for spare capacity.

Multi-user failure is stronger than one speculative hypothesis without becoming a router command.
Grid maps bounded affinity digests into 16 anonymous cohorts and retains only five-minute aggregate
workload latency/error buckets. At least three active cohorts, 12 samples, and a majority cohort SLO
breach graduate that workload to ordinary service urgency. It may reclaim capacity from historical
or portfolio speculation, but configured baselines, pins, and administrator priority remain
stronger. One stable caller and requests without affinity remain a canary; rotating affinity keys
cannot create unbounded state but can occupy more than one cohort, so this is bounded breadth
evidence rather than a Sybil-proof tenant authority. Token-level fairness, authenticated tenant
shares, and admission still belong to the router/runtime; the allocator changes supply, never queue
order.

This counterfactual step prevents a loaded-only feedback loop: a Grid containing only a general LLM
can still notice sustained image demand and prewarm a configured image model.

## Shared state, not shared policy

The allocator publishes deployment truth (`loading`, `warming`, `ready`, `draining`, `unhealthy`),
capacity, endpoints, and eligibility through the normal registry. The router reads that registry.
The router's ordinary decision and outcome traces enter the same observation stream as any other
request, but it does not summarize requirements for the allocator or wait synchronously for a
placement change.

Both loops may use capacity, latency, quality, and cost. The separation is the decision variable:

- the router optimizes the destination of one request over current ready supply;
- the allocator optimizes future supply over the whole Grid.

## Control timescales

- seconds: congestion response, replica count, admission and node protection;
- minutes: load, warm, drain, unload, and workload canaries;
- hours to days: artifact cache, portfolio mix, recurring workflows, and capacity planning.

Every destructive transition remains subject to the allocator's existing last-replica,
active-request, minimum-residency, failure-domain, cooldown, and acknowledgement guards.
