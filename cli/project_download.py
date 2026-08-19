"""`grid project download` — take the whole project away (ADR 0034 D-m, issue 45).

PRD-non-dev user story 18: *"I want to download the project, so that I can use it elsewhere."* The
answer used to be `grid project clone`, which needs git and a working credential helper.

⚠️ **Not called `archive`.** `grid project archive` puts a project AWAY (ADR 0033 D-p, issue 33), and
that command's whole promise is that reading keeps working — so the two would sit beside each other
meaning opposite things.
"""
from __future__ import annotations

import argparse
from pathlib import Path


def project_download(args: argparse.Namespace) -> int:
    """Write the project to a zip. Exit 0 once the whole file is on disk."""
    from remote import relay

    from . import remote_project

    base, token, _label = remote_project._resolve(args)
    destination = Path(getattr(args, "output", None) or f"{args.project_id}.zip")
    if destination.is_dir():
        # A directory is a plausible thing to type and would otherwise fail inside the streamer with
        # an `IsADirectoryError` after the download had already been paid for.
        raise SystemExit(f"{destination} is a folder. Give --output a file name to write to.")

    written = relay.download_project(base, token, args.project_id, destination)

    if written == 0:
        # ⚠️ **Not "the project is empty" — that is a 22-byte zip, not a 0-byte one.** MEASURED:
        # `git archive` of the empty tree still writes a valid end-of-central-directory record, and
        # a project with no trunk at all is refused by the relay with `project_has_no_trunk` before
        # anything streams. So there is no legitimate way to get here, and the first draft's
        # reassuring note would have described a real fault as normal. Found by review.
        raise SystemExit(
            f"The relay sent no data for {args.project_id}, so nothing was written. This is not "
            f"what an empty project looks like — try again, and tell your grid's operator if it "
            f"keeps happening.")

    if remote_project._emit(args, {"project_id": args.project_id,
                                   "path": str(destination), "bytes": written}):
        return 0
    print(f"wrote {written} bytes to {destination}")
    return 0
