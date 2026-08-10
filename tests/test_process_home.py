"""Which `GRID_HOME` a live process belongs to — the discriminator the argv sweep does not have.

Written for dev-VM finding E-03: two accounts serving the SAME grid from one provider box spawn
children whose `__remote-engine <network_id> <engine_id>` argv agrees in every token, so one
operator's `grid leave` reaped the other's provider, silently. Nothing on the command line separates
them; `GRID_HOME` does.

The conftest fixture `_no_test_reads_a_real_process_environment` patches `home_of` away for every
test in the suite, so the tests here that mean to exercise the real reader call the underlying
`_read_environ` / `_home_from_environ` directly.
"""
from __future__ import annotations

import os
import sys

import pytest

from shared import process_home


# --- reading a home out of one process's environment ---------------------------------------------

def test_an_explicit_grid_home_is_read_verbatim():
    """The variable wins when it is set, and the answer is absolute and normalised."""
    assert process_home._home_from_environ(
        {"GRID_HOME": "/root/.grid-provider3", "HOME": "/root"}
    ) == "/root/.grid-provider3"


def test_an_unset_grid_home_falls_back_to_THAT_process_HOME_not_ours(monkeypatch, tmp_path):
    """The default is `<HOME>/.grid`, and the HOME that decides it is the *other* process's.

    Positive control against the obvious bug: `Path("~/.grid").expanduser()` would expand against the
    environment of whoever is running `grid leave`, so a child of another account's home would be
    read as ours and killed — the very failure this module exists to stop.

    Real directories, because `_canonical` resolves them: `/home/...` is an autofs firmlink on macOS
    and comes back as `/System/Volumes/Data/home/...`, so a literal expectation would assert the
    host's filesystem layout rather than whose HOME was used.
    """
    theirs = tmp_path / "bob"
    (theirs / ".grid").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "alice"))
    assert process_home._home_from_environ({"HOME": str(theirs)}) == os.path.realpath(
        theirs / ".grid")


@pytest.mark.parametrize("env, why", [
    ({}, "nothing to go on at all"),
    ({"GRID_HOME": ""}, "set but empty — the shell's way of saying unset"),
    ({"GRID_HOME": "relative/path"}, "resolves against a cwd we cannot know"),
    ({"HOME": "relative"}, "same, one level down"),
    ({"GRID_HOME": "~someone/.grid", "HOME": "/root"}, "another account's ~, which we cannot expand"),
    ({"GRID_HOME": "~/.grid"}, "a bare ~ with no HOME beside it"),
])
def test_a_home_we_cannot_pin_down_is_unknown(env, why):
    """Unknown, never a guess. Everything downstream treats unknown as *ours*, i.e. exactly the
    pre-fix behaviour — so a wrong guess here would either strand an orphan or resurrect E-03."""
    assert process_home._home_from_environ(env) is None, why


def test_a_leading_tilde_expands_against_the_processs_own_home():
    """`GRID_HOME=~/.grid-b` is what an operator actually types, and it is not the same directory
    for two accounts."""
    assert process_home._home_from_environ(
        {"GRID_HOME": "~/.grid-b", "HOME": "/root"}
    ) == "/root/.grid-b"


def test_the_first_spelling_of_a_repeated_variable_wins():
    """A duplicated key in an environment block is what libc `getenv` answers first, and a process
    can be handed one. Reading the last would let a planted trailing copy re-point the answer."""
    blob = b"GRID_HOME=/first\x00GRID_HOME=/second\x00"
    assert process_home._parse_environ_blob(blob)["GRID_HOME"] == "/first"


# --- reading it off a live process ---------------------------------------------------------------

@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc/<pid>/environ is Linux-only")
def test_the_real_reader_reads_this_processs_own_environment():
    """The positive control the rest of this file needs: the Linux reader really does work.

    Without it every other assertion here is about a parser that might never be handed anything —
    "measuring the fake" is how this feature's two worst defects survived a green suite.
    """
    env = process_home._read_environ(os.getpid())
    assert env is not None
    assert env.get("PATH")  # a variable every process has, so this cannot pass on an empty dict


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc/<pid>/environ is Linux-only")
def test_the_real_reader_is_unknown_for_a_pid_that_names_nothing():
    """A pid with no `/proc` entry answers unknown rather than raising — this runs inside a teardown
    that must never crash."""
    assert process_home._read_environ(process_home._UNUSED_PID_PROBE) is None


def test_a_platform_without_proc_answers_unknown(monkeypatch):
    """macOS 26.6 was measured: `ps -E` prints no environment even for the caller's own child, and
    Windows would need a remote-thread PEB read. Both answer unknown, which is the pre-fix
    behaviour — this check narrows what a sweep kills and never widens it."""
    monkeypatch.setattr(process_home.sys, "platform", "darwin")
    assert process_home._read_environ(os.getpid()) is None


def test_an_environment_too_large_to_trust_is_unknown(monkeypatch, tmp_path):
    """A block at the read cap may have `GRID_HOME` past the cut, and a truncated read that answered
    "no GRID_HOME" would fall back to HOME and name the wrong directory with confidence."""
    monkeypatch.setattr(process_home, "_ENVIRON_MAX_BYTES", 16)
    monkeypatch.setattr(process_home.sys, "platform", "linux")
    blob = tmp_path / "environ"
    blob.write_bytes(b"GRID_HOME=/root/.grid-provider3\x00")
    monkeypatch.setattr(process_home, "_environ_path", lambda pid: blob)
    assert process_home._read_environ(1234) is None


# --- the answer the sweep acts on ----------------------------------------------------------------

def test_only_a_home_proven_DIFFERENT_is_named(monkeypatch):
    """Three pids, three answers: ours, unreadable, and another account's. Only the last is named.

    The middle one is the load-bearing case — an unknown home must stay killable, or a real
    record-less orphan (this whole sweep's reason to exist) is stranded on every platform that
    cannot read an environment.
    """
    monkeypatch.setenv("GRID_HOME", "/root/.grid-provider")
    homes = {111: "/root/.grid-provider", 222: None, 333: "/root/.grid-provider3"}
    monkeypatch.setattr(process_home, "home_of", homes.get)
    assert process_home.other_home_pids([111, 222, 333]) == {333: "/root/.grid-provider3"}


def test_nothing_is_spared_when_we_cannot_pin_down_our_OWN_home(monkeypatch):
    """If we cannot say where we live, we cannot prove anybody else lives elsewhere — and the
    failure direction has to be "sweep as before", not "spare everything"."""
    monkeypatch.setattr(process_home, "own_home", lambda: None)
    monkeypatch.setattr(process_home, "home_of", lambda pid: "/root/.grid-provider3")
    assert process_home.other_home_pids([333]) == {}


def test_two_spellings_of_one_directory_are_the_same_home(monkeypatch, tmp_path):
    """`realpath` on both sides, so a symlinked or trailing-slash `GRID_HOME` does not read as
    somebody else's box and strand this account's own orphan."""
    real = tmp_path / "grid-real"
    real.mkdir()
    link = tmp_path / "grid-link"
    link.symlink_to(real)
    monkeypatch.setenv("GRID_HOME", str(real))
    monkeypatch.setattr(process_home, "home_of",
                        lambda pid: process_home._home_from_environ({"GRID_HOME": f"{link}/"}))
    assert process_home.other_home_pids([333]) == {}
