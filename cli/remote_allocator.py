"""Remote-mode dispatch for allocator enrollment.

Allocator policy remains controller-administered.  A provider may only opt its own already-live
remote identity into managed capacity; all other allocator commands keep their existing local-mode
gate until their remote authorization model is explicit.
"""

from __future__ import annotations

import argparse


def cmd_remote_allocator(args: argparse.Namespace) -> int:
    if getattr(args, "allocator_command", None) == "join":
        return args.handler(args)
    raise SystemExit(
        "`grid allocator` policy commands aren't available; `grid allocator` isn't available in "
        "remote mode yet except for provider enrollment. "
        "`grid allocator join <grid>` is available for an already-serving provider."
    )
