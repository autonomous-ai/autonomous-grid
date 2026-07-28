"""Pull training examples straight out of the tools a business already runs.

The browser's upload path is the one a manager uses, and it works from any export. This module is
for the other half of the room: whoever administers the helpdesk, who would rather run one command
on a schedule than re-export a CSV every month.

What it does NOT do is as important as what it does:

* **No credentials are stored.** The token comes from an environment variable, is never written to
  disk, and never appears in output. A connector that saved an API key into a workspace folder
  would be a worse liability than the training it enables.
* **Nothing leaves the network except the request to that vendor.** These pull *from* a SaaS tool
  the business already trusts with this data, into local files. There is no round trip anywhere
  else, and the trained model never sees a vendor.
* **It writes the same shape the upload path produces** — a JSONL of raw rows — so it feeds
  `train.web.prepare` unchanged, and the column-guessing, the honest report and the refusals all
  apply identically. One code path decides whether data is trainable.

Transport is injectable so every path here is tested without a network.
"""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

_PAGE_LIMIT = 100          # pages, not rows: a stop so a bad cursor cannot loop forever
_WAIT_BUDGET = 120.0       # seconds of rate-limit waiting for a whole pull, not per item
_GIVE_UP_AFTER = 25        # consecutive unreadable items before we stop and say so


@dataclasses.dataclass(frozen=True)
class Pulled:
    source: str
    rows: list[dict]
    pages: int
    truncated: bool
    detail: str
    unreadable: int = 0        # rows we fetched but could not complete (rate limits, hiccups)
    complete: bool = True      # False when the vendor stopped us short of the history
    fetched: int = 0           # rows the vendor handed us, before any were dropped

    @property
    def trustworthy(self) -> str:
        """What a person needs to know before treating this file as their history."""
        if self.complete and not self.unreadable:
            return ""
        parts = []
        if not self.complete:
            parts.append("this is a PARTIAL pull")
        if self.unreadable:
            parts.append(f"{self.unreadable} item(s) could not be read (rate limits or network) "
                         "and were left out rather than recorded as having no reply")
        return " — ".join(parts)


class ConnectorError(RuntimeError):
    """Something a person can fix: a missing token, a wrong subdomain, a refused key."""


def _client(transport=None, *, headers: dict | None = None, auth=None):
    import httpx

    return httpx.Client(timeout=60.0, transport=transport, headers=headers or {}, auth=auth)


def _token(env_var: str, what: str) -> str:
    value = (os.environ.get(env_var) or "").strip()
    if not value:
        raise ConnectorError(
            f"{env_var} is not set. Create {what} and put it in that variable — "
            f"`export {env_var}=…` — so it is never written to a file."
        )
    return value


def _explain(status: int, source: str) -> str:
    return {
        401: f"{source} refused the token (401). Check the key, and that it has read access.",
        403: f"{source} refused the request (403). The key is valid but lacks permission to read "
             "this data.",
        404: f"{source} has no such account or endpoint (404). Check the subdomain.",
        429: f"{source} is rate-limiting us (429). Wait a minute and run it again.",
    }.get(status, f"{source} returned {status}.")


# --- Zendesk ------------------------------------------------------------------------------------

def pull_zendesk(subdomain: str, email: str, *, max_rows: int = 5000, status: str = "solved",
                 transport=None, token_env: str = "ZENDESK_API_TOKEN") -> Pulled:
    """Solved tickets with the first customer message and the agent's public reply.

    Only solved/closed tickets by default: an open ticket has no answer to learn from, and a
    half-finished thread teaches the model to write half-finished replies.
    """
    import httpx

    token = _token(token_env, "an API token in Zendesk (Admin Center → Apps and integrations "
                              "→ Zendesk API)")
    base = f"https://{subdomain.strip().strip('/')}.zendesk.com/api/v2"
    auth = (f"{email}/token", token)
    rows: list[dict] = []
    unreadable = 0
    in_a_row = 0
    fetched = 0
    pages = 0
    hit_page_limit = False
    gave_up = False
    budget = _Budget()
    url = f"{base}/search.json"
    params = {"query": f"type:ticket status:{status}", "sort_by": "created_at", "sort_order": "desc"}
    with _client(transport, auth=auth) as client:
        while url and len(rows) < max_rows and pages < _PAGE_LIMIT:
            try:
                response = client.get(url, params=params if pages == 0 else None)
            except httpx.HTTPError as exc:
                raise ConnectorError(f"could not reach Zendesk: {exc}") from exc
            if response.status_code != 200:
                raise ConnectorError(_explain(response.status_code, "Zendesk"))
            body = response.json()
            pages += 1
            for ticket in body.get("results", []) or []:
                fetched += 1
                reply = _zendesk_reply(client, base, ticket.get("id"), budget)
                if reply is UNREADABLE:
                    unreadable += 1
                    in_a_row += 1
                    if in_a_row >= _GIVE_UP_AFTER:
                        # The vendor is refusing, not hiccuping. Grinding on to page 100 produces
                        # a file that looks like history and is mostly absence.
                        gave_up = True
                        break
                    continue
                in_a_row = 0
                rows.append({
                    "subject": ticket.get("subject") or "",
                    "description": ticket.get("description") or "",
                    "reply": reply,
                    "status": ticket.get("status") or "",
                })
                if len(rows) >= max_rows:
                    break
            if gave_up:
                break
            url, params = body.get("next_page"), None
            if url and pages >= _PAGE_LIMIT:
                hit_page_limit = True
    kept = [row for row in rows if row["reply"]]
    stopped_early = fetched >= max_rows or hit_page_limit or gave_up
    detail = f"{len(kept)} solved tickets with a public reply, from {pages} page(s)."
    if gave_up:
        detail += (f" Stopped after {_GIVE_UP_AFTER} tickets in a row we could not read — the "
                   "vendor is rate-limiting us. Run it again later; nothing was lost.")
    elif hit_page_limit:
        detail += (f" Stopped at the {_PAGE_LIMIT}-page safety limit — there is more history than "
                   "this file contains.")
    if unreadable:
        detail += f" {unreadable} ticket(s) could not be read and were left out."
    # Unreadable rows make a pull incomplete even when we reach the end: the file is missing
    # tickets the account has, which is exactly what "complete" is asked about.
    return Pulled("zendesk", kept, pages, stopped_early, detail, unreadable=unreadable,
                  complete=not stopped_early and not unreadable, fetched=fetched)


UNREADABLE = object()      # distinct from "": we could not read it, vs it genuinely had no reply


def _zendesk_reply(client, base: str, ticket_id, budget=None):
    """The first public agent comment — or UNREADABLE if we could not find out.

    Returning "" for a rate-limited or failed fetch is the dangerous version: 5,000 tickets behind
    a 429 all look like tickets your team never answered, every one is dropped, and the report
    says so confidently. One retry, then say we do not know.
    """
    import httpx

    if not ticket_id:
        return ""
    url = f"{base}/tickets/{ticket_id}/comments.json"
    for attempt in (0, 1):
        try:
            response = client.get(url)
        except httpx.HTTPError:
            continue                        # a hiccup on one ticket must not end the pull
        if response.status_code == 200:
            comments = response.json().get("comments", []) or []
            for comment in comments[1:]:    # [0] is the customer's own first message
                if comment.get("public") and (comment.get("body") or "").strip():
                    return comment["body"].strip()
            return ""                       # read it; there was genuinely no public reply
        if attempt == 0 and (response.status_code == 429 or response.status_code >= 500):
            # A rate limit and a server hiccup both deserve one retry; only the rate limit costs
            # waiting time, and only against the whole pull's budget.
            if (response.status_code == 429 and budget is not None
                    and not budget.wait(response.headers.get("retry-after"))):
                break                       # out of patience; the caller counts this unreadable
            continue
        break
    return UNREADABLE


class _Budget:
    """How long this pull may spend waiting, in total.

    A per-item cap is not a limit. Thirty seconds, applied once per ticket across ten thousand
    rate-limited tickets, is eighty-three hours of silent sleeping — the scheduled pull that hit it
    was still going when the next night's started, and it wrote nothing.
    """

    def __init__(self, seconds: float = _WAIT_BUDGET) -> None:
        self.left = seconds

    def wait(self, retry_after: str | None) -> bool:
        """Sleep if we can still afford it. False means the budget is spent — stop pulling."""
        import time

        if self.left <= 0:
            return False
        try:
            asked = float(retry_after or 2)
        except ValueError:
            asked = 2.0
        seconds = max(min(asked, 30.0, self.left), 0.0)
        self.left -= max(seconds, 1.0)      # a zero-length wait still costs, so this terminates
        time.sleep(seconds)
        return True


# --- HubSpot ------------------------------------------------------------------------------------

_HUBSPOT_PROPS = ("dealname", "dealstage", "description", "notes_last_contacted", "amount")


def pull_hubspot(*, max_rows: int = 5000, transport=None,
                 token_env: str = "HUBSPOT_ACCESS_TOKEN") -> Pulled:
    """Deals with their stage — the answer key for lead triage is what actually closed."""
    import httpx

    token = _token(token_env, "a private app token in HubSpot (Settings → Integrations → "
                              "Private Apps, scope crm.objects.deals.read)")
    rows: list[dict] = []
    pages = 0
    after = None
    hit_page_limit = False
    with _client(transport, headers={"authorization": f"Bearer {token}"}) as client:
        while len(rows) < max_rows and pages < _PAGE_LIMIT:
            params = {"limit": 100, "properties": ",".join(_HUBSPOT_PROPS)}
            if after:
                params["after"] = after
            try:
                response = client.get("https://api.hubapi.com/crm/v3/objects/deals", params=params)
            except httpx.HTTPError as exc:
                raise ConnectorError(f"could not reach HubSpot: {exc}") from exc
            if response.status_code != 200:
                raise ConnectorError(_explain(response.status_code, "HubSpot"))
            body = response.json()
            pages += 1
            for deal in body.get("results", []) or []:
                props = deal.get("properties") or {}
                text = (props.get("description") or props.get("notes_last_contacted") or "").strip()
                stage = (props.get("dealstage") or "").strip()
                if text and stage:
                    rows.append({"lead": text, "outcome": stage,
                                 "name": props.get("dealname") or ""})
            after = ((body.get("paging") or {}).get("next") or {}).get("after")
            if not after:
                break
            if pages >= _PAGE_LIMIT:
                hit_page_limit = True
    stopped_early = len(rows) >= max_rows or hit_page_limit
    detail = f"{len(rows)} deals with both a description and a stage, from {pages} page(s)."
    if hit_page_limit:
        detail += (f" Stopped at the {_PAGE_LIMIT}-page safety limit — there is more history than "
                   "this file contains.")
    return Pulled("hubspot", rows, pages, stopped_early, detail, complete=not stopped_early,
                  fetched=len(rows))


# --- writing what we pulled ---------------------------------------------------------------------

def write_jsonl(pulled: Pulled, dest: Path) -> Path:
    """One row per line, raw — the same thing an export would have given us."""
    dest = Path(dest).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in pulled.rows),
                    encoding="utf-8")
    return dest


PULLERS = {"zendesk": pull_zendesk, "hubspot": pull_hubspot}
