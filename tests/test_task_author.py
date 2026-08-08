"""A commit says who asked for it — the provider's half (ADR 0033 D-m, feature issue 21).

The relay authors a task's INPUT commit; this repo authors its RESULT commit, which is the one
holding everything the agent wrote. Both name the project member who asked for the task, and both
are committed by `grid`.

The member's identity arrives on the **claim payload**, because the provider has nothing else to
resolve it from: the creating request's identity is long gone by the time a task is picked up, and
the provider has no access to the relay's `users` table. *Absent ⇒ `grid <grid@invalid>`*, which is
what this repo did before this slice — so an old relay's payload degrades to anonymity rather than
to a failure, in either rollout direction.

Its own module rather than more of `test_task_agent.py`, which is 145 KB and long past this repo's
800-line file ceiling.

Every identity is read back with real `git log` / `git blame` run by the test, never through the
module that wrote it.
"""
from __future__ import annotations

import os
import subprocess

import pytest

# The identity every commit falls back to, spelled out rather than imported: a test that took the
# constant from the module under test would keep passing if the constant changed.
GRID = "grid|grid@invalid"

# A `member_key` shaped like the relay's — 32 hex characters. Since ADR 0033 D-g a workspace, and
# the transcript pathspec the result commit stages, belong to a (project, member) pair. Unrelated to
# the AUTHOR this module is about: the key names the workspace, the author names the person, and
# nothing derives one from the other.
_MEMBER = "9f2b" * 8


def _git(cwd, *args, check=True):
    """Real git, run by the TEST — never the module under test.

    Its own identity (`t`), deliberately different from both `grid` and any member below, so a
    commit this helper makes is never mistaken for one the code under test made.
    """
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=check,
        env={"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_NOSYSTEM": "1",
             "HOME": "/nonexistent", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@invalid",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@invalid"})


def _remote_for(tmp_path, branch, files, name="origin.git"):
    """A real bare repo standing in for the relay's, and the branch tip. `(GitRemote, commit)`."""
    from remote.task_repo import GitRemote

    seed = tmp_path / f"seed-{name}"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main", ".")
    for path, content in files.items():
        target = seed / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "input")
    _git(seed, "branch", "-f", branch)
    bare = tmp_path / name
    _git(tmp_path, "clone", "--bare", "-q", str(seed), str(bare))
    return GitRemote(url=str(bare), token="tok"), _git(bare, "rev-parse", branch).stdout.strip()


def _transcript_dir(path):
    """This member's conversation directory inside `path`, which `commit_and_push` now requires.

    Required rather than defaulted there, so a caller cannot silently stop committing the
    conversation — see that function. Here it is setup noise: nothing in this module writes a
    transcript, and every test below is about the author.
    """
    from remote import task_agent

    return task_agent.transcript_dir(path, _MEMBER)


def _idents(repo, rev):
    """`author-name|author-email|committer-name|committer-email`, the four fields this slice moves.

    All four together, so a test that only checked the author would not silently accept the
    committer changing with it.
    """
    return _git(repo, "log", "-1", "--format=%an|%ae|%cn|%ce", rev).stdout.strip()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A provider workspace rooted somewhere writable, materialized on demand from a remote."""
    from remote import task_agent, task_repo

    monkeypatch.setenv("GRID_TASK_ROOT", str(tmp_path / "root"))

    def _materialize(remote, commit, branch="task/T1"):
        path = task_agent.ensure_workspace(task_agent.workspace_for("proj-1", _MEMBER))
        task_repo.materialize(path, url=remote.url, token=remote.token,
                              branch=branch, input_commit=commit)
        return path

    return _materialize


class TestTheResultCommitCarriesTheMember:
    """The author is the person; the committer is the grid. Both halves, every time.

    Setting all four to the member would satisfy any test that only read the author, and it would
    destroy the property the split exists for (ADR 0033 D-m): the commit stays evidently
    machine-made while `blame` attributes the intent.
    """

    def test_the_author_is_the_member_and_the_committer_is_grid(self, tmp_path, workspace):
        from remote import task_repo

        remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
        path = workspace(remote, commit)
        (path / "fix.py").write_text("done\n")

        pushed = task_repo.commit_and_push(
            path, url=remote.url, token=remote.token, branch="task/T1", message="task T1",
            transcript=_transcript_dir(path),
            author=task_repo.GitIdentity("Alice Nguyen", "alice@example.com"))

        assert _idents(remote.url, pushed.commit) == f"Alice Nguyen|alice@example.com|{GRID}"

    def test_no_author_is_the_pre_0033_identity_on_both_halves(self, tmp_path, workspace):
        """What an OLD relay's payload produces, and it has to be identical, not merely similar."""
        from remote import task_repo

        remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
        path = workspace(remote, commit)
        (path / "fix.py").write_text("done\n")

        pushed = task_repo.commit_and_push(
            path, url=remote.url, token=remote.token, branch="task/T1", message="task T1",
            transcript=_transcript_dir(path))

        assert _idents(remote.url, pushed.commit) == f"{GRID}|{GRID}"

    def test_blame_on_a_line_the_agent_wrote_names_the_member(self, tmp_path, workspace):
        """Issue 21's second acceptance criterion, asked of git the way a person would ask it.

        This is the whole point of the slice: not that a commit header says a name, but that
        `git blame` on the line an agent produced answers the member who asked for it instead of
        `grid` — which is the question nobody can answer from a repository today.
        """
        from remote import task_repo

        remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "first\n"})
        path = workspace(remote, commit)
        (path / "a.txt").write_text("first\nthe agent wrote this\n")

        task_repo.commit_and_push(
            path, url=remote.url, token=remote.token, branch="task/T1", message="task T1",
            transcript=_transcript_dir(path),
            author=task_repo.GitIdentity("Alice Nguyen", "alice@example.com"))

        blamed = _git(path, "blame", "--line-porcelain", "-L", "2,2", "a.txt").stdout
        assert "author Alice Nguyen" in blamed, blamed
        assert "author-mail <alice@example.com>" in blamed, blamed
        # The line that was already there is NOT re-attributed — blame still names whoever wrote it.
        first = _git(path, "blame", "--line-porcelain", "-L", "1,1", "a.txt").stdout
        assert "author t" in first, first


class _Publisher:
    """Just enough of `TaskEventPublisher` to record what a failing push tried to say."""

    def __init__(self):
        self.published = []

    def publish(self, event_type, **fields):
        self.published.append((event_type, fields))


class TestTheClaimPayloadIsWhereTheAuthorComesFrom:
    """The provider has nothing else to resolve a member from — no `users` table, and the creating
    request's identity is long gone by the time a task is claimed.

    A hand-duplicated lockstep value with the relay's `_claim_one`, and the cheapest kind:
    *absent ⇒ `grid <grid@invalid>`*. An old relay sends neither key and this falls back; an old
    provider ignores both. Neither half can break the other, so there is no rollout order.
    """

    def _push(self, tmp_path, workspace, job_extra):
        from remote import task_repo, tasks

        remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
        path = workspace(remote, commit)
        (path / "fix.py").write_text("done\n")
        job = {"task_id": "T1", "project_id": "proj-1", "member_key": _MEMBER,
               "branch": "task/T1", **job_extra}
        outcome, landed = tasks._push_result(
            job, tasks.TaskOutcome("completed", "ok", None), True, remote, _Publisher())
        assert landed, outcome.error
        return remote, outcome, task_repo

    def test_the_two_keys_reach_the_result_commit(self, tmp_path, workspace):
        remote, outcome, _ = self._push(tmp_path, workspace, {
            "author_name": "Alice Nguyen", "author_email": "alice@example.com"})

        assert _idents(remote.url, outcome.result_commit) == (
            f"Alice Nguyen|alice@example.com|{GRID}")

    def test_a_payload_with_neither_key_still_pushes(self, tmp_path, workspace):
        """Issue 21's third acceptance criterion. The task must land, anonymously."""
        remote, outcome, _ = self._push(tmp_path, workspace, {})

        assert _idents(remote.url, outcome.result_commit) == f"{GRID}|{GRID}"

    @pytest.mark.parametrize("name, email, expected", [
        # The POSITIVE control: without it a provider that flattened everything to `grid` would
        # satisfy every other row here.
        ("Alice Nguyen", "alice@example.com", "Alice Nguyen|alice@example.com"),
        # A NUL is what `_clean` is load-bearing for: git never gets a chance to be lenient, because
        # Python refuses to build an environment containing one, and that `ValueError` is not an
        # `OSError` — so WITHOUT the strip it would escape `_run`'s handler, become a `PushError`,
        # leave the task `running`, and be retried into the identical failure on every provider
        # forever. These two rows are what hold the strip in place.
        ("Alice\x00", "alice@example.com", "Alice|alice@example.com"),
        ("Alice", "alice\x00@example.com", "Alice|alice@example.com"),
        # git REFUSES an empty or whitespace-only author name outright.
        ("", "alice@example.com", "alice|alice@example.com"),
        ("   ", "alice@example.com", "alice|alice@example.com"),
        # No usable address ⇒ the whole default. `Alice <>` names someone unreachable.
        ("Alice", "", GRID),
        ("Alice", "<>", GRID),
        # git strips these itself; stripped here too so both commits of one task agree.
        ("al<ice>", "a@b.c", "alice|a@b.c"),
        ("al\nice", "a@b.c", "alice|a@b.c"),
        # git does NOT strip these two — a commit object would keep them verbatim.
        ("al\rice", "a@b.c", "alice|a@b.c"),
        ("al\tice", "a@b.c", "alice|a@b.c"),
        # Wrong TYPE, not merely a wrong value: the payload is JSON off the wire and nothing
        # upstream promises a string.
        (None, "alice@example.com", "alice|alice@example.com"),
        (12345, "alice@example.com", "alice|alice@example.com"),
        ({"name": "Alice"}, "alice@example.com", "alice|alice@example.com"),
        ("Alice", ["alice@example.com"], GRID),
        ("Alice", 12345, GRID),
    ])
    def test_a_hostile_payload_never_costs_the_task(self, tmp_path, workspace, name, email,
                                                    expected):
        """The claim payload is a system boundary: authenticated, not trusted.

        Every row has to PUSH. A refusal here is not a lost name — the task is left `running`, its
        lease lapses, another provider claims it, and it fails identically. Forever.
        """
        remote, outcome, _ = self._push(tmp_path, workspace, {
            "author_name": name, "author_email": email})

        assert _idents(remote.url, outcome.result_commit) == f"{expected}|{GRID}"


class TestTheIsolationIsUnchanged:
    """Issue 21's fifth acceptance criterion. Four variables move; the posture does not."""

    def test_the_hardening_variables_survive_an_author(self):
        from remote import task_repo

        env = task_repo._env(author=task_repo.GitIdentity("Alice", "alice@example.com"))

        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert env["HOME"] == "/nonexistent"
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_the_credential_still_travels_with_an_author(self):
        """An author must not displace the token, which rides the same fixed dict."""
        from remote import task_repo

        env = task_repo._env("SEKRIT", task_repo.GitIdentity("Alice", "alice@example.com"))

        assert env["GIT_CONFIG_VALUE_0"] == "Authorization: Bearer SEKRIT"
        assert env["GIT_CONFIG_VALUE_1"] == "false"

    def test_the_committer_is_never_the_member(self):
        """The kill-test for a later "just set all four" (ADR 0033 D-m)."""
        from remote import task_repo

        env = task_repo._env(author=task_repo.GitIdentity("Alice", "alice@example.com"))

        assert env["GIT_COMMITTER_NAME"] == "grid"
        assert env["GIT_COMMITTER_EMAIL"] == "grid@invalid"

    def test_a_commit_the_agent_makes_itself_still_names_the_member(self, monkeypatch):
        """ADR 0033 D-m, reached from the one direction issue 15 opened.

        Until tier 3 the agent was never asked to commit — the provider did it, with
        `GIT_AUTHOR_*` set from the claim. A merge task's prompt tells the agent to commit the
        merge, so its OWN `git commit` now writes history.

        `_GIT_CONFIG_FLOOR` was believed to make that fail loudly: with `GIT_CONFIG_GLOBAL` at
        `/dev/null` the agent inherits no `user.name`. **Measured on git 2.54.0 — it does not
        fail.** git auto-detects an identity from the OS username and hostname and exits 0, so the
        merge commit would be authored `<user>@<hostname>`: the provider's machine, in the
        requesting team's history, permanently. Authorship is the one thing ADR 0033 records as not
        retroactively fixable.

        So the identity is on the child's environment, exactly as it is on the provider's own git
        calls, and an agent commit and a grid commit come out identical.
        """
        from remote import task_agent, task_repo

        env = task_agent.child_env(
            author=task_repo.GitIdentity("Alice Nguyen", "alice@example.com"))

        assert env["GIT_AUTHOR_NAME"] == "Alice Nguyen"
        assert env["GIT_AUTHOR_EMAIL"] == "alice@example.com"
        # The committer is the grid on both halves of the split, whatever the author says.
        assert env["GIT_COMMITTER_NAME"] == task_repo.DEFAULT_IDENTITY.name
        assert env["GIT_COMMITTER_EMAIL"] == task_repo.DEFAULT_IDENTITY.email

    def test_an_agent_commit_with_no_author_on_the_claim_is_the_pre_0033_identity(self):
        """The same degrade every other half of D-m has: an older relay sends no author keys, and
        the result is `grid <grid@invalid>` — never the provider's hostname."""
        from remote import task_agent, task_repo

        env = task_agent.child_env()

        assert env["GIT_AUTHOR_NAME"] == task_repo.DEFAULT_IDENTITY.name
        assert env["GIT_AUTHOR_EMAIL"] == task_repo.DEFAULT_IDENTITY.email

    def test_no_configuration_file_on_the_provider_can_reach_a_commit(
            self, tmp_path, workspace, monkeypatch):
        """Asserted by BEHAVIOUR: put a config on the host and watch it fail to matter.

        This matters more here than on the relay — a `[core] hooksPath` in the provider operator's
        own `~/.gitconfig` would otherwise run against a repository whose contents came off the wire.
        """
        from remote import task_repo

        remote, commit = _remote_for(tmp_path, "task/T1", {"a.txt": "x\n"})
        path = workspace(remote, commit)
        home = tmp_path / "home"
        home.mkdir()
        (home / ".gitconfig").write_text(
            "[user]\n\tname = hijack\n\temail = hijack@evil.invalid\n"
            "[core]\n\thooksPath = /tmp/hooks\n", encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))
        # The ambient environment is the other door, and a fixed allowlist is what closes it:
        # `_env()` BUILDS a dict rather than copying `os.environ`.
        monkeypatch.setenv("GIT_AUTHOR_NAME", "hijack")
        monkeypatch.setenv("GIT_COMMITTER_EMAIL", "hijack@evil.invalid")

        pushed = task_repo.commit_and_push(
            path, url=remote.url, token=remote.token, branch="task/T1", message="task T1",
            transcript=_transcript_dir(path),
            author=task_repo.GitIdentity("Alice", "alice@example.com"))

        assert _idents(remote.url, pushed.commit) == f"Alice|alice@example.com|{GRID}"
