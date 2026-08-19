"""`grid project files | file` — see your project without git (ADR 0034 D-m, issue 45).

The commands the person this product is now for actually needs. Until this slice the only way to
look inside a project was `grid project clone`, which needs git on the machine and refuses up front
without a `git credential capability` probe — a dead end at the first step for somebody who cannot
install developer tools.

**Its own module rather than two more handlers in `remote_project.py`**, which is already at this
repository's 800-line ceiling; `project_visibility.py`, `project_archive.py` and `task_undo.py` are
the precedent. Dispatch still arrives through `cmd_remote_project`, so `cli/parser.py` needs no new
import.

⚠️ **The word "tree" is deliberately not used here.** `remote/task_tree.py` already means a running
task's live workspace snapshot, pushed on the heartbeat — a different noun, and one a person reading
`grid task follow` sees. Two things called "tree" in one product is how somebody ends up debugging
the wrong one.
"""
from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path

# What a listing prints beside a folder, so the eye can separate the two without reading types.
_FOLDER_SUFFIX = "/"


def project_files(args: argparse.Namespace) -> int:
    """List one folder of the project. Exit 0 when the relay answered, whatever it holds."""
    from remote import relay

    from . import remote_project

    base, token, _label = remote_project._resolve(args)
    path = (getattr(args, "path", None) or "").strip("/")
    answer = relay.project_files(base, token, args.project_id, path=path)

    emitted = remote_project._emit(args, answer)

    entries = answer.get("entries")
    if not isinstance(entries, list):
        # The house guard: a reply this command cannot read is not a listing it may report. An empty
        # folder and an unreadable answer must never print the same thing.
        #
        # ⚠️ **Checked AFTER `--json` has printed, never instead of it**, and the first draft got
        # this backwards by returning early. `_emit` is `True` whatever the payload's shape, so an
        # unreadable listing exited **0** under `--json` and **1** without it — the one mode an
        # application actually drives being the one that could not tell. That is exactly what
        # `_project_create`'s guard documents: stdout stays one parseable document, the explanation
        # goes to stderr, and **the exit code carries the verdict**. Found by review; the sibling
        # `project_file` below already had it right, which is what made the split indefensible.
        raise SystemExit(
            f"The relay's answer for {args.project_id} did not contain a readable listing, so "
            f"there is nothing this can show. "
            f"`grid project files {args.project_id} --json` shows what it sent.")

    if emitted:
        return 0

    if answer.get("commit") is None:
        # Not an error: a project created without `--empty` has nothing in it yet. Said plainly,
        # with the two ways forward, because "empty" on its own reads like a fault.
        print(f"{args.project_id} has nothing in it yet.")
        print("Start it with `grid project init` or send a task, and it will fill up.")
        return 0

    where = path or "the top of the project"
    if not entries:
        print(f"{where}: nothing here")
        return 0

    print(f"{where}:")
    for entry in entries:
        print(f"  {_line(entry)}")
    if answer.get("truncated") is True:
        # The honest count, for `remote/task_tree.py`'s reason: "500 files" and "500 of 12,431" are
        # different facts and only the second one tells a person what they are looking at.
        total = answer.get("total")
        print(f"  … showing {len(entries)} of {total} — open a folder to see less at once")
    return 0


def _line(entry) -> str:
    """One entry, as a person reads it. Never raises on a shape the relay might change."""
    if not isinstance(entry, dict):
        return str(entry)
    name = entry.get("name", "?")
    if entry.get("type") == "directory":
        return f"{name}{_FOLDER_SUFFIX}"
    size = entry.get("size")
    marks = "*" if entry.get("executable") is True else ""
    return f"{name}{marks}" + (f"  ({size} bytes)" if isinstance(size, int) else "")


def project_file(args: argparse.Namespace) -> int:
    """Print one file, or write it to `--output`. Exit 0 when its contents were delivered."""
    from remote import relay

    from . import remote_project

    base, token, _label = remote_project._resolve(args)
    path = args.path.strip("/")
    answer = relay.project_file(base, token, args.project_id, path=path)

    emitted = remote_project._emit(args, answer)

    if answer.get("too_large") is True:
        # An answer rather than a refusal on the wire, and a REFUSAL here: this command's promise is
        # the file's contents, and it did not get them. Non-zero, so a script branches correctly.
        size, limit = answer.get("size"), answer.get("limit")
        raise SystemExit(
            f"{path} is {size} bytes, which is more than this grid will send in one piece "
            f"({limit}). `grid project download {args.project_id}` gets the whole project "
            f"including this file.")

    encoding, content = answer.get("encoding"), answer.get("content")
    if encoding not in ("utf-8", "base64") or not isinstance(content, str):
        raise SystemExit(
            f"The relay's answer for {path} did not contain readable contents. "
            f"`grid project file {args.project_id} {path} --json` shows what it sent.")

    raw = base64.b64decode(content) if encoding == "base64" else content.encode("utf-8")
    output = getattr(args, "output", None)
    if output:
        Path(output).write_bytes(raw)
        # To stderr when `--json` was asked for, so stdout stays one parseable document.
        print(f"wrote {len(raw)} bytes to {output}",
              file=sys.stderr if emitted else sys.stdout)
        return 0
    if emitted:
        return 0
    if encoding == "base64":
        # Binary is never printed. A terminal that is handed a PNG stops rendering text, and the
        # person's next command is invisible to them — a worse outcome than being told to use a flag.
        raise SystemExit(
            f"{path} is not text ({answer.get('size')} bytes). "
            f"`grid project file {args.project_id} {path} --output <file>` saves it.")
    sys.stdout.write(content)
    if content and not content.endswith("\n"):
        # A file with no trailing newline would otherwise leave the shell prompt on its last line.
        sys.stdout.write("\n")
    return 0
