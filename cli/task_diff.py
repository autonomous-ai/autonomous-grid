"""`grid task diff <turn-id>` — what one task changed (ADR 0034 D-m, issue 45).

The audit surface auto-apply created. Since ADR 0034 D-d every finished task reaches the project by
itself, so "what actually went in, and who asked for it" stopped being answerable by reading a
promotion ledger — that went with `grid project promote`. This is what answers it now, for somebody
who cannot run `git show`.

**Its own module rather than a seventh handler in `remote_task.py`**, which is past this
repository's size ceiling; `task_undo.py` is the precedent, and dispatch still arrives through
`cmd_remote_task`, so `cli/parser.py` needs no new import.
"""
from __future__ import annotations

import argparse
import sys

# What the relay calls a step the grid inserted rather than a message somebody wrote. Compared for
# EQUALITY and never for truthiness — the `serves_you` / `archived` / `visibility` rule: *absent ⇒ a
# person's message*, which is the correct degrade for an older relay, so a truthiness test would
# label every ordinary task as machinery.
_MERGE_KIND = "merge"

# Why there is nothing to show, in the relay's vocabulary. Both are ANSWERS — a task that produced
# nothing, and one whose history the grid's retention window has collected — so neither is an error
# here either, and the exit code stays 0.
_NO_RESULT = "no_result"
_EXPIRED = "expired"


def task_diff(args: argparse.Namespace) -> int:
    """Show one task's change. Exit 0 whenever the grid answered, including "nothing to show"."""
    from remote import relay

    from . import remote_task

    base, token, _label = remote_task._resolve(args)
    answer = relay.turn_diff(base, token, args.task_id)

    # ⚠️ **Emitted, then validated — never returned on.** `_emit` is `True` whatever the payload's
    # shape, so returning here would put the check below out of reach of `--json`, which is the one
    # mode an application drives: an unreadable reply would exit 0 there and 1 in a terminal. The
    # first fix for that guard landed with this early return still above it and reintroduced the
    # split; found by review, twice, which is why the ordering now carries a comment.
    emitted = remote_task_emit(args, answer)

    available = answer.get("available")
    if not isinstance(available, bool):
        # ⚠️ **A reply this command cannot read is NOT a negative answer**, and the first draft made
        # it one — falling through to "nothing to show" for a missing key, a wrong type or an empty
        # object. On the surface that exists to audit what landed, "I do not understand what the
        # relay sent" printed as "this task changed nothing" is the one wrong answer that stops
        # somebody looking. `project_files.py` refuses in the same situation and this now matches it.
        # Found by review.
        raise SystemExit(
            f"The relay's answer for {args.task_id} did not say whether it has a change to show, "
            f"so this cannot report one either way. "
            f"`grid task diff {args.task_id} --json` shows what it sent.")
    if emitted:
        return 0

    if not available:
        # Keyed on an explicit `False` now that the type is known. Both documented reasons — a task
        # that produced nothing, and one whose details the grid no longer keeps — are ANSWERS, so
        # this exits 0.
        print(_nothing_to_show(args.task_id, answer))
        return 0

    who = answer.get("author") or {}
    name = who.get("name") if isinstance(who, dict) else None
    if answer.get("kind") == _MERGE_KIND:
        # ADR 0034 D-g: a merge step is machinery, not a message. Attributing it to the person whose
        # conversation it belongs to would tell them they wrote something they did not.
        print(f"{args.task_id} — a step the grid ran to combine your work with a colleague's")
    elif name:
        print(f"{args.task_id} — asked for by {name}")
    else:
        print(f"{args.task_id}")

    files = answer.get("files")
    if not isinstance(files, list) or not files:
        print("  changed nothing")
        return 0

    for entry in files:
        print(f"  {_file_line(entry)}")

    patch = answer.get("patch")
    if isinstance(patch, str) and patch:
        print()
        sys.stdout.write(patch if patch.endswith("\n") else patch + "\n")
    if answer.get("patch_truncated") is True:
        print("… the rest is too long to show here. "
              "`grid project file` reads any of these files as they are now.")
    return 0


def remote_task_emit(args: argparse.Namespace, payload) -> bool:
    """`--json` short-circuit. Named for the module it mirrors rather than re-implemented, so this
    command's JSON is byte-identical in shape to every other one an application drives."""
    from . import remote_project

    return remote_project._emit(args, payload)


def _nothing_to_show(task_id: str, answer) -> str:
    """The sentence for a task with no change to show — one per reason, and never a git word."""
    reason = answer.get("reason") if isinstance(answer, dict) else None
    if reason == _EXPIRED:
        return (f"{task_id} ran too long ago — this grid no longer keeps the details of what it "
                f"changed. What it did and why is still in `grid task get {task_id}`.")
    if reason == _NO_RESULT:
        return f"{task_id} did not change anything in the project."
    # An unknown reason is DISPLAYED, never guessed at — the `task.retry` rule. A relay free to add
    # reasons without a client release is the whole point of not comparing on the wording.
    return (f"{task_id} has no change to show"
            + (f" ({reason})." if isinstance(reason, str) and reason else "."))


def _file_line(entry) -> str:
    """One changed file, as a person reads it. Never raises on a shape the relay might change."""
    if not isinstance(entry, dict):
        return str(entry)
    path = entry.get("path", "?")
    status = entry.get("status", "changed")
    if entry.get("binary") is True:
        return f"{status:>7}  {path}  (not text)"
    added, deleted = entry.get("added"), entry.get("deleted")
    counts = ""
    if isinstance(added, int) and isinstance(deleted, int):
        counts = f"  +{added} -{deleted}"
    return f"{status:>7}  {path}{counts}"
