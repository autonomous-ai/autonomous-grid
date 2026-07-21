---
status: accepted
---

# `/responses` as an engine capability: per-engine endpoints, discovery by probe, a narrowed allow-list statement

ADR 0015 D-b made the servable relay endpoint a property of an engine's **kind** — codex ⇒
`responses`, openai ⇒ `chat/completions`, hardware ⇒ the chat pair. That is why the Responses
dialect today reaches exactly one kind of engine: a codex subscription seat. Everything else a
grid serves — llama.cpp, Ollama, vLLM, and the `openai` API engine whose vendor defines the
dialect — is invisible to an app that speaks Responses: real capacity someone is contributing that
a whole class of apps cannot reach. This ADR records how `responses` becomes a capability **any**
engine can hold. Grill log: `.scratch/extend-responses-api/` (PRD + `decisions.md`, the verified
file-and-line map).

Scope: this CLI + grid-src (the relay); grid-apis has **no slice** — checked, not assumed (PRD §9).
Written **before** any code lands: the decisions below are normative, not descriptive. It ships in
two phases: **P1** = the shared core plus the `openai` kind; **P2** = hardware engines (the probe,
per-engine resolution, and the live capability display). Two wire values this feature touches are
hand-duplicated with grid-src and have no compile-time link between the copies: the endpoint-path
literal `"responses"`, which must match grid-src's `endpoint_path` byte-for-byte, and the per-model
`endpoints` capability list, whose absence on grid-src's side means chat-only — which is precisely
what makes old CLIs fail closed. Changing one repo alone compiles, passes every test, and breaks the
seam at runtime.

Choices a future reader will otherwise re-litigate:

- **Endpoint capability resolves per ENGINE for hardware, and stays per KIND for API engines —
  partially superseding ADR 0015 D-b.** What D-b got right and is kept in full: the gate runs
  **after** routing where the kind is known; a mismatch is refused with a structured error, never
  translated and never blind-forwarded; each advertised model's capability envelope carries its
  endpoints so the relay's per-model filter excludes mismatched candidates at selection time; and
  old CLIs, which never advertise the capability, fail closed. What is superseded is exactly one
  clause **of the mechanism**: **`hardware ⇒ chat/completions + completions` as a fixed property of
  being hardware.** A per-kind matrix is only true while every engine of a kind is identical, and
  hardware engines stop being identical the moment they differ by *software version* — one Ollama
  box serves the dialect and the box beside it, a few releases behind, does not, and both are the
  same "kind". Which
  release grew the endpoint is second-hand for every hardware engine (`decisions.md` §4 rates it
  medium confidence), and that is itself an argument for asking the engine rather than tabulating
  versions. API-engine kinds are genuinely uniform (the kind *is* one vendor's service), so they
  keep static per-kind catalog data; hardware resolves from what was discovered at join.
  D-b's enumeration nonetheless goes stale in a **second** place, for an unrelated reason: the
  `openai ⇒ chat/completions only` row's *value* gains the dialect in P1. That is catalog data
  moving under the mechanism D-b established, not the mechanism being superseded — but a reader
  diffing D-b against the running system finds two of its three rows no longer describing reality,
  and only one of them is a design change. Both are recorded here so neither reads as drift.
  Rejected **keeping the matrix wholly per-kind** — ship P1's `openai` widening and declare hardware
  chat-only forever. That is a shippable position rather than a straw man, and the cheapest one on
  offer; it loses because it permanently excludes every hardware engine from the dialect on the
  strength of a claim about kinds that the world falsifies as soon as two boxes run different
  versions.
  Rejected **per-model resolution**: the route either exists on the engine's server or it does not,
  so per-model would re-ask the same question once per model and invite two models on one server to
  disagree about their server. The probe is a property of the server, run **once per engine**,
  stamped onto every model that engine advertises.

- **Capability is DISCOVERED at join, not assumed and not declared.** The engine is asked once
  whether the route exists — a deliberately invalid request, where a *route-missing* status means
  no and a *bad-request* status means yes. By design no inference runs and no tokens are spent, so
  joining stays free. Transport failure, timeout, or an ambiguous answer is treated as **no** — fail
  closed, because the cost of under-advertising is that an engine keeps serving chat traffic
  normally, while the cost of over-advertising is a candidate filter that lies.
  Rejected **assume-and-let-the-forward-fail**:
  it makes the relay's candidate filter untruthful and reproduces precisely the misleading-error
  class ADR 0017 exists to prevent — a request routed to an engine that cannot serve it, failing in
  a way that describes the request rather than the gap. Rejected **an opt-in join flag**: it pushes
  an engine-version detail onto the person joining, and when they get it wrong it fails in exactly
  the same way as assuming, with the added insult that they were asked. Because capability is
  discovered rather than enumerated, an engine this ADR never names — LM Studio, MLX, something
  that does not exist yet — is handled the day it grows the endpoint, with no release from us.
  Recorded as **unverified** (`decisions.md` §5, items 3 **and** 4): that a route-existence probe
  reliably answers *bad-request* rather than 404/405/a catch-all page is not confirmed per engine —
  and neither is the premise that an empty probe body never triggers work, which is the whole reason
  the probe is free. P2's live gate confirms both. Until then the fail-closed rule makes the first
  ambiguity safe; the second has no such backstop, so an engine found to do real work on an empty
  body would force the probe to change shape rather than merely fail closed.

- **The anti-traversal statement at the engine-side gate is deliberately NARROWED, and this is not a
  loosening.** The gate today asserts that "`responses` never enters `_ALLOWED_ENDPOINTS`, so the
  anti-traversal property is unchanged", under the heading that the global allow-list is
  "deliberately NOT widened". Be precise about what stops holding: `_ALLOWED_ENDPOINTS` is only the
  hardware branch's return value inside `_served_endpoints`, so an implementation may leave that
  module constant untouched and have the branch compose a per-engine set instead. What necessarily
  changes is that the literal enters **the set the gate consults** for a capable hardware engine —
  the thing the comment was really asserting could never happen. It is the **implication** that goes
  stale, not inevitably the sentence, and it must be corrected rather than quietly deleted.
  **The safety was never in which literals are in that set — it is in the endpoint being
  checked against a closed set of fixed literals before it is used to build a URL.** The allow-list's
  own comment states the threat model it defends: the endpoint is *relay-supplied* and interpolated
  into a local engine URL, so the membership check is what stops a buggy or compromised relay
  probing other local paths via `../`. Only a value that matched the set ever reaches
  `f"{target_url}/{endpoint}"`, and every member of that set is a literal authored in this repo,
  never a string from the request or the wire — and for P2 the probe decides **whether** the
  authored literal `"responses"` is included, not **what** it is, so nothing discovered over the
  network reaches URL construction. Rejected **a second, parallel allow-list for the Responses
  path**: the `_MEDIA_ENDPOINTS` frozenset is precedent for exactly that shape, but it is separate
  for a reason that does not transfer — media routes to this box's *media server*, a different URL,
  whereas Responses goes to the same LLM engine as chat. A second set for the same URL costs another
  closed set to keep in lockstep and buys nothing the membership check does not already provide.
  D-b's actual concern also survives, by a different mechanism: it rejected widening the *global*
  allow-list because a Responses job could then blind-forward into a
  hardware engine and die as a 404 instead of a clean refusal — and per-engine resolution keeps
  exactly that protection, since an engine that failed the probe does not have the literal in its
  set and still gets the clean structured refusal. What is genuinely lost is only that the old
  comment claimed a **stronger property than was ever needed**. Recording this is the single
  strongest reason this ADR exists: a future reader who finds a security statement silently relaxed
  cannot tell whether it was deliberate, and would be right to assume the worst.

- **The auto-router filters `/responses` candidates on the output cap, so `auto` never routes a capped
  request to an engine that cannot honour one.** Moving the cap refusal to the engine-side per-kind
  gate (this feature's decision above / PRD §3) is right for a *named* model, where the app chose the
  engine. But `auto` picks *for* the app, and the auto-branch candidate filter keys only on the dialect
  endpoint, vision, and tools — not the cap. On a grid serving both an `openai` engine and a codex
  seat, a capped `auto` request could therefore be ranked onto the seat and refused *after* queueing:
  the same request succeeding or failing on where routing lands, which is precisely what user story 21
  ("`auto` never picks an engine that will fail") forbids. So "honours an output cap" becomes a hard
  routing filter — sourced from the **same** catalog fact the engine gate reads
  (`max_output_tokens ∈ unsupported_params`, so the two layers can never disagree), advertised in the
  capability envelope, and required by the auto deriver only when the body carries a cap. This makes
  the request-contract split **three** layers, not two: the relay normalizer refuses the universal
  facts; the **auto-router excludes cap-incapable engines**; the engine-side gate refuses a *named*
  seat pick. It extends ADR 0016's auto-branch hard filter by one axis, reusing the existing `features`
  subset check — not `special_params`, which that deriver deliberately avoids. Rejected **accept the
  asymmetry and only document it**: cheaper (no wire change) and the post-queue failure is loud and
  actionable, but it leaves `auto` non-deterministic against its own contract, and the config that
  triggers it — both kinds on one grid, `auto`, a cap — is real, not a straw man.

Deliberately unchanged, so the widening is not read as wider than it is: **local mode** stays
chat-only (it forwards blind by design and has never probed *any* text capability — not vision, not
tools — so a Responses probe there would be the first of its kind; the exclusion is the absent
capability pipeline, not effort); **stateful Responses** — prior-response linkage, conversation
reference, and server-side storage — stays *refused* by the relay's normalizer for every engine,
because the grid keeps no conversation and pinning one to an engine would break the property that
any capable engine can serve the request; the **retrieval and cancellation routes** are a different
mechanism and not refused at all, simply **not proxied** — some engines expose them, the grid does
not forward them, and they share that stateful reasoning; and **no translation shim is built** for
engines that lack the dialect, which is the whole point of discovering capability rather than
assuming it.

Rollout order is fixed per phase: the **master deploys before the CLI is released**. The reverse
order does not break — it degrades silently, because an older relay still enforces mandatory
streaming and still refuses the output cap, so someone would see subscription-seat restrictions on
an ordinary hardware engine with nothing to explain why. The opposite skew is safe by the capability
list's own fail-closed design: an older CLI advertises nothing new, and the relay's filter excludes
it.
