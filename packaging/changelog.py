#!/usr/bin/env python3
"""Draft a release's notes from the commit log.

    packaging/changelog.py <ref> [<base>] > /tmp/notes.md

`<ref>` is the release being drafted (v0.2.1), or a branch to preview one (main).
`<base>` overrides the previous-tag lookup — needed only when no earlier tag is
reachable, e.g. a genuine first release (pass the root commit).

This is a **draft for a human to edit**, not the last word. The release-grid-cli skill's
step 5 pipes it to a file, opens it in an editor, and tags with the result
(`git tag -a vX.Y.Z -F /tmp/notes.md`); release.yml then publishes that tag message
verbatim. So nothing here ships unreviewed — cut what you like.

It drafts from `git log` rather than leaning on GitHub's `generate_release_notes`
because that generator lists only commits it can tie to a *merged PR*, and plenty of
grid's commits (every release bump, for one) go straight to main with no PR: its list is
a silent subset, complete-looking and missing half the release. Every release through
v0.2.1 got the degenerate version of that — an empty list and one bare compare link.
A git log cannot miss a commit; the compare link is re-added here.

The trade is that PR numbers and @credit are gone — paste them in while editing if a
release wants them.

This runs only on the release path — the same once-a-release path that shipped v0.1.1
(wrong version in the wheel) and v0.1.12 (tagged without a bump), both on green runs.
So it prefers dying loudly over guessing: a changelog that is quietly wrong looks
exactly like one that is right.

Only `!` marks a breaking change here — a `BREAKING CHANGE:` footer won't, since this
reads subjects, not bodies. Nothing is dropped silently: an unrecognised subject is
listed verbatim under "Other changes".
"""

import os
import re
import subprocess
import sys

# Actions sets both; the fallbacks are for previewing a release from a local checkout.
# Deliberately not derived from a git remote: `origin` here is a throwaway staging repo,
# so a link built from it would point somewhere users can't reach.
DEFAULT_SERVER = "https://github.com"
DEFAULT_REPO = "autonomous-ai/autonomous-grid"

# feat(scope)!: description
CONVENTIONAL = re.compile(
    r"^(?P<type>[a-z]+)(?:\((?P<scope>[^)]*)\))?(?P<breaking>!)?: (?P<desc>.+)$"
)

# Types that earn their own section, in the order users care about.
SECTION_BY_TYPE = {
    "feat": "Features",
    "fix": "Fixes",
    "perf": "Performance",
}
BREAKING = "Breaking changes"
OTHER = "Other changes"
HEADING_ORDER = [BREAKING, *SECTION_BY_TYPE.values(), OTHER]

# Separates the sha from the subject in git's pretty format. A subject may legally
# contain this byte, so the sha — always plain hex — goes first and we split once.
SEP = "\x1f"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


def require_full_history() -> None:
    """Refuse to run against a shallow clone.

    A shallow clone can't see the previous tag, so every commit before the fetch horizon
    vanishes. The result isn't an error — it's a changelog that is silently wrong, and
    *how* wrong depends on which commit happens to sit at the tip. actions/checkout
    defaults to depth 1; release.yml passes fetch-depth: 0.
    """
    if _git("rev-parse", "--is-shallow-repository") == "true":
        sys.exit(
            "changelog: refusing to run in a shallow clone — the previous tag isn't "
            "reachable, so the notes would be silently incomplete. Give the checkout "
            "step `fetch-depth: 0`."
        )


def previous_tag(ref: str) -> str:
    """The nearest tag reachable from `ref`'s first parent.

    Never falls back to "render everything". "No tag is reachable" is indistinguishable
    from "the tags were never fetched" (a full-depth clone can still carry zero tags —
    `clone --no-tags`, `fetch-tags: false`), and guessing wrong renders the repo's entire
    history as one release: real subjects, real shas, perfectly formatted, completely
    wrong. Pass an explicit `<base>` for the rare case where there truly is no previous
    tag.

    `--tags` matches lightweight tags too — most of this repo's are. Tags cut on a
    divergent line (v0.1.13) aren't ancestors, so they're correctly skipped.
    """
    try:
        return _git("describe", "--tags", "--abbrev=0", f"{ref}^")
    except subprocess.CalledProcessError:
        pass  # no tag reachable, or no parent — both land on the same remedy below
    _git("rev-parse", "--verify", f"{ref}^{{commit}}")  # a bad ref dies here, with git's words
    sys.exit(
        f"changelog: no tag is reachable from {ref}^ — either the tags weren't fetched "
        "(actions/checkout needs fetch-depth: 0), or this is a first release. If you "
        f"meant it, name the base explicitly:  changelog.py {ref} <base>"
    )


def commits(ref: str, base: str) -> list[tuple[str, str]]:
    """(subject, short_sha) for each non-merge commit shipping in this release."""
    log = _git("log", "--no-merges", f"--pretty=%h{SEP}%s", f"{base}..{ref}")
    return [
        (line.partition(SEP)[2], line.partition(SEP)[0]) for line in log.splitlines()
    ]


def _described(match: re.Match) -> str:
    """"scope: description" — the type is already the heading; the scope isn't."""
    return f"{match['scope']}: {match['desc']}" if match["scope"] else match["desc"]


def _classify(match: re.Match | None, subject: str) -> tuple[str, str]:
    """(heading, entry) for one commit. `!` outranks the type: a breaking refactor is
    still breaking, and burying it under "Other changes" is how a migration gets missed.
    """
    if match and match["breaking"]:
        return BREAKING, _described(match)
    if match and match["type"] in SECTION_BY_TYPE:
        return SECTION_BY_TYPE[match["type"]], _described(match)
    return OTHER, subject  # out here the type *is* the context — keep it verbatim


def compare_link(base: str, ref: str) -> str:
    """The line GitHub contributed back when it generated part of the body."""
    server = os.environ.get("GITHUB_SERVER_URL") or DEFAULT_SERVER
    slug = os.environ.get("GITHUB_REPOSITORY") or DEFAULT_REPO
    return f"**Full Changelog**: {server}/{slug}/compare/{base}...{ref}"


def render(ref: str, base: str | None = None) -> str:
    """The whole Release body: the commits in `ref`, grouped, plus the compare link.

    The `chore(release)` bump for this very release is dropped as noise in its own notes.
    The compare link is always emitted — a release with nothing else to say should still
    say where to look.
    """
    require_full_history()
    base = base or previous_tag(ref)
    buckets: dict[str, list[str]] = {}
    for subject, sha in commits(ref, base):
        match = CONVENTIONAL.match(subject)
        if match and match["type"] == "chore" and match["scope"] == "release":
            continue
        heading, entry = _classify(match, subject)
        buckets.setdefault(heading, []).append(f"- {entry} ({sha})")

    sections = [
        f"### {heading}\n" + "\n".join(buckets[heading])
        for heading in HEADING_ORDER
        if heading in buckets
    ]
    return "\n\n".join([*sections, compare_link(base, ref)])


def main() -> int:
    if not 2 <= len(sys.argv) <= 3:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        print(render(sys.argv[1], sys.argv[2] if len(sys.argv) == 3 else None))
    except subprocess.CalledProcessError as exc:
        # capture_output holds git's diagnostic, and CalledProcessError's str() drops it.
        # Release-day debugging is the worst time to be handed a bare exit code.
        sys.exit(f"changelog: {' '.join(exc.cmd)} failed:\n{(exc.stderr or '').strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
