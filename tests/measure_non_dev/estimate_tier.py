"""Measurement 6 — the real tier-3 rate, which can only be ESTIMATED, and says so.

ADR 0034's `1 − e^(−RT)` table gives the probability that `main` MOVES during a merge. It does not
give how often two people touch the same LINES, and only that number says what auto-apply costs
(issues 41 and 42). The difference is the whole point: the first is arithmetic about arrival rates,
the second is a fact about how a team works, and no amount of reasoning produces it.

So this is a replay, not a measurement, and every number it emits is labelled as one. Issue 35 is
explicit about why — "an estimate presented as a measurement is worse than no number" — and this is
the number in the set most likely to be lifted out of the report and quoted alone.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import gitrun
from .report import MEASURED, Result, unavailable

M6_TIER3_RATE = "6-tier-3-rate"

#: The window two commits must fall inside to be replayed as if they had been concurrent turns.
#: ADR 0034's "designed for" row — seven members, three conversations, a turn every 15 minutes.
DEFAULT_WINDOW_SECONDS = 15 * 60

#: What this estimate is NOT, stated beside it every time. Not a docstring: these travel INTO the
#: report, because the report is what gets read and the docstring is what does not.
LIMITS = (
    "a commit timestamp is not a turn timestamp — a rebase, a squash or an amend rewrites it, so "
    "co-occurrence here is inferred from times that may never have been when the work happened",
    "a linear history has no true concurrency, so every pair is a SYNTHETIC co-occurrence: this "
    "replays what would have conflicted had the two been concurrent, not what did",
    "one repository's team habits are not another's — file layout, review culture and how finely "
    "work is split all move this number, and none of them is a property of the grid",
    "git-conflict is a SUPERSET of agent-unresolvable: tier 3 is what merge-tree cannot do, and "
    "some of that an agent resolves trivially, so this OVER-counts the paid work auto-apply causes",
    "pairs are capped and the cap is reported; a capped run's rate is over the pairs tried, never "
    "over the repository — they are sampled at an even stride across the whole eligible range, so "
    "the sample spans the history rather than one end of it, but it is still a sample",
)


def estimated(*, name: str, data: dict, method: str, limits: list[str] | tuple[str, ...]) -> Result:
    """An estimate, which may not be recorded without saying how it was reached and what it is not.

    Required keyword arguments AND a non-empty check, because the two failures are different: the
    first stops a caller forgetting, the second stops one passing `[]` to satisfy the signature.
    Refusing outright rather than defaulting — a default limits list would be this module's opinion
    printed as though the caller had asserted it.
    """
    if not method.strip():
        raise ValueError(f"{name}: an estimate must state the method that produced it")
    if not [line for line in limits if line.strip()]:
        raise ValueError(
            f"{name}: an estimate must state its limits beside its number — an estimate presented "
            f"as a measurement is worse than no number")
    return Result(name=name, status=MEASURED,
                  data={**data, "estimate": True, "method": method, "limits": list(limits)})


@dataclass(frozen=True)
class _Commit:
    oid: str
    author: str
    when: int


def _history(seed: Path, limit: int) -> list[_Commit]:
    """Every commit's oid, author email and COMMITTER time, newest first.

    Committer time rather than author time: author time survives a rebase and therefore describes
    when somebody typed, while committer time describes when the object entered this history. For
    "were these two in flight at once" the second is the closer question — and neither is right,
    which is the first entry in `LIMITS`.
    """
    listed = gitrun.run("-C", str(seed), "log", "--all", "--no-merges",
                        f"--max-count={limit}", "--format=%H%x1f%ae%x1f%ct").stdout
    commits = []
    for line in listed.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 3 or not parts[2].isdigit():
            continue
        commits.append(_Commit(oid=parts[0], author=parts[1].strip().lower(), when=int(parts[2])))
    return commits


def stride_sample(candidates: list, cap: int) -> list:
    """At most `cap` items, spread EVENLY across `candidates` rather than taken from one end.

    This is not a refinement — the first version took the first `cap`, and the candidates are time
    ordered. Measured on the real run: 29,553 eligible pairs spanning 2024-10-24 to 2026-04-17, and
    every one of the 500 replayed fell in the first three weeks. The rate described that team in
    late 2024 while the report said "500 of 29,553", which reads like a sample and was a slice.

    Deterministic, and that is a requirement rather than a convenience: a committed harness whose
    number cannot be re-taken is the anecdote it exists to replace, so no randomness.
    """
    if cap <= 0 or len(candidates) <= cap:
        return list(candidates)
    step = len(candidates) / cap
    return [candidates[int(index * step)] for index in range(cap)]


def _pairs(commits: list[_Commit], window: int, cap: int) -> tuple[list[tuple[_Commit, _Commit]],
                                                                   int]:
    """Pairs by DIFFERENT authors within `window` seconds, evenly sampled, and how many existed.

    Different authors is the load-bearing filter: one person's two commits a minute apart are
    sequential work, and counting them would measure how often somebody edits their own file twice.

    Every eligible pair is collected BEFORE the cap applies, because a cap that stops the search
    cannot sample what it never saw. The inner `break` bounds the work: candidates are time
    ordered, so once a pair exceeds the window every later one does too.
    """
    ordered = sorted(commits, key=lambda c: c.when)
    eligible = []
    for index, left in enumerate(ordered):
        for right in ordered[index + 1:]:
            if right.when - left.when > window:
                break
            if right.author != left.author:
                eligible.append((left, right))
    return stride_sample(eligible, cap), len(eligible)


def measure_tier3_rate(seed: Path, *, window_seconds: int = DEFAULT_WINDOW_SECONDS,
                       max_pairs: int = 500, max_commits: int = 20_000) -> Result:
    """How often two near-simultaneous commits by different people would not merge cleanly.

    The replay is exactly what the relay's apply would do: `merge-tree --write-tree` against the
    pair's own merge base. Same command, same exit codes, so the rate is about the mechanism that
    will really run rather than a model of it.
    """
    commits = _history(seed, max_commits)
    if len(commits) < 2:
        return unavailable(M6_TIER3_RATE,
                           f"{seed.name} holds {len(commits)} non-merge commits; a rate needs pairs")
    pairs, considered = _pairs(commits, window_seconds, max_pairs)
    if not pairs:
        return unavailable(
            M6_TIER3_RATE,
            f"no two commits by DIFFERENT authors fall within {window_seconds}s of each other in "
            f"the {len(commits)} commits examined — this history has no concurrency to replay, so "
            f"a rate taken from it would describe nothing")
    if considered > len(pairs):
        print(f"  note: {considered} eligible pairs, only {len(pairs)} replayed (--pairs)",
              flush=True)

    conflicted = unmergeable = 0
    for left, right in pairs:
        attempt = gitrun.run("-C", str(seed), "merge-tree", "--write-tree",
                             left.oid, right.oid, check=False, timeout=1800)
        if attempt.returncode == 1:
            conflicted += 1
        elif attempt.returncode != 0:
            # Unrelated histories, most often. Counted and reported rather than dropped: a run
            # where half the pairs never merged at all has a denominator that means something
            # different, and silently shrinking it would raise the rate for free.
            unmergeable += 1

    merged = len(pairs) - unmergeable
    return estimated(
        name=M6_TIER3_RATE,
        method=(f"replayed {len(pairs)} pairs of commits by different authors whose committer "
                f"times fall within {window_seconds}s, merging each pair with the same "
                f"`git merge-tree --write-tree` the relay's apply uses, against its own merge base"),
        limits=LIMITS,
        data={"pairs_replayed": len(pairs), "pairs_eligible": considered,
              "pairs_capped_at": max_pairs, "commits_examined": len(commits),
              "window_seconds": window_seconds,
              "conflicted": conflicted, "merged_cleanly": merged - conflicted,
              "unmergeable_pairs": unmergeable,
              "tier_3_rate": round(conflicted / merged, 4) if merged else None,
              "authors": len({c.author for c in commits})})
