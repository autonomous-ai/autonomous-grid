"""Is the human using this machine? — the host-priority rule, as two cheap signals.

Grid's promise about pooling *personal* computers is that the owner outranks the scheduler. That
has been prose in the design docs; training is what makes it load-bearing, because a training run
is sustained 100% duty rather than a burst, and nobody wants their laptop cooking rollouts during
a meeting.

Both signals return `None` when the platform can't answer, and callers treat unknown as "free" —
a machine that cannot report must not veto the whole feature. macOS is first-class (pmset/ioreg);
Linux is supported via sysfs; Windows returns None for now and is documented as such.
"""
from __future__ import annotations

import platform
import re
import shutil
import subprocess
from pathlib import Path


def _run(command: list[str], timeout: float = 2.0) -> str | None:
    if not shutil.which(command[0]):
        return None
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                                   check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def on_battery() -> bool | None:
    """True on battery, False on mains, None if unknown (desktops answer False)."""
    system = platform.system()
    if system == "Darwin":
        out = _run(["pmset", "-g", "batt"])
        if out is None:
            return None
        # "Now drawing from 'AC Power'" / "'Battery Power'"
        if "'AC Power'" in out:
            return False
        if "'Battery Power'" in out:
            return True
        return None
    if system == "Linux":
        for supply in sorted(Path("/sys/class/power_supply").glob("A*")):
            online = supply / "online"
            if online.is_file():
                try:
                    return online.read_text().strip() == "0"
                except OSError:
                    return None
        # No AC adapter reported at all: almost certainly a desktop or a VM.
        return False if Path("/sys/class/power_supply").is_dir() else None
    return None


def user_idle_seconds() -> float | None:
    """Seconds since the last keyboard/mouse event, or None if the platform won't say."""
    if platform.system() == "Darwin":
        out = _run(["ioreg", "-c", "IOHIDSystem", "-d", "4", "-r"])
        if not out:
            return None
        # HIDIdleTime is in nanoseconds; several entries may appear — the smallest is the truth.
        values = [int(m) for m in re.findall(r'"HIDIdleTime"\s*=\s*(\d+)', out)]
        return min(values) / 1_000_000_000 if values else None
    if platform.system() == "Linux":
        out = _run(["xprintidle"])  # optional package; absent is fine
        if out and out.strip().isdigit():
            return int(out.strip()) / 1000
        return None
    return None


def summary() -> dict:
    """For `grid train doctor` and the web interface's machine page."""
    battery = on_battery()
    idle = user_idle_seconds()
    return {
        "on_battery": battery,
        "idle_seconds": None if idle is None else int(idle),
        "power": {True: "on battery", False: "on mains", None: "unknown"}[battery],
        "activity": ("unknown" if idle is None
                     else f"idle {int(idle)}s" if idle >= 300
                     else f"in use ({int(idle)}s ago)"),
    }
