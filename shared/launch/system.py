"""The one OS-facing seam a launch acts through: find the app, ask about it, run it, report its code.

Module-level functions rather than a class, so a test substitutes them and asserts on the argument
vector and environment the child was handed — that pair is the whole observable contract of a launch.

Each is the smallest primitive that still hides an OS detail, and *policy* stays above them: these
answer "is there a runnable file here" and "did the user say yes", never "where should we look" or
"what should we do about it". That is what lets one target search two install locations and the next
target search none, with nothing here to change.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

# A shell reports a signal-killed foreground process as 128 + the signal number. SIGINT is 2.
_SIGNAL_EXIT_BASE = 128
SIGINT_EXIT_CODE = _SIGNAL_EXIT_BASE + 2


def find_executable(name: str) -> str | None:
    """``name`` resolved on ``PATH``, or ``None`` when it is not there."""
    return shutil.which(name)


def executable_at(path: Path) -> tuple[str | None, str | None]:
    """``path`` as a runnable command, and why that could not be determined. At most one is not None.

    The companion to ``find_executable`` for a candidate an app is known to install itself into, where
    there is no name to search for — only a place to look. *Which* places is the target's own
    knowledge, not this module's.

    Symlinks are followed on purpose: the conventional location for a self-updating app is a launcher
    symlink into a versioned directory, so refusing links would reject the ordinary install.

    **Nothing here and could-not-look are separate answers**, the same distinction ``claude._settings_env``
    draws for the same reason. ``Path.is_file`` hides only the errnos that *mean* absent — ENOENT,
    ENOTDIR, EBADF, ELOOP — so a dangling symlink or a missing parent is a plain ``(None, None)``.
    Everything else is a real obstacle: a ``~/.local/bin`` whose traverse bit is stripped raises
    ``PermissionError``, and reporting that as "not installed" would tell a user who *has* the app that
    they do not, then offer an install that hits the same wall.

    It still never raises, because a caller with two candidates must be able to try the second one.
    """
    try:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path), None
    except OSError as exc:
        return None, exc.strerror or str(exc)
    return None, None


# The TTY pair below duplicates a private one in `cli/provider.py` (`_interactive` / `_confirm`).
# `interactive` is byte-for-byte identical to `_interactive`; `confirm` shares the prompt shape but
# **not** the body — it treats EOF and Ctrl-C as a decline, which the provider's version does not, so
# there those two escape as a traceback. The duplication is deliberate and recorded rather than fixed:
# `shared/launch` must not import `cli`, so consolidating would mean moving the provider's pair into
# `shared/` and re-pointing its callers — a change to the join path, in a slice about launching an app.
# Explicitly out of scope, and `cli/provider.py` is left untouched.


def interactive() -> bool:
    """Whether there is a person on the other end who could answer a question.

    Both streams, because a question needs both halves: stdout carries the prompt (``input`` writes it
    there) and stdin carries the answer. Requiring stdout in particular is what keeps the prompt out of
    a captured pipe — ``grid launch claude … | jq`` gets the app's output, never a half-written
    question waiting on input that will never come.
    """
    return sys.stdin.isatty() and sys.stdout.isatty()


def confirm(question: str) -> bool:
    """Ask ``question`` and return whether the answer was yes. Anything but an explicit yes is no.

    ``EOFError`` and ``KeyboardInterrupt`` are answers, not faults: a user who hits Ctrl-D or Ctrl-C at
    a prompt has declined, and the caller's clean refusal is the right outcome for both. Letting either
    escape would turn "no thanks" into a traceback.
    """
    try:
        return input(f"{question} [y/N] ").strip().lower() == "y"
    except (EOFError, KeyboardInterrupt):
        return False


def spawn(argv: Sequence[str], env: Mapping[str, str]) -> int:
    """Run ``argv`` in the foreground to completion with exactly ``env``, returning its exit code.

    The child inherits this process's stdio and process group, so it owns the terminal and a Ctrl-C is
    delivered straight to it by the terminal — which is the whole point: the app must behave the way it
    does when the user runs the binary themselves.

    ``subprocess.run`` cannot be used for that. Its implementation wraps the wait in a bare ``except:``
    that calls ``process.kill()``, so the ``KeyboardInterrupt`` the same signal raises *here* SIGKILLs
    the app in the middle of the shutdown it had already started — verified against a real child that
    traps SIGINT: its cleanup never finished. So the child is waited for again instead.

    Every signal death is reported the way a shell reports it, 128 + the signal number, so what this
    returns always *is* an exit code.
    """
    try:
        process = subprocess.Popen(list(argv), env=dict(env))
    except OSError as exc:
        # A binary that resolved but cannot be executed (deleted, or not executable): a clean error
        # naming it, never a traceback out of subprocess.
        raise SystemExit(f"Could not start {argv[0]}: {exc}") from exc
    try:
        code = process.wait()
    except KeyboardInterrupt:
        try:
            code = process.wait()  # the app already has the signal; let it finish on its own terms
        except KeyboardInterrupt:
            # A second Ctrl-C: the user wants their shell back more than they want a clean app exit.
            # The child keeps every signal the terminal already sent it — killing it here would be the
            # very thing this function exists to avoid — so stop waiting and report the interrupt.
            return SIGINT_EXIT_CODE
    # A child killed by a signal has no exit status; POSIX reports ``-signum``. Passing that out would
    # reach the shell as 256-signum (SIGKILL → 247), so it is normalised to what a shell would say.
    return _SIGNAL_EXIT_BASE - code if code < 0 else code
