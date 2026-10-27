"""The CLI prints in an ASCII locale — the Linux binary's case (found after the v0.3.49 release).

CPython started in the C/POSIX locale switches itself to UTF-8 (PEP 538 coerces the locale, PEP 540
turns on UTF-8 mode), so the wheel never noticed. The Nuitka-built Linux binary does not: measured in
`ubuntu:24.04` with `LANG` unset, its stdout was ASCII and `grid models --help`, `grid stats --help`,
`grid catalog` and `grid device-info` all died with `UnicodeEncodeError` on a "…" or a "—" — while the
same wheel under CPython printed every one. v0.3.48 did the same, so it was never this release's doing.

These tests put an ASCII stream where the binary had one and drive the real entry point.
"""
from __future__ import annotations

import io
import sys

import pytest

import cli
from shared import stdio


def _ascii_stream(errors: str = "strict") -> io.TextIOWrapper:
    """What the binary's stdout is in the C locale: an ASCII text stream over bytes."""
    return io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors=errors)


def _written(stream: io.TextIOWrapper) -> str:
    stream.flush()
    return stream.buffer.getvalue().decode("utf-8")


@pytest.fixture()
def ascii_stdio(monkeypatch, tmp_path):
    """Returns a function that puts ASCII streams in place of stdout and stderr.

    ⚠️ **Called from the test BODY, never here.** pytest's own capture puts its stream back into
    `sys.stdout` when the call phase begins, so a stream swapped in during setup is silently replaced:
    the first draft of these tests passed their `SystemExit` check with nothing written anywhere and
    never reached the bug.
    """
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)

    def swap() -> tuple[io.TextIOWrapper, io.TextIOWrapper]:
        out, err = _ascii_stream(), _ascii_stream("backslashreplace")
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", err)
        return out, err

    return swap


def test_a_help_screen_prints_in_an_ascii_locale(ascii_stdio):
    out, _err = ascii_stdio()

    with pytest.raises(SystemExit) as caught:
        cli.main(["models", "--help"])

    assert caught.value.code == 0
    assert "Grid name or id (ag-…)" in _written(out)


def test_command_output_prints_in_an_ascii_locale(ascii_stdio):
    """Not only help: ordinary output with a "—" in it died the same way."""
    out, _err = ascii_stdio()

    assert cli.main(["catalog"]) == 0
    assert _written(out).strip()


def test_stdio_is_switched_to_utf8_when_its_encoding_is_ascii(monkeypatch):
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    streams = [_ascii_stream(), _ascii_stream("backslashreplace")]

    stdio.use_utf8_in_an_ascii_locale(streams)

    assert [s.encoding for s in streams] == ["utf-8", "utf-8"]
    assert [s.errors for s in streams] == ["strict", "backslashreplace"], "the error handler is kept"


@pytest.mark.parametrize("encoding", ["utf-8", "cp1252", "latin-1", "utf-16"])
def test_any_other_encoding_is_left_alone(monkeypatch, encoding):
    """Only ASCII is switched off Windows. (On Windows a pipe on a legacy code page is the next bug —
    see the tests below.)"""
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    monkeypatch.setattr(stdio, "_IS_WINDOWS", False)
    stream = io.TextIOWrapper(io.BytesIO(), encoding=encoding)

    stdio.use_utf8_in_an_ascii_locale([stream])

    assert stream.encoding == encoding


class _Console(io.BytesIO):
    def isatty(self) -> bool:
        return True


@pytest.mark.parametrize("encoding", ["cp1252", "cp437", "cp850"])
def test_windows_pipe_on_a_legacy_code_page_switches_to_utf8(monkeypatch, encoding):
    """`grid ls | findstr x`: a pipe gets the ANSI code page, which cannot hold "✓" (cp437 not even
    "—"), and `print` raised UnicodeEncodeError. A real console writes UTF-16 and is never touched."""
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    monkeypatch.setattr(stdio, "_IS_WINDOWS", True)
    streams = [io.TextIOWrapper(io.BytesIO(), encoding=encoding),
               io.TextIOWrapper(io.BytesIO(), encoding=encoding, errors="backslashreplace")]

    stdio.use_utf8_in_an_ascii_locale(streams)

    assert [s.encoding for s in streams] == ["utf-8", "utf-8"]
    assert [s.errors for s in streams] == ["strict", "backslashreplace"], "the error handler is kept"
    print("✓ — …", file=streams[0])


def test_windows_piped_input_keeps_its_code_page(monkeypatch):
    """`type notes.txt | grid …`: the bytes really are in the producer's code page, so input is not
    re-decoded as UTF-8."""
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    monkeypatch.setattr(stdio, "_IS_WINDOWS", True)
    stream = io.TextIOWrapper(io.BufferedReader(io.BytesIO("café".encode("cp1252"))), encoding="cp1252")

    stdio.use_utf8_in_an_ascii_locale([stream])

    assert stream.encoding == "cp1252"
    assert stream.read() == "café"


def test_windows_console_is_left_alone(monkeypatch):
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    monkeypatch.setattr(stdio, "_IS_WINDOWS", True)
    stream = io.TextIOWrapper(_Console(), encoding="cp1252")

    stdio.use_utf8_in_an_ascii_locale([stream])

    assert stream.encoding == "cp1252"


def test_windows_utf8_pipe_is_left_alone(monkeypatch):
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    monkeypatch.setattr(stdio, "_IS_WINDOWS", True)
    stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="replace")

    stdio.use_utf8_in_an_ascii_locale([stream])

    assert (stream.encoding, stream.errors) == ("utf-8", "replace")


def test_windows_pipe_with_pythonioencoding_is_left_alone(monkeypatch):
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    monkeypatch.setattr(stdio, "_IS_WINDOWS", True)
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")

    stdio.use_utf8_in_an_ascii_locale([stream])

    assert stream.encoding == "cp1252"


def test_an_encoding_the_person_asked_for_is_left_alone(monkeypatch):
    """`PYTHONIOENCODING` wins over UTF-8 mode in CPython too — somebody who set it meant it."""
    monkeypatch.setenv("PYTHONIOENCODING", "ascii")
    stream = _ascii_stream()

    stdio.use_utf8_in_an_ascii_locale([stream])

    assert stream.encoding == "ascii"


@pytest.mark.parametrize("stream", [io.StringIO(), None, object()])
def test_a_stream_it_cannot_reconfigure_is_skipped_not_fatal(monkeypatch, stream):
    """A test harness's capture, a closed or absent stream: the CLI must still run."""
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)

    stdio.use_utf8_in_an_ascii_locale([stream])
