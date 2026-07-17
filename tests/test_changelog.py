"""Tests for packaging/changelog.py — the release-notes renderer.

GitHub's `generate_release_notes` builds its list from *merged PRs*, but grid's release
commits land on public/main directly, with no PR — so every release through v0.2.1
shipped notes consisting of one bare "Full Changelog" compare link. This renderer reads
the commit log instead.

It runs only at release time, which is precisely the code path that shipped the v0.1.1
(wrong version in the wheel) and v0.1.12 (tagged without a bump) mistakes: both had green
runs, because nothing exercised the release path until it was the release. Hence tests.

Each test builds a throwaway git repo and runs the real script against it — same approach
as tests/test_install_sh.py, which drives the real install.sh.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "packaging" / "changelog.py"


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return res.stdout.strip()


def _commit(repo: Path, subject: str) -> None:
    _git(repo, "commit", "--allow-empty", "-q", "-m", subject)


def _render(repo: Path, ref: str, *base: str, env: dict[str, str] | None = None) -> str:
    """Run the real renderer against `repo`, returning the markdown body."""
    res = subprocess.run(
        [sys.executable, str(SCRIPT), ref, *base],
        cwd=repo, capture_output=True, text=True, env=env,
    )
    assert res.returncode == 0, f"changelog.py failed:\n{res.stdout}\n{res.stderr}"
    return res.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "test@example.com")
    _git(r, "config", "user.name", "Test")
    _git(r, "config", "commit.gpgsign", "false")
    _commit(r, "chore: initial commit")
    return r


def test_groups_features_and_fixes_under_their_own_headings(repo):
    _git(repo, "tag", "v1.0.0")
    _commit(repo, "feat(router): add auto model selection")
    _commit(repo, "fix(api-engine): send the vendor's own output-token parameter")
    _git(repo, "tag", "v1.1.0")

    out = _render(repo, "v1.1.0")

    assert "### Features" in out
    assert "### Fixes" in out
    # The type prefix is stripped, the scope is kept — the scope is the useful part.
    assert "- router: add auto model selection" in out
    assert "- api-engine: send the vendor's own output-token parameter" in out
    assert "feat(router)" not in out, "the conventional-commit type should be stripped"


def test_skips_the_release_bump_commit(repo):
    """`chore(release): vX.Y.Z` is the bump itself — noise in its own release notes."""
    _git(repo, "tag", "v1.0.0")
    _commit(repo, "feat: something users care about")
    _commit(repo, "chore(release): v1.1.0")
    _git(repo, "tag", "v1.1.0")

    out = _render(repo, "v1.1.0")

    assert "something users care about" in out
    assert "chore(release)" not in out
    # Not a bare `"v1.1.0" not in out`: the compare link names the ref, legitimately.
    listed = [line for line in out.splitlines() if line.startswith("- ")]
    assert not any("v1.1.0" in line for line in listed), (
        f"the release bump commit must not appear in its own notes; listed: {listed}"
    )


def test_only_includes_commits_since_the_previous_tag(repo):
    _commit(repo, "feat: shipped in the previous release")
    _git(repo, "tag", "v1.0.0")
    _commit(repo, "feat: shipped in this release")
    _git(repo, "tag", "v1.1.0")

    out = _render(repo, "v1.1.0")

    assert "shipped in this release" in out
    assert "shipped in the previous release" not in out


def test_previous_tag_resolution_works_for_annotated_tags(repo):
    """This repo's history is split: v0.1.15/v0.2.0 annotated, the rest lightweight."""
    _commit(repo, "feat: shipped in the previous release")
    _git(repo, "tag", "-a", "v1.0.0", "-m", "v1.0.0")
    _commit(repo, "feat: shipped in this release")
    _git(repo, "tag", "-a", "v1.1.0", "-m", "v1.1.0")

    out = _render(repo, "v1.1.0")

    assert "shipped in this release" in out
    assert "shipped in the previous release" not in out


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], cwd=repo, capture_output=True, text=True
    )


def test_refuses_when_no_previous_tag_is_reachable(repo):
    """"No previous tag" and "the tags were never fetched" look identical from here.

    Guessing the former renders the whole repo history as one release — real subjects,
    real shas, perfectly formatted, completely wrong. So say so instead, and name both
    the likely cause and the override.
    """
    _commit(repo, "feat: the very first feature")
    _git(repo, "tag", "v0.1.0")

    res = _run(repo, "v0.1.0")

    assert res.returncode != 0, f"expected a loud failure; instead printed {res.stdout!r}"
    assert "fetch-depth: 0" in res.stderr, f"error must name the likely cause: {res.stderr!r}"
    assert "<base>" in res.stderr, f"error must name the override: {res.stderr!r}"


def test_a_clone_with_full_depth_but_no_tags_refuses_rather_than_rendering_everything(
    repo, tmp_path
):
    """The gap `require_full_history` alone misses: complete history, absent tags.

    `--is-shallow-repository` reports false here, so the shallow guard waves it through.
    Without the previous-tag guard this rendered every commit since the repo's creation,
    exit 0, including releases that shipped long ago.
    """
    _commit(repo, "feat: shipped ages ago in v1.0.0")
    _git(repo, "tag", "v1.0.0")
    _commit(repo, "fix: the only thing in this release")
    _git(repo, "tag", "v1.1.0")

    notags = tmp_path / "notags"
    subprocess.run(
        ["git", "clone", "-q", "--no-tags", f"file://{repo}", str(notags)],
        check=True, capture_output=True,
    )
    _git(notags, "tag", "v1.1.0")  # only the pushed tag came along
    assert _git(notags, "rev-parse", "--is-shallow-repository") == "false"

    res = _run(notags, "v1.1.0")

    assert res.returncode != 0, f"expected a loud failure; instead printed {res.stdout!r}"
    assert "shipped ages ago" not in res.stdout


def test_an_explicit_base_overrides_tag_discovery(repo):
    """The escape hatch the refusal points at — also the first-release path."""
    root = _git(repo, "rev-list", "--max-parents=0", "HEAD")
    _commit(repo, "feat: the very first feature")
    _git(repo, "tag", "v0.1.0")

    out = _render(repo, "v0.1.0", root)

    assert "the very first feature" in out


def test_a_bad_ref_fails_loudly_with_gits_own_diagnostic(repo):
    """CalledProcessError's str() drops stderr; a bare exit code is useless at 2am."""
    res = _run(repo, "v9.9.9-does-not-exist")

    assert res.returncode != 0
    assert "Needed a single revision" in res.stderr or "unknown revision" in res.stderr, (
        f"git's own message must survive; got: {res.stderr!r}"
    )


def test_breaking_change_gets_its_own_section(repo):
    _git(repo, "tag", "v1.0.0")
    _commit(repo, "feat(cli)!: rename --node to --engine")
    _git(repo, "tag", "v2.0.0")

    out = _render(repo, "v2.0.0")

    assert "### Breaking changes" in out
    assert "- cli: rename --node to --engine" in out
    # A breaking feat belongs in Breaking changes, not duplicated under Features.
    assert out.count("rename --node to --engine") == 1


def test_breaking_marker_wins_for_any_type_not_just_the_curated_ones(repo):
    """`!` means breaking whatever the type — a breaking refactor is still breaking.

    Routing it to "Other changes" is how a migration-impacting flag rename gets missed
    by someone scanning the notes for exactly that.
    """
    _git(repo, "tag", "v1.0.0")
    _commit(repo, "refactor!: drop --node flag, use --engine")
    _git(repo, "tag", "v2.0.0")

    out = _render(repo, "v2.0.0")

    assert "### Breaking changes" in out
    assert "- drop --node flag, use --engine" in out
    assert "### Other changes" not in out


def test_a_subject_containing_the_field_separator_survives_intact(repo):
    """git allows \\x1f in a subject; splitting sha-first and once keeps it whole.

    Splitting subject-first truncated the description and printed a fragment of the
    subject where the sha belongs — exit 0, nothing amiss to look at.
    """
    _git(repo, "tag", "v1.0.0")
    _commit(repo, "feat: weird\x1fsubject here")
    _git(repo, "tag", "v1.1.0")
    sha = _git(repo, "rev-parse", "--short", "HEAD")

    out = _render(repo, "v1.1.0")

    assert "weird\x1fsubject here" in out
    assert f"({sha})" in out, f"the real sha must be the sha; got:\n{out}"


def test_renders_a_branch_ref_for_the_dry_run_preview(repo):
    """release.yml's render step is not tag-guarded: a dispatch previews from `main`."""
    _git(repo, "tag", "v1.0.0")
    _commit(repo, "feat: not yet tagged")
    _commit(repo, "chore(release): v1.1.0")

    out = _render(repo, "main")

    assert "- not yet tagged" in out
    assert "v1.1.0" not in out, "the untagged bump commit is still noise"


def test_a_tag_on_a_divergent_branch_is_not_used_as_the_base(repo):
    """v0.1.13 was cut on a line that never merged; describe must skip such tags."""
    _git(repo, "tag", "v1.0.0")
    _git(repo, "checkout", "-q", "-b", "sideline")
    _commit(repo, "feat: never merged to main")
    _git(repo, "tag", "v1.0.5-divergent")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "feat: actually shipped")
    _git(repo, "tag", "v1.1.0")

    out = _render(repo, "v1.1.0")

    assert "- actually shipped" in out
    assert "never merged" not in out, "a tag off the mainline must not become the base"


def test_other_types_land_in_other_changes_with_their_prefix_intact(repo):
    _git(repo, "tag", "v1.0.0")
    _commit(repo, "docs(router): ADR 0014 — the Advisor sees candidate prices")
    _git(repo, "tag", "v1.0.1")

    out = _render(repo, "v1.0.1")

    assert "### Other changes" in out
    # Outside the curated sections the type itself is the context — keep it.
    assert "- docs(router): ADR 0014 — the Advisor sees candidate prices" in out


def test_non_conventional_subjects_are_kept_verbatim(repo):
    _git(repo, "tag", "v1.0.0")
    _commit(repo, "Revert an unrelated thing")
    _git(repo, "tag", "v1.0.1")

    out = _render(repo, "v1.0.1")

    assert "- Revert an unrelated thing" in out


def test_each_entry_carries_its_short_sha(repo):
    _git(repo, "tag", "v1.0.0")
    _commit(repo, "fix: a real bug")
    _git(repo, "tag", "v1.0.1")
    sha = _git(repo, "rev-parse", "--short", "HEAD")

    out = _render(repo, "v1.0.1")

    assert sha in out, f"expected short sha {sha} in:\n{out}"


def test_refuses_to_run_in_a_shallow_clone(repo, tmp_path):
    """A shallow clone can't see the previous tag, so it renders *empty* notes.

    That fails in the worst possible way: silently, on the release path, producing
    exactly the empty changelog this script exists to fix. actions/checkout defaults
    to depth 1 — release.yml must pass fetch-depth: 0 — so if that ever regresses,
    say so loudly instead of shipping a blank release body.
    """
    _commit(repo, "feat: shipped in the previous release")
    _git(repo, "tag", "v1.0.0")
    _commit(repo, "feat: shipped in this release")
    _git(repo, "tag", "v1.1.0")
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", "--branch", "v1.1.0",
         f"file://{repo}", str(shallow)],
        check=True, capture_output=True,
    )

    res = subprocess.run(
        [sys.executable, str(SCRIPT), "v1.1.0"], cwd=shallow, capture_output=True, text=True
    )

    assert res.returncode != 0, f"expected a loud failure; instead it printed {res.stdout!r}"
    assert "shallow" in res.stderr.lower(), f"error must name the cause; got: {res.stderr!r}"


def test_a_range_with_no_shippable_commits_still_renders_the_compare_link(repo):
    """`generate_release_notes` is off, so nothing back-fills an empty body any more."""
    _commit(repo, "feat: shipped already")
    _git(repo, "tag", "v1.0.0")
    _commit(repo, "chore(release): v1.0.1")
    _git(repo, "tag", "v1.0.1")

    out = _render(repo, "v1.0.1")

    assert out.strip() == (
        "**Full Changelog**: https://github.com/autonomous-ai/autonomous-grid"
        "/compare/v1.0.0...v1.0.1"
    )


def test_the_body_ends_with_the_compare_link(repo):
    _git(repo, "tag", "v1.0.0")
    _commit(repo, "fix: a real bug")
    _git(repo, "tag", "v1.1.0")

    out = _render(repo, "v1.1.0")

    assert out.strip().endswith("/compare/v1.0.0...v1.1.0")
    assert "### Fixes" in out


def test_the_compare_link_names_the_repository_ci_is_building(repo):
    """Actions sets GITHUB_REPOSITORY/GITHUB_SERVER_URL; they win over the fallback."""
    _git(repo, "tag", "v1.0.0")
    _commit(repo, "fix: a real bug")
    _git(repo, "tag", "v1.1.0")
    env = {
        **os.environ,
        "GITHUB_SERVER_URL": "https://github.example",
        "GITHUB_REPOSITORY": "someone/elsewhere",
    }

    out = _render(repo, "v1.1.0", env=env)

    assert (
        "**Full Changelog**: https://github.example/someone/elsewhere"
        "/compare/v1.0.0...v1.1.0" in out
    )


def test_the_compare_link_falls_back_to_the_public_repo_outside_ci(repo):
    """A local preview must not point at `origin` — that's the throwaway staging repo."""
    _git(repo, "tag", "v1.0.0")
    _commit(repo, "fix: a real bug")
    _git(repo, "tag", "v1.1.0")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")}

    out = _render(repo, "v1.1.0", env=env)

    assert "https://github.com/autonomous-ai/autonomous-grid/compare/" in out
    assert "super-grid-tmp" not in out
