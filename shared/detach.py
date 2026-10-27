"""Spawn a child that outlives the terminal that started it — on POSIX and on Windows alike.

Every long-lived child the CLI starts (the `__server` of `grid start`, the `__engine` and remote serve
children of `grid join`, CLI seats, the media servers, the background update check, training jobs)
used to pass ``start_new_session=True`` to ``subprocess.Popen``. On POSIX that gives the child its own
session, so the terminal's SIGHUP on close and its Ctrl-C never reach it. On Windows CPython **silently
ignores** the argument: the child stays attached to the terminal's console, so closing the window
(``CTRL_CLOSE_EVENT``) or pressing Ctrl-C in it takes the grid and every engine down with it.

The Windows equivalent is three creation flags:

* ``CREATE_NO_WINDOW`` — the child gets its own hidden console instead of the terminal's, so closing
  the terminal does not reach it; a console grandchild (``llama-server.exe``) inherits that hidden
  console. ``DETACHED_PROCESS`` would leave the child with no console at all, and every console
  grandchild would then open a window of its own.
* ``CREATE_NEW_PROCESS_GROUP`` — Ctrl-C in the terminal is delivered to the terminal's group only.
* ``CREATE_BREAKAWAY_FROM_JOB`` — leave the job object the terminal runs in. OpenSSH's ``sshd`` puts a
  session in a job that kills every member when the session ends — the Windows form of the SIGHUP this
  module exists to dodge. A job that does not allow breakaway refuses the spawn with
  ``ERROR_ACCESS_DENIED``; the child is then started without that flag, still detached from the
  console, which is the most that job permits.

The POSIX call is unchanged byte for byte: the same ``start_new_session=True`` every call site passed.
"""
from __future__ import annotations

import os
import subprocess
from typing import Any

from shared import app_control

_IS_WINDOWS = os.name == "nt"

# Literal values from WinBase.h rather than ``subprocess.CREATE_*``: those attributes exist only on
# Windows, and spelling the numbers here is what lets the Windows branch be unit-tested on Linux CI.
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000
WINDOWS_FLAGS = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW | CREATE_BREAKAWAY_FROM_JOB

_ERROR_ACCESS_DENIED = 5


def popen(argv: list[str], **kwargs: Any) -> subprocess.Popen:
    """``subprocess.Popen(argv, **kwargs)``, detached from the caller's terminal (see the module doc).

    Everything else (``stdout``, ``env``, ``cwd`` …) is passed as it would be to ``Popen``; the caller
    must not pass ``start_new_session`` or ``creationflags`` itself."""
    if not _IS_WINDOWS:
        return subprocess.Popen(argv, start_new_session=True, **kwargs)
    try:
        try:
            return subprocess.Popen(argv, creationflags=WINDOWS_FLAGS, **kwargs)
        except OSError as exc:
            if getattr(exc, "winerror", None) != _ERROR_ACCESS_DENIED:
                raise
        # The job this process runs in does not allow breakaway: detach from the console all the same.
        return subprocess.Popen(argv, creationflags=WINDOWS_FLAGS & ~CREATE_BREAKAWAY_FROM_JOB, **kwargs)
    except OSError as exc:
        if app_control.blocked(exc):
            raise SystemExit(app_control.explain(argv[0])) from None
        raise
