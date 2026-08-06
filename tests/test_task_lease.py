"""What keeps a claimed task's lease alive, and what lets it lapse (ADR 0032 issue 07, D-c).

Its own module rather than more of `test_task_agent.py`, which is already the largest suite in this
repo — and the subject here is different: that one is about the agent child, this one is about the
single fact the provider keeps asserting about it.

The rule the whole module exists to pin: **renewal proves the CHILD PROCESS, not the network.**
Piggybacking on anything that merely shows the provider is reachable means a task whose agent died
keeps its lease forever, and its project stays locked by the one-active-task rule — exactly the
failure this issue exists to prevent. And the mirror image matters just as much: **silence is not
death.** A task legitimately produces nothing for ten minutes while a build or a test suite runs,
and inferring a hang from quiet spends a real attempt to learn nothing.
"""

import time

import pytest


class _FakeProc:
    """A child whose liveness the test controls.

    `pid` is deliberately a REAL, live pid — this process's own. Any implementation that reads a pid
    and asks the operating system about it therefore sees "alive" and passes the wrong tests, which
    is precisely the hazard ADRs 0020 and 0026 removed from the run-record seams.
    """

    def __init__(self, *, alive=True):
        import os
        self.pid = os.getpid()
        self._returncode = None if alive else 0
        self.killed = False
        self.terminated = False

    def poll(self):
        return self._returncode

    def exit(self, returncode=0):
        self._returncode = returncode

    def kill(self):
        self.killed = True
        self._returncode = -9

    def terminate(self):
        self.terminated = True


class _FakeState:
    """The serve state the renewer borrows: a token, a refresh, and where to send it."""

    def __init__(self, *, refreshes=True):
        self.signaling_url = "http://relay.test"
        self._token = "tok-1"
        self._refreshes = refreshes
        self.refresh_calls = []

    def token(self):
        return self._token

    def refresh(self, stale_token=None):
        self.refresh_calls.append(stale_token)
        if not self._refreshes:
            return False
        self._token = "tok-2"
        return True


def _wait_for(predicate, timeout=3.0):
    """Wait for a background thread to get somewhere, without pinning a cadence to wall clock."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


@pytest.fixture
def renewals(monkeypatch):
    """Every renewal the module actually puts on the wire, recorded as `(token, task_id)`.

    Patched at the relay boundary rather than inside the renewer, so the test exercises the real
    call path — including the token the renewer chose to send, which is what the 401 case turns on.
    """
    from remote import task_lease

    sent = []

    def _fake(signaling_url, token, task_id):
        sent.append((token, task_id))

    monkeypatch.setattr(task_lease.relay, "renew_task_lease", _fake)
    return sent


def test_it_renews_while_the_child_is_alive(renewals):
    """The tracer bullet. A task that is genuinely being served keeps its lease."""
    from remote import task_lease

    renewer = task_lease.LeaseRenewer(_FakeState(), "t-1", interval=0.01)
    renewer.attach(_FakeProc(alive=True))
    renewer.start()
    try:
        assert _wait_for(lambda: len(renewals) >= 3)
    finally:
        renewer.close()

    assert {task_id for _token, task_id in renewals} == {"t-1"}


def test_it_stops_the_moment_the_child_exits(renewals):
    """The criterion: a task whose agent process dies while the provider stays online loses its
    lease, so the relay can hand the work to someone else.

    Latched permanently rather than merely paused — a supervisor wedged AFTER its child died must
    not be able to resume vouching for it.
    """
    from remote import task_lease

    proc = _FakeProc(alive=True)
    renewer = task_lease.LeaseRenewer(_FakeState(), "t-1", interval=0.01)
    renewer.attach(proc)
    renewer.start()
    try:
        assert _wait_for(lambda: len(renewals) >= 2)
        proc.exit(0)
        settled = len(renewals)
        time.sleep(0.15)  # many renewal intervals

        assert len(renewals) <= settled + 1, "renewal continued past the child's exit"
    finally:
        renewer.close()


def test_a_child_that_is_gone_is_never_vouched_for_by_its_pid(renewals):
    """The criterion phrased so a pid-probing implementation FAILS it.

    This fake's `pid` is this very process's — genuinely alive, and it will stay alive for the whole
    test. An implementation that read the pid back and asked the operating system about it would see
    "running" and keep renewing. Only the held handle's `poll()` gives the right answer.
    """
    from remote import task_lease

    proc = _FakeProc(alive=False)  # exited, but `proc.pid` is a live process
    renewer = task_lease.LeaseRenewer(_FakeState(), "t-1", interval=0.01)
    renewer.attach(proc)
    renewer.start()
    try:
        time.sleep(0.15)
    finally:
        renewer.close()

    assert renewals == [], "a dead child was vouched for — something believed a pid, not the handle"


def test_nothing_on_the_renewal_path_touches_the_childs_pid(renewals):
    """The same rule, proved by absence rather than by outcome.

    This handle has no `pid` attribute at all, so ANY code path that reached for one would raise
    `AttributeError` instead of quietly working. A source scan could not say this — the module's own
    docstring contains the word — and the outcome test above passes for an implementation that reads
    the pid and happens to also check `poll()`.
    """
    from remote import task_lease

    class _HandleWithNoPid:
        def poll(self):
            return None

        def kill(self):
            raise AssertionError("nothing should kill a live child on the happy path")

    renewer = task_lease.LeaseRenewer(_FakeState(), "t-1", interval=0.01)
    renewer.attach(_HandleWithNoPid())
    renewer.start()
    try:
        assert _wait_for(lambda: len(renewals) >= 3)
    finally:
        renewer.close()


def test_a_task_that_says_nothing_for_far_longer_than_the_lease_keeps_its_lease(renewals):
    """Silence is not death (ADR 0032 D-c).

    Nothing is ever fed to this renewer — no output, no progress, no activity of any kind — across
    many multiples of its own interval, which is what a real ten-minute build or test suite looks
    like from here. An implementation with any idle timer in it stops; this one must not.
    """
    from remote import task_lease

    renewer = task_lease.LeaseRenewer(_FakeState(), "t-1", interval=0.01)
    renewer.attach(_FakeProc(alive=True))
    renewer.start()
    try:
        assert _wait_for(lambda: len(renewals) >= 20), (
            "renewal stopped while the child was alive — something inferred a hang from quiet")
    finally:
        renewer.close()


def test_it_renews_before_the_agent_has_even_been_spawned(renewals):
    """The pre-spawn window is real work, and it is longer than the lease.

    A claimed task fetches its input over the network first, and that fetch's own ceiling is 300s
    against a 120s lease TTL. A renewer that waited for a child would guarantee a reclaim mid-
    checkout on every slow clone — and the next provider would face the same slow clone.
    """
    from remote import task_lease

    renewer = task_lease.LeaseRenewer(_FakeState(), "t-1", interval=0.01)
    renewer.start()  # nothing attached: `run_task` has not reached the spawn yet
    try:
        assert _wait_for(lambda: len(renewals) >= 3)
    finally:
        renewer.close()


def test_a_403_latches_the_renewer_off_and_terminates_the_agent(monkeypatch):
    """403 means another provider holds this task's lease now — and only the real endpoint can say
    it. This child's work can no longer be delivered (its push and its report are both fenced), so
    it is stopped rather than left spending the operator's agent subscription on a refused result.
    """
    from remote import task_lease

    calls = []

    def _refused(signaling_url, token, task_id):
        calls.append(task_id)
        raise task_lease.relay.RelayError("someone else has it", status=403)

    monkeypatch.setattr(task_lease.relay, "renew_task_lease", _refused)

    proc = _FakeProc(alive=True)
    renewer = task_lease.LeaseRenewer(_FakeState(), "t-1", interval=0.01)
    renewer.attach(proc)
    renewer.start()
    try:
        assert _wait_for(lambda: proc.killed)
    finally:
        renewer.close()

    assert len(calls) == 1, "it kept renewing after an answer that no retry can change"
    assert renewer.lost is True


def test_a_404_stops_renewing_but_never_kills_the_agent(monkeypatch):
    """A 404 on THIS endpoint is ambiguous in a way 403 is not, and the ambiguity is a fleet-wide
    hazard rather than a corner case.

    `POST /tasks/{id}/lease` is new in this issue, while `/tasks/claim`, `/result` and `/events` all
    predate it. A relay that has not redeployed yet therefore answers a bare framework 404 for the
    unmatched route — indistinguishable from the relay's own "this task already ended". A provider
    that reads it as a verdict and kills its agent would destroy every task longer than one renewal
    interval, on every provider that updated ahead of its relay, deterministically, until the relay
    caught up. That is the exact inversion of this repo's degrade rule: an absent feature must fail
    back to the OLD behaviour, never to a new failure.

    So the fallback is the one `start()` already uses when a renewer cannot be built at all: stop
    renewing and let the relay's own lease expiry and deadline decide. Losing renewal costs a retry;
    killing a healthy agent costs the work.
    """
    from remote import task_lease

    calls = []

    def _no_such_route(signaling_url, token, task_id):
        calls.append(task_id)
        raise task_lease.relay.RelayError("Not Found", status=404)

    monkeypatch.setattr(task_lease.relay, "renew_task_lease", _no_such_route)

    proc = _FakeProc(alive=True)
    renewer = task_lease.LeaseRenewer(_FakeState(), "t-1", interval=0.01)
    renewer.attach(proc)
    renewer.start()
    try:
        assert _wait_for(lambda: len(calls) >= 1)
        time.sleep(0.15)  # many renewal intervals
    finally:
        renewer.close()

    assert len(calls) == 1, "it kept asking a relay that has already said no"
    assert not proc.killed, "a 404 killed a healthy agent — an old relay now breaks every task"
    assert proc.poll() is None, "the agent was stopped some other way"


def test_a_verdict_before_the_agent_is_spawned_still_stops_the_renewer(monkeypatch):
    """The verdict latch, on the one path where killing the child cannot do the stopping for it.

    With a child attached, `_give_up` terminates it and the next tick sees a dead handle — so an
    implementation that forgot to latch would still stop, by accident. In the pre-spawn window there
    is no child to kill, and a renewer that kept going would hammer the relay with a refusal it has
    already been given, for the rest of the task.
    """
    from remote import task_lease

    calls = []

    def _refused(signaling_url, token, task_id):
        calls.append(task_id)
        raise task_lease.relay.RelayError("someone else has it", status=403)

    monkeypatch.setattr(task_lease.relay, "renew_task_lease", _refused)

    renewer = task_lease.LeaseRenewer(_FakeState(), "t-1", interval=0.01)
    renewer.start()  # nothing attached: still checking out the task's input
    try:
        assert _wait_for(lambda: len(calls) >= 1)
        time.sleep(0.15)  # many intervals
    finally:
        renewer.close()

    assert len(calls) == 1
    assert renewer.lost is True


def test_a_relay_that_is_merely_unreachable_does_not_cost_the_lease(monkeypatch):
    """Nobody has decided anything: a 5xx, a proxy's 408/429, or a bare transport failure.

    Giving up here would hand a perfectly healthy task to another provider over one bad round trip —
    and would kill an agent that was doing exactly what it should.
    """
    from remote import task_lease

    attempts = []

    def _flaky(signaling_url, token, task_id):
        attempts.append(task_id)
        if len(attempts) <= 3:
            raise task_lease.relay.RelayError("relay is having a moment", status=503)

    monkeypatch.setattr(task_lease.relay, "renew_task_lease", _flaky)

    proc = _FakeProc(alive=True)
    renewer = task_lease.LeaseRenewer(_FakeState(), "t-1", interval=0.01)
    renewer.attach(proc)
    renewer.start()
    try:
        assert _wait_for(lambda: len(attempts) >= 5)
    finally:
        renewer.close()

    assert not proc.killed
    assert renewer.lost is False


def test_a_401_refreshes_the_token_once_and_renews_with_the_new_one(monkeypatch):
    """The same single-retry rule `claim_once` and `report_once` use — the credential rotates while
    a task is running, and an hour-long task will outlive its access token."""
    from remote import task_lease

    seen = []

    def _needs_fresh_token(signaling_url, token, task_id):
        seen.append(token)
        if token == "tok-1":
            raise task_lease.relay.RelayUnauthorized("stale")

    monkeypatch.setattr(task_lease.relay, "renew_task_lease", _needs_fresh_token)

    state = _FakeState()
    renewer = task_lease.LeaseRenewer(state, "t-1", interval=0.01)
    renewer.attach(_FakeProc(alive=True))
    renewer.start()
    try:
        assert _wait_for(lambda: seen.count("tok-2") >= 2)
    finally:
        renewer.close()

    assert state.refresh_calls == ["tok-1"], "it refreshed more than once for one stale token"


def test_a_401_that_no_refresh_can_fix_stops_renewing(monkeypatch):
    """Reaching a second 401 means no credential is available, so no later renewal can land — the
    task's lease lapses and another provider retries it, which is the recoverable outcome."""
    from remote import task_lease

    attempts = []

    def _always_401(signaling_url, token, task_id):
        attempts.append(token)
        raise task_lease.relay.RelayUnauthorized("no")

    monkeypatch.setattr(task_lease.relay, "renew_task_lease", _always_401)

    renewer = task_lease.LeaseRenewer(_FakeState(refreshes=False), "t-1", interval=0.01)
    renewer.attach(_FakeProc(alive=True))
    renewer.start()
    try:
        assert _wait_for(lambda: len(attempts) >= 1)
        time.sleep(0.15)
    finally:
        renewer.close()

    assert len(attempts) == 1


def test_a_renewer_that_cannot_even_start_does_not_fail_the_task(monkeypatch, capsys):
    """A task that runs without renewal is reclaimed and retried. A task that never runs because
    its renewer could not start a thread is simply lost — a far worse trade."""
    from remote import task_lease

    def _no_threads(*_args, **_kwargs):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(task_lease.threading, "Thread", _no_threads)

    renewer = task_lease.LeaseRenewer(_FakeState(), "t-1", interval=0.01)
    renewer.start()   # must not raise
    renewer.close()   # nor must this, with no thread to join

    assert "could not start lease renewal" in capsys.readouterr().err


def test_a_wire_level_system_exit_does_not_kill_the_renewal_thread(monkeypatch):
    """`SystemExit` is this repo's clean-error idiom and is NOT an `Exception`, so a guard naming
    only `Exception` lets it through — and a daemon thread that dies looks exactly like one that is
    working, right up until the task is reclaimed mid-run."""
    from remote import task_lease

    attempts = []

    def _exits(signaling_url, token, task_id):
        attempts.append(task_id)
        if len(attempts) <= 2:
            raise SystemExit("a clean error from somewhere below")

    monkeypatch.setattr(task_lease.relay, "renew_task_lease", _exits)

    renewer = task_lease.LeaseRenewer(_FakeState(), "t-1", interval=0.01)
    renewer.attach(_FakeProc(alive=True))
    renewer.start()
    try:
        assert _wait_for(lambda: len(attempts) >= 4), "the thread died on a SystemExit"
    finally:
        renewer.close()


def test_a_fault_reaching_the_loop_itself_does_not_kill_the_thread(renewals):
    """The loop's OWN guard, on the path that actually reaches it.

    `_renew_once` guards the wire call, so a fault there never reaches the loop — which means the
    loop's guard is only exercised by something raised OUTSIDE that try: reading the token, or a
    handle whose `poll()` misbehaves. `state.token()` takes a lock and can raise this repo's
    `SystemExit` idiom, so that is the honest case to pin.
    """
    from remote import task_lease

    class _TokenThatFailsFirst:
        signaling_url = "http://relay.test"

        def __init__(self):
            self.reads = 0

        def token(self):
            self.reads += 1
            if self.reads <= 2:
                raise SystemExit("could not read the credential")
            return "tok-1"

        def refresh(self, stale_token=None):
            return False

    state = _TokenThatFailsFirst()
    renewer = task_lease.LeaseRenewer(state, "t-1", interval=0.01)
    renewer.attach(_FakeProc(alive=True))
    renewer.start()
    try:
        assert _wait_for(lambda: len(renewals) >= 2), "the loop's own guard let a SystemExit through"
    finally:
        renewer.close()


def test_close_is_safe_to_call_twice_and_from_a_renewer_that_never_started():
    """`_run_and_report` closes in a `finally`, which runs on paths where `start` was never
    reached."""
    from remote import task_lease

    renewer = task_lease.LeaseRenewer(_FakeState(), "t-1", interval=0.01)
    renewer.close()
    renewer.close()


def test_the_reset_reason_is_bounded_to_what_the_relay_will_accept(monkeypatch, tmp_path):
    """A diagnostic field must never be able to cost a task its result.

    The relay refuses an over-long `session_reset_reason` with a 422 — and a 422 rejects the WHOLE
    terminal report. The report loop reads a 4xx as a verdict and does not retry, so the task is left
    `running`, reclaimed, and retried by a provider that produces the identical reason and the
    identical 422. Every attempt burns on a task whose agent may have succeeded each time, and the
    durable log blames `lease_expired`. Bounding here is what stops that.

    Pinned against the RELAY's own constant, read from the other repository, so the two cannot drift
    apart silently — the whole point of a lockstep value.
    """
    from remote import tasks

    relay_cap = _relay_constant("_MAX_SESSION_RESET_REASON_CHARS")
    assert tasks._MAX_SESSION_RESET_REASON_CHARS <= relay_cap, (
        f"the provider would send up to {tasks._MAX_SESSION_RESET_REASON_CHARS} characters into a "
        f"relay that refuses anything over {relay_cap} — with the whole report")


def test_the_event_ceiling_is_the_one_the_relay_actually_enforces():
    """`MAX_EVENT_BYTES` is a lockstep value, and every other test measures it against OUR copy.

    `task_stream.bounded`, `task_tree`'s byte budget and the agent's output cap are all asserted
    against `task_events.MAX_EVENT_BYTES` — which keeps them consistent with each other and says
    nothing at all about the relay. So the two halves could drift apart with the whole suite green:
    lower the relay's `_MAX_EVENT_BYTES` alone and every one of those tests still passes while the
    fleet starts refusing events.

    What that costs is worse than a refused event. The relay answers **422**, and
    `TaskEventPublisher` treats a 422 as a dropped BATCH rather than a latch — so the agent output
    coalesced alongside the offending event goes with it, every beat, for the rest of the task. The
    task still completes, the log is simply missing most of what happened, and nothing anywhere says
    so.

    Read from grid-src rather than restated, for the same reason the lease TTL is
    (`test_the_worst_case_gap_between_two_renewals_stays_inside_the_relays_lease_ttl`). This test
    lives beside the other parsed checks rather than beside the event code, because `_relay_constant`
    is the thing being reused and one home for it beats a second copy.
    """
    from remote import task_events

    relay_ceiling = _relay_constant("_MAX_EVENT_BYTES")
    assert task_events.MAX_EVENT_BYTES <= relay_ceiling, (
        f"the provider builds events up to {task_events.MAX_EVENT_BYTES} bytes for a relay that "
        f"refuses anything over {relay_ceiling} — and a refusal drops the whole batch, so the "
        f"agent's output goes with it")


def test_the_heartbeat_carries_a_tree_snapshot_while_the_agent_works(renewals):
    """ADR 0032 D-f's tracer bullet: the workspace view rides the beat that already exists.

    Not its own thread and not its own connection — the whole point of publishing rather than
    answering requests is that observability costs one more thing on a beat the provider was sending
    anyway.
    """
    from remote import task_lease

    beats = []
    renewer = task_lease.LeaseRenewer(
        _FakeState(), "t-1", interval=0.01, on_beat=lambda: beats.append(1))
    renewer.attach(_FakeProc(alive=True))
    renewer.start()
    try:
        assert _wait_for(lambda: len(beats) >= 3)
    finally:
        renewer.close()


def test_nothing_is_snapshotted_before_the_agent_exists(renewals):
    """The renewer starts BEFORE `run_task`, and the checkout it covers can run for 300s.

    A snapshot taken in that window would show the PREVIOUS task's workspace — the directory is
    per-project and persists — labelled as this task's. Renewal in that window is still correct and
    still happens; the tree is the part that would be lying.
    """
    from remote import task_lease

    beats = []
    renewer = task_lease.LeaseRenewer(
        _FakeState(), "t-1", interval=0.01, on_beat=lambda: beats.append(1))
    renewer.start()
    try:
        assert _wait_for(lambda: len(renewals) >= 3), "the lease must still be renewed pre-spawn"
    finally:
        renewer.close()

    assert beats == []


def test_the_lease_is_renewed_before_the_tree_is_ever_read(renewals):
    """Order, and it is the whole reason a slow listing is survivable.

    Renewing first means a listing that takes its full budget delays only the NEXT beat, and the
    arithmetic below keeps that inside the relay's TTL. Reading the tree first would put a git
    invocation between the lease and its renewal on every single beat.
    """
    from remote import task_lease

    order = []
    renewer = task_lease.LeaseRenewer(
        _FakeState(), "t-1", interval=0.01, on_beat=lambda: order.append("tree"))
    monkey = renewer  # kept readable below
    renewer.attach(_FakeProc(alive=True))

    import remote.task_lease as module
    real = module.relay.renew_task_lease

    def _recording(signaling_url, token, task_id):
        order.append("lease")
        return real(signaling_url, token, task_id)

    module.relay.renew_task_lease = _recording
    try:
        monkey.start()
        assert _wait_for(lambda: len(order) >= 4)
    finally:
        module.relay.renew_task_lease = real
        renewer.close()

    assert order[:4] == ["lease", "tree", "lease", "tree"]


def test_a_tree_snapshot_that_blows_up_never_costs_the_lease(renewals):
    """The criterion: tree publication failing does not break the heartbeat.

    `WorkspaceTree.beat` is written never to raise, and this does not rest on that — the guard is
    here as well, because a beat that escaped would end the renewal thread and the task would be
    reclaimed mid-run by a *progress* feature.
    """
    from remote import task_lease

    def _explode():
        raise SystemExit("the workspace is on fire")

    renewer = task_lease.LeaseRenewer(_FakeState(), "t-1", interval=0.01, on_beat=_explode)
    renewer.attach(_FakeProc(alive=True))
    renewer.start()
    try:
        assert _wait_for(lambda: len(renewals) >= 3), "a failing tree snapshot stopped the renewals"
    finally:
        renewer.close()


def test_the_tree_stops_the_moment_the_agent_does(renewals):
    """Nothing is vouched for after the child exits, and nothing is narrated either — the terminal
    commit is what says what the workspace finally held."""
    from remote import task_lease

    beats = []
    proc = _FakeProc(alive=True)
    renewer = task_lease.LeaseRenewer(
        _FakeState(), "t-1", interval=0.01, on_beat=lambda: beats.append(1))
    renewer.attach(proc)
    renewer.start()
    try:
        assert _wait_for(lambda: len(beats) >= 2)
        proc.exit(0)
        time.sleep(0.05)
        settled = len(beats)
        time.sleep(0.05)
        assert len(beats) == settled
    finally:
        renewer.close()


def test_the_worst_case_gap_between_two_renewals_stays_inside_the_relays_lease_ttl():
    """The arithmetic that makes riding the heartbeat safe, asserted rather than described — and
    asserted over EVERY term, which is where the first version of it was wrong.

    What must hold is one thing: two SUCCESSFUL renewals are never further apart than the relay's
    TTL. The gap is the loop's whole period, so it counts the renewal's own round trip and the beat's
    — not just the wait. The first version counted the wait and the listing and stopped there, which
    made a 130s gap look like a 35s one:

        30s wait + 30s renew (a POST plus one refresh retry) + 10s listing (`ls-files` twice)
        + 30s waiting on the publisher's lock + 30s for the tree's own POST = 130s > 120s TTL.

    Three changes closed it, and each is pinned by its own test elsewhere in this file or in
    `tests/test_task_tree.py`: the wait is now measured from the START of an iteration, so ordinary
    work no longer adds to the period; the listing has ONE budget across both git calls; and the tree
    publish declines to wait for a busy channel instead of parking on it.

    Every relay-side figure is read from grid-src rather than restated, so raising one of ours
    without raising theirs fails here instead of in a fleet that quietly redoes every long task.
    """
    from remote import relay, task_lease, task_repo

    ttl = _relay_config_constant("task_lease_seconds")
    # A relay round trip, doubled: `_renew_once` and `_send` both retry once past a 401 refresh.
    round_trip = 2 * relay._TASK_EVENT_TIMEOUT
    # One beat: the listing's whole budget, then the tree's own POST. No lock wait — the beat asks
    # the publisher not to block (`test_the_heartbeats_snapshot_never_waits_for_a_busy_channel`).
    beat = task_repo.LS_FILES_TIMEOUT_SECONDS + round_trip
    # The wait is absolute, so a period is the interval OR the work, whichever is longer — never the
    # sum (`test_a_slow_beat_does_not_push_the_next_renewal_out_by_its_own_duration`).
    gap = max(task_lease.RENEW_INTERVAL_SECONDS, round_trip + beat)

    assert gap * 1.5 <= ttl, (
        f"two renewals can be {gap}s apart in the worst case, which leaves no real margin inside "
        f"the relay's {ttl}s lease TTL — long tasks would be reclaimed while running perfectly")


def test_a_slow_beat_does_not_push_the_next_renewal_out_by_its_own_duration(renewals):
    """The period is measured from the start of an iteration, not from the end of its work.

    With a fixed sleep after the work, every second the tree spends is a second the lease is older
    before its next renewal — so the feature that is supposed to cost nothing sets the cadence. Here
    a beat that takes several intervals is absorbed rather than added: renewals keep landing on the
    interval, and only a beat slower than the whole interval delays anything.
    """
    from remote import task_lease

    renewer = task_lease.LeaseRenewer(
        _FakeState(), "t-1", interval=0.05, on_beat=lambda: time.sleep(0.04))
    renewer.attach(_FakeProc(alive=True))
    started = time.monotonic()
    renewer.start()
    try:
        assert _wait_for(lambda: len(renewals) >= 4)
    finally:
        renewer.close()
    elapsed = time.monotonic() - started

    # Four renewals at a 0.05s period is ~0.2s. Adding the 0.04s beat to every period instead would
    # be ~0.36s, so the bound below separates the two without pinning a wall-clock cadence.
    assert elapsed < 0.30, f"the beat's duration is being added to the renewal period ({elapsed:.2f}s)"


def test_the_whole_listing_shares_one_budget_rather_than_one_each(monkeypatch, tmp_path):
    """`list_files` makes TWO git calls, and the lease arithmetic counts the function, not the call.

    A per-call ceiling makes the real worst case double the figure the safety test checks against,
    and it does so invisibly: every test still passes, the comment still reads as though it were one
    budget, and the margin is simply gone. Asserted on what git is actually handed.
    """
    import subprocess

    from remote import task_repo

    spent_by_the_first_call = 0.5
    handed = []
    real = subprocess.run

    def _recording(argv, **kwargs):
        handed.append(kwargs.get("timeout"))
        if len(handed) == 1:
            time.sleep(spent_by_the_first_call)
        return real(argv, **kwargs)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task_repo._ensure_repo(workspace)
    monkeypatch.setattr(subprocess, "run", _recording)

    task_repo.list_files(workspace, timeout=5.0)

    assert len(handed) == 2, f"expected two git calls, got {len(handed)}"
    # The second call's ceiling is what the first one LEFT, not a fresh one. Asserted this way round
    # rather than on the sum of the two figures: the second budget is computed once the first call
    # has returned, so the two never actually overlap on the clock — what a per-call ceiling would
    # break is precisely this, that time already spent is deducted.
    assert handed[1] <= 5.0 - spent_by_the_first_call, (
        f"the second git call was handed {handed[1]}s after the first had already spent "
        f"{spent_by_the_first_call}s — a budget each, so the listing's real ceiling is twice what "
        f"the lease arithmetic assumes")


def test_a_beat_is_never_started_after_close_was_called(monkeypatch):
    """`close()` waits a bounded time and then gives up, and the beat must respect that.

    The bound cannot cover an iteration: `_renew_once`'s own round trip is bounded at
    `relay._TASK_EVENT_TIMEOUT`, which is already three times the join. So the honest guarantee is
    not "no work is in flight" but "no NEW work is started" — and without this check a `close()` that
    timed out while a renewal was parked would be followed by a full tree snapshot, on a task whose
    terminal report is already being written.
    """
    import threading

    from remote import task_lease

    monkeypatch.setattr(task_lease, "_CLOSE_JOIN_SECONDS", 0.1)
    inside_the_relay = threading.Event()
    release = threading.Event()
    beats = []

    def _parked_renewal(_url, _token, _task_id):
        inside_the_relay.set()
        release.wait(5.0)

    monkeypatch.setattr(task_lease.relay, "renew_task_lease", _parked_renewal)
    renewer = task_lease.LeaseRenewer(
        _FakeState(), "t-1", interval=0.01, on_beat=lambda: beats.append(1))
    renewer.attach(_FakeProc(alive=True))
    renewer.start()
    try:
        assert inside_the_relay.wait(5.0)
        renewer.close()
    finally:
        release.set()
    assert _wait_for(lambda: not renewer._thread.is_alive())

    assert beats == [], "a snapshot was started after the renewer had been closed"


def _relay_config_constant(name):
    """A default out of grid-src's `config.py`, parsed rather than imported. See `_relay_constant`."""
    import ast
    import pathlib

    source = pathlib.Path(
        "/Users/macbookpro/Projects/grid-src-feats/distributed-tasks"
        "/grid_cli/private_server/config.py")
    if not source.exists():
        pytest.skip("grid-src worktree is not beside this one; the lockstep cannot be checked here")
    for node in ast.walk(ast.parse(source.read_text())):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == name:
            # `int(os.getenv("TASK_LEASE_SECONDS", "120"))` — the default is the literal the fleet
            # runs on, and it is the second argument of the `getenv` call.
            for sub in ast.walk(node.value):
                if (isinstance(sub, ast.Call)
                        and getattr(getattr(sub, "func", None), "attr", None) == "getenv"
                        and len(sub.args) == 2):
                    return int(ast.literal_eval(sub.args[1]))
    raise AssertionError(f"{name} is no longer defined with a default in grid-src's config.py")


def test_the_provider_outwaits_the_relay_on_a_fetch_and_both_fit_inside_the_deadline():
    """The git transport ceilings, in the one order that works (ADR 0033 issue 16a, item 3).

        relay GIT_RPC_TIMEOUT_SECONDS  <  provider _GIT_NETWORK_TIMEOUT_SECONDS  <  deadline

    Each `<` is a different failure if it is broken, and neither is visible from one repository:

      * **provider below the relay's** — the relay is still willing to serve a fetch the provider has
        already given up on. Every large import fails on the client side while the server does the
        work anyway, and the provider's logs blame a timeout the relay never saw.
      * **provider above the task's deadline** — a single fetch can consume the whole wallclock
        budget, so `task_reaper.reap_past_deadline` ends the task while it is still checking out and
        the agent never runs at all.

    Read out of grid-src rather than restated, for the same reason the lease TTL is: a value
    duplicated by hand drifts, and this is the only place that can notice.
    """
    from remote import task_repo

    relay_rpc = _relay_constant("GIT_RPC_TIMEOUT_SECONDS", module="task_repo.py")
    deadline = _relay_config_constant("task_deadline_seconds")
    provider = task_repo._GIT_NETWORK_TIMEOUT_SECONDS

    assert relay_rpc < provider, (
        f"the relay serves a fetch for up to {relay_rpc}s but this provider gives up at "
        f"{provider}s — a fetch the relay is still willing to serve is one the provider has already "
        f"abandoned, so every large import fails client-side while the relay does the work anyway")
    assert provider < deadline, (
        f"a single fetch may take {provider}s out of the task's whole {deadline}s budget — the "
        f"reaper would end the task mid-checkout and the agent would never run")


def _relay_constant(name, module="tasks.py"):
    """Read a constant out of a grid-src module by parsing it, never by importing it.

    The two repositories share no code and are not installed together; the value is duplicated by
    hand on purpose (see CLAUDE.md's lockstep table). Parsing is how a test can check the duplicate
    still agrees without pretending there is an import path between them.

    `module` defaults to `tasks.py` because that is where the first lockstep values lived; the git
    transport ceiling (ADR 0033 issue 16a) is in `task_repo.py`, so the file is a parameter rather
    than a second copy of this function.
    """
    import ast
    import pathlib

    source = pathlib.Path(
        "/Users/macbookpro/Projects/grid-src-feats/distributed-tasks"
        "/grid_cli/private_server") / module
    if not source.exists():
        pytest.skip("grid-src worktree is not beside this one; the lockstep cannot be checked here")
    for node in ast.parse(source.read_text()).body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == name for t in node.targets):
            return _numeric(node.value, name)
    raise AssertionError(f"{name} is no longer defined in grid-src's tasks.py")


# `ast.literal_eval` accepts `+` and `-` only (for complex literals), so a size written the way
# sizes are normally written — `64 * 1024` — raises rather than evaluating. That is not a nuisance:
# it is why `_MAX_EVENT_BYTES` had no lockstep check at all while the two constants that happen to
# be plain integers did. A helper that silently cannot read half the register is a register that is
# only half checked, so this evaluates the small arithmetic a constant is allowed to be.
# Keyed by the operator node's class NAME so this module keeps `ast` a local import, the way every
# other helper here does.
_OPERATORS = {"Mult": lambda a, b: a * b, "Add": lambda a, b: a + b,
              "Sub": lambda a, b: a - b, "FloorDiv": lambda a, b: a // b}


def _numeric(node, name):
    """The value of an integer constant expression, refusing anything that is not one.

    Deliberately not `eval`: the input is another repository's source file, and the point of parsing
    rather than importing is that nothing in it runs here.
    """
    import ast

    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op).__name__ in _OPERATORS:
        return _OPERATORS[type(node.op).__name__](
            _numeric(node.left, name), _numeric(node.right, name))
    raise AssertionError(
        f"{name} in grid-src's tasks.py is no longer a numeric constant expression "
        f"({ast.dump(node)[:120]}) — the lockstep check cannot read it, so it is not checking it")
