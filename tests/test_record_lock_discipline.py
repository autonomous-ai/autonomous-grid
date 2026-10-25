"""The run-record lock's two rules, checked over the source rather than remembered.

`shared.filelock.file_lock` opens a fresh fd on every call, and `flock` binds the lock to that open
file description, so the lock is NOT re-entrant: a second acquisition in the same thread waits on the
first forever. `run_records.mutate_record` takes the lock; `update_record` does not. Hence:

* **(i)** inside `with file_lock(...)` / `with try_file_lock(...)`, never call anything that takes the
  lock again (`file_lock`, `try_file_lock`, `mutate_record`, `try_mutate_record`). `remote/serve.py`
  `_stamp_own_pid` is the case to keep: "cleaning" its `update_record` up to `mutate_record` hangs the
  serve child before its first socket, and every `grid join` with it.
* **(ii)** outside such a block, never call `update_record`, an unlocked read-modify-write that can
  lose `grid join`'s union merge and re-create a record `grid leave` just deleted.

The check is lexical and per function: a nested `def` starts a fresh scope, because defining a
function inside the lock is not calling it there.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("cli", "remote", "shared", "local")
LOCKS = {"file_lock", "try_file_lock"}
RELOCKS = LOCKS | {"mutate_record", "try_mutate_record"}

LOCAL_MODE = (
    "local mode takes no record lock anywhere, in its join or its leave, so a lock on one side "
    "would serialize against nothing; converting it is a change of its own"
)
#: (path, function) → why its unlocked `update_record` is allowed. Keyed by function, not line, so
#: an unrelated edit above it does not move the exemption onto something else.
UNLOCKED_UPDATE_ALLOWED = {
    ("cli/provider.py", "_fixed_up_endpoint"): LOCAL_MODE,
    ("cli/provider.py", "_run_engine"): LOCAL_MODE,
}


def _called_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _takes_lock(item: ast.withitem) -> bool:
    expr = item.context_expr
    return isinstance(expr, ast.Call) and _called_name(expr) in LOCKS


class _Checker(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scope: list[str] = []
        self.depth: list[int] = [0]  # lock blocks open in the current function
        self.found: list[tuple[str, int, str]] = []  # (function, line, rule)

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scope.append(node.name)
        self.depth.append(0)
        self.generic_visit(node)
        self.depth.pop()
        self.scope.pop()

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self.visit(item)
        locks = sum(_takes_lock(item) for item in node.items)
        self.depth[-1] += locks
        for stmt in node.body:
            self.visit(stmt)
        self.depth[-1] -= locks

    def visit_Call(self, node: ast.Call) -> None:
        name = _called_name(node)
        where = self.scope[-1] if self.scope else "<module>"
        if self.depth[-1] and name in RELOCKS:
            self.found.append((where, node.lineno, f"(i) {name} inside a held record lock"))
        if not self.depth[-1] and name == "update_record":
            self.found.append((where, node.lineno, "(ii) update_record outside the record lock"))
        self.generic_visit(node)


def _violations(source: str) -> list[tuple[str, int, str]]:
    checker = _Checker()
    checker.visit(ast.parse(source))
    return checker.found


def test_the_source_keeps_both_record_lock_rules():
    found = []
    for package in PACKAGES:
        for path in sorted((ROOT / package).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            for function, line, rule in _violations(path.read_text(encoding="utf-8")):
                if rule.startswith("(ii)") and (rel, function) in UNLOCKED_UPDATE_ALLOWED:
                    continue
                found.append(f"{rel}:{line} {function}: {rule}")
    assert found == []


def test_every_exemption_still_names_an_unlocked_update():
    """An exemption whose call is gone would silently cover the next one written there."""
    for rel, function in UNLOCKED_UPDATE_ALLOWED:
        hits = [f for f, _, rule in _violations((ROOT / rel).read_text(encoding="utf-8"))
                if f == function and rule.startswith("(ii)")]
        assert hits, f"{rel} {function} has no unlocked update_record left: drop its exemption"


def test_the_checker_sees_each_shape():
    """Positive controls: each rule fires on a planted case, and its correct twin is clean."""
    relock = (
        "def f(p):\n"
        "    with file_lock(p):\n"
        "        run_records.mutate_record(g, e, m)\n"
    )
    unlocked = "def f():\n    run_records.update_record(g, e, k=1)\n"
    locked = (
        "def f(p):\n"
        "    with file_lock(p):\n"
        "        run_records.update_record(g, e, k=1)\n"
    )
    nested_def = (
        "def f(p):\n"
        "    with file_lock(p):\n"
        "        def later():\n"
        "            run_records.mutate_record(g, e, m)\n"
    )
    after_block = (
        "def f(p):\n"
        "    with file_lock(p):\n"
        "        pass\n"
        "    run_records.mutate_record(g, e, m)\n"
    )
    assert [r for _, _, r in _violations(relock)] == ["(i) mutate_record inside a held record lock"]
    assert [r for _, _, r in _violations(unlocked)] == ["(ii) update_record outside the record lock"]
    assert _violations(locked) == []
    assert _violations(nested_def) == []
    assert _violations(after_block) == []
