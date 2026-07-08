# ADR 0012 — API engines (v1: OpenAI): catalog, namespacing, key store

Status: accepted (2026-07-08). Scope: the API-engines feature (`grid join --api openai`,
PRD in the feature branch's `.scratch/api-engines/`). This ADR records the three
decisions that become hard to reverse once consumers and scripts depend on them; the
full feature ships across several slices, of which the whitelist + `grid catalog --api`
surface is the first.

## Context

A provider with no capable hardware but a paid OpenAI account has no way to contribute
capacity to a grid. The fix is an **API engine**: an engine that is a third-party LLM
API service, joined by supplying its API key (`grid join --api openai`). Requests flow
consumer → relay → poll as usual; the serving machine forwards each job to OpenAI with
the provider's key. Three design points shape everything downstream and would each be
breaking to change later: where the model whitelist lives, what the advertised model
names look like, and where the vendor key is stored.

Constraints: remote mode only (the flow needs the relay); membership semantics are
unchanged (the human is the member, OpenAI is merely their engine); no relay or
control-plane changes (model registration already accepts arbitrary names).

## Decisions

**D-a (CLI-direct catalog — no hosted catalog API).** The curated whitelist of OpenAI
chat models ships as static data inside the CLI (`shared/models/api_catalog.py`), keyed
by service kind from day one. `grid catalog --api openai` prints it with per-model
capabilities and a last-verified date — no key, no network call (the same "Grid can
pull" discovery posture as the GGUF catalog). At join time the CLI calls OpenAI's
model-listing endpoint with the provider's key, which doubles as key validation and as
the intersection filter (whitelist ∩ models the key can see) — join-time validation is
the only place the CLI itself calls OpenAI. Rejected: a control-plane catalog API
(needless moving part for a small curated table; keeping it current is a data edit
verified against vendor docs, not a live probing subsystem).

Curation rule: the current flagship family plus mini/nano variants and the reasoning
series (which, as of the GPT-5.x lineup, is folded into the flagship family rather than
a separate o-series); excludes audio/realtime, embeddings, image, moderation, and
legacy chat models.
As verified 2026-07-08 against live OpenAI docs this yields four models (`gpt-5.5`,
`gpt-5.4`, `gpt-5.4-mini`, `gpt-5.4-nano`) — fewer than the ~8–10 the PRD anticipated,
because OpenAI folded reasoning into the GPT-5.x family and deprecated the o-series and
gpt-5.2. The **pro tiers are deliberately excluded**: they do not stream and answer in
minutes, the wrong fit for relay-polled chat serving. The rule wins over the count.

**D-b (`openai:*` model namespacing).** API-engine models are advertised as
`openai:<vendor-name>` (`openai:gpt-5.5`), the `comfyui:*` precedent. The
advertised→real rewrite before forwarding reuses the existing advertised/upstream
mapping mechanism (the one `--advertise-as` uses). Provenance is deliberately visible:
every model list shows consumers that requests to these models leave the grid for a
third party. Rejected: advertising bare vendor names (indistinguishable from local
models, and a name collision with a hardware engine serving the same model would be
silent).

**D-c (kind-keyed key store that survives `grid logout`).** Vendor keys live in a
separate TOML file in the grid home directory, keyed by service kind (`openai`, later
`anthropic`, …), mode `0o600`, written through the existing hardened atomic writer. It
is **not** part of the sign-in credential store: `grid logout` destroys the autonomous
session, not the provider's own vendor credential. Input precedence: env var
(`OPENAI_API_KEY`) → stored key → hidden interactive prompt; a supplied env value
overwrites the stored key (rotation is one re-join). No `--api-key` flag exists — keys
must never enter shell history or process listings. The detached serve process reads
the key store at startup; run records never carry the key. Rejected: storing the key in
`credentials.toml` (couples a vendor credential to sign-in lifecycle), or per-grid
storage (the key belongs to the machine's operator, not to any one grid).

## Consequences

- Adding `anthropic`/`gemini` or an OpenAI-compatible base URL later is a data edit
  plus a kind guard — no on-disk, record, or catalog format change (each new kind still
  gets its own ADR).
- The whitelist is maintenance-by-data-edit: bump entries and the last-verified date
  against vendor docs; an integrity test guards shape (non-empty, unique names, dated).
- Consumers can rely on the `openai:` prefix as a stable privacy signal; scripts can
  rely on `grid catalog --api openai --json` as a stable machine-readable surface.
- A revoked/rotated upstream key is invisible to the grid until serve-time job errors
  surface it (upstream 401 is a job error + stderr warning, never a relay-token refresh,
  never an auto-eject in v1) — accepted; auto-eject is a deferred observability slice.
- Because the key lives in a durable store the serve process re-reads on **reload** (not
  only at startup), appending an API engine to a live identity **hot-reloads in place**
  (issue 05): the vendor bearer joins the reload-swappable routing snapshot, so `grid join
  --api openai` onto a running identity — and `grid leave --engine openai` — re-advertise
  the union with no respawn and no dropped in-flight requests, reusing static caps (never a
  probe). A *rotated* key still respawns, by CLI policy (operator certainty), not mechanism.
