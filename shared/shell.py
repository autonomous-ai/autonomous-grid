"""Rendering a value for a shell to read back.

One function, in `shared/` rather than beside either of its callers, because both places that print
shell exports are **token-printing carve-outs** (ADR 0003 §6) — `grid info --env` and
`grid launch <target> --print-env`. A block a user is invited to `eval` has exactly one correct
quoting, and two copies of it is one copy that goes stale on the day a credential starts containing
a character nobody expected.
"""
from __future__ import annotations


def quote(value: str) -> str:
    """``value`` as a single, always-quoted shell word.

    Not ``shlex.quote``: that leaves a value with no shell-special character bare. A block printed
    for a human has to be quoted **uniformly**, so a reader copying one line out of it never has to
    judge which values needed it — and so that a credential which becomes shell-special after a
    rotation cannot silently turn a working recipe into a different one.

    Single quotes because a shell expands nothing between them — no ``$``, no backtick, no
    backslash, no history ``!``. The only character that cannot appear there is the quote itself, so
    it is closed, escaped and reopened, which is the portable POSIX spelling and leaves the quoting
    balanced for every possible input.
    """
    return "'" + value.replace("'", "'\\''") + "'"
