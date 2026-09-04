"""The "Next:" block every command prints after it succeeds.

One formatter so the hint that follows `grid login`, `grid sync` and `grid use` looks the same
wherever the reader lands: the commands aligned, each with the reason you'd run it.
"""
from __future__ import annotations

import sys


def print_next_steps(steps: list[tuple[str, str]]) -> None:
    """Print ``steps`` as an indented, blank-line-separated block; comments share one column."""
    width = max(len(cmd) for cmd, _ in steps)
    print("")
    print("Next:")
    for cmd, note in steps:
        print(f"  {cmd.ljust(width)}   # {note}")
    print("")


def print_env_hint(command: str) -> None:
    """Say what the two ``export`` lines above are FOR — to **stderr**, and only on a terminal.

    ``info --env`` prints a block that exists to be evaluated (``eval "$(grid info --env)"`` is the
    documented recipe, and a test evaluates it), so a single explanatory word on stdout would be fed
    to the shell. stderr carries the explanation where a person reads it and no pipe ever sees it;
    the tty check keeps it out of a script's error log too. Someone who has never met this command
    otherwise gets two lines of shell and no hint that they are meant to run them, in this terminal,
    before starting the agent.
    """
    if not sys.stdout.isatty():
        return
    # The exports go to stdout and this goes to stderr: without the flush the two streams can reach
    # the terminal out of order, and the hint reads as if it were about something above it.
    sys.stdout.flush()
    print(f'\nTo point a coding agent at this grid, run:  eval "$({command})"', file=sys.stderr)
    print("Then start opencode, codex, or any agent that reads OPENAI_* in that same terminal.",
          file=sys.stderr)
