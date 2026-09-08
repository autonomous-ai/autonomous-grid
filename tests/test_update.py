"""The version check, the stale notice, and `grid update` (cli/update.py).

The properties worth locking are the ones a future edit can quietly break: the notice must be
impossible to mistake for command output (stderr, suppressed under --json / non-TTY / internals /
dev builds / opt-out), the check must never add latency or an exit code to the user's command, and
the binary swap must refuse on a checksum mismatch with the old binary intact.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import time

import httpx
import pytest

import cli
from cli import update
from shared import paths


def _ns(**kw):
    base = {"command": "ls", "json": False}
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def grid_home(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    monkeypatch.delenv("GRID_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(update, "__version__", "0.3.34")
    monkeypatch.setattr(update, "_stderr_is_tty", lambda: True)
    return tmp_path


# ------------------------------------------------------------------ version compare


@pytest.mark.parametrize(
    ("candidate", "current", "expected"),
    [
        ("0.3.41", "0.3.34", True),
        ("0.3.34", "0.3.34", False),
        ("0.3.33", "0.3.34", False),
        ("v0.4.0", "0.3.34", True),          # tag form, as the redirect hands it over
        ("0.4", "0.3.9", True),              # unequal lengths pad with zeros
        ("0.3.34", "0.0.0+dev", True),       # local label carries no ordering
        ("nonsense", "0.3.34", False),       # unparseable never claims an upgrade
        ("", "0.3.34", False),
    ],
)
def test_is_newer(candidate, current, expected):
    assert update.is_newer(candidate, current) is expected


@pytest.mark.parametrize(
    ("tag", "expected"),
    [("v0.3.41", "0.3.41"), ("0.4.0", "0.4.0"), ("latest", None), ("", None),
     ("https://x/releases/tag/v1", None)],
    ids=["v-prefixed", "plain", "not-a-tag", "empty", "full-url"],
)
def test_parse_tag(tag, expected):
    assert update._parse_tag(tag) == expected


# ------------------------------------------------------------------ resolution


def test_fetch_latest_from_release_redirect():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/releases/latest"):
            return httpx.Response(302, headers={"location": "https://github.com/o/r/releases/tag/v9.9.9"})
        raise AssertionError(f"unexpected request: {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    assert update.fetch_latest_version(client) == "9.9.9"


def test_fetch_latest_falls_back_to_atom_feed():
    """The redirect source answers nothing useful (the failure mode install.sh:61 defends against)
    — the Atom feed's newest entry must carry the resolution."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/releases/latest"):
            return httpx.Response(200, text="no redirect today")
        return httpx.Response(
            200,
            text='<entry><link>https://github.com/o/r/releases/tag/v7.7.7</link></entry>',
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    assert update.fetch_latest_version(client) == "7.7.7"


def test_fetch_latest_silent_when_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    assert update.fetch_latest_version(client) is None


# ------------------------------------------------------------------ cache + check


def test_run_check_caches_and_survives_failure(grid_home, monkeypatch):
    monkeypatch.setattr(update, "fetch_latest_version", lambda *a, **k: "0.4.0")
    assert update.run_check() == 0
    cache = update._read_cache()
    assert cache["latest"] == "0.4.0" and cache["checked_at"] > 0

    # A failed resolution advances `checked_at` (no re-spawn storm against an offline network)
    # but drops `latest` — a notice must never outlive the release it announced.
    monkeypatch.setattr(update, "fetch_latest_version", lambda *a, **k: None)
    update.run_check()
    cache = update._read_cache()
    assert cache["latest"] is None and cache["checked_at"] >= 1


def test_run_check_preserves_notified_at(grid_home, monkeypatch):
    paths.update_check_file().write_text('{"checked_at": 1, "latest": "0.4.0", "notified_at": 5}')
    monkeypatch.setattr(update, "fetch_latest_version", lambda *a, **k: "0.4.0")
    update.run_check()
    assert update._read_cache()["notified_at"] == 5  # the background writer must not un-silence today


# ------------------------------------------------------------------ the notice


def test_notice_prints_once_per_interval(grid_home, capsys):
    update._write_cache({"checked_at": 1.0, "latest": "0.4.0"})
    update.print_notice(_ns())
    err = capsys.readouterr().err
    assert "A new version of grid is available: 0.3.34 → 0.4.0" in err
    assert "`grid update`" in err

    update.print_notice(_ns())  # second command the same day: silent, and the clock moved
    assert capsys.readouterr().err == ""
    assert update._read_cache()["notified_at"] > 0


def test_notice_never_appears_when_suppressed(grid_home, capsys, monkeypatch):
    update._write_cache({"checked_at": 1.0, "latest": "0.4.0"})

    update.print_notice(_ns(json=True), json_requested=True)
    update.print_notice(_ns(command="update"))
    monkeypatch.setenv("GRID_NO_UPDATE_CHECK", "1")
    update.print_notice(_ns())
    monkeypatch.delenv("GRID_NO_UPDATE_CHECK")
    monkeypatch.setattr(update, "_stderr_is_tty", lambda: False)
    update.print_notice(_ns())
    monkeypatch.setattr(update, "_stderr_is_tty", lambda: True)
    monkeypatch.setattr(update, "__version__", "0.0.0+dev")
    update.print_notice(_ns())

    assert capsys.readouterr().err == ""
    assert "notified_at" not in update._read_cache()  # suppressed = unseen, not seen-and-hidden


def test_notice_quiet_when_already_current_or_cache_broken(grid_home, capsys):
    update._write_cache({"checked_at": 1.0, "latest": "0.3.34"})
    update.print_notice(_ns())
    update._write_cache({"checked_at": 1.0, "latest": None})
    update.print_notice(_ns())
    paths.update_check_file().write_text("{corrupt")  # must read as "no answer", never kill `grid ls`
    update.print_notice(_ns())
    assert capsys.readouterr().err == ""


# ------------------------------------------------------------------ the spawn


def test_spawn_when_due_not_when_fresh(grid_home, monkeypatch):
    calls: list[tuple[list[str], dict]] = []
    monkeypatch.setattr(
        update.subprocess, "Popen",
        lambda argv, **kw: calls.append((argv, kw)) or object(),
    )
    update.maybe_spawn_check(_ns())  # no cache at all → due
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[-1] == "__update-check"
    assert argv[0] == sys.executable  # source/wheel run re-execs through the interpreter
    assert kwargs["start_new_session"] is True  # detached: the user's command exits first
    assert kwargs["stdout"] is update.subprocess.DEVNULL

    # The child's answer lands; the next hour of commands must not spawn another one.
    update._write_cache({"checked_at": time.time(), "latest": "0.4.0"})
    update.maybe_spawn_check(_ns())
    assert len(calls) == 1  # fresh cache → no spawn


def test_spawn_suppressions_touch_no_process(grid_home, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a suppressed run must not spawn anything")

    monkeypatch.setattr(update.subprocess, "Popen", boom)
    monkeypatch.setenv("GRID_NO_UPDATE_CHECK", "1")
    update.maybe_spawn_check(_ns())
    monkeypatch.delenv("GRID_NO_UPDATE_CHECK")
    update.maybe_spawn_check(_ns(json=True), json_requested=True)
    update.maybe_spawn_check(_ns(command="update"))
    monkeypatch.setattr(update, "_stderr_is_tty", lambda: False)
    update.maybe_spawn_check(_ns())


# ------------------------------------------------------------------ `grid update`


def test_update_noop_when_current(grid_home, monkeypatch, capsys):
    monkeypatch.setattr(update, "fetch_latest_version", lambda: "0.3.34")
    assert update.cmd_update(_ns(command="update")) == 0
    assert "0.3.34 is the latest version" in capsys.readouterr().out


def test_update_check_flag_reports_without_installing(grid_home, monkeypatch, capsys):
    monkeypatch.setattr(update, "fetch_latest_version", lambda: "0.4.0")
    monkeypatch.setattr(update, "_update_binary", lambda v: pytest.fail("--check installed"))
    monkeypatch.setattr(update, "_update_wheel", lambda v: pytest.fail("--check installed"))
    assert update.cmd_update(_ns(command="update", check=True)) == 0
    assert "0.3.34 → 0.4.0 is available" in capsys.readouterr().out


def test_update_refuses_source_checkout(grid_home, monkeypatch):
    monkeypatch.setattr(update, "__version__", "0.0.0+dev")
    with pytest.raises(SystemExit, match="source checkout"):
        update.cmd_update(_ns(command="update"))


def test_update_reports_unreachable_github(grid_home, monkeypatch):
    monkeypatch.setattr(update, "fetch_latest_version", lambda: None)
    with pytest.raises(SystemExit, match="could not resolve"):
        update.cmd_update(_ns(command="update"))


def test_update_routes_wheel_installs_to_uv(grid_home, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(update, "fetch_latest_version", lambda: "0.4.0")
    monkeypatch.setattr(update, "_is_binary_build", lambda: False)
    monkeypatch.setattr(update, "_update_wheel", seen.append)
    assert update.cmd_update(_ns(command="update")) == 0
    assert seen == ["0.4.0"]


def test_binary_swap_verifies_then_replaces(grid_home, monkeypatch):
    payload = b"NEW-GRID-BINARY"
    asset = f"grid-{update._os_tag()}-{update._arch_tag()}"
    digest = hashlib.sha256(payload).hexdigest()

    def fake_fetch(url: str, client=None):
        if url.endswith("/SHA256SUMS"):
            return f"{digest}  {asset}\n".encode()
        if url.endswith(f"/{asset}"):
            return payload
        raise AssertionError(url)

    binary = grid_home / "grid"
    binary.write_bytes(b"OLD")
    binary.chmod(0o755)
    monkeypatch.setattr(update, "_fetch_bytes", fake_fetch)
    monkeypatch.setattr(update, "_installed_binary_path", lambda: binary)

    update._update_binary("0.4.0")  # returns quietly; failure is a SystemExit, covered below
    assert binary.read_bytes() == payload
    assert binary.stat().st_mode & 0o777 == 0o755  # the swap keeps the file it replaced executable
    assert not list(grid_home.glob(".grid.new-*"))  # and leaves no orphan behind


def test_binary_swap_refuses_on_checksum_mismatch(grid_home, monkeypatch):
    asset = f"grid-{update._os_tag()}-{update._arch_tag()}"

    def fake_fetch(url: str, client=None):
        if url.endswith("/SHA256SUMS"):
            return f"{'0' * 64}  {asset}\n".encode()
        return b"TAMPERED"

    binary = grid_home / "grid"
    binary.write_bytes(b"OLD")
    monkeypatch.setattr(update, "_fetch_bytes", fake_fetch)
    monkeypatch.setattr(update, "_installed_binary_path", lambda: binary)

    with pytest.raises(SystemExit, match="checksum mismatch"):
        update._update_binary("0.4.0")
    assert binary.read_bytes() == b"OLD"  # refused = untouched, the whole point of the verification


def test_binary_swap_reports_a_download_404(grid_home, monkeypatch):
    monkeypatch.setattr(update, "_fetch_bytes", lambda url, client=None: None)
    monkeypatch.setattr(update, "_installed_binary_path", lambda: pytest.fail("must not touch the binary"))
    with pytest.raises(SystemExit, match="no grid-"):
        update._update_binary("0.4.0")


# ------------------------------------------------------------------ wiring


def test_main_spawns_check_and_prints_notice_around_the_command(monkeypatch, grid_home, capsys):
    events: list[str] = []
    monkeypatch.setattr(update, "maybe_spawn_check", lambda a, **k: events.append("spawn"))
    monkeypatch.setattr(update, "print_notice", lambda a, **k: events.append("notice"))
    assert cli.main(["version"]) == 0
    assert events == ["spawn", "notice"]  # spawn before the work, notice after its output


def test_internal_update_check_command_writes_cache(grid_home, monkeypatch):
    monkeypatch.setattr(update, "fetch_latest_version", lambda: "1.2.3")
    assert cli.main(["__update-check"]) == 0
    assert update._read_cache()["latest"] == "1.2.3"


def test_update_reaches_its_handler_in_both_modes(grid_home, monkeypatch, capsys):
    """AGNOSTIC by decision (dispatch.py): replacing the binary is the same act in both modes, so
    neither mode may refuse it — locked end-to-end, not just through the classification set."""
    monkeypatch.setattr(update, "fetch_latest_version", lambda: "0.3.34")
    assert cli.main(["--remote", "update"]) == 0
    assert "latest version" in capsys.readouterr().out
    assert cli.main(["--local", "update"]) == 0
