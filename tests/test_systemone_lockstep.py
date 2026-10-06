"""System One's wire names, pinned across the repo boundary to the relay that serves them.

Two things a remote engine says about decision models are read by the relay, which keeps its own
copy of each with no import path between the repositories:

* the endpoint literal (`systemone`) — the relay's route and the `endpoint_path` it queues jobs
  under. Drift is silent: an engine advertising a name the relay never asks for gets no decisions.
* Jev's default model names (`default`, `jev-latest`, …) — the relay keeps them out of `/models`
  (`systemone_contract.DEFAULT_MODEL_ALIASES`). An alias here and not there would sit in every chat
  picker as a model, and fail when picked; one there and not here would never reach this engine.

⚠️ Like every cross-repo check in this repository, this **skips unless grid-src sits beside this
worktree** (or `GRID_SRC_REPO` names it) — so it skips in CI. Run it locally with both checkouts.
"""
from __future__ import annotations

import ast

import pytest

from remote import probe, serve
from tests.grid_src_repo import grid_src_private_server as _grid_src_private_server


def _relay_source(module: str) -> str:
    package = _grid_src_private_server()
    if package is None or not (package / module).exists():
        pytest.skip("grid-src worktree is not beside this one; the lockstep cannot be checked here")
    return (package / module).read_text()


def _relay_aliases() -> frozenset[str]:
    """grid-src's `DEFAULT_MODEL_ALIASES`, parsed rather than imported. Any shape this does not
    understand is an assertion, never a shrug: a check that reads a set partly is worse than none."""
    for node in ast.parse(_relay_source("systemone_contract.py")).body:
        if not (isinstance(node, ast.Assign)
                and any(getattr(t, "id", None) == "DEFAULT_MODEL_ALIASES" for t in node.targets)):
            continue
        value = node.value
        if isinstance(value, ast.Call) and len(value.args) == 1:  # frozenset({...})
            value = value.args[0]
        assert isinstance(value, (ast.Set, ast.Tuple, ast.List)), (
            "grid-src's DEFAULT_MODEL_ALIASES is no longer a literal collection; teach this check its shape")
        assert all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in value.elts)
        return frozenset(e.value for e in value.elts)
    raise AssertionError("DEFAULT_MODEL_ALIASES moved out of grid-src's systemone_contract.py; find it")


def test_this_engine_answers_exactly_the_names_the_relay_hides():
    assert frozenset(serve._SYSTEMONE_ALIASES) == _relay_aliases(), (
        "grid-src's systemone_contract.DEFAULT_MODEL_ALIASES and remote/serve.py's _SYSTEMONE_ALIASES "
        "have drifted; edit both sides")


def test_the_relay_serves_the_endpoint_this_engine_advertises():
    source = _relay_source("relay.py")
    assert f'@router.post("/{probe.SYSTEMONE_ENDPOINT}")' in source
    assert f'_relay_inference(request, "{probe.SYSTEMONE_ENDPOINT}"' in source
