"""The `providers` block of `grid project status`, rendered for a person (ADR 0033 D-l/D-f).

Extracted from `cli/remote_project.py` when G-01's fix took that file past this project's 800-line
ceiling — the split the distributed-tasks notes had already flagged as owed by the next slice to
touch it. One concern lives here: turning the relay's fleet answer into the one or two sentences a
member can act on, and staying silent otherwise.

Two rules run through all of it and are easy to lose in a later edit:

* **Nothing is said when nothing is wrong.** A line reading "0 paused" on every healthy poll is the
  kind of noise that makes the one that matters invisible.
* **A reply this command cannot read is not a report.** Saying nothing is exactly what an older
  relay produces, and it beats "2 of None providers have withdrawn".
"""
from __future__ import annotations


def is_count(value) -> bool:
    """Whether `value` is a number of things this command may print.

    `isinstance(value, bool)` first, because **`bool` subclasses `int`** — so a bare
    `isinstance(value, int)` accepts `True` and renders "True of True providers have withdrawn",
    which is exactly the unreadable-reply case the caller is trying to stay silent about. The same
    trap `remote/task_capacity._resets_at` already excludes by hand on the other side of this wire.
    """
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def print_unserved(providers: dict | None) -> bool:
    """Say that this grid serves nobody at the caller's email domain, if that is what it said.
    Returns whether anything was printed, so `print_providers` can stop there.

    The one state `online` gets exactly BACKWARDS (ADR 0033 D-f, issue 24): a member outside the
    relay's served-domain allowlist is shown the providers serving OTHER PEOPLE — a healthy number
    beside work no provider will ever be offered, which reads as "the grid is busy" until the task
    expires hours later.

    ⚠️ **Callable outside the queue gate on purpose, and that is dev-VM finding G-01.** Being
    unserved is a fact about the CALLER, not about the queue: it does not become true when work
    starts waiting and it does not stop being true when the waiting ends. It used to be printed only
    from inside `print_providers`, i.e. only beside a non-empty queue — and `queue_expired`, whose
    own terminal message tells the member by name to run `grid project status`, is precisely the
    event that EMPTIES that queue. So the one member who could not be served followed the advice
    they were given and got a status with nothing about providers in it, every time. Measured both
    ways on the dev VM: the sentence appeared while the task was queued and vanished the moment it
    timed out.

    It also runs BEFORE the count guard and before the `paused` branch, for two different reasons.
    Before the counts, because `serves_you` is its own readable fact and does not need `online` and
    `paused` to be renderable numbers — otherwise one unreadable counter withholds the only sentence
    the caller can act on. Before `paused`, because nothing here IS paused: a fleet entirely healthy
    and entirely unavailable to this member is the whole failure, and no action they take alone can
    fix it.

    `is False`, never falsiness: *absent ⇒ served*, which is both a relay predating this slice and
    every relay with no allowlist configured. A missing key must not invent a refusal.
    """
    if not isinstance(providers, dict) or providers.get("serves_you") is not False:
        return False
    print("This grid does not serve your account's email domain, so no provider will claim "
          "your tasks. Ask whoever runs this grid to add your domain.")
    return True


def print_providers(providers: dict | None) -> None:
    """Who could take the work, and when the rest come back (ADR 0033 D-l, issue 19b).

    Only ever called beside a queue that is not empty: this answers "why is my work waiting", and a
    fleet report on a project with nothing queued is a fact nobody asked for. That rule still holds
    for everything here — it is only `print_unserved`, a fact about the caller rather than about the
    queue, that had to come out from behind it (G-01).

    *Absent ⇒ nothing said.* A relay predating this slice sends no `providers` key at all, and the
    rest of the status renders exactly as it did — the same degrade every other 19b value has.
    """
    if print_unserved(providers):
        return
    if not isinstance(providers, dict):
        return
    online = providers.get("online")
    paused = providers.get("paused")
    if not is_count(online) or not is_count(paused):
        # A block this command cannot read is not a fleet report. Saying nothing is the same thing
        # an older relay produces, and it beats "2 of None providers have withdrawn" — the sibling
        # rule to `grid task list`'s, which learned it the same way in 19a's review.
        return
    if online == 0:
        # The state a queue depth alone cannot express, and the one that needs a different action
        # from every other: the work is not slow, there is nobody to do it.
        print("No provider is online, so nothing in this queue can start. "
              "Someone needs to run `grid join`.")
        return
    if not paused:
        return
    print(f"{paused} of {online} providers have withdrawn — their Claude subscription is out of "
          f"headroom, so they are not claiming tasks.")
    resumes_at = providers.get("resumes_at")
    if resumes_at:
        # The actionable half. "Paused" alone tells a team to keep watching; a time tells them
        # whether to wait or to add a provider.
        print(f"The first of them starts claiming again at {resumes_at}.")
