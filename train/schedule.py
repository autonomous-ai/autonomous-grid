"""Putting the nightly cycle into the computer's own scheduler.

The autopilot only improves a model if something wakes it up at eleven. Until now the product's
answer was a crontab line to paste, which is a fine answer for an engineer and no answer at all
for the person this interface is for: the model that improves overnight would simply never run.

So this module installs a real job, per platform, in the least invasive place available:

  * **macOS** — a per-user LaunchAgent (`~/Library/LaunchAgents/…plist`). One file, owned by the
    user, removable by deleting it. No sudo, no shared crontab to clobber.
  * **Linux** — a systemd *user* timer, for the same reasons.
  * **anything else** — no install. `plan()` still returns the cron line to paste, and the caller
    must say so rather than pretending a schedule exists.

Three rules this module keeps, because a scheduler is state that outlives the tab that made it:

1. **Never claim more than it did.** Every function returns what actually happened, including
   the path it wrote and the loader's own complaint on failure.
2. **Removable by the same hand.** `remove()` undoes exactly what `install()` did.
3. **Absolute paths only.** A scheduled job runs with almost no environment: no PATH, no venv,
   no shell profile. `sys.executable` and an absolute working directory are the whole trick.
"""
from __future__ import annotations

import dataclasses
import getpass
import os
import platform
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

LABEL_PREFIX = "ai.autonomous.grid.train"


@dataclasses.dataclass(frozen=True)
class Plan:
    """What we would install, in enough detail to show someone before we do it."""

    supported: bool
    mechanism: str            # "launchd" | "systemd" | "none"
    path: Path | None         # the file we would write
    command: list[str]        # argv the scheduler will run
    when: str                 # "every night at 23:00"
    cron_line: str            # the fallback, always populated


@dataclasses.dataclass(frozen=True)
class Result:
    ok: bool
    detail: str               # a sentence for a person, including the loader's complaint on fail
    path: Path | None = None


def label_for(slug: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in slug).strip("-") or "model"
    return f"{LABEL_PREFIX}.{safe}"


def _command(workspace: Path, config: Path | None) -> list[str]:
    """The argv a scheduled run needs. `sys.executable` because there is no PATH out there."""
    argv = [sys.executable, "-m", "cli", "train", "autopilot"]
    if config:
        argv += ["--config", str(config)]
    return argv


def plan(workspace: Path, *, slug: str = "model", hour: int = 23, minute: int = 0,
         config: Path | None = None, system: str | None = None) -> Plan:
    workspace = Path(workspace).expanduser().resolve()
    system = system or platform.system()
    command = _command(workspace, config)
    when = f"every night at {hour:02d}:{minute:02d}"
    cron = f"{minute} {hour} * * *  cd {workspace} && grid train autopilot"
    if system == "Darwin":
        return Plan(True, "launchd", _plist_path(slug), command, when, cron)
    if system == "Linux" and shutil.which("systemctl"):
        return Plan(True, "systemd", _unit_path(slug, "timer"), command, when, cron)
    return Plan(False, "none", None, command, when, cron)


# --- macOS -----------------------------------------------------------------------------------

def _agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _plist_path(slug: str) -> Path:
    return _agents_dir() / f"{label_for(slug)}.plist"


def _launchctl(*args: str) -> tuple[int, str]:
    try:
        done = subprocess.run(["launchctl", *args], capture_output=True, text=True,
                              timeout=20, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return done.returncode, (done.stderr or done.stdout).strip()


def _install_launchd(workspace: Path, plan_: Plan, slug: str, hour: int, minute: int) -> Result:
    path = plan_.path
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    log = workspace / "autopilot.log"
    body = {
        "Label": label_for(slug),
        "ProgramArguments": plan_.command,
        "WorkingDirectory": str(workspace),
        "StartCalendarInterval": {"Hour": int(hour), "Minute": int(minute)},
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        # Not RunAtLoad: installing the schedule should not start a training run this second.
        "RunAtLoad": False,
        "ProcessType": "Background",
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", "GRID_HOME": os.environ.get(
            "GRID_HOME", str(Path.home() / ".grid"))},
    }
    try:
        path.write_bytes(plistlib.dumps(body))
    except OSError as exc:
        return Result(False, f"could not write {path}: {exc}")

    target = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{target}/{label_for(slug)}")     # replace any older copy; may 'fail'
    code, message = _launchctl("bootstrap", target, str(path))
    if code != 0:
        # Older macOS, or a sandboxed shell. `load -w` is the pre-bootstrap spelling.
        code, message = _launchctl("load", "-w", str(path))
    if code != 0:
        return Result(False,
                      f"wrote {path} but the scheduler would not take it: {message or code}. "
                      "Running `launchctl load -w " + str(path) + "` in Terminal usually says why.",
                      path)
    return Result(True, f"Installed. macOS will run it {plan_.when}, and log to "
                        f"{workspace / 'autopilot.log'}.", path)


# --- Linux -----------------------------------------------------------------------------------

def _unit_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def _unit_path(slug: str, kind: str) -> Path:
    return _unit_dir() / f"{label_for(slug)}.{kind}"


def _systemctl(*args: str) -> tuple[int, str]:
    try:
        done = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True,
                              timeout=20, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return done.returncode, (done.stderr or done.stdout).strip()


def _install_systemd(workspace: Path, plan_: Plan, slug: str, hour: int, minute: int) -> Result:
    unit_dir = _unit_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    name = label_for(slug)
    service = f"""[Unit]
Description=Grid: improve a model from captured work, unattended

[Service]
Type=oneshot
WorkingDirectory={workspace}
Environment=PYTHONUNBUFFERED=1
ExecStart={' '.join(plan_.command)}
StandardOutput=append:{workspace / 'autopilot.log'}
StandardError=append:{workspace / 'autopilot.log'}
"""
    timer = f"""[Unit]
Description=Grid: nightly training cycle

[Timer]
OnCalendar=*-*-* {hour:02d}:{minute:02d}:00
Persistent=true

[Install]
WantedBy=timers.target
"""
    try:
        _unit_path(slug, "service").write_text(service, encoding="utf-8")
        _unit_path(slug, "timer").write_text(timer, encoding="utf-8")
    except OSError as exc:
        return Result(False, f"could not write the unit files: {exc}")
    _systemctl("daemon-reload")
    code, message = _systemctl("enable", "--now", f"{name}.timer")
    if code != 0:
        return Result(False,
                      f"wrote the unit files but systemd would not enable the timer: "
                      f"{message or code}", _unit_path(slug, "timer"))
    return Result(True, f"Installed. systemd will run it {plan_.when}, and log to "
                        f"{workspace / 'autopilot.log'}.", _unit_path(slug, "timer"))


# --- the three verbs ---------------------------------------------------------------------------

def install(workspace: Path, *, slug: str = "model", hour: int = 23, minute: int = 0,
            config: Path | None = None) -> Result:
    """Put the nightly cycle in the user's own scheduler. Never raises."""
    workspace = Path(workspace).expanduser().resolve()
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return Result(False, f"{hour:02d}:{minute:02d} is not a time of day")
    plan_ = plan(workspace, slug=slug, hour=hour, minute=minute, config=config)
    if not plan_.supported:
        return Result(False,
                      "This computer has no per-user scheduler we can write to. Paste this into "
                      f"your scheduler instead:\n{plan_.cron_line}")
    if plan_.mechanism == "launchd":
        return _install_launchd(workspace, plan_, slug, hour, minute)
    return _install_systemd(workspace, plan_, slug, hour, minute)


def status(*, slug: str = "model", system: str | None = None) -> dict:
    """Is a schedule installed for this model, and when does it run?

    Reads the file we wrote rather than asking the loader: `launchctl list` needs a session the
    web server may not have, and the file is the thing that survives a reboot anyway.
    """
    system = system or platform.system()
    if system == "Darwin":
        path = _plist_path(slug)
        if not path.is_file():
            return {"installed": False, "mechanism": "launchd", "where": str(path), "when": ""}
        try:
            body = plistlib.loads(path.read_bytes())
        except (OSError, ValueError):
            return {"installed": False, "mechanism": "launchd", "where": str(path), "when": ""}
        at = body.get("StartCalendarInterval") or {}
        when = f"{int(at.get('Hour', 0)):02d}:{int(at.get('Minute', 0)):02d}"
        return {"installed": True, "mechanism": "launchd", "where": str(path),
                "when": when, "log": body.get("StandardOutPath", "")}
    if system == "Linux":
        path = _unit_path(slug, "timer")
        if not path.is_file():
            return {"installed": False, "mechanism": "systemd", "where": str(path), "when": ""}
        when = ""
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("OnCalendar="):
                when = line.split()[-1][:5]
        return {"installed": True, "mechanism": "systemd", "where": str(path), "when": when}
    return {"installed": False, "mechanism": "none", "where": "", "when": ""}


def remove(*, slug: str = "model", system: str | None = None) -> Result:
    """Undo exactly what install() did."""
    system = system or platform.system()
    if system == "Darwin":
        path = _plist_path(slug)
        if not path.is_file():
            return Result(True, "There was no schedule to remove.")
        _launchctl("bootout", f"gui/{os.getuid()}/{label_for(slug)}")
        _launchctl("unload", "-w", str(path))
        try:
            path.unlink()
        except OSError as exc:
            return Result(False, f"could not remove {path}: {exc}", path)
        return Result(True, "Removed. Nothing will run overnight until you turn it back on.", path)
    if system == "Linux":
        name = label_for(slug)
        _systemctl("disable", "--now", f"{name}.timer")
        removed = False
        for kind in ("timer", "service"):
            path = _unit_path(slug, kind)
            if path.is_file():
                try:
                    path.unlink()
                    removed = True
                except OSError as exc:
                    return Result(False, f"could not remove {path}: {exc}", path)
        _systemctl("daemon-reload")
        return Result(True, "Removed." if removed else "There was no schedule to remove.")
    return Result(True, "There was no schedule to remove.")


def describe(workspace: Path, *, slug: str = "model", hour: int = 23, minute: int = 0,
             config: Path | None = None) -> str:
    """One paragraph for a person deciding whether to let us do this."""
    plan_ = plan(workspace, slug=slug, hour=hour, minute=minute, config=config)
    if not plan_.supported:
        return ("We cannot add a scheduled job on this computer. Paste this into your scheduler:\n"
                + plan_.cron_line)
    what = {"launchd": "a login item for you only (a LaunchAgent)",
            "systemd": "a timer for your user account"}[plan_.mechanism]
    return (f"This adds {what} at {plan_.path}, which runs {plan_.when} as "
            f"{getpass.getuser()}. It does not need an administrator, it starts nothing now, and "
            "turning it off deletes that file.")
