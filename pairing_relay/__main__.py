"""Run the cell: `python -m pairing_relay`.

The origin matters more than it looks. It is baked into every host proof, so a
desktop that dials `ws://127.0.0.1:8787` and a cell that believes it is
`ws://localhost:8787` will fail every handshake, with both sides convinced the
other is broken. Pass `--origin` when the address a desktop dials is not the
address this process binds.
"""
from __future__ import annotations

import argparse

import uvicorn

from pairing_relay.cell import Cell
from pairing_relay.server import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument(
        "--origin",
        default=None,
        help="what desktops dial; defaults to ws://<host>:<port>",
    )
    args = parser.parse_args()
    origin = args.origin or f"ws://{args.host}:{args.port}"
    print(f"[pairing-relay] cell up on {args.host}:{args.port}, proofs bound to {origin}")
    uvicorn.run(create_app(Cell(relay_origin=origin)), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
