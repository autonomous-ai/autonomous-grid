"""Listing and deletion of local GGUF model files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from shared import paths


@dataclass(frozen=True)
class StoredModel:
    name: str
    path: Path
    size_bytes: int


def list_all() -> list[StoredModel]:
    model_dir = paths.models_dir()
    if not model_dir.exists():
        return []
    out: list[StoredModel] = []
    for entry in sorted(model_dir.iterdir()):
        if not entry.is_file():
            continue
        if entry.suffix in (".part", ".tmp"):
            continue
        out.append(StoredModel(name=entry.name, path=entry, size_bytes=entry.stat().st_size))
    return out


def find(name: str) -> StoredModel | None:
    target = paths.models_dir() / Path(name).name
    if target.is_file():
        return StoredModel(name=target.name, path=target, size_bytes=target.stat().st_size)
    return None


def remove(name: str) -> bool:
    model = find(name)
    if not model:
        return False
    model.path.unlink()
    return True

