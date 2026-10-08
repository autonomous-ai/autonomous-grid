"""Fixtures for the conformance suite (grid-platform ticket 13). Run it by naming the directory:

    GRID_SRC_REPO=… GRID_APIS_REPO=… <grid-src>/.venv/bin/python -m pytest conformance

It runs under grid-src's interpreter, which has what it needs (pytest, httpx, PyJWT, cryptography, grid-protocol).

⚠️ **A conformance run that cannot find the server must FAIL in CI, not skip.** pytest exits 0 when every test
skips, so a CI job whose sibling checkout went missing would report green forever. With `GRID_CONFORMANCE_REQUIRED=1`
(both workflows set it) the missing sibling is an error; on a laptop without them it is a skip.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# The suite checks every answer against ITS OWN `protocol/` (the shapes this checkout defines), not against whichever
# `grid-protocol` wheel grid-src happens to pin: a schema changed here first must be what the server is held to.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "protocol"))

from conformance.clients import RELEASED, Client, harness_pin, install
from conformance.stack import Stack, locate_siblings


def _versions() -> list[str]:
    pin = harness_pin()
    return [*RELEASED, *([pin] if pin not in RELEASED else [])]


VERSIONS = _versions()


def pytest_report_header(config) -> list[str]:
    return [f"conformance: released {', '.join(RELEASED)}; harness pin {harness_pin()}"]


@pytest.fixture(scope="session")
def siblings():
    found = locate_siblings()
    if found is None:
        if os.environ.get("GRID_CONFORMANCE_REQUIRED") == "1":
            pytest.fail("grid-src and grid-apis are required (GRID_SRC_REPO, GRID_APIS_REPO), and one is missing")
        pytest.skip("grid-src or grid-apis is not beside this checkout; set GRID_SRC_REPO and GRID_APIS_REPO")
    return found


@pytest.fixture(scope="session")
def stack(siblings, tmp_path_factory):
    with Stack.up(siblings, tmp_path_factory.mktemp("stack")) as running:
        yield running


@pytest.fixture(autouse=True)
def grid_starts_awake(request):
    """Every test starts on an awake grid, whatever the previous one left."""
    if "stack" in request.fixturenames:
        request.getfixturevalue("stack").set_state("running")


@pytest.fixture(scope="session")
def executables():
    return {}


@pytest.fixture
def client(request, executables, tmp_path) -> Client:
    version = request.param
    if version not in executables:
        executables[version] = install(version)
    return Client(version, executables[version], tmp_path / f"home-{version}")


def pytest_generate_tests(metafunc):
    if "client" in metafunc.fixturenames:
        metafunc.parametrize("client", VERSIONS, ids=[f"grid-{v}" for v in VERSIONS], indirect=True)

