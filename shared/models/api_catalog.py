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
    env_var: str  # the environment variable the provider's key is read from
    entries: tuple[ApiModelEntry, ...]
    supports_model_listing: bool = True  # whether the vendor exposes GET /models (media APIs like Doggi don't)


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

# One structure per kind: the verified-date and the entries can't drift apart.
WHITELISTS: dict[str, ApiWhitelist] = {
    "openai": ApiWhitelist(
        last_verified=OPENAI_LAST_VERIFIED,
        base_url="https://api.openai.com/v1",
        env_var="OPENAI_API_KEY",
        entries=OPENAI_WHITELIST,
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
