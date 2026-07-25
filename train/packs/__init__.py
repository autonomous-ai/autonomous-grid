"""Task packs: ready-made training setups for common business data shapes.

A pack is a directory template — config, data-prep script, reward functions, sample data, and
a README — that `grid train init --pack <name>` copies into the user's workspace. Packs answer
the hardest question in this product ("how do I turn *my* data into tasks and rewards?") with
an opinionated, editable starting point instead of a framework: everything a pack installs is
plain files the user owns and edits.

Registry = subdirectories of this package containing a PACK.md (installed as README.md).
"""
from __future__ import annotations

import shutil
from pathlib import Path

_ROOT = Path(__file__).parent


def available_packs() -> dict[str, str]:
    """name -> one-line description (first non-heading line of PACK.md)."""
    packs: dict[str, str] = {}
    for pack_dir in sorted(_ROOT.iterdir()):
        manifest = pack_dir / "PACK.md"
        if not manifest.is_file():
            continue
        description = next(
            (
                line.strip()
                for line in manifest.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.startswith("#")
            ),
            "",
        )
        packs[pack_dir.name.replace("_", "-")] = description
    return packs


def install_pack(name: str, dest: Path) -> list[Path]:
    """Copy pack files into `dest` (created if needed); refuses to overwrite. Returns paths."""
    source = _ROOT / name.replace("-", "_")
    if not (source / "PACK.md").is_file():
        known = ", ".join(available_packs()) or "(none bundled)"
        raise SystemExit(f"grid train: unknown pack {name!r}. Available: {known}")
    dest.mkdir(parents=True, exist_ok=True)
    installed: list[Path] = []
    for src in sorted(source.iterdir()):
        if src.name in ("__pycache__",):
            continue
        target = dest / ("README.md" if src.name == "PACK.md" else src.name)
        if target.exists():
            raise SystemExit(f"grid train: {target} exists — install into an empty directory")
        shutil.copy2(src, target)
        installed.append(target)
    return installed
