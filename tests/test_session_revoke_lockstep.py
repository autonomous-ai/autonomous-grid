"""The session-revoke route, pinned across the repository boundary (`harness-grid-login` issue 07).

`grid logout --everywhere` takes back every grid session the account holds by POSTing to the
control plane, which bumps one number on one row. There is no import path between this CLI and
grid-apis, so the path literal is hand-duplicated and kept in step by editing both sides — and by
this file.

**The chain is control plane → this CLI, and it fails loudly in the only direction it can:** a
`grid` against a control plane that predates the route gets a bare 404, which `cli.auth` turns into
one sentence naming `grid logout`. So the rollout order (control plane first) is a deployment
convenience rather than a correctness requirement. What is NOT loud is a **rename**: rename it on
either side and both halves keep compiling, every suite in both repositories stays green, and every
`--everywhere` sign-out 404s in production — reported as "this control plane is too old" about a
control plane that is in fact newer than this CLI.

⚠️ Per this repository's rule for every cross-repo assertion, the grid-apis case **skips unless
that worktree sits beside this one** — i.e. it skips in CI, and a green CI proves nothing about it.
Run it locally, on a machine that has both.

The canonical path is written out here rather than imported from either side: a pin that reads one
side's constant and compares it to itself checks nothing. Both halves are compared to this literal,
so a rename has to be made in three places to go unnoticed — and the third is a test.
"""

from __future__ import annotations

from remote import control_plane
from tests import grid_apis_routes

#: The route, whole — grid-apis' router prefix plus its decorator's literal.
CANONICAL_PATH = "/v1/grid/auth/sessions/revoke"

#: The key on the reply that says the revocation happened. Its own pin because this CLI treats it
#: as a **postcondition**: a 200 without it is refused rather than reported as a sign-out, so a
#: rename here turns every successful revocation into a failure the person cannot act on.
CANONICAL_REPLY_KEY = "revoked"


def test_the_control_plane_serves_the_route_this_cli_calls():
    """Exactly one, because two would mean the route was split and this pin would be reading
    whichever came first — and none means it was renamed, which is the silent break the CLI meets
    in production as a 404 it blames on the control plane's age."""
    served = grid_apis_routes.served_post_paths()

    matches = [path for path in served if path == CANONICAL_PATH]

    assert len(matches) == 1, (
        f"grid-apis serves {len(matches)} POST {CANONICAL_PATH!r}; the auth routes it does serve "
        f"are {sorted(path for path in served if '/auth/' in path)}. The route was renamed, moved "
        f"or split — edit BOTH sides, and remember the control plane deploys first")


def test_this_cli_sends_the_canonical_path():
    """Needs no sibling worktree, so it is the half that runs in CI.

    It cannot see a rename on the far side on its own — that is what the case above is for — but it
    is what stops this repository's constant drifting quietly while the pin above keeps passing
    against a route nothing calls.
    """
    assert control_plane.SESSIONS_REVOKE_PATH == CANONICAL_PATH


def test_this_cli_checks_the_canonical_reply_key():
    assert control_plane.SESSIONS_REVOKE_KEY == CANONICAL_REPLY_KEY


def test_the_control_plane_answers_with_the_key_this_cli_demands():
    """The postcondition's far half, read off grid-apis' own `return` statement.

    A new key on an existing endpoint degrades silently, and this key is what stops this route's
    *newness* from being the only protection: dropped or renamed on the far side, every revocation
    that actually happened is reported here as one that did not.

    The value is checked too, and for identity with a literal `True` — because that is exactly how
    this CLI compares it (`is not True`), so a far end that answered `"revoked": 1` would satisfy
    a key-only pin and refuse every real sign-out.
    """
    replies = _revoke_reply_literals()

    assert replies.get(CANONICAL_REPLY_KEY) is True, (
        f"grid-apis' revoke handler returns {replies!r}, and this CLI compares "
        f"{CANONICAL_REPLY_KEY!r} for identity with `True` and refuses everything else — so a "
        f"rename, a drop or a computed value here turns every successful sign-out into a refusal")


def _revoke_reply_literals() -> dict[str, object]:
    """The literal `{key: value}` pairs grid-apis' revoke handler returns.

    Read through the AST rather than off the source text: `ast.unparse` normalises quoting, so a
    substring match against `'"revoked": True'` compares this test's typography to Python's
    rather than comparing either side's contract.

    The handler is located by its **decorator**, so renaming the function — an ordinary refactor
    that moves no route — does not read as drift.
    """
    import ast

    tree = grid_apis_routes.handler_tree()
    prefix = grid_apis_routes.router_prefix(tree)
    wanted = CANONICAL_PATH[len(prefix):]
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(_posts_to(decorator, wanted) for decorator in node.decorator_list):
            continue
        for statement in ast.walk(node):
            if isinstance(statement, ast.Return) and isinstance(statement.value, ast.Dict):
                return {
                    key.value: value.value
                    for key, value in zip(statement.value.keys, statement.value.values)
                    if isinstance(key, ast.Constant) and isinstance(value, ast.Constant)
                }
        raise AssertionError(
            f"grid-apis' handler for {wanted!r} returns no literal dict, so the postcondition key "
            f"cannot be read — teach this check the new shape rather than deleting it")
    raise AssertionError(
        f"grid-apis hangs no POST handler off {wanted!r} (its router is mounted at {prefix!r}), so "
        f"the reply key cannot be read — the route above is what to fix first")


def _posts_to(decorator, path: str) -> bool:
    import ast

    return (
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr == "post"
        and getattr(decorator.func.value, "id", None) == grid_apis_routes.ROUTER
        and bool(decorator.args)
        and isinstance(decorator.args[0], ast.Constant)
        and decorator.args[0].value == path
    )
