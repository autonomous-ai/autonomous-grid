"""How this harness runs git, and the read-only discipline it owes the operator's own repository.

One module because the discipline is cross-cutting, not a detail of whichever measurement happens
to need git next: every call is made with no system or global configuration, a fixed identity, and
literal pathspecs. A measurement that inherited the operator's `~/.gitconfig` would measure their
aliases, their `core.excludesFile` and their merge driver — and `info/exclude` is precisely the kind
of question a stray `core.excludesFile` would answer wrongly and invisibly.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Generous: these run against a 792 MiB repository and a slow disk is not a failure.
DEFAULT_TIMEOUT_SECONDS = 3600.0


@dataclass(frozen=True)
class Git:
    """What one git invocation did — kept whole, because a measurement's evidence IS its stderr."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    seconds: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def env(*, literal_pathspecs: bool = True) -> dict[str, str]:
    """A git environment with nothing of the operator's in it.

    `GIT_CONFIG_GLOBAL=/dev/null` and `GIT_CONFIG_NOSYSTEM=1` together are what make a number
    reproducible on another machine. `GIT_LITERAL_PATHSPECS` is here for the reason `task_repo._env`
    carries it: a path that reaches git as a pathspec matches things nobody named, and `:odd.txt`
    matches NOTHING — an existing file then looks absent, which in a measurement reads as a result.

    ⚠️ **`check-ignore` cannot be run with it, and that is measured, not assumed.** On git 2.54.0,
    `GIT_LITERAL_PATHSPECS=1 git check-ignore -v <path>` exits **128** with
    `fatal: <path>: pathspec magic not supported by this command: 'literal'` — with and without
    `--no-index`. The exclude probe found this by failing its own positive control, which is what
    that control is for: a probe that could not run at all would otherwise have reported "not
    ignored" for both rows, and "not ignored in the per-worktree location" is exactly the reading
    the answer `common` is built from. Dropped for that one call, by name, rather than removed
    everywhere.
    """
    variables = {
        "PATH": os.environ.get("PATH", ""),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": "/nonexistent",
        "GIT_AUTHOR_NAME": "measure", "GIT_AUTHOR_EMAIL": "measure@invalid",
        "GIT_COMMITTER_NAME": "measure", "GIT_COMMITTER_EMAIL": "measure@invalid",
    }
    if literal_pathspecs:
        # Set or ABSENT, never `=0`: git parses the value as a boolean today, but a variable whose
        # meaning depends on parsing is one a future git could read differently, and "unset" is the
        # only spelling of off that cannot be reinterpreted.
        variables["GIT_LITERAL_PATHSPECS"] = "1"
    return variables


def run(*args: str, cwd: Path | None = None, check: bool = True,
        literal_pathspecs: bool = True, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> Git:
    """One git call, timed, with its output kept whole.

    `check=False` is the interesting mode and it is not laziness: several measurements here are
    ABOUT git failing — a conflicting `merge-tree` exits 1, a contended fetch exits 128 — so the
    exit code is data. `check=True` is for the setup steps, where a failure is the harness's bug
    and must not be mistaken for a finding.
    """
    import time

    argv = ("git", *args)
    started = time.monotonic()
    proc = subprocess.run(argv, cwd=None if cwd is None else str(cwd),
                          env=env(literal_pathspecs=literal_pathspecs),
                          capture_output=True, text=True, errors="replace",
                          timeout=timeout, stdin=subprocess.DEVNULL)
    result = Git(argv=argv, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr,
                 seconds=time.monotonic() - started)
    if check and not result.ok:
        raise RuntimeError(f"{' '.join(argv)} exited {result.returncode}: "
                           f"{(result.stderr or result.stdout).strip()}")
    return result


@dataclass(frozen=True)
class TreeSize:
    """What a directory weighs, and how much of it could not be weighed.

    Two fields rather than an int, because a walk that skipped files and a walk that found none are
    different observations and only one of them supports a comparison — the harness's own rule,
    applied to its own measuring instrument.
    """

    total_bytes: int
    skipped_files: int

    def __int__(self) -> int:
        return self.total_bytes


def tree_bytes(path: Path) -> TreeSize:
    """Every byte under `path`, following no symlink and counting each file once.

    `du` is not used: its answer is in disk BLOCKS and varies with the filesystem, so two machines
    would disagree about a number this harness exists to make comparable. Apparent size is the
    property being compared — "does a worktree cost a second copy of the history" — and it is
    filesystem-independent.

    Anything that cannot be weighed is SKIPPED AND COUNTED. Skipping alone was the original
    behaviour and its comment argued the race case correctly — a file vanishing mid-walk cannot
    bias a comparison whose sides are walked alike. But the same `except` also swallows systematic
    causes (a permission, a path-length limit), and a one-sided systematic skip undercounts that
    side with nothing in the report to show for it. The count is what makes the difference visible.

    ⚠️ **`os.walk(onerror=…)` and not `Path.rglob`, and that is the whole point of the change.**
    Measured: on a directory the walker cannot enter, `rglob` yields the directory itself and then
    silently omits everything beneath it — no exception reaches the caller, so a per-file
    `except OSError` counts **zero** skips while an entire subtree goes unweighed. `os.walk` is the
    only spelling here that hands the traversal error to us at all.
    """
    import os

    total = skipped = 0
    errors: list[OSError] = []
    for root, _dirs, files in os.walk(path, onerror=errors.append):
        for name in files:
            entry = Path(root) / name
            try:
                if entry.is_symlink() or not entry.is_file():
                    continue
                total += entry.stat().st_size
            except OSError:
                skipped += 1
    # A directory that could not be listed is one skip, not one per file it held — nobody can know
    # how many that was, and inventing a number would be the fabrication this counter exists to
    # prevent. What matters is that the count is non-zero.
    return TreeSize(total_bytes=total, skipped_files=skipped + len(errors))
