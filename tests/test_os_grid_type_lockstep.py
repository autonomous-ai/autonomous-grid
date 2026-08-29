"""`os-community`, pinned across the repository boundary (ADR 0039 D-a, D-j).

grid-src holds **two** copies of this literal — `grid_cli/network_runtime.py`, which decides whether
`grid network create --network-type os-community` is even a legal argv, and
`grid_cli/private_server/grid_auth.py`, which decides whether a token the control plane signed for
that grid is admitted at the master. **grid-apis `grid_networks/store.py` is the third**, and it is
the one that decides whether a grid of this type can exist in the database at all. There is no import
path between any of them, so they are kept in step by editing every side — and by this test.

⚠️ **The deploy order is the REVERSE of the web-tool routes' and must not be inferred from them.**
grid-src rolls out FIRST, before the control plane, because auto-provisioning an OS grid is not an
INSERT: grid-apis `managed_networks.build_create_argv` shells out
`grid network create <name> --network-type os-community --advertise-url … --port … --network-id …`
and that `grid` binary IS grid-src. A control plane that knows the literal before grid-src does gets
argparse exit 2, a failed create, and a grid stuck at `pending` retrying forever — loud and
fail-closed, but wrong. (The *public* CLI in this repository has no ordering in either direction:
`os=` is a new query parameter on an existing endpoint.)

⚠️ Per this repository's rule for every cross-repo assertion, each case here **skips unless the
worktree it reads sits beside this one** — grid-src for most, grid-apis for the last two — i.e. they
skip in CI, and a green CI proves nothing about any of them. Run it locally, on a machine that has
all three.

Its own module rather than more of `tests/test_task_lease.py`: nothing here is about the task plane,
and that file names one worktree by absolute path, which is why its checks skip in every *other*
worktree. This one resolves both siblings from its own location (`tests/grid_src_repo.py`), the same
way `tests/test_starter_engine_lockstep.py` does.
"""
from __future__ import annotations

import ast

import pytest

from tests.grid_src_repo import grid_apis_root, grid_src_root

# The literal, as ADR 0039 D-a fixes it.
#
# ⚠️ **A literal in the test, and this repository still holds no `os-community` constant of its own
# — deliberately.** `cli/remote_grid.cmd_remote_ls` prints whatever `network_type` the control plane
# hands it, and the `os=` slice gave this repo an OS *token* vocabulary
# (`shared/system/os_grid.OS_TOKENS` — `macos`/`windows`/`linux`) which is a **different value**:
# that one names a machine, this one names a grid type. Do not "finally" re-point this constant at
# `os_grid`; they would then agree by accident and this pin would be checking nothing.
#
# What HAS changed is that the pin now has two independent authorities on the other side rather than
# one — grid-src and grid-apis — so a rename on either is caught by the other's copy, not merely by
# a string written here.
OS_COMMUNITY = "os-community"

# The name both grid-src modules give their copy, and a literal that predates this slice — the
# positive control, so a helper that has quietly stopped finding anything fails as a HARNESS fault
# rather than reading as "the seam is fine".
_CONSTANT = "NETWORK_TYPE_OS_COMMUNITY"
_CONTROL_CONSTANT = "NETWORK_TYPE_PRIVATE_DOMAIN"
_CONTROL_VALUE = "private-domain"

_SKIP = "the {repo} worktree is not beside this one; the lockstep cannot be checked here"


def _parse(root, repo, relative_path):
    """Parse one module of a sibling repository, or skip. Never plausible-looking on a miss.

    ⚠️ **Only "no such repository at all" skips. A missing named module RAISES.** By the time this
    runs the resolver has already proved a marker directory exists under that root, so a module
    absent from it has been renamed or moved — which is drift, exactly what this suite is for, and
    reporting it as "the repository is not beside this one" would turn the assertions off with a
    message blaming the wrong thing. `_collection` and `_open_network_types` raise for the same class
    of change one level down; this is the same rule at file granularity.
    """
    if root is None:
        pytest.skip(_SKIP.format(repo=repo))
    source = root / relative_path
    if not source.exists():
        raise AssertionError(
            f"{repo} is at {root} but has no {relative_path} — the module was renamed or moved, "
            f"so teach this check where it went rather than letting it skip")
    return ast.parse(source.read_text())


def _module(relative_path):
    """Parse one grid-src module."""
    return _parse(grid_src_root(), "grid-src", relative_path)


def _apis_module(relative_path):
    """Parse one grid-apis module — the control plane, the literal's third copy."""
    return _parse(grid_apis_root(), "grid-apis", relative_path)


def _targets(node):
    """The names a module-level assignment binds — plain or annotated, one shape for both.

    `NETWORK_TYPE_OS_COMMUNITY: str = "os-community"` is as ordinary as the bare form, and reading
    only the bare one would report the constant ABSENT rather than say it could not read it — a
    loud failure pointing at the wrong cause. `tests/test_starter_engine_lockstep.py` already
    handles `AnnAssign` for its own constant; this keeps the two suites reading the same Python.
    """
    if isinstance(node, ast.Assign):
        return node.targets
    if isinstance(node, ast.AnnAssign) and node.value is not None:
        return [node.target]
    return []


def _string_constants(tree):
    """Every module-level ``NAME = "literal"`` in the module, as a dict."""
    found = {}
    for node in tree.body:
        value = getattr(node, "value", None)
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        for target in _targets(node):
            if isinstance(target, ast.Name):
                found[target.id] = value.value
    return found


def _resolve(elements, constants, where):
    """The string VALUES of a collection's elements, following ``NETWORK_TYPE_*`` names.

    Every shape this does not understand is an assertion rather than a shrug: a check that silently
    returns a plausible subset is worse than no check, because what it guards fails silently too.
    """
    values = set()
    for element in elements:
        if isinstance(element, ast.Constant) and isinstance(element.value, str):
            values.add(element.value)
        elif isinstance(element, ast.Name):
            assert element.id in constants, (
                f"grid-src's {where} names {element.id}, which is not a module-level string "
                f"constant in that file — teach this check the new shape rather than deleting it")
            values.add(constants[element.id])
        else:
            # TRY004 wants a TypeError here; this is a HARNESS fault, not a caller's bad argument —
            # and `assert False` would vanish under `python -O`, which is the one way this check
            # could go quiet.
            raise AssertionError(  # noqa: TRY004
                f"grid-src's {where} holds something this check cannot read "
                f"({type(element).__name__}); teach it the new shape rather than deleting the check")
    return values


def _collection(tree, name, relative_path):
    """A module-level collection assignment's members, resolved to their string values."""
    constants = _string_constants(tree)
    for node in tree.body:
        if not any(getattr(t, "id", None) == name for t in _targets(node)):
            continue
        value = node.value
        # `frozenset({...})` / `set([...])` — the collection is the call's single argument.
        if isinstance(value, ast.Call) and len(value.args) == 1:
            value = value.args[0]
        assert isinstance(value, (ast.Set, ast.Tuple, ast.List)), (
            f"grid-src's {name} is no longer a literal collection, so this lockstep check cannot "
            f"read it — teach this helper the new shape rather than deleting the check")
        return _resolve(value.elts, constants, f"{name} in {relative_path}")

    raise AssertionError(
        f"{name} is no longer defined in grid-src's {relative_path} — it was renamed or moved, so "
        f"teach this check where it went rather than deleting it")


def _open_network_types():
    """The types `grid_auth._requires_allowlist` admits with NO allowlist snapshot entry.

    Read out of the function's single ``if nt in (...)`` membership test. The other comparison in
    that function is `set(roles) != {"consumer"}`, which is not an `in`, so exactly one match is
    expected and anything else is a restructuring this check must be taught rather than trusted.
    """
    relative_path = "grid_cli/private_server/grid_auth.py"
    tree = _module(relative_path)
    constants = _string_constants(tree)
    function = next(
        (n for n in tree.body
         if isinstance(n, ast.FunctionDef) and n.name == "_requires_allowlist"), None)
    assert function is not None, (
        f"grid-src's {relative_path} no longer defines _requires_allowlist — the master's "
        f"allowlist decision moved, so teach this check where it went")

    memberships = [
        node for node in ast.walk(function)
        if isinstance(node, ast.Compare)
        and len(node.ops) == 1 and isinstance(node.ops[0], ast.In)
        and isinstance(node.comparators[0], (ast.Set, ast.Tuple, ast.List))
    ]
    assert len(memberships) == 1, (
        f"expected exactly one `nt in (...)` test in grid-src's _requires_allowlist, found "
        f"{len(memberships)} — the function was restructured, so teach this check the new shape")
    return _resolve(
        memberships[0].comparators[0].elts, constants, f"_requires_allowlist in {relative_path}")


# --- the literal ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "relative_path",
    ["grid_cli/network_runtime.py", "grid_cli/private_server/grid_auth.py"])
def test_both_grid_src_copies_spell_the_type_the_way_this_repo_prints_it(relative_path):
    """One literal, two files, and this repository's `grid ls` prints whatever they say.

    A rename on one side alone is not an error anywhere in grid-src: `network_runtime` renamed and
    `grid_auth` not means every create succeeds and every member is then refused at the master;
    the other way round means the control plane's auto-provision dies at argparse.
    """
    constants = _string_constants(_module(relative_path))
    assert constants.get(_CONTROL_CONSTANT) == _CONTROL_VALUE, (
        f"positive control: this check can no longer read even {_CONTROL_CONSTANT} out of "
        f"{relative_path}, so its answer about {_CONSTANT} means nothing — fix the harness")
    assert constants.get(_CONSTANT) == OS_COMMUNITY, (
        f"grid-src's {relative_path} spells the OS grid type {constants.get(_CONSTANT)!r}, this "
        f"repository prints {OS_COMMUNITY!r} (ADR 0039 D-a); edit BOTH sides")


# --- the four sets that have to name it -----------------------------------------------------------


@pytest.mark.parametrize(
    "collection,why",
    [
        ("VALID_NETWORK_TYPES",
         ("`normalize_network_type` refuses the type outright, so the control plane's "
          "auto-provision exits non-zero and the grid stays at `pending`, retrying forever "
          "(ADR 0039 D-j)")),
        ("VALID_NETWORK_TYPE_INPUTS",
         ("`grid network create --network-type os-community` is argparse exit 2 — the same D-j "
          "failure, one layer earlier, and `network set-type` loses the value with it")),
        ("PUBLIC_NETWORK_TYPES",
         ("`cmd_network_create` refuses `--advertise-url` on a non-public type and grid-apis "
          "`build_create_argv` always passes it, so the auto-provision dies just as hard")),
    ])
def test_the_network_runtime_sets_that_decide_whether_a_grid_can_be_created(collection, why):
    relative_path = "grid_cli/network_runtime.py"
    members = _collection(_module(relative_path), collection, relative_path)
    assert _CONTROL_VALUE in members, (
        f"positive control: {_CONTROL_VALUE} is missing from grid-src's {collection} too, so this "
        f"check is reading the wrong thing — fix the harness before believing its verdict")
    assert OS_COMMUNITY in members, f"{OS_COMMUNITY} is missing from grid-src's {collection}: {why}"


def test_the_master_admits_an_os_grid_with_no_allowlist_snapshot_entry():
    """The one whose absence is NOT caught by any create succeeding.

    An `os-community` in every set above but missing here is a grid that provisions, starts, appears
    in `grid ls` — and 403s every member at the master. ⚠️ It cannot be fixed by adding an allowlist
    row either: ADR 0039 D-e persists nothing about a person's OS, so the roster is empty by
    construction and there is no row for anyone to add.
    """
    open_types = _open_network_types()
    assert _CONTROL_VALUE in open_types, (
        f"positive control: {_CONTROL_VALUE} is missing from grid-src's _requires_allowlist too, "
        f"so this check is reading the wrong branch — fix the harness")
    assert OS_COMMUNITY in open_types, (
        f"grid-src's _requires_allowlist does not admit {OS_COMMUNITY} without an allowlist entry, "
        f"so every member of every OS grid is refused at the master with no row anyone can add "
        f"(ADR 0039 D-a, D-e)")


# --- the control plane's copy, the third one ------------------------------------------------------
# grid-apis is the CLIENT for this literal, not the server, and that is what makes its rollout order
# the reverse of the web-tool routes': `managed_networks.build_create_argv` shells out
# `grid network create … --network-type os-community` and that binary is grid-src. So a mismatch here
# is not a 404 to be degraded around — it is a failed create and a grid stuck at `pending`.

_APIS_STORE = "grid_networks/store.py"


def test_the_control_plane_spells_the_type_the_way_grid_src_does():
    """A rename on one side alone is silent in the OTHER direction too.

    grid-apis renamed and grid-src not: the create argv carries a literal grid-src's argparse has
    never heard of, so the auto-provision exits 2 and the grid never comes up. grid-src renamed and
    grid-apis not: every create succeeds and the master then refuses every member, because
    `_requires_allowlist` no longer recognises what the token says.
    """
    constants = _string_constants(_apis_module(_APIS_STORE))
    assert constants.get(_CONTROL_CONSTANT) == _CONTROL_VALUE, (
        f"positive control: this check can no longer read even {_CONTROL_CONSTANT} out of "
        f"grid-apis' {_APIS_STORE}, so its answer about {_CONSTANT} means nothing — fix the harness")
    assert constants.get(_CONSTANT) == OS_COMMUNITY, (
        f"grid-apis' {_APIS_STORE} spells the OS grid type {constants.get(_CONSTANT)!r}; grid-src and "
        f"this repository say {OS_COMMUNITY!r} (ADR 0039 D-a) — edit EVERY side")


def test_the_control_plane_admits_the_type_as_a_valid_one():
    """Missing from `VALID_NETWORK_TYPES`, `normalize_network_type` RAISES rather than refuses.

    Every gate, serializer and predicate in that store runs the value through it, so the type would
    not merely be rejected — the grid would be unreadable, and each read would surface as a 500
    rather than as anything naming the type.
    """
    members = _collection(_apis_module(_APIS_STORE), "VALID_NETWORK_TYPES", _APIS_STORE)
    assert _CONTROL_VALUE in members, (
        f"positive control: {_CONTROL_VALUE} is missing from grid-apis' VALID_NETWORK_TYPES too, so "
        f"this check is reading the wrong thing — fix the harness before believing its verdict")
    assert OS_COMMUNITY in members, (
        f"{OS_COMMUNITY} is missing from grid-apis' VALID_NETWORK_TYPES, so `normalize_network_type` "
        f"raises on every OS grid row the store reads")
