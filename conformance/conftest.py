"""Fixtures for the conformance suite (grid-platform ticket 13; M5 ticket 07). Run it by naming the directory:

    GRID_SRC_REPO=… GRID_APIS_REPO=… <grid-src>/.venv/bin/python -m pytest conformance

It runs under grid-src's interpreter, which has what it needs (pytest, httpx, PyJWT, cryptography, grid-protocol, and
what the pool target imports). The server side is what serves every grid — relay instances on two relay pools, the
projector, grid-apis' real proxy — and is grid-platform's own pool conformance target, taken from the sibling grid-src
(`conformance.stack.pool_target_from`): its `pools` fixture is the session's stack, and each test asks its checks of a
grid of its own, created by rows on one of those pools.

⚠️ **A conformance run that cannot find the server must FAIL in CI, not skip.** pytest exits 0 when every test
skips, so a CI job whose sibling checkout went missing would report green forever. With `GRID_CONFORMANCE_REQUIRED=1`
the missing sibling is an error; on a laptop without them it is a skip.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# The suite checks every answer against ITS OWN `protocol/` (the shapes this checkout defines), not against whichever
# `grid-protocol` wheel grid-src happens to pin: a schema changed here first must be what the server is held to.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "protocol"))

from conformance.clients import Client, harness_pin, install, released, versions
from conformance.stack import locate_siblings, pool_target_from

VERSIONS = versions()
SIBLINGS = locate_siblings()
#: The sibling grid-src's pool target, or None without the siblings (the `siblings` fixture then skips or fails).
POOL_TARGET = pool_target_from(SIBLINGS) if SIBLINGS is not None else None
if POOL_TARGET is not None:
    pools = POOL_TARGET.pools  # the session's relay pools, projector and proxy: a fixture, registered from here


def pytest_report_header(config) -> list[str]:
    chosen = os.environ.get("GRID_CONFORMANCE_CLIENTS") or "all"
    stack = f"the pool target of {SIBLINGS.grid_src}" if SIBLINGS is not None else "no siblings"
    return [f"conformance: released ({chosen}) {', '.join(released())}; harness pin {harness_pin()}; {stack}"]


@pytest.fixture(scope="session")
def siblings():
    if SIBLINGS is None:
        if os.environ.get("GRID_CONFORMANCE_REQUIRED") == "1":
            pytest.fail("grid-src and grid-apis are required (GRID_SRC_REPO, GRID_APIS_REPO), and one is missing")
        pytest.skip("grid-src or grid-apis is not beside this checkout; set GRID_SRC_REPO and GRID_APIS_REPO")
    return SIBLINGS


@pytest.fixture
def stack(siblings, request):
    """A grid of its own, created by rows on the first relay pool, permissionless: what a released CLI joins."""
    return POOL_TARGET.PoolGrid(request.getfixturevalue("pools"), POOL_TARGET.POOLS[0], POOL_TARGET.BORN)


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
