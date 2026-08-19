"""The harness's one entry point: parse a tier, run it, write one report.

Split from `tests/measure_non_dev_design.py` — which is a four-line shim — so the argument surface
and the tier gate are reachable from a test without spawning a subprocess. The gate is the part
worth testing: issue 35's first draft was un-runnable precisely because one "runs from a clean
checkout" promise was made across measurements that need a 792 MiB repository and a live Claude
subscription.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

from . import agent_tier, estimate_tier, git_tier, report
from .report import CannotRun

#: The three tiers, exactly as issue 35's table names them in its `Tier` column.
TIER_GIT = "git"
TIER_AGENT = "agent"
TIER_ESTIMATE = "estimate"
TIERS = (TIER_GIT, TIER_AGENT, TIER_ESTIMATE)

#: Tiers that cannot run without a real repository to measure against.
_TIERS_NEEDING_A_REPO = (TIER_GIT, TIER_ESTIMATE)

#: Exit code for "you did not give me what this tier needs". Distinct from 1 so a script can tell a
#: harness that refused to start from one that ran and found something.
EXIT_CANNOT_RUN = 2
#: The run completed and at least one number CONTRADICTS ADR 0034. A non-zero exit because issue
#: 35's criterion is that a contradiction be "called out rather than absorbed", and a harness that
#: exits 0 having found one has absorbed it — a CI job or a shell loop would report it as fine.
EXIT_CONTRADICTED = 3

#: Names the temp scratch so an interrupted run's several gigabytes are findable and removable.
_SCRATCH_PREFIX = "measure-0034-"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="measure_non_dev_design", description=__doc__)
    parser.add_argument("--tier", required=True, choices=TIERS,
                        help="which tier to run; they have different prerequisites")
    parser.add_argument("--repo", default=None,
                        help="a REAL repository to measure against (read-only; cloned into scratch)")
    parser.add_argument("--report", default=None, type=Path,
                        help="where to write the report (default: ./measure-0034-<tier>.json — "
                             "outside the scratch dir, which is deleted)")
    parser.add_argument("--scratch", default=None, type=Path,
                        help="where to work (default: a temp dir, removed afterwards)")
    parser.add_argument("--copies", type=int, default=3,
                        help="conversations to model in the worktree-vs-clone comparison "
                             "(default 3, ADR 0034 D-c's own example)")
    parser.add_argument("--workers", type=int, default=4,
                        help="concurrent fetches into one shared object store (default 4)")
    parser.add_argument("--repeats", type=int, default=5,
                        help="merge-tree samples per pair (default 5; one is not a number)")
    parser.add_argument("--window", type=int, default=estimate_tier.DEFAULT_WINDOW_SECONDS,
                        help="seconds two commits must fall within to be replayed as concurrent "
                             "(default 900 — ADR 0034's 'designed for' row)")
    parser.add_argument("--pairs", type=int, default=500,
                        help="most pairs to replay for the tier-3 estimate (default 500; a cap "
                             "that truncates is reported, never silent)")
    parser.add_argument("--turns", type=int, default=50,
                        help="turns in the measured conversation (default 50, issue 35's figure)")
    parser.add_argument("--fill-turns", type=int, default=25,
                        help="further turns spent trying to force a compaction (default 25)")
    parser.add_argument("--model", default=agent_tier.DEFAULT_MODEL,
                        help="the transcript is under measurement, not the model's reasoning")
    parser.add_argument("--autocompact", type=int, default=agent_tier.MIN_AUTOCOMPACT_TOKENS,
                        help="auto-compact window (100000 is the documented minimum, and is what "
                             "makes a compaction reachable inside an affordable conversation)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)

    binary = None
    try:
        source = _require_repo(args) if args.tier in _TIERS_NEEDING_A_REPO else None
        if args.tier == TIER_AGENT:
            binary = agent_tier.require_claude()
    except CannotRun as exc:
        # The ONE place a prerequisite refusal becomes a sentence and an exit code, so no tier can
        # invent its own spelling of "I did not run". Raised BEFORE any scratch directory exists,
        # so a refused run leaves nothing behind to mistake for a partial result.
        print(str(exc), file=sys.stderr)
        return EXIT_CANNOT_RUN

    scratch = Path(args.scratch) if args.scratch else Path(tempfile.mkdtemp(prefix=_SCRATCH_PREFIX))
    scratch.mkdir(parents=True, exist_ok=True)
    # NEVER inside `scratch` by default. That is where it used to go, and the `finally` below then
    # deleted it on the very invocation this harness documents — leaving the numbers as terminal
    # scrollback and nothing else. `measure_git_plane.py`'s note is the rule: a number nobody can
    # reproduce is an anecdote, and one nobody can retrieve is worse.
    destination = Path(args.report) if args.report else Path.cwd() / f"measure-0034-{args.tier}.json"
    try:
        results = _run_tier(args, source, scratch, binary)
        body = report.assemble(tier=args.tier, source=source, results=results)
        report.write(destination, body)
    finally:
        # A `--scratch` the operator named is theirs to keep; a temp dir this run invented is not.
        # A 792 MiB seed plus three clones is several gigabytes, and leaving that behind on every
        # run is how a measurement harness becomes something nobody runs.
        if args.scratch is None:
            shutil.rmtree(scratch, ignore_errors=True)
    return EXIT_CONTRADICTED if body["contradictions"] else 0


def _run_tier(args, source: Path | None, scratch: Path,
              binary: str | None) -> list[report.Result]:
    """Dispatch, kept separate so each tier's measurements are listed in one readable place."""
    if args.tier == TIER_GIT:
        seed = git_tier.seed_bare(source, scratch / "seed.git")
        return [
            git_tier.measure_exclude_locus(seed, scratch / "exclude"),
            git_tier.measure_worktree_vs_clone(seed, scratch / "disk", copies=args.copies),
            git_tier.measure_concurrent_fetches(seed, scratch / "fetch", workers=args.workers),
            *git_tier.measure_merge_tree(seed, repeats=args.repeats),
        ]
    if args.tier == TIER_ESTIMATE:
        seed = git_tier.seed_bare(source, scratch / "seed.git")
        return [estimate_tier.measure_tier3_rate(
            seed, window_seconds=args.window, max_pairs=args.pairs)]
    if args.tier == TIER_AGENT:
        return agent_tier.run_agent_tier(
            scratch, binary=binary, turns=args.turns, model=args.model,
            autocompact=args.autocompact, fill_turns=args.fill_turns)
    # Unreachable while `--tier` is an argparse choice, and it RAISES rather than returning an
    # empty list: a tier that silently measured nothing would write a clean-looking report.
    raise CannotRun(f"the {args.tier} tier is not wired up")


def _require_repo(args) -> Path:
    """The repository to measure, checked for being one BEFORE anything is cloned.

    `git clone` on a non-repository answers a message about a remote, which reads as a network
    fault rather than a typo in the path the operator typed.
    """
    if not args.repo:
        raise CannotRun(f"the {args.tier} tier measures a real repository and you named none: "
                        f"pass --repo <path>")
    source = Path(args.repo).expanduser().resolve()
    if not (source / ".git").exists() and not (source / "HEAD").exists():
        raise CannotRun(f"{source} is not a git repository")
    return source
