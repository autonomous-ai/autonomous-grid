"""Asking the system of record what happened — the station that makes the loop continuous.

Everything here is about *not* being confidently wrong. A join that attributes one team's answer to
another team's ticket teaches the model somebody else's job, and it does it silently, so the rules
about when we refuse to join matter more than the happy path.
"""
from __future__ import annotations

import httpx
import pytest

from train import capture, outcomes


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    capture.save_policy(capture.Policy(enabled=True))
    return tmp_path


def _served(prompt: str, completion: str, request_id: str, ref: str = "") -> None:
    capture.record(prompt=prompt, completion=completion, model="support-v1",
                   request_id=request_id, ref=ref)
    capture.flush()


DRAFT = ("Sorry about the E07 — unplug the desk for sixty seconds, then hold the down button for "
         "ten seconds until it recalibrates.")


def test_a_reply_sent_as_we_wrote_it_is_weaker_than_one_a_person_rewrote():
    assert outcomes.verdict_for(DRAFT, DRAFT, resolved=True) == ("accepted", "")
    verdict, truth = outcomes.verdict_for(DRAFT, "Unplug it for a minute and hold DOWN for ten "
                                                 "seconds. If it comes back I will ship you a new "
                                                 "controller under warranty.", resolved=True)
    assert verdict == "edited"
    assert truth.startswith("Unplug it for a minute")      # the human's text is the ground truth


def test_a_reply_that_was_never_sent_is_never_imitated():
    assert outcomes.verdict_for(DRAFT, "", resolved=True) == ("rejected", "")
    assert outcomes.verdict_for(DRAFT, DRAFT, resolved=False) == ("rejected", "")


def test_whitespace_and_a_signature_are_not_an_edit():
    """A word-level comparison, because "the same reply" survives reformatting."""
    padded = "  " + DRAFT.replace(" — ", " - ") + "\n\n"
    assert outcomes.verdict_for(DRAFT, padded, resolved=True)[0] == "accepted"


def test_an_explicit_reference_beats_reading_the_prompt():
    row = {"ref": "zendesk:99", "prompt": "about ticket 12345 and ticket 6789"}
    assert outcomes.reference_for(row, "zendesk") == "99"


def test_two_candidate_ids_in_one_prompt_are_not_joined():
    """A prompt naming two tickets is a prompt we cannot attribute. Attributing it anyway would
    train the model on another ticket's reply — silently, and forever."""
    two = {"prompt": "see ticket 12345, related to ticket 67890"}
    assert outcomes.reference_for(two, "zendesk") is None
    one = {"prompt": "Ticket #12345: my desk shows E07"}
    assert outcomes.reference_for(one, "zendesk") == "12345"
    none = {"prompt": "my desk shows E07"}
    assert outcomes.reference_for(none, "zendesk") is None


def test_a_reference_for_another_system_is_not_used():
    row = {"ref": "hubspot:4242", "prompt": "ticket 12345"}
    assert outcomes.reference_for(row, "zendesk") is None


def test_a_persons_verdict_is_never_overwritten():
    """An automated guess is the weakest signal in the store, not the loudest."""
    _served("Ticket #12345: desk shows E07", DRAFT, "req-1")
    capture.record_feedback("req-1", "edited", final_text="what the human really sent")
    capture.flush()

    result = outcomes.join("zendesk", {"12345": {"reply": DRAFT, "resolved": True}})
    assert result.skipped_human == 1 and result.written == 0
    kept = {e.prompt: e for e in capture.build_examples()}
    assert kept["Ticket #12345: desk shows E07"].answer == "what the human really sent"


def test_the_join_writes_the_verdicts_a_person_would_have():
    _served("Ticket #100: desk shows E07", DRAFT, "req-a")
    _served("Ticket #200: where is my order", "We ship in 3 days.", "req-b")
    _served("Ticket #300: refund please", "No.", "req-c")

    result = outcomes.join("zendesk", {
        "100": {"reply": DRAFT, "resolved": True},                       # sent as written
        "200": {"reply": "It left the warehouse yesterday, tracking below.", "resolved": True},
        "300": {"reply": "", "resolved": False},                         # never sent, reopened
    })
    assert (result.accepted, result.edited, result.rejected) == (1, 1, 1)
    assert result.matched == 3

    examples = {e.prompt: e for e in capture.build_examples()}
    assert examples["Ticket #200: where is my order"].source == "edited"
    assert examples["Ticket #200: where is my order"].weight == 1.0
    assert examples["Ticket #100: desk shows E07"].source == "accepted"
    assert "Ticket #300: refund please" not in examples          # rejected is never imitated


def test_a_dry_run_records_nothing():
    _served("Ticket #100: desk shows E07", DRAFT, "req-a")
    result = outcomes.join("zendesk", {"100": {"reply": "different text entirely", "resolved": True}},
                           dry_run=True)
    assert result.edited == 1
    assert capture.build_examples() == []          # counted, not written


def test_we_only_fetch_the_records_we_need():
    _served("Ticket #100: desk shows E07", DRAFT, "req-a")
    _served("no reference in this one at all", DRAFT, "req-b")
    _served("Ticket #100: a second question", DRAFT, "req-c")
    assert outcomes.refs_needed("zendesk") == ["100", "100"]     # and dict.fromkeys dedupes


def test_a_record_the_vendor_would_not_give_us_is_counted_not_guessed():
    _served("Ticket #100: desk shows E07", DRAFT, "req-a")
    result = outcomes.join("zendesk", {})            # the fetch returned nothing for it
    assert result.unreadable == 1 and result.written == 0


def test_the_fetcher_reads_status_and_the_public_reply(monkeypatch):
    monkeypatch.setenv("ZENDESK_API_TOKEN", "t0ken")
    sent = "Unplug the desk for sixty seconds, then hold the down button until it recalibrates."

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/comments.json"):
            return httpx.Response(200, json={"comments": [
                {"public": True, "body": "my desk is stuck"},
                {"public": False, "body": "internal note"},
                {"public": True, "body": sent}]})
        return httpx.Response(200, json={"ticket": {"id": 100, "status": "solved"}})

    records = outcomes.zendesk_records("acme", "me@acme.com", ["100", "100"],
                                       transport=httpx.MockTransport(handle))
    assert records == {"100": {"reply": sent, "resolved": True}}

    def reopened(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/comments.json"):
            return httpx.Response(200, json={"comments": [{"public": True, "body": "x"},
                                                          {"public": True, "body": sent}]})
        return httpx.Response(200, json={"ticket": {"id": 100, "status": "open"}})

    reopened_records = outcomes.zendesk_records("acme", "me@acme.com", ["100"],
                                                transport=httpx.MockTransport(reopened))
    assert reopened_records["100"]["resolved"] is False      # a reopened ticket is a negative
