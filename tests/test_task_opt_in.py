"""The task opt-in is read in ONE place (issue 57).

`GRID_TASKS` and `GRID_MAX_TASKS` began life inside `remote/serve.py`, which is the serve child's
own module. Two processes need the answers now — the child at startup, and the CLI parent before it
spawns that child (issues 58, 60, 61) — and the parent cannot ask `serve` for them.

The hazard this file exists for is not the import: it is the second READING. A parent that
re-implements "is `GRID_TASKS` on" compiles, passes every test, and then gets edited apart from the
copy in the serve child, which is the silent-divergence failure this repository keeps paying for.
So the rule is enforced by a scan, not by a convention.

⚠️ **The scan measures a reading, not a mention.** `remote/task_evict.py` names `GRID_MAX_TASKS` in
its module docstring to say what bounds what, and `docs/` names both everywhere — those are
cross-references, and a rule that forced them out would make the tree less legible to enforce a
property they do not violate. So the scan parses each file and looks at STRING LITERALS IN CODE,
with docstrings excluded.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
# The packages shipped in the wheel. ⚠️ `build/lib/` and `.venv/` hold *copies* of this tree; walking
# either would count every reading twice and make the scan unfixable.
_SOURCE_PACKAGES = ("cli", "local", "remote", "shared", "doggi", "train")
# A walk that visits nothing reports "read in one place" for a tree that reads it in ten. Well below
# the real count, so it pins the walk without breaking on ordinary growth.
_MINIMUM_FILES_WALKED = 100

_OPT_IN_MODULE = "remote/task_opt_in.py"


def _source_files() -> list[Path]:
    files: list[Path] = []
    for package in _SOURCE_PACKAGES:
        files.extend(sorted((_REPO / package).rglob("*.py")))
    return files


def _code_string_literals(path: Path) -> set[str]:
    """Every string literal in `path` that is CODE. Docstrings are prose and are excluded.

    A name in prose is a cross-reference and must stay readable; a name in a code literal is a
    reading of the variable, and that is what may exist only once.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    prose: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            prose.add(id(body[0].value))
    return {
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in prose
    }


def _files_reading(literal: str, files: list[Path] | None = None) -> list[str]:
    """Every source file that spells `literal` in code, as repo-relative paths."""
    found = []
    for path in (files if files is not None else _source_files()):
        if literal in _code_string_literals(path):
            relative = path.relative_to(_REPO) if path.is_relative_to(_REPO) else path
            found.append(str(relative).replace("\\", "/"))
    return sorted(found)


def test_the_walk_really_visited_the_source():
    """The floor. Every assertion below is "found in exactly one file", and a walk that found
    nothing at all reports the same thing — with the rule completely unenforced."""
    walked = _source_files()
    assert len(walked) >= _MINIMUM_FILES_WALKED, (
        f"the walk visited only {len(walked)} source files; it is not walking the tree")
    for package in _SOURCE_PACKAGES:
        assert any(path.is_relative_to(_REPO / package) for path in walked), (
            f"the walk contributed no file from {package}/")
    assert not any("build/lib" in str(path) or ".venv" in str(path) for path in walked), (
        "the walk reached a COPY of the tree; every reading would be counted twice")


def test_every_source_file_could_actually_be_PARSED():
    """A file the scan cannot parse is a file it cannot police, and skipping one in silence is how
    an enforcement scan comes to certify a tree it never read."""
    unparseable = []
    for path in _source_files():
        try:
            _code_string_literals(path)
        except (SyntaxError, UnicodeDecodeError) as exc:
            unparseable.append(f"{path.relative_to(_REPO)}: {exc}")
    assert not unparseable, "the scan could not read:\n  " + "\n  ".join(unparseable)


@pytest.mark.parametrize("literal", ["GRID_TASKS", "GRID_MAX_TASKS"])
def test_the_task_opt_in_is_read_in_exactly_one_module(literal):
    """One reading, two callers. A second spelling anywhere in the source is the divergence."""
    reading = _files_reading(literal)
    assert reading == [_OPT_IN_MODULE], (
        f"{literal} is read outside {_OPT_IN_MODULE}: {reading}. Import it from there instead — two "
        f"readings of one environment variable get edited apart, silently.")


def test_the_scan_catches_a_planted_second_reading(tmp_path):
    """The positive control. Every assertion above is "nothing else was found", which is exactly
    what a broken matcher also reports."""
    planted = tmp_path / "planted.py"
    planted.write_text('import os\nopt_in = os.getenv("GRID_TASKS")\n', encoding="utf-8")

    assert _files_reading("GRID_TASKS", [planted]) == [str(planted).replace("\\", "/")]


def test_the_scan_does_not_fire_on_a_MENTION(tmp_path):
    """The control for the exclusion above — and it is a real file, not a hypothetical one:
    `remote/task_evict.py` names `GRID_MAX_TASKS` in its docstring to say what bounds what."""
    prose = tmp_path / "prose.py"
    prose.write_text(
        '"""`GRID_TASKS` turns this on."""\n# and GRID_MAX_TASKS sizes the pool\n', encoding="utf-8")

    assert _files_reading("GRID_TASKS", [prose]) == []
    assert _files_reading("GRID_MAX_TASKS", [prose]) == []


def test_the_scan_matches_the_whole_NAME(tmp_path):
    """`GRID_TASK_ROOT` is a different variable that shares a prefix with `GRID_TASKS`. A substring
    match would report the provider's workspace root as a second reading of the opt-in."""
    other = tmp_path / "other.py"
    other.write_text('import os\nroot = os.getenv("GRID_TASK_ROOT")\n', encoding="utf-8")

    assert _files_reading("GRID_TASKS", [other]) == []


def test_the_opt_in_module_does_not_pull_the_provider_runtime():
    """Asserted, not assumed. The whole point of the move is that a CLI command can ask this
    question without importing the serve child's world.

    A subprocess, because in-process `sys.modules` is whatever the rest of the suite already
    imported — measured: `remote.serve` costs 83 ms and 273 modules and pulls httpx, against 16 ms
    and 119 modules for `remote.task_agent`.
    """
    probe = (
        "import sys, remote.task_opt_in; "
        "print('httpx' in sys.modules, 'remote.serve' in sys.modules, 'remote.relay' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], cwd=_REPO, capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False False False", (
        f"the opt-in module dragged the provider runtime in with it: {result.stdout.strip()}")
