"""Regression tests for packaging/build_binary.sh — where a onefile binary unpacks itself.

Nuitka's ``--onefile`` bootstrap unpacks the real program into ``--onefile-tempdir-spec`` and, when
that directory already exists, rewrites the files that differ IN PLACE. The spec used to name a
constant (``--product-version="0.1.0"`` → ``localagi-grid/0.1.0-<os>``), so every release shared one
directory: an upgrade rewrote the previous release's ``grid.bin`` on the same inode, and on Apple
Silicon — which caches a signed binary's code signature per vnode — every later exec of it was
SIGKILLed, the new release and the old one alike, while ``codesign --verify`` still read "valid on
disk". It surfaced the first time the harness moved its managed grid (0.3.47 → 0.3.50): every Mac
that had run 0.3.47 could no longer run ``grid`` at all.

So each release must unpack into its own directory, named by the version ``pyproject.toml`` carries.
"""

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_SH = REPO_ROOT / "packaging" / "build_binary.sh"


def _script() -> str:
    return BUILD_SH.read_text()


def _version_line() -> str:
    lines = [line for line in _script().splitlines() if line.startswith("GRID_VERSION=")]
    assert len(lines) == 1, "build_binary.sh must derive GRID_VERSION exactly once"
    return lines[0]


def _derive(tmp_path: Path, version: str) -> str:
    """Run the script's own GRID_VERSION line against a pyproject.toml carrying ``version``."""
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "grid"\nversion = "{version}"\n')
    result = subprocess.run(
        ["bash", "-c", f'{_version_line()}\nprintf "%s" "$GRID_VERSION"'],
        cwd=tmp_path,
        env={"BUILD_PY": sys.executable, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def test_the_version_comes_from_pyproject(tmp_path):
    assert _derive(tmp_path, "1.2.3") == "1.2.3"


def test_a_suffix_is_cut_to_the_numeric_version_nuitka_accepts(tmp_path):
    # --product-version takes up to four numbers; a local or dev suffix would fail the build.
    assert _derive(tmp_path, "1.2.3+dev") == "1.2.3"
    assert _derive(tmp_path, "2.0.0rc1") == "2.0.0"


def test_each_release_unpacks_into_its_own_directory():
    spec = re.search(r'--onefile-tempdir-spec="([^"]+)"', _script())
    assert spec, "the onefile tempdir spec is gone — where does the binary unpack now?"
    assert "${GRID_VERSION}" in spec.group(1), (
        "the unpack directory must be named by the release, or an upgrade rewrites the previous "
        "release's binary in place (SIGKILL on Apple Silicon)"
    )


def test_the_product_version_is_the_release_not_a_constant():
    product = re.search(r'--product-version="([^"]+)"', _script())
    assert product and product.group(1) == "$GRID_VERSION"
