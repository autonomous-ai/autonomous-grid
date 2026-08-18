"""`grid task undo <turn-id>` — take one change back out of the project (ADR 0034 D-l, issue 44).

The counterweight to `grid task create`'s new behaviour, not a convenience. Since ADR 0034 D-d a
finished turn reaches the project by itself, with nobody asked — so the moment a person could decline
is gone, and the only way back has to be something they can press afterwards.

**Its own module rather than a seventh handler in `remote_task.py`**, which is past this repository's
size ceiling; `project_visibility.py` and `project_archive.py` are the precedent. Dispatch still
arrives through `cmd_remote_task`, so `cli/parser.py` needs no new import.

⚠️ **The surface names the TURN and never a commit** (ADR 0034 D-m). Everything this command is
about — reverting a patch, a three-way merge, a trunk that only moves forward — is the relay's, and
none of that vocabulary belongs on the screen of somebody who does not know what a branch is.
"""
from __future__ import annotations

import argparse


def task_undo(args: argparse.Namespace) -> int:
    """Undo one turn's change. Exit 0 when the project no longer has it."""
    from remote import relay
    from . import remote_task

    base, token, _label = remote_task._resolve(args)
    answer = relay.undo_task(base, token, args.task_id)

    # ⚠️ **Emitted, then validated — never returned on** (ADR 0034 D-m, issue 46). The guard below
    # was unreachable under `--json`, which is the one mode an application drives: a reply that did
    # not confirm the undo exited 0 there and 1 in a terminal, so a client was told a change had
    # been taken back out of the project on the strength of a body that never said so. `task_diff`
    # carries the same ordering and the same warning.
    emitted = remote_task._emit(args, answer)

    # ⚠️ Keyed on an explicit `True`, never on truthiness, and this is the fifth time that rule is
    # applied in this CLI (`serves_you`, `archived`, `visibility`, `kind`). Here the misreading is
    # the expensive direction: a relay that answered without the key would have its silence reported
    # as a change taken out of the project, and the person would stop looking for it.
    if not isinstance(answer, dict) or answer.get("undone") is not True:
        raise SystemExit(
            f"The relay's answer for {args.task_id} did not confirm that the change was undone, so "
            f"this cannot be reported as one. The project may be unchanged. "
            f"`grid task undo {args.task_id} --json` shows what it sent.")
    if emitted:
        return 0

    print(f"undone — {args.task_id}'s change is no longer in the project")
    # Said unconditionally rather than only when somebody has worked since, because this CLI cannot
    # tell the two apart from here and the reassurance is what the person actually wants: undoing
    # their own mistake must not look like it might have created somebody else's.
    print("Everything done since then is untouched.")
    return 0
