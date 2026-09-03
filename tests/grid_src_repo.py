"""Where the sibling repositories are, for the cross-repo lockstep suites that read their source.

Every seam between these repositories is wire-level with no import path across it, so the shared
constants are hand-duplicated and kept in step by tests that PARSE the other repository. Those
tests need to find it, and finding it is the part that quietly decides whether they run at all:
`tests/test_task_lease.py` names one worktree by absolute path, which is why its cross-repo checks
skip in every other worktree on the machine.

One copy, here, because two hand-written path derivations drift exactly like two hand-written
constants — and a resolver that drifts does not fail, it skips. That is also why **grid-apis** is
resolved here rather than in the one suite that reads it (`test_os_grid_type_lockstep.py`): the
module keeps its `grid_src_repo` name for the sake of the two suites already importing it, but the
rule it exists for is about path derivations, not about which repository they point at.
"""
from __future__ import annotations

import os
import pathlib


def _sibling_root(
    marker: pathlib.Path, repo: str, env_var: str, *, hint: str = ""
) -> pathlib.Path | None:
    """A sibling checkout of ``repo``, or ``None`` when this machine has none beside this worktree.

    Derived from THIS file's location rather than written out, so the checks run in whichever
    worktree they are checked out into instead of skipping everywhere but one. The convention is
    `<repo>-feats/<slug>` beside `<repo>`, so a worktree of `autonomous-grid` at
    `…/autonomous-grid-feats/<slug>` looks for `…/<repo>-feats/<slug>` first and falls back to the
    main `…/<repo>` checkout.

    ``marker`` is a directory that must exist under a candidate for it to count as that repository —
    the difference between "found it" and "found a directory with the right name".

    ``env_var`` wins over both, and is **validated**: a bad one raises rather than skips, because an
    override pointing somewhere else is a person's configuration mistake, not evidence the
    repository is absent. Skipping there would turn every pin off with a message blaming the wrong
    thing.
    """
    override = os.environ.get(env_var)
    if override:
        root = pathlib.Path(override)
        if not (root / marker).is_dir():
            raise AssertionError(
                f"{env_var}={override!r} does not hold {marker}, so it is not a {repo} checkout. "
                f"Unset it to fall back to the worktree beside this one"
                + (f" — {hint}" if hint else ""))
        return root

    here = pathlib.Path(__file__).resolve().parent.parent  # the autonomous-grid checkout
    projects = here.parent
    candidates = []
    if projects.name == "autonomous-grid-feats":
        candidates.append(projects.parent / f"{repo}-feats" / here.name)
        candidates.append(projects.parent / repo)
    else:
        candidates.append(projects / repo)
    for candidate in candidates:
        if (candidate / marker).is_dir():
            return candidate
    return None


def grid_apis_root() -> pathlib.Path | None:
    """grid-apis' checkout — the CONTROL PLANE — or ``None`` when it is not beside this worktree.

    A third repository joined the lockstep with ADR 0039's `os-community` literal: grid-apis
    `grid_networks/store.py` holds a copy of it, alongside grid-src's two. `GRID_APIS_REPO`
    overrides the derivation.
    """
    return _sibling_root(pathlib.Path("grid_networks"), "grid-apis", "GRID_APIS_REPO")


def grid_src_root() -> pathlib.Path | None:
    """grid-src's checkout, or ``None`` when this machine does not have one beside this worktree.

    Derived from THIS file's location rather than written out, so the checks run in whichever
    worktree they are checked out into instead of skipping everywhere but one. The convention is
    `<repo>-feats/<slug>` beside `<repo>`, so a worktree of `autonomous-grid` at
    `…/autonomous-grid-feats/<slug>` looks for `…/grid-src-feats/<slug>` first and falls back to
    the main `…/grid-src` checkout.

    ⚠️ `GRID_SRC_REPO` — the cross-repo E2E's own override — wins over both, so a machine that lays
    the repositories out differently can still run these. It is **validated**, and a bad one raises
    rather than skips: `tests/e2e_cross_repo/_harness.py` defaults this same variable to one
    hardcoded worktree, so on this machine the ordinary way to acquire it is to export it for a
    different suite entirely — and an override that points somewhere else is a person's
    configuration mistake, not evidence that grid-src is absent. Skipping there would turn every pin
    in both lockstep suites off with a message saying the repository is not here while it sits right
    beside this one.
    """
    return _sibling_root(
        pathlib.Path("grid_cli") / "private_server",
        "grid-src",
        "GRID_SRC_REPO",
        hint=(
            "the cross-repo E2E harness defaults it to a DIFFERENT worktree, and left exported it "
            "silently decides what every lockstep check reads"
        ),
    )


def grid_src_private_server() -> pathlib.Path | None:
    """grid-src's `private_server` package — the relay's own source tree."""
    root = grid_src_root()
    return None if root is None else root / "grid_cli" / "private_server"
