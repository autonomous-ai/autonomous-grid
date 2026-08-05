"""How many tasks a provider can actually take (ADR 0032 issue 09).

Its own module for the same reason `test_task_lease.py` and `test_task_tree.py` are theirs: the
subject is one fact the provider asserts about itself, and this one is *the state of its own Claude
subscription* — the real ceiling on task work, since every agent child a provider spawns draws on it.

The rule the whole module exists to pin runs in ONE direction: **a signal that cannot be read keeps
the provider serving.** Stopping is the expensive answer — a provider that withdraws is capacity the
grid has lost until someone notices — so it is only ever reached from a reading that says, in terms
this build recognises, both *that* the window is spent and *when* it comes back.
"""

import time

import pytest


def _blocked(resets_in=3600.0, status="rejected", limit_type="five_hour"):
    """A `rate_limit_info` payload shaped exactly as the vendor sends one."""
    return {"status": status, "rateLimitType": limit_type,
            "resetsAt": int(time.time() + resets_in),
            "overageStatus": "rejected", "isUsingOverage": False}


def test_a_spent_subscription_stops_the_provider_claiming_until_the_window_resets():
    """The tracer bullet: the provider reads its own pressure and withdraws for exactly as long as
    the vendor said, rather than for a duration anyone tuned."""
    from remote import task_capacity

    capacity = task_capacity.TaskCapacity()
    assert capacity.pause_seconds() == 0.0        # nothing observed yet: keep serving

    capacity.observe(_blocked(resets_in=1800.0))

    pause = capacity.pause_seconds()
    assert 1700.0 < pause <= 1800.0               # the vendor's window, not a constant of ours


def test_a_healthy_reading_lets_the_provider_claim_again_at_once():
    """The other half of "resumes when the window resets": the provider does not have to wait out a
    stale block once the subscription itself says it is serving."""
    from remote import task_capacity

    capacity = task_capacity.TaskCapacity()
    capacity.observe(_blocked())
    assert capacity.pause_seconds() > 0.0

    capacity.observe(_blocked(status="allowed"))

    assert capacity.pause_seconds() == 0.0


def test_a_reset_further_out_than_any_real_window_is_not_believed():
    """A `resetsAt` in MILLISECONDS is what a units mistake looks like, and believing one would
    retire this provider's task serving for fifty thousand years — silently, which is the one
    outcome this feature may never produce. Past the bound it is not a window this build
    recognises, so it falls back to serving."""
    from remote import task_capacity

    capacity = task_capacity.TaskCapacity()

    capacity.observe({"status": "rejected", "resetsAt": int(time.time() * 1000)})

    assert capacity.pause_seconds() == 0.0


def test_withdrawing_is_announced_once_per_window_not_once_per_record(capsys):
    """`rate_limit_event` rides along with the agent's output, so a spent subscription reports itself
    over and over. The operator needs to be told that this provider has stopped taking work — once,
    with when it comes back — not once per line for the rest of the run."""
    from remote import task_capacity

    capacity = task_capacity.TaskCapacity()

    for _ in range(5):
        capacity.observe(_blocked(resets_in=600.0))

    err = capsys.readouterr().err
    assert err.count("no longer claiming tasks") == 1
    assert "five_hour" in err                      # which window, so the wait is explicable


def test_a_new_window_is_announced_again(capsys):
    """The latch is per block, not per process: a provider that recovered and was later spent again
    must say so, or its second withdrawal is invisible."""
    from remote import task_capacity

    capacity = task_capacity.TaskCapacity()
    capacity.observe(_blocked())
    capacity.observe(_blocked(status="allowed"))
    capsys.readouterr()

    capacity.observe(_blocked())

    assert "no longer claiming tasks" in capsys.readouterr().err


# Payloads that carry no verdict this build can act on. Each is a real shape, not a fuzz artefact:
# a `rate_limit_info` key that is missing entirely, a vendor that changes a type, a units mistake,
# and a stamp that has already passed by the time the line is read.
_UNREADABLE = [
    None, [], "rejected", 123, True, {},
    {"status": None},
    {"status": ""},
    {"status": 1, "resetsAt": 99999999999},
    {"status": "rejected"},                                       # spent, but no window named
    {"status": "rejected", "resetsAt": None},
    {"status": "rejected", "resetsAt": "in an hour"},
    {"status": "rejected", "resetsAt": True},                      # a bool is an int in Python
    {"status": "rejected", "resetsAt": 1},                         # 1970 — already behind us
    {"status": "rejected", "rateLimitType": {"nested": "junk"}},
]


@pytest.mark.parametrize("info", _UNREADABLE)
def test_a_payload_that_carries_no_usable_verdict_keeps_the_provider_serving(info):
    """The whole contract, in one direction: unreadable is not evidence of exhaustion. A provider
    that stopped taking work because the vendor changed a field would be capacity the grid has lost
    with nothing anywhere saying why."""
    from remote import task_capacity

    capacity = task_capacity.TaskCapacity()

    capacity.observe(info)                                         # must not raise

    assert capacity.pause_seconds() == 0.0


@pytest.mark.parametrize("info", _UNREADABLE)
def test_an_unreadable_payload_does_not_lift_a_block_either(info):
    """The fail-open rule is about not INVENTING a block, never about discarding one we were given.
    A garbled line arriving mid-run must not hand this provider back work its subscription has
    already refused."""
    from remote import task_capacity

    capacity = task_capacity.TaskCapacity()
    capacity.observe(_blocked(resets_in=900.0))

    capacity.observe(info)

    assert capacity.pause_seconds() > 0.0


def test_a_wall_clock_step_cannot_move_a_block_already_taken(monkeypatch):
    """`resetsAt` is the vendor's wall clock and is converted ONCE, at the moment the block is taken.
    Re-deriving the wait from `time.time()` on every read would let an NTP correction — or a laptop
    waking from sleep — either hand the provider back work its subscription still refuses, or hold it
    out of the fleet for a day."""
    from types import SimpleNamespace

    from remote import task_capacity

    capacity = task_capacity.TaskCapacity()
    capacity.observe(_blocked(resets_in=600.0))
    before = capacity.pause_seconds()

    stepped = time.time() - 86400.0
    monkeypatch.setattr(task_capacity, "time",
                        SimpleNamespace(time=lambda: stepped, monotonic=time.monotonic))

    assert abs(capacity.pause_seconds() - before) < 1.0


def test_every_task_worker_reads_the_same_subscription():
    """One provider process, one Claude subscription. The gate is shared so a limit one worker
    discovered stops the others too — a per-worker view would let N workers each learn the same
    refusal the expensive way."""
    from remote import task_capacity

    assert task_capacity.shared() is task_capacity.shared()


def test_a_status_this_build_has_never_seen_keeps_the_provider_serving():
    """The one shape that is READABLE and still carries no verdict: a well-formed payload naming a
    status no version of this build has special-cased, with a perfectly ordinary window beside it.

    An allowlist would call it spent — everything that is not `allowed` must be — and park the
    provider for the whole window the vendor named. That is a confident, wrong diagnosis of what is
    actually a client parsing gap, and nothing about it self-corrects: the same string arrives every
    window, forever. `allowed_overage` is not a hypothetical shape either — the payload already
    carries `isUsingOverage`.

    Serving instead costs the opposite way, and costs it in the direction that recovers: if the new
    status did mean "spent", the next task fails fast, says so in its own error, and the fleet keeps
    working.
    """
    from remote import task_capacity

    capacity = task_capacity.TaskCapacity()

    capacity.observe(_blocked(status="allowed_overage"))

    assert capacity.pause_seconds() == 0.0


def test_a_status_this_build_has_never_seen_does_not_lift_a_block_either():
    """Unrecognised is "no verdict", not "you are fine" — the same rule every other unreadable
    payload follows."""
    from remote import task_capacity

    capacity = task_capacity.TaskCapacity()
    capacity.observe(_blocked(resets_in=900.0))

    capacity.observe(_blocked(status="something_new"))

    assert capacity.pause_seconds() > 0.0


def test_an_unrecognised_status_is_named_once_so_it_can_be_acted_on(capsys):
    """Serving through a status we do not understand is the safe answer AND a blind spot: if it did
    mean "spent", every task this provider claims now fails. So it is reported — with the string
    itself, which is the only thing an operator can act on — once per distinct status rather than
    once per record, since the signal repeats with every turn of the agent's output."""
    from remote import task_capacity

    capacity = task_capacity.TaskCapacity()

    for _ in range(4):
        capacity.observe(_blocked(status="throttled_soft"))
    capacity.observe(_blocked(status="another_new_one"))

    err = capsys.readouterr().err
    assert err.count("throttled_soft") == 1
    assert "another_new_one" in err
    assert "out of headroom" not in err, "an unrecognised status must not be diagnosed as spent"


def test_a_recognised_refusal_is_still_a_refusal():
    """The negative half of the same rule: widening what counts as unreadable must not stop the one
    status that genuinely means spent from stopping this provider."""
    from remote import task_capacity

    capacity = task_capacity.TaskCapacity()

    capacity.observe(_blocked(status="rejected", resets_in=1200.0))

    assert capacity.pause_seconds() > 0.0
