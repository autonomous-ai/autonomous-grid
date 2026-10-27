"""A grid joined by URL gets a valid directory on Windows (`shared.paths`).

`grid join http://<host>:<port> …` — the README quickstart — makes the URL itself the grid id
(`local/config.select_grid`), and the run tree used it as a path component: `run\\engines\\http:\\…`.
POSIX accepts the `:`; Windows refuses the whole path with WinError 123, so the quickstart's join died
there. Found on a real Windows 11 PC. POSIX keeps its paths byte for byte — existing records live there.
"""
from __future__ import annotations

import pytest

from shared import paths

URL_ID = "http://127.0.0.1:18090"


@pytest.fixture
def grid_home(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    return tmp_path


def test_windows_url_grid_id_is_one_valid_directory_name(grid_home, monkeypatch):
    monkeypatch.setattr(paths, "_IS_WINDOWS", True)
    for directory, root in ((paths.engines_dir(URL_ID), paths.run_dir() / "engines"),
                            (paths.grid_dir(URL_ID), paths.grids_dir())):
        assert directory.parent == root, "one component, not a nested http:/host tree"
        assert directory.name == "http%3A%2F%2F127.0.0.1%3A18090"
        assert not set(directory.name) & set('<>:"/\\|?*')


def test_windows_encoding_keeps_distinct_ids_distinct(monkeypatch):
    monkeypatch.setattr(paths, "_IS_WINDOWS", True)
    assert paths._dir_name("a:b") != paths._dir_name("a%3Ab")


def test_windows_plain_grid_id_is_unchanged(grid_home, monkeypatch):
    monkeypatch.setattr(paths, "_IS_WINDOWS", True)
    assert paths.engines_dir("ag-home-f359e812").name == "ag-home-f359e812"


def test_posix_paths_are_unchanged(grid_home, monkeypatch):
    monkeypatch.setattr(paths, "_IS_WINDOWS", False)
    assert paths.engines_dir(URL_ID) == paths.run_dir() / "engines" / URL_ID
    assert paths.grid_dir(URL_ID) == paths.grids_dir() / URL_ID
