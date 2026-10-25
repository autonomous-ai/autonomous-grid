"""Where this CLI's releases live, and the one command that adds the training extra.

The CLI ships only as this repository's GitHub release: the wheel, the Linux binaries and
``SHA256SUMS``. It is not on PyPI, where ``grid`` is an unrelated package with no ``train`` extra —
so asking ``pip`` for the extra installs the wrong thing, and a ``uv tool`` install (the installer's
wheel path) has no ``pip`` inside it anyway.
"""
from __future__ import annotations

import os
import shutil

from shared._version import __version__

#: install.sh bootstraps uv only on its wheel path, so a Linux binary install usually has none.
UV_INSTALLER = "curl -LsSf https://astral.sh/uv/install.sh | sh"


def repo() -> tuple[str, str]:
    """(owner, repo) of the releases to track — the same env knobs install.sh honours."""
    return (
        os.environ.get("GRID_REPO_OWNER", "autonomous-ai"),
        os.environ.get("GRID_REPO_NAME", "autonomous-grid"),
    )


def release_base(version: str) -> str:
    owner, name = repo()
    return f"https://github.com/{owner}/{name}/releases/download/v{version}"


def wheel_url(version: str) -> str:
    return f"{release_base(version)}/grid-{version}-py3-none-any.whl"


def train_requirement(version: str) -> str:
    """Our own release wheel with the ``train`` extra, as a requirement ``uv tool install`` takes."""
    return f"grid[train] @ {wheel_url(version)}"


def train_install_command(version: str = __version__) -> str:
    """What every missing-training-dependency message tells the user to run.

    It reinstalls the running version with the extra. ``--force`` is what lets it replace a Linux
    binary on the same path (a binary cannot take an extra), and ``grid update`` on the wheel keeps
    the extra afterwards (``cli.update._update_wheel``). Without ``uv`` on PATH, the line installs it
    first. A source checkout (``+dev``) has no release wheel, so it is told to install the extra
    into its own environment instead."""
    if "+dev" in version:
        return "uv pip install -e '.[train]'  (in your source checkout)"
    command = f'uv tool install --force "{train_requirement(version)}"'
    return command if shutil.which("uv") else f"{UV_INSTALLER} && {command}"
