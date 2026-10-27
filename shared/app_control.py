"""Windows refusing to start a program: recognise it, and say so in words a person can act on.

Smart App Control (Windows 11) and App Control for Business block an unsigned program Windows does not
trust before it runs: `CreateProcess` fails with ERROR 4551 ("An Application Control policy has blocked
this file"), and the older Software Restriction Policies with 1260. Raised as a bare OSError it reads as a
crash in grid; it is Windows' own policy, about one specific file.
"""
from __future__ import annotations

_BLOCKED = (
    4551,  # ERROR_SYSTEM_INTEGRITY_POLICY_VIOLATION — Smart App Control / App Control for Business
    1260,  # ERROR_ACCESS_DISABLED_BY_POLICY — Software Restriction Policies / AppLocker
)


def blocked(error: OSError) -> bool:
    return getattr(error, "winerror", None) in _BLOCKED


def explain(path: str) -> str:
    return (
        f"Windows blocked {path} from starting: Smart App Control or an App Control policy does not "
        "trust it. grid cannot override that. Ask your administrator to allow this file, or turn "
        "Smart App Control off in Windows Security (it may not turn back on without reinstalling "
        "Windows). See https://aka.ms/smartappcontrol"
    )
