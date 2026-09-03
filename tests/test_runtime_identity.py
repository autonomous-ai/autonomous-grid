from __future__ import annotations

from types import SimpleNamespace

from shared import runtime_identity


def _result(returncode: int, stdout: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout)


def test_build_revision_is_preferred_and_normalized(monkeypatch):
    runtime_identity.grid_runtime_identity.cache_clear()
    monkeypatch.setenv("GRID_BUILD_REVISION", "ABCDEF1234567")
    monkeypatch.setattr(runtime_identity, "_git", lambda *_args: (_ for _ in ()).throw(
        AssertionError("Git must not be consulted for a stamped build")))

    answer = runtime_identity.grid_runtime_identity()

    assert answer == {
        "version": runtime_identity.__version__,
        "revision": "abcdef1234567",
        "dirty": False,
    }
    runtime_identity.grid_runtime_identity.cache_clear()


def test_source_revision_reports_tracked_runtime_changes(monkeypatch):
    runtime_identity.grid_runtime_identity.cache_clear()
    monkeypatch.delenv("GRID_BUILD_REVISION", raising=False)
    calls = []

    def fake_git(_root, *args):
        calls.append(args)
        if args[:2] == ("rev-parse", "--verify"):
            return _result(0, "4E5DCC7A3FA929B7\n")
        return _result(0, " M remote/tasks.py\n")

    monkeypatch.setattr(runtime_identity, "_git", fake_git)

    assert runtime_identity.grid_runtime_identity() == {
        "version": runtime_identity.__version__,
        "revision": "4e5dcc7a3fa929b7",
        "dirty": True,
    }
    assert calls[1][-4:] == runtime_identity._TRACKED_RUNTIME_PATHS
    runtime_identity.grid_runtime_identity.cache_clear()


def test_unknown_or_invalid_revision_is_omitted(monkeypatch):
    runtime_identity.grid_runtime_identity.cache_clear()
    monkeypatch.setenv("GRID_BUILD_REVISION", "not a commit; rm -rf anything")
    assert runtime_identity.grid_runtime_identity() == {
        "version": runtime_identity.__version__}
    runtime_identity.grid_runtime_identity.cache_clear()

