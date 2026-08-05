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


def _relay_constant(name):
    """Read a constant out of grid-src's tasks module by parsing it, never by importing it.

    The two repositories share no code and are not installed together; the value is duplicated by
    hand on purpose (see CLAUDE.md's lockstep table). Parsing is how a test can check the duplicate
    still agrees without pretending there is an import path between them.
    """
    import ast
    import pathlib

    source = pathlib.Path(
        "/Users/macbookpro/Projects/grid-src-feats/distributed-tasks"
        "/grid_cli/private_server/tasks.py")
    if not source.exists():
        pytest.skip("grid-src worktree is not beside this one; the lockstep cannot be checked here")
    for node in ast.parse(source.read_text()).body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} is no longer defined in grid-src's tasks.py")
