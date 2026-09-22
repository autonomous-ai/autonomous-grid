"""The public CLI's half of one credential store's lock (PRD `grid-scale-phase-a`, issue 12).

Issue 12 locked the two writers of a hosted admin's `credentials.toml` — grid-apis'
`managed_homes.seed_home` and grid-src's `config.upsert_network_credentials` — and put THIS CLI out
of scope on the grounds that its store is *one person's file on their own machine, written by
commands they run one at a time*.

⚠️ **That is true of a laptop and false of the first-provider home.** grid-apis'
`first_provider.py` seeds `homes/providers/<network_id>/` through the same `seed_home_at` — which
takes the lock — and then shells out THIS binary (`grid --remote sync`, `grid --remote join`) into
it. Two writers, one file, and until now only one of them locked, which is the arrangement the whole
seam exists to rule out: a lock only one side takes is a lock that does nothing.

⚠️ **The refusal needed no new decision.** `shared.run_records` already takes this same `file_lock`
for the same kind of read-merge-write and lets a broken lock out, so a filesystem that cannot
`flock` already stops `grid join` and `grid leave`. Locking the credential store the same way costs
those machines nothing they had.
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import _public_credentials_writer as writer
import pytest

from remote import control_plane, credentials
from shared import filelock, paths

REPO_ROOT = Path(__file__).resolve().parent.parent
WRITER = Path(__file__).resolve().parent / "_public_credentials_writer.py"

#: The lock file all THREE repositories must name. Hand-duplicated with grid-src
#: `paths.CREDENTIALS_FILE` + `filelock.LOCK_SUFFIX` and grid-apis
#: `managed_homes.CREDENTIALS_LOCK_FILE`; pinned across them by
#: `tests/test_credentials_lock_lockstep.py`.
CREDENTIALS_LOCK_FILE = "credentials.toml.lock"

_CHILD_TIMEOUT_SECONDS = 60.0


def _unusable_lock(path):
    """A filesystem that cannot `flock`: a writable directory whose lock still cannot be taken."""
    raise OSError(errno.ENOLCK, "No locks available")


# --- the spelling, and that it is a sibling -----------------------------------------------------


def test_this_cli_locks_the_file_the_other_two_repositories_also_name():
    assert filelock.lock_path_for(paths.credentials_file()).name == CREDENTIALS_LOCK_FILE
    assert filelock.LOCK_SUFFIX == ".lock"
    # ⚠️ A SIBLING, never the store: `jsonio.atomic_write_bytes` replaces the inode, so a lock held
    # on the store stops existing the moment anybody writes.
    assert filelock.lock_path_for(paths.credentials_file()) != paths.credentials_file()


# --- two processes, one file --------------------------------------------------------------------


def _race(tmp_path: Path, mode: str) -> set[str]:
    home = tmp_path / "home"
    home.mkdir()
    rendezvous = tmp_path / "rendezvous"
    rendezvous.mkdir()
    children = [
        (role, subprocess.Popen(
            [sys.executable, str(WRITER), nid, role, str(rendezvous), mode],
            cwd=REPO_ROOT,
            env={**os.environ, "GRID_HOME": str(home), "PYTHONPATH": str(REPO_ROOT)},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ))
        for nid, role in (("grid-slow", "slow"), ("grid-fast", "fast"))
    ]
    try:
        for role, child in children:
            _, stderr = child.communicate(timeout=_CHILD_TIMEOUT_SECONDS)
            assert child.returncode == 0, f"the {role} writer failed ({child.returncode}): {stderr}"
    finally:
        # A failed assertion leaves the other child running, possibly parked in `flock`, which has
        # no timeout. Reap unconditionally so a red run cannot orphan an interpreter.
        for _, child in children:
            if child.poll() is None:
                child.kill()
                child.communicate()

    with (home / "credentials.toml").open("rb") as fh:
        return {n["network_id"] for n in tomllib.load(fh).get("networks", [])}


def test_two_processes_adding_different_grids_both_survive(tmp_path):
    assert _race(tmp_path, "locked") == {"grid-slow", "grid-fast"}


def test_without_the_lock_the_slower_writer_drops_the_other_grid(tmp_path):
    """The negative control, and the reason the test above is worth anything."""
    survived = _race(tmp_path, "unlocked")

    assert survived == {"grid-slow"}
    assert "grid-fast" not in survived


# --- the lock is taken once, on every path ------------------------------------------------------


class _RefusesToNest:
    """Stands in for `file_lock` and raises instead of hanging when a path takes it twice.

    ⚠️ Not re-entrant by MECHANISM: `_open_lock_fd` opens a fresh fd per call and `flock` locks are
    per open file description, so a second acquisition on one path blocks on the first forever. A
    test that let that happen would HANG the suite rather than fail it.
    """

    def __init__(self):
        self.taken: list[Path] = []
        self._held = 0

    def __call__(self, path):
        self.taken.append(path)
        if self._held:
            raise AssertionError(f"{path} was locked again while already held — this deadlocks")
        return self

    def __enter__(self):
        self._held += 1

    def __exit__(self, *exc):
        self._held -= 1
        return False


@pytest.mark.parametrize(
    "write",
    [
        pytest.param(lambda: credentials.add_network(writer.record("grid-a")), id="add_network"),
        pytest.param(lambda: credentials.remove_network("grid-a"), id="remove_network"),
        pytest.param(
            lambda: credentials.update_network_tokens("grid-a", access_token="new"),
            id="update_network_tokens",
        ),
        pytest.param(lambda: credentials.clear_credentials(), id="clear_credentials"),
    ],
)
def test_every_writer_takes_the_lock_exactly_once(monkeypatch, tmp_path, write):
    """Every writer, including the DELETE: a logout is a writer here too.

    Unlocked, a logout landing inside another process's load→save is undone by that save, which
    rewrites the file from a snapshot taken while the session still existed — a sign-out that reports
    success and leaves a working credential on disk.
    """
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    credentials.save_credentials({"session_token": "s", "networks": [writer.record("grid-a")]})
    nesting = _RefusesToNest()
    monkeypatch.setattr(filelock, "file_lock", nesting)

    write()

    assert nesting.taken == [paths.credentials_file()]


def test_the_load_and_save_primitives_do_not_take_the_lock_themselves(monkeypatch, tmp_path):
    """Where the lock sits is the whole of why it cannot nest: pushing it into either primitive
    makes every caller that does a load AND a save take it twice on one path."""
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    nesting = _RefusesToNest()
    monkeypatch.setattr(filelock, "file_lock", nesting)

    credentials.save_credentials({"networks": []})
    credentials.load_credentials()

    assert nesting.taken == []


# --- what happens when the lock cannot be taken -------------------------------------------------


def test_a_lock_that_cannot_be_taken_refuses_the_write_and_says_so(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    monkeypatch.setattr(filelock, "file_lock", _unusable_lock)

    with pytest.raises(SystemExit) as refusal:
        credentials.add_network(writer.record("grid-a"))

    message = str(refusal.value)
    assert CREDENTIALS_LOCK_FILE in message, message
    assert not paths.credentials_file().exists(), "refused, then wrote anyway"


@pytest.mark.parametrize("exchange", ["fetch_tokens", "refresh_network_token"])
def test_no_rotating_exchange_is_made_when_its_reply_cannot_be_stored(monkeypatch, tmp_path, exchange):
    """⚠️ A refresh CONSUMES the credential it presents, and the control plane commits before it
    answers — so refusing only at the write leaves the dead token on disk, which is the loss the
    lock exists to prevent, caused by the prevention. Ask first, then make the round trip."""
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    monkeypatch.setattr(filelock, "file_lock", _unusable_lock)
    monkeypatch.setattr(
        control_plane, "_client",
        lambda *a, **kw: pytest.fail(f"{exchange} rotated although its reply cannot be stored"),
    )
    call = {
        "fetch_tokens": lambda: control_plane.fetch_tokens("sess", "dev"),
        "refresh_network_token": lambda: control_plane.refresh_network_token(
            network_id="grid-a", refresh_token="r"),
    }[exchange]

    with pytest.raises(SystemExit) as refusal:
        call()

    assert CREDENTIALS_LOCK_FILE in str(refusal.value)


def test_a_write_that_fails_inside_the_lock_keeps_its_own_error(monkeypatch, tmp_path):
    """⚠️ Only the ACQUISITION is translated. One `except OSError` around the whole `with` block
    reports a full disk during the write as a lock that could not be taken."""
    monkeypatch.setenv("GRID_HOME", str(tmp_path))

    def _no_space(data):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(credentials, "save_credentials", _no_space)

    with pytest.raises(OSError) as failure:
        credentials.add_network(writer.record("grid-a"))

    assert failure.value.errno == errno.ENOSPC
    assert not isinstance(failure.value, SystemExit)


# --- `grid sync`: the writer whose critical section used to span a network round trip -----------


def _sync(monkeypatch, tmp_path, *, during_fetch, stored=None):
    """Run `grid sync` with `during_fetch` standing in for another process writing mid-round-trip."""
    import cli
    from cli import signout

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    credentials.save_credentials(
        stored if stored is not None
        else {"session_token": "s", "api_url": "https://api.example.com", "networks": []}
    )

    def fake_fetch(session_token, device_id, api_url=None):
        during_fetch()
        return control_plane.TokenFetch(networks=[writer.record("grid-a")], os_served=None)

    monkeypatch.setattr(control_plane, "fetch_tokens", fake_fetch)
    # `warn_stranded` reads the host process table; not the seam under test here.
    monkeypatch.setattr(signout, "warn_stranded", lambda *a, **kw: None)
    return cli.cmd_sync(cli.build_parser().parse_args(["sync"]))


def test_sync_merges_the_file_as_it_is_now_not_the_snapshot_it_started_with(monkeypatch, tmp_path):
    """⚠️ The one writer here whose critical section spanned a NETWORK ROUND TRIP.

    `cmd_sync` read the file, fetched for seconds, then merged its grid list into that first
    snapshot — so every key another writer changed while the fetch was in flight was put back as it
    had been. This is the command grid-apis shells into a first-provider home it also writes, so the
    other writer is not hypothetical. The lock cannot shrink the fetch's window; re-reading under it
    makes the MERGE see the file as it is now.
    """
    def another_process_writes():
        credentials.save_credentials({**credentials.load_credentials(), "user": {"email": "x@y"}})

    assert _sync(monkeypatch, tmp_path, during_fetch=another_process_writes) == 0

    data = credentials.load_credentials()
    assert data.get("user") == {"email": "x@y"}, "the concurrent write was merged away"
    assert [n["network_id"] for n in data.get("networks", [])] == ["grid-a"]
    assert data.get("session_token") == "s"


def test_sync_does_not_resurrect_a_session_a_concurrent_logout_removed(monkeypatch, tmp_path):
    """⚠️ The older code put the session token BACK, from the snapshot it read before the fetch.

    A `grid logout` that lands while a sync is in flight was undone by the sync's own merge: the
    file reappeared with a working session, and the sign-out had already reported success. Under the
    lock the re-read is authoritative, so the sign-out wins and nothing is written.
    """
    with pytest.raises(SystemExit) as refused:
        _sync(monkeypatch, tmp_path, during_fetch=credentials.clear_credentials)

    assert "signed out while" in str(refused.value)
    assert not paths.credentials_file().exists(), "the sync put the signed-out credential back"
