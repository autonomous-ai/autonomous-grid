"""The asleep code, pinned across the repository boundary (`idle-sleep` issue 02).

grid-apis' proxy answers a request that did not wake a SLEEPING grid with a 503 carrying
`code: grid_asleep`, and this CLI's provider parks on it instead of polling the grid every two seconds
(`remote/bringup`, `remote/serve`). There is no import path between the two, so the code is
hand-duplicated and kept in step by editing both sides — and by this file.

**Both skew directions degrade to today's behaviour**, so the rollout order (proxy first) is a
convenience: a provider too old to know the code reads the 503 as transient and keeps retrying, and a
provider against a proxy too old to send it does the same. What is NOT loud is a **rename**: renamed on
either side, both suites stay green and every provider goes back to hammering every sleeping grid it
serves — silently, since "retrying" is also what a provider does when everything is fine.

⚠️ Per this repository's rule for every cross-repo assertion, the grid-apis cases **skip unless that
worktree sits beside this one** — i.e. they skip in CI, and a green CI proves nothing about them. Run
them locally, on a machine that has both.

The canonical value is written out here rather than imported from either side: a pin that reads one
side's constant and compares it to itself checks nothing.
"""
from __future__ import annotations

import ast

import pytest

from remote import relay
from tests.grid_src_repo import grid_apis_root

CANONICAL_CODE = "grid_asleep"

PROXY_MODULE = "grid_proxy.py"
PROXY_CONSTANT = "GRID_ASLEEP_CODE"
SKIP_NO_APIS = "the grid-apis worktree is not beside this one; the lockstep cannot be checked here"


def _proxy_tree() -> ast.Module:
    """grid-apis' proxy, parsed rather than imported — separate installs, no import path.

    Only "no such repository" skips. A grid-apis checkout without the module means it was renamed or
    moved, which is drift, and skipping would turn the pin off blaming the wrong thing.
    """
    root = grid_apis_root()
    if root is None:
        pytest.skip(SKIP_NO_APIS)
    source = root / PROXY_MODULE
    if not source.exists():
        raise AssertionError(
            f"grid-apis is at {root} but has no {PROXY_MODULE} — teach this check where the proxy "
            f"went rather than letting it skip")
    return ast.parse(source.read_text())


def test_the_proxy_answers_with_the_code_this_cli_parks_on():
    values = [
        node.value.value
        for node in _proxy_tree().body
        if isinstance(node, ast.Assign)
        and any(getattr(target, "id", None) == PROXY_CONSTANT for target in node.targets)
        and isinstance(node.value, ast.Constant)
    ]
    assert values == [CANONICAL_CODE], (
        f"grid-apis' `{PROXY_CONSTANT}` is {values!r}. Renamed on one side, every provider goes back "
        f"to polling every sleeping grid every two seconds — edit BOTH sides")


def test_the_proxy_sends_the_code_beside_detail_where_this_cli_reads_it():
    """The shape is half the contract. This CLI reads the code at the TOP LEVEL of the answer
    (`relay._answer_code`); nested under `detail` — the task plane's shape — it would be a code this
    CLI never finds, and the provider would silently stop parking."""
    beside_detail = [
        node
        for node in ast.walk(_proxy_tree())
        if isinstance(node, ast.Dict)
        and "detail" in {key.value for key in node.keys if isinstance(key, ast.Constant)}
        and any(
            isinstance(key, ast.Constant) and key.value == "code"
            and isinstance(value, ast.Name) and value.id == PROXY_CONSTANT
            for key, value in zip(node.keys, node.values)
        )
    ]
    assert beside_detail, (
        f"grid-apis' proxy no longer builds an answer `{{'detail': …, 'code': {PROXY_CONSTANT}}}` — "
        f"moved or reshaped, this CLI stops finding the code")


def test_this_cli_parks_on_the_canonical_code():
    """Needs no sibling worktree, so it is the half that runs in CI."""
    assert relay.GRID_ASLEEP_CODE == CANONICAL_CODE
