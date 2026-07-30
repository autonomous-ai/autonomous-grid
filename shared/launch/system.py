"""The one OS-facing seam a launch acts through: find the app, run it, report its exit code.

Two module-level functions rather than a class, so a test substitutes them and asserts on the argument
vector and environment the child was handed — that pair is the whole observable contract of a launch,
and there is nothing meaningful below it to test. Issue 04 adds the TTY pair (interactive-check and
confirm) here, when the install prompt that calls them lands; they are absent now because nothing in
this slice would call them.
"""
from __future__ import annotations

import shutil
import subprocess
from collections.abc import Mapping, Sequence

# A shell reports a signal-killed foreground process as 128 + the signal number. SIGINT is 2.
_SIGNAL_EXIT_BASE = 128
SIGINT_EXIT_CODE = _SIGNAL_EXIT_BASE + 2


def find_executable(name: str) -> str | None:
    """``name`` resolved on ``PATH``, or ``None`` when it is not there."""
    return shutil.which(name)


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
