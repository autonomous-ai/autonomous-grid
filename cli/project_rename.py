"""`grid project rename` — giving a project a different name (ADR 0035 D-a / D-g, issue 55).

Extracted rather than added to `cli/remote_project.py`, for the reason `cli/project_archive.py` and
`cli/project_visibility.py` both record: that file is already past this project's 800-line ceiling.

**The id is what does not change, and this command says so out loud.** A name was never an address
(ADR 0033 D-a). A rename therefore breaks no clone, no script and no stored reference — and it FREES
the old name, which matters because `grid project create` is create-or-get *by name*: a colleague
whose script still says the old one now gets a new, empty project rather than this one. That is the
documented meaning of create-or-get and is deliberately not guarded against; printing the id is the
mitigation, because the id is the thing that still reaches the work.

## The `default` warning lives HERE, and only here

The projectless `grid task create` resolves the caller's own project *named* `default`
(ADR 0033 D-o). So renaming **to** `default` silently changes where the next unqualified task lands,
and renaming **away from** it makes that command start refusing.

⚠️ **The relay must not learn this rule.** `default` is a pure client convention and the relay
deliberately no longer resolves it for anybody — putting the check server-side would rebuild a
coupling that was removed on purpose. Nothing about `default` is sent, and a test asserts the
request body is `{"name": …}` and nothing else.

The warning goes to **stderr**, on both paths. Under `--json` stdout is the relay's document and has
to stay one parseable thing (`cli/json_error.py`'s contract), and an application that never reads
stderr is no worse off than it is today; a person at a terminal sees it either way.
"""
from __future__ import annotations

import argparse
import shlex
import sys


def _as_the_relay_will_store_it(name: str) -> str:
    """What the relay will have stored for `name`, so a postcondition can compare like with like.

    ⚠️ **A hand-duplicated normalisation**, and the only one on this route. grid-src's
    `projects.requested_name` does `raw.strip()` before it stores and echoes the name, so the reply
    is not always byte-identical to what the caller typed — and this command's postcondition
    compares the two.

    Keying that comparison on the RAW argument reported every `--name "acme "` as a **failed**
    rename for a rename that had landed, and the refusal's own remedy re-runs the same request, so
    it repeated forever. The person's next move is `grid project create --name acme`, which is
    create-or-get BY NAME: a second, empty project, with their work left in the first — the exact
    fork this command exists to prevent, delivered by the command itself. Worse in one case:
    `--name "default "` really does make the project their `default`, and a warning keyed on the raw
    argument could not see it. Found by a silent-failure sweep, not by review.

    Duplicated rather than avoided because both alternatives are worse. Sending `name.strip()` to
    the relay would make this CLI a second validator, which `relay.rename_project` forbids — the
    relay owns what a project may be called. Dropping the comparison would let a relay that answered
    with the OLD name be reported as a rename, which is the failure the guard exists for. So it is
    duplicated AND pinned, the discipline every hand-kept value on this seam follows:
    `tests/test_task_lease.py::test_the_relay_normalises_a_project_name_the_way_this_cli_expects`
    reads grid-src's validator and fails if it ever normalises differently.

    It deliberately REFUSES nothing — length, emptiness and type are the relay's to judge, and a
    second opinion here is precisely what this function exists to avoid becoming.
    """
    return name.strip()


def project_rename(args: argparse.Namespace) -> int:
    """Rename a project, and print the id that did not change."""
    from remote import relay

    from . import remote_project

    base, token, label = remote_project._resolve(args)
    answer = relay.rename_project(base, token, args.project_id, name=args.name)

    # What the relay will have STORED, which is not always what the caller typed. Everything
    # downstream reads this and not `args.name`: the guard below, the sentences, and the `default`
    # warning.
    asked = _as_the_relay_will_store_it(args.name)

    # ⚠️ **Emitted, then validated — never returned on** (ADR 0034 D-m, issue 46). The guard below
    # was unreachable under `--json` in the two commands this one is modelled on, which is the one
    # mode an application drives: a body that never said so exited 0 there, and the caller walks
    # away believing the project was renamed.
    emitted = remote_project._emit(args, answer)
    if not isinstance(answer, dict) or answer.get("name") != asked:
        # The house guard: a reply this command cannot read is not a state change it may report.
        # ⚠️ Compared against the name we ASKED for, never a truthiness test — a relay that omitted
        # the key, or answered with the old name, would otherwise be reported as having renamed the
        # project. After which somebody tells a colleague the new name and nothing answers to it.
        raise SystemExit(
            f"The relay's answer for project {args.project_id} did not say the project was renamed "
            f"to {asked!r}, so this cannot be reported as one. "
            f"`grid project rename {args.project_id} --name {asked} --json` shows what it sent.")

    # `isinstance`, not a bare `.get()`: this is the relay's value and it is compared against a
    # constant below. *Absent ⇒ say nothing about the old name* — the whole route is missing on any
    # relay that would not send it, so absence here means a reply this build cannot fully read, and
    # a WARNING is advice rather than a state claim. It degrades instead of refusing; the guard
    # above is what refuses.
    #
    # ⚠️ **`.strip()` as well as `isinstance`, because `isinstance("", str)` is True.** The empty
    # string is the one wrong VALUE the type check lets through, and it reaches the two places
    # `previous` is PRINTED rather than compared: it printed `and was ` followed by a bare
    # `--name` with nothing after it, which argparse refuses. Not reachable from a correct relay —
    # the column is NOT NULL and every name went through `requested_name` — but this guard's whole
    # job is surviving a reply this build cannot fully read.
    previous = answer.get("previous_name")
    previous = previous if isinstance(previous, str) and previous.strip() else None

    _warn_if_it_touches_default(previous, asked)

    if emitted:
        return 0

    if previous == asked:
        # `==` against the name we asked for, so this can only fire when the relay confirmed both.
        # A previous request had already done it, or nothing needed doing — either way saying "is
        # now called" would claim an act that did not happen.
        print(f"{args.project_id} was already called {asked} on {label}")
    elif previous is None:
        print(f"{args.project_id} is now called {asked} on {label}")
    else:
        print(f"{args.project_id} is now called {asked} on {label}, and was {previous}")
    print()
    print("Its id has not changed, so anything pointing at it still works.")
    if previous is not None and previous != asked:
        # Every printed command goes at the END of its line — `test_task_lease.py` retypes each one
        # through the real parser and reads to end of line, so a command with prose after it is
        # reported as advice the CLI cannot run.
        #
        # ⚠️ **`--name=` with a shell-quoted value, and both halves are load-bearing.** This is the
        # one line here built from a value the CLI does not control: `requested_name` strips a name
        # and bounds its length, and permits everything else. `shlex.quote` is what makes
        # `my old app` paste back as ONE argument (the rule `remote_task._no_trunk_message`
        # follows), and the `=` form is what survives a leading dash — `shlex.quote("-evil")`
        # returns it UNQUOTED (measured, and recorded in `remote/project_clone.py`), so
        # `--name -evil` reads as a flag and argparse answers "expected one argument".
        #
        # One spelling for both, rather than quoting conditionally: two spellings of one rule is how
        # one of them comes to be wrong. `test_the_undo_line_parses_even_when_the_old_name_needs_quoting`
        # puts the real value through the real parser, which the `{...}`-blanking retype scan cannot.
        print(f"Undo with: grid project rename {args.project_id} "
              f"--name={shlex.quote(previous)}")
    return 0


# What a person has to be told, per direction. A table rather than two `if`s, because the two
# sentences differ only in what breaks and writing them apart is how one of them comes to describe
# the other's failure. Keyed on whether it is the OLD name or the NEW one that is `default`.
#
# ⚠️ **The "to" sentence must not say work MOVED, because that state cannot exist.**
# `idx_projects_owner_name` is `(owner_id, name)` and covers archived rows, so a rename TO `default`
# only succeeds when the caller had no project of that name at all — which means
# `grid task create` with no `--project` was REFUSING beforehand, not landing work somewhere else.
# The rename is what makes the shorthand start working. An earlier draft said "rather than wherever
# it used to go", describing a state the reader cannot be in; D-g's whole point is that each
# direction names the failure they are actually in, so that is the same defect as no warning.
_DEFAULT_WARNINGS = {
    "from": ("was your 'default' project, and `grid task create` with no --project looks that name "
             "up every time. That command will now refuse until you have another project called "
             "'default' — name this one explicitly instead"),
    "to": ("is now your 'default' project, so `grid task create` with no --project will land work "
           "here from now on. That shorthand had nothing to resolve before this rename"),
}


def _warn_if_it_touches_default(previous: str | None, name: str) -> None:
    """Say so when a rename moves the projectless `grid task create` (ADR 0035 D-g).

    Both directions, because they break differently and a person can only act on the one they are
    in: renaming **away from** `default` makes that command start refusing, and renaming **to** it
    silently changes where the next unqualified task lands.

    `DEFAULT_PROJECT_NAME` is imported rather than spelled again — one convention, one constant, so
    a build that ever changed it cannot leave this warning firing on the old word.

    Written to **stderr** so that `--json`'s stdout stays one parseable document.

    ⚠️ `name` must be the name the relay STORED, never the raw argument. `--name "default "` really
    does make the project the caller's `default` — the relay strips it — and a warning keyed on what
    was typed cannot see that, because `"default " != "default"`. Found by a silent-failure sweep.
    """
    from .remote_task import DEFAULT_PROJECT_NAME

    if previous == name:
        # ⚠️ **Nothing changed, so nothing moved.** The relay answers this double-submit with a 200
        # on purpose (`idx_projects_owner_name` is satisfied by the row itself), and warning here
        # made stderr contradict stdout in one invocation: "was already called default" beside "that
        # command will now refuse". Both directions fire at once on a project that is called
        # `default` and was asked to be called `default`, which is the shape an application
        # re-submitting an unchanged form produces. Caught by a silent-failure sweep, and it is the
        # very failure the paragraph below warns about — a warning that cries wolf is one nobody
        # reads on the day it is true.
        return

    # `==`, never a substring or a truthiness test: a project called `default-old` is not the
    # caller's default project, and warning about it would teach people to ignore this line.
    directions = []
    if previous == DEFAULT_PROJECT_NAME:
        directions.append(("from", previous))
    if name == DEFAULT_PROJECT_NAME:
        directions.append(("to", name))

    for direction, which in directions:
        print(f"Note: {which!r} {_DEFAULT_WARNINGS[direction]}.", file=sys.stderr)
