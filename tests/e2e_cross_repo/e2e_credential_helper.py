"""The credential helper against the REAL relay, over real HTTP (ADR 0033 D-h, issue 17).

The unit tests in `tests/test_local_cli.py` clone from a local path, because a local path is a
perfectly good git "URL" — which is exactly why they cannot test this. No HTTP means no
`Authorization` header, so the helper is never invoked and the one property that matters is never
exercised. Here git really speaks smart-HTTP to grid-src's own `task_git` routes, whose
`_bearer_or_api_key` accepts `Bearer` and nothing else.

`grid` is installed as a real executable on PATH and `sys.argv[0]` points at it, so
`project_clone.grid_command()` resolves the way it does in the field and git runs **this repository's
CLI** as a subprocess of a subprocess. Nothing about the credential path is stubbed.

Run it explicitly (`pytest tests/e2e_cross_repo/e2e_credential_helper.py`); it skips without the
grid-src worktree beside this one, which includes CI.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _harness as H  # noqa: E402

sys.path.insert(0, str(H.GRID_REPO))

NETWORK_ID = "e2e-net"


@pytest.fixture(scope="module")
def grid_bin(tmp_path_factory):
    """This repository's CLI as a real executable, the way an installed `grid` is.

    `sys.argv[0]` under pytest is the pytest binary, so without this the clone would write a helper
    command pointing at pytest — and every assertion about the credential path would be measuring a
    program that cannot answer. A shim rather than a mock: git runs it, with git's own environment,
    through a shell, exactly as it will run the installed console script.
    """
    bindir = tmp_path_factory.mktemp("bin")
    target = bindir / "grid"
    target.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"sys.path.insert(0, {str(H.GRID_REPO)!r})\n"
        "from cli import main\n"
        "sys.exit(main())\n",
        encoding="utf-8")
    target.chmod(0o755)
    return target


@pytest.fixture
def member_home(tmp_path, relay, owner_token):
    """A `GRID_HOME` holding one signed-in grid whose relay is the one under test."""
    home = tmp_path / "grid-home"
    home.mkdir()
    _write_credentials(home, relay, owner_token)
    return home


def _write_credentials(home: Path, relay: str, token: str) -> None:
    """The credential store, written the way `grid login` writes it."""
    sys.path.insert(0, str(H.GRID_REPO))
    from remote import credentials

    previous = os.environ.get("GRID_HOME")
    os.environ["GRID_HOME"] = str(home)
    try:
        credentials.save_credentials({
            "session_token": "session", "api_url": "https://control-plane.invalid",
            "user": {"email": "alice@invalid"},
            "networks": [{"network_id": NETWORK_ID, "name": "e2e", "signaling_url": relay,
                          "access_token": token, "refresh_token": "RT"}]})
    finally:
        if previous is None:
            os.environ.pop("GRID_HOME", None)
        else:
            os.environ["GRID_HOME"] = previous


def _member_env(home: Path, grid_bin: Path, **extra) -> dict[str, str]:
    """What the member's own git runs with — an IDE's environment, near enough.

    Deliberately NOT hermetic in the way `_SEED_GIT_ENV` is: a real `HOME` and a real `PATH` are
    what the helper needs to find `~/.grid` and to be found at all, and pretending otherwise would
    test a topology nobody has.
    """
    env = {
        **os.environ,
        "HOME": str(home.parent),
        "GRID_HOME": str(home),
        "PATH": f"{grid_bin.parent}{os.pathsep}{os.environ.get('PATH', '')}",
        # No ambient identity and no developer's own config deciding what happens here.
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "e2e", "GIT_AUTHOR_EMAIL": "e2e@invalid",
        "GIT_COMMITTER_NAME": "e2e", "GIT_COMMITTER_EMAIL": "e2e@invalid",
    }
    env.update(extra)
    return env


def _git(cwd: Path, *args: str, env: dict[str, str], check=True):
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          env=env, timeout=120)
    if check:
        assert proc.returncode == 0, f"git {' '.join(args)} failed:\n{proc.stdout}\n{proc.stderr}"
    return proc


def _project(relay, owner_token, name, *, with_wip=False):
    """A project with a trunk, and optionally with the caller's WIP branch already created.

    ⚠️ **A member's WIP branch does not exist until the relay makes one.** It is written when a task
    of theirs settles or when they commit — never by creating or joining a project. `with_wip` uses
    the real `POST …/commit` route (ADR 0033 D-j) rather than pushing a ref, because a member
    cannot push, and a helper that reached into the relay's bare repository would be testing a
    topology nobody has.
    """
    from remote import relay as relay_client

    project_id = relay_client.create_project(relay, owner_token, name=name)["id"]
    H.seed_trunk(relay, owner_token, project_id)
    if with_wip:
        import base64

        # `{path, content_b64, executable}` — the wire shape `task_files.parse_files` accepts, which
        # REFUSES unknown keys rather than dropping them.
        relay_client.commit_project(
            relay, owner_token, project_id, message="the member's own work",
            files=[{"path": "mine.txt",
                    "content_b64": base64.b64encode(b"written without an agent\n").decode()}])
    return project_id, relay_client.project_status(relay, owner_token, project_id)


def _clone(relay, owner_token, project_id, status, dest, home, grid_bin, monkeypatch):
    from remote import project_clone, relay as relay_client

    # `grid_command()` reads `sys.argv[0]`; point it at the shim so the config git ends up with
    # names a program that can actually answer.
    monkeypatch.setattr(sys, "argv", [str(grid_bin), "project", "clone"])
    monkeypatch.setenv("GRID_HOME", str(home))
    monkeypatch.setenv("PATH", f"{grid_bin.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    return project_clone.clone_project(
        dest, url=relay_client.git_remote_url(relay, project_id), project_id=project_id,
        branch=status["branch"], trunk=status.get("trunk") or "main",
        relay_base=relay, network_id=NETWORK_ID)


def test_01_a_clone_pulls_over_http_with_no_credential_anywhere_on_disk(
        relay, owner_token, tmp_path, member_home, grid_bin, monkeypatch):
    """The headline criterion, end to end and over real HTTP.

    The clone itself already proves the helper works — the fetch runs through it, with no token in
    this process's git environment — and then a plain `git pull`, the thing an IDE does on a timer,
    is run with nothing of ours in the call path.

    The token is then searched for across every byte of the clone, working tree and `.git/` alike.
    Reading `.git/config` and pronouncing it clean is not the same check: `FETCH_HEAD`, a
    `packed-refs`, or a log would each hold it just as permanently.
    """
    H.require_relay_repo()
    # With a WIP branch, because `git pull` is the thing under test and a branch that does not
    # exist on the remote cannot be pulled — git refuses before any credential is needed, which
    # would make this test pass for a reason that has nothing to do with the helper.
    project_id, status = _project(relay, owner_token, "clone-pull", with_wip=True)
    dest = tmp_path / "clone"

    cloned = _clone(relay, owner_token, project_id, status, dest, member_home, grid_bin, monkeypatch)
    assert not cloned.started_from_trunk, "the fixture did not create the member's branch"
    assert (dest / "mine.txt").exists(), "the member's own work is not in the clone"

    env = _member_env(member_home, grid_bin)
    pulled = _git(dest, "pull", "--ff-only", env=env)
    assert "fatal" not in pulled.stderr.lower(), pulled.stderr

    holding = [p for p in dest.rglob("*") if p.is_file() and owner_token.encode() in p.read_bytes()]
    assert holding == [], f"the token is on disk in {[str(p.relative_to(dest)) for p in holding]}"


def test_02_the_helper_is_really_what_authenticates(
        relay, owner_token, tmp_path, member_home, grid_bin, monkeypatch):
    """The positive control for test 01, and it is not optional.

    A `git pull` that succeeds proves the helper works only if it would FAIL without it. Otherwise
    an ambient credential — an inherited helper, a cached one, a relay that stopped checking — would
    make test 01 pass while the mechanism it names does nothing at all.

    So: break the stored token, and the same pull must fail. Then repair it, and it must succeed
    again. That second half also demonstrates the refresh property from the same evidence, since the
    helper is re-reading `credentials.toml` on every operation rather than caching anything.
    """
    H.require_relay_repo()
    project_id, status = _project(relay, owner_token, "helper-control")
    dest = tmp_path / "clone"
    _clone(relay, owner_token, project_id, status, dest, member_home, grid_bin, monkeypatch)
    env = _member_env(member_home, grid_bin)

    _write_credentials(member_home, relay, "not.a.valid-token")
    broken = _git(dest, "fetch", "origin", env=env, check=False)
    assert broken.returncode != 0, (
        "the fetch succeeded with a junk credential, so something other than the helper is "
        "authenticating and test 01 proves nothing")

    _write_credentials(member_home, relay, owner_token)
    _git(dest, "fetch", "origin", env=env)


def test_03_a_refreshed_token_is_picked_up_without_touching_the_clone(
        relay, owner_token, tmp_path, member_home, grid_bin, monkeypatch):
    """The reason a helper beats a URL with a token in it.

    A token written into `.git/config` once is a token that expires in place, and the member's fix
    is to re-clone or hand-edit a file. Here `credentials.toml` is rewritten with a DIFFERENT valid
    token mid-session — which is what a refresh does — and the very next `git fetch` uses it, with
    nothing in the clone changed and no grid command run.
    """
    H.require_relay_repo()
    project_id, status = _project(relay, owner_token, "refreshed")
    dest = tmp_path / "clone"
    _clone(relay, owner_token, project_id, status, dest, member_home, grid_bin, monkeypatch)
    env = _member_env(member_home, grid_bin)
    config_before = (dest / ".git" / "config").read_bytes()

    # A different token string for the same member — what a refresh produces.
    refreshed = H.token("alice", "client-node-refreshed")
    assert refreshed != owner_token
    _write_credentials(member_home, relay, refreshed)

    _git(dest, "fetch", "origin", env=env)
    assert (dest / ".git" / "config").read_bytes() == config_before, "the clone was rewritten"


def test_04_a_pull_works_with_the_control_plane_unreachable(
        relay, owner_token, tmp_path, member_home, grid_bin, monkeypatch):
    """`grid credential get` makes no network call, asserted by removing the network.

    The CLI's ordinary path for every other remote command is `_resolve()` →
    `remote_grid.resolve_relay_base` → `control_plane.get_managed_network_status`, which is a call to
    a HOST THAT IS NOT THE RELAY. Routing the helper through it would mean a control-plane blip
    breaks `git pull` against a perfectly healthy relay — for every member, on every fetch.

    `GRID_CONTROL_PLANE_URL` is pointed at a closed port. If anything in the helper reaches for the
    control plane, this hangs or fails; the relay itself is untouched.
    """
    H.require_relay_repo()
    project_id, status = _project(relay, owner_token, "no-control-plane")
    dest = tmp_path / "clone"
    _clone(relay, owner_token, project_id, status, dest, member_home, grid_bin, monkeypatch)

    dead = f"http://127.0.0.1:{H.free_port()}"
    env = _member_env(member_home, grid_bin, GRID_CONTROL_PLANE_URL=dead)

    _git(dest, "fetch", "origin", env=env)


def test_05_a_member_cannot_push_their_wip_branch(
        relay, owner_token, tmp_path, member_home, grid_bin, monkeypatch):
    """The clone's output promises the relay refuses this, and here it really does (ADR 0033 D-h).

    A push would add a writer of the WIP branch the task table cannot see — no row is inserted, so
    D-d's serialization cannot reach it, and a push that fast-forwards `wip/<self>` while that
    member's own task is running breaks the task's settle exactly as an unserialized integration
    would. The CLI says so at clone time; this asserts the relay is not merely being trusted to.

    Authenticated, so this measures the FENCE rather than the credential — an unauthenticated push
    would be refused for a reason that proves nothing.
    """
    from remote import relay as relay_client

    H.require_relay_repo()
    project_id, status = _project(relay, owner_token, "no-push")
    dest = tmp_path / "clone"
    _clone(relay, owner_token, project_id, status, dest, member_home, grid_bin, monkeypatch)
    env = _member_env(member_home, grid_bin)
    url = relay_client.git_remote_url(relay, project_id)
    ref = f"refs/heads/{status['branch']}"
    before = H.git_ls_remote(url, ref, bearer=owner_token)

    (dest / "by-hand.txt").write_text("resolved a conflict in my editor\n")
    _git(dest, "add", "-A", env=env)
    _git(dest, "commit", "-q", "-m", "by hand", env=env)

    pushed = _git(dest, "push", "origin", f"HEAD:{status['branch']}", env=env, check=False)

    assert pushed.returncode != 0, (
        f"the relay accepted a member's push to {status['branch']}, which is the one thing "
        f"`grid project clone` tells the member cannot happen")
    # A non-zero exit is not enough on its own: a push can fail AFTER the ref moved. Read the ref
    # back over the same front and require it to be exactly what it was.
    assert H.git_ls_remote(url, ref, bearer=owner_token) == before, (
        f"{status['branch']} moved even though the push reported failure")
