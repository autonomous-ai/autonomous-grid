"""The harness sign-in route, pinned across the repository boundary (PRD `harness-grid-login`, D-1).

`grid login --harness` trades an Autonomous account token for a grid session by POSTing it to the
control plane, which answers the same `{session_token, user}` envelope `/auth/google` answers. There
is no import path between this CLI and grid-apis, so the path literal and the body key are
hand-duplicated and kept in step by editing both sides — and by this test.

**The chain is control plane → this CLI → the harness, and every step of it fails loudly**, which is
why the ordering is a deployment convenience rather than a correctness requirement: a `grid` CLI
against a control plane that predates the route gets a bare 404 it turns into one sentence naming
`grid login`. What is NOT loud is a **rename**: rename the route on either side and both halves keep
compiling, every suite in both repositories stays green, and the only thing that changes is that
every hand-off 404s in production. That is the failure this file exists to catch.

⚠️ Per this repository's rule for every cross-repo assertion, the grid-apis cases **skip unless that
worktree sits beside this one** — i.e. they skip in CI, and a green CI proves nothing about them. Run
it locally, on a machine that has both.

⚠️ **This CLI's half has not landed yet** (issue 03 owns `grid login --harness`), so
[test_the_cli_names_the_route_the_control_plane_serves] skips today and turns itself on the moment a
route literal mentioning the hand-off appears anywhere in this package tree. It needs no edit then —
and it is deliberately written to FAIL rather than skip if that literal lands spelled differently,
because a half-pin that goes quiet on the one change it guards is worse than no pin. The control
plane's own spelling is pinned unconditionally below, so the canonical path is asserted against a
real router today whichever way round the halves land.

Its own module rather than more of `tests/test_task_lease.py` (nothing here is about the task plane,
and that file's grid-src resolver names one worktree by absolute path) and rather than more of
`tests/test_os_grid_type_lockstep.py`, for the same reason that one is separate from the task plane's.
It reads its siblings through `tests/grid_src_repo.py`, like both of the other pins, and the
control plane's routes through `tests/grid_apis_routes.py`, which
`tests/test_session_revoke_lockstep.py` shares.
"""

from __future__ import annotations

import argparse
import ast
import os
import pathlib
import re
import subprocess
import sys

import pytest

from tests import grid_apis_routes
from tests.grid_src_repo import harness_root

#: The path, as PRD D-1 fixes it. Written here rather than imported from either side, because a pin
#: that reads one side's constant and compares it to itself checks nothing.
CANONICAL_PATH = "/v1/grid/auth/harness"

#: The body key the CLI will send. A rename of this one is *not* silent — the control plane answers
#: 422 — but it is a 422 nobody can act on from the CLI's side, so it is pinned with the path.
CANONICAL_BODY_KEY = "harness_token"

_APIS_REQUEST_MODEL = "HarnessAuthRequest"

#: The two sign-in paths this CLI demonstrably sends today (`remote/control_plane.py`). They are the
#: positive control for the literal scanner: without them, "this repository mentions no harness
#: route" is satisfied just as well by a scanner that has quietly stopped reading anything.
_DEVICE_FLOW_PATHS = frozenset({"/v1/grid/auth/device/start", "/v1/grid/auth/device/poll"})

#: Where this CLI's own source lives. Wider than `remote/` on purpose — issue 03 is free to put the
#: call somewhere else, and a scanner that only looked where the author expected would report the
#: half absent rather than compare it.
_CLI_PACKAGES = ("cli", "local", "remote", "shared")


def _request_model_fields(tree: ast.Module) -> set[str]:
    """The keys grid-apis' request model declares — the body this CLI has to send."""
    classes = [
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == _APIS_REQUEST_MODEL
    ]
    assert len(classes) == 1, (
        f"expected exactly one `class {_APIS_REQUEST_MODEL}` in grid-apis' {grid_apis_routes.HANDLER}, found "
        f"{len(classes)} — the model moved or was renamed, so teach this check where it went rather "
        f"than letting the pin read nothing")
    return {
        node.target.id
        for node in classes[0].body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }


def _cli_path_literals() -> dict[str, list[str]]:
    """Every bare control-plane path spelled anywhere in this CLI, as `{path: [where]}`.

    "Bare" is the filter that keeps prose out: a path literal carries no whitespace, so a docstring
    or a message that merely *names* a route — and there are several, including the sentence a 404
    turns into — is not mistaken for the CLI sending one. Interpolated paths
    (`f"/v1/grid/tokens/{network_id}"`) are read through their leading literal segment, which is
    where a rename of this route would land if issue 03 ever builds the path that way.
    """
    here = pathlib.Path(__file__).resolve().parent.parent
    found: dict[str, list[str]] = {}

    def _record(value: object, where: str) -> None:
        if not isinstance(value, str) or value.split() != [value] or not value:
            return
        if "/v1/grid" not in value and "auth/" not in value:
            return
        found.setdefault(value, []).append(where)

    for package in _CLI_PACKAGES:
        for module in sorted((here / package).rglob("*.py")):
            tree = ast.parse(module.read_text())
            where = str(module.relative_to(here))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant):
                    _record(node.value, where)
                elif isinstance(node, ast.JoinedStr) and node.values:
                    lead = node.values[0]
                    if isinstance(lead, ast.Constant):
                        _record(lead.value, where)
    return found


def _harness_path_literals() -> dict[str, list[str]]:
    """The subset that names this hand-off — however it ends up spelled."""
    return {
        path: where for path, where in _cli_path_literals().items()
        if "harness" in path and "auth" in path
    }


# --- the control, which needs no sibling ----------------------------------------------------------


def test_the_scanner_still_finds_the_sign_in_paths_this_cli_already_sends():
    """The positive control for [test_the_cli_names_the_route_the_control_plane_serves].

    That check's quiet state — "this repository names no harness route yet" — is reached just as
    well by a scanner that has stopped reading anything at all: a package renamed out of
    `_CLI_PACKAGES`, a literal built some way `ast` no longer sees. Then the CLI half could land
    misspelled and the pin would go on skipping. These two paths are sent by `remote/control_plane`
    today, so if they stop being found it is this harness that broke, and it says so here rather
    than by falling quiet one test down.
    """
    literals = _cli_path_literals()

    missing = _DEVICE_FLOW_PATHS - set(literals)
    assert not missing, (
        f"this CLI's own device-flow paths {sorted(missing)} are no longer found by the literal "
        f"scanner, so it can no longer tell whether the harness route is spelled correctly either — "
        f"fix the scanner (or teach it where those paths moved) before trusting anything below")


# --- the control plane's own spelling -------------------------------------------------------------


def test_the_control_plane_serves_the_sign_in_route_this_cli_will_call():
    """The path, whole: the router's prefix plus the decorator's literal.

    Exactly one, because two would mean the route was split and this pin would be reading whichever
    came first — and none means it was renamed, which is the silent break the CLI meets in
    production as a 404 on every hand-off.
    """
    served = grid_apis_routes.served_post_paths()
    matches = [path for path in served if path == CANONICAL_PATH]

    assert len(matches) == 1, (
        f"grid-apis serves {len(matches)} POST {CANONICAL_PATH!r}; the sign-in routes it does "
        f"serve are {sorted(path for path in served if '/auth/' in path)}. The route was renamed, "
        f"moved or split — edit BOTH sides, and remember the control plane deploys first")


def test_the_control_plane_takes_the_body_key_this_cli_will_send():
    """A rename here answers 422 rather than 404 — loud, but from the wrong end.

    The CLI can say nothing useful about it: `harness_token` is the only key it sends, so a 422
    naming a field it has never heard of is a sentence about the control plane's vocabulary shown to
    somebody holding a perfectly good credential.
    """
    fields = _request_model_fields(grid_apis_routes.handler_tree())

    assert CANONICAL_BODY_KEY in fields, (
        f"grid-apis' {_APIS_REQUEST_MODEL} declares {sorted(fields)}, not {CANONICAL_BODY_KEY!r} — "
        f"the body key was renamed, so edit both sides")


# --- this CLI's half, once it exists --------------------------------------------------------------


def test_the_cli_names_the_route_the_control_plane_serves():
    """The lockstep itself, and it turns itself on when issue 03 lands.

    Skipping while this repository has no half is the honest state — there is nothing to compare —
    but it is only honest because the skip is *narrow*: any path literal that mentions the hand-off
    at all makes this run, so a half that lands misspelled fails here instead of extending the
    silence. What it cannot see is a path assembled from pieces at runtime; nothing in
    `remote/control_plane` is written that way, and the control above fails if that stops being true
    for the paths this CLI already sends.
    """
    named = _harness_path_literals()
    if not named:
        pytest.skip(
            "this CLI has no `grid login --harness` yet (issue 03); the control plane's own "
            "spelling is pinned by the cases above")

    wrong = {path: where for path, where in named.items() if path != CANONICAL_PATH}
    assert not wrong, (
        f"this CLI spells the harness sign-in route {sorted(wrong)} at "
        f"{sorted({place for places in wrong.values() for place in places})}, but the control plane "
        f"serves {CANONICAL_PATH!r} — every hand-off would 404. Edit both sides, and read the "
        f"lockstep register's entry before deciding which spelling is right")


# --- the harness's half: an ARGV rather than a route ----------------------------------------------
#
# The chain is control plane → this CLI → the harness, and the cases above pin only its first two
# links. The third is not a path in a request, it is the argv `harness grid login` spawns — and it is
# hand-duplicated in exactly the same way, with exactly the same silence when it drifts.
#
# ⚠️ **A rename of `--harness` is the dangerous edit, and it is silent in a way that BLAMES THE WRONG
# REPOSITORY.** Rename it here alone and: this suite stays green (nothing else reads the flag), the
# harness suite stays green (its fake `grid` enforces the harness's own copy of the spelling), and
# every hand-off in production reaches argparse, is refused as an unknown flag, and exits 2 — which
# the harness reports, correctly by its own lights, as *your `grid` CLI is too old*. The person then
# updates a `grid` that was already current.

#: The argv the harness sends, as this CLI's parser must accept it. Written out rather than imported
#: from either side: a pin that reads one side's constant and compares it to itself checks nothing.
CANONICAL_HANDOFF_ARGV = ("login", "--harness")

#: What argparse exits on an unknown flag, and therefore what the harness is entitled to read as "the
#: installed `grid` predates this feature". It fires BEFORE the handler, so it is the loud failure
#: that makes the rollout order a deployment convenience rather than a correctness requirement.
ARGPARSE_USAGE_EXIT = 2

_HARNESS_HANDOFF = "cli/src/lib/gridHandoff.ts"

#: Where the harness locates the credential store this CLI writes. See the sign-out section at the
#: foot of this file.
_HARNESS_CREDENTIALS = "cli/src/lib/gridCredentials.ts"

#: The harness is TypeScript, so its half is read with a regex where grid-apis' is read with `ast`.
#: Anchored on `export const NAME = '<value>'` — the shape those modules actually use — and a miss
#: RAISES rather than skips, for the same reason the grid-apis handler's does.
_TS_CONST = r"export const {name}\s*=\s*['\"]([^'\"]+)['\"]"


def _harness_source(module: str = _HARNESS_HANDOFF) -> str:
    """One harness module, or a skip when that repository is not beside this one.

    ⚠️ Only "no such repository at all" skips. The resolver has already proved a `cli/src` directory
    exists under that root, so a module absent from it was renamed or moved — which is drift, the
    thing this file is for — and reporting it as "the harness is not beside this one" would turn the
    pin off with a message blaming the wrong thing.
    """
    root = harness_root()
    if root is None:
        pytest.skip("the autonomous-harness worktree is not beside this one; its half cannot be checked here")
    source = root / module
    if not source.exists():
        raise AssertionError(
            f"autonomous-harness is at {root} but has no {module} — the module was renamed "
            f"or moved, so teach this check where it went rather than letting it skip")
    return source.read_text()


def _ts_const(source: str, name: str) -> str:
    """One exported string constant out of the harness's TypeScript."""
    match = re.search(_TS_CONST.format(name=name), source)
    if match is None:
        raise AssertionError(
            f"autonomous-harness' {_HARNESS_HANDOFF} no longer exports a literal `{name}`, so this "
            f"check cannot read the argv it sends — teach it the new shape rather than deleting it")
    return match.group(1)


def _login_parser() -> argparse.ArgumentParser:
    """This CLI's own `grid login` subparser, taken off the real parser rather than rebuilt."""
    from cli.parser import build_parser

    for action in build_parser()._subparsers._group_actions:  # type: ignore[union-attr]
        if "login" in getattr(action, "choices", {}):
            return action.choices["login"]
    raise AssertionError("this CLI's parser no longer has a `login` subcommand at all")


# --- what this CLI offers, which needs no sibling -------------------------------------------------


def test_this_cli_accepts_the_argv_the_harness_sends():
    """The positive control, and half the lockstep: the real parser, not a re-derivation of it.

    Parsed rather than grepped, so a `--harness` that survives only in a docstring — or one moved
    into a group that makes it conflict with something the harness also sends — fails here.
    """
    parsed = _login_parser().parse_args(list(CANONICAL_HANDOFF_ARGV[1:]) + ["--json"])

    assert getattr(parsed, "harness", False) is True, (
        f"`grid {' '.join(CANONICAL_HANDOFF_ARGV)} --json` no longer sets `harness` on this CLI, so "
        f"every hand-off from `harness grid login` would sign in the wrong way or not at all")


def test_an_unknown_flag_on_login_exits_two():
    """The meaning the harness reads off the exit code, asserted against the real parser.

    Not a tautology about argparse: it pins that `grid login` still *reaches* argparse for an unknown
    flag, rather than growing a hand-rolled pre-parse that would exit 1 and take the distinction away.
    """
    with pytest.raises(SystemExit) as refused:
        _login_parser().parse_args(["--a-flag-this-cli-does-not-have"])

    assert refused.value.code == ARGPARSE_USAGE_EXIT, (
        f"an unknown flag on `grid login` now exits {refused.value.code}, not {ARGPARSE_USAGE_EXIT} "
        f"— `harness grid login` reads {ARGPARSE_USAGE_EXIT} as 'this `grid` is too old' and would "
        f"report a plain failure instead")


# --- the harness's half, once that worktree is beside this one ------------------------------------


def test_the_harness_spawns_the_argv_this_cli_accepts():
    """The lockstep itself: the flag the harness spells against the flag this CLI declares."""
    source = _harness_source()

    flag = _ts_const(source, "GRID_HANDOFF_FLAG")
    binary = _ts_const(source, "GRID_BINARY")

    assert (binary, flag) == ("grid", CANONICAL_HANDOFF_ARGV[1]), (
        f"autonomous-harness spawns `{binary} … {flag}` but this CLI declares "
        f"`{CANONICAL_HANDOFF_ARGV[1]}` on `{CANONICAL_HANDOFF_ARGV[0]}` — every hand-off would be "
        f"refused as an unknown flag, and the harness would report THIS CLI as out of date. Edit "
        f"both sides")


def test_the_harness_still_reads_argparse_s_refusal_as_an_outdated_cli():
    """The other half of the same seam: the number, not the spelling.

    A harness that stopped reading 2 specially would turn the one loud failure in this chain into a
    generic one — the person is told the hand-off failed, and nothing tells them their `grid` is the
    reason.
    """
    source = _harness_source()

    match = re.search(r"ARGPARSE_USAGE_EXIT\s*=\s*(\d+)", source)
    assert match is not None, (
        f"autonomous-harness' {_HARNESS_HANDOFF} no longer names the argparse exit code it reads as "
        f"an outdated CLI — teach this check the new shape rather than deleting it")
    assert int(match.group(1)) == ARGPARSE_USAGE_EXIT, (
        f"autonomous-harness reads exit {match.group(1)} as 'this `grid` is too old', but argparse "
        f"refuses an unknown flag with {ARGPARSE_USAGE_EXIT}")


# --- what exit 2 must NOT mean --------------------------------------------------------------------
#
# The harness reads 2 off `grid login --harness` as "the installed `grid` predates the flag", and
# that reading is sound only while 2 has exactly ONE cause on this path.
#
# ⚠️ **2 is not a free number in this CLI.** `cli/_main.py` documents it as this CLI's code for *not
# finished yet, ask again* (issue 32) — a client polling on it retries — and the comment there
# records that argparse's own `SystemExit(2)` colliding with that meaning was already found once, in
# review, on this side. The two do not collide on `login` today: `cmd_login` returns 0, or raises a
# `SystemExit` carrying a sentence, which the interpreter turns into 1. Nothing pinned that, and the
# cost of it drifting is paid one repository over — the harness would tell somebody to update a
# `grid` that is perfectly current, and say nothing about the refusal they actually hit.

_LOGIN_MODULE = pathlib.Path(__file__).resolve().parent.parent / "cli" / "auth.py"


def _login_exit_status(argv: list[str], *, home: pathlib.Path) -> int:
    """The exit status a shell would see, MEASURED by running this CLI rather than modelled.

    A `SystemExit` carrying a sentence exits 1 while `.code` holds the string, so reading `.code` in
    process would be re-deriving the interpreter's own rule and calling it a measurement. The harness
    reads a real process's status; so does this.

    `--remote` because these are remote-mode commands and the mode is a stored setting: a `GRID_HOME`
    the test just made has none, and this pin is about exit codes rather than about mode dispatch.
    """
    return subprocess.run(
        [sys.executable, "-c", "import sys; from cli import main; sys.exit(main(sys.argv[1:]))", *argv],
        cwd=str(pathlib.Path(__file__).resolve().parent.parent),
        env={**os.environ, "GRID_HOME": str(home)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,  # a non-zero status IS the measurement here
    ).returncode


def test_an_unknown_flag_on_login_really_reaches_the_harness_as_two(tmp_path):
    """The positive control for the case below, and the ONE thing 2 is allowed to mean.

    Without it, "no refusal exits 2" is satisfied just as well by a probe that could never observe a
    2 at all — a wrong `cwd`, an import that fails, a runner that swallows the status. This is also
    the closest thing there is to a test of what an OLD `grid` does with `--harness`, since an
    unknown flag is exactly what one would be.
    """
    assert _login_exit_status(["--remote", "login", "--a-flag-this-build-does-not-have"],
                              home=tmp_path) == ARGPARSE_USAGE_EXIT


def test_a_refusal_of_the_hand_off_does_not_spend_that_code(tmp_path):
    """An ordinary refusal must not look like an outdated CLI.

    Empty stdin is the refusal that needs no network and no stubbing — it is decided locally, before
    anything is sent — which is what lets this run the real binary end to end rather than a double.
    """
    status = _login_exit_status(["--remote", "login", "--harness"], home=tmp_path)

    assert status == 1, (
        f"`grid login --harness` refused with exit {status}. If that is {ARGPARSE_USAGE_EXIT}, every "
        f"harness hand-off now reports THIS CLI as out of date instead of the refusal the person "
        f"actually hit")


def test_the_login_path_names_no_exit_code_of_its_own():
    """The guard for a refusal nobody has written yet, which the case above cannot enumerate.

    That one drives the refusals that exist. This one fails on a NEW one that spends a status
    directly — `SystemExit(2)`, `sys.exit(2)`, or a handler returning 2 — which is how the
    ask-again meaning would arrive on this path, and it would arrive green.
    """
    tree = ast.parse(_LOGIN_MODULE.read_text())

    def is_the_code(node: ast.expr | None) -> bool:
        """⚠️ `bool` is a subclass of `int` and `True == 1`, so a bare `==` would read `return True`
        as an exit status the day this constant is a 1 rather than a 2."""
        return (isinstance(node, ast.Constant) and isinstance(node.value, int)
                and not isinstance(node.value, bool) and node.value == ARGPARSE_USAGE_EXIT)

    spent = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and is_the_code(node.value):
            spent.append(f"`return {ARGPARSE_USAGE_EXIT}` at line {node.lineno}")
        if isinstance(node, ast.Call) and node.args and is_the_code(node.args[0]) \
                and (getattr(node.func, "id", None) or getattr(node.func, "attr", None)) in {"SystemExit", "exit"}:
            spent.append(f"`SystemExit`/`exit`({ARGPARSE_USAGE_EXIT}) at line {node.lineno}")

    assert not spent, (
        f"cli/auth.py now spends exit {ARGPARSE_USAGE_EXIT} itself ({', '.join(spent)}), which is "
        f"the code `harness grid login` reads as 'this `grid` is too old'. That refusal would be "
        f"reported to the person as an outdated CLI. Use 1 — this CLI's `SystemExit(<sentence>)` "
        f"idiom — or change both sides")


# --- signing out: a FILESYSTEM layout rather than a route or an argv ------------------------------
#
# `harness logout` detaches the harness alone. It deliberately does not cascade into the grid — the
# grid sign-out can refuse over a running serve child, and the store may predate the harness entirely
# — so all it owes a person is one sentence naming `harness grid logout` when a grid sign-in is still
# on the machine (PRD `harness-grid-login` D-8). To decide whether to say it, the harness looks for
# THIS CLI's credential store, and it reaches it by rebuilding the path from three literals of its
# own: the environment variable, the default directory under `$HOME`, and the file name.
#
# ⚠️ **This one degrades in SILENCE, and the silence is the whole failure.** Move the store here —
# rename `credentials.toml`, change the `~/.grid` default, read a differently-named variable — and
# the harness finds nothing, which is spelled exactly like a machine that was never signed in to a
# grid. `harness logout` then stops warning, both suites stay green, and a long-lived credential is
# left behind without a word. There is no loud direction to fall back on: nothing 404s, nothing
# exits 2, no postcondition can be checked in a reply, because there is no reply.

#: This CLI's own accessor, exercised rather than restated. A pin that spelled `credentials.toml`
#: here and compared it to the harness would check the two against a third copy nobody ships.
def _cli_credentials_file():
    from shared import paths

    return paths.credentials_file()


def test_the_harness_reads_the_environment_variable_that_moves_this_cli_s_store(tmp_path, monkeypatch):
    """`GRID_HOME` relocates the whole of this CLI's state, and the harness has to follow it.

    Measured through `shared.paths` under an env this test sets, so what is compared is where this
    CLI actually puts the file — not a second spelling of the path written out here.
    """
    source = _harness_source(_HARNESS_CREDENTIALS)
    variable = _ts_const(source, "GRID_HOME_ENV")
    filename = _ts_const(source, "GRID_CREDENTIALS_FILE")

    monkeypatch.setenv(variable, str(tmp_path / "elsewhere"))

    assert _cli_credentials_file() == tmp_path / "elsewhere" / filename, (
        f"autonomous-harness looks for `${{{variable}}}/{filename}`, but this CLI puts its "
        f"credential store at {_cli_credentials_file()} — `harness logout` would find nothing and "
        f"silently stop telling anybody their grid sign-in is still on the machine")


def test_the_harness_falls_back_where_this_cli_falls_back(tmp_path, monkeypatch):
    """With no `GRID_HOME`, both sides must land on the same directory under the user's home.

    The ordinary case, and the one a developer never sets an environment variable for — so if only
    the override above were pinned, the default could move and the pin would stay green.
    """
    source = _harness_source(_HARNESS_CREDENTIALS)
    variable = _ts_const(source, "GRID_HOME_ENV")
    default_dir = _ts_const(source, "GRID_HOME_DEFAULT")
    filename = _ts_const(source, "GRID_CREDENTIALS_FILE")

    monkeypatch.delenv(variable, raising=False)
    # And by this CLI's own name for it, so a developer's shell cannot decide what this measures. Not
    # a second spelling of the pin — the assertion below still compares the harness's literal against
    # a measured path, and the case above is what fails if the two names ever diverge.
    monkeypatch.delenv("GRID_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert _cli_credentials_file() == tmp_path / default_dir / filename, (
        f"autonomous-harness falls back to `~/{default_dir}/{filename}`, but with no {variable} set "
        f"this CLI puts its credential store at {_cli_credentials_file()} — `harness logout` would "
        f"go quiet on every machine that has not moved GRID_HOME, which is most of them")


def test_the_harness_expands_a_leading_tilde_because_this_cli_does(tmp_path, monkeypatch):
    """`grid_home()` runs `expanduser()` over `GRID_HOME`, and the harness now mirrors that.

    A shell expands `~` at the point of assignment, so this only bites where nothing does — a systemd
    unit, a Docker `ENV`, a `.env` file, a CI variable. Drop the `expanduser()` here and the harness
    is suddenly the one resolving a value this CLI takes literally, which puts the two in different
    directories and takes `harness logout`'s warning away in silence.

    Not a test of `pathlib`: what it pins is that this CLI still *calls* it on that value.
    """
    source = _harness_source(_HARNESS_CREDENTIALS)
    variable = _ts_const(source, "GRID_HOME_ENV")
    filename = _ts_const(source, "GRID_CREDENTIALS_FILE")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(variable, "~/grid-state")

    assert _cli_credentials_file() == tmp_path / "grid-state" / filename, (
        f"a `{variable}` of '~/grid-state' puts this CLI's store at {_cli_credentials_file()}. The "
        f"harness resolves the leading `~` against $HOME, so the two would look in different places "
        f"and `harness logout` would go quiet about a credential that is there")
