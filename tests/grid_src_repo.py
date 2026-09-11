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

FEATS_DIR_NAME = "autonomous-grid-feats"

#: The directory suffix the `new-grid-worktree` skill gives a feature's parallel-ticket root
#: (`<repo>-feats/<feat>_parallel/<ticket>`). Hand-duplicated with that skill's
#: `scripts/parallel-worktree.sh` (`PARALLEL_SUFFIX`), and kept in step by editing both: renamed on
#: one side alone, every ticket worktree still resolves its exact mirror and only the fallback to
#: the feature worktree is lost -- silently, as a skip.
PARALLEL_SUFFIX = "_parallel"


def _mirror_candidates(here: pathlib.Path, repo: str) -> list[pathlib.Path]:
    """Where a checkout of ``repo`` could sit beside the checkout at ``here``, best guess first.

    Split out of `_sibling_root` so the layouts can be pinned as data: this function is the whole
    of the derivation, and the derivation is what decides whether the lockstep suites run at all.

    Three layouts, and the fallbacks are ordered by how close they are to the code this checkout
    is actually working on:

    * a MAIN checkout (`<projects>/autonomous-grid`) looks for `<projects>/<repo>`;
    * a FEATURE worktree (`<projects>/autonomous-grid-feats/<slug>`) mirrors the slug into
      `<projects>/<repo>-feats/<slug>`, then falls back to the main checkout;
    * a PARALLEL TICKET worktree (`…-feats/<slug>_parallel/<ticket>`) mirrors the WHOLE path below
      the feats directory, so ticket `t12` reads ticket `t12`. Only then does it fall back to the
      feature worktree the ticket was cut from, and last to the main checkout. The exact mirror has
      to come first: two agents working two tickets have moved the same seam in two different
      directions, and reading the sibling's feature branch would compare against neither.

    The middle candidate is the one piece of the parallel layout this module knows about; a nested
    directory that is not `<something>_parallel` gets the exact mirror and the main checkout only,
    because inventing a checkout out of an arbitrary directory name is how a resolver starts
    answering confidently about the wrong tree.
    """
    for ancestor in here.parents:
        if ancestor.name != FEATS_DIR_NAME:
            continue
        projects = ancestor.parent
        rel = here.relative_to(ancestor)
        candidates = [projects / f"{repo}-feats" / rel]
        feature = rel.parts[0][: -len(PARALLEL_SUFFIX)]
        if len(rel.parts) > 1 and rel.parts[0].endswith(PARALLEL_SUFFIX) and feature:
            candidates.append(projects / f"{repo}-feats" / feature)
        candidates.append(projects / repo)
        return candidates
    return [here.parent / repo]


def _sibling_root(
    marker: pathlib.Path, repo: str, env_var: str, *, hint: str = ""
) -> pathlib.Path | None:
    """A sibling checkout of ``repo``, or ``None`` when this machine has none beside this worktree.

    Derived from THIS file's location rather than written out, so the checks run in whichever
    worktree they are checked out into instead of skipping everywhere but one. The convention is
    `<repo>-feats/<slug>` beside `<repo>`, so a worktree of `autonomous-grid` at
    `…/autonomous-grid-feats/<slug>` looks for `…/<repo>-feats/<slug>` first and falls back to the
    main `…/<repo>` checkout. `_mirror_candidates` holds the full rule, including the
    parallel-ticket layout, and `tests/test_sibling_root.py` pins it.

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
    for candidate in _mirror_candidates(here, repo):
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


def harness_root() -> pathlib.Path | None:
    """autonomous-harness' checkout — the AGENT HARNESS — or ``None`` when it is not beside this one.

    The fourth repository in the chain, and the only one that is not Python: `harness grid login`
    spawns THIS CLI as a child, so the seam between them is an **argv**, hand-duplicated exactly the
    way a wire constant is. `HARNESS_REPO` overrides the derivation.
    """
    return _sibling_root(pathlib.Path("cli") / "src", "autonomous-harness", "HARNESS_REPO")


def app_root() -> pathlib.Path | None:
    """autonomous-grid-app's checkout — the FLUTTER APP — or ``None`` when it is not beside this one.

    The fifth repository, and the only one that reaches the lockstep with **no half in this
    repository at all**: ADR 0042 D-l's refusal sentence runs grid-src → the app, and this checkout
    is in the path of neither. It is resolved here anyway because the pin that compares those two
    halves has to live somewhere, and this repository is the hub.

    ⚠️ The app deliberately has **no worktree** for the billing-activation feature, so the mirror
    candidate never exists and the derivation lands on the main checkout. That is the intended
    answer, not a fallback that went wrong: the app's half of this seam is one substring in one file
    and it does not move per feature branch. `GRID_APP_REPO` overrides the derivation.
    """
    return _sibling_root(pathlib.Path("lib"), "autonomous-grid-app", "GRID_APP_REPO")


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
