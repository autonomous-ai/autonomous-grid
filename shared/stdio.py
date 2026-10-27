"""Standard streams in UTF-8 when the locale would leave them ASCII.

CPython started in the C/POSIX locale switches itself to UTF-8 — PEP 538 coerces the locale and
PEP 540 turns on UTF-8 mode — so a `grid` installed from the wheel prints "—" and "…" anywhere. The
Nuitka-built Linux binary does neither: with `LANG` unset (a Docker image, a CI runner, a minimal
server) its stdout is ASCII, and every help screen and every line of output with such a character in
it died with `UnicodeEncodeError` — measured on v0.3.48 and v0.3.49 in `ubuntu:24.04`. Nuitka 4.2 has
no flag for UTF-8 mode, so the CLI does for its own streams what CPython's UTF-8 mode would have done.

Windows has the same failure in a different place. A real console is written in UTF-16 (PEP 528) and
says anything, but a pipe or a file gets the ANSI code page: cp1252 cannot encode "✓", "✗", "→" or
"⚠", and cp437/cp850 not even "—" or "…" — so `grid ls | findstr x` died the same way.
"""
from __future__ import annotations

import codecs
import os
import sys
from collections.abc import Iterable
from typing import Any

_IS_WINDOWS = sys.platform == "win32"

# What the CLI's own output is made of; a stream that cannot hold these cannot print it.
_OUTPUT_SAMPLE = "✓✗→⚠—…"


def use_utf8_in_an_ascii_locale(streams: Iterable[Any] | None = None) -> None:
    """Re-encode each ASCII text stream as UTF-8, keeping its error handler. Never raises.

    Only ASCII is touched off Windows: any other encoding — a UTF-8 terminal, a pytest capture — can
    already say what the CLI prints, or was chosen by somebody. On Windows an **output** stream that is
    not a console and cannot encode the CLI's own characters (a pipe on a legacy code page) is switched
    too; a console is left alone, and so is input, which really is in the code page of whatever program
    produced it. Nothing at all when `PYTHONIOENCODING` is set, which outranks UTF-8 mode in CPython too.
    """
    if os.environ.get("PYTHONIOENCODING"):
        return
    for stream in (sys.stdin, sys.stdout, sys.stderr) if streams is None else streams:
        try:
            if codecs.lookup(stream.encoding).name == "ascii" or _windows_pipe_that_cannot_print(stream):
                # `errors` passed back explicitly: given an encoding alone, `reconfigure` resets the
                # handler to "strict", and stderr's "backslashreplace" is what keeps it from ever raising.
                stream.reconfigure(encoding="utf-8", errors=stream.errors)
        except (AttributeError, TypeError, LookupError, ValueError, OSError):
            continue  # no encoding, no reconfigure, closed, or already read from: leave it as it is


def _windows_pipe_that_cannot_print(stream: Any) -> bool:
    if not _IS_WINDOWS or stream.isatty() or not stream.writable():
        return False
    try:
        _OUTPUT_SAMPLE.encode(stream.encoding)
    except UnicodeEncodeError:
        return True
    return False
