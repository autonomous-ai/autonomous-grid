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
3. **Absolute paths only, and prove they run.** A scheduled job has almost no environment: no
   PATH, no venv, no shell profile. The argv comes from `runtime.cli_command()` (the same helper
   every other detached subprocess here uses) and is smoke-tested before we call the install a
   success — a job that cannot start is indistinguishable, from the page, from one that never had
   anything to do.
"""
from __future__ import annotations

import dataclasses
import getpass
import os
import platform
import plistlib
import shlex
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


def _unlink(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass


def label_for(slug: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in slug).strip("-") or "model"
    return f"{LABEL_PREFIX}.{safe}"


def _command(workspace: Path, config: Path | None) -> list[str]:
    """The argv a scheduled run needs — absolute, because there is no PATH out there.

    Not `[sys.executable, "-m", "cli", …]`. On the Linux install (a Nuitka one-file binary, no
    Python at runtime) `sys.executable` is a path inside a temporary extraction directory that does
    not exist between runs, and the binary refuses to re-invoke itself with `-m` anyway. That job
    would have been installed, reported as on, and failed silently every night forever.
    `runtime.cli_command()` already solves this for every other detached subprocess in the codebase
    — it prefers argv[0] when that is a real executable — so use it here too.
    """
    argv = [*_cli_argv(), "train", "autopilot"]
    if config:
        argv += ["--config", str(config)]
    return argv


def _cli_argv() -> list[str]:
    """How to re-invoke this CLI from a scheduler.

    `runtime.cli_command()` prefers argv[0], which is right for an installed `grid` and wrong when
    the process was started by something else that happens to be executable — a test runner, an
    editor's debugger. So take argv[0] only when it IS our CLI, and otherwise use the interpreter,
    which works anywhere Python exists. The one place with no Python is the Linux binary, and there
    argv[0] is always the binary.
    """
    from local.runtime import cli_command

    argv = cli_command()
    if len(argv) == 1 and Path(argv[0]).name.removesuffix(".exe") != "grid":
        return [sys.executable, "-m", "cli"]
    return argv


def _can_run(command: list[str]) -> tuple[bool, str]:
    """Does this argv actually start? Cheaper to find out now than at 23:00 in six weeks.

    `--help` exits 0 having parsed the whole subcommand path, touches nothing, and needs no config
    — so it answers "can this argv start at all", which is the failure being guarded against.
    """
    try:
        done = subprocess.run([*command, "--help"], capture_output=True, text=True,
                              timeout=90, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if done.returncode == 0:
        return True, ""
    complaint = (done.stderr or done.stdout or "").strip().splitlines()
    return False, (complaint[-1] if complaint else f"exit code {done.returncode}")


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
        # Take the file away again. status() reads the file, so leaving a rejected plist behind
        # would make the page say "on" for a job that will never run — the exact lie this module
        # exists to avoid.
        _unlink(path)
        return Result(False,
                      f"the scheduler would not take it: {message or code}. "
                      "Running `launchctl load -w " + str(path) + "` in Terminal usually says why.")
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
    # Quoted, because systemd splits ExecStart on whitespace and a workspace under
    # "/home/dee/My Models" would otherwise arrive as two arguments. GRID_HOME is passed for the
    # same reason the plist passes it: the scheduled run must read the same store the person does.
    exec_start = " ".join(shlex.quote(part) for part in plan_.command)
    grid_home = os.environ.get("GRID_HOME", str(Path.home() / ".grid"))
    service = f"""[Unit]
Description=Grid: improve a model from captured work, unattended

[Service]
Type=oneshot
WorkingDirectory={workspace}
Environment=PYTHONUNBUFFERED=1
Environment=GRID_HOME={shlex.quote(grid_home)}
X-GridWorkspace={workspace}
ExecStart={exec_start}
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
        for kind in ("timer", "service"):      # same rule as launchd: no file, no claim
            _unlink(_unit_path(slug, kind))
        _systemctl("daemon-reload")
        return Result(False,
                      f"systemd would not enable the timer: {message or code}")
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
    runs, complaint = _can_run(plan_.command)
    if not runs:
        return Result(False,
                      "we could not start the nightly command on this computer, so scheduling it "
                      f"would do nothing: {complaint}")
    if plan_.mechanism == "launchd":
        return _install_launchd(workspace, plan_, slug, hour, minute)
    return _install_systemd(workspace, plan_, slug, hour, minute)


def status(*, slug: str = "model", system: str | None = None,
           workspace: Path | str | None = None) -> dict:
    """Is a schedule installed for this model, when does it run — and is it *this* model's?

    Reads the file we wrote rather than asking the loader: `launchctl list` needs a session the
    web server may not have, and the file is the thing that survives a reboot anyway.

    The label is derived from the model's name, so two workspaces called "Support replies" in
    different folders resolve to the same job. Pass `workspace` and the answer carries `mine`:
    False means another folder owns this schedule and turning it on here would replace it.
    """
    system = system or platform.system()
    if system == "Darwin":
        path = _plist_path(slug)
        if not path.is_file():
            return _with_owner({"installed": False, "mechanism": "launchd", "where": str(path), "when": ""}, workspace)
        try:
            body = plistlib.loads(path.read_bytes())
        except (OSError, ValueError):
            return _with_owner({"installed": False, "mechanism": "launchd", "where": str(path), "when": ""}, workspace)
        at = body.get("StartCalendarInterval") or {}
        when = f"{int(at.get('Hour', 0)):02d}:{int(at.get('Minute', 0)):02d}"
        return _with_owner({"installed": True, "mechanism": "launchd", "where": str(path),
                "when": when, "log": body.get("StandardOutPath", ""),
                "workspace": body.get("WorkingDirectory", "")}, workspace)
    if system == "Linux":
        path = _unit_path(slug, "timer")
        if not path.is_file():
            return _with_owner({"installed": False, "mechanism": "systemd", "where": str(path), "when": ""}, workspace)
        when = ""
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("OnCalendar="):
                when = line.split()[-1][:5]
        owner = ""
        service = _unit_path(slug, "service")
        if service.is_file():
            for line in service.read_text(encoding="utf-8").splitlines():
                if line.startswith("X-GridWorkspace="):
                    owner = line.split("=", 1)[1].strip()
        return _with_owner({"installed": True, "mechanism": "systemd", "where": str(path), "when": when,
                "workspace": owner}, workspace)
    return _with_owner({"installed": False, "mechanism": "none", "where": "", "when": ""}, workspace)


def _with_owner(state: dict, workspace: Path | str | None) -> dict:
    owner = state.get("workspace") or ""
    if workspace is None or not state.get("installed"):
        state["mine"] = True
        return state
    state["mine"] = (not owner) or Path(owner) == Path(workspace).expanduser().resolve()
    return state


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
