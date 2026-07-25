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
    assert "Input/output error" in result.detail
    assert "launchctl load" in result.detail          # and how to find out why


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
