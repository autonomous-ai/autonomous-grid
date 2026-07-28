"""The nightly schedule: a real job in the user's own scheduler, and an honest report of it.

Everything here runs against a fake HOME with the loader (`launchctl` / `systemctl`) stubbed, so
no test touches the machine's real LaunchAgents. The one thing a stub cannot prove — that launchd
actually accepts the plist and runs the job — was verified by hand on macOS 15: installed, fired
with `launchctl kickstart`, and the job logged `SKIPPED: someone is using this machine`, which is
the correct decision and proof the argv, working directory and environment survive the trip.
"""
from __future__ import annotations

import plistlib

import pytest

from train import schedule


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(schedule, "_launchctl", lambda *a: (0, ""))
    monkeypatch.setattr(schedule, "_systemctl", lambda *a: (0, ""))
    return tmp_path


def test_the_plist_runs_the_right_thing_from_the_right_place(home, tmp_path, monkeypatch):
    monkeypatch.setattr(schedule.platform, "system", lambda: "Darwin")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    result = schedule.install(workspace, slug="support-replies", hour=23, minute=30)
    assert result.ok, result.detail
    body = plistlib.loads(result.path.read_bytes())

    # A scheduled job has no PATH and no venv: an absolute interpreter is the whole trick.
    assert body["ProgramArguments"][0].startswith("/")
    assert body["ProgramArguments"][1:4] == ["-m", "cli", "train"]
    assert body["ProgramArguments"][4] == "autopilot"
    assert body["WorkingDirectory"] == str(workspace.resolve())
    assert body["StartCalendarInterval"] == {"Hour": 23, "Minute": 30}
    # Installing a schedule must not start a training run this second.
    assert body["RunAtLoad"] is False
    assert body["StandardOutPath"].endswith("autopilot.log")


def test_status_reads_back_what_install_wrote(home, tmp_path, monkeypatch):
    monkeypatch.setattr(schedule.platform, "system", lambda: "Darwin")
    assert schedule.status(slug="s", system="Darwin")["installed"] is False
    schedule.install(tmp_path, slug="s", hour=2, minute=5)
    state = schedule.status(slug="s", system="Darwin")
    assert state["installed"] and state["when"] == "02:05"
    assert state["mechanism"] == "launchd"


def test_removing_takes_the_file_away(home, tmp_path, monkeypatch):
    monkeypatch.setattr(schedule.platform, "system", lambda: "Darwin")
    schedule.install(tmp_path, slug="s")
    path = schedule._plist_path("s")
    assert path.is_file()
    assert schedule.remove(slug="s", system="Darwin").ok
    assert not path.is_file()
    # Removing twice is not an error — the button can be pressed again.
    assert schedule.remove(slug="s", system="Darwin").ok


def test_a_loader_that_refuses_is_reported_not_swallowed(home, tmp_path, monkeypatch):
    """The worst outcome is a page that says "scheduled" when nothing is scheduled."""
    monkeypatch.setattr(schedule.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(schedule, "_launchctl", lambda *a: (1, "Load failed: 5: Input/output error"))
    result = schedule.install(tmp_path, slug="s")
    assert not result.ok
    assert "Input/output error" in result.detail       # the loader's own words
    # And it points at a file that EXISTS: the rejected plist is moved aside, not deleted, because
    # telling someone to inspect a path we just unlinked is worse than saying nothing.
    assert ".plist.rejected" in result.detail
    assert schedule._plist_path("s").with_suffix(".plist.rejected").is_file()
    assert not schedule._plist_path("s").exists()      # and status() sees nothing installed


def test_an_unsupported_computer_says_so_and_hands_over_the_line(home, tmp_path, monkeypatch):
    monkeypatch.setattr(schedule.platform, "system", lambda: "Windows")
    result = schedule.install(tmp_path, slug="s")
    assert not result.ok
    assert "grid train autopilot" in result.detail    # the fallback, not a shrug
    assert not schedule.plan(tmp_path, system="Windows").supported


def test_the_systemd_units_are_a_oneshot_and_a_calendar_timer(home, tmp_path, monkeypatch):
    monkeypatch.setattr(schedule.platform, "system", lambda: "Linux")
    monkeypatch.setattr(schedule.shutil, "which", lambda name: "/usr/bin/systemctl")
    result = schedule.install(tmp_path, slug="s", hour=1, minute=0)
    assert result.ok, result.detail
    timer = schedule._unit_path("s", "timer").read_text(encoding="utf-8")
    service = schedule._unit_path("s", "service").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 01:00:00" in timer
    assert "Persistent=true" in timer                 # a laptop asleep at 01:00 still catches up
    assert "Type=oneshot" in service
    assert str(tmp_path.resolve()) in service
    assert schedule.status(slug="s", system="Linux")["when"] == "01:00"
    assert schedule.remove(slug="s", system="Linux").ok
    assert not schedule._unit_path("s", "timer").is_file()


def test_a_nonsense_time_is_refused_before_anything_is_written(home, tmp_path, monkeypatch):
    monkeypatch.setattr(schedule.platform, "system", lambda: "Darwin")
    result = schedule.install(tmp_path, slug="s", hour=25)
    assert not result.ok and "not a time of day" in result.detail
    assert not schedule._plist_path("s").exists()


def test_describe_tells_a_person_exactly_what_will_happen(home, tmp_path, monkeypatch):
    monkeypatch.setattr(schedule.platform, "system", lambda: "Darwin")
    text = schedule.describe(tmp_path, slug="s", hour=23, minute=0)
    assert "does not need an administrator" in text
    assert "starts nothing now" in text
    assert "deletes that file" in text                # reversible, and said so


def test_labels_cannot_escape_into_a_filename(home, tmp_path, monkeypatch):
    """The slug comes from a model name someone typed."""
    assert schedule.label_for("../../etc/passwd") == "ai.autonomous.grid.train.etc-passwd"
    assert schedule.label_for("") == "ai.autonomous.grid.train.model"
    assert "/" not in schedule.label_for("a/b c")


def test_a_rejected_install_leaves_nothing_behind(home, tmp_path, monkeypatch):
    """status() reads the file. A rejected plist left on disk = a page saying "on" for a job
    that will never run, which is the one thing this module must never do."""
    monkeypatch.setattr(schedule.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(schedule, "_launchctl", lambda *a: (1, "Load failed: 5: I/O error"))
    result = schedule.install(tmp_path, slug="s")
    assert not result.ok
    assert not schedule._plist_path("s").exists()
    assert schedule.status(slug="s", system="Darwin")["installed"] is False


def test_a_rejected_systemd_timer_leaves_nothing_behind(home, tmp_path, monkeypatch):
    monkeypatch.setattr(schedule.platform, "system", lambda: "Linux")
    monkeypatch.setattr(schedule.shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(schedule, "_systemctl", lambda *a: (1, "Failed to enable unit"))
    result = schedule.install(tmp_path, slug="s")
    assert not result.ok
    assert not schedule._unit_path("s", "timer").exists()
    assert not schedule._unit_path("s", "service").exists()


def test_a_command_that_cannot_start_is_never_scheduled(home, tmp_path, monkeypatch):
    """The Linux binary case: sys.executable is a path that does not exist between runs, so the
    job would be installed, reported as on, and fail every night in silence."""
    monkeypatch.setattr(schedule.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(schedule, "_can_run", lambda cmd: (False, "no such file or directory"))
    result = schedule.install(tmp_path, slug="s")
    assert not result.ok
    assert "scheduling it would do nothing" in result.detail
    assert not schedule._plist_path("s").exists()


def test_the_scheduled_command_is_one_this_computer_can_actually_run(home, tmp_path):
    """Not a mock: build the real argv and run it."""
    command = schedule._command(tmp_path, None)
    assert command[-2:] == ["train", "autopilot"]
    ok, complaint = schedule._can_run(command)
    assert ok, complaint


def test_a_path_with_a_space_survives_systemd(home, tmp_path, monkeypatch):
    """systemd splits ExecStart on whitespace; "/home/dee/My Models" would arrive as two args."""
    monkeypatch.setattr(schedule.platform, "system", lambda: "Linux")
    monkeypatch.setattr(schedule.shutil, "which", lambda name: "/usr/bin/systemctl")
    spaced = tmp_path / "My Models"
    spaced.mkdir()
    config = spaced / "grid-train.toml"
    config.write_text("", encoding="utf-8")
    result = schedule.install(spaced, slug="s", config=config)
    assert result.ok, result.detail
    service = schedule._unit_path("s", "service").read_text(encoding="utf-8")
    exec_line = next(line for line in service.splitlines() if line.startswith("ExecStart="))
    assert "'" in exec_line or '"' in exec_line          # the path is quoted, not split
    assert "GRID_HOME=" in service                        # same store the person sees, not root's


def test_one_folder_cannot_take_over_another_folders_schedule(home, tmp_path, monkeypatch):
    """The guard has to live in install(), not in a caller.

    It was in the CLI only, and the browser's toggle called straight past it: one button in one
    folder silently took over another folder's nightly job and told nobody.
    """
    monkeypatch.setattr(schedule.platform, "system", lambda: "Darwin")
    first, second = tmp_path / "team-a", tmp_path / "team-b"
    first.mkdir()
    second.mkdir()

    assert schedule.install(first, slug="support-replies").ok
    stolen = schedule.install(second, slug="support-replies")
    assert not stolen.ok
    assert str(first) in stolen.detail                 # names the folder that owns it
    body = schedule.status(slug="support-replies", workspace=first)
    assert body["mine"] and body["workspace"] == str(first)

    # Deliberate replacement is still possible, but it has to be asked for.
    assert schedule.install(second, slug="support-replies", force=True).ok


def test_turning_it_off_from_the_wrong_folder_refuses(home, tmp_path, monkeypatch):
    """Following the CLI's own advice used to delete someone else's job and report success."""
    monkeypatch.setattr(schedule.platform, "system", lambda: "Darwin")
    first, second = tmp_path / "team-a", tmp_path / "team-b"
    first.mkdir()
    second.mkdir()
    schedule.install(first, slug="support-replies")

    refused = schedule.remove(slug="support-replies", system="Darwin", workspace=second)
    assert not refused.ok
    assert str(first) in refused.detail
    assert schedule.status(slug="support-replies", system="Darwin")["installed"]   # still there

    assert schedule.remove(slug="support-replies", system="Darwin", workspace=first).ok


def test_an_older_linux_unit_is_still_attributable(home, tmp_path, monkeypatch):
    """Units written before X-GridWorkspace existed carry WorkingDirectory, which says the same
    thing. Reading only the new key made the guard inert for everyone who upgraded."""
    monkeypatch.setattr(schedule.platform, "system", lambda: "Linux")
    monkeypatch.setattr(schedule.shutil, "which", lambda name: "/usr/bin/systemctl")
    owner = tmp_path / "team-a"
    owner.mkdir()
    schedule._unit_dir().mkdir(parents=True, exist_ok=True)
    schedule._unit_path("s", "timer").write_text(
        "[Timer]\nOnCalendar=*-*-* 23:00:00\n", encoding="utf-8")
    schedule._unit_path("s", "service").write_text(          # no X-GridWorkspace, as before
        f"[Service]\nType=oneshot\nWorkingDirectory={owner}\nExecStart=/bin/true\n",
        encoding="utf-8")

    state = schedule.status(slug="s", system="Linux", workspace=tmp_path / "team-b")
    assert state["workspace"] == str(owner)
    assert state["mine"] is False


def test_the_cron_fallback_survives_a_path_with_a_space(home, tmp_path, monkeypatch):
    """The only mechanism offered where there is no launchd or systemd — and `cd /home/My Models`
    is `cd: too many arguments`, every night, silently."""
    monkeypatch.setattr(schedule.platform, "system", lambda: "Windows")
    spaced = tmp_path / "My Models"
    spaced.mkdir()
    line = schedule.plan(spaced, system="Windows").cron_line
    assert "'" in line or '"' in line
    assert schedule.describe(spaced, slug="s").count("My Models") >= 1
