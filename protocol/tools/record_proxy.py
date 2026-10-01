"""Record the platform proxy's refusals a live grid cannot be put into without a write, from its own code.

    python protocol/tools/record_proxy.py --grid-apis PATH [--out DIR]

`grid_stopped` needs a grid its OWNER stopped, and `grid_master_down` a master that died while its grid should be
running; producing either on a shared box means changing a grid. So they are recorded from grid-apis' proxy itself,
run in-process (`grid_proxy.build_app`) in front of a closed loopback port — the harness grid-apis' own
`tests/test_proxy_refusals.py` uses, so the refusal comes from the production code path, not a mock. PATH is a
grid-apis checkout; the recording names its commit. Each answer is validated against its schema and sanitized before
it is written, like every other recording.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

TOOLS = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS.parent))

import grid_protocol  # noqa: E402
from sanitize import Sanitizer  # noqa: E402

DEFAULT_OUT = TOOLS.parent / "recordings"
NETWORK = "grid-0123456789abcdef"
SIGNED_IN = {"Authorization": "Bearer token-of-somebody"}  # a fake token; the proxy only tests its presence


async def _cannot_ask(network_id: str) -> None:
    return None


async def _restarting(network_id: str) -> dict:
    return {"network_id": network_id, "coming_up": True, "reason": "restarting"}


def _cases(grid_proxy, closed_port):
    """(file stem, definition, request, method, path, the proxy, headers) for each refusal recorded."""
    def proxy(state: str, revive=_cannot_ask):
        port = closed_port()
        return grid_proxy.build_app(get_port=lambda nid: {"host": "127.0.0.1", "port": port},
                                    record_activity=lambda nid: None, read_grid_state=lambda nid: state,
                                    revive=revive, wake=_cannot_ask, wake_wait_timeout=5.0)
    return [
        ("refusal--GridStopped--in-process", "GridStopped", "GET /relay/v1/grid/overview", "GET",
         "/relay/v1/grid/overview", proxy("stopped"), {}),
        ("refusal--GridMasterDown--in-process", "GridMasterDown", "POST /nodes/heartbeat", "POST",
         "/nodes/heartbeat", proxy("running"), {}),
        ("refusal--GridMasterDown--restarting-in-process", "GridMasterDown", "POST /nodes/heartbeat", "POST",
         "/nodes/heartbeat", proxy("running", revive=_restarting), SIGNED_IN),
    ]


def record(grid_apis: pathlib.Path, out: pathlib.Path = DEFAULT_OUT) -> list[pathlib.Path]:
    sys.path.insert(0, str(grid_apis))
    from starlette.testclient import TestClient

    import grid_proxy
    from tests._proxy import closed_port

    sha = subprocess.run(["git", "-C", str(grid_apis), "rev-parse", "--short", "HEAD"], capture_output=True,
                         text=True, check=True).stdout.strip()
    written = []
    out.mkdir(parents=True, exist_ok=True)
    for stem, definition, request, method, path, app, headers in _cases(grid_proxy, closed_port):
        response = TestClient(app).request(method, f"/{NETWORK}{path}", content=b"{}", headers=headers)
        body = response.json()
        errors = list(grid_protocol.validator("refusal", definition).iter_errors(body))
        if errors:
            raise SystemExit(f"{stem}: the proxy's answer is not refusal/{definition}: {errors[0].message}")
        recording = Sanitizer().recording({
            "schema": "refusal", "definition": definition, "request": request, "status": response.status_code,
            "headers": {name: value for name, value in response.headers.items() if name.lower() == "retry-after"},
            "source": f"grid-apis @ {sha}, grid_proxy.build_app in-process against a closed port (its own "
                      "tests/test_proxy_refusals.py harness); the DEV VM has no grid in this state",
            "body": body,
        })
        target = out / f"{stem}.json"
        target.write_text(json.dumps(recording, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append(target)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python protocol/tools/record_proxy.py", description=__doc__.splitlines()[0])
    parser.add_argument("--grid-apis", type=pathlib.Path, required=True, help="a grid-apis checkout")
    parser.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    for path in record(args.grid_apis.resolve(), args.out):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
