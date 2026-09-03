#!/usr/bin/env python3
"""Measure what ADR 0034 rests on (issue 35) — the entry point.

ADR 0034 (*A task is a conversation, and nobody merges by hand*) rests on six numbers and none had
been taken. This tracker's rule is that a value reached by reasoning ABOUT git is a value that has
not been measured; the record already holds `add -A` clearing an unmerged index, a tree mode git
reads as `040000` and writes as `40000`, and `git commit` succeeding without an identity after a
comment claimed for two slices that it fails.

Not a test, and deliberately not collected as one — pytest's default `python_files` is `test_*.py`,
so the `measure_` prefix keeps a multi-gigabyte, subscription-spending run out of every ordinary
suite. `measure_git_plane.py` in grid-src made the same call for issue 16a, and its numbers are
cited in `CLAUDE.md` to this day. The harness's own DECISIONS are tested, from
`tests/test_measure_non_dev_design.py`, which runs in the ordinary suite in seconds.

⚠️ **THREE TIERS, and they have different prerequisites.** Issue 35's first draft was un-runnable
because one "runs from a clean checkout" promise was made across all of them.

    # 1 · the git tier — measurements 1 and 2. Needs a large repository, read-only.
    #     Use the one 0033 issue 16a measured, so the disk figures are comparable.
    .venv/bin/python tests/measure_non_dev_design.py --tier git \
        --repo ~/Projects/large-website-repo

    # 2 · the estimate tier — measurement 6. Needs a repository with a real TEAM in its history.
    .venv/bin/python tests/measure_non_dev_design.py --tier estimate \
        --repo ~/Projects/large-website-repo

    # 3 · the agent tier — measurements 3, 4 and 5. Needs a logged-in Claude Code, and SPENDS a
    #     real subscription.
    .venv/bin/python tests/measure_non_dev_design.py --tier agent

`--repo` is opened READ-ONLY: it is cloned once into a scratch bare repo with `--no-hardlinks`, so
the source repository's object store is never shared, let alone written.

Exit codes: 0 measured, 2 a prerequisite was missing and nothing ran, 3 a number CONTRADICTS
ADR 0034 — which issue 35 requires be called out rather than absorbed, and an exit of 0 would
absorb it.
"""
from __future__ import annotations

import sys
from pathlib import Path

# The repo root, so `tests.measure_non_dev` and the provider's own `remote.task_agent` both import
# the way they do under pytest (`pythonpath = ["."]`). Same single path entry, so a module cannot
# resolve differently here than it does in the suite.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.measure_non_dev.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
