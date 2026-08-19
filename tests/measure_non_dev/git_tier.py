"""Measurements 1 and 2 — what a real repository costs, answered by real git.

Measurement 1 decides the provider's layout (issue 50, and issue 38's path change assumes it):
whether `$GIT_DIR/info/exclude` lives in the common directory, what the Nth worktree costs against
the Nth clone, and whether N worktrees of one object store can fetch at once. Measurement 2 decides
how long the auto-apply holds its per-project serialization (issues 41 and 43).

Everything here runs against a scratch bare clone taken with `--no-hardlinks`, so the operator's own
repository is opened read-only and its objects are never shared — `measure_git_plane.py`'s rule,
adopted rather than restated.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import gitrun
from .report import CONSISTENT, CONTRADICTS, MEASURED, NOT_STATED, Result, unavailable

#: `$GIT_DIR/info/exclude` is in the COMMON directory, so one file governs every worktree — the
#: reading ADR 0034 D-j's uniform `/.grid/` line assumes.
LOCUS_COMMON = "common"
#: A per-worktree `info/exclude` is honoured too, so a worktree can exclude something its siblings
#: do not. A different design, not a stronger version of the same one.
LOCUS_PER_WORKTREE_HONOURED = "per_worktree_honoured"

_M1_EXCLUDE = "1a-info-exclude-locus"
_M1_DISK = "1b-nth-worktree-vs-nth-clone"
_M1_FETCH = "1c-concurrent-fetches-one-object-store"
M2_CLEAN = "2a-merge-tree-clean"
M2_CONFLICTING = "2b-merge-tree-conflicting"

#: ADR 0034 D-c's claim, quoted so a contradiction names the line to amend rather than a memory of
#: it. Verified against `docs/adr/0034-…md` — the ADR states it twice, in D-c and in the
#: measurement table.
ADR_CLAIM_LAYOUT = (
    "ADR 0034 D-c: \"Three conversations of one member on the 581 MiB repository measured in 0033 "
    "issue 16a is ~12 GB of checkouts. One local clone per (project, member) with a `git worktree` "
    "per conversation shares the object store; whether that is viable is a measurement\"")

#: The share of the equivalent clones' cost above which "sharing the object store" has bought
#: nothing worth the layout's complexity — so D-c's premise fails and the ADR must be amended
#: rather than reinterpreted. A stated threshold, not a hidden one: the raw curves are in the
#: report beside it, so a reader who disagrees with the line can draw their own.
_SHARING_MUST_SAVE_BELOW = 0.80

#: The ~12 GB D-c asserts three conversations cost as separate checkouts. Checked as a NUMBER, not
#: paraphrased: the ADR uses it to justify the whole layout, so being wrong about it matters even
#: when the conclusion it supports survives.
_ADR_CLAIMED_BYTES = 12 * 1000 ** 3
#: How many conversations that figure is stated FOR. `--copies` is a free knob, and a verdict on a
#: three-conversation claim taken from a one-conversation run is not a comparison.
_ADR_CLAIMED_CONVERSATIONS = 3
#: How far under the claim a measurement may land before the claim is called wrong. Generous —
#: "~12 GB" is written as an approximation and 0033's repository has grown since it was measured —
#: so anything this flags is an error of magnitude rather than of rounding.
_CLAIM_TOLERANCE = 0.5


def decide_exclude_locus(*, common_took_effect: bool, per_worktree_took_effect: bool,
                         evidence: dict) -> Result:
    """The `info/exclude` answer, or a refusal to give one — from BOTH control rows.

    Separated from the git that produces the two booleans so that every combination is reachable
    from a test, including the ones a working git never produces. A decision only exercised by the
    happy path is a decision nobody has checked.

    The positive control is load-bearing and it is the one that can fail silently: if a pattern in
    the COMMON `info/exclude` is not honoured, this probe is not measuring exclusion at all — and
    the reading it would then produce is `common`, which is exactly the answer the design is hoping
    for. So a dead positive control withholds the answer regardless of what the negative row says.
    """
    if not common_took_effect:
        return unavailable(
            _M1_EXCLUDE,
            "the positive control did not fire: a pattern written to the COMMON info/exclude was "
            "not honoured inside the worktree, so this probe was not measuring exclusion and its "
            f"answer would have been meaningless. Evidence: {evidence}")
    locus = LOCUS_PER_WORKTREE_HONOURED if per_worktree_took_effect else LOCUS_COMMON
    return Result(name=_M1_EXCLUDE, status=MEASURED,
                  data={"locus": locus,
                        "common_took_effect": common_took_effect,
                        "per_worktree_took_effect": per_worktree_took_effect,
                        "evidence": evidence})


def seed_bare(source: Path, dest: Path) -> Path:
    """A scratch bare copy of `source`, sharing NOTHING with it.

    `--no-hardlinks` is the whole point: the default local clone hardlinks `objects/`, so a later
    `gc` or a `worktree prune` in the scratch repo could touch files the operator's own repository
    still points at. `measure_git_plane.py` made the same call for the same reason — a few seconds
    is not worth any chance of writing to somebody's real work.
    """
    if dest.exists():
        return dest
    gitrun.run("clone", "--bare", "--no-hardlinks", str(source), str(dest))
    return dest


def measure_worktree_vs_clone(seed: Path, work: Path, *, copies: int = 3) -> Result:
    """What the Nth conversation costs as a worktree, against the Nth as its own clone.

    D-c proposes one clone per `(project, member)` with a worktree per conversation. The number that
    decides it is not "a worktree is smaller" — it is what the **Nth** one ADDS, because a provider
    holding a team's conversations pays that increment repeatedly. So both sides are measured
    cumulatively and reported per N, and the totals are derived rather than the other way round.

    Both sides are built from the same seed with the same `--no-hardlinks` rule, so the comparison
    is like for like: a hardlinked clone would report a near-zero cost that a real provider — whose
    clone comes over the wire from the relay — could never have.
    """
    work.mkdir(parents=True, exist_ok=True)
    branches = _branches(seed)
    if not branches:
        return unavailable(_M1_DISK, f"{seed} advertises no branches to check out")

    skipped = 0
    worktree_root = work / "worktrees"
    worktree_root.mkdir(exist_ok=True)
    worktrees, previous = [], gitrun.tree_bytes(seed).total_bytes
    for n in range(1, copies + 1):
        path = worktree_root / f"conversation-{n}"
        gitrun.run("-C", str(seed), "worktree", "add", "--quiet", "--detach", str(path),
                   branches[0])
        # The seed is re-measured each round, not just the new directory: `worktree add` writes
        # administrative files INTO the common dir, and a measurement that only weighed the new
        # checkout would miss a cost that grows with exactly the thing being counted.
        store, trees = gitrun.tree_bytes(seed), gitrun.tree_bytes(worktree_root)
        skipped += store.skipped_files + trees.skipped_files
        total = store.total_bytes + trees.total_bytes
        worktrees.append({"n": n, "total_bytes": total, "added_bytes": total - previous})
        previous = total

    clone_root = work / "clones"
    clone_root.mkdir(exist_ok=True)
    clones, previous = [], 0
    for n in range(1, copies + 1):
        path = clone_root / f"conversation-{n}"
        gitrun.run("clone", "--quiet", "--no-hardlinks", "--branch", branches[0],
                   str(seed), str(path))
        weighed = gitrun.tree_bytes(clone_root)
        skipped += weighed.skipped_files
        total = weighed.total_bytes
        clones.append({"n": n, "total_bytes": total, "added_bytes": total - previous})
        previous = total

    # The comparison D-c actually proposes: ONE shared object store plus N worktrees, against N
    # independent clones. The seed is inside the worktree side on purpose — a provider pays for it
    # once and it is part of that layout's cost, not a free baseline to subtract.
    worktree_total = worktrees[-1]["total_bytes"]
    clone_total = clones[-1]["total_bytes"]
    # BOTH sides, symmetrically. The clone guard existed alone, so a permissions fault that made
    # every `stat` under the worktree tree fail would have reported `worktree_share_of_clones: 0.0`
    # and `sharing_pays: true` — the design's hoped-for answer, produced by a broken measurement.
    if not clone_total or not worktree_total:
        return unavailable(
            _M1_DISK,
            f"a side of the comparison weighed zero bytes ({worktree_total} worktree, "
            f"{clone_total} clone, {skipped} file(s) could not be weighed), so no ratio taken from "
            f"it means anything")
    verdict = decide_layout_verdict(worktree_total_bytes=worktree_total,
                                    clone_total_bytes=clone_total, copies=copies)
    return Result(
        name=_M1_DISK, status=MEASURED,
        adr_claim=verdict.adr_claim, adr_verdict=verdict.adr_verdict,
        data={**verdict.data, "branch": branches[0],
              "worktrees": worktrees, "clones": clones,
              "worktree_added_bytes": worktrees[-1]["added_bytes"],
              "clone_added_bytes": clones[-1]["added_bytes"],
              # Zero on a healthy run. Non-zero means some of this comparison was not weighed, and
              # a reader has to know that before citing the ratio.
              "files_that_could_not_be_weighed": skipped})


@dataclass(frozen=True)
class Contention:
    """What a contended git call collided on, as far as git's own words say.

    Both fields `None` means "this output does not describe a lock collision" — NOT "no collision
    happened". The distinction matters because this is a race: a worker can fail for a reason that
    has nothing to do with contention, and reporting that as contention would answer the
    measurement's question wrongly in the direction the design is hoping for.
    """

    lock_path: str | None = None
    ref: str | None = None


# git's own wording on 2.54.0. Two patterns rather than one, because the two facts appear on the
# same line but only one of them is always present: `unable to create '<path>.lock'` shows up for
# object and index locks too, where no ref is named at all.
_LOCK_PATH = re.compile(r"[Uu]nable to create '([^']+\.lock)'")
_LOCK_REF = re.compile(r"cannot lock ref '([^']+)'")


def parse_contention(stderr: str) -> Contention:
    """The lock a fetch collided on, picked out of git's stderr — or nothing, said plainly."""
    path = _LOCK_PATH.search(stderr)
    ref = _LOCK_REF.search(stderr)
    return Contention(lock_path=path.group(1) if path else None,
                      ref=ref.group(1) if ref else None)


def measure_concurrent_fetches(seed: Path, work: Path, *, workers: int = 4) -> Result:
    """Can N worktrees of ONE object store fetch at the same time, and if not, on exactly what?

    D-c's layout has every conversation of a member sharing one object store, and every turn begins
    with a fetch. If those serialize — or worse, fail — the layout costs a provider its concurrency
    at the moment it is most needed. Nothing had tried it.

    Each worker fetches a DIFFERENT branch into the same store, so the collision is real: identical
    fetches can be satisfied without writing anything, which would measure an idle repository.
    """
    work.mkdir(parents=True, exist_ok=True)
    upstream = seed_bare(seed, work / "upstream.git")
    branches = _branches(upstream)
    if not branches:
        return unavailable(_M1_FETCH, f"{upstream} advertises no branches to fetch")

    shared = work / "shared.git"
    gitrun.run("clone", "--bare", "--no-hardlinks", str(upstream), str(shared))
    trees = []
    for n in range(workers):
        path = work / f"wt{n}"
        gitrun.run("-C", str(shared), "worktree", "add", "--quiet", "--detach", str(path))
        trees.append(path)

    def _fetch(index: int) -> dict:
        # A branch each, cycling when there are fewer branches than workers, and a distinct LOCAL
        # ref per worker so two workers never contend on the same destination by accident — the
        # contention under measurement is the shared object store's, not a self-inflicted one.
        branch = branches[index % len(branches)]
        got = gitrun.run("fetch", "--quiet", str(upstream),
                         f"+refs/heads/{branch}:refs/measure/{index}",
                         cwd=trees[index], check=False, timeout=600)
        collision = parse_contention(got.stderr)
        return {"worker": index, "branch": branch, "returncode": got.returncode,
                "seconds": round(got.seconds, 3), "ok": got.ok,
                "stderr": got.stderr.strip(),
                "lock_path": collision.lock_path, "locked_ref": collision.ref}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(_fetch, range(workers)))

    failed = [row for row in rows if not row["ok"]]
    return Result(
        name=_M1_FETCH, status=MEASURED,
        data={"workers": rows, "succeeded": len(rows) - len(failed), "failed": len(failed),
              # Named separately from `failed`: a fetch can fail for reasons that are not
              # contention, and only the contended ones say anything about the layout.
              "lock_collisions": [row for row in failed if row["lock_path"]],
              "slowest_seconds": max(row["seconds"] for row in rows),
              "fastest_seconds": min(row["seconds"] for row in rows)})


def measure_merge_tree(seed: Path, *, repeats: int = 5,
                       max_pairs: int = 200) -> list[Result]:
    """How long `merge-tree --write-tree` takes on this repository, clean and conflicting.

    This is the number ADR 0034 D-d's apply holds a per-project lock for. D-d already moved the
    apply OUT of the settle request because the cost was unknown and unbounded; what it does not
    say is how long the lock is then held, and that is what decides whether the apply becomes the
    grid's bottleneck.

    Two results, because a repository can have a clean pair and no conflicting one. Each half is
    measured or withheld on its own.

    ⚠️ `merge-tree --write-tree` WRITES an unreferenced tree object every time, including on the
    conflicting run. That is git's design, not a leak — `git gc` collects them — and it is stated
    here because a reader watching the scratch repository grow during a `--repeats 5` run against a
    large repository would otherwise have to work out why.
    """
    branches = _branches(seed)
    pairs = [(a, b) for i, a in enumerate(branches) for b in branches[i + 1:]][:max_pairs]
    considered = len([1 for i, _ in enumerate(branches) for _ in branches[i + 1:]])
    if considered > max_pairs:
        # Never a silent cap: a truncated search that found no conflicting pair is a different fact
        # from a repository that has none, and only this line tells them apart.
        print(f"  note: {considered} branch pairs available, only the first {max_pairs} tried",
              flush=True)

    found: dict[bool, tuple[str, str]] = {}
    source = "branches"
    _search(seed, pairs, found)
    if len(found) < 2:
        # Branch HEADS were the wrong population, and running this against a real repository is
        # what showed it: 34,159 commits and 73 authors, and **two** local branches — so one pair,
        # which merged cleanly, and the conflicting half came back unavailable on a repository full
        # of conflicts. What the relay's apply merges is a turn's branch against `main`, i.e.
        # arbitrary commits by different people, so that is the population to search.
        source = "commits" if not found else "branches+commits"
        _search(seed, _commit_pairs(seed, max_pairs), found)

    results = []
    for conflicted, name in ((False, M2_CLEAN), (True, M2_CONFLICTING)):
        pair = found.get(conflicted)
        if pair is None:
            results.append(unavailable(
                name,
                f"no {'conflicting' if conflicted else 'cleanly merging'} pair was found in "
                f"{seed.name} among {len(pairs)} pairs of its {len(branches)} branch heads, nor "
                f"among up to {max_pairs} pairs of commits by different authors"))
            continue
        results.append(_time_merge(seed, name, pair, conflicted=conflicted, repeats=repeats,
                                   pair_source=source))
    return results


def _search(seed: Path, pairs, found: dict) -> None:
    """Fill `found` with one cleanly-merging pair and one conflicting one, stopping at both."""
    for left, right in pairs:
        if len(found) == 2:
            return
        attempt = gitrun.run("-C", str(seed), "merge-tree", "--write-tree", left, right,
                             check=False, timeout=1800)
        if attempt.returncode not in (0, 1):
            # Not a merge outcome — unrelated histories, most often. Skipping it is right; treating
            # it as "clean" would time a merge that never happened.
            continue
        found.setdefault(attempt.returncode == 1, (left, right))


def _commit_pairs(seed: Path, cap: int) -> list[tuple[str, str]]:
    """Pairs of commits by DIFFERENT authors, near each other in time.

    The estimate tier's own pairing, reused rather than reimplemented: measurement 6 replays
    exactly this population, so a second copy here would let the two drift into timing one thing
    and estimating another.
    """
    from . import estimate_tier

    commits = estimate_tier._history(seed, 5_000)
    pairs, _ = estimate_tier._pairs(commits, estimate_tier.DEFAULT_WINDOW_SECONDS, cap)
    return [(left.oid, right.oid) for left, right in pairs]


def decide_layout_verdict(*, worktree_total_bytes: int, clone_total_bytes: int,
                          copies: int) -> Result:
    """How the measured layout stands against ADR 0034 D-c — BOTH halves of what it claims.

    D-c asserts two things and they can fail independently: that sharing an object store is worth
    doing, and that the thing being avoided costs **~12 GB**. The first version of this checked
    only the ratio, and would have reported `consistent` beside a measured 3.13 GB — absorbing a 4×
    error in the very figure the layout is justified by, which is exactly what issue 35's eighth
    criterion forbids.

    The conclusion can survive while its magnitude does not, and that is the useful thing to say:
    an overstated cost does not make worktrees wrong, it makes the ARGUMENT for them weaker than
    the ADR's prose implies.
    """
    share = (worktree_total_bytes / clone_total_bytes) if clone_total_bytes else None
    sharing_pays = share is not None and share < _SHARING_MUST_SAVE_BELOW
    within_reach = clone_total_bytes >= _ADR_CLAIMED_BYTES * _CLAIM_TOLERANCE
    comparable = copies == _ADR_CLAIMED_CONVERSATIONS
    # The ratio is a real measurement at any N; the 12 GB is stated for THREE conversations, so only
    # at three is a verdict on it like-for-like. Withheld rather than scaled: the ADR's figure is a
    # measured-once claim about a specific case, and dividing it by three would invent a
    # per-conversation number nobody asserted.
    verdict = NOT_STATED if not comparable else (
        CONSISTENT if (sharing_pays and within_reach) else CONTRADICTS)
    return Result(
        name=_M1_DISK, status=MEASURED, adr_verdict=verdict,
        adr_claim=ADR_CLAIM_LAYOUT if comparable else "",
        data={"copies": copies,
              "worktree_total_bytes": worktree_total_bytes,
              "clone_total_bytes": clone_total_bytes,
              "worktree_share_of_clones": round(share, 4) if share is not None else None,
              "sharing_must_save_below": _SHARING_MUST_SAVE_BELOW,
              "sharing_pays": sharing_pays,
              "adr_claimed_bytes": _ADR_CLAIMED_BYTES,
              "adr_claim_applies_at_copies": _ADR_CLAIMED_CONVERSATIONS,
              "adr_figure_within_reach": within_reach if comparable else None,
              "adr_verdict_withheld_reason": "" if comparable else (
                  f"ADR 0034 D-c's figure is stated for {_ADR_CLAIMED_CONVERSATIONS} conversations "
                  f"and this run modelled {copies}, so no verdict on it is like-for-like")})


def _time_merge(seed: Path, name: str, pair: tuple[str, str], *, conflicted: bool,
                repeats: int, pair_source: str = "branches") -> Result:
    """One pair, merged `repeats` times, with every sample kept.

    Every sample rather than an average: the first run is cold and the rest are not, and an average
    hides which of those a reader is looking at. The apply pays the COLD cost on a relay that has
    just restarted and the warm one thereafter, so both are the answer.
    """
    left, right = pair
    samples, conflicts_seen = [], set()
    for _ in range(repeats):
        attempt = gitrun.run("-C", str(seed), "merge-tree", "--write-tree", left, right,
                             check=False, timeout=1800)
        if attempt.returncode not in (0, 1):
            return unavailable(name, f"merge-tree on {left}…{right} exited {attempt.returncode}: "
                                     f"{attempt.stderr.strip()}")
        samples.append(round(attempt.seconds, 4))
        conflicts_seen.add(attempt.returncode == 1)
    if conflicts_seen != {conflicted}:
        # The same pair merging cleanly once and conflicting the next time would mean the repository
        # changed underneath the measurement, and every number here would be about two inputs.
        return unavailable(name, f"merge-tree on {left}…{right} was not consistent across "
                                 f"{repeats} runs: conflicted={sorted(conflicts_seen)}")
    # The returncode is READ, not assumed. On failure `stdout` is usually empty and this would fall
    # back to "(unknown)" — indistinguishable from git genuinely answering nothing, with the stderr
    # discarded. Contained (it is context beside the timings, which are unaffected), but "could not
    # ask" and "asked and got nothing" are the distinction this whole harness is built on.
    counted = gitrun.run("-C", str(seed), "rev-list", "--count", "--left-right",
                         f"{left}...{right}", check=False, timeout=600)
    ahead = counted.stdout.strip() if counted.ok else (
        f"(rev-list exited {counted.returncode}: {counted.stderr.strip()[:120]})")
    return Result(
        name=name, status=MEASURED,
        # The pair and its divergence sit beside the timings because the cost depends on the input:
        # a number with no input is not citable, and two runs on different pairs are not comparable.
        data={"left": left, "right": right, "divergence_left_right": ahead or "(unknown)",
              # Which population the pair came from. A branch-head timing and a commit-pair timing
              # are not the same measurement, and a reader has to be able to tell them apart.
              "pair_source": pair_source,
              "conflicted": conflicted, "repeats": repeats, "seconds": samples,
              "cold_seconds": samples[0], "min_seconds": min(samples), "max_seconds": max(samples),
              "median_seconds": sorted(samples)[len(samples) // 2]})


def _branches(seed: Path) -> list[str]:
    listed = gitrun.run("-C", str(seed), "for-each-ref", "--format=%(refname:short)",
                        "refs/heads/").stdout.split()
    # `main` first when it exists: it is the ref a provider actually checks out, and the biggest
    # tree, so measuring a stray tiny topic branch would understate every number here.
    return sorted(listed, key=lambda name: (name != "main", name))


def _is_ignored(worktree: Path, name: str) -> tuple[bool, str]:
    """Whether git excludes `name` in this worktree, and the rule it named.

    `check-ignore -v` rather than reading `git status`: it answers about the path directly and
    reports WHICH file and line decided, which is the evidence a withheld answer has to carry. Exit
    1 means "not ignored" and is an ordinary answer here, so the call cannot be checked — which is
    also why `literal_pathspecs=False` is not optional. With it on, this command exits 128 (see
    `gitrun.env`), and a 128 is indistinguishable from a 1 to a caller that only reads `ok`.

    Exit codes are therefore matched EXACTLY rather than by truthiness: 0 is ignored, 1 is not, and
    anything else is the probe failing, which must not be reported as an answer.
    """
    probe = gitrun.run("check-ignore", "-v", "--no-index", name,
                       cwd=worktree, check=False, literal_pathspecs=False, timeout=120)
    detail = (probe.stdout or probe.stderr).strip()
    if probe.returncode not in (0, 1):
        raise RuntimeError(f"git check-ignore on {name} exited {probe.returncode}: {detail}")
    return probe.returncode == 0, detail


def measure_exclude_locus(seed: Path, work: Path) -> Result:
    """Is `$GIT_DIR/info/exclude` common to every worktree? Measured, with both controls.

    ADR 0034 D-j collapses the provider's exclude handling to "one uniform `/.grid/` line", which is
    only the same design in both readings if the file is common-directory. Nothing had measured it.

    Both control files are written BEFORE either is probed, and they use different patterns, so the
    two rows cannot mask each other — a single file rewritten between probes would make the second
    reading depend on the first having been cleaned up.
    """
    work.mkdir(parents=True, exist_ok=True)
    worktree = work / "wt1"
    gitrun.run("-C", str(seed), "worktree", "add", "--quiet", "--detach", str(worktree))

    common_dir = Path(gitrun.run("rev-parse", "--path-format=absolute", "--git-common-dir",
                                 cwd=worktree).stdout.strip())
    git_dir = Path(gitrun.run("rev-parse", "--path-format=absolute", "--git-dir",
                              cwd=worktree).stdout.strip())
    if common_dir == git_dir:
        # Not a linked worktree at all, so there is no per-worktree location to distinguish and the
        # question cannot be asked. Its own fact — the alternative is answering `common` about a
        # setup that has only one place to look.
        return unavailable(
            _M1_EXCLUDE,
            f"the probe's worktree was not a LINKED worktree: --git-dir and --git-common-dir are "
            f"both {git_dir}, so there is no per-worktree location for the negative control")

    for directory, pattern in ((common_dir, "positive-control-*.txt"),
                               (git_dir, "negative-control-*.txt")):
        (directory / "info").mkdir(parents=True, exist_ok=True)
        (directory / "info" / "exclude").write_text(pattern + "\n", encoding="utf-8")
    (worktree / "positive-control-1.txt").write_text("x\n", encoding="utf-8")
    (worktree / "negative-control-1.txt").write_text("x\n", encoding="utf-8")

    common_took_effect, common_rule = _is_ignored(worktree, "positive-control-1.txt")
    per_worktree_took_effect, per_worktree_rule = _is_ignored(worktree, "negative-control-1.txt")

    return decide_exclude_locus(
        common_took_effect=common_took_effect,
        per_worktree_took_effect=per_worktree_took_effect,
        evidence={
            "common_dir": str(common_dir), "worktree_git_dir": str(git_dir),
            "positive_control_rule": common_rule or "(not ignored)",
            "negative_control_rule": per_worktree_rule or "(not ignored)",
        })
