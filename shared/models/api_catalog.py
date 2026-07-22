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

# The vendor's own `PlanType` vocabulary, read from the client binary (facts.md #5). Distinguishes
# "unrecognized tier" (vendor drift — outside this set) from "known but unverified" (inside it, no
# populated row in CODEX_TIER_MODELS). Neither advertises beyond the minimal row; only the warn
# wording turns on the difference, and that wording lives with the CLI (issue 05).
CODEX_PLAN_TYPES = frozenset({
    "free", "go", "plus", "pro", "prolite", "team", "self_serve_business_usage_based",
    "business", "enterprise_cbp_usage_based", "enterprise", "hc", "edu", "education",
})

# Per-tier rows: ONLY a tier verified against a live seat may be populated — paid tiers are
# UNRESOLVED-NOACCESS and must not be filled from guesswork (facts.md #5); an absent row means
# "unverified", never "empty tier". Vendor priority order preserved. `codex-auto-review`
# (visibility: "hide") is deliberately excluded. The json-mode/structured-outputs booleans are
# False because they are chat-dialect notions a Responses passthrough cannot honestly claim — and
# the capability envelope OMITS those keys outright rather than advertising False
# (remote/probe.codex_capability_entry, issue 05).
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

CODEX_TIER_MODELS: dict[str, tuple[ApiModelEntry, ...]] = {"free": _CODEX_FREE_MODELS}

# What a missing, unrecognized, or unverified tier advertises (ADR 0015 D-f: never the full
# table). Free is the least-entitled tier, so degrading to it can only under-advertise.
CODEX_MINIMAL_TIER = "free"


def _codex_tier_union() -> tuple[ApiModelEntry, ...]:
    """The flat ``entries`` for the codex whitelist row: every tier row merged, first occurrence
    wins. Keeping ``entries`` = the union is what lets the kind-generic helpers — ``find_advertised``,
    the join's ``-m`` validation — resolve every codex model without learning about tiers."""
    merged: dict[str, ApiModelEntry] = {}
    for entries in CODEX_TIER_MODELS.values():
        for entry in entries:
            merged.setdefault(entry.vendor_name, entry)
    return tuple(merged.values())

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
    ),
    "codex": ApiWhitelist(
        last_verified=CODEX_LAST_VERIFIED,
        base_url="https://chatgpt.com/backend-api/codex",
        # The union of the tier rows (issue 05); per-tier selection is `codex_tier_entries`.
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
    return f"{kind}:{entry.vendor_name}"


def find_advertised(kind: str, advertised: str) -> ApiModelEntry | None:
    """The whitelist entry advertised under ``advertised`` (e.g. ``openai:gpt-5.5``), or None.

    Only the namespaced form resolves — a bare vendor name is not an advertised name.
    """
    whitelist = WHITELISTS.get(kind)
    if whitelist is None:
        return None
    for entry in whitelist.entries:
        if advertised_name(kind, entry) == advertised:
            return entry
    return None


def codex_effective_tier(plan_type: str | None) -> str:
    """The tier whose row a seat claiming ``plan_type`` advertises: its own when populated, else
    the minimal tier. Split from ``codex_tier_entries`` because operator messages need the NAME
    of the row they ended up on, not just its contents."""
    if plan_type is not None and plan_type in CODEX_TIER_MODELS:
        return plan_type
    return CODEX_MINIMAL_TIER


def codex_tier_entries(plan_type: str | None) -> tuple[ApiModelEntry, ...]:
    """The tier row a seat claiming ``plan_type`` may advertise (ADR 0015 D-f).

    Missing (``None``), unrecognized (outside ``CODEX_PLAN_TYPES``), and known-but-unverified (no
    populated row) tiers all degrade to the minimal row — the join must never widen on a guess.
    Pure lookup: the operator-facing warns for the three degrade cases are the CLI's (issue 05),
    because they differ only in wording, not in what is advertised.
    """
    return CODEX_TIER_MODELS[codex_effective_tier(plan_type)]


def codex_vendor_rank(plan_type: str | None, vendor_name: str) -> int | None:
    """The 1-based capability rank of ``vendor_name`` within the seat's effective tier row
    (``codex_tier_entries``): 1 = the row's most-capable head, the curated order we own (ADR 0016).
    ``None`` when the model is absent from that row — a drifted model omits the fact rather than
    fabricating a position, so the master renders nothing for it and ordering degrades gracefully.

    The tier ROW, not the flat ``entries`` union, is the authority: two tiers may order the same
    model differently, and a seat advertises exactly one tier's row (``codex_tier_entries`` applies
    the same D-f degrade the join used to pick that row). Positions are contiguous ``1..N`` because
    each row is duplicate-free (pinned by ``test_codex_tier_whitelist_integrity``). Sourced from
    the whitelist order, never the vendor's unverified ``priority`` field (ADR 0016)."""
    for index, entry in enumerate(codex_tier_entries(plan_type)):
        if entry.vendor_name == vendor_name:
            return index + 1
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


def format_api_entry(kind: str, entry: ApiModelEntry) -> str:
    caps = ", ".join(
        name
        for name, supported in (
            ("tools", entry.supports_tools),
            ("vision", entry.supports_vision),
            ("json", entry.supports_json_mode),
            ("structured", entry.supports_structured_outputs),
        )
        if supported
    )
    return (
        f"  {advertised_name(kind, entry):<24} {entry.context_window:>9,} ctx   "
        f"{caps or 'text only'}"
    )
