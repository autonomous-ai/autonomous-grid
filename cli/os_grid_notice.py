"""Why there is no OS grid in your list — one line on ``grid login`` and ``grid sync`` (ADR 0039 D-k).

An *OS grid* is a grid keyed on an operating system rather than on an email domain (see
``shared.system.os_grid``). Three different things leave somebody looking at a grid list with no OS
grid in it, and until this module they shared one symptom that neither a support conversation nor the
person in front of it could take apart:

1. **The CLI is too old to send an OS claim at all.** Nothing here can report that — a CLI that has
   this module is not that CLI — but the control plane can see it, which is why ``os_served`` is on
   the reply rather than being computed locally.
2. **The control plane handed this call no OS grid** — that OS is not one it serves, or nothing has
   been provisioned for it. Reported from ``os_served``. ⚠️ **Not "the feature is switched off
   there"**, which is what this issue's own text said and what ADR 0039 D-k's amendment exists to
   correct: that switch governs creation and not admission, so a deployment with it off still serves
   an OS grid that already exists. D-k lists the four deployment states this one flag collapses, and
   why the reason-string that would have separated them lost.
3. **The machine's operating system resolves to no token at all** — a BSD, a frozen build that names
   no system. ⚠️ **The CLI answers this one itself, with no round trip**: it knows its own system and
   the closed set of tokens it can emit, so the answer is already in hand before the request is made.
   That independence is load-bearing, not an optimisation — folding this into the ``os_served`` branch
   would make the honest answer depend on the far end agreeing to talk.

Three rules the wording and the plumbing both answer to:

- **Quiet in the ordinary case.** Somebody who has an OS grid, and anybody talking to a control plane
  too old to send the key, sees nothing new. A line on every ordinary sign-in is a line people learn
  to scroll past, and it would fire for every user of every deployment that has not enabled OS grids.
- **Never a failure.** An absent OS grid is an ordinary answer — an empty answer that says why is
  still an empty answer. Nothing here touches an exit code, and nothing here raises.
  ⚠️ **And nothing here speaks over a failure either.** `cli.auth.cmd_sync` raises before the fetch
  returns when the session has expired, so a machine outside the token set is told its session
  expired and nothing about OS grids. That is deliberate, not an oversight to be hoisted: a failed
  command should report its failure, and burying the one actionable sentence under a side fact serves
  nobody. The line arrives on the next command that succeeds. "Without a round trip" (above) is about
  why the CLI *can* answer cause 3 without the far end, not a requirement that it answer despite it.
- **The same fact reaches a script.** ``--json`` carries :meth:`OsGridAbsence.as_json` rather than
  dropping what the human line says.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from shared.system import os_grid

#: This machine runs an operating system this CLI has no OS token for, so no OS grid can exist for
#: it — cause 3, answered without asking anybody.
UNSUPPORTED_SYSTEM = "unsupported_system"

#: This machine has a token, and the control plane says it was handed no OS grid for it — cause 2.
NOT_SERVED = "not_served"

#: What to call a machine that does not even name its own system (``platform.system()`` answers the
#: empty string on some frozen builds). Rare, and a sentence with a hole in it reads as a bug.
_UNNAMED_SYSTEM = "this system"


def _listed(tokens: tuple[str, ...]) -> str:
    """``("a", "b", "c")`` → ``"a, b and c"`` — the closed set, read out in a sentence."""
    if len(tokens) < 2:
        return "".join(tokens)
    return f"{', '.join(tokens[:-1])} and {tokens[-1]}"


@dataclass(frozen=True)
class OsGridAbsence:
    """Why this machine has no OS grid — the one fact, in the two shapes it is reported in.

    ``os_token`` is ``None`` exactly when ``reason`` is :data:`UNSUPPORTED_SYSTEM`: having no token
    IS that reason. Both are carried anyway rather than one being derived, so a script may key on
    whichever it finds clearer and neither reading can drift from the other.
    """

    reason: str
    system: str
    os_token: str | None

    def line(self) -> str:
        """The one line a person sees. Names the machine's OS and says nothing else failed.

        ⚠️ **The cause clause must never restate the four words before it.** The first draft read
        *"No OS grid for FreeBSD: this CLI has no OS grid for that operating system"* — a colon that
        promises a reason and delivers the subject again, in a feature whose whole purpose is to say
        why. Naming the closed set is the only thing that makes the unsupported case actionable: it
        is what lets somebody see they are outside it and stop looking.

        ⚠️ **The set is DERIVED from `OS_TOKENS`, never written out**, so it cannot claim a grid this
        CLI could not ask for and it grows itself when `omarchy` lands (ADR 0039 D-c, issue 04). It
        is spelled in OS **tokens** rather than prettied labels on purpose — a label map here would
        be a fourth copy of `macos`→`macOS` across these repositories, and grid-apis'
        `os_networks._OS_LABELS` already carries the warning that the two must stay separate values
        because a prettied-up derivation would silently move the gate. Speaking in tokens throughout
        also keeps this sentence's subject one kind of value.
        """
        cause = (
            f"grid has one only for {_listed(os_grid.OS_TOKENS)}"
            if self.reason == UNSUPPORTED_SYSTEM
            else "the control plane isn't serving one"
        )
        named = self.os_token or self.system or _UNNAMED_SYSTEM
        return f"No OS grid for {named}: {cause}. Your other grids are unaffected."

    def as_json(self) -> dict[str, Any]:
        """The same fact for a script — what ``--json`` puts under ``os_grid``."""
        return {"reason": self.reason, "system": self.system, "os_token": self.os_token}


def absence(os_served: bool | None) -> OsGridAbsence | None:
    """Why this machine has no OS grid, or ``None`` when there is nothing to say.

    ``os_served`` is the control plane's answer from ``remote.control_plane.TokenFetch`` — ``True``
    served, ``False`` not served, ``None`` a control plane too old to have said.

    ⚠️ **The local answer is decided FIRST and does not look at ``os_served`` at all.** A machine
    outside the closed token set has no OS grid whatever the far end reports, the two causes have
    different remedies, and printing both lines would be worse than printing neither.
    """
    token = os_grid.os_token()
    if token is None:
        return OsGridAbsence(
            reason=UNSUPPORTED_SYSTEM, system=os_grid.system_name(), os_token=None)
    # `is False` and never `not os_served`: `None` is an older control plane that did not send the
    # key, and reporting a refusal it never made is the one direction D-k exists to keep quiet.
    if os_served is False:
        return OsGridAbsence(
            reason=NOT_SERVED, system=os_grid.system_name(), os_token=token)
    return None
