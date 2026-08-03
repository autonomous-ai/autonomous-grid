"""Curated whitelists of API-engine models, keyed by service kind.

An API engine (`grid join --api <kind>`) serves third-party models through the
provider's own API key. Each kind's whitelist is static data: capabilities are
copied from the vendor's documentation, never live-probed, and the table carries
the date it was last checked against those docs.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ApiModelEntry:
    vendor_name: str  # the id sent upstream, e.g. "gpt-5.5"
    context_window: int
    supports_tools: bool
    supports_vision: bool
    supports_json_mode: bool  # response_format {"type": "json_object"}
    supports_structured_outputs: bool  # response_format {"type": "json_schema"}
    notes: str = ""


@dataclass(frozen=True)
class ApiWhitelist:
    last_verified: str  # ISO date the table was last checked against the vendor's docs
    base_url: str | None  # default vendor endpoint (no trailing slash); None means user must supply --at
    entries: tuple[ApiModelEntry, ...]
    env_var: str | None = None
    supports_model_listing: bool = True  # whether the vendor exposes GET /models (media APIs like Doggi don't)
    max_output_param: str | None = "max_tokens"
    unsupported_params: tuple[str, ...] = ()
    endpoints: tuple[str, ...] = ("chat/completions",)
    # This kind's backend speaks SSE only, so it cannot serve a NON-streaming Responses request — the
    # codex subscription seat (ADR 0018 / issue 06c). Advertised to the relay as the NEGATIVE
    # `stream_only` capability-feature (remote/probe.codex_capability_entry) and forbidden by a
    # non-streaming `auto` request, so the auto-router never lands one on a seat that would 400 it
    # post-queue. Read by BOTH that advertise path and the engine-side stream gate (remote/serve.py) via
    # `kind_is_stream_only`, so the two can't disagree. Hand-duplicated with grid-src's
    # KNOWN_FEATURE_KEYS (CLAUDE.md lockstep): only the seat ever sets this True, and an old CLI / master
    # that doesn't know the literal simply doesn't exclude the seat — the pre-06c behaviour, never a NEW
    # failure (master-before-CLI rollout, ADR 0018 §11). Default False: every other API kind and every
    # hardware engine (never in this table) serves non-streaming.
    stream_only: bool = False
    # How this kind authenticates, as DATA rather than a branch per kind. "key" = a metered API key
    # read via `api_keys.require_bearer`; "oauth" = a subscription seat holding its own bundle
    # (ADR 0015 D-c); "none" = a LOCAL CLI seat with no grid-held credential at all — the binary
    # signs itself in. Every credential-shaped `if kind == …` in the serve loop reads this instead,
    # so a new kind never needs a new exemption.
    credential: str = "key"
    # A flat-rate subscription seat: one operator's personal allowance, so the default poll-worker
    # count is pinned to 1 rather than the API-engine default of 8 (ADR 0015 D-f). Draining a
    # personal allowance eight-wide by default is the harm; it is a property of being flat-rate,
    # not of being codex.
    flat_rate: bool = False
    # Non-None => this kind's "engine" is a LOCAL process on this box (a CLI seat), served behind a
    # loopback server on this default port. Also what makes the kind joinable in local mode. Each
    # seat needs a DIFFERENT default so two seats can run on one box without colliding.
    local_seat_port: int | None = None

# Verified against https://platform.openai.com/docs/models (which 301-redirects to
# https://developers.openai.com/api/docs/models) on 2026-07-08.
# Curation: the current flagship family plus mini/nano variants; reasoning is built
# into the GPT-5.x family (the separate o-series is deprecated, removal 2026-12-11).
# Excluded: pro tiers (no streaming, multi-minute answers — wrong fit for relay-polled
# chat), gpt-5.3-codex (agentic coding specialty), gpt-4.1 family (outside the flagship
# family), gpt-5.2 and earlier (deprecated), and all audio/realtime/image/embedding/
# moderation models.
OPENAI_LAST_VERIFIED = "2026-07-08"

OPENAI_WHITELIST: tuple[ApiModelEntry, ...] = (
    ApiModelEntry(
        vendor_name="gpt-5.5",
        context_window=1_050_000,
        supports_tools=True,
        supports_vision=True,
        supports_json_mode=True,
        supports_structured_outputs=True,
        notes="Flagship model for coding and professional work.",
    ),
    ApiModelEntry(
        vendor_name="gpt-5.4",
        context_window=1_050_000,
        supports_tools=True,
        supports_vision=True,
        supports_json_mode=True,
        supports_structured_outputs=True,
        notes="More affordable flagship-family model.",
    ),
    ApiModelEntry(
        vendor_name="gpt-5.4-mini",
        context_window=400_000,
        supports_tools=True,
        supports_vision=True,
        supports_json_mode=True,
        supports_structured_outputs=True,
        notes="Strongest mini model for high-volume work.",
    ),
    ApiModelEntry(
        vendor_name="gpt-5.4-nano",
        context_window=400_000,
        supports_tools=True,
        supports_vision=True,
        supports_json_mode=True,
        supports_structured_outputs=True,
        notes="Cheapest GPT-5.4-class model for simple tasks.",
    ),
)

DOGGI_LAST_VERIFIED = "2026-07-09"

DOGGI_WHITELIST: tuple[ApiModelEntry, ...] = (
    ApiModelEntry(
        vendor_name="hunyuan-image-3-t2i",
        context_window=0,
        supports_tools=False,
        supports_vision=False,
        supports_json_mode=False,
        supports_structured_outputs=False,
        notes="Text-to-image. Aspect ratios: square_hd, square, portrait_4_3, "
              "portrait_16_9, landscape_4_3, landscape_16_9.",
    ),
    ApiModelEntry(
        vendor_name="hunyuan-image-3-i2i",
        context_window=0,
        supports_tools=False,
        supports_vision=False,
        supports_json_mode=False,
        supports_structured_outputs=False,
        notes="Image-to-image. Aspect ratios: auto, 21:9, 16:9, 3:2, 4:3, 5:4, "
              "1:1, 4:5, 3:4, 2:3, 9:16, 4:1, 1:4, 8:1, 1:8.",
    ),
    ApiModelEntry(
        vendor_name="Wan-AI/Wan2.2-I2V-A14B-Lightning",
        context_window=0,
        supports_tools=False,
        supports_vision=False,
        supports_json_mode=False,
        supports_structured_outputs=False,
        notes="Image-to-video. Resolutions: 480p, 580p, 720p. "
              "Aspect ratios: auto, 21:9, 16:9, 4:3, 1:1, 3:4, 9:16.",
    ),
)

# The seat's backend (ADR 0015). Verified live on 2026-07-15 (spike 01, `.scratch/codex-subs/facts.md`).
CODEX_LAST_VERIFIED = "2026-07-15"

# The service-kind key. Defined here — not in remote/api_keys, which re-exports it — because the
# run-record concurrency rule in shared/ needs it and shared/ must not import remote/.
CODEX_KIND = "codex"

# The `client_version` the join probe pins on `GET {base}/models` (the endpoint 400s without one —
# facts.md B1). The REAL client's version at verification time; static data, re-verified by hand
# with the whitelist itself.
CODEX_CLIENT_VERSION = "0.144.2"

# The free set is live-verified (2026-07-15, facts.md #5) against a real seat; `codex-auto-review`
# (visibility: "hide") is deliberately excluded. The json-mode/structured-outputs booleans are False
# because they are chat-dialect notions a Responses passthrough cannot honestly claim — the capability
# envelope OMITS those keys outright rather than advertising False (remote/probe.codex_capability_entry).
_CODEX_FREE_MODELS: tuple[ApiModelEntry, ...] = (
    ApiModelEntry(
        vendor_name="gpt-5.6-terra",
        context_window=272_000,
        supports_tools=True,
        supports_vision=True,
        supports_json_mode=False,
        supports_structured_outputs=False,
        notes="Balanced agentic coding model for everyday work.",
    ),
    ApiModelEntry(
        vendor_name="gpt-5.6-luna",
        context_window=272_000,
        supports_tools=True,
        supports_vision=True,
        supports_json_mode=False,
        supports_structured_outputs=False,
        notes="Fast and affordable agentic coding model.",
    ),
    ApiModelEntry(
        vendor_name="gpt-5.5",
        context_window=272_000,
        supports_tools=True,
        supports_vision=True,
        supports_json_mode=False,
        supports_structured_outputs=False,
        notes="Frontier model for complex coding, research, and real-world work.",
    ),
    ApiModelEntry(
        vendor_name="gpt-5.4-mini",
        context_window=272_000,
        supports_tools=True,
        supports_vision=True,
        supports_json_mode=False,
        supports_structured_outputs=False,
        notes="Small, fast, and cost-efficient model for simpler coding tasks.",
    ),
)

# The paid-only rows (issue 10b): ILLUSTRATIVE, encoded from the public pricing docs
# (`CODEX_PRICING_DOCS_URL`), which give a display name + plan only — no caps. So the context window is
# the `0` unknown sentinel (rendered `—`, never a fabricated number) and tools/vision follow the
# profile every codex model has had to date. The slugs are INFERRED from display names — verify against
# a real paid-seat probe. DISPLAY-ONLY: `grid catalog` shows them; serving is the live probe's job.
_CODEX_PAID_MODELS: tuple[ApiModelEntry, ...] = (
    ApiModelEntry(
        vendor_name="gpt-5.6-sol",
        context_window=0,
        supports_tools=True,
        supports_vision=True,
        supports_json_mode=False,
        supports_structured_outputs=False,
        notes="Illustrative (pricing docs) — Plus and up; verify against a paid seat.",
    ),
    ApiModelEntry(
        vendor_name="gpt-5.4",
        context_window=0,
        supports_tools=True,
        supports_vision=True,
        supports_json_mode=False,
        supports_structured_outputs=False,
        notes="Illustrative (pricing docs) — Plus and up; verify against a paid seat.",
    ),
)

_CODEX_PRO_ONLY: tuple[ApiModelEntry, ...] = (
    ApiModelEntry(
        vendor_name="gpt-5.3-codex-spark",
        context_window=0,
        supports_tools=True,
        supports_vision=True,
        supports_json_mode=False,
        supports_structured_outputs=False,
        notes="Illustrative (pricing docs) — Pro only, research preview; verify against a paid seat.",
    ),
)

# The illustrative cross-plan reference (issue 10b — relabeled from the old serving-gate tier table).
# DISPLAY-ONLY: `grid catalog --api codex` renders it as a menu of what each plan can serve; it NEVER
# gates a join (the live probe is the source of truth for the served set). Keyed by PLAN LABELS (not
# the vendor's `PlanType` vocabulary) — the three distinct membership sets across all plans. Business /
# Enterprise / Edu share the Plus set, so they are surfaced as a note rather than a duplicate row.
CODEX_TIER_MODELS: dict[str, tuple[ApiModelEntry, ...]] = {
    "free / go": _CODEX_FREE_MODELS,
    "plus": _CODEX_FREE_MODELS + _CODEX_PAID_MODELS,
    "pro": _CODEX_FREE_MODELS + _CODEX_PAID_MODELS + _CODEX_PRO_ONLY,
}

# Provenance for the illustrative reference (issue 10b). The pricing-docs read date is distinct from
# `CODEX_LAST_VERIFIED` (the free-seat probe date) — different data, different verification.
CODEX_PRICING_DOCS_URL = "https://learn.chatgpt.com/docs/pricing"
CODEX_REFERENCE_LAST_VERIFIED = "2026-07-24"


def _codex_tier_union() -> tuple[ApiModelEntry, ...]:
    """The flat ``entries`` for the codex whitelist row: every tier row merged, first occurrence
    wins. Keeping ``entries`` = the union is what lets the kind-generic helpers — ``find_advertised``,
    the join's ``-m`` validation — resolve every codex model without learning about tiers."""
    merged: dict[str, ApiModelEntry] = {}
    for entries in CODEX_TIER_MODELS.values():
        for entry in entries:
            merged.setdefault(entry.vendor_name, entry)
    return tuple(merged.values())

# The Claude seat (`grid join --api claude`): the operator's own Claude Code CLI, driven as an
# engine by `shared/agent/cli_seat.py`. Unlike every other row here this kind names no vendor
# endpoint and no credential — the seat is a subprocess and the CLI authenticates itself.
CLAUDE_LAST_VERIFIED = "2026-07-28"

# The service-kind key, named here (not in remote/) for the same reason CODEX_KIND is.
CLAUDE_KIND = "claude"

# The FALLBACK row — what a box ADVERTISES when discovery cannot read its `claude`.
# `entries_for("claude")` normally answers with concrete ids read from the installed binary
# (`shared/agent/claude_models.py`); this list is the answer when that returns nothing.
#
# CONCRETE IDS, not `--model` tier aliases, and the trade is deliberate. An alias can never go
# stale, which is why this row held `opus`/`sonnet`/`haiku`/`fable` before — but a tier alias is a
# name no client ever ASKS for. Claude Code sends `claude-opus-5`; the relay's bare-name matcher
# compares suffixes exactly (`alias_targets_for_endpoint`, cli internal), so a grid whose only seat
# fell back to aliases advertises four names and matches none of them. Measured on the `gmail.com`
# grid 2026-07-31: 193 × 503 in one day, every one of them a bare concrete id against a
# tier-alias-only node. A stale id is a bug we can see and fix; a name nobody requests is a seat
# that silently serves nothing.
#
# Staleness is bounded by where this row is even reachable: discovery is the normal path, and it
# only falls through here when the binary cannot be read at all. Keep these in step with the
# newest tier ids when the seat is re-verified (`CLAUDE_LAST_VERIFIED`).
#
# The tier aliases are NOT dropped — they move to `CLAUDE_TIER_ALIASES` below and stay resolvable.
#
# `context_window` is the `0` unknown sentinel (rendered `—`) throughout. The seat has no /models
# endpoint to probe and the repo's rule is that an un-probed number is not written down — the
# alternative would be a fabricated figure that silently mis-sizes routing decisions.
#
# `supports_tools=True` describes the WIRE CONTRACT, which is what a consumer can rely on: send
# OpenAI `tools[]`, get `tool_calls[]` back. Mechanically the seat teaches the CLI a JSON reply
# shape in the prompt and parses it back out — NOT the vendor's native tool_use — see
# `shared/agent/cli_seat.py`.
CLAUDE_WHITELIST: tuple[ApiModelEntry, ...] = tuple(
    ApiModelEntry(
        vendor_name=model_id,
        context_window=0,
        supports_tools=True,
        supports_vision=False,      # the adapter drops image parts; claiming vision would lie
        supports_json_mode=False,   # `--json-schema` is not wired into the seat yet
        supports_structured_outputs=False,
        notes=note,
    )
    for model_id, note in (
        ("claude-opus-5", "Claude Code CLI seat — most capable tier."),
        ("claude-sonnet-5", "Claude Code CLI seat — balanced tier."),
        ("claude-haiku-4-5", "Claude Code CLI seat — fastest tier."),
        ("claude-fable-5", "Claude Code CLI seat — Fable tier."),
    )
)

# Resolvable-only: names the seat still ANSWERS to but no longer advertises.
#
# Load-bearing during the rollout, not politeness. A relay holding a capability envelope written
# before this change routes `claude:sonnet` to a seat that has since upgraded; without this the
# seat would reject a name it had itself advertised an hour earlier. The `claude` CLI takes either
# spelling for `--model`, so answering to both costs nothing. Advertisement narrows; resolution
# does not — the same rule `resolvable_entries` already states.
CLAUDE_TIER_ALIASES: tuple[ApiModelEntry, ...] = tuple(
    ApiModelEntry(
        vendor_name=alias,
        context_window=0,
        supports_tools=True,
        supports_vision=False,
        supports_json_mode=False,
        supports_structured_outputs=False,
        notes=note,
    )
    for alias, note in (
        ("opus", "Claude Code CLI seat — most capable tier."),
        ("sonnet", "Claude Code CLI seat — balanced tier."),
        ("haiku", "Claude Code CLI seat — fastest tier."),
        ("fable", "Claude Code CLI seat — Fable tier."),
    )
)

# The Codex CLI seat — the `codex` binary driven locally, distinct from the `codex` kind above
# (which is the OAuth HTTP seat, ADR 0015). Different kind key because a grid may serve both.
# UNVERIFIED: no signed-in codex seat was available; slugs are the ones the OAuth seat reports.
# The seat translates BOTH dialects (`chat/completions` and `responses`) through the CLI's own
# subprocess, so a grid-src caller that speaks the Responses API natively is served without the
# relay needing to convert — the seat server (`local/cli_seat_server.py`) answers `/responses`
# directly. Unlike the OAuth `codex` kind (responses-only, stream-only), this seat serves chat too
# and honours a non-streaming responses request, because the subprocess is not an SSE-only pipe.
CODEX_CLI_LAST_VERIFIED = "2026-07-31"

CODEX_CLI_KIND = "codex-cli"

CODEX_CLI_WHITELIST: tuple[ApiModelEntry, ...] = tuple(
    ApiModelEntry(
        vendor_name=name,
        context_window=0,
        supports_tools=True,
        supports_vision=False,
        supports_json_mode=False,
        supports_structured_outputs=False,
        notes="Codex CLI seat — verify against a signed-in seat.",
    )
    # `gpt-5.6-sol` is what Codex CLI picks when the user takes its DEFAULT model. Captured live:
    # 30 consecutive requests for it, every one answered "No providers available for this model",
    # because the seat advertised the other four and not the one the client reaches for first.
    for name in ("gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.5", "gpt-5.4-mini")
)

# One structure per kind: the verified-date and the entries can't drift apart.
WHITELISTS: dict[str, ApiWhitelist] = {
    "openai": ApiWhitelist(
        last_verified=OPENAI_LAST_VERIFIED,
        base_url="https://api.openai.com/v1",
        env_var="OPENAI_API_KEY",
        entries=OPENAI_WHITELIST,
        # The whole GPT-5.x family rejects `max_tokens`: "Unsupported parameter: 'max_tokens' is not
        # supported with this model. Use 'max_completion_tokens' instead."
        max_output_param="max_completion_tokens",
        # All four whitelist models reject `stop` ("Unsupported parameter: 'stop' is not supported
        # with this model.") — verified against the live API on 2026-07-14. `stop: null` is accepted.
        unsupported_params=("stop",),
        # ADR 0018 (issue 03): the openai vendor serves the Responses dialect natively, so this kind
        # serves BOTH endpoints. `responses` is hand-duplicated with grid-src's per-model
        # `provider_supports` filter and must match its `endpoint_path` byte-for-byte (absent there ⇒
        # chat-only, so old CLIs fail closed) — CLAUDE.local.md lockstep rule. Never `completions`.
        endpoints=("chat/completions", "responses"),
    ),
    "codex": ApiWhitelist(
        last_verified=CODEX_LAST_VERIFIED,
        base_url="https://chatgpt.com/backend-api/codex",
        # The union of the tier rows — the pre-probe `-m` validation reference (issue 05). Serving no
        # longer intersects it: the live probe is the source of truth for the served set + caps (issue
        # 10a). 10b relabels this table as the illustrative cross-plan reference and adds the paid rows.
        entries=_codex_tier_union(),
        env_var=None,  # ADR 0015 D-c: an OAuth seat has no env-var input path
        max_output_param=None,  # facts.md #1: this backend has no output-cap parameter, under any name
        # Refused before the round-trip rather than translated, because no translation exists. The
        # three cap names each return `400 {"detail":"Unsupported parameter: ..."}`, as does
        # `temperature` — this backend runs a small allowlist and denies chat-era knobs outright
        # rather than ignoring them (facts.md #1, #7).
        unsupported_params=("max_tokens", "max_output_tokens", "max_completion_tokens", "temperature"),
        # ADR 0015 D-b: a codex seat serves the `responses` endpoint ONLY.
        endpoints=("responses",),
        # ADR 0018 / issue 06c: the seat's backend is SSE-only, so it cannot serve a non-streaming
        # Responses request. The ONLY row that sets this — see `kind_is_stream_only`.
        stream_only=True,
        credential="oauth",  # ADR 0015 D-c: an OAuth seat has no env-var input path
        flat_rate=True,      # ADR 0015 D-f: pins the default poll-worker count to 1
    ),
    "claude": ApiWhitelist(
        last_verified=CLAUDE_LAST_VERIFIED,
        # No vendor endpoint: this kind's "engine" is a loopback server on THIS box driving the
        # operator's own `claude` CLI, so the join computes the URL rather than reading it here.
        base_url=None,
        env_var=None,  # no credential of any kind — the CLI authenticates itself
        entries=CLAUDE_WHITELIST,
        supports_model_listing=False,  # the CLI is not an HTTP service; there is no GET /models
        # The seat speaks Anthropic natively beside OpenAI (`local/cli_seat_server.py` serves both
        # `/chat/completions` and `/messages`), so both dialects are advertised here.
        endpoints=("chat/completions", "messages"),
        credential="none",   # the `claude` CLI authenticates itself; the grid holds nothing
        flat_rate=True,      # a personal subscription — never polled eight-wide by default
        local_seat_port=8099,
    ),
    "codex-cli": ApiWhitelist(
        last_verified=CODEX_CLI_LAST_VERIFIED,
        base_url=None,
        env_var=None,
        entries=CODEX_CLI_WHITELIST,
        supports_model_listing=False,
        endpoints=("chat/completions", "responses"),
        credential="none",
        flat_rate=True,
        local_seat_port=8098,
    ),
    "doggi": ApiWhitelist(
        last_verified=DOGGI_LAST_VERIFIED,
        base_url=None,  # user supplies endpoint via --at
        env_var="DOGGI_API_KEY",
        entries=DOGGI_WHITELIST,
        supports_model_listing=False,  # Doggi has no /models endpoint
    ),
}


def supported_kinds() -> tuple[str, ...]:
    return tuple(sorted(WHITELISTS))


def advertised_name(kind: str, entry: ApiModelEntry) -> str:
    """`<kind>:<vendor>` — the prefix is load-bearing for the codex seat.

    Bare vendor names were tried. Codex CLI recognises its own model slugs and, for the 5.6 family,
    then sends `tools: []` through any non-built-in provider — measured 0 tools bare vs 10 with the
    prefix. Nothing in its catalog changes that (`tool_mode`, `use_responses_lite` and
    `multi_agent_version` were all patched in its own cache, still 0), so the prefix is the only
    thing that keeps those models usable at all.
    """
    return f"{kind}:{entry.vendor_name}"


_claude_entries: tuple[ApiModelEntry, ...] | None = None
_codex_cli_entries: tuple[ApiModelEntry, ...] | None = None


def entries_for(kind: str) -> tuple[ApiModelEntry, ...]:
    """The models ``kind`` serves right now — the ONE reader every surface goes through.

    Every kind but ``claude`` answers from its static row, unchanged. The Claude seat is not a
    vendor endpoint but the operator's own CLI, so the honest answer is whatever ids that binary
    carries; they are discovered once per process (see ``shared/agent/claude_models.py``) and fall
    back to ``CLAUDE_WHITELIST`` whenever the binary cannot be read.
    """
    whitelist = WHITELISTS.get(kind)
    if whitelist is None:
        return ()
    if kind == CLAUDE_KIND:
        global _claude_entries
        if _claude_entries is None:
            _claude_entries = _discovered_claude_entries() or whitelist.entries
        return _claude_entries
    if kind == CODEX_CLI_KIND:
        global _codex_cli_entries
        if _codex_cli_entries is None:
            _codex_cli_entries = _discovered_codex_cli_entries() or whitelist.entries
        return _codex_cli_entries
    return whitelist.entries


def _discovered_claude_entries() -> tuple[ApiModelEntry, ...]:
    """Imported inside the call, not at module scope: the seat modules import this one, so a
    top-level import would close the cycle. By the time anybody asks, both are loaded."""
    from shared.agent import cli_seat, claude_models
    from shared.agent.seats import claude as claude_seat

    binary = cli_seat.seat_bin(claude_seat.SPEC)
    if binary is None:
        return ()
    return tuple(
        ApiModelEntry(
            vendor_name=model_id,
            # Still the `0` unknown sentinel: the seat has no /models endpoint to probe, and the
            # `[1m]` alias form that would justify a real number is not verified for full ids.
            context_window=0,
            supports_tools=True,
            supports_vision=False,
            supports_json_mode=False,
            supports_structured_outputs=False,
            notes="Claude Code CLI seat — id read from the installed binary.",
        )
        for model_id in claude_models.discover(binary)
    )


def resolvable_entries(kind: str) -> tuple[ApiModelEntry, ...]:
    """Every name ``kind`` will ANSWER to — what it currently advertises, plus the static row.

    The two differ only for ``claude``, where the advertised set is concrete ids (discovered from
    the binary, or `CLAUDE_WHITELIST`) while `claude:sonnet` and the other tier aliases must keep
    resolving: a relay holding a capability envelope from before an upgrade routes the old name,
    and the `claude` CLI takes either spelling for ``--model``. Advertisement narrows; resolution
    does not.

    The aliases come from `CLAUDE_TIER_ALIASES` rather than from the whitelist, because the
    whitelist no longer holds them — it is the concrete-id fallback now, and on a box where that
    fallback IS what got advertised, `current` and `whitelist.entries` are the same four names.
    """
    current = entries_for(kind)
    whitelist = WHITELISTS.get(kind)
    if whitelist is None:
        return current
    extra = whitelist.entries + (CLAUDE_TIER_ALIASES if kind == CLAUDE_KIND else ())
    seen = {entry.vendor_name for entry in current}
    out: list[ApiModelEntry] = list(current)
    for entry in extra:
        if entry.vendor_name in seen:
            continue
        seen.add(entry.vendor_name)   # `extra` may repeat a name across its two halves
        out.append(entry)
    return tuple(out)



def _discovered_codex_cli_entries() -> tuple[ApiModelEntry, ...]:
    """The signed-in account's own list, via the app-server's `model/list`.

    Imported inside the call for the same cycle reason as the Claude one. `()` on any failure, so
    an unauthenticated or older CLI falls back to CODEX_CLI_WHITELIST rather than serving nothing.
    """
    from shared.agent import cli_seat, codex_models
    from shared.agent.seats import codex as codex_seat

    binary = cli_seat.seat_bin(codex_seat.SPEC)
    if binary is None:
        return ()
    return tuple(
        ApiModelEntry(
            vendor_name=model_id,
            # `model/list` carries no window or tool flag; both stay at the static row's values
            # rather than being invented from a field that does not exist.
            context_window=0,
            supports_tools=True,
            supports_vision=False,
            supports_json_mode=False,
            supports_structured_outputs=False,
            notes="Codex CLI seat — id read from the signed-in account.",
        )
        for model_id in codex_models.discover(binary)
    )

def find_advertised(kind: str, advertised: str) -> ApiModelEntry | None:
    """The whitelist entry advertised under ``advertised``, or None.

    The bare vendor name is the advertised name now; the old `<kind>:<vendor>` spelling still
    resolves so a record written before the change keeps working.
    """
    whitelist = WHITELISTS.get(kind)
    if whitelist is None:
        return None
    for entry in resolvable_entries(kind):
        if advertised in (advertised_name(kind, entry), entry.vendor_name):
            return entry   # bare vendor name resolves too, so an older record still works
    return None


def probed_features(entry: ApiModelEntry) -> dict[str, bool]:
    """The entry's capabilities in the probed-dict shape ``remote.probe.capability_entry``
    consumes — API engines register these statically, never via a live probe."""
    return {
        "vision": entry.supports_vision,
        "tools": entry.supports_tools,
        # OpenAI models that support tools support parallel tool calls.
        "parallel_tool_calls": entry.supports_tools,
        "json_object": entry.supports_json_mode,
        "json_schema": entry.supports_structured_outputs,
    }


def codex_features(entry: ApiModelEntry) -> dict[str, bool]:
    """The honest feature claims for one codex model — the ONE derivation both the capability
    envelope (`remote/probe.codex_capability_entry`) and `grid catalog --api codex --json` read,
    so the two surfaces cannot disagree (issue 05).

    `parallel_tool_calls` is derived `= supports_tools` — the `probed_features` rule, and true of
    every verified codex model (facts.md #5). Chat-dialect notions (json_object/json_schema) are
    ABSENT, not False: a Responses passthrough cannot honestly claim them either way.
    """
    return {
        "vision": entry.supports_vision,
        "tools": entry.supports_tools,
        "parallel_tool_calls": entry.supports_tools,
    }


def responses_only_kind(model: str) -> str | None:
    """The API-service kind ``model`` is namespaced under, IF that kind cannot serve
    chat/completions — else ``None``.

    The `grid chat` pre-flight (ADR 0015 D-b consumer clarity): a chat request to a
    responses-only model is refused before any network round-trip, with a message saying which
    client to use instead. Data-driven from the whitelist's ``endpoints`` so a future
    responses-only kind inherits the refusal without anyone remembering this function exists.
    A name that merely contains ``:`` without being a known kind's namespace is not an API model
    and returns ``None`` — hardware engines may serve colons in model names.
    """
    kind, sep, _ = model.partition(":")
    if not sep:
        return None
    whitelist = WHITELISTS.get(kind)
    if whitelist is None or "chat/completions" in whitelist.endpoints:
        return None
    return kind


# The Responses-dialect output-token cap parameter. A kind honours a cap IFF this is NOT among its
# `unsupported_params` — the exact fact remote/serve.py `_api_unsupported_params` refuses on — so the
# auto-router's cap filter (issue 06b) and the per-kind engine gate (issue 04) read ONE source and can
# never disagree. This is the dialect's OWN spelling (`max_tokens`/`max_completion_tokens` are the
# chat-dialect spellings the relay refuses on `responses` outright).
RESPONSES_OUTPUT_CAP_PARAM = "max_output_tokens"


def kind_honours_output_cap(kind: str) -> bool:
    """True iff an API engine of ``kind`` honours a Responses output-token cap (``max_output_tokens``).

    Read from the SAME catalog fact issue 04's engine-side gate reads (``unsupported_params``), so the
    auto-router's candidate filter (issue 06b, layer 2) and the engine gate (issue 04, layer 3) can
    never disagree about a kind: ``openai`` honours it, the ``codex`` seat cannot cap under any name.
    An unknown kind is not-cap-capable — fail closed, matching how ``_static_api_caps`` and
    ``_served_endpoints`` degrade an unknown kind to the conservative answer.
    """
    whitelist = WHITELISTS.get(kind)
    if whitelist is None:
        return False
    return RESPONSES_OUTPUT_CAP_PARAM not in whitelist.unsupported_params


def kind_credential(kind: str) -> str:
    """How ``kind`` authenticates: "key" | "oauth" | "none". An UNKNOWN kind reads as "key", the
    conservative answer — it makes the caller look for a credential rather than silently serving
    without one."""
    whitelist = WHITELISTS.get(kind)
    return whitelist.credential if whitelist else "key"


def kind_is_flat_rate(kind: str) -> bool:
    """True iff ``kind`` is one operator's flat-rate allowance (a subscription seat), so the default
    poll-worker count must be 1 rather than the API-engine default. Read by
    ``run_records.effective_max_concurrency``; an unknown kind is not flat-rate (the pre-existing
    default), so this can never *lower* an existing kind's concurrency by accident."""
    whitelist = WHITELISTS.get(kind)
    return bool(whitelist and whitelist.flat_rate)


def local_seat_port(kind: str) -> int | None:
    """The loopback port a CLI-seat kind's local server binds by default, or None when ``kind`` is
    not a CLI seat. Doubles as the "is this kind a local seat?" predicate — one fact, so the two
    can never disagree."""
    whitelist = WHITELISTS.get(kind)
    return whitelist.local_seat_port if whitelist else None


def local_seat_kinds() -> tuple[str, ...]:
    """Every kind whose engine is a local process on this box — the kinds `grid join --api <kind>`
    accepts in LOCAL mode, alongside the media gateways."""
    return tuple(sorted(k for k, w in WHITELISTS.items() if w.local_seat_port is not None))


def kind_is_stream_only(kind: str) -> bool:
    """True iff an API engine of ``kind`` can serve ONLY streaming Responses requests — the codex
    subscription seat, whose backend speaks SSE only (ADR 0018 / issue 06c).

    Read from the whitelist row's ``stream_only`` field by BOTH the engine-side stream gate
    (``remote/serve.py``, which refuses a non-stream job for such a kind) and the advertised envelope
    (``remote/probe.codex_capability_entry``, which emits the negative ``stream_only`` trait the
    auto-router forbids on a non-streaming request), so the layer that refuses and the layer that routes
    around it can never disagree about which kind is stream-only.

    An unknown kind — and every hardware engine, which is never in ``WHITELISTS`` — is NOT stream-only:
    absence is the safe default (never excluded), the OPPOSITE fail-direction from
    ``kind_honours_output_cap`` and the reason a non-streaming request stays backward-compatible against
    an old CLI that advertises nothing.
    """
    whitelist = WHITELISTS.get(kind)
    if whitelist is None:
        return False
    return whitelist.stream_only


def format_api_entry(kind: str, entry: ApiModelEntry, endpoints: tuple[str, ...]) -> str:
    """The human-readable catalog line for one model: ``kind``/``entry`` name and size it, while
    ``endpoints`` (the whitelist row's — a per-KIND fact, not a per-model boolean) says which relay
    dialects the kind serves.

    Only the notable dialect — ``responses`` — is surfaced as a capability; ``chat/completions`` is
    never rendered as one, so a kind that serves only chat shows no endpoint tag at all (issue 06
    AC3). Reads the same ``endpoints`` tuple the relay filter and ``responses_only_kind`` read, so a
    new responses-serving kind lights up here for free.
    """
    caps = ", ".join(
        name
        for name, supported in (
            ("tools", entry.supports_tools),
            ("vision", entry.supports_vision),
            ("json", entry.supports_json_mode),
            ("structured", entry.supports_structured_outputs),
            # Appended last so the existing caps order and the fixed-width columns are undisturbed;
            # folding it into the SAME list (not a separate suffix after `caps or 'text only'`) is
            # what keeps a model whose only capability is the dialect rendering `responses`, never
            # the contradictory `text only, responses` (issue 06 AC4).
            ("responses", "responses" in endpoints),
        )
        if supported
    )
    # An unknown context window (the `0` sentinel — a media row, or an illustrative paid codex row
    # whose caps the pricing docs don't give, issue 10b) renders `—`, never `0 ctx` or a fabricated
    # number (DESIGN §8.1). No "ctx" unit for the unknown case; width-matched so the caps column stays
    # aligned. The known (>0) path is byte-identical to before (so the `" ctx   "` split in
    # test_format_api_entry_shows_responses_dialect is undisturbed).
    if entry.context_window > 0:
        ctx = f"{entry.context_window:>9,} ctx   "
    else:
        ctx = f"{'—':>9}       "
    return f"  {advertised_name(kind, entry):<24} {ctx}{caps or 'text only'}"
