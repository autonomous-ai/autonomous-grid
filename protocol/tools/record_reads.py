"""Record one credential-less read of a grid as a `grid-protocol` recording: validated, sanitized, written.

    python protocol/tools/record_reads.py URL --schema S [--definition D] --stem STEM --source TEXT [--out DIR]

URL is a grid's public read through the platform's proxy — ``<grid address>/relay/v1/grid/overview`` or
``<grid address>/nodes/discover`` — or any URL whose answer is a shape the schemas define (a deleted grid's 410, an
unknown network's 404). It is sent with NO credential, on purpose and with no way to add one: a credential-less read
never wakes a sleeping grid, so recording one is free, and a recording must never be what woke it. An address that
carries a user name or password is refused.

The answer is written only if it is the named shape (``--schema`` and ``--definition``, as in
``grid_protocol.validator``) and, for a coded refusal, carries the status the contract gives that code. Its body is
sanitized first (``sanitize.py``). To record an AWAKE grid's reads, the grid has to be awake: waking it is a person's
act (a signed-in read, inference, an owner's start), never this tool's.
"""
from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import sys
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import grid_protocol  # noqa: E402
from grid_protocol import constants  # noqa: E402
from sanitize import Sanitizer  # noqa: E402

DEFAULT_OUT = pathlib.Path(__file__).resolve().parent.parent / "recordings"
USER_AGENT = f"grid-protocol-recorder/{grid_protocol.__version__} (no-wake)"
TIMEOUT_SECONDS = 15.0


class NotTheShape(ValueError):
    """The answer is not the shape it was meant to be recorded as; nothing was written."""


def _check(body: object, status: int, schema: str, definition: str | None) -> None:
    errors = list(grid_protocol.validator(schema, definition).iter_errors(body))
    if errors:
        details = "; ".join(f"{list(error.path)}: {error.message}" for error in errors[:5])
        raise NotTheShape(f"the answer is not {schema}/{definition or 'root'}: {details}")
    envelope = body.get("error") if isinstance(body, dict) and isinstance(body.get("error"), dict) else {}
    code = (body.get("code") if isinstance(body, dict) else None) or envelope.get("code")
    expected = constants.REFUSAL_STATUS.get(code) if isinstance(code, str) else None
    if expected is not None and expected != status:
        raise NotTheShape(f"`{code}` came with {status}; the contract says {expected}")


def record(url: str, *, schema: str, definition: str | None, stem: str, source: str,
           out: pathlib.Path = DEFAULT_OUT, transport: httpx.BaseTransport | None = None) -> pathlib.Path:
    """Take the read, check it, sanitize it and write ``<out>/<stem>.json``; return that path."""
    parts = urlsplit(url)
    if parts.username or parts.password:
        raise ValueError("refusing an address that carries a credential: a recording is a credential-less read")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    with httpx.Client(transport=transport, timeout=TIMEOUT_SECONDS, follow_redirects=False) as client:
        response = client.get(url, headers=headers)
    body = response.json()
    _check(body, response.status_code, schema, definition)
    route = parts.path
    for known in (constants.OVERVIEW_PATH, constants.DISCOVER_PATH):
        if route.endswith(known):
            route = known
    recording = Sanitizer().recording({
        "schema": schema, "definition": definition, "request": f"GET {route}", "status": response.status_code,
        "source": source, "recorded_at": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%MZ"),
        "body": body,
    })
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"{stem}.json"
    target.write_text(json.dumps(recording, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python protocol/tools/record_reads.py", description=__doc__.splitlines()[0])
    parser.add_argument("url")
    parser.add_argument("--schema", required=True)
    parser.add_argument("--definition", default=None)
    parser.add_argument("--stem", required=True, help="the file name, without .json (<schema>--<definition>--<case>)")
    parser.add_argument("--source", required=True, help="where the answer came from, with no address of ours")
    parser.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    try:
        written = record(args.url, schema=args.schema, definition=args.definition, stem=args.stem,
                         source=args.source, out=args.out)
    except NotTheShape as exc:
        print(f"not written: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
