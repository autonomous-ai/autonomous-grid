"""The sleep seam's wire values, pinned across the repository boundary (`idle-sleep` issues 02, 04).

grid-apis' proxy answers a request to a grid that is not up with a code beside ``detail``, and this
CLI's provider acts on three of them: it PARKS on ``grid_asleep`` (issue 02) and ``grid_stopped`` (the
owner stopped the grid, issue 04), and STOPS on ``grid_deleted`` (issue 04). And the control plane's
status endpoint says ``asleep`` for a grid its reaper slept, which this CLI calls rather than refusing
(`cli/remote_grid.resolve_relay_base`, issue 04). There is no import path between the two, so each value
is hand-duplicated and kept in step by editing both sides — and by this file.

**Every skew direction degrades to today's behaviour**, which is what makes the rollout order a
convenience: an unknown code is transient, and a missing one is too. The exception is ``asleep``, whose
rollout is CLI FIRST — a CLI too old to know it refuses a sleeping grid with "run `grid start` first",
which works, but is the deadlock this value ends. What is NOT loud is a **rename**: renamed on either
side, both suites stay green and the behaviour silently goes back to what it was.

⚠️ Per this repository's rule for every cross-repo assertion, the grid-apis cases **skip unless that
worktree sits beside this one** — i.e. they skip in CI, and a green CI proves nothing about them. Run
them locally, on a machine that has both.

The canonical values are written out here rather than imported from either side: a pin that reads one
side's constant and compares it to itself checks nothing.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from cli import remote_grid
from remote import relay
from tests.grid_src_repo import grid_apis_root

#: (grid-apis `grid_proxy` constant, this CLI's `remote.relay` constant, the canonical value).
PROXY_CODES = [
    ("GRID_ASLEEP_CODE", "GRID_ASLEEP_CODE", "grid_asleep"),
    ("GRID_STOPPED_CODE", "GRID_STOPPED_CODE", "grid_stopped"),
    ("GRID_DELETED_CODE", "GRID_DELETED_CODE", "grid_deleted"),
]
CANONICAL_ASLEEP_STATE = "asleep"

PROXY_MODULE = "grid_proxy.py"
STATE_MODULE = pathlib.Path("grid_networks") / "grid_sleep_state.py"
SKIP_NO_APIS = "the grid-apis worktree is not beside this one; the lockstep cannot be checked here"


def _tree(relative: pathlib.Path | str) -> ast.Module:
    """A grid-apis module, parsed rather than imported — separate installs, no import path.

    Only "no such repository" skips. A grid-apis checkout without the module means it was renamed or
    moved, which is drift, and skipping would turn the pin off blaming the wrong thing.
    """
    root = grid_apis_root()
    if root is None:
        pytest.skip(SKIP_NO_APIS)
    source = root / relative
    if not source.exists():
        raise AssertionError(
            f"grid-apis is at {root} but has no {relative} — teach this check where it went rather "
            f"than letting it skip")
    return ast.parse(source.read_text())


def _module_constant(tree: ast.Module, name: str) -> list[object]:
    return [
        node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(getattr(target, "id", None) == name for target in node.targets)
        and isinstance(node.value, ast.Constant)
    ]


@pytest.mark.parametrize(("apis_name", "_cli_name", "canonical"), PROXY_CODES)
def test_the_proxy_answers_with_each_code_this_cli_acts_on(apis_name, _cli_name, canonical):
    values = _module_constant(_tree(PROXY_MODULE), apis_name)
    assert values == [canonical], (
        f"grid-apis' `{apis_name}` is {values!r}. Renamed on one side, providers silently go back to "
        f"treating it as a transient failure — edit BOTH sides")


@pytest.mark.parametrize(("apis_name", "_cli_name", "_canonical"), PROXY_CODES)
def test_the_proxy_sends_each_code_beside_detail_where_this_cli_reads_it(
    apis_name, _cli_name, _canonical
):
    """The shape is half the contract. This CLI reads the code at the TOP LEVEL of the answer
    (`relay._answer_code`); nested under `detail` — the task plane's shape — it would be a code this
    CLI never finds."""
    beside_detail = [
        node
        for node in ast.walk(_tree(PROXY_MODULE))
        if isinstance(node, ast.Dict)
        and "detail" in {key.value for key in node.keys if isinstance(key, ast.Constant)}
        and any(
            isinstance(key, ast.Constant) and key.value == "code"
            and isinstance(value, ast.Name) and value.id == apis_name
            for key, value in zip(node.keys, node.values)
        )
    ]
    assert beside_detail, (
        f"grid-apis' proxy no longer builds an answer `{{'detail': …, 'code': {apis_name}}}` — moved "
        f"or reshaped, this CLI stops finding the code")


def test_the_control_plane_says_asleep_in_the_word_this_cli_calls():
    values = _module_constant(_tree(STATE_MODULE), "ASLEEP")
    assert values == [CANONICAL_ASLEEP_STATE], (
        f"grid-apis' `grid_sleep_state.ASLEEP` is {values!r}. Renamed there alone, this CLI refuses every "
        f"sleeping grid with 'run `grid start` first' — edit BOTH sides")


# --- this CLI's halves: need no sibling worktree, so they are the ones that run in CI -------------------


@pytest.mark.parametrize(("_apis_name", "cli_name", "canonical"), PROXY_CODES)
def test_this_cli_acts_on_the_canonical_code(_apis_name, cli_name, canonical):
    assert getattr(relay, cli_name) == canonical


def test_this_cli_calls_a_grid_in_the_canonical_asleep_state():
    assert CANONICAL_ASLEEP_STATE in remote_grid._CALLABLE_STATES


# --- the timing pair: a parked provider's probe against the woken master's boot hold (issue 04) --------


def _boot_hold_default() -> float:
    from tests.grid_src_repo import grid_src_private_server

    root = grid_src_private_server()
    if root is None:
        pytest.skip("the grid-src worktree is not beside this one; the lockstep cannot be checked here")
    source = root / "relay.py"
    values = _module_constant(ast.parse(source.read_text()), "DEFAULT_BOOT_HOLD_SECONDS")
    assert len(values) == 1 and isinstance(values[0], (int, float)), (
        f"grid-src's relay.py no longer defines DEFAULT_BOOT_HOLD_SECONDS as a number ({values!r}) — "
        f"teach this check where the boot hold's default went rather than letting it pass")
    return float(values[0])


@pytest.mark.parametrize("probe_name", ["_ASLEEP_PROBE_SECONDS", "_AFTER_A_FAILED_BEAT_SECONDS"])
def test_a_parked_provider_probes_well_inside_the_woken_masters_boot_hold(probe_name):
    """A woken (or restarted) master holds a request that finds no live provider for up to
    `DEFAULT_BOOT_HOLD_SECONDS` (grid-src), and a provider parked on a sleeping grid is back only on its
    next heartbeat. The hold must outlast a WHOLE probe interval plus the beat's own round trip, so the
    probe is held to at most half of it. ⚠️ Raise the probe or shorten the hold and nothing fails — the
    first request after every wake answers `no_providers_available` again, silently."""
    from remote import serve

    assert getattr(serve, probe_name) * 2 <= _boot_hold_default()
