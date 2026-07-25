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


@dataclasses.dataclass(frozen=True)
class Pulled:
    source: str
    rows: list[dict]
    pages: int
    truncated: bool
    detail: str


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
    pages = 0
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
                rows.append({
                    "subject": ticket.get("subject") or "",
                    "description": ticket.get("description") or "",
                    "reply": _zendesk_reply(client, base, ticket.get("id")),
                    "status": ticket.get("status") or "",
                })
                if len(rows) >= max_rows:
                    break
            url, params = body.get("next_page"), None
    kept = [row for row in rows if row["reply"]]
    return Pulled("zendesk", kept, pages, len(rows) >= max_rows,
                  f"{len(kept)} solved tickets with a public reply, from {pages} page(s).")


def _zendesk_reply(client, base: str, ticket_id) -> str:
    """The first public agent comment — what the team actually sent."""
    if not ticket_id:
        return ""
    response = client.get(f"{base}/tickets/{ticket_id}/comments.json")
    if response.status_code != 200:
        return ""
    comments = response.json().get("comments", []) or []
    for comment in comments[1:]:            # [0] is the customer's own first message
        if comment.get("public") and (comment.get("body") or "").strip():
            return comment["body"].strip()
    return ""


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
    return Pulled("hubspot", rows, pages, len(rows) >= max_rows,
                  f"{len(rows)} deals with both a description and a stage, from {pages} page(s).")


# --- writing what we pulled ---------------------------------------------------------------------

def write_jsonl(pulled: Pulled, dest: Path) -> Path:
    """One row per line, raw — the same thing an export would have given us."""
    dest = Path(dest).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in pulled.rows),
                    encoding="utf-8")
    return dest


PULLERS = {"zendesk": pull_zendesk, "hubspot": pull_hubspot}
