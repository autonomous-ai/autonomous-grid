"""The live view of a running task's working directory (ADR 0032 issue 08, D-f).

Its own module rather than more of `test_task_agent.py`, which is already the largest suite in this
repo — and the subject is different again: that one is about the agent child, `test_task_lease.py` is
about the fact the provider keeps asserting about it, and this one is about the *shape of the
directory* that fact is asserted over.

The rule the whole module exists to pin: **a tree snapshot is progress, and progress may never cost
the result.** Every failure here — an unreadable directory, a workspace that is not a git repository,
a file the agent deleted between the listing and the stat — is a snapshot that does not happen, never
a task that fails. The second rule is the one that keeps the first affordable: **an unchanged tree
publishes nothing**, so a task that sits in a ten-minute test run adds no traffic at all.
"""

import json
import os
import subprocess

import pytest


def _git(cwd, *args):
    """Real git, with the same scrubbed environment the provider's own invocations get."""
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        env={"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_NOSYSTEM": "1",
             "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": "/nonexistent",
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@invalid",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@invalid"})


def _write(workspace, relative, text="x"):
    path = workspace / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def workspace(tmp_path):
    """A real git working copy, prepared exactly the way the provider prepares one.

    Through `task_repo`'s own `_ensure_repo` rather than a hand-rolled `git init`, so the
    `$GIT_DIR/info/exclude` that hides `.grid/` is the real one — the tree's exclusion of the
    provider's reserved directory is a property of that file, and a test that wrote its own would be
    asserting against a fixture instead of against the code.
    """
    from remote import task_repo

    path = tmp_path / "workspace"
    path.mkdir()
    task_repo._ensure_repo(path)
    return path


def test_a_snapshot_names_the_files_in_the_workspace(workspace):
    """The tracer bullet: what the agent has created is what the client gets to see."""
    from remote import task_tree

    _write(workspace, "README.md")
    _write(workspace, "src/main.py")

    snapshot = task_tree.snapshot(workspace)

    assert snapshot.paths == ("README.md", "src/main.py")
    assert snapshot.total == 2
    assert snapshot.truncated is False


def test_the_projects_own_ignore_rules_keep_a_dependency_tree_out(workspace):
    """The criterion: a workspace holding a large dependency tree does not produce an oversized
    payload — and the mechanism is the PROJECT's rules, not a list of directory names we guessed.

    `node_modules/` is only excluded here because this repository's own `.gitignore` says so. A
    hand-rolled skip list would have to guess `vendor/`, `target/`, `.venv/`, `dist/` and every
    convention that exists in a language nobody here has heard of, and would be silently wrong for
    all the rest.
    """
    from remote import task_tree

    _write(workspace, ".gitignore", "node_modules/\n*.log\n")
    _write(workspace, "src/main.py")
    for n in range(50):
        _write(workspace, f"node_modules/pkg{n}/index.js")
    _write(workspace, "build.log")

    paths = task_tree.snapshot(workspace).paths

    assert paths == (".gitignore", "src/main.py")


def test_the_providers_own_reserved_directory_is_never_shown(workspace):
    """`.grid/` is the provider's internals — the transcript, the agent's memory — and it is not
    part of the shape of the user's workspace.

    It is excluded by the SAME rule that keeps it out of `git add -A`: the `$GIT_DIR/info/exclude`
    that `_ensure_repo` writes. Restating it here as a second skip list is exactly how two copies of
    one rule drift apart in silence.
    """
    from remote import task_tree

    _write(workspace, "src/main.py")
    _write(workspace, ".grid/agent/projects/-var-grid/session.jsonl", "{}")

    assert task_tree.snapshot(workspace).paths == ("src/main.py",)


def test_a_file_the_agent_deleted_leaves_the_snapshot_at_once(workspace):
    """The index outlives the file, and the snapshot must not.

    A tracked file the agent removes stays in the index until the terminal commit — so a listing
    built from `--cached` alone would keep showing it for the rest of the run, in the one view whose
    entire job is to say what is there NOW.
    """
    from remote import task_tree

    _write(workspace, "keep.py")
    _write(workspace, "gone.py")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-m", "input")

    (workspace / "gone.py").unlink()

    assert task_tree.snapshot(workspace).paths == ("keep.py",)


def test_an_oversized_tree_is_delivered_truncated_and_says_so(workspace):
    """The criterion, and the reason the cap cannot be advisory.

    An event over the relay's `_MAX_EVENT_BYTES` is refused with a 422 — and a 422 does not latch
    the publisher off, it DROPS the batch. So an uncapped tree would not merely fail to appear: it
    would take every progress line batched beside it down with it, every beat, for the life of the
    task. Asserted against `task_events.MAX_EVENT_BYTES` rather than a literal, because that
    constant is the one held in lockstep with the relay's.
    """
    from remote import task_events, task_tree

    for n in range(2000):
        _write(workspace, f"generated/file{n:05d}.txt")

    snapshot = task_tree.snapshot(workspace)

    assert snapshot.truncated is True
    assert snapshot.total == 2000
    assert len(snapshot.paths) < 2000
    on_the_wire = json.dumps({"type": "task.tree", **snapshot.as_event()}).encode("utf-8")
    assert len(on_the_wire) <= task_events.MAX_EVENT_BYTES


class _RecordingPublisher:
    """The task's event channel, recorded. Same shape `TaskEventPublisher` presents.

    Returning `True` is part of that shape, not boilerplate: the real publisher answers whether it
    ACCEPTED the event, and a double that answered `None` would make every snapshot look refused.
    """

    def __init__(self):
        self.published = []

    def publish(self, kind, *, blocking=True, **fields):
        self.published.append((kind, fields))
        return True


def test_the_cap_is_measured_in_what_JSON_costs_not_in_what_the_path_weighs(workspace):
    """The budget has to count the bytes the RELAY counts, and those are not the path's own.

    `json.dumps` escapes to ASCII by default — which is exactly how grid-src measures an event
    against `_MAX_EVENT_BYTES` — so a CJK path costs six bytes per character on the wire against
    three in UTF-8, and a control character costs six against one. A budget that counted the path's
    own length would let an ordinary Chinese-language project produce an event the relay refuses with
    a 422 — and a 422 drops the WHOLE batch, so the user's agent output would disappear alongside it,
    every beat, for the rest of the task.
    """
    from remote import task_events, task_tree

    for n in range(400):
        _write(workspace, f"文書/測試檔案名稱很長很長很長很長{n:04d}.txt")

    snapshot = task_tree.snapshot(workspace)

    on_the_wire = json.dumps({"type": "task.tree", **snapshot.as_event()}).encode("utf-8")
    assert len(on_the_wire) <= task_events.MAX_EVENT_BYTES, (
        f"{len(on_the_wire)} bytes on the wire against a {task_events.MAX_EVENT_BYTES} ceiling")


def test_a_path_full_of_control_characters_cannot_burst_the_event(workspace):
    """The worst case of the same rule, and it is reachable rather than theoretical: the agent runs
    with `bypassPermissions` against a prompt nobody here wrote, and every byte except `/` and NUL is
    a legal filename character."""
    from remote import task_events, task_tree

    for n in range(400):
        _write(workspace, "odd/" + "\x01" * 60 + f"{n:04d}")

    snapshot = task_tree.snapshot(workspace)

    on_the_wire = json.dumps({"type": "task.tree", **snapshot.as_event()}).encode("utf-8")
    assert len(on_the_wire) <= task_events.MAX_EVENT_BYTES, (
        f"{len(on_the_wire)} bytes on the wire against a {task_events.MAX_EVENT_BYTES} ceiling")


def test_a_beat_publishes_the_tree_and_an_unchanged_tree_publishes_nothing(workspace):
    """The criterion that makes this feature free: watching an idle task produces no tree events.

    Not a rate limiter and not a sampling interval — the comparison is on the CONTENT. A task that
    spends ten minutes in a test suite and then writes one file publishes exactly twice.
    """
    from remote import task_tree

    publisher = _RecordingPublisher()
    tree = task_tree.WorkspaceTree(workspace, publisher)
    _write(workspace, "src/main.py")

    tree.beat()
    tree.beat()
    tree.beat()

    assert [kind for kind, _fields in publisher.published] == ["task.tree"]
    assert publisher.published[0][1]["paths"] == ["src/main.py"]


def test_a_beat_publishes_again_the_moment_the_agent_creates_a_file(workspace):
    """The other half of the criterion: it updates as the agent works."""
    from remote import task_tree

    publisher = _RecordingPublisher()
    tree = task_tree.WorkspaceTree(workspace, publisher)
    _write(workspace, "src/main.py")
    tree.beat()

    _write(workspace, "src/util.py")
    tree.beat()

    assert [fields["paths"] for _kind, fields in publisher.published] == [
        ["src/main.py"], ["src/main.py", "src/util.py"]]
    hashes = [fields["hash"] for _kind, fields in publisher.published]
    assert hashes[0] != hashes[1]


def test_a_workspace_that_is_not_a_repository_costs_the_tree_and_nothing_else(tmp_path, capsys):
    """The criterion: tree publication failing does not fail the task or break the heartbeat.

    Reachable in exactly one configuration — a relay with no git plane sends no `input_commit`, so
    nothing ever runs `_ensure_repo` on this workspace. That is the pre-issue-04 degrade, and the
    right answer to it is the pre-issue-08 behaviour: no tree, and a task that runs exactly as it
    did before.
    """
    from remote import task_tree

    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    publisher = _RecordingPublisher()

    task_tree.WorkspaceTree(plain, publisher).beat()

    assert publisher.published == []
    assert "tree" in capsys.readouterr().err.lower()


def test_a_workspace_that_vanished_underneath_the_beat_is_not_an_incident(tmp_path):
    """The other half of the criterion: a race with the agent, or with a reclaim's cleanup."""
    from remote import task_tree

    publisher = _RecordingPublisher()
    tree = task_tree.WorkspaceTree(tmp_path / "never-existed", publisher)

    tree.beat()

    assert publisher.published == []


def test_a_system_exit_from_the_listing_does_not_escape_the_beat(workspace, monkeypatch):
    """`SystemExit` is this repo's clean-error idiom and is NOT an `Exception`.

    A guard naming only `Exception` would let it through — and this beat runs on the lease renewer's
    thread, where an escaping `SystemExit` kills the renewal loop. The task would then be reclaimed
    mid-run, and the cause would be a file listing.
    """
    from remote import task_repo, task_tree

    def _boom(*_args, **_kwargs):
        raise SystemExit("no")

    monkeypatch.setattr(task_repo, "list_files", _boom)
    publisher = _RecordingPublisher()

    task_tree.WorkspaceTree(workspace, publisher).beat()

    assert publisher.published == []


def test_a_publisher_that_raises_does_not_escape_the_beat(workspace):
    """`TaskEventPublisher` is documented never to raise — and this beat's contract cannot rest on
    another module keeping a promise, for the same reason `tasks._publish_safely` does not."""
    from remote import task_tree

    class _Exploding:
        def publish(self, *_args, **_kwargs):
            raise RuntimeError("channel is gone")

    _write(workspace, "src/main.py")

    task_tree.WorkspaceTree(workspace, _Exploding()).beat()  # must simply return


def test_a_beat_that_failed_to_publish_tries_again_rather_than_calling_it_delivered(workspace):
    """The digest records what was PUBLISHED, not what was read.

    Remembering a snapshot the publisher rejected would silence the tree until the workspace changed
    again — and a workspace that changes rarely is exactly the one whose single tree event matters.
    """
    from remote import task_tree

    class _FailsOnce:
        def __init__(self):
            self.published = []
            self._first = True

        def publish(self, kind, **fields):
            if self._first:
                self._first = False
                raise RuntimeError("dropped")
            self.published.append((kind, fields))

    _write(workspace, "src/main.py")
    publisher = _FailsOnce()
    tree = task_tree.WorkspaceTree(workspace, publisher)

    tree.beat()
    tree.beat()

    assert [kind for kind, _fields in publisher.published] == ["task.tree"]


def test_a_second_DIFFERENT_reason_is_still_said(workspace, capsys):
    """Quieting a repeated reason must not quiet a NEW one.

    A single latch shared by every failure site means a transient hiccup early on buys silence for
    the permanent breakage that follows it — and a task runs for up to an hour, so the operator's
    only signal would be a stale message about a problem that already cleared.
    """
    from remote import task_tree

    class _FailsThenFailsDifferently:
        def __init__(self):
            self.calls = 0

        def publish(self, *_args, **_fields):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("a transient hiccup")
            raise RuntimeError("PERMANENT auth failure")

    _write(workspace, "src/main.py")
    tree = task_tree.WorkspaceTree(workspace, _FailsThenFailsDifferently())

    for _ in range(4):
        tree.beat()

    err = capsys.readouterr().err
    assert "transient hiccup" in err
    assert "PERMANENT auth failure" in err
    assert err.count("PERMANENT") == 1, "the new reason must be said once, not on every beat"


def test_a_reason_that_cleared_and_came_back_is_said_again(workspace, capsys):
    """The latch is about repetition, not about having ever complained. A failure that recovers and
    then recurs an hour later is news again."""
    from remote import task_tree

    class _FailsWhenTold:
        def __init__(self):
            self.failing = True

        def publish(self, *_args, **_fields):
            if self.failing:
                raise RuntimeError("the channel is gone")
            return True

    _write(workspace, "src/main.py")
    publisher = _FailsWhenTold()
    tree = task_tree.WorkspaceTree(workspace, publisher)

    tree.beat()
    publisher.failing = False
    tree.beat()
    publisher.failing = True
    _write(workspace, "src/util.py")
    tree.beat()

    assert capsys.readouterr().err.count("the channel is gone") == 2


def test_a_snapshot_the_publisher_discarded_is_never_recorded_as_delivered(workspace):
    """The publisher returns whether it accepted the event, and this has to believe the answer.

    A latched-off publisher drops the event and returns normally. Treating "did not raise" as "landed"
    would advance the digest, and the snapshot would then never be re-sent — so a channel that
    recovers would resume with the tree permanently one revision stale.
    """
    from remote import task_tree

    class _AcceptsNothing:
        def __init__(self):
            self.calls = 0

        def publish(self, *_args, **_fields):
            self.calls += 1
            return False

    _write(workspace, "src/main.py")
    publisher = _AcceptsNothing()
    tree = task_tree.WorkspaceTree(workspace, publisher)

    tree.beat()
    tree.beat()
    tree.beat()

    assert publisher.calls == 3, "a refused snapshot must be offered again, not remembered as sent"


def test_the_heartbeats_snapshot_never_waits_for_a_busy_channel(workspace):
    """The beat runs on the renewal thread, so it asks the publisher not to block (ADR 0032 D-c)."""
    from remote import task_tree

    class _RecordsHowItWasAsked:
        def __init__(self):
            self.blocking = []

        def publish(self, _kind, *, blocking=True, **_fields):
            self.blocking.append(blocking)
            return True

    _write(workspace, "src/main.py")
    publisher = _RecordsHowItWasAsked()

    task_tree.WorkspaceTree(workspace, publisher).beat()

    assert publisher.blocking == [False]


def test_a_workspace_that_stays_broken_is_complained_about_once_not_every_beat(tmp_path, capsys):
    """A beat fires every 30s for the life of the task. A reason repeated on each one buries the
    provider's log in the same line and hides everything else happening on the box."""
    from remote import task_tree

    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    tree = task_tree.WorkspaceTree(plain, _RecordingPublisher())

    for _ in range(5):
        tree.beat()

    # Counted on the `[tasks]` prefix rather than on a word from the message: `tmp_path` is named
    # after this test, so anything the message and the path have in common counts twice.
    assert capsys.readouterr().err.count("[tasks]") == 1


def test_a_few_very_long_paths_are_capped_by_BYTES_not_by_COUNT(workspace):
    """The entry count alone does not bound anything a wire cares about.

    Five hundred paths is a small-looking number and, at filesystem-legal lengths, several hundred
    kilobytes. The byte budget is the bound that actually protects the event; the count is a
    readability bound sitting behind it.
    """
    from remote import task_events, task_tree

    deep = "/".join(f"directory-with-a-long-name-{n:03d}" for n in range(12))
    for n in range(200):
        _write(workspace, f"{deep}/file-with-a-fairly-long-name-{n:04d}.txt")

    snapshot = task_tree.snapshot(workspace)

    assert snapshot.truncated is True
    assert len(snapshot.paths) < task_tree.MAX_TREE_ENTRIES, (
        "the byte budget must bite before the entry count does")
    on_the_wire = json.dumps({"type": "task.tree", **snapshot.as_event()}).encode("utf-8")
    assert len(on_the_wire) <= task_events.MAX_EVENT_BYTES
