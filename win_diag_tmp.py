"""TEMPORARY Windows CI probe — surfaces the real PowerShell error behind `#< CLIXML`.

Deleted in the fix commit. Reuses the module's own constants verbatim so the probe cannot
drift from what `_win_process_output` actually runs.
"""
from __future__ import annotations

import base64
import subprocess
import sys

from shared import orphan_sweep


def _run(label: str, argv: list[str]) -> None:
    print(f"\n===== {label} =====", flush=True)
    print(f"argv[0]={argv[0]}", flush=True)
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=60,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:  # noqa: BLE001 - probe
        print(f"EXCEPTION: {type(exc).__name__}: {exc}", flush=True)
        return
    print(f"returncode={proc.returncode}", flush=True)
    print(f"--- stdout (first 5 lines, {len(proc.stdout)} chars) ---", flush=True)
    for line in proc.stdout.splitlines()[:5]:
        print(repr(line), flush=True)
    print("--- stderr (FULL) ---", flush=True)
    print(proc.stderr, flush=True)


def enc(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def main() -> None:
    ps = orphan_sweep._win_powershell_path()
    print(f"sys.platform={sys.platform}", flush=True)
    print(f"powershell path={ps}", flush=True)
    print("\n=== the script constant, verbatim ===", flush=True)
    print(orphan_sweep._WIN_PROCESS_LIST_SCRIPT, flush=True)

    base = [ps, "-NoProfile", "-NonInteractive", "-InputFormat", "None", "-OutputFormat", "Text"]

    # 1. Exactly what the shipping code runs.
    _run("A: real script, -EncodedCommand (what ships)",
         base + ["-EncodedCommand", enc(orphan_sweep._WIN_PROCESS_LIST_SCRIPT)])

    # 2. Same script WITHOUT the try/catch, so the terminating error text reaches stderr
    #    instead of being swallowed by `catch { exit 1 }`.
    body = orphan_sweep._WIN_PROCESS_LIST_SCRIPT
    body = body.replace("try { ", "", 1)
    body = body.replace("exit 0 } catch { exit 1 }", "exit 0", 1)
    _run("B: same script, try/catch REMOVED (shows the real error)", base + ["-EncodedCommand", enc(body)])

    # 3. Isolate the pieces.
    _run("C: Get-CimInstance alone",
         base + ["-EncodedCommand", enc(
             "$ErrorActionPreference='Stop'; "
             "(Get-CimInstance -ClassName Win32_Process -Property ProcessId,ParentProcessId,CommandLine "
             "| Measure-Object).Count")])

    _run("D: [Console]::Out.WriteLine alone",
         base + ["-EncodedCommand", enc("[Console]::Out.WriteLine('hello from console out'); exit 0")])

    _run("E: Get-CimInstance with NO -Property",
         base + ["-EncodedCommand", enc(
             "$ErrorActionPreference='Stop'; "
             "(Get-CimInstance -ClassName Win32_Process | Measure-Object).Count")])

    _run("F: $PSVersionTable / LanguageMode",
         base + ["-EncodedCommand", enc(
             "[Console]::Out.WriteLine($PSVersionTable.PSVersion.ToString()); "
             "[Console]::Out.WriteLine($ExecutionContext.SessionState.LanguageMode.ToString())")])


if __name__ == "__main__":
    main()
