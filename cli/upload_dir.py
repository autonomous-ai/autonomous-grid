"""`--dir LOCAL[:DEST]` — expand a local folder into the `files` list `--file` already produces.

Client-only (ADR 0033 D-j, issue 27). **No wire change**: the relay sees exactly the payload it sees
today, so there is no lockstep value here and no rollout order in either direction.

The bounds are what decide this design. `MAX_FILES` is 200, and a naive recursive walk of any real
directory blows past it immediately on `.git/`, `node_modules/`, `__pycache__/` — so the selection
rules ARE the feature. Without them the flag is unusable on its first try.

**A selection rule is not a validation rule, and this file only holds the first kind.**
`remote_task._collect_files` carries an explicit rule that path VALIDATION — `..`, `.git/`,
absolute — is deliberately not re-implemented client-side, because a second copy in another repo
drifts silently. That rule is intact:

  * a **selection** rule decides which local files to *offer*;
  * a **validation** rule decides which paths the relay *accepts*.

The relay stays the sole authority on the second. And neither direction of drift here is silent:
skipping a path the relay would have taken shows up as a missing file the user adds with `--file`,
and sending one it refuses produces the clean 422 that already exists.
"""
from __future__ import annotations

import os
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path

# Environment variables that make git IGNORE `-C` and resolve a different repository entirely.
# Measured: with `GIT_DIR`/`GIT_WORK_TREE` exported at another repo, `git -C <dir> ls-files` returns
# that repo's tracked paths and none of `<dir>`'s.
#
# A different class from `GIT_CONFIG_GLOBAL`, which `_list_subtree` deliberately leaves alone: these
# do not affect the ignore rules, they redirect which repository is being listed, and no listing of
# a folder the user just named has any reason to honour them. Plausible from a git hook or a CI
# wrapper that forgot to unset them. Scrubbed individually rather than by replacing the whole
# environment, so `core.excludesFile` and the rest of the user's own setup still apply.
_REDIRECTING_GIT_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")

# Ceiling on the one subprocess this module runs. Local and network-free — it reads the index and
# the ignore rules for ONE subtree — so this is headroom for a cold cache on a huge repository, not
# the expected cost. Exceeding it is not fatal: the walk falls back and SAYS it did.
LS_FILES_TIMEOUT_SECONDS = 10

# Directory names skipped at any depth, and REPORTED rather than refused. Two different reasons that
# happen to want the same treatment:
#
#   * `.git` and `.grid` are what grid-src `task_files._RESERVED_COMPONENTS` refuses — the
#     repository's own state. Walking into `.git/` would also upload the object store, which on any
#     real repository is the whole MAX_FILES budget several times over.
#   * `.claude` is ADR 0033 D-f: a project-scope agent config is code execution on the provider,
#     because print mode skips the workspace-trust dialog.
#
# Refusing the WHOLE upload because a folder happens to contain one of these is hostile — the rule's
# purpose is that the path must not reach the workspace, and skipping satisfies it exactly. That is
# the difference between this SELECTION rule and the relay's VALIDATION rule, which still refuses
# the same names outright when a caller sends them explicitly with `--file`.
_SKIPPED_COMPONENTS = frozenset({".git", ".grid", ".claude"})

# The same class as `.claude/`, as a FILE rather than a directory, so it needs its own rule. Any
# depth, matching the relay's `_AGENT_CONFIG_FILES`.
_SKIPPED_FILES = frozenset({".mcp.json"})


def _folded(component: str) -> str:
    """A path component reduced to the form a *filesystem* would collide on.

    The same three transforms as grid-src `task_files._folded`, and duplicated deliberately: this
    is a SELECTION rule, so the copy is not the "second authority on validation" the module
    docstring forbids. Without it `.CLAUDE/` sails past this filter and the relay refuses the ENTIRE
    upload with a 422 — taking every legitimately selected file with it — which is precisely the
    hostile outcome skipping exists to avoid. `mkdir .CLAUDE` once on a case-insensitive machine is
    all it takes.

    **Both directions of drift from the relay's copy are safe**, which is what makes duplicating it
    the right call rather than a risk: narrower here and the relay still refuses, loudly; wider here
    and a file is skipped, visibly, and re-added with `--file`.

      * **format characters removed** — `.gi<ZWSP>t` is a distinct string several filesystems fold
        to `.git`; git's own documented attack class.
      * **NFKC** — `.ｇit` (fullwidth `g`) normalizes to `.git`.
      * **casefold** — `.GIT` is the same directory on any case-insensitive filesystem, the default
        on macOS.

    Order matters: stripping first means a format character cannot hide inside a sequence NFKC
    would otherwise leave alone.
    """
    stripped = "".join(c for c in component if unicodedata.category(c) != "Cf")
    return unicodedata.normalize("NFKC", stripped).casefold()


def _is_reserved_dir(name: str) -> bool:
    """One predicate for both comparison sites — `_walk`'s prune and `_skip_reason`'s component
    loop. Sharing the SET was not enough: a name folded in one place and compared raw in the other
    is a filter that works only when the folder happens not to be a git repository."""
    return _folded(name) in _SKIPPED_COMPONENTS


@dataclass(frozen=True)
class Entry:
    """One selected file: where it is locally, where it goes, and how big it is.

    The size rides along from the walk that found it so the caller's bounds check needs no second
    `stat` — and, more to the point, so it can refuse BEFORE any content is read. Reading 1,847
    files into memory and only then refusing is the failure that ordering exists to prevent.
    """

    local: Path
    dest: str
    size: int


@dataclass(frozen=True)
class Listing:
    """What one listing attempt produced, with its two complaints kept APART.

    ⚠️ `note` and `incomplete` were one field, and conflating them cost two bugs at once. They are
    different kinds of fact:

      * `note` — HOW this was listed. "Not in a git work tree, so .gitignore was not applied" is
        informational; the listing is complete.
      * `incomplete` — the listing may be MISSING paths. A defect, and never a reason to call a
        folder empty.

    `from_git` is a third fact and must not be inferred from either being `None`: a git run that
    SUCCEEDS can still emit `incomplete`, and reading "git answered" off a null note made a
    successful-but-warned listing look like a fallback — which routed a permission problem to the
    "the folder is empty" sentence.
    """

    candidates: list[str]
    unreadable: list[str]
    note: str | None = None
    incomplete: str | None = None
    from_git: bool = False

    def notes(self) -> list[str]:
        """Whatever this listing has to say, for the report.

        At most ONE is set today — `_fallback` sets only `note`, the git-success path only
        `incomplete` — so the ordering here decides nothing yet. Stated rather than dressed up as a
        deliberate tie-break: a comment defending a case that cannot happen is a claim the code does
        not have, and the next reader would take it as evidence the pair had been reasoned about.
        `incomplete` is first for when that changes, because it is the one saying the upload may be
        short.
        """
        return [text for text in (self.incomplete, self.note) if text]


@dataclass(frozen=True)
class Selected:
    """What the walk found: the files to upload, everything it passed over, and how it looked.

    `notes` is not decoration. It carries the one fact a user cannot see for themselves — that the
    ignore rules they expect to apply were NOT applied, because git could not answer — and acting on
    it (adding a `.gitignore`, or naming files with `--file`) is the difference between an upload
    that works and one refused for being 1,800 files.
    """

    entries: tuple[Entry, ...]
    skipped: tuple[str, ...]
    notes: tuple[str, ...] = ()


def select(roots: list[tuple[str, str]]) -> Selected:
    """Expand each `(local_dir, dest_root)` into the files under it.

    Refusals are local and happen BEFORE the relay is contacted, exactly like `--file`'s.
    """
    entries: list[Entry] = []
    skipped: list[str] = []
    notes: list[str] = []
    for local, dest_root in roots:
        found, passed_over, said = _select_one(local, dest_root)
        entries.extend(found)
        skipped.extend(passed_over)
        notes.extend(said)
    return Selected(tuple(entries), tuple(skipped), tuple(notes))


def _select_one(local: str, dest_root: str) -> tuple[list[Entry], list[str], list[str]]:
    source = Path(local)
    _refuse_bad_root(local, source)
    prefix = _dest_prefix(local, dest_root)
    listing = _list_subtree(local, source)
    candidates, problems = listing.candidates, listing.unreadable

    entries: list[Entry] = []
    # A dict rather than a set: the report reads in walk order, and a reserved directory collapses
    # to ONE line (`bundle/.git/`) instead of one per file inside it.
    skipped: dict[str, None] = {f"{prefix}/{problem}": None for problem in problems}
    for relative in candidates:
        reason = _skip_reason(source, relative)
        if reason is not None:
            skipped.setdefault(f"{prefix}/{reason}", None)
            continue
        try:
            size = (source / relative).stat().st_size
        except OSError as exc:
            # It was a regular file a moment ago. Skipped and SAID rather than crashing the upload
            # or, worse, sending it as empty — the user can then name it with `--file` and get a
            # real error, instead of an agent silently reading nothing.
            skipped.setdefault(f"{prefix}/{relative} (unreadable: {exc.strerror or exc})", None)
            continue
        entries.append(Entry(source / relative, f"{prefix}/{relative}", size))
    if not entries:
        _refuse_empty_selection(local, prefix, source, skipped, listing)
    return entries, list(skipped), listing.notes()


def _refuse_empty_selection(local: str, prefix: str, source: Path, skipped: dict[str, None],
                            listing: Listing) -> None:
    """Nothing was selected — and WHY decides which sentence the user gets.

    An upload that quietly sends nothing is the worst outcome available here: the task runs, the
    agent finds no input, and the answer is wrong for a reason nothing anywhere states. So this
    always refuses. The causes are genuinely different actions, though, and collapsing them into
    "empty" would name a visibly-full directory as empty — which reads as a bug in this CLI.

    ⚠️ **Every COMPLAINT the listing made has to reach this message** — `incomplete`, and any path
    it could not read. Nothing here returns, so anything left out is destroyed rather than merely
    unused, and the user is told the folder is empty when the truth was a permission on their own
    machine. That is the whole reason `Listing` keeps `incomplete` apart from `note`.

    `note` is deliberately NOT in that rule: it describes HOW the listing was made, and on the
    genuinely-empty branch "not in a git work tree" explains nothing a reader needs — the walk found
    nothing and the walk is authoritative there. It rides along on the trouble branch only because
    that message is already enumerating causes.
    """
    # The re-walk is CLASSIFIED, never judged raw. A freshly `git init`-ed folder holds exactly one
    # thing — `.git/` — which `ls-files` cannot see and the prune records as a candidate on purpose,
    # so "the walk found something git did not" was true there and meant the opposite of what branch
    # three says: the cause is this tool's own reserved rule, not a `.gitignore` nobody wrote.
    walked, unreadable = _walk(source)
    kept: list[str] = []
    rewalked: dict[str, None] = {}
    for relative in walked:
        reason = _skip_reason(source, relative)
        if reason is None:
            kept.append(relative)
        else:
            rewalked.setdefault(reason, None)

    # `or`, not `+`: `skipped` already holds everything the real listing passed over, and the
    # re-walk is only consulted to explain an otherwise unexplained emptiness. Prefixed either way,
    # so every line in every report is a destination path rather than two kinds of thing.
    trouble = list(skipped) or [f"{prefix}/{item}" for item in [*unreadable, *rewalked]]
    if trouble:
        # `notes()`, not just `incomplete`: a refusal raised here happens BEFORE `select()` collects
        # this root's notes, so anything left out of the message is destroyed rather than merely
        # unused. The rule is unconditional — every note reaches the user — because which note
        # matters is precisely the judgement that has already been wrong twice in this file.
        raise SystemExit(
            f"Nothing to upload from {local}: every path under it was skipped.\n"
            "  " + "\n  ".join(trouble) + "\n"
            + "".join(f"{said}\n" for said in listing.notes())
            + "Name what you need with --file if one of these is the file you meant.")
    if listing.incomplete:
        # git said the listing may be short and nothing at all came back. Never "empty".
        raise SystemExit(f"Nothing to upload from {local}.\n{listing.incomplete}")
    if listing.from_git and kept:
        # git answered, listed nothing, and a plain walk DOES find files — so the ignore rules cover
        # all of them. git's own answer to `git add ./build` is to refuse and name `.gitignore`;
        # this mirrors that rather than inventing a force flag, and rather than silently overriding
        # the rules the user wrote down.
        #
        # The walk, not `iterdir()`: git lists FILES, so a folder holding nothing but empty
        # subdirectories makes it answer nothing while `iterdir()` still sees the subdirectory —
        # which would blame a `.gitignore` that does not mention it and send the user to edit the
        # wrong thing. Only reached when the selection is already empty, so it costs nothing in the
        # ordinary case.
        raise SystemExit(
            f"Nothing to upload from {local}: every file under it is ignored by .gitignore.\n"
            f"Pass the ones you need with --file, or use `grid project import` for a repository.")
    raise SystemExit(f"Cannot upload {local}: the folder is empty.")


def _skip_reason(source: Path, relative: str) -> str | None:
    """Why this path is not offered, as the text the report shows — or `None` to keep it.

    A reserved component answers with the DIRECTORY prefix rather than the file, so forty objects
    under `.git/` become one line. Everything else answers with the path itself.

    Order matters: the component and name rules are pure string work and cost nothing, while
    `is_symlink` touches the filesystem. `is_symlink` still comes before `is_file`, for
    `_collect_files`' reason — `is_file()` FOLLOWS the link, so asking it first would report a
    planted symlink as a perfectly ordinary file.
    """
    parts = relative.split("/")
    for index, part in enumerate(parts):
        if _is_reserved_dir(part):
            return "/".join(parts[:index + 1]) + "/"
    if _folded(parts[-1]) in _SKIPPED_FILES:
        return relative

    path = source / relative
    try:
        if path.is_symlink():
            # The TARGET is named, because "a link was skipped" does not tell anyone whether they
            # have just been protected from uploading a private key or merely lost a convenience
            # alias. Reading the link NAME is not following it: nothing opens the target.
            #
            # Returned from inside the `try` so that `is_file()` is never reached for a link —
            # `is_file()` follows it, and the discipline this whole rule rests on is that a planted
            # symlink is settled before anything touches what it points at.
            try:
                target = os.readlink(path)
            except OSError:
                target = "?"
            return f"{relative} → {target}"
        if not path.is_file():
            # A `--cached` path the user has deleted, a socket, or an embedded repository that
            # `ls-files --others` reported as a directory. Nothing here is uploadable.
            return f"{relative} (not a regular file)"
    except OSError as exc:
        # ⚠️ `Path.is_file()` and `is_symlink()` do NOT swallow EACCES. Measured on CPython 3.12:
        # pathlib's `_ignore_error` covers ENOENT / ENOTDIR / ELOOP and not EACCES, so both RAISE on
        # a path whose parent directory cannot be read.
        #
        # Reachable specifically on the GIT path: `ls-files --cached` lists a TRACKED file out of
        # the index without touching the disk, so a file inside a chmod-000 directory arrives here
        # as a candidate and then cannot be examined. The fallback walk never reaches this, because
        # `os.walk` has already declined to enter that directory — which is exactly why the walk's
        # `onerror` is not a substitute for this guard.
        #
        # Skipped and reported, like every other thing this walk cannot use; unguarded it escaped as
        # a raw traceback instead of the clean `SystemExit` this CLI answers everything else with.
        return f"{relative} (unreadable: {exc.strerror or exc})"
    return None


def _list_subtree(local: str, source: Path) -> Listing:
    """Ask git what is under `source`, falling back to a plain walk and SAYING SO.

    `git ls-files` run against a subdirectory lists that subtree only, relative to it — which is
    exactly the question, so no pathspec is needed and none is passed (a client path reaching git as
    a pathspec is its own trap: `a*b.txt` matches files nobody named). `-z` is required rather than
    tidy: without it git QUOTES unusual paths, and the quoted form is not a filename.

    `--cached --others --exclude-standard` is "tracked, plus untracked that the ignore rules keep" —
    the same one rule `remote/task_repo.list_files` leans on, for the same reason. Hand-rolling the
    match would be a second, worse implementation of gitignore semantics whose errors are silent.

    The user's own git config is deliberately NOT neutered the way `task_repo._run` neuters the
    provider's. `GIT_CONFIG_GLOBAL=/dev/null` there stops a hostile checkout executing anything;
    here it would disable `core.excludesFile`, i.e. the global gitignore that `--exclude-standard`
    exists to honour. This is the user's own machine and their own rules are the point.
    """
    try:
        done = subprocess.run(
            ["git", "-C", str(source), "ls-files", "-z",
             "--cached", "--others", "--exclude-standard"],
            capture_output=True, timeout=LS_FILES_TIMEOUT_SECONDS,
            env={key: value for key, value in os.environ.items()
                 if key not in _REDIRECTING_GIT_VARS})
    except subprocess.TimeoutExpired:
        return _fallback(source, (
            f"{local}: git took longer than {LS_FILES_TIMEOUT_SECONDS}s to list it, so .gitignore "
            f"was NOT applied and everything under it was considered."))
    except OSError as exc:
        return _fallback(source, (
            f"{local}: git could not be run ({exc}), so .gitignore was NOT applied and everything "
            f"under it was considered."))
    if done.returncode != 0:
        # Overwhelmingly "not a git repository", which is an ordinary and documented way to use this
        # flag — a folder of assets or fixtures is the case `--dir` exists for. Said anyway, because
        # "your ignore rules were not applied" is the fact that explains a surprising file count,
        # and a user who BELIEVED they were in a work tree has just learned they are not.
        return _fallback(source, (
            f"{local} is not in a git work tree, so .gitignore was not applied."))
    # `surrogateescape`, matching how Python decoded argv: a filename that is not valid UTF-8 still
    # round-trips back to the filesystem, so it can be opened. The relay is the authority on whether
    # such a path is acceptable and refuses it by name — this side must not decide that quietly.
    listed = done.stdout.decode("utf-8", "surrogateescape")
    return Listing(
        candidates=sorted(path for path in listed.split("\0") if path),
        unreadable=[],
        incomplete=_incomplete_listing_note(local, done.stderr),
        from_git=True)


def _fallback(source: Path, note: str) -> Listing:
    """git could not answer, so walk — and carry the reason AND whatever the walk could not read.

    `from_git=False` is explicit rather than inferred: the git path can also produce a complaint,
    and reading "git answered" off a null note is what once routed a permission problem to the
    "the folder is empty" sentence.
    """
    found, unreadable = _walk(source)
    return Listing(candidates=found, unreadable=unreadable, note=note, from_git=False)


# How many of git's own complaint lines the note repeats before it summarizes. A repository with a
# whole unreadable tree can produce one per directory.
_MAX_GIT_WARNING_LINES = 5


def _incomplete_listing_note(local: str, stderr: bytes) -> str | None:
    """git's complaints about a listing it nevertheless called a SUCCESS.

    ⚠️ **A zero exit code does not mean the listing is complete.** Measured on git 2.54.0:
    `ls-files --others` over a directory it cannot open prints
    `warning: could not open directory 'blocked/': Permission denied` on **stderr** and exits **0**,
    with the files under it simply absent from stdout. Branching on the return code alone therefore
    reads a partial listing as a whole one, and those files are dropped with no trace anywhere —
    which is the exact silent skip this module exists to prevent, arriving through the path that
    most `--dir` calls take, since the flag is at its best inside a work tree.

    `os.walk`'s `onerror` does not cover this: that fires only on the FALLBACK.

    Passed through verbatim rather than parsed. git owns this text, the warnings it can emit are not
    an enumerable set, and guessing which ones mean "files are missing" would go quietly wrong on
    the first one nobody predicted. The framing says *may* be missing for the same reason: a warning
    does not say how many paths it cost, and claiming a number would be inventing one.
    """
    text = stderr.decode("utf-8", "replace").strip()
    if not text:
        return None
    lines = text.splitlines()
    shown = lines[:_MAX_GIT_WARNING_LINES]
    if len(lines) > _MAX_GIT_WARNING_LINES:
        shown.append(f"…and {len(lines) - _MAX_GIT_WARNING_LINES} more")
    return (f"{local}: git reported a problem while listing it, so files may be MISSING from this "
            f"upload:\n  " + "\n  ".join(shown))


def _refuse_bad_root(local: str, source: Path) -> None:
    """`--dir` names a directory. Everything else is refused by name, before anything is read.

    `is_symlink` FIRST, for `_collect_files`' reason: `exists()` and `is_dir()` both follow the link,
    so checking them first would report a planted symlink as a perfectly ordinary directory. The
    user named THIS path, so refusing is right here — inside the walk it is a skip instead, because
    there the user named the folder and not the link.
    """
    if source.is_symlink():
        raise SystemExit(
            f"Refusing to upload {local}: it is a symlink, and walking what it points at would "
            f"send files you did not name. Pass the target directly if you meant it.")
    if not source.exists():
        raise SystemExit(f"Cannot upload {local}: no such directory.")
    if not source.is_dir():
        raise SystemExit(
            f"Cannot upload {local}: it is not a directory. Use --file for a single file.")


def _dest_prefix(local: str, dest_root: str) -> str:
    """Where this folder's contents land, with no trailing slash.

    Defaults to the folder's OWN basename — `--dir ./fixtures` → `fixtures/a.json` — matching
    `--file ./conf.toml` → `conf.toml`. `Path('.').name` is `''`, so a caller who wrote `--dir .`
    falls back to the resolved directory's real name rather than uploading to the workspace root,
    which is deliberately not expressible.
    """
    prefix = dest_root.rstrip("/")
    if not prefix:
        prefix = os.path.basename(os.path.abspath(local))
    if not prefix:
        raise SystemExit(
            f"Cannot work out where {local} should land. Give a destination: --dir {local}:<path>")
    return prefix


def _walk(source: Path) -> tuple[list[str], list[str]]:
    """Every file under `source` as `/`-joined relative paths, sorted, plus what could not be read.

    The second half is not an afterthought: see `unreadable` below.

    `followlinks=False` is `os.walk`'s default and is load-bearing rather than incidental: a
    symlinked directory is never descended into, so its contents can never be uploaded.

    Paths are built with `as_posix()` and never with `os.sep` — on Windows the latter would put
    `a\\b.txt` on the wire, which is one path component to git and to the relay, not two.
    """
    found: list[str] = []
    problems: list[str] = []

    def unreadable(exc: OSError) -> None:
        """`os.walk`'s default `onerror` is `None`, which SWALLOWS this and walks on.

        Left alone, an unreadable subdirectory contributes nothing and says nothing: the upload
        succeeds, the agent runs, and the files under it were never sent. That is precisely the
        silent skip this module's report exists to prevent, arriving through the one door that
        reports nothing by default.
        """
        where = Path(exc.filename) if exc.filename else source
        try:
            shown = where.relative_to(source).as_posix()
        except ValueError:
            shown = str(where)
        problems.append(f"{shown} (unreadable: {exc.strerror or exc})")

    for dirpath, dirnames, filenames in os.walk(source, onerror=unreadable):
        here = Path(dirpath)
        keep = []
        for name in dirnames:
            if _is_reserved_dir(name) or (here / name).is_symlink():
                # Recorded as a candidate and NOT descended into, so the one `_skip_reason` filter
                # classifies and reports it — rather than a second copy of the rule living here and
                # drifting from the one that judges git's listing.
                found.append((here / name).relative_to(source).as_posix())
                continue
            keep.append(name)
        # In place, because `os.walk` reads this list back to decide where to recurse. Rebinding the
        # name would prune nothing and walk the whole object store.
        dirnames[:] = keep
        for name in filenames:
            found.append((here / name).relative_to(source).as_posix())
    return sorted(found), problems
