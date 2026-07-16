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
    base_url: str  # the vendor endpoint jobs forward to, no trailing slash
    entries: tuple[ApiModelEntry, ...]
    # The environment variable the provider's key is read from — the first step of ADR 0012 D-c's
    # env → stored → prompt precedence. **None for a kind with no env-var input path**: a `codex`
    # seat is a rotating OAuth bundle, which cannot be an env var, and ADR 0015 D-c bars the path
    # outright so that a stray `CODEX_API_KEY` can never masquerade as a signed-in subscription.
    # Ordered after `entries` because it is now optional and `entries` is not.
    env_var: str | None = None
    # The vendor's name for the output-token cap. The grid speaks `max_tokens` internally — the only
    # name hardware engines know — so a vendor that renamed it needs the value translated on the way
    # out, or every job 400s. See `_adapt_output_token_param` in remote/serve.py.
    # **None when the vendor has no such parameter under any name** — verified for codex, whose
    # backend rejects every candidate rather than ignoring it (facts.md #1). None means "do not
    # translate", which is not the same as "translate to the default name".
    max_output_param: str | None = "max_tokens"
    # Request parameters the vendor rejects outright (no translation exists). A job carrying one is
    # refused by the provider before any upstream call, wearing the vendor's own error shape — same
    # outcome as forwarding, minus the round-trip. Null values are NOT refused (the vendor accepts
    # them). See `_api_unsupported_params` in remote/serve.py.
    unsupported_params: tuple[str, ...] = ()


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

# The seat's backend (ADR 0015). Verified live on 2026-07-15 (spike 01, `.scratch/codex-subs/facts.md`).
# The model entries are deliberately EMPTY here: a codex seat's model set is bounded by its
# subscription tier, so the table is keyed by tier rather than flat, and populating it is issue 05's
# — with only the free tier verified, any paid-tier row today would be guesswork (ADR 0015 D-f's
# "unknown ⇒ minimal, never full" is what protects the gap meanwhile).
CODEX_LAST_VERIFIED = "2026-07-15"

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
        entries=(),  # issue 05's, keyed by subscription tier
        env_var=None,  # ADR 0015 D-c: an OAuth seat has no env-var input path
        max_output_param=None,  # facts.md #1: this backend has no output-cap parameter, under any name
        # Refused before the round-trip rather than translated, because no translation exists. The
        # three cap names each return `400 {"detail":"Unsupported parameter: ..."}`, as does
        # `temperature` — this backend runs a small allowlist and denies chat-era knobs outright
        # rather than ignoring them (facts.md #1, #7).
        unsupported_params=("max_tokens", "max_output_tokens", "max_completion_tokens", "temperature"),
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
