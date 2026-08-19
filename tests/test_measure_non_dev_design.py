"""What the measurement harness DECIDES, as opposed to what it measures (ADR 0034, issue 35).

A measurement's number belongs to the machine it ran on and cannot be asserted here. Its
**decisions** can, and they are the whole reason the harness is committed rather than run once:

  * a tier that cannot run says so by name, instead of failing obscurely;
  * a measurement that could not run is its OWN fact, never a zero and never an absent key;
  * an answer whose control did not fire is withheld — a harness whose control silently did not run
    is the failure this tracker names by name;
  * a `--resume` that exits 0 having forgotten the conversation is not a resume.

So this file tests the harness the way the harness tests git: at the seam, with a negative control
beside every guarded assertion.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests.measure_non_dev import agent_tier, cli, estimate_tier, git_tier, gitrun, report

_VERSION = re.compile(r"\d+\.\d+")


def _git(*args: str, cwd: Path, author: str | None = None) -> str:
    """Real git, with an identity and no user configuration — the harness's own discipline.

    `author` names a DIFFERENT person for the commits that need one: the tier-3 replay pairs on
    author email, so a fixture that committed everything as one identity could never produce a
    pair, and the test would pass by measuring nothing.
    """
    who = author or "t"
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": "/nonexistent",
        "GIT_AUTHOR_NAME": who, "GIT_AUTHOR_EMAIL": f"{who}@invalid",
        "GIT_COMMITTER_NAME": who, "GIT_COMMITTER_EMAIL": f"{who}@invalid",
    }
    return subprocess.run(["git", *args], cwd=str(cwd), env=env, text=True,
                          capture_output=True, check=True, timeout=120).stdout


@pytest.fixture
def tiny_repo(tmp_path: Path) -> Path:
    """A REAL repository, small enough for the suite.

    The git tier's plumbing does not care how big a repository is — only its NUMBERS do. So the
    suite drives the whole tier against real git, real worktrees and a real `merge-tree`, and the
    792 MiB repository is reserved for producing the figures that get cited. A tier exercised only
    by the citable run is a tier whose decisions nothing checks.
    """
    source = tmp_path / "source"
    source.mkdir()
    _git("init", "--quiet", "--initial-branch=main", ".", cwd=source)
    (source / "shared.txt").write_text("base\n")
    (source / "untouched.txt").write_text("still here\n")
    _git("add", "-A", cwd=source)
    _git("commit", "--quiet", "-m", "base", cwd=source)

    # Two branches off `main` that BOTH edit `shared.txt` on the same line — the conflicting pair
    # measurement 2 needs — plus one that edits a different file, for the clean pair.
    _git("checkout", "--quiet", "-b", "theirs", cwd=source)
    (source / "shared.txt").write_text("theirs\n")
    _git("commit", "--quiet", "-am", "theirs", cwd=source)
    _git("checkout", "--quiet", "main", cwd=source)
    _git("checkout", "--quiet", "-b", "clean", cwd=source)
    (source / "untouched.txt").write_text("changed elsewhere\n")
    _git("commit", "--quiet", "-am", "clean", cwd=source)
    _git("checkout", "--quiet", "main", cwd=source)
    (source / "shared.txt").write_text("ours\n")
    _git("commit", "--quiet", "-am", "ours", cwd=source)
    return source


class TestEveryReportSaysWhatItMeasuredAgainst:
    """Acceptance criterion 1: "prints the git version and the Claude Code version it measured
    against". A number outlives the tools that produced it, so a report that does not name them is
    a number nobody can re-take."""

    def test_the_report_names_both_tools_even_on_a_tier_that_used_one(
            self, tiny_repo, tmp_path):
        # Arrange
        report_path = tmp_path / "report.json"

        # Act — the git tier touches git and never spawns an agent.
        cli.main(["--tier", "git", "--repo", str(tiny_repo), "--report", str(report_path)])

        # Assert — both are named anyway, because "which Claude Code" is part of what a later
        # reader needs even when this tier did not use one.
        versions = json.loads(report_path.read_text())["versions"]
        assert _VERSION.search(versions["git"]), versions
        assert "claude_code" in versions, versions


class TestAMeasurementThatCouldNotRunIsItsOwnFact:
    """`Pushed.unchecked`'s rule, stated for a harness: "verified" and "unexamined" must not be the
    same observation. The type is where it is enforced, because a measurement that forgot is one
    that reports a plausible zero and nobody ever looks again."""

    def test_an_unavailable_result_cannot_carry_a_number(self):
        # Arrange / Act / Assert — the combination is refused OUTRIGHT rather than dropped, because
        # a silently-dropped number is exactly what a reader would go looking for.
        with pytest.raises(ValueError):
            report.Result(name="m", status=report.UNAVAILABLE, reason="the pair never appeared",
                          data={"seconds": 0.0})

    def test_an_unavailable_result_cannot_omit_its_reason(self):
        with pytest.raises(ValueError):
            report.Result(name="m", status=report.UNAVAILABLE, reason="")

    def test_an_unavailable_result_cannot_also_claim_a_verdict_on_the_adr(self):
        """Review finding, verified: this was ACCEPTED, and `assemble` then listed the same name in
        BOTH `contradictions` — the section lifted to the top as the most-read finding, and what
        drives exit code 3 — and `unavailable`, carrying zero data because the unavailable
        invariant forbids any. A reader is told the ADR is contradicted by a measurement that
        explicitly did not happen."""
        with pytest.raises(ValueError):
            report.Result(name="m", status=report.UNAVAILABLE, reason="could not run",
                          adr_verdict=report.CONTRADICTS, adr_claim="ADR says X")

    def test_a_measured_result_cannot_carry_a_reason_that_would_be_dropped(self):
        """`as_json` copies `reason` only for an unavailable result, so one set here vanishes from
        the report in silence. The class's docstring claims the combinations are refused outright
        "the only version of this rule that cannot be forgotten by a caller" — so a gap in it is a
        gap in the contract, not a nicety."""
        with pytest.raises(ValueError):
            report.Result(name="m", status=report.MEASURED, data={"n": 1},
                          reason="a note that would silently vanish")


class TestANumberThatContradictsTheAdrIsCalledOut:
    """Acceptance criterion 8: "any number that contradicts ADR 0034 is called out rather than
    absorbed". One entry among six that happens to read `contradicts` IS absorbed — so the report
    carries the list at the top, where a reader cannot finish the file without seeing it."""

    def test_a_contradiction_is_named_at_the_top_of_the_report(self):
        # Arrange
        results = [
            report.Result(name="1-worktree", status=report.MEASURED, data={"mib": 3.0},
                          adr_claim="~12 GB of checkouts for three conversations",
                          adr_verdict=report.CONTRADICTS),
            report.Result(name="2-merge-tree", status=report.MEASURED, data={"seconds": 0.1}),
        ]

        # Act
        body = report.assemble(tier="git", source=None, results=results)

        # Assert
        assert body["contradictions"] == ["1-worktree"]

    def test_a_run_that_contradicts_nothing_says_so_with_an_empty_list(self):
        """The positive row. Without it, a `contradictions` key that stopped being written at all
        would read exactly like a clean run."""
        # Arrange
        results = [report.Result(name="2-merge-tree", status=report.MEASURED, data={"s": 0.1})]

        # Act
        body = report.assemble(tier="git", source=None, results=results)

        # Assert
        assert body["contradictions"] == []

    def test_a_contradiction_cannot_be_recorded_without_quoting_the_claim(self):
        """Otherwise "this contradicts the ADR" is unactionable: nobody can find the line to amend."""
        with pytest.raises(ValueError):
            report.Result(name="1-worktree", status=report.MEASURED, data={"mib": 3.0},
                          adr_verdict=report.CONTRADICTS)


class TestTheExcludeAnswerIsWithheldUnlessBothControlsBehaved:
    """Issue 35 asks measurement 1 to answer the `info/exclude` question "with a negative control".
    A control that silently did not run is this tracker's named failure — a harness reporting a
    confident answer about a probe that was measuring nothing. So the answer is a function of BOTH
    rows, and the function is separated from the git that produces them precisely so the
    disagreement cases are reachable by a test."""

    def test_common_is_answered_only_when_the_negative_control_did_not_fire(self):
        # Arrange / Act — the expected shape: the common file is honoured, the per-worktree one is
        # not. Asserted rather than assumed, because that is the whole measurement.
        result = git_tier.decide_exclude_locus(
            common_took_effect=True, per_worktree_took_effect=False, evidence={})

        # Assert
        assert result.status == report.MEASURED
        assert result.data["locus"] == git_tier.LOCUS_COMMON

    def test_a_per_worktree_exclude_that_fires_is_a_different_answer_not_the_same_one(self):
        result = git_tier.decide_exclude_locus(
            common_took_effect=True, per_worktree_took_effect=True, evidence={})

        assert result.status == report.MEASURED
        assert result.data["locus"] == git_tier.LOCUS_PER_WORKTREE_HONOURED

    def test_the_answer_is_withheld_when_the_positive_control_did_not_fire(self):
        """The row that matters. A common-directory exclude that is NOT honoured means the probe is
        not measuring exclusion at all — and the reading it would otherwise produce is
        `common`, the very answer the design is hoping for."""
        # Arrange / Act
        result = git_tier.decide_exclude_locus(
            common_took_effect=False, per_worktree_took_effect=False, evidence={"raw": "…"})

        # Assert
        assert result.status == report.UNAVAILABLE
        assert "control" in result.reason, result.reason

    def test_the_answer_is_withheld_even_when_the_per_worktree_row_looks_conclusive(self):
        """The subtle one: positive dead, negative alive. Nothing about that combination supports
        an answer, and the reading it superficially suggests is the opposite of the safe one."""
        result = git_tier.decide_exclude_locus(
            common_took_effect=False, per_worktree_took_effect=True, evidence={})

        assert result.status == report.UNAVAILABLE


class TestTheExcludeProbeAgainstRealGit:
    """The positive row for the whole probe, and the one that produces the actual answer. Feeding
    the decision function fabricated booleans proves the decision; only real git proves the two
    booleans mean what they are named."""

    def test_the_probe_reaches_an_answer_on_real_git(self, tiny_repo, tmp_path):
        # Arrange
        seed = git_tier.seed_bare(tiny_repo, tmp_path / "seed.git")

        # Act
        result = git_tier.measure_exclude_locus(seed, tmp_path / "work")

        # Assert — WHICH answer is the measurement's business, not the suite's; that it reached one
        # at all, with its controls intact, is what a test can hold.
        assert result.status == report.MEASURED, result.reason
        assert result.data["locus"] in (git_tier.LOCUS_COMMON,
                                        git_tier.LOCUS_PER_WORKTREE_HONOURED)
        assert result.data["common_took_effect"] is True


class TestTheNthWorktreeAgainstTheNthClone:
    """Issue 35: "reports the Nth worktree's disk cost against the Nth clone". WHICH is cheaper is
    the measurement's answer and belongs in the report, not in an assertion — on a tiny repository
    a worktree's checkout can easily outweigh a tiny object store, and a suite that asserted the
    792 MiB repository's conclusion would fail for being right about the wrong input."""

    def test_the_adrs_absolute_figure_is_checked_and_not_only_the_ratio(self):
        """ADR 0034 D-c asserts a NUMBER — "~12 GB of checkouts" for three conversations — and the
        first version of this measurement only checked whether sharing saved anything. It would
        have reported `consistent` beside a measured 3.13 GB, absorbing a 4× error in the very
        figure the layout is justified by. Both halves of the claim get checked."""
        # Arrange / Act — a measured total far below what the ADR asserts.
        verdict = git_tier.decide_layout_verdict(
            worktree_total_bytes=1_736_000_000, clone_total_bytes=3_282_000_000, copies=3)

        # Assert
        assert verdict.adr_verdict == report.CONTRADICTS
        assert verdict.adr_claim, "a contradiction must quote the claim it contradicts"

    def test_a_file_that_could_not_be_weighed_is_counted_and_surfaced(self, tmp_path):
        """Review finding (LOW), and it is the harness breaking its own rule: `tree_bytes` skipped
        any file it could not `stat`, with no count anywhere. A race is harmless and both sides are
        walked alike — but a SYSTEMATIC cause (permissions, path length) would undercount one side
        with nothing in the report to reveal it, which is "unexamined" reported as "verified"."""
        # Arrange — a directory the walk cannot stat into.
        (tmp_path / "readable.txt").write_text("12345")
        blocked = tmp_path / "blocked"
        blocked.mkdir()
        (blocked / "hidden.txt").write_text("xxxxxxxxxx")
        blocked.chmod(0o000)
        try:
            # Act
            size = gitrun.tree_bytes(tmp_path)

            # Assert — the readable byte is counted, and the unreadable one is REPORTED, not lost.
            assert size.total_bytes == 5
            assert size.skipped_files >= 1
        finally:
            blocked.chmod(0o755)

    def test_a_clean_walk_reports_nothing_skipped(self):
        """The positive row — otherwise a counter that stopped incrementing would look like a
        clean walk forever."""
        size = gitrun.tree_bytes(Path(__file__).resolve().parent / "measure_non_dev")

        assert size.total_bytes > 0
        assert size.skipped_files == 0

    def test_the_adr_verdict_is_withheld_when_the_run_did_not_model_three_conversations(self):
        """Review finding, verified: `--copies 1` measured a single clone (~1.04 GB) against a
        figure ADR 0034 D-c states for THREE conversations, and answered `contradicts` — which
        drives exit code 3 and the top of the report, sending somebody to amend an ADR over a
        comparison that was never like-for-like.

        The RATIO stays meaningful at any N, so the measurement survives; only the absolute claim
        is three-specific, so only the verdict is withheld."""
        verdict = git_tier.decide_layout_verdict(
            worktree_total_bytes=1_094_000_000, clone_total_bytes=1_094_000_000, copies=1)

        assert verdict.adr_verdict == report.NOT_STATED
        assert verdict.data["worktree_share_of_clones"] is not None, \
            "the ratio is still a real measurement and must survive"

    def test_a_measured_cost_matching_the_adr_is_consistent(self):
        """The positive row: the same function must be able to answer `consistent`, or the check
        is a constant dressed as a comparison."""
        verdict = git_tier.decide_layout_verdict(
            worktree_total_bytes=4_000_000_000, clone_total_bytes=12_000_000_000, copies=3)

        assert verdict.adr_verdict == report.CONSISTENT

    def test_both_curves_are_recorded_per_n_not_as_a_total(self, tiny_repo, tmp_path):
        # Arrange
        seed = git_tier.seed_bare(tiny_repo, tmp_path / "seed.git")

        # Act
        result = git_tier.measure_worktree_vs_clone(seed, tmp_path / "work", copies=3)

        # Assert — per-N, because the question is what the Nth one adds. A total cannot answer it,
        # and a total is what a linear-cost assumption would silently produce.
        assert result.status == report.MEASURED, result.reason
        assert len(result.data["worktrees"]) == 3
        assert len(result.data["clones"]) == 3
        assert [row["n"] for row in result.data["worktrees"]] == [1, 2, 3]
        assert all(row["added_bytes"] >= 0 for row in result.data["clones"])


class TestConcurrentFetchesIntoOneObjectStore:
    """Issue 35: "runs N concurrent fetches into N worktrees of one object store and reports what
    fails, and on exactly what". "On exactly what" is the criterion — `contention: true` would be
    useless to whoever has to decide whether the provider serializes its fetches."""

    def test_a_lock_failure_names_the_lock_file_and_the_exit_code(self):
        """Parsed from git's real wording. Kept a pure function of the text so the unhappy path is
        reachable: a contended fetch is a race, and a test that could only observe it by winning
        one would pass on the machines where it never happens."""
        # Arrange — git 2.54.0's actual message when two fetches collide on a ref lock.
        stderr = ("error: cannot lock ref 'refs/remotes/origin/main': Unable to create "
                  "'/tmp/seed.git/refs/remotes/origin/main.lock': File exists.\n"
                  "\nAnother git process seems to be running in this repository.\n")

        # Act
        failure = git_tier.parse_contention(stderr)

        # Assert
        assert failure.lock_path == "/tmp/seed.git/refs/remotes/origin/main.lock"
        assert failure.ref == "refs/remotes/origin/main"

    def test_output_with_no_lock_in_it_is_not_reported_as_a_lock_failure(self):
        """The negative row. Without it, a parser that returned a truthy object for anything would
        turn every unrelated fetch error into "lock contention" — a wrong diagnosis of the exact
        question this measurement exists to answer."""
        failure = git_tier.parse_contention("fatal: could not read Username for 'https://x': \n")

        assert failure.lock_path is None
        assert failure.ref is None

    def test_the_probe_records_every_worker_including_the_ones_that_succeeded(
            self, tiny_repo, tmp_path):
        """A measurement that only recorded failures could not tell "nothing failed" from "nothing
        ran", which is the same conflation the unavailable rule exists to prevent."""
        # Arrange
        seed = git_tier.seed_bare(tiny_repo, tmp_path / "seed.git")

        # Act
        result = git_tier.measure_concurrent_fetches(seed, tmp_path / "work", workers=4)

        # Assert
        assert result.status == report.MEASURED, result.reason
        assert len(result.data["workers"]) == 4
        assert result.data["failed"] + result.data["succeeded"] == 4


class TestMergeTreeWallTime:
    """Measurement 2 — how long the auto-apply holds its per-project serialization (issues 41, 43).
    ADR 0034 D-d moved the apply out of the settle request precisely because this number was
    unknown and unbounded; nothing had taken it."""

    def test_both_a_clean_and_a_conflicting_merge_are_timed(self, tiny_repo, tmp_path):
        # Arrange — `tiny_repo` is built with both pairs on purpose: `clean` touches a different
        # file, `theirs` touches the same line of `shared.txt` that `main` does.
        seed = git_tier.seed_bare(tiny_repo, tmp_path / "seed.git")

        # Act — TWO results, not one with two halves: a repository can easily have a clean pair and
        # no conflicting one, and `Result` forbids an unavailable outcome that carries numbers. One
        # combined result would have to choose between lying about the missing half and dragging
        # the measured half down with it.
        results = {r.name: r for r in git_tier.measure_merge_tree(seed, repeats=3)}

        # Assert
        clean = results[git_tier.M2_CLEAN]
        conflicting = results[git_tier.M2_CONFLICTING]
        assert clean.status == report.MEASURED, clean.reason
        assert conflicting.status == report.MEASURED, conflicting.reason
        assert clean.data["conflicted"] is False
        assert conflicting.data["conflicted"] is True
        # Repeats, because one sample on a warm cache is not a number.
        assert len(clean.data["seconds"]) == 3

    def test_a_conflicting_pair_is_found_among_COMMITS_when_branch_heads_offer_none(self, tmp_path):
        """Found by running it against the real 792 MiB repository: 34,159 commits, 73 authors —
        and **2 local branches**, so one branch pair, which merged cleanly. The conflicting half
        came back unavailable on a repository full of conflicts.

        Branch heads were the wrong population from the start. What the relay's apply merges is a
        TURN's branch against `main` — arbitrary commits by different people — so that is what has
        to be timed."""
        # Arrange — one branch, and a conflict reachable only between two commits on it.
        source = tmp_path / "linear"
        source.mkdir()
        _git("init", "--quiet", "--initial-branch=main", ".", cwd=source)
        (source / "f.txt").write_text("base\n")
        _git("add", "-A", cwd=source)
        _git("commit", "--quiet", "-m", "base", cwd=source)
        for who, text in (("alice", "alice\n"), ("bob", "bob\n")):
            _git("checkout", "--quiet", "-B", f"tmp-{who}", "main", cwd=source)
            (source / "f.txt").write_text(text)
            _git("commit", "--quiet", "-am", who, cwd=source, author=who)
        # ONE branch head, both conflicting commits still reachable from it — a merge is what makes
        # that possible, and it is also the shape a real repository has: `main` reaches everybody's
        # work while carrying a single head. `--strategy-option=ours` only settles the merge; the
        # two PARENTS still conflict with each other, which is the pair being looked for.
        _git("checkout", "--quiet", "-B", "main", "tmp-alice", cwd=source)
        _git("merge", "--quiet", "--no-ff", "--strategy-option=ours", "-m", "merged", "tmp-bob",
             cwd=source)
        _git("branch", "--quiet", "-D", "tmp-alice", cwd=source)
        _git("branch", "--quiet", "-D", "tmp-bob", cwd=source)
        seed = git_tier.seed_bare(source, tmp_path / "seed.git")
        assert len(git_tier._branches(seed)) == 1, "the fixture must leave exactly one branch head"

        # Act
        results = {r.name: r for r in git_tier.measure_merge_tree(seed, repeats=2)}

        # Assert
        conflicting = results[git_tier.M2_CONFLICTING]
        assert conflicting.status == report.MEASURED, conflicting.reason
        assert conflicting.data["conflicted"] is True
        # And it says where the pair came from, so a reader can tell a branch-head timing from a
        # commit-pair one — they are not the same measurement.
        assert conflicting.data["pair_source"] in ("branches", "commits")

    def test_a_repository_with_no_pair_to_merge_withholds_both_halves(self, tmp_path):
        """A single-commit repository has no pair to merge at all. The honest report says which
        half is missing and why — a zero would be indistinguishable from an instant merge."""
        # Arrange
        lonely = tmp_path / "lonely"
        lonely.mkdir()
        _git("init", "--quiet", "--initial-branch=main", ".", cwd=lonely)
        (lonely / "only.txt").write_text("x\n")
        _git("add", "-A", cwd=lonely)
        _git("commit", "--quiet", "-m", "only", cwd=lonely)
        seed = git_tier.seed_bare(lonely, tmp_path / "seed.git")

        # Act
        results = git_tier.measure_merge_tree(seed, repeats=2)

        # Assert — both, and each says why rather than being absent from the report.
        assert {r.status for r in results} == {report.UNAVAILABLE}
        assert all(r.reason and not r.data for r in results)


class TestAnEstimateCarriesItsMethodAndItsLimits:
    """Issue 35: "Measurement 6 states its method and its limits beside its number", because "an
    estimate presented as a measurement is worse than no number". The rate this produces is the
    whole cost model of auto-apply (issues 41 and 42), so it is the number most likely to be lifted
    out of the report and quoted on its own."""

    def test_a_rate_without_stated_limits_is_refused(self):
        with pytest.raises(ValueError):
            estimate_tier.estimated(name="6-tier-3-rate", data={"rate": 0.02},
                                    method="pairs within a window", limits=[])

    def test_a_rate_without_a_stated_method_is_refused(self):
        with pytest.raises(ValueError):
            estimate_tier.estimated(name="6-tier-3-rate", data={"rate": 0.02},
                                    method="   ", limits=["commit times are not turn times"])

    def test_an_estimate_is_labelled_as_one_beside_its_number(self):
        """The positive row. A guard nothing exercises is a guard that can stop firing unnoticed —
        and this one also has to prove the fields REACH the report rather than merely being
        demanded of the caller."""
        result = estimate_tier.estimated(
            name="6-tier-3-rate", data={"rate": 0.02},
            method="pairs of commits by different authors within a window",
            limits=["commit timestamps are not turn timestamps"])

        assert result.data["estimate"] is True
        assert result.data["method"]
        assert result.data["limits"] == ["commit timestamps are not turn timestamps"]


class TestTheReplaySampleIsNotTakenFromOneEndOfTheHistory:
    """Found by checking the real run: 29,553 eligible pairs spanning 2024-10-24 to 2026-04-17, and
    the 500 that were replayed all fell in the **first three weeks** — because the candidates are
    time-sorted and the cap took the first N. The rate then describes that team in late 2024 rather
    than across eighteen months, while the report says only "500 of 29,553", which reads like a
    sample rather than a slice.

    Fixed by striding evenly. Deterministic, so the run stays reproducible — randomness would make
    the number un-retakeable, which is the whole point of committing the harness."""

    def test_the_sample_spans_the_whole_candidate_range(self):
        # Arrange — 1000 candidates in order; the cap admits a tenth of them.
        candidates = list(range(1000))

        # Act
        sample = estimate_tier.stride_sample(candidates, 100)

        # Assert
        assert len(sample) == 100
        assert sample[0] == 0
        assert sample[-1] >= 900, f"the sample stops at {sample[-1]}, far short of the end"

    def test_a_candidate_list_under_the_cap_is_taken_whole(self):
        assert estimate_tier.stride_sample([1, 2, 3], 100) == [1, 2, 3]

    def test_the_sample_is_deterministic(self):
        """Two runs of the harness must produce the same number, or nobody can re-take it."""
        candidates = list(range(997))

        assert estimate_tier.stride_sample(candidates, 50) == \
            estimate_tier.stride_sample(candidates, 50)


class TestTheTier3ReplayAgainstRealHistory:

    def test_a_single_author_history_has_no_concurrency_and_says_so(self, tiny_repo, tmp_path):
        """`tiny_repo` is one author throughout. A rate computed from it would be a number about
        nothing — and it would come out as 0.0, which reads like the best possible news."""
        # Arrange
        seed = git_tier.seed_bare(tiny_repo, tmp_path / "seed.git")

        # Act
        result = estimate_tier.measure_tier3_rate(seed, window_seconds=3600)

        # Assert
        assert result.status == report.UNAVAILABLE
        assert "DIFFERENT authors" in result.reason, result.reason

    def test_two_authors_committing_together_produce_a_rate(self, tmp_path):
        # Arrange — two authors, same instant, editing the SAME line: a guaranteed tier 3.
        source = tmp_path / "team"
        source.mkdir()
        _git("init", "--quiet", "--initial-branch=main", ".", cwd=source)
        (source / "f.txt").write_text("base\n")
        _git("add", "-A", cwd=source)
        _git("commit", "--quiet", "-m", "base", cwd=source)
        for who, branch, text in (("alice", "a", "alice\n"), ("bob", "b", "bob\n")):
            _git("checkout", "--quiet", "-B", branch, "main", cwd=source)
            (source / "f.txt").write_text(text)
            _git("-c", f"user.name={who}", "-c", f"user.email={who}@invalid",
                 "commit", "--quiet", "-am", who, cwd=source, author=who)
        seed = git_tier.seed_bare(source, tmp_path / "seed.git")

        # Act
        result = estimate_tier.measure_tier3_rate(seed, window_seconds=3600)

        # Assert — the decomposition, not just the rate, because the rate alone cannot show which
        # denominator produced it. Three different-author pairs exist (the base is committed by a
        # third identity): base↔alice and base↔bob each merge cleanly, alice↔bob collides on the
        # one line both rewrote. So one in three, and the denominator is pairs that MERGED.
        assert result.status == report.MEASURED, result.reason
        assert result.data["conflicted"] == 1
        assert result.data["merged_cleanly"] == 2
        assert result.data["unmergeable_pairs"] == 0
        assert result.data["tier_3_rate"] == pytest.approx(1 / 3, abs=1e-4)
        assert result.data["estimate"] is True


class TestResumingIsNotTheSameAsExitingZero:
    """Measurement 5, and the reason issue 35 says the first draft missed it: it asked whether
    compaction changes the BLOB, which is a git question, and not whether the conversation still
    resumes, which is the product one.

    Today the provider validates only that the transcript's first line parses as a JSON object
    before handing the id to `--resume` (`task_agent.resumable_session`, measured). So a transcript
    that survives that check and has lost the conversation produces a task that completes, pushes,
    and reports healthy — with the person's context silently gone. A boolean cannot express that,
    which is why this returns three values."""

    def test_a_run_that_echoes_the_planted_token_resumed(self):
        verdict = agent_tier.classify_resume(
            returncode=0, stdout="The word you asked me to remember was XYZZY-1.", token="XYZZY-1")

        assert verdict == agent_tier.RESUMED_WITH_MEMORY

    def test_a_run_that_exits_zero_without_the_token_did_NOT_resume(self):
        """The failure that reads healthy, and the whole reason this is not a boolean. Exit 0, a
        fluent answer, and the conversation gone."""
        verdict = agent_tier.classify_resume(
            returncode=0, stdout="I don't have any record of a word you asked me to remember.",
            token="XYZZY-1")

        assert verdict == agent_tier.RESUMED_WITHOUT_MEMORY
        assert verdict != agent_tier.RESUMED_WITH_MEMORY

    def test_a_nonzero_exit_is_a_refusal_and_not_amnesia(self):
        """Kept apart because the two call for opposite conclusions: a refusal says the round trip
        broke the transcript, amnesia says it did not and the conversation was lost anyway."""
        verdict = agent_tier.classify_resume(
            returncode=1, stdout="", token="XYZZY-1")

        assert verdict == agent_tier.RESUME_REFUSED

    def test_a_token_that_never_appears_in_the_prompt_cannot_be_echoed_by_accident(self):
        """The planted token is checked case-insensitively but as a whole word, so a model that
        merely says "xyzzy" in passing is still a match — and one that says something ELSE is not.
        A substring rule over a short token would make almost any fluent answer count."""
        verdict = agent_tier.classify_resume(
            returncode=0, stdout="I remember the word XYZZY-2.", token="XYZZY-1")

        assert verdict == agent_tier.RESUMED_WITHOUT_MEMORY


class TestCompactionIsNotInferredFromTheFileSizeAlone:
    """Measurement 4. Two independent signals — did the `.jsonl` SHRINK, and did Claude Code record
    a compaction — because the interesting outcomes are exactly the ones where they disagree, and
    a size-only rule silently reports the wrong one of them."""

    def test_a_compaction_that_shrank_the_file_answers_the_question(self):
        result = agent_tier.decide_compaction(
            samples=[{"turn": 1, "bytes": 100}, {"turn": 2, "bytes": 900},
                     {"turn": 3, "bytes": 300}],
            compaction_records=1)

        assert result.status == report.MEASURED, result.reason
        assert result.data["shortens"] is True
        assert result.data["shrank_at_turn"] == 3

    def test_a_compaction_that_did_NOT_shrink_the_file_is_the_finding_not_a_failure(self):
        """The measurement's most valuable outcome: a compaction happened and the blob did not get
        smaller, so a fast-forward-only rule on the side ref is reasoning about the wrong thing."""
        result = agent_tier.decide_compaction(
            samples=[{"turn": 1, "bytes": 100}, {"turn": 2, "bytes": 900},
                     {"turn": 3, "bytes": 1200}],
            compaction_records=1)

        assert result.status == report.MEASURED, result.reason
        assert result.data["shortens"] is False

    def test_no_compaction_at_all_is_withheld_and_never_reported_as_does_not_shorten(self):
        """The conflation this measurement exists to prevent. A run where compaction simply never
        fired has observed NOTHING about compaction — and a size-only rule would report exactly
        `shortens: false`, which is a claim about a thing that did not happen."""
        result = agent_tier.decide_compaction(
            samples=[{"turn": 1, "bytes": 100}, {"turn": 2, "bytes": 900}],
            compaction_records=0)

        assert result.status == report.UNAVAILABLE
        assert "no compaction" in result.reason.lower(), result.reason

    def test_turns_that_never_RAN_are_not_reported_as_turns_that_did_not_compact(self):
        """Found by running it: twelve fill turns in a row exited non-zero — `--allowedTools` takes
        `<tools...>`, so `--allowedTools Read "<prompt>"` swallowed the prompt as a second tool
        name — and the transcript sat at exactly 99,628 bytes throughout. Every signal the harness
        had said "no compaction occurred", which is true and completely misleading: nothing had
        happened at all. The failure count is the only thing that separates them."""
        result = agent_tier.decide_compaction(
            samples=[{"turn": 1, "bytes": 100}, {"turn": 2, "bytes": 100}],
            compaction_records=0, failed_turns=12)

        assert result.status == report.UNAVAILABLE
        assert "failed" in result.reason.lower(), result.reason
        assert "no compaction occurred" not in result.reason.lower(), result.reason

    def test_a_sample_that_was_never_READ_cannot_be_the_turn_a_compaction_happened_at(self):
        """Review finding (MEDIUM-HIGH), verified: `decide_growth` gained a `missing` guard and
        this function did not, while both consume the same `samples` list.

        A single intermediate sample coming back `missing` (0 bytes) between two real, larger
        values — with a genuine compaction elsewhere in the run, so `compaction_records > 0` —
        walks past both `unavailable` branches and returns `shortens: True` with
        `shrank_at_turn` pointing at the turn nothing was read for. A specific, plausible, WRONG
        fact: "the compaction happened at turn N", manufactured by a sampling artifact. And the
        subprocess exits 0 on such a turn, so the failed-turn counter cannot catch it either."""
        result = agent_tier.decide_compaction(
            samples=[{"turn": 1, "bytes": 500},
                     {"turn": 2, "bytes": 0, "missing": True},
                     {"turn": 3, "bytes": 900}],
            compaction_records=1)

        assert result.status == report.UNAVAILABLE
        assert result.data == {}, "a fabricated shrink point must not reach the report"
        assert "not found" in result.reason.lower() or "missing" in result.reason.lower(), \
            result.reason

    def test_a_shrink_with_no_compaction_record_is_withheld_rather_than_guessed(self):
        """Neither reading is safe: the transcript may have been truncated by something else
        entirely. An unexercisable branch that quietly picked one is where the next version of this
        bug would live, so it reports and names the disagreement instead."""
        result = agent_tier.decide_compaction(
            samples=[{"turn": 1, "bytes": 900}, {"turn": 2, "bytes": 100}],
            compaction_records=0)

        assert result.status == report.UNAVAILABLE
        assert "shrank" in result.reason.lower(), result.reason


class TestTranscriptGrowthIsRecordedPerTurn:
    """Measurement 3 decides whether the side ref is fetched and pushed every turn or only on
    change (issue 39). What answers that is what the Nth turn ADDS — a total cannot, and a total is
    what a linear-cost assumption produces without anybody noticing it assumed one."""

    def test_the_delta_of_every_turn_is_kept(self):
        result = agent_tier.decide_growth(
            samples=[{"turn": 1, "bytes": 100, "lines": 2},
                     {"turn": 2, "bytes": 350, "lines": 6},
                     {"turn": 3, "bytes": 900, "lines": 11}])

        assert result.status == report.MEASURED, result.reason
        assert [row["added_bytes"] for row in result.data["turns"]] == [100, 250, 550]
        assert result.data["largest_turn_bytes"] == 550

    def test_a_transcript_that_was_never_FOUND_is_not_a_curve_of_zeros(self):
        """`_sample` marks an absent transcript `missing`, and nothing read that flag — so a wrong
        path derivation (the exact bug issue 06 shipped: the provider planted its symlink at the
        unresolved workspace path while the binary wrote elsewhere) produced 50 samples of 0 bytes
        and a report saying `total_bytes: 0, mean_turn_bytes: 0`. Every turn would have succeeded,
        so the failed-turn counter cannot catch it either."""
        result = agent_tier.decide_growth(samples=[
            {"turn": 1, "bytes": 0, "lines": 0, "missing": True},
            {"turn": 2, "bytes": 0, "lines": 0, "missing": True}])

        assert result.status == report.UNAVAILABLE
        assert "not found" in result.reason.lower() or "missing" in result.reason.lower(), \
            result.reason

    def test_one_turn_cannot_answer_a_question_about_growth(self):
        """A single sample yields a delta of "all of it", which reads as a measured growth rate."""
        result = agent_tier.decide_growth(samples=[{"turn": 1, "bytes": 100, "lines": 2}])

        assert result.status == report.UNAVAILABLE
        assert result.reason


class TestTheCompactionMarkerScan:
    """The transcript schema belongs to the vendor, and a normal transcript carries no compaction
    marker at all (measured on 2.1.232 — `type` is one of queue-operation / attachment / user /
    last-prompt / assistant, and no key mentions compaction). So the scan looks STRUCTURALLY and
    reports what it found, rather than pinning a key name nobody has seen."""

    def test_a_marker_key_anywhere_in_a_record_is_found(self, tmp_path):
        # Arrange
        path = tmp_path / "t.jsonl"
        path.write_text(
            '{"type":"user","message":{"content":"hi"}}\n'
            '{"type":"user","isCompactSummary":true,"message":{"content":"summary"}}\n')

        # Act
        found, markers = agent_tier.scan_compaction_markers(path)

        # Assert
        assert found == 1
        assert "isCompactSummary" in markers

    def test_a_type_value_naming_compaction_is_found_too(self, tmp_path):
        """Two shapes because the vendor could use either, and a scan that knew only one would
        report "no compaction" for a transcript that plainly had one."""
        path = tmp_path / "t.jsonl"
        path.write_text('{"type":"compact_boundary","subtype":"auto"}\n')

        found, markers = agent_tier.scan_compaction_markers(path)

        assert found == 1
        assert markers

    def test_the_word_compact_in_a_persons_message_is_not_a_marker(self, tmp_path):
        """The negative row, and it is not hypothetical: a raw text search for "compact" matches
        any turn where somebody — or the model — used the word, which would manufacture a
        compaction the run never had."""
        path = tmp_path / "t.jsonl"
        path.write_text(
            '{"type":"user","message":{"content":"please compact the CSS"}}\n'
            '{"type":"assistant","message":{"content":"I compacted it."}}\n')

        found, markers = agent_tier.scan_compaction_markers(path)

        assert found == 0
        assert markers == []

    def test_an_unparseable_line_does_not_stop_the_scan_or_count_as_a_marker(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text('not json\n{"type":"user","isCompactSummary":true}\n')

        found, _ = agent_tier.scan_compaction_markers(path)

        assert found == 1


class TestTheHarnessNeverRidesAlongWithTheOrdinarySuite:
    """The `measure_` prefix is the only thing keeping a 792 MiB clone and a paid conversation out
    of `pytest tests/`. It works because the repo sets no custom `python_files` — which is a fact
    about `pyproject.toml`, not about this file, and therefore one a future edit can quietly
    change. `measure_git_plane.py` relies on the same trick in grid-src and states the reason; this
    asserts it."""

    def test_pytest_collects_none_of_the_harness(self):
        # Arrange / Act — the real collector on the real directory, so this cannot pass by
        # agreeing with a rule the harness wrote down itself.
        collected = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests/"],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True, text=True, timeout=900).stdout

        # Assert — the PATH PREFIX, not the bare name. This test file is itself
        # `tests/test_measure_non_dev_design.py`, so a substring check matches its own node ids and
        # fails for the wrong reason; only a collected id under `tests/measure_non_dev` (no `test_`)
        # is the harness.
        harness = [line for line in collected.splitlines()
                   if line.strip().startswith("tests/measure_non_dev")]
        assert harness == [], \
            f"the measurement harness is being collected by the ordinary suite: {harness[:5]}"

    def test_the_entry_point_is_not_named_like_a_test(self):
        """The cheap half of the same guard, so a rename is caught without a full collection."""
        for module in (Path(__file__).resolve().parent / "measure_non_dev").glob("*.py"):
            assert not module.name.startswith("test_"), module
        assert not (Path(__file__).resolve().parent / "measure_non_dev_design.py").name \
            .startswith("test_")


class TestTheReportSurvivesTheRunThatProducedIt:
    """Found by running the documented invocation: with no `--report` and no `--scratch`, the
    report was written INTO the temp scratch directory and then deleted by the cleanup — the
    numbers survived only as terminal scrollback. `measure_git_plane.py`'s note applies exactly:
    a number nobody can reproduce is an anecdote, and one nobody can retrieve is worse."""

    def test_a_run_with_no_report_flag_still_leaves_a_report_on_disk(
            self, tiny_repo, tmp_path, monkeypatch, capsys):
        # Arrange — the documented invocation: a tier and a repo, nothing else.
        monkeypatch.chdir(tmp_path)

        # Act
        cli.main(["--tier", "git", "--repo", str(tiny_repo)])

        # Assert — a file exists, and the run SAID where, so it can be cited.
        written = list(tmp_path.glob("*.json"))
        assert written, "the run left no report behind"
        assert json.loads(written[0].read_text())["measurements"]
        assert str(written[0]) in capsys.readouterr().out

    def test_the_bulky_scratch_directory_is_still_cleaned_up(self, tiny_repo, tmp_path,
                                                             monkeypatch):
        """The other half, or the fix trades a lost report for gigabytes of abandoned clones on
        every run — which is how a measurement harness becomes one nobody runs."""
        # Arrange
        monkeypatch.chdir(tmp_path)
        scratches = set(Path(tempfile.gettempdir()).glob("measure-0034-*"))

        # Act
        cli.main(["--tier", "git", "--repo", str(tiny_repo)])

        # Assert
        assert set(Path(tempfile.gettempdir()).glob("measure-0034-*")) == scratches


class TestATierThatCannotRunSaysSo:
    """Issue 35: "the ones needing a large repository or a real agent say so rather than failing
    obscurely". A traceback is the obscure failure; a sentence naming the flag is not."""

    def test_the_git_tier_without_a_repo_names_the_flag(self, capsys):
        # Arrange / Act
        code = cli.main(["--tier", "git"])

        # Assert
        assert code != 0
        message = capsys.readouterr().err
        assert "--repo" in message, message

    def test_the_agent_tier_without_claude_code_names_claude_code(
            self, capsys, monkeypatch, tmp_path):
        """No mock: `claude_install.resolve()` reads PATH and then `Path.home()`, so emptying both
        is a REAL machine without Claude Code rather than a stubbed answer about one."""
        # Arrange — an empty PATH and a home holding neither conventional install location.
        (tmp_path / "empty").mkdir()
        (tmp_path / "home").mkdir()
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        monkeypatch.setenv("HOME", str(tmp_path / "home"))

        # Act
        code = cli.main(["--tier", "agent"])

        # Assert
        assert code == cli.EXIT_CANNOT_RUN
        message = capsys.readouterr().err
        assert "Claude Code" in message, message
