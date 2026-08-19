"""What a measurement is allowed to say, and the one way it says "I could not look".

The vocabulary is deliberately small and deliberately three-valued in two places, because both are
distinctions this tracker has already paid for once:

  * `CannotRun` — the harness will not START a tier, because a prerequisite it cannot supply is
    missing. It is raised before anything is measured, so nothing partial is reported.
  * a measurement that ran and could not reach an answer is `unavailable` WITH A REASON — never a
    zero, never an absent key. `Pushed.unchecked` in `CLAUDE.md` is the same rule stated for the
    merge check: "verified" and "unexamined" must not be the same observation.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

#: A measurement reached an answer.
MEASURED = "measured"
#: A measurement ran and could NOT reach one. Always carries a reason, never a number.
UNAVAILABLE = "unavailable"

#: How a result stands against what ADR 0034 already asserts. Three-valued because "the ADR says
#: nothing about this" and "the ADR agrees" are different facts, and collapsing them is how a
#: contradiction gets absorbed instead of called out.
CONSISTENT = "consistent"
CONTRADICTS = "contradicts"
NOT_STATED = "not_stated"
_VERDICTS = (CONSISTENT, CONTRADICTS, NOT_STATED)

#: How a version string reads when the tool could not be asked. Deliberately not a version-shaped
#: string and deliberately not an empty one: a reader scanning a report must not be able to mistake
#: "we did not ask" for "it answered nothing".
UNKNOWN_VERSION = "unavailable: {reason}"

_VERSION_TIMEOUT_SECONDS = 15


class CannotRun(Exception):
    """A tier's prerequisite is absent, and the harness refuses to start rather than half-run.

    An exception rather than a returned sentinel so a tier cannot forget to check one: the raise
    unwinds to `cli.main`, which is the single place that turns it into a sentence and an exit code.
    """


@dataclass(frozen=True)
class Result:
    """One measurement's outcome — including the outcome "I looked and could not tell".

    The invariants are enforced in the constructor rather than checked at the report's edge,
    because the failure they prevent is silent by construction: a measurement that hit an error and
    returned its zero-initialised numbers produces a report indistinguishable from a real one, and
    the number then gets cited. Refusing the combination outright is the only version of this rule
    that cannot be forgotten by a caller.
    """

    #: Which measurement, in issue 35's own numbering.
    name: str
    status: str
    #: Required when `unavailable`, forbidden when `measured` — the reason IS the finding.
    reason: str = ""
    #: The numbers. Forbidden when `unavailable`.
    data: dict = field(default_factory=dict)
    #: What ADR 0034 asserts about this, quoted, so a later reader compares against the words
    #: rather than against somebody's memory of them.
    adr_claim: str = ""
    adr_verdict: str = NOT_STATED

    def __post_init__(self) -> None:
        if self.status not in (MEASURED, UNAVAILABLE):
            raise ValueError(f"{self.name}: {self.status!r} is not a status")
        if self.adr_verdict not in _VERDICTS:
            raise ValueError(f"{self.name}: {self.adr_verdict!r} is not a verdict")
        if self.status == UNAVAILABLE:
            if not self.reason.strip():
                raise ValueError(f"{self.name}: an unavailable result must say why")
            if self.data:
                raise ValueError(
                    f"{self.name}: an unavailable result carries no numbers, and it was given "
                    f"{sorted(self.data)} — a number taken from a measurement that did not reach "
                    f"an answer is the one thing this harness must never emit")
            if self.adr_verdict != NOT_STATED:
                # `assemble` lists a CONTRADICTS result at the TOP of the report and exits 3 on it.
                # Combined with the rule above — an unavailable result carries no data — that is a
                # headline claim about the ADR backed by a measurement which explicitly did not
                # happen, and the same name would appear in `contradictions` and `unavailable` at
                # once. A measurement that did not run has no opinion about the ADR.
                raise ValueError(
                    f"{self.name}: a measurement that did not run cannot hold a verdict on the "
                    f"ADR, and it was given {self.adr_verdict!r}")
        else:
            if not self.data:
                raise ValueError(
                    f"{self.name}: a measured result with no data is an unavailable one")
            if self.reason.strip():
                # `as_json` copies `reason` only for an unavailable result, so one set here would
                # vanish from the report in silence — and the caller who wrote it would believe it
                # had been recorded.
                raise ValueError(
                    f"{self.name}: a measured result has no reason field in the report, so "
                    f"{self.reason!r} would be dropped silently; put it in `data`")
        if self.adr_verdict == CONTRADICTS and not self.adr_claim.strip():
            raise ValueError(
                f"{self.name}: a contradiction must quote the ADR claim it contradicts, or it "
                f"cannot be acted on")

    def as_json(self) -> dict:
        body: dict = {"status": self.status}
        if self.status == UNAVAILABLE:
            body["reason"] = self.reason
        else:
            body.update(self.data)
        body["adr_verdict"] = self.adr_verdict
        if self.adr_claim:
            body["adr_claim"] = self.adr_claim
        return body


def unavailable(name: str, reason: str) -> Result:
    """The only spelling of "I looked and could not tell", so every tier says it the same way."""
    return Result(name=name, status=UNAVAILABLE, reason=reason)


def _first_line_of(argv: list[str]) -> str:
    """What a tool answers when asked for its version, or a reason it could not be asked.

    Never raises. A version probe that could fail the run would make "which git did this" the one
    thing capable of losing every number beside it.
    """
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, errors="replace",
                              timeout=_VERSION_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return UNKNOWN_VERSION.format(reason=f"{argv[0]} could not be run ({exc})")
    reported = (proc.stdout or proc.stderr or "").strip().splitlines()
    if not reported:
        return UNKNOWN_VERSION.format(reason=f"{argv[0]} answered nothing")
    return reported[0].strip()


def tool_versions() -> dict[str, str]:
    """The tools every number in this report was taken against.

    BOTH are recorded on every tier, including the ones that use only one. A measurement outlives
    the tools that produced it — `merge-tree --write-tree` needs git ≥ 2.38 and did not exist
    before it, and Claude Code's transcript behaviour is the vendor's to change without notice — so
    a report that names only the tool a tier happened to touch cannot be re-taken or compared.
    """
    from . import agent_tier

    try:
        claude = _first_line_of([agent_tier.require_claude(), "--version"])
    except CannotRun as exc:
        claude = UNKNOWN_VERSION.format(reason=str(exc))
    return {"git": _first_line_of(["git", "--version"]), "claude_code": claude}


def assemble(*, tier: str, source: Path | str | None, results: list[Result]) -> dict:
    """One report body: what ran, what it ran against, and — first — what disagrees with the ADR.

    `contradictions` and `unavailable` are lifted to the top on purpose. Six measurements is
    already more than a reader checks one by one, and both of these are findings that change what
    somebody does next: one means ADR 0034 needs amending, the other means a number everyone is
    about to cite was never taken. Buried among the entries, each reads as detail.
    """
    return {
        "tier": tier,
        "source": str(source) if source is not None else None,
        "versions": tool_versions(),
        "contradictions": [r.name for r in results if r.adr_verdict == CONTRADICTS],
        "unavailable": [r.name for r in results if r.status == UNAVAILABLE],
        "measurements": {r.name: r.as_json() for r in results},
    }


def write(path: Path, body: dict) -> None:
    """One report, on disk and on stdout, so a run that scrolled past is still citable.

    The PATH is printed too, and that is not decoration: a run's whole product is this file, and a
    harness that leaves one somewhere the operator has to guess at is one whose numbers get
    re-taken instead of cited.
    """
    text = json.dumps(body, indent=2, sort_keys=True)
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")
    print(text, flush=True)
    print(f"\nreport written to {path}", flush=True)
