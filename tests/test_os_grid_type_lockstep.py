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


# --- the denylist's reach, a rule with a half in each repository (ADR 0039 D-h) --------------------
# The one enforcement mechanism this type widens, and the only lever that can stop ONE abusive
# account on a grid that admits strangers automatically. Its two halves answer different questions —
# the control plane decides what a caller SEES and is issued, the master decides what a credential
# already in flight is SERVED — so a widening applied to one side alone leaves an account that has
# vanished from its own grid list and is still being served at the relay, or the reverse.

#: The predicate that decides WHO a denylist row refuses is not the same on every type, and this
#: check cannot read that half: it is a role condition inside a function body, spelled differently
#: in the two repositories on purpose (grid-apis has no allowlist predicate to be the inverse of).
#: Each repository pins its own role condition against a `both` member — the role an OS grid's gate
#: actually mints — and a mutation run proved both matrices fail when the providers type's
#: `== {"consumer"}` is carried across. What travels between repositories, and what this reads, is
#: the SET of types the denylist reaches at all.
_DENYLIST_TYPES = "DENYLIST_NETWORK_TYPES"
_DENYLIST_CONTROL_VALUE = "permissioned-providers"


@pytest.mark.parametrize(
    "side,relative_path,parse",
    [
        ("grid-src's master", "grid_cli/private_server/grid_auth.py", _module),
        ("grid-apis' control plane", "grid_networks/store.py", _apis_module),
    ])
def test_the_denylist_reaches_an_os_grid_on_both_sides_of_the_call(side, relative_path, parse):
    """Neither repository's own suite can see this: each is right about its own half.

    ⚠️ The failure is **silent in both directions**. Widened here and not there, an operator bans an
    account, watches the grid disappear from its `grid ls`, and the credential in that machine's
    `~/.grid` keeps being served until it expires — a year. Widened there and not here, the ban bites
    at the relay while `GET /tokens` keeps handing the same account a fresh credential every sign-in,
    which reads as a broken grid rather than as a ban.
    """
    members = _collection(parse(relative_path), _DENYLIST_TYPES, relative_path)
    assert _DENYLIST_CONTROL_VALUE in members, (
        f"positive control: {_DENYLIST_CONTROL_VALUE} is missing from {side} {_DENYLIST_TYPES} too, "
        f"so this check is reading the wrong thing — fix the harness before believing its verdict")
    assert OS_COMMUNITY in members, (
        f"{side} {_DENYLIST_TYPES} does not name {OS_COMMUNITY}, so the denylist reaches an OS grid "
        f"on one side of the call and not the other (ADR 0039 D-h) — edit BOTH sides")


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


# --- the same claim on the RENEWAL, which is a body key and not a query parameter ------------------

#: grid-apis' Pydantic model for the refresh exchange's body. A different route, a different shape,
#: and therefore a THIRD hand-duplicated spelling of the same claim (ADR 0039 D-e, issue 10).
_REFRESH_MODEL = "TokenRefreshRequest"
#: Its pre-existing field, the positive control — the credential the exchange has always carried.
_REFRESH_CONTROL_FIELD = "refresh_token"


def _apis_model_fields(model_name):
    """The field names grid-apis declares on a Pydantic model, or an assertion naming what it found."""
    tree = _apis_module(_APIS_HANDLER)
    classes = [
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == model_name
    ]
    assert len(classes) == 1, (
        f"expected exactly one `class {model_name}` in grid-apis' {_APIS_HANDLER}, found "
        f"{len(classes)} — the model moved or was renamed, so teach this check where it went rather "
        f"than letting the pin read nothing")
    return {
        node.target.id
        for node in classes[0].body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }


def _os_key_the_cli_sends_on_a_refresh(monkeypatch, tmp_path):
    """The body key this CLI actually puts on a refresh, read off a real request.

    Read from the request and not the source, for the same reason the query-parameter pin above is:
    what the far end must recognise is the string that reaches it.
    """
    import json
    import platform

    import httpx

    from remote import control_plane

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    # Fixed, so the claim is present whatever this developer's machine runs — see the note above.
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"access_token": "a", "refresh_token": "r"})

    real_client = httpx.Client
    monkeypatch.setattr(
        control_plane.httpx,
        "Client",
        lambda *a, **k: real_client(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )
    control_plane.refresh_network_token(network_id="grid-1", refresh_token="rt-1")

    body = seen.get("body") or {}
    assert _REFRESH_CONTROL_FIELD in body, (
        f"positive control: `refresh_network_token` sent {sorted(body)} and not even "
        f"{_REFRESH_CONTROL_FIELD}, so its answer about the OS claim means nothing — fix the harness")
    claim = set(body) - {_REFRESH_CONTROL_FIELD}
    assert len(claim) == 1, (
        f"`refresh_network_token` put {sorted(body)} on the wire, so this check cannot tell which key "
        f"carries the OS claim — teach it the new shape rather than deleting the pin")
    return claim.pop()


def test_the_os_claim_on_a_renewal_is_spelled_the_same_on_both_sides(monkeypatch, tmp_path):
    """The renewal's copy of the claim, pinned separately from the fetch's — they are two spellings.

    ⚠️ **Pinning the query parameter does not pin this.** The fetch carries the claim in the URL and
    the refresh carries it in a JSON body, so they are written in different places on both sides and
    can drift apart independently. Renaming only the body key leaves the fetch pin green.

    And this one degrades even more quietly than the fetch's, because it is a key on an EXISTING body
    whose model does not forbid unknown fields: dropped in silence, the exchange still answers, and
    the only symptom is that a machine on an OS grid can never renew — which looks exactly like the
    bug this slice was written to remove, reappearing with nothing red anywhere.
    """
    declared = _apis_model_fields(_REFRESH_MODEL)
    assert _REFRESH_CONTROL_FIELD in declared, (
        f"positive control: grid-apis' {_REFRESH_MODEL} does not declare {_REFRESH_CONTROL_FIELD} "
        f"either, so this check is reading the wrong class — fix the harness before believing it")

    sent = _os_key_the_cli_sends_on_a_refresh(monkeypatch, tmp_path)
    assert sent in declared, (
        f"this CLI sends the OS claim on a refresh as {sent!r}, but grid-apis' {_REFRESH_MODEL} "
        f"declares {sorted(declared)} — the key is IGNORED rather than refused (the model does not "
        f"forbid extras), so a machine on an OS grid silently never renews (ADR 0039 D-e); either "
        f"rename both sides together or teach this pin the new spelling")


def test_the_fetch_and_the_renewal_agree_with_each_other(monkeypatch, tmp_path):
    """Both ends of one claim, so the two spellings cannot drift apart inside this repository either.

    The pins above each compare this CLI against grid-apis. Nothing yet says the CLI's own two call
    sites agree — and a machine that announced `os` on sign-in and `os_token` on renewal would be
    admitted and then quietly unable to renew, which is the same outage arriving one step later.

    ⚠️ **A context per call, and it is not tidiness.** Both helpers patch `control_plane.httpx.Client`
    and capture the real one first; run under a single `monkeypatch` the second captures the FIRST
    one's replacement, so its handler never fires and it reads an empty body. That is not a
    hypothetical — it is what this test did when written, and the helper's positive control is what
    said so instead of letting the comparison report a verdict it had not measured.
    """
    with pytest.MonkeyPatch.context() as on_sign_in:
        fetch = _os_parameter_the_cli_sends(on_sign_in, tmp_path)
    with pytest.MonkeyPatch.context() as on_renewal:
        refresh = _os_key_the_cli_sends_on_a_refresh(on_renewal, tmp_path)

    assert fetch == refresh, (
        f"this CLI announces the OS claim as {fetch!r} when signing in and {refresh!r} when renewing; "
        f"a machine would be admitted to an OS grid and then silently unable to renew on it")


# --- the TOKEN SET itself: what this CLI can claim vs what the control plane will serve ------------
# Every pin above is about a value's SPELLING. This one is about its MEMBERSHIP, and it arrived with
# `omarchy` (issue 04) because that is the first token added since the two lists were written.
#
# `shared/system/os_grid.OS_TOKENS` is everything a machine running this CLI can claim.
# `os_networks.DEFAULT_SERVED_OS_TOKENS` is everything the control plane provisions a grid for when
# `GRID_OS_GRID_TOKENS` is unset — and it is unset on the dev VM and on prod, so that default is not
# a fallback in practice, it is the served list.
#
# ⚠️ **A token this CLI claims and the control plane does not serve is a machine with NO OS grid, not
# a machine that falls back to another one.** That is the whole point of the claim being single-valued
# (ADR 0039 D-c): an Omarchy machine claims `omarchy`, so it is no longer claiming `linux`, so an
# unserved `omarchy` costs it the Linux grid it used to be on. The degrade is at least LOUD — D-k's
# absence line fires on `os_served: false` — which is why this is an ordering rather than a fail-open.
#
# ⚠️ **Roll the CONTROL PLANE out BEFORE the CLI for this value, the REVERSE of the `os-community`
# literal at the top of this file.** That one goes grid-src first because the control plane shells out
# to grid-src's argparse; this one goes control plane first because the CLI is what starts making the
# new claim. Same feature, opposite answers, and inferring either from the other gets it backwards.
#
# ⚠️ Deliberately NOT compared against `_OS_LABELS`. The label map is the GRID'S NAME per token
# (`omarchy` → `Omagrid`) and lives only in grid-apis; nothing in this repository reads a grid's name
# for anything but printing it, so it has no half here and no lockstep to keep.

_APIS_OS_NETWORKS = "grid_networks/os_networks.py"
_SERVED_TOKENS = "DEFAULT_SERVED_OS_TOKENS"

#: The token that predates this slice on both sides — the positive control, so a helper that has
#: quietly stopped reading the tuple fails as a HARNESS fault rather than as "the seam is fine".
_TOKEN_CONTROL_VALUE = "macos"


def test_every_token_this_cli_can_claim_is_one_the_control_plane_serves():
    """Membership, compared list against list — neither repository can see this alone.

    This repository's suite is right that `os_token()` resolves `omarchy` on an Omarchy machine.
    grid-apis' suite is right that `served_os_tokens()` answers exactly what its default tuple holds.
    Both stay green while an Omarchy machine signs in, claims a token nobody serves, and is handed
    nothing — which is a REGRESSION for that machine, because it was on the Linux grid the day before.

    Asserted as a subset rather than as equality: the control plane may legitimately serve a token no
    shipped CLI claims yet (that is how a token gets deployed FIRST, per the ordering above), and
    calling that a failure would make the safe rollout order the one that fails the test.
    """
    from shared.system import os_grid

    served = _collection(
        _apis_module(_APIS_OS_NETWORKS), _SERVED_TOKENS, _APIS_OS_NETWORKS)

    assert _TOKEN_CONTROL_VALUE in served, (
        f"positive control: even {_TOKEN_CONTROL_VALUE!r} is missing from grid-apis' "
        f"{_SERVED_TOKENS}, so this check is reading the wrong thing — fix the harness")
    assert _TOKEN_CONTROL_VALUE in os_grid.OS_TOKENS, (
        f"positive control: even {_TOKEN_CONTROL_VALUE!r} is missing from this repository's "
        f"OS_TOKENS, so this check is reading the wrong thing — fix the harness")

    unserved = sorted(set(os_grid.OS_TOKENS) - served)
    assert not unserved, (
        f"this CLI can claim {unserved} and grid-apis' {_SERVED_TOKENS} serves {sorted(served)}, so a "
        f"machine of that system claims a token nobody provisions a grid for — and because the claim "
        f"is single-valued it has stopped claiming the one it used to get (ADR 0039 D-c). Add the "
        f"token to BOTH, and roll the control plane out FIRST")


# --- the ANSWER's key, this slice's FOURTH cross-repo value ----------------------------------------
# Everything above pins what the CLI SAYS. `os_served` is the one value on this seam that travels the
# other way: `GET /v1/grid/tokens` reports whether this call was handed an OS grid, so the three
# causes of "my grid list has no OS grid in it" stop sharing one symptom (ADR 0039 D-k). It is
# written independently on both sides with no import between them — a key in the dict grid-apis'
# `get_tokens` returns, and `OS_SERVED_KEY` in this repository's `remote/control_plane.py`.
#
# ⚠️ **It degrades SILENTLY in BOTH directions, which is what makes it the quietest value here.** An
# unknown key in a JSON body is not an error to anybody: renamed on the control plane, this CLI reads
# it as absent and says nothing, which is by design indistinguishable from an older control plane —
# the exact behaviour D-k specifies for that case. So a rename leaves both repositories green, every
# request succeeding, and the feature that exists to explain an absent OS grid silently explaining
# nothing. There is no symptom to notice, which is why this needs a pin rather than an argument.
#
# ⚠️ **The rollout order is NONE, in either direction**, and for the same reason the `os=` parameter
# has none: a new key on an existing endpoint. An old control plane sends nothing and the CLI prints
# nothing (the previous behaviour exactly); an old CLI ignores what a new control plane sends.

#: The key that has been in this answer since long before OS grids — the positive control on the
#: grid-apis side, and the proof the reader below is looking at the right dict.
_TOKENS_CONTROL_KEY = "networks"


def _apis_answer_keys(function):
    """Every constant string key grid-apis puts in a dict it RETURNS from this handler.

    Read off the `return` statements rather than a response model, because this route declares none —
    it returns a plain dict, so the literal is the contract. Anything this cannot read raises instead
    of narrowing the set in silence: a lockstep helper that returns a plausible subset guards nothing,
    and what it fails to guard fails silently too.
    """
    keys = set()
    returns = [node for node in ast.walk(function) if isinstance(node, ast.Return)]
    assert returns, (
        f"grid-apis' {function.name} has no `return` at all — this pin cannot read what the route "
        f"answers, so teach it the new shape rather than letting it compare an empty set")
    for node in returns:
        assert isinstance(node.value, ast.Dict), (
            f"grid-apis' {function.name} returns a {type(node.value).__name__} rather than a dict "
            f"literal; teach this pin the new shape instead of letting it read nothing")
        for key in node.value.keys:
            assert isinstance(key, ast.Constant) and isinstance(key.value, str), (
                f"grid-apis' {function.name} builds a response key this pin cannot read "
                f"({ast.dump(key) if key is not None else '**spread'}); teach it the new shape")
            keys.add(key.value)
    return keys


def _os_served_keys_the_cli_reads(monkeypatch, tmp_path, candidates):
    """Which of ``candidates`` this CLI actually reads as "you were handed an OS grid".

    Measured by ANSWERING with each key in turn and asking what `fetch_tokens` made of it, rather
    than by reading this repository's source: what has to keep agreeing is the string that arrives,
    and a constant is one refactor away from not being the string the parser looks up.

    ``False`` is the probe value on purpose. It is the answer that must produce a *line* — "the
    control plane isn't serving one" — and it is truthy nowhere, so a key that flips the CLI's answer
    to `False` is unambiguously the one being read.
    """
    import httpx

    from remote import control_plane

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    real_client = httpx.Client

    def answering(payload):
        monkeypatch.setattr(
            control_plane.httpx,
            "Client",
            lambda *a, **k: real_client(*a, **{
                **k, "transport": httpx.MockTransport(lambda r: httpx.Response(200, json=payload))}),
        )
        return control_plane.fetch_tokens("sess-tok", "dev-1")

    # Positive control FIRST: prove the transport patch reaches the real parser at all. Without it a
    # helper that answered nobody would report "no key is read" and read as drift that has not
    # happened — the failure mode every other pin in this file guards against explicitly.
    control = answering({_TOKENS_CONTROL_KEY: [{"network_id": "n1", "name": "team"}]})
    assert control.networks == [{"network_id": "n1", "name": "team"}], (
        f"positive control: `fetch_tokens` made {control!r} of an ordinary answer, so its verdict "
        f"about {_TOKENS_CONTROL_KEY} means nothing — fix the harness before believing it")
    assert control.os_served is None, (
        "positive control: `fetch_tokens` reported an os_served with no such key in the answer, so "
        "the probe below cannot tell which key set it — fix the harness")

    return {key for key in candidates
            if answering({_TOKENS_CONTROL_KEY: [], key: False}).os_served is False}


def test_the_key_that_says_why_there_is_no_os_grid_is_spelled_the_same_on_both_sides(monkeypatch,
                                                                                     tmp_path):
    """What grid-apis answers and what this CLI reads, compared against each other.

    Neither repository can catch this alone. grid-apis' own suite asserts the key it emits, this
    repository's `test_fetch_tokens_carries_the_os_served_flag_beside_the_networks` asserts the key it
    reads, and a developer renaming it updates the test on their side along with the code. Both
    suites stay green and the absent OS grid stops saying why — with no error anywhere, because an
    unrecognised key in a JSON body is simply dropped.
    """
    answered = _apis_answer_keys(_apis_route(_TOKENS_METHOD, _TOKENS_PATH))
    assert _TOKENS_CONTROL_KEY in answered, (
        f"positive control: grid-apis' {_TOKENS_METHOD.upper()} {_TOKENS_PATH} does not appear to "
        f"answer {_TOKENS_CONTROL_KEY} either, so this check is reading the wrong function — fix the "
        f"harness before believing its verdict")

    read = _os_served_keys_the_cli_reads(monkeypatch, tmp_path, answered)
    assert len(read) == 1, (
        f"grid-apis answers {sorted(answered)} on {_TOKENS_METHOD.upper()} {_TOKENS_PATH}, and this "
        f"CLI reads {sorted(read) or 'none of them'} as the OS-grid flag — the key is dropped in "
        f"silence, so an absent OS grid stops saying why and NOTHING goes red (ADR 0039 D-k); edit "
        f"BOTH sides, or teach this pin the new spelling")


def test_the_flag_this_cli_names_is_the_one_it_actually_reads(monkeypatch, tmp_path):
    """`OS_SERVED_KEY` is what the pin above compares; nothing yet says the parser uses it.

    A constant that has drifted from the code beside it is worse than no constant: the cross-repo
    check would go on comparing grid-apis to a name this CLI no longer looks up, and would keep
    reporting agreement while the seam was broken.
    """
    from remote import control_plane

    read = _os_served_keys_the_cli_reads(monkeypatch, tmp_path, {control_plane.OS_SERVED_KEY})
    assert read == {control_plane.OS_SERVED_KEY}, (
        f"`control_plane.OS_SERVED_KEY` is {control_plane.OS_SERVED_KEY!r} but `fetch_tokens` does "
        f"not read that key out of the answer — the constant and the parser have drifted apart")


# --- who wrote the machine's name (ADR 0039 D-n, issue 16) ----------------------------------------

# A boolean on the register meta saying whether the machine's `name` was chosen by a PERSON or is
# just the box's hostname. On `os-community` the relay's public overview publishes the name only when
# it was chosen; every other type publishes both, unchanged.
#
# ⚠️ **THREE copies, and two of them are providers in different repositories.** grid-src's fleet
# runtime (`provider_runtime/provider/lifecycle.node_name_meta`) and this repository's public CLI
# (`remote/serve._meta` — the binary an `os-community` member actually installs) each SEND it;
# grid-src's relay (`private_server/overview.py`, through `member_identity_access
# .published_node_name`) READS it. A rename on any one of the three leaves the other two green.
#
# ⚠️ **It degrades SILENTLY in both directions**, which is what makes this a pin rather than an
# argument. The register meta is a MERGE, not a schema: an unknown key is stored and ignored, and a
# missing key is not an error to anybody. Rename the provider's half and every suite in both
# repositories stays green, every heartbeat succeeds, and the only symptom is that machines whose
# operators DID name them stop being named on one grid type. Rename the relay's and the same thing
# happens from the other end.
#
# ⚠️ **The rollout order is NONE, in either direction, and it is not hazardous either way** — the
# fail-closed direction is what buys that. An old provider against a new relay sends nothing, absent
# reads as *not chosen*, and the name is withheld: the private answer. An old relay against a new
# provider stores the key and ignores it, which is today's behaviour exactly.

#: The key that has been on this meta since the grid page existed — the positive control on both
#: sides, and the proof each reader below is looking at the right dict.
_META_CONTROL_KEY = "name"

#: grid-src's relay half: the module that decides what the public overview calls a machine, and the
#: function it hands the provider's claim to.
_OVERVIEW = "grid_cli/private_server/overview.py"
_OVERVIEW_GATE = "published_node_name"
#: Its keyword — the argument carrying the provider's claim, as opposed to the name itself.
_OVERVIEW_CLAIM_KEYWORD = "chosen"

#: grid-src's own provider half: the fleet runtime's, which is NOT the one an os-community member
#: runs but is the one most likely to be forgotten when the public CLI's is edited.
_PROVIDER_LIFECYCLE = "grid_cli/provider_runtime/provider/lifecycle.py"
_PROVIDER_META_FUNCTION = "node_name_meta"


def _meta_key_the_relay_reads_as_chosen():
    """The meta key grid-src's overview hands to its D-n gate, read off the call itself.

    Read from the CALL rather than from a constant in `member_identity_access`, because what has to
    keep agreeing with this CLI is the string looked up in the provider's payload — and a constant is
    one refactor away from not being that string. Every shape this cannot read raises: a helper that
    returned a plausible subset would guard nothing, and what it failed to guard would fail silently
    too.
    """
    tree = _module(_OVERVIEW)
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call)
             and (getattr(node.func, "id", None) == _OVERVIEW_GATE
                  or getattr(node.func, "attr", None) == _OVERVIEW_GATE)]
    assert len(calls) == 1, (
        f"expected exactly one call to {_OVERVIEW_GATE} in grid-src's {_OVERVIEW}, found "
        f"{len(calls)} — the overview was restructured, so teach this pin the new shape rather than "
        f"letting it read the wrong one")

    claim = next((kw for kw in calls[0].keywords if kw.arg == _OVERVIEW_CLAIM_KEYWORD), None)
    assert claim is not None, (
        f"grid-src's {_OVERVIEW_GATE} call no longer passes {_OVERVIEW_CLAIM_KEYWORD}= — the claim "
        f"is spelled somewhere else now, so teach this pin where")

    keys = {node.value for node in ast.walk(claim.value)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    assert len(keys) == 1, (
        f"grid-src reads its {_OVERVIEW_CLAIM_KEYWORD}= from "
        f"{sorted(keys) or 'no string key at all'} in {_OVERVIEW}, so this check cannot tell which "
        f"meta key carries the claim — teach it the new shape rather than deleting the pin")
    return keys.pop()


def _meta_keys_the_grid_src_provider_sends():
    """Every constant key grid-src's fleet provider puts on the register meta's name pair."""
    tree = _module(_PROVIDER_LIFECYCLE)
    function = next(
        (n for n in tree.body
         if isinstance(n, ast.FunctionDef) and n.name == _PROVIDER_META_FUNCTION), None)
    assert function is not None, (
        f"grid-src's {_PROVIDER_LIFECYCLE} no longer defines {_PROVIDER_META_FUNCTION} — the fleet "
        f"provider builds its name meta somewhere else now, so teach this pin where it went rather "
        f"than letting it skip")

    returns = [node for node in ast.walk(function) if isinstance(node, ast.Return)]
    assert len(returns) == 1 and isinstance(returns[0].value, ast.Dict), (
        f"grid-src's {_PROVIDER_META_FUNCTION} no longer returns a single dict literal, so this pin "
        f"cannot read what it sends — teach it the new shape")

    keys = set()
    for key in returns[0].value.keys:
        assert isinstance(key, ast.Constant) and isinstance(key.value, str), (
            f"grid-src's {_PROVIDER_META_FUNCTION} builds a key this pin cannot read "
            f"({ast.dump(key) if key is not None else '**spread'}); teach it the new shape")
        keys.add(key.value)
    return keys


def _meta_key_this_cli_sends():
    """The key this CLI's register meta uses to state that a person chose the name.

    Measured by BUILDING two metas that differ in exactly that fact and asking which key moved,
    rather than by reading this repository's source: what the relay has to recognise is the string
    that arrives on the wire, and a pin that parsed `remote/serve.py` would go on agreeing with
    grid-src about a spelling nobody sends.
    """
    from remote import serve

    base = {"meta_name": "mybox", "endpoint_url": "http://h/v1"}
    chosen = serve._meta({**base, "meta_name_chosen": True}, "remote")
    unchosen = serve._meta({**base, "meta_name_chosen": False}, "remote")

    # Positive control FIRST: prove `_meta` built a real payload at all. Without it a helper that
    # returned two empty dicts would report "no key carries the claim" and read as drift that has not
    # happened — the failure mode every pin in this file guards against explicitly.
    assert chosen.get(_META_CONTROL_KEY) == "mybox" == unchosen.get(_META_CONTROL_KEY), (
        f"positive control: `serve._meta` did not even put {_META_CONTROL_KEY!r} on the meta, so its "
        f"verdict about the claim means nothing — fix the harness before believing it")

    moved = {key for key in set(chosen) | set(unchosen) if chosen.get(key) != unchosen.get(key)}
    assert len(moved) == 1, (
        f"`serve._meta` answered differently on {sorted(moved) or 'no key at all'} for a chosen name "
        f"and an unchosen one, so this check cannot tell which key carries the claim — teach it the "
        f"new shape rather than deleting the pin")
    return moved.pop()


def test_the_key_saying_who_named_the_machine_is_spelled_the_same_on_both_sides_of_the_call():
    """What this CLI sends and what grid-src's relay reads, compared against each other.

    Neither repository can catch this alone, which is the whole reason it is written here. This
    repository's `test_meta_says_whether_a_person_chose_the_node_name` asserts what the CLI sends,
    grid-src's `test_os_grid_member_identity.py` asserts what the relay reads, and a developer
    renaming the key updates the test on their own side along with the code. Both suites stay green,
    the key stops being recognised, and every machine on an `os-community` grid whose operator DID
    name it goes back to `node-<id>` — silently, because an unknown key on a merged meta payload is
    stored and ignored rather than refused (ADR 0039 D-n).
    """
    read = _meta_key_the_relay_reads_as_chosen()
    sent = _meta_key_this_cli_sends()

    assert sent == read, (
        f"this CLI states the claim as {sent!r} and grid-src's {_OVERVIEW} reads {read!r} — the key "
        f"is dropped in silence, so a name its operator chose stops being published on an "
        f"os-community grid and NOTHING goes red; edit BOTH sides")


def test_the_other_provider_half_in_grid_src_sends_the_same_key():
    """The half issue 15's audit named and issue 16 nearly repeated: grid-src's own fleet runtime.

    ⚠️ **There are TWO provider halves and they are in different repositories.** An `os-community`
    member runs the PUBLIC CLI pinned above; the grid-src runtime is what runs on the fleet. A fix
    applied to one leaves the other publishing hostnames with every suite in both repositories still
    green — the exact shape that has bitten this codebase before — so the relay's single reader is
    pinned against BOTH senders rather than against whichever one was edited last.
    """
    read = _meta_key_the_relay_reads_as_chosen()
    sends = _meta_keys_the_grid_src_provider_sends()

    assert _META_CONTROL_KEY in sends, (
        f"positive control: grid-src's {_PROVIDER_META_FUNCTION} does not appear to send "
        f"{_META_CONTROL_KEY!r} either, so this check is reading the wrong function — fix the "
        f"harness before believing its verdict")
    assert read in sends, (
        f"grid-src's relay reads {read!r} but its own provider runtime sends {sorted(sends)} — the "
        f"fleet's machines are all published as `node-<id>` on an os-community grid, or all "
        f"published by hostname, and nothing anywhere goes red; edit BOTH sides")
