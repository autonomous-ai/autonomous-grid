"""Where grid-src is, for the cross-repo lockstep suites that read its source.

Every seam between these repositories is wire-level with no import path across it, so the shared
constants are hand-duplicated and kept in step by tests that PARSE the other repository. Those
tests need to find it, and finding it is the part that quietly decides whether they run at all:
`tests/test_task_lease.py` names one worktree by absolute path, which is why its cross-repo checks
skip in every other worktree on the machine.

One copy, here, because two hand-written path derivations drift exactly like two hand-written
constants — and a resolver that drifts does not fail, it skips.
"""
from __future__ import annotations

import os
import pathlib


def grid_src_root() -> pathlib.Path | None:
    """grid-src's checkout, or ``None`` when this machine does not have one beside this worktree.

    Derived from THIS file's location rather than written out, so the checks run in whichever
    worktree they are checked out into instead of skipping everywhere but one. The convention is
    `<repo>-feats/<slug>` beside `<repo>`, so a worktree of `autonomous-grid` at
    `…/autonomous-grid-feats/<slug>` looks for `…/grid-src-feats/<slug>` first and falls back to
    the main `…/grid-src` checkout.

    `GRID_SRC_REPO` — the cross-repo E2E's own override — wins over both, so a machine that lays
    the repositories out differently can still run these. It is **validated**, and a bad one raises
    rather than skips; see the comment on that branch for why the difference matters here.
    """
    override = os.environ.get("GRID_SRC_REPO")
    if override:
        # ⚠️ **Validated, and a bad one RAISES rather than skips.** `tests/e2e_cross_repo/
        # _harness.py` defaults this same variable to one hardcoded worktree, so on this machine the
        # ordinary way to acquire it is to export it for a different suite entirely — and an
        # override that points somewhere else is a person's configuration mistake, not evidence that
        # grid-src is absent. Skipping there would turn every pin in both lockstep suites off with
        # a message saying the repository is not here while it sits right beside this one.
        root = pathlib.Path(override)
        if not (root / "grid_cli" / "private_server").is_dir():
            raise AssertionError(
                f"GRID_SRC_REPO={override!r} does not hold grid_cli/private_server, so it is not a "
                f"grid-src checkout. Unset it to fall back to the worktree beside this one — the "
                f"cross-repo E2E harness defaults it to a DIFFERENT worktree, and left exported it "
                f"silently decides what every lockstep check reads")
        return root

    here = pathlib.Path(__file__).resolve().parent.parent  # the autonomous-grid checkout
    projects = here.parent
    candidates = []
    if projects.name == "autonomous-grid-feats":
        candidates.append(projects.parent / "grid-src-feats" / here.name)
        candidates.append(projects.parent / "grid-src")
    else:
        candidates.append(projects / "grid-src")
    for candidate in candidates:
        if (candidate / "grid_cli" / "private_server").is_dir():
            return candidate
    return None


def grid_src_private_server() -> pathlib.Path | None:
    """grid-src's `private_server` package — the relay's own source tree."""
    root = grid_src_root()
    return None if root is None else root / "grid_cli" / "private_server"
