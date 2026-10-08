"""grid-apis' REAL proxy app, with its control-plane reads answered from a state file (grid-platform ticket 13).

Run with grid-apis' own Python, from grid-apis' checkout:

    GRID_CONFORMANCE_STATE=/path/state.json python <this file> <port>

`grid_proxy.build_app` takes every dependency it has on the control plane as a parameter: the port of a grid, its sleep
state, its sleep record, the wake. Those are what a conformance run controls, so they come from `state.json`:

    {"grids": {"<network_id>": {"port": 13071, "state": "running", "last_known": {...} | null}}}

What is NOT replaced is everything the proxy decides on the wire: which route may wake, which refusal and code a grid
that is not up gets, how `last_known` rides the answer, what is forwarded. That is what the clients are tested against.

A grid keeps its port whatever its state, as on the fleet; the conformance run stops its master while it is not
`running`, so the proxy finds nobody there and reads its state. Asked to wake it, this flips it to `running` in the file,
and the run brings the master back, as the control plane's wake would.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

STATE = Path(os.environ["GRID_CONFORMANCE_STATE"])


def _grids() -> dict:
    return json.loads(STATE.read_text())["grids"]


def _grid(network_id: str) -> dict | None:
    return _grids().get(network_id)


def get_port(network_id: str) -> dict | None:
    grid = _grid(network_id)
    return {"host": "127.0.0.1", "port": grid["port"]} if grid else None


def read_grid_state(network_id: str) -> str | None:
    grid = _grid(network_id)
    return grid["state"] if grid else None


def read_last_known(network_id: str) -> dict | None:
    grid = _grid(network_id)
    return grid.get("last_known") if grid else None


async def wake(network_id: str) -> dict | None:
    """The control plane's wake: flip an asleep grid to `running`, under the state file's lock (the test writes it too)."""
    import fcntl

    with open(STATE.with_suffix(".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(STATE.read_text())
        grid = state["grids"].get(network_id)
        if grid is None or grid["state"] != "asleep":
            return None
        grid["state"] = "running"
        grid["woken"] = grid.get("woken", 0) + 1
        tmp = STATE.with_name(f".{STATE.name}.{os.getpid()}")
        tmp.write_text(json.dumps(state))
        tmp.replace(STATE)
    return {"network_id": network_id, "coming_up": True, "reason": "starting"}


async def no_revive(network_id: str) -> dict | None:
    """The admin revive is out of scope (README): a master that is down while `running` stays down here."""
    return None


def main() -> None:
    import grid_proxy
    import uvicorn

    app = grid_proxy.build_app(
        get_port=get_port,
        read_grid_state=read_grid_state,
        read_last_known=read_last_known,
        wake=wake,
        revive=no_revive,
        record_activity=lambda network_id: None,
        record_served=lambda network_id: None,
        record_inference=lambda network_id: None,
        available_memory_mb=lambda: 1_000_000.0,
        observe_wake=lambda observed: None,
        wake_wait_timeout=30.0,
    )
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")


if __name__ == "__main__":
    main()
