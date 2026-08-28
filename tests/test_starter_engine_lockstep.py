"""The set of grid-run service kinds, pinned across the repo boundary (ADR 0038 D-c).

A grid created from the web front end gets a **starter engine** stood up for it: the control plane
spawns this CLI into a per-grid home and joins it with a credential the member never holds. The
relay has to know which node that is — it ranks its models last (D-i) and counts its traffic against
the grid's allowance (D-a) — and it recognises it by the service kind the node advertises, compared
for equality against a closed set it holds its own copy of.

This CLI is where the authority lives: `ApiWhitelist.member_joinable` is the field that decides who
may join a kind, and a kind the grid stands up on a member's behalf is exactly one nobody can join.
There is no import path between the two repositories, so the copies are kept in lockstep by editing
both sides — and by this test.

⚠️ **It is the only thing that can catch D-c's failure, and that failure is SILENT.** A second
grid-run kind added here and not to the relay is not an error anywhere: the node registers, serves,
ranks first on free capacity, and is counted against nothing. The invoice is where it shows up.

⚠️ Per this repository's rule for every cross-repo assertion, this **skips unless the grid-src
worktree sits beside this one** — i.e. it skips in CI, and a green CI proves nothing about it. Run
it locally, on a machine that has both.

Its own module rather than more of `tests/test_task_lease.py`, which carries the task plane's
lockstep checks and is already this repository's second-largest suite. Nothing here is about tasks,
and its grid-src resolver is different: that file names one worktree by absolute path, which is why
its checks skip in every *other* worktree. This one derives the sibling from its own location.
"""

import os
import pathlib

import pytest

# The grid-src module holding the relay's copy, and the name of the copy inside it.
_RELAY_MODULE = "starter_engine.py"
_RELAY_CONSTANT = "GRID_RUN_ENGINE_KINDS"


def _grid_src_private_server():
    """grid-src's `private_server` package, or ``None`` when this machine does not have it.

    Derived from THIS file's location rather than written out, so the check runs in whichever
    worktree it is checked out into instead of skipping everywhere but one. The convention is
    `<repo>-feats/<slug>` beside `<repo>`, so a worktree of `autonomous-grid` at
    `…/autonomous-grid-feats/<slug>` looks for `…/grid-src-feats/<slug>` first and falls back to the
    main `…/grid-src` checkout.

    `GRID_SRC_REPO` — the cross-repo E2E's own override — wins over both, so a machine that lays the
    repositories out differently can still run this.
    """
    override = os.environ.get("GRID_SRC_REPO")
    if override:
        return pathlib.Path(override) / "grid_cli" / "private_server"

    here = pathlib.Path(__file__).resolve().parent.parent  # the autonomous-grid checkout
    projects = here.parent
    candidates = []
    if projects.name == "autonomous-grid-feats":
        candidates.append(projects.parent / "grid-src-feats" / here.name)
        candidates.append(projects.parent / "grid-src")
    else:
        candidates.append(projects / "grid-src")
    for candidate in candidates:
        if (candidate / "grid_cli" / "private_server").is_dir():
            return candidate / "grid_cli" / "private_server"
    return None


def _relay_grid_run_kinds():
    """The relay's copy of the set, parsed out of grid-src rather than imported.

    The two repositories are separate installs with no import path between them. Parsed with `ast`,
    and every shape this helper does not understand is an assertion rather than a shrug — a check
    that silently returns something plausible is worse than no check, because the thing it guards
    fails silently too.
    """
    import ast

    package = _grid_src_private_server()
    if package is None:
        pytest.skip("grid-src worktree is not beside this one; the lockstep cannot be checked here")
    source = package / _RELAY_MODULE
    if not source.exists():
        pytest.skip("grid-src worktree is not beside this one; the lockstep cannot be checked here")

    for node in ast.parse(source.read_text()).body:
        targets = (
            node.targets if isinstance(node, ast.Assign)
            else [node.target] if isinstance(node, ast.AnnAssign) and node.value is not None
            else []
        )
        if not any(getattr(t, "id", None) == _RELAY_CONSTANT for t in targets):
            continue
        value = node.value
        # `frozenset({...})` / `set([...])` — the collection is the call's single argument.
        if isinstance(value, ast.Call) and len(value.args) == 1:
            value = value.args[0]
        assert isinstance(value, (ast.Set, ast.Tuple, ast.List)), (
            f"grid-src's {_RELAY_CONSTANT} is no longer a literal collection, so this lockstep "
            f"check cannot read it — teach this helper the new shape rather than deleting the check")
        members = []
        for element in value.elts:
            assert isinstance(element, ast.Constant) and isinstance(element.value, str), (
                f"grid-src's {_RELAY_CONSTANT} holds something that is not a plain string literal, "
                f"so this check would compare a set it has only partly read")
            members.append(element.value)
        assert members, (
            f"grid-src's {_RELAY_CONSTANT} is empty, so the relay recognises no starter engine at "
            f"all — every grid's engine would serve traffic nobody counts")
        return frozenset(members)

    raise AssertionError(
        f"{_RELAY_CONSTANT} is no longer defined in grid-src's {_RELAY_MODULE} — it was renamed or "
        f"moved, so teach this check where it went rather than deleting it")


def _cli_grid_run_kinds():
    """The kinds this CLI says nobody may join — the authority the relay's copy must match."""
    from shared.models.api_catalog import WHITELISTS

    return frozenset(kind for kind, entry in WHITELISTS.items() if not entry.member_joinable)


def test_this_cli_still_has_a_kind_no_member_may_join():
    """The control, and it does NOT need grid-src.

    Without it the comparison below is satisfied by two empty sets: `member_joinable` defaulting to
    True everywhere, and a relay recognising nothing, agree perfectly and mean the feature is gone.
    """
    assert _cli_grid_run_kinds(), (
        "no ApiWhitelist sets member_joinable=False any more, so this CLI describes no engine the "
        "grid stands up on a member's behalf — ADR 0038 D-c has nothing left to recognise")


def test_the_relay_recognises_exactly_the_kinds_no_member_may_join():
    """The lockstep itself. Both directions of drift are a real failure, in opposite ways.

    A kind marked `member_joinable=False` here and missing from the relay's set is the silent one:
    that grid's starter engine ranks FIRST on free capacity and its traffic is counted against
    nothing. A kind in the relay's set that members CAN join is the mirror: somebody's own API key,
    paid for out of their own pocket, is ranked last and metered as if the operator bought it.
    """
    assert _relay_grid_run_kinds() == _cli_grid_run_kinds(), (
        "grid-src's starter_engine.GRID_RUN_ENGINE_KINDS and this CLI's member_joinable=False rows "
        "have drifted; edit BOTH sides, and read ADR 0038 D-c before choosing which is right")
