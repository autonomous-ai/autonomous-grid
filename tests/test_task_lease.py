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

from tests.grid_src_repo import grid_src_private_server


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


def test_a_404_that_says_the_task_was_cancelled_does_stop_the_agent(monkeypatch):
    """The one 404 that is not ambiguous (ADR 0033 D-l, issue 19b).

    A member cancelling their task frees their slot on the relay immediately. What the relay cannot
    do is stop the agent: this renewer holds a live child and keeps paying for it out of the
    OPERATOR's own Claude subscription until something tells it not to. The issue was drafted saying
    a cancelled row's next renewal answers 403 — the answer this renewer already kills on — and that
    was measured to be false: a cancelled row keeps its `provider_id`, so grid-src's
    `_refuse_unleased` falls past the "someone else holds it" branch and answers 404.

    So the discriminator is the refusal CODE, which every 4xx in that plane has carried since issue
    19a. The ambiguity the 404 branch exists to protect is untouched: a relay with no lease route at
    all answers FastAPI's bare `{"detail": "Not Found"}`, which carries no code and still leaves the
    agent alone. *Absent ⇒ today's behaviour*, so there is no rollout order — a fleet gains the
    saving as it updates.
    """
    from remote import task_lease

    calls = []

    def _cancelled(signaling_url, token, task_id):
        calls.append(task_id)
        raise task_lease.relay.RelayError(
            "This task was cancelled", status=404, code=task_lease.CANCELLED_CODE)

    monkeypatch.setattr(task_lease.relay, "renew_task_lease", _cancelled)

    proc = _FakeProc(alive=True)
    renewer = task_lease.LeaseRenewer(_FakeState(), "t-1", interval=0.01)
    renewer.attach(proc)
    renewer.start()
    try:
        assert _wait_for(lambda: proc.killed)
    finally:
        renewer.close()

    assert len(calls) == 1, "it kept renewing after an answer that no retry can change"
    # `lost` is False, and that is not an oversight. It means "another provider took this task over",
    # which drives a different sentence in `_supervise_one_task` — nobody took a cancelled task, it
    # was stopped, and reporting a takeover would send somebody looking for a provider that does not
    # exist.
    assert renewer.lost is False
    # ...and `cancelled` is what carries THIS case to `_supervise_one_task` instead. Leaving both
    # False was a real defect: the supervisor branched only on `lost`, so every cancel fell through
    # to the generic no-report message, which promises a reclaim, a retry cap and finally
    # `retries_exhausted`. Measured against a live cancel, NONE of that happens — the row is already
    # terminal, and a terminal row is inert. The operator was being told to wait for a retry the
    # relay would never schedule.
    assert renewer.cancelled is True


def test_a_404_carrying_some_other_code_still_leaves_the_agent_running(monkeypatch):
    """The negative control for the branch above, and the one that matters most.

    `task_not_running` is what an ordinary terminal task answers — most often because an earlier
    report of this provider's own landed and only the acknowledgement was lost. Killing on it would
    be killing on every 404 again, which is the fleet-wide hazard the 404 branch exists to avoid.
    """
    from remote import task_lease

    calls = []

    def _ended(signaling_url, token, task_id):
        calls.append(task_id)
        raise task_lease.relay.RelayError(
            "Task not found or no longer running", status=404, code="task_not_running")

    monkeypatch.setattr(task_lease.relay, "renew_task_lease", _ended)

    proc = _FakeProc(alive=True)
    renewer = task_lease.LeaseRenewer(_FakeState(), "t-1", interval=0.01)
    renewer.attach(proc)
    renewer.start()
    try:
        assert _wait_for(lambda: len(calls) >= 1)
        time.sleep(0.15)  # many renewal intervals
    finally:
        renewer.close()

    assert len(calls) == 1
    assert not proc.killed, "a coded 404 that is not a cancellation killed a healthy agent"


def _lease_against(monkeypatch, response, _real=None):
    """Call the real `relay.renew_task_lease` against a canned HTTP response, and return the error.

    Real request-building and real response-parsing, no network — the same shape `test_local_cli`'s
    `_mock_relay` uses. Patching the module function (as the `renewals` fixture does) is right for
    testing the RENEWER, and useless for testing what the renewer is handed: a hand-built
    `RelayError` would prove only that this test can set an attribute. The code has to come off a
    body the relay could really send.
    """
    import httpx

    from remote import relay

    real = _real or httpx.Client
    monkeypatch.setattr(
        relay.httpx, "Client",
        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(lambda _req: response)}))
    with pytest.raises(relay.RelayError) as caught:
        relay.renew_task_lease("http://relay.test", "tok", "t-1")
    return caught.value


def test_a_cancelled_lease_refusal_arrives_with_its_code_on_the_error(monkeypatch):
    """End to end through the real client: the relay's coded 404 becomes `RelayError.code`."""
    import httpx

    exc = _lease_against(monkeypatch, httpx.Response(404, json={
        "detail": {"code": "task_cancelled", "message": "This task was cancelled"}}))

    assert (exc.status, exc.code) == (404, "task_cancelled")


def test_an_old_relays_bare_404_carries_no_code_at_all(monkeypatch):
    """The degrade, proven against the body FastAPI really sends for an unmatched route.

    This is the case the whole 404 branch exists for, so it is asserted on the wire rather than
    argued: no code means the renewer takes the leave-the-agent-alone path.
    """
    import httpx

    exc = _lease_against(monkeypatch, httpx.Response(404, json={"detail": "Not Found"}))

    assert (exc.status, exc.code) == (404, None)


@pytest.mark.parametrize("body,why", [
    (b"<html>gateway timeout</html>", "a proxy's HTML error page"),
    (b"", "an empty body"),
    (b'{"detail": {"code": 7}}', "a code that is not a string"),
    (b'{"detail": ["not", "a", "dict"]}', "FastAPI's own list-shaped validation detail"),
])
def test_a_body_this_provider_cannot_read_is_never_mistaken_for_a_cancellation(
        monkeypatch, body, why):
    """Every unreadable shape means "no code was stated", never a guess and never a traceback.

    A renewal runs on a background thread, so an exception escaping the parse would not merely lose
    the code — it would take the renewer down and leave the lease to lapse on a task that is running
    perfectly. `json.loads` can raise well outside `ValueError`, which is why the guard is broad.
    """
    import httpx

    exc = _lease_against(monkeypatch, httpx.Response(404, content=body))

    assert exc.code is None, why


def test_a_deeply_nested_body_does_not_take_the_renewal_thread_down(monkeypatch):
    """The reason the parse guard is `except Exception` and not `except ValueError`.

    Every other unreadable body above fails with a `JSONDecodeError`, which IS a `ValueError` — so a
    narrowed guard passes all of them and still has a hole. `json` parses recursively, and past the
    interpreter's stack limit it raises `RecursionError`, a `RuntimeError`. That would escape a
    ValueError-only guard, escape `_renew_once`'s handler as an unrecognised failure, and — because
    a renewal runs on a background thread — end the renewer for a task that is running perfectly.
    """
    import httpx

    exc = _lease_against(monkeypatch, httpx.Response(404, content=b"[" * 200_000))

    assert exc.code is None


def test_promote_and_integrate_are_gone_from_this_cli():
    """ADR 0034 D-d (issue 41). The relay applies every successful turn to `main` itself, so there
    is no release to ask for and nothing to integrate back. Both routes and both commands are
    deleted.

    ⚠️ **The failure this catches is a half-deletion.** A CLI that kept `grid project promote` gets
    FastAPI's bare 404, which `_OLD_RELAY` renders as *"this grid does not serve projects — either
    its relay predates project membership, or … switched off. Ask its operator which"* — sending a
    member to chase an operator over a command this repository was supposed to have removed.

    It asserted the relay's half too (no promote or integrate route in grid-src's `projects.py` or
    `project_status.py`, and no router at all in `merge_tiers.py`) until grid-platform M2 ticket 04
    deleted those modules with the rest of the relay's task plane. This repository's half is what is
    left to check.
    """
    from remote import relay

    for gone in ("promote_project", "integrate_project", "preview_integration"):
        assert not hasattr(relay, gone), (
            f"remote/relay.py still exposes {gone}; ADR 0034 D-d deleted the route it calls, so "
            f"every use of it is a bare 404 reported to the member as an out-of-date relay")


def test_the_auth_scheme_the_credential_helper_names_is_the_one_the_relay_parses():
    """The fourth lockstep value in the register (ADR 0033 D-h, issue 17).

    The credential helper answers `authtype=Bearer`, and git builds the header by joining that to
    the credential with a single space. The relay's `_bearer_or_api_key` reads the header by
    stripping a `"Bearer "` prefix. Two spellings of one wire value, in repositories that share no
    code.

    Read out of the FUNCTION rather than off a module constant, because grid-src spells it as a
    literal at the point of use and giving it a name over there is a cross-repo change this slice
    deliberately does not make. What the check buys is the same thing every other row buys: if the
    relay ever moves to another scheme, or drops Bearer, this fails here instead of every member's
    `git pull` failing with `fatal: Authentication failed` and no clue why.
    """
    from remote import git_credential

    literals = _relay_function_strings("_bearer_or_api_key", module="relay.py")

    assert f"{git_credential.AUTH_SCHEME} " in literals, (
        f"grid-src's `_bearer_or_api_key` no longer reads a "
        f"{git_credential.AUTH_SCHEME!r} prefix, so the credential helper's `authtype` names a "
        f"scheme the relay does not accept. Its literals are: {sorted(literals)}")


def _relay_function_strings(name, module):
    """Every string literal inside one of grid-src's functions, parsed rather than imported.

    It reads the body of a function rather than a module-level assignment, because not every value
    in the cross-repo register is spelled as a named constant on both sides.

    The docstring is collected too, and is deliberately NOT filtered out. A function may name its
    own wire value in its prose — `_bearer_or_api_key` describes the `"Bearer "` prefix it strips —
    which looks at first like a way for this check to pass on English after the real literal has
    drifted. It is not: the caller asserts `value in <this set>`, which is an EXACT element match,
    and the docstring arrives as one element. Measured (on the task plane's `refuse_if_trunk_exists`,
    whose pins grid-platform M2 ticket 04 deleted with it) by renaming the code literal in a copy of
    grid-src's source: the assertion goes red with the docstring counted exactly as it does without
    it. Filtering it would be inert code justified by a hazard that does not exist.
    """
    import ast

    source = _relay_module(module)
    if not source.exists():
        pytest.skip("grid-src worktree is not beside this one; the lockstep cannot be checked here")
    for node in ast.walk(ast.parse(source.read_text())):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return {
                child.value for child in ast.walk(node)
                if isinstance(child, ast.Constant) and isinstance(child.value, str)}
    raise AssertionError(f"{name} is no longer defined in grid-src's {module}")


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


def _relay_module(name):
    """One of grid-src's private_server modules, as a path. `_relay_function_strings` reads source
    out of that package, and finding it is the part that quietly decides whether it runs at all.

    Resolved by `tests/grid_src_repo.py` — the one path derivation this repo has — rather than
    written out here. The absolute path that used to sit here named ONE worktree, and the failure
    that costs is not the one it was written for: measured 2026-09-04 from a second worktree, the
    named path still existed and sat on a DIFFERENT branch, so nothing skipped and every pin
    asserted against the wrong checkout. Absent is loud; present-but-wrong is silent.

    `GRID_SRC_REPO` still overrides, and is validated there rather than skipped past. Skipping here
    stays the rule for a machine that simply has no grid-src beside this worktree: a developer
    without it cannot check the duplicates.
    """
    package = grid_src_private_server()
    if package is None:
        pytest.skip("grid-src worktree is not beside this one; the lockstep cannot be checked here")
    return package / name


def test_a_takeover_and_a_cancel_are_never_the_same_flag(monkeypatch):
    """The negative control for `cancelled`, and it is the half that keeps the fix honest.

    Two flags exist only because the two cases need opposite sentences: after a takeover another
    provider is still producing the result, and after a cancel nobody is and nobody will. A fix that
    set `cancelled` on every give-up would restore the original bug wearing a new name — the
    supervisor would announce "nothing will be retried" for a task that is, at that moment, being
    retried by somebody else.
    """
    from remote import task_lease

    def _taken_over(signaling_url, token, task_id):
        raise task_lease.relay.RelayError("someone else holds it", status=403)

    monkeypatch.setattr(task_lease.relay, "renew_task_lease", _taken_over)

    proc = _FakeProc(alive=True)
    renewer = task_lease.LeaseRenewer(_FakeState(), "t-1", interval=0.01)
    renewer.attach(proc)
    renewer.start()
    try:
        assert _wait_for(lambda: proc.killed)
    finally:
        renewer.close()

    assert renewer.lost is True
    assert renewer.cancelled is False, "a takeover would be announced as 'nothing will be retried'"


def test_an_ambiguous_404_claims_neither_thing(monkeypatch):
    """A relay with no lease route answers a bare framework 404. We know nothing, so we claim nothing.

    Both flags stay False here for the same reason the agent stays alive: this answer is
    indistinguishable from an old relay's, and either flag would put a confident sentence in the
    provider's log about a state nobody has established.
    """
    from remote import task_lease

    def _bare_404(signaling_url, token, task_id):
        raise task_lease.relay.RelayError("Not Found", status=404)

    monkeypatch.setattr(task_lease.relay, "renew_task_lease", _bare_404)

    proc = _FakeProc(alive=True)
    renewer = task_lease.LeaseRenewer(_FakeState(), "t-1", interval=0.01)
    renewer.attach(proc)
    renewer.start()
    try:
        assert _wait_for(lambda: renewer._thread is None or not renewer._thread.is_alive())
    finally:
        renewer.close()

    assert proc.killed is False, "the ambiguous 404 must never kill the agent"
    assert (renewer.lost, renewer.cancelled) == (False, False)


def test_every_command_this_cli_tells_you_to_run_actually_parses():
    """The hints are commands, so they are checked by RUNNING them through the parser.

    Three of them did not. `grid project import` and an empty `grid task list` both printed
    `grid task create --project <id> "<prompt>"`, and `--prompt` is `required=True` with no
    positional to catch the text; `grid project clone` printed a `grid project commit` line with no
    `-m`, which is required too. Each is the first thing a reader copies at that exact moment.

    Asserting the STRINGS would have passed the day they were written and rotted the moment the
    parser changed; asserting that `build_parser()` accepts them cannot.

    Read through the AST rather than line by line, and that is not tidiness. These hints are written
    as implicit string concatenation across two source lines, so a line scan sees `grid task create
    --project <id>` with the `--prompt` on the next line and reports a defect that is not there — a
    FALSE alarm on the one hint that was always correct. `ast` joins the parts the way Python does.
    """
    import ast
    import inspect
    import re

    from cli import parser as cli_parser
    # `project_archive` (ADR 0033 D-p, issue 33) joins the scan because it prints advice of its own
    # — "Undo with: grid project unarchive <id>" is the one sentence a member reads after archiving
    # something they did not mean to. `project_rename` (ADR 0035 D-a, issue 55) for the same reason,
    # and its hint is the harder one: it offers `grid project rename <id> --name <old>`, which has a
    # REQUIRED flag — exactly the shape that produced three of the four defects this test was
    # written for.
    # `project_leave` (ADR 0035 D-b, issue 56) joins them, and it is the shape that produced three
    # of the four defects this test was written for: the line it prints is the OWNER's
    # `grid project member add <id> --email <address>`, which has a required flag AND a free-text
    # value after it.
    from cli import project_archive, project_leave, project_rename, remote_project, remote_task

    def printed_strings(module) -> list[str]:
        """Every literal a `print(...)` in this module emits, with each `{...}` as one token."""
        out = []
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "print"):
                continue
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    out.append(arg.value)
                elif isinstance(arg, ast.JoinedStr):
                    out.append("".join(
                        part.value if isinstance(part, ast.Constant) else "PLACEHOLDER"
                        for part in arg.values))
        return out

    hints = []
    for module in (project_archive, project_leave, project_rename, remote_project, remote_task):
        for text in printed_strings(module):
            found = re.search(r"grid (?:task|project) [a-z][^\n]*", text)
            if found:
                hints.append(found.group(0))

    assert len(hints) >= 4, f"the scan stopped recognising printed hints (found {len(hints)})"

    built = cli_parser.build_parser()
    for hint in hints:
        # As a reader would retype it: drop a trailing `# comment`, then make every placeholder and
        # every quoted free-text argument exactly one token.
        argv = hint.split("#")[0]
        argv = re.sub(r"<[^>]+>", "PLACEHOLDER", argv)
        argv = argv.replace("\u2026", "PLACEHOLDER").replace("'", " ").replace('"', " ")
        argv = argv.replace("grid ", "", 1).strip().rstrip("`.,")
        try:
            built.parse_args(argv.split())
        except SystemExit as exc:  # argparse exits 2 on a usage error
            raise AssertionError(
                f"this CLI prints `grid {argv}` as the next thing to run, and the parser "
                f"refuses it (exit {exc.code})") from exc


def test_the_help_for_a_task_slot_does_not_claim_it_is_per_project():
    """The slot moved from per-project to per-member at ADR 0033 issue 12, and the help did not.

    grid-src serialises on `tasks_one_active_per_member`, so two members run tasks in one project at
    the same time — measured live. A user who believes the old sentence reads a colleague's
    concurrent task as a bug in the grid, and reads their OWN refusal as the grid being busy rather
    than as their own task still running.

    ⚠️ **The claim moved AGAIN at ADR 0034 D-b (issue 40) and the help lagged a second time**, which
    is what this test exists to catch and did not: the index re-keyed to the CONVERSATION, so one
    member holds as many as they like at once and `create_task` no longer refuses on capacity at
    all. The sentence still said "one task in flight per project at a time" — wrong on both halves.
    Corrected at issue 47, where `grid task send`'s own help sits one screen away and would
    otherwise have contradicted it.

    What is pinned is the CLAIM, not the wording: the old sentence must be gone, and the positional
    must still describe what concurrency a member actually gets. `--help` is the only place a person
    reads it.
    """
    from cli import parser as cli_parser

    text = cli_parser.build_parser().format_help()
    # Cheap and specific: the claim, not the wording around it.
    assert "per project at a time" not in text
    # Since issue 28 the id has two spellings, and the sentence rides on the POSITIONAL — `--project`
    # is the same value under another name and carries only a pointer to it. Both must still exist:
    # this describes what a task slot is, and it has to be reachable from the command's own --help.
    create = cli_parser.build_parser()._subparsers._group_actions[0].choices["task"] \
        ._subparsers._group_actions[0].choices["create"]
    assert any("--project" in a.option_strings for a in create._actions), \
        "`grid task create --project` no longer exists"
    for action in create._actions:
        if action.dest == "project_id":
            help_text = action.help or ""
            assert "one task in flight" not in help_text, (
                "the positional still claims a member gets one task at a time; since ADR 0034 D-b "
                "they hold as many conversations as they like and create never refuses on capacity")
            assert "conversations" in help_text, (
                "the positional no longer says what concurrency a member gets, which is the one "
                "thing `--help` is the only place to read")
            break
    else:
        raise AssertionError("`grid task create <project-id>` no longer exists to describe")


# --------------------------------------------------------------------------------------------
# ADR 0034 D-m (issue 45) — the read plane: files, one file, one turn's diff, and a download.
# --------------------------------------------------------------------------------------------


def test_the_download_bound_is_the_relays_alone_and_this_cli_states_no_second_one():
    """A NEGATIVE lockstep check, and the reason it is worth writing.

    `task_download_max_bytes` is the relay's, measured against the tree before a byte is streamed. A
    copy over here would be a second bound that silently disagrees the moment an operator raises
    theirs — this CLI would refuse a download the grid was perfectly willing to serve, and nothing
    would say why. The refusal is displayed verbatim instead, like `project_archived`.
    """
    from pathlib import Path

    client = Path(__file__).resolve().parent.parent
    for name in ("remote/relay.py", "cli/project_download.py"):
        text = (client / name).read_text()
        assert "task_download_max_bytes" not in text and "TASK_DOWNLOAD_MAX_BYTES" not in text, (
            f"{name} names the relay's download ceiling. It is the relay's alone — a copy here is a "
            f"second bound that disagrees the moment an operator raises theirs")


