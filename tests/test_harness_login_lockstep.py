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
It reads its siblings through `tests/grid_src_repo.py`, like both of the other pins.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from tests.grid_src_repo import grid_apis_root

#: The path, as PRD D-1 fixes it. Written here rather than imported from either side, because a pin
#: that reads one side's constant and compares it to itself checks nothing.
CANONICAL_PATH = "/v1/grid/auth/harness"

#: The body key the CLI will send. A rename of this one is *not* silent — the control plane answers
#: 422 — but it is a 422 nobody can act on from the CLI's side, so it is pinned with the path.
CANONICAL_BODY_KEY = "harness_token"

_APIS_HANDLER = "grid_networks/handler.py"
_APIS_ROUTER = "router"
_APIS_REQUEST_MODEL = "HarnessAuthRequest"

#: The two sign-in paths this CLI demonstrably sends today (`remote/control_plane.py`). They are the
#: positive control for the literal scanner: without them, "this repository mentions no harness
#: route" is satisfied just as well by a scanner that has quietly stopped reading anything.
_DEVICE_FLOW_PATHS = frozenset({"/v1/grid/auth/device/start", "/v1/grid/auth/device/poll"})

#: Where this CLI's own source lives. Wider than `remote/` on purpose — issue 03 is free to put the
#: call somewhere else, and a scanner that only looked where the author expected would report the
#: half absent rather than compare it.
_CLI_PACKAGES = ("cli", "local", "remote", "shared")

_SKIP_NO_APIS = "the grid-apis worktree is not beside this one; the lockstep cannot be checked here"


def _apis_handler_tree() -> ast.Module:
    """grid-apis' route module, parsed rather than imported — separate installs, no import path.

    ⚠️ **Only "no such repository at all" skips; a missing module RAISES.** The resolver has already
    proved a `grid_networks` directory exists under that root, so a handler absent from it was
    renamed or moved — which is drift, exactly what this file is for. Reporting it as "grid-apis is
    not beside this one" would turn the pin off with a message blaming the wrong thing.
    """
    root = grid_apis_root()
    if root is None:
        pytest.skip(_SKIP_NO_APIS)
    source = root / _APIS_HANDLER
    if not source.exists():
        raise AssertionError(
            f"grid-apis is at {root} but has no {_APIS_HANDLER} — the module was renamed or moved, "
            f"so teach this check where it went rather than letting it skip")
    return ast.parse(source.read_text())


def _router_prefix(tree: ast.Module) -> str:
    """The prefix grid-apis mounts its grid router at, so this pin compares the WHOLE path.

    The decorator carries `/auth/harness` and the prefix carries `/v1/grid`; the CLI sends their
    concatenation, and either half moving breaks it identically. Reading only the decorator would
    leave a repointed `APIRouter(prefix=…)` — one line, no test in either repository — invisible.
    """
    for node in tree.body:
        targets = (
            node.targets if isinstance(node, ast.Assign)
            else [node.target] if isinstance(node, ast.AnnAssign) and node.value is not None
            else []
        )
        if not any(getattr(target, "id", None) == _APIS_ROUTER for target in targets):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        for keyword in call.keywords:
            if keyword.arg == "prefix" and isinstance(keyword.value, ast.Constant):
                return keyword.value.value
        raise AssertionError(
            f"grid-apis' `{_APIS_ROUTER}` no longer carries a literal `prefix=`, so this check "
            f"cannot read the path it serves — teach it the new shape rather than deleting it")
    raise AssertionError(
        f"grid-apis' {_APIS_HANDLER} no longer defines `{_APIS_ROUTER}` at module level — it was "
        f"renamed or moved, so teach this check where it went rather than deleting it")


def _posted_paths(tree: ast.Module) -> list[str]:
    """Every literal path grid-apis hangs a POST handler off, decorator by decorator.

    Matched on the decorator rather than the handler name so that renaming `auth_harness` — an
    ordinary refactor that moves no route — does not read as drift.

    ⚠️ BOTH function kinds: `async def` is an `ast.AsyncFunctionDef` and is NOT a subclass of
    `ast.FunctionDef`, and that module already spells many of its routes that way. Matching only the
    sync kind would make converting this one handler look like a deleted route.
    """
    return [
        decorator.args[0].value
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr == "post"
        and getattr(decorator.func.value, "id", None) == _APIS_ROUTER
        and decorator.args
        and isinstance(decorator.args[0], ast.Constant)
        and isinstance(decorator.args[0].value, str)
    ]


def _request_model_fields(tree: ast.Module) -> set[str]:
    """The keys grid-apis' request model declares — the body this CLI has to send."""
    classes = [
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == _APIS_REQUEST_MODEL
    ]
    assert len(classes) == 1, (
        f"expected exactly one `class {_APIS_REQUEST_MODEL}` in grid-apis' {_APIS_HANDLER}, found "
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
    tree = _apis_handler_tree()
    prefix = _router_prefix(tree)

    served = [prefix + path for path in _posted_paths(tree)]
    matches = [path for path in served if path == CANONICAL_PATH]

    assert len(matches) == 1, (
        f"grid-apis serves {len(matches)} POST {CANONICAL_PATH!r} (its router is mounted at "
        f"{prefix!r}); the sign-in routes it does serve are "
        f"{sorted(path for path in served if '/auth/' in path)}. The route was renamed, moved or "
        f"split — edit BOTH sides, and remember the control plane deploys first")


def test_the_control_plane_takes_the_body_key_this_cli_will_send():
    """A rename here answers 422 rather than 404 — loud, but from the wrong end.

    The CLI can say nothing useful about it: `harness_token` is the only key it sends, so a 422
    naming a field it has never heard of is a sentence about the control plane's vocabulary shown to
    somebody holding a perfectly good credential.
    """
    fields = _request_model_fields(_apis_handler_tree())

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
