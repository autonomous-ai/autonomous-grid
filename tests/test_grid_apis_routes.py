"""The route reader itself, driven against a synthetic module (`billing-activation` issue 01).

`tests/grid_apis_routes.py` is the one derivation every lockstep pin here uses to answer *what
whole paths does the control plane actually serve?* Until this slice it could answer that for
**POST only**, and the billing seam sits on three methods — a POST report, two GET reads, and a PUT
toggle. So it gains a method-generic reader and keeps the POST accessor as a wrapper.

⚠️ **These cases parse a module written out below rather than grid-apis' own.** That is deliberate,
and it is the only way this file can hold a *negative* case honestly: a reader that answered
nothing at all would satisfy "a PUT route is not reported for GET" against any real source, and it
would also satisfy it against a grid-apis that is not beside this worktree. The synthetic module
carries one route per method, so every negative here has a positive beside it proving the reader
can produce the other answer.

Two cases at the end DO need the sibling, and say so by skipping: they read whole paths off
grid-apis itself and compare them to literals written here, because the prefix is the half a
synthetic module cannot vouch for.
"""

from __future__ import annotations

import ast

import pytest

from tests import grid_apis_routes

#: One route per method, plus the two shapes that have caught this reader out before: an
#: `async def` handler (not an `ast.FunctionDef`) and a decorator on some *other* object.
SYNTHETIC_HANDLER = '''
from fastapi import APIRouter

router = APIRouter(prefix="/v1/grid")
other = APIRouter(prefix="/nope")


@router.post("/internal/usage")
def report_usage():
    ...


@router.get("/internal/min-balance")
async def internal_min_balance():
    ...


@router.put("/managed-networks/{network_id}/billing-mode")
def set_billing_mode():
    ...


@other.put("/somewhere-else")
def not_ours():
    ...
'''


@pytest.fixture()
def tree() -> ast.Module:
    return ast.parse(SYNTHETIC_HANDLER)


def test_a_put_route_is_read_by_the_method_generic_accessor(tree):
    """The whole reason this slice exists: three of the billing seam's five values are not POSTs."""
    assert grid_apis_routes.decorated_paths(tree, "put") == [
        "/managed-networks/{network_id}/billing-mode"
    ]


def test_a_get_route_is_read_even_though_its_handler_is_async(tree):
    """`async def` is an `ast.AsyncFunctionDef` and is NOT a subclass of `ast.FunctionDef`.

    grid-apis spells many of its routes that way, so matching only the sync kind would make
    converting one handler look like a deleted route.
    """
    assert grid_apis_routes.decorated_paths(tree, "get") == ["/internal/min-balance"]


def test_one_methods_routes_are_not_reported_for_another(tree):
    """The negative, with the two positives above as its control.

    Without them this assertion passes for a reader that answers nothing at all — which is the
    shape a pin fails *open* in: an empty list reads as "the route is gone" to a caller counting
    matches, and as "nothing to check" to one that is not.
    """
    posts = grid_apis_routes.decorated_paths(tree, "post")

    assert posts == ["/internal/usage"]
    assert "/internal/min-balance" not in posts
    assert "/managed-networks/{network_id}/billing-mode" not in posts


def test_another_routers_routes_are_not_reported(tree):
    """Only the module-level `router` this repository's pins name is read.

    grid-apis mounts more than one `APIRouter`, and a path served under a different prefix is a
    different whole path — reporting it here would make a pin pass against a route no caller of
    ours can reach.

    The control is in the same assertion rather than a sibling test: `put` has to come back with
    *our* router's route, or a reader that answers nothing satisfies the exclusion for free.
    """
    puts = grid_apis_routes.decorated_paths(tree, "put")

    assert puts == ["/managed-networks/{network_id}/billing-mode"]
    assert "/somewhere-else" not in puts


def test_a_method_the_reader_does_not_know_RAISES(tree):
    """⚠️ The typo this reader must not answer quietly.

    `decorated_paths(tree, "POST")` — the natural spelling, since that is how a method is written
    everywhere else — matches no decorator, and an empty list is indistinguishable from *the route
    was renamed*. A pin fed one reports drift that is not there, or, where it counts nothing,
    reports agreement it never checked. So an unknown method is a programming error and says so.
    """
    with pytest.raises(AssertionError, match="POST"):
        grid_apis_routes.decorated_paths(tree, "POST")

    with pytest.raises(AssertionError, match="fetch"):
        grid_apis_routes.decorated_paths(tree, "fetch")


def test_the_post_accessor_answers_exactly_what_the_generic_one_does(tree):
    """The wrapper is the contract with the suites that already use it: same answer, same order."""
    assert grid_apis_routes.posted_paths(tree) == grid_apis_routes.decorated_paths(tree, "post")


def test_a_method_the_reader_does_not_know_RAISES_BEFORE_it_needs_the_sibling(monkeypatch):
    """⚠️ The guard has to fire where grid-apis is **absent**, which is where CI runs.

    `served_paths` reaches for the sibling worktree, and `handler_tree()` *skips* when it is not
    there. Validate the method after that call and a typo'd one is reported as "the grid-apis
    worktree is not beside this one" on every machine that could still have caught it cheaply — a
    programming error reachable only on a developer's laptop, in a module written to stop that.

    ⚠️ **The absence has to be forced, or this case proves nothing here.** Run on a machine that
    *has* the sibling — which is the one this feature's worktrees were cut on — the reader raises
    from either position and the ordering is invisible. So the resolver is patched to answer *no
    such repository*, with the positive control below proving the patch really produced that state.

    ⚠️ And the failure is caught as a `BaseException`: `pytest.skip` raises one, so a misplaced
    guard would make this case **skip** rather than fail — the quiet outcome, in the assertion
    written to stop a quiet outcome.
    """
    monkeypatch.setattr(grid_apis_routes, "grid_apis_root", lambda: None)

    with pytest.raises(BaseException) as caught:  # a skip is a BaseException — see the docstring
        grid_apis_routes.served_paths("POST")

    assert isinstance(caught.value, AssertionError), (
        f"served_paths reached for grid-apis BEFORE validating the method — it raised "
        f"{type(caught.value).__name__}, which on a machine without the sibling worktree is a skip, "
        f"so a typo'd method would never be reported anywhere CI could see it")
    assert "POST" in str(caught.value)


def test_the_forced_absence_really_does_make_the_reader_skip(monkeypatch):
    """The positive control for the case above: proves the patch produces the CI state.

    Without it that case passes for a resolver patch that did nothing at all, and the ordering it
    exists to pin would go unchecked while reading as covered.
    """
    monkeypatch.setattr(grid_apis_routes, "grid_apis_root", lambda: None)

    with pytest.raises(BaseException) as caught:  # a skip is not an `Exception`
        grid_apis_routes.served_paths("post")

    assert type(caught.value).__name__ == "Skipped"


def test_the_reader_finds_whole_paths_this_CLI_actually_calls_on_GET_and_PUT():
    """The end-to-end half, and the reason this slice exists: the billing seam is not all POSTs.

    Two paths written out here rather than derived — `remote/control_plane.py` sends both, and
    comparing a *literal* is the only way this can fail. Deriving the expectation from the same
    prefix the reader joined on would be true by construction: a repointed `APIRouter(prefix=…)`
    would move both sides together and the assertion would never notice.

    ⚠️ These are **not** lockstep pins for those two routes and must not be read as ones — nothing
    here checks a request body or a reply key. What they check is that `served_paths` answers whole,
    joined paths for a non-POST method at all, which is the property every billing pin will rest on.
    """
    assert "/v1/grid/tokens" in grid_apis_routes.served_paths("get")
    assert "/v1/grid/networks/{network_id}/router/advisors" in grid_apis_routes.served_paths("put")


def test_the_post_wrapper_answers_over_the_real_control_plane_too():
    """`served_post_paths()` is what the two existing pins call, and this slice moved its body.

    Compared against a literal for the same reason as the case above: asserting it equals
    `served_paths("post")` would be `f() == f()`, since the wrapper is that call.
    """
    assert "/v1/grid/auth/sessions/revoke" in grid_apis_routes.served_post_paths()
