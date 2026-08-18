"""What the surface an application drives has to look like (ADR 0034 D-m, issues 41 and 46).

Two contracts, in one file because they are about the same set of commands and would otherwise keep
two copies of it: **every command a program drives emits `--json`**, and **none of them speaks git**.

The person at the other end reaches the grid through a desktop application, describes what they want
in their own words, and does not know what a branch is. D-m states it as a flat rule: *"No git
vocabulary and no raw git error reaches the application's surface."*

Issue 41 met that criterion by asserting the free cross-repo E2E's OUTPUT carried no branch
vocabulary, which is real but narrow — a help string nobody exercised in that session is never
looked at, and `--help` is the only user-facing prose in this repository with no test over it at all,
which is exactly why it rots first (`test_no_help_text_tells_anybody_to_run_a_command_that_was_deleted`
records the same finding for a different word list). This is the standing scan the criterion asks
for: enforced by a test rather than by review.

## Why it is scoped, and why the exemptions are individual

**Scoped**, because half this CLI is *supposed* to speak git. `grid project clone`, `import`,
`commit`, `refresh`, `wip reset` and `grid task fetch` exist for somebody with git on their machine
and a repository in front of them; telling them "a starting point" instead of "the trunk" would make
those commands worse, not better. `GIT_PLANE` is that list, named rather than derived, so adding a
command to it is a decision somebody makes on purpose.

**Individually exempted**, because every word on the list also has an ordinary English sense.
`grid task get` says *"so a script can branch on it"* — the verb, in a sentence about exit codes,
nowhere near a repository. A blanket regex cannot tell those apart and a blanket exemption would
hide the real ones, so each survivor is written down here with the reason it survives. Adding an
entry is a review decision; that is the contract this file is.
"""
from __future__ import annotations

import argparse
import ast
import pathlib
import re

import pytest

# The shared list, quoted from issue 46 and used by issue 41's criterion as well. Kept here rather
# than in `cli/` because it is a fact about the PRODUCT's vocabulary, not a value any code reads.
GIT_WORDS = (
    "branch", "merge", "commit", "ref", "conflict", "fast-forward", "rebase", "HEAD", "trunk",
    "main", "wip", "oid", "sha",
)

# `(?<![\w-])…(?![\w-])` rather than `\b`: `\b` matches inside `fast-forward` and would report the
# hyphenated word twice, and it also fires on `ref` inside `refresh`, which is a command name.
_PATTERN = re.compile(
    r"(?<![\w-])(" + "|".join(re.escape(word) for word in GIT_WORDS) + r")(?![\w-])",
    re.IGNORECASE,
)

# Every leaf command the Flutter client drives. Written out rather than derived from `grid task *` +
# `grid project *`, because the two groups are NOT the boundary — `grid task fetch` is in the task
# group and is a git-plane command, and a derived list would have quietly admitted it.
APPLICATION_SURFACE = frozenset({
    "grid task create", "grid task send", "grid task get", "grid task follow", "grid task list",
    "grid task cancel", "grid task diff", "grid task undo",
    "grid project create", "grid project list", "grid project status",
    "grid project files", "grid project file", "grid project download",
    "grid project archive", "grid project unarchive", "grid project delete",
    "grid project share", "grid project private",
    "grid project member list", "grid project member add", "grid project member remove",
})

# Commands for somebody who HAS git and a repository, where this vocabulary is the correct
# vocabulary. Listed so that the scan's boundary is a decision rather than an accident.
GIT_PLANE = frozenset({
    "grid project clone", "grid project import", "grid project commit", "grid project refresh",
    "grid project init", "grid project wip reset", "grid task fetch",
})

# `(command, where, word)` → why this one is not git vocabulary. One line each, and each has to say
# what the word means HERE. An entry that cannot be written is a string to reword.
ALLOWED = {
    ("grid task get", "description", "branch"):
        "the English verb, in `a script can branch on it` — about exit codes, not a repository",
}

# Exact printed strings that survive the scan, and why — the same contract `ALLOWED` is for the
# help text. Keyed on the whole string rather than on `(module, word)`, so an exemption covers the
# one sentence somebody justified and not every future use of that word in the file.
ALLOWED_PRINTED = {
    "Unknown project wip action: ":
        "names the `grid project wip` command itself, in a guard for an action argparse's own "
        "`choices` already makes unreachable — the same reading that lets `grid project refresh` "
        "keep its name",
}

# A floor under the walk itself. A scan that visits nothing passes, and this file's whole value is
# that it fires; `enforcement-scan-needs-per-shape-floor` is the standing lesson.
_MINIMUM_COMMANDS = 20
# Below today's count (82) so that ordinary editing never trips it, and far above what a walker
# that stopped descending would find, which is zero.
_MINIMUM_STRINGS = 70

# The handler modules whose printed strings a person driving the application actually reads.
#
# ⚠️ **`remote_project.py` is IN, with its git-plane handlers exempted by FUNCTION** — found in
# review. Excluding the whole module (its docstring says it "holds clone, import, commit and
# refresh") also excluded `_project_create`, which is on the application's surface and printed
# `main is now at …` and "a task is cut from `main`". The module is not the boundary: that file
# straddles both planes, and a per-module rule silently exempted the busiest application command in
# it. `remote_task.py` is in for the same reason — it holds `grid task fetch`.
_HANDLER_MODULES = (
    "cli/remote_task.py",
    "cli/remote_project.py",
    "cli/task_diff.py",
    "cli/task_undo.py",
    "cli/project_files.py",
    "cli/project_download.py",
    "cli/project_archive.py",
    "cli/project_visibility.py",
    "cli/json_error.py",
)

# The functions inside those modules that serve a `GIT_PLANE` command, and may speak git. Named
# rather than derived: nothing in the source links a handler to the command that dispatches to it,
# so this is a decision somebody makes, exactly like `GIT_PLANE` itself.
_GIT_PLANE_FUNCTIONS = frozenset({
    "_project_clone", "_project_import", "_project_commit", "_project_refresh", "_project_init",
    "_wip_reset",
    # `remote_task.py`'s half: `grid task fetch` is the one command in this CLI that runs git.
    "_task_fetch", "_fetch_failure_message",
})

_REPO = pathlib.Path(__file__).resolve().parent.parent


def offences(text: str) -> list[str]:
    """The git words in `text`, lowercased. Public so issue 41's E2E can use the same reading."""
    return [match.group(0).lower() for match in _PATTERN.finditer(text or "")]


def _leaves(parser, prefix="grid"):
    """Every leaf command, as `(name, [(where, text), …])`."""
    subs = [a for a in parser._actions if hasattr(a, "_name_parser_map")]
    if not subs:
        texts = [("description", parser.description or "")]
        for action in parser._actions:
            where = "/".join(action.option_strings) or action.dest
            texts.append((where, action.help or ""))
        yield prefix, texts
        return
    for action in subs:
        for name, sub in action._name_parser_map.items():
            yield from _leaves(sub, f"{prefix} {name}")


def _application_leaves():
    from cli.parser import build_parser

    return [(name, texts) for name, texts in _leaves(build_parser())
            if name in APPLICATION_SURFACE]


def test_the_surface_an_application_drives_speaks_no_git():
    """Every `--help` string on the application's commands, swept.

    The failure this prevents is not a bad word in isolation: it is a person being told their
    project has "no trunk", looking that up, and arriving at git's documentation for a product that
    has deliberately hidden git from them.
    """
    found = []
    for name, texts in _application_leaves():
        for where, text in texts:
            for word in offences(text):
                if (name, where, word) in ALLOWED:
                    continue
                excerpt = " ".join(text.split())
                found.append(f"`{name}` [{where}] says {word!r}: {excerpt[:120]}")
    assert not found, (
        "git vocabulary on the surface an application drives (ADR 0034 D-m). Reword it, or — if the "
        "word really is being used in its ordinary English sense — add it to `ALLOWED` with the "
        "reason:\n  " + "\n  ".join(sorted(found)))


def test_the_scan_really_walked_the_surface():
    """A scan that visits nothing passes, which is the one way this file can be worthless.

    Pinned by count AND by name: a walker that silently stopped descending would still find some
    commands, and a rename that took `grid task create` out of the application surface without
    anybody noticing is the change this second half catches.
    """
    walked = {name for name, _texts in _application_leaves()}
    assert len(walked) >= _MINIMUM_COMMANDS, (
        f"the walk found only {len(walked)} of the application's commands: {sorted(walked)}")
    assert walked == APPLICATION_SURFACE, (
        "the application surface and the parser have drifted apart; missing from the parser: "
        f"{sorted(APPLICATION_SURFACE - walked)}")


@pytest.mark.parametrize("planted,word", [
    ("Your work is on a branch until somebody promotes it.", "branch"),
    ("The project has no trunk yet.", "trunk"),
    ("This left main unchanged.", "main"),
    ("Resolve the conflict and try again.", "conflict"),
    ("It could not fast-forward.", "fast-forward"),
    ("Its wip ref moved.", "wip"),
    ("The commit oid is unchanged.", "commit"),
])
def test_the_scan_catches_a_planted_word(planted, word):
    """The positive control. A negative-result harness with no positive row proves nothing — every
    assertion above is "nothing was found", which is exactly what a broken regex also reports."""
    assert word in offences(planted), f"{word!r} was not caught in {planted!r}"


@pytest.mark.parametrize("ordinary", [
    "so a script can branch on it",
    "refresh your view of the grid",
    "the main-thread loop",
])
def test_the_scan_does_not_fire_on_a_word_that_only_looks_like_one(ordinary):
    """The other control, and the reason `ALLOWED` is per-string rather than per-word.

    ⚠️ Two of these are about the PATTERN, not about English: `refresh` contains `ref` and
    `main-thread` contains `main`, and a `\\b`-anchored regex reports both. `grid project refresh` is
    a real command name, so a scan that flagged it would be unfixable without renaming a command.
    """
    if ordinary == "so a script can branch on it":
        # This one really does contain the word; it is exempted by name, not by the pattern.
        assert offences(ordinary) == ["branch"]
        return
    assert offences(ordinary) == [], f"the pattern fired on {ordinary!r}"


# What a string has to flow into before a person can read it. `TaskRefusal` is here beside the two
# obvious ones because `remote/relay.py` raises it with the relay's sentence and this CLI now
# substitutes its own into it (issue 46) — a refusal is prose whichever class carries it.
_USER_FACING_CALLS = frozenset({"print", "SystemExit", "TaskRefusal"})


def _printed_strings(path: pathlib.Path):
    """Every string a module can put in front of a person, with the line it is on.

    ⚠️ **Not every string literal, and the difference is the whole of what makes this scan usable.**
    `_MERGE_KIND = "merge"` and `answer.get("commit")` are WIRE values — a lockstep enum and a JSON
    key — and both are the vocabulary the relay speaks, correctly, in code nobody reads out loud.
    A scan over every constant reports them, and the only way to keep it green would be to exempt
    the two words that matter most.

    So this walks the SINKS instead: what is passed to `print`, raised as a refusal, or returned
    from a function that builds a sentence. Docstrings and comments are out for the same reason —
    they are written for the next person editing this repository, and they must go on saying `trunk`
    and `wip/<conversation-id>`; the register depends on it.

    ⚠️ A string at MODULE level reaches nobody through these sinks and is therefore not walked; the
    constants that matter (`_MERGE_TURN_LABEL`, the refusal templates) are read inside a function
    that prints or raises, so they are covered where they are used.
    """
    tree = ast.parse(path.read_text())

    def parts(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value
        elif isinstance(node, ast.JoinedStr):
            for piece in node.values:
                if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                    yield node.lineno, piece.value

    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if function.name in _GIT_PLANE_FUNCTIONS:
            # A `GIT_PLANE` command's handler, where this vocabulary is the correct vocabulary. Per
            # FUNCTION and not per module: `remote_project.py` and `remote_task.py` each straddle
            # both planes, and a per-module rule exempted `_project_create` — the busiest command on
            # the application's surface — for years without anybody choosing to.
            continue
        for node in ast.walk(function):
            if isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else getattr(
                    node.func, "attr", "")
                if name in _USER_FACING_CALLS:
                    for argument in node.args:
                        yield from parts(argument)
            elif isinstance(node, ast.Return) and node.value is not None:
                # A helper that BUILDS a sentence for its caller to print —
                # `task_diff._nothing_to_show` is the one this exists for, and it is the most
                # user-facing prose in that module.
                yield from parts(node.value)


def test_what_the_application_facing_handlers_print_speaks_no_git():
    """The other half: the strings these modules build at run time, not their `--help`.

    A command's help can be spotless while the sentence it prints on failure names a ref — which is
    precisely where a person is when they most need to understand what happened.
    """
    found, counted = [], 0
    for relative in _HANDLER_MODULES:
        path = _REPO / relative
        for lineno, value in _printed_strings(path):
            counted += 1
            if value in ALLOWED_PRINTED:
                continue
            for word in offences(value):
                found.append(f"{relative}:{lineno} says {word!r}: {' '.join(value.split())[:110]}")
    assert counted >= _MINIMUM_STRINGS, (
        f"only {counted} strings were read out of {len(_HANDLER_MODULES)} modules, so this scan is "
        f"not looking at what it thinks it is")
    assert not found, (
        "git vocabulary in what an application-facing handler prints (ADR 0034 D-m):\n  "
        + "\n  ".join(sorted(found)))


def test_every_task_and_project_command_is_classified_as_one_or_the_other():
    """A command added later is covered without anybody remembering this file exists.

    The two sets must PARTITION `grid task *` and `grid project *`: disjoint, and together the whole
    of both groups. Without this, a new command is simply absent from `APPLICATION_SURFACE` and the
    scan above says nothing about it — the failure mode `cli/dispatch.py` guards against with the
    same shape, for the same reason ("a test asserts this so a future command can never silently run
    local code in remote mode").
    """
    from cli.parser import build_parser

    groups = {name for name, _texts in _leaves(build_parser())
              if name.startswith("grid task ") or name.startswith("grid project ")}
    overlap = APPLICATION_SURFACE & GIT_PLANE
    assert not overlap, f"a command is on both lists, so its treatment is undecided: {sorted(overlap)}"
    unclassified = groups - APPLICATION_SURFACE - GIT_PLANE
    assert not unclassified, (
        "a task or project command is on neither list, so nothing decides whether it may speak git. "
        "Put it in `APPLICATION_SURFACE` (a person driving the app sees it) or in `GIT_PLANE` (it is "
        f"for somebody with a repository in front of them): {sorted(unclassified)}")
    stale = (APPLICATION_SURFACE | GIT_PLANE) - groups
    assert not stale, f"these are listed but no longer exist: {sorted(stale)}"


def test_every_command_an_application_drives_takes_json():
    """`docs/cli.md` claims *"Every state-reading command supports `--json`"*. Checked, not restated.

    ⚠️ Issue 46 asked for this claim to be VERIFIED rather than repeated, and the verification is
    what makes it worth anything: the sentence has been in that file since long before this plane
    existed, so it was a statement about a much smaller CLI that nobody had re-read since. It is true
    today — and a command added tomorrow without the flag makes it false silently, in the one mode
    the client uses for everything.
    """
    without = []
    for name, texts in _application_leaves():
        flags = {where for where, _text in texts}
        if "--json" not in flags:
            without.append(name)
    assert not without, (
        "these are on the application's surface and cannot answer a program: " + ", ".join(
            sorted(without)))
    # The floor, for the reason `test_the_scan_really_walked_the_surface` has one: a walk that
    # yielded nothing would satisfy every line above.
    assert len(_application_leaves()) >= _MINIMUM_COMMANDS


def test_argparse_is_the_only_thing_this_file_needs_to_know_about():
    """`_leaves` reaches into argparse's private `_name_parser_map`, as three other tests in this
    repository already do. Pinned so that an argparse release renaming it fails HERE, loudly, rather
    than by silently yielding no commands — which every assertion above would read as success."""
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    sub.add_parser("thing")
    action, = [a for a in parser._actions if hasattr(a, "_name_parser_map")]
    assert list(action._name_parser_map) == ["thing"]
