"""Local-engine reachability, carried on the heartbeat (ADR 0019 / grid-leave issue 07).

The bug this closes: the serve child heartbeats every 30s and never asks the engine behind it
whether it is still there, so a dead ``llama-server`` leaves a live child advertising its models —
``grid models`` lists them, the relay routes to them, and every job fails at forward time. On a busy
grid the relay's reactive demotion eventually notices; on an **idle** grid there are no failures to
count, so the advertisement stays wrong indefinitely.

The fix is a **forbidden list**: the heartbeat's ``load`` gains ``unhealthy_models`` — the advertised
names this box currently cannot serve. Absent means nothing is withheld, which is what makes every
version skew safe (an old CLI never sends it; an old master stores and ignores it; a probe that has
not run, raised, or ran out of budget emits nothing). Capacity is only ever withdrawn by a probe
that positively observed a transport failure, ``UNHEALTHY_AFTER`` times in a row.

What "unreachable" claims is deliberately narrow — the TCP/HTTP conversation failed. Any HTTP
status, 404 included, proves a server is listening and counts as healthy: the observable that
motivated this is a *dead process*, and every widening adds a way to withdraw capacity that is
actually fine. An engine whose server is up but whose model failed to load therefore still
advertises; that gap is the reactive per-(node, model) prune's, by design.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Mapping, NamedTuple

import httpx

# The heartbeat ``load`` key. **Hand-duplicated** with grid-src's
# ``engine_health.HEALTH_LOAD_KEY`` and kept in lockstep by editing both repos — there is no
# compile-time link between the copies (see CLAUDE.md's lockstep register). Absent on either side
# means "nothing withheld", so a mismatch degrades to today's behaviour rather than breaking.
UNHEALTHY_LOAD_KEY = "unhealthy_models"

# Per-probe wall clock, with the phases stated EXPLICITLY rather than as a bare float. httpx expands
# a bare float to all four phases independently, so a nominal "3 seconds" really means a slow connect
# *and then* a slow read — about 6s for one call. That matters twice over: the probe rides the
# heartbeat thread (which must not be delayed into the relay's 120s node TTL), and a probe already in
# flight when a leave lands still has to return before the heartbeat thread can be joined. So one
# call's realistic worst case (connect + read) is kept BELOW `serve._DRAIN_TIMEOUT`, or a single slow
# engine could spend the whole teardown budget and starve the reload daemon's join (ADR 0010 C5).
# The two constants live in different modules; a test pins the relationship.
PROBE_TIMEOUT = httpx.Timeout(connect=1.0, read=2.0, write=1.0, pool=1.0)

# Consecutive failed probes before a URL's models are withdrawn. Asymmetric on purpose — a
# withdrawal removes capacity from a working grid, a restoration only re-adds it, so the expensive
# direction is debounced and recovery is immediate on the first success.
UNHEALTHY_AFTER = 2

# Total wall clock one sweep may spend. The sweep rides the heartbeat thread, so N slow engines
# would delay the NEXT heartbeat by N x PROBE_TIMEOUT — and a large enough union (repeated
# `--api`/`--all` appends) reaches the relay's 120s node TTL, which unlists everything this box
# serves. That is a strictly worse outage than the stale advertisement this feature fixes, so the
# sweep is bounded and the engines it did not reach keep their previous verdict.
PROBE_BUDGET = 10.0

# Monotonic clock seam, so the budget is testable without real sleeps (grid-src's `_breaker_clock`
# is the same shape). Module-level rather than a parameter: `_maybe_probe_engines` should not have
# to thread a clock it never varies.
_clock = time.monotonic


@dataclass(frozen=True)
class EngineHealth:
    """Consecutive failed probes per engine URL. Immutable: ``sweep`` returns a new one."""

    failures: Mapping[str, int] = field(default_factory=dict)

    def unreachable(self, url: str) -> bool:
        return self.failures.get(url, 0) >= UNHEALTHY_AFTER


class SweepResult(NamedTuple):
    """One probe round: the new verdict, plus every engine it did not manage to check.

    Two distinct reasons, kept apart because they call for different operator actions: ``skipped``
    ran out of budget (transient, will be retried first next round), ``errors`` blew up in the probe
    itself (a malformed URL, a bug — it will keep blowing up until someone looks).
    """

    health: EngineHealth
    skipped: tuple[str, ...] = ()
    errors: tuple[tuple[str, str], ...] = ()  # (url, repr(exc))

    @property
    def unchecked(self) -> tuple[str, ...]:
        """Every URL this round produced no verdict for — the backlog to probe first next time."""
        return self.skipped + tuple(url for url, _exc in self.errors)


def probe_reachable(url: str, *, timeout: httpx.Timeout = PROBE_TIMEOUT) -> bool:
    """Is an OpenAI-compatible engine answering at ``url``?

    Deliberately NOT ``probe._get``: that helper is another module's private surface, and the claim
    here is this module's own — **only a transport failure counts**. A 404/500 answer means a server
    is listening, which is all this probe is allowed to conclude.

    A bare ``httpx.Client(timeout=…)``, exactly like every forward path in ``serve.py`` and every
    probe in ``probe.py`` — so it inherits the same environment (proxy vars included). That is the
    point, not an oversight: this asks "could a job reach this engine?", so it must fail wherever a
    real forward would, and a probe configured differently from the forward could only lie.
    """
    try:
        with httpx.Client(timeout=timeout) as client:
            client.get(f"{url.rstrip('/')}/models")
    except httpx.HTTPError:
        return False
    return True


def probe_urls(routes: Mapping[str, str], api_kind_by_url: Mapping[str, str]) -> tuple[str, ...]:
    """The distinct **hardware** engine URLs behind a routing snapshot, in first-seen order.

    API engines (``openai``, ``codex``) are excluded: a vendor's uptime is not this box's health, and
    pinging a metered key or a flat-rate seat every 30s spends the operator's credential to learn
    something the grid cannot act on anyway. Media is excluded structurally rather than by a rule —
    media models never enter ``_Snapshot.routes`` at all (they route through the box's own ComfyUI
    URL), so there is nothing here to filter.
    """
    return tuple(dict.fromkeys(url for url in routes.values() if url not in api_kind_by_url))


def prioritize(urls: tuple[str, ...], backlog: tuple[str, ...]) -> tuple[str, ...]:
    """``urls`` reordered so last round's unchecked engines come first.

    Probe order is otherwise the snapshot's stable route order, so whoever is last stays last: on a
    box where earlier engines reliably eat the budget, a late engine is never probed again. Missing
    a withdrawal that way is only the pre-feature behaviour, but missing a RESTORE is worse — an
    already-withheld engine could never get the single success that brings its models back, and
    nothing would say why. Entries no longer in ``urls`` (a hot-reload dropped them) fall away.
    """
    if not backlog:
        return urls
    owed = [url for url in backlog if url in urls]
    return tuple(dict.fromkeys(owed + list(urls)))


def sweep(
    urls: tuple[str, ...],
    previous: EngineHealth,
    *,
    probe: Callable[..., bool] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> SweepResult:
    """Probe each URL once and fold the result into a new ``EngineHealth``.

    A URL absent from ``urls`` (a hot-reload dropped its engine) is dropped from the result too, so
    a stale verdict can never outlive the engine it was about. A URL the budget cut off is neither
    counted as a failure nor cleared: its previous verdict is carried forward unchanged and it is
    named in ``skipped``, because "did not check" is not "checked, clean".

    ``probe`` defaults to :func:`probe_reachable` resolved **at call time**, never as a default
    argument value: a default binds the function object at import, which would silently freeze the
    seam so that every ``monkeypatch.setattr(engine_health, "probe_reachable", …)`` became a no-op
    and the "hermetic" test quietly hit a real socket instead.
    """
    probe = probe if probe is not None else probe_reachable
    deadline = _clock() + PROBE_BUDGET
    failures: dict[str, int] = {}
    skipped: list[str] = []
    errors: list[tuple[str, str]] = []

    def carry(url: str) -> None:
        """An unchecked engine keeps whatever verdict it already had — neither confirmed nor cleared."""
        if previous.failures.get(url) is not None:
            failures[url] = previous.failures[url]

    for url in urls:
        # A teardown must not wait out the round. `_serve_loop` joins the heartbeat and the reload
        # daemon against ONE shared drain budget, and the reload daemon can have a `register_once`
        # in flight whose PUT would resurrect the node if it lands after `unregister_node`
        # (ADR 0010 C5) — so an uninterruptible sweep here spends the daemon's whole turn. The
        # abandoned engines are `skipped`, exactly like a budget cut-off: unchecked, not judged.
        if (should_stop is not None and should_stop()) or _clock() >= deadline:
            skipped.append(url)
            carry(url)
            continue
        try:
            reachable = probe(url, timeout=PROBE_TIMEOUT)
        except (Exception, SystemExit) as exc:
            # Per-URL, like `orphan_sweep` classifies per pid: one engine must never veto the round
            # and freeze every sibling's verdict. Reachable in production — `httpx.InvalidURL` is
            # NOT an `HTTPError` subclass, so a malformed engine URL escapes `probe_reachable`'s own
            # guard. Recorded as UNCHECKED, never as a failure: an unexplained fault is not evidence
            # that an engine is down, and withdrawing capacity needs evidence.
            errors.append((url, repr(exc)))
            carry(url)
            continue
        if reachable:
            continue  # healthy — its count resets to absent, so recovery needs no second success
        failures[url] = previous.failures.get(url, 0) + 1
    return SweepResult(
        health=EngineHealth(failures=failures), skipped=tuple(skipped), errors=tuple(errors)
    )


def transitions(
    before: EngineHealth, after: EngineHealth, routes: Mapping[str, str]
) -> list[str]:
    """One human line per engine that just crossed the unreachable boundary, either way.

    Only URLs still in ``routes`` are considered: an engine a hot-reload just dropped is gone, not
    recovered, and reporting it as "answering again" would be a lie. Reported on the CROSSING only —
    a permanently dead engine must not print a line every 30s forever.
    """
    lines: list[str] = []
    for url in dict.fromkeys(routes.values()):
        was, now = before.unreachable(url), after.unreachable(url)
        if was == now:
            continue
        models = ", ".join(model for model, target in routes.items() if target == url)
        lines.append(
            f"engine {url} is unreachable — withdrawing {models} from the grid"
            if now
            else f"engine {url} is answering again — restoring {models}"
        )
    return lines


def unhealthy_models(routes: Mapping[str, str], health: EngineHealth) -> list[str]:
    """The advertised model names to withhold, in the snapshot's own order.

    Keyed by **URL** and expanded to models at read time, so a SIGHUP hot-reload that re-points a
    model can never carry a stale verdict onto a different engine.
    """
    return [model for model, url in routes.items() if health.unreachable(url)]
