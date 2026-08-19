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


# ---------------------------------------------------------------------------
# ADR 0033 D-l / issue 19b — the withdrawal becomes visible to the team.
#
# ADR 0032 published none of this on purpose, and CLAUDE.md recorded the reason: a task is claimed
# from a durable queue at poll time, so a provider that does not ask is simply not given one and the
# relay needs to know nothing. **That argument was written for one member per project.** With six
# other people on the same subscription, one member's rate-limit reading withdraws the provider from
# the whole team for the vendor's window, and the explanation reaches only the client whose task was
# running — everybody else sees a queued task and, past the deadline, a silent reaping.
#
# So the reading is published, and ONLY published: nothing consults it to route, to claim, or to
# hand out work. It is for people to read.
# ---------------------------------------------------------------------------


def test_a_serving_provider_publishes_no_pause_at_all():
    """Absent is the wire's "nothing withheld", exactly as `unhealthy_models` is (ADR 0019). A
    healthy provider's heartbeat stays byte-identical to a pre-19b build, which is what makes the
    rollout free in both directions."""
    from remote import task_capacity

    assert task_capacity.TaskCapacity().paused_until() is None


def test_a_withdrawn_provider_publishes_when_it_comes_back():
    """A TIMESTAMP, not a boolean. "Paused" with no "until" tells a team to keep watching; a reset
    time tells them whether to wait or to add a provider, which are the only two things they can
    do."""
    from remote import task_capacity

    capacity = task_capacity.TaskCapacity()
    capacity.observe(_blocked(resets_in=1800.0))

    until = capacity.paused_until()

    assert until is not None
    # The vendor's own window, in wall clock, within a second of where `pause_seconds` says it is.
    assert abs(until - (time.time() + capacity.pause_seconds())) < 1.0
    assert 1700.0 < until - time.time() <= 1801.0


def test_the_published_time_is_derived_from_the_block_and_not_from_the_vendors_stamp():
    """One clock, not two.

    `resetsAt` arrives on the vendor's wall clock and is converted to a MONOTONIC deadline once, at
    the moment the block is taken, so that a later clock step can neither extend nor shorten it —
    `test_a_wall_clock_step_cannot_move_a_block_already_taken` pins that. Publishing the raw stamp
    instead would reintroduce the thing that conversion removed, and would also republish a value
    the two-week sanity ceiling had already refused. So this is `now + pause_seconds()`, and a clock
    step moves the published time with the clock rather than un-pausing the provider.
    """
    from remote import task_capacity

    capacity = task_capacity.TaskCapacity()
    capacity.observe(_blocked(resets_in=1800.0))
    before = capacity.paused_until()

    real = time.time
    try:
        time.time = lambda: real() + 600.0        # the wall clock jumps ten minutes forward
        after = capacity.paused_until()
    finally:
        time.time = real

    assert after - before == pytest.approx(600.0, abs=1.0)
    assert capacity.pause_seconds() > 1700.0, "the block itself must not have moved"


def test_a_reading_with_no_believable_window_publishes_nothing():
    """The fail-open rule reaches the wire too. A spent reading whose reset this build will not
    believe takes no block — so there is nothing to publish, and publishing "paused, until unknown"
    would tell a team to stop waiting on a provider that is still claiming."""
    from remote import task_capacity

    capacity = task_capacity.TaskCapacity()
    capacity.observe(_blocked(resets_in=40 * 24 * 3600.0))    # past the two-week ceiling

    assert capacity.pause_seconds() == 0.0
    assert capacity.paused_until() is None


def test_a_window_that_has_run_out_publishes_nothing_again():
    """Recovery needs no fresh reading. Once the block's own deadline passes, `pause_seconds` is
    0.0 and the key must disappear from the heartbeat — otherwise a provider that came back an hour
    ago still reads as withdrawn to every member of every project."""
    from remote import task_capacity

    capacity = task_capacity.TaskCapacity()
    capacity.observe(_blocked(resets_in=1800.0))
    assert capacity.paused_until() is not None

    real = time.monotonic
    try:
        time.monotonic = lambda: real() + 1801.0
        assert capacity.pause_seconds() == 0.0
        assert capacity.paused_until() is None
    finally:
        time.monotonic = real


def test_the_only_reading_worth_no_words_is_plain_allowed():
    """`worth_reporting` decides NARRATION, and it is deliberately not `serving`'s complement.

    `allowed_warning` is serving — the provider keeps claiming — and is also exactly when the next
    task's wait needs explaining, so it must still reach the follower. An unrecognised status is news
    for the same reason `_SERVING_STATUSES` refuses to guess at one: silence about a string nobody
    has seen is the failure mode with no recovery.
    """
    from remote import task_capacity

    assert task_capacity.worth_reporting("allowed") is False
    assert task_capacity.worth_reporting("allowed_warning") is True
    assert task_capacity.worth_reporting("rejected") is True
    assert task_capacity.worth_reporting("some_status_from_2027") is True
    # Missing / unreadable, which is what a payload built from a subprocess's stdout hands over.
    assert task_capacity.worth_reporting(None) is True
    assert task_capacity.worth_reporting(123) is True


def test_a_healthy_reading_reaches_the_gate_but_not_the_follower():
    """The filter is on narration ONLY, and the ordering is the whole property.

    Dropping the event before `_tell_the_gate` would blind the capacity gate to every healthy
    reading, and the gate is what decides whether this provider keeps claiming work — so it must see
    every one. What the follower loses is a line reading "the provider's five_hour window is allowed"
    on stderr, once per task, on a provider with nothing wrong with it.
    """
    import json

    from remote import task_stream

    seen = []
    translator = task_stream.StreamTranslator(on_rate_limit=seen.append)

    healthy = {"type": "rate_limit_event",
               "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour",
                                   "resetsAt": 1785832800}}
    events = translator.feed(json.dumps(healthy))

    assert events == [], "a healthy window is not news for the person who submitted the task"
    assert seen == [healthy["rate_limit_info"]], "the gate must still see every reading"


def test_a_spent_reading_still_reaches_the_follower():
    """The positive control. A filter with no case that survives it is just a deletion.

    This is the reading the event exists for: the user whose next task sits queued is owed the
    reason, and the provider's own log is not somewhere they can read.
    """
    import json

    from remote import task_stream

    seen = []
    translator = task_stream.StreamTranslator(on_rate_limit=seen.append)

    spent = {"type": "rate_limit_event",
             "rate_limit_info": {"status": "rejected", "rateLimitType": "weekly",
                                 "resetsAt": 1785832800}}
    events = translator.feed(json.dumps(spent))

    assert [kind for kind, _ in events] == ["task.rate_limit"]
    assert events[0][1] == {"status": "rejected", "limit_type": "weekly",
                            "resets_at": 1785832800}
    assert seen == [spent["rate_limit_info"]]
