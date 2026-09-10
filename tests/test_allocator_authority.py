from __future__ import annotations

import json

import pytest

from shared.allocator.authority import (
    AuthorityUnavailable,
    ControllerAuthorityLease,
)


class Clock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def test_authority_is_single_writer_and_takeover_increments_term(tmp_path):
    path = tmp_path / "controller.json"
    clock = Clock(100)
    first = ControllerAuthorityLease(
        path, ttl_seconds=30, leader_id="leader-a", clock=clock
    )
    rival = ControllerAuthorityLease(
        path, ttl_seconds=30, leader_id="leader-b", clock=clock
    )

    assert first.ensure().term == 1
    with pytest.raises(AuthorityUnavailable, match="another live controller"):
        rival.ensure()

    clock.value = 130
    takeover = rival.ensure()
    assert takeover.term == 2
    with pytest.raises(AuthorityUnavailable, match="another live controller"):
        first.ensure()


def test_authority_renews_same_term_and_release_advances_successor(tmp_path):
    path = tmp_path / "controller.json"
    clock = Clock(10)
    first = ControllerAuthorityLease(
        path, ttl_seconds=30, leader_id="leader-a", clock=clock
    )
    initial = first.ensure()
    clock.value = 31
    renewed = first.ensure()
    assert renewed.term == initial.term
    assert renewed.expires_at == 61

    first.release()
    successor = ControllerAuthorityLease(
        path, ttl_seconds=30, leader_id="leader-b", clock=clock
    ).ensure()
    assert successor.term == initial.term + 1


def test_corrupt_authority_state_fails_closed(tmp_path):
    path = tmp_path / "controller.json"
    authority_path = path.with_suffix(path.suffix + ".authority")
    authority_path.write_text(json.dumps({"schema_version": 1, "term": -1}))
    lease = ControllerAuthorityLease(path, leader_id="leader-a")
    with pytest.raises(ValueError, match="invalid persisted"):
        lease.ensure()

    authority_path.write_text("{not-json")
    with pytest.raises(ValueError, match="invalid allocator authority file"):
        lease.ensure()
