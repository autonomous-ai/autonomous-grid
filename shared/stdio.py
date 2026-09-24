"""Standard streams in UTF-8 when the locale would leave them ASCII.

CPython started in the C/POSIX locale switches itself to UTF-8 — PEP 538 coerces the locale and
PEP 540 turns on UTF-8 mode — so a `grid` installed from the wheel prints "—" and "…" anywhere. The
Nuitka-built Linux binary does neither: with `LANG` unset (a Docker image, a CI runner, a minimal
server) its stdout is ASCII, and every help screen and every line of output with such a character in
it died with `UnicodeEncodeError` — measured on v0.3.48 and v0.3.49 in `ubuntu:24.04`. Nuitka 4.2 has
no flag for UTF-8 mode, so the CLI does for its own streams what CPython's UTF-8 mode would have done.
"""
from __future__ import annotations

import codecs
import os
import sys
from collections.abc import Iterable
from typing import Any


def use_utf8_in_an_ascii_locale(streams: Iterable[Any] | None = None) -> None:
    """Re-encode each ASCII text stream as UTF-8, keeping its error handler. Never raises.

    Only ASCII is touched: any other encoding — a Windows code page, a UTF-8 terminal, a pytest capture
    — can already say what the CLI prints, or was chosen by somebody. And nothing at all when
    `PYTHONIOENCODING` is set, which outranks UTF-8 mode in CPython too.
    """
    if os.environ.get("PYTHONIOENCODING"):
        return
    for stream in (sys.stdin, sys.stdout, sys.stderr) if streams is None else streams:
        try:
            if codecs.lookup(stream.encoding).name == "ascii":
                # `errors` passed back explicitly: given an encoding alone, `reconfigure` resets the
                # handler to "strict", and stderr's "backslashreplace" is what keeps it from ever raising.
                stream.reconfigure(encoding="utf-8", errors=stream.errors)
        except (AttributeError, TypeError, LookupError, ValueError, OSError):
            continue  # no encoding, no reconfigure, closed, or already read from: leave it as it is
