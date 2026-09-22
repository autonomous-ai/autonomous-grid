"""The lock one admin's `credentials.toml` is serialized on, pinned across two repositories.

PRD `grid-scale-phase-a`, issue 12. **Neither half is in this repository**, and it is pinned here
anyway because this is the hub and because there is nowhere else both halves can be read from: the
seam is grid-src's `grid` CLI ↔ grid-apis' control plane, and by design neither imports the other.

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


def test_both_writers_name_one_lock():
    """The assertion the other two exist to make possible, stated on its own so a failure says it.

    Compared to each other as well as to the literal: a future edit that changed the literal here
    and one side to match would still be caught, because the OTHER side would no longer agree.
    """
    assert _grid_src_lock_file() == _grid_apis_lock_file()


def test_the_lock_is_not_the_file_it_guards():
    """⚠️ The trap that looks like the fix. Locking `credentials.toml` itself serializes nothing,
    because each atomic write replaces the inode the lock was taken on."""
    assert CANONICAL_LOCK_FILE != CANONICAL_CREDENTIALS_FILE
    assert _grid_src_lock_file() != CANONICAL_CREDENTIALS_FILE
    assert _grid_apis_lock_file() != CANONICAL_CREDENTIALS_FILE


@pytest.mark.parametrize("repo", ["grid-src", "grid-apis"])
def test_each_side_derives_the_lock_path_from_the_guarded_one(repo):
    """Both `lock_path_for` implementations append to the guarded path rather than naming a file.

    The names agreeing is necessary and not sufficient: a side that stopped deriving the sibling —
    locking the target, or a fixed path in a directory the other never looks at — would keep every
    constant above intact while the two writers stopped meeting. Structural rather than textual, so
    reformatting it does not fail.
    """
    root = grid_src_root() if repo == "grid-src" else grid_apis_root()
    if root is None:
        pytest.skip(f"{repo} worktree is not beside this one; the lockstep cannot be checked here")
    package = "grid_cli" if repo == "grid-src" else "grid_networks"
    tree = ast.parse((root / package / "filelock.py").read_text())

    derived = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "lock_path_for"
    ]

    assert len(derived) == 1, f"{repo} no longer defines exactly one lock_path_for"
    body = ast.dump(derived[0])
    assert "with_suffix" in body, f"{repo}'s lock_path_for no longer derives a sibling path"
    assert "LOCK_SUFFIX" in body, f"{repo}'s lock_path_for no longer uses the pinned suffix"
