"""grid-apis' routes, read out of its source — the one derivation, for every pin here.

Two lockstep suites need the same answer to the same question: *what whole paths does the control
plane actually serve?* The path is a concatenation — `APIRouter(prefix="/v1/grid")` in one place, a
`@router.post("/auth/…")` decorator in another — and either half moving breaks a caller
identically, so a reader that looked at only one of them would go quiet on half the drift.

One copy, here, for the reason `grid_src_repo` exists: two hand-written derivations drift exactly
like two hand-written constants, and a reader that drifts does not fail, it **skips**.

Parsed rather than imported: separate installs, no import path, and importing grid-apis' handler
would drag its whole dependency tree into this suite.
"""
from __future__ import annotations

import ast

import pytest

from tests.grid_src_repo import grid_apis_root

HANDLER = "grid_networks/handler.py"
ROUTER = "router"

#: The decorator names `APIRouter` hangs a route off. A method outside this set is a typo, never an
#: answer — see `decorated_paths`. FastAPI also exposes `api_route(path, methods=[…])`; grid-apis
#: uses none, and the day it does this reader must be taught about it rather than quietly missing
#: those routes, which is why the vocabulary is written down instead of inferred.
HTTP_METHODS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "trace"})

SKIP_NO_APIS = "the grid-apis worktree is not beside this one; the lockstep cannot be checked here"


def handler_tree() -> ast.Module:
    """grid-apis' route module, parsed rather than imported — separate installs, no import path.

    ⚠️ **Only "no such repository at all" skips; a missing module RAISES.** The resolver has already
    proved a `grid_networks` directory exists under that root, so a handler absent from it was
    renamed or moved — which is drift, exactly what these files are for. Reporting it as "grid-apis
    is not beside this one" would turn the pin off with a message blaming the wrong thing.
    """
    root = grid_apis_root()
    if root is None:
        pytest.skip(SKIP_NO_APIS)
    source = root / HANDLER
    if not source.exists():
        raise AssertionError(
            f"grid-apis is at {root} but has no {HANDLER} — the module was renamed or moved, "
            f"so teach this check where it went rather than letting it skip")
    return ast.parse(source.read_text())


def router_prefix(tree: ast.Module) -> str:
    """The prefix grid-apis mounts its grid router at, so a pin compares the WHOLE path.

    The decorator carries `/auth/harness` and the prefix carries `/v1/grid`; a CLI sends their
    concatenation, and either half moving breaks it identically. Reading only the decorator would
    leave a repointed `APIRouter(prefix=…)` — one line, no test in either repository — invisible.
    """
    for node in tree.body:
        targets = (
            node.targets if isinstance(node, ast.Assign)
            else [node.target] if isinstance(node, ast.AnnAssign) and node.value is not None
            else []
        )
        if not any(getattr(target, "id", None) == ROUTER for target in targets):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        for keyword in call.keywords:
            if keyword.arg == "prefix" and isinstance(keyword.value, ast.Constant):
                return keyword.value.value
        raise AssertionError(
            f"grid-apis' `{ROUTER}` no longer carries a literal `prefix=`, so this check "
            f"cannot read the path it serves — teach it the new shape rather than deleting it")
    raise AssertionError(
        f"grid-apis' {HANDLER} no longer defines `{ROUTER}` at module level — it was "
        f"renamed or moved, so teach this check where it went rather than deleting it")


def _reject_unknown_method(method: str) -> None:
    """One membership check, so the two readers cannot disagree about what a method is."""
    if method not in HTTP_METHODS:
        raise AssertionError(
            f"{method!r} is not a route decorator grid-apis' router carries — the known ones are "
            f"{sorted(HTTP_METHODS)}, all lowercase. Reading it would answer an empty list, which "
            f"is spelled the same as a route that was renamed")


def decorated_paths(tree: ast.Module, method: str) -> list[str]:
    """Every literal path grid-apis hangs a handler for `method` off, decorator by decorator.

    Matched on the decorator rather than the handler name so that renaming a handler — an ordinary
    refactor that moves no route — does not read as drift.

    ⚠️ BOTH function kinds: `async def` is an `ast.AsyncFunctionDef` and is NOT a subclass of
    `ast.FunctionDef`, and that module already spells many of its routes that way. Matching only the
    sync kind would make converting one handler look like a deleted route.

    ⚠️ **A method this reader does not know RAISES rather than answering `[]`.** `"POST"` is the
    natural spelling everywhere else in this repository, it matches no decorator, and an empty list
    is spelled exactly like *the route was renamed* — so a pin fed one either reports drift that is
    not there or, worse, counts nothing and reports an agreement it never checked. Both readings
    are wrong and neither is loud, which is why the vocabulary is a set rather than a comment.
    """
    _reject_unknown_method(method)
    return [
        decorator.args[0].value
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr == method
        and getattr(decorator.func.value, "id", None) == ROUTER
        and decorator.args
        and isinstance(decorator.args[0], ast.Constant)
        and isinstance(decorator.args[0].value, str)
    ]


def posted_paths(tree: ast.Module) -> list[str]:
    """`decorated_paths(tree, "post")` — the POST-only spelling this module shipped with.

    Kept rather than replaced: it is part of this module's existing surface, and taking a public
    name out of shared test infrastructure is a different change from adding one to it.
    """
    return decorated_paths(tree, "post")


def served_paths(method: str) -> list[str]:
    """The whole paths grid-apis answers `method` on — prefix and decorator, joined the way it
    serves them. Skips only when grid-apis is not beside this worktree.

    ⚠️ **The method is checked BEFORE `handler_tree()`, and the order is the point.** `handler_tree`
    skips when the sibling worktree is absent, which is what happens in CI — so validating after it
    would report a typo'd method as *"grid-apis is not beside this one"* everywhere the typo could
    still be caught cheaply. A programming error must not be reachable only on a developer's laptop.
    """
    _reject_unknown_method(method)
    tree = handler_tree()
    prefix = router_prefix(tree)
    return [prefix + path for path in decorated_paths(tree, method)]


def served_post_paths() -> list[str]:
    """`served_paths("post")` — the accessor `test_session_revoke_lockstep.py` and
    `test_harness_login_lockstep.py` already call, kept by that name so neither had to change."""
    return served_paths("post")
