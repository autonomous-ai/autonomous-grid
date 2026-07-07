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
    entries: tuple[ApiModelEntry, ...]


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

# One structure per kind: the verified-date and the entries can't drift apart.
WHITELISTS: dict[str, ApiWhitelist] = {
    "openai": ApiWhitelist(last_verified=OPENAI_LAST_VERIFIED, entries=OPENAI_WHITELIST),
}


def supported_kinds() -> tuple[str, ...]:
    return tuple(sorted(WHITELISTS))


def advertised_name(kind: str, entry: ApiModelEntry) -> str:
    return f"{kind}:{entry.vendor_name}"


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
