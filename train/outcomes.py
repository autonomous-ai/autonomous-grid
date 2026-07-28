"""Asking the system of record what actually happened, so nobody has to be asked.

Capture keeps every answer the grid gave. Feedback records what a person did with one — but only
if an app was wired to say so, which means a business gets continuous learning exactly as far as
its integrations reach, and no further. That is the gap between "nightly training on captured work"
and the thing the product claims.

The fix is not another integration to write. **The helpdesk already knows.** The ticket carries the
reply that was actually sent and whether it stayed solved; the CRM carries the stage the deal ended
in. Pulling that back and lining it up against what we served produces the same three verdicts a
person would have given, with the human's own text as the truth:

    we drafted X · the ticket's public reply is X       → accepted   (weight 0.6)
    we drafted X · the reply sent was Y                 → edited, and Y is ground truth (1.0)
    we drafted X · nothing was sent, or it reopened     → rejected  (never imitated)

Three rules hold this honest:

1. **A person's verdict always wins.** If feedback already exists for a request, this never
   overwrites it. An automated guess is the weakest signal in the store, not the loudest.
2. **We only join what we can prove is the same piece of work.** A request carries a reference
   (`X-Grid-Ref: zendesk:12345`) if the app sets one; otherwise the reference is read out of the
   prompt itself, and only when exactly one candidate id is present. Two ids, or none, is a row we
   leave alone — a wrong join teaches the model somebody else's answer.
3. **Nothing here writes a reward.** It writes the same feedback rows a human would, and the
   existing weights (train/capture.py) decide what those are worth.
"""
from __future__ import annotations

import dataclasses
import difflib
import re

from .capture import VERDICTS, Policy, _read_days, load_policy, record_feedback

# What "the same answer" means. Word-level, because whitespace and a signature block are not edits.
SAME_ENOUGH = 0.90

# Reference shapes we can recognise in a prompt without being told. Deliberately narrow: a false
# join is worse than no join.
_REF_PATTERNS = {
    "zendesk": re.compile(r"\b(?:ticket|case)\s*#?\s*(\d{3,9})\b", re.IGNORECASE),
    "hubspot": re.compile(r"\b(?:deal|opportunity)\s*#?\s*(\d{3,12})\b", re.IGNORECASE),
}


@dataclasses.dataclass
class Joined:
    """What one pass over the system of record concluded."""

    source: str
    looked_at: int = 0        # captured rows in the window
    matched: int = 0          # rows we could tie to a record
    accepted: int = 0
    edited: int = 0
    rejected: int = 0
    skipped_human: int = 0    # a person had already said
    unmatched: int = 0        # no reference we would trust
    unreadable: int = 0       # the vendor would not tell us
    detail: str = ""

    @property
    def written(self) -> int:
        return self.accepted + self.edited + self.rejected

    def summary(self) -> str:
        if not self.looked_at:
            return "Nothing captured in that window."
        parts = [f"{self.written} outcomes joined from {self.source}",
                 f"{self.edited} rewritten by a person",
                 f"{self.accepted} sent as we wrote them",
                 f"{self.rejected} not used"]
        if self.skipped_human:
            parts.append(f"{self.skipped_human} already had a person's verdict")
        if self.unmatched:
            parts.append(f"{self.unmatched} had no reference we would trust")
        if self.unreadable:
            parts.append(f"{self.unreadable} could not be read")
        return " · ".join(parts) + "."


def reference_for(row: dict, source: str) -> str | None:
    """The record this answer was about, if we can say so without guessing.

    An explicit `ref` (set by the app through the `X-Grid-Ref` header) is authoritative. Otherwise
    the prompt is searched, and **exactly one** candidate must be present: a prompt that mentions
    two ticket numbers is a prompt we cannot attribute, and attributing it anyway would train the
    model on another ticket's reply.
    """
    explicit = str(row.get("ref") or "").strip()
    if explicit:
        kind, _, ident = explicit.partition(":")
        if not ident:
            return explicit
        return ident if kind.lower() in (source, "") else None
    pattern = _REF_PATTERNS.get(source)
    if not pattern:
        return None
    found = {m.group(1) for m in pattern.finditer(str(row.get("prompt") or ""))}
    return found.pop() if len(found) == 1 else None


def verdict_for(ours: str, theirs: str, *, resolved: bool) -> tuple[str, str]:
    """(verdict, ground truth) for one answer, given what the record now holds."""
    ours, theirs = (ours or "").strip(), (theirs or "").strip()
    if not resolved or not theirs:
        # Nothing was sent, or the work came back. Either way this answer is not one to imitate.
        return "rejected", ""
    ratio = difflib.SequenceMatcher(None, ours.lower().split(), theirs.lower().split()).ratio()
    if ratio >= SAME_ENOUGH:
        return "accepted", ""
    return "edited", theirs


def join(source: str, records: dict[str, dict], *, days: int = 7,
         policy: Policy | None = None, dry_run: bool = False) -> Joined:
    """Line captured answers up against `records` (reference → {reply, resolved}).

    Pure: the caller fetches from the vendor, this decides. That split is what makes every rule
    above testable without a network, and it is why a new source is a fetcher, not a rewrite.
    """
    policy = policy or load_policy()
    result = Joined(source=source)
    traffic = _read_days("traffic", days)
    known = {row.get("id") for row in _read_days("feedback", days) if row.get("id")}
    result.looked_at = len(traffic)

    for row in traffic:
        request_id = row.get("id")
        if not request_id:
            continue
        if request_id in known:
            result.skipped_human += 1          # a person already said; never overwrite them
            continue
        ref = reference_for(row, source)
        if not ref:
            result.unmatched += 1
            continue
        record = records.get(str(ref))
        if record is None:
            result.unreadable += 1
            continue
        result.matched += 1
        verdict, truth = verdict_for(row.get("completion", ""), record.get("reply", ""),
                                     resolved=bool(record.get("resolved")))
        assert verdict in VERDICTS
        setattr(result, verdict, getattr(result, verdict) + 1)
        if not dry_run:
            record_feedback(request_id, verdict, final_text=truth, policy=policy)

    result.detail = result.summary()
    return result


# --- fetchers: the only part that talks to a vendor -----------------------------------------

def zendesk_records(subdomain: str, email: str, refs: list[str], *, transport=None) -> dict:
    """{ticket id → {reply, resolved}} for the tickets we actually need."""
    from .connectors import UNREADABLE, _client, _token, _zendesk_reply

    if not refs:
        return {}
    token = _token("ZENDESK_API_TOKEN", "an API token in Zendesk (Admin Center → Apps and "
                                        "integrations → Zendesk API)")
    base = f"https://{subdomain.strip().strip('/')}.zendesk.com/api/v2"
    out: dict[str, dict] = {}
    with _client(transport, auth=(f"{email}/token", token)) as client:
        for ref in dict.fromkeys(refs):                 # unique, order preserved
            response = client.get(f"{base}/tickets/{ref}.json")
            if response.status_code != 200:
                continue                                 # counted as unreadable by join()
            ticket = (response.json() or {}).get("ticket") or {}
            reply = _zendesk_reply(client, base, ref)
            out[str(ref)] = {
                "reply": "" if reply is UNREADABLE else reply,
                # Solved or closed, and nobody reopened it. A reopened ticket is the clearest
                # negative signal a helpdesk produces.
                "resolved": str(ticket.get("status", "")).lower() in ("solved", "closed"),
            }
    return out


def refs_needed(source: str, days: int = 7) -> list[str]:
    """Which records we would have to fetch to judge the window — so we fetch only those."""
    known = {row.get("id") for row in _read_days("feedback", days) if row.get("id")}
    refs = []
    for row in _read_days("traffic", days):
        if row.get("id") in known:
            continue
        ref = reference_for(row, source)
        if ref:
            refs.append(str(ref))
    return refs
