"""Absolute paths to the Windows system tools the CLI shells out to.

Why this exists at all: ``CreateProcess`` resolves a bare executable name through a search order that
includes the **current directory**, and the CLI runs wherever the operator happens to be — so a
``taskkill.exe`` or ``powershell.exe`` planted in a working directory is executed as us, with
whatever privilege we hold. Every Windows tool invocation therefore names an absolute path.

It lives in ``shared/`` because both sides of a teardown need it — ``shared.run_records.kill_group``
and ``shared.orphan_sweep``'s process enumerator — and neither may depend on the other.

Everything here is import-safe on POSIX: the ``ctypes`` call is inside the function, so a module that
imports this on Linux pays nothing and crashes nowhere.
"""
from __future__ import annotations

_FALLBACK_WINDOWS_DIRECTORY = r"C:\Windows"


def system_directory() -> str:
    """The real Windows directory, asked of the kernel rather than read from the environment.

    ``%SystemRoot%`` is attacker-writable and survives UAC elevation, so anything derived from it can
    select the binary we execute. Validating the string cannot save it: matching
    ``[A-Za-z]:\\(Windows|WINNT)`` case-insensitively still admits ``W:\\Windows`` (``subst`` and
    ``net use`` map a UNC share to a drive letter unprivileged), ``C:\\Window\u017f`` (``re.IGNORECASE``
    on a ``str`` pattern Unicode-case-folds U+017F to ``s``, and a standard user can create folders at
    the root of ``C:``), and ``C:\\Windows\\n`` (``$`` matches before a trailing newline; the control
    character then makes the path unopenable, which silently switches the caller off). All three were
    measured. ``GetSystemWindowsDirectoryW`` reads none of that — it is the system directory the
    kernel knows, which is also the documented way to locate system files on a Terminal Server.

    A separate function so its callers stay unit-testable off Windows; the real call is exercised by
    the ``test-windows`` CI job.
    """
    import ctypes

    buffer = ctypes.create_unicode_buffer(260)
    written = ctypes.windll.kernel32.GetSystemWindowsDirectoryW(buffer, len(buffer))
    return buffer.value if written else _FALLBACK_WINDOWS_DIRECTORY


def system32_tool(relative: str) -> str:
    """An absolute path to ``System32\\<relative>`` under the kernel's Windows directory.

    Joined with a literal ``\\`` rather than ``os.path.join`` so the string is identical on every
    platform — which is what lets the Windows branches of both callers be unit-tested on the Linux CI.
    """
    return "\\".join((system_directory().rstrip("\\"), "System32", relative))
