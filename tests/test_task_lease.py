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


def test_the_cancel_code_this_provider_kills_on_is_the_one_the_relay_sends():
    """The lockstep check, parsed out of grid-src rather than restated here.

    There is no compile-time link between the copies. A typo on either side compiles, passes every
    unit test in BOTH repositories, and silently disables the kill — the provider goes on paying for
    an agent nobody is waiting for, and the only symptom is a subscription bill.
    """
    from remote import task_lease

    assert task_lease.CANCELLED_CODE == _relay_string_constant(
        "CANCELLED_CODE", module="task_errors.py")


def test_the_claim_pins_the_transcript_a_retry_must_resume():
    """ADR 0034 D-j's latch, asserted structurally rather than by running an old relay.

    "A failed task's conversation does not carry forward" was recorded by issue 06 as a deliberate
    consequence, but it was never a RULE — it fell out of the transcript riding in a commit that only
    reached `main` on success. Off the merge path it has to be rebuilt by hand, and a retry and a
    follow-up are **not** distinguishable from the claim payload: they arrive as the same shape, and
    `attempt` does not separate them because `_claim_one` increments it on every claim including the
    first.

    So the relay pins the side ref's oid on the TURN row when the turn is created, and sends it here.
    A retry re-claims the same row and therefore sees the same pin — the transcript as it was BEFORE
    the attempt it is retrying. A follow-up is a new row pinned at its own creation and sees the tip.
    The provider needs no branch of its own, which is the point: the pin IS the answer.

    ⚠️ This is the ONE place the relay sends a bare oid to a provider, and it inverts `merge_ref`'s
    argument next to it. `merge_commit` is deliberately withheld because a bare oid is unfetchable —
    `uploadpack.allowAnySHA1InWant` is off. This one works because the provider fetches by NAME and
    the pin is guaranteed reachable in what it fetched, precisely because the ref is fast-forward
    only. Drop the fast-forward rule and this key stops working, which is what makes that rule
    load-bearing rather than tidy.

    *Absent ⇒ the provider fetches no transcript and starts a fresh session* — an older relay's
    behaviour, and never a failure. So the rollout order is the relay before the fleet, and this
    check is what catches a rename before it gets that far.
    """
    keys = _relay_claim_keys()

    assert "transcript_commit" in keys, (
        "grid-src's claim no longer pins the transcript oid, so an automatic retry inherits the "
        "conversation of the attempt it is retrying instead of resetting with its workspace — "
        "silently, because the turn completes and every other signal reads healthy")


def test_the_transcript_ref_prefix_this_provider_pushes_to_is_the_one_the_fence_grants():
    """ADR 0034 D-j: a conversation's transcript lives on `refs/grid/agent/<conversation_id>`.

    The provider BUILDS this ref name — `task_repo.transcript_ref(conversation_id)` — and the relay
    builds the same name to put in `push_refs` and to un-hide in `transfer.hideRefs`. Nothing on the
    wire carries it, so the prefix is the whole of what the two repositories have to agree about.
    The pinned oid rides the claim; the NAME does not, deliberately, because a name a client derives
    from a duplicated constant is one fewer field a proxy can mangle.

    Drift is silent on both sides and total: the fence hides a namespace the provider never asks
    about and refuses the one it does, so `git push` fails with `provider_may_not_push_ref`, the
    settle reports nothing, and the turn is reclaimed until `retries_exhausted` — while every unit
    test in BOTH repositories passes. That is the `refs/integrate/*` dev-VM CRITICAL's shape exactly,
    which is why this check exists before either half is written rather than after.
    """
    from remote import task_repo

    assert task_repo.TRANSCRIPT_PREFIX == _relay_string_constant(
        "TRANSCRIPT_PREFIX", module="task_repo.py")


def _relay_bare_repo_suffix():
    """The suffix grid-src gives a project's bare repository, parsed out of its `task_repo`.

    Parsed rather than restated because it is not a named constant on that side — it is the tail of
    an f-string, `Path(root) / f"{project_id}.git"` — and a value nobody named is exactly the one a
    refactor renames without noticing it was load-bearing somewhere else.

    ⚠️ Matched by SHAPE, not by function name. The first draft of this pinned `repo_path`, which is
    the name of a keyword argument at the call site and not of the function; it failed loudly, which
    is the only reason the mistake was visible at all. Keying on the name would also have made a
    harmless rename over there look like a suffix change over here — and the suffix is the thing
    that matters, so that is what is read.
    """
    import ast

    source = _relay_module("task_repo.py")
    if not source.exists():
        pytest.skip("grid-src worktree is not beside this one; the lockstep cannot be checked here")
    found = set()
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.Return) and isinstance(node.value, ast.BinOp)):
            continue
        joined = node.value.right
        if not (isinstance(joined, ast.JoinedStr) and joined.values):
            continue
        tail = joined.values[-1]
        if isinstance(tail, ast.Constant) and isinstance(tail.value, str):
            found.add(tail.value)
    assert len(found) == 1, (
        f"expected exactly one `Path(...) / f'…'` return in grid-src's task_repo.py to read the "
        f"bare-repo suffix from, found {sorted(found)} — this check can no longer say which "
        f"spelling this provider's sweep has to recognise")
    return found.pop()


def test_the_directory_the_sweep_refuses_to_collect_is_the_one_the_relay_creates():
    """⚠️ The only thing standing between this provider's eviction sweep and a live repository.

    A relay and a provider on one box share `/var/grid/projects` **by default** —
    `task_agent.default_workspace_root()` is `/var/grid` on Linux and grid-src's
    `config.task_repo_root` is `/var/grid/projects` — so the relay's bare repos are siblings of this
    provider's project directories. `task_evict` walks that tree three levels deep and `rmtree`s
    what it finds, so without the suffix check `<project_id>.git/objects/pack` is an ordinary
    eviction candidate. Reproduced in `tests/test_task_evict.py`: the relay's whole object store,
    gone in one sweep.

    Nothing on the wire carries this name, which is what makes it a lockstep value rather than a
    protocol one: both sides derive it, and drift is silent AND catastrophic in one direction only.
    A rename on the relay side does not break a request or fail a test over there — it quietly
    re-arms a `shutil.rmtree` over here.
    """
    from remote import task_evict

    assert task_evict.RELAY_REPO_SUFFIX == _relay_bare_repo_suffix(), (
        "grid-src names a project's bare repository with a different suffix now, so this "
        "provider's sweep no longer recognises one and will collect it as a stale workspace")


def test_the_two_refusal_codes_this_cli_branches_on_are_the_ones_the_relay_sends():
    """The CLI's half of the parsed-`detail` register (ADR 0033 D-o, issue 26).

    `grid task create` reads exactly two codes, and each one changes what the user is told to do:

      * **`project_has_no_trunk`** decides whether they get the two commands that fix a trunkless
        project or the relay's own sentence, which names import as the only way forward and has been
        wrong since D-o added a second;
      * **`project_already_has_trunk`** decides whether `--init-project` runs their task or refuses
        it, on a project whose trunk somebody else created five minutes earlier.

    Neither is a named constant in grid-src — both are literals at their point of use, and giving
    them names over there is a cross-repo change this slice deliberately does not make — so the
    FUNCTION is parsed instead. Both degrade safely if they drift (the branch simply never fires),
    which is exactly why nothing else would ever notice: no test fails in either repository, and the
    only symptom is advice quietly reverting to the version that could not work.
    """
    from cli import remote_task

    assert remote_task._NO_TRUNK in _relay_function_strings("wip_base_ref", module="tasks.py"), (
        "grid-src's `wip_base_ref` no longer refuses a trunkless project with the code "
        "`grid task create` offers `--init-project` for")
    assert remote_task._TRUNK_EXISTS in _relay_function_strings(
        "refuse_if_trunk_exists", module="project_trunk.py"), (
        "grid-src's shared trunk guard no longer sends the code `--init-project` treats as "
        "'the trunk you asked for is already there'")


def test_the_terminal_state_set_is_the_same_in_all_four_copies():
    """ADR 0034 D-a keeps the state sets on the TURN and unrenamed on the wire — and there are FOUR
    copies of them in step, not two.

    grid-src holds three and pins them against each other (`test_task_events.py`): `TERMINAL_STATES`
    in `tasks.py`, its private twin in `task_events.py`, and `TASK_ACTIVE_STATES` in `db.py`, which
    is the predicate of the partial unique index and is documented as FROZEN. The fourth is here,
    and until this test nothing in either repository compared it to the other three.

    What drift costs is asymmetric, which is why the CLI's copy is the conservative half: a state
    this build has never heard of is treated as not-yet-finished, so `grid task fetch` refuses
    rather than handing back the input files as though they were the result. That is a good default
    and it is not a substitute for agreeing — a terminal state MISSING from this set makes every
    fetch of a task that ended in it refuse forever, with the relay reporting a perfectly finished
    task.

    The two sets are complements of one closed set of six, so this asserts the relationship rather
    than either list: an overlap would let a "terminal" report leave the member's slot held with no
    reaper watching it.
    """
    from cli import remote_task

    terminal = _relay_string_set("TERMINAL_STATES", module="tasks.py")
    private_twin = _relay_string_set("_TERMINAL_STATES", module="task_events.py")
    active = _relay_string_set("TASK_ACTIVE_STATES", module="db.py")

    assert terminal == private_twin, (
        "grid-src's own two copies of the terminal set have drifted from each other")
    assert remote_task._TERMINAL_STATES == terminal, (
        f"this CLI treats {sorted(remote_task._TERMINAL_STATES)} as terminal and the relay reports "
        f"{sorted(terminal)} — `grid task fetch` refuses a finished task, or hands back input files "
        f"as though they were a result")
    assert not (terminal & active), (
        "a state is both terminal and active, so a finished task would hold its member's slot with "
        "no reaper watching it")


def test_the_claim_payload_carries_both_ids_and_they_are_not_one_key():
    """ADR 0034 D-a's second spelling, asserted STRUCTURALLY rather than by running an old provider.

    `conversation_id` joins the claim payload in issue 37 and `task_id` keeps meaning the TURN —
    what `/lease`, `/result`, `/events` and `/cancel` address, and what this provider hands its
    lease renewer (`remote/task_lease.renew_task_lease`). One spelling for both is how a provider
    renews the wrong id, gets a bare 404 — byte-identical to the one an older relay sends for a
    route it does not have — stops renewing WITHOUT killing the agent, and leaves the run
    unattended until its lease expires. Nothing goes red anywhere in that story.

    Read off `_claim_one`'s returned dict rather than off a live response, because two of these
    keys are ones `remote/tasks.run_task` REFUSES a claim without — terminally, so a relay that
    stopped sending either would fail every task on the fleet rather than degrade. That is the
    fail-closed direction by design, and it is why the rollout order is the relay BEFORE the
    providers; this check is what catches the rename before it gets that far.
    """
    keys = _relay_claim_keys()

    assert "task_id" in keys, (
        "grid-src's claim no longer sends `task_id`; the lease renewer has nothing to renew with")
    assert "conversation_id" in keys, (
        "grid-src's claim no longer sends `conversation_id` — `remote/tasks.run_task` refuses such "
        "a claim outright (ADR 0034 D-c) rather than running in a member-level workspace, so every "
        "task on every provider would fail")
    assert "member_key" in keys, (
        "the key `remote/tasks.run_task` already refuses a claim without (ADR 0033 D-g) is gone")


def _relay_string_set(name, module):
    """A set or tuple of STRING literals out of a grid-src module, parsed rather than imported.

    A third sibling of `_relay_constant` and `_relay_string_constant`, and separate for their
    reason: each refuses a shape it does not understand instead of returning something plausible.
    The two spellings accepted here are the two grid-src uses — `frozenset({...})` for the terminal
    sets and a bare tuple for `TASK_ACTIVE_STATES`, which is a tuple because it is the predicate of
    a partial index. Returned as a `frozenset` so a caller compares membership, never order.
    """
    import ast

    source = _relay_module(module)
    if not source.exists():
        pytest.skip("grid-src worktree is not beside this one; the lockstep cannot be checked here")
    for node in ast.parse(source.read_text()).body:
        if not (isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == name for t in node.targets)):
            continue
        value = node.value
        # `frozenset({...})` / `set([...])` — the collection is the call's single argument.
        if isinstance(value, ast.Call) and len(value.args) == 1:
            value = value.args[0]
        assert isinstance(value, (ast.Set, ast.Tuple, ast.List)), (
            f"grid-src's {name} is no longer a literal collection, so this lockstep check cannot "
            f"read it — teach this helper the new shape rather than deleting the check")
        members = []
        for element in value.elts:
            assert isinstance(element, ast.Constant) and isinstance(element.value, str), (
                f"grid-src's {name} holds something that is not a plain string literal, so this "
                f"check would compare a set it has only partly read")
            members.append(element.value)
        assert members, f"grid-src's {name} is empty, so comparing against it proves nothing"
        return frozenset(members)
    raise AssertionError(f"{name} is no longer defined in grid-src's {module}")


# The functions the claim is made of on grid-src's side. `_claim_one` alone until ADR 0034 D-b
# (issue 40) split the transaction into `_claim_pass` so the catch for an `IntegrityError` raised by
# the COMMIT could sit outside it — the payload dict moved with the body. Both are searched rather
# than the name being swapped, because either one holding it is a correct shape and this check is
# about the KEYS, not about which function returns them.
_RELAY_CLAIM_FUNCTIONS = ("_claim_one", "_claim_pass")


def _relay_claim_keys():
    """The key set of the dict the claim answers a provider with, parsed out of grid-src.

    Reads the LAST `return {...}` across `_claim_one`/`_claim_pass`, which is the payload — their
    only other returns are a bare `None` for an empty queue. A dict whose keys stopped being plain
    literals is an error rather than a partial answer, for `_relay_string_set`'s reason.

    ⚠️ Finding NO dict at all is an error too, and deliberately not a skip: that is what a rename or
    a refactor on the other side looks like, and skipping would turn this lockstep check off in
    silence — which is the one failure it exists to prevent.
    """
    import ast

    source = _relay_module("tasks.py")
    if not source.exists():
        pytest.skip("grid-src worktree is not beside this one; the lockstep cannot be checked here")
    tree = ast.parse(source.read_text())
    dicts = []
    seen = set()
    for node in ast.walk(tree):
        if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in _RELAY_CLAIM_FUNCTIONS):
            continue
        seen.add(node.name)
        dicts += [child.value for child in ast.walk(node)
                  if isinstance(child, ast.Return) and isinstance(child.value, ast.Dict)]
    assert seen, (
        f"none of {list(_RELAY_CLAIM_FUNCTIONS)} is defined in grid-src's tasks.py — the claim was "
        f"renamed, so teach this check the new name rather than deleting it")
    assert dicts, (
        "grid-src's claim no longer returns a dict literal, so this check cannot read the claim "
        "payload's shape — teach it the new one rather than deleting the check")
    keys = []
    for key in dicts[-1].keys:
        assert isinstance(key, ast.Constant) and isinstance(key.value, str), (
            "the claim payload has a computed key, so this check would compare a shape it has "
            "only partly read")
        keys.append(key.value)
    return frozenset(keys)


def _relay_return_keys(function, module):
    """The key set of the LAST dict a named grid-src function returns.

    `_relay_claim_keys` generalised — that one searches two functions for the claim payload; this
    one reads a single named function, which is what `task_view` is. Split rather than widened
    because the claim's "either of these two may hold it" is a property of the claim's own split
    (ADR 0034 D-b) and not something a caller here should have to state.

    Everything it refuses, it refuses the way `_relay_claim_keys` does, for the same reason: a
    function that is gone, a return that is no longer a dict literal, and a computed key are all
    ERRORS rather than skips — each is exactly what a rename on the other side looks like, and
    skipping would turn the check off in silence.
    """
    import ast

    source = _relay_module(module)
    if not source.exists():
        pytest.skip("grid-src worktree is not beside this one; the lockstep cannot be checked here")
    dicts = []
    found = False
    for node in ast.walk(ast.parse(source.read_text())):
        if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == function):
            continue
        found = True
        dicts += [child.value for child in ast.walk(node)
                  if isinstance(child, ast.Return) and isinstance(child.value, ast.Dict)]
    assert found, (
        f"{function} is no longer defined in grid-src's {module} — it was renamed, so teach this "
        f"check the new name rather than deleting it")
    assert dicts, (
        f"grid-src's {function} no longer returns a dict literal, so this check cannot read its "
        f"shape — teach it the new one rather than deleting the check")
    keys = []
    for key in dicts[-1].keys:
        assert isinstance(key, ast.Constant) and isinstance(key.value, str), (
            f"grid-src's {function} has a computed key, so this check would compare a shape it has "
            f"only partly read")
        keys.append(key.value)
    return frozenset(keys)


def _relay_route_paths(module):
    """Every path string a grid-src module's `@router.<method>(...)` decorators declare."""
    import ast

    source = _relay_module(module)
    if not source.exists():
        pytest.skip("grid-src worktree is not beside this one; the lockstep cannot be checked here")
    paths = []
    for node in ast.walk(ast.parse(source.read_text())):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not (isinstance(decorator, ast.Call) and decorator.args):
                continue
            target = decorator.func
            if getattr(getattr(target, "value", None), "id", None) != "router":
                continue
            first = decorator.args[0]
            assert isinstance(first, ast.Constant) and isinstance(first.value, str), (
                f"a route in grid-src's {module} no longer declares a literal path, so this check "
                f"would compare a shape it has only partly read")
            paths.append(first.value)
    assert paths, (
        f"grid-src's {module} declares no routes, so this check proves nothing — it was renamed or "
        f"emptied, and that is what this assertion is for")
    return paths


def test_the_follow_up_route_this_cli_posts_to_is_the_one_the_relay_serves():
    """ADR 0034 D-n (issue 47): `POST /relay/v1/tasks/{conversation_id}/turns`.

    The path is the whole of the contract here — there is no body key to disagree about — and a
    drift is SILENT in the worst direction: this CLI would post to a path the relay does not serve,
    get FastAPI's bare 404, and `missing_route_hint` would turn it into "ask your operator to
    update the relay" about a relay that is perfectly up to date. The member is then told to chase
    an operator over a typo in this repository.

    The CLI's half is read out of `send_turn`'s own f-string rather than restated, so the thing
    compared is what the request is actually built from.
    """
    import ast
    import inspect

    from remote import relay

    sent = [
        "".join(part.value if isinstance(part, ast.Constant) else "{}" for part in node.values)
        for node in ast.walk(ast.parse(inspect.getsource(relay.send_turn)))
        if isinstance(node, ast.JoinedStr)
    ]
    assert sent, "send_turn no longer builds its path from an f-string; teach this check the new one"

    # The relay's routers carry the `/relay/v1` prefix on the `APIRouter`, not on each decorator,
    # so it is added back here rather than stripped off the client's — the prefix is a third copy
    # (grid-apis has one too) and asserting the CLIENT still spells it is part of the point.
    served = {"/relay/v1" + path.replace("{conversation_id}", "{}")
              for path in _relay_route_paths("task_turns.py")}

    assert set(sent) <= served, (
        f"this CLI posts a follow-up to {sorted(set(sent) - served)}, which grid-src's "
        f"task_turns.py does not serve — every send would get a bare 404 and be reported as "
        f"'your relay is too old'")


def test_the_conversation_stream_this_cli_reads_is_the_one_the_relay_serves():
    """ADR 0034 D-m (issue 51): `GET /relay/v1/tasks/{conversation_id}/stream`.

    Its own route rather than a parameter on `/events`, because `/tasks/{id}/events` addresses a
    TURN and `task_id` on the wire has always meant the turn. So the path is the whole contract, and
    a drift is silent in the worst direction — exactly `send_turn`'s: this CLI would ask for a path
    the relay does not serve, get the bare framework 404, and report a perfectly up-to-date relay as
    too old.

    Read out of `stream_conversation_events`' own f-string rather than restated.
    """
    import ast
    import inspect

    from remote import relay

    # Filtered to the f-strings that are PATHS. Unlike `send_turn`, this function also builds its
    # transport-error message with one, and a check that swept both would fail on a sentence.
    sent = [
        text for text in (
            "".join(part.value if isinstance(part, ast.Constant) else "{}" for part in node.values)
            for node in ast.walk(ast.parse(inspect.getsource(relay.stream_conversation_events)))
            if isinstance(node, ast.JoinedStr))
        if text.startswith("/relay/")
    ]
    assert sent, ("stream_conversation_events no longer builds its path from an f-string; teach "
                  "this check the new one")

    served = {"/relay/v1" + path.replace("{conversation_id}", "{}")
              for path in _relay_route_paths("conversation_stream.py")}

    assert set(sent) <= served, (
        f"this CLI follows a conversation at {sorted(set(sent) - served)}, which grid-src's "
        f"conversation_stream.py does not serve — every follow would get a bare 404 and be "
        f"reported as 'your relay is too old'")


def test_the_block_that_ends_a_conversation_stream_is_the_one_the_relay_sends():
    """ADR 0034 D-m (issue 51). `conversation.idle` is to a conversation what `task.terminal` is to
    a turn, and a conversation has no terminal state to fall back on (D-a).

    Drifted, this follower never recognises the end: it falls into its empty-reattach budget and
    reports a healthy idle conversation as a lost stream, exiting non-zero. Loud rather than silent,
    unlike `kind`'s degrade — but wrong on every single follow, so it is pinned.
    """
    from cli import remote_task

    assert remote_task._CONVERSATION_IDLE == _relay_string_constant(
        "CONVERSATION_IDLE_EVENT", "task_events.py")


def _client_paths(function):
    """Every path a relay-client function builds from an f-string, with its holes blanked.

    Adjacent f-strings are concatenated by the PARSER, so a path split over two source lines is one
    `JoinedStr` here — which is what `reset_project_wip` relies on.
    """
    import ast
    import inspect

    return {
        "".join(part.value if isinstance(part, ast.Constant) else "{}" for part in node.values)
        for node in ast.walk(ast.parse(inspect.getsource(function)))
        if isinstance(node, ast.JoinedStr)
    }


def test_the_commit_route_this_cli_posts_to_is_the_one_the_relay_serves():
    """ADR 0034 D-e (issue 41): `POST /relay/v1/tasks/{conversation_id}/commit`.

    ⚠️ **This route MOVED, and the move is the safety property.** Under ADR 0033 it was
    `POST /projects/{id}/commit`, and the branch it wrote was the member's. D-e re-keys that branch
    to the conversation, so the request has to name one — and adding a `conversation_id` KEY to the
    old path would have been the silent-degrade shape this tracker has recorded twice (issues 10 and
    48): the old route reads the keys it knows and drops the rest, so an old relay would answer
    **200** for a commit that landed on a different branch. A new PATH answers a bare 404 instead.

    Which makes a typo here worse than usual: this CLI would post to a path no relay serves, get
    FastAPI's bare 404, and `_OLD_RELAY_NO_COMMIT` would turn it into "ask your operator to update
    the relay" about a relay that is perfectly up to date.
    """
    from remote import relay

    sent = _client_paths(relay.commit_project)
    assert sent, "commit_project no longer builds its path from an f-string; teach this check"

    served = {"/relay/v1" + path.replace("{conversation_id}", "{}")
              for path in _relay_route_paths("project_commit.py")}

    assert sent <= served, (
        f"this CLI commits to {sorted(sent - served)}, which grid-src's project_commit.py does not "
        f"serve — every commit would get a bare 404 and be reported as 'your relay is too old'")


def test_the_wip_reset_route_this_cli_posts_to_is_the_one_the_relay_serves():
    """ADR 0034 D-e (issue 41): the reset is addressed by CONVERSATION, not by member key.

    `wip reset` is the one command of the old surface that SURVIVES the clean break (ADR 0034 D-m),
    because D-d's apply can still leave a branch ahead of a turn's input and this is the documented
    recovery. Its path segment changed meaning, and nothing about the URL's SHAPE says so — both
    spellings are `…/wip/<something>/reset` — so a half-updated pair would 404 or, worse, resolve
    against a member key that happens to exist.
    """
    from remote import relay

    sent = _client_paths(relay.reset_project_wip)
    assert sent, "reset_project_wip no longer builds its path from an f-string; teach this check"

    served = {"/relay/v1" + path.replace("{project_id}", "{}").replace("{conversation_id}", "{}")
              for path in _relay_route_paths("projects.py")}

    assert sent <= served, (
        f"this CLI resets a branch at {sorted(sent - served)}, which grid-src's projects.py does "
        f"not serve")


def test_promote_and_integrate_are_gone_from_BOTH_repositories():
    """ADR 0034 D-d (issue 41). Their absence is a BREAK rather than a degrade, so it is asserted
    from both sides at once.

    The relay applies every successful turn to `main` itself, so there is no release to ask for and
    nothing to integrate back. Both routes and both commands are deleted.

    ⚠️ **The failure this catches is a half-deletion, and it is silent in the direction that
    matters.** A CLI that kept `grid project promote` against a relay that dropped the route gets
    FastAPI's bare 404, which `_OLD_RELAY` renders as *"this grid's relay does not have projects
    yet — ask its operator to update it"* — sending a member to chase an operator over a command
    this repository was supposed to have removed. Keeping the relay half instead is the milder
    failure and is still wrong: it leaves a second, unreachable writer of `main`.
    """
    from remote import relay

    for gone in ("promote_project", "integrate_project", "preview_integration"):
        assert not hasattr(relay, gone), (
            f"remote/relay.py still exposes {gone}; ADR 0034 D-d deleted the route it calls, so "
            f"every use of it is a bare 404 reported to the member as an out-of-date relay")

    served = set()
    for module in ("projects.py", "project_status.py"):
        served |= set(_relay_route_paths(module))
    offending = {path for path in served
                 if "promote" in path or "integrate" in path}
    assert not offending, (
        f"grid-src still serves {sorted(offending)} — ADR 0034 D-d deletes promote, integrate and "
        f"the integrate preview, and a route with no client is a second writer of `main` nothing "
        f"in this repository can see")

    # The tier machinery SURVIVES the route that used to call it — `trunk_apply` asks `decide_tier`
    # on every settle, and issue 42 needs `_merge_task`. What must not survive is a way in from
    # outside, so this asserts the module declares no router at all rather than that its paths are
    # clean. `_relay_route_paths` cannot be used here: it refuses a module with no routes, which is
    # exactly the state being asserted.
    tiers = _relay_module("merge_tiers.py")
    if not tiers.exists():
        pytest.skip("grid-src worktree is not beside this one; the lockstep cannot be checked here")
    assert "@router." not in tiers.read_text(), (
        "grid-src's merge_tiers.py declares a route again — it is the tier DECISION and the "
        "merge-turn machinery, and anything reachable there is a writer of `main` with no client")


def test_the_task_view_names_the_conversation_a_follow_up_is_addressed_to():
    """ADR 0034 D-n (issue 47): `conversation_id` on `tasks.task_view`.

    ⚠️ **It is the ONLY way a person can learn the id `grid task send` takes.** Before this slice
    `conversation_id` reached exactly one payload — the provider's CLAIM — so nothing a member can
    call had ever said what conversation their turn is in. Drop it from the view and the command
    still parses, still posts, and has no address anybody can obtain; `grid task create`'s output
    silently loses the line that makes the feature discoverable, and nothing else goes red.

    ⚠️ And `id` must stay beside it. They are two objects (D-a) — `id` is the TURN, which `/lease`,
    `/result`, `/events` and `/cancel` address — and a view that reported one of them for both is
    how a client cancels the wrong thing.
    """
    keys = _relay_return_keys("task_view", module="tasks.py")

    assert "conversation_id" in keys, (
        "grid-src's task_view no longer reports conversation_id, so `grid task send` has no "
        "address a person could get — the command is unreachable and nothing else fails")
    assert "id" in keys, (
        "the turn's own id left the view, so a client has the conversation and nothing to follow, "
        "cancel or fetch")


def test_the_single_turn_read_says_when_a_turns_work_no_longer_stands():
    """ND-16/F-3: `changed_since_count` / `changed_since_paths` on `GET /tasks/{id}`.

    ⚠️ **A rename on the relay side is SILENT here, in the direction that costs somebody their
    work.** `cli/remote_task._changed_since_note` requires both keys, type-checked, and prints
    NOTHING otherwise — the correct degrade for a relay predating this slice, and byte-identical to
    a relay that renamed them. So a drift raises nothing, warns nobody and fails no request: the
    warning simply stops appearing, and the person whose line a colleague's turn dropped goes back
    to reading `completed` on every surface they have.

    ⚠️ **Pinned on `get_task` and NOT on `task_view`, deliberately.** The pair is added by the route
    rather than by the shared view because the answer costs two git reads, and `task_view` is also
    what `GET /tasks` builds every row of — a listing of forty turns would pay eighty. A future
    tidy-up that moved it into the view would make the listing quietly expensive, so this check
    names the place the cost is bounded.

    Rollout is **relay before CLI**, with the project routes: an old relay sends neither key and the
    CLI says nothing, which is the pre-slice behaviour exactly.
    """
    import ast

    source = _relay_module("tasks.py")
    if not source.exists():
        pytest.skip("grid-src worktree is not beside this one; the lockstep cannot be checked here")
    found = [node for node in ast.walk(ast.parse(source.read_text()))
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "get_task"]
    assert found, (
        "get_task is no longer defined in grid-src's tasks.py — it was renamed, so teach this check "
        "the new name rather than deleting it")
    literals = {node.value for node in ast.walk(found[0])
                if isinstance(node, ast.Constant) and isinstance(node.value, str)}

    for key in ("changed_since_count", "changed_since_paths"):
        assert key in literals, (
            f"grid-src's `GET /tasks/{{id}}` no longer reports `{key}`, so `grid task get` silently "
            f"stops saying that the project may no longer hold what a turn changed — the CLI reads "
            f"a missing key as 'an old relay' and prints nothing at all")


def test_the_status_view_reports_where_a_member_stands_against_their_running_cap():
    """ADR 0034 D-i (issue 49): `member_running_turns` / `member_running_cap` on the status view.

    ⚠️ **A rename on the relay side is SILENT on this one**, which is the whole reason it is pinned
    here rather than left to either suite. `cli/remote_project._print_your_running_cap` requires both
    keys to be present integers and prints NOTHING when they are not — the correct degrade for a
    relay predating this slice, and indistinguishable from a relay that renamed them. So a drift does
    not raise, does not warn and fails no request: the line simply stops appearing, and the one state
    where the queue and provider block below it is not the explanation goes back to being
    unexplainable.

    The MECHANISM has no half to duplicate — the cap is `turn_eligibility.is_under_the_member_cap`
    and `TASK_MEMBER_RUNNING_CAP` is the relay's alone. These two keys are the only wire values in
    the slice, and they inherit the project routes' **relay before CLI**.
    """
    keys = _relay_return_keys("project_status", module="project_status.py")

    for key in ("member_running_turns", "member_running_cap"):
        assert key in keys, (
            f"grid-src's status view no longer reports `{key}`, so `grid project status` silently "
            f"stops saying why a member's own work is holding their next message — the CLI treats a "
            f"missing key as 'an old relay' and prints nothing at all")
    # Beside them, and deliberately DIFFERENT: `active_turns` is this PROJECT's, the two above are
    # grid-wide. If that one ever went away the CLI's turn listing would go quiet the same way.
    assert "active_turns" in keys, (
        "`active_turns` left the status view; `member_running_turns` is grid-wide and is not a "
        "replacement for it")


def test_the_merge_turn_marker_this_cli_reads_is_the_one_the_relay_writes():
    """ADR 0034 D-g (issue 42): `kind` on the task view.

    ⚠️ **A typo in either copy is SILENT in the direction that matters.** This CLI compares for
    equality and treats anything else as a person's message, which is the correct degrade for an old
    relay — so a drifted literal does not raise, does not warn, and does not fail a request. It shows
    the relay's own merge prompt, `git merge` and a `refs/integrate/…` ref included, in a column
    headed PROMPT beside the member's own messages and attributed to them. Every unit test on this
    side reads a document this repo wrote down, so both suites stay green.

    Parsed out of grid-src rather than restated, the rule this file has followed since the lease
    pair. **The relay before the CLI**, like every other value on this view.

    `MESSAGE_KIND` is asserted too even though this CLI never compares against it: it is the other
    half of the closed set `task_view` answers with, and a value that drifted out of it would make
    `kind` report something neither side recognises while both remain internally consistent.
    """
    from cli import remote_task

    assert remote_task._MERGE_KIND == _relay_string_constant("MERGE_KIND", module="db.py")
    assert _relay_string_constant("MESSAGE_KIND", module="db.py") == "message"


def test_the_task_view_says_whether_a_person_or_the_grid_asked_for_a_turn():
    """ADR 0034 D-g (issue 42). The marker has to be on the view a MEMBER reads, not only in the
    database: `grid task list` and an application's conversation view are where a merge turn is
    otherwise indistinguishable from something the person typed.

    Separate from the equality check above because the two fail differently. That one catches the
    literals drifting apart; this one catches the key never arriving — the relay stores `kind`
    faithfully, the CLI reads `task.get("kind")`, and the answer is `None` on every row forever, so
    every merge turn renders as a person's message and nothing anywhere is inconsistent.
    """
    keys = _relay_return_keys("task_view", module="tasks.py")

    assert "kind" in keys, (
        "grid-src's task_view no longer says who asked for a turn, so every merge turn the relay "
        "inserts is rendered as a message the member typed — including the relay's own git prose")


def test_the_owner_role_this_cli_filters_on_is_the_one_the_relay_writes():
    """`grid task create` with no `--project` resolves the caller's OWN project called `default`,
    and `owner` is the whole of what makes it theirs (ADR 0033 D-a / D-o).

    `GET /relay/v1/projects` lists every project the caller is a member of, so if this role string
    ever stopped matching, the lookup would find nothing and every projectless `task create` would
    refuse — annoying, loud, and harmless. The dangerous direction is the one this pins from the
    other end: the filter is the only thing standing between "my default project" and a colleague's,
    and a project name is unique per OWNER rather than per grid.
    """
    from cli import remote_task

    assert remote_task._OWNER_ROLE == _relay_string_constant(
        "OWNER_ROLE", module="project_members.py")


def test_the_bootstrap_values_this_cli_sends_are_the_ones_the_relay_accepts():
    """ADR 0034 D-o (issue 48): `bootstrap` on `POST /relay/v1/projects`.

    ⚠️ **This pair has no safe degrade and that is why it is here.** grid-src's `create_project`
    reads `name` and nothing else — there is no unknown-key refusal on that route — so a relay
    that has not been updated DROPS the key and answers 201 for a project with no trunk. A typo in
    either copy therefore does not fail loudly anywhere: this CLI would send `"emtpy"`, a new relay
    would refuse it with a 422 naming the field, and an OLD one would create a trunkless project
    and report success. The client's postcondition check catches the second case; only this check
    catches the first before it ships.

    `BOOTSTRAP_IMPORT` is asserted even though this CLI never sends it. It is half of the closed set
    the relay validates against, and a value that drifted out of that set is exactly what would make
    a future `--for-import` flag refuse on arrival.
    """
    from remote import relay

    assert relay.BOOTSTRAP_EMPTY == _relay_string_constant(
        "BOOTSTRAP_EMPTY", module="project_trunk.py")
    assert relay.BOOTSTRAP_IMPORT == _relay_string_constant(
        "BOOTSTRAP_IMPORT", module="project_trunk.py")


def test_the_capacity_load_key_this_provider_publishes_is_the_one_the_relay_reads():
    """The other lockstep value this slice adds (ADR 0033 D-l, issue 19b).

    Filed here rather than in `test_task_capacity.py` because this module is where the cross-repo
    register is checked — the lease TTL pair and the git-transport pair are already parsed out of
    grid-src from these helpers, and one home for "does the duplicate still agree" is what stops a
    second copy of the parser drifting.

    A typo in either copy of the key is silent in a way the cancel code's is not: no test fails and
    no error is logged, the relay simply never sees a withdrawal and a whole team watches an
    unexplained queue — which is the failure this slice exists to remove.
    """
    from remote import task_capacity

    assert task_capacity.PAUSED_LOAD_KEY == _relay_string_constant(
        "PAUSED_LOAD_KEY", module="task_capacity.py")


def test_the_queue_expired_reason_this_client_explains_is_the_one_the_relay_writes():
    """The third lockstep value in the register (ADR 0033 D-k, issue 18).

    Terminal `error` slugs are otherwise displayed verbatim and never compared — the rule
    `task.retry`'s `reason` follows — and this one is the deliberate exception, because
    `queue_expired` and `deadline_exceeded` call for OPPOSITE actions and a client that cannot tell
    them apart tells a team to fix a task that never ran.

    A typo on either side is silent in the register's usual way: nothing fails, the sentence simply
    never prints, and the reader is back to reading `timed_out` and guessing which kind it was.
    """
    from cli import remote_task

    assert remote_task.QUEUE_EXPIRED == _relay_string_constant(
        "QUEUE_EXPIRED", module="task_reaper.py")


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

    A sibling of `_relay_string_constant` rather than a widening of it: that one reads a
    module-level assignment, and this one reads the body of a function, because not every value in
    the cross-repo register is spelled as a named constant on both sides. Kept separate so
    "I could not find the constant" and "I could not find the function" stay different failures.

    The docstring is collected too, and is deliberately NOT filtered out. Two of the three functions
    read through here name their own wire value in their prose — `refuse_if_trunk_exists` opens with
    "a **409 `project_already_has_trunk`**", and `_bearer_or_api_key` describes the `"Bearer "`
    prefix it strips — which looks at first like a way for these checks to pass on English after the
    real literal has drifted. It is not: every caller asserts `value in <this set>`, which is an
    EXACT element match, and the docstring arrives as one 1,367-character element. Measured by
    renaming the code literal in a copy of grid-src's source: the assertion goes red with the
    docstring counted exactly as it does without it. Filtering it would be inert code justified by a
    hazard that does not exist.
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


def _relay_function_params(name, module):
    """Every parameter NAME of one of grid-src's functions (ADR 0033 D-p, issue 33).

    The third sibling beside `_relay_string_constant` and `_relay_function_strings`, and it exists
    because a FastAPI query parameter's wire name is its **argument name** — there is no string
    literal on the relay side to read. A body scan of `list_projects` finds `"false"`, the `Query`
    default, and would happily pass while `include_archived` had been renamed on the wire.

    Keyword-only, positional and ordinary arguments all counted, because which of the three a route
    declares is FastAPI's business and not a thing this check should have an opinion about.
    """
    import ast

    source = _relay_module(module)
    if not source.exists():
        pytest.skip("grid-src worktree is not beside this one; the lockstep cannot be checked here")
    for node in ast.walk(ast.parse(source.read_text())):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            spec = node.args
            return {arg.arg for arg in
                    (*spec.posonlyargs, *spec.args, *spec.kwonlyargs)}
    raise AssertionError(f"{name} is no longer defined in grid-src's {module}")


def test_the_queue_budget_stays_longer_than_the_run_budget():
    """The inequality, not just the two names (ADR 0033 D-k, issue 18).

        run deadline  <  queue deadline

    A queue budget SHORTER than a run budget reintroduces the bug this slice removes, in miniature:
    a task would be given up on for waiting sooner than one is given up on for working, so a fleet
    only slightly behind demand would reap tasks whose agents would have finished comfortably.

    ⚠️ **This guards the DEFAULTS in source, and nothing more.** `_relay_config_constant` parses the
    second argument of the `os.getenv` call, so what it sees is what grid-src ships — never what an
    operator sets via `TASK_QUEUE_DEADLINE_SECONDS`. It also SKIPS whenever the grid-src worktree is
    not beside this one, which includes CI (`.github/workflows/ci.yml` checks out one repository).
    The runtime enforcement is therefore grid-src's own `config.validate_task_budgets`, which
    refuses to boot; this test is the cheap guard against the shipped pair drifting.
    """
    run = _relay_config_constant("task_deadline_seconds")
    queue = _relay_config_constant("task_queue_deadline_seconds")

    assert run < queue, (
        f"the relay gives a task {queue}s to find a provider but {run}s to run — waiting is bounded "
        f"more tightly than working, so a backlog is reaped before it is served")


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


def _relay_module(name):
    """One of grid-src's private_server modules, as a path. Three helpers below read constants out
    of that package, and the worktree location was written out at each of them — so a moved
    checkout meant three edits, and a missed one is a lockstep check that skips instead of failing.

    Machine-specific on purpose, like the value it replaces: the two repositories are separate
    installs with no import path between them, and every caller here skips rather than fails when it
    is absent (`GRID_SRC_REPO` is the cross-repo E2E's override; this side has deliberately never
    needed one, because a developer without the worktree simply cannot check the duplicates).
    """
    import pathlib

    return pathlib.Path(
        "/Users/macbookpro/Projects/grid-src-feats/distributed-tasks"
        "/grid_cli/private_server") / name


def _relay_config_constant(name):
    """A default out of grid-src's `config.py`, parsed rather than imported. See `_relay_constant`."""
    import ast

    source = _relay_module("config.py")
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

    source = _relay_module(module)
    if not source.exists():
        pytest.skip("grid-src worktree is not beside this one; the lockstep cannot be checked here")
    for node in ast.parse(source.read_text()).body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == name for t in node.targets):
            return _numeric(node.value, name)
    raise AssertionError(f"{name} is no longer defined in grid-src's tasks.py")


def _relay_string_constant(name, module):
    """A STRING constant out of a grid-src module, parsed rather than imported.

    A sibling of `_relay_constant` rather than a widening of it: that one funnels through `_numeric`,
    which refuses anything that is not an integer expression precisely so a constant it cannot read
    is an error instead of a silently skipped check. Teaching it to also accept strings would make
    "this is not a number" and "this is a string I understand" the same answer.
    """
    import ast

    source = _relay_module(module)
    if not source.exists():
        pytest.skip("grid-src worktree is not beside this one; the lockstep cannot be checked here")
    for node in ast.parse(source.read_text()).body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == name for t in node.targets):
            value = node.value
            assert isinstance(value, ast.Constant) and isinstance(value.value, str), (
                f"grid-src's {name} is no longer a plain string literal, so this lockstep check "
                f"cannot read it — teach this helper the new shape rather than deleting the check")
            return value.value
    raise AssertionError(f"{name} is no longer defined in grid-src's {module}")


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


def test_the_archived_keys_this_cli_reads_are_the_ones_the_relay_writes():
    """The wire values ADR 0033 D-p (issue 33) hand-duplicates across the two repos.

    Three of them, and each fails in a different, silent direction:

      * **`archived`** on the project view and on `/status` — the CLI keys on an explicit `True`,
        so a relay that renamed it would simply stop marking archived projects. `grid project list`
        would then show a project that takes no work as though it were live, and the member would
        find out from a refused `task create`.
      * **`include_archived`** — the query parameter `--all` sends. A rename makes the flag a silent
        no-op: the command exits 0, prints the unarchived listing, and the archived project the
        member was looking for is simply not there.
      * **`archived_at`** — the row the boolean is derived FROM. Parsed as the column name rather
        than the wire key, so the two halves of the derivation cannot drift apart.

    None of them raises anywhere. That is exactly why they are pinned here: every unit test on this
    side reads a reply this repository wrote down, so a relay that renamed one leaves both suites
    green and the feature quietly half-working.
    """
    # `project_view`, public since ADR 0034 D-k (issue 36) — `project_visibility` answers with the
    # same shape, so a second hand-written copy of it would be the drift this check exists to catch.
    # The rename is what made THIS test go red, which is the helper doing its job: it reads a name
    # out of grid-src rather than trusting one.
    view = _relay_function_strings("project_view", module="projects.py")
    assert "archived" in view, (
        "grid-src's `project_view` no longer carries `archived`, so `grid project list` would "
        "show an archived project as though it were live")

    # The PARAMETER names, not the body's string literals: a query parameter's wire name is its
    # argument name in FastAPI, so a scan of the body would pass while the name on the wire changed.
    # (`_relay_function_strings` would find "false", the Query default, and prove nothing.)
    assert "include_archived" in _relay_function_params("list_projects", module="projects.py"), (
        "grid-src's `list_projects` no longer reads `include_archived`, so `grid project list "
        "--all` is a silent no-op that hides the projects it was asked to show")

    status = _relay_function_strings("project_status", module="project_status.py")
    assert "archived" in status, (
        "grid-src's `/projects/{id}/status` no longer carries `archived`, so `grid project status` "
        "cannot say why a member's next task will be refused")


def test_the_archive_refusal_codes_are_the_ones_the_relay_sends():
    """Two codes this CLI does NOT branch on, pinned anyway — and the reason is worth stating.

    `project_archived` and `project_not_empty` are **displayed verbatim** (the `task.retry` rule),
    because each relay message already names the command that fixes it. So a rename is a display
    change, not a behaviour change, and nothing here would break.

    What this catches instead is the messages losing their REMEDY. `grid project archive` is the
    only way out of `project_archived`, and `grid project archive` is the only answer to
    `project_not_empty` — if either sentence stopped naming it, a member would be refused with no
    way forward and no test in either repository would notice, because the CLI is only passing the
    words through.
    """
    guard = _relay_function_strings("refuse_if_archived", module="project_writable.py")
    assert "project_archived" in guard, "grid-src's write guard no longer sends `project_archived`"
    assert any("unarchive" in value for value in guard), (
        "grid-src's archived refusal no longer names `grid project unarchive`, so a member is "
        "refused with no way forward")

    empty = _relay_function_strings("_refuse_if_not_empty", module="project_archive.py")
    assert "project_not_empty" in empty, (
        "grid-src's delete guard no longer sends `project_not_empty`")
    assert any("archive" in value for value in empty), (
        "grid-src's delete refusal no longer names `grid project archive`, which is the only thing "
        "a member refused for a non-empty project can do instead")


def test_the_visibility_values_this_cli_sends_are_the_ones_the_relay_accepts():
    """The wire values ADR 0034 D-k (issue 36) hand-duplicates across the two repositories.

    `grid project private` posts `{"visibility": "private"}` and the relay's `_visibility` refuses
    anything outside its own closed set — so a rename on either side turns the command into a **422
    on every invocation**. Loud, which is the good direction; pinned anyway because the READING side
    fails silently: `grid project list` and `grid project status` compare against `private` to mark a
    project, and a rename there simply stops marking one. A member would learn that their project is
    still shared by finding out a colleague can read it.

    ⚠️ Read as CONSTANTS, not as literals in the validator's body. The first draft of this test
    scanned `project_visibility._visibility` for its strings and went red immediately — that
    function validates against `project_access.VISIBILITIES` and contains no value at all, so a
    body scan would have proved nothing about the wire and everything about the relay's spelling
    habits. The constants are what both routes and both readings resolve through.
    """
    from cli import project_visibility

    assert project_visibility.VISIBILITY_PRIVATE == _relay_string_constant(
        "VISIBILITY_PRIVATE", module="project_access.py"), (
        "grid-src no longer accepts the value `grid project private` sends")
    assert project_visibility.VISIBILITY_GRID == _relay_string_constant(
        "VISIBILITY_GRID", module="project_access.py"), (
        "grid-src no longer accepts the value `grid project share` sends")


def test_the_visibility_key_this_cli_reads_is_the_one_the_relay_writes():
    """`visibility` on the project view and on `/status`, and it raises nowhere.

    That is exactly why it is pinned here: every unit test on this side reads a reply this
    repository wrote down, so a relay that renamed the key would leave both suites green and the
    marking quietly absent. The same argument `archived` carries two tests up.
    """
    view = _relay_function_strings("project_view", module="projects.py")
    assert "visibility" in view, (
        "grid-src's `project_view` no longer carries `visibility`, so `grid project list` would "
        "show a private project as though anyone on the grid could read it")

    # `grid_access` beside it: the setting and its EFFECT are two facts, and a CLI reading only the
    # first prints "shared with everyone" about a project nobody but its members can reach. It
    # raises nowhere and the CLI keys on an explicit `False`, so a rename is silent in exactly the
    # direction that matters — `share` over-claims, `private` stays accurate.
    assert "grid_access" in view, (
        "grid-src's `project_view` no longer carries `grid_access`, so `grid project share` would "
        "claim a widening on a relay that does not serve the rule at all")

    status = _relay_function_strings("project_status", module="project_status.py")
    assert "grid_access" in status, (
        "grid-src's `/projects/{id}/status` no longer carries `grid_access`")
    assert "visibility" in status, (
        "grid-src's `/projects/{id}/status` no longer carries `visibility`, so `grid project "
        "status` cannot tell a member their project is restricted")


def test_the_rename_route_this_cli_posts_to_is_the_one_the_relay_serves():
    """ADR 0035 D-a (issue 55): `POST /relay/v1/projects/{project_id}/name`.

    The path is most of the contract here, and a drift is silent in the worst direction — the same
    one issues 45 and 47 record: this CLI would post to a path the relay does not serve, get
    FastAPI's bare 404, and `missing_route_hint` would turn it into *"ask your operator to update
    the relay"* about a relay that is perfectly up to date. Worse here than elsewhere, because that
    sentence then steers somebody towards `grid project create --name <new>` — create-or-get BY
    name, which hands them a second, empty project and leaves their work in the first. A typo in
    this repository would deliver the exact failure the command exists to prevent.
    """
    from remote import relay

    assert _client_paths(relay.rename_project) <= _served("project_rename.py", "{project_id}"), (
        f"this CLI renames at {sorted(_client_paths(relay.rename_project))}, which grid-src's "
        f"project_rename.py does not serve")


def test_the_rename_reply_still_carries_what_the_project_used_to_be_called():
    """`previous_name`, and it raises **nowhere** — which is exactly why it is pinned here.

    It is the only way this CLI can know the old name: the caller typed an id and a new name, and
    nothing else on this side has ever seen the project. Drop it on the relay and
    `grid project rename` silently stops warning that it just renamed somebody's `default` project
    away — after which their next `grid task create` with no `--project` refuses, and nothing
    anywhere connects the two. Both suites stay green.

    Read out of the route's own strings rather than restated, and paired with the KEY this CLI
    reads, so the two spellings cannot drift apart.
    """
    from cli import project_rename  # noqa: F401  — the reader whose behaviour this protects

    route = _relay_function_strings("_view", module="project_rename.py")
    assert "previous_name" in route, (
        "grid-src's rename route no longer answers with `previous_name`, so `grid project rename` "
        "cannot warn that it renamed the caller's `default` project away — the ADR 0035 D-g "
        "failure, arriving with nothing red in either repository")


def test_the_relay_normalises_a_project_name_the_way_this_cli_expects():
    """⚠️ The one hand-duplicated NORMALISATION on this seam, and it is invisible from both sides.

    grid-src's `projects.requested_name` strips the name before storing and echoing it, so a caller
    who typed `--name "acme "` gets back `acme`. `grid project rename` compares the echo against
    what it asked for in order to decide whether the rename happened — so the two have to agree
    about what "what it asked for" means, and `cli/project_rename._as_the_relay_will_store_it` is
    this side's copy of that rule.

    Both directions of drift are bad, and neither raises anywhere:

      * the relay normalising **more** (case-folding, collapsing inner whitespace) makes every such
        rename report a FAILURE for a rename that landed — and the refusal's remedy re-runs the same
        request, so it never resolves. The person's next move is `grid project create --name <new>`,
        create-or-get BY NAME, which is the second-empty-project fork this command exists to remove.
      * the relay normalising **less** makes this CLI's `default` warning fire on a name the relay
        did not store as `default`, and stay silent on one it did.

    Read out of grid-src's validator rather than restated. The shape is asserted — `.strip()` and
    nothing else touching the value — because what must be pinned is the TRANSFORMATION, and there
    is no constant to compare.
    """
    import ast

    from cli import project_rename

    source = _relay_module("projects.py")
    if not source.exists():
        pytest.skip("grid-src worktree is not beside this one; the lockstep cannot be checked here")

    tree = ast.parse(source.read_text())
    validator = next((n for n in ast.walk(tree)
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                      and n.name == "requested_name"), None)
    assert validator is not None, (
        "grid-src's `projects.requested_name` is gone — it was `_name` before ADR 0035 D-a made the "
        "rename its second consumer. Whatever replaced it is what this CLI's postcondition has to "
        "agree with")

    body = ast.unparse(validator)
    # The transformation applied to the value that is STORED. `.strip()` and nothing else: a second
    # call here (`.lower()`, `.replace(...)`) is exactly the "normalises more" drift above.
    assert "raw.strip()" in body, (
        "grid-src no longer normalises a project name with `raw.strip()`, so "
        "`cli/project_rename._as_the_relay_will_store_it` is now a different rule from the relay's "
        "— every rename whose name needs normalising would be reported as a failure that landed")
    for extra in (".lower()", ".upper()", ".casefold()", ".title()"):
        assert extra not in body, (
            f"grid-src's project-name validator now also applies `{extra}`, which this CLI does "
            f"not — teach `_as_the_relay_will_store_it` the same rule, or the postcondition "
            f"reports a landed rename as a failure")

    # And this side really does apply it, rather than having quietly become the identity function.
    assert project_rename._as_the_relay_will_store_it("  acme  ") == "acme"


def test_the_rename_refusal_still_names_the_way_forward():
    """`project_name_taken` is **displayed verbatim** — deliberately not a fourth parsed code
    (ADR 0035 D-g), because exactly three are read across the two repositories and keeping the count
    that low is itself the contract.

    So a rename of the code is a display change and nothing breaks. What this catches instead is the
    message losing its REMEDY: the sentence is the whole of what a person gets, and if it stopped
    naming a command they can run they would be refused with no way to find out which of their own
    projects is holding the name. Nothing on this side would notice, because the CLI is only passing
    the words through — the `project_archived` / `project_not_empty` rule, applied to a third
    refusal.
    """
    taken = _relay_function_strings("_taken", module="project_rename.py")
    assert "project_name_taken" in taken, (
        "grid-src's rename no longer sends `project_name_taken`")
    assert any("grid project list" in value for value in taken), (
        "grid-src's taken-name refusal no longer names a command, so somebody refused for a "
        "collision has no way to find the project that is holding the name")


def test_the_relay_still_refuses_a_rename_from_anybody_but_the_owner():
    """Owner-only is a property of the NAME, not politeness, and the damage lands on THIS side.

    `idx_projects_owner_name` is `(owner_id, name)`, so a name lives in the owner's namespace and
    other people read it. Since ADR 0034 D-k every authenticated caller on the grid is auto-minted
    as a member of a non-private project — so a relay that relaxed this gate to "any member" would
    let anyone on the grid rename any team's project, and `grid project rename --help` in this
    repository still promises otherwise.

    Parsed out of the route rather than restated, so the promise here and the rule over there cannot
    drift apart silently.
    """
    import ast

    source = _relay_module("project_rename.py")
    if not source.exists():
        pytest.skip("grid-src worktree is not beside this one; the lockstep cannot be checked here")

    tree = ast.parse(source.read_text())
    route = next((n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == "rename_project"), None)
    assert route is not None, (
        "grid-src's project_rename.py no longer has a `rename_project` route, so this check reads "
        "nothing")

    body = ast.unparse(route)
    assert "require_owner" in body, (
        "the rename route no longer goes through `require_owner`. Under ADR 0034 D-k every "
        "authenticated caller on the grid reaches a non-private project, so without it any of them "
        "can rename any team's project")
    assert "refuse_if_archived" in body, (
        "the rename route no longer refuses an archived project (ADR 0035 D-f), and "
        "`grid project rename --help` in this repository still says it does")


def test_the_leave_route_this_cli_posts_to_is_the_one_the_relay_serves():
    """ADR 0035 D-b (issue 56): `POST /relay/v1/projects/{project_id}/leave`.

    The path is the whole contract here — there is no body and no `member_key`, which is the point
    of the route — and a drift is silent in the worst direction, the one issues 45 and 47 record:
    this CLI would post to a path the relay does not serve, get FastAPI's bare 404, and
    `missing_route_hint` would turn it into `_OLD_RELAY_NO_LEAVE` about a relay that is perfectly up
    to date. That sentence tells the member *you are still a member* and sends them to the project's
    owner — so a typo in this repository would deliver, in full sincerity, the exact dead end this
    command was written to remove.
    """
    from remote import relay

    assert _client_paths(relay.leave_project) <= _served("project_leave.py", "{project_id}"), (
        f"this CLI leaves at {sorted(_client_paths(relay.leave_project))}, which grid-src's "
        f"project_leave.py does not serve")


def test_the_leave_reply_still_says_the_caller_left():
    """`left`, and it raises **nowhere** — which is why it is pinned here.

    `cli/project_leave.py` refuses to report a departure the relay did not confirm, keyed on
    `answer.get("left") is not True`. Drop the key on the relay and every landed leave is reported
    as a FAILURE — the member is told they may still be a member of a project they have just left,
    and the remedy the refusal names (`grid project list`) agrees with the relay rather than with
    the CLI, so nothing anywhere connects the two. Both suites stay green.

    Read out of the route's own strings rather than restated, and paired with the reader whose
    behaviour it protects, so the two spellings cannot drift apart.
    """
    from cli import project_leave  # noqa: F401  — the reader whose behaviour this protects

    route = _relay_function_strings("leave_project", module="project_leave.py")
    assert "left" in route, (
        "grid-src's leave route no longer answers with `left`, so `grid project leave` refuses "
        "every successful departure as an answer it cannot read")


def test_the_two_removal_routes_still_share_one_implementation():
    """⚠️ **Issue 56's first acceptance criterion, checked from this side**, because this is the
    repository that pays for it being wrong.

    `DELETE …/members/{member_key}` and `POST …/leave` mean the same thing to a project and differ
    only in who may ask. Two spellings of "remove a member" is the two-authorization-models failure
    this plane keeps warning about, and the grid-visible refusal is the one nobody would think to
    copy: on a grid-visible project the membership row is not what grants access, so removing it
    revokes nothing and the next request mints it straight back with an identical key.

    A copy that lost that refusal answers **200** to `grid project leave`, and this CLI would print
    "You have left project P1" over a departure that did not happen — a reply that says what the
    caller asked for is not evidence the caller got it. Nothing here can see that, which is exactly
    why the structure is pinned rather than the behaviour.
    """
    import ast

    source = _relay_module("project_leave.py")
    if not source.exists():
        pytest.skip("grid-src worktree is not beside this one; the lockstep cannot be checked here")

    route = next((n for n in ast.walk(ast.parse(source.read_text()))
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == "leave_project"), None)
    assert route is not None, (
        "grid-src's project_leave.py no longer has a `leave_project` route, so this check reads "
        "nothing")

    body = ast.unparse(route)
    assert "remove_membership" in body, (
        "grid-src's leave route no longer calls the shared `projects.remove_membership`, so this "
        "plane now has two opinions about what removing a member means — and the one this CLI "
        "reaches is the copy, which is where the grid-visible refusal goes missing first")
    assert "refuse_unless_reachable" in body, (
        "grid-src's leave route no longer runs the project-shaped 404 first, so it can tell a "
        "stranger whether a project id is real — on the one route in this plane that needs no "
        "setup at all to call")
    # ⚠️ The ABSENCE, and it is a decision rather than an oversight (ADR 0035 D-f). The rule that
    # recruits a route into `project_writable` — *does it move a ref or write a row?* — would take
    # `leave`, and it has already pulled in two routes nobody thought of. Refusing it would trap a
    # member in a project nobody is working in and make them ask its owner to REOPEN the project
    # purely so they could walk away from it. `grid project leave --help` here promises otherwise.
    assert "refuse_if_archived" not in body, (
        "grid-src's leave route now refuses an archived project, which ADR 0035 D-f decides "
        "against — a member would have to ask the owner to unarchive a project in order to leave "
        "it, and `grid project leave --help` in this repository says archiving does not stop them")


def test_a_project_you_are_not_a_member_of_can_never_read_as_yours():
    """⚠️ **The load-bearing relation in issue 36, and it is between two of the relay's OWN
    constants** — so this is the one lockstep check whose two halves both live over there.

    `GET /relay/v1/projects` now lists projects the caller has no membership row in, and reports
    `role` as `project_access.GRID_ROLE` for them. `cli/remote_task._own_default_project` filters
    that listing on `role == "owner"` to decide which project a `grid task create` with no
    `--project` means — and a project NAME is unique per OWNER rather than per grid, so on any real
    grid several people have a `default`.

    The day those two strings collided, every forgotten `--project` would run somebody's task in a
    colleague's project, silently. Nothing else in either repository would notice: the create
    succeeds, the task runs, the work lands — in the wrong history.

    Asserted from this side because this is where the damage lands, and `test_project_visibility.py`
    asserts it over there too because a cross-repo check SKIPS whenever grid-src is not beside this
    worktree, which is every CI run.
    """
    from cli import remote_task

    owner = _relay_string_constant("OWNER_ROLE", module="project_members.py")
    grid = _relay_string_constant("GRID_ROLE", module="project_access.py")

    assert owner != grid, (
        "grid-src reports the same `role` for a project you own and one you merely reach through "
        "the grid, so a projectless `grid task create` would resolve `default` to a colleague's "
        "project")
    assert remote_task._OWNER_ROLE == owner, (
        "this CLI's owner filter no longer matches the role grid-src writes")
    assert remote_task._OWNER_ROLE != grid


def test_the_undo_route_this_cli_posts_to_is_the_one_the_relay_serves():
    """ADR 0034 D-l (issue 44): `POST /relay/v1/tasks/{task_id}/undo`.

    The path is the whole of the contract — there is no body at all — and a drift is SILENT in the
    worst direction, exactly as `send_turn`'s is: this CLI would post to a path the relay does not
    serve, get FastAPI's bare 404, and `missing_route_hint` would turn it into "ask your operator to
    update the relay" about a relay that is perfectly up to date. The member is then sent to chase an
    operator over a typo in this repository, about a change still sitting in their project.

    ⚠️ **`{task_id}`, not `{conversation_id}`.** The same `/tasks/{id}/…` prefix addresses a TURN for
    `undo`, `cancel`, `lease`, `result` and `events`, and a CONVERSATION for `turns`, `commit` and
    `stream`. Both spellings are literals in one relay module apiece, so this comparison is what
    stops the two being fused by a rename.
    """
    import ast
    import inspect

    from remote import relay

    sent = [
        "".join(part.value if isinstance(part, ast.Constant) else "{}" for part in node.values)
        for node in ast.walk(ast.parse(inspect.getsource(relay.undo_task)))
        if isinstance(node, ast.JoinedStr)
    ]
    assert sent, "undo_task no longer builds its path from an f-string; teach this check the new one"

    served = {"/relay/v1" + path.replace("{task_id}", "{}")
              for path in _relay_route_paths("task_undo.py")}

    assert set(sent) <= served, (
        f"this CLI posts an undo to {sorted(set(sent) - served)}, which grid-src's task_undo.py "
        f"does not serve — every undo would get a bare 404 and be reported as 'your relay is too "
        f"old', about a change that is still in the project")


def test_the_relay_still_refuses_an_undo_from_anybody_but_the_two_owners():
    """ADR 0034 D-l's authorization, asserted from THIS side because it is what the CLI's surface
    promises: `grid task undo --help` tells a member only they and the project's owner can do it.

    Parsed out of grid-src's route rather than restated, so the sentence in this repository's help
    text and the rule in the other cannot drift apart silently. Since ADR 0034 D-k opened every
    non-private project to every caller on the grid, a relay that dropped this check would let any
    of them rewrite any team's trunk — and nothing on this side would notice.
    """
    import ast

    source = _relay_module("task_undo.py")
    if not source.exists():
        pytest.skip("grid-src worktree is not beside this one; the lockstep cannot be checked here")

    tree = ast.parse(source.read_text())
    route = next((n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == "undo_turn"), None)
    assert route is not None, (
        "grid-src's task_undo.py no longer has an `undo_turn` route, so this check reads nothing")

    body = ast.unparse(route)
    assert "turn.owner_id" in body and "project.owner_id" in body, (
        "the undo route no longer compares the caller against BOTH the turn's owner and the "
        "project's owner. Under ADR 0034 D-k every authenticated caller on the grid can reach a "
        "non-private project, so without this any of them can undo any turn in it")
    assert "not_yours_to_undo" in body, (
        "the refusal code changed; `grid task undo --help` in this repository still promises the "
        "rule it named")


# --------------------------------------------------------------------------------------------
# ADR 0034 D-m (issue 45) — the read plane: files, one file, one turn's diff, and a download.
# --------------------------------------------------------------------------------------------


def _client_paths(function):
    """Every path a relay-client function builds, with its `{...}` slots blanked.

    Read out of the function's own f-strings rather than restated, so what is compared is what the
    request is actually built from — `test_the_follow_up_route_…`'s method, extracted here because
    issue 45 needs it four times.
    """
    import ast
    import inspect

    built = [
        "".join(part.value if isinstance(part, ast.Constant) else "{}" for part in node.values)
        for node in ast.walk(ast.parse(inspect.getsource(function)))
        if isinstance(node, ast.JoinedStr)
    ]
    # Narrowed to the ones that are actually RELAY PATHS. A function may build other f-strings —
    # `download_project` builds a temp-file prefix and a transport-error sentence — and comparing
    # those against a route table would fail for a reason that has nothing to do with the contract.
    # The prefix is the honest discriminator: it is what makes a string a path on this plane, and a
    # client that stopped spelling it is exactly what the comparison below is looking for.
    paths = {value for value in built if value.startswith("/relay/")}
    assert paths, (
        f"{function.__name__} no longer builds a /relay/… path from an f-string; teach this check "
        f"the new spelling rather than deleting it")
    return paths


def _served(module, *slots):
    """grid-src's declared paths for `module`, prefixed and with `slots` blanked.

    The `/relay/v1` prefix lives on the `APIRouter` rather than on each decorator, so it is added
    back here instead of stripped off the client's — asserting the CLIENT still spells it is part of
    what this compares, since the prefix is a third hand-kept copy (grid-apis has one too).
    """
    served = set()
    for path in _relay_route_paths(module):
        for slot in slots:
            path = path.replace(slot, "{}")
        served.add("/relay/v1" + path)
    return served


def test_the_three_project_reads_this_cli_asks_for_are_the_ones_the_relay_serves():
    """ADR 0034 D-m (issue 45): `…/files`, `…/file` and `…/download`.

    The path is the whole contract for all three — no body, one query parameter — and a drift is
    silent in the worst direction, exactly as issue 47's is: this CLI would get FastAPI's bare 404
    and `missing_route_hint` would turn it into *"ask your operator to update the relay"* about a
    relay that is perfectly up to date, sending somebody to chase an operator over a typo in this
    repository.
    """
    from remote import relay

    served = _served("project_files.py", "{project_id}") | _served(
        "project_download.py", "{project_id}")

    for function in (relay.project_files, relay.project_file, relay.download_project):
        asked = _client_paths(function)
        assert asked <= served, (
            f"{function.__name__} asks for {sorted(asked - served)}, which grid-src does not "
            f"serve — every call would get a bare 404 and be reported as 'your relay is too old'")


def test_the_turn_diff_this_cli_asks_for_addresses_a_TURN_on_the_relay_too():
    """⚠️ `{task_id}` is a TURN on `/diff`, where the SAME segment is a CONVERSATION on `/turns`,
    `/commit` and `/stream`.

    That is issue 44's trap one route along, and the reason this is its own test rather than a
    fourth entry above: the two objects share a path prefix, so a drift here is not a 404 but a
    request about the wrong object — and grid-src's own route is what says which it means.
    """
    from remote import relay

    served = _served("turn_diff.py", "{task_id}")
    asked = _client_paths(relay.turn_diff)

    assert asked <= served, (
        f"`grid task diff` asks for {sorted(asked - served)}, which grid-src's turn_diff.py does "
        f"not serve")
    # And the relay really does spell it `task_id` — a rename there to `conversation_id` would keep
    # this CLI's path working while changing what the id MEANS, which no path comparison can see.
    assert any("{task_id}" in path for path in _relay_route_paths("turn_diff.py")), (
        "grid-src's diff route no longer addresses a TURN. If it now addresses a conversation, this "
        "CLI is sending the wrong id and every diff is about somebody's whole conversation or "
        "about nothing")


def test_this_clis_download_timeout_stays_above_the_relays_own_ceiling():
    """The third timeout pair in this file, and it faces the same way as the import one.

    A client that gives up before the relay does turns a refusal the relay was about to make into
    "the connection died" — and nothing on either side can then say which happened. The relay's
    figure is PARSED rather than restated, so raising one without the other fails here instead of in
    a fleet where large downloads mysteriously stop working.
    """
    import ast

    from remote import relay

    source = _relay_module("project_download.py")
    if not source.exists():
        pytest.skip("grid-src worktree is not beside this one; the lockstep cannot be checked here")
    relay_ceiling = None
    for node in ast.parse(source.read_text()).body:
        if isinstance(node, ast.Assign) and any(
                getattr(target, "id", None) == "ARCHIVE_TIMEOUT_SECONDS" for target in node.targets):
            relay_ceiling = node.value.value
    assert isinstance(relay_ceiling, (int, float)), (
        "grid-src's project_download.ARCHIVE_TIMEOUT_SECONDS is no longer a literal at module "
        "scope, so this pair cannot be checked from here")

    assert relay._DOWNLOAD_TIMEOUT > relay_ceiling, (
        f"this CLI gives up on a download after {relay._DOWNLOAD_TIMEOUT}s while the relay is "
        f"willing to spend {relay_ceiling}s packing it — so a large project fails client-side "
        f"while the relay does the work anyway, and the log blames a timeout the relay never saw")


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


def test_the_relay_still_accepts_a_task_list_with_no_project():
    """ADR 0034 D-m (issue 46). `grid task list` with no `--project` OMITS the parameter, and the
    relay has to read that as "every project you can reach" rather than refusing it.

    ⚠️ **This is the one direction that fails LOUDLY, and the test exists to keep it that way.** An
    older relay answers a 422 carrying `invalid_request`, which the CLI shows verbatim — never an
    empty list, and never somebody's `default` project standing in for "everywhere". What would be
    silent is the reverse drift: `_project_id` going back to raising on a missing value while this
    CLI keeps omitting it, so a person's home screen refuses on a relay that has *already* been
    rolled out. Nothing else in either repository compares the two halves.

    Parsed rather than requested, like every other check in this module: the two repositories share
    no code and there is no relay running here.
    """
    import ast
    import inspect

    from remote import relay

    # This side: `list_tasks` must OMIT the parameter, never send it empty. A blank `project_id` is
    # a different request from no `project_id` against any relay that validates it.
    sending = ast.parse(inspect.getsource(relay.list_tasks))
    guards = [node for node in ast.walk(sending)
              if isinstance(node, ast.Compare) and isinstance(node.ops[0], ast.IsNot)]
    assert guards, (
        "`relay.list_tasks` no longer decides whether to send `project_id` at all, so a projectless "
        "`grid task list` sends an empty one — which is a project id to any relay that checks")

    # The relay's side: `_project_id` must have a path that RETURNS for a missing value rather than
    # raising on it. `task_errors.invalid` is still reachable there — the length ceiling — so the
    # test is about the early return, not about the absence of a refusal.
    source = _relay_module("task_list.py")
    if not source.exists():
        pytest.skip("grid-src worktree is not beside this one; the lockstep cannot be checked here")
    function = next(
        (node for node in ast.walk(ast.parse(source.read_text()))
         if isinstance(node, ast.FunctionDef) and node.name == "_project_id"), None)
    assert function is not None, (
        "grid-src's `task_list._project_id` was renamed; teach this check its new name")
    returns_none = [node for node in ast.walk(function)
                    if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant)
                    and node.value.value is None]
    assert returns_none, (
        "grid-src's `task_list._project_id` no longer answers a MISSING project with `None`, so "
        "`grid task list` with no --project is refused — a person's home screen, against a relay "
        "that has already been rolled out")
    assert "str | None" in ast.unparse(function.returns or ast.Constant(value="")), (
        "grid-src's `task_list._project_id` no longer declares that it may answer None; the "
        "cross-project listing is the only reason it does")
