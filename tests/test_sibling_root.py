"""Where `grid_src_repo` looks for a sibling checkout, for every worktree layout on this machine.

`_sibling_root` decides whether the cross-repo lockstep suites RUN. It fails by returning `None`,
which reads as "that repository is not on this machine" and skips — so a layout it does not
understand turns every pin off without a single red test. That is why the candidate list is pinned
here as data rather than exercised only through the suites that consume it.

The parallel-ticket layout (`<repo>-feats/<feat>_parallel/<ticket>`, cut by the `new-grid-worktree`
skill's `parallel` mode) is the case that motivated the multi-level mirror: under the old
single-level derivation `projects.name` was `<feat>_parallel`, not `autonomous-grid-feats`, so every
ticket worktree fell through to a candidate that cannot exist and skipped.
"""
from __future__ import annotations

import pathlib

from tests.grid_src_repo import PARALLEL_SUFFIX, _mirror_candidates


def _c(here: str, repo: str = "grid-src") -> list[str]:
    return [str(p) for p in _mirror_candidates(pathlib.Path(here), repo)]


def test_main_checkout_looks_beside_itself() -> None:
    # Arrange / Act
    got = _c("/P/autonomous-grid")

    # Assert
    assert got == ["/P/grid-src"]


def test_feature_worktree_mirrors_the_slug_then_falls_back_to_the_main_checkout() -> None:
    # The pre-existing behaviour, unchanged: `<repo>-feats/<slug>` first, main checkout second.
    assert _c("/P/autonomous-grid-feats/billing-activation") == [
        "/P/grid-src-feats/billing-activation",
        "/P/grid-src",
    ]


def test_parallel_ticket_worktree_mirrors_the_whole_path_below_the_feats_dir() -> None:
    # The exact mirror comes FIRST: a ticket worktree must read its own ticket's sibling, not the
    # feature branch it was cut from, or a lockstep test compares against code the other agent has
    # already moved past.
    assert _c("/P/autonomous-grid-feats/billing-activation_parallel/t12") == [
        "/P/grid-src-feats/billing-activation_parallel/t12",
        "/P/grid-src-feats/billing-activation",
        "/P/grid-src",
    ]


def test_parallel_fallback_is_the_feature_worktree_not_the_parallel_directory() -> None:
    # `<feat>_parallel/` holds worktrees; it is not one. The fallback strips the suffix.
    got = _c("/P/autonomous-grid-feats/billing-activation_parallel/t12")
    assert f"/P/grid-src-feats/billing-activation{PARALLEL_SUFFIX}" not in got


def test_a_nested_directory_that_is_not_parallel_gets_no_feature_fallback() -> None:
    # Only the `_parallel` convention earns the middle candidate; anything else would be inventing
    # a checkout out of a directory name.
    assert _c("/P/autonomous-grid-feats/a/b") == ["/P/grid-src-feats/a/b", "/P/grid-src"]


def test_the_repo_name_is_carried_into_every_candidate() -> None:
    assert _c("/P/autonomous-grid-feats/billing-activation_parallel/t12", "grid-apis") == [
        "/P/grid-apis-feats/billing-activation_parallel/t12",
        "/P/grid-apis-feats/billing-activation",
        "/P/grid-apis",
    ]
