"""Pulling examples out of a helpdesk or CRM, with no network and no stored credentials.

Every request is answered by a mock transport, so these tests assert the two things that actually
matter: that we ask for the right thing, and that a refusal comes back as a sentence someone can
act on rather than a traceback.
"""
from __future__ import annotations

import json

import httpx
import pytest

from train import connectors


def _transport(handler):
    return httpx.MockTransport(handler)


def _zendesk_handler(pages: int = 1,
                     reply: str = ("Unplug the desk for sixty seconds, then hold the down button "
                                   "until it recalibrates.")):
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "/comments.json" in request.url.path:
            return httpx.Response(200, json={"comments": [
                {"public": True, "body": "my desk is stuck"},        # the customer's own message
                {"public": False, "body": "internal note, not ours to imitate"},
                {"public": True, "body": reply},
            ]})
        page = int(dict(request.url.params).get("page", "1"))
        body = {"results": [{"id": 100 + page, "subject": f"Desk {page}",
                             "description": "It shows E07", "status": "solved"}]}
        if page < pages:
            body["next_page"] = f"https://acme.zendesk.com/api/v2/search.json?page={page + 1}"
        return httpx.Response(200, json=body)

    return handle, seen


def test_zendesk_takes_the_public_reply_not_the_internal_note(monkeypatch):
    monkeypatch.setenv("ZENDESK_API_TOKEN", "t0ken")
    handler, seen = _zendesk_handler()
    pulled = connectors.pull_zendesk("acme", "me@acme.com", transport=_transport(handler))
    assert pulled.rows[0]["reply"].startswith("Unplug the desk")
    assert "internal note" not in json.dumps(pulled.rows)
    # Only solved tickets: an open one has no answer to learn from.
    assert any("status%3Asolved" in url or "status:solved" in url for url in seen)


def test_zendesk_follows_pages_and_stops_at_the_limit(monkeypatch):
    monkeypatch.setenv("ZENDESK_API_TOKEN", "t0ken")
    handler, _ = _zendesk_handler(pages=3)
    pulled = connectors.pull_zendesk("acme", "me@acme.com", transport=_transport(handler))
    assert pulled.pages == 3 and len(pulled.rows) == 3

    handler, _ = _zendesk_handler(pages=3)
    capped = connectors.pull_zendesk("acme", "me@acme.com", max_rows=2,
                                     transport=_transport(handler))
    assert len(capped.rows) == 2 and capped.truncated


def test_a_ticket_with_no_public_reply_is_dropped(monkeypatch):
    monkeypatch.setenv("ZENDESK_API_TOKEN", "t0ken")
    handler, _ = _zendesk_handler(reply="")
    pulled = connectors.pull_zendesk("acme", "me@acme.com", transport=_transport(handler))
    assert pulled.rows == []          # nothing to imitate, so nothing kept


def test_a_missing_token_says_where_to_get_one_and_not_to_write_it_down(monkeypatch):
    monkeypatch.delenv("ZENDESK_API_TOKEN", raising=False)
    with pytest.raises(connectors.ConnectorError) as exc:
        connectors.pull_zendesk("acme", "me@acme.com")
    assert "Admin Center" in str(exc.value)
    assert "never written to a file" in str(exc.value)


@pytest.mark.parametrize("status,needle", [
    (401, "refused the token"), (403, "lacks permission"),
    (404, "Check the subdomain"), (429, "rate-limiting"),
])
def test_every_refusal_is_a_sentence_not_a_traceback(status, needle, monkeypatch):
    monkeypatch.setenv("ZENDESK_API_TOKEN", "t0ken")
    transport = _transport(lambda request: httpx.Response(status, json={}))
    with pytest.raises(connectors.ConnectorError) as exc:
        connectors.pull_zendesk("acme", "me@acme.com", transport=transport)
    assert needle in str(exc.value)


def test_hubspot_keeps_deals_that_have_both_a_story_and_an_outcome(monkeypatch):
    monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", "pat-x")
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        after = dict(request.url.params).get("after")
        if after:
            return httpx.Response(200, json={"results": [
                {"properties": {"description": "second page deal", "dealstage": "closedlost"}}]})
        return httpx.Response(200, json={
            "results": [
                {"properties": {"description": "30 desks, budget approved",
                                "dealstage": "closedwon", "dealname": "Acme"}},
                {"properties": {"description": "", "dealstage": "closedwon"}},   # no story
                {"properties": {"description": "asked a question", "dealstage": ""}},  # no outcome
            ],
            "paging": {"next": {"after": "2"}}})

    pulled = connectors.pull_hubspot(transport=_transport(handle))
    assert [row["outcome"] for row in pulled.rows] == ["closedwon", "closedlost"]
    assert pulled.pages == 2
    # The token travels in the header and nowhere else.
    assert seen[0].headers["authorization"] == "Bearer pat-x"
    assert "pat-x" not in str(seen[0].url)


def test_what_is_pulled_feeds_the_same_preparation_as_an_upload(monkeypatch, tmp_path):
    """The point of writing raw rows: one code path decides whether data is trainable."""
    from train.web import prepare

    monkeypatch.setenv("ZENDESK_API_TOKEN", "t0ken")
    handler, _ = _zendesk_handler(pages=60)
    pulled = connectors.pull_zendesk("acme", "me@acme.com", transport=_transport(handler))
    path = connectors.write_jsonl(pulled, tmp_path / "zendesk-export.jsonl")

    report, _ = prepare.prepare("support-replies", path.read_text(encoding="utf-8"),
                                path.name)
    assert report.rows_usable == len(pulled.rows)
    assert report.columns_used["reply"] == "reply"


def test_a_runaway_cursor_cannot_loop_forever(monkeypatch):
    """A vendor that always returns a next page must not spin until the disk fills."""
    monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", "pat-x")
    transport = _transport(lambda request: httpx.Response(200, json={
        "results": [{"properties": {"description": "x", "dealstage": "s"}}],
        "paging": {"next": {"after": "always"}}}))
    pulled = connectors.pull_hubspot(max_rows=10_000, transport=transport)
    assert pulled.pages == connectors._PAGE_LIMIT


def test_a_rate_limited_ticket_is_not_recorded_as_having_no_reply(monkeypatch):
    """The dangerous version: 5,000 tickets behind a 429 all look like tickets nobody answered."""
    monkeypatch.setenv("ZENDESK_API_TOKEN", "t0ken")
    monkeypatch.setattr(connectors, "_wait", lambda retry_after: None)
    seen = {"comments": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        if "/comments.json" in request.url.path:
            seen["comments"] += 1
            return httpx.Response(429, headers={"retry-after": "1"}, json={})
        return httpx.Response(200, json={"results": [
            {"id": 1, "subject": "s", "description": "d", "status": "solved"},
            {"id": 2, "subject": "s", "description": "d", "status": "solved"},
        ]})

    pulled = connectors.pull_zendesk("acme", "me@acme.com", transport=_transport(handle))
    assert pulled.rows == []
    assert pulled.unreadable == 2
    assert seen["comments"] == 4                      # one retry each, then give up
    assert "could not be read" in pulled.detail
    assert "left out rather than recorded as having no reply" in pulled.trustworthy


def test_one_network_hiccup_does_not_throw_away_the_whole_pull(monkeypatch):
    monkeypatch.setenv("ZENDESK_API_TOKEN", "t0ken")
    calls = {"n": 0}
    reply = "Unplug the desk for sixty seconds and hold the down button until it recalibrates."

    def handle(request: httpx.Request) -> httpx.Response:
        if "/comments.json" in request.url.path:
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectError("connection reset")
            return httpx.Response(200, json={"comments": [
                {"public": True, "body": "the customer"}, {"public": True, "body": reply}]})
        return httpx.Response(200, json={"results": [
            {"id": 1, "subject": "s", "description": "d", "status": "solved"}]})

    pulled = connectors.pull_zendesk("acme", "me@acme.com", transport=_transport(handle))
    assert len(pulled.rows) == 1 and pulled.rows[0]["reply"] == reply   # retried, not abandoned


def test_a_partial_pull_never_reads_as_a_complete_one(monkeypatch):
    """Hitting the page cap on a big CRM must not print like a full history."""
    monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", "pat-x")
    transport = _transport(lambda request: httpx.Response(200, json={
        "results": [{"properties": {"description": "x", "dealstage": "closedwon"}}],
        "paging": {"next": {"after": "always"}}}))
    pulled = connectors.pull_hubspot(max_rows=10_000, transport=transport)
    assert pulled.pages == connectors._PAGE_LIMIT
    assert pulled.truncated and not pulled.complete
    assert "safety limit" in pulled.detail
    assert "PARTIAL" in pulled.trustworthy
