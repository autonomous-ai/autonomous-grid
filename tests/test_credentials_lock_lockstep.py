"""The lock one admin's `credentials.toml` is serialized on, pinned across two repositories.

PRD `grid-scale-phase-a`, issue 12 and its follow-up. **THREE repositories take this lock**, and it
is pinned here because this is the hub and there is nowhere else all of them can be read from: the
seam is grid-src's internal CLI ↔ grid-apis' control plane ↔ this repository's public CLI, and by
design none of them imports another.

⚠️ **This repository's half was added AFTER the other two**, because issue 12 put the public CLI out
of scope on the grounds that its store is *a file one person writes one command at a time*. True of
a laptop, false of the first-provider home: grid-apis seeds it with `seed_home_at` — which takes the
lock — and then shells THIS binary into it.

A hosted admin has ONE ``GRID_HOME`` holding every grid they own — one ``[[networks]]`` entry each,
carrying that grid's **refresh credential**. Two processes read-modify-write that file: grid-apis'
`managed_homes.seed_home` at the start of every `managed-*` call, and the CLI's
`config.upsert_network_credentials` whenever a grid's credential is refreshed. Both writes are
atomic and atomicity is not the property in question — it says the file is never *torn*, and
nothing about a **lost update**, where the second writer carries a snapshot taken before the first.

⚠️ **A lock only one side takes is a lock that does nothing**, and that is the whole reason this
file exists. A rename on either side leaves both repositories compiling, both suites green, every
`managed-*` call succeeding — and the two writers serializing against nothing, until the day two of
an admin's grids are touched at once and one of them loses a refresh credential the control plane
has already consumed. There is no 404 to see, no postcondition in a reply to check, and no
rollout order that helps: the failure is **silence**.

⚠️ Per this repository's rule for every cross-repo assertion, these **skip unless both worktrees sit
beside this one** — i.e. they skip in CI, and a green CI proves nothing about them. Unlike the other
lockstep suites there is no half here that CI can run, because this repository has no half at all.
Each repository carries its own pin against the same literal (`tests/test_credentials_lock.py` in
grid-src, `tests/test_managed_homes_lock.py` in grid-apis), so a rename has to be made in FOUR
places to go unnoticed — and two of them are tests.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from tests.grid_src_repo import grid_apis_root, grid_src_root

#: The file both writers take the lock on, inside the admin's ``GRID_HOME``.
#:
#: ⚠️ A **sibling** of the credential store, never the store itself. `credentials.toml` is written
#: by an atomic replace, so a lock held on the file itself is a lock on an inode that stops existing
#: the moment anybody writes, and every later acquirer takes a lock nobody else is holding. That is
#: the version of this fix that looks right and protects nothing.
CANONICAL_LOCK_FILE = "credentials.toml.lock"

#: The store the lock guards. Named separately so the assertion below can say that the two differ.
CANONICAL_CREDENTIALS_FILE = "credentials.toml"


def _string_constant(path, name: str) -> str:
    """A module-level ``NAME = "literal"``, parsed rather than imported.

    ⚠️ Finding no such assignment is an **error, not a skip**: that is exactly what a rename on the
    other side looks like, and skipping would turn this check off in silence — which is the failure
    mode the whole file is about.
    """
    for node in ast.walk(ast.parse(path.read_text())):
        targets = getattr(node, "targets", []) if isinstance(node, ast.Assign) else []
        for target in targets:
            if getattr(target, "id", None) == name and isinstance(node.value, ast.Constant):
                return node.value.value
    raise AssertionError(f"{name} is no longer a module-level string literal in {path}")


def _grid_src_lock_file() -> str:
    root = grid_src_root()
    if root is None:
        pytest.skip("grid-src worktree is not beside this one; the lockstep cannot be checked here")
    package = root / "grid_cli"
    return (
        _string_constant(package / "paths.py", "CREDENTIALS_FILE")
        + _string_constant(package / "filelock.py", "LOCK_SUFFIX")
    )


def _public_cli_lock_file() -> str:
    """This repository's own composition — no sibling needed, so this half runs in CI."""
    from shared import filelock, paths

    return filelock.lock_path_for(paths.credentials_file()).name


def _grid_apis_lock_file() -> str:
    root = grid_apis_root()
    if root is None:
        pytest.skip("grid-apis worktree is not beside this one; the lockstep cannot be checked here")
    package = root / "grid_networks"
    return (
        _string_constant(package / "managed_homes.py", "_CREDENTIALS_FILE")
        + _string_constant(package / "filelock.py", "LOCK_SUFFIX")
    )


def test_the_cli_locks_the_canonical_file():
    assert _grid_src_lock_file() == CANONICAL_LOCK_FILE


def test_the_control_plane_locks_the_canonical_file():
    assert _grid_apis_lock_file() == CANONICAL_LOCK_FILE


def test_the_public_cli_locks_the_canonical_file():
    """Needs no sibling worktree, so it is the one half of this file that runs in CI."""
    assert _public_cli_lock_file() == CANONICAL_LOCK_FILE


def test_all_three_writers_name_one_lock():
    """The assertion the other two exist to make possible, stated on its own so a failure says it.

    Compared to each other as well as to the literal: a future edit that changed the literal here
    and one side to match would still be caught, because the OTHER side would no longer agree.
    """
    assert _grid_src_lock_file() == _grid_apis_lock_file() == _public_cli_lock_file()


def test_the_lock_is_not_the_file_it_guards():
    """⚠️ The trap that looks like the fix. Locking `credentials.toml` itself serializes nothing,
    because each atomic write replaces the inode the lock was taken on."""
    assert CANONICAL_LOCK_FILE != CANONICAL_CREDENTIALS_FILE
    assert _grid_src_lock_file() != CANONICAL_CREDENTIALS_FILE
    assert _grid_apis_lock_file() != CANONICAL_CREDENTIALS_FILE
    assert _public_cli_lock_file() != CANONICAL_CREDENTIALS_FILE


@pytest.mark.parametrize("repo", ["grid-src", "grid-apis", "autonomous-grid"])
def test_each_side_derives_the_lock_path_from_the_guarded_one(repo):
    """Both `lock_path_for` implementations append to the guarded path rather than naming a file.

    The names agreeing is necessary and not sufficient: a side that stopped deriving the sibling —
    locking the target, or a fixed path in a directory the other never looks at — would keep every
    constant above intact while the two writers stopped meeting. Structural rather than textual, so
    reformatting it does not fail.
    """
    here = pathlib.Path(__file__).resolve().parent.parent
    root = {"grid-src": grid_src_root, "grid-apis": grid_apis_root,
            "autonomous-grid": lambda: here}[repo]()
    if root is None:
        pytest.skip(f"{repo} worktree is not beside this one; the lockstep cannot be checked here")
    package = {"grid-src": "grid_cli", "grid-apis": "grid_networks", "autonomous-grid": "shared"}[repo]
    tree = ast.parse((root / package / "filelock.py").read_text())

    derived = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "lock_path_for"
    ]

    assert len(derived) == 1, f"{repo} no longer defines exactly one lock_path_for"
    body = ast.dump(derived[0])
    assert "with_suffix" in body, f"{repo}'s lock_path_for no longer derives a sibling path"
    assert "LOCK_SUFFIX" in body, f"{repo}'s lock_path_for no longer uses the pinned suffix"


# --- the guard's own count, which is the thing that was got wrong -------------------------------

#: Every grid-apis call that answers by ROTATING a device's refresh credential, by the handler it
#: sits in. `_issue_bundle(..., rotate_refresh=True)` reaches `store.issue_refresh_credential`,
#: which COMMITS an `UPDATE` replacing `refresh_token_hash` — so the token the CLI still holds is
#: dead the moment the reply is on the wire.
#:
#: ⚠️ **This set is the fact the first version of the guard asserted without checking.** It was
#: written as *"the one place a rotation is spent"*, wired to one CLI function, and the miss was
#: reachable from the control plane: `grid network set-type` goes through the device-id half of
#: `refresh_token` and its caller swallows `SystemExit` to print *"token refresh skipped"* and
#: return **0** — for a rotation that had been spent. A count is pinned here so a FOURTH one
#: appearing in grid-apis fails a test instead of being discovered the same way.
ROTATING_HANDLERS = {
    "refresh_token",   # POST /tokens/{id} — BOTH halves: a refresh token, and a session + device id
    "get_tokens",      # GET  /tokens      — rotates EVERY grid in the account in one call
    "create_network",  # POST /networks    — see GUARDED_CLI_EXCHANGES for why this one is exempt
}

#: The grid-src `control_plane` functions that must ask before they spend one.
#:
#: Three, against three handlers, and deliberately not a one-to-one map: grid-apis' `refresh_token`
#: serves two different CLI calls on one route (a refresh token, or a session plus a device id).
#: `create_network` is the exempt one — it rotates for a device that held no credential for a grid
#: that did not exist a moment earlier, so there is nothing to destroy, and its caller already tears
#: the half-made grid down on any `BaseException`.
GUARDED_CLI_EXCHANGES = {"fetch_tokens", "fetch_network_token", "refresh_network_token"}

GUARD_CALL = "_refuse_if_the_reply_cannot_be_stored"


def _enclosing_functions(tree, predicate) -> set[str]:
    """The names of the functions containing every node `predicate` accepts (innermost wins)."""
    spans = [
        (n.lineno, n.end_lineno, n.name)
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    found = set()
    for node in ast.walk(tree):
        if not predicate(node):
            continue
        owners = [s for s in spans if s[0] <= node.lineno <= s[1]]
        if not owners:
            raise AssertionError(f"a match at line {node.lineno} sits outside every function")
        found.add(min(owners, key=lambda s: s[1] - s[0])[2])
    return found


def test_the_control_plane_rotates_in_exactly_the_places_the_cli_knows_about():
    """⚠️ A FOURTH rotating handler must fail here rather than be found in production.

    Errors rather than skips when the call disappears entirely: `_issue_bundle` losing its keyword,
    or the rotation moving behind another helper, is exactly what this check must not sail past.
    """
    root = grid_apis_root()
    if root is None:
        pytest.skip("grid-apis worktree is not beside this one; the lockstep cannot be checked here")
    tree = ast.parse((root / "grid_networks" / "handler.py").read_text())

    rotating = _enclosing_functions(
        tree,
        lambda n: isinstance(n, ast.Call)
        and any(
            kw.arg == "rotate_refresh"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value is True
            for kw in n.keywords
        ),
    )

    assert rotating, (
        "no `rotate_refresh=True` call site found in grid-apis' handler.py at all — the rotation "
        "moved or was renamed, and this pin has to move with it rather than pass empty")
    assert rotating == ROTATING_HANDLERS, (
        f"grid-apis rotates in {sorted(rotating)}, this pin expects {sorted(ROTATING_HANDLERS)}. A "
        f"NEW one destroys the caller's refresh credential, so decide whether the CLI's "
        f"`{GUARD_CALL}` has to cover the call that reaches it — and update both this set and "
        f"GUARDED_CLI_EXCHANGES")


def test_the_cli_asks_before_every_exchange_that_spends_one():
    """The near half: the guard is on all three, and on nothing that does not need it.

    Pinned as a SET rather than a count so adding it to a fourth function is as loud as dropping it
    from one — a guard on a read would be a lock taken on a path that never writes.
    """
    root = grid_src_root()
    if root is None:
        pytest.skip("grid-src worktree is not beside this one; the lockstep cannot be checked here")
    tree = ast.parse((root / "grid_cli" / "control_plane.py").read_text())

    guarded = _enclosing_functions(
        tree,
        lambda n: isinstance(n, ast.Call) and getattr(n.func, "id", None) == GUARD_CALL,
    )
    guarded.discard(GUARD_CALL)  # the helper's own definition is not a call site

    assert guarded == GUARDED_CLI_EXCHANGES, (
        f"grid-src guards {sorted(guarded)}, this pin expects {sorted(GUARDED_CLI_EXCHANGES)}. "
        f"Every exchange grid-apis answers by rotating must ask BEFORE the round trip — refusing "
        f"afterwards leaves the dead token on disk, which is the loss the guard exists to prevent")


# --- what the critical section is allowed to contain --------------------------------------------

#: Every call a locked read-modify-write of the credential store may make, across all three
#: repositories, as `ast.unparse` spells it.
#:
#: ⚠️ **This list is what makes "no timeout" a safe rule rather than a hope.** `file_lock` blocks
#: with no deadline, and the argument for that is sound only in one direction: `flock` is released by
#: the kernel when a holder EXITS, so a crash never strands it — but a holder that stops making
#: progress while still alive holds it forever, and nothing rescues the waiter. What keeps that from
#: mattering is that every critical section is a few milliseconds of local filesystem work: no
#: network round trip, no subprocess, no second lock, no wait on another thread. That is a property
#: of today's code and nothing but this test stops tomorrow's edit from ending it — `cmd_sync` was
#: already one such edit away, and for a while it WAS one (its merge used to span an HTTP fetch).
#:
#: So the list is deliberately EXACT rather than a denylist of dangerous-looking names. A denylist
#: has to guess the next way somebody blocks; this way, anything new inside the block fails until a
#: person writes it down — which is the moment to ask whether it belongs inside the lock at all.
#: Adding a local helper here is a one-line edit. Adding `client.post` is not, and should not be.
#:
#: ⚠️ Normalising these to their last attribute (`get`, `post`, …) would shorten the list and put a
#: hole in it: `httpx.get` would then be indistinguishable from `data.get`.
CRITICAL_SECTION_CALLS = frozenset({
    # raising the refusal itself
    "SystemExit",
    # the unlocked load/save primitives the lock exists to wrap, by each repo's spelling
    "load_credentials", "save_credentials",
    "config.load_credentials", "config.save_credentials",
    "credentials.load_credentials", "credentials.save_credentials",
    "_load_creds_preserving", "_atomic_write_toml",
    # local bookkeeping on data already in hand
    "_reject_none", "control_plane_self_url",
    "api_url.rstrip", "current.get", "data.get", "data.pop", "n.get", "net.get",
    "networks.append", "record.get", "len", "list", "sorted",
    # the logout, which is a writer too
    "paths.credentials_file", "paths.credentials_file().unlink",
})

#: How many locked blocks each repository has today. A **minimum**, and the asymmetry is the point:
#: adding a locked writer is free, deleting one is a regression this catches. Zero is never the
#: answer — a scan that finds nothing has been defeated by a rename, and would otherwise pass.
LOCKED_BLOCKS = {"grid-src": 3, "grid-apis": 1, "autonomous-grid": 7}

#: Directories that hold no production critical section. `tests` is here because a test may
#: legitimately take the lock and then do anything at all inside it.
_SKIP_DIRS = frozenset({".venv", "tests", "build", "dist", ".git", "__pycache__", "node_modules"})


def _production_sources(root: pathlib.Path):
    for path in sorted(root.rglob("*.py")):
        if not any(part in _SKIP_DIRS for part in path.relative_to(root).parts):
            yield path


def _is_credentials_lock(item) -> bool:
    """Whether a `with` item is one of the three repositories' credential locks.

    Matched on the call's NAME ending in `credentials_lock`, so it catches grid-apis' private
    `_credentials_lock` and both the bare and module-qualified spellings of the other two without
    a per-repo table that could fall behind a rename.
    """
    expr = item.context_expr
    if not isinstance(expr, ast.Call):
        return False
    name = getattr(expr.func, "attr", None) or getattr(expr.func, "id", "") or ""
    return name.endswith("credentials_lock")


def _repo_root(repo: str):
    if repo == "autonomous-grid":
        return pathlib.Path(__file__).resolve().parent.parent
    root = grid_src_root() if repo == "grid-src" else grid_apis_root()
    if root is None:
        pytest.skip(f"{repo} worktree is not beside this one; the lockstep cannot be checked here")
    return root


@pytest.mark.parametrize("repo", ["grid-src", "grid-apis", "autonomous-grid"])
def test_no_critical_section_can_block_on_anything_but_the_local_disk(repo):
    """⚠️ The invariant the no-timeout rule rests on, checked instead of assumed.

    `autonomous-grid`'s case needs no sibling, so that one runs in CI; the other two skip there, as
    every cross-repo assertion in this repository does.
    """
    root = _repo_root(repo)
    blocks = 0
    offenders = []
    for path in _production_sources(root):
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, UnicodeDecodeError):  # a fixture, or source for another interpreter
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.With) and any(_is_credentials_lock(i) for i in node.items)):
                continue
            blocks += 1
            lock_exprs = {id(i.context_expr) for i in node.items}
            for call in ast.walk(node):
                if not isinstance(call, ast.Call) or id(call) in lock_exprs:
                    continue
                spelling = ast.unparse(call.func)
                if spelling not in CRITICAL_SECTION_CALLS:
                    offenders.append(f"{path.relative_to(root)}:{call.lineno} {spelling}()")

    assert blocks >= LOCKED_BLOCKS[repo], (
        f"{repo} has {blocks} locked credential blocks, expected at least "
        f"{LOCKED_BLOCKS[repo]}. Either a writer lost its lock, or the lock was renamed and this "
        f"scan now matches nothing — which would let it pass empty forever")
    assert not offenders, (
        f"{repo} calls something new inside a credential critical section:\n  "
        + "\n  ".join(offenders)
        + "\n\nThe lock blocks with NO timeout, which is only safe while every holder finishes in "
          "milliseconds of local disk work — a holder that merely stalls keeps it forever and "
          "nothing rescues the waiter. If this call can wait on a network, a subprocess, another "
          "lock or a person, move it OUTSIDE the block. If it is ordinary local work, add it to "
          "CRITICAL_SECTION_CALLS.")
