"""The "Next:" block every command prints after it succeeds.

One formatter so the hint that follows `grid login`, `grid sync` and `grid use` looks the same
wherever the reader lands: the commands aligned, each with the reason you'd run it.
"""
from __future__ import annotations


def print_next_steps(steps: list[tuple[str, str]]) -> None:
    """Print ``steps`` as an indented, blank-line-separated block; comments share one column."""
    width = max(len(cmd) for cmd, _ in steps)
    print("")
    print("Next:")
    for cmd, note in steps:
        print(f"  {cmd.ljust(width)}   # {note}")
    print("")
