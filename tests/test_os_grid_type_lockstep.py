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


# --- the `os=` query parameter's NAME, this slice's SECOND cross-repo value -----------------------
# The type literal above is not the only value the OS gate hand-duplicates across a repository
# boundary. The claim itself rides as a query parameter on `GET /v1/grid/tokens`, and its name is
# written independently in two places with no import between them: `params["os"]` in this
# repository's `remote/control_plane.fetch_tokens`, and `alias="os"` on grid-apis'
# `grid_networks/handler.get_tokens`.
#
# ⚠️ **It degrades SILENTLY, which is why it needs a pin rather than an argument.** An unknown query
# parameter is not an error to any HTTP framework — it is dropped. So a rename on one side alone
# leaves both repositories green (each repo's own test asserts what that repo does, and both are
# still right), every request succeeds, every other grid is issued exactly as before, and the only
# symptom is that no machine is ever admitted to an OS grid. Nothing is red and nothing is loud.
#
# ⚠️ **The rollout order is NONE, in either direction — derived, not copied from the rows above.**
# The type literal a few tests up rolls grid-src first because grid-apis SHELLS OUT to it; this value
# has no such asymmetry. It is a new key on an EXISTING endpoint, and both halves already treat
# "no claim" as an ordinary answer: an old control plane ignores what a new CLI sends, and a new
# control plane facing an old CLI reads the absent parameter as a machine claiming nothing. Either
# way the caller keeps every grid it already had and simply gets no OS grid (ADR 0039 D-j).

_APIS_HANDLER = "grid_networks/handler.py"

# The route the claim rides on, named by its DECORATOR and not by the Python function's name: what
# has to keep agreeing is the endpoint, and a handler renamed while `GET /tokens` stays put is not
# drift. `_APIS_ROUTER` is the module-level `APIRouter` every route in that file is hung off.
_APIS_ROUTER = "router"
_TOKENS_METHOD = "get"
_TOKENS_PATH = "/tokens"

# The parameter that has ridden on this call since long before OS grids — the positive control. It is
# also the one that proves the two readers below are looking at the same request: the CLI sends it
# beside the OS claim and the control plane declares it beside the OS parameter.
_CONTROL_PARAMETER = "device_id"


def _apis_route(method, path):
    """The function grid-apis hangs off ``@router.<method>("<path>")``, or an assertion.

    Found by decorator rather than by name so that renaming the handler — an ordinary refactor that
    breaks no seam — does not read as drift, while moving or deleting the ROUTE does.
    """
    tree = _apis_module(_APIS_HANDLER)
    matches = [
        node for node in tree.body
        # ⚠️ BOTH function kinds. `async def` is an `ast.AsyncFunctionDef`, a separate node type that
        # is NOT a subclass of `ast.FunctionDef` — and grid-apis' handler module already spells 15 of
        # its routes that way. Matching only the sync kind meant that converting this one handler to
        # `async def` — an ordinary refactor that moves no route and breaks no seam — emptied the
        # match list and fired the assertion below, reporting drift that had not happened. A pin that
        # cries wolf on a legitimate refactor is a pin somebody deletes.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute) and decorator.func.attr == method
        and getattr(decorator.func.value, "id", None) == _APIS_ROUTER
        and decorator.args and isinstance(decorator.args[0], ast.Constant)
        and decorator.args[0].value == path
    ]
    assert len(matches) == 1, (
        f"expected exactly one @{_APIS_ROUTER}.{method}({path!r}) handler in grid-apis' "
        f"{_APIS_HANDLER}, found {len(matches)} — the route moved or was split, so teach this check "
        f"where it went rather than letting the pin read the wrong function")
    return matches[0]


def _query_parameter_names(function):
    """Every query parameter a FastAPI handler accepts, as ``{python name: wire name}``.

    FastAPI takes the wire name from ``Query(alias=…)`` when there is one and from the Python
    parameter's own name when there is not: `device_id` is the second kind, `os` the first — a
    parameter actually *called* ``os`` would shadow the stdlib module for the whole function, which
    is why the alias exists at all and why the two names differ here.

    ``Header``-defaulted parameters are deliberately excluded: they are the same ``Call`` shape with
    the same ``alias=`` keyword, and folding them in would let ``Authorization`` satisfy a check
    about the query string.

    Every shape this cannot read raises rather than being skipped over. A lockstep helper that
    returns a plausible subset is worse than no helper at all, because what it guards fails silently
    too — the same rule `_resolve` and `_collection` follow above.
    """
    spec = function.args
    # `defaults` covers the TAIL of the positional parameters; `kw_defaults` is one-for-one with the
    # keyword-only ones and holds None where there is no default. Handling both means a handler that
    # is later given a `*` separator keeps being read instead of quietly emptying this dict.
    pairs = list(zip(spec.args[len(spec.args) - len(spec.defaults):], spec.defaults))
    pairs += [(arg, default)
              for arg, default in zip(spec.kwonlyargs, spec.kw_defaults) if default is not None]

    names = {}
    for arg, default in pairs:
        if not isinstance(default, ast.Call):
            continue  # a plain Python default — not a FastAPI parameter declaration at all
        kind = getattr(default.func, "id", None) or getattr(default.func, "attr", None)
        if kind != "Query":
            continue
        alias = next((kw.value for kw in default.keywords if kw.arg == "alias"), None)
        if alias is None:
            names[arg.arg] = arg.arg
            continue
        assert isinstance(alias, ast.Constant) and isinstance(alias.value, str), (
            f"grid-apis' {arg.arg} declares an alias this check cannot read "
            f"({type(alias).__name__}); teach it the new shape rather than deleting the check")
        names[arg.arg] = alias.value
    return names


def _os_parameter_the_cli_sends(monkeypatch, tmp_path):
    """The query-parameter name this CLI actually puts on the wire, read off a real request.

    Read from the request rather than from the source, because what the control plane has to
    recognise is the string that reaches it — `params["os"]` is one refactor away from being built
    somewhere else, and a pin that parsed this repository's source would then be pinning a spelling
    nobody sends.
    """
    import platform

    import httpx

    from remote import control_plane

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    # A fixed system, so the claim is present whatever this developer's own machine runs: a machine
    # outside the closed token set sends no `os=` at all (ADR 0039 D-c), and a suite that skipped on
    # a BSD laptop would be a pin that quietly stopped pinning.
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"networks": []})

    real_client = httpx.Client
    monkeypatch.setattr(
        control_plane.httpx,
        "Client",
        lambda *a, **k: real_client(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )
    control_plane.fetch_tokens("sess-tok", "dev-1")

    params = seen.get("params") or {}
    assert _CONTROL_PARAMETER in params, (
        f"positive control: `fetch_tokens` sent {sorted(params)} and not even {_CONTROL_PARAMETER}, "
        f"so its answer about the OS claim means nothing — fix the harness")
    claim = set(params) - {_CONTROL_PARAMETER}
    assert len(claim) == 1, (
        f"`fetch_tokens` put {sorted(params)} on the wire, so this check cannot tell which parameter "
        f"carries the OS claim — teach it the new shape rather than deleting the pin")
    return claim.pop()


def test_the_os_claims_parameter_is_spelled_the_same_on_both_sides_of_the_call(monkeypatch,
                                                                               tmp_path):
    """The name the CLI sends and the name the control plane declares, compared against each other.

    Neither repository can catch this alone, and that is the whole point of writing it here: this
    repository's `test_fetch_tokens_sends_the_os_token_of_the_machine_it_runs_on` asserts what the
    CLI sends, grid-apis' own suite asserts what it accepts, and a developer renaming the parameter
    would update the test on their side along with the code. Both suites stay green, the parameter
    stops being recognised, and no machine is admitted to an OS grid — silently, because an unknown
    query parameter is dropped rather than refused.
    """
    accepted = _query_parameter_names(_apis_route(_TOKENS_METHOD, _TOKENS_PATH))
    assert _CONTROL_PARAMETER in accepted.values(), (
        f"positive control: grid-apis' {_TOKENS_METHOD.upper()} {_TOKENS_PATH} does not appear to "
        f"take {_CONTROL_PARAMETER} either, so this check is reading the wrong thing — fix the "
        f"harness before believing its verdict")

    sent = _os_parameter_the_cli_sends(monkeypatch, tmp_path)
    assert sent in accepted.values(), (
        f"this CLI sends the OS claim as {sent!r}, but grid-apis' {_TOKENS_METHOD.upper()} "
        f"{_TOKENS_PATH} accepts {sorted(accepted.values())} — the parameter is dropped, so nobody "
        f"is ever issued an OS grid and nothing anywhere goes red (ADR 0039 D-e); edit BOTH sides")
