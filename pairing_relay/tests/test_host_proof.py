"""Proving possession of the key a relay host id is derived from.

The attack this stops is one desktop claiming another's id and being handed its
phones. The attack it must not *become* is a signing oracle: a relay that can
make a desktop sign arbitrary bytes.
"""
from __future__ import annotations

import time

import pytest

from pairing_relay.host_proof import (
    HostProofRefused,
    answer_challenge,
    derive_relay_host_id,
    issue_challenge,
    verify_proof,
)
from pairing_relay.tests.support import RELAY_ORIGIN, FakeHost

TTL_MS = 10_000


def _challenge(host: FakeHost, now_ms: int, *, origin: str = RELAY_ORIGIN):
    return issue_challenge(
        relay_origin=origin,
        relay_host_id=host.relay_host_id,
        host_public_key=host.public_key,
        now_ms=now_ms,
        ttl_ms=TTL_MS,
    )


def test_a_host_id_is_owned_by_a_key_rather_than_handed_out():
    host = FakeHost()
    assert derive_relay_host_id(host.public_key) == host.relay_host_id
    assert len(host.relay_host_id) == 16
    # A different key is a different id, so there is nothing to squat.
    assert derive_relay_host_id(FakeHost().public_key) != host.relay_host_id


def test_the_real_desktop_answers_and_the_relay_accepts():
    host, now = FakeHost(), int(time.time() * 1000)
    wire, pending = _challenge(host, now)
    proof = answer_challenge(
        wire, host_private_key=host.private_key, context=host.context(), now_ms=now
    )
    assert verify_proof(pending, proof, now_ms=now)


def test_someone_without_the_private_key_cannot_even_read_the_challenge():
    host, impostor, now = FakeHost(), FakeHost(), int(time.time() * 1000)
    wire, _ = _challenge(host, now)
    with pytest.raises(HostProofRefused, match="challenge-undecryptable"):
        answer_challenge(
            wire,
            host_private_key=impostor.private_key,
            context=host.context(),
            now_ms=now,
        )


def test_a_desktop_refuses_to_sign_for_a_relay_it_did_not_dial():
    # Without this the proof is a signing oracle: a relay the desktop happens to
    # reach could collect an answer valid at the relay it is impersonating.
    host, now = FakeHost(), int(time.time() * 1000)
    wire, _ = _challenge(host, now, origin="ws://someone-elses-relay:8787")
    with pytest.raises(HostProofRefused, match="relayOrigin"):
        answer_challenge(
            wire, host_private_key=host.private_key, context=host.context(), now_ms=now
        )


def test_a_desktop_refuses_a_transcript_naming_an_id_that_is_not_its_own():
    host, other, now = FakeHost(), FakeHost(), int(time.time() * 1000)
    wire, _ = issue_challenge(
        relay_origin=RELAY_ORIGIN,
        relay_host_id=other.relay_host_id,
        host_public_key=host.public_key,
        now_ms=now,
        ttl_ms=TTL_MS,
    )
    with pytest.raises(HostProofRefused, match="relayHostId"):
        answer_challenge(
            wire, host_private_key=host.private_key, context=host.context(), now_ms=now
        )


def test_an_answer_from_one_challenge_does_not_pass_another():
    host, now = FakeHost(), int(time.time() * 1000)
    first_wire, _ = _challenge(host, now)
    _, second_pending = _challenge(host, now)
    proof = answer_challenge(
        first_wire, host_private_key=host.private_key, context=host.context(), now_ms=now
    )
    assert not verify_proof(second_pending, proof, now_ms=now)


def test_a_proof_that_arrives_after_the_window_is_refused():
    host, now = FakeHost(), int(time.time() * 1000)
    wire, pending = _challenge(host, now)
    proof = answer_challenge(
        wire, host_private_key=host.private_key, context=host.context(), now_ms=now
    )
    assert not verify_proof(pending, proof, now_ms=now + TTL_MS + 1)


def test_a_clock_a_little_out_of_step_still_pairs():
    # Measured in the field as the thing that breaks first: a desktop whose NTP
    # is a second ahead would otherwise fail every proof it ever makes.
    host, now = FakeHost(), int(time.time() * 1000)
    wire, pending = _challenge(host, now)
    proof = answer_challenge(
        wire,
        host_private_key=host.private_key,
        context=host.context(),
        now_ms=now - 20_000,
    )
    assert verify_proof(pending, proof, now_ms=now)


def test_garbage_in_place_of_a_proof_is_refused_rather_than_raising():
    host, now = FakeHost(), int(time.time() * 1000)
    _, pending = _challenge(host, now)
    assert not verify_proof(pending, "not base64 at all !!", now_ms=now)
    assert not verify_proof(pending, "", now_ms=now)
