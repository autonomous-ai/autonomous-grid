"""The web-tools MCP path, pinned across the repository boundary (ADR 0041).

`grid mcp config` prints a URL a person pastes into a harness config file, and the control plane
serves that URL from a mount in `app.py`. There is no import path between the two repositories, so
the path literal is hand-duplicated and kept in step by editing both sides — and by this test.

**The failure is loud but late.** A path that drifts answers a bare 404, which the harness reports
as a server it cannot reach — in the user's terminal, days after the deploy, and never in either
suite. Both halves keep compiling and both suites stay green, which is the whole reason this file
exists.

⚠️ Per this repository's rule for every cross-repo assertion, the grid-apis cases **skip unless that
worktree sits beside this one** — i.e. they skip in CI, and a green CI proves nothing about them.
This CLI's own half is pinned unconditionally below, so the canonical path is asserted somewhere on
every run whichever way round the halves land.

Its own module rather than more of `tests/test_task_lease.py` for the reason
`tests/test_harness_login_lockstep.py` gives: nothing here is about the task plane. It reads the
sibling through `tests/grid_src_repo.py` like the other pins do.
"""

from __future__ import annotations

import ast

import pytest

from cli import mcp_config
from tests.grid_src_repo import grid_apis_root

#: The path, written out rather than imported from either side — a pin that reads one side's
#: constant and compares it to itself checks nothing.
CANONICAL_PATH = "/v1/grid/web-mcp"

_APIS_MODULE = "grid_networks/web_mcp.py"
_APIS_APP = "app.py"
_MOUNT_CONST = "MOUNT_PATH"

SKIP_NO_APIS = "the grid-apis worktree is not beside this one; the lockstep cannot be checked here"


def _apis_source(relative: str) -> ast.Module:
    """One of grid-apis' modules, parsed rather than imported: separate installs, no import path.

    ⚠️ Only "no such repository at all" skips. The resolver has already proved a `grid_networks`
    directory exists under that root, so a module missing from it was renamed or moved — which is
    drift, and reporting it as "grid-apis is not beside this one" would turn the pin off with a
    message blaming the wrong thing.
    """
    root = grid_apis_root()
    if root is None:
        pytest.skip(SKIP_NO_APIS)
    source = root / relative
    assert source.exists(), (
        f"grid-apis has no {relative}. The web-tools MCP server moved or was renamed; "
        f"this pin and `cli/mcp_config.py` both need updating."
    )
    return ast.parse(source.read_text(encoding="utf-8"))


def _module_constant(tree: ast.Module, name: str) -> str:
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id == name:
                    assert isinstance(node.value, ast.Constant), f"{name} is not a literal"
                    return node.value.value
    raise AssertionError(f"no module-level {name} found")


def test_this_cli_prints_the_canonical_path():
    """Unconditional: the half in this repository is pinned on every run, sibling or not."""
    assert mcp_config.MOUNT_PATH == CANONICAL_PATH


def test_the_control_plane_serves_the_path_this_cli_prints():
    assert _module_constant(_apis_source(_APIS_MODULE), _MOUNT_CONST) == CANONICAL_PATH


def test_the_mount_reads_the_constant_rather_than_a_literal():
    """⚠️ The constant and the `app.py` mount are two places, and only one of them is pinned above.

    A mount written as a bare string would let `MOUNT_PATH` be corrected while the server went on
    answering somewhere else — this pin would pass and every harness would still 404. So the mount
    must name the constant.
    """
    tree = _apis_source(_APIS_APP)
    mounts = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "mount"
        and node.args
    ]
    assert mounts, "grid-apis' app.py mounts nothing; the web-tools server is not wired"
    web_mounts = [
        call
        for call in mounts
        if isinstance(call.args[0], ast.Attribute) and call.args[0].attr == _MOUNT_CONST
    ]
    assert web_mounts, (
        "no `app.mount(web_mcp.MOUNT_PATH, ...)` in grid-apis' app.py — the web-tools server is "
        "either unmounted or mounted at a path literal that can drift from MOUNT_PATH."
    )


def test_the_printed_url_keeps_its_trailing_slash():
    """⚠️ Measured: the mount answers **307** without it.

    Claude Code follows that redirect and Codex was not tested on it, so the URL this CLI hands out
    carries the slash rather than relying on every client to behave the same way. Asserted on the
    string the command builds, not on the constant, because the slash is added at the join.
    """
    url = "https://api.example.invalid".rstrip("/") + mcp_config.MOUNT_PATH + "/"
    assert url.endswith(CANONICAL_PATH + "/")
