---
status: accepted
---

# Advisor sees candidate prices (supersedes the pricing clause of ADR 0013)

ADR 0013 chose to render only name + context window + capabilities per candidate to the
Advisor — "never on pricing or live load", because "cost and availability are decided locally
at pick time, where the data is fresh." This ADR reverses the **pricing** half of that clause.
Free capacity and throughput remain never-sent; their freshness rationale is intact.

Each Candidate line now ends with a price segment: the model's raw input/output rates in USD
per 1,000,000 tokens — `price: $<in> in / $<out> out per 1M` — taken from the cheapest engine
(by price score) among the engines serving that model. A model with no price renders
`$0 in / $0 out per 1M`.

Why reverse it, and why now:

- **The rubric already claimed cheapness without data.** It instructed the Advisor to rank the
  "smallest, cheapest adequate candidate" while its only cost signal was the parameter size in
  the model's name — wrong in both directions (a 70B can be free and self-hosted; an 8B can be
  hosted and priced). Provider-set prices (`grid price set`, the authoritative per-provider
  table) exist precisely to answer this, and had no effect on the ranked path.
- **Price is a slow-moving owner-side fact**, in the same class as ctx and caps: it changes
  only when a provider runs `grid price set`, not per request. The freshness argument that
  keeps capacity/throughput local does not apply to it.
- **$0 is billing-consistent, not fabricated.** A model with no price row bills exactly $0
  today, so rendering `$0 in / $0 out` shows the Advisor the amount actually charged. This is the
  opposite of the ctx case (where the bake-default 128000 was a fabricated value and is
  therefore omitted when unknown): free-vs-unknown are deliberately indistinguishable
  everywhere, so the render stays consistent by construction.

Rubric changes (tie-breaker within class — the classification-first structure is untouched):

- A neutral definition of the price field ("lower is cheaper, $0 in / $0 out = free"). The "grid OWNER
  pays for every token" sentence is removed as stale: under provider-set pricing, consumers pay
  per token and providers earn revenue share.
- SIMPLE → smallest, cheapest adequate candidate, now backed by real rates. The "never a large
  or API-hosted model" guard is unchanged (size still guards compute waste, independent of
  price).
- DEMANDING → among comparably capable candidates, **prefer the cheaper, then the leaner**.
  Leanness was the cost proxy; with real rates it demotes to secondary tie-break. The
  reserve-the-single-most-capable clause is unchanged.

Rejected alternatives: **cost-first ranking** (discards the A/B-converged rubric; regresses
quality to "just adequate"); **data without guidance** (per-advisor unpredictable weighting at
the same validation cost); **rendering the blended price score** (opaque to an LLM and bakes
the 3× output weight into the prompt; raw rates read naturally — the score remains the local
sort key so the Fallback ordering is unchanged); **min-per-rate across engines** (would render
a rate pair no engine actually charges; the cheapest engine's real pair is what selection lands
on).

Consequences: the two never-price tests flip into price-render tests (still guarding
capacity/availability from the prompt); the rubric revision gates on the replay ladder before
merge (both prior wording bugs were only visible there); the router debug log carries the same
price fields; grid-src records the mirror decision alongside the code change (its ADR 0003, which
supersedes the pricing clause of grid-src ADR 0001).
