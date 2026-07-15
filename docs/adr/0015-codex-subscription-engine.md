# ADR 0015 — Codex subscription engine: Responses passthrough, OAuth seat, per-kind endpoints

Status: accepted (2026-07-15). Scope: the codex-subs feature (`grid join --api codex`, PRD in
the feature branch's `.scratch/codex-subs/`). Cross-repo: this CLI + grid-src (the relay);
grid-apis is untouched (it only vends the consumer's `base_url` + `api_key` and is not in the
inference path). This ADR records the decisions that become hard to reverse once external
Codex clients and provider seats depend on them.

## Context

ADR 0012 made a third-party API service joinable as an **API engine** via a metered API key.
A provider whose OpenAI spend is a ChatGPT/Codex **subscription** — an OAuth seat, flat-rate —
still has nothing to contribute. The seat's backend (`chatgpt.com/backend-api/codex/responses`)
speaks the OpenAI **Responses API** over SSE, authenticates with an OAuth bearer plus a
single-use rotating refresh token, and rejects the parameters the grid's chat pipeline lives
on. Meanwhile the consumers who want this capacity are **external Codex apps** that natively
speak Responses — not `grid request`. Verified against the live backend (2026-07-15):
`stream` must be true, `store` must be false, `previous_response_id`/`conversation` are
rejected, every turn resends the full history, usage arrives only inside the terminal
`response.completed` event, and datacenter egress IPs can draw a Cloudflare challenge.

Constraints kept from ADR 0012: membership unchanged (the human is the member; the seat is
merely their engine), remote mode only, the catalog as static CLI data keyed by service kind,
namespaced advertised names (D-b), and the kind-keyed vendor-credential store that survives
`grid logout` (D-c). Constraint **revisited**: "no relay or control-plane changes" no longer
holds — the relay gains a consumer endpoint — making codex the first cross-repo API-engine
kind (grid-apis still unchanged).

## Decisions

**D-a (Responses passthrough — a new consumer endpoint, no dialect translation).** The relay
exposes `POST /relay/v1/responses`. Consumers are external Codex apps configured with the
grid's relay base URL, a consumer api key, and the `codex:`-prefixed model name; the body
flows consumer → relay → provider → vendor with no chat↔responses conversion in either
direction. The relay's contract layer gains a third, deliberately thin normalizer for this
endpoint: a **denylist with passthrough default** (`input` required; `store:true`,
`stream:false`, `previous_response_id`, `conversation` refused with the vendor's own error
shape; `instructions`/`tools`/`reasoning`/`include`/everything else passes through verbatim)
that keeps the endpoint inside the same guard ring as chat — body-size cap, output-token cap,
image caps re-derived over `input_image` parts, and billing estimation re-derived over
`input[]`. Rejected: translating chat/completions↔responses so `grid request` could reach
seats (lossy and heavy — tool schemas, reasoning items, and streaming deltas all differ — and
the target consumer already speaks Responses natively); a contract-free pass-through endpoint
(it would bypass every cap and bill ~1 token per request — the relay would become an
unmetered open proxy for the provider's seat).

> **Amended 2026-07-15 (spike 01 — `.scratch/codex-subs/facts.md`).** One leg of the guard ring
> above is **unimplementable as written, and needs a decision**: the vendor accepts **no
> output-token cap parameter at all** — `max_output_tokens`, `max_tokens`, and
> `max_completion_tokens` each return `400 {"detail":"Unsupported parameter: …"}`. The response
> object *echoes* `"max_output_tokens": null`, so the field is readable but not settable. There
> is no name to enforce against. The relay's options are (a) count output tokens in the stream
> and cut it — which protects the relay and consumer but **not** the provider's allowance, since
> the vendor has already generated the tokens; or (b) drop the output cap on this path and
> document it. Body-size, image caps, and billing estimation are unaffected.
>
> Two further corrections to this decision's shape:
> - The "denylist with passthrough default" is the **inverse of the backend's own behaviour**:
>   it runs a small **allowlist** and 400s anything else (`temperature` and every cap name are
>   refused, not ignored). Passthrough will therefore surface vendor 400s for any parameter we
>   have not enumerated — acceptable, but it is a documented consequence, not a surprise.
> - "No dialect translation anywhere" **leaks the provider's identity**: the vendor echoes a
>   `safety_identifier` derived from the operator's own user id *inside* the stream, plus a
>   `prompt_cache_key` and `prompt_cache_retention: "24h"` (the vendor caches prompts for 24h
>   even under `store:false`). A pure pipe forwards the provider's stable identifier to every
>   consumer of that seat. D-e's fidelity list needs a scrub entry, or D-a needs an explicit
>   carve-out for identity fields.

**D-b (per-kind served-endpoint matrix, enforced on both sides).** Which relay endpoint an
engine serves is a property of its kind: codex ⇒ `responses` only; openai ⇒
`chat/completions` only; hardware ⇒ `chat/completions` + `completions`. The provider's serve
loop gates jobs by this matrix — a chat job reaching a codex engine (including through the
single-URL fallback) is refused with a structured error, never translated, and vice versa —
and each advertised model's capability envelope carries its endpoints so the relay's existing
per-model endpoint filter excludes mismatched candidates at selection time. Old CLIs fail
closed: they never advertise `responses`, so the relay never sends them one. Rejected:
widening the provider's global endpoint allow-list (a `responses` job could then
blind-forward into a hardware engine via the single-URL fallback and die as a 404 instead of
a clean refusal).

**D-c (OAuth seat credential: grid-owned PKCE, no env var, no Codex-CLI dependency).**
`grid join --api codex` runs the OAuth PKCE authorization itself — browser plus a one-shot
localhost callback listener by default; `--no-browser` prints the authorize URL and accepts
the pasted redirect URL, with a paste deadline, `state` verification, `?error=` handling, and
a graceful fallback when the callback port is already taken. The credential — access token,
refresh token, account id, last-refresh stamp — lives in the same kind-keyed store as vendor
API keys (0o600, atomic writer, survives `grid logout`). Two deliberate deviations from 0012
D-c's env → stored → prompt precedence: there is **no env-var input path** (a rotating OAuth
bundle cannot be an env var; the whitelist's `env_var` field becomes optional), and
`~/.codex/auth.json` is **never read or written** — adopting it would double-spend the
single-use refresh token against the operator's real Codex CLI and revoke the seat.

**D-d (refresh discipline: cross-process CAS under the store lock; token outside the routing
snapshot).** The refresh token is single-use and rotates; a double-spend revokes the seat,
and N grids on one box are N detached serve processes sharing ONE stored credential. Refresh
therefore serializes read → compare-and-swap → exchange → write under the key store's
cross-process file lock: the loser re-reads inside the lock, sees an access token fresher
than its stale one, and adopts it without spending the refresh token. Triggers: reactive
(upstream 401 → refresh → retry once — **codex-scoped**; openai keeps 0012's
job-error-without-retry) and proactive on the heartbeat tick (token expiry near, or
last-refresh older than the vendor's rotation window), so an idle grid still rotates. The
in-flight exchange is journaled before the network call and the new bundle persisted the
moment it returns, shrinking the crash window in which a spent-but-unsaved rotation bricks
the seat; serve shutdown must not abandon a worker mid-exchange. Deviation from 0012's
"vendor bearer joins the reload-swappable snapshot": the codex access token lives **outside**
the routing snapshot, resolved by kind at forward time — a rotation must not rebuild routing
or race a hot-reload swap. Everything else about the engine (kind, models, caps) stays
snapshot-resident, so hot-append and leave behave exactly as 0012.

**D-e (the SSE event block is the streaming unit for responses jobs).** The vendor streams
`event:` + `data:` line pairs; the relay's mailbox is line-oriented and would tear each pair
into two separate SSE events — a spec-compliant client silently loses the event type. For
responses jobs the provider submits, and the relay stores and re-emits, whole event blocks;
chat jobs keep line framing untouched. Recorded with it, because "no translation" does not
mean "no relay work" — the passthrough decision drags in a fidelity list the chat pipeline
cannot provide: billing estimation over `input[]`, real usage read from
`response.completed`, model-name masking applied to the model field nested inside
`response.created`/`response.completed`, relay-originated mid-stream errors shaped as
`response.failed` (never the chat error envelope), no injected `data: [DONE]`, no
`stream_options` injection (the vendor rejects it), and a responses-shaped terminal response
object for settlement and resume instead of an empty chat.completion.

> **Amended 2026-07-15 (spike 01 — `.scratch/codex-subs/facts.md`).** The decision stands; three
> of its details were verified and two of them were wrong:
>
> - **The stated rationale for event blocks is FALSE, though the decision survives.** "A
>   spec-compliant client silently loses the event type" — verified otherwise: the vendor emits
>   `event:` on every event and it **always** equals the JSON `type` inside `data:` (47/47, zero
>   mismatches), and the OpenAI Python SDK's Responses path dispatches on the **JSON `type`**,
>   reading `sse.event` only to special-case Assistants `thread.*` (`openai/_streaming.py:60-83`,
>   SDK 2.11.0). Per the SSE spec an event with no data buffer is never dispatched, so a torn
>   pair drops the orphaned `event:` and the `data:` line still fires with correct JSON — **the
>   Responses path survives a torn pair.** Keep the block framing (it is free, the Assistants
>   path *does* key on `event:`, and the spec permits it), but justify it as a **wire-fidelity
>   invariant**, not as client breakage. Tests assert fidelity, not a rescue.
> - **Model masking must handle snapshot expansion.** We send `gpt-5.4-mini`; the response
>   echoes **`gpt-5.4-mini-2026-03-17`**. The raw upstream slug is *not* the advertised vendor
>   name, so a mask keyed on equality silently misses and leaks it (user story 19), and a naive
>   substring replace yields `codex:gpt-5.4-mini-2026-03-17`. Related: the vendor can silently
>   **reroute** to another model (`ModelRerouteEvent{from_model}`; "…routed to gpt-5.2 as a
>   fallback") — masking would *hide* that from the consumer and meter it at the wrong model's
>   rate. **Needs a decision.**
> - **Real usage confirmed, and the shape we had was wrong.** Verbatim from
>   `response.completed`: `input_tokens`, `input_tokens_details{cache_write_tokens, cached_tokens}`,
>   `output_tokens`, `output_tokens_details{reasoning_tokens}`, `total_tokens`. The prior
>   ("from memory") shape omitted **`cache_write_tokens`** — a third bucket, typically billed at
>   a premium. Metering built on the remembered shape would silently drop it. `no [DONE]` on the
>   wire: confirmed. Also: the streaming 200 carries **no `Content-Type` header at all** — code
>   that sniffs the declared type to decide "is this a stream" will mis-handle every response.

**D-f (plan-tier whitelist; the join probe is the reachability oracle; seat-safe defaults).** The
codex whitelist is keyed by subscription tier, because the models a seat can run differ by
plan. The join-time probe — run only when the credential or engine spec actually changed, never
on a no-op re-join — validates OAuth, headers, egress IP, and model in a single round-trip.
Unknown or absent tier ⇒ the **minimal** whitelist, never the full one — a free
seat must not advertise capacity it cannot serve.

> **Amended 2026-07-15 (spike 01 — `.scratch/codex-subs/facts.md`).** Three of this decision's
> premises were wrong, and the correction makes the probe cheaper and the tier oracle free:
>
> - **"the access-token JWT is assumed to carry only the account id; no tier claim was ever
>   verified" — FALSE.** The token's `https://api.openai.com/auth` claim carries
>   `chatgpt_plan_type` (observed `free`). **The tier is read offline at sign-in, at no cost**
>   (`remote/codex_auth.py`), so the probe is no longer the tier oracle. The
>   `x-codex-plan-type` response header *does* also exist and agrees — but it rides only the
>   inference call, so it is a cross-check, not the source. Neither is signature-verified by us.
> - **"the vendor has no model-listing endpoint" (PRD:136) — FALSE.** `GET
>   {base}/models?client_version=…` returns 200 with the seat's **real entitled set** and full
>   capability metadata, for **free**. The probe should be this GET. It returns strictly more
>   than a tier guess: tier is a *proxy* for the model list; this *is* the list. Every model
>   outside it is refused server-side, so the whitelist concept survives — but keying it by tier
>   is an indirection around a free authoritative source. The static per-tier table remains only
>   for `grid catalog`'s no-credential/no-network posture (ADR 0012 D-a).
> - **"ONE minimal `POST /responses`" cannot be made minimal — the backend accepts NO
>   output-cap parameter** (`max_output_tokens`, `max_tokens`, `max_completion_tokens` all
>   400 "Unsupported parameter"). A `POST` probe would spend an *uncapped* response. This is
>   what makes the free GET decisive rather than merely nicer. **It also breaks D-a's guard
>   ring — see the note there.**
>
> The probe keeps three jobs, none of them tier: egress/Cloudflare reachability (unknowable
> offline), seat liveness (`exp` says nothing about server-side revocation), and the real
> entitled model set. "Unknown ⇒ minimal" survives unchanged; only its trigger moves (claim
> missing or outside the known `PlanType` set). A Cloudflare-challenge 403 at probe time
refuses the join naming the egress-IP cause (a VPS cannot serve a seat); at serve time,
CF-403 and auth-403 produce distinct operator warnings, which requires the forward seam to
expose response headers/body to the warning path, not just the status code. The API-only
poll-worker default of 8 does **not** apply to codex — a codex-containing union defaults to
1 (a flat-rate seat must not be hammered eight-wide by default); an explicit
`--max-concurrency` still wins.

## Consequences

- The first cross-repo API-engine kind: the wire contracts (the `responses` endpoint-path
  value, the per-model endpoints list, the event-block framing) are hand-duplicated constants
  between the two repos, kept in lockstep by convention — changing one side alone silently
  breaks the seam.
- A flat-rate seat has no per-token rate: with no price row it sorts as free. Safe today only
  because the endpoint matrix keeps codex out of every chat path INCLUDING `model:"auto"`
  (the Advisor never sees a codex candidate — ADR 0013/0014 untouched). Anyone who later
  routes chat traffic to codex must first answer the free-sort question. `grid price set`
  remains available per-model.
- Grid jobs drain the provider's **personal** monthly Codex allowance (quota is per-account
  and windowed) — the docs must say so; quota headers beyond the join-time plan-type stay
  unread in v1 (they are opportunistic, not guaranteed).
- The vendor is forced stateless (`store:false`) but the relay retains stream chunks for its
  task TTL like every other endpoint — a documented mismatch, not a v1 change.
- 401-refresh-retry exists for codex only; openai keeps job-error-only (0012). No auto-eject
  in v1 for either kind.
- Two grids on one box share one seat: refresh is serialized cross-process; quota and rate
  limits are shared and unmanaged in v1.
- A residual brick risk remains by design: a crash after the token exchange returns but
  before the write can still lose the rotation; the journal makes it *detectable* (next
  refresh fails cleanly → "re-run `grid join --api codex` to sign in again"), not
  recoverable.
