"""The launch-target registry: every app `grid launch` knows how to start.

One entry today. A second target is a new module plus a line in ``TARGETS`` — the targets know
nothing about this table, so adding one cannot change an existing one.
"""
from __future__ import annotations

from . import claude
from .target import LaunchTarget

TARGETS: dict[str, LaunchTarget] = {
    claude.CLAUDE.name: claude.CLAUDE,
}


def names() -> tuple[str, ...]:
    """Every registered target name, in registration order."""
    return tuple(TARGETS)


def get(name: str) -> LaunchTarget | None:
    """The target registered under ``name``, or ``None`` — the caller renders the error."""
    return TARGETS.get(name)
